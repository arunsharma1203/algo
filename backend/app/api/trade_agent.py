"""
FastAPI Router for Trade Sentinel Research Agent
================================================
Exposes read-only telemetry, shadow scoreboard, decisions, and research journal
to the frontend dashboard.
"""

import sys
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# Ensure agent_lab is on Python path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from agent_lab.agent_core.sentinel_agent import TradeSentinelAgent

router = APIRouter(prefix="/agent", tags=["Trade Sentinel Agent"])
logger = logging.getLogger("api.trade_agent")

class EvaluateCandidateRequest(BaseModel):
    ticker: str
    direction: str = "BUY"
    qlib_score: float = 50.0
    qlib_alpha_strength: float = 50.0
    legacy_score: float = 50.0
    composite_score: float = 50.0

_MODULES_RELOADED = False

@router.get("/status")
def get_agent_status(reload: bool = False):
    """Returns the operational status and resource profile of the Trade Sentinel Agent."""
    global _MODULES_RELOADED
    if not _MODULES_RELOADED or reload:
        try:
            import importlib
            for mod_name in [
                "agent_lab.tools.production_reader",
                "agent_lab.agent_core.llm_router",
                "agent_lab.agent_core.sentinel_agent",
                "app.analytics.consensus_scanner",
                "app.analytics.consensus_game_engine"
            ]:
                if mod_name in sys.modules:
                    importlib.reload(sys.modules[mod_name])

            # Refresh in-memory scheduler job for 1-minute reconciler
            try:
                from app.main import get_or_create_scheduler, IST_TIMEZONE
                sched = get_or_create_scheduler()
                if sched and sched.running:
                    def _reconcile_consensus_trades_daemon():
                        try:
                            from app.analytics.consensus_tracker import ConsensusTracker
                            ConsensusTracker.reconcile_open_consensus_trades(min_interval_sec=45.0)
                        except Exception as ct_err:
                            logger.warning(f"[TradeAgent] Reconcile trades note: {ct_err}")
                        try:
                            from app.analytics.consensus_dynamic_guard import ConsensusDynamicGuard
                            ConsensusDynamicGuard.audit_active_arena_positions(session_id="default_arena_session")
                        except Exception as dg_err:
                            logger.debug(f"[TradeAgent] Dynamic guard note: {dg_err}")
                        try:
                            from app.analytics.consensus_game_engine import ConsensusGameEngine
                            ConsensusGameEngine.sync_and_dispatch_arena_alerts(session_id="default_arena_session")
                        except Exception as alert_err:
                            logger.warning(f"[TradeAgent] Game alert dispatch note: {alert_err}")

                    sched.add_job(
                        _reconcile_consensus_trades_daemon,
                        'cron',
                        day_of_week='mon-fri',
                        hour='9-15',
                        minute='*',
                        timezone=IST_TIMEZONE,
                        id='consensus_trade_reconciler_1m',
                        replace_existing=True
                    )
            except Exception as _s_err:
                logger.debug(f"Scheduler hot-refresh in status note: {_s_err}")

            _MODULES_RELOADED = True
            logger.info("✅ Hot-reloaded optimized ConsensusScanner, ProductionReader & TradeSentinel modules in-memory (zero downtime).")
        except Exception as r_err:
            logger.debug(f"Module hot-reload note: {r_err}")

    try:
        from agent_lab.agent_core.sentinel_agent import TradeSentinelAgent
        return TradeSentinelAgent.get_status()
    except Exception as e:
        logger.error(f"Error fetching agent status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/reload-modules")
def reload_in_memory_modules():
    """Hot-reloads in-memory modules and refreshes scheduler jobs without stopping or restarting FastAPI/uvicorn services."""
    reloaded = []
    try:
        import importlib
        for mod_name in [
            "agent_lab.tools.production_reader",
            "agent_lab.agent_core.llm_router",
            "agent_lab.agent_core.sentinel_agent",
            "app.analytics.consensus_scanner",
            "app.analytics.consensus_game_engine",
            "app.data.upstox_stream_service",
            "app.analytics.option_sniffer.option_constants",
            "app.analytics.option_sniffer.option_db",
            "app.analytics.option_sniffer.option_feature_extractor",
            "app.analytics.option_sniffer.option_alpha_model",
            "app.analytics.option_sniffer.option_stream_manager",
            "app.analytics.option_sniffer.option_signal_engine",
            "app.analytics.option_sniffer.option_contract_discovery",
            "app.analytics.option_sniffer.option_paper_execution",
            "app.analytics.option_sniffer.option_sniffer_orchestrator",
        ]:
            if mod_name in sys.modules:
                importlib.reload(sys.modules[mod_name])
                reloaded.append(mod_name)

        # Trigger Option Sniffer orchestrator self-healing reset
        try:
            from app.analytics.option_sniffer.option_sniffer_orchestrator import get_option_sniffer_orchestrator
            orch = get_option_sniffer_orchestrator()
            with orch._lock:
                orch._last_discovery_date = ""
            reloaded.append("orchestrator.discovery_reset")
        except Exception as o_err:
            logger.debug(f"Option sniffer discovery reset note: {o_err}")

        # Refresh in-memory scheduler job for 1-minute reconciler
        try:
            from app.main import get_or_create_scheduler, IST_TIMEZONE
            sched = get_or_create_scheduler()
            if sched and sched.running:
                def _reconcile_consensus_trades_daemon():
                    try:
                        from app.analytics.consensus_tracker import ConsensusTracker
                        ConsensusTracker.reconcile_open_consensus_trades(min_interval_sec=45.0)
                    except Exception as ct_err:
                        logger.warning(f"[TradeAgent] Reconcile trades note: {ct_err}")
                    try:
                        from app.analytics.consensus_dynamic_guard import ConsensusDynamicGuard
                        ConsensusDynamicGuard.audit_active_arena_positions(session_id="default_arena_session")
                    except Exception as dg_err:
                        logger.debug(f"[TradeAgent] Dynamic guard note: {dg_err}")
                    try:
                        from app.analytics.consensus_game_engine import ConsensusGameEngine
                        ConsensusGameEngine.sync_and_dispatch_arena_alerts(session_id="default_arena_session")
                    except Exception as alert_err:
                        logger.warning(f"[TradeAgent] Game alert dispatch note: {alert_err}")

                sched.add_job(
                    _reconcile_consensus_trades_daemon,
                    'cron',
                    day_of_week='mon-fri',
                    hour='9-15',
                    minute='*',
                    timezone=IST_TIMEZONE,
                    id='consensus_trade_reconciler_1m',
                    replace_existing=True
                )
                reloaded.append("scheduler.consensus_trade_reconciler_1m")
        except Exception as s_err:
            logger.debug(f"Scheduler hot-refresh note: {s_err}")

        return {"status": "SUCCESS", "reloaded_modules": reloaded, "message": "In-memory modules and scheduler hot-reloaded successfully."}
    except Exception as e:
        logger.error(f"Hot-reload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/scoreboard")
def get_agent_scoreboard():
    """Returns Consensus vs Trade Sentinel performance comparison metrics."""
    try:
        # Reconcile outcomes first to ensure fresh data
        TradeSentinelAgent.reconcile_outcomes()
        return TradeSentinelAgent.get_scoreboard_summary()
    except Exception as e:
        import traceback
        logger.error(f"Error fetching agent scoreboard: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/evaluate")
def evaluate_candidate(req: EvaluateCandidateRequest):
    """Generates an immediate shadow evaluation for a given candidate."""
    try:
        cand_dict = req.dict()
        cand_dict["is_actionable"] = True
        return TradeSentinelAgent.observe_and_evaluate(cand_dict)
    except Exception as e:
        logger.error(f"Error evaluating candidate: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/reconcile")
def trigger_reconciliation():
    """Reconciles closed trades against agent shadow decisions."""
    try:
        return TradeSentinelAgent.reconcile_outcomes()
    except Exception as e:
        logger.error(f"Error reconciling outcomes: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/mission/core-edge-forensic")
def trigger_core_edge_forensic():
    """Triggers the autonomous CORE_EDGE_FORENSIC research mission in the sandbox."""
    try:
        return TradeSentinelAgent.run_core_edge_forensic(max_cycles=5)
    except Exception as e:
        logger.error(f"Error executing CORE_EDGE_FORENSIC: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/mission/latest")
def get_latest_mission():
    """Returns the latest autonomous mission report and hypotheses."""
    try:
        import sqlite3, json
        from agent_lab.memory.agent_db import get_agent_db_path
        conn = sqlite3.connect(get_agent_db_path())
        cur = conn.cursor()
        cur.execute("""
            SELECT final_report_json, mission_id, objective, status, cycle_count, current_best_explanation, started_at, completed_at
            FROM autonomous_missions
            ORDER BY started_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        conn.close()
        if row:
            if row[0]:
                return json.loads(row[0])
            return {
                "mission_id": row[1],
                "objective": row[2],
                "status": row[3],
                "cycle_count": row[4],
                "current_best_explanation": row[5],
                "started_at": row[6],
                "completed_at": row[7]
            }
        return {"status": "NO_MISSIONS_YET"}
    except Exception as e:
        logger.error(f"Error fetching latest mission: {e}")
        raise HTTPException(status_code=500, detail=str(e))

