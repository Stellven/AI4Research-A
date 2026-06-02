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
    # Real captions are short per-line snippets, each well under the 80-char evidence floor.
    # The adapter must coalesce them into paragraph-sized spans (otherwise a whole transcript
    # yields no evidence) while preserving each line's timestamp for the &t= deep link.
    lines = [
        "skills taxonomy governance should remain fully auditable",
        "and be reviewed by practitioners across the whole team",
        "every competency framework claim must cite real evidence",
        "so the published catalog of skills stays trustworthy over time",
        "teams version the taxonomy and record each change carefully",
        "reviewers sign off before any new skill becomes published",
        "deprecating an existing skill follows the same review path",
        "automation can propose updates from observed daily work",
        "but a human approves every single promotion decision made",
        "metrics track how often each skill is exercised in practice",
    ]
    fixture.write_text(json.dumps(
        [{"start": i * 8, "text": t} for i, t in enumerate(lines)]), encoding="utf-8")
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


def _youtube_container_from_captions(tmp: str, raw: list, name: str = "caps") -> dict:
    fixture = Path(tmp) / f"{name}.json"
    fixture.write_text(json.dumps(raw), encoding="utf-8")
    return {
        "container_id": f"C-YT-{name}", "source_pack_type": "youtube_channel", "label": "Channel",
        "items": [{
            "item_id": f"I-YT-{name}", "title": "transcript",
            "item_locator": {"url": f"https://www.youtube.com/watch?v={name}",
                             "local_fixture_path": str(fixture)},
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
            ts_urls = [u for u in urls if "&t=" in u]
            # short caption lines used to fall under the evidence floor and yield nothing;
            # coalescing must now produce evidence-backed, timestamped citations
            self.assertTrue(ts_urls)
            caption_starts = {i * 8 for i in range(10)}
            seconds = {int(re.search(r"&t=(\d+)s", u).group(1)) for u in ts_urls}
            self.assertTrue(seconds <= caption_starts)   # every &t= lands on a real caption boundary
            self.assertGreaterEqual(len(seconds), 2)     # coalesced into multiple precisely-timed spans

        with tempfile.TemporaryDirectory() as tmp:
            _, work, _ = _run_domain(tmp, [support.youtube_container()], {"domain_pack": "youtube_github_research"})
            urls = [c["url"] for c in work.read_rows("citations") if c["url"]]
            self.assertTrue(urls)
            self.assertTrue(all("&t=" not in url for url in urls))

    def test_youtube_timestamp_is_exact_when_captions_have_interior_newlines(self):
        # Real captions carry interior newlines / trailing whitespace before them, which
        # normalize() deletes. start_char must index the NORMALIZED text so &t= maps to the
        # exact caption; recording pre-normalize offsets drifts the timestamp to an earlier one.
        raw = [{"start": i * 8,
                "text": (f"governance point number {i} concerns auditable competency frameworks   \n"
                         "and the practitioner review that keeps the published skills catalog honest")}
               for i in range(12)]
        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run_domain(tmp, [_youtube_container_from_captions(tmp, raw)],
                                          {"domain_pack": "youtube_github_research"})
            self.assertEqual(result.status, "finalized")
            spans = {s["span_id"]: s for s in work.read_rows("spans")}
            cleaned = {seg["start"]: " ".join(seg["text"].split()) for seg in raw}
            ts = [(c, spans[c["span_id"]]) for c in work.read_rows("citations") if c["url"] and "&t=" in c["url"]]
            self.assertTrue(ts)
            for cite, span in ts:
                secs = int(re.search(r"&t=(\d+)s", cite["url"]).group(1))
                # the cited passage must BEGIN with the caption its &t= points to (exact mapping)
                self.assertTrue(span["text"].startswith(cleaned[secs]),
                                f"&t={secs}s but span starts {span['text'][:50]!r}")

    def test_short_trailing_caption_is_not_dropped_below_the_evidence_floor(self):
        # A short caption stranded just past a paragraph-flush boundary must merge into the
        # previous paragraph, not become a lone sub-floor span silently dropped from the report.
        tail = "and this final closing remark still matters to the whole audience"
        raw = ([{"start": i * 5,
                 "text": "skills governance reviewers audit every published competency claim with care"}
                for i in range(6)]
               + [{"start": 99, "text": tail}])
        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run_domain(tmp, [_youtube_container_from_captions(tmp, raw, "tail")],
                                          {"domain_pack": "youtube_github_research"})
            self.assertEqual(result.status, "finalized")
            evidence_text = " ".join(e["quoted_text"] for e in work.read_rows("evidence"))
            self.assertIn(tail, evidence_text)

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
