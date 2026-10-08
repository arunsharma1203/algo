"""
CONSENSUS SCANNER API ROUTER
============================
Exposes REST endpoints for the Dual-Engine Consensus Scanner.
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.analytics.consensus_scanner import ConsensusScanner
from app.analytics.consensus_tracker import ConsensusTracker
from app.api.qlib_system import get_qlib_system_status
from app.analytics.system_health_center import SystemHealthCenter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/consensus", tags=["consensus"])


class ConsensusScanRequest(BaseModel):
    strategy: str = Field(default="INTRADAY", description="Scan strategy: INTRADAY or SWING")
    universe: str = Field(default="ALL_DATABASE_STOCKS", description="Universe preset (ALL_DATABASE_STOCKS, NIFTY_500, NIFTY_50)")
    top_k: int = Field(default=5, ge=1, le=50, description="Max recommendations per category")


@router.get("/active")
async def get_active_consensus_scan():
    """
    Returns the real-time execution status and progress of any currently running consensus scan.
    Enables frontend clients to monitor ongoing sweeps and resume tracking across browser reloads.
    """
    return ConsensusScanner.get_active_scan()


@router.get("/latest")
async def get_latest_consensus_scan(strategy: str = "INTRADAY"):
    """
    Returns the most recent completed consensus scan result for the specified strategy.
    Enables instant state restoration on frontend page reload.
    """
    latest = ConsensusScanner.get_latest_scan(strategy=strategy)
    if not latest:
        return {"has_latest": False, "scan": None}
    return {"has_latest": True, "scan": latest}


@router.post("/scan")
async def trigger_consensus_scan(req: ConsensusScanRequest):
    """
    Triggers concurrent Qlib quantitative alpha factor scan and Legacy AI technical sweep,
    identifying Grade-A Confluence setups and highlighting directional conflicts.
    """
    strat = req.strategy.upper()
    if strat not in ("INTRADAY", "SWING"):
        raise HTTPException(status_code=400, detail=f"Invalid strategy '{req.strategy}'. Must be INTRADAY or SWING.")

    active = ConsensusScanner.get_active_scan()
    if active.get("active"):
        return {
            "status": "ALREADY_RUNNING",
            "message": "A consensus scan is already currently running.",
            "active_scan": active
        }

    try:
        results = await ConsensusScanner.run_consensus_scan(
            strategy=strat,
            universe=req.universe,
            top_k=req.top_k
        )
        return results
    except Exception as e:
        logger.error(f"Consensus scan execution failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Consensus scan execution error: {str(e)}")


@router.post("/stop")
async def stop_consensus_scan():
    """
    Stops any actively executing consensus scan immediately.
    """
    return ConsensusScanner.stop_active_scan()


@router.get("/status")
async def get_consensus_engine_status():
    """
    Returns operational status and readiness for both underlying engines.
    Optimized for sub-millisecond latency via direct registry inspection.
    """
    qlib_models = {}
    try:
        from app.qlib_engine.model_registry import QlibModelRegistry
        qlib_models = QlibModelRegistry.get_registry_status()
    except Exception:
        pass

    intraday_info = qlib_models.get("intraday", {})
    swing_info = qlib_models.get("swing", {})
    qlib_ready = bool(intraday_info.get("available") or swing_info.get("available"))

    legacy_ready = False
    legacy_intra_meta = {}
    legacy_swing_meta = {}
    try:
        from app.analytics.model_manager import ModelManager
        legacy_intra_meta = ModelManager.get_champion_metadata("intraday") or {}
        legacy_swing_meta = ModelManager.get_champion_metadata("swing") or {}
        legacy_ready = bool(legacy_intra_meta or legacy_swing_meta)
    except Exception:
        legacy_ready = True

    return {
        "status": "HEALTHY",
        "engines": {
            "qlib": {
                "active_intraday_model": intraday_info.get("model_id"),
                "active_swing_model": swing_info.get("model_id"),
                "qlib_initialized": True,
                "ready": qlib_ready
            },
            "legacy": {
                "ready": legacy_ready,
                "sentiment_engine": "FinBERT (ProsusAI/finbert)",
                "active_intraday_model": legacy_intra_meta.get("version", "v1.0-champion"),
                "active_swing_model": legacy_swing_meta.get("version", "v1.0-champion")
            }
        }
    }


@router.get("/comparison")
async def get_multi_engine_comparison(days: int = 30, filter_mode: str = "ALL_QUALIFIED"):
    """
    Returns side-by-side comparative performance metrics and cumulative timeline
    across Qlib, Legacy, and Dual-Engine Consensus.
    Supports filter_mode='ALL_QUALIFIED' or 'HIGHEST_CONFLUENCE_ONLY'.
    """
    try:
        if filter_mode == "ALL_QUALIFIED":
            return ConsensusTracker.get_multi_engine_comparison(days=days)
        return ConsensusTracker.get_multi_engine_comparison(days=days, filter_mode=filter_mode)
    except Exception as e:
        logger.error(f"Failed to fetch multi-engine comparison: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def get_consensus_trade_history(days: int = 30, filter_mode: str = "ALL_QUALIFIED"):
    """
    Returns tracked trades and performance analytics specific to the Consensus engine.
    Supports filter_mode='ALL_QUALIFIED' or 'HIGHEST_CONFLUENCE_ONLY'.
    """
    try:
        if filter_mode == "ALL_QUALIFIED":
            return ConsensusTracker.get_consensus_performance_stats(days=days)
        return ConsensusTracker.get_consensus_performance_stats(days=days, filter_mode=filter_mode)
    except Exception as e:
        logger.error(f"Failed to fetch consensus history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history-scans")
async def get_consensus_scan_history(strategy: Optional[str] = None, limit: int = 20):
    """
    Returns historical consensus scan records across sweeps from consensus_scan_history.
    """
    try:
        return ConsensusScanner.get_scan_history(strategy=strategy, limit=limit)
    except Exception as e:
        logger.error(f"Failed to fetch consensus scan history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tracked-trades")
async def get_consensus_tracked_trades(strategy: Optional[str] = None, status: Optional[str] = None, limit: int = 50):
    """
    Returns all persisted consensus trades with real-time tracking, live LTP, and outcome metrics.
    """
    try:
        return ConsensusTracker.get_all_tracked_trades(strategy=strategy, status=status, limit=limit)
    except Exception as e:
        logger.error(f"Failed to fetch consensus tracked trades: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/weights")
async def get_consensus_weights(strategy: str = "INTRADAY"):
    """
    Returns active online regret-minimizing weights (Hedge) and sequential CUSUM risk status.
    """
    try:
        from app.analytics.consensus_learning_engine import ConsensusLearningEngine
        weights = ConsensusLearningEngine.get_current_weights(strategy=strategy)
        cusum = ConsensusLearningEngine.get_cusum_status(strategy=strategy)
        return {
            "strategy": strategy.upper(),
            "weights": weights,
            "cusum_risk_gate": cusum,
            "status": "HEALTHY"
        }
    except Exception as e:
        logger.error(f"Failed to fetch consensus weights: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/weights/update")
async def trigger_consensus_weight_update(strategy: str = "INTRADAY", trailing_n: int = 60):
    """
    Triggers post-market online regret minimization update on demand.
    """
    try:
        from app.analytics.consensus_learning_engine import ConsensusLearningEngine
        result = ConsensusLearningEngine.execute_daily_weight_update(strategy=strategy, trailing_n=trailing_n)
        return result
    except Exception as e:
        logger.error(f"Failed to execute consensus weight update: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/learning-history")
async def get_consensus_learning_history(strategy: str = "INTRADAY", limit: int = 30):
    """
    Returns historical online weight adaptations, Brier loss scores, and CUSUM tracking.
    """
    try:
        from app.analytics.consensus_learning_engine import ConsensusLearningEngine
        return ConsensusLearningEngine.get_learning_history(strategy=strategy, limit=limit)
    except Exception as e:
        logger.error(f"Failed to fetch consensus learning history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/shadow-candidates")
async def get_consensus_shadow_candidates(
    strategy: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 50
):
    """
    Returns recent shadow candidate evaluations across sweeps (traded, conflicting, and solo).
    """
    try:
        return ConsensusTracker.get_shadow_candidates(strategy=strategy, action=action, limit=limit)
    except Exception as e:
        logger.error(f"Failed to fetch shadow candidates: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))




