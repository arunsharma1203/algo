"""
Unit & Integration Tests for Option Sniffer Institutional Dynamic Paper Engine (V2).

Verifies:
  1. Spot ATR x Delta mapping accuracy for NIFTY and BANKNIFTY.
  2. Volatility clamping ([12.0%, 24.0%] for SL, [25.0%, 45.0%] for Target).
  3. Dynamic trade record creation and SQLite persistence.
  4. Dynamic exit hurdle evaluation (Target Met, Dynamic SL Hit, Dynamic Trailing Ratchet).
  5. 100% Reversibility & Backwards Compatibility with V1_CLASSIC trades.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from app.analytics.option_sniffer.option_constants import (
    STOP_LOSS_PCT,
    TARGET_GAIN_PCT,
    DYNAMIC_SL_MIN_PCT,
    DYNAMIC_SL_MAX_PCT,
    DYNAMIC_RR_RATIO,
    DYNAMIC_TGT_MIN_PCT,
    DYNAMIC_TGT_MAX_PCT,
    OPTION_EXECUTION_ENGINE_MODE,
)
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.option_sniffer.option_db import OptionDB, init_option_sniffer_tables


class TestOptionDynamicPaperEngine(unittest.TestCase):
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

        self.mode_patcher1 = patch("app.analytics.option_sniffer.option_paper_execution.OPTION_EXECUTION_ENGINE_MODE", "DYNAMIC_V2")
        self.mode_patcher1.start()
        self.mode_patcher2 = patch("app.analytics.option_sniffer.option_live_order_intent.OPTION_EXECUTION_ENGINE_MODE", "DYNAMIC_V2")
        self.mode_patcher2.start()
        from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
        OptionSignalEngine.reset_class_state()

    def tearDown(self):
        from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
        OptionSignalEngine.reset_class_state()
        self.mode_patcher2.stop()
        self.mode_patcher1.stop()
        self.live_db_patcher.stop()
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_calculate_dynamic_sl_and_target_nifty_normal(self):
        """
        On normal ATM NIFTY option (₹145, Spot ATR ~40, Delta 0.50),
        SL should be ~13.8% and Target should be ~25.0% (convex 1.75:1).
        """
        sl, tgt, atr, delta = OptionPaperExecution.calculate_dynamic_sl_and_target(
            underlying="NIFTY",
            spot_price=25000.0,
            option_ask=145.0,
            delta=0.50,
            spot_atr_pct=0.80
        )
        self.assertAlmostEqual(delta, 0.50, places=2)
        self.assertGreaterEqual(atr, 30.0)
        self.assertLessEqual(atr, 65.0)
        self.assertGreaterEqual(sl, DYNAMIC_SL_MIN_PCT)
        self.assertLessEqual(sl, DYNAMIC_SL_MAX_PCT)
        self.assertGreaterEqual(tgt, DYNAMIC_TGT_MIN_PCT)
        self.assertLessEqual(tgt, DYNAMIC_TGT_MAX_PCT)

    def test_calculate_dynamic_sl_and_target_expensive_clamped(self):
        """
        On high-premium NIFTY option (₹250), raw SL % is low (~9%),
        which must clamp to DYNAMIC_SL_MIN_PCT (18.0%) to prevent noise stop-outs.
        """
        sl, tgt, atr, delta = OptionPaperExecution.calculate_dynamic_sl_and_target(
            underlying="NIFTY",
            spot_price=25000.0,
            option_ask=250.0,
            delta=0.50,
            spot_atr_pct=0.80
        )
        self.assertEqual(sl, DYNAMIC_SL_MIN_PCT)  # 18.0% floor
        self.assertEqual(tgt, round(DYNAMIC_SL_MIN_PCT * DYNAMIC_RR_RATIO, 1))  # 31.5% target

    def test_calculate_dynamic_sl_and_target_cheap_clamped(self):
        """
        On low-premium NIFTY option (₹40), raw SL % is high (~50%),
        which must clamp to DYNAMIC_SL_MAX_PCT (22.0%) to protect capital.
        """
        sl, tgt, atr, delta = OptionPaperExecution.calculate_dynamic_sl_and_target(
            underlying="NIFTY",
            spot_price=25000.0,
            option_ask=40.0,
            delta=0.50,
            spot_atr_pct=0.80
        )
        self.assertEqual(sl, DYNAMIC_SL_MAX_PCT)  # 22.0% ceiling
        self.assertEqual(tgt, round(DYNAMIC_SL_MAX_PCT * DYNAMIC_RR_RATIO, 1))  # 38.5% target

    def test_calculate_dynamic_sl_and_target_banknifty(self):
        """
        On BANKNIFTY option (₹450, Spot ATR ~100, Delta 0.52),
        Spot ATR should scale to BANKNIFTY bounds (70-160 pts).
        """
        sl, tgt, atr, delta = OptionPaperExecution.calculate_dynamic_sl_and_target(
            underlying="BANKNIFTY",
            spot_price=54000.0,
            option_ask=450.0,
            delta=0.52,
            spot_atr_pct=0.80
        )
        self.assertAlmostEqual(delta, 0.52, places=2)
        self.assertGreaterEqual(atr, 70.0)
        self.assertLessEqual(atr, 160.0)
        self.assertGreaterEqual(sl, DYNAMIC_SL_MIN_PCT)
        self.assertLessEqual(sl, DYNAMIC_SL_MAX_PCT)

    def test_open_trade_records_dynamic_v2_parameters(self):
        """
        Verifies open_trade creates record with engine_version='DYNAMIC_V2'
        and accurately computed dynamic SL and Target percentages in SQLite.
        """
        signal = {
            "signal_id": "SIG_DYN_TEST_01",
            "instrument_key": "NSE_FO|12345",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-15",
            "lot_size": 65,
            "option_ask": 150.0,
            "option_bid": 149.5,
            "spot_price": 25000.0,
            "delta": 0.52,
            "spot_atr_pct": 0.80
        }

        trade_id = OptionPaperExecution.open_trade(signal, enforce_cutoff=False)
        self.assertIsNotNone(trade_id)

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT engine_version, dynamic_sl_pct, dynamic_target_pct, spot_atr, delta_used
                FROM option_sniffer_paper_trades WHERE trade_id = ?;
            """, (trade_id,))
            row = cur.fetchone()
            self.assertIsNotNone(row)
            engine_ver, dyn_sl, dyn_tgt, s_atr, d_used = row
            self.assertEqual(engine_ver, "DYNAMIC_V2")
            self.assertGreater(dyn_sl, 0)
            self.assertGreater(dyn_tgt, 0)
            self.assertGreater(s_atr, 0)
            self.assertEqual(d_used, 0.52)

    def test_update_open_trades_triggers_dynamic_target(self):
        """
        When a DYNAMIC_V2 trade reaches its dynamically calculated target,
        it must exit with TARGET_MET.
        """
        signal = {
            "signal_id": "SIG_DYN_TGT_01",
            "instrument_key": "NSE_FO|22222",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-15",
            "lot_size": 65,
            "option_ask": 100.0,
            "option_bid": 99.5,
            "spot_price": 25000.0,
            "delta": 0.50,
            "spot_atr_pct": 0.80
        }
        trade_id = OptionPaperExecution.open_trade(signal, enforce_cutoff=False)

        # Get the assigned dynamic target
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT dynamic_target_pct FROM option_sniffer_paper_trades WHERE trade_id = ?;", (trade_id,))
            dyn_tgt = cur.fetchone()[0]

        # Bid reaches the dynamic target price
        target_bid = 100.0 * (1.0 + (dyn_tgt / 100.0) + 0.01)
        quotes = {
            "NSE_FO|22222": {"bid": target_bid, "ask": target_bid + 0.5}
        }
        closed = OptionPaperExecution.update_open_trades(quotes)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "TARGET_MET")

    def test_update_open_trades_triggers_dynamic_stop(self):
        """
        When a DYNAMIC_V2 trade drops to its dynamically calculated stop,
        it must exit with STOP_LOSS_HIT.
        """
        signal = {
            "signal_id": "SIG_DYN_SL_01",
            "instrument_key": "NSE_FO|33333",
            "trading_symbol": "NIFTY 25000 PE",
            "underlying": "NIFTY",
            "option_type": "PE",
            "strike": 25000.0,
            "expiry": "2026-10-15",
            "lot_size": 65,
            "option_ask": 100.0,
            "option_bid": 99.5,
            "spot_price": 25000.0,
            "delta": 0.50,
            "spot_atr_pct": 0.80
        }
        trade_id = OptionPaperExecution.open_trade(signal, enforce_cutoff=False)

        # Get the assigned dynamic SL
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT dynamic_sl_pct FROM option_sniffer_paper_trades WHERE trade_id = ?;", (trade_id,))
            dyn_sl = cur.fetchone()[0]

        # Bid drops below the dynamic SL price
        sl_bid = 100.0 * (1.0 - (dyn_sl / 100.0) - 0.01)
        quotes = {
            "NSE_FO|33333": {"bid": sl_bid, "ask": sl_bid + 0.5}
        }
        closed = OptionPaperExecution.update_open_trades(quotes)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "STOP_LOSS_HIT")

    def test_engine_reversibility_fallback_to_v1_classic(self):
        """
        Verifies that legacy or explicitly tagged 'V1_CLASSIC' trades
        faithfully use the original static STOP_LOSS_PCT (20%) and TARGET_GAIN_PCT (35%).
        """
        signal = {
            "signal_id": "SIG_CLASSIC_01",
            "instrument_key": "NSE_FO|44444",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-15",
            "lot_size": 65,
            "option_ask": 100.0,
            "option_bid": 99.5,
            "spot_price": 25000.0,
            "delta": 0.50,
            "spot_atr_pct": 0.80
        }

        with patch("app.analytics.option_sniffer.option_paper_execution.OPTION_EXECUTION_ENGINE_MODE", "V1_CLASSIC"):
            trade_id = OptionPaperExecution.open_trade(signal, enforce_cutoff=False)

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT engine_version, dynamic_sl_pct, dynamic_target_pct FROM option_sniffer_paper_trades WHERE trade_id = ?;", (trade_id,))
            engine_ver, sl_p, tgt_p = cur.fetchone()
            self.assertEqual(engine_ver, "V1_CLASSIC")
            self.assertEqual(sl_p, STOP_LOSS_PCT)  # 20.0%
            self.assertEqual(tgt_p, TARGET_GAIN_PCT)  # 35.0%

        # Pullback to -18% must NOT trigger stop in V1_CLASSIC (because classic stop is 20%)
        quotes_18 = {"NSE_FO|44444": {"bid": 82.0, "ask": 82.5}}
        closed_18 = OptionPaperExecution.update_open_trades(quotes_18, enforce_cutoff=False)
        self.assertEqual(len(closed_18), 0)

        # Pullback to -21% triggers classic stop
        quotes_21 = {"NSE_FO|44444": {"bid": 79.0, "ask": 79.5}}
        closed_21 = OptionPaperExecution.update_open_trades(quotes_21, enforce_cutoff=False)
        self.assertEqual(len(closed_21), 1)
        self.assertEqual(closed_21[0]["exit_reason"], "STOP_LOSS_HIT")

    def test_real_trade_intent_dynamic_sl_and_target_stored(self):
        """
        Verifies that OrderIntentEngine in Dynamic V2 mode sets dynamic_sl_pct and
        dynamic_target_pct on live order intents, safely clearing the dynamic rupee risk check.
        """
        from app.analytics.option_sniffer.option_live_order_intent import OrderIntentEngine

        engine = OrderIntentEngine()
        signal_payload = {
            "signal_id": "SIG_LIVE_DYN_001",
            "instrument_key": "NSE_FO|55555",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 220.0
        }
        live_quote = {"ask": 220.0, "bid": 219.5, "quote_age_ms": 100}

        ok, reason, intent = engine.create_and_validate_intent(signal_payload, live_quote, deployable_capital=30000.0)
        self.assertTrue(ok, f"Validation failed: {reason}")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.get("engine_version"), "DYNAMIC_V2")
        # For ₹220 option, dynamic SL clamps to 18.0%
        self.assertEqual(intent.get("dynamic_sl_pct"), 18.0)
        self.assertEqual(intent.get("dynamic_target_pct"), 31.5)

        # Verify DB persistence
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT dynamic_sl_pct, dynamic_target_pct, engine_version FROM option_live_order_intents WHERE order_intent_id = ?;", (intent["order_intent_id"],))
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 18.0)
            self.assertEqual(row[1], 31.5)
            self.assertEqual(row[2], "DYNAMIC_V2")

    def test_real_trade_intent_broker_sl_uses_dynamic_sl(self):
        """
        Verifies that place_broker_stop_loss_order uses the intent's dynamic_sl_pct (18.0%)
        instead of the static 20%, placing the resting exchange stop-loss trigger at ₹180.4.
        """
        from app.analytics.option_sniffer.option_live_order_intent import OrderIntentEngine
        from unittest.mock import MagicMock

        engine = OrderIntentEngine()
        signal_payload = {
            "signal_id": "SIG_LIVE_DYN_002",
            "instrument_key": "NSE_FO|66666",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 220.0
        }
        live_quote = {"ask": 220.0, "bid": 219.5, "quote_age_ms": 100}

        ok, _, intent = engine.create_and_validate_intent(signal_payload, live_quote, deployable_capital=30000.0)
        self.assertTrue(ok)
        intent_id = intent["order_intent_id"]

        with patch("app.analytics.option_sniffer.option_live_adapter.get_option_live_adapter") as mock_adapter_getter:
            mock_adapter = MagicMock()
            mock_adapter._is_live_armed = True
            mock_adapter_getter.return_value = mock_adapter

            captured_requests = []
            def mock_api_call(method, endpoint, payload=None, is_emergency=False):
                captured_requests.append({"endpoint": endpoint, "payload": payload})
                return True, 200, {"status": "success", "data": {"order_id": "UPSTOX_SL_DYN_01"}}

            with patch.object(engine, "_call_upstox_broker_api", side_effect=mock_api_call):
                res = engine.place_broker_stop_loss_order(
                    order_intent_id=intent_id,
                    filled_price=220.0,
                    filled_qty=65
                )

                self.assertEqual(res["status"], "PLACED")
                # 220.0 * (1 - 0.18) = 180.4
                self.assertEqual(res["trigger_price"], 180.4)
                self.assertEqual(len(captured_requests), 1)
                self.assertEqual(captured_requests[0]["payload"]["trigger_price"], 180.4)

    def test_calculate_chandelier_stop_tiers(self):
        """
        Verifies continuous Chandelier trailing stop across all dynamic volatility tiers:
          - Sub-breakeven (< +15.0%): returns None
          - Tier 1 (+15.0% to +21.9%): 8.0% below peak
          - Early Breakeven Floor (>= +18.0%): minimum Entry + 6.0% locked
          - Tier 2 (+22.0% to +29.9%): 6.0% below peak
          - Tier 3 Runner (>= +30.0%): 4.5% below peak
        """
        entry = 100.0

        # Sub-breakeven: MFE 5.0% and 10.0% -> None (below 15.0% trigger)
        self.assertIsNone(OptionPaperExecution.calculate_chandelier_stop(entry, 105.0, 5.0))
        self.assertIsNone(OptionPaperExecution.calculate_chandelier_stop(entry, 110.0, 10.0))

        # Tier 1: MFE 16.0%, Peak 116.0 -> 116.0 * (1 - 0.08) = 106.72
        self.assertAlmostEqual(OptionPaperExecution.calculate_chandelier_stop(entry, 116.0, 16.0), 106.72, places=2)

        # Early Breakeven + Tier 1: MFE 18.0%, Peak 118.0 -> max(118.0 * 0.92 = 108.56, 100 * 1.06 = 106.0) = 108.56
        self.assertAlmostEqual(OptionPaperExecution.calculate_chandelier_stop(entry, 118.0, 18.0), 108.56, places=2)

        # Tier 2: MFE 25.0%, Peak 125.0 -> 125.0 * (1 - 0.06) = 117.50
        self.assertAlmostEqual(OptionPaperExecution.calculate_chandelier_stop(entry, 125.0, 25.0), 117.50, places=2)

        # Tier 3: MFE 35.0%, Peak 135.0 -> 135.0 * (1 - 0.045) = 128.925
        self.assertAlmostEqual(OptionPaperExecution.calculate_chandelier_stop(entry, 135.0, 35.0), 128.925, places=2)

    def test_update_open_trades_chandelier_exits_runner_at_high_profit(self):
        """
        Verifies that a runner position with +32% MFE ratchets its stop to 4.5% below peak
        (132.0 * 0.955 = 126.06) and exits when bid pulls back to 125.0, locking in > +25% profit.
        """
        signal = {
            "signal_id": "SIG_RUNNER_01",
            "instrument_key": "NSE_FO|55555",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-15",
            "lot_size": 65,
            "option_ask": 100.0,
            "option_bid": 99.5,
            "spot_price": 25000.0,
            "delta": 0.50,
            "spot_atr_pct": 0.80
        }
        trade_id = OptionPaperExecution.open_trade(signal, enforce_cutoff=False)

        # Peak reaches 132.0 (+32% MFE)
        OptionDB.update_trade_extrema(
            trade_id=trade_id,
            peak_price=132.0,
            trough_price=99.0,
            mfe_pct=32.0,
            mae_pct=-1.0
        )

        # Bid pulls back to 125.0 (below 126.06 chandelier stop)
        quotes = {
            "NSE_FO|55555": {"bid": 125.0, "ask": 125.5}
        }
        closed = OptionPaperExecution.update_open_trades(quotes)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "TRAILING_STOP_HIT")
        self.assertGreater(closed[0]["net_pnl"], 1500.0)  # Locked high profit


if __name__ == "__main__":
    unittest.main()


