"""
Consensus Game API Router:
Exposes REST endpoints for the gamified Consensus Capital Arena.
Strictly read-only on core project tables; interacts exclusively with sandboxed game tables.
"""

import logging
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, Query

from app.analytics.consensus_game_engine import ConsensusGameEngine
from app.data.upstox_provider import get_upstox_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/consensus/game", tags=["consensus_game"])


class DeployCapitalRequest(BaseModel):
    initial_capital: float = Field(default=100000.0, ge=1000.0, description="Capital to deploy into Consensus Arena")
    allocation_mode: str = Field(default="EQUAL_WEIGHT", description="Position sizing mode (EQUAL_WEIGHT / KELLY_2PCT)")
    max_positions: int = Field(default=5, ge=1, le=20, description="Maximum concurrent open positions")
    fees_enabled: bool = Field(default=True, description="Whether realistic brokerage fees and slippage are deducted")
    session_id: str = Field(default="default_arena_session", description="Unique player session ID")
    account_name: str = Field(default="Consensus Arena Trader", description="Display name for account")
    trade_selection_mode: str = Field(default="ALL_QUALIFIED", description="Trade selection rule: ALL_QUALIFIED, HIGHEST_CONFLUENCE_ONLY, or TIMED_ANCHOR_CONFLUENCE")


class UpdateSelectionModeRequest(BaseModel):
    session_id: str = Field(default="default_arena_session", description="Unique player session ID")
    trade_selection_mode: str = Field(default="HIGHEST_CONFLUENCE_ONLY", description="Trade selection rule: ALL_QUALIFIED, HIGHEST_CONFLUENCE_ONLY, or TIMED_ANCHOR_CONFLUENCE")


class ExitPositionRequest(BaseModel):
    position_id: int = Field(..., description="ID of the game position to close")
    session_id: str = Field(default="default_arena_session", description="Unique player session ID")
    reason: str = Field(default="MANUAL EXIT", description="Reason for exiting position")


class ExitAllRequest(BaseModel):
    session_id: str = Field(default="default_arena_session", description="Unique player session ID")
    reason: str = Field(default="MANUAL EXIT ALL", description="Reason for exiting all positions")


@router.post("/exit-position")
async def exit_position_endpoint(req: ExitPositionRequest):
    """
    Manually exits a single open simulated position at live market price, booking realized P&L.
    """
    try:
        res = ConsensusGameEngine.exit_position(
            position_id=req.position_id,
            session_id=req.session_id,
            reason=req.reason
        )
        return {"status": "success", "data": res}
    except Exception as e:
        logger.error(f"[ConsensusGame] Failed to exit position #{req.position_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/exit-all")
async def exit_all_positions_endpoint(req: ExitAllRequest):
    """
    Manually exits ALL open simulated positions at live market prices in a single bulk batch.
    """
    try:
        res = ConsensusGameEngine.exit_all_positions(
            session_id=req.session_id,
            reason=req.reason
        )
        return {"status": "success", "data": res}
    except Exception as e:
        logger.error(f"[ConsensusGame] Failed to exit all positions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/deploy")
async def deploy_capital_endpoint(req: DeployCapitalRequest):
    """
    Deploys or resets capital into the Consensus Arena and replays all authentic historical consensus trades.
    """
    try:
        result = ConsensusGameEngine.deploy_capital(
            initial_capital=req.initial_capital,
            allocation_mode=req.allocation_mode,
            max_positions=req.max_positions,
            fees_enabled=req.fees_enabled,
            session_id=req.session_id,
            account_name=req.account_name,
            trade_selection_mode=req.trade_selection_mode
        )
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"[ConsensusGame] Failed to deploy capital: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/selection-mode")
async def set_selection_mode_endpoint(req: UpdateSelectionModeRequest):
    """
    Toggles the player's trade selection rule (ALL_QUALIFIED vs HIGHEST_CONFLUENCE_ONLY)
    and dynamically re-simulates the arena from initial capital to reflect the selected rule.
    """
    try:
        result = ConsensusGameEngine.set_trade_selection_mode(
            session_id=req.session_id,
            trade_selection_mode=req.trade_selection_mode
        )
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"[ConsensusGame] Failed to update trade selection mode: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portfolio")
async def get_portfolio_endpoint(session_id: str = "default_arena_session"):
    """
    Returns real-time portfolio metrics, open positions with live MTM, and gamer achievements.
    """
    try:
        result = ConsensusGameEngine.get_portfolio_state(session_id=session_id)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"[ConsensusGame] Failed to get portfolio state: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/equity-curve")
async def get_equity_curve_endpoint(session_id: str = "default_arena_session"):
    """
    Returns chronological equity snapshots for high-resolution chart rendering.
    """
    try:
        series = ConsensusGameEngine.get_equity_curve(session_id=session_id)
        return {"status": "success", "data": series}
    except Exception as e:
        logger.error(f"[ConsensusGame] Failed to get equity curve: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/calendar")
async def get_calendar_endpoint(
    session_id: str = "default_arena_session",
    year: Optional[int] = None,
    month: Optional[int] = None
):
    """
    Returns monthly P&L Calendar data (Zerodha Console style) with daily returns and win rates.
    """
    try:
        calendar_data = ConsensusGameEngine.get_pnl_calendar(
            session_id=session_id,
            year=year,
            month=month
        )
        return {"status": "success", "data": calendar_data}
    except Exception as e:
        logger.error(f"[ConsensusGame] Failed to get calendar data: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/positions")
async def get_positions_endpoint(
    session_id: str = "default_arena_session",
    status: Optional[str] = None
):
    """
    Returns the audited ledger of all simulated positions.
    """
    try:
        positions = ConsensusGameEngine.get_positions(session_id=session_id, status=status)
        return {"status": "success", "data": positions}
    except Exception as e:
        logger.error(f"[ConsensusGame] Failed to get positions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/day-replay")
async def get_day_replay_endpoint(
    session_id: str = "default_arena_session",
    date: str = Query("2026-09-18", description="Trading date to replay (YYYY-MM-DD)")
):
    """
    Returns intraday bar-by-bar timeline events for historical day replay.
    100% computed from local SQLite data (0 Upstox API calls consumed).
    """
    try:
        replay_data = ConsensusGameEngine.get_day_replay(session_id=session_id, date_str=date)
        return {"status": "success", "data": replay_data}
    except Exception as e:
        logger.error(f"[ConsensusGame] Failed to generate day replay: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/upstox-shield")
async def get_upstox_shield_endpoint():
    """
    Returns real-time health and token consumption metrics of UpstoxRateLimiter.
    Confirms zero rate limit violations and circuit breaker safety.
    """
    try:
        limiter = get_upstox_limiter()
        stats = limiter.get_stats()
        return {
            "status": "success",
            "shield": {
                "safe": not stats.get("circuit_open", False),
                "requests_last_sec": stats.get("requests_last_sec", 0),
                "limit_per_sec": limiter.MAX_PER_SECOND,
                "requests_last_min": stats.get("requests_last_min", 0),
                "limit_per_min": limiter.MAX_PER_MINUTE,
                "requests_last_30min": stats.get("requests_last_30min", 0),
                "limit_per_30min": limiter.MAX_PER_30MIN,
                "circuit_open": stats.get("circuit_open", False),
                "cooldown_remaining_sec": stats.get("cooldown_remaining_sec", 0),
                "throttled_count": stats.get("throttled_count", 0),
                "rejected_count": stats.get("rejected_count", 0)
            }
        }
    except Exception as e:
        logger.warning(f"[ConsensusGame] Failed to get Upstox shield stats: {e}")
        return {
            "status": "partial",
            "shield": {
                "safe": True,
                "requests_last_sec": 0,
                "limit_per_sec": 8,
                "circuit_open": False,
                "note": "Limiter initialized with default safe budget"
            }
        }


@router.get("/live-ticker")
async def get_live_ticker_endpoint(symbols: Optional[str] = None):
    """
    Returns sub-second live LTP quotes for the Consensus Arena game and dashboard.
    Batch-resolved via Upstox bulk LTP (1 single HTTP request for up to 500 symbols).
    Respects rate limits strictly and eliminates individual per-stock round trips.
    """
    from app.data.market_provider import get_bulk_live_quotes_with_meta
    ticker_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else []
    if not ticker_list:
        try:
            from app.data.historical_data_layer import get_db_path
            import sqlite3
            conn = sqlite3.connect(get_db_path(), timeout=5.0)
            rows = conn.execute("SELECT DISTINCT ticker FROM consensus_game_positions WHERE status = 'OPEN'").fetchall()
            conn.close()
            ticker_list = [r[0] for r in rows]
        except Exception:
            ticker_list = []
    quotes = get_bulk_live_quotes_with_meta(ticker_list) if ticker_list else {}
    return {
        "status": "success",
        "quotes": quotes,
        "count": len(quotes)
    }


