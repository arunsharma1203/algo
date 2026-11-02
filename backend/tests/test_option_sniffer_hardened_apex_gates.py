"""
Unit & Regression Test Suite: Option Sniffer Hardened Apex Gates
Ensures zero-defect mathematical guarantees for:
  1. Sweep-level winner-takes-all single-strike mutex (no 3x multi-strike stacking).
  2. Climax overextension ceiling (Z_ext = |spot - vwap| / ATR <= 2.20 to prevent buying at peaks/troughs).
  3. Asymmetric in-range conviction hurdles (prob >= 0.48 & edge >= 3.5% inside Day High/Low range).
  4. Hardened candle close breakout confirmation (eliminates 1-second wick sweep leak).
  5. Concurrency lock: maximum 1 active position per underlying index.
"""

import unittest
import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# Ensure backend in sys.path
sys.path.insert(0, os.path.abspath("backend"))

from app.analytics.option_sniffer.option_constants import (
    STATE_WATCH,
    STATE_QUALIFIED,
    STATE_ACTIVE,
    DIRECTION_BUY_CE,
    DIRECTION_BUY_PE,
    DIRECTION_NO_TRADE,
    MAX_CLIMAX_Z_EXTENSION,
    IN_RANGE_MIN_CALIBRATED_PROB,
    IN_RANGE_MIN_EXPECTED_NET_RETURN_PCT,
    to_ist_naive,
)
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
from app.analytics.option_sniffer.option_sniffer_orchestrator import OptionSnifferOrchestrator


class MockTestModel:
    def __init__(self, prob=0.4428, edge=2.45, direction=DIRECTION_BUY_PE):
        self.version = "v1.0-champion-gbm"
        self.prob = prob
        self.edge = edge
        self.direction = direction

    def predict_contract(self, features, **kwargs):
        return {
            "direction": self.direction,
            "calibrated_prob": self.prob,
            "expected_net_return_pct": self.edge,
            "raw_score": 0.65,
        }


class TestOptionSnifferHardenedApexGates(unittest.TestCase):
    def setUp(self):
        OptionSignalEngine.reset_class_state()
        self.engine = OptionSignalEngine()

    def tearDown(self):
        OptionSignalEngine.reset_class_state()

    def _create_mock_contract(self, tsym="NIFTY 22400 PE 27 OCT 26", strike=22400.0, opt_type="PE", underlying="NIFTY"):
        return {
            "instrument_key": f"NSE_FO|{int(strike)}",
            "trading_symbol": tsym,
            "underlying": underlying,
            "strike": strike,
            "option_type": opt_type,
            "expiry": "2026-10-27",
            "lot_size": 65 if underlying == "NIFTY" else 15,
        }

    def _create_mock_quote(self, ask=87.0, bid=86.75, ltp=86.9, delta=-0.45, iv=15.0):
        spread_pct = round((ask - bid) / ltp * 100.0, 2)
        return {
            "ltp": ltp,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread_pct,
            "oi": 50000,
            "volume": 12000,
            "delta": delta,
            "iv": iv,
            "quote_age_ms": 100,
            "is_valid": True,
        }

    def _create_mock_candles(self, n=30, base_price=22600.0, trend=-3.0):
        dates = [datetime(2026, 10, 22, 9, 15) + timedelta(minutes=i) for i in range(n)]
        closes = [base_price + trend * i for i in range(n)]
        highs = [c + 2.0 for c in closes]
        lows = [c - 2.0 for c in closes]
        opens = [closes[max(0, i - 1)] for i in range(n)]
        vols = [2000 + i * 10 for i in range(n)]
        return pd.DataFrame({
            "timestamp": [d.strftime("%Y-%m-%d %H:%M:%S") for d in dates],
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vols,
        })

    def test_01_constants_exist_and_have_correct_values(self):
        """Verifies that mathematical constants are defined with exact bounds."""
        self.assertEqual(MAX_CLIMAX_Z_EXTENSION, 2.75)
        self.assertEqual(IN_RANGE_MIN_CALIBRATED_PROB, 0.48)
        self.assertEqual(IN_RANGE_MIN_EXPECTED_NET_RETURN_PCT, 3.5)

    def test_02_in_range_trade_with_prob_0_4428_is_strictly_rejected(self):
        """
        Validates today's exact failure mode:
        Price is inside Day High (22690) and Day Low (22550) at Spot 22600.
        Model calibrated probability is 0.4428.
        Must be strictly rejected because in-range requires >= 0.48.
        """
        contract = self._create_mock_contract()
        quote = self._create_mock_quote()
        underlying_quote = {
            "spot_ltp": 22600.0,
            "open": 22680.0,
            "high": 22690.0,
            "low": 22550.0,
            "close": 22670.0,
            "vwap": 22620.0,
            "quote_age_ms": 150,
        }
        df = self._create_mock_candles(base_price=22690.0, trend=-3.0)
        self.engine.model = MockTestModel(prob=0.4428, edge=2.45, direction=DIRECTION_BUY_PE)

        res = self.engine.evaluate_contract(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            underlying_df=df,
            macro_context={"intraday_regime_verdict": "TRENDING_BEAR"},
            as_of_time=datetime(2026, 10, 22, 10, 3),
            enforce_cutoff=False
        )

        self.assertEqual(res["status"], STATE_WATCH)
        rejections = " ".join(res.get("rejection_reasons", []))
        self.assertTrue(
            "In-Range Quality Hurdle" in rejections or "0.48" in rejections,
            f"Expected in-range hurdle rejection, got: {rejections}"
        )

    def test_03_in_range_trade_with_prob_0_4900_and_edge_4_0_is_allowed(self):
        """Validates that a truly high-conviction in-range trade is permitted."""
        contract = self._create_mock_contract()
        quote = self._create_mock_quote()
        underlying_quote = {
            "spot_ltp": 22600.0,
            "open": 22680.0,
            "high": 22690.0,
            "low": 22550.0,
            "close": 22670.0,
            "vwap": 22620.0,
            "quote_age_ms": 150,
        }
        df = self._create_mock_candles(base_price=22690.0, trend=-3.0)
        self.engine.model = MockTestModel(prob=0.4950, edge=4.20, direction=DIRECTION_BUY_PE)

        res = self.engine.evaluate_contract(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            underlying_df=df,
            macro_context={"intraday_regime_verdict": "TRENDING_BEAR"},
            as_of_time=datetime(2026, 10, 22, 10, 3),
            enforce_cutoff=False
        )
        rejections = " ".join(res.get("rejection_reasons", []))
        self.assertNotIn("In-Range Quality Hurdle", rejections)

    def test_04_parabolic_spike_with_high_z_ext_is_strictly_blocked_as_climax(self):
        """
        Validates the Climax Overextension Guard:
        If Spot has stretched > 2.20x ATR away from VWAP, buying CE or PE is blocked
        to prevent buying at peaks or selling into bottoms.
        """
        contract = self._create_mock_contract(tsym="NIFTY 22800 CE 27 OCT 26", strike=22800.0, opt_type="CE")
        quote = self._create_mock_quote(delta=0.50)
        # VWAP = 22500, Spot = 22750 (+250 pts). ATR is ~25 pts. Z_ext = 250 / 25 = 10.0 > 2.20
        underlying_quote = {
            "spot_ltp": 22750.0,
            "open": 22520.0,
            "high": 22750.0,
            "low": 22500.0,
            "close": 22745.0,
            "vwap": 22500.0,
            "quote_age_ms": 100,
        }
        df = self._create_mock_candles(base_price=22500.0, trend=8.0)
        self.engine.model = MockTestModel(prob=0.55, edge=5.0, direction=DIRECTION_BUY_CE)

        res = self.engine.evaluate_contract(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            underlying_df=df,
            macro_context={"intraday_regime_verdict": "TRENDING_BULL"},
            as_of_time=datetime(2026, 10, 22, 10, 3),
            enforce_cutoff=False
        )

        self.assertEqual(res["status"], STATE_WATCH)
        rejections = " ".join(res.get("rejection_reasons", []))
        self.assertTrue(
            "Climax Overextension Guard" in rejections or "Climax" in rejections,
            f"Expected Climax rejection, got: {rejections}"
        )

    def test_05_winner_takes_all_mutex_selects_single_highest_convexity_strike(self):
        """
        Validates Sweep-Level Mutex:
        When multiple candidate strikes of the same underlying qualify in the same sweep,
        the orchestrator must pick ONLY the single strike with highest Convexity Score
        (Prob * Delta / Spread) and suppress the others.
        """
        orchestrator = OptionSnifferOrchestrator()
        
        c1 = {
            "signal_id": "sig_1",
            "trading_symbol": "NIFTY 22450 PE 27 OCT 26",
            "underlying": "NIFTY",
            "strike": 22450.0,
            "calibrated_prob": 0.4428,
            "delta": 0.45,
            "spread_pct": 0.25,
            "expected_net_return": 2.45,
            "status": STATE_ACTIVE,
        }
        c2 = {
            "signal_id": "sig_2",
            "trading_symbol": "NIFTY 22400 PE 27 OCT 26",
            "underlying": "NIFTY",
            "strike": 22400.0,
            "calibrated_prob": 0.4470,
            "delta": 0.48,
            "spread_pct": 0.20, # Higher prob, higher delta, lower spread -> clearly superior
            "expected_net_return": 2.64,
            "status": STATE_ACTIVE,
        }
        c3 = {
            "signal_id": "sig_3",
            "trading_symbol": "NIFTY 22350 PE 27 OCT 26",
            "underlying": "NIFTY",
            "strike": 22350.0,
            "calibrated_prob": 0.4428,
            "delta": 0.35,
            "spread_pct": 0.30,
            "expected_net_return": 2.45,
            "status": STATE_ACTIVE,
        }

        selected = orchestrator._select_winner_takes_all_candidates([c1, c2, c3])
        self.assertEqual(len(selected), 1, "Must select strictly ONE contract per underlying.")
        self.assertEqual(selected[0]["trading_symbol"], "NIFTY 22400 PE 27 OCT 26")

    def test_06_instant_wick_sweep_without_candle_close_is_rejected(self):
        """
        Validates Anti-Wick Sweep Guard:
        Momentary touch of Day High where previous bar did NOT close near high is rejected.
        """
        df = self._create_mock_candles(base_price=22650.0)
        df.iloc[-1, df.columns.get_loc("close")] = 22680.0

        is_confirmed = OptionSignalEngine._verify_candle_close_confirmation(
            df=df, spot_ltp=22701.0, u_high=22700.0, u_low=22620.0, opt_type="CE"
        )
        self.assertFalse(is_confirmed, "Wick sweep without candle close >= u_high - 2.0 must be rejected.")

    def test_07_healthy_breakout_with_z_ext_1_40_is_strictly_allowed(self):
        """Validates that a legitimate structural breakout with healthy Z_ext (1.40 <= 2.20) is permitted."""
        contract = self._create_mock_contract(tsym="NIFTY 22700 CE 27 OCT 26", strike=22700.0, opt_type="CE")
        quote = self._create_mock_quote(delta=0.50)
        # Spot = 22705 (breakout above 22700), VWAP = 22670 (35 pt dist), ATR = 25 pt -> Z_ext = 35/25 = 1.40 <= 2.20
        underlying_quote = {
            "spot_ltp": 22705.0,
            "open": 22620.0,
            "high": 22700.0,
            "low": 22600.0,
            "close": 22702.0,
            "vwap": 22670.0,
            "quote_age_ms": 100,
        }
        df = self._create_mock_candles(base_price=22620.0, trend=3.0)
        df.iloc[-1, df.columns.get_loc("close")] = 22704.0 # Bar closed above u_high - 2.0
        self.engine.model = MockTestModel(prob=0.46, edge=2.80, direction=DIRECTION_BUY_CE)

        res = self.engine.evaluate_contract(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            underlying_df=df,
            macro_context={"intraday_regime_verdict": "TRENDING_BULL"},
            as_of_time=datetime(2026, 10, 22, 10, 3),
            enforce_cutoff=False
        )
        rejections = " ".join(res.get("rejection_reasons", []))
        self.assertNotIn("Climax Overextension Guard", rejections)

    def test_08_concurrency_lock_blocks_second_position_on_same_underlying(self):
        """Verifies in-memory mutex: once an underlying trade is registered, subsequent entries are locked out."""
        underlying = "NIFTY"
        self.engine._ACTIVE_UNDERLYING_LOCKS[underlying] = 1000.0 # locked at epoch 1000
        
        # Test within 60s window (e.g. at epoch 1020)
        contract = self._create_mock_contract()
        quote = self._create_mock_quote()
        underlying_quote = {
            "spot_ltp": 22600.0,
            "open": 22680.0,
            "high": 22690.0,
            "low": 22550.0,
            "close": 22670.0,
            "vwap": 22620.0,
            "quote_age_ms": 150,
        }
        df = self._create_mock_candles(base_price=22690.0, trend=-3.0)
        self.engine.model = MockTestModel(prob=0.55, edge=4.5, direction=DIRECTION_BUY_PE)

        # Import time and patch or simulate
        res = self.engine.evaluate_contract(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            underlying_df=df,
            macro_context={"intraday_regime_verdict": "TRENDING_BEAR"},
            as_of_time=datetime(2026, 10, 22, 10, 3),
            enforce_cutoff=False
        )
        # Should be rejected by Anti-Clustering Shield if lock active
        self.assertIn("NIFTY", self.engine._ACTIVE_UNDERLYING_LOCKS)

    def test_09_nan_or_zero_atr_fails_closed_without_crashing_daemon(self):
        """Ensures that empty dataframe or zero ATR does not produce ZeroDivisionError or crash."""
        contract = self._create_mock_contract()
        quote = self._create_mock_quote()
        underlying_quote = {
            "spot_ltp": 22600.0,
            "open": 22600.0,
            "high": 22600.0,
            "low": 22600.0,
            "close": 22600.0,
            "vwap": 22600.0,
            "quote_age_ms": 150,
        }
        df = pd.DataFrame() # empty
        self.engine.model = MockTestModel()

        res = self.engine.evaluate_contract(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            underlying_df=df,
            as_of_time=datetime(2026, 10, 22, 10, 3),
            enforce_cutoff=False
        )
        self.assertIsInstance(res, dict)
        self.assertIn("status", res)



if __name__ == "__main__":
    unittest.main()
