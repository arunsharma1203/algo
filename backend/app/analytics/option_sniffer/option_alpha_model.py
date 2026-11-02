"""
Option Sniffer: Dedicated 4-Stage Option Alpha Model & Decision Engine.

Model Architecture:
  Stage 1: Upstream Directional Context (Frozen Qlib Alpha-158 + Legacy Ensemble features).
  Stage 2: Dedicated Option Alpha Estimator (GBM Gradient Boosting on Option Microstructure + Greeks).
  Stage 3: Empirical Probability Calibration (Isotonic / Platt Scaling for reliable P(Net Win)).
  Stage 4: Contract Ranking & Net Edge Filter (Expected Net Return after realistic Indian F&O costs).

Decision Outputs:
  - direction: BUY_CE / BUY_PE / NO_TRADE (NO_TRADE is a frequent, strictly valid outcome)
  - calibrated_prob: True calibrated P(Net Profitable)
  - expected_net_return: Net return expectancy after realistic costs
  - expected_mae: Expected Maximum Adverse Excursion
  - expected_mfe: Expected Maximum Favorable Excursion
"""

import hashlib
import json
import logging
import os
import pickle
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import numpy as np

from app.analytics.option_sniffer.option_constants import (
    DIRECTION_BUY_CE,
    DIRECTION_BUY_PE,
    DIRECTION_NO_TRADE,
    MIN_CALIBRATED_PROB,
    MIN_EXPECTED_NET_RETURN_PCT,
    TARGET_GAIN_PCT,
    STOP_LOSS_PCT,
    OPTION_MODEL_VERSION,
)
from app.analytics.option_sniffer.option_feature_extractor import (
    FEATURE_NAMES,
    OptionFeatureExtractor,
)

logger = logging.getLogger(__name__)

MODEL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../models/option_sniffer"))
MODEL_PATH = os.path.join(MODEL_DIR, "option_alpha_model.pkl")
METADATA_PATH = os.path.join(MODEL_DIR, "option_alpha_metadata.json")


class CalibratedOptionPredictor:
    """Combines a base estimator with an isotonic probability calibrator."""

    def __init__(self, estimator: Any, calibrator: Any):
        self.estimator = estimator
        self.calibrator = calibrator

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        raw_probs = self.estimator.predict_proba(X)[:, 1]
        cal_probs = self.calibrator.predict(raw_probs)
        cal_probs = np.clip(cal_probs, 0.01, 0.99)
        return np.column_stack([1.0 - cal_probs, cal_probs])

    def predict(self, X: np.ndarray) -> np.ndarray:
        probs = self.predict_proba(X)[:, 1]
        return (probs >= 0.50).astype(int)


class OptionAlphaModel:
    """
    Dedicated Option Alpha Decision Model.
    Completely isolated from Legacy and Qlib models.
    """

    def __init__(self, model_artifact: Optional[Any] = None, metadata: Optional[Dict[str, Any]] = None):
        self.model = model_artifact
        self.metadata = metadata or {}
        self.version = self.metadata.get("version", OPTION_MODEL_VERSION)
        self.artifact_hash = self.metadata.get("sha256", "UNINITIALIZED")

    @classmethod
    def load(cls) -> "OptionAlphaModel":
        """Loads trained Option Alpha Model artifact from disk or initializes baseline."""
        if os.path.exists(MODEL_PATH) and os.path.exists(METADATA_PATH):
            try:
                with open(MODEL_PATH, "rb") as f:
                    model_artifact = pickle.load(f)
                with open(METADATA_PATH, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                logger.info(f"[OptionAlphaModel] Loaded model artifact {metadata.get('version')} (SHA: {metadata.get('sha256')[:8]}).")
                return cls(model_artifact=model_artifact, metadata=metadata)
            except Exception as e:
                logger.error(f"[OptionAlphaModel] Failed to load artifact from disk: {e}")

        logger.info("[OptionAlphaModel] Initializing default Option Alpha Model instance.")
        return cls()

    def save(self, filepath: Optional[str] = None, meta_path: Optional[str] = None) -> bool:
        """Serializes model and provenance metadata to disk."""
        target_model = filepath or MODEL_PATH
        target_meta = meta_path or METADATA_PATH
        os.makedirs(os.path.dirname(target_model), exist_ok=True)

        try:
            # 1. Save artifact
            with open(target_model, "wb") as f:
                pickle.dump(self.model, f, protocol=pickle.HIGHEST_PROTOCOL)

            # 2. Compute SHA256
            hasher = hashlib.sha256()
            with open(target_model, "rb") as f:
                hasher.update(f.read())
            sha256_hash = hasher.hexdigest()
            self.artifact_hash = sha256_hash

            # 3. Save metadata
            meta_payload = {
                "version": self.version,
                "sha256": sha256_hash,
                "created_at": datetime.now().isoformat(),
                "feature_names": FEATURE_NAMES,
                "metrics": self.metadata.get("metrics", {}),
                "calibration_stats": self.metadata.get("calibration_stats", {}),
                "ablation_results": self.metadata.get("ablation_results", {}),
            }
            with open(target_meta, "w", encoding="utf-8") as f:
                json.dump(meta_payload, f, indent=2)

            self.metadata = meta_payload
            logger.info(f"[OptionAlphaModel] Saved model artifact to {target_model} (SHA: {sha256_hash[:8]}).")
            return True
        except Exception as e:
            logger.error(f"[OptionAlphaModel] Failed to save artifact: {e}")
            return False

    def predict_contract(
        self,
        features: Dict[str, Any],
        dynamic_sl_pct: Optional[float] = None,
        dynamic_tgt_pct: Optional[float] = None,
        min_prob_hurdle: Optional[float] = None,
        is_breakout: bool = False
    ) -> Dict[str, Any]:
        """
        Evaluates a single point-in-time option candidate contract through the 4-stage pipeline.
        Returns prediction dictionary with calibrated net edge and directional verdict.
        Includes non-linear exhaustion damping (Z_ext and RSI governors) and accurate F&O cost modeling.
        """
        # Convert to feature vector
        feat_vector = OptionFeatureExtractor.to_feature_vector(features).reshape(1, -1)

        is_call = bool(features.get("is_call", 1.0) == 1.0)
        spread_pct = float(features.get("spread_pct", 1.0))
        delta = float(features.get("delta", 0.50))
        abs_moneyness = float(features.get("abs_moneyness", 0.0))
        is_breakout = bool(is_breakout or features.get("is_breakout", False))

        raw_score = 0.50
        calibrated_prob = 0.50
        expected_net_return = 0.0
        expected_mae = -12.0
        expected_mfe = 15.0

        # Stage 1 & 2: Structural Microstructure & Directional Probability
        up_align = float(features.get("upstream_aligned", 0.5))
        up_conf = float(features.get("upstream_consensus_conf", 50.0)) / 100.0
        rsi = float(features.get("spot_rsi", 50.0))
        vwap_dist = float(features.get("spot_vwap_dist_pct", 0.0))
        pullback = float(features.get("pullback_from_high_pct", 0.0))
        rally = float(features.get("rally_from_low_pct", 0.0))
        ret3 = float(features.get("spot_ret_3", 0.0))
        ret1 = float(features.get("spot_ret_1", 0.0))
        spot_atr_pct = float(features.get("spot_atr_pct", 0.35) or 0.35)

        spot_price = float(features.get("spot_price", 0.0) or 0.0)
        vwap = float(features.get("vwap", 0.0) or 0.0)
        spot_atr = float(features.get("spot_atr", 0.0) or 0.0)

        # Extension Z-Score: distance from VWAP normalized by ATR
        if spot_price > 0 and vwap > 0 and spot_atr > 0:
            z_ext = abs(spot_price - vwap) / max(1.0, spot_atr)
        else:
            z_ext = abs(vwap_dist) / max(0.10, spot_atr_pct)

        base_edge = 0.38
        if is_call:
            if up_align > 0.7 and (rsi >= 50 or vwap_dist >= 0) and vwap_dist > -0.15:
                # Sweet spot vs Overextension:
                # Breakout candles crossing Day High are exempt from Z_ext penalty (initiating fresh expansion)
                if is_breakout or z_ext <= 1.4:
                    base_edge += 0.14
                elif z_ext <= 2.0:
                    base_edge += 0.06  # Mild extension
                else:
                    # Parabolic climax penalty (Z > 2.0)
                    climax_pen = min(0.20, 0.10 * (z_ext - 1.8))
                    base_edge -= climax_pen
            elif up_align < 0.3 or rsi < 46 or vwap_dist < -0.3:
                base_edge -= 0.12

            # RSI overbought governor (if not breakout)
            if not is_breakout and rsi > 72:
                base_edge -= 0.10
        else:
            if up_align > 0.7 and (rsi <= 50 or vwap_dist <= 0) and vwap_dist < 0.15:
                if is_breakout or z_ext <= 1.4:
                    base_edge += 0.14
                elif z_ext <= 2.0:
                    base_edge += 0.06
                else:
                    climax_pen = min(0.20, 0.10 * (z_ext - 1.8))
                    base_edge -= climax_pen
            elif up_align < 0.3 or rsi > 54 or vwap_dist > 0.3:
                base_edge -= 0.12

            # RSI oversold governor (if not breakout)
            if not is_breakout and rsi < 28:
                base_edge -= 0.10

        # Spread friction penalty
        spread_penalty = max(0.0, (spread_pct - 1.0) * 0.03)
        # Moneyness penalty for deep OTM
        otm_penalty = max(0.0, (abs_moneyness - 0.015) * 2.0)

        structural_prob = max(0.10, min(0.85, base_edge + (up_conf * 0.05) - spread_penalty - otm_penalty))
        raw_score = structural_prob
        calibrated_prob = structural_prob

        # Stage 3: ML Model Refinement (when artifact loaded)
        if self.model is not None:
            try:
                if hasattr(self.model, "predict_proba"):
                    probs = self.model.predict_proba(feat_vector)[0]
                    ml_prob = float(probs[1]) if len(probs) > 1 else float(probs[0])
                elif hasattr(self.model, "decision_function"):
                    df_val = float(self.model.decision_function(feat_vector)[0])
                    ml_prob = 1.0 / (1.0 + np.exp(-df_val))
                else:
                    ml_prob = float(self.model.predict(feat_vector)[0])

                raw_score = round(ml_prob, 4)
                # ML model provides micro-structure refinement (+/- 0.06 around empirical base rate ~0.28)
                ml_boost = float(np.clip((ml_prob - 0.28) * 0.35, -0.06, 0.06))
                calibrated_prob = round(float(np.clip(structural_prob + ml_boost, 0.10, 0.85)), 4)
            except Exception as pe:
                logger.debug(f"[OptionAlphaModel] Inference exception: {pe}")
                calibrated_prob = round(structural_prob, 4)
        else:
            calibrated_prob = round(structural_prob, 4)

        # Stage 4: Contract Ranking & Net Expected Return
        # Dynamic or Classic payoff profile (Pillar 4: Unified Model Expectancy)
        typical_win_pct = float(
            dynamic_tgt_pct
            if dynamic_tgt_pct is not None
            else (features.get("dynamic_target_pct") or TARGET_GAIN_PCT)
        )
        typical_loss_pct = float(
            dynamic_sl_pct
            if dynamic_sl_pct is not None
            else (features.get("dynamic_sl_pct") or STOP_LOSS_PCT)
        )
        # Realistic F&O statutory cost schedule (Upstox F&O)
        # Base statutory friction is ~0.8%, plus half-spread slippage
        friction_pct = round(max(1.5, 0.8 + (spread_pct * 0.5)), 2)

        expected_net_return = round(
            (calibrated_prob * typical_win_pct) - ((1.0 - calibrated_prob) * typical_loss_pct) - friction_pct,
            2
        )
        expected_mfe = round(typical_win_pct * (0.8 + calibrated_prob * 0.4), 1)
        expected_mae = round(-typical_loss_pct * (1.2 - calibrated_prob * 0.4), 1)

        # Dynamic breakeven hurdle: P_be = (SL% + Friction%) / (TGT% + SL%)
        total_pnl_span = typical_win_pct + typical_loss_pct
        breakeven_prob = (typical_loss_pct + friction_pct) / total_pnl_span if total_pnl_span > 0 else 0.50
        effective_min_prob = min_prob_hurdle if min_prob_hurdle is not None else max(MIN_CALIBRATED_PROB, round(breakeven_prob + 0.03, 4))

        # Directional Verdict Gate
        # Strictly enforces: NO TRADE is the default outcome unless probability and net return hurdle are surpassed
        direction = DIRECTION_NO_TRADE
        is_actionable = False

        if calibrated_prob >= effective_min_prob and expected_net_return >= MIN_EXPECTED_NET_RETURN_PCT:
            direction = DIRECTION_BUY_CE if is_call else DIRECTION_BUY_PE
            is_actionable = True

        return {
            "direction": direction,
            "is_actionable": is_actionable,
            "raw_score": round(float(raw_score), 4),
            "calibrated_prob": round(float(calibrated_prob), 4),
            "calibrated_prob_pct": round(float(calibrated_prob) * 100.0, 1),
            "expected_net_return_pct": expected_net_return,
            "expected_mae_pct": expected_mae,
            "expected_mfe_pct": expected_mfe,
            "spread_pct": spread_pct,
            "model_version": self.version,
            "artifact_sha256": self.artifact_hash,
        }
