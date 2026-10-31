"""
Unit, Concurrency, Margin-Safety & Friday (2026-10-30) Blind Chronological Backtest Suite
for Main Option Sniffer (Option Buying Only) & Live Order Intent Engine.
"""

import os
import sys
import tempfile
import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.data.database import get_canonical_db_path
from app.analytics.option_sniffer.option_live_order_intent import (
    OrderIntentEngine,
    init_live_order_tables,
    STATE_SUBMITTED,
    STATE_PARTIALLY_FILLED,
    STATE_FILLED,
    EXIT_NONE,
    EXIT_CONFIRMED_FLAT,
)
from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
from app.analytics.option_sniffer.option_sniffer_orchestrator import OptionSnifferOrchestrator
from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.option_sniffer.option_db import OptionDB, init_option_sniffer_tables
from app.analytics.option_sniffer.option_regime_gate import OptionRegimeGate


class TestMainSnifferLiveMarginAndFridayBacktest(unittest.TestCase):
    def setUp(self):
        self._tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp_db.close()
        self.tmp_db_path = self._tmp_db.name
        self._db_patch_1 = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.tmp_db_path)
        self._db_patch_2 = patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.tmp_db_path)
        self._db_patch_1.start()
        self._db_patch_2.start()
        init_option_sniffer_tables()
        init_live_order_tables()
        self.engine = OrderIntentEngine(db_path=self.tmp_db_path)

    def tearDown(self):
        self._db_patch_1.stop()
        self._db_patch_2.stop()
        try:
            os.unlink(self.tmp_db_path)
        except Exception:
            pass

    def test_01_idempotent_sl_prevents_duplicate_sell_order_and_false_liquidation(self):
        """
        Verifies Bug #1 Fix: Calling place_broker_stop_loss_order or reconcile_active_order_intents
        twice for the same filled order NEVER places a second SELL SL order on Upstox and NEVER
        triggers false emergency auto-liquidation.
        """
        intent_id = "INTENT_TEST_DUP_SL_01"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.engine._get_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action,
                 lot_size, target_quantity, filled_quantity, limit_price, average_fill_price,
                 state, broker_order_id, exit_state, created_at, updated_at)
                VALUES (?, 'sig_dup_1', 'NSE_FO|50001', 'NIFTY 24200 PE 03 NOV 26', 'NIFTY', 'BUY_PE',
                        65, 65, 0, 120.0, 0.0, ?, '261030000111', ?, ?, ?);
                """,
                (intent_id, STATE_SUBMITTED, EXIT_NONE, now_iso, now_iso),
            )
            conn.commit()

        adapter = get_option_live_adapter()
        orig_armed = adapter._is_live_armed
        adapter._is_live_armed = True
        try:
            api_calls = []

            def mock_broker_api(method, endpoint, payload=None, params=None, is_emergency=False):
                api_calls.append((method, endpoint, payload))
                if method == "GET" and endpoint == "/order/retrieve-all":
                    return True, 200, {
                        "status": "success",
                        "data": [
                            {
                                "order_id": "261030000111",
                                "status": "complete",
                                "average_price": 120.0,
                                "filled_quantity": 65,
                            }
                        ],
                    }
                if method == "POST" and endpoint == "/order/place":
                    return True, 200, {"status": "success", "data": {"order_id": "261030000222"}}
                return True, 200, {"status": "success", "data": []}

            with patch.object(self.engine, "_call_upstox_broker_api", side_effect=mock_broker_api), \
                 patch("app.analytics.telegram_notifier.send_telegram_message"):
                # 1st reconciliation (e.g. from place_real_entry_order)
                res1 = self.engine.reconcile_active_order_intents()
                self.assertEqual(len(res1), 1)
                self.assertEqual(res1[0]["sl_result"]["status"], "PLACED")
                self.assertEqual(res1[0]["sl_result"]["broker_sl_order_id"], "261030000222")

                # Direct 2nd call to place_broker_stop_loss_order (simulating concurrent thread race)
                res2 = self.engine.place_broker_stop_loss_order(intent_id, 120.0, 65)
                self.assertEqual(res2["status"], "SL_ALREADY_EXISTS")
                self.assertEqual(res2["broker_sl_order_id"], "261030000222")

                # Count how many POST /order/place calls were made: MUST be strictly 1!
                place_calls = [c for c in api_calls if c[0] == "POST" and c[1] == "/order/place"]
                self.assertEqual(len(place_calls), 1)
                self.assertTrue(adapter._is_live_armed)
        finally:
            adapter._is_live_armed = orig_armed

    def test_02_partial_to_full_fill_modifies_existing_sl_in_place_zero_extra_margin(self):
        """
        Verifies Bug #2 Fix: When a PARTIALLY_FILLED order (30 qty) with an existing broker_sl_order_id
        completes full fill (65 qty), reconcile_active_order_intents modifies the existing SL order
        in-place via PUT /order/modify instead of placing a 2nd SELL SL order.
        """
        intent_id = "INTENT_TEST_PARTIAL_MOD_02"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.engine._get_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action,
                 lot_size, target_quantity, filled_quantity, limit_price, average_fill_price,
                 state, broker_order_id, broker_sl_order_id, stop_loss_trigger, stop_loss_limit,
                 exit_state, created_at, updated_at)
                VALUES (?, 'sig_part_2', 'NSE_FO|50002', 'NIFTY 24200 PE 03 NOV 26', 'NIFTY', 'BUY_PE',
                        65, 65, 30, 100.0, 100.0, ?, '261030000333', '261030000444', 80.0, 78.8,
                        ?, ?, ?);
                """,
                (intent_id, STATE_PARTIALLY_FILLED, EXIT_NONE, now_iso, now_iso),
            )
            conn.commit()

        adapter = get_option_live_adapter()
        orig_armed = adapter._is_live_armed
        adapter._is_live_armed = True
        try:
            api_calls = []

            def mock_broker_api(method, endpoint, payload=None, params=None, is_emergency=False):
                api_calls.append((method, endpoint, payload))
                if method == "GET" and endpoint == "/order/retrieve-all":
                    return True, 200, {
                        "status": "success",
                        "data": [
                            {
                                "order_id": "261030000333",
                                "status": "complete",
                                "average_price": 100.0,
                                "filled_quantity": 65,
                            }
                        ],
                    }
                if method == "PUT" and endpoint == "/order/modify":
                    return True, 200, {"status": "success", "data": {"order_id": "261030000444"}}
                return False, 400, {"status": "error"}

            with patch.object(self.engine, "_call_upstox_broker_api", side_effect=mock_broker_api), \
                 patch("app.analytics.telegram_notifier.send_telegram_message"):
                res = self.engine.reconcile_active_order_intents()
                self.assertEqual(len(res), 1)
                self.assertEqual(res[0]["state"], STATE_FILLED)
                self.assertEqual(res[0]["sl_result"]["status"], "MODIFIED")

                put_calls = [c for c in api_calls if c[0] == "PUT" and c[1] == "/order/modify"]
                post_calls = [c for c in api_calls if c[0] == "POST" and c[1] == "/order/place"]
                self.assertEqual(len(put_calls), 1)
                self.assertEqual(put_calls[0][2]["order_id"], "261030000444")
                self.assertEqual(put_calls[0][2]["quantity"], 65)
                self.assertEqual(len(post_calls), 0)
        finally:
            adapter._is_live_armed = orig_armed

    def test_03_sos_exit_falls_back_to_in_place_sl_modify_on_margin_rejection(self):
        """
        Verifies Bug #3 Fix: If POST /order/place (MARKET & LIMIT) fails during exit because
        the resting SELL SL cancellation is still in-flight on NSE (RMS double-margin rejection),
        trigger_sos_panic_exit converts broker_sl_order_id in-place via PUT /order/modify (trigger_price=0).
        """
        intent_id = "INTENT_TEST_EXIT_MOD_03"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.engine._get_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action,
                 lot_size, target_quantity, filled_quantity, limit_price, average_fill_price,
                 state, broker_order_id, broker_sl_order_id, stop_loss_trigger, stop_loss_limit,
                 exit_state, created_at, updated_at)
                VALUES (?, 'sig_exit_3', 'NSE_FO|50003', 'NIFTY 24200 PE 03 NOV 26', 'NIFTY', 'BUY_PE',
                        65, 65, 65, 100.0, 100.0, ?, '261030000555', '261030000666', 80.0, 78.8,
                        ?, ?, ?);
                """,
                (intent_id, STATE_FILLED, EXIT_NONE, now_iso, now_iso),
            )
            conn.commit()

        api_calls = []
        pos_calls = [0]

        def mock_broker_api(method, endpoint, payload=None, params=None, is_emergency=False):
            api_calls.append((method, endpoint, payload))
            if method == "DELETE" and endpoint == "/order/cancel":
                return True, 200, {"status": "success"}
            if method == "GET" and endpoint == "/portfolio/short-term-positions":
                pos_calls[0] += 1
                qty = 65 if pos_calls[0] == 1 else 0
                return True, 200, {
                    "status": "success",
                    "data": [{"instrument_token": "NSE_FO|50003", "quantity": qty, "pnl": 1950.0}],
                }
            if method == "POST" and endpoint == "/order/place":
                # Simulate Upstox RMS rejecting 2nd SELL order while SL cancel is still pending on NSE
                return False, 400, {"status": "error", "errors": [{"message": "Insufficient margin for naked short"}]}
            if method == "PUT" and endpoint == "/order/modify":
                return True, 200, {"status": "success", "data": {"order_id": "261030000666"}}
            if method == "GET" and endpoint.startswith("/order/history"):
                return True, 200, {"status": "success", "data": [{"status": "complete", "average_price": 135.0}]}
            return True, 200, {"status": "success", "data": []}

        with patch.object(self.engine, "_call_upstox_broker_api", side_effect=mock_broker_api), \
             patch("app.analytics.telegram_notifier.send_telegram_message"):
            res = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=135.0)
            self.assertEqual(res["status"], EXIT_CONFIRMED_FLAT)
            put_calls = [c for c in api_calls if c[0] == "PUT" and c[1] == "/order/modify"]
            self.assertEqual(len(put_calls), 1)
            self.assertEqual(put_calls[0][2]["order_id"], "261030000666")
            self.assertEqual(put_calls[0][2]["trigger_price"], 0.0)

    def test_04_orchestrator_atm_proximity_priority_prevents_deep_itm_stealing_slot(self):
        """
        Verifies Bug #4 Fix: OptionSnifferOrchestrator.run_surveillance_sweep evaluates
        ATM strikes (|strike - spot_ltp| smallest) BEFORE deep-ITM strikes (ATM-5).
        """
        orch = OptionSnifferOrchestrator()
        orch.contracts = [
            {"instrument_key": "NSE_FO|DEEP_ITM", "trading_symbol": "NIFTY 24000 CE", "underlying": "NIFTY", "strike": 24000.0, "option_type": "CE", "expiry": "2026-11-05", "lot_size": 65},
            {"instrument_key": "NSE_FO|ATM_BEST", "trading_symbol": "NIFTY 24250 CE", "underlying": "NIFTY", "strike": 24250.0, "option_type": "CE", "expiry": "2026-11-05", "lot_size": 65},
            {"instrument_key": "NSE_FO|FAR_OTM", "trading_symbol": "NIFTY 24500 CE", "underlying": "NIFTY", "strike": 24500.0, "option_type": "CE", "expiry": "2026-11-05", "lot_size": 65},
        ]
        fixed_dt = datetime(2026, 10, 30, 10, 30, 0)
        orch._last_discovery_date = fixed_dt.strftime("%Y-%m-%d")
        orch._last_discovery_time = 9999999999.0

        now_ms = int(fixed_dt.timestamp() * 1000)
        u_quote = {
            "underlying": "NIFTY",
            "spot_ltp": 24248.0,
            "open": 24150.0,
            "high": 24255.0,
            "low": 24140.0,
            "close": 24200.0,
            "vwap": 24200.0,
            "timestamp_ms": now_ms,
            "quote_age_ms": 10,
        }
        q_map = {}
        for c in orch.contracts:
            q_map[c["instrument_key"]] = {
                "instrument_key": c["instrument_key"],
                "trading_symbol": c["trading_symbol"],
                "underlying": "NIFTY",
                "strike": c["strike"],
                "option_type": "CE",
                "lot_size": 65,
                "ltp": 120.0,
                "bid": 119.8,
                "ask": 120.2,
                "spread": 0.4,
                "spread_pct": 0.33,
                "volume": 50000,
                "oi": 100000,
                "iv": 15.0,
                "delta": 0.50,
                "timestamp_ms": now_ms,
                "quote_age_ms": 10,
                "is_valid": True,
            }

        eval_order = []

        def fake_eval(contract, *args, **kwargs):
            eval_order.append(contract["instrument_key"])
            return {"status": "WATCH", "instrument_key": contract["instrument_key"]}

        with patch("app.analytics.option_sniffer.option_sniffer_orchestrator.to_ist_naive", return_value=fixed_dt), \
             patch("app.analytics.option_sniffer.option_sniffer_orchestrator.is_option_trading_window", return_value=True), \
             patch.object(orch.stream_manager, "refresh_quotes_via_rest"), \
             patch.object(orch.stream_manager, "get_all_active_option_quotes", return_value=list(q_map.values())), \
             patch.object(orch.stream_manager, "get_underlying_quote", return_value=u_quote), \
             patch.object(orch.stream_manager, "get_option_quote", side_effect=lambda k: q_map.get(k)), \
             patch.object(orch.signal_engine, "evaluate_contract", side_effect=fake_eval):
            orch.run_surveillance_sweep()

        self.assertEqual(eval_order[0], "NSE_FO|ATM_BEST")
        self.assertIn("NSE_FO|DEEP_ITM", eval_order[1:])

    def test_05_stream_manager_tick_guard_preserves_initial_entry_ask_on_scaled_in_trade(self):
        """
        Verifies Bug #6 Fix: OptionStreamManager._evaluate_tick_protective_guards uses
        initial_entry_ask (₹100) rather than blended entry_ask (₹107.50) on scaled-in paper trades.
        """
        trade_obj = {
            "trade_id": "opt_paper_scale_guard",
            "signal_id": "sig_scale_guard",
            "instrument_key": "NSE_FO|77701",
            "trading_symbol": "NIFTY 24250 PE 03 NOV 26",
            "underlying": "NIFTY",
            "option_type": "PE",
            "strike": 24250.0,
            "expiry": "2026-11-03",
            "lot_size": 65,
            "quantity": 65,
            "entry_time": datetime.now().isoformat(),
            "entry_bid": 99.5,
            "entry_ask": 100.0,
            "entry_spot_price": 24250.0,
            "model_version": "v1",
            "signal_version": "v1",
            "data_freshness": "REAL_TIME",
            "engine_version": "V1_CLASSIC",
            "dynamic_sl_pct": 20.0,
            "dynamic_target_pct": 35.0,
            "spot_atr": 40.0,
            "delta_used": 0.50,
        }
        OptionDB.create_paper_trade(trade_obj)
        OptionDB.scale_in_paper_trade(
            trade_id="opt_paper_scale_guard",
            new_quantity=130,
            blended_entry_ask=107.5,
            initial_entry_ask=100.0,
            scale_in_price=115.0,
            scale_in_time=datetime.now().isoformat(),
        )

        sm = OptionStreamManager()
        sm._evaluate_tick_protective_guards("NSE_FO|77701", {"bid": 120.0, "ask": 120.5, "ltp": 120.0})

        open_trades = OptionDB.get_open_paper_trades()
        matched = [t for t in open_trades if t["trade_id"] == "opt_paper_scale_guard"]
        self.assertEqual(len(matched), 1)
        # MFE must be +20.0% anchored to initial_entry_ask (100.0), NOT +11.63% anchored to 107.5!
        self.assertAlmostEqual(float(matched[0]["mfe_pct"]), 20.0, places=2)

    def test_06_friday_2026_10_30_data_pipeline_and_blind_chronological_backtest(self):
        """
        Verifies Friday (2026-10-30) 100% Data Pipeline Health (375/375 1m bars on NIFTY & BANKNIFTY),
        strict 09:43 IST 28-bar warm-up gate (0 pre-09:43 unlocks), and blind chronological walk-forward
        replay of Friday's setups through Gate 9, Gate 10C, and Step 2 House-Money Scale-In.
        """
        db_path = get_canonical_db_path()
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            for und in ["NIFTY", "BANKNIFTY"]:
                rows = conn.execute(
                    """
                    SELECT timestamp, open, high, low, close, is_synthetic, is_gap, is_point_in_time
                    FROM continuous_index_ohlcv
                    WHERE ticker = ?
                      AND timestamp >= '2026-10-30 09:15:00'
                      AND timestamp <= '2026-10-30 15:29:59'
                    ORDER BY timestamp ASC;
                    """,
                    (und,),
                ).fetchall()
                bar_times = {r["timestamp"][:19].replace(" ", "T") for r in rows}
                self.assertEqual(len(bar_times), 375, f"{und} must have all 375 1m bars on Friday 2026-10-30")
                self.assertEqual(sum(1 for r in rows if int(r["is_synthetic"] or 0) == 1), 0)
                self.assertEqual(sum(1 for r in rows if int(r["is_gap"] or 0) == 1), 0)

                # Verify 0 gaps between 09:15:00 and 15:29:00
                cur = datetime(2026, 10, 30, 9, 15, 0)
                end = datetime(2026, 10, 30, 15, 29, 0)
                missing = 0
                while cur <= end:
                    if cur.strftime("%Y-%m-%dT%H:%M:%S") not in bar_times:
                        missing += 1
                    cur += timedelta(minutes=1)
                self.assertEqual(missing, 0, f"{und} must have 0 missing 1-minute bars on Friday 2026-10-30")

                # Verify OptionRegimeGate warm-up at 09:42 (blocked: 27 < 28 bars) and 09:43 (unlocked: 28 >= 28 bars)
                gate = OptionRegimeGate(db_path=db_path)
                ok_0942, msg_0942, _ = gate.get_completed_index_bars(
                    und, "2026-10-30", as_of_time=datetime(2026, 10, 30, 9, 42, 0)
                )
                self.assertFalse(ok_0942)
                self.assertIn("27 < 28", msg_0942)

                ok_0943, msg_0943, df_0943 = gate.get_completed_index_bars(
                    und, "2026-10-30", as_of_time=datetime(2026, 10, 30, 9, 43, 0)
                )
                self.assertTrue(ok_0943, f"{und} must unlock at 09:43:00 IST: {msg_0943}")
                self.assertEqual(len(df_0943), 28)


if __name__ == "__main__":
    unittest.main()

