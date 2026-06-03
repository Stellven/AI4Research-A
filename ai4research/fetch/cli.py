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
    perspective_tags: dict[str, str] = {}       # repo url / ytsearch channel -> stance (provenance)
    perspective_gaps: list[str] = []            # minority seats that landed nothing — recorded, not refilled
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
            plan = plan_queries(args.query, runtime)   # a LIST of perspective records (#12)
            print(f"query plan ({plan[0]['source']}): {len(plan)} perspective(s)")
            seen_repos: set = set()
            seen_yt: set = set()
            for persp, repo_budget, vid_budget in _allocate(plan, args.max_repos, args.max_videos):
                print(f"  · {persp['perspective']}: github={persp['github']!r} youtube={persp['youtube']!r}")
                if repo_budget > 0:
                    try:
                        found = ApiGitHubClient().search_repos(persp["github"], repo_budget)
                    except FetchDependencyError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - one perspective's failure must not abort the rest
                        print(f"github search failed ({persp['perspective']}): {type(exc).__name__}: {exc}", file=sys.stderr)
                        found = []
                    fresh = [u for u in found if u not in seen_repos]
                    for u in fresh:
                        seen_repos.add(u)
                        perspective_tags[u] = persp["perspective"]
                    repos += fresh
                    if not fresh and persp is not plan[0]:    # a minority github seat landed nothing
                        perspective_gaps.append(f"{persp['perspective']} github seat empty")
                if vid_budget > 0 and persp["youtube"].lower() not in seen_yt:
                    seen_yt.add(persp["youtube"].lower())
                    channel = f"ytsearch{vid_budget}:{persp['youtube']}"   # yt-dlp search, no API key
                    channels.append(channel)
                    perspective_tags[channel] = persp["perspective"]
            print(f"github: discovered {len(seen_repos)} repos across {len(plan)} perspective(s)")

        if not channels and not repos:
            print("nothing to fetch: pass --query, --channels, and/or --repos", file=sys.stderr)
            return 2

        summary = run_fetch(args.out, channels, repos, since=args.since,
                            max_per_channel=args.max_per_channel, perspectives=perspective_tags)
    except FetchDependencyError as exc:
        print(f"missing fetch dependency: {exc}\nrun: pip install ai4research[fetch]", file=sys.stderr)
        return 1
    if args.query:
        from collections import Counter
        summary["perspective_seats"] = dict(Counter(perspective_tags.values()))
        summary["perspective_gaps"] = perspective_gaps
    _print_fetch_summary(args.out, args.query, summary)
    return 0


def _allocate(plan: list, max_repos: int, max_videos: int) -> list:
    """Seat split for perspective-balanced sourcing: >=1 repo + >=1 video per perspective while the
    budget allows, remainder distributed to the NON-basic (minority) perspectives first so they are
    not crowded out. Totals stay bounded to max_repos / max_videos."""
    n = len(plan)
    if n == 0:
        return []

    def split(total: int) -> list:
        seats = [1 if i < total else 0 for i in range(n)]        # floor 1 each while budget allows
        order = (list(range(1, n)) + [0]) if n > 1 else [0]      # remainder to minority (idx >= 1) first
        i, rem = 0, max(0, total - sum(seats))
        while rem > 0:
            seats[order[i % len(order)]] += 1
            rem -= 1
            i += 1
        return seats

    repos, vids = split(max_repos), split(max_videos)
    return [(plan[i], repos[i], vids[i]) for i in range(n)]


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
    if summary.get("perspective_seats"):
        ratio = " / ".join(f"{k} {v}" for k, v in summary["perspective_seats"].items())
        row("balance", f"perspectives — {ratio}" + gaps(len(summary.get("perspective_gaps") or [])))
        for g in (summary.get("perspective_gaps") or []):
            print("  " + s(f"{s.glyph('warning')} {g}", "yellow"), file=sys.stderr)
    for err in summary["errors"]:
        print("  " + s(f"{s.glyph('warning')} {err}", "yellow"), file=sys.stderr)
    print()
    row("next", f"ai4research demo --topic '<topic>' --source-pack {pack} "
                "--domain-pack youtube_github_research")


if __name__ == "__main__":
    raise SystemExit(main())
