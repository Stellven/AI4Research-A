"""Optional LLM synthesis operators.

These are local and deterministic unless a live runtime is explicitly selected. They only
write draft proposals; gates decide acceptance later (LLMSynthesis -> EntailmentGate;
AnswerSynthesis -> a deterministic grounding pass that keeps only citations it can verify).
"""
from __future__ import annotations

import json

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


# --- Increment 5: render-phase synthesis of the findings brief ---------------------------

# codex's --output-schema requires a top-level object (strict mode: every field required, no
# extra properties). The brief is an executive summary plus a ranked list of the most material
# findings, each carrying the ids of the evidence that supports it.
ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["executive_summary", "key_findings"],
    "properties": {
        "executive_summary": {"type": "string"},
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
    },
}


class AnswerSynthesisOperator(Operator):
    """Render-phase synthesis: with all claims built and gated, an LLM acts as an analyst and
    writes the report's findings brief — an executive summary plus a ranked, selective list of
    the most material findings, each citing the evidence that supports it. A deterministic pass
    then keeps only citations the model could ground in the evidence catalog and drops any
    finding it could not ground (no garbage), recording an AnswerGroundingGate row. Off unless a
    live runtime is selected — the default stub proposes nothing, so the report is unchanged.
    The model proposes the prose; code validates the citations."""

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
        kept_refs = dropped_refs = dropped_findings = 0
        findings = []
        for item in (draft.get("key_findings") or []):
            text = str(item.get("finding", "")).strip()
            cited = [e for e in (item.get("evidence_ids") or []) if isinstance(e, str)]
            grounded = [e for e in cited if e in valid]
            dropped_refs += len(cited) - len(grounded)
            if text and grounded:
                findings.append({"finding": text, "evidence_ids": grounded})
                kept_refs += len(grounded)
            elif text:
                dropped_findings += 1   # an ungrounded finding -> dropped (no garbage)
        if not findings:
            return {"enabled": 1, "runtime": runtime_name, "findings": 0, "reason": "ungrounded"}

        work.write_rows("answer", [{
            "run_id": ctx.run_id,
            "executive_summary": str(draft.get("executive_summary", "")).strip(),
            "key_findings": findings, "source": runtime_name,
            "citations_kept": kept_refs, "citations_dropped": dropped_refs,
            "findings_dropped": dropped_findings,
        }])
        issues = []
        if dropped_findings:
            issues.append(f"dropped {dropped_findings} ungrounded finding(s)")
        if dropped_refs:
            issues.append(f"stripped {dropped_refs} ungrounded citation(s)")
        work.append_row("gate_results", {
            "gate_result_id": ids.mint(ctx.run_id, "ANSGATE", 0),
            "run_id": ctx.run_id, "gate_id": "AnswerGroundingGate", "gate_version": "0.1.0",
            "status": "warning" if issues else "pass", "severity": "warning",
            "checked_tables": ["answer", "evidence"], "issues": issues,
            "metrics": {"findings": len(findings), "citations_kept": kept_refs,
                        "citations_dropped": dropped_refs, "findings_dropped": dropped_findings},
            "created_at": ids.utc_now_iso(),
        })
        return {"enabled": 1, "runtime": runtime_name, "findings": len(findings),
                "citations_kept": kept_refs, "citations_dropped": dropped_refs,
                "findings_dropped": dropped_findings}


def _answer_prompt(topic: str, sub_questions: list[str], evidence: dict[str, dict]) -> str:
    lines = [
        "You are a research analyst writing the findings brief of a report for a busy decision-maker "
        "(think investor or associate): high signal, specific, no filler. From the evidence below, "
        "select ONLY the most material, decision-relevant findings; ignore intros, asides, and minor "
        "detail. Prefer findings with concrete specifics — numbers, names, comparisons. Rank them, "
        "most important first, and keep to the 5-8 strongest.",
        "",
        f"Topic: {topic}",
        "",
        "Cover these angles where the evidence supports them (do not pad to fill an angle):",
    ]
    lines += [f"- {q}" for q in sub_questions]
    lines += ["",
              "Each finding is one tight, specific sentence plus the ids of the evidence that supports "
              "it. Cite only ids that genuinely support the sentence; never invent ids.",
              "",
              "Evidence (cite by these exact ids):"]
    for eid, ev in evidence.items():
        summary = (ev.get("summary") or ev.get("quoted_text") or "").strip().replace("\n", " ")
        if len(summary) > 240:
            summary = summary[:237] + "..."
        lines.append(f"- {eid}: {summary}")
    lines += ["",
              'Return JSON {"executive_summary": "<2-3 sentence top-line answer to the topic>", '
              '"key_findings": [{"finding": "<one specific, material sentence>", '
              '"evidence_ids": ["<id>", ...]}]}.']
    return "\n".join(lines)
