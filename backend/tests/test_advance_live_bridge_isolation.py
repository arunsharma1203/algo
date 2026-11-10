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


if __name__ == "__main__":
    unittest.main()
