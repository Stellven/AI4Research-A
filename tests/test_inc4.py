import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai4research.model_runtime import CodexRuntime, StubRuntime
from ai4research.operators import OperatorRunner, build_pipeline
from ai4research.operators import gates as G
from tests import support


def _write_run_config(ctx, config: dict) -> None:
    (ctx.input_dir / "run_config.json").write_text(json.dumps(config), encoding="utf-8")


def _first_evidence_id(prompt: str) -> str:
    match = re.search(r"run_[A-Za-z0-9_]+\.EV\d+", prompt)
    assert match is not None
    return match.group(0)


def _run_with_stub(tmp, records, *, pack="youtube_github_research", containers=None):
    ctx, work = support.stage(tmp, support.PROVING_TOPIC, containers or [support.local_container()])
    _write_run_config(ctx, {"domain_pack": pack, "model_runtime": "stub"})
    OperatorRunner(build_pipeline(model_runtime=StubRuntime(records))).run(ctx, work)
    from ai4research.finalize import finalize
    result = finalize(ctx, work)
    return ctx, work, result


def _github_item(item_id: str, title: str, stars=None, releases=None) -> dict:
    metadata = {"path": f"{title}.md"}
    if stars is not None:
        metadata["stars"] = stars
    if releases is not None:
        metadata["releases_in_window"] = releases
    return {
        "item_id": item_id,
        "title": title,
        "provider_metadata": metadata,
        "item_locator": {
            "url": f"https://github.com/example/{title}/blob/main/README.md",
            "local_fixture_path": str(support.FIXTURES / "repos/skills_framework_readme.md"),
        },
    }


def _github_container(items: list[dict]) -> dict:
    return {
        "container_id": "C-GH-INC4",
        "source_pack_type": "github_repo",
        "label": "Metric repos",
        "items": items,
    }


def _snap(work):
    tables = ["runs", "research_contracts", "question_graph_nodes", "source_containers",
              "selected_source_items", "acquisition_attempts", "spans", "evidence",
              "claims", "claim_evidence", "claim_edges", "citations", "figures"]
    s = {table: work.read_rows(table) for table in tables}
    s["documents"] = work.read_documents()
    return s


class Increment4Test(unittest.TestCase):
    def test_stub_grounded_claim_is_accepted_and_rendered(self):
        def records(prompt):
            return [{"claim_text": "Skills governance evidence",
                     "claim_type": "trend_claim",
                     "cited_evidence_ids": [_first_evidence_id(prompt)]}]

        with tempfile.TemporaryDirectory() as tmp:
            ctx, work, result = _run_with_stub(tmp, records)
            self.assertEqual(result.status, "finalized")
            llm_claim = next(c for c in work.read_rows("claims") if c.get("proposed_by") == "llm_synthesis")
            self.assertEqual(llm_claim["status"], "accepted")
            self.assertEqual(work.read_rows("quality_dossier")[0]["grounding_level"], "entailment_checked")
            report = (ctx.exports_dir / "final_report.md").read_text(encoding="utf-8")
            self.assertIn("Skills governance evidence", report)

    def test_stub_hallucination_is_rejected_not_rendered(self):
        def records(prompt):
            return [{"claim_text": "Quantum robotics teleportation",
                     "claim_type": "trend_claim",
                     "cited_evidence_ids": [_first_evidence_id(prompt)]}]

        with tempfile.TemporaryDirectory() as tmp:
            ctx, work, result = _run_with_stub(tmp, records)
            self.assertEqual(result.status, "finalized")
            llm_claim = next(c for c in work.read_rows("claims") if c.get("proposed_by") == "llm_synthesis")
            self.assertEqual(llm_claim["status"], "rejected")
            report = (ctx.exports_dir / "final_report.md").read_text(encoding="utf-8")
            self.assertNotIn("Quantum robotics teleportation", report)

    def test_stub_missing_evidence_record_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run_with_stub(tmp, [{"claim_text": "Skills governance evidence",
                                                   "cited_evidence_ids": ["missing"]}])
            self.assertEqual(result.status, "finalized")
            self.assertEqual([c for c in work.read_rows("claims") if c.get("proposed_by") == "llm_synthesis"], [])

    def test_runtime_failure_is_fail_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run_with_stub(tmp, [], containers=[support.local_container()])
            self.assertEqual(result.status, "finalized")
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            _write_run_config(ctx, {"domain_pack": "youtube_github_research", "model_runtime": "stub"})
            OperatorRunner(build_pipeline(model_runtime=StubRuntime(error=RuntimeError("boom")))).run(ctx, work)
            from ai4research.finalize import finalize
            result = finalize(ctx, work)
            self.assertEqual(result.status, "finalized")
            self.assertEqual([c for c in work.read_rows("claims") if c.get("proposed_by") == "llm_synthesis"], [])
            llm_inv = next(i for i in work.read_rows("operator_invocations") if i["operator_name"] == "LLMSynthesisOperator")
            self.assertEqual(llm_inv["metrics"]["runtime_status"], "failed")

    def test_llm_off_for_generic_even_with_populated_stub(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run_with_stub(
                tmp,
                [{"claim_text": "Skills governance evidence", "cited_evidence_ids": ["missing"]}],
                pack="generic",
            )
            self.assertEqual(result.status, "finalized")
            self.assertEqual([c for c in work.read_rows("claims") if c.get("proposed_by") == "llm_synthesis"], [])

    def test_metric_value_faithfulness_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, _ = _run_with_stub(tmp, [], containers=[_github_container([
                _github_item("I-A", "RepoA", stars=1240, releases=7),
                _github_item("I-B", "RepoB", stars=310, releases=2),
            ])])
            snap = _snap(work)
            self.assertEqual(G.gate_synthesis_grounding(snap, {})["status"], G.PASS)
            bad = {**snap, "claims": [dict(c) for c in snap["claims"]]}
            comp = next(c for c in bad["claims"] if c["claim_type"] == "comparison_claim")
            comp["derivation"] = json.loads(json.dumps(comp["derivation"]))
            comp["derivation"]["inputs"][0]["value"] = 999
            self.assertEqual(G.gate_synthesis_grounding(bad, {})["status"], G.HARD_FAIL)

    def test_schema_round_trips_llm_and_metric_columns(self):
        def records(prompt):
            return [{"claim_text": "Skills governance evidence",
                     "cited_evidence_ids": [_first_evidence_id(prompt)]}]

        with tempfile.TemporaryDirectory() as tmp:
            ctx, _, result = _run_with_stub(tmp, records, containers=[support.local_container(), _github_container([
                _github_item("I-A", "RepoA", stars=1240, releases=7),
            ])])
            self.assertTrue(result.persisted)
            conn = support.store(ctx)
            try:
                self.assertEqual(conn.execute(
                    "SELECT proposed_by FROM claims WHERE proposed_by='llm_synthesis' LIMIT 1"
                ).fetchone()[0], "llm_synthesis")
                self.assertIsNotNone(conn.execute(
                    "SELECT metric_value FROM evidence WHERE metric_value IS NOT NULL LIMIT 1"
                ).fetchone()[0])
            finally:
                conn.close()


class CodexRuntimeSmokeTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("codex"), "codex binary not available")
    def test_codex_runtime_dispatch_and_parse_with_mocked_subprocess(self):
        def fake_run(cmd, input, text, capture_output, timeout, check):
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            out_path.write_text('[{"claim_text":"x","cited_evidence_ids":["E1"]}]', encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            records = CodexRuntime(timeout=1).propose("prompt", {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["claim_text", "cited_evidence_ids"],
                    "properties": {
                        "claim_text": {"type": "string"},
                        "cited_evidence_ids": {"type": "array"},
                    },
                },
            })
        self.assertEqual(records, [{"claim_text": "x", "cited_evidence_ids": ["E1"]}])


class AnswerSynthesisTest(unittest.TestCase):
    """Increment 5: the render-phase analyst-brief synthesis (LLM proposes prose, code validates
    the citations and drops ungrounded findings). Uses a prompt-keyed StubRuntime — no network."""

    def test_findings_brief_is_selective_grounded_and_leads_report(self):
        def stub(prompt):
            if "findings brief" not in prompt:   # e.g. the LLMSynthesis prompt — stay silent
                return []
            eid = _first_evidence_id(prompt)
            return [{
                "executive_summary": "The sources frame the topic in concrete, practical terms.",
                "key_findings": [
                    {"finding": "The leading repository shows strong adoption and active releases.",
                     "evidence_ids": [eid]},
                    {"finding": "An unsupported claim with only an invented citation.",
                     "evidence_ids": ["run_x.EV9999"]},
                ],
            }]

        with tempfile.TemporaryDirectory() as tmp:
            ctx, work, result = _run_with_stub(tmp, stub)
            self.assertEqual(result.status, "finalized")
            answer = work.read_rows("answer")
            self.assertTrue(answer)
            self.assertEqual(len(answer[0]["key_findings"]), 1)            # ungrounded finding dropped
            self.assertGreaterEqual(answer[0]["citations_kept"], 1)
            md = (ctx.exports_dir / "final_report.md").read_text(encoding="utf-8")
            self.assertIn("## Key Findings", md)
            self.assertIn("The sources frame the topic in concrete, practical terms.", md)
            self.assertIn("The leading repository shows strong adoption", md)
            self.assertNotIn("EV9999", md)                                # invented ref never rendered
            self.assertNotIn("unsupported claim", md)                     # ungrounded finding dropped
            gate = next(g for g in work.read_rows("gate_results") if g["gate_id"] == "AnswerGroundingGate")
            self.assertEqual(gate["status"], "warning")                   # something was dropped


class QuestionGraphDerivationTest(unittest.TestCase):
    """#18: sub-questions are derived from the topic when a runtime is present, else fall back
    to the pack template (deterministic). Prompt-keyed StubRuntime — no network."""

    def test_sub_questions_derived_from_topic(self):
        def stub(prompt):
            if "Decompose this research topic" in prompt:
                return [{"sub_questions": ["Who is adopting it?", "What are the benchmarks?",
                                           "What criticisms exist?"]}]
            return []   # LLMSynthesis / AnswerSynthesis prompts stay silent

        with tempfile.TemporaryDirectory() as tmp:
            ctx, work, _ = _run_with_stub(tmp, stub)
            subs = [n["text"] for n in work.read_rows("question_graph_nodes") if n["type"] == "sub_question"]
            self.assertIn("Who is adopting it?", subs)
            self.assertNotIn("Which repositories are most active?", subs)   # template was replaced

    def test_falls_back_to_template_without_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            _write_run_config(ctx, {"domain_pack": "youtube_github_research"})
            OperatorRunner(build_pipeline()).run(ctx, work)   # no runtime injected
            subs = [n["text"] for n in work.read_rows("question_graph_nodes") if n["type"] == "sub_question"]
            self.assertIn("Which repositories are most active?", subs)   # the pack template


if __name__ == "__main__":
    unittest.main()
