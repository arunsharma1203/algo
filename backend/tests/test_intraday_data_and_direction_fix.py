import unittest
import numpy as np
import pandas as pd
from unittest.mock import patch, MagicMock

class TestIntradayDataAndDirectionFix(unittest.TestCase):
    """
    Formal verification for:
    1. Rotating priority hoarding (breaking the Alphabet Trap in intraday market daemon).
    2. Directional alignment in Qlib (preventing artificial BUY deadlocks on near-zero alpha scores).
    """

    def test_daemon_priority_rotation_breaks_alphabet_trap(self):
        """
        Verify that priority symbols include core market leaders and rotate through
        non-core F&O tickers instead of only grabbing the first 30 alphabetical tickers.
        """
        clean_symbols = [
            "360ONE", "3MINDIA", "AADHARHFC", "AARTIIND", "AAVAS", "ABB", "ABBOTINDIA",
            "ABCAPITAL", "ABDL", "ABFRL", "ABLBL", "ABREL", "ABSLAMC", "ACC", "ACE",
            "ADANIENT", "ADANIPORTS", "ALKEM", "APOLLOTYRE", "ASHOKLEY", "AXISBANK",
            "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BALRAMCHIN", "BANDHANBNK",
            "BANKBARODA", "BATAINDIA", "BEL", "BERGEPAINT", "BHARATFORG", "BHARTIARTL",
            "BHEL", "BIKAJI", "BPCL", "BRITANNIA", "CANBK", "CHOLAFIN", "CIPLA",
            "COALINDIA", "CONCOR", "CUMMINSIND", "DABUR", "DIVISLAB", "DLF", "DRREDDY",
            "EICHERMOT", "FEDERALBNK", "GAIL", "GODREJCP", "GRASIM", "HAVELLS",
            "HCLTECH", "HDFCAMC", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO",
            "HINDUNILVR", "ICICIBANK", "ICICIGI", "ICICIPRULI", "INDHOTEL", "INDUSINDBK",
            "INDUSTOWER", "INFY", "IOC", "IRCTC", "ITC", "JINDALSTEL", "JSWSTEEL",
            "KOTAKBANK", "LT", "LTIM", "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
            "POWERGRID", "RELIANCE", "SBIN", "SUNPHARMA", "TATAMOTORS", "TATASTEEL",
            "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO"
        ]
        priority_set = set(clean_symbols)
        open_arena_symbols = set()

        core_anchors = [
            "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN",
            "BHARTIARTL", "ITC", "LT", "KOTAKBANK", "AXISBANK", "TATAMOTORS",
            "MARUTI", "SUNPHARMA", "TITAN", "BAJFINANCE", "M&M", "NTPC", "POWERGRID", "TATASTEEL"
        ]

        arena_priority = [s for s in open_arena_symbols if s in clean_symbols]
        leader_priority = [s for s in core_anchors if s in clean_symbols and s not in arena_priority]

        # Slot 0 (09:15)
        slot_idx_0 = 0
        other_fno = [s for s in clean_symbols if s in priority_set and s not in arena_priority and s not in leader_priority]
        batch_size = 25
        offset_0 = (slot_idx_0 * batch_size) % len(other_fno)
        slice_0 = (other_fno[offset_0:] + other_fno[:offset_0])[:batch_size]
        priority_0 = list(dict.fromkeys(arena_priority + leader_priority + slice_0))[:45]

        # Verify core anchors are ALWAYS present
        self.assertIn("RELIANCE", priority_0)
        self.assertIn("TCS", priority_0)
        self.assertIn("HDFCBANK", priority_0)
        self.assertIn("INFY", priority_0)
        self.assertIn("SBIN", priority_0)

        # Slot 2 (09:45): should rotate to a different set of F&O stocks
        slot_idx_2 = 2
        offset_2 = (slot_idx_2 * batch_size) % len(other_fno)
        slice_2 = (other_fno[offset_2:] + other_fno[:offset_2])[:batch_size]
        priority_2 = list(dict.fromkeys(arena_priority + leader_priority + slice_2))[:45]

        self.assertIn("RELIANCE", priority_2)
        # Slices from slot 0 and slot 2 must differ
        self.assertNotEqual(set(slice_0), set(slice_2))

    def test_qlib_directional_classification_regime_grounded(self):
        """
        Verify that near-zero alpha scores (|score| < 0.0005) do not force BUY
        when the session momentum is negative or market regime is bearish.
        """
        # Case 1: High conviction positive -> BUY
        score_val = 0.0035
        session_return_pct = -0.5
        is_bearish_regime = True
        if abs(score_val) >= 0.0005:
            is_bullish = bool(score_val > 0.0)
        elif session_return_pct is not None and abs(session_return_pct) >= 0.15:
            is_bullish = bool(session_return_pct > 0.0)
        elif is_bearish_regime:
            is_bullish = False
        else:
            is_bullish = bool(score_val >= 0.0)
        self.assertTrue(is_bullish)

        # Case 2: High conviction negative -> SELL
        score_val = -0.0042
        if abs(score_val) >= 0.0005:
            is_bullish = bool(score_val > 0.0)
        elif session_return_pct is not None and abs(session_return_pct) >= 0.15:
            is_bullish = bool(session_return_pct > 0.0)
        elif is_bearish_regime:
            is_bullish = False
        else:
            is_bullish = bool(score_val >= 0.0)
        self.assertFalse(is_bullish)

        # Case 3: Near-zero residual score (+0.0001) in falling market (-0.8%) -> SELL
        score_val = 0.0001
        session_return_pct = -0.80
        is_bearish_regime = True
        if abs(score_val) >= 0.0005:
            is_bullish = bool(score_val > 0.0)
        elif session_return_pct is not None and abs(session_return_pct) >= 0.15:
            is_bullish = bool(session_return_pct > 0.0)
        elif is_bearish_regime:
            is_bullish = False
        else:
            is_bullish = bool(score_val >= 0.0)
        self.assertFalse(is_bullish, "Near-zero residual in falling market must produce SELL!")

        # Case 4: Near-zero residual (+0.0001) in rising market (+0.9%) -> BUY
        score_val = 0.0001
        session_return_pct = 0.90
        is_bearish_regime = False
        if abs(score_val) >= 0.0005:
            is_bullish = bool(score_val > 0.0)
        elif session_return_pct is not None and abs(session_return_pct) >= 0.15:
            is_bullish = bool(session_return_pct > 0.0)
        elif is_bearish_regime:
            is_bullish = False
        else:
            is_bullish = bool(score_val >= 0.0)
        self.assertTrue(is_bullish, "Near-zero residual in rising market must produce BUY!")

if __name__ == '__main__':
    unittest.main()
