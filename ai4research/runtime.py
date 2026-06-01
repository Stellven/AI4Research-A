"""Run context: the per-run directory layout (design §5).

    runs/
      ai4research.db                 # durable SQLite store (all runs)
      <run_id>/
        input/   source_containers.jsonl, topic.json, fixtures/
        work/    one JSON/JSONL per Section 7 table (working state during the run)
        exports/ final_report.* / diagnostic_report.* / bundle_manifest.json (later milestones)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunContext:
    run_id: str
    runs_dir: Path

    @property
    def db_path(self) -> Path:
        return self.runs_dir / "ai4research.db"

    @property
    def run_dir(self) -> Path:
        return self.runs_dir / self.run_id

    @property
    def input_dir(self) -> Path:
        return self.run_dir / "input"

    @property
    def work_dir(self) -> Path:
        return self.run_dir / "work"

    @property
    def work_documents_dir(self) -> Path:
        return self.work_dir / "documents"

    @property
    def exports_dir(self) -> Path:
        return self.run_dir / "exports"

    def ensure_dirs(self) -> None:
        for d in (self.input_dir, self.work_dir, self.work_documents_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)
