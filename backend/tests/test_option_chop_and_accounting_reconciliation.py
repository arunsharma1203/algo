"""
Unit test suite verifying:
  1. Mid-Day Sideways Chop & Compression Defense in OptionSignalEngine Gate 5B.
  2. Chop & Theta Defense in OrderIntentEngine automated entries.
  3. Upstox Real Loss Accounting Reconciliation (Gross MTM, Statutory Charges, Net Realized).
"""

import unittest
import sqlite3
import tempfile
import os
import pandas as pd
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.analytics.option_sniffer.option_constants import (
    DIRECTION_BUY_CE,
    DIRECTION_BUY_PE,
    DIRECTION_NO_TRADE,
    STATE_WATCH,
    STATE_QUALIFIED,
)
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
from app.analytics.option_sniffer.option_live_order_intent import OrderIntentEngine, init_live_order_tables
from app.analytics.option_sniffer.option_live_adapter import OptionLiveAdapter
from app.analytics.option_sniffer.option_db import init_option_sniffer_tables
from app.analytics.option_sniffer.option_regime_gate import seed_mock_continuous_bars


class TestOptionChopAndAccountingReconciliation(unittest.TestCase):

    def setUp(self):
        OptionSignalEngine.reset_class_state()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self.p1 = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.p2 = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.p1.start()
        self.p2.start()
        init_option_sniffer_tables()
        from app.analytics.option_sniffer.option_regime_gate import seed_mock_continuous_bars
        seed_mock_continuous_bars(self.db_path, ticker="NIFTY", num_bars=30)

        self.mock_model = MagicMock()
        self.mock_model.version = "1.0.0-TEST"
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.65,
            "expected_net_return_pct": 8.0,
        }
        self.engine = OptionSignalEngine(model=self.mock_model)

        self.quote = {
            "bid": 100.0,
            "ask": 101.0,
            "ltp": 100.5,
            "spread_pct": 0.99,
            "quote_age_ms": 200,
            "oi": 500000,
            "volume": 200000,
            "delta": 0.50,
            "iv": 14.5,
            "quote_freshness": "REAL_TIME",
        }

        self.contract = {
            "instrument_key": "NSE_FO|99001",
            "trading_symbol": "NIFTY 22500 CE",
            "underlying": "NIFTY",
            "strike": 22500,
            "option_type": "CE",
            "expiry": "2026-10-15",
            "lot_size": 65,
        }

    def tearDown(self):
        OptionSignalEngine.reset_class_state()
        self.p1.stop()
        self.p2.stop()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def test_chop_defense_blocks_option_entries_during_10m_compression(self):
        """
        Underlying NIFTY has been trading in a tight 10-point range (22,500 to 22,510)
        over the last 10 minutes (range = 0.044% < 0.08%, 10 pts < 18 pts).
        Gate 5B must block option buying to protect capital from theta decay.
        """
        u_quote = {
            "spot_ltp": 22505.0,
            "open": 22500.0,
            "high": 22510.0,
            "low": 22500.0,
            "close": 22505.0,
            "quote_age_ms": 150,
        }
        # 10 bars of 1-minute data compressed between 22500 and 22510
        bars = []
        for i in range(10):
            bars.append({"open": 22502.0, "high": 22510.0, "low": 22500.0, "close": 22505.0, "volume": 1000})
        u_df = pd.DataFrame(bars)

        res = self.engine.evaluate_contract(
            contract=self.contract,
            quote=self.quote,
            underlying_quote=u_quote,
            underlying_df=u_df,
            enforce_cutoff=False,
        )

        self.assertFalse(res["hurdles_passed"])
        self.assertEqual(res["status"], STATE_WATCH)
        rejections = " ".join(res["rejection_reasons"])
        self.assertIn("compressed 10-minute sideways consolidation", rejections)
        self.assertIn("Option buying halted to prevent theta decay", rejections)

    def test_chop_defense_permits_trending_or_volatile_market(self):
        """
        Underlying NIFTY has an active 35-point range over the last 10 minutes
        (range = 0.155% > 0.08%, 35 pts > 18 pts).
        Gate 5B should NOT block on chop defense.
        """
        u_quote = {
            "spot_ltp": 22535.0,
            "open": 22480.0,
            "high": 22540.0,
            "low": 22475.0,
            "close": 22530.0,
            "quote_age_ms": 150,
        }
        bars = []
        for i in range(10):
            bars.append({
                "open": 22500.0 + i * 3.5,
                "high": 22505.0 + i * 3.5,
                "low": 22495.0 + i * 3.5,
                "close": 22502.0 + i * 3.5,
                "volume": 5000
            })
        u_df = pd.DataFrame(bars)

        res = self.engine.evaluate_contract(
            contract=self.contract,
            quote=self.quote,
            underlying_quote=u_quote,
            underlying_df=u_df,
            enforce_cutoff=False,
        )

        # Should pass chop defense without rejection
        rejections = " ".join(res.get("rejection_reasons", []))
        self.assertNotIn("compressed 10-minute sideways consolidation", rejections)
        self.assertTrue(res["hurdles_passed"])

    def test_chop_defense_gracefully_skips_when_history_short(self):
        """
        When underlying history has fewer than 10 bars (cold start),
        chop defense does not starve trading.
        """
        u_quote = {
            "spot_ltp": 22530.0,
            "open": 22490.0,
            "high": 22535.0,
            "low": 22485.0,
            "close": 22525.0,
            "quote_age_ms": 150,
        }
        bars = [{"open": 22500.0, "high": 22505.0, "low": 22500.0, "close": 22502.0, "volume": 1000}] * 5
        u_df = pd.DataFrame(bars)

        res = self.engine.evaluate_contract(
            contract=self.contract,
            quote=self.quote,
            underlying_quote=u_quote,
            underlying_df=u_df,
            enforce_cutoff=False,
        )

        rejections = " ".join(res.get("rejection_reasons", []))
        self.assertNotIn("compressed 10-minute sideways consolidation", rejections)
        self.assertTrue(res["hurdles_passed"])

    def test_pnl_summary_reconciles_gross_and_statutory_charges(self):
        """
        Verifies get_live_pnl_summary properly computes:
          - realized_gross_pnl (broker gross MTM)
          - statutory_charges
          - realized_net_pnl (gross - charges)
          - total_net_pnl (matches Upstox screen headline)
        """
        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp_db.close()
        db_path = temp_db.name

        try:
            with patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=db_path), \
                 patch("app.analytics.option_sniffer.option_live_adapter.get_db_path", return_value=db_path):

                init_live_order_tables()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # Insert 2 completed trades
                # Trade 1: NIFTY CE buy 200, sell 180, qty 65 -> gross = -1300.0, charges = 80.0, net = -1380.0
                cur.execute("""
                    INSERT INTO option_live_order_intents (
                        order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action,
                        lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state,
                        broker_order_id, exit_state, exit_fill_price, gross_pnl, statutory_charges, realized_pnl,
                        created_at, updated_at
                    ) VALUES (
                        'INTENT_01', 'SIG_01', 'NSE_FO|111', 'NIFTY 22500 CE', 'NIFTY', 'BUY_CE',
                        65, 65, 65, 200.0, 200.0, 'FILLED',
                        'B_01', 'CONFIRMED_FLAT', 180.0, -1300.0, 80.0, -1380.0,
                        ?, ?
                    );
                """, (now_str, now_str))

                # Trade 2: NIFTY CE buy 100, sell 110, qty 65 -> gross = +650.0, charges = 50.0, net = +600.0
                cur.execute("""
                    INSERT INTO option_live_order_intents (
                        order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action,
                        lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state,
                        broker_order_id, exit_state, exit_fill_price, gross_pnl, statutory_charges, realized_pnl,
                        created_at, updated_at
                    ) VALUES (
                        'INTENT_02', 'SIG_02', 'NSE_FO|222', 'NIFTY 22600 CE', 'NIFTY', 'BUY_CE',
                        65, 65, 65, 100.0, 100.0, 'FILLED',
                        'B_02', 'CONFIRMED_FLAT', 110.0, 650.0, 50.0, 600.0,
                        ?, ?
                    );
                """, (now_str, now_str))
                conn.commit()
                conn.close()

                adapter = OptionLiveAdapter()
                adapter._last_tradebook_sync = datetime.now().timestamp() + 999  # Skip external Upstox HTTP call

                summary = adapter.get_live_pnl_summary()
                # Gross = -1300 + 650 = -650.0
                self.assertEqual(summary["realized_gross_pnl"], -650.0)
                # Charges = 80 + 50 = 130.0
                self.assertEqual(summary["statutory_charges"], 130.0)
                # Realized Net = -1380 + 600 = -780.0
                self.assertEqual(summary["realized_net_pnl"], -780.0)
                # Headline = Gross (-650.0) + MTM (0.0) = -650.0 (Matches broker screen)
                self.assertEqual(summary["total_net_pnl"], -650.0)
                self.assertEqual(summary["closed_trades_count"], 2)
                self.assertEqual(summary["win_count"], 1)
                self.assertEqual(summary["loss_count"], 1)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_direction_sl_cooldown_blocks_same_direction_but_allows_reversal(self):
        """
        Verifies that when a CE contract hits Stop Loss, ALL CE strikes for that underlying
        are placed on a 10-minute cooldown (preventing 2-second slot-refill machine-gunning),
        while PE reversal signals are immediately allowed through.
        """
        OptionSignalEngine.record_direction_sl_cooldown("NIFTY", "CE")

        u_quote = {
            "spot_ltp": 22535.0,
            "open": 22480.0,
            "high": 22540.0,
            "low": 22475.0,
            "close": 22530.0,
            "quote_age_ms": 150,
        }
        # Neighbouring CE strike (different instrument_key) should be blocked by Direction SL Cooldown
        neighbor_ce = dict(self.contract)
        neighbor_ce["instrument_key"] = "NSE_FO|99002"
        neighbor_ce["trading_symbol"] = "NIFTY 22550 CE"
        neighbor_ce["strike"] = 22550

        res_ce = self.engine.evaluate_contract(
            contract=neighbor_ce,
            quote=self.quote,
            underlying_quote=u_quote,
            enforce_cutoff=False,
        )
        self.assertFalse(res_ce["hurdles_passed"])
        self.assertEqual(res_ce["status"], STATE_QUALIFIED)
        self.assertIn("Direction SL cooldown active for NIFTY CE", " ".join(res_ce["rejection_reasons"]))

        # Opposite direction (PE) should NOT be blocked by NIFTY_CE SL cooldown
        pe_contract = dict(self.contract)
        pe_contract["instrument_key"] = "NSE_FO|99003"
        pe_contract["trading_symbol"] = "NIFTY 22500 PE"
        pe_contract["option_type"] = "PE"
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_PE,
            "calibrated_prob": 0.65,
            "expected_net_return_pct": 8.0,
        }
        u_quote_bear = {
            "spot_ltp": 22410.0,
            "open": 22500.0,
            "high": 22510.0,
            "low": 22405.0,
            "close": 22480.0,
            "quote_age_ms": 150,
        }
        seed_mock_continuous_bars(self.db_path, ticker="NIFTY", num_bars=30, trending="BEAR")
        res_pe = self.engine.evaluate_contract(
            contract=pe_contract,
            quote=dict(self.quote, delta=-0.50),
            underlying_quote=u_quote_bear,
            enforce_cutoff=False,
        )
        self.assertTrue(res_pe["hurdles_passed"])
        self.assertEqual(res_pe["status"], "ACTIVE")

    def test_v_bottom_bull_recovery_unlocks_ce_below_morning_high(self):
        """
        Verifies that if NIFTY dropped in the morning (High 24,050 -> Low 23,800)
        and then staged a genuine V-bottom rally to 23,900 (+0.42% off session low,
        still -0.62% below morning high) with positive 3-minute momentum,
        Gate 5B unlocks CE entries instead of starving the recovery.
        """
        u_quote = {
            "spot_ltp": 23900.0,
            "open": 24000.0,
            "high": 24050.0,
            "low": 23800.0,
            "close": 23950.0,
            "quote_age_ms": 150,
        }
        # Rising 1m bars over 10 minutes (range > 35 pts, 3m return > +0.08%)
        bars = []
        for i in range(10):
            px = 23850.0 + i * 5.5
            bars.append({
                "open": px - 2.0,
                "high": px + 3.0,
                "low": px - 4.0,
                "close": px,
                "volume": 5000
            })
        u_df = pd.DataFrame(bars)

        res = self.engine.evaluate_contract(
            contract=self.contract,
            quote=self.quote,
            underlying_quote=u_quote,
            underlying_df=u_df,
            enforce_cutoff=False,
        )
        rejections = " ".join(res.get("rejection_reasons", []))
        self.assertNotIn("Intraday Bear Reversal active", rejections)
        self.assertTrue(res["hurdles_passed"])
        self.assertEqual(res["status"], "ACTIVE")

    def test_blended_rsi_resists_micro_3m_pause_whiplash(self):
        """
        Verifies that in an intraday bear day (Open 24,000, High 24,020, Low 23,840, Spot 23,855),
        a tiny 4-bar pause of +1.5 pt per minute does NOT spike spot_rsi to 75+ (which previously
        killed PE conviction), because structural_rsi anchors it to the intraday structure.
        """
        from app.analytics.option_sniffer.option_feature_extractor import OptionFeatureExtractor
        u_quote = {
            "spot_ltp": 23855.0,
            "open": 24000.0,
            "high": 24020.0,
            "low": 23840.0,
            "close": 23980.0,
            "quote_age_ms": 150,
        }
        # 6 bars where the last 4 ticked up by +1.5 pts each after a drop
        closes = [23870.0, 23849.0, 23850.5, 23852.0, 23853.5, 23855.0]
        u_df = pd.DataFrame([
            {"open": c - 1, "high": c + 1, "low": c - 2, "close": c, "volume": 1000}
            for c in closes
        ])
        pe_contract = dict(self.contract, option_type="PE")
        feats = OptionFeatureExtractor.extract_features(
            contract=pe_contract,
            quote=dict(self.quote, delta=-0.50),
            underlying_quote=u_quote,
            underlying_df=u_df,
        )
        # Blended RSI remains well below 60 (structural bear regime) rather than spiking to 75+
        self.assertLess(feats["spot_rsi"], 58.0)


if __name__ == "__main__":
    unittest.main()

