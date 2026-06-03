"""Increment 11: axis-aware claim<->claim disagreement (the bake-off winner). The LLM proposes a
cross-source claim PAIR that disagrees on a concept+axis; code validates (distinct accepted claims,
DIFFERENT sources, relation refutes|qualifies, dedup incl. reverse) and writes a claim->claim edge.
Both claims stay accepted (record-only). The report renders it as "A disagrees with B"."""
import unittest

from ai4research.operators.llm import _validate_disagreements
from ai4research.report import _contradictions


class ValidateDisagreementsTest(unittest.TestCase):
    def test_valid_cross_source_pair_becomes_edge(self):
        seen: set = set()
        edges, axes = _validate_disagreements(
            [{"claim_a": "A", "claim_b": "B", "relation": "refutes",
              "axis": "Efficiency", "concept": "Mamba", "reason": "opposite"}],
            accepted={"A", "B"}, claim_container={"A": "c1", "B": "c2"}, seen=seen, run_id="run")
        self.assertEqual(edges, [{"run_id": "run", "from_id": "A", "to_id": "B", "type": "refutes"}])
        self.assertEqual(axes, {"efficiency"})

    def test_drops_same_source_invented_self_and_bad_relation(self):
        items = [
            {"claim_a": "A", "claim_b": "B", "relation": "refutes", "axis": "x"},   # same source -> drop
            {"claim_a": "A", "claim_b": "Z", "relation": "refutes", "axis": "x"},   # Z not accepted -> drop
            {"claim_a": "A", "claim_b": "A", "relation": "refutes", "axis": "x"},   # self -> drop
            {"claim_a": "A", "claim_b": "C", "relation": "supports", "axis": "x"},  # bad relation -> drop
        ]
        edges, _ = _validate_disagreements(
            items, accepted={"A", "B", "C"},
            claim_container={"A": "c1", "B": "c1", "C": "c2"}, seen=set(), run_id="r")
        self.assertEqual(edges, [])

    def test_reverse_pair_deduped(self):
        items = [
            {"claim_a": "A", "claim_b": "C", "relation": "refutes", "axis": "e"},
            {"claim_a": "C", "claim_b": "A", "relation": "refutes", "axis": "e"},   # reverse duplicate
        ]
        edges, _ = _validate_disagreements(
            items, accepted={"A", "C"}, claim_container={"A": "c1", "C": "c2"}, seen=set(), run_id="r")
        self.assertEqual(len(edges), 1)


class _FakeRD:
    """Minimal ReportData for _contradictions: one claim<->claim refutes edge, no evidence edges."""
    claim_edges = [{"type": "refutes", "from_id": "CA", "to_id": "CB"}]
    by_claim = {"CA": {"claim_text": "Mamba matches transformers and is faster"},
                "CB": {"claim_text": "Transformers are better at exact copying"}}
    claims = [{"claim_id": "CA", "status": "accepted"}, {"claim_id": "CB", "status": "accepted"}]
    container_by_claim = {"CA": "c1", "CB": "c2"}
    container_label = {"c1": "YouTube explainer", "c2": "Copying paper"}
    by_evidence: dict = {}
    container_by_evidence: dict = {}
    cite_by_evidence: dict = {}


class ContradictionRenderTest(unittest.TestCase):
    def test_claim_pair_renders_as_disagrees_with(self):
        md = "\n".join(_contradictions(_FakeRD()))
        self.assertIn("### Where sources disagree", md)
        self.assertIn("**disagrees with**", md)
        self.assertIn("Mamba matches transformers", md)
        self.assertIn("(YouTube explainer)", md)
        self.assertIn("(Copying paper)", md)


if __name__ == "__main__":
    unittest.main()
