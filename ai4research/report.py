"""Report compilation (design §13). A report is a deterministic view over the run's
artifacts — it never invents content. Findings render only from accepted claims and
their resolved citations; a blocked run renders a diagnostic report (coverage + gaps,
no findings) instead. A minimal Markdown->HTML renderer keeps the run dependency-free.
"""
from __future__ import annotations

import html as _html
import re

from .workfiles import WorkStore


class ReportData:
    """Everything the report needs, assembled from the working files."""

    def __init__(self, work: WorkStore):
        rows = work.read_rows
        self.run = (rows("runs") or [{}])[0]
        self.contract = (rows("research_contracts") or [{}])[0]
        self.dossier = (rows("quality_dossier") or [{}])[0]
        self.gate_results = rows("gate_results")
        self.repair_tasks = rows("repair_tasks")
        self.containers = rows("source_containers")
        self.items = rows("selected_source_items")
        self.attempts = rows("acquisition_attempts")
        self.documents = work.read_documents()
        self.spans = rows("spans")
        self.evidence = rows("evidence")
        self.claims = rows("claims")
        self.claim_evidence = rows("claim_evidence")
        self.claim_edges = rows("claim_edges")
        self.citations = rows("citations")
        self.figures = rows("figures")
        self.answer = rows("answer")
        self.question_nodes = rows("question_graph_nodes")
        self.entities = rows("entities")
        self.claim_entities = rows("claim_entities")
        self.approved = bool(self.dossier.get("approved_for_report_rendering"))

        self.by_item = {i["selected_item_id"]: i for i in self.items}
        self.by_doc = {d["document_id"]: d for d in self.documents}
        self.by_span = {s["span_id"]: s for s in self.spans}
        self.by_evidence = {e["evidence_id"]: e for e in self.evidence}
        self.by_claim = {c["claim_id"]: c for c in self.claims}
        self.cite_by_evidence = {c["evidence_id"]: c for c in self.citations}
        # each claim's and each evidence's source container — for the cross-source counter-search render.
        _item_container = {i["selected_item_id"]: i.get("container_id") for i in self.items}
        self.container_by_evidence = {e["evidence_id"]: _item_container.get(e.get("selected_item_id"))
                                      for e in self.evidence}
        _support: dict = {}
        for ce in self.claim_evidence:
            _support.setdefault(ce["claim_id"], []).append(ce["evidence_id"])
        self.container_by_claim = {cid: next((self.container_by_evidence.get(e) for e in eids
                                              if self.container_by_evidence.get(e)), None)
                                   for cid, eids in _support.items()}
        self.container_label = {c["container_id"]: (c.get("label") or c["container_id"]) for c in self.containers}


def _summary(d: ReportData) -> list[str]:
    """The 'How this was researched' methodology surface — provenance in plain terms (angles →
    sources → claims → findings), scope, and any advisories — computed from real artifact counts,
    not raw gate tallies."""
    angles = sum(1 for n in d.question_nodes if n.get("type") == "sub_question")
    yt = sum(1 for c in d.containers if c.get("source_pack_type") == "youtube_channel")
    gh = sum(1 for c in d.containers if c.get("source_pack_type") == "github_repo")
    findings = len((d.answer[0].get("key_findings") or [])) if d.answer else 0
    mix = ", ".join(p for p in [f"{yt} YouTube" if yt else "", f"{gh} GitHub" if gh else ""] if p) \
        or f"{len(d.containers)} container(s)"
    window = d.contract.get("freshness_window_days")
    method = (
        f"Compiled from {len(d.documents)} sources ({mix}) on **{d.run.get('topic')}**. "
        f"The pipeline decomposed the topic into {angles} angle(s), extracted {len(d.evidence)} "
        f"evidence spans into {len(d.claims)} claims, "
        + (f"and synthesized {findings} findings — every citation checked against the evidence, "
           "ungrounded ones dropped."
           if findings else
           "rendered as the evidence-backed claims below (no LLM synthesis on this run).")
    )
    scope = ("Scope: within the provided source set; no live web retrieval (Phase 0)."
             + (f" Freshness window: {window} days." if window else ""))
    out = ["## How this was researched", "", method, "", scope, ""]
    advisories = [g for g in d.gate_results if g.get("status") == "warning"]
    if advisories:
        for g in advisories:
            issues = "; ".join(g.get("issues") or []) or g["gate_id"]
            out.append(f"- _Advisory:_ {issues}")
        out.append("")
    return out


def _answer_section(d: ReportData) -> list[str]:
    """The synthesized dossier (Increment 5) — present only when an LLM run produced a grounded
    answer: a long summary, an at-a-glance findings list with confidence, thematic angle sections,
    a forward outlook, and caveats / open questions. Inline [evidence_id] refs become deep links."""
    if not d.answer:
        return []
    a = d.answer[0]
    out = ["## Summary", ""]
    summary = _linkify_evidence(a.get("summary", ""), d)
    if summary:
        out += [summary, ""]
    findings = a.get("key_findings", [])
    if findings:
        out += ["### Key findings at a glance", ""]
        for item in findings:
            text = _linkify_evidence((item.get("finding") or "").strip(), d)
            if not text:
                continue
            cites = _cites(item.get("evidence_ids", []), d)
            conf = (item.get("confidence") or "").strip().capitalize()
            tag = f"  _({conf} confidence)_" if conf else ""
            out.append(f"- {text}" + ((" " + cites) if cites else "") + tag)
        out.append("")
    for sec in a.get("sections", []):
        title = _linkify_evidence((sec.get("title") or "").strip(), d)   # B2: titles too — no raw ids in a heading
        body = _linkify_evidence(sec.get("body", ""), d)
        if title and body:
            out += [f"### {title}", "", body, ""]
    outlook = a.get("outlook") or []
    if outlook:
        out += ["### Where it's heading", ""] + [f"- {_linkify_evidence(x, d)}" for x in outlook] + [""]
    caveats = a.get("caveats") or []
    open_q = a.get("open_questions") or []
    if caveats or open_q:
        out += ["### Caveats & open questions", ""]
        out += [f"- {_linkify_evidence(x, d)}" for x in caveats]
        out += [f"- _Open:_ {_linkify_evidence(x, d)}" for x in open_q]
        out.append("")
    return out


_EVID_TOKEN = re.compile(r"\[?\b(run_[A-Za-z0-9_]+\.EV\d+)\b\]?")


def _linkify_evidence(text: str, d: ReportData) -> str:
    """Resolve inline evidence-id references in synthesized prose to citation deep links — whether
    the model wrote them bracketed (`[run_..EV0001]`) or bare (`run_..EV0001`). A raw internal
    evidence id must NEVER survive into reader-facing prose (it breaks deep-link auditability), so
    any evidence-id token that can't be resolved to a citation is stripped."""
    def repl(match):
        cite = d.cite_by_evidence.get(match.group(1))
        if cite:
            return f"[{cite['label']}]({cite['url']})" if cite.get("url") else f"[{cite['label']}]"
        return ""   # unknown / unresolved id -> strip; never leak a raw run_..EV.. token
    out = _EVID_TOKEN.sub(repl, text or "")
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _cites(evidence_ids: list, d: ReportData) -> str:
    parts = []
    for eid in evidence_ids:
        cite = d.cite_by_evidence.get(eid)
        if cite:
            parts.append(f"[{cite['label']}]({cite['url']})" if cite.get("url") else f"[{cite['label']}]")
    return " ".join(parts)


def _cell(value) -> str:
    """Sanitize a value for a Markdown table cell: collapse newlines and neutralize `|`, which
    source text (e.g. a README that itself contains a Markdown table) would otherwise use to
    split the row into spurious columns."""
    return str(value).replace("\n", " ").replace("|", "¦")


def _coverage(d: ReportData) -> list[str]:
    out = ["## Source And Acquisition Coverage", "",
           "| Container | Type | Item | Acquisition |", "| --- | --- | --- | --- |"]
    status_by_item = {a["selected_item_id"]: a for a in d.attempts}
    for c in d.containers:
        items = [i for i in d.items if i["container_id"] == c["container_id"]]
        label = _cell(c.get("label") or c["container_id"])
        if not items:
            out.append(f"| {label} | {c['source_pack_type']} | _(no items — coverage gap)_ | — |")
        for i in items:
            att = status_by_item.get(i["selected_item_id"], {})
            verdict = att.get("status", "pending")
            if att.get("failure_code"):
                verdict += f" ({att['failure_code']})"
            out.append(f"| {label} | {c['source_pack_type']} | {_cell(i.get('title') or i['selected_item_id'])} | {verdict} |")
    out.append("")
    return out


def _findings(d: ReportData, *, as_appendix: bool = False) -> list[str]:
    # On a dossier run the synthesized "Key findings at a glance" is the headline; this 1:1
    # claim-per-evidence list is demoted to an auditable appendix so the report carries one
    # headline, not three overlapping "what we found" surfaces. On a deterministic (no-dossier)
    # run it stays the lead findings surface.
    if as_appendix:
        out = ["## Full claim ledger (appendix)", "",
               "Every accepted (and qualified) claim, one-to-one with its evidence — the auditable "
               "expansion of the findings synthesized above.", ""]
    else:
        out = ["## Evidence-Backed Findings", ""]
    # Qualified claims (#22 C1) stay in the ledger, visibly hedged; rejected claims are excluded
    # (they surface only in the Contradictions section). On a no-runtime run nothing is qualified,
    # so this is accepted-only and byte-identical.
    shown = [c for c in d.claims if c["status"] in ("accepted", "qualified")]
    if not shown:
        out += ["_No accepted claims for this run._", ""]
        return out
    support = {}
    for ce in d.claim_evidence:
        support.setdefault(ce["claim_id"], []).append(ce["evidence_id"])
    for c in shown:
        labels = []
        for eid in support.get(c["claim_id"], []):
            cite = d.cite_by_evidence.get(eid)
            if cite:
                if cite.get("url"):
                    labels.append(f"[{cite['label']}]({cite['url']})")
                else:
                    labels.append(f"[{cite['label']}]")
        suffix = (" " + " ".join(labels)) if labels else ""
        prefix = "_(qualified)_ " if c["status"] == "qualified" else ""
        out.append(f"- {prefix}{c['claim_text']}{suffix}")
    out.append("")
    return out


def _contradictions(d: ReportData) -> list[str]:
    """#22/9/11: cross-source disagreement + rejected/qualified claims, straight from the claim
    graph. Inc 11 writes claim<->claim disagreement edges (from_id is a claim) rendered as "A
    disagrees with B"; the older counter-search writes claim<->evidence edges (from_id is evidence)
    rendered as the claim *disputed by* counter-evidence. Returns [] when the graph holds none, so a
    no-runtime run — which can produce neither — stays byte-identical."""
    edges = [e for e in d.claim_edges if e.get("type") in ("refutes", "qualifies")]
    pair_edges = [e for e in edges if e.get("from_id") in d.by_claim]        # claim <-> claim (Inc 11)
    ev_edges = [e for e in edges if e.get("from_id") not in d.by_claim]      # evidence <-> claim (#22)

    def _review_reason(c):   # the marker only the contradiction reviewer writes — NOT EntailmentGate
        for x in (c.get("limitations") or []):
            if isinstance(x, str) and x.startswith(("rejected on review", "qualified by")):
                return x
        return None

    # A hallucinated claim rejected by EntailmentGate carries no such marker and must stay hidden
    # (don't give fabricated text oxygen); only the contradiction review's reasoned verdicts surface.
    flagged = [c for c in d.claims if c.get("status") in ("rejected", "qualified") and _review_reason(c)]
    if not edges and not flagged:
        return []

    def _clip(text):
        text = (text or "").strip().replace("\n", " ")
        return text[:140] + ("…" if len(text) > 140 else "")

    def _pair_bullet(e):                                       # claim <-> claim, both stay accepted
        a, b = e["from_id"], e["to_id"]
        sa = d.container_label.get(d.container_by_claim.get(a), "one source")
        sb = d.container_label.get(d.container_by_claim.get(b), "another source")
        verb = "disagrees with" if e["type"] == "refutes" else "qualifies"
        return (f"- _{_clip(d.by_claim.get(a, {}).get('claim_text', a))}_ ({sa}) — "
                f"**{verb}** — _{_clip(d.by_claim.get(b, {}).get('claim_text', b))}_ ({sb})")

    def _dispute_bullet(e):
        claim_id, ev_id = e["to_id"], e["from_id"]              # evidence -> claim edge
        claim_text = d.by_claim.get(claim_id, {}).get("claim_text", claim_id)
        ev = d.by_evidence.get(ev_id, {})
        summ = (ev.get("summary") or ev.get("quoted_text") or "").strip().replace("\n", " ")[:160]
        src = d.container_label.get(d.container_by_evidence.get(ev_id), "another source")
        cite = d.cite_by_evidence.get(ev_id)
        link = f" ([{cite['label']}]({cite['url']}))" if cite and cite.get("url") else ""
        verb = "disputed by" if e["type"] == "refutes" else "qualified by"
        return f"- _{claim_text}_ — **{verb}** {src}: {summ}{link}"

    out = ["## Contradictions & rejected claims", ""]
    if pair_edges or ev_edges:                 # all cross-source by construction
        out += ["### Where sources disagree", ""]
        out += [_pair_bullet(e) for e in pair_edges] + [_dispute_bullet(e) for e in ev_edges] + [""]
    if flagged:
        out += ["### Rejected / qualified claims", ""]
        for c in flagged:
            reason = _review_reason(c) or ("rejected (no recorded reason)"
                                           if c["status"] == "rejected" else "qualified")
            out.append(f"- **[{c['status']}]** {c.get('claim_text', c['claim_id'])} — {reason}")
        out.append("")
    return out


def _evidence_table(d: ReportData) -> list[str]:
    out = ["## Evidence Table", "",
           "| Citation | Item | Evidence | Excerpt |", "| --- | --- | --- | --- |"]
    for ev in d.evidence:
        cite = d.cite_by_evidence.get(ev["evidence_id"])
        label = cite["label"] if cite else "—"
        item = d.by_item.get(ev["selected_item_id"], {})
        excerpt = _cell(ev["quoted_text"].strip())
        if len(excerpt) > 160:
            excerpt = excerpt[:157] + "..."
        out.append(f"| {label} | {_cell(item.get('title') or ev['selected_item_id'])} "
                   f"| {ev['evidence_type']} | {excerpt} |")
    out.append("")
    return out


def _figures(d: ReportData) -> list[str]:
    out = []
    for fig in d.figures:
        if fig.get("kind") != "comparison_matrix":
            continue
        spec = fig.get("spec") or {}
        columns = spec.get("columns") or []
        rows = spec.get("rows") or []
        if not columns or not rows:
            continue
        out += ["### Grounded Comparison Matrix", ""]
        out.append("| " + " | ".join(str(c) for c in columns) + " |")
        out.append("| " + " | ".join("---" for _ in columns) + " |")
        for row in rows:
            out.append("| " + " | ".join("—" if value is None else str(value) for value in row) + " |")
        out.append("")
    return out


_CONCEPT_CAP = 18   # most cross-source concepts shown; full ontology lives in the claim ledger


def _key_concepts(d: ReportData) -> list[str]:
    """The topic ontology made legible (#10c): the concepts the analysis turns on, where sources
    engage them, and which are CONTESTED — appearing in claims from >=2 independent sources, where
    cross-source disagreement lives. Renders nothing without an ontology, so the deterministic
    (no-runtime) report is unchanged."""
    if not d.entities or not d.claim_entities:
        return []
    accepted = {c["claim_id"] for c in d.claims if c.get("status") in ("accepted", "qualified")}
    ent = {e["entity_id"]: e for e in d.entities}
    by_entity: dict = {}
    for link in d.claim_entities:
        cid, eid = link.get("claim_id"), link.get("entity_id")
        if cid not in accepted or eid not in ent:
            continue
        rec = by_entity.setdefault(eid, {"claims": set(), "containers": set()})
        rec["claims"].add(cid)
        cont = d.container_by_claim.get(cid)
        if cont:
            rec["containers"].add(cont)
    ranked = sorted(((eid, len(r["containers"]), len(r["claims"])) for eid, r in by_entity.items()),
                    key=lambda r: (-r[1], -r[2]))
    if not ranked:
        return []
    cross_source = sum(1 for _, nsrc, _ in ranked if nsrc >= 2)
    out = ["## Key concepts", "",
           f"The concepts this topic turns on — {len(ranked)} derived from the claims, {cross_source} "
           "**cross-source** (engaged by ≥2 independent sources). ⚑ marks a cross-source concept — see "
           "*Where sources disagree* for the concepts sources actually conflict on.", ""]
    for eid, nsrc, nclaims in ranked[:_CONCEPT_CAP]:
        e = ent[eid]
        flag = "⚑ " if nsrc >= 2 else ""
        out.append(f"- {flag}**{e.get('canonical_name')}** ({e.get('entity_type') or 'Concept'}) — "
                   f"{nsrc} source{'s' if nsrc != 1 else ''}, {nclaims} claim{'s' if nclaims != 1 else ''}")
    out.append("")
    return out


def _gaps(d: ReportData) -> list[str]:
    out = ["## Gaps And Repair Tasks", ""]
    failed = [a for a in d.attempts if a["status"] != "succeeded"]
    for a in failed:
        out.append(f"- Acquisition {a['status']} for `{a['selected_item_id']}`"
                   + (f": {a['failure_code']}" if a.get("failure_code") else ""))
    for g in d.gate_results:
        if g["status"] in ("warning", "repairable_fail", "hard_fail"):
            issues = "; ".join(g.get("issues") or []) or g["status"]
            out.append(f"- Gate {g['gate_id']} [{g['status']}]: {issues}")
    for t in d.repair_tasks:
        out.append(f"- Repair task `{t['task_id']}` ({t['status']}): {t['description']}")
    if len(out) == 2:
        out.append("_No gaps, failed acquisitions, or repair tasks._")
    out.append("")
    return out


def _traceability(d: ReportData) -> list[str]:
    out = ["## Traceability Appendix", "",
           "Each accepted claim traces to evidence, span, document, item, and container.", ""]
    container_of_item = {i["selected_item_id"]: i["container_id"] for i in d.items}
    support = {}
    for ce in d.claim_evidence:
        support.setdefault(ce["claim_id"], []).append(ce["evidence_id"])
    for c in d.claims:
        if c["status"] != "accepted":
            continue
        for eid in support.get(c["claim_id"], []):
            ev = d.by_evidence.get(eid, {})
            out.append(
                f"- `{c['claim_id']}` -> `{eid}` -> span `{ev.get('span_id')}` -> "
                f"doc `{ev.get('document_id')}` -> item `{ev.get('selected_item_id')}` -> "
                f"container `{container_of_item.get(ev.get('selected_item_id'))}`"
            )
    out.append("")
    return out


def _full_source_appendix(d: ReportData) -> list[str]:
    out = ["## Full Source Appendix", "",
           "Every supplied container and item, including unused seeds.", ""]
    for c in d.containers:
        out.append(f"- **{c.get('label') or c['container_id']}** ({c['source_pack_type']})"
                   + (f" — {c.get('container_locator')}" if c.get("container_locator") else ""))
        for i in [x for x in d.items if x["container_id"] == c["container_id"]]:
            out.append(f"  - {i.get('title') or i['selected_item_id']} (`{i['selected_item_id']}`)")
    out.append("")
    return out


def render_markdown(d: ReportData) -> tuple[str, str]:
    """Returns `(markdown, kind)`; kind is 'markdown_report' (final) or 'diagnostic_report'."""
    topic = d.run.get("topic", "")
    if d.approved:
        title = f"Phase 0 Evidence Report: {topic}"
        if d.answer:
            # dossier run: the synthesized findings lead; "How this was researched" sits near the
            # end (like the exemplar), then the 1:1 claim ledger as an auditable appendix.
            body = (_answer_section(d) + _coverage(d) + _key_concepts(d) + _figures(d) + _evidence_table(d)
                    + _contradictions(d) + _gaps(d) + _traceability(d) + _summary(d)
                    + _findings(d, as_appendix=True) + _full_source_appendix(d))
        else:
            # deterministic run: the evidence-backed findings are the only headline (unchanged).
            # _contradictions is empty here (no refutes/qualifies edges or non-accepted claims
            # without a runtime), so the deterministic output stays byte-identical.
            body = (_coverage(d) + _findings(d) + _figures(d) + _evidence_table(d)
                    + _contradictions(d) + _gaps(d) + _traceability(d) + _summary(d)
                    + _full_source_appendix(d))
        kind = "markdown_report"
    else:
        title = f"Phase 0 Diagnostic Report: {topic}"
        body = (_summary(d)
                + ["> Rendering was blocked by a quality gate; findings are withheld.", ""]
                + _coverage(d) + _gaps(d) + _full_source_appendix(d))
        kind = "diagnostic_report"
    return "\n".join([f"# {title}", ""] + body), kind


def build_view_model(d: ReportData) -> dict:
    """The small data bundle the HTML header/metric-band/badge render from — derived purely
    from gated artifacts (no new content). Mirrors the §14 HtmlReportViewModel idea."""
    citations = [c for c in d.citations if c.get("url")]
    yt = sum(1 for c in citations if "&t=" in c["url"])
    gh = sum(1 for c in citations if "#L" in c["url"])
    accepted = sum(1 for c in d.claims if c.get("status") == "accepted")
    # ledger volume = what the "Full claim ledger" actually renders: accepted + qualified (#22 C1),
    # never rejected. Counting all claims (incl. rejected) would overstate the rendered ledger.
    ledger = sum(1 for c in d.claims if c.get("status") in ("accepted", "qualified"))
    # "findings" are the synthesized, ranked key findings (Increment 5) — the report's headline.
    # They are NOT the same as claims: claims include the 1:1 evidence-backed extractive claims
    # (the ledger volume), so surfacing claim count as "findings" would overstate the result.
    findings = len((d.answer[0].get("key_findings") or [])) if d.answer else 0
    # reader-meaningful trust signals (#21): sources we read, where claims disagree, what was
    # rejected on review — not span/claim/gate telemetry.
    sources = len(d.documents)
    # disputed claims = distinct claims with cross-source counter-evidence (the counter-search target)
    disagreements = len({e["to_id"] for e in d.claim_edges if e.get("type") in ("refutes", "qualifies")})
    rejected = sum(1 for c in d.claims if c.get("status") == "rejected"
                   and any(isinstance(x, str) and x.startswith(("rejected on review", "qualified by"))
                           for x in (c.get("limitations") or [])))
    gp = sum(1 for g in d.gate_results if g.get("status") == "pass")
    gw = sum(1 for g in d.gate_results if g.get("status") == "warning")
    gf = sum(1 for g in d.gate_results if g.get("status") in ("hard_fail", "repairable_fail"))
    # M4: source classes that contributed no evidence — the run is a *partial* brief, say so.
    incomplete = next((g.get("metrics", {}).get("failed_classes", []) for g in d.gate_results
                       if g.get("gate_id") == "SourceClassCoverageGate"), [])
    return {
        "heading": "Phase 0 Evidence Report" if d.approved else "Phase 0 Diagnostic Report",
        "topic": d.run.get("topic", ""),
        "run_id": d.run.get("run_id", ""),
        "generated": d.run.get("completed_at") or d.run.get("created_at") or "",
        "mode": d.contract.get("domain_pack_id") or "generic",
        "incomplete_classes": incomplete,
        "gate_status": "BLOCKED" if not d.approved else ("WARN" if gw else "PASS"),
        "metrics": {
            "documents": len(d.documents), "spans": len(d.spans), "evidence": len(d.evidence),
            "findings": findings, "claims": ledger, "claims_accepted": accepted,
            "sources": sources, "disagreements": disagreements, "rejected": rejected,
            "citations": len(citations), "citations_youtube": yt, "citations_github": gh,
            "gates_pass": gp, "gates_warn": gw, "gates_fail": gf, "gates_total": len(d.gate_results),
        },
    }


# Self-contained dossier styling: one indigo accent, semantic gate colours, an editorial
# serif-heading / sans-body pairing, zebra tables, sticky section nav, dark-mode + print
# variants. No external assets, fonts, or JS — the report stays a single portable file.
_THEME_JS = (
    "(function(){var r=document.documentElement;"
    "function s(t){r.setAttribute('data-theme',t);}"
    "try{if(matchMedia('(prefers-color-scheme:dark)').matches)s('dark');}catch(e){}"
    "window.toggleDark=function(){s(r.getAttribute('data-theme')==='dark'?'light':'dark');};})();"
)

# Editorial brief: a reading-first 3-column layout (contents · ~720px prose column · trust rail),
# one restrained accent (no gradient), hairline section rules (not cards), system+serif type, and
# a real dark toggle. The trust rail shows reader-meaningful signals — sources / findings / traced /
# disagreements / pass+advisories — never raw dev telemetry (#21).
_CSS = """
*{box-sizing:border-box}
:root{--bg:#fbfbfa;--surface:#fff;--ink:#1f232a;--muted:#5b6471;--faint:#8a929e;
--line:#ecedf1;--border:#e3e6ea;--accent:#2f5bd0;--accent-soft:#eef2fc;
--good:#0f7b4f;--warn:#9a6b00;--red:#b3261e;
--serif:Georgia,"Iowan Old Style","Times New Roman",serif;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
html[data-theme=dark]{--bg:#0d0f13;--surface:#15181e;--ink:#e7eaf0;--muted:#9aa2ad;--faint:#6b7480;
--line:#1d212a;--border:#262b34;--accent:#8ea2ff;--accent-soft:#171b2c;
--good:#54c08a;--warn:#caa64a;--red:#ff7b73}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
font-size:17px;line-height:1.66;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.topbar{position:sticky;top:0;z-index:40;height:50px;display:flex;align-items:center;
padding:0 22px;border-bottom:1px solid var(--border);background:var(--bg)}
.topbar .brand{font-size:13px;font-weight:650;letter-spacing:.02em;color:var(--muted)}
.topbar .sp{flex:1}
.topbar .act{cursor:pointer;background:transparent;border:1px solid var(--border);color:var(--muted);
border-radius:8px;padding:5px 11px;font-size:12.5px;font-family:var(--sans)}
.topbar .act:hover{color:var(--ink)}
.app{max-width:1180px;margin:0 auto;display:grid;
grid-template-columns:200px minmax(0,1fr) 232px;gap:38px;padding:0 24px}
.col-toc,.col-rail{position:sticky;top:50px;align-self:start;max-height:calc(100vh - 50px);
overflow-y:auto;padding:32px 0 60px}
.col-main{min-width:0;padding:36px 0 90px}
.toc-h,.rail-h{font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;
color:var(--faint);margin:0 0 12px}
#toc{display:flex;flex-direction:column;gap:1px}
#toc a{font-size:13px;color:var(--muted);padding:5px 9px;border-radius:6px;
border-left:2px solid transparent;line-height:1.35}
#toc a:hover{background:var(--accent-soft);color:var(--ink)}
.hero{max-width:720px;margin:0 0 4px}
.kicker{font-size:11.5px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:var(--accent)}
.hero h1{font-family:var(--serif);font-size:32px;line-height:1.18;letter-spacing:-.01em;
font-weight:600;margin:.25em 0 .25em}
.dateline{font-size:12.5px;color:var(--faint);font-family:var(--mono);margin:0}
.col-main section{max-width:720px}
.col-main section+section{margin-top:32px;padding-top:28px;border-top:1px solid var(--line)}
.col-main h2{font-family:var(--serif);font-size:22px;font-weight:600;letter-spacing:-.01em;
margin:0 0 .5em;scroll-margin-top:62px}
.col-main h3{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
color:var(--muted);margin:1.4em 0 .5em}
p{margin:0 0 1em}
.col-main section:first-of-type>p:first-of-type{font-size:19px;line-height:1.6}
ul,ol{padding-left:22px}li{margin:7px 0}
blockquote{margin:14px 0;padding:2px 0 2px 16px;border-left:3px solid var(--border);
color:var(--muted);font-style:italic}
code{background:var(--accent-soft);border-radius:5px;padding:.08em .36em;font-size:85%;font-family:var(--mono)}
.tablewrap{overflow-x:auto;border:1px solid var(--border);border-radius:10px;margin:1.1em 0}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{border-bottom:1px solid var(--line);padding:9px 12px;text-align:left;vertical-align:top}
th{background:var(--accent-soft);font-weight:650;font-size:11.5px;text-transform:uppercase;
letter-spacing:.03em;color:var(--muted)}
tbody tr:last-child td{border-bottom:0}
main a[href*="youtube.com"],main a[href*="github.com"]{font-weight:600}
.trust{display:flex;flex-direction:column;gap:13px}
.trust .t-num{font-family:var(--serif);font-size:23px;font-weight:600;line-height:1}
.trust .t-lbl{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-top:3px}
.trust .t-note{font-size:12.5px;line-height:1.45;color:var(--muted)}
.trust .ok{color:var(--good)}.trust .flag{color:var(--warn)}
.trust .rule{height:1px;background:var(--line);margin:1px 0}
.report-footer{max-width:1180px;margin:0 auto;padding:20px 24px 60px;color:var(--faint);
font-size:12px;font-family:var(--mono);border-top:1px solid var(--border)}
@media (max-width:980px){.app{grid-template-columns:1fr;gap:0}
.col-toc,.col-rail{position:static;max-height:none;padding:14px 0;border-bottom:1px solid var(--line)}
.col-rail{order:-1}.col-main section,.hero{max-width:none}}
@media print{.topbar,.col-toc,.col-rail{display:none}.app{display:block}.col-main section{break-inside:avoid}}
"""


def render_html(markdown: str, title: str, view_model: dict | None = None) -> str:
    """Render the Markdown report to a self-contained, styled HTML dossier (embedded CSS,
    no external assets/JS). With a view model it adds a header, gate badge, metric band, and
    sticky section nav; without one it falls back to a minimal body render. Citation anchors
    keep the exact `<a href="URL">LABEL</a>` shape — deep-link colour-coding is pure CSS."""
    body, sections = _convert_body(markdown.split("\n"), drop_first_h1=bool(view_model))
    if not view_model:
        return ("<!doctype html><html><head><meta charset='utf-8'>"
                f"<title>{_html.escape(title)}</title></head><body>{body}</body></html>")
    head = ("<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_html.escape(title)}</title><style>{_CSS}</style>")
    return ("<!doctype html><html lang='en'><head>" + head + "</head><body>"
            "<div class='topbar'><span class='brand'>AI4Research · Deep Research</span>"
            "<span class='sp'></span>"
            "<button class='act' type='button' onclick='toggleDark()'>◐ Theme</button></div>"
            "<div class='app'>"
            + "<aside class='col-toc'>" + _nav_html(sections) + "</aside>"
            + "<main class='col-main'>" + _header_html(view_model) + body + "</main>"
            + "<aside class='col-rail'>" + _metric_band_html(view_model) + "</aside>"
            + "</div>" + _footer_html(view_model)
            + "<script>" + _THEME_JS + "</script></body></html>")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def _convert_body(lines: list[str], drop_first_h1: bool) -> tuple[str, list[tuple[str, str]]]:
    """Markdown->HTML for headings, tables, lists, quotes. `## ` headings open styled
    `<section>`s (collected for the nav); the leading `# ` title is dropped when the header
    renders it instead."""
    out: list[str] = []
    sections: list[tuple[str, str]] = []
    open_section = False
    dropped = not drop_first_h1
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("# ") and not dropped:
            dropped = True
            i += 1
            continue
        if line.startswith("### "):
            out.append(f"<h3>{_html.escape(line[4:])}</h3>")
        elif line.startswith("## "):
            text = line[3:]
            slug = _slug(text)
            if open_section:
                out.append("</section>")
            out.append(f"<section id='{slug}' class='report-section'><h2>{_html.escape(text)}</h2>")
            sections.append((slug, text))
            open_section = True
        elif line.startswith("# "):
            out.append(f"<h1>{_html.escape(line[2:])}</h1>")
        elif line.startswith("> "):
            out.append(f"<blockquote>{_inline(line[2:])}</blockquote>")
        elif line.startswith("|"):
            table, j = [], i
            while j < len(lines) and lines[j].startswith("|"):
                table.append(lines[j])
                j += 1
            out.append(_html_table(table))
            i = j
            continue
        elif line.strip().startswith("- ") or line.startswith("  - "):
            items, j = [], i
            while j < len(lines) and (lines[j].strip().startswith("- ")):
                items.append(lines[j].strip()[2:])
                j += 1
            out.append("<ul>" + "".join(f"<li>{_inline(it)}</li>" for it in items) + "</ul>")
            i = j
            continue
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
        i += 1
    if open_section:
        out.append("</section>")
    return "".join(out), sections


def _header_html(vm: dict) -> str:
    """The reading column's hero: a kicker, the topic as the title, and a quiet dateline. The
    gate status lives in the trust rail (reader-meaningful), not a badge here."""
    if vm.get("gate_status") == "BLOCKED":
        kicker = "Diagnostic"
    elif vm.get("incomplete_classes"):
        kicker = "Partial brief"
    else:
        kicker = "Research Brief"
    dateline = [_html.escape(vm["generated"])] if vm.get("generated") else []
    dateline.append(f"run {_html.escape(vm['run_id'])}")
    return ("<div class='hero'>"
            f"<div class='kicker'>{kicker}</div>"
            f"<h1>{_html.escape(vm.get('topic') or vm['heading'])}</h1>"
            f"<p class='dateline'>{' · '.join(dateline)}</p></div>")


def _metric_band_html(vm: dict) -> str:
    """The trust rail — what a reader needs to trust the report: how many sources, how many
    findings, whether every claim is traced, where sources disagree, and a plain quality line.
    Never raw dev telemetry (no span/claim/gate counts) — that is the #21 principle."""
    m = vm["metrics"]
    out = ["<div class='rail-h'>At a glance</div><div class='trust'>"]

    def stat(num, lbl):
        return (f"<div><div class='t-num'>{_html.escape(str(num))}</div>"
                f"<div class='t-lbl'>{_html.escape(lbl)}</div></div>")

    out.append(stat(m.get("sources", m.get("documents", 0)), "sources"))
    for cls in (vm.get("incomplete_classes") or []):     # M4: a whole source class contributed nothing
        out.append(f"<div class='t-note flag'>⚠ incomplete — no {_html.escape(cls)} evidence</div>")
    if m["findings"]:                       # synthesized headline findings (LLM run)
        out.append(stat(m["findings"], "key finding" if m["findings"] == 1 else "key findings"))
    else:                                   # deterministic run: the evidence-backed claim ledger
        out.append(stat(m.get("claims", 0), "claim" if m.get("claims") == 1 else "claims"))
    out.append("<div class='rule'></div>")
    if m.get("citations"):
        out.append("<div class='t-note ok'>✓ every claim traced to evidence</div>")
    dis, rej = m.get("disagreements", 0), m.get("rejected", 0)
    if dis:
        out.append(f"<div class='t-note flag'>⚠ {dis} point{'s' if dis != 1 else ''} of disagreement</div>")
    if rej:
        out.append(f"<div class='t-note flag'>⚠ {rej} claim{'s' if rej != 1 else ''} rejected on review</div>")
    out.append("<div class='rule'></div>")
    blocked = m.get("gates_fail")
    adv = m.get("gates_warn", 0)
    note = "all checks clear" if not adv else (f"{adv} advisory" if adv == 1 else f"{adv} advisories")
    out.append(f"<div class='t-note {'flag' if blocked else 'ok'}'>"
               f"{'✗ Blocked' if blocked else '✓ Passed'} · {note}</div>")
    out.append("</div>")
    return "".join(out)


def _nav_html(sections: list[tuple[str, str]]) -> str:
    if not sections:
        return ""
    links = "".join(f"<a href='#{s}'>{_html.escape(t)}</a>" for s, t in sections)
    return f"<div class='toc-h'>Contents</div><nav id='toc'>{links}</nav>"


def _footer_html(vm: dict) -> str:
    bits = [_html.escape(vm["generated"])] if vm.get("generated") else []
    bits.append(f"run {_html.escape(vm['run_id'])}")
    bits.append("AI4Research · deterministic research compiler (Phase 0)")
    return f"<footer class='report-footer'>{' · '.join(bits)}</footer>"


def _html_table(rows: list[str]) -> str:
    def cells(r):
        return [c.strip() for c in r.strip().strip("|").split("|")]
    if len(rows) < 2:
        return ""
    header = cells(rows[0])
    body = [cells(r) for r in rows[2:]]  # row 1 is the markdown separator
    thead = "<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in header) + "</tr>"
    tbody = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
    return f"<div class='tablewrap'><table>{thead}{tbody}</table></div>"


def _inline(s: str) -> str:
    s = _html.escape(s)
    s = s.replace("**", "")  # drop bold markers; minimal renderer
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _safe_anchor, s)
    return s.replace("`", "")


def _safe_anchor(match: "re.Match") -> str:
    """Render a markdown link only for safe schemes — http/https or relative. Source text
    (READMEs, transcripts) is untrusted, so a `[x](javascript:…)` / `data:` link must not become
    an active anchor in the HTML report (XSS); drop the link, keep the label."""
    label, url = match.group(1), match.group(2)
    low = url.strip().lower()
    if low.startswith(("http://", "https://")) or not re.match(r"[a-z][a-z0-9+.-]*:", low):
        return f'<a href="{url}">{label}</a>'
    return label
