"""
Unit test suite verifying decoupled Gate 9 live vs paper capacity
and real-time Telegram alerts for Upstox live entries, rejections, and exits.
"""

import os
import tempfile
import sqlite3
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

from app.analytics.option_sniffer.option_constants import (
    STATE_QUALIFIED,
    STATE_ACTIVE,
    DIRECTION_BUY_CE,
)
from app.analytics.option_sniffer.option_db import (
    OptionDB,
    init_option_sniffer_tables,
)
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
from app.analytics.option_sniffer.option_live_adapter import OptionLiveAdapter
from app.analytics.option_sniffer.option_live_order_intent import OrderIntentEngine, STATE_VALIDATED


class TestOptionLiveDecoupling(unittest.TestCase):
    def setUp(self):
        OptionSignalEngine.reset_class_state()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self.db_patcher = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.db_patcher2 = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.db_patcher.start()
        self.db_patcher2.start()

        init_option_sniffer_tables()
        from app.analytics.option_sniffer.option_regime_gate import seed_mock_continuous_bars
        seed_mock_continuous_bars(self.db_path, ticker="NIFTY", num_bars=60, base_time=datetime(2026, 10, 13, 10, 15, 0))

        # Build mock model that always returns high conviction
        self.mock_model = MagicMock()
        self.mock_model.version = "1.0.0-TEST"
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.58,
            "expected_net_return_pct": 8.5,
        }

        self.engine = OptionSignalEngine(model=self.mock_model)

        self.contract = {
            "instrument_key": "NSE_FO|99001",
            "trading_symbol": "NIFTY 22500 CE 19 OCT 26",
            "underlying": "NIFTY",
            "strike": 22500,
            "option_type": "CE",
            "expiry": "2026-10-19",
            "lot_size": 65,
        }
        self.quote = {
            "bid": 100.0,
            "ask": 101.0,
            "ltp": 100.5,
            "spread_pct": 0.99,
            "quote_age_ms": 250,
            "oi": 500000,
            "volume": 200000,
            "delta": 0.50,
            "iv": 14.5,
            "quote_freshness": "REAL_TIME",
        }
        self.underlying_quote = {
            "spot_ltp": 22510.0,
            "quote_age_ms": 200,
        }

    def tearDown(self):
        OptionSignalEngine.reset_class_state()
        self.db_patcher.stop()
        self.db_patcher2.stop()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def _fill_paper_trades(self, count=3):
        for i in range(count):
            OptionDB.create_paper_trade({
                "trade_id": f"pt_test_{i}",
                "signal_id": f"sig_{i}",
                "instrument_key": f"NSE_FO|9900{i}",
                "trading_symbol": f"NIFTY 22{i}00 CE",
                "underlying": "NIFTY",
                "strike": 22000 + i * 100,
                "option_type": "CE",
                "expiry": "2026-10-19",
                "lot_size": 65,
                "quantity": 65,
                "entry_time": datetime.now().isoformat(),
                "entry_bid": 99.0,
                "entry_ask": 100.0,
                "peak_price": 100.0,
                "trough_price": 100.0,
                "model_version": "1.0",
                "signal_version": "1.0",
                "data_freshness": "REAL_TIME",
                "engine_version": "V2_DYNAMIC",
                "dynamic_sl_pct": 15.0,
                "dynamic_target_pct": 25.0,
                "spot_atr": 50.0,
                "delta_used": 0.50,
            })

    @patch("app.analytics.option_sniffer.option_signal_engine.send_telegram_message")
    @patch("app.analytics.option_sniffer.option_signal_engine.is_option_entry_window", return_value=True)
    @patch("app.analytics.option_sniffer.option_signal_engine.is_option_trading_window", return_value=True)
    def test_gate_9_paper_full_live_armed_activates(self, mock_tw, mock_ew, mock_tg):
        """When 3 paper trades exist but live trading is armed with 0 positions, signal activates."""
        self._fill_paper_trades(3)
        self.assertEqual(len(OptionDB.get_open_paper_trades()), 3)

        mock_adapter = MagicMock()
        mock_adapter._is_live_armed = True

        mock_live_engine = MagicMock()
        mock_live_engine.get_active_positions_count.return_value = 0

        with patch("app.analytics.option_sniffer.option_live_adapter.get_option_live_adapter", return_value=mock_adapter), \
             patch("app.analytics.option_sniffer.option_live_order_intent.get_order_intent_engine", return_value=mock_live_engine):
            sig = self.engine.evaluate_contract(
                contract=self.contract,
                quote=self.quote,
                underlying_quote=self.underlying_quote,
                as_of_time=datetime(2026, 10, 13, 10, 15, 0),
                enforce_cutoff=True,
            )

        self.assertEqual(sig["status"], STATE_ACTIVE)
        self.assertTrue(sig["hurdles_passed"])
        # Paper positions must not exceed 3
        self.assertEqual(len(OptionDB.get_open_paper_trades()), 3)
        mock_tg.assert_called_once()
        self.assertIn("LIVE & PAPER BUY", mock_tg.call_args[0][0])

    @patch("app.analytics.telegram_notifier.send_telegram_message")
    @patch("app.analytics.option_sniffer.option_signal_engine.is_option_entry_window", return_value=True)
    @patch("app.analytics.option_sniffer.option_signal_engine.is_option_trading_window", return_value=True)
    def test_gate_9_paper_full_live_not_armed_blocks(self, mock_tw, mock_ew, mock_tg):
        """When 3 paper trades exist and live trading is NOT armed, signal stays QUALIFIED."""
        self._fill_paper_trades(3)

        mock_adapter = MagicMock()
        mock_adapter._is_live_armed = False

        with patch("app.analytics.option_sniffer.option_live_adapter.get_option_live_adapter", return_value=mock_adapter):
            sig = self.engine.evaluate_contract(
                contract=self.contract,
                quote=self.quote,
                underlying_quote=self.underlying_quote,
                as_of_time=datetime(2026, 10, 13, 10, 15, 0),
                enforce_cutoff=True,
            )

        self.assertEqual(sig["status"], STATE_QUALIFIED)
        self.assertFalse(sig["hurdles_passed"])
        self.assertTrue(any("Max concurrent paper trades reached" in r for r in sig["rejection_reasons"]))
        mock_tg.assert_not_called()

    @patch("app.analytics.telegram_notifier.send_telegram_message")
    @patch("app.analytics.option_sniffer.option_signal_engine.is_option_entry_window", return_value=True)
    @patch("app.analytics.option_sniffer.option_signal_engine.is_option_trading_window", return_value=True)
    def test_gate_9_both_capacities_full_blocks(self, mock_tw, mock_ew, mock_tg):
        """When both 3 paper trades and 2 live positions are active, signal stays QUALIFIED."""
        self._fill_paper_trades(3)

        mock_adapter = MagicMock()
        mock_adapter._is_live_armed = True

        mock_live_engine = MagicMock()
        mock_live_engine.get_active_positions_count.return_value = 2

        with patch("app.analytics.option_sniffer.option_live_adapter.get_option_live_adapter", return_value=mock_adapter), \
             patch("app.analytics.option_sniffer.option_live_order_intent.get_order_intent_engine", return_value=mock_live_engine):
            sig = self.engine.evaluate_contract(
                contract=self.contract,
                quote=self.quote,
                underlying_quote=self.underlying_quote,
                as_of_time=datetime(2026, 10, 13, 10, 15, 0),
                enforce_cutoff=True,
            )

        self.assertEqual(sig["status"], STATE_QUALIFIED)
        self.assertFalse(sig["hurdles_passed"])
        self.assertTrue(any("Max concurrent trades reached" in r for r in sig["rejection_reasons"]))
        mock_tg.assert_not_called()

    @patch("app.analytics.telegram_notifier.send_telegram_message")
    def test_live_autopilot_dispatch_telegram_alerts(self, mock_tg):
        """Verifies Telegram alerts on live order submission and rejection."""
        live_engine = OrderIntentEngine()

        cand = {
            "signal_id": "sig_live_test_1",
            "instrument_key": "NSE_FO|99001",
            "trading_symbol": "NIFTY 22500 CE 19 OCT 26",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "status": "ACTIVE",
            "calibrated_prob": 0.58,
            "expected_net_return_pct": 8.5,
            "created_at": datetime.now().isoformat(),
        }
        live_quote = {
            "bid": 100.0,
            "ask": 101.0,
            "ltp": 100.5,
            "spread_pct": 0.99,
            "quote_age_ms": 250,
        }

        mock_adapter = MagicMock()
        mock_adapter._is_live_armed = True
        mock_adapter.get_read_only_recent_signals.return_value = [cand]
        mock_adapter.fetch_account_readiness.return_value = {
            "live_trading_eligible": True,
            "available_margin": 30000.0,
        }

        with patch("app.analytics.option_sniffer.option_live_adapter.get_option_live_adapter", return_value=mock_adapter), \
             patch("app.analytics.option_sniffer.option_live_order_intent.is_option_entry_window", return_value=True), \
             patch("app.analytics.option_sniffer.option_live_order_intent.is_live_option_entry_window", return_value=True), \
             patch.object(live_engine, "reconcile_broker_position", return_value={"status": "OK", "quantity": 0, "pnl": 0.0}), \
             patch.object(live_engine, "place_real_entry_order", return_value={"status": "SUBMITTED", "broker_order_id": "2610130001", "order_intent_id": "INTENT_1"}):
            dispatched = live_engine.check_and_dispatch_automated_entries(
                live_quotes_map={"NSE_FO|99001": live_quote},
                deployable_capital=30000.0,
            )

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["status"], "SUBMITTED")
        mock_tg.assert_called_once()
        self.assertIn("UPSTOX LIVE ORDER SUBMITTED", mock_tg.call_args[0][0])
        self.assertIn("2610130001", mock_tg.call_args[0][0])

    def test_live_pnl_summary_includes_instrument_key_and_mtm(self):
        """Verifies get_active_live_positions includes instrument_key and get_live_pnl_summary computes live MTM."""
        with patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path), \
             patch("app.analytics.option_sniffer.option_live_adapter.get_db_path", return_value=self.db_path):
            live_engine = OrderIntentEngine()

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO option_live_order_intents (
                        order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action,
                        lot_size, target_quantity, filled_quantity, limit_price, average_fill_price,
                        state, broker_order_id, exit_state, created_at, updated_at
                    ) VALUES (
                        'INTENT_MTM_1', 'sig_mtm_1', 'NSE_FO|47133', 'NIFTY 22500 CE 19 OCT 26', 'NIFTY', 'BUY_CE',
                        65, 65, 65, 200.0, 200.0,
                        'FILLED', '261013000135035', 'NONE', ?, ?
                    );
                """, (now_str, now_str))
                conn.commit()

            adapter = OptionLiveAdapter()

            mock_stream = MagicMock()
            mock_stream.get_option_quote.return_value = {"bid": 210.0, "ltp": 211.0}

            with patch("app.analytics.option_sniffer.option_stream_manager.OptionStreamManager", return_value=mock_stream):
                active = adapter.get_active_live_positions()
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["instrument_key"], "NSE_FO|47133")
                self.assertEqual(active[0]["current_bid"], 210.0)
                # (210 - 200) * 65 - 55 = 650 - 55 = 595.0
                self.assertEqual(active[0]["unrealized_pnl"], 595.0)

                pnl = adapter.get_live_pnl_summary()
                self.assertEqual(pnl["unrealized_net_pnl"], 595.0)
                self.assertEqual(pnl["total_net_pnl"], 595.0)
                self.assertEqual(pnl["active_positions_count"], 1)

    def test_zero_lookback_blocks_signals_created_before_arming(self):
        """Zero Lookback Gate: Signals created before the user armed live trading are strictly ignored."""
        with patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path), \
             patch("app.analytics.option_sniffer.option_live_adapter.get_db_path", return_value=self.db_path):
            live_engine = OrderIntentEngine()
            adapter = OptionLiveAdapter()
            adapter._is_live_armed = True
            # User armed live trading at 10:30:00
            adapter._armed_at_iso = "2026-10-13 10:30:00"

            # Signal was generated at 10:29:30 (before arming)
            pre_arm_signal = {
                "signal_id": "sig_pre_arm_01",
                "instrument_key": "NSE_FO|47133",
                "trading_symbol": "NIFTY 22500 CE 19 OCT 26",
                "underlying": "NIFTY",
                "action": "BUY_CE",
                "status": "ACTIVE",
                "calibrated_prob": 0.65,
                "expected_net_return_pct": 8.0,
                "timestamp": "2026-10-13 10:29:30",
                "created_at": "2026-10-13 10:29:30",
                "suggested_entry": 200.0,
                "option_ask": 200.0,
                "option_bid": 199.0,
            }

            mock_adapter = MagicMock()
            mock_adapter._is_live_armed = True
            mock_adapter._armed_at_iso = "2026-10-13 10:30:00"
            mock_adapter.get_read_only_recent_signals.return_value = [pre_arm_signal]

            with patch("app.analytics.option_sniffer.option_live_adapter.get_option_live_adapter", return_value=mock_adapter), \
                 patch("app.analytics.option_sniffer.option_live_order_intent.is_option_entry_window", return_value=True):
                dispatched = live_engine.check_and_dispatch_automated_entries(
                    live_quotes_map={"NSE_FO|47133": {"ask": 200.0, "bid": 199.0, "quote_age_ms": 100}},
                    deployable_capital=30000.0
                )
                # Must be 0 because signal was created BEFORE arming!
                self.assertEqual(len(dispatched), 0)

    def test_single_underlying_blocks_duplicate_nifty_live_orders(self):
        """Single Underlying Guard: Never open 2 concurrent live positions on the same underlying index."""
        with patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path), \
             patch("app.analytics.option_sniffer.option_live_adapter.get_db_path", return_value=self.db_path):
            live_engine = OrderIntentEngine()

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Existing active live position for NIFTY
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO option_live_order_intents (
                        order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action,
                        lot_size, target_quantity, filled_quantity, limit_price, average_fill_price,
                        state, broker_order_id, exit_state, created_at, updated_at
                    ) VALUES (
                        'INTENT_NIFTY_1', 'sig_1', 'NSE_FO|47133', 'NIFTY 22500 CE 19 OCT 26', 'NIFTY', 'BUY_CE',
                        65, 65, 65, 200.0, 200.0,
                        'FILLED', '261013000135035', 'NONE', ?, ?
                    );
                """, (now_str, now_str))
                conn.commit()

            # Second signal for a different strike on NIFTY
            second_signal = {
                "signal_id": "sig_nifty_2",
                "instrument_key": "NSE_FO|47153",
                "trading_symbol": "NIFTY 22800 CE 19 OCT 26",
                "underlying": "NIFTY",
                "action": "BUY_CE",
                "status": "ACTIVE",
                "calibrated_prob": 0.65,
                "expected_net_return_pct": 8.0,
                "suggested_entry": 65.0,
                "option_ask": 65.0,
                "option_bid": 64.0,
            }

            ok, reason, intent = live_engine.create_and_validate_intent(
                signal_payload=second_signal,
                live_quote={"ask": 65.0, "bid": 64.0, "quote_age_ms": 100},
                deployable_capital=30000.0
            )
            self.assertFalse(ok)
            self.assertIn("UNDERLYING_EXPOSURE_CAPPED", reason)

    def test_telegram_silence_for_paper_signals(self):
        """Verifies send_telegram_message silences non-real signals and reports."""
        from app.analytics.telegram_notifier import send_telegram_message, send_telegram_document
        with patch("app.data.database.is_testing_environment", return_value=False), \
             patch.dict("os.environ", {"SUPPRESS_TELEGRAM": "false"}):
            # Paper signal alert -> MUST BE SUPPRESSED (returns False)
            paper_msg = "<b>📡 OPTION SNIFFER | PAPER BUY</b>\nNIFTY 22500 CE\n⚠️ PAPER ONLY"
            res_paper = send_telegram_message(paper_msg, is_real_option_trade=False)
            self.assertFalse(res_paper)

            # Scanner alert -> MUST BE SUPPRESSED
            scan_msg = "AUTOPILOT SCANNER: Found RELIANCE"
            res_scan = send_telegram_message(scan_msg, is_real_option_trade=False)
            self.assertFalse(res_scan)

            # PDF document -> MUST BE SUPPRESSED
            res_doc = send_telegram_document(b"fake_pdf", "report.pdf", is_real_option_trade=False)
            self.assertFalse(res_doc)


if __name__ == "__main__":
    unittest.main()
