"""Optional LLM synthesis operators.

These are local and deterministic unless a live runtime is explicitly selected. They only
write draft proposals; gates decide acceptance later (LLMSynthesis -> EntailmentGate;
AnswerSynthesis -> a deterministic grounding pass that keeps only citations it can verify).
"""
from __future__ import annotations

import json
import re

from .. import ids
from ..model_runtime import ModelRuntime, ModelRuntimeError, get_runtime
from ..runtime import RunContext
from ..workfiles import WorkStore
from .base import Operator
from .extraction import _term_pattern   # reuse the deterministic word-boundary matcher for grounding

# Object-wrapped (codex --output-schema requires a top-level object, strict): a {claims: [...]}
# envelope. CodexRuntime returns the object; the stub may return a bare list of claim records —
# both are handled in run().
PROPOSAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claims"],
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim_text", "claim_type", "cited_evidence_ids"],
                "properties": {
                    "claim_text": {"type": "string"},
                    "claim_type": {"type": "string"},
                    "cited_evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


def _read_run_config(ctx: RunContext) -> dict:
    path = ctx.input_dir / "run_config.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# --- Increment 10: topic-derived ontology (the relational keystone) ------------------------

ONTOLOGY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entities"],
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "type", "synonyms"],
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "synonyms": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

ONTOLOGY_MAX_CLAIMS = 60   # accepted-claim sample handed to the model (bounded; size logged, no silent cap)


def _ontology_prompt(topic: str, claim_texts: list) -> str:
    lines = [
        "You are building the ONTOLOGY of a research topic: the recurring entities/concepts the "
        "sources actually argue about. From the topic and claim excerpts below, list the salient "
        "concepts the analysis turns on — the architectures, methods, models, mechanisms, metrics, "
        "and named systems that sources compare, support, or dispute. Prefer specific recurring "
        "nouns over generic words. For each, give a short canonical name, a type (Architecture, "
        "Method, Model, Mechanism, Metric, System, or Concept), and synonyms/abbreviations exactly "
        "as they appear in the text. Only list concepts that actually occur in the excerpts.",
        "",
        "Return JSON: entities [{name, type, synonyms}].",
        "",
        f"Topic: {topic}",
        "",
        "Claim excerpts:",
    ]
    lines += [f"- {t}" for t in claim_texts]
    return "\n".join(lines)


class OntologyDeriveOperator(Operator):
    """#10 the relational keystone: derive the TOPIC ontology (not a static pack vocabulary) so the
    claim graph has entities to cluster on. The deterministic EntityTagOperator tags claims against
    the pack's static vocabulary (the floor — empty/stale for arbitrary topics); this LLM pass
    proposes the topic's salient concepts from the accepted claims, code GROUNDS each (its name or a
    synonym must occur, word-boundary, in a real accepted claim — no hallucinated ontology), dedups
    case-insensitively against the existing entities, then appends `entities` + `claim_entities`
    links. Off unless a runtime is configured and the pack opts the operator in; the static floor
    stands without it. Populating the ontology is what lets ContradictionDetect anchor on contested
    cross-source concepts instead of narration fragments, and gives the report a relational spine."""

    NAME = "OntologyDeriveOperator"
    INPUT_SCHEMAS = ["claims", "entities", "claim_entities", "runs"]
    OUTPUT_SCHEMAS = ["entities", "claim_entities"]

    def __init__(self, runtime: ModelRuntime | None = None):
        self.runtime = runtime

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        contract = (work.read_rows("research_contracts") or [{}])[0]
        pack = next((p for p in work.read_rows("domain_packs") if p.get("pack_id") == contract.get("domain_pack_id")), {})
        if self.NAME not in (pack.get("llm_operators") or []):
            return {"enabled": 0, "reason": "not enabled for pack"}
        runtime_name = _read_run_config(ctx).get("model_runtime")
        if not runtime_name:
            return {"enabled": 0, "reason": "no model_runtime"}

        accepted = [c for c in work.read_rows("claims") if c.get("status") == "accepted"]
        if not accepted:
            return {"enabled": 1, "reason": "no accepted claims", "entities": 0, "links": 0}

        runtime = self.runtime or get_runtime(runtime_name)
        runtime_name = getattr(runtime, "name", runtime_name)
        topic = (work.read_rows("runs") or [{}])[0].get("topic", "")
        sample = [(c.get("claim_text") or "").strip().replace("\n", " ")[:240] for c in accepted[:ONTOLOGY_MAX_CLAIMS]]
        try:
            records = runtime.propose(_ontology_prompt(topic, sample), ONTOLOGY_SCHEMA)
        except ModelRuntimeError as exc:
            return {"enabled": 1, "runtime": runtime_name, "runtime_status": "failed", "error": str(exc)}
        proposal = records[0] if records and isinstance(records[0], dict) else {}

        existing_entities = work.read_rows("entities")
        existing_link_rows = work.read_rows("claim_entities")
        existing_links = {(l["claim_id"], l["entity_id"]) for l in existing_link_rows}
        claim_text_lower = {c["claim_id"]: (c.get("claim_text") or "").lower() for c in accepted}
        seen_names = {e.get("canonical_name", "").lower() for e in existing_entities}

        new_entities: list = []
        new_links: list = []
        grounded = dropped = 0
        for item in (proposal.get("entities") or []):
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip()
            if not name or name.lower() in seen_names:
                continue
            synonyms = [s.strip() for s in (item.get("synonyms") or []) if isinstance(s, str) and s.strip()]
            patterns = [_term_pattern(t) for t in [name] + synonyms]
            matched = [cid for cid, tl in claim_text_lower.items() if any(p.search(tl) for p in patterns)]
            if not matched:                          # ungrounded: not part of THIS topic's ontology
                dropped += 1
                continue
            seen_names.add(name.lower())
            ent_id = ids.mint(ctx.run_id, "ENT", len(existing_entities) + len(new_entities))
            new_entities.append({
                "entity_id": ent_id, "run_id": ctx.run_id, "canonical_name": name,
                "entity_type": (item.get("type") or "Concept").strip() or "Concept",
                "synonyms": synonyms, "domain_tags": ["llm_derived"],
            })
            grounded += 1
            for cid in matched:
                if (cid, ent_id) not in existing_links:
                    new_links.append({"claim_id": cid, "entity_id": ent_id})

        if new_entities:
            work.write_rows("entities", existing_entities + new_entities)
        if new_links:
            work.write_rows("claim_entities", existing_link_rows + new_links)
        return {"enabled": 1, "runtime": runtime_name, "entities": grounded, "dropped": dropped,
                "links": len(new_links), "claim_sample": len(sample)}


class LLMSynthesisOperator(Operator):
    NAME = "LLMSynthesisOperator"
    INPUT_SCHEMAS = ["claims", "evidence"]
    OUTPUT_SCHEMAS = ["claims", "claim_evidence"]

    def __init__(self, runtime: ModelRuntime | None = None):
        self.runtime = runtime

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        contract = (work.read_rows("research_contracts") or [{}])[0]
        pack = next((p for p in work.read_rows("domain_packs") if p.get("pack_id") == contract.get("domain_pack_id")), {})
        # Unified enablement: an LLM stage fires iff a runtime is configured AND the pack opts the
        # operator in. No 'stub' fallback — "off without --model-runtime" is literally true.
        if self.NAME not in (pack.get("llm_operators") or []):
            return {"enabled": 0, "proposed": 0, "dropped": 0}
        run_config = _read_run_config(ctx)
        runtime_name = run_config.get("model_runtime")
        if not runtime_name:
            return {"enabled": 0, "reason": "no model_runtime", "proposed": 0, "dropped": 0}
        runtime = self.runtime or get_runtime(runtime_name)
        runtime_name = getattr(runtime, "name", runtime_name)
        evidence = {e["evidence_id"]: e for e in work.read_rows("evidence")}
        prompt = _prompt(work.read_rows("claims"), evidence)
        try:
            records = runtime.propose(prompt, PROPOSAL_SCHEMA)
        except ModelRuntimeError as exc:
            return {"enabled": 1, "runtime": runtime_name, "runtime_status": "failed", "error": str(exc),
                    "proposed": 0, "dropped": 0}

        # codex returns the {claims: [...]} envelope; a stub may return a bare list of records.
        proposals = records[0]["claims"] if (len(records) == 1 and isinstance(records[0].get("claims"), list)) else records

        claims = work.read_rows("claims")
        links = work.read_rows("claim_evidence")
        proposed = dropped = 0
        for record in proposals:
            cited = [eid for eid in record.get("cited_evidence_ids", []) if isinstance(eid, str)]
            if not cited or any(eid not in evidence for eid in cited):
                dropped += 1
                continue
            claim_type = record.get("claim_type") if record.get("claim_type") in ("trend_claim", "comparison_claim") else "trend_claim"
            claim_id = ids.mint(ctx.run_id, "CLAIM", len(claims))
            claims.append({
                "claim_id": claim_id,
                "run_id": ctx.run_id,
                "claim_type": claim_type,
                "claim_kind": "synthesized",
                "claim_text": record["claim_text"],
                "claim_scope": "within provided source set",
                "criticality": "normal",
                "status": "draft",
                "confidence": "proposed_by_model",
                "limitations": [],
                "proposed_by": "llm_synthesis",
                "derivation": {
                    "method": "llm_synthesis",
                    "model": runtime_name,
                    "inputs": [{"evidence_id": eid} for eid in cited],
                },
            })
            for eid in cited:
                links.append({"claim_id": claim_id, "evidence_id": eid, "role": "supporting"})
            proposed += 1

        work.write_rows("claims", claims)
        work.write_rows("claim_evidence", links)
        return {"enabled": 1, "runtime": runtime_name, "runtime_status": "ok",
                "proposed": proposed, "dropped": dropped}


def _prompt(claims: list[dict], evidence: dict[str, dict]) -> str:
    lines = [
        "Propose up to three cross-source synthesized claims as JSON only.",
        "Each record must be {\"claim_text\": str, \"claim_type\": \"trend_claim\"|\"comparison_claim\", "
        "\"cited_evidence_ids\": [str]}.",
        "Use only the listed evidence IDs.",
        "",
        "Evidence:",
    ]
    for claim in claims:
        if claim.get("claim_kind") != "extractive" or claim.get("status") != "accepted":
            continue
        lines.append(f"- Claim: {claim.get('claim_text')}")
    for ev in evidence.values():
        lines.append(f"- {ev['evidence_id']}: {ev.get('summary')}")
    return "\n".join(lines)


# --- Increment 5: render-phase synthesis of the report dossier ---------------------------

# codex's --output-schema requires a top-level object (strict mode: every property required,
# additionalProperties:false at every level). The dossier = a long synthesized summary, an
# at-a-glance findings list (each naming the accepted claim_ids it draws on + their evidence ids),
# thematic angle sections, a forward outlook, caveats, and open questions. The synthesis is built
# over the gated claim graph; evidence is cited inline by id in the prose.
ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "key_findings", "sections", "outlook", "caveats", "open_questions"],
    "properties": {
        "summary": {"type": "string"},
        "key_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["finding", "claim_ids", "evidence_ids"],
                "properties": {
                    "finding": {"type": "string"},
                    "claim_ids": {"type": "array", "items": {"type": "string"}},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "body"],
                "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
            },
        },
        "outlook": {"type": "array", "items": {"type": "string"}},
        "caveats": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
}

_EVIDENCE_REF = re.compile(r"\[([\w.:-]+)\]")


def _ground_prose(text: str, valid: set) -> tuple[str, int, int]:
    """Keep inline [evidence_id] refs the model could ground; strip any it invented. Returns
    (clean_text, kept, dropped). Valid refs stay in the text for the renderer to deep-link."""
    kept = dropped = 0

    def repl(match):
        nonlocal kept, dropped
        if match.group(1) in valid:
            kept += 1
            return match.group(0)
        dropped += 1
        return ""

    cleaned = _EVIDENCE_REF.sub(repl, text or "")
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip(), kept, dropped


_STAR_FLOOR = 1000   # GitHub repos under this read as prototype / self-authored README (low rigor)


def _confidence(evidence_ids: list, ev_to_container: dict, ev_to_quality: dict | None = None) -> str:
    """Code-computed confidence (not a model self-rating). Base level from evidence density + source
    diversity; then capped by source QUALITY (M5/M6) — a finding resting only on low-rigor sources
    (prototype / self-authored low-star repos) is never 'high', because diversity of *weak* sources
    is not strength."""
    containers = {ev_to_container.get(e) for e in evidence_ids if ev_to_container.get(e)}
    base = "high" if len(containers) >= 2 else ("medium" if len(evidence_ids) >= 2 else "low")
    if ev_to_quality and evidence_ids and all(ev_to_quality.get(e, "low") == "low" for e in evidence_ids):
        return "medium" if len(evidence_ids) >= 2 else "low"   # cap: never high on all-low-rigor evidence
    return base


class AnswerSynthesisOperator(Operator):
    """Render-phase synthesis: with all claims built and gated, an LLM acts as a senior analyst
    and writes the report dossier as a *view over the accepted claim graph* — it is fed the
    accepted claims (their claim_text + backing evidence), not raw evidence, and writes a long
    synthesized summary, an at-a-glance findings list, thematic angle sections, a forward outlook,
    caveats, and open questions. Each finding names the accepted claim_ids it draws on; a
    deterministic pass keeps only citations the model could ground (dropping invented refs and
    findings that rest on no accepted claim), bounds a finding's evidence to the evidence of the
    claims it cites, and computes per-finding confidence from source diversity; it records an
    AnswerGroundingGate row. Because the input is the gated claim graph, a rejected/qualified claim
    (e.g. from contradiction review, #22) never enters synthesis — it drops a finding at the
    source. Off unless a live runtime is selected. The model proposes the prose; code validates the
    citations against the claim graph and calibrates confidence."""

    NAME = "AnswerSynthesisOperator"
    INPUT_SCHEMAS = ["claims", "evidence", "claim_evidence", "claim_edges", "question_graph_nodes",
                     "quality_dossier", "runs", "selected_source_items"]
    OUTPUT_SCHEMAS = ["answer", "gate_results"]

    def __init__(self, runtime: ModelRuntime | None = None):
        self.runtime = runtime

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        dossier = (work.read_rows("quality_dossier") or [{}])[0]
        if not dossier.get("approved_for_report_rendering"):
            return {"enabled": 0, "reason": "report not approved"}
        # Same unified predicate as LLMSynthesis: fire iff a runtime is configured AND the pack opts
        # this operator into llm_operators.
        contract = (work.read_rows("research_contracts") or [{}])[0]
        pack = next((p for p in work.read_rows("domain_packs") if p.get("pack_id") == contract.get("domain_pack_id")), {})
        if self.NAME not in (pack.get("llm_operators") or []):
            return {"enabled": 0, "reason": "not enabled for pack"}
        if not _read_run_config(ctx).get("model_runtime"):
            return {"enabled": 0, "reason": "no model_runtime"}

        evidence = {e["evidence_id"]: e for e in work.read_rows("evidence")}
        support: dict = {}
        for ce in work.read_rows("claim_evidence"):
            support.setdefault(ce["claim_id"], []).append(ce["evidence_id"])

        # The dossier is a view over the gated claim graph: synthesize from ACCEPTED claims, not raw
        # evidence. Each accepted claim carries its claim_text (the substance) + the evidence backing
        # it. claim_ev maps each accepted claim to its present evidence; `used` is the citable
        # evidence catalog (the union of accepted claims' evidence). Rejected/qualified claims never
        # enter the input, so a #22 rejection removes a finding at its source.
        claim_ev: dict = {}
        accepted_claims: list = []
        used: dict = {}
        for claim in work.read_rows("claims"):
            if claim.get("status") != "accepted":
                continue
            eids = [e for e in support.get(claim["claim_id"], []) if e in evidence]
            claim_ev[claim["claim_id"]] = eids
            accepted_claims.append({"claim_id": claim["claim_id"], "claim_text": claim.get("claim_text", ""),
                                    "claim_kind": claim.get("claim_kind", "extractive"), "evidence_ids": eids})
            for eid in eids:
                used[eid] = evidence[eid]
        if not accepted_claims or not used:
            return {"enabled": 1, "reason": "no accepted claims"}
        items = work.read_rows("selected_source_items")
        item_by_id = {i["selected_item_id"]: i for i in items}
        ev_to_container = {eid: (item_by_id.get(ev.get("selected_item_id")) or {}).get("container_id")
                           for eid, ev in used.items()}
        # M5/M6: per-evidence source-quality tier (low-star/prototype GitHub README = low rigor).
        class_by_container = {c["container_id"]: c.get("source_pack_type")
                              for c in work.read_rows("source_containers")}

        def _ev_quality(ev: dict) -> str:
            item = item_by_id.get(ev.get("selected_item_id"), {})
            if class_by_container.get(item.get("container_id")) == "github_repo":
                try:                                       # coerce string stars; None/missing -> low
                    if float((item.get("provider_metadata") or {}).get("stars")) >= _STAR_FLOOR:
                        return "normal"
                except (TypeError, ValueError):
                    pass
                return "low"                               # prototype / self-authored / unknown-star repo
            return "normal"

        ev_to_quality = {eid: _ev_quality(ev) for eid, ev in used.items()}

        run_config = _read_run_config(ctx)
        runtime_name = run_config.get("model_runtime", "stub")
        runtime = self.runtime or get_runtime(runtime_name)
        runtime_name = getattr(runtime, "name", runtime_name)
        topic = (work.read_rows("runs") or [{}])[0].get("topic", "")
        sub_questions = [n["text"] for n in work.read_rows("question_graph_nodes")
                         if n.get("type") == "sub_question"]
        # #22 (5b): hand the synthesis the disagreements among the claims it will narrate, so it can
        # hedge ("…though sources disagree"); only refutes/qualifies edges between accepted claims.
        accepted_ids = {c["claim_id"] for c in accepted_claims}
        edges = [e for e in work.read_rows("claim_edges")
                 if e.get("type") in ("refutes", "qualifies")
                 and e.get("from_id") in accepted_ids and e.get("to_id") in accepted_ids]
        try:
            records = runtime.propose(_answer_prompt(topic, sub_questions, accepted_claims, edges), ANSWER_SCHEMA)
        except ModelRuntimeError as exc:
            return {"enabled": 1, "runtime": runtime_name, "runtime_status": "failed", "error": str(exc)}
        if not records:
            return {"enabled": 1, "runtime": runtime_name, "findings": 0}

        draft = records[0]
        valid = set(used)
        valid_claims = set(claim_ev)
        kept = dropped = 0

        summary, k, d = _ground_prose(str(draft.get("summary", "")), valid)
        kept += k
        dropped += d

        findings = []
        for item in (draft.get("key_findings") or []):
            text = str(item.get("finding", "")).strip()
            claim_ids = [c for c in (item.get("claim_ids") or []) if c in valid_claims]
            # a finding's citable evidence is bounded to the evidence of the accepted claims it
            # draws on — so it cannot reach evidence that only backs a rejected/other claim.
            allowed = {e for cid in claim_ids for e in claim_ev[cid]}
            cited = [e for e in (item.get("evidence_ids") or []) if isinstance(e, str)]
            grounded = [e for e in cited if e in allowed]
            dropped += len(cited) - len(grounded)
            # deep-link to the model's grounded cites, else fall back to the cited claims' evidence
            link_ev = grounded or sorted(allowed)
            if not text or not claim_ids or not link_ev:   # must rest on an accepted, evidenced claim
                continue                                   # (no bare, undeep-linkable bullets)
            kept += len(link_ev)                           # count what actually renders as a citation
            # confidence reflects the evidence the finding is attributed to (its claims' evidence),
            # not just which ids the model retyped — so a multi-source claim reads as high.
            findings.append({"finding": text, "claim_ids": claim_ids, "evidence_ids": link_ev,
                             "confidence": _confidence(link_ev, ev_to_container, ev_to_quality)})

        sections = []
        for sec in (draft.get("sections") or []):
            title = str(sec.get("title", "")).strip()
            body, k, d = _ground_prose(str(sec.get("body", "")), valid)
            kept += k
            dropped += d
            if title and k > 0:           # a section must carry at least one grounded citation
                sections.append({"title": title, "body": body})

        def _ground_items(items):
            nonlocal kept, dropped
            out = []
            for raw in items or []:
                text, k2, d2 = _ground_prose(str(raw), valid)
                kept += k2
                dropped += d2
                if text:
                    out.append(text)
            return out

        outlook = _ground_items(draft.get("outlook"))
        caveats = _ground_items(draft.get("caveats"))
        open_questions = [str(x).strip() for x in (draft.get("open_questions") or []) if str(x).strip()]

        if not findings and not sections:
            return {"enabled": 1, "runtime": runtime_name, "findings": 0, "reason": "ungrounded"}

        work.write_rows("answer", [{
            "run_id": ctx.run_id, "summary": summary, "key_findings": findings, "sections": sections,
            "outlook": outlook, "caveats": caveats, "open_questions": open_questions,
            "source": runtime_name, "sources_count": len(used),
            "citations_kept": kept, "citations_dropped": dropped,
        }])
        work.append_row("gate_results", {
            "gate_result_id": ids.mint(ctx.run_id, "ANSGATE", 0),
            "run_id": ctx.run_id, "gate_id": "AnswerGroundingGate", "gate_version": "0.1.0",
            "status": "warning" if dropped else "pass", "severity": "warning",
            "checked_tables": ["answer", "evidence"],
            "issues": ([f"stripped {dropped} ungrounded citation(s)"] if dropped else []),
            "metrics": {"findings": len(findings), "sections": len(sections),
                        "citations_kept": kept, "citations_dropped": dropped},
            "created_at": ids.utc_now_iso(),
        })
        # Reconcile the persisted dossier with the gate_results it now post-dates: the
        # AnswerGroundingGate row was appended after PreRenderQualityGateSuite computed warning_count,
        # so recompute from the full gate set (the report already recomputes live; the persisted
        # dossier must not disagree with its own gate rows). AnswerSynthesis only runs on an approved
        # run, so there are no blocking failures to flip overall_status to "fail".
        warnings = sum(1 for r in work.read_rows("gate_results") if r.get("status") == "warning")
        dossier["warning_count"] = warnings
        if not dossier.get("blocking_gate_failures"):
            dossier["overall_status"] = "warning" if warnings else "pass"
        work.write_rows("quality_dossier", [dossier])
        return {"enabled": 1, "runtime": runtime_name, "findings": len(findings),
                "sections": len(sections), "citations_kept": kept, "citations_dropped": dropped}


def _answer_prompt(topic: str, sub_questions: list[str], claims: list[dict],
                   edges: list[dict] | None = None) -> str:
    lines = [
        "You are a senior research analyst writing a deep-research dossier that answers the topic. "
        "You are given the ACCEPTED, GATED CLAIMS distilled from the sources — each with an id, a "
        "kind (extractive = a quoted finding from one source; synthesized/comparative = a "
        "cross-source conclusion), and the evidence ids backing it. Build the dossier FROM THESE "
        "CLAIMS. Cite evidence inline in square brackets by id, e.g. [EV0001], drawing the ids from "
        "the claims you use (cite more than one where apt). Connective sentences may be uncited, but "
        "never state a fact without a citation, and never invent ids.",
        "",
        f"Topic: {topic}",
        "",
        "Return JSON with these fields:",
        "- summary: THE CENTREPIECE and BY FAR THE LONGEST field — at least 6 full paragraphs, "
        "roughly 600-900 words, longer and richer than all the sections combined. Devote a paragraph "
        "to each major angle/perspective, then a synthesis paragraph on the throughline, where the "
        "claims converge AND where they conflict, and what it all means. Write a deep, well-cited "
        "analytical narrative — never a short abstract or a restatement of the findings list. Do not "
        "be terse; depth and multiple perspectives are the point.",
        "- key_findings: the 5-8 most material findings; each a tight sentence, with claim_ids (the "
        "accepted claims it draws on) and the evidence_ids of those claims.",
        "- sections: 3-5 thematic sections, each {title: an insightful named angle, body: one to three "
        "paragraphs of grounded narrative with inline [id] citations}. Organize around the angles below.",
        "- outlook: 3-5 forward-looking 'where this is heading' points (cite where the claims support it).",
        "- caveats: honest limitations — source bias, thin or single-source evidence, conflicting or "
        "unverified claims.",
        "- open_questions: genuinely unresolved questions the claims raise.",
        "",
        "Angles to cover where the claims support them (do not pad to fill one):",
    ]
    lines += [f"- {q}" for q in sub_questions]
    lines += ["", "Accepted claims (build from these; cite their evidence ids):"]
    order = {"synthesized": 0, "comparative": 1, "extractive": 2}   # lead with cross-source claims
    for c in sorted(claims, key=lambda c: order.get(c.get("claim_kind"), 3)):
        text = (c.get("claim_text") or "").strip().replace("\n", " ")
        if len(text) > 220:
            text = text[:217] + "..."
        ev = ", ".join(c.get("evidence_ids") or [])
        lines.append(f"- {c['claim_id']} [{c.get('claim_kind', 'extractive')}; evidence: {ev}]: {text}")
    if edges:
        short = {c["claim_id"]: (c.get("claim_text") or "")[:120] for c in claims}
        lines += ["", "Known disagreements among these claims (weave in as hedges where relevant — "
                  "do not drop the claim, note the tension):"]
        for e in edges:
            verb = "is contradicted by" if e["type"] == "refutes" else "is qualified by"
            lines.append(f"- \"{short.get(e['from_id'], e['from_id'])}\" {verb} "
                         f"\"{short.get(e['to_id'], e['to_id'])}\"")
    return "\n".join(lines)


# --- Increment 6/9: contradiction-aware claim graph via COUNTER-SEARCH (#22 -> north-star) ----

# The north stars frame contradiction as a per-claim COUNTER-SEARCH: for each headline claim, find
# EVIDENCE from a different source that contradicts/qualifies it (claim-anchored, not claim-pair —
# the pairing approach missed every cross-source dispute on real topics). codex strict object; the
# operator re-validates every item (the runtime does not recurse into array items).
CONTRADICTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["disputes", "rejections"],
    "properties": {
        "disputes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim_id", "evidence_id", "relation", "axis", "note"],
                "properties": {
                    "claim_id": {"type": "string"},
                    "evidence_id": {"type": "string"},
                    "relation": {"type": "string", "enum": ["contradicts", "qualifies"]},
                    "axis": {"type": "string"},   # the dimension of disagreement (S4 bake-off winner)
                    "note": {"type": "string"},
                },
            },
        },
        "rejections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim_id", "reason"],
                "properties": {"claim_id": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
    },
}

MAX_ANCHORS = 40   # headline claims the counter-search runs on (synthesized/comparative first)
MAX_POOL = 100     # source-balanced evidence sample to search for counter-evidence (wide coverage)


def _append_limitation(claim: dict, note: str) -> None:
    lims = claim.get("limitations")
    claim["limitations"] = (lims if isinstance(lims, list) else []) + [note]


def _mark_reviewed(claim: dict, source: str = "contradiction_review") -> None:
    # record the review additively — preserve the claim's original derivation.method (provenance)
    deriv = dict(claim["derivation"]) if isinstance(claim.get("derivation"), dict) else {}
    deriv["review"] = source
    claim["derivation"] = deriv


def _reject_claim(claim: dict, reason: str, source: str = "contradiction_review") -> None:
    """The single claim-rejection path (#22 rails), reused by ContradictionDetect and the critic
    (#14): status=rejected + the "rejected on review:" limitation marker the report filters on +
    an additive derivation review tag recording which reviewer rejected it."""
    claim["status"] = "rejected"
    _append_limitation(claim, "rejected on review: " + reason)
    _mark_reviewed(claim, source)


def _claim_container_map(work: WorkStore) -> dict:
    """claim_id -> the container its evidence came from (its source). For cross-source detection."""
    item_container = {i["selected_item_id"]: i.get("container_id") for i in work.read_rows("selected_source_items")}
    ev_container = {e["evidence_id"]: item_container.get(e.get("selected_item_id"))
                    for e in work.read_rows("evidence")}
    support: dict = {}
    for ce in work.read_rows("claim_evidence"):
        support.setdefault(ce["claim_id"], []).append(ce["evidence_id"])
    return {cid: next((ev_container.get(e) for e in eids if ev_container.get(e)), None)
            for cid, eids in support.items()}


def _round_robin(buckets: dict, cap: int) -> list:
    """Take from each bucket in turn so the capped result spans every key (source)."""
    out: list = []
    while len(out) < cap and any(buckets.values()):
        for k in list(buckets):
            if buckets[k] and len(out) < cap:
                out.append(buckets[k].pop(0))
    return out


def _contested_claims(work: WorkStore, claim_container: dict, accepted: set) -> set:
    """claim_ids touching a CONTESTED entity — one that appears in accepted claims from >=2 source
    containers. That is exactly where cross-source disagreement can live, so these make the strongest
    counter-search anchors (#10b). Empty when the ontology is empty (no runtime), so anchoring falls
    back to the kind+source ordering and nothing regresses."""
    ent_containers: dict = {}
    claim_ents: dict = {}
    for link in work.read_rows("claim_entities"):
        cid, eid = link.get("claim_id"), link.get("entity_id")
        if cid not in accepted:
            continue
        claim_ents.setdefault(cid, set()).add(eid)
        cont = claim_container.get(cid)
        if cont:
            ent_containers.setdefault(eid, set()).add(cont)
    contested_ents = {eid for eid, conts in ent_containers.items() if len(conts) >= 2}
    return {cid for cid, ents in claim_ents.items() if ents & contested_ents}


def _anchor_claims(claims: list, accepted: set, claim_container: dict, contested: set | None = None) -> list:
    """The headline claims worth disputing: claims on CONTESTED entities first (cross-source
    disagreement lives there), then synthesized/comparative, then a source-balanced sample of
    extractive — capped at MAX_ANCHORS. `contested` is empty without an ontology, so this degrades to
    the prior kind+source ordering."""
    contested = contested or set()
    kind_order = {"synthesized": 0, "comparative": 1, "extractive": 2}
    ranked = sorted((c for c in claims if c["claim_id"] in accepted),
                    key=lambda c: (0 if c["claim_id"] in contested else 1, kind_order.get(c.get("claim_kind"), 3)))
    buckets: dict = {}
    for c in ranked:
        buckets.setdefault(claim_container.get(c["claim_id"]), []).append(c)
    return _round_robin(buckets, MAX_ANCHORS)


def _evidence_pool(evidence_rows: list, ev_container: dict, accepted_ev: set,
                   contested_ev: set | None = None) -> list:
    """A source-balanced sample of accepted evidence to search for counter-evidence (cap MAX_POOL).
    Evidence backing a CONTESTED cross-source concept is sampled first (#10b) so the counter-search
    disputes with substantive material rather than bibliographic filler; an empty contested set
    degrades to the prior source-balanced sample."""
    contested_ev = contested_ev or set()
    buckets: dict = {}
    for e in evidence_rows:
        if e["evidence_id"] in accepted_ev:
            buckets.setdefault(ev_container.get(e["evidence_id"]), []).append(e)
    for bucket in buckets.values():
        bucket.sort(key=lambda e: 0 if e["evidence_id"] in contested_ev else 1)   # stable: contested first
    return _round_robin(buckets, MAX_POOL)


def _contradiction_search_prompt(anchors: list, pool: list, claim_container: dict,
                                 ev_container: dict, container_label: dict) -> str:
    lines = [
        "You are a research reviewer running a COUNTER-SEARCH for cross-source disagreement. Below are "
        "headline CLAIMS (each tagged with its source) and a pool of EVIDENCE from various sources. For "
        "each claim, find EVIDENCE FROM A DIFFERENT SOURCE that CONTRADICTS it (gives the opposite "
        "answer to the same question) or QUALIFIES it (limits/narrows it). Only pair a claim with "
        "evidence whose source differs. Use the ids exactly as given — never invent ids.",
        "",
        "Seek SUBSTANTIVE disagreement about the SAME concept — e.g. one source says a method is "
        "comparable or superior while another shows it is weaker or fails on a specific task. Do NOT "
        "treat the mere existence of a paper, repository, survey, or alternative method as a "
        "contradiction; a bibliographic 'X also exists' is not a dispute.",
        "",
        "Return JSON: disputes [{claim_id, evidence_id, relation: \"contradicts\"|\"qualifies\", note}] "
        "and rejections [{claim_id, reason}] (a claim to withdraw as unsupported — reject sparingly). "
        "Leave an array empty if nothing applies.",
        "",
        "Claims:",
    ]
    for c in anchors:
        src = container_label.get(claim_container.get(c["claim_id"]), "unknown source")
        text = (c.get("claim_text") or "").strip().replace("\n", " ")[:200]
        lines.append(f"- {c['claim_id']} [source: {src}]: {text}")
    lines += ["", "Evidence pool (cite a different source than the claim's):"]
    for e in pool:
        src = container_label.get(ev_container.get(e["evidence_id"]), "unknown source")
        summ = (e.get("summary") or e.get("quoted_text") or "").strip().replace("\n", " ")[:200]
        lines.append(f"- {e['evidence_id']} [source: {src}]: {summ}")
    return "\n".join(lines)


class ContradictionDetectOperator(Operator):
    """#22 via the north-star COUNTER-SEARCH: for each headline claim the LLM searches a
    source-balanced evidence pool for EVIDENCE FROM A DIFFERENT SOURCE that contradicts or qualifies
    it; code validates the ids + enforces cross-source, then writes an evidence -> claim
    `refutes`/`qualifies` edge (a `qualifies` also flips the claim to status=qualified). It also
    proposes reason-bearing rejections (the shared #14 path). Claim-anchored, not claim-pair — the
    pairing approach missed every real cross-source dispute on live topics. Off unless the unified
    predicate holds; a warning-level ContradictionReviewGate records rejections."""

    NAME = "ContradictionDetectOperator"
    INPUT_SCHEMAS = ["claims", "claim_edges", "claim_evidence", "claim_entities", "evidence", "selected_source_items"]
    OUTPUT_SCHEMAS = ["claims", "claim_edges", "gate_results"]

    def __init__(self, runtime: ModelRuntime | None = None):
        self.runtime = runtime

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        contract = (work.read_rows("research_contracts") or [{}])[0]
        pack = next((p for p in work.read_rows("domain_packs") if p.get("pack_id") == contract.get("domain_pack_id")), {})
        if self.NAME not in (pack.get("llm_operators") or []):
            return {"enabled": 0, "reason": "not enabled for pack"}
        runtime_name = _read_run_config(ctx).get("model_runtime")
        if not runtime_name:
            return {"enabled": 0, "reason": "no model_runtime"}

        claims = work.read_rows("claims")
        accepted = {c["claim_id"] for c in claims if c.get("status") == "accepted"}   # snapshot once
        if len(accepted) < 2:
            return {"enabled": 1, "reason": "too few accepted claims",
                    "disputes": 0, "qualifications": 0, "rejections": 0}

        runtime = self.runtime or get_runtime(runtime_name)
        runtime_name = getattr(runtime, "name", runtime_name)
        # Source maps: each claim's and each evidence's container (the counter-search is cross-source).
        items = work.read_rows("selected_source_items")
        item_container = {i["selected_item_id"]: i.get("container_id") for i in items}
        evidence_rows = work.read_rows("evidence")
        ev_container = {e["evidence_id"]: item_container.get(e.get("selected_item_id")) for e in evidence_rows}
        claim_container = _claim_container_map(work)
        container_label = {c["container_id"]: (c.get("label") or c["container_id"])
                           for c in work.read_rows("source_containers")}
        accepted_ev: set = set()                                   # the searchable pool: accepted-claim evidence
        for ce in work.read_rows("claim_evidence"):
            if ce.get("role", "supporting") == "supporting" and ce.get("claim_id") in accepted:
                accepted_ev.add(ce.get("evidence_id"))
        contested = _contested_claims(work, claim_container, accepted)   # #10b: ontology-driven anchoring
        contested_ev = {ce.get("evidence_id") for ce in work.read_rows("claim_evidence")
                        if ce.get("role", "supporting") == "supporting" and ce.get("claim_id") in contested}
        anchors = _anchor_claims(claims, accepted, claim_container, contested)
        pool = _evidence_pool(evidence_rows, ev_container, accepted_ev, contested_ev)
        try:
            records = runtime.propose(
                _contradiction_search_prompt(anchors, pool, claim_container, ev_container, container_label),
                CONTRADICTION_SCHEMA)
        except ModelRuntimeError as exc:
            return {"enabled": 1, "runtime": runtime_name, "runtime_status": "failed", "error": str(exc)}
        proposal = records[0] if records and isinstance(records[0], dict) else {}

        anchor_ids = {c["claim_id"] for c in anchors}
        pool_ids = {e["evidence_id"] for e in pool}
        existing = work.read_rows("claim_edges")
        seen = {(e.get("from_id"), e.get("to_id"), e.get("type")) for e in existing}
        new_edges: list = []
        disputes = qualifications = 0
        qualified: set = set()
        reject_reason: dict = {}

        # disputes: counter-evidence (from a DIFFERENT source) that contradicts/qualifies a claim,
        # written as an evidence -> claim refutes/qualifies edge. Per-item validation (runtime ignores
        # item shape); cross-source enforced by container(claim) != container(evidence).
        for item in (proposal.get("disputes") or []):
            if not isinstance(item, dict):
                continue
            cid, eid, rel = item.get("claim_id"), item.get("evidence_id"), item.get("relation")
            if not (isinstance(cid, str) and isinstance(eid, str) and isinstance(rel, str)):
                continue
            if rel not in ("contradicts", "qualifies") or cid not in anchor_ids or eid not in pool_ids:
                continue
            cc, ec = claim_container.get(cid), ev_container.get(eid)
            if not (cc and ec and cc != ec):                       # cross-source only — the whole point
                continue
            etype = "refutes" if rel == "contradicts" else "qualifies"
            key = (eid, cid, etype)
            if key in seen:
                continue
            seen.add(key)
            new_edges.append({"run_id": ctx.run_id, "from_id": eid, "to_id": cid, "type": etype})
            if etype == "refutes":
                disputes += 1
            else:
                qualifications += 1
                qualified.add(cid)

        # rejections — reason-gated, id-validated against the accepted snapshot (the shared #14 path)
        for item in (proposal.get("rejections") or []):
            if not isinstance(item, dict):
                continue
            cid, reason = item.get("claim_id"), item.get("reason", "")
            if isinstance(cid, str) and isinstance(reason, str) and reason.strip() and cid in accepted:
                reject_reason[cid] = reason.strip()

        # apply: reject beats qualify (a qualifies edge already written stays as structure)
        for c in claims:
            cid = c["claim_id"]
            if cid in reject_reason:
                _reject_claim(c, reject_reason[cid])
            elif cid in qualified:
                c["status"] = "qualified"
                _append_limitation(c, "qualified by counter-evidence on review")
                _mark_reviewed(c)
        rejections = len(reject_reason)

        if new_edges:
            work.write_rows("claim_edges", existing + new_edges)
        if reject_reason or qualified:
            work.write_rows("claims", claims)

        work.append_row("gate_results", {
            "gate_result_id": ids.mint(ctx.run_id, "CONTRAGATE", 0),
            "run_id": ctx.run_id, "gate_id": "ContradictionReviewGate", "gate_version": "0.1.0",
            "status": "warning" if rejections else "pass", "severity": "warning",
            "checked_tables": ["claims", "claim_edges", "evidence"],
            "issues": ([f"rejected {rejections} claim(s) on review"] if rejections else []),
            "metrics": {"disputes": disputes, "qualifications": qualifications, "rejections": rejections,
                        "anchors": len(anchors), "pool": len(pool), "contested": len(contested)},
            "created_at": ids.utc_now_iso(),
        })
        # reconcile the persisted dossier (it post-dates the gate suite; mirror #27 Step 4) — both
        # warning_count and overall_status, so the row is never internally contradictory.
        dossier_rows = work.read_rows("quality_dossier")
        if dossier_rows:
            dossier = dossier_rows[0]
            warnings = sum(1 for r in work.read_rows("gate_results") if r.get("status") == "warning")
            dossier["warning_count"] = warnings
            if not dossier.get("blocking_gate_failures"):
                dossier["overall_status"] = "warning" if warnings else "pass"
            work.write_rows("quality_dossier", [dossier])
        return {"enabled": 1, "runtime": runtime_name, "disputes": disputes,
                "qualifications": qualifications, "rejections": rejections}


# --- Increment 7: codex critic pass (#14) -------------------------------------------------

CRITIC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim_id", "supported", "reason"],
                "properties": {
                    "claim_id": {"type": "string"},
                    "supported": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}

CRITIC_MAX = 30   # bound the review set; capping is surfaced in the gate metrics (no silent cut)


def _critic_candidates(claims: list) -> tuple[list, bool]:
    """Accepted claims, synthesized/comparative first (most prone to overstatement), capped."""
    order = {"synthesized": 0, "comparative": 1, "extractive": 2}
    accepted = sorted((c for c in claims if c.get("status") == "accepted"),
                      key=lambda c: order.get(c.get("claim_kind"), 3))
    return accepted[:CRITIC_MAX], len(accepted) > CRITIC_MAX


def _critic_prompt(candidates: list, support: dict, evidence: dict, sample: int) -> str:
    lines = [
        f"You are a meticulous research critic (reviewer #{sample + 1}). For each claim below, decide "
        "whether its OWN cited evidence genuinely supports it AS STATED, or whether it overstates / "
        "misattributes / generalizes beyond the evidence. Return one verdict per claim_id.",
        "",
        "supported=true if the evidence backs the claim as written; supported=false with a one-"
        "sentence reason if it does not. Use only the claim ids listed; judge sparingly and only "
        "refuse with a clear, specific reason.",
        "",
        "Claims and their cited evidence:",
    ]
    for c in candidates:
        text = (c.get("claim_text") or "").strip().replace("\n", " ")
        lines.append(f"- {c['claim_id']}: {text}")
        for eid in support.get(c["claim_id"], []):
            ev = evidence.get(eid)
            if ev:
                summ = (ev.get("summary") or ev.get("quoted_text") or "").strip().replace("\n", " ")
                if len(summ) > 200:
                    summ = summ[:197] + "..."
                lines.append(f"    evidence {eid}: {summ}")
    return "\n".join(lines)


class ClaimCriticOperator(Operator):
    """#14: a bounded critic pass. For each accepted claim it shows the model the claim + the text
    of its cited evidence and asks whether the evidence supports it AS STATED. K independent votes
    (`run_config.critic_votes`, default 1); a claim refuted by a majority (`K//2 + 1`) is rejected
    through #22's shared rejection path — so it drops from the dossier and shows in the contradictions
    section, with no second rejection mechanism. Off unless the unified predicate holds (runtime +
    pack opt-in). Bounded to K calls and fail-safe: on any model error the claims survive untouched.
    The model proposes verdicts; code validates ids + reasons and applies the majority."""

    NAME = "ClaimCriticOperator"
    INPUT_SCHEMAS = ["claims", "claim_evidence", "evidence"]
    OUTPUT_SCHEMAS = ["claims", "gate_results"]

    def __init__(self, runtime: ModelRuntime | None = None):
        self.runtime = runtime

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        contract = (work.read_rows("research_contracts") or [{}])[0]
        pack = next((p for p in work.read_rows("domain_packs") if p.get("pack_id") == contract.get("domain_pack_id")), {})
        if self.NAME not in (pack.get("llm_operators") or []):
            return {"enabled": 0, "reason": "not enabled for pack"}
        run_config = _read_run_config(ctx)
        runtime_name = run_config.get("model_runtime")
        if not runtime_name:
            return {"enabled": 0, "reason": "no model_runtime"}

        claims = work.read_rows("claims")
        candidates, capped = _critic_candidates(claims)
        if not candidates:
            return {"enabled": 1, "reviewed": 0, "refuted": 0, "kept": 0}
        candidate_ids = {c["claim_id"] for c in candidates}
        evidence = {e["evidence_id"]: e for e in work.read_rows("evidence")}
        support: dict = {}
        for ce in work.read_rows("claim_evidence"):
            support.setdefault(ce["claim_id"], []).append(ce["evidence_id"])

        runtime = self.runtime or get_runtime(runtime_name)
        runtime_name = getattr(runtime, "name", runtime_name)
        votes = max(1, int(run_config.get("critic_votes", 1) or 1))

        refute: dict = {}
        reasons: dict = {}
        try:
            for s in range(votes):
                records = runtime.propose(_critic_prompt(candidates, support, evidence, s), CRITIC_SCHEMA)
                proposal = records[0] if records and isinstance(records[0], dict) else {}
                voted: set = set()                          # one vote per reviewer per claim, so a
                for v in (proposal.get("verdicts") or []):  # double-listed claim can't fake a majority
                    if not isinstance(v, dict):
                        continue
                    cid, supported, reason = v.get("claim_id"), v.get("supported"), v.get("reason", "")
                    if cid not in candidate_ids or supported is not False or cid in voted:
                        continue
                    if not isinstance(reason, str) or not reason.strip():
                        continue
                    voted.add(cid)
                    refute[cid] = refute.get(cid, 0) + 1
                    reasons.setdefault(cid, reason.strip())
        except ModelRuntimeError as exc:                  # fail-safe: keep the claims untouched
            return {"enabled": 1, "runtime": runtime_name, "runtime_status": "failed", "error": str(exc)}

        threshold = votes // 2 + 1                          # majority (K=1->1, K=3->2 = "2 of 3")
        rejected = [cid for cid, n in refute.items() if n >= threshold]
        by_id = {c["claim_id"]: c for c in claims}
        for cid in rejected:
            _reject_claim(by_id[cid], reasons[cid], source="critic_review")
        if rejected:
            work.write_rows("claims", claims)

        work.append_row("gate_results", {
            "gate_result_id": ids.mint(ctx.run_id, "CRITICGATE", 0),
            "run_id": ctx.run_id, "gate_id": "CriticGate", "gate_version": "0.1.0",
            "status": "warning" if rejected else "pass", "severity": "warning",
            "checked_tables": ["claims", "evidence"],
            "issues": ([f"critic refuted {len(rejected)} claim(s)"] if rejected else []),
            "metrics": {"reviewed": len(candidates), "refuted": len(rejected),
                        "kept": len(candidates) - len(rejected), "votes": votes,
                        "candidates_considered": len(candidates), "capped": capped},
            "created_at": ids.utc_now_iso(),
        })
        dossier_rows = work.read_rows("quality_dossier")    # reconcile (parity with #22/#27 Step 4)
        if dossier_rows:
            dossier = dossier_rows[0]
            warnings = sum(1 for r in work.read_rows("gate_results") if r.get("status") == "warning")
            dossier["warning_count"] = warnings
            if not dossier.get("blocking_gate_failures"):
                dossier["overall_status"] = "warning" if warnings else "pass"
            work.write_rows("quality_dossier", [dossier])
        return {"enabled": 1, "runtime": runtime_name, "reviewed": len(candidates),
                "refuted": len(rejected), "kept": len(candidates) - len(rejected), "votes": votes}
