"""
Unit & Integration Tests for Top-Down Macro Regime Gating.
Verifies that counter-trend trades are suppressed during broad market distribution/expansion regimes.
"""

import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np

from app.analytics.macro_engine import is_direction_permitted, get_macro_regime
from app.analytics.decision_engine import evaluate_ticker


class TestMacroRegimeGating(unittest.TestCase):
    def test_intraday_direction_permitted_rules(self):
        # 1. Intraday Bull Momentum: Allows BUY, suppresses SELL
        bull_state = {
            "intraday_regime_verdict": "INTRADAY_BULL_MOMENTUM",
            "intraday_change_pct": 0.45,
            "regime_verdict": "BEAR_DISTRIBUTION"  # Multi-day daily is bear, but session is green!
        }
        ok_buy, reason_buy = is_direction_permitted("BUY", "INTRADAY", bull_state)
        ok_sell, reason_sell = is_direction_permitted("SELL", "INTRADAY", bull_state)
        self.assertTrue(ok_buy)
        self.assertIn("INTRADAY_BULL_MOMENTUM", reason_buy)
        self.assertFalse(ok_sell)
        self.assertIn("INTRADAY_BULL_MOMENTUM", reason_sell)

        # 2. Intraday Bear Pressure: Allows SELL, suppresses BUY
        bear_state = {
            "intraday_regime_verdict": "INTRADAY_BEAR_PRESSURE",
            "intraday_change_pct": -0.55,
            "regime_verdict": "BULL_EXPANSION"
        }
        ok_buy, reason_buy = is_direction_permitted("BUY", "INTRADAY", bear_state)
        ok_sell, reason_sell = is_direction_permitted("SELL", "INTRADAY", bear_state)
        self.assertFalse(ok_buy)
        self.assertIn("INTRADAY_BEAR_PRESSURE", reason_buy)
        self.assertTrue(ok_sell)
        self.assertIn("INTRADAY_BEAR_PRESSURE", reason_sell)

        # 3. Intraday Neutral Chop: Allows both
        neutral_state = {
            "intraday_regime_verdict": "INTRADAY_NEUTRAL_CHOP",
            "intraday_change_pct": 0.02
        }
        ok_buy, _ = is_direction_permitted("BUY", "INTRADAY", neutral_state)
        ok_sell, _ = is_direction_permitted("SELL", "INTRADAY", neutral_state)
        self.assertTrue(ok_buy)
        self.assertTrue(ok_sell)

    def test_swing_direction_permitted_rules(self):
        # 1. Bear Distribution: Suppresses BUY, allows SELL
        bear_state = {
            "regime_verdict": "BEAR_DISTRIBUTION",
            "distance_ema20_pct": -2.5
        }
        ok_buy, reason_buy = is_direction_permitted("BUY", "SWING", bear_state)
        ok_sell, reason_sell = is_direction_permitted("SELL", "SWING", bear_state)
        self.assertFalse(ok_buy)
        self.assertIn("BEAR_DISTRIBUTION", reason_buy)
        self.assertTrue(ok_sell)

        # 2. Bull Expansion: Allows BUY, suppresses SELL
        bull_state = {
            "regime_verdict": "BULL_EXPANSION",
            "distance_ema20_pct": +2.1
        }
        ok_buy, reason_buy = is_direction_permitted("BUY", "SWING", bull_state)
        ok_sell, reason_sell = is_direction_permitted("SELL", "SWING", bull_state)
        self.assertTrue(ok_buy)
        self.assertFalse(ok_sell)
        self.assertIn("BULL_EXPANSION", reason_sell)

    def test_green_day_counter_trend_fix(self):
        """
        Validates the exact fix for today's user issue:
        On a green session (+0.25%) during a multi-day pull-back (-1.5% vs daily 20-EMA):
        - Intraday longs MUST be permitted.
        - Intraday counter-trend shorts MUST be blocked.
        - Swing longs remain blocked to protect multi-day capital.
        - Swing shorts remain permitted.
        """
        hybrid_state = {
            "regime_verdict": "BEAR_DISTRIBUTION",
            "distance_ema20_pct": -1.51,
            "intraday_regime_verdict": "INTRADAY_BULL_MOMENTUM",
            "intraday_change_pct": 0.25,
            "intraday_open_change_pct": 0.10
        }

        # Intraday
        intra_buy, _ = is_direction_permitted("BUY", "INTRADAY", hybrid_state)
        intra_sell, _ = is_direction_permitted("SELL", "INTRADAY", hybrid_state)
        self.assertTrue(intra_buy, "Intraday BUY must be permitted on green session momentum!")
        self.assertFalse(intra_sell, "Intraday counter-trend SELL must be suppressed on green session!")

        # Swing
        swing_buy, _ = is_direction_permitted("BUY", "SWING", hybrid_state)
        swing_sell, _ = is_direction_permitted("SELL", "SWING", hybrid_state)
        self.assertFalse(swing_buy, "Swing BUY must be suppressed when daily is in BEAR_DISTRIBUTION!")
        self.assertTrue(swing_sell, "Swing SELL must be permitted in BEAR_DISTRIBUTION!")

    def test_decision_engine_macro_gate_blocks_counter_trend(self):
        # Create a mock ensemble model predicting bullish with 85% probability
        mock_ens = MagicMock()
        mock_ens.predict_proba.return_value = np.array([[0.15, 0.85]])
        mock_meta = {"version": "v1.0-test"}

        # Create valid OHLCV dataframe (100 rows)
        dates = pd.date_range(end="2026-09-15 15:30", periods=100, freq="15min")
        df = pd.DataFrame({
            "open": np.linspace(100, 110, 100),
            "high": np.linspace(101, 111, 100),
            "low": np.linspace(99, 109, 100),
            "close": np.linspace(100.5, 110.5, 100),
            "volume": np.full(100, 50000)
        }, index=dates)

        # Under an INTRADAY_BEAR_PRESSURE macro state, a BUY trade should be disqualified
        bear_macro = {
            "intraday_regime_verdict": "INTRADAY_BEAR_PRESSURE",
            "intraday_change_pct": -0.8,
            "regime_verdict": "BEAR_DISTRIBUTION"
        }

        res = evaluate_ticker(
            ticker="RELIANCE.NS",
            df=df,
            champion_model=mock_ens,
            champion_meta=mock_meta,
            trade_type="INTRADAY",
            qualification_threshold=50.0,
            macro_state=bear_macro,
            allow_candle_close=True,
            skip_enrichment=True
        )

        self.assertFalse(res.qualified)
        self.assertIn("MACRO_REGIME_BLOCK", res.rejection_reason)
        self.assertFalse(res.pipeline_components.get("macro_permitted"))


if __name__ == "__main__":
    unittest.main()
