"""
Unit & Regression Test Suite: True Breakout & Kaufman HUD Gate
Verifies the 4 surgical fixes preventing in-range post-spike chop trades:
  1. Opening Range (09:15-09:30) Trap Guard & Removal of Pre-Spike 09:15 Low V-Bottom Bypass (Nov 4 scenario).
  2. In-Range Bear Trap Guard when Spot bounces inside Opening Range above ORB_Lo (Oct 22 scenario).
  3. Zero Trade Starvation on Genuine Opening Range & Day Extrema Breakouts (Oct 26 / Oct 29 / Oct 30 scenarios).
  4. Microstructure HUD Post-09:45 Lockout when trapped inside ORB & Live Order Intent Enforcement.
  5. Completed-Bar Synchronization in OptionMicrostructureEvaluator.get_spot_kaufman_er.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.analytics.option_sniffer.option_constants import (
    DIRECTION_BUY_CE,
    DIRECTION_BUY_PE,
    STATE_WATCH,
    STATE_QUALIFIED,
    STATE_ACTIVE,
)
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
from app.analytics.option_sniffer.option_regime_gate import OptionRegimeGate
from app.analytics.option_sniffer.option_microstructure_evaluator import OptionMicrostructureEvaluator
from app.analytics.option_sniffer.option_live_order_intent import OrderIntentEngine
from app.analytics.option_sniffer.option_db import init_option_sniffer_tables


class TestTrueBreakoutAndKaufmanHudGate(unittest.TestCase):

    def setUp(self):
        OptionSignalEngine.reset_class_state()
        OptionMicrostructureEvaluator._mock_or_data = None
        OptionMicrostructureEvaluator._mock_kaufman_er = None
        OptionMicrostructureEvaluator._mock_spread_atm = None
        OptionMicrostructureEvaluator._cache_payload = None
        OptionMicrostructureEvaluator._cache_timestamp = 0.0

        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self.p1 = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.p2 = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.p3 = patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path)
        self.p1.start()
        self.p2.start()
        self.p3.start()
        init_option_sniffer_tables()

    def tearDown(self):
        OptionSignalEngine.reset_class_state()
        OptionMicrostructureEvaluator._mock_or_data = None
        OptionMicrostructureEvaluator._mock_kaufman_er = None
        OptionMicrostructureEvaluator._mock_spread_atm = None
        OptionMicrostructureEvaluator._cache_payload = None
        OptionMicrostructureEvaluator._cache_timestamp = 0.0
        self.p1.stop()
        self.p2.stop()
        self.p3.stop()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def test_01_nov4_midrange_call_72pts_below_day_high_strictly_blocked(self):
        """
        Replays the exact 2026-11-04 10:51:07 failure mode:
        - 09:15 low = 23,696.15, 09:16/09:30 ORB high = 23,942.50, Day high = 23,946.35.
        - Market crashes to 23,804.75, then bounces 15m to 23,874.20 (-72.15 pts below Day High
          and -68.30 pts inside the Opening Range).
        - Even with calibrated_prob = 0.5032 (> 0.48) and edge = +6.18% (> +3.5%),
          both OptionSignalEngine and OptionRegimeGate MUST block CE entry!
        """
        base_dt = datetime(2026, 11, 4, 9, 15, 0)
        rows = []
        for i in range(96):  # 09:15 to 10:50 (96 completed 1m bars)
            ts = (base_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            if i == 0:
                o, h, l, c = 23710.0, 23890.0, 23696.15, 23886.75
            elif i < 15:
                o, h, l, c = 23900.0, 23942.50, 23880.0, 23920.0
            elif i == 15:
                o, h, l, c = 23925.0, 23946.35, 23910.0, 23930.0
            elif i < 80:
                # Crash down to 23,805 and chop
                c = 23830.0 - (i % 5) * 3.0
                o, h, l = c + 2.0, c + 5.0, c - 5.0
            else:
                # 15m bounce from 23,828.55 up to 23,871.30 inside the range
                step = i - 80
                c = 23828.55 + step * 2.85
                o, h, l = c - 2.0, c + 3.0, c - 3.0
            rows.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 0})
        df = pd.DataFrame(rows)

        mock_model = MagicMock()
        mock_model.version = "v1.0-champion-gbm"
        mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.5032,
            "expected_net_return_pct": 6.18,
        }
        engine = OptionSignalEngine(model=mock_model)

        contract = {
            "instrument_key": "NSE_FO|58801",
            "trading_symbol": "NIFTY 23850 CE 09 NOV 26",
            "underlying": "NIFTY",
            "strike": 23850.0,
            "option_type": "CE",
            "expiry": "2026-11-09",
            "lot_size": 65,
        }
        quote = {
            "bid": 188.20,
            "ask": 188.75,
            "ltp": 188.50,
            "spread_pct": 0.29,
            "quote_age_ms": 100,
            "oi": 1500000,
            "volume": 800000,
            "delta": 0.52,
            "iv": 14.2,
        }
        u_quote = {
            "spot_ltp": 23874.20,
            "open": 23710.0,
            "high": 23946.35,
            "low": 23696.15,
            "close": 23700.0,
            "vwap": 23860.0,
            "quote_age_ms": 100,
        }

        res = engine.evaluate_contract(
            contract=contract,
            quote=quote,
            underlying_quote=u_quote,
            underlying_df=df,
            macro_context={"intraday_regime_verdict": "INTRADAY_BULL_MOMENTUM"},
            as_of_time=datetime(2026, 11, 4, 10, 51, 7),
            enforce_cutoff=True,
            enforce_regime_gate=True,
        )

        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        reasons_joined = " | ".join(res.get("rejection_reasons", []))
        self.assertIn("Opening Range Trap Guard", reasons_joined)
        self.assertTrue(
            "Normalized Reversal Guard" in reasons_joined or "Reversal Guard" in reasons_joined,
            f"Expected Reversal Guard rejection, got: {reasons_joined}",
        )

    def test_02_oct22_midrange_put_inside_orb_strictly_blocked(self):
        """
        Replays the 2026-10-22 10:03:33 failure mode:
        - ORB [22,557.95, 22,697.55], Day Low = 22,554.85 at 09:35.
        - Price bounces +44.30 pts back inside the Opening Range to 22,599.15 at 10:03.
        - PE entry MUST be blocked by Opening Range Trap Guard & Reversal Guard!
        """
        base_dt = datetime(2026, 10, 22, 9, 15, 0)
        rows = []
        for i in range(48):  # 09:15 to 10:02
            ts = (base_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            if i < 15:
                o, h, l, c = 22650.0, 22697.55, 22557.95, 22600.0
            elif i == 20:
                o, h, l, c = 22565.0, 22570.0, 22554.85, 22558.0
            elif i < 33:
                o, h, l, c = 22620.0, 22630.0, 22610.0, 22625.0
            else:
                step = i - 33
                c = 22625.0 - step * 1.8
                o, h, l = c + 1.5, c + 2.5, c - 2.5
            rows.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 0})
        df = pd.DataFrame(rows)

        mock_model = MagicMock()
        mock_model.version = "v1.0-champion-gbm"
        mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_PE,
            "calibrated_prob": 0.51,
            "expected_net_return_pct": 5.5,
        }
        engine = OptionSignalEngine(model=mock_model)

        contract = {
            "instrument_key": "NSE_FO|58802",
            "trading_symbol": "NIFTY 22450 PE 27 OCT 26",
            "underlying": "NIFTY",
            "strike": 22450.0,
            "option_type": "PE",
            "expiry": "2026-10-27",
            "lot_size": 65,
        }
        quote = {
            "bid": 95.0,
            "ask": 95.5,
            "ltp": 95.25,
            "spread_pct": 0.52,
            "quote_age_ms": 100,
            "oi": 900000,
            "volume": 400000,
            "delta": -0.46,
            "iv": 15.0,
        }
        u_quote = {
            "spot_ltp": 22599.15,
            "open": 22680.0,
            "high": 22697.55,
            "low": 22554.85,
            "close": 22670.0,
            "vwap": 22620.0,
            "quote_age_ms": 100,
        }

        res = engine.evaluate_contract(
            contract=contract,
            quote=quote,
            underlying_quote=u_quote,
            underlying_df=df,
            macro_context={"intraday_regime_verdict": "INTRADAY_BEAR_MOMENTUM"},
            as_of_time=datetime(2026, 10, 22, 10, 3, 33),
            enforce_cutoff=True,
            enforce_regime_gate=True,
        )

        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        reasons_joined = " | ".join(res.get("rejection_reasons", []))
        self.assertIn("Opening Range Trap Guard", reasons_joined)

    def test_03_genuine_day_high_and_orb_breakout_passes_without_starvation(self):
        """
        Verifies that a genuine Day High & ORB breakout (Oct 26 / Oct 29 scenario)
        passes all regime & breakout gates without starvation.
        """
        base_dt = datetime(2026, 10, 26, 9, 15, 0)
        rows = []
        for i in range(40):  # 09:15 to 09:54
            ts = (base_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            c = 22450.0 + i * 2.5
            o, h, l = c - 2.0, c + 3.0, c - 3.0
            rows.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 5000})
        df = pd.DataFrame(rows)

        gate = OptionRegimeGate(db_path=self.db_path)
        res = gate.evaluate_regime(
            underlying="NIFTY",
            opt_type="CE",
            as_of_time=datetime(2026, 10, 26, 9, 55, 0),
            continuous_bars=df,
            spot_ltp=22550.4,
            day_high=22550.5,
            day_low=22444.5,
            option_ask=212.15,
            target_pct=35.0,
            lot_size=65,
            is_breakout=True,
            is_reversal_recovery=False,
            heavyweight_quotes={
                "RELIANCE": {"spot_ltp": 1420.0, "vwap": 1410.0},
                "HDFCBANK": {"spot_ltp": 1750.0, "vwap": 1740.0},
            },
            enforce_cutoff=True,
            option_iv=14.0,
        )
        self.assertTrue(res["hurdles_passed"], f"Expected genuine breakout to pass, got: {res['rejection_reasons']}")

    @patch("app.analytics.option_sniffer.option_db.OptionDB.has_paper_stop_loss_today")
    def test_04_microstructure_hud_locks_post_0945_when_trapped_in_orb_and_blocks_live_order(self, mock_canary):
        """
        Verifies that after 09:45 IST, if Spot is trapped inside the Opening Range
        (breakout_direction == 'INSIDE_RANGE'), OptionMicrostructureEvaluator locks the gate
        ('OPENING_RANGE_TRAP_LOCKED') and place_real_entry_order blocks the order.
        """
        mock_canary.return_value = (False, "Zero paper stop losses today")
        OptionMicrostructureEvaluator._mock_spread_atm = 0.30
        OptionMicrostructureEvaluator._mock_kaufman_er = 0.29  # Yellow (< 0.35)
        OptionMicrostructureEvaluator._mock_or_data = {
            "is_accepted": False,
            "or_high": 23942.50,
            "or_low": 23696.15,
            "breakout_direction": "INSIDE_RANGE",
            "range_pct": 1.04,
            "vol_floor_passed": True,
        }

        eval_dt = datetime(2026, 11, 4, 10, 51, 8)
        ms_res = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=eval_dt, force_refresh=True)
        self.assertFalse(ms_res["is_unlocked"])
        self.assertFalse(ms_res["kaufman_ok"])
        self.assertIn("OPENING_RANGE_TRAP_LOCKED", ms_res["unlock_reason"])

        # Verify place_real_entry_order blocks when HUD is locked
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        adapter = get_option_live_adapter()
        prev_armed = adapter._is_live_armed
        try:
            adapter._is_live_armed = True
            intent_engine = OrderIntentEngine()
            with patch("app.analytics.option_sniffer.option_live_order_intent.datetime") as mock_dt:
                mock_dt.now.return_value = eval_dt
                mock_dt.strptime = datetime.strptime
                order_res = intent_engine.place_real_entry_order("INTENT_TEST_NOV4")
            self.assertEqual(order_res["status"], "BLOCKED")
            self.assertIn("MICROSTRUCTURE_GATE_LOCKED", order_res["reason"])
        finally:
            adapter._is_live_armed = prev_armed

    def test_05_surgical_5_alpha_calibrations_itm_otm_and_session_history(self):
        """
        Verifies the 5 isolated alpha calibrations:
        1. OptionAlphaModel does NOT penalize ITM strikes (moneyness > 0) or near-ATM strikes (<= 0.45% OTM),
           preserving Oct 26 (+Rs 3,488) and Oct 30 (+Rs 3,686) while penalizing far-OTM (> 0.45%).
        2. OptionStreamManager.get_underlying_history_df defaults to limit=375 (full session >= 09:15 IST).
        """
        import inspect
        from app.analytics.option_sniffer.option_alpha_model import OptionAlphaModel
        from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

        sig = inspect.signature(OptionStreamManager.get_underlying_history_df)
        self.assertEqual(sig.parameters["limit"].default, 375)

        alpha_model = OptionAlphaModel(model_artifact=None)
        base_feats = {
            "is_call": 1.0,
            "spread_pct": 0.40,
            "delta": 0.55,
            "upstream_aligned": 0.85,
            "upstream_consensus_conf": 75.0,
            "spot_rsi": 60.0,
            "spot_vwap_dist_pct": 0.25,
            "spot_price": 22988.0,
            "vwap": 22940.0,
            "spot_atr": 45.0,
        }
        # 1. ITM contract (Oct 30 22850 CE @ Spot 22988 -> moneyness = +0.0060, abs_moneyness = 0.0060)
        itm_feats = dict(base_feats, moneyness=0.0060, abs_moneyness=0.0060, delta=0.65)
        atm_feats = dict(base_feats, moneyness=0.0, abs_moneyness=0.0, delta=0.52)
        far_otm_feats = dict(base_feats, moneyness=-0.0115, abs_moneyness=0.0115, delta=0.25)

        res_itm = alpha_model.predict_contract(itm_feats, is_breakout=True)
        res_atm = alpha_model.predict_contract(atm_feats, is_breakout=True)
        res_far_otm = alpha_model.predict_contract(far_otm_feats, is_breakout=True)

        self.assertEqual(res_itm["calibrated_prob"], res_atm["calibrated_prob"])
        self.assertLess(res_far_otm["calibrated_prob"], res_atm["calibrated_prob"] - 0.06)

    def test_06_premarket_0900_auction_ohlc_sanitization_and_gate7_live_conviction(self):
        """
        Verifies the Nov 5 09:49 IST root-cause fix:
        1. When Upstox raw underlying quote has 09:00 pre-open auction spike (high=24580.75, low=23608.50)
           while true >= 09:15 IST session range is [24296.30, 24442.00] and spot=24440.00 (at true Day High),
           OptionSignalEngine and OptionFeatureExtractor sanitize u_high/u_low against >= 09:15 session bars
           so genuine breakouts are NOT falsely flagged as -140pt pullbacks from High.
        2. Even when a contract stays in STATE_WATCH due to a gate rejection, calibrated_prob and
           expected_net_return are computed for Gate 7 HUD transparency.
        """
        from app.analytics.option_sniffer.option_feature_extractor import OptionFeatureExtractor

        base_dt = datetime(2026, 11, 5, 9, 15, 0)
        rows = [
            # Pre-open 09:00 auction spike bar
            {"timestamp": "2026-11-05 09:00:00", "open": 24363.25, "high": 24580.75, "low": 23608.50, "close": 24384.30, "volume": 0},
        ]
        for i in range(35):  # 09:15 to 09:49
            ts = (base_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            if i < 15:
                o, h, l, c = 24340.0, 24391.95, 24296.30, 24360.0
            else:
                step = i - 15
                c = 24392.0 + step * 2.5
                o, h, l = c - 2.0, min(24442.0, c + 2.0), c - 2.0
            rows.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 5000})
        df = pd.DataFrame(rows)

        contract = {
            "instrument_key": "NSE_FO|39962",
            "trading_symbol": "NIFTY 24400 CE 09 NOV 26",
            "underlying": "NIFTY",
            "strike": 24400.0,
            "option_type": "CE",
            "expiry": "2026-11-09",
            "lot_size": 65,
        }
        quote = {
            "bid": 154.0,
            "ask": 154.5,
            "ltp": 154.25,
            "spread_pct": 0.32,
            "quote_age_ms": 100,
            "oi": 1200000,
            "volume": 650000,
            "delta": 0.54,
            "iv": 13.8,
        }
        # Contaminated raw Upstox OHLC with 09:00 pre-open auction spike
        contaminated_u_quote = {
            "spot_ltp": 24440.0,
            "open": 24363.25,
            "high": 24580.75,
            "low": 23608.50,
            "close": 24350.0,
            "vwap": 24385.0,
            "quote_age_ms": 100,
        }

        feats = OptionFeatureExtractor.extract_features(
            contract=contract,
            quote=quote,
            underlying_quote=contaminated_u_quote,
            underlying_df=df,
            macro_context={"intraday_regime_verdict": "INTRADAY_BULL_MOMENTUM"},
            as_of_time=datetime(2026, 11, 5, 9, 49, 30),
        )
        # Blended RSI (0.5 * 100 candle RSI + 0.5 * 70 structural RSI cap) == 85.0
        self.assertEqual(feats["spot_rsi"], 85.0)

        mock_model = MagicMock()
        mock_model.version = "v1.0-champion-gbm"
        mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.495,
            "expected_net_return_pct": 5.8,
        }
        engine = OptionSignalEngine(model=mock_model)

        # Verify genuine breakout with contaminated pre-open u_quote is NOT blocked by Bull Reversal Guard
        clean_res = engine.evaluate_contract(
            contract=contract,
            quote=quote,
            underlying_quote=contaminated_u_quote,
            underlying_df=df,
            macro_context={"intraday_regime_verdict": "INTRADAY_BULL_MOMENTUM"},
            as_of_time=datetime(2026, 11, 5, 9, 49, 30),
            enforce_cutoff=True,
            enforce_regime_gate=False,
            defer_execution=True,
        )
        reasons_str = " | ".join(clean_res.get("rejection_reasons", []))
        self.assertNotIn("Reversal Guard", reasons_str)

        # Even if a contract is rejected by a gate (e.g. enforce_regime_gate=False with low volume),
        # calibrated_prob and expected_net_return are still populated when quote is valid
        low_vol_quote = dict(quote, volume=50)
        watch_res = engine.evaluate_contract(
            contract=contract,
            quote=low_vol_quote,
            underlying_quote=contaminated_u_quote,
            underlying_df=df,
            macro_context={"intraday_regime_verdict": "INTRADAY_BULL_MOMENTUM"},
            as_of_time=datetime(2026, 11, 5, 9, 49, 30),
            enforce_cutoff=True,
            enforce_regime_gate=False,
        )
        self.assertEqual(watch_res["status"], STATE_WATCH)
        self.assertAlmostEqual(watch_res["calibrated_prob"], 0.495, places=3)
        self.assertAlmostEqual(watch_res["expected_net_return"], 5.8, places=2)


if __name__ == "__main__":
    unittest.main()

