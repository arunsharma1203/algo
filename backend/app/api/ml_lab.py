from fastapi import APIRouter, Query
from pydantic import BaseModel
import sqlite3
from typing import Optional
from datetime import datetime

from app.analytics.optuna_tuner import load_best_params, run_optuna_tuning
from app.analytics.retrain_models import load_champion_metadata, get_retraining_history, execute_retraining_pipeline
from app.analytics.model_manager import ModelManager
from app.analytics.calibration import calibrator
from app.analytics.foundation_models.manager import foundation_model_manager
from app.analytics.foundation_models.challenger_evaluator import FoundationChallengerEvaluator

router = APIRouter()

class RollbackRequest(BaseModel):
    timeframe: str = "swing"
    target_version: Optional[str] = None

class FoundationForecastRequest(BaseModel):
    symbol: str = "RELIANCE.NS"
    timeframe: str = "1d"
    horizon_bars: int = 5

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
    conn = sqlite3.connect('market_data.db')
    
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
        closed_trades = [t for t in evaluated_trades if t.get('outcome') not in ('OPEN', None)]
        wins = [t for t in closed_trades if t.get('outcome') == 'TARGET MET' or (t.get('profit_pct') is not None and t.get('profit_pct') > 0)]
        
        total = len(closed_trades)
        win_rate = round((len(wins) / total * 100), 1) if total > 0 else 0
    except Exception as e:
        win_rate = 0
        total = 0

    conn.close()
    
    optuna_params_swing = load_best_params("swing")
    optuna_params_intra = load_best_params("intraday")
    champion_meta_swing = ModelManager.load_champion_metadata("swing")
    champion_meta_intra = ModelManager.load_champion_metadata("intraday")
    retrain_history = get_retraining_history(limit=10)

    calibration_info = {
        "is_fitted": calibrator.is_fitted,
        "method": calibrator.method,
        "brier_score": calibrator.brier_score
    }

    foundation_status = foundation_model_manager.get_status()

    return {
        "status": "success",
        "memory_stats": memory_stats,
        "feature_importance": features,
        "win_rate": win_rate,
        "total_closed_trades": total,
        "optuna_params": optuna_params_swing,
        "optuna_params_intraday": optuna_params_intra,
        "champion_meta": champion_meta_swing,
        "champion_meta_intraday": champion_meta_intra,
        "retrain_history": retrain_history,
        "calibration": calibration_info,
        "foundation_models": foundation_status
    }

@router.get("/foundation/status")
def get_foundation_status_api():
    """Returns runtime status and metadata for TimesFM 2.5 and Chronos-2."""
    return {
        "status": "success",
        "data": foundation_model_manager.get_status()
    }

@router.post("/foundation/evaluate")
def evaluate_foundation_challenger_api(timeframe: str = Query("swing", enum=["swing", "intraday"])):
    """
    Executes an Out-Of-Sample incremental value benchmark comparing:
    Baseline Champion vs Baseline+TimesFM vs Baseline+Chronos vs Combined Challenger.
    """
    res = FoundationChallengerEvaluator.evaluate_incremental_value(timeframe=timeframe)
    return {"status": "success", "data": res}

@router.post("/foundation/forecast")
def run_foundation_forecast_api(req: FoundationForecastRequest):
    """Generates point-in-time TimesFM & Chronos forecasts for a specific symbol."""
    import yfinance as yf
    try:
        clean_sym = req.symbol.strip().upper()
        if not clean_sym.endswith(('.NS', '.BO')):
            clean_sym = f"{clean_sym}.NS"

        period = "60d" if req.timeframe == "15m" else "2y"
        interval = req.timeframe
        df = yf.download(clean_sym, period=period, interval=interval, progress=False)

        if df.empty:
            return {"status": "error", "message": f"No market data found for {clean_sym}."}

        tfm, chr_res, feat = foundation_model_manager.generate_foundation_signals(
            symbol=clean_sym,
            historical_df=df,
            timeframe=req.timeframe,
            horizon_bars=req.horizon_bars,
            as_of_time=datetime.now()
        )

        return {
            "status": "success",
            "symbol": clean_sym,
            "timesfm": tfm.to_dict(),
            "chronos": chr_res.to_dict(),
            "agreement_features": feat.to_dict()
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/optuna/tune")
def trigger_optuna_tune(trials: int = 10, timeframe: str = Query("swing", enum=["swing", "intraday"])):
    res = run_optuna_tuning(n_trials=trials, timeframe=timeframe)
    return {"status": "success", "data": res}

@router.get("/retraining/history")
def get_retrain_history_api(timeframe: str = "swing", limit: int = 15):
    return {
        "status": "success",
        "champion": ModelManager.load_champion_metadata(timeframe),
        "history": get_retraining_history(timeframe=timeframe, limit=limit),
        "versions": ModelManager.get_version_history(timeframe)
    }

@router.post("/retraining/trigger")
def trigger_retraining_api(timeframe: str = Query("swing", enum=["swing", "intraday"])):
    res = execute_retraining_pipeline(timeframe=timeframe)
    return {"status": "success", "data": res}

@router.post("/retraining/rollback")
def rollback_model_api(req: RollbackRequest):
    try:
        meta = ModelManager.rollback_champion(timeframe=req.timeframe, target_version=req.target_version)
        return {"status": "success", "data": meta}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/autopilot/status")
def get_autopilot_status_api():
    """Returns runtime state of Autonomous Bot and Autopilot discovery sweeps."""
    from app.analytics.autonomous_bot import is_market_open
    from app.tasks.autopilot_scanner import is_autopilot_enabled
    from app.analytics.telegram_notifier import send_telegram_message, get_db_path

    now = datetime.now()
    market_open = is_market_open()
    enabled = is_autopilot_enabled()

    # Check open positions count
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM ml_trade_history WHERE status = 'OPEN'")
        open_count = cur.fetchone()[0]
    except Exception:
        open_count = 0

    # Check telegram configuration
    try:
        cur = conn.execute("SELECT value FROM app_settings WHERE key = 'telegram_bot_token'")
        tg_tok = cur.fetchone()
        tg_ok = bool(tg_tok and tg_tok[0] and str(tg_tok[0]).strip())
    except Exception:
        tg_ok = False
    conn.close()

    return {
        "status": "success",
        "market_open": market_open,
        "market_status_text": "OPEN (Trading Active)" if market_open else "CLOSED (Weekend / Off-Hours)",
        "current_time_ist": now.strftime("%H:%M:%S"),
        "day_of_week": now.strftime("%A"),
        "autopilot_enabled": enabled,
        "scheduled_sweeps": [
            {"time": "09:30 IST", "session": "Morning Momentum", "days": "Mon-Fri"},
            {"time": "11:30 IST", "session": "Mid-Day Continuation", "days": "Mon-Fri"},
            {"time": "13:30 IST", "session": "Afternoon Breakout", "days": "Mon-Fri"}
        ],
        "active_trade_tracker_interval": "Every 5 minutes",
        "open_trades_monitored": open_count,
        "telegram_configured": tg_ok
    }

@router.post("/telegram/test")
def test_telegram_api():
    """Sends a safe, clearly-labeled diagnostic push alert to verify Telegram bot connectivity."""
    from app.analytics.telegram_notifier import send_telegram_message
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = (
        f"🧪 <b>[SYSTEM DIAGNOSTIC TEST]</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>Telegram Push Engine:</b> CONNECTED & OPERATIONAL\n"
        f"⏰ <b>Timestamp:</b> {now_str} IST\n"
        f"🤖 <b>Bot:</b> Antigravity Autonomous Trading Notifier\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>All high-conviction trade calls and risk alerts will appear here in real time.</i>"
    )
    sent = send_telegram_message(msg)
    if sent:
        return {"status": "success", "message": "Telegram test message sent successfully."}
    else:
        return {"status": "error", "message": "Failed to send Telegram test message. Check bot credentials in Settings."}

@router.post("/autopilot/trigger")
def trigger_autopilot_sweep_api(session_name: str = "Manual Diagnostic Sweep"):
    """Manually triggers an Autopilot discovery sweep (overriding market-hours gate for diagnostics)."""
    from app.tasks.autopilot_scanner import run_scheduled_autopilot_sweep
    try:
        # Run sweep
        run_scheduled_autopilot_sweep(session_name=session_name)
        return {"status": "success", "message": f"Autopilot {session_name} sweep executed."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/report/{ticker}")
def get_ml_report(ticker: str):
    try:
        conn = sqlite3.connect('market_data.db')
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
        import pandas as pd
        df = pd.read_sql_query(query, conn)
        conn.close()
        df = df.fillna('N/A')
        return {"status": "success", "data": df.to_dict(orient='records')}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}
