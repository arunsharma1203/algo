"""
FULL UNIVERSE QLIB INTRADAY RETRAINING RUNNER
==============================================
Trains Microsoft Qlib Intraday model with Alpha158 across all 508 stocks
using complete 15-minute history up to today (2026-09-18).
Evaluates on locked 20% Out-Of-Sample partition.
"""

import time
import json
import logging
from qlib.data import D
from app.qlib_engine.india_data_adapter import ensure_qlib_ready
from app.qlib_engine.trainer import QlibTrainer
from app.qlib_engine.model_registry import QlibModelRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def main():
    logger.info("Initializing Qlib environment...")
    adapter = ensure_qlib_ready()
    
    # Get all available 15m instruments in binary store
    all_instruments = sorted(list(D.list_instruments(instruments=D.instruments(market="all")).keys()))
    logger.info(f"Resolved {len(all_instruments)} instruments with complete 15m binary features.")
    
    t0 = time.time()
    logger.info("Starting Full Universe Qlib Intraday Training (Alpha158, LGBModel, 15min frequency)...")
    
    result = QlibTrainer.train_model(
        strategy="INTRADAY",
        instruments=all_instruments,
        universe="ALL_DATABASE_STOCKS",
        feature_family="Alpha158",
        model_family="LGBModel",
        horizon_days=5,
        target_definition="FORWARD_RETURN",
        freq="15min"
    )
    
    elapsed = round(time.time() - t0, 2)
    logger.info(f"Training completed in {elapsed}s.")
    
    candidate_id = result["candidate_model_id"]
    oos_metrics = result["oos_metrics"]
    
    print("\n" + "="*70)
    print(f"TRAINING SUCCESSFUL: Candidate Model ID: {candidate_id}")
    print(f"Elapsed Time: {elapsed} seconds")
    print(f"Instruments Trained: {result['instruments_count']}")
    print(f"Artifact SHA-256: {result['artifact_sha256']}")
    print("="*70)
    print("Out-Of-Sample Scorecard:")
    print(f"  IC: {oos_metrics.get('ic')}")
    print(f"  Rank IC: {oos_metrics.get('rank_ic')}")
    print(f"  Trades Count: {oos_metrics.get('trades_count')}")
    print(f"  Raw Win Rate: {oos_metrics.get('signal_metrics', {}).get('raw_win_rate')}%")
    print(f"  Raw Profit Factor: {oos_metrics.get('signal_metrics', {}).get('raw_profit_factor')}")
    print(f"  Gross Mean Trade P&L: {oos_metrics.get('signal_metrics', {}).get('gross_mean_trade_pnl_pct')}%")
    print("="*70 + "\n")
    
    # Save training summary JSON
    summary_path = f"models/qlib/challengers/intraday/{candidate_id}/training_summary.json"
    try:
        with open(summary_path, "w") as f:
            json.dump({
                "candidate_model_id": candidate_id,
                "elapsed_seconds": elapsed,
                "instruments_count": result["instruments_count"],
                "oos_metrics": oos_metrics
            }, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save summary JSON: {e}")

if __name__ == "__main__":
    main()

