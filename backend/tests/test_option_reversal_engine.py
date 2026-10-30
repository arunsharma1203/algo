"""
Unit test suite verifying the Intraday Momentum & Top-Reversal Engine:
  1. Detection of top-reversals from session highs (blocks CE, unlocks PE).
  2. Detection of gap-up fades (trading below open, blocks CE, unlocks PE).
  3. Dynamic RSI and momentum extraction from underlying price action.
  4. Option Alpha Model conviction scoring on reversal setups.
"""

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.analytics.option_sniffer.option_constants import (
    DIRECTION_BUY_CE,
    DIRECTION_BUY_PE,
    DIRECTION_NO_TRADE,
    STATE_WATCH,
    STATE_QUALIFIED,
)
import tempfile
import os
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
from app.analytics.option_sniffer.option_feature_extractor import OptionFeatureExtractor
from app.analytics.option_sniffer.option_alpha_model import OptionAlphaModel
from app.analytics.option_sniffer.option_db import init_option_sniffer_tables


class TestOptionReversalEngine(unittest.TestCase):

    def setUp(self):
        OptionSignalEngine._COOLDOWNS.clear()
        OptionSignalEngine._DIRECTION_SL_COOLDOWNS.clear()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self.p1 = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.p2 = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.p1.start()
        self.p2.start()
        init_option_sniffer_tables()

        self.mock_model = MagicMock()
        self.mock_model.version = "1.0.0-TEST"
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_PE,
            "calibrated_prob": 0.62,
            "expected_net_return_pct": 7.5,
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
            "delta": -0.50,
            "iv": 14.5,
            "quote_freshness": "REAL_TIME",
        }

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def test_top_reversal_from_high_blocks_ce_and_unlocks_pe(self):
        """
        NIFTY opened at 22,500, rallied to 22,650, then dropped to 22,570.
        Spot is still above open (22,570 > 22,500), but down -0.35% from peak.
        Gate 5B must block CALLs (CE) and permit PUTs (PE).
        """
        u_quote_reversal = {
            "spot_ltp": 22570.0,
            "open": 22500.0,
            "high": 22650.0,
            "low": 22490.0,
            "close": 22480.0,
            "quote_age_ms": 150,
        }

        # 1. Test CALL option (CE) -> MUST BE BLOCKED
        ce_contract = {
            "instrument_key": "NSE_FO|99001",
            "trading_symbol": "NIFTY 22600 CE",
            "underlying": "NIFTY",
            "strike": 22600,
            "option_type": "CE",
            "expiry": "2026-10-19",
            "lot_size": 65,
        }
        ce_res = self.engine.evaluate_contract(
            contract=ce_contract,
            quote=self.quote,
            underlying_quote=u_quote_reversal,
            macro_context={"intraday_regime_verdict": "INTRADAY_BULL_MOMENTUM"},
            as_of_time=datetime(2026, 10, 13, 10, 30, 0),
        )
        self.assertEqual(ce_res["status"], STATE_WATCH)
        self.assertTrue(any("Intraday bear reversal" in r for r in ce_res["rejection_reasons"]))

        # 2. Test PUT option (PE) -> MUST BE PERMITTED
        pe_contract = {
            "instrument_key": "NSE_FO|99002",
            "trading_symbol": "NIFTY 22550 PE",
            "underlying": "NIFTY",
            "strike": 22550,
            "option_type": "PE",
            "expiry": "2026-10-19",
            "lot_size": 65,
        }
        pe_res = self.engine.evaluate_contract(
            contract=pe_contract,
            quote=self.quote,
            underlying_quote=u_quote_reversal,
            macro_context={"intraday_regime_verdict": "INTRADAY_BULL_MOMENTUM"},
            as_of_time=datetime(2026, 10, 13, 10, 30, 0),
        )
        self.assertFalse(any("Intraday bear reversal" in r for r in pe_res.get("rejection_reasons", [])))

    def test_gap_up_fade_below_open_unlocks_pe(self):
        """
        NIFTY gaps up to 22,600, but sells off below open to 22,540 (drift vs open = -0.26%).
        Prev close was 22,480 so change vs close is still positive (+0.27%).
        Must NOT be blocked as CHOP; PE must be permitted.
        """
        u_quote_gap_fade = {
            "spot_ltp": 22540.0,
            "open": 22600.0,
            "high": 22620.0,
            "low": 22530.0,
            "close": 22480.0,
            "quote_age_ms": 100,
        }
        pe_contract = {
            "instrument_key": "NSE_FO|99002",
            "trading_symbol": "NIFTY 22550 PE",
            "underlying": "NIFTY",
            "strike": 22550,
            "option_type": "PE",
            "expiry": "2026-10-19",
            "lot_size": 65,
        }
        res = self.engine.evaluate_contract(
            contract=pe_contract,
            quote=self.quote,
            underlying_quote=u_quote_gap_fade,
            macro_context={"intraday_regime_verdict": "INTRADAY_NEUTRAL_CHOP"},
            as_of_time=datetime(2026, 10, 13, 10, 30, 0),
        )
        self.assertFalse(any("CHOP" in r for r in res.get("rejection_reasons", [])))

    def test_feature_extractor_dynamic_rsi_on_reversal(self):
        """Verifies synthesized RSI compresses below 48 when spot drops from high."""
        u_quote = {
            "spot_ltp": 22560.0,
            "open": 22500.0,
            "high": 22660.0,
            "low": 22490.0,
            "close": 22480.0,
            "quote_age_ms": 100,
        }
        features = OptionFeatureExtractor.extract_features(
            contract={"strike": 22550, "option_type": "PE"},
            quote=self.quote,
            underlying_quote=u_quote,
        )
        self.assertLess(features["spot_rsi"], 48.0)
        self.assertLess(features["spot_ret_1"], 0.0)
        self.assertLess(features["spot_ret_3"], 0.0)

    def test_alpha_model_penalizes_call_and_boosts_put_on_reversal(self):
        """Verifies OptionAlphaModel drops Call probability, rejects unconfirmed micro-whipsaws, and qualifies confirmed reversals."""
        alpha_model = OptionAlphaModel()

        features_call = {
            "is_call": 1.0,
            "spot_rsi": 44.0,
            "spot_vwap_dist_pct": -0.1,
            "pullback_from_high_pct": -0.35,
            "rally_from_low_pct": 0.1,
            "spot_ret_3": -0.25,
            "upstream_aligned": 0.5,
            "spread_pct": 0.8,
            "abs_moneyness": 0.005,
        }
        pred_call = alpha_model.predict_contract(features_call)
        self.assertEqual(pred_call["direction"], DIRECTION_NO_TRADE)
        self.assertLess(pred_call["calibrated_prob"], 0.45)

        # Unconfirmed micro-pullback without upstream consensus -> strictly rejected as NO_TRADE (whipsaw filter)
        features_put_unconfirmed = {
            "is_call": 0.0,
            "spot_rsi": 44.0,
            "spot_vwap_dist_pct": -0.1,
            "pullback_from_high_pct": -0.35,
            "rally_from_low_pct": 0.1,
            "spot_ret_3": -0.25,
            "upstream_aligned": 0.5,
            "spread_pct": 0.8,
            "abs_moneyness": 0.005,
        }
        pred_put_unconfirmed = alpha_model.predict_contract(features_put_unconfirmed)
        self.assertEqual(pred_put_unconfirmed["direction"], DIRECTION_NO_TRADE)

        # Confirmed reversal supported by upstream consensus -> qualifies as BUY_PE
        features_put_confirmed = dict(features_put_unconfirmed, upstream_aligned=0.85)
        pred_put_confirmed = alpha_model.predict_contract(features_put_confirmed)
        self.assertEqual(pred_put_confirmed["direction"], DIRECTION_BUY_PE)
        self.assertGreaterEqual(pred_put_confirmed["calibrated_prob"], 0.55)


if __name__ == "__main__":
    unittest.main()
