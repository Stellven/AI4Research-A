"""Full-spine green run on a single local .md: the source-agnostic spine flows O0-O14 + G
+ finalize to a persisted, finalized run with rendered reports.
"""
import tempfile
import unittest
from pathlib import Path

from ai4research.operators import OperatorRunner, build_pipeline
from ai4research.operators import RunFailed
from tests import support


class SpineTest(unittest.TestCase):
    def test_local_only_full_run_finalizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            invocations = support.run_pipeline(ctx, work)
            self.assertEqual(len(invocations), 16)
            self.assertTrue(all(i["status"] == "success" for i in invocations))

            from ai4research.finalize import finalize
            result = finalize(ctx, work)
            self.assertEqual(result.status, "finalized")
            self.assertTrue(result.persisted)

            # exports exist
            self.assertTrue((ctx.exports_dir / "final_report.md").exists())
            self.assertTrue((ctx.exports_dir / "final_report.html").exists())
            self.assertTrue((ctx.exports_dir / "bundle_manifest.json").exists())

            conn = support.store(ctx)
            try:
                doc = conn.execute("SELECT raw_text, normalized_text, content_hash FROM documents").fetchone()
                self.assertIn("Skills governance", doc["raw_text"])
                self.assertTrue(doc["normalized_text"])           # O7 set it
                self.assertTrue(doc["content_hash"].startswith("sha256:"))
                self.assertGreater(support.count(conn, "spans"), 0)
                self.assertGreater(support.count(conn, "evidence"), 0)
                self.assertGreater(support.count(conn, "claims"), 0)
                self.assertGreater(support.count(conn, "citations"), 0)
                self.assertEqual(support.count(conn, "report_sections"), 7)
                self.assertEqual(conn.execute("SELECT status FROM runs").fetchone()[0], "finalized")
                # every accepted claim has supporting evidence
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM claims c WHERE c.status='accepted' "
                                 "AND NOT EXISTS (SELECT 1 FROM claim_evidence ce "
                                 "WHERE ce.claim_id=c.claim_id AND ce.role='supporting')").fetchone()[0],
                    0,
                )
            finally:
                conn.close()

    def test_missing_fixture_is_a_recorded_failed_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            container = {
                "container_id": "C-DOC-001", "source_pack_type": "local_document_set", "label": "Missing",
                "items": [{"item_id": "I-1", "item_locator": {"local_path": str(Path(tmp) / "nope.md")}}],
            }
            ctx, work = support.stage(tmp, "t", [container])
            result = support.run_full(ctx, work)
            self.assertEqual(result.status, "diagnostic_only")
            self.assertTrue((ctx.exports_dir / "diagnostic_report.md").exists())
            self.assertFalse((ctx.exports_dir / "final_report.md").exists())
            conn = support.store(ctx)
            try:
                row = conn.execute("SELECT status, failure_code FROM acquisition_attempts").fetchone()
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["failure_code"], "local_fixture_missing")
                self.assertEqual(support.count(conn, "documents"), 0)
            finally:
                conn.close()

    def test_directory_path_is_recorded_failed_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "source_dir"
            source_dir.mkdir()
            container = {
                "container_id": "C-DOC-001", "source_pack_type": "local_document_set", "label": "Directory",
                "items": [{"item_id": "I-DIR", "item_locator": {"local_path": str(source_dir)}}],
            }
            ctx, work = support.stage(tmp, "t", [container])
            result = support.run_full(ctx, work)
            self.assertEqual(result.status, "diagnostic_only")
            conn = support.store(ctx)
            try:
                self.assertEqual(support.count(conn, "runs"), 1)
                row = conn.execute("SELECT status, failure_code FROM acquisition_attempts").fetchone()
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["failure_code"], "not_a_file")
            finally:
                conn.close()

    def test_partial_batch_failure_keeps_good_document_and_failed_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "source_dir"
            source_dir.mkdir()
            container = {
                "container_id": "C-DOC-001", "source_pack_type": "local_document_set", "label": "Mixed",
                "items": [
                    {"item_id": "I-GOOD", "title": "Good",
                     "item_locator": {"inline_text": "Skills governance needs evidence and standards."}},
                    {"item_id": "I-BAD", "item_locator": {"local_path": str(source_dir)}},
                ],
            }
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [container])
            result = support.run_full(ctx, work)
            self.assertTrue(result.persisted)
            conn = support.store(ctx)
            try:
                self.assertEqual(support.count(conn, "runs"), 1)
                self.assertEqual(support.count(conn, "documents"), 1)
                row = conn.execute(
                    "SELECT status, failure_code FROM acquisition_attempts "
                    "WHERE selected_item_id LIKE '%I-BAD'"
                ).fetchone()
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["failure_code"], "not_a_file")
            finally:
                conn.close()

    def test_zero_acquired_run_is_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            container = {
                "container_id": "C-DOC-001", "source_pack_type": "local_document_set", "label": "Missing",
                "items": [
                    {"item_id": "I-MISS-1", "item_locator": {"local_path": str(Path(tmp) / "missing1.md")}},
                    {"item_id": "I-MISS-2", "item_locator": {"local_path": str(Path(tmp) / "missing2.md")}},
                ],
            }
            ctx, work = support.stage(tmp, "t", [container])
            result = support.run_full(ctx, work)
            self.assertEqual(result.status, "diagnostic_only")
            self.assertTrue((ctx.exports_dir / "diagnostic_report.md").exists())
            self.assertFalse((ctx.exports_dir / "final_report.md").exists())

    def test_freshness_top_n_is_deterministic(self):
        def run_once(tmp):
            container = {
                "container_id": "C-DOC-001", "source_pack_type": "local_document_set",
                "items": [
                    {"item_id": "I-OLD", "published_at": "2024-01-01",
                     "item_locator": {"inline_text": "old item with enough evidence text"}},
                    {"item_id": "I-NEW", "published_at": "2026-05-01",
                     "item_locator": {"inline_text": "new item with enough evidence text"}},
                    {"item_id": "I-MID", "published_at": "2025-06-01",
                     "item_locator": {"inline_text": "mid item with enough evidence text"}},
                    {"item_id": "I-NEWER", "published_at": "2026-05-02",
                     "item_locator": {"inline_text": "newer item with enough evidence text"}},
                ],
            }
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [container])
            (ctx.input_dir / "run_config.json").write_text('{"max_items_per_container": 2}', encoding="utf-8")
            support.run_pipeline(ctx, work)
            return [row["selected_item_id"].rsplit(".", 1)[1] for row in work.read_rows("selected_source_items")]

        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            selected1 = run_once(tmp1)
            selected2 = run_once(tmp2)
        self.assertEqual(selected1, ["I-NEWER", "I-NEW"])
        self.assertEqual(selected2, selected1)

    def test_disallowed_pack_type_rejected_by_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, work = support.stage(tmp, support.PROVING_TOPIC, [support.local_container()])
            pipeline = build_pipeline()
            OperatorRunner(pipeline[:4]).run(ctx, work)
            contracts = work.read_rows("research_contracts")
            contracts[0]["source_policy"]["allowed_source_pack_types"] = ["youtube_channel"]
            work.write_rows("research_contracts", contracts)
            with self.assertRaises(RunFailed) as cm:
                OperatorRunner(pipeline, plan=["SourceContainerLoadOperator"]).run(ctx, work)
            self.assertEqual(cm.exception.operator_name, "SourceContainerLoadOperator")


if __name__ == "__main__":
    unittest.main()
