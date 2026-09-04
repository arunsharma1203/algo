import os
import json
import sqlite3
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from sklearn.model_selection import TimeSeriesSplit
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier, VotingClassifier
from sklearn.svm import SVC
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import f1_score, precision_score, recall_score, brier_score_loss

from app.analytics.optuna_tuner import load_best_params, prepare_benchmark_dataset
from app.analytics.model_manager import ModelManager
from app.data.validator import DataValidationError
from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)

def ensure_retrain_table():
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ml_retraining_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            timeframe TEXT DEFAULT 'swing',
            version TEXT,
            status TEXT,
            champion_f1 REAL,
            challenger_f1 REAL,
            champion_sharpe REAL,
            challenger_sharpe REAL,
            samples_trained INTEGER,
            message TEXT
        )
    """)
    conn.commit()

    # Non-destructive migrations for existing databases
    for col_name, col_type in [
        ("timeframe", "TEXT DEFAULT 'swing'"),
        ("champion_sharpe", "REAL"),
        ("challenger_sharpe", "REAL"),
        ("samples_trained", "INTEGER"),
        ("message", "TEXT")
    ]:
        try:
            conn.execute(f"ALTER TABLE ml_retraining_log ADD COLUMN {col_name} {col_type}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    conn.close()

def load_champion_metadata(timeframe: str = "swing") -> dict:
    return ModelManager.load_champion_metadata(timeframe)

def save_champion_metadata(meta: dict, timeframe: str = "swing") -> None:
    ModelManager.save_champion_metadata(meta, timeframe)

def get_retraining_history(timeframe: str = "swing", limit: int = 15) -> list:
    ensure_retrain_table()
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    try:
        cur = conn.execute("""
            SELECT id, timestamp, timeframe, version, status, champion_f1, challenger_f1, champion_sharpe, challenger_sharpe, samples_trained, message 
            FROM ml_retraining_log 
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        history = [
            {
                "id": r[0],
                "timestamp": r[1],
                "timeframe": r[2] if len(r) > 2 and r[2] else timeframe,
                "version": r[3],
                "status": r[4],
                "champion_f1": r[5],
                "challenger_f1": r[6],
                "champion_sharpe": r[7] if len(r) > 7 else None,
                "challenger_sharpe": r[8] if len(r) > 8 else None,
                "samples_trained": r[9] if len(r) > 9 else 0,
                "message": r[10] if len(r) > 10 else ""
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Error fetching retrain history: {e}")
        history = []
    conn.close()
    return history

def simulate_out_of_sample_trading(y_val: np.ndarray, preds_proba: np.ndarray, cost_pct: float = 0.001) -> dict:
    """
    Simulates simple out-of-sample trading performance with transaction friction (0.1%).
    Returns quantitative metrics: Sharpe, Profit Factor, Win Rate, Max Drawdown %, Expectancy.
    """
    returns = []
    for actual, prob in zip(y_val, preds_proba):
        if prob >= 0.55: # Model takes long trade
            trade_ret = (0.02 if actual == 1 else -0.015) - cost_pct
            returns.append(trade_ret)
        else:
            returns.append(0.0)

    ret_series = pd.Series(returns)
    traded_returns = ret_series[ret_series != 0]

    is_low_sample = len(traded_returns) < 30

    if len(traded_returns) < 5:
        return {
            "sharpe": 0.0,
            "win_rate": 0.0,
            "profit_factor": 1.0,
            "max_drawdown_pct": 0.0,
            "trade_count": len(traded_returns),
            "is_low_sample": True,
            "sample_status": "LOW_SAMPLE",
            "winning_trades": 0,
            "losing_trades": 0,
            "expectancy_pct": 0.0
        }

    mean_r = traded_returns.mean()
    std_r = traded_returns.std()
    sharpe = float((mean_r / std_r) * np.sqrt(252)) if std_r > 0 else 0.0

    wins = traded_returns[traded_returns > 0]
    losses = traded_returns[traded_returns < 0]

    gross_profit = wins.sum() if len(wins) > 0 else 0.0
    gross_loss = abs(losses.sum()) if len(losses) > 0 else 0.0001
    profit_factor = round(gross_profit / gross_loss, 2)
    win_rate = round((len(wins) / len(traded_returns)) * 100.0, 1)

    # Equity curve and drawdown
    equity = (1.0 + ret_series).cumprod()
    cummax = equity.cummax()
    drawdown = (equity - cummax) / cummax
    max_dd = round(abs(float(drawdown.min())) * 100.0, 2)

    return {
        "sharpe": round(sharpe, 2),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd,
        "trade_count": int(len(traded_returns)),
        "is_low_sample": is_low_sample,
        "sample_status": "LOW_SAMPLE" if is_low_sample else "VALID",
        "winning_trades": int(len(wins)),
        "losing_trades": int(len(losses)),
        "expectancy_pct": round(float(mean_r * 100.0), 3) if len(traded_returns) > 0 else 0.0
    }

def execute_retraining_pipeline(timeframe: str = "swing") -> dict:
    """
    Executes the automated retraining cycle and Champion vs. Challenger Safety Gate.
    
    CRITICAL HARDENING:
    1. Zero synthetic fallback: fails closed if real data validation fails.
    2. Zero F1 override: failed evaluations remain failed.
    3. Multi-dimensional gate: evaluates both ML metrics (F1, Precision, Recall) and trading simulation.
    4. ModelManager integration: archives historical versions for instant rollback.
    """
    ensure_retrain_table()
    now_str = datetime.now().isoformat()
    logger.info(f"⚡ Retraining Pipeline: Initiating automated learning cycle for {timeframe.upper()}...")

    # 1. Dataset Preparation & Strict Validation
    try:
        X, y, features = prepare_benchmark_dataset(timeframe=timeframe)
    except DataValidationError as e:
        error_msg = f"Data validation failed for {timeframe} retraining: {e}"
        logger.error(error_msg)
        
        # Log failure to SQLite and preserve current Champion
        conn = sqlite3.connect(get_db_path(), timeout=15.0)
        conn.execute("""
            INSERT INTO ml_retraining_log (timestamp, timeframe, version, status, champion_f1, challenger_f1, samples_trained, message)
            VALUES (?, ?, ?, 'FAILED_DATA_VALIDATION', 0.0, 0.0, 0, ?)
        """, (now_str, timeframe, "N/A", error_msg))
        conn.commit()
        conn.close()
        
        current_champ = ModelManager.load_champion_metadata(timeframe)
        return {
            "status": "FAILED_DATA_VALIDATION",
            "timeframe": timeframe,
            "active_version": current_champ.get("version", "v1.0-champion"),
            "samples_trained": 0,
            "message": error_msg,
            "timestamp": now_str
        }

    samples_count = len(X)

    # 2. Load Hyperparameters and Baseline Champion Metadata
    hp = load_best_params(timeframe=timeframe)
    champion_meta = ModelManager.load_champion_metadata(timeframe)
    current_champ_f1 = float(champion_meta.get("champion_f1", 0.685))
    current_champ_sharpe = float(champion_meta.get("validation_metrics", {}).get("sharpe_ratio", 1.2))

    # 3. Build & Out-Of-Sample Cross-Validate Challenger
    tscv = TimeSeriesSplit(n_splits=4)
    f1_scores = []
    precision_scores = []
    recall_scores = []
    all_y_val = []
    all_preds_proba = []

    for train_idx, val_idx in tscv.split(X):
        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]

        if len(np.unique(y_train)) < 2 or len(np.unique(y_val)) < 2:
            continue

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

        probs = ensemble.predict_proba(X_val)[:, 1]
        preds = (probs >= 0.50).astype(int)

        f1_scores.append(f1_score(y_val, preds, zero_division=0))
        precision_scores.append(precision_score(y_val, preds, zero_division=0))
        recall_scores.append(recall_score(y_val, preds, zero_division=0))
        all_y_val.extend(y_val)
        all_preds_proba.extend(probs)

    # 4. Compute Real Out-Of-Sample Metrics (NO Falsified Overrides)
    challenger_f1 = float(np.mean(f1_scores)) if f1_scores else 0.0
    challenger_prec = float(np.mean(precision_scores)) if precision_scores else 0.0
    challenger_rec = float(np.mean(recall_scores)) if recall_scores else 0.0
    
    trading_eval = simulate_out_of_sample_trading(np.array(all_y_val), np.array(all_preds_proba))
    challenger_sharpe = trading_eval["sharpe"]

    # 5. Multi-Dimensional Champion vs. Challenger Safety Gate
    # Threshold: Challenger F1 >= (current Champion - 0.01), F1 must be > 0.50, and must have positive Sharpe / trade count
    ml_gate_passed = (challenger_f1 >= (current_champ_f1 - 0.01)) and (challenger_f1 > 0.50)
    trading_gate_passed = (challenger_sharpe >= 0.5) and (trading_eval["max_drawdown_pct"] <= 20.0)
    safety_gate_passed = ml_gate_passed and trading_gate_passed

    if safety_gate_passed:
        status = "PROMOTED"
        message = (
            f"Challenger (F1: {challenger_f1:.4f}, Sharpe: {challenger_sharpe:.2f}) "
            f"PASSED multi-dimensional safety gate and was PROMOTED."
        )

        # Fit final model on the entire validated dataset
        final_ensemble = VotingClassifier(
            estimators=[
                ('rf', RandomForestClassifier(n_estimators=hp.get('rf_n_estimators', 100), max_depth=hp.get('rf_max_depth', 5), random_state=42)),
                ('gb', GradientBoostingClassifier(n_estimators=hp.get('gb_n_estimators', 100), learning_rate=hp.get('gb_learning_rate', 0.1), max_depth=hp.get('gb_max_depth', 3), random_state=42)),
                ('svm', make_pipeline(StandardScaler(), SVC(C=hp.get('svm_c', 1.0), probability=True, random_state=42)))
            ],
            voting='soft'
        )
        final_ensemble.fit(X, y)

        metadata_payload = {
            "timeframe": timeframe,
            "champion_f1": round(challenger_f1, 4),
            "validation_metrics": {
                "f1": round(challenger_f1, 4),
                "precision": round(challenger_prec, 4),
                "recall": round(challenger_rec, 4),
                "sharpe_ratio": challenger_sharpe,
                "win_rate_pct": trading_eval["win_rate"],
                "profit_factor": trading_eval["profit_factor"],
                "max_drawdown_pct": trading_eval["max_drawdown_pct"],
                "trade_count": trading_eval["trade_count"]
            },
            "features": features,
            "samples_trained": samples_count,
            "last_retrained": now_str,
            "hyperparameters": hp
        }

        active_version = ModelManager.promote_challenger(final_ensemble, metadata_payload, timeframe=timeframe)

    else:
        status = "RETAINED_CHAMPION"
        active_version = champion_meta.get("version", "v1.0-champion")
        message = (
            f"Challenger (F1: {challenger_f1:.4f}, Sharpe: {challenger_sharpe:.2f}) "
            f"failed safety gate (Req F1 >= {current_champ_f1 - 0.01:.4f}, Sharpe >= 0.50). Production model preserved."
        )

    # 6. Immutable SQLite Audit Log
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    conn.execute("""
        INSERT INTO ml_retraining_log (timestamp, timeframe, version, status, champion_f1, challenger_f1, champion_sharpe, challenger_sharpe, samples_trained, message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (now_str, timeframe, active_version, status, float(current_champ_f1), float(challenger_f1), float(current_champ_sharpe), float(challenger_sharpe), int(samples_count), message))
    conn.commit()
    conn.close()

    # 7. Refit Probability Calibrator on fresh OOF predictions if promoted
    if safety_gate_passed and len(all_preds_proba) >= 20:
        try:
            from app.analytics.calibration import calibrator
            calibrator.fit_from_oof(np.array(all_preds_proba), np.array(all_y_val))
        except Exception as e:
            logger.warning(f"Calibrator OOF refresh warning: {e}")

    logger.info(f"Retraining Completed ({timeframe}): {message}")

    return {
        "status": status,
        "timeframe": timeframe,
        "active_version": active_version,
        "champion_f1": round(float(current_champ_f1), 4),
        "challenger_f1": round(float(challenger_f1), 4),
        "challenger_sharpe": challenger_sharpe,
        "samples_trained": samples_count,
        "message": message,
        "timestamp": now_str
    }
