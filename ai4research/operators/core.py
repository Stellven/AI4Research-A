"""Core operators O0-O3: run init, research contract, question-graph stub, static plan."""
from __future__ import annotations

import json

from .. import ids
from ..adapters import SOURCE_PACK_MAP
from ..domain_packs import get_pack, pack_rows
from ..runtime import RunContext
from ..workfiles import WorkStore
from .base import Operator, plan_rows, spec_rows, validate_plan


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
    NAME = "QuestionGraphStubOperator"
    INPUT_SCHEMAS = ["research_contracts"]
    OUTPUT_SCHEMAS = ["question_graph_nodes"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        topic = _read_topic(ctx)
        contract = (work.read_rows("research_contracts") or [{}])[0]
        pack = get_pack(contract.get("domain_pack_id"))
        nodes = []
        edges = []
        root_node_id = None
        for entry in pack["question_template"]:
            qid = entry["id"]
            node_id = f"{ctx.run_id}.{qid}"
            if entry["type"] == "root_question":
                root_node_id = node_id
                text_value = topic
            else:
                text_value = entry["text"]
            nodes.append({
                "node_id": node_id,
                "run_id": ctx.run_id,
                "type": entry["type"],
                "text": text_value,
                "status": "open",
            })
        if root_node_id:
            for node in nodes:
                if node["type"] == "sub_question":
                    edges.append({
                        "run_id": ctx.run_id,
                        "from_node": root_node_id,
                        "to_node": node["node_id"],
                        "type": "decomposes_to",
                    })
        work.write_rows("question_graph_nodes", nodes)
        work.write_rows("question_graph_edges", edges)
        return {"nodes": len(nodes), "edges": len(edges)}


class StaticPlanOperator(Operator):
    NAME = "StaticPlanOperator"
    INPUT_SCHEMAS = ["operator_specs"]
    OUTPUT_SCHEMAS = ["physical_plan_nodes", "physical_plan_edges", "optimizer_decisions"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        validate_plan(pipeline)
        nodes, edges = plan_rows(ctx.run_id, pipeline)
        work.write_rows("physical_plan_nodes", nodes)
        work.write_rows("physical_plan_edges", edges)
        work.write_rows("optimizer_decisions", [{
            "decision_id": f"{ctx.run_id}.D0",
            "run_id": ctx.run_id,
            "reason": "Phase 0 uses a fixed static plan; no alternatives considered.",
            "alternatives_considered": [],
        }])
        return {"plan_nodes": len(nodes)}
