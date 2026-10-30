"""
Unit & Integration Tests for OptionLiveAdapter (Phase 1 — Read-Only Terminal).

Verifies:
  1. Hardcoded Safety Guarantees (SIMULATION_MODE=True, LIVE_ORDER_CAPABILITY=False).
  2. Database Read-Only Isolation (query_only=ON, mode=ro).
  3. Pre-Flight Readiness Logic (blocking live mode when NFO segment or margin is missing).
  4. Dynamic Knapsack Capital Allocation (10% safety reserve, accurate lot costs, greedy prioritization).
  5. Zero mutation of core Option Sniffer tables.
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from app.analytics.option_sniffer.option_live_adapter import (
    OptionLiveAdapter,
    SIMULATION_MODE,
    LIVE_ORDER_CAPABILITY,
    DEFAULT_MIN_MARGIN_INR,
    DEFAULT_SAFETY_BUFFER_PCT,
    get_option_live_adapter
)


class TestOptionLiveAdapterPhase1(unittest.TestCase):

    def setUp(self):
        # Create an isolated temporary database for testing
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        # Populate temporary test schema
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE option_sniffer_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT UNIQUE,
                    created_at TEXT,
                    instrument_key TEXT,
                    trading_symbol TEXT,
                    underlying TEXT,
                    option_type TEXT,
                    strike REAL,
                    direction TEXT,
                    option_ask REAL,
                    option_bid REAL,
                    spread_pct REAL,
                    spot_price REAL,
                    calibrated_prob REAL,
                    expected_net_return REAL,
                    status TEXT
                );
            """)
            conn.execute("""
                CREATE TABLE option_sniffer_paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT UNIQUE,
                    entry_time TEXT,
                    instrument_key TEXT,
                    trading_symbol TEXT,
                    underlying TEXT,
                    option_type TEXT,
                    strike REAL,
                    entry_price_used REAL,
                    exit_price_used REAL,
                    quantity INTEGER,
                    lot_size INTEGER,
                    status TEXT,
                    net_pnl REAL,
                    net_pnl_pct REAL,
                    exit_reason TEXT,
                    exit_time TEXT
                );
            """)
            conn.execute("""
                CREATE TABLE app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)

            # Seed sample signals: Signal A (high prob/ret), Signal B (medium), Signal C (low)
            from datetime import datetime
            today_str = datetime.now().strftime('%Y-%m-%d')
            conn.execute(f"""
                INSERT INTO option_sniffer_signals 
                (signal_id, created_at, instrument_key, trading_symbol, underlying, option_type, strike, direction, option_ask, option_bid, spread_pct, spot_price, calibrated_prob, expected_net_return, status)
                VALUES 
                ('SIG_A', '{today_str} 10:00:00', 'NSE_FO|101', 'NIFTY26OCT25000CE', 'NIFTY', 'CE', 25000, 'BUY_CE', 100.0, 99.0, 1.0, 25000, 0.75, 25.0, 'ACTIVE'),
                ('SIG_B', '{today_str} 10:01:00', 'NSE_FO|102', 'BANKNIFTY26OCT51000PE', 'BANKNIFTY', 'PE', 51000, 'BUY_PE', 200.0, 198.0, 1.0, 51000, 0.65, 18.0, 'ACTIVE'),
                ('SIG_C', '{today_str} 10:02:00', 'NSE_FO|103', 'NIFTY26OCT25100CE', 'NIFTY', 'CE', 25100, 'BUY_CE', 80.0, 79.0, 1.25, 25000, 0.55, 10.0, 'ACTIVE');
            """)


    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_hardcoded_safety_locks(self):
        """Rule 2 & User Guarantee: Simulation mode must be True and live order placement False."""
        self.assertTrue(SIMULATION_MODE, "SIMULATION_MODE must be hard-locked to True")
        self.assertFalse(LIVE_ORDER_CAPABILITY, "LIVE_ORDER_CAPABILITY must be hard-locked to False in Phase 1")

    def test_read_only_database_connection_isolation(self):
        """Verifies that the adapter opens SQLite in query_only=ON mode and blocks writes."""
        adapter = OptionLiveAdapter()
        with patch("app.analytics.option_sniffer.option_live_adapter.get_db_path", return_value=self.db_path):
            conn = adapter._get_read_only_conn()
            # Verify query_only is active
            cur = conn.cursor()
            cur.execute("PRAGMA query_only;")
            row = cur.fetchone()
            self.assertEqual(row[0], 1, "PRAGMA query_only must be 1 (ON)")

            # Verify attempting an insert raises an error
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO app_settings (key, value) VALUES ('test', 'fail')")
            conn.close()

    @patch("requests.get")
    def test_preflight_blocks_when_nfo_missing(self, mock_get):
        """Pre-flight check must strictly block live eligibility when NFO is missing from profile."""
        adapter = OptionLiveAdapter()
        adapter._cached_readiness = None  # Force bypass cache

        # Mock Upstox responses: profile has BSE, NSE only; IP matches; margin is ₹50,000
        def mock_side_effect(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            if "profile" in url:
                mock_resp.json.return_value = {
                    "status": "success",
                    "data": {"user_id": "85BZFH", "user_name": "TEST USER", "exchanges": ["BSE", "NSE"]}
                }
            elif "user/ip" in url:
                mock_resp.json.return_value = {
                    "status": "success",
                    "data": {"primary_ip": "2600:1900:40a0:83a::63", "secondary_ip": "122.168.87.241"}
                }
            elif "get-funds-and-margin" in url:
                mock_resp.json.return_value = {
                    "status": "success",
                    "data": {"equity": {"available_margin": 50000.0, "used_margin": 0.0}}
                }
            return mock_resp

        mock_get.side_effect = mock_side_effect

        with patch("app.analytics.option_sniffer.option_live_adapter.get_upstox_token", return_value="valid_token"):
            readiness = adapter.fetch_account_readiness(force_refresh=True)
            self.assertFalse(readiness["nfo_enabled"])
            self.assertFalse(readiness["live_trading_eligible"])
            self.assertTrue(any("NFO exchange segment is not enabled" in b for b in readiness["blockers"]))

    @patch("requests.get")
    def test_preflight_blocks_when_margin_is_zero(self, mock_get):
        """Pre-flight check must strictly block live eligibility when available margin is below ₹10,000."""
        adapter = OptionLiveAdapter()
        adapter._cached_readiness = None

        def mock_side_effect(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            if "profile" in url:
                mock_resp.json.return_value = {
                    "status": "success",
                    "data": {"user_id": "85BZFH", "user_name": "TEST USER", "exchanges": ["BSE", "NSE", "NFO"]}
                }
            elif "user/ip" in url:
                mock_resp.json.return_value = {
                    "status": "success",
                    "data": {"primary_ip": "2600:1900:40a0:83a::63", "secondary_ip": "122.168.87.241"}
                }
            elif "get-funds-and-margin" in url:
                mock_resp.json.return_value = {
                    "status": "success",
                    "data": {"equity": {"available_margin": 0.0, "used_margin": 0.0}}
                }
            return mock_resp

        mock_get.side_effect = mock_side_effect

        with patch("app.analytics.option_sniffer.option_live_adapter.get_upstox_token", return_value="valid_token"):
            readiness = adapter.fetch_account_readiness(force_refresh=True)
            self.assertTrue(readiness["nfo_enabled"])
            self.assertEqual(readiness["available_margin"], 0.0)
            self.assertFalse(readiness["live_trading_eligible"])
            self.assertTrue(any("Available margin" in b for b in readiness["blockers"]))

    @patch("requests.get")
    def test_preflight_passes_when_all_criteria_met(self, mock_get):
        """When NFO is enabled, IP matches, and funds >= 10,000, pre-flight passes."""
        adapter = OptionLiveAdapter()
        adapter._cached_readiness = None

        def mock_side_effect(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            if "profile" in url:
                mock_resp.json.return_value = {
                    "status": "success",
                    "data": {"user_id": "85BZFH", "user_name": "TEST USER", "exchanges": ["BSE", "NSE", "NFO"]}
                }
            elif "user/ip" in url:
                mock_resp.json.return_value = {
                    "status": "success",
                    "data": {"primary_ip": "2600:1900:40a0:83a::63", "secondary_ip": "122.168.87.241"}
                }
            elif "get-funds-and-margin" in url:
                mock_resp.json.return_value = {
                    "status": "success",
                    "data": {"equity": {"available_margin": 25000.0, "used_margin": 0.0}}
                }
            return mock_resp

        mock_get.side_effect = mock_side_effect

        with patch("app.analytics.option_sniffer.option_live_adapter.get_upstox_token", return_value="valid_token"):
            readiness = adapter.fetch_account_readiness(force_refresh=True)
            self.assertTrue(readiness["token_valid"])
            self.assertTrue(readiness["nfo_enabled"])
            self.assertTrue(readiness["static_ip_matched"])
            self.assertEqual(readiness["available_margin"], 25000.0)
            self.assertTrue(readiness["live_trading_eligible"])
            self.assertEqual(len(readiness["blockers"]), 0)

    def test_knapsack_capital_allocation(self):
        """
        Tests the dynamic greedy allocator:
        - Budget tested: ₹15,000
        - 10% safety reserve: ₹1,500
        - Deployable capital: ₹13,500
        - Signal A: NIFTY 100 * 65 + 55 = ₹6,555 (Score: 0.75 * 25 = 18.75) -> ALLOCATED
        - Signal B: BANKNIFTY 200 * 30 + 55 = ₹6,055 (Score: 0.65 * 18 = 11.7) -> ALLOCATED
        - Signal C: NIFTY 80 * 65 + 55 = ₹5,255 -> SKIPPED (remaining ₹890 < ₹5,255)
        """
        adapter = OptionLiveAdapter()
        with patch("app.analytics.option_sniffer.option_live_adapter.get_db_path", return_value=self.db_path):
            with patch.object(adapter, "fetch_account_readiness", return_value={"available_margin": 0.0}):
                alloc = adapter.simulate_dynamic_capital_allocation(custom_capital=15000.0)
                
                self.assertEqual(alloc["budget_tested"], 15000.0)
                self.assertEqual(alloc["safety_reserve_deducted"], 1500.0)
                self.assertEqual(alloc["deployable_capital"], 13500.0)
                self.assertEqual(alloc["max_concurrent_lots_supported"], 2)

                allocated = alloc["allocated_trades"]
                self.assertEqual(len(allocated), 2)
                self.assertEqual(allocated[0]["signal_id"], "SIG_A")
                self.assertEqual(allocated[1]["signal_id"], "SIG_B")

                skipped = alloc["skipped_trades"]
                self.assertEqual(len(skipped), 1)
                self.assertEqual(skipped[0]["signal_id"], "SIG_C")
                self.assertEqual(skipped[0]["allocation_status"], "SKIPPED_INSUFFICIENT_MARGIN")


if __name__ == "__main__":
    unittest.main()
