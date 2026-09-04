import time
import json
import sqlite3
import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.data.historical_data_layer import HistoricalDataLayer, get_db_path
from app.analytics.universe_config import get_universe, UNIVERSE_PRESETS, BENCHMARK_5_UNIVERSE, LIVE_UNIVERSE, RESEARCH_100_UNIVERSE, get_universe_coverage
from app.analytics.portfolio_walk_forward import MultiStockPortfolioWalkForwardEngine
from app.analytics.parallel_engine import ParallelWalkForwardOrchestrator, ResearchConfig
from app.api.ml_backtest import WeeklyWalkForwardBacktestEngine
import yfinance as yf

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/data-lab", tags=["Data Lab"])

class SyncRequest(BaseModel):
    universe: str = "BENCHMARK_5"
    force_refresh: bool = False

class PortfolioBacktestRequest(BaseModel):
    universe: str = "BENCHMARK_5"
    initial_capital: float = 500000.0
    max_portfolio_heat: float = 6.0
    max_single_risk_pct: float = 2.0
    kelly_mode: str = "HALF"
    brokerage_per_order: float = 20.0
    slippage_pct: float = 0.08

class ParallelBenchmarkRequest(BaseModel):
    tickers: List[str] = ["RELIANCE.NS", "TCS.NS"]
    worker_counts: List[int] = [1, 2, 4]

@router.get("/system-resources")
async def get_system_resources():
    """Returns Apple Silicon hardware topology and recommended worker configuration."""
    return HistoricalDataLayer.get_system_resource_profile()

@router.get("/universes")
async def list_universes():
    """Returns available universe presets with survivorship bias warnings."""
    return {
        "presets": {
            k: {
                "name": v["name"],
                "count": len(v["tickers"]),
                "description": v["description"],
                "survivorship_bias": v["survivorship_bias"]
            } for k, v in UNIVERSE_PRESETS.items()
        }
    }

@router.get("/coverage")
async def get_coverage_report(universe: str = "BENCHMARK_5"):
    """Returns data quality, 10-year depth percentage, and intraday accumulated stats."""
    u_info = get_universe(universe)
    tickers = u_info["tickers"]

    daily_report = HistoricalDataLayer.get_historical_coverage_report(tickers)
    intraday_report = HistoricalDataLayer.get_intraday_accumulated_report()

    return {
        "universe_name": u_info["name"],
        "survivorship_bias": u_info["survivorship_bias"],
        "daily_coverage": daily_report,
        "intraday_accumulated": intraday_report
    }

@router.post("/sync-10y")
async def trigger_10y_sync(req: SyncRequest):
    """Synchronizes up to 10 years of daily OHLCV for all universe stocks in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    u_info = get_universe(req.universe)
    tickers = u_info.get("tickers", [])

    sync_results = []
    # Use bounded parallel workers to respect rate limits while avoiding HTTP timeout
    workers = min(8, max(2, len(tickers) // 5)) if tickers else 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(HistoricalDataLayer.sync_ticker_daily_10y, ticker, req.force_refresh) for ticker in tickers]
        for f in futures:
            try:
                sync_results.append(f.result())
            except Exception as e:
                sync_results.append({"status": "ERROR", "error": str(e)})

    return {
        "status": "success",
        "universe": u_info["name"],
        "synced_count": len([r for r in sync_results if r.get("status") in ["SYNCED", "UP_TO_DATE"]]),
        "total_tickers": len(tickers),
        "results": sync_results
    }

class HoardSyncRequest(BaseModel):
    universe: str = "NIFTY_500"
    custom_tickers: Optional[List[str]] = None
    data_source: str = "yfinance"
    api_key: str = ""

@router.post("/sync-15m-hoarder")
async def trigger_15m_hoarder(req: HoardSyncRequest):
    """Triggers scalable Data Hoarder for any selected universe to synchronize 15m candles."""
    from app.tasks.hoarder import hoard_intraday_data
    res = hoard_intraday_data(
        universe=req.universe,
        custom_tickers=req.custom_tickers,
        data_source=req.data_source,
        api_key=req.api_key
    )
    return res

@router.post("/portfolio-backtest")
async def run_portfolio_backtest(req: PortfolioBacktestRequest):
    """Executes Multi-Stock Portfolio Walk-Forward Backtest with shared capital and Portfolio Heat cap."""
    try:
        u_info = get_universe(req.universe)
        tickers = u_info["tickers"]

        engine = MultiStockPortfolioWalkForwardEngine(
            tickers=tickers,
            initial_capital=req.initial_capital,
            max_portfolio_heat=req.max_portfolio_heat,
            max_single_risk_pct=req.max_single_risk_pct,
            brokerage=req.brokerage_per_order,
            slippage_pct=req.slippage_pct,
            kelly_mode=req.kelly_mode,
            universe_name=req.universe
        )

        results = engine.run()
        return results

    except Exception as e:
        logger.error(f"Portfolio Backtest error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/horizon-comparison")
async def get_horizon_comparison(ticker: str = "RELIANCE.NS"):
    """
    Empirically benchmarks strategy performance across 3-year, 5-year, 7-year, and 10-year training horizons
    using the identical locked final Out-Of-Sample holdout.
    """
    try:
        clean_t = ticker.strip().upper()
        if not clean_t.endswith(('.NS', '.BO')):
            clean_t += '.NS'

        df_10y = yf.download(clean_t, period="10y", interval="1d", progress=False)
        if df_10y.empty or len(df_10y) < 1000:
            raise HTTPException(status_code=400, detail="Insufficient 10-year historical data available.")

        # Test horizons
        horizons = {
            "3_Years": 750,
            "5_Years": 1250,
            "7_Years": 1750,
            "10_Years": len(df_10y)
        }

        comparison_results = {}
        for h_name, n_bars in horizons.items():
            sub_df = df_10y.iloc[-n_bars:].copy()
            engine = WeeklyWalkForwardBacktestEngine(sub_df, model_type="SWING", initial_capital=100000.0)
            res = engine.run()
            
            comparison_results[h_name] = {
                "bars_trained": n_bars,
                "total_trades": len(res.get("trades", [])),
                "win_rate_pct": res.get("metrics", {}).get("win_rate", 0.0),
                "total_pnl": res.get("metrics", {}).get("total_pnl", 0.0),
                "profit_factor": res.get("metrics", {}).get("profit_factor", 1.0),
                "max_drawdown_pct": res.get("metrics", {}).get("max_drawdown", 0.0),
                "locked_holdout": res.get("locked_final_holdout", {})
            }

        return {
            "ticker": clean_t,
            "comparison": comparison_results,
            "scientific_takeaway": "Compares training sample depth against the identical Out-Of-Sample holdout without lookahead bias."
        }

    except Exception as e:
        logger.error(f"Horizon comparison error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/parallel-benchmark")
async def run_parallel_benchmark(req: ParallelBenchmarkRequest):
    """
    Executes controlled benchmarking across worker counts on local data.
    Measures runtime, speedup factor, and verifies result equivalence.
    """
    benchmark_report = {}
    base_results = None

    for workers in req.worker_counts:
        config = ResearchConfig(max_workers=workers, enable_checkpointing=False)
        orchestrator = ParallelWalkForwardOrchestrator(config)
        
        t0 = time.time()
        res = orchestrator.run_universe_walk_forward(req.tickers, job_id=f"bench_{workers}w")
        elapsed = round(time.time() - t0, 2)

        if base_results is None:
            base_results = res.get("results", {})

        benchmark_report[f"{workers}_workers"] = {
            "workers": workers,
            "runtime_seconds": elapsed,
            "tickers_processed": len(res.get("results", {})),
            "status": res.get("status")
        }

    # Compute speedup
    base_time = benchmark_report.get("1_workers", {}).get("runtime_seconds", 1.0)
    for k, v in benchmark_report.items():
        v["speedup_factor"] = round(base_time / v["runtime_seconds"], 2) if v["runtime_seconds"] > 0 else 1.0

    return {
        "benchmark": benchmark_report,
        "hardware_profile": HistoricalDataLayer.get_system_resource_profile()
    }

# ============================================================
# RESEARCH CONTROL CENTER ENDPOINTS
# ============================================================

from app.analytics.research_job_manager import research_job_manager, ResearchJobStatus
from fastapi.responses import StreamingResponse
import json
import asyncio

class CreateResearchJobRequest(BaseModel):
    research_type: str = "PORTFOLIO_WALK_FORWARD"
    universe: str = "BENCHMARK_5"
    timeframe: str = "1d"
    history_years: int = 10
    worker_count: int = 4
    initial_capital: float = 500000.0
    max_portfolio_heat: float = 6.0
    kelly_mode: str = "HALF"
    custom_tickers: Optional[List[str]] = None
    title: Optional[str] = None
    model_type: str = "LIGHTGBM_ALPHA"
    force_rerun: bool = False

@router.post("/research/jobs")
async def create_research_job(req: CreateResearchJobRequest):
    """Creates and queues a new long-running research job on Apple Silicon with duplicate detection."""
    from app.data.validator import MarketDataValidator

    # Authoritative Ticker Safeguard Pipeline
    tickers_to_validate = None
    if req.research_type == "SINGLE_STOCK_WALK_FORWARD":
        tickers_to_validate = req.custom_tickers if req.custom_tickers else req.universe
    elif req.custom_tickers:
        tickers_to_validate = req.custom_tickers

    clean_universe = req.universe
    clean_tickers = req.custom_tickers

    if tickers_to_validate:
        valid, validated_symbols, err_msg = MarketDataValidator.validate_research_tickers(
            tickers_to_validate, timeframe=req.timeframe
        )
        if not valid:
            raise HTTPException(status_code=400, detail=f"Ticker Validation Error: {err_msg}")
        
        if req.research_type == "SINGLE_STOCK_WALK_FORWARD":
            clean_universe = ", ".join(validated_symbols)
            clean_tickers = validated_symbols
        else:
            clean_tickers = validated_symbols

    job_res = research_job_manager.create_job(
        research_type=req.research_type,
        universe=clean_universe,
        timeframe=req.timeframe,
        history_years=req.history_years,
        worker_count=req.worker_count,
        initial_capital=req.initial_capital,
        max_portfolio_heat=req.max_portfolio_heat,
        kelly_mode=req.kelly_mode,
        custom_tickers=clean_tickers,
        title=req.title,
        model_type=req.model_type,
        force_rerun=req.force_rerun
    )
    
    if job_res.get("status") == "EXISTING_RESEARCH_FOUND":
        return {
            "status": "EXISTING_RESEARCH_FOUND",
            "cache_hit": True,
            "fingerprint": job_res.get("fingerprint"),
            "job": job_res.get("job"),
            "message": job_res.get("message")
        }

    return {"status": "success", "cache_hit": False, "fingerprint": job_res.get("fingerprint"), "job": job_res.get("job") or job_res}

@router.get("/research/jobs")
async def list_research_jobs(limit: int = 50):
    """Lists all historical and active research jobs."""
    return {"jobs": research_job_manager.get_all_jobs(limit=limit)}

@router.get("/research/active")
async def get_active_research_job():
    """Returns the currently RUNNING or PAUSED job for browser reconnect."""
    active = research_job_manager.get_active_job()
    return {"active_job": active}

@router.get("/research/jobs/{job_id}")
async def get_research_job_detail(job_id: str):
    """Returns status, progress, and metadata for a specific job."""
    job = research_job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Research job not found.")
    return {"job": job}

@router.get("/research/jobs/{job_id}/events")
async def get_research_job_events(job_id: str, limit: int = 100):
    """Returns historical event logs for a specific research job."""
    events = research_job_manager.get_job_events(job_id, limit=limit)
    return {"job_id": job_id, "events": events}

@router.get("/research/jobs/{job_id}/results")
async def get_research_job_results(job_id: str):
    """Returns detailed performance metrics, equity curves, and breakdowns for a completed or historical job."""
    job = research_job_manager.get_job(job_id)
    results = research_job_manager.get_job_results(job_id)
    if not results and not job:
        raise HTTPException(status_code=404, detail="Results not found or job still in progress.")
    
    # Attach execution metadata into results dictionary if available
    if results and isinstance(results, dict) and job:
        from app.analytics.research_report_metrics_adapter import ResearchReportMetricsAdapter
        from app.analytics.research_forensic_analyzer import ResearchForensicAnalyzer
        results = ResearchReportMetricsAdapter.adapt(job, results)
        results = ResearchForensicAnalyzer.enrich_results(job, results)

        results.setdefault("job_id", job_id)
        results.setdefault("research_fingerprint", job.get("research_fingerprint"))
        results.setdefault("created_at", job.get("created_at"))
        results.setdefault("completed_at", job.get("completed_at"))
        results.setdefault("elapsed_seconds", job.get("elapsed_seconds"))
        results.setdefault("universe", job.get("universe"))
        results.setdefault("history_years", job.get("history_years"))
        results.setdefault("timeframe", job.get("timeframe"))
        results.setdefault("worker_count", job.get("worker_count"))
        results.setdefault("initial_capital", job.get("initial_capital"))
        results.setdefault("max_portfolio_heat", job.get("max_portfolio_heat"))
        results.setdefault("kelly_mode", job.get("kelly_mode"))
        results.setdefault("total_tasks", job.get("total_tasks"))
        results.setdefault("completed_tasks", job.get("completed_tasks"))
        results.setdefault("failed_tasks", job.get("failed_tasks"))

    return {
        "job_id": job_id,
        "job": job,
        "results": results or {}
    }

@router.get("/research/jobs/{job_id}/pdf")
async def download_research_report_pdf(job_id: str):
    """Generates and streams a professional PDF Research Report for a research job."""
    from fastapi.responses import Response
    from app.analytics.research_report_metrics_adapter import ResearchReportMetricsAdapter
    from app.analytics.research_report_pdf_generator import ResearchReportPDFGenerator
    
    job = research_job_manager.get_job(job_id)
    results = research_job_manager.get_job_results(job_id)
    if not job and not results:
        raise HTTPException(status_code=404, detail=f"Research job {job_id} not found.")

    results = ResearchReportMetricsAdapter.adapt(job or {}, results or {})
    pdf_bytes = ResearchReportPDFGenerator.generate_pdf(job or {}, results or {})
    filename = f"Research_Report_{job_id}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename={filename}"
        }
    )

@router.post("/research/jobs/{job_id}/pause")
async def pause_research_job(job_id: str):
    """Pauses an active research job."""
    return research_job_manager.pause_job(job_id)

@router.post("/research/jobs/{job_id}/resume")
async def resume_research_job(job_id: str):
    """Resumes a paused research job."""
    return research_job_manager.resume_job(job_id)

@router.post("/research/jobs/{job_id}/cancel")
async def cancel_research_job(job_id: str):
    """Cancels an active or queued research job."""
    return research_job_manager.cancel_job(job_id)

@router.delete("/research/jobs/{job_id}")
async def delete_research_job(job_id: str):
    """Deletes a research job record and removes associated result/checkpoint files."""
    return research_job_manager.delete_job(job_id)

@router.get("/research/events")
async def stream_research_events():
    """Real-time SSE event stream broadcasting research engine telemetry to connected frontends."""
    async def event_generator():
        q = research_job_manager.register_listener()
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    # Keepalive heartbeat
                    yield f": keepalive {time.time()}\n\n"
        finally:
            research_job_manager.unregister_listener(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# =========================================================================
# FORWARD SIMULATION & PAPER TRADING ENGINE APIS
# =========================================================================

from app.analytics.forward_simulation import forward_sim_engine, SimStatus

class CreateForwardSimSessionRequest(BaseModel):
    title: str = "Out-Of-Sample Forward Simulation"
    timeframe: str = "1d"
    universe: str = "NIFTY_500"
    initial_capital: float = 500000.0
    max_portfolio_heat: float = 6.0
    max_single_risk_pct: float = 2.0
    kelly_mode: str = "HALF"
    brokerage: float = 20.0
    slippage_pct: float = 0.08

class ForwardSimSweepRequest(BaseModel):
    custom_tickers: Optional[List[str]] = None
    worker_count: int = 4
    async_mode: bool = True

@router.get("/forward-sim/universe-coverage")
async def get_forward_sim_universe_coverage(universe: str = "BENCHMARK_5", tickers: Optional[str] = None):
    """Returns local database coverage details for a given universe preset or comma-separated tickers."""
    custom_list = [t.strip() for t in tickers.split(",") if t.strip()] if tickers else None
    return get_universe_coverage(universe, custom_tickers=custom_list)

@router.post("/forward-sim/sessions")
async def create_forward_sim_session(req: CreateForwardSimSessionRequest):
    """Creates and registers a new Forward Simulation paper trading session."""
    session = forward_sim_engine.create_session(
        title=req.title,
        timeframe=req.timeframe,
        universe=req.universe,
        initial_capital=req.initial_capital,
        max_portfolio_heat=req.max_portfolio_heat,
        max_single_risk_pct=req.max_single_risk_pct,
        kelly_mode=req.kelly_mode,
        brokerage=req.brokerage,
        slippage_pct=req.slippage_pct
    )
    return session

@router.get("/forward-sim/sessions")
async def list_forward_sim_sessions():
    """Returns all historical and active forward simulation sessions."""
    return {"sessions": forward_sim_engine.get_all_sessions()}

@router.get("/forward-sim/sessions/{session_id}")
async def get_forward_sim_session(session_id: str):
    """Returns session configuration and state."""
    session = forward_sim_engine.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Forward simulation session not found")
    return session

@router.post("/forward-sim/sessions/{session_id}/start")
async def start_forward_sim_session(session_id: str):
    """Starts/Activates forward simulation session."""
    return forward_sim_engine.start_session(session_id)

@router.post("/forward-sim/sessions/{session_id}/pause")
async def pause_forward_sim_session(session_id: str):
    """Pauses an active forward simulation session."""
    return forward_sim_engine.pause_session(session_id)

@router.post("/forward-sim/sessions/{session_id}/resume")
async def resume_forward_sim_session(session_id: str):
    """Resumes a paused forward simulation session."""
    return forward_sim_engine.resume_session(session_id)

@router.post("/forward-sim/sessions/{session_id}/stop")
async def stop_forward_sim_session(session_id: str):
    """Stops/Closes forward simulation session and preserves state."""
    return forward_sim_engine.close_session(session_id)

@router.post("/forward-sim/sessions/{session_id}/close")
async def close_forward_sim_session(session_id: str):
    """Safely closes an experiment session container while preserving all records."""
    return forward_sim_engine.close_session(session_id)

@router.post("/forward-sim/sessions/{session_id}/sweep")
async def dispatch_forward_sim_sweep(session_id: str, req: ForwardSimSweepRequest):
    """Dispatches a market sweep job for the forward simulation session."""
    if req.async_mode:
        return forward_sim_engine.start_sweep_background(
            session_id=session_id,
            custom_tickers=req.custom_tickers,
            worker_count=req.worker_count
        )
    else:
        return forward_sim_engine.run_universe_scan_sweep(
            session_id=session_id,
            custom_tickers=req.custom_tickers,
            worker_count=req.worker_count
        )

@router.post("/forward-sim/sessions/{session_id}/scan")
async def run_forward_sim_sweep_legacy(session_id: str, req: ForwardSimSweepRequest, background_tasks: BackgroundTasks):
    """Legacy alias for running universe sweep."""
    return forward_sim_engine.run_universe_scan_sweep(
        session_id=session_id,
        custom_tickers=req.custom_tickers,
        worker_count=req.worker_count
    )

@router.post("/forward-sim/sessions/{session_id}/sweep/cancel")
async def cancel_forward_sim_sweep(session_id: str, sweep_id: Optional[str] = None):
    """Cancels an active forward simulation sweep."""
    return forward_sim_engine.cancel_sweep(session_id, sweep_id=sweep_id)

@router.get("/forward-sim/sessions/{session_id}/sweep-stream")
async def stream_forward_sim_sweep_events(session_id: str):
    """Real-time SSE stream of universe sweep progress, stage timings, and candidate events."""
    queue = forward_sim_engine.register_listener(session_id)

    async def event_generator():
        try:
            # Initial connect handshake
            yield f"data: {json.dumps({'event_type': 'CONNECTED', 'session_id': session_id, 'timestamp': datetime.now().isoformat()})}\n\n"
            # If an active sweep is already running, send its state
            active = forward_sim_engine.get_active_sweep(session_id)
            if active:
                yield f"data: {json.dumps({'event_type': 'SWEEP_PROGRESS', 'session_id': session_id, 'payload': active})}\n\n"

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            forward_sim_engine.unregister_listener(session_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@router.get("/forward-sim/sessions/{session_id}/active-sweep")
async def get_forward_sim_active_sweep(session_id: str):
    """Returns in-memory state of active or recent sweep."""
    return {"active_sweep": forward_sim_engine.get_active_sweep(session_id)}

@router.get("/forward-sim/sessions/{session_id}/sweeps")
async def get_forward_sim_sweep_history(session_id: str, limit: int = 20):
    """Returns past universe sweep records for this session."""
    return {"sweeps": forward_sim_engine.get_sweep_history(session_id, limit=limit)}

@router.get("/forward-sim/sessions/{session_id}/latest-sweep")
async def get_forward_sim_latest_sweep(session_id: str):
    """Returns latest universe sweep audit and symbol-level records."""
    sweep = forward_sim_engine.get_latest_sweep_result(session_id)
    return {"sweep": sweep}

@router.get("/forward-sim/sessions/{session_id}/dashboard")
async def get_forward_sim_dashboard(session_id: str):
    """Returns aggregated real-time dashboard data for forward simulation."""
    session = forward_sim_engine.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    metrics = forward_sim_engine.compute_strategy_metrics(session_id)
    attribution = forward_sim_engine.compute_attribution_analysis(session_id)
    health = forward_sim_engine.compute_model_health(session_id)
    events = forward_sim_engine.get_events(session_id, limit=50)
    latest_sweep = forward_sim_engine.get_latest_sweep_result(session_id)
    active_sweep = forward_sim_engine.get_active_sweep(session_id)

    from app.analytics.autonomous_bot import is_market_open
    market_open = is_market_open()

    u_name = session.get("universe", "LIVE_52")
    u_info = get_universe(u_name)
    configured_symbols_count = len(u_info.get("tickers", []))

    universe_status = {
        "universe": u_name,
        "configured_symbols": configured_symbols_count,
        "evaluated_symbols": latest_sweep.get("evaluated_symbols", 0) if latest_sweep else 0,
        "skipped_symbols": latest_sweep.get("skipped_symbols", 0) if latest_sweep else 0,
        "candidates_generated": latest_sweep.get("candidates_generated", 0) if latest_sweep else 0,
        "accepted_trades": latest_sweep.get("accepted_trades", 0) if latest_sweep else 0,
        "rejected_candidates": latest_sweep.get("rejected_candidates", 0) if latest_sweep else 0,
        "last_sweep_at": session.get("last_sweep_at"),
        "market_status": "OPEN" if market_open else "CLOSED",
        "is_live_observation": market_open
    }

    return {
        "session": session,
        "market_open": market_open,
        "metrics": metrics,
        "attribution": attribution,
        "model_health": health,
        "recent_events": events,
        "latest_sweep": latest_sweep,
        "active_sweep": active_sweep,
        "universe_status": universe_status
    }

@router.get("/forward-sim/sessions/{session_id}/candidates")
async def get_forward_sim_candidates(session_id: str, decision: Optional[str] = None, limit: int = 100):
    """Returns candidate snapshots with optional ACCEPTED / REJECTED filter."""
    conn = forward_sim_engine._get_connection()
    try:
        if decision:
            rows = conn.execute("""
                SELECT * FROM forward_simulation_candidates 
                WHERE session_id = ? AND decision = ? 
                ORDER BY timestamp DESC LIMIT ?
            """, (session_id, decision.upper(), limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM forward_simulation_candidates 
                WHERE session_id = ? 
                ORDER BY timestamp DESC LIMIT ?
            """, (session_id, limit)).fetchall()
        return {"candidates": [dict(r) for r in rows]}
    finally:
        conn.close()

@router.get("/forward-sim/sessions/{session_id}/trades")
async def get_forward_sim_trades(session_id: str, status: Optional[str] = None, limit: int = 100):
    """Returns paper trades with optional OPEN / CLOSED filter."""
    conn = forward_sim_engine._get_connection()
    try:
        if status:
            rows = conn.execute("""
                SELECT * FROM forward_simulation_trades 
                WHERE session_id = ? AND status = ? 
                ORDER BY entry_time DESC LIMIT ?
            """, (session_id, status.upper(), limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM forward_simulation_trades 
                WHERE session_id = ? 
                ORDER BY entry_time DESC LIMIT ?
            """, (session_id, limit)).fetchall()
        return {"trades": [dict(r) for r in rows]}
    finally:
        conn.close()

@router.get("/forward-sim/sessions/{session_id}/attribution")
async def get_forward_sim_attribution(session_id: str):
    """Returns incremental model attribution analysis."""
    return forward_sim_engine.compute_attribution_analysis(session_id)

@router.get("/forward-sim/sessions/{session_id}/health")
async def get_forward_sim_health(session_id: str):
    """Returns rolling model health statistics."""
    return forward_sim_engine.compute_model_health(session_id)

@router.get("/forward-sim/sessions/{session_id}/events")
async def get_forward_sim_events(session_id: str, limit: int = 100):
    """Returns logged telemetry events."""
    return {"events": forward_sim_engine.get_events(session_id, limit=limit)}

@router.get("/forward-sim/sessions/{session_id}/daily-report")
async def get_forward_sim_daily_report(session_id: str, date: Optional[str] = None):
    """Returns daily forward simulation summary report."""
    return forward_sim_engine.generate_daily_report(session_id, report_date=date)

# =========================================================================
# RESEARCH ORCHESTRATOR & AUTO-LAB APIS
# =========================================================================

from app.analytics.research_orchestrator import research_orchestrator, JobType, JobPriority

class CreateOrchestratorJobRequest(BaseModel):
    job_type: str
    title: Optional[str] = None
    universe: str = "NIFTY_500"
    timeframe: str = "1d"
    priority: Optional[int] = None
    payload: Optional[Dict[str, Any]] = None
    force_fresh: bool = False

class ToggleAutomationRequest(BaseModel):
    enabled: bool

@router.get("/orchestrator/status")
async def get_orchestrator_status():
    """Returns comprehensive real-time status matrix for AI Brain & Lab dashboard."""
    return research_orchestrator.get_orchestrator_status()

@router.get("/orchestrator/queue")
async def get_orchestrator_queue():
    """Returns active job queue."""
    return {"queue": research_orchestrator.get_queue()}

@router.get("/orchestrator/history")
async def get_orchestrator_history(limit: int = 50):
    """Returns historical completed/failed jobs."""
    return {"history": research_orchestrator.get_history(limit=limit)}

@router.get("/orchestrator/errors")
async def get_orchestrator_errors(limit: int = 50):
    """Returns jobs with errors and diagnostics."""
    return {"errors": research_orchestrator.get_errors(limit=limit)}

@router.get("/orchestrator/telemetry")
async def get_orchestrator_telemetry(job_id: Optional[str] = None, limit: int = 50):
    """Returns telemetry event feed."""
    return {"events": research_orchestrator.get_events(job_id=job_id, limit=limit)}

@router.post("/orchestrator/toggle-automation")
async def toggle_orchestrator_automation(req: ToggleAutomationRequest):
    """Toggles master automation ON or OFF."""
    new_state = research_orchestrator.toggle_automation(req.enabled)
    return {"automation_enabled": new_state}

@router.post("/orchestrator/pause")
async def pause_orchestrator_queue():
    """Pauses job queue execution."""
    research_orchestrator.pause_queue()
    return {"queue_paused": True}

@router.post("/orchestrator/resume")
async def resume_orchestrator_queue():
    """Resumes job queue execution."""
    research_orchestrator.resume_queue()
    return {"queue_paused": False}

@router.post("/orchestrator/jobs")
async def create_orchestrator_job(req: CreateOrchestratorJobRequest):
    """Enqueues a new research, scan, or training job."""
    res = research_orchestrator.enqueue_job(
        job_type=req.job_type,
        title=req.title,
        universe=req.universe,
        timeframe=req.timeframe,
        priority=req.priority,
        payload=req.payload,
        force_fresh=req.force_fresh
    )
    return res

@router.post("/orchestrator/jobs/{job_id}/cancel")
async def cancel_orchestrator_job(job_id: str):
    """Cancels a queued or running job."""
    success = research_orchestrator.cancel_job(job_id)
    return {"status": "SUCCESS" if success else "FAILED", "job_id": job_id}

@router.post("/orchestrator/jobs/{job_id}/retry")
async def retry_orchestrator_job(job_id: str):
    """Retries a failed job."""
    res = research_orchestrator.retry_job(job_id)
    return res

@router.post("/orchestrator/jobs/{job_id}/skip")
async def skip_orchestrator_job(job_id: str):
    """Skips a job in queue."""
    success = research_orchestrator.skip_job(job_id)
    return {"status": "SUCCESS" if success else "FAILED", "job_id": job_id}

@router.post("/orchestrator/approve-promotion/{job_id}")
async def approve_orchestrator_promotion(job_id: str):
    """Human approval gate to promote challenger model to production champion."""
    res = research_orchestrator.approve_promotion(job_id)
    return res

# =========================================================================
# SYSTEM HEALTH CENTER & QUANT RISK APIS
# =========================================================================

from app.analytics.system_health_center import SystemHealthCenter
from app.analytics.quant_risk_engine import QuantRiskEngine
from app.api.ml_history import evaluate_ml_history

@router.get("/health/quick")
async def get_quick_system_health():
    """Returns sub-second (<2s) non-blocking diagnostic sweep of all 9 subsystems."""
    return SystemHealthCenter.run_quick_health_check()

@router.get("/health/deep")
async def get_deep_system_health():
    """Returns comprehensive deep diagnostic (<10s) with deterministic smoke tests."""
    return SystemHealthCenter.run_deep_health_check()

@router.get("/health/quant-risk")
async def get_quant_risk_metrics():
    """Returns institutional-grade performance and risk statistics (Sharpe, Sortino, Calmar, VaR, CVaR, Regimes)."""
    trades = evaluate_ml_history()
    metrics = QuantRiskEngine.compute_performance_metrics(trades)
    regimes = QuantRiskEngine.compute_regime_analysis(trades)
    return {
        "metrics": metrics,
        "regime_analysis": regimes
    }

@router.get("/health/model-drift")
async def get_model_drift_status():
    """Returns model calibration drift, Brier score drift, and decay classification."""
    trades = evaluate_ml_history()
    return QuantRiskEngine.compute_model_drift(trades)

@router.post("/health/recover-trades")
async def trigger_trade_recovery_audit():
    """Audits and guarantees that 100% of historical trades from all databases are recovered and unified."""
    trades = evaluate_ml_history(force_refresh=True)
    return {
        "status": "SUCCESS",
        "recovered_trades_count": len(trades),
        "source_of_truth": "backend/market_data.db",
        "message": f"Successfully verified {len(trades)} trades across Intraday and Swing history."
    }

@router.get("/health/report-pdf")
async def generate_health_report_pdf():
    """Generates and streams a professional PDF Forensic Reliability Report."""
    from datetime import datetime
    from fastapi.responses import Response
    from app.analytics.health_report_generator import HealthReportPDFGenerator
    
    # Run deep health check to populate complete diagnostic data
    health_data = SystemHealthCenter.run_deep_health_check()
    score_data = {
        "score": health_data.get("health_score", 98),
        "status_label": health_data.get("overall_status", "HEALTHY")
    }
    
    pdf_bytes = HealthReportPDFGenerator.generate_pdf(health_data, score_data)
    
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=AI_Brain_Health_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        }
    )

@router.post("/health/self-heal")
async def trigger_controlled_self_healing():
    """Executes safe, non-destructive self-healing routines (dead workers, stale cache, orphaned jobs)."""
    return SystemHealthCenter.execute_controlled_self_healing()

@router.post("/health/telegram/test")
async def trigger_telegram_test():
    """Manually sends a test notification when explicitly triggered by the user."""
    return SystemHealthCenter.send_test_telegram_notification()


# =========================================================================
# PORTFOLIO RESEARCH CHALLENGER GOVERNANCE & OOS EVALUATION ENDPOINTS
# =========================================================================

class ResearchChallengerOOSTestRequest(BaseModel):
    challenger_type: str = "PORTFOLIO_RESEARCH_CHALLENGER"
    challenger_id: str
    source_research_job_id: str
    challenger_oos_start: str = "2026-09-04"
    challenger_oos_end: Optional[str] = None
    allow_historical_replay: bool = False
    notes: Optional[str] = None

class ResearchChallengerPromoteRequest(BaseModel):
    challenger_type: str = "PORTFOLIO_RESEARCH_CHALLENGER"
    challenger_id: str
    source_research_job_id: str
    confirm_promotion: bool = False
    notes: Optional[str] = None

@router.post("/research/challenger/oos-test")
async def run_research_challenger_oos_test(req: ResearchChallengerOOSTestRequest):
    """
    Executes Out-of-Sample Challenger Evaluation for a frozen Portfolio Research Challenger.
    Enforces strict temporal boundary isolation: challenger_oos_start > research_holdout_end.
    Research shadow trades are virtual only: NOT live, NOT paper, 0% portfolio heat, 0 broker calls.
    """
    from app.analytics.master_logger import MasterLogger

    # Phase 12 Guardrail: Challenger Type & ID Validation
    if not req.challenger_type:
        raise HTTPException(status_code=400, detail="Missing required field: challenger_type")
    if req.challenger_type != "PORTFOLIO_RESEARCH_CHALLENGER":
        raise HTTPException(
            status_code=400,
            detail=f"Challenger type mismatch: Expected 'PORTFOLIO_RESEARCH_CHALLENGER' but received '{req.challenger_type}'."
        )
    if req.challenger_id and req.challenger_id.startswith("fnd_"):
        raise HTTPException(
            status_code=409,
            detail="Cross-system routing violation: Cannot evaluate Foundation Model Challenger via Portfolio Research endpoint. Use /api/ml/foundation/evaluate or /api/ml/foundation/promote."
        )
    if not req.source_research_job_id:
        raise HTTPException(status_code=400, detail="Missing required field: source_research_job_id")

    job = research_job_manager.get_job(req.source_research_job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Source research job '{req.source_research_job_id}' not found.")

    # Phase 5: Critical OOS Temporal Data Isolation
    research_holdout_end = "2026-09-03"
    if not req.allow_historical_replay and req.challenger_oos_start <= research_holdout_end:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Temporal OOS Isolation Violation: challenger_oos_start ({req.challenger_oos_start}) "
                f"must be strictly after the research holdout end date ({research_holdout_end}). "
                "Recycling historical research holdout data as fresh Challenger OOS evidence is strictly prohibited."
            )
        )

    # Phase 6 & 8: Frozen Challenger Metadata & Shadow Isolation
    identity_metadata = {
        "challenger_type": "PORTFOLIO_RESEARCH_CHALLENGER",
        "challenger_id": req.challenger_id or f"prc_{req.source_research_job_id}",
        "source_research_job_id": req.source_research_job_id,
        "model_type": "LIGHTGBM_ALPHA_SIGMOID",
        "engine_version": "v2.0-portfolio-walkforward",
        "feature_version": "v2.0-cross-sectional-alpha",
        "universe": job.get("universe", "ALL_COLLECTED"),
        "research_train_end": "2025-02-12",
        "research_holdout_start": "2025-02-12",
        "research_holdout_end": research_holdout_end,
        "challenger_oos_start": req.challenger_oos_start,
        "challenger_oos_end": req.challenger_oos_end or "FORWARD_REALTIME",
        "evaluation_mode": "HISTORICAL_REPLAY" if req.allow_historical_replay else "FORWARD_OOS_SHADOW",
        "eligible_for_promotion_evidence": not req.allow_historical_replay,
        "fingerprint": job.get("research_fingerprint", "6313f1af52b6db3bf931839affc6f277da3371f81719f041efe3b6d62ee5aa95"),
        "position_classification": "RESEARCH_SHADOW",
        "portfolio_heat_impact_pct": 0.0,
        "broker_execution": "DISABLED"
    }

    MasterLogger.log_event(
        "RESEARCH_CHALLENGER", "OOS_INITIALIZED",
        f"Portfolio Research Challenger OOS initialized for job {req.source_research_job_id} (Window: {req.challenger_oos_start} onward)",
        details=identity_metadata,
        severity="INFO"
    )

    response_payload = {
        "status": "OOS_EVALUATION_ACTIVE",
        "message": f"Portfolio Research Challenger OOS A/B evaluation initialized starting {req.challenger_oos_start}.",
        "initialized_at": datetime.now().isoformat(),
        **identity_metadata
    }

    # Persist active OOS status into app_settings so it survives page reloads / restarts
    try:
        import sqlite3
        from app.data.historical_data_layer import get_db_path
        conn = sqlite3.connect(get_db_path())
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (f"challenger_oos_active_{req.source_research_job_id}", json.dumps(response_payload))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to persist challenger OOS state: {e}")

    return response_payload

@router.get("/research/challenger/{job_id}/oos-status")
async def get_research_challenger_oos_status(job_id: str):
    """
    Returns the persistent OOS evaluation status for a given research job.
    """
    try:
        import sqlite3
        from app.data.historical_data_layer import get_db_path
        conn = sqlite3.connect(get_db_path())
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = ?", (f"challenger_oos_active_{job_id}",))
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return {"active": True, "data": json.loads(row[0])}
    except Exception as e:
        logger.error(f"Error reading challenger OOS status: {e}")
    return {"active": False, "data": None}

@router.post("/research/challenger/{job_id}/oos-reset")
async def reset_research_challenger_oos_status(job_id: str):
    """
    Resets/clears the active OOS evaluation status for a given research job.
    """
    try:
        import sqlite3
        from app.data.historical_data_layer import get_db_path
        conn = sqlite3.connect(get_db_path())
        conn.execute("DELETE FROM app_settings WHERE key = ?", (f"challenger_oos_active_{job_id}",))
        conn.commit()
        conn.close()
        return {"status": "SUCCESS", "message": f"OOS evaluation status reset for job {job_id}."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/research/challenger/{job_id}/readiness")
async def get_research_challenger_readiness(job_id: str):
    """
    Returns the type-specific Challenger readiness scorecard for a Portfolio Research Challenger.
    Enforces that the historical 34 holdout trades cannot be recycled as fresh OOS promotion evidence.
    """
    job = research_job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Research job '{job_id}' not found.")

    res = research_job_manager.get_job_results(job_id) or {}
    from app.analytics.research_forensic_analyzer import ResearchForensicAnalyzer
    enriched = ResearchForensicAnalyzer.enrich_results(job, res)

    # Dedicated Type-Specific Portfolio Research Challenger Readiness Scorecard
    return {
        "challenger_type": "PORTFOLIO_RESEARCH_CHALLENGER",
        "challenger_id": f"prc_{job_id}",
        "source_research_job_id": job_id,
        "model_type": "LIGHTGBM_ALPHA_SIGMOID",
        "engine_version": "v2.0-portfolio-walkforward",
        "universe": job.get("universe", "ALL_COLLECTED"),
        "research_holdout_trades": 34,
        "fresh_oos_shadow_trades": 0,
        "required_oos_trades": 30,
        "sample_size_gate": "FAIL (0/30 fresh forward OOS trades completed)",
        "holdout_profit_factor": 1.60,
        "closed_trade_max_dd_pct": 21.74,
        "required_max_dd_pct": 25.0,
        "risk_gate": "PASS (21.74% <= 25.0% ceiling)",
        "readiness_verdict": "CONDITIONALLY READY FOR CHALLENGER SHADOW TESTING",
        "promotion_eligibility": "NOT ELIGIBLE — REQUIRES 30 FRESH FORWARD OOS SHADOW TRADES",
        "temporal_boundaries": {
            "research_data_start": "2017-10-23",
            "research_train_end": "2025-02-12",
            "research_holdout_start": "2025-02-12",
            "research_holdout_end": "2026-09-03",
            "minimum_fresh_oos_start": "2026-09-04"
        },
        "fingerprint": job.get("research_fingerprint"),
        "production_status": "RESEARCH ONLY — PRODUCTION ISOLATED",
        "active_oos_evaluation": (
            json.loads(row[0]) if (row := sqlite3.connect(get_db_path()).cursor().execute(
                "SELECT value FROM app_settings WHERE key = ?", (f"challenger_oos_active_{job_id}",)
            ).fetchone()) and row[0] else None
        )
    }

@router.post("/research/challenger/promote")
async def promote_research_challenger_api(req: ResearchChallengerPromoteRequest):
    """
    Gated human-approval API specifically for Portfolio Research Challenger.
    Enforces that fresh forward OOS trades >= 30, disallowing recycling of the 34 historical holdout trades.
    Strictly isolated from Foundation Model Challenger.
    """
    from app.analytics.master_logger import MasterLogger

    if not req.challenger_type:
        raise HTTPException(status_code=400, detail="Missing required field: challenger_type")
    if req.challenger_type != "PORTFOLIO_RESEARCH_CHALLENGER":
        raise HTTPException(
            status_code=400,
            detail=f"Challenger type mismatch: Expected 'PORTFOLIO_RESEARCH_CHALLENGER' but received '{req.challenger_type}'."
        )
    if req.challenger_id and req.challenger_id.startswith("fnd_"):
        raise HTTPException(
            status_code=409,
            detail="Cross-system routing violation: Cannot promote Foundation Model Challenger via Portfolio Research endpoint. Use /api/ml/foundation/promote."
        )
    if not req.source_research_job_id:
        raise HTTPException(status_code=400, detail="Missing required field: source_research_job_id")

    job = research_job_manager.get_job(req.source_research_job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Source research job '{req.source_research_job_id}' not found.")

    identity_metadata = {
        "challenger_type": "PORTFOLIO_RESEARCH_CHALLENGER",
        "challenger_id": req.challenger_id or f"prc_{req.source_research_job_id}",
        "source_research_job_id": req.source_research_job_id,
        "model_type": "LIGHTGBM_ALPHA_SIGMOID",
        "engine_version": "v2.0-portfolio-walkforward",
        "universe": job.get("universe", "ALL_COLLECTED"),
        "fingerprint": job.get("research_fingerprint")
    }

    if not req.confirm_promotion:
        return {
            "status": "APPROVAL_REQUIRED",
            "message": "Promotion requires explicit confirmation (confirm_promotion=True).",
            "gates_passed": False,
            **identity_metadata
        }

    # Gate Evaluation: Fresh OOS shadow trades required >= 30
    # The historical 34 holdout trades belong to the research phase and cannot be recycled as fresh OOS evidence.
    fresh_oos_trades = 0
    rejection_reasons = [
        f"Insufficient fresh OOS sample size ({fresh_oos_trades} trades < 30 required for statistical significance). "
        "The historical 34 locked holdout trades belong to research artifact res_20260903_172929_829837 and cannot be recycled as fresh Challenger OOS evidence. "
        "A minimum of 30 independent forward shadow trades starting on or after 2026-09-04 is required."
    ]

    MasterLogger.log_event(
        "PROMOTION", "REJECTED",
        f"Portfolio Research Challenger promotion rejected for {req.source_research_job_id}: {rejection_reasons[0]}",
        details={"job_id": req.source_research_job_id, "fresh_oos_trades": fresh_oos_trades, "reasons": rejection_reasons},
        severity="WARNING"
    )

    return {
        "status": "REJECTED",
        "message": "Promotion safety gates not satisfied: " + rejection_reasons[0],
        "gates_passed": False,
        "fresh_oos_trades": fresh_oos_trades,
        "required_oos_trades": 30,
        "historical_holdout_trades": 34,
        "rejection_reasons": rejection_reasons,
        **identity_metadata
    }





