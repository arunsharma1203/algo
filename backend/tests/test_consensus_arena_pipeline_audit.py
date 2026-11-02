"""
CONSENSUS ARENA PIPELINE AUDIT TEST SUITE
=========================================
Rigorous forensic regression tests verifying the 5 operational and execution integrity fixes:
1. BAR/SNAPSHOT SYNCHRONIZATION: Frozen 15-minute decision timestamp and synchronized pricing.
2. DATA FRESHNESS / STALE FLAG RECOVERY: Fail-closed during market hours when live quote feed fails,
   and immediate recovery when fresh quote arrives.
3. TIMED SCAN INTEGRITY: Quantized 15-minute decision buckets and tightened anchor matching tolerance.
4. MISSED ANCHOR RECOVERY: Persistent schedule ledger, idempotent single recovery, zero duplicate scans/trades.
5. ARENA SELECTION INTEGRITY: Confluence composite score priority over database insertion order (id ASC),
   strict separation of active vs rejected states.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

from app.analytics.consensus_game_engine import ConsensusGameEngine
from app.analytics.consensus_scanner import ConsensusScanner
from app.analytics.consensus_tiebreaker import (
    HISTORICAL_TIEBREAKER_CUTOFF,
    CandidateMetrics,
    compare_candidates_economically,
)
from app.analytics.consensus_tracker import ConsensusTracker
from app.data.historical_data_layer import get_db_path
from app.tasks.intraday_market_daemon import (
    check_and_recover_missed_anchors,
    init_anchor_schedule_ledger,
)


class TestConsensusArenaPipelineAudit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        self.db_patcher = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.db_patcher.start()

        self.tracker_db_patcher = patch("app.analytics.consensus_tracker.get_db_path", return_value=self.db_path)
        self.tracker_db_patcher.start()

        self.daemon_db_patcher = patch("app.tasks.intraday_market_daemon.get_db_path", return_value=self.db_path)
        self.daemon_db_patcher.start()

        self.engine_db_patcher = patch("app.analytics.consensus_game_engine.get_db_path", return_value=self.db_path)
        self.engine_db_patcher.start()

        # Initialize tables
        ConsensusTracker._schema_initialized = False
        ConsensusGameEngine._schema_initialized = False
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        ConsensusTracker.init_consensus_tracking_schema(conn)
        ConsensusGameEngine.init_game_schema(conn)
        init_anchor_schedule_ledger(self.db_path)
        conn.close()

        # Silence Telegram alerts during tests
        self.tg_patch = patch("app.analytics.telegram_notifier.send_telegram_message", return_value=True)
        self.tg_patch.start()

    def tearDown(self):
        self.tg_patch.stop()
        self.engine_db_patcher.stop()
        self.daemon_db_patcher.stop()
        self.tracker_db_patcher.stop()
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    # ── ITEM 1: BAR/SNAPSHOT SYNCHRONIZATION ────────────────────────────
    @patch("app.data.data_gateway.DataGateway.is_market_open", return_value=False)
    @patch("app.analytics.consensus_scanner.SmartScannerPipeline.run_sweep_batch")
    @patch("app.analytics.consensus_scanner.QlibScanners.run_intraday_scan")
    async def test_01_bar_snapshot_synchronization(self, mock_qlib, mock_legacy, mock_market_open):
        """Verifies that each 15-minute decision uses one frozen bar timestamp and synchronous pricing."""
        mock_qlib.return_value = {
            "status": "SUCCESS",
            "opportunities": [
                {
                    "ticker": "RELIANCE.NS",
                    "direction": "BUY",
                    "entry_price": 2500.0,
                    "stop_loss": 2450.0,
                    "target_price": 2600.0,
                    "score": 0.85,
                    "rank": 1
                }
            ]
        }
        mock_legacy.return_value = {
            "status": "SUCCESS",
            "candidates": [
                {
                    "ticker": "RELIANCE.NS",
                    "direction": "BULLISH",
                    "is_bullish": True,
                    "entry": 2500.0,
                    "sl": 2450.0,
                    "tp1": 2600.0,
                    "confidence": 85.0
                }
            ]
        }

        res = await ConsensusScanner.run_consensus_scan(strategy="INTRADAY", universe="NIFTY_50", top_k=5)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(len(res["unanimous_trades"]), 1)

        trade = res["unanimous_trades"][0]
        # Must have frozen decision timestamp aligned to 15-minute bar
        self.assertIn("decision_timestamp", trade)
        self.assertIn("timestamp", trade)
        self.assertEqual(trade["decision_timestamp"], trade["timestamp"])

        dt_val = datetime.strptime(trade["decision_timestamp"], "%Y-%m-%d %H:%M:%S")
        self.assertEqual(dt_val.minute % 15, 0, "Decision timestamp must be quantized to 15-minute bar")
        self.assertEqual(dt_val.second, 0)

    # ── ITEM 2: DATA FRESHNESS / STALE FLAG RECOVERY ─────────────────────
    @patch("app.data.data_gateway.DataGateway.is_market_open", return_value=True)
    @patch("app.data.upstox_provider.fetch_upstox_bulk_quotes")
    @patch("app.analytics.consensus_scanner.SmartScannerPipeline.run_sweep_batch")
    @patch("app.analytics.consensus_scanner.QlibScanners.run_intraday_scan")
    async def test_02_data_freshness_fail_closed_and_recovery(
        self, mock_qlib, mock_legacy, mock_bulk_quotes, mock_market_open
    ):
        """Verifies fail-closed behavior during market hours when live quote is missing, and immediate recovery on fresh quote."""
        mock_qlib.return_value = {
            "status": "SUCCESS",
            "opportunities": [
                {
                    "ticker": "INFY.NS",
                    "direction": "BUY",
                    "entry_price": 1800.0,
                    "stop_loss": 1780.0,
                    "target_price": 1840.0,
                    "score": 0.80,
                    "rank": 1
                }
            ]
        }
        mock_legacy.return_value = {
            "status": "SUCCESS",
            "candidates": [
                {
                    "ticker": "INFY.NS",
                    "direction": "BULLISH",
                    "is_bullish": True,
                    "entry": 1800.0,
                    "sl": 1780.0,
                    "tp1": 1840.0,
                    "confidence": 80.0
                }
            ]
        }

        # Case A: Live quote feed fails (empty dict returned during open market)
        mock_bulk_quotes.return_value = {}
        res_stale = await ConsensusScanner.run_consensus_scan(strategy="INTRADAY", universe="NIFTY_50", top_k=5)
        trade_stale = res_stale["unanimous_trades"][0]
        self.assertFalse(trade_stale["is_actionable"], "Must fail closed during open market when live quote is missing")
        self.assertIn("BLOCKED: Live quote feed unavailable", trade_stale["precision_status"])

        # Case B: Fresh Upstox fetch succeeds -> Stale state immediately cleared
        mock_bulk_quotes.return_value = {
            "INFY.NS": {"ltp": 1805.0, "vwap": 1802.0, "open": 1795.0}
        }
        res_fresh = await ConsensusScanner.run_consensus_scan(strategy="INTRADAY", universe="NIFTY_50", top_k=5)
        trade_fresh = res_fresh["unanimous_trades"][0]
        self.assertTrue(trade_fresh["is_actionable"], "Fresh live quote must immediately clear fail-closed block")
        self.assertEqual(trade_fresh["precision_status"], "QUALIFIED")
        self.assertEqual(trade_fresh["entry_price"], 1805.0)

    # ── ITEM 3: TIMED SCAN INTEGRITY ────────────────────────────────────
    def test_03_timed_scan_bucket_integrity_and_deduplication(self):
        """Verifies that candidates in the same 15m scan window update rather than creating split minute buckets."""
        trade_data = {
            "ticker": "TATAMOTORS.NS",
            "symbol": "TATAMOTORS",
            "direction": "BUY",
            "blended_entry": 950.0,
            "blended_sl": 940.0,
            "blended_tp": 970.0,
            "composite_score": 82.0,
            "risk_reward_ratio": 2.0,
            "qlib_metrics": {"score": 0.025},
            "legacy_metrics": {"confidence": 80.0}
        }

        # First candidate arrives at 09:30:15
        saved1, reason1 = ConsensusTracker.save_consensus_trade(
            trade_data, strategy="INTRADAY", decision_timestamp="2026-10-05 09:30:15"
        )
        self.assertTrue(saved1)

        # Updated candidate arrives at 09:31:05 (same 15-minute decision bar: 09:30-09:45)
        trade_data["composite_score"] = 85.0
        saved2, reason2 = ConsensusTracker.save_consensus_trade(
            trade_data, strategy="INTRADAY", decision_timestamp="2026-10-05 09:31:05"
        )
        self.assertFalse(saved2)
        self.assertIn("DUPLICATE_SESSION_TRADE", reason2)

        # Verify only 1 row exists in consensus_trade_history with the updated score
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        rows = cur.execute("SELECT id, composite_score FROM consensus_trade_history WHERE ticker = 'TATAMOTORS.NS'").fetchall()
        conn.close()

        self.assertEqual(len(rows), 1, "Candidates within the same 15m bar must not create multiple split rows")
        self.assertEqual(rows[0][1], 85.0, "Candidate within the same 15m bar must be updated in-place")

    # ── ITEM 4: MISSED ANCHOR RECOVERY ──────────────────────────────────
    @patch("app.analytics.consensus_scanner.ConsensusScanner.run_consensus_scan")
    def test_04_missed_anchor_recovery_idempotence(self, mock_scan):
        """Verifies that a missed scheduled anchor is recovered safely once, with zero duplicates."""
        mock_scan.return_value = {
            "status": "SUCCESS",
            "scan_id": "test_scan_recov",
            "unanimous_trades": []
        }

        # Suppose current time is 09:40 IST (09:30 anchor was missed 10 minutes ago due to restart)
        fixed_dt = datetime.strptime("2026-10-05 09:40:00", "%Y-%m-%d %H:%M:%S")

        with patch("app.tasks.intraday_market_daemon.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_dt
            mock_dt.strftime = datetime.strftime

            # First recovery run
            recov1 = check_and_recover_missed_anchors(force_run=False, db_path=self.db_path)
            self.assertEqual(len(recov1), 1)
            self.assertEqual(recov1[0]["anchor"], "09:30")
            self.assertEqual(recov1[0]["status"], "RECOVERED")

            # Second call (idempotent: anchor is already recovered)
            recov2 = check_and_recover_missed_anchors(force_run=False, db_path=self.db_path)
            self.assertEqual(len(recov2), 0, "Subsequent check must NOT re-run recovered anchor (zero duplicate scans)")

        # Verify schedule ledger state in database
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        ledger_rows = cur.execute("SELECT anchor_name, status FROM consensus_anchor_schedule_ledger WHERE date = '2026-10-05'").fetchall()
        conn.close()

        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(ledger_rows[0][0], "09:30")
        self.assertEqual(ledger_rows[0][1], "RECOVERED")

    # ── ITEM 5: ARENA SELECTION INTEGRITY ────────────────────────────────
    def test_05_selection_order_prefers_composite_score_over_db_id(self):
        """
        Verifies that when multiple candidate trades arrive at the same scan bucket,
        selection into open slots is based strictly on composite_score DESC,
        NEVER by arbitrary database insertion order (id ASC).
        """
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        # Setup an account with max_positions = 1 (only 1 slot available)
        session_id = "test_selection_arena"
        cur.execute("""
            INSERT OR REPLACE INTO consensus_game_accounts (
                session_id, account_name, initial_capital, current_cash,
                peak_equity, max_positions, allocation_mode, trade_selection_mode, status,
                created_at, updated_at
            ) VALUES (?, 'tester', 100000.0, 100000.0, 100000.0, 1, 'EQUAL_WEIGHT', 'ALL_QUALIFIED', 'ACTIVE', '2026-10-05T09:00:00', '2026-10-05T09:00:00')
        """, (session_id,))

        scan_ts = "2026-10-05 10:15:00"

        # Trade A: Inserted FIRST (id = 1), lower score (55.0)
        cur.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction, entry_price, stop_loss, target_price,
                composite_score, qlib_score, legacy_score, status, outcome
            ) VALUES (1, ?, 'LOW_SCORE.NS', 'LOW_SCORE', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 55.0, 55.0, 55.0, 'OPEN', NULL)
        """, (scan_ts,))

        # Trade B: Inserted SECOND (id = 2), higher score (92.0)
        cur.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction, entry_price, stop_loss, target_price,
                composite_score, qlib_score, legacy_score, status, outcome
            ) VALUES (2, ?, 'HIGH_SCORE.NS', 'HIGH_SCORE', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 92.0, 92.0, 92.0, 'OPEN', NULL)
        """, (scan_ts,))
        conn.commit()

        # Run replay progression
        ConsensusGameEngine._sync_and_progress_portfolio(session_id, conn)

        # Check which trade claimed the single slot
        active_pos = cur.execute("SELECT ticker, trade_ref_id FROM consensus_game_positions WHERE session_id = ? AND status = 'OPEN'", (session_id,)).fetchall()
        self.assertEqual(len(active_pos), 1)
        self.assertEqual(active_pos[0][0], "HIGH_SCORE.NS", "High score candidate (92.0) must win slot over low score (55.0), despite id=2 > id=1")
        self.assertEqual(active_pos[0][1], 2)

        # Check audit outcome on the rejected trade
        low_outcome = cur.execute("SELECT outcome FROM consensus_trade_history WHERE id = 1").fetchone()[0]
        self.assertEqual(low_outcome, "REJECTED_RISK", "Lower ranked trade that missed slot must have audit rejection outcome")

        conn.close()


if __name__ == "__main__":
    unittest.main()
