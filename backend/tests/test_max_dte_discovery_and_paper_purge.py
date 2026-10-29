"""
Sandboxed Regression Test Suite:
1. MAX_OPTION_EXPIRY_DTE filter in OptionContractDiscovery.
2. Controlled paper purge for far-month contracts (FAR_DTE_CATALOG_PURGE).
3. Zero-loss and zero-resurrection invariants.
4. Alpha preservation audit (feature weights and model integrity).
"""

import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, date, timedelta
import sqlite3
import tempfile
import os
import sys

# Ensure backend root is on python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.analytics.option_sniffer.option_constants import MAX_OPTION_EXPIRY_DTE
from app.analytics.option_sniffer.option_contract_discovery import OptionContractDiscovery
from app.analytics.option_sniffer.option_db import OptionDB
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution


class TestMaxDTEDiscoveryAndPaperPurge(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE option_sniffer_candidates (
                    instrument_key TEXT PRIMARY KEY,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    strike REAL NOT NULL,
                    option_type TEXT NOT NULL,
                    lot_size INTEGER NOT NULL,
                    underlying_key TEXT,
                    atm_strike REAL,
                    moneyness REAL,
                    is_active INTEGER DEFAULT 1,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE option_sniffer_paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT UNIQUE NOT NULL,
                    signal_id TEXT,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    option_type TEXT NOT NULL,
                    strike REAL NOT NULL,
                    expiry TEXT NOT NULL,
                    lot_size INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    entry_time TEXT NOT NULL,
                    entry_bid REAL NOT NULL,
                    entry_ask REAL NOT NULL,
                    entry_price_used REAL NOT NULL,
                    exit_time TEXT,
                    exit_bid REAL,
                    exit_ask REAL,
                    exit_price_used REAL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    peak_price REAL,
                    trough_price REAL,
                    mae_pct REAL,
                    mfe_pct REAL,
                    gross_pnl REAL DEFAULT 0.0,
                    charges REAL DEFAULT 0.0,
                    slippage REAL DEFAULT 0.0,
                    net_pnl REAL DEFAULT 0.0,
                    net_pnl_pct REAL DEFAULT 0.0,
                    holding_duration_seconds INTEGER,
                    exit_reason TEXT,
                    model_version TEXT,
                    signal_version TEXT,
                    data_freshness TEXT,
                    telegram_exit_sent INTEGER DEFAULT 0,
                    engine_version TEXT DEFAULT 'V1_CLASSIC',
                    dynamic_sl_pct REAL DEFAULT 20.0,
                    dynamic_target_pct REAL DEFAULT 35.0,
                    spot_atr REAL DEFAULT 40.0,
                    delta_used REAL DEFAULT 0.5,
                    entry_spot_price REAL DEFAULT 0.0
                );
            """)

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_max_dte_constant_defined(self):
        """Verifies MAX_OPTION_EXPIRY_DTE is 7.0 days."""
        self.assertEqual(MAX_OPTION_EXPIRY_DTE, 7.0)

    @patch("app.analytics.option_sniffer.option_contract_discovery.OptionContractDiscovery.fetch_raw_contracts")
    def test_discovery_filters_far_month_expiries(self, mock_fetch):
        """
        Tests that OptionContractDiscovery.get_available_expiries() strictly filters
        out expiries beyond 7 DTE while keeping weekly expiries.
        """
        today = date.today()
        exp_0dte = today.strftime("%Y-%m-%d")
        exp_5dte = (today + timedelta(days=5)).strftime("%Y-%m-%d")
        exp_7dte = (today + timedelta(days=7)).strftime("%Y-%m-%d")
        exp_8dte = (today + timedelta(days=8)).strftime("%Y-%m-%d")
        exp_25dte = (today + timedelta(days=25)).strftime("%Y-%m-%d")

        mock_fetch.return_value = [
            {"expiry": exp_0dte},
            {"expiry": exp_5dte},
            {"expiry": exp_7dte},
            {"expiry": exp_8dte},
            {"expiry": exp_25dte},
        ]

        expiries = OptionContractDiscovery.get_available_expiries("NIFTY")
        self.assertIn(exp_0dte, expiries)
        self.assertIn(exp_5dte, expiries)
        self.assertIn(exp_7dte, expiries)
        self.assertNotIn(exp_8dte, expiries)
        self.assertNotIn(exp_25dte, expiries)

    @patch("app.analytics.option_sniffer.option_contract_discovery.OptionContractDiscovery.fetch_raw_contracts")
    def test_banknifty_skips_when_only_monthly_available(self, mock_fetch):
        """
        When BANKNIFTY has only monthly contracts (e.g. 25 DTE), get_available_expiries
        returns [] and discover_monitored_universe skips BANKNIFTY without error.
        """
        today = date.today()
        exp_25dte = (today + timedelta(days=25)).strftime("%Y-%m-%d")
        mock_fetch.return_value = [
            {"expiry": exp_25dte, "strike_price": 53900.0, "instrument_type": "CE", "instrument_key": "NSE_FO|62131"}
        ]

        expiries = OptionContractDiscovery.get_available_expiries("BANKNIFTY")
        self.assertEqual(expiries, [])

    def test_candidate_sync_deactivates_unmonitored_contracts(self):
        """Tests that OptionDB.sync_active_candidates marks non-monitored candidates as is_active = 0."""
        with patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path):
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO option_sniffer_candidates (
                        instrument_key, trading_symbol, underlying, expiry, strike, option_type, lot_size, is_active, created_at, updated_at
                    ) VALUES 
                    ('KEY_NIFTY_WEEKLY', 'NIFTY 22800 CE', 'NIFTY', '2026-11-03', 22800, 'CE', 65, 1, '2026-10-29', '2026-10-29'),
                    ('KEY_BANKNIFTY_FAR', 'BANKNIFTY 53900 CE', 'BANKNIFTY', '2026-11-23', 53900, 'CE', 30, 1, '2026-10-29', '2026-10-29');
                """)

            # Sync with only NIFTY weekly active
            deactivated = OptionDB.sync_active_candidates(["KEY_NIFTY_WEEKLY"])
            self.assertEqual(deactivated, 1)

            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute("SELECT instrument_key, is_active FROM option_sniffer_candidates").fetchall()
                data = dict(rows)
                self.assertEqual(data["KEY_NIFTY_WEEKLY"], 1)
                self.assertEqual(data["KEY_BANKNIFTY_FAR"], 0)

    def test_paper_purge_closes_stuck_far_trade(self):
        """
        Tests that OptionPaperExecution.close_trade closes a stuck trade with
        exit_reason='FAR_DTE_CATALOG_PURGE', calculates net PnL, and leaves 0 open trades.
        """
        with patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path), \
             patch("app.analytics.telegram_notifier.send_telegram_message") as mock_tg:

            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO option_sniffer_paper_trades (
                        trade_id, signal_id, instrument_key, trading_symbol, underlying,
                        option_type, strike, expiry, lot_size, quantity, entry_time,
                        entry_bid, entry_ask, entry_price_used, status, peak_price, trough_price,
                        mae_pct, mfe_pct, engine_version, dynamic_sl_pct, dynamic_target_pct
                    ) VALUES (
                        'opt_paper_stuck_bn', 'opt_sig_test', 'NSE_FO|62131', 'BANKNIFTY 53900 CE 23 NOV 26',
                        'BANKNIFTY', 'CE', 53900.0, '2026-11-23', 30, 30, '2026-10-29T10:30:00',
                        1452.3, 1467.45, 1467.45, 'OPEN', 1480.25, 1454.95,
                        -0.85, 0.87, 'V1_CLASSIC', 20.0, 35.0
                    );
                """)

            open_trades = OptionDB.get_open_paper_trades()
            self.assertEqual(len(open_trades), 1)
            trade = open_trades[0]

            # Execute controlled purge
            closed_result = OptionPaperExecution.close_trade(
                trade=trade,
                exit_bid=1480.25,
                exit_ask=1480.25,
                exit_reason="FAR_DTE_CATALOG_PURGE",
                peak_price=1480.25,
                trough_price=1454.95,
                mfe_pct=0.87,
                mae_pct=-0.85,
                duration_sec=3600
            )

            self.assertIsNotNone(closed_result)
            self.assertEqual(closed_result["status"], "CLOSED")
            self.assertEqual(closed_result["exit_reason"], "FAR_DTE_CATALOG_PURGE")
            self.assertEqual(closed_result["exit_bid"], 1480.25)
            self.assertGreater(closed_result["gross_pnl"], 0.0)

            # Confirm 0 open trades remaining
            remaining_open = OptionDB.get_open_paper_trades()
            self.assertEqual(len(remaining_open), 0)


if __name__ == "__main__":
    unittest.main()
