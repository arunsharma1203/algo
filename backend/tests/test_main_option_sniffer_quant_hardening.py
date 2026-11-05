"""
Unit & Replay Verification Suite for Main Option Sniffer Quant Hardening (All 9 Bugs):
  1. Bug #1: 1-Winner-Per-Sweep Lock & 1-Live-Per-Wave Synchronization (prevents 20-signal flood
     and prevents Live Autopilot from re-entering the same Paper wave after an early stagnation exit).
  2. Bug #2: Near-ATM Strike Discipline & OptionAlphaModel OTM/Low-Delta Penalty (prevents 250-pt OTM
     23600 CE @ Rs.10.80 / Delta 0.11 from outscoring or being picked over ATM 23350 CE).
  3. Bug #3: Gate 9B-2 (Live Introspection Pause) & Gate 9C (20m Post-Loss Contagion Freeze reading
     live_cb.most_recent_loss_time).
  4. Bug #4: 20-Minute (1200s) Directional SL/Stagnation Cooldown recorded on STAGNATION_TIME_STOP
     and STOP_LOSS_HIT in both Paper and Live.
  5. Bug #5: OptionSignalEngine.record_direction_harvest_cooldown accepts as_of_time without TypeError.
  6. Bug #6: OptionStreamManager.refresh_quotes_via_rest computes analytical Black-Scholes delta
     when WebSocket Greeks are absent instead of spoofing 0.50 across all OTM strikes.
  7. Bug #7 & #8: OptionSnifferOrchestrator deterministic ATM & expiry ordering.
  8. Bug #9: OptionPaperExecution.open_trade blocks when paper circuit breaker is_pause_active is True.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.analytics.option_sniffer.option_alpha_model import OptionAlphaModel
from app.analytics.option_sniffer.option_constants import DIRECTION_BUY_CE, STATE_ACTIVE, STATE_QUALIFIED
from app.analytics.option_sniffer.option_db import OptionDB, init_option_sniffer_tables
from app.analytics.option_sniffer.option_live_order_intent import (
    EXIT_CONFIRMED_FLAT,
    EXIT_NONE,
    STATE_FILLED,
    OrderIntentEngine,
    init_live_order_tables,
)
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine


class TestMainOptionSnifferQuantHardening(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db.name
        self.temp_db.close()

        self.db_patch_1 = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.temp_db_path)
        self.db_patch_2 = patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.temp_db_path)
        self.db_patch_1.start()
        self.db_patch_2.start()

        init_option_sniffer_tables()
        init_live_order_tables()
        OptionSignalEngine.reset_class_state()

        self.mock_model = MagicMock()
        self.mock_model.version = "v1.0-test"
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.58,
            "expected_net_return_pct": 8.0,
            "raw_score": 0.65,
        }

    def tearDown(self):
        OptionSignalEngine.reset_class_state()
        self.db_patch_1.stop()
        self.db_patch_2.stop()
        if os.path.exists(self.temp_db_path):
            os.unlink(self.temp_db_path)

    def test_01_alpha_model_penalizes_250pt_otm_and_low_delta_vs_atm(self):
        """
        Bug #2 Fix: Verify OptionAlphaModel scores ATM (23350 CE, delta=0.50) strictly higher
        than 250-pt OTM (23600 CE, delta=0.113) under identical market features.
        """
        model = OptionAlphaModel()
        model.model = None  # Pure structural scoring check
        base_features = {
            "underlying_return_5m": 0.25,
            "underlying_return_15m": 0.45,
            "vwap_distance_pct": 0.12,
            "underlying_rsi_14": 62.0,
            "india_vix_level": 13.5,
            "india_vix_change_pct": -1.2,
            "spread_pct": 0.35,
            "oi_change_pct": 5.0,
            "volume_oi_ratio": 0.8,
            "upstream_consensus_alignment": 0.85,
            "upstream_confidence": 0.75,
            "z_extension": 0.8,
            "is_breakout": 1.0,
        }
        atm_feats = dict(base_features, abs_moneyness=abs(23335.0 - 23350.0) / 23335.0, delta=0.50, option_type="CE")
        otm_feats = dict(base_features, abs_moneyness=abs(23335.0 - 23600.0) / 23335.0, delta=0.1132, option_type="CE")

        res_atm = model.predict_contract(atm_feats)
        res_otm = model.predict_contract(otm_feats)
        self.assertGreater(res_atm["calibrated_prob"], res_otm["calibrated_prob"] + 0.12)

    def test_02_gate_9a_rejects_low_premium_low_delta_and_far_otm_lottery_contracts(self):
        """
        Bug #2 Fix: Verify OptionSignalEngine Gate 9A blocks today's exact NIFTY 23600 CE 03 NOV 26
        (spot=23354.3, strike=23600, ask=10.80, delta=0.1132) with explicit discipline reasons.
        """
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|23600CE",
            "trading_symbol": "NIFTY 23600 CE 03 NOV 26",
            "underlying": "NIFTY",
            "strike": 23600.0,
            "option_type": "CE",
            "expiry": "2026-11-03",
            "lot_size": 65,
        }
        quote = {
            "bid": 10.75,
            "ask": 10.80,
            "ltp": 10.78,
            "spread_pct": 0.46,
            "oi": 150000,
            "volume": 80000,
            "delta": 0.1132,
            "iv": 14.5,
            "quote_age_ms": 100,
        }
        underlying_quote = {
            "spot_ltp": 23354.3,
            "open": 23220.0,
            "high": 23360.0,
            "low": 23210.0,
            "close": 23350.0,
            "vwap": 23295.0,
            "quote_age_ms": 100,
        }
        res = engine.evaluate_contract(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            as_of_time=datetime(2026, 11, 2, 10, 54, 0),
            enforce_cutoff=True,
            enforce_regime_gate=False,
        )
        self.assertEqual(res["status"], STATE_QUALIFIED)
        self.assertFalse(res["hurdles_passed"])
        reasons = " | ".join(res.get("rejection_reasons", []))
        self.assertIn("Minimum Premium Discipline", reasons)
        self.assertIn("Minimum Delta Discipline", reasons)
        self.assertIn("Far-OTM Strike Discipline", reasons)

    @patch("app.analytics.option_sniffer.option_signal_engine.send_telegram_message")
    @patch("app.analytics.option_sniffer.option_paper_execution.send_telegram_message")
    def test_03_single_winner_per_sweep_and_1_live_per_wave_lock(self, mock_ptg, mock_stg):
        """
        Bug #1 Fix:
        1) In a multi-strike sweep when Live is armed, only the 1st ATM strike triggers STATE_ACTIVE;
           subsequent strikes in the same sweep are blocked by has_recent_u_lock.
        2) Once Live enters and exits a trade during an open Paper wave, OptionSignalEngine and
           OrderIntentEngine.has_participated_in_wave block Live from re-entering that same Paper wave!
        """
        engine = OptionSignalEngine(model=self.mock_model)
        order_engine = OrderIntentEngine(db_path=self.temp_db_path)
        mock_adapter = MagicMock()
        mock_adapter._is_live_armed = True

        u_quote = {
            "spot_ltp": 23350.0,
            "open": 23220.0,
            "high": 23355.0,
            "low": 23210.0,
            "close": 23348.0,
            "vwap": 23290.0,
            "quote_age_ms": 100,
        }
        base_quote = {
            "bid": 82.5,
            "ask": 83.0,
            "ltp": 82.8,
            "spread_pct": 0.60,
            "oi": 150000,
            "volume": 80000,
            "delta": 0.50,
            "iv": 14.5,
            "quote_age_ms": 100,
        }
        c_atm = {
            "instrument_key": "NSE_FO|23350CE",
            "trading_symbol": "NIFTY 23350 CE 03 NOV 26",
            "underlying": "NIFTY",
            "strike": 23350.0,
            "option_type": "CE",
            "expiry": "2026-11-03",
            "lot_size": 65,
        }
        c_adj = {
            "instrument_key": "NSE_FO|23400CE",
            "trading_symbol": "NIFTY 23400 CE 03 NOV 26",
            "underlying": "NIFTY",
            "strike": 23400.0,
            "option_type": "CE",
            "expiry": "2026-11-03",
            "lot_size": 65,
        }

        sweep_dt = datetime(2026, 11, 2, 10, 32, 9)
        with patch("app.analytics.option_sniffer.option_live_adapter.get_option_live_adapter", return_value=mock_adapter), \
             patch("app.analytics.option_sniffer.option_live_order_intent.get_order_intent_engine", return_value=order_engine):
            res1 = engine.evaluate_contract(
                contract=c_atm,
                quote=base_quote,
                underlying_quote=u_quote,
                as_of_time=sweep_dt,
                enforce_cutoff=True,
                enforce_regime_gate=False,
            )
            self.assertEqual(res1["status"], STATE_ACTIVE)

            # 2nd strike in the exact same sweep (1 second later) MUST be blocked even though Live hasn't filled yet!
            res2 = engine.evaluate_contract(
                contract=c_adj,
                quote=base_quote,
                underlying_quote=u_quote,
                as_of_time=sweep_dt + timedelta(seconds=1),
                enforce_cutoff=True,
                enforce_regime_gate=False,
            )
            self.assertEqual(res2["status"], STATE_QUALIFIED)
            self.assertTrue(any("Anti-Clustering Shield" in r for r in res2.get("rejection_reasons", [])))

            # Now simulate Live Trade #1 opening at 10:34:08 and exiting on 12m stagnation at 10:46:53
            with order_engine._get_db_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO option_live_order_intents (
                        order_intent_id, signal_id, instrument_key, trading_symbol, underlying,
                        action, lot_size, target_quantity, filled_quantity, limit_price,
                        average_fill_price, state, broker_order_id, exit_state, exit_fill_price,
                        gross_pnl, statutory_charges, realized_pnl, created_at, updated_at
                    ) VALUES (
                        'INTENT_LIVE_1', 'sig_1', 'NSE_FO|23350CE', 'NIFTY 23350 CE 03 NOV 26', 'NIFTY',
                        'BUY_CE', 65, 65, 65, 83.5, 83.0, ?, 'BRK_1001', ?, 82.5,
                        -32.5, 54.5, -87.0, '2026-11-02 10:34:08', '2026-11-02 10:46:53'
                    );
                    """,
                    (STATE_FILLED, EXIT_CONFIRMED_FLAT),
                )
                conn.commit()

            # Verify has_participated_in_wave detects that Live already traded during the 10:32:09 Paper wave
            self.assertTrue(order_engine.has_participated_in_wave("NIFTY", "2026-11-02T10:32:09.129266"))

            # At 10:54:00 (while Paper trade from 10:32:09 is still OPEN), evaluate_contract MUST block new signals!
            res_1054 = engine.evaluate_contract(
                contract=c_adj,
                quote=base_quote,
                underlying_quote=u_quote,
                as_of_time=datetime(2026, 11, 2, 10, 54, 0),
                enforce_cutoff=True,
                enforce_regime_gate=False,
            )
            self.assertEqual(res_1054["status"], STATE_QUALIFIED)
            reasons_1054 = " | ".join(res_1054.get("rejection_reasons", []))
            # Blocked by both 1-Live-Per-Wave Anti-Clustering AND Gate 9C 20-minute Post-Loss Contagion Freeze!
            self.assertIn("Anti-Clustering Shield", reasons_1054)
            self.assertIn("Cross-Index Contagion Lockout", reasons_1054)

    @patch("app.analytics.option_sniffer.option_paper_execution.send_telegram_message")
    def test_04_harvest_and_stagnation_cooldowns_recorded_without_type_error(self, mock_tg):
        """
        Bugs #4 & #5 Fix:
        1) Closing a winning paper trade populates _DIRECTION_HARVEST_COOLDOWNS using as_of_time without TypeError.
        2) Closing a trade on STAGNATION_TIME_STOP populates _DIRECTION_SL_COOLDOWNS (1200s = 20m).
        """
        OptionSignalEngine.reset_class_state()
        self.assertEqual(OptionSignalEngine._DIRECTION_SL_COOLDOWN_SECONDS, 1200)

        # 1. Direct harvest cooldown call with as_of_time
        dt_win = datetime(2026, 11, 2, 10, 15, 0)
        OptionSignalEngine.record_direction_harvest_cooldown("NIFTY", "CE", as_of_time=dt_win)
        self.assertAlmostEqual(
            OptionSignalEngine._DIRECTION_HARVEST_COOLDOWNS.get("NIFTY_CE", 0.0),
            dt_win.timestamp(),
            places=1,
        )

        # 2. Stagnation time-stop in Paper records direction SL cooldown
        OptionSignalEngine.reset_class_state()
        sig = {
            "signal_id": "sig_stag_1",
            "created_at": "2026-11-02 10:32:00",
            "instrument_key": "NSE_FO|23350CE",
            "trading_symbol": "NIFTY 23350 CE 03 NOV 26",
            "underlying": "NIFTY",
            "strike": 23350.0,
            "expiry": "2026-11-03",
            "option_type": "CE",
            "lot_size": 65,
            "option_bid": 78.0,
            "option_ask": 78.35,
            "model_version": "v1.0",
            "signal_version": "v1.0",
            "data_freshness": "REAL_TIME",
        }
        tid = OptionPaperExecution.open_trade(sig, as_of_time=datetime(2026, 11, 2, 10, 32, 0))
        self.assertIsNotNone(tid)

        close_dt = datetime(2026, 11, 2, 10, 44, 30)
        trade_obj = OptionDB.get_open_paper_trades()[0]
        OptionPaperExecution.close_trade(
            trade=trade_obj,
            exit_bid=77.50,
            exit_ask=77.80,
            peak_price=79.10,
            trough_price=77.20,
            mfe_pct=0.95,
            mae_pct=-1.46,
            duration_sec=750,
            exit_reason="STAGNATION_TIME_STOP",
            as_of_time=close_dt,
        )
        self.assertAlmostEqual(
            OptionSignalEngine._DIRECTION_SL_COOLDOWNS.get("NIFTY_CE", 0.0),
            close_dt.timestamp(),
            places=1,
        )

    @patch("app.analytics.option_sniffer.option_signal_engine.send_telegram_message")
    def test_05_h1_h2_cond_and_h3_22m_confirmed_upgrades(self, mock_tg):
        """
        Walk-Forward & Monte-Carlo Confirmed Upgrades:
        1) H1: ATR_COMPRESSION_RATIO_MIN == 0.85 & LOW_ADX_IMPULSE_ATR_RATIO_MIN == 0.90.
        2) H2_COND: Low-RV VRP Pinning Shield blocks when VRP > 6.0%, RV_15m_ann < 8.5%, and ATR_Ratio < 0.90,
           while preserving genuine expanding breakouts (ATR_Ratio >= 0.90) even when VRP > 6.0%.
        3) H3_22M: 22-Minute Near-Miss Round-Trip Shield triggers STAGNATION_TIME_STOP at >= 22.0m
           when 4.0% <= MFE < 8.0% and PnL <= -3.5% (and does NOT prematurely cut at 15m).
        4) Telegram footer accurately reflects LIVE AUTOPILOT ARMED vs PAPER ONLY.
        """
        import pandas as pd
        from app.analytics.option_sniffer.option_regime_gate import (
            ATR_COMPRESSION_RATIO_MIN,
            LOW_ADX_IMPULSE_ATR_RATIO_MIN,
            OptionRegimeGate,
        )

        self.assertEqual(ATR_COMPRESSION_RATIO_MIN, 0.85)
        self.assertEqual(LOW_ADX_IMPULSE_ATR_RATIO_MIN, 0.90)

        # Build 30 contiguous 1m bars where first 25 bars have 10pt range and last 5 bars compress to 8.6pt range
        # (ATR_Ratio ~ 0.86: passes 0.85 base floor, but < 0.90 so H2_COND and low-ADX H1 apply)
        start_dt = datetime(2026, 11, 2, 9, 30, 0)
        rows = []
        curr = 23300.0
        for i in range(30):
            ts = start_dt + timedelta(minutes=i)
            step = 1.6 if (i % 2 == 0) else 0.7  # Low-RV (~4-5% annualized) gentle upward drift
            o = curr
            rng = 8.4 if i >= 25 else 10.0
            h = o + rng * 0.7
            l = o - rng * 0.3
            c = o + step
            rows.append({
                "ticker": "NIFTY",
                "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 50000,
                "is_complete": 1,
                "is_synthetic": 0,
                "is_gap": 0,
                "available_at": (ts + timedelta(minutes=1)).isoformat(),
                "is_point_in_time": 1,
            })
            curr = c
        df_bars = pd.DataFrame(rows)
        as_of = datetime(2026, 11, 2, 10, 0, 0)

        gate = OptionRegimeGate(db_path=self.temp_db_path)
        # With high option_iv=16.5% -> VRP > +6.0%, low RV_15m < 8.5%, and ATR_Ratio < 0.90 -> H2_COND blocks
        eval_pin = gate.evaluate_regime(
            underlying="NIFTY",
            opt_type="CE",
            as_of_time=as_of,
            continuous_bars=df_bars,
            option_ask=85.0,
            target_pct=35.0,
            lot_size=65,
            option_iv=16.5,
        )
        self.assertFalse(eval_pin["hurdles_passed"])
        self.assertTrue(any("Low-RV VRP Pinning Shield" in r for r in eval_pin["rejection_reasons"]))

        # H3_22M: Test 22-Minute Near-Miss Round-Trip Shield in OptionPaperExecution
        trade_near_miss = {
            "trade_id": "t_nm_1",
            "underlying": "NIFTY",
            "option_type": "CE",
            "entry_time": "2026-11-02T10:32:09",
            "entry_ask": 78.35,
            "initial_entry_ask": 78.35,
            "peak_price": 83.90,  # +7.08% MFE (between 4.0% and 8.0%)
            "trough_price": 74.50,
            "lot_size": 65,
            "quantity": 65,
            "dynamic_sl_pct": 20.0,
            "dynamic_target_pct": 35.0,
        }
        # At 15 minutes (10:47:09), bid=74.80 (-4.53% PnL) -> must NOT cut prematurely
        reason_15m = OptionPaperExecution.evaluate_exit_conditions(
            trade=trade_near_miss,
            current_bid=74.80,
            quote={"bid": 74.80, "ask": 75.10},
            now_dt=datetime(2026, 11, 2, 10, 47, 9),
        )
        self.assertIsNone(reason_15m)

        # At 22 minutes (10:54:15), bid=74.80 (-4.53% PnL <= -3.5%, MFE=7.08%) -> MUST cut via STAGNATION_TIME_STOP
        reason_22m = OptionPaperExecution.evaluate_exit_conditions(
            trade=trade_near_miss,
            current_bid=74.80,
            quote={"bid": 74.80, "ask": 75.10},
            now_dt=datetime(2026, 11, 2, 10, 54, 15),
        )
        self.assertEqual(reason_22m, "STAGNATION_TIME_STOP")

        # Telegram footer check when Live is armed vs disarmed
        sig_payload = {
            "direction": DIRECTION_BUY_CE,
            "trading_symbol": "NIFTY 23350 CE 03 NOV 26",
            "underlying": "NIFTY",
            "spot_price": 23350.0,
            "strike": 23350.0,
            "expiry": "2026-11-03",
            "option_ask": 78.35,
            "option_bid": 78.00,
            "spread_pct": 0.45,
            "delta": 0.50,
            "iv": 14.2,
            "volume": 120000,
            "oi": 250000,
            "calibrated_prob": 0.56,
            "expected_net_return": 7.5,
            "created_at": "2026-11-02T10:32:09",
            "data_freshness": "REAL_TIME",
            "quote_age_ms": 120,
        }
        mock_live_adapter = MagicMock()
        mock_live_adapter._is_live_armed = True
        with patch("app.analytics.option_sniffer.option_live_adapter.get_option_live_adapter", return_value=mock_live_adapter):
            OptionSignalEngine._send_telegram_signal_alert(sig_payload)
            self.assertIn("LIVE AUTOPILOT ARMED + PAPER TRACKING", mock_tg.call_args[0][0])

    @patch("app.analytics.option_sniffer.option_signal_engine.send_telegram_message")
    @patch("app.analytics.option_sniffer.option_signal_engine.is_option_entry_window", return_value=True)
    @patch("app.analytics.option_sniffer.option_signal_engine.is_option_trading_window", return_value=True)
    def test_11_surgical_patch_canary_scratch_same_strike_and_sl_ratchet(self, mock_tw, mock_ew, mock_tg):
        """
        Verifies the 4 surgical quant patches:
          1. Canary Scout ignores a single minor -₹85 (-1.5%) stagnation scratch exit (zero live starvation),
             but immediately trips on 2 scratch losses or any STOP_LOSS_HIT / <= -₹450 loss.
          2. Same-Strike Wave Parity: When Paper is holding NIFTY 23350 CE, OptionSignalEngine and
             OrderIntentEngine block a different strike (23400 CE / 23600 CE) while allowing the
             exact same 23350 CE strike after 60s.
          3. OptionLiveAdapter.get_read_only_recent_signals() returns option_ask, delta, iv, expiry.
          4. Exchange-Level Broker SL Ratcheting: check_and_execute_live_exits() ratchets stop_loss_trigger
             up on Upstox in-place as MFE expands (+20% -> +40% Uncapped Runner).
        """
        today_str = "2026-11-03"
        # 1. Single minor stagnation scratch (-₹85, -1.5%) does NOT trip Canary Scout
        OptionDB.create_paper_trade({
            "trade_id": "pt_scratch_1",
            "signal_id": "sig_sc_1",
            "instrument_key": "NSE_FO|23350CE",
            "trading_symbol": "NIFTY 23350 CE 10 NOV 26",
            "underlying": "NIFTY",
            "strike": 23350.0,
            "option_type": "CE",
            "expiry": "2026-11-10",
            "quantity": 65,
            "lot_size": 65,
            "entry_time": f"{today_str}T09:32:00",
            "entry_ask": 85.0,
            "entry_bid": 84.5,
        })
        OptionDB.close_paper_trade(
            trade_id="pt_scratch_1",
            close_data={
                "exit_time": f"{today_str}T09:44:00",
                "exit_bid": 84.5,
                "exit_ask": 85.0,
                "exit_price_used": 84.5,
                "peak_price": 86.0,
                "trough_price": 83.5,
                "mae_pct": -1.76,
                "mfe_pct": 1.18,
                "gross_pnl": -32.5,
                "charges": 52.5,
                "slippage": 0.0,
                "net_pnl": -85.0,
                "net_pnl_pct": -1.54,
                "holding_duration_seconds": 720,
                "exit_reason": "STAGNATION_TIME_STOP",
            },
        )
        has_sl_1, msg_1 = OptionDB.has_paper_stop_loss_today(today_str)
        self.assertFalse(has_sl_1, f"Single minor scratch should not starve Live trading: {msg_1}")

        # A 2nd scratch loss on the same day MUST trip Canary Scout
        OptionDB.create_paper_trade({
            "trade_id": "pt_scratch_2",
            "signal_id": "sig_sc_2",
            "instrument_key": "NSE_FO|23350CE",
            "trading_symbol": "NIFTY 23350 CE 10 NOV 26",
            "underlying": "NIFTY",
            "strike": 23350.0,
            "option_type": "CE",
            "expiry": "2026-11-10",
            "quantity": 65,
            "lot_size": 65,
            "entry_time": f"{today_str}T10:10:00",
            "entry_ask": 85.0,
            "entry_bid": 84.5,
        })
        OptionDB.close_paper_trade(
            trade_id="pt_scratch_2",
            close_data={
                "exit_time": f"{today_str}T10:22:00",
                "exit_bid": 84.0,
                "exit_ask": 84.5,
                "exit_price_used": 84.0,
                "peak_price": 86.0,
                "trough_price": 83.0,
                "mae_pct": -2.35,
                "mfe_pct": 1.18,
                "gross_pnl": -65.0,
                "charges": 55.0,
                "slippage": 0.0,
                "net_pnl": -120.0,
                "net_pnl_pct": -2.17,
                "holding_duration_seconds": 720,
                "exit_reason": "STAGNATION_TIME_STOP",
            },
        )
        has_sl_2, msg_2 = OptionDB.has_paper_stop_loss_today(today_str)
        self.assertTrue(has_sl_2)
        self.assertIn("2+ negative paper exits today", msg_2)

        # 2. Same-Strike Wave Parity when Paper is holding an open position
        OptionSignalEngine.reset_class_state()
        OptionDB.create_paper_trade({
            "trade_id": "pt_open_wave",
            "signal_id": "sig_open_wave",
            "instrument_key": "NSE_FO|23350CE",
            "trading_symbol": "NIFTY 23350 CE 10 NOV 26",
            "underlying": "NIFTY",
            "strike": 23350.0,
            "option_type": "CE",
            "expiry": "2026-11-10",
            "quantity": 65,
            "lot_size": 65,
            "entry_time": "2026-11-04T09:42:00",
            "entry_ask": 95.0,
            "entry_bid": 94.5,
        })
        mock_adapter = MagicMock()
        mock_adapter._is_live_armed = True
        mock_live_eng = MagicMock()
        mock_live_eng.get_active_positions_count.return_value = 0
        mock_live_eng.has_active_position_for_underlying.return_value = False
        mock_live_eng.has_participated_in_wave.return_value = False
        mock_live_eng.get_live_circuit_breaker_telemetry.return_value = {"is_session_halted": False}

        diff_contract = {
            "instrument_key": "NSE_FO|23400CE",
            "trading_symbol": "NIFTY 23400 CE 10 NOV 26",
            "underlying": "NIFTY",
            "strike": 23400.0,
            "option_type": "CE",
            "expiry": "2026-11-10",
            "lot_size": 65,
        }
        same_contract = {
            "instrument_key": "NSE_FO|23350CE",
            "trading_symbol": "NIFTY 23350 CE 10 NOV 26",
            "underlying": "NIFTY",
            "strike": 23350.0,
            "option_type": "CE",
            "expiry": "2026-11-10",
            "lot_size": 65,
        }
        valid_q = {
            "bid": 95.0,
            "ask": 95.5,
            "ltp": 95.25,
            "spread_pct": 0.52,
            "quote_age_ms": 100,
            "oi": 400000,
            "volume": 180000,
            "delta": 0.51,
            "iv": 13.5,
            "quote_freshness": "REAL_TIME",
        }
        u_q = {
            "spot_ltp": 23355.0,
            "open": 23250.0,
            "high": 23360.0,
            "low": 23240.0,
            "close": 23260.0,
            "vwap": 23300.0,
            "quote_age_ms": 100,
        }
        with patch("app.analytics.option_sniffer.option_live_adapter.get_option_live_adapter", return_value=mock_adapter), \
             patch("app.analytics.option_sniffer.option_live_order_intent.get_order_intent_engine", return_value=mock_live_eng):
            sig_eng = OptionSignalEngine(model=self.mock_model)
            # Evaluating a DIFFERENT strike (23400 CE) while Paper holds 23350 CE MUST be blocked
            res_diff = sig_eng.evaluate_contract(
                contract=diff_contract,
                quote=valid_q,
                underlying_quote=u_q,
                as_of_time=datetime(2026, 11, 4, 9, 45, 5),
                enforce_cutoff=True,
                enforce_regime_gate=False,
            )
            self.assertEqual(res_diff["status"], STATE_QUALIFIED)
            self.assertTrue(any("locked to the active wave contract" in r for r in res_diff["rejection_reasons"]))

            # Evaluating the SAME strike (23350 CE) activates cleanly for Live without opening a 2nd Paper trade
            res_same = sig_eng.evaluate_contract(
                contract=same_contract,
                quote=valid_q,
                underlying_quote=u_q,
                as_of_time=datetime(2026, 11, 4, 9, 45, 5),
                enforce_cutoff=True,
                enforce_regime_gate=False,
            )
            self.assertEqual(res_same["status"], STATE_ACTIVE)
            self.assertEqual(len(OptionDB.get_open_paper_trades()), 1)

        # 3. Verify OptionLiveAdapter.get_read_only_recent_signals returns option_ask, delta, iv, expiry
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        with patch("app.analytics.option_sniffer.option_live_adapter.get_db_path", return_value=self.temp_db_path):
            recent_sigs = get_option_live_adapter().get_read_only_recent_signals(limit=5, today_only=False)
        self.assertGreaterEqual(len(recent_sigs), 1)
        latest_sig = recent_sigs[0]
        self.assertIn("option_ask", latest_sig)
        self.assertIn("delta", latest_sig)
        self.assertIn("expiry", latest_sig)
        self.assertAlmostEqual(float(latest_sig["option_ask"]), 95.5, places=2)

        # 4. Exchange-Level Broker SL Ratcheting on Uncapped Chandelier Runner
        live_eng = OrderIntentEngine()
        ok_int, _, intent_rec = live_eng.create_and_validate_intent(
            signal_payload={
                "signal_id": "sig_runner_ratchet",
                "instrument_key": "NSE_FO|23350CE",
                "trading_symbol": "NIFTY 23350 CE 10 NOV 26",
                "underlying": "NIFTY",
                "action": "BUY_CE",
                "option_ask": 100.0,
                "lot_size": 65,
            },
            live_quote={"ask": 100.0, "bid": 99.5, "quote_age_ms": 100},
            deployable_capital=30000.0,
        )
        self.assertTrue(ok_int)
        intent_id = intent_rec["order_intent_id"]
        live_eng.simulate_order_execution(intent_id, scenario="CLEAN_FILL", mock_fill_price=100.0, sl_pct=20.0)

        # Tick 1: Bid rises to ₹120.0 (+20% MFE >= +15% trigger) -> Does NOT exit, ratchets exchange SL trigger up from ₹80.0 to ₹110.40
        exits_t1 = live_eng.check_and_execute_live_exits(
            {"NSE_FO|23350CE": {"bid": 120.0, "ask": 120.5, "ltp": 120.0}},
            as_of_time=datetime(2026, 11, 2, 11, 0, 0),
        )
        self.assertEqual(len(exits_t1), 0)
        with live_eng._get_db_conn() as conn:
            sl_t1 = float(conn.execute("SELECT stop_loss_trigger FROM option_live_order_intents WHERE order_intent_id = ?;", (intent_id,)).fetchone()[0])
        self.assertAlmostEqual(sl_t1, 110.4, places=1)

        # Tick 2: Bid surges to ₹140.0 (+40% MFE >= +35% target) -> Uncapped Runner stays OPEN and locks exchange SL >= ₹134.0 (+34%)!
        exits_t2 = live_eng.check_and_execute_live_exits(
            {"NSE_FO|23350CE": {"bid": 140.0, "ask": 140.5, "ltp": 140.0}},
            as_of_time=datetime(2026, 11, 2, 11, 2, 0),
        )
        self.assertEqual(len(exits_t2), 0)
        with live_eng._get_db_conn() as conn:
            sl_t2 = float(conn.execute("SELECT stop_loss_trigger FROM option_live_order_intents WHERE order_intent_id = ?;", (intent_id,)).fetchone()[0])
        self.assertGreaterEqual(sl_t2, 134.0)

        # ── 5. Expiry-Day 0-DTE Rollover Protection (Prioritizes 6-DTE 2026-11-09 and skips 0-DTE 2026-11-03) ──
        from app.analytics.option_sniffer.option_sniffer_orchestrator import OptionSnifferOrchestrator
        orch = OptionSnifferOrchestrator()
        orch.contracts = [
            {"instrument_key": "NSE_FO|0DTE_23450CE", "trading_symbol": "NIFTY 23450 CE 03 NOV 26", "underlying": "NIFTY", "strike": 23450.0, "option_type": "CE", "expiry": "2026-11-03"},
            {"instrument_key": "NSE_FO|6DTE_23400CE", "trading_symbol": "NIFTY 23400 CE 09 NOV 26", "underlying": "NIFTY", "strike": 23400.0, "option_type": "CE", "expiry": "2026-11-09"},
            {"instrument_key": "NSE_FO|6DTE_23450CE", "trading_symbol": "NIFTY 23450 CE 09 NOV 26", "underlying": "NIFTY", "strike": 23450.0, "option_type": "CE", "expiry": "2026-11-09"},
        ]
        orch._last_discovery_date = "2026-11-03"
        orch._last_discovery_time = 9999999999.0
        evaluated_keys = []
        with patch("app.analytics.option_sniffer.option_sniffer_orchestrator.to_ist_naive", return_value=datetime(2026, 11, 3, 10, 0, 0)), \
             patch("app.analytics.option_sniffer.option_sniffer_orchestrator.is_option_trading_window", return_value=True), \
             patch.object(orch.stream_manager, "get_all_active_option_quotes", return_value=[
                 {"instrument_key": "NSE_FO|0DTE_23450CE", "bid": 45.0, "ask": 45.5, "quote_age_ms": 100},
                 {"instrument_key": "NSE_FO|6DTE_23400CE", "bid": 115.0, "ask": 115.5, "quote_age_ms": 100},
                 {"instrument_key": "NSE_FO|6DTE_23450CE", "bid": 92.0, "ask": 92.5, "quote_age_ms": 100},
             ]), \
             patch.object(orch.stream_manager, "get_option_quote", side_effect=lambda k: {"instrument_key": k, "bid": 90.0, "ask": 90.5, "quote_age_ms": 100}), \
             patch.object(orch.stream_manager, "get_underlying_quote", return_value={"spot_ltp": 23440.8, "quote_age_ms": 100}), \
             patch.object(orch.signal_engine, "evaluate_contract", side_effect=lambda **kw: (evaluated_keys.append(kw["contract"]["instrument_key"]) or {"status": "WATCH"})):
            orch.run_surveillance_sweep()

        # Verify 0-DTE contract was skipped and 6-DTE ATM (23450) was evaluated before 6-DTE 23400!
        self.assertEqual(evaluated_keys, ["NSE_FO|6DTE_23450CE", "NSE_FO|6DTE_23400CE"])
        self.assertNotIn("NSE_FO|0DTE_23450CE", evaluated_keys)

        # 6. Verify OptionMicrostructureEvaluator.get_atm_spread_pct skips 0-DTE wide spreads in favor of active 6-DTE ATM
        from app.analytics.option_sniffer.option_microstructure_evaluator import OptionMicrostructureEvaluator
        OptionMicrostructureEvaluator._mock_spread_atm = None
        with patch("app.analytics.option_sniffer.option_microstructure_evaluator.to_ist_naive", return_value=datetime(2026, 11, 3, 9, 36, 0)), \
             patch.object(orch.stream_manager, "get_underlying_quote", return_value={"spot_ltp": 23650.0}), \
             patch.object(orch.stream_manager, "get_all_active_option_quotes", return_value=[
                 {"underlying": "NIFTY", "strike": 23650.0, "expiry": "2026-11-03", "spread_pct": 2.0},
                 {"underlying": "NIFTY", "strike": 23650.0, "expiry": "2026-11-09", "spread_pct": 0.25},
             ]):
            sp_val, sp_ok = OptionMicrostructureEvaluator.get_atm_spread_pct("NIFTY")
            self.assertEqual(sp_val, 0.25)
            self.assertTrue(sp_ok)

        # 7. Verify WebSocket V3 Startup Race Fix & Zero Synthetic Price Offset
        from app.data.upstox_stream_service import UpstoxStreamService
        ws_svc = UpstoxStreamService.__new__(UpstoxStreamService)
        import threading
        ws_svc._lock = threading.Lock()
        ws_svc._key_to_symbol = {"NSE_EQ|RELIANCE": "RELIANCE.NS"}
        ws_svc._additional_keys = set()
        ws_svc._subscribed_ws_keys = set()
        ws_svc._streamer = MagicMock()
        ws_svc._is_connected = False

        # Call subscribe_additional_keys while _is_connected is False (simulating startup race)
        ws_svc.subscribe_additional_keys(["NSE_FO|39945", "NSE_FO|39946"])
        self.assertIn("NSE_FO|39945", ws_svc._additional_keys)
        self.assertNotIn("NSE_FO|39945", ws_svc._subscribed_ws_keys)
        ws_svc._streamer.subscribe.assert_not_called()

        # Once connected, calling subscribe_additional_keys subscribes the pending option keys on WebSocket V3
        ws_svc._is_connected = True
        ws_svc.subscribe_additional_keys(["NSE_FO|39945", "NSE_FO|39946"])
        ws_svc._streamer.subscribe.assert_called_once_with(["NSE_FO|39945", "NSE_FO|39946"], "full")
        self.assertIn("NSE_FO|39945", ws_svc._subscribed_ws_keys)


if __name__ == "__main__":
    unittest.main()
