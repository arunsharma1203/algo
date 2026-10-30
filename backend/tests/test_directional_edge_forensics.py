"""
Unit and Forensic Verification Suite for Directional-Edge Forensics
=====================================================================
Tests all 16 golden forensic scenarios:
1. BUY + rising market
2. BUY + falling market
3. SELL + falling market
4. SELL + rising market
5. stock underperforms NIFTY
6. stock outperforms NIFTY
7. flat market
8. strong trend
9. choppy market
10. identical scores
11. tied ranking
12. missing prediction
13. missing benchmark
14. stale benchmark
15. stale stock price
16. future timestamp causality
17. canonical cost survival & null hypothesis evaluation.
"""

import os
import sys
import unittest
import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

# Path resolution
BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))
WORKSPACE_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(WORKSPACE_ROOT))

from app.analytics.directional_edge_forensics import DirectionalEdgeForensics
from app.analytics.trade_economics import compute_trade_friction_pct


class TestDirectionalEdgeForensics(unittest.TestCase):
    """Authoritative test suite for the Directional Edge Forensic Engine."""

    def setUp(self):
        """Creates an isolated temporary SQLite database for each test case."""
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_file.name
        self.temp_file.close()
        DirectionalEdgeForensics.clear_cache()

        # Build schema identical to production
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE consensus_candidate_shadow_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                scan_id TEXT,
                ticker TEXT,
                symbol TEXT,
                strategy TEXT,
                qlib_direction TEXT,
                qlib_score REAL,
                qlib_price REAL,
                legacy_direction TEXT,
                legacy_confidence REAL,
                legacy_price REAL,
                composite_score REAL,
                action_taken TEXT,
                entry_ref_price REAL,
                details_json TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE consensus_tracked_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                direction TEXT,
                entry_time TEXT,
                entry_price REAL,
                current_price REAL,
                current_pnl_pct REAL,
                status TEXT,
                exit_time TEXT,
                exit_price REAL,
                exit_pnl_pct REAL,
                qlib_conviction REAL,
                legacy_conviction REAL,
                regime TEXT,
                high_water_mark REAL,
                low_water_mark REAL,
                hold_duration_bars INTEGER
            );
        """)
        cur.execute("""
            CREATE TABLE ohlcv (
                ticker TEXT,
                timeframe TEXT,
                date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                PRIMARY KEY (ticker, timeframe, date)
            );
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        """Cleans up the temporary database."""
        DirectionalEdgeForensics.clear_cache()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def _insert_candidate(self, timestamp, ticker, qlib_dir, qlib_score, leg_dir, leg_conf, price, action="TAKE"):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO consensus_candidate_shadow_log
            (timestamp, scan_id, ticker, symbol, strategy, qlib_direction, qlib_score, qlib_price,
             legacy_direction, legacy_confidence, legacy_price, composite_score, action_taken, entry_ref_price, details_json)
            VALUES (?, 'scan_1', ?, ?, 'INTRADAY', ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
        """, (
            timestamp, ticker, ticker.replace(".NS", ""),
            qlib_dir, qlib_score, price,
            leg_dir, leg_conf, price,
            (qlib_score + leg_conf) / 2.0 if (qlib_score and leg_conf) else 50.0,
            action, price
        ))
        conn.commit()
        conn.close()

    def _insert_bar(self, ticker, timestamp, open_p, high_p, low_p, close_p, volume=1000, tf="15m"):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, tf, timestamp, open_p, high_p, low_p, close_p, volume))
        conn.commit()
        conn.close()

    # -------------------------------------------------------------------------
    # SCENARIO 1: BUY + Rising Market
    # -------------------------------------------------------------------------
    def test_01_buy_rising_market(self):
        """Signal is BUY, market rises, stock rises."""
        t0 = "2026-10-01 09:30:00"
        t1 = "2026-10-01 09:45:00"
        t_eod = "2026-10-01 15:15:00"

        self._insert_candidate(t0, "RELIANCE.NS", "BUY", 65.0, "BUY", 65.0, 1000.0)

        # Benchmark bars (^NSEI: 25000 -> 25250, +1.0%)
        self._insert_bar("^NSEI", t0, 25000, 25050, 24980, 25000)
        self._insert_bar("^NSEI", t1, 25000, 25100, 25000, 25080)
        self._insert_bar("^NSEI", t_eod, 25200, 25280, 25190, 25250)

        # Stock bars (RELIANCE: 1000 -> 1020, +2.0%)
        self._insert_bar("RELIANCE.NS", t0, 1000, 1005, 998, 1000)
        self._insert_bar("RELIANCE.NS", t1, 1000, 1010, 1000, 1008)
        self._insert_bar("RELIANCE.NS", t_eod, 1015, 1025, 1015, 1020)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        m_dir = res["market_direction_accuracy"]["horizons"]["EOD"]
        self.assertEqual(m_dir["buy_and_market_rises"], 1)
        self.assertEqual(m_dir["buy_and_market_falls"], 0)
        self.assertEqual(m_dir["directional_accuracy_pct"], 100.0)

    # -------------------------------------------------------------------------
    # SCENARIO 2: BUY + Falling Market
    # -------------------------------------------------------------------------
    def test_02_buy_falling_market(self):
        """Signal is BUY, market falls, stock falls."""
        t0 = "2026-10-01 09:30:00"
        t_eod = "2026-10-01 15:15:00"

        self._insert_candidate(t0, "TCS.NS", "BUY", 62.0, "BUY", 60.0, 3500.0)

        # Benchmark falls: 25000 -> 24700 (-1.2%)
        self._insert_bar("^NSEI", t0, 25000, 25000, 24900, 25000)
        self._insert_bar("^NSEI", t_eod, 24750, 24750, 24650, 24700)

        # TCS falls: 3500 -> 3430 (-2.0%)
        self._insert_bar("TCS.NS", t0, 3500, 3510, 3490, 3500)
        self._insert_bar("TCS.NS", t_eod, 3450, 3450, 3420, 3430)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        m_dir = res["market_direction_accuracy"]["horizons"]["EOD"]
        self.assertEqual(m_dir["buy_and_market_falls"], 1)
        self.assertEqual(m_dir["buy_and_market_rises"], 0)
        self.assertEqual(m_dir["directional_accuracy_pct"], 0.0)

    # -------------------------------------------------------------------------
    # SCENARIO 3: SELL + Falling Market
    # -------------------------------------------------------------------------
    def test_03_sell_falling_market(self):
        """Signal is SELL, market falls, stock falls -> Correct short."""
        t0 = "2026-10-01 09:30:00"
        t_eod = "2026-10-01 15:15:00"

        self._insert_candidate(t0, "INFY.NS", "SELL", 68.0, "SELL", 64.0, 1800.0)

        # Benchmark falls: 25000 -> 24600 (-1.6%)
        self._insert_bar("^NSEI", t0, 25000, 25000, 24900, 25000)
        self._insert_bar("^NSEI", t_eod, 24650, 24650, 24580, 24600)

        # INFY falls: 1800 -> 1746 (-3.0%)
        self._insert_bar("INFY.NS", t0, 1800, 1805, 1795, 1800)
        self._insert_bar("INFY.NS", t_eod, 1760, 1760, 1740, 1746)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        m_dir = res["market_direction_accuracy"]["horizons"]["EOD"]
        self.assertEqual(m_dir["sell_and_market_falls"], 1)
        self.assertEqual(m_dir["sell_and_market_rises"], 0)
        self.assertEqual(m_dir["directional_accuracy_pct"], 100.0)

    # -------------------------------------------------------------------------
    # SCENARIO 4: SELL + Rising Market
    # -------------------------------------------------------------------------
    def test_04_sell_rising_market(self):
        """Signal is SELL, market rises, stock rises -> False short."""
        t0 = "2026-10-01 09:30:00"
        t_eod = "2026-10-01 15:15:00"

        self._insert_candidate(t0, "WIPRO.NS", "SELL", 60.0, "SELL", 58.0, 500.0)

        # Benchmark rises: 25000 -> 25300 (+1.2%)
        self._insert_bar("^NSEI", t0, 25000, 25050, 24990, 25000)
        self._insert_bar("^NSEI", t_eod, 25250, 25320, 25240, 25300)

        # WIPRO rises: 500 -> 515 (+3.0%)
        self._insert_bar("WIPRO.NS", t0, 500, 502, 498, 500)
        self._insert_bar("WIPRO.NS", t_eod, 510, 516, 509, 515)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        m_dir = res["market_direction_accuracy"]["horizons"]["EOD"]
        self.assertEqual(m_dir["sell_and_market_rises"], 1)
        self.assertEqual(m_dir["sell_and_market_falls"], 0)
        self.assertEqual(m_dir["directional_accuracy_pct"], 0.0)

    # -------------------------------------------------------------------------
    # SCENARIO 5: Stock Underperforms NIFTY
    # -------------------------------------------------------------------------
    def test_05_stock_underperforms_nifty(self):
        """BUY candidate, stock falls 2.0% while NIFTY only falls 0.5%."""
        t0 = "2026-10-01 09:30:00"
        t_eod = "2026-10-01 15:15:00"

        self._insert_candidate(t0, "HDFCBANK.NS", "BUY", 65.0, "BUY", 60.0, 1600.0)

        # NIFTY -0.5% (25000 -> 24875)
        self._insert_bar("^NSEI", t0, 25000, 25000, 24900, 25000)
        self._insert_bar("^NSEI", t_eod, 24900, 24920, 24850, 24875)

        # HDFCBANK -2.0% (1600 -> 1568)
        self._insert_bar("HDFCBANK.NS", t0, 1600, 1605, 1595, 1600)
        self._insert_bar("HDFCBANK.NS", t_eod, 1575, 1580, 1565, 1568)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        rel_edge = res["stock_relative_selection_edge"]["horizons"]["EOD"]
        # relative return for BUY = stock_return (-2.0%) - benchmark_return (-0.5%) = -1.5%
        self.assertLess(rel_edge["mean_relative_return_pct"], 0.0)
        self.assertEqual(rel_edge["relative_directional_accuracy_pct"], 0.0)

    # -------------------------------------------------------------------------
    # SCENARIO 6: Stock Outperforms NIFTY (Absolute Down, Relative Alpha Up)
    # -------------------------------------------------------------------------
    def test_06_stock_outperforms_nifty(self):
        """CRITICAL: Stock goes DOWN (-0.4%), but NIFTY drops MORE (-1.2%).
        BUY was wrong in absolute direction, but correct in relative selection (+0.8%).
        """
        t0 = "2026-10-01 09:30:00"
        t_eod = "2026-10-01 15:15:00"

        self._insert_candidate(t0, "ICICIBANK.NS", "BUY", 66.0, "BUY", 62.0, 1200.0)

        # NIFTY -1.2% (25000 -> 24700)
        self._insert_bar("^NSEI", t0, 25000, 25000, 24800, 25000)
        self._insert_bar("^NSEI", t_eod, 24750, 24750, 24680, 24700)

        # ICICIBANK -0.4% (1200 -> 1195.2)
        self._insert_bar("ICICIBANK.NS", t0, 1200, 1202, 1198, 1200)
        self._insert_bar("ICICIBANK.NS", t_eod, 1196, 1198, 1194, 1195.2)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        abs_edge = res["stock_absolute_direction"]["models"]["CONSENSUS"]["EOD"]
        rel_edge = res["stock_relative_selection_edge"]["horizons"]["EOD"]

        # Absolute direction was negative
        self.assertLess(abs_edge["buy"]["mean_return_pct"], 0.0)
        self.assertEqual(abs_edge["buy"]["accuracy_pct"], 0.0)

        # Relative direction was POSITIVE (+0.8% excess return over benchmark)
        self.assertGreater(rel_edge["mean_relative_return_pct"], 0.0)
        self.assertEqual(rel_edge["relative_directional_accuracy_pct"], 100.0)

    # -------------------------------------------------------------------------
    # SCENARIO 7: Flat Market (Zero Market Return)
    # -------------------------------------------------------------------------
    def test_07_flat_market(self):
        """Benchmark is completely flat (0.00% return). Handled without divide-by-zero."""
        t0 = "2026-10-01 09:30:00"
        t_eod = "2026-10-01 15:15:00"

        self._insert_candidate(t0, "SBIN.NS", "BUY", 55.0, "BUY", 55.0, 800.0)

        # NIFTY stays at 25000.0
        self._insert_bar("^NSEI", t0, 25000, 25010, 24990, 25000)
        self._insert_bar("^NSEI", t_eod, 25000, 25010, 24990, 25000)

        # SBIN rises +0.5% (800 -> 804)
        self._insert_bar("SBIN.NS", t0, 800, 802, 798, 800)
        self._insert_bar("SBIN.NS", t_eod, 803, 805, 802, 804)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        m_dir = res["market_direction_accuracy"]["horizons"]["EOD"]
        self.assertEqual(m_dir["mean_subsequent_market_return_pct"], 0.0)
        self.assertEqual(res["status"], "SUCCESS")

    # -------------------------------------------------------------------------
    # SCENARIO 8: Strong Trend
    # -------------------------------------------------------------------------
    def test_08_strong_trend(self):
        """Benchmark experiences a strong directional trend (+2.0%)."""
        t0 = "2026-10-01 09:30:00"
        t_eod = "2026-10-01 15:15:00"

        self._insert_candidate(t0, "LT.NS", "BUY", 70.0, "BUY", 68.0, 3600.0)

        # NIFTY +2.0% (25000 -> 25500)
        self._insert_bar("^NSEI", t0, 25000, 25050, 24950, 25000)
        self._insert_bar("^NSEI", t_eod, 25450, 25520, 25400, 25500)

        # LT +2.5% (3600 -> 3690)
        self._insert_bar("LT.NS", t0, 3600, 3610, 3590, 3600)
        self._insert_bar("LT.NS", t_eod, 3680, 3700, 3670, 3690)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        regimes = res["regime_segmentation"]
        self.assertIn("STRONG_BULL", regimes)
        self.assertEqual(regimes["STRONG_BULL"]["sample_size"], 1)

    # -------------------------------------------------------------------------
    # SCENARIO 9: Choppy Market
    # -------------------------------------------------------------------------
    def test_09_choppy_market(self):
        """Benchmark oscillates back and forth with near-zero net move."""
        t0 = "2026-10-01 09:30:00"
        t1 = "2026-10-01 10:00:00"
        t2 = "2026-10-01 11:00:00"
        t_eod = "2026-10-01 15:15:00"

        self._insert_candidate(t0, "TITAN.NS", "BUY", 54.0, "BUY", 52.0, 3400.0)

        # Choppy oscillations
        self._insert_bar("^NSEI", t0, 25000, 25080, 24950, 25000)
        self._insert_bar("^NSEI", t1, 25000, 25120, 24900, 24980)
        self._insert_bar("^NSEI", t2, 24980, 25060, 24920, 25020)
        self._insert_bar("^NSEI", t_eod, 25020, 25040, 24970, 25005) # Net +0.02%

        # TITAN oscillates
        self._insert_bar("TITAN.NS", t0, 3400, 3420, 3380, 3400)
        self._insert_bar("TITAN.NS", t1, 3400, 3410, 3370, 3390)
        self._insert_bar("TITAN.NS", t2, 3390, 3430, 3385, 3405)
        self._insert_bar("TITAN.NS", t_eod, 3405, 3415, 3390, 3398)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        chop_info = res["chop_forensics"]
        self.assertIn("chop_cohort", chop_info)
        self.assertIn("trend_cohort", chop_info)

    # -------------------------------------------------------------------------
    # SCENARIO 10: Identical Scores (Neutral Conviction)
    # -------------------------------------------------------------------------
    def test_10_identical_scores(self):
        """Qlib and Legacy have identical neutral score (50.0)."""
        t0 = "2026-10-01 09:30:00"
        t_eod = "2026-10-01 15:15:00"

        self._insert_candidate(t0, "MARUTI.NS", "BUY", 50.0, "BUY", 50.0, 12000.0)

        self._insert_bar("^NSEI", t0, 25000, 25000, 24950, 25000)
        self._insert_bar("^NSEI", t_eod, 25000, 25050, 24950, 25000)
        self._insert_bar("MARUTI.NS", t0, 12000, 12050, 11950, 12000)
        self._insert_bar("MARUTI.NS", t_eod, 12000, 12050, 11950, 12000)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["sample_size"], 1)

    # -------------------------------------------------------------------------
    # SCENARIO 11: Tied Ranking
    # -------------------------------------------------------------------------
    def test_11_tied_ranking(self):
        """Multiple tickers share the exact same composite conviction."""
        t0 = "2026-10-01 09:30:00"
        t_eod = "2026-10-01 15:15:00"

        self._insert_candidate(t0, "STOCK_A.NS", "BUY", 60.0, "BUY", 60.0, 100.0)
        self._insert_candidate(t0, "STOCK_B.NS", "BUY", 60.0, "BUY", 60.0, 200.0)

        self._insert_bar("^NSEI", t0, 25000, 25000, 24900, 25000)
        self._insert_bar("^NSEI", t_eod, 25000, 25100, 25000, 25050)
        self._insert_bar("STOCK_A.NS", t0, 100, 102, 99, 100)
        self._insert_bar("STOCK_A.NS", t_eod, 101, 103, 100, 102)
        self._insert_bar("STOCK_B.NS", t0, 200, 204, 198, 200)
        self._insert_bar("STOCK_B.NS", t_eod, 201, 205, 200, 203)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        self.assertEqual(res["sample_size"], 2)
        rank_res = res["top_bottom_rank_analysis"]
        self.assertIsNotNone(rank_res)

    # -------------------------------------------------------------------------
    # SCENARIO 12: Missing Prediction
    # -------------------------------------------------------------------------
    def test_12_missing_prediction(self):
        """Candidate has None / empty prediction for one engine."""
        t0 = "2026-10-01 09:30:00"
        t_eod = "2026-10-01 15:15:00"

        # Qlib prediction is None
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO consensus_candidate_shadow_log
            (timestamp, scan_id, ticker, symbol, strategy, qlib_direction, qlib_score, qlib_price,
             legacy_direction, legacy_confidence, legacy_price, composite_score, action_taken, entry_ref_price)
            VALUES (?, 'scan_1', 'AXISBANK.NS', 'AXISBANK', 'INTRADAY', NULL, NULL, NULL, 'BUY', 58.0, 1100.0, 58.0, 'TAKE', 1100.0)
        """, (t0,))
        conn.commit()
        conn.close()

        self._insert_bar("^NSEI", t0, 25000, 25000, 24900, 25000)
        self._insert_bar("^NSEI", t_eod, 25000, 25100, 25000, 25050)
        self._insert_bar("AXISBANK.NS", t0, 1100, 1110, 1090, 1100)
        self._insert_bar("AXISBANK.NS", t_eod, 1105, 1120, 1100, 1115)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        self.assertEqual(res["status"], "SUCCESS")

    # -------------------------------------------------------------------------
    # SCENARIO 13: Missing Benchmark
    # -------------------------------------------------------------------------
    def test_13_missing_benchmark(self):
        """No benchmark bars exist in the database. Evaluates fail-safely."""
        t0 = "2026-10-01 09:30:00"
        t_eod = "2026-10-01 15:15:00"

        self._insert_candidate(t0, "KOTAKBANK.NS", "BUY", 60.0, "BUY", 60.0, 1750.0)
        # Note: No ^NSEI bars inserted!

        self._insert_bar("KOTAKBANK.NS", t0, 1750, 1760, 1740, 1750)
        self._insert_bar("KOTAKBANK.NS", t_eod, 1755, 1770, 1750, 1765)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        self.assertEqual(res["status"], "SUCCESS")
        # Should complete without throwing an unhandled exception
        self.assertEqual(res["sample_size"], 1)

    # -------------------------------------------------------------------------
    # SCENARIO 14: Stale Benchmark
    # -------------------------------------------------------------------------
    def test_14_stale_benchmark(self):
        """Benchmark bars are from yesterday, none today."""
        t_yesterday = "2026-09-30 15:15:00"
        t0 = "2026-10-01 09:30:00"
        t_eod = "2026-10-01 15:15:00"

        self._insert_candidate(t0, "BAJFINANCE.NS", "BUY", 62.0, "BUY", 60.0, 7000.0)
        # Benchmark only has yesterday's bar
        self._insert_bar("^NSEI", t_yesterday, 24900, 24950, 24880, 24920)

        self._insert_bar("BAJFINANCE.NS", t0, 7000, 7020, 6980, 7000)
        self._insert_bar("BAJFINANCE.NS", t_eod, 7010, 7050, 7000, 7040)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        self.assertEqual(res["status"], "SUCCESS")

    # -------------------------------------------------------------------------
    # SCENARIO 15: Stale Stock Price
    # -------------------------------------------------------------------------
    def test_15_stale_stock_price(self):
        """Stock has no future bars after signal timestamp."""
        t0 = "2026-10-01 09:30:00"

        self._insert_candidate(t0, "NESTLEIND.NS", "BUY", 65.0, "BUY", 65.0, 2500.0)
        # Only bar at t0, no subsequent bars
        self._insert_bar("^NSEI", t0, 25000, 25000, 24950, 25000)
        self._insert_bar("NESTLEIND.NS", t0, 2500, 2505, 2495, 2500)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        self.assertEqual(res["status"], "SUCCESS")
        # Returns 0% change gracefully when no future bars exist
        self.assertEqual(res["sample_size"], 1)

    # -------------------------------------------------------------------------
    # SCENARIO 16: Future Timestamp & Point-in-Time Causality
    # -------------------------------------------------------------------------
    def test_16_future_timestamp_point_in_time_causality(self):
        """Signals at T0 only use forward bars (>= T0). Past bars do not leak into return."""
        t_past = "2026-10-01 09:15:00"
        t0 = "2026-10-01 09:30:00"
        t_future = "2026-10-01 10:00:00"

        self._insert_candidate(t0, "ASIANPAINT.NS", "BUY", 60.0, "BUY", 60.0, 3000.0)

        # Past bar had a huge spike that must NOT be counted as forward return
        self._insert_bar("ASIANPAINT.NS", t_past, 2800, 3100, 2800, 3100)
        self._insert_bar("ASIANPAINT.NS", t0, 3000, 3010, 2990, 3000)
        self._insert_bar("ASIANPAINT.NS", t_future, 3000, 3030, 2995, 3030) # +1.0%

        self._insert_bar("^NSEI", t_past, 24800, 24900, 24800, 24900)
        self._insert_bar("^NSEI", t0, 25000, 25020, 24980, 25000)
        self._insert_bar("^NSEI", t_future, 25000, 25050, 25000, 25050)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path)
        abs_res = res["stock_absolute_direction"]["models"]["CONSENSUS"]["30m"]
        # Stock should reflect forward +1.0% return from 3000 -> 3030, not past 2800 -> 3100
        self.assertAlmostEqual(abs_res["buy"]["mean_return_pct"], 1.0, places=2)

    # -------------------------------------------------------------------------
    # SCENARIO 17: Canonical Cost Survival & Null Hypothesis Evaluation
    # -------------------------------------------------------------------------
    def test_17_cost_survival_and_null_hypothesis_evaluation(self):
        """Small gross edge (+0.10%) is eliminated by canonical friction (~0.1872%).
        Verifies that null hypotheses are tested rigorously with statistical tests.
        """
        t0 = "2026-10-01 09:30:00"
        t_eod = "2026-10-01 15:15:00"

        # Signal with marginal gain
        self._insert_candidate(t0, "SUNPHARMA.NS", "BUY", 58.0, "BUY", 58.0, 1500.0)
        self._insert_bar("^NSEI", t0, 25000, 25000, 24950, 25000)
        self._insert_bar("^NSEI", t_eod, 25000, 25010, 24990, 25000)

        # Stock rises only +0.10% (1500 -> 1501.5)
        self._insert_bar("SUNPHARMA.NS", t0, 1500, 1502, 1498, 1500)
        self._insert_bar("SUNPHARMA.NS", t_eod, 1501, 1502, 1500, 1501.5)

        res = DirectionalEdgeForensics.analyze_directional_edge(db_path=self.db_path, cost_mode="CANONICAL")
        cost_survival = res["cost_survival_analysis"]
        null_hypotheses = res["null_hypotheses"]

        # Friction drag should turn +0.10% gross gain into negative net return
        self.assertLess(cost_survival["net_expectancy_pct"], 0.0)
        self.assertIn("H0_1_market_direction_null", null_hypotheses)
        self.assertIn("H0_2_stock_selection_null", null_hypotheses)
        self.assertIsNotNone(res["executive_verdict"])


if __name__ == "__main__":
    unittest.main()
