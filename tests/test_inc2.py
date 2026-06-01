import json
import re
import tempfile
import unittest
from pathlib import Path

from ai4research.finalize import finalize
from ai4research.operators import OperatorRunner, build_pipeline
from ai4research.operators import gates as G
from ai4research.operators.extraction import _classify
from tests import support


def _write_run_config(ctx, config: dict) -> None:
    (ctx.input_dir / "run_config.json").write_text(json.dumps(config), encoding="utf-8")


def _run_domain(tmp, containers, config=None):
    ctx, work = support.stage(tmp, support.PROVING_TOPIC, containers)
    if config:
        _write_run_config(ctx, config)
    result = support.run_full(ctx, work)
    return ctx, work, result


def _github_container(stars=1250, with_metrics=True) -> dict:
    metadata = {"path": "README.md"}
    if with_metrics:
        metadata.update({"stars": stars, "releases_in_window": 7, "last_release": "2026-05-20"})
    return {
        "container_id": "C-GH-INC2", "source_pack_type": "github_repo",
        "container_locator": "https://github.com/example/skills",
        "label": "Metrics Repo",
        "items": [{
            "item_id": "I-GH-INC2",
            "title": "Metrics README",
            "provider_metadata": metadata,
            "item_locator": {
                "url": "https://github.com/example/skills/blob/main/README.md",
                "local_fixture_path": str(support.FIXTURES / "repos/skills_framework_readme.md"),
            },
        }],
    }


def _timed_youtube_container(tmp: str) -> dict:
    fixture = Path(tmp) / "timed_transcript.json"
    fixture.write_text(json.dumps([
        {"start": 5, "text": "Short intro."},
        {"start": 42, "text": (
            "The channel says skills taxonomy governance should be auditable, reviewed by "
            "practitioners, and connected to verifiable evidence so competency framework "
            "claims remain trustworthy."
        )},
    ]), encoding="utf-8")
    return {
        "container_id": "C-YT-TIMED", "source_pack_type": "youtube_channel",
        "label": "Timed Channel",
        "items": [{
            "item_id": "I-YT-TIMED",
            "title": "Timed transcript",
            "item_locator": {
                "url": "https://www.youtube.com/watch?v=timed",
                "local_fixture_path": str(fixture),
            },
        }],
    }


class Increment2Test(unittest.TestCase):
    def test_pack_selection_drives_contract_and_generic_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            _write_run_config(ctx, {"domain_pack": "youtube_github_research"})
            OperatorRunner(build_pipeline()[:2]).run(ctx, work)
            contract = work.read_rows("research_contracts")[0]
            self.assertEqual(contract["domain_pack_id"], "youtube_github_research")
            self.assertEqual(contract["source_policy"]["max_items_per_container"], 5)
            self.assertEqual(contract["freshness_window_days"], 180)

        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            OperatorRunner(build_pipeline()[:2]).run(ctx, work)
            contract = work.read_rows("research_contracts")[0]
            self.assertEqual(contract["domain_pack_id"], "generic")
            self.assertIsNone(contract["source_policy"]["max_items_per_container"])
            self.assertIsNone(contract["freshness_window_days"])

    def test_generic_default_keeps_single_root_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            support.run_pipeline(ctx, work)
            self.assertEqual(len(work.read_rows("question_graph_nodes")), 1)
            self.assertEqual(work.read_rows("question_graph_nodes")[0]["type"], "root_question")

    def test_domain_pack_builds_multi_node_question_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            _write_run_config(ctx, {"domain_pack": "youtube_github_research"})
            OperatorRunner(build_pipeline()[:3]).run(ctx, work)
            nodes = work.read_rows("question_graph_nodes")
            edges = work.read_rows("question_graph_edges")
            self.assertGreater(len(nodes), 1)
            self.assertTrue(all(e["type"] == "decomposes_to" for e in edges))
            self.assertEqual(len(edges), len([n for n in nodes if n["type"] == "sub_question"]))

    def test_github_metrics_text_and_no_metrics_degrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, _ = _run_domain(tmp, [_github_container()], {"domain_pack": "youtube_github_research"})
            doc = work.read_documents()[0]
            self.assertIn("Repository metrics - stars: 1250", doc["normalized_text"])
            self.assertTrue(any("Repository metrics" in s["text"] for s in work.read_rows("spans")))
            self.assertTrue(any("Repository metrics" in e["quoted_text"] for e in work.read_rows("evidence")))

        with tempfile.TemporaryDirectory() as tmp:
            _, work, _ = _run_domain(tmp, [_github_container(with_metrics=False)],
                                     {"domain_pack": "youtube_github_research"})
            doc = work.read_documents()[0]
            self.assertNotIn("Repository metrics", doc["normalized_text"])

    def test_source_quality_score_is_deterministic_and_neutral_elsewhere(self):
        scores = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                _, work, _ = _run_domain(
                    tmp,
                    [_github_container(stars=2500), support.local_container(), support.youtube_container()],
                    {"domain_pack": "youtube_github_research"},
                )
                gh_scores = [e["source_quality_score"] for e in work.read_rows("evidence")
                             if e.get("source_quality_score") is not None]
                self.assertTrue(gh_scores)
                self.assertTrue(all(0 < score <= 1 for score in gh_scores))
                scores.append(gh_scores[0])
                local_or_youtube = [
                    e for e in work.read_rows("evidence")
                    if e.get("source_quality_score") is None
                ]
                self.assertTrue(local_or_youtube)
        self.assertEqual(scores[0], scores[1])

    def test_evidence_taxonomy_and_enum_accept_new_types(self):
        self.assertEqual(_classify('the source says "quoted evidence" is important'), "quote")
        evidence = [
            {"evidence_id": f"E{i}", "span_id": f"S{i}", "summary": "s", "quoted_text": "q", "evidence_type": kind}
            for i, kind in enumerate(["quote", "benchmark", "code", "policy", "product_release"])
        ]
        self.assertEqual(G.gate_schema_validation({"evidence": evidence}, {})["status"], G.PASS)

    def test_github_deep_link_uses_span_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, _ = _run_domain(tmp, [_github_container()], {"domain_pack": "youtube_github_research"})
            citation = next(c for c in work.read_rows("citations") if c["url"])
            span = next(s for s in work.read_rows("spans") if s["span_id"] == citation["span_id"])
            doc = next(d for d in work.read_documents() if d["document_id"] == citation["document_id"])
            start_line = 1 + doc["normalized_text"][:span["start_char"]].count("\n")
            end_line = 1 + doc["normalized_text"][:span["end_char"]].count("\n")
            self.assertTrue(citation["url"].endswith(f"#L{start_line}-L{end_line}"))

    def test_youtube_timestamp_deep_link_and_plain_txt_degrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, _ = _run_domain(tmp, [_timed_youtube_container(tmp)], {"domain_pack": "youtube_github_research"})
            urls = [c["url"] for c in work.read_rows("citations") if c["url"]]
            self.assertTrue(any("&t=42s" in url for url in urls))

        with tempfile.TemporaryDirectory() as tmp:
            _, work, _ = _run_domain(tmp, [support.youtube_container()], {"domain_pack": "youtube_github_research"})
            urls = [c["url"] for c in work.read_rows("citations") if c["url"]]
            self.assertTrue(urls)
            self.assertTrue(all("&t=" not in url for url in urls))

    def test_question_coverage_warning_records_coverage_and_remains_nonblocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work, result = _run_domain(tmp, [support.local_container()],
                                            {"domain_pack": "youtube_github_research"})
            self.assertEqual(result.status, "finalized")
            gate = next(g for g in work.read_rows("gate_results") if g["gate_id"] == "QuestionCoverageGate")
            self.assertEqual(gate["status"], G.WARNING)
            dossier = work.read_rows("quality_dossier")[0]
            self.assertTrue(dossier["coverage"]["uncovered"])

        covered = G.gate_question_coverage({
            "question_graph_nodes": [{"node_id": "Q1", "type": "sub_question", "text": "Which repositories are active?"}],
            "claims": [{"claim_id": "C1", "status": "accepted", "claim_text": "Repositories are active."}],
            "claim_evidence": [],
            "evidence": [],
        }, {})
        self.assertEqual(covered["status"], G.PASS)

    def test_schema_round_trips_domain_pack_and_quality_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _, result = _run_domain(tmp, [_github_container()], {"domain_pack": "youtube_github_research"})
            self.assertTrue(result.persisted)
            conn = support.store(ctx)
            try:
                self.assertEqual(
                    conn.execute("SELECT pack_id FROM domain_packs WHERE pack_id='youtube_github_research'").fetchone()[0],
                    "youtube_github_research",
                )
                self.assertEqual(
                    conn.execute("SELECT domain_pack_id FROM research_contracts").fetchone()[0],
                    "youtube_github_research",
                )
                self.assertIsNotNone(conn.execute(
                    "SELECT source_quality_score FROM documents WHERE document_kind='github_document'"
                ).fetchone()[0])
                self.assertIsNotNone(conn.execute(
                    "SELECT source_quality_score FROM evidence WHERE source_quality_score IS NOT NULL LIMIT 1"
                ).fetchone()[0])
            finally:
                conn.close()

    def test_html_contains_clickable_citation_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _, _ = _run_domain(tmp, [_github_container()], {"domain_pack": "youtube_github_research"})
            html = (ctx.exports_dir / "final_report.html").read_text(encoding="utf-8")
            self.assertRegex(html, r'<a href="https://github\.com/example/skills/blob/main/README\.md#L\d+-L\d+">CITE\d+</a>')


if __name__ == "__main__":
    unittest.main()
