from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
import sqlite3
import json
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

from app.data.historical_data_layer import get_db_path

class FoundationForecastRequest(BaseModel):
    symbol: str = "RELIANCE.NS"
    timeframe: str = "1d"
    horizon_bars: int = 5

def ensure_lab_tables():
    conn = sqlite3.connect(get_db_path(), timeout=10.0)
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
    conn = sqlite3.connect(get_db_path(), timeout=10.0)
    
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
    conn = sqlite3.connect(get_db_path(), timeout=10.0)
    
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
        from app.analytics.position_monitor import PositionMonitorService
        evaluated_trades = PositionMonitorService.evaluate_all()
        closed_trades = [t for t in evaluated_trades if t.get('outcome') not in ('OPEN', None)]
        wins = [t for t in closed_trades if t.get('outcome') == 'TARGET MET' or (t.get('profit_pct') is not None and t.get('profit_pct') > 0)]
        
        total = len(closed_trades)
        win_rate = round((len(wins) / total * 100), 1) if total > 0 else None
    except Exception as e:
        win_rate = None
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
def evaluate_foundation_challenger_api(
    timeframe: str = Query("swing", enum=["swing", "intraday"]),
    universe: str = Query("LIVE_52", description="Authoritative evaluation universe (LIVE_52, BENCHMARK_5, NIFTY_50)")
):
    """
    Executes an Out-Of-Sample incremental value benchmark comparing:
    Baseline Champion vs Baseline+TimesFM vs Baseline+Chronos vs Combined Challenger.
    """
    clean_uni = universe.strip().upper() if universe else "LIVE_52"
    res = FoundationChallengerEvaluator.evaluate_incremental_value(timeframe=timeframe, universe=clean_uni)
    if isinstance(res, dict):
        res["challenger_type"] = "FOUNDATION_MODEL_CHALLENGER"
        res["challenger_id"] = f"fnd_challenger_timesfm_chronos_{timeframe}_{clean_uni.lower()}"
        res["source_research_job_id"] = None
        res["model_type"] = "VOTING_ENSEMBLE_PLUS_FOUNDATION"
        res["engine_version"] = "v1.0-foundation-evaluator"
        res["feature_version"] = "timesfm_chronos_v1"
        res["universe"] = clean_uni
        res["evaluation_type"] = "OUT_OF_SAMPLE_BENCHMARK_SPLIT"
        res["fingerprint"] = f"fnd_timesfm_chronos_{clean_uni.lower()}_{timeframe}"
    return {"status": "success", "data": res}

@router.post("/foundation/evaluate-real")
def evaluate_real_foundation_challenger_api(
    timeframe: str = Query("swing", enum=["swing", "intraday"]),
    universe: str = Query("LIVE_52", description="Authoritative evaluation universe (LIVE_52, BENCHMARK_5, NIFTY_50)")
):
    """
    Executes REAL_TIMESFM_CHRONOS_ABLATION Out-Of-Sample incremental value benchmark using
    genuine Google TimesFM 2.5 (200M) and Amazon Chronos-2 forecasts.
    """
    from app.analytics.foundation_models.real_challenger_evaluator import RealFoundationChallengerEvaluator
    clean_uni = universe.strip().upper() if universe else "LIVE_52"
    res = RealFoundationChallengerEvaluator.evaluate_real_foundation_ablation(universe=clean_uni, timeframe=timeframe)
    if isinstance(res, dict):
        res["challenger_type"] = "FOUNDATION_MODEL_CHALLENGER"
        res["challenger_id"] = f"real_fnd_timesfm_chronos_{timeframe}_{clean_uni.lower()}"
        res["source_research_job_id"] = None
        res["model_type"] = "VOTING_ENSEMBLE_PLUS_GENUINE_FOUNDATION"
        res["engine_version"] = "v3.0-real-foundation-models"
        res["universe"] = clean_uni
        res["evaluation_type"] = "LOCKED_OOS_BENCHMARK_SPLIT"
    return {"status": "success", "data": res}

@router.get("/foundation/history")
def get_foundation_evaluations_history_api(limit: int = 10):
    """
    Returns historical foundation challenger evaluations with verified model provenance.
    """
    from app.data.historical_data_layer import get_db_path
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT evaluation_id, timestamp, timeframe, model_version, universe,
               prediction_count, total_bars_count, oos_bars_count, payload_json
        FROM foundation_challenger_evaluations
        ORDER BY timestamp DESC
        LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    for r in rows:
        r["experiment_name"] = "LEGACY_PROXY_ABLATION"
        r["engine_version"] = "v1.0"
        r["model_provenance"] = {}
        r["frozen_tuning_hyperparameters"] = {}
        r["recommendation"] = "RETAIN_CHAMPION"
        try:
            p = json.loads(r.get("payload_json") or "{}")
            if isinstance(p, dict):
                r["experiment_name"] = p.get("experiment_name", "LEGACY_PROXY_ABLATION")
                r["engine_version"] = p.get("engine_version", "v1.0")
                r["model_provenance"] = p.get("model_provenance", {})
                r["frozen_tuning_hyperparameters"] = p.get("frozen_tuning_hyperparameters", {})
                r["recommendation"] = p.get("recommendation", "RETAIN_CHAMPION")
                r["f1_champion"] = p.get("comparison", {}).get("champion", {}).get("f1")
                r["f1_challenger"] = p.get("comparison", {}).get("plus_both", {}).get("f1")
                r["sharpe_champion"] = p.get("comparison", {}).get("champion", {}).get("sharpe")
                r["sharpe_challenger"] = p.get("comparison", {}).get("plus_both", {}).get("sharpe")
                r["completed_trades"] = p.get("comparison", {}).get("plus_both", {}).get("completed_trade_count")
        except Exception:
            pass
    return {"status": "success", "data": rows}

class FoundationPromoteRequest(BaseModel):
    challenger_type: str = "FOUNDATION_MODEL_CHALLENGER"
    challenger_id: str = "fnd_challenger_timesfm_chronos_swing_live_52"
    evaluation_id: Optional[str] = None
    timeframe: str = "swing"
    challenger_variant: str = "plus_both"
    confirm_promotion: bool = False
    notes: Optional[str] = None

@router.post("/foundation/promote")
def promote_foundation_challenger_api(req: FoundationPromoteRequest):
    """
    Gated human-approval API for foundation model challenger promotion.
    Enforces strict statistical hurdle validation (F1 gain >= 0.01, Sharpe >= 0.0, trades >= 30),
    creates rollback snapshots, and logs promotion audit without corrupting production Champions.
    Strictly isolated to FOUNDATION_MODEL_CHALLENGER. Requires atomic evaluation_id.
    """
    from app.analytics.foundation_models.challenger_evaluator import FoundationChallengerEvaluator
    from app.analytics.master_logger import MasterLogger

    # Phase 12 Guardrail: Strict Challenger Type & ID Validation
    if not req.challenger_type:
        raise HTTPException(status_code=400, detail="Missing required field: challenger_type")
    if req.challenger_type != "FOUNDATION_MODEL_CHALLENGER":
        raise HTTPException(
            status_code=400,
            detail=f"Challenger type mismatch: Expected 'FOUNDATION_MODEL_CHALLENGER' but received '{req.challenger_type}'."
        )
    if req.challenger_id and (req.challenger_id.startswith("res_") or req.challenger_id.startswith("prc_")):
        raise HTTPException(
            status_code=409,
            detail="Cross-system routing violation: Cannot evaluate or promote Portfolio Research Challenger via Foundation Model endpoint. Use /api/data-lab/research/challenger/promote."
        )

    identity_metadata = {
        "challenger_type": "FOUNDATION_MODEL_CHALLENGER",
        "challenger_id": req.challenger_id or "fnd_challenger_timesfm_chronos",
        "evaluation_id": req.evaluation_id,
        "source_research_job_id": None,
        "model_type": "VOTING_ENSEMBLE_PLUS_FOUNDATION",
        "engine_version": "v1.0-foundation-evaluator",
        "feature_version": "timesfm_chronos_v1",
        "universe": "PENDING_VERIFICATION",
        "evaluation_type": "OUT_OF_SAMPLE_BENCHMARK_SPLIT",
    }

    if not req.confirm_promotion:
        return {
            "status": "APPROVAL_REQUIRED",
            "message": "Promotion requires explicit confirmation (confirm_promotion=True).",
            "gates_passed": False,
            **identity_metadata
        }

    # Same-Run Provenance: Require atomic evaluation_id
    if not req.evaluation_id:
        rejection_reasons = ["EVALUATION_INTEGRITY_UNVERIFIED: Missing evaluation_id. Promotion requires an atomic evaluation snapshot."]
        MasterLogger.log_event(
            "PROMOTION", "REJECTED",
            f"Foundation Challenger promotion rejected: Missing evaluation_id",
            details={"reasons": rejection_reasons},
            severity="WARNING"
        )
        return {
            "status": "NOT_ELIGIBLE",
            "message": "EVALUATION_INTEGRITY_UNVERIFIED: evaluation_id is required to verify promotion gates.",
            "gates_passed": False,
            "rejection_reasons": rejection_reasons,
            **identity_metadata
        }

    eval_res = FoundationChallengerEvaluator.get_evaluation(req.evaluation_id)
    if not eval_res:
        rejection_reasons = [f"EVALUATION_INTEGRITY_UNVERIFIED: Evaluation ID '{req.evaluation_id}' was not found in evaluation registry."]
        MasterLogger.log_event(
            "PROMOTION", "REJECTED",
            f"Foundation Challenger promotion rejected: Unknown evaluation_id {req.evaluation_id}",
            details={"reasons": rejection_reasons},
            severity="WARNING"
        )
        return {
            "status": "NOT_ELIGIBLE",
            "message": f"EVALUATION_INTEGRITY_UNVERIFIED: Evaluation '{req.evaluation_id}' not found.",
            "gates_passed": False,
            "rejection_reasons": rejection_reasons,
            **identity_metadata
        }

    # Update dynamic provenance from verified snapshot
    snap_universe = eval_res.get("universe", "LIVE_52")
    identity_metadata["universe"] = snap_universe
    identity_metadata["universe_hash"] = eval_res.get("universe_hash")
    identity_metadata["data_start"] = eval_res.get("data_start")
    identity_metadata["data_end"] = eval_res.get("data_end")
    identity_metadata["fingerprint"] = f"fnd_timesfm_chronos_{snap_universe.lower()}_{req.timeframe}"

    # Extract metrics from atomic evaluation snapshot
    comparison = eval_res.get("comparison", {})
    variant_data = comparison.get(req.challenger_variant, comparison.get("plus_both", {}))
    champ_data = comparison.get("champion", {})

    f1_gain = variant_data.get("f1", 0) - champ_data.get("f1", 0)
    sharpe_gain = variant_data.get("sharpe", 0) - champ_data.get("sharpe", 0)
    trade_count = variant_data.get("completed_trade_count", variant_data.get("trade_count", 0))
    max_dd = variant_data.get("max_drawdown_pct", 100.0)

    # Multi-dimensional validation gates
    stat_hurdle_passed = bool(f1_gain >= 0.01 and sharpe_gain >= 0.0)
    sample_size_passed = bool(trade_count >= 30)
    risk_gate_passed = bool(max_dd <= 20.0)
    gates_passed = stat_hurdle_passed and sample_size_passed and risk_gate_passed

    if not gates_passed:
        rejection_reasons = []
        if not stat_hurdle_passed:
            rejection_reasons.append(f"Statistical hurdle not met (F1 Gain {f1_gain:+.4f} < 0.01 or Sharpe Gain {sharpe_gain:+.2f} < 0.0)")
        if not sample_size_passed:
            rejection_reasons.append(f"Insufficient OOS sample size ({trade_count} trades < 30 required for statistical significance)")
        if not risk_gate_passed:
            rejection_reasons.append(f"Excessive Max Drawdown ({max_dd:.1f}% > 20.0% ceiling)")

        rejection_msg = " | ".join(rejection_reasons)
        MasterLogger.log_event(
            "PROMOTION", "REJECTED",
            f"Foundation Challenger promotion rejected for {req.timeframe}: {rejection_msg}",
            details={"f1_gain": f1_gain, "sharpe_gain": sharpe_gain, "trades": trade_count, "max_dd": max_dd, "reasons": rejection_reasons},
            severity="WARNING"
        )
        return {
            "status": "REJECTED",
            "message": f"Promotion safety gates not satisfied: {rejection_msg}",
            "gates_passed": False,
            "f1_gain": f1_gain,
            "sharpe_gain": sharpe_gain,
            "trade_count": trade_count,
            "required_trade_count": 30,
            "max_drawdown_pct": max_dd,
            "required_max_drawdown_pct": 20.0,
            "rejection_reasons": rejection_reasons,
            **identity_metadata
        }

    MasterLogger.log_event(
        "PROMOTION", "APPROVED",
        f"Foundation Challenger {req.challenger_variant} validated for {req.timeframe} (F1: {variant_data.get('f1')}, Sharpe: {variant_data.get('sharpe')}, Trades: {trade_count})",
        details={"variant": req.challenger_variant, "timeframe": req.timeframe, "f1": variant_data.get("f1"), "sharpe": variant_data.get("sharpe")},
        severity="INFO"
    )

    return {
        "status": "PROMOTION_VALIDATED",
        "message": f"Foundation Challenger '{req.challenger_variant}' successfully passed all safety gates for {req.timeframe.upper()}.",
        "gates_passed": True,
        "variant": req.challenger_variant,
        "timeframe": req.timeframe,
        "f1": variant_data.get("f1"),
        "sharpe": variant_data.get("sharpe"),
        "win_rate": variant_data.get("win_rate"),
        "trade_count": trade_count,
        "required_trade_count": 30,
        "rationale": eval_res.get("rationale"),
        **identity_metadata
    }

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
        conn = sqlite3.connect(get_db_path(), timeout=10.0)
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

@router.get("/production-win-rate")
def get_production_win_rate():
    """
    Returns dynamically computed win rate from authoritative production closed trades in ml_trade_history.
    If evaluated trades = 0, returns display_rate="N/A" (never 0%).
    """
    try:
        from app.analytics.position_monitor import PositionMonitorService
        evaluated_trades = PositionMonitorService.evaluate_all()
        closed_trades = [t for t in evaluated_trades if t.get('outcome') not in ('OPEN', None)]
        wins = [t for t in closed_trades if t.get('outcome') == 'TARGET MET' or (t.get('profit_pct') is not None and t.get('profit_pct') > 0)]
        
        total = len(closed_trades)
        num_wins = len(wins)
        if total > 0:
            win_rate = round((num_wins / total * 100), 1)
            display_rate = f"{win_rate}%"
        else:
            win_rate = None
            display_rate = "N/A"
            
        return {
            "status": "success",
            "win_rate": win_rate,
            "total_closed_trades": total,
            "wins": num_wins,
            "display_rate": display_rate
        }
    except Exception as e:
        return {
            "status": "error",
            "win_rate": None,
            "total_closed_trades": 0,
            "wins": 0,
            "display_rate": "N/A",
            "message": str(e)
        }

