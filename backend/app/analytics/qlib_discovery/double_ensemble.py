"""
QLIB-INSPIRED / QLIB-COMPATIBLE IMPLEMENTATION: DoubleEnsemble
============================================================
Provenance:
    Implementation Version: qlib_research_v1
    Methodology: Microsoft Qlib DoubleEnsemble Algorithm
    Reference: Chu et al., 2020 (Microsoft Research)
               "DoubleEnsemble: A New Ensemble Method for Financial Market Data
                by Exploiting Sub-models and Data Shuffle"
    Specification Source: qlib/contrib/model/double_ensemble.py
    Framework: Scikit-learn BaseEstimator API, LightGBM / XGBoost / CatBoost
"""

import numpy as np
import copy
from typing import List, Optional, Tuple, Dict, Any
from sklearn.base import BaseEstimator, ClassifierMixin

PROVENANCE_LABEL = "QLIB-INSPIRED / QLIB-COMPATIBLE FEATURE IMPLEMENTATION"
MODEL_PROVENANCE = "Microsoft Qlib DoubleEnsemble (Chu et al., 2020) Architecture"

class DoubleEnsembleClassifier(BaseEstimator, ClassifierMixin):
    """
    DoubleEnsemble Classifier:
    Trains K sub-models sequentially with:
    1. Sample loss re-weighting / data shuffling
    2. Feature subsetting (bagging) to mitigate factor multicollinearity.
    """
    def __init__(
        self,
        base_estimator_type: str = "lightgbm",
        n_submodels: int = 5,
        sub_feature_ratio: float = 0.70,
        decay: float = 0.8,
        random_state: int = 42,
        base_params: Optional[Dict[str, Any]] = None
    ):
        self.base_estimator_type = base_estimator_type.lower()
        self.n_submodels = n_submodels
        self.sub_feature_ratio = sub_feature_ratio
        self.decay = decay
        self.random_state = random_state
        self.base_params = base_params or {}
        
        self.submodels_: List[Any] = []
        self.feature_indices_: List[np.ndarray] = []
        self.weights_: List[float] = []

    def _build_base_estimator(self, seed: int):
        params = copy.deepcopy(self.base_params)
        
        if self.base_estimator_type == "lightgbm":
            import lightgbm as lgb
            default_p = {
                "objective": "binary",
                "metric": "binary_logloss",
                "boosting_type": "gbdt",
                "n_estimators": 80,
                "learning_rate": 0.05,
                "num_leaves": 20,
                "max_depth": 5,
                "verbose": -1,
                "n_jobs": 1,
                "random_state": seed
            }
            default_p.update(params)
            return lgb.LGBMClassifier(**default_p)
            
        elif self.base_estimator_type == "xgboost":
            import xgboost as xgb
            default_p = {
                "objective": "binary:logistic",
                "eval_metric": "logloss",
                "n_estimators": 80,
                "learning_rate": 0.05,
                "max_depth": 4,
                "n_jobs": 1,
                "random_state": seed
            }
            default_p.update(params)
            return xgb.XGBClassifier(**default_p)
            
        elif self.base_estimator_type == "catboost":
            import catboost as cb
            default_p = {
                "loss_function": "Logloss",
                "iterations": 80,
                "learning_rate": 0.05,
                "depth": 4,
                "thread_count": 1,
                "verbose": False,
                "random_seed": seed
            }
            default_p.update(params)
            return cb.CatBoostClassifier(**default_p)
            
        else:
            from sklearn.ensemble import GradientBoostingClassifier
            return GradientBoostingClassifier(n_estimators=50, random_state=seed)

    def fit(self, X: np.ndarray, y: np.ndarray, sample_weight: Optional[np.ndarray] = None):
        X = np.asarray(X, dtype=np.float32)
        y = np.asarray(y, dtype=np.int32)
        n_samples, n_features = X.shape
        n_sub_features = max(2, int(n_features * self.sub_feature_ratio))
        
        rng = np.random.RandomState(self.random_state)
        self.submodels_ = []
        self.feature_indices_ = []
        self.weights_ = []
        
        current_sample_weights = np.ones(n_samples, dtype=np.float32) if sample_weight is None else np.array(sample_weight, dtype=np.float32)
        current_sample_weights /= np.mean(current_sample_weights) + 1e-8

        ensemble_preds = np.zeros(n_samples, dtype=np.float32)

        for k in range(self.n_submodels):
            sub_seed = rng.randint(0, 1000000)
            
            # 1. Feature Bagging
            feat_idx = rng.choice(n_features, size=n_sub_features, replace=False)
            feat_idx.sort()
            self.feature_indices_.append(feat_idx)
            
            X_sub = X[:, feat_idx]
            
            # 2. Fit Sub-model
            model = self._build_base_estimator(seed=sub_seed)
            model.fit(X_sub, y, sample_weight=current_sample_weights)
            self.submodels_.append(model)
            self.weights_.append(1.0)
            
            # 3. Predict & Loss Evaluation for next iteration
            sub_probs = model.predict_proba(X_sub)[:, 1]
            ensemble_preds = (ensemble_preds * k + sub_probs) / (k + 1)
            
            # Sample Loss (Cross Entropy)
            eps = 1e-6
            p_clipped = np.clip(sub_probs, eps, 1.0 - eps)
            sample_losses = -(y * np.log(p_clipped) + (1.0 - y) * np.log(1.0 - p_clipped))
            
            # Re-weighting: samples with higher loss receive higher attention
            norm_losses = sample_losses / (np.mean(sample_losses) + eps)
            current_sample_weights = (self.decay * current_sample_weights) + ((1.0 - self.decay) * norm_losses)
            current_sample_weights = np.clip(current_sample_weights, 0.1, 5.0)
            current_sample_weights /= np.mean(current_sample_weights)

        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=np.float32)
        n_samples = X.shape[0]
        accum_probs = np.zeros(n_samples, dtype=np.float64)
        total_w = sum(self.weights_)
        
        for model, feat_idx, w in zip(self.submodels_, self.feature_indices_, self.weights_):
            X_sub = X[:, feat_idx]
            accum_probs += w * model.predict_proba(X_sub)[:, 1]
            
        prob_1 = accum_probs / total_w
        prob_0 = 1.0 - prob_1
        return np.column_stack([prob_0, prob_1])

    def predict(self, X: np.ndarray, threshold: float = 0.5) -> np.ndarray:
        probs = self.predict_proba(X)[:, 1]
        return (probs >= threshold).astype(int)
