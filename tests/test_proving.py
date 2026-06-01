"""§14.2 proving run: multiple source families flow through the same spine to a finalized
report; every container/item appears in coverage (including a fixture-less channel); the
full traceability chain reconstructs per family.
"""
import tempfile
import unittest

from tests import support


def _missing_fixture_channel() -> dict:
    return {
        "container_id": "C-YT-GAP", "source_pack_type": "youtube_channel",
        "container_locator": "https://www.youtube.com/@gap", "label": "Channel with no fixture",
        "items": [{"item_id": "I-YT-GAP", "title": "Unstaged video",
                   "item_locator": {"url": "https://www.youtube.com/watch?v=missing",
                                    "local_fixture_path": "does/not/exist.txt"}}],
    }


class ProvingRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        containers = [support.local_container(), support.youtube_container(),
                      support.github_container(), _missing_fixture_channel()]
        self.ctx, work = support.stage(self._tmp.name, support.PROVING_TOPIC, containers)
        self.result = support.run_full(self.ctx, work)
        self.conn = support.store(self.ctx)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_source_agnostic_execution_finalizes(self):
        self.assertEqual(self.result.status, "finalized")
        self.assertTrue((self.ctx.exports_dir / "final_report.html").exists())

    def test_preset_coverage_lists_every_container(self):
        report = (self.ctx.exports_dir / "final_report.md").read_text()
        for label in ("Local notes", "Governance channel", "Open Skills Framework", "Channel with no fixture"):
            self.assertIn(label, report)
        # the unstaged video is surfaced as a failed acquisition (coverage gap), not dropped
        self.assertEqual(
            self.conn.execute("SELECT failure_code FROM acquisition_attempts "
                              "WHERE selected_item_id LIKE '%I-YT-GAP'").fetchone()[0],
            "local_fixture_missing",
        )

    def test_traceability_reconstructs_per_family(self):
        rows = self.conn.execute(
            "SELECT DISTINCT d.document_kind FROM claims cl "
            "JOIN claim_evidence ce ON ce.claim_id = cl.claim_id "
            "JOIN evidence e ON e.evidence_id = ce.evidence_id "
            "JOIN spans s ON s.span_id = e.span_id "
            "JOIN documents d ON d.document_id = e.document_id "
            "JOIN selected_source_items i ON i.selected_item_id = e.selected_item_id "
            "JOIN source_containers c ON c.container_id = i.container_id "
            "WHERE cl.status = 'accepted'"
        ).fetchall()
        kinds = {r[0] for r in rows}
        self.assertEqual({"local_document", "youtube_transcript", "github_document"}, kinds)

    def test_final_sentence_traces_to_a_claim(self):
        # §14.2: the rendered report sentence reconstructs to an accepted claim. The findings
        # section renders accepted claim_text verbatim, so a claim_text must appear in the report.
        report = (self.ctx.exports_dir / "final_report.md").read_text()
        accepted = [r[0] for r in self.conn.execute(
            "SELECT claim_text FROM claims WHERE status='accepted'").fetchall()]
        self.assertTrue(any(text in report for text in accepted))

    def test_gates_pass_with_only_warnings(self):
        dossier = self.conn.execute("SELECT * FROM quality_dossier").fetchone()
        self.assertEqual(dossier["blocking_gate_failures"], 0)
        self.assertEqual(dossier["approved_for_report_rendering"], 1)


if __name__ == "__main__":
    unittest.main()
