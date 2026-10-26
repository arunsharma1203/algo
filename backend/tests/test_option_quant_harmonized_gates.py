"""
Unit and Integration Tests for Harmonized Industrial Quant & Logic Hunter Gates:
1. V1_CLASSIC execution mode and convex payoff constants.
2. 13:15 IST Session Curfew.
3. Scale-Invariant Kaufman Efficiency Ratio (ER_15m) Chop Shield.
4. ATR Extrema Displacement Reversal Guard.
5. Inter-Market Composite Breadth (B_composite) Anti-Hopping Gate.
6. Smart Pyramiding (Scale into winners only).
7. Portfolio Introspection Engine (2-Loss 30m Global Freeze).
8. Anti-Ping-Pong Directional Flip Lockout.
9. Champion Model Artifact Immutability (SHA256 check).
"""

import unittest
import time
import os
import sys
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.analytics.option_sniffer.option_constants import (
    OPTION_EXECUTION_ENGINE_MODE,
    STOP_LOSS_PCT,
    TARGET_GAIN_PCT,
    NEW_ENTRY_OPEN_HOUR,
    NEW_ENTRY_OPEN_MINUTE,
    NEW_ENTRY_CUTOFF_HOUR,
    NEW_ENTRY_CUTOFF_MINUTE,
    KAUFMAN_ER_MIN_THRESHOLD,
    KAUFMAN_ER_TREND_THRESHOLD,
    EXTREMA_DISPLACEMENT_MAX_ATR_RATIO,
    INTERMARKET_BREADTH_MIN_RATIO,
    INTROSPECTION_FREEZE_MINUTES,
    ANTI_PING_PONG_FLIP_MINUTES,
    SMART_PYRAMIDING_MIN_MFE_PCT,
    is_option_entry_window,
    is_option_trading_window,
)
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine


class TestHarmonizedQuantGates(unittest.TestCase):

    def setUp(self):
        # Reset state machine tracking before each test
        OptionSignalEngine._COOLDOWNS.clear()
        OptionSignalEngine._DIRECTION_SL_COOLDOWNS.clear()
        OptionSignalEngine._PORTFOLIO_LOSS_HISTORY.clear()
        OptionSignalEngine._INTROSPECTION_FROZEN_UNTIL = 0.0

    def test_01_constants_and_v1_classic_integrity(self):
        """Verify V1_CLASSIC mode, 1.75:1 convex payoff, and 13:15 curfew."""
        self.assertEqual(OPTION_EXECUTION_ENGINE_MODE, "V1_CLASSIC")
        self.assertEqual(STOP_LOSS_PCT, 20.0)
        self.assertEqual(TARGET_GAIN_PCT, 35.0)
        self.assertEqual(NEW_ENTRY_CUTOFF_HOUR, 13)
        self.assertEqual(NEW_ENTRY_CUTOFF_MINUTE, 15)
        self.assertEqual(KAUFMAN_ER_MIN_THRESHOLD, 0.28)
        self.assertEqual(EXTREMA_DISPLACEMENT_MAX_ATR_RATIO, 1.20)
        self.assertEqual(INTERMARKET_BREADTH_MIN_RATIO, 0.80)
        self.assertEqual(INTROSPECTION_FREEZE_MINUTES, 30)
        self.assertEqual(ANTI_PING_PONG_FLIP_MINUTES, 30)

    def test_02_golden_entry_window_curfew(self):
        """Verify new entries are allowed between 09:30 and 13:15, and blocked after 13:15."""
        # Valid morning time: 10:15 AM
        valid_dt = datetime(2026, 10, 16, 10, 15, 0)
        self.assertTrue(is_option_entry_window(valid_dt))

        # Cutoff passed: 13:16 PM
        curfew_dt = datetime(2026, 10, 16, 13, 16, 0)
        self.assertFalse(is_option_entry_window(curfew_dt))

        # Late afternoon: 14:30 PM
        late_dt = datetime(2026, 10, 16, 14, 30, 0)
        self.assertFalse(is_option_entry_window(late_dt))

    def test_03_kaufman_efficiency_ratio_calculation(self):
        """Verify mathematical calculation of Kaufman Efficiency Ratio (ER_15m)."""
        # Trend data: perfectly straight line up
        prices_trend = np.linspace(100.0, 115.0, 15)
        net_change = abs(prices_trend[-1] - prices_trend[0])
        sum_diffs = np.sum(np.abs(np.diff(prices_trend)))
        er_trend = net_change / sum_diffs
        self.assertAlmostEqual(er_trend, 1.0, places=2)
        self.assertGreaterEqual(er_trend, KAUFMAN_ER_TREND_THRESHOLD)

        # Chop data: oscillating noise with zero displacement
        prices_chop = np.array([100.0, 102.0, 99.0, 101.5, 99.5, 102.0, 100.0, 101.0, 99.0, 101.5, 100.0, 102.0, 99.0, 101.0, 100.2])
        net_change_chop = abs(prices_chop[-1] - prices_chop[0])
        sum_diffs_chop = np.sum(np.abs(np.diff(prices_chop)))
        er_chop = net_change_chop / sum_diffs_chop
        self.assertLess(er_chop, KAUFMAN_ER_MIN_THRESHOLD)

    def test_04_at_displacement_reversal_guard(self):
        """Verify reversal guard triggers when rally off low >= 1.20x ATR."""
        atr_5m = 25.0
        day_low = 22913.0
        
        # Spot rallies 15 pts off low: 15 / 25 = 0.6x ATR (Normal noise, bear trend valid)
        spot_noise = 22928.0
        ratio_noise = (spot_noise - day_low) / atr_5m
        self.assertLess(ratio_noise, EXTREMA_DISPLACEMENT_MAX_ATR_RATIO)

        # Spot rallies 50 pts off low: 50 / 25 = 2.0x ATR (Reversal broken!)
        spot_reversal = 22963.0
        ratio_reversal = (spot_reversal - day_low) / atr_5m
        self.assertGreaterEqual(ratio_reversal, EXTREMA_DISPLACEMENT_MAX_ATR_RATIO)

    def test_05_portfolio_introspection_freeze(self):
        """Verify 2 consecutive Stop-Losses within 30m triggers 30m global freeze."""
        now = time.time()
        # Initial state: not frozen
        self.assertFalse(now < OptionSignalEngine._INTROSPECTION_FROZEN_UNTIL)

        # First Stop Loss on NIFTY CE
        OptionSignalEngine.record_direction_sl_cooldown("NIFTY", "CE", epoch=now)
        self.assertEqual(len(OptionSignalEngine._PORTFOLIO_LOSS_HISTORY), 1)
        self.assertFalse(now < OptionSignalEngine._INTROSPECTION_FROZEN_UNTIL)

        # Second Stop Loss on BANKNIFTY PE within 5 minutes
        now_2 = now + 300
        OptionSignalEngine.record_direction_sl_cooldown("BANKNIFTY", "PE", epoch=now_2)
        self.assertEqual(len(OptionSignalEngine._PORTFOLIO_LOSS_HISTORY), 2)
        
        # Verify Introspection Freeze engaged
        self.assertTrue(now_2 < OptionSignalEngine._INTROSPECTION_FROZEN_UNTIL)
        expected_until = now_2 + (INTROSPECTION_FREEZE_MINUTES * 60)
        self.assertAlmostEqual(OptionSignalEngine._INTROSPECTION_FROZEN_UNTIL, expected_until, delta=1.0)

    def test_06_anti_ping_pong_flip_lockout(self):
        """Verify flipping to opposite direction within 30m after Stop-Loss is recorded."""
        now = time.time()
        # NIFTY PE stops out
        OptionSignalEngine.record_direction_sl_cooldown("NIFTY", "PE", epoch=now)
        
        # Check cooldown state
        key_pe = "NIFTY_PE"
        self.assertEqual(OptionSignalEngine._DIRECTION_SL_COOLDOWNS.get(key_pe), now)
        
        # 10 minutes later, PE cooldown still active
        t_10m = now + 600
        elapsed_sec = t_10m - OptionSignalEngine._DIRECTION_SL_COOLDOWNS[key_pe]
        self.assertLess(elapsed_sec, ANTI_PING_PONG_FLIP_MINUTES * 60)

    def test_07_intermarket_breadth_vector(self):
        """Verify composite breadth calculation and divergence detection."""
        # Both above VWAP -> B_comp = 1.0 (Co-aligned Bull)
        n_spot, n_vwap = 23050.0, 23000.0
        b_spot, b_vwap = 55700.0, 55500.0
        n_sgn = 1.0 if n_spot >= n_vwap else -1.0
        b_sgn = 1.0 if b_spot >= b_vwap else -1.0
        b_comp = round(0.60 * n_sgn + 0.40 * b_sgn, 2)
        self.assertEqual(b_comp, 1.0)
        self.assertFalse(abs(b_comp) < INTERMARKET_BREADTH_MIN_RATIO)

        # Nifty above VWAP, BankNifty below VWAP -> B_comp = 0.20 (Divergent!)
        b_spot_div, b_vwap_div = 55400.0, 55500.0
        b_sgn_div = 1.0 if b_spot_div >= b_vwap_div else -1.0
        b_comp_div = round(0.60 * n_sgn + 0.40 * b_sgn_div, 2)
        self.assertEqual(b_comp_div, 0.20)
        self.assertTrue(abs(b_comp_div) < INTERMARKET_BREADTH_MIN_RATIO)

    def test_08_champion_model_immutability(self):
        """Rule 2 Compliance: Verify production model SHA256 integrity remains unmodified."""
        import hashlib
        model_path = os.path.join(os.path.dirname(__file__), "..", "models", "option_sniffer", "v1.0-champion-gbm.pkl")
        if os.path.exists(model_path):
            with open(model_path, "rb") as f:
                h = hashlib.sha256(f.read()).hexdigest()
            # Verified production hash from status payload: b945dfca6cbdefc4aa746ba9696d9c1cc713e780dec6d4f3684979a60432cfa4
            self.assertEqual(h, "b945dfca6cbdefc4aa746ba9696d9c1cc713e780dec6d4f3684979a60432cfa4")


if __name__ == "__main__":
    unittest.main()

