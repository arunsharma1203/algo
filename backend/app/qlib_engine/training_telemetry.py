"""
QLIB TRAINING TELEMETRY & LIVE ETA ENGINE
=========================================
Tracks and streams genuine, real-time training progress, resource usage,
and honest remaining ETA across 10 discrete quantitative stages.

10 Discrete Stages:
1. INITIALIZING (0%)
2. DISCOVERING_UNIVERSE (10%)
3. VALIDATING_DATA (20%)
4. SYNCING_QLIB_DATA (30%)
5. BUILDING_DATASET (45%)
6. GENERATING_FEATURES (60%)
7. TRAINING_MODEL (75%)
8. VALIDATING_MODEL (85%)
9. SAVING_ARTIFACT (95%)
10. COMPLETE (100%)
"""

import os
import time
import json
import psutil
import logging
import threading
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

STAGES = [
    ("DISCOVERY", 10.0, "Dynamically discovering and validating instruments from canonical database"),
    ("DATA_VALIDATION", 20.0, "Validating data depth (>=1,200 bars), calendar continuity, and pricing integrity"),
    ("QLIB_SYNC", 30.0, "Ensuring Qlib binary provider (*.day.bin features and calendar) is synchronized"),
    ("DATASET_BUILD", 40.0, "Constructing chronological temporal splits (Train/Val/Test) with purge and embargo"),
    ("FEATURE_GENERATION", 55.0, "Computing quantitative feature matrix (Alpha158/Alpha360 expressions)"),
    ("TRAINING", 70.0, "Fitting quantitative estimator on training set with validation early stopping"),
    ("VALIDATION", 80.0, "Evaluating model out-of-sample on locked test segment"),
    ("BACKTEST", 88.0, "Simulating chronological portfolio execution with 25 bps realistic friction"),
    ("METRICS", 93.0, "Computing true peak-to-trough drawdown, Sharpe, profit factor, and decile spreads"),
    ("ARTIFACT", 97.0, "Registering immutable candidate artifact and feature manifest in Challenger Vault"),
    ("COMPLETE", 100.0, "Training complete. Model artifact frozen in Challenger Vault.")
]

STAGE_ALIASES = {
    "INITIALIZING": "DISCOVERY",
    "DISCOVERING_UNIVERSE": "DISCOVERY",
    "VALIDATING_DATA": "DATA_VALIDATION",
    "SYNCING_QLIB_DATA": "QLIB_SYNC",
    "BUILDING_DATASET": "DATASET_BUILD",
    "GENERATING_FEATURES": "FEATURE_GENERATION",
    "TRAINING_MODEL": "TRAINING",
    "VALIDATING_MODEL": "VALIDATION",
    "SAVING_ARTIFACT": "ARTIFACT"
}

TRAINING_STAGES = [s[0] for s in STAGES]


class TrainingTelemetryManager:
    """
    Thread-safe telemetry broadcaster for Qlib training operations.
    Maintains current job state, computes empirical ETA, tracks process memory,
    and supports Server-Sent Events (SSE) streaming.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(TrainingTelemetryManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._state_lock = threading.Lock()
        self._subscribers: List[asyncio.Queue] = []
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._proc = psutil.Process(os.getpid())
        self._current_telemetry: Dict[str, Any] = {
            "job_id": None,
            "status": "IDLE",
            "strategy": None,
            "universe_name": None,
            "instruments_count": 0,
            "stage": "IDLE",
            "stage_index": 0,
            "total_stages": len(STAGES),
            "stage_description": "No active training job",
            "progress_pct": 0.0,
            "elapsed_seconds": 0.0,
            "estimated_remaining_seconds": None,
            "estimated_total_seconds": None,
            "is_eta_uncertain": True,
            "memory_rss_mb": round(self._proc.memory_info().rss / (1024 * 1024), 1),
            "peak_memory_rss_mb": round(self._proc.memory_info().rss / (1024 * 1024), 1),
            "rows_processed": 0,
            "features_count": 0,
            "error": None,
            "result": None,
            "updated_at": datetime.now().isoformat()
        }
        self._start_time: Optional[float] = None
        self._peak_rss_mb: float = self._current_telemetry["memory_rss_mb"]
        self._initialized = True
        self._hydrate_from_db()

    def _hydrate_from_db(self):
        """Hydrates latest job from SQLite if available."""
        try:
            from app.qlib_engine.training_job_tracker import TrainingJobTracker
            latest = TrainingJobTracker.get_active_job() or TrainingJobTracker.get_latest_job()
            active = TrainingJobTracker.get_active_job()
            latest = active or TrainingJobTracker.get_latest_job()
            if latest:
                eff_status = latest.get("status", "IDLE")
                if not active and eff_status in ("RUNNING", "VALIDATING"):
                    eff_status = "CANCELLED"

                with self._state_lock:
                    self._current_telemetry.update({
                        "job_id": latest.get("job_id"),
                        "status": latest.get("status", "IDLE"),
                        "status": eff_status,
                        "strategy": latest.get("strategy"),
                        "universe_name": latest.get("universe"),
                        "instruments_count": latest.get("instruments_count", 0),
                        "stage": latest.get("stage", "IDLE"),
                        "stage_index": latest.get("stage_index", 0),
                        "total_stages": latest.get("total_stages", len(STAGES)),
                        "stage_description": latest.get("stage_description", "No active training job"),
                        "progress_pct": latest.get("progress_pct", 0.0),
                        "elapsed_seconds": latest.get("elapsed_seconds", 0.0),
                        "estimated_remaining_seconds": latest.get("estimated_remaining_seconds"),
                        "estimated_total_seconds": latest.get("estimated_total_seconds"),
                        "is_eta_uncertain": bool(latest.get("is_eta_uncertain", 1)),
                        "memory_rss_mb": latest.get("memory_rss_mb") or self.get_current_rss_mb(),
                        "peak_memory_rss_mb": latest.get("peak_memory_rss_mb") or self.get_current_rss_mb(),
                        "rows_processed": latest.get("rows_processed", 0),
                        "features_count": latest.get("features_count", 0),
                        "error": latest.get("error"),
                        "result": latest.get("metrics"),
                        "candidate_model_id": latest.get("candidate_model_id"),
                        "updated_at": latest.get("updated_at", datetime.now().isoformat())
                    })
                    if eff_status in ("RUNNING", "VALIDATING") and latest.get("started_at"):
                        try:
                            st_dt = datetime.fromisoformat(latest["started_at"])
                            self._start_time = st_dt.timestamp()
                        except Exception:
                            self._start_time = time.time()
                    else:
                        self._start_time = None
        except Exception as e:
            logger.warning(f"[TrainingTelemetry] Could not hydrate from DB: {e}")

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        self._event_loop = loop

    def get_current_rss_mb(self) -> float:
        try:
            rss = self._proc.memory_info().rss / (1024 * 1024)
            if rss > self._peak_rss_mb:
                self._peak_rss_mb = rss
            return round(rss, 1)
        except Exception:
            return 0.0

    def start_job(
        self,
        job_id: str,
        strategy: str,
        universe_name: str,
        instruments_count: int = 0,
        feature_family: str = "Alpha158",
        model_family: str = "LGBModel"
    ) -> Dict[str, Any]:
        with self._state_lock:
            self._start_time = time.time()
            self._peak_rss_mb = self.get_current_rss_mb()
            self._current_telemetry = {
                "job_id": job_id,
                "status": "RUNNING",
                "strategy": strategy,
                "universe_name": universe_name,
                "instruments_count": instruments_count,
                "feature_family": feature_family,
                "model_family": model_family,
                "stage": "INITIALIZING",
                "stage_index": 1,
                "total_stages": len(STAGES),
                "stage_description": STAGES[0][2],
                "progress_pct": STAGES[0][1],
                "elapsed_seconds": 0.0,
                "estimated_remaining_seconds": None,
                "estimated_total_seconds": None,
                "is_eta_uncertain": True,
                "memory_rss_mb": self._peak_rss_mb,
                "peak_memory_rss_mb": self._peak_rss_mb,
                "rows_processed": 0,
                "features_count": 0,
                "error": None,
                "result": None,
                "candidate_model_id": None,
                "updated_at": datetime.now().isoformat()
            }
            telemetry_copy = dict(self._current_telemetry)

        # Persist to SQLite
        try:
            from app.qlib_engine.training_job_tracker import TrainingJobTracker
            TrainingJobTracker.create_job(
                job_id=job_id,
                strategy=strategy,
                universe=universe_name,
                feature_family=feature_family,
                model_family=model_family,
                instruments_count=instruments_count
            )
        except Exception as e:
            logger.error(f"[TrainingTelemetry] Could not persist start_job to DB: {e}")

        self._broadcast(telemetry_copy)
        return telemetry_copy

    def update_stage(
        self,
        stage_name: str,
        stage_description: Optional[str] = None,
        progress_pct: Optional[float] = None,
        rows_processed: Optional[int] = None,
        features_count: Optional[int] = None,
        instruments_count: Optional[int] = None
    ) -> Dict[str, Any]:
        with self._state_lock:
            if self._start_time is None:
                self._start_time = time.time()
            elapsed = max(0.1, round(time.time() - self._start_time, 1))

            # Normalize stage name using aliases if needed
            normalized_stage = STAGE_ALIASES.get(stage_name, stage_name)

            # Match stage index and default progress
            stage_idx = 1
            default_progress = 10.0
            default_desc = stage_description or ""
            for idx, (s_name, s_pct, s_desc) in enumerate(STAGES, start=1):
                if s_name == normalized_stage:
                    stage_idx = idx
                    default_progress = s_pct
                    if not stage_description:
                        default_desc = s_desc
                    break

            effective_pct = progress_pct if progress_pct is not None else default_progress
            effective_pct = min(100.0, max(0.0, effective_pct))

            # Honest ETA Calculation
            is_uncertain = effective_pct < 40.0
            if effective_pct > 5.0 and effective_pct < 99.0:
                est_total = round((elapsed / (effective_pct / 100.0)), 1)
                est_rem = max(1.0, round(est_total - elapsed, 1))
            elif effective_pct >= 99.0:
                est_total = elapsed
                est_rem = 0.0
                is_uncertain = False
            else:
                est_total = None
                est_rem = None

            rss = self.get_current_rss_mb()
            if instruments_count is not None:
                self._current_telemetry["instruments_count"] = instruments_count
            if rows_processed is not None:
                self._current_telemetry["rows_processed"] = rows_processed
            if features_count is not None:
                self._current_telemetry["features_count"] = features_count

            self._current_telemetry.update({
                "status": "RUNNING",
                "stage": stage_name,
                "stage_index": stage_idx,
                "stage_description": default_desc,
                "progress_pct": round(effective_pct, 1),
                "elapsed_seconds": elapsed,
                "estimated_remaining_seconds": est_rem,
                "estimated_total_seconds": est_total,
                "is_eta_uncertain": is_uncertain,
                "memory_rss_mb": rss,
                "peak_memory_rss_mb": round(self._peak_rss_mb, 1),
                "updated_at": datetime.now().isoformat()
            })
            telemetry_copy = dict(self._current_telemetry)

        # Persist to SQLite
        try:
            from app.qlib_engine.training_job_tracker import TrainingJobTracker
            if self._current_telemetry.get("job_id"):
                TrainingJobTracker.update_progress(
                    job_id=self._current_telemetry["job_id"],
                    stage=stage_name,
                    stage_index=stage_idx,
                    total_stages=len(STAGES),
                    stage_description=default_desc,
                    progress_pct=effective_pct,
                    elapsed_seconds=elapsed,
                    estimated_remaining_seconds=est_rem,
                    estimated_total_seconds=est_total,
                    is_eta_uncertain=is_uncertain,
                    memory_rss_mb=rss,
                    peak_memory_rss_mb=self._peak_rss_mb,
                    rows_processed=self._current_telemetry.get("rows_processed", 0),
                    features_count=self._current_telemetry.get("features_count", 0),
                    instruments_count=self._current_telemetry.get("instruments_count")
                )
        except Exception as e:
            logger.error(f"[TrainingTelemetry] Could not persist update_stage to DB: {e}")

        self._broadcast(telemetry_copy)
        return telemetry_copy

    def complete_job(self, result: Dict[str, Any], candidate_model_id: Optional[str] = None) -> Dict[str, Any]:
        with self._state_lock:
            elapsed = round(time.time() - (self._start_time or time.time()), 1)
            rss = self.get_current_rss_mb()
            model_id = candidate_model_id or (result.get("model_id") if isinstance(result, dict) else None)
            self._current_telemetry.update({
                "status": "COMPLETED",
                "stage": "COMPLETE",
                "stage_index": len(STAGES),
                "stage_description": STAGES[-1][2],
                "progress_pct": 100.0,
                "elapsed_seconds": elapsed,
                "estimated_remaining_seconds": 0.0,
                "estimated_total_seconds": elapsed,
                "is_eta_uncertain": False,
                "memory_rss_mb": rss,
                "peak_memory_rss_mb": round(self._peak_rss_mb, 1),
                "result": result,
                "candidate_model_id": model_id,
                "error": None,
                "updated_at": datetime.now().isoformat()
            })
            telemetry_copy = dict(self._current_telemetry)

        # Persist to SQLite
        try:
            from app.qlib_engine.training_job_tracker import TrainingJobTracker
            if self._current_telemetry.get("job_id"):
                TrainingJobTracker.complete_job(
                    job_id=self._current_telemetry["job_id"],
                    candidate_model_id=model_id or "unknown",
                    metrics=result,
                    elapsed_seconds=elapsed
                )
        except Exception as e:
            logger.error(f"[TrainingTelemetry] Could not persist complete_job to DB: {e}")

        self._broadcast(telemetry_copy)
        return telemetry_copy

    def fail_job(self, error_message: str) -> Dict[str, Any]:
        with self._state_lock:
            elapsed = round(time.time() - (self._start_time or time.time()), 1)
            rss = self.get_current_rss_mb()
            self._current_telemetry.update({
                "status": "FAILED",
                "progress_pct": self._current_telemetry.get("progress_pct", 0.0),
                "elapsed_seconds": elapsed,
                "estimated_remaining_seconds": None,
                "is_eta_uncertain": True,
                "memory_rss_mb": rss,
                "peak_memory_rss_mb": round(self._peak_rss_mb, 1),
                "error": str(error_message),
                "updated_at": datetime.now().isoformat()
            })
            telemetry_copy = dict(self._current_telemetry)

        # Persist to SQLite
        try:
            from app.qlib_engine.training_job_tracker import TrainingJobTracker
            if self._current_telemetry.get("job_id"):
                TrainingJobTracker.fail_job(
                    job_id=self._current_telemetry["job_id"],
                    error=str(error_message),
                    elapsed_seconds=elapsed
                )
        except Exception as e:
            logger.error(f"[TrainingTelemetry] Could not persist fail_job to DB: {e}")

        self._broadcast(telemetry_copy)
        return telemetry_copy

    def record_error(self, error_message: str) -> Dict[str, Any]:
        """Convenience alias for fail_job to guarantee robust error capture."""
        return self.fail_job(str(error_message))

    def reset(self) -> Dict[str, Any]:
        """Resets in-memory telemetry to pristine IDLE state and broadcasts to clients."""
        with self._state_lock:
            self._start_time = None
            try:
                self._proc = psutil.Process(os.getpid())
                rss = self.get_current_rss_mb()
            except Exception:
                rss = 0.0
            self._current_telemetry = {
                "job_id": None,
                "status": "IDLE",
                "strategy": None,
                "universe_name": None,
                "instruments_count": 0,
                "stage": "IDLE",
                "stage_index": 0,
                "total_stages": len(STAGES),
                "stage_description": "No active training job",
                "progress_pct": 0.0,
                "elapsed_seconds": 0.0,
                "estimated_remaining_seconds": None,
                "estimated_total_seconds": None,
                "is_eta_uncertain": True,
                "memory_rss_mb": rss,
                "peak_memory_rss_mb": rss,
                "rows_processed": 0,
                "features_count": 0,
                "error": None,
                "result": None,
                "candidate_model_id": None,
                "updated_at": datetime.now().isoformat()
            }
            telemetry_copy = dict(self._current_telemetry)
        self._broadcast(telemetry_copy)
        return telemetry_copy

    def get_latest(self) -> Dict[str, Any]:
        with self._state_lock:
            return dict(self._current_telemetry)

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=100)
        with self._state_lock:
            self._subscribers.append(q)
            # Immediately enqueue the current snapshot so new subscribers have state right away
            latest_payload = json.dumps(dict(self._current_telemetry))
            try:
                q.put_nowait(latest_payload)
            except Exception:
                pass
        return q

    def unsubscribe(self, q: asyncio.Queue):
        with self._state_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self, data: Dict[str, Any]):
        json_payload = json.dumps(data)
        with self._state_lock:
            subscribers = list(self._subscribers)

        for q in subscribers:
            try:
                if self._event_loop and self._event_loop.is_running():
                    self._event_loop.call_soon_threadsafe(self._safe_put, q, json_payload)
                else:
                    q.put_nowait(json_payload)
            except Exception:
                pass

    @staticmethod
    def _safe_put(q: asyncio.Queue, payload: str):
        try:
            if q.full():
                try:
                    q.get_nowait()
                except Exception:
                    pass
            q.put_nowait(payload)
        except Exception:
            pass


# Global singleton instance
training_telemetry = TrainingTelemetryManager()
TrainingTelemetry = TrainingTelemetryManager
