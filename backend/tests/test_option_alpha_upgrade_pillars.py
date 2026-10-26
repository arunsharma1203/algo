"""
Unit & Integration Tests for Option Sniffer 4-Pillar Modernization:
  - Pillar 1: Underlying Structure Invalidation Exit (Immediate Loss Truncation)
  - Pillar 2: Scalper-Runner Regime-Adaptive Target Scaling
  - Pillar 3: Multi-Bar Close & Volume Ratio Breakout Confirmation
  - Pillar 4: Unified Model Expectancy & Dynamic Breakeven Hurdle
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock
import pandas as pd

from app.analytics.option_sniffer.option_constants import (
    STOP_LOSS_PCT,
    TARGET_GAIN_PCT,
    DYNAMIC_SL_MIN_PCT,
    DYNAMIC_SL_MAX_PCT,
    DYNAMIC_TGT_MIN_PCT,
    DYNAMIC_TGT_MAX_PCT,
    DYNAMIC_SCALP_TGT_MIN_PCT,
    DYNAMIC_SCALP_TGT_MAX_PCT,
    NARROW_RANGE_THRESHOLD_PTS,
    STRUCTURE_INVALIDATION_BUFFER_PCT,
    STRUCTURE_INVALIDATION_MIN_PTS_NIFTY,
    STRUCTURE_INVALIDATION_MIN_PTS_BANKNIFTY,
    BREAKOUT_VOLUME_MIN_RATIO,
    STATE_WATCH,
    STATE_ACTIVE,
    DIRECTION_BUY_CE,
    DIRECTION_BUY_PE,
)
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.option_sniffer.option_db import OptionDB, init_option_sniffer_tables
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
from app.analytics.option_sniffer.option_alpha_model import OptionAlphaModel


class TestOptionAlphaUpgradePillars(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp_db.name
        self.tmp_db.close()

        self.db_patcher = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.db_patcher.start()
        self.live_db_patcher = patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path)
        self.live_db_patcher.start()

        # Initialize schema in isolated temp DB
        init_option_sniffer_tables()
        from app.analytics.option_sniffer.option_live_order_intent import init_live_order_tables
        init_live_order_tables()

        # Reset all class state (cooldowns, loss history, freeze timers)
        OptionSignalEngine.reset_class_state()

    def tearDown(self):
        OptionSignalEngine.reset_class_state()
        self.live_db_patcher.stop()
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    # =========================================================================
    # PILLAR 1: UNDERLYING STRUCTURE INVALIDATION EXIT TESTS
    # =========================================================================

    def test_pillar1_ce_structure_invalidation_exit(self):
        """
        When a CALL (CE) trade is open, if underlying Spot plunges below VWAP
        by > 0.08% AND drops >= 18 pts below entry spot, the position must exit
        immediately with STRUCTURE_INVALIDATION_BEAR before reaching the hard -15% SL.
        """
        signal = {
            "instrument_key": "NSE_FO|NIFTY26OCT25000CE",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
            "spot_price": 25000.0,
            "option_ask": 100.0,
            "option_bid": 98.0,
            "delta": 0.50,
            "iv": 14.0,
            "spot_atr_pct": 0.80,
            "day_range_pts": 150.0,
            "is_breakout": True,
        }
        trade_id = OptionPaperExecution.open_trade(signal)
        self.assertIsNotNone(trade_id)

        # Spot plunges to 24975 (-25 pts from entry 25000) and VWAP is 25010
        # 24975 < 25010 * 0.9992 (24990) -> below VWAP by > 0.08%
        # 24975 <= 25000 - 18.0 (24982) -> adverse > 18 pts
        # Option bid dropped to 95.0 (-5.0% loss, well above the -13.8% hard SL)
        live_quotes = {
            "NSE_FO|NIFTY26OCT25000CE": {"bid": 95.0, "ask": 96.0, "ltp": 95.0}
        }
        underlying_quotes = {
            "NIFTY": {"spot_ltp": 24975.0, "vwap": 25010.0}
        }

        closed = OptionPaperExecution.update_open_trades(
            live_quotes_map=live_quotes,
            underlying_quotes_map=underlying_quotes
        )
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "STRUCTURE_INVALIDATION_BEAR")
        self.assertEqual(closed[0]["exit_bid"], 95.0)

        # Verify trade is now closed in SQLite
        open_trades = OptionDB.get_open_paper_trades()
        self.assertEqual(len(open_trades), 0)

        # Verify direction SL cooldown activated for NIFTY CE
        cd_key = "NIFTY_CE"
        self.assertIn(cd_key, OptionSignalEngine._DIRECTION_SL_COOLDOWNS)

    def test_pillar1_pe_structure_invalidation_exit(self):
        """
        When a PUT (PE) trade is open, if underlying Spot surges above VWAP
        by > 0.08% AND rises >= 18 pts above entry spot, the position must exit
        immediately with STRUCTURE_INVALIDATION_BULL.
        """
        signal = {
            "instrument_key": "NSE_FO|NIFTY26OCT25000PE",
            "trading_symbol": "NIFTY 25000 PE",
            "underlying": "NIFTY",
            "option_type": "PE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
            "spot_price": 25000.0,
            "option_ask": 110.0,
            "option_bid": 108.0,
            "delta": -0.50,
            "iv": 14.0,
            "spot_atr_pct": 0.80,
            "day_range_pts": 140.0,
            "is_breakout": True,
        }
        trade_id = OptionPaperExecution.open_trade(signal)
        self.assertIsNotNone(trade_id)

        # Spot surges to 25030 (+30 pts from entry 25000) and VWAP is 24995
        # 25030 > 24995 * 1.0008 (25015) -> above VWAP by > 0.08%
        # 25030 >= 25000 + 18.0 (25018) -> adverse > 18 pts
        # Option bid dropped slightly to 105.0 (-4.5% loss)
        live_quotes = {
            "NSE_FO|NIFTY26OCT25000PE": {"bid": 105.0, "ask": 106.0, "ltp": 105.0}
        }
        underlying_quotes = {
            "NIFTY": {"spot_ltp": 25030.0, "vwap": 24995.0}
        }

        closed = OptionPaperExecution.update_open_trades(
            live_quotes_map=live_quotes,
            underlying_quotes_map=underlying_quotes
        )
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "STRUCTURE_INVALIDATION_BULL")
        self.assertEqual(closed[0]["exit_bid"], 105.0)

        # Verify direction SL cooldown activated for NIFTY PE
        cd_key = "NIFTY_PE"
        self.assertIn(cd_key, OptionSignalEngine._DIRECTION_SL_COOLDOWNS)

    def test_pillar1_normal_pullback_does_not_invalidation_exit(self):
        """
        A normal 5-point minor pullback inside VWAP should NOT trigger invalidation.
        """
        signal = {
            "instrument_key": "NSE_FO|NIFTY26OCT25000CE",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
            "spot_price": 25000.0,
            "option_ask": 100.0,
            "option_bid": 98.0,
            "delta": 0.50,
            "spot_atr_pct": 0.80,
        }
        trade_id = OptionPaperExecution.open_trade(signal)
        self.assertIsNotNone(trade_id)

        # Spot only pulled back to 24995 (-5 pts), still near VWAP 24998
        live_quotes = {
            "NSE_FO|NIFTY26OCT25000CE": {"bid": 97.0, "ask": 98.0, "ltp": 97.0}
        }
        underlying_quotes = {
            "NIFTY": {"spot_ltp": 24995.0, "vwap": 24998.0}
        }

        closed = OptionPaperExecution.update_open_trades(
            live_quotes_map=live_quotes,
            underlying_quotes_map=underlying_quotes,
            enforce_cutoff=False
        )
        self.assertEqual(len(closed), 0)
        open_trades = OptionDB.get_open_paper_trades()
        self.assertEqual(len(open_trades), 1)

    # =========================================================================
    # PILLAR 2: REGIME-ADAPTIVE TARGET SCALING TESTS
    # =========================================================================

    def test_pillar2_scalp_target_clamping_in_narrow_range(self):
        """
        When day_range_pts < 110.0 and is_breakout is False,
        target must clamp to Scalp Mode [+18.0%, +22.0%].
        """
        sl, tgt, atr, delta = OptionPaperExecution.calculate_dynamic_sl_and_target(
            underlying="NIFTY",
            spot_price=25000.0,
            option_ask=145.0,
            delta=0.50,
            spot_atr_pct=0.80,
            day_range_pts=75.0,  # Narrow 75-point morning range
            is_breakout=False
        )
        self.assertGreaterEqual(tgt, DYNAMIC_SCALP_TGT_MIN_PCT)
        self.assertLessEqual(tgt, DYNAMIC_SCALP_TGT_MAX_PCT)

    def test_pillar2_runner_target_expansion_on_trend_breakout(self):
        """
        When day_range_pts >= 110.0 or is_breakout is True,
        target must expand to Runner Mode [+25.0%, +45.0%].
        """
        sl, tgt, atr, delta = OptionPaperExecution.calculate_dynamic_sl_and_target(
            underlying="NIFTY",
            spot_price=25000.0,
            option_ask=145.0,
            delta=0.50,
            spot_atr_pct=0.80,
            day_range_pts=160.0,  # Trend expansion day
            is_breakout=True
        )
        self.assertGreaterEqual(tgt, DYNAMIC_TGT_MIN_PCT)
        self.assertLessEqual(tgt, DYNAMIC_TGT_MAX_PCT)

    def test_pillar2_scalp_target_met_exit(self):
        """
        A trade opened in a narrow day with a scalp target
        should exit cleanly with TARGET_MET when bid hits target gain.
        """
        signal = {
            "instrument_key": "NSE_FO|NIFTY26OCT25000CE",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
            "spot_price": 25000.0,
            "option_ask": 100.0,
            "option_bid": 98.0,
            "delta": 0.50,
            "spot_atr_pct": 0.80,
            "day_range_pts": 85.0,  # Narrow morning range
            "is_breakout": False,
        }
        with patch("app.analytics.option_sniffer.option_paper_execution.OPTION_EXECUTION_ENGINE_MODE", "DYNAMIC_V2"):
            trade_id = OptionPaperExecution.open_trade(signal)
        self.assertIsNotNone(trade_id)

        # Check assigned target is within scalp bounds
        open_trades = OptionDB.get_open_paper_trades()
        self.assertEqual(len(open_trades), 1)
        self.assertLessEqual(open_trades[0]["dynamic_target_pct"], DYNAMIC_SCALP_TGT_MAX_PCT)

        # Option price surges to ₹129.0 (+29.0% gain)
        live_quotes = {
            "NSE_FO|NIFTY26OCT25000CE": {"bid": 129.0, "ask": 130.0, "ltp": 129.0}
        }
        closed = OptionPaperExecution.update_open_trades(live_quotes_map=live_quotes)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "TARGET_MET")
        self.assertGreater(closed[0]["net_pnl"], 0)

    # =========================================================================
    # PILLAR 3: MULTI-BAR CLOSE & VOLUME RATIO BREAKOUT CONFIRMATION TESTS
    # =========================================================================

    def test_pillar3_breakout_rejected_on_low_volume(self):
        """
        When Spot is at Day High but candle volume ratio is < 1.15x SMA,
        Gate 5B flags low volume and rejects the trade (remains in WATCH state).
        """
        engine = OptionSignalEngine()

        contract = {
            "instrument_key": "NSE_FO|40001",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "option_type": "CE",
            "expiry": "2026-10-29",
            "lot_size": 65,
        }
        quote = {
            "instrument_key": "NSE_FO|40001",
            "ltp": 100.0,
            "bid": 99.0,
            "ask": 100.0,
            "bid_qty": 500,
            "ask_qty": 400,
            "spread_pct": 1.0,
            "volume": 2000,
            "oi": 15000,
            "delta": 0.50,
            "iv": 13.5,
            "timestamp_ms": int(datetime.now().timestamp() * 1000),
            "quote_age_ms": 100,
            "is_valid": True,
        }
        # Spot at 25010 (Day High is 25010), but day range is 60 pts (trapped in chop)
        underlying_quote = {
            "underlying": "NIFTY",
            "spot_ltp": 25010.0,
            "high": 25010.0,
            "low": 24950.0,
            "open": 24980.0,
            "close": 25010.0,
            "vwap": 24985.0,
            "quote_age_ms": 100,
            "timestamp_ms": int(datetime.now().timestamp() * 1000),
        }

        # 10 candles where the last candle volume is 200 while average is 1000 (ratio 0.20 < 1.15)
        candles = []
        for i in range(10):
            candles.append({
                "open": 24980.0 + i,
                "high": 24985.0 + i,
                "low": 24975.0 + i,
                "close": 24982.0 + i,
                "volume": 1000.0 if i < 9 else 200.0,  # Weak volume on breakout candle
            })
        u_df = pd.DataFrame(candles)

        res = engine.evaluate_contract(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            underlying_df=u_df,
            macro_context={"regime_verdict": "SIDEWAYS_CHOP"}
        )

        # Must be rejected because low volume prevents breakout qualification through regime shield
        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Regime Shield" in r for r in res["rejection_reasons"]))

    # =========================================================================
    # PILLAR 4: UNIFIED MODEL EXPECTANCY & DYNAMIC BREAKEVEN HURDLE TESTS
    # =========================================================================

    def test_pillar4_model_expectancy_dynamic_synchronization(self):
        """
        Verify OptionAlphaModel.predict_contract calculates expected net return
        and dynamic breakeven hurdle using the assigned dynamic SL and Target.
        """
        model = OptionAlphaModel.load()

        sample_features = {
            "is_call": 1.0,
            "spread_pct": 0.8,
            "delta": 0.50,
            "spot_rsi": 58.0,
            "spot_vwap_dist_pct": 0.15,
            "macro_bullish": 1.0,
            "macro_bearish": 0.0,
            "upstream_qlib_score": 70.0,
            "upstream_legacy_score": 65.0,
            "upstream_consensus_conf": 68.0,
            "upstream_aligned": 1.0,
            "rally_from_low_pct": 0.35,
            "spot_ret_3": 0.12,
            "spot_ret_1": 0.05,
        }

        # Scalp setup: Dynamic SL = 13.0%, Dynamic Target = 20.0%
        pred_scalp = model.predict_contract(
            sample_features,
            dynamic_sl_pct=13.0,
            dynamic_tgt_pct=20.0
        )
        self.assertIn("direction", pred_scalp)
        self.assertIn("expected_net_return_pct", pred_scalp)

        # Expected return formula verification:
        # P * 20.0 - (1-P) * 13.0 - (1.8 + 0.8*0.5 = 2.2)
        p = pred_scalp["calibrated_prob"]
        expected_ev = round((p * 20.0) - ((1.0 - p) * 13.0) - 2.2, 2)
        self.assertEqual(pred_scalp["expected_net_return_pct"], expected_ev)

        # Runner setup: Dynamic SL = 16.0%, Dynamic Target = 38.0%
        pred_runner = model.predict_contract(
            sample_features,
            dynamic_sl_pct=16.0,
            dynamic_tgt_pct=38.0
        )
        expected_runner_ev = round((p * 38.0) - ((1.0 - p) * 16.0) - 2.2, 2)
        self.assertEqual(pred_runner["expected_net_return_pct"], expected_runner_ev)

    # =========================================================================
    # OPTION STREAM MANAGER & REGIME TELEMETRY TESTS
    # =========================================================================

    def test_option_stream_manager_index_vwap_and_heavyweights(self):
        """
        Verify that OptionStreamManager computes accurate intraday session VWAP
        for cash indices (where ATP is 0/None) instead of collapsing to spot_ltp,
        and verifies that heavyweight quotes (RELIANCE) record exchange ATP.
        """
        from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

        manager = OptionStreamManager()
        manager.sync_monitored_contracts([])

        # 1. Feed simulated Nifty index tick with no exchange ATP (typical of cash indices)
        nifty_key = "NSE_INDEX|Nifty 50"
        nifty_tick = {
            nifty_key: {
                "fullFeed": {
                    "indexFF": {
                        "ltpc": {"ltp": 25050.0, "ltt": 1728000000000},
                        "marketOHLC": {
                            "ohlc": [{"open": 24900.0, "high": 25100.0, "low": 24900.0, "close": 24950.0}]
                        }
                    }
                }
            }
        }
        manager.on_feed_message(nifty_tick)
        nifty_quote = manager.get_underlying_quote("NIFTY")
        self.assertIsNotNone(nifty_quote)
        self.assertEqual(nifty_quote["spot_ltp"], 25050.0)
        # Expected pivot VWAP: (24900 + 25100 + 24900 + 25050) / 4.0 = 24987.5
        self.assertEqual(nifty_quote["vwap"], 24987.5)
        self.assertNotEqual(nifty_quote["spot_ltp"], nifty_quote["vwap"])

        # 2. Feed simulated RELIANCE heavyweight tick with real exchange ATP
        reliance_key = "NSE_EQ|INE002A01018"
        reliance_tick = {
            reliance_key: {
                "fullFeed": {
                    "marketFF": {
                        "ltpc": {"ltp": 2800.0, "ltt": 1728000000000},
                        "atp": 2792.5,
                        "marketOHLC": {
                            "ohlc": [{"open": 2780.0, "high": 2810.0, "low": 2780.0, "close": 2785.0}]
                        }
                    }
                }
            }
        }
        manager.on_feed_message(reliance_tick)
        reliance_quote = manager.get_underlying_quote("RELIANCE")
        self.assertIsNotNone(reliance_quote)
        self.assertEqual(reliance_quote["spot_ltp"], 2800.0)
        self.assertEqual(reliance_quote["vwap"], 2792.5)

    def test_orchestrator_market_regime_telemetry_reason_logic(self):
        """
        Verify that get_market_regime_telemetry() does not produce contradictory
        strings such as 'Bearish pressure below VWAP (+0.00%)' when drift is zero/near-zero.
        """
        from app.analytics.option_sniffer.option_sniffer_orchestrator import OptionSnifferOrchestrator

        orchestrator = OptionSnifferOrchestrator()

        # Scenario A: Zero drift with BEAR macro verdict -> should report equilibrium, NOT 'below VWAP (+0.00%)'
        with patch.object(orchestrator.stream_manager, "get_underlying_quote", return_value={
            "spot_ltp": 25000.0,
            "open": 24950.0,
            "high": 25150.0,
            "low": 24850.0,
            "vwap": 25000.0,
        }), patch("app.analytics.macro_engine.get_macro_regime", return_value={"regime_verdict": "BEAR"}):
            telemetry = orchestrator.get_market_regime_telemetry()
            self.assertNotIn("below VWAP (+0.00%)", telemetry["reason"])
            self.assertIn("VWAP equilibrium", telemetry["reason"])

        # Scenario B: Legitimate bearish drift (-0.20%)
        with patch.object(orchestrator.stream_manager, "get_underlying_quote", return_value={
            "spot_ltp": 24950.0,
            "open": 25100.0,
            "high": 25150.0,
            "low": 24850.0,
            "vwap": 25000.0,
        }), patch("app.analytics.macro_engine.get_macro_regime", return_value={"regime_verdict": "BEAR"}):
            telemetry_bear = orchestrator.get_market_regime_telemetry()
            self.assertEqual(telemetry_bear["regime"], "TRENDING_BEAR")
            self.assertIn("Bearish pressure below VWAP", telemetry_bear["reason"])
            self.assertIn("-0.20%", telemetry_bear["reason"])


if __name__ == "__main__":
    unittest.main()

