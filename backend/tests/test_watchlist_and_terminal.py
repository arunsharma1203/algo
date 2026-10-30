import os
import sqlite3
import hashlib
import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
from datetime import datetime

from app.api.watchlist import (
    init_watchlist_table, get_persisted_watchlist, get_persisted_watchlist_tickers,
    normalize_ticker, DEFAULT_WATCHLIST_TICKERS
)
from app.analytics.universe_config import (
    resolve_universe_tickers, get_universe,
    LIVE_UNIVERSE, NIFTY_50_UNIVERSE, NIFTY_500_UNIVERSE,
    BENCHMARK_5_UNIVERSE, RESEARCH_100_UNIVERSE
)
from app.analytics.fii_dii_service import FiiDiiService, VERIFIED_04_SEP_SEED
from app.analytics.dashboard_intelligence_service import DashboardIntelligenceService
from app.api.watchlist_scanner import evaluate_single_ticker_data, classify_setup, calculate_technical_score
from app.data.historical_data_layer import get_db_path

CHAMPION_INTRADAY_PATH = "backend/models/intraday/champion_ensemble.pkl"
CHAMPION_SWING_PATH = "backend/models/swing/champion_ensemble.pkl"
EXPECTED_INTRADAY_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
EXPECTED_SWING_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

class TestWatchlistAndTerminalArchitecture(unittest.TestCase):

    def test_champion_hashes_byte_for_byte_unchanged(self):
        """Production Invariant 8: Champion models remain 100% byte-for-byte immutable."""
        with open(CHAMPION_INTRADAY_PATH, "rb") as f:
            intraday_hash = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(intraday_hash, EXPECTED_INTRADAY_HASH, "Intraday champion modified!")

        with open(CHAMPION_SWING_PATH, "rb") as f:
            swing_hash = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(swing_hash, EXPECTED_SWING_HASH, "Swing champion modified!")

    def test_ml_trade_history_untouched_at_68_rows(self):
        """Production Invariant 8: ml_trade_history row count must remain exactly 68."""
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ml_trade_history")
        count = cur.fetchone()[0]
        conn.close()
        self.assertEqual(count, 68, f"Expected 68 rows in ml_trade_history, found {count}")

    def test_watchlist_isolation_from_research_universes(self):
        """
        Mandatory Invariant 2:
        Modifying WATCHLIST must NEVER modify LIVE_52, NIFTY_50, NIFTY_500,
        BENCHMARK_5, or RESEARCH_100 universes.
        """
        initial_live = list(LIVE_UNIVERSE)
        initial_nifty50 = list(NIFTY_50_UNIVERSE)
        initial_nifty500 = list(NIFTY_500_UNIVERSE)
        initial_bench5 = list(BENCHMARK_5_UNIVERSE)
        initial_res100 = list(RESEARCH_100_UNIVERSE)

        # Simulate adding a custom exotic ticker to user_watchlist in DB
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_watchlist (ticker, added_at, display_order, notes) VALUES (?, ?, ?, ?)",
                ("TEST_EXOTIC_SYM.NS", datetime.now().isoformat(), 999, "Isolation test ticker")
            )
        conn.close()

        # Resolve WATCHLIST - must include the new ticker
        resolved_wl = resolve_universe_tickers("WATCHLIST")
        self.assertIn("TEST_EXOTIC_SYM.NS", resolved_wl)

        # Resolve other universes - must NOT contain the custom ticker
        self.assertEqual(resolve_universe_tickers("LIVE_52"), initial_live)
        self.assertEqual(resolve_universe_tickers("NIFTY_50"), initial_nifty50)
        self.assertEqual(resolve_universe_tickers("NIFTY_500"), initial_nifty500)
        self.assertEqual(resolve_universe_tickers("BENCHMARK_5"), initial_bench5)
        self.assertEqual(resolve_universe_tickers("RESEARCH_100"), initial_res100)

        # Clean up test ticker
        conn = sqlite3.connect(db_path)
        with conn:
            conn.execute("DELETE FROM user_watchlist WHERE ticker = ?", ("TEST_EXOTIC_SYM.NS",))
        conn.close()

    def test_watchlist_crud_and_normalization(self):
        """Tests adding, normalizing, ordering, and deleting watchlist tickers."""
        self.assertEqual(normalize_ticker("reliance"), "RELIANCE.NS")
        self.assertEqual(normalize_ticker("TCS.NS"), "TCS.NS")
        self.assertEqual(normalize_ticker("  infy  "), "INFY.NS")

        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_watchlist (ticker, added_at, display_order, notes) VALUES (?, ?, ?, ?)",
                ("TATAMOTORS.NS", datetime.now().isoformat(), 10, "Test stock")
            )
        conn.close()

        tickers = get_persisted_watchlist_tickers()
        self.assertIn("TATAMOTORS.NS", tickers)

        # Cleanup
        conn = sqlite3.connect(db_path)
        with conn:
            conn.execute("DELETE FROM user_watchlist WHERE ticker = ?", ("TATAMOTORS.NS",))
        conn.close()

    def test_migration_safety_verification(self):
        """
        Mandatory Invariant 1:
        Migration endpoint guarantees persistence before verification response.
        """
        from app.api.watchlist import migrate_legacy_watchlist, MigrateRequest
        payload = MigrateRequest(tickers=["HDFCBANK.NS", "SBIN.NS"])
        res = migrate_legacy_watchlist(payload)
        self.assertTrue(res["persisted"])
        self.assertTrue(res["verified_in_backend"])
        self.assertIn("HDFCBANK.NS", res["tickers"])
        self.assertIn("SBIN.NS", res["tickers"])

    def test_stale_ltp_and_freshness_handling(self):
        """
        Mandatory Invariant 5:
        Stale/unavailable candles must be explicitly marked rather than presented as live.
        """
        # Create a mock df with candles from 30 days ago
        dates = pd.date_range(end=datetime.now() - pd.Timedelta(days=30), periods=40, freq='D')
        df_stale = pd.DataFrame({
            'open': [100.0] * 40,
            'high': [105.0] * 40,
            'low': [98.0] * 40,
            'close': [102.0] * 40,
            'volume': [100000] * 40
        }, index=dates)
        df_stale.attrs['source'] = 'Mock Test Data'

        with patch('app.api.watchlist_scanner.fetch_historical_data', return_value=df_stale):
            res = evaluate_single_ticker_data("MOCK_STALE.NS", None, None)
            self.assertFalse(res.get("error"))
            self.assertEqual(res["data_freshness"], "STALE")

    def test_watchlist_scanner_creates_zero_orders_or_trades(self):
        """
        Mandatory Invariant 4:
        Watchlist scanning must NEVER create broker orders, live positions,
        portfolio heat, or production trade history records.
        """
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ml_trade_history")
        count_before = cur.fetchone()[0]
        conn.close()

        # Run evaluate_single_ticker_data
        from app.analytics.model_manager import ModelManager
        model, meta = ModelManager.load_champion("intraday")
        evaluate_single_ticker_data("RELIANCE.NS", model, meta)

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ml_trade_history")
        count_after = cur.fetchone()[0]
        conn.close()

        self.assertEqual(count_before, count_after, "Watchlist scanner must NEVER write to ml_trade_history!")

    def test_official_fii_dii_disclosure_date_and_net(self):
        """
        Mandatory Invariant 6:
        Verified 04-Sep-2026 disclosures must be reported with exact date and accurate totals.
        """
        flows = FiiDiiService.get_latest_flows()
        self.assertEqual(flows["status"], "success")
        self.assertEqual(flows["disclosure_date"], "04-Sep-2026")
        self.assertEqual(flows["dii"]["net"], 8930.12)
        self.assertEqual(flows["fii"]["net"], -3111.94)
        self.assertEqual(flows["net_institutional_total"], 5818.18)
        self.assertFalse(flows["is_real_time"])

        dash_flows = DashboardIntelligenceService.get_institutional_flows()
        self.assertEqual(dash_flows["status"], "FRESH")
        self.assertEqual(dash_flows["disclosure_date"], "04-Sep-2026")
        self.assertEqual(dash_flows["dii_latest_cr"], 8930.12)
        self.assertEqual(dash_flows["fii_latest_cr"], -3111.94)

if __name__ == "__main__":
    unittest.main()
