"""Fetch-tool tests: deterministic, via injected fake clients (no network, no API keys).
The end-to-end test proves the fetched pack satisfies the compiler's input contract.
"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ai4research import ids
from ai4research.cli import stage_source_pack
from ai4research.fetch import run_fetch
from ai4research.fetch.cli import main as fetch_cli_main
from ai4research.fetch.clients import (
    FetchDependencyError, RepoMeta, VideoMeta, YouTubeTranscriptFetcher, YtDlpChannelLister,
    _blob_url, _github_error, _parse_repo, _releases_in_window, normalize_segments,
)
from ai4research.finalize import finalize
from ai4research.operators import OperatorRunner, build_pipeline
from ai4research.runtime import RunContext
from ai4research.workfiles import WorkStore

SEG1 = "Skills governance is the disciplined practice of defining validating and overseeing organizational competencies so that every claim maps to verifiable evidence."
SEG2 = "Repository activity and interoperability standards are recurring themes across recent governance discussions in this space over the past several months of analysis."
README = ("# Open Skills\n\nThis repository provides an interoperable skills taxonomy intended to make "
          "competency data portable across human resource and learning platforms for governance and standards.")


class FakeLister:
    def __init__(self, videos): self.videos = videos
    def list_videos(self, channel_url, since, max_items):
        vids = [v for v in self.videos if not since or (v.published_at or "") >= since]
        return vids[:max_items] if max_items else vids


class FakeTranscripts:
    def __init__(self, by_id): self.by_id = by_id
    def fetch(self, video_id): return self.by_id.get(video_id)


class FakeGitHub:
    def __init__(self, by_url): self.by_url = by_url
    def repo_meta(self, repo_url, since): return self.by_url[repo_url]


class RaisingLister:
    def list_videos(self, *a): raise RuntimeError("api down")


class FetchToolTest(unittest.TestCase):
    def test_channel_to_pack_with_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            lister = FakeLister([
                VideoMeta("vid1", "https://youtu.be/vid1", "Has transcript", "2026-05-01"),
                VideoMeta("vid2", "https://youtu.be/vid2", "No captions", "2026-04-01"),
            ])
            transcripts = FakeTranscripts({"vid1": [{"start": 0, "text": SEG1}]})  # vid2 -> None
            summary = run_fetch(tmp, channels=["https://youtube.com/@x"], repos=[],
                                channel_lister=lister, transcript_fetcher=transcripts, github_client=FakeGitHub({}))
            pack = [json.loads(l) for l in (Path(tmp) / "source_containers.jsonl").read_text().splitlines()]
            self.assertEqual(len(pack), 1)
            items = pack[0]["items"]
            self.assertEqual(len(items), 2)
            self.assertIn("local_fixture_path", items[0]["item_locator"])       # vid1 staged
            self.assertNotIn("local_fixture_path", items[1]["item_locator"])    # vid2 = gap
            self.assertTrue((Path(tmp) / items[0]["item_locator"]["local_fixture_path"]).exists())
            self.assertEqual(summary["transcripts"], 1)
            self.assertEqual(summary["video_gaps"], 1)

    def test_github_to_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHub({"https://github.com/x/skills": RepoMeta(
                "https://github.com/x/skills", "skills", "README.md",
                "https://github.com/x/skills/blob/main/README.md", README, stars=1240,
                releases_in_window=7, last_release="2026-05-20")})
            run_fetch(tmp, channels=[], repos=["https://github.com/x/skills"],
                      channel_lister=FakeLister([]), transcript_fetcher=FakeTranscripts({}), github_client=gh)
            pack = [json.loads(l) for l in (Path(tmp) / "source_containers.jsonl").read_text().splitlines()]
            item = pack[0]["items"][0]
            self.assertEqual(item["provider_metadata"]["stars"], 1240)
            self.assertEqual(item["provider_metadata"]["releases_in_window"], 7)
            self.assertTrue((Path(tmp) / item["item_locator"]["local_fixture_path"]).exists())

    def test_failsafe_channel_error_becomes_empty_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = run_fetch(tmp, channels=["https://youtube.com/@dead"], repos=[],
                                channel_lister=RaisingLister(), transcript_fetcher=FakeTranscripts({}),
                                github_client=FakeGitHub({}))
            pack = [json.loads(l) for l in (Path(tmp) / "source_containers.jsonl").read_text().splitlines()]
            self.assertEqual(pack[0]["items"], [])          # recorded as an empty container, no crash
            self.assertTrue(summary["errors"])

    def test_missing_dependency_raises_clear_error(self):
        with mock.patch.dict(sys.modules, {"yt_dlp": None}):   # make `import yt_dlp` fail
            with self.assertRaises(FetchDependencyError):
                YtDlpChannelLister().list_videos("https://youtube.com/@x", None, None)

    def test_end_to_end_fetched_pack_finalizes_in_compiler(self):
        with tempfile.TemporaryDirectory() as fetched, tempfile.TemporaryDirectory() as runs:
            lister = FakeLister([VideoMeta("panel01", "https://www.youtube.com/watch?v=panel01",
                                           "Governance panel", "2026-05-01")])
            transcripts = FakeTranscripts({"panel01": [{"start": 0, "text": SEG1}, {"start": 42, "text": SEG2}]})
            gh = FakeGitHub({
                "https://github.com/x/a": RepoMeta("https://github.com/x/a", "a", "README.md",
                    "https://github.com/x/a/blob/main/README.md", README, stars=1240, releases_in_window=7, last_release="2026-05-20"),
                "https://github.com/x/b": RepoMeta("https://github.com/x/b", "b", "README.md",
                    "https://github.com/x/b/blob/main/README.md", README, stars=310, releases_in_window=2, last_release="2026-03-01"),
            })
            run_fetch(fetched, channels=["https://youtube.com/@gov"],
                      repos=["https://github.com/x/a", "https://github.com/x/b"],
                      channel_lister=lister, transcript_fetcher=transcripts, github_client=gh)

            # stage exactly like the CLI (resolves relative fixture paths against the pack dir)
            ctx = RunContext(ids.new_run_id(), Path(runs) / "runs")
            ctx.ensure_dirs()
            (ctx.input_dir / "topic.json").write_text(json.dumps({"topic": "skills governance"}))
            (ctx.input_dir / "run_config.json").write_text(json.dumps({"domain_pack": "youtube_github_research"}))
            stage_source_pack(Path(fetched) / "source_containers.jsonl", ctx.input_dir / "source_containers.jsonl")

            work = WorkStore(ctx)
            OperatorRunner(build_pipeline()).run(ctx, work)
            result = finalize(ctx, work)

            self.assertEqual(result.status, "finalized")
            cites = work.read_rows("citations")
            self.assertTrue(any("&t=" in (c.get("url") or "") for c in cites))   # youtube timestamp deep link
            kinds = {c["claim_type"] for c in work.read_rows("claims")}
            self.assertIn("comparison_claim", kinds)                              # 2 repos -> a comparison


class RaisingDepLister:
    def list_videos(self, *a): raise FetchDependencyError("yt-dlp not installed")


class FetchToolRegressionTest(unittest.TestCase):
    """Locks the fetch-tool verification findings (2 blockers + majors/minors)."""

    def test_transcript_fetcher_supports_v1x_api(self):
        # BLOCKER: youtube-transcript-api v1.x removed the static get_transcript; the v1.x
        # instance API (.fetch().to_raw_data()) must work, not silently return None.
        class FetchedV1:
            def to_raw_data(self): return [{"start": 0.0, "text": "hello"}, {"start": 2.0, "text": "world"}]

        class YTApiV1:                       # no get_transcript -> v1.x path
            def fetch(self, video_id): return FetchedV1()

        fake_mod = types.ModuleType("youtube_transcript_api")
        fake_mod.YouTubeTranscriptApi = YTApiV1
        with mock.patch.dict(sys.modules, {"youtube_transcript_api": fake_mod}):
            segs = YouTubeTranscriptFetcher().fetch("vid")
        self.assertEqual(segs, [{"start": 0.0, "text": "hello"}, {"start": 2.0, "text": "world"}])

    def test_transcript_fetcher_api_mismatch_surfaces_not_silent_gap(self):
        class YTApiBroken:                   # neither get_transcript nor fetch
            pass
        fake_mod = types.ModuleType("youtube_transcript_api")
        fake_mod.YouTubeTranscriptApi = YTApiBroken
        with mock.patch.dict(sys.modules, {"youtube_transcript_api": fake_mod}):
            with self.assertRaises(Exception):   # must NOT be swallowed into None
                YouTubeTranscriptFetcher().fetch("vid")

    def test_same_named_repos_do_not_collide(self):
        # BLOCKER: two repos with the same bare name from different owners must not overwrite.
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHub({
                "https://github.com/owner1/skills": RepoMeta("https://github.com/owner1/skills", "owner1/skills",
                    "README.md", "https://github.com/owner1/skills/blob/main/README.md", "OWNER ONE CONTENT", stars=10),
                "https://github.com/owner2/skills": RepoMeta("https://github.com/owner2/skills", "owner2/skills",
                    "README.md", "https://github.com/owner2/skills/blob/main/README.md", "OWNER TWO CONTENT", stars=20),
            })
            run_fetch(tmp, channels=[], repos=["https://github.com/owner1/skills", "https://github.com/owner2/skills"],
                      channel_lister=FakeLister([]), transcript_fetcher=FakeTranscripts({}), github_client=gh)
            files = sorted((Path(tmp) / "sources" / "repos").glob("*.md"))
            self.assertEqual(len(files), 2)                          # two distinct fixtures, no overwrite
            texts = {f.read_text() for f in files}
            self.assertEqual(texts, {"OWNER ONE CONTENT", "OWNER TWO CONTENT"})

    def test_parse_repo_handles_real_url_forms(self):
        for url in ("https://github.com/x/skills", "https://github.com/x/skills.git",
                    "http://github.com/x/skills", "git@github.com:x/skills.git",
                    "github.com/x/skills", "https://ghe.corp.com/x/skills", "x/skills"):
            self.assertEqual(_parse_repo(url), ("x", "skills"), url)
        with self.assertRaises(ValueError):
            _parse_repo("notarepo")

    def test_releases_in_window_tolerates_draft(self):
        # draft release has published_at == null (key present) -> must not crash
        count, last = _releases_in_window(
            [{"published_at": None}, {"published_at": "2026-05-20T00:00:00Z"}, {"published_at": "2026-01-01T00:00:00Z"}],
            since="2026-04-01")
        self.assertEqual(count, 1)
        self.assertEqual(last, "2026-05-20")
        self.assertEqual(_releases_in_window({"message": "Not Found"}, None), (None, None))  # error body

    def test_github_error_detection(self):
        self.assertTrue(_github_error({"message": "Not Found", "documentation_url": "..."}))
        self.assertIsNone(_github_error({"name": "skills", "stargazers_count": 5}))   # success
        self.assertIsNone(_github_error([]))                                          # releases list

    def test_malformed_client_output_is_failsafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            class NoneLister:
                def list_videos(self, *a): return None          # not a list
            summary = run_fetch(tmp, channels=["c"], repos=[], channel_lister=NoneLister(),
                                transcript_fetcher=FakeTranscripts({}), github_client=FakeGitHub({}))
            self.assertTrue((Path(tmp) / "source_containers.jsonl").exists())          # still wrote a pack
            self.assertEqual(summary["channels"], 1)

    def test_non_list_transcript_is_a_gap_not_a_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            lister = FakeLister([VideoMeta("v", "https://youtu.be/v", "t", "2026-05-01")])
            bad = FakeTranscripts({"v": "not a list of segments"})                     # truthy non-list
            summary = run_fetch(tmp, channels=["c"], repos=[], channel_lister=lister,
                                transcript_fetcher=bad, github_client=FakeGitHub({}))
            self.assertEqual(summary["transcripts"], 0)
            self.assertEqual(summary["video_gaps"], 1)
            self.assertEqual(list((Path(tmp) / "sources" / "transcripts").glob("*")), [])

    def test_missing_dependency_surfaces_not_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FetchDependencyError):     # re-raised, not recorded as a gap
                run_fetch(tmp, channels=["c"], repos=[], channel_lister=RaisingDepLister(),
                          transcript_fetcher=FakeTranscripts({}), github_client=FakeGitHub({}))

    def test_repo_loop_failsafe_keeps_prior_results(self):
        # a RepoMeta with non-str file_text (injected client) must become a gap, not crash,
        # and must not lose the prior good repo or the whole pack.
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHub({
                "https://github.com/x/good": RepoMeta("https://github.com/x/good", "x/good", "README.md",
                    "https://github.com/x/good/blob/main/README.md", README, stars=5),
                "https://github.com/x/bad": RepoMeta("https://github.com/x/bad", "x/bad", "README.md",
                    "https://github.com/x/bad/blob/main/README.md", b"bytes-not-str", stars=9),
            })
            summary = run_fetch(tmp, channels=[], repos=["https://github.com/x/good", "https://github.com/x/bad"],
                                channel_lister=FakeLister([]), transcript_fetcher=FakeTranscripts({}), github_client=gh)
            self.assertTrue((Path(tmp) / "source_containers.jsonl").exists())     # pack written despite the bad repo
            files = list((Path(tmp) / "sources" / "repos").glob("*.md"))
            self.assertEqual([f.read_text() for f in files], [README])            # good repo preserved
            self.assertTrue(summary["errors"])                                    # bad repo recorded as an error/gap

    def test_lister_skips_none_entries_and_uses_canonical_url(self):
        class FakeYDL:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download=False):
                return {"entries": [{"id": "a", "title": "A", "upload_date": "20260501"}, None,
                                    {"id": "b", "title": "B", "upload_date": "20260401"}]}
        fake = types.ModuleType("yt_dlp"); fake.YoutubeDL = FakeYDL
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            vids = YtDlpChannelLister().list_videos("https://youtube.com/@x", None, None)
        self.assertEqual([v.video_id for v in vids], ["a", "b"])                  # None entry skipped, no crash
        self.assertTrue(all(v.url == f"https://www.youtube.com/watch?v={v.video_id}" for v in vids))

    def test_blob_url_is_canonical(self):
        self.assertEqual(_blob_url("x", "skills", "main"), "https://github.com/x/skills/blob/main/README.md")

    def test_all_whitespace_transcript_is_a_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            lister = FakeLister([VideoMeta("v", "https://youtu.be/v", "t", "2026-05-01")])
            blank = FakeTranscripts({"v": [{"start": 0.0, "text": "   "}, {"start": 2.0, "text": "\n"}]})
            summary = run_fetch(tmp, channels=["c"], repos=[], channel_lister=lister,
                                transcript_fetcher=blank, github_client=FakeGitHub({}))
            self.assertEqual(summary["transcripts"], 0)
            self.assertEqual(summary["video_gaps"], 1)

    def test_cli_no_sources_and_missing_dep_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(fetch_cli_main(["--out", tmp]), 2)                   # nothing to fetch
            def _raise(*a, **k): raise FetchDependencyError("yt-dlp missing")
            with mock.patch("ai4research.fetch.cli.run_fetch", _raise):
                self.assertEqual(fetch_cli_main(["--out", tmp, "--channels", "https://youtube.com/@x"]), 1)

    def test_pack_is_deterministic(self):
        def once(tmp):
            gh = FakeGitHub({"https://github.com/x/a": RepoMeta("https://github.com/x/a", "x/a", "README.md",
                "https://github.com/x/a/blob/main/README.md", README, stars=1240, releases_in_window=7, last_release="2026-05-20")})
            lister = FakeLister([VideoMeta("vid1", "https://youtu.be/vid1", "T", "2026-05-01")])
            run_fetch(tmp, channels=["c"], repos=["https://github.com/x/a"], channel_lister=lister,
                      transcript_fetcher=FakeTranscripts({"vid1": [{"start": 0, "text": SEG1}]}), github_client=gh)
            return (Path(tmp) / "source_containers.jsonl").read_text()
        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            self.assertEqual(once(t1), once(t2))


class QueryPlanningTest(unittest.TestCase):
    """The discovery query planner: deterministic heuristic + optional LLM via the ModelRuntime
    seam, with a guaranteed heuristic fallback. No network — the LLM path uses StubRuntime."""

    def test_normalize_strips_framing_words_to_keywords(self):
        from ai4research.fetch.query import normalize_query
        self.assertEqual(normalize_query("latest development on GEPA"), "GEPA")
        self.assertEqual(normalize_query("an overview of the current state of RAG"), "RAG")
        # a query with no framing words is left intact
        self.assertEqual(normalize_query("skills governance"), "skills governance")
        # all-filler degrades to the original rather than empty
        self.assertEqual(normalize_query("the latest news"), "the latest news")

    def test_plan_queries_heuristic_without_runtime(self):
        from ai4research.fetch.query import plan_queries
        plan = plan_queries("latest development on GEPA")
        self.assertEqual(plan["source"], "heuristic")
        self.assertEqual(plan["github"], "GEPA")
        self.assertEqual(plan["youtube"], "GEPA")

    def test_plan_queries_uses_llm_records(self):
        from ai4research.fetch.query import plan_queries
        from ai4research.model_runtime import StubRuntime
        rt = StubRuntime(records=[{"github_query": "gepa-ai gepa",
                                   "youtube_query": "GEPA prompt optimization DSPy"}])
        plan = plan_queries("latest development on GEPA", rt)
        self.assertEqual(plan["source"], "stub")
        self.assertEqual(plan["github"], "gepa-ai gepa")
        self.assertEqual(plan["youtube"], "GEPA prompt optimization DSPy")

    def test_plan_queries_falls_back_when_runtime_fails_or_empty(self):
        from ai4research.fetch.query import plan_queries
        from ai4research.model_runtime import StubRuntime
        failed = plan_queries("latest development on GEPA", StubRuntime(error=RuntimeError("boom")))
        self.assertEqual(failed["source"], "heuristic")
        self.assertEqual(failed["github"], "GEPA")
        empty = plan_queries("latest development on GEPA", StubRuntime(records=[]))
        self.assertEqual(empty["source"], "heuristic")

    def test_github_search_relaxes_to_longest_token_on_empty(self):
        from ai4research.fetch.clients import ApiGitHubClient, _search_relaxations
        self.assertEqual(list(_search_relaxations("latest development on GEPA")),
                         ["latest development on GEPA", "development"])
        # search_repos retries with the relaxed candidate when the full query matches nothing
        client = ApiGitHubClient()
        calls = []

        def fake_once(query, limit):
            calls.append(query)
            return ["https://github.com/gepa-ai/gepa"] if query == "GEPA" else []

        client._search_repos_once = fake_once  # noqa: SLF001 - exercises the relaxation loop
        self.assertEqual(client.search_repos("ai GEPA"), ["https://github.com/gepa-ai/gepa"])
        self.assertEqual(calls, ["ai GEPA", "GEPA"])


class YtDlpListerTest(unittest.TestCase):
    """YtDlpChannelLister enriches flat (dateless) entries with real upload dates (#15) — yt_dlp
    is mocked, so this runs without the optional dependency or the network."""

    @staticmethod
    def _fake_ydl(flat_entries, dates):
        class FakeYDL:
            def __init__(self, opts):
                self.flat = bool(opts.get("extract_flat"))
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def extract_info(self, url, download=False):
                if self.flat:
                    return {"entries": flat_entries}
                vid = url.rsplit("=", 1)[-1]
                return {"upload_date": dates.get(vid)}
        return types.SimpleNamespace(YoutubeDL=FakeYDL)

    def test_enriches_dates_and_sorts_recent_first(self):
        fake = self._fake_ydl([{"id": "v1", "title": "older"}, {"id": "v2", "title": "newer"}],
                              {"v1": "20260115", "v2": "20260601"})
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            from ai4research.fetch.clients import YtDlpChannelLister
            vids = YtDlpChannelLister().list_videos("ytsearch2:x", None, 2)
        self.assertEqual([v.video_id for v in vids], ["v2", "v1"])      # newest first
        self.assertEqual(vids[0].published_at, "2026-06-01")
        self.assertEqual(vids[1].published_at, "2026-01-15")

    def test_since_filter_uses_enriched_dates(self):
        fake = self._fake_ydl([{"id": "old", "title": "o"}, {"id": "new", "title": "n"}],
                              {"old": "20240101", "new": "20260601"})
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            from ai4research.fetch.clients import YtDlpChannelLister
            vids = YtDlpChannelLister().list_videos("ytsearch2:x", "2026-01-01", 2)
        self.assertEqual([v.video_id for v in vids], ["new"])           # 'old' (2024) excluded by since


class ResearchWrapperTest(unittest.TestCase):
    """The single-command wrapper chains fetch -> compile (#13). Both stages are mocked."""

    def test_chains_fetch_then_compile(self):
        from ai4research import research
        calls = {}
        with mock.patch("ai4research.fetch.cli.main", lambda argv: calls.setdefault("fetch", argv) and 0 or 0), \
             mock.patch("ai4research.cli.main", lambda argv: calls.setdefault("demo", argv) and 0 or 0), \
             tempfile.TemporaryDirectory() as tmp:
            rc = research.main(["my topic", "--out", tmp, "--model-runtime", "stub", "--copy-to", tmp])
        self.assertEqual(rc, 0)
        self.assertIn("--query", calls["fetch"])
        self.assertIn("my topic", calls["fetch"])
        self.assertNotIn("--model-runtime", calls["fetch"])      # stub -> heuristic planner, no codex flag
        self.assertEqual(calls["demo"][0], "demo")
        self.assertIn("--copy-to", calls["demo"])
        sp = calls["demo"][calls["demo"].index("--source-pack") + 1]
        self.assertTrue(sp.endswith("snap/source_containers.jsonl"))

    def test_aborts_when_fetch_fails(self):
        from ai4research import research
        demo_called = []
        with mock.patch("ai4research.fetch.cli.main", lambda argv: 2), \
             mock.patch("ai4research.cli.main", lambda argv: demo_called.append(argv) or 0), \
             tempfile.TemporaryDirectory() as tmp:
            rc = research.main(["t", "--out", tmp])
        self.assertEqual(rc, 2)                                  # fetch's exit code propagates
        self.assertEqual(demo_called, [])                        # compile never reached


if __name__ == "__main__":
    unittest.main()
