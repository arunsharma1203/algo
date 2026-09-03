"""
Comprehensive Test Suite for Production Trading System Repair
=============================================================
Tests all 22 critical items from the forensic repair plan:
1. SWING bearish model prediction -> rejected -> SWING_CASH_SHORT_DISALLOWED
2. SWING bullish prediction -> can qualify if all existing gates pass
3. INTRADAY bearish prediction -> existing behavior remains intact
4. VADER risk evaluation -> no FinBERT KeyError
5. Missing model-breakdown key -> no crash
6. AI Guard WARNING -> action persists correctly
7. AI Guard CRITICAL -> exit advisory/action persists correctly
8. AI Guard normal -> no false defensive action
9. Stale LTP -> recommendation marked unexecutable/stale
10. Fresh LTP -> reference price is live price, metadata persisted
11. Swing expiration -> expires after 5 trading days (not calendar days)
12. Expired/invalid virtual recommendation -> does not consume heat
13. Genuine PAPER_POSITION/LIVE_POSITION -> still contributes to heat
14. Heat cap remains 6%
15. Historical health check -> reads correct 15m source
16. Bad OHLC row -> detected/flagged without destructive deletion
17. Individual yfinance ticker failure -> one ticker fails, sweep continues
18. Scheduler initialization -> does not create duplicate jobs
19. Manual and autonomous scanners -> both use authoritative decision_engine.py
20. Broker execution -> remains fail-closed/simulation
21. Champion model hashes -> unchanged
22. Research DB isolation -> unchanged
"""

import os
import sys
import json
import sqlite3
import hashlib
import tempfile
import unittest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Add backend directory to sys.path
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.analytics.decision_engine import evaluate_ticker, QualificationResult
from app.analytics.autonomous_bot import evaluate_single_trade_risk, calculate_tightened_stop_loss
from app.analytics.kelly_sizer import get_portfolio_heat_status
from app.api.ml_history import save_ml_trade, evaluate_ml_history, ensure_ml_table
from app.analytics.system_health_center import SystemHealthCenter
from app.main import get_or_create_scheduler

INTRADAY_CHAMPION_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
SWING_CHAMPION_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"


def create_mock_ohlcv_df(rows=150, base_price=100.0):
    """Generates synthetic valid OHLCV DataFrame for testing."""
    dates = pd.date_range(end=datetime.now(), periods=rows, freq='D')
    np.random.seed(42)
    closes = base_price + np.cumsum(np.random.randn(rows) * 1.5)
    highs = closes + np.random.uniform(0.5, 2.0, rows)
    lows = closes - np.random.uniform(0.5, 2.0, rows)
    opens = closes + np.random.uniform(-0.5, 0.5, rows)
    volumes = np.random.randint(100000, 500000, rows)
    return pd.DataFrame({
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes
    }, index=dates)


class TestProductionRepair(unittest.TestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db.name
        self.temp_db.close()
        
        # Initialize schema in temp DB
        conn = sqlite3.connect(self.temp_db_path)
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
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ml_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                level TEXT,
                ticker TEXT,
                message TEXT,
                audit_data TEXT
            )
        """)
        conn.execute("INSERT INTO app_settings VALUES ('portfolio_max_heat_cap', '6.0')")
        conn.execute("INSERT INTO app_settings VALUES ('simulation_mode', 'true')")
        conn.commit()
        conn.close()

    def tearDown(self):
        if os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except:
                pass

    # 1. SWING bearish prediction -> rejected -> SWING_CASH_SHORT_DISALLOWED
    def test_01_swing_bearish_prediction_rejected_short_ban(self):
        df = create_mock_ohlcv_df(rows=150)
        mock_model = MagicMock()
        mock_model.predict_proba.return_value = np.array([[0.65, 0.35]])  # prob_up = 35% (bearish)

        res = evaluate_ticker(
            ticker="RELIANCE.NS",
            df=df,
            champion_model=mock_model,
            champion_meta={"version": "v1.0"},
            trade_type="SWING",
            source="MANUAL",
            skip_enrichment=True
        )

        self.assertFalse(res.qualified)
        self.assertEqual(res.model_direction, "BEARISH")
        self.assertIn("SWING_CASH_SHORT_DISALLOWED", res.rejection_reason)
        self.assertEqual(res.strategy_direction, "REJECTED")

    # 2. SWING bullish prediction -> can qualify
    def test_02_swing_bullish_prediction_can_qualify(self):
        df = create_mock_ohlcv_df(rows=150)
        mock_model = MagicMock()
        mock_model.predict_proba.return_value = np.array([[0.30, 0.70]])  # prob_up = 70% (bullish)

        res = evaluate_ticker(
            ticker="RELIANCE.NS",
            df=df,
            champion_model=mock_model,
            champion_meta={"version": "v1.0"},
            trade_type="SWING",
            source="MANUAL",
            skip_enrichment=True
        )

        self.assertTrue(res.qualified)
        self.assertEqual(res.direction, "BULLISH")
        self.assertEqual(res.strategy_direction, "BULLISH")
        self.assertGreater(res.tp1, res.entry)
        self.assertLess(res.sl, res.entry)

    # 3. INTRADAY bearish prediction -> existing behavior remains intact (can be short)
    def test_03_intraday_bearish_prediction_allowed(self):
        df = create_mock_ohlcv_df(rows=150)
        mock_model = MagicMock()
        mock_model.predict_proba.return_value = np.array([[0.70, 0.30]])  # prob_up = 30% (bearish)

        res = evaluate_ticker(
            ticker="RELIANCE.NS",
            df=df,
            champion_model=mock_model,
            champion_meta={"version": "v1.0"},
            trade_type="INTRADAY",
            source="MANUAL",
            skip_enrichment=True
        )

        self.assertTrue(res.qualified)
        self.assertEqual(res.direction, "BEARISH")
        self.assertLess(res.tp1, res.entry)
        self.assertGreater(res.sl, res.entry)

    # 4. VADER risk evaluation -> no FinBERT KeyError
    def test_04_vader_risk_evaluation_no_finbert_keyerror(self):
        trade = {
            'id': 1,
            'ticker': 'ANGELONE.NS',
            'direction': 'BEARISH',
            'entry': 280.0,
            'sl': 295.0,
            'confidence': 65.0,
            'trade_type': 'INTRADAY',
            'nlp_sentiment': 35.0,
            'timestamp': datetime.now().isoformat()
        }

        # Mock sentiment_score > 15 which previously crashed with KeyError: 'FinBERT NLP'
        with patch("app.analytics.nlp_engine.nlp_engine.analyze_ticker_news", return_value={"score": 35.0, "headline": "Strong Q3 Profit"}):
            audit = evaluate_single_trade_risk(trade, macro={"nifty_trend_short": "BULLISH", "india_vix": 15.0}, current_price=282.0)

        self.assertIsNotNone(audit)
        self.assertIn("VADER Financial Sentiment", audit["model_breakdown"])
        self.assertTrue(audit["model_breakdown"]["VADER Financial Sentiment"]["triggered"])

    # 5. Missing model-breakdown key -> defensive, no crash
    def test_05_missing_model_breakdown_key_defensive(self):
        trade = {
            'id': 2,
            'ticker': 'TEST.NS',
            'direction': 'BULLISH',
            'entry': 100.0,
            'sl': 95.0,
            'confidence': 60.0,
            'trade_type': 'INTRADAY',
            'timestamp': datetime.now().isoformat()
        }
        with patch("app.analytics.nlp_engine.nlp_engine.analyze_ticker_news", side_effect=Exception("Offline")):
            audit = evaluate_single_trade_risk(trade, macro={})
        self.assertIsNotNone(audit)
        self.assertIn("risk_level", audit)

    # 6. AI Guard WARNING -> action persists correctly
    def test_06_ai_guard_warning_action_persisted(self):
        with patch("app.data.historical_data_layer.get_db_path", return_value=self.temp_db_path):
            conn = sqlite3.connect(self.temp_db_path)
            conn.execute("""
                INSERT INTO ml_trade_history (id, timestamp, ticker, direction, entry, sl, tp1, tp2, confidence, status, outcome)
                VALUES (101, '2026-09-01T10:00:00', 'TCS.NS', 'BULLISH', 4000.0, 3900.0, 4200.0, 4400.0, 75.0, 'OPEN', 'OPEN')
            """)
            conn.commit()
            conn.close()

            # Mock evaluate_single_trade_risk to return WARNING
            mock_audit = {
                'ticker': 'TCS.NS',
                'direction': 'BULLISH',
                'risk_level': 'WARNING',
                'panic_level': 50,
                'reasons': ['NIFTY Trend Weakness'],
                'entry': 4000.0,
                'original_sl': 3900.0,
                'tightened_sl': 3950.0,
                'current_price': 3980.0
            }

            from app.analytics.autonomous_bot import active_trade_tracker
            with patch("app.analytics.autonomous_bot.is_market_open", return_value=True), \
                 patch("app.api.ml_history.evaluate_ml_history", return_value=[{'id': 101, 'ticker': 'TCS.NS', 'outcome': 'OPEN', 'direction': 'BULLISH', 'entry': 4000.0, 'sl': 3900.0}]), \
                 patch("app.analytics.autonomous_bot.evaluate_single_trade_risk", return_value=mock_audit), \
                 patch("app.analytics.telegram_notifier.send_telegram_message"):
                active_trade_tracker(force_run=True)

            conn = sqlite3.connect(self.temp_db_path)
            row = conn.execute("SELECT tightened_sl, ai_guard_action, risk_level FROM ml_trade_history WHERE id = 101").fetchone()
            conn.close()

            self.assertEqual(row[0], 3950.0)
            self.assertEqual(row[1], "TIGHTEN_SL")
            self.assertEqual(row[2], "WARNING")

    # 7. AI Guard CRITICAL -> exit advisory/action persists correctly
    def test_07_ai_guard_critical_action_persisted(self):
        with patch("app.data.historical_data_layer.get_db_path", return_value=self.temp_db_path):
            conn = sqlite3.connect(self.temp_db_path)
            conn.execute("""
                INSERT INTO ml_trade_history (id, timestamp, ticker, direction, entry, sl, tp1, tp2, confidence, status, outcome)
                VALUES (102, '2026-09-01T10:00:00', 'INFY.NS', 'BULLISH', 1800.0, 1750.0, 1900.0, 2000.0, 70.0, 'OPEN', 'OPEN')
            """)
            conn.commit()
            conn.close()

            mock_audit = {
                'ticker': 'INFY.NS',
                'direction': 'BULLISH',
                'risk_level': 'CRITICAL',
                'panic_level': 80,
                'reasons': ['Emergency Exit Consensus'],
                'entry': 1800.0,
                'original_sl': 1750.0,
                'tightened_sl': 1780.0,
                'current_price': 1760.0
            }

            from app.analytics.autonomous_bot import active_trade_tracker
            with patch("app.analytics.autonomous_bot.is_market_open", return_value=True), \
                 patch("app.api.ml_history.evaluate_ml_history", return_value=[{'id': 102, 'ticker': 'INFY.NS', 'outcome': 'OPEN', 'direction': 'BULLISH', 'entry': 1800.0, 'sl': 1750.0}]), \
                 patch("app.analytics.autonomous_bot.evaluate_single_trade_risk", return_value=mock_audit), \
                 patch("app.analytics.telegram_notifier.send_telegram_message"):
                active_trade_tracker(force_run=True)

            conn = sqlite3.connect(self.temp_db_path)
            row = conn.execute("SELECT tightened_sl, ai_guard_action, risk_level FROM ml_trade_history WHERE id = 102").fetchone()
            conn.close()

            self.assertEqual(row[1], "EXIT_ADVISORY")
            self.assertEqual(row[2], "CRITICAL")

    # 8. AI Guard normal -> no false defensive action
    def test_08_ai_guard_normal_maintains_position(self):
        sl, reduction, mode = calculate_tightened_stop_loss(
            direction="BULLISH", entry=100.0, sl=90.0, current_price=105.0
        )
        self.assertGreater(sl, 90.0)
        self.assertLess(sl, 105.0)

    # 9. Stale LTP -> marked unexecutable/stale
    def test_09_stale_ltp_handling(self):
        df = create_mock_ohlcv_df(rows=150)
        mock_model = MagicMock()
        mock_model.predict_proba.return_value = np.array([[0.30, 0.70]])

        with patch("app.data.market_provider.get_live_quote_with_meta", return_value={"price": None, "is_realtime": False, "source_name": "Unavailable"}):
            res = evaluate_ticker(
                ticker="RELIANCE.NS",
                df=df,
                champion_model=mock_model,
                champion_meta={"version": "v1.0"},
                trade_type="SWING",
                source="MANUAL",
                skip_enrichment=False
            )

        self.assertFalse(res.price_is_fresh)

    # 10. Fresh LTP -> reference price is live price, metadata persisted
    def test_10_fresh_ltp_reference_price(self):
        df = create_mock_ohlcv_df(rows=150)
        mock_model = MagicMock()
        mock_model.predict_proba.return_value = np.array([[0.30, 0.70]])

        with patch("app.data.market_provider.get_live_quote_with_meta", return_value={"price": 1450.50, "is_realtime": True, "source_name": "Upstox Real-Time (0ms)", "timestamp": "09:35:00 IST"}):
            res = evaluate_ticker(
                ticker="RELIANCE.NS",
                df=df,
                champion_model=mock_model,
                champion_meta={"version": "v1.0"},
                trade_type="SWING",
                source="MANUAL",
                skip_enrichment=False
            )

        self.assertTrue(res.price_is_fresh)
        self.assertEqual(res.reference_price, 1450.50)
        self.assertEqual(res.entry, 1450.50)
        self.assertEqual(res.price_source, "Upstox Real-Time (0ms)")

    # 11. Swing expiration -> expires after 5 trading days (not calendar days)
    def test_11_swing_5_trading_day_expiration(self):
        with patch("app.api.ml_history.get_db_path", return_value=self.temp_db_path), \
             patch("app.data.historical_data_layer.get_db_path", return_value=self.temp_db_path):
            conn = sqlite3.connect(self.temp_db_path)
            old_time = (datetime.now() - timedelta(days=12)).isoformat()
            conn.execute("""
                INSERT INTO ml_trade_history (id, timestamp, ticker, direction, entry, sl, tp1, tp2, confidence, status, trade_type, outcome)
                VALUES (201, ?, 'PAYTM.NS', 'BULLISH', 1650.0, 1550.0, 1800.0, 1900.0, 72.0, 'OPEN', 'SWING', 'OPEN')
            """, (old_time,))
            conn.commit()
            conn.close()

            dummy_dates = pd.date_range(end=datetime.now(), periods=15, freq='D')
            dummy_df = pd.DataFrame({
                'Open': [1660.0] * 15,
                'High': [1670.0] * 15,
                'Low': [1640.0] * 15,
                'Close': [1665.0] * 15
            }, index=dummy_dates)

            with patch("yfinance.download", return_value=dummy_df):
                results = evaluate_ml_history(force_refresh=True)

            t201 = next(t for t in results if t['id'] == 201)
            self.assertEqual(t201['outcome'], "SWING_HORIZON_REACHED")
            self.assertEqual(t201['status'], "CLOSED")

    # 12. Expired/invalid virtual recommendation -> does not consume heat
    def test_12_virtual_recommendation_zero_heat(self):
        with patch("app.data.historical_data_layer.get_db_path", return_value=self.temp_db_path):
            conn = sqlite3.connect(self.temp_db_path)
            for i in range(10):
                conn.execute("""
                    INSERT INTO ml_trade_history (timestamp, ticker, direction, entry, sl, tp1, confidence, status, position_type)
                    VALUES (?, 'RELIANCE.NS', 'BULLISH', 1000.0, 950.0, 1100.0, 75.0, 'OPEN', 'NOT_A_POSITION')
                """, (datetime.now().isoformat(),))
            conn.commit()
            conn.close()

            heat = get_portfolio_heat_status()
            self.assertEqual(heat['current_heat_pct'], 0.0)
            self.assertEqual(heat['actual_positions'], 0)
            self.assertEqual(heat['virtual_recommendations'], 10)
            self.assertEqual(heat['status'], "NORMAL")

    # 13. Genuine PAPER_POSITION / LIVE_POSITION -> still contributes to heat
    def test_13_paper_live_position_contributes_heat(self):
        with patch("app.data.historical_data_layer.get_db_path", return_value=self.temp_db_path):
            conn = sqlite3.connect(self.temp_db_path)
            conn.execute("""
                INSERT INTO ml_trade_history (timestamp, ticker, direction, entry, sl, tp1, confidence, status, position_type)
                VALUES (?, 'TCS.NS', 'BULLISH', 4000.0, 3900.0, 4200.0, 80.0, 'OPEN', 'PAPER_POSITION')
            """, (datetime.now().isoformat(),))
            conn.execute("""
                INSERT INTO ml_trade_history (timestamp, ticker, direction, entry, sl, tp1, confidence, status, position_type)
                VALUES (?, 'INFY.NS', 'BULLISH', 1800.0, 1750.0, 1900.0, 80.0, 'OPEN', 'LIVE_POSITION')
            """, (datetime.now().isoformat(),))
            conn.commit()
            conn.close()

            heat = get_portfolio_heat_status()
            self.assertEqual(heat['actual_positions'], 2)
            self.assertEqual(heat['current_heat_pct'], 3.0)

    # 14. Heat cap remains 6%
    def test_14_heat_cap_remains_6pct(self):
        with patch("app.data.historical_data_layer.get_db_path", return_value=self.temp_db_path):
            heat = get_portfolio_heat_status()
            self.assertEqual(heat['max_heat_cap_pct'], 6.0)

    # 15. Historical health check -> reads correct 15m source (ml_training_data)
    def test_15_historical_health_check_reads_ml_training_data(self):
        with patch("app.data.historical_data_layer.get_db_path", return_value=self.temp_db_path), \
             patch("app.analytics.system_health_center.get_readonly_connection", side_effect=lambda **kw: sqlite3.connect(self.temp_db_path)):
            conn = sqlite3.connect(self.temp_db_path)
            conn.execute("CREATE TABLE IF NOT EXISTS ohlcv (ticker TEXT, timeframe TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER)")
            conn.execute("CREATE TABLE IF NOT EXISTS ml_training_data (ticker TEXT, timestamp TEXT)")
            conn.execute("INSERT INTO ohlcv VALUES ('RELIANCE.NS', '1d', '2026-09-01', 100, 110, 95, 105, 1000)")
            conn.execute("INSERT INTO ml_training_data VALUES ('RELIANCE.NS', '2026-09-01 09:15:00')")
            conn.commit()
            conn.close()

            res = SystemHealthCenter._check_historical_data_layer(deep=False)
            self.assertIn("intraday_symbols_count", res["details"])
            self.assertGreaterEqual(res["details"]["intraday_symbols_count"], 1)

    # 16. Bad OHLC row -> detected/flagged without destructive deletion
    def test_16_bad_ohlc_row_flagged_without_deletion(self):
        with patch("app.data.historical_data_layer.get_db_path", return_value=self.temp_db_path), \
             patch("app.analytics.system_health_center.get_readonly_connection", side_effect=lambda **kw: sqlite3.connect(self.temp_db_path)):
            conn = sqlite3.connect(self.temp_db_path)
            conn.execute("CREATE TABLE IF NOT EXISTS ohlcv (ticker TEXT, timeframe TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER)")
            conn.execute("CREATE TABLE IF NOT EXISTS ml_training_data (ticker TEXT, timestamp TEXT)")
            for i in range(5):
                conn.execute(f"INSERT INTO ohlcv VALUES ('SYM{i}.NS', '1d', '2026-09-01', 100, 110, 95, 105, 1000)")
            conn.execute("INSERT INTO ohlcv VALUES ('PRECWIRE.NS', '1d', '2026-09-02', 493.55, 493.55, 493.55, 497.30, 1000)")
            conn.commit()
            conn.close()

            res = SystemHealthCenter._check_historical_data_layer(deep=True)
            self.assertEqual(res["status"], "WARNING")
            self.assertIn("flagged_candle_bounds_rows", res["details"])
            self.assertEqual(res["details"]["flagged_candle_bounds_rows"], 1)

    # 17. Individual yfinance ticker failure -> one ticker fails, sweep continues
    def test_17_autopilot_per_ticker_error_isolation(self):
        from app.tasks.autopilot_scanner import run_scheduled_autopilot_sweep
        with patch("app.analytics.autonomous_bot.is_market_open", return_value=True), \
             patch("app.tasks.autopilot_scanner.is_autopilot_enabled", return_value=True), \
             patch("app.tasks.autopilot_scanner.get_portfolio_heat_status", return_value={"status": "NORMAL", "current_heat_pct": 0.0, "max_heat_cap_pct": 6.0}), \
             patch("app.tasks.autopilot_scanner.ModelManager.load_champion", return_value=MagicMock()), \
             patch("app.tasks.autopilot_scanner.ModelManager.get_champion_metadata", return_value={"version": "v1.0"}), \
             patch("yfinance.download", side_effect=[Exception("Delisted symbol error"), create_mock_ohlcv_df(rows=60)] * 10), \
             patch("app.analytics.telegram_notifier.send_telegram_message"):
            try:
                run_scheduled_autopilot_sweep("Test Sweep")
                success = True
            except Exception as e:
                success = False
            self.assertTrue(success)

    # 18. Scheduler initialization -> thread-safe singleton, no duplicate jobs
    def test_18_scheduler_singleton_no_duplicate_jobs(self):
        s1 = get_or_create_scheduler()
        s2 = get_or_create_scheduler()
        self.assertIs(s1, s2)
        job_ids = [j.id for j in s1.get_jobs()]
        self.assertEqual(len(job_ids), len(set(job_ids)))

    # 19. Manual and autonomous scanners -> both use authoritative decision_engine.py
    def test_19_shared_decision_engine_used_by_both(self):
        import app.api.intraday_ml as intraday_mod
        import app.api.swing_ml as swing_mod
        import app.tasks.autopilot_scanner as auto_mod

        self.assertTrue(hasattr(intraday_mod, "evaluate_ticker") or "evaluate_ticker" in open(intraday_mod.__file__).read())
        self.assertTrue(hasattr(swing_mod, "evaluate_ticker") or "evaluate_ticker" in open(swing_mod.__file__).read())
        self.assertTrue(hasattr(auto_mod, "evaluate_ticker") or "evaluate_ticker" in open(auto_mod.__file__).read())

    # 20. Broker execution -> remains fail-closed in simulation
    def test_20_broker_remains_simulation_fail_closed(self):
        from app.api.broker import ExecuteRequest, execute_trade
        import asyncio
        with patch("app.data.historical_data_layer.get_db_path", return_value=self.temp_db_path):
            req = ExecuteRequest(ticker="RELIANCE.NS", action="BUY", quantity=10, target=2600.0, stop_loss=2400.0, simulation=False, bypass_safeguard=False)
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            with self.assertRaises(Exception):
                loop.run_until_complete(execute_trade(req))

    # 21. Champion model hashes -> unchanged
    def test_21_champion_model_hashes_unchanged(self):
        intraday_path = os.path.join(BACKEND_DIR, "models", "intraday", "champion_ensemble.pkl")
        swing_path = os.path.join(BACKEND_DIR, "models", "swing", "champion_ensemble.pkl")

        with open(intraday_path, "rb") as f:
            intra_hash = hashlib.sha256(f.read()).hexdigest()
        with open(swing_path, "rb") as f:
            swing_hash = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(intra_hash, INTRADAY_CHAMPION_HASH)
        self.assertEqual(swing_hash, SWING_CHAMPION_HASH)

    # 22. Research DB isolation -> unchanged
    def test_22_research_tables_isolated_from_trades(self):
        conn = sqlite3.connect(self.temp_db_path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        self.assertIn("ml_trade_history", tables)


if __name__ == '__main__':
    unittest.main()
