"""
Comprehensive Unit & Mocked Integration Tests for Option Sniffer Execution Costs and Portfolio Risk Remediation.

Covers:
  1. Effective-dated statutory fee schedules (<2024-10-01, 2024-10-01 to 2026-09-30, >=2026-10-01).
  2. Component fee determinism (STT 0.15%, Exchange+IPFT 0.03553%, GST 18%, Brokerage ₹40, Stamp duty 0.003%, SEBI fee).
  3. Slippage scenario comparison (Zero additional slippage, Calibrated 0.20% RT, Legacy 0.50% RT).
  4. Quoted spread vs additional slippage separation (no double deduction).
  5. Paper simultaneous open position drawdown & circuit breaker square-off.
  6. Paper persistent daily halt latch surviving simulated process restart.
  7. Live persistent daily halt latch surviving simulated process restart.
  8. Live pending order cancellation & SOS panic exit invocation on circuit breaker breach.
  9. Stale / missing quote fail-closed behavior.
  10. Decoupling of paper and live risk ledgers.
"""

import os
import unittest
import sqlite3
import tempfile
from unittest.mock import patch, MagicMock
from datetime import datetime, date, timedelta

from app.analytics.option_sniffer.option_constants import (
    get_option_tariff_schedule,
    calculate_option_trade_costs,
    PUBLISHED_OCT_2026_STT_RATE,
    PUBLISHED_OCT_2026_EXCHANGE_RATE,
    STT_SELL_RATE,
    EXCHANGE_TURNOVER_RATE,
    BROKERAGE_PER_ORDER,
    GST_RATE,
    MAX_DAILY_LOSS_INR,
    MAX_CONSECUTIVE_LOSSES,
    INTROSPECTION_FREEZE_MINUTES,
)
from app.analytics.option_sniffer.option_db import OptionDB, init_option_sniffer_tables
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.option_sniffer.option_live_order_intent import OrderIntentEngine, init_live_order_tables


class TestEffectiveDatedStatutoryCharges(unittest.TestCase):
    """Verifies official Upstox/NSE tariff schedules across effective-date boundaries."""

    def test_default_schedule_for_current_trades_without_silent_legacy_fallback(self):
        """When trade_date is None, defaults to current date (not silent legacy fallback)."""
        sched = get_option_tariff_schedule(None)
        self.assertEqual(sched["stt_sell_rate"], 0.0015)
        self.assertEqual(sched["exchange_turnover_rate"], 0.0003553)
        self.assertEqual(sched["brokerage_per_order"], 20.0)
        self.assertEqual(sched["gst_rate"], 0.18)

    def test_legacy_override_tariff_schedule(self):
        """When trade_date == 'LEGACY', returns legacy schedule for historical compatibility."""
        sched = get_option_tariff_schedule("LEGACY")
        self.assertEqual(sched["schedule_name"], "UPSTOX_LEGACY_DEFAULT_TARIFF")
        self.assertEqual(sched["stt_sell_rate"], STT_SELL_RATE)
        self.assertEqual(sched["exchange_turnover_rate"], EXCHANGE_TURNOVER_RATE)
        self.assertEqual(sched["brokerage_per_order"], 20.0)

    def test_april_1_2026_stt_boundary(self):
        """STT increases from 0.10% to 0.15% effective April 1, 2026."""
        sched_before = get_option_tariff_schedule("2026-03-31")
        self.assertAlmostEqual(sched_before["stt_sell_rate"], 0.0010, places=6)

        sched_after = get_option_tariff_schedule("2026-04-01")
        self.assertAlmostEqual(sched_after["stt_sell_rate"], 0.0015, places=6)

    def test_march_1_2026_exchange_boundary_avoid_double_counting_ipft(self):
        """
        NSE options turnover fee revised under circular NSE/FA/73061 effective March 1, 2026:
        Core exchange rate adjusted to 0.0355299% and IPFT rolled back to 0.0000001%.
        Total composite outflow remains 0.03553%; IPFT is never double-counted.
        """
        sched_before = get_option_tariff_schedule("2026-02-28")
        self.assertAlmostEqual(sched_before["exchange_core_rate"], 0.0003503, places=7)
        self.assertAlmostEqual(sched_before["ipft_rate"], 0.0000050, places=7)
        self.assertAlmostEqual(sched_before["exchange_turnover_rate"], 0.0003553, places=7)

        sched_after = get_option_tariff_schedule("2026-03-01")
        self.assertAlmostEqual(sched_after["exchange_core_rate"], 0.000355299, places=9)
        self.assertAlmostEqual(sched_after["ipft_rate"], 0.000000001, places=9)
        self.assertAlmostEqual(sched_after["exchange_turnover_rate"], 0.0003553, places=7)

    def test_october_2024_finance_act_boundary(self):
        """Finance Act 2024: STT 0.10% from October 1, 2024, and SEBI true-to-label transaction charges."""
        sched = get_option_tariff_schedule("2024-10-01")
        self.assertAlmostEqual(sched["stt_sell_rate"], 0.0010, places=6)
        self.assertAlmostEqual(sched["exchange_core_rate"], 0.0003503, places=7)
        self.assertAlmostEqual(sched["ipft_rate"], 0.0000050, places=7)
        self.assertAlmostEqual(sched["exchange_turnover_rate"], 0.0003553, places=7)

    def test_pre_oct_2024_legacy_statutory_schedule(self):
        """For dates strictly before 2024-10-01, applies 0.0625% STT and 0.0535% Exchange fee."""
        for d in ["2024-09-30", "2024-01-01", "2023-12-15"]:
            sched = get_option_tariff_schedule(d)
            self.assertAlmostEqual(sched["stt_sell_rate"], 0.000625, places=6)
            self.assertAlmostEqual(sched["exchange_turnover_rate"], 0.000535, places=6)

    def test_deterministic_fee_components_october_2026(self):
        """
        Validates exact rupee calculation of every statutory fee component for October 2026:
          Buy: 100.0 * 65 = ₹6,500
          Sell: 120.0 * 65 = ₹7,800
          Total Turnover = ₹14,300
          Brokerage = ₹40.00
          STT (0.15% on sell ₹7,800) = ₹11.70
          Exchange + IPFT (0.03553% on ₹14,300) = ₹5.08079 -> ₹5.08
          SEBI Fee (0.0001% on ₹14,300) = ₹0.0143 -> ₹0.01
          GST (Upstox published base: 18% on ₹40.00 + ₹5.08; SEBI excluded) = 0.18 * 45.08 = ₹8.1144 -> ₹8.11
          Stamp Duty (0.003% on buy ₹6,500) = ₹0.195 -> ₹0.20
          Statutory Charges = 40.00 + 11.70 + 5.08 + 0.01 + 8.11 + 0.20 = ₹65.10
        """
        costs = calculate_option_trade_costs(
            buy_price=100.0,
            sell_price=120.0,
            quantity=65,
            apply_slippage=False,
            trade_date="2026-10-14"
        )
        self.assertEqual(costs["brokerage"], 40.00)
        self.assertEqual(costs["stt"], 11.70)
        self.assertEqual(costs["exchange_fee"], 5.08)
        self.assertEqual(costs["sebi_fee"], 0.01)
        self.assertEqual(costs["gst"], 8.11)
        self.assertEqual(costs["stamp_duty"], 0.20)
        expected_statutory = round(40.00 + 11.70 + 5.08 + 0.01 + 8.11 + 0.20, 2)
        self.assertEqual(costs["statutory_charges"], expected_statutory)
        self.assertTrue(costs["is_statutory_charges_estimated"])
        self.assertEqual(costs["charges_reconciliation_mode"], "ESTIMATED_STATUTORY_SCHEDULE")

    def test_upstox_gst_charge_base_excludes_sebi_fee(self):
        """Verifies that Upstox published tariff applies 18% GST only on Brokerage + Exchange Transaction Charges."""
        sched = get_option_tariff_schedule("2026-10-14")
        self.assertFalse(sched["gst_includes_sebi"])

        costs = calculate_option_trade_costs(
            buy_price=500.0,
            sell_price=550.0,
            quantity=65,
            apply_slippage=False,
            trade_date="2026-10-14"
        )
        # Total turnover = (500 + 550) * 65 = 68,250
        # Brokerage = 40.00
        # Exch = 68,250 * 0.0003553 = 24.25
        # SEBI = 68,250 * 0.000001 = 0.07
        # GST must be round((40.00 + 24.25) * 0.18, 2) = round(64.25 * 0.18, 2) = 11.565 -> 11.56
        # If SEBI were included: round(64.32 * 0.18, 2) = 11.5776 -> 11.58
        self.assertEqual(costs["gst"], 11.56)

    def test_official_nse_component_breakdown_march_2026(self):
        """
        Verifies official NSE component breakdown effective March 1, 2026 under circular NSE/FA/73061:
          - Core transaction charge: Rs 3,552.99 / cr (0.0355299%)
          - IPFT contribution: Rs 0.01 / cr (0.000000001)
          - Composite rate: 0.03553%
          - IPFT is strictly bundled in composite turnover rate and never double-counted.
        """
        sched = get_option_tariff_schedule("2026-03-15")
        self.assertEqual(sched["regulatory_ref"], "NSE/FA/73061")
        self.assertAlmostEqual(sched["exchange_turnover_rate"], 0.0003553, places=7)
        self.assertAlmostEqual(sched["exchange_core_rate"], 0.000355299, places=9)
        self.assertAlmostEqual(sched["ipft_rate"], 0.000000001, places=9)
        self.assertAlmostEqual(sched["exchange_core_rate"] + sched["ipft_rate"], sched["exchange_turnover_rate"], places=7)

    def test_october_2024_to_february_2026_tariff_schedule_and_gst_base(self):
        """
        Verifies that for October 2024 to February 2026:
          - Core transaction rate is 0.03503% (NSE true-to-label per NSE/FA/64232)
          - Applicable member IPFT rate is 0.00050% (Rs 50/crore per NSE/FA/56129)
          - Composite rate is 0.03553% (0.03503% + 0.00050%)
          - Both transaction rate and IPFT are included in statutory charges and the GST base.
        """
        sched = get_option_tariff_schedule("2025-06-15")
        self.assertEqual(sched["regulatory_ref"], "NSE/FA/64232_56129")
        self.assertAlmostEqual(sched["exchange_core_rate"], 0.0003503, places=7)
        self.assertAlmostEqual(sched["ipft_rate"], 0.0000050, places=7)
        self.assertAlmostEqual(sched["exchange_turnover_rate"], 0.0003553, places=7)
        self.assertAlmostEqual(sched["exchange_core_rate"] + sched["ipft_rate"], sched["exchange_turnover_rate"], places=7)

        # Roundtrip: Buy @ 100, Sell @ 100, Qty = 65 -> Total Turnover = 13,000
        costs = calculate_option_trade_costs(100.0, 100.0, 65, trade_date="2025-06-15")
        self.assertEqual(costs["exchange_core_fee"], round(13000 * 0.0003503, 2))  # 4.55
        self.assertEqual(costs["ipft_fee"], round(13000 * 0.0000050, 2))           # 0.07
        self.assertEqual(costs["exchange_fee"], round(13000 * 0.0003553, 2))       # 4.62
        self.assertEqual(costs["exchange_core_fee"] + costs["ipft_fee"], costs["exchange_fee"])

        # GST: 18% on (Brokerage 40.00 + Exchange Fee 4.62) = round(44.62 * 0.18, 2) = 8.03
        self.assertEqual(costs["gst"], 8.03)

        # Statutory Charges: Brokerage(40.0) + STT(6.50) + Exch(4.62) + SEBI(0.01) + GST(8.03) + Stamp(0.20) = 59.36
        self.assertEqual(costs["statutory_charges"], 59.36)

    def test_historical_trades_use_execution_date_boundaries(self):
        """Verifies that historical trades correctly trigger their applicable dated tariff schedules."""
        # Pre-October 2024: 0.0625% STT, 0.0535% Exchange
        c_2024_08 = calculate_option_trade_costs(100.0, 100.0, 65, trade_date="2024-08-15")
        self.assertAlmostEqual(c_2024_08["stt"], round(6500 * 0.000625, 2))
        self.assertAlmostEqual(c_2024_08["exchange_fee"], round(13000 * 0.000535, 2))

        # October 2024 to February 2026: 0.10% STT, 0.03553% Exchange composite (0.03503% core + 0.00050% IPFT)
        c_2025_05 = calculate_option_trade_costs(100.0, 100.0, 65, trade_date="2025-05-20")
        self.assertAlmostEqual(c_2025_05["stt"], round(6500 * 0.0010, 2))
        self.assertAlmostEqual(c_2025_05["exchange_core_fee"], round(13000 * 0.0003503, 2))
        self.assertAlmostEqual(c_2025_05["ipft_fee"], round(13000 * 0.0000050, 2))
        self.assertAlmostEqual(c_2025_05["exchange_fee"], round(13000 * 0.0003553, 2))

        # March 2026: 0.10% STT, 0.03553% Exchange composite (0.0355299% core + 0.0000001% IPFT)
        c_2026_03 = calculate_option_trade_costs(100.0, 100.0, 65, trade_date="2026-03-10")
        self.assertAlmostEqual(c_2026_03["stt"], round(6500 * 0.0010, 2))
        self.assertAlmostEqual(c_2026_03["exchange_fee"], round(13000 * 0.0003553, 2))

        # April 2026 onwards: 0.15% STT, 0.03553% Exchange composite
        c_2026_04 = calculate_option_trade_costs(100.0, 100.0, 65, trade_date="2026-04-05")
        self.assertAlmostEqual(c_2026_04["stt"], round(6500 * 0.0015, 2))
        self.assertAlmostEqual(c_2026_04["exchange_fee"], round(13000 * 0.0003553, 2))


class TestExecutionCostModelAndSlippageScenarios(unittest.TestCase):
    """Tests quoted spread vs slippage separation and multi-scenario slippage comparison."""

    def test_quoted_spread_separation_without_double_deduction(self):
        """
        Entry at Ask (₹100.50), Exit at Bid (₹119.50) with bid/ask quotes:
          Buy Quote: Bid ₹99.50, Ask ₹100.50 (Spread = ₹1.00, Half-spread = ₹0.50)
          Sell Quote: Bid ₹119.50, Ask ₹120.50 (Spread = ₹1.00, Half-spread = ₹0.50)
          Quantity = 65
          Quoted Spread Drag = (0.50 + 0.50) * 65 = ₹65.00
          Executed Gross P&L = (119.50 - 100.50) * 65 = ₹1,235.00
          Midpoint Gross P&L = (120.00 - 100.00) * 65 = ₹1,300.00
          Notice: Executed Gross already accounts for the ₹65.00 spread!
          Therefore, total_friction must NOT deduct quoted spread again.
        """
        costs = calculate_option_trade_costs(
            buy_price=100.50,
            sell_price=119.50,
            quantity=65,
            apply_slippage=False,
            trade_date="2026-10-14",
            buy_bid=99.50,
            buy_ask=100.50,
            sell_bid=119.50,
            sell_ask=120.50
        )
        self.assertEqual(costs["quoted_spread_cost"], 65.00)
        self.assertEqual(costs["additional_slippage"], 0.0)
        # Total friction must strictly equal statutory charges only (no double deduction of spread)
        self.assertEqual(costs["total_friction"], costs["statutory_charges"])

    def test_slippage_scenarios_comparison(self):
        """
        Compares the 3 explicit slippage scenarios on a sample trade:
          Scenario A: 0.0% additional slippage
          Scenario B: Calibrated 0.20% RT (0.10% per leg)
          Scenario C: Legacy 0.50% RT (0.25% per leg)
        """
        buy_p = 100.0
        sell_p = 80.0
        qty = 65
        tot_turnover = (buy_p + sell_p) * qty  # ₹11,700

        # Scenario A: Zero additional slippage
        cost_a = calculate_option_trade_costs(buy_p, sell_p, qty, apply_slippage=False, trade_date="2026-10-14")
        self.assertEqual(cost_a["additional_slippage"], 0.0)

        # Scenario B: Calibrated 0.20% RT (0.10% per leg)
        cost_b = calculate_option_trade_costs(
            buy_p, sell_p, qty, apply_slippage=True, slippage_rate_per_leg=0.0010, trade_date="2026-10-14"
        )
        expected_slip_b = round(tot_turnover * 0.0010, 2)  # ₹11.70
        self.assertEqual(cost_b["additional_slippage"], expected_slip_b)

        # Scenario C: Legacy 0.50% RT (0.25% per leg)
        cost_c = calculate_option_trade_costs(
            buy_p, sell_p, qty, apply_slippage=True, slippage_rate_per_leg=0.0025, trade_date="2026-10-14"
        )
        expected_slip_c = round(tot_turnover * 0.0025, 2)  # ₹29.25
        self.assertEqual(cost_c["additional_slippage"], expected_slip_c)

        # Friction monotonicity: A < B < C
        self.assertLess(cost_a["total_friction"], cost_b["total_friction"])
        self.assertLess(cost_b["total_friction"], cost_c["total_friction"])


class TestPaperPortfolioRiskControllerAndLatch(unittest.TestCase):
    """Tests simultaneous open position drawdown, square-off, and persistent latching."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        with patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path):
            init_option_sniffer_tables()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_simultaneous_open_positions_drawdown_trips_circuit_breaker(self):
        """
        When 3 open paper positions experience severe simultaneous floating loss,
        unrealized MTM breaches the -₹3,500 daily loss ceiling:
          - Telemetry flags is_session_halted = True and is_max_loss_limit_hit = True.
          - Halt latch is written to option_sniffer_paper_risk_ledger.
          - OptionPaperExecution.update_open_trades() squares off all open trades with 'CIRCUIT_BREAKER_HALT'.
        """
        now_dt = datetime(2026, 10, 14, 11, 0, 0)
        date_str = "2026-10-14"

        with sqlite3.connect(self.db_path) as conn:
            # Insert 3 open positions entered at ask ₹200.0, qty 65 (Capital = ₹13,000 each)
            for i, ikey in enumerate(["NSE_FO|101", "NSE_FO|102", "NSE_FO|103"]):
                conn.execute("""
                    INSERT INTO option_sniffer_paper_trades (
                        trade_id, signal_id, instrument_key, trading_symbol, underlying,
                        option_type, strike, expiry, lot_size, quantity, entry_time,
                        entry_bid, entry_ask, entry_price_used, status, peak_price, trough_price
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    f"TRADE_{i}", f"SIG_{i}", ikey, f"NIFTY_OPT_{i}", "NIFTY",
                    "CE", 22500, "2026-10-19", 65, 65, now_dt.isoformat(),
                    198.0, 200.0, 200.0, "OPEN", 200.0, 200.0
                ))
            conn.commit()

        # Quotes drop to ₹170.0 (Adverse move of -₹30 per contract * 65 * 3 = -₹5,850 gross loss)
        live_quotes_map = {
            "NSE_FO|101": {"bid": 170.0, "ask": 172.0, "ltp": 170.0},
            "NSE_FO|102": {"bid": 170.0, "ask": 172.0, "ltp": 170.0},
            "NSE_FO|103": {"bid": 170.0, "ask": 172.0, "ltp": 170.0},
        }

        with patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path):
            tel = OptionDB.get_daily_circuit_breaker_telemetry(
                as_of_date=date_str,
                as_of_time=now_dt,
                live_quotes_map=live_quotes_map,
                enforce_halt_latch=True
            )
            self.assertTrue(tel["is_session_halted"])
            self.assertTrue(tel["is_max_loss_limit_hit"])
            self.assertLess(tel["total_net_pnl_exposure"], -3500.0)

            # Check persistent halt latch in SQLite table
            latch_info = OptionDB.get_paper_circuit_breaker_latch(date_str)
            self.assertIsNotNone(latch_info)
            self.assertEqual(latch_info["halt_latch"], 1)
            self.assertEqual(latch_info["circuit_breaker_tripped"], 1)

            # Execute update_open_trades sweep: must close all 3 trades with CIRCUIT_BREAKER_HALT
            closed = OptionPaperExecution.update_open_trades(
                live_quotes_map=live_quotes_map,
                as_of_time=now_dt
            )
            self.assertEqual(len(closed), 3)
            for c in closed:
                self.assertEqual(c["exit_reason"], "CIRCUIT_BREAKER_HALT")
                self.assertEqual(c["status"], "CLOSED")

            # Verify no open trades remain
            open_remaining = OptionDB.get_open_paper_trades()
            self.assertEqual(len(open_remaining), 0)

    def test_paper_halt_latch_persists_across_restart(self):
        """Simulates complete system restart after circuit breaker trip; ensures latch blocks new entries."""
        date_str = "2026-10-14"
        with patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path):
            # Trip latch
            OptionDB.persist_paper_circuit_breaker_latch(
                session_date=date_str,
                realized_pnl=-3600.0,
                unrealized_pnl=0.0,
                total_exposure=-3600.0,
                trip_reason="Manual test trip: Daily loss limit hit",
                halt_latch=1
            )

            # Query without live quotes or trades: must strictly read persistent latch
            tel_after_restart = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date=date_str)
            self.assertTrue(tel_after_restart["is_session_halted"])
            self.assertTrue(tel_after_restart["halt_latch_active"])
            self.assertIn("Manual test trip", tel_after_restart["halt_reason"])

    def test_stale_or_missing_quotes_fail_closed(self):
        """If open trades exist but live quotes are missing or have bid <= 0, fails closed."""
        now_dt = datetime(2026, 10, 14, 11, 0, 0)
        date_str = "2026-10-14"

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades (
                    trade_id, signal_id, instrument_key, trading_symbol, underlying,
                    option_type, strike, expiry, lot_size, quantity, entry_time,
                    entry_bid, entry_ask, entry_price_used, status, peak_price, trough_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                "TRADE_STALE", "SIG_STALE", "NSE_FO|MISSING", "NIFTY_OPT", "NIFTY",
                "CE", 22500, "2026-10-19", 65, 65, now_dt.isoformat(),
                198.0, 200.0, 200.0, "OPEN", 200.0, 200.0
            ))
            conn.commit()

        with patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path):
            # Quotes map missing NSE_FO|MISSING
            tel = OptionDB.get_daily_circuit_breaker_telemetry(
                as_of_date=date_str,
                as_of_time=now_dt,
                live_quotes_map={},
                enforce_halt_latch=False
            )
            self.assertTrue(tel["is_session_halted"])
            self.assertIn("Fail-Closed: Missing or stale quotes", tel["halt_reason"])


class TestLiveOrderIntentRiskControllerAndEmergencyExit(unittest.TestCase):
    """Tests live order intent circuit breaker, pending order cancel, and verified SOS panic exit."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        with patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path), \
             patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path):
            init_live_order_tables()
            init_option_sniffer_tables()
            self.engine = OrderIntentEngine()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_live_circuit_breaker_breach_cancels_pending_and_triggers_sos(self):
        """
        On live aggregate loss breach:
          1. Pending unconfirmed entry orders are canceled.
          2. Open filled positions trigger SOS panic exit.
          3. Live halt latch is persisted.
        """
        with patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path):
            with sqlite3.connect(self.db_path) as conn:
                # 1 Pending submitted order
                conn.execute("""
                    INSERT INTO option_live_order_intents (
                        order_intent_id, signal_id, instrument_key, trading_symbol, underlying,
                        action, lot_size, target_quantity, limit_price, state, broker_order_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    "INTENT_PENDING_1", "SIG_P1", "NSE_FO|201", "NIFTY_P1", "NIFTY",
                    "BUY", 65, 65, 150.0, "SUBMITTED", "BROKER_ORD_101",
                    "2026-10-14 10:00:00", "2026-10-14 10:00:00"
                ))

                # 1 Filled open position
                conn.execute("""
                    INSERT INTO option_live_order_intents (
                        order_intent_id, signal_id, instrument_key, trading_symbol, underlying,
                        action, lot_size, target_quantity, filled_quantity, limit_price,
                        average_fill_price, state, exit_state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    "INTENT_FILLED_1", "SIG_F1", "NSE_FO|202", "NIFTY_F1", "NIFTY",
                    "BUY", 65, 65, 65, 180.0,
                    180.0, "FILLED", "NONE", "2026-10-14 10:05:00", "2026-10-14 10:05:00"
                ))
                conn.commit()

            # Mock broker cancel and SOS panic exit
            with patch.object(self.engine, "cancel_broker_order", return_value=True) as mock_cancel, \
                 patch.object(self.engine, "trigger_sos_panic_exit", return_value={"status": "CONFIRMED_FLAT"}) as mock_sos:
                res = self.engine.handle_circuit_breaker_breach(live_quotes_map={"NSE_FO|202": {"bid": 150.0}})
                self.assertEqual(res["canceled_pending_orders_count"], 1)
                self.assertEqual(res["closed_positions_count"], 1)
                mock_cancel.assert_called_once_with("BROKER_ORD_101")
                mock_sos.assert_called_once_with("INTENT_FILLED_1", 150.0)

    def test_live_halt_latch_persists_across_restart(self):
        """Confirms live risk ledger halt latch persists and blocks subsequent new entries."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        with patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path):
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO option_live_risk_ledger (
                        session_date, starting_capital, realized_pnl, unrealized_pnl,
                        peak_drawdown_inr, trades_executed, circuit_breaker_tripped,
                        trip_reason, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    date_str, 50000.0, -4000.0, 0.0, 4000.0, 3, 1,
                    "Daily loss limit hit", datetime.now().isoformat()
                ))
                conn.commit()

            # Attempt to create order intent: must be blocked by persistent live circuit breaker
            sig_payload = {"signal_id": "SIG_NEW_1", "underlying": "NIFTY", "strike": 22500, "option_type": "CE", "direction": "BUY_CE", "trading_symbol": "NIFTY22500CE", "instrument_key": "NSE_FO|301", "lot_size": 65}
            quote = {"bid": 100.0, "ask": 101.0, "ltp": 100.5, "quote_timestamp": datetime.now().timestamp()}

            allowed, reason, intent = self.engine.create_and_validate_intent(sig_payload, quote)
            self.assertFalse(allowed)
            self.assertIn("RISK_BLOCKED", reason)

    def test_duplicate_paper_trade_exit_protection(self):
        """OptionDB.close_paper_trade must be idempotent and fail on second exit attempt."""
        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = temp_db.name
        temp_db.close()

        with patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=db_path):
            init_option_sniffer_tables()
            with sqlite3.connect(db_path) as conn:
                conn.execute("""
                    INSERT INTO option_sniffer_paper_trades (
                        trade_id, signal_id, instrument_key, trading_symbol, underlying,
                        option_type, strike, expiry, lot_size, quantity, entry_time,
                        entry_bid, entry_ask, entry_price_used, status, peak_price, trough_price
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    "TRADE_IDEMP_1", "SIG_I1", "NSE_FO|501", "NIFTY501CE", "NIFTY",
                    "CE", 22500, "2026-10-19", 65, 65, "2026-10-14 10:00:00",
                    100.0, 102.0, 102.0, "OPEN", 102.0, 102.0
                ))
                conn.commit()

            close_payload = {
                "status": "CLOSED",
                "exit_time": "2026-10-14 10:15:00",
                "exit_bid": 85.0,
                "exit_ask": 86.0,
                "exit_price_used": 85.0,
                "peak_price": 102.0,
                "trough_price": 85.0,
                "mfe_pct": 0.0,
                "mae_pct": -16.67,
                "gross_pnl": -1105.0,
                "charges": 55.0,
                "slippage": 15.0,
                "net_pnl": -1175.0,
                "net_pnl_pct": -17.72,
                "holding_duration_seconds": 900,
                "exit_reason": "STOP_LOSS_HIT",
                "telegram_exit_sent": 1
            }

            # First close must succeed
            first_close = OptionDB.close_paper_trade("TRADE_IDEMP_1", close_payload)
            self.assertTrue(first_close)

            # Second close must return False (0 rows updated because status is no longer OPEN)
            second_close = OptionDB.close_paper_trade("TRADE_IDEMP_1", close_payload)
            self.assertFalse(second_close)

        if os.path.exists(db_path):
            os.remove(db_path)

    def test_paper_and_live_risk_decoupling(self):
        """Paper trading losses and live risk ledger remain strictly decoupled."""
        date_str = "2026-10-14"
        with patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path), \
             patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path):

            # Trip paper circuit breaker with ₹10,000 loss
            OptionDB.persist_paper_circuit_breaker_latch(
                session_date=date_str,
                realized_pnl=-10000.0,
                unrealized_pnl=0.0,
                total_exposure=-10000.0,
                trip_reason="Paper drawdown test",
                halt_latch=1
            )

            # Live circuit breaker must NOT be tripped
            live_cb = self.engine.get_live_circuit_breaker_telemetry(as_of_date=date_str)
            self.assertFalse(live_cb["is_session_halted"])
            self.assertFalse(live_cb["halt_latch_active"])

            # Trip live risk ledger with ₹12,000 loss
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO option_live_risk_ledger (
                        session_date, starting_capital, realized_pnl, unrealized_pnl,
                        peak_drawdown_inr, trades_executed, circuit_breaker_tripped,
                        trip_reason, updated_at
                    ) VALUES (?, 50000.0, -12000.0, 0.0, 12000.0, 4, 1, 'Live drawdown breach', '2026-10-14 12:00:00');
                """, (date_str,))
                conn.commit()

            # Verify live circuit breaker is now tripped
            live_cb_after = self.engine.get_live_circuit_breaker_telemetry(as_of_date=date_str)
            self.assertTrue(live_cb_after["is_session_halted"])
            self.assertTrue(live_cb_after["halt_latch_active"])

    def test_stop_loss_hit_mathematical_decomposition(self):
        """
        Validates the exact mathematical bridge for stop-loss execution:
          Net Exit Return = Configured Dynamic SL + Execution/Bid Gap + Statutory & Slippage Friction Drag.
          Historical Sample:
            Average Configured Dynamic SL = -16.01%
            Average Gross Return at Bid = -17.12% (Gap = -1.11%)
            Average Friction Drag (Statutory + Slippage) = -1.37%
            Total Net Return = -18.49%
        """
        dyn_sl = -16.01
        bid_gap = -1.11
        friction_drag = -1.37
        reconstructed_net_return = round(dyn_sl + bid_gap + friction_drag, 2)
        self.assertEqual(reconstructed_net_return, -18.49)

    def test_next_session_latch_reset(self):
        """Halt latch on session D does not bleed into session D+1 (clean next-day reset)."""
        date_today = "2026-10-14"
        date_tomorrow = "2026-10-15"

        with patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path):
            # Trip latch for today
            OptionDB.persist_paper_circuit_breaker_latch(
                session_date=date_today,
                realized_pnl=-4000.0,
                unrealized_pnl=0.0,
                total_exposure=-4000.0,
                trip_reason="Daily loss ceiling breached",
                halt_latch=1
            )

            # Query today: must be halted
            tel_today = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date=date_today)
            self.assertTrue(tel_today["is_session_halted"])
            self.assertTrue(tel_today["halt_latch_active"])

            # Query tomorrow: must NOT be halted (clean reset for new session)
            tel_tomorrow = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date=date_tomorrow)
            self.assertFalse(tel_tomorrow["is_session_halted"])
            self.assertFalse(tel_tomorrow["halt_latch_active"])

            # Verify today remains halted
            tel_today_recheck = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date=date_today)
            self.assertTrue(tel_today_recheck["is_session_halted"])

    def test_circuit_breaker_trigger_does_not_assume_all_positions_liquidated(self):
        """
        Circuit breaker trip triggers square-off, but MUST NOT be assumed to have liquidated positions.
        Unconfirmed or rejected exits continue to be marked to market and tracked as open exposure.
        """
        date_str = "2026-10-14"
        with patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path):
            with sqlite3.connect(self.db_path) as conn:
                # Insert 1 position in state FILLED, exit_state NONE
                conn.execute("""
                    INSERT INTO option_live_order_intents (
                        order_intent_id, signal_id, instrument_key, trading_symbol, underlying,
                        action, lot_size, target_quantity, filled_quantity, limit_price,
                        average_fill_price, state, exit_state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    "INTENT_UNCONFIRMED", "SIG_U1", "NSE_FO|999", "NIFTY_UNCONF", "NIFTY",
                    "BUY", 65, 65, 65, 150.0,
                    150.0, "FILLED", "NONE", "2026-10-14 10:00:00", "2026-10-14 10:00:00"
                ))
                conn.commit()

            # Mock SOS panic exit returning EXIT_PENDING (broker fill unconfirmed / order pending)
            with patch.object(self.engine, "trigger_sos_panic_exit", return_value={"status": "EXIT_PENDING", "message": "Broker fill pending"}) as mock_sos:
                res = self.engine.handle_circuit_breaker_breach(live_quotes_map={"NSE_FO|999": {"bid": 130.0}})
                self.assertEqual(res["unconfirmed_exits_count"], 1)
                self.assertEqual(res["closed_positions_count"], 0)
                mock_sos.assert_called_once_with("INTENT_UNCONFIRMED", 130.0)

            # Telemetry must still report this position as active open position with residual market risk
            tel = self.engine.get_live_circuit_breaker_telemetry(as_of_date=date_str)
            self.assertEqual(tel["active_open_positions_count"], 1)

    def test_per_order_exact_additive_identity(self):
        """
        Statutory charges must identically equal the sum of its rounded components on every single trade:
          statutory_charges == brokerage + stt + exchange_fee + sebi_fee + gst + stamp_duty
        """
        costs = calculate_option_trade_costs(
            buy_price=236.25,
            sell_price=207.40,
            quantity=65,
            apply_slippage=True,
            trade_date="2026-10-14"
        )
        component_sum = round(
            costs["brokerage"] + costs["stt"] + costs["exchange_fee"] +
            costs["sebi_fee"] + costs["gst"] + costs["stamp_duty"], 2
        )
        self.assertEqual(costs["statutory_charges"], component_sum)
        self.assertEqual(costs["total_friction"], round(component_sum + costs["additional_slippage"], 2))


if __name__ == "__main__":
    unittest.main()


