"""Optional LLM synthesis operator.

The operator is local and deterministic unless a live runtime is explicitly selected. It
only writes draft proposals; EntailmentGate decides acceptance later.
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
