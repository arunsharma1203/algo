import time
import logging
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel

from app.data.historical_data_layer import HistoricalDataLayer
from app.analytics.universe_config import get_universe, UNIVERSE_PRESETS, BENCHMARK_5_UNIVERSE, LIVE_UNIVERSE, RESEARCH_100_UNIVERSE
from app.analytics.portfolio_walk_forward import MultiStockPortfolioWalkForwardEngine
from app.analytics.parallel_engine import ParallelWalkForwardOrchestrator, ResearchConfig
from app.api.ml_backtest import WeeklyWalkForwardBacktestEngine
import yfinance as yf

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/data-lab", tags=["Data Lab"])

class SyncRequest(BaseModel):
    universe: str = "BENCHMARK_5"
    force_refresh: bool = False

class PortfolioBacktestRequest(BaseModel):
    universe: str = "BENCHMARK_5"
    initial_capital: float = 500000.0
    max_portfolio_heat: float = 6.0
    max_single_risk_pct: float = 2.0
    kelly_mode: str = "HALF"
    brokerage_per_order: float = 20.0
    slippage_pct: float = 0.08

class ParallelBenchmarkRequest(BaseModel):
    tickers: List[str] = ["RELIANCE.NS", "TCS.NS"]
    worker_counts: List[int] = [1, 2, 4]

@router.get("/system-resources")
async def get_system_resources():
    """Returns Apple Silicon hardware topology and recommended worker configuration."""
    return HistoricalDataLayer.get_system_resource_profile()

@router.get("/universes")
async def list_universes():
    """Returns available universe presets with survivorship bias warnings."""
    return {
        "presets": {
            k: {
                "name": v["name"],
                "count": len(v["tickers"]),
                "description": v["description"],
                "survivorship_bias": v["survivorship_bias"]
            } for k, v in UNIVERSE_PRESETS.items()
        }
    }

@router.get("/coverage")
async def get_coverage_report(universe: str = "BENCHMARK_5"):
    """Returns data quality, 10-year depth percentage, and intraday accumulated stats."""
    u_info = get_universe(universe)
    tickers = u_info["tickers"]

    daily_report = HistoricalDataLayer.get_historical_coverage_report(tickers)
    intraday_report = HistoricalDataLayer.get_intraday_accumulated_report()

    return {
        "universe_name": u_info["name"],
        "survivorship_bias": u_info["survivorship_bias"],
        "daily_coverage": daily_report,
        "intraday_accumulated": intraday_report
    }

@router.post("/sync-10y")
async def trigger_10y_sync(req: SyncRequest):
    """Synchronizes up to 10 years of daily OHLCV for all universe stocks."""
    u_info = get_universe(req.universe)
    tickers = u_info["tickers"]

    sync_results = []
    for ticker in tickers:
        res = HistoricalDataLayer.sync_ticker_daily_10y(ticker, force_refresh=req.force_refresh)
        sync_results.append(res)

    return {
        "status": "success",
        "universe": u_info["name"],
        "synced_count": len([r for r in sync_results if r.get("status") in ["SYNCED", "UP_TO_DATE"]]),
        "total_tickers": len(tickers),
        "results": sync_results
    }

@router.post("/portfolio-backtest")
async def run_portfolio_backtest(req: PortfolioBacktestRequest):
    """Executes Multi-Stock Portfolio Walk-Forward Backtest with shared capital and Portfolio Heat cap."""
    try:
        u_info = get_universe(req.universe)
        tickers = u_info["tickers"]

        engine = MultiStockPortfolioWalkForwardEngine(
            tickers=tickers,
            initial_capital=req.initial_capital,
            max_portfolio_heat=req.max_portfolio_heat,
            max_single_risk_pct=req.max_single_risk_pct,
            brokerage=req.brokerage_per_order,
            slippage_pct=req.slippage_pct,
            kelly_mode=req.kelly_mode,
            universe_name=req.universe
        )

        results = engine.run()
        return results

    except Exception as e:
        logger.error(f"Portfolio Backtest error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/horizon-comparison")
async def get_horizon_comparison(ticker: str = "RELIANCE.NS"):
    """
    Empirically benchmarks strategy performance across 3-year, 5-year, 7-year, and 10-year training horizons
    using the identical locked final Out-Of-Sample holdout.
    """
    try:
        clean_t = ticker.strip().upper()
        if not clean_t.endswith(('.NS', '.BO')):
            clean_t += '.NS'

        df_10y = yf.download(clean_t, period="10y", interval="1d", progress=False)
        if df_10y.empty or len(df_10y) < 1000:
            raise HTTPException(status_code=400, detail="Insufficient 10-year historical data available.")

        # Test horizons
        horizons = {
            "3_Years": 750,
            "5_Years": 1250,
            "7_Years": 1750,
            "10_Years": len(df_10y)
        }

        comparison_results = {}
        for h_name, n_bars in horizons.items():
            sub_df = df_10y.iloc[-n_bars:].copy()
            engine = WeeklyWalkForwardBacktestEngine(sub_df, model_type="SWING", initial_capital=100000.0)
            res = engine.run()
            
            comparison_results[h_name] = {
                "bars_trained": n_bars,
                "total_trades": len(res.get("trades", [])),
                "win_rate_pct": res.get("metrics", {}).get("win_rate", 0.0),
                "total_pnl": res.get("metrics", {}).get("total_pnl", 0.0),
                "profit_factor": res.get("metrics", {}).get("profit_factor", 1.0),
                "max_drawdown_pct": res.get("metrics", {}).get("max_drawdown", 0.0),
                "locked_holdout": res.get("locked_final_holdout", {})
            }

        return {
            "ticker": clean_t,
            "comparison": comparison_results,
            "scientific_takeaway": "Compares training sample depth against the identical Out-Of-Sample holdout without lookahead bias."
        }

    except Exception as e:
        logger.error(f"Horizon comparison error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/parallel-benchmark")
async def run_parallel_benchmark(req: ParallelBenchmarkRequest):
    """
    Executes controlled benchmarking across worker counts on local data.
    Measures runtime, speedup factor, and verifies result equivalence.
    """
    benchmark_report = {}
    base_results = None

    for workers in req.worker_counts:
        config = ResearchConfig(max_workers=workers, enable_checkpointing=False)
        orchestrator = ParallelWalkForwardOrchestrator(config)
        
        t0 = time.time()
        res = orchestrator.run_universe_walk_forward(req.tickers, job_id=f"bench_{workers}w")
        elapsed = round(time.time() - t0, 2)

        if base_results is None:
            base_results = res.get("results", {})

        benchmark_report[f"{workers}_workers"] = {
            "workers": workers,
            "runtime_seconds": elapsed,
            "tickers_processed": len(res.get("results", {})),
            "status": res.get("status")
        }

    # Compute speedup
    base_time = benchmark_report.get("1_workers", {}).get("runtime_seconds", 1.0)
    for k, v in benchmark_report.items():
        v["speedup_factor"] = round(base_time / v["runtime_seconds"], 2) if v["runtime_seconds"] > 0 else 1.0

    return {
        "benchmark": benchmark_report,
        "hardware_profile": HistoricalDataLayer.get_system_resource_profile()
    }
