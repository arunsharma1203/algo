"""
TEST RESEARCH EXPERIMENT INSPECTOR & AUDIT VIEW (V1)
=====================================================
Comprehensive verification test suite for:
1. ResearchMemory.get_experiment_audit_details (30+ authoritative metrics, 8 governance gates, provenance hashes)
2. GET /api/research-autopilot/experiment/{experiment_id} (read-only audit detail endpoint)
3. GET /api/research-autopilot/experiment/{experiment_id}/report.pdf (institutional PDF download endpoint)
4. ResearchReportPDFGenerator.generate_experiment_audit_pdf (ReportLab PDF generation)
5. Read-only research isolation and strict production invariant preservation
"""

import unittest
import os
import json
import sqlite3
from fastapi.testclient import TestClient

from app.main import app
from app.data.historical_data_layer import get_db_path
from app.analytics.research_orchestrator.research_memory import ResearchMemory
from app.analytics.research_orchestrator.research_governance import ResearchGovernance
from app.analytics.research_orchestrator.research_orchestrator import (
    ResearchOrchestrator,
    CHAMPION_INTRADAY_HASH,
    CHAMPION_SWING_HASH,
    EXPECTED_HISTORY_ROWS
)
from app.analytics.research_report_pdf_generator import ResearchReportPDFGenerator


class TestResearchExperimentInspector(unittest.TestCase):
    """Rigorous tests validating authoritative Research Autopilot Inspector and PDF export."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.db_path = get_db_path()

        # Seed a complete test experiment in research_experiments_ledger
        cls.test_exp_id = "exp_audit_fixture_001"
        cls.test_cand_id = "cand_audit_fixture_001"
        cls.test_config_hash = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
        cls.test_artifact_sha256 = "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
        cls.test_model_hash = "mod_fixture_hash_123"
        cls.test_dataset_hash = "ds_fixture_hash_456"

        cls.full_metrics = {
            "completed_trades": 84,
            "trade_count": 84,
            "win_rate_pct": 58.33,
            "profit_factor": 1.74,
            "expectancy": 0.38,
            "cagr_net": 18.42,
            "cagr_gross": 24.10,
            "sharpe": 1.62,
            "sortino": 2.15,
            "max_drawdown_pct": 11.4,
            "turnover_pct": 420.0,
            "cost_drag_bps": 15.0,
            "drag_10bps": 3.8,
            "drag_15bps": 5.68,
            "drag_20bps": 7.55,
            "drag_30bps": 11.35,
            "cagr_net_10bps": 20.30,
            "cagr_net_15bps": 18.42,
            "cagr_net_20bps": 16.55,
            "cagr_net_30bps": 12.75,
            "survives_friction_10bps": True,
            "survives_friction_15bps": True,
            "survives_friction_20bps": True,
            "survives_friction_30bps": True,
            "walk_forward_stability_pct": 80.0
        }

        cls.changes = {
            "model_family": "LIGHTGBM",
            "feature_family": "TECHNICAL_ALPHA",
            "feature_version": "v1.0",
            "horizon_days": 10,
            "portfolio_family": "HYSTERESIS_TOP5_15",
            "entry_top_k": 5,
            "exit_top_k": 15,
            "holding_period": 10,
            "rebalance_frequency": 1,
            "universe": "LIVE_52"
        }

        with sqlite3.connect(cls.db_path) as conn:
            # Insert into research_experiments_ledger
            conn.execute("""
                INSERT OR REPLACE INTO research_experiments_ledger (
                    experiment_id, mission_id, parent_id, hypothesis, changes_json,
                    config_hash, dataset_hash, model_hash, code_version, seed,
                    train_start, train_end, val_start, val_end, oos_start, oos_end,
                    metrics_json, quality_class, governance_verdict, rejection_reasons_json,
                    runtime_seconds, rss_mb, status, created_at
                ) VALUES (
                    ?, 'mission_audit_test', 'ROOT', 'Test fixture hypothesis for research inspector audit.', ?,
                    ?, ?, ?, 'v1.0-autopilot', 42,
                    '2016-08-29', '2021-09-04', '2021-09-05', '2023-09-04', '2023-09-05', '2026-09-04',
                    ?, 'EXCELLENT', 'PASS', '[]',
                    14.5, 256.0, 'COMPLETED', '2026-09-06 20:00:00'
                )
            """, (
                cls.test_exp_id,
                json.dumps(cls.changes),
                cls.test_config_hash,
                cls.test_dataset_hash,
                cls.test_model_hash,
                json.dumps(cls.full_metrics)
            ))

            # Insert into research_candidate_vault
            conn.execute("""
                INSERT OR REPLACE INTO research_candidate_vault (
                    candidate_id, experiment_id, mission_id, status, discovery_universe,
                    config_hash, artifact_path, artifact_sha256, metrics_json, universe_transfer_json,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, 'mission_audit_test', 'FROZEN', 'LIVE_52',
                    ?, 'models/research/cand_fixture.pkl', ?, ?,
                    '{"LIVE_52": "PASSED", "RESEARCH_100": "TESTING"}',
                    '2026-09-06 20:05:00', '2026-09-06 20:05:00'
                )
            """, (
                cls.test_cand_id,
                cls.test_exp_id,
                cls.test_config_hash,
                cls.test_artifact_sha256,
                json.dumps(cls.full_metrics)
            ))
            conn.commit()

    @classmethod
    def tearDownClass(cls):
        # Clean up test fixtures from DB
        with sqlite3.connect(cls.db_path) as conn:
            conn.execute("DELETE FROM research_experiments_ledger WHERE experiment_id = ?", (cls.test_exp_id,))
            conn.execute("DELETE FROM research_candidate_vault WHERE candidate_id = ?", (cls.test_cand_id,))
            conn.commit()

    def test_01_get_experiment_audit_details_returns_all_required_items(self):
        """Verifies get_experiment_audit_details resolves by experiment_id and returns all 30+ items."""
        audit = ResearchMemory.get_experiment_audit_details(self.test_exp_id)
        self.assertIsNotNone(audit)

        # Identification & Hashes
        self.assertEqual(audit["experiment_id"], self.test_exp_id)
        self.assertEqual(audit["candidate_id"], self.test_cand_id)
        self.assertEqual(audit["config_hash"], self.test_config_hash)
        self.assertEqual(audit["artifact_sha256"], self.test_artifact_sha256)
        self.assertEqual(audit["model_hash"], self.test_model_hash)
        self.assertEqual(audit["dataset_hash"], self.test_dataset_hash)
        self.assertEqual(audit["code_version"], "v1.0-autopilot")
        self.assertEqual(audit["research_engine_version"], "v1.0-autopilot")

        # Strategy & Hypothesis
        self.assertEqual(audit["model_family"], "LIGHTGBM")
        self.assertEqual(audit["feature_family"], "TECHNICAL_ALPHA")
        self.assertEqual(audit["target"], "10D Return")
        self.assertEqual(audit["horizon"], "10D")
        self.assertIn("LIVE_52", audit["universe"])
        self.assertGreater(audit["ticker_count"], 0)
        self.assertEqual(audit["portfolio_construction"], "HYSTERESIS_TOP5_15")

        # Strategy Parameters
        params = audit["parameters"]
        self.assertIsNotNone(params)
        self.assertEqual(params["entry_threshold"], 5)
        self.assertEqual(params["exit_threshold"], 15)
        self.assertEqual(params["top_k"], 5)
        self.assertEqual(params["max_holding_days"], 10)
        self.assertEqual(params["rebalance_frequency"], 1)
        self.assertEqual(params["seed"], 42)

        # Data Boundaries
        self.assertEqual(audit["train_date_range"]["start"], "2016-08-29")
        self.assertEqual(audit["train_date_range"]["end"], "2021-09-04")
        self.assertEqual(audit["val_date_range"]["start"], "2021-09-05")
        self.assertEqual(audit["val_date_range"]["end"], "2023-09-04")
        self.assertEqual(audit["locked_oos_date_range"]["start"], "2023-09-05")
        self.assertEqual(audit["locked_oos_date_range"]["end"], "2026-09-04")
        self.assertEqual(audit["locked_oos_date_range"]["status"], "SEALED - UNSEEN")

        # Performance Metrics
        metrics = audit["metrics"]
        self.assertEqual(metrics["completed_trades"], 84)
        self.assertEqual(metrics["win_rate_pct"], 58.33)
        self.assertEqual(metrics["profit_factor"], 1.74)
        self.assertEqual(metrics["expectancy"], 0.38)
        self.assertEqual(metrics["cagr_net"], 18.42)
        self.assertEqual(metrics["cagr_gross"], 24.10)
        self.assertEqual(metrics["sharpe"], 1.62)
        self.assertEqual(metrics["sortino"], 2.15)
        self.assertEqual(metrics["max_drawdown_pct"], 11.4)
        self.assertEqual(metrics["turnover_pct"], 420.0)

        # Cost Sensitivity
        cost = audit["cost_sensitivity"]
        self.assertIn("10bps", cost)
        self.assertIn("15bps", cost)
        self.assertIn("20bps", cost)
        self.assertIn("30bps", cost)
        self.assertTrue(cost["30bps"]["survived"])
        self.assertEqual(cost["30bps"]["cagr_net"], 12.75)

        # Walk-Forward Multi-Window Breakdown
        wf = audit["walk_forward"]
        self.assertEqual(len(wf), 5)
        for w in wf:
            self.assertIn("window", w)
            self.assertIn("val_span", w)
            self.assertIn("status", w)

        # 8 Formal Governance Gates
        gov = audit["governance_gates"]
        self.assertIn("gates", gov)
        self.assertEqual(len(gov["gates"]), 8)
        for g in gov["gates"]:
            self.assertIn("gate_id", g)
            self.assertIn("gate_name", g)
            self.assertIn("condition", g)
            self.assertIn("measured_value", g)
            self.assertIn("threshold", g)
            self.assertIn("passed", g)
            self.assertIn("description", g)
        self.assertTrue(gov["all_passed"])

        # Lineage & Verdict
        self.assertEqual(audit["verdict"], "PASS")
        self.assertEqual(audit["quality_class"], "EXCELLENT")
        self.assertEqual(audit["candidate_status"], "FROZEN")
        self.assertEqual(audit["parent_id"], "ROOT")
        self.assertIsNotNone(audit["lineage_narrative"])
        self.assertEqual(audit["rejection_reasons"], [])

        # Production Safety Disclaimer
        self.assertIn("RESEARCH ARTIFACT ONLY", audit["production_safety_disclaimer"])
        self.assertIn("NOT A PRODUCTION CHAMPION", audit["production_safety_disclaimer"])

    def test_02_resolve_by_candidate_id(self):
        """Verifies get_experiment_audit_details resolves by candidate_id."""
        audit = ResearchMemory.get_experiment_audit_details(self.test_cand_id)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["candidate_id"], self.test_cand_id)
        self.assertEqual(audit["experiment_id"], self.test_exp_id)
        self.assertEqual(audit["status"], "COMPLETED")

    def test_03_nonexistent_identifier_returns_none(self):
        """Verifies get_experiment_audit_details returns None for non-existent identifier."""
        audit = ResearchMemory.get_experiment_audit_details("exp_totally_non_existent_99999")
        self.assertIsNone(audit)

    def test_04_incomplete_metrics_safe_na_handling(self):
        """Verifies missing or None metrics produce valid dict with None values, never crash."""
        temp_id = "exp_audit_fixture_empty"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO research_experiments_ledger (
                    experiment_id, mission_id, parent_id, hypothesis, changes_json,
                    config_hash, metrics_json, governance_verdict, quality_class,
                    status, created_at
                ) VALUES (
                    ?, 'mission_audit_test', 'ROOT', 'Incomplete metrics test', '{}',
                    'empty_hash', '{}', 'FAIL', 'REJECTED',
                    'FAILED', '2026-09-06 20:10:00'
                )
            """, (temp_id,))
            conn.commit()

        try:
            audit = ResearchMemory.get_experiment_audit_details(temp_id)
            self.assertIsNotNone(audit)
            # Metrics should be empty or contain None, enabling UI to display 'N/A'
            self.assertIsNone(audit["metrics"].get("sharpe"))
            self.assertIsNone(audit["metrics"].get("cagr_net"))
            self.assertEqual(audit["verdict"], "FAIL")
            self.assertEqual(audit["quality_class"], "REJECTED")
            # 8 gates still evaluated safely
            self.assertEqual(len(audit["governance_gates"]["gates"]), 8)
            self.assertFalse(audit["governance_gates"]["all_passed"])
        finally:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM research_experiments_ledger WHERE experiment_id = ?", (temp_id,))
                conn.commit()

    def test_05_api_get_experiment_audit_endpoint(self):
        """Verifies GET /api/research-autopilot/experiment/{id} returns 200 with complete audit payload."""
        response = self.client.get(f"/api/research-autopilot/experiment/{self.test_exp_id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("status"), "success")
        self.assertIn("audit", data)
        self.assertIn("experiment", data)
        self.assertIn("explanation", data)

        audit = data["audit"]
        self.assertEqual(audit["experiment_id"], self.test_exp_id)
        self.assertEqual(len(audit["governance_gates"]["gates"]), 8)
        self.assertEqual(len(audit["walk_forward"]), 5)
        self.assertEqual(audit["cost_sensitivity"]["30bps"]["cagr_net"], 12.75)

    def test_06_api_get_experiment_audit_404(self):
        """Verifies GET /api/research-autopilot/experiment/{id} returns 404 for unknown experiment."""
        response = self.client.get("/api/research-autopilot/experiment/nonexistent_audit_id_xyz")
        self.assertEqual(response.status_code, 404)
        self.assertIn("not found", response.json()["detail"].lower())

    def test_07_api_download_experiment_pdf(self):
        """Verifies GET /api/research-autopilot/experiment/{id}/report.pdf returns valid PDF."""
        response = self.client.get(f"/api/research-autopilot/experiment/{self.test_exp_id}/report.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-type"), "application/pdf")
        self.assertIn(f"Research_Audit_{self.test_exp_id}.pdf", response.headers.get("content-disposition", ""))
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertGreater(len(response.content), 5000)

    def test_08_api_download_experiment_pdf_by_candidate_id(self):
        """Verifies GET /api/research-autopilot/experiment/{candidate_id}/report.pdf succeeds for candidate."""
        response = self.client.get(f"/api/research-autopilot/experiment/{self.test_cand_id}/report.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-type"), "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-"))

    def test_09_api_download_experiment_pdf_404(self):
        """Verifies GET /api/research-autopilot/experiment/{id}/report.pdf returns 404 for unknown ID."""
        response = self.client.get("/api/research-autopilot/experiment/nonexistent_audit_id_xyz/report.pdf")
        self.assertEqual(response.status_code, 404)

    def test_10_pdf_generator_institutional_elements(self):
        """Verifies generate_experiment_audit_pdf produces institutional PDF with safety disclaimer & hashes."""
        audit = ResearchMemory.get_experiment_audit_details(self.test_exp_id)
        pdf_bytes = ResearchReportPDFGenerator.generate_experiment_audit_pdf(audit)

        self.assertIsInstance(pdf_bytes, bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertGreater(len(pdf_bytes), 5000)

    def test_11_pdf_generator_empty_audit_resilience(self):
        """Verifies generate_experiment_audit_pdf safely handles empty/null dict without crashing."""
        pdf_bytes = ResearchReportPDFGenerator.generate_experiment_audit_pdf({})
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertGreater(len(pdf_bytes), 3000)

    def test_12_read_only_isolation_and_production_invariants(self):
        """Verifies that inspecting and exporting experiments strictly preserves all production invariants."""
        # Baseline invariants check
        safety_before = ResearchOrchestrator.verify_production_safety_invariants()
        self.assertTrue(safety_before["all_invariants_preserved"])
        self.assertEqual(safety_before["intraday_champion_hash"], CHAMPION_INTRADAY_HASH)
        self.assertEqual(safety_before["swing_champion_hash"], CHAMPION_SWING_HASH)
        self.assertEqual(safety_before["ml_trade_history_rows"], EXPECTED_HISTORY_ROWS)
        self.assertEqual(safety_before["portfolio_heat_pct"], 0.0)

        # Trigger inspector queries and PDF export
        _ = self.client.get(f"/api/research-autopilot/experiment/{self.test_exp_id}")
        _ = self.client.get(f"/api/research-autopilot/experiment/{self.test_exp_id}/report.pdf")

        # Post-execution invariants check
        safety_after = ResearchOrchestrator.verify_production_safety_invariants()
        self.assertTrue(safety_after["all_invariants_preserved"])
        self.assertEqual(safety_after["intraday_champion_hash"], CHAMPION_INTRADAY_HASH)
        self.assertEqual(safety_after["swing_champion_hash"], CHAMPION_SWING_HASH)
        self.assertEqual(safety_after["ml_trade_history_rows"], EXPECTED_HISTORY_ROWS)
        self.assertEqual(safety_after["portfolio_heat_pct"], 0.0)
        self.assertEqual(safety_after["broker_orders"], 0)
        self.assertEqual(safety_after["telegram_alerts"], 0)


if __name__ == "__main__":
    unittest.main()
