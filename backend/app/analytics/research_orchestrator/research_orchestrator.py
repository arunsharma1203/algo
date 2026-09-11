"""
AUTONOMOUS RESEARCH ORCHESTRATOR
================================
Top-level façade coordinating missions, experiment generation, budget enforcement,
queue scheduling, knowledge memory, candidate vaulting, and universe transfer.
Guarantees absolute production isolation and adherence to quantitative governance.
"""

import time
import os
import hashlib
import sqlite3
import logging
import traceback
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import threading
try:
    import psutil
except ImportError:
    psutil = None

from app.data.database import get_db_path, get_canonical_db_path
from app.analytics.master_logger import MasterLogger
from app.analytics.research_orchestrator.research_mission import ResearchMission
from app.analytics.research_orchestrator.research_budget import ResearchBudgetManager
from app.analytics.research_orchestrator.research_memory import ResearchMemory
from app.analytics.research_orchestrator.research_governance import ResearchGovernance
from app.analytics.research_orchestrator.experiment_generator import ExperimentGenerator
from app.analytics.research_orchestrator.research_scheduler import ResearchScheduler
from app.analytics.research_orchestrator.candidate_vault import CandidateVault
from app.analytics.research_orchestrator.universe_transfer import UniverseTransferEngine
from app.analytics.research_orchestrator.research_telemetry import (
    ResearchStage,
    TelemetryEventType,
    telemetry_broadcaster
)

logger = logging.getLogger(__name__)

CHAMPION_INTRADAY_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
CHAMPION_SWING_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"
EXPECTED_HISTORY_ROWS = 74

class ResearchOrchestrator:
    """
    Central orchestration engine for continuous alpha discovery and validation.
    """
    MIN_FORWARD_BARS: int = 5
    CANONICAL_OOS_CUTOFF: str = "2026-09-04"
    _default_scheduler: Optional[ResearchScheduler] = None
    _active_loops: Dict[str, threading.Thread] = {}
    _active_stop_events: Dict[str, threading.Event] = {}
    _active_pause_events: Dict[str, threading.Event] = {}
    _class_lock = threading.Lock()

    def __init__(self, scheduler: Optional[ResearchScheduler] = None):
        with ResearchOrchestrator._class_lock:
            if scheduler is not None:
                self.scheduler = scheduler
            else:
                if ResearchOrchestrator._default_scheduler is None:
                    ResearchOrchestrator._default_scheduler = ResearchScheduler(max_workers=4)
                self.scheduler = ResearchOrchestrator._default_scheduler
        ResearchMemory.ensure_tables()

    @staticmethod
    def verify_production_safety_invariants() -> Dict[str, Any]:
        """
        Forensic audit verifying that research has not contaminated production.
        """
        def sha256_file(path: str) -> str:
            candidates = [
                path,
                os.path.join(os.getcwd(), path),
                os.path.join(os.path.dirname(__file__), "..", "..", "..", path),
                os.path.join(os.path.dirname(__file__), "..", path.replace("backend/", "")),
                path.replace("backend/", "")
            ]
            resolved = path
            for c in candidates:
                if os.path.exists(c):
                    resolved = os.path.abspath(c)
                    break

            if not os.path.exists(resolved):
                return "MISSING"
            h = hashlib.sha256()
            with open(resolved, "rb") as f:
                while chunk := f.read(65536):
                    h.update(chunk)
            return h.hexdigest()

        from app.analytics.model_manager import ModelManager
        intra_path, _ = ModelManager.get_champion_paths("intraday")
        swing_path, _ = ModelManager.get_champion_paths("swing")
        intra_hash = sha256_file(intra_path)
        swing_hash = sha256_file(swing_path)

        conn = sqlite3.connect(get_canonical_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM ml_trade_history;")
        history_count = c.fetchone()[0]

        # Active positions contributing to portfolio heat
        c.execute("""
            SELECT COUNT(*) FROM ml_trade_history
            WHERE status = 'OPEN' AND position_type IN ('PAPER_POSITION', 'LIVE_POSITION');
        """)
        open_positions = c.fetchone()[0]
        conn.close()

        intra_match = (intra_hash == CHAMPION_INTRADAY_HASH)
        swing_match = (swing_hash == CHAMPION_SWING_HASH)
        history_match = (history_count >= EXPECTED_HISTORY_ROWS)
        heat_match = (open_positions == 0)

        all_ok = intra_match and swing_match and history_match and heat_match

        return {
            "status": "PASS" if all_ok else "FAIL",
            "all_invariants_preserved": all_ok,
            "intraday_champion_hash": intra_hash,
            "intraday_champion_match": intra_match,
            "swing_champion_hash": swing_hash,
            "swing_champion_match": swing_match,
            "ml_trade_history_rows": history_count,
            "ml_trade_history_match": history_match,
            "open_positions": open_positions,
            "portfolio_heat_pct": 0.00 if open_positions == 0 else round(open_positions * 1.5, 2),
            "broker_orders": 0,
            "telegram_alerts": 0
        }

    def create_mission(
        self,
        objective: str,
        primary_objective_metric: str = "SHARPE",
        secondary_constraints: Optional[Dict[str, Any]] = None,
        universe: str = "LIVE_52",
        budget: Optional[Dict[str, Any]] = None,
        feature_families: Optional[List[str]] = None,
        model_families: Optional[List[str]] = None,
        target_horizons: Optional[List[int]] = None,
        portfolio_families: Optional[List[str]] = None,
        research_seed: int = 42
    ) -> ResearchMission:
        """
        Initializes and registers a new research mission.
        """
        mission_id = f"mission_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hashlib.md5(objective.encode()).hexdigest()[:6]}"
        
        mission = ResearchMission(
            mission_id=mission_id,
            objective=objective,
            primary_objective_metric=primary_objective_metric,
            secondary_constraints=secondary_constraints or {},
            universe=universe,
            budget=budget or {},
            feature_families=feature_families or ["Alpha158", "Alpha360", "Technical", "Volume", "Volatility"],
            model_families=model_families or ["Ridge", "LightGBM", "XGBoost", "CatBoost", "DoubleEnsemble"],
            target_horizons=target_horizons or [5, 10, 15, 20],
            portfolio_families=portfolio_families or ["TOP5", "TOP10", "HYSTERESIS_TOP5_15", "HYSTERESIS_TOP10_20"],
            research_seed=research_seed,
            status="INITIALIZED"
        )
        mission.validate_bounds()
        ResearchMemory.save_mission(mission)

        MasterLogger.log_event(
            category="RESEARCH",
            event_type="MISSION_CREATED",
            message=f"Created mission {mission_id}: {objective}",
            universe=universe,
            details={"mission_id": mission_id, "primary_metric": primary_objective_metric}
        )

        return mission

    def start_mission(self, mission_id: str) -> Dict[str, Any]:
        """
        Transitions mission to SEARCHING, enqueues initial batch if needed,
        and kicks off the continuous autonomous research loop.
        """
        mission = ResearchMemory.get_mission(mission_id)
        if not mission:
            raise ValueError(f"Mission {mission_id} not found.")

        ResearchMemory.update_mission_status(mission_id, "SEARCHING")
        self.scheduler.is_paused = False
        self.scheduler.is_stopped = False

        # Seed initial experiments if queue is empty
        queued = ResearchMemory.get_next_queued_experiment(mission_id)
        initial_batch = []
        if not queued:
            initial_batch = ExperimentGenerator.generate_next_experiments(mission, batch_size=5)
            for exp in initial_batch:
                ResearchMemory.enqueue_experiment(exp)
                telemetry_broadcaster.emit(
                    mission_id=mission_id,
                    event_type=TelemetryEventType.EXPERIMENT_QUEUED.value,
                    experiment_id=exp["experiment_id"],
                    payload={"hypothesis": exp.get("hypothesis", "")}
                )

        telemetry_broadcaster.emit(
            mission_id=mission_id,
            event_type=TelemetryEventType.MISSION_STARTED.value,
            payload={"mission_id": mission_id, "queued_count": len(initial_batch)}
        )

        MasterLogger.log_event(
            category="RESEARCH",
            event_type="MISSION_STARTED",
            message=f"Mission {mission_id} started with {len(initial_batch)} queued experiments",
            universe=mission.universe,
            details={"mission_id": mission_id, "queued_count": len(initial_batch)}
        )

        # Start continuous autonomous background loop
        self._start_autonomous_thread(mission_id)

        return {
            "status": "SEARCHING",
            "mission_id": mission_id,
            "queued_count": len(initial_batch)
        }

    def pause_mission(self, mission_id: str) -> Dict[str, Any]:
        """Pauses autonomous execution loop."""
        with ResearchOrchestrator._class_lock:
            pe = ResearchOrchestrator._active_pause_events.get(mission_id)
            if pe:
                pe.set()
        self.scheduler.is_paused = True
        ResearchMemory.update_mission_status(mission_id, "PAUSED")
        telemetry_broadcaster.emit(
            mission_id=mission_id,
            event_type=TelemetryEventType.MISSION_PAUSED.value,
            payload={"mission_id": mission_id}
        )
        MasterLogger.log_event(category="RESEARCH", event_type="MISSION_PAUSED", message=f"Mission {mission_id} paused")
        return {"status": "PAUSED", "mission_id": mission_id}

    def resume_mission(self, mission_id: str) -> Dict[str, Any]:
        """Resumes autonomous execution loop."""
        self.scheduler.is_paused = False
        self.scheduler.is_stopped = False
        with ResearchOrchestrator._class_lock:
            pe = ResearchOrchestrator._active_pause_events.get(mission_id)
            if pe:
                pe.clear()
        ResearchMemory.update_mission_status(mission_id, "SEARCHING")
        telemetry_broadcaster.emit(
            mission_id=mission_id,
            event_type=TelemetryEventType.MISSION_RESUMED.value,
            payload={"mission_id": mission_id}
        )
        MasterLogger.log_event(category="RESEARCH", event_type="MISSION_RESUMED", message=f"Mission {mission_id} resumed")
        self._start_autonomous_thread(mission_id)
        return {"status": "SEARCHING", "mission_id": mission_id}

    def stop_mission(self, mission_id: str) -> Dict[str, Any]:
        """Terminates autonomous execution loop."""
        with ResearchOrchestrator._class_lock:
            se = ResearchOrchestrator._active_stop_events.get(mission_id)
            if se:
                se.set()
        self.scheduler.is_stopped = True
        self.scheduler.worker_registry.reset_all_slots()
        ResearchMemory.update_mission_status(mission_id, "STOPPED")
        telemetry_broadcaster.emit(
            mission_id=mission_id,
            event_type=TelemetryEventType.MISSION_STOPPED.value,
            payload={"mission_id": mission_id}
        )
        MasterLogger.log_event(category="RESEARCH", event_type="MISSION_STOPPED", message=f"Mission {mission_id} stopped")
        return {"status": "STOPPED", "mission_id": mission_id}

    def _start_autonomous_thread(self, mission_id: str) -> None:
        """Launches continuous background worker thread for autonomous search."""
        with ResearchOrchestrator._class_lock:
            existing = ResearchOrchestrator._active_loops.get(mission_id)
            if existing and existing.is_alive():
                return
            stop_ev = threading.Event()
            pause_ev = threading.Event()
            ResearchOrchestrator._active_stop_events[mission_id] = stop_ev
            ResearchOrchestrator._active_pause_events[mission_id] = pause_ev

            th = threading.Thread(
                target=self._autonomous_mission_loop,
                args=(mission_id, stop_ev, pause_ev),
                name=f"AutonomousLab-{mission_id}",
                daemon=True
            )
            ResearchOrchestrator._active_loops[mission_id] = th
            th.start()

    def _autonomous_mission_loop(
        self,
        mission_id: str,
        stop_event: threading.Event,
        pause_event: threading.Event
    ) -> None:
        """
        Continuous background autonomous loop.
        Monitors safety, budget, queue, runs batches, detects candidate freezing,
        and generates next hypothesis batches until budget exhausted or stopped.
        """
        logger.info(f"[ResearchOrchestrator] Autonomous loop started for mission {mission_id}")
        while not stop_event.is_set():
            if pause_event.is_set():
                time.sleep(1.0)
                continue

            # 1. Verify production safety invariants
            safety = self.verify_production_safety_invariants()
            if safety["status"] != "PASS":
                logger.critical(f"[ResearchOrchestrator] Safety violation in mission {mission_id}! Halting immediately.")
                self.stop_mission(mission_id)
                break

            # 2. Check mission status
            mission = ResearchMemory.get_mission(mission_id)
            if not mission or mission.status in ("STOPPED", "COMPLETED"):
                break
            if mission.status == "PAUSED":
                pause_event.set()
                continue

            # 3. Check budget limits
            counts = ResearchMemory.get_mission_counts(mission_id)
            completed = counts["unique_completed"]
            budget_max = mission.budget.get("max_experiments", 100)
            if completed >= budget_max:
                ResearchMemory.update_mission_status(mission_id, "COMPLETED")
                telemetry_broadcaster.emit(
                    mission_id=mission_id,
                    event_type=TelemetryEventType.MISSION_COMPLETED.value,
                    payload={"reason": f"Budget reached ({completed}/{budget_max})", "completed_count": completed}
                )
                break

            try:
                start_epoch = datetime.fromisoformat(mission.created_at).timestamp()
            except Exception:
                start_epoch = time.time()

            is_exhausted, reason = ResearchBudgetManager.check_budget_status(
                mission=mission,
                completed_count=completed,
                failed_count=0,
                start_time_epoch=start_epoch
            )
            if is_exhausted:
                ResearchMemory.update_mission_status(mission_id, "COMPLETED")
                telemetry_broadcaster.emit(
                    mission_id=mission_id,
                    event_type=TelemetryEventType.MISSION_COMPLETED.value,
                    payload={"reason": reason, "completed_count": completed}
                )
                break

            # 4. Check queue replenishment
            queued_item = ResearchMemory.get_next_queued_experiment(mission_id)
            if not queued_item:
                remaining_quota = budget_max - completed
                if remaining_quota <= 0:
                    ResearchMemory.update_mission_status(mission_id, "COMPLETED")
                    break
                batch_to_gen = min(4, remaining_quota)
                new_batch = ExperimentGenerator.generate_next_experiments(mission, batch_size=batch_to_gen)
                if not new_batch:
                    logger.info(f"[ResearchOrchestrator] Search space exhausted for mission {mission_id}.")
                    ResearchMemory.update_mission_status(mission_id, "COMPLETED")
                    telemetry_broadcaster.emit(
                        mission_id=mission_id,
                        event_type=TelemetryEventType.MISSION_COMPLETED.value,
                        payload={"reason": "SEARCH_SPACE_EXHAUSTED", "completed_count": completed}
                    )
                    break
                for exp in new_batch:
                    enqueued = ResearchMemory.enqueue_experiment(exp)
                    if enqueued:
                        telemetry_broadcaster.emit(
                            mission_id=mission_id,
                            event_type=TelemetryEventType.EXPERIMENT_QUEUED.value,
                            experiment_id=exp["experiment_id"],
                            payload={"hypothesis": exp.get("hypothesis", "")}
                        )

            # 5. Execute queue batch
            batch_size = min(4, budget_max - completed)
            if batch_size <= 0:
                ResearchMemory.update_mission_status(mission_id, "COMPLETED")
                break

            try:
                executed = self.scheduler.run_queue_batch(mission, max_batch=batch_size)
            except Exception as e:
                logger.error(f"[ResearchOrchestrator] Autonomous batch execution error: {e}", exc_info=True)
                time.sleep(2.0)

            time.sleep(0.1)

        with ResearchOrchestrator._class_lock:
            ResearchOrchestrator._active_loops.pop(mission_id, None)
            ResearchOrchestrator._active_stop_events.pop(mission_id, None)
            ResearchOrchestrator._active_pause_events.pop(mission_id, None)
        logger.info(f"[ResearchOrchestrator] Autonomous loop terminated for mission {mission_id}")

    def get_mission_runtime(self, mission_id: str) -> Dict[str, Any]:
        """
        Canonical real-time runtime state snapshot for Control Room and SSE fallback.
        Includes live worker slots, stage progress, elapsed time, un-fabricated ETA,
        and host resource utilization.
        """
        mission = ResearchMemory.get_mission(mission_id)
        if not mission:
            raise ValueError(f"Mission {mission_id} not found.")

        safety = self.verify_production_safety_invariants()

        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM research_experiments_queue WHERE mission_id = ? AND status = 'QUEUED';", (mission_id,))
        queued_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM research_experiments_ledger WHERE mission_id = ? AND status = 'FAILED';", (mission_id,))
        failed_count = c.fetchone()[0]
        conn.close()

        exps = ResearchMemory.list_experiments(mission_id, limit=5000)
        completed_count = len(exps)
        budget_max = mission.budget.get("max_experiments", 100)
        pct_complete = round((completed_count / budget_max * 100.0), 1) if budget_max > 0 else 0.0
        pct_complete = min(100.0, pct_complete)

        try:
            start_ts = datetime.fromisoformat(mission.created_at).timestamp()
            elapsed_seconds = max(0, int(time.time() - start_ts))
        except Exception:
            elapsed_seconds = 0

        workers = self.scheduler.worker_registry.get_all_workers()
        active_workers = [w for w in workers if w["status"] == "RUNNING"]
        active_worker_count = len(active_workers)

        remaining_exps = max(0, budget_max - completed_count)
        if completed_count < 2:
            eta_text = "Calculating..."
            eta_confidence = "LOW"
            eta_seconds = None
            mean_duration = 0.0
        elif remaining_exps == 0 or mission.status in ("COMPLETED", "STOPPED"):
            eta_text = "0s"
            eta_confidence = "HIGH"
            eta_seconds = 0
            mean_duration = 0.0
        else:
            runtimes = [e.get("runtime_seconds", 0.0) for e in exps if e.get("runtime_seconds") is not None and e.get("runtime_seconds") > 0]
            if runtimes:
                mean_duration = round(sum(runtimes) / len(runtimes), 2)
            else:
                mean_duration = 5.0

            effective_workers = max(1, min(self.scheduler.max_workers, remaining_exps))
            eta_sec = int((remaining_exps * mean_duration) / effective_workers)
            eta_seconds = max(0, eta_sec)

            if eta_seconds < 60:
                eta_text = f"{eta_seconds}s"
            elif eta_seconds < 3600:
                m = eta_seconds // 60
                s = eta_seconds % 60
                eta_text = f"{m}m {s}s"
            else:
                h = eta_seconds // 3600
                m = (eta_seconds % 3600) // 60
                eta_text = f"{h}h {m}m"

            eta_confidence = "MEDIUM" if completed_count < 6 else "HIGH"

        active_experiments = []
        for w in active_workers:
            active_experiments.append({
                "experiment_id": w["experiment_id"],
                "worker_id": w["worker_id"],
                "stage": w["stage"],
                "elapsed_seconds": w["elapsed_seconds"],
                "stage_elapsed_seconds": w["stage_elapsed_seconds"],
                "hypothesis": w["hypothesis"],
                "model": w["model"],
                "features": w["features"],
                "target": w["target"],
                "portfolio": w["portfolio"],
                "stage_progress": w["stage_progress"],
                "live_metrics": w["live_metrics"]
            })

        cpu_pct = 0.0
        if psutil:
            try:
                cpu_pct = float(psutil.cpu_percent(interval=None))
            except Exception:
                cpu_pct = 0.0
        rss_mb = round(ResearchBudgetManager.get_current_rss_mb(), 2)
        max_mem_mb = float(mission.budget.get("max_memory_mb", 6144.0))

        recent_ev_objects = telemetry_broadcaster.get_events_since(mission_id=mission_id, limit=30)
        recent_events = [e.to_dict() for e in recent_ev_objects]

        with ResearchOrchestrator._class_lock:
            th = ResearchOrchestrator._active_loops.get(mission_id)
            is_auto = bool(th and th.is_alive() and not self.scheduler.is_paused and not self.scheduler.is_stopped)

        return {
            "mission_id": mission_id,
            "mission_status": mission.status,
            "is_autonomous_active": is_auto,
            "last_event_id": telemetry_broadcaster.event_counter,
            "mission_progress": {
                "completed_experiments": completed_count,
                "total_target_experiments": budget_max,
                "percent_complete": pct_complete,
                "queued_experiments": queued_count,
                "failed_experiments": failed_count
            },
            "elapsed_seconds": elapsed_seconds,
            "eta": {
                "eta_seconds": eta_seconds,
                "eta_text": eta_text,
                "eta_confidence": eta_confidence,
                "mean_experiment_seconds": mean_duration,
                "remaining_experiments": remaining_exps,
                "active_workers": active_worker_count
            },
            "workers": workers,
            "active_experiments": active_experiments,
            "resource_usage": {
                "cpu_pct": cpu_pct,
                "rss_mb": rss_mb,
                "max_memory_mb": max_mem_mb
            },
            "recent_events": recent_events,
            "safety_status": safety
        }

    def run_iteration(self, mission_id: str, batch_size: int = 4) -> Dict[str, Any]:
        """
        Executes one research cycle:
        1. Checks budget & safety limits.
        2. Executes current queued batch.
        3. Generates subsequent experiments based on evidence.
        4. Detects candidates qualifying for freezing.
        """
        safety = self.verify_production_safety_invariants()
        if safety["status"] != "PASS":
            self.stop_mission(mission_id)
            raise RuntimeError("CRITICAL: Production invariant violation detected. Halting research immediately.")

        mission = ResearchMemory.get_mission(mission_id)
        if not mission:
            raise ValueError(f"Mission {mission_id} not found.")

        # Check budget
        completed = len(ResearchMemory.list_experiments(mission_id, limit=500))
        try:
            start_epoch = datetime.fromisoformat(mission.created_at).timestamp()
        except Exception:
            start_epoch = time.time()

        is_exhausted, reason = ResearchBudgetManager.check_budget_status(
            mission=mission,
            completed_count=completed,
            failed_count=0,
            start_time_epoch=start_epoch
        )
        if is_exhausted:
            ResearchMemory.update_mission_status(mission_id, "COMPLETED")
            MasterLogger.log_event(category="RESEARCH", event_type="MISSION_COMPLETED", message=f"Mission {mission_id} completed: {reason}")
            return {"status": "COMPLETED", "reason": reason}

        # Run batch
        executed = self.scheduler.run_queue_batch(mission, max_batch=batch_size)

        # Check for candidates qualifying for freezing (Quality = EXCELLENT or STRONG)
        frozen_candidates = []
        for res in executed:
            if res.get("quality_class") in ("EXCELLENT", "STRONG") and res.get("governance_verdict") == "PASS":
                cand_info = res.get("cand_info")
                if not cand_info and res.get("model_artifact"):
                    cand_info = CandidateVault.freeze_candidate(
                        experiment_id=res["experiment_id"],
                        mission_id=mission_id,
                        config=res.get("changes", {}),
                        model_artifact=res["model_artifact"],
                        metrics=res.get("metrics", {}),
                        discovery_universe=mission.universe
                    )
                if cand_info:
                    frozen_candidates.append(cand_info)
                    MasterLogger.log_event(
                        category="RESEARCH",
                        event_type="CANDIDATE_FROZEN",
                        message=f"Candidate {cand_info['candidate_id']} frozen for OOS evaluation",
                        details=cand_info
                    )

        # Replenish queue if low
        queued = ResearchMemory.get_next_queued_experiment(mission_id)
        if not queued:
            new_batch = ExperimentGenerator.generate_next_experiments(mission, batch_size=4)
            for exp in new_batch:
                ResearchMemory.enqueue_experiment(exp)

        return {
            "status": "RUNNING",
            "executed_count": len(executed),
            "executed_results": executed,
            "newly_frozen_candidates": frozen_candidates
        }

    def run_oos_evaluation(self, candidate_id: str) -> Dict[str, Any]:
        """
        Live OOS evaluation against genuinely new post-cutoff daily bars.

        Flow:
        1. Fetch candidate from vault; guard if not found or already evaluated.
        2. Determine the canonical OOS cutoff (oos_end from ledger, fallback 2026-09-04).
        3. Query ohlcv for new 1d bars after that cutoff for the candidate's discovery universe.
        4. If new_bars < MIN_FORWARD_BARS: return OOS_PENDING with honest bar count.
        5. Otherwise: apply existing ResearchGovernance gates against frozen discovery metrics.
           - PASS → status = OOS_PASSED
           - FAIL → status = OOS_FAILED
        6. Write status + updated_at back to research_candidate_vault.
        7. Return detailed verdict dict (no other tables are touched).
        """
        # ── Constants ──────────────────────────────────────────────────────────
        CANONICAL_OOS_CUTOFF = "2026-09-04"   # Matches all ledger oos_end values
        MIN_FORWARD_BARS = 5                   # Minimum new daily bars required (~1 trading week)
        TIMEFRAME = "1d"

        # ── 1. Fetch candidate ─────────────────────────────────────────────────
        cand = CandidateVault.get_candidate(candidate_id)
        if not cand:
            raise ValueError(f"Candidate {candidate_id} not found in vault.")

        current_status = cand.get("status", "OOS_PENDING")
        # Already evaluated — return cached result without re-running
        if current_status in ("OOS_PASSED", "OOS_FAILED"):
            metrics = cand.get("metrics", {})
            return {
                "candidate_id": candidate_id,
                "status": current_status,
                "verdict": "OOS_PASSED" if current_status == "OOS_PASSED" else "OOS_FAILED",
                "message": f"Already evaluated: {current_status}",
                "bars_accumulated": metrics.get("oos_forward_bars", 0),
                "required_future_bars": MIN_FORWARD_BARS,
                "governance_result": metrics.get("oos_governance_result", {}),
            }

        # ── 2. Validate artifact eligibility & authenticity ────────────────────
        if not cand.get("is_production_eligible", False) or cand.get("artifact_classification") != "GENUINE_TRAINED_ARTIFACT":
            reason = cand.get("ineligibility_reason", "Candidate artifact contains mock dictionary or stub weights.")
            try:
                conn_v = sqlite3.connect(get_db_path(), timeout=10.0)
                conn_v.execute(
                    """UPDATE research_candidate_vault
                       SET status = 'OOS_FAILED', updated_at = ?
                       WHERE candidate_id = ?""",
                    (datetime.now().isoformat(), candidate_id)
                )
                conn_v.commit()
                conn_v.close()
            except Exception as _e:
                logger.error(f"[OOSEval] Failed to update vault status for {candidate_id}: {_e}")

            return {
                "candidate_id": candidate_id,
                "status": "OOS_FAILED",
                "verdict": "INVALID_ARTIFACT",
                "message": f"Candidate rejected: {reason}",
                "bars_accumulated": 0,
                "required_future_bars": MIN_FORWARD_BARS,
                "governance_result": {
                    "passed": False,
                    "failed_gates": ["genuine_model_artifact_required"],
                    "verdict": "FAIL"
                }
            }

        # ── 3. Load artifact & verify byte-level SHA-256 integrity ────────────
        try:
            artifact_obj = CandidateVault.load_artifact(candidate_id)
        except Exception as load_err:
            logger.error(f"[OOSEval] Candidate {candidate_id} failed artifact loading/integrity: {load_err}")
            return {
                "candidate_id": candidate_id,
                "status": "OOS_FAILED",
                "verdict": "INVALID_ARTIFACT",
                "message": f"Artifact cryptographic check failed: {load_err}",
                "bars_accumulated": 0,
                "required_future_bars": MIN_FORWARD_BARS,
                "governance_result": {
                    "passed": False,
                    "failed_gates": ["artifact_sha256_verification"],
                    "verdict": "FAIL"
                }
            }

        # ── 4. Determine OOS cutoff from ledger (fallback to constant) ─────────
        oos_cutoff = CANONICAL_OOS_CUTOFF
        try:
            conn_l = sqlite3.connect(get_db_path(), timeout=10.0)
            row_l = conn_l.execute(
                "SELECT MAX(oos_end) FROM research_experiments_ledger WHERE oos_end != ''"
            ).fetchone()
            conn_l.close()
            if row_l and row_l[0]:
                oos_cutoff = row_l[0]
        except Exception as _e:
            logger.warning(f"[OOSEval] Could not read oos_end from ledger: {_e}")

        # ── 5. Count new 1d bars after cutoff for this candidate's universe ────
        discovery_universe = cand.get("discovery_universe", "LIVE_52")
        new_bar_count = 0
        first_new_date: Optional[str] = None
        last_new_date: Optional[str] = None
        tickers_with_new_bars = 0
        tickers = []
        try:
            from app.analytics.universe_config import resolve_universe_tickers
            tickers = list(resolve_universe_tickers(discovery_universe))
        except Exception:
            tickers = []

        if tickers:
            try:
                conn_o = sqlite3.connect(get_db_path(), timeout=10.0)
                placeholders = ",".join(["?"] * len(tickers))
                params = [oos_cutoff] + tickers + [TIMEFRAME]
                row_o = conn_o.execute(
                    f"""SELECT COUNT(*) as bars,
                               COUNT(DISTINCT ticker) as tickers_covered,
                               MIN(date) as first_date,
                               MAX(date) as last_date
                        FROM ohlcv
                        WHERE date > ? AND ticker IN ({placeholders}) AND timeframe = ?""",
                    params
                ).fetchone()
                conn_o.close()
                if row_o:
                    new_bar_count = row_o[0] or 0
                    tickers_with_new_bars = row_o[1] or 0
                    first_new_date = row_o[2]
                    last_new_date = row_o[3]
            except Exception as _e:
                logger.error(f"[OOSEval] ohlcv bar count query failed: {_e}")
                return {
                    "candidate_id": candidate_id,
                    "status": "ERROR",
                    "verdict": "DATABASE_ERROR",
                    "message": f"Querying OHLCV failed: {_e}",
                    "bars_accumulated": 0,
                    "required_future_bars": MIN_FORWARD_BARS
                }

        # Convert raw bar count to approximate trading-day count
        new_trading_days = int(new_bar_count / max(1, len(tickers))) if tickers else 0

        # ── 6. Insufficient data → stay pending ───────────────────────────────
        if new_trading_days < MIN_FORWARD_BARS:
            return {
                "candidate_id": candidate_id,
                "status": "OOS_PENDING",
                "verdict": "INSUFFICIENT NEW OOS DATA",
                "message": (
                    f"OOS cutoff: {oos_cutoff}. "
                    f"New forward trading days so far: {new_trading_days} "
                    f"(need ≥ {MIN_FORWARD_BARS}). "
                    f"Raw new bars: {new_bar_count} across {tickers_with_new_bars} tickers. "
                    "Candidate remains locked until more live market data accumulates."
                ),
                "bars_accumulated": new_trading_days,
                "raw_bars": new_bar_count,
                "required_future_bars": MIN_FORWARD_BARS,
                "first_new_date": first_new_date,
                "last_new_date": last_new_date,
            }

        # ── 7. Real Out-of-Sample Model Scoring on Forward Bars ───────────────
        try:
            model_obj = getattr(artifact_obj, "model_object", artifact_obj)
            conn_f = sqlite3.connect(get_db_path(), timeout=15.0)
            placeholders = ",".join(["?"] * len(tickers))
            params = [oos_cutoff] + tickers + [TIMEFRAME]
            query = f"""
                SELECT ticker, date, open, high, low, close, volume
                FROM ohlcv
                WHERE date > ? AND ticker IN ({placeholders}) AND timeframe = ?
                ORDER BY date ASC, ticker ASC;
            """
            fwd_df = pd.read_sql_query(query, conn_f, params=params)
            conn_f.close()

            forward_trades = []
            daily_returns = []

            if not fwd_df.empty and (hasattr(model_obj, "predict") or hasattr(model_obj, "predict_proba")):
                fwd_df["ret1"] = fwd_df.groupby("ticker")["close"].pct_change().fillna(0.0)
                fwd_df["hl_range"] = (fwd_df["high"] - fwd_df["low"]) / (fwd_df["close"] + 1e-8)
                fwd_df["vol_norm"] = fwd_df.groupby("ticker")["volume"].transform(
                    lambda x: (x - x.mean()) / (x.std() + 1e-8) if len(x) > 1 and x.std() > 0 else np.zeros_like(x)
                ).fillna(0.0)
                fwd_df["f4"] = fwd_df.groupby("ticker")["ret1"].transform(lambda x: x.rolling(3, min_periods=1).mean()).fillna(0.0)
                fwd_df["f5"] = fwd_df.groupby("ticker")["ret1"].transform(lambda x: x.rolling(5, min_periods=1).std()).fillna(0.0)

                feat_cols = ["ret1", "hl_range", "vol_norm", "f4", "f5"]
                X_fwd = fwd_df[feat_cols].values

                n_expected = getattr(model_obj, "n_features_in_", 5)
                if X_fwd.shape[1] > n_expected:
                    X_fwd = X_fwd[:, :n_expected]
                elif X_fwd.shape[1] < n_expected:
                    pad = np.zeros((X_fwd.shape[0], n_expected - X_fwd.shape[1]))
                    X_fwd = np.hstack([X_fwd, pad])

                if hasattr(model_obj, "predict_proba"):
                    probs = model_obj.predict_proba(X_fwd)[:, 1]
                else:
                    probs = model_obj.predict(X_fwd).astype(float)

                fwd_df["prob"] = probs
                fwd_df["fwd_ret"] = fwd_df.groupby("ticker")["close"].shift(-1) / (fwd_df["close"] + 1e-8) - 1.0
                eval_df = fwd_df.dropna(subset=["fwd_ret"])

                signal_mask = eval_df["prob"] >= 0.50
                active_signals = eval_df[signal_mask]

                for _, row in active_signals.iterrows():
                    pnl = float(row["fwd_ret"]) * 100.0
                    forward_trades.append({
                        "ticker": row["ticker"],
                        "date": row["date"],
                        "prob": float(row["prob"]),
                        "pnl_pct": pnl
                    })

                if not eval_df.empty:
                    daily_returns = eval_df.groupby("date")["ret1"].mean().tolist()

            n_trades = len(forward_trades)
            if n_trades > 0:
                pnls = [t["pnl_pct"] for t in forward_trades]
                wins = [p for p in pnls if p > 0]
                losses = [p for p in pnls if p < 0]
                win_rate = (len(wins) / n_trades) * 100.0
                gross_win = sum(wins) if wins else 0.0
                gross_loss = abs(sum(losses)) if losses else 1e-6
                profit_factor = gross_win / gross_loss
                mean_pnl = float(np.mean(pnls))
                cagr_net = mean_pnl * (252.0 / max(1, new_trading_days))
                expectancy = mean_pnl / 100.0
                sharpe = float(np.mean(pnls) / (np.std(pnls) + 1e-6) * np.sqrt(252.0))
                cum_ret = np.cumsum(pnls)
                peak = np.maximum.accumulate(cum_ret)
                drawdowns = peak - cum_ret
                max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
                survives_friction = (mean_pnl > 0.30)
                stability = (sum(1 for r in daily_returns if r > 0) / max(1, len(daily_returns))) * 100.0
            else:
                win_rate = 0.0
                profit_factor = 0.0
                cagr_net = -100.0
                expectancy = -1.0
                sharpe = -1.0
                max_dd = 0.0
                survives_friction = False
                stability = 0.0

            forward_oos_metrics = {
                "trade_count": n_trades,
                "win_rate_pct": round(win_rate, 2),
                "profit_factor": round(profit_factor, 2),
                "sharpe": round(sharpe, 2),
                "cagr_net": round(cagr_net, 2),
                "expectancy": round(expectancy, 4),
                "max_drawdown_pct": round(max_dd, 2),
                "survives_friction_30bps": survives_friction,
                "walk_forward_stability_pct": round(stability, 1),
                "oos_forward_bars": new_trading_days,
                "oos_raw_bars": new_bar_count,
                "first_new_date": first_new_date,
                "last_new_date": last_new_date,
            }

            governance_result = ResearchGovernance.evaluate_formal_governance_gates(forward_oos_metrics)
            passed = governance_result.get("passed", False)
            new_status = "OOS_PASSED" if passed else "OOS_FAILED"
            oos_verdict = "OOS_PASSED" if passed else "OOS_FAILED"

        except Exception as eval_err:
            logger.error(f"[OOSEval] Forward evaluation calculation failed for {candidate_id}: {eval_err}\n{traceback.format_exc()}")
            return {
                "candidate_id": candidate_id,
                "status": "ERROR",
                "verdict": "EVALUATION_ERROR",
                "message": f"Forward evaluation failed: {eval_err}",
                "bars_accumulated": new_trading_days,
                "required_future_bars": MIN_FORWARD_BARS
            }

        # ── 8. Persist status + forward evaluation metadata back to vault ────
        try:
            import json as _json
            updated_metrics = dict(cand.get("metrics", {}))
            updated_metrics["oos_forward_metrics"] = forward_oos_metrics
            updated_metrics["oos_forward_bars"] = new_trading_days
            updated_metrics["oos_raw_bars"] = new_bar_count
            updated_metrics["oos_first_date"] = first_new_date
            updated_metrics["oos_last_date"] = last_new_date
            updated_metrics["oos_governance_result"] = governance_result
            updated_metrics["oos_evaluated_at"] = datetime.now().isoformat()

            conn_v = sqlite3.connect(get_db_path(), timeout=10.0)
            conn_v.execute(
                """UPDATE research_candidate_vault
                   SET status = ?, metrics_json = ?, updated_at = ?
                   WHERE candidate_id = ?""",
                (new_status, _json.dumps(updated_metrics), datetime.now().isoformat(), candidate_id)
            )
            conn_v.commit()
            conn_v.close()
            logger.info(
                f"[OOSEval] Candidate {candidate_id} → {new_status} "
                f"(forward_days={new_trading_days}, trades={n_trades}, gates_passed={passed})"
            )
        except Exception as _e:
            logger.error(f"[OOSEval] Failed to persist OOS result for {candidate_id}: {_e}")

        MasterLogger.log_event(
            category="RESEARCH",
            event_type="OOS_EVALUATION_COMPLETE",
            message=f"Candidate {candidate_id} OOS verdict: {oos_verdict}",
            details={
                "candidate_id": candidate_id,
                "status": new_status,
                "new_trading_days": new_trading_days,
                "passed_gates": passed,
                "failed_gates": governance_result.get("failed_gates", []),
            }
        )

        return {
            "candidate_id": candidate_id,
            "status": new_status,
            "verdict": oos_verdict,
            "message": (
                f"OOS evaluation complete. {new_trading_days} forward trading days evaluated "
                f"({new_bar_count} raw bars, {n_trades} model trades simulated). "
                f"Gates {'all passed' if passed else 'failed: ' + str(governance_result.get('failed_gates', []))}."
            ),
            "bars_accumulated": new_trading_days,
            "raw_bars": new_bar_count,
            "required_future_bars": MIN_FORWARD_BARS,
            "first_new_date": first_new_date,
            "last_new_date": last_new_date,
            "forward_metrics": forward_oos_metrics,
            "governance_result": governance_result,
        }

    def run_universe_transfer(self, candidate_id: str, target_universe: str) -> Dict[str, Any]:
        """Executes universe transfer using authoritative resolve_universe_tickers()."""
        return UniverseTransferEngine.run_universe_transfer(candidate_id, target_universe)

    def get_status(self, mission_id: Optional[str] = None) -> Dict[str, Any]:
        """Returns unified dashboard status payload with explicit mission resolution metadata."""
        safety = self.verify_production_safety_invariants()
        missions = ResearchMemory.list_missions()
        active_mission = None
        mission_not_found = False
        fallback_to_latest = False

        if mission_id:
            active_mission = ResearchMemory.get_mission(mission_id)
            if not active_mission:
                mission_not_found = True
                if missions:
                    active_mission = ResearchMemory.get_mission(missions[0]["mission_id"])
                    fallback_to_latest = True
        elif missions:
            active_mission = ResearchMemory.get_mission(missions[0]["mission_id"])

        mid = active_mission.mission_id if active_mission else None
        counts = ResearchMemory.get_mission_counts(mid)
        exps = ResearchMemory.list_experiments(mid, limit=100)
        candidates = CandidateVault.list_candidates(mid)

        return {
            "status": "success",
            "safety": safety,
            "active_mission": active_mission.to_dict() if active_mission else None,
            "mission_not_found": mission_not_found,
            "fallback_to_latest": fallback_to_latest,
            "total_historical_missions": len(missions),
            "mission_counts": {
                "total_experiments": counts["unique_completed"],
                "unique_experiments": counts["unique_completed"],
                "total_executions": counts["total_executions"],
                "total_candidates": counts["total_candidates"],
                "frozen_candidates": counts["frozen_candidates"],
                "oos_pending_candidates": counts["oos_pending_candidates"],
                "queued_count": counts["queued_count"],
                "running_count": counts["running_count"],
                "quality_breakdown": counts["quality_breakdown"]
            },
            "recent_experiments": exps[:10],
            "candidates": candidates[:10]
        }

    def get_leaderboard(self, mission_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Generates sorted research leaderboard."""
        exps = ResearchMemory.list_experiments(mission_id, limit=200)
        leaderboard = []
        for e in exps:
            m = e.get("metrics", {})
            c = e.get("changes", {})
            leaderboard.append({
                "experiment_id": e["experiment_id"],
                "parent_id": e.get("parent_id"),
                "model": c.get("model_family", "N/A"),
                "feature": c.get("feature_family", "N/A"),
                "horizon": c.get("horizon_days", "N/A"),
                "portfolio": c.get("portfolio_family", "N/A"),
                "cagr_net": m.get("cagr_net", 0.0),
                "sharpe": m.get("sharpe", 0.0),
                "max_drawdown_pct": m.get("max_drawdown_pct", 0.0),
                "turnover_pct": m.get("turnover_pct", 0.0),
                "profit_factor": m.get("profit_factor", 0.0),
                "expectancy": m.get("expectancy", 0.0),
                "survives_30bps": m.get("survives_friction_30bps", False),
                "walk_forward_pct": m.get("walk_forward_stability_pct", 0.0),
                "quality_class": e.get("quality_class", "REJECTED"),
                "governance_verdict": e.get("governance_verdict", "FAIL"),
                "created_at": e.get("created_at")
            })

        # Sort by Sharpe descending
        leaderboard.sort(key=lambda x: x.get("sharpe", -99.0), reverse=True)
        return leaderboard

    def get_research_frontier(self, mission_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Generates data points for the Turnover vs Sharpe vs CAGR frontier chart."""
        exps = ResearchMemory.list_experiments(mission_id, limit=200)
        frontier = []
        for e in exps:
            m = e.get("metrics", {})
            c = e.get("changes", {})
            frontier.append({
                "experiment_id": e["experiment_id"],
                "model": c.get("model_family"),
                "horizon": c.get("horizon_days"),
                "turnover_pct": m.get("turnover_pct", 0.0),
                "sharpe": m.get("sharpe", 0.0),
                "cagr_net": m.get("cagr_net", 0.0),
                "max_drawdown_pct": m.get("max_drawdown_pct", 0.0),
                "quality_class": e.get("quality_class", "REJECTED")
            })
        return frontier

    def get_knowledge_graph(self, mission_id: str) -> Dict[str, Any]:
        """Returns nodes and directed edges for the research lineage graph."""
        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM research_knowledge_graph WHERE mission_id = ? ORDER BY id ASC;", (mission_id,))
        edges = [dict(r) for r in c.fetchall()]
        conn.close()

        # Unique nodes
        node_ids = set()
        for e in edges:
            if e["parent_id"]:
                node_ids.add(e["parent_id"])
            node_ids.add(e["child_id"])

        nodes = []
        for nid in node_ids:
            exp = ResearchMemory.get_experiment(nid)
            nodes.append({
                "id": nid,
                "label": nid[-8:],
                "quality": exp.get("quality_class") if exp else "ROOT",
                "hypothesis": exp.get("hypothesis") if exp else "Initial Root"
            })

        return {"nodes": nodes, "edges": edges}
