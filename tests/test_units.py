"""§14.1 unit tests: operator validations, deterministic text, and each blocking gate."""
import tempfile
import unittest
import json
from pathlib import Path

from ai4research import ids, text
from ai4research.adapters import base as abase
from ai4research.cli import stage_source_pack
from ai4research.operators import OperatorRunner, build_pipeline
from ai4research.operators import RunFailed
from ai4research.operators import gates as G
from ai4research.operators.base import Operator, validate_plan
from ai4research.operators.extraction import _citation_url, _classify
from tests import support

_TABLES = ["runs", "research_contracts", "question_graph_nodes", "physical_plan_nodes",
           "source_containers", "selected_source_items", "acquisition_attempts", "documents",
           "spans", "evidence", "claims", "claim_evidence", "citations",
           "section_claims", "section_citations"]


def snap(**over) -> dict:
    s = {t: [] for t in _TABLES}
    s.update(over)
    return s


class CitationUrlTest(unittest.TestCase):
    """The youtube timestamp deep link must use the right query separator (#16)."""

    def test_youtube_timestamp_separator(self):
        doc = {"document_kind": "youtube_transcript",
               "provider_metadata": {"segments": [{"start_char": 0, "start_seconds": 42}]}}
        span = {"start_char": 0, "end_char": 10}
        # a watch?v=… URL already has '?', so the timestamp joins with '&'
        self.assertEqual(_citation_url({"url": "https://www.youtube.com/watch?v=ID"}, doc, span),
                         "https://www.youtube.com/watch?v=ID&t=42s")
        # a youtu.be/… URL has no '?', so it must join with '?' (not a malformed '&t=')
        self.assertEqual(_citation_url({"url": "https://youtu.be/ID"}, doc, span),
                         "https://youtu.be/ID?t=42s")


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

    def test_apostrophe_is_not_classified_as_quote(self):
        # a possessive/contraction apostrophe must NOT be read as a quotation
        self.assertNotEqual(_classify("the organization's taxonomy is governed by policy."), "quote")
        self.assertNotEqual(_classify("we're adopting verifiable credentials."), "quote")
        # an actual double-quoted quotation IS a quote
        self.assertEqual(_classify('the report states "skills must be auditable" here.'), "quote")


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

    def test_citation_path_incoherence_hard_fails(self):
        s = snap(
            selected_source_items=[{"selected_item_id": "I"}],
            documents=[{"document_id": "D"}],
            spans=[{"span_id": "S1"}, {"span_id": "S2"}],
            evidence=[{"evidence_id": "E", "span_id": "S1", "document_id": "D", "selected_item_id": "I"}],
            citations=[{"citation_id": "CT1", "evidence_id": "E",
                        "span_id": "S2", "document_id": "D", "selected_item_id": "I"}],
        )
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

    def test_schema_validation_required_columns_and_duplicate_pk_hard_fail(self):
        missing = snap(claims=[{"claim_id": "C1", "status": "accepted"}])
        self.assertEqual(G.gate_schema_validation(missing, {})["status"], G.HARD_FAIL)
        duplicate = snap(claims=[
            {"claim_id": "C1", "claim_type": "definition", "status": "accepted"},
            {"claim_id": "C1", "claim_type": "risk_claim", "status": "accepted"},
        ])
        self.assertEqual(G.gate_schema_validation(duplicate, {})["status"], G.HARD_FAIL)

    def test_source_pack_type_enum_derives_from_source_pack_map(self):
        original = dict(abase.SOURCE_PACK_MAP)
        try:
            abase.SOURCE_PACK_MAP["new_family"] = ("new_source", "new_adapter", "new_document")
            s = snap(source_containers=[{"container_id": "C", "source_pack_type": "new_family"}])
            self.assertEqual(G.gate_schema_validation(s, {})["status"], G.PASS)
        finally:
            abase.SOURCE_PACK_MAP.clear()
            abase.SOURCE_PACK_MAP.update(original)

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

    def test_pack_relative_paths_are_staged_relative_to_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack_dir = Path(tmp) / "pack"
            pack_dir.mkdir()
            absolute = Path(tmp) / "abs.md"
            pack = pack_dir / "sources.jsonl"
            dest = Path(tmp) / "staged.jsonl"
            row = {
                "container_id": "C", "source_pack_type": "local_document_set",
                "items": [
                    {"item_id": "I-REL", "item_locator": {"local_path": "sources/doc.md"}},
                    {"item_id": "I-ABS", "item_locator": {"local_path": str(absolute)}},
                ],
            }
            pack.write_text(json.dumps(row), encoding="utf-8")
            stage_source_pack(pack, dest)
            staged = json.loads(dest.read_text(encoding="utf-8"))
            self.assertEqual(staged["items"][0]["item_locator"]["local_path"], str(pack_dir / "sources/doc.md"))
            self.assertEqual(staged["items"][1]["item_locator"]["local_path"], str(absolute))

    def test_custom_plan_subset_drives_runner_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            plan = ["RunInitializeOperator", "ResearchContractOperator"]
            invocations = OperatorRunner(build_pipeline(), plan=plan).run(ctx, work)
            self.assertEqual([row["operator_name"] for row in invocations], plan)
            self.assertEqual([row["operator_name"] for row in work.read_rows("operator_invocations")], plan)


class PlanPipelineTest(unittest.TestCase):
    """#25: the planner selects from the registry and records a real decision (golden parity)."""

    def test_records_real_decision(self):
        from ai4research.operators.base import plan_pipeline
        pipe = build_pipeline()
        names, dec = plan_pipeline(pipe, {"model_runtime": "codex"})
        self.assertEqual(names, [op.NAME for op in pipe])     # v1: full pipeline, dependency order
        self.assertTrue(dec["alternatives_considered"])       # a real alternative, not []
        self.assertIn("llm_augmented", dec["reason"])
        _, det = plan_pipeline(pipe, {})
        self.assertIn("deterministic_core", det["reason"])    # no runtime -> deterministic plan


class AuditHardeningTest(unittest.TestCase):
    """Smaller correctness fixes from the external audit."""

    def test_max_per_container_rejects_non_positive(self):
        from ai4research.cli import main as cli_main
        rc = cli_main(["demo", "--topic", "t", "--source-pack", "/no/such.jsonl", "--max-per-container", "-1"])
        self.assertEqual(rc, 2)   # validated and rejected before staging

    def test_table_cell_neutralizes_pipes_and_newlines(self):
        from ai4research.report import _cell
        self.assertNotIn("|", _cell("a | b\nc"))   # source pipes can't split the row
        self.assertNotIn("\n", _cell("a\nb"))


class InlineLinkSafetyTest(unittest.TestCase):
    """The HTML renderer must not turn unsafe-scheme markdown links from untrusted source text
    into active anchors (XSS). Audit finding."""

    def test_blocks_unsafe_schemes_allows_http_and_relative(self):
        from ai4research.report import _inline
        self.assertNotIn("<a", _inline("[x](javascript:alert(1))"))   # dropped, label kept
        self.assertNotIn("<a", _inline("[x](data:text/html,evil)"))
        self.assertIn('<a href="https://example.com">y</a>', _inline("[y](https://example.com)"))
        self.assertIn("<a href=", _inline("[z](docs/readme.md)"))     # relative allowed


if __name__ == "__main__":
    unittest.main()
