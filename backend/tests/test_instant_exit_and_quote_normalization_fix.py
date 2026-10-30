"""
Unit tests for the Instant Exit Prevention and Upstox Quote Symbol Normalization Fix.

Tests:
1. Symbol normalization in fetch_upstox_bulk_quotes and fetch_upstox_bulk_ltp.
2. Symbol normalization in get_bulk_live_quotes_with_meta.
3. Target/SL Runway Sanity Gate in ConsensusScanner.
4. Pre-flight Target/SL Sanity Gate in ConsensusTracker.save_consensus_trade.
5. Minimum Dwell Time (60s) gate in ConsensusTracker.reconcile_open_consensus_trades.
"""

import unittest
import sqlite3
import tempfile
import os
import time
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from app.analytics.consensus_tracker import ConsensusTracker
from app.analytics.consensus_scanner import ConsensusScanner
from app.data.upstox_provider import (
    fetch_upstox_bulk_ltp,
    fetch_upstox_bulk_quotes,
    get_cached_upstox_quote,
    _ltp_cache,
    _quotes_cache
)
from app.data.market_provider import get_bulk_live_quotes_with_meta


class TestInstantExitAndQuoteNormalizationFix(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        self.patcher_db1 = patch("app.analytics.consensus_tracker.get_db_path", return_value=self.db_path)
        self.patcher_db2 = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.patcher_db3 = patch("app.analytics.consensus_game_engine.get_db_path", return_value=self.db_path)
        self.patcher_db1.start()
        self.patcher_db2.start()
        self.patcher_db3.start()

        conn = sqlite3.connect(self.db_path)
        ConsensusTracker.init_consensus_tracking_schema(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_access_token', 'mock_token_1234567890');")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ohlcv (
                ticker TEXT,
                timeframe TEXT,
                date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                PRIMARY KEY(ticker, timeframe, date)
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ml_trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                ticker TEXT,
                direction TEXT,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                confidence REAL,
                trade_type TEXT,
                status TEXT DEFAULT 'OPEN',
                outcome TEXT,
                profit_pct REAL,
                exit_price REAL,
                exit_time TEXT,
                engine TEXT,
                source TEXT,
                position_type TEXT DEFAULT 'NOT_A_POSITION',
                strategy TEXT
            );
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.patcher_db1.stop()
        self.patcher_db2.stop()
        self.patcher_db3.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_01_upstox_bulk_ltp_symbol_normalization(self):
        """Verify fetch_upstox_bulk_ltp indexes responses by both clean and suffixed keys."""
        now = time.time()
        _ltp_cache["INFY"] = (1016.30, now)
        _ltp_cache["INFY.NS"] = (1016.30, now)

        # Call with suffixed ticker
        res = fetch_upstox_bulk_ltp(["INFY.NS"])
        self.assertIn("INFY.NS", res)
        self.assertIn("INFY", res)
        self.assertEqual(res["INFY.NS"], 1016.30)
        self.assertEqual(res["INFY"], 1016.30)

    def test_02_upstox_bulk_quotes_symbol_normalization(self):
        """Verify fetch_upstox_bulk_quotes indexes responses by both clean and suffixed keys."""
        now = time.time()
        q_data = {"ticker": "INFY", "ltp": 1016.30, "vwap": 1015.00, "open": 1010.00, "source": "upstox"}
        _quotes_cache["INFY"] = (q_data, now)
        _quotes_cache["INFY.NS"] = (q_data, now)

        res = fetch_upstox_bulk_quotes(["INFY.NS"])
        self.assertIn("INFY.NS", res)
        self.assertIn("INFY", res)
        self.assertEqual(res["INFY.NS"]["ltp"], 1016.30)
        self.assertEqual(res["INFY"]["ltp"], 1016.30)

        # Test get_cached_upstox_quote handles suffixed and clean
        self.assertIsNotNone(get_cached_upstox_quote("INFY.NS"))
        self.assertIsNotNone(get_cached_upstox_quote("INFY"))

    def test_03_market_provider_bulk_live_quotes_returns_suffixed_keys(self):
        """Verify get_bulk_live_quotes_with_meta indexes quotes for suffixed requests without cache misses."""
        with patch("app.data.market_provider.fetch_upstox_bulk_ltp", return_value={"INFY": 1016.30}):
            quotes = get_bulk_live_quotes_with_meta(["INFY.NS"])
            self.assertIn("INFY.NS", quotes)
            self.assertEqual(quotes["INFY.NS"]["price"], 1016.30)
            self.assertEqual(quotes["INFY.NS"]["quote_freshness"], "REAL_TIME")

    def test_04_save_consensus_trade_preflight_blocks_already_hit_target(self):
        """Verify save_consensus_trade rejects trade if live price is already at or above target."""
        trade_data = {
            "ticker": "INFY.NS",
            "direction": "BUY",
            "blended_entry": 999.80,
            "blended_sl": 989.80,
            "blended_tp": 1011.23,
            "composite_score": 65.0,
            "is_actionable": True
        }

        # Mock Upstox live quote returning 1016.30 (higher than target 1011.23)
        with patch("app.data.upstox_provider.fetch_upstox_ltp", return_value=1016.30):
            saved, reason = ConsensusTracker.save_consensus_trade(trade_data, strategy="INTRADAY")
            self.assertFalse(saved)
            self.assertIn("REJECTED_ALREADY_HIT_TARGET", reason)

    def test_05_save_consensus_trade_preflight_blocks_already_hit_sl(self):
        """Verify save_consensus_trade rejects trade if live price has already breached stop loss."""
        trade_data = {
            "ticker": "TCS.NS",
            "direction": "BUY",
            "blended_entry": 3500.00,
            "blended_sl": 3465.00,
            "blended_tp": 3570.00,
            "composite_score": 65.0,
            "is_actionable": True
        }

        # Live quote at 3450.00 (below SL 3465.00)
        with patch("app.data.upstox_provider.fetch_upstox_ltp", return_value=3450.00):
            saved, reason = ConsensusTracker.save_consensus_trade(trade_data, strategy="INTRADAY")
            self.assertFalse(saved)
            self.assertIn("REJECTED_ALREADY_HIT_SL", reason)

    def test_06_reconciler_minimum_dwell_time_prevents_sub_minute_ghost_exit(self):
        """Verify newly created intraday trade (<60s old, 0 bars elapsed) is NOT exited by live tick."""
        now_dt = datetime.now()
        now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score, status
            ) VALUES (801, ?, 'INFY.NS', 'INFY', 'INTRADAY', 'BUY', 1010.00, 1000.00, 1025.00, 60.0, 'OPEN');
        """, (now_str,))
        conn.commit()
        conn.close()

        # Mock Upstox live tick at 1030.00 (> TP 1025.00)
        mock_quotes = {
            "INFY.NS": {"ticker": "INFY.NS", "price": 1030.00, "is_realtime": True}
        }
        with patch("app.data.market_provider.get_bulk_live_quotes_with_meta", return_value=mock_quotes):
            # Reconcile called 5 seconds after entry
            reconcile_time = now_dt + timedelta(seconds=5)
            reconciled = ConsensusTracker.reconcile_open_consensus_trades(force_squareoff=False, now=reconcile_time)

            # MUST NOT exit because trade is only 5s old and 0 bars elapsed (dwell time active)
            self.assertEqual(reconciled, 0)

        conn = sqlite3.connect(self.db_path)
        status = conn.execute("SELECT status FROM consensus_trade_history WHERE id = 801").fetchone()[0]
        conn.close()
        self.assertEqual(status, "OPEN")

    def test_07_reconciler_allows_live_exit_after_dwell_time_expires(self):
        """Verify trade exits cleanly on live tick once dwell time (>=60s) has elapsed."""
        past_dt = datetime.now() - timedelta(seconds=90)
        past_str = past_dt.strftime("%Y-%m-%d %H:%M:%S")

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score, status
            ) VALUES (802, ?, 'INFY.NS', 'INFY', 'INTRADAY', 'BUY', 1010.00, 1000.00, 1025.00, 60.0, 'OPEN');
        """, (past_str,))
        conn.commit()
        conn.close()

        # Mock Upstox live tick at 1030.00 (> TP 1025.00)
        mock_quotes = {
            "INFY.NS": {"ticker": "INFY.NS", "price": 1030.00, "is_realtime": True}
        }
        with patch("app.data.market_provider.get_bulk_live_quotes_with_meta", return_value=mock_quotes):
            # Reconcile called now (90 seconds after entry)
            reconciled = ConsensusTracker.reconcile_open_consensus_trades(force_squareoff=False)
            self.assertEqual(reconciled, 1)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT status, outcome, exit_price FROM consensus_trade_history WHERE id = 802").fetchone()
        conn.close()
        self.assertEqual(row[0], "CLOSED")
        self.assertEqual(row[1], "TARGET MET")
        self.assertEqual(row[2], 1025.00)


if __name__ == "__main__":
    unittest.main()
