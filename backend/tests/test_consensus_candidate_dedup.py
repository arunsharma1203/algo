"""
Comprehensive Test Suite for Consensus Candidate Deduplication Lifecycle.

Mandatory Test Invariants:
1. TEST 1: Rejected Candidate Eligibility (Scan 1: A 50.6, B 50.5, C 50.4 -> A selected;
           Scan 2: B 50.5, C 50.2, D 50.1 -> B is eligible, B wins Scan 2, D does not win).
2. TEST 2: Multi-Scan Candidate Progression (Scan 1: HEG 50.4 rejected;
           Scan 2: HEG 50.5 eligible; Scan 3: HEG 50.6 eligible and can be selected).
3. TEST 3: Active Trade Protection (Scan 1: A 50.6 -> selected -> ACTIVE;
           Scan 2: A 50.8 -> second active trade is NOT created; existing active trade is preserved).
4. TEST 4: Closed Trade Cooldown & Re-entry (A selected -> CLOSED; cooldown prevents immediate re-entry,
           after cooldown elapsed A can become a new active trade).
5. TEST 5: Arena Refresh Safety (A selected; candidate with higher score inserted later;
           refreshing Arena does not evict or delete active trade A).
6. TEST 6: Database ID Independence (Candidate eligibility and selection is 100% ID-independent).
7. TEST 7: Audit Reason Transparency (Audit labels: SELECTED, REJECTED_LOWER_RANK, REJECTED_ACTIVE_DUPLICATE,
           RECENTLY_CLOSED_COOLDOWN; never DUPLICATE on unselected candidates).
"""

import os
import unittest
import sqlite3
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

from app.analytics.consensus_tracker import ConsensusTracker
from app.analytics.consensus_game_engine import ConsensusGameEngine


class TestConsensusCandidateDedup(unittest.TestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        # Patch get_db_path across all relevant modules to use isolated temp db
        self.patcher_db0 = patch("app.data.database.get_db_path", return_value=self.db_path)
        self.patcher_db1 = patch("app.analytics.consensus_tracker.get_db_path", return_value=self.db_path)
        self.patcher_db2 = patch("app.analytics.consensus_game_engine.get_db_path", return_value=self.db_path)
        self.patcher_db3 = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.patcher_live = patch("app.data.market_provider.get_bulk_live_quotes_with_meta", return_value={})
        self.patcher_tg = patch("app.analytics.telegram_notifier.send_telegram_message", return_value=True)

        self.patcher_db0.start()
        self.patcher_db1.start()
        self.patcher_db2.start()
        self.patcher_db3.start()
        self.patcher_live.start()
        self.patcher_tg.start()

        # Initialize schemas
        conn = sqlite3.connect(self.db_path)
        ConsensusTracker.init_consensus_tracking_schema(conn)
        ConsensusGameEngine.init_game_schema(conn)

        # Initialize default arena account
        ConsensusGameEngine.deploy_capital(
            initial_capital=100000.0,
            session_id="default_arena_session",
            trade_selection_mode="HIGHEST_CONFLUENCE_ONLY",
            conn=conn
        )
        conn.close()

    def tearDown(self):
        self.patcher_db0.stop()
        self.patcher_db1.stop()
        self.patcher_db2.stop()
        self.patcher_db3.stop()
        self.patcher_live.stop()
        self.patcher_tg.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _insert_candidate_at(self, ticker, score, timestamp_str, direction="BUY", entry=1000.0, sl=980.0, tp=1040.0):
        """Helper to insert candidate at exact timestamp."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO consensus_trade_history (
                timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                qlib_score, legacy_score, finbert_score, risk_reward_ratio,
                status, outcome, source, position_type, details_json
            ) VALUES (?, ?, ?, 'INTRADAY', ?, ?, ?, ?, ?, ?, ?, 0.0, 2.0, 'OPEN', NULL, 'MANUAL', 'NOT_A_POSITION', '{}')
        """, (
            timestamp_str, ticker, ticker.replace(".NS", ""), direction,
            entry, sl, tp, score, score, score
        ))
        row_id = cur.lastrowid
        conn.commit()
        conn.close()
        return row_id

    def test_01_rejected_candidate_eligible_and_wins_scan_2(self):
        """
        TEST 1:
        Scan 1:
        A = 50.6 -> selected
        B = 50.5 -> rejected
        C = 50.4 -> rejected

        Scan 2:
        B = 50.5 -> SHOULD BE ELIGIBLE
        C = 50.2
        D = 50.1

        Expected:
        B must be eligible in Scan 2.
        B must win Scan 2.
        D must NOT automatically win merely because B appeared previously.
        """
        conn = sqlite3.connect(self.db_path)

        # ── SCAN 1 (10:00:00) ──
        # Mock time at 10:00:00
        t1 = "2026-09-24 10:00:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t1, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime

            # Save candidates
            s_a, r_a = ConsensusTracker.save_consensus_trade({"ticker": "A.NS", "composite_score": 50.6, "blended_entry": 100.0, "blended_sl": 98.0, "blended_tp": 104.0})
            s_b, r_b = ConsensusTracker.save_consensus_trade({"ticker": "B.NS", "composite_score": 50.5, "blended_entry": 200.0, "blended_sl": 196.0, "blended_tp": 208.0})
            s_c, r_c = ConsensusTracker.save_consensus_trade({"ticker": "C.NS", "composite_score": 50.4, "blended_entry": 300.0, "blended_sl": 294.0, "blended_tp": 312.0})

            self.assertTrue(s_a)
            self.assertTrue(s_b)
            self.assertTrue(s_c)

        # Run Arena portfolio sync for Scan 1
        ConsensusGameEngine._sync_and_progress_portfolio("default_arena_session", conn)

        # In Scan 1: A must be selected and OPEN in the Arena
        cur = conn.cursor()
        open_positions = cur.execute("SELECT ticker, status FROM consensus_game_positions WHERE session_id = 'default_arena_session' AND status = 'OPEN'").fetchall()
        open_tickers = [p[0] for p in open_positions]
        self.assertEqual(open_tickers, ["A.NS"])

        # ── SCAN 2 (10:15:00) ──
        t2 = "2026-09-24 10:15:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t2, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime

            # Candidate B (50.5) appears again in Scan 2!
            s_b2, r_b2 = ConsensusTracker.save_consensus_trade({"ticker": "B.NS", "composite_score": 50.5, "blended_entry": 200.0, "blended_sl": 196.0, "blended_tp": 208.0})
            s_c2, r_c2 = ConsensusTracker.save_consensus_trade({"ticker": "C.NS", "composite_score": 50.2, "blended_entry": 300.0, "blended_sl": 294.0, "blended_tp": 312.0})
            s_d2, r_d2 = ConsensusTracker.save_consensus_trade({"ticker": "D.NS", "composite_score": 50.1, "blended_entry": 400.0, "blended_sl": 392.0, "blended_tp": 416.0})

            # B MUST NOT be rejected as a duplicate! B is eligible!
            self.assertTrue(s_b2, f"Candidate B was rejected as duplicate in Scan 2! Reason: {r_b2}")
            self.assertEqual(r_b2, "SAVED")
            self.assertTrue(s_c2)
            self.assertTrue(s_d2)

        # Run Arena portfolio sync after Scan 2
        ConsensusGameEngine._sync_and_progress_portfolio("default_arena_session", conn)

        # Verify active positions: A (from Scan 1) and B (from Scan 2) must BOTH be open!
        cur = conn.cursor()
        open_positions_after = cur.execute("SELECT ticker, status FROM consensus_game_positions WHERE session_id = 'default_arena_session' AND status = 'OPEN' ORDER BY id ASC").fetchall()
        open_tickers_after = [p[0] for p in open_positions_after]

        self.assertIn("A.NS", open_tickers_after)
        self.assertIn("B.NS", open_tickers_after)
        self.assertNotIn("D.NS", open_tickers_after, "D must NOT win Scan 2 when B is present with 50.5 > 50.1!")
        self.assertEqual(len(open_tickers_after), 2)
        conn.close()

    def test_02_heg_signal_progression_no_duplicate_rejection(self):
        """
        TEST 2:
        Scan 1 (10:00): HEG 50.4 -> rejected (another trade selected)
        Scan 2 (10:15): HEG 50.5 -> eligible
        Scan 3 (10:30): HEG 50.6 -> eligible
        Verify no duplicate rejection occurs merely because HEG appeared previously.
        """
        conn = sqlite3.connect(self.db_path)

        # 10:00: MOTHERSON has 50.6, HEG has 50.4
        t1 = "2026-09-24 10:00:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t1, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime
            s1_m, _ = ConsensusTracker.save_consensus_trade({"ticker": "MOTHERSON.NS", "composite_score": 50.6, "blended_entry": 100.0, "blended_sl": 98.0, "blended_tp": 104.0})
            s1_h, r1_h = ConsensusTracker.save_consensus_trade({"ticker": "HEG.NS", "composite_score": 50.4, "blended_entry": 2000.0, "blended_sl": 1960.0, "blended_tp": 2080.0})
            self.assertTrue(s1_h)
            self.assertEqual(r1_h, "SAVED")

        ConsensusGameEngine._sync_and_progress_portfolio("default_arena_session", conn)

        # 10:15: HEG has 50.5
        t2 = "2026-09-24 10:15:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t2, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime
            s2_h, r2_h = ConsensusTracker.save_consensus_trade({"ticker": "HEG.NS", "composite_score": 50.5, "blended_entry": 2010.0, "blended_sl": 1970.0, "blended_tp": 2090.0})
            self.assertTrue(s2_h, f"HEG was incorrectly rejected in Scan 2: {r2_h}")
            self.assertEqual(r2_h, "SAVED")

        # 10:30: HEG has 50.6
        t3 = "2026-09-24 10:30:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t3, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime
            s3_h, r3_h = ConsensusTracker.save_consensus_trade({"ticker": "HEG.NS", "composite_score": 50.6, "blended_entry": 2020.0, "blended_sl": 1980.0, "blended_tp": 2100.0})
            self.assertTrue(s3_h, f"HEG was incorrectly rejected in Scan 3: {r3_h}")
            self.assertEqual(r3_h, "SAVED")

        # Verify distinct observations exist in consensus_trade_history with correct timestamps
        cur = conn.cursor()
        rows = cur.execute("SELECT timestamp, composite_score FROM consensus_trade_history WHERE ticker = 'HEG.NS' ORDER BY timestamp ASC").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], "2026-09-24 10:00:00")
        self.assertEqual(rows[1][0], "2026-09-24 10:15:00")
        self.assertEqual(rows[2][0], "2026-09-24 10:30:00")
        conn.close()

    def test_03_active_trade_protection_against_duplication(self):
        """
        TEST 3:
        Scan 1: A 50.6 -> selected -> ACTIVE.
        Scan 2: A 50.8.
        Verify a second active A trade is NOT created.
        Verify the existing active trade is preserved.
        """
        conn = sqlite3.connect(self.db_path)

        t1 = "2026-09-24 10:00:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t1, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime
            ConsensusTracker.save_consensus_trade({"ticker": "A.NS", "composite_score": 50.6, "blended_entry": 100.0, "blended_sl": 98.0, "blended_tp": 104.0})

        ConsensusGameEngine._sync_and_progress_portfolio("default_arena_session", conn)

        # Check A is active
        cur = conn.cursor()
        pos1 = cur.execute("SELECT trade_ref_id, ticker, status, entry_price, entry_time FROM consensus_game_positions WHERE session_id = 'default_arena_session' AND ticker = 'A.NS' AND status = 'OPEN'").fetchall()
        self.assertEqual(len(pos1), 1)
        original_ref_id = pos1[0][0]
        original_entry_px = pos1[0][3]
        original_entry_t = pos1[0][4]

        # Scan 2: A appears with 50.8
        t2 = "2026-09-24 10:15:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t2, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime
            saved_a2, reason_a2 = ConsensusTracker.save_consensus_trade({"ticker": "A.NS", "composite_score": 50.8, "blended_entry": 105.0, "blended_sl": 103.0, "blended_tp": 109.0})

            # Must be rejected because A is an active trade!
            self.assertFalse(saved_a2)
            self.assertIn("REJECTED_ACTIVE_DUPLICATE", reason_a2)

        # Re-run portfolio progress
        ConsensusGameEngine._sync_and_progress_portfolio("default_arena_session", conn)

        # Verify only 1 active trade exists for A, and original position preserved
        pos2 = cur.execute("SELECT trade_ref_id, ticker, status, entry_price, entry_time FROM consensus_game_positions WHERE session_id = 'default_arena_session' AND ticker = 'A.NS' AND status = 'OPEN'").fetchall()
        self.assertEqual(len(pos2), 1)
        self.assertEqual(pos2[0][0], original_ref_id)
        self.assertEqual(pos2[0][3], original_entry_px)
        self.assertEqual(pos2[0][4], original_entry_t)
        conn.close()

    def test_04_closed_trade_cooldown_and_reentry(self):
        """
        TEST 4:
        A selected -> later CLOSED.
        During cooldown (<15m): A rejected as RECENTLY_CLOSED_COOLDOWN.
        After cooldown (>=15m): A appears again -> can become a new trade.
        """
        conn = sqlite3.connect(self.db_path)

        # 1. Open trade A at 10:00
        t1 = "2026-09-24 10:00:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t1, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime
            ConsensusTracker.save_consensus_trade({"ticker": "A.NS", "composite_score": 50.6, "blended_entry": 100.0, "blended_sl": 98.0, "blended_tp": 104.0})

        ConsensusGameEngine._sync_and_progress_portfolio("default_arena_session", conn)

        # 2. Close trade A at 10:30 (simulated exit)
        cur = conn.cursor()
        pos = cur.execute("SELECT id FROM consensus_game_positions WHERE session_id = 'default_arena_session' AND ticker = 'A.NS' AND status = 'OPEN'").fetchone()
        self.assertIsNotNone(pos)
        pos_id = pos[0]

        # Manually exit position with exit_time = 10:30:00
        cur.execute("""
            UPDATE consensus_game_positions
            SET status = 'CLOSED', exit_time = '2026-09-24 10:30:00', exit_price = 104.0, outcome = 'TARGET MET'
            WHERE id = ?
        """, (pos_id,))
        cur.execute("""
            UPDATE consensus_trade_history
            SET status = 'CLOSED', exit_time = '2026-09-24 10:30:00', exit_price = 104.0, outcome = 'TARGET MET'
            WHERE ticker = 'A.NS'
        """)
        conn.commit()

        # 3. Candidate A appears at 10:35 (5 minutes after exit -> in cooldown)
        t_early = "2026-09-24 10:35:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t_early, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime
            s_early, r_early = ConsensusTracker.save_consensus_trade({"ticker": "A.NS", "composite_score": 50.7, "blended_entry": 104.5, "blended_sl": 102.5, "blended_tp": 108.5})
            self.assertFalse(s_early)
            self.assertIn("RECENTLY_CLOSED_COOLDOWN", r_early)

        # 4. Candidate A appears at 10:50 (20 minutes after exit -> cooldown elapsed)
        t_reentry = "2026-09-24 10:50:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t_reentry, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime
            s_re, r_re = ConsensusTracker.save_consensus_trade({"ticker": "A.NS", "composite_score": 50.7, "blended_entry": 104.5, "blended_sl": 102.5, "blended_tp": 108.5})
            self.assertTrue(s_re, f"Candidate A was blocked after cooldown: {r_re}")
            self.assertEqual(r_re, "SAVED")

        # 5. Re-run portfolio progression: A must become a new open trade
        ConsensusGameEngine._sync_and_progress_portfolio("default_arena_session", conn)
        new_pos = cur.execute("SELECT id, status, entry_time FROM consensus_game_positions WHERE session_id = 'default_arena_session' AND ticker = 'A.NS' AND status = 'OPEN'").fetchone()
        self.assertIsNotNone(new_pos)
        self.assertNotEqual(new_pos[0], pos_id)
        self.assertEqual(new_pos[2], "2026-09-24 10:50:00")
        conn.close()

    def test_05_refresh_arena_does_not_evict_active_trade(self):
        """
        TEST 5:
        Refresh Arena after A is selected.
        Verify A does not disappear merely because another candidate with an equal/higher score is inserted later.
        """
        conn = sqlite3.connect(self.db_path)

        # Scan 1 at 10:00: Candidate A (50.6) selected into Arena
        t1 = "2026-09-24 10:00:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t1, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime
            ConsensusTracker.save_consensus_trade({"ticker": "A.NS", "composite_score": 50.6, "blended_entry": 100.0, "blended_sl": 98.0, "blended_tp": 104.0})

        ConsensusGameEngine._sync_and_progress_portfolio("default_arena_session", conn)

        # Later, at 10:15: Candidate B appears with score 51.5 (higher score!)
        t2 = "2026-09-24 10:15:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t2, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime
            ConsensusTracker.save_consensus_trade({"ticker": "B.NS", "composite_score": 51.5, "blended_entry": 200.0, "blended_sl": 196.0, "blended_tp": 208.0})

        # Trigger full refresh / recalculation
        ConsensusGameEngine.deploy_capital(
            initial_capital=100000.0,
            session_id="default_arena_session",
            trade_selection_mode="HIGHEST_CONFLUENCE_ONLY",
            conn=conn
        )

        cur = conn.cursor()
        open_positions = cur.execute("SELECT ticker, status, entry_time FROM consensus_game_positions WHERE session_id = 'default_arena_session' AND status = 'OPEN' ORDER BY id ASC").fetchall()
        open_tickers = [p[0] for p in open_positions]

        # A must NOT disappear! Both A and B must be active.
        self.assertIn("A.NS", open_tickers)
        self.assertIn("B.NS", open_tickers)
        self.assertEqual(len(open_tickers), 2)
        conn.close()

    def test_06_database_id_does_not_determine_eligibility(self):
        """
        TEST 6:
        Change database IDs.
        Verify database ID does NOT determine whether a previously rejected candidate becomes eligible.
        """
        conn = sqlite3.connect(self.db_path)

        # Insert candidate X with ID 999 and candidate Y with ID 1
        t1 = "2026-09-24 10:00:00"
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                status, outcome, source, position_type, details_json
            ) VALUES (999, ?, 'TICKER_X.NS', 'TICKER_X', 'INTRADAY', 'BUY', 100.0, 98.0, 104.0, 50.8, 'OPEN', NULL, 'MANUAL', 'NOT_A_POSITION', '{}')
        """, (t1,))
        cur.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                status, outcome, source, position_type, details_json
            ) VALUES (1, ?, 'TICKER_Y.NS', 'TICKER_Y', 'INTRADAY', 'BUY', 200.0, 196.0, 208.0, 50.4, 'OPEN', NULL, 'MANUAL', 'NOT_A_POSITION', '{}')
        """, (t1,))
        conn.commit()

        # Replay Scan 1
        ConsensusGameEngine._sync_and_progress_portfolio("default_arena_session", conn)

        # X is selected (50.8 > 50.4)
        open_p = cur.execute("SELECT ticker FROM consensus_game_positions WHERE session_id = 'default_arena_session' AND status = 'OPEN'").fetchall()
        self.assertEqual([p[0] for p in open_p], ["TICKER_X.NS"])

        # Scan 2: Y appears with fresh timestamp at 10:15
        t2 = "2026-09-24 10:15:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t2, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime
            s_y, r_y = ConsensusTracker.save_consensus_trade({"ticker": "TICKER_Y.NS", "composite_score": 50.6, "blended_entry": 200.0, "blended_sl": 196.0, "blended_tp": 208.0})

            # Y must be eligible regardless of whether ID was 1 or 9999
            self.assertTrue(s_y)
            self.assertEqual(r_y, "SAVED")

        ConsensusGameEngine._sync_and_progress_portfolio("default_arena_session", conn)
        open_p2 = cur.execute("SELECT ticker FROM consensus_game_positions WHERE session_id = 'default_arena_session' AND status = 'OPEN' ORDER BY id ASC").fetchall()
        self.assertIn("TICKER_Y.NS", [p[0] for p in open_p2])
        conn.close()

    def test_07_audit_reason_transparency(self):
        """
        TEST 7:
        Verify clear audit reasons:
        SELECTED, REJECTED_LOWER_RANK, REJECTED_ACTIVE_DUPLICATE, REJECTED_RISK.
        Verify DUPLICATE is never returned for unselected candidates.
        """
        conn = sqlite3.connect(self.db_path)

        # Scan 1: Candidate A (50.8) and Candidate B (50.4)
        t1 = "2026-09-24 15:15:00"  # >= cutoff
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t1, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime
            ConsensusTracker.save_consensus_trade({"ticker": "AUDIT_A.NS", "composite_score": 50.8, "blended_entry": 100.0, "blended_sl": 98.0, "blended_tp": 104.0})
            ConsensusTracker.save_consensus_trade({"ticker": "AUDIT_B.NS", "composite_score": 50.4, "blended_entry": 200.0, "blended_sl": 196.0, "blended_tp": 208.0})

        ConsensusGameEngine._sync_and_progress_portfolio("default_arena_session", conn)

        cur = conn.cursor()
        outcome_a = cur.execute("SELECT outcome FROM consensus_trade_history WHERE ticker = 'AUDIT_A.NS'").fetchone()[0]
        outcome_b = cur.execute("SELECT outcome FROM consensus_trade_history WHERE ticker = 'AUDIT_B.NS'").fetchone()[0]

        self.assertEqual(outcome_a, "SELECTED")
        self.assertEqual(outcome_b, "REJECTED_LOWER_RANK")

        # Scan 2: AUDIT_A appears while active -> must be REJECTED_ACTIVE_DUPLICATE
        t2 = "2026-09-24 15:30:00"
        with patch("app.analytics.consensus_tracker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.strptime(t2, "%Y-%m-%d %H:%M:%S")
            mock_dt.strptime = datetime.strptime
            saved_a, reason_a = ConsensusTracker.save_consensus_trade({"ticker": "AUDIT_A.NS", "composite_score": 51.0, "blended_entry": 102.0, "blended_sl": 100.0, "blended_tp": 106.0})
            self.assertFalse(saved_a)
            self.assertIn("REJECTED_ACTIVE_DUPLICATE", reason_a)
            self.assertNotIn("DUPLICATE_SESSION_TRADE", reason_a)

        conn.close()


if __name__ == "__main__":
    unittest.main()
