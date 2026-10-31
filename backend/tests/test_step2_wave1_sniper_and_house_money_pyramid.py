"""
Step 2 Verification Suite: Wave-1 Sniper Discipline & Same-Contract House-Money Scale-In
Verifies:
1. Updated BANKNIFTY lot size (30) and Step 2 constants in option_constants.py.
2. Same-Contract House-Money Scale-In (1 Lot -> 2 Lots at +15.0% MFE in 09:43-11:30 window)
   with Unified +8.5% Breakeven Floor (initial_entry_ask * 1.085) and exact 2-order statutory charges.
3. Gate 10C: Post-Harvest Wave Exhaustion Guard (60-minute same-direction cooldown after
   closing a winner with MFE >= +15.0%).
4. Gate 9: Underlying 2-Lot Capacity Tracking (scaled 2-lot trade blocks opening a 2nd contract)
   and Far-OTM Pyramiding Guard (> 1.5 strike intervals OTM blocked for 2nd contracts).
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.analytics.option_sniffer import option_constants as const
from app.analytics.option_sniffer.option_constants import DIRECTION_BUY_CE, STATE_ACTIVE
from app.analytics.option_sniffer.option_db import OptionDB, init_option_sniffer_tables
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine


class TestStep2Wave1SniperAndHouseMoneyPyramid(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db.name
        self.temp_db.close()

        self.db_patcher = patch(
            "app.analytics.option_sniffer.option_db.get_db_path",
            return_value=self.temp_db_path,
        )
        self.db_patcher.start()
        init_option_sniffer_tables()
        OptionSignalEngine.reset_class_state()

        self.mock_model = MagicMock()
        self.mock_model.version = "test-step2-v1.0"
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.68,
            "expected_net_return_pct": 6.0,
            "raw_score": 0.72,
        }

    def tearDown(self):
        OptionSignalEngine.reset_class_state()
        self.db_patcher.stop()
        if os.path.exists(self.temp_db_path):
            os.unlink(self.temp_db_path)

    def test_01_constants_and_banknifty_lot_size(self):
        """Verify BANKNIFTY lot size is 30 and Step 2 constants are properly configured."""
        self.assertEqual(const.DEFAULT_LOT_SIZES["NIFTY"], 65)
        self.assertEqual(const.DEFAULT_LOT_SIZES["BANKNIFTY"], 30)
        self.assertEqual(const.POST_HARVEST_WAVE_COOLDOWN_SECONDS, 3600)
        self.assertAlmostEqual(const.POST_HARVEST_MIN_MFE_PCT, 15.0)
        self.assertTrue(const.HOUSE_MONEY_SCALE_IN_ACTIVE)
        self.assertAlmostEqual(const.HOUSE_MONEY_SCALE_IN_TRIGGER_MFE_PCT, 15.0)
        self.assertAlmostEqual(const.HOUSE_MONEY_UNIFIED_FLOOR_PCT, 8.5)
        self.assertAlmostEqual(const.PYRAMIDING_MAX_OTM_STRIKES, 1.5)

    @patch("app.analytics.option_sniffer.option_paper_execution.send_telegram_message")
    def test_02_same_contract_house_money_scale_in_and_unified_floor(self, mock_tg):
        """
        Verify a 1-lot (65 qty) morning trade scales in to 2 lots (130 qty) when MFE >= +15%,
        locks the Unified +8.5% floor (initial_entry_ask * 1.085), and computes 2-order charges.
        """
        signal = {
            "signal_id": "sig_step2_scale",
            "created_at": "2026-10-30 10:05:00",
            "instrument_key": "NSE_FO|NIFTY_22800_CE",
            "trading_symbol": "NIFTY26NOV22800CE",
            "underlying": "NIFTY",
            "strike": 22800.0,
            "expiry": "2026-11-05",
            "option_type": "CE",
            "lot_size": 65,
            "option_bid": 199.5,
            "option_ask": 200.0,
            "model_version": "v1.0",
            "signal_version": "v1.0",
            "data_freshness": "REAL_TIME",
        }
        trade_id = OptionPaperExecution.open_trade(
            signal, as_of_time=datetime(2026, 10, 30, 10, 5, 0)
        )
        self.assertIsNotNone(trade_id)

        # Simulate quote tick reaching +16% MFE (bid=232.0, ask=233.0 on initial_entry_ask=200.0)
        tick_time_1 = datetime(2026, 10, 30, 10, 18, 0)
        live_quotes = {
            "NSE_FO|NIFTY_22800_CE": {
                "instrument_key": "NSE_FO|NIFTY_22800_CE",
                "ltp": 232.5,
                "bid": 232.0,
                "ask": 233.0,
            }
        }
        closed_sweep_1 = OptionPaperExecution.update_open_trades(live_quotes, as_of_time=tick_time_1)
        self.assertEqual(len(closed_sweep_1), 0)  # Trade remains open and scaled in

        open_trades = OptionDB.get_open_paper_trades()
        self.assertEqual(len(open_trades), 1)
        t = open_trades[0]
        self.assertEqual(int(t["quantity"]), 130)
        self.assertEqual(int(t["scale_in_count"]), 1)
        self.assertAlmostEqual(float(t["initial_entry_ask"]), 200.0, places=2)
        self.assertAlmostEqual(float(t["scale_in_price"]), 233.0, places=2)
        self.assertAlmostEqual(float(t["entry_ask"]), 216.5, places=2)  # (200.0 + 233.0) / 2
        self.assertAlmostEqual(float(t["entry_price_used"]), 216.5, places=2)

        # Now simulate price pulling back to 216.5 (below the 217.0 unified floor = 200.0 * 1.085)
        pullback_quotes = {
            "NSE_FO|NIFTY_22800_CE": {
                "instrument_key": "NSE_FO|NIFTY_22800_CE",
                "ltp": 216.8,
                "bid": 216.5,
                "ask": 217.2,
            }
        }
        res_close = OptionPaperExecution.update_open_trades(
            pullback_quotes,
            as_of_time=datetime(2026, 10, 30, 10, 24, 0),
        )
        self.assertEqual(len(res_close), 1)
        self.assertEqual(res_close[0]["status"], "CLOSED")
        self.assertEqual(res_close[0]["exit_reason"], "TRAILING_STOP_HIT")
        # Verify 2-order statutory charges (> ₹100 since 2 separate orders of ₹20 + ₹20 = ₹80 brokerage + STT/exchange)
        self.assertGreater(float(res_close[0]["charges"]), 100.0)

    @patch("app.analytics.option_sniffer.option_paper_execution.send_telegram_message")
    def test_03_gate_10c_post_harvest_wave_exhaustion_guard(self, mock_tg):
        """
        Verify that after closing a winning trade with MFE >= +15%, same-direction
        re-entries on the same underlying are blocked for 60 minutes (Gate 10C),
        while after 60 minutes they are allowed again.
        """
        signal = {
            "signal_id": "sig_step2_wave1",
            "created_at": "2026-10-30 09:55:00",
            "instrument_key": "NSE_FO|NIFTY_22800_CE",
            "trading_symbol": "NIFTY26NOV22800CE",
            "underlying": "NIFTY",
            "strike": 22800.0,
            "expiry": "2026-11-05",
            "option_type": "CE",
            "lot_size": 65,
            "option_bid": 199.5,
            "option_ask": 200.0,
            "model_version": "v1.0",
            "signal_version": "v1.0",
            "data_freshness": "REAL_TIME",
        }
        OptionPaperExecution.open_trade(
            signal, as_of_time=datetime(2026, 10, 30, 9, 55, 0)
        )

        # Tick 1: Surges to +25% MFE (bid=250.0) at 10:10 IST
        OptionPaperExecution.update_open_trades(
            {
                "NSE_FO|NIFTY_22800_CE": {
                    "instrument_key": "NSE_FO|NIFTY_22800_CE",
                    "ltp": 250.5,
                    "bid": 250.0,
                    "ask": 251.0,
                }
            },
            as_of_time=datetime(2026, 10, 30, 10, 10, 0),
        )

        # Tick 2: Pulls back to 220.0 -> closes as a winner at 10:15 IST
        closed = OptionPaperExecution.update_open_trades(
            {
                "NSE_FO|NIFTY_22800_CE": {
                    "instrument_key": "NSE_FO|NIFTY_22800_CE",
                    "ltp": 220.5,
                    "bid": 220.0,
                    "ask": 221.0,
                }
            },
            as_of_time=datetime(2026, 10, 30, 10, 15, 0),
        )
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["status"], "CLOSED")
        self.assertGreater(closed[0]["net_pnl"], 0.0)

        # Verify DB helper detects the harvested wave 10 minutes later (10:25 IST)
        check_time_10m = datetime(2026, 10, 30, 10, 25, 0)
        recent_wave = OptionDB.get_recent_harvested_wave("NIFTY", "CE", as_of_time=check_time_10m)
        self.assertIsNotNone(recent_wave)
        self.assertLess(recent_wave["elapsed_seconds"], 3600.0)

        # Verify Gate 10C in OptionSignalEngine blocks a chased CE re-entry at 10:25 IST
        engine = OptionSignalEngine(model=self.mock_model)
        contract_ce = {
            "instrument_key": "NSE_FO|NIFTY_22850_CE",
            "trading_symbol": "NIFTY26NOV22850CE",
            "underlying": "NIFTY",
            "strike": 22850.0,
            "expiry": "2026-11-05",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "bid": 150.0,
            "ask": 150.5,
            "ltp": 150.2,
            "oi": 50000,
            "volume": 2500,
            "spread_pct": 0.33,
            "quote_age_ms": 100,
        }
        underlying = {
            "spot_ltp": 22860.0,
            "open": 22750.0,
            "high": 22870.0,
            "low": 22740.0,
            "close": 22855.0,
            "vwap": 22810.0,
            "quote_age_ms": 100,
        }
        res_10m = engine.evaluate_contract(
            contract_ce,
            quote,
            underlying,
            as_of_time=check_time_10m,
            enforce_cutoff=True,
            enforce_regime_gate=False,
        )
        self.assertNotEqual(res_10m["status"], STATE_ACTIVE)
        self.assertFalse(res_10m["hurdles_passed"])
        self.assertTrue(
            any("Post-Harvest Wave Exhaustion Guard" in r for r in res_10m.get("rejection_reasons", []))
        )

        # Verify 65 minutes later (11:20:01 IST) cooldown has expired and Gate 10C passes
        check_time_65m = datetime(2026, 10, 30, 11, 20, 1)
        expired_wave = OptionDB.get_recent_harvested_wave("NIFTY", "CE", as_of_time=check_time_65m)
        self.assertIsNone(expired_wave)
        res_65m = engine.evaluate_contract(
            contract_ce,
            quote,
            underlying,
            as_of_time=check_time_65m,
            enforce_cutoff=True,
            enforce_regime_gate=False,
        )
        self.assertEqual(res_65m["status"], STATE_ACTIVE)
        self.assertTrue(res_65m["hurdles_passed"])

    @patch("app.analytics.option_sniffer.option_paper_execution.send_telegram_message")
    def test_04_gate_9_scaled_trade_and_far_otm_pyramiding_guard(self, mock_tg):
        """
        Verify Gate 9:
        1) Blocks a 2nd contract when the 1st open contract is winning (+18% MFE) but the
           candidate 2nd contract is far-OTM (> 1.5 strike intervals OTM, e.g. 22950 CE when spot is 22700).
        2) Blocks a 2nd contract when the 1st open contract has already scaled to 2 lots (130 qty).
        """
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute(
            """
            INSERT INTO option_sniffer_paper_trades (
                trade_id, instrument_key, trading_symbol, underlying, option_type,
                strike, expiry, lot_size, quantity, entry_time, entry_ask,
                entry_price_used, status, mfe_pct, net_pnl_pct
            ) VALUES (?, ?, ?, ?, 'CE', 22700, '2026-11-05', 65, 65, '2026-10-29 10:20:00', 200.0, 200.0, 'OPEN', 18.5, 8.2)
            """,
            ("open_step2_g9", "NSE_FO|NIFTY_22700_CE", "NIFTY26NOV22700CE", "NIFTY"),
        )
        conn.commit()
        conn.close()

        engine = OptionSignalEngine(model=self.mock_model)

        # Case 1: Evaluate far-OTM 2nd strike (22950 CE when spot is 22700 -> 5.0 strikes OTM > 1.5 limit)
        far_otm_contract = {
            "instrument_key": "NSE_FO|NIFTY_22950_CE",
            "trading_symbol": "NIFTY26NOV22950CE",
            "underlying": "NIFTY",
            "strike": 22950.0,
            "expiry": "2026-11-05",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "bid": 120.0,
            "ask": 120.5,
            "ltp": 120.2,
            "oi": 50000,
            "volume": 2500,
            "spread_pct": 0.4,
            "quote_age_ms": 100,
        }
        underlying = {
            "spot_ltp": 22700.0,
            "open": 22600.0,
            "high": 22710.0,
            "low": 22590.0,
            "close": 22700.0,
            "vwap": 22650.0,
            "quote_age_ms": 100,
        }
        res_far_otm = engine.evaluate_contract(
            far_otm_contract,
            quote,
            underlying,
            as_of_time=datetime(2026, 10, 29, 10, 30, 0),
            enforce_cutoff=True,
            enforce_regime_gate=False,
        )
        self.assertNotEqual(res_far_otm["status"], STATE_ACTIVE)
        self.assertFalse(res_far_otm["hurdles_passed"])
        self.assertTrue(
            any("Far-OTM pyramiding blocked" in r for r in res_far_otm.get("rejection_reasons", []))
        )

        # Case 2: Scale the 1st trade to 2 lots (130 qty) and verify even an ATM 2nd contract is blocked
        OptionDB.scale_in_paper_trade(
            trade_id="open_step2_g9",
            new_quantity=130,
            blended_entry_ask=218.25,
            initial_entry_ask=200.0,
            scale_in_price=236.5,
            scale_in_time="2026-10-29 10:28:00",
        )
        atm_2nd_contract = {
            "instrument_key": "NSE_FO|NIFTY_22750_CE",
            "trading_symbol": "NIFTY26NOV22750CE",
            "underlying": "NIFTY",
            "strike": 22750.0,
            "expiry": "2026-11-05",
            "option_type": "CE",
            "lot_size": 65,
        }
        res_scaled = engine.evaluate_contract(
            atm_2nd_contract,
            quote,
            underlying,
            as_of_time=datetime(2026, 10, 29, 10, 31, 0),
            enforce_cutoff=True,
            enforce_regime_gate=False,
        )
        self.assertNotEqual(res_scaled["status"], STATE_ACTIVE)
        self.assertFalse(res_scaled["hurdles_passed"])
        self.assertTrue(
            any("Anti-Clustering Shield: Active NIFTY position exists" in r for r in res_scaled.get("rejection_reasons", []))
        )


if __name__ == "__main__":
    unittest.main()
