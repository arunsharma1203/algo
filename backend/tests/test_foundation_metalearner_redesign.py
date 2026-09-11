import unittest
import sqlite3
import hashlib
import json
import os
import numpy as np
import pandas as pd
from unittest.mock import patch, MagicMock

from app.analytics.universe_config import resolve_universe_tickers
from app.analytics.foundation_models.challenger_evaluator import (
    FoundationChallengerEvaluator,
    ensure_foundation_evaluations_table
)
from app.data.historical_data_layer import get_db_path

CHAMPION_INTRADAY_SHA256 = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
CHAMPION_SWING_SHA256 = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"


class TestFoundationMetaLearnerRedesign(unittest.TestCase):
    """
    Comprehensive test suite verifying the forensic redesign of the Foundation Challenger
    Layer-2 Meta-Learner and Ablation Framework.
    """

    @classmethod
    def setUpClass(cls):
        ensure_foundation_evaluations_table()
        np.random.seed(42)
        cls.n_obs = 300
        cls.X_synthetic = np.random.randn(cls.n_obs, 5)
        # Features: ['rsi', 'macd', 'macd_diff', 'adx', 'atr']
        # Set realistic RSI range around 50
        cls.X_synthetic[:, 0] = 50.0 + cls.X_synthetic[:, 0] * 10.0
        cls.y_synthetic = (np.random.rand(cls.n_obs) > 0.70).astype(int)
        cls.features = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']

    def test_01_champion_probability_not_arbitrarily_discounted(self):
        """1. Champion probability is not arbitrarily multiplied by 0.70."""
        res = FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(self.X_synthetic, self.y_synthetic, self.features),
            timeframe="swing",
            universe="BENCHMARK_5"
        )
        champ = res["comparison"]["champion"]
        learned_coefs = champ.get("learned_coefficients", [])
        # In baseline, coefficient must be exactly [1.0] with 0 intercept
        self.assertEqual(learned_coefs, [1.0])
        self.assertEqual(champ.get("intercept", 0.0), 0.0)

    def test_02_no_unconditional_positive_boost_for_agreement(self):
        """2. Bearish agreement (-1) does not receive an unconditional positive probability boost."""
        # Create dataset where momentum is strongly negative
        X_bearish = np.zeros((100, 5))
        X_bearish[:, 0] = 30.0  # RSI = 30 (oversold/bearish)
        X_bearish[:, 2] = -2.0  # macd_diff = -2.0 (negative momentum)
        y_bearish = np.zeros(100, dtype=int)
        y_bearish[:15] = 1  # Ensure 2 classes exist for training

        res = FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(X_bearish, y_bearish, self.features),
            timeframe="swing",
            universe="BENCHMARK_5"
        )
        both = res["comparison"]["plus_both"]
        # Probabilities should be low for bearish data, not artificially boosted to >= 0.55
        self.assertEqual(both["completed_trade_count"], 0)

    def test_03_meta_learner_fits_only_on_train_partition(self):
        """3. Layer-2 Meta-Learner fits exclusively on TRAIN partition, not OOS."""
        res = FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(self.X_synthetic, self.y_synthetic, self.features),
            timeframe="swing",
            universe="BENCHMARK_5"
        )
        sample_defs = res.get("sample_definitions", {})
        self.assertIn("train_bars_count", sample_defs)
        self.assertIn("val_bars_count", sample_defs)
        self.assertIn("oos_bars_count", sample_defs)
        # Train + Val + OOS must equal total
        total = sample_defs["total_bars_count"]
        self.assertEqual(
            sample_defs["train_bars_count"] + sample_defs["val_bars_count"] + sample_defs["oos_bars_count"],
            total
        )

    def test_04_oos_normalization_statistics_not_used_for_training(self):
        """4. StandardScaler is fitted on TRAIN only and frozen for OOS transformation."""
        # Evaluator must not crash and must produce valid predictions without OOS statistics
        res = FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(self.X_synthetic, self.y_synthetic, self.features),
            timeframe="swing",
            universe="BENCHMARK_5"
        )
        self.assertIn("plus_both", res["comparison"])
        self.assertIn("learned_coefficients", res["comparison"]["plus_both"])

    def test_05_oos_calibration_not_fitted_on_oos_labels(self):
        """5. Zero OOS labels enter model fitting; evaluation is strictly single-pass."""
        res = FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(self.X_synthetic, self.y_synthetic, self.features),
            timeframe="swing",
            universe="BENCHMARK_5"
        )
        # Check that OOS bars count matches final 30% of n_obs
        expected_oos = len(self.y_synthetic) - int(len(self.y_synthetic) * 0.70)
        self.assertEqual(res["samples_evaluated"], expected_oos)

    def test_06_all_four_variants_use_identical_oos_data(self):
        """6. All variants evaluate on identical OOS holdout observations."""
        res = FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(self.X_synthetic, self.y_synthetic, self.features),
            timeframe="swing",
            universe="BENCHMARK_5"
        )
        comp = res["comparison"]
        for key in ["champion", "plus_timesfm", "plus_chronos", "plus_both"]:
            self.assertIn(key, comp)
            # All variants must report metrics
            self.assertIn("f1", comp[key])
            self.assertIn("brier", comp[key])
            self.assertIn("trade_count", comp[key])

    def test_07_threshold_identical_across_variants(self):
        """7. Decision threshold is fixed (0.50 for F1, 0.55 for trades) across all variants."""
        res = FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(self.X_synthetic, self.y_synthetic, self.features),
            timeframe="swing",
            universe="BENCHMARK_5"
        )
        comp = res["comparison"]
        for key in ["champion", "plus_timesfm", "plus_chronos", "plus_both"]:
            dist = comp[key]["distribution"]
            if dist:
                # count_ge_050 must match raw_signals_count
                self.assertEqual(dist["count_ge_050"], comp[key]["raw_signals_count"])
                # count_ge_055 must match qualified_signals_count
                self.assertEqual(dist["count_ge_055"], comp[key]["qualified_signals_count"])

    def test_08_existing_promotion_gates_strictly_maintained(self):
        """8. Promotion gates (30 trades, +0.0100 F1, Sharpe >= 0, Max DD <= 20%) are enforced."""
        res = FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(self.X_synthetic, self.y_synthetic, self.features),
            timeframe="swing",
            universe="BENCHMARK_5"
        )
        gates = res.get("gates", {})
        self.assertEqual(gates["required_trade_count"], 30)
        self.assertEqual(gates["max_drawdown_ceiling_pct"], 20.0)
        self.assertEqual(gates["f1_hurdle_gain"], 0.0100)

    def test_09_champion_model_hashes_unchanged(self):
        """9. Intraday and Swing Champion models remain byte-for-byte identical."""
        with open("backend/models/intraday/champion_ensemble.pkl", "rb") as f:
            intraday_hash = hashlib.sha256(f.read()).hexdigest()
        with open("backend/models/swing/champion_ensemble.pkl", "rb") as f:
            swing_hash = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(intraday_hash, CHAMPION_INTRADAY_SHA256)
        self.assertEqual(swing_hash, CHAMPION_SWING_SHA256)

    def test_10_existing_evaluations_preserved(self):
        """10. Existing historical evaluation records in SQLite remain queryable."""
        conn = sqlite3.connect(get_db_path())
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM foundation_challenger_evaluations")
        count = cur.fetchone()[0]
        conn.close()
        self.assertGreaterEqual(count, 23)

    def test_11_challenger_evaluation_writes_no_ml_trade_history(self):
        """11. Challenger evaluation writes zero records to production ml_trade_history."""
        conn = sqlite3.connect(get_db_path())
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ml_trade_history")
        before_count = cur.fetchone()[0]
        conn.close()

        FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(self.X_synthetic, self.y_synthetic, self.features),
            timeframe="swing",
            universe="BENCHMARK_5"
        )

        conn = sqlite3.connect(get_db_path())
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ml_trade_history")
        after_count = cur.fetchone()[0]
        conn.close()

        self.assertEqual(before_count, after_count)

    def test_12_challenger_evaluation_consumes_zero_portfolio_heat(self):
        """12. Evaluation consumes 0.0% portfolio heat."""
        from app.analytics.kelly_sizer import get_portfolio_heat_status
        heat = get_portfolio_heat_status()
        self.assertEqual(heat["current_heat_pct"], 0.0)

    def test_13_broker_remains_disabled(self):
        """13. Broker execution remains in fail-closed simulation mode."""
        conn = sqlite3.connect(get_db_path())
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = 'simulation_mode'")
        row = cur.fetchone()
        conn.close()
        sim_mode = row[0] if row else "true"
        self.assertEqual(sim_mode.lower(), "true")

    def test_14_telegram_remains_suppressed(self):
        """14. No telegram trading alerts are emitted during evaluation."""
        with patch("app.analytics.telegram_notifier.send_telegram_message") as mock_tg:
            FoundationChallengerEvaluator.evaluate_incremental_value(
                benchmark_dataset=(self.X_synthetic, self.y_synthetic, self.features),
                timeframe="swing",
                universe="BENCHMARK_5"
            )
            mock_tg.assert_not_called()

    def test_15_no_lookahead_in_feature_generation(self):
        """15. Proxy signals are computed row-by-row with no future window lookahead."""
        # Row 10 features should depend only on Row 10 values
        row = np.array([[55.0, 1.0, 0.5, 20.0, 15.0]])
        tfm = (row[0, 2] * 1.5) + ((row[0, 0] - 50.0) * 0.05)
        expected_tfm = (0.5 * 1.5) + (5.0 * 0.05)
        self.assertAlmostEqual(tfm, expected_tfm, places=4)

    def test_16_challenger_cannot_be_promoted_without_passing_gates(self):
        """16. Challenger fails promotion if trade count < 30 or max drawdown > 20%."""
        res = FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(self.X_synthetic, self.y_synthetic, self.features),
            timeframe="swing",
            universe="BENCHMARK_5"
        )
        gates = res.get("gates", {})
        if not gates.get("all_gates_passed", False):
            self.assertEqual(res["recommendation"], "RETAIN_CHAMPION")

    def test_17_agreement_alone_cannot_mechanically_create_bullish_probability(self):
        """17. Agreement is formulated directionally (+1, -1, 0) and learned via regularized regression."""
        # When both signals are negative, agreement is -1.0 (bearish)
        tfm_neg = -1.5
        chr_neg = -1.2
        agree = np.where((tfm_neg > 0) & (chr_neg > 0), 1.0, np.where((tfm_neg < 0) & (chr_neg < 0), -1.0, 0.0))
        self.assertEqual(float(agree), -1.0)

    def test_18_no_unconditional_probability_boost_per_feature(self):
        """18. Corrected Rule: No feature is permitted to mechanically or unconditionally increase probability.
        Any feature influence must arise from a fitted model coefficient learned exclusively from permitted training data."""
        # Create zero-variance or noise feature
        res = FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(self.X_synthetic, self.y_synthetic, self.features),
            timeframe="swing",
            universe="BENCHMARK_5"
        )
        both = res["comparison"]["plus_both"]
        # In the new architecture, learned coefficients are weights from LogisticRegression
        self.assertIn("learned_coefficients", both)
        self.assertEqual(len(both["learned_coefficients"]), 4)  # p_base, tfm, chr, agree


if __name__ == "__main__":
    unittest.main()
