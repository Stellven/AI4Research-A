"""CLI for the fetch tool: `python -m ai4research.fetch --channels … --repos … --out <dir>`.

Wires the real network clients into `run_fetch`, then the produced pack is fed to the
compiler: `ai4research demo --source-pack <out>/source_containers.jsonl
--domain-pack youtube_github_research`.
"""
from __future__ import annotations

import argparse
import sys

from .build import run_fetch
from .clients import FetchDependencyError


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ai4research-fetch",
                                description="Fetch YouTube channels + GitHub repos into a source pack")
    p.add_argument("--channels", nargs="*", default=[], help="YouTube channel URLs")
    p.add_argument("--repos", nargs="*", default=[], help="GitHub repo URLs")
    p.add_argument("--since", default=None, help="ISO date (YYYY-MM-DD) freshness floor")
    p.add_argument("--max-per-channel", type=int, default=None)
    p.add_argument("--out", required=True, help="output directory for the source pack + fixtures")
    args = p.parse_args(argv)

    if not args.channels and not args.repos:
        print("nothing to fetch: pass --channels and/or --repos", file=sys.stderr)
        return 2

    try:
        summary = run_fetch(args.out, args.channels, args.repos,
                            since=args.since, max_per_channel=args.max_per_channel)
    except FetchDependencyError as exc:
        print(f"missing fetch dependency: {exc}\nrun: pip install ai4research[fetch]", file=sys.stderr)
        return 1
    print(f"pack written: {args.out}/source_containers.jsonl")
    for k in ("channels", "videos", "transcripts", "video_gaps", "repos", "repo_gaps"):
        print(f"  {k}: {summary[k]}")
    for err in summary["errors"]:
        print(f"  ! {err}", file=sys.stderr)
    print("\nNext: python -m ai4research demo --topic '<topic>' "
          f"--source-pack {args.out}/source_containers.jsonl --domain-pack youtube_github_research")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
