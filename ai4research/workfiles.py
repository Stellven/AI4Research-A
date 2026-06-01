"""Work-file I/O. During a run, every operator writes its output as JSON/JSONL under
`<run_id>/work/` — one file per Section 7 table (table-named). `PersistRunToStore`
loads these into SQLite at the end. The same row dicts serialize to the file and the row.
"""
from __future__ import annotations

import json
from typing import Any

from .runtime import RunContext


class WorkStore:
    """Reads and writes the per-run working files (one JSONL per table)."""

    def __init__(self, ctx: RunContext):
        self.ctx = ctx

    # --- tabular working files (one JSONL per table) ---
    def write_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        path = self.ctx.work_dir / f"{table}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def append_row(self, table: str, row: dict[str, Any]) -> None:
        path = self.ctx.work_dir / f"{table}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def read_rows(self, table: str) -> list[dict[str, Any]]:
        path = self.ctx.work_dir / f"{table}.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def has(self, table: str) -> bool:
        return (self.ctx.work_dir / f"{table}.jsonl").exists()

    # --- documents: one JSON per document (text can be large) ---
    def write_document(self, document: dict[str, Any]) -> None:
        path = self.ctx.work_documents_dir / f"{document['document_id']}.json"
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    def read_documents(self) -> list[dict[str, Any]]:
        docs_dir = self.ctx.work_documents_dir
        if not docs_dir.exists():
            return []
        return [
            json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(docs_dir.glob("*.json"))
        ]
