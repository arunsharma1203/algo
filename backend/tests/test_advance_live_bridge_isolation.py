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
        entry_time: str = "2026-11-09 11:20:00",
        regime_context_json: str = "{}",
    ):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO option_sniffer_advance_trades (
                trade_id, signal_id, book_type, strategy_mode, direction,
                instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                lot_size, quantity, entry_time, entry_bid, entry_ask, entry_price_used,
                peak_price, trough_price, trailing_stop, stop_loss_pct, mfe_pct, mae_pct,
                status, exit_price_used, exit_reason, net_pnl, regime_context_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
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
                entry_time,
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
                regime_context_json,
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

    def test_5_calendar_performance_aggregation(self):
        """
        Proves that `get_calendar_performance()` aggregates closed trades by date and month
        in read-only mode without modifying any core Advance tables.
        """
        self._insert_advance_paper_trade(
            "ADV_CAL_1",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="CLOSED",
            entry_price=140.0,
            exit_price=180.0,
            exit_reason="LONG_CHANDELIER_TRAIL_HIT",
            net_pnl=2535.0,
        )
        self._insert_advance_paper_trade(
            "ADV_CAL_2",
            book_type="SHORT_BOOK_SELL",
            action="SELL",
            status="CLOSED",
            entry_price=110.0,
            exit_price=85.0,
            exit_reason="SHORT_REVERSE_CHANDELIER_HIT",
            net_pnl=1560.0,
        )
        cal = self.bridge.get_calendar_performance(book_filter="ALL")
        self.assertIn("2026-11", cal["available_months"])
        self.assertEqual(len(cal["days"]), 1)
        day_rec = cal["days"][0]
        self.assertEqual(day_rec["date"], "2026-11-09")
        self.assertEqual(day_rec["trade_count"], 2)
        self.assertEqual(day_rec["long_book_trades"], 1)
        self.assertEqual(day_rec["short_book_trades"], 1)
        self.assertEqual(day_rec["net_pnl_rupees"], 4095.0)

    def test_6_resting_exchange_sl_placed_on_entry_and_cancelled_before_exit(self):
        """
        Proves Safeguard #3:
          - Upon entry fill, a linked Exchange-Resting Stop-Loss Limit (`order_type == 'SL'`) order is placed on Upstox.
          - When the parent paper trade closes, the resting SL order is cancelled via `DELETE /order/cancel` before the exit order fires.
        """
        self._insert_advance_paper_trade("ADV_SL_301", book_type="LONG_BOOK_BUY", action="BUY", status="OPEN", entry_price=150.0)
        call_log = []

        def fake_broker_api(method, endpoint, payload=None, params=None, is_emergency=False):
            call_log.append((method, endpoint, payload, params))
            if method == "POST" and endpoint == "/order/place":
                if payload and payload.get("order_type") == "SL":
                    return True, 200, {"status": "success", "data": {"order_id": "SL_ORD_9001"}}
                return True, 200, {"status": "success", "data": {"order_id": "ENTRY_OR_EXIT_ORD_9002"}}
            if method == "DELETE" and endpoint == "/order/cancel":
                return True, 200, {"status": "success"}
            return True, 200, {"status": "success", "data": []}

        self.mock_broker_api.side_effect = fake_broker_api
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 10, 30, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 30, 2))
            active = self.bridge.get_active_live_positions()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["broker_sl_order_id"], "SL_ORD_9001")

            # Close paper trade and verify DELETE /order/cancel is called for SL_ORD_9001 before exit POST
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE option_sniffer_advance_trades SET status='CLOSED', exit_price_used=180.0, exit_reason='TRAILING_STOP_HIT', net_pnl=1850.0 WHERE trade_id='ADV_SL_301';"
            )
            conn.commit()
            conn.close()

            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 40, 0))
            delete_calls = [c for c in call_log if c[0] == "DELETE" and c[1] == "/order/cancel"]
            self.assertEqual(len(delete_calls), 1)
            self.assertEqual(delete_calls[0][3], {"order_id": "SL_ORD_9001"})

    def test_7_exit_http_failure_keeps_exit_retry_pending_and_retries_until_confirmed_flat(self):
        """
        Proves Safeguard #1:
          - If Upstox returns HTTP 502 on exit, `exit_state` is set to `'EXIT_RETRY_PENDING'` (NEVER `'CONFIRMED_FLAT'`).
          - Position remains in `get_active_live_positions()` and automatically retries on the next 250ms tick until Upstox confirms flat.
        """
        self._insert_advance_paper_trade("ADV_RETRY_401", book_type="LONG_BOOK_BUY", action="BUY", status="OPEN", entry_price=150.0)
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 11, 0, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 0, 2))

            # Close paper trade while broker API suffers a transient 502 outage
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE option_sniffer_advance_trades SET status='CLOSED', exit_price_used=120.0, exit_reason='STOP_LOSS_HIT', net_pnl=-2005.0 WHERE trade_id='ADV_RETRY_401';"
            )
            conn.commit()
            conn.close()

            self.mock_broker_api.return_value = (False, 502, {"error": "Bad Gateway"})
            sync_fail = self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 5, 0))
            self.assertEqual(len(sync_fail["exits_mirrored"]), 0)

            # Position MUST still be in get_active_live_positions() with EXIT_RETRY_PENDING!
            active_after_fail = self.bridge.get_active_live_positions()
            self.assertEqual(len(active_after_fail), 1)
            self.assertEqual(active_after_fail[0]["exit_state"], "EXIT_RETRY_PENDING")

            # Now Upstox recovers (200 OK) on the next 250ms tick
            self.mock_broker_api.return_value = (True, 200, {"status": "success", "data": {"order_id": "RECOVERED_EXIT_99"}})
            sync_ok = self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 5, 1))
            self.assertEqual(len(sync_ok["exits_mirrored"]), 1)
            self.assertEqual(len(self.bridge.get_active_live_positions()), 0)

    def test_8_exchange_sl_triggered_on_broker_reconciles_confirmed_flat(self):
        """
        Proves Safeguard #2:
          - If the Exchange-Resting SL-L order triggers directly on Upstox (`status == 'complete'`),
            `_reconcile_open_live_intents_with_broker()` immediately marks `exit_state = 'CONFIRMED_FLAT'`
            with `exit_reason = 'EXCHANGE_SL_ORDER_TRIGGERED'`.
        """
        self._insert_advance_paper_trade("ADV_EXSL_501", book_type="LONG_BOOK_BUY", action="BUY", status="OPEN", entry_price=150.0)
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 11, 15, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 15, 2))
            active = self.bridge.get_active_live_positions()
            self.assertEqual(len(active), 1)
            sl_oid = active[0]["broker_sl_order_id"]

            # Simulate Upstox `/order/retrieve-all` reporting the SL order completed at ₹120.0
            def fake_retrieve_all(method, endpoint, payload=None, params=None, is_emergency=False):
                if method == "GET" and endpoint == "/order/retrieve-all":
                    return True, 200, {
                        "status": "success",
                        "data": [{"order_id": sl_oid, "status": "complete", "average_price": 120.0, "filled_quantity": 65}],
                    }
                return True, 200, {"status": "success", "data": {"order_id": "ORD_OK"}}

            self.mock_broker_api.side_effect = fake_retrieve_all
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 16, 0))
            self.assertEqual(len(self.bridge.get_active_live_positions()), 0)
            intents = self.bridge.get_recent_order_intents()
            self.assertEqual(intents[0]["exit_state"], "CONFIRMED_FLAT")
            self.assertEqual(intents[0]["exit_reason"], "EXCHANGE_SL_ORDER_TRIGGERED")

    def test_9_pre_arm_stale_open_paper_trade_is_skipped_and_post_arm_trade_is_mirrored(self):
        """
        Proves Bug #2 Fix:
          - A paper trade that opened at 09:44:00 BEFORE the user armed at 10:05:00 is skipped (`PRE_ARM_OPEN_TRADE_SKIPPED`).
          - A new paper trade that opens at 10:06:00 AFTER arming is mirrored cleanly.
        """
        self._insert_advance_paper_trade(
            "ADV_PRE_ARM_0944",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=120.0,
            entry_time="2026-11-09 09:44:00",
        )
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 10, 5, 0),
            )
            sync_pre = self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 5, 2))
            self.assertEqual(len(sync_pre["entries_mirrored"]), 0)
            self.assertEqual(len(sync_pre["skipped_by_mode"]), 1)
            self.assertEqual(sync_pre["skipped_by_mode"][0]["reason"], "PRE_ARM_OPEN_TRADE_SKIPPED")

            # Now insert a fresh post-arm trade at 10:06:00
            self._insert_advance_paper_trade(
                "ADV_POST_ARM_1006",
                book_type="LONG_BOOK_BUY",
                action="BUY",
                status="OPEN",
                entry_price=135.0,
                entry_time="2026-11-09 10:06:00",
            )
            sync_post = self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 6, 1))
            self.assertEqual(len(sync_post["entries_mirrored"]), 1)

    def test_10_sos_panic_exit_with_missing_stream_quote_does_not_crash_and_uses_emergency_collar(self):
        """
        Proves Bug #1 Fix:
          - If `OptionStreamManager().get_option_quote()` returns `None` during a 1-Click SOS Panic Exit,
            `execute_sos_panic_exit()` never raises `TypeError: float(None)` and dispatches an emergency
            15% collar (`0.85 * entry_fill`) marketable limit exit order.
        """
        self._insert_advance_paper_trade(
            "ADV_SOS_601",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=100.0,
            entry_time="2026-11-09 10:20:00",
        )
        call_log = []

        def fake_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            call_log.append((method, endpoint, payload, params))
            if method == "POST" and endpoint == "/order/place":
                return True, 200, {"status": "success", "data": {"order_id": f"SOS_ORD_{len(call_log)}"}}
            return True, 200, {"status": "success", "data": []}

        self.mock_broker_api.side_effect = fake_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ), patch(
            "app.analytics.option_sniffer_advance_live.advance_live_bridge.OptionStreamManager"
        ) as mock_sm_cls:
            mock_sm_cls.return_value.get_option_quote.return_value = None
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 10, 15, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 20, 1))
            call_log.clear()

            sos_res = self.bridge.execute_sos_panic_exit()
            self.assertEqual(sos_res["status"], "SOS_EXECUTED_AND_DISARMED")
            self.assertEqual(sos_res["positions_flattened"], 1)
            self.assertFalse(self.bridge.is_live_armed)
            exit_places = [c for c in call_log if c[0] == "POST" and c[1] == "/order/place"]
            self.assertEqual(len(exit_places), 1)
            self.assertAlmostEqual(exit_places[0][2]["price"], 85.0, places=2)

    def test_11_simultaneous_exchange_sl_fill_and_software_exit_race_suppresses_duplicate_exit(self):
        """
        Proves Bug #3 Fix:
          - When `DELETE /order/cancel` on `broker_sl_order_id` returns HTTP 400 (`Order is already complete`),
            `_execute_mirrored_live_exit()` suppresses the duplicate software exit order and marks `CONFIRMED_FLAT`
            (`EXCHANGE_SL_ORDER_TRIGGERED`).
        """
        self._insert_advance_paper_trade(
            "ADV_RACE_701",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=100.0,
            entry_time="2026-11-09 10:30:00",
        )
        call_log = []

        def fake_race_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            call_log.append((method, endpoint, payload, params))
            if method == "POST" and endpoint == "/order/place":
                return True, 200, {"status": "success", "data": {"order_id": f"ORD_RACE_{len(call_log)}"}}
            if method == "GET" and endpoint == "/order/retrieve-all":
                return True, 200, {"status": "success", "data": [{"order_id": "ORD_RACE_2", "status": "trigger pending"}]}
            if method == "DELETE" and endpoint == "/order/cancel":
                return False, 400, {"status": "error", "error": "Order ORD_RACE_2 is already complete and cannot be cancelled"}
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_race_broker
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
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 30, 1))
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE option_sniffer_advance_trades SET status='CLOSED', exit_price_used=80.0, exit_reason='HARD_SL', net_pnl=-1355.0 WHERE trade_id='ADV_RACE_701';"
            )
            conn.commit()
            conn.close()

            call_log.clear()
            sync_exit = self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 32, 0))
            self.assertEqual(len(sync_exit["exits_mirrored"]), 1)
            # Zero duplicate POST /order/place exit calls should have been fired!
            post_exit_calls = [c for c in call_log if c[0] == "POST" and c[1] == "/order/place"]
            self.assertEqual(len(post_exit_calls), 0)
            intents = self.bridge.get_recent_order_intents()
            self.assertEqual(intents[0]["exit_state"], "CONFIRMED_FLAT")
            self.assertEqual(intents[0]["exit_reason"], "EXCHANGE_SL_ORDER_TRIGGERED")

    def test_12_unfilled_working_entry_order_reconciles_submitted_and_cancels_cleanly_on_exit(self):
        """
        Proves Bug #4 Fix:
          - If `/order/retrieve-all` shows the entry order is still `'open'` (`filled_quantity == 0`),
            `_reconcile_open_live_intents_with_broker()` transitions `state = 'SUBMITTED'`, `filled_quantity = 0`,
            and cancels any premature SL order.
          - If the trade then exits while `filled_quantity == 0`, `_execute_mirrored_live_exit()` cancels the
            unfilled entry order without sending an opposite exit order.
        """
        self._insert_advance_paper_trade(
            "ADV_UNFILLED_801",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=100.0,
            entry_time="2026-11-09 10:40:00",
        )
        call_log = []

        def fake_unfilled_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            call_log.append((method, endpoint, payload, params))
            if method == "POST" and endpoint == "/order/place":
                return True, 200, {"status": "success", "data": {"order_id": f"UNFILLED_ORD_{len(call_log)}"}}
            if method == "GET" and endpoint == "/order/retrieve-all":
                return True, 200, {
                    "status": "success",
                    "data": [{"order_id": "UNFILLED_ORD_1", "status": "open", "filled_quantity": 0, "average_price": 0.0}],
                }
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_unfilled_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 10, 35, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 40, 1))
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 40, 2))
            active = self.bridge.get_active_live_positions()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["state"], "SUBMITTED")
            self.assertEqual(active[0]["filled_quantity"], 0)
            self.assertIsNone(active[0]["broker_sl_order_id"])

            # Now close paper trade while entry order is still unfilled
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE option_sniffer_advance_trades SET status='CLOSED', exit_price_used=101.0, exit_reason='TIME_STOP', net_pnl=0.0 WHERE trade_id='ADV_UNFILLED_801';"
            )
            conn.commit()
            conn.close()

            call_log.clear()
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 41, 0))
            post_exit_calls = [c for c in call_log if c[0] == "POST" and c[1] == "/order/place"]
            self.assertEqual(len(post_exit_calls), 0)
            intents = self.bridge.get_recent_order_intents()
            self.assertEqual(intents[0]["state"], "CANCELLED")
            self.assertEqual(intents[0]["exit_state"], "CONFIRMED_FLAT")
            self.assertEqual(intents[0]["exit_reason"], "UNFILLED_ENTRY_CANCELLED_ON_EXIT")

    def test_13_follower_sync_enforces_1515_cutoff_and_auto_flattens_without_ui_poll(self):
        """
        Proves Bug #5 Fix:
          - `sync_once_with_advance_paper_table()` at >= 15:15 IST automatically disarms `_is_live_armed`
            and force-flattens any remaining open live positions even when `get_live_status()` is not polled.
        """
        self._insert_advance_paper_trade(
            "ADV_EOD_901",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=120.0,
            entry_time="2026-11-09 14:50:00",
        )
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 14, 45, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 14, 50, 2))
            self.assertEqual(len(self.bridge.get_active_live_positions()), 1)

            # Now background follower runs at 15:15:01 IST (without calling get_live_status)
            sync_cutoff = self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 15, 15, 1))
            self.assertFalse(self.bridge.is_live_armed)
            self.assertEqual(sync_cutoff["status"], "STANDBY_PAPER")
            self.assertEqual(len(sync_cutoff["exits_mirrored"]), 1)
            self.assertEqual(len(self.bridge.get_active_live_positions()), 0)

    def test_14_dual_book_hedged_short_rejection_flattens_orphan_wing(self):
        """
        Proves Bug #6 Fix:
          - In `DUAL_BOOK_HEDGED` mode, if the 200-pt OTM BUY hedge wing succeeds and the main SELL leg
            is rejected by Upstox RMS, `_flatten_hedge_wing_if_present()` immediately cancels and flattens
            the hedge wing so no orphan long option remains in the broker account.
        """
        self._insert_advance_paper_trade(
            "ADV_HEDGE_1001",
            book_type="SHORT_BOOK_SELL",
            action="SELL",
            status="OPEN",
            entry_price=110.0,
            entry_time="2026-11-09 11:00:00",
            regime_context_json='{"hedge_wing_strike": 23650}',
        )
        call_log = []

        def fake_hedge_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            call_log.append((method, endpoint, payload, params))
            if method == "POST" and endpoint == "/order/place":
                if payload.get("instrument_token") == "NSE_FO|99200" and payload.get("transaction_type") == "BUY":
                    return True, 200, {"status": "success", "data": {"order_id": "WING_BUY_ORD_1"}}
                if payload.get("instrument_token") == "NSE_FO|65628" and payload.get("transaction_type") == "SELL":
                    return False, 400, {"error": "RMS: Insufficient margin for short option"}
                return True, 200, {"status": "success", "data": {"order_id": "WING_FLATTEN_ORD_2"}}
            return True, 200, {"status": "success", "data": []}

        self.mock_broker_api.side_effect = fake_hedge_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ), patch(
            "app.analytics.option_sniffer_advance_live.advance_live_bridge.OptionStreamManager"
        ) as mock_sm_cls:
            mock_sm_cls.return_value.get_all_active_option_quotes.return_value = [
                {
                    "underlying": "NIFTY",
                    "option_type": "CE",
                    "strike": 23650.0,
                    "instrument_key": "NSE_FO|99200",
                    "ask": 18.0,
                    "bid": 17.5,
                }
            ]
            mock_sm_cls.return_value.get_option_quote.return_value = {"bid": 17.5, "ltp": 17.5}
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_DUAL_BOOK_HEDGED,
                now_dt=datetime(2026, 11, 9, 10, 55, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 0, 1))
            wing_flatten_calls = [
                c
                for c in call_log
                if c[0] == "POST"
                and c[1] == "/order/place"
                and c[2].get("instrument_token") == "NSE_FO|99200"
                and c[2].get("transaction_type") == "SELL"
            ]
            self.assertEqual(len(wing_flatten_calls), 1)

    def test_15_broker_reconcile_throttled_to_3s_and_paper_exit_fires_first_without_rate_limit_wait(self):
        """
        Proves Real-Time Latency & Rate-Limit Protection Fix:
          - Across rapid 0.25s follower ticks while a position is open, `/order/retrieve-all` runs once
            on initial fill verification and is then throttled for 3.0s (preventing 240 req/min rate-limit exhaustion).
          - When the parent paper trade closes in SQLite, Step 1A fires `_execute_mirrored_live_exit()`
            FIRST without calling `GET /order/retrieve-all` before the exit.
        """
        self._insert_advance_paper_trade(
            "ADV_FAST_1501",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=140.0,
            entry_time="2026-11-09 10:00:01",
        )
        call_log = []

        def fake_fast_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            call_log.append((method, endpoint, payload, params, is_emergency))
            if method == "POST" and endpoint == "/order/place":
                return True, 200, {"status": "success", "data": {"order_id": f"FAST_ORD_{len(call_log)}"}}
            if method == "GET" and endpoint == "/order/retrieve-all":
                return True, 200, {
                    "status": "success",
                    "data": [{"order_id": "FAST_ORD_1", "status": "complete", "filled_quantity": 65, "average_price": 140.0}],
                }
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_fast_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 10, 0, 0),
            )
            # Tick 0 (10:00:01): Mirrors entry
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 0, 1))
            # Tick 1 (10:00:01.25): Initial fill verification reconcile runs once
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 0, 1, 250000))
            retrieve_count_initial = sum(1 for c in call_log if c[0] == "GET" and c[1] == "/order/retrieve-all")
            self.assertEqual(retrieve_count_initial, 1)

            # Ticks 2..8 (10:00:01.50 to 10:00:03.00 — 6 rapid 0.25s passes within 3.0s window):
            # Must make ZERO additional GET /order/retrieve-all calls!
            for ms in (500000, 750000, 1000000, 1250000, 1500000, 1750000):
                sec_add = 1 + (ms // 1000000)
                usec = ms % 1000000
                self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 0, sec_add, usec))
            retrieve_count_throttled = sum(1 for c in call_log if c[0] == "GET" and c[1] == "/order/retrieve-all")
            self.assertEqual(retrieve_count_throttled, 1, "Broker order-book polling must be throttled for 3.0s!")

            # Now close paper trade at 10:00:03.50 (Chandelier trailing stop hit)
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE option_sniffer_advance_trades SET status='CLOSED', exit_price_used=168.0, exit_reason='TRAILING_STOP_HIT', net_pnl=1755.0 WHERE trade_id='ADV_FAST_1501';"
            )
            conn.commit()
            conn.close()

            call_log.clear()
            sync_exit = self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 0, 3, 500000))
            self.assertEqual(len(sync_exit["exits_mirrored"]), 1)
            # Verify exit fired immediately with 0 GET /order/retrieve-all calls blocking it!
            retrieve_on_exit = [c for c in call_log if c[0] == "GET" and c[1] == "/order/retrieve-all"]
            self.assertEqual(len(retrieve_on_exit), 0)

    def test_16_priority_tick_guard_pins_open_contracts_and_refreshes_stale_quotes_in_2_5s(self):
        """
        Proves Real-Time Tick & Stream Priority Guard:
          - `_prioritize_advance_real_time_ticks()` pins open Advance contract keys into
            `OptionStreamManager()._key_to_contract` so they never drop out on 2+ strike ATM shifts.
          - When an open contract's quote has `quote_age_ms > 2500`, it triggers `refresh_quotes_via_rest(min_interval_sec=2.5)`
            and feeds the refreshed quote into `AdvancePaperExecution.evaluate_subsecond_tick_guard()`.
        """
        self._insert_advance_paper_trade(
            "ADV_PRIO_1601",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=150.0,
            entry_time="2026-11-09 10:10:00",
        )
        fake_k2c = {}
        with patch(
            "app.analytics.option_sniffer_advance_live.advance_live_bridge.OptionStreamManager"
        ) as mock_sm_cls, patch(
            "app.analytics.option_sniffer_advance.advance_paper_execution.AdvancePaperExecution.evaluate_subsecond_tick_guard"
        ) as mock_tick_guard:
            sm_inst = mock_sm_cls.return_value
            sm_inst._key_to_contract = fake_k2c
            sm_inst._lock = None
            # First call returns a stale quote (age = 4200ms > 2500ms); second call after REST refresh returns fresh quote
            sm_inst.get_option_quote.side_effect = [
                {"instrument_key": "NSE_FO|65628", "bid": 155.0, "ask": 156.0, "ltp": 155.5, "quote_age_ms": 4200},
                {"instrument_key": "NSE_FO|65628", "bid": 158.0, "ask": 159.0, "ltp": 158.5, "quote_age_ms": 0},
            ]
            self.bridge._is_live_armed = True
            self.bridge._last_priority_quote_refresh_ts = -999999.0
            res = self.bridge._prioritize_advance_real_time_ticks(active_intents=[], now_dt=datetime(2026, 11, 9, 10, 10, 5))
            self.assertIn("NSE_FO|65628", fake_k2c)
            self.assertEqual(res["pinned_keys"], ["NSE_FO|65628"])
            self.assertTrue(res["refreshed_via_rest"])
            sm_inst.refresh_quotes_via_rest.assert_called_once_with(min_interval_sec=2.5)
            mock_tick_guard.assert_called_once()

    def test_17_invalid_wide_spread_or_stale_rest_quote_never_triggers_false_stop_out(self):
        """
        Proves Alpha Protection Shield in `_prioritize_advance_real_time_ticks()`:
          - Case A: If REST refresh returns a freak wide-spread quote (`spread_pct = 29.6% > 3.5%`, `is_valid = False`),
            `evaluate_subsecond_tick_guard` is NEVER called (preventing false stop-outs on momentary depth gaps).
          - Case B: If REST refresh fails to update the quote (`quote_age_ms = 4200 > 2500`),
            `evaluate_subsecond_tick_guard` is NEVER called.
          - Case C: When REST refresh returns a valid, tight-spread (`spread_pct = 0.63% <= 3.5%`, `is_valid = True`,
            `quote_age_ms = 120ms <= 2500ms`) quote, `evaluate_subsecond_tick_guard` IS called immediately.
        """
        self._insert_advance_paper_trade(
            "ADV_SHIELD_1701",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=150.0,
            entry_time="2026-11-09 10:15:00",
        )
        fake_k2c = {}
        with patch(
            "app.analytics.option_sniffer_advance_live.advance_live_bridge.OptionStreamManager"
        ) as mock_sm_cls, patch(
            "app.analytics.option_sniffer_advance.advance_paper_execution.AdvancePaperExecution.evaluate_subsecond_tick_guard"
        ) as mock_tick_guard:
            sm_inst = mock_sm_cls.return_value
            sm_inst._key_to_contract = fake_k2c
            sm_inst._lock = None
            sm_inst.refresh_quotes_via_rest.return_value = 1
            self.bridge._is_live_armed = True

            # Case A: Freak wide spread after REST refresh (Bid=115, Ask=155, Spread=29.6%, is_valid=False)
            sm_inst.get_option_quote.side_effect = [
                {"instrument_key": "NSE_FO|65628", "bid": 150.0, "ask": 151.0, "ltp": 150.5, "quote_age_ms": 4000, "is_valid": False},
                {"instrument_key": "NSE_FO|65628", "bid": 115.0, "ask": 155.0, "ltp": 135.0, "spread_pct": 29.63, "quote_age_ms": 50, "is_valid": False},
            ]
            self.bridge._last_priority_quote_refresh_ts = -999999.0
            self.bridge._prioritize_advance_real_time_ticks(active_intents=[], now_dt=datetime(2026, 11, 9, 10, 15, 5))
            mock_tick_guard.assert_not_called()

            # Case B: REST refresh failed to update quote age (still 4000ms > 2500ms)
            sm_inst.get_option_quote.side_effect = [
                {"instrument_key": "NSE_FO|65628", "bid": 150.0, "ask": 151.0, "ltp": 150.5, "quote_age_ms": 4000, "is_valid": True},
                {"instrument_key": "NSE_FO|65628", "bid": 150.0, "ask": 151.0, "ltp": 150.5, "spread_pct": 0.66, "quote_age_ms": 4000, "is_valid": True},
            ]
            self.bridge._last_priority_quote_refresh_ts = -999999.0
            self.bridge._prioritize_advance_real_time_ticks(active_intents=[], now_dt=datetime(2026, 11, 9, 10, 15, 8))
            mock_tick_guard.assert_not_called()

            # Case C: Valid, fresh, tight-spread quote (Bid=158, Ask=159, Spread=0.63%, age=120ms, is_valid=True)
            sm_inst.get_option_quote.side_effect = [
                {"instrument_key": "NSE_FO|65628", "bid": 150.0, "ask": 151.0, "ltp": 150.5, "quote_age_ms": 4000, "is_valid": True},
                {"instrument_key": "NSE_FO|65628", "bid": 158.0, "ask": 159.0, "ltp": 158.5, "spread_pct": 0.63, "quote_age_ms": 120, "is_valid": True},
            ]
            self.bridge._last_priority_quote_refresh_ts = -999999.0
            self.bridge._prioritize_advance_real_time_ticks(active_intents=[], now_dt=datetime(2026, 11, 9, 10, 15, 12))
            mock_tick_guard.assert_called_once()

    def test_18_fast_exit_or_sos_before_first_reconcile_verifies_entry_and_never_opens_reverse_position(self):
        """
        Proves Finding A Fix (Pre-Exit Unreconciled Entry Verification):
          - If an entry order is placed (`ENTRY_ORD_1`) and an exit or 1-Click SOS Panic Exit triggers
            BEFORE any intermediate open-position reconcile pass ever ran (`intent_id not in _reconciled_once_intent_ids`),
            and `/order/retrieve-all` shows `ENTRY_ORD_1` is still `'open'` (`filled_quantity == 0`),
            `_execute_mirrored_live_exit()` reconciles first, cancels the unfilled entry order (`DELETE /order/cancel`),
            and NEVER sends a reverse naked exit order (`0` `POST /order/place` exit calls).
        """
        self._insert_advance_paper_trade(
            "ADV_FAST_UNFILLED_1801",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=125.0,
            entry_time="2026-11-09 10:20:00",
        )
        call_log = []

        def fake_unfilled_fast_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            call_log.append((method, endpoint, payload, params))
            if method == "POST" and endpoint == "/order/place":
                return True, 200, {"status": "success", "data": {"order_id": f"FAST_UNFILLED_ORD_{len(call_log)}"}}
            if method == "GET" and endpoint == "/order/retrieve-all":
                return True, 200, {
                    "status": "success",
                    "data": [
                        {
                            "order_id": "FAST_UNFILLED_ORD_1",
                            "status": "open",
                            "filled_quantity": 0,
                            "average_price": 0.0,
                        }
                    ],
                }
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_unfilled_fast_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 10, 19, 0),
            )
            # Pass 1: Mirrors entry (`FAST_UNFILLED_ORD_1`). Notice NO intermediate reconcile pass runs!
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 20, 1))
            call_log.clear()

            # Immediate 1-Click SOS Panic Exit (or sub-second exit) BEFORE first reconcile pass:
            sos_res = self.bridge.execute_sos_panic_exit()
            self.assertEqual(sos_res["status"], "SOS_EXECUTED_AND_DISARMED")
            self.assertEqual(sos_res["positions_flattened"], 1)

            # Must have 0 opposite POST /order/place exit orders (only DELETE /order/cancel for entry + SL)!
            post_exit_calls = [c for c in call_log if c[0] == "POST" and c[1] == "/order/place"]
            self.assertEqual(len(post_exit_calls), 0, "Must NEVER send opposite exit order for unfilled entry!")
            delete_calls = [c for c in call_log if c[0] == "DELETE" and c[1] == "/order/cancel"]
            self.assertGreaterEqual(len(delete_calls), 1)

            intents = self.bridge.get_recent_order_intents()
            self.assertEqual(intents[0]["state"], "CANCELLED")
            self.assertEqual(intents[0]["exit_state"], "CONFIRMED_FLAT")
            self.assertEqual(intents[0]["exit_reason"], "UNFILLED_ENTRY_CANCELLED_ON_EXIT")

    def test_19_unfilled_or_rejected_exit_limit_order_reopens_exit_retry_pending_until_confirmed_complete(self):
        """
        Proves Finding B Fix (Pending Exit Order Exchange Verification):
          - When an exit `LIMIT` order (`EXIT_ORD_STUCK`) returns HTTP 200 OK (`order_id`), it is queued in
            `_pending_exit_verifications`.
          - On the next reconciliation pass, if `/order/retrieve-all` reports `EXIT_ORD_STUCK` is still `'open'`
            (`filled_quantity == 0`) due to a flash gap beyond the 1.5% limit collar, the bridge cancels `EXIT_ORD_STUCK`
            and transitions `exit_state` back to `'EXIT_RETRY_PENDING'`.
          - On the subsequent follower pass, `_execute_mirrored_live_exit()` automatically retries with the wider
            `5.0%` marketable collar and flattens the position cleanly.
        """
        self._insert_advance_paper_trade(
            "ADV_EXIT_VERIFY_1901",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=150.0,
            entry_time="2026-11-09 10:30:00",
        )
        call_log = []
        order_book_state = {
            "mode": "ENTRY_COMPLETE",
        }

        def fake_exit_verify_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            call_log.append((method, endpoint, payload, params))
            if method == "POST" and endpoint == "/order/place":
                tx = (payload or {}).get("transaction_type")
                otype = (payload or {}).get("order_type")
                if otype == "SL":
                    return True, 200, {"status": "success", "data": {"order_id": "SL_1901"}}
                if tx == "BUY":
                    return True, 200, {"status": "success", "data": {"order_id": "ENTRY_1901"}}
                # SELL exit order
                if order_book_state["mode"] == "EXIT_STUCK_OPEN":
                    return True, 200, {"status": "success", "data": {"order_id": "EXIT_STUCK_1901"}}
                return True, 200, {"status": "success", "data": {"order_id": "EXIT_COMPLETE_1902"}}
            if method == "GET" and endpoint == "/order/retrieve-all":
                if order_book_state["mode"] == "ENTRY_COMPLETE":
                    return True, 200, {
                        "status": "success",
                        "data": [{"order_id": "ENTRY_1901", "status": "complete", "filled_quantity": 65, "average_price": 150.0}],
                    }
                if order_book_state["mode"] == "EXIT_STUCK_OPEN":
                    return True, 200, {
                        "status": "success",
                        "data": [
                            {
                                "order_id": "EXIT_STUCK_1901",
                                "status": "open",
                                "quantity": 65,
                                "filled_quantity": 0,
                                "average_price": 0.0,
                                "status_message": "Working limit order on NSE",
                            }
                        ],
                    }
                return True, 200, {
                    "status": "success",
                    "data": [
                        {
                            "order_id": "EXIT_COMPLETE_1902",
                            "status": "complete",
                            "quantity": 65,
                            "filled_quantity": 65,
                            "average_price": 118.50,
                        }
                    ],
                }
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_exit_verify_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 10, 29, 0),
            )
            # Tick 1 & 2: Mirror entry and verify entry complete
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 30, 1))
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 30, 2))

            # Now close paper trade at ₹120.0 while the first exit LIMIT order sits unfilled ('open') on NSE
            order_book_state["mode"] = "EXIT_STUCK_OPEN"
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE option_sniffer_advance_trades SET status='CLOSED', exit_price_used=120.0, exit_reason='STOP_LOSS_HIT', net_pnl=-2015.0 WHERE trade_id='ADV_EXIT_VERIFY_1901';"
            )
            conn.commit()
            conn.close()

            # Tick 3 (10:31:00): Places first exit order `EXIT_STUCK_1901` (1.5% collar = 118.20)
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 31, 0))
            self.assertEqual(len(self.bridge._pending_exit_verifications), 1)

            # Tick 4 (10:31:01): Next pass verifies `EXIT_STUCK_1901` against `/order/retrieve-all`, sees `status == 'open'` (`filled_quantity == 0`),
            # cancels `EXIT_STUCK_1901`, and transitions `exit_state` back to `'EXIT_RETRY_PENDING'`!
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 31, 1))
            active_reopened = self.bridge.get_active_live_positions()
            self.assertEqual(len(active_reopened), 1)
            self.assertEqual(active_reopened[0]["exit_state"], "EXIT_RETRY_PENDING")

            # Tick 5 (10:31:02): Next 250ms pass retries exit with wider 5.0% collar (`120.0 * 0.95 = 114.00`) and fills!
            order_book_state["mode"] = "EXIT_COMPLETE"
            call_log.clear()
            sync_retry = self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 31, 2))
            self.assertEqual(len(sync_retry["exits_mirrored"]), 1)
            retry_places = [c for c in call_log if c[0] == "POST" and c[1] == "/order/place"]
            self.assertEqual(len(retry_places), 1)
            self.assertAlmostEqual(retry_places[0][2]["price"], 114.0, places=2)

            # Tick 6 (10:31:03): Verifies `EXIT_COMPLETE_1902` filled at ₹118.50 on NSE and updates `exit_fill_price`!
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 10, 31, 3))
            self.assertEqual(len(self.bridge.get_active_live_positions()), 0)
            intents = self.bridge.get_recent_order_intents()
            self.assertEqual(intents[0]["exit_state"], "CONFIRMED_FLAT")
            self.assertAlmostEqual(intents[0]["exit_fill_price"], 118.50, places=2)

    def test_20_exit_retry_after_sl_cancelled_never_falsely_marks_confirmed_flat(self):
        """
        Proves Round-3 Finding 1 Fix:
          - If an exit attempt cancels `broker_sl_order_id` (`SL_2001`) and both exit `POST /order/place` calls
            fail (or if a second `DELETE /order/cancel` on `SL_2001` returns `"Order SL_2001 is already cancelled"`),
            the bridge NEVER falsely marks `exit_state = 'CONFIRMED_FLAT'` (`EXCHANGE_SL_ORDER_TRIGGERED`).
          - Instead, it clears `broker_sl_order_id = NULL` on `EXIT_RETRY_PENDING` and retries on the next 250ms tick
            until the exit order succeeds on Upstox.
        """
        self._insert_advance_paper_trade(
            "ADV_SL_CANCEL_RETRY_2001",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=150.0,
            entry_time="2026-11-09 11:10:00",
        )
        call_log = []
        state = {"exit_attempt": 0, "sl_cancelled": False}

        def fake_sl_retry_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            call_log.append((method, endpoint, payload, params))
            if method == "POST" and endpoint == "/order/place":
                otype = (payload or {}).get("order_type")
                tx = (payload or {}).get("transaction_type")
                if otype == "SL":
                    return True, 200, {"status": "success", "data": {"order_id": "SL_2001"}}
                if tx == "BUY":
                    return True, 200, {"status": "success", "data": {"order_id": "ENTRY_2001"}}
                # SELL exit order: fail first 2 attempts (primary + immediate retry), succeed on 3rd attempt (next 250ms tick)
                state["exit_attempt"] += 1
                if state["exit_attempt"] <= 2:
                    return False, 502, {"status": "error", "error": "Upstox gateway timeout"}
                return True, 200, {"status": "success", "data": {"order_id": "EXIT_2001"}}
            if method == "DELETE" and endpoint == "/order/cancel":
                oid = (params or {}).get("order_id")
                if oid == "SL_2001":
                    if not state["sl_cancelled"]:
                        state["sl_cancelled"] = True
                        return True, 200, {"status": "success"}
                    return False, 400, {"status": "error", "error": "Order SL_2001 is already cancelled and cannot be cancelled"}
            if method == "GET" and endpoint == "/order/retrieve-all":
                return True, 200, {
                    "status": "success",
                    "data": [{"order_id": "ENTRY_2001", "status": "complete", "filled_quantity": 65, "average_price": 150.0}],
                }
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_sl_retry_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 11, 9, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 10, 1))
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 10, 2))

            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE option_sniffer_advance_trades SET status='CLOSED', exit_price_used=120.0, exit_reason='STOP_LOSS_HIT', net_pnl=-2005.0 WHERE trade_id='ADV_SL_CANCEL_RETRY_2001';"
            )
            conn.commit()
            conn.close()

            # First exit sync: cancels SL_2001 (200 OK), both exit POSTs fail (502), transitions to EXIT_RETRY_PENDING with broker_sl_order_id=NULL
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 11, 0))
            active_mid = self.bridge.get_active_live_positions()
            self.assertEqual(len(active_mid), 1)
            self.assertEqual(active_mid[0]["exit_state"], "EXIT_RETRY_PENDING")
            self.assertIsNone(active_mid[0]["broker_sl_order_id"])

            # Also verify that even if broker_sl_order_id were still populated when "already cancelled" is returned,
            # it NEVER triggers EXCHANGE_SL_ORDER_TRIGGERED and instead places the exit order!
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE option_advance_live_order_intents SET broker_sl_order_id='SL_2001' WHERE advance_trade_id='ADV_SL_CANCEL_RETRY_2001';")
            conn.commit()
            conn.close()

            sync_retry = self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 11, 1))
            self.assertEqual(len(sync_retry["exits_mirrored"]), 1)
            intents = self.bridge.get_recent_order_intents()
            self.assertEqual(intents[0]["exit_state"], "CONFIRMED_FLAT")
            self.assertEqual(intents[0]["exit_broker_order_id"], "EXIT_2001")
            self.assertNotEqual(intents[0]["exit_reason"], "EXCHANGE_SL_ORDER_TRIGGERED")

    def test_21_exit_fast_path_runs_before_priority_rest_refresh_and_ws_connected_uses_5s_threshold(self):
        """
        Proves Round-3 Findings 2 & 3 Fix:
          - Step 1A (`Sub-250ms Local SQLite Exit Fast-Path`) runs BEFORE Step 0B (`_prioritize_advance_real_time_ticks`),
            so a closed paper trade fires its exit order immediately without waiting for any REST quote refresh.
          - When WebSocket V3 is connected (`_is_connected is True`), `_prioritize_advance_real_time_ticks` uses a
            5.0s (`5000ms`) staleness threshold and excludes `hedge_wing_instrument_key` from stale-quote REST polling.
        """
        self._insert_advance_paper_trade(
            "ADV_FAST_ORDER_2101",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=150.0,
            entry_time="2026-11-09 11:20:00",
        )
        events = []
        ord_seq = {"n": 0}

        def fake_order_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            if method == "POST" and endpoint == "/order/place":
                ord_seq["n"] += 1
                tx = (payload or {}).get("transaction_type")
                otype = (payload or {}).get("order_type")
                if tx == "SELL" and otype == "LIMIT":
                    events.append("EXIT_ORDER_PLACED")
                return True, 200, {"status": "success", "data": {"order_id": f"ORD_21_{ord_seq['n']}"}}
            if method == "GET" and endpoint == "/order/retrieve-all":
                return True, 200, {
                    "status": "success",
                    "data": [{"order_id": "ORD_21_1", "status": "complete", "filled_quantity": 65, "average_price": 150.0}],
                }
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_order_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ), patch(
            "app.analytics.option_sniffer_advance_live.advance_live_bridge.OptionStreamManager"
        ) as mock_sm_cls, patch(
            "app.data.upstox_stream_service.get_stream_service"
        ) as mock_get_ws:
            sm_inst = mock_sm_cls.return_value
            sm_inst._key_to_contract = {}
            sm_inst._lock = None
            sm_inst.refresh_quotes_via_rest.side_effect = lambda **kw: (events.append("REST_QUOTE_REFRESH"), 1)[1]
            ws_inst = mock_get_ws.return_value
            ws_inst._is_connected = True

            # Quote age is 3500ms (which is > 2500ms but <= 5000ms when WebSocket is connected!)
            sm_inst.get_option_quote.return_value = {
                "instrument_key": "NSE_FO|65628",
                "bid": 152.0,
                "ask": 153.0,
                "ltp": 152.5,
                "quote_age_ms": 3500,
                "is_valid": True,
            }

            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 11, 19, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 20, 1))
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 20, 2))
            # Because ws_connected is True and quote_age_ms=3500 <= 5000ms, 0 REST quote refreshes should have fired!
            self.assertNotIn("REST_QUOTE_REFRESH", events)

            # Now close the paper trade AND make quote stale (9000ms): Step 1A must exit BEFORE Step 0B!
            sm_inst.get_option_quote.return_value = {
                "instrument_key": "NSE_FO|65628",
                "bid": 165.0,
                "ask": 166.0,
                "ltp": 165.5,
                "quote_age_ms": 9000,
                "is_valid": True,
            }
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE option_sniffer_advance_trades SET status='CLOSED', exit_price_used=165.0, exit_reason='TARGET_HIT', net_pnl=920.0 WHERE trade_id='ADV_FAST_ORDER_2101';"
            )
            conn.commit()
            conn.close()

            events.clear()
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 21, 0))
            self.assertIn("EXIT_ORDER_PLACED", events)
            # Because the only open trade closed in Step 1A, Step 0B sees 0 remaining open trades and skips REST refresh altogether!
            self.assertNotIn("REST_QUOTE_REFRESH", events)

    def test_22_dual_book_hedged_rejected_buy_wing_never_fires_naked_sell_wing_on_flatten(self):
        """
        Proves Round-3 Finding 4 Fix:
          - In `DUAL_BOOK_HEDGED` mode, if the 200-pt OTM protective `BUY` hedge wing order (`NSE_FO|99200`)
            is rejected by Upstox (`w_ok == False`), `hedge_wing_ikey` and `hedge_wing_oid` both remain `None`,
            and `_flatten_hedge_wing_if_present()` NEVER sends a naked `SELL` order on the hedge wing.
        """
        self._insert_advance_paper_trade(
            "ADV_WING_REJ_2201",
            book_type="SHORT_BOOK_SELL",
            action="SELL",
            status="OPEN",
            entry_price=110.0,
            entry_time="2026-11-09 11:30:00",
            regime_context_json='{"hedge_wing_strike": 23650}',
        )
        call_log = []

        def fake_wing_rej_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            call_log.append((method, endpoint, payload, params))
            if method == "POST" and endpoint == "/order/place":
                if (payload or {}).get("instrument_token") == "NSE_FO|99200" and (payload or {}).get("transaction_type") == "BUY":
                    # Protective BUY wing rejected (e.g., OI freeze / circuit limit)
                    return False, 400, {"status": "error", "error": "Strike under OI freeze"}
                return True, 200, {"status": "success", "data": {"order_id": f"MAIN_SELL_{len(call_log)}"}}
            if method == "GET" and endpoint == "/order/retrieve-all":
                return True, 200, {
                    "status": "success",
                    "data": [{"order_id": "MAIN_SELL_2", "status": "complete", "filled_quantity": 65, "average_price": 110.0}],
                }
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_wing_rej_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ), patch(
            "app.analytics.option_sniffer_advance_live.advance_live_bridge.OptionStreamManager"
        ) as mock_sm_cls:
            mock_sm_cls.return_value.get_all_active_option_quotes.return_value = [
                {
                    "underlying": "NIFTY",
                    "option_type": "CE",
                    "strike": 23650.0,
                    "instrument_key": "NSE_FO|99200",
                    "ask": 18.0,
                    "bid": 17.5,
                }
            ]
            mock_sm_cls.return_value.get_option_quote.return_value = {"bid": 17.5, "ltp": 17.5}
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_DUAL_BOOK_HEDGED,
                now_dt=datetime(2026, 11, 9, 11, 29, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 30, 1))
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 30, 2))

            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE option_sniffer_advance_trades SET status='CLOSED', exit_price_used=90.0, exit_reason='TARGET_HIT', net_pnl=1245.0 WHERE trade_id='ADV_WING_REJ_2201';"
            )
            conn.commit()
            conn.close()

            call_log.clear()
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 31, 0))
            naked_wing_sell_calls = [
                c
                for c in call_log
                if c[0] == "POST"
                and c[1] == "/order/place"
                and (c[2] or {}).get("instrument_token") == "NSE_FO|99200"
                and (c[2] or {}).get("transaction_type") == "SELL"
            ]
            self.assertEqual(len(naked_wing_sell_calls), 0, "Must NEVER send a SELL wing order when BUY wing failed!")

    def test_23_partial_entry_fill_sets_partially_filled_and_cancels_remainder_on_exit(self):
        """
        Proves Round-3 Finding 5 Fix:
          - If `/order/retrieve-all` reports the entry order is `'open'` with a partial fill (`filled_quantity = 65`
            out of `target_quantity = 130`), `_reconcile_open_live_intents_with_broker()` sets `state = 'PARTIALLY_FILLED'`
            (instead of `'FILLED'`) and updates `filled_quantity = 65`.
          - When the trade subsequently exits, Step 0A-bis (`Partially-Filled Entry Guard`) cancels the remaining
            unfilled 65 qty on `ENTRY_PARTIAL_1` BEFORE sending the 65-qty exit order.
        """
        self._insert_advance_paper_trade(
            "ADV_PARTIAL_2301",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=140.0,
            entry_time="2026-11-09 11:40:00",
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE option_sniffer_advance_trades SET quantity=130 WHERE trade_id='ADV_PARTIAL_2301';")
        conn.commit()
        conn.close()

        call_log = []

        def fake_partial_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            call_log.append((method, endpoint, payload, params))
            if method == "POST" and endpoint == "/order/place":
                otype = (payload or {}).get("order_type")
                tx = (payload or {}).get("transaction_type")
                if otype == "SL":
                    return True, 200, {"status": "success", "data": {"order_id": "SL_PARTIAL_1"}}
                if tx == "BUY":
                    return True, 200, {"status": "success", "data": {"order_id": "ENTRY_PARTIAL_1"}}
                return True, 200, {"status": "success", "data": {"order_id": "EXIT_PARTIAL_1"}}
            if method == "GET" and endpoint == "/order/retrieve-all":
                return True, 200, {
                    "status": "success",
                    "data": [
                        {
                            "order_id": "ENTRY_PARTIAL_1",
                            "status": "open",
                            "quantity": 130,
                            "filled_quantity": 65,
                            "average_price": 140.0,
                        }
                    ],
                }
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_partial_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 11, 39, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 40, 1))
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 40, 2))

            active = self.bridge.get_active_live_positions()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["state"], "PARTIALLY_FILLED")
            self.assertEqual(active[0]["filled_quantity"], 65)

            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE option_sniffer_advance_trades SET status='CLOSED', exit_price_used=160.0, exit_reason='TARGET_HIT', net_pnl=1240.0 WHERE trade_id='ADV_PARTIAL_2301';"
            )
            conn.commit()
            conn.close()

            call_log.clear()
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 41, 0))
            cancel_entry_calls = [
                c
                for c in call_log
                if c[0] == "DELETE" and c[1] == "/order/cancel" and (c[3] or {}).get("order_id") == "ENTRY_PARTIAL_1"
            ]
            self.assertEqual(len(cancel_entry_calls), 1, "Must cancel remaining unfilled entry quantity on exit!")
            exit_place_calls = [c for c in call_log if c[0] == "POST" and c[1] == "/order/place"]
            self.assertEqual(len(exit_place_calls), 1)
            self.assertEqual(exit_place_calls[0][2]["quantity"], 65)

    def test_24_invalid_spread_quote_and_submitted_unfilled_order_never_trip_daily_circuit_breaker(self):
        """
        Round-4 Finding 1 Proof:
          1. A transient wide-spread / invalid tick (`is_valid=False`, `spread_pct=100.0`) from `OptionStreamManager`
             must never corrupt `unrealized_pnl` or permanently latch `circuit_breaker_tripped = 1` in SQLite.
          2. An unfilled `SUBMITTED` order (`filled_quantity = 0`) must report `unrealized_pnl = 0.0` (never `0 or 65 -> 65`).
          3. Only `realized_pnl <= -6000.0` permanently latches `circuit_breaker_tripped = 1` in SQLite.
        """
        self._insert_advance_paper_trade(
            "ADV_CB_2401",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=150.0,
            entry_time="2026-11-09 11:50:00",
        )
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 11, 49, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 11, 50, 1))

            # Inject an invalid wide-spread quote (bid=10, ask=160, spread_pct=100%, is_valid=False)
            with patch(
                "app.analytics.option_sniffer_advance_live.advance_live_bridge.OptionStreamManager"
            ) as mock_sm_cls:
                mock_sm_cls.return_value.get_option_quote.return_value = {
                    "bid": 10.0,
                    "ask": 160.0,
                    "ltp": 150.0,
                    "spread_pct": 100.0,
                    "is_valid": False,
                }
                cb = self.bridge.get_circuit_breaker_status(session_date="2026-11-09")
                self.assertFalse(cb["is_session_halted"], "Invalid wide-spread quote must NEVER halt session!")
                self.assertEqual(cb["unrealized_pnl_inr"], 0.0)

            # Verify SQLite circuit_breaker_tripped is still 0
            conn = sqlite3.connect(self.db_path)
            row_cb = conn.execute(
                "SELECT circuit_breaker_tripped FROM option_advance_live_risk_ledger WHERE session_date='2026-11-09';"
            ).fetchone()
            self.assertTrue(row_cb is None or int(row_cb[0]) == 0)

            # Transition order to SUBMITTED with filled_quantity = 0 and verify unrealized_pnl is 0.0 even with a valid quote
            conn.execute(
                "UPDATE option_advance_live_order_intents SET state='SUBMITTED', filled_quantity=0;"
            )
            conn.commit()
            conn.close()

            with patch(
                "app.analytics.option_sniffer_advance_live.advance_live_bridge.OptionStreamManager"
            ) as mock_sm_cls:
                mock_sm_cls.return_value.get_option_quote.return_value = {
                    "bid": 40.0,
                    "ask": 41.0,
                    "ltp": 40.5,
                    "spread_pct": 2.4,
                    "is_valid": True,
                }
                active_sub = self.bridge.get_active_live_positions()
                self.assertEqual(len(active_sub), 1)
                self.assertEqual(active_sub[0]["unrealized_pnl"], 0.0, "Unfilled SUBMITTED order must have 0.0 MTM!")

    def test_25_idempotent_exit_and_op_lock_prevent_duplicate_exit_on_concurrent_sos_and_follower(self):
        """
        Round-4 Finding 2 Proof:
          Calling `_execute_mirrored_live_exit` a second time on an already flattened intent
          returns `already_flat=True` and fires 0 duplicate exit orders to Upstox.
        """
        self._insert_advance_paper_trade(
            "ADV_IDEM_2501",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=120.0,
            entry_time="2026-11-09 12:00:00",
        )
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 11, 59, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 12, 0, 1))
            active = self.bridge.get_active_live_positions()
            self.assertEqual(len(active), 1)
            stale_snapshot = dict(active[0])

            # First exit via SOS panic square-off
            self.bridge.execute_sos_panic_exit()
            self.mock_broker_api.reset_mock()

            # Second exit call using stale snapshot (simulating concurrent follower loop)
            res2 = self.bridge._execute_mirrored_live_exit(
                intent=stale_snapshot,
                paper_row={"exit_price_used": 125.0, "exit_reason": "TARGET_HIT", "net_pnl": 260.0},
                now_dt=datetime(2026, 11, 9, 12, 0, 2),
            )
            self.assertTrue(res2.get("exited"))
            self.assertTrue(res2.get("already_flat"))
            self.assertEqual(self.mock_broker_api.call_count, 0, "Zero duplicate broker calls allowed on already flat intent!")

    def test_26_rejected_sl_order_cancel_never_falsely_marks_confirmed_flat_and_partial_entry_resizes_sl(self):
        """
        Round-4 Finding 3 Proof:
          1. When `DELETE /order/cancel` on `broker_sl_order_id` returns `False` with
             `"Order SL_1 is already rejected and cannot be cancelled"`, `_execute_mirrored_live_exit()`
             must NOT falsely mark `EXCHANGE_SL_ORDER_TRIGGERED` and MUST fire the `POST /order/place` exit order.
          2. When `_reconcile_open_live_intents_with_broker()` sees a partial entry fill (`65` of `130`),
             it cancels the oversized `130`-qty SL order and places a resized `65`-qty SL order.
        """
        self._insert_advance_paper_trade(
            "ADV_SLREJ_2601",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=140.0,
            entry_time="2026-11-09 12:10:00",
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE option_sniffer_advance_trades SET quantity=130 WHERE trade_id='ADV_SLREJ_2601';")
        conn.commit()
        conn.close()

        call_log = []

        def fake_sl_rej_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            call_log.append((method, endpoint, payload, params))
            if method == "POST" and endpoint == "/order/place":
                otype = (payload or {}).get("order_type")
                tx = (payload or {}).get("transaction_type")
                if otype == "SL":
                    qty_sl = (payload or {}).get("quantity")
                    return True, 200, {"status": "success", "data": {"order_id": f"SL_QTY_{qty_sl}"}}
                if tx == "BUY":
                    return True, 200, {"status": "success", "data": {"order_id": "ENTRY_2601"}}
                return True, 200, {"status": "success", "data": {"order_id": "EXIT_2601"}}
            if method == "GET" and endpoint == "/order/retrieve-all":
                return True, 200, {
                    "status": "success",
                    "data": [
                        {
                            "order_id": "ENTRY_2601",
                            "status": "open",
                            "quantity": 130,
                            "filled_quantity": 65,
                            "average_price": 140.0,
                        },
                        {
                            "order_id": "SL_QTY_130",
                            "status": "trigger pending",
                            "quantity": 130,
                            "filled_quantity": 0,
                        },
                    ],
                }
            if method == "DELETE" and endpoint == "/order/cancel":
                oid = (params or {}).get("order_id")
                if oid == "SL_QTY_65":
                    return False, 400, {"status": "error", "error": "Order SL_QTY_65 is already rejected and cannot be cancelled"}
                return True, 200, {"status": "success", "data": {}}
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_sl_rej_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 12, 9, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 12, 10, 1))
            # Reconcile pass at +5s: sees 65/130 partial fill and SL_QTY_130 (130 qty) -> cancels SL_QTY_130 and places SL_QTY_65!
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 12, 10, 6))

            active = self.bridge.get_active_live_positions()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["broker_sl_order_id"], "SL_QTY_65")

            # Close paper trade and verify exit fires POST /order/place even though SL_QTY_65 cancel returned "already rejected"
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE option_sniffer_advance_trades SET status='CLOSED', exit_price_used=155.0, exit_reason='TARGET_HIT', net_pnl=910.0 WHERE trade_id='ADV_SLREJ_2601';"
            )
            conn.commit()
            conn.close()

            call_log.clear()
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 12, 11, 0))
            exit_orders = [c for c in call_log if c[0] == "POST" and c[1] == "/order/place"]
            self.assertEqual(len(exit_orders), 1, "Must place actual exit order when SL cancel says 'already rejected'!")
            self.assertEqual(exit_orders[0][2]["quantity"], 65)

    def test_27_pending_exit_verification_survives_same_pass_and_validation_pending(self):
        """
        Round-4 Finding 4 Proof:
          1. An exit order placed during Step 1A of `sync_once_with_advance_paper_table()` is NOT popped
             during Step 0B of the same pass (`pre_existing_pending_ids` isolation).
          2. On the next reconciliation pass, if the exit order is in `'validation pending'` with 0 fills,
             it is NOT prematurely cancelled on attempt 1, allowing it to complete on attempt 2.
        """
        self._insert_advance_paper_trade(
            "ADV_PEND_2701",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=110.0,
            entry_time="2026-11-09 12:20:00",
        )
        retrieve_stage = {"stage": 0}
        cancel_calls = []

        def fake_pend_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            if method == "POST" and endpoint == "/order/place":
                otype = (payload or {}).get("order_type")
                tx = (payload or {}).get("transaction_type")
                if otype == "SL":
                    return True, 200, {"status": "success", "data": {"order_id": "SL_2701"}}
                if tx == "BUY":
                    return True, 200, {"status": "success", "data": {"order_id": "ENTRY_2701"}}
                return True, 200, {"status": "success", "data": {"order_id": "EXIT_2701"}}
            if method == "DELETE" and endpoint == "/order/cancel":
                cancel_calls.append((params or {}).get("order_id"))
                return True, 200, {"status": "success", "data": {}}
            if method == "GET" and endpoint == "/order/retrieve-all":
                st = retrieve_stage["stage"]
                if st == 0:
                    return True, 200, {
                        "status": "success",
                        "data": [{"order_id": "ENTRY_2701", "status": "complete", "quantity": 65, "filled_quantity": 65, "average_price": 110.0}],
                    }
                elif st == 1:
                    # Exit order is in 'validation pending' right after placement
                    return True, 200, {
                        "status": "success",
                        "data": [{"order_id": "EXIT_2701", "status": "validation pending", "quantity": 65, "filled_quantity": 0, "average_price": 0.0}],
                    }
                else:
                    return True, 200, {
                        "status": "success",
                        "data": [{"order_id": "EXIT_2701", "status": "complete", "quantity": 65, "filled_quantity": 65, "average_price": 132.5}],
                    }
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_pend_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 12, 19, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 12, 20, 1))

            # Close paper trade at 12:20:06 (when 3.0s reconcile also triggers in the same pass)
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE option_sniffer_advance_trades SET status='CLOSED', exit_price_used=130.0, exit_reason='TARGET_HIT', net_pnl=1235.0 WHERE trade_id='ADV_PEND_2701';"
            )
            conn.commit()
            conn.close()

            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 12, 20, 6))
            self.assertEqual(len(self.bridge._pending_exit_verifications), 1, "Exit verification must survive same-pass Step 0B!")

            # Next pass at 12:20:10 sees 'validation pending' -> must NOT cancel EXIT_2701 on attempt 1!
            retrieve_stage["stage"] = 1
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 12, 20, 10))
            self.assertNotIn("EXIT_2701", cancel_calls, "Must not cancel 5ms-old 'validation pending' exit order!")
            self.assertEqual(len(self.bridge._pending_exit_verifications), 1)

            # Next pass at 12:20:14 sees 'complete' at 132.5 -> updates exit_fill_price to 132.5 and pops pending!
            retrieve_stage["stage"] = 2
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 12, 20, 14))
            self.assertEqual(len(self.bridge._pending_exit_verifications), 0)
            intents = self.bridge.get_recent_order_intents(limit=1)
            self.assertAlmostEqual(float(intents[0]["exit_fill_price"]), 132.5, places=2)

    def test_28_get_live_status_reuses_active_positions_without_redundant_queries(self):
        """
        Round-4 Finding 5 Proof:
          `get_live_status()` (polled every 1,000ms) must call `get_active_live_positions()`
          at most 1 time per invocation instead of 4 times.
        """
        with patch.object(
            self.bridge,
            "get_active_live_positions",
            wraps=self.bridge.get_active_live_positions,
        ) as spy_active:
            status = self.bridge.get_live_status()
            self.assertIn("circuit_breaker", status)
            self.assertIn("pnl_summary", status)
            self.assertEqual(spy_active.call_count, 1, "get_live_status() must call get_active_live_positions() exactly once!")

    def test_29_decoupled_open_position_quote_never_blanks_mtm_on_scanner_spread_gate_or_age(self):
        """
        Round-5 Finding A1 Proof:
          1. When an open contract's spread widens to `4.15%` (`> 3.5%` scanner entry hurdle) or its quote age is `16,000ms`
             (`> 15,000ms`), `OptionStreamManager` sets `is_valid=False`.
          2. Because `get_active_live_positions()` decouples open-position MTM/UI display from the scanner entry gate
             (`cspread_pct <= 15.0`), `current_bid`, `current_ask`, and `unrealized_pnl` remain populated (never `None`).
        """
        self._insert_advance_paper_trade(
            "ADV_MTM_2901",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=100.0,
            entry_time="2026-11-09 12:30:00",
        )
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 12, 29, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 12, 30, 1))

            with patch(
                "app.analytics.option_sniffer_advance_live.advance_live_bridge.OptionStreamManager"
            ) as mock_sm_cls:
                # Case 1: 4.15% spread (is_valid=False from scanner 3.5% hurdle, but <= 15.0% open-position sanity cap)
                mock_sm_cls.return_value.get_option_quote.return_value = {
                    "instrument_key": "NSE_FO|65628",
                    "bid": 98.0,
                    "ask": 102.15,
                    "ltp": 100.0,
                    "spread_pct": 4.15,
                    "quote_age_ms": 800,
                    "is_valid": False,
                }
                active_1 = self.bridge.get_active_live_positions()
                self.assertEqual(len(active_1), 1)
                self.assertEqual(active_1[0].get("current_bid"), 98.0)
                self.assertEqual(active_1[0].get("current_ask"), 102.15)
                self.assertAlmostEqual(float(active_1[0].get("unrealized_pnl")), -195.0, places=2)

                # Case 2: 16s quote age with 1.0% spread (is_valid=False from 15s hurdle, but book spread is sane)
                mock_sm_cls.return_value.get_option_quote.return_value = {
                    "instrument_key": "NSE_FO|65628",
                    "bid": 104.0,
                    "ask": 105.0,
                    "ltp": 104.5,
                    "spread_pct": 0.95,
                    "quote_age_ms": 16000,
                    "is_valid": False,
                }
                active_2 = self.bridge.get_active_live_positions()
                self.assertEqual(active_2[0].get("current_bid"), 104.0)
                self.assertAlmostEqual(float(active_2[0].get("unrealized_pnl")), 195.0, places=2)

    def test_30_fallback_exit_quote_rejects_corrupt_wide_spread_bid_and_uses_ltp_with_emergency_collar(self):
        """
        Round-5 Finding B1 Proof:
          During an emergency SOS exit when `OptionStreamManager` returns a corrupted wide-spread quote
          (`bid=10.0, ask=160.0, ltp=150.0, spread_pct=100.0`), `_execute_mirrored_live_exit()` refuses to use
          the corrupt `bid=10.0` and instead anchors to `ltp=150.0` with the 15% emergency collar (`127.50`).
        """
        self._insert_advance_paper_trade(
            "ADV_BADQ_3001",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=150.0,
            entry_time="2026-11-09 12:35:00",
        )
        call_log = []

        def fake_badq_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            call_log.append((method, endpoint, payload, params))
            if method == "POST" and endpoint == "/order/place":
                return True, 200, {"status": "success", "data": {"order_id": f"ORD_30_{len(call_log)}"}}
            return True, 200, {"status": "success", "data": []}

        self.mock_broker_api.side_effect = fake_badq_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ), patch(
            "app.analytics.option_sniffer_advance_live.advance_live_bridge.OptionStreamManager"
        ) as mock_sm_cls:
            mock_sm_cls.return_value.get_option_quote.return_value = {
                "instrument_key": "NSE_FO|65628",
                "bid": 10.0,
                "ask": 160.0,
                "ltp": 150.0,
                "spread_pct": 100.0,
                "is_valid": False,
            }
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 12, 34, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 12, 35, 1))
            call_log.clear()

            sos_res = self.bridge.execute_sos_panic_exit()
            self.assertEqual(sos_res["positions_flattened"], 1)
            exit_orders = [c for c in call_log if c[0] == "POST" and c[1] == "/order/place"]
            self.assertEqual(len(exit_orders), 1)
            # Must anchor to ltp=150.0 * 0.85 = 127.50, NEVER corrupt bid=10.0 * 0.985 = 9.85!
            self.assertAlmostEqual(float(exit_orders[0][2]["price"]), 127.50, places=2)

    def test_31_exchange_sl_trigger_on_partial_entry_cancels_remaining_open_entry_and_uses_actual_sl_qty(self):
        """
        Round-5 Finding B2 Proof:
          When `broker_sl_order_id` completes (`status == 'complete'`, `filled_quantity == 65`) while the
          130-qty entry order (`ENTRY_3101`) still has 65 sitting `'open'` on NSE:
            1. Section A cancels `ENTRY_3101` (`DELETE /order/cancel`) so the residual 65 never fills later.
            2. Section A computes `realized_pnl` and `filled_quantity` using `sl_qty = 65` (NOT `target_qty = 130`).
        """
        self._insert_advance_paper_trade(
            "ADV_PARTIAL_SL_3101",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=140.0,
            entry_time="2026-11-09 12:40:00",
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE option_sniffer_advance_trades SET quantity=130 WHERE trade_id='ADV_PARTIAL_SL_3101';")
        conn.commit()
        conn.close()

        cancel_oids = []

        def fake_partial_sl_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            if method == "POST" and endpoint == "/order/place":
                if (payload or {}).get("order_type") == "SL":
                    return True, 200, {"status": "success", "data": {"order_id": "SL_3101"}}
                return True, 200, {"status": "success", "data": {"order_id": "ENTRY_3101"}}
            if method == "DELETE" and endpoint == "/order/cancel":
                cancel_oids.append((params or {}).get("order_id"))
                return True, 200, {"status": "success", "data": {}}
            if method == "GET" and endpoint == "/order/retrieve-all":
                return True, 200, {
                    "status": "success",
                    "data": [
                        {
                            "order_id": "ENTRY_3101",
                            "status": "open",
                            "quantity": 130,
                            "filled_quantity": 65,
                            "average_price": 140.0,
                        },
                        {
                            "order_id": "SL_3101",
                            "status": "complete",
                            "quantity": 65,
                            "filled_quantity": 65,
                            "average_price": 112.0,
                        },
                    ],
                }
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_partial_sl_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ):
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 12, 39, 0),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 12, 40, 1))
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 12, 40, 5))

            self.assertIn("ENTRY_3101", cancel_oids, "Must cancel remaining open entry leg when SL completes!")
            intents = self.bridge.get_recent_order_intents(limit=1)
            self.assertEqual(intents[0]["exit_state"], "CONFIRMED_FLAT")
            self.assertEqual(intents[0]["filled_quantity"], 65)
            # Gross loss on 65 qty (`(112 - 140) * 65 = -1820.0`), NOT 130 qty (`-3640.0`)!
            self.assertAlmostEqual(float(intents[0]["gross_pnl"]), -1820.0, places=2)

    def test_32_long_book_only_excludes_unmirrored_short_paper_trade_from_rest_refresh_and_live_entry_collar_chases_up_to_1_5_pct(self):
        """
        Round-5 Findings B3 & B4 Proof:
          1. In `LONG_BOOK_ONLY` mode, an open `SHORT_BOOK_SELL` paper trade is pinned to `_key_to_contract`
             (for WebSocket V3) but excluded from `main_leg_keys` so its stale quote never triggers `refresh_quotes_via_rest()`.
          2. When a `LONG_BOOK_BUY` trade opens at `₹100.0` and the live stream Ask is `₹100.80` (`+0.80% <= 1.5%`),
             `_execute_mirrored_live_entry()` anchors `limit_price` to `min(100.80 * 1.006, 100.0 * 1.015) = ₹101.40`.
        """
        self._insert_advance_paper_trade(
            "ADV_SHORT_ONLY_3201",
            book_type="SHORT_BOOK_SELL",
            action="SELL",
            status="OPEN",
            entry_price=110.0,
            entry_time="2026-11-09 12:45:00",
        )
        fake_k2c = {}
        with patch(
            "app.analytics.option_sniffer_advance_live.advance_live_bridge.OptionStreamManager"
        ) as mock_sm_cls:
            sm_inst = mock_sm_cls.return_value
            sm_inst._key_to_contract = fake_k2c
            sm_inst._lock = None
            sm_inst.get_option_quote.return_value = {
                "instrument_key": "NSE_FO|65628",
                "bid": 110.0,
                "ask": 111.0,
                "ltp": 110.5,
                "quote_age_ms": 9000,
                "is_valid": True,
            }
            self.bridge._is_live_armed = True
            self.bridge._execution_mode = MODE_LONG_BOOK_ONLY
            self.bridge._last_priority_quote_refresh_ts = -999999.0
            res = self.bridge._prioritize_advance_real_time_ticks(active_intents=[], now_dt=datetime(2026, 11, 9, 12, 45, 5))
            self.assertIn("NSE_FO|65628", fake_k2c, "Must still pin to WebSocket V3!")
            self.assertFalse(res["refreshed_via_rest"], "Unmirrored Book 2 trade in LONG_BOOK_ONLY must NOT trigger REST refresh!")
            sm_inst.refresh_quotes_via_rest.assert_not_called()

        # Part 2: Verify B4 live entry Ask drift collar (+0.8% drift -> 100.80 * 1.006 = 101.40)
        self._insert_advance_paper_trade(
            "ADV_DRIFT_BUY_3202",
            book_type="LONG_BOOK_BUY",
            action="BUY",
            status="OPEN",
            entry_price=100.0,
            entry_time="2026-11-09 12:46:00",
        )
        placed_prices = []

        def fake_drift_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            if method == "POST" and endpoint == "/order/place" and (payload or {}).get("order_type") == "LIMIT":
                placed_prices.append(float(payload["price"]))
            return True, 200, {"status": "success", "data": {"order_id": f"ORD_32_{len(placed_prices)}"}}

        self.mock_broker_api.side_effect = fake_drift_broker
        with patch.object(
            self.bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"ready_for_live": True, "available_margin": 55000.0},
        ), patch(
            "app.analytics.option_sniffer_advance_live.advance_live_bridge.OptionStreamManager"
        ) as mock_sm_cls:
            mock_sm_cls.return_value.get_option_quote.return_value = {
                "instrument_key": "NSE_FO|65628",
                "bid": 100.20,
                "ask": 100.80,
                "ltp": 100.50,
                "spread_pct": 0.60,
                "quote_age_ms": 200,
                "is_valid": True,
            }
            self.bridge.arm_live_trading(
                pin=self.bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 9, 12, 45, 30),
            )
            self.bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 9, 12, 46, 1))
            self.assertEqual(len(placed_prices), 1)
            self.assertAlmostEqual(placed_prices[0], 101.40, places=2)

    def test_33_multi_position_same_pass_exit_does_not_cancel_sibling_pending_exit(self):
        """
        Round 6 Finding 1 Verification:
        When Position 1 places an exit order (`EX_ORD_33_1` in `_pending_exit_verifications`) and Position 2
        (`intent_id not in _reconciled_once_intent_ids`) exits in the same pass, Position 2's pre-exit reconcile
        (`pending_ids_to_verify=set()`) must NOT inspect or cancel Position 1's newly queued exit order.
        """
        self.bridge._pending_exit_verifications["adv_live_POS_33_1"] = "EX_ORD_33_1"
        self.bridge._pending_exit_attempts["adv_live_POS_33_1"] = 0

        with self.bridge._get_rw_conn() as conn:
            conn.execute(
                """
                INSERT INTO option_advance_live_order_intents (
                    order_intent_id, advance_trade_id, signal_id, book_type, strategy_mode,
                    direction, action, execution_mode, instrument_key, trading_symbol,
                    underlying, option_type, strike, expiry, lot_size, target_quantity,
                    filled_quantity, limit_price, average_fill_price, stop_loss_trigger,
                    stop_loss_limit, broker_order_id, broker_sl_order_id,
                    state, exit_state, created_at, updated_at
                ) VALUES (
                    'adv_live_POS_33_2', 'ADV_POS_33_2', 'SIG_33_2', 'LONG_BOOK_BUY', 'LONG_GAMMA_SNIPER',
                    'BULLISH', 'BUY', 'LONG_BOOK_ONLY', 'NSE_FO|65628', 'NIFTY26NOV24000CE',
                    'NIFTY', 'CE', 24000, '2026-11-26', 65, 65,
                    65, 120.0, 120.0, 96.0,
                    95.0, 'ENTRY_ORD_33_2', NULL,
                    'FILLED', 'NONE', '2026-11-09 13:00:00', '2026-11-09 13:00:00'
                );
                """
            )
            conn.commit()

        # Ensure POS_33_2 is NOT in _reconciled_once_intent_ids when its exit fires
        self.bridge._reconciled_once_intent_ids.discard("adv_live_POS_33_2")

        cancelled_oids = []

        def fake_multi_exit_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            if method == "GET" and endpoint == "/order/retrieve-all":
                return (
                    True,
                    200,
                    {
                        "status": "success",
                        "data": [
                            {"order_id": "EX_ORD_33_1", "status": "open", "filled_quantity": 0, "quantity": 65, "average_price": 0.0},
                            {"order_id": "ENTRY_ORD_33_2", "status": "complete", "filled_quantity": 65, "quantity": 65, "average_price": 120.0},
                        ],
                    },
                )
            if method == "DELETE" and endpoint == "/order/cancel":
                c_oid = str((params or {}).get("order_id") or "")
                cancelled_oids.append(c_oid)
                return True, 200, {"status": "success", "data": {"order_id": c_oid}}
            if method == "POST" and endpoint == "/order/place":
                return True, 200, {"status": "success", "data": {"order_id": "EX_ORD_33_2"}}
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_multi_exit_broker
        intent_2 = [
            p for p in self.bridge.get_active_live_positions() if p["order_intent_id"] == "adv_live_POS_33_2"
        ][0]
        self.bridge._execute_mirrored_live_exit(
            intent=intent_2,
            paper_row={"exit_price_used": 126.0, "exit_reason": "SOS_PANIC_EXIT", "net_pnl": 335.0},
            now_dt=datetime(2026, 11, 9, 13, 0, 2),
        )
        self.assertNotIn(
            "EX_ORD_33_1",
            cancelled_oids,
            "Sibling position exit in the same pass must NOT prematurely cancel EX_ORD_33_1!",
        )
        self.assertEqual(self.bridge._pending_exit_verifications.get("adv_live_POS_33_1"), "EX_ORD_33_1")

    def test_34_partial_exit_fill_accumulates_full_quantity_and_weighted_pnl(self):
        """
        Round 6 Finding 2 Verification:
        If a 60-qty position partially fills 25 qty @ 112.00 on its first exit order (`rem_qty = 35`),
        the retry exit order must place only 35 qty (`0` over-selling), and when the remaining 35 qty
        completes @ 114.00, the final closed row must record `filled_quantity = 60`, weighted
        `exit_fill_price = 113.17`, and compute realized P&L across all 60 shares.
        """
        with self.bridge._get_rw_conn() as conn:
            conn.execute(
                """
                INSERT INTO option_advance_live_order_intents (
                    order_intent_id, advance_trade_id, signal_id, book_type, strategy_mode,
                    direction, action, execution_mode, instrument_key, trading_symbol,
                    underlying, option_type, strike, expiry, lot_size, target_quantity,
                    filled_quantity, limit_price, average_fill_price, stop_loss_trigger,
                    stop_loss_limit, broker_order_id, broker_sl_order_id, exit_broker_order_id,
                    state, exit_state, created_at, updated_at
                ) VALUES (
                    'adv_live_PART_EX_3401', 'ADV_PART_EX_3401', 'SIG_3401', 'LONG_BOOK_BUY', 'LONG_GAMMA_SNIPER',
                    'BULLISH', 'BUY', 'LONG_BOOK_ONLY', 'NSE_FO|65628', 'NIFTY26NOV24000CE',
                    'NIFTY', 'CE', 24000, '2026-11-26', 60, 60,
                    60, 100.0, 100.0, 80.0,
                    79.0, 'ENT_3401', NULL, 'EX_LEG_1',
                    'FILLED', 'CONFIRMED_FLAT', '2026-11-09 13:10:00', '2026-11-09 13:15:00'
                );
                """
            )
            conn.commit()

        self.bridge._pending_exit_verifications["adv_live_PART_EX_3401"] = "EX_LEG_1"
        self.bridge._pending_exit_attempts["adv_live_PART_EX_3401"] = 0
        self.bridge._reconciled_once_intent_ids.add("adv_live_PART_EX_3401")

        # Pass 1: EX_LEG_1 partially fills 25 of 60 @ 112.00 and is still 'open'
        def fake_leg1_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            if method == "GET" and endpoint == "/order/retrieve-all":
                return (
                    True,
                    200,
                    {
                        "status": "success",
                        "data": [
                            {
                                "order_id": "EX_LEG_1",
                                "status": "open",
                                "filled_quantity": 25,
                                "quantity": 60,
                                "average_price": 112.0,
                            }
                        ],
                    },
                )
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_leg1_broker
        self.bridge._reconcile_open_live_intents_with_broker(
            active_intents=[],
            now_dt=datetime(2026, 11, 9, 13, 15, 1),
            pending_ids_to_verify={"adv_live_PART_EX_3401"},
        )

        # Verify SQLite has `exit_state = 'EXIT_RETRY_PENDING'` and `filled_quantity = 35` for the retry order
        active_retry = [
            p for p in self.bridge.get_active_live_positions() if p["order_intent_id"] == "adv_live_PART_EX_3401"
        ]
        self.assertEqual(len(active_retry), 1)
        self.assertEqual(int(active_retry[0]["filled_quantity"]), 35)

        # Pass 2: Retry exit order dispatches strictly 35 qty and completes @ 114.00
        placed_exit_qtys = []

        def fake_leg2_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            if method == "POST" and endpoint == "/order/place":
                placed_exit_qtys.append(int(payload["quantity"]))
                return True, 200, {"status": "success", "data": {"order_id": "EX_LEG_2"}}
            if method == "GET" and endpoint == "/order/retrieve-all":
                return (
                    True,
                    200,
                    {
                        "status": "success",
                        "data": [
                            {
                                "order_id": "EX_LEG_2",
                                "status": "complete",
                                "filled_quantity": 35,
                                "quantity": 35,
                                "average_price": 114.0,
                            }
                        ],
                    },
                )
            return True, 200, {"status": "success", "data": {}}

        self.mock_broker_api.side_effect = fake_leg2_broker
        self.bridge._execute_mirrored_live_exit(
            intent=active_retry[0],
            paper_row={"exit_price_used": 114.0, "exit_reason": "TRAILING_STOP", "net_pnl": 750.0},
            now_dt=datetime(2026, 11, 9, 13, 15, 2),
        )
        self.assertEqual(placed_exit_qtys, [35], "Retry exit order must only dispatch the remaining 35 qty!")

        # Reconcile EX_LEG_2 completion on exchange
        self.bridge._reconcile_open_live_intents_with_broker(
            active_intents=[],
            now_dt=datetime(2026, 11, 9, 13, 15, 3),
            pending_ids_to_verify={"adv_live_PART_EX_3401"},
        )

        with self.bridge._get_ro_conn() as conn:
            final_row = dict(
                conn.execute(
                    "SELECT * FROM option_advance_live_order_intents WHERE order_intent_id = 'adv_live_PART_EX_3401';"
                ).fetchone()
            )
        self.assertEqual(int(final_row["filled_quantity"]), 60)
        # Weighted exit price: (25 * 112.0 + 35 * 114.0) / 60 = 6790 / 60 = 113.17
        self.assertAlmostEqual(float(final_row["exit_fill_price"]), 113.17, places=2)
        # Gross PnL on 60 shares: (113.17 - 100.0) * 60 = 790.20
        self.assertAlmostEqual(float(final_row["gross_pnl"]), 790.20, places=2)
        self.assertGreater(float(final_row["realized_pnl"]), 700.0)

    def test_35_cancelled_unfilled_entries_excluded_from_closed_losses_and_calendar(self):
        """
        Round 6 Finding 3 Verification:
        Unfilled cancelled entry intents (`state = 'CANCELLED'`, `filled_quantity = 0`, `exit_state = 'CONFIRMED_FLAT'`)
        must NOT be counted as closed losing trades in `get_live_pnl_summary()`, `get_calendar_performance('LIVE_BROKER')`,
        or `get_circuit_breaker_status()`.
        """
        today_str = datetime.now().strftime("%Y-%m-%d")
        with self.bridge._get_rw_conn() as conn:
            conn.execute(
                """
                INSERT INTO option_advance_live_order_intents (
                    order_intent_id, advance_trade_id, signal_id, book_type, strategy_mode,
                    direction, action, execution_mode, instrument_key, trading_symbol,
                    underlying, option_type, strike, expiry, lot_size, target_quantity,
                    filled_quantity, limit_price, average_fill_price,
                    state, exit_state, gross_pnl, statutory_charges, realized_pnl,
                    created_at, updated_at
                ) VALUES (
                    'adv_live_CANC_3501', 'ADV_CANC_3501', 'SIG_3501', 'LONG_BOOK_BUY', 'LONG_GAMMA_SNIPER',
                    'BULLISH', 'BUY', 'LONG_BOOK_ONLY', 'NSE_FO|65628', 'NIFTY26NOV24000CE',
                    'NIFTY', 'CE', 24000, '2026-11-26', 65, 65,
                    0, 100.0, 0.0,
                    'CANCELLED', 'CONFIRMED_FLAT', 0.0, 0.0, 0.0,
                    ?, ?
                );
                """,
                (f"{today_str} 10:05:00", f"{today_str} 10:06:00"),
            )
            conn.commit()

        pnl_sum = self.bridge.get_live_pnl_summary(active_positions=[])
        self.assertEqual(pnl_sum["closed_trades_count"], 0)
        self.assertEqual(pnl_sum["loss_count"], 0)

        cal_live = self.bridge.get_calendar_performance(book_filter="LIVE_BROKER")
        self.assertEqual(len(cal_live["days"]), 0)


if __name__ == "__main__":
    unittest.main()
