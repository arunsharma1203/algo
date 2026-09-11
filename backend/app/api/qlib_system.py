"""
QLIB SYSTEM API ROUTER
======================
FastAPI endpoints for the parallel Microsoft Qlib trading and research system.

Features:
- Live Qlib environment telemetry
- Multi-strategy scanner endpoints (Swing, Intraday, F&O)
- Dynamic training execution
- Virtual recommendation log & performance
- Legacy vs Qlib comparison matrix
"""

import sys
import platform
import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel

import qlib
from app.qlib_engine.india_data_adapter import get_qlib_adapter, ensure_qlib_ready
from app.qlib_engine.feature_pipeline import QlibFeaturePipeline
from app.qlib_engine.model_registry import QlibModelRegistry, QlibModelIntegrityError
from app.qlib_engine.scanners import QlibScanners, QlibModelUnavailableError
from app.qlib_engine.trainer import QlibTrainer
from app.qlib_engine.virtual_tracker import QlibVirtualTracker
from app.qlib_engine.comparison_service import ComparisonService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/qlib", tags=["qlib_system"])


class QlibScanRequest(BaseModel):
    strategy: str = "SWING"  # 'SWING', 'INTRADAY', 'FNO'
    universe: str = "ALL_DATABASE_STOCKS"
    top_k: int = 5
    tickers: Optional[List[str]] = None


class QlibTrainRequest(BaseModel):
    strategy: str = "SWING"  # 'SWING', 'INTRADAY', 'FNO'
    feature_family: str = "Alpha158"  # 'Alpha158' or 'Alpha360'
    model_family: str = "LGBModel"  # 'LGBModel', 'DEnsembleModel', 'CatBoostModel'
    instruments: Optional[List[str]] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None


@router.get("/status")
def get_qlib_system_status():
    """
    Returns complete telemetry for the Microsoft Qlib engine.
    """
    from app.analytics.universe_config import resolve_all_database_stocks
    adapter = get_qlib_adapter()
    uni_stats = resolve_all_database_stocks(timeframe="1d", min_bars=60)
    return {
        "engine": "QLIB",
        "badge": "QLIB ENGINE • ACTIVE",
        "qlib_installed": True,
        "qlib_version": getattr(qlib, "__version__", "unknown"),
        "qlib_package_file": getattr(qlib, "__file__", "unknown"),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "provider_uri": adapter.provider_uri,
        "feature_handlers": ["Alpha158", "Alpha360"],
        "supported_models": ["LGBModel", "DEnsembleModel", "CatBoostModel"],
        "models_status": QlibModelRegistry.get_registry_status(),
        "system_mode": "PARALLEL_EXPERIMENTAL_BRANCH",
        "primary_universe": "ALL_DATABASE_STOCKS",
        "universe_stats": {
            "stocks_discovered": uni_stats["discovered_count"],
            "stocks_valid": uni_stats["valid_count"],
            "stocks_scan_ready": uni_stats["scan_ready_count"]
        }
    }


@router.get("/universe/stats")
def get_universe_stats():
    """Returns dynamic discovery and validation counts for database universe."""
    from app.analytics.universe_config import resolve_all_database_stocks
    stats = resolve_all_database_stocks(timeframe="1d", min_bars=60)
    return {
        "universe": "ALL_DATABASE_STOCKS",
        "stocks_discovered": stats["discovered_count"],
        "stocks_valid": stats["valid_count"],
        "stocks_scan_ready": stats["scan_ready_count"],
        "invalid_symbols_count": len(stats["invalid_symbols"]),
        "insufficient_bar_symbols_count": len(stats["insufficient_bar_symbols"])
    }


@router.get("/models")
def get_registered_models():
    """Returns active models and provenance manifests across all strategies."""
    return {
        "engine": "QLIB",
        "registry": QlibModelRegistry.get_registry_status()
    }


@router.post("/scan")
def run_scan(req: QlibScanRequest):
    """
    Executes a real Qlib scan on fresh market data.
    """
    strat = req.strategy.upper().strip()
    try:
        if strat == "SWING":
            return QlibScanners.run_swing_scan(
                universe=req.universe,
                tickers=req.tickers,
                top_k=req.top_k
            )
        elif strat == "INTRADAY":
            return QlibScanners.run_intraday_scan(
                tickers=req.tickers,
                top_k=req.top_k
            )
        elif strat == "FNO":
            return QlibScanners.run_fno_scan(
                underlyings=req.tickers,
                top_k=req.top_k
            )
        else:
            raise HTTPException(status_code=400, detail=f"Invalid strategy: {req.strategy}")
    except QlibModelUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"[QlibAPI] Scan error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Qlib scan failed: {e}")


@router.post("/train")
def train_model(req: QlibTrainRequest):
    """
    Trains a real Qlib model on Indian historical data.
    """
    strat = req.strategy.upper().strip()
    freq = "15min" if strat == "INTRADAY" else "day"
    try:
        res = QlibTrainer.train_model(
            strategy=strat,
            instruments=req.instruments,
            feature_family=req.feature_family,
            model_family=req.model_family,
            start_date=req.start_date,
            end_date=req.end_date,
            freq=freq
        )
        return res
    except Exception as e:
        logger.error(f"[QlibAPI] Training failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Qlib training failed: {e}")


@router.get("/recommendations")
def get_recommendations(strategy: Optional[str] = None, limit: int = 50):
    """Returns virtual recommendation history from SQLite."""
    return {
        "engine": "QLIB",
        "recommendations": QlibVirtualTracker.get_recommendations(strategy=strategy, limit=limit),
        "performance": QlibVirtualTracker.get_performance_summary(strategy=strategy)
    }


@router.get("/fno")
def get_fno_opportunities():
    """Runs or returns F&O scanner opportunities."""
    return QlibScanners.run_fno_scan()


@router.get("/comparison")
def get_system_comparison():
    """Returns apples-to-apples Legacy vs Qlib comparison scorecard."""
    return ComparisonService.get_side_by_side_comparison()
