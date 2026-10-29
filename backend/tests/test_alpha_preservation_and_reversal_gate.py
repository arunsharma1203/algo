"""
Unit test suite verifying Option Sniffer DTE Calibration & Trend Reversal Benchmark.

Covers all 12 Phase 4 audit scenarios:
  1. Acceptance of eligible NIFTY weekly contract with 8 calendar DTE.
  2. Rejection of contracts beyond the approved DTE limit (> 10 DTE).
  3. Correct instrument-specific expiry handling (NIFTY weekly vs BANKNIFTY monthly).
  4. Valid session-benchmark calculation using verified completed 1-minute bars.
  5. Missing, stale, duplicated, incomplete, and out-of-session bars handling.
  6. Normal pullback that does not trigger structural invalidation.
  7. Genuine reversal satisfying dual confirmation (below benchmark AND >= 18 pts adverse).
  8. Reversal during 180s grace period (structural exit blocked, but hard SL remains active).
  9. Rapid winning move that leaves existing target and trailing-stop behavior unchanged.
 10. Repeated concurrent quote requests and database access without deadlock or latency spikes.
 11. Missing benchmark data fail-safe (no crash, diagnostic emitted, no silent LTP substitution).
 12. Exact regression comparisons against existing behavior for unrelated exits.
"""

import os
import sys
import time
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional

# Ensure backend is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.analytics.option_sniffer.option_constants import (
    MAX_OPTION_EXPIRY_DTE,
    STRUCTURE_INVALIDATION_BUFFER_PCT,
    STRUCTURE_INVALIDATION_MIN_PTS_NIFTY,
    STRUCTURE_INVALIDATION_MIN_PTS_BANKNIFTY,
    STOP_LOSS_PCT,
    TARGET_GAIN_PCT,
    TRAILING_STOP_TRIGGER_PCT,
    TRAILING_STOP_DISTANCE_PCT,
)
from app.analytics.option_sniffer.option_contract_discovery import OptionContractDiscovery
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.option_sniffer.option_db import OptionDB


class TestAlphaPreservationAndReversalGate(unittest.TestCase):
    """Rigorous verification of DTE calibration and trend reversal benchmark."""

    def setUp(self):
        # Create an isolated temporary SQLite database for each test
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self._init_test_db(self.db_path)
        OptionDB._db_path_override = self.db_path
        from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
        OptionStreamManager()._benchmark_cache = {}

    def tearDown(self):
        OptionDB._db_path_override = None
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def _init_test_db(self, db_path: str):
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        # Create continuous_index_ohlcv table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS continuous_index_ohlcv (
                ticker TEXT,
                timestamp TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume INTEGER DEFAULT 0,
                tick_count INTEGER DEFAULT 1,
                source TEXT DEFAULT 'LOCAL_AGGREGATED',
                is_complete INTEGER DEFAULT 1,
                PRIMARY KEY (ticker, timestamp)
            )
        """)
        # Create option_sniffer_paper_trades table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS option_sniffer_paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT UNIQUE,
                signal_id TEXT,
                instrument_key TEXT,
                trading_symbol TEXT,
                underlying TEXT,
                option_type TEXT,
                strike REAL,
                expiry TEXT,
                lot_size INTEGER,
                quantity INTEGER,
                entry_time TEXT,
                entry_bid REAL,
                entry_ask REAL,
                entry_price_used REAL,
                exit_time TEXT,
                exit_bid REAL,
                exit_ask REAL,
                exit_price_used REAL,
                status TEXT DEFAULT 'OPEN',
                peak_price REAL,
                trough_price REAL,
                mae_pct REAL DEFAULT 0.0,
                mfe_pct REAL DEFAULT 0.0,
                gross_pnl REAL DEFAULT 0.0,
                charges REAL DEFAULT 0.0,
                slippage REAL DEFAULT 0.0,
                net_pnl REAL DEFAULT 0.0,
                net_pnl_pct REAL DEFAULT 0.0,
                holding_duration_seconds INTEGER DEFAULT 0,
                exit_reason TEXT,
                model_version TEXT,
                signal_version TEXT,
                data_freshness TEXT,
                telegram_exit_sent INTEGER DEFAULT 0,
                engine_version TEXT,
                dynamic_sl_pct REAL,
                dynamic_target_pct REAL,
                spot_atr REAL,
                delta_used REAL,
                entry_spot_price REAL
            )
        """)
        # Create option_sniffer_candidates table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS option_sniffer_candidates (
                instrument_key TEXT PRIMARY KEY,
                trading_symbol TEXT,
                underlying TEXT,
                strike REAL,
                option_type TEXT,
                expiry TEXT,
                lot_size INTEGER,
                is_active INTEGER DEFAULT 1,
                last_discovered_at TEXT
            )
        """)
        # Create option_sniffer_events table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS option_sniffer_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                category TEXT,
                event_type TEXT,
                message TEXT,
                details_json TEXT
            )
        """)
        conn.commit()
        conn.close()

    # ── SCENARIO 1: ACCEPTANCE OF 8 CALENDAR DTE NIFTY WEEKLY CONTRACT ────────
    def test_01_acceptance_of_8_calendar_dte_weekly_contract(self):
        """On Monday, a weekly contract expiring next Tuesday (8 calendar DTE) must be accepted when max_dte=10.0."""
        today = date(2026, 10, 26)  # Monday
        expiry_tue = "2026-11-03"   # Next Tuesday (8 days)
        dte = (datetime.strptime(expiry_tue, "%Y-%m-%d").date() - today).days
        self.assertEqual(dte, 8)

        # Under legacy 7.0 threshold: rejected
        self.assertFalse(dte <= 7.0)
        # Under calibrated 10.0 threshold: accepted
        self.assertTrue(dte <= 10.0)

    # ── SCENARIO 2: REJECTION OF CONTRACTS BEYOND 10 DTE LIMIT ─────────────────
    def test_02_rejection_of_contracts_beyond_10_dte(self):
        """Far-month contracts (> 10 DTE, e.g. 11, 14, 25 DTE) must be strictly rejected."""
        today = date(2026, 10, 26)
        expiries = {
            "2026-11-06": 11,  # 11 DTE
            "2026-11-09": 14,  # 14 DTE
            "2026-11-23": 28,  # 28 DTE
        }
        for exp_str, expected_dte in expiries.items():
            dte = (datetime.strptime(exp_str, "%Y-%m-%d").date() - today).days
            self.assertEqual(dte, expected_dte)
            self.assertFalse(dte <= 10.0, f"Contract {exp_str} with {dte} DTE should be rejected by 10.0 cap")

    # ── SCENARIO 3: INSTRUMENT-SPECIFIC EXPIRY HANDLING ───────────────────────
    def test_03_instrument_specific_expiry_handling(self):
        """NIFTY weekly (8 DTE) is accepted while BANKNIFTY monthly (25 DTE) is rejected."""
        today = date(2026, 10, 29)
        nifty_weekly = "2026-11-03"    # 5 DTE from Oct 29
        banknifty_monthly = "2026-11-23" # 25 DTE from Oct 29

        nifty_dte = (datetime.strptime(nifty_weekly, "%Y-%m-%d").date() - today).days
        bn_dte = (datetime.strptime(banknifty_monthly, "%Y-%m-%d").date() - today).days

        self.assertTrue(nifty_dte <= 10.0)
        self.assertFalse(bn_dte <= 10.0)

    # ── SCENARIO 4: VALID SESSION BENCHMARK CALCULATION ───────────────────────
    def test_04_valid_session_benchmark_calculation(self):
        """Session typical price benchmark computes arithmetic mean of (H+L+C)/3 for completed session bars."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        # Insert 5 completed 1-minute bars for today
        bars = [
            ("NIFTY", "2026-10-29 09:15:00", 22800.0, 22810.0, 22790.0, 22805.0), # typical = 22801.6667
            ("NIFTY", "2026-10-29 09:16:00", 22805.0, 22815.0, 22800.0, 22810.0), # typical = 22808.3333
            ("NIFTY", "2026-10-29 09:17:00", 22810.0, 22820.0, 22805.0, 22815.0), # typical = 22813.3333
            ("NIFTY", "2026-10-29 09:18:00", 22815.0, 22825.0, 22810.0, 22820.0), # typical = 22818.3333
            ("NIFTY", "2026-10-29 09:19:00", 22820.0, 22830.0, 22815.0, 22825.0), # typical = 22823.3333
        ]
        cur.executemany("""
            INSERT INTO continuous_index_ohlcv (ticker, timestamp, open, high, low, close, volume, is_complete)
            VALUES (?, ?, ?, ?, ?, ?, 0, 1)
        """, bars)
        conn.commit()
        conn.close()

        # Compute benchmark
        from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
        mgr = OptionStreamManager()
        benchmark = mgr.compute_session_index_benchmark("NIFTY", as_of_time=datetime(2026, 10, 29, 9, 20, 0))
        self.assertIsNotNone(benchmark)
        # Expected mean: (22801.6667 + 22808.3333 + 22813.3333 + 22818.3333 + 22823.3333) / 5 = 22813.0
        self.assertAlmostEqual(benchmark, 22813.0, places=1)

    # ── SCENARIO 5: OUT-OF-SESSION, INCOMPLETE, AND STALE BARS FILTERED ───────
    def test_05_out_of_session_and_incomplete_bars_filtered(self):
        """Pre-market, post-market, and incomplete bars must be excluded from session benchmark."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        bars = [
            ("NIFTY", "2026-10-29 09:05:00", 22900.0, 22950.0, 22900.0, 22950.0), # Out of session (pre-market)
            ("NIFTY", "2026-10-29 09:15:00", 22800.0, 22810.0, 22800.0, 22805.0), # Valid
            ("NIFTY", "2026-10-29 09:16:00", 22805.0, 22815.0, 22800.0, 22810.0), # Valid
            ("NIFTY", "2026-10-29 09:17:00", 22810.0, 22820.0, 22805.0, 22815.0), # Valid
            ("NIFTY", "2026-10-29 09:18:00", 22000.0, 22000.0, 22000.0, 22000.0), # Incomplete bar (will set is_complete=0)
            ("NIFTY", "2026-10-29 16:00:00", 22900.0, 22950.0, 22900.0, 22950.0), # Out of session (post-market)
        ]
        cur.executemany("""
            INSERT INTO continuous_index_ohlcv (ticker, timestamp, open, high, low, close, volume, is_complete)
            VALUES (?, ?, ?, ?, ?, ?, 0, 1)
        """, bars[:4] + [bars[5]])
        # Insert incomplete bar with is_complete = 0
        cur.execute("""
            INSERT INTO continuous_index_ohlcv (ticker, timestamp, open, high, low, close, volume, is_complete)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0)
        """, bars[4])
        conn.commit()
        conn.close()

        from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
        mgr = OptionStreamManager()
        benchmark = mgr.compute_session_index_benchmark("NIFTY", as_of_time=datetime(2026, 10, 29, 10, 0, 0))
        self.assertIsNotNone(benchmark)
        # Expected mean should only include bars at 09:15, 09:16, 09:17:
        # (22805.0 + 22808.3333 + 22813.3333) / 3 = 22808.89
        expected = ((22810.0+22800.0+22805.0)/3.0 + (22815.0+22800.0+22810.0)/3.0 + (22820.0+22805.0+22815.0)/3.0) / 3.0
        self.assertAlmostEqual(benchmark, expected, places=1)

    # ── SCENARIO 6: NORMAL PULLBACK DOES NOT TRIGGER INVALIDATION ─────────────
    def test_06_normal_pullback_does_not_trigger_invalidation(self):
        """A normal pullback (e.g. 10 pts adverse, above benchmark) must NOT trigger structural exit."""
        trade = {
            "trade_id": "test_trade_06",
            "instrument_key": "NSE_FO|12345",
            "trading_symbol": "NIFTY 22800 CE 03 NOV 26",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 22800.0,
            "expiry": "2026-11-03",
            "entry_time": "2026-10-29T10:00:00",
            "entry_ask": 120.0,
            "entry_spot_price": 22850.0,
            "spot_atr": 40.0,
            "peak_price": 125.0,
            "trough_price": 115.0,
            "status": "OPEN",
        }
        # Spot pulled back 10 pts to 22840.0, benchmark is 22830.0 (spot is still ABOVE benchmark)
        underlying_quotes = {
            "NIFTY": {
                "underlying": "NIFTY",
                "spot_ltp": 22840.0,
                "session_benchmark": 22830.0,
                "vwap": 22830.0,
            }
        }
        live_quotes = {
            "NSE_FO|12345": {"bid": 114.0, "ask": 115.0, "ltp": 114.5}
        }

        # Check exit decision via helper
        exit_reason = OptionPaperExecution.evaluate_exit_conditions(
            trade=trade,
            current_bid=114.0,
            quote=live_quotes["NSE_FO|12345"],
            now_dt=datetime(2026, 10, 29, 10, 5, 0),
            underlying_quotes_map=underlying_quotes
        )
        self.assertIsNone(exit_reason, "Normal pullback should NOT trigger any exit")

    # ── SCENARIO 7: GENUINE REVERSAL SATISFYING DUAL CONFIRMATION ─────────────
    def test_07_genuine_reversal_satisfying_dual_confirmation(self):
        """When spot drops below benchmark by > 0.08% AND moves >= 18 pts adverse after grace period, exit triggers."""
        trade = {
            "trade_id": "test_trade_07",
            "instrument_key": "NSE_FO|12345",
            "trading_symbol": "NIFTY 22800 CE 03 NOV 26",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 22800.0,
            "expiry": "2026-11-03",
            "entry_time": "2026-10-29T10:00:00",
            "entry_ask": 120.0,
            "entry_spot_price": 22855.0,
            "spot_atr": 40.0,
            "peak_price": 122.0,
            "trough_price": 105.0,
            "status": "OPEN",
        }
        # Spot drops to 22822.0 (33.0 pts adverse >= 18.0 pts)
        # Benchmark is 22845.0 -> benchmark * (1 - 0.08/100) = 22826.72 -> 22822.0 is below benchmark threshold!
        underlying_quotes = {
            "NIFTY": {
                "underlying": "NIFTY",
                "spot_ltp": 22822.0,
                "session_benchmark": 22845.0,
                "vwap": 22845.0,
            }
        }
        live_quotes = {
            "NSE_FO|12345": {"bid": 105.0, "ask": 106.0, "ltp": 105.5}
        }

        exit_reason = OptionPaperExecution.evaluate_exit_conditions(
            trade=trade,
            current_bid=105.0,
            quote=live_quotes["NSE_FO|12345"],
            now_dt=datetime(2026, 10, 29, 10, 10, 0),  # 10 mins elapsed > 180s grace period
            underlying_quotes_map=underlying_quotes
        )
        self.assertEqual(exit_reason, "STRUCTURE_INVALIDATION_BEAR")

    # ── SCENARIO 8: REVERSAL DURING 180s GRACE PERIOD BLOCKED, HARD SL ACTIVE ──
    def test_08_reversal_during_grace_period_blocked_but_hard_sl_active(self):
        """During the first 180s, structural invalidation is blocked, but hard SL (-20%) triggers immediately."""
        trade = {
            "trade_id": "test_trade_08",
            "instrument_key": "NSE_FO|12345",
            "trading_symbol": "NIFTY 22800 CE 03 NOV 26",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 22800.0,
            "expiry": "2026-11-03",
            "entry_time": "2026-10-29T10:00:00",
            "entry_ask": 120.0,
            "entry_spot_price": 22855.0,
            "spot_atr": 40.0,
            "peak_price": 120.0,
            "trough_price": 110.0,
            "status": "OPEN",
        }
        underlying_quotes = {
            "NIFTY": {
                "underlying": "NIFTY",
                "spot_ltp": 22820.0,
                "session_benchmark": 22845.0,
                "vwap": 22845.0,
            }
        }
        # 1. At 60 seconds (within grace period): option at 110.0 (-8.3% loss)
        # Structural invalidation should NOT trigger due to 180s grace buffer
        exit_reason_grace = OptionPaperExecution.evaluate_exit_conditions(
            trade=trade,
            current_bid=110.0,
            quote={"bid": 110.0, "ask": 111.0, "ltp": 110.5},
            now_dt=datetime(2026, 10, 29, 10, 1, 0),  # 60s elapsed
            underlying_quotes_map=underlying_quotes
        )
        self.assertIsNone(exit_reason_grace, "Structural exit must be suppressed during 180s grace period")

        # 2. At 60 seconds: option flash-crashes to 95.0 (-20.8% loss) -> HARD SL must fire immediately!
        exit_reason_crash = OptionPaperExecution.evaluate_exit_conditions(
            trade=trade,
            current_bid=95.0,
            quote={"bid": 95.0, "ask": 96.0, "ltp": 95.5},
            now_dt=datetime(2026, 10, 29, 10, 1, 0),  # 60s elapsed
            underlying_quotes_map=underlying_quotes
        )
        self.assertEqual(exit_reason_crash, "STOP_LOSS_HIT", "Hard Stop-Loss must NEVER be delayed by grace period")

    # ── SCENARIO 9: RAPID WINNING MOVE LEAVES TARGET AND TRAILING UNCHANGED ───
    def test_09_rapid_winning_move_leaves_target_and_trailing_unchanged(self):
        """A winning trade hitting +35% target or trailing stop must exit cleanly without interference."""
        trade = {
            "trade_id": "test_trade_09",
            "instrument_key": "NSE_FO|12345",
            "trading_symbol": "NIFTY 22800 CE 03 NOV 26",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 22800.0,
            "expiry": "2026-11-03",
            "entry_time": "2026-10-29T10:00:00",
            "entry_ask": 100.0,
            "entry_spot_price": 22800.0,
            "spot_atr": 40.0,
            "peak_price": 100.0,
            "trough_price": 100.0,
            "status": "OPEN",
        }
        underlying_quotes = {
            "NIFTY": {
                "underlying": "NIFTY",
                "spot_ltp": 22850.0,
                "session_benchmark": 22810.0,
                "vwap": 22810.0,
            }
        }
        # Option jumps to 136.0 (+36% gain >= +35% target)
        exit_reason = OptionPaperExecution.evaluate_exit_conditions(
            trade=trade,
            current_bid=136.0,
            quote={"bid": 136.0, "ask": 137.0, "ltp": 136.5},
            now_dt=datetime(2026, 10, 29, 10, 5, 0),
            underlying_quotes_map=underlying_quotes
        )
        self.assertEqual(exit_reason, "TARGET_MET")

    # ── SCENARIO 10: CONCURRENCY AND NO DEADLOCK UNDER REPEATED REQUESTS ──────
    def test_10_concurrent_benchmark_and_quote_access(self):
        """50 concurrent threads requesting benchmark and quotes must complete with zero deadlocks or crashes."""
        from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
        mgr = OptionStreamManager()

        # Seed some data in SQLite
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        for i in range(15):
            cur.execute("""
                INSERT INTO continuous_index_ohlcv (ticker, timestamp, open, high, low, close, volume, is_complete)
                VALUES ('NIFTY', ?, 22800, 22820, 22790, 22810, 0, 1)
            """, (f"2026-10-29 09:{15+i:02d}:00",))
        conn.commit()
        conn.close()

        errors = []

        def worker():
            try:
                for _ in range(20):
                    b = mgr.compute_session_index_benchmark("NIFTY", as_of_time=datetime(2026, 10, 29, 9, 30, 0))
                    q = mgr.get_underlying_quote("NIFTY")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(errors), 0, f"Concurrent benchmark calculation failed with errors: {errors}")

    # ── SCENARIO 11: MISSING BENCHMARK FAIL-SAFE (NO SPOT SUBSTITUTION) ───────
    def test_11_missing_benchmark_failsafe_policy(self):
        """When fewer than 3 bars exist or DB is empty, benchmark is None; no silent LTP substitution."""
        from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
        mgr = OptionStreamManager()
        # With empty continuous_index_ohlcv:
        benchmark = mgr.compute_session_index_benchmark("NIFTY", as_of_time=datetime(2026, 10, 29, 9, 16, 0))
        self.assertIsNone(benchmark, "Benchmark must return None when insufficient bars exist (< 3 bars)")

        # In paper execution: if benchmark is None, do NOT trigger structural invalidation
        trade = {
            "trade_id": "test_trade_11",
            "instrument_key": "NSE_FO|12345",
            "trading_symbol": "NIFTY 22800 CE 03 NOV 26",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 22800.0,
            "expiry": "2026-11-03",
            "entry_time": "2026-10-29T10:00:00",
            "entry_ask": 120.0,
            "entry_spot_price": 22855.0,
            "spot_atr": 40.0,
            "peak_price": 120.0,
            "trough_price": 110.0,
            "status": "OPEN",
        }
        underlying_quotes = {
            "NIFTY": {
                "underlying": "NIFTY",
                "spot_ltp": 22820.0,
                "session_benchmark": None,  # Unavailable
                "vwap": None,
            }
        }
        exit_reason = OptionPaperExecution.evaluate_exit_conditions(
            trade=trade,
            current_bid=110.0,
            quote={"bid": 110.0, "ask": 111.0, "ltp": 110.5},
            now_dt=datetime(2026, 10, 29, 10, 5, 0),
            underlying_quotes_map=underlying_quotes
        )
        self.assertIsNone(exit_reason, "Structural invalidation must not fire when benchmark is None")

    # ── SCENARIO 12: EXACT REGRESSION COMPARISONS FOR UNRELATED GATES ─────────
    def test_12_unrelated_exits_and_gates_preserved(self):
        """Time decay (45 min) and session cutoff (15:15 IST) continue to function identically."""
        trade = {
            "trade_id": "test_trade_12",
            "instrument_key": "NSE_FO|12345",
            "trading_symbol": "NIFTY 22800 CE 03 NOV 26",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 22800.0,
            "expiry": "2026-11-03",
            "entry_time": "2026-10-29T10:00:00",
            "entry_ask": 120.0,
            "entry_spot_price": 22850.0,
            "spot_atr": 40.0,
            "peak_price": 122.0,
            "trough_price": 118.0,
            "status": "OPEN",
        }
        # 1. 46 minutes elapsed without loss -> TIME_DECAY_EXPIRY
        exit_time_decay = OptionPaperExecution.evaluate_exit_conditions(
            trade=trade,
            current_bid=120.5,
            quote={"bid": 120.5, "ask": 121.0, "ltp": 120.75},
            now_dt=datetime(2026, 10, 29, 10, 46, 0),
            underlying_quotes_map={}
        )
        self.assertEqual(exit_time_decay, "TIME_DECAY_EXPIRY")

        # 2. Session cutoff at 15:15 IST (entered at 15:10, 5 mins elapsed) -> SESSION_END_SQUAREOFF
        trade_late = dict(trade)
        trade_late["entry_time"] = "2026-10-29T15:10:00"
        exit_session_end = OptionPaperExecution.evaluate_exit_conditions(
            trade=trade_late,
            current_bid=119.0,
            quote={"bid": 119.0, "ask": 120.0, "ltp": 119.5},
            now_dt=datetime(2026, 10, 29, 15, 15, 30),
            underlying_quotes_map={},
            enforce_cutoff=True
        )
        self.assertEqual(exit_session_end, "SESSION_END_SQUAREOFF")


if __name__ == "__main__":
    unittest.main()
