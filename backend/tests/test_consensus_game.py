"""
Comprehensive Unit Test Suite for Consensus Game Arena.

Validates:
1. Isolated Sandbox Schema & Invariant Enforcement
2. Capital Deployment (₹1,00,000 & Custom Amounts)
3. Strict Whole-Share Capital Allocation & Money Management
4. Zero Contamination / Read-Only Guarantee on `consensus_trade_history`
5. P&L Calendar Aggregation (Zerodha Console Style)
6. Historical Day Replay Playback (0 External API Calls)
7. Gamer Level, XP, and Badges Calculation
8. Upstox Rate Limiter Shield Telemetry
"""

import unittest
import sqlite3
import tempfile
import os
import json
from unittest.mock import patch

from app.analytics.consensus_game_engine import ConsensusGameEngine
from app.data.upstox_provider import get_upstox_limiter


class TestConsensusGameEngine(unittest.TestCase):
    def setUp(self):
        # Create isolated temporary database for test
        self.temp_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db_file.name
        self.temp_db_file.close()

        # Connect and seed schema
        self.conn = sqlite3.connect(self.db_path)
        self.cur = self.conn.cursor()

        # Create consensus_trade_history with authentic columns
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

        # Seed 3 authentic sample consensus trades
        # Trade 1: HAL.NS (BUY, Exit: SQUARED OFF, profit: -0.37%)
        self.cur.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                status, outcome, profit_pct, exit_price, exit_time
            ) VALUES (1, '2026-09-18 11:24:10', 'HAL.NS', 'HAL', 'INTRADAY', 'BUY',
                      4878.0, 4849.44, 4892.56, 60.0,
                      'CLOSED', 'SQUARED OFF', -0.37, 4860.10, '2026-09-18 15:23:47')
        """)

        # Trade 2: GODIGIT.NS (BUY, Exit: TARGET MET, profit: +0.36%)
        self.cur.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                status, outcome, profit_pct, exit_price, exit_time
            ) VALUES (2, '2026-09-18 11:24:10', 'GODIGIT.NS', 'GODIGIT', 'INTRADAY', 'BUY',
                      241.45, 241.69, 242.33, 58.7,
                      'CLOSED', 'TARGET MET', 0.36, 242.33, '2026-09-18 15:09:50')
        """)

        # Trade 3: BALKRISIND.NS (BUY, Exit: TARGET MET, profit: +0.71%)
        self.cur.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                status, outcome, profit_pct, exit_price, exit_time
            ) VALUES (3, '2026-09-18 11:24:10', 'BALKRISIND.NS', 'BALKRISIND', 'INTRADAY', 'BUY',
                      2237.5, 2239.74, 2253.31, 60.0,
                      'CLOSED', 'TARGET MET', 0.71, 2253.31, '2026-09-18 15:09:50')
        """)
        self.conn.commit()

        # Patch get_db_path in both historical_data_layer and consensus_game_engine
        self.patcher = patch("app.analytics.consensus_game_engine.get_db_path", return_value=self.db_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_schema_initialization(self):
        """Verify dedicated sandbox tables are created without modifying production tables."""
        ConsensusGameEngine.init_game_schema(self.conn)
        tables = [r[0] for r in self.cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        self.assertIn("consensus_game_accounts", tables)
        self.assertIn("consensus_game_positions", tables)
        self.assertIn("consensus_game_snapshots", tables)

    def test_deploy_capital_1_lakh(self):
        """Verify deploying ₹1,00,000 initializes account and sizes authentic trades."""
        state = ConsensusGameEngine.deploy_capital(
            initial_capital=100000.0,
            allocation_mode="EQUAL_WEIGHT",
            max_positions=5,
            fees_enabled=True,
            session_id="test_session",
            conn=self.conn
        )
        self.assertEqual(state["initial_capital"], 100000.0)
        self.assertGreater(state["total_closed_trades"], 0)
        self.assertEqual(state["session_id"], "test_session")

        # Verify whole share quantities
        positions = ConsensusGameEngine.get_positions(session_id="test_session", conn=self.conn)
        self.assertEqual(len(positions), 3)
        for pos in positions:
            self.assertIsInstance(pos["shares"], int)
            self.assertGreater(pos["shares"], 0)
            self.assertLessEqual(pos["allocated_capital"], 100000.0 / 5 + 10.0)

    def test_strict_read_only_invariants(self):
        """Verify core consensus_trade_history is 100% UNTOUCHED by game operations."""
        # Check initial trade history state
        initial_count = self.cur.execute("SELECT COUNT(*) FROM consensus_trade_history").fetchone()[0]
        initial_data = self.cur.execute("SELECT id, ticker, status, profit_pct FROM consensus_trade_history ORDER BY id").fetchall()

        # Run game actions
        ConsensusGameEngine.deploy_capital(100000.0, session_id="s1", conn=self.conn)
        ConsensusGameEngine.deploy_capital(50000.0, session_id="s2", conn=self.conn)
        ConsensusGameEngine.get_portfolio_state(session_id="s1", conn=self.conn)
        ConsensusGameEngine.get_equity_curve(session_id="s1", conn=self.conn)
        ConsensusGameEngine.get_pnl_calendar(session_id="s1", conn=self.conn)

        # Verify trade history rows and content are completely unchanged
        final_count = self.cur.execute("SELECT COUNT(*) FROM consensus_trade_history").fetchone()[0]
        final_data = self.cur.execute("SELECT id, ticker, status, profit_pct FROM consensus_trade_history ORDER BY id").fetchall()

        self.assertEqual(initial_count, final_count)
        self.assertEqual(initial_data, final_data)

    def test_pnl_calendar_aggregation(self):
        """Verify P&L Calendar aggregates daily net returns, trade counts, and win rates accurately."""
        ConsensusGameEngine.deploy_capital(100000.0, session_id="cal_session", conn=self.conn)
        calendar = ConsensusGameEngine.get_pnl_calendar(session_id="cal_session", conn=self.conn)

        self.assertEqual(calendar["session_id"], "cal_session")
        self.assertEqual(len(calendar["days"]), 1)
        day_0 = calendar["days"][0]
        self.assertEqual(day_0["date"], "2026-09-18")
        self.assertEqual(day_0["trade_count"], 3)
        self.assertIn("net_pnl_rupees", day_0)
        self.assertIn("win_rate", day_0)
        self.assertEqual(len(day_0["trades"]), 3)

    def test_historical_day_replay(self):
        """Verify day replay timeline produces chronological equity progression."""
        ConsensusGameEngine.deploy_capital(100000.0, session_id="replay_session", conn=self.conn)
        replay = ConsensusGameEngine.get_day_replay(session_id="replay_session", date_str="2026-09-18", conn=self.conn)

        self.assertEqual(replay["date"], "2026-09-18")
        self.assertEqual(replay["trades_count"], 3)
        self.assertGreater(len(replay["timeline"]), 0)
        for point in replay["timeline"]:
            self.assertIn("timestamp", point)
            self.assertIn("equity", point)
            self.assertIn("cash", point)
            self.assertIn("event", point)

    def test_equity_curve_snapshots(self):
        """Verify equity curve snapshots reflect initial deployment and subsequent trades."""
        ConsensusGameEngine.deploy_capital(100000.0, session_id="eq_session", conn=self.conn)
        series = ConsensusGameEngine.get_equity_curve(session_id="eq_session", conn=self.conn)

        self.assertGreater(len(series), 1)
        # First point should be initial capital deployment
        self.assertEqual(series[0]["event_type"], "CAPITAL_DEPLOYED")
        self.assertEqual(series[0]["total_equity"], 100000.0)

    def test_gamer_profile_and_badges(self):
        """Verify XP, level calculation, and unlocked badges."""
        state = ConsensusGameEngine.deploy_capital(100000.0, session_id="badge_session", conn=self.conn)
        profile = state.get("gamer_profile", {})
        self.assertIn("xp", profile)
        self.assertIn("level", profile)
        self.assertIn("badges", profile)
        self.assertGreaterEqual(profile["level"], 1)

    def test_upstox_shield_stats(self):
        """Verify UpstoxRateLimiter stats are clean and under limits."""
        limiter = get_upstox_limiter()
        stats = limiter.get_stats()
        self.assertIn("current_1s", stats)
        self.assertIn("circuit_open", stats)
        self.assertFalse(stats["circuit_open"])
        self.assertLessEqual(stats["current_1s"], 8)

    def test_highest_confluence_filter_picks_single_best_trade_per_scan(self):
        """Verify HIGHEST_CONFLUENCE_ONLY accepts exclusively the top-scoring candidate per scan."""
        # Insert a scan batch at 2026-09-21 09:30:00 with 3 open trades of varying confluence scores
        self.cur.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score, status
            ) VALUES
            (101, '2026-09-21 09:30:00', 'LOW_SCORE.NS', 'LOW_SCORE', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 55.1, 'OPEN'),
            (102, '2026-09-21 09:30:00', 'TOP_SCORE.NS', 'TOP_SCORE', 'INTRADAY', 'BUY', 200.0, 198.0, 204.0, 58.8, 'OPEN'),
            (103, '2026-09-21 09:30:00', 'MID_SCORE.NS', 'MID_SCORE', 'INTRADAY', 'BUY', 150.0, 148.5, 153.0, 56.4, 'OPEN');
        """)
        self.conn.commit()

        # Mode A: HIGHEST_CONFLUENCE_ONLY
        state_single = ConsensusGameEngine.deploy_capital(
            100000.0,
            session_id="mode_single_session",
            trade_selection_mode="HIGHEST_CONFLUENCE_ONLY",
            conn=self.conn
        )
        self.assertEqual(state_single["trade_selection_mode"], "HIGHEST_CONFLUENCE_ONLY")
        open_tickers_single = [p["ticker"] for p in state_single["open_positions"]]
        # Must contain TOP_SCORE.NS and reject LOW_SCORE.NS and MID_SCORE.NS from that scan
        self.assertIn("TOP_SCORE.NS", open_tickers_single)
        self.assertNotIn("LOW_SCORE.NS", open_tickers_single)
        self.assertNotIn("MID_SCORE.NS", open_tickers_single)

        # Mode B: ALL_QUALIFIED
        state_all = ConsensusGameEngine.deploy_capital(
            100000.0,
            session_id="mode_all_session",
            trade_selection_mode="ALL_QUALIFIED",
            conn=self.conn
        )
        self.assertEqual(state_all["trade_selection_mode"], "ALL_QUALIFIED")
        open_tickers_all = [p["ticker"] for p in state_all["open_positions"]]
        self.assertIn("TOP_SCORE.NS", open_tickers_all)
        self.assertIn("LOW_SCORE.NS", open_tickers_all)
        self.assertIn("MID_SCORE.NS", open_tickers_all)

    def test_set_trade_selection_mode_toggle_dynamically_resimulates(self):
        """Verify toggling selection mode dynamically re-simulates portfolio state."""
        # Start in ALL_QUALIFIED mode
        state_1 = ConsensusGameEngine.deploy_capital(
            100000.0,
            session_id="toggle_session",
            trade_selection_mode="ALL_QUALIFIED",
            conn=self.conn
        )
        self.assertEqual(state_1["trade_selection_mode"], "ALL_QUALIFIED")

        # Toggle to HIGHEST_CONFLUENCE_ONLY
        state_2 = ConsensusGameEngine.set_trade_selection_mode(
            session_id="toggle_session",
            trade_selection_mode="HIGHEST_CONFLUENCE_ONLY",
            conn=self.conn
        )
        self.assertEqual(state_2["trade_selection_mode"], "HIGHEST_CONFLUENCE_ONLY")

        # Toggle to TIMED_ANCHOR_CONFLUENCE
        state_3 = ConsensusGameEngine.set_trade_selection_mode(
            session_id="toggle_session",
            trade_selection_mode="TIMED_ANCHOR_CONFLUENCE",
            conn=self.conn
        )
        self.assertEqual(state_3["trade_selection_mode"], "TIMED_ANCHOR_CONFLUENCE")

        # Toggle back to ALL_QUALIFIED
        state_4 = ConsensusGameEngine.set_trade_selection_mode(
            session_id="toggle_session",
            trade_selection_mode="ALL_QUALIFIED",
            conn=self.conn
        )
        self.assertEqual(state_4["trade_selection_mode"], "ALL_QUALIFIED")

    def test_timed_anchor_confluence_filters_strictly_to_institutional_windows(self):
        """Verify TIMED_ANCHOR_CONFLUENCE selects exclusively trades from 09:30, 11:30 & 13:30 anchors."""
        self.cur.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score, status
            ) VALUES
            (201, '2026-09-22 09:30:00', 'ANCHOR_0930.NS', 'A0930', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 52.0, 'OPEN'),
            (202, '2026-09-22 10:15:00', 'NOISE_1015.NS', 'N1015', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 59.0, 'OPEN'),
            (203, '2026-09-22 11:30:00', 'ANCHOR_1130.NS', 'A1130', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 53.0, 'OPEN'),
            (204, '2026-09-22 13:30:00', 'ANCHOR_1330.NS', 'A1330', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 51.0, 'OPEN');
        """)
        self.conn.commit()

        state_timed = ConsensusGameEngine.deploy_capital(
            100000.0,
            session_id="timed_anchor_test_session",
            trade_selection_mode="TIMED_ANCHOR_CONFLUENCE",
            conn=self.conn
        )
        self.assertEqual(state_timed["trade_selection_mode"], "TIMED_ANCHOR_CONFLUENCE")
        open_tickers = [p["ticker"] for p in state_timed["open_positions"]]
        self.assertIn("ANCHOR_0930.NS", open_tickers)
        self.assertIn("ANCHOR_1130.NS", open_tickers)
        self.assertIn("ANCHOR_1330.NS", open_tickers)
        # Even though NOISE_1015.NS had a higher score (59.0 vs 52.0), it was outside timed anchors and must be filtered out
        self.assertNotIn("NOISE_1015.NS", open_tickers)

    def test_point_in_time_slot_capacity_replays_under_max_positions(self):
        """Verify that in ALL_QUALIFIED mode, portfolio never exceeds max_positions concurrent active positions."""
        # Insert 7 trades all opening at 09:30, but max_positions is 5
        self.cur.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                status, outcome, profit_pct, exit_price, exit_time
            ) VALUES
            (201, '2026-09-22 09:30:00', 'T1.NS', 'T1', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 60.0, 'CLOSED', 'TARGET MET', 1.0, 101.0, '2026-09-22 11:00:00'),
            (202, '2026-09-22 09:30:00', 'T2.NS', 'T2', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 59.0, 'CLOSED', 'TARGET MET', 1.0, 101.0, '2026-09-22 11:00:00'),
            (203, '2026-09-22 09:30:00', 'T3.NS', 'T3', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 58.0, 'CLOSED', 'TARGET MET', 1.0, 101.0, '2026-09-22 11:00:00'),
            (204, '2026-09-22 09:30:00', 'T4.NS', 'T4', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 57.0, 'CLOSED', 'TARGET MET', 1.0, 101.0, '2026-09-22 11:00:00'),
            (205, '2026-09-22 09:30:00', 'T5.NS', 'T5', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 56.0, 'CLOSED', 'TARGET MET', 1.0, 101.0, '2026-09-22 11:00:00'),
            (206, '2026-09-22 09:30:00', 'T6.NS', 'T6', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 55.0, 'CLOSED', 'TARGET MET', 1.0, 101.0, '2026-09-22 11:00:00'),
            (207, '2026-09-22 09:30:00', 'T7.NS', 'T7', 'INTRADAY', 'BUY', 100.0, 99.0, 102.0, 54.0, 'CLOSED', 'TARGET MET', 1.0, 101.0, '2026-09-22 11:00:00');
        """)
        self.conn.commit()

        state = ConsensusGameEngine.deploy_capital(
            100000.0,
            session_id="capacity_session",
            max_positions=5,
            trade_selection_mode="ALL_QUALIFIED",
            conn=self.conn
        )
        positions = ConsensusGameEngine.get_positions(session_id="capacity_session", conn=self.conn)
        pos_tickers = [p["ticker"] for p in positions]
        
        # Of the 7 trades competing at 09:30, exactly 5 (T1..T5) entered and T6, T7 were blocked
        # (plus the 3 base trades from 09-18 which exited on 09-18)
        self.assertIn("T1.NS", pos_tickers)
        self.assertIn("T2.NS", pos_tickers)
        self.assertIn("T3.NS", pos_tickers)
        self.assertIn("T4.NS", pos_tickers)
        self.assertIn("T5.NS", pos_tickers)
        self.assertNotIn("T6.NS", pos_tickers)
        self.assertNotIn("T7.NS", pos_tickers)

        # Verify all snapshots never recorded open_positions_count > 5
        snaps = ConsensusGameEngine.get_equity_curve(session_id="capacity_session", conn=self.conn)
        for s in snaps:
            self.assertLessEqual(s["open_positions"], 5)

    @patch("app.analytics.telegram_notifier.send_telegram_message")
    def test_arena_slot_entry_dispatches_telegram_alert(self, mock_telegram):
        """Validates that a live candidate claiming an arena slot sends a formatted Telegram alert."""
        from datetime import datetime
        mock_telegram.return_value = True
        today_str = datetime.now().strftime("%Y-%m-%d")
        now_ts = f"{today_str} 10:15:00"

        # Insert a high-confluence trade for today
        self.cur.execute("""
            INSERT INTO consensus_trade_history (
                timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                qlib_score, legacy_score, finbert_score, risk_reward_ratio,
                status, source, position_type
            ) VALUES (
                ?, 'TCS.NS', 'TCS', 'INTRADAY', 'BUY',
                3800.0, 3760.0, 3880.0, 82.5,
                0.015, 80.0, 0.65, 2.0,
                'OPEN', 'MANUAL', 'NOT_A_POSITION'
            )
        """, (now_ts,))
        self.conn.commit()

        # Run sync and dispatch
        res = ConsensusGameEngine.sync_and_dispatch_arena_alerts(session_id="test_alert_session", conn=self.conn)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertGreaterEqual(res["entry_alerts_sent"], 1)

        # Verify telegram was called
        mock_telegram.assert_called()
        sent_msg = mock_telegram.call_args[0][0]
        self.assertIn("CONSENSUS ARENA: NEW SLOT OCCUPIED", sent_msg)
        self.assertIn("TCS.NS", sent_msg)
        self.assertIn("BUY", sent_msg)
        self.assertIn("Slot 1 of 5", sent_msg)
        self.assertIn("82.5%", sent_msg)

        # Verify record in consensus_game_alerts
        alert_rows = self.cur.execute("""
            SELECT alert_type, ticker FROM consensus_game_alerts WHERE session_id = 'test_alert_session'
        """).fetchall()
        self.assertIn(('SLOT_ENTRY', 'TCS.NS'), alert_rows)

    @patch("app.analytics.telegram_notifier.send_telegram_message")
    def test_arena_slot_entry_deduplication(self, mock_telegram):
        """Validates that running sync twice does not duplicate the slot entry alert."""
        from datetime import datetime
        mock_telegram.return_value = True
        today_str = datetime.now().strftime("%Y-%m-%d")
        now_ts = f"{today_str} 11:00:00"

        self.cur.execute("""
            INSERT INTO consensus_trade_history (
                timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                qlib_score, legacy_score, finbert_score, risk_reward_ratio,
                status, source, position_type
            ) VALUES (
                ?, 'INFY.NS', 'INFY', 'INTRADAY', 'BUY',
                1850.0, 1830.0, 1890.0, 78.0,
                0.012, 76.0, 0.45, 2.0,
                'OPEN', 'MANUAL', 'NOT_A_POSITION'
            )
        """, (now_ts,))
        self.conn.commit()

        # First run: should send
        res1 = ConsensusGameEngine.sync_and_dispatch_arena_alerts(session_id="test_dedup_session", conn=self.conn)
        self.assertEqual(res1["entry_alerts_sent"], 1)
        initial_calls = mock_telegram.call_count

        # Second run: should NOT resend
        res2 = ConsensusGameEngine.sync_and_dispatch_arena_alerts(session_id="test_dedup_session", conn=self.conn)
        self.assertEqual(res2["entry_alerts_sent"], 0)
        self.assertEqual(mock_telegram.call_count, initial_calls)

    @patch("app.analytics.telegram_notifier.send_telegram_message")
    def test_arena_slot_exit_dispatches_telegram_alert(self, mock_telegram):
        """Validates that exiting a position dispatches a formatted exit alert with net P&L and freed slot info."""
        from datetime import datetime
        mock_telegram.return_value = True
        today_str = datetime.now().strftime("%Y-%m-%d")
        now_ts = f"{today_str} 09:45:00"

        self.cur.execute("""
            INSERT INTO consensus_trade_history (
                timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                qlib_score, legacy_score, finbert_score, risk_reward_ratio,
                status, source, position_type
            ) VALUES (
                ?, 'RELIANCE.NS', 'RELIANCE', 'INTRADAY', 'BUY',
                2900.0, 2870.0, 2960.0, 85.0,
                0.020, 84.0, 0.80, 2.0,
                'OPEN', 'MANUAL', 'NOT_A_POSITION'
            )
        """, (now_ts,))
        self.conn.commit()

        # Step 1: Slot entry
        ConsensusGameEngine.sync_and_dispatch_arena_alerts(session_id="test_exit_session", conn=self.conn)
        mock_telegram.reset_mock()

        # Step 2: Retrieve open position
        open_pos = self.cur.execute("""
            SELECT id FROM consensus_game_positions WHERE session_id = 'test_exit_session' AND status = 'OPEN'
        """).fetchone()
        self.assertIsNotNone(open_pos)
        pos_id = open_pos[0]

        # Step 3: Exit position at Target Price (2960)
        exit_res = ConsensusGameEngine.exit_position(
            position_id=pos_id,
            session_id="test_exit_session",
            exit_price=2960.0,
            reason="TARGET MET",
            conn=self.conn
        )
        self.assertEqual(exit_res["status"], "SUCCESS")

        # Step 4: Verify exit telegram alert was sent
        mock_telegram.assert_called()
        exit_msg = mock_telegram.call_args[0][0]
        self.assertIn("CONSENSUS ARENA: SLOT FREED / EXIT", exit_msg)
        self.assertIn("RELIANCE.NS", exit_msg)
        self.assertIn("TARGET MET", exit_msg)
        self.assertIn("Net Realized P&L", exit_msg)
        self.assertIn("Slot Freed", exit_msg)

        # Verify alert record in consensus_game_alerts
        alert_rows = self.cur.execute("""
            SELECT alert_type, ticker FROM consensus_game_alerts WHERE session_id = 'test_exit_session' AND alert_type = 'SLOT_EXIT'
        """).fetchall()
        self.assertIn(('SLOT_EXIT', 'RELIANCE.NS'), alert_rows)
        self.assertEqual(mock_telegram.call_count, 1)

    @patch("app.analytics.telegram_notifier.send_telegram_message")
    def test_historical_replays_do_not_send_live_alerts(self, mock_telegram):
        """Validates that historical trades from past dates do NOT trigger live Telegram alert spam."""
        mock_telegram.return_value = True

        # Insert ancient trade from 2025
        self.cur.execute("""
            INSERT INTO consensus_trade_history (
                timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                qlib_score, legacy_score, finbert_score, risk_reward_ratio,
                status, outcome, profit_pct, exit_price, exit_time, source, position_type
            ) VALUES (
                '2025-01-15 10:00:00', 'OLD_HIST.NS', 'OLD_HIST', 'INTRADAY', 'BUY',
                1000.0, 980.0, 1040.0, 90.0,
                0.03, 88.0, 0.5, 2.0,
                'CLOSED', 'TARGET MET', 4.0, 1040.0, '2025-01-15 14:00:00',
                'MANUAL', 'NOT_A_POSITION'
            )
        """)
        self.conn.commit()

        # Run alert sync
        res = ConsensusGameEngine.sync_and_dispatch_arena_alerts(session_id="test_hist_session", conn=self.conn)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["entry_alerts_sent"], 0)
        self.assertEqual(res["exit_alerts_sent"], 0)
        mock_telegram.assert_not_called()

    @patch("app.analytics.telegram_notifier.send_telegram_message")
    def test_yesterdays_entry_alerted_trades_do_not_dispatch_stale_exit_today(self, mock_telegram):
        """
        REGRESSION TEST FOR OCTOBER 1 BUG:
        A trade was entered and entry-alerted yesterday (2026-09-30), and closed yesterday at 15:15.
        When sync_and_dispatch_arena_alerts runs today (2026-10-01), it MUST NOT dispatch an exit alert.
        """
        mock_telegram.return_value = True
        ConsensusGameEngine.init_game_schema(self.conn)

        # 1. Insert trade from yesterday that closed yesterday
        self.cur.execute("""
            INSERT INTO consensus_trade_history (
                id, timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                qlib_score, legacy_score, finbert_score, risk_reward_ratio,
                status, outcome, profit_pct, exit_price, exit_time, source, position_type
            ) VALUES (
                999, '2026-09-30 10:30:00', 'YESTERDAY_STOCK.NS', 'YESTERDAY_STOCK', 'INTRADAY', 'BUY',
                500.0, 490.0, 520.0, 85.0,
                0.02, 80.0, 0.5, 2.0,
                'CLOSED', 'SQUARED OFF', -0.5, 497.5, '2026-09-30 15:15:50',
                'MANUAL', 'NOT_A_POSITION'
            )
        """)

        # 2. Record that it HAD a live SLOT_ENTRY alert sent yesterday
        self.cur.execute("""
            INSERT INTO consensus_game_alerts (
                session_id, trade_ref_id, ticker, alert_type, sent_at, details_json
            ) VALUES (
                'test_stale_exit_session', 999, 'YESTERDAY_STOCK.NS', 'SLOT_ENTRY', '2026-09-30 10:30:05',
                '{"is_live": true, "entry_time": "2026-09-30 10:30:00"}'
            )
        """)
        self.conn.commit()

        # 3. Synchronize and dispatch alerts (simulating morning sync next day)
        res = ConsensusGameEngine.sync_and_dispatch_arena_alerts(session_id="test_stale_exit_session", conn=self.conn)

        # 4. Verify ZERO exit alerts were dispatched
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["exit_alerts_sent"], 0, "Stale yesterday exit alert must NOT be sent to Telegram!")
        mock_telegram.assert_not_called()

        # 5. Verify the alert is now cleanly silenced in consensus_game_alerts with is_live=False
        alert_row = self.cur.execute("""
            SELECT alert_type, details_json FROM consensus_game_alerts
            WHERE session_id = 'test_stale_exit_session' AND trade_ref_id = 999 AND alert_type = 'SLOT_EXIT'
        """).fetchone()
        self.assertIsNotNone(alert_row)
        self.assertIn('"is_live": false', alert_row[1].lower())


if __name__ == "__main__":
    unittest.main()
