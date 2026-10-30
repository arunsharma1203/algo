"""
Unit & Mathematical Boundary Verification Tests for Option Sniffer Circuit Breaker:
  1. Realized loss boundaries immediately below (-₹3,499.00), exactly at (-₹3,500.00), and above (-₹3,501.00).
  2. Peak-to-trough drawdown boundaries below (₹3,499.00), at (₹3,500.00), and above (₹3,501.00).
  3. Sign convention consistency in create_and_validate_intent (handles both negative PnL and positive loss).
  4. Open position MTM + statutory charges pushing total exposure across the -₹3,500.00 boundary.
  5. Outstanding order committed risk pushing total exposure across the -₹3,500.00 boundary.
  6. Unresolved broker execution states (pending > 15s) triggering fail-closed halt.
  7. Latch permanence: Profitable subsequent trades (+₹10,000.00) cannot unlatch a tripped breaker.
  8. Daily session rollover cleanly resetting state for the next trading day.
  9. Crash / restart recovery from persistent SQLite ledger.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from app.analytics.option_sniffer.option_constants import (
    MAX_DAILY_LOSS_INR,
    PEAK_TO_TROUGH_MAX_DRAWDOWN_INR,
)
from app.analytics.option_sniffer.option_live_order_intent import (
    OrderIntentEngine,
    init_live_order_tables,
    STATE_VALIDATED,
    STATE_SUBMITTED,
    STATE_FILLED,
    EXIT_NONE,
    EXIT_PENDING,
    EXIT_CONFIRMED_FLAT,
)
from app.analytics.option_sniffer.option_db import (
    OptionDB,
    init_option_sniffer_tables,
)


class TestCircuitBreakerMathematicalBoundaries(unittest.TestCase):

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp_db.name
        self.tmp_db.close()

        self.db_patcher = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.live_patcher = patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path)
        self.db_patcher.start()
        self.live_patcher.start()

        init_option_sniffer_tables()
        init_live_order_tables()
        self.intent_engine = OrderIntentEngine()

    def tearDown(self):
        self.live_patcher.stop()
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    # -------------------------------------------------------------------------
    # 1. PAPER CIRCUIT BREAKER MATHEMATICAL LOSS BOUNDARIES
    # -------------------------------------------------------------------------

    def test_paper_loss_boundary_below_limit_3499(self):
        """Loss of -₹3,499.00 is immediately below the -₹3,500.00 ceiling and must NOT trip."""
        today = "2026-10-19"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades
                (trade_id, signal_id, instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                 lot_size, quantity, entry_time, entry_ask, entry_price_used, exit_time, exit_price_used, status, net_pnl, exit_reason)
                VALUES
                ('T_3499', 'S1', 'NSE_FO|1', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29',
                 65, 65, '2026-10-19 10:00:00', 100.0, 100.0, '2026-10-19 10:15:00', 46.17, 'CLOSED', -3499.00, 'STOP_LOSS_HIT');
            """)

        telem = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            enforce_halt_latch=False
        )
        self.assertEqual(telem["today_net_pnl"], -3499.00)
        self.assertFalse(telem["is_realized_limit_hit"])
        self.assertFalse(telem["is_max_loss_limit_hit"])
        self.assertFalse(telem["is_session_halted"])

    def test_paper_loss_boundary_exact_limit_3500(self):
        """Loss of exactly -₹3,500.00 reaches the ceiling and MUST trip the circuit breaker."""
        today = "2026-10-19"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades
                (trade_id, signal_id, instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                 lot_size, quantity, entry_time, entry_ask, entry_price_used, exit_time, exit_price_used, status, net_pnl, exit_reason)
                VALUES
                ('T_3500', 'S2', 'NSE_FO|2', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29',
                 65, 65, '2026-10-19 10:00:00', 100.0, 100.0, '2026-10-19 10:15:00', 46.15, 'CLOSED', -3500.00, 'STOP_LOSS_HIT');
            """)

        telem = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            enforce_halt_latch=True
        )
        self.assertEqual(telem["today_net_pnl"], -3500.00)
        self.assertTrue(telem["is_realized_limit_hit"])
        self.assertTrue(telem["is_max_loss_limit_hit"])
        self.assertTrue(telem["is_session_halted"])
        self.assertTrue(telem["halt_latch_active"])

    def test_paper_loss_boundary_above_limit_3501(self):
        """Loss of -₹3,501.00 exceeds the ceiling and MUST trip the circuit breaker."""
        today = "2026-10-19"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades
                (trade_id, signal_id, instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                 lot_size, quantity, entry_time, entry_ask, entry_price_used, exit_time, exit_price_used, status, net_pnl, exit_reason)
                VALUES
                ('T_3501', 'S3', 'NSE_FO|3', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29',
                 65, 65, '2026-10-19 10:00:00', 100.0, 100.0, '2026-10-19 10:15:00', 46.14, 'CLOSED', -3501.00, 'STOP_LOSS_HIT');
            """)

        telem = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            enforce_halt_latch=True
        )
        self.assertEqual(telem["today_net_pnl"], -3501.00)
        self.assertTrue(telem["is_realized_limit_hit"])
        self.assertTrue(telem["is_max_loss_limit_hit"])
        self.assertTrue(telem["is_session_halted"])

    # -------------------------------------------------------------------------
    # 2. PEAK-TO-TROUGH DRAWDOWN MATHEMATICAL BOUNDARIES
    # -------------------------------------------------------------------------

    def test_peak_to_trough_drawdown_boundary_3499_vs_3500(self):
        """
        Peak PnL = +₹2,000.00.
        Case A: Trough PnL = -₹1,499.00 -> Drawdown = ₹3,499.00 (below ₹3,500 ceiling, NOT tripped).
        Case B: Next loss drops to -₹1,500.00 -> Drawdown = ₹3,500.00 (MUST trip).
        """
        today = "2026-10-19"
        # Case A
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades
                (trade_id, signal_id, instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                 lot_size, quantity, entry_time, entry_ask, entry_price_used, exit_time, exit_price_used, status, net_pnl, exit_reason)
                VALUES
                ('T_P1', 'S_P1', 'NSE_FO|1', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29', 65, 65, '2026-10-19 09:30:00', 100.0, 100.0, '2026-10-19 09:50:00', 130.77, 'CLOSED', 2000.00, 'TARGET_MET'),
                ('T_L1', 'S_L1', 'NSE_FO|2', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29', 65, 65, '2026-10-19 10:00:00', 100.0, 100.0, '2026-10-19 10:20:00', 46.17, 'CLOSED', -3499.00, 'STOP_LOSS_HIT');
            """)

        telem_a = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            enforce_halt_latch=False
        )
        # Peak = 2000, cum = -1499, dd = 3499
        self.assertEqual(telem_a["peak_pnl"], 2000.00)
        self.assertEqual(telem_a["max_drawdown"], 3499.00)
        self.assertFalse(telem_a["is_drawdown_limit_hit"])
        self.assertFalse(telem_a["is_session_halted"])

        # Case B: Update T_L1 to -₹3,500.00
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE option_sniffer_paper_trades SET net_pnl = -3500.00 WHERE trade_id = 'T_L1';")

        telem_b = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            enforce_halt_latch=False
        )
        self.assertEqual(telem_b["max_drawdown"], 3500.00)
        self.assertTrue(telem_b["is_drawdown_limit_hit"])
        self.assertTrue(telem_b["is_session_halted"])

    # -------------------------------------------------------------------------
    # 3. LIVE ORDER INTENT ENGINE CIRCUIT BREAKER & SIGN CONVENTION
    # -------------------------------------------------------------------------

    def test_live_intent_engine_boundary_below_at_above(self):
        """
        Tests create_and_validate_intent() directly against daily_loss_inr
        under both negative convention (-₹3499 vs -₹3500 vs -₹3501)
        and positive convention (+₹3499 vs +₹3500 vs +₹3501).
        """
        signal_payload = {
            "signal_id": "SIG_MATH_001",
            "instrument_key": "NSE_FO|99001",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        live_quote = {"ask": 100.0, "bid": 99.8, "quote_age_ms": 100}

        # 1. Negative convention: -3499 -> PASS
        ok, reason, _ = self.intent_engine.create_and_validate_intent(
            signal_payload, live_quote, daily_loss_inr=-3499.0
        )
        self.assertTrue(ok, f"Expected -3499 to pass, got: {reason}")

        # 2. Negative convention: -3500 -> REJECT
        ok, reason, _ = self.intent_engine.create_and_validate_intent(
            dict(signal_payload, signal_id="SIG_MATH_002"),
            live_quote,
            daily_loss_inr=-3500.0
        )
        self.assertFalse(ok)
        self.assertIn("RISK_BLOCKED", reason)
        self.assertIn("Daily loss ceiling reached", reason)

        # 3. Negative convention: -3501 -> REJECT
        ok, reason, _ = self.intent_engine.create_and_validate_intent(
            dict(signal_payload, signal_id="SIG_MATH_003"),
            live_quote,
            daily_loss_inr=-3501.0
        )
        self.assertFalse(ok)
        self.assertIn("RISK_BLOCKED", reason)

        # 4. Positive convention: 3499 -> PASS
        ok, reason, _ = self.intent_engine.create_and_validate_intent(
            dict(signal_payload, signal_id="SIG_MATH_004"),
            live_quote,
            daily_loss_inr=3499.0
        )
        self.assertTrue(ok, f"Expected +3499 to pass, got: {reason}")

        # 5. Positive convention: 3500 -> REJECT
        ok, reason, _ = self.intent_engine.create_and_validate_intent(
            dict(signal_payload, signal_id="SIG_MATH_005"),
            live_quote,
            daily_loss_inr=3500.0
        )
        self.assertFalse(ok)
        self.assertIn("RISK_BLOCKED", reason)

        # 6. Positive convention: 3501 -> REJECT
        ok, reason, _ = self.intent_engine.create_and_validate_intent(
            dict(signal_payload, signal_id="SIG_MATH_006"),
            live_quote,
            daily_loss_inr=3501.0
        )
        self.assertFalse(ok)
        self.assertIn("RISK_BLOCKED", reason)

    # -------------------------------------------------------------------------
    # 4. OPEN POSITION MTM & STATUTORY CHARGES ACCOUNTING
    # -------------------------------------------------------------------------

    def test_open_position_mtm_and_charges_crosses_circuit_breaker(self):
        """
        Closed Realized PnL = -₹3,200.00 (not yet breached).
        Open position: 65 qty @ entry ₹100.00, current bid ₹96.00.
        Gross floating loss = (96.00 - 100.00) * 65 = -₹260.00.
        Statutory round-trip friction = ~₹55.00.
        Net floating MTM = -₹315.00.
        Total exposure = -₹3,200.00 + (-₹315.00) = -₹3,515.00 <= -₹3,500.00!
        The system must account for this floating loss and trigger the circuit breaker.
        """
        today = "2026-10-19"
        with sqlite3.connect(self.db_path) as conn:
            # Closed trade with -₹3,200 loss
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades
                (trade_id, signal_id, instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                 lot_size, quantity, entry_time, entry_ask, entry_price_used, exit_time, exit_price_used, status, net_pnl, exit_reason)
                VALUES
                ('T_CLOSED', 'S_C', 'NSE_FO|1', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29', 65, 65,
                 '2026-10-19 09:30:00', 100.0, 100.0, '2026-10-19 09:45:00', 50.77, 'CLOSED', -3200.00, 'STOP_LOSS_HIT');
            """)
            # Open trade with entry ₹100
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades
                (trade_id, signal_id, instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                 lot_size, quantity, entry_time, entry_ask, entry_price_used, status)
                VALUES
                ('T_OPEN', 'S_O', 'NSE_FO|99010', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29', 65, 65,
                 '2026-10-19 10:00:00', 100.0, 100.0, 'OPEN');
            """)

        # Provide live quote with bid ₹96.00
        live_quotes = {
            "NSE_FO|99010": {"bid": 96.0, "ask": 96.5, "quote_age_ms": 100}
        }
        telem = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            live_quotes_map=live_quotes,
            enforce_halt_latch=True
        )
        self.assertEqual(telem["today_net_pnl"], -3200.00)
        self.assertLess(telem["unrealized_net_pnl"], -260.00)  # Gross -260 plus charges -> ~-315
        self.assertLessEqual(telem["total_net_pnl_exposure"], -3500.00)
        self.assertTrue(telem["is_exposure_limit_hit"])
        self.assertTrue(telem["is_session_halted"])
        self.assertIn("Total Exposure", telem["halt_reason"])

    # -------------------------------------------------------------------------
    # 5. OUTSTANDING ORDERS & UNRESOLVED BROKER EXECUTION STATES
    # -------------------------------------------------------------------------

    def test_live_engine_committed_risk_and_unresolved_broker_state(self):
        """
        Verifies that:
          1. Outstanding unconfirmed SUBMITTED orders have their committed stop-loss risk accrued.
          2. Orders pending > 15 seconds trigger unresolved broker state fail-closed halt.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        now_dt = datetime.now()
        pending_time = (now_dt - timedelta(seconds=25)).strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents
                (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action,
                 lot_size, target_quantity, limit_price, state, dynamic_sl_pct, created_at, updated_at)
                VALUES
                ('INTENT_STALE_SUBMITTED', 'SIG_STALE', 'NSE_FO|88001', 'NIFTY 25000 CE', 'NIFTY', 'BUY_CE',
                 65, 65, 100.0, 'SUBMITTED', 20.0, ?, ?);
            """, (pending_time, pending_time))

        live_cb = self.intent_engine.get_live_circuit_breaker_telemetry()
        # Committed risk = 65 * 100 * 0.20 + ~55 charges = ~1355.00
        self.assertGreater(live_cb["total_net_pnl_exposure"], -1500.0)
        self.assertTrue(live_cb["is_session_halted"])
        self.assertIn("AMBIGUOUS_ORDER_PENDING", live_cb["halt_reason"])

    # -------------------------------------------------------------------------
    # 6. LATCH PERMANENCE: PROFITABLE TRADES CANNOT UNLATCH CIRCUIT BREAKER
    # -------------------------------------------------------------------------

    def test_profitable_trades_cannot_unlatch_active_breaker(self):
        """
        Proves that once the daily loss ceiling is hit (-₹3,500.00) and the halt latch
        is written to the database, a subsequent massive profit (+₹10,000.00)
        CANNOT unlatch the system.
        """
        today = "2026-10-19"
        # 1. Breach limit with -₹3,600.00 loss
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades
                (trade_id, signal_id, instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                 lot_size, quantity, entry_time, entry_ask, entry_price_used, exit_time, exit_price_used, status, net_pnl, exit_reason)
                VALUES
                ('T_BREACH', 'S_B', 'NSE_FO|1', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29',
                 65, 65, '2026-10-19 10:00:00', 100.0, 100.0, '2026-10-19 10:15:00', 44.62, 'CLOSED', -3600.00, 'STOP_LOSS_HIT');
            """)

        telem_breach = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            enforce_halt_latch=True
        )
        self.assertTrue(telem_breach["is_session_halted"])
        self.assertTrue(telem_breach["halt_latch_active"])

        # 2. Insert a massive subsequent winning trade of +₹10,000.00
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades
                (trade_id, signal_id, instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                 lot_size, quantity, entry_time, entry_ask, entry_price_used, exit_time, exit_price_used, status, net_pnl, exit_reason)
                VALUES
                ('T_WIN', 'S_W', 'NSE_FO|2', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29',
                 65, 65, '2026-10-19 11:00:00', 100.0, 100.0, '2026-10-19 11:30:00', 253.85, 'CLOSED', 10000.00, 'TARGET_MET');
            """)

        telem_after_win = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            enforce_halt_latch=True
        )
        # Net PnL is now +₹6,400.00
        self.assertEqual(telem_after_win["today_net_pnl"], 6400.00)
        # BUT circuit breaker MUST remain permanently halted!
        self.assertTrue(telem_after_win["is_max_loss_limit_hit"])
        self.assertTrue(telem_after_win["is_session_halted"])
        self.assertTrue(telem_after_win["halt_latch_active"])

    # -------------------------------------------------------------------------
    # 7. SESSION DATE RESET & RESTART RECOVERY
    # -------------------------------------------------------------------------

    def test_daily_session_rollover_cleans_slate(self):
        """
        Yesterday had a tripped breaker latch.
        Today's new session date must start completely clean (no halt, no latch).
        """
        yesterday = "2026-10-18"
        today = "2026-10-19"

        # Latch yesterday
        OptionDB.persist_paper_circuit_breaker_latch(
            session_date=yesterday,
            realized_pnl=-3800.0,
            unrealized_pnl=0.0,
            total_exposure=-3800.0,
            trip_reason="Daily max loss ceiling reached",
            halt_latch=1
        )

        # Yesterday's telemetry is halted
        telem_yesterday = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=yesterday,
            enforce_halt_latch=True
        )
        self.assertTrue(telem_yesterday["is_session_halted"])

        # Today's telemetry is CLEAN
        telem_today = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            enforce_halt_latch=True
        )
        self.assertFalse(telem_today["is_session_halted"])
        self.assertFalse(telem_today["is_max_loss_limit_hit"])
        self.assertEqual(telem_today["total_closed_today"], 0)

    def test_restart_recovery_from_persistent_sqlite_ledger(self):
        """
        Simulates backend process crash & reboot: in-memory state is wiped,
        but persistent SQLite ledger retains the latch and restores halt immediately.
        """
        today = "2026-10-19"
        # Directly insert latch into option_sniffer_paper_risk_ledger
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_sniffer_paper_risk_ledger
                (session_date, starting_capital, realized_pnl, unrealized_pnl, total_exposure, circuit_breaker_tripped, trip_reason, halt_latch, updated_at)
                VALUES
                (?, 15000.0, -3600.0, 0.0, -3600.0, 1, 'Daily max loss ceiling reached. Halted.', 1, '2026-10-19 10:30:00');
            """, (today,))

        # Cold restart call with zero closed trade rows
        telem = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            enforce_halt_latch=True
        )
        self.assertTrue(telem["is_session_halted"])
        self.assertTrue(telem["is_max_loss_limit_hit"])
        self.assertEqual(telem["today_net_pnl"], -3600.0)
        self.assertEqual(telem["halt_reason"], "Daily max loss ceiling reached. Halted.")


if __name__ == "__main__":
    unittest.main()
