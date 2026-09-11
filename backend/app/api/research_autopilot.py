"""
AUTONOMOUS RESEARCH AUTOPILOT API ROUTER
========================================
REST endpoints and Server-Sent Events (SSE) stream for mission control,
live research telemetry, experiment inspection, leaderboard, candidate vault,
and multi-stage universe transfer.
"""

import asyncio
import json
import logging
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.analytics.research_orchestrator.research_orchestrator import ResearchOrchestrator
from app.analytics.research_orchestrator.research_memory import ResearchMemory
from app.analytics.research_orchestrator.candidate_vault import CandidateVault
from app.analytics.research_orchestrator.universe_transfer import UniverseTransferEngine
from app.analytics.research_orchestrator.autonomous_runner import AutonomousResearchRunner
from app.analytics.research_orchestrator.research_telemetry import (
    telemetry_broadcaster,
    TelemetryEventType
)
from app.analytics.master_logger import MasterLogger

logger = logging.getLogger(__name__)
router = APIRouter()
orchestrator = ResearchOrchestrator()

class RunAutonomousResearchRequest(BaseModel):
    mission_id: Optional[str] = None
    objective: Optional[str] = None
    universe: str = "LIVE_52"
    batch_size: int = 4

@router.post("/run-autonomous")
def run_autonomous_research_api(req: RunAutonomousResearchRequest = RunAutonomousResearchRequest()):
    """
    Executes unified 7-step autonomous research action:
    1. Inspects champion baselines.
    2. Consults research memory.
    3. Generates non-duplicate hypotheses.
    4. Runs parallel holdout cross-validation.
    5. Executes 4-stage universe transfer (LIVE_52 -> RESEARCH_100 -> NIFTY_200 -> NIFTY_500).
    6. Freezes qualifying candidates in CandidateVault.
    7. Verifies zero unauthorized champion promotion.
    """
    try:
        res = AutonomousResearchRunner.run_autonomous_research(
            mission_id=req.mission_id,
            objective=req.objective,
            universe=req.universe,
            batch_size=req.batch_size
        )
        post_safety = res.get("safety_invariants", {})
        return {
            "success": True,
            "status": "success",
            "mission_id": res.get("mission_id"),
            "stats": {
                "candidates_generated": res.get("hypotheses_generated", 0),
                "evaluated_count": res.get("experiments_completed", 0),
                "qualifying_count": res.get("candidates_qualified_and_frozen", 0),
            },
            "safety_audit": {
                "champions_intact": post_safety.get("post_flight_safety") == "PASS",
                "portfolio_heat_pct": post_safety.get("portfolio_heat_pct", 0.0),
                "live_broker_orders": post_safety.get("broker_orders", 0),
            },
            "report": res
        }
    except Exception as e:
        logger.error(f"[ResearchAPI] Autonomous research error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/autonomous-stream")
async def run_autonomous_research_stream_api(
    mission_id: Optional[str] = Query(None),
    universe: str = Query("LIVE_52"),
    batch_size: int = Query(4)
):
    """
    Streams live SSE progress events during autonomous research run.
    """
    return StreamingResponse(
        AutonomousResearchRunner.run_autonomous_research_stream(
            mission_id=mission_id,
            universe=universe,
            batch_size=batch_size
        ),
        media_type="text/event-stream"
    )

class CreateMissionRequest(BaseModel):
    objective: str
    primary_objective_metric: str = "SHARPE"
    secondary_constraints: Dict[str, Any] = Field(default_factory=dict)
    universe: str = "LIVE_52"
    strategy_type: str = "SWING"
    timeframe: str = "1d"
    budget: Dict[str, Any] = Field(default_factory=dict)
    feature_families: Optional[List[str]] = None
    model_families: Optional[List[str]] = None
    target_horizons: Optional[List[int]] = None
    portfolio_families: Optional[List[str]] = None
    research_seed: int = 42

class UniverseTransferRequest(BaseModel):
    candidate_id: str
    target_universe: str = "RESEARCH_100"

@router.get("/status")
def get_research_status(mission_id: Optional[str] = None):
    """Returns production safety locks, active mission info, and research metrics."""
    return orchestrator.get_status(mission_id)

@router.get("/missions")
def get_historical_missions_api():
    """
    Returns all historical missions from authoritative research database with
    aggregated execution, candidate, and search space metrics. Sorted newest first.
    """
    try:
        missions = ResearchMemory.list_missions_summary()
        return {
            "status": "success",
            "missions": missions,
            "count": len(missions)
        }
    except Exception as e:
        logger.error(f"[ResearchAPI] Failed to retrieve historical missions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/mission/create")
def create_mission_api(req: CreateMissionRequest):
    """Creates a new immutable research mission."""
    try:
        mission = orchestrator.create_mission(
            objective=req.objective,
            primary_objective_metric=req.primary_objective_metric,
            secondary_constraints=req.secondary_constraints,
            universe=req.universe,
            budget=req.budget,
            feature_families=req.feature_families,
            model_families=req.model_families,
            target_horizons=req.target_horizons,
            portfolio_families=req.portfolio_families,
            research_seed=req.research_seed
        )
        return {"status": "success", "mission": mission.to_dict()}
    except Exception as e:
        logger.error(f"[ResearchAPI] Create mission error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/mission/start")
def start_mission_api(mission_id: str = Query(...)):
    """Starts autonomous research mission and enqueues initial batch."""
    try:
        res = orchestrator.start_mission(mission_id)
        return {"status": "success", "data": res}
    except Exception as e:
        logger.error(f"[ResearchAPI] Start mission error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/mission/pause")
def pause_mission_api(mission_id: str = Query(...)):
    """Pauses research loop."""
    try:
        res = orchestrator.pause_mission(mission_id)
        return {"status": "success", "data": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/mission/resume")
def resume_mission_api(mission_id: str = Query(...)):
    """Resumes research loop."""
    try:
        res = orchestrator.resume_mission(mission_id)
        return {"status": "success", "data": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/mission/stop")
def stop_mission_api(mission_id: str = Query(...)):
    """Stops research loop."""
    try:
        res = orchestrator.stop_mission(mission_id)
        return {"status": "success", "data": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/iteration")
def run_iteration_api(mission_id: str = Query(...), batch_size: int = Query(4)):
    """Triggers one research iteration batch."""
    try:
        res = orchestrator.run_iteration(mission_id, batch_size=batch_size)
        return {"status": "success", "data": res}
    except Exception as e:
        logger.error(f"[ResearchAPI] Iteration error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/queue")
def get_queue_api(mission_id: str = Query(...)):
    """Retrieves pending experiments in the queue."""
    conn = None
    try:
        import sqlite3
        from app.data.historical_data_layer import get_db_path
        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
            SELECT experiment_id, mission_id, priority, status, hypothesis, created_at
            FROM research_experiments_queue
            WHERE mission_id = ?
            ORDER BY created_at DESC
            LIMIT 50;
        """, (mission_id,))
        rows = c.fetchall()
        return {"status": "success", "queue": [dict(r) for r in rows]}
    finally:
        if conn:
            conn.close()

@router.get("/experiment/{experiment_id}")
def get_experiment_api(experiment_id: str):
    """Deep inspector audit details for a specific experiment or candidate."""
    audit = ResearchMemory.get_experiment_audit_details(experiment_id)
    if not audit:
        raise HTTPException(status_code=404, detail=f"Experiment {experiment_id} not found.")
    
    return {
        "status": "success",
        "audit": audit,
        "experiment": audit,
        "explanation": audit.get("explanation")
    }

@router.get("/experiment/{experiment_id}/report.pdf")
def get_experiment_report_pdf_api(experiment_id: str):
    """Generates an institutional research audit PDF report for an experiment or candidate."""
    audit = ResearchMemory.get_experiment_audit_details(experiment_id)
    if not audit:
        raise HTTPException(status_code=404, detail=f"Experiment {experiment_id} not found.")
    
    from app.analytics.research_report_pdf_generator import ResearchReportPDFGenerator
    from fastapi.responses import Response

    pdf_bytes = ResearchReportPDFGenerator.generate_experiment_audit_pdf(audit)
    clean_id = experiment_id.replace("/", "_").replace("\\", "_")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=Research_Audit_{clean_id}.pdf",
            "Cache-Control": "no-cache"
        }
    )

@router.get("/leaderboard")
def get_leaderboard_api(mission_id: Optional[str] = None):
    """Returns ranked research leaderboard."""
    board = orchestrator.get_leaderboard(mission_id)
    return {"status": "success", "leaderboard": board}

@router.get("/frontier")
def get_frontier_api(mission_id: Optional[str] = None):
    """Returns Turnover vs Sharpe vs CAGR trade-off points."""
    pts = orchestrator.get_research_frontier(mission_id)
    return {"status": "success", "frontier": pts}

@router.get("/lineage")
def get_lineage_api(mission_id: str = Query(...)):
    """Returns knowledge graph nodes and edges for research lineage."""
    graph = orchestrator.get_knowledge_graph(mission_id)
    return {"status": "success", "graph": graph}

@router.get("/vault")
def get_vault_api(mission_id: Optional[str] = None):
    """Lists frozen candidate configurations in the vault."""
    cands = CandidateVault.list_candidates(mission_id)
    return {"status": "success", "candidates": cands}

@router.post("/vault/universe-transfer")
def run_universe_transfer_api(req: UniverseTransferRequest):
    """Triggers an explicit universe transfer test for a frozen candidate."""
    try:
        res = orchestrator.run_universe_transfer(req.candidate_id, req.target_universe)
        return {"status": "success", "data": res}
    except Exception as e:
        logger.error(f"[ResearchAPI] Universe transfer error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/mission/{mission_id}/runtime")
def get_mission_runtime_api(mission_id: str):
    """Returns canonical real-time runtime state snapshot for research control room."""
    try:
        data = orchestrator.get_mission_runtime(mission_id)
        return {"status": "success", "runtime": data}
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as e:
        logger.error(f"[ResearchAPI] get_mission_runtime error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/telemetry")
async def get_telemetry_stream(
    request: Request,
    mission_id: Optional[str] = Query(None),
    last_event_id: Optional[int] = Query(None),
    max_events: Optional[int] = Query(None, description="Optional limit of events to stream")
):
    """
    True real-time SSE telemetry stream.
    Replays missed events from ring buffer on connect, broadcasts live stage
    transitions from worker threads, and sends periodic keepalive heartbeats.
    """
    header_last_id = request.headers.get("last-event-id")
    effective_last_id = 0
    if header_last_id:
        try:
            effective_last_id = int(header_last_id)
        except ValueError:
            pass
    if last_event_id is not None and last_event_id > effective_last_id:
        effective_last_id = last_event_id

    loop = asyncio.get_running_loop()
    telemetry_broadcaster.set_event_loop(loop)
    q = telemetry_broadcaster.subscribe()

    async def event_generator():
        sent_count = 0
        try:
            # 1. Replay missed events since last_id
            missed = telemetry_broadcaster.get_events_since(
                mission_id=mission_id,
                last_event_id=effective_last_id,
                limit=100
            )
            for ev in missed:
                yield ev.to_sse_format()
                sent_count += 1
                if max_events is not None and sent_count >= max_events:
                    return

            # 2. Stream live events with 2s heartbeat
            while True:
                if max_events is not None and sent_count >= max_events:
                    break
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=2.0)
                    if mission_id is None or event.mission_id == mission_id:
                        yield event.to_sse_format()
                        sent_count += 1
                except asyncio.TimeoutError:
                    yield f": ping\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            telemetry_broadcaster.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


