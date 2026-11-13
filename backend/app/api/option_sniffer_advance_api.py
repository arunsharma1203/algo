"""
Dedicated routes for Option Sniffer Advance (Sandboxed Dual-Book Paper Dashboard + Isolated Live Bridge):
  - GET  /api/option-sniffer-advance/status
  - GET  /api/option-sniffer-advance/chain-intelligence
  - GET  /api/option-sniffer-advance/signals
  - GET  /api/option-sniffer-advance/trades
  - GET  /api/option-sniffer-advance/walkforward-audit
  - POST /api/option-sniffer-advance/scan
  - GET  /api/option-sniffer-advance/live/status
  - GET  /api/option-sniffer-advance/live/order-intents
  - POST /api/option-sniffer-advance/live/mode
  - POST /api/option-sniffer-advance/live/arm
  - POST /api/option-sniffer-advance/live/disarm
  - POST /api/option-sniffer-advance/live/sos-panic-exit
"""

import logging
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Body, HTTPException, Query

from app.analytics.option_sniffer_advance.advance_orchestrator import (
    OptionSnifferAdvanceOrchestrator,
    get_option_sniffer_advance_orchestrator,
)
from app.analytics.option_sniffer_advance_live.advance_live_bridge import (
    get_advance_live_bridge,
)
from app.analytics.option_sniffer_advance_s205.s205_upstox_engine import (
    get_s205_orchestrator,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/option-sniffer-advance",
    tags=["Option Sniffer Advance (Dual-Book Sandbox)"],
)


def _ensure_s205_only_running():
    """Ensures S2_05 Upstox V3 orchestrator is active and legacy V2.0 loop is stopped."""
    legacy_inst = OptionSnifferAdvanceOrchestrator._instance
    if legacy_inst is not None and getattr(legacy_inst, "_is_running", False):
        legacy_inst.stop()
    orch = get_s205_orchestrator()
    if not orch._is_running:
        orch.start()
    return orch


@router.get("/status")
def get_advance_status() -> Dict[str, Any]:
    """Provides operational status of Option Sniffer Advance S2_05 + Upstox V3 engine, chain intelligence, and P&L."""
    try:
        orch = _ensure_s205_only_running()
        bridge = get_advance_live_bridge()
        bridge.start_background_follower()
        status = orch.get_status()
        status["live_bridge"] = bridge.get_live_status()
        status["calendar"] = bridge.get_calendar_performance(book_filter="ALL")
        return status
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to get status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/backend-readiness")
def get_advance_backend_readiness() -> Dict[str, Any]:
    """Returns real-time S2_05 backend activity, :00 candle countdown, Upstox V3 feed, and 4-engine order trigger readiness."""
    try:
        orch = _ensure_s205_only_running()
        return orch.get_backend_readiness()
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to get backend readiness: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/calendar")
def get_advance_calendar(book_filter: str = Query("ALL")) -> Dict[str, Any]:
    """Returns read-only Day-by-Day and Month-by-Month P&L Calendar data across all months (`Oct 2026`, `Nov 2026`, etc.)."""
    try:
        bridge = get_advance_live_bridge()
        return bridge.get_calendar_performance(book_filter=book_filter)
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to get calendar performance: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chain-intelligence")
def get_advance_chain_intelligence() -> Dict[str, Any]:
    """Returns cross-sectional option chain intelligence (GEX, VRP, Put-Call Parity Basis, PCR, OI Walls, Max Pain, Upstox V3 Futures)."""
    try:
        orch = _ensure_s205_only_running()
        return orch.get_chain_intelligence()
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to get chain intelligence: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals")
def get_advance_signals(
    limit: int = Query(50, ge=1, le=200),
    book_type: Optional[str] = Query(None),
) -> List[Dict[str, Any]]:
    """Returns recent Dual-Book signals (LONG_BOOK_BUY and SHORT_BOOK_SELL)."""
    try:
        orch = _ensure_s205_only_running()
        return orch.get_signals(limit=limit, book_type=book_type)
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to get signals: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trades")
def get_advance_trades() -> Dict[str, Any]:
    """Returns open and closed Dual-Book paper positions and split Long vs Short Book P&L summary."""
    try:
        orch = _ensure_s205_only_running()
        return orch.get_trades()
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to get trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/walkforward-audit")
def get_advance_walkforward_audit(force_refresh: bool = Query(False)) -> Dict[str, Any]:
    """Returns causal walk-forward Dual-Book audit + Hunt V7 18-month stitched OOS summary for S2_05."""
    try:
        legacy_orch = get_option_sniffer_advance_orchestrator()
        report = dict(legacy_orch.get_walkforward_audit_report(force_refresh=force_refresh) or {})
        report["s205_walkforward_summary"] = {
            "candidate_id": "S2_05",
            "window": "2025-06 to 2026-11 (18 Stitched OOS Months, 6m Train -> 1m Test + Embargo)",
            "total_trades": 349,
            "win_rate_pct": 63.3,
            "profit_factor": 1.563,
            "delay_1bar_pf": 1.376,
            "bootstrap_95ci_pf": [1.318, 1.868],
            "net_pnl_inr": 67719.0,
            "max_mtm_drawdown_inr": 13560.0,
            "profitable_months": "14/18",
        }
        return report
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to get walkforward audit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/scan")
def trigger_advance_scan() -> Dict[str, Any]:
    """Triggers an immediate S2_05 + Upstox V3 surveillance & readiness pass."""
    try:
        orch = _ensure_s205_only_running()
        results = orch.run_surveillance_sweep()
        return {"status": "SUCCESS", "results": results}
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to run scan sweep: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/live/status")
def get_advance_live_status() -> Dict[str, Any]:
    """Returns isolated Live Bridge status, PIN challenge, account readiness, execution mode, and mirrored live P&L."""
    try:
        bridge = get_advance_live_bridge()
        bridge.start_background_follower()
        return bridge.get_live_status()
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to get live status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/live/order-intents")
def get_advance_live_order_intents(limit: int = Query(30, ge=1, le=200)) -> List[Dict[str, Any]]:
    """Returns recent isolated Advance Live order intents (`option_advance_live_order_intents`)."""
    try:
        bridge = get_advance_live_bridge()
        return bridge.get_recent_order_intents(limit=limit)
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to get live order intents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/live/mode")
def set_advance_live_execution_mode(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Updates the capital-matched execution mode (`LONG_BOOK_ONLY`, `DUAL_BOOK_NAKED`, or `DUAL_BOOK_HEDGED`)."""
    try:
        bridge = get_advance_live_bridge()
        mode = str(payload.get("execution_mode") or "").strip().upper()
        result = bridge.set_execution_mode(mode)
        if result.get("status") == "ERROR":
            raise HTTPException(status_code=400, detail=result.get("message", "Invalid execution mode"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to set live execution mode: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/live/arm")
def arm_advance_live_trading(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Human-exclusive endpoint to arm Live Broker Mirroring via 4-digit security PIN.
    Verifies PIN, trading window (<15:15 IST), daily loss cap, and Upstox available margin for the selected mode.
    """
    try:
        bridge = get_advance_live_bridge()
        bridge.start_background_follower()
        pin = str(payload.get("pin") or "").strip()
        mode = payload.get("execution_mode")
        result = bridge.arm_live_trading(pin=pin, execution_mode=mode)
        if result.get("status") != "ARMED":
            raise HTTPException(status_code=400, detail=result.get("message", "Failed to arm live bridge"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to arm live trading: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/live/disarm")
def disarm_advance_live_trading() -> Dict[str, Any]:
    """Disarms Live Broker Mirroring and reverts immediately to 100% Paper Sandbox mode."""
    try:
        bridge = get_advance_live_bridge()
        return bridge.disarm_live_trading()
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to disarm live trading: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/live/sos-panic-exit")
def trigger_advance_sos_panic_exit() -> Dict[str, Any]:
    """Emergency 1-Click SOS Panic Exit: Flattens all open Advance live positions and disarms to Paper Sandbox."""
    try:
        bridge = get_advance_live_bridge()
        return bridge.execute_sos_panic_exit()
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to execute SOS panic exit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


