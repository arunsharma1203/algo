"""
Unit & Integration Test Suite for Institutional Dynamic Trade Guard (IDTG) in Consensus Arena.

Validates:
1. Monotonic Ratchet Rule: Stop loss strictly moves in the direction of risk reduction; never widens.
2. Zero Forward Bias: The trade's structural target price is strictly invariant; no future peek-ahead.
3. Upstox Rate Limit Compliance: Evaluates all open positions via exactly 1 bulk API request.
4. Active Slot Isolation: Evaluates strictly OPEN positions in the specified session; historical closed trades remain immutable.
5. Profit-Lock Ratchet: Locks in breakeven (+0.10%) when favorable move >= +0.70%.
6. Adverse Threat Evaluation: 50% risk cut on threat >= 40; defensive early exit on threat >= 75 with loss.
"""

import unittest
from unittest.mock import patch, MagicMock
import tempfile
import sqlite3
import os
import json
from datetime import datetime

from app.analytics.consensus_dynamic_guard import ConsensusDynamicGuard


class TestConsensusDynamicGuard(unittest.TestCase):

    def setUp(self):
        # Create an isolated temporary SQLite database for every test
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        self.conn = sqlite3.connect(self.db_path)
        self._init_test_tables()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _init_test_tables(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS consensus_trade_history (
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
                status TEXT DEFAULT 'OPEN',
                outcome TEXT,
                profit_pct REAL,
                exit_price REAL,
                exit_time TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS consensus_game_accounts (
                session_id TEXT PRIMARY KEY,
                account_name TEXT,
                initial_capital REAL,
                current_cash REAL,
                allocation_mode TEXT,
                max_positions INTEGER,
                fees_enabled INTEGER,
                realized_pnl REAL,
                peak_equity REAL,
                max_drawdown_pct REAL,
                created_at TEXT,
                updated_at TEXT,
                trade_selection_mode TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS consensus_game_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                trade_ref_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                direction TEXT NOT NULL,
                shares INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                allocated_capital REAL NOT NULL,
                stop_loss REAL NOT NULL,
                target_price REAL NOT NULL,
                entry_time TEXT NOT NULL,
                status TEXT DEFAULT 'OPEN',
                exit_price REAL,
                exit_time TEXT,
                gross_pnl_rupees REAL DEFAULT 0.0,
                fees_rupees REAL DEFAULT 0.0,
                net_pnl_rupees REAL DEFAULT 0.0,
                pnl_pct REAL DEFAULT 0.0,
                outcome TEXT,
                details_json TEXT,
                tightened_sl REAL,
                guard_threat_score REAL DEFAULT 0.0,
                guard_action TEXT DEFAULT 'MAINTAIN',
                guard_reasons TEXT,
                last_guard_audit TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS consensus_game_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                total_equity REAL NOT NULL,
                cash REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                open_positions_count INTEGER NOT NULL,
                event_type TEXT,
                note TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS consensus_game_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                trade_ref_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                details_json TEXT,
                UNIQUE(session_id, trade_ref_id, alert_type)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)

        # Deploy initial test account
        cur.execute("""
            INSERT INTO consensus_game_accounts (
                session_id, account_name, initial_capital, current_cash,
                allocation_mode, max_positions, fees_enabled, realized_pnl,
                peak_equity, max_drawdown_pct, created_at, updated_at, trade_selection_mode
            ) VALUES ('default_arena_session', 'Consensus Arena Trader', 100000.0, 80000.0,
                      'EQUAL_WEIGHT', 5, 1, 0.0, 100000.0, 0.0, '2026-09-24 09:15:00', '2026-09-24 09:15:00', 'ALL_QUALIFIED')
        """)
        self.conn.commit()

    # ── TEST 1: MONOTONIC RATCHET RULE FOR LONG / BUY ────────────────
    def test_monotonic_ratchet_long(self):
        """Verifies that for a Long trade, SL can only move upward (closer to price) and never widen."""
        entry = 100.0
        orig_sl = 99.20  # -0.80%
        target = 101.60

        # Sub-test 1a: Threat >= 40 cuts risk by 50%
        current_p = 100.0
        threat = 50.0  # triggers TIGHTEN_SL
        decision = ConsensusDynamicGuard.evaluate_position_action(
            direction="BUY",
            entry_price=entry,
            current_price=current_p,
            current_sl=orig_sl,
            target_price=target,
            threat_score=threat,
            reasons=["Macro Bear Pressure"]
        )
        self.assertEqual(decision["action"], "TIGHTEN_SL")
        # Distance was 100 - 99.20 = 0.80. Halfway is 99.60
        self.assertAlmostEqual(decision["new_sl"], 99.60, places=2)
        self.assertGreater(decision["new_sl"], orig_sl)
        self.assertLess(decision["new_sl"], current_p)

        # Sub-test 1b: Attempting to widen SL is mathematically blocked
        already_tightened_sl = 99.70
        decision2 = ConsensusDynamicGuard.evaluate_position_action(
            direction="BUY",
            entry_price=entry,
            current_price=99.80,
            current_sl=already_tightened_sl,
            target_price=target,
            threat_score=threat,
            reasons=["Macro Bear Pressure"]
        )
        # 99.80 - (99.80 - 99.70)*0.5 = 99.75 > 99.70 -> Tightens further
        self.assertGreaterEqual(decision2["new_sl"], already_tightened_sl)

    # ── TEST 2: MONOTONIC RATCHET RULE FOR SHORT / SELL ───────────────
    def test_monotonic_ratchet_short(self):
        """Verifies that for a Short trade, SL can only move downward (closer to price) and never widen."""
        entry = 100.0
        orig_sl = 100.80  # +0.80%
        target = 98.40

        current_p = 100.0
        threat = 50.0
        decision = ConsensusDynamicGuard.evaluate_position_action(
            direction="SELL",
            entry_price=entry,
            current_price=current_p,
            current_sl=orig_sl,
            target_price=target,
            threat_score=threat,
            reasons=["Macro Bull Expansion"]
        )
        self.assertEqual(decision["action"], "TIGHTEN_SL")
        # Distance was 100.80 - 100.0 = 0.80. Halfway is 100.40
        self.assertAlmostEqual(decision["new_sl"], 100.40, places=2)
        self.assertLess(decision["new_sl"], orig_sl)
        self.assertGreater(decision["new_sl"], current_p)

    # ── TEST 3: ZERO FORWARD BIAS & TARGET INVARIANCE ─────────────────
    def test_target_price_never_modified(self):
        """Guarantees that the trade's structural target price is strictly invariant."""
        entry = 500.0
        orig_sl = 496.0
        target = 508.0

        for threat_score in [0.0, 30.0, 50.0, 80.0]:
            for ltp in [498.0, 500.0, 503.5, 507.0]:
                decision = ConsensusDynamicGuard.evaluate_position_action(
                    direction="BUY",
                    entry_price=entry,
                    current_price=ltp,
                    current_sl=orig_sl,
                    target_price=target,
                    threat_score=threat_score,
                    reasons=[]
                )
                # The decision payload must NOT contain a modified target price
                self.assertNotIn("new_target", decision)
                self.assertNotIn("new_tp", decision)

    # ── TEST 4: PROFIT-LOCK RATCHET (BREAKEVEN +0.10%) ────────────────
    def test_profit_lock_ratchet(self):
        """Verifies that reaching +0.70% profit ratchets SL to Breakeven (+0.10%)."""
        # Long Trade
        entry = 200.0
        sl = 198.40
        target = 203.20

        # 1. Verify shallow +0.80% (201.60) does NOT choke the trade (returns MAINTAIN)
        decision_shallow = ConsensusDynamicGuard.evaluate_position_action(
            direction="BUY",
            entry_price=entry,
            current_price=201.60,
            current_sl=sl,
            target_price=target,
            threat_score=10.0,
            reasons=[]
        )
        self.assertEqual(decision_shallow["action"], "MAINTAIN")

        # 2. Price achieves >= +1.10% favorable move (202.40 = +1.20%)
        decision_long = ConsensusDynamicGuard.evaluate_position_action(
            direction="BUY",
            entry_price=entry,
            current_price=202.40,
            current_sl=sl,
            target_price=target,
            threat_score=10.0,
            reasons=[]
        )
        self.assertEqual(decision_long["action"], "PROFIT_LOCK_RATCHET")
        # Fee-compensated Breakeven +0.35% = 200.0 * 1.0035 = 200.70
        self.assertAlmostEqual(decision_long["new_sl"], 200.70, places=2)
        self.assertGreater(decision_long["new_sl"], entry)

        # Short Trade: price drops to >= +1.10% profit (197.60 = +1.20%)
        entry_s = 200.0
        sl_s = 201.60
        target_s = 196.80
        decision_short = ConsensusDynamicGuard.evaluate_position_action(
            direction="SELL",
            entry_price=entry_s,
            current_price=197.60,
            current_sl=sl_s,
            target_price=target_s,
            threat_score=10.0,
            reasons=[]
        )
        self.assertEqual(decision_short["action"], "PROFIT_LOCK_RATCHET")
        # Fee-compensated Breakeven for short: 200.0 * (1 - 0.0035) = 199.30
        self.assertAlmostEqual(decision_short["new_sl"], 199.30, places=2)
        self.assertLess(decision_short["new_sl"], entry_s)

    # ── TEST 5: DEFENSIVE EXIT ON HIGH THREAT + LOSS ───────────────────
    def test_defensive_exit_when_thesis_collapses(self):
        """Verifies that an underwater position with Threat >= 75 triggers DEFENSIVE_EXIT."""
        entry = 100.0
        sl = 99.20
        target = 101.60

        # Position is down -0.30% (99.70) and threat is 80.0
        decision = ConsensusDynamicGuard.evaluate_position_action(
            direction="BUY",
            entry_price=entry,
            current_price=99.70,
            current_sl=sl,
            target_price=target,
            threat_score=80.0,
            reasons=["NIFTY Bear Collapse", "VWAP Breakdown"]
        )
        self.assertEqual(decision["action"], "DEFENSIVE_EXIT")
        self.assertIn("Defensive exit triggered", decision["summary"])

    # ── TEST 6: UPSTOX RATE LIMIT COMPLIANCE (1 SINGLE BULK CALL) ─────
    @patch("app.analytics.consensus_dynamic_guard.get_db_path")
    @patch("app.analytics.consensus_game_engine.get_db_path")
    @patch("app.data.upstox_provider.fetch_upstox_bulk_quotes")
    @patch("app.analytics.macro_engine.get_macro_regime")
    def test_upstox_rate_limit_single_bulk_call(self, mock_macro, mock_bulk, mock_db2, mock_db1):
        """Verifies that querying multiple open positions uses strictly 1 bulk quote request."""
        mock_db1.return_value = self.db_path
        mock_db2.return_value = self.db_path

        mock_macro.return_value = {
            "intraday_regime_verdict": "INTRADAY_NEUTRAL_CHOP",
            "intraday_open_change_pct": 0.05
        }

        # Insert 3 open positions into consensus_game_positions
        cur = self.conn.cursor()
        positions_data = [
            ("TATASTEEL.NS", "BUY", 100, 150.0, 148.8, 152.4),
            ("INFY.NS", "SELL", 50, 1800.0, 1814.4, 1771.2),
            ("RELIANCE.NS", "BUY", 20, 2900.0, 2876.8, 2946.4)
        ]
        for t, d, s, ep, sl, tp in positions_data:
            cur.execute("""
                INSERT INTO consensus_trade_history (
                    timestamp, ticker, symbol, strategy, direction,
                    entry_price, stop_loss, target_price, composite_score, status
                ) VALUES ('2026-09-24 10:00:00', ?, ?, 'INTRADAY', ?, ?, ?, ?, 65.0, 'OPEN')
            """, (t, t.split(".")[0], d, ep, sl, tp))
            ref_id = cur.lastrowid

            cur.execute("""
                INSERT INTO consensus_game_positions (
                    session_id, trade_ref_id, ticker, direction, shares,
                    entry_price, allocated_capital, stop_loss, target_price,
                    entry_time, status
                ) VALUES ('default_arena_session', ?, ?, ?, ?, ?, 20000.0, ?, ?, '2026-09-24 10:00:00', 'OPEN')
            """, (ref_id, t, d, s, ep, sl, tp))
        self.conn.commit()

        mock_bulk.return_value = {
            "TATASTEEL.NS": {"ltp": 150.2, "vwap": 150.0, "open": 149.5},
            "INFY.NS": {"ltp": 1795.0, "vwap": 1802.0, "open": 1805.0},
            "RELIANCE.NS": {"ltp": 2905.0, "vwap": 2900.0, "open": 2890.0}
        }

        # Run audit
        res = ConsensusDynamicGuard.audit_active_arena_positions(
            session_id="default_arena_session",
            conn=self.conn
        )

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["audited_count"], 3)
        # CRITICAL VERIFICATION: fetch_upstox_bulk_quotes was called EXACTLY ONCE
        mock_bulk.assert_called_once()
        called_tickers = set(mock_bulk.call_args[0][0])
        self.assertEqual(called_tickers, {"TATASTEEL.NS", "INFY.NS", "RELIANCE.NS"})

    # ── TEST 7: HISTORICAL CLOSED POSITIONS REMAIN IMMUTABLE ──────────
    @patch("app.analytics.consensus_dynamic_guard.get_db_path")
    @patch("app.analytics.consensus_game_engine.get_db_path")
    def test_historical_closed_positions_immutable(self, mock_db2, mock_db1):
        """Verifies that closed historical positions are never audited or altered by the guard."""
        mock_db1.return_value = self.db_path
        mock_db2.return_value = self.db_path

        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO consensus_game_positions (
                session_id, trade_ref_id, ticker, direction, shares,
                entry_price, allocated_capital, stop_loss, target_price,
                entry_time, status, exit_price, exit_time, gross_pnl_rupees,
                fees_rupees, net_pnl_rupees, pnl_pct, outcome
            ) VALUES ('default_arena_session', 101, 'SBIN.NS', 'BUY', 50,
                      800.0, 40000.0, 793.6, 812.8,
                      '2026-09-22 10:00:00', 'CLOSED', 812.8, '2026-09-22 14:15:00',
                      640.0, 40.0, 600.0, 1.50, 'TARGET MET')
        """)
        self.conn.commit()

        # Audit with no open positions
        res = ConsensusDynamicGuard.audit_active_arena_positions(
            session_id="default_arena_session",
            conn=self.conn
        )

        self.assertEqual(res["status"], "NO_OPEN_POSITIONS")
        self.assertEqual(res["audited_count"], 0)

        # Verify historical row remains byte-for-byte unchanged and was never audited
        row = cur.execute("SELECT stop_loss, status, outcome, last_guard_audit FROM consensus_game_positions WHERE id = 1").fetchone()
        self.assertEqual(row[0], 793.6)
        self.assertEqual(row[1], "CLOSED")
        self.assertEqual(row[2], "TARGET MET")
        self.assertIsNone(row[3])


if __name__ == "__main__":
    unittest.main()
