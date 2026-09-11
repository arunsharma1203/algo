"""
FastAPI Router for Unified Smart Scanner.
Routes all manual screening sweeps through the authoritative SmartScannerPipeline.
"""

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from typing import Optional, List

from app.analytics.smart_scanner import SmartScannerPipeline
from app.analytics.model_registry import ModelRegistry
from app.analytics.macro_engine import get_macro_regime
from app.analytics.position_monitor import PositionMonitorService
from app.analytics.universe_config import UNIVERSE_PRESETS

router = APIRouter(prefix="/smart-scanner", tags=["Smart Scanner"])

@router.get("/sweep")
async def sweep(
    timeframe: str = Query("intraday", description="Scanning timeframe: 'intraday' (15m) or 'swing' (1d)"),
    universe: str = Query("NIFTY_500", description="Target universe preset"),
    custom_tickers: Optional[str] = Query(None, description="Comma-separated custom tickers"),
    min_confidence: float = Query(60.0, description="Minimum conviction threshold (0-100)")
):
    """
    Executes unified AI sweep streaming Server-Sent Events (SSE).
    Guarantees 0.0% portfolio heat and strict Champion model integrity.
    """
    clean_custom = [t.strip() for t in custom_tickers.split(",") if t.strip()] if custom_tickers else None

    return StreamingResponse(
        SmartScannerPipeline.run_sweep(
            timeframe=timeframe,
            universe=universe,
            custom_tickers=clean_custom,
            min_confidence=min_confidence
        ),
        media_type="text/event-stream"
    )

@router.get("/universes")
def get_available_universes():
    """Returns available universe options and descriptions."""
    return {
        "presets": [
            {"id": "NIFTY_500", "label": "NIFTY 500 (Broad Market)", "count": 500},
            {"id": "NIFTY_50", "label": "NIFTY 50 (Blue-Chip Large Cap)", "count": 50},
            {"id": "BANK_NIFTY", "label": "Bank NIFTY (Banking Sector)", "count": 12},
            {"id": "NIFTY_IT", "label": "NIFTY IT (Technology Sector)", "count": 10},
            {"id": "LIVE_52", "label": "Live Priority Universe", "count": 52},
            {"id": "WATCHLIST", "label": "My Watchlist", "count": "Dynamic"},
            {"id": "BENCHMARK_5", "label": "Benchmark 5 (Fast Smoke Test)", "count": 5},
            {"id": "CUSTOM", "label": "Custom Symbol Selection", "count": "User-Defined"}
        ]
    }

@router.get("/status")
def get_scanner_status():
    """Returns current macro regime, active model verification, and heat status."""
    return {
        "champions": ModelRegistry.verify_all_champions(),
        "macro": get_macro_regime(),
        "positions": PositionMonitorService.get_open_positions_summary()
    }
