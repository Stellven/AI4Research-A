"""Core operators O0-O3: run init, research contract, question-graph stub, static plan."""
from __future__ import annotations

import json

from .. import ids
from ..adapters import SOURCE_PACK_MAP
from ..domain_packs import get_pack, pack_rows
from ..model_runtime import ModelRuntime, get_runtime
from ..runtime import RunContext
from ..workfiles import WorkStore
from .base import Operator, plan_pipeline, plan_rows, spec_rows

# codex requires a top-level object schema (strict). The sub-questions are the angles an
# analyst would investigate; an LLM proposes them, the pack template is the deterministic fallback.
QUESTION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["sub_questions"],
    "properties": {"sub_questions": {"type": "array", "items": {"type": "string"}}},
}


def _question_prompt(topic: str) -> str:
    return (
        "Decompose this research topic into the distinct angles a good analyst would investigate "
        "to answer it well — different perspectives such as what it is / latest developments, "
        "adoption and who is using it, evidence and results, criticisms and limitations, "
        "alternatives — but only those the topic actually warrants. Return 3-6 concise, "
        'non-overlapping sub-questions as JSON {"sub_questions": ["...", ...]}.\n\n'
        f"Topic: {topic}"
    )


def _derive_sub_questions(topic: str, runtime, fallback: list[str]) -> list[str]:
    """LLM-proposed angle sub-questions for the topic; deterministic fallback to the pack
    template on no runtime / failure / empty. Dedup + cap; code validates the model's output."""
    if runtime is None:
        return fallback
    try:
        records = runtime.propose(_question_prompt(topic), QUESTION_SCHEMA)
    except Exception:  # noqa: BLE001 - best-effort; fall back to the template
        return fallback
    if not records:
        return fallback
    seen, out = set(), []
    for q in records[0].get("sub_questions") or []:
        q = str(q).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:6] or fallback


def _read_topic(ctx: RunContext) -> str:
    return json.loads((ctx.input_dir / "topic.json").read_text(encoding="utf-8"))["topic"]


def _read_run_config(ctx: RunContext) -> dict:
    path = ctx.input_dir / "run_config.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


class RunInitializeOperator(Operator):
    NAME = "RunInitializeOperator"
    OUTPUT_SCHEMAS = ["runs", "operator_specs", "domain_packs"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        topic = _read_topic(ctx)
        work.write_rows("runs", [{
            "run_id": ctx.run_id,
            "topic": topic,
            "phase": "phase0",
            "status": "running",
            "started_at": ids.utc_now_iso(),
            "completed_at": None,
            "root_dir": str(ctx.run_dir),
        }])
        work.write_rows("operator_specs", spec_rows(pipeline))
        work.write_rows("domain_packs", pack_rows())
        return {"operators_registered": len(pipeline)}


class ResearchContractOperator(Operator):
    NAME = "ResearchContractOperator"
    INPUT_SCHEMAS = ["runs"]
    OUTPUT_SCHEMAS = ["research_contracts"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        topic = _read_topic(ctx)
        if not topic.strip():
            raise ValueError("topic must be non-empty")
        run_config = _read_run_config(ctx)
        pack = get_pack(run_config.get("domain_pack"))
        allowed_pack_types = list(pack["allowed_source_pack_types"])
        source_policy = {
            "allowed_source_pack_types": allowed_pack_types,
            "allowed_source_adapters": [
                SOURCE_PACK_MAP[pack_type][1] for pack_type in allowed_pack_types if pack_type in SOURCE_PACK_MAP
            ],
            "minimum_source_count": int(run_config.get("minimum_source_count", 1)),
            "expected_source_count": None,
            "exact_source_list_required": True,
            "max_items_per_container": run_config.get("max_items_per_container", pack["max_items_per_container"]),
        }
        work.write_rows("research_contracts", [{
            "contract_id": ids.mint(ctx.run_id, "RC", 0),
            "run_id": ctx.run_id,
            "domain_pack_id": pack["pack_id"],
            "topic": topic,
            "research_type": "source_pack_evidence_report",
            "audience": None,
            "freshness_required": 0,
            "freshness_window_days": run_config.get("freshness_window_days", pack["freshness_window_days"]),
            "source_policy": source_policy,
            "required_dimensions": [
                "what the provided sources say",
                "evidence-backed claims",
                "limitations of the source set",
            ],
            "critical_claim_policy": {
                "minimum_supporting_evidence": 1,
                "allow_unsupported_critical_claims": False,
            },
            "deliverables": ["final_report_md", "final_report_html", "evidence_ledger", "claim_graph", "quality_dossier"],
        }])
        return {}


class QuestionGraphStubOperator(Operator):
    """Build the question graph: a root question (the topic) decomposed into sub-question angles.
    When a live runtime is selected the angles are derived from the topic; otherwise they fall
    back to the domain pack's fixed template (so the default run stays deterministic). A pack with
    no sub-question template (generic) keeps a single root."""

    NAME = "QuestionGraphStubOperator"
    INPUT_SCHEMAS = ["research_contracts"]
    OUTPUT_SCHEMAS = ["question_graph_nodes"]

    def __init__(self, runtime: "ModelRuntime | None" = None):
        self.runtime = runtime

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        topic = _read_topic(ctx)
        contract = (work.read_rows("research_contracts") or [{}])[0]
        pack = get_pack(contract.get("domain_pack_id"))
        template_subs = [e["text"] for e in pack["question_template"] if e["type"] == "sub_question"]

        run_config = _read_run_config(ctx)
        runtime = self.runtime or (get_runtime(run_config["model_runtime"]) if run_config.get("model_runtime") else None)
        # only packs that decompose at all (have a sub-question template) get topic-derived angles;
        # generic keeps a single root question.
        sub_questions = _derive_sub_questions(topic, runtime, template_subs) if template_subs else []

        root_entry = next((e for e in pack["question_template"] if e["type"] == "root_question"), None)
        nodes, edges, root_id = [], [], None
        if root_entry:
            root_id = f"{ctx.run_id}.{root_entry['id']}"
            nodes.append({"node_id": root_id, "run_id": ctx.run_id, "type": "root_question",
                          "text": topic, "status": "open"})
        for i, text_value in enumerate(sub_questions, start=1):
            node_id = f"{ctx.run_id}.Q{i}"
            nodes.append({"node_id": node_id, "run_id": ctx.run_id, "type": "sub_question",
                          "text": text_value, "status": "open"})
            if root_id:
                edges.append({"run_id": ctx.run_id, "from_node": root_id,
                              "to_node": node_id, "type": "decomposes_to"})
        work.write_rows("question_graph_nodes", nodes)
        work.write_rows("question_graph_edges", edges)
        return {"nodes": len(nodes), "edges": len(edges),
                "derived": int(bool(runtime) and sub_questions != template_subs)}


class StaticPlanOperator(Operator):
    NAME = "StaticPlanOperator"
    INPUT_SCHEMAS = ["operator_specs"]
    OUTPUT_SCHEMAS = ["physical_plan_nodes", "physical_plan_edges", "optimizer_decisions"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        names, decision = plan_pipeline(pipeline, _read_run_config(ctx))   # selects + records a real decision
        nodes, edges = plan_rows(ctx.run_id, pipeline)
        work.write_rows("physical_plan_nodes", nodes)
        work.write_rows("physical_plan_edges", edges)
        work.write_rows("optimizer_decisions", [{
            "decision_id": f"{ctx.run_id}.D0",
            "run_id": ctx.run_id,
            "reason": decision["reason"],
            "alternatives_considered": decision["alternatives_considered"],
        }])
        return {"plan_nodes": len(nodes), "operators": len(names)}
