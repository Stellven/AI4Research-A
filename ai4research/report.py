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
        self.citations = rows("citations")
        self.figures = rows("figures")
        self.answer = rows("answer")
        self.approved = bool(self.dossier.get("approved_for_report_rendering"))

        self.by_item = {i["selected_item_id"]: i for i in self.items}
        self.by_doc = {d["document_id"]: d for d in self.documents}
        self.by_span = {s["span_id"]: s for s in self.spans}
        self.by_evidence = {e["evidence_id"]: e for e in self.evidence}
        self.by_claim = {c["claim_id"]: c for c in self.claims}
        self.cite_by_evidence = {c["evidence_id"]: c for c in self.citations}


def _summary(d: ReportData) -> list[str]:
    acquired = sum(1 for a in d.attempts if a["status"] == "succeeded")
    gaps = sum(1 for a in d.attempts if a["status"] != "succeeded")
    status = d.dossier.get("overall_status", "unknown")
    return [
        "## Executive Run Summary", "",
        f"Run `{d.run.get('run_id')}` processed {len(d.items)} supplied item(s) "
        f"({acquired} acquired, {gaps} gap(s)) across {len(d.containers)} container(s). "
        f"Gate status: **{status}**. No live retrieval (Phase 0).", "",
        f"- Topic: {d.run.get('topic')}",
        f"- Documents: {len(d.documents)} | Spans: {len(d.spans)} | "
        f"Evidence: {len(d.evidence)} | Claims: {len(d.claims)} | Citations: {len(d.citations)}", "",
    ]


def _answer_section(d: ReportData) -> list[str]:
    """The synthesized findings brief (Increment 5) — present only when an LLM run produced a
    grounded answer: an executive summary plus a ranked list of the most material findings, each
    with its cited evidence resolved to deep links."""
    if not d.answer:
        return []
    a = d.answer[0]
    out = ["## Key Findings", ""]
    summary = (a.get("executive_summary") or "").strip()
    if summary:
        out += [summary, ""]
    for item in a.get("key_findings", []):
        text = (item.get("finding") or "").strip()
        if not text:
            continue
        cites = []
        for eid in item.get("evidence_ids", []):
            cite = d.cite_by_evidence.get(eid)
            if cite:
                cites.append(f"[{cite['label']}]({cite['url']})" if cite.get("url") else f"[{cite['label']}]")
        out.append(f"- {text}" + ((" " + " ".join(cites)) if cites else ""))
    out.append("")
    return out


def _coverage(d: ReportData) -> list[str]:
    out = ["## Source And Acquisition Coverage", "",
           "| Container | Type | Item | Acquisition |", "| --- | --- | --- | --- |"]
    status_by_item = {a["selected_item_id"]: a for a in d.attempts}
    for c in d.containers:
        items = [i for i in d.items if i["container_id"] == c["container_id"]]
        label = c.get("label") or c["container_id"]
        if not items:
            out.append(f"| {label} | {c['source_pack_type']} | _(no items — coverage gap)_ | — |")
        for i in items:
            att = status_by_item.get(i["selected_item_id"], {})
            verdict = att.get("status", "pending")
            if att.get("failure_code"):
                verdict += f" ({att['failure_code']})"
            out.append(f"| {label} | {c['source_pack_type']} | {i.get('title') or i['selected_item_id']} | {verdict} |")
    out.append("")
    return out


def _findings(d: ReportData) -> list[str]:
    out = ["## Evidence-Backed Findings", ""]
    accepted = [c for c in d.claims if c["status"] == "accepted"]
    if not accepted:
        out += ["_No accepted claims for this run._", ""]
        return out
    support = {}
    for ce in d.claim_evidence:
        support.setdefault(ce["claim_id"], []).append(ce["evidence_id"])
    for c in accepted:
        labels = []
        for eid in support.get(c["claim_id"], []):
            cite = d.cite_by_evidence.get(eid)
            if cite:
                if cite.get("url"):
                    labels.append(f"[{cite['label']}]({cite['url']})")
                else:
                    labels.append(f"[{cite['label']}]")
        suffix = (" " + " ".join(labels)) if labels else ""
        out.append(f"- {c['claim_text']}{suffix}")
    out.append("")
    return out


def _evidence_table(d: ReportData) -> list[str]:
    out = ["## Evidence Table", "",
           "| Citation | Item | Evidence | Excerpt |", "| --- | --- | --- | --- |"]
    for ev in d.evidence:
        cite = d.cite_by_evidence.get(ev["evidence_id"])
        label = cite["label"] if cite else "—"
        item = d.by_item.get(ev["selected_item_id"], {})
        excerpt = ev["quoted_text"].strip().replace("\n", " ")
        if len(excerpt) > 160:
            excerpt = excerpt[:157] + "..."
        out.append(f"| {label} | {item.get('title') or ev['selected_item_id']} "
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
        body = (_answer_section(d) + _summary(d) + _coverage(d) + _findings(d) + _figures(d)
                + _evidence_table(d) + _gaps(d) + _traceability(d) + _full_source_appendix(d))
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
    gp = sum(1 for g in d.gate_results if g.get("status") == "pass")
    gw = sum(1 for g in d.gate_results if g.get("status") == "warning")
    gf = sum(1 for g in d.gate_results if g.get("status") in ("hard_fail", "repairable_fail"))
    return {
        "heading": "Phase 0 Evidence Report" if d.approved else "Phase 0 Diagnostic Report",
        "topic": d.run.get("topic", ""),
        "run_id": d.run.get("run_id", ""),
        "generated": d.run.get("completed_at") or d.run.get("created_at") or "",
        "mode": d.contract.get("domain_pack_id") or "generic",
        "gate_status": "BLOCKED" if not d.approved else ("WARN" if gw else "PASS"),
        "metrics": {
            "documents": len(d.documents), "spans": len(d.spans), "evidence": len(d.evidence),
            "claims": len(d.claims), "claims_accepted": accepted, "citations": len(citations),
            "citations_youtube": yt, "citations_github": gh,
            "gates_pass": gp, "gates_warn": gw, "gates_fail": gf, "gates_total": len(d.gate_results),
        },
    }


# Self-contained dossier styling: one indigo accent, semantic gate colours, an editorial
# serif-heading / sans-body pairing, zebra tables, sticky section nav, dark-mode + print
# variants. No external assets, fonts, or JS — the report stays a single portable file.
_CSS = """
*{box-sizing:border-box}
:root{--bg:#fbfbfa;--surface:#fff;--ink:#1c1c1e;--muted:#6b6b76;--line:#e6e6ea;
--accent:#5a4be7;--accent2:#8b5cf6;--green:#1a7f4b;--green-bg:#e7f5ec;--amber:#8a5d00;
--amber-bg:#fdf3e0;--red:#b3261e;--red-bg:#fbe9e7;--yt:#c4302b;--gh:#5a4be7;
--serif:Georgia,"Times New Roman",serif;--sans:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);line-height:1.62;font-size:16px}
.topbar{height:6px;background:linear-gradient(90deg,var(--accent),var(--accent2))}
.report-header{max-width:920px;margin:0 auto;padding:30px 24px 6px}
.report-header h1{font-family:var(--serif);font-size:30px;line-height:1.2;margin:0 0 6px}
.report-header .topic{font-size:18px;color:var(--muted);margin:0 0 14px}
.report-header .meta{font-family:var(--mono);font-size:12.5px;color:var(--muted);display:flex;
gap:16px;flex-wrap:wrap;align-items:center}
.badge{padding:3px 11px;border-radius:999px;font-weight:600;font-size:12px;font-family:var(--sans)}
.badge.pass{background:var(--green-bg);color:var(--green)}
.badge.warn{background:var(--amber-bg);color:var(--amber)}
.badge.blocked{background:var(--red-bg);color:var(--red)}
.metric-band{max-width:920px;margin:18px auto;padding:0 24px;display:grid;
grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px}
.metric{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:13px 16px}
.metric .num{font-size:24px;font-weight:700;font-family:var(--serif)}
.metric .lbl{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-top:2px}
.metric .sub{font-size:11.5px;color:var(--muted);margin-top:5px}
.report-nav{position:sticky;top:0;z-index:5;background:rgba(251,251,250,.92);
backdrop-filter:blur(6px);border-bottom:1px solid var(--line)}
.report-nav ul{max-width:920px;margin:0 auto;padding:11px 24px;display:flex;gap:18px;
flex-wrap:wrap;list-style:none;font-size:13px}
.report-nav a{color:var(--muted);text-decoration:none}
.report-nav a:hover{color:var(--accent)}
.report-main{max-width:920px;margin:0 auto;padding:8px 24px 30px}
.report-section{background:var(--surface);border:1px solid var(--line);border-radius:14px;
padding:4px 22px 18px;margin:18px 0}
.report-section h2{font-family:var(--serif);font-size:21px;border-bottom:2px solid var(--line);
padding-bottom:8px;margin:18px 0 14px}
.report-section h3{font-size:15px;color:var(--accent);margin:18px 0 8px}
table{border-collapse:collapse;width:100%;font-size:13.5px;margin:10px 0;
border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
thead th{background:#f2f2f5;font-size:11.5px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
tbody tr:nth-child(even){background:#fafafb}
ul{padding-left:20px}li{margin:5px 0}
blockquote{margin:12px 0;padding:9px 14px;border-left:4px solid var(--amber);
background:var(--amber-bg);border-radius:0 8px 8px 0;color:#6a4e16}
a{color:var(--accent)}
main a[href*="youtube.com"]{color:var(--yt);text-decoration:none;font-weight:600}
main a[href*="youtube.com"]::before{content:"\\23F1  "}
main a[href*="github.com"]{color:var(--gh);text-decoration:none;font-weight:600}
main a[href*="github.com"]::before{content:"#\\2009";font-family:var(--mono)}
.report-footer{max-width:920px;margin:0 auto;padding:18px 24px 50px;color:var(--muted);
font-size:12px;font-family:var(--mono);border-top:1px solid var(--line)}
@media (max-width:640px){.report-header h1{font-size:24px}
.report-main,.report-header,.metric-band{padding-left:16px;padding-right:16px}}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--surface:#1f1f23;--ink:#e9e9ec;
--muted:#9a9aa6;--line:#2c2c33;--green-bg:#13301f;--amber-bg:#332811;--red-bg:#3a1714;
--yt:#ff7b73;--gh:#a899ff;--accent:#a899ff}
thead th{background:#26262c}tbody tr:nth-child(even){background:#1b1b1f}
blockquote{color:#d8c79a}}
@media print{.report-nav{display:none}.report-section{break-inside:avoid;border:none;padding:0}}
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
            "<div class='topbar'></div>"
            + _header_html(view_model) + _metric_band_html(view_model) + _nav_html(sections)
            + "<main class='report-main'>" + body + "</main>" + _footer_html(view_model)
            + "</body></html>")


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
    cls = {"PASS": "pass", "WARN": "warn", "BLOCKED": "blocked"}.get(vm["gate_status"], "warn")
    meta = [f"Run {_html.escape(vm['run_id'])}"]
    if vm.get("generated"):
        meta.append(f"Generated {_html.escape(vm['generated'])}")
    meta.append(f"Mode {_html.escape(vm['mode'])}")
    meta_html = "".join(f"<span>{m}</span>" for m in meta)
    return ("<header class='report-header'>"
            f"<h1>{_html.escape(vm['heading'])}</h1>"
            f"<p class='topic'>{_html.escape(vm['topic'])}</p>"
            f"<div class='meta'>{meta_html}<span class='badge {cls}'>{vm['gate_status']}</span></div></header>")


def _metric_band_html(vm: dict) -> str:
    m = vm["metrics"]
    cards = [
        (m["documents"], "Documents", ""),
        (m["spans"], "Spans", ""),
        (m["evidence"], "Evidence", ""),
        (m["claims_accepted"], "Claims", f"{m['claims_accepted']} of {m['claims']} accepted"),
        (m["citations"], "Deep Links", f"⏱ {m['citations_youtube']}  ·  # {m['citations_github']}"),
        (f"{m['gates_pass']}/{m['gates_total']}", "Gates Passed", f"{m['gates_warn']} warn · {m['gates_fail']} fail"),
    ]
    out = ["<section class='metric-band'>"]
    for num, lbl, sub in cards:
        sub_html = f"<div class='sub'>{_html.escape(str(sub))}</div>" if sub else ""
        out.append(f"<div class='metric'><div class='num'>{_html.escape(str(num))}</div>"
                   f"<div class='lbl'>{_html.escape(lbl)}</div>{sub_html}</div>")
    out.append("</section>")
    return "".join(out)


def _nav_html(sections: list[tuple[str, str]]) -> str:
    if not sections:
        return ""
    links = "".join(f"<li><a href='#{s}'>{_html.escape(t)}</a></li>" for s, t in sections)
    return f"<nav class='report-nav'><ul>{links}</ul></nav>"


def _footer_html(vm: dict) -> str:
    bits = [f"Run {_html.escape(vm['run_id'])}"]
    if vm.get("generated"):
        bits.append(_html.escape(vm["generated"]))
    bits.append("Phase 0 · deterministic research compiler")
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
    return f"<table>{thead}{tbody}</table>"


def _inline(s: str) -> str:
    s = _html.escape(s)
    s = s.replace("**", "")  # drop bold markers; minimal renderer
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    return s.replace("`", "")
