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

PROPOSAL_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["claim_text", "cited_evidence_ids"],
        "properties": {
            "claim_text": {"type": "string"},
            "claim_type": {"type": "string"},
            "cited_evidence_ids": {"type": "array", "items": {"type": "string"}},
        },
    },
}


def _read_run_config(ctx: RunContext) -> dict:
    path = ctx.input_dir / "run_config.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


class LLMSynthesisOperator(Operator):
    NAME = "LLMSynthesisOperator"
    INPUT_SCHEMAS = ["claims", "evidence"]
    OUTPUT_SCHEMAS = ["claims", "claim_evidence"]

    def __init__(self, runtime: ModelRuntime | None = None):
        self.runtime = runtime

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        contract = (work.read_rows("research_contracts") or [{}])[0]
        pack = next((p for p in work.read_rows("domain_packs") if p.get("pack_id") == contract.get("domain_pack_id")), {})
        if self.NAME not in (pack.get("llm_operators") or []):
            return {"enabled": 0, "proposed": 0, "dropped": 0}

        run_config = _read_run_config(ctx)
        runtime_name = run_config.get("model_runtime", "stub")
        runtime = self.runtime or get_runtime(runtime_name)
        runtime_name = getattr(runtime, "name", runtime_name)
        evidence = {e["evidence_id"]: e for e in work.read_rows("evidence")}
        prompt = _prompt(work.read_rows("claims"), evidence)
        try:
            records = runtime.propose(prompt, PROPOSAL_SCHEMA)
        except ModelRuntimeError as exc:
            return {"enabled": 1, "runtime": runtime_name, "runtime_status": "failed", "error": str(exc),
                    "proposed": 0, "dropped": 0}

        claims = work.read_rows("claims")
        links = work.read_rows("claim_evidence")
        proposed = dropped = 0
        for record in records:
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
# at-a-glance findings list (each with its evidence ids), thematic angle sections, a forward
# outlook, caveats, and open questions. Evidence is cited inline by id in the prose.
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
                "required": ["finding", "evidence_ids"],
                "properties": {
                    "finding": {"type": "string"},
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


def _confidence(evidence_ids: list, ev_to_container: dict) -> str:
    """Code-computed confidence from evidence density + source diversity (not a model self-rating)."""
    containers = {ev_to_container.get(e) for e in evidence_ids if ev_to_container.get(e)}
    if len(containers) >= 2:
        return "high"
    if len(evidence_ids) >= 2:
        return "medium"
    return "low"


class AnswerSynthesisOperator(Operator):
    """Render-phase synthesis: with all claims built and gated, an LLM acts as a senior analyst
    and writes the report dossier — a long synthesized summary, an at-a-glance findings list,
    thematic angle sections, a forward outlook, caveats, and open questions. A deterministic pass
    keeps only citations the model could ground in the evidence catalog (dropping invented refs
    and ungrounded findings/sections) and computes per-finding confidence from source diversity;
    it records an AnswerGroundingGate row. Off unless a live runtime is selected. The model
    proposes the prose; code validates the citations and calibrates confidence."""

    NAME = "AnswerSynthesisOperator"
    INPUT_SCHEMAS = ["claims", "evidence", "claim_evidence", "question_graph_nodes"]
    OUTPUT_SCHEMAS = ["answer", "gate_results"]

    def __init__(self, runtime: ModelRuntime | None = None):
        self.runtime = runtime

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        dossier = (work.read_rows("quality_dossier") or [{}])[0]
        if not dossier.get("approved_for_report_rendering"):
            return {"enabled": 0, "reason": "report not approved"}

        evidence = {e["evidence_id"]: e for e in work.read_rows("evidence")}
        support: dict = {}
        for ce in work.read_rows("claim_evidence"):
            support.setdefault(ce["claim_id"], []).append(ce["evidence_id"])
        used: dict = {}
        for claim in work.read_rows("claims"):
            if claim.get("status") != "accepted":
                continue
            for eid in support.get(claim["claim_id"], []):
                if eid in evidence:
                    used[eid] = evidence[eid]
        if not used:
            return {"enabled": 1, "reason": "no accepted evidence"}
        item_container = {i["selected_item_id"]: i.get("container_id")
                          for i in work.read_rows("selected_source_items")}
        ev_to_container = {eid: item_container.get(ev.get("selected_item_id")) for eid, ev in used.items()}

        run_config = _read_run_config(ctx)
        runtime_name = run_config.get("model_runtime", "stub")
        runtime = self.runtime or get_runtime(runtime_name)
        runtime_name = getattr(runtime, "name", runtime_name)
        topic = (work.read_rows("runs") or [{}])[0].get("topic", "")
        sub_questions = [n["text"] for n in work.read_rows("question_graph_nodes")
                         if n.get("type") == "sub_question"]
        try:
            records = runtime.propose(_answer_prompt(topic, sub_questions, used), ANSWER_SCHEMA)
        except ModelRuntimeError as exc:
            return {"enabled": 1, "runtime": runtime_name, "runtime_status": "failed", "error": str(exc)}
        if not records:
            return {"enabled": 1, "runtime": runtime_name, "findings": 0}

        draft = records[0]
        valid = set(used)
        kept = dropped = 0

        summary, k, d = _ground_prose(str(draft.get("summary", "")), valid)
        kept += k
        dropped += d

        findings = []
        for item in (draft.get("key_findings") or []):
            text = str(item.get("finding", "")).strip()
            cited = [e for e in (item.get("evidence_ids") or []) if isinstance(e, str)]
            grounded = [e for e in cited if e in valid]
            dropped += len(cited) - len(grounded)
            if text and grounded:
                findings.append({"finding": text, "evidence_ids": grounded,
                                 "confidence": _confidence(grounded, ev_to_container)})
                kept += len(grounded)

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
        return {"enabled": 1, "runtime": runtime_name, "findings": len(findings),
                "sections": len(sections), "citations_kept": kept, "citations_dropped": dropped}


def _answer_prompt(topic: str, sub_questions: list[str], evidence: dict[str, dict]) -> str:
    lines = [
        "You are a senior research analyst writing a deep-research dossier that answers the topic. "
        "Use ONLY the evidence below and cite it inline in square brackets by id, e.g. [EV0001] "
        "(cite more than one where apt). Connective sentences may be uncited, but never state a fact "
        "without a citation, and never invent ids.",
        "",
        f"Topic: {topic}",
        "",
        "Return JSON with these fields:",
        "- summary: THE CENTREPIECE and BY FAR THE LONGEST field — at least 6 full paragraphs, "
        "roughly 600-900 words, longer and richer than all the sections combined. Devote a paragraph "
        "to each major angle/perspective, then a synthesis paragraph on the throughline, where the "
        "sources converge AND where they conflict, and what it all means. Write a deep, well-cited "
        "analytical narrative — never a short abstract or a restatement of the findings list. Do not "
        "be terse; depth and multiple perspectives are the point.",
        "- key_findings: the 5-8 most material findings; each one tight sentence with its evidence_ids.",
        "- sections: 3-5 thematic sections, each {title: an insightful named angle, body: one to three "
        "paragraphs of grounded narrative with inline [id] citations}. Organize around the angles below.",
        "- outlook: 3-5 forward-looking 'where this is heading' points (cite where the evidence supports it).",
        "- caveats: honest limitations — source bias, thin or single-source evidence, conflicting or "
        "unverified claims.",
        "- open_questions: genuinely unresolved questions the evidence raises.",
        "",
        "Angles to cover where the evidence supports them (do not pad to fill one):",
    ]
    lines += [f"- {q}" for q in sub_questions]
    lines += ["", "Evidence (cite by these exact ids):"]
    for eid, ev in evidence.items():
        summary = (ev.get("summary") or ev.get("quoted_text") or "").strip().replace("\n", " ")
        if len(summary) > 240:
            summary = summary[:237] + "..."
        lines.append(f"- {eid}: {summary}")
    return "\n".join(lines)
