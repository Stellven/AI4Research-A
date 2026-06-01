"""OperatorRunner — executes the static plan in order, recording one
operator_invocations row per node. Phase 0 fails closed: a failed operator is
recorded, then the run stops.
"""
from __future__ import annotations

from .. import ids
from ..runtime import RunContext
from ..workfiles import WorkStore
from .base import Operator, RunFailed


class OperatorRunner:
    def __init__(self, pipeline: list[Operator], plan: list[str] | None = None):
        self.pipeline = pipeline
        self.registry = {op.NAME: op for op in pipeline}
        if len(self.registry) != len(pipeline):
            raise ValueError("operator names must be unique")
        self.plan = plan or [op.NAME for op in pipeline]
        missing = [name for name in self.plan if name not in self.registry]
        if missing:
            raise ValueError(f"plan references unknown operator(s): {missing}")
        self.planned_pipeline = [self.registry[name] for name in self.plan]

    def run(self, ctx: RunContext, work: WorkStore) -> list[dict]:
        invocations: list[dict] = []
        for idx, name in enumerate(self.plan):
            op = self.registry[name]
            started = ids.utc_now_iso()
            status, error, metrics = "success", None, {}
            try:
                metrics = op.run(ctx, work, self.planned_pipeline) or {}
            except Exception as exc:  # noqa: BLE001 - record any operator failure
                status, error = "failed", f"{type(exc).__name__}: {exc}"
            inv = {
                "invocation_id": ids.mint(ctx.run_id, "OPINV", idx),
                "run_id": ctx.run_id,
                "node_id": ids.node_id(ctx.run_id, idx),
                "operator_name": op.NAME,
                "operator_version": op.VERSION,
                "runtime": op.RUNTIME,
                "started_at": started,
                "completed_at": ids.utc_now_iso(),
                "status": status,
                "metrics": metrics,
                "error": error,
            }
            work.append_row("operator_invocations", inv)
            invocations.append(inv)
            if status == "failed":
                raise RunFailed(op.NAME, error or "unknown error")
        return invocations
