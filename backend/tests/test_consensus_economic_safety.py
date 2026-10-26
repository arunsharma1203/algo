"""
UNIT TESTS: CONSENSUS ECONOMIC SAFETY & RECONCILIATION
======================================================
Tests the immediate Phase 1 safety and economic correctness fixes:
1. Hard Session Guard (pre-market, post-market, weekend rejection).
2. Entry Cutoff Gate (15:00 default cutoff, configurable window).
3. True Economic Break-Even calculation (covers friction, non-negative net).
4. Explicit canonical exit labels and mathematical trade reconciliation.
"""

import os
import unittest
import sqlite3
import tempfile
from datetime import datetime, time
from unittest.mock import patch

from app.analytics.consensus_economic_engine import (
    ConsensusEconomicEngine,
    DEFAULT_ENTRY_CUTOFF_TIME,
    ROUNDTRIP_BROKERAGE
)
from app.analytics.consensus_tracker import ConsensusTracker
from app.analytics.consensus_game_engine import ConsensusGameEngine


class TestConsensusEconomicSafety(unittest.TestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        self.patcher_db1 = patch("app.analytics.consensus_tracker.get_db_path", return_value=self.db_path)
        self.patcher_db2 = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.patcher_db3 = patch("app.analytics.consensus_game_engine.get_db_path", return_value=self.db_path)
        self.patcher_live = patch("app.data.market_provider.get_bulk_live_quotes_with_meta", return_value={})

        self.patcher_db1.start()
        self.patcher_db2.start()
        self.patcher_db3.start()
        self.patcher_live.start()

        conn = sqlite3.connect(self.db_path)
        from app.api.ml_history import ensure_ml_table
        ensure_ml_table()
        ConsensusTracker.init_consensus_tracking_schema(conn)
        ConsensusGameEngine.init_game_schema(conn)
        conn.close()

    def tearDown(self):
        self.patcher_db1.stop()
        self.patcher_db2.stop()
        self.patcher_db3.stop()
        self.patcher_live.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    # ── 1. HARD SESSION GUARD TESTS ──────────────────────────────────────────

    def test_session_guard_rejects_weekend(self):
        """Weekend timestamps (Saturday, Sunday) must be strictly rejected."""
        saturday_ts = "2026-10-03 10:30:00"  # Oct 3, 2026 is Saturday
        is_ok, reason = ConsensusEconomicEngine.is_valid_trading_session(saturday_ts)
        self.assertFalse(is_ok)
        self.assertIn("SESSION_CLOSED_WEEKEND", reason)

        sunday_ts = "2026-10-04 11:00:00"  # Oct 4, 2026 is Sunday
        is_ok, reason = ConsensusEconomicEngine.is_valid_trading_session(sunday_ts)
        self.assertFalse(is_ok)
        self.assertIn("SESSION_CLOSED_WEEKEND", reason)

    def test_session_guard_rejects_premarket(self):
        """Entries before 09:15:00 IST must be strictly rejected."""
        pre_market_ts = "2026-10-05 08:45:00"  # Monday 08:45
        is_ok, reason = ConsensusEconomicEngine.is_valid_trading_session(pre_market_ts)
        self.assertFalse(is_ok)
        self.assertIn("SESSION_PREMARKET_REJECTED", reason)

    def test_session_guard_rejects_afterhours(self):
        """Entries after 15:30:00 IST (like the HDBFS 15:18 or JSWSTEEL 19:01 bug) must be rejected."""
        post_close_ts = "2026-10-05 15:31:00"
        is_ok, reason = ConsensusEconomicEngine.is_valid_trading_session(post_close_ts)
        self.assertFalse(is_ok)
        self.assertIn("SESSION_AFTERHOURS_REJECTED", reason)

        evening_ts = "2026-10-01 19:01:35"
        is_ok, reason = ConsensusEconomicEngine.is_valid_trading_session(evening_ts)
        self.assertFalse(is_ok)
        self.assertIn("SESSION_AFTERHOURS_REJECTED", reason)

    def test_session_guard_allows_active_session(self):
        """Valid trading hours (Monday-Friday 09:15 to 15:30) must be accepted."""
        active_ts = "2026-10-05 10:30:00"
        is_ok, reason = ConsensusEconomicEngine.is_valid_trading_session(active_ts)
        self.assertTrue(is_ok)
        self.assertEqual(reason, "ACTIVE_SESSION")

    # ── 2. ENTRY CUTOFF GATE TESTS ───────────────────────────────────────────

    def test_entry_cutoff_gate_blocks_late_entries(self):
        """Entries at or past the cutoff time (default 15:00:00) must be rejected as ENTRY_CUTOFF_REJECTED."""
        late_ts = "2026-10-05 15:05:00"
        is_breached, reason = ConsensusEconomicEngine.is_entry_cutoff_breached(late_ts)
        self.assertTrue(is_breached)
        self.assertIn("ENTRY_CUTOFF_REJECTED", reason)

        at_cutoff_ts = "2026-10-05 15:00:00"
        is_breached, reason = ConsensusEconomicEngine.is_entry_cutoff_breached(at_cutoff_ts)
        self.assertTrue(is_breached)
        self.assertIn("ENTRY_CUTOFF_REJECTED", reason)

    def test_entry_cutoff_gate_permits_timely_entries(self):
        """Entries before the cutoff must be allowed."""
        valid_ts = "2026-10-05 14:45:00"
        is_breached, reason = ConsensusEconomicEngine.is_entry_cutoff_breached(valid_ts)
        self.assertFalse(is_breached)
        self.assertEqual(reason, "WITHIN_ENTRY_WINDOW")

    def test_entry_cutoff_gate_custom_time(self):
        """Cutoff gate must be configurable (e.g. 14:15:00)."""
        custom_cutoff = time(14, 15, 0)
        ts_1410 = "2026-10-05 14:10:00"
        ts_1420 = "2026-10-05 14:20:00"

        breached_1410, _ = ConsensusEconomicEngine.is_entry_cutoff_breached(ts_1410, cutoff_time=custom_cutoff)
        self.assertFalse(breached_1410)

        breached_1420, reason_1420 = ConsensusEconomicEngine.is_entry_cutoff_breached(ts_1420, cutoff_time=custom_cutoff)
        self.assertTrue(breached_1420)
        self.assertIn("ENTRY_CUTOFF_REJECTED", reason_1420)

    def test_tracker_save_enforces_session_and_cutoff_gates(self):
        """save_consensus_trade must block after-hours and cutoff entries immediately."""
        trade = {
            "ticker": "HDBFS.NS",
            "direction": "BUY",
            "blended_entry": 621.55,
            "blended_sl": 615.0,
            "blended_tp": 635.0,
            "composite_score": 80.0,
            "timestamp": "2026-10-05 15:18:15"  # Past 15:00 cutoff
        }
        saved, reason = ConsensusTracker.save_consensus_trade(trade, strategy="INTRADAY")
        self.assertFalse(saved)
        self.assertIn("ENTRY_CUTOFF_REJECTED", reason)

    # ── 3. TRUE ECONOMIC BREAK-EVEN CALCULATION TESTS ────────────────────────

    def test_economic_breakeven_covers_all_friction_buy(self):
        """
        For a BUY order of 20 shares at ₹1,000 (Capital ₹20,000):
        Round-trip brokerage = ₹40.
        Slippage + statutory fees = ~₹20 to ₹30.
        Total friction = ~₹70.
        Economic Breakeven Price must be at least ₹1003.50 (+0.35%).
        """
        entry = 1000.0
        shares = 20
        be_price = ConsensusEconomicEngine.calculate_economic_breakeven_price(
            entry_price=entry,
            shares=shares,
            direction="BUY",
            min_desired_net_profit=0.0
        )
        self.assertGreaterEqual(be_price, 1003.50)

        # Verify that exiting at be_price produces gross profit >= estimated fees
        gross_pnl = shares * (be_price - entry)
        friction = ConsensusEconomicEngine.estimate_roundtrip_friction(entry, shares)
        self.assertGreaterEqual(gross_pnl, friction - 1.0)  # within penny tolerance

    def test_economic_breakeven_covers_all_friction_sell(self):
        """
        For a SELL order of 20 shares at ₹1,000 (Capital ₹20,000):
        Economic Breakeven Price must be at most ₹996.50 (-0.35%).
        """
        entry = 1000.0
        shares = 20
        be_price = ConsensusEconomicEngine.calculate_economic_breakeven_price(
            entry_price=entry,
            shares=shares,
            direction="SELL",
            min_desired_net_profit=0.0
        )
        self.assertLessEqual(be_price, 996.50)

        gross_pnl = shares * (entry - be_price)
        friction = ConsensusEconomicEngine.estimate_roundtrip_friction(entry, shares)
        self.assertGreaterEqual(gross_pnl, friction - 1.0)

    def test_uncompensated_breakeven_would_lose_money(self):
        """
        Demonstrates why the old arbitrary +0.10% breakeven resulted in a losing streak:
        +0.10% on ₹20,000 gives +₹20 gross, but friction is ~₹60-70 -> guarantees net loss.
        """
        entry = 1000.0
        shares = 20
        old_bad_be = entry * 1.0010  # +0.10% = 1001.0
        gross = shares * (old_bad_be - entry)  # +₹20.00
        friction = ConsensusEconomicEngine.estimate_roundtrip_friction(entry, shares)  # ~₹70
        net = gross - friction
        self.assertLess(net, 0.0, "Old uncompensated breakeven correctly identified as guaranteed loser")

    # ── 4. TRADE ACCOUNTING RECONCILIATION TESTS ─────────────────────────────

    def test_reconciliation_math_invariance(self):
        """
        Mathematical proof of zero accounting leaks:
        sum(gross) - sum(fees) == sum(net)
        sum(category counts) == total closed trades
        """
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        # Seed synthetic realistic closed trades across all terminal categories
        trades = [
            ("TARGET MET", 1000.0, 1020.0, 400.0, 60.0, 340.0),       # TARGET
            ("ECONOMIC_BREAKEVEN", 1000.0, 1003.5, 70.0, 60.0, 10.0), # ECONOMIC_BREAKEVEN
            ("PROFIT_LOCK", 1000.0, 1010.0, 200.0, 60.0, 140.0),       # PROFIT_LOCK
            ("SL HIT", 1000.0, 985.0, -300.0, 60.0, -360.0),          # STOP
            ("SESSION_EXIT", 1000.0, 1001.0, 20.0, 60.0, -40.0),      # SESSION_EXIT
        ]

        for i, (outcome, en, ex, gross, fee, net) in enumerate(trades, 1):
            cur.execute("""
                INSERT INTO consensus_game_positions (
                    session_id, trade_ref_id, ticker, direction, shares,
                    entry_price, allocated_capital, stop_loss, target_price,
                    entry_time, status, exit_price, exit_time,
                    gross_pnl_rupees, fees_rupees, net_pnl_rupees, pnl_pct, outcome
                ) VALUES (
                    'test_session', ?, 'STOCK.NS', 'BUY', 20,
                    ?, 20000.0, 980.0, 1020.0,
                    '2026-10-05 10:00:00', 'CLOSED', ?, '2026-10-05 14:00:00',
                    ?, ?, ?, 1.0, ?
                )
            """, (i, en, ex, gross, fee, net, outcome))

        conn.commit()

        # Reconcile
        recon = ConsensusEconomicEngine.reconcile_trade_accounting(session_id="test_session", conn=conn)

        self.assertTrue(recon["is_reconciled"], "Accounting must reconcile exactly")
        self.assertEqual(recon["total_closed_trades"], 5)
        self.assertEqual(recon["discrepancies_count"], 0)
        self.assertAlmostEqual(
            recon["total_gross_pnl"] - recon["total_fees"],
            recon["total_net_pnl"],
            places=2
        )

        categories = recon["exit_categories"]
        self.assertIn("TARGET", categories)
        self.assertIn("ECONOMIC_BREAKEVEN", categories)
        self.assertIn("PROFIT_LOCK", categories)
        self.assertIn("STOP", categories)
        self.assertIn("SESSION_EXIT", categories)

        total_cat_count = sum(c["count"] for c in categories.values())
        self.assertEqual(total_cat_count, 5)

        conn.close()


if __name__ == "__main__":
    unittest.main()
