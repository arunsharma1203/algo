"""
TEST SUITE: MODEL REGISTRY & REAL PROMOTION PATH (MILESTONE 3)
==============================================================
Validates that:
1. ModelRegistry enforces cryptographic provenance:
   candidate_artifact_sha256 == oos_evaluated_sha256 == deployed_sha256.
2. Candidates with mock weights, invalid artifacts, or unpassed OOS cannot be promoted.
3. Promotion in test environment blocks mutation of production champion models.
4. Isolated promotion successfully archives previous champion, deploys artifact, and verifies hash.
5. Invariant: zero mutation of production database or production champion files.
"""

import os
import sys
import json
import shutil
import pickle
import hashlib
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.data.database import set_test_db_override
from app.analytics.research_orchestrator.candidate_vault import (
    ResearchModelArtifact,
    CandidateVault,
    VAULT_STORAGE_DIR
)
from app.analytics.model_manager import (
    ModelManager,
    ProductionModelMutationBlockedError
)
from app.analytics.model_registry import (
    ModelRegistry,
    BlockedPromotionError
)
from app.analytics.qlib_discovery.model_trainer import create_model

class TestModelRegistryRealPromotion(unittest.TestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db.name
        self.temp_db.close()

        set_test_db_override(self.temp_db_path)

        import sqlite3
        conn = sqlite3.connect(self.temp_db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS research_candidate_vault (
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

        ModelRegistry.ensure_tables()
        self.created_artifacts = []

    def tearDown(self):
        set_test_db_override(None)

        for p in self.created_artifacts:
            if os.path.exists(p):
                os.remove(p)

        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    def _train_dummy_estimator(self):
        m = create_model("LightGBM", random_state=42)
        X = np.random.RandomState(42).normal(0, 1, (30, 5))
        y = (X[:, 0] > 0).astype(int)
        m.fit(X, y)
        return m

    def test_01_unpassed_oos_candidate_blocked(self):
        """Candidate with status OOS_PENDING or OOS_FAILED is blocked from promotion."""
        m = self._train_dummy_estimator()
        cand = CandidateVault.freeze_candidate(
            experiment_id="exp_prom_1",
            mission_id="m_prom_1",
            config={"model_family": "LightGBM", "p": 1},
            model_artifact=m,
            metrics={"sharpe": 1.5}
        )
        self.created_artifacts.append(cand["artifact_path"])

        # Status is currently OOS_PENDING
        with self.assertRaises(BlockedPromotionError) as ctx:
            ModelRegistry.promote_candidate(cand["candidate_id"], timeframe="swing")
        self.assertIn("OOS_PASSED", str(ctx.exception))

    def test_02_legacy_mock_candidate_blocked(self):
        """Candidate with legacy mock artifact is blocked from promotion."""
        mock_path = os.path.join(VAULT_STORAGE_DIR, "cand_mock_prom.pkl")
        with open(mock_path, "wb") as f:
            pickle.dump({"mock_weights": [0.1, 0.2]}, f)
        self.created_artifacts.append(mock_path)

        import sqlite3
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("""
            INSERT INTO research_candidate_vault (
                candidate_id, experiment_id, mission_id, status, discovery_universe,
                config_hash, artifact_path, artifact_sha256, metrics_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            "cand_mock_prom", "exp_m", "m_m", "OOS_PASSED", "LIVE_52",
            "cfg_m", mock_path, "sha_m", json.dumps({"sharpe": 1.5}), "now", "now"
        ))
        conn.commit()
        conn.close()

        with self.assertRaises(BlockedPromotionError) as ctx:
            ModelRegistry.promote_candidate("cand_mock_prom", timeframe="swing")
        self.assertIn("Prohibited artifact classification", str(ctx.exception))

    def test_03_test_environment_blocks_production_file_mutation(self):
        """Automated test mode prevents overwriting production champion model paths."""
        m = self._train_dummy_estimator()
        cand = CandidateVault.freeze_candidate(
            experiment_id="exp_prom_3",
            mission_id="m_prom_3",
            config={"model_family": "LightGBM", "p": 3},
            model_artifact=m,
            metrics={"sharpe": 1.8}
        )
        self.created_artifacts.append(cand["artifact_path"])

        # Mark candidate as OOS_PASSED in vault
        import sqlite3
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("UPDATE research_candidate_vault SET status = 'OOS_PASSED' WHERE candidate_id = ?", (cand["candidate_id"],))
        conn.commit()
        conn.close()

        # Should be blocked by test environment guard
        with self.assertRaises(ProductionModelMutationBlockedError):
            ModelRegistry.promote_candidate(cand["candidate_id"], timeframe="swing")

    def test_04_isolated_promotion_verifies_hash_and_registry(self):
        """Simulated isolated promotion deploys model, verifies byte hash, and updates registry."""
        m = self._train_dummy_estimator()
        cand = CandidateVault.freeze_candidate(
            experiment_id="exp_prom_4",
            mission_id="m_prom_4",
            config={"model_family": "LightGBM", "p": 4},
            model_artifact=m,
            metrics={"sharpe": 2.2}
        )
        self.created_artifacts.append(cand["artifact_path"])

        import sqlite3
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("UPDATE research_candidate_vault SET status = 'OOS_PASSED' WHERE candidate_id = ?", (cand["candidate_id"],))
        conn.commit()
        conn.close()

        # Create isolated temporary target directory for champion files
        temp_model_dir = tempfile.mkdtemp()
        temp_target_pkl = os.path.join(temp_model_dir, "champion_ensemble.pkl")
        temp_target_meta = os.path.join(temp_model_dir, "champion_metadata.json")
        temp_versions_dir = os.path.join(temp_model_dir, "versions")
        os.makedirs(temp_versions_dir, exist_ok=True)

        with open(temp_target_pkl, "wb") as f:
            pickle.dump(m, f)
        with open(temp_target_meta, "w") as f:
            json.dump({"version": "v1.0-test", "champion_f1": 0.65}, f)

        # Allow promotion in test with mocked model paths
        ModelRegistry._ALLOW_PRODUCTION_PROMOTION_IN_TEST = True
        try:
            with patch.object(ModelManager, "get_champion_paths", return_value=(temp_target_pkl, temp_target_meta)), \
                 patch.object(ModelManager, "get_versions_dir", return_value=temp_versions_dir):
                res = ModelRegistry.promote_candidate(cand["candidate_id"], timeframe="swing", confirm=True)

                self.assertEqual(res["status"], "PROMOTED")
                self.assertEqual(res["artifact_sha256"], cand["artifact_sha256"])

                # Verify deployed file SHA-256 matches candidate artifact
                with open(temp_target_pkl, "rb") as f:
                    deployed_sha = hashlib.sha256(f.read()).hexdigest()
                self.assertEqual(deployed_sha, cand["artifact_sha256"])

                # Verify model_registry record in DB
                models = ModelRegistry.get_registered_models("swing")
                self.assertGreaterEqual(len(models), 1)
                active = [mod for mod in models if mod["role"] == "CHAMPION"][0]
                self.assertEqual(active["candidate_id"], cand["candidate_id"])
                self.assertEqual(active["artifact_sha256"], cand["artifact_sha256"])
        finally:
            ModelRegistry._ALLOW_PRODUCTION_PROMOTION_IN_TEST = False
            shutil.rmtree(temp_model_dir, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
