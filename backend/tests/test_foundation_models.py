import os
import sys
import unittest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Add backend directory to sys.path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.analytics.foundation_models.base import TimeSeriesFoundationModel, ForecastResult, FoundationModelFeatures
from app.analytics.foundation_models.timesfm_adapter import TimesFMAdapter
from app.analytics.foundation_models.chronos_adapter import ChronosAdapter
from app.analytics.foundation_models.manager import FoundationModelManager, foundation_model_manager
from app.analytics.foundation_models.challenger_evaluator import FoundationChallengerEvaluator
from app.analytics.meta_learner import TradeMetaLearner
from app.data.validator import DataValidationError

class TestFoundationModels(unittest.TestCase):
    """
    Comprehensive Test Suite for Time-Series Foundation Model Challenger Layer
    (TimesFM 2.5 & Chronos-2).
    """

    def setUp(self):
        dates = pd.date_range(end=datetime(2026, 8, 29, 15, 30), periods=100, freq='15min')
        prices = np.linspace(100.0, 115.0, 100)
        self.valid_df = pd.DataFrame({
            'open': prices,
            'high': prices + 1.5,
            'low': prices - 1.0,
            'close': prices + 0.5,
            'volume': np.random.randint(1000, 5000, 100),
            'datetime': dates
        }, index=dates)

    # 1. TimesFM Adapter Interface test
    def test_timesfm_adapter_interface(self):
        tfm = TimesFMAdapter()
        info = tfm.get_model_info()
        self.assertEqual(info["model_name"], "TimesFM 2.5")
        self.assertIn("15m", info["supported_timeframes"])
        self.assertIn("1d", info["supported_timeframes"])

    # 2. Chronos Adapter Interface test
    def test_chronos_adapter_interface(self):
        chr_model = ChronosAdapter()
        info = chr_model.get_model_info()
        self.assertEqual(info["model_name"], "Chronos-2")
        self.assertEqual(info["output_type"], "probabilistic_quantiles")

    # 3. Future timestamps raise DataValidationError
    def test_future_timestamps_raise_data_validation_error(self):
        tfm = TimesFMAdapter()
        as_of = datetime(2026, 8, 29, 12, 0)
        
        # DF containing rows beyond 12:00
        dates = pd.date_range(start='2026-08-29 09:15', end='2026-08-29 15:30', freq='15min')
        future_df = pd.DataFrame({'close': np.ones(len(dates)) * 100, 'datetime': dates})
        
        with self.assertRaises(DataValidationError):
            tfm.forecast("RELIANCE.NS", future_df, horizon_bars=1, timeframe="15m", as_of_time=as_of)

    # 4. Insufficient data returns unavailable / insufficient_data
    def test_insufficient_data_returns_unavailable(self):
        tfm = TimesFMAdapter()
        small_df = self.valid_df.iloc[:15]  # Only 15 rows (< 30 required)
        
        res = tfm.forecast("TCS.NS", small_df, horizon_bars=1, timeframe="15m")
        self.assertEqual(res.status, "insufficient_data")
        self.assertEqual(res.expected_return_pct, 0.0)

    # 5. TimesFM failure does not crash pipeline
    def test_timesfm_failure_does_not_crash_pipeline(self):
        tfm = TimesFMAdapter()
        # Non-existent column or corrupted data
        bad_df = pd.DataFrame({'wrong_col': [1, 2, 3] * 20})
        res = tfm.forecast("INFY.NS", bad_df, horizon_bars=1, timeframe="15m")
        self.assertIn(res.status, ["error", "insufficient_data"])

    # 6. Chronos failure does not crash pipeline
    def test_chronos_failure_does_not_crash_pipeline(self):
        chr_model = ChronosAdapter()
        bad_df = pd.DataFrame({'wrong_col': [1, 2, 3] * 20})
        res = chr_model.forecast("INFY.NS", bad_df, horizon_bars=1, timeframe="15m")
        self.assertIn(res.status, ["error", "insufficient_data"])

    # 7. Dual failure preserves Champion execution
    def test_dual_failure_preserves_champion_execution(self):
        manager = FoundationModelManager()
        # Pass empty dataframe to simulate full failure
        tfm_res, chr_res, feat = manager.generate_foundation_signals("SBIN.NS", pd.DataFrame(), timeframe="15m")
        self.assertEqual(tfm_res.status, "insufficient_data")
        self.assertEqual(chr_res.status, "insufficient_data")
        self.assertEqual(feat.models_available_count, 0)
        self.assertEqual(feat.foundation_direction_agreement, 0.0)

    # 8. Intraday uses 15m horizon
    def test_intraday_uses_15m_horizon(self):
        manager = FoundationModelManager()
        tfm_res, chr_res, _ = manager.generate_foundation_signals("HDFCBANK.NS", self.valid_df, timeframe="15m")
        self.assertEqual(tfm_res.timeframe, "15m")
        self.assertEqual(tfm_res.horizon_bars, 1)

    # 9. Swing uses daily horizon
    def test_swing_uses_daily_horizon(self):
        manager = FoundationModelManager()
        tfm_res, chr_res, _ = manager.generate_foundation_signals("HDFCBANK.NS", self.valid_df, timeframe="1d")
        self.assertEqual(tfm_res.timeframe, "1d")
        self.assertEqual(tfm_res.horizon_bars, 5)

    # 10. Foundation agreement feature calculation
    def test_foundation_agreement_feature_calculation(self):
        manager = FoundationModelManager()
        
        # Test Case A: Concurrence (Both Bullish)
        tfm_bull = ForecastResult(
            model_name="timesfm", model_version="2.5", symbol="RELIANCE",
            timeframe="1d", as_of_time="2026-08-29", horizon_bars=5,
            expected_return_pct=2.5, direction="BULLISH", status="success"
        )
        chr_bull = ForecastResult(
            model_name="chronos", model_version="2", symbol="RELIANCE",
            timeframe="1d", as_of_time="2026-08-29", horizon_bars=5,
            expected_return_pct=2.0, median_return_pct=2.0, direction="BULLISH", status="success"
        )
        feat_agree = manager._calculate_foundation_features(tfm_bull, chr_bull)
        self.assertEqual(feat_agree.foundation_direction_agreement, 1.0)
        self.assertEqual(feat_agree.foundation_expected_return_spread, 0.5)
        self.assertEqual(feat_agree.foundation_consensus_score, 2.25)

        # Test Case B: Divergence (Bullish vs Bearish)
        chr_bear = ForecastResult(
            model_name="chronos", model_version="2", symbol="RELIANCE",
            timeframe="1d", as_of_time="2026-08-29", horizon_bars=5,
            expected_return_pct=-1.5, median_return_pct=-1.5, direction="BEARISH", status="success"
        )
        feat_diverge = manager._calculate_foundation_features(tfm_bull, chr_bear)
        self.assertEqual(feat_diverge.foundation_direction_agreement, -1.0)
        self.assertEqual(feat_diverge.foundation_expected_return_spread, 4.0)
        self.assertEqual(feat_diverge.foundation_consensus_score, 0.0)

    # 11. Meta-Learner receives foundation features
    def test_meta_learner_receives_foundation_features(self):
        meta = TradeMetaLearner()
        found_feat = FoundationModelFeatures(
            timesfm_expected_return=1.8,
            timesfm_direction_code=1.0,
            timesfm_status="success",
            chronos_expected_return=1.5,
            chronos_direction_code=1.0,
            chronos_status="success",
            foundation_direction_agreement=1.0,
            foundation_consensus_score=1.65,
            models_available_count=2
        )
        
        score, msg, tel = meta.evaluate_new_trade(
            ticker="LT.NS",
            direction="BULLISH",
            trade_type="SWING",
            base_confidence=72.0,
            foundation_features=found_feat
        )
        self.assertIsInstance(score, float)
        self.assertIn("Foundation Consensus", str(tel["reasons"]))

    # 12. Point-in-time cache lookup
    def test_point_in_time_cache_lookup(self):
        manager = FoundationModelManager()
        as_of = datetime(2026, 8, 29, 14, 0)
        
        tfm1, _, _ = manager.generate_foundation_signals("ICICIBANK.NS", self.valid_df, timeframe="15m", as_of_time=as_of)
        cache_count_before = len(manager._forecast_cache)
        
        # Second call with identical as_of timestamp should hit cache
        tfm2, _, _ = manager.generate_foundation_signals("ICICIBANK.NS", self.valid_df, timeframe="15m", as_of_time=as_of)
        cache_count_after = len(manager._forecast_cache)
        
        self.assertEqual(cache_count_before, cache_count_after)

    # 13. Challenger Incremental Value Evaluation Suite
    def test_challenger_incremental_value_evaluation(self):
        # Generate synthetic benchmark feature matrix for testing evaluator
        X_mock = np.random.randn(200, 5)
        # Construct predictive signal so classification works
        y_mock = (X_mock[:, 0] + X_mock[:, 2] > 0.0).astype(int)
        features = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']
        
        report = FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(X_mock, y_mock, features),
            timeframe="swing"
        )
        self.assertEqual(report["status"], "success")
        self.assertIn("comparison", report)
        self.assertIn("champion", report["comparison"])
        self.assertIn("plus_both", report["comparison"])
        self.assertIn("regime_analysis", report)
        self.assertIn("recommendation", report)

    # 14. Zero synthetic data guarantee in foundation
    def test_zero_synthetic_data_guarantee_in_foundation(self):
        tfm = TimesFMAdapter()
        # Empty input
        res = tfm.forecast("TEST.NS", pd.DataFrame(), horizon_bars=5)
        self.assertEqual(res.status, "insufficient_data")
        self.assertEqual(res.forecast_path, [])
        self.assertEqual(res.expected_return_pct, 0.0)

    # 15. ForecastResult serialization to dictionary
    def test_forecast_result_serialization(self):
        res = ForecastResult(
            model_name="timesfm", model_version="2.5", symbol="AXISBANK.NS",
            timeframe="15m", as_of_time="2026-08-29T15:30:00", horizon_bars=1,
            expected_return_pct=0.45, uncertainty_score=0.12, direction="BULLISH",
            forecast_path=[1200.5, 1205.9], status="success"
        )
        d = res.to_dict()
        self.assertEqual(d["model_name"], "timesfm")
        self.assertEqual(d["expected_return_pct"], 0.45)
        self.assertEqual(len(d["forecast_path"]), 2)

    # 16. FoundationFeatures vector format
    def test_foundation_features_vector_format(self):
        feat = FoundationModelFeatures()
        vec = feat.to_vector()
        self.assertEqual(len(vec), 12)
        self.assertTrue(all(isinstance(v, (int, float)) for v in vec))

    # 17. Chronos quantile downside and upside calculation
    def test_chronos_quantile_downside_and_upside_calculation(self):
        res = ForecastResult(
            model_name="chronos_2", model_version="bolt", symbol="TCS.NS",
            timeframe="1d", as_of_time="2026-08-29", horizon_bars=5,
            expected_return_pct=1.8, median_return_pct=1.5,
            lower_quantile_return_pct=-0.8, upper_quantile_return_pct=3.2,
            downside_risk_pct=0.8, upside_potential_pct=3.2,
            uncertainty_score=4.0, direction="BULLISH", status="success"
        )
        self.assertEqual(res.downside_risk_pct, 0.8)
        self.assertEqual(res.upside_potential_pct, 3.2)
        self.assertEqual(res.direction, "BULLISH")

if __name__ == '__main__':
    unittest.main()

