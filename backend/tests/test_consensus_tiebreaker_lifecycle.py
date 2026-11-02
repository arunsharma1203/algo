"""
Unit Test Suite for Consensus Arena Economic Tiebreaker & Lifecycle Stability.

Validates:
1. Economic Tiebreaker Priority Hierarchy (R:R, Engine Spread, Sector, VWAP, Alphabetical)
2. Score Conviction Dominance (Outside epsilon=0.05%)
3. Complete Elimination of Database ID Dependency for Future Candidates
4. Absolute Historical Data Lock (Strict byte-for-byte replay of past trades)
5. Active Open Position Stability (Existing positions cannot be displaced or evicted)
6. 5-Slot Capacity Invariant Enforcement
"""

import unittest
import sqlite3
import tempfile
import os
import json
from unittest.mock import patch

from app.analytics.consensus_tiebreaker import (
    ConsensusTieBreaker,
    CandidateMetrics,
    compare_candidates_economically,
    HISTORICAL_TIEBREAKER_CUTOFF,
    EPSILON_SCORE
)
from app.analytics.consensus_game_engine import ConsensusGameEngine


class TestConsensusTiebreakerLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        self.conn = sqlite3.connect(self.db_path)
        self.cur = self.conn.cursor()

        # Seed full consensus schema
        self.cur.execute("""
            CREATE TABLE consensus_trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                target_price REAL NOT NULL,
                composite_score REAL NOT NULL,
                qlib_score REAL,
                legacy_score REAL,
                finbert_score REAL,
                risk_reward_ratio REAL,
                status TEXT DEFAULT 'OPEN',
                outcome TEXT,
                profit_pct REAL,
                exit_price REAL,
                exit_time TEXT,
                source TEXT DEFAULT 'CONSENSUS_DAEMON',
                position_type TEXT DEFAULT 'NOT_A_POSITION',
                details_json TEXT
            );
        """)
        self.conn.commit()
        ConsensusGameEngine.init_game_schema(self.conn)

        self.patcher = patch("app.analytics.consensus_game_engine.get_db_path", return_value=self.db_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_01_economic_tiebreaker_mothersson_vs_federalbnk(self):
        """
        Verify MOTHERSON vs FEDERALBNK tiebreak:
        When composite scores are identical (50.4), candidate with better R:R wins
        REGARDLESS of database ID.
        """
        # Case A: FEDERALBNK has better R:R (1.43 vs 1.25)
        mothersson_a = {
            "id": 10,
            "timestamp": "2026-09-25 10:00:00",
            "ticker": "MOTHERSON.NS",
            "composite_score": 50.4,
            "risk_reward_ratio": 1.25,
            "qlib_score": 50.0,
            "legacy_score": 50.7
        }
        federalbnk_a = {
            "id": 20,
            "timestamp": "2026-09-25 10:00:00",
            "ticker": "FEDERALBNK.NS",
            "composite_score": 50.4,
            "risk_reward_ratio": 1.43,
            "qlib_score": 50.0,
            "legacy_score": 50.6
        }
        winner_a = ConsensusTieBreaker.select_best_candidate([mothersson_a, federalbnk_a])
        self.assertEqual(winner_a["ticker"], "FEDERALBNK.NS")

        # Case B: INVERT R:R - MOTHERSON now has better R:R (1.80 vs 1.43)
        # Even though MOTHERSON has LOWER database ID (10 < 20), it MUST WIN!
        mothersson_b = dict(mothersson_a, risk_reward_ratio=1.80)
        federalbnk_b = dict(federalbnk_a, risk_reward_ratio=1.43)
        winner_b = ConsensusTieBreaker.select_best_candidate([mothersson_b, federalbnk_b])
        self.assertEqual(winner_b["ticker"], "MOTHERSON.NS")

    def test_02_score_conviction_dominance_outside_epsilon(self):
        """Verify that score conviction dominance holds when difference exceeds 0.05%."""
        high_score = {
            "id": 1,
            "timestamp": "2026-09-25 10:00:00",
            "ticker": "HIGH.NS",
            "composite_score": 50.5,
            "risk_reward_ratio": 1.25
        }
        low_score_better_rr = {
            "id": 2,
            "timestamp": "2026-09-25 10:00:00",
            "ticker": "LOW.NS",
            "composite_score": 50.4,
            "risk_reward_ratio": 3.0
        }
        # 50.5 vs 50.4 differs by 0.1 > epsilon (0.05), so HIGH.NS wins on conviction
        winner = ConsensusTieBreaker.select_best_candidate([low_score_better_rr, high_score])
        self.assertEqual(winner["ticker"], "HIGH.NS")

    def test_03_engine_agreement_spread_tiebreaker(self):
        """When scores and R:R are tied, candidate with smaller Qlib-Legacy spread wins."""
        tight_spread = {
            "id": 1,
            "timestamp": "2026-09-25 10:00:00",
            "ticker": "TIGHT.NS",
            "composite_score": 50.4,
            "risk_reward_ratio": 1.5,
            "qlib_score": 50.3,
            "legacy_score": 50.5  # spread = 0.2
        }
        wide_spread = {
            "id": 2,
            "timestamp": "2026-09-25 10:00:00",
            "ticker": "WIDE.NS",
            "composite_score": 50.4,
            "risk_reward_ratio": 1.5,
            "qlib_score": 50.0,
            "legacy_score": 51.5  # spread = 1.5
        }
        winner = ConsensusTieBreaker.select_best_candidate([wide_spread, tight_spread])
        self.assertEqual(winner["ticker"], "TIGHT.NS")

    def test_04_sector_momentum_directional_tiebreaker(self):
        """When scores, R:R, and spreads are tied, directional sector momentum decides."""
        # For BUY: higher sector % from open wins
        aligned_sector = {
            "id": 1,
            "timestamp": "2026-09-25 10:00:00",
            "ticker": "BULL_ALIGNED.NS",
            "direction": "BUY",
            "composite_score": 50.4,
            "risk_reward_ratio": 1.5,
            "qlib_score": 50.0,
            "legacy_score": 50.0,
            "sector_pct_from_open": 1.2
        }
        weak_sector = {
            "id": 2,
            "timestamp": "2026-09-25 10:00:00",
            "ticker": "BULL_WEAK.NS",
            "direction": "BUY",
            "composite_score": 50.4,
            "risk_reward_ratio": 1.5,
            "qlib_score": 50.0,
            "legacy_score": 50.0,
            "sector_pct_from_open": 0.1
        }
        winner = ConsensusTieBreaker.select_best_candidate([weak_sector, aligned_sector])
        self.assertEqual(winner["ticker"], "BULL_ALIGNED.NS")

    def test_05_deterministic_alphabetical_tiebreaker(self):
        """When all metrics are identical, deterministic alphabetical sort decides (0% ID dependence)."""
        cand_a = {
            "id": 999,
            "timestamp": "2026-09-25 10:00:00",
            "ticker": "ALPHA.NS",
            "composite_score": 50.4,
            "risk_reward_ratio": 1.5
        }
        cand_b = {
            "id": 1,
            "timestamp": "2026-09-25 10:00:00",
            "ticker": "BETA.NS",
            "composite_score": 50.4,
            "risk_reward_ratio": 1.5
        }
        # ALPHA should win alphabetically regardless of whether cand_b has ID 1 and cand_a has ID 999
        winner = ConsensusTieBreaker.select_best_candidate([cand_b, cand_a])
        self.assertEqual(winner["ticker"], "ALPHA.NS")

    def test_06_historical_data_lock_byte_for_byte(self):
        """
        Verify Absolute Historical Data Lock:
        Trades timestamped prior to HISTORICAL_TIEBREAKER_CUTOFF preserve their exact
        historical ID tiebreak behavior byte-for-byte.
        """
        hist_a = {
            "id": 100,
            "timestamp": "2026-09-24 10:00:00",  # Prior to cutoff
            "ticker": "HIST_A.NS",
            "composite_score": 50.4,
            "risk_reward_ratio": 1.25
        }
        hist_b = {
            "id": 200,
            "timestamp": "2026-09-24 10:00:00",  # Prior to cutoff
            "ticker": "HIST_B.NS",
            "composite_score": 50.4,
            "risk_reward_ratio": 1.0  # Even though RR is lower, historical ID 200 > 100 wins!
        }
        hist_winner = ConsensusTieBreaker.select_best_candidate([hist_a, hist_b], historical_id_sign=1)
        self.assertEqual(hist_winner["ticker"], "HIST_B.NS")
        self.assertEqual(hist_winner["id"], 200)

    def test_07_slot_capacity_5_invariants(self):
        """
        Verify that exactly 5 concurrently active positions are permitted in the Arena,
        and new candidates cannot displace active holdings.
        """
        # Seed 5 active trades
        for i in range(1, 6):
            self.cur.execute("""
                INSERT INTO consensus_trade_history (
                    id, timestamp, ticker, symbol, strategy, direction,
                    entry_price, stop_loss, target_price, composite_score,
                    risk_reward_ratio, status, source
                ) VALUES (?, '2026-09-25 09:30:00', ?, ?, 'INTRADAY', 'BUY',
                          100.0, 98.0, 104.0, 60.0, 2.0, 'OPEN', 'MANUAL')
            """, (i, f"TICKER_{i}.NS", f"TICKER_{i}"))

        # Seed 6th candidate with high score (80.0) at a later time
        self.cur.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                risk_reward_ratio, status, source
            ) VALUES (6, '2026-09-25 10:00:00', 'TICKER_6.NS', 'TICKER_6', 'INTRADAY', 'BUY',
                      200.0, 195.0, 210.0, 80.0, 2.0, 'OPEN', 'MANUAL')
        """)
        self.conn.commit()

        # Run portfolio sync
        state = ConsensusGameEngine.deploy_capital(
            initial_capital=100000.0,
            session_id="test_capacity_session",
            conn=self.conn
        )
        self.assertEqual(state["open_count"], 5)

        # TICKER_6 should NOT be open because all 5 slots were occupied by earlier positions
        open_tickers = [p["ticker"] for p in state["open_positions"]]
        self.assertNotIn("TICKER_6.NS", open_tickers)
        for i in range(1, 6):
            self.assertIn(f"TICKER_{i}.NS", open_tickers)


if __name__ == "__main__":
    unittest.main()

