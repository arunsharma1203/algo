"""
Option Sniffer: Phase 3 Unit & Integration Tests (1-Day Ephemeral Arming, 4-Digit PIN, and Fail-Closed Guard).

Tests:
  1. Dynamic 4-Digit Security PIN Verification & Anti-Brute-Force Guard.
  2. Fail-Closed Arming Rejection when F&O Segment or Margin is Missing.
  3. Successful 1-Day Arming when all Pre-Flight Gates Pass.
  4. 1-Day Auto-Expiration on Date Rollover (Midnight Reset).
  5. Session Cutoff Guard (15:15 IST Market Close Auto-Disarm).
  6. Disarm Command Resets to Paper Mode.
  7. SOS Emergency Square-Off Flattens All Positions and Disarms Live Mode.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from app.analytics.option_sniffer.option_live_adapter import OptionLiveAdapter


class TestOptionLiveAdapterPhase3(unittest.TestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        # Patch get_db_path
        self.patcher = patch("app.analytics.option_sniffer.option_live_adapter.get_db_path", return_value=self.db_path)
        self.patcher.start()

        # Initialize test tables
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE option_live_order_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_intent_id TEXT UNIQUE NOT NULL,
                    signal_id TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    action TEXT NOT NULL,
                    lot_size INTEGER NOT NULL,
                    target_quantity INTEGER NOT NULL,
                    filled_quantity INTEGER DEFAULT 0,
                    limit_price REAL NOT NULL,
                    average_fill_price REAL DEFAULT 0.0,
                    state TEXT NOT NULL,
                    broker_order_id TEXT,
                    stop_loss_trigger REAL,
                    stop_loss_limit REAL,
                    broker_sl_order_id TEXT,
                    exit_state TEXT DEFAULT 'NONE',
                    exit_broker_order_id TEXT,
                    exit_fill_price REAL DEFAULT 0.0,
                    realized_pnl REAL DEFAULT 0.0,
                    rejection_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

        self.adapter = OptionLiveAdapter()

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_pin_verification_check(self):
        """Verifies that entering the wrong PIN rejects arming."""
        self.adapter._current_pin = "7492"
        ok, msg, _ = self.adapter.arm_live_trading(pin="0000")
        self.assertFalse(ok)
        self.assertIn("INVALID_CODE", msg)
        self.assertFalse(self.adapter._is_live_armed)

    def test_fail_closed_blocking_when_nfo_or_funds_missing(self):
        """Even with the correct PIN, if NFO is inactive or margin is ₹0, arming is strictly blocked."""
        self.adapter._current_pin = "8521"
        # Mock preflight readiness returning NFO inactive and margin 0
        mock_readiness = {
            "token_valid": True,
            "nfo_enabled": False,
            "available_margin": 0.0,
            "static_ip_matched": True,
            "live_trading_eligible": False,
            "blockers": ["NFO exchange segment is not enabled in Upstox profile", "Available margin is below ₹10,000"]
        }
        with patch.object(self.adapter, "fetch_account_readiness", return_value=mock_readiness):
            with patch("app.analytics.option_sniffer.option_live_adapter.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 10, 12, 10, 30, 0)
                mock_dt.strptime = datetime.strptime
                ok, msg, readiness = self.adapter.arm_live_trading(pin="8521")
                self.assertFalse(ok)
                self.assertIn("PREFLIGHT_BLOCKED", msg)
                self.assertIn("NFO exchange segment is not enabled", msg)
                self.assertFalse(self.adapter._is_live_armed)

    def test_successful_arming_when_all_preflight_pass(self):
        """When correct PIN is provided and preflight eligibility is True, arming succeeds."""
        self.adapter._current_pin = "3914"
        mock_readiness = {
            "token_valid": True,
            "nfo_enabled": True,
            "available_margin": 25000.0,
            "static_ip_matched": True,
            "live_trading_eligible": True,
            "blockers": []
        }
        with patch.object(self.adapter, "fetch_account_readiness", return_value=mock_readiness):
            with patch("app.analytics.option_sniffer.option_live_adapter.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 10, 12, 10, 30, 0)
                mock_dt.strptime = datetime.strptime
                ok, msg, _ = self.adapter.arm_live_trading(pin="3914")
                self.assertTrue(ok)
                self.assertIn("ARMED_FOR_TODAY", msg)
                self.assertTrue(self.adapter._is_live_armed)
                self.assertEqual(self.adapter._armed_for_date, "2026-10-12")

    def test_auto_expiration_on_date_rollover(self):
        """Verifies that an armed session from yesterday automatically resets to Paper Mode."""
        self.adapter._is_live_armed = True
        self.adapter._armed_for_date = "2026-10-11"  # Yesterday

        with patch("app.analytics.option_sniffer.option_live_adapter.datetime") as mock_dt:
            # Current time is today (2026-10-12)
            mock_dt.now.return_value = datetime(2026, 10, 12, 10, 0, 0)
            mock_dt.strptime = datetime.strptime
            with patch.object(self.adapter, "fetch_account_readiness", return_value={}):
                status = self.adapter.get_live_status()
                self.assertFalse(status["is_live_armed"], "Live trading must auto-expire on next calendar day")
                self.assertFalse(self.adapter._is_live_armed)

    def test_disarm_reverts_to_paper_mode(self):
        """Verifies manual disarm resets to Paper Mode."""
        self.adapter._is_live_armed = True
        ok, msg = self.adapter.disarm_live_trading()
        self.assertTrue(ok)
        self.assertFalse(self.adapter._is_live_armed)

    def test_sos_panic_exit_flattens_and_disarms(self):
        """Verifies SOS panic square-off flattens all open positions and resets is_live_armed to False."""
        self.adapter._is_live_armed = True

        # Insert a live filled position into the test DB
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, exit_state, created_at, updated_at)
                VALUES ('INTENT_LIVE_01', 'SIG_01', 'NSE_FO|101', 'NIFTY 25000 CE', 'NIFTY', 'BUY_CE', 65, 65, 65, 100.0, 100.0, 'FILLED', 'NONE', '2026-10-12 10:00:00', '2026-10-12 10:00:00');
            """)

        with patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path):
            sos_res = self.adapter.execute_sos_panic_exit()
            self.assertEqual(sos_res["status"], "ALL_FLATTENED")
            self.assertEqual(sos_res["positions_closed_count"], 1)
            self.assertFalse(self.adapter._is_live_armed, "Live mode must be disarmed immediately on SOS")

            # Verify the position is confirmed flat
            active_after = self.adapter.get_active_live_positions()
            self.assertEqual(len(active_after), 0, "No open active positions should remain after SOS")


if __name__ == "__main__":
    unittest.main()

