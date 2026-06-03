"""Increment 7 (#14): codex critic pass. For each accepted claim the critic judges whether its
cited evidence supports it AS STATED; a claim refuted by a majority of K votes is rejected through
#22's shared rejection path (status=rejected + reason). Bounded, optional, fail-safe. Prompt-keyed
StubRuntime — no network."""
import json
import re
import tempfile
import unittest

from ai4research.model_runtime import ModelRuntimeError, StubRuntime
from ai4research.operators import OperatorRunner, build_pipeline
from tests import support

_CRITIC_KEY = "meticulous research critic"   # only the ClaimCritic prompt contains this


def _write_run_config(ctx, config: dict) -> None:
    (ctx.input_dir / "run_config.json").write_text(json.dumps(config), encoding="utf-8")


def _candidate_ids(prompt: str) -> list[str]:
    return re.findall(r"run_[A-Za-z0-9_]+\.CLAIM\d+", prompt)


def _run(tmp, verdict_fn, *, runtime="stub", critic_votes=None, pack="youtube_github_research"):
    """verdict_fn(prompt)->dict drives the critic; every other LLM prompt gets []."""
    ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
    cfg = {"domain_pack": pack}
    if runtime:
        cfg["model_runtime"] = runtime
    if critic_votes is not None:
        cfg["critic_votes"] = critic_votes
    _write_run_config(ctx, cfg)

    def stub(prompt):
        if verdict_fn is not None and _CRITIC_KEY in prompt:
            return [verdict_fn(prompt)]
        return []

    rt = StubRuntime(stub) if runtime else None
    OperatorRunner(build_pipeline(model_runtime=rt)).run(ctx, work)
    from ai4research.finalize import finalize
    return ctx, work, finalize(ctx, work)


def _by_id(work):
    return {c["claim_id"]: c for c in work.read_rows("claims")}


def _critic_gate(work):
    return next((g for g in work.read_rows("gate_results") if g["gate_id"] == "CriticGate"), None)


class PipelineOrderTest(unittest.TestCase):
    def test_critic_between_contradiction_and_synthesis(self):
        names = [op.NAME for op in build_pipeline()]
        self.assertLess(names.index("ContradictionDetectOperator"), names.index("ClaimCriticOperator"))
        self.assertLess(names.index("ClaimCriticOperator"), names.index("AnswerSynthesisOperator"))


class ClaimCriticTest(unittest.TestCase):
    def test_ac1_refuted_claim_is_rejected_with_reason(self):
        def verdict(prompt):
            c = _candidate_ids(prompt)
            return {"verdicts": [{"claim_id": c[0], "supported": False,
                                  "reason": "overstates the evidence"}]}

        with tempfile.TemporaryDirectory() as tmp:
            ctx, work, result = _run(tmp, verdict)
            self.assertEqual(result.status, "finalized")
            claims = _by_id(work)
            target = next(c for c in claims.values() if c["status"] == "rejected")
            self.assertTrue(any("rejected on review: overstates the evidence" in str(x)
                                for x in target["limitations"]))
            self.assertEqual(target["derivation"].get("review"), "critic_review")  # critic provenance
            gate = _critic_gate(work)
            self.assertEqual(gate["status"], "warning")
            self.assertEqual(gate["metrics"]["refuted"], 1)
            md = (ctx.exports_dir / "final_report.md").read_text(encoding="utf-8")
            self.assertIn("## Contradictions & rejected claims", md)            # surfaced as structure
            # the rejected claim is not a headline/ledger finding
            self.assertNotIn(target["claim_id"], [c["claim_id"] for c in work.read_rows("claims")
                                                  if c["status"] in ("accepted", "qualified")])

    def test_ac2_invalid_verdicts_ignored(self):
        def verdict(prompt):
            c = _candidate_ids(prompt)
            return {"verdicts": [
                        {"claim_id": "run_x.CLAIM9999", "supported": False, "reason": "invented id"},
                        {"claim_id": c[0], "supported": False, "reason": "   "},        # empty reason
                        {"claim_id": c[1], "supported": True, "reason": "fine"},          # supported
                    ]}

        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run(tmp, verdict)
            self.assertEqual(result.status, "finalized")
            self.assertNotIn("rejected", {c["status"] for c in work.read_rows("claims")})
            self.assertEqual(_critic_gate(work)["metrics"]["refuted"], 0)

    def test_ac3_runtime_failure_is_fail_safe(self):
        def boom(prompt):
            raise ModelRuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run(tmp, boom)
            self.assertEqual(result.status, "finalized")               # run survives
            self.assertTrue(all(c["status"] == "accepted" for c in work.read_rows("claims")))
            self.assertIsNone(_critic_gate(work))                      # no gate row on failure

    def test_ac4_inert_without_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work, result = _run(tmp, lambda p: {"verdicts": []}, runtime=None)
            self.assertEqual(result.status, "finalized")
            self.assertIsNone(_critic_gate(work))
            self.assertTrue(all(c["status"] == "accepted" for c in work.read_rows("claims")))
            md = (ctx.exports_dir / "final_report.md").read_text(encoding="utf-8")
            self.assertNotIn("## Contradictions & rejected claims", md)

    def test_ac5_gate_metrics_and_reconciliation(self):
        def verdict(prompt):
            c = _candidate_ids(prompt)
            return {"verdicts": [{"claim_id": c[0], "supported": False, "reason": "thin support"}]}

        with tempfile.TemporaryDirectory() as tmp:
            _, work, _ = _run(tmp, verdict)
            gate = _critic_gate(work)
            self.assertEqual(gate["metrics"]["votes"], 1)
            self.assertEqual(gate["metrics"]["reviewed"], gate["metrics"]["refuted"] + gate["metrics"]["kept"])
            qd = work.read_rows("quality_dossier")[0]
            warnings = sum(1 for g in work.read_rows("gate_results") if g["status"] == "warning")
            self.assertEqual(qd["warning_count"], warnings)            # dossier reconciled

    def test_ac6_majority_vote_threshold(self):
        # K=3: c[0] refuted by reviewers 1 & 2 (2/3 -> rejected); c[1] refuted by reviewer 1 only
        # (1/3 -> kept).
        def verdict(prompt):
            c = _candidate_ids(prompt)
            if "reviewer #1" in prompt:
                return {"verdicts": [{"claim_id": c[0], "supported": False, "reason": "overstated"},
                                     {"claim_id": c[1], "supported": False, "reason": "lone doubt"}]}
            if "reviewer #2" in prompt:
                return {"verdicts": [{"claim_id": c[0], "supported": False, "reason": "overstated"}]}
            return {"verdicts": []}                                    # reviewer #3 supports all

        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run(tmp, verdict, critic_votes=3)
            self.assertEqual(result.status, "finalized")
            claims = _by_id(work)
            ids_ = sorted(c for c in claims)
            self.assertEqual(claims[ids_[0]]["status"], "rejected")    # 2 of 3 refused
            self.assertEqual(claims[ids_[1]]["status"], "accepted")    # only 1 of 3 -> kept
            self.assertEqual(_critic_gate(work)["metrics"]["votes"], 3)

    def test_one_reviewer_cannot_fake_a_majority(self):
        # K=3: reviewer #1 double-lists the same claim as refuted; #2/#3 support it. One reviewer's
        # duplicate verdicts must NOT reach the 2-of-3 majority — the claim stays accepted.
        def verdict(prompt):
            c = _candidate_ids(prompt)
            if "reviewer #1" in prompt:
                return {"verdicts": [{"claim_id": c[0], "supported": False, "reason": "first"},
                                     {"claim_id": c[0], "supported": False, "reason": "again"}]}
            return {"verdicts": []}                                    # #2 and #3 support everything

        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run(tmp, verdict, critic_votes=3)
            self.assertEqual(result.status, "finalized")
            self.assertTrue(all(c["status"] == "accepted" for c in work.read_rows("claims")))
            self.assertEqual(_critic_gate(work)["metrics"]["refuted"], 0)


if __name__ == "__main__":
    unittest.main()
