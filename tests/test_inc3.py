import json
import tempfile
import unittest

from ai4research.operators import gates as G
from tests import support


def _write_run_config(ctx, config: dict) -> None:
    (ctx.input_dir / "run_config.json").write_text(json.dumps(config), encoding="utf-8")


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
        "container_id": "C-GH-INC3",
        "source_pack_type": "github_repo",
        "container_locator": "https://github.com/example",
        "label": "Comparison repos",
        "items": items,
    }


def _run_domain(tmp, containers, *, pack="youtube_github_research"):
    ctx, work = support.stage(tmp, support.PROVING_TOPIC, containers)
    _write_run_config(ctx, {"domain_pack": pack})
    result = support.run_full(ctx, work)
    return ctx, work, result


def _snap(work):
    tables = ["runs", "research_contracts", "question_graph_nodes", "source_containers",
              "selected_source_items", "acquisition_attempts", "spans", "evidence",
              "claims", "claim_evidence", "claim_edges", "citations", "figures"]
    s = {table: work.read_rows(table) for table in tables}
    s["documents"] = work.read_documents()
    return s


class Increment3Test(unittest.TestCase):
    def test_extractive_claims_have_kind_and_no_derivation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            result = support.run_full(ctx, work)
            self.assertEqual(result.status, "finalized")
            self.assertTrue(work.read_rows("claims"))
            self.assertTrue(all(c["claim_kind"] == "extractive" for c in work.read_rows("claims")))
            self.assertTrue(all(c["derivation"] is None for c in work.read_rows("claims")))

    def test_comparison_and_trend_claims_from_metric_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run_domain(tmp, [_github_container([
                _github_item("I-GH-A", "RepoA", stars=1240, releases=7),
                _github_item("I-GH-B", "RepoB", stars=310, releases=2),
            ])])
            self.assertEqual(result.status, "finalized")
            comparison = [c for c in work.read_rows("claims") if c["claim_type"] == "comparison_claim"]
            trends = [c for c in work.read_rows("claims") if c["claim_type"] == "trend_claim"]
            self.assertEqual(len(comparison), 1)
            self.assertEqual(comparison[0]["claim_kind"], "comparative")
            self.assertEqual(comparison[0]["derivation"]["computed"]["ratio"], 4.0)
            self.assertEqual(len(trends), 2)
            self.assertTrue(any(c["derivation"]["computed"]["count"] == 7 for c in trends))
            self.assertTrue(any(e["type"] == "compares_to" for e in work.read_rows("claim_edges")))

    def test_single_metric_repo_gets_trend_no_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run_domain(tmp, [_github_container([
                _github_item("I-GH-A", "RepoA", stars=1240, releases=7),
            ])])
            self.assertEqual(result.status, "finalized")
            self.assertEqual(len([c for c in work.read_rows("claims") if c["claim_type"] == "trend_claim"]), 1)
            self.assertEqual(len([c for c in work.read_rows("claims") if c["claim_type"] == "comparison_claim"]), 0)

    def test_synthesis_grounding_gate_passes_and_fails_on_tamper_or_missing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, _ = _run_domain(tmp, [_github_container([
                _github_item("I-GH-A", "RepoA", stars=1240, releases=7),
                _github_item("I-GH-B", "RepoB", stars=310, releases=2),
            ])])
            snap = _snap(work)
            self.assertEqual(G.gate_synthesis_grounding(snap, {})["status"], G.PASS)

            tampered = {**snap, "claims": [dict(c) for c in snap["claims"]]}
            comp = next(c for c in tampered["claims"] if c["claim_type"] == "comparison_claim")
            comp["derivation"] = dict(comp["derivation"])
            comp["derivation"]["computed"] = {"ratio": 99}
            self.assertEqual(G.gate_synthesis_grounding(tampered, {})["status"], G.HARD_FAIL)

            missing = {**snap, "claims": [dict(c) for c in snap["claims"]]}
            trend = next(c for c in missing["claims"] if c["claim_type"] == "trend_claim")
            trend["derivation"] = dict(trend["derivation"])
            trend["derivation"]["inputs"] = [dict(trend["derivation"]["inputs"][0])]
            trend["derivation"]["inputs"][0]["evidence_id"] = "missing"
            self.assertEqual(G.gate_synthesis_grounding(missing, {})["status"], G.HARD_FAIL)

    def test_entity_tagging_and_generic_has_no_entities(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, _ = _run_domain(tmp, [support.local_container()])
            self.assertTrue(work.read_rows("entities"))
            self.assertTrue(work.read_rows("claim_entities"))

        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            support.run_full(ctx, work)
            self.assertEqual(work.read_rows("entities"), [])
            self.assertEqual(work.read_rows("claim_entities"), [])

    def test_figure_and_figure_grounding_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, _ = _run_domain(tmp, [_github_container([
                _github_item("I-GH-A", "RepoA", stars=1240, releases=7),
                _github_item("I-GH-B", "RepoB", stars=310, releases=2),
            ])])
            figures = work.read_rows("figures")
            self.assertEqual(len(figures), 1)
            self.assertEqual(figures[0]["kind"], "comparison_matrix")
            self.assertEqual(G.gate_figure_grounding(_snap(work), {})["status"], G.PASS)

            bad = _snap(work)
            bad["figures"] = [dict(figures[0])]
            bad["figures"][0]["grounded_claim_ids"] = ["bogus"]
            self.assertEqual(G.gate_figure_grounding(bad, {})["status"], G.HARD_FAIL)

    def test_no_metric_data_synthesizes_nothing_and_finalizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run_domain(tmp, [support.github_container()])
            self.assertEqual(result.status, "finalized")
            self.assertEqual([c for c in work.read_rows("claims") if c["claim_kind"] != "extractive"], [])

    def test_schema_round_trips_synthesis_tables_and_grounding_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _, result = _run_domain(tmp, [_github_container([
                _github_item("I-GH-A", "RepoA", stars=1240, releases=7),
                _github_item("I-GH-B", "RepoB", stars=310, releases=2),
            ])])
            self.assertTrue(result.persisted)
            conn = support.store(ctx)
            try:
                self.assertEqual(conn.execute(
                    "SELECT claim_kind FROM claims WHERE claim_type='comparison_claim'"
                ).fetchone()[0], "comparative")
                self.assertIsNotNone(conn.execute(
                    "SELECT derivation FROM claims WHERE claim_type='comparison_claim'"
                ).fetchone()[0])
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 0)
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM claim_entities").fetchone()[0], 0)
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM figures").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT grounding_level FROM quality_dossier").fetchone()[0], "traceable")
            finally:
                conn.close()

    def test_report_contains_comparison_finding_and_grounded_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _, result = _run_domain(tmp, [_github_container([
                _github_item("I-GH-A", "RepoA", stars=1240, releases=7),
                _github_item("I-GH-B", "RepoB", stars=310, releases=2),
            ])])
            self.assertEqual(result.status, "finalized")
            report = (ctx.exports_dir / "final_report.md").read_text(encoding="utf-8")
            self.assertIn("Repo RepoA has 4.0x the stars of Repo RepoB (1240 vs 310).", report)
            self.assertIn("### Grounded Comparison Matrix", report)
            self.assertIn("| repo | stars | releases |", report)


if __name__ == "__main__":
    unittest.main()
