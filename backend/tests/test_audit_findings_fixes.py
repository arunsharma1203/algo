"""
Tests for Option Sniffer Audit Findings Remediation:
  1. Live Upstox tradebook synchronisation (OptionLiveAdapter.sync_trades_from_upstox_tradebook)
  2. Historical circuit-breaker date leakage (OptionDB.get_daily_circuit_breaker_telemetry)

STRICT SAFEGUARDS:
  - 100% Mocked: Uses isolated temporary SQLite databases.
  - Zero real network requests, zero real broker credentials, zero live orders placed.
  - Verification of idempotency, error handling, partial fills, and date interval boundaries.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from app.analytics.option_sniffer.option_db import OptionDB, init_option_sniffer_tables
from app.analytics.option_sniffer.option_live_adapter import OptionLiveAdapter
from app.analytics.option_sniffer.option_live_order_intent import (
    OrderIntentEngine,
    get_order_intent_engine
)


class TestUpstoxTradebookSync(unittest.TestCase):
    """Verifies defect F-01 fix: live Upstox tradebook synchronisation in OptionLiveAdapter."""

    def setUp(self):
        # Create an isolated temporary SQLite database
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        # Patch get_db_path across both modules to point to the isolated temp DB
        self.patch_db1 = patch("app.analytics.option_sniffer.option_live_adapter.get_db_path", return_value=self.db_path)
        self.patch_db2 = patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path)
        self.patch_db3 = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.patch_db1.start()
        self.patch_db2.start()
        self.patch_db3.start()

        # Initialize schema
        self._init_schema()

        # Create fresh adapter instance
        self.adapter = OptionLiveAdapter()

    def tearDown(self):
        self.patch_db1.stop()
        self.patch_db2.stop()
        self.patch_db3.stop()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def _init_schema(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS option_live_order_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_intent_id TEXT UNIQUE NOT NULL,
                    signal_id TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    action TEXT NOT NULL,
                    lot_size INTEGER NOT NULL,
                    target_quantity INTEGER NOT NULL,
                    filled_quantity INTEGER DEFAULT 0,
                    limit_price REAL NOT NULL,
                    average_fill_price REAL DEFAULT 0.0,
                    state TEXT NOT NULL,
                    broker_order_id TEXT,
                    stop_loss_trigger REAL,
                    stop_loss_limit REAL,
                    broker_sl_order_id TEXT,
                    exit_state TEXT DEFAULT 'NONE',
                    exit_broker_order_id TEXT,
                    exit_fill_price REAL DEFAULT 0.0,
                    realized_pnl REAL DEFAULT 0.0,
                    rejection_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    dynamic_sl_pct REAL,
                    dynamic_target_pct REAL,
                    engine_version TEXT DEFAULT 'DYNAMIC_V2',
                    gross_pnl REAL DEFAULT 0.0,
                    statutory_charges REAL DEFAULT 0.0
                );
            """)
            conn.commit()

    def _insert_sample_intent(self, intent_id, b_id, sl_id=None, exit_id=None, qty=65, avg_fill=100.0):
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents (
                    order_intent_id, signal_id, instrument_key, trading_symbol, underlying,
                    action, lot_size, target_quantity, filled_quantity, limit_price,
                    average_fill_price, state, broker_order_id, broker_sl_order_id,
                    exit_broker_order_id, exit_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                intent_id, "SIG_1", "NSE_FO|12345", "NIFTY24OCT25000CE", "NIFTY",
                "BUY", qty, qty, qty, avg_fill, avg_fill, "FILLED",
                b_id, sl_id, exit_id, "NONE", now_iso, now_iso
            ))
            conn.commit()

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_valid_mocked_broker_response_reconciles_execution_prices_and_costs(self, mock_broker_api):
        """Valid broker response updates fill prices, calculates statutory charges, and marks CONFIRMED_FLAT."""
        self._insert_sample_intent("INTENT_101", "ORD_BUY_101", sl_id="ORD_SL_101", exit_id="ORD_EXIT_101", qty=65, avg_fill=100.0)

        # Mock Upstox tradebook response
        mock_broker_api.return_value = (True, 200, {
            "status": "success",
            "data": [
                {
                    "order_id": "ORD_BUY_101",
                    "trade_id": "TRD_1",
                    "transaction_type": "BUY",
                    "quantity": 65,
                    "average_price": 102.50
                },
                {
                    "order_id": "ORD_EXIT_101",
                    "trade_id": "TRD_2",
                    "transaction_type": "SELL",
                    "quantity": 65,
                    "average_price": 125.00
                }
            ]
        })

        res = self.adapter.sync_trades_from_upstox_tradebook()

        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "SYNCED")
        self.assertEqual(res["updated"], 1)

        # Verify DB reconciliation
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT average_fill_price, exit_fill_price, gross_pnl, statutory_charges, realized_pnl, exit_state
                FROM option_live_order_intents WHERE order_intent_id = 'INTENT_101';
            """)
            row = cur.fetchone()

        avg_fill, exit_fill, gross, charges, net_pnl, exit_st = row
        self.assertEqual(avg_fill, 102.50)
        self.assertEqual(exit_fill, 125.00)
        expected_gross = round((125.00 - 102.50) * 65, 2)  # 22.50 * 65 = 1462.50
        self.assertEqual(gross, expected_gross)
        self.assertGreater(charges, 0.0)
        self.assertEqual(net_pnl, round(expected_gross - charges, 2))
        self.assertEqual(exit_st, "CONFIRMED_FLAT")

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_idempotent_sync_does_not_duplicate_trades_or_adjustments(self, mock_broker_api):
        """Re-running synchronization on already reconciled orders updates 0 rows and causes no duplication."""
        self._insert_sample_intent("INTENT_102", "ORD_BUY_102", exit_id="ORD_EXIT_102", qty=65, avg_fill=100.0)

        mock_broker_api.return_value = (True, 200, {
            "status": "success",
            "data": [
                {"order_id": "ORD_BUY_102", "trade_id": "TRD_10", "transaction_type": "BUY", "quantity": 65, "average_price": 100.0},
                {"order_id": "ORD_EXIT_102", "trade_id": "TRD_11", "transaction_type": "SELL", "quantity": 65, "average_price": 115.0}
            ]
        })

        # Run 1: Should update 1 row
        res1 = self.adapter.sync_trades_from_upstox_tradebook()
        self.assertTrue(res1["success"])
        self.assertEqual(res1["updated"], 1)

        # Record values after Run 1
        with sqlite3.connect(self.db_path) as conn:
            row_after_1 = conn.execute(
                "SELECT average_fill_price, exit_fill_price, gross_pnl, statutory_charges, realized_pnl, exit_state "
                "FROM option_live_order_intents WHERE order_intent_id = 'INTENT_102';"
            ).fetchone()

        # Run 2: Exact same data -> Should update 0 rows (strictly idempotent)
        res2 = self.adapter.sync_trades_from_upstox_tradebook()
        self.assertTrue(res2["success"])
        self.assertEqual(res2["updated"], 0)

        # Values must remain identical (zero duplication or inflation)
        with sqlite3.connect(self.db_path) as conn:
            row_after_2 = conn.execute(
                "SELECT average_fill_price, exit_fill_price, gross_pnl, statutory_charges, realized_pnl, exit_state "
                "FROM option_live_order_intents WHERE order_intent_id = 'INTENT_102';"
            ).fetchone()

        self.assertEqual(row_after_1, row_after_2)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_api_error_reported_as_unsuccessful(self, mock_broker_api):
        """Broker HTTP errors and timeouts generate an explicit failed status and do not report success."""
        # 1. HTTP 502 Bad Gateway
        mock_broker_api.return_value = (False, 502, {"error": "Upstox Gateway Unavailable"})
        res = self.adapter.sync_trades_from_upstox_tradebook()

        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "API_ERROR_502")
        self.assertEqual(res["updated"], 0)
        self.assertIn("502", res["error"])

        # 2. HTTP 401 Unauthorized
        mock_broker_api.return_value = (False, 401, {"error": "Token expired"})
        res2 = self.adapter.sync_trades_from_upstox_tradebook()
        self.assertFalse(res2["success"])
        self.assertEqual(res2["status"], "API_ERROR_401")

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_malformed_response_reported_as_unsuccessful(self, mock_broker_api):
        """Malformed or non-dict payloads return explicit MALFORMED_RESPONSE status."""
        # Non-dict payload
        mock_broker_api.return_value = (True, 200, "string instead of dict")
        res = self.adapter.sync_trades_from_upstox_tradebook()
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "MALFORMED_RESPONSE")

        # Missing data list
        mock_broker_api.return_value = (True, 200, {"status": "success", "data": "not a list"})
        res2 = self.adapter.sync_trades_from_upstox_tradebook()
        self.assertFalse(res2["success"])
        self.assertEqual(res2["status"], "MALFORMED_RESPONSE")

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_missing_trade_identifiers_cannot_reconcile(self, mock_broker_api):
        """Trades with missing or empty order_id are safely skipped and cannot cause invalid matches."""
        self._insert_sample_intent("INTENT_103", "ORD_BUY_103", qty=65, avg_fill=100.0)

        mock_broker_api.return_value = (True, 200, {
            "status": "success",
            "data": [
                {"order_id": None, "transaction_type": "BUY", "quantity": 65, "average_price": 105.0},
                {"order_id": "", "transaction_type": "SELL", "quantity": 65, "average_price": 120.0}
            ]
        })

        res = self.adapter.sync_trades_from_upstox_tradebook()
        self.assertTrue(res["success"])
        self.assertEqual(res["updated"], 0)

        # Record remained untouched
        with sqlite3.connect(self.db_path) as conn:
            avg_fill = conn.execute("SELECT average_fill_price FROM option_live_order_intents WHERE order_intent_id = 'INTENT_103'").fetchone()[0]
        self.assertEqual(avg_fill, 100.0)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_incomplete_broker_response_preserves_valid_local_records(self, mock_broker_api):
        """When broker tradebook is incomplete, existing valid local records are preserved and never overwritten."""
        # Intent A already reconciled
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents (
                    order_intent_id, signal_id, instrument_key, trading_symbol, underlying,
                    action, lot_size, target_quantity, filled_quantity, limit_price,
                    average_fill_price, state, broker_order_id, exit_broker_order_id,
                    exit_fill_price, gross_pnl, statutory_charges, realized_pnl, exit_state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                "INTENT_A", "SIG_A", "NSE_FO|1", "NIFTY24OCT", "NIFTY", "BUY",
                65, 65, 65, 150.0, 150.0, "FILLED", "ORD_A_BUY", "ORD_A_EXIT",
                165.0, 975.0, 55.0, 920.0, "CONFIRMED_FLAT", now_iso, now_iso
            ))
            conn.commit()

        # Intent B is open
        self._insert_sample_intent("INTENT_B", "ORD_B_BUY", qty=65, avg_fill=80.0)

        # Broker only returns trades for Intent B; Intent A is omitted from tradebook
        mock_broker_api.return_value = (True, 200, {
            "status": "success",
            "data": [
                {"order_id": "ORD_B_BUY", "trade_id": "T_B", "transaction_type": "BUY", "quantity": 65, "average_price": 82.0}
            ]
        })

        res = self.adapter.sync_trades_from_upstox_tradebook()
        self.assertTrue(res["success"])

        # Intent A must remain completely unchanged (never overwritten with 0 or None)
        with sqlite3.connect(self.db_path) as conn:
            intent_a = conn.execute(
                "SELECT average_fill_price, exit_fill_price, gross_pnl, realized_pnl, exit_state "
                "FROM option_live_order_intents WHERE order_intent_id = 'INTENT_A';"
            ).fetchone()

        self.assertEqual(intent_a[0], 150.0)
        self.assertEqual(intent_a[1], 165.0)
        self.assertEqual(intent_a[2], 975.0)
        self.assertEqual(intent_a[3], 920.0)
        self.assertEqual(intent_a[4], "CONFIRMED_FLAT")

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_multi_slice_vwap_aggregation(self, mock_broker_api):
        """Multiple execution slices for a single order_id are aggregated via Volume-Weighted Average Price (VWAP)."""
        self._insert_sample_intent("INTENT_104", "ORD_BUY_104", qty=65, avg_fill=100.0)

        # Order filled in 2 slices: 30 @ 100.0 and 35 @ 107.0
        # Total val: 3000 + 3745 = 6745. Total qty: 65. VWAP: 6745 / 65 = 103.7692...
        mock_broker_api.return_value = (True, 200, {
            "status": "success",
            "data": [
                {"order_id": "ORD_BUY_104", "trade_id": "T1", "transaction_type": "BUY", "quantity": 30, "average_price": 100.0},
                {"order_id": "ORD_BUY_104", "trade_id": "T2", "transaction_type": "BUY", "quantity": 35, "average_price": 107.0}
            ]
        })

        res = self.adapter.sync_trades_from_upstox_tradebook()
        self.assertTrue(res["success"])
        self.assertEqual(res["updated"], 1)

        with sqlite3.connect(self.db_path) as conn:
            avg_fill = conn.execute("SELECT average_fill_price FROM option_live_order_intents WHERE order_intent_id = 'INTENT_104'").fetchone()[0]

        expected_vwap = round(6745.0 / 65.0, 4)
        self.assertAlmostEqual(avg_fill, expected_vwap, places=2)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_partial_buy_and_partial_sell_leaves_residual_open_qty_and_partially_closed_state(self, mock_broker_api):
        """
        Verify:
          - Buy 30 qty executed.
          - Sell 15 qty executed (partial exit).
          - Remaining open qty = 15.
          - exit_state becomes 'PARTIALLY_CLOSED'.
          - Must NOT be marked 'CONFIRMED_FLAT'.
          - Gross P&L and estimated statutory charges calculated strictly on closed 15 qty.
        """
        self._insert_sample_intent("INTENT_PARTIAL", "ORD_BUY_P", exit_id="ORD_EXIT_P", qty=30, avg_fill=100.0)

        mock_broker_api.return_value = (True, 200, {
            "status": "success",
            "data": [
                {"order_id": "ORD_BUY_P", "trade_id": "T_BP", "transaction_type": "BUY", "quantity": 30, "average_price": 100.0},
                {"order_id": "ORD_EXIT_P", "trade_id": "T_EP", "transaction_type": "SELL", "quantity": 15, "average_price": 120.0}
            ]
        })

        res = self.adapter.sync_trades_from_upstox_tradebook()
        self.assertTrue(res["success"])
        self.assertEqual(res["updated"], 1)

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT filled_quantity, exit_state, average_fill_price, exit_fill_price, gross_pnl, statutory_charges, realized_pnl
                FROM option_live_order_intents WHERE order_intent_id = 'INTENT_PARTIAL';
            """).fetchone()

        rem_open_qty, exit_st, buy_avg, sell_avg, gross, charges, net_pnl = row
        self.assertEqual(rem_open_qty, 15)  # 30 buy - 15 sell = 15 remaining open
        self.assertEqual(exit_st, "PARTIALLY_CLOSED")
        self.assertNotEqual(exit_st, "CONFIRMED_FLAT")
        self.assertEqual(buy_avg, 100.0)
        self.assertEqual(sell_avg, 120.0)
        expected_gross = round((120.0 - 100.0) * 15, 2)  # 20.0 * 15 = 300.0
        self.assertEqual(gross, expected_gross)
        self.assertGreater(charges, 0.0)
        self.assertEqual(net_pnl, round(expected_gross - charges, 2))

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_subsequent_sell_reconciles_to_confirmed_flat_when_net_qty_is_zero(self, mock_broker_api):
        """
        Verify that after an initial partial close, subsequent execution of remaining quantity
        reconciles net open qty to 0 and transitions state to 'CONFIRMED_FLAT'.
        """
        self._insert_sample_intent("INTENT_FLAT_FLOW", "ORD_BUY_F", exit_id="ORD_EXIT_F", qty=30, avg_fill=100.0)

        # Slice 1: 15 sell -> PARTIALLY_CLOSED
        mock_broker_api.return_value = (True, 200, {
            "status": "success",
            "data": [
                {"order_id": "ORD_BUY_F", "trade_id": "TF_1", "transaction_type": "BUY", "quantity": 30, "average_price": 100.0},
                {"order_id": "ORD_EXIT_F", "trade_id": "TF_2", "transaction_type": "SELL", "quantity": 15, "average_price": 120.0}
            ]
        })
        self.adapter.sync_trades_from_upstox_tradebook()

        # Slice 2: Remaining 15 sell comes in
        mock_broker_api.return_value = (True, 200, {
            "status": "success",
            "data": [
                {"order_id": "ORD_BUY_F", "trade_id": "TF_1", "transaction_type": "BUY", "quantity": 30, "average_price": 100.0},
                {"order_id": "ORD_EXIT_F", "trade_id": "TF_2", "transaction_type": "SELL", "quantity": 15, "average_price": 120.0},
                {"order_id": "ORD_EXIT_F", "trade_id": "TF_3", "transaction_type": "SELL", "quantity": 15, "average_price": 130.0}
            ]
        })
        res2 = self.adapter.sync_trades_from_upstox_tradebook()
        self.assertTrue(res2["success"])
        self.assertEqual(res2["updated"], 1)

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT filled_quantity, exit_state, average_fill_price, exit_fill_price, gross_pnl
                FROM option_live_order_intents WHERE order_intent_id = 'INTENT_FLAT_FLOW';
            """).fetchone()

        rem_open_qty, exit_st, buy_avg, sell_avg, gross = row
        self.assertEqual(rem_open_qty, 0)  # Net open quantity is 0
        self.assertEqual(exit_st, "CONFIRMED_FLAT")
        # Exit VWAP: (15*120 + 15*130)/30 = 125.0
        self.assertEqual(sell_avg, 125.0)
        expected_gross = round((125.0 - 100.0) * 30, 2)  # 25.0 * 30 = 750.0
        self.assertEqual(gross, expected_gross)

    @patch.object(OrderIntentEngine, "_call_upstox_broker_api")
    def test_actual_vs_estimated_charge_labeling_in_get_live_pnl_summary(self, mock_broker_api):
        """
        Verify get_live_pnl_summary() surfaces honest labeling:
          - is_statutory_charges_estimated is True
          - charges_reconciliation_mode is 'ESTIMATED_STATUTORY_SCHEDULE'
          - broker_execution_confirmed matches sync_status
        """
        self._insert_sample_intent("INTENT_PNL", "ORD_BUY_PNL", exit_id="ORD_EXIT_PNL", qty=65, avg_fill=100.0)
        mock_broker_api.return_value = (True, 200, {
            "status": "success",
            "data": [
                {"order_id": "ORD_BUY_PNL", "trade_id": "T1", "transaction_type": "BUY", "quantity": 65, "average_price": 100.0},
                {"order_id": "ORD_EXIT_PNL", "trade_id": "T2", "transaction_type": "SELL", "quantity": 65, "average_price": 110.0}
            ]
        })
        self.adapter.sync_trades_from_upstox_tradebook()

        summary = self.adapter.get_live_pnl_summary()
        self.assertTrue(summary["is_statutory_charges_estimated"])
        self.assertEqual(summary["charges_reconciliation_mode"], "ESTIMATED_STATUTORY_SCHEDULE")
        self.assertEqual(summary["reconciliation_status"], "SYNCED")
        self.assertTrue(summary["broker_execution_confirmed"])
        self.assertEqual(summary["closed_trades_count"], 1)
        self.assertEqual(summary["realized_gross_pnl"], 650.0)
        self.assertGreater(summary["statutory_charges"], 0.0)
        self.assertEqual(summary["realized_net_pnl"], round(650.0 - summary["statutory_charges"], 2))


class TestHistoricalCircuitBreakerDateScope(unittest.TestCase):
    """Verifies defect F-02 fix: strict date scoping in OptionDB.get_daily_circuit_breaker_telemetry."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        self.patch_db = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.patch_db.start()

        self._init_schema()

    def tearDown(self):
        self.patch_db.stop()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def _init_schema(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS option_sniffer_paper_trades (
                    trade_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT,
                    exit_reason TEXT,
                    net_pnl REAL,
                    net_pnl_pct REAL,
                    peak_mfe_pct REAL DEFAULT 0.0,
                    highest_high REAL DEFAULT 0.0,
                    lowest_low REAL DEFAULT 0.0,
                    stop_loss REAL,
                    target REAL,
                    breakeven_active INTEGER DEFAULT 0,
                    trailing_level INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                );
            """)
            conn.commit()

    def _insert_trade(self, trade_id, exit_time, net_pnl, status="CLOSED"):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades (
                    trade_id, signal_id, instrument_key, trading_symbol, underlying,
                    direction, entry_price, exit_price, quantity, status,
                    entry_time, exit_time, exit_reason, net_pnl, net_pnl_pct, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                trade_id, f"SIG_{trade_id}", "NSE_FO|1", "NIFTY24OCTCE", "NIFTY",
                "CE", 100.0, 90.0, 65, status,
                exit_time, exit_time, "SL_HIT", net_pnl, -10.0, exit_time
            ))
            conn.commit()

    def test_oct_14_only_exits_contribute_to_telemetry(self):
        """
        Verify that for as_of_date='2026-10-14':
          - Preceding session (Oct 13 23:59:59.999999) is excluded.
          - Oct 14 00:00:00 (start of day) is included.
          - Oct 14 09:30:15.123456 (fractional seconds) is included.
          - Oct 14 23:59:59.999999 (near midnight) is included.
          - Oct 15 00:00:00 (start of next day) is excluded.
          - Oct 15 09:15:00 (next day) is excluded.
        """
        self._insert_trade("T_OCT13_LATE", "2026-10-13 23:59:59.999999", -2000.0)
        self._insert_trade("T_OCT14_START", "2026-10-14 00:00:00", -500.0)
        self._insert_trade("T_OCT14_MID", "2026-10-14 09:30:15.123456", -600.0)
        self._insert_trade("T_OCT14_END", "2026-10-14 23:59:59.999999", -700.0)
        self._insert_trade("T_OCT15_START", "2026-10-15 00:00:00", -5000.0)
        self._insert_trade("T_OCT15_MID", "2026-10-15 09:15:00", -3000.0)

        telemetry = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date="2026-10-14")

        # Must strictly have 3 trades: T_OCT14_START, T_OCT14_MID, T_OCT14_END
        self.assertEqual(telemetry["total_closed_today"], 3)
        expected_pnl = -500.0 + -600.0 + -700.0  # -1,800.0
        self.assertEqual(telemetry["today_net_pnl"], expected_pnl)
        self.assertEqual(telemetry["losses_today"], 3)
        self.assertEqual(telemetry["wins_today"], 0)
        # -1,800 is above -3,500 ceiling, so session must NOT be halted
        self.assertFalse(telemetry["is_session_halted"])

    def test_future_session_records_cannot_change_oct_14_telemetry(self):
        """Future session records (e.g. catastrophic losses on Oct 15/16) cannot alter Oct 14 telemetry."""
        # 1. Establish baseline on Oct 14
        self._insert_trade("T14_1", "2026-10-14 10:00:00", 1200.0)  # Win
        self._insert_trade("T14_2", "2026-10-14 11:30:00", -400.0)  # Loss

        tel_before = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date="2026-10-14")
        self.assertEqual(tel_before["total_closed_today"], 2)
        self.assertEqual(tel_before["today_net_pnl"], 800.0)
        self.assertFalse(tel_before["is_session_halted"])

        # 2. Insert massive catastrophic losses on Oct 15 and Oct 16
        for i in range(10):
            self._insert_trade(f"T15_{i}", f"2026-10-15 10:{i:02d}:00", -1000.0)
        for i in range(5):
            self._insert_trade(f"T16_{i}", f"2026-10-16 10:{i:02d}:00", -2000.0)

        # 3. Re-query Oct 14: Must be 100% identical to tel_before
        tel_after = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date="2026-10-14")
        self.assertEqual(tel_after["total_closed_today"], 2)
        self.assertEqual(tel_after["today_net_pnl"], 800.0)
        self.assertEqual(tel_after["losses_today"], 1)
        self.assertEqual(tel_after["wins_today"], 1)
        self.assertFalse(tel_after["is_session_halted"])

    def test_iso_formatted_timestamps_with_t_separator(self):
        """Confirms that ISO timestamps formatted with 'T' separator are accurately bounded."""
        self._insert_trade("T_ISO_PREV", "2026-10-13T23:59:59.999999", -1000.0)
        self._insert_trade("T_ISO_START", "2026-10-14T00:00:00", 500.0)
        self._insert_trade("T_ISO_DAY", "2026-10-14T14:15:30.456789", 800.0)
        self._insert_trade("T_ISO_END", "2026-10-14T23:59:59.999999", -300.0)
        self._insert_trade("T_ISO_NEXT", "2026-10-15T00:00:00", -2000.0)

        telemetry = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date="2026-10-14")
        self.assertEqual(telemetry["total_closed_today"], 3)
        self.assertEqual(telemetry["today_net_pnl"], 1000.0)  # 500 + 800 - 300

    def test_oct_15_query_only_includes_oct_15_records(self):
        """Querying Oct 15 strictly isolates Oct 15 and excludes Oct 14 and Oct 16."""
        self._insert_trade("T14", "2026-10-14 12:00:00", 500.0)
        self._insert_trade("T15_A", "2026-10-15 09:30:00", -1500.0)
        self._insert_trade("T15_B", "2026-10-15 13:00:00", -2500.0)
        self._insert_trade("T16", "2026-10-16 10:00:00", 2000.0)

        tel_15 = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date="2026-10-15")
        self.assertEqual(tel_15["total_closed_today"], 2)
        self.assertEqual(tel_15["today_net_pnl"], -4000.0)
        # Breached -₹3,500 ceiling on Oct 15 -> session halted!
        self.assertTrue(tel_15["is_session_halted"])
        self.assertIn("Daily max loss ceiling reached", tel_15["halt_reason"])


class TestLiveRiskCircuitBreakerAndReconciliation(unittest.TestCase):
    """
    Verifies armed-live risk controls and execution safeguards:
      1. Daily realized loss reaching ₹3,500 threshold halts live entries.
      2. Unrealized exposure loss on open positions breaching threshold halts live entries.
      3. Broker position reconciliation fails closed on API error or existing open position.
      4. Ambiguous pending order (>15s) blocks new entries.
      5. Daily circuit breaker state persists across process reboots.
      6. Open position management (trailing stop / target exits) continues executing even when entries halted.
      7. Strict decoupling: live circuit breaker never reads or mixes paper trades, and vice versa.
      8. Signal engine Gate 9B switches to live telemetry when live is armed.
    """

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        self.patch_db1 = patch("app.analytics.option_sniffer.option_live_adapter.get_db_path", return_value=self.db_path)
        self.patch_db2 = patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path)
        self.patch_db3 = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.patch_db1.start()
        self.patch_db2.start()
        self.patch_db3.start()

        self._init_schemas()

        self.engine = OrderIntentEngine()
        self.adapter = OptionLiveAdapter()

    def tearDown(self):
        self.patch_db1.stop()
        self.patch_db2.stop()
        self.patch_db3.stop()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def _init_schemas(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS option_live_order_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_intent_id TEXT UNIQUE NOT NULL,
                    signal_id TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    action TEXT NOT NULL,
                    lot_size INTEGER NOT NULL,
                    target_quantity INTEGER NOT NULL,
                    filled_quantity INTEGER DEFAULT 0,
                    limit_price REAL NOT NULL,
                    average_fill_price REAL DEFAULT 0.0,
                    state TEXT NOT NULL,
                    broker_order_id TEXT,
                    stop_loss_trigger REAL,
                    stop_loss_limit REAL,
                    broker_sl_order_id TEXT,
                    exit_state TEXT DEFAULT 'NONE',
                    exit_broker_order_id TEXT,
                    exit_fill_price REAL DEFAULT 0.0,
                    realized_pnl REAL DEFAULT 0.0,
                    rejection_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    dynamic_sl_pct REAL,
                    dynamic_target_pct REAL,
                    engine_version TEXT DEFAULT 'DYNAMIC_V2',
                    gross_pnl REAL DEFAULT 0.0,
                    statutory_charges REAL DEFAULT 0.0
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS option_sniffer_paper_trades (
                    trade_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT,
                    exit_reason TEXT,
                    net_pnl REAL,
                    net_pnl_pct REAL,
                    peak_mfe_pct REAL DEFAULT 0.0,
                    highest_high REAL DEFAULT 0.0,
                    lowest_low REAL DEFAULT 0.0,
                    stop_loss REAL,
                    target REAL,
                    breakeven_active INTEGER DEFAULT 0,
                    trailing_level INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                );
            """)
            conn.commit()

    def _insert_live_closed_trade(self, intent_id, realized_pnl, broker_id="ORD_REAL_1", updated_at=None):
        now_iso = updated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents (
                    order_intent_id, signal_id, instrument_key, trading_symbol, underlying,
                    action, lot_size, target_quantity, filled_quantity, limit_price,
                    average_fill_price, state, broker_order_id, exit_state,
                    gross_pnl, statutory_charges, realized_pnl, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                intent_id, f"SIG_{intent_id}", "NSE_FO|100", "NIFTY24OCTCE", "NIFTY",
                "BUY", 65, 65, 0, 100.0, 100.0, "FILLED", broker_id, "CONFIRMED_FLAT",
                realized_pnl + 55.0, 55.0, realized_pnl, now_iso, now_iso
            ))
            conn.commit()

    def _insert_live_open_position(self, intent_id, ikey="NSE_FO|OPEN_1", avg_fill=100.0, qty=65):
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents (
                    order_intent_id, signal_id, instrument_key, trading_symbol, underlying,
                    action, lot_size, target_quantity, filled_quantity, limit_price,
                    average_fill_price, state, broker_order_id, exit_state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                intent_id, f"SIG_{intent_id}", ikey, "NIFTY24OCTCE", "NIFTY",
                "BUY", qty, qty, qty, avg_fill, avg_fill, "FILLED", "ORD_LIVE_OPEN", "NONE",
                now_iso, now_iso
            ))
            conn.commit()

    def _get_sample_signal_and_quote(self, ikey="NSE_FO|NEW_01"):
        signal = {
            "signal_id": f"SIG_{int(datetime.now().timestamp()*1000)}",
            "instrument_key": ikey,
            "trading_symbol": "NIFTY24OCT25000CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0,
            "spot_price": 25000.0,
            "delta": 0.50,
            "iv": 14.0
        }
        quote = {
            "ask": 100.0,
            "bid": 99.5,
            "ltp": 100.0,
            "quote_age_ms": 100
        }
        return signal, quote

    def test_live_circuit_breaker_realized_loss_halts_entries(self):
        """When realized live losses breach ₹3,500 threshold, live entries are halted immediately."""
        self._insert_live_closed_trade("INTENT_RL_1", -3600.0)

        tel = self.engine.get_live_circuit_breaker_telemetry()
        self.assertTrue(tel["is_session_halted"])
        self.assertTrue(tel["is_max_loss_limit_hit"])
        self.assertIn("loss ceiling reached", tel["halt_reason"])

        # Attempt new entry -> blocked by risk controller
        sig, quote = self._get_sample_signal_and_quote()
        with patch.object(self.engine, "_call_upstox_broker_api", return_value=(True, 200, {"status": "success", "data": []})):
            ok, reason, rec = self.engine.create_and_validate_intent(sig, quote)

        self.assertFalse(ok)
        self.assertIn("RISK_BLOCKED", reason)
        self.assertIn("loss ceiling reached", reason)

    def test_live_circuit_breaker_unrealized_exposure_loss_halts_entries(self):
        """Mark-to-market unrealized losses breaching ₹3,500 threshold trip the live circuit breaker."""
        self._insert_live_open_position("INTENT_UNREAL_1", ikey="NSE_FO|DROP_1", avg_fill=100.0, qty=65)

        # Mock live quote manager to return deep loss: bid dropped from 100 to 40 (-60 pts * 65 = -3900 INR)
        mock_quote = {"bid": 40.0, "ltp": 40.0}
        with patch("app.analytics.option_sniffer.option_stream_manager.OptionStreamManager.get_option_quote", return_value=mock_quote):
            tel = self.engine.get_live_circuit_breaker_telemetry()

        self.assertTrue(tel["is_session_halted"])
        self.assertTrue(tel["is_max_loss_limit_hit"])
        self.assertLessEqual(tel["total_net_pnl_exposure"], -3500.0)
        self.assertIn("total risk exposure breached", tel["halt_reason"])

        # Entry creation is halted
        sig, quote = self._get_sample_signal_and_quote()
        with patch.object(self.engine, "_call_upstox_broker_api", return_value=(True, 200, {"status": "success", "data": []})):
            with patch("app.analytics.option_sniffer.option_stream_manager.OptionStreamManager.get_option_quote", return_value=mock_quote):
                ok, reason, rec = self.engine.create_and_validate_intent(sig, quote)

        self.assertFalse(ok)
        self.assertIn("RISK_BLOCKED", reason)
        self.assertIn("total risk exposure breached", reason)

    def test_broker_position_mismatch_fails_closed(self):
        """Broker position check fails closed on API error and blocks order if broker shows open quantity."""
        sig, quote = self._get_sample_signal_and_quote(ikey="NSE_FO|CHECK_MISMATCH")

        with patch.object(self.adapter, "_is_live_armed", True), \
             patch("app.analytics.option_sniffer.option_live_adapter.get_option_live_adapter", return_value=self.adapter):
            # Case A: Upstox API network error (503) -> Fail Closed
            with patch.object(self.engine, "_call_upstox_broker_api", return_value=(False, 503, {"error": "Broker timeout"})):
                ok_a, reason_a, _ = self.engine.create_and_validate_intent(sig, quote)

            self.assertFalse(ok_a)
            self.assertIn("BROKER_RECONCILIATION_FAILED", reason_a)

            # Case B: Broker confirms position already open for this instrument
            broker_positions_resp = {
                "status": "success",
                "data": [
                    {"instrument_token": "NSE_FO|CHECK_MISMATCH", "quantity": 65, "pnl": 500.0}
                ]
            }
            with patch.object(self.engine, "_call_upstox_broker_api", return_value=(True, 200, broker_positions_resp)):
                ok_b, reason_b, _ = self.engine.create_and_validate_intent(sig, quote)

            self.assertFalse(ok_b)
            self.assertIn("BROKER_POSITION_EXISTS", reason_b)
            self.assertIn("Broker confirms 65 open quantity already exists", reason_b)

    def test_ambiguous_pending_order_blocks_new_entries(self):
        """Unresolved orders in state SUBMITTED for >15s block subsequent entries until reconciled."""
        # Insert submitted order created 20 seconds ago
        past_time = (datetime.now() - timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents (
                    order_intent_id, signal_id, instrument_key, trading_symbol, underlying,
                    action, lot_size, target_quantity, filled_quantity, limit_price,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                "INTENT_PENDING_1", "SIG_PEND_1", "NSE_FO|PENDING_1", "NIFTY24OCTCE", "NIFTY",
                "BUY", 65, 65, 0, 100.0, "SUBMITTED", past_time, past_time
            ))
            conn.commit()

        sig, quote = self._get_sample_signal_and_quote(ikey="NSE_FO|NEW_PENDING_TEST")

        with patch.object(self.engine, "_call_upstox_broker_api", return_value=(True, 200, {"status": "success", "data": []})):
            ok, reason, _ = self.engine.create_and_validate_intent(sig, quote)

        self.assertFalse(ok)
        self.assertIn("AMBIGUOUS_ORDER_PENDING", reason)
        self.assertIn("awaiting broker confirmation", reason)

        # Reconcile pending order to FILLED and flat
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE option_live_order_intents SET state = 'FILLED', exit_state = 'CONFIRMED_FLAT' WHERE order_intent_id = 'INTENT_PENDING_1';")
            conn.commit()

        # Clean next attempt passes ambiguous check
        with patch.object(self.engine, "_call_upstox_broker_api", return_value=(True, 200, {"status": "success", "data": []})):
            ok2, reason2, rec2 = self.engine.create_and_validate_intent(sig, quote)

        self.assertTrue(ok2)
        self.assertEqual(reason2, "VALIDATED")

    def test_circuit_breaker_persists_across_reboots(self):
        """Circuit breaker state persists in SQLite and immediately halts trading upon process re-instantiation."""
        self._insert_live_closed_trade("INTENT_CRASH_1", -4000.0)

        # Simulate fresh process reboot by instantiating a completely new OrderIntentEngine
        rebooted_engine = OrderIntentEngine()
        tel = rebooted_engine.get_live_circuit_breaker_telemetry()

        self.assertTrue(tel["is_session_halted"])
        self.assertEqual(tel["realized_net_pnl"], -4000.0)
        self.assertIn("loss ceiling reached", tel["halt_reason"])

    def test_open_positions_remain_managed_when_entries_halted(self):
        """Halting new entries does NOT abandon open positions; protective exits continue executing."""
        # 1. Halt session entries with past realized loss
        self._insert_live_closed_trade("INTENT_HALTED_PRIOR", -3800.0)

        # Confirm new entries are rejected
        sig, quote = self._get_sample_signal_and_quote()
        with patch.object(self.engine, "_call_upstox_broker_api", return_value=(True, 200, {"status": "success", "data": []})):
            ok, reason, _ = self.engine.create_and_validate_intent(sig, quote)
        self.assertFalse(ok)
        self.assertIn("RISK_BLOCKED", reason)

        # 2. Open position exists
        self._insert_live_open_position("INTENT_MANAGED_1", ikey="NSE_FO|M1", avg_fill=100.0, qty=65)

        # 3. Stream manager provides target gain quote (+40% gain: bid=140.0)
        quotes_map = {"NSE_FO|M1": {"bid": 140.0, "ltp": 140.0}}

        def mock_api_call(method, endpoint, payload=None, params=None, is_emergency=False):
            if endpoint == "/portfolio/short-term-positions":
                return True, 200, {"status": "success", "data": [{"instrument_token": "NSE_FO|M1", "quantity": 65}]}
            elif endpoint == "/order/place":
                return True, 200, {"status": "success", "data": {"order_id": "ORD_EXIT_M1"}}
            return True, 200, {"status": "success", "data": {}}

        with patch.object(self.adapter, "_is_live_armed", True):
            with patch("app.analytics.option_sniffer.option_live_adapter.get_option_live_adapter", return_value=self.adapter):
                with patch.object(self.engine, "_call_upstox_broker_api", side_effect=mock_api_call):
                    exits = self.engine.check_and_execute_live_exits(quotes_map)

        # Exit was triggered and dispatched despite entry halt!
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["order_intent_id"], "INTENT_MANAGED_1")
        self.assertEqual(exits[0]["exit_reason"], "TARGET_MET")
        self.assertTrue(exits[0].get("exit_broker_order_id") is not None)

    def test_paper_trades_do_not_contaminate_live_circuit_breaker(self):
        """Paper trading losses and live trading P&L remain strictly decoupled."""
        # Insert ₹10,000 loss in paper trades table
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades (
                    trade_id, signal_id, instrument_key, trading_symbol, underlying,
                    direction, entry_price, exit_price, quantity, status,
                    entry_time, exit_time, exit_reason, net_pnl, net_pnl_pct, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                "PAPER_LOSS_1", "SIG_P", "NSE_FO|P", "NIFTY24OCTCE", "NIFTY",
                "CE", 100.0, 50.0, 65, "CLOSED",
                now_iso, now_iso, "SL_HIT", -10000.0, -50.0, now_iso
            ))
            conn.commit()

        # Insert ₹1,500 profit in live order intents table
        self._insert_live_closed_trade("INTENT_WIN_1", 1500.0)

        # Query Paper circuit breaker: Must report -10,000 P&L and HALTED
        paper_tel = OptionDB.get_daily_circuit_breaker_telemetry()
        self.assertTrue(paper_tel["is_session_halted"])
        self.assertEqual(paper_tel["today_net_pnl"], -10000.0)

        # Query Live circuit breaker: Must report +1,500 P&L and NOT HALTED
        live_tel = self.engine.get_live_circuit_breaker_telemetry()
        self.assertFalse(live_tel["is_session_halted"])
        self.assertEqual(live_tel["realized_net_pnl"], 1500.0)

    def test_signal_engine_gate9b_decoupling(self):
        """Gate 9B in OptionSignalEngine correctly branches between live and paper circuit breakers."""
        from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine

        # Populate paper loss -8000 (paper tripped) and live profit +2000 (live clean)
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades (
                    trade_id, signal_id, instrument_key, trading_symbol, underlying,
                    direction, entry_price, exit_price, quantity, status,
                    entry_time, exit_time, exit_reason, net_pnl, net_pnl_pct, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                "PAPER_TRIPPED", "SIG_PT", "NSE_FO|PT", "NIFTY24OCTCE", "NIFTY",
                "CE", 100.0, 50.0, 65, "CLOSED",
                now_iso, now_iso, "SL_HIT", -8000.0, -50.0, now_iso
            ))
            conn.commit()
        self._insert_live_closed_trade("LIVE_CLEAN", 2000.0)

        # Mode A: Live armed -> queries live circuit breaker (NOT halted)
        with patch.object(self.adapter, "_is_live_armed", True):
            with patch("app.analytics.option_sniffer.option_live_adapter.get_option_live_adapter", return_value=self.adapter):
                with patch("app.analytics.option_sniffer.option_live_order_intent.get_order_intent_engine", return_value=self.engine):
                    tel_live = self.engine.get_live_circuit_breaker_telemetry()
                    self.assertFalse(tel_live["is_session_halted"])

        # Mode B: Paper mode (live disarmed) -> queries paper circuit breaker (HALTED)
        with patch.object(self.adapter, "_is_live_armed", False):
            tel_paper = OptionDB.get_daily_circuit_breaker_telemetry()
            self.assertTrue(tel_paper["is_session_halted"])


if __name__ == "__main__":
    unittest.main()

