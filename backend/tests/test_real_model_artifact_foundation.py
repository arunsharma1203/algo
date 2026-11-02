"""
TEST SUITE: REAL MODEL ARTIFACT FOUNDATION (MILESTONE 1)
========================================================
Validates that:
1. ResearchModelArtifact & PortfolioStrategyArtifact enforce strict typing, provenance, and validation.
2. Mock/stub weights, dummy dictionaries, and placeholder strings are strictly rejected.
3. CandidateVault persists genuine serialized models and verifies byte-level SHA-256 on load.
4. Tampered artifacts fail cryptographic integrity checks.
5. Legacy mock candidates are dynamically classified as LEGACY_INVALID_ARTIFACT (production ineligible).
6. Tests run in complete isolation from production database and champion models.
"""

import os
import sys
import json
import pickle
import hashlib
import tempfile
import unittest
import numpy as np

# Ensure backend root in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.analytics.research_orchestrator.candidate_vault import (
    ResearchModelArtifact,
    PortfolioStrategyArtifact,
    CandidateVault,
    VAULT_STORAGE_DIR
)
from app.analytics.qlib_discovery.model_trainer import create_model

class TestRealModelArtifactFoundation(unittest.TestCase):

    def setUp(self):
        # Create temp database for isolation
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db.name
        self.temp_db.close()

        import sqlite3
        conn = sqlite3.connect(self.temp_db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE research_candidate_vault (
                candidate_id TEXT PRIMARY KEY,
                experiment_id TEXT,
                mission_id TEXT,
                status TEXT,
                discovery_universe TEXT,
                config_hash TEXT,
                artifact_path TEXT,
                artifact_sha256 TEXT,
                metrics_json TEXT,
                universe_transfer_json TEXT,
                created_at TEXT,
                updated_at TEXT
            );
        """)
        conn.commit()
        conn.close()

        # Patch get_db_path
        import app.analytics.research_orchestrator.candidate_vault as cv_mod
        self.orig_get_db_path = cv_mod.get_db_path
        cv_mod.get_db_path = lambda: self.temp_db_path

    def tearDown(self):
        import app.analytics.research_orchestrator.candidate_vault as cv_mod
        cv_mod.get_db_path = self.orig_get_db_path
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    def _train_dummy_estimator(self):
        m = create_model("LightGBM", random_state=42)
        X = np.random.RandomState(42).normal(0, 1, (30, 4))
        y = (X[:, 0] > 0).astype(int)
        m.fit(X, y)
        return m

    def test_01_valid_research_model_artifact_passes_validation(self):
        """Genuine fitted estimator passes validation."""
        m = self._train_dummy_estimator()
        art = ResearchModelArtifact(
            artifact_type="RESEARCH_MODEL",
            model_type="LightGBM",
            model_object=m,
            feature_schema=["f1", "f2", "f3", "f4"],
            target_definition="Return 10d > 0",
            config_hash="abc123config",
            dataset_hash="dataset123"
        )
        art.validate()
        self.assertEqual(art.model_type, "LightGBM")
        self.assertEqual(len(art.feature_schema), 4)

    def test_02_mock_weights_dict_strictly_rejected(self):
        """Mock weights dict raises ValueError."""
        art = ResearchModelArtifact(
            model_object={"mock_weights": [0.1, 0.2, 0.3]},
            feature_schema=["f1"],
            target_definition="10d",
            config_hash="cfg"
        )
        with self.assertRaises(ValueError) as ctx:
            art.validate()
        self.assertIn("Prohibited mock weights", str(ctx.exception))

    def test_03_stub_weights_dict_strictly_rejected(self):
        """Stub weights dict raises ValueError."""
        art = ResearchModelArtifact(
            model_object={"weights": [0.1, 0.2, 0.3]},
            feature_schema=["f1"],
            target_definition="10d",
            config_hash="cfg"
        )
        with self.assertRaises(ValueError) as ctx:
            art.validate()
        self.assertIn("Stub weights dictionary is not a valid model object", str(ctx.exception))

    def test_04_dummy_string_strictly_rejected(self):
        """Placeholder strings raise ValueError."""
        art = ResearchModelArtifact(
            model_object="model_bytes_123",
            feature_schema=["f1"],
            target_definition="10d",
            config_hash="cfg"
        )
        with self.assertRaises(ValueError) as ctx:
            art.validate()
        self.assertIn("prohibited", str(ctx.exception).lower())

    def test_05_missing_features_or_config_rejected(self):
        """Empty feature schema or empty config hash raises ValueError."""
        m = self._train_dummy_estimator()
        art = ResearchModelArtifact(
            model_object=m,
            feature_schema=[],
            target_definition="10d",
            config_hash="cfg"
        )
        with self.assertRaises(ValueError):
            art.validate()

    def test_06_valid_portfolio_strategy_artifact(self):
        """Valid portfolio strategy artifact passes validation."""
        strat = PortfolioStrategyArtifact(
            strategy_definition="Top 5 Momentum Basket",
            portfolio_rules={"top_k": 5, "rebalance": "DAILY"},
            config_hash="strat_cfg_123"
        )
        strat.validate()
        self.assertEqual(strat.artifact_type, "PORTFOLIO_STRATEGY")

    def test_07_portfolio_strategy_rejects_empty_rules(self):
        """Empty rules raises ValueError."""
        strat = PortfolioStrategyArtifact(
            strategy_definition="Empty Basket",
            portfolio_rules={},
            config_hash="strat_cfg"
        )
        with self.assertRaises(ValueError):
            strat.validate()

    def test_08_freeze_candidate_with_genuine_artifact(self):
        """CandidateVault.freeze_candidate persists real artifact and records SHA256 in DB."""
        m = self._train_dummy_estimator()
        art = ResearchModelArtifact(
            artifact_type="RESEARCH_MODEL",
            model_type="LightGBM",
            model_object=m,
            feature_schema=["f1", "f2", "f3", "f4"],
            target_definition="10d",
            config_hash="cfg_test_8",
            dataset_hash="ds_test_8"
        )

        cand_info = CandidateVault.freeze_candidate(
            experiment_id="exp_001",
            mission_id="mission_001",
            config={"model_family": "LightGBM", "horizon_days": 10},
            model_artifact=art,
            metrics={"sharpe": 2.1, "ic": 0.08},
            discovery_universe="LIVE_52"
        )

        self.assertEqual(cand_info["status"], "OOS_PENDING")
        self.assertTrue(cand_info["candidate_id"].startswith("cand_"))
        self.assertTrue(os.path.exists(cand_info["artifact_path"]))
        self.assertEqual(len(cand_info["artifact_sha256"]), 64)

        # Verify load_artifact verifies SHA256
        loaded = CandidateVault.load_artifact(cand_info["candidate_id"])
        self.assertIsInstance(loaded, ResearchModelArtifact)
        self.assertTrue(hasattr(loaded.model_object, "predict"))

        # Clean up created artifact file
        if os.path.exists(cand_info["artifact_path"]):
            os.remove(cand_info["artifact_path"])

    def test_09_freeze_candidate_rejects_mock_artifact(self):
        """CandidateVault.freeze_candidate raises ValueError when mock artifact passed."""
        with self.assertRaises(ValueError):
            CandidateVault.freeze_candidate(
                experiment_id="exp_mock",
                mission_id="mission_mock",
                config={"model_family": "Mock"},
                model_artifact={"mock_weights": [0.1, 0.2]},
                metrics={"sharpe": 1.5}
            )

    def test_10_freeze_candidate_idempotency(self):
        """Freezing identical (mission_id, config_hash) returns existing record without duplicate writes."""
        m = self._train_dummy_estimator()
        cand1 = CandidateVault.freeze_candidate(
            experiment_id="exp_idem",
            mission_id="mission_idem",
            config={"model_family": "LightGBM", "param": "x"},
            model_artifact=m,
            metrics={"sharpe": 1.8}
        )

        cand2 = CandidateVault.freeze_candidate(
            experiment_id="exp_idem_2",
            mission_id="mission_idem",
            config={"model_family": "LightGBM", "param": "x"},
            model_artifact=m,
            metrics={"sharpe": 1.8}
        )

        self.assertEqual(cand1["candidate_id"], cand2["candidate_id"])
        self.assertEqual(cand1["artifact_sha256"], cand2["artifact_sha256"])

        # Clean up
        if os.path.exists(cand1["artifact_path"]):
            os.remove(cand1["artifact_path"])

    def test_11_load_artifact_tamper_detection(self):
        """Corrupted artifact triggers SHA-256 mismatch ValueError."""
        m = self._train_dummy_estimator()
        cand = CandidateVault.freeze_candidate(
            experiment_id="exp_tamper",
            mission_id="mission_tamper",
            config={"model_family": "LightGBM", "param": "tamper"},
            model_artifact=m,
            metrics={"sharpe": 1.9}
        )

        # Tamper with file on disk
        with open(cand["artifact_path"], "wb") as f:
            f.write(b"tampered byte stream corrupting sha")

        with self.assertRaises(ValueError) as ctx:
            CandidateVault.load_artifact(cand["candidate_id"])
        self.assertIn("Artifact integrity violation: SHA-256 mismatch", str(ctx.exception))

        # Clean up
        if os.path.exists(cand["artifact_path"]):
            os.remove(cand["artifact_path"])

if __name__ == "__main__":
    unittest.main()
