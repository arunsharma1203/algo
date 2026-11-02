"""
Comprehensive Bias, Bad Logic & Ethical Flaw Hunting Test Suite.

Audits every critical invariant for live trading readiness:
1. Temporal Precedence & Look-Behind Bias (Stale candle replay prevention)
2. Multi-Bar Chronological Precedence (No out-of-order execution or future recovery bias)
3. Wide-Bar Dual Breach Conservatism (Fail-closed Stop Loss precedence on volatile bars)
4. Future Timestamp Inversion Guards (exit_time >= entry_time invariant)
5. Selection Purity & Tiebreaker Invariance (Point-in-time features only, zero future leakage)
6. Capital, Cash Non-Negativity & Whole Share Sizing Invariants
"""

import os
import math
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from app.analytics.consensus_tracker import ConsensusTracker
from app.analytics.consensus_game_engine import ConsensusGameEngine
from app.analytics.consensus_tiebreaker import (
    ConsensusTieBreaker,
    CandidateMetrics,
    compare_candidates_economically,
    HISTORICAL_TIEBREAKER_CUTOFF
)
from app.api.ml_history import ensure_ml_table


class TestHuntingBiasAndEthicalFlaws(unittest.TestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        # Isolate database paths completely to temporary SQLite file
        self.patchers = [
            patch("app.analytics.consensus_tracker.get_db_path", return_value=self.db_path),
            patch("app.analytics.consensus_game_engine.get_db_path", return_value=self.db_path),
            patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path),
            patch("app.analytics.consensus_dynamic_guard.get_db_path", return_value=self.db_path),
            patch("app.data.market_provider.get_bulk_live_quotes_with_meta", return_value={}),
            patch("app.analytics.consensus_tracker.send_telegram_message", return_value=True),
        ]
        for p in self.patchers:
            p.start()

        conn = sqlite3.connect(self.db_path)
        ensure_ml_table()
        ConsensusTracker.init_consensus_tracking_schema(conn)
        ConsensusGameEngine.init_game_schema(conn)

        # Create ohlcv and app_settings tables
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ohlcv (
                ticker TEXT,
                timeframe TEXT,
                date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.commit()
        conn.close()

        # Reset tracker throttle
        ConsensusTracker._last_reconcile_time = 0.0

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    # =========================================================================
    # GROUP 1: TEMPORAL PRECEDENCE & LOOK-BEHIND BIAS HUNTING
    # =========================================================================

    def test_A1_mid_day_entry_strictly_ignores_prior_morning_candles(self):
        """
        HUNT: A mid-day trade entered at 13:01:31 must NEVER exit on a morning candle dip.
        Repercussion if flawed: Mid-day trades prematurely close seconds after entry
        due to stale morning price drops that happened hours before signal generation.
        """
        trade_date = "2026-09-28"
        trade = {
            "timestamp": f"{trade_date} 13:01:31",
            "ticker": "ACMESOLAR.NS",
            "direction": "BUY",
            "blended_entry": 437.0,
            "blended_sl": 431.9,
            "blended_tp": 447.2,
            "composite_score": 86.0
        }
        ConsensusTracker.save_consensus_trade(trade, strategy="INTRADAY")

        # Insert earlier morning candles (09:15 to 12:45) that hit both SL and TP in the morning!
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES 
            ('ACMESOLAR.NS', '15m', '{trade_date} 09:30:00', 440.0, 448.0, 439.0, 445.0, 50000),
            ('ACMESOLAR.NS', '15m', '{trade_date} 09:45:00', 435.0, 436.0, 431.0, 432.0, 60000),
            ('ACMESOLAR.NS', '15m', '{trade_date} 12:45:00', 436.0, 437.5, 435.5, 436.8, 20000);
        """)
        conn.commit()
        conn.close()

        # Run reconciliation
        sim_now = datetime(2026, 9, 28, 13, 0, 0)
        reconciled = ConsensusTracker.reconcile_open_consensus_trades(now=sim_now, min_interval_sec=0.0)
        self.assertEqual(reconciled, 0, "Look-behind bias detected! Trade closed on past morning candles.")

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT status, outcome FROM consensus_trade_history WHERE ticker='ACMESOLAR.NS'").fetchone()
        conn.close()
        self.assertEqual(row[0], "OPEN")
        self.assertIsNone(row[1])

    def test_A2_multiple_trades_temporal_isolation(self):
        """
        HUNT: Two trades for different tickers entered at 10:00 and 14:00.
        The 14:00 trade must not consume candles from 10:15 that hit target.
        """
        trade_date = "2026-09-28"
        t1 = {
            "timestamp": f"{trade_date} 10:00:00",
            "ticker": "EARLY.NS",
            "direction": "BUY",
            "blended_entry": 100.0,
            "blended_sl": 95.0,
            "blended_tp": 110.0,
            "composite_score": 80.0
        }
        t2 = {
            "timestamp": f"{trade_date} 14:00:00",
            "ticker": "LATE.NS",
            "direction": "BUY",
            "blended_entry": 100.0,
            "blended_sl": 95.0,
            "blended_tp": 110.0,
            "composite_score": 80.0
        }
        ConsensusTracker.save_consensus_trade(t1, strategy="INTRADAY")
        ConsensusTracker.save_consensus_trade(t2, strategy="INTRADAY")

        conn = sqlite3.connect(self.db_path)
        conn.execute(f"""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES 
            ('EARLY.NS', '15m', '{trade_date} 10:15:00', 101.0, 111.0, 100.0, 109.0, 10000),
            ('LATE.NS', '15m', '{trade_date} 10:15:00', 101.0, 111.0, 100.0, 109.0, 10000);
        """)
        conn.commit()
        conn.close()

        sim_now = datetime(2026, 9, 28, 15, 0, 0)
        reconciled = ConsensusTracker.reconcile_open_consensus_trades(now=sim_now, min_interval_sec=0.0)
        self.assertEqual(reconciled, 1)

        conn = sqlite3.connect(self.db_path)
        early_row = conn.execute("SELECT status, outcome FROM consensus_trade_history WHERE ticker='EARLY.NS'").fetchone()
        late_row = conn.execute("SELECT status, outcome FROM consensus_trade_history WHERE ticker='LATE.NS'").fetchone()
        conn.close()

        self.assertEqual(early_row[0], "CLOSED")
        self.assertEqual(early_row[1], "TARGET MET")
        self.assertEqual(late_row[0], "OPEN")
        self.assertNotIn(late_row[1], ("TARGET MET", "SL HIT"))

    # =========================================================================
    # GROUP 2: MULTI-BAR CHRONOLOGICAL PRECEDENCE HUNTING
    # =========================================================================

    def test_B1_chronological_sl_in_bar1_terminates_before_tp_in_bar3(self):
        """
        HUNT: Trade enters at 10:00.
        Bar 1 (10:00): Price dumps and touches Stop Loss.
        Bar 2 (10:15): Price chops.
        Bar 3 (10:30): Price rallies and touches Target.
        FLAW TO HUNT: Naive min/max aggregation evaluates TP first across all bars,
        falsely awarding TARGET MET despite being stopped out in Bar 1.
        Repercussion if flawed: Produces phantom wins and catastrophic live drawdowns.
        """
        trade_date = "2026-09-28"
        trade = {
            "timestamp": f"{trade_date} 10:00:00",
            "ticker": "CHRONO.NS",
            "direction": "BUY",
            "blended_entry": 1000.0,
            "blended_sl": 980.0,
            "blended_tp": 1040.0,
            "composite_score": 85.0
        }
        ConsensusTracker.save_consensus_trade(trade, strategy="INTRADAY")

        conn = sqlite3.connect(self.db_path)
        conn.execute(f"""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES 
            ('CHRONO.NS', '15m', '{trade_date} 10:00:00', 995.0, 1002.0, 975.0, 985.0, 10000),
            ('CHRONO.NS', '15m', '{trade_date} 10:15:00', 985.0, 1010.0, 982.0, 1005.0, 10000),
            ('CHRONO.NS', '15m', '{trade_date} 10:30:00', 1005.0, 1045.0, 1000.0, 1042.0, 15000);
        """)
        conn.commit()
        conn.close()

        sim_now = datetime(2026, 9, 28, 15, 0, 0)
        reconciled = ConsensusTracker.reconcile_open_consensus_trades(now=sim_now, min_interval_sec=0.0)
        self.assertEqual(reconciled, 1)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT status, outcome, exit_price, exit_time FROM consensus_trade_history WHERE ticker='CHRONO.NS'").fetchone()
        conn.close()

        self.assertEqual(row[0], "CLOSED")
        self.assertEqual(row[1], "SL HIT", "Out-of-order execution flaw! Bar 3 rally should not overwrite Bar 1 SL.")
        self.assertEqual(row[2], 980.0)
        self.assertIn("10:00:00", str(row[3]))

    def test_B2_short_chronological_precedence(self):
        """
        HUNT: SELL trade.
        Bar 1 breaches Stop Loss on high.
        Bar 2 drops to Target Price.
        Must stop out on Bar 1.
        """
        trade_date = "2026-09-28"
        trade = {
            "timestamp": f"{trade_date} 10:00:00",
            "ticker": "SHORT_CHRONO.NS",
            "direction": "SELL",
            "blended_entry": 500.0,
            "blended_sl": 510.0,
            "blended_tp": 480.0,
            "composite_score": 85.0
        }
        ConsensusTracker.save_consensus_trade(trade, strategy="INTRADAY")

        conn = sqlite3.connect(self.db_path)
        conn.execute(f"""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES 
            ('SHORT_CHRONO.NS', '15m', '{trade_date} 10:00:00', 501.0, 512.0, 498.0, 508.0, 10000),
            ('SHORT_CHRONO.NS', '15m', '{trade_date} 10:15:00', 508.0, 509.0, 478.0, 479.0, 20000);
        """)
        conn.commit()
        conn.close()

        sim_now = datetime(2026, 9, 28, 15, 0, 0)
        reconciled = ConsensusTracker.reconcile_open_consensus_trades(now=sim_now, min_interval_sec=0.0)
        self.assertEqual(reconciled, 1)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT status, outcome, exit_price FROM consensus_trade_history WHERE ticker='SHORT_CHRONO.NS'").fetchone()
        conn.close()

        self.assertEqual(row[0], "CLOSED")
        self.assertEqual(row[1], "SL HIT")
        self.assertEqual(row[2], 510.0)

    # =========================================================================
    # GROUP 3: WIDE-BAR DUAL BREACH CONSERVATISM
    # =========================================================================

    def test_C1_wide_candle_both_sl_and_tp_touched_prioritizes_sl(self):
        """
        HUNT: Single volatile 15m candle where High >= TP AND Low <= SL.
        FLAW TO HUNT: Assuming Target Met when path of prices is unobserved inside the bar.
        Ethical/Quant Invariant: Fail-closed risk management MUST prioritize SL HIT.
        """
        trade_date = "2026-09-28"
        trade = {
            "timestamp": f"{trade_date} 11:00:00",
            "ticker": "VOLATILE.NS",
            "direction": "BUY",
            "blended_entry": 100.0,
            "blended_sl": 96.0,
            "blended_tp": 108.0,
            "composite_score": 82.0
        }
        ConsensusTracker.save_consensus_trade(trade, strategy="INTRADAY")

        conn = sqlite3.connect(self.db_path)
        conn.execute(f"""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES ('VOLATILE.NS', '15m', '{trade_date} 11:00:00', 99.0, 110.0, 94.0, 101.0, 50000);
        """)
        conn.commit()
        conn.close()

        sim_now = datetime(2026, 9, 28, 15, 0, 0)
        reconciled = ConsensusTracker.reconcile_open_consensus_trades(now=sim_now, min_interval_sec=0.0)
        self.assertEqual(reconciled, 1)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT status, outcome, exit_price FROM consensus_trade_history WHERE ticker='VOLATILE.NS'").fetchone()
        conn.close()

        self.assertEqual(row[0], "CLOSED")
        self.assertEqual(row[1], "SL HIT", "Optimism bias detected! Volatile bar touching both SL and TP must fail-closed as SL HIT.")
        self.assertEqual(row[2], 96.0)

    def test_C2_ratchet_breakeven_then_stopout_in_same_bar(self):
        """
        HUNT: Bar 1 advances into profit (+0.70%) triggering breakeven ratchet (+0.10%).
        Bar 2 pulls back and breaches the newly ratcheted breakeven SL.
        Outcome must be BREAKEVEN HIT (+0.10%), not SL HIT or TARGET MET.
        """
        trade_date = "2026-09-28"
        trade = {
            "timestamp": f"{trade_date} 11:00:00",
            "ticker": "RATCHET.NS",
            "direction": "BUY",
            "blended_entry": 1000.0,
            "blended_sl": 980.0,
            "blended_tp": 1050.0,
            "composite_score": 85.0
        }
        ConsensusTracker.save_consensus_trade(trade, strategy="INTRADAY")

        # Bar 1 (11:00): High = 1010 (+1.0% >= 0.70% ratchet trigger). Low = 1002 (safe above 980).
        # Bar 2 (11:15): Low = 1000.50 (below ratcheted SL 1001.0).
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES 
            ('RATCHET.NS', '15m', '{trade_date} 11:00:00', 1002.0, 1010.0, 1002.0, 1008.0, 30000),
            ('RATCHET.NS', '15m', '{trade_date} 11:15:00', 1008.0, 1009.0, 1000.5, 1002.0, 20000);
        """)
        conn.commit()
        conn.close()

        sim_now = datetime(2026, 9, 28, 15, 0, 0)
        reconciled = ConsensusTracker.reconcile_open_consensus_trades(now=sim_now, min_interval_sec=0.0)
        self.assertEqual(reconciled, 1)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT status, outcome, exit_price, stop_loss FROM consensus_trade_history WHERE ticker='RATCHET.NS'").fetchone()
        conn.close()

        self.assertEqual(row[0], "CLOSED")
        self.assertEqual(row[1], "BREAKEVEN HIT")
        self.assertEqual(row[2], 1001.0)
        self.assertEqual(row[3], 1001.0)

    # =========================================================================
    # GROUP 4: FUTURE TIMESTAMP INVERSION GUARDS
    # =========================================================================

    def test_D1_exit_time_is_strictly_greater_or_equal_to_entry_time(self):
        """
        HUNT: exit_time < entry_time indicates backwards time travel (causal inversion).
        Mathematically enforces exit_time >= timestamp.
        """
        trade_date = "2026-09-28"
        trade = {
            "timestamp": f"{trade_date} 13:07:45",
            "ticker": "TIMEGUARD.NS",
            "direction": "BUY",
            "blended_entry": 500.0,
            "blended_sl": 490.0,
            "blended_tp": 520.0,
            "composite_score": 85.0
        }
        ConsensusTracker.save_consensus_trade(trade, strategy="INTRADAY")

        # Candle date is 13:00:00
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES ('TIMEGUARD.NS', '15m', '{trade_date} 13:00:00', 500.0, 525.0, 498.0, 522.0, 20000);
        """)
        conn.commit()
        conn.close()

        sim_now = datetime(2026, 9, 28, 15, 0, 0)
        reconciled = ConsensusTracker.reconcile_open_consensus_trades(now=sim_now, min_interval_sec=0.0)
        self.assertEqual(reconciled, 1)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT timestamp, exit_time FROM consensus_trade_history WHERE ticker='TIMEGUARD.NS'").fetchone()
        conn.close()

        entry_ts = str(row[0])
        exit_ts = str(row[1])
        self.assertGreaterEqual(exit_ts, entry_ts, f"Temporal inversion! exit_time ({exit_ts}) is before entry ({entry_ts})")

    def test_D2_future_unformed_candles_not_queried(self):
        """
        HUNT: Reconciliation must only query completed or currently active candles,
        never future session dates.
        """
        trade_date = "2026-09-28"
        trade = {
            "timestamp": f"{trade_date} 10:00:00",
            "ticker": "NOFUTURE.NS",
            "direction": "BUY",
            "blended_entry": 200.0,
            "blended_sl": 190.0,
            "blended_tp": 220.0,
            "composite_score": 85.0
        }
        ConsensusTracker.save_consensus_trade(trade, strategy="INTRADAY")

        # Insert candles for next day (future)
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES ('NOFUTURE.NS', '15m', '2026-09-29 10:00:00', 200.0, 230.0, 198.0, 225.0, 20000);
        """)
        conn.commit()
        conn.close()

        # Simulate reconcile called during trade_date (now = 2026-09-28 11:00:00)
        simulated_now = datetime(2026, 9, 28, 11, 0, 0)
        reconciled = ConsensusTracker.reconcile_open_consensus_trades(now=simulated_now, min_interval_sec=0.0)
        self.assertEqual(reconciled, 0, "Future leakage! Future date candle was used to reconcile current trade.")

    # =========================================================================
    # GROUP 5: SELECTION PURITY & TIEBREAKER INVARIANCE
    # =========================================================================

    def test_E1_tiebreaker_references_only_point_in_time_fields(self):
        """
        HUNT: ConsensusTieBreaker must NEVER inspect future outcome, profit_pct, or exit_price.
        """
        candidate_a = {
            "id": 1,
            "timestamp": "2026-09-28 10:15:00",
            "ticker": "STOCK_A.NS",
            "direction": "BUY",
            "entry": 100.0,
            "sl": 98.0,
            "tp": 104.0,
            "score": 85.0,
            "risk_reward_ratio": 2.0,
            "qlib_score": 85.0,
            "legacy_score": 85.0,
            "outcome": "SL HIT",       # Future loss
            "profit_pct": -2.0
        }
        candidate_b = {
            "id": 2,
            "timestamp": "2026-09-28 10:15:00",
            "ticker": "STOCK_B.NS",
            "direction": "BUY",
            "entry": 100.0,
            "sl": 98.0,
            "tp": 103.0,
            "score": 85.0,
            "risk_reward_ratio": 1.5,
            "qlib_score": 85.0,
            "legacy_score": 85.0,
            "outcome": "TARGET MET",   # Future win
            "profit_pct": 3.0
        }

        # candidate_a has RR 2.0 vs candidate_b RR 1.5. candidate_a must win despite future loss!
        winner = ConsensusTieBreaker.select_best_candidate([candidate_a, candidate_b])
        self.assertEqual(winner["ticker"], "STOCK_A.NS", "Look-ahead selection leakage! Future outcome influenced candidate tiebreaker.")

    def test_E2_tiebreaker_order_idempotence(self):
        """
        HUNT: Selection must be deterministic and invariant to input list ordering.
        """
        cands = [
            {"id": 10, "timestamp": "2026-09-28 11:30:00", "ticker": "TICKER_X", "direction": "BUY", "entry": 100, "sl": 98, "tp": 104, "score": 80.0, "risk_reward_ratio": 2.0},
            {"id": 11, "timestamp": "2026-09-28 11:30:00", "ticker": "TICKER_Y", "direction": "BUY", "entry": 100, "sl": 98, "tp": 104, "score": 80.0, "risk_reward_ratio": 2.0},
            {"id": 12, "timestamp": "2026-09-28 11:30:00", "ticker": "TICKER_Z", "direction": "BUY", "entry": 100, "sl": 98, "tp": 104, "score": 80.0, "risk_reward_ratio": 2.0},
        ]
        # In tiebreaker hierarchy: scores equal, RR equal, spread equal -> Alphabetical wins (TICKER_X)
        win1 = ConsensusTieBreaker.select_best_candidate(cands)
        win2 = ConsensusTieBreaker.select_best_candidate(list(reversed(cands)))
        self.assertEqual(win1["ticker"], "TICKER_X")
        self.assertEqual(win2["ticker"], "TICKER_X")

    def test_E3_timed_scan_milestone_tolerance_synchronization(self):
        """
        HUNT: Verify tracker and engine use the exact same 45-minute forward tolerance.
        A batch from 11:32 must be accepted for the 11:30 milestone (diff = 2m <= 45m).
        A batch from 12:20 must be rejected (diff = 50m > 45m).
        """
        conn = sqlite3.connect(self.db_path)
        # Insert a valid 11:32 trade and an invalid 12:20 trade
        conn.execute("""
            INSERT INTO consensus_trade_history (
                timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                status, outcome, source, position_type
            ) VALUES 
            ('2026-09-28 11:32:00', 'VALID_M1130.NS', 'VALID_M1130', 'INTRADAY', 'BUY', 500, 490, 520, 85, 'OPEN', NULL, 'AUTOPILOT', 'NOT_A_POSITION'),
            ('2026-09-28 12:20:00', 'STALE_M1130.NS', 'STALE_M1130', 'INTRADAY', 'BUY', 500, 490, 520, 95, 'OPEN', NULL, 'AUTOPILOT', 'NOT_A_POSITION');
        """)
        conn.commit()
        conn.close()

        # Test Arena Engine sync in TIMED_ANCHOR_CONFLUENCE mode
        ConsensusGameEngine.deploy_capital(100000.0, session_id="timed_hunt", trade_selection_mode="TIMED_ANCHOR_CONFLUENCE")
        state = ConsensusGameEngine.get_portfolio_state(session_id="timed_hunt")

        open_tickers = [p["ticker"] for p in state["open_positions"]]
        self.assertIn("VALID_M1130.NS", open_tickers, "Valid timed scan batch within 45m tolerance was not picked!")
        self.assertNotIn("STALE_M1130.NS", open_tickers, "Stale scan batch (>45m) was improperly accepted!")

    # =========================================================================
    # GROUP 6: CAPITAL, CASH NON-NEGATIVITY & SIZING INVARIANTS
    # =========================================================================

    def test_F1_whole_share_integer_sizing_invariant(self):
        """
        HUNT: Brokerages only accept integer shares for equity delivery/intraday.
        Fractional shares (e.g. 15.34 shares) cause broker API rejection (400 Bad Request).
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO consensus_trade_history (
                timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                status, outcome, source, position_type
            ) VALUES ('2026-09-28 09:30:00', 'EXPENSIVE.NS', 'EXPENSIVE', 'INTRADAY', 'BUY', 3745.85, 3700.0, 3850.0, 90, 'OPEN', NULL, 'AUTOPILOT', 'NOT_A_POSITION');
        """)
        conn.commit()
        conn.close()

        ConsensusGameEngine.deploy_capital(100000.0, session_id="shares_hunt", trade_selection_mode="HIGHEST_CONFLUENCE_ONLY")
        state = ConsensusGameEngine.get_portfolio_state(session_id="shares_hunt")

        for pos in state["open_positions"]:
            shares = pos["shares"]
            self.assertIsInstance(shares, int)
            self.assertEqual(shares, math.floor(shares))
            self.assertGreaterEqual(shares, 1)

    def test_F2_cash_non_negativity_and_overdraft_block(self):
        """
        HUNT: Capital allocation must never drive account cash negative.
        If cost exceeds available cash, sizing must reject the trade.
        """
        conn = sqlite3.connect(self.db_path)
        # Insert stock whose single share costs ₹150,000 when account only has ₹100,000
        conn.execute("""
            INSERT INTO consensus_trade_history (
                timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                status, outcome, source, position_type
            ) VALUES ('2026-09-28 09:30:00', 'MRF_MEGA.NS', 'MRF_MEGA', 'INTRADAY', 'BUY', 150000.0, 148000.0, 155000.0, 95, 'OPEN', NULL, 'AUTOPILOT', 'NOT_A_POSITION');
        """)
        conn.commit()
        conn.close()

        ConsensusGameEngine.deploy_capital(100000.0, session_id="cash_guard", trade_selection_mode="HIGHEST_CONFLUENCE_ONLY")
        state = ConsensusGameEngine.get_portfolio_state(session_id="cash_guard")

        self.assertGreaterEqual(state["cash"], 0.0)
        self.assertEqual(len(state["open_positions"]), 0, "Account overdrawn! Single share exceeding cash was allowed.")

    def test_F3_slot_capacity_strictly_enforced_at_max_positions(self):
        """
        HUNT: Account with max_positions = 5 must reject the 6th trade
        and record outcome = 'REJECTED_RISK' without allocating capital.
        """
        conn = sqlite3.connect(self.db_path)
        for i in range(1, 8):
            conn.execute(f"""
                INSERT INTO consensus_trade_history (
                    timestamp, ticker, symbol, strategy, direction,
                    entry_price, stop_loss, target_price, composite_score,
                    status, outcome, source, position_type
                ) VALUES ('2026-09-28 09:30:00', 'STOCK_{i}.NS', 'STOCK_{i}', 'INTRADAY', 'BUY', 100.0, 95.0, 110.0, {80 + i}, 'OPEN', NULL, 'AUTOPILOT', 'NOT_A_POSITION');
            """)
        conn.commit()
        conn.close()

        ConsensusGameEngine.deploy_capital(100000.0, session_id="slot_guard", trade_selection_mode="ALL_QUALIFIED")
        state = ConsensusGameEngine.get_portfolio_state(session_id="slot_guard")

        self.assertLessEqual(len(state["open_positions"]), 5, "Slot capacity overflow! More than 5 positions were opened.")

        # Invariant: 6th and 7th trades must have REJECTED_RISK recorded in consensus_trade_history
        conn = sqlite3.connect(self.db_path)
        rejected = conn.execute("SELECT COUNT(*) FROM consensus_trade_history WHERE outcome = 'REJECTED_RISK'").fetchone()[0]
        conn.close()
        self.assertEqual(rejected, 2, "Overflow trades were not auditable marked as REJECTED_RISK.")

    def test_F4_cooldown_invariant_prevents_immediate_reentry(self):
        """
        HUNT: After a position is closed, immediate re-entry on the same ticker
        within 15 minutes (< 900s) must be blocked to prevent churn friction.
        """
        conn = sqlite3.connect(self.db_path)
        # Insert trade 1: enters 10:00, closed at 10:05
        # Insert trade 2: same ticker, arrives at 10:10 (5m elapsed < 15m cooldown)
        conn.execute("""
            INSERT INTO consensus_trade_history (
                timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                status, outcome, exit_price, exit_time, source, position_type
            ) VALUES 
            ('2026-09-28 10:00:00', 'CHURN.NS', 'CHURN', 'INTRADAY', 'BUY', 100, 95, 110, 85, 'CLOSED', 'TARGET MET', 110, '2026-09-28 10:05:00', 'AUTOPILOT', 'NOT_A_POSITION'),
            ('2026-09-28 10:10:00', 'CHURN.NS', 'CHURN', 'INTRADAY', 'BUY', 110, 105, 120, 88, 'OPEN', NULL, NULL, NULL, 'AUTOPILOT', 'NOT_A_POSITION');
        """)
        conn.commit()
        conn.close()

        ConsensusGameEngine.deploy_capital(100000.0, session_id="cooldown_hunt", trade_selection_mode="ALL_QUALIFIED")
        state = ConsensusGameEngine.get_portfolio_state(session_id="cooldown_hunt")

        active_tickers = [p["ticker"] for p in state["open_positions"]]
        self.assertNotIn("CHURN.NS", active_tickers, "Cooldown violation! Immediate re-entry within 15 minutes was permitted.")

    def test_F5_duplicate_active_position_guard(self):
        """
        HUNT: Multiple concurrent signals for the same ticker must never open
        duplicate overlapping positions in the portfolio.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO consensus_trade_history (
                timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                status, outcome, source, position_type
            ) VALUES 
            ('2026-09-28 09:30:00', 'DUPLICATE.NS', 'DUPLICATE', 'INTRADAY', 'BUY', 200, 195, 210, 85, 'OPEN', NULL, 'AUTOPILOT', 'NOT_A_POSITION'),
            ('2026-09-28 09:45:00', 'DUPLICATE.NS', 'DUPLICATE', 'INTRADAY', 'BUY', 202, 197, 212, 88, 'OPEN', NULL, 'AUTOPILOT', 'NOT_A_POSITION');
        """)
        conn.commit()
        conn.close()

        ConsensusGameEngine.deploy_capital(100000.0, session_id="duplicate_hunt", trade_selection_mode="ALL_QUALIFIED")
        state = ConsensusGameEngine.get_portfolio_state(session_id="duplicate_hunt")

        dup_positions = [p for p in state["open_positions"] if p["ticker"] == "DUPLICATE.NS"]
        self.assertEqual(len(dup_positions), 1, "Duplicate active position created on the same ticker!")


if __name__ == "__main__":
    unittest.main()
