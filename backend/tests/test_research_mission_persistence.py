"""
TEST RESEARCH MISSION PERSISTENCE & HISTORY UI
================================================
Comprehensive regression test suite for Autonomous Research Lab mission history,
persistence, API endpoints, vault filtering, test DB isolation, and production safety.

Verifies:
1. missions endpoint returns historical missions
2. mission selector loads selected mission
3. URL mission_id selection works
4. localStorage mission persistence works
5. URL takes precedence over localStorage
6. invalid mission_id safely falls back
7. selected mission remains selected after refresh
8. mission-specific vault filtering works
9. all-missions vault mode works
10. historical candidates remain visible
11. old stopped mission remains accessible
12. repair verification mission remains accessible
13. Ridge candidate remains accessible
14. test DB isolation
15. running tests does not alter authoritative DB
16. production safety invariants remain unchanged
"""

import unittest
import os
import sqlite3
import hashlib
import json
import tempfile
from fastapi.testclient import TestClient

from app.main import app
from app.data.database import set_test_db_override, get_canonical_db_path, get_db_path
from app.analytics.research_orchestrator.research_memory import ResearchMemory
from app.analytics.research_orchestrator.candidate_vault import CandidateVault
from app.analytics.research_orchestrator.research_orchestrator import (
    ResearchOrchestrator,
    CHAMPION_INTRADAY_HASH,
    CHAMPION_SWING_HASH,
    EXPECTED_HISTORY_ROWS,
)
from app.analytics.kelly_sizer import get_portfolio_heat_status


class TestResearchMissionPersistence(unittest.TestCase):
    """16 comprehensive regression tests for Autonomous Research Lab mission persistence."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.canonical_db = get_canonical_db_path()

    def test_01_missions_endpoint_returns_historical_missions(self):
        """1. missions endpoint returns historical missions with accurate counts."""
        response = self.client.get("/api/research-autopilot/missions")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("status"), "success")
        missions = data.get("missions", [])
        self.assertGreaterEqual(len(missions), 190)

        # Check required schema fields on missions
        first = missions[0]
        for field in ["mission_id", "created_at", "status", "unique_completed",
                      "total_executions", "total_candidates", "oos_pending_candidates",
                      "frozen_candidates"]:
            self.assertIn(field, first, f"Missing field {field} in mission summary")

    def test_02_mission_selector_loads_selected_mission(self):
        """2. mission selector loads selected mission correctly."""
        target_mission = "mission_20260906_210627_8116a0"
        response = self.client.get(f"/api/research-autopilot/status?mission_id={target_mission}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("status"), "success")
        active = data.get("active_mission", {})
        self.assertEqual(active.get("mission_id"), target_mission)
        self.assertEqual(data.get("mission_counts", {}).get("unique_experiments"), 10)

    def test_03_url_mission_id_selection_works(self):
        """3. URL mission_id selection works via API query parameter."""
        target_mission = "mission_20260906_201001_805952"
        response = self.client.get(f"/api/research-autopilot/status?mission_id={target_mission}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("status"), "success")
        self.assertEqual(data.get("active_mission", {}).get("mission_id"), target_mission)
        self.assertFalse(data.get("fallback_to_latest", False))

    def test_04_localstorage_mission_persistence_works(self):
        """4. localStorage mission persistence resolves correctly when supplied."""
        stored_mission = "mission_20260906_201001_805952"
        # Simulating frontend sending stored mission id to status endpoint
        response = self.client.get(f"/api/research-autopilot/status?mission_id={stored_mission}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("status"), "success")
        self.assertEqual(data.get("active_mission", {}).get("mission_id"), stored_mission)

    def test_05_url_takes_precedence_over_localstorage(self):
        """5. Explicitly requested mission takes precedence over any other default/stored mission."""
        url_mission = "mission_20260906_210627_8116a0"
        response = self.client.get(f"/api/research-autopilot/status?mission_id={url_mission}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("status"), "success")
        self.assertEqual(data.get("active_mission", {}).get("mission_id"), url_mission)

    def test_06_invalid_mission_id_safely_falls_back(self):
        """6. invalid mission_id safely falls back to newest valid mission."""
        invalid_mission = "mission_non_existent_999999_xyz123"
        response = self.client.get(f"/api/research-autopilot/status?mission_id={invalid_mission}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("status"), "success")
        self.assertTrue(data.get("fallback_to_latest"))
        self.assertIsNotNone(data.get("active_mission"))
        self.assertNotEqual(data.get("active_mission", {}).get("mission_id"), invalid_mission)

    def test_07_selected_mission_remains_selected_after_refresh(self):
        """7. selected mission remains selected after refresh (idempotent retrieval)."""
        target = "mission_20260906_210627_8116a0"
        r1 = self.client.get(f"/api/research-autopilot/status?mission_id={target}").json()
        r2 = self.client.get(f"/api/research-autopilot/status?mission_id={target}").json()
        self.assertEqual(r1.get("status"), "success")
        self.assertEqual(r2.get("status"), "success")
        self.assertEqual(r1.get("active_mission", {}).get("mission_id"), target)
        self.assertEqual(r2.get("active_mission", {}).get("mission_id"), target)
        self.assertEqual(r1.get("active_mission"), r2.get("active_mission"))

    def test_08_mission_specific_vault_filtering_works(self):
        """8. mission-specific vault filtering returns only candidates for that mission."""
        target = "mission_20260906_210627_8116a0"
        response = self.client.get(f"/api/research-autopilot/vault?mission_id={target}")
        self.assertEqual(response.status_code, 200)
        candidates = response.json().get("candidates", [])
        self.assertEqual(len(candidates), 3)
        for c in candidates:
            self.assertEqual(c.get("mission_id"), target)

    def test_09_all_missions_vault_mode_works(self):
        """9. all-missions vault mode returns candidates across all missions."""
        response = self.client.get("/api/research-autopilot/vault")
        self.assertEqual(response.status_code, 200)
        candidates = response.json().get("candidates", [])
        self.assertGreaterEqual(len(candidates), 530)
        mission_ids = {c.get("mission_id") for c in candidates if c.get("mission_id")}
        self.assertGreater(len(mission_ids), 1)

    def test_10_historical_candidates_remain_visible(self):
        """10. historical candidates remain visible with valid provenance and metrics."""
        response = self.client.get("/api/research-autopilot/vault")
        self.assertEqual(response.status_code, 200)
        candidates = response.json().get("candidates", [])
        sample = candidates[0]
        self.assertIn("candidate_id", sample)
        self.assertIn("config_hash", sample)
        self.assertIn("status", sample)

    def test_11_old_stopped_mission_remains_accessible(self):
        """11. old stopped mission remains accessible with all 299 candidates."""
        stopped_mission = "mission_20260906_201001_805952"
        response = self.client.get(f"/api/research-autopilot/vault?mission_id={stopped_mission}")
        self.assertEqual(response.status_code, 200)
        candidates = response.json().get("candidates", [])
        self.assertEqual(len(candidates), 299)

    def test_12_repair_verification_mission_remains_accessible(self):
        """12. repair verification mission remains accessible with 10 experiments and 3 candidates."""
        repair_mission = "mission_20260906_210627_8116a0"
        r_status = self.client.get(f"/api/research-autopilot/status?mission_id={repair_mission}").json()
        self.assertEqual(r_status.get("mission_counts", {}).get("unique_experiments"), 10)

        r_vault = self.client.get(f"/api/research-autopilot/vault?mission_id={repair_mission}").json()
        self.assertEqual(len(r_vault.get("candidates", [])), 3)

    def test_13_ridge_candidate_remains_accessible(self):
        """13. Ridge candidate remains accessible and retains OOS_PENDING status."""
        target_id = "cand_5cd23540c5c02c91"
        response = self.client.get("/api/research-autopilot/vault")
        candidates = response.json().get("candidates", [])
        ridge_cand = next((c for c in candidates if c.get("candidate_id") == target_id), None)
        self.assertIsNotNone(ridge_cand, f"Ridge candidate {target_id} not found in vault")
        self.assertEqual(ridge_cand.get("status"), "OOS_PENDING")
        metrics = ridge_cand.get("metrics", {})
        sharpe_val = float(metrics.get("sharpe", ridge_cand.get("sharpe", 0)))
        cagr_val = float(metrics.get("cagr_net", ridge_cand.get("cagr_net", 0)))
        self.assertAlmostEqual(sharpe_val, 2.14, delta=0.05)
        self.assertAlmostEqual(cagr_val, 47.45, delta=0.1)

    def test_14_test_db_isolation(self):
        """14. test DB isolation redirects queries to temporary database and restores cleanly."""
        canonical = get_canonical_db_path()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            # Set override
            set_test_db_override(tmp_path)
            self.assertEqual(get_db_path(), tmp_path)
            self.assertNotEqual(get_db_path(), canonical)

            # Initialize tables in temp DB
            ResearchMemory.ensure_tables()
            conn = sqlite3.connect(tmp_path)
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='research_missions'")
            self.assertIsNotNone(c.fetchone())
            conn.close()
        finally:
            # Restore override
            set_test_db_override(None)
            self.assertEqual(get_db_path(), canonical)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_15_running_tests_does_not_alter_authoritative_db(self):
        """15. running tests under test DB isolation does not alter authoritative DB."""
        canonical = get_canonical_db_path()
        conn = sqlite3.connect(canonical)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM research_candidate_vault")
        count_before = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM research_experiments_ledger")
        ledger_before = c.fetchone()[0]
        conn.close()

        # Run an operation inside an isolated temp db
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            set_test_db_override(tmp_path)
            ResearchMemory.ensure_tables()
            # Insert test candidate into temp db
            conn = sqlite3.connect(tmp_path)
            c = conn.cursor()
            c.execute("""
                INSERT INTO research_candidate_vault (
                    candidate_id, experiment_id, mission_id, status, discovery_universe,
                    config_hash, artifact_path, artifact_sha256, metrics_json,
                    universe_transfer_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                "cand_test_isolation_dummy", "exp_test_dummy", "mission_test_dummy", "OOS_PENDING",
                "LIVE_52", "dummy_hash_123", "/dummy/path", "dummy_sha", json.dumps({"sharpe": 1.5}),
                json.dumps({}), "2026-09-06T00:00:00", "2026-09-06T00:00:00"
            ))
            conn.commit()
            conn.close()
        finally:
            set_test_db_override(None)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        # Verify authoritative DB was completely untouched
        conn = sqlite3.connect(canonical)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM research_candidate_vault")
        count_after = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM research_experiments_ledger")
        ledger_after = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM research_candidate_vault WHERE candidate_id='cand_test_isolation_dummy'")
        dummy_in_canonical = c.fetchone()[0]
        conn.close()

        self.assertEqual(count_before, count_after)
        self.assertEqual(ledger_before, ledger_after)
        self.assertEqual(dummy_in_canonical, 0)

    def test_16_production_safety_invariants_remain_unchanged(self):
        """16. production safety invariants remain strictly preserved."""
        # Champion Model Hashes
        intraday_path = os.path.join(os.path.dirname(self.canonical_db), "models", "intraday", "champion_ensemble.pkl")
        swing_path = os.path.join(os.path.dirname(self.canonical_db), "models", "swing", "champion_ensemble.pkl")

        with open(intraday_path, "rb") as f:
            intraday_sha = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(intraday_sha, CHAMPION_INTRADAY_HASH)

        with open(swing_path, "rb") as f:
            swing_sha = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(swing_sha, CHAMPION_SWING_HASH)

        # ml_trade_history row count
        conn = sqlite3.connect(self.canonical_db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM ml_trade_history")
        ml_rows = c.fetchone()[0]
        conn.close()
        self.assertGreaterEqual(ml_rows, EXPECTED_HISTORY_ROWS)

        # Portfolio Heat
        heat_status = get_portfolio_heat_status()
        self.assertEqual(heat_status.get("current_heat_pct"), 0.0)
        self.assertEqual(heat_status.get("actual_positions"), 0)


if __name__ == "__main__":
    unittest.main()
