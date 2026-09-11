"""
RESEARCH TELEMETRY & EVENT BROADCASTING SYSTEM
==============================================
Canonical event model, real execution stage definitions, thread-safe
in-memory ring buffer (last 500 events), MasterLogger SQLite persistence,
and async subscriber distribution for real-time SSE telemetry streaming.
"""

import time
import json
import asyncio
import logging
import threading
from enum import Enum
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Dict, Any, List, Optional, Set

from app.analytics.master_logger import MasterLogger

logger = logging.getLogger(__name__)

class ResearchStage(str, Enum):
    QUEUED = "QUEUED"
    DATA_LOADING = "DATA_LOADING"
    DATA_VALIDATION = "DATA_VALIDATION"
    FEATURE_GENERATION = "FEATURE_GENERATION"
    TARGET_GENERATION = "TARGET_GENERATION"
    MODEL_TRAINING = "MODEL_TRAINING"
    PREDICTION = "PREDICTION"
    VALIDATION = "VALIDATION"
    PORTFOLIO_SIMULATION = "PORTFOLIO_SIMULATION"
    COST_ANALYSIS = "COST_ANALYSIS"
    WALK_FORWARD = "WALK_FORWARD"
    REGIME_ANALYSIS = "REGIME_ANALYSIS"
    GOVERNANCE = "GOVERNANCE"
    CANDIDATE_EVALUATION = "CANDIDATE_EVALUATION"
    LEDGER_COMMIT = "LEDGER_COMMIT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class TelemetryEventType(str, Enum):
    # Mission lifecycle
    MISSION_STARTED = "MISSION_STARTED"
    MISSION_PAUSED = "MISSION_PAUSED"
    MISSION_RESUMED = "MISSION_RESUMED"
    MISSION_STOPPED = "MISSION_STOPPED"
    MISSION_COMPLETED = "MISSION_COMPLETED"
    
    # Experiment lifecycle
    EXPERIMENT_QUEUED = "EXPERIMENT_QUEUED"
    EXPERIMENT_STARTED = "EXPERIMENT_STARTED"
    EXPERIMENT_COMPLETED = "EXPERIMENT_COMPLETED"
    EXPERIMENT_FAILED = "EXPERIMENT_FAILED"
    EXPERIMENT_REJECTED = "EXPERIMENT_REJECTED"
    EXPERIMENT_SHORTLISTED = "EXPERIMENT_SHORTLISTED"
    CANDIDATE_FROZEN = "CANDIDATE_FROZEN"
    
    # Stage lifecycle
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_PROGRESS = "STAGE_PROGRESS"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    
    # Telemetry updates
    METRIC_UPDATE = "METRIC_UPDATE"
    GOVERNANCE_RESULT = "GOVERNANCE_RESULT"
    TIMEOUT_WARNING = "TIMEOUT_WARNING"
    TIMEOUT_TERMINATED = "TIMEOUT_TERMINATED"
    RUNTIME_SNAPSHOT = "RUNTIME_SNAPSHOT"
    HEARTBEAT = "HEARTBEAT"

@dataclass
class TelemetryEvent:
    event_id: int
    timestamp: str
    mission_id: str
    event_type: str
    experiment_id: Optional[str] = None
    worker_id: Optional[str] = None
    stage: Optional[str] = None
    stage_progress: Optional[Dict[str, Any]] = None
    metrics: Optional[Dict[str, Any]] = None
    config_hash: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_sse_format(self) -> str:
        """Formats event as a standard Server-Sent Event block."""
        data_json = json.dumps(self.to_dict())
        return f"id: {self.event_id}\nevent: {self.event_type}\ndata: {data_json}\n\n"

class TelemetryBroadcaster:
    """
    Thread-safe event broadcaster and historical ring buffer for live telemetry.
    Maintains:
    1. Monotonic event counter.
    2. Ring buffer of the last 500 events for fast replay on client reconnect.
    3. Multi-subscriber asyncio queues for live SSE dispatch.
    4. Durable write-through to MasterLogger / SQLite.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(TelemetryBroadcaster, cls).__new__(cls)
                cls._instance._init_broadcaster()
            return cls._instance

    def _init_broadcaster(self):
        self.ring_buffer_capacity = 500
        self.ring_buffer: List[TelemetryEvent] = []
        self.event_counter = 0
        self.subscribers: Set[asyncio.Queue] = set()
        self.subscribers_lock = threading.Lock()
        self.main_loop: Optional[asyncio.AbstractEventLoop] = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Sets the active asyncio event loop for thread-safe cross-thread scheduling."""
        self.main_loop = loop

    def emit(
        self,
        mission_id: str,
        event_type: str,
        experiment_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        stage: Optional[str] = None,
        stage_progress: Optional[Dict[str, Any]] = None,
        metrics: Optional[Dict[str, Any]] = None,
        config_hash: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None
    ) -> TelemetryEvent:
        """
        Emits a new telemetry event atomically across:
        - Monotonic ID assignment
        - In-memory ring buffer
        - MasterLogger SQLite durable store
        - Active SSE subscriber queues
        """
        with self._lock:
            self.event_counter += 1
            ev_id = self.event_counter

            now_iso = datetime.now().isoformat()
            event = TelemetryEvent(
                event_id=ev_id,
                timestamp=now_iso,
                mission_id=mission_id,
                event_type=event_type,
                experiment_id=experiment_id,
                worker_id=worker_id,
                stage=stage,
                stage_progress=stage_progress,
                metrics=metrics,
                config_hash=config_hash,
                payload=payload or {}
            )

            # 1. Store in ring buffer
            self.ring_buffer.append(event)
            if len(self.ring_buffer) > self.ring_buffer_capacity:
                self.ring_buffer.pop(0)

            # 2. Notify active SSE subscriber queues in strict monotonic order
            self._dispatch_to_subscribers(event)

        # 3. Persist to MasterLogger / SQLite for durable history outside lock
        try:
            details = {
                "event_id": event.event_id,
                "worker_id": worker_id,
                "stage": stage,
                "stage_progress": stage_progress,
                "metrics": metrics,
                "config_hash": config_hash,
                "config_hash_short": config_hash[:8] if config_hash else None,
                **(payload or {})
            }
            MasterLogger.log_event(
                category="RESEARCH",
                event_type=event_type,
                message=f"[{event_type}] Mission: {mission_id}" + (f" | Exp: {experiment_id}" if experiment_id else "") + (f" | Stage: {stage}" if stage else ""),
                universe=payload.get("universe") if payload else None,
                details=details
            )
        except Exception as e:
            logger.debug(f"[TelemetryBroadcaster] MasterLogger durable log error (non-fatal): {e}")

        return event

    def _dispatch_to_subscribers(self, event: TelemetryEvent):
        """Pushes event into all active asyncio.Queue instances thread-safely."""
        with self.subscribers_lock:
            dead_subs = set()
            for q in self.subscribers:
                try:
                    if self.main_loop and self.main_loop.is_running():
                        self.main_loop.call_soon_threadsafe(q.put_nowait, event)
                    else:
                        q.put_nowait(event)
                except asyncio.QueueFull:
                    dead_subs.add(q)
                except Exception:
                    dead_subs.add(q)
            if dead_subs:
                self.subscribers.difference_update(dead_subs)

    def subscribe(self) -> asyncio.Queue:
        """Registers a new SSE listener queue."""
        q = asyncio.Queue(maxsize=100)
        with self.subscribers_lock:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        """Unregisters an SSE listener queue."""
        with self.subscribers_lock:
            self.subscribers.discard(q)

    def get_events_since(
        self,
        mission_id: Optional[str] = None,
        last_event_id: int = 0,
        limit: int = 200
    ) -> List[TelemetryEvent]:
        """
        Retrieves missed events for reconnecting clients.
        Checks ring buffer first, filters by mission_id and last_event_id.
        """
        with self._lock:
            matched = [
                e for e in self.ring_buffer
                if e.event_id > last_event_id and (mission_id is None or e.mission_id == mission_id)
            ]
            return matched[-limit:]

# Singleton instance accessor
telemetry_broadcaster = TelemetryBroadcaster()

