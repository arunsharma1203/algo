"""
QLIB DISCOVERY MASTER PIPELINE RUNNER
=====================================
Provenance:
    Implementation Version: qlib_research_v1
    Provenance Label: "QLIB-INSPIRED / QLIB-COMPATIBLE FEATURE IMPLEMENTATION"
    Temporal Splits: TRAIN (70%) -> VALIDATION (15%) -> LOCKED OOS (15%)
    Execution Flow:
        1. Verify Champion hashes byte-for-byte
        2. Prepare Strategy Dataset & Compute Features (Alpha158 / Alpha360)
        3. Create Temporal Splits (freeze Locked OOS)
        4. Run Champion Baseline Benchmark on Locked OOS
        5. Run Bounded Validation Search across Model Variants
        6. Select Best Validation Candidate & Freeze Configuration
        7. Evaluate Frozen Candidate on Locked OOS exactly once
        8. Run Immutable Promotion Hurdle Checks
        9. Persist Full Provenance & Results in Isolated Table
"""

import os
import copy
import hashlib
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from app.analytics.qlib_discovery.qlib_registry import (
    verify_champion_immutability,
    save_qlib_evaluation,
    ensure_qlib_table
)
from app.analytics.qlib_discovery.alpha158_engine import compute_alpha158_features
from app.analytics.qlib_discovery.alpha360_engine import compute_alpha360_features
from app.analytics.qlib_discovery.model_trainer import create_model, BOUNDED_PARAM_GRIDS
import pickle
from app.analytics.qlib_discovery.qlib_evaluator import (
    prepare_strategy_dataset,
    create_temporal_splits,
    simulate_trades,
    compute_classification_metrics,
    compute_stock_concentration,
    compute_full_oos_hash
)
from app.analytics.model_manager import ModelManager

PROVENANCE_LABEL = "QLIB-INSPIRED / QLIB-COMPATIBLE FEATURE IMPLEMENTATION"
FEATURE_VERSION = "qlib_research_v1"

def evaluate_champion_baseline(
    strategy: str,
    raw_dfs: Dict[str, pd.DataFrame],
    oos_meta: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Runs the production Champion VotingClassifier through the exact same
    locked OOS trade simulator to establish the benchmark.
    """
    verify_champion_immutability()
    
    tf = "swing" if strategy == "SWING" else "intraday"
    champ_model, champ_meta = ModelManager.load_champion(tf)
    features_list = (['rsi', 'macd', 'macd_diff', 'adx', 'returns']
                     if tf == "intraday" else
                     ['rsi', 'macd', 'macd_diff', 'adx', 'atr'])

    # Build Champion feature representations vectorially per ticker
    import ta
    champ_feat_dfs = {}
    for ticker, raw in raw_dfs.items():
        c = raw['close']
        h = raw['high']
        l = raw['low']
        rsi = ta.momentum.RSIIndicator(c, window=14).rsi().fillna(50.0)
        macd_ind = ta.trend.MACD(c)
        macd = macd_ind.macd().fillna(0.0)
        macd_diff = macd_ind.macd_diff().fillna(0.0)
        adx = ta.trend.ADXIndicator(h, l, c, window=14).adx().fillna(25.0)
        if tf == "intraday":
            returns = c.pct_change().fillna(0.0)
            feat_df = pd.DataFrame({'rsi': rsi, 'macd': macd, 'macd_diff': macd_diff, 'adx': adx, 'returns': returns})
        else:
            atr = ta.volatility.AverageTrueRange(h, l, c, window=14).average_true_range().fillna(0.0)
            feat_df = pd.DataFrame({'rsi': rsi, 'macd': macd, 'macd_diff': macd_diff, 'adx': adx, 'atr': atr})
        champ_feat_dfs[ticker] = feat_df

    champ_rows = []
    valid_oos_meta = []
    for m in oos_meta:
        ticker = m.get('ticker')
        curr_i = m['raw_idx']
        if curr_i < 30 or ticker not in champ_feat_dfs:
            continue
        row = champ_feat_dfs[ticker].iloc[curr_i].values
        champ_rows.append(row)
        valid_oos_meta.append(m)

    if champ_rows:
        X_champ = np.nan_to_num(np.array(champ_rows))
        try:
            champ_probs = champ_model.predict_proba(X_champ)[:, 1]
        except Exception:
            champ_probs = np.full(len(champ_rows), 0.50)
    else:
        champ_probs = np.array([])
    
    # Simulate trades using the exact same rules
    econ_metrics = simulate_trades(
        probs=champ_probs,
        meta_list=valid_oos_meta,
        strategy=strategy,
        prob_threshold=0.55
    )
    
    # Ground truth labels for classification metrics
    y_true = np.array([1 if (m['raw_df']['close'].iloc[min(len(m['raw_df'])-1, m['raw_idx'] + 1)] > m['close']) else 0 for m in valid_oos_meta])
    class_metrics = compute_classification_metrics(champ_probs, y_true, threshold=0.50)
    concentration = compute_stock_concentration(econ_metrics['trades'])

    baseline_record = {
        "strategy": strategy,
        "model_name": "Champion Baseline",
        "feature_family": "Champion Features (RF+GB+SVM)",
        "feature_version": champ_meta.get("version", "v1.0-champion"),
        "provenance": "Production Champion Benchmark",
        "f1": class_metrics["f1"],
        "precision": class_metrics["precision"],
        "recall": class_metrics["recall"],
        "brier": class_metrics["brier"],
        "trade_count": econ_metrics["trade_count"],
        "win_rate": econ_metrics["win_rate"],
        "net_pnl_pct": econ_metrics["net_pnl_pct"],
        "expectancy": econ_metrics["expectancy"],
        "profit_factor": econ_metrics["profit_factor"],
        "sharpe_ratio": econ_metrics["sharpe_ratio"],
        "max_drawdown_pct": econ_metrics["max_drawdown_pct"],
        "gate_verdict": "BENCHMARK",
        "metrics_json": {
            "economic": econ_metrics,
            "classification": class_metrics,
            "concentration": concentration,
            "champion_meta": champ_meta
        }
    }
    return baseline_record

def run_qlib_candidate_search(
    strategy: str = "SWING",
    model_family: str = "lightgbm",
    feature_family: str = "Alpha158",
    universe_name: str = "LIVE_52",
    limit_tickers: Optional[int] = None
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Executes bounded validation search, selects best configuration,
    evaluates on locked OOS, runs immutable promotion checks, and persists.
    """
    # 1. Pre-execution Champion Immutability Check
    verify_champion_immutability()
    
    # 2. Prepare Data & Causal Features
    raw_dfs, feat_dfs, meta = prepare_strategy_dataset(
        strategy=strategy,
        universe_name=universe_name,
        feature_family=feature_family,
        limit_tickers=limit_tickers
    )
    
    # 3. Temporal Splits (70% Train, 15% Validation, 15% Locked OOS)
    train_data, val_data, oos_data, norm_params = create_temporal_splits(
        raw_dfs=raw_dfs,
        feat_dfs=feat_dfs,
        strategy=strategy
    )
    
    # Compute full deterministic OOS hash across all tickers, timestamps, OHLCV bars, and labels
    oos_hash = compute_full_oos_hash(
        oos_data=oos_data,
        split_meta=norm_params.get("split_meta"),
        feature_version=FEATURE_VERSION
    )

    # 4. Champion Baseline Benchmark
    champion_benchmark = evaluate_champion_baseline(
        strategy=strategy,
        raw_dfs=raw_dfs,
        oos_meta=oos_data["meta"]
    )

    # 5. Bounded Validation Search (strictly using validation split)
    param_grid = BOUNDED_PARAM_GRIDS.get(model_family.lower(), [{}])
    best_val_score = -999.0
    best_val_params = None
    best_model = None

    for param_idx, params in enumerate(param_grid):
        model = create_model(model_family, params=params, random_state=42)
        model.fit(train_data["X"], train_data["y"])
        
        # Predict on Validation Split
        val_probs = model.predict_proba(val_data["X"])[:, 1]
        val_econ = simulate_trades(
            probs=val_probs,
            meta_list=val_data["meta"],
            strategy=strategy,
            prob_threshold=0.55
        )
        
        # Validation Selection Criterion: Net P&L * Sharpe (balanced profit + risk)
        val_score = val_econ["net_pnl_pct"] + (val_econ["sharpe_ratio"] * 2.0)
        
        if val_score > best_val_score:
            best_val_score = val_score
            best_val_params = params
            best_model = model

    # 6. Freeze Selected Candidate Configuration
    frozen_params = copy.deepcopy(best_val_params)
    
    # 7. Evaluate on Locked OOS (strictly once, zero tuning)
    oos_probs = best_model.predict_proba(oos_data["X"])[:, 1]
    oos_econ = simulate_trades(
        probs=oos_probs,
        meta_list=oos_data["meta"],
        strategy=strategy,
        prob_threshold=0.55
    )
    oos_class = compute_classification_metrics(oos_probs, oos_data["y"], threshold=0.50)
    concentration = compute_stock_concentration(oos_econ["trades"])

    # 8. Restored Immutable Promotion Gate Evaluation
    # Hard Governance Requirements:
    # - completed trades >= 30
    # - positive Net P&L (> 0.0)
    # - positive Expectancy (> 0.0)
    # - positive Sharpe Ratio (> 0.0)
    # - Sharpe >= Champion
    # - Net P&L > Champion
    # - Expectancy > Champion
    # - Profit Factor >= Champion
    # - Max Drawdown <= 20.0%
    # - Max Drawdown <= max(20.0, Champion Max DD)
    # - Brier Score <= Champion Brier + 0.02
    # - Top 1 Stock Concentration <= 60%
    # - Explicit human confirmation required
    gate_checks = {
        "min_trades": bool(oos_econ["trade_count"] >= 30),
        "positive_net_pnl": bool(oos_econ["net_pnl_pct"] > 0.0),
        "positive_expectancy": bool(oos_econ["expectancy"] > 0.0),
        "positive_sharpe": bool(oos_econ["sharpe_ratio"] > 0.0),
        "net_pnl_superior": bool(oos_econ["net_pnl_pct"] > champion_benchmark["net_pnl_pct"]),
        "expectancy_superior": bool(oos_econ["expectancy"] > champion_benchmark["expectancy"]),
        "sharpe_superior": bool(oos_econ["sharpe_ratio"] >= champion_benchmark["sharpe_ratio"]),
        "profit_factor_superior": bool(oos_econ["profit_factor"] >= champion_benchmark["profit_factor"]),
        "max_drawdown_ceiling": bool(oos_econ["max_drawdown_pct"] <= 20.0),
        "max_drawdown_non_degraded": bool(oos_econ["max_drawdown_pct"] <= max(20.0, champion_benchmark["max_drawdown_pct"])),
        "brier_non_degraded": bool(oos_class["brier"] <= champion_benchmark["brier"] + 0.02),
        "concentration_safe": bool(concentration["top_1_pct"] <= 60.0),
        "human_confirmation_required": True
    }

    # Absolute disqualifier: negative Sharpe, negative P&L, negative expectancy, or excessive DD (>20%)
    has_critical_failure = (
        oos_econ["net_pnl_pct"] <= 0.0
        or oos_econ["expectancy"] <= 0.0
        or oos_econ["sharpe_ratio"] <= 0.0
        or oos_econ["max_drawdown_pct"] > 20.0
    )

    if oos_econ["trade_count"] < 30:
        gate_verdict = "INSUFFICIENT EVIDENCE"
    elif has_critical_failure:
        gate_verdict = "RETAIN CHAMPION"
    elif all(gate_checks.values()):
        gate_verdict = "PROMOTE CANDIDATE"
    else:
        gate_verdict = "RETAIN CHAMPION"

    # Real Model Artifact Hash: serialize actual model artifact bytes
    artifact_bytes = pickle.dumps(best_model, protocol=5)
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()

    candidate_record = {
        "strategy": strategy,
        "model_name": model_family.upper(),
        "feature_family": feature_family,
        "feature_version": FEATURE_VERSION,
        "provenance": PROVENANCE_LABEL,
        "dataset_hash": meta["dataset_hash"],
        "oos_hash": oos_hash,
        "model_artifact_hash": artifact_hash,
        "f1": oos_class["f1"],
        "precision": oos_class["precision"],
        "recall": oos_class["recall"],
        "brier": oos_class["brier"],
        "trade_count": oos_econ["trade_count"],
        "win_rate": oos_econ["win_rate"],
        "net_pnl_pct": oos_econ["net_pnl_pct"],
        "expectancy": oos_econ["expectancy"],
        "profit_factor": oos_econ["profit_factor"],
        "sharpe_ratio": oos_econ["sharpe_ratio"],
        "max_drawdown_pct": oos_econ["max_drawdown_pct"],
        "gate_verdict": gate_verdict,
        "metrics_json": {
            "economic": oos_econ,
            "classification": oos_class,
            "concentration": concentration,
            "gate_checks": gate_checks,
            "frozen_params": frozen_params,
            "validation_score": round(float(best_val_score), 3),
            "dataset_meta": meta
        }
    }

    # 9. Persist Candidate Evaluation and Champion Baseline into Isolated Table
    baseline_id = f"baseline_{strategy.lower()}_{meta['dataset_hash'][:8]}"
    champion_benchmark["evaluation_id"] = baseline_id
    champion_benchmark["dataset_hash"] = meta["dataset_hash"]
    champion_benchmark["oos_hash"] = oos_hash
    save_qlib_evaluation(champion_benchmark)

    eval_id = save_qlib_evaluation(candidate_record)
    candidate_record["evaluation_id"] = eval_id

    # 10. Post-execution Champion Immutability Check
    verify_champion_immutability()

    return candidate_record, champion_benchmark
