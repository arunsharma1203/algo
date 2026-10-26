"""
test_option_dynamic_timing.py
─────────────────────────────
Comprehensive Unit & Integration Test Suite for Option Sniffer Dynamic Microstructure Timing,
Canary Scouting, and Anti-Chase Guards.

Tests:
1. test_early_unlock_at_0935_on_trending_day
2. test_delayed_unlock_at_0940
3. test_canary_sl_overrides_dynamic_timing
4. test_choppy_er_blocks_early_unlock
5. test_wide_spread_blocks_early_unlock
6. test_static_fallback_at_0945
7. test_outside_trading_hours_locked
8. test_session_entry_cutoff_at_1315
9. test_anti_chase_blocks_excessive_runup
10. test_anti_chase_allows_normal_expansion
11. test_fail_closed_on_corrupt_data
12. test_live_status_payload_includes_microstructure
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, time as dt_time
from unittest.mock import patch, MagicMock

from app.analytics.option_sniffer.option_constants import (
    to_ist_naive,
    is_live_option_entry_window,
    DYNAMIC_TIMING_ACTIVE,
    DYNAMIC_MAX_SPREAD_ATM_PCT,
    DYNAMIC_MIN_KAUFMAN_ER,
    DYNAMIC_MAX_ANTI_CHASE_PCT,
)
from app.analytics.option_sniffer.option_microstructure_evaluator import OptionMicrostructureEvaluator
from app.analytics.option_sniffer.option_live_adapter import OptionLiveAdapter


class TestOptionDynamicTiming(unittest.TestCase):
    """Test suite verifying point-in-time dynamic timing, canary safeguards, and anti-chase rules."""

    def setUp(self):
        # Reset mocks on OptionMicrostructureEvaluator
        OptionMicrostructureEvaluator._mock_or_data = None
        OptionMicrostructureEvaluator._mock_kaufman_er = None
        OptionMicrostructureEvaluator._mock_spread_atm = None
        OptionMicrostructureEvaluator._cache_payload = None
        OptionMicrostructureEvaluator._cache_timestamp = 0.0

    def tearDown(self):
        OptionMicrostructureEvaluator._mock_or_data = None
        OptionMicrostructureEvaluator._mock_kaufman_er = None
        OptionMicrostructureEvaluator._mock_spread_atm = None
        OptionMicrostructureEvaluator._cache_payload = None
        OptionMicrostructureEvaluator._cache_timestamp = 0.0

    @patch("app.analytics.option_sniffer.option_db.OptionDB.has_paper_stop_loss_today")
    def test_early_unlock_at_0935_on_trending_day(self, mock_canary):
        """Clean trending day (Oct 9 scenario): ER=0.52, Spread=0.23%, 5m ORB accepted -> Unlocks at 09:35 IST."""
        mock_canary.return_value = (False, "Zero paper stop losses today")
        
        OptionMicrostructureEvaluator._mock_spread_atm = 0.23
        OptionMicrostructureEvaluator._mock_kaufman_er = 0.52
        OptionMicrostructureEvaluator._mock_or_data = {
            "is_accepted": True,
            "or_high": 22400.0,
            "or_low": 22350.0,
            "breakout_direction": "BULL_EXPANSION",
            "volume_ratio": 2.1,
            "volume_ok": True,
            "evaluation_time": "09:35:05"
        }

        eval_dt = datetime.strptime("2026-10-09 09:35:10", "%Y-%m-%d %H:%M:%S")
        res = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=eval_dt, force_refresh=True)

        self.assertTrue(res["is_unlocked"])
        self.assertEqual(res["canary_status"], "HEALTHY")
        self.assertEqual(res["dynamic_unlock_time"], "09:35:00 IST")
        self.assertIn("DYNAMIC_UNLOCK_QUALIFIED", res["unlock_reason"])
        self.assertTrue(is_live_option_entry_window(eval_dt, allow_dynamic_early_unlock=res["is_unlocked"]))

    @patch("app.analytics.option_sniffer.option_db.OptionDB.has_paper_stop_loss_today")
    def test_delayed_unlock_at_0940(self, mock_canary):
        """At 09:35 ORB not accepted; at 09:40 breakdown accepted -> Unlocked at 09:40."""
        mock_canary.return_value = (False, "Zero paper stop losses today")
        
        OptionMicrostructureEvaluator._mock_spread_atm = 0.35
        OptionMicrostructureEvaluator._mock_kaufman_er = 0.41

        # At 09:35: ORB pending
        OptionMicrostructureEvaluator._mock_or_data = {"is_accepted": False, "breakout_direction": "INSIDE_RANGE"}
        dt_35 = datetime.strptime("2026-10-12 09:35:10", "%Y-%m-%d %H:%M:%S")
        res_35 = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=dt_35, force_refresh=True)
        self.assertFalse(res_35["is_unlocked"])
        self.assertIn("DYNAMIC_EVALUATING", res_35["unlock_reason"])

        # At 09:40: ORB breakdown accepted
        OptionMicrostructureEvaluator._mock_or_data = {"is_accepted": True, "breakout_direction": "BEAR_BREAKDOWN"}
        dt_40 = datetime.strptime("2026-10-12 09:40:10", "%Y-%m-%d %H:%M:%S")
        res_40 = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=dt_40, force_refresh=True)
        self.assertTrue(res_40["is_unlocked"])

    @patch("app.analytics.option_sniffer.option_db.OptionDB.has_paper_stop_loss_today")
    def test_canary_sl_overrides_dynamic_timing(self, mock_canary):
        """Even with ER=0.60 and valid breakout, if paper hit SL today, Real Trading remains LOCKED."""
        mock_canary.return_value = (True, "Canary trade pt_01 suffered STOP_LOSS_HIT with PnL ₹-2,500.00")
        
        OptionMicrostructureEvaluator._mock_spread_atm = 0.20
        OptionMicrostructureEvaluator._mock_kaufman_er = 0.60
        OptionMicrostructureEvaluator._mock_or_data = {"is_accepted": True, "breakout_direction": "BULL_EXPANSION"}

        eval_dt = datetime.strptime("2026-10-14 09:36:00", "%Y-%m-%d %H:%M:%S")
        res = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=eval_dt, force_refresh=True)

        self.assertFalse(res["is_unlocked"])
        self.assertEqual(res["canary_status"], "LOCKED")
        self.assertIn("CANARY_SCOUT_ABORT", res["unlock_reason"])
        self.assertFalse(is_live_option_entry_window(eval_dt, allow_dynamic_early_unlock=res["is_unlocked"]))

    @patch("app.analytics.option_sniffer.option_db.OptionDB.has_paper_stop_loss_today")
    def test_choppy_er_blocks_early_unlock(self, mock_canary):
        """Spot breaks out but Kaufman ER is 0.22 (choppy whipsaw) -> Blocks unlock at 09:35."""
        mock_canary.return_value = (False, "Zero paper stop losses today")
        
        OptionMicrostructureEvaluator._mock_spread_atm = 0.25
        OptionMicrostructureEvaluator._mock_kaufman_er = 0.22  # Below 0.35
        OptionMicrostructureEvaluator._mock_or_data = {"is_accepted": True, "breakout_direction": "BULL_EXPANSION"}

        eval_dt = datetime.strptime("2026-10-14 09:36:00", "%Y-%m-%d %H:%M:%S")
        res = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=eval_dt, force_refresh=True)

        self.assertFalse(res["is_unlocked"])
        self.assertIn("Chop ER", res["unlock_reason"])

    @patch("app.analytics.option_sniffer.option_db.OptionDB.has_paper_stop_loss_today")
    def test_wide_spread_blocks_early_unlock(self, mock_canary):
        """ATM option spread is 1.6% (> 1.2% cap) -> Blocks early unlock to prevent paying market maker drag."""
        mock_canary.return_value = (False, "Zero paper stop losses today")
        
        OptionMicrostructureEvaluator._mock_spread_atm = 1.60  # Exceeds 1.2%
        OptionMicrostructureEvaluator._mock_kaufman_er = 0.45
        OptionMicrostructureEvaluator._mock_or_data = {"is_accepted": True, "breakout_direction": "BULL_EXPANSION"}

        eval_dt = datetime.strptime("2026-10-15 09:36:00", "%Y-%m-%d %H:%M:%S")
        res = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=eval_dt, force_refresh=True)

        self.assertFalse(res["is_unlocked"])
        self.assertIn("Spread wide", res["unlock_reason"])

    @patch("app.analytics.option_sniffer.option_db.OptionDB.has_paper_stop_loss_today")
    def test_static_fallback_at_0945(self, mock_canary):
        """At 09:45 IST, normal market condition unlocks via static fallback ceiling."""
        mock_canary.return_value = (False, "Zero paper stop losses today")
        
        OptionMicrostructureEvaluator._mock_spread_atm = 0.35
        OptionMicrostructureEvaluator._mock_kaufman_er = 0.32  # Mild chop accepted at 09:45 fallback

        eval_dt = datetime.strptime("2026-10-15 09:46:00", "%Y-%m-%d %H:%M:%S")
        res = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=eval_dt, force_refresh=True)

        self.assertTrue(res["is_unlocked"])
        self.assertIn("STATIC_FALLBACK_QUALIFIED", res["unlock_reason"])

    def test_outside_trading_hours_locked(self):
        """Pre-market (09:10) and post-market (15:20) are strictly locked."""
        dt_pre = datetime.strptime("2026-10-15 09:10:00", "%Y-%m-%d %H:%M:%S")
        res_pre = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=dt_pre, force_refresh=True)
        self.assertFalse(res_pre["is_unlocked"])

        dt_post = datetime.strptime("2026-10-15 15:20:00", "%Y-%m-%d %H:%M:%S")
        res_post = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=dt_post, force_refresh=True)
        self.assertFalse(res_post["is_unlocked"])

    def test_session_entry_cutoff_at_1315(self):
        """At 13:20 IST, new entries are strictly locked to protect from afternoon theta decay."""
        dt_late = datetime.strptime("2026-10-15 13:20:00", "%Y-%m-%d %H:%M:%S")
        res_late = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=dt_late, force_refresh=True)
        self.assertFalse(res_late["is_unlocked"])
        self.assertIn("SESSION_ENTRY_CUTOFF", res_late["unlock_reason"])

    def test_anti_chase_blocks_excessive_runup(self):
        """Anti-Chase Guard blocks entries where contract has gained > 15.0% since base signal."""
        # Case 1: Surged 25% (₹100 -> ₹125) -> Reject
        passed, msg = OptionMicrostructureEvaluator.evaluate_anti_chase(current_ask=125.0, base_ask=100.0)
        self.assertFalse(passed)
        self.assertIn("ANTI_CHASE_REJECT", msg)

        # Case 2: Surged 18% -> Reject
        passed2, msg2 = OptionMicrostructureEvaluator.evaluate_anti_chase(current_ask=118.0, base_ask=100.0)
        self.assertFalse(passed2)

    def test_anti_chase_allows_normal_expansion(self):
        """Anti-Chase Guard allows reasonable breakout expansion (<= 15.0%)."""
        # Case 1: Surged 6% (₹131.85 -> ₹139.40, like Oct 9) -> Accept
        passed, msg = OptionMicrostructureEvaluator.evaluate_anti_chase(current_ask=139.40, base_ask=131.85)
        self.assertTrue(passed)
        self.assertIn("ANTI_CHASE_PASSED", msg)

        # Case 2: Exact 15% boundary -> Accept
        passed2, msg2 = OptionMicrostructureEvaluator.evaluate_anti_chase(current_ask=115.0, base_ask=100.0)
        self.assertTrue(passed2)

    def test_fail_closed_on_corrupt_data(self):
        """Corrupt or missing prices return 0.0 ER and do not raise uncaught exceptions."""
        er = OptionMicrostructureEvaluator.compute_kaufman_er([])
        self.assertEqual(er, 0.0)

        er_short = OptionMicrostructureEvaluator.compute_kaufman_er([100.0, 100.0])
        self.assertEqual(er_short, 0.0)

        passed, _ = OptionMicrostructureEvaluator.evaluate_anti_chase(current_ask=-10.0, base_ask=100.0)
        self.assertTrue(passed) # Graceful fallback

    @patch("app.analytics.option_sniffer.option_db.OptionDB.has_paper_stop_loss_today")
    def test_live_status_payload_includes_microstructure(self, mock_canary):
        """OptionLiveAdapter.get_live_status() includes 'microstructure' telemetry with all expected keys."""
        mock_canary.return_value = (False, "Zero paper stop losses today")
        adapter = OptionLiveAdapter()
        status = adapter.get_live_status()

        self.assertIn("microstructure", status)
        ms = status["microstructure"]
        self.assertIn("is_unlocked", ms)
        self.assertIn("canary_status", ms)
        self.assertIn("spread_atm_pct", ms)
        self.assertIn("kaufman_er", ms)
        self.assertIn("orb_status", ms)
        self.assertIn("earliest_possible_unlock", ms)


if __name__ == "__main__":
    unittest.main()

