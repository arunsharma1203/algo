"""
RESEARCH CANDIDATE VAULT
========================
Manages immutable candidate freezing, SHA-256 content addressing, artifact persistence,
and candidate lifecycle transitions.
Lifecycle states:
DISCOVERY -> SHORTLISTED -> FROZEN -> OOS_PENDING -> OOS_PASSED / OOS_FAILED
-> SHADOW_TESTING -> PROMOTION_CANDIDATE -> RETAINED / REJECTED -> ARCHIVED.
"""

import os
import json
import pickle
import hashlib
import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass, field, asdict

from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)

# Canonical absolute path to models/research/candidates
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
VAULT_STORAGE_DIR = os.path.join(BASE_DIR, "models", "research", "candidates")

@dataclass
class ResearchModelArtifact:
    """
    Contract for directional and cross-sectional quantitative ML model artifacts.
    Enforces strict provenance, serialization safety, and model object integrity.
    """
    artifact_type: str = "RESEARCH_MODEL"
    model_type: str = "LightGBM"  # LightGBM, CatBoost, XGBoost, DoubleEnsemble, VotingClassifier, etc.
    model_object: Any = None
    feature_version: str = "v1.0"
    feature_schema: List[str] = field(default_factory=list)
    target_definition: str = ""
    training_period: Dict[str, str] = field(default_factory=dict)
    validation_period: Dict[str, str] = field(default_factory=dict)
    oos_period: Dict[str, str] = field(default_factory=dict)
    config_hash: str = ""
    dataset_hash: str = ""
    code_version: str = "v1.0"
    artifact_sha256: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Enforces that the artifact contains a genuine trained model and required provenance."""
        if self.model_object is None:
            raise ValueError("ResearchModelArtifact validation failed: model_object cannot be None.")

        # Block mock dictionaries and weight stubs
        if isinstance(self.model_object, dict):
            if "mock_weights" in self.model_object or "fixed_weights" in self.model_object:
                raise ValueError("ResearchModelArtifact validation failed: Prohibited mock weights dictionary detected.")
            if len(self.model_object) == 1 and "weights" in self.model_object:
                raise ValueError("ResearchModelArtifact validation failed: Stub weights dictionary is not a valid model object.")
            if "model_type" in self.model_object and len(self.model_object) <= 2 and "weights" in self.model_object:
                raise ValueError("ResearchModelArtifact validation failed: Mock dictionary with weights is prohibited.")

        if isinstance(self.model_object, str):
            if any(term in self.model_object.lower() for term in ("mock", "dummy", "bytes_123", "placeholder")):
                raise ValueError(f"ResearchModelArtifact validation failed: Mock string '{self.model_object}' is prohibited.")

        if not self.feature_schema:
            raise ValueError("ResearchModelArtifact validation failed: feature_schema cannot be empty.")
        if not self.config_hash:
            raise ValueError("ResearchModelArtifact validation failed: config_hash cannot be empty.")
        if not self.target_definition:
            raise ValueError("ResearchModelArtifact validation failed: target_definition cannot be empty.")

@dataclass
class PortfolioStrategyArtifact:
    """
    Contract for portfolio selection, rebalancing, and alpha basket strategy artifacts.
    """
    artifact_type: str = "PORTFOLIO_STRATEGY"
    strategy_definition: str = ""
    model_object: Any = None
    feature_definition: Dict[str, Any] = field(default_factory=dict)
    portfolio_rules: Dict[str, Any] = field(default_factory=dict)
    costs_bps: float = 15.0
    slippage_bps: float = 5.0
    holding_period: int = 5
    rebalance_frequency: str = "DAILY"
    universe: str = "LIVE_52"
    config_hash: str = ""
    dataset_hash: str = ""
    artifact_sha256: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Enforces that the portfolio strategy contains valid rules and provenance."""
        if not self.strategy_definition:
            raise ValueError("PortfolioStrategyArtifact validation failed: strategy_definition cannot be empty.")
        if not self.config_hash:
            raise ValueError("PortfolioStrategyArtifact validation failed: config_hash cannot be empty.")
        if not self.portfolio_rules:
            raise ValueError("PortfolioStrategyArtifact validation failed: portfolio_rules cannot be empty.")

class CandidateVault:
    """
    Authoritative repository for frozen research candidate configurations and model artifacts.
    """

    @classmethod
    def _ensure_storage_dir(cls) -> None:
        os.makedirs(VAULT_STORAGE_DIR, exist_ok=True)

    @classmethod
    def freeze_candidate(
        cls,
        experiment_id: str,
        mission_id: str,
        config: Dict[str, Any],
        model_artifact: Any,
        metrics: Dict[str, Any],
        discovery_universe: str = "LIVE_52"
    ) -> Dict[str, Any]:
        """
        Freezes a shortlisted candidate with strict idempotency:
        1. Checks if candidate already exists for (mission_id, config_hash).
        2. If exists: returns existing candidate without duplicate disk/DB writes.
        3. If new: persists serialized artifact, computes SHA-256, and inserts single row.
        4. Sets initial status strictly to OOS_PENDING.
        """
        cls._ensure_storage_dir()
        
        # Compute authoritative canonical config_hash
        try:
            from app.analytics.research_orchestrator.experiment_generator import ExperimentGenerator
            config_hash = ExperimentGenerator.compute_config_hash(config)
        except Exception:
            config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()

        # Idempotency check: return existing record if already frozen for this mission
        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
            SELECT * FROM research_candidate_vault
            WHERE mission_id = ? AND config_hash = ?
            LIMIT 1;
        """, (mission_id, config_hash))
        existing = c.fetchone()
        if existing:
            conn.close()
            logger.info(f"[CandidateVault] Candidate already frozen for config_hash {config_hash[:12]} ({existing['candidate_id']}). Idempotent return.")
            return {
                "candidate_id": existing["candidate_id"],
                "status": existing["status"],
                "verdict": "INSUFFICIENT NEW OOS DATA",
                "artifact_path": existing["artifact_path"],
                "artifact_sha256": existing["artifact_sha256"],
                "config_hash": existing["config_hash"]
            }

        # Deterministic candidate identity derived from config_hash
        candidate_id = f"cand_{config_hash[:16]}"
        
        # Enforce Real Model Artifact Validation
        if isinstance(model_artifact, (ResearchModelArtifact, PortfolioStrategyArtifact)):
            model_artifact.config_hash = config_hash
            model_artifact.validate()
            final_artifact = model_artifact
        elif hasattr(model_artifact, "predict") or hasattr(model_artifact, "predict_proba"):
            # Wrap raw trained model estimator into ResearchModelArtifact
            m_type = getattr(model_artifact, "__class__", type(model_artifact)).__name__
            final_artifact = ResearchModelArtifact(
                artifact_type="RESEARCH_MODEL",
                model_type=m_type,
                model_object=model_artifact,
                feature_schema=config.get("feature_schema", ["alpha_factors"]),
                target_definition=f"Return horizon {config.get('horizon_days', 10)}d",
                config_hash=config_hash,
                dataset_hash=config.get("dataset_hash", "real_ohlcv_dataset"),
                training_period=config.get("training_period", {}),
                validation_period=config.get("validation_period", {}),
                oos_period=config.get("oos_period", {})
            )
            final_artifact.validate()
        else:
            raise ValueError(
                f"CandidateVault Error: Candidate {experiment_id} artifact validation failed. "
                f"Artifact must be an instance of ResearchModelArtifact, PortfolioStrategyArtifact, "
                f"or a trained estimator with predict/predict_proba. Received: {type(model_artifact).__name__}"
            )

        # Serialize model artifact
        artifact_filename = f"{candidate_id}.pkl"
        artifact_path = os.path.join(VAULT_STORAGE_DIR, artifact_filename)
        
        with open(artifact_path, "wb") as f:
            pickle.dump(final_artifact, f)

        # Compute SHA-256
        h = hashlib.sha256()
        with open(artifact_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        artifact_sha256 = h.hexdigest()
        if hasattr(final_artifact, "artifact_sha256"):
            final_artifact.artifact_sha256 = artifact_sha256

        # Initial universe transfer status map
        initial_universe_status = {
            "LIVE_52": "PASSED" if discovery_universe == "LIVE_52" else "NOT_TESTED",
            "NIFTY_100": "NOT_TESTED",
            "NIFTY_200": "NOT_TESTED",
            "NIFTY_500": "NOT_TESTED"
        }

        now = datetime.now().isoformat()
        try:
            c.execute("""
            INSERT INTO research_candidate_vault (
                candidate_id, experiment_id, mission_id, status, discovery_universe,
                config_hash, artifact_path, artifact_sha256, metrics_json,
                universe_transfer_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                candidate_id, experiment_id, mission_id, "OOS_PENDING",
                discovery_universe, config_hash, artifact_path, artifact_sha256,
                json.dumps(metrics), json.dumps(initial_universe_status), now, now
            ))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
        finally:
            conn.close()

        logger.info(f"[CandidateVault] Successfully frozen candidate {candidate_id} (SHA256: {artifact_sha256[:12]}...)")

        return {
            "candidate_id": candidate_id,
            "status": "OOS_PENDING",
            "verdict": "INSUFFICIENT NEW OOS DATA",
            "artifact_path": artifact_path,
            "artifact_sha256": artifact_sha256,
            "config_hash": config_hash
        }

    @classmethod
    def load_artifact(cls, candidate_id: str) -> Any:
        """
        Loads and verifies the serialized artifact for a given candidate.
        Performs byte-level SHA-256 integrity verification against DB record.
        """
        cand = cls.get_candidate(candidate_id)
        if not cand:
            raise ValueError(f"Candidate {candidate_id} not found in vault.")

        artifact_path = cand.get("artifact_path")
        if not artifact_path or not os.path.exists(artifact_path):
            raise FileNotFoundError(f"Artifact file missing on disk for candidate {candidate_id}: {artifact_path}")

        # Compute SHA-256
        h = hashlib.sha256()
        with open(artifact_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        disk_sha = h.hexdigest()

        expected_sha = cand.get("artifact_sha256")
        if expected_sha and disk_sha != expected_sha:
            raise ValueError(
                f"Artifact integrity violation: SHA-256 mismatch for {candidate_id}. "
                f"Expected {expected_sha}, computed {disk_sha}."
            )

        with open(artifact_path, "rb") as f:
            obj = pickle.load(f)

        return obj

    @classmethod
    def get_candidate(cls, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves candidate record from vault with artifact provenance classification."""
        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM research_candidate_vault WHERE candidate_id = ?", (candidate_id,))
        row = c.fetchone()
        conn.close()

        if not row:
            return None

        res = dict(row)
        res["metrics"] = json.loads(res["metrics_json"]) if res.get("metrics_json") else {}
        res["universe_transfer"] = json.loads(res["universe_transfer_json"]) if res.get("universe_transfer_json") else {}

        # Audit artifact on disk for legacy vs genuine classification
        art_path = res.get("artifact_path")
        res["artifact_classification"] = "UNKNOWN"
        res["is_production_eligible"] = False

        if art_path and os.path.exists(art_path):
            try:
                with open(art_path, "rb") as f:
                    sample_obj = pickle.load(f)
                if isinstance(sample_obj, (ResearchModelArtifact, PortfolioStrategyArtifact)):
                    res["artifact_classification"] = "GENUINE_TRAINED_ARTIFACT"
                    res["is_production_eligible"] = True
                    res["model_type"] = getattr(sample_obj, "model_type", "STRATEGY")
                elif hasattr(sample_obj, "predict") or hasattr(sample_obj, "predict_proba"):
                    res["artifact_classification"] = "GENUINE_TRAINED_ARTIFACT"
                    res["is_production_eligible"] = True
                    res["model_type"] = type(sample_obj).__name__
                elif isinstance(sample_obj, (dict, str)):
                    res["artifact_classification"] = "LEGACY_INVALID_ARTIFACT"
                    res["is_production_eligible"] = False
                    res["ineligibility_reason"] = "Legacy candidate artifact contains mock dictionary or stub weights, not a trained model."
            except Exception as e:
                res["artifact_classification"] = "CORRUPTED_ARTIFACT"
                res["is_production_eligible"] = False
                res["ineligibility_reason"] = f"Artifact deserialization error: {e}"
        else:
            res["artifact_classification"] = "MISSING_ARTIFACT"
            res["is_production_eligible"] = False
            res["ineligibility_reason"] = "Artifact file missing on disk."
        return res

    @classmethod
    def list_candidates(cls, mission_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lists all frozen candidates in the vault."""
        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        if mission_id:
            c.execute("SELECT * FROM research_candidate_vault WHERE mission_id = ? ORDER BY created_at DESC;", (mission_id,))
        else:
            c.execute("SELECT * FROM research_candidate_vault ORDER BY created_at DESC;")

        rows = c.fetchall()
        conn.close()

        result = []
        for r in rows:
            d = dict(r)
            d["metrics"] = json.loads(d["metrics_json"]) if d.get("metrics_json") else {}
            d["universe_transfer"] = json.loads(d["universe_transfer_json"]) if d.get("universe_transfer_json") else {}
            result.append(d)
        return result

    @classmethod
    def update_universe_transfer_status(
        cls,
        candidate_id: str,
        universe_name: str,
        status: str,
        eval_metrics: Optional[Dict[str, Any]] = None
    ) -> None:
        """Updates universe transfer evaluation result for a candidate."""
        cand = cls.get_candidate(candidate_id)
        if not cand:
            return

        transfer_map = cand.get("universe_transfer", {})
        transfer_map[universe_name] = {
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "metrics": eval_metrics or {}
        }

        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("""
            UPDATE research_candidate_vault
            SET universe_transfer_json = ?, updated_at = ?
            WHERE candidate_id = ?
        """, (json.dumps(transfer_map), datetime.now().isoformat(), candidate_id))
        conn.commit()
        conn.close()

