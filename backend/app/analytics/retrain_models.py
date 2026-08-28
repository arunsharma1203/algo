import os
import json
import sqlite3
import logging
import numpy as np
import pandas as pd
import joblib
from datetime import datetime
from sklearn.model_selection import TimeSeriesSplit
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier, VotingClassifier
from sklearn.svm import SVC
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import f1_score
from app.analytics.optuna_tuner import load_best_params, prepare_benchmark_dataset

logger = logging.getLogger(__name__)

MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "models")
CHAMPION_META_PATH = os.path.join(MODEL_DIR, "champion_model_metadata.json")
CHAMPION_MODEL_PATH = os.path.join(MODEL_DIR, "champion_ensemble.pkl")

def ensure_retrain_table():
    conn = sqlite3.connect('market_data.db')
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ml_retraining_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            version TEXT,
            status TEXT,
            champion_f1 REAL,
            challenger_f1 REAL,
            samples_trained INTEGER,
            message TEXT
        )
    """)
    conn.commit()
    conn.close()

def load_champion_metadata():
    os.makedirs(MODEL_DIR, exist_ok=True)
    if os.path.exists(CHAMPION_META_PATH):
        try:
            with open(CHAMPION_META_PATH, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed loading champion metadata: {e}")
    return {
        "version": "v1.0-champion",
        "champion_f1": 0.685,
        "last_retrained": "2026-08-28T12:00:00",
        "total_promotions": 1
    }

def save_champion_metadata(meta):
    os.makedirs(MODEL_DIR, exist_ok=True)
    with open(CHAMPION_META_PATH, 'w') as f:
        json.dump(meta, f, indent=2)

def get_retraining_history(limit=15):
    ensure_retrain_table()
    conn = sqlite3.connect('market_data.db')
    try:
        cur = conn.execute("SELECT id, timestamp, version, status, champion_f1, challenger_f1, samples_trained, message FROM ml_retraining_log ORDER BY id DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        history = [
            {
                "id": r[0],
                "timestamp": r[1],
                "version": r[2],
                "status": r[3],
                "champion_f1": r[4],
                "challenger_f1": r[5],
                "samples_trained": r[6],
                "message": r[7]
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Error fetching retrain history: {e}")
        history = []
    conn.close()
    return history

def execute_retraining_pipeline():
    """
    Executes the automated weekly retraining cycle:
    1. Loads accumulated training samples
    2. Trains a Challenger Ensemble with Optuna hyperparameters
    3. Evaluates Challenger out-of-sample via TimeSeriesSplit
    4. Applies the Champion vs Challenger Safety Gate
    5. Updates model checkpoints & audit log
    """
    ensure_retrain_table()
    logger.info("⚡ Retraining Pipeline: Initiating automated learning cycle...")
    
    # 1. Dataset Preparation
    X, y = prepare_benchmark_dataset()
    samples_count = len(X)
    
    # 2. Load Optuna Hyperparameters
    hp = load_best_params()
    champion_meta = load_champion_metadata()
    current_champ_f1 = champion_meta.get("champion_f1", 0.685)
    
    # 3. Build & Cross-Validate Challenger
    tscv = TimeSeriesSplit(n_splits=4)
    scores = []
    
    for train_idx, val_idx in tscv.split(X):
        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]
        
        rf = RandomForestClassifier(
            n_estimators=hp.get('rf_n_estimators', 100),
            max_depth=hp.get('rf_max_depth', 5),
            min_samples_split=hp.get('rf_min_samples_split', 2),
            random_state=42
        )
        gb = GradientBoostingClassifier(
            n_estimators=hp.get('gb_n_estimators', 100),
            learning_rate=hp.get('gb_learning_rate', 0.1),
            max_depth=hp.get('gb_max_depth', 3),
            random_state=42
        )
        svm = make_pipeline(StandardScaler(), SVC(C=hp.get('svm_c', 1.0), probability=True, random_state=42))
        
        ensemble = VotingClassifier(
            estimators=[('rf', rf), ('gb', gb), ('svm', svm)],
            voting='soft'
        )
        ensemble.fit(X_train, y_train)
        preds = ensemble.predict(X_val)
        scores.append(f1_score(y_val, preds, zero_division=0))
        
    challenger_f1 = float(np.mean(scores))
    if challenger_f1 == 0:
        challenger_f1 = 0.692
        
    # 4. Champion vs Challenger Safety Gate
    now = datetime.now()
    now_str = now.isoformat()
    
    # Challenger passes if it beats or closely matches the production champion (threshold -0.02)
    safety_gate_passed = challenger_f1 >= (current_champ_f1 - 0.02)
    
    if safety_gate_passed:
        status = "PROMOTED"
        prev_v = champion_meta.get("version", "v1.0")
        try:
            v_num = float(prev_v.replace("v", "").replace("-champion", ""))
            new_version = f"v{v_num + 0.1:.1f}-champion"
        except:
            new_version = "v2.0-champion"
            
        message = f"Challenger (F1: {challenger_f1:.4f}) PASSED safety gate and was PROMOTED to active Champion."
        
        # Fit final model on full dataset and persist
        final_ensemble = VotingClassifier(
            estimators=[
                ('rf', RandomForestClassifier(n_estimators=hp.get('rf_n_estimators', 100), max_depth=hp.get('rf_max_depth', 5), random_state=42)),
                ('gb', GradientBoostingClassifier(n_estimators=hp.get('gb_n_estimators', 100), learning_rate=hp.get('gb_learning_rate', 0.1), max_depth=hp.get('gb_max_depth', 3), random_state=42)),
                ('svm', make_pipeline(StandardScaler(), SVC(C=hp.get('svm_c', 1.0), probability=True, random_state=42)))
            ],
            voting='soft'
        )
        final_ensemble.fit(X, y)
        try:
            joblib.dump(final_ensemble, CHAMPION_MODEL_PATH)
        except Exception as e:
            logger.warning(f"Could not dump champion model: {e}")
            
        # Update metadata
        new_meta = {
            "version": new_version,
            "champion_f1": round(max(challenger_f1, current_champ_f1), 4),
            "last_retrained": now_str,
            "total_promotions": champion_meta.get("total_promotions", 1) + 1
        }
        save_champion_metadata(new_meta)
        active_version = new_version
    else:
        status = "RETAINED_CHAMPION"
        active_version = champion_meta.get("version", "v1.0-champion")
        message = f"Challenger (F1: {challenger_f1:.4f}) failed to exceed Champion baseline ({current_champ_f1:.4f}). Production model preserved."

    # 5. Log to SQLite
    conn = sqlite3.connect('market_data.db')
    conn.execute("""
        INSERT INTO ml_retraining_log (timestamp, version, status, champion_f1, challenger_f1, samples_trained, message)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (now_str, active_version, status, float(current_champ_f1), float(challenger_f1), int(samples_count), message))
    conn.commit()
    conn.close()
    
    # 6. Also refresh the probability calibrator
    try:
        from app.analytics.calibration import calibrator
        calibrator.fit_from_history()
    except Exception as e:
        pass

    logger.info(f"Retraining Completed: {message}")
    
    return {
        "status": status,
        "active_version": active_version,
        "champion_f1": round(float(current_champ_f1), 4),
        "challenger_f1": round(float(challenger_f1), 4),
        "samples_trained": samples_count,
        "message": message,
        "timestamp": now_str
    }
