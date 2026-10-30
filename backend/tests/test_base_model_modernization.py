import unittest
import numpy as np
import pandas as pd
from unittest.mock import patch

from app.analytics.optuna_tuner import prepare_benchmark_dataset
from app.analytics.decision_engine import evaluate_ticker, INTRADAY_FEATURES
from app.analytics.model_manager import ModelManager


class TestBaseModelModernization(unittest.TestCase):
    def test_feature_manifest_contains_structural_features(self):
        expected_features = [
            'rsi', 'macd', 'macd_diff', 'adx', 'returns',
            'vwap_deviation', 'orb_breakout', 'ema_alignment', 'volume_surge', 'time_of_day'
        ]
        self.assertEqual(INTRADAY_FEATURES, expected_features)

    def test_prepare_benchmark_dataset_intraday_structure(self):
        # Run on 3 liquid tickers
        X, y, features, meta = prepare_benchmark_dataset(
            timeframe='intraday', 
            tickers=['RELIANCE.NS', 'TCS.NS', 'HDFCBANK.NS'], 
            return_metadata=True
        )
        # Check shapes
        self.assertEqual(len(features), 10)
        self.assertEqual(X.shape[1], 10)
        self.assertEqual(len(X), len(y))
        self.assertGreater(len(X), 500)
        
        # Check zero NaNs
        self.assertFalse(np.isnan(X).any(), "Feature matrix X contains NaNs")
        self.assertFalse(np.isnan(y).any(), "Target vector y contains NaNs")
        
        # Check target class balance (Triple Barrier should be between 10% and 40%)
        pos_rate = y.mean() * 100.0
        self.assertGreater(pos_rate, 10.0, f"Positive rate {pos_rate:.1f}% is too low")
        self.assertLess(pos_rate, 40.0, f"Positive rate {pos_rate:.1f}% is too high")
        
        # Check session bounds: metadata dates
        self.assertIsNotNone(meta.get("data_start"))
        self.assertIsNotNone(meta.get("data_end"))

    def test_decision_engine_inference_compatibility(self):
        # Create a synthetic 15m OHLCV dataframe of 100 bars with valid OHLC relations
        dates = pd.date_range("2026-09-18 09:15", periods=100, freq="15min")
        np.random.seed(42)
        close = 1000.0 + np.cumsum(np.random.randn(100) * 2)
        open_p = close + np.random.randn(100) * 0.5
        high = np.maximum(open_p, close) + np.abs(np.random.randn(100) * 1.5) + 0.1
        low = np.minimum(open_p, close) - np.abs(np.random.randn(100) * 1.5) - 0.1
        volume = np.random.randint(1000, 50000, size=100)
        
        df = pd.DataFrame({
            "open": open_p,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume
        }, index=dates)
        
        champ, meta = ModelManager.load_champion('intraday'), ModelManager.load_champion_metadata('intraday')
        
        res = evaluate_ticker(
            ticker="TEST_TICKER.NS",
            df=df,
            champion_model=champ,
            champion_meta=meta,
            trade_type="INTRADAY",
            source="HISTORICAL_TEST",
            is_simulation=True
        )
        
        self.assertEqual(res.ticker, "TEST_TICKER.NS")
        self.assertIn("feature_engineering", res.pipeline_components)
        self.assertTrue(res.pipeline_components["feature_engineering"])


if __name__ == "__main__":
    unittest.main()
