"""§14.1 unit tests: operator validations, deterministic text, and each blocking gate."""
import tempfile
import unittest

from ai4research import ids, text
from ai4research.operators import RunFailed
from ai4research.operators import gates as G
from ai4research.operators.base import Operator, validate_plan
from tests import support

_TABLES = ["runs", "research_contracts", "question_graph_nodes", "physical_plan_nodes",
           "source_containers", "selected_source_items", "acquisition_attempts", "documents",
           "spans", "evidence", "claims", "claim_evidence", "citations",
           "section_claims", "section_citations"]


def snap(**over) -> dict:
    s = {t: [] for t in _TABLES}
    s.update(over)
    return s


class OperatorValidationTest(unittest.TestCase):
    def test_empty_topic_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, "   ", [support.local_container()])
            with self.assertRaises(RunFailed) as cm:
                support.run_pipeline(ctx, work)
            self.assertEqual(cm.exception.operator_name, "ResearchContractOperator")

    def test_question_graph_has_one_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            support.run_pipeline(ctx, work)
            nodes = work.read_rows("question_graph_nodes")
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0]["type"], "root_question")

    def test_plan_node_missing_runtime_hard_fails(self):
        class NoRuntime(Operator):
            NAME, RUNTIME = "NoRuntime", ""
        with self.assertRaises(ValueError):
            validate_plan([NoRuntime()])

    def test_duplicate_container_ids_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            c1, c2 = support.local_container(), support.local_container()  # same container_id
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [c1, c2])
            with self.assertRaises(RunFailed) as cm:
                support.run_pipeline(ctx, work)
            self.assertEqual(cm.exception.operator_name, "SourceContainerLoadOperator")

    def test_duplicate_item_ids_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = support.local_container()
            c["items"].append(dict(c["items"][0]))  # duplicate item_id
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [c])
            with self.assertRaises(RunFailed) as cm:
                support.run_pipeline(ctx, work)
            self.assertEqual(cm.exception.operator_name, "SourceItemSelectOperator")


class DeterministicTextTest(unittest.TestCase):
    def test_normalized_hash_stable(self):
        a = text.normalize("line one\r\n\r\n\r\nline two   \n")
        b = text.normalize("line one\n\nline two\n")
        self.assertEqual(a, b)
        self.assertEqual(ids.sha256_text(a), ids.sha256_text(b))

    def test_span_offsets_valid(self):
        normalized = text.normalize((support.FIXTURES / "docs/skills_governance.md").read_text())
        spans = text.segment(normalized)
        self.assertGreater(len(spans), 0)
        for start, end, span_text in spans:
            self.assertEqual(normalized[start:end], span_text)


class GateTest(unittest.TestCase):
    def test_provider_field_quarantine_hard_fails(self):
        bad = G.gate_provider_quarantine(snap(evidence=[{"evidence_id": "E1", "video_id": "abc"}]), {})
        self.assertEqual(bad["status"], G.HARD_FAIL)
        clean = G.gate_provider_quarantine(snap(evidence=[{"evidence_id": "E1", "summary": "x"}]), {})
        self.assertEqual(clean["status"], G.PASS)

    def test_reference_integrity_missing_span_hard_fails(self):
        s = snap(evidence=[{"evidence_id": "E1", "span_id": "SX", "document_id": "DX", "selected_item_id": "IX"}])
        self.assertEqual(G.gate_reference_integrity(s, {})["status"], G.HARD_FAIL)

    def test_accepted_claim_without_evidence_hard_fails(self):
        s = snap(claims=[{"claim_id": "C1", "status": "accepted", "criticality": "normal"}], claim_evidence=[])
        self.assertEqual(G.gate_claim_support(s, {})["status"], G.HARD_FAIL)

    def test_critical_claim_below_minimum_hard_fails(self):
        s = snap(claims=[{"claim_id": "C1", "status": "accepted", "criticality": "critical"}], claim_evidence=[])
        contract = {"critical_claim_policy": {"minimum_supporting_evidence": 1}}
        self.assertEqual(G.gate_critical_claim(s, contract)["status"], G.HARD_FAIL)

    def test_citation_unresolvable_hard_fails(self):
        s = snap(citations=[{"citation_id": "CT1", "evidence_id": "Emissing",
                             "span_id": "S", "document_id": "D", "selected_item_id": "I"}])
        self.assertEqual(G.gate_citation_resolution(s, {})["status"], G.HARD_FAIL)

    def test_section_references_unsupported_claim_hard_fails(self):
        s = snap(claims=[{"claim_id": "C1", "status": "rejected", "criticality": "normal"}],
                 section_claims=[{"section_id": "SEC", "claim_id": "C1"}])
        self.assertEqual(G.gate_report_grounding(s, {})["status"], G.HARD_FAIL)

    def test_required_rows_empty_hard_fails(self):
        self.assertEqual(G.gate_required_rows(snap(), {})["status"], G.HARD_FAIL)

    def test_schema_validation_bad_enum_hard_fails(self):
        s = snap(claims=[{"claim_id": "C1", "status": "bogus", "criticality": "normal", "claim_type": "definition"}])
        self.assertEqual(G.gate_schema_validation(s, {})["status"], G.HARD_FAIL)

    def test_span_offset_mismatch_hard_fails(self):
        s = snap(documents=[{"document_id": "D", "normalized_text": "hello world"}],
                 spans=[{"span_id": "S", "document_id": "D", "start_char": 0, "end_char": 5, "text": "WRONG"}])
        self.assertEqual(G.gate_span_offsets(s, {})["status"], G.HARD_FAIL)

    def test_source_coverage_below_minimum_hard_fails(self):
        s = snap(source_containers=[{"container_id": "C1"}])
        contract = {"source_policy": {"minimum_source_count": 2}}
        result = G.gate_source_coverage(s, contract)
        self.assertEqual(result["status"], G.HARD_FAIL)
        self.assertEqual(result["severity"], G.BLOCKING)

    def test_provider_key_inside_item_locator_hard_fails(self):
        # nested-JSON leak: a provider key smuggled into a generic JSON column
        s = snap(selected_source_items=[{"selected_item_id": "I1", "run_id": "r",
                                         "item_locator": {"url": "u", "video_id": "x"}}])
        self.assertEqual(G.gate_provider_quarantine(s, {})["status"], G.HARD_FAIL)


if __name__ == "__main__":
    unittest.main()
