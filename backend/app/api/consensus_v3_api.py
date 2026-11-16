"""
Consensus V3 FastAPI Router (`/api/consensus/v3/*`)
===================================================
Exposes institutional endpoints for the 6-Layer Equity Consensus Sniffer & Funded-Cash Arena:
  - GET  /api/consensus/v3/status
  - GET  /api/consensus/v3/signals
  - GET  /api/consensus/v3/positions
  - GET  /api/consensus/v3/performance
  - GET  /api/consensus/v3/diagnostics
  - POST /api/consensus/v3/sniffer-sweep
  - POST /api/consensus/v3/arena/configure
  - POST /api/consensus/v3/positions/exit
  - GET  /api/consensus/v3/live/status
  - GET  /api/consensus/v3/live/order-intents
  - POST /api/consensus/v3/live/mode
  - POST /api/consensus/v3/live/arm          (HUMAN-EXCLUSIVE PIN INTERLOCK — AGENTS.md Rule 6)
  - POST /api/consensus/v3/live/disarm
  - POST /api/consensus/v3/live/sos-panic-exit
"""

import logging
from typing import Any, Dict, List
from fastapi import APIRouter, Body, HTTPException, Query

from app.analytics.consensus_v3.orchestrator import get_consensus_v3_orchestrator

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/consensus/v3",
    tags=["Consensus V3 (6-Layer Sniffer & Funded Arena)"],
)


@router.get("/status")
def get_v3_status() -> Dict[str, Any]:
    try:
        orch = get_consensus_v3_orchestrator()
        return orch.get_workspace_status()
    except Exception as e:
        logger.error(f"[ConsensusV3API] /status error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals")
def get_v3_signals(limit: int = Query(60, ge=1, le=200)) -> Dict[str, Any]:
    try:
        orch = get_consensus_v3_orchestrator()
        return orch.get_signals_feed(limit=limit)
    except Exception as e:
        logger.error(f"[ConsensusV3API] /signals error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/positions")
def get_v3_positions(limit: int = Query(100, ge=1, le=300)) -> Dict[str, Any]:
    try:
        orch = get_consensus_v3_orchestrator()
        orch.bridge.reconcile_orders_and_positions()
        return orch.bridge.get_positions_and_reservations(limit=limit)
    except Exception as e:
        logger.error(f"[ConsensusV3API] /positions error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/performance")
def get_v3_performance() -> Dict[str, Any]:
    try:
        orch = get_consensus_v3_orchestrator()
        return orch.get_performance_vs_baseline()
    except Exception as e:
        logger.error(f"[ConsensusV3API] /performance error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/diagnostics")
def get_v3_diagnostics() -> Dict[str, Any]:
    try:
        orch = get_consensus_v3_orchestrator()
        return orch.get_diagnostics()
    except Exception as e:
        logger.error(f"[ConsensusV3API] /diagnostics error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sniffer-sweep")
def trigger_v3_sniffer_sweep() -> Dict[str, Any]:
    try:
        orch = get_consensus_v3_orchestrator()
        res = orch.run_15m_sniffer_sweep(reserve_arena_orders=True)
        return {"status": "SUCCESS", "data": res}
    except Exception as e:
        logger.error(f"[ConsensusV3API] /sniffer-sweep error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/book1-rebalance")
def trigger_v3_book1_rebalance(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    try:
        orch = get_consensus_v3_orchestrator()
        force_flag = bool((payload or {}).get("force", True))
        res = orch.run_book1_rebalance(force=force_flag)
        return {"status": "SUCCESS", "data": res}
    except Exception as e:
        logger.error(f"[ConsensusV3API] /book1-rebalance error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/arena/configure")
def configure_v3_arena(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        orch = get_consensus_v3_orchestrator()
        cap = float(payload.get("funded_capital_inr", 400000.0))
        mode = str(payload.get("execution_mode", "DUAL_FUNDED_CASH_ARENA"))
        reset_flag = bool(payload.get("reset_ledger", False))
        margin_mode = payload.get("margin_mode")
        mis_lev = payload.get("intraday_mis_leverage")
        risk_pct = payload.get("max_risk_per_trade_pct")
        b1_slots = payload.get("book1_max_slots")
        res = orch.bridge.configure_arena_capital(
            funded_capital_inr=cap,
            execution_mode=mode,
            reset_ledger=reset_flag,
            margin_mode=str(margin_mode) if margin_mode is not None else None,
            intraday_mis_leverage=float(mis_lev) if mis_lev is not None else None,
            max_risk_per_trade_pct=float(risk_pct) if risk_pct is not None else None,
            book1_max_slots=int(b1_slots) if b1_slots is not None else None,
        )
        if res.get("status") == "ERROR":
            raise HTTPException(status_code=400, detail=res.get("message"))
        return {"status": "SUCCESS", "accounting": res}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ConsensusV3API] /arena/configure error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/positions/exit")
def exit_v3_position(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        orch = get_consensus_v3_orchestrator()
        pid = str(payload.get("position_id") or "").strip()
        reason = str(payload.get("reason") or "MANUAL_OPERATOR_EXIT").strip()
        if not pid:
            raise HTTPException(status_code=400, detail="position_id is required")
        res = orch.bridge.manual_exit_position(position_id=pid, reason=reason)
        if res.get("status") == "NOT_FOUND":
            raise HTTPException(status_code=404, detail=res.get("message"))
        return res
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ConsensusV3API] /positions/exit error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/live/status")
def get_v3_live_status() -> Dict[str, Any]:
    try:
        orch = get_consensus_v3_orchestrator()
        return orch.bridge.get_live_status()
    except Exception as e:
        logger.error(f"[ConsensusV3API] /live/status error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/live/order-intents")
def get_v3_live_order_intents(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> List[Dict[str, Any]]:
    try:
        orch = get_consensus_v3_orchestrator()
        return orch.bridge.get_recent_live_intents(limit=limit, offset=offset)
    except Exception as e:
        logger.error(f"[ConsensusV3API] /live/order-intents error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/live/mode")
def set_v3_live_mode(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        orch = get_consensus_v3_orchestrator()
        mode = str(payload.get("execution_mode") or "").strip().upper()
        res = orch.bridge.set_execution_mode(mode)
        if res.get("status") == "ERROR":
            raise HTTPException(status_code=400, detail=res.get("message"))
        return res
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ConsensusV3API] /live/mode error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/live/arm")
def arm_v3_live_trading(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Human-exclusive endpoint to arm Live Upstox Equity routing via 4-digit confirmation PIN (`AGENTS.md` Rule 6)."""
    try:
        orch = get_consensus_v3_orchestrator()
        pin = str(payload.get("pin") or "").strip()
        mode = payload.get("execution_mode")
        res = orch.bridge.arm_live_trading(pin=pin, execution_mode=mode)
        if res.get("status") != "ARMED":
            raise HTTPException(status_code=400, detail=res.get("message", "Failed to arm Consensus V3 bridge"))
        return res
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ConsensusV3API] /live/arm error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/live/disarm")
def disarm_v3_live_trading() -> Dict[str, Any]:
    try:
        orch = get_consensus_v3_orchestrator()
        return orch.bridge.disarm_live_trading()
    except Exception as e:
        logger.error(f"[ConsensusV3API] /live/disarm error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/live/sos-panic-exit")
def sos_v3_panic_exit() -> Dict[str, Any]:
    try:
        orch = get_consensus_v3_orchestrator()
        return orch.bridge.execute_sos_panic_exit()
    except Exception as e:
        logger.error(f"[ConsensusV3API] /live/sos-panic-exit error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
