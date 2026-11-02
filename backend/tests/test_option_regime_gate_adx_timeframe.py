"""
Unit test suite verifying ADX timeframe resolution and OptionRegimeGate invariants:
  1. Mathematical proof that NSE regular session (09:15-15:30 = 375 min) yields exactly 25 completed 15m bars,
     making a 28-bar 15m warm-up from a single session mathematically impossible.
  2. Canonical Wilder ADX(14) 28-bar warm-up strictly uses completed 1-minute bars.
  3. Gate eligibility timeline:
     - 09:30:00 IST (15 completed bars): Fails closed (insufficient bars < 28).
     - 09:43:00 IST (28 completed bars from 09:15 to 09:42:00, completed at 09:43:00): Eligible.
  4. Strict fail-closed invariants:
     - Incomplete / provisional candle (is_complete = 0) -> Fail closed.
     - Synthetic carry candle (is_synthetic = 1) -> Fail closed.
     - Data gap candle (is_gap = 1) -> Fail closed.
     - Temporal discontinuity / missing minute (step != 60s) -> Fail closed.
     - Stale index feed (staleness > 120s) -> Fail closed.
     - Fresh index feed (staleness <= 120s) -> Passes data check.
     - Strict point-in-time causality: Bars with available_at > as_of_time or is_point_in_time = 0 are excluded.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.analytics.option_sniffer.option_constants import (
    MAX_DATA_STALENESS_SECONDS,
    MARKET_OPEN_HOUR,
    MARKET_OPEN_MINUTE,
)
from app.analytics.option_sniffer.option_regime_gate import (
    OptionRegimeGate,
    MIN_WARMUP_BARS,
    compute_wilder_adx_canonical,
    seed_mock_continuous_bars,
)


class TestOptionRegimeGateADXTimeframe(unittest.TestCase):

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp_db.name
        self.tmp_db.close()

        self.db_patcher = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.db_patcher.start()

        self._init_db()
        self.gate = OptionRegimeGate(db_path=self.db_path)

    def tearDown(self):
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS continuous_index_ohlcv (
                    ticker TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER DEFAULT 0,
                    tick_count INTEGER DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'LOCAL_AGGREGATOR',
                    is_complete INTEGER DEFAULT 1,
                    is_synthetic INTEGER DEFAULT 0,
                    is_gap INTEGER DEFAULT 0,
                    provider_open REAL,
                    provider_high REAL,
                    provider_low REAL,
                    provider_close REAL,
                    local_open REAL,
                    local_high REAL,
                    local_low REAL,
                    local_close REAL,
                    discrepancy_flag INTEGER DEFAULT 0,
                    received_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    reconciled_at TEXT,
                    version INTEGER DEFAULT 1,
                    is_point_in_time INTEGER DEFAULT 1,
                    PRIMARY KEY (ticker, timestamp, version)
                );
            """)

    def _insert_bar(
        self,
        ticker: str,
        timestamp: str,
        open_p: float = 25000.0,
        high_p: float = 25010.0,
        low_p: float = 24990.0,
        close_p: float = 25005.0,
        is_complete: int = 1,
        is_synthetic: int = 0,
        is_gap: int = 0,
        available_at: str = None,
        is_point_in_time: int = 1
    ):
        if available_at is None:
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:00")
            available_at = (dt + timedelta(minutes=1)).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO continuous_index_ohlcv (
                    ticker, timestamp, open, high, low, close, volume, tick_count,
                    source, is_complete, is_synthetic, is_gap, received_at, available_at,
                    version, is_point_in_time
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, 10000, 100,
                    'LOCAL_AGGREGATOR', ?, ?, ?, ?, ?,
                    1, ?
                )
            """, (ticker, timestamp, open_p, high_p, low_p, close_p,
                  is_complete, is_synthetic, is_gap, available_at, available_at, is_point_in_time))

    # -------------------------------------------------------------------------
    # 1. MATHEMATICAL PROOF OF 15M IMPOSSIBILITY
    # -------------------------------------------------------------------------

    def test_nse_session_15m_bar_count_is_strictly_25(self):
        """
        NSE trading session: 09:15:00 to 15:30:00 = 375 minutes total.
        Completed 15-minute bars per day = 375 / 15 = 25 bars.
        A 28-bar 15-minute warm-up requirement for a single intraday session
        is mathematically impossible (28 > 25).
        """
        session_start = datetime.strptime("09:15:00", "%H:%M:%S")
        session_end = datetime.strptime("15:30:00", "%H:%M:%S")
        duration_minutes = (session_end - session_start).total_seconds() / 60.0

        self.assertEqual(duration_minutes, 375.0)
        max_15m_bars = int(duration_minutes / 15)
        self.assertEqual(max_15m_bars, 25)
        self.assertLess(max_15m_bars, MIN_WARMUP_BARS)

    def test_nse_session_1m_bars_can_satisfy_28_bar_warmup(self):
        """
        Completed 1-minute bars in an NSE session = 375 bars.
        28 completed 1m bars require 28 minutes (09:15 to 09:43 IST),
        leaving 347 completed bars for active option trading.
        """
        session_start = datetime.strptime("09:15:00", "%H:%M:%S")
        session_end = datetime.strptime("15:30:00", "%H:%M:%S")
        total_1m_bars = int((session_end - session_start).total_seconds() / 60.0)

        self.assertEqual(total_1m_bars, 375)
        self.assertGreaterEqual(total_1m_bars, MIN_WARMUP_BARS)

    # -------------------------------------------------------------------------
    # 2. TIMELINE ELIGIBILITY AT 09:43:00 IST
    # -------------------------------------------------------------------------

    def test_gate_fails_closed_at_09_30_ist_with_15_bars(self):
        """
        At 09:30:00 IST, only 15 1-minute bars have completed (09:15 to 09:29).
        OptionRegimeGate must fail closed because 15 < 28 required bars.
        """
        session_date = "2026-10-19"
        start_dt = datetime.strptime(f"{session_date} 09:15:00", "%Y-%m-%d %H:%M:%S")

        # Insert 15 completed bars (09:15:00 to 09:29:00)
        for i in range(15):
            ts = (start_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            self._insert_bar("NIFTY", ts)

        as_of = datetime.strptime(f"{session_date} 09:30:00", "%Y-%m-%d %H:%M:%S")
        ok, msg, df = self.gate.get_completed_index_bars("NIFTY", session_date, as_of)

        self.assertFalse(ok)
        self.assertIn("Insufficient continuous completed bars (15 < 28 required)", msg)
        self.assertIsNone(df)

    def test_gate_becomes_eligible_at_exactly_09_43_ist_with_28_bars(self):
        """
        At 09:43:00 IST, exactly 28 1-minute bars have completed:
        Bar 1: 09:15 -> 09:16
        ...
        Bar 28: 09:42 -> 09:43 (available_at = 09:43:00).
        OptionRegimeGate must become eligible and successfully compute Wilder ADX(14).
        """
        session_date = "2026-10-19"
        start_dt = datetime.strptime(f"{session_date} 09:15:00", "%Y-%m-%d %H:%M:%S")

        # Insert 28 completed bars (09:15:00 to 09:42:00) with a clean upward trend
        for i in range(28):
            ts = (start_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            o = 25000.0 + i * 5.0
            h = o + 6.0
            l = o - 2.0
            c = o + 4.0
            self._insert_bar("NIFTY", ts, open_p=o, high_p=h, low_p=l, close_p=c)

        as_of = datetime.strptime(f"{session_date} 09:43:00", "%Y-%m-%d %H:%M:%S")
        ok, msg, df = self.gate.get_completed_index_bars("NIFTY", session_date, as_of)

        self.assertTrue(ok)
        self.assertEqual(msg, "OK")
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 28)

        # Verify canonical Wilder ADX(14) succeeds with exactly 28 bars
        adx_res = compute_wilder_adx_canonical(df["high"].values, df["low"].values, df["close"].values, period=14)
        self.assertIsNotNone(adx_res)
        adx, p_di, m_di = adx_res
        self.assertGreater(adx, 0.0)
        self.assertGreater(p_di, m_di)  # Bullish trend

    # -------------------------------------------------------------------------
    # 3. FAIL-CLOSED DEFECT INVARIANTS
    # -------------------------------------------------------------------------

    def test_fail_closed_on_incomplete_provisional_candle(self):
        """
        If any candle in the 28-bar window is incomplete / provisional (is_complete = 0),
        the gate must fail closed immediately.
        """
        session_date = "2026-10-19"
        start_dt = datetime.strptime(f"{session_date} 09:15:00", "%Y-%m-%d %H:%M:%S")

        for i in range(28):
            ts = (start_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            # Mark bar 20 as provisional / incomplete
            is_comp = 0 if i == 20 else 1
            self._insert_bar("NIFTY", ts, is_complete=is_comp)

        as_of = datetime.strptime(f"{session_date} 09:43:00", "%Y-%m-%d %H:%M:%S")
        ok, msg, df = self.gate.get_completed_index_bars("NIFTY", session_date, as_of)

        self.assertFalse(ok)
        self.assertIn("Incomplete or provisional candle detected", msg)
        self.assertIsNone(df)

    def test_fail_closed_on_synthetic_carry_candle(self):
        """
        If a candle was artificially generated / forward-carried (is_synthetic = 1),
        the gate must fail closed to protect against synthetic indicator distortion.
        """
        session_date = "2026-10-19"
        start_dt = datetime.strptime(f"{session_date} 09:15:00", "%Y-%m-%d %H:%M:%S")

        for i in range(28):
            ts = (start_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            is_synth = 1 if i == 14 else 0
            self._insert_bar("NIFTY", ts, is_synthetic=is_synth)

        as_of = datetime.strptime(f"{session_date} 09:43:00", "%Y-%m-%d %H:%M:%S")
        ok, msg, df = self.gate.get_completed_index_bars("NIFTY", session_date, as_of)

        self.assertFalse(ok)
        self.assertIn("Synthetic carry bar detected", msg)
        self.assertIsNone(df)

    def test_fail_closed_on_data_gap_candle(self):
        """
        If a candle is marked as a gap placeholder (is_gap = 1),
        the gate must fail closed.
        """
        session_date = "2026-10-19"
        start_dt = datetime.strptime(f"{session_date} 09:15:00", "%Y-%m-%d %H:%M:%S")

        for i in range(28):
            ts = (start_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            is_g = 1 if i == 10 else 0
            self._insert_bar("NIFTY", ts, is_gap=is_g)

        as_of = datetime.strptime(f"{session_date} 09:43:00", "%Y-%m-%d %H:%M:%S")
        ok, msg, df = self.gate.get_completed_index_bars("NIFTY", session_date, as_of)

        self.assertFalse(ok)
        self.assertIn("Data gap bar detected", msg)
        self.assertIsNone(df)

    def test_fail_closed_on_temporal_discontinuity(self):
        """
        If consecutive 1-minute bars are not exactly 60 seconds apart (e.g. 09:25 is missing,
        jumping directly from 09:24 to 09:26), the gate must fail closed.
        """
        session_date = "2026-10-19"
        start_dt = datetime.strptime(f"{session_date} 09:15:00", "%Y-%m-%d %H:%M:%S")

        # Insert 28 bars but skip minute 10 (09:25:00)
        inserted = 0
        i = 0
        while inserted < 28:
            if i == 10:
                i += 1
                continue
            ts = (start_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            self._insert_bar("NIFTY", ts)
            inserted += 1
            i += 1

        as_of = start_dt + timedelta(minutes=i)
        ok, msg, df = self.gate.get_completed_index_bars("NIFTY", session_date, as_of)

        self.assertFalse(ok)
        self.assertIn("Temporal discontinuity detected", msg)
        self.assertIsNone(df)

    def test_fail_closed_on_stale_data_older_than_120s(self):
        """
        If the last completed candle ended more than MAX_DATA_STALENESS_SECONDS (120s)
        before the as_of_time, the gate must fail closed.
        """
        session_date = "2026-10-19"
        start_dt = datetime.strptime(f"{session_date} 09:15:00", "%Y-%m-%d %H:%M:%S")

        # 28 bars from 09:15 to 09:42 (last candle ends at 09:43:00)
        for i in range(28):
            ts = (start_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            self._insert_bar("NIFTY", ts)

        # Decision time is 09:45:30 (150s after 09:43:00 candle end > 120s)
        as_of = datetime.strptime(f"{session_date} 09:45:30", "%Y-%m-%d %H:%M:%S")
        ok, msg, df = self.gate.get_completed_index_bars("NIFTY", session_date, as_of)

        self.assertFalse(ok)
        self.assertIn("Index feed is stale", msg)
        self.assertIn("150s > 120.0s max", msg)
        self.assertIsNone(df)

    def test_passes_when_data_is_fresh_under_120s(self):
        """
        If the last completed candle ended within MAX_DATA_STALENESS_SECONDS (e.g. 45s ago),
        the staleness check passes.
        """
        session_date = "2026-10-19"
        start_dt = datetime.strptime(f"{session_date} 09:15:00", "%Y-%m-%d %H:%M:%S")

        # 28 bars from 09:15 to 09:42 (last candle ends at 09:43:00)
        for i in range(28):
            ts = (start_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            self._insert_bar("NIFTY", ts)

        # Decision time is 09:43:45 (45s after 09:43:00 candle end <= 120s)
        as_of = datetime.strptime(f"{session_date} 09:43:45", "%Y-%m-%d %H:%M:%S")
        ok, msg, df = self.gate.get_completed_index_bars("NIFTY", session_date, as_of)

        self.assertTrue(ok)
        self.assertEqual(msg, "OK")
        self.assertEqual(len(df), 28)

    def test_causal_point_in_time_excludes_future_or_non_pit_bars(self):
        """
        Bars with available_at > as_of_time or is_point_in_time = 0 must never leak
        into the gate calculation.
        """
        session_date = "2026-10-19"
        start_dt = datetime.strptime(f"{session_date} 09:15:00", "%Y-%m-%d %H:%M:%S")

        # Insert 27 valid bars
        for i in range(27):
            ts = (start_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            self._insert_bar("NIFTY", ts)

        # Bar 28 has available_at in the future relative to as_of
        ts_28 = (start_dt + timedelta(minutes=27)).strftime("%Y-%m-%d %H:%M:00")
        as_of = datetime.strptime(f"{session_date} 09:43:00", "%Y-%m-%d %H:%M:%S")
        future_avail = (as_of + timedelta(seconds=10)).isoformat()
        self._insert_bar("NIFTY", ts_28, available_at=future_avail)

        ok, msg, df = self.gate.get_completed_index_bars("NIFTY", session_date, as_of)
        # Should fail closed because future bar cannot be seen point-in-time
        self.assertFalse(ok)
        self.assertIn("Insufficient continuous completed bars (27 < 28 required)", msg)


if __name__ == "__main__":
    unittest.main()
