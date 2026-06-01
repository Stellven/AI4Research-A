"""SQLite store. The durable record (design §6). During a run operators write work
files; `persist_run` loads them into the store in one transaction. Foreign keys are the
final integrity check — a broken reference fails the load. The finalize steps (closeout,
bundle) append their rows after the bulk load via `insert`.

The store schema lives in schema.sql, generated from the frozen design doc.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .runtime import RunContext
from .workfiles import WorkStore

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Columns whose values are stored as JSON TEXT, per table.
JSON_COLS: dict[str, set[str]] = {
    "operator_specs": {"input_schemas", "output_schemas"},
    "optimizer_decisions": {"alternatives_considered"},
    "domain_packs": {"source_families", "vocabulary", "scoring_weights",
                     "required_sections", "required_gates", "question_template"},
    "research_contracts": {"source_policy", "required_dimensions", "critical_claim_policy", "deliverables"},
    "selected_source_items": {"item_locator", "provider_metadata"},
    "acquisition_attempts": {"input_locator"},
    "documents": {"normalization", "provider_metadata"},
    "evidence": {"limitations"},
    "claims": {"limitations", "derivation"},
    "entities": {"synonyms", "domain_tags"},
    "figures": {"spec", "grounded_claim_ids"},
    "gate_results": {"checked_tables", "issues", "metrics"},
    "quality_dossier": {"coverage"},
    "operator_invocations": {"metrics"},
}

# FK-safe load order: parents before children. `documents` is read from per-doc JSON.
# `bundle_artifacts` is written by the bundle step after the bulk load.
_PERSIST_ORDER: list[str] = [
    "runs", "operator_specs", "physical_plan_nodes", "physical_plan_edges",
    "optimizer_decisions", "domain_packs", "research_contracts", "question_graph_nodes", "question_graph_edges",
    "source_containers", "selected_source_items", "acquisition_attempts", "documents",
    "spans", "evidence", "claims", "entities", "claim_entities", "claim_evidence", "claim_edges", "citations",
    "report_sections", "figures", "section_claims", "section_citations",
    "gate_results", "quality_dossier", "repair_tasks", "operator_invocations", "artifact_exports",
]

# Global registry (PK not run-scoped); the same operators recur every run, so upsert.
_OR_IGNORE: set[str] = {"operator_specs", "domain_packs"}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply schema.sql once (idempotent across runs sharing one DB)."""
    have = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
    ).fetchone()
    if have:
        return
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '0.1.0')"
    )
    conn.commit()


def _encode(row: dict[str, Any], json_cols: set[str]) -> dict[str, Any]:
    out = {}
    for k, v in row.items():
        if k in json_cols and v is not None and not isinstance(v, str):
            out[k] = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            out[k] = int(v)
        else:
            out[k] = v
    return out


def _insert(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]], json_cols: set[str]) -> int:
    verb = "INSERT OR IGNORE INTO" if table in _OR_IGNORE else "INSERT INTO"
    n = 0
    for row in rows:
        enc = _encode(row, json_cols)
        cols = list(enc.keys())
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"{verb} {table} ({', '.join(cols)}) VALUES ({placeholders})",
            [enc[c] for c in cols],
        )
        n += 1
    return n


def insert(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> int:
    """Insert rows into an already-open store (used by the finalize steps after the bulk
    load). Caller controls the transaction."""
    return _insert(conn, table, rows, JSON_COLS.get(table, set()))


def persist_run(ctx: RunContext, work: WorkStore) -> dict[str, int]:
    """Load every working file for the run into the SQLite store, in one transaction.

    Returns per-table row counts. Raises sqlite3.IntegrityError (rolls back) if any
    foreign-key reference is unresolved — the load is the final integrity check.
    """
    counts: dict[str, int] = {}
    plan_nodes: set[str] = set()
    conn = connect(ctx.db_path)
    try:
        init_schema(conn)
        with conn:  # one transaction; rolls back on any error
            for table in _PERSIST_ORDER:
                rows = work.read_documents() if table == "documents" else work.read_rows(table)
                if table == "physical_plan_nodes":
                    plan_nodes = {r["node_id"] for r in rows}
                elif table == "operator_invocations":
                    # A run that fails before O3 has invocation rows but no plan nodes yet.
                    # node_id is nullable; null the orphans so the failed run still loads.
                    for r in rows:
                        if r.get("node_id") not in plan_nodes:
                            r["node_id"] = None
                if rows:
                    counts[table] = _insert(conn, table, rows, JSON_COLS.get(table, set()))
    finally:
        conn.close()
    return counts
