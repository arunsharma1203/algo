from fastapi import APIRouter
import sqlite3

router = APIRouter()

def ensure_lab_tables():
    conn = sqlite3.connect('market_data.db')
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ml_feature_importance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            ticker TEXT,
            rsi REAL,
            macd REAL,
            macd_diff REAL,
            adx REAL,
            returns REAL
        )
    """)
    conn.commit()
    conn.close()

def save_feature_importance(ticker, importances, features):
    ensure_lab_tables()
    from datetime import datetime
    conn = sqlite3.connect('market_data.db')
    
    # map features
    val_map = {f: 0.0 for f in ['rsi', 'macd', 'macd_diff', 'adx', 'returns']}
    for i, f in enumerate(features):
        if f in val_map:
            val_map[f] = float(importances[i])
            
    conn.execute("""
        INSERT INTO ml_feature_importance (timestamp, ticker, rsi, macd, macd_diff, adx, returns)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now().isoformat(), ticker, val_map['rsi'], val_map['macd'], val_map['macd_diff'], val_map['adx'], val_map['returns']))
    
    conn.commit()
    conn.close()

@router.get("/lab-stats")
def get_lab_stats():
    ensure_lab_tables()
    conn = sqlite3.connect('market_data.db')
    
    # 1. Row counts per ticker in memory
    try:
        cur = conn.execute("SELECT ticker, COUNT(*) as count FROM ml_training_data GROUP BY ticker ORDER BY count DESC LIMIT 50")
        memory_stats = [{"ticker": row[0], "rows": row[1]} for row in cur.fetchall()]
    except:
        memory_stats = []
        
    # 2. Global Feature Importance (Average of last 100 runs)
    try:
        cur = conn.execute("""
            SELECT AVG(rsi), AVG(macd), AVG(macd_diff), AVG(adx), AVG(returns) 
            FROM (SELECT * FROM ml_feature_importance ORDER BY timestamp DESC LIMIT 100)
        """)
        row = cur.fetchone()
        if row and row[0] is not None:
            features = {
                "RSI": round(row[0] * 100, 1),
                "MACD": round(row[1] * 100, 1),
                "MACD_DIFF": round(row[2] * 100, 1),
                "ADX": round(row[3] * 100, 1),
                "RETURNS": round(row[4] * 100, 1),
            }
        else:
            features = {"RSI": 20, "MACD": 20, "MACD_DIFF": 20, "ADX": 20, "RETURNS": 20}
    except:
        features = {"RSI": 20, "MACD": 20, "MACD_DIFF": 20, "ADX": 20, "RETURNS": 20}
        
    # 3. Model Accuracy (Win Rate of past trades)
    try:
        from app.api.ml_history import evaluate_ml_history
        evaluated_trades = evaluate_ml_history()
        
        # Only count closed trades (not OPEN)
        closed_trades = [t for t in evaluated_trades if t['outcome'] != 'OPEN']
        wins = [t for t in closed_trades if t['outcome'] == 'TARGET MET']
        
        total = len(closed_trades)
        win_rate = round((len(wins) / total * 100), 1) if total > 0 else 0
    except Exception as e:
        print("Error evaluating accuracy:", e)
        win_rate = 0
        total = 0

    conn.close()
    
    from app.analytics.optuna_tuner import load_best_params
    from app.analytics.retrain_models import load_champion_metadata, get_retraining_history
    optuna_params = load_best_params()
    champion_meta = load_champion_metadata()
    retrain_history = get_retraining_history(limit=5)

    return {
        "status": "success",
        "memory_stats": memory_stats,
        "feature_importance": features,
        "win_rate": win_rate,
        "total_closed_trades": total,
        "optuna_params": optuna_params,
        "champion_meta": champion_meta,
        "retrain_history": retrain_history
    }

@router.post("/optuna/tune")
def trigger_optuna_tune(trials: int = 10):
    from app.analytics.optuna_tuner import run_optuna_tuning
    res = run_optuna_tuning(n_trials=trials)
    return {"status": "success", "data": res}

@router.get("/retraining/history")
def get_retrain_history_api(limit: int = 15):
    from app.analytics.retrain_models import get_retraining_history, load_champion_metadata
    return {
        "status": "success",
        "champion": load_champion_metadata(),
        "history": get_retraining_history(limit=limit)
    }

@router.post("/retraining/trigger")
def trigger_retraining_api():
    from app.analytics.retrain_models import execute_retraining_pipeline
    res = execute_retraining_pipeline()
    return {"status": "success", "data": res}

@router.get("/report/{ticker}")
def get_ml_report(ticker: str):
    import sqlite3
    import pandas as pd
    try:
        conn = sqlite3.connect('market_data.db')
        
        # Ensure columns exist so query doesn't crash on older DBs
        try:
            conn.execute("ALTER TABLE ml_training_data ADD COLUMN source TEXT DEFAULT 'yfinance'")
            conn.execute("ALTER TABLE ml_training_data ADD COLUMN hoard_timestamp TEXT")
        except Exception:
            pass
            
        query = f"""
            SELECT 
                datetime, close, rsi, macd, adx, returns, source, hoard_timestamp
            FROM ml_training_data 
            WHERE ticker = '{ticker}'
            ORDER BY datetime DESC
            LIMIT 100
        """
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        # Clean NaNs
        df = df.fillna('N/A')
        return {"status": "success", "data": df.to_dict(orient='records')}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e), "data": []}
