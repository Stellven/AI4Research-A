"""Finalization: O15 PersistRunToStore, F FinalCloseoutGate, B BundleExportOperator
(design §8.5, §11). These run after the operator runner completes the plan (O0-O14 + G).

Sequencing is intrinsic: the bulk load is the final integrity check, closeout passes only
if the load succeeded *and* exports are valid, and the bundle records the terminal status.
The bulk `persist_run` is one transaction; closeout/bundle append their rows in a second.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import ids, store
from .runtime import RunContext
from .workfiles import WorkStore

CLOSEOUT_VERSION = "0.1.0"

# Tables whose work files (if present) are recorded as bundle artifacts.
_BUNDLE_TABLES = list(store._PERSIST_ORDER)


@dataclass
class FinalizeResult:
    status: str               # finalized | diagnostic_only | failed
    persisted: bool
    counts: dict
    persist_error: str | None
    report_path: str | None


def finalize(ctx: RunContext, work: WorkStore) -> FinalizeResult:
    dossier = (work.read_rows("quality_dossier") or [{}])[0]
    approved = bool(dossier.get("approved_for_report_rendering"))

    # Provisional terminal status loaded into the store; closeout confirms it.
    provisional = "finalized" if approved else "diagnostic_only"
    _set_run_status(work, provisional)

    # O15 PersistRunToStore — the bulk one-transaction load (FKs = integrity check).
    persisted, counts, persist_error = True, {}, None
    try:
        counts = store.persist_run(ctx, work)
    except sqlite3.Error as exc:  # any load failure (FK, NOT NULL, schema) fails closed
        persisted, persist_error = False, str(exc)

    # F FinalCloseoutGate — decide the terminal status from persist + exports.
    report_name = "final_report.md" if approved else "diagnostic_report.md"
    report_path = ctx.exports_dir / report_name
    html_path = ctx.exports_dir / report_name.replace(".md", ".html")
    exports_ok = report_path.exists() and report_path.stat().st_size > 0 and html_path.exists()

    if not persisted:
        status, issues = "failed", [f"persist failed: {persist_error}"]
    elif not exports_ok:
        status, issues = "failed", ["expected exports missing or empty"]
    else:
        status, issues = (("finalized", []) if approved else ("diagnostic_only", []))

    closeout = {
        "gate_result_id": ids.mint(ctx.run_id, "CLOSE", 0),
        "run_id": ctx.run_id,
        "gate_id": "FinalCloseoutGate",
        "gate_version": CLOSEOUT_VERSION,
        "status": "pass" if status in ("finalized", "diagnostic_only") else "hard_fail",
        "severity": "blocking",
        "checked_tables": ["artifact_exports", "runs"],
        "issues": issues,
        "metrics": {"persisted": int(persisted), "approved": int(approved), "final_status": status},
        "created_at": ids.utc_now_iso(),
    }
    work.append_row("gate_results", closeout)
    _set_run_status(work, status)

    # B BundleExportOperator — bundle artifacts + manifest for every run.
    bundle_rows, manifest_export = _build_bundle(ctx, work, status)

    # Second transaction: reconcile the store with the finalized work files.
    if persisted:
        conn = store.connect(ctx.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE runs SET status = ?, completed_at = ? WHERE run_id = ?",
                    (status, ids.utc_now_iso(), ctx.run_id),
                )
                store.insert(conn, "gate_results", [closeout])
                store.insert(conn, "artifact_exports", [manifest_export])
                store.insert(conn, "bundle_artifacts", bundle_rows)
        finally:
            conn.close()

    return FinalizeResult(
        status=status, persisted=persisted, counts=counts,
        persist_error=persist_error,
        report_path=str(report_path) if report_path.exists() else None,
    )


def _set_run_status(work: WorkStore, status: str) -> None:
    runs = work.read_rows("runs")
    if runs:
        runs[0]["status"] = status
        runs[0]["completed_at"] = ids.utc_now_iso()
        work.write_rows("runs", runs)


def _build_bundle(ctx: RunContext, work: WorkStore, status: str):
    """Write bundle_artifacts work rows + exports/bundle_manifest.json, and return the
    rows plus the manifest's artifact_exports row for store insertion."""
    exports = work.read_rows("artifact_exports")
    rows = []
    for i, table in enumerate(_BUNDLE_TABLES):
        present = bool(work.read_documents()) if table == "documents" else work.has(table)
        if present:
            rows.append({
                "bundle_artifact_id": ids.mint(ctx.run_id, "BNDLT", i),
                "run_id": ctx.run_id, "kind": "table", "ref": table,
                "sha256": None, "bundle_status": status,
            })
    for i, exp in enumerate(exports):
        rows.append({
            "bundle_artifact_id": ids.mint(ctx.run_id, "BNDLE", i),
            "run_id": ctx.run_id, "kind": "export", "ref": exp["path"],
            "sha256": exp["sha256"], "bundle_status": status,
        })

    manifest = {
        "run_id": ctx.run_id,
        "status": status,
        "topic": (work.read_rows("runs") or [{}])[0].get("topic"),
        "exports": [{"path": e["path"], "kind": e["kind"], "sha256": e["sha256"]} for e in exports],
        "tables": [r["ref"] for r in rows if r["kind"] == "table"],
        "generated_at": ids.utc_now_iso(),
    }
    manifest_path = ctx.exports_dir / "bundle_manifest.json"
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2)
    manifest_path.write_text(manifest_text, encoding="utf-8")

    manifest_export = {
        "export_id": ids.mint(ctx.run_id, "EXP", 2),
        "run_id": ctx.run_id,
        "path": str(manifest_path.relative_to(ctx.run_dir)),
        "kind": "bundle_manifest",
        "sha256": ids.sha256_text(manifest_text),
        "created_by_invocation_id": None,
        "created_at": ids.utc_now_iso(),
    }
    work.append_row("artifact_exports", manifest_export)
    work.write_rows("bundle_artifacts", rows)
    return rows, manifest_export
