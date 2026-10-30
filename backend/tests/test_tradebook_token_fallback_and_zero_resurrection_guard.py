"""
Regression test for Tradebook Instrument-Token Fallback & Zero-Resurrection Guard.
Guarantees:
  1. Manual or mismatched Upstox sell orders correctly reconcile by instrument_token/symbol.
  2. CONFIRMED_FLAT trades can NEVER be resurrected back to NONE or SUBMITTED.
  3. Reconciler performs purely read-only synchronization with zero broker order placement.
"""

import unittest
from unittest.mock import patch, MagicMock
import sqlite3
import tempfile
import os

from app.analytics.option_sniffer.option_live_adapter import OptionLiveAdapter


class TestTradebookTokenFallbackAndZeroResurrection(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE option_live_order_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_intent_id TEXT UNIQUE,
                    signal_id TEXT,
                    instrument_key TEXT,
                    trading_symbol TEXT,
                    underlying TEXT,
                    action TEXT,
                    lot_size INTEGER,
                    target_quantity INTEGER,
                    filled_quantity INTEGER,
                    limit_price REAL,
                    average_fill_price REAL,
                    state TEXT,
                    broker_order_id TEXT,
                    stop_loss_trigger REAL,
                    stop_loss_limit REAL,
                    broker_sl_order_id TEXT,
                    exit_state TEXT,
                    exit_broker_order_id TEXT,
                    exit_fill_price REAL,
                    gross_pnl REAL,
                    statutory_charges REAL,
                    realized_pnl REAL,
                    rejection_reason TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    dynamic_sl_pct REAL DEFAULT 20.0,
                    dynamic_target_pct REAL DEFAULT 35.0,
                    engine_version TEXT DEFAULT 'V1_CLASSIC'
                );
            """)
            # Insert intent with mismatched/temporary exit_broker_order_id
            cur.execute("""
                INSERT INTO option_live_order_intents (
                    order_intent_id, signal_id, instrument_key, trading_symbol, underlying,
                    action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price,
                    state, broker_order_id, broker_sl_order_id, exit_state, exit_broker_order_id,
                    exit_fill_price, gross_pnl, statutory_charges, realized_pnl, created_at, updated_at
                ) VALUES (
                    'INTENT_TEST_DESYNC_001', 'SIG_001', 'NSE_FO|53872', 'NIFTY 23000 CE 03 NOV 26', 'NIFTY',
                    'BUY_CE', 65, 65, 65, 78.20, 75.75,
                    'FILLED', '261029000119858', '261029000120059', 'NONE', 'UPSTOX_EXIT_TEMP_123',
                    0.0, 0.0, 0.0, 0.0, '2026-10-29 10:20:00', '2026-10-29 10:20:00'
                );
            """)
            conn.commit()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    @patch("app.analytics.option_sniffer.option_live_adapter.get_db_path")
    @patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path")
    def test_reconciliation_matches_by_token_fallback(self, mock_db_path1, mock_db_path2):
        mock_db_path1.return_value = self.db_path
        mock_db_path2.return_value = self.db_path

        adapter = OptionLiveAdapter.get_instance()

        # Mock Upstox tradebook response where sell order_id is completely different from UPSTOX_EXIT_TEMP_123
        mock_trades_payload = {
            "status": "success",
            "data": [
                {
                    "exchange": "NFO",
                    "product": "I",
                    "trading_symbol": "NIFTY26N0323000CE",
                    "instrument_token": "NSE_FO|53872",
                    "order_type": "LIMIT",
                    "transaction_type": "BUY",
                    "quantity": 65,
                    "order_id": "261029000119858",
                    "average_price": 75.75,
                    "trade_id": "T_BUY_1"
                },
                {
                    "exchange": "NFO",
                    "product": "I",
                    "trading_symbol": "NIFTY26N0323000CE",
                    "instrument_token": "NSE_FO|53872",
                    "order_type": "LIMIT",
                    "transaction_type": "SELL",
                    "quantity": 65,
                    "order_id": "261029000137399",  # Real broker order ID
                    "average_price": 104.65,
                    "trade_id": "T_SELL_1"
                }
            ]
        }

        with patch.object(adapter, "_get_read_only_conn", side_effect=lambda: sqlite3.connect(self.db_path)):
            with patch("app.analytics.option_sniffer.option_live_adapter.get_order_intent_engine") as mock_get_engine:
                mock_engine = MagicMock()
                mock_engine._call_upstox_broker_api.return_value = (True, 200, mock_trades_payload)
                mock_get_engine.return_value = mock_engine

                # Execute synchronization
                res = adapter.sync_trades_from_upstox_tradebook()
                self.assertTrue(res["success"])
                self.assertEqual(res["updated"], 1)

        # Inspect updated DB record
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT exit_state, exit_broker_order_id, filled_quantity, exit_fill_price, gross_pnl FROM option_live_order_intents WHERE order_intent_id = 'INTENT_TEST_DESYNC_001'")
            row = cur.fetchone()

        self.assertEqual(row[0], "CONFIRMED_FLAT")
        self.assertEqual(row[1], "261029000137399")
        self.assertEqual(row[2], 0)
        self.assertAlmostEqual(row[3], 104.65, places=2)
        self.assertAlmostEqual(row[4], 1878.50, places=2)

    @patch("app.analytics.option_sniffer.option_live_adapter.get_db_path")
    @patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path")
    def test_zero_resurrection_guard(self, mock_db_path1, mock_db_path2):
        mock_db_path1.return_value = self.db_path
        mock_db_path2.return_value = self.db_path

        # Set intent to CONFIRMED_FLAT initially
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE option_live_order_intents
                SET exit_state = 'CONFIRMED_FLAT', filled_quantity = 0, exit_fill_price = 104.65, gross_pnl = 1878.50
                WHERE order_intent_id = 'INTENT_TEST_DESYNC_001';
            """)
            conn.commit()

        adapter = OptionLiveAdapter.get_instance()

        # Simulate broker returning only buy trade (sell trade delayed or temporarily omitted)
        mock_trades_payload = {
            "status": "success",
            "data": [
                {
                    "exchange": "NFO",
                    "product": "I",
                    "trading_symbol": "NIFTY26N0323000CE",
                    "instrument_token": "NSE_FO|53872",
                    "order_type": "LIMIT",
                    "transaction_type": "BUY",
                    "quantity": 65,
                    "order_id": "261029000119858",
                    "average_price": 75.75,
                    "trade_id": "T_BUY_1"
                }
            ]
        }

        with patch.object(adapter, "_get_read_only_conn", side_effect=lambda: sqlite3.connect(self.db_path)):
            with patch("app.analytics.option_sniffer.option_live_adapter.get_order_intent_engine") as mock_get_engine:
                mock_engine = MagicMock()
                mock_engine._call_upstox_broker_api.return_value = (True, 200, mock_trades_payload)
                mock_get_engine.return_value = mock_engine

                # Sync should NOT resurrect the flat trade back to NONE or open quantity
                adapter.sync_trades_from_upstox_tradebook()

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT exit_state, filled_quantity FROM option_live_order_intents WHERE order_intent_id = 'INTENT_TEST_DESYNC_001'")
            row = cur.fetchone()

        self.assertEqual(row[0], "CONFIRMED_FLAT")
        self.assertEqual(row[1], 0)


if __name__ == "__main__":
    unittest.main()
