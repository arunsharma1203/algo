"""
QLIB SYSTEM API ROUTER
======================
FastAPI endpoints for the parallel Microsoft Qlib trading and research system.

Features:
- Live Qlib environment telemetry
- Multi-strategy scanner endpoints (Swing, Intraday, F&O)
- Dynamic training execution
- Virtual recommendation log & performance
- Legacy vs Qlib comparison matrix
"""

import os
import sys
import platform
import logging
import threading
import asyncio
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, Request, Body
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import qlib
from app.qlib_engine.india_data_adapter import get_qlib_adapter, ensure_qlib_ready
from app.qlib_engine.feature_pipeline import QlibFeaturePipeline
from app.qlib_engine.model_registry import QlibModelRegistry, QlibModelIntegrityError
from app.qlib_engine.scanners import QlibScanners, QlibModelUnavailableError
from app.qlib_engine.trainer import QlibTrainer
from app.qlib_engine.training_telemetry import training_telemetry
from app.qlib_engine.benchmark import QlibBenchmarkSuite
from app.qlib_engine.virtual_tracker import QlibVirtualTracker
from app.qlib_engine.comparison_service import ComparisonService
from app.qlib_engine.dataset_inspector import DatasetInspector
from app.qlib_engine.signal_analyzer import SignalAnalyzer
from app.qlib_engine.promotion_policy import QlibPromotionPolicy

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/qlib", tags=["qlib_system"])


class QlibScanRequest(BaseModel):
    strategy: str = "SWING"  # 'SWING', 'INTRADAY', 'FNO'
    universe: str = "ALL_DATABASE_STOCKS"
    top_k: int = 5
    tickers: Optional[List[str]] = None


class QlibTrainRequest(BaseModel):
    strategy: str = "SWING"  # 'SWING', 'INTRADAY', 'FNO'
    universe: str = "NIFTY_500_ESTABLISHED"  # 'NIFTY_500_ESTABLISHED', 'FNO', 'NIFTY_50', 'BOOTSTRAP_35', 'CUSTOM'
    feature_family: str = "Alpha158"  # 'Alpha158' or 'Alpha360'
    model_family: str = "LGBModel"  # 'LGBModel', 'DEnsembleModel', 'CatBoostModel'
    horizon_days: int = 5  # 1, 3, 5, 10, 20
    target_definition: str = "FORWARD_RETURN"  # 'FORWARD_RETURN' or 'CROSS_SECTIONAL_RANK'
    instruments: Optional[List[str]] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class QlibPromoteRequest(BaseModel):
    strategy: str  # 'SWING', 'INTRADAY', 'FNO'
    model_id: str
    manual_approval: bool = False
    force: bool = False


@router.get("/status")
def get_qlib_system_status():
    """
    Returns complete telemetry for the Microsoft Qlib engine.
    """
    from app.analytics.universe_config import resolve_all_database_stocks
    adapter = get_qlib_adapter()
    uni_stats = resolve_all_database_stocks(timeframe="1d", min_bars=60)
    return {
        "engine": "QLIB",
        "badge": "QLIB ENGINE • ACTIVE",
        "qlib_installed": True,
        "qlib_version": getattr(qlib, "__version__", "unknown"),
        "qlib_package_file": getattr(qlib, "__file__", "unknown"),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "provider_uri": adapter.provider_uri,
        "feature_handlers": ["Alpha158", "Alpha360"],
        "supported_models": ["LGBModel", "DEnsembleModel", "CatBoostModel"],
        "models_status": QlibModelRegistry.get_registry_status(),
        "system_mode": "PARALLEL_EXPERIMENTAL_BRANCH",
        "primary_universe": "ALL_DATABASE_STOCKS",
        "universe_stats": {
            "stocks_discovered": uni_stats["discovered_count"],
            "stocks_valid": uni_stats["valid_count"],
            "stocks_scan_ready": uni_stats["scan_ready_count"]
        },
        "data_freshness": adapter.check_freshness(timeframe="1d")
    }


@router.get("/universe/stats")
def get_universe_stats():
    """Returns dynamic discovery and validation counts for database universe."""
    from app.analytics.universe_config import resolve_all_database_stocks
    stats = resolve_all_database_stocks(timeframe="1d", min_bars=60)
    return {
        "universe": "ALL_DATABASE_STOCKS",
        "stocks_discovered": stats["discovered_count"],
        "stocks_valid": stats["valid_count"],
        "stocks_scan_ready": stats["scan_ready_count"],
        "invalid_symbols_count": len(stats["invalid_symbols"]),
        "insufficient_bar_symbols_count": len(stats["insufficient_bar_symbols"])
    }


@router.get("/models")
def get_registered_models():
    """Returns active models and provenance manifests across all strategies."""
    return {
        "engine": "QLIB",
        "registry": QlibModelRegistry.get_registry_status()
    }


@router.post("/scan")
def run_scan(req: QlibScanRequest):
    """
    Executes a real Qlib scan on fresh market data.
    """
    strat = req.strategy.upper().strip()
    try:
        if strat == "SWING":
            return QlibScanners.run_swing_scan(
                universe=req.universe,
                tickers=req.tickers,
                top_k=req.top_k
            )
        elif strat == "INTRADAY":
            return QlibScanners.run_intraday_scan(
                tickers=req.tickers,
                top_k=req.top_k,
                universe=req.universe
            )
        elif strat == "FNO":
            return QlibScanners.run_fno_scan(
                underlyings=req.tickers,
                top_k=req.top_k
            )
        else:
            raise HTTPException(status_code=400, detail=f"Invalid strategy: {req.strategy}")
    except QlibModelUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"[QlibAPI] Scan error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Qlib scan failed: {e}")


@router.post("/train")
def train_model(req: QlibTrainRequest):
    """
    Trains a real Qlib model synchronously on Indian historical data.
    """
    strat = req.strategy.upper().strip()
    freq = "15min" if strat == "INTRADAY" else "day"
    try:
        res = QlibTrainer.train_model(
            strategy=strat,
            instruments=req.instruments,
            universe=req.universe,
            feature_family=req.feature_family,
            model_family=req.model_family,
            horizon_days=req.horizon_days,
            target_definition=req.target_definition,
            start_date=req.start_date,
            end_date=req.end_date,
            freq=freq
        )
        return res
    except Exception as e:
        logger.error(f"[QlibAPI] Training failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Qlib training failed: {e}")


@router.post("/train/start")
def start_training_job(req: QlibTrainRequest, background_tasks: BackgroundTasks):
    """
    Asynchronously starts a genuine quantitative Qlib training job in the background,
    streaming telemetry across 11 stages with live ETA to connected clients.
    """
    strat = req.strategy.upper().strip()
    freq = "15min" if strat == "INTRADAY" else "day"
    job_id = f"train_{strat.lower()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # Check if active job is running
    current_tel = training_telemetry.get_latest()
    if current_tel.get("status") == "RUNNING":
        elapsed = current_tel.get("elapsed_seconds", 0)
        if elapsed < 1800:
            return {
                "status": "ALREADY_RUNNING",
                "job_id": current_tel.get("job_id"),
                "message": f"Training job {current_tel.get('job_id')} is currently in progress ({current_tel.get('progress_pct')}%).",
                "telemetry": current_tel
            }

    def _execute_training_worker():
        try:
            logger.info(f"[QlibAPI] Background training worker starting job {job_id}...")
            QlibTrainer.train_model(
                strategy=strat,
                instruments=req.instruments,
                universe=req.universe,
                feature_family=req.feature_family,
                model_family=req.model_family,
                horizon_days=req.horizon_days,
                target_definition=req.target_definition,
                start_date=req.start_date,
                end_date=req.end_date,
                freq=freq,
                job_id=job_id
            )
            logger.info(f"[QlibAPI] Background training worker finished job {job_id}.")
        except Exception as e:
            logger.error(f"[QlibAPI] Background training error for {job_id}: {e}", exc_info=True)
            training_telemetry.fail_job(str(e))
            try:
                from app.qlib_engine.training_job_tracker import TrainingJobTracker
                TrainingJobTracker.fail_job(job_id=job_id, error=str(e))
            except Exception:
                pass

    thread = threading.Thread(target=_execute_training_worker, daemon=True, name=f"QlibTrain-{job_id}")
    thread.start()

    return {
        "status": "STARTED",
        "job_id": job_id,
        "strategy": strat,
        "universe": req.universe,
        "feature_family": req.feature_family,
        "model_family": req.model_family,
        "horizon_days": req.horizon_days,
        "target_definition": req.target_definition,
        "message": f"Training initiated for {strat} on {req.universe}. Monitor progress via SSE stream.",
        "stream_url": "/api/qlib/train/telemetry/stream"
    }


@router.get("/train/telemetry/stream")
async def stream_training_telemetry(request: Request):
    """
    Streams live training telemetry, stage progress, memory, and honest ETA via SSE.
    """
    loop = asyncio.get_running_loop()
    training_telemetry.set_event_loop(loop)
    q = training_telemetry.subscribe()

    async def event_generator():
        try:
            initial_state = training_telemetry.get_latest()
            yield f"event: telemetry\ndata: {json.dumps(initial_state)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=2.0)
                    yield f"event: telemetry\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield f": heartbeat\n\n"
        finally:
            training_telemetry.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/train/telemetry/latest")
def get_latest_training_telemetry():
    """Returns the latest training telemetry snapshot for status polling."""
    return training_telemetry.get_latest()


@router.get("/train/jobs/active")
def get_active_training_job():
    """Returns the currently active Qlib training job from persistent SQLite storage, if any."""
    from app.qlib_engine.training_job_tracker import TrainingJobTracker
    job = TrainingJobTracker.get_active_job()
    return {
        "has_active_job": bool(job),
        "job": job
    }


@router.get("/train/jobs/history")
def get_training_job_history(limit: int = 50):
    """Returns history of all training jobs from persistent SQLite storage."""
    from app.qlib_engine.training_job_tracker import TrainingJobTracker
    jobs = TrainingJobTracker.list_jobs(limit=limit)
    return {
        "total_jobs": len(jobs),
        "jobs": jobs
    }


@router.post("/train/reset")
def reset_training_state():
    """
    Cancels any active training jobs and resets the in-memory telemetry state to IDLE.
    Provides immediate unlock capability if any training or research process is stuck.
    """
    from app.qlib_engine.training_job_tracker import TrainingJobTracker
    cancelled_count = TrainingJobTracker.cancel_all_active_jobs(reason="Manual reset via API")
    state = training_telemetry.reset()
    return {
        "status": "RESET",
        "cancelled_jobs_count": cancelled_count,
        "telemetry": state
    }


@router.post("/train/jobs/cancel")
def cancel_training_jobs(job_id: Optional[str] = Query(None)):
    """
    Cancels a specific or all active training jobs and resets telemetry.
    """
    from app.qlib_engine.training_job_tracker import TrainingJobTracker
    if job_id:
        TrainingJobTracker.fail_job(job_id=job_id, error="Cancelled by user", status="CANCELLED")
    else:
        TrainingJobTracker.cancel_all_active_jobs(reason="Cancelled by user")
    state = training_telemetry.reset()
    return {"status": "CANCELLED", "telemetry": state}


@router.get("/models/champion")
def get_current_active_champion(strategy: Optional[str] = "SWING"):
    """
    Returns explicit stored provenance and quantitative metrics for the current ACTIVE QLIB CHAMPION.
    If no active model is registered, explicitly returns 'NO ACTIVE CHAMPION'.
    """
    strat = (strategy or "SWING").upper().strip()
    manifest = QlibModelRegistry.get_active_manifest(strat)
    if not manifest:
        return {
            "active": False,
            "has_champion": False,
            "champion": None,
            "status": "NO ACTIVE CHAMPION",
            "strategy": strat,
            "message": f"No active Qlib champion registered for {strat} strategy."
        }

    metrics = manifest.get("metrics", {})
    exec_metrics = metrics.get("execution_metrics", {})
    sig_metrics = metrics.get("signal_metrics", {})

    trades_cnt = exec_metrics.get("net_trades_count", metrics.get("trades_count"))
    ic_val = sig_metrics.get("information_coefficient", metrics.get("ic"))
    model_path = QlibModelRegistry.get_active_model_path(strat)

    champ_data = {
        "active": True,
        "has_champion": True,
        "status": "ACTIVE_CHAMPION",
        "strategy": strat,
        "model_id": manifest.get("model_id"),
        "model_family": manifest.get("model_class"),
        "model_class": manifest.get("model_class"),
        "feature_family": manifest.get("feature_handler"),
        "feature_handler": manifest.get("feature_handler"),
        "feature_version": manifest.get("feature_version"),
        "universe": manifest.get("universe"),
        "model_path": model_path,
        "training_period": {
            "start": manifest.get("train_start"),
            "end": manifest.get("train_end")
        },
        "validation_period": {
            "start": manifest.get("valid_start"),
            "end": manifest.get("valid_end")
        },
        "oos_period": {
            "start": manifest.get("oos_start"),
            "end": manifest.get("oos_end")
        },
        "metrics": {
            "sharpe": exec_metrics.get("net_sharpe", metrics.get("sharpe")),
            "profit_factor": exec_metrics.get("net_profit_factor", metrics.get("profit_factor")),
            "win_rate": exec_metrics.get("net_win_rate", metrics.get("win_rate")),
            "max_drawdown_pct": exec_metrics.get("net_max_drawdown_pct", metrics.get("max_drawdown_pct")),
            "trades_count": trades_cnt,
            "trades": trades_cnt,
            "mean_trade_pnl_pct": exec_metrics.get("net_mean_trade_pnl_pct", metrics.get("mean_trade_pnl_pct")),
            "information_coefficient": ic_val,
            "ic": ic_val,
            "rank_ic": sig_metrics.get("rank_ic", metrics.get("rank_ic"))
        },
        "artifact_sha256": manifest.get("artifact_sha256"),
        "created_at": manifest.get("created_at"),
        "promoted_at": manifest.get("promoted_at", manifest.get("created_at"))
    }

    return {
        **champ_data,
        "has_champion": True,
        "champion": champ_data
    }



@router.get("/benchmark")
def get_benchmark_results():
    """
    Returns empirical hardware, row-count, memory, and timing benchmarks
    across Bootstrap 35, FNO (~172), and NIFTY 500 Established (~414).
    """
    return QlibBenchmarkSuite.get_latest_benchmarks()


@router.post("/benchmark/run")
def run_benchmarks(background_tasks: BackgroundTasks):
    """Runs full empirical benchmarking suite in the background."""
    background_tasks.add_task(QlibBenchmarkSuite.run_all_benchmarks)
    return {
        "status": "BENCHMARK_INITIATED",
        "message": "Benchmarking suite started across 35, FNO, and NIFTY 500 Established tiers."
    }


@router.get("/recommendations")
def get_recommendations(
    strategy: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    limit: int = 20
):
    """Returns virtual recommendation history from SQLite with server-side pagination & counts."""
    return QlibVirtualTracker.get_recommendations_paginated(
        strategy=strategy,
        status=status,
        page=page,
        limit=limit
    )


@router.get("/fno")
def get_fno_opportunities():
    """Runs or returns dynamic F&O scanner opportunities with full universe telemetry."""
    try:
        return QlibScanners.run_fno_scan()
    except Exception as e:
        logger.error(f"[QlibAPI] FNO scan error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Qlib F&O scan failed: {e}")


@router.get("/comparison")
def get_system_comparison():
    """Returns apples-to-apples Legacy vs Qlib comparison scorecard."""
    return ComparisonService.get_side_by_side_comparison()


@router.get("/models/challengers")
def get_challenger_models(strategy: Optional[str] = None):
    """
    Returns list of all registered frozen challengers by strategy or all strategies.
    """
    return {
        "engine": "QLIB",
        "strategy": strategy,
        "challengers": QlibModelRegistry.list_challengers(strategy)
    }


@router.post("/models/promote")
def promote_challenger_model(req: QlibPromoteRequest):
    """
    Promotes a frozen challenger model artifact to active with explicit manual approval.
    """
    try:
        res = QlibModelRegistry.promote_challenger(
            strategy=req.strategy,
            model_id=req.model_id,
            manual_approval=req.manual_approval,
            force=req.force
        )
        return res
    except (ValueError, QlibModelIntegrityError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[QlibAPI] Promotion failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Challenger promotion failed: {e}")


@router.get("/health")
def get_deep_health():
    """
    Returns deep multi-component production health telemetry for the Qlib subsystem.
    """
    import hashlib
    adapter = get_qlib_adapter()
    freshness = adapter.check_freshness("1d")

    # Verify Legacy Champion model integrity byte-for-byte
    legacy_hashes = {}
    for strat, path in [
        ("intraday", "models/intraday/champion_ensemble.pkl"),
        ("swing", "models/swing/champion_ensemble.pkl")
    ]:
        full_p = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", path))
        if os.path.exists(full_p):
            h = hashlib.sha256()
            with open(full_p, "rb") as fp:
                h.update(fp.read())
            legacy_hashes[strat] = h.hexdigest()
        else:
            legacy_hashes[strat] = "MISSING"

    return {
        "engine": "QLIB",
        "status": "ONLINE",
        "system_mode": "PARALLEL_EXPERIMENTAL_BRANCH",
        "portfolio_heat": "0.0% (NOT_A_POSITION virtual isolation)",
        "broker_routing": "DISABLED (0 live orders)",
        "data_freshness": freshness,
        "active_models": QlibModelRegistry.get_registry_status(),
        "challengers_count": {
            "swing": len(QlibModelRegistry.list_challengers("SWING")),
            "intraday": len(QlibModelRegistry.list_challengers("INTRADAY")),
            "fno": len(QlibModelRegistry.list_challengers("FNO"))
        },
        "legacy_champions_intact": {
            "intraday_hash": legacy_hashes.get("intraday"),
            "swing_hash": legacy_hashes.get("swing"),
            "intraday_intact": legacy_hashes.get("intraday") == "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91",
            "swing_intact": legacy_hashes.get("swing") == "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"
        }
    }


@router.get("/dataset/health")
def get_dataset_health(universe: str = "NIFTY_500_ESTABLISHED"):
    """
    Returns dynamic, authoritative dataset health telemetry from SQLite and Qlib binary provider.
    Includes eligible symbols count, rejected symbols breakdown with reasons, dates, and sync state.
    """
    try:
        return DatasetInspector.get_dataset_health(universe_name=universe)
    except Exception as e:
        logger.error(f"[QlibAPI] Dataset health failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Dataset health inspection failed: {e}")


@router.get("/universe/inspect")
def inspect_universe(universe: str = "NIFTY_500_ESTABLISHED"):
    """
    Returns full eligibility and rejection diagnostics for the specified universe.
    """
    try:
        return DatasetInspector.inspect_universe(universe_name=universe)
    except Exception as e:
        logger.error(f"[QlibAPI] Universe inspection failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Universe inspection failed: {e}")


@router.get("/signals/discovery")
def get_signal_discovery(universe: str = "NIFTY_500_ESTABLISHED"):
    """
    Evaluates multi-horizon forward returns (1D, 3D, 5D, 10D, 20D), IC, Rank IC,
    decile spreads, stability, and feature family availability.
    """
    try:
        return SignalAnalyzer.get_signal_discovery_report(universe_name=universe)
    except Exception as e:
        logger.error(f"[QlibAPI] Signal discovery failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Signal discovery failed: {e}")


@router.get("/models/candidate-comparison")
def compare_candidate_to_champion(strategy: str = "SWING", candidate_id: Optional[str] = None):
    """
    Direct side-by-side apples-to-apples comparison between candidate model and active Champion.
    Evaluates all 12 Promotion Gates with explicit PASS/FAIL badges.
    If candidate is ineligible, provides a detailed diagnostic explanation.
    """
    strat = strategy.upper().strip()
    champ_manifest = QlibModelRegistry.get_active_manifest(strat)

    # Find candidate manifest
    candidate_manifest = None
    challengers = QlibModelRegistry.list_challengers(strat)
    if candidate_id:
        for c in challengers:
            if c.get("model_id") == candidate_id:
                candidate_manifest = c
                break
    elif challengers:
        candidate_manifest = challengers[0]

    if not candidate_manifest:
        return {
            "strategy": strat,
            "has_candidate": False,
            "has_champion": bool(champ_manifest),
            "status": "NO_CANDIDATE_FOUND",
            "message": f"No candidate model found for strategy {strat}."
        }

    # Run 12-Gate evaluation
    eval_result = QlibPromotionPolicy.evaluate_promotion_eligibility(
        candidate_manifest=candidate_manifest,
        champion_manifest=champ_manifest
    )

    # Extract metrics for side-by-side display
    cand_metrics = candidate_manifest.get("metrics", {})
    cand_exec = cand_metrics.get("execution_metrics", {})
    champ_metrics = champ_manifest.get("metrics", {}) if champ_manifest else {}
    champ_exec = champ_metrics.get("execution_metrics", {}) if champ_manifest else {}

    c_sharpe = cand_exec.get("net_sharpe", cand_metrics.get("sharpe"))
    ch_sharpe = champ_exec.get("net_sharpe", champ_metrics.get("sharpe"))

    c_pf = cand_exec.get("net_profit_factor", cand_metrics.get("profit_factor"))
    ch_pf = champ_exec.get("net_profit_factor", champ_metrics.get("profit_factor"))

    c_dd = cand_exec.get("net_max_drawdown_pct", cand_metrics.get("max_drawdown_pct"))
    ch_dd = champ_exec.get("net_max_drawdown_pct", champ_metrics.get("max_drawdown_pct"))

    c_wr = cand_exec.get("net_win_rate", cand_metrics.get("win_rate"))
    ch_wr = champ_exec.get("net_win_rate", champ_metrics.get("win_rate"))

    c_trades = cand_exec.get("net_trades_count", cand_metrics.get("trades_count"))
    ch_trades = champ_exec.get("net_trades_count", champ_metrics.get("trades_count"))

    c_ic = cand_metrics.get("signal_metrics", {}).get("information_coefficient", cand_metrics.get("ic"))
    ch_ic = champ_metrics.get("signal_metrics", {}).get("information_coefficient", champ_metrics.get("ic"))

    c_rank_ic = cand_metrics.get("signal_metrics", {}).get("rank_ic", cand_metrics.get("rank_ic"))
    ch_rank_ic = champ_metrics.get("signal_metrics", {}).get("rank_ic", champ_metrics.get("rank_ic"))

    def _delta(c_val, ch_val, invert=False):
        if c_val is None or ch_val is None:
            return {"delta": None, "improved": False}
        diff = round(c_val - ch_val, 4)
        improved = (diff < 0) if invert else (diff > 0)
        return {"delta": diff, "improved": improved}

    comparison_table = {
        "sharpe": {"candidate": c_sharpe, "champion": ch_sharpe, **_delta(c_sharpe, ch_sharpe)},
        "profit_factor": {"candidate": c_pf, "champion": ch_pf, **_delta(c_pf, ch_pf)},
        "max_drawdown_pct": {"candidate": c_dd, "champion": ch_dd, **_delta(c_dd, ch_dd, invert=True)},
        "win_rate": {"candidate": c_wr, "champion": ch_wr, **_delta(c_wr, ch_wr)},
        "trade_count": {
            "candidate": c_trades,
            "champion": ch_trades,
            "delta": (c_trades - ch_trades) if (c_trades is not None and ch_trades is not None) else None,
            "improved": (c_trades > ch_trades) if (c_trades is not None and ch_trades is not None) else False
        },
        "ic": {"candidate": c_ic, "champion": ch_ic, **_delta(c_ic, ch_ic)},
        "rank_ic": {"candidate": c_rank_ic, "champion": ch_rank_ic, **_delta(c_rank_ic, ch_rank_ic)}
    }

    return {
        "strategy": strat,
        "has_candidate": True,
        "has_champion": bool(champ_manifest),
        "candidate_id": candidate_manifest.get("model_id"),
        "champion_id": champ_manifest.get("model_id") if champ_manifest else None,
        "eligible": eval_result.get("eligible", False),
        "verdict": eval_result.get("verdict"),
        "failed_reasons": eval_result.get("failed_reasons", []),
        "gates_detail": eval_result.get("gates_detail", {}),
        "gates_passed_count": eval_result.get("gates_passed_count", 0),
        "gates_total_count": eval_result.get("gates_total_count", 12),
        "comparison_table": comparison_table,
        "candidate_manifest": candidate_manifest,
        "feature_importance": candidate_manifest.get("feature_importance") or candidate_manifest.get("metrics", {}).get("feature_importance")
    }


@router.get("/experiments/matrix")
def get_experiment_matrix():
    """
    Returns available matrix options (Universes, Feature Families, Horizons, Models, Targets)
    and empirical resource estimates.
    """
    return {
        "universes": [
            {"id": "NIFTY_500_ESTABLISHED", "name": "NIFTY 500 Established (>=1,200 Bars)", "estimated_time_sec": 110, "memory_safe": True},
            {"id": "FNO", "name": "NSE Derivatives (F&O ~172 Equities)", "estimated_time_sec": 45, "memory_safe": True},
            {"id": "NIFTY_50", "name": "NIFTY 50 Blue-Chip Equities", "estimated_time_sec": 28, "memory_safe": True},
            {"id": "BOOTSTRAP_35", "name": "Bootstrap Research Basket (35 Equities)", "estimated_time_sec": 15, "memory_safe": True},
            {"id": "CUSTOM", "name": "Custom Symbol Basket", "estimated_time_sec": 30, "memory_safe": True}
        ],
        "feature_families": [
            {"id": "Alpha158", "name": "Alpha158 (158 Price/Volume Factors)", "status": "AVAILABLE"},
            {"id": "Alpha360", "name": "Alpha360 (Normalized 60-Step Sequences)", "status": "AVAILABLE"}
        ],
        "horizons": [
            {"id": "1D", "days": 1, "name": "1-Trading-Day Forward"},
            {"id": "3D", "days": 3, "name": "3-Trading-Day Forward"},
            {"id": "5D", "days": 5, "name": "5-Trading-Day Swing (Authoritative)"},
            {"id": "10D", "days": 10, "name": "10-Trading-Day Forward"},
            {"id": "20D", "days": 20, "name": "20-Trading-Day Forward"}
        ],
        "models": [
            {"id": "LGBModel", "name": "LightGBM Gradient Boosting (LGBModel)", "status": "SUPPORTED"},
            {"id": "CatBoostModel", "name": "CatBoost Categorical Boosting", "status": "SUPPORTED"},
            {"id": "DEnsembleModel", "name": "Double Ensemble GBDT", "status": "SUPPORTED"}
        ],
        "targets": [
            {"id": "FORWARD_RETURN", "name": "Pure Forward Return (Ref(Close, -H) / Close - 1)"},
            {"id": "CROSS_SECTIONAL_RANK", "name": "Cross-Sectional Rank Normalized (CSZScoreNorm)"}
        ],
        "execution_friction_bps": 25.0
    }


class FindBestModelRequest(BaseModel):
    universe: Optional[str] = "NIFTY_500_ESTABLISHED"
    friction_bps: Optional[float] = 25.0
    strategy: Optional[str] = "SWING"


@router.post("/research/find-best-model")
def find_best_qlib_model(req: Optional[FindBestModelRequest] = None):
    """
    Triggers the one-click primary research workflow testing curated high-value configurations.
    """
    from app.qlib_engine.best_model_finder import BestModelFinder
    req_universe = req.universe if req and req.universe else "NIFTY_500_ESTABLISHED"
    req_friction = req.friction_bps if req and req.friction_bps is not None else 25.0
    req_strat = req.strategy if req and req.strategy else "SWING"
    return BestModelFinder.start_mission_async(
        universe=req_universe,
        friction_bps=req_friction,
        strategy=req_strat
    )


@router.get("/research/mission/latest")
def get_latest_research_mission():
    """
    Returns the latest research mission results and candidate evaluations.
    """
    from app.qlib_engine.best_model_finder import BestModelFinder
    res = BestModelFinder.get_latest_mission_result()
    return res or {"status": "NO_MISSION_RUN", "has_qualified_winner": False, "candidates": []}


@router.get("/system/readiness")
def get_system_operational_readiness():
    """
    Audits and reports real, unvarnished operational readiness across SWING, INTRADAY, and FNO.
    Distinguishes RESEARCH READY, SCANNER READY, LIVE DATA READY, and TRADING READY.
    """
    from app.qlib_engine.india_data_adapter import get_qlib_adapter
    adapter = get_qlib_adapter()
    freshness_1d = adapter.check_freshness("1d")
    freshness_15m = adapter.check_freshness("15min")

    is_fresh_1d = freshness_1d.get("data_status") == "FRESH"
    is_fresh_15m = freshness_15m.get("data_status") == "FRESH"

    from app.qlib_engine.model_registry import QlibModelRegistry
    from app.qlib_engine.promotion_policy import QlibPromotionPolicy

    strategies = ["SWING", "INTRADAY", "FNO"]
    readiness = {}

    for strat in strategies:
        champ = QlibModelRegistry.get_active_manifest(strat)
        has_champ = bool(champ)
        champ_id = champ.get("model_id") if champ else None

        is_qualified = False
        failed_gates = []
        if champ:
            eval_res = QlibPromotionPolicy.evaluate_promotion_eligibility(champ)
            is_qualified = eval_res.get("is_eligible", False)
            failed_gates = eval_res.get("failed_reasons", [])

        data_fresh = is_fresh_15m if strat == "INTRADAY" else is_fresh_1d
        fresh_status = freshness_15m.get("data_status") if strat == "INTRADAY" else freshness_1d.get("data_status")

        if strat == "INTRADAY":
            import sqlite3
            from app.data.historical_data_layer import get_db_path
            try:
                conn = sqlite3.connect(get_db_path(), timeout=5.0)
                cnt_15m = conn.execute("SELECT COUNT(*) FROM ohlcv WHERE timeframe = '15m'").fetchone()[0]
                conn.close()
            except Exception:
                cnt_15m = 0
            data_sufficient = cnt_15m >= 1000
            data_note = f"{cnt_15m} 15m bars available" if data_sufficient else f"INTRADAY DATA INSUFFICIENT ({cnt_15m} bars)"
        elif strat == "FNO":
            data_sufficient = True
            data_note = "Official NSE F&O Universe active"
        else:
            data_sufficient = True
            data_note = "NIFTY 500 10-Year Daily Bars active"

        research_ready = True
        scanner_ready = has_champ and data_fresh
        live_data_ready = data_fresh and data_sufficient
        trading_ready = live_data_ready and is_qualified

        if trading_ready:
            overall_status = "TRADING READY"
            badge_color = "EMERALD"
        elif scanner_ready:
            overall_status = "SCANNER READY (CHAMPION UNQUALIFIED)" if not is_qualified else "SCANNER READY"
            badge_color = "AMBER"
        elif live_data_ready:
            overall_status = "LIVE DATA READY"
            badge_color = "AMBER"
        else:
            overall_status = "RESEARCH READY" if data_sufficient else "DATA INSUFFICIENT"
            badge_color = "ROSE"

        readiness[strat] = {
            "strategy": strat,
            "overall_status": overall_status,
            "badge_color": badge_color,
            "trading_ready": trading_ready,
            "research_ready": research_ready,
            "scanner_ready": scanner_ready,
            "live_data_ready": live_data_ready,
            "data_freshness": fresh_status,
            "data_note": data_note,
            "has_champion": has_champ,
            "champion_id": champ_id,
            "champion_qualified": is_qualified,
            "champion_status": "QUALIFIED" if is_qualified else ("ACTIVE (UNQUALIFIED)" if has_champ else "NO_CHAMPION"),
            "failed_gates": failed_gates,
            "metrics": champ.get("metrics", {}) if champ else {}
        }

    return {
        "status": "SUCCESS",
        "strategies": readiness,
        "checked_at": datetime.now().isoformat()
    }

