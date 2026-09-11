"""
QLIB MODEL DISCOVERY API ROUTER
================================
Provenance:
    Implementation: "QLIB-INSPIRED / QLIB-COMPATIBLE FEATURE IMPLEMENTATION"
    Feature Version: "qlib_research_v1"
    Strategy Support: Intraday (15m) & Swing (1D)
    Strict Research Isolation: Dedicated table 'qlib_model_evaluations'
"""

import logging
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, Dict, Any, List

from app.analytics.qlib_discovery.pipeline_runner import run_qlib_candidate_search
from app.analytics.qlib_discovery.qlib_registry import (
    get_qlib_evaluations,
    verify_champion_immutability,
    ensure_qlib_table
)

logger = logging.getLogger(__name__)
router = APIRouter()

PROVENANCE_LABEL = "QLIB-INSPIRED / QLIB-COMPATIBLE FEATURE IMPLEMENTATION"
FEATURE_VERSION = "qlib_research_v1"

class QlibEvaluateRequest(BaseModel):
    strategy: str = "SWING"  # SWING or INTRADAY
    model_family: str = "lightgbm"  # lightgbm, catboost, xgboost, double_ensemble
    feature_family: str = "Alpha158"  # Alpha158 or Alpha360
    universe: str = "LIVE_52"
    limit_tickers: Optional[int] = None

@router.get("/status")
def get_qlib_status():
    """Returns runtime environment status, model availability, and provenance metadata."""
    ensure_qlib_table()
    verify_champion_immutability()
    
    import lightgbm as lgb
    import xgboost as xgb
    import catboost as cb
    
    past_evals = get_qlib_evaluations()
    
    return {
        "status": "success",
        "provenance": PROVENANCE_LABEL,
        "feature_version": FEATURE_VERSION,
        "supported_strategies": ["SWING", "INTRADAY"],
        "supported_features": ["Alpha158", "Alpha360"],
        "supported_models": ["LightGBM", "CatBoost", "XGBoost", "DoubleEnsemble"],
        "packages": {
            "lightgbm": getattr(lgb, '__version__', 'installed'),
            "xgboost": getattr(xgb, '__version__', 'installed'),
            "catboost": getattr(cb, '__version__', 'installed')
        },
        "total_evaluations": len(past_evals),
        "recent_evaluations": past_evals[:10]
    }

@router.post("/evaluate")
def run_evaluation_api(req: QlibEvaluateRequest):
    """
    Runs bounded model search, evaluates on frozen locked OOS,
    and returns complete economic & classification report against Champion.
    """
    try:
        candidate_record, champion_benchmark = run_qlib_candidate_search(
            strategy=req.strategy.upper(),
            model_family=req.model_family.lower(),
            feature_family=req.feature_family,
            universe_name=req.universe,
            limit_tickers=req.limit_tickers
        )
        return {
            "status": "success",
            "provenance": PROVENANCE_LABEL,
            "feature_version": FEATURE_VERSION,
            "candidate": candidate_record,
            "champion_benchmark": champion_benchmark,
            "gate_verdict": candidate_record.get("gate_verdict", "RETAIN CHAMPION")
        }
    except Exception as e:
        logger.error(f"[QlibResearch] Evaluation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/evaluations")
def get_evaluations_api(strategy: Optional[str] = None):
    """Retrieves all persisted Qlib candidate evaluations."""
    ensure_qlib_table()
    evals = get_qlib_evaluations(strategy=strategy)
    return {
        "status": "success",
        "strategy": strategy,
        "count": len(evals),
        "evaluations": evals
    }

class SignalDiscoveryRunRequest(BaseModel):
    universe: str = "LIVE_52"
    horizons: List[int] = [1, 3, 5, 10, 20]
    seeds: List[int] = [42, 101, 777]
    max_tickers: Optional[int] = None

@router.post("/signal-discovery/run")
def run_signal_discovery_api(req: Optional[SignalDiscoveryRunRequest] = None):
    """
    Executes the comprehensive QLib Signal Discovery Engine V1.
    Evaluates Alpha158 features, horizons, models, cross-sectional ranking,
    regimes, and locked OOS under strict research isolation.
    """
    from app.analytics.qlib_discovery.signal_discovery_engine import QlibSignalDiscoveryEngine
    req_obj = req or SignalDiscoveryRunRequest()
    try:
        results = QlibSignalDiscoveryEngine.run_staged_discovery(
            universe=req_obj.universe,
            horizons=req_obj.horizons,
            seeds=req_obj.seeds,
            max_tickers=req_obj.max_tickers
        )
        return {
            "status": "success",
            "experiment_id": results["experiment_id"],
            "verdict": results["final_verdict"],
            "discovery_results": results
        }
    except Exception as e:
        logger.error(f"[SignalDiscovery] Run failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/signal-discovery/latest")
def get_latest_signal_discovery_api():
    """Retrieves the latest completed QLib Signal Discovery experiment."""
    from app.analytics.qlib_discovery.discovery_ledger import get_latest_discovery_run
    run = get_latest_discovery_run()
    if not run:
        return {"status": "empty", "message": "No discovery runs completed yet."}
    return {
        "status": "success",
        "discovery_run": run
    }

@router.get("/signal-discovery/experiments")
def get_all_signal_discovery_experiments_api():
    """Lists past signal discovery experiments."""
    from app.analytics.qlib_discovery.discovery_ledger import list_all_experiments
    exps = list_all_experiments()
    return {
        "status": "success",
        "count": len(exps),
        "experiments": exps
    }

@router.get("/signal-discovery/features")
def get_signal_discovery_features_api(experiment_id: Optional[str] = None):
    """Retrieves Alpha158 feature screening stats for the latest or specified experiment."""
    from app.analytics.qlib_discovery.discovery_ledger import get_latest_discovery_run
    run = get_latest_discovery_run()
    if not run:
        return {"status": "empty", "features": []}
    return {
        "status": "success",
        "experiment_id": run["experiment_id"],
        "features": run.get("feature_stats", [])
    }

@router.get("/signal-discovery/deciles")
def get_signal_discovery_deciles_api(experiment_id: Optional[str] = None):
    """Retrieves cross-sectional decile progression for the latest or specified experiment."""
    from app.analytics.qlib_discovery.discovery_ledger import get_latest_discovery_run
    run = get_latest_discovery_run()
    if not run:
        return {"status": "empty", "deciles": []}
    return {
        "status": "success",
        "experiment_id": run["experiment_id"],
        "deciles": run.get("decile_results", [])
    }

@router.get("/signal-discovery/regimes")
def get_signal_discovery_regimes_api(experiment_id: Optional[str] = None):
    """Retrieves market regime robustness results for the latest or specified experiment."""
    from app.analytics.qlib_discovery.discovery_ledger import get_latest_discovery_run
    run = get_latest_discovery_run()
    if not run:
        return {"status": "empty", "regimes": []}
    return {
        "status": "success",
        "experiment_id": run["experiment_id"],
        "regimes": run.get("regime_results", [])
    }

# =====================================================================
# QLIB SIGNAL DISCOVERY V2 ENDPOINTS (REGIME + CROSS-SECTIONAL VALIDATION)
# =====================================================================

class SignalDiscoveryV2RunRequest(BaseModel):
    universe: str = "LIVE_52"
    horizons: List[int] = [5, 10]
    models: List[str] = ["double_ensemble", "lightgbm", "catboost"]
    top_k_modes: List[str] = ["TOP_5", "TOP_10", "TOP_20_PCT", "TOP_30_PCT"]
    friction_tiers: List[float] = [0.10, 0.15, 0.20, 0.30]
    max_tickers: Optional[int] = None

@router.post("/signal-discovery-v2/run")
def run_signal_discovery_v2_api(req: Optional[SignalDiscoveryV2RunRequest] = None):
    """
    Executes the QLib Signal Discovery V2 Engine:
    - Long-only portfolio simulation across Top-5, Top-10, Top-20%, Top-30%
    - 4 transaction cost friction tiers (10, 15, 20, 30 bps)
    - Macro market regimes (Bull, Bear, Sideways, High Vol)
    - 5-window rolling walk-forward stability
    - Single-pass Locked OOS evaluation on frozen candidate
    """
    from app.analytics.qlib_discovery.signal_discovery_v2_engine import QlibSignalDiscoveryV2Engine
    req_obj = req or SignalDiscoveryV2RunRequest()
    try:
        results = QlibSignalDiscoveryV2Engine.run_discovery_v2(
            universe=req_obj.universe,
            horizons=req_obj.horizons,
            models=req_obj.models,
            top_k_modes=req_obj.top_k_modes,
            friction_tiers=req_obj.friction_tiers,
            max_tickers=req_obj.max_tickers
        )
        return {
            "status": "success",
            "experiment_id": results["experiment_id"],
            "verdict": results["final_verdict"],
            "discovery_v2_results": results
        }
    except Exception as e:
        logger.error(f"[SignalDiscoveryV2] Run failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/signal-discovery-v2/latest")
def get_latest_signal_discovery_v2_api():
    """Retrieves the latest completed QLib Signal Discovery V2 experiment."""
    from app.analytics.qlib_discovery.discovery_v2_ledger import get_latest_v2_discovery_run
    run = get_latest_v2_discovery_run()
    if not run:
        return {"status": "empty", "message": "No V2 discovery runs completed yet."}
    return {
        "status": "success",
        "discovery_run": run
    }

@router.get("/signal-discovery-v2/experiments")
def get_all_signal_discovery_v2_experiments_api():
    """Lists past V2 signal discovery experiments."""
    from app.analytics.qlib_discovery.discovery_v2_ledger import list_all_v2_experiments
    exps = list_all_v2_experiments()
    return {
        "status": "success",
        "count": len(exps),
        "experiments": exps
    }

@router.get("/signal-discovery-v2/portfolios")
def get_signal_discovery_v2_portfolios_api(experiment_id: Optional[str] = None):
    """Retrieves long-only portfolio simulations across Top-K and cost tiers."""
    from app.analytics.qlib_discovery.discovery_v2_ledger import get_latest_v2_discovery_run
    run = get_latest_v2_discovery_run()
    if not run:
        return {"status": "empty", "portfolios": []}
    return {
        "status": "success",
        "experiment_id": run["experiment_id"],
        "portfolios": run.get("portfolios", [])
    }

@router.get("/signal-discovery-v2/regimes")
def get_signal_discovery_v2_regimes_api(experiment_id: Optional[str] = None):
    """Retrieves regime breakdown results for V2."""
    from app.analytics.qlib_discovery.discovery_v2_ledger import get_latest_v2_discovery_run
    run = get_latest_v2_discovery_run()
    if not run:
        return {"status": "empty", "regimes": []}
    return {
        "status": "success",
        "experiment_id": run["experiment_id"],
        "regimes": run.get("regimes", [])
    }

@router.get("/signal-discovery-v2/walk-forward")
def get_signal_discovery_v2_walk_forward_api(experiment_id: Optional[str] = None):
    """Retrieves rolling walk-forward window results for V2."""
    from app.analytics.qlib_discovery.discovery_v2_ledger import get_latest_v2_discovery_run
    run = get_latest_v2_discovery_run()
    if not run:
        return {"status": "empty", "walk_forward": []}
    return {
        "status": "success",
        "experiment_id": run["experiment_id"],
        "walk_forward": run.get("walk_forward", [])
    }

@router.get("/signal-discovery-v2/oos")
def get_signal_discovery_v2_oos_api(experiment_id: Optional[str] = None):
    """Retrieves single-pass locked OOS evaluation for V2."""
    from app.analytics.qlib_discovery.discovery_v2_ledger import get_latest_v2_discovery_run
    run = get_latest_v2_discovery_run()
    if not run:
        return {"status": "empty", "oos_result": None}
    return {
        "status": "success",
        "experiment_id": run["experiment_id"],
        "oos_result": run.get("oos_result")
    }

# =========================================================================
# QLIB SIGNAL DISCOVERY V3 (ECONOMIC EFFICIENCY & HYSTERESIS) ENDPOINTS
# =========================================================================

class QlibV3RunRequest(BaseModel):
    universe: str = "LIVE_52"
    horizons: List[int] = [5, 10, 15, 20]
    friction_tiers: List[float] = [0.10, 0.15, 0.20, 0.30]

@router.get("/signal-discovery-v3/latest")
def get_latest_signal_discovery_v3_api():
    """Retrieves latest completed V3 discovery experiment."""
    from app.analytics.qlib_discovery.discovery_v3_ledger import get_latest_v3_discovery_run
    run = get_latest_v3_discovery_run()
    if not run:
        return {"status": "empty", "message": "No V3 discovery runs completed yet."}
    return {
        "status": "success",
        "discovery_run": run
    }

@router.post("/signal-discovery-v3/run")
def run_signal_discovery_v3_api(req: Optional[QlibV3RunRequest] = None):
    """Executes V3 quantitative research on economic efficiency and turnover reduction."""
    from app.analytics.qlib_discovery.signal_discovery_v3_engine import QlibSignalDiscoveryV3Engine
    u = req.universe if req else "LIVE_52"
    h = req.horizons if req else [5, 10, 15, 20]
    res = QlibSignalDiscoveryV3Engine.run_v3_discovery_experiment(
        universe_name=u,
        horizons=h
    )
    return res

@router.get("/signal-discovery-v3/frontier")
def get_signal_discovery_v3_frontier_api():
    """Retrieves turnover vs return/Sharpe frontier."""
    from app.analytics.qlib_discovery.discovery_v3_ledger import get_latest_v3_discovery_run
    run = get_latest_v3_discovery_run()
    if not run or "metrics" not in run:
        return {"status": "empty", "frontier": []}
    return {
        "status": "success",
        "experiment_id": run["experiment_id"],
        "frontier": run["metrics"].get("turnover_frontier", [])
    }

@router.get("/signal-discovery-v3/hysteresis")
def get_signal_discovery_v3_hysteresis_api():
    """Retrieves hysteresis buffer configurations."""
    from app.analytics.qlib_discovery.discovery_v3_ledger import get_latest_v3_discovery_run
    run = get_latest_v3_discovery_run()
    if not run or "metrics" not in run:
        return {"status": "empty", "hysteresis": []}
    return {
        "status": "success",
        "experiment_id": run["experiment_id"],
        "hysteresis": run["metrics"].get("hysteresis_results", [])
    }

@router.get("/signal-discovery-v3/rebalance")
def get_signal_discovery_v3_rebalance_api():
    """Retrieves rebalance horizon grid results."""
    from app.analytics.qlib_discovery.discovery_v3_ledger import get_latest_v3_discovery_run
    run = get_latest_v3_discovery_run()
    if not run or "metrics" not in run:
        return {"status": "empty", "rebalance": []}
    return {
        "status": "success",
        "experiment_id": run["experiment_id"],
        "rebalance": run["metrics"].get("rebalance_comparisons", [])
    }

@router.get("/signal-discovery-v3/regimes")
def get_signal_discovery_v3_regimes_api():
    """Retrieves regime breakdown results for V3."""
    from app.analytics.qlib_discovery.discovery_v3_ledger import get_latest_v3_discovery_run
    run = get_latest_v3_discovery_run()
    if not run or "metrics" not in run:
        return {"status": "empty", "regimes": []}
    return {
        "status": "success",
        "experiment_id": run["experiment_id"],
        "regimes": run["metrics"].get("regimes", [])
    }

@router.get("/signal-discovery-v3/walk-forward")
def get_signal_discovery_v3_walk_forward_api():
    """Retrieves rolling walk-forward results for V3."""
    from app.analytics.qlib_discovery.discovery_v3_ledger import get_latest_v3_discovery_run
    run = get_latest_v3_discovery_run()
    if not run or "metrics" not in run:
        return {"status": "empty", "walk_forward": []}
    return {
        "status": "success",
        "experiment_id": run["experiment_id"],
        "walk_forward": run["metrics"].get("walk_forward", [])
    }

@router.get("/signal-discovery-v3/oos")
def get_signal_discovery_v3_oos_api():
    """Retrieves single-pass retrospective locked OOS evaluation for V3."""
    from app.analytics.qlib_discovery.discovery_v3_ledger import get_latest_v3_discovery_run
    run = get_latest_v3_discovery_run()
    if not run or "metrics" not in run:
        return {"status": "empty", "oos_result": None}
    return {
        "status": "success",
        "experiment_id": run["experiment_id"],
        "oos_result": run["metrics"].get("oos_result")
    }




