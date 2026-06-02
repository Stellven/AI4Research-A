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
    _print_summary(ctx, result)
    return rc


def _print_summary(ctx: RunContext, result: FinalizeResult) -> None:
    print(f"run_id : {ctx.run_id}")
    print(f"status : {result.status}")
    print(f"report : {result.report_path or '(none)'}")
    print(f"store  : {ctx.db_path if result.persisted else '(not persisted)'}")
    if result.persist_error:
        print(f"persist error: {result.persist_error}")
    print("loaded :")
    for table, n in result.counts.items():
        print(f"  {table}: {n}")


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
