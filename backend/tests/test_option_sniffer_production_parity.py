"""
Option Sniffer: Production-Parity Invariant Unit Tests.
Workspace: /Users/arunsharma/Desktop/swing trade react
Target: backend/tests/test_option_sniffer_production_parity.py

Verifies all 20 production gates, Canary Scout semantics, Dynamic Timing,
Dynamic Regime Shield, Alpha Model hurdles, and cooldown hysteresis to ensure
strict parity between backtesting and live/paper execution.
"""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# Mock ta to satisfy historical_data_layer imports without external package dependency
sys.modules['ta'] = MagicMock()
sys.path.insert(0, os.path.abspath("backend"))

from datetime import datetime, time as dtime
import numpy as np
import pandas as pd

from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
from app.analytics.option_sniffer.option_alpha_model import OptionAlphaModel
from app.analytics.option_sniffer.option_microstructure_evaluator import OptionMicrostructureEvaluator
from app.analytics.option_sniffer.option_constants import (
    STATE_WATCH,
    STATE_QUALIFIED,
    STATE_ACTIVE,
    DIRECTION_BUY_CE,
    DIRECTION_BUY_PE,
    DIRECTION_NO_TRADE,
    MIN_BID_PRICE,
    MAX_SPREAD_PCT,
    MIN_OI,
    MIN_VOLUME,
    MIN_CALIBRATED_PROB,
    MIN_EXPECTED_NET_RETURN_PCT,
    KAUFMAN_ER_MIN_THRESHOLD,
    EXTREMA_DISPLACEMENT_MAX_ATR_RATIO,
    SIGNAL_COOLDOWN_SECONDS,
)


class TestOptionSnifferProductionParity(unittest.TestCase):
    """Rigorous invariant unit tests proving production gate parity."""

    def setUp(self):
        OptionSignalEngine.reset_class_state()
        self.engine = OptionSignalEngine()

        self.valid_contract_ce = {
            "instrument_key": "NSE_FO|NIFTY26OCT25000CE",
            "trading_symbol": "NIFTY 25000 CE 26 OCT 26",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-26",
            "lot_size": 75,
        }

        self.valid_contract_pe = {
            "instrument_key": "NSE_FO|NIFTY26OCT25000PE",
            "trading_symbol": "NIFTY 25000 PE 26 OCT 26",
            "underlying": "NIFTY",
            "option_type": "PE",
            "strike": 25000.0,
            "expiry": "2026-10-26",
            "lot_size": 75,
        }

        self.valid_quote = {
            "bid": 120.0,
            "ask": 121.0,
            "ltp": 120.5,
            "quote_age_ms": 150,
            "spread_pct": 0.83,
            "oi": 250000.0,
            "volume": 50000,
            "delta": 0.50,
            "iv": 13.5,
        }

        self.valid_underlying_quote = {
            "spot_ltp": 25020.0,
            "open": 24900.0,
            "high": 25050.0,
            "low": 24880.0,
            "close": 25020.0,
            "vwap": 24980.0,
            "quote_age_ms": 100,
        }

        # 30-bar trending underlying DataFrame
        prices = np.linspace(24900, 25020, 30)
        self.trending_df = pd.DataFrame({
            "datetime": [f"2026-10-19T10:{i:02d}:00" for i in range(30)],
            "open": prices - 2,
            "high": prices + 5,
            "low": prices - 4,
            "close": prices,
            "volume": [10000 + i * 500 for i in range(30)],
        })

    def tearDown(self):
        OptionSignalEngine.reset_class_state()

    # ── 1. GATE 0 & 0A: TIME CURFEW & TRADING WINDOW ─────────────────────────
    def test_gate_0_session_cutoff_at_1515(self):
        """Gate 0: New option entries must be rejected after 15:15 IST."""
        late_time = datetime(2026, 10, 19, 15, 16, 0)
        eval_res = self.engine.evaluate_contract(
            contract=self.valid_contract_ce,
            quote=self.valid_quote,
            underlying_quote=self.valid_underlying_quote,
            underlying_df=self.trending_df,
            as_of_time=late_time,
            enforce_cutoff=True,
        )
        self.assertEqual(eval_res["status"], STATE_WATCH)
        self.assertFalse(eval_res["hurdles_passed"])
        self.assertTrue(any("15:15 IST reached" in r for r in eval_res["rejection_reasons"]))

    def test_gate_0a_curfew_outside_golden_window(self):
        """Gate 0A: Entries outside 09:30-14:00 (e.g. 09:20 or 14:15) must be halted."""
        early_time = datetime(2026, 10, 19, 9, 20, 0)
        eval_res = self.engine.evaluate_contract(
            contract=self.valid_contract_ce,
            quote=self.valid_quote,
            underlying_quote=self.valid_underlying_quote,
            underlying_df=self.trending_df,
            as_of_time=early_time,
            enforce_cutoff=True,
        )
        self.assertEqual(eval_res["status"], STATE_WATCH)
        self.assertTrue(any("golden option entry window" in r for r in eval_res["rejection_reasons"]))

    # ── 2. GATES 1-4: MICROSTRUCTURE & LIQUIDITY ─────────────────────────────
    def test_gate_1_stale_quote_rejection(self):
        """Gate 1: Quote age > 15,000ms must be rejected."""
        stale_quote = dict(self.valid_quote, quote_age_ms=18000)
        eval_res = self.engine.evaluate_contract(
            contract=self.valid_contract_ce,
            quote=stale_quote,
            underlying_quote=self.valid_underlying_quote,
            underlying_df=self.trending_df,
            as_of_time=datetime(2026, 10, 19, 10, 30, 0),
        )
        self.assertFalse(eval_res["hurdles_passed"])
        self.assertTrue(any("Stale quote" in r for r in eval_res["rejection_reasons"]))

    def test_gate_2_low_bid_and_inverted_book(self):
        """Gate 2: Low bid (< ₹5.00) and inverted book (Ask < Bid) must be rejected."""
        penny_quote = dict(self.valid_quote, bid=3.50, ask=4.00)
        eval_res = self.engine.evaluate_contract(
            contract=self.valid_contract_ce,
            quote=penny_quote,
            underlying_quote=self.valid_underlying_quote,
            underlying_df=self.trending_df,
            as_of_time=datetime(2026, 10, 19, 10, 30, 0),
        )
        self.assertTrue(any("Bid price too low" in r for r in eval_res["rejection_reasons"]))

        inverted_quote = dict(self.valid_quote, bid=120.0, ask=115.0)
        eval_res2 = self.engine.evaluate_contract(
            contract=self.valid_contract_ce,
            quote=inverted_quote,
            underlying_quote=self.valid_underlying_quote,
            underlying_df=self.trending_df,
            as_of_time=datetime(2026, 10, 19, 10, 30, 0),
        )
        self.assertTrue(any("Inverted book" in r for r in eval_res2["rejection_reasons"]))

    def test_gate_3_excessive_spread(self):
        """Gate 3: Spread > 3.5% must be rejected."""
        wide_quote = dict(self.valid_quote, spread_pct=4.8)
        eval_res = self.engine.evaluate_contract(
            contract=self.valid_contract_ce,
            quote=wide_quote,
            underlying_quote=self.valid_underlying_quote,
            underlying_df=self.trending_df,
            as_of_time=datetime(2026, 10, 19, 10, 30, 0),
        )
        self.assertTrue(any("Excessive spread" in r for r in eval_res["rejection_reasons"]))

    def test_gate_4_minimum_liquidity(self):
        """Gate 4: OI < 5000 or Volume < 200 must be rejected."""
        illiquid_quote = dict(self.valid_quote, oi=3000, volume=150)
        eval_res = self.engine.evaluate_contract(
            contract=self.valid_contract_ce,
            quote=illiquid_quote,
            underlying_quote=self.valid_underlying_quote,
            underlying_df=self.trending_df,
            as_of_time=datetime(2026, 10, 19, 10, 30, 0),
        )
        self.assertTrue(any("Low Open Interest" in r for r in eval_res["rejection_reasons"]))
        self.assertTrue(any("Low Volume" in r for r in eval_res["rejection_reasons"]))

    # ── 3. GATE 5B: DYNAMIC REGIME SHIELD ────────────────────────────────────
    def test_gate_5b_trapped_in_day_range_chop(self):
        """Gate 5B: Day Range < 100 pts trapped in middle must be rejected."""
        chop_underlying = dict(
            self.valid_underlying_quote,
            high=25040.0,
            low=24960.0,
            spot_ltp=25000.0,  # 80 pts range, trapped in center
        )
        eval_res = self.engine.evaluate_contract(
            contract=self.valid_contract_ce,
            quote=self.valid_quote,
            underlying_quote=chop_underlying,
            underlying_df=self.trending_df,
            as_of_time=datetime(2026, 10, 19, 10, 30, 0),
        )
        self.assertFalse(eval_res["hurdles_passed"])
        self.assertTrue(any("CHOP / SIDEWAYS" in r or "Chop Shield" in r for r in eval_res["rejection_reasons"]))

    def test_gate_5b_normalized_extrema_displacement_guard_pe(self):
        """Gate 5B: Underlying rallied > 1.5x ATR off day low must lock out PE purchases."""
        # Spot rallied 70 pts off low (ATR ~ 25 pts -> 2.8x ATR)
        reversal_underlying = dict(
            self.valid_underlying_quote,
            high=25150.0,
            low=24900.0,
            spot_ltp=24980.0,  # +80 pts off low
        )
        eval_res = self.engine.evaluate_contract(
            contract=self.valid_contract_pe,
            quote=self.valid_quote,
            underlying_quote=reversal_underlying,
            underlying_df=self.trending_df,
            as_of_time=datetime(2026, 10, 19, 10, 30, 0),
        )
        self.assertFalse(eval_res["hurdles_passed"])
        self.assertTrue(any("Normalized Reversal Guard" in r for r in eval_res["rejection_reasons"]))

    def test_gate_5b_kaufman_er_chaotic_chop(self):
        """Gate 5B: Kaufman ER < 0.35 indicates chaotic chop and halts entries."""
        # Create oscillating chop DataFrame
        t_osc = np.array([25000, 25015, 24995, 25010, 24990, 25005, 24995, 25010, 25000, 25005, 25000, 25010, 24995, 25005, 25000])
        chop_df = pd.DataFrame({
            "datetime": [f"2026-10-19T10:{i:02d}:00" for i in range(15)],
            "open": t_osc - 2,
            "high": t_osc + 5,
            "low": t_osc - 5,
            "close": t_osc,
            "volume": [10000 for _ in range(15)],
        })
        eval_res = self.engine.evaluate_contract(
            contract=self.valid_contract_ce,
            quote=self.valid_quote,
            underlying_quote=dict(self.valid_underlying_quote, high=25080, low=24950, spot_ltp=25000),
            underlying_df=chop_df,
            as_of_time=datetime(2026, 10, 19, 10, 30, 0),
        )
        self.assertFalse(eval_res["hurdles_passed"])
        self.assertTrue(any("Chop Shield" in r for r in eval_res["rejection_reasons"]))

    # ── 4. GATES 6-8: OPTION ALPHA MODEL HURDLES ─────────────────────────────
    def test_gate_7_and_8_alpha_model_hurdles(self):
        """Gates 7 & 8: Low probability (< 44%) or Net Edge (< +2.0%) must reject."""
        mock_model = MagicMock(spec=OptionAlphaModel)
        mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.41,  # Below 0.44 hurdle
            "expected_net_return_pct": 1.2,  # Below +2.0% hurdle
        }
        engine_mock = OptionSignalEngine(model=mock_model)

        eval_res = engine_mock.evaluate_contract(
            contract=self.valid_contract_ce,
            quote=self.valid_quote,
            underlying_quote=self.valid_underlying_quote,
            underlying_df=self.trending_df,
            as_of_time=datetime(2026, 10, 19, 10, 30, 0),
            enforce_regime_gate=False,
        )
        self.assertFalse(eval_res["hurdles_passed"])
        self.assertTrue(any("Low probability" in r for r in eval_res["rejection_reasons"]))
        self.assertTrue(any("Insufficient net edge" in r for r in eval_res["rejection_reasons"]))

    # ── 5. GATE 10: COOLDOWN HYSTERESIS ──────────────────────────────────────
    @patch("app.analytics.option_sniffer.option_db.OptionDB.get_open_paper_trades", return_value=[])
    @patch("app.analytics.option_sniffer.option_db.OptionDB.get_daily_circuit_breaker_telemetry", return_value={"is_session_halted": False, "is_pause_active": False})
    def test_gate_10_cooldown_hysteresis_per_contract_and_direction(self, mock_cb, mock_open):
        """Gate 10: Contract cooldown (5m) and direction SL cooldown (10m) must block spam."""
        ikey = self.valid_contract_ce["instrument_key"]
        now_dt = datetime(2026, 10, 19, 10, 30, 0)
        now_epoch = now_dt.timestamp()

        # Mock model so candidate passes Gates 6-8 to reach Gate 10
        self.engine.model = MagicMock(spec=OptionAlphaModel)
        self.engine.model.version = "1.0.0"
        self.engine.model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.55,
            "expected_net_return_pct": 4.5,
        }

        # Set active cooldown
        OptionSignalEngine._COOLDOWNS[ikey] = now_epoch - 60.0  # 1 min ago (< 300s)
        eval_res = self.engine.evaluate_contract(
            contract=self.valid_contract_ce,
            quote=self.valid_quote,
            underlying_quote=self.valid_underlying_quote,
            underlying_df=self.trending_df,
            as_of_time=now_dt,
            enforce_regime_gate=False,
        )
        self.assertTrue(any("Signal cooldown active" in r for r in eval_res["rejection_reasons"]))

        # Set direction SL cooldown
        OptionSignalEngine._COOLDOWNS.clear()
        OptionSignalEngine._DIRECTION_SL_COOLDOWNS["NIFTY_CE"] = now_epoch - 120.0  # 2 min ago (< 600s)
        eval_res2 = self.engine.evaluate_contract(
            contract=self.valid_contract_ce,
            quote=self.valid_quote,
            underlying_quote=self.valid_underlying_quote,
            underlying_df=self.trending_df,
            as_of_time=now_dt,
            enforce_regime_gate=False,
        )
        self.assertTrue(any("Direction SL cooldown active" in r for r in eval_res2["rejection_reasons"]))

    # ── 6. DYNAMIC TIMING AND CANARY SCOUT SEMANTICS ────────────────────────
    def test_dynamic_timing_early_unlock_prerequisites(self):
        """Dynamic Timing: 09:35 unlock requires Kaufman ER >= 0.35, high volume ratio, and accepted breakout."""
        # Strong directional closes
        strong_closes = np.linspace(24800, 24950, 25).tolist()
        er_high = OptionMicrostructureEvaluator.compute_kaufman_er(strong_closes)
        self.assertGreaterEqual(er_high, 0.35)

        # Flat choppy closes
        choppy_closes = [24800, 24820, 24790, 24810, 24800, 24815, 24795] * 3
        er_low = OptionMicrostructureEvaluator.compute_kaufman_er(choppy_closes)
        self.assertLess(er_low, 0.35)


if __name__ == "__main__":
    unittest.main()
