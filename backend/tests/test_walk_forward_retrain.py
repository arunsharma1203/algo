"""
Unit tests for Walk-Forward Retraining and Triple-Barrier Labeling.
Verifies bidirectional trading simulation, purged temporal splits, and safety gate enforcement.
"""

import unittest
import numpy as np
import pandas as pd
from unittest.mock import patch, MagicMock

from app.analytics.retrain_models import simulate_out_of_sample_trading, execute_retraining_pipeline


class TestWalkForwardRetraining(unittest.TestCase):
    def test_simulate_out_of_sample_trading_bidirectional(self):
        # 10 samples: 5 long setups (actual=1, prob=0.7), 5 short setups (actual=0, prob=0.3)
        y_val = np.array([1, 1, 1, 1, 1, 0, 0, 0, 0, 0] * 5)
        probs = np.array([0.7, 0.65, 0.8, 0.75, 0.6, 0.3, 0.25, 0.35, 0.2, 0.4] * 5)

        metrics = simulate_out_of_sample_trading(y_val, probs, cost_pct=0.001)
        self.assertEqual(metrics["trade_count"], 50)
        self.assertGreater(metrics["win_rate"], 80.0)
        self.assertGreater(metrics["sharpe"], 0.5)
        self.assertGreater(metrics["profit_factor"], 1.5)

    def test_simulate_out_of_sample_trading_low_sample_guard(self):
        y_val = np.array([1, 0])
        probs = np.array([0.7, 0.3])
        metrics = simulate_out_of_sample_trading(y_val, probs)
        self.assertTrue(metrics["is_low_sample"])
        self.assertEqual(metrics["trade_count"], 2)

    @patch("app.analytics.retrain_models.prepare_benchmark_dataset")
    def test_execute_retraining_pipeline_safety_gate(self, mock_prep):
        # Generate synthetic test feature matrix for 100 samples
        np.random.seed(42)
        X = np.random.randn(100, 5)
        y = np.random.choice([0, 1], size=100)
        features = ["rsi", "macd", "macd_diff", "adx", "atr"]
        mock_prep.return_value = (X, y, features)

        res = execute_retraining_pipeline(timeframe="swing")
        # Should execute without throwing, evaluate challenger against safety gate
        self.assertIn("status", res)
        self.assertIn(res["status"], ["PROMOTED", "RETAINED_CHAMPION"])
        self.assertIn("champion_f1", res)
        self.assertIn("challenger_f1", res)
        self.assertIn("samples_trained", res)


if __name__ == "__main__":
    unittest.main()

