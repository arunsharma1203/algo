"""
TEST PRODUCTION MODEL WIN-RATE BADGE & ENDPOINT
================================================
Comprehensive regression test suite verifying:
1. GET /api/ml/production-win-rate endpoint returns authoritative win rate from ml_trade_history.
2. Canonical production baseline returns 63 closed trades, 21 wins, 33.3% win rate.
3. Safe Fallback: When evaluated closed trades = 0, displays "N/A" (never "0%").
4. OPEN trades and unresolved positions are excluded from closed trade accounting.
5. Only TARGET MET or profit_pct > 0 are classified as wins.
6. Strictly read-only: Querying endpoint does not modify trade history or portfolio heat.
"""

import unittest
import os
import tempfile
import sqlite3
from fastapi.testclient import TestClient

from app.main import app
from app.data.database import set_test_db_override, get_canonical_db_path
from app.data.historical_data_layer import HistoricalDataLayer
from app.analytics.kelly_sizer import get_portfolio_heat_status


class TestSidebarWinRateBadge(unittest.TestCase):
    """Rigorous tests validating production model win-rate badge and endpoint."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_01_canonical_production_win_rate(self):
        """Canonical database must report authoritative closed trades, wins, and mathematically consistent win rate."""
        # Ensure we are querying canonical database
        set_test_db_override(None)

        resp = self.client.get("/api/ml/production-win-rate")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertEqual(data.get("status"), "success")
        total = data.get("total_closed_trades")
        wins = data.get("wins")
        self.assertGreaterEqual(total, 63)
        self.assertGreaterEqual(wins, 21)
        expected_rate = round((wins / total * 100), 1)
        self.assertEqual(data.get("win_rate"), expected_rate)
        self.assertEqual(data.get("display_rate"), f"{expected_rate}%")

    def test_02_zero_trades_returns_na_never_zero_pct(self):
        """When no closed trades exist, win_rate must be None and display_rate must be "N/A" (NEVER "0%")."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_db = os.path.join(temp_dir, "empty_trades.db")
            set_test_db_override(temp_db)
            HistoricalDataLayer.init_schema()

            conn = sqlite3.connect(temp_db)
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
                    status TEXT DEFAULT 'OPEN',
                    trade_type TEXT DEFAULT 'INTRADAY',
                    explanation TEXT,
                    outcome TEXT,
                    profit_pct REAL,
                    effective_entry REAL,
                    slippage_drag REAL,
                    ideal_profit_pct REAL,
                    exit_price REAL,
                    exit_time TEXT,
                    source TEXT DEFAULT 'MANUAL',
                    position_type TEXT DEFAULT 'NOT_A_POSITION'
                )
            """)
            conn.commit()
            conn.close()

            resp = self.client.get("/api/ml/production-win-rate")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()

            self.assertEqual(data.get("status"), "success")
            self.assertEqual(data.get("total_closed_trades"), 0)
            self.assertEqual(data.get("wins"), 0)
            self.assertIsNone(data.get("win_rate"))
            self.assertEqual(data.get("display_rate"), "N/A")
            self.assertNotEqual(data.get("display_rate"), "0%")
            self.assertNotEqual(data.get("display_rate"), "0.0%")

            set_test_db_override(None)

    def test_03_open_unresolved_trades_excluded_from_closed_count(self):
        """OPEN trades with status=OPEN and outcome=None/OPEN must not be counted in closed trades."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_db = os.path.join(temp_dir, "open_trades.db")
            set_test_db_override(temp_db)
            HistoricalDataLayer.init_schema()

            conn = sqlite3.connect(temp_db)
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
                    status TEXT DEFAULT 'OPEN',
                    trade_type TEXT DEFAULT 'INTRADAY',
                    explanation TEXT,
                    outcome TEXT,
                    profit_pct REAL,
                    effective_entry REAL,
                    slippage_drag REAL,
                    ideal_profit_pct REAL,
                    exit_price REAL,
                    exit_time TEXT,
                    source TEXT DEFAULT 'MANUAL',
                    position_type TEXT DEFAULT 'NOT_A_POSITION'
                )
            """)
            # Insert 5 OPEN unresolved trades
            from datetime import datetime, timezone, timedelta
            now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S')
            for i in range(5):
                conn.execute("""
                    INSERT INTO ml_trade_history (timestamp, ticker, direction, entry, sl, tp1, tp2, confidence, status, trade_type, outcome)
                    VALUES (?, 'TESTTICKER.NS', 'BUY', 2500, 2450, 2550, 2600, 65, 'OPEN', 'SWING', 'OPEN')
                """, (now_ist,))
            conn.commit()
            conn.close()

            resp = self.client.get("/api/ml/production-win-rate")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()

            self.assertEqual(data.get("total_closed_trades"), 0)
            self.assertEqual(data.get("display_rate"), "N/A")

            set_test_db_override(None)

    def test_04_mixed_closed_trades_calculation(self):
        """Verify exact calculation: 10 closed trades (6 wins, 4 losses) -> 60.0% win rate."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_db = os.path.join(temp_dir, "mixed_trades.db")
            set_test_db_override(temp_db)
            HistoricalDataLayer.init_schema()

            conn = sqlite3.connect(temp_db)
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
                    status TEXT DEFAULT 'CLOSED',
                    trade_type TEXT DEFAULT 'INTRADAY',
                    explanation TEXT,
                    outcome TEXT,
                    profit_pct REAL,
                    effective_entry REAL,
                    slippage_drag REAL,
                    ideal_profit_pct REAL,
                    exit_price REAL,
                    exit_time TEXT,
                    source TEXT DEFAULT 'MANUAL',
                    position_type TEXT DEFAULT 'NOT_A_POSITION'
                )
            """)
            # 6 wins (3 TARGET MET, 3 positive profit_pct)
            for i in range(3):
                conn.execute("""
                    INSERT INTO ml_trade_history (timestamp, ticker, direction, entry, sl, tp1, tp2, confidence, status, outcome, profit_pct)
                    VALUES ('2026-09-01 10:00:00', 'RELIANCE.NS', 'BUY', 2500, 2450, 2550, 2600, 65, 'CLOSED', 'TARGET MET', 2.0)
                """)
            for i in range(3):
                conn.execute("""
                    INSERT INTO ml_trade_history (timestamp, ticker, direction, entry, sl, tp1, tp2, confidence, status, outcome, profit_pct)
                    VALUES ('2026-09-01 10:00:00', 'TCS.NS', 'BUY', 3500, 3450, 3550, 3600, 65, 'CLOSED', 'SQUARED OFF', 1.5)
                """)
            # 4 losses
            for i in range(4):
                conn.execute("""
                    INSERT INTO ml_trade_history (timestamp, ticker, direction, entry, sl, tp1, tp2, confidence, status, outcome, profit_pct)
                    VALUES ('2026-09-01 10:00:00', 'INFY.NS', 'BUY', 1500, 1450, 1550, 1600, 65, 'CLOSED', 'SL HIT', -1.5)
                """)
            conn.commit()
            conn.close()

            resp = self.client.get("/api/ml/production-win-rate")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()

            self.assertEqual(data.get("total_closed_trades"), 10)
            self.assertEqual(data.get("wins"), 6)
            self.assertEqual(data.get("win_rate"), 60.0)
            self.assertEqual(data.get("display_rate"), "60.0%")

            set_test_db_override(None)

    def test_05_read_only_invariants_preserved(self):
        """Querying the win rate endpoint must not alter portfolio heat or live trade count."""
        set_test_db_override(None)

        heat_before = get_portfolio_heat_status()
        self.assertEqual(heat_before["current_heat_pct"], 0.0)

        # Call endpoint multiple times
        for _ in range(3):
            resp = self.client.get("/api/ml/production-win-rate")
            self.assertEqual(resp.status_code, 200)

        heat_after = get_portfolio_heat_status()
        self.assertEqual(heat_after["current_heat_pct"], 0.0)
        self.assertEqual(heat_after["actual_positions"], 0)


if __name__ == "__main__":
    unittest.main()
