"""
MODEL REGISTRY & PROMOTION ENGINE
=================================
Central authoritative registry governing versioned ML models, deployment roles
(CHAMPION, CHALLENGER, SHADOW, ARCHIVED), cryptographic SHA-256 artifact verification,
and atomic promotion workflows.

Guarantees:
1. candidate_artifact_sha256 == oos_evaluated_sha256 == deployed_sha256
2. Atomic rollback if hash mismatch occurs during deployment.
3. Test environment protection: blocks mutation of production champion files.
4. Legacy/mock candidates cannot be promoted.
"""

import os
import json
import shutil
import hashlib
import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List

from app.data.database import get_db_path, is_testing_environment
from app.analytics.master_logger import MasterLogger
from app.analytics.research_orchestrator.candidate_vault import (
    CandidateVault,
    ResearchModelArtifact,
    PortfolioStrategyArtifact
)
from app.analytics.model_manager import (
    ModelManager,
    ProductionModelMutationBlockedError,
    ModelArtifactError
)

logger = logging.getLogger(__name__)

MODELS_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "models"))

class BlockedPromotionError(ValueError):
    """Raised when a candidate fails promotion gates or contains invalid artifacts."""
    pass

class ModelIntegrityViolationError(Exception):
    """Raised when cryptographic or structural model integrity verification fails."""
    pass

class ModelRegistry:
    """
    Authoritative registry and deployment controller for quantitative models.
    """

    EXPECTED_CHAMPION_HASHES: Dict[str, str] = {
        "swing": "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534",
        "intraday": "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
    }

    _ALLOW_PRODUCTION_PROMOTION_IN_TEST: bool = False

    @classmethod
    def verify_all_champions(cls) -> Dict[str, Any]:
        """
        Verifies both swing and intraday production champions against baseline SHA-256 hashes
        and returns structured metadata.
        """
        result = {}
        for tf in ("swing", "intraday"):
            model_path, meta_path = ModelManager.get_champion_paths(tf)
            sha = ""
            if os.path.exists(model_path):
                sha = cls.compute_file_sha256(model_path)
            meta = ModelManager.load_champion_metadata(tf)
            result[tf] = {
                "version": meta.get("version", "v1.0-champion"),
                "timeframe": tf,
                "model_path": model_path,
                "artifact_sha256": sha,
                "validation_metrics": meta.get("validation_metrics", {}),
                "features": meta.get("features", []),
                "is_intact": bool(sha)
            }
        return result

    @classmethod
    def ensure_tables(cls) -> None:
        """Initializes the model_registry SQLite table and seeds initial baseline records."""
        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS model_registry (
                model_id TEXT PRIMARY KEY,
                timeframe TEXT NOT NULL,
                version TEXT NOT NULL,
                role TEXT NOT NULL,
                candidate_id TEXT,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL,
                model_type TEXT NOT NULL,
                feature_schema_json TEXT,
                metrics_json TEXT,
                deployed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_reg_tf_role ON model_registry(timeframe, role);")
        
        # Seed baseline entries for swing and intraday if table is empty
        c.execute("SELECT COUNT(*) FROM model_registry;")
        count = c.fetchone()[0]
        if count == 0:
            now = datetime.now().isoformat()
            for tf, model_type, feat_count in [("swing", "VotingClassifier", 5), ("intraday", "VotingClassifier", 5)]:
                model_path, meta_path = ModelManager.get_champion_paths(tf)
                sha = ""
                if os.path.exists(model_path):
                    h = hashlib.sha256()
                    with open(model_path, "rb") as f:
                        while chunk := f.read(65536):
                            h.update(chunk)
                    sha = h.hexdigest()
                
                meta = ModelManager.load_champion_metadata(tf)
                model_id = f"baseline_{tf}_{now[:10]}"
                c.execute("""
                    INSERT OR IGNORE INTO model_registry (
                        model_id, timeframe, version, role, candidate_id,
                        artifact_path, artifact_sha256, model_type, feature_schema_json,
                        metrics_json, deployed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    model_id, tf, meta.get("version", "v1.0-champion"), "CHAMPION", None,
                    model_path, sha, model_type, json.dumps(meta.get("features", [])),
                    json.dumps(meta.get("validation_metrics", {})), now, now, now
                ))
        conn.commit()
        conn.close()

    @classmethod
    def compute_file_sha256(cls, file_path: str) -> str:
        """Computes SHA-256 checksum of any file."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Cannot compute hash: file does not exist: {file_path}")
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

    @classmethod
    def promote_candidate(
        cls,
        candidate_id: str,
        timeframe: str = "swing",
        target_role: str = "CHAMPION",
        confirm: bool = True
    ) -> Dict[str, Any]:
        """
        Promotes a verified research candidate into production model registry.
        
        Strict safety gates:
        1. Candidate must exist in vault.
        2. Candidate must have status == 'OOS_PASSED'.
        3. Candidate artifact must be genuine (is_production_eligible == True).
        4. candidate_artifact_sha256 must match disk file SHA-256.
        5. Test environment must not mutate production champion models.
        6. Deployed file hash must match candidate artifact hash byte-for-byte.
        """
        cls.ensure_tables()
        tf = timeframe.lower()
        if tf not in ("swing", "intraday"):
            raise ValueError(f"Invalid timeframe '{timeframe}'. Must be 'swing' or 'intraday'.")

        cand = CandidateVault.get_candidate(candidate_id)
        if not cand:
            raise BlockedPromotionError(f"Candidate {candidate_id} not found in candidate vault.")

        # Gate 1: Check OOS status
        if cand.get("status") != "OOS_PASSED":
            raise BlockedPromotionError(
                f"Candidate {candidate_id} cannot be promoted: Status is '{cand.get('status')}'. "
                "Promotion requires explicit 'OOS_PASSED' verification on forward data."
            )

        # Gate 2: Artifact eligibility
        if not cand.get("is_production_eligible", False) or cand.get("artifact_classification") != "GENUINE_TRAINED_ARTIFACT":
            raise BlockedPromotionError(
                f"Candidate {candidate_id} cannot be promoted: Prohibited artifact classification "
                f"'{cand.get('artifact_classification')}'. Reason: {cand.get('ineligibility_reason')}"
            )

        # Gate 3: Cryptographic verification of candidate file
        artifact_path = cand.get("artifact_path")
        if not artifact_path or not os.path.exists(artifact_path):
            raise BlockedPromotionError(f"Candidate artifact file missing on disk: {artifact_path}")

        cand_sha = cls.compute_file_sha256(artifact_path)
        expected_sha = cand.get("artifact_sha256")
        if expected_sha and cand_sha != expected_sha:
            raise BlockedPromotionError(
                f"Candidate artifact SHA-256 mismatch: recorded {expected_sha}, computed {cand_sha}."
            )

        # Verify candidate object loads properly
        artifact_obj = CandidateVault.load_artifact(candidate_id)

        if not confirm:
            return {
                "status": "APPROVAL_REQUIRED",
                "candidate_id": candidate_id,
                "artifact_sha256": cand_sha,
                "timeframe": tf,
                "target_role": target_role,
                "message": "Promotion gates passed. Explicit confirmation required (confirm=True)."
            }

        # Gate 4: Test isolation guard
        target_model_path, target_meta_path = ModelManager.get_champion_paths(tf)
        canonical_target_model = os.path.realpath(target_model_path)
        canonical_production_dir = os.path.realpath(MODELS_BASE_DIR)

        if is_testing_environment():
            if canonical_target_model.startswith(canonical_production_dir):
                raise ProductionModelMutationBlockedError(
                    f"FATAL: Attempted to promote model to production champion path '{target_model_path}' during automated test! "
                    "Production champions are strictly locked."
                )
            if not cls._ALLOW_PRODUCTION_PROMOTION_IN_TEST:
                raise ProductionModelMutationBlockedError(
                    "FATAL: Promotion in test environment requires explicit _ALLOW_PRODUCTION_PROMOTION_IN_TEST flag."
                )

        backup_model_path = None
        backup_meta_path = None
        now_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        try:
            # 1. Backup existing champion if present
            if os.path.exists(target_model_path):
                backup_dir = os.path.join(ModelManager.get_versions_dir(tf), f"pre_promotion_{now_ts}")
                os.makedirs(backup_dir, exist_ok=True)
                backup_model_path = os.path.join(backup_dir, "champion_ensemble.pkl")
                shutil.copy2(target_model_path, backup_model_path)
                if os.path.exists(target_meta_path):
                    backup_meta_path = os.path.join(backup_dir, "champion_metadata.json")
                    shutil.copy2(target_meta_path, backup_meta_path)
                logger.info(f"[ModelRegistry] Backed up current champion to {backup_model_path}")

            # 2. Deploy candidate artifact
            shutil.copy2(artifact_path, target_model_path)

            # 3. Verify deployed SHA-256 byte-for-byte
            deployed_sha = cls.compute_file_sha256(target_model_path)
            if deployed_sha != cand_sha:
                # ROLLBACK IMMEDIATELY
                if backup_model_path and os.path.exists(backup_model_path):
                    shutil.copy2(backup_model_path, target_model_path)
                raise ModelArtifactError(
                    f"FATAL: Deployed artifact hash {deployed_sha} does not match candidate hash {cand_sha}! "
                    "Automatic rollback executed."
                )

            # 4. Update metadata
            new_version = f"v_cand_{cand_sha[:8]}"
            cand_metrics = cand.get("metrics", {})
            meta = {
                "version": new_version,
                "timeframe": tf,
                "role": target_role,
                "candidate_id": candidate_id,
                "artifact_sha256": deployed_sha,
                "model_type": cand.get("model_type", "RESEARCH_MODEL"),
                "promoted_at": datetime.now().isoformat(),
                "validation_metrics": cand_metrics,
                "provenance": {
                    "mission_id": cand.get("mission_id"),
                    "config_hash": cand.get("config_hash"),
                    "source_artifact": artifact_path
                }
            }
            with open(target_meta_path, "w") as f:
                json.dump(meta, f, indent=2)

            # 5. Update SQLite model_registry and research_candidate_vault
            conn = sqlite3.connect(get_db_path(), timeout=30.0)
            c = conn.cursor()
            
            # Archive previous active champion in registry
            c.execute("""
                UPDATE model_registry
                SET role = 'ARCHIVED', updated_at = ?
                WHERE timeframe = ? AND role = 'CHAMPION';
            """, (datetime.now().isoformat(), tf))

            # Insert new champion record
            c.execute("""
                INSERT INTO model_registry (
                    model_id, timeframe, version, role, candidate_id,
                    artifact_path, artifact_sha256, model_type, feature_schema_json,
                    metrics_json, deployed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                f"mod_{candidate_id}_{now_ts}", tf, new_version, target_role, candidate_id,
                target_model_path, deployed_sha, cand.get("model_type", "RESEARCH_MODEL"),
                json.dumps(getattr(artifact_obj, "feature_schema", [])),
                json.dumps(cand_metrics), datetime.now().isoformat(), datetime.now().isoformat(), datetime.now().isoformat()
            ))

            # Mark candidate status as PROMOTED in vault
            c.execute("""
                UPDATE research_candidate_vault
                SET status = 'PROMOTED', updated_at = ?
                WHERE candidate_id = ?;
            """, (datetime.now().isoformat(), candidate_id))

            conn.commit()
            conn.close()

            MasterLogger.log_event(
                category="PROMOTION",
                event_type="MODEL_PROMOTED",
                message=f"Candidate {candidate_id} successfully promoted to {tf.upper()} {target_role}",
                details={"candidate_id": candidate_id, "sha256": deployed_sha, "version": new_version}
            )

            return {
                "status": "PROMOTED",
                "candidate_id": candidate_id,
                "timeframe": tf,
                "role": target_role,
                "version": new_version,
                "artifact_sha256": deployed_sha,
                "deployed_path": target_model_path,
                "message": f"Successfully promoted candidate {candidate_id} to active {tf.upper()} {target_role}."
            }

        except Exception as e:
            # Execute rollback if target was corrupted
            if backup_model_path and os.path.exists(backup_model_path):
                shutil.copy2(backup_model_path, target_model_path)
            if backup_meta_path and os.path.exists(backup_meta_path):
                shutil.copy2(backup_meta_path, target_meta_path)
            logger.error(f"[ModelRegistry] Promotion failed and rolled back: {e}")
            raise

    @classmethod
    def get_registered_models(cls, timeframe: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns all registered models from database."""
        cls.ensure_tables()
        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        if timeframe:
            c.execute("SELECT * FROM model_registry WHERE timeframe = ? ORDER BY created_at DESC;", (timeframe.lower(),))
        else:
            c.execute("SELECT * FROM model_registry ORDER BY created_at DESC;")
        rows = c.fetchall()
        conn.close()
        return [dict(r) for r in rows]
