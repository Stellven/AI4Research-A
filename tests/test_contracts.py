"""§14.4 contract tests for later phases. These guarantee the Phase 0 data contracts and
executor survive Phases 1-4: a new adapter, a swapped optimizer, a non-local_python
runtime, and a multi-node question graph must not change the row shapes the spine consumes.
"""
import tempfile
import unittest

from ai4research import store, ids
from ai4research.adapters import base as abase, register
from ai4research.adapters.base import AcquireResult, SourceAdapter
from ai4research.operators import OperatorRunner, build_pipeline
from ai4research.operators.base import Operator, plan_rows, validate_plan
from ai4research.operators.core import QuestionGraphStubOperator, StaticPlanOperator
from ai4research.finalize import finalize
from tests import support

DOC_COLS = {"document_id", "run_id", "selected_item_id", "acquisition_attempt_id", "document_kind",
            "title", "raw_text", "normalized_text", "language", "published_at", "content_hash",
            "normalization", "provider_metadata"}
SPAN_COLS = {"span_id", "run_id", "document_id", "selected_item_id", "span_index",
             "start_char", "end_char", "text", "segmentation_strategy", "text_hash"}
EVIDENCE_COLS = {"evidence_id", "run_id", "selected_item_id", "document_id", "span_id",
                 "evidence_type", "summary", "quoted_text", "support_strength", "limitations", "published_at"}
CLAIM_COLS = {"claim_id", "run_id", "claim_type", "claim_text", "claim_scope",
              "criticality", "status", "confidence", "limitations"}


def _swap(index: int, op: Operator) -> list[Operator]:
    pipeline = build_pipeline()
    pipeline[index] = op
    return pipeline


class NewAdapterContractTest(unittest.TestCase):
    def test_new_adapter_flows_through_unchanged_spine(self):
        class Phase2StubAdapter(SourceAdapter):
            ADAPTER_ID = "phase2_stub"

            def acquire(self, item):
                return AcquireResult(
                    True, document_kind="local_document", title="Stub",
                    text="A Phase 2 stub document describes skills governance interoperability "
                         "standards and verifiable credentials so the spine can process it.",
                )

        register(Phase2StubAdapter())
        original = abase.SOURCE_PACK_MAP["local_document_set"]
        abase.SOURCE_PACK_MAP["local_document_set"] = ("local_document", "phase2_stub", "local_document")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                container = {"container_id": "C-NEW", "source_pack_type": "local_document_set",
                             "items": [{"item_id": "I-NEW", "item_locator": {"inline_text": "x"}}]}
                ctx, work = support.stage(tmp, support.PROVING_TOPIC, [container])
                result = support.run_full(ctx, work)
                self.assertEqual(result.status, "finalized")
                # same row shapes as the canonical families
                self.assertEqual(set(work.read_documents()[0]), DOC_COLS)
                self.assertEqual(set(work.read_rows("spans")[0]), SPAN_COLS)
                self.assertEqual(set(work.read_rows("evidence")[0]), EVIDENCE_COLS)
                self.assertEqual(set(work.read_rows("claims")[0]), CLAIM_COLS)
        finally:
            abase.SOURCE_PACK_MAP["local_document_set"] = original


class OptimizerSwapContractTest(unittest.TestCase):
    def test_rule_optimizer_changes_decisions_not_plan_schema(self):
        class RuleOptimizerStub(StaticPlanOperator):
            def run(self, ctx, work, pipeline):
                validate_plan(pipeline)
                nodes, edges = plan_rows(ctx.run_id, pipeline)
                work.write_rows("physical_plan_nodes", nodes)
                work.write_rows("physical_plan_edges", edges)
                work.write_rows("optimizer_decisions", [{
                    "decision_id": ids.mint(ctx.run_id, "D", 0), "run_id": ctx.run_id,
                    "reason": "rule-based selection among candidate operators",
                    "alternatives_considered": ["logical_plan_v1", "logical_plan_v2"],
                }])
                return {"plan_nodes": len(nodes)}

        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            OperatorRunner(_swap(3, RuleOptimizerStub())).run(ctx, work)
            result = finalize(ctx, work)
            self.assertEqual(result.status, "finalized")  # run still completes with the swapped optimizer
            self.assertEqual(set(work.read_rows("physical_plan_nodes")[0]),
                             {"node_id", "run_id", "operator_name", "runtime", "order_index"})
            self.assertTrue(work.read_rows("optimizer_decisions")[0]["alternatives_considered"])


class RuntimeSeamContractTest(unittest.TestCase):
    def test_non_local_python_runtime_accepted_by_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.connect(f"{tmp}/db.sqlite")
            try:
                store.init_schema(conn)
                conn.execute("INSERT INTO runs(run_id,topic,phase,status,started_at,root_dir) "
                             "VALUES('r1','t','phase0','running','now','/x')")
                conn.execute("INSERT INTO operator_specs(operator_name,version,input_schemas,output_schemas,runtime) "
                             "VALUES('Op','0.1','[]','[]','local_python')")
                conn.execute("INSERT INTO physical_plan_nodes(node_id,run_id,operator_name,runtime,order_index) "
                             "VALUES('n1','r1','Op','codex_worker',0)")
                conn.commit()
                self.assertEqual(
                    conn.execute("SELECT runtime FROM physical_plan_nodes").fetchone()[0], "codex_worker"
                )
            finally:
                conn.close()


class QuestionGraphSwapContractTest(unittest.TestCase):
    def test_multi_node_question_graph_does_not_break_downstream(self):
        class MultiNodeQuestionGraph(QuestionGraphStubOperator):
            def run(self, ctx, work, pipeline):
                base = f"{ctx.run_id}.Q"
                work.write_rows("question_graph_nodes", [
                    {"node_id": f"{base}0", "run_id": ctx.run_id, "type": "root_question", "text": "root", "status": "open"},
                    {"node_id": f"{base}1", "run_id": ctx.run_id, "type": "sub_question", "text": "a", "status": "open"},
                    {"node_id": f"{base}2", "run_id": ctx.run_id, "type": "sub_question", "text": "b", "status": "open"},
                ])
                work.write_rows("question_graph_edges", [
                    {"run_id": ctx.run_id, "from_node": f"{base}0", "to_node": f"{base}1", "type": "decomposes_to"},
                    {"run_id": ctx.run_id, "from_node": f"{base}0", "to_node": f"{base}2", "type": "decomposes_to"},
                ])
                return {"nodes": 3}

        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            OperatorRunner(_swap(2, MultiNodeQuestionGraph())).run(ctx, work)
            result = finalize(ctx, work)
            self.assertEqual(result.status, "finalized")
            self.assertEqual(len(work.read_rows("question_graph_nodes")), 3)


if __name__ == "__main__":
    unittest.main()
