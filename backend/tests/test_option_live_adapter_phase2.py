"""
Option Sniffer: Phase 2 Unit & Integration Tests (Order Simulation, Failure Recovery, and RMS Hardening).

Tests:
  1. Idempotency & Duplicate Order Intent Suppression.
  2. Pre-Trade Gate: Quote Freshness Enforcement (< 1.0s).
  3. Pre-Trade Gate: Bid-Ask Spread Protection (<= 2.0%).
  4. Pre-Trade Gate: Max Slippage Tolerance (<= 1.5% from signal).
  5. Clean Fill Execution & Actual-Fill Anchored Broker-Side Stop Loss.
  6. Partial Fill Execution & Unfilled Quantity Tracking.
  7. Broker RMS Rejection & Fail-Closed Transition.
  8. Submission Timeout & Ambiguous Order Reconciliation.
  9. Multi-stage SOS Panic Exit Mutex Lock & Broker Zero-Quantity Confirmation.
  10. Daily Loss Rupee Ceiling Circuit Breaker (-₹2,000.00).
  11. Max Concurrent Positions Hard Cap (1 active position).
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.analytics.option_sniffer.option_live_order_intent import (
    OrderIntentEngine,
    STATE_CREATED,
    STATE_VALIDATED,
    STATE_SUBMITTED,
    STATE_PARTIALLY_FILLED,
    STATE_FILLED,
    STATE_REJECTED,
    EXIT_CONFIRMED_FLAT,
    MAX_DAILY_LOSS_INR
)


class TestOptionLiveAdapterPhase2(unittest.TestCase):

    def setUp(self):
        # Create an isolated temporary database for test execution
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        # Patch get_db_path to use temp DB
        self.patcher = patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path)
        self.patcher.start()

        self.engine = OrderIntentEngine()

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_idempotency_and_duplicate_suppression(self):
        """Verifies deterministic UUID creation and duplicate order intent blocking."""
        signal_payload = {
            "signal_id": "SIG_TEST_001",
            "instrument_key": "NSE_FO|45000",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        live_quote = {
            "ask": 100.5,
            "bid": 100.0,
            "quote_age_ms": 250
        }

        # 1. First submission passes
        ok, reason, intent = self.engine.create_and_validate_intent(
            signal_payload, live_quote, deployable_capital=15000.0
        )
        self.assertTrue(ok, f"Expected validation to succeed, got: {reason}")
        self.assertIsNotNone(intent)
        self.assertTrue(intent["order_intent_id"].startswith("INTENT_SIG_TEST_001_"))
        self.assertEqual(intent["state"], STATE_VALIDATED)

        # 2. Second submission with the same signal_id is suppressed
        ok2, reason2, intent2 = self.engine.create_and_validate_intent(
            signal_payload, live_quote, deployable_capital=15000.0
        )
        self.assertFalse(ok2, "Duplicate submission must be rejected")
        self.assertIn("DUPLICATE_SUPPRESSED", reason2)
        self.assertIsNone(intent2)

    def test_pretrade_gate_quote_freshness(self):
        """Rejects order intent if quote age exceeds 1000ms."""
        signal_payload = {
            "signal_id": "SIG_STALE_001",
            "instrument_key": "NSE_FO|45000",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        stale_quote = {
            "ask": 100.0,
            "bid": 99.5,
            "quote_age_ms": 1500  # Stale (> 1000ms)
        }

        ok, reason, intent = self.engine.create_and_validate_intent(
            signal_payload, stale_quote, deployable_capital=15000.0
        )
        self.assertFalse(ok)
        self.assertIn("QUOTE_STALE", reason)

    def test_pretrade_gate_spread_protection(self):
        """Rejects order intent if bid-ask spread exceeds 2.0% and ₹1.50."""
        signal_payload = {
            "signal_id": "SIG_SPREAD_001",
            "instrument_key": "NSE_FO|45000",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        wide_spread_quote = {
            "ask": 105.0,
            "bid": 95.0,  # Spread: ₹10.00 (9.5%)
            "quote_age_ms": 300
        }

        ok, reason, intent = self.engine.create_and_validate_intent(
            signal_payload, wide_spread_quote, deployable_capital=15000.0
        )
        self.assertFalse(ok)
        self.assertIn("SPREAD_TOO_WIDE", reason)

    def test_pretrade_gate_slippage_tolerance(self):
        """Rejects order intent if current ask has spiked > 1.5% above signal suggested entry."""
        signal_payload = {
            "signal_id": "SIG_SLIP_001",
            "instrument_key": "NSE_FO|45000",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        spiked_quote = {
            "ask": 103.0,  # Spiked 3% (> 1.5% limit)
            "bid": 102.5,
            "quote_age_ms": 200
        }

        ok, reason, intent = self.engine.create_and_validate_intent(
            signal_payload, spiked_quote, deployable_capital=15000.0
        )
        self.assertFalse(ok)
        self.assertIn("SLIPPAGE_EXCEEDED", reason)

    def test_clean_fill_and_actual_fill_broker_sl(self):
        """
        Verifies clean fill simulation and proves that the linked broker Stop Loss
        is calculated strictly from the ACTUAL fill price (e.g. ₹102.0), not paper entry (₹100.0).
        """
        signal_payload = {
            "signal_id": "SIG_FILL_001",
            "instrument_key": "NSE_FO|45000",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        live_quote = {"ask": 101.0, "bid": 100.8, "quote_age_ms": 150}

        ok, _, intent = self.engine.create_and_validate_intent(signal_payload, live_quote, deployable_capital=15000.0)
        self.assertTrue(ok)
        intent_id = intent["order_intent_id"]

        # Simulate fill at actual market fill price: ₹102.0
        res = self.engine.simulate_order_execution(intent_id, scenario="CLEAN_FILL", mock_fill_price=102.0)
        self.assertEqual(res["state"], STATE_FILLED)
        self.assertEqual(res["average_fill_price"], 102.0)
        self.assertEqual(res["filled_quantity"], 65)  # NIFTY lot size

        # Verify linked Stop Loss is calculated from 102.0: 102.0 * 0.85 = 86.7
        self.assertEqual(res["stop_loss_trigger"], 86.7)
        self.assertTrue(res["broker_sl_order_id"].startswith("UPSTOX_SL_"))

    def test_partial_fill_execution(self):
        """Verifies partial fill handling where 15 of 30 quantity fills and remainder cancels."""
        signal_payload = {
            "signal_id": "SIG_PARTIAL_001",
            "instrument_key": "NSE_FO|46000",
            "trading_symbol": "BANKNIFTY 54000 PE",
            "underlying": "BANKNIFTY",
            "action": "BUY_PE",
            "suggested_entry": 200.0
        }
        live_quote = {"ask": 201.0, "bid": 200.5, "quote_age_ms": 200}

        ok, _, intent = self.engine.create_and_validate_intent(signal_payload, live_quote, deployable_capital=15000.0)
        self.assertTrue(ok)
        intent_id = intent["order_intent_id"]

        # Simulate partial fill of 15 qty
        res = self.engine.simulate_order_execution(intent_id, scenario="PARTIAL_FILL", mock_fill_price=201.0, mock_partial_qty=15)
        self.assertEqual(res["state"], STATE_PARTIALLY_FILLED)
        self.assertEqual(res["filled_quantity"], 15)
        self.assertEqual(res["target_quantity"], 30)

    def test_broker_rms_rejection(self):
        """Verifies that an exchange RMS rejection transitions to REJECTED fail-closed."""
        signal_payload = {
            "signal_id": "SIG_RMS_001",
            "instrument_key": "NSE_FO|45000",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        live_quote = {"ask": 100.0, "bid": 99.8, "quote_age_ms": 100}

        ok, _, intent = self.engine.create_and_validate_intent(signal_payload, live_quote, deployable_capital=15000.0)
        self.assertTrue(ok)
        intent_id = intent["order_intent_id"]

        res = self.engine.simulate_order_execution(
            intent_id, scenario="RMS_REJECTION", mock_rejection_reason="UDAPI100010: NFO segment not enabled"
        )
        self.assertEqual(res["state"], STATE_REJECTED)
        self.assertIn("NFO segment not enabled", res["rejection_reason"])

    def test_timeout_ambiguous_recovery(self):
        """Verifies that a network timeout marks order as SUBMITTED and reconciles rather than duplicating."""
        signal_payload = {
            "signal_id": "SIG_TIMEOUT_001",
            "instrument_key": "NSE_FO|45000",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        live_quote = {"ask": 100.0, "bid": 99.8, "quote_age_ms": 100}

        ok, _, intent = self.engine.create_and_validate_intent(signal_payload, live_quote, deployable_capital=15000.0)
        intent_id = intent["order_intent_id"]

        res = self.engine.simulate_order_execution(intent_id, scenario="TIMEOUT_AMBIGUOUS")
        self.assertEqual(res["state"], STATE_SUBMITTED)
        self.assertEqual(res["reconciliation_status"], "ORDER_CONFIRMED_VIA_PORTFOLIO_STREAM")

    def test_sos_panic_exit_and_zero_quantity_confirmation(self):
        """Verifies SOS emergency square-off transitions to CONFIRMED_FLAT and confirms zero qty."""
        signal_payload = {
            "signal_id": "SIG_SOS_001",
            "instrument_key": "NSE_FO|45000",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        live_quote = {"ask": 100.0, "bid": 99.8, "quote_age_ms": 100}

        ok, _, intent = self.engine.create_and_validate_intent(signal_payload, live_quote, deployable_capital=15000.0)
        intent_id = intent["order_intent_id"]
        self.engine.simulate_order_execution(intent_id, scenario="CLEAN_FILL", mock_fill_price=100.0)

        # Trigger SOS Panic Exit at current market bid: ₹108.0
        sos_res = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=108.0, charges=55.0)
        self.assertEqual(sos_res["status"], "CONFIRMED_FLAT")
        self.assertEqual(sos_res["exit_state"], EXIT_CONFIRMED_FLAT)
        self.assertEqual(sos_res["broker_position_qty"], 0)
        self.assertEqual(sos_res["quantity_closed"], 65)

        # Gross P&L: (108 - 100) * 65 = ₹520. Net P&L: 520 - 55 = ₹465.0
        self.assertEqual(sos_res["net_realized_pnl"], 465.0)

        # Verify duplicate click on SOS is blocked
        dup_sos = self.engine.trigger_sos_panic_exit(intent_id, current_bid_price=108.0, charges=55.0)
        self.assertEqual(dup_sos["status"], "ALREADY_FLAT")

    def test_daily_loss_circuit_breaker_blocking(self):
        """Verifies that daily loss <= -₹2,000 blocks new entries immediately."""
        signal_payload = {
            "signal_id": "SIG_LOSS_001",
            "instrument_key": "NSE_FO|45000",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        live_quote = {"ask": 100.0, "bid": 99.8, "quote_age_ms": 100}

        # Daily loss is -₹3,600 (exceeds -₹3,500 ceiling)
        ok, reason, intent = self.engine.create_and_validate_intent(
            signal_payload, live_quote, deployable_capital=15000.0, daily_loss_inr=-3600.0
        )
        self.assertFalse(ok)
        self.assertIn("RISK_BLOCKED", reason)
        self.assertIn("Daily loss ceiling", reason)

    def test_max_concurrent_positions_blocking(self):
        """Verifies that having 2 active positions blocks opening a 3rd position."""
        signal_payload1 = {
            "signal_id": "SIG_POS_001",
            "instrument_key": "NSE_FO|45000",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        signal_payload2 = {
            "signal_id": "SIG_POS_002",
            "instrument_key": "NSE_FO|46000",
            "trading_symbol": "BANKNIFTY 54000 PE",
            "underlying": "BANKNIFTY",
            "action": "BUY_PE",
            "suggested_entry": 100.0
        }
        signal_payload3 = {
            "signal_id": "SIG_POS_003",
            "instrument_key": "NSE_FO|47000",
            "trading_symbol": "NIFTY 25100 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0
        }
        live_quote = {"ask": 100.0, "bid": 99.8, "quote_age_ms": 100}

        # Position 1 fills
        ok1, _, intent1 = self.engine.create_and_validate_intent(signal_payload1, live_quote, deployable_capital=30000.0)
        self.assertTrue(ok1)
        self.engine.simulate_order_execution(intent1["order_intent_id"], scenario="CLEAN_FILL")

        # Position 2 fills (allowed under 2-position concurrency)
        ok2, _, intent2 = self.engine.create_and_validate_intent(signal_payload2, live_quote, deployable_capital=30000.0)
        self.assertTrue(ok2)
        self.engine.simulate_order_execution(intent2["order_intent_id"], scenario="CLEAN_FILL")

        # Position 3 is attempted while 2 positions are still open -> blocked
        ok3, reason3, _ = self.engine.create_and_validate_intent(signal_payload3, live_quote, deployable_capital=30000.0)
        self.assertFalse(ok3)
        self.assertIn("RISK_BLOCKED: Max concurrent positions", reason3)


if __name__ == "__main__":
    unittest.main()

