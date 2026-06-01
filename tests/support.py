"""Shared test helpers: stage a run, run the pipeline + finalize, and build source-pack
containers that point at the committed proving fixtures by absolute path (cwd-independent).
"""
from __future__ import annotations

import json
from pathlib import Path

from ai4research import ids
from ai4research.finalize import finalize
from ai4research.operators import OperatorRunner, build_pipeline
from ai4research.runtime import RunContext
from ai4research.store import connect
from ai4research.workfiles import WorkStore

FIXTURES = Path(__file__).parent / "fixtures" / "proving"
PROVING_TOPIC = "latest technologies in skills governance"


def stage(tmp, topic: str, containers: list[dict]) -> tuple[RunContext, WorkStore]:
    ctx = RunContext(ids.new_run_id(), Path(tmp) / "runs")
    ctx.ensure_dirs()
    (ctx.input_dir / "topic.json").write_text(json.dumps({"topic": topic}), encoding="utf-8")
    (ctx.input_dir / "source_containers.jsonl").write_text(
        "\n".join(json.dumps(c) for c in containers), encoding="utf-8"
    )
    return ctx, WorkStore(ctx)


def run_pipeline(ctx: RunContext, work: WorkStore):
    return OperatorRunner(build_pipeline()).run(ctx, work)


def run_full(ctx: RunContext, work: WorkStore):
    run_pipeline(ctx, work)
    return finalize(ctx, work)


def store(ctx: RunContext):
    return connect(ctx.db_path)


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- container builders, one per source family, pointing at proving fixtures ---

def local_container() -> dict:
    return {
        "container_id": "C-DOC-001", "source_pack_type": "local_document_set", "label": "Local notes",
        "items": [{"item_id": "I-DOC-1", "title": "Skills governance notes",
                   "item_locator": {"local_path": str(FIXTURES / "docs/skills_governance.md")}}],
    }


def youtube_container() -> dict:
    return {
        "container_id": "C-YT-001", "source_pack_type": "youtube_channel",
        "container_locator": "https://www.youtube.com/@gov", "label": "Governance channel",
        "items": [
            {"item_id": "I-YT-1", "title": "Governance panel", "provider_metadata": {"video_id": "a"},
             "item_locator": {"url": "https://www.youtube.com/watch?v=a",
                              "local_fixture_path": str(FIXTURES / "transcripts/governance_panel.txt")}},
            {"item_id": "I-YT-2", "title": "Credentials talk", "provider_metadata": {"video_id": "b"},
             "item_locator": {"url": "https://www.youtube.com/watch?v=b",
                              "local_fixture_path": str(FIXTURES / "transcripts/credentials_talk.txt")}},
        ],
    }


def github_container() -> dict:
    return {
        "container_id": "C-GH-001", "source_pack_type": "github_repo",
        "container_locator": "https://github.com/x/skills", "label": "Open Skills Framework",
        "items": [{"item_id": "I-GH-1", "title": "README", "provider_metadata": {"path": "README.md"},
                   "item_locator": {"url": "https://github.com/x/skills/blob/main/README.md",
                                    "local_fixture_path": str(FIXTURES / "repos/skills_framework_readme.md")}}],
    }
