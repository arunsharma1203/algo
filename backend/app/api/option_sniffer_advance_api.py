"""
Dedicated routes for Option Sniffer Advance (100% Sandboxed Dual-Book Paper Dashboard):
  - GET  /api/option-sniffer-advance/status
  - GET  /api/option-sniffer-advance/chain-intelligence
  - GET  /api/option-sniffer-advance/signals
  - GET  /api/option-sniffer-advance/trades
  - GET  /api/option-sniffer-advance/walkforward-audit
  - POST /api/option-sniffer-advance/scan
"""

import logging
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Query

from app.analytics.option_sniffer_advance.advance_orchestrator import (
    get_option_sniffer_advance_orchestrator,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/option-sniffer-advance",
    tags=["Option Sniffer Advance (Dual-Book Sandbox)"],
)


@router.get("/status")
def get_advance_status() -> Dict[str, Any]:
    """Provides operational status of Option Sniffer Advance Dual-Book engine, chain intelligence, and P&L."""
    try:
        orch = get_option_sniffer_advance_orchestrator()
        if not orch._is_running:
            orch.start()
        return orch.get_status()
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to get status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chain-intelligence")
def get_advance_chain_intelligence() -> Dict[str, Any]:
    """Returns cross-sectional option chain intelligence (GEX, VRP, Put-Call Parity Basis, PCR, OI Walls, Max Pain)."""
    try:
        orch = get_option_sniffer_advance_orchestrator()
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
        orch = get_option_sniffer_advance_orchestrator()
        return orch.get_signals(limit=limit, book_type=book_type)
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to get signals: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trades")
def get_advance_trades() -> Dict[str, Any]:
    """Returns open and closed Dual-Book paper positions and split Long vs Short Book P&L summary."""
    try:
        orch = get_option_sniffer_advance_orchestrator()
        return orch.get_trades()
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to get trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/walkforward-audit")
def get_advance_walkforward_audit(force_refresh: bool = Query(False)) -> Dict[str, Any]:
    """Returns the causal walk-forward Dual-Book audit across Oct 19 – Oct 30, 2026."""
    try:
        orch = get_option_sniffer_advance_orchestrator()
        return orch.get_walkforward_audit_report(force_refresh=force_refresh)
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to get walkforward audit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/scan")
def trigger_advance_scan() -> Dict[str, Any]:
    """Triggers an immediate Dual-Book evaluation sweep across all active option contracts."""
    try:
        orch = get_option_sniffer_advance_orchestrator()
        results = orch.run_surveillance_sweep()
        return {"status": "SUCCESS", "results": results}
    except Exception as e:
        logger.error(f"[OptionSnifferAdvanceAPI] Failed to run scan sweep: {e}")
        raise HTTPException(status_code=500, detail=str(e))

