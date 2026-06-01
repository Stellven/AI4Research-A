"""§14.3 negative integration: a missing fixture plus an unsupported critical claim. The
critical claim is blocked specifically by CriticalClaimGate (not by ordinary support), no
final report is produced, a diagnostic report is exported, a repair task is written, and
the bundle is diagnostic_only.

To isolate CriticalClaimGate the injected claim has one supporting evidence row — enough to
satisfy ClaimSupportGate — but the contract requires two for critical claims, so only the
critical gate fires. The claim is also placed in section_claims, proving the *gate* (not
blueprint sequencing) is what keeps it out of the report. The deterministic spine never
invents criticality itself, so a gate is what must catch a critical claim.
"""
import tempfile
import unittest

from ai4research import ids
from ai4research.operators import OperatorRunner, build_pipeline
from ai4research.operators.gates import PreRenderQualityGateSuiteOperator
from ai4research.operators.render import HtmlRenderOperator, MarkdownReportCompileOperator
from tests import support

FINDINGS_HEADING = "Evidence-Backed Findings"


def _missing_fixture_channel() -> dict:
    return {
        "container_id": "C-YT-001", "source_pack_type": "youtube_channel",
        "container_locator": "https://www.youtube.com/@gap", "label": "Gap channel",
        "items": [{"item_id": "I-YT-MISS", "title": "Unstaged",
                   "item_locator": {"url": "https://www.youtube.com/watch?v=x",
                                    "local_fixture_path": "no/such/file.txt"}}],
    }


class NegativeRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx, self.work = support.stage(
            self._tmp.name, support.PROVING_TOPIC, [support.local_container(), _missing_fixture_channel()]
        )
        pipeline = build_pipeline()
        # Run O0-O12 (everything up to, but not including, the gate suite at index 13).
        OperatorRunner(pipeline[:13]).run(self.ctx, self.work)

        # Raise the critical-claim bar to 2 supporting evidence rows.
        contracts = self.work.read_rows("research_contracts")
        contracts[0]["critical_claim_policy"]["minimum_supporting_evidence"] = 2
        self.work.write_rows("research_contracts", contracts)

        # Inject a critical claim with ONE supporting evidence row: ClaimSupportGate passes
        # (>=1), CriticalClaimGate fails (1 < 2). Reuse a real evidence id so all FKs resolve.
        evidence_id = self.work.read_rows("evidence")[0]["evidence_id"]
        claim_id = ids.mint(self.ctx.run_id, "CLAIM", 9000)
        claims = self.work.read_rows("claims")
        claims.append({
            "claim_id": claim_id, "run_id": self.ctx.run_id, "claim_type": "risk_claim",
            "claim_text": "A critical security control is required.",
            "claim_scope": "within provided source set", "criticality": "critical",
            "status": "accepted", "confidence": None, "limitations": [],
        })
        self.work.write_rows("claims", claims)
        links = self.work.read_rows("claim_evidence")
        links.append({"claim_id": claim_id, "evidence_id": evidence_id, "role": "supporting"})
        self.work.write_rows("claim_evidence", links)

        # Place it in the findings section, so only the gate can keep it out of the report.
        findings_id = next(s["section_id"] for s in self.work.read_rows("report_sections")
                           if s["heading"] == FINDINGS_HEADING)
        section_claims = self.work.read_rows("section_claims")
        section_claims.append({"section_id": findings_id, "claim_id": claim_id})
        self.work.write_rows("section_claims", section_claims)

        PreRenderQualityGateSuiteOperator().run(self.ctx, self.work, pipeline)
        MarkdownReportCompileOperator().run(self.ctx, self.work, pipeline)
        HtmlRenderOperator().run(self.ctx, self.work, pipeline)
        from ai4research.finalize import finalize
        self.result = finalize(self.ctx, self.work)
        self.conn = support.store(self.ctx)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _gate_status(self, gate_id: str) -> str:
        return self.conn.execute("SELECT status FROM gate_results WHERE gate_id=?", (gate_id,)).fetchone()[0]

    def test_missing_fixture_recorded(self):
        row = self.conn.execute(
            "SELECT status, failure_code FROM acquisition_attempts WHERE selected_item_id LIKE '%I-YT-MISS'"
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["failure_code"], "local_fixture_missing")

    def test_only_critical_gate_blocks(self):
        # isolation: ordinary support passes; the critical gate alone fails
        self.assertEqual(self._gate_status("ClaimSupportGate"), "pass")
        self.assertEqual(self._gate_status("CriticalClaimGate"), "hard_fail")
        dossier = self.conn.execute("SELECT * FROM quality_dossier").fetchone()
        self.assertEqual(dossier["approved_for_report_rendering"], 0)

    def test_no_final_report_only_diagnostic(self):
        self.assertFalse((self.ctx.exports_dir / "final_report.md").exists())
        self.assertTrue((self.ctx.exports_dir / "diagnostic_report.md").exists())
        self.assertTrue((self.ctx.exports_dir / "diagnostic_report.html").exists())

    def test_repair_task_and_diagnostic_bundle(self):
        self.assertGreater(
            self.conn.execute("SELECT COUNT(*) FROM repair_tasks WHERE gate_id='CriticalClaimGate'").fetchone()[0], 0
        )
        statuses = {r[0] for r in self.conn.execute("SELECT DISTINCT bundle_status FROM bundle_artifacts").fetchall()}
        self.assertEqual(statuses, {"diagnostic_only"})
        self.assertEqual(self.result.status, "diagnostic_only")


if __name__ == "__main__":
    unittest.main()
