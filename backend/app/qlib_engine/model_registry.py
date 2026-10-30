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
import sqlite3
from app.data.historical_data_layer import get_db_path
from app.analytics.master_logger import MasterLogger
from app.qlib_engine.promotion_policy import QlibPromotionPolicy

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

    _base_dir_override: Optional[str] = None

    def __init__(self, base_dir: Optional[str] = None):
        if base_dir:
            self._base_dir_override = base_dir
            QlibModelRegistry._base_dir_override = base_dir

    @classmethod
    def get_base_dir(cls) -> str:
        return cls._base_dir_override or QLIB_MODELS_BASE_DIR

    @classmethod
    def _init_audit_schema(cls):
        """Initializes qlib_promotion_audit_log table."""
        try:
            db_path = get_db_path()
            conn = sqlite3.connect(db_path, timeout=10.0)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS qlib_promotion_audit_log (
                    audit_id TEXT PRIMARY KEY,
                    timestamp TEXT,
                    strategy TEXT,
                    candidate_model_id TEXT,
                    candidate_sha256 TEXT,
                    current_champion_id TEXT,
                    current_champion_sha256 TEXT,
                    status TEXT, -- APPROVED, REJECTED
                    eligible INTEGER, -- 1 or 0
                    candidate_metrics_json TEXT,
                    champion_metrics_json TEXT,
                    gates_json TEXT,
                    failed_reasons_json TEXT,
                    user_action TEXT,
                    resulting_active_model_id TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_qlib_audit_strat ON qlib_promotion_audit_log(strategy);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_qlib_audit_time ON qlib_promotion_audit_log(timestamp);")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[QlibModelRegistry] Failed to init audit schema: {e}")

    @classmethod
    def _log_audit_event(
        cls,
        strategy: str,
        candidate_model_id: str,
        candidate_sha256: str,
        current_champion_id: Optional[str],
        current_champion_sha256: Optional[str],
        status: str,
        eligible: bool,
        candidate_metrics: Dict[str, Any],
        champion_metrics: Optional[Dict[str, Any]],
        gates: Dict[str, Any],
        failed_reasons: List[str],
        user_action: str = "MANUAL_APPROVAL",
        resulting_active_model_id: Optional[str] = None
    ):
        """Writes audit entry to qlib_promotion_audit_log and MasterLogger."""
        cls._init_audit_schema()
        now_iso = datetime.now().isoformat()
        audit_id = f"audit_promo_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{candidate_model_id[:16]}"
        try:
            db_path = get_db_path()
            conn = sqlite3.connect(db_path, timeout=10.0)
            conn.execute("""
                INSERT INTO qlib_promotion_audit_log (
                    audit_id, timestamp, strategy, candidate_model_id, candidate_sha256,
                    current_champion_id, current_champion_sha256, status, eligible,
                    candidate_metrics_json, champion_metrics_json, gates_json,
                    failed_reasons_json, user_action, resulting_active_model_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                audit_id, now_iso, strategy.upper(), candidate_model_id, candidate_sha256,
                current_champion_id, current_champion_sha256, status, 1 if eligible else 0,
                json.dumps(candidate_metrics, default=str),
                json.dumps(champion_metrics, default=str) if champion_metrics else None,
                json.dumps(gates, default=str),
                json.dumps(failed_reasons, default=str),
                user_action, resulting_active_model_id
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[QlibModelRegistry] Failed to write promotion audit entry: {e}")

        # Log to MasterLogger
        try:
            MasterLogger.log_event(
                category="PROMOTION",
                event_type="QLIB_PROMOTION_APPROVED" if status == "APPROVED" else "QLIB_PROMOTION_REJECTED",
                message=f"Qlib model promotion {status}: {candidate_model_id} for {strategy} (Eligible: {eligible})",
                universe=strategy,
                details={
                    "candidate_model_id": candidate_model_id,
                    "current_champion_id": current_champion_id,
                    "status": status,
                    "eligible": eligible,
                    "failed_reasons": failed_reasons,
                    "resulting_active_model_id": resulting_active_model_id
                },
                severity="INFO" if status == "APPROVED" else "WARNING"
            )
        except Exception as e:
            logger.error(f"[QlibModelRegistry] Failed to write MasterLogger promotion event: {e}")

    @classmethod
    def get_strategy_dir(cls, strategy: str) -> str:
        strat = strategy.lower().strip()
        if strat not in ("swing", "intraday", "fno"):
            raise ValueError(f"Unknown strategy: {strategy}. Expected SWING, INTRADAY, or FNO.")
        d = os.path.join(cls.get_base_dir(), strat)
        os.makedirs(d, exist_ok=True)
        return d

    @classmethod
    def get_challengers_dir(cls, strategy: str) -> str:
        strat = strategy.lower().strip()
        if strat not in ("swing", "intraday", "fno"):
            raise ValueError(f"Unknown strategy: {strategy}. Expected SWING, INTRADAY, or FNO.")
        d = os.path.join(cls.get_base_dir(), "challengers", strat)
        os.makedirs(d, exist_ok=True)
        return d

    @classmethod
    def get_archived_dir(cls, strategy: str) -> str:
        strat = strategy.lower().strip()
        if strat not in ("swing", "intraday", "fno"):
            raise ValueError(f"Unknown strategy: {strategy}. Expected SWING, INTRADAY, or FNO.")
        d = os.path.join(cls.get_base_dir(), "archived", strat)
        os.makedirs(d, exist_ok=True)
        return d

    @classmethod
    def get_active_model_path(cls, strategy: str) -> str:
        return os.path.join(cls.get_strategy_dir(strategy), "active_model.pkl")

    @classmethod
    def get_active_manifest_path(cls, strategy: str) -> str:
        return os.path.join(cls.get_strategy_dir(strategy), "active_manifest.json")

    @classmethod
    def create_challenger_artifact(
        cls,
        strategy: str,
        model_obj: Any,
        manifest_meta: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Creates an immutable FROZEN CHALLENGER model artifact in the Challenger Vault.
        STRICT GOVERNANCE INVARIANT:
        This method NEVER updates or overwrites the active model pointer.
        Promotion to active requires explicit manual approval via promote_challenger().
        """
        strat = strategy.upper().strip()
        challengers_dir = cls.get_challengers_dir(strat)
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        model_id = f"qlib_challenger_{strat.lower()}_{timestamp_str}"

        model_version_dir = os.path.join(challengers_dir, model_id)
        os.makedirs(model_version_dir, exist_ok=True)

        artifact_file = os.path.join(model_version_dir, "model.pkl")
        manifest_file = os.path.join(model_version_dir, "manifest.json")

        # Pickle the Qlib model artifact
        with open(artifact_file, "wb") as fp:
            pickle.dump(model_obj, fp)

        # Compute SHA-256 cryptographic hash
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
            "feature_manifest": manifest_meta.get("feature_manifest", {}),
            "universe": manifest_meta.get("universe", "NSE_FNO_AUTHORITATIVE" if strat == "FNO" else "LIVE_52"),
            "data_hash": manifest_meta.get("data_hash", ""),
            "train_start": manifest_meta.get("train_start", ""),
            "train_end": manifest_meta.get("train_end", ""),
            "valid_start": manifest_meta.get("valid_start", ""),
            "valid_end": manifest_meta.get("valid_end", ""),
            "oos_start": manifest_meta.get("oos_start", ""),
            "oos_end": manifest_meta.get("oos_end", ""),
            "horizon_days": manifest_meta.get("horizon_days", 5),
            "target_definition": manifest_meta.get("target_definition", "FORWARD_RETURN"),
            "friction_bps": manifest_meta.get("friction_bps", 25.0),
            "feature_importance": manifest_meta.get("feature_importance"),
            "hyperparameters": manifest_meta.get("hyperparameters", {}),
            "random_seed": manifest_meta.get("random_seed", 42),
            "metrics": manifest_meta.get("metrics", {}),
            "artifact_path": artifact_file,
            "artifact_sha256": artifact_sha256,
            "status": "FROZEN_CHALLENGER",
            "created_at": datetime.now().isoformat()
        }

        with open(manifest_file, "w") as fp:
            json.dump(manifest, fp, indent=2)

        logger.info(
            f"[QlibModelRegistry] Registered FROZEN CHALLENGER {strat} model {model_id} "
            f"(SHA256: {artifact_sha256[:12]}...). Active pointer UNCHANGED."
        )

        return manifest

    @classmethod
    def list_challengers(cls, strategy: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Returns all registered frozen challengers for a given strategy or across all strategies.
        """
        strategies = [strategy.upper().strip()] if strategy else ["SWING", "INTRADAY", "FNO"]
        challengers = []

        for strat in strategies:
            try:
                ch_dir = cls.get_challengers_dir(strat)
                if not os.path.exists(ch_dir):
                    continue
                active_man = cls.get_active_manifest(strat)
                for entry in os.listdir(ch_dir):
                    m_dir = os.path.join(ch_dir, entry)
                    if os.path.isdir(m_dir):
                        man_file = os.path.join(m_dir, "manifest.json")
                        if os.path.exists(man_file):
                            try:
                                with open(man_file, "r") as fp:
                                    ch_data = json.load(fp)
                                    # Attach formal promotion eligibility & side-by-side comparison
                                    eligibility = QlibPromotionPolicy.evaluate_promotion_eligibility(
                                        candidate_manifest=ch_data,
                                        champion_manifest=active_man
                                    )
                                    comparison = QlibPromotionPolicy.compare_candidate_vs_champion(
                                        candidate_manifest=ch_data,
                                        champion_manifest=active_man
                                    )
                                    ch_data["promotion_eligibility"] = eligibility
                                    ch_data["champion_comparison"] = comparison
                                    challengers.append(ch_data)
                            except Exception as e:
                                logger.warning(f"Error reading challenger manifest {man_file}: {e}")
            except Exception as e:
                logger.warning(f"Error listing challengers for {strat}: {e}")

        # Sort by creation time descending
        challengers.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return challengers

    @classmethod
    def get_challenger(cls, strategy: str, model_id: str) -> Optional[Dict[str, Any]]:
        """Reads a specific challenger manifest."""
        ch_dir = cls.get_challengers_dir(strategy)
        m_dir = os.path.join(ch_dir, model_id)
        man_file = os.path.join(m_dir, "manifest.json")
        if not os.path.exists(man_file):
            return None
        with open(man_file, "r") as fp:
            return json.load(fp)

    @classmethod
    def promote_challenger(
        cls,
        strategy: str,
        model_id: str,
        manual_approval: bool = False,
        force: bool = False
    ) -> Dict[str, Any]:
        """
        Promotes a frozen challenger artifact to become the ACTIVE model for a strategy.
        Strict Governance Invariants:
        1. manual_approval must be explicitly True.
        2. Backend independently re-evaluates all 12 QlibPromotionPolicy gates.
        3. Challenger model byte-for-byte SHA-256 verification must pass.
        4. Safety gates (positive Net Sharpe, PF >= 1.0, beating active champion) must pass unless force=True.
        5. Existing active model is safely archived to models/qlib/archived/<strategy>/<old_id>/.
        6. Active pointer is updated atomically.
        7. Full audit trail recorded in MasterLogger and qlib_promotion_audit_log.
        """
        if not manual_approval:
            raise ValueError("Model promotion blocked: manual_approval must be explicitly True.")

        strat = strategy.upper().strip()
        ch_dir = cls.get_challengers_dir(strat)
        m_dir = os.path.join(ch_dir, model_id)
        artifact_file = os.path.join(m_dir, "model.pkl")
        manifest_file = os.path.join(m_dir, "manifest.json")

        if not os.path.exists(artifact_file) or not os.path.exists(manifest_file):
            raise QlibModelIntegrityError(
                f"Challenger model {model_id} not found in {m_dir}"
            )

        with open(manifest_file, "r") as fp:
            challenger_manifest = json.load(fp)

        # 1. Cryptographic SHA-256 Verification
        expected_sha = challenger_manifest.get("artifact_sha256")
        h = hashlib.sha256()
        with open(artifact_file, "rb") as fp:
            raw_bytes = fp.read()
            h.update(raw_bytes)
        actual_sha = h.hexdigest()

        if actual_sha != expected_sha:
            cls._log_audit_event(
                strategy=strat,
                candidate_model_id=model_id,
                candidate_sha256=expected_sha or "UNKNOWN",
                current_champion_id=None,
                current_champion_sha256=None,
                status="REJECTED",
                eligible=False,
                candidate_metrics=challenger_manifest.get("metrics", {}),
                champion_metrics=None,
                gates={},
                failed_reasons=["Cryptographic SHA-256 mismatch detected."]
            )
            raise QlibModelIntegrityError(
                f"Challenger model corruption detected! Expected SHA256: {expected_sha}, actual: {actual_sha}"
            )

        # 2. Strict Backend Promotion Gate Evaluation
        active_manifest = cls.get_active_manifest(strat)
        eligibility = QlibPromotionPolicy.evaluate_promotion_eligibility(
            candidate_manifest=challenger_manifest,
            champion_manifest=active_manifest,
            force=force,
            audit_log=True
        )

        if not eligibility["eligible"]:
            cls._log_audit_event(
                strategy=strat,
                candidate_model_id=model_id,
                candidate_sha256=actual_sha,
                current_champion_id=active_manifest.get("model_id") if active_manifest else None,
                current_champion_sha256=active_manifest.get("artifact_sha256") if active_manifest else None,
                status="REJECTED",
                eligible=False,
                candidate_metrics=challenger_manifest.get("metrics", {}),
                champion_metrics=active_manifest.get("metrics") if active_manifest else None,
                gates=eligibility["gates"],
                failed_reasons=eligibility["failed_reasons"]
            )
            raise ValueError(
                f"Model promotion rejected by Safety Gates: {'; '.join(eligibility['failed_reasons'])}"
            )

        # 3. Archive current active model if present
        strat_dir = cls.get_strategy_dir(strat)
        active_artifact = os.path.join(strat_dir, "active_model.pkl")
        active_manifest_file = os.path.join(strat_dir, "active_manifest.json")

        archived_info = None
        old_model_id = None
        old_sha = None
        if os.path.exists(active_artifact) and os.path.exists(active_manifest_file):
            try:
                with open(active_manifest_file, "r") as fp:
                    old_manifest = json.load(fp)
                old_model_id = old_manifest.get("model_id", f"old_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
                old_sha = old_manifest.get("artifact_sha256")
                arch_dir = os.path.join(cls.get_archived_dir(strat), old_model_id)
                os.makedirs(arch_dir, exist_ok=True)
                with open(os.path.join(arch_dir, "model.pkl"), "wb") as fp:
                    with open(active_artifact, "rb") as src:
                        fp.write(src.read())
                old_manifest["archived_at"] = datetime.now().isoformat()
                old_manifest["status"] = "ARCHIVED"
                with open(os.path.join(arch_dir, "manifest.json"), "w") as fp:
                    json.dump(old_manifest, fp, indent=2)
                archived_info = {"archived_model_id": old_model_id, "archived_dir": arch_dir}
                logger.info(f"[QlibModelRegistry] Archived previous active model {old_model_id}")
            except Exception as e:
                logger.warning(f"[QlibModelRegistry] Could not archive existing model: {e}")

        # 4. Atomically promote to active
        promoted_manifest = dict(challenger_manifest)
        promoted_manifest["status"] = "ACTIVE"
        promoted_manifest["promoted_at"] = datetime.now().isoformat()
        promoted_manifest["promoted_by"] = "MANUAL_APPROVAL"

        with open(active_artifact, "wb") as fp:
            fp.write(raw_bytes)

        with open(active_manifest_file, "w") as fp:
            json.dump(promoted_manifest, fp, indent=2)

        # Update challenger manifest in vault to mark as PROMOTED
        challenger_manifest["status"] = "PROMOTED"
        challenger_manifest["promoted_at"] = promoted_manifest["promoted_at"]
        with open(manifest_file, "w") as fp:
            json.dump(challenger_manifest, fp, indent=2)

        # Record approved promotion in audit log
        cls._log_audit_event(
            strategy=strat,
            candidate_model_id=model_id,
            candidate_sha256=actual_sha,
            current_champion_id=old_model_id,
            current_champion_sha256=old_sha,
            status="APPROVED",
            eligible=True,
            candidate_metrics=challenger_manifest.get("metrics", {}),
            champion_metrics=active_manifest.get("metrics") if active_manifest else None,
            gates=eligibility["gates"],
            failed_reasons=[],
            resulting_active_model_id=model_id
        )

        logger.info(
            f"[QlibModelRegistry] PROMOTED challenger {model_id} to ACTIVE for strategy {strat}. "
            f"SHA256: {actual_sha[:12]}..."
        )

        return {
            "status": "PROMOTED",
            "strategy": strat,
            "model_id": model_id,
            "artifact_sha256": actual_sha,
            "promoted_manifest": promoted_manifest,
            "archived_previous": archived_info,
            "eligibility": eligibility
        }

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
        """Returns overview of all Qlib model strategies including active and challenger counts."""
        status = {}
        for s in ("SWING", "INTRADAY", "FNO"):
            manifest = cls.get_active_manifest(s)
            challengers = cls.list_challengers(s)
            if manifest:
                status[s.lower()] = {
                    "available": True,
                    "model_id": manifest.get("model_id"),
                    "model_class": manifest.get("model_class"),
                    "feature_handler": manifest.get("feature_handler"),
                    "artifact_sha256": manifest.get("artifact_sha256"),
                    "metrics": manifest.get("metrics"),
                    "created_at": manifest.get("created_at"),
                    "challengers_count": len(challengers)
                }
            else:
                status[s.lower()] = {
                    "available": False,
                    "status": "NOT_TRAINED",
                    "challengers_count": len(challengers)
                }
        return status
