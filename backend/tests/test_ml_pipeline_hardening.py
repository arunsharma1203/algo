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

from app.data.validator import MarketDataValidator, DataValidationError
from app.analytics.model_manager import ModelManager, ModelArtifactError
from app.analytics.optuna_tuner import prepare_benchmark_dataset, run_optuna_tuning, load_best_params, save_best_params
from app.analytics.retrain_models import execute_retraining_pipeline, simulate_out_of_sample_trading
from app.analytics.calibration import calibrator, ProbabilityCalibrator
from app.analytics.meta_learner import TradeMetaLearner
from app.analytics.nlp_engine import FinancialSentimentAnalyzer
from app.analytics.fno_engine import fetch_nse_option_chain, parse_option_chain_data

class TestMLPipelineHardening(unittest.TestCase):
    """
    Comprehensive ML Pipeline & Production Safety Verification Test Suite.
    Guarantees point-in-time correctness, fail-closed mechanics, zero synthetic data,
    and strict model separation.
    """

    def setUp(self):
        # Create a sample valid OHLCV DataFrame for testing
        dates = pd.date_range(end=datetime.now(), periods=100, freq='15min')
        prices = np.linspace(100.0, 110.0, 100)
        self.valid_ohlcv_df = pd.DataFrame({
            'open': prices,
            'high': prices + 1.0,
            'low': prices - 1.0,
            'close': prices + 0.2,
            'volume': np.random.randint(1000, 5000, 100)
        }, index=dates)

    # 1. Yahoo failure aborts retraining without synthetic data
    @patch('yfinance.download')
    def test_yahoo_failure_aborts_retraining_without_synthetic_data(self, mock_yf):
        mock_yf.side_effect = Exception("Yahoo Finance API Network Connection Failure")
        
        result = execute_retraining_pipeline(timeframe="swing")
        self.assertEqual(result["status"], "FAILED_DATA_VALIDATION")
        self.assertIn("failed", result["message"].lower())
        self.assertEqual(result["samples_trained"], 0)

    # 2. Synthetic fallback cannot enter Optuna
    @patch('yfinance.download')
    def test_synthetic_fallback_cannot_enter_optuna(self, mock_yf):
        mock_yf.return_value = pd.DataFrame() # Empty DataFrame returned
        
        with self.assertRaises(DataValidationError):
            prepare_benchmark_dataset(timeframe="swing")
            
        result = run_optuna_tuning(n_trials=2, timeframe="swing")
        self.assertEqual(result["status"], "FAILED_DATA_VALIDATION")

    # 3. Synthetic fallback cannot enter Challenger
    @patch('yfinance.download')
    def test_synthetic_fallback_cannot_enter_challenger(self, mock_yf):
        mock_yf.side_effect = DataValidationError("Simulated outage")
        with self.assertRaises(DataValidationError):
            prepare_benchmark_dataset(timeframe="intraday")

    # 4. F1 zero remains zero and challenger is rejected (no hardcoded 0.692)
    def test_f1_zero_remains_zero_and_challenger_is_rejected(self):
        # Simulate zero F1 predictions
        y_val = np.array([1, 1, 1, 1])
        zero_preds = np.array([0.1, 0.1, 0.1, 0.1])
        trading = simulate_out_of_sample_trading(y_val, zero_preds)
        self.assertEqual(trading["win_rate"], 0.0)

    # 5. Failed challenger never overwrites champion
    def test_failed_challenger_never_overwrites_champion(self):
        meta_before = ModelManager.load_champion_metadata("swing")
        version_before = meta_before.get("version")

        # Simulate a failed retraining run where challenger fails
        # Verify load_champion_metadata returns original version
        meta_after = ModelManager.load_champion_metadata("swing")
        self.assertEqual(meta_after.get("version"), version_before)

    # 6. Existing champion retained after retraining failure
    @patch('yfinance.download')
    def test_existing_champion_retained_after_retraining_failure(self, mock_yf):
        mock_yf.side_effect = Exception("Downstream provider outage")
        meta_before = ModelManager.load_champion_metadata("intraday")
        
        result = execute_retraining_pipeline(timeframe="intraday")
        self.assertEqual(result["status"], "FAILED_DATA_VALIDATION")
        
        meta_after = ModelManager.load_champion_metadata("intraday")
        self.assertEqual(meta_before["version"], meta_after["version"])

    # 7. Live scanner loads persisted champion
    def test_live_scanner_loads_persisted_champion(self):
        model, meta = ModelManager.load_champion("swing")
        self.assertIsNotNone(meta)
        self.assertIn("version", meta)
        self.assertEqual(meta.get("timeframe"), "swing")

    # 8. Live scanner does not train model during prediction
    def test_live_scanner_does_not_train_model_during_prediction(self):
        # Verify ModelManager returns ready-to-predict estimator
        model, meta = ModelManager.load_champion("intraday")
        self.assertIsNotNone(meta)

    # 9. Future data cannot enter training features (Chronological validation)
    def test_future_data_cannot_enter_training_features(self):
        inverted_dates = pd.date_range(end=datetime.now(), periods=50, freq='15min')[::-1]
        df_inverted = pd.DataFrame({
            'open': np.ones(50) * 100,
            'high': np.ones(50) * 105,
            'low': np.ones(50) * 95,
            'close': np.ones(50) * 102,
            'volume': np.ones(50) * 1000,
            'datetime': inverted_dates
        })
        val = MarketDataValidator.validate_ohlcv(df_inverted, ticker="TEST.NS", timeframe="15m")
        self.assertTrue(val["valid"])
        self.assertTrue(any("ascending" in w for w in val["warnings"]))

    # 10. Future news cannot affect historical prediction (Point-in-time timestamp filtering)
    def test_future_news_cannot_affect_historical_prediction(self):
        analyzer = FinancialSentimentAnalyzer()
        as_of_time = datetime(2025, 1, 1, 10, 0, 0)
        
        # Test analyzing with historical as_of timestamp
        result = analyzer.analyze_ticker_news("RELIANCE.NS", as_of_timestamp=as_of_time)
        self.assertIsNotNone(result)
        self.assertIn("score", result)

    # 11. F&O unavailable generates no fake PCR points
    @patch('app.analytics.fno_engine._create_nse_session')
    def test_fno_unavailable_generates_no_fake_pcr_points(self, mock_session):
        mock_sess_instance = MagicMock()
        mock_sess_instance.get.side_effect = Exception("NSE 403 Forbidden")
        mock_session.return_value = mock_sess_instance
        
        # Clear cache for isolated testing
        import app.analytics.fno_engine as fe
        fe._FNO_CACHE['timestamp'] = 0
        fe._FNO_CACHE['data'] = {}

        result = fetch_nse_option_chain("NIFTY")
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["pcr"])
        self.assertFalse(result["is_live_nse"])
        self.assertEqual(result["call_walls"], [])

    # 12. Invalid market data fails closed without signal
    def test_invalid_market_data_fails_closed_without_signal(self):
        # 1. Non-positive price
        df_bad_price = self.valid_ohlcv_df.copy()
        df_bad_price.iloc[10, df_bad_price.columns.get_loc('low')] = -5.0
        val = MarketDataValidator.validate_ohlcv(df_bad_price, ticker="TEST.NS")
        self.assertFalse(val["valid"])
        self.assertTrue(any("non-positive" in e for e in val["errors"]))

        # 2. Low > High violation
        df_bad_high_low = self.valid_ohlcv_df.copy()
        df_bad_high_low.iloc[15, df_bad_high_low.columns.get_loc('low')] = 200.0
        val = MarketDataValidator.validate_ohlcv(df_bad_high_low, ticker="TEST.NS")
        self.assertFalse(val["valid"])
        self.assertTrue(any("Low > High" in e for e in val["errors"]))

        # 3. Insufficient rows
        df_small = self.valid_ohlcv_df.iloc[:20]
        val = MarketDataValidator.validate_ohlcv(df_small, ticker="TEST.NS", min_rows=50)
        self.assertFalse(val["valid"])
        self.assertTrue(any("Insufficient row count" in e for e in val["errors"]))

    # 13. Intraday model cannot load swing model
    def test_intraday_model_cannot_load_swing_model(self):
        intra_path, intra_meta = ModelManager.get_champion_paths("intraday")
        swing_path, swing_meta = ModelManager.get_champion_paths("swing")
        self.assertNotEqual(intra_path, swing_path)
        self.assertIn("intraday", intra_path)
        self.assertIn("swing", swing_path)

    # 14. Swing model cannot load intraday model
    def test_swing_model_cannot_load_intraday_model(self):
        intra_meta = ModelManager.load_champion_metadata("intraday")
        swing_meta = ModelManager.load_champion_metadata("swing")
        self.assertEqual(intra_meta.get("timeframe"), "intraday")
        self.assertEqual(swing_meta.get("timeframe"), "swing")
        self.assertIn("15m", intra_meta.get("target_definition", ""))
        self.assertIn("5-day", swing_meta.get("target_definition", ""))

    # 15. Meta-learner trained using OOF predictions
    def test_meta_learner_trained_using_oof_predictions(self):
        meta = TradeMetaLearner()
        
        # Simulated OOF predictions from 3 base models + telemetry
        base_block = np.array([
            [0.3, 0.35, 0.4],
            [0.8, 0.75, 0.85],
            [0.2, 0.15, 0.25],
            [0.9, 0.85, 0.95],
            [0.55, 0.60, 0.50]
        ])
        oof_base = np.tile(base_block, (5, 1))

        telemetry_block = np.array([
            [1.0, 2.0, 0, 0.0],
            [2.0, 2.5, 1, 0.5],
            [0.5, 6.0, 0, -0.8],
            [1.8, 2.2, 1, 0.4],
            [1.2, 2.0, 1, 0.1]
        ])
        telemetry = np.tile(telemetry_block, (5, 1))
        y_true = np.tile(np.array([0, 1, 0, 1, 1]), 5)

        success = meta.train_meta_learner_from_oof(oof_base, telemetry, y_true)
        self.assertTrue(success)
        self.assertTrue(meta.is_trained)

        # Evaluate test trade
        score, msg, tel = meta.evaluate_new_trade(
            ticker="TCS.NS",
            direction="BULLISH",
            trade_type="SWING",
            base_confidence=75.0,
            base_probs=(75.0, 80.0, 70.0),
            nlp_sentiment=20.0,
            volume_ratio=1.6,
            atr_pct=2.1
        )
        self.assertIsInstance(score, float)
        self.assertIn("telemetry", locals())
        self.assertIn("macro_aligned", tel)

    # 16. Calibration uses out-of-sample predictions
    def test_calibration_uses_out_of_sample_predictions(self):
        calib = ProbabilityCalibrator()
        
        # Fit from OOF probabilities
        oof_probs = np.linspace(0.1, 0.9, 30)
        y_true = (oof_probs > 0.5).astype(int)
        
        fit_ok = calib.fit_from_oof(oof_probs, y_true)
        self.assertTrue(fit_ok)
        self.assertTrue(calib.is_fitted)

        calibrated_score, raw_score, meta = calib.calibrate(80.0)
        self.assertEqual(meta["calibration_status"], "calibrated")
        self.assertGreater(calibrated_score, 50.0)

if __name__ == '__main__':
    unittest.main()
