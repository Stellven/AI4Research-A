"""Increment 6/9 (#22): contradiction-aware claim graph via the north-star COUNTER-SEARCH. For each
headline claim the LLM searches a source-balanced evidence pool for evidence FROM A DIFFERENT SOURCE
that contradicts/qualifies it; code validates ids + enforces cross-source, then writes an
evidence -> claim refutes/qualifies edge (rendered "disputed by {source}"). Prompt-keyed StubRuntime."""
import re
import tempfile
import unittest

from ai4research.model_runtime import StubRuntime
from ai4research.operators import OperatorRunner, build_pipeline
from tests import support

import json

_CONTRA_KEY = "COUNTER-SEARCH"   # only the ContradictionDetect (counter-search) prompt contains this


def _write_run_config(ctx, config: dict) -> None:
    (ctx.input_dir / "run_config.json").write_text(json.dumps(config), encoding="utf-8")


def _run(tmp, proposal_fn, *, pack="youtube_github_research", runtime="stub", containers=None):
    """proposal_fn(prompt)->dict drives ContradictionDetect; every other LLM prompt gets []."""
    ctx, work = support.stage(tmp, support.PROVING_TOPIC, containers or [support.local_container()])
    cfg = {"domain_pack": pack}
    if runtime:
        cfg["model_runtime"] = runtime
    _write_run_config(ctx, cfg)

    def stub(prompt):
        if proposal_fn is not None and _CONTRA_KEY in prompt:
            return [proposal_fn(prompt)]
        return []

    rt = StubRuntime(stub) if runtime else None
    OperatorRunner(build_pipeline(model_runtime=rt)).run(ctx, work)
    from ai4research.finalize import finalize
    return ctx, work, finalize(ctx, work)


def _edges(work, *types):
    return [e for e in work.read_rows("claim_edges") if e["type"] in types]


def _gate(work):
    return next((g for g in work.read_rows("gate_results") if g["gate_id"] == "ContradictionReviewGate"), None)


def _claim_sources(prompt):
    return re.findall(r"(run_[A-Za-z0-9_]+\.CLAIM\d+) \[source: ([^\]]+)\]", prompt)


def _evidence_sources(prompt):
    return re.findall(r"(run_[A-Za-z0-9_]+\.EV\d+) \[source: ([^\]]+)\]", prompt)


def _cross_dispute(prompt):
    """Pick a (claim, evidence) pair from DIFFERENT sources — what the counter-search asks for."""
    evid = _evidence_sources(prompt)
    for cid, cs in _claim_sources(prompt):
        ev = next((e for e, es in evid if es != cs), None)
        if ev:
            return cid, ev
    return None, None


def _two_source_run(tmp, proposal):
    from tests.test_inc4 import _github_container, _github_item
    return _run(tmp, proposal, containers=[support.local_container(),
                _github_container([_github_item("I-A", "RepoA", stars=1240, releases=7)])])


class PipelineOrderTest(unittest.TestCase):
    def test_contradiction_detect_between_gate_suite_and_synthesis(self):
        names = [op.NAME for op in build_pipeline()]
        self.assertLess(names.index("PreRenderQualityGateSuite"), names.index("ContradictionDetectOperator"))
        self.assertLess(names.index("ContradictionDetectOperator"), names.index("AnswerSynthesisOperator"))


class ContradictionSearchTest(unittest.TestCase):
    def test_ac1_cross_source_dispute_and_rejection(self):
        def proposal(prompt):
            cid, eid = _cross_dispute(prompt)
            claims = _claim_sources(prompt)
            return {"disputes": ([{"claim_id": cid, "evidence_id": eid,
                                   "relation": "contradicts", "note": "opposes"}] if cid else []),
                    "rejections": ([{"claim_id": claims[-1][0], "reason": "unsupported on review"}]
                                   if claims else [])}

        with tempfile.TemporaryDirectory() as tmp:
            ctx, work, result = _two_source_run(tmp, proposal)
            self.assertEqual(result.status, "finalized")
            refutes = _edges(work, "refutes")
            self.assertEqual(len(refutes), 1)
            self.assertIn(".EV", refutes[0]["from_id"])        # edge is evidence -> claim (counter-evidence)
            self.assertIn(".CLAIM", refutes[0]["to_id"])
            rejected = [c for c in work.read_rows("claims") if c["status"] == "rejected"
                        and any("rejected on review" in str(x) for x in c["limitations"])]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["derivation"].get("review"), "contradiction_review")
            gate = _gate(work)
            self.assertEqual(gate["status"], "warning")
            self.assertEqual(gate["metrics"]["disputes"], 1)
            self.assertEqual(gate["metrics"]["rejections"], 1)
            md = (ctx.exports_dir / "final_report.md").read_text(encoding="utf-8")
            self.assertIn("### Where sources disagree", md)
            self.assertIn("disputed by", md)

    def test_ac2_invalid_and_same_source_ignored(self):
        def proposal(prompt):
            claims, evid = _claim_sources(prompt), _evidence_sources(prompt)
            same = next(((cid, e) for cid, cs in claims for e, es in evid if es == cs), (None, None))
            disputes = [
                {"claim_id": "run_x.CLAIM9999", "evidence_id": evid[0][0], "relation": "contradicts", "note": "bad claim"},
                {"claim_id": claims[0][0], "evidence_id": "run_x.EV9999", "relation": "contradicts", "note": "bad ev"},
                {"claim_id": claims[0][0], "evidence_id": evid[0][0], "relation": "supports", "note": "bad relation"},
            ]
            if same[0]:    # a claim + evidence from the SAME source must be dropped (cross-source only)
                disputes.append({"claim_id": same[0], "evidence_id": same[1],
                                 "relation": "contradicts", "note": "same source"})
            return {"disputes": disputes, "rejections": [{"claim_id": claims[0][0], "reason": "   "}]}

        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _two_source_run(tmp, proposal)
            self.assertEqual(result.status, "finalized")
            self.assertEqual(_edges(work, "refutes", "qualifies"), [])      # nothing written
            self.assertNotIn("rejected", {c["status"] for c in work.read_rows("claims")})
            self.assertEqual(_gate(work)["metrics"]["disputes"], 0)

    def test_c1_qualifies_flips_status(self):
        def proposal(prompt):
            cid, eid = _cross_dispute(prompt)
            return {"disputes": ([{"claim_id": cid, "evidence_id": eid,
                                   "relation": "qualifies", "note": "narrows it"}] if cid else []),
                    "rejections": []}

        with tempfile.TemporaryDirectory() as tmp:
            ctx, work, result = _two_source_run(tmp, proposal)
            self.assertEqual(result.status, "finalized")
            self.assertEqual(len(_edges(work, "qualifies")), 1)
            qualified = [c for c in work.read_rows("claims") if c["status"] == "qualified"]
            self.assertEqual(len(qualified), 1)
            self.assertTrue(any(str(x).startswith("qualified by") for x in qualified[0]["limitations"]))
            md = (ctx.exports_dir / "final_report.md").read_text(encoding="utf-8")
            self.assertIn("**qualified by**", md)

    def test_ac4_inert_without_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work, result = _run(tmp, None, runtime=None)
            self.assertEqual(result.status, "finalized")
            self.assertIsNone(_gate(work))
            self.assertTrue(all(c["status"] == "accepted" for c in work.read_rows("claims")))
            self.assertEqual(_edges(work, "refutes", "qualifies"), [])
            md = (ctx.exports_dir / "final_report.md").read_text(encoding="utf-8")
            self.assertNotIn("## Contradictions & rejected claims", md)

    def test_ac5_gate_metrics_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, _ = _two_source_run(tmp, lambda p: {"disputes": [], "rejections": []})
            gate = _gate(work)
            for k in ("disputes", "qualifications", "rejections", "anchors", "pool"):
                self.assertIn(k, gate["metrics"])
            self.assertGreaterEqual(gate["metrics"]["pool"], 1)

    def test_ac6_inert_without_pack_optin(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run(tmp, lambda p: {"disputes": [], "rejections": []}, pack="generic")
            self.assertEqual(result.status, "finalized")
            self.assertIsNone(_gate(work))


if __name__ == "__main__":
    unittest.main()
