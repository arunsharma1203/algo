"""
Option Sniffer: FastAPI REST API Endpoints.

Dedicated routes for Option Sniffer surveillance dashboard:
  - /api/option-sniffer/status
  - /api/option-sniffer/candidates
  - /api/option-sniffer/signals
  - /api/option-sniffer/paper-trades
  - /api/option-sniffer/pnl
  - /api/option-sniffer/calibration
  - /api/option-sniffer/scan
"""

import logging
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Query

from app.analytics.option_sniffer.option_sniffer_orchestrator import get_option_sniffer_orchestrator
from app.analytics.option_sniffer.option_db import OptionDB

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/option-sniffer", tags=["Option Sniffer"])


@router.get("/status")
def get_sniffer_status() -> Dict[str, Any]:
    """Provides operational status of Option Sniffer feed, model, and active trades."""
    try:
        orch = get_option_sniffer_orchestrator()
        if not orch._is_running:
            orch.start()
        return orch.get_status()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/candidates")
def get_sniffer_candidates() -> List[Dict[str, Any]]:
    """Returns active monitored candidate option contracts with live order book quotes."""
    try:
        orch = get_option_sniffer_orchestrator()
        return orch.get_candidates()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get candidates: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals")
def get_sniffer_signals(
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = Query(None)
) -> List[Dict[str, Any]]:
    """Returns historical and active signals from the state machine."""
    try:
        return OptionDB.get_recent_signals(limit=limit, status=status)
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get signals: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/paper-trades")
def get_sniffer_paper_trades() -> Dict[str, Any]:
    """Returns active open paper positions and closed trade performance."""
    try:
        orch = get_option_sniffer_orchestrator()
        return orch.get_paper_trades()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get paper trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pnl")
def get_sniffer_pnl() -> Dict[str, Any]:
    """Returns cumulative and today's realized/unrealized paper P&L metrics."""
    try:
        return OptionDB.get_pnl_summary()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get PnL: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/calibration")
def get_sniffer_calibration() -> Dict[str, Any]:
    """Returns empirical calibration curves, Brier score, and feature ablation results."""
    try:
        orch = get_option_sniffer_orchestrator()
        return orch.get_calibration_stats()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get calibration stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/scan")
def trigger_sniffer_scan() -> Dict[str, Any]:
    """Triggers an immediate evaluation sweep across all active option contracts."""
    try:
        orch = get_option_sniffer_orchestrator()
        results = orch.run_surveillance_sweep()
        return {"status": "SUCCESS", "results": results}
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to run scan sweep: {e}")
        raise HTTPException(status_code=500, detail=str(e))
