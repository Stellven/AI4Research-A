"""Single-command research: a topic in, a compiled report out.

`ai4research-research "<topic>"` chains the two stages so a user never has to wire paths:
discover + fetch sources (the fetch tool) -> compile the dossier (the compiler). It is a thin
orchestrator over the two existing entry points and requires the optional [fetch] extra; the
compiler core still never imports the fetch tool.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="ai4research-research",
        description="Research a topic end to end: discover + fetch sources, then compile the report.")
    p.add_argument("topic", help="the research topic / question")
    p.add_argument("--model-runtime", default="codex", choices=["codex", "stub"],
                   help="codex (default) plans queries + synthesizes the dossier; stub = deterministic, no LLM")
    p.add_argument("--max-repos", type=int, default=5)
    p.add_argument("--max-videos", type=int, default=5)
    p.add_argument("--max-per-container", type=int, default=None)
    p.add_argument("--out", default=None, help="working directory (default: a fresh temp dir)")
    p.add_argument("--copy-to", default=None, help="copy the finalized report into this directory (e.g. Downloads)")
    args = p.parse_args(argv)

    work = Path(args.out).expanduser() if args.out else Path(tempfile.mkdtemp(prefix="research_"))
    snap, runs = work / "snap", work / "runs"
    print(f"researching: {args.topic!r}\n  workspace: {work}\n")

    # 1) discover + fetch (the fetch tool; codex plans the queries when selected)
    from .fetch.cli import main as fetch_main
    fetch_argv = ["--query", args.topic, "--max-repos", str(args.max_repos),
                  "--max-videos", str(args.max_videos), "--out", str(snap)]
    if args.model_runtime == "codex":
        fetch_argv += ["--model-runtime", "codex"]   # the fetch planner only offers codex; stub => heuristic
    rc = fetch_main(fetch_argv)
    if rc != 0:
        print("fetch failed; aborting.", file=sys.stderr)
        return rc

    # 2) compile the report (the deterministic compiler + optional LLM synthesis)
    from .cli import main as demo_main
    demo_argv = ["demo", "--topic", args.topic,
                 "--source-pack", str(snap / "source_containers.jsonl"),
                 "--domain-pack", "youtube_github_research",
                 "--model-runtime", args.model_runtime, "--runs-dir", str(runs)]
    if args.max_per_container is not None:
        demo_argv += ["--max-per-container", str(args.max_per_container)]
    if args.copy_to:
        demo_argv += ["--copy-to", args.copy_to]
    return demo_main(demo_argv)


if __name__ == "__main__":
    raise SystemExit(main())
