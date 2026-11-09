import unittest
from datetime import datetime
from unittest.mock import patch
import pandas as pd

from app.analytics.option_sniffer.option_microstructure_evaluator import OptionMicrostructureEvaluator
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
from app.analytics.option_sniffer.option_regime_gate import OptionRegimeGate
from app.analytics.option_sniffer.option_live_order_intent import OrderIntentEngine


class TestNov09FourProvenBugs(unittest.TestCase):
    def test_bug1_orb_acceptance_and_rule_e_synchronization(self):
        """
        Bug #1:
        - get_opening_range_status must not crash on missing volume_surge_ratio column
          and must grant >= 1.25x live_vol_ratio on liquid near-ATM breakouts (vol >= 500).
        - evaluate_dynamic_timing_gate Rule E (09:45-13:15) must require orb_status['is_accepted']
          when a valid ORB exists, so Frontend Light #3 and Backend is_unlocked never diverge.
        """
        orb_res = OptionMicrostructureEvaluator.get_opening_range_status(
            underlying="NIFTY",
            now_dt=datetime(2026, 11, 9, 10, 43, 18),
        )
        self.assertIn("is_accepted", orb_res)
        self.assertIn("volume_ratio", orb_res)

        with patch.object(
            OptionMicrostructureEvaluator,
            "get_atm_spread_pct",
            return_value=(0.18, True),
        ), patch.object(
            OptionMicrostructureEvaluator,
            "get_spot_kaufman_er",
            return_value=(0.38, True),
        ), patch.object(
            OptionMicrostructureEvaluator,
            "get_opening_range_status",
            return_value={
                "is_accepted": False,
                "or_high": 25084.20,
                "or_low": 24898.35,
                "breakout_direction": "BEARISH_BREAK",
                "volume_ratio": 1.0,
                "volume_ok": False,
                "range_pct": 0.74,
                "vol_floor_passed": True,
            },
        ), patch(
            "app.analytics.option_sniffer.option_db.OptionDB.has_paper_stop_loss_today",
            return_value=(False, ""),
        ):
            payload = OptionMicrostructureEvaluator.evaluate_microstructure_gate(
                dt=datetime(2026, 11, 9, 10, 43, 18),
                force_refresh=True,
                underlying="NIFTY",
            )
            self.assertFalse(
                payload["is_unlocked"],
                f"Rule E must block when ORB is not accepted, got: {payload['unlock_reason']}",
            )
            self.assertIn("OPENING_RANGE_BREAKOUT_UNCONFIRMED", payload["unlock_reason"])

    def test_bug2_emergency_priority_on_live_entry_and_stagnation_sync(self):
        """
        Bug #2:
        - place_real_entry_order must pass is_emergency=True to _call_upstox_broker_api('/order/place')
          so live entry orders never wait behind background discovery calls.
        - check_and_execute_live_exits must synchronize holding duration & stagnation exit with parent Paper trade.
        """
        import inspect
        src = inspect.getsource(OrderIntentEngine.place_real_entry_order)
        self.assertIn('"/order/place", payload=payload, is_emergency=True', src)
        exit_src = inspect.getsource(OrderIntentEngine.check_and_execute_live_exits)
        self.assertIn("parent_paper_exit_reason", exit_src)

    def test_bug4a_seven_bar_consecutive_candle_exhaustion_climax_guard(self):
        """
        Bug #4A:
        - 7+ consecutive same-direction 1m candles (in a two-sided session) triggers Climax Overextension Guard,
          catching Nov 04 (#307, 7 bars) and Nov 09 (#309, 9 bars) while allowing <= 6 bar impulse breakouts.
        """
        engine = OptionSignalEngine()
        rows = []
        base_dt = pd.Timestamp("2026-11-09 10:24:00")
        for i in range(11):
            o = 24950.0 + (i % 2) * 5.0
            c = o + (6.0 if i % 2 == 0 else -3.0)
            rows.append({
                "timestamp": base_dt + pd.Timedelta(minutes=i),
                "open": o,
                "high": max(o, c) + 2.0,
                "low": min(o, c) - 2.0,
                "close": c,
                "volume": 1000,
            })
        curr = 24960.0
        for i in range(9):
            o = curr
            c = curr - 14.0
            rows.append({
                "timestamp": base_dt + pd.Timedelta(minutes=11 + i),
                "open": o,
                "high": o + 1.0,
                "low": c - 1.0,
                "close": c,
                "volume": 1500,
            })
            curr = c
        df_9_bear = pd.DataFrame(rows)
        underlying_quote = {
            "spot_ltp": curr,
            "vwap": 24890.0,
            "open": 24950.0,
            "high": 25084.20,
            "low": curr - 1.0,
            "quote_age_ms": 100,
        }
        contract = {
            "instrument_key": "NSE_FO|48204",
            "trading_symbol": "NIFTY 24850 PE 10 NOV 26",
            "underlying": "NIFTY",
            "strike_price": 24850.0,
            "option_type": "PE",
            "expiry_date": "2026-11-10",
            "lot_size": 65,
        }
        quote = {
            "ltp": 195.85,
            "bid": 195.60,
            "ask": 196.05,
            "volume": 250000,
            "oi": 150000,
            "oi_change": 25000,
            "delta": -0.49,
            "iv": 22.0,
            "quote_age_ms": 100,
        }
        eval_9 = engine.evaluate_contract(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            underlying_df=df_9_bear,
            as_of_time=datetime(2026, 11, 9, 10, 43, 18),
        )
        reasons_9 = " | ".join(eval_9.get("rejection_reasons") or [])
        self.assertIn("consecutive 1m bearish candles detected (>= 7)", reasons_9)

    def test_bug4b_high_vrp_pinning_shield_with_moderate_er(self):
        """
        Bug #4B:
        - High-VRP Pinning Shield blocks entries when VRP >= +10.3% and Kaufman ER < 0.45.
        """
        gate = OptionRegimeGate()
        rows = []
        base_dt = pd.Timestamp("2026-11-09 10:00:00")
        price = 25000.0
        steps = [
            3, -2, 4, -2, 3, -1, 2, -2, 3, -1, 4, -2, 3, -2, -5, 2, -6, 3, -5, 2,
            -6, 2, -5, 3, -6, 2, -5, 2, -6, 3, -5, 2, -6, 1, -5,
        ]
        for i, st in enumerate(steps):
            o = price
            c = price + st
            rows.append({
                "timestamp": (base_dt + pd.Timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
                "open": o,
                "high": max(o, c) + 3.0,
                "low": min(o, c) - 3.0,
                "close": c,
                "volume": 1000,
                "is_complete": 1,
                "is_synthetic": 0,
                "is_gap": 0,
            })
            price = c
        df = pd.DataFrame(rows)
        res = gate.evaluate_regime(
            underlying="NIFTY",
            opt_type="PE",
            as_of_time=datetime(2026, 11, 9, 10, 35, 0),
            continuous_bars=df,
            spot_ltp=price,
            day_high=25020.0,
            day_low=price - 2.0,
            option_ask=196.0,
            target_pct=25.0,
            is_breakout=True,
            option_iv=30.08,
        )
        telemetry = res.get("telemetry") or {}
        reasons = res.get("rejection_reasons") or []
        self.assertGreaterEqual(telemetry.get("vrp", 0.0), 10.3)
        joined = " | ".join(reasons)
        self.assertIn("Pinning Shield", joined)


if __name__ == "__main__":
    unittest.main()
