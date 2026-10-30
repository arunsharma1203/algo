"""
REGRESSION TEST SUITE: QLIB PATH RESOLUTION & SCAN PNL INTEGRITY
================================================================
Comprehensive verification for:
1. Qlib Alpha158 Staged Discovery Champion Path Resolution (CWD-independent)
2. PVRINOX Stop-Loss Evaluation and Real-Time Breach Detection
3. Intraday & Swing P&L Pipeline Formula Correctness (No Overwrite Bug)
4. Fresh LTP vs Stale Fallback Distinction
5. Production Safety Invariants & Historical Data Immutability
"""

import os
import sys
import unittest
import tempfile
import sqlite3
import hashlib
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock

from app.analytics.qlib_discovery.qlib_registry import (
    verify_champion_immutability,
    get_authoritative_champion_path,
    CHAMPION_PATHS,
    CHAMPION_HASHES
)
from app.analytics.model_manager import ModelManager
from app.data.historical_data_layer import get_db_path
from app.data.database import get_canonical_db_path
from app.api.ml_history import evaluate_ml_history, save_ml_trade, ensure_ml_table
from app.analytics.research_orchestrator.research_orchestrator import ResearchOrchestrator


class TestQlibPathResolutionAndSafety(unittest.TestCase):
    """Verifies Qlib model path resolution across arbitrary working directories."""

    def test_01_authoritative_champion_paths_exist(self):
        """Authoritative champion paths must exist and resolve to valid files."""
        for tf in ["intraday", "swing"]:
            path = get_authoritative_champion_path(tf)
            self.assertTrue(os.path.isabs(path), f"Path {path} is not absolute")
            self.assertTrue(os.path.exists(path), f"Champion model missing at {path}")

    def test_02_champion_dict_interface(self):
        """CHAMPION_PATHS dict interface resolves correctly."""
        self.assertTrue(os.path.exists(CHAMPION_PATHS["intraday"]))
        self.assertTrue(os.path.exists(CHAMPION_PATHS["swing"]))

    def test_03_verify_champion_immutability_cwd_independence(self):
        """verify_champion_immutability must succeed regardless of current working directory."""
        original_cwd = os.getcwd()
        try:
            # Test from project root
            self.assertTrue(verify_champion_immutability())

            # Test from backend/ directory
            backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            os.chdir(backend_dir)
            self.assertTrue(verify_champion_immutability())

            # Test from a clean temp directory
            with tempfile.TemporaryDirectory() as tmpdir:
                os.chdir(tmpdir)
                self.assertTrue(verify_champion_immutability())
        finally:
            os.chdir(original_cwd)

    def test_04_champion_hashes_exact_match(self):
        """Byte-for-byte verification of production Champion models."""
        for tf, exp_hash in CHAMPION_HASHES.items():
            path = get_authoritative_champion_path(tf)
            with open(path, "rb") as f:
                act_hash = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(act_hash, exp_hash, f"{tf} Champion hash mismatch!")

    def test_05_missing_model_produces_clear_error(self):
        """If a champion model file is missing, verify_champion_immutability raises clear FileNotFoundError."""
        with patch("app.analytics.qlib_discovery.qlib_registry.get_authoritative_champion_path", return_value="/nonexistent/path/champion.pkl"):
            with self.assertRaises(FileNotFoundError) as ctx:
                verify_champion_immutability()
            self.assertIn("Champion artifact missing", str(ctx.exception))

    def test_06_no_duplicate_model_artifacts_created(self):
        """Verify that path resolution does not copy or symlink duplicate model files."""
        intraday_dir = os.path.dirname(get_authoritative_champion_path("intraday"))
        files = os.listdir(intraday_dir)
        pkl_files = [f for f in files if f.endswith(".pkl")]
        self.assertEqual(len(pkl_files), 1, f"Found extra pkl files in {intraday_dir}: {pkl_files}")


class TestPVRINOXStopLossAndPnLPipeline(unittest.TestCase):
    """Verifies stop loss evaluation, P&L formulas, and outcome persistence in ml_history."""

    def setUp(self):
        # Create isolated temporary database for test execution
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db.name
        self.temp_db.close()

        # Initialize schema in temp DB
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("""
            CREATE TABLE ml_trade_history (
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
                position_type TEXT DEFAULT 'NOT_A_POSITION',
                tightened_sl REAL,
                ai_guard_action TEXT,
                risk_level TEXT DEFAULT 'NORMAL',
                risk_reasons TEXT,
                risk_updated_at TEXT,
                current_price REAL,
                reference_price REAL,
                model_candle_close REAL,
                price_source TEXT,
                price_timestamp TEXT,
                price_is_fresh INTEGER DEFAULT 0
            );
        """)
        conn.commit()
        conn.close()

        # Patch get_db_path across relevant modules
        self.db_patcher = patch("app.data.historical_data_layer.get_db_path", return_value=self.temp_db_path)
        self.db_patcher2 = patch("app.api.ml_history.get_db_path", return_value=self.temp_db_path)
        self.db_patcher.start()
        self.db_patcher2.start()

    def tearDown(self):
        self.db_patcher2.stop()
        self.db_patcher.stop()
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    def test_07_pvrinox_long_sl_hit_when_low_breaches(self):
        """LONG swing trade reaches SL HIT when candle low <= stop loss."""
        # Insert test PVRINOX trade with exact stored parameters
        entry = 1221.90
        sl = 1143.19
        tp1 = 1339.97
        entry_time = "2026-09-04T09:33:51.938169"

        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("""
            INSERT INTO ml_trade_history (
                id, timestamp, ticker, direction, entry, sl, tp1, confidence, trade_type, status, outcome
            ) VALUES (87, ?, 'PVRINOX.NS', 'BULLISH', ?, ?, ?, 41.7, 'SWING', 'OPEN', 'OPEN')
        """, (entry_time, entry, sl, tp1))
        conn.commit()
        conn.close()

        # Synthetic 15m market candles: candle 1 normal, candle 2 low breaches SL (1126.60)
        dt1 = pd.Timestamp("2026-09-04 10:00:00")
        dt2 = pd.Timestamp("2026-09-07 09:15:00")
        mock_candles = pd.DataFrame({
            "High": [1230.0, 1198.80],
            "Low": [1215.0, 1126.60],  # Breached!
            "Close": [1225.0, 1138.80]
        }, index=[dt1, dt2])

        with patch("app.api.ml_history.yf.download", return_value=mock_candles):
            with patch("app.data.market_provider.get_live_quote_with_meta", return_value={
                "ticker": "PVRINOX.NS", "price": 1162.0, "source_name": "Live Feed", "is_realtime": True, "timestamp": "09:45:00 IST"
            }):
                results = evaluate_ml_history(force_refresh=True)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["status"], "CLOSED")
        self.assertEqual(r["outcome"], "SL HIT")
        self.assertEqual(r["exit_price"], sl)
        self.assertAlmostEqual(r["ideal_profit_pct"], ((sl - entry) / entry) * 100, places=2)
        # Verify persistence into SQLite
        conn = sqlite3.connect(self.temp_db_path)
        row = conn.execute("SELECT status, outcome, profit_pct, exit_price FROM ml_trade_history WHERE id = 87").fetchone()
        conn.close()
        self.assertEqual(row[0], "CLOSED")
        self.assertEqual(row[1], "SL HIT")
        self.assertEqual(row[3], sl)

    def test_08_short_trade_sl_hit_when_high_breaches(self):
        """BEARISH trade reaches SL HIT when candle high >= stop loss."""
        entry = 500.0
        sl = 515.0
        tp1 = 475.0
        entry_time = "2026-09-07T09:15:00"

        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("""
            INSERT INTO ml_trade_history (
                id, timestamp, ticker, direction, entry, sl, tp1, confidence, trade_type, status, outcome
            ) VALUES (1, ?, 'XYZ.NS', 'BEARISH', ?, ?, ?, 65.0, 'INTRADAY', 'OPEN', 'OPEN')
        """, (entry_time, entry, sl, tp1))
        conn.commit()
        conn.close()

        dt = pd.Timestamp("2026-09-07 09:30:00")
        mock_candles = pd.DataFrame({
            "High": [518.0],  # Breached SL!
            "Low": [498.0],
            "Close": [516.0]
        }, index=[dt])

        with patch("app.api.ml_history.yf.download", return_value=mock_candles):
            with patch("app.data.market_provider.get_live_quote_with_meta", return_value={"ticker": "XYZ.NS", "price": 516.0}):
                results = evaluate_ml_history(force_refresh=True)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "CLOSED")
        self.assertEqual(results[0]["outcome"], "SL HIT")
        self.assertEqual(results[0]["exit_price"], sl)

    def test_09_bullish_open_pnl_formula_not_overwritten(self):
        """Verifies that bullish mark-to-market P&L is NOT overwritten with bearish formula."""
        entry = 1000.0
        sl = 950.0
        tp1 = 1100.0
        current_price = 1050.0  # Price went UP by 5%
        entry_time = (datetime.now() - timedelta(minutes=30)).isoformat()

        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("""
            INSERT INTO ml_trade_history (
                id, timestamp, ticker, direction, entry, sl, tp1, confidence, trade_type, status, outcome
            ) VALUES (2, ?, 'BULL.NS', 'BULLISH', ?, ?, ?, 60.0, 'INTRADAY', 'OPEN', 'OPEN')
        """, (entry_time, entry, sl, tp1))
        conn.commit()
        conn.close()

        dt = pd.Timestamp(datetime.now())
        mock_candles = pd.DataFrame({
            "High": [1055.0],
            "Low": [995.0],
            "Close": [1050.0]
        }, index=[dt])

        with patch("app.api.ml_history.yf.download", return_value=mock_candles):
            with patch("app.data.market_provider.get_live_quote_with_meta", return_value={
                "ticker": "BULL.NS", "price": current_price, "source_name": "Upstox Real-Time", "is_realtime": True, "timestamp": "10:00:00 IST"
            }):
                results = evaluate_ml_history(force_refresh=True)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["status"], "OPEN")
        # In the bug, profit_pct would have been negative (-4.69%) because bearish formula overwrote it!
        # With the fix, profit_pct MUST be POSITIVE (~ +4.92% net of slippage)
        self.assertGreater(r["profit_pct"], 0.0, "Bullish OPEN trade with price gain was incorrectly calculated as negative!")
        self.assertEqual(r["current_price"], current_price)
        self.assertTrue(r["price_is_fresh"])

    def test_10_bearish_open_pnl_formula(self):
        """Verifies that bearish mark-to-market P&L is positive when price drops."""
        entry = 1000.0
        sl = 1050.0
        tp1 = 900.0
        current_price = 950.0  # Price went DOWN by 5%
        entry_time = (datetime.now() - timedelta(minutes=30)).isoformat()

        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("""
            INSERT INTO ml_trade_history (
                id, timestamp, ticker, direction, entry, sl, tp1, confidence, trade_type, status, outcome
            ) VALUES (3, ?, 'BEAR.NS', 'BEARISH', ?, ?, ?, 60.0, 'INTRADAY', 'OPEN', 'OPEN')
        """, (entry_time, entry, sl, tp1))
        conn.commit()
        conn.close()

        dt = pd.Timestamp(datetime.now())
        mock_candles = pd.DataFrame({
            "High": [1005.0],
            "Low": [945.0],
            "Close": [950.0]
        }, index=[dt])

        with patch("app.api.ml_history.yf.download", return_value=mock_candles):
            with patch("app.data.market_provider.get_live_quote_with_meta", return_value={
                "ticker": "BEAR.NS", "price": current_price, "source_name": "Live Feed", "is_realtime": False, "timestamp": "10:00:00 IST"
            }):
                results = evaluate_ml_history(force_refresh=True)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["status"], "OPEN")
        self.assertGreater(r["profit_pct"], 0.0, "Bearish OPEN trade with price drop should be profitable!")

    def test_11_target_met_evaluation(self):
        """Verifies that TARGET MET is triggered and persisted when high >= tp1."""
        entry = 100.0
        sl = 90.0
        tp1 = 115.0
        entry_time = (datetime.now() - timedelta(hours=1)).isoformat()

        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("""
            INSERT INTO ml_trade_history (
                id, timestamp, ticker, direction, entry, sl, tp1, confidence, trade_type, status, outcome
            ) VALUES (4, ?, 'TGT.NS', 'BULLISH', ?, ?, ?, 70.0, 'INTRADAY', 'OPEN', 'OPEN')
        """, (entry_time, entry, sl, tp1))
        conn.commit()
        conn.close()

        dt = pd.Timestamp(datetime.now())
        mock_candles = pd.DataFrame({
            "High": [120.0],  # Hit target!
            "Low": [98.0],
            "Close": [118.0]
        }, index=[dt])

        with patch("app.api.ml_history.yf.download", return_value=mock_candles):
            with patch("app.data.market_provider.get_live_quote_with_meta", return_value={"ticker": "TGT.NS", "price": 118.0}):
                results = evaluate_ml_history(force_refresh=True)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "CLOSED")
        self.assertEqual(results[0]["outcome"], "TARGET MET")
        self.assertEqual(results[0]["exit_price"], tp1)

    def test_12_stale_quote_marked_properly(self):
        """When live quote is delayed or unavailable, price_is_fresh is False."""
        entry = 500.0
        sl = 450.0
        tp1 = 550.0
        entry_time = (datetime.now() - timedelta(minutes=15)).isoformat()

        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("""
            INSERT INTO ml_trade_history (
                id, timestamp, ticker, direction, entry, sl, tp1, confidence, trade_type, status, outcome
            ) VALUES (5, ?, 'STALE.NS', 'BULLISH', ?, ?, ?, 55.0, 'INTRADAY', 'OPEN', 'OPEN')
        """, (entry_time, entry, sl, tp1))
        conn.commit()
        conn.close()

        with patch("app.api.ml_history.yf.download", return_value=pd.DataFrame()):
            with patch("app.data.market_provider.get_live_quote_with_meta", return_value={
                "ticker": "STALE.NS", "price": 500.0, "source_name": "Yahoo Finance (15m Delay)", "is_realtime": False, "timestamp": "09:30:00 IST"
            }):
                results = evaluate_ml_history(force_refresh=True)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["price_is_fresh"])


class TestSafetyInvariantsAndProductionIntegrity(unittest.TestCase):
    """Verifies SQLite integrity check, research preservation, and safety invariants."""

    def test_13_canonical_database_integrity(self):
        """PRAGMA integrity_check on canonical DB must return ok."""
        db_path = get_canonical_db_path()
        conn = sqlite3.connect(db_path)
        res = conn.execute("PRAGMA integrity_check;").fetchall()
        conn.close()
        self.assertEqual(res, [("ok",)])

    def test_14_research_data_preserved(self):
        """Historical research missions, experiments, and ledger remain intact."""
        db_path = get_canonical_db_path()
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        missions = cur.execute("SELECT count(*) FROM research_missions;").fetchone()[0]
        ledger = cur.execute("SELECT count(*) FROM research_experiments_ledger;").fetchone()[0]
        vault = cur.execute("SELECT count(*) FROM research_candidate_vault;").fetchone()[0]
        conn.close()
        self.assertGreaterEqual(missions, 200, "Research missions deleted!")
        self.assertGreaterEqual(ledger, 1000, "Research experiment ledger deleted!")
        self.assertGreaterEqual(vault, 600, "Research candidate vault deleted!")

    def test_15_production_safety_invariants_pass(self):
        """ResearchOrchestrator safety invariants pass 100%."""
        safety = ResearchOrchestrator.verify_production_safety_invariants()
        self.assertEqual(safety["status"], "PASS")
        self.assertTrue(safety["all_invariants_preserved"])
        self.assertTrue(safety["intraday_champion_match"])
        self.assertTrue(safety["swing_champion_match"])
        self.assertEqual(safety["open_positions"], 0)
        self.assertEqual(safety["portfolio_heat_pct"], 0.0)
        self.assertEqual(safety["broker_orders"], 0)
        self.assertEqual(safety["telegram_alerts"], 0)


if __name__ == "__main__":
    unittest.main()
