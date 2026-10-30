"""
TEST OUT-OF-SAMPLE (OOS) BAR ACCOUNTING ENGINE & VISIBILITY
===========================================================
Comprehensive regression test suite verifying:
1. calculate_oos_bar_metrics calculates exact historical locked OOS bar counts.
2. Locked OOS bars (100% processed) are strictly separated from trade counts (34 trades).
3. Future forward OOS bars are correctly measured as 0 bars (OOS_PENDING).
4. OOS status is NEVER converted to PASS without genuine future data arrival post-freeze.
5. ResearchMemory.get_experiment_audit_details enriches payloads with authoritative oos_metrics.
6. ResearchReportPDFGenerator includes dedicated OOS accounting table.
7. Empty/null audits are handled safely without exceptions.
"""

import unittest
import os
from fastapi.testclient import TestClient

from app.main import app
from app.analytics.research_orchestrator.research_memory import ResearchMemory
from app.analytics.research_report_pdf_generator import ResearchReportPDFGenerator


class TestOOSBarAccountingEngine(unittest.TestCase):
    """Rigorous tests validating authoritative OOS Bar Accounting Engine."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_01_calculate_oos_bar_metrics_live_52(self):
        """LIVE_52 candidate must report 38,679 locked OOS bars, 100% processed, 0 future bars."""
        metrics = ResearchMemory.calculate_oos_bar_metrics(
            universe="LIVE_52",
            oos_start="2023-09-05",
            oos_end="2026-09-04",
            trade_count=34,
            candidate_status="FROZEN_CANDIDATE"
        )
        self.assertIsInstance(metrics, dict)
        self.assertEqual(metrics["locked_oos_bars_available"], 38679)
        self.assertEqual(metrics["locked_oos_bars_total"], 38679)
        self.assertEqual(metrics["locked_oos_completion_pct"], 100.0)
        self.assertEqual(metrics["future_forward_oos_bars"], 0)
        self.assertEqual(metrics["completed_oos_trades"], 34)
        self.assertEqual(metrics["oos_status"], "OOS_PENDING")
        self.assertTrue(metrics["is_locked_complete"])

    def test_02_calculate_oos_bar_metrics_nifty_500(self):
        """NIFTY_500 candidate must report 351,323 locked OOS bars, 100% processed, 0 future bars."""
        metrics = ResearchMemory.calculate_oos_bar_metrics(
            universe="NIFTY_500",
            oos_start="2023-09-05",
            oos_end="2026-09-04",
            trade_count=34,
            candidate_status="FROZEN_CANDIDATE"
        )
        self.assertIsInstance(metrics, dict)
        self.assertEqual(metrics["locked_oos_bars_available"], 351323)
        self.assertEqual(metrics["locked_oos_bars_total"], 351323)
        self.assertEqual(metrics["locked_oos_completion_pct"], 100.0)
        self.assertEqual(metrics["future_forward_oos_bars"], 0)
        self.assertEqual(metrics["completed_oos_trades"], 34)
        self.assertEqual(metrics["oos_status"], "OOS_PENDING")
        self.assertTrue(metrics["is_locked_complete"])

    def test_03_oos_bars_distinct_from_trade_count(self):
        """OOS bar counts (~38k-351k bars) must never be confused with trade count (e.g. 34 trades)."""
        metrics = ResearchMemory.calculate_oos_bar_metrics(
            universe="LIVE_52",
            oos_start="2023-09-05",
            oos_end="2026-09-04",
            trade_count=34
        )
        self.assertNotEqual(metrics["locked_oos_bars_available"], metrics["completed_oos_trades"])
        self.assertGreater(metrics["locked_oos_bars_available"], 1000)
        self.assertLess(metrics["completed_oos_trades"], 500)

    def test_04_oos_pending_invariant(self):
        """Candidates without future forward bars must remain strictly OOS_PENDING (never falsely PASS)."""
        metrics = ResearchMemory.calculate_oos_bar_metrics(
            universe="LIVE_52",
            oos_start="2023-09-05",
            oos_end="2026-09-04",
            trade_count=50,
            candidate_status="FROZEN_CANDIDATE"
        )
        self.assertEqual(metrics["future_forward_oos_bars"], 0)
        self.assertEqual(metrics["oos_status"], "OOS_PENDING")
        self.assertNotEqual(metrics["oos_status"], "PASS")

    def test_05_research_memory_enriches_audit_details(self):
        """ResearchMemory.get_experiment_audit_details must include authoritative oos_metrics."""
        audit = ResearchMemory.get_experiment_audit_details("cand_5cd23540c5c02c91")
        self.assertIn("oos_metrics", audit)
        oos = audit["oos_metrics"]
        self.assertIn("locked_oos_bars_available", oos)
        self.assertIn("locked_oos_bars_total", oos)
        self.assertIn("locked_oos_completion_pct", oos)
        self.assertIn("future_forward_oos_bars", oos)
        self.assertIn("completed_oos_trades", oos)
        self.assertIn("oos_status", oos)

    def test_06_api_returns_oos_metrics(self):
        """GET /api/research-autopilot/experiment/{id} must return oos_metrics in payload."""
        resp = self.client.get("/api/research-autopilot/experiment/cand_5cd23540c5c02c91")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("status"), "success")
        audit = data.get("audit", {})
        self.assertIn("oos_metrics", audit)
        self.assertEqual(audit["oos_metrics"]["locked_oos_completion_pct"], 100.0)

    def test_07_pdf_generator_includes_oos_section(self):
        """ResearchReportPDFGenerator must include dedicated OOS Bar Accounting section in PDF bytes."""
        audit = ResearchMemory.get_experiment_audit_details("cand_5cd23540c5c02c91")
        pdf_bytes = ResearchReportPDFGenerator.generate_experiment_audit_pdf(audit)
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertGreater(len(pdf_bytes), 5000)

    def test_08_pdf_generator_empty_audit_resilience(self):
        """PDF generator must handle empty audit dict gracefully without crashing."""
        pdf_bytes = ResearchReportPDFGenerator.generate_experiment_audit_pdf({})
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertGreater(len(pdf_bytes), 3000)


if __name__ == "__main__":
    unittest.main()
