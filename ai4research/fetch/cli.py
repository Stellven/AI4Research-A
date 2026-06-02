"""CLI for the fetch tool: `python -m ai4research.fetch --channels … --repos … --out <dir>`.

Wires the real network clients into `run_fetch`, then the produced pack is fed to the
compiler: `ai4research demo --source-pack <out>/source_containers.jsonl
--domain-pack youtube_github_research`.
"""
from __future__ import annotations

import argparse
import sys

from ..termstyle import Style
from .build import run_fetch
from .clients import FetchDependencyError


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ai4research-fetch",
                                description="Fetch YouTube channels + GitHub repos into a source pack")
    p.add_argument("--query", default=None, help="discover sources by topic (GitHub repo search + YouTube search)")
    p.add_argument("--channels", nargs="*", default=[], help="explicit YouTube channel URLs")
    p.add_argument("--repos", nargs="*", default=[], help="explicit GitHub repo URLs")
    p.add_argument("--since", default=None, help="ISO date (YYYY-MM-DD) freshness floor")
    p.add_argument("--max-per-channel", type=int, default=None)
    p.add_argument("--max-repos", type=int, default=5, help="repos to discover per --query")
    p.add_argument("--max-videos", type=int, default=5, help="videos to discover per --query")
    p.add_argument("--model-runtime", default=None, choices=["codex"],
                   help="use an LLM to plan discovery queries (default: deterministic keyword heuristic)")
    p.add_argument("--out", required=True, help="output directory for the source pack + fixtures")
    args = p.parse_args(argv)

    channels, repos = list(args.channels), list(args.repos)
    try:
        if args.query:
            from .clients import ApiGitHubClient
            from .query import plan_queries
            runtime = None
            if args.model_runtime:
                from ..model_runtime import ModelRuntimeError, get_runtime
                try:
                    runtime = get_runtime(args.model_runtime)
                except ModelRuntimeError as exc:
                    print(f"query planner unavailable ({exc}); using heuristic", file=sys.stderr)
            plan = plan_queries(args.query, runtime)
            print(f"query plan ({plan['source']}): github={plan['github']!r} · youtube={plan['youtube']!r}")
            try:
                found = ApiGitHubClient().search_repos(plan["github"], args.max_repos)
                repos += found
                print(f"github: discovered {len(found)} repos")
            except FetchDependencyError:
                raise
            except Exception as exc:  # noqa: BLE001 - a failed GitHub search must not abort the YouTube search
                print(f"github search failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            channels.append(f"ytsearch{args.max_videos}:{plan['youtube']}")   # yt-dlp search, no API key

        if not channels and not repos:
            print("nothing to fetch: pass --query, --channels, and/or --repos", file=sys.stderr)
            return 2

        summary = run_fetch(args.out, channels, repos,
                            since=args.since, max_per_channel=args.max_per_channel)
    except FetchDependencyError as exc:
        print(f"missing fetch dependency: {exc}\nrun: pip install ai4research[fetch]", file=sys.stderr)
        return 1
    _print_fetch_summary(args.out, args.query, summary)
    return 0


def _print_fetch_summary(out: str, query: str | None, summary: dict) -> None:
    """Compact source-pack summary: what was gathered and — importantly — what was missed
    (coverage gaps / errors are highlighted, never buried). Plain text off a TTY."""
    s = Style()
    pack = f"{out}/source_containers.jsonl"
    dot = s(" · ", "dim")

    def row(label: str, value: str) -> None:
        print(f"  {s(label.ljust(10), 'dim')}{value}")

    def gaps(n: int) -> str:
        return s(f"  ({n} gaps)", "yellow") if n else ""

    if s.enabled:
        print("\n  " + s("ai4research-fetch", "cyan", "bold"))
    print()
    if query:
        row("query", query)
    row("pack", s(f"{s.glyph('pass')} ", "green") + pack)
    row("youtube", f"{summary['channels']} sources{dot}{summary['videos']} videos"
                   f"{dot}{summary['transcripts']} transcripts" + gaps(summary["video_gaps"]))
    row("github", f"{summary['repos']} repos" + gaps(summary["repo_gaps"]))
    for err in summary["errors"]:
        print("  " + s(f"{s.glyph('warning')} {err}", "yellow"), file=sys.stderr)
    print()
    row("next", f"ai4research demo --topic '<topic>' --source-pack {pack} "
                "--domain-pack youtube_github_research")


if __name__ == "__main__":
    raise SystemExit(main())
