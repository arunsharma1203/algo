"""
QLIB MODEL REGISTRY & PROVENANCE SYSTEM
=======================================
Authoritative model management for real Microsoft Qlib models.

Supported strategies:
- 'SWING'
- 'INTRADAY'
- 'FNO'

Invariants:
- 100% physically isolated from legacy Champion models (stored under `models/qlib/`).
- Every model has a content-addressed SHA-256 hash and cryptographic provenance manifest.
- Model loading enforces byte-for-byte SHA-256 verification before returning.
- If a model is missing or hash does not match, raises QlibModelIntegrityError (fail-closed).
"""

import os
import json
import pickle
import hashlib
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

import qlib

logger = logging.getLogger(__name__)

QLIB_MODELS_BASE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "models", "qlib")
)


class QlibModelIntegrityError(RuntimeError):
    """Raised when Qlib model is missing, corrupt, or hash verification fails."""
    pass


class QlibModelRegistry:
    """
    Registry for managing real Microsoft Qlib model artifacts and provenance.
    """

    @staticmethod
    def get_strategy_dir(strategy: str) -> str:
        strat = strategy.lower().strip()
        if strat not in ("swing", "intraday", "fno"):
            raise ValueError(f"Unknown strategy: {strategy}. Expected SWING, INTRADAY, or FNO.")
        d = os.path.join(QLIB_MODELS_BASE_DIR, strat)
        os.makedirs(d, exist_ok=True)
        return d

    @classmethod
    def save_model(
        cls,
        strategy: str,
        model_obj: Any,
        manifest_meta: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Saves a trained Qlib model artifact with cryptographic SHA-256 provenance.
        """
        strat = strategy.upper().strip()
        strat_dir = cls.get_strategy_dir(strat)
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        model_id = f"qlib_{strat.lower()}_{timestamp_str}"

        model_version_dir = os.path.join(strat_dir, model_id)
        os.makedirs(model_version_dir, exist_ok=True)

        artifact_file = os.path.join(model_version_dir, "model.pkl")
        manifest_file = os.path.join(model_version_dir, "manifest.json")

        # Pickle the Qlib model
        with open(artifact_file, "wb") as fp:
            pickle.dump(model_obj, fp)

        # Compute SHA-256
        h = hashlib.sha256()
        with open(artifact_file, "rb") as fp:
            h.update(fp.read())
        artifact_sha256 = h.hexdigest()

        # Build complete provenance manifest
        manifest = {
            "model_id": model_id,
            "strategy": strat,
            "engine": "QLIB",
            "qlib_version": getattr(qlib, "__version__", "unknown"),
            "model_class": manifest_meta.get("model_class", type(model_obj).__name__),
            "feature_handler": manifest_meta.get("feature_handler", "Alpha158"),
            "feature_version": manifest_meta.get("feature_version", "qlib_alpha158_v1"),
            "universe": manifest_meta.get("universe", "LIVE_52"),
            "data_hash": manifest_meta.get("data_hash", ""),
            "train_start": manifest_meta.get("train_start", ""),
            "train_end": manifest_meta.get("train_end", ""),
            "valid_start": manifest_meta.get("valid_start", ""),
            "valid_end": manifest_meta.get("valid_end", ""),
            "oos_start": manifest_meta.get("oos_start", ""),
            "oos_end": manifest_meta.get("oos_end", ""),
            "metrics": manifest_meta.get("metrics", {}),
            "artifact_path": artifact_file,
            "artifact_sha256": artifact_sha256,
            "status": "ACTIVE",
            "created_at": datetime.now().isoformat()
        }

        with open(manifest_file, "w") as fp:
            json.dump(manifest, fp, indent=2)

        # Update active pointer symlink / copy in strat_dir
        active_artifact = os.path.join(strat_dir, "active_model.pkl")
        active_manifest = os.path.join(strat_dir, "active_manifest.json")

        with open(active_artifact, "wb") as fp:
            with open(artifact_file, "rb") as src:
                fp.write(src.read())

        with open(active_manifest, "w") as fp:
            json.dump(manifest, fp, indent=2)

        logger.info(
            f"[QlibModelRegistry] Registered active {strat} model {model_id} (SHA256: {artifact_sha256[:12]}...)"
        )

        return manifest

    @classmethod
    def load_active_model(cls, strategy: str) -> Tuple[Any, Dict[str, Any]]:
        """
        Loads the active Qlib model for a strategy, verifying byte-for-byte SHA-256 integrity.
        Raises QlibModelIntegrityError if model is missing or hash check fails.
        """
        strat_dir = cls.get_strategy_dir(strategy)
        active_artifact = os.path.join(strat_dir, "active_model.pkl")
        active_manifest = os.path.join(strat_dir, "active_manifest.json")

        if not os.path.exists(active_artifact) or not os.path.exists(active_manifest):
            raise QlibModelIntegrityError(
                f"Active Qlib model for {strategy} is not available! "
                f"Path {active_artifact} does not exist. Train a Qlib model first."
            )

        with open(active_manifest, "r") as fp:
            manifest = json.load(fp)

        expected_sha = manifest.get("artifact_sha256")
        # Verify SHA256
        h = hashlib.sha256()
        with open(active_artifact, "rb") as fp:
            data = fp.read()
            h.update(data)
        actual_sha = h.hexdigest()

        if actual_sha != expected_sha:
            raise QlibModelIntegrityError(
                f"Qlib model corruption detected for {strategy}! "
                f"Expected SHA256: {expected_sha}, Actual SHA256: {actual_sha}"
            )

        model_obj = pickle.loads(data)
        return model_obj, manifest

    @classmethod
    def get_active_manifest(cls, strategy: str) -> Optional[Dict[str, Any]]:
        """Returns active model manifest without unpickling the model object."""
        strat_dir = cls.get_strategy_dir(strategy)
        active_manifest = os.path.join(strat_dir, "active_manifest.json")
        if not os.path.exists(active_manifest):
            return None
        with open(active_manifest, "r") as fp:
            return json.load(fp)

    @classmethod
    def get_registry_status(cls) -> Dict[str, Any]:
        """Returns overview of all Qlib model strategies."""
        status = {}
        for s in ("SWING", "INTRADAY", "FNO"):
            manifest = cls.get_active_manifest(s)
            if manifest:
                status[s.lower()] = {
                    "available": True,
                    "model_id": manifest.get("model_id"),
                    "model_class": manifest.get("model_class"),
                    "feature_handler": manifest.get("feature_handler"),
                    "artifact_sha256": manifest.get("artifact_sha256"),
                    "metrics": manifest.get("metrics"),
                    "created_at": manifest.get("created_at")
                }
            else:
                status[s.lower()] = {
                    "available": False,
                    "status": "NOT_TRAINED"
                }
        return status
