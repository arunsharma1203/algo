import unittest
import sqlite3
import os
import pandas as pd

from app.data.historical_data_layer import get_db_path
from app.api.ml_history import evaluate_ml_history
from app.analytics.quant_risk_engine import QuantRiskEngine

class TestTradeRecoveryAndQuantRisk(unittest.TestCase):
    """
    Unit tests for Trade History Recovery, Canonical Path Unification, and QuantRiskEngine.
    """

    def test_01_canonical_db_path_resolution(self):
        """get_db_path() must resolve to an absolute path pointing to backend/market_data.db."""
        path = get_db_path()
        self.assertTrue(os.path.isabs(path), f"Path {path} is not absolute.")
        self.assertTrue(path.endswith("backend/market_data.db"), f"Path {path} does not point to backend/market_data.db.")
        self.assertTrue(os.path.exists(path), f"Database file does not exist at {path}.")

    def test_02_all_recovered_trades_accessible(self):
        """evaluate_ml_history() must return all recovered trades (>= 32)."""
        trades = evaluate_ml_history(force_refresh=True)
        self.assertGreaterEqual(len(trades), 32, f"Expected >= 32 trades, got {len(trades)}")
        
        # Check presence of expected symbols
        tickers = {t.get("ticker") for t in trades}
        self.assertTrue(any("PAYTM" in t for t in tickers))
        self.assertTrue(any("ICICIAMC" in t for t in tickers))

    def test_03_quant_risk_engine_performance_metrics(self):
        """QuantRiskEngine must compute performance statistics without error."""
        trades = evaluate_ml_history()
        metrics = QuantRiskEngine.compute_performance_metrics(trades)

        self.assertEqual(metrics["status"], "HEALTHY")
        self.assertIn("win_rate_pct", metrics)
        self.assertIn("profit_factor", metrics)
        self.assertIn("net_pnl_pct", metrics)
        self.assertIn("gross_pnl_pct", metrics)
        self.assertIn("max_drawdown_pct", metrics)
        self.assertIn("sharpe_ratio", metrics)

    def test_04_quant_risk_regime_analysis(self):
        """QuantRiskEngine must break down trades into market regimes."""
        trades = evaluate_ml_history()
        regimes = QuantRiskEngine.compute_regime_analysis(trades)

        self.assertIn("regimes", regimes)
        self.assertGreater(regimes.get("total_regimes_tracked", 0), 0)

    def test_05_quant_risk_model_drift(self):
        """QuantRiskEngine must compute calibration drift and Brier score."""
        trades = evaluate_ml_history()
        drift = QuantRiskEngine.compute_model_drift(trades)

        self.assertIn(drift["health"], ["HEALTHY", "WATCH", "DECAYING", "CRITICAL", "INSUFFICIENT_DATA"])
        self.assertIn("brier_score", drift)
        self.assertIn("calibration_gap_pct", drift)

    def test_06_insufficient_data_handling(self):
        """QuantRiskEngine must return INSUFFICIENT DATA markers when sample size < 5."""
        tiny_trades = [
            {"status": "CLOSED", "profit_pct": 1.2, "outcome": "TARGET MET", "confidence": 75.0, "direction": "BULLISH"},
            {"status": "CLOSED", "profit_pct": -0.8, "outcome": "SL HIT", "confidence": 65.0, "direction": "BEARISH"}
        ]
        metrics = QuantRiskEngine.compute_performance_metrics(tiny_trades)
        self.assertFalse(metrics["statistically_significant"])
        self.assertEqual(metrics["sharpe_ratio"], "INSUFFICIENT DATA")

        drift = QuantRiskEngine.compute_model_drift(tiny_trades)
        self.assertEqual(drift["health"], "INSUFFICIENT_DATA")

    def test_07_database_boundary_isolation(self):
        """Verifies table separation: ml_trade_history vs forward_simulation_trades vs research_job_results."""
        conn = sqlite3.connect(get_db_path())
        cur = conn.cursor()
        
        cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='ml_trade_history'")
        self.assertEqual(cur.fetchone()[0], 1)

        cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='forward_simulation_trades'")
        self.assertEqual(cur.fetchone()[0], 1)

        cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='research_job_results'")
        self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

if __name__ == "__main__":
    unittest.main()
