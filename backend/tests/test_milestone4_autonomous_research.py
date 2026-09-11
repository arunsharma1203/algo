"""
Test suite for Milestone 4: Unified Autonomous Research & One-Click AI Research.
Tests:
1. Active Champion Baseline Inspection
2. Research Memory Consultation & Tested Hash Tracking
3. Deterministic Hypothesis Generation (No duplicates)
4. Cross-Validation Job Execution
5. Hurdle Evaluation (Sharpe >= 0.85, CAGR >= 8%, Max DD <= 22%, 30 bps cost survival)
6. 4-Stage Universe Transfer (LIVE_52 -> RESEARCH_100 -> NIFTY_200 -> NIFTY_500)
7. Candidate Vault Freezing
8. Strict Governance: Zero Autonomous Champion Promotion (Locked behind human approval)
9. FastAPI Endpoints (/api/research-autopilot/run-autonomous)
10. Production Safety Invariants: Champion hashes byte-exact, Portfolio Heat strictly 0.0%
"""

import unittest
import json
import os
import hashlib
import sqlite3
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.analytics.research_orchestrator.autonomous_runner import AutonomousResearchRunner
from app.analytics.research_orchestrator.research_mission import ResearchMission
from app.analytics.research_orchestrator.research_memory import ResearchMemory
from app.analytics.research_orchestrator.candidate_vault import CandidateVault
from app.analytics.model_registry import ModelRegistry, CHAMPION_HASHES
from app.analytics.kelly_sizer import get_portfolio_heat_status
from app.data.historical_data_layer import get_db_path

EXPECTED_INTRADAY_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
EXPECTED_SWING_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

class TestMilestone4AutonomousResearch(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)

    # ─────────────────────────────────────────────────────────────
    # 1. BASELINE INSPECTION & MEMORY CONSULTATION
    # ─────────────────────────────────────────────────────────────

    def test_01_inspect_active_champions(self):
        baselines = AutonomousResearchRunner.inspect_active_champions()
        self.assertTrue(baselines["all_verified"])
        self.assertIn("intraday", baselines)
        self.assertIn("swing", baselines)
        self.assertTrue(baselines["intraday"]["verified"])
        self.assertTrue(baselines["swing"]["verified"])
        self.assertEqual(baselines["intraday"]["sha256"], EXPECTED_INTRADAY_HASH)
        self.assertEqual(baselines["swing"]["sha256"], EXPECTED_SWING_HASH)
        self.assertGreaterEqual(baselines["intraday"]["f1"], 0.0)
        self.assertGreaterEqual(baselines["swing"]["f1"], 0.0)

    def test_02_consult_research_memory(self):
        memory_state = AutonomousResearchRunner.consult_research_memory("test_mission_memory")
        self.assertIn("mission_id", memory_state)
        self.assertIn("total_tested_hashes", memory_state)
        self.assertIn("model_family_distribution", memory_state)
        self.assertIn("feature_family_distribution", memory_state)

    # ─────────────────────────────────────────────────────────────
    # 2. DETERMINISTIC HYPOTHESIS GENERATION
    # ─────────────────────────────────────────────────────────────

    def test_03_generate_candidate_hypotheses_deterministic_and_unique(self):
        mission = ResearchMission(mission_id="test_mission_hypo", objective="Test hypothesis generation")
        candidates = AutonomousResearchRunner.generate_candidate_hypotheses(mission, count=4)
        self.assertGreaterEqual(len(candidates), 1)

        seen_hashes = set()
        for cand in candidates:
            cfg_hash = cand.get("config_hash")
            self.assertIsNotNone(cfg_hash)
            self.assertNotIn(cfg_hash, seen_hashes)
            seen_hashes.add(cfg_hash)
            self.assertIn("changes", cand)
            self.assertIn("hypothesis", cand)

    def test_04_generate_candidates_excludes_tested_hashes(self):
        mission = ResearchMission(mission_id="test_mission_tested", objective="Test dedup")
        candidates_first = AutonomousResearchRunner.generate_candidate_hypotheses(mission, count=2)
        if candidates_first:
            first_hash = candidates_first[0]["config_hash"]
            ResearchMemory.record_experiment_result({
                "mission_id": mission.mission_id,
                "experiment_id": candidates_first[0]["experiment_id"],
                "config_hash": first_hash,
                "changes": candidates_first[0].get("changes", {}),
                "metrics": {"sharpe": 1.2, "cagr_net": 15.0},
                "status": "COMPLETED",
                "governance_verdict": "ACCEPTED"
            })
            candidates_second = AutonomousResearchRunner.generate_candidate_hypotheses(mission, count=2)
            second_hashes = {c.get("config_hash") for c in candidates_second}
            self.assertNotIn(first_hash, second_hashes)

    # ─────────────────────────────────────────────────────────────
    # 3. CROSS VALIDATION & HURDLE EVALUATION
    # ─────────────────────────────────────────────────────────────

    def test_05_execute_cross_validation_batch(self):
        mission = ResearchMission(mission_id="test_mission_cv", objective="Test CV execution")
        candidates = AutonomousResearchRunner.generate_candidate_hypotheses(mission, count=2)
        if candidates:
            executed = AutonomousResearchRunner.execute_cross_validation_batch(mission, candidates[:2])
            self.assertEqual(len(executed), len(candidates[:2]))
            for exp in executed:
                self.assertIn("metrics", exp)
                self.assertIn("sharpe", exp["metrics"])

    def test_06_hurdle_evaluation_and_freeze(self):
        mission = ResearchMission(mission_id="test_mission_hurdles", objective="Test Hurdles & Transfer")
        cand_pass = {
            "experiment_id": "exp_test_pass_123",
            "hypothesis": "Test High Sharpe Alpha",
            "changes": {"model_family": "LightGBM", "feature_family": "momentum"},
            "metrics": {
                "sharpe": 1.45,
                "cagr_net": 18.5,
                "max_drawdown_pct": 12.0,
                "survives_cost_30bps": True
            },
            "verdict": "CANDIDATE_SHORTLISTED"
        }
        cand_fail = {
            "experiment_id": "exp_test_fail_123",
            "hypothesis": "Test Low Sharpe Alpha",
            "changes": {"model_family": "RandomForest", "feature_family": "trend"},
            "metrics": {
                "sharpe": 0.40,
                "cagr_net": 4.0,
                "max_drawdown_pct": 28.0,
                "survives_cost_30bps": False
            },
            "verdict": "REJECTED"
        }

        qualifiers = AutonomousResearchRunner.evaluate_and_transfer_qualifiers(
            mission,
            [cand_pass, cand_fail]
        )

        self.assertEqual(len(qualifiers), 1)
        q = qualifiers[0]
        self.assertIn("candidate_id", q)
        self.assertEqual(q["experiment_id"], "exp_test_pass_123")
        self.assertIn(q["vault_status"], ["FROZEN", "OOS_PENDING"])
        self.assertIn("4_stage_transfer", q)
        self.assertFalse(q["promoted_to_champion"])

        # Verify artifact frozen in vault
        vault_entry = CandidateVault.get_candidate(q["candidate_id"])
        self.assertIsNotNone(vault_entry)
        self.assertEqual(vault_entry["candidate_id"], q["candidate_id"])

    # ─────────────────────────────────────────────────────────────
    # 4. STRICT GOVERNANCE: ZERO AUTONOMOUS CHAMPION PROMOTION
    # ─────────────────────────────────────────────────────────────

    def test_07_autonomous_research_never_promotes_champion(self):
        with open("backend/models/intraday/champion_ensemble.pkl", "rb") as f:
            pre_intra = hashlib.sha256(f.read()).hexdigest()
        with open("backend/models/swing/champion_ensemble.pkl", "rb") as f:
            pre_swing = hashlib.sha256(f.read()).hexdigest()

        result = AutonomousResearchRunner.run_autonomous_research(batch_size=2)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertFalse(result["safety_invariants"]["champion_promoted_autonomously"])

        with open("backend/models/intraday/champion_ensemble.pkl", "rb") as f:
            post_intra = hashlib.sha256(f.read()).hexdigest()
        with open("backend/models/swing/champion_ensemble.pkl", "rb") as f:
            post_swing = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(pre_intra, EXPECTED_INTRADAY_HASH)
        self.assertEqual(pre_swing, EXPECTED_SWING_HASH)
        self.assertEqual(post_intra, pre_intra)
        self.assertEqual(post_swing, pre_swing)

        self.assertEqual(result["safety_invariants"]["portfolio_heat_pct"], 0.0)
        self.assertEqual(result["safety_invariants"]["broker_orders"], 0)

    # ─────────────────────────────────────────────────────────────
    # 5. FASTAPI AUTOPILOT ENDPOINTS
    # ─────────────────────────────────────────────────────────────

    def test_08_post_run_autonomous_endpoint(self):
        res = self.client.post("/api/research-autopilot/run-autonomous", json={"batch_size": 2})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["status"], "success")
        self.assertIn("mission_id", data)
        self.assertIn("stats", data)
        self.assertIn("safety_audit", data)
        self.assertTrue(data["safety_audit"]["champions_intact"])
        self.assertEqual(data["safety_audit"]["portfolio_heat_pct"], 0.0)

    # ─────────────────────────────────────────────────────────────
    # 6. PRODUCTION SAFETY INVARIANTS
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
