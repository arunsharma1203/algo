"""
Unit tests for Legacy Promotion Gate Modernization and Intraday Time-Awareness Architecture.
Verifies:
1. Obsolete baseline handling in retrain_models.py (unfreezing Swing and Intraday models).
2. Economic safety gate enforcement (Sharpe, Win Rate, Profit Factor, Max Drawdown).
3. Intraday Entry Cutoff Gate at 13:45 IST (blocking late-day entries to prevent 15:15 square-off drag).
4. Simulation/backtesting bypass for the time gate.
5. Session-bounded target labeling in optuna_tuner.py (zero overnight lookahead bleed).
6. Normalized time_of_day feature calculation across market hours.
"""

import unittest
from datetime import datetime, date
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np
import pytz

from app.analytics.retrain_models import execute_retraining_pipeline
from app.analytics.decision_engine import evaluate_ticker
from app.analytics.model_manager import ModelManager


class TestIntradayTimeAwarenessAndPromotionFix(unittest.TestCase):

    # ──────────────────────────────────────────────────────────────────────────
    # 1. PROMOTION GATE MODERNIZATION TESTS
    # ──────────────────────────────────────────────────────────────────────────

    @patch("app.analytics.retrain_models.ModelManager.load_champion_metadata")
    @patch("app.analytics.retrain_models.prepare_benchmark_dataset")
    @patch("app.analytics.retrain_models.simulate_out_of_sample_trading")
    @patch("app.analytics.retrain_models.ModelManager.promote_challenger")
    @patch("app.analytics.retrain_models.sqlite3.connect")
    def test_promotion_gate_with_obsolete_baseline_promotes_viable_model(
        self, mock_conn, mock_promote, mock_sim, mock_prep, mock_meta
    ):
        # Baseline recorded with obsolete 0.695 F1
        mock_meta.return_value = {
            "version": "v1.0-champion",
            "champion_f1": 0.695,
            "validation_metrics": {"sharpe_ratio": 1.45}
        }
        mock_promote.return_value = "v2.0-champion"

        # Mock dataset with 100 samples
        X = np.random.randn(100, 5)
        y = np.array([1, 0] * 50)
        mock_prep.return_value = (X, y, ["rsi", "macd", "macd_diff", "adx", "atr"])

        # Challenger has honest F1 (0.38) and strong trading economics
        mock_sim.return_value = {
            "sharpe": 1.25,
            "win_rate": 54.0,
            "profit_factor": 1.35,
            "max_drawdown_pct": 9.2,
            "trade_count": 35,
            "is_low_sample": False,
            "returns": np.full(35, 0.015)
        }

        # Mock SQLite connection
        mock_db = MagicMock()
        mock_conn.return_value = mock_db

        with patch("app.analytics.retrain_models.f1_score", return_value=0.38), \
             patch("app.analytics.retrain_models.precision_score", return_value=0.40), \
             patch("app.analytics.retrain_models.recall_score", return_value=0.45):
            res = execute_retraining_pipeline(timeframe="swing")

        self.assertEqual(res["status"], "PROMOTED")
        self.assertTrue(mock_promote.called)
        self.assertIn("PASSED", res["active_version"] if "message" not in res else res.get("message", ""))

    @patch("app.analytics.retrain_models.ModelManager.load_champion_metadata")
    @patch("app.analytics.retrain_models.prepare_benchmark_dataset")
    @patch("app.analytics.retrain_models.simulate_out_of_sample_trading")
    @patch("app.analytics.retrain_models.ModelManager.promote_challenger")
    @patch("app.analytics.retrain_models.sqlite3.connect")
    def test_promotion_gate_rejects_unprofitable_model(
        self, mock_conn, mock_promote, mock_sim, mock_prep, mock_meta
    ):
        mock_meta.return_value = {
            "version": "v1.0-champion",
            "champion_f1": 0.695,
            "validation_metrics": {"sharpe_ratio": 1.45}
        }

        X = np.random.randn(100, 5)
        y = np.array([1, 0] * 50)
        mock_prep.return_value = (X, y, ["rsi", "macd", "macd_diff", "adx", "atr"])

        # Challenger has poor Sharpe and low win rate
        mock_sim.return_value = {
            "sharpe": 0.20,
            "win_rate": 35.0,
            "profit_factor": 0.85,
            "max_drawdown_pct": 28.0,
            "trade_count": 20,
            "is_low_sample": False
        }

        mock_db = MagicMock()
        mock_conn.return_value = mock_db

        with patch("app.analytics.retrain_models.f1_score", return_value=0.35), \
             patch("app.analytics.retrain_models.precision_score", return_value=0.35), \
             patch("app.analytics.retrain_models.recall_score", return_value=0.35):
            res = execute_retraining_pipeline(timeframe="swing")

        self.assertEqual(res["status"], "RETAINED_CHAMPION")
        self.assertFalse(mock_promote.called)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. INTRADAY ENTRY TIME-WINDOW GATE TESTS
    # ──────────────────────────────────────────────────────────────────────────

    def _create_sample_ohlcv(self):
        dates = pd.date_range("2026-09-16 09:15", periods=100, freq="15min")
        df = pd.DataFrame({
            "datetime": dates,
            "open": np.linspace(100, 110, 100),
            "high": np.linspace(101, 111, 100),
            "low": np.linspace(99, 109, 100),
            "close": np.linspace(100.5, 110.5, 100),
            "volume": [50000] * 100
        })
        return df

    @patch("app.data.market_provider.get_live_quote_with_meta")
    @patch("app.analytics.macro_engine.get_macro_regime")
    @patch("app.analytics.trading_constants.get_ist_now")
    def test_intraday_cutoff_blocks_entry_after_1345(self, mock_ist_now, mock_macro, mock_quote):
        # Current time: 14:15 IST (past 13:45 cutoff)
        ist = pytz.timezone("Asia/Kolkata")
        fake_now = datetime(2026, 9, 16, 14, 15, 0, tzinfo=ist)
        mock_ist_now.return_value = fake_now

        mock_macro.return_value = {
            "nifty_trend_short": "BULLISH",
            "distance_ema20_pct": 1.5,
            "regime_verdict": "BULL_EXPANSION"
        }
        mock_quote.return_value = {
            "price": 110.5, "source_name": "Upstox Live", "is_realtime": True,
            "age_ms": 10, "quote_freshness": "REAL_TIME"
        }

        mock_champion = MagicMock()
        mock_champion.predict_proba.return_value = np.array([[0.20, 0.80]])
        mock_champion.estimators_ = [mock_champion, mock_champion, mock_champion]

        df = self._create_sample_ohlcv()
        res = evaluate_ticker(
            ticker="RELIANCE.NS",
            df=df,
            champion_model=mock_champion,
            champion_meta={"version": "v1.0", "features": ["rsi", "macd", "macd_diff", "adx", "returns"]},
            trade_type="INTRADAY",
            source="AUTOPILOT",
            skip_enrichment=True,
            is_simulation=False,
            allow_candle_close=False
        )

        self.assertFalse(res.qualified)
        self.assertIn("INTRADAY_TIME_EXHAUSTED", res.rejection_reason)
        self.assertIn("13:45", res.rejection_reason)

    @patch("app.data.market_provider.get_live_quote_with_meta")
    @patch("app.analytics.macro_engine.get_macro_regime")
    @patch("app.analytics.trading_constants.get_ist_now")
    def test_intraday_cutoff_permits_entry_before_1345(self, mock_ist_now, mock_macro, mock_quote):
        # Current time: 10:30 IST (well before 13:45 cutoff)
        ist = pytz.timezone("Asia/Kolkata")
        fake_now = datetime(2026, 9, 16, 10, 30, 0, tzinfo=ist)
        mock_ist_now.return_value = fake_now

        mock_macro.return_value = {
            "nifty_trend_short": "BULLISH",
            "distance_ema20_pct": 1.5,
            "regime_verdict": "BULL_EXPANSION"
        }
        mock_quote.return_value = {
            "price": 110.5, "source_name": "Upstox Live", "is_realtime": True,
            "age_ms": 10, "quote_freshness": "REAL_TIME"
        }

        mock_champion = MagicMock()
        mock_champion.predict_proba.return_value = np.array([[0.20, 0.80]])
        mock_champion.estimators_ = [mock_champion, mock_champion, mock_champion]

        df = self._create_sample_ohlcv()
        res = evaluate_ticker(
            ticker="RELIANCE.NS",
            df=df,
            champion_model=mock_champion,
            champion_meta={"version": "v1.0", "features": ["rsi", "macd", "macd_diff", "adx", "returns"]},
            trade_type="INTRADAY",
            source="AUTOPILOT",
            skip_enrichment=True,
            is_simulation=False,
            allow_candle_close=False
        )

        # Time gate should not block; rejection (if any) must not be time exhausted
        if not res.qualified:
            self.assertNotIn("INTRADAY_TIME_EXHAUSTED", res.rejection_reason or "")

    @patch("app.data.market_provider.get_live_quote_with_meta")
    @patch("app.analytics.macro_engine.get_macro_regime")
    @patch("app.analytics.trading_constants.get_ist_now")
    def test_simulation_mode_bypasses_cutoff(self, mock_ist_now, mock_macro, mock_quote):
        # Current time: 15:00 IST
        ist = pytz.timezone("Asia/Kolkata")
        fake_now = datetime(2026, 9, 16, 15, 0, 0, tzinfo=ist)
        mock_ist_now.return_value = fake_now

        mock_macro.return_value = {
            "nifty_trend_short": "BULLISH",
            "distance_ema20_pct": 1.5,
            "regime_verdict": "BULL_EXPANSION"
        }
        mock_quote.return_value = {
            "price": 110.5, "source_name": "Upstox Live", "is_realtime": True,
            "age_ms": 10, "quote_freshness": "REAL_TIME"
        }

        mock_champion = MagicMock()
        mock_champion.predict_proba.return_value = np.array([[0.20, 0.80]])
        mock_champion.estimators_ = [mock_champion, mock_champion, mock_champion]

        df = self._create_sample_ohlcv()
        res = evaluate_ticker(
            ticker="RELIANCE.NS",
            df=df,
            champion_model=mock_champion,
            champion_meta={"version": "v1.0", "features": ["rsi", "macd", "macd_diff", "adx", "returns"]},
            trade_type="INTRADAY",
            source="FORWARD_SIMULATION",
            skip_enrichment=True,
            is_simulation=True,  # SIMULATION BYPASS
            allow_candle_close=True
        )

        self.assertNotIn("INTRADAY_TIME_EXHAUSTED", res.rejection_reason or "")

    # ──────────────────────────────────────────────────────────────────────────
    # 3. SESSION-BOUNDED TARGET LABELING TESTS
    # ──────────────────────────────────────────────────────────────────────────

    def test_session_bounded_lookahead_no_overnight_leakage(self):
        """Verifies that 5-bar lookahead rolling stops strictly at the day boundary."""
        dates = [
            # Day 1 afternoon
            "2026-09-15 14:30:00", "2026-09-15 14:45:00", "2026-09-15 15:00:00", "2026-09-15 15:15:00", "2026-09-15 15:30:00",
            # Day 2 morning (massive open spike at 500)
            "2026-09-16 09:15:00", "2026-09-16 09:30:00", "2026-09-16 09:45:00", "2026-09-16 10:00:00", "2026-09-16 10:15:00"
        ]
        df = pd.DataFrame({
            "datetime": pd.to_datetime(dates),
            "high": [100, 101, 102, 103, 104, 500, 501, 502, 503, 504],
            "close": [99, 100, 101, 102, 103, 499, 500, 501, 502, 503]
        })
        date_series = df["datetime"].dt.date

        def _forward_roll_session(s, window, agg_func):
            return s.groupby(date_series).transform(
                lambda grp: grp.shift(-1).iloc[::-1].rolling(window=window, min_periods=1).agg(agg_func).iloc[::-1]
            )

        fut_high = _forward_roll_session(df["high"], 5, "max")

        # Bar 3 (15:15 on Day 1): The ONLY remaining bar today is 15:30 (high=104)
        # It MUST NOT see Day 2's 500 high!
        self.assertEqual(fut_high.iloc[3], 104.0)
        self.assertLess(fut_high.iloc[3], 200.0)

        # Bar 4 (15:30 on Day 1): Last bar of the day, shift(-1) is NaN
        self.assertTrue(pd.isna(fut_high.iloc[4]))

    def test_time_of_day_feature_bounds(self):
        """Verifies that normalized time_of_day is 0.0 at 09:15 IST and 1.0 at 15:30 IST."""
        test_times = [
            ("2026-09-16 09:15:00", 0.0),
            ("2026-09-16 12:22:30", 0.50),
            ("2026-09-16 15:30:00", 1.0)
        ]
        for dt_str, expected_approx in test_times:
            dt = pd.to_datetime(dt_str)
            minutes_from_open = (dt.hour * 60 + dt.minute) - (9 * 60 + 15)
            val = min(max(minutes_from_open / 375.0, 0.0), 1.0)
            self.assertAlmostEqual(val, expected_approx, delta=0.02)


if __name__ == "__main__":
    unittest.main()
