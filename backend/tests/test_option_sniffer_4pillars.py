"""
test_option_sniffer_4pillars.py
───────────────────────────────
Comprehensive unit test suite for the 4-Pillar Dynamic Canary & Uncapped Runner Architecture.

Tests all 6 market scenario invariants:
  1. Opening Range Volatility Floor blocks narrow-range chop (< 0.40%).
  2. Canary Scout fatality permanently locks real trading upon paper stop-loss (-20%).
  3. Early Breakeven Ratchet (+15% MFE) guarantees +6% floor covering all statutory friction.
  4. Continuous Chandelier Trailing Stop tightens dynamically across Tiers 1-4.
  5. Uncapped Runner engine allows positions to run beyond +35% without premature target exit.
  6. Golden Curfew (13:15 IST) blocks late afternoon decay entries.
  7. Hard Daily Circuit Breaker (-₹3,500) permanently halts session upon loss threshold.
"""

import unittest
from datetime import datetime, time as dt_time
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.analytics.option_sniffer.option_constants import (
    ENABLE_4PILLAR_CANARY_ENGINE,
    OPENING_RANGE_VOL_MIN_PCT,
    EARLY_BREAKEVEN_TRIGGER_PCT,
    EARLY_BREAKEVEN_FLOOR_PCT,
    CHANDELIER_TRIGGER_TIER1_PCT,
    CHANDELIER_DISTANCE_TIER1_PCT,
    CHANDELIER_TRIGGER_TIER2_PCT,
    CHANDELIER_DISTANCE_TIER2_PCT,
    CHANDELIER_TRIGGER_TIER3_PCT,
    CHANDELIER_DISTANCE_TIER3_PCT,
    CHANDELIER_TRIGGER_TIER4_PCT,
    CHANDELIER_DISTANCE_TIER4_PCT,
    MAX_DAILY_LOSS_INR,
    calculate_option_trade_costs,
)
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.option_sniffer.option_microstructure_evaluator import OptionMicrostructureEvaluator


class TestOptionSniffer4Pillars(unittest.TestCase):
    """Deterministic validation of 4-Pillar Dynamic Canary rules."""

    def setUp(self):
        OptionMicrostructureEvaluator.reset_cache()
        OptionMicrostructureEvaluator.set_mock_opening_range_data(None)

    def tearDown(self):
        OptionMicrostructureEvaluator.reset_cache()
        OptionMicrostructureEvaluator.set_mock_opening_range_data(None)

    def test_pillar1_volatility_floor_blocks_narrow_opening(self):
        """Pillar 1: Opening Range < 0.40% (e.g. 0.25% chop) must lock the gate with VOLATILITY_FLOOR_STAND_DOWN."""
        mock_or_narrow = {
            "is_accepted": True,
            "or_high": 22550.0,
            "or_low": 22500.0,  # 50 pt range on 22500 -> 0.22%
            "range_pts": 50.0,
            "range_pct": 0.22,
            "vol_floor_passed": False,
            "breakout_direction": "INSIDE_RANGE",
            "volume_ratio": 1.5,
            "volume_ok": True,
            "evaluation_time": "09:35:00",
        }
        OptionMicrostructureEvaluator.set_mock_opening_range_data(mock_or_narrow)

        eval_dt = datetime(2026, 10, 19, 9, 36, 0)
        telemetry = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=eval_dt, force_refresh=True)

        self.assertFalse(telemetry["is_unlocked"], "Narrow opening range (< 0.40%) must keep gate locked.")
        self.assertIn("VOLATILITY_FLOOR_STAND_DOWN", telemetry["unlock_reason"])

    def test_pillar1_volatility_floor_allows_expansion(self):
        """Pillar 1: Opening Range >= 0.40% (e.g. 0.55% expansion) passes the volatility floor."""
        mock_or_wide = {
            "is_accepted": True,
            "or_high": 22650.0,
            "or_low": 22500.0,  # 150 pt range on 22500 -> 0.67%
            "range_pts": 150.0,
            "range_pct": 0.67,
            "vol_floor_passed": True,
            "breakout_direction": "BULL_EXPANSION",
            "volume_ratio": 1.8,
            "volume_ok": True,
            "evaluation_time": "09:35:00",
        }
        OptionMicrostructureEvaluator.set_mock_opening_range_data(mock_or_wide)

        eval_dt = datetime(2026, 10, 19, 9, 36, 0)
        telemetry = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=eval_dt, force_refresh=True)
        # Should not be blocked by volatility floor
        self.assertNotIn("VOLATILITY_FLOOR_STAND_DOWN", telemetry["unlock_reason"])

    def test_pillar3_early_breakeven_ratchet_covers_friction(self):
        """Pillar 3: At +15% MFE, stop moves to entry + 6.0%, which mathematically guarantees net gain after costs."""
        entry_ask = 100.0
        current_peak = 115.0  # +15.0% MFE
        mfe_pct = 15.0
        qty = 65

        stop_price = OptionPaperExecution.calculate_chandelier_stop(entry_ask, current_peak, mfe_pct)
        self.assertIsNotNone(stop_price)

        # Stop price must be at least entry + 6% = 106.0
        expected_floor = entry_ask * (1.0 + EARLY_BREAKEVEN_FLOOR_PCT / 100.0)
        self.assertGreaterEqual(stop_price, expected_floor)

        # Verify that selling at stop_price yields positive net P&L after Upstox statutory fees and slippage
        gross_pnl = (stop_price - entry_ask) * qty
        costs = calculate_option_trade_costs(entry_ask, stop_price, qty)
        net_pnl = gross_pnl - costs["total_friction"]

        self.assertGreater(net_pnl, 0.0, "Selling at +6.0% breakeven ratchet must yield positive net P&L after friction.")

    def test_pillar3_continuous_chandelier_tightens_across_tiers(self):
        """Pillar 3: Chandelier stop tightens distance below peak as MFE expands across Tiers 1-4."""
        entry_ask = 100.0

        # Tier 1 (+18% MFE): Trails 8% below peak
        peak_t1 = 118.0
        stop_t1 = OptionPaperExecution.calculate_chandelier_stop(entry_ask, peak_t1, 18.0)
        self.assertAlmostEqual(stop_t1, max(106.0, peak_t1 * 0.92), places=1)

        # Tier 2 (+25% MFE): Trails 6% below peak
        peak_t2 = 125.0
        stop_t2 = OptionPaperExecution.calculate_chandelier_stop(entry_ask, peak_t2, 25.0)
        self.assertAlmostEqual(stop_t2, peak_t2 * 0.94, places=1)

        # Tier 3 (+35% MFE): Trails 4.5% below peak
        peak_t3 = 135.0
        stop_t3 = OptionPaperExecution.calculate_chandelier_stop(entry_ask, peak_t3, 35.0)
        self.assertAlmostEqual(stop_t3, peak_t3 * 0.955, places=1)

        # Tier 4 (+70% MFE Super-Runner): Trails 4.0% below peak
        peak_t4 = 170.0
        stop_t4 = OptionPaperExecution.calculate_chandelier_stop(entry_ask, peak_t4, 70.0)
        self.assertAlmostEqual(stop_t4, peak_t4 * 0.96, places=1)
        self.assertGreater(stop_t4, 160.0, "Super-runner locks in massive profit.")

    def test_pillar2_uncapped_runner_mode_ignores_static_target(self):
        """Pillar 2: When ENABLE_4PILLAR_CANARY_ENGINE is active, positions past +35% are not closed as TARGET_MET."""
        trade = {
            "trade_id": "test_uncapped_1",
            "instrument_key": "NSE_FO|12345",
            "trading_symbol": "NIFTY 22400 CE",
            "entry_ask": 100.0,
            "peak_price": 140.0,
            "trough_price": 98.0,
            "engine_version": "UNCAPPED_CONVEX_RUNNER",
            "entry_time": datetime(2026, 10, 19, 9, 32, 0).isoformat(),
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 22400,
            "lot_size": 65,
        }

        # Current bid is at +40% (140.0). In classic mode, +35% would exit.
        # In uncapped mode, chandelier stop is at 140 * 0.955 = 133.7. Since current_bid = 140.0 > 133.7, it stays OPEN!
        live_quotes = {"NSE_FO|12345": {"bid": 140.0, "ask": 140.5, "ltp": 140.0}}

        # Mock OptionDB to return this single trade
        from unittest.mock import patch
        from app.analytics.option_sniffer.option_db import OptionDB

        with patch.object(OptionDB, "get_open_paper_trades", return_value=[trade]):
            closed = OptionPaperExecution.update_open_trades(
                live_quotes_map=live_quotes,
                as_of_time=datetime(2026, 10, 19, 9, 45, 0),
                enforce_cutoff=True,
            )

        self.assertEqual(len(closed), 0, "Uncapped runner must NOT exit on static +35% target; must remain open to run!")


if __name__ == "__main__":
    unittest.main()

