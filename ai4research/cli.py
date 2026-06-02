"""Command-line interface. `ai4research demo --topic ... --source-pack <file>` runs the
Phase 0 spine over a source pack, renders the report, and loads the run into the store.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ids
from .finalize import FinalizeResult, finalize
from .operators import OperatorRunner, RunFailed, build_pipeline
from .runtime import RunContext
from .termstyle import Style
from .workfiles import WorkStore


def stage_source_pack(pack_path: Path, dest: Path) -> None:
    rows = []
    pack_dir = pack_path.parent
    for line in pack_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        for item in row.get("items", []):
            loc = item.get("item_locator")
            if not isinstance(loc, dict):
                continue
            for key in ("local_path", "local_fixture_path"):
                value = loc.get(key)
                if value:
                    path = Path(value)
                    loc[key] = str(path if path.is_absolute() else pack_dir / path)
        rows.append(row)
    dest.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")


def cmd_demo(args: argparse.Namespace) -> int:
    runs_dir = Path(args.runs_dir).resolve()
    ctx = RunContext(ids.new_run_id(), runs_dir)
    ctx.ensure_dirs()

    # Stage inputs: the topic and the user-supplied source pack.
    (ctx.input_dir / "topic.json").write_text(
        json.dumps({"topic": args.topic}, indent=2), encoding="utf-8"
    )
    run_config = {}
    if args.max_per_container is not None:
        run_config["max_items_per_container"] = args.max_per_container
    if args.domain_pack is not None:
        run_config["domain_pack"] = args.domain_pack
    if args.model_runtime is not None:
        run_config["model_runtime"] = args.model_runtime
    if run_config:
        (ctx.input_dir / "run_config.json").write_text(json.dumps(run_config, indent=2), encoding="utf-8")
    stage_source_pack(Path(args.source_pack).resolve(), ctx.input_dir / "source_containers.jsonl")

    work = WorkStore(ctx)
    rc = 0
    try:
        OperatorRunner(build_pipeline()).run(ctx, work)
    except RunFailed as exc:
        rc = 1
        print(f"[operator failed] {exc}", file=sys.stderr)

    # Persist, closeout, bundle (runs for finalized, diagnostic-only, and failed runs).
    result = finalize(ctx, work)
    if result.status == "failed":
        rc = 1
    _print_summary(ctx, result, work)
    return rc


def _banner(s: Style) -> str:
    title = "ai4research  ·  deterministic research compiler"
    width = len(title) + 2
    lines = ["╭" + "─" * width + "╮", "│ " + title + " │", "╰" + "─" * width + "╯"]
    return "\n".join("  " + s(line, "cyan") for line in lines)


def _print_summary(ctx: RunContext, result: FinalizeResult, work: WorkStore) -> None:
    """A compact scorecard: terminal status, what the run produced, the provenance split
    (youtube timestamps vs github line anchors), and the gate rollup — the auditable shape
    of the report at a glance. Plain text when stdout is not a TTY (see termstyle)."""
    s = Style()
    docs = work.read_documents()
    evidence = work.read_rows("evidence")
    claims = work.read_rows("claims")
    citations = [c for c in work.read_rows("citations") if c.get("url")]
    gates = work.read_rows("gate_results")
    topic = (work.read_rows("runs") or [{}])[0].get("topic")
    accepted = sum(1 for c in claims if c.get("status") == "accepted")
    yt = sum(1 for c in citations if "&t=" in c["url"])
    gh = sum(1 for c in citations if "#L" in c["url"])
    gp = sum(1 for g in gates if g.get("status") == "pass")
    gw = sum(1 for g in gates if g.get("status") == "warning")
    gf = sum(1 for g in gates if g.get("status") == "hard_fail")
    color = {"finalized": "green", "diagnostic_only": "yellow", "failed": "red"}.get(result.status, "")
    dot = s("  ·  ", "dim")

    def row(label: str, value: str) -> None:
        print(f"  {s(label.ljust(10), 'dim')}{value}")

    if s.enabled:
        print(_banner(s))
    print()
    row("run", ctx.run_id)
    row("status", s(f"{s.glyph(result.status)} {result.status}", color, "bold"))
    if topic:
        row("topic", topic)
    if result.persist_error:
        row("persist", s(result.persist_error, "red"))
    print()
    row("sources", f"{len(docs)} documents{dot}{len(evidence)} evidence cards")
    row("claims", f"{accepted} accepted{s(' / ', 'dim')}{len(claims)}")
    deeplinks = f"{len(citations)} deep links"
    if citations:
        deeplinks += "   " + f"{s.glyph('youtube')} {yt} youtube{dot}{s.glyph('github')} {gh} github"
    row("citations", deeplinks)
    row("gates", dot.join([
        s(f"{s.glyph('pass')} {gp} pass", "green"),
        s(f"{s.glyph('warning')} {gw} warn", "yellow"),
        s(f"{s.glyph('hard_fail')} {gf} fail", "red" if gf else "dim"),
    ]))
    print()
    row("report", result.report_path or "(none)")
    row("store", str(ctx.db_path) if result.persisted else "(not persisted)")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai4research", description="Pipeline A — Phase 0")
    sub = parser.add_subparsers(dest="cmd", required=True)

    demo = sub.add_parser("demo", help="run the Phase 0 spine over a source pack")
    demo.add_argument("--topic", required=True)
    demo.add_argument("--source-pack", required=True, help="path to a source_containers.jsonl")
    demo.add_argument("--runs-dir", default="runs")
    demo.add_argument("--max-per-container", type=int, default=None)
    demo.add_argument("--domain-pack", default=None)
    demo.add_argument("--model-runtime", default=None, choices=["stub", "codex"])
    demo.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
