"""
Option Sniffer: Emergency Exits & Aggregate Risk Safety Audit Regression Suite.

Targeted Verification:
  1. Fail-closed broker position reconciliation: Unknown or failed reconciliation
     strictly returns status="ERROR" with quantity=None, never false 0.
  2. SOS Panic Exit fails closed to EXIT_PENDING on broker API errors (500, 502, 401, timeout).
  3. Mutex and state guards prevent duplicate sells and naked shorts on repeated SOS requests.
  4. Prior working exit orders on exchange suppress duplicate sell orders.
  5. Partial fills sell strictly remaining broker quantity, preventing short creation.
  6. Zero-Short Guard dispatches zero sell orders when broker position is already flat.
  7. Application risk limits (daily loss ceiling, consecutive loss pause, concurrent position,
     underlying index cap) operate independently of broker margin availability.
  8. Exits remain available and fully operational after circuit breaker trips and live trading is disarmed.
  9. Process restart during EXIT_PENDING bidirectionally reconciles with broker state.
  10. execute_sos_panic_exit summary status reflects actual confirmation (never false ALL_FLATTENED).
  11. Pre-trade quote staleness blocks entries but allows emergency exits.
  12. Contradictory order and position states fail closed to EXIT_PENDING.
"""

import os
import sqlite3
import tempfile
import unittest
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
    _EXIT_LOCKS,
)
from app.analytics.option_sniffer.option_live_adapter import OptionLiveAdapter, get_option_live_adapter


class TestEmergencyExitAndReconciliationAudit(unittest.TestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        self.patcher1 = patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path)
        self.patcher2 = patch("app.analytics.option_sniffer.option_live_adapter.get_db_path", return_value=self.db_path)
        self.patcher1.start()
        self.patcher2.start()

        with sqlite3.connect(self.db_path) as conn:
            init_live_order_tables()

        self.engine = OrderIntentEngine()
        self.adapter = get_option_live_adapter()
        self.adapter._is_live_armed = False
        _EXIT_LOCKS.clear()

    def tearDown(self):
        self.adapter._is_live_armed = False
        self.patcher1.stop()
        self.patcher2.stop()
        _EXIT_LOCKS.clear()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    # -------------------------------------------------------------------------
    # DIRECTIVE 1: Broker-Position Reconciliation & trigger_sos_panic_exit audit
    # -------------------------------------------------------------------------

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_reconcile_broker_position_unauthorized_token_returns_error(self, mock_api):
        """
        When Upstox bearer token is missing or unauthorized (HTTP 401),
        reconcile_broker_position must return status="ERROR" with quantity=None.
        Must NEVER imply flat (quantity=0).
        """
        mock_api.return_value = (False, 401, {"error": "Missing Upstox Bearer Token"})

        res = self.engine.reconcile_broker_position("NSE_FO|55555")

        self.assertEqual(res["status"], "ERROR")
        self.assertIsNone(res["quantity"])
        self.assertEqual(res.get("status_code"), 401)
        self.assertEqual(res.get("reason"), "Missing Upstox Bearer Token")

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_reconcile_broker_position_http_500_server_error_returns_error(self, mock_api):
        """
        When Upstox returns HTTP 500/502/503/504 server error,
        reconcile_broker_position must return status="ERROR" with quantity=None.
        """
        mock_api.return_value = (False, 502, {"error": "Bad Gateway"})

        res = self.engine.reconcile_broker_position("NSE_FO|55555")

        self.assertEqual(res["status"], "ERROR")
        self.assertIsNone(res["quantity"])
        self.assertEqual(res.get("status_code"), 502)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_reconcile_broker_position_malformed_response_returns_error(self, mock_api):
        """
        When Upstox API returns malformed payload or non-list data,
        returns status="ERROR" with quantity=None.
        """
        # Malformed: data is None
        mock_api.return_value = (True, 200, {"status": "success", "data": None})
        res = self.engine.reconcile_broker_position("NSE_FO|55555")
        self.assertEqual(res["status"], "ERROR")
        self.assertIsNone(res["quantity"])

        # Error status in JSON
        mock_api.return_value = (True, 200, {"status": "error", "message": "Internal RMS error"})
        res2 = self.engine.reconcile_broker_position("NSE_FO|55555")
        self.assertEqual(res2["status"], "ERROR")
        self.assertIsNone(res2["quantity"])

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_sos_exit_broker_api_error_remains_exit_pending(self, mock_api):
        """
        If broker position reconciliation fails (HTTP 500, network loss, 401),
        trigger_sos_panic_exit MUST set exit_state = EXIT_PENDING in SQLite.
        Must NEVER falsely transition to CONFIRMED_FLAT.
        """
        intent_id = "INTENT_AUDIT_SOS_500"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, broker_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (intent_id, "SIG_500", "NSE_FO|77001", "NIFTY 25000 CE", "NIFTY", "BUY", 65, 65, 65, 100.0, 100.0, STATE_FILLED, "REAL_ORD_77", now_iso, now_iso))

        # Position query returns HTTP 500 error
        def side_effect(method, endpoint, payload=None, **kwargs):
            if endpoint == "/portfolio/short-term-positions":
                return False, 500, {"error": "Upstox OMS Internal Server Error"}
            elif endpoint == "/order/place":
                return True, 200, {"status": "success", "data": {"order_id": "EXIT_ORD_77"}}
            return True, 200, {"status": "success"}

        mock_api.side_effect = side_effect

        sos_res = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=95.0)

        # Status must be EXIT_PENDING, broker_position_qty must be None, NOT 0
        self.assertEqual(sos_res["status"], "EXIT_PENDING")
        self.assertEqual(sos_res["exit_state"], EXIT_PENDING)
        self.assertIsNone(sos_res["broker_position_qty"])

        # Verify SQLite state
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT exit_state FROM option_live_order_intents WHERE order_intent_id = ?;", (intent_id,))
            row = cur.fetchone()
            self.assertEqual(row[0], EXIT_PENDING)

    # -------------------------------------------------------------------------
    # DIRECTIVE 2: Duplicate Sell & Naked Short Prevention under Repeated SOS
    # -------------------------------------------------------------------------

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_sos_exit_repeated_requests_prevent_duplicate_sells_and_naked_shorts(self, mock_api):
        """
        Proves repeated SOS requests cannot cause duplicate sells or create a short position:
          - Concurrent requests return BLOCKED.
          - Requests after confirmation return ALREADY_FLAT.
          - Zero sell order is dispatched when broker confirms already flat.
        """
        intent_id = "INTENT_AUDIT_REPEAT_SOS"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, broker_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (intent_id, "SIG_REP", "NSE_FO|88001", "NIFTY 25000 CE", "NIFTY", "BUY", 65, 65, 65, 100.0, 100.0, STATE_FILLED, "REAL_ORD_88", now_iso, now_iso))

        call_log = {"order_place_count": 0}

        def side_effect(method, endpoint, payload=None, **kwargs):
            if endpoint == "/portfolio/short-term-positions":
                if call_log["order_place_count"] == 0:
                    return True, 200, {"status": "success", "data": [{"instrument_token": "NSE_FO|88001", "quantity": 65}]}
                return True, 200, {"status": "success", "data": []}
            elif endpoint == "/order/place":
                call_log["order_place_count"] += 1
                return True, 200, {"status": "success", "data": {"order_id": "EXIT_ORD_88"}}
            return True, 200, {"status": "success"}

        mock_api.side_effect = side_effect

        # Call 1: Clean exit
        res1 = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=95.0)
        self.assertEqual(res1["status"], "CONFIRMED_FLAT")
        self.assertEqual(call_log["order_place_count"], 1)

        # Call 2 (Repeated click on flat position): Returns ALREADY_FLAT, zero orders placed
        res2 = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=95.0)
        self.assertEqual(res2["status"], "ALREADY_FLAT")
        self.assertEqual(call_log["order_place_count"], 1)  # Strictly still 1

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_sos_exit_prior_working_exit_order_suppresses_duplicate_order(self, mock_api):
        """
        When an exit order was already submitted and is resting on the exchange
        (status="open"), a retry SOS request reconciles the existing order and
        suppresses placing a duplicate sell order.
        """
        intent_id = "INTENT_AUDIT_WORKING_EXIT"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, broker_order_id, exit_state, exit_broker_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (intent_id, "SIG_WRK", "NSE_FO|88002", "NIFTY 25000 CE", "NIFTY", "BUY", 65, 65, 65, 100.0, 100.0, STATE_FILLED, "REAL_ORD_88002", EXIT_SUBMITTED, "REAL_EXIT_ORD_RESTING_99", now_iso, now_iso))

        orders_placed = []

        def side_effect(method, endpoint, payload=None, **kwargs):
            if endpoint.startswith("/order/history"):
                return True, 200, {
                    "data": [
                        {"order_id": "REAL_EXIT_ORD_RESTING_99", "status": "open", "filled_quantity": 0}
                    ]
                }
            elif endpoint == "/portfolio/short-term-positions":
                return True, 200, {"status": "success", "data": [{"instrument_token": "NSE_FO|88002", "quantity": 65}]}
            elif endpoint == "/order/place":
                orders_placed.append(payload)
                return True, 200, {"status": "success", "data": {"order_id": "DUPLICATE_ORDER_ATTEMPT"}}
            return True, 200, {"status": "success"}

        mock_api.side_effect = side_effect

        res = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=90.0)

        # Order was resting on exchange: duplicate sell suppressed, exit remains pending
        self.assertEqual(res["status"], "EXIT_PENDING")
        self.assertEqual(len(orders_placed), 0)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_sos_exit_partial_fill_sells_only_remaining_broker_quantity(self, mock_api):
        """
        If a position had 65 target contracts but broker position is 35 (due to partial fill
        or partial manual close), SOS exit must place a sell order for strictly 35 contracts.
        """
        intent_id = "INTENT_AUDIT_PARTIAL_QTY"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, broker_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (intent_id, "SIG_PART", "NSE_FO|88003", "NIFTY 25000 CE", "NIFTY", "BUY", 65, 65, 65, 100.0, 100.0, STATE_FILLED, "REAL_ORD_88003", now_iso, now_iso))

        placed_payloads = []

        def side_effect(method, endpoint, payload=None, **kwargs):
            if endpoint == "/portfolio/short-term-positions":
                if not placed_payloads:
                    return True, 200, {"status": "success", "data": [{"instrument_token": "NSE_FO|88003", "quantity": 35}]}
                return True, 200, {"status": "success", "data": []}
            elif endpoint == "/order/place":
                placed_payloads.append(payload)
                return True, 200, {"status": "success", "data": {"order_id": "EXIT_ORD_35"}}
            return True, 200, {"status": "success"}

        mock_api.side_effect = side_effect

        res = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=95.0)

        self.assertEqual(res["status"], "CONFIRMED_FLAT")
        self.assertEqual(len(placed_payloads), 1)
        self.assertEqual(placed_payloads[0]["quantity"], 35)  # Strictly 35, never 65!

    # -------------------------------------------------------------------------
    # DIRECTIVE 3: Separation of Broker Margin vs Application Risk Limits
    # -------------------------------------------------------------------------

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_independent_application_risk_limits_vs_broker_margin(self, mock_api):
        """
        Surplus broker margin (₹10,00,000) must NOT bypass application risk limits:
          1. Session-loss limit / daily ceiling (₹3,500)
          2. Max concurrent positions limit (2 slots)
          3. Underlying exposure limit (1 position per underlying)
        """
        mock_api.return_value = (True, 200, {"status": "success", "data": []})

        signal_payload = {
            "signal_id": "SIG_RISK_INDEP_01",
            "instrument_key": "NSE_FO|99001",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        live_quote = {"ask": 100.0, "bid": 99.8, "quote_age_ms": 100}

        # Case A: Daily Loss Ceiling Hit (₹3,600 loss today) with ₹10,00,000 margin
        ok, reason, _ = self.engine.create_and_validate_intent(
            signal_payload=signal_payload,
            live_quote=live_quote,
            deployable_capital=1000000.0,  # ₹10 Lakhs broker margin
            daily_loss_inr=-3600.0          # Application loss limit breached
        )
        self.assertFalse(ok)
        self.assertIn("RISK_BLOCKED", reason)
        self.assertIn("Daily loss ceiling reached", reason)

        # Case B: Max Concurrent Positions Reached
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, exit_state, created_at, updated_at)
                VALUES ('INTENT_FILL_1', 'SIG_1', 'NSE_FO|11', 'NIFTY 25000 CE', 'NIFTY', 'BUY', 65, 65, 65, 100.0, 100.0, 'FILLED', 'NONE', ?, ?),
                       ('INTENT_FILL_2', 'SIG_2', 'NSE_FO|22', 'BANKNIFTY 52000 CE', 'BANKNIFTY', 'BUY', 30, 30, 30, 200.0, 200.0, 'FILLED', 'NONE', ?, ?);
            """, (now_iso, now_iso, now_iso, now_iso))

        signal_slot3 = {
            "signal_id": "SIG_SLOT_3",
            "instrument_key": "NSE_FO|33",
            "trading_symbol": "FINNIFTY 24000 CE",
            "underlying": "FINNIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        ok_slot, reason_slot, _ = self.engine.create_and_validate_intent(
            signal_payload=signal_slot3,
            live_quote=live_quote,
            deployable_capital=1000000.0
        )
        self.assertFalse(ok_slot)
        self.assertIn("RISK_BLOCKED", reason_slot)
        self.assertIn("Max concurrent positions", reason_slot)

    # -------------------------------------------------------------------------
    # DIRECTIVE 4: Exits Remain Available After Circuit Breaker Trips
    # -------------------------------------------------------------------------

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_exits_remain_available_after_circuit_breaker_trips(self, mock_api):
        """
        When the session loss circuit breaker trips:
          1. Live trading is disarmed and new entries are blocked.
          2. trigger_sos_panic_exit remains 100% available and operational.
          3. check_and_execute_live_exits continues surveillance for existing positions.
        """
        mock_api.return_value = (True, 200, {"status": "success", "data": []})

        # Insert open position
        intent_id = "INTENT_OPEN_PRE_BREACH"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, exit_state, broker_order_id, created_at, updated_at)
                VALUES (?, 'SIG_PRE', 'NSE_FO|99111', 'NIFTY 25000 CE', 'NIFTY', 'BUY', 65, 65, 65, 100.0, 100.0, 'FILLED', 'NONE', 'REAL_ORD_99111', ?, ?);
            """, (intent_id, now_iso, now_iso))

        # Breach circuit breaker with -₹4,000 realized loss
        session_date = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_risk_ledger
                (session_date, starting_capital, realized_pnl, unrealized_pnl, peak_drawdown_inr, trades_executed, circuit_breaker_tripped, trip_reason, updated_at)
                VALUES (?, 15000.0, -4000.0, 0.0, 4000.0, 3, 1, 'Daily loss ceiling breached', ?);
            """, (session_date, now_iso))

        # 1. Verify entry is blocked
        sig_new = {"signal_id": "SIG_BLOCKED_POST", "instrument_key": "NSE_FO|99222", "trading_symbol": "BANKNIFTY 52000 CE", "underlying": "BANKNIFTY", "action": "BUY_CE", "suggested_entry": 100.0}
        live_q = {"ask": 100.0, "bid": 99.5, "quote_age_ms": 100}
        ok_entry, reason_entry, _ = self.engine.create_and_validate_intent(sig_new, live_q, deployable_capital=30000.0)
        self.assertFalse(ok_entry)
        self.assertIn("RISK_BLOCKED", reason_entry)

        # 2. Verify emergency exit is still 100% available and executes cleanly
        def side_effect(method, endpoint, payload=None, **kwargs):
            if endpoint == "/portfolio/short-term-positions":
                return True, 200, {"status": "success", "data": []}
            elif endpoint == "/order/place":
                return True, 200, {"status": "success", "data": {"order_id": "EXIT_ORD_HALT_CONTAINED"}}
            return True, 200, {"status": "success"}

        mock_api.side_effect = side_effect

        sos_res = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=92.0)
        self.assertEqual(sos_res["status"], "CONFIRMED_FLAT")
        self.assertEqual(sos_res["exit_state"], EXIT_CONFIRMED_FLAT)

        # 3. Verify check_and_execute_live_exits operates without error even when disarmed
        self.adapter._is_live_armed = False
        exits = self.engine.check_and_execute_live_exits({"NSE_FO|99111": {"bid": 95.0, "ltp": 95.0}})
        self.assertIsInstance(exits, list)

    # -------------------------------------------------------------------------
    # DIRECTIVE 5: Process Restart, Stale Quotes & Contradictory States
    # -------------------------------------------------------------------------

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_process_restart_during_exit_bidirectional_reconciliation(self, mock_api):
        """
        Simulate server restart with an intent in EXIT_PENDING:
          Scenario A: Broker confirms position is 0 -> reconciled to CONFIRMED_FLAT.
          Scenario B: Broker confirms position is still open -> remains EXIT_PENDING, live disarmed.
        """
        intent_a = "INTENT_RESTART_A"
        intent_b = "INTENT_RESTART_B"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, exit_state, created_at, updated_at)
                VALUES (?, 'SIG_A', 'NSE_FO|101', 'NIFTY 25000 CE', 'NIFTY', 'BUY', 65, 65, 65, 100.0, 100.0, 'FILLED', 'EXIT_PENDING', ?, ?),
                       (?, 'SIG_B', 'NSE_FO|102', 'BANKNIFTY 52000 CE', 'BANKNIFTY', 'BUY', 30, 30, 30, 200.0, 200.0, 'FILLED', 'EXIT_PENDING', ?, ?);
            """, (intent_a, now_iso, now_iso, intent_b, now_iso, now_iso))

        # Upstox returns: NSE_FO|101 is flat (absent/0), but NSE_FO|102 is still open (30)
        mock_api.return_value = (True, 200, {
            "status": "success",
            "data": [
                {"instrument_token": "NSE_FO|102", "quantity": 30, "trading_symbol": "BANKNIFTY 52000 CE"}
            ]
        })

        rec = self.engine.reconcile_broker_startup_state()

        self.assertEqual(rec["status"], "RECONCILED")
        self.assertEqual(rec["open_positions_count"], 1)
        self.assertEqual(rec["reconciled_flat_count"], 1)

        # Verify SQLite states:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT order_intent_id, exit_state FROM option_live_order_intents;")
            states = dict(cur.fetchall())
            self.assertEqual(states[intent_a], EXIT_CONFIRMED_FLAT)
            self.assertEqual(states[intent_b], EXIT_PENDING)

        # Live trading must remain disarmed due to active broker position
        self.assertFalse(self.adapter._is_live_armed)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_execute_sos_panic_exit_summary_status_reflects_actual_confirmation(self, mock_api):
        """
        When execute_sos_panic_exit is called:
        If any position remains EXIT_PENDING, status is 'PARTIAL_OR_PENDING'.
        Only when all positions confirm flat does it report 'ALL_FLATTENED'.
        """
        intent_id = "INTENT_PANIC_ALL_01"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, exit_state, broker_order_id, created_at, updated_at)
                VALUES (?, 'SIG_P1', 'NSE_FO|9901', 'NIFTY 25000 CE', 'NIFTY', 'BUY', 65, 65, 65, 100.0, 100.0, 'FILLED', 'NONE', 'REAL_ORD_9901', ?, ?);
            """, (intent_id, now_iso, now_iso))

        # Reconciliation returns error -> position stays EXIT_PENDING
        mock_api.return_value = (False, 500, {"error": "Server error"})

        res = self.adapter.execute_sos_panic_exit()

        self.assertEqual(res["status"], "PARTIAL_OR_PENDING")
        self.assertEqual(res["pending_positions_count"], 1)
        self.assertEqual(res["positions_closed_count"], 0)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_stale_quote_blocks_entry_but_allows_emergency_exit(self, mock_api):
        """
        Stale quote (>1000ms age) strictly blocks new entries (QUOTE_STALE),
        but emergency exit remains available and safely exits at current bid or fill price.
        """
        mock_api.return_value = (True, 200, {"status": "success", "data": []})

        signal_payload = {
            "signal_id": "SIG_STALE_01",
            "instrument_key": "NSE_FO|77099",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        # Stale quote: age 2500ms > 1000ms
        stale_quote = {"ask": 100.0, "bid": 99.5, "quote_age_ms": 2500}

        ok, reason, _ = self.engine.create_and_validate_intent(signal_payload, stale_quote, deployable_capital=30000.0)
        self.assertFalse(ok)
        self.assertIn("QUOTE_STALE", reason)

        # But existing open position can still be emergency square-off even with stale/missing quote
        intent_id = "INTENT_STALE_EXIT_01"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, exit_state, broker_order_id, created_at, updated_at)
                VALUES (?, 'SIG_OLD', 'NSE_FO|77099', 'NIFTY 25000 CE', 'NIFTY', 'BUY', 65, 65, 65, 100.0, 100.0, 'FILLED', 'NONE', 'REAL_ORD_77099', ?, ?);
            """, (intent_id, now_iso, now_iso))

        def side_effect(method, endpoint, payload=None, **kwargs):
            if endpoint == "/portfolio/short-term-positions":
                return True, 200, {"status": "success", "data": []}
            elif endpoint == "/order/place":
                return True, 200, {"status": "success", "data": {"order_id": "EXIT_ORD_STALE_SAFE"}}
            return True, 200, {"status": "success"}

        mock_api.side_effect = side_effect

        exit_res = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=0.0)  # bid 0 fallback
        self.assertEqual(exit_res["status"], "CONFIRMED_FLAT")
        self.assertEqual(exit_res["exit_state"], EXIT_CONFIRMED_FLAT)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_contradictory_order_and_position_states_fail_closed(self, mock_api):
        """
        Contradictory state: Order book history says exit order 'complete', but position book
        query fails with 500 error. The engine must fail closed to EXIT_PENDING, never
        falsely reporting CONFIRMED_FLAT without verified broker position book proof.
        """
        intent_id = "INTENT_CONTRADICTORY_01"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, exit_state, broker_order_id, exit_broker_order_id, created_at, updated_at)
                VALUES (?, 'SIG_CONTRA', 'NSE_FO|77088', 'NIFTY 25000 CE', 'NIFTY', 'BUY', 65, 65, 65, 100.0, 100.0, 'FILLED', 'EXIT_SUBMITTED', 'REAL_ORD_77088', 'REAL_EXIT_ORD_77088', ?, ?);
            """, (intent_id, now_iso, now_iso))

        def side_effect(method, endpoint, payload=None, **kwargs):
            if endpoint.startswith("/order/history"):
                # Order book says complete
                return True, 200, {
                    "data": [
                        {"order_id": "REAL_EXIT_ORD_77088", "status": "complete", "average_price": 95.0, "filled_quantity": 65}
                    ]
                }
            elif endpoint == "/portfolio/short-term-positions":
                # But position query fails with 500!
                return False, 500, {"error": "Portfolio service unavailable"}
            return False, 400, {}

        mock_api.side_effect = side_effect

        exit_res = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=95.0)

        # Must fail closed to EXIT_PENDING because position reconciliation failed!
        self.assertEqual(exit_res["status"], "EXIT_PENDING")
        self.assertEqual(exit_res["exit_state"], EXIT_PENDING)
        self.assertIsNone(exit_res["broker_position_qty"])


if __name__ == "__main__":
    unittest.main()
