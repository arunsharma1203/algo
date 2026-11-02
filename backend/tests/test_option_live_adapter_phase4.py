"""
Option Sniffer: Phase 4 Unit & Integration Tests (Exchange Hardening & Extreme Edge Cases).

Covers:
  1. Ambiguous Submission Timeout Recovery (queries order book before marking rejected).
  2. Order Fill Triggers Linked Broker-Side Stop Loss automatically.
  3. Stop-Loss Rejection Triggers Immediate Fail-Closed Auto-Liquidation & Disarm.
  4. Zero-Short SOS Exit Guard (avoids sending sell orders when broker position is already flat).
  5. Remnant Broker Position Sets EXIT_PENDING (never false CONFIRMED_FLAT).
  6. Startup Crash Reconciliation Detects Untracked Broker Positions & Fails Closed.
  7. Emergency Rate Limiter Priority Channel Bypasses Routine Limiting for Exits.
"""

import os
import sqlite3
import tempfile
import unittest
import time
from datetime import datetime
from unittest.mock import patch, MagicMock

from app.analytics.option_sniffer.option_live_order_intent import (
    OrderIntentEngine,
    STATE_VALIDATED,
    STATE_SUBMITTED,
    STATE_FILLED,
    STATE_REJECTED,
    EXIT_NONE,
    EXIT_SUBMITTED,
    EXIT_PENDING,
    EXIT_CONFIRMED_FLAT,
    init_live_order_tables,
    sanitize_upstox_tag,
)
from app.analytics.option_sniffer.option_live_adapter import OptionLiveAdapter, get_option_live_adapter
from app.data.upstox_provider import UpstoxRateLimiter


class TestOptionLiveAdapterPhase4(unittest.TestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        # Patch get_db_path for both modules
        self.patcher1 = patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path)
        self.patcher2 = patch("app.analytics.option_sniffer.option_live_adapter.get_db_path", return_value=self.db_path)
        self.patcher1.start()
        self.patcher2.start()

        # Initialize tables
        with sqlite3.connect(self.db_path) as conn:
            init_live_order_tables()

        self.patcher3 = patch.object(
            OrderIntentEngine,
            "_call_upstox_broker_api",
            return_value=(True, 200, {"status": "success", "data": []})
        )
        self.patcher3.start()

        self.engine = OrderIntentEngine()
        self.adapter = get_option_live_adapter()
        self.adapter._is_live_armed = True
        self.adapter._armed_for_date = datetime.now().strftime("%Y-%m-%d")

    def tearDown(self):
        self.adapter._is_live_armed = False
        self.patcher3.stop()
        self.patcher1.stop()
        self.patcher2.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    @patch("app.analytics.option_sniffer.option_live_order_intent.time.sleep", return_value=None)
    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_ambiguous_submission_timeout_recovery(self, mock_api, mock_sleep):
        """
        When Upstox order placement times out or returns network failure,
        the engine must query the order book to discover if the order exists
        before declaring it rejected, preventing untracked ghost positions.
        """
        signal_payload = {
            "signal_id": "SIG_AMB_001",
            "instrument_key": "NSE_FO|45000",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        live_quote = {"ask": 100.0, "bid": 99.8, "quote_age_ms": 100}
        # Scenario: /order/place returns failure (e.g. 504 Gateway Timeout)
        # But follow-up /order/retrieve-all reveals the order was indeed accepted by exchange
        def api_side_effect(method, endpoint, **kwargs):
            if endpoint == "/portfolio/short-term-positions":
                return True, 200, {"status": "success", "data": []}
            elif endpoint == "/order/place":
                return False, 504, {"error": "Gateway Timeout"}
            elif endpoint == "/order/retrieve-all":
                return True, 200, {
                    "data": [
                        {
                            "order_id": "RECOVERED_EXCHANGE_ORD_101",
                            "tag": tag,
                            "status": "trigger pending",
                            "trading_symbol": "NIFTY 25000 CE"
                        }
                    ]
                }
            return False, 400, {}

        mock_api.side_effect = api_side_effect

        ok, _, intent = self.engine.create_and_validate_intent(signal_payload, live_quote, deployable_capital=15000.0)
        self.assertTrue(ok)
        intent_id = intent["order_intent_id"]
        tag = intent_id[:20]

        with patch("app.analytics.option_sniffer.option_live_order_intent.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 12, 10, 30, 0)
            mock_dt.strptime = datetime.strptime
            with patch.object(self.adapter, "fetch_account_readiness", return_value={"live_trading_eligible": True}):
                res = self.engine.place_real_entry_order(intent_id)

        self.assertEqual(res["status"], "SUBMITTED")
        self.assertEqual(res["broker_order_id"], "RECOVERED_EXCHANGE_ORD_101")
        self.assertTrue(res.get("recovered_after_timeout"))

        # Verify state in SQLite is SUBMITTED with the recovered broker order ID
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT state, broker_order_id FROM option_live_order_intents WHERE order_intent_id = ?;", (intent_id,))
            row = cur.fetchone()
            self.assertEqual(row[0], STATE_SUBMITTED)
            self.assertEqual(row[1], "RECOVERED_EXCHANGE_ORD_101")

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_order_fill_triggers_linked_stop_loss(self, mock_api):
        """
        When order status becomes 'complete', active order reconciliation must
        automatically calculate and submit a linked broker Stop-Loss order.
        """
        intent_id = "INTENT_FILL_SL_001"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, limit_price, state, broker_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (intent_id, "SIG_FILL", "NSE_FO|45000", "NIFTY 25000 CE", "NIFTY", "BUY", 65, 65, 100.0, STATE_SUBMITTED, "UPSTOX_ORD_EXCHANGE_777", now_iso, now_iso))

        # Mock /order/retrieve-all returning complete fill at ₹105.0
        # And mock /order/place returning success for Stop Loss order
        def api_side_effect(method, endpoint, **kwargs):
            if endpoint == "/order/retrieve-all":
                return True, 200, {
                    "data": [
                        {
                            "order_id": "UPSTOX_ORD_EXCHANGE_777",
                            "status": "complete",
                            "average_price": 105.0,
                            "filled_quantity": 65
                        }
                    ]
                }
            elif endpoint == "/order/place" and kwargs.get("payload", {}).get("order_type") == "SL":
                return True, 200, {
                    "status": "success",
                    "data": {"order_id": "EXCHANGE_SL_ORD_888"}
                }
            return False, 400, {}

        mock_api.side_effect = api_side_effect

        updates = self.engine.reconcile_active_order_intents()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["state"], STATE_FILLED)
        self.assertEqual(updates[0]["sl_result"]["status"], "PLACED")
        self.assertEqual(updates[0]["sl_result"]["broker_sl_order_id"], "EXCHANGE_SL_ORD_888")

        # Verify in DB: state = FILLED, average_fill_price = 105.0, broker_sl_order_id = EXCHANGE_SL_ORD_888
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT state, average_fill_price, broker_sl_order_id, stop_loss_trigger FROM option_live_order_intents WHERE order_intent_id = ?;", (intent_id,))
            row = cur.fetchone()
            self.assertEqual(row[0], STATE_FILLED)
            self.assertEqual(row[1], 105.0)
            self.assertEqual(row[2], "EXCHANGE_SL_ORD_888")
            # 105.0 - 20% = 84.0
            self.assertEqual(row[3], 84.0)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_stop_loss_rejection_fail_closed_liquidation(self, mock_api):
        """
        If linked Stop-Loss order placement fails or is rejected by broker,
        engine must engage Emergency Fail-Closed Protocol: disarm live trading
        and auto-liquidate unprotected position at market.
        """
        intent_id = "INTENT_SL_REJ_001"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, broker_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (intent_id, "SIG_SL_REJ", "NSE_FO|45000", "NIFTY 25000 CE", "NIFTY", "BUY", 65, 65, 65, 100.0, 100.0, STATE_FILLED, "BROKER_ORD_REAL_555", now_iso, now_iso))

        # SL placement is rejected by broker, but emergency liquidation sell order succeeds and position reconciles flat
        def sl_rej_side_effect(method, endpoint, payload=None, **kwargs):
            if endpoint == "/order/place":
                tag = (payload or {}).get("tag", "")
                if "SL" in tag:
                    return False, 400, {"errors": [{"message": "RMS: Margin blocked for SL"}]}
                return True, 200, {"status": "success", "data": {"order_id": "AUTO_LIQ_EXIT_ORD_001"}}
            elif endpoint == "/portfolio/short-term-positions":
                return True, 200, {"status": "success", "data": []}
            return True, 200, {"status": "success"}

        mock_api.side_effect = sl_rej_side_effect

        res = self.engine.place_broker_stop_loss_order(intent_id, filled_price=100.0, filled_qty=65)

        self.assertEqual(res["status"], "SL_FAILED_AUTO_LIQUIDATED")
        # Verify live trading was immediately disarmed
        self.assertFalse(self.adapter._is_live_armed)

        # Verify position transitioned to flat / exit in SQLite
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT exit_state FROM option_live_order_intents WHERE order_intent_id = ?;", (intent_id,))
            row = cur.fetchone()
            self.assertEqual(row[0], EXIT_CONFIRMED_FLAT)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_zero_short_sos_exit_guard_when_already_flat(self, mock_api):
        """
        In Indian F&O, sending MARKET SELL when broker net position is 0 opens
        a dangerous naked short option position. If broker position is already 0,
        trigger_sos_panic_exit must cancel SL and dispatch ZERO sell orders.
        """
        intent_id = "INTENT_ZERO_SHORT_001"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, broker_order_id, broker_sl_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (intent_id, "SIG_ZS", "NSE_FO|45000", "NIFTY 25000 CE", "NIFTY", "BUY", 65, 65, 65, 100.0, 100.0, STATE_FILLED, "REAL_BROKER_ORD_111", "REAL_SL_ORD_222", now_iso, now_iso))

        # Broker returns position quantity = 0 (already closed on terminal or by broker SL)
        def api_side_effect(method, endpoint, **kwargs):
            if endpoint == "/portfolio/short-term-positions":
                return True, 200, {
                    "data": [
                        {"instrument_token": "NSE_FO|45000", "quantity": 0, "pnl": 120.0}
                    ]
                }
            elif endpoint == "/order/cancel":
                return True, 200, {"status": "success"}
            elif endpoint == "/order/place":
                # If market sell is called, test should fail!
                self.fail("MARKET SELL order should NEVER be sent when broker position quantity <= 0!")
            return False, 400, {}

        mock_api.side_effect = api_side_effect

        sos_res = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=105.0)

        self.assertEqual(sos_res["status"], "CONFIRMED_FLAT")
        self.assertEqual(sos_res["broker_position_qty"], 0)

        # Verify that /order/cancel was called for SL order, but NO /order/place was called
        calls = [c[0] for c in mock_api.call_args_list]
        called_endpoints = [c[1] for c in calls]
        self.assertIn("/order/cancel", called_endpoints)
        self.assertNotIn("/order/place", called_endpoints)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_sos_exit_remnant_sets_exit_pending(self, mock_api):
        """
        If emergency SOS square-off is executed but post-trade broker position
        verification still shows remaining contracts > 0, system must set
        EXIT_PENDING rather than falsely declaring CONFIRMED_FLAT.
        """
        intent_id = "INTENT_REMNANT_001"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, broker_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (intent_id, "SIG_REM", "NSE_FO|45000", "NIFTY 25000 CE", "NIFTY", "BUY", 65, 65, 65, 100.0, 100.0, STATE_FILLED, "REAL_BROKER_ORD_333", now_iso, now_iso))

        # First position query returns qty=65. Second query (post-exit) returns qty=20 (partial fill on sell)
        query_count = {"cnt": 0}
        def api_side_effect(method, endpoint, **kwargs):
            if endpoint == "/portfolio/short-term-positions":
                query_count["cnt"] += 1
                qty = 65 if query_count["cnt"] == 1 else 20
                return True, 200, {
                    "data": [{"instrument_token": "NSE_FO|45000", "quantity": qty, "pnl": 50.0}]
                }
            elif endpoint == "/order/place":
                return True, 200, {"status": "success", "data": {"order_id": "EXIT_BROKER_ORD_444"}}
            return False, 400, {}

        mock_api.side_effect = api_side_effect

        sos_res = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=102.0)

        self.assertEqual(sos_res["status"], "EXIT_PENDING")
        self.assertEqual(sos_res["exit_state"], EXIT_PENDING)
        self.assertEqual(sos_res["broker_position_qty"], 20)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_startup_reconciliation_detects_untracked_broker_positions(self, mock_api):
        """
        On server startup, if active broker option positions exist that are not
        tracked in SQLite, system must import them and fail-closed (disarm live trading).
        """
        # Mock /portfolio/short-term-positions returning open 65 qty position
        mock_api.return_value = (True, 200, {
            "data": [
                {
                    "instrument_token": "NSE_FO|99999",
                    "trading_symbol": "BANKNIFTY 52000 CE",
                    "quantity": 15,
                    "buy_price": 250.0,
                    "average_price": 250.0
                }
            ]
        })

        rec_res = self.engine.reconcile_broker_startup_state()

        self.assertEqual(rec_res["status"], "RECONCILED")
        self.assertEqual(rec_res["open_positions_count"], 1)
        # Verify live trading was immediately disarmed to protect capital
        self.assertFalse(self.adapter._is_live_armed)

        # Verify untracked position was saved in SQLite as STARTUP_RECOVERY
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT signal_id, instrument_key, trading_symbol, filled_quantity, average_fill_price, state FROM option_live_order_intents WHERE instrument_key = 'NSE_FO|99999';")
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "STARTUP_RECOVERY")
            self.assertEqual(row[1], "NSE_FO|99999")
            self.assertEqual(row[2], "BANKNIFTY 52000 CE")
            self.assertEqual(row[3], 15)
            self.assertEqual(row[4], 250.0)
            self.assertEqual(row[5], STATE_FILLED)

    def test_emergency_rate_limiter_priority_bypass(self):
        """
        Verifies that emergency exit and protection requests bypass rate limiter
        cooldowns and circuit breakers via the is_emergency=True channel.
        """
        from app.data.upstox_provider import UpstoxRateLimitExceeded
        limiter = UpstoxRateLimiter()
        # Artificially trip circuit breaker
        limiter.trip_circuit_breaker(duration=60.0, reason="test 429")
        self.assertTrue(limiter.is_circuit_open())

        # Routine request must fast-reject with UpstoxRateLimitExceeded
        with self.assertRaises(UpstoxRateLimitExceeded):
            limiter.acquire(timeout=0.1, is_emergency=False)

        # Emergency request MUST succeed immediately despite active circuit breaker
        acquired = limiter.acquire(timeout=0.1, is_emergency=True)
        self.assertTrue(acquired)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_partial_fill_cancellation_remainder_places_stop_loss(self, mock_api):
        """
        If an order partially fills and the exchange/broker cancels the remainder,
        the engine must still mark STATE_FILLED for the executed quantity and place
        a linked broker Stop Loss for the exact filled contracts.
        """
        intent_id = "INTENT_PARTIAL_CANCEL_001"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, limit_price, state, broker_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (intent_id, "SIG_PC", "NSE_FO|45000", "NIFTY 25000 CE", "NIFTY", "BUY", 65, 65, 100.0, STATE_SUBMITTED, "BROKER_ORD_PC_999", now_iso, now_iso))

        def api_side_effect(method, endpoint, **kwargs):
            if endpoint == "/order/retrieve-all":
                return True, 200, {
                    "data": [
                        {
                            "order_id": "BROKER_ORD_PC_999",
                            "status": "cancelled",
                            "average_price": 110.0,
                            "filled_quantity": 15
                        }
                    ]
                }
            elif endpoint == "/order/place" and kwargs.get("payload", {}).get("order_type") == "SL":
                # Verify that SL payload requests exactly 15 contracts, not 0 and not 65!
                payload = kwargs.get("payload", {})
                self.assertEqual(payload.get("quantity"), 15)
                return True, 200, {
                    "status": "success",
                    "data": {"order_id": "EXCHANGE_SL_ORD_PC_15"}
                }
            return False, 400, {}

        mock_api.side_effect = api_side_effect

        updates = self.engine.reconcile_active_order_intents()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["state"], STATE_FILLED)
        self.assertTrue(updates[0].get("partial_remainder_cancelled"))
        self.assertEqual(updates[0]["sl_result"]["status"], "PLACED")

        # Verify DB state
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT state, filled_quantity, average_fill_price, broker_sl_order_id FROM option_live_order_intents WHERE order_intent_id = ?;", (intent_id,))
            row = cur.fetchone()
            self.assertEqual(row[0], STATE_FILLED)
            self.assertEqual(row[1], 15)
            self.assertEqual(row[2], 110.0)
            self.assertEqual(row[3], "EXCHANGE_SL_ORD_PC_15")

    def test_background_reconciliation_daemon_detects_pending_orders(self):
        """
        Verifies that start_order_reconciler_daemon starts the daemon and
        daemon recognizes pending orders when live trading is armed.
        """
        intent_id = "INTENT_BG_PENDING_001"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, limit_price, state, broker_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (intent_id, "SIG_BG", "NSE_FO|45000", "NIFTY 25000 CE", "NIFTY", "BUY", 65, 65, 100.0, STATE_SUBMITTED, "BROKER_ORD_BG_123", now_iso, now_iso))

        self.adapter.start_order_reconciler_daemon()
        self.assertTrue(self.adapter._reconciler_running)
        self.assertIsNotNone(self.adapter._reconciler_thread)
        self.assertTrue(self.adapter._reconciler_thread.is_alive())
        self.adapter.stop_order_reconciler_daemon()

    def test_trailing_stop_ratchet_live_exit(self):
        """
        Verifies that when a live position reaches >= +15% MFE (e.g. ₹100 -> ₹120)
        and then pulls back below the 10% trailing stop (₹120 * 0.90 = ₹108),
        check_and_execute_live_exits automatically triggers TRAILING_STOP_HIT.
        """
        intent_id = "INTENT_TRAIL_001"
        now_dt = datetime(2026, 10, 12, 10, 30, 0)
        now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, exit_state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (intent_id, "SIG_TR", "NSE_FO|45000", "NIFTY 25000 CE", "NIFTY", "BUY", 65, 65, 65, 100.0, 100.0, STATE_FILLED, "NONE", now_iso, now_iso))

        with patch("app.analytics.option_sniffer.option_live_order_intent.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 12, 10, 35, 0)
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.strptime = datetime.strptime

            # Tick 1: Price rises to ₹120 (+20% MFE, activates +15% trailing ratchet, stop = max(120*0.90=108, 100*1.02=102) = ₹108)
            closed_1 = self.engine.check_and_execute_live_exits({"NSE_FO|45000": {"bid": 120.0, "ltp": 120.0}})
            self.assertEqual(len(closed_1), 0)

            # Tick 2: Price pulls back to ₹107.0 (<= ₹108.0 ratchet stop) -> triggers TRAILING_STOP_HIT!
            closed_2 = self.engine.check_and_execute_live_exits({"NSE_FO|45000": {"bid": 107.0, "ltp": 107.0}})
            self.assertEqual(len(closed_2), 1)
            self.assertEqual(closed_2[0]["exit_reason"], "TRAILING_STOP_HIT")
            self.assertEqual(closed_2[0]["status"], "CONFIRMED_FLAT")
            self.assertGreater(closed_2[0]["net_realized_pnl"], 0.0)

    def test_golden_entry_window_gate(self):
        """
        Verifies that place_real_entry_order and check_and_dispatch_automated_entries
        strictly block new option entries outside the 09:30-12:45 IST Golden Entry Window.
        """
        signal_payload = {
            "signal_id": "SIG_LATE_001",
            "instrument_key": "NSE_FO|48000",
            "trading_symbol": "NIFTY 25200 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        live_quote = {"ask": 100.0, "bid": 99.8, "quote_age_ms": 100}
        ok, _, intent = self.engine.create_and_validate_intent(signal_payload, live_quote, deployable_capital=30000.0)
        self.assertTrue(ok)

        # Simulate 14:15 IST (outside 09:30-14:00 IST Golden Window, before 15:15 IST cutoff)
        with patch("app.analytics.option_sniffer.option_live_order_intent.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 12, 14, 15, 0)
            mock_dt.strptime = datetime.strptime
            with patch("app.analytics.option_sniffer.option_live_order_intent.is_option_entry_window", return_value=False):
                res = self.engine.place_real_entry_order(intent["order_intent_id"])
                self.assertEqual(res["status"], "BLOCKED")
                self.assertIn("OUTSIDE_GOLDEN_WINDOW", res["reason"])

                dispatched = self.engine.check_and_dispatch_automated_entries({"NSE_FO|48000": live_quote}, 30000.0)
                self.assertEqual(len(dispatched), 0)

    def test_cooldown_qualified_and_stale_signal_rejection(self):
        """
        Verifies that check_and_dispatch_automated_entries rejects:
          1. QUALIFIED signals (blocked by Gate 10 cooldown)
          2. Stale signals (older than 90 seconds)
          3. Signals with missing live WebSocket quotes (no quote_age_ms spoofing)
        """
        now_dt = datetime(2026, 10, 12, 10, 30, 0)
        stale_ts = datetime(2026, 10, 12, 9, 45, 0).isoformat()
        fresh_ts = datetime(2026, 10, 12, 10, 29, 45).isoformat()

        mock_signals = [
            # 1. QUALIFIED status (should be ignored)
            {"signal_id": "SIG_QUAL", "instrument_key": "NSE_FO|501", "trading_symbol": "NIFTY 25000 CE", "underlying": "NIFTY", "action": "BUY_CE", "status": "QUALIFIED", "suggested_entry": 100.0, "calibrated_prob": 0.75, "expected_net_return_pct": 20.0, "timestamp": fresh_ts},
            # 2. ACTIVE but stale timestamp (45 min old -> should be ignored)
            {"signal_id": "SIG_STALE_AGE", "instrument_key": "NSE_FO|502", "trading_symbol": "NIFTY 25100 CE", "underlying": "NIFTY", "action": "BUY_CE", "status": "ACTIVE", "suggested_entry": 100.0, "calibrated_prob": 0.75, "expected_net_return_pct": 20.0, "timestamp": stale_ts},
            # 3. ACTIVE and fresh, but missing from live_quotes_map (should fail closed, not spoof quote_age_ms=100)
            {"signal_id": "SIG_NO_WS", "instrument_key": "NSE_FO|503", "trading_symbol": "NIFTY 25200 CE", "underlying": "NIFTY", "action": "BUY_CE", "status": "ACTIVE", "suggested_entry": 100.0, "calibrated_prob": 0.75, "expected_net_return_pct": 20.0, "timestamp": fresh_ts},
        ]

        with patch("app.analytics.option_sniffer.option_live_order_intent.datetime") as mock_dt:
            mock_dt.now.return_value = now_dt
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.strptime = datetime.strptime
            with patch("app.analytics.option_sniffer.option_live_order_intent.is_option_entry_window", return_value=True):
                with patch.object(self.adapter, "get_read_only_recent_signals", return_value=mock_signals):
                    dispatched = self.engine.check_and_dispatch_automated_entries({}, 30000.0)
                    self.assertEqual(len(dispatched), 0)

    def test_duplicate_instrument_active_position_blocked(self):
        """
        Verifies that Slot 2 cannot open a second position on the exact same
        instrument_key while Slot 1 already holds an active position in that contract.
        """
        sig1 = {
            "signal_id": "SIG_INST_1",
            "instrument_key": "NSE_FO|77777",
            "trading_symbol": "NIFTY 25000 PE",
            "underlying": "NIFTY",
            "action": "BUY_PE",
            "suggested_entry": 100.0
        }
        sig2_same_instrument = {
            "signal_id": "SIG_INST_2",
            "instrument_key": "NSE_FO|77777",
            "trading_symbol": "NIFTY 25000 PE",
            "underlying": "NIFTY",
            "action": "BUY_PE",
            "suggested_entry": 100.0
        }
        live_quote = {"ask": 100.0, "bid": 99.8, "quote_age_ms": 100}

        # Slot 1 opens and fills
        ok1, _, intent1 = self.engine.create_and_validate_intent(sig1, live_quote, deployable_capital=30000.0)
        self.assertTrue(ok1)
        self.engine.simulate_order_execution(intent1["order_intent_id"], scenario="CLEAN_FILL")

        # Slot 2 attempts to open on a new signal_id but the SAME instrument_key -> blocked!
        ok2, reason2, _ = self.engine.create_and_validate_intent(sig2_same_instrument, live_quote, deployable_capital=30000.0)
        self.assertFalse(ok2)
        self.assertIn("DUPLICATE_INSTRUMENT_BLOCKED", reason2)

    def test_upstox_tag_sanitization(self):
        """
        Verifies that Upstox order tags strictly comply with Upstox V2 API specifications:
        Alphanumeric and hyphens only, no underscores, max length 20.
        """
        raw_tag = "INTENT_NIFTY24OCT_1728740000"
        sanitized = sanitize_upstox_tag(raw_tag)
        self.assertLessEqual(len(sanitized), 20)
        self.assertNotIn("_", sanitized)
        self.assertEqual(sanitized, "INTENT-NIFTY24OCT-17")

        sl_tag = sanitize_upstox_tag("SL_INTENT_NIFTY_1234567890")
        self.assertLessEqual(len(sl_tag), 20)
        self.assertNotIn("_", sl_tag)
        self.assertTrue(sl_tag.startswith("SL-INTENT-NIFTY-12"))

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_sos_exit_market_ban_falls_back_to_marketable_limit(self, mock_api):
        """
        When Upstox/NSE rejects a raw MARKET order during emergency liquidation,
        the engine must automatically retry with an aggressive marketable LIMIT order.
        """
        signal_payload = {
            "signal_id": "SIG_MKT_FALLBACK_01",
            "instrument_key": "NSE_FO|99001",
            "trading_symbol": "BANKNIFTY 52000 CE",
            "underlying": "BANKNIFTY",
            "action": "BUY_CE",
            "suggested_entry": 200.0
        }
        live_quote = {"ask": 200.0, "bid": 199.5, "quote_age_ms": 100}
        # Mock API behavior:
        # 1. Position reconcile before intent -> empty (0 quantity)
        # 2. Position reconcile during SOS exit -> quantity = 30
        # 3. First /order/place with order_type: "MARKET" -> rejected by exchange (e.g. F&O market order ban)
        # 4. Second /order/place with order_type: "LIMIT" -> accepted!
        # 5. Position reconcile -> quantity = 0
        call_count = {"market_place": 0, "limit_place": 0, "intent_validated": False}

        def side_effect(method, endpoint, payload=None, **kwargs):
            if endpoint == "/portfolio/short-term-positions":
                if not call_count["intent_validated"]:
                    return True, 200, {"data": []}
                # Return 30 initially, then 0 after limit order executes
                if call_count["limit_place"] > 0:
                    return True, 200, {"data": [{"instrument_token": "NSE_FO|99001", "quantity": 0, "pnl": 500.0}]}
                return True, 200, {"data": [{"instrument_token": "NSE_FO|99001", "quantity": 30, "pnl": 500.0}]}
            elif endpoint == "/order/place":
                if payload and payload.get("order_type") == "MARKET":
                    call_count["market_place"] += 1
                    return False, 400, {"status": "error", "message": "Market orders restricted for options"}
                elif payload and payload.get("order_type") == "LIMIT":
                    call_count["limit_place"] += 1
                    return True, 200, {"status": "success", "data": {"order_id": "RECOVERED_LIMIT_EXIT_ORD_01"}}
            return False, 400, {}

        mock_api.side_effect = side_effect

        ok, _, intent = self.engine.create_and_validate_intent(signal_payload, live_quote, deployable_capital=30000.0)
        self.assertTrue(ok)
        intent_id = intent["order_intent_id"]
        call_count["intent_validated"] = True

        # Simulate fill with a real broker order id
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE option_live_order_intents
                SET state = 'FILLED', filled_quantity = 30, average_fill_price = 200.0, broker_order_id = 'REAL_BROKER_ORD_99', updated_at = ?
                WHERE order_intent_id = ?;
            """, (now_iso, intent_id))
            conn.commit()

        exit_res = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=180.0)
        self.assertEqual(exit_res["status"], "CONFIRMED_FLAT")
        self.assertEqual(call_count["market_place"], 1)
        self.assertEqual(call_count["limit_place"], 1)
        self.assertEqual(exit_res["exit_state"], "CONFIRMED_FLAT")


if __name__ == "__main__":
    unittest.main()

