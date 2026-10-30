import unittest
import sqlite3
from fastapi.testclient import TestClient
from app.main import app
from app.analytics.synthetic_pipeline_tester import SyntheticPipelineTester
from app.data.historical_data_layer import get_db_path
from app.analytics.kelly_sizer import get_portfolio_heat_status

class TestPipelineDiagnosticTelemetry(unittest.TestCase):
    """
    Tests the additive telemetry and safety invariants of the synthetic pipeline diagnostic system.
    """

    def setUp(self):
        self.client = TestClient(app)

    def test_diagnostic_intraday_structure(self):
        """Verifies intraday synthetic diagnostic contains all rich additive telemetry fields."""
        diag = SyntheticPipelineTester.run_diagnostic(timeframe="intraday", symbol="TESTSTOCK.NS")
        self.assertTrue(diag.get("overall_pass"))
        self.assertEqual(diag.get("pipeline_status"), "HEALTHY")
        self.assertEqual(diag.get("eligibility"), "ELIGIBLE")
        self.assertEqual(diag.get("timeframe"), "INTRADAY")
        self.assertEqual(diag.get("symbol"), "TESTSTOCK.NS")
        self.assertIn("duration_ms", diag)
        self.assertIn("execution_time_sec", diag)
        self.assertIn("risk_heat_pct", diag)
        self.assertEqual(diag.get("risk_heat_pct"), 0.0)

        stages = diag.get("stages", [])
        self.assertGreaterEqual(len(stages), 10)

        # Check that stages have structured metadata
        for st in stages:
            self.assertIn("stage_id", st)
            self.assertIn("stage", st)
            self.assertIn("status", st)
            self.assertIn(st["status"], ["PASS", "WARNING", "REJECTED", "NOT_RUN", "FAIL"])
            self.assertIn("purpose", st)
            self.assertIn("source", st)
            self.assertIn("duration_ms", st)

    def test_diagnostic_swing_disallowed_short_is_not_run(self):
        """
        Verifies that when cash equity short is disallowed in Swing, downstream stages are
        marked NOT_RUN rather than FAIL, preserving PIPELINE HEALTHY while marking trade NOT ELIGIBLE.
        """
        diag = SyntheticPipelineTester.run_diagnostic(timeframe="swing", symbol="TESTSTOCK.NS")
        self.assertTrue(diag.get("overall_pass"))
        self.assertEqual(diag.get("pipeline_status"), "HEALTHY")
        self.assertEqual(diag.get("eligibility"), "REJECTED")

        # Find decision gate stage
        stages_by_id = {st["stage_id"]: st for st in diag.get("stages", [])}
        self.assertIn("final_decision_gate", stages_by_id)
        self.assertEqual(stages_by_id["final_decision_gate"]["rejection_reason"], 
                         "SWING_CASH_SHORT_DISALLOWED: Cash-equity swing short disallowed (Indian equity multi-day positions must be LONG)")

        # Downstream stages should be NOT_RUN
        self.assertIn("meta_learner_consensus", stages_by_id)
        self.assertEqual(stages_by_id["meta_learner_consensus"]["status"], "NOT_RUN")
        self.assertIn("conviction_calibration", stages_by_id)
        self.assertEqual(stages_by_id["conviction_calibration"]["status"], "NOT_RUN")

    def test_teststock_zero_ml_history_contamination(self):
        """Verifies TESTSTOCK.NS never writes rows into production ml_trade_history."""
        conn = sqlite3.connect(get_db_path())
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM ml_trade_history WHERE ticker = ?", ("TESTSTOCK.NS",))
        count_before = cursor.fetchone()[0]

        # Run multiple diagnostics across intraday and swing
        SyntheticPipelineTester.run_diagnostic(timeframe="intraday", symbol="TESTSTOCK.NS")
        SyntheticPipelineTester.run_diagnostic(timeframe="swing", symbol="TESTSTOCK.NS")

        cursor.execute("SELECT count(*) FROM ml_trade_history WHERE ticker = ?", ("TESTSTOCK.NS",))
        count_after = cursor.fetchone()[0]
        conn.close()

        self.assertEqual(count_before, 0)
        self.assertEqual(count_after, 0)

    def test_teststock_zero_portfolio_heat_drag(self):
        """Verifies running TESTSTOCK diagnostics contributes 0.00% to portfolio heat."""
        heat_before = get_portfolio_heat_status()
        SyntheticPipelineTester.run_diagnostic(timeframe="intraday", symbol="TESTSTOCK.NS")
        heat_after = get_portfolio_heat_status()

        self.assertEqual(heat_before.get("current_portfolio_heat_pct"), heat_after.get("current_portfolio_heat_pct"))
        self.assertEqual(heat_after.get("status"), "NORMAL")

    def test_fastapi_pipeline_endpoint(self):
        """Verifies the /api/system/pipeline-test endpoint handles timeframe parameter."""
        res_intra = self.client.post("/api/system/pipeline-test?timeframe=intraday")
        self.assertEqual(res_intra.status_code, 200)
        d_intra = res_intra.json()
        self.assertTrue(d_intra.get("overall_pass"))
        self.assertEqual(d_intra.get("timeframe"), "INTRADAY")

        res_swing = self.client.post("/api/system/pipeline-test?timeframe=swing")
        self.assertEqual(res_swing.status_code, 200)
        d_swing = res_swing.json()
        self.assertTrue(d_swing.get("overall_pass"))
        self.assertEqual(d_swing.get("timeframe"), "SWING")

if __name__ == "__main__":
    unittest.main()
