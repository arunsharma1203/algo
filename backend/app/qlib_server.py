"""
QLIB DEDICATED BACKEND SERVER
=============================
Standalone FastAPI server for Microsoft Qlib Parallel Trading System.

Invariants:
- 100% physically isolated process running on QLIB_BACKEND_PORT (default 8001).
- Does NOT start Legacy APScheduler jobs (data_hoarder, autopilot_sweep, weekly_retrain, dashboard_telegram).
- Maintains its own dedicated QlibScheduler for virtual recommendation reconciliation and scheduled scans.
- Independent lifecycle: stopping or restarting Qlib server does NOT affect Legacy backend on port 8000.
"""

import os
import sys
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
import threading

# Configure MLflow and Qlib environment
os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"
os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

import qlib
from app.qlib_engine.model_registry import QlibModelRegistry
from app.qlib_engine.virtual_tracker import QlibVirtualTracker, init_qlib_tracking_schema
from app.api.qlib_system import router as qlib_system_router

logger = logging.getLogger("qlib_server")
logging.basicConfig(level=logging.INFO)

IST_TIMEZONE = pytz.timezone("Asia/Kolkata")
QLIB_BACKEND_PORT = int(os.environ.get("QLIB_BACKEND_PORT", 8001))

# ── DEDICATED QLIB SCHEDULER (ISOLATED FROM LEGACY) ───────────────────────────
_QLIB_SCHEDULER_LOCK = threading.Lock()
_QLIB_SCHEDULER_INSTANCE = None


def get_or_create_qlib_scheduler() -> BackgroundScheduler:
    """Singleton scheduler exclusively for Qlib background tasks."""
    global _QLIB_SCHEDULER_INSTANCE
    with _QLIB_SCHEDULER_LOCK:
        if _QLIB_SCHEDULER_INSTANCE is None:
            _QLIB_SCHEDULER_INSTANCE = BackgroundScheduler(daemon=True, timezone=IST_TIMEZONE)
            
            if os.environ.get("SUPPRESS_SCHEDULER") == "true":
                logger.info("[QLIB SCHEDULER] Suppressed for testing environment.")
                return _QLIB_SCHEDULER_INSTANCE

            # Job 1: Reconcile virtual recommendations against latest market prices (Every 30 minutes)
            def reconcile_virtual_outcomes():
                try:
                    logger.info("[QLIB SCHEDULER] Running periodic virtual recommendation reconciliation...")
                    count = QlibVirtualTracker.reconcile_open_recommendations()
                    logger.info(f"[QLIB SCHEDULER] Reconciled {count} virtual recommendations.")
                except Exception as e:
                    logger.error(f"[QLIB SCHEDULER] Error during reconciliation: {e}")

            _QLIB_SCHEDULER_INSTANCE.add_job(
                reconcile_virtual_outcomes,
                "interval",
                minutes=30,
                id="qlib_virtual_reconciliation",
                replace_existing=True
            )

            # Job 2: Daily Scheduled Qlib Swing & F&O Scan (15:40 IST Mon-Fri after market close)
            def scheduled_qlib_evening_scan():
                try:
                    from app.qlib_engine.scanners import QlibScanners
                    logger.info("[QLIB SCHEDULER] Running daily post-market Qlib scan...")
                    QlibScanners.run_swing_scan(top_k=5)
                    QlibScanners.run_fno_scan(top_k=3)
                except Exception as e:
                    logger.error(f"[QLIB SCHEDULER] Error during evening scan: {e}")

            _QLIB_SCHEDULER_INSTANCE.add_job(
                scheduled_qlib_evening_scan,
                "cron",
                day_of_week="mon-fri",
                hour=15,
                minute=40,
                timezone=IST_TIMEZONE,
                id="qlib_postmarket_scan",
                replace_existing=True
            )

            logger.info("✅ QlibScheduler initialized with isolated Qlib-only jobs.")
        return _QLIB_SCHEDULER_INSTANCE


# ── FASTAPI APPLICATION ───────────────────────────────────────────────────────
app = FastAPI(
    title="Microsoft Qlib Parallel Trading Engine API",
    description="Dedicated API server for Qlib-powered trading, training, scanning, and virtual tracking.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_qlib_tracking_schema()
    sched = get_or_create_qlib_scheduler()
    if not sched.running:
        sched.start()
        logger.info("🚀 QlibScheduler background worker started.")


@app.on_event("shutdown")
def on_shutdown():
    sched = get_or_create_qlib_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
        logger.info("🛑 QlibScheduler stopped gracefully.")


# Mount Qlib routes
app.include_router(qlib_system_router, prefix="/api", tags=["qlib_system"])
app.include_router(qlib_system_router, prefix="/api/qlib", tags=["qlib_system_direct"])


@app.get("/")
def qlib_root():
    return {
        "engine": "QLIB",
        "name": "Microsoft Qlib Parallel Trading Engine",
        "status": "RUNNING",
        "qlib_version": getattr(qlib, "__version__", "unknown"),
        "port": QLIB_BACKEND_PORT,
        "pid": os.getpid(),
        "timestamp": datetime.now().isoformat()
    }


@app.get("/api/qlib/runtime-status")
def get_qlib_runtime_status():
    """
    Authoritative real-time health check for Qlib server.
    Used by frontend dual-engine monitoring.
    """
    sched = get_or_create_qlib_scheduler()
    scheduled_jobs = []
    for j in sched.get_jobs():
        scheduled_jobs.append({
            "id": j.id,
            "next_run_time": j.next_run_time.isoformat() if j.next_run_time else None,
            "trigger": str(j.trigger)
        })

    registry = QlibModelRegistry.get_registry_status()
    latest_recs = QlibVirtualTracker.get_recommendations(limit=1)
    last_rec = latest_recs[0] if latest_recs else None
    active_training = None

    return {
        "engine": "QLIB",
        "status": "RUNNING",
        "runtime_status": "ONLINE",
        "qlib_installed": True,
        "qlib_version": getattr(qlib, "__version__", "unknown"),
        "port": QLIB_BACKEND_PORT,
        "pid": os.getpid(),
        "models": registry,
        "active_models_count": sum(1 for v in registry.values() if v.get("available")),
        "active_training_job": active_training,
        "last_recommendation": last_rec,
        "scheduler_status": "RUNNING" if sched.running else "STOPPED",
        "scheduler_job_count": len(scheduled_jobs),
        "scheduler_jobs": scheduled_jobs,
        "timestamp": datetime.now().isoformat()
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.qlib_server:app", host="0.0.0.0", port=QLIB_BACKEND_PORT, reload=False)
