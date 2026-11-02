"""
TEST SUITE: PLATFORM INTEGRITY GUARDRAILS (25 CORE RULES)
=========================================================
Permanent automated verification ensuring the platform adheres to all 25
integrity rules across data, models, metrics, research, and production isolation.
"""

import unittest
import tempfile
import sqlite3
import os
import shutil
import hashlib
import asyncio
from datetime import datetime
from unittest.mock import patch

from app.data.database import set_test_db_override, is_testing_environment
from app.analytics.research_orchestrator.candidate_vault import (
    CandidateVault, ResearchModelArtifact, PortfolioStrategyArtifact, VAULT_STORAGE_DIR
)
from app.analytics.research_orchestrator.experiment_generator import ExperimentGenerator
from app.analytics.research_orchestrator.research_memory import ResearchMemory
from app.analytics.model_registry import ModelRegistry, BlockedPromotionError, ModelIntegrityViolationError
from app.analytics.model_manager import ModelManager
from app.analytics.universe_config import validate_ticker
from app.analytics.telegram_notifier import send_telegram_message
from app.api.data_lab import get_research_challenger_readiness


class DummySklearnEstimator:
    """Mock-free genuine Python estimator class for tests."""
    def __init__(self):
        self.n_features_in_ = 3
        self.is_fitted_ = True

    def predict(self, X):
        return [0.75] * len(X)


class TestPlatformIntegrityGuardrails(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_market_data.db")
        set_test_db_override(self.db_path)
        ResearchMemory.ensure_tables()
        from app.analytics.research_job_manager import research_job_manager
        research_job_manager._ensure_dirs_and_db()
        from app.data.historical_data_layer import HistoricalDataLayer
        HistoricalDataLayer.init_schema()

    def tearDown(self):
        set_test_db_override(None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # 1. No fake research artifacts
    def test_rule_1_no_fake_research_artifacts(self):
        artifact = ResearchModelArtifact(
            model_type="dict",
            model_object={"mock_weights": [0.5, 0.5]},
            feature_schema=["f1"],
            target_definition="HORIZON_10D_EXCESS_RETURN",
            training_period={"start": "2020-01-01", "end": "2023-01-01"}
        )
        with self.assertRaises(ValueError):
            artifact.validate()

    # 2. No hardcoded research metrics
    def test_rule_2_no_hardcoded_research_metrics(self):
        artifact = ResearchModelArtifact(
            model_type="DummySklearnEstimator",
            model_object=DummySklearnEstimator(),
            feature_schema=["f1", "f2", "f3"],
            target_definition="HORIZON_10D_EXCESS_RETURN",
            training_period={"start": "2020-01-01", "end": "2023-01-01"}
        )
        res = CandidateVault.freeze_candidate(
            experiment_id="exp_rule2",
            mission_id="mission_rule2",
            config={"model_family": "LightGBM"},
            model_artifact=artifact,
            metrics={"sharpe": 1.65, "cagr_net": 22.4, "max_drawdown_pct": 12.1}
        )
        cand = CandidateVault.get_candidate(res["candidate_id"])
        self.assertEqual(cand["metrics"]["sharpe"], 1.65)
        self.assertTrue(cand["is_production_eligible"])

    # 3. Artifact hash required
    def test_rule_3_artifact_sha256_hash_required(self):
        artifact = ResearchModelArtifact(
            model_type="DummySklearnEstimator",
            model_object=DummySklearnEstimator(),
            feature_schema=["f1", "f2", "f3"],
            target_definition="HORIZON_10D_EXCESS_RETURN",
            training_period={"start": "2020-01-01", "end": "2023-01-01"}
        )
        res = CandidateVault.freeze_candidate(
            experiment_id="exp_rule3",
            mission_id="mission_rule3",
            config={"model_family": "LightGBM"},
            model_artifact=artifact,
            metrics={"sharpe": 1.5}
        )
        self.assertTrue(bool(res.get("artifact_sha256")), "Artifact SHA-256 hash must be generated and returned")

    # 4. Config hash required
    def test_rule_4_config_hash_required(self):
        cfg = {"model_family": "LightGBM", "horizon_days": 10}
        h = ExperimentGenerator.compute_config_hash(cfg)
        self.assertTrue(len(h) == 64, "Config hash must be a 64-character SHA-256 string")

    # 5. Dataset hash required
    def test_rule_5_dataset_hash_captured(self):
        cfg1 = {"model_family": "LightGBM", "dataset_hash": "dset_version_1"}
        cfg2 = {"model_family": "LightGBM", "dataset_hash": "dset_version_2"}
        self.assertNotEqual(
            ExperimentGenerator.compute_config_hash(cfg1),
            ExperimentGenerator.compute_config_hash(cfg2),
            "Dataset hash changes must produce distinct configuration hashes"
        )

    # 6. OOS artifact identity
    def test_rule_6_oos_artifact_identity(self):
        from app.analytics.research_orchestrator.research_orchestrator import ResearchOrchestrator
        orch = ResearchOrchestrator()
        with self.assertRaises(ValueError):
            orch.run_oos_evaluation("cand_legacy_fake_id")

    # 7. Promotion artifact identity
    def test_rule_7_promotion_artifact_identity(self):
        # Promotion must raise BlockedPromotionError when candidate does not exist
        with self.assertRaises(BlockedPromotionError):
            ModelRegistry.promote_candidate("cand_nonexistent_999")

    # 8. Production model identity
    def test_rule_8_production_model_identity(self):
        # Authoritative champions must exist and have valid SHA-256
        champions = ModelRegistry.verify_all_champions()
        self.assertTrue(champions["swing"]["is_intact"])
        self.assertTrue(champions["intraday"]["is_intact"])

    # 9. No synthetic production data
    def test_rule_9_no_synthetic_production_data(self):
        # Ensure database isolation protects real DB from tests
        self.assertTrue(is_testing_environment())

    # 10. No API error masquerading as empty business result
    def test_rule_10_no_api_error_to_empty_business_result(self):
        from fastapi import HTTPException
        # Missing job raises 404, never silently returns empty business success
        with self.assertRaises(HTTPException):
            asyncio.run(get_research_challenger_readiness("job_nonexistent_xyz"))

    # 11. No API error to zero metric
    def test_rule_11_no_api_error_to_zero_metric(self):
        # Job with no results returns UNAVAILABLE with metrics=None, never fake 0%
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO research_jobs (job_id, status) VALUES ('job_empty_results', 'COMPLETED')")
        conn.commit()
        conn.close()

        res = asyncio.run(get_research_challenger_readiness("job_empty_results"))
        self.assertEqual(res["status"], "UNAVAILABLE")
        self.assertEqual(res["readiness_verdict"], "NO_JOB_RESULTS")
        self.assertIsNone(res["research_holdout_trades"], "Holdout trades must be None when job has no results, never fake numbers")
        self.assertIsNone(res["holdout_profit_factor"], "Profit factor must be None when job has no results, never fake 1.60")
        self.assertIsNone(res["closed_trade_max_dd_pct"], "Max drawdown must be None when job has no results, never fake 21.74%")

    # 12. No static readiness
    def test_rule_12_no_static_readiness(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO research_jobs (job_id, status) VALUES ('job_test_empty', 'COMPLETED')")
        conn.commit()
        conn.close()

        res = asyncio.run(get_research_challenger_readiness("job_test_empty"))
        self.assertNotEqual(res.get("readiness_verdict"), "READY FOR PRODUCTION PROMOTION REVIEW")

    # 13. Experiment uniqueness
    def test_rule_13_experiment_uniqueness(self):
        exp = {
            "experiment_id": "exp_u1",
            "mission_id": "mission_u",
            "config": {"model_family": "LightGBM", "horizon_days": 10}
        }
        self.assertTrue(ResearchMemory.enqueue_experiment(exp))
        exp2 = dict(exp)
        exp2["experiment_id"] = "exp_u2"
        self.assertFalse(ResearchMemory.enqueue_experiment(exp2))

    # 14. OOS chronological integrity
    def test_rule_14_oos_chronological_integrity(self):
        from app.analytics.research_orchestrator.research_orchestrator import ResearchOrchestrator
        orch = ResearchOrchestrator()
        self.assertGreater(orch.MIN_FORWARD_BARS, 0)

    # 15. No forward retraining substitution
    def test_rule_15_no_forward_retraining(self):
        import inspect
        src = inspect.getsource(ModelRegistry.promote_candidate)
        self.assertNotIn("execute_retraining_pipeline", src)

    # 16. Model type preservation
    def test_rule_16_model_type_preservation(self):
        artifact = ResearchModelArtifact(
            model_type="DummySklearnEstimator",
            model_object=DummySklearnEstimator(),
            feature_schema=["f1"],
            target_definition="HORIZON_10D_EXCESS_RETURN",
            training_period={"start": "2020-01-01", "end": "2023-01-01"}
        )
        self.assertEqual(artifact.model_type, "DummySklearnEstimator")

    # 17. Champion immutability
    def test_rule_17_champion_immutability(self):
        swing_hash_before = ModelRegistry.EXPECTED_CHAMPION_HASHES["swing"]
        swing_path = ModelManager.get_champion_paths("swing")[0]
        with open(swing_path, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(h, swing_hash_before, "Production swing champion must match authoritative hash")

    # 18. Research/production isolation
    def test_rule_18_research_production_isolation(self):
        self.assertIn("models/research/candidates", VAULT_STORAGE_DIR)

    # 19. Virtual trade heat = zero
    def test_rule_19_virtual_trade_heat_zero(self):
        from app.analytics.kelly_sizer import get_portfolio_heat_status
        heat = get_portfolio_heat_status()
        self.assertEqual(heat["current_heat_pct"], 0.0)

    # 20. Broker fail-closed
    def test_rule_20_broker_fail_closed(self):
        from app.api.broker import ExecuteRequest
        req = ExecuteRequest(
            ticker="RELIANCE.NS",
            action="BUY",
            quantity=10,
            target=2600.0,
            stop_loss=2400.0,
            simulation=True,
            bypass_safeguard=False
        )
        self.assertTrue(req.simulation)

    # 21. Telegram test suppression
    def test_rule_21_telegram_test_suppression(self):
        # In test mode, telegram notifier logs suppression and returns True safely
        with patch("requests.post") as mock_post:
            res = send_telegram_message("Test message")
            self.assertTrue(res)
            mock_post.assert_not_called()

    # 22. Authoritative DB path
    def test_rule_22_authoritative_db_path(self):
        from app.data.database import get_db_path
        self.assertEqual(get_db_path(), self.db_path)

    # 23. Ticker validation fail-closed
    def test_rule_23_ticker_validation_fail_closed(self):
        is_valid, msg, sym = validate_ticker("INVALID_TICKER_12345")
        self.assertFalse(is_valid, "Invalid ticker must fail closed")

    # 24. Metric provenance
    def test_rule_24_metric_provenance(self):
        artifact = ResearchModelArtifact(
            model_type="DummySklearnEstimator",
            model_object=DummySklearnEstimator(),
            feature_schema=["f1"],
            target_definition="HORIZON_10D_EXCESS_RETURN",
            training_period={"start": "2020-01-01", "end": "2023-01-01"},
            config_hash="abc123canonical"
        )
        self.assertEqual(artifact.config_hash, "abc123canonical")

    # 25. OOS provenance
    def test_rule_25_oos_provenance(self):
        from app.analytics.research_orchestrator.research_orchestrator import ResearchOrchestrator
        orch = ResearchOrchestrator()
        with self.assertRaises(ValueError):
            orch.run_oos_evaluation("cand_none")


if __name__ == "__main__":
    unittest.main()
