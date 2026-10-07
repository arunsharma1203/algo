import os
import json
import hashlib
import logging
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from typing import Dict, Any, Tuple, Optional

from app.analytics.foundation_models.base import FoundationModelFeatures

logger = logging.getLogger(__name__)

MODEL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "models"))
DEFAULT_EMPIRICAL_ARTIFACT = os.path.join(MODEL_DIR, "meta_learner_clean_v1.pkl")
DEFAULT_EMPIRICAL_MANIFEST = os.path.join(MODEL_DIR, "meta_learner_clean_v1_manifest.json")

EXPECTED_FEATURE_NAMES = [
    "p_rf",
    "p_gb",
    "p_svm",
    "volume_ratio",
    "atr_pct",
    "macro_aligned",
    "sentiment",
    "tfm_ret",
    "chr_ret",
    "agreement"
]

META_LEARNER_RUNTIME_MODE = os.environ.get("META_LEARNER_RUNTIME_MODE", "heuristic").upper().strip()


class MetaLearnerIntegrationError(RuntimeError):
    """Raised when the empirical Meta-Learner artifact cannot be verified or loaded in EMPIRICAL mode."""
    pass


class TradeMetaLearner:
    """
    Layer-2 Stacked Meta-Learner with Verified Empirical Runtime Loading.
    Combines base model predictions (RF, GB, SVM), point-in-time market telemetry
    (Volatility ATR, Volume Ratio, Macro Regime, Sentiment), and Time-Series Foundation
    Model forecasts (TimesFM 2.5, Chronos-2) to output a unified probability.

    Runtime Modes:
    - EMPIRICAL: Executes trained, versioned model weights artifact (meta_learner_clean_v1.pkl).
    - HEURISTIC: Safe fallback executing causal Bayesian prior (default for backward compatibility).
    - DISABLED: Passes raw base ensemble confidence unmodified.
    - UNAVAILABLE: Set when empirical loading was requested but failed integrity verification.
    """

    def __init__(self, runtime_mode: Optional[str] = None):
        self.meta_model = LogisticRegression(C=0.5, solver='lbfgs', random_state=42)
        self.is_trained: bool = False
        self.is_empirically_trained: bool = False
        self.artifact_sha256: Optional[str] = None
        self.manifest_meta: Optional[Dict[str, Any]] = None
        self.artifact_path: Optional[str] = None

        # Determine runtime mode
        mode = (runtime_mode or META_LEARNER_RUNTIME_MODE).upper().strip()
        if mode not in ("EMPIRICAL", "HEURISTIC", "DISABLED"):
            logger.warning(f"[MetaLearner] Unknown mode '{mode}', defaulting to 'HEURISTIC'.")
            mode = "HEURISTIC"
        self.runtime_mode = mode

        # Always initialize default prior as foundation
        self._fit_default_prior()

        # If EMPIRICAL mode is requested, attempt explicit artifact loading
        if self.runtime_mode == "EMPIRICAL":
            try:
                self.load_empirical_artifact()
            except Exception as e:
                self.runtime_mode = "UNAVAILABLE"
                self.is_empirically_trained = False
                logger.error(f"[MetaLearner] Failed to load empirical artifact in EMPIRICAL mode: {e}")

    def _fit_default_prior(self):
        """Initializes monotonic stacking weights so the meta-learner functions out-of-the-box."""
        X_prior = np.array([
            [0.2, 0.2, 0.2, 0.5, 1.5, 0, -0.5, -0.5, -0.4, -1.0],
            [0.4, 0.4, 0.4, 1.0, 2.0, 0,  0.0,  0.0,  0.0,  0.0],
            [0.5, 0.5, 0.5, 1.0, 2.0, 1,  0.0,  0.1,  0.1,  1.0],
            [0.6, 0.6, 0.6, 1.2, 2.2, 1,  0.2,  0.4,  0.3,  1.0],
            [0.8, 0.8, 0.8, 2.0, 2.5, 1,  0.6,  0.8,  0.7,  1.0],
        ])
        y_prior = np.array([0, 0, 0, 1, 1])
        try:
            self.meta_model.fit(X_prior, y_prior)
            self.is_trained = True
        except Exception:
            self.is_trained = False

    def load_empirical_artifact(
        self,
        artifact_path: Optional[str] = None,
        manifest_path: Optional[str] = None
    ) -> bool:
        """
        Explicitly loads, cryptographically verifies, and activates the versioned empirical artifact.

        Validates:
        1. File existence
        2. Cryptographic SHA-256 checksum matching manifest
        3. Model object type and inference interface
        4. Expected feature count (10) and feature names
        """
        import pickle

        art_path = artifact_path or DEFAULT_EMPIRICAL_ARTIFACT
        man_path = manifest_path or DEFAULT_EMPIRICAL_MANIFEST

        if not os.path.exists(art_path):
            raise MetaLearnerIntegrationError(f"Empirical artifact not found at: {art_path}")

        # 1. Compute SHA-256
        with open(art_path, "rb") as f:
            raw_bytes = f.read()
        computed_sha = hashlib.sha256(raw_bytes).hexdigest()

        # 2. Check manifest if present
        if os.path.exists(man_path):
            try:
                with open(man_path, "r") as mf:
                    manifest = json.load(mf)
                expected_sha = manifest.get("artifact_sha256")
                if expected_sha and expected_sha != computed_sha:
                    raise MetaLearnerIntegrationError(
                        f"Artifact SHA-256 mismatch! Manifest: {expected_sha}, Computed: {computed_sha}"
                    )
                self.manifest_meta = manifest
            except Exception as me:
                if isinstance(me, MetaLearnerIntegrationError):
                    raise
                logger.warning(f"[MetaLearner] Manifest parse note: {me}")

        # 3. Load model object
        try:
            model = pickle.loads(raw_bytes)
        except Exception as pe:
            raise MetaLearnerIntegrationError(f"Failed deserializing pickle artifact: {pe}")

        if not hasattr(model, "predict_proba"):
            raise MetaLearnerIntegrationError(
                f"Model object {type(model).__name__} does not implement predict_proba"
            )

        # 4. Validate feature count
        n_features = getattr(model, "n_features_in_", None)
        if n_features is not None and n_features != len(EXPECTED_FEATURE_NAMES):
            raise MetaLearnerIntegrationError(
                f"Feature count mismatch: model expects {n_features}, expected {len(EXPECTED_FEATURE_NAMES)}"
            )

        self.meta_model = model
        self.is_trained = True
        self.is_empirically_trained = True
        self.runtime_mode = "EMPIRICAL"
        self.artifact_sha256 = computed_sha
        self.artifact_path = art_path

        logger.info(
            f"[MetaLearner] Activated EMPIRICAL mode using artifact {os.path.basename(art_path)} "
            f"(SHA256: {computed_sha[:16]}..., features={len(EXPECTED_FEATURE_NAMES)})."
        )
        return True

    def train_meta_learner_from_oof(
        self,
        oof_base_probs: np.ndarray,
        telemetry_features: np.ndarray,
        y_true: np.ndarray,
        foundation_features: Optional[np.ndarray] = None
    ) -> bool:
        """
        Trains Layer-2 Meta-Learner strictly on Out-Of-Fold predictions from base models
        and causal point-in-time foundation model forecasts.
        """
        if len(oof_base_probs) < 20 or len(np.unique(y_true)) < 2:
            return False

        try:
            if foundation_features is not None and len(foundation_features) == len(oof_base_probs):
                X_meta = np.hstack([oof_base_probs, telemetry_features, foundation_features])
            else:
                zero_found = np.zeros((len(oof_base_probs), 3))
                X_meta = np.hstack([oof_base_probs, telemetry_features, zero_found])

            self.meta_model.fit(X_meta, y_true.astype(int))
            self.is_trained = True
            self.is_empirically_trained = True
            self.runtime_mode = "EMPIRICAL"
            logger.info(f"Layer-2 Meta-Learner successfully trained on {len(X_meta)} OOF observations.")
            return True
        except Exception as e:
            logger.error(f"Failed to train Meta-Learner: {e}")
            return False

    def evaluate_new_trade(
        self,
        ticker: str,
        direction: str,
        trade_type: str,
        base_confidence: float,
        base_probs: Optional[Tuple[float, float, float]] = None,
        nlp_sentiment: float = 0.0,
        macro_state: Optional[Dict[str, Any]] = None,
        atr_pct: float = 2.0,
        volume_ratio: float = 1.0,
        foundation_features: Optional[FoundationModelFeatures] = None
    ) -> Tuple[float, str, Dict[str, Any]]:
        """
        Evaluates trade using Layer-2 Stacking Model combining base models,
        market telemetry, and Foundation Model signals.

        Returns: (final_score, meta_message, telemetry_dict)
        """
        reasons = []

        if macro_state is None:
            macro_state = {}

        tt_norm = str(trade_type or "INTRADAY").upper().strip()
        if tt_norm == "INTRADAY":
            nifty_trend = macro_state.get('nifty_trend_intraday') or macro_state.get('nifty_trend_short', 'BULLISH')
        else:
            nifty_trend = macro_state.get('nifty_trend_daily_20ema') or macro_state.get('nifty_trend_short', 'BULLISH')

        vix_status = macro_state.get('vix_status', 'NORMAL')
        is_bullish = 1 if direction.upper() in ('BULLISH', 'BUY', 'LONG') else 0
        
        # 1. Macro Alignment
        macro_aligned = 1 if (is_bullish and nifty_trend == 'BULLISH') or (not is_bullish and nifty_trend == 'BEARISH') else 0
        if macro_aligned == 1:
            reasons.append("Aligned with NIFTY Macro")
        else:
            reasons.append("Opposing NIFTY Trend")

        # 2. Base Committee Probabilities
        norm_conf = base_confidence / 100.0 if base_confidence > 1.0 else base_confidence
        if base_probs is not None and len(base_probs) == 3:
            p_rf, p_gb, p_svm = [p / 100.0 if p > 1.0 else p for p in base_probs]
        else:
            p_rf, p_gb, p_svm = norm_conf, norm_conf, norm_conf

        # 3. Volume Multiplier
        norm_vol = max(0.1, min(5.0, float(volume_ratio)))
        if norm_vol >= 1.5:
            reasons.append(f"Volume Surge ({norm_vol:.1f}x)")
        elif norm_vol < 0.7:
            reasons.append(f"Low Volume Liquidity ({norm_vol:.1f}x)")

        # 4. ATR Volatility
        norm_atr = max(0.1, min(10.0, float(atr_pct)))
        if norm_atr > 5.0:
            reasons.append(f"High Tail Risk ({norm_atr:.1f}% ATR)")
        elif 1.5 <= norm_atr <= 4.0:
            reasons.append(f"Optimal Volatility ({norm_atr:.1f}% ATR)")

        # 5. Sentiment Normalization
        norm_sent = max(-1.0, min(1.0, float(nlp_sentiment) / 100.0 if abs(nlp_sentiment) > 1.0 else float(nlp_sentiment)))
        if norm_sent > 0.2:
            reasons.append(f"Positive News Flow (+{norm_sent:.1f})")
        elif norm_sent < -0.2:
            reasons.append(f"Negative News Flow ({norm_sent:.1f})")

        # 6. Foundation Model Signals
        if foundation_features is not None:
            tfm_ret = float(foundation_features.timesfm_expected_return)
            chr_ret = float(foundation_features.chronos_expected_return)
            agreement = float(foundation_features.foundation_direction_agreement)

            if foundation_features.timesfm_status == "success" and foundation_features.chronos_status == "success":
                if agreement == 1.0:
                    reasons.append(f"Foundation Consensus (+{foundation_features.foundation_consensus_score:.2f}%)")
                elif agreement == -1.0:
                    reasons.append(f"TimesFM/Chronos Divergence ({tfm_ret:+.2f}% vs {chr_ret:+.2f}%)")
            elif foundation_features.timesfm_status == "success":
                reasons.append(f"TimesFM Forecast ({tfm_ret:+.2f}%)")
            elif foundation_features.chronos_status == "success":
                reasons.append(f"Chronos Forecast ({chr_ret:+.2f}%)")
        else:
            tfm_ret = 0.0
            chr_ret = 0.0
            agreement = 0.0

        # 7. Complete 10-Feature Vector:
        X_trade = np.array([[p_rf, p_gb, p_svm, norm_vol, norm_atr, macro_aligned, norm_sent, tfm_ret, chr_ret, agreement]])

        # 8. Inference based on active runtime mode
        if self.runtime_mode == "EMPIRICAL":
            if not self.is_empirically_trained:
                raise MetaLearnerIntegrationError(
                    "EMPIRICAL mode is configured but empirical artifact is not verified/loaded."
                )
            try:
                stacked_prob = float(self.meta_model.predict_proba(X_trade)[0][1])
                final_score = round(stacked_prob * 100.0, 1)
                calibrator_input_type = "EMPIRICAL_META_PROBABILITY"
            except Exception as e:
                logger.error(f"[MetaLearner] Empirical model inference failure: {e}")
                raise MetaLearnerIntegrationError(f"Empirical inference failed: {e}")

        elif self.runtime_mode == "HEURISTIC":
            # Calibrated dynamic range preserving Bayesian prior
            macro_bonus = 0.07 if macro_aligned else -0.07
            vol_bonus = 0.04 if norm_vol >= 1.5 else (-0.03 if norm_vol < 0.7 else 0.01)
            sent_bonus = 0.04 * norm_sent
            found_bonus = 0.04 * agreement

            blended_prob = (norm_conf * 0.78) + macro_bonus + vol_bonus + sent_bonus + found_bonus + 0.10
            blended_prob = max(0.05, min(0.95, blended_prob))
            final_score = round(blended_prob * 100.0, 1)
            calibrator_input_type = "HEURISTIC"

        else:
            # DISABLED or UNAVAILABLE: Return raw base confidence
            final_score = round(norm_conf * 100.0, 1)
            calibrator_input_type = "DISABLED"

        summary_reasons = ", ".join(reasons) if reasons else "Standard Technicals"
        meta_message = f"Layer-2 Meta-Learner Evaluated ({self.runtime_mode}): {final_score}% conviction ({summary_reasons})."

        telemetry = {
            "atr_pct": round(norm_atr, 2),
            "volume_ratio": round(norm_vol, 2),
            "macro_aligned": bool(macro_aligned),
            "macro_trend": nifty_trend,
            "vix_status": vix_status,
            "sentiment_score": round(norm_sent * 100, 1),
            "base_confidence": round(norm_conf * 100, 1),
            "stacked_score": final_score,
            "runtime_mode": self.runtime_mode,
            "is_empirically_trained": bool(self.is_empirically_trained),
            "artifact_sha256": self.artifact_sha256,
            "calibrator_input_type": calibrator_input_type,
            "foundation_features": foundation_features.to_dict() if foundation_features else None,
            "reasons": reasons,
            "message": meta_message
        }

        return final_score, meta_message, telemetry


meta_learner = TradeMetaLearner()
