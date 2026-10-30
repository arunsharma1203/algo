"""
CONSENSUS META-LEARNER V2: CORE ARTIFACT & INFERENCE ENGINE
===========================================================
Secondary Layer-2 supervised learning architecture implementing De Prado Meta-Labeling.

Mathematical Framework:
- Base Probability: P_base in (0, 1) derived from Qlib + Legacy consensus fusion.
- Base Log-Odds: logit(P_base) = ln(P_base / (1 - P_base))
- Learned Contextual Contribution: Delta = w_0 + w^T * X_context
- Meta Log-Odds: logit(P_meta) = logit(P_base) + Delta
- Final Calibrated Probability: P_final = Calibrator.predict_proba(sigma(logit(P_meta)))
"""

import os
import json
import hashlib
import logging
import math
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.isotonic import IsotonicRegression

from app.analytics.consensus_meta_feature_extractor import (
    ConsensusMetaFeatureExtractor,
    FEATURE_NAMES,
    GROUP_SLICES
)

logger = logging.getLogger(__name__)

MODEL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "models"))
DEFAULT_V2_ARTIFACT = os.path.join(MODEL_DIR, "consensus_meta_learner_v2.pkl")
DEFAULT_V2_MANIFEST = os.path.join(MODEL_DIR, "consensus_meta_learner_v2_manifest.json")


def logit(p: float, eps: float = 1e-4) -> float:
    p_clamped = max(eps, min(1.0 - eps, float(p)))
    return float(np.log(p_clamped / (1.0 - p_clamped)))


def sigmoid(z: float) -> float:
    if z >= 35.0:
        return 1.0
    elif z <= -35.0:
        return 0.0
    return float(1.0 / (1.0 + np.exp(-z)))


class ConsensusMetaLearnerV2:
    def __init__(
        self,
        artifact_path: Optional[str] = None,
        manifest_path: Optional[str] = None,
        auto_load: bool = True
    ):
        self.artifact_path = artifact_path or DEFAULT_V2_ARTIFACT
        self.manifest_path = manifest_path or DEFAULT_V2_MANIFEST

        self.model: Optional[LogisticRegression] = None
        self.calibrator: Optional[IsotonicRegression] = None
        self.is_trained: bool = False
        self.feature_names: List[str] = list(FEATURE_NAMES)
        self.group_slices: Dict[str, List[str]] = dict(GROUP_SLICES)
        self.manifest_meta: Dict[str, Any] = {}

        if auto_load and os.path.exists(self.artifact_path) and os.path.exists(self.manifest_path):
            self.load_model(self.artifact_path, self.manifest_path)

    def load_model(self, artifact_path: str, manifest_path: str) -> bool:
        try:
            import pickle
            if not os.path.exists(artifact_path) or not os.path.exists(manifest_path):
                logger.warning(f"[MetaLearnerV2] Artifact or manifest missing at {artifact_path}")
                return False

            with open(manifest_path, "r") as mf:
                manifest = json.load(mf)

            with open(artifact_path, "rb") as af:
                raw_bytes = af.read()
                computed_sha = hashlib.sha256(raw_bytes).hexdigest()
                expected_sha = manifest.get("artifact_sha256")
                if expected_sha and computed_sha != expected_sha:
                    logger.error(f"[MetaLearnerV2] SHA256 mismatch! Expected {expected_sha}, got {computed_sha}")
                    return False
                payload = pickle.loads(raw_bytes)

            self.model = payload["model"]
            self.calibrator = payload.get("calibrator")
            self.feature_names = payload.get("feature_names", FEATURE_NAMES)
            self.manifest_meta = manifest
            self.is_trained = True
            logger.info(f"[MetaLearnerV2] Successfully loaded model from {artifact_path} (SHA: {computed_sha[:12]})")
            return True
        except Exception as e:
            logger.error(f"[MetaLearnerV2] Error loading artifact: {e}")
            self.is_trained = False
            return False

    def save_model(
        self,
        artifact_path: str,
        manifest_path: str,
        metrics_summary: Optional[Dict[str, Any]] = None
    ) -> bool:
        try:
            import pickle
            os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
            os.makedirs(os.path.dirname(manifest_path), exist_ok=True)

            payload = {
                "model": self.model,
                "calibrator": self.calibrator,
                "feature_names": self.feature_names,
                "group_slices": self.group_slices
            }
            raw_bytes = pickle.dumps(payload, protocol=4)
            sha256_hash = hashlib.sha256(raw_bytes).hexdigest()

            with open(artifact_path, "wb") as af:
                af.write(raw_bytes)

            manifest = {
                "version": "consensus_meta_learner_v2",
                "artifact_file": os.path.basename(artifact_path),
                "artifact_sha256": sha256_hash,
                "feature_count": len(self.feature_names),
                "feature_names": self.feature_names,
                "metrics": metrics_summary or {},
                "created_at": str(np.datetime64("now"))
            }

            with open(manifest_path, "w") as mf:
                json.dump(manifest, mf, indent=2)

            self.manifest_meta = manifest
            self.is_trained = True
            logger.info(f"[MetaLearnerV2] Successfully saved artifact {artifact_path} (SHA: {sha256_hash[:12]})")
            return True
        except Exception as e:
            logger.error(f"[MetaLearnerV2] Error saving artifact: {e}")
            return False

    def predict_candidate(
        self,
        candidate: Dict[str, Any],
        db_conn: Optional[Any] = None
    ) -> Dict[str, Any]:
        base_conviction = float(candidate.get("composite_score") or 50.0)
        p_base = max(0.01, min(0.99, base_conviction / 100.0))
        base_logit = logit(p_base)

        if not self.is_trained or self.model is None:
            return {
                "base_conviction": round(base_conviction, 1),
                "meta_adjustment": 0.0,
                "final_conviction": round(base_conviction, 1),
                "raw_meta_probability": round(p_base, 4),
                "calibrated_probability": round(p_base, 4),
                "group_attributions": {
                    "MARKET_CONTEXT": 0.0,
                    "SECTOR_CONTEXT": 0.0,
                    "RELATIVE_STRENGTH": 0.0,
                    "FNO_CONTEXT": 0.0,
                    "NEWS_CONTEXT": 0.0,
                    "CHART_STRUCTURE": 0.0
                },
                "status": "FALLBACK_BASE_ONLY"
            }

        feats_dict = ConsensusMetaFeatureExtractor.extract_point_in_time_features(
            candidate, db_conn=db_conn
        )
        x_vec = np.array([feats_dict.get(k, 0.0) for k in self.feature_names], dtype=np.float64)

        coefs = self.model.coef_[0]
        intercept = float(self.model.intercept_[0])

        feat_contributions = coefs * x_vec
        total_context_logit = float(intercept + np.sum(feat_contributions))

        meta_logit = base_logit + total_context_logit
        p_meta_raw = sigmoid(meta_logit)

        if self.calibrator is not None:
            try:
                p_calibrated = float(self.calibrator.predict([p_meta_raw])[0])
                p_calibrated = max(0.01, min(0.99, p_calibrated))
            except Exception:
                p_calibrated = p_meta_raw
        else:
            p_calibrated = p_meta_raw

        final_conviction = round(p_calibrated * 100.0, 1)
        meta_adj = round(final_conviction - base_conviction, 1)

        group_attributions: Dict[str, float] = {}
        total_abs_contr = sum(abs(feat_contributions[self.feature_names.index(f)]) 
                              for f in self.feature_names if f in feats_dict) + 1e-9

        for grp, feat_list in self.group_slices.items():
            if grp == "BASE_QLIB_LEGACY":
                continue
            grp_logit = sum(feat_contributions[self.feature_names.index(f)] 
                            for f in feat_list if f in self.feature_names)
            share = grp_logit / total_abs_contr
            group_attributions[grp] = round(meta_adj * share, 1)

        return {
            "base_conviction": round(base_conviction, 1),
            "meta_adjustment": meta_adj,
            "final_conviction": final_conviction,
            "raw_meta_probability": round(p_meta_raw, 4),
            "calibrated_probability": round(p_calibrated, 4),
            "group_attributions": group_attributions,
            "features_telemetry": feats_dict,
            "status": "EVALUATED_V2"
        }
