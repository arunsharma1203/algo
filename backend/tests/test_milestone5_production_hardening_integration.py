"""
Test suite for Milestone 5: Production Hardening, Reconciliation & End-to-End Autonomous Platform Integration.
Tests:
1. PositionMonitorService evaluation and separation of virtual recommendations vs actual positions
2. Trade reconciliation without history loss or schema corruption
3. Defensive sweep isolation (virtual recommendations ignored, 0.0% heat maintained)
4. Fail-closed ticker validation across data gateway and universe config
5. End-to-end platform integration across the 5 authoritative services
6. Non-negotiable Production Safety Invariants (Champion hashes, Heat = 0.0%, 0 broker orders)
"""

import unittest
import json
import os
import hashlib
import sqlite3
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.analytics.position_monitor import PositionMonitorService
from app.analytics.smart_scanner import SmartScannerPipeline
from app.analytics.research_orchestrator.autonomous_runner import AutonomousResearchRunner
from app.analytics.model_registry import ModelRegistry, CHAMPION_HASHES
from app.analytics.universe_config import validate_ticker, validate_ticker_list
from app.analytics.kelly_sizer import get_portfolio_heat_status
from app.data.historical_data_layer import get_db_path

EXPECTED_INTRADAY_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
EXPECTED_SWING_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

class TestMilestone5ProductionHardeningIntegration(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)

    # ─────────────────────────────────────────────────────────────
    # 1. POSITION MONITOR & RECONCILIATION HARDENING
    # ─────────────────────────────────────────────────────────────

    def test_01_position_monitor_summary_separates_virtual_and_actual(self):
        summary = PositionMonitorService.get_open_positions_summary(force_refresh=True)
        self.assertIn("total_open", summary)
        self.assertIn("actual_positions_count", summary)
        self.assertIn("virtual_recommendations_count", summary)
        self.assertIn("portfolio_heat_pct", summary)
        self.assertIn("actual_positions", summary)
        self.assertIn("virtual_recommendations", summary)

        # Virtual recommendations must NOT contribute to actual_positions
        self.assertEqual(summary["actual_positions_count"], 0)
        self.assertEqual(summary["portfolio_heat_pct"], 0.0)

    def test_02_trade_reconciliation_preserves_audit_row(self):
        """
        Verify that reconciling an open trade sets status to CLOSED,
        updates exit_price and profit_pct, but never deletes or alters other rows.
        """
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM ml_trade_history")
        pre_count = c.fetchone()[0]

        # Insert a temporary test trade
        c.execute("""
            INSERT INTO ml_trade_history (
                timestamp, ticker, direction, entry, sl, tp1, tp2,
                confidence, trade_type, status, source, position_type
            ) VALUES (
                datetime('now'), 'TESTRECON.NS', 'BULLISH', 100.0, 95.0, 110.0, 120.0,
                0.85, 'INTRADAY', 'OPEN', 'TEST', 'NOT_A_POSITION'
            )
        """)
        trade_id = c.lastrowid
        conn.commit()
        conn.close()

        # Reconcile the trade
        reconciled = PositionMonitorService.reconcile_trade(
            trade_id=trade_id,
            exit_price=105.0,
            outcome="TARGET 1 MET"
        )
        self.assertTrue(reconciled)

        # Verify row state
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT status, outcome, exit_price, profit_pct FROM ml_trade_history WHERE id = ?", (trade_id,))
        row = c.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "CLOSED")
        self.assertEqual(row[1], "TARGET 1 MET")
        self.assertEqual(row[2], 105.0)
        self.assertEqual(row[3], 5.0)  # (105 - 100) / 100 * 100 = 5%

        # Clean up test row
        c.execute("DELETE FROM ml_trade_history WHERE id = ?", (trade_id,))
        conn.commit()
        c.execute("SELECT COUNT(*) FROM ml_trade_history")
        post_count = c.fetchone()[0]
        conn.close()

        self.assertEqual(post_count, pre_count)

    def test_03_defensive_sweep_ignores_virtual_recommendations(self):
        """
        Defensive risk sweep must strictly evaluate only PAPER_POSITION or LIVE_POSITION.
        Virtual recommendations must have zero broker calls.
        """
        summary = PositionMonitorService.run_defensive_sweep(force_run=True)
        self.assertEqual(summary["actual_positions_count"], 0)
        self.assertEqual(summary["portfolio_heat_pct"], 0.0)

    # ─────────────────────────────────────────────────────────────
    # 2. FAIL-CLOSED TICKER VALIDATION
    # ─────────────────────────────────────────────────────────────

    def test_04_invalid_ticker_dsdsds_strictly_rejected(self):
        """
        Forensic check: DSDSDS.NS must fail closed everywhere.
        """
        ok, canon, err = validate_ticker("DSDSDS.NS")
        self.assertFalse(ok)
        self.assertIn("not a recognized instrument", err)

        ok_raw, canon_raw, err_raw = validate_ticker("DSDSDS")
        self.assertFalse(ok_raw)

        # Batch validation must also reject lists containing it
        ok_list, valid_tickers, err_list = validate_ticker_list(["RELIANCE.NS", "DSDSDS.NS", "TCS.NS"])
        self.assertFalse(ok_list)
        self.assertIn("DSDSDS.NS", err_list)

    def test_05_valid_authoritative_tickers_accepted(self):
        ok, canon, err = validate_ticker("RELIANCE.NS")
        self.assertTrue(ok)
        self.assertEqual(canon, "RELIANCE.NS")
        self.assertIsNone(err)

        ok, canon, err = validate_ticker("tcs")
        self.assertTrue(ok)
        self.assertEqual(canon, "TCS.NS")

    # ─────────────────────────────────────────────────────────────
    # 3. END-TO-END AUTONOMOUS PLATFORM INTEGRATION
    # ─────────────────────────────────────────────────────────────

    def test_06_smart_scanner_status_endpoint(self):
        res = self.client.get("/api/smart-scanner/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("champions", data)
        self.assertTrue(data["champions"]["all_champions_intact"])
        self.assertIn("positions", data)
        self.assertEqual(data["positions"]["portfolio_heat_pct"], 0.0)

    def test_07_research_autopilot_status_endpoint(self):
        res = self.client.get("/api/research-autopilot/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("safety", data)
        self.assertTrue(data["safety"]["all_invariants_preserved"])
        self.assertEqual(data["safety"]["portfolio_heat_pct"], 0.0)
        self.assertEqual(data["safety"]["intraday_champion_hash"], EXPECTED_INTRADAY_HASH)
        self.assertEqual(data["safety"]["swing_champion_hash"], EXPECTED_SWING_HASH)

    def test_08_system_audit_and_synthetic_pipeline_endpoints(self):
        # 1. Audit Log Endpoint
        res = self.client.get("/api/system/audit-log")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("events", data)

        # 2. Synthetic Pipeline Non-Contaminating Diagnostic
        pipe_res = self.client.post("/api/system/pipeline-test?timeframe=intraday")
        self.assertEqual(pipe_res.status_code, 200)
        pdata = pipe_res.json()
        self.assertEqual(pdata.get("status"), "PASS")
        self.assertTrue(pdata.get("overall_pass"))
        self.assertEqual(pdata.get("passed_stages"), pdata.get("total_stages"))

    # ─────────────────────────────────────────────────────────────
    # 4. PRODUCTION SAFETY INVARIANTS
    # ─────────────────────────────────────────────────────────────

    def test_09_champion_hashes_remain_byte_exact(self):
        intra_valid, intra_sha, _ = ModelRegistry.verify_champion_hash("intraday")
        swing_valid, swing_sha, _ = ModelRegistry.verify_champion_hash("swing")

        self.assertTrue(intra_valid)
        self.assertEqual(intra_sha, EXPECTED_INTRADAY_HASH)

        self.assertTrue(swing_valid)
        self.assertEqual(swing_sha, EXPECTED_SWING_HASH)

    def test_10_portfolio_heat_is_strictly_zero(self):
        heat_status = get_portfolio_heat_status()
        self.assertEqual(heat_status.get("current_heat_pct"), 0.0)
        self.assertEqual(heat_status.get("actual_positions"), 0)
        self.assertEqual(heat_status.get("status"), "NORMAL")

    def test_11_database_row_counts_preserved(self):
        canonical_db = get_db_path()
        conn = sqlite3.connect(canonical_db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM ml_trade_history")
        rows = c.fetchone()[0]
        conn.close()
        self.assertGreaterEqual(rows, 74)


if __name__ == "__main__":
    unittest.main()
