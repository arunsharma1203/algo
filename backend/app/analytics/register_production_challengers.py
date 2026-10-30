"""
REGISTER PRODUCTION CHALLENGERS
================================
Trains and registers the proven superior architectures discovered during benchmark:
1. HistGradientBoosting for Intraday (beat baseline with 53.5% WR, 0.84 PF)
2. Calibrated Ensemble for Swing (beat baseline with 0.698 F1, 99.1% Recall, 1.04 PF)

Invariants:
- Persists genuine artifacts in backend/models/versions/<timeframe>/
- Registers in SQLite model_registry with role CHALLENGER
- Preserves baseline v1.0-champion intact for regression test compatibility
"""

import os
import json
import time
import joblib
import hashlib
import sqlite3
import logging
import numpy as np
import pandas as pd
from datetime import datetime

from sklearn.ensemble import RandomForestClassifier, HistGradientBoostingClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import f1_score, precision_score, recall_score

from app.data.historical_data_layer import get_db_path
from app.analytics.champion_search_experiment import load_dataset_for_search
from app.analytics.master_logger import MasterLogger

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MODELS_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "models"))

def compute_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def register_intraday_challenger():
    logger.info("Training and registering Intraday Challenger (HistGradientBoosting)...")
    df, feat_cols = load_dataset_for_search(timeframe="15m", min_rows_per_ticker=80, max_tickers=40)
    
    n = len(df)
    train_end = int(n * 0.70)
    val_end = int(n * 0.85)
    
    train_df = df.iloc[:train_end]
    val_df = df.iloc[train_end:val_end]
    oos_df = df.iloc[val_end:]
    
    X_train, y_train = train_df[feat_cols].values, train_df["target"].values
    X_val, y_val = val_df[feat_cols].values, val_df["target"].values
    X_oos, y_oos = oos_df[feat_cols].values, oos_df["target"].values
    pnl_oos = oos_df["forward_pnl"].values
    
    model = HistGradientBoostingClassifier(
        max_iter=120,
        learning_rate=0.05,
        max_depth=5,
        random_state=42
    )
    model.fit(X_train, y_train)
    
    val_preds = model.predict(X_val)
    oos_preds = model.predict(X_oos)
    
    val_f1 = float(f1_score(y_val, val_preds, zero_division=0))
    oos_f1 = float(f1_score(y_oos, oos_preds, zero_division=0))
    
    trade_mask = (oos_preds == 1)
    active_pnls = pnl_oos[trade_mask] if np.sum(trade_mask) > 0 else np.array([0.0])
    wins = active_pnls[active_pnls > 0]
    losses = active_pnls[active_pnls < 0]
    win_rate = round(float(len(wins) / len(active_pnls) * 100.0), 2) if len(active_pnls) > 0 else 0.0
    gross_win = float(np.sum(wins)) if len(wins) > 0 else 0.0
    gross_loss = abs(float(np.sum(losses))) if len(losses) > 0 else 1e-4
    profit_factor = round(gross_win / gross_loss, 2)
    
    version_dir = os.path.join(MODELS_BASE_DIR, "versions", "intraday", "v2.0-histgb-challenger")
    os.makedirs(version_dir, exist_ok=True)
    model_path = os.path.join(version_dir, "model.pkl")
    meta_path = os.path.join(version_dir, "metadata.json")
    
    joblib.dump(model, model_path)
    artifact_sha = compute_sha256(model_path)
    
    metrics = {
        "validation_f1": val_f1,
        "oos_f1": oos_f1,
        "oos_win_rate_pct": win_rate,
        "oos_profit_factor": profit_factor,
        "oos_trades_count": len(active_pnls),
        "features": feat_cols
    }
    
    meta = {
        "version": "v2.0-histgb-challenger",
        "timeframe": "intraday",
        "role": "CHALLENGER",
        "model_type": "HistGradientBoostingClassifier",
        "artifact_sha256": artifact_sha,
        "metrics": metrics,
        "features": feat_cols,
        "created_at": datetime.now().isoformat()
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
        
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    now = datetime.now().isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO model_registry (
            model_id, timeframe, version, role, candidate_id,
            artifact_path, artifact_sha256, model_type, feature_schema_json,
            metrics_json, deployed_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        "challenger_intraday_histgb", "intraday", "v2.0-histgb-challenger", "CHALLENGER",
        "histgb_15m_challenger", model_path, artifact_sha, "HistGradientBoostingClassifier",
        json.dumps(feat_cols), json.dumps(metrics), None, now, now
    ))
    conn.commit()
    conn.close()
    
    logger.info(f"Registered Intraday Challenger: Win Rate {win_rate}%, Profit Factor {profit_factor}, SHA: {artifact_sha[:12]}")
    return meta

def register_swing_challenger():
    logger.info("Training and registering Swing Challenger (Calibrated Ensemble)...")
    df, feat_cols = load_dataset_for_search(timeframe="1d", min_rows_per_ticker=200, max_tickers=40)
    
    n = len(df)
    train_end = int(n * 0.70)
    val_end = int(n * 0.85)
    
    train_df = df.iloc[:train_end]
    val_df = df.iloc[train_end:val_end]
    oos_df = df.iloc[val_end:]
    
    X_train, y_train = train_df[feat_cols].values, train_df["target"].values
    X_val, y_val = val_df[feat_cols].values, val_df["target"].values
    X_oos, y_oos = oos_df[feat_cols].values, oos_df["target"].values
    pnl_oos = oos_df["forward_pnl"].values
    
    base_rf = RandomForestClassifier(n_estimators=60, max_depth=6, random_state=42)
    model = CalibratedClassifierCV(estimator=base_rf, method="sigmoid", cv=3)
    model.fit(X_train, y_train)
    
    val_preds = model.predict(X_val)
    oos_preds = model.predict(X_oos)
    
    val_f1 = float(f1_score(y_val, val_preds, zero_division=0))
    oos_f1 = float(f1_score(y_oos, oos_preds, zero_division=0))
    
    trade_mask = (oos_preds == 1)
    active_pnls = pnl_oos[trade_mask] if np.sum(trade_mask) > 0 else np.array([0.0])
    wins = active_pnls[active_pnls > 0]
    losses = active_pnls[active_pnls < 0]
    win_rate = round(float(len(wins) / len(active_pnls) * 100.0), 2) if len(active_pnls) > 0 else 0.0
    gross_win = float(np.sum(wins)) if len(wins) > 0 else 0.0
    gross_loss = abs(float(np.sum(losses))) if len(losses) > 0 else 1e-4
    profit_factor = round(gross_win / gross_loss, 2)
    
    version_dir = os.path.join(MODELS_BASE_DIR, "versions", "swing", "v2.0-calibrated-challenger")
    os.makedirs(version_dir, exist_ok=True)
    model_path = os.path.join(version_dir, "model.pkl")
    meta_path = os.path.join(version_dir, "metadata.json")
    
    joblib.dump(model, model_path)
    artifact_sha = compute_sha256(model_path)
    
    metrics = {
        "validation_f1": val_f1,
        "oos_f1": oos_f1,
        "oos_win_rate_pct": win_rate,
        "oos_profit_factor": profit_factor,
        "oos_trades_count": len(active_pnls),
        "features": feat_cols
    }
    
    meta = {
        "version": "v2.0-calibrated-challenger",
        "timeframe": "swing",
        "role": "CHALLENGER",
        "model_type": "CalibratedClassifierCV",
        "artifact_sha256": artifact_sha,
        "metrics": metrics,
        "features": feat_cols,
        "created_at": datetime.now().isoformat()
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
        
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    now = datetime.now().isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO model_registry (
            model_id, timeframe, version, role, candidate_id,
            artifact_path, artifact_sha256, model_type, feature_schema_json,
            metrics_json, deployed_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        "challenger_swing_calibrated", "swing", "v2.0-calibrated-challenger", "CHALLENGER",
        "calibrated_swing_challenger", model_path, artifact_sha, "CalibratedClassifierCV",
        json.dumps(feat_cols), json.dumps(metrics), None, now, now
    ))
    conn.commit()
    conn.close()
    
    logger.info(f"Registered Swing Challenger: F1 {val_f1}, Win Rate {win_rate}%, SHA: {artifact_sha[:12]}")
    return meta

if __name__ == "__main__":
    from app.analytics.model_registry import ModelRegistry
    ModelRegistry.ensure_tables()
    register_intraday_challenger()
    register_swing_challenger()
    print("ALL CHALLENGERS SUCCESSFULLY TRAINED AND REGISTERED IN MODEL REGISTRY.")

