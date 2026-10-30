"""
Unit tests for Dynamic Breakeven Ratchet in ConsensusTracker.
Verifies that winning positions lock in breakeven (+0.1%) upon achieving +0.70% floating gain.
"""

import unittest
import sqlite3
import tempfile
import os
from datetime import datetime
from unittest.mock import patch

from app.analytics.consensus_tracker import ConsensusTracker


class TestDynamicBreakevenRatchet(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        # Patch get_db_path across modules for strict test isolation
        self.patcher_db1 = patch("app.analytics.consensus_tracker.get_db_path", return_value=self.db_path)
        self.patcher_db2 = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.patcher_db3 = patch("app.analytics.consensus_game_engine.get_db_path", return_value=self.db_path)
        self.patcher_db1.start()
        self.patcher_db2.start()
        self.patcher_db3.start()

        # Initialize schema in temp db
        conn = sqlite3.connect(self.db_path)
        ConsensusTracker.init_consensus_tracking_schema(conn)

        # Create ohlcv and ml_trade_history schema
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

    def test_breakeven_ratchet_and_exit_buy(self):
        conn = sqlite3.connect(self.db_path)

        # Insert BUY trade: entry = 1000.0, sl = 985.0 (-1.5%), tp = 1030.0 (+3.0%)
        conn.execute("""
            INSERT INTO consensus_trade_history (
                timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score, status
            ) VALUES ('2026-09-16 10:00:00', 'INFY.NS', 'INFY', 'INTRADAY', 'BUY', 1000.0, 985.0, 1030.0, 85.0, 'OPEN');
        """)
        conn.commit()

        # 1. Bar 1: Price rallies to 1012.0 (+1.20% gain, crosses the new +1.10% fee-compensated threshold)
        # But Low drops to 1004.0 (above ratcheted SL of 1003.5)
        conn.execute("""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES ('INFY.NS', '15m', '2026-09-16 10:15:00', 1002.0, 1012.0, 1004.0, 1008.0, 5000);
        """)
        conn.commit()

        # Run reconciliation during market hours (11:00 IST)
        market_time = datetime(2026, 9, 16, 11, 0, 0)
        ConsensusTracker.reconcile_open_consensus_trades(force_squareoff=False, now=market_time)

        # Verify stop loss has been ratcheted to 1003.5 (fee-compensated: entry * 1.0035)
        cur = conn.cursor()
        cur.execute("SELECT stop_loss, status FROM consensus_trade_history WHERE ticker = 'INFY.NS'")
        sl_val, status = cur.fetchone()
        self.assertEqual(sl_val, 1003.5)
        self.assertEqual(status, "OPEN")

        # 2. Bar 2: Market pulls back and drops below ratcheted SL (low = 1001.0)
        conn.execute("""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES ('INFY.NS', '15m', '2026-09-16 10:30:00', 1005.0, 1006.0, 1001.0, 1002.0, 6000);
        """)
        conn.commit()

        # Reconcile again
        ConsensusTracker.reconcile_open_consensus_trades(force_squareoff=False, now=market_time)

        # Trade should be closed as ECONOMIC_BREAKEVEN at 1003.5 (+0.35% profit, net-positive cash after friction)
        cur.execute("SELECT status, outcome, exit_price, profit_pct FROM consensus_trade_history WHERE ticker = 'INFY.NS'")
        status, outcome, exit_p, pnl = cur.fetchone()
        self.assertEqual(status, "CLOSED")
        self.assertIn(outcome, ["ECONOMIC_BREAKEVEN", "BREAKEVEN HIT"])
        self.assertEqual(exit_p, 1003.5)
        self.assertGreaterEqual(pnl, 0.35)

        conn.close()

    def test_breakeven_ratchet_and_exit_sell(self):
        conn = sqlite3.connect(self.db_path)

        # Insert SELL trade: entry = 1000.0, sl = 1015.0 (+1.5%), tp = 970.0 (-3.0%)
        conn.execute("""
            INSERT INTO consensus_trade_history (
                timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score, status
            ) VALUES ('2026-09-16 10:00:00', 'TCS.NS', 'TCS', 'INTRADAY', 'SELL', 1000.0, 1015.0, 970.0, 85.0, 'OPEN');
        """)
        conn.commit()

        # 1. Bar 1: Price drops to 988.0 (+1.20% gain for short, crosses +1.10% threshold)
        conn.execute("""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES ('TCS.NS', '15m', '2026-09-16 10:15:00', 998.0, 998.0, 988.0, 992.0, 5000);
        """)
        conn.commit()

        market_time = datetime(2026, 9, 16, 11, 0, 0)
        ConsensusTracker.reconcile_open_consensus_trades(force_squareoff=False, now=market_time)

        # Verify stop loss ratcheted down to 996.5 (entry * (1 - 0.0035))
        cur = conn.cursor()
        cur.execute("SELECT stop_loss, status FROM consensus_trade_history WHERE ticker = 'TCS.NS'")
        sl_val, status = cur.fetchone()
        self.assertEqual(sl_val, 996.5)
        self.assertEqual(status, "OPEN")

        # 2. Bar 2: Market bounces back and rises above ratcheted SL (high = 999.0)
        conn.execute("""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES ('TCS.NS', '15m', '2026-09-16 10:30:00', 992.0, 999.0, 991.0, 998.0, 6000);
        """)
        conn.commit()

        ConsensusTracker.reconcile_open_consensus_trades(force_squareoff=False, now=market_time)

        cur.execute("SELECT status, outcome, exit_price, profit_pct FROM consensus_trade_history WHERE ticker = 'TCS.NS'")
        status, outcome, exit_p, pnl = cur.fetchone()
        self.assertEqual(status, "CLOSED")
        self.assertIn(outcome, ["ECONOMIC_BREAKEVEN", "BREAKEVEN HIT"])
        self.assertEqual(exit_p, 996.5)
        self.assertGreaterEqual(pnl, 0.35)

        conn.close()


if __name__ == "__main__":
    unittest.main()
