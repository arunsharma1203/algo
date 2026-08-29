import os
import sys
import time
import json
import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field, asdict

# Ensure sub-process inherits correct backend path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

logger = logging.getLogger(__name__)

def _worker_init(ml_threads: int = 1):
    """
    Initializes worker process environment to enforce single-threaded BLAS/OpenMP
    and prevent thread oversubscription on Apple Silicon.
    """
    os.environ["OMP_NUM_THREADS"] = str(ml_threads)
    os.environ["OPENBLAS_NUM_THREADS"] = str(ml_threads)
    os.environ["MKL_NUM_THREADS"] = str(ml_threads)
    os.environ["VECLIB_MAXIMUM_THREADS"] = str(ml_threads)
    os.environ["NUMEXPR_NUM_THREADS"] = str(ml_threads)

@dataclass
class ResearchConfig:
    max_workers: int = 4
    ml_threads_per_worker: int = 1
    checkpoint_dir: str = "backend/checkpoints"
    enable_checkpointing: bool = True
    cpu_limit_pct: float = 75.0

def _run_ticker_walk_forward_worker(ticker: str, initial_capital: float = 100000.0,
                                   model_type: str = "SWING") -> Dict[str, Any]:
    """
    Self-contained worker execution function for a single ticker.
    Computes expanding weekly walk-forward strictly in worker memory without SQLite writes.
    """
    try:
        from app.data.historical_data_layer import HistoricalDataLayer
        from app.api.ml_backtest import WeeklyWalkForwardBacktestEngine

        df = HistoricalDataLayer.get_historical_ohlcv(ticker, timeframe="1d")
        if df.empty or len(df) < 300:
            return {
                "ticker": ticker,
                "status": "INSUFFICIENT_DATA",
                "total_trades": 0,
                "net_pnl": 0.0,
                "win_rate": 0.0
            }

        engine = WeeklyWalkForwardBacktestEngine(
            df=df,
            model_type=model_type,
            initial_capital=initial_capital
        )
        res = engine.run()
        
        return {
            "ticker": ticker,
            "status": "SUCCESS",
            "metrics": res.get("metrics", {}),
            "trades_count": len(res.get("trades", [])),
            "champion_lifecycle": res.get("champion_challenger_lifecycle", {}),
            "locked_holdout": res.get("locked_final_holdout", {})
        }
    except Exception as e:
        logger.error(f"Worker failure on {ticker}: {e}")
        return {
            "ticker": ticker,
            "status": "ERROR",
            "error": str(e)
        }

class ParallelWalkForwardOrchestrator:
    """
    Controlled Apple Silicon Parallel Execution Orchestrator.
    Manages process worker allocation, progress tracking, checkpointing,
    and single-threaded aggregation to prevent CPU oversubscription and SQLite locks.
    """

    def __init__(self, config: Optional[ResearchConfig] = None):
        self.config = config or ResearchConfig()
        if self.config.enable_checkpointing:
            os.makedirs(self.config.checkpoint_dir, exist_ok=True)

    def _get_checkpoint_path(self, job_id: str) -> str:
        return os.path.join(self.config.checkpoint_dir, f"checkpoint_{job_id}.json")

    def save_checkpoint(self, job_id: str, completed_results: Dict[str, Any]):
        if not self.config.enable_checkpointing:
            return
        path = self._get_checkpoint_path(job_id)
        try:
            with open(path, "w") as f:
                json.dump(completed_results, f)
        except Exception as e:
            logger.warning(f"Could not save checkpoint: {e}")

    def load_checkpoint(self, job_id: str) -> Dict[str, Any]:
        if not self.config.enable_checkpointing:
            return {}
        path = self._get_checkpoint_path(job_id)
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load checkpoint: {e}")
        return {}

    def clear_checkpoint(self, job_id: str):
        path = self._get_checkpoint_path(job_id)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    def run_universe_walk_forward(self, tickers: List[str], job_id: str = "universe_run",
                                   initial_capital: float = 100000.0,
                                   progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """
        Executes parallel walk-forward across multiple tickers with checkpointing and progress telemetry.
        """
        start_time = time.time()
        results = self.load_checkpoint(job_id)
        pending_tickers = [t for t in tickers if t not in results]

        total_jobs = len(tickers)
        completed_jobs = len(results)

        if pending_tickers:
            num_workers = min(self.config.max_workers, len(pending_tickers))
            logger.info(f"Launching {num_workers} parallel workers on Apple Silicon for {len(pending_tickers)} pending tickers...")

            with ProcessPoolExecutor(
                max_workers=num_workers,
                initializer=_worker_init,
                initargs=(self.config.ml_threads_per_worker,)
            ) as executor:
                future_to_ticker = {
                    executor.submit(_run_ticker_walk_forward_worker, t, initial_capital): t
                    for t in pending_tickers
                }

                for future in as_completed(future_to_ticker):
                    t = future_to_ticker[future]
                    try:
                        res = future.result()
                        results[t] = res
                    except Exception as e:
                        results[t] = {"ticker": t, "status": "FAILED", "error": str(e)}

                    completed_jobs += 1
                    elapsed = time.time() - start_time
                    rate = completed_jobs / elapsed if elapsed > 0 else 1.0
                    remaining_jobs = total_jobs - completed_jobs
                    estimated_remaining_seconds = round(remaining_jobs / rate, 1) if rate > 0 else 0.0

                    self.save_checkpoint(job_id, results)

                    telemetry = {
                        "job_id": job_id,
                        "total_jobs": total_jobs,
                        "completed_jobs": completed_jobs,
                        "active_workers": num_workers,
                        "elapsed_seconds": round(elapsed, 1),
                        "estimated_remaining_seconds": estimated_remaining_seconds,
                        "latest_completed": t
                    }

                    if progress_callback:
                        progress_callback(telemetry)

        # Clear checkpoint on clean completion
        self.clear_checkpoint(job_id)
        total_time = round(time.time() - start_time, 2)

        return {
            "status": "SUCCESS",
            "job_id": job_id,
            "total_tickers": total_jobs,
            "total_runtime_seconds": total_time,
            "workers_used": self.config.max_workers,
            "results": results
        }

