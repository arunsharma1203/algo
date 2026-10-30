"""
Test Suite: Option Canary Scout & Decoupled Gate 9B Interlocks
=============================================================
Verifies:
  1. Canary Scout Window:
     - Paper trading starts at 09:30 IST.
     - Live broker trading is strictly locked until 09:45 IST.
  2. Canary Scout Health Protection:
     - If paper trading experiences any Stop Loss hit or trips its circuit breaker,
       real trading is locked/blocked for the session.
  3. Decoupled Gate 9B:
     - Paper trading is ALWAYS capped by the Paper Circuit Breaker (-₹3,500).
     - Live trading arming does NOT leak or bypass paper circuit limits.
  4. Real Order Intent Interlocks:
     - place_real_entry_order and check_and_dispatch_automated_entries fail-closed
       before 09:45 IST and when Canary Scout is aborted.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, time
from unittest.mock import patch, MagicMock

from app.analytics.option_sniffer.option_constants import (
    is_option_entry_window,
    is_live_option_entry_window,
    LIVE_ENTRY_OPEN_HOUR,
    LIVE_ENTRY_OPEN_MINUTE,
    DIRECTION_BUY_CE,
    DIRECTION_BUY_PE,
)
from app.analytics.option_sniffer.option_db import (
    OptionDB,
    init_option_sniffer_tables,
)
from app.analytics.option_sniffer.option_live_adapter import (
    OptionLiveAdapter,
    get_option_live_adapter,
)
from app.analytics.option_sniffer.option_live_order_intent import (
    OrderIntentEngine,
    STATE_VALIDATED,
)
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine


class TestOptionCanaryScoutAndDecoupling(unittest.TestCase):
    def setUp(self):
        OptionSignalEngine.reset_class_state()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self.db_patcher1 = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.db_patcher2 = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.db_patcher1.start()
        self.db_patcher2.start()

        init_option_sniffer_tables()
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT);")
        conn.commit()
        conn.close()

        from app.analytics.option_sniffer.option_regime_gate import seed_mock_continuous_bars
        seed_mock_continuous_bars(self.db_path, ticker="NIFTY", num_bars=60, base_time=datetime(2026, 10, 19, 10, 15, 0))

        self.adapter = get_option_live_adapter()
        self.adapter._is_live_armed = False
        self.intent_engine = OrderIntentEngine(db_path=self.db_path)

        self.mock_model = MagicMock()
        self.mock_model.version = "1.0.0-TEST"
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.60,
            "expected_net_return_pct": 9.5,
        }
        self.signal_engine = OptionSignalEngine(model=self.mock_model)

        self.contract = {
            "instrument_key": "NSE_FO|47168",
            "trading_symbol": "NIFTY 23100 CE 19 OCT 26",
            "underlying": "NIFTY",
            "strike": 23100.0,
            "option_type": "CE",
            "expiry": "2026-10-19",
            "lot_size": 65,
        }
        self.quote = {
            "bid": 150.0,
            "ask": 151.0,
            "ltp": 150.5,
            "spread_pct": 0.66,
            "quote_age_ms": 150,
            "oi": 800000,
            "volume": 350000,
            "delta": 0.52,
            "iv": 13.8,
            "quote_freshness": "REAL_TIME",
        }
        self.underlying_quote = {
            "spot_ltp": 23105.0,
            "quote_age_ms": 100,
        }

    def tearDown(self):
        self.adapter._is_live_armed = False
        OptionSignalEngine.reset_class_state()
        self.db_patcher1.stop()
        self.db_patcher2.stop()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    # ──────────────────────────────────────────────────────────────────────────
    # 1. WINDOW VERIFICATION TESTS
    # ──────────────────────────────────────────────────────────────────────────
    def test_live_option_entry_window_gates(self):
        """Verifies paper window is open at 09:30, but live window is strictly locked until 09:45 IST."""
        # 09:29 IST -> Both locked
        dt_0929 = datetime(2026, 10, 19, 9, 29, 0)
        self.assertFalse(is_option_entry_window(dt_0929))
        self.assertFalse(is_live_option_entry_window(dt_0929))

        # 09:30 IST -> Paper OPEN (Canary Scout begins), Live LOCKED
        dt_0930 = datetime(2026, 10, 19, 9, 30, 0)
        self.assertTrue(is_option_entry_window(dt_0930))
        self.assertFalse(is_live_option_entry_window(dt_0930))

        # 09:44:59 IST -> Paper OPEN, Live STILL LOCKED
        dt_0944 = datetime(2026, 10, 19, 9, 44, 59)
        self.assertTrue(is_option_entry_window(dt_0944))
        self.assertFalse(is_live_option_entry_window(dt_0944))

        # 09:45:00 IST -> Both OPEN
        dt_0945 = datetime(2026, 10, 19, 9, 45, 0)
        self.assertTrue(is_option_entry_window(dt_0945))
        self.assertTrue(is_live_option_entry_window(dt_0945))

        # 13:14:59 IST -> Both OPEN
        dt_1314 = datetime(2026, 10, 19, 13, 14, 59)
        self.assertTrue(is_option_entry_window(dt_1314))
        self.assertTrue(is_live_option_entry_window(dt_1314))

        # 13:15:00 IST -> Both LOCKED (no afternoon bleed)
        dt_1315 = datetime(2026, 10, 19, 13, 15, 0)
        self.assertFalse(is_option_entry_window(dt_1315))
        self.assertFalse(is_live_option_entry_window(dt_1315))

    # ──────────────────────────────────────────────────────────────────────────
    # 2. CANARY SCOUT HEALTH AUDIT TESTS
    # ──────────────────────────────────────────────────────────────────────────
    def test_has_paper_stop_loss_today_clean(self):
        """Clean slate: has_paper_stop_loss_today returns False."""
        has_sl, msg = OptionDB.has_paper_stop_loss_today("2026-10-19")
        self.assertFalse(has_sl)
        self.assertIn("Canary scout healthy", msg)

    def test_has_paper_stop_loss_today_with_profitable_trade(self):
        """Winning trades do not trip Canary Scout."""
        OptionDB.create_paper_trade({
            "trade_id": "pt_win_01",
            "signal_id": "sig_win_01",
            "instrument_key": "NSE_FO|47168",
            "trading_symbol": "NIFTY 23100 CE 19 OCT 26",
            "underlying": "NIFTY",
            "strike": 23100.0,
            "option_type": "CE",
            "expiry": "2026-10-19",
            "quantity": 65,
            "lot_size": 65,
            "entry_time": "2026-10-19T09:35:00",
            "entry_ask": 150.0,
            "entry_bid": 149.5,
        })
        OptionDB.close_paper_trade(
            trade_id="pt_win_01",
            close_data={
                "exit_time": "2026-10-19T09:42:00",
                "exit_bid": 180.0,
                "exit_ask": 180.5,
                "exit_price_used": 180.0,
                "peak_price": 182.0,
                "trough_price": 148.0,
                "mae_pct": -1.33,
                "mfe_pct": 21.33,
                "gross_pnl": 1950.0,
                "charges": 55.0,
                "slippage": 20.0,
                "net_pnl": 1875.0,
                "net_pnl_pct": 19.2,
                "holding_duration_seconds": 420,
                "exit_reason": "TARGET_HIT",
            }
        )

        has_sl, msg = OptionDB.has_paper_stop_loss_today("2026-10-19")
        self.assertFalse(has_sl)

    def test_has_paper_stop_loss_today_trips_on_sl_hit(self):
        """A paper trade exiting on STOP_LOSS_HIT trips the Canary Scout."""
        OptionDB.create_paper_trade({
            "trade_id": "pt_loss_01",
            "signal_id": "sig_loss_01",
            "instrument_key": "NSE_FO|47168",
            "trading_symbol": "NIFTY 23100 CE 19 OCT 26",
            "underlying": "NIFTY",
            "strike": 23100.0,
            "option_type": "CE",
            "expiry": "2026-10-19",
            "quantity": 65,
            "lot_size": 65,
            "entry_time": "2026-10-19T09:31:00",
            "entry_ask": 150.0,
            "entry_bid": 149.5,
        })
        OptionDB.close_paper_trade(
            trade_id="pt_loss_01",
            close_data={
                "exit_time": "2026-10-19T09:36:00",
                "exit_bid": 120.0,
                "exit_ask": 120.5,
                "exit_price_used": 120.0,
                "peak_price": 152.0,
                "trough_price": 119.0,
                "mae_pct": -20.67,
                "mfe_pct": 1.33,
                "gross_pnl": -1950.0,
                "charges": 55.0,
                "slippage": 20.0,
                "net_pnl": -2025.0,
                "net_pnl_pct": -20.7,
                "holding_duration_seconds": 300,
                "exit_reason": "STOP_LOSS_HIT",
            }
        )

        has_sl, msg = OptionDB.has_paper_stop_loss_today("2026-10-19")
        self.assertTrue(has_sl)
        self.assertIn("STOP_LOSS_HIT", msg)
        self.assertIn("-2,025.00", msg)

    def test_has_paper_stop_loss_today_trips_on_latch(self):
        """If paper circuit breaker halt latch is stored in DB, Canary Scout is tripped."""
        OptionDB.persist_paper_circuit_breaker_latch(
            session_date="2026-10-19",
            realized_pnl=-3600.0,
            unrealized_pnl=0.0,
            total_exposure=-3600.0,
            trip_reason="Daily max loss ceiling reached (-₹3,600)",
            halt_latch=1
        )
        has_sl, msg = OptionDB.has_paper_stop_loss_today("2026-10-19")
        self.assertTrue(has_sl)
        self.assertIn("Paper circuit breaker tripped", msg)

    # ──────────────────────────────────────────────────────────────────────────
    # 3. ARMING INTERLOCK TESTS
    # ──────────────────────────────────────────────────────────────────────────
    def test_arm_live_trading_blocked_before_0945(self):
        """User cannot arm live trading before 09:45 IST even with correct PIN."""
        self.adapter._current_pin = "1234"
        with patch("app.analytics.option_sniffer.option_live_adapter.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 19, 9, 35, 0)
            mock_dt.strptime = datetime.strptime
            ok, msg, _ = self.adapter.arm_live_trading("1234")
            self.assertFalse(ok)
            self.assertIn("CANARY_SCOUT_ACTIVE", msg)
            self.assertIn("09:45", msg)
            self.assertFalse(self.adapter._is_live_armed)

    def test_arm_live_trading_blocked_if_paper_hit_sl(self):
        """User cannot arm live trading at 10:00 IST if paper trading hit SL earlier."""
        # Insert a paper SL
        OptionDB.create_paper_trade({
            "trade_id": "pt_morning_sl",
            "signal_id": "sig_morning_sl",
            "instrument_key": "NSE_FO|47168",
            "trading_symbol": "NIFTY 23100 PE 19 OCT 26",
            "underlying": "NIFTY",
            "strike": 23100.0,
            "option_type": "PE",
            "expiry": "2026-10-19",
            "quantity": 65,
            "lot_size": 65,
            "entry_time": "2026-10-19T09:30:10",
            "entry_ask": 200.0,
            "entry_bid": 199.5,
        })
        OptionDB.close_paper_trade(
            trade_id="pt_morning_sl",
            close_data={
                "exit_time": "2026-10-19T09:32:00",
                "exit_bid": 160.0,
                "exit_ask": 160.5,
                "exit_price_used": 160.0,
                "exit_reason": "STOP_LOSS_HIT",
                "charges": 55.0,
                "slippage": 20.0,
                "net_pnl": -2675.0,
            }
        )

        self.adapter._current_pin = "5555"
        with patch("app.analytics.option_sniffer.option_live_adapter.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 19, 10, 0, 0)
            mock_dt.strptime = datetime.strptime
            ok, msg, _ = self.adapter.arm_live_trading("5555")
            self.assertFalse(ok)
            self.assertIn("CANARY_SCOUT_ABORTED", msg)
            self.assertIn("pt_morning_sl", msg)
            self.assertFalse(self.adapter._is_live_armed)

    def test_arm_live_trading_succeeds_at_1000_with_clean_scout(self):
        """User can arm live trading after 09:45 IST when paper scout is pristine."""
        self.adapter._current_pin = "7777"
        mock_readiness = {
            "token_valid": True,
            "nfo_enabled": True,
            "available_margin": 50000.0,
            "static_ip_matched": True,
            "live_trading_eligible": True,
            "blockers": []
        }
        with patch.object(self.adapter, "fetch_account_readiness", return_value=mock_readiness):
            with patch("app.analytics.option_sniffer.option_live_adapter.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 10, 19, 10, 15, 0)
                mock_dt.strptime = datetime.strptime
                ok, msg, _ = self.adapter.arm_live_trading("7777")
                self.assertTrue(ok)
                self.assertIn("ARMED_FOR_TODAY", msg)
                self.assertTrue(self.adapter._is_live_armed)

    # ──────────────────────────────────────────────────────────────────────────
    # 4. LIVE ORDER DISPATCH & INTENT GATES
    # ──────────────────────────────────────────────────────────────────────────
    def test_check_and_dispatch_blocked_before_0945(self):
        """Automated entries are blocked before 09:45 IST even if live is armed."""
        self.adapter._is_live_armed = True
        self.adapter._armed_at_iso = "2026-10-19 09:30:00"

        with patch("app.analytics.option_sniffer.option_live_order_intent.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 19, 9, 35, 0)
            dispatched = self.intent_engine.check_and_dispatch_automated_entries({}, 50000.0)
            self.assertEqual(dispatched, [])

    def test_check_and_dispatch_blocked_if_canary_scout_tripped(self):
        """Automated entries are blocked at 10:00 IST if paper trading experienced an SL."""
        self.adapter._is_live_armed = True
        self.adapter._armed_at_iso = "2026-10-19 09:50:00"

        # Seed a paper SL
        OptionDB.create_paper_trade({
            "trade_id": "pt_sl_02",
            "signal_id": "sig_sl_02",
            "instrument_key": "NSE_FO|47168",
            "trading_symbol": "NIFTY 23100 PE 19 OCT 26",
            "underlying": "NIFTY",
            "strike": 23100.0,
            "option_type": "PE",
            "expiry": "2026-10-19",
            "quantity": 65,
            "lot_size": 65,
            "entry_time": "2026-10-19T09:30:10",
            "entry_ask": 200.0,
            "entry_bid": 199.5,
        })
        OptionDB.close_paper_trade(
            trade_id="pt_sl_02",
            close_data={
                "exit_time": "2026-10-19T09:32:00",
                "exit_bid": 160.0,
                "exit_ask": 160.5,
                "exit_price_used": 160.0,
                "exit_reason": "STOP_LOSS_HIT",
                "charges": 55.0,
                "slippage": 20.0,
                "net_pnl": -2675.0,
            }
        )

        with patch("app.analytics.option_sniffer.option_live_order_intent.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 19, 10, 0, 0)
            dispatched = self.intent_engine.check_and_dispatch_automated_entries({}, 50000.0)
            self.assertEqual(dispatched, [])

    def test_place_real_entry_order_blocked_before_0945(self):
        """place_real_entry_order returns BLOCKED when invoked before 09:45 IST."""
        self.adapter._is_live_armed = True
        with patch("app.analytics.option_sniffer.option_live_order_intent.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 19, 9, 31, 0)
            mock_dt.strptime = datetime.strptime
            res = self.intent_engine.place_real_entry_order("INTENT_TEST_001")
            self.assertEqual(res["status"], "BLOCKED")
            self.assertIn("OUTSIDE_LIVE_WINDOW", res["reason"])

    # ──────────────────────────────────────────────────────────────────────────
    # 5. GATE 9B DECOUPLING VERIFICATION (FIXES LEAKAGE BUG)
    # ──────────────────────────────────────────────────────────────────────────
    def test_gate_9b_decoupling_paper_circuit_breaker_enforced_even_if_live_armed(self):
        """
        REGRESSION PROOF FOR TODAY'S BUG:
        When live trading was armed and had ₹0 loss, Gate 9B erroneously checked the live
        ledger instead of paper, allowing paper to take Wave 2 after tripping its ₹3,500 limit.
        This test proves that paper circuit breaker trips reject signal evaluation even when
        live is armed with 0 losses!
        """
        # 1. Arm live trading
        with patch.object(self.adapter, "_is_live_armed", True):
            # 2. Trip paper circuit breaker latch
            OptionDB.persist_paper_circuit_breaker_latch(
                session_date="2026-10-19",
                realized_pnl=-3600.0,
                unrealized_pnl=0.0,
                total_exposure=-3600.0,
                trip_reason="Daily max loss ceiling reached (-₹3,600)",
                halt_latch=1
            )

            # 3. Evaluate signal in OptionSignalEngine at 10:00 IST
            eval_res = self.signal_engine.evaluate_contract(
                contract=self.contract,
                quote=self.quote,
                underlying_quote=self.underlying_quote,
                as_of_time=datetime(2026, 10, 19, 10, 0, 0)
            )

            # 4. MUST be rejected due to Daily Session Circuit Breaker Tripped!
            self.assertFalse(eval_res["hurdles_passed"])
            self.assertNotEqual(eval_res["status"], "ACTIVE")
            rejections = " | ".join(eval_res.get("rejection_reasons", []))
            self.assertIn("Daily Session Circuit Breaker Tripped", rejections)

    def test_open_paper_trade_rejected_when_paper_circuit_breaker_halted(self):
        """OptionPaperExecution.open_trade directly rejects opening trade if paper circuit breaker is halted."""
        OptionDB.persist_paper_circuit_breaker_latch(
            session_date="2026-10-19",
            realized_pnl=-3600.0,
            unrealized_pnl=0.0,
            total_exposure=-3600.0,
            trip_reason="Daily max loss ceiling reached (-₹3,600)",
            halt_latch=1
        )

        signal = {
            "signal_id": "sig_test_halt",
            "instrument_key": "NSE_FO|47168",
            "trading_symbol": "NIFTY 23100 CE 19 OCT 26",
            "underlying": "NIFTY",
            "strike": 23100.0,
            "option_type": "CE",
            "expiry": "2026-10-19",
            "lot_size": 65,
            "option_ask": 150.0,
            "option_bid": 149.5,
            "spot_price": 23105.0,
        }

        trade_id = OptionPaperExecution.open_trade(signal, as_of_time=datetime(2026, 10, 19, 10, 0, 0))
        self.assertIsNone(trade_id, "Paper trade MUST NOT open when paper circuit breaker is halted")


if __name__ == "__main__":
    unittest.main()
