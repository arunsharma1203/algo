"""
Comprehensive verification test suite for Quant Audit Remediation & Statistical Rigor Hardening.

Covers:
1. Stationary Block Bootstrap (Politis & Romano 1994, single unified resample draw, seed invariance).
2. Noise rejection: White noise yields Sharpe p05 <= 0.0.
3. Dual-Key promotion gate: Strict economic floors (N>=30, Sharpe>=0.75, WR>=48%, PF>=1.05, MaxDD<=20%).
4. Dual-Key Key 2: Paired OOS comparison (Delta Sharpe >= +0.05 vs Champion on same slices).
5. SSOT Timezone normalization (UTC to IST conversion, aware vs naive handling).
6. Dynamic Intraday Runway Gate (13:45 IST hard cutoff, distance/ATR bar requirement vs remaining session time).
7. First-principles Brownian volatility scaling (0.01 / sqrt(25) = 0.20%).
8. Scoped Upstox token probe (/market-quote/ltp, 5-minute cache).
9. Simulation Mode Guard in broker.py (fail-closed 403 on live order attempt).
"""

import math
import unittest
from datetime import datetime, time, timezone
from unittest.mock import patch, MagicMock
import numpy as np
import pytz
from fastapi import HTTPException

from app.analytics.trading_constants import (
    IST_TZ,
    HARD_CUTOFF_TIME_IST,
    SQUAREOFF_TIME_IST,
    get_ist_now,
    to_ist_datetime,
    evaluate_intraday_runway
)
from app.analytics.retrain_models import (
    compute_stationary_bootstrap_sharpe,
    simulate_out_of_sample_trading,
    execute_retraining_pipeline
)
from app.data.upstox_provider import (
    verify_upstox_data_token_live,
    get_upstox_token_status
)
from app.api.broker import execute_trade, ExecuteRequest


class TestQuantAuditFixes(unittest.TestCase):

    # ──────────────────────────────────────────────────────────────────────────
    # 1. POLITIS-ROMANO STATIONARY BLOCK BOOTSTRAP
    # ──────────────────────────────────────────────────────────────────────────

    def test_politis_romano_bootstrap_single_draw_determinism(self):
        """Unified resample draw is strictly deterministic with the same seed."""
        rets = np.array([0.01, -0.005, 0.015, -0.002, 0.008, 0.012, -0.004, 0.009, 0.003, -0.001] * 5)
        p05_run1, median_run1 = compute_stationary_bootstrap_sharpe(rets, n_resamples=500, mean_block_len=5.0, seed=42)
        p05_run2, median_run2 = compute_stationary_bootstrap_sharpe(rets, n_resamples=500, mean_block_len=5.0, seed=42)

        self.assertEqual(p05_run1, p05_run2)
        self.assertEqual(median_run1, median_run2)
        self.assertGreater(p05_run1, 0.0)

    def test_politis_romano_bootstrap_rejects_noise(self):
        """Zero-mean Gaussian noise must yield a non-positive 5th percentile lower bound (p05 <= 0.0)."""
        rng = np.random.default_rng(123)
        zero_mean_noise = rng.normal(loc=0.0, scale=0.01, size=100)

        p05, median = compute_stationary_bootstrap_sharpe(zero_mean_noise, n_resamples=1000, mean_block_len=5.0, seed=42)
        self.assertLessEqual(p05, 0.0, f"Expected p05 <= 0.0 for white noise, got {p05}")

    def test_politis_romano_bootstrap_empty_or_short(self):
        """Returns (0.0, 0.0) when sample count is fewer than 10 trades."""
        short_rets = np.array([0.01, 0.02, -0.01])
        p05, median = compute_stationary_bootstrap_sharpe(short_rets)
        self.assertEqual(p05, 0.0)
        self.assertEqual(median, 0.0)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. DUAL-KEY PROMOTION GATE ENFORCEMENT
    # ──────────────────────────────────────────────────────────────────────────

    @patch("app.analytics.retrain_models.ModelManager.load_champion_metadata")
    @patch("app.analytics.retrain_models.ModelManager.load_champion")
    @patch("app.analytics.retrain_models.prepare_benchmark_dataset")
    @patch("app.analytics.retrain_models.simulate_out_of_sample_trading")
    @patch("app.analytics.retrain_models.ModelManager.promote_challenger")
    @patch("app.analytics.retrain_models.sqlite3.connect")
    def test_dual_key_gate_rejects_challenger_inferior_to_champion(
        self, mock_conn, mock_promote, mock_sim, mock_prep, mock_champ_obj, mock_meta
    ):
        """Key 2 check: Challenger with Sharpe 0.80 is rejected if Champion scored 0.85 (delta < +0.05)."""
        mock_meta.return_value = {
            "version": "v1.0-champion",
            "champion_f1": 0.40,  # Active honest baseline
            "validation_metrics": {"sharpe_ratio": 0.85}
        }
        mock_conn.return_value = MagicMock()

        X = np.random.randn(100, 5)
        y = np.array([1, 0] * 50)
        mock_prep.return_value = (X, y, ["rsi", "macd", "macd_diff", "adx", "atr"])

        # simulate_out_of_sample_trading called twice:
        # 1st call for Challenger: Sharpe 0.80, 40 trades, WR 52%
        # 2nd call for Champion: Sharpe 0.85
        challenger_sim = {
            "sharpe": 0.80,
            "win_rate": 52.0,
            "profit_factor": 1.20,
            "max_drawdown_pct": 12.0,
            "trade_count": 40,
            "is_low_sample": False,
            "returns": np.full(40, 0.01)
        }
        champion_sim = {
            "sharpe": 0.85,
            "win_rate": 54.0,
            "profit_factor": 1.25,
            "max_drawdown_pct": 11.0,
            "trade_count": 38,
            "is_low_sample": False,
            "returns": np.full(38, 0.012)
        }
        mock_sim.side_effect = [challenger_sim, champion_sim]

        mock_champ_model = MagicMock()
        mock_champ_model.predict_proba.return_value = np.array([[0.3, 0.7]] * 20)
        mock_champ_obj.return_value = mock_champ_model

        with patch("app.analytics.retrain_models.f1_score", return_value=0.42), \
             patch("app.analytics.retrain_models.precision_score", return_value=0.44), \
             patch("app.analytics.retrain_models.recall_score", return_value=0.45):
            res = execute_retraining_pipeline(timeframe="swing")

        self.assertEqual(res["status"], "RETAINED_CHAMPION")
        self.assertFalse(mock_promote.called)

    @patch("app.analytics.retrain_models.ModelManager.load_champion_metadata")
    @patch("app.analytics.retrain_models.prepare_benchmark_dataset")
    @patch("app.analytics.retrain_models.simulate_out_of_sample_trading")
    @patch("app.analytics.retrain_models.ModelManager.promote_challenger")
    @patch("app.analytics.retrain_models.sqlite3.connect")
    def test_dual_key_gate_enforces_original_strict_thresholds(
        self, mock_conn, mock_promote, mock_sim, mock_prep, mock_meta
    ):
        """Key 1 checks: Rejects if Sharpe < 0.75, WinRate < 48%, PF < 1.05, or MaxDD > 20%."""
        mock_meta.return_value = {
            "version": "v1.0-champion",
            "champion_f1": 0.695,  # Obsolete baseline
            "validation_metrics": {"sharpe_ratio": 0.50}
        }
        mock_conn.return_value = MagicMock()
        X = np.random.randn(100, 5)
        y = np.array([1, 0] * 50)
        mock_prep.return_value = (X, y, ["rsi", "macd", "macd_diff", "adx", "atr"])

        # Case A: Sub-threshold Sharpe (0.68 < 0.75)
        mock_sim.return_value = {
            "sharpe": 0.68,
            "win_rate": 55.0,
            "profit_factor": 1.25,
            "max_drawdown_pct": 10.0,
            "trade_count": 40,
            "is_low_sample": False,
            "returns": np.full(40, 0.008)
        }
        with patch("app.analytics.retrain_models.f1_score", return_value=0.38):
            res = execute_retraining_pipeline(timeframe="swing")
        self.assertEqual(res["status"], "RETAINED_CHAMPION")

        # Case B: Sub-threshold Win Rate (46.0% < 48.0%)
        mock_sim.return_value = {
            "sharpe": 0.95,
            "win_rate": 46.0,
            "profit_factor": 1.25,
            "max_drawdown_pct": 10.0,
            "trade_count": 40,
            "is_low_sample": False,
            "returns": np.full(40, 0.01)
        }
        with patch("app.analytics.retrain_models.f1_score", return_value=0.38):
            res = execute_retraining_pipeline(timeframe="swing")
        self.assertEqual(res["status"], "RETAINED_CHAMPION")

        # Case C: Excessive Max Drawdown (22.5% > 20.0%)
        mock_sim.return_value = {
            "sharpe": 0.95,
            "win_rate": 52.0,
            "profit_factor": 1.25,
            "max_drawdown_pct": 22.5,
            "trade_count": 40,
            "is_low_sample": False,
            "returns": np.full(40, 0.01)
        }
        with patch("app.analytics.retrain_models.f1_score", return_value=0.38):
            res = execute_retraining_pipeline(timeframe="swing")
        self.assertEqual(res["status"], "RETAINED_CHAMPION")

    @patch("app.analytics.retrain_models.ModelManager.load_champion_metadata")
    @patch("app.analytics.retrain_models.prepare_benchmark_dataset")
    @patch("app.analytics.retrain_models.simulate_out_of_sample_trading")
    @patch("app.analytics.retrain_models.ModelManager.promote_challenger")
    @patch("app.analytics.retrain_models.sqlite3.connect")
    def test_dual_key_gate_enforces_minimum_30_trades(
        self, mock_conn, mock_promote, mock_sim, mock_prep, mock_meta
    ):
        """Rejects challenger with high Sharpe (1.40) if sample count is under 30 trades."""
        mock_meta.return_value = {"version": "v1.0", "champion_f1": 0.695}
        mock_conn.return_value = MagicMock()
        X = np.random.randn(100, 5)
        y = np.array([1, 0] * 50)
        mock_prep.return_value = (X, y, ["rsi", "macd", "macd_diff", "adx", "atr"])

        mock_sim.return_value = {
            "sharpe": 1.40,
            "win_rate": 60.0,
            "profit_factor": 1.80,
            "max_drawdown_pct": 8.0,
            "trade_count": 22,  # < 30 trades
            "is_low_sample": True,
            "returns": np.full(22, 0.02)
        }
        with patch("app.analytics.retrain_models.f1_score", return_value=0.40):
            res = execute_retraining_pipeline(timeframe="swing")

        self.assertEqual(res["status"], "RETAINED_CHAMPION")

    # ──────────────────────────────────────────────────────────────────────────
    # 3. SSOT TIMEZONE & DYNAMIC RUNWAY
    # ──────────────────────────────────────────────────────────────────────────

    def test_trading_constants_utc_conversion(self):
        """UTC 08:15 must convert to 13:45 IST; UTC 13:45 must convert to 19:15 IST."""
        utc_0815 = datetime(2026, 9, 16, 8, 15, 0, tzinfo=timezone.utc)
        ist_converted = to_ist_datetime(utc_0815)
        self.assertEqual(ist_converted.time(), HARD_CUTOFF_TIME_IST)
        self.assertEqual(ist_converted.strftime("%H:%M"), "13:45")

        utc_1345 = datetime(2026, 9, 16, 13, 45, 0, tzinfo=timezone.utc)
        ist_converted_late = to_ist_datetime(utc_1345)
        self.assertEqual(ist_converted_late.strftime("%H:%M"), "19:15")

        # get_ist_now returns timezone-aware IST
        now = get_ist_now()
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(str(now.tzinfo), "Asia/Kolkata")

    def test_dynamic_runway_gate_rejects_wide_target(self):
        """
        At 13:30 IST (105 mins to 15:15 = 7 bars remaining):
        - Target 4x ATR requires ceil(4.0 * 2.0) = 8 bars -> Rejected (INSUFFICIENT_RUNWAY).
        - Target 1x ATR requires ceil(1.0 * 2.0) = 2 bars -> Permitted (OK).
        """
        test_dt = datetime(2026, 9, 16, 13, 30, 0, tzinfo=IST_TZ)

        # Wide target: Entry 100, Target 120, ATR 5.0 -> Target distance = 20.0 = 4x ATR
        ok_wide, reason_wide = evaluate_intraday_runway(
            entry_price=100.0,
            target_price=120.0,
            atr_15m=5.0,
            now_dt=test_dt
        )
        self.assertFalse(ok_wide)
        self.assertIn("INSUFFICIENT_RUNWAY", reason_wide)
        self.assertIn("requires 8 bars", reason_wide)

        # Realistic tight target: Entry 100, Target 105, ATR 5.0 -> Target distance = 5.0 = 1x ATR
        ok_tight, reason_tight = evaluate_intraday_runway(
            entry_price=100.0,
            target_price=105.0,
            atr_15m=5.0,
            now_dt=test_dt
        )
        self.assertTrue(ok_tight)
        self.assertEqual(reason_tight, "OK")

    def test_dynamic_runway_gate_hard_cutoff_at_1345(self):
        """At 13:45 IST, setup is rejected regardless of target proximity."""
        test_dt = datetime(2026, 9, 16, 13, 45, 0, tzinfo=IST_TZ)
        ok, reason = evaluate_intraday_runway(
            entry_price=100.0,
            target_price=100.5,
            atr_15m=5.0,
            now_dt=test_dt
        )
        self.assertFalse(ok)
        self.assertIn("INTRADAY_TIME_EXHAUSTED", reason)
        self.assertIn("13:45", reason)

    # ──────────────────────────────────────────────────────────────────────────
    # 4. FIRST-PRINCIPLES BROWNIAN VOLATILITY SCALING
    # ──────────────────────────────────────────────────────────────────────────

    def test_first_principles_volatility_scaling_formula(self):
        """Brownian diffusion scaling: daily 1.0% / sqrt(25) = exactly 0.20% (0.0020)."""
        daily_threshold = 0.010
        bars_per_day_15m = 25.0
        intraday_15m_threshold = daily_threshold / math.sqrt(bars_per_day_15m)

        self.assertAlmostEqual(intraday_15m_threshold, 0.0020, places=6)
        self.assertEqual(round(intraday_15m_threshold * 100.0, 2), 0.20)

    # ──────────────────────────────────────────────────────────────────────────
    # 5. SCOPED UPSTOX DATA PROBE
    # ──────────────────────────────────────────────────────────────────────────

    @patch("app.data.upstox_provider.get_upstox_token", return_value="fake_access_token_1234567890")
    @patch("app.data.upstox_provider.requests.get")
    def test_scoped_upstox_data_probe_and_caching(self, mock_get, mock_token):
        """Confirms probe issues scoped GET /market-quote/ltp and caches for 300s."""
        # Reset cache for testing
        import app.data.upstox_provider as up
        up._last_probe_time = 0.0
        up._last_probe_result = None

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        # First probe issues request
        ok, status = verify_upstox_data_token_live(force=True)
        self.assertTrue(ok)
        self.assertEqual(status, "ACTIVE_AUTHENTICATED")
        self.assertEqual(mock_get.call_count, 1)

        # Check call arguments hit the scoped endpoint
        called_url = mock_get.call_args[0][0]
        self.assertIn("/market-quote/ltp", called_url)
        self.assertIn("NSE_EQ|INE002A01018", called_url)

        # Second probe within 5 minutes uses cache without second HTTP call
        ok2, status2 = verify_upstox_data_token_live(force=False)
        self.assertTrue(ok2)
        self.assertEqual(status2, "ACTIVE_AUTHENTICATED")
        self.assertEqual(mock_get.call_count, 1)

    # ──────────────────────────────────────────────────────────────────────────
    # 6. SIMULATION MODE GUARD IN BROKER.PY
    # ──────────────────────────────────────────────────────────────────────────

    def test_simulation_guard_hard_blocks_live_order(self):
        """Attempting to route a live order (simulation=False) raises 403 unprovisioned."""
        import asyncio
        req = ExecuteRequest(
            ticker="RELIANCE",
            action="BUY",
            quantity=10,
            target=2500.0,
            stop_loss=2400.0,
            simulation=False  # Live real-money request
        )
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(execute_trade(req))

        self.assertEqual(cm.exception.status_code, 403)
        self.assertIn("NOT PROVISIONED", cm.exception.detail)
        self.assertIn("fail-closed", cm.exception.detail)

    @patch("app.api.broker.get_live_quote_with_meta")
    def test_simulation_mode_paper_order_succeeds(self, mock_quote):
        """Paper simulation trade (simulation=True) succeeds and returns simulation=True."""
        import asyncio
        mock_quote.return_value = {"price": 2450.0, "source_name": "Upstox"}

        req = ExecuteRequest(
            ticker="TCS",
            action="BUY",
            quantity=5,
            target=3600.0,
            stop_loss=3400.0,
            simulation=True
        )
        res = asyncio.run(execute_trade(req))
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["simulation"])
        self.assertIn("SIM-", res["order_id"])


if __name__ == "__main__":
    unittest.main()
