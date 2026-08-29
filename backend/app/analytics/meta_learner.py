import os
import json
import logging
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from typing import Dict, Any, Tuple, Optional

from app.analytics.foundation_models.base import FoundationModelFeatures

logger = logging.getLogger(__name__)

class TradeMetaLearner:
    """
    Genuine Layer-2 Stacked Meta-Learner.
    Combines base model predictions (RF, GB, SVM), point-in-time market telemetry
    (Volatility ATR, Volume Ratio, Macro Regime, Sentiment), and Time-Series Foundation
    Model forecasts (TimesFM 2.5, Chronos-2) to output a unified probability.
    """

    def __init__(self):
        self.meta_model = LogisticRegression(C=0.5, solver='lbfgs', random_state=42)
        self.is_trained = False
        self._fit_default_prior()

    def _fit_default_prior(self):
        """Initializes monotonic stacking weights so the meta-learner functions out-of-the-box."""
        # Features: [p_rf, p_gb, p_svm, volume_ratio, atr_pct, macro_aligned, sentiment, tfm_ret, chr_ret, agreement]
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
                # Add default zeros for foundation features
                zero_found = np.zeros((len(oof_base_probs), 3))
                X_meta = np.hstack([oof_base_probs, telemetry_features, zero_found])

            self.meta_model.fit(X_meta, y_true.astype(int))
            self.is_trained = True
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
        base_probs: Optional[Tuple[float, float, float]] = None, # (prob_rf, prob_gb, prob_svm)
        nlp_sentiment: float = 0.0,
        macro_state: Optional[Dict[str, Any]] = None,
        atr_pct: float = 2.0,
        volume_ratio: float = 1.0,
        foundation_features: Optional[FoundationModelFeatures] = None
    ) -> Tuple[float, str, Dict[str, Any]]:
        """
        Evaluates trade using Layer-2 Stacking Model combining base models,
        market telemetry, and Foundation Model Challenger signals.
        
        Returns: (final_stacked_confidence, meta_message, telemetry_dict)
        """
        reasons = []

        if macro_state is None:
            macro_state = {}

        nifty_trend = macro_state.get('nifty_trend_short', 'BULLISH')
        vix_status = macro_state.get('vix_status', 'NORMAL')
        is_bullish = 1 if direction.upper() == 'BULLISH' else 0
        
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

        # 7. Construct Complete Meta-Feature Vector:
        # [p_rf, p_gb, p_svm, volume_ratio, atr_pct, macro_aligned, sentiment, tfm_ret, chr_ret, agreement]
        X_trade = np.array([[p_rf, p_gb, p_svm, norm_vol, norm_atr, macro_aligned, norm_sent, tfm_ret, chr_ret, agreement]])

        if self.is_trained:
            try:
                stacked_prob = float(self.meta_model.predict_proba(X_trade)[0][1])
                final_score = round(stacked_prob * 100.0, 1)
            except Exception as e:
                logger.warning(f"Meta-model inference note: {e}")
                final_score = round(norm_conf * 100.0, 1)
        else:
            final_score = round(norm_conf * 100.0, 1)

        summary_reasons = ", ".join(reasons) if reasons else "Standard Technicals"
        meta_message = f"Layer-2 Meta-Learner Evaluated: {final_score}% conviction ({summary_reasons})."

        telemetry = {
            "atr_pct": round(norm_atr, 2),
            "volume_ratio": round(norm_vol, 2),
            "macro_aligned": bool(macro_aligned),
            "macro_trend": nifty_trend,
            "vix_status": vix_status,
            "sentiment_score": round(norm_sent * 100, 1),
            "base_confidence": round(norm_conf * 100, 1),
            "stacked_score": final_score,
            "foundation_features": foundation_features.to_dict() if foundation_features else None,
            "reasons": reasons,
            "message": meta_message
        }

        return final_score, meta_message, telemetry

meta_learner = TradeMetaLearner()
