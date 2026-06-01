"""Full-spine green run on a single local .md: the source-agnostic spine flows O0-O14 + G
+ finalize to a persisted, finalized run with rendered reports.
"""
import tempfile
import unittest
from pathlib import Path

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
            # coverage threshold (minimum_source_count=1) is met, so rendering is NOT blocked
            self.assertEqual(result.status, "finalized")
            conn = support.store(ctx)
            try:
                row = conn.execute("SELECT status, failure_code FROM acquisition_attempts").fetchone()
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["failure_code"], "local_fixture_missing")
                self.assertEqual(support.count(conn, "documents"), 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
