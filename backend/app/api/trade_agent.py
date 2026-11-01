"""
FastAPI Router for Trade Sentinel Research Agent (DECOMMISSIONED STUB)
======================================================================
Trade Sentinel (agent_lab/) was decommissioned and purged to reclaim ~98 GB of disk space
and eliminate 30-second background CPU/SSD I/O overhead.
"""

import sys
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/agent", tags=["Trade Sentinel Agent"])
logger = logging.getLogger("api.trade_agent")


class EvaluateCandidateRequest(BaseModel):
    ticker: str
    direction: str = "BUY"
    qlib_score: float = 50.0
    qlib_alpha_strength: float = 50.0
    legacy_score: float = 50.0
    composite_score: float = 50.0


@router.get("/status")
def get_agent_status():
    """Returns decommissioned status for Trade Sentinel Agent."""
    return {
        "status": "DECOMMISSIONED",
        "is_running": False,
        "mode": "PURGED",
        "message": "Trade Sentinel (agent_lab) has been decommissioned and purged (~98 GB reclaimed).",
    }


@router.post("/reload-modules")
def reload_in_memory_modules():
    """Hot-reloads in-memory modules and refreshes scheduler jobs without stopping or restarting FastAPI/uvicorn services."""
    reloaded = []
    try:
        import importlib
        for mod_name in [
            "app.analytics.consensus_scanner",
            "app.analytics.consensus_game_engine",
            "app.data.upstox_stream_service",
            "app.analytics.option_sniffer.option_constants",
            "app.analytics.option_sniffer.option_db",
            "app.analytics.option_sniffer.option_feature_extractor",
            "app.analytics.option_sniffer.option_alpha_model",
            "app.analytics.option_sniffer.option_stream_manager",
            "app.analytics.option_sniffer.option_microstructure_evaluator",
            "app.analytics.option_sniffer.option_signal_engine",
            "app.analytics.option_sniffer.option_contract_discovery",
            "app.analytics.option_sniffer.option_paper_execution",
            "app.analytics.option_sniffer.option_sniffer_orchestrator",
            "app.analytics.option_sniffer.option_live_adapter",
            "app.analytics.option_sniffer.option_live_order_intent",
        ]:
            if mod_name in sys.modules:
                importlib.reload(sys.modules[mod_name])
                reloaded.append(mod_name)

        try:
            from app.analytics.option_sniffer.option_sniffer_orchestrator import get_option_sniffer_orchestrator
            orch = get_option_sniffer_orchestrator()
            with orch._lock:
                orch._last_discovery_date = ""
            reloaded.append("orchestrator.discovery_reset")
        except Exception as o_err:
            logger.debug(f"Option sniffer discovery reset note: {o_err}")

        return {"status": "SUCCESS", "reloaded_modules": reloaded, "message": "In-memory modules hot-reloaded successfully."}
    except Exception as e:
        logger.error(f"Hot-reload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scoreboard")
def get_agent_scoreboard():
    """Returns empty decommissioned scoreboard."""
    return {
        "status": "DECOMMISSIONED",
        "decisions": [],
        "journal": [],
        "metrics": {"total_evaluated": 0, "win_rate": 0.0},
    }


@router.post("/evaluate")
def evaluate_candidate(req: EvaluateCandidateRequest):
    return {"status": "DECOMMISSIONED", "ticker": req.ticker}


@router.post("/reconcile")
def trigger_reconciliation():
    return {"status": "DECOMMISSIONED", "reconciled": 0}

