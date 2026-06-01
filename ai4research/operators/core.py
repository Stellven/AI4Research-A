"""Core operators O0-O3: run init, research contract, question-graph stub, static plan."""
from __future__ import annotations

import json

from .. import ids
from ..runtime import RunContext
from ..workfiles import WorkStore
from .base import Operator, plan_rows, spec_rows, validate_plan


def _read_topic(ctx: RunContext) -> str:
    return json.loads((ctx.input_dir / "topic.json").read_text(encoding="utf-8"))["topic"]


class RunInitializeOperator(Operator):
    NAME = "RunInitializeOperator"
    OUTPUT_SCHEMAS = ["runs", "operator_specs"]

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
        return {"operators_registered": len(pipeline)}


class ResearchContractOperator(Operator):
    NAME = "ResearchContractOperator"
    INPUT_SCHEMAS = ["runs"]
    OUTPUT_SCHEMAS = ["research_contracts"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        topic = _read_topic(ctx)
        if not topic.strip():
            raise ValueError("topic must be non-empty")
        work.write_rows("research_contracts", [{
            "contract_id": ids.mint(ctx.run_id, "RC", 0),
            "run_id": ctx.run_id,
            "topic": topic,
            "research_type": "source_pack_evidence_report",
            "audience": None,
            "freshness_required": 0,
            "freshness_window_days": None,
            "source_policy": {
                "allowed_source_pack_types": ["local_document_set", "youtube_channel", "github_repo"],
                "allowed_source_adapters": ["local_document_file", "youtube_transcript_fixture", "github_file_fixture"],
                "minimum_source_count": 1,
                "expected_source_count": None,
                "exact_source_list_required": True,
            },
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
        work.write_rows("question_graph_nodes", [{
            "node_id": f"{ctx.run_id}.Q0",
            "run_id": ctx.run_id,
            "type": "root_question",
            "text": topic,
            "status": "open",
        }])
        return {"nodes": 1}


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
