import os
from fastapi import FastAPI, Query
from apscheduler.schedulers.background import BackgroundScheduler
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from app.tasks.hoarder import hoard_intraday_data

# Set up logging for scheduler
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Enable SQLite Write-Ahead Logging (WAL) for concurrent read/writes
def init_db_wal():
    import sqlite3
    from app.data.historical_data_layer import get_db_path
    try:
        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.commit()
        conn.close()
        logger.info("SQLite WAL (Write-Ahead Logging) Mode Enabled on canonical database.")
    except Exception as e:
        logger.warning(f"Could not enable WAL mode: {e}")

init_db_wal()

import threading
import pytz

IST_TIMEZONE = pytz.timezone("Asia/Kolkata")

# ── SCHEDULER SINGLETON & LIFECYCLE MANAGEMENT ────────────────────────
_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_INSTANCE = None

def get_or_create_scheduler() -> BackgroundScheduler:
    """Thread-safe singleton guaranteeing exactly one scheduler instance."""
    global _SCHEDULER_INSTANCE
    with _SCHEDULER_LOCK:
        if _SCHEDULER_INSTANCE is None:
            from app.data.database import is_testing_environment
            if is_testing_environment() or os.environ.get("SUPPRESS_SCHEDULER") == "true":
                logger.info("[SCHEDULER ISOLATION] Test execution environment detected. Background schedulers disabled.")
                _SCHEDULER_INSTANCE = BackgroundScheduler(daemon=True, timezone=IST_TIMEZONE)
                return _SCHEDULER_INSTANCE

            _SCHEDULER_INSTANCE = BackgroundScheduler(daemon=True, timezone=IST_TIMEZONE)
            
            # 1. Data Hoarder: Daily 16:00 IST (Broad 15m intraday sync)
            _SCHEDULER_INSTANCE.add_job(
                hoard_intraday_data, 'cron', hour=16, minute=0,
                timezone=IST_TIMEZONE,
                kwargs={'universe': 'NIFTY_500'},
                id='data_hoarder_1600', replace_existing=True
            )

            # 1b. Broad Daily OHLCV Ingestion: Daily 16:15 IST Mon-Fri
            try:
                from app.data.historical_data_layer import HistoricalDataLayer
                _SCHEDULER_INSTANCE.add_job(
                    HistoricalDataLayer.sync_universe_daily, 'cron',
                    day_of_week='mon-fri', hour=16, minute=15,
                    kwargs={'universe': 'NIFTY_500'},
                    id='daily_ohlcv_sync_1615', replace_existing=True
                )
            except Exception as e:
                logger.error(f"Failed to register broad daily OHLCV sync: {e}")

            # 2. AI Guard Babysitter: Every 5 minutes during market hours
            try:
                from app.analytics.autonomous_bot import active_trade_tracker
                _SCHEDULER_INSTANCE.add_job(
                    active_trade_tracker, 'interval', minutes=5,
                    id='active_trade_tracker_5m', replace_existing=True
                )
            except Exception as e:
                logger.error(f"Failed to register active_trade_tracker: {e}")

            # 3. Weekly Sunday Retraining: 23:00 IST
            try:
                from app.analytics.retrain_models import execute_retraining_pipeline
                def scheduled_weekly_retrain():
                    logger.info("Executing scheduled Sunday retraining for SWING models...")
                    execute_retraining_pipeline(timeframe="swing")
                    logger.info("Executing scheduled Sunday retraining for INTRADAY models...")
                    execute_retraining_pipeline(timeframe="intraday")

                _SCHEDULER_INSTANCE.add_job(
                    scheduled_weekly_retrain, 'cron', day_of_week='sun', hour=23, minute=0,
                    id='weekly_retrain_sun', replace_existing=True
                )
            except Exception as e:
                logger.error(f"Failed to register weekly retraining: {e}")

            # 4. Autopilot Scheduled Discovery Scans (09:30, 11:30, 13:30 IST Mon-Fri)
            try:
                from app.tasks.autopilot_scanner import run_scheduled_autopilot_sweep
                _SCHEDULER_INSTANCE.add_job(
                    lambda: run_scheduled_autopilot_sweep("Morning Momentum"),
                    'cron', day_of_week='mon-fri', hour=9, minute=30,
                    id='autopilot_0930', replace_existing=True,
                    misfire_grace_time=900
                )
                _SCHEDULER_INSTANCE.add_job(
                    lambda: run_scheduled_autopilot_sweep("Mid-Day Continuation"),
                    'cron', day_of_week='mon-fri', hour=11, minute=30,
                    id='autopilot_1130', replace_existing=True,
                    misfire_grace_time=900
                )
                _SCHEDULER_INSTANCE.add_job(
                    lambda: run_scheduled_autopilot_sweep("Afternoon Breakout"),
                    'cron', day_of_week='mon-fri', hour=13, minute=30,
                    id='autopilot_1330', replace_existing=True,
                    misfire_grace_time=900
                )
            except Exception as e:
                logger.error(f"Failed to register autopilot sweeps: {e}")

            # 5. Research Orchestrator & Auto-Lab
            try:
                from app.analytics.research_orchestrator import research_orchestrator
                research_orchestrator.register_scheduled_jobs(_SCHEDULER_INSTANCE)
                research_orchestrator.start_orchestrator_daemon()
            except Exception as e:
                logger.error(f"Failed to initialize research orchestrator daemon: {e}")

            # 6. Daily Dashboard Market Intelligence Report (DECOMMISSIONED - Telegram report disabled)
            # Retained in codebase for reference, but unregistered from active APScheduler to stop notifications
            # try:
            #     from app.analytics.dashboard_telegram_scheduler import execute_daily_dashboard_telegram_job
            #     _SCHEDULER_INSTANCE.add_job(
            #         execute_daily_dashboard_telegram_job,
            #         'cron', day_of_week='mon-fri', hour=8, minute=15,
            #         timezone=IST_TIMEZONE,
            #         id='daily_dashboard_report_0815', replace_existing=True
            #     )
            # except Exception as e:
            #     logger.error(f"Failed to register daily dashboard report job: {e}")

            # 7. Automated Legacy Intraday Square-off (15:15 IST Mon-Fri)
            try:
                from app.analytics.intraday_squareoff import execute_intraday_squareoff
                _SCHEDULER_INSTANCE.add_job(
                    execute_intraday_squareoff,
                    'cron', day_of_week='mon-fri', hour=15, minute=15,
                    timezone=IST_TIMEZONE,
                    id='intraday_squareoff_1515', replace_existing=True
                )
                logger.info("✅ Automated 15:15 IST intraday square-off job registered.")
            except Exception as e:
                logger.error(f"Failed to register intraday square-off job: {e}")

            # 8. Automated 15-Minute Market-Hours Data Hoarder & OOS Evaluator (09:15-15:30 IST Mon-Fri)
            try:
                from app.tasks.intraday_market_daemon import run_15m_market_tick_pipeline
                _SCHEDULER_INSTANCE.add_job(
                    run_15m_market_tick_pipeline,
                    'cron',
                    day_of_week='mon-fri',
                    hour='9-15',
                    minute='0,15,30,45',
                    timezone=IST_TIMEZONE,
                    id='market_hours_15m_hoard_oos',
                    replace_existing=True
                )
                logger.info("✅ Automated 15-minute market-hours data/OOS hoarding daemon registered.")
            except Exception as e:
                logger.error(f"Failed to register 15-minute market daemon job: {e}")

            # 8b. Automated 1-Minute Active Consensus Trade Reconciler (09:15-15:30 IST Mon-Fri)
            try:
                def _reconcile_consensus_trades_daemon():
                    from app.analytics.consensus_tracker import ConsensusTracker
                    ConsensusTracker.reconcile_open_consensus_trades(min_interval_sec=45.0)
                    try:
                        from app.analytics.consensus_dynamic_guard import ConsensusDynamicGuard
                        ConsensusDynamicGuard.audit_active_arena_positions(session_id="default_arena_session")
                    except Exception:
                        pass

                _SCHEDULER_INSTANCE.add_job(
                    _reconcile_consensus_trades_daemon,
                    'cron',
                    day_of_week='mon-fri',
                    hour='9-15',
                    minute='*',
                    timezone=IST_TIMEZONE,
                    id='consensus_trade_reconciler_1m',
                    replace_existing=True
                )
                logger.info("✅ Automated 1-minute consensus trade reconciler registered.")
            except Exception as e:
                logger.error(f"Failed to register 1-minute consensus reconciler job: {e}")

            # 9. Automated Weekly Rolling Walk-Forward Retraining (Saturdays 02:00 IST)
            try:
                def _run_weekly_rolling_retrain():
                    from app.analytics.retrain_models import execute_retraining_pipeline
                    for tf in ('swing', 'intraday'):
                        try:
                            execute_retraining_pipeline(timeframe=tf)
                        except Exception as err:
                            logger.error(f"[WeeklyRetrain] Error during {tf} retraining: {err}")

                _SCHEDULER_INSTANCE.add_job(
                    _run_weekly_rolling_retrain,
                    'cron',
                    day_of_week='sat',
                    hour=2,
                    minute=0,
                    timezone=IST_TIMEZONE,
                    id='weekly_rolling_walk_forward_retrain',
                    replace_existing=True
                )
                logger.info("✅ Automated weekly rolling walk-forward retraining daemon registered.")
            except Exception as e:
                logger.error(f"Failed to register weekly retrain job: {e}")

            # 10. Automated Daily Post-Market Calibration Refresh (Mon-Fri 16:00 IST)
            try:
                from app.analytics.calibration import fit_rolling_calibrator
                _SCHEDULER_INSTANCE.add_job(
                    fit_rolling_calibrator,
                    'cron',
                    day_of_week='mon-fri',
                    hour=16,
                    minute=0,
                    timezone=IST_TIMEZONE,
                    id='daily_post_market_calibration_refresh',
                    replace_existing=True
                )
                logger.info("✅ Automated daily empirical calibration refresh daemon registered.")
            except Exception as e:
                logger.error(f"Failed to register daily calibration daemon: {e}")

            # 11. Automated Daily Post-Market Telegram Intelligence Summary (Mon-Fri 16:00 IST)
            try:
                from app.analytics.post_market_reporter import PostMarketReporter
                _SCHEDULER_INSTANCE.add_job(
                    PostMarketReporter.dispatch_post_market_report,
                    'cron',
                    day_of_week='mon-fri',
                    hour=16,
                    minute=0,
                    timezone=IST_TIMEZONE,
                    id='daily_post_market_telegram_summary',
                    replace_existing=True
                )
                logger.info("✅ Automated daily post-market Telegram summary daemon registered.")
            except Exception as e:
                logger.error(f"Failed to register post-market Telegram summary daemon: {e}")

            logger.info("✅ APScheduler initialized with unique job IDs and duplicate protection.")

        return _SCHEDULER_INSTANCE

scheduler = get_or_create_scheduler()

from fastapi.middleware.cors import CORSMiddleware
from app.api.backtest import router as backtest_router
from app.api.market import router as market_router
from app.api.intraday_ml import router as intraday_ml_router
from app.api.ml_lab import router as ml_lab_router
from app.api.broker import router as broker_router
from app.api.swing_ml import router as swing_router
from app.api.ml_backtest import router as ml_backtest_router
from app.api.settings import router as settings_router
from app.api.fno import router as fno_router
from app.api.data_lab import router as data_lab_router
from app.api.dashboard_intelligence import router as dashboard_router
from app.api.qlib_research import router as qlib_research_router
from app.api.research_autopilot import router as research_autopilot_router
from app.api.watchlist import router as watchlist_router
from app.api.watchlist_scanner import router as watchlist_scanner_router
from app.api.smart_scanner import router as smart_scanner_router

app = FastAPI(title="Swing Trading AI Backend")

@app.on_event("startup")
def on_startup():
    from app.analytics.process_lifecycle_manager import ProcessLifecycleManager
    ProcessLifecycleManager.register_shutdown_handlers()
    ProcessLifecycleManager.cleanup_orphaned_python_workers()

    sched = get_or_create_scheduler()
    if not sched.running:
        sched.start()
        logger.info("🚀 APScheduler background worker started.")

    # Initialize Microsoft Qlib runtime once at startup (process-level singleton)
    try:
        from app.qlib_engine.india_data_adapter import ensure_qlib_ready
        ensure_qlib_ready()
        logger.info("✅ Microsoft Qlib runtime initialized on startup.")
    except Exception as e:
        logger.warning(f"Qlib runtime startup initialization warning: {e}")

    # Reconcile overdue intraday positions from previous sessions or past 15:15 IST
    try:
        from app.analytics.intraday_squareoff import reconcile_overdue_intraday_positions
        rec_res = reconcile_overdue_intraday_positions()
        if rec_res.get("reconciled_count", 0) > 0:
            logger.info(f"✅ Startup reconciliation closed {rec_res['reconciled_count']} overdue intraday positions.")
    except Exception as e:
        logger.warning(f"Intraday startup reconciliation warning: {e}")

    # Start continuous Upstox Market Data Feed V3 streaming service (~507 equity universe)
    try:
        from app.data.upstox_stream_service import get_stream_service
        stream_svc = get_stream_service()
        stream_svc.start()
        logger.info("✅ Continuous Upstox V3 Market Data Feed streaming service started.")
    except Exception as e:
        logger.warning(f"Upstox streaming service startup warning: {e}")

    # Start Option Sniffer Surveillance Daemon (Paper-only)
    try:
        from app.analytics.option_sniffer.option_sniffer_orchestrator import get_option_sniffer_orchestrator
        get_option_sniffer_orchestrator().start()
        logger.info("✅ Option Sniffer surveillance daemon started (PAPER ONLY).")
    except Exception as e:
        logger.warning(f"Option Sniffer startup warning: {e}")

@app.on_event("shutdown")
def on_shutdown():
    from app.analytics.process_lifecycle_manager import ProcessLifecycleManager
    ProcessLifecycleManager.terminate_all_pools()

    try:
        from app.analytics.option_sniffer.option_sniffer_orchestrator import get_option_sniffer_orchestrator
        get_option_sniffer_orchestrator().stop()
        logger.info("🛑 Option Sniffer surveillance daemon stopped.")
    except Exception as e:
        logger.warning(f"Option Sniffer shutdown warning: {e}")

    try:
        from app.data.upstox_stream_service import get_stream_service
        get_stream_service().stop()
        logger.info("🛑 Upstox streaming service stopped.")
    except Exception as e:
        logger.warning(f"Upstox streaming service shutdown warning: {e}")

    sched = get_or_create_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
        logger.info("🛑 APScheduler stopped gracefully.")

@app.get("/api/stream/status")
def get_stream_status():
    """Operational telemetry of the continuous Upstox V3 streaming service."""
    from app.data.upstox_stream_service import get_stream_service
    return get_stream_service().get_status()

@app.get("/api/scheduler/status")
def get_scheduler_status():
    """Operational status of the autonomous background scheduler."""
    sched = get_or_create_scheduler()
    jobs = []
    for job in sched.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger)
        })
    return {
        "status": "RUNNING" if sched.running else "STOPPED",
        "running": sched.running,
        "backend_status": "ONLINE",
        "job_count": len(jobs),
        "jobs": jobs,
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/system/audit-log")
def get_system_audit_log(
    category: Optional[str] = None,
    event_type: Optional[str] = None,
    ticker: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
):
    from app.analytics.master_logger import MasterLogger
    events = MasterLogger.get_events(
        category=category,
        event_type=event_type,
        ticker=ticker,
        severity=severity,
        limit=limit,
        offset=offset
    )
    return {"status": "success", "count": len(events), "events": events}

@app.post("/api/system/pipeline-test")
def run_pipeline_test(timeframe: str = Query("intraday", enum=["intraday", "swing"])):
    """
    Executes a safe, non-contaminating end-to-end synthetic pipeline diagnostic using TESTSTOCK.NS.
    Validates all 11 stages: Ingestion, Validation, Models, Meta-Learner, Calibration,
    Decision Gate, Risk Isolation, and Master Logger without mutating real market or broker state.
    """
    from app.analytics.synthetic_pipeline_tester import SyntheticPipelineTester
    return SyntheticPipelineTester.run_diagnostic(timeframe=timeframe)

@app.get("/api/market/universes")
def get_market_universes():
    """
    Returns all authoritative universe presets and their configurations.
    Used by Intraday Scanner, Swing Scanner, DataLab, and Research orchestrators.
    """
    from app.analytics.universe_config import UNIVERSE_PRESETS, get_available_db_tickers
    db_tickers = get_available_db_tickers()
    presets = {}
    for k, v in UNIVERSE_PRESETS.items():
        count = len(v.get("tickers", []))
        if k in ("ALL_117", "ALL_COLLECTED"):
            count = len(db_tickers)
        presets[k] = {
            "id": k,
            "name": v.get("name"),
            "count": count,
            "description": v.get("description"),
            "survivorship_bias": v.get("survivorship_bias")
        }
    return {"status": "success", "universes": presets, "all_db_symbols": db_tickers}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(backtest_router, prefix="/api/backtest", tags=["backtest"])
app.include_router(market_router, prefix="/api/market", tags=["market"])
app.include_router(intraday_ml_router, prefix="/api/ml", tags=["ml"])
app.include_router(ml_lab_router, prefix="/api/ml", tags=["ml_lab"])
app.include_router(broker_router, prefix="/api/broker", tags=["broker"])
app.include_router(swing_router, prefix="/api/ml", tags=["swing_ml"])
app.include_router(ml_backtest_router, prefix="/api/ml", tags=["ml_backtest"])
app.include_router(settings_router, prefix="/api/settings", tags=["settings"])
app.include_router(fno_router, prefix="/api/fno", tags=["fno"])
app.include_router(data_lab_router, prefix="/api", tags=["data_lab"])
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(qlib_research_router, prefix="/api/ml/qlib", tags=["qlib_research"])
app.include_router(watchlist_router, prefix="/api/watchlist", tags=["watchlist"])
app.include_router(watchlist_scanner_router, prefix="/api/watchlist", tags=["watchlist_scanner"])
app.include_router(research_autopilot_router, prefix="/api/research-autopilot", tags=["research_autopilot"])
app.include_router(smart_scanner_router, prefix="/api", tags=["smart_scanner"])

from app.api.qlib_system import router as qlib_system_router
app.include_router(qlib_system_router, prefix="/api", tags=["qlib_system"])

from app.api.consensus import router as consensus_router
app.include_router(consensus_router, prefix="/api", tags=["consensus"])

from app.api.consensus_game import router as consensus_game_router
app.include_router(consensus_game_router, prefix="/api", tags=["consensus_game"])

from app.api.option_sniffer_api import router as option_sniffer_router
app.include_router(option_sniffer_router)

@app.get("/")
def read_root():
    return {"message": "Swing Trading AI API is running"}

from fastapi.responses import StreamingResponse
from app.tasks.hoarder import hoard_intraday_data_stream

from pydantic import BaseModel

class HoarderRequest(BaseModel):
    universe: str = 'NIFTY_500'
    custom_tickers: Optional[List[str]] = None
    data_source: str = 'upstox'
    api_key: str = ''

@app.post("/api/hoarder/trigger")
def trigger_hoarder(req: HoarderRequest):
    return StreamingResponse(
        hoard_intraday_data_stream(
            universe=req.universe,
            custom_tickers=req.custom_tickers,
            data_source=req.data_source,
            api_key=req.api_key
        ),
        media_type="text/event-stream"
    )
