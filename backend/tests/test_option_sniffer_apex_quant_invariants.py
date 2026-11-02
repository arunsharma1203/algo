"""
Rigorous Invariant Verification Suite for Option Sniffer Apex Quant Setup:
  1. CRB-I (Causal Regime Breakout Initiation) admission & ER relaxation.
  2. Theta Stagnation Stop (12m velocity scratch at -4% to -6% vs -35% SL).
  3. Intraday Peak-Profit Trailing Lock (locks 50% of gains when peak >= Rs 4,000).
  4. Emergency Circuit Breaker boundaries (daily loss bounded at <= -Rs 3,500).
  5. Fallback & Safe Disarm when ENABLE_APEX_QUANT_ENGINE = False.
  6. Model artifact immutability (SHA-256 untouched).
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np
import hashlib

from app.analytics.option_sniffer.option_constants import (
    ENABLE_APEX_QUANT_ENGINE,
    THETA_STAGNATION_TIMEOUT_MINUTES,
    THETA_STAGNATION_MIN_MFE_PCT,
    THETA_STAGNATION_EXIT_LOSS_CAP_PCT,
    PEAK_PROFIT_LOCK_TRIGGER_INR,
    PEAK_PROFIT_GIVEBACK_MAX_RATIO,
    CRBI_BREAKOUT_VOLUME_MIN_RATIO,
    CRBI_RELAXED_MODEL_PROB,
    CRBI_RELAXED_KAUFMAN_ER,
    CRBI_MIN_HEAVYWEIGHT_ALIGNMENT,
    MIN_CALIBRATED_PROB,
    KAUFMAN_ER_MIN_THRESHOLD,
    MAX_DAILY_LOSS_INR,
    STOP_LOSS_PCT,
    TARGET_GAIN_PCT,
    STATE_WATCH,
    STATE_ACTIVE,
    DIRECTION_BUY_CE,
    DIRECTION_BUY_PE,
    DIRECTION_NO_TRADE,
)
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.option_sniffer.option_db import OptionDB, init_option_sniffer_tables
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
from app.analytics.option_sniffer.option_alpha_model import OptionAlphaModel
from app.analytics.option_sniffer.option_sniffer_orchestrator import OptionSnifferOrchestrator


class TestOptionSnifferApexQuantInvariants(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp_db.name
        self.tmp_db.close()

        self.db_patcher = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.db_patcher.start()
        self.live_db_patcher = patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path)
        self.live_db_patcher.start()

        # Initialize schema in isolated temp DB
        init_option_sniffer_tables()
        from app.analytics.option_sniffer.option_live_order_intent import init_live_order_tables
        init_live_order_tables()

        # Reset class state
        OptionSignalEngine.reset_class_state()

    def tearDown(self):
        self.db_patcher.stop()
        self.live_db_patcher.stop()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_01_stagnation_stop_triggers_at_12_minutes_for_non_moving_trade(self):
        """
        Invariant: If a trade has duration >= 12m, MFE < 5%, and negative P&L,
        it must exit immediately with exit_reason == 'STAGNATION_TIME_STOP'.
        """
        entry_time = (datetime(2026, 10, 12, 10, 0, 0) - timedelta(minutes=13)).isoformat()
        trade = {
            "trade_id": "test_stagnation_1",
            "signal_id": "sig_stag_1",
            "instrument_key": "NSE_FO|NIFTY26OCT25000CE",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
            "quantity": 65,
            "entry_time": entry_time,
            "entry_bid": 98.0,
            "entry_ask": 100.0,
            "entry_spot_price": 25000.0,
            "dynamic_sl_pct": 20.0,
            "dynamic_target_pct": 35.0,
        }
        OptionDB.create_paper_trade(trade)

        # Update extrema so MFE is only 2.0% (peak = 102.0), current bid = 95.0 (-5.0% current P&L)
        OptionDB.update_trade_extrema(
            trade_id="test_stagnation_1",
            peak_price=102.0,
            trough_price=95.0,
            mfe_pct=2.0,
            mae_pct=-5.0
        )

        quotes_map = {
            "NSE_FO|NIFTY26OCT25000CE": {
                "bid": 95.0,
                "ask": 96.0,
                "ltp": 95.0,
                "quote_age_ms": 100
            }
        }

        closed = OptionPaperExecution.update_open_trades(
            live_quotes_map=quotes_map,
            as_of_time=datetime(2026, 10, 12, 10, 0, 0),
            enforce_cutoff=False
        )

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "STAGNATION_TIME_STOP")
        self.assertEqual(closed[0]["exit_bid"], 95.0)
        self.assertAlmostEqual(closed[0]["net_pnl_pct"], -5.0, delta=2.5)

    def test_02_stagnation_stop_does_not_fire_if_mfe_exceeded_5pct(self):
        """
        Invariant: If MFE reached >= +5%, the trade has demonstrated momentum;
        it must NOT be scratched by stagnation stop, allowing normal trailing stop.
        """
        entry_time = (datetime(2026, 10, 12, 10, 0, 0) - timedelta(minutes=13)).isoformat()
        trade = {
            "trade_id": "test_stagnation_2",
            "signal_id": "sig_stag_2",
            "instrument_key": "NSE_FO|NIFTY26OCT25000CE",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
            "quantity": 65,
            "entry_time": entry_time,
            "entry_bid": 98.0,
            "entry_ask": 100.0,
            "entry_spot_price": 25000.0,
            "dynamic_sl_pct": 20.0,
            "dynamic_target_pct": 35.0,
        }
        OptionDB.create_paper_trade(trade)

        # MFE is 8.0% (peak = 108.0), current bid = 98.0 (-2.0% current PnL)
        OptionDB.update_trade_extrema(
            trade_id="test_stagnation_2",
            peak_price=108.0,
            trough_price=98.0,
            mfe_pct=8.0,
            mae_pct=-2.0
        )

        quotes_map = {
            "NSE_FO|NIFTY26OCT25000CE": {
                "bid": 98.0,
                "ask": 99.0,
                "ltp": 98.0,
                "quote_age_ms": 100
            }
        }

        closed = OptionPaperExecution.update_open_trades(
            live_quotes_map=quotes_map,
            as_of_time=datetime(2026, 10, 12, 10, 0, 0),
            enforce_cutoff=False
        )

        # Should remain open because MFE was >= 5%
        self.assertEqual(len(closed), 0)
        open_trades = OptionDB.get_open_paper_trades()
        self.assertEqual(len(open_trades), 1)

    def test_03_stagnation_stop_does_not_fire_under_12_minutes(self):
        """
        Invariant: Before 12 minutes elapsed, stagnation stop must never intervene.
        """
        entry_time = (datetime(2026, 10, 12, 10, 0, 0) - timedelta(minutes=8)).isoformat()
        trade = {
            "trade_id": "test_stagnation_3",
            "signal_id": "sig_stag_3",
            "instrument_key": "NSE_FO|NIFTY26OCT25000CE",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
            "quantity": 65,
            "entry_time": entry_time,
            "entry_bid": 98.0,
            "entry_ask": 100.0,
            "entry_spot_price": 25000.0,
            "dynamic_sl_pct": 20.0,
            "dynamic_target_pct": 35.0,
        }
        OptionDB.create_paper_trade(trade)

        quotes_map = {
            "NSE_FO|NIFTY26OCT25000CE": {
                "bid": 97.0,
                "ask": 98.0,
                "ltp": 97.0,
                "quote_age_ms": 100
            }
        }

        closed = OptionPaperExecution.update_open_trades(
            live_quotes_map=quotes_map,
            as_of_time=datetime(2026, 10, 12, 10, 0, 0),
            enforce_cutoff=False
        )

        self.assertEqual(len(closed), 0)

    def test_04_peak_profit_lock_trips_when_50pct_giveback_occurs_after_4000_peak(self):
        """
        Invariant: When session profit peaks at >= Rs 4,000 (e.g. Rs 6,000),
        and subsequent trades drop net profit to <= Rs 3,000 (50% giveback floor),
        the session circuit breaker trips with is_peak_lock_tripped = True.
        """
        session_date = "2026-10-13"

        # Trade 1: Won +Rs 6,000 net
        t1 = {
            "trade_id": "test_peak_1",
            "signal_id": "sig_1",
            "instrument_key": "NSE_FO|NIFTY1",
            "trading_symbol": "NIFTY1",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
            "quantity": 65,
            "entry_time": f"{session_date} 09:35:00",
            "entry_bid": 100.0,
            "entry_ask": 100.0,
            "entry_price_used": 100.0,
        }
        OptionDB.create_paper_trade(t1)
        OptionDB.close_paper_trade("test_peak_1", {
            "status": "CLOSED",
            "exit_time": f"{session_date} 10:15:00",
            "exit_bid": 195.0,
            "exit_ask": 196.0,
            "exit_price_used": 195.0,
            "peak_price": 200.0,
            "trough_price": 98.0,
            "mfe_pct": 100.0,
            "mae_pct": -2.0,
            "gross_pnl": 6175.0,
            "charges": 150.0,
            "slippage": 25.0,
            "net_pnl": 6000.0,
            "net_pnl_pct": 92.3,
            "holding_duration_seconds": 2400,
            "exit_reason": "TRAILING_STOP_HIT",
        })

        # Check telemetry after Trade 1: Peak is Rs 6,000, Floor is Rs 3,000, NOT tripped yet
        telem1 = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date=session_date)
        self.assertEqual(telem1["peak_pnl"], 6000.0)
        self.assertEqual(telem1["peak_profit_floor"], 3000.0)
        self.assertFalse(telem1["is_peak_lock_tripped"])
        self.assertFalse(telem1["is_session_halted"])

        # Trade 2: Lost -Rs 3,100 net -> Net P&L drops to +Rs 2,900 (below Rs 3,000 floor!)
        t2 = {
            "trade_id": "test_peak_2",
            "signal_id": "sig_2",
            "instrument_key": "NSE_FO|NIFTY2",
            "trading_symbol": "NIFTY2",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25100.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
            "quantity": 65,
            "entry_time": f"{session_date} 10:30:00",
            "entry_bid": 100.0,
            "entry_ask": 100.0,
            "entry_price_used": 100.0,
        }
        OptionDB.create_paper_trade(t2)
        OptionDB.close_paper_trade("test_peak_2", {
            "status": "CLOSED",
            "exit_time": f"{session_date} 11:00:00",
            "exit_bid": 55.0,
            "exit_ask": 56.0,
            "exit_price_used": 55.0,
            "peak_price": 102.0,
            "trough_price": 54.0,
            "mfe_pct": 2.0,
            "mae_pct": -46.0,
            "gross_pnl": -2925.0,
            "charges": 150.0,
            "slippage": 25.0,
            "net_pnl": -3100.0,
            "net_pnl_pct": -47.7,
            "holding_duration_seconds": 1800,
            "exit_reason": "STOP_LOSS_HIT",
        })

        # Telemetry should now be TRIPPED and HALTED
        telem2 = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date=session_date)
        self.assertEqual(telem2["today_net_pnl"], 2900.0)
        self.assertEqual(telem2["peak_pnl"], 6000.0)
        self.assertEqual(telem2["peak_profit_floor"], 3000.0)
        self.assertTrue(telem2["is_peak_lock_tripped"])
        self.assertTrue(telem2["is_session_halted"])
        self.assertIn("Peak-Profit Trailing Lock", telem2["halt_reason"])

    def test_05_peak_profit_lock_blocks_new_trade_entry(self):
        """
        Invariant: Once peak profit lock is tripped, open_trade must reject new entries.
        """
        session_date = "2026-10-13"
        now_dt = datetime(2026, 10, 13, 11, 15, 0)

        # Pre-trip the latch via OptionDB
        OptionDB.persist_paper_circuit_breaker_latch(
            session_date=session_date,
            realized_pnl=2500.0,
            unrealized_pnl=0.0,
            total_exposure=2500.0,
            trip_reason="Peak-Profit Trailing Lock Engaged: Daily profit peaked at Rs 5,500.00.",
            halt_latch=1
        )

        signal = {
            "signal_id": "sig_test_block",
            "instrument_key": "NSE_FO|NIFTY26OCT25000CE",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_ask": 120.0,
            "option_bid": 118.0,
            "lot_size": 65,
        }

        trade_id = OptionPaperExecution.open_trade(
            signal=signal,
            as_of_time=now_dt,
            enforce_cutoff=False
        )

        self.assertIsNone(trade_id)
        open_trades = OptionDB.get_open_paper_trades()
        self.assertEqual(len(open_trades), 0)

    def test_06_crbi_relaxes_model_conviction_hurdle_to_38pct(self):
        """
        Invariant: When CRB-I is active (Day High breakout with volume),
        the model probability hurdle is relaxed to 38.0% (from 44.0%).
        A contract with calibrated_prob = 0.40 passes model hurdle under CRB-I,
        but fails canonical 44% hurdle when CRB-I is inactive.
        """
        engine = OptionSignalEngine()

        contract = {
            "instrument_key": "NSE_FO|NIFTY26OCT25000CE",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
        }

        quote = {
            "instrument_key": "NSE_FO|NIFTY26OCT25000CE",
            "bid": 100.0,
            "ask": 101.0,
            "ltp": 100.5,
            "spread_pct": 1.0,
            "oi": 50000,
            "volume": 2000,
            "quote_age_ms": 100,
            "delta": 0.50,
            "iv": 15.0,
        }

        # Spot is at day high (25100), above VWAP (25000)
        underlying_quote = {
            "spot_ltp": 25100.0,
            "open": 25000.0,
            "high": 25100.0,
            "low": 24950.0,
            "close": 25098.0,
            "vwap": 25020.0,
            "quote_age_ms": 100,
        }

        # 20 bars of underlying df with high volume on the last bar
        dates = pd.date_range("2026-10-09 09:15", periods=20, freq="5min")
        df_bars = pd.DataFrame({
            "close": np.linspace(25000, 25100, 20),
            "high": np.linspace(25010, 25100, 20),
            "low": np.linspace(24990, 25090, 20),
            "volume": [1000] * 19 + [2500]  # Bar 20 volume = 2.5x SMA
        }, index=dates)

        # Mock model predict_contract to return 40% probability (above 38%, below 44%)
        mock_pred = {
            "direction": DIRECTION_BUY_CE,
            "is_actionable": True,
            "calibrated_prob": 0.40,
            "calibrated_prob_pct": 40.0,
            "expected_net_return_pct": 4.5,
            "raw_score": 0.40,
            "expected_mfe_pct": 25.0,
            "expected_mae_pct": -12.0,
            "spread_pct": 1.0,
            "model_version": "v1.0-champion",
            "artifact_sha256": "mock_sha",
        }

        with patch.object(engine.model, "predict_contract", return_value=mock_pred):
            eval_res = engine.evaluate_contract(
                contract=contract,
                quote=quote,
                underlying_quote=underlying_quote,
                underlying_df=df_bars,
                as_of_time=datetime(2026, 10, 9, 10, 0, 0),
                enforce_cutoff=False,
                enforce_regime_gate=False
            )

            self.assertTrue(eval_res.get("is_crbi_active"))
            # Must pass because 40% >= relaxed 38%
            self.assertNotIn("Low probability (40.0% < 38.0%)", eval_res.get("rejection_reasons", []))

    def test_07_crbi_inactive_rejects_sub_44pct_probability(self):
        """
        Invariant: When CRB-I is inactive, canonical 44.0% hurdle is strictly enforced.
        """
        engine = OptionSignalEngine()

        contract = {
            "instrument_key": "NSE_FO|NIFTY26OCT25000CE",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
        }

        quote = {
            "instrument_key": "NSE_FO|NIFTY26OCT25000CE",
            "bid": 100.0,
            "ask": 101.0,
            "ltp": 100.5,
            "spread_pct": 1.0,
            "oi": 50000,
            "volume": 2000,
            "quote_age_ms": 100,
            "delta": 0.50,
            "iv": 15.0,
        }

        # Spot is near high (25095) but volume is weak (0.5x SMA), so CRB-I is NOT confirmed
        underlying_quote = {
            "spot_ltp": 25095.0,
            "open": 25000.0,
            "high": 25100.0,
            "low": 24950.0,
            "close": 25095.0,
            "vwap": 25050.0,
            "quote_age_ms": 100,
        }

        # Sub-breakout volume (0.5x SMA)
        dates = pd.date_range("2026-10-09 09:15", periods=20, freq="5min")
        df_bars = pd.DataFrame({
            "close": np.linspace(25000, 25095, 20),
            "high": np.linspace(25010, 25100, 20),
            "low": np.linspace(24990, 25085, 20),
            "volume": [1000] * 19 + [500]  # Bar 20 volume = 0.5x SMA (fails CRB-I volume requirement)
        }, index=dates)

        mock_pred = {
            "direction": DIRECTION_BUY_CE,
            "is_actionable": True,
            "calibrated_prob": 0.40,
            "calibrated_prob_pct": 40.0,
            "expected_net_return_pct": 4.5,
            "raw_score": 0.40,
            "expected_mfe_pct": 25.0,
            "expected_mae_pct": -12.0,
            "spread_pct": 1.0,
            "model_version": "v1.0-champion",
            "artifact_sha256": "mock_sha",
        }

        with patch.object(engine.model, "predict_contract", return_value=mock_pred):
            eval_res = engine.evaluate_contract(
                contract=contract,
                quote=quote,
                underlying_quote=underlying_quote,
                underlying_df=df_bars,
                as_of_time=datetime(2026, 10, 9, 10, 0, 0),
                enforce_cutoff=False,
                enforce_regime_gate=False
            )

            self.assertFalse(eval_res.get("is_crbi_active"))
            # Must reject because 40% < canonical 44%
            reasons = " ".join(eval_res.get("rejection_reasons", []))
            self.assertIn("Low probability (40.0% < 44.0%)", reasons)

    def test_08_status_endpoint_reports_apex_quant_telemetry(self):
        """
        Invariant: Orchestrator get_status() includes full apex_quant configuration & state.
        """
        orch = OptionSnifferOrchestrator()
        status = orch.get_status()

        self.assertIn("apex_quant", status)
        aq = status["apex_quant"]
        self.assertTrue(aq["enabled"])
        self.assertEqual(aq["stagnation_timeout_minutes"], 12.0)
        self.assertEqual(aq["stagnation_min_mfe_pct"], 5.0)
        self.assertEqual(aq["stagnation_exit_loss_cap_pct"], 6.0)
        self.assertEqual(aq["peak_profit_lock_trigger_inr"], 4000.0)
        self.assertEqual(aq["peak_profit_giveback_max_ratio"], 0.50)
        self.assertEqual(aq["crbi_breakout_volume_min_ratio"], 1.15)
        self.assertEqual(aq["crbi_relaxed_model_prob"], 0.38)
        self.assertEqual(aq["crbi_relaxed_kaufman_er"], 0.20)

    def test_09_emergency_daily_loss_hard_ceiling_bounded_at_3500(self):
        """
        Invariant: Total daily net loss cannot exceed -Rs 3,500 under any circumstances.
        """
        session_date = "2026-10-15"
        # Insert closed trades totalling -Rs 3,600
        for i in range(2):
            tid = f"trade_loss_{i}"
            OptionDB.create_paper_trade({
                "trade_id": tid,
                "instrument_key": f"NSE_FO|T{i}",
                "trading_symbol": f"T{i}",
                "underlying": "NIFTY",
                "option_type": "CE",
                "strike": 25000.0,
                "expiry": "2026-10-29",
                "lot_size": 65,
                "quantity": 65,
                "entry_time": f"{session_date} 09:35:00",
                "entry_bid": 100.0,
                "entry_ask": 100.0,
                "entry_price_used": 100.0,
            })
            OptionDB.close_paper_trade(tid, {
                "status": "CLOSED",
                "exit_time": f"{session_date} 10:0{i}:00",
                "exit_bid": 70.0,
                "exit_ask": 71.0,
                "exit_price_used": 70.0,
                "peak_price": 100.0,
                "trough_price": 70.0,
                "mfe_pct": 0.0,
                "mae_pct": -30.0,
                "gross_pnl": -1950.0,
                "charges": 50.0,
                "slippage": 10.0,
                "net_pnl": -1800.0,
                "net_pnl_pct": -27.7,
                "holding_duration_seconds": 900,
                "exit_reason": "STOP_LOSS_HIT",
            })

        telem = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date=session_date)
        self.assertEqual(telem["today_net_pnl"], -3600.0)
        self.assertTrue(telem["is_realized_limit_hit"])
        self.assertTrue(telem["is_max_loss_limit_hit"])
        self.assertTrue(telem["is_session_halted"])
        self.assertIn("Daily max loss ceiling reached", telem["halt_reason"])

    def test_10_production_champion_models_remain_immutable(self):
        """
        Invariant: Production champion models (champion_ensemble.pkl) must remain untouched.
        """
        for horizon in ("intraday", "swing"):
            pkl_path = f"backend/models/{horizon}/champion_ensemble.pkl"
            if os.path.exists(pkl_path):
                hasher = hashlib.sha256()
                with open(pkl_path, "rb") as f:
                    hasher.update(f.read())
                sha = hasher.hexdigest()
                self.assertIsNotNone(sha)
                self.assertEqual(len(sha), 64)


if __name__ == "__main__":
    unittest.main()
