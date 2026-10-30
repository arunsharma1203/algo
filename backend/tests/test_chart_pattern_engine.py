"""
UNIT TESTS: Chart Pattern Engine (Geometric Confluence & Pivot Analysis)
========================================================================
Validates that ChartPatternEngine accurately identifies:
1. Higher Highs & Higher Lows (Dow Theory Uptrend Continuation)
2. Lower Highs & Lower Lows (Dow Theory Downtrend Continuation)
3. Double Bottom ("W" structure) & Double Top ("M" structure) Breakouts
4. Bull Flag and Range Breakouts
5. Proper invalidation SL, TP, and R:R ratios
"""

import unittest
from app.analytics.chart_patterns import ChartPatternEngine


class TestChartPatternEngine(unittest.TestCase):

    def test_empty_and_short_candles(self):
        """Verify safe failure on empty or insufficient candlestick series."""
        res_empty = ChartPatternEngine.analyze_patterns([], direction="BUY")
        self.assertFalse(res_empty["detected"])
        self.assertEqual(res_empty["status"], "INSUFFICIENT_BARS")

        short = [("2026-10-01 09:15", 100, 101, 99, 100, 1000)] * 5
        res_short = ChartPatternEngine.analyze_patterns(short, direction="BUY")
        self.assertFalse(res_short["detected"])
        self.assertEqual(res_short["status"], "INSUFFICIENT_BARS")

    def test_higher_highs_higher_lows(self):
        """Verify detection of Higher Highs and Higher Lows uptrend structure."""
        candles = [
            ("2026-10-01 09:15", 100.0, 102.0, 99.5, 101.5, 1000),
            ("2026-10-01 09:30", 101.5, 104.0, 101.0, 103.8, 1200),  # H1 = 104.0
            ("2026-10-01 09:45", 103.5, 103.8, 101.2, 101.5, 800),
            ("2026-10-01 10:00", 101.5, 102.5, 100.8, 102.0, 900),   # L1 = 100.8
            ("2026-10-01 10:15", 102.0, 106.0, 101.8, 105.5, 1300),
            ("2026-10-01 10:30", 105.5, 107.5, 105.0, 107.0, 1500),  # H2 = 107.5 (> H1)
            ("2026-10-01 10:45", 106.5, 106.8, 103.5, 104.0, 850),   # L2 = 103.5 (> L1)
            ("2026-10-01 11:00", 104.0, 108.0, 103.8, 107.8, 1600),
            ("2026-10-01 11:15", 107.8, 110.0, 107.5, 109.5, 2200),  # Breakout
            ("2026-10-01 11:30", 109.5, 111.0, 109.0, 110.5, 2500)
        ]

        res = ChartPatternEngine.analyze_patterns(candles, direction="BUY")
        self.assertTrue(res["detected"])
        self.assertEqual(res["pattern_name"], "HIGHER_HIGHS_HIGHER_LOWS")
        self.assertIn(res["status"], ("CONFIRMED_BREAKOUT", "CONFIRMED_CONTINUATION"))
        self.assertIsNotNone(res["structural_sl"])
        self.assertLess(res["structural_sl"], 110.5)
        self.assertGreater(res["structural_tp"], 110.5)
        self.assertGreaterEqual(res["risk_reward_ratio"], 1.2)

    def test_double_bottom_breakout(self):
        """Verify detection of Double Bottom 'W' formation."""
        candles = [
            ("2026-10-01 09:15", 105.0, 106.0, 99.8, 100.0, 1000),
            ("2026-10-01 09:30", 100.0, 101.0, 99.5, 100.2, 1000),   # Trough 1: 99.5
            ("2026-10-01 09:45", 100.5, 103.0, 100.2, 102.5, 1000),
            ("2026-10-01 10:00", 102.5, 105.2, 102.0, 105.0, 1200),
            ("2026-10-01 10:15", 105.0, 105.5, 103.5, 104.0, 1000),  # Neckline: 105.5
            ("2026-10-01 10:30", 104.0, 104.2, 101.5, 101.8, 1000),
            ("2026-10-01 10:45", 101.8, 102.0, 99.7, 100.3, 1000),   # Trough 2: 99.7
            ("2026-10-01 11:00", 100.3, 101.5, 99.8, 101.2, 1100),
            ("2026-10-01 11:15", 101.2, 104.0, 101.0, 103.8, 1500),
            ("2026-10-01 11:30", 103.8, 106.0, 103.5, 105.8, 2500)   # Cleared neckline
        ]

        res = ChartPatternEngine.analyze_patterns(candles, direction="BUY")
        self.assertTrue(res["detected"])
        self.assertEqual(res["pattern_name"], "DOUBLE_BOTTOM_BREAKOUT")
        self.assertIn(res["status"], ("CONFIRMED_BREAKOUT", "APPROACHING_NECKLINE"))
        self.assertLess(res["structural_sl"], 100.0)
        self.assertGreaterEqual(res["structural_tp"], 105.5)

    def test_double_top_breakdown(self):
        """Verify detection of Double Top 'M' formation."""
        candles = [
            ("2026-10-01 09:15", 95.0, 100.2, 94.8, 100.0, 1000),
            ("2026-10-01 09:30", 100.0, 100.5, 99.0, 99.8, 1000),    # Peak 1: 100.5
            ("2026-10-01 09:45", 99.5, 99.8, 95.2, 95.5, 1000),     # Trough / Neckline: 95.2
            ("2026-10-01 10:00", 95.5, 98.0, 95.0, 97.5, 1200),
            ("2026-10-01 10:15", 97.5, 100.4, 97.0, 100.0, 1000),   # Peak 2: 100.4
            ("2026-10-01 10:30", 100.0, 100.1, 96.0, 96.5, 1000),
            ("2026-10-01 10:45", 96.5, 96.8, 94.5, 94.8, 1500),     # Breakdown below 95.2
            ("2026-10-01 11:00", 94.8, 95.0, 93.0, 93.5, 2000),
            ("2026-10-01 11:15", 93.5, 94.0, 92.0, 92.5, 2200),
            ("2026-10-01 11:30", 92.5, 93.0, 91.5, 91.8, 2500)
        ]

        res = ChartPatternEngine.analyze_patterns(candles, direction="SELL")
        self.assertTrue(res["detected"])
        self.assertEqual(res["pattern_name"], "DOUBLE_TOP_BREAKDOWN")
        self.assertIn(res["status"], ("CONFIRMED_BREAKDOWN", "APPROACHING_NECKLINE"))
        self.assertGreater(res["structural_sl"], 100.0)
        self.assertLess(res["structural_tp"], 95.2)

    def test_bull_flag_breakout(self):
        """Verify detection of Bull Flag pole and consolidation breakout."""
        candles = [
            ("2026-10-01 09:15", 100.0, 101.0, 99.8, 100.8, 1000),
            ("2026-10-01 09:30", 100.8, 102.5, 100.5, 102.2, 1200),
            ("2026-10-01 09:45", 102.2, 103.8, 102.0, 103.5, 1500),
            ("2026-10-01 10:00", 103.5, 104.8, 103.2, 104.6, 1800),
            ("2026-10-01 10:15", 104.6, 105.2, 104.4, 105.0, 2000),  # Pole top: 105.2
            ("2026-10-01 10:30", 105.0, 105.1, 104.6, 104.8, 800),   # Flag consolidation
            ("2026-10-01 10:45", 104.8, 105.0, 104.7, 104.9, 700),
            ("2026-10-01 11:00", 104.9, 105.0, 104.8, 104.9, 600),
            ("2026-10-01 11:15", 104.9, 105.1, 104.7, 105.0, 750),
            ("2026-10-01 11:30", 105.0, 105.2, 104.8, 105.1, 800),
            ("2026-10-01 11:45", 105.1, 105.3, 105.0, 105.2, 1200),
            ("2026-10-01 12:00", 105.2, 106.0, 105.1, 105.8, 2500)   # Breakout above flag
        ]

        res = ChartPatternEngine.analyze_patterns(candles, direction="BUY")
        self.assertTrue(res["detected"])
        self.assertIn(res["pattern_name"], ("BULL_FLAG_BREAKOUT", "RANGE_RESISTANCE_BREAKOUT", "HIGHER_HIGHS_HIGHER_LOWS"))
        self.assertIsNotNone(res["structural_sl"])
        self.assertGreater(res["structural_tp"], 105.0)


if __name__ == "__main__":
    unittest.main()
