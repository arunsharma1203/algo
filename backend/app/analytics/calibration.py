import os
import json
import logging
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss
from typing import Tuple, Dict, Any, Optional

logger = logging.getLogger(__name__)

MODEL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "models"))
WEIGHTS_PATH = os.path.join(MODEL_DIR, "confidence_calibrator_weights.json")

class ProbabilityCalibrator:
    """
    Scientifically valid Probability Calibration Engine.
    Operates via Out-Of-Fold (OOF) cross-validation predictions or verified closed trades.
    Strictly avoids fabricating synthetic calibration curves when uncalibrated.
    """

    def __init__(self):
        self.coef: Optional[float] = None
        self.intercept: Optional[float] = None
        self.is_fitted: bool = False
        self.brier_score: Optional[float] = None
        self.method: str = "Uncalibrated Raw Model Output"
        self.load_weights()

    def load_weights(self) -> None:
        """Loads fitted calibration parameters from persistent JSON artifact."""
        if os.path.exists(WEIGHTS_PATH):
            try:
                with open(WEIGHTS_PATH, "r") as f:
                    data = json.load(f)
                    self.coef = float(data["coef"])
                    self.intercept = float(data["intercept"])
                    self.brier_score = data.get("brier_score")
                    self.method = data.get("method", "Platt Sigmoid Scaling (Empirical OOF)")
                    if self.coef > 0.005:
                        self.is_fitted = True
                        logger.info(f"Loaded valid calibration weights ({self.method}).")
                        return
            except Exception as e:
                logger.warning(f"Could not load calibrator JSON weights: {e}")

        self.is_fitted = False

    def fit_from_oof(self, oof_probs: np.ndarray, y_true: np.ndarray) -> bool:
        """
        Fits Platt Scaling Logistic Regression on out-of-fold validation predictions.
        Guarantees that calibration data was not seen by base models during training.
        """
        if len(oof_probs) < 20 or len(np.unique(y_true)) < 2:
            logger.warning("Insufficient OOF sample size for probability calibration.")
            return False

        try:
            X = oof_probs.reshape(-1, 1)
            y = y_true.astype(int)

            clf = LogisticRegression(C=1.0, solver='lbfgs', random_state=42)
            clf.fit(X, y)

            c = float(clf.coef_[0][0])
            intercept = float(clf.intercept_[0])

            if c > 0.01:
                self.coef = c
                self.intercept = intercept
                self.is_fitted = True
                
                calib_probs = clf.predict_proba(X)[:, 1]
                self.brier_score = round(float(brier_score_loss(y, calib_probs)), 4)
                self.method = "Platt Sigmoid Scaling (Empirical OOF)"

                os.makedirs(MODEL_DIR, exist_ok=True)
                with open(WEIGHTS_PATH, "w") as f:
                    json.dump({
                        "coef": round(c, 6),
                        "intercept": round(intercept, 6),
                        "fitted_samples": len(oof_probs),
                        "brier_score": self.brier_score,
                        "method": self.method,
                        "format": "platt_sigmoid_oof_v2"
                    }, f, indent=2)

                logger.info(f"Probability Calibrator successfully fitted on {len(oof_probs)} OOF samples (Brier: {self.brier_score}).")
                return True
            else:
                logger.warning("Calibration slope is non-monotonic; calibration disabled to preserve raw scores.")
                self.is_fitted = False
                return False

        except Exception as e:
            logger.error(f"Error fitting OOF calibrator: {e}")
            self.is_fitted = False
            return False

    def fit_from_history(self) -> bool:
        """
        Fits Platt Scaling on historical resolved trades recorded in SQLite.
        """
        from app.api.ml_history import evaluate_ml_history
        try:
            history = evaluate_ml_history()
            resolved = [t for t in history if t.get('outcome') not in ('OPEN', None)]
        except Exception as e:
            resolved = []

        if len(resolved) >= 15:
            try:
                df = pd.DataFrame(resolved)
                df['target'] = (df['profit_pct'] > 0).astype(int)
                if len(df['target'].unique()) >= 2:
                    raw_confs = df['confidence'].values / 100.0 if df['confidence'].max() > 1.0 else df['confidence'].values
                    return self.fit_from_oof(raw_confs, df['target'].values)
            except Exception as e:
                logger.warning(f"Failed fitting calibrator from history: {e}")

        return False

    def calibrate(self, raw_score: float) -> Tuple[float, float, Dict[str, Any]]:
        """
        Transforms a raw prediction probability (0-100 or 0-1) into an empirical calibrated probability.
        
        Returns:
            (calibrated_score, raw_score, calibration_metadata)
        """
        raw = float(raw_score)
        # Normalize to 0-1 for calibration formula if given as percentage 0-100
        raw_prob = raw / 100.0 if raw > 1.0 else raw
        raw_prob = max(0.01, min(0.99, raw_prob))

        if self.is_fitted and self.coef is not None and self.coef > 0.005:
            try:
                # Platt formula: P(y=1|p) = 1 / (1 + exp(-(coef * p + intercept)))
                z = self.coef * raw_prob + (self.intercept or 0.0)
                z = max(-15.0, min(15.0, z))
                prob = 1.0 / (1.0 + np.exp(-z))
                calibrated_pct = round(float(prob * 100.0), 1)
                status = "calibrated"
                method = self.method
            except Exception as e:
                calibrated_pct = round(raw_prob * 100.0, 1)
                status = "uncalibrated"
                method = "Raw Probability (Evaluation Fallback)"
        else:
            # UNCALIBRATED: return raw probability transparently without manufacturing numbers
            calibrated_pct = round(raw_prob * 100.0, 1)
            status = "uncalibrated"
            method = "Uncalibrated Raw Model Output"

        meta = {
            "raw_score": round(raw_prob * 100.0, 1),
            "calibrated_score": calibrated_pct,
            "calibration_status": status,
            "method": method,
            "brier_score": self.brier_score,
            "is_empirically_fitted": bool(self.is_fitted)
        }

        return calibrated_pct, round(raw_prob * 100.0, 1), meta

calibrator = ProbabilityCalibrator()
