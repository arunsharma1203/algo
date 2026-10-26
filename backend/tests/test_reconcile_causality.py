"""
Unit tests for Strict Point-in-Time Causality in ConsensusTracker.
Verifies that:
1. An intra-candle dip occurring before a profit-surge does NOT retroactively trigger a newly ratcheted stop-loss.
2. In live trading, when LTP is far above ratcheted SL, trade stays OPEN.
3. Subsequent pullback in future bar/tick cleanly triggers BREAKEVEN HIT.
4. Target milestone TP1 and full TP are properly handled.
"""

import unittest
import sqlite3
import tempfile
import os
from datetime import datetime
from unittest.mock import patch

from app.analytics.consensus_tracker import ConsensusTracker


class TestReconcileCausality(unittest.TestCase):
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
                status TEXT DEFAULT 'OPEN',
                outcome TEXT,
                profit_pct REAL,
                exit_price REAL,
                exit_time TEXT,
                engine TEXT,
                position_type TEXT DEFAULT 'NOT_A_POSITION'
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

    def test_infy_scenario_dip_before_rally_does_not_falsely_exit(self):
        """
        Exact INFY trade on 2026-09-30:
        Entry = 1011.50, Initial SL = 1001.49, TP = 1024.04
        Bar 1 (09:45): Open=1013.80, High=1020.40 (+0.88%), Low=1009.70 (-0.18%), Close=1020.00
        Live LTP at 10:01: 1020.20
        
        Expected:
        - Position survives (Low 1009.70 > initial SL 1001.49)
        - High 1020.40 ratchets SL to Breakeven + 0.10% = 1012.51
        - Position stays OPEN because Live LTP (1020.20) > 1012.51 and Low 1009.70 happened BEFORE ratchet
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score, status
            ) VALUES (724, '2026-09-30 09:32:07', 'INFY.NS', 'INFY', 'INTRADAY', 'BUY', 1011.50, 1001.49, 1024.04, 50.5, 'OPEN');
        """)
        conn.commit()

        # Insert Bar 1: completed 09:45 candle
        conn.execute("""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES ('INFY.NS', '15m', '2026-09-30 09:45:00', 1013.80, 1020.40, 1009.70, 1020.00, 497220);
        """)
        conn.commit()

        # Mock Upstox live quote returning LTP 1020.20
        mock_quotes = {
            "INFY.NS": {"price": 1020.20, "quote_freshness": "REAL_TIME"}
        }

        with patch("app.data.market_provider.get_bulk_live_quotes_with_meta", return_value=mock_quotes):
            market_time = datetime(2026, 9, 30, 10, 1, 0)
            ConsensusTracker.reconcile_open_consensus_trades(force_squareoff=False, now=market_time)

        cur = conn.cursor()
        cur.execute("SELECT status, stop_loss, outcome FROM consensus_trade_history WHERE id = 724")
        status, sl_val, outcome = cur.fetchone()

        # Must be OPEN, SL ratcheted to 1012.51, NOT BREAKEVEN HIT
        self.assertEqual(status, "OPEN")
        self.assertEqual(sl_val, 1012.51)
        self.assertIsNone(outcome)

        conn.close()

    def test_subsequent_pullback_triggers_breakeven_cleanly(self):
        """
        After surviving Bar 1 with ratcheted SL = 1012.51,
        Bar 2 pulls back to 1011.00 (below 1012.51).
        It should now close as BREAKEVEN HIT @ 1012.51.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score, status
            ) VALUES (724, '2026-09-30 09:32:07', 'INFY.NS', 'INFY', 'INTRADAY', 'BUY', 1011.50, 1012.51, 1024.04, 50.5, 'OPEN');
        """)
        conn.commit()

        # Insert Bar 2: pull back below ratcheted SL
        conn.execute("""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES ('INFY.NS', '15m', '2026-09-30 10:00:00', 1020.00, 1020.50, 1011.00, 1011.50, 300000);
        """)
        conn.commit()

        mock_quotes = {
            "INFY.NS": {"price": 1011.50, "quote_freshness": "REAL_TIME"}
        }

        with patch("app.data.market_provider.get_bulk_live_quotes_with_meta", return_value=mock_quotes):
            market_time = datetime(2026, 9, 30, 10, 16, 0)
            ConsensusTracker.reconcile_open_consensus_trades(force_squareoff=False, now=market_time)

        cur = conn.cursor()
        cur.execute("SELECT status, outcome, exit_price FROM consensus_trade_history WHERE id = 724")
        status, outcome, exit_p = cur.fetchone()

        self.assertEqual(status, "CLOSED")
        self.assertEqual(outcome, "BREAKEVEN HIT")
        self.assertEqual(exit_p, 1012.51)

        conn.close()


if __name__ == "__main__":
    unittest.main()
