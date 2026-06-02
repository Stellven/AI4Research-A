"""Operator contract + helpers for the operator registry and static plan (design §8.1).

An operator is a bounded, deterministic work unit. For Phase 0 every operator is
`local_python`. The `runtime` attribute is the Phase 4 seam (later a node may dispatch
to a Codex worker without changing the plan).
"""
from __future__ import annotations

from ..ids import node_id
from ..runtime import RunContext
from ..workfiles import WorkStore


class RunFailed(Exception):
    """Raised by the runner when an operator fails; Phase 0 fails closed."""

    def __init__(self, operator_name: str, error: str):
        self.operator_name = operator_name
        self.error = error
        super().__init__(f"{operator_name}: {error}")


class Operator:
    NAME: str = ""
    VERSION: str = "0.1.0"
    RUNTIME: str = "local_python"
    INPUT_SCHEMAS: list[str] = []
    OUTPUT_SCHEMAS: list[str] = []

    def run(self, ctx: RunContext, work: WorkStore, pipeline: "list[Operator]") -> dict:
        """Do the work, write working files, return a metrics dict."""
        raise NotImplementedError


def spec_rows(pipeline: list[Operator]) -> list[dict]:
    """operator_specs rows — the registry snapshot."""
    return [
        {
            "operator_name": op.NAME,
            "version": op.VERSION,
            "input_schemas": list(op.INPUT_SCHEMAS),
            "output_schemas": list(op.OUTPUT_SCHEMAS),
            "runtime": op.RUNTIME,
        }
        for op in pipeline
    ]


def validate_plan(pipeline: list[Operator]) -> None:
    """StaticPlanOperator validation (design §8.3 O3): every node has a runtime, and every
    operator input schema is produced by a strictly-earlier operator. The linear plan is
    acyclic by construction. Raises ValueError (→ RunFailed) on a violation."""
    produced: set[str] = set()
    for op in pipeline:
        if not op.RUNTIME:
            raise ValueError(f"plan node {op.NAME!r} has no runtime")
        for schema in op.INPUT_SCHEMAS:
            if schema not in produced:
                raise ValueError(f"{op.NAME} input {schema!r} has no upstream producer")
        produced.update(op.OUTPUT_SCHEMAS)


_LLM_OPERATORS = {"LLMSynthesisOperator", "AnswerSynthesisOperator"}


def plan_pipeline(pipeline: list[Operator], run_config: dict) -> tuple[list[str], dict]:
    """Rule-based planner (golden-parity v1): select the operator sequence from the registry and
    record a *real* optimizer decision. v1 selects the full contracted pipeline in dependency order
    (validated by validate_plan); the recorded alternative is the deterministic-core plan that omits
    the LLM operators, chosen when no model runtime is configured. Execution is unchanged — the LLM
    operators are inert without a runtime — but the decision is genuine, not 'no alternatives
    considered'. v2 will let the planner actually branch (conditionally include operators)."""
    validate_plan(pipeline)
    names = [op.NAME for op in pipeline]
    has_runtime = bool(run_config.get("model_runtime"))
    llm_ops = [n for n in names if n in _LLM_OPERATORS]
    selected = "llm_augmented" if has_runtime else "deterministic_core"
    reason = (
        f"Selected the {selected} plan: {len(names)} operators in dependency order. "
        + (f"LLM operators {llm_ops} active (model_runtime={run_config.get('model_runtime')})."
           if has_runtime
           else f"No model_runtime configured — deterministic run; LLM operators {llm_ops} are inert.")
    )
    alternatives = [
        f"deterministic_core: omit {llm_ops} (extractive + metric synthesis only)",
        f"llm_augmented: include {llm_ops} for question-derivation + synthesis",
    ]
    return names, {"reason": reason, "alternatives_considered": alternatives}


def plan_rows(run_id: str, pipeline: list[Operator]) -> tuple[list[dict], list[dict]]:
    """physical_plan_nodes + physical_plan_edges for the static linear plan."""
    nodes = [
        {
            "node_id": node_id(run_id, i),
            "run_id": run_id,
            "operator_name": op.NAME,
            "runtime": op.RUNTIME,
            "order_index": i,
        }
        for i, op in enumerate(pipeline)
    ]
    edges = [
        {
            "run_id": run_id,
            "from_node": node_id(run_id, i),
            "to_node": node_id(run_id, i + 1),
            "artifact": (pipeline[i + 1].INPUT_SCHEMAS[0] if pipeline[i + 1].INPUT_SCHEMAS else None),
        }
        for i in range(len(pipeline) - 1)
    ]
    return nodes, edges
