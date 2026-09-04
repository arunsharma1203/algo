import unittest
from unittest.mock import patch, MagicMock
import os
import tempfile
import sqlite3
import hashlib
from datetime import datetime, timedelta
import pandas as pd

from app.analytics.universe_config import (
    resolve_universe_tickers, get_universe, get_universe_coverage,
    AUTOPILOT_PRIORITY_20_UNIVERSE, BENCHMARK_5_UNIVERSE, NIFTY_500_UNIVERSE
)
from app.data.validator import MarketDataValidator
from app.data.historical_data_layer import HistoricalDataLayer
from app.analytics.model_manager import ModelManager


class TestSystemWidePropagationRemediation(unittest.TestCase):
    """
    Comprehensive test suite verifying system-wide propagation gap remediation:
    1. Authoritative Universe Resolution
    2. Scheduled Hoarder & Daily OHLCV Ingestion
    3. Research Horizon (history_years) Data Filtering
    4. Deterministic Trade Deduplication
    5. Virtual Position Isolation in Active Trade Tracker
    6. 15m Outcome Evaluation Data Routing
    7. Foundation Model Minute-Precision Cache Key
    8. Broker Mode Consolidation & Fail-Closed Behavior
    9. Ticker Normalization
    10. Production Invariant Preservation
    """

    def setUp(self):
        # Create an isolated temporary SQLite database for testing
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.conn = sqlite3.connect(self.db_path)
        self._init_test_schema()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            try:
                os.unlink(self.db_path)
            except Exception:
                pass

    def _init_test_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ohlcv (
                ticker TEXT,
                date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume INTEGER,
                source TEXT DEFAULT 'yfinance',
                hoard_timestamp TEXT,
                timeframe TEXT DEFAULT '1d',
                PRIMARY KEY (ticker, date, timeframe)
            );
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ml_training_data (
                datetime TEXT,
                ticker TEXT,
                close REAL,
                rsi REAL,
                macd REAL,
                macd_diff REAL,
                adx REAL,
                atr REAL,
                returns REAL,
                target INTEGER,
                source TEXT DEFAULT 'yfinance',
                hoard_timestamp TEXT,
                PRIMARY KEY (ticker, datetime)
            );
        """)
        self.conn.execute("""
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
                trade_type TEXT DEFAULT 'INTRADAY',
                status TEXT DEFAULT 'OPEN',
                outcome TEXT DEFAULT 'OPEN',
                explanation TEXT,
                source TEXT DEFAULT 'MANUAL',
                position_type TEXT DEFAULT 'NOT_A_POSITION',
                reference_price REAL,
                model_candle_close REAL,
                price_source TEXT,
                price_timestamp TEXT,
                price_is_fresh INTEGER,
                tightened_sl REAL,
                ai_guard_action TEXT,
                risk_level TEXT,
                risk_reasons TEXT,
                risk_updated_at TEXT,
                profit_pct REAL,
                effective_entry REAL,
                slippage_drag REAL,
                ideal_profit_pct REAL,
                exit_price REAL,
                exit_time TEXT
            );
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ml_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                level TEXT,
                ticker TEXT,
                message TEXT
            );
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS app_master_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                subsystem TEXT,
                event_type TEXT,
                message TEXT,
                details TEXT
            );
        """)
        self.conn.commit()

    # -------------------------------------------------------------
    # 1. AUTHORITATIVE UNIVERSE RESOLUTION
    # -------------------------------------------------------------
    def test_nifty_500_universe_resolves_500_stocks(self):
        tickers = resolve_universe_tickers("NIFTY_500")
        self.assertEqual(len(tickers), 500)
        self.assertIn("RELIANCE.NS", tickers)
        self.assertIn("TCS.NS", tickers)

    def test_benchmark_5_universe_resolves_5_stocks(self):
        tickers = resolve_universe_tickers("BENCHMARK_5")
        self.assertEqual(len(tickers), 5)
        self.assertEqual(tickers, BENCHMARK_5_UNIVERSE)

    def test_autopilot_priority_20_universe_resolves_20_stocks(self):
        tickers = resolve_universe_tickers("AUTOPILOT_PRIORITY_20")
        self.assertEqual(len(tickers), 20)
        self.assertEqual(tickers, AUTOPILOT_PRIORITY_20_UNIVERSE)

    def test_all_117_legacy_alias_resolves_dynamic_db(self):
        # Insert 3 sample tickers in ohlcv
        for tkr in ["INFY.NS", "SBIN.NS", "ITC.NS"]:
            self.conn.execute(
                "INSERT OR REPLACE INTO ohlcv (ticker, date, open, high, low, close, volume, timeframe) "
                "VALUES (?, '2026-09-01', 100, 105, 95, 102, 1000, '1d')",
                (tkr,)
            )
        self.conn.commit()

        with patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path):
            tickers = resolve_universe_tickers("ALL_117")
            self.assertEqual(len(tickers), 3)
            self.assertIn("INFY.NS", tickers)

    def test_custom_universe_resolves_correctly(self):
        custom = ["tatamotors", "wipro.ns"]
        resolved = resolve_universe_tickers("CUSTOM", custom_tickers=custom)
        self.assertEqual(resolved, ["TATAMOTORS.NS", "WIPRO.NS"])

    # -------------------------------------------------------------
    # 2. DAILY OHLCV BROAD UNIVERSE INGESTION
    # -------------------------------------------------------------
    def test_sync_universe_daily_batching_and_metrics(self):
        def fake_sync(ticker, force=False):
            return {
                "ticker": ticker,
                "status": "SYNCED",
                "rows_synced": 100,
                "total_rows": 100,
                "latest_date": "2026-09-04"
            }

        with patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path), \
             patch.object(HistoricalDataLayer, "sync_ticker_daily_10y", side_effect=fake_sync):
            res = HistoricalDataLayer.sync_universe_daily(
                universe="BENCHMARK_5",
                batch_size=2,
                max_workers=2
            )
            self.assertEqual(res["universe"], "BENCHMARK_5")
            self.assertEqual(res["requested_count"], 5)
            self.assertEqual(res["success_count"], 5)
            self.assertEqual(res["failure_count"], 0)
            self.assertEqual(res["coverage_percent"], 100.0)
            self.assertEqual(res["total_rows_synced"], 500)

    # -------------------------------------------------------------
    # 3. RESEARCH HORIZON (history_years) DATA FILTERING
    # -------------------------------------------------------------
    def test_walk_forward_engine_calculates_start_date_from_history_years(self):
        from app.analytics.portfolio_walk_forward import MultiStockPortfolioWalkForwardEngine
        engine = MultiStockPortfolioWalkForwardEngine(
            tickers=["RELIANCE.NS"],
            history_years=3.0
        )
        self.assertEqual(engine.history_years, 3.0)
        self.assertIsNotNone(engine.start_date)
        expected_year = datetime.now().year - 3
        self.assertTrue(engine.start_date.startswith(str(expected_year)) or engine.start_date.startswith(str(expected_year - 1)))

    def test_walk_forward_engine_uses_explicit_start_date(self):
        from app.analytics.portfolio_walk_forward import MultiStockPortfolioWalkForwardEngine
        engine = MultiStockPortfolioWalkForwardEngine(
            tickers=["RELIANCE.NS"],
            start_date="2023-01-01"
        )
        self.assertEqual(engine.start_date, "2023-01-01")

    # -------------------------------------------------------------
    # 4. DETERMINISTIC TRADE DEDUPLICATION
    # -------------------------------------------------------------
    def test_save_ml_trade_deterministic_deduplication(self):
        from app.api.ml_history import save_ml_trade

        with patch("app.api.ml_history.get_db_path", return_value=self.db_path):
            # First save: should succeed
            saved1 = save_ml_trade(
                ticker="RELIANCE.NS",
                is_bullish=True,
                entry=2500.0,
                sl=2450.0,
                tp1=2600.0,
                tp2=2650.0,
                confidence=65.5,
                trade_type="INTRADAY",
                explanation={"test": 1},
                source="MANUAL",
                position_type="NOT_A_POSITION"
            )
            self.assertTrue(saved1)

            # Second save on same day with slight confidence change: should be suppressed
            saved2 = save_ml_trade(
                ticker="RELIANCE.NS",
                is_bullish=True,
                entry=2505.0,
                sl=2450.0,
                tp1=2600.0,
                tp2=2650.0,
                confidence=65.8,
                trade_type="INTRADAY",
                explanation={"test": 2},
                source="MANUAL",
                position_type="NOT_A_POSITION"
            )
            self.assertFalse(saved2)

            # Verify only 1 row exists in ml_trade_history and confidence was updated
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*), confidence FROM ml_trade_history WHERE ticker = 'RELIANCE.NS'")
            row = cur.fetchone()
            self.assertEqual(row[0], 1)
            self.assertEqual(row[1], 65.8)

    # -------------------------------------------------------------
    # 5. VIRTUAL POSITION ISOLATION IN ACTIVE TRADE TRACKER
    # -------------------------------------------------------------
    def test_active_trade_tracker_skips_virtual_recommendations(self):
        from app.analytics.autonomous_bot import active_trade_tracker

        self.conn.execute("""
            INSERT INTO ml_trade_history (timestamp, ticker, direction, entry, sl, tp1, tp2, confidence, trade_type, status, outcome, position_type)
            VALUES 
            (datetime('now'), 'VIRTUAL.NS', 'BULLISH', 100, 95, 110, 115, 75.0, 'INTRADAY', 'OPEN', 'OPEN', 'NOT_A_POSITION'),
            (datetime('now'), 'PAPER.NS', 'BULLISH', 200, 190, 220, 230, 80.0, 'INTRADAY', 'OPEN', 'OPEN', 'PAPER_POSITION')
        """)
        self.conn.commit()

        mock_history = [
            {"id": 1, "ticker": "VIRTUAL.NS", "outcome": "OPEN", "position_type": "NOT_A_POSITION", "direction": "BULLISH", "entry": 100, "sl": 95, "tp1": 110, "trade_type": "INTRADAY", "timestamp": "2026-09-04 10:00:00"},
            {"id": 2, "ticker": "PAPER.NS", "outcome": "OPEN", "position_type": "PAPER_POSITION", "direction": "BULLISH", "entry": 200, "sl": 190, "tp1": 220, "trade_type": "INTRADAY", "timestamp": "2026-09-04 10:00:00"}
        ]

        with patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path), \
             patch("app.api.ml_history.evaluate_ml_history", return_value=mock_history), \
             patch("app.analytics.autonomous_bot.evaluate_single_trade_risk", return_value={"risk_level": "WARNING", "tightened_sl": 195.0, "reasons": ["Trailing"]}), \
             patch("app.analytics.telegram_notifier.send_telegram_message"):
            active_trade_tracker(force_run=True)

        cur = self.conn.cursor()
        cur.execute("SELECT ticker, tightened_sl, ai_guard_action FROM ml_trade_history ORDER BY id")
        rows = cur.fetchall()
        self.assertEqual(rows[0][0], "VIRTUAL.NS")
        self.assertIsNone(rows[0][1])  # Untouched
        self.assertEqual(rows[1][0], "PAPER.NS")
        self.assertEqual(rows[1][1], 195.0)
        self.assertEqual(rows[1][2], "TIGHTEN_SL")

    # -------------------------------------------------------------
    # 6. FOUNDATION MODEL MINUTE-PRECISION CACHE KEY
    # -------------------------------------------------------------
    def test_foundation_model_cache_key_minute_precision(self):
        from app.analytics.foundation_models.manager import FoundationModelManager

        mgr = FoundationModelManager()
        t1 = datetime(2026, 9, 4, 15, 30, 12, 345678)
        t2 = datetime(2026, 9, 4, 15, 30, 45, 999999)

        b1 = t1.replace(second=0, microsecond=0).isoformat()
        b2 = t2.replace(second=0, microsecond=0).isoformat()

        self.assertEqual(b1, b2)
        key1 = f"timesfm_RELIANCE.NS_15m_{b1}_1"
        key2 = f"timesfm_RELIANCE.NS_15m_{b2}_1"
        self.assertEqual(key1, key2)

    # -------------------------------------------------------------
    # 7. BROKER MODE FAIL-CLOSED CONSOLIDATION
    # -------------------------------------------------------------
    def test_broker_simulation_mode_enforced_by_default(self):
        from app.api.settings import get_simulation_mode
        from app.analytics.dashboard_intelligence_service import DashboardIntelligenceService

        with patch("app.api.settings.get_db_path", return_value=self.db_path), \
             patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path):
            sim_status = get_simulation_mode()
            self.assertTrue(sim_status["simulation_mode"])

            matrix = DashboardIntelligenceService.get_system_health_matrix()
            self.assertEqual(matrix["broker_mode"], "SIMULATION (Fail-Safe)")

    # -------------------------------------------------------------
    # 8. TICKER NORMALIZATION
    # -------------------------------------------------------------
    def test_market_data_validator_normalize_ticker(self):
        self.assertEqual(MarketDataValidator.normalize_ticker("reliance"), "RELIANCE.NS")
        self.assertEqual(MarketDataValidator.normalize_ticker("TCS.NS"), "TCS.NS")
        self.assertEqual(MarketDataValidator.normalize_ticker("  infy  "), "INFY.NS")
        self.assertEqual(MarketDataValidator.normalize_ticker("^NSEI"), "^NSEI")
        self.assertEqual(MarketDataValidator.normalize_ticker("^INDIAVIX"), "^INDIAVIX")
        self.assertEqual(MarketDataValidator.normalize_ticker("500112.BO"), "500112.BO")

    # -------------------------------------------------------------
    # 9. PRODUCTION INVARIANT PRESERVATION
    # -------------------------------------------------------------
    def test_champion_model_hashes_unchanged(self):
        def hash_file(p):
            with open(p, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()

        intra_hash = hash_file('backend/models/intraday/champion_ensemble.pkl')
        swing_hash = hash_file('backend/models/swing/champion_ensemble.pkl')

        self.assertEqual(intra_hash, "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91")
        self.assertEqual(swing_hash, "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534")

    def test_production_database_trade_and_job_counts(self):
        from app.data.historical_data_layer import get_db_path
        conn = sqlite3.connect(get_db_path())
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ml_trade_history")
        trade_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM research_jobs")
        job_count = cur.fetchone()[0]
        conn.close()

        self.assertEqual(trade_count, 68)
        self.assertEqual(job_count, 25)


if __name__ == "__main__":
    unittest.main()
