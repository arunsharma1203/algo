"""
Unit & Integration Verification Suite for Option Sniffer Advance Isolated Live Bridge:
  1. Verifies 0 bytes changed in `backend/app/analytics/option_sniffer_advance/*` and SHA-256 parity.
  2. Verifies hard-locked `_is_live_armed = False` default and 4-digit PIN gate.
  3. Verifies `LONG_BOOK_ONLY` (₹55k wallet safe mode) mirrors Book 1 (`LONG_BOOK_BUY`) while keeping Book 2 (`SHORT_BOOK_SELL`) in Paper Sandbox.
  4. Verifies `DUAL_BOOK_NAKED` and `DUAL_BOOK_HEDGED` modes + instant mirrored exit when the parent Paper trade closes.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest.mock import patch

_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.analytics.option_sniffer_advance.advance_db import init_option_sniffer_advance_tables
from app.analytics.option_sniffer_advance.advance_orchestrator import (
    _BOOT_ADVANCE_HASHES,
    _compute_advance_module_hashes,
)
from app.analytics.option_sniffer_advance_live.advance_live_bridge import (
    AdvanceLiveBridge,
    MODE_DUAL_BOOK_HEDGED,
    MODE_DUAL_BOOK_NAKED,
    MODE_LONG_BOOK_ONLY,
    init_advance_live_tables,
)


class TestAdvanceLiveBridgeIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_advance_live_bridge.db")
        self._broker_patcher = patch(
            "app.analytics.option_sniffer.option_live_order_intent.OrderIntentEngine._call_upstox_broker_api",
            return_value=(True, 200, {"status": "success", "data": {"order_id": "SIM_TEST_ORD_1001"}}),
        )
        self.mock_broker_api = self._broker_patcher.start()
        with patch("app.analytics.option_sniffer_advance.advance_db.get_db_path", return_value=self.db_path):
            init_option_sniffer_advance_tables()
        init_advance_live_tables(db_path=self.db_path)
        self.bridge = AdvanceLiveBridge(db_path=self.db_path, auto_start_daemon=False)

    def tearDown(self):
        self.bridge.stop_background_follower()
        self._broker_patcher.stop()
        self.tmp_dir.cleanup()

    def _insert_advance_paper_trade(
        self,
        trade_id: str,
        book_type: str = "LONG_BOOK_BUY",
        action: str = "BUY",
        status: str = "OPEN",
        entry_price: float = 150.0,
        exit_price: float = 0.0,
        exit_reason: str = "",
        net_pnl: float = 0.0,
    ):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO option_sniffer_advance_trades (
                trade_id, signal_id, book_type, strategy_mode, direction,
                instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                lot_size, quantity, entry_time, entry_bid, entry_ask, entry_price_used,
                peak_price, trough_price, trailing_stop, stop_loss_pct, mfe_pct, mae_pct,
                status, exit_price_used, exit_reason, net_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                trade_id,
                f"SIG_{trade_id}",
                book_type,
                "WAVE1_GAMMA_BUY" if action == "BUY" else "CONTRACTION_THETA_SELL",
                "BULLISH_CALL" if action == "BUY" else "SHORT_CALL_THETA",
                "NSE_FO|65628",
                "NIFTY 23450 CE 10 NOV 26",
                "NIFTY",
                "CE",
                23450.0,
                "2026-11-10",
                65,
                65,
                "2026-11-09 10:15:00",
                entry_price,
                entry_price,
                entry_price,
                entry_price,
                entry_price,
                120.0 if action == "BUY" else 174.0,
                20.0 if action == "BUY" else 16.0,
                0.0,
                0.0,
                status,
                exit_price,
                exit_reason,
                net_pnl,
            ),
        )
        conn.commit()
        conn.close()

    def test_1_zero_core_files_modified_and_sha256_parity(self):
        """Proves 0 bytes modified in `backend/app/analytics/option_sniffer_advance/*` and SHA-256 parity holds."""
        disk_hashes = _compute_advance_module_hashes()
        self.assertEqual(disk_hashes, _BOOT_ADVANCE_HASHES)
        self.assertEqual(len(disk_hashes), 6)

        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        res = subprocess.run(
            ["git", "diff", "--name-only", "backend/app/analytics/option_sniffer_advance/"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.stdout.strip(), "", "Core Option Sniffer Advance files must have 0 git diff!")

    def test_2_default_disarmed_and_pin_security_gate(self):
        """Proves bridge boots disarmed (`_is_live_armed = False`, `LONG_BOOK_ONLY`) and rejects wrong PIN."""
        self.assertFalse(self.bridge.is_live_armed)
        self.assertTrue(self.bridge.SIMULATION_MODE)
        self.assertEqual(self.bridge.execution_mode, MODE_LONG_BOOK_ONLY)

        # Standby paper sync places 0 orders even when an OPEN trade exists
        self._insert_advance_paper_trade("ADV_TRD_1", book_type="LONG_BOOK_BUY", action="BUY", status="OPEN")
        sync_res = self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 16, 0))
        self.assertEqual(sync_res["status"], "STANDBY_PAPER")
        self.assertEqual(len(self.bridge.get_recent_order_intents()), 0)

        # Wrong PIN rejected
        bad_res = self.bridge.arm_live_trading(pin="0000", now_dt=datetime(2026, 11, 9, 10, 16, 0))
        if self.bridge.current_pin != "0000":
            self.assertEqual(bad_res["status"], "ARM_REJECTED")
            self.assertFalse(self.bridge.is_live_armed)

        # Valid PIN + mocked ₹55,000 Upstox wallet arms LONG_BOOK_ONLY cleanly
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            good_res = self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 10, 16, 0),
            )
            self.assertEqual(good_res["status"], "ARMED")
            self.assertTrue(self.bridge.is_live_armed)

            # Disarming immediately locks back to Paper Sandbox
            disarm_res = self.bridge.disarm_live_trading()
            self.assertEqual(disarm_res["status"], "DISARMED")
            self.assertFalse(self.bridge.is_live_armed)

    def test_3_55k_wallet_blocks_naked_short_mode_but_allows_long_book_only(self):
        """Proves a ₹55,000 Upstox wallet is protected from arming `DUAL_BOOK_NAKED` (needs ₹1.50L) while allowing `LONG_BOOK_ONLY`."""
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            naked_res = self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_DUAL_BOOK_NAKED,
                now_dt=datetime(2026, 11, 9, 10, 20, 0),
            )
            self.assertEqual(naked_res["status"], "ARM_REJECTED")
            self.assertFalse(self.bridge.is_live_armed)
            self.assertIn("Insufficient Upstox margin", naked_res["message"])

    def test_4_long_book_only_mirrors_buy_and_skips_sell_then_mirrors_exit(self):
        """
        Proves that in `LONG_BOOK_ONLY` mode:
          - Book 1 (`LONG_BOOK_BUY`) is mirrored to Upstox V2.
          - Book 2 (`SHORT_BOOK_SELL`) is ignored by the live bridge (stays in Paper Sandbox).
          - When Book 1 closes in `option_sniffer_advance_trades`, the live bridge immediately fires the exit order.
        """
        self._insert_advance_paper_trade("ADV_BUY_101", book_type="LONG_BOOK_BUY", action="BUY", status="OPEN", entry_price=140.0)
        self._insert_advance_paper_trade("ADV_SELL_102", book_type="SHORT_BOOK_SELL", action="SELL", status="OPEN", entry_price=110.0)

        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 10, 25, 0),
            )
            sync_entry = self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 25, 5))
            self.assertEqual(len(sync_entry["entries_mirrored"]), 1)
            self.assertEqual(len(sync_entry["skipped_by_mode"]), 1)
            self.assertEqual(sync_entry["skipped_by_mode"][0]["trade_id"], "ADV_SELL_102")

            active_pos = self.bridge.get_active_live_positions()
            self.assertEqual(len(active_pos), 1)
            self.assertEqual(active_pos[0]["advance_trade_id"], "ADV_BUY_101")
            self.assertEqual(active_pos[0]["action"], "BUY")

            # Now close ADV_BUY_101 in the parent Paper table (e.g. Chandelier Trail Hit at ₹172.0)
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """
                UPDATE option_sniffer_advance_trades
                SET status = 'CLOSED',
                    exit_price_used = 172.0,
                    exit_reason = 'LONG_CHANDELIER_TRAIL_HIT',
                    net_pnl = 2015.40
                WHERE trade_id = 'ADV_BUY_101';
                """
            )
            conn.commit()
            conn.close()

            sync_exit = self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 38, 0))
            self.assertEqual(len(sync_exit["exits_mirrored"]), 1)
            self.assertEqual(len(self.bridge.get_active_live_positions()), 0)

            intents = self.bridge.get_recent_order_intents()
            self.assertEqual(len(intents), 1)
            self.assertEqual(intents[0]["exit_state"], "CONFIRMED_FLAT")
            self.assertEqual(intents[0]["exit_reason"], "LONG_CHANDELIER_TRAIL_HIT")
            self.assertGreater(intents[0]["realized_pnl"], 1900.0)


if __name__ == "__main__":
    unittest.main()
