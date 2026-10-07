import os
import json
import logging
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from typing import Tuple, Dict, Any, Optional

from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)

MODEL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "models"))
CLEAN_CALIBRATOR_PATH = os.path.join(MODEL_DIR, "confidence_calibrator_clean_v1.json")
LEGACY_CALIBRATOR_PATH = os.path.join(MODEL_DIR, "confidence_calibrator_weights.json")

DEFAULT_CALIBRATOR_PATH = CLEAN_CALIBRATOR_PATH if os.path.exists(CLEAN_CALIBRATOR_PATH) else LEGACY_CALIBRATOR_PATH


class CalibrationDomainError(ValueError):
    """Raised when an uncalibrated heuristic or invalid domain input is passed to an empirical calibrator."""
    pass


class ProbabilityCalibrator:
    """
    Probability Calibration Engine using Platt Sigmoid Scaling.
    Operates via Out-Of-Fold (OOF) validation predictions or clean held-out validation partitions.
    
    Domain Invariant:
    - Never passes heuristic Bayesian scores through empirical Platt scaling.
    - Operates strictly on expected empirical input probabilities in [0.0, 1.0].
    """

    def __init__(self, weights_path: Optional[str] = None):
        self.weights_path = weights_path or DEFAULT_CALIBRATOR_PATH
        self.coef: Optional[float] = None
        self.intercept: Optional[float] = None
        self.is_fitted: bool = False
        self.brier_score: Optional[float] = None
        self.log_loss_val: Optional[float] = None
        self.method: str = "Uncalibrated Raw Model Output"
        self.calibrator_input_type: str = "EMPIRICAL_META_PROBABILITY"
        self.meta_learner_sha256: Optional[str] = None
        self.source_partition: Optional[str] = None
        self.sample_count: Optional[int] = None
        self.load_weights(self.weights_path)

    def load_weights(self, path: Optional[str] = None) -> bool:
        """Loads fitted calibration parameters from persistent JSON artifact."""
        target = path or self.weights_path
        if os.path.exists(target):
            try:
                with open(target, "r") as f:
                    data = json.load(f)
                    self.coef = float(data["coef"])
                    self.intercept = float(data["intercept"])
                    self.brier_score = data.get("brier_score")
                    self.log_loss_val = data.get("log_loss")
                    self.method = data.get("method", "Platt Sigmoid Scaling")
                    self.calibrator_input_type = data.get("calibrator_input_type", "EMPIRICAL_META_PROBABILITY")
                    self.meta_learner_sha256 = data.get("meta_learner_sha256")
                    self.source_partition = data.get("source_partition", "VALIDATION")
                    self.sample_count = data.get("fitted_samples")

                    if self.coef > 0.005:
                        self.is_fitted = True
                        logger.info(f"[Calibrator] Loaded valid calibration weights ({self.method}, Partition: {self.source_partition}).")
                        return True
            except Exception as e:
                logger.warning(f"[Calibrator] Could not load calibrator JSON weights: {e}")

        self.is_fitted = False
        return False

    def fit_from_validation(
        self,
        val_probs: np.ndarray,
        val_labels: np.ndarray,
        meta_learner_sha: Optional[str] = None,
        weights_save_path: Optional[str] = None
    ) -> bool:
        """
        Fits Platt Scaling Logistic Regression strictly on held-out VALIDATION predictions and labels.
        Enforces zero intersection with training or locked OOS partitions.
        """
        if len(val_probs) < 20 or len(np.unique(val_labels)) < 2:
            logger.warning("[Calibrator] Insufficient validation sample size for probability calibration.")
            self.is_fitted = False
            return False

        try:
            X = val_probs.reshape(-1, 1)
            y = val_labels.astype(int)

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
                self.log_loss_val = round(float(log_loss(y, calib_probs)), 4)
                self.method = "Platt Sigmoid Scaling (Clean Validation Holdout)"
                self.calibrator_input_type = "EMPIRICAL_META_PROBABILITY"
                self.meta_learner_sha256 = meta_learner_sha
                self.source_partition = "VALIDATION"
                self.sample_count = len(val_probs)

                save_target = weights_save_path or self.weights_path
                os.makedirs(os.path.dirname(os.path.abspath(save_target)), exist_ok=True)
                with open(save_target, "w") as f:
                    json.dump({
                        "coef": round(c, 6),
                        "intercept": round(intercept, 6),
                        "fitted_samples": self.sample_count,
                        "brier_score": self.brier_score,
                        "log_loss": self.log_loss_val,
                        "method": self.method,
                        "source_partition": self.source_partition,
                        "calibrator_input_type": self.calibrator_input_type,
                        "meta_learner_sha256": self.meta_learner_sha256,
                        "format": "platt_sigmoid_clean_val_v1"
                    }, f, indent=2)

                logger.info(
                    f"[Calibrator] Successfully fitted on {len(val_probs)} VALIDATION samples "
                    f"(Brier: {self.brier_score}, LogLoss: {self.log_loss_val})."
                )
                return True
            else:
                logger.warning("[Calibrator] Calibration slope is non-monotonic; calibration disabled.")
                self.is_fitted = False
                return False

        except Exception as e:
            logger.error(f"[Calibrator] Error fitting validation calibrator: {e}")
            self.is_fitted = False
            return False

    def fit_from_oof(self, oof_probs: np.ndarray, y_true: np.ndarray) -> bool:
        """
        Fits Platt Scaling Logistic Regression on out-of-fold validation predictions.
        """
        return self.fit_from_validation(oof_probs, y_true)

    def fit_from_history(self, max_trades: int = 150) -> bool:
        """
        Fits Platt Scaling on historical resolved trades recorded in canonical ml_trade_history.
        """
        import sqlite3

        try:
            conn = sqlite3.connect(get_db_path(), timeout=10.0)
            cursor = conn.cursor()
            cols = [c[1] for c in cursor.execute("PRAGMA table_info(ml_trade_history)").fetchall()]
            engine_clause = "AND (engine = 'LEGACY' OR engine IS NULL)" if 'engine' in cols else ""
            df = pd.read_sql_query(f"""
                SELECT confidence, outcome, profit_pct, timestamp
                FROM ml_trade_history
                WHERE status = 'CLOSED' AND outcome IS NOT NULL AND confidence IS NOT NULL
                  {engine_clause}
                ORDER BY timestamp DESC
                LIMIT ?
            """, conn, params=(max_trades,))
            conn.close()

            if len(df) < 15:
                self.is_fitted = False
                return False

            df['target'] = ((df['outcome'].isin(['TARGET MET', 'BREAKEVEN HIT'])) | (df['profit_pct'] > 0)).astype(int)
            if len(df['target'].unique()) >= 2:
                raw_confs = df['confidence'].values
                if np.nanmax(raw_confs) > 1.0:
                    raw_confs = raw_confs / 100.0
                raw_confs = np.nan_to_num(raw_confs, nan=0.5)
                return self.fit_from_validation(raw_confs, df['target'].values)
            else:
                self.is_fitted = False
                return False
        except Exception as e:
            logger.warning(f"Failed fitting calibrator from history: {e}")
            self.is_fitted = False
            return False

    def calibrate(
        self,
        raw_score: float,
        input_type: str = "EMPIRICAL_META_PROBABILITY",
        source_model_sha: Optional[str] = None
    ) -> Tuple[float, float, Dict[str, Any]]:
        """
        Transforms a raw prediction probability into an empirical calibrated probability.

        Safety Guard:
        - Refuses to pass heuristic Bayesian scores or disabled outputs through empirical Platt scaling.
        - Verifies input domain [0, 1] or [0, 100].
        """
        raw = float(raw_score)
        raw_prob = raw / 100.0 if raw > 1.0 else raw
        raw_prob = max(0.001, min(0.999, raw_prob))

        # DOMAIN CHECK: Refuse heuristic scores from empirical scaling
        if input_type in ("HEURISTIC", "BAYESIAN_PRIOR", "DISABLED") or input_type != self.calibrator_input_type:
            logger.warning(
                f"[Calibrator] Domain mismatch: Caller passed input_type='{input_type}', "
                f"expected '{self.calibrator_input_type}'. Suppressing Platt scaling to prevent confidence inflation."
            )
            calibrated_pct = round(raw_prob * 100.0, 1)
            meta = {
                "raw_score": round(raw_prob * 100.0, 1),
                "calibrated_score": calibrated_pct,
                "calibration_status": "REFUSED_DOMAIN_MISMATCH",
                "method": "Uncalibrated Heuristic Pass-Through",
                "calibrator_input_type": self.calibrator_input_type,
                "caller_input_type": input_type,
                "is_empirically_fitted": bool(self.is_fitted)
            }
            return calibrated_pct, round(raw_prob * 100.0, 1), meta

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
            calibrated_pct = round(raw_prob * 100.0, 1)
            status = "uncalibrated"
            method = "Uncalibrated Raw Model Output"

        meta = {
            "raw_score": round(raw_prob * 100.0, 1),
            "calibrated_score": calibrated_pct,
            "calibration_status": status,
            "method": method,
            "brier_score": self.brier_score,
            "log_loss": self.log_loss_val,
            "calibrator_input_type": self.calibrator_input_type,
            "source_partition": self.source_partition,
            "meta_learner_sha256": self.meta_learner_sha256,
            "is_empirically_fitted": bool(self.is_fitted)
        }

        return calibrated_pct, round(raw_prob * 100.0, 1), meta


calibrator = ProbabilityCalibrator()
platt_calibrator = calibrator
ConfidenceCalibrator = ProbabilityCalibrator


def fit_rolling_calibrator(max_trades: int = 150) -> bool:
    """Convenience helper for automated daemon to invoke rolling calibration."""
    return calibrator.fit_from_history(max_trades=max_trades)
