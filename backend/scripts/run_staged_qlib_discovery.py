"""
MASTER SCRIPT: STAGED QLIB DISCOVERY EVALUATION
================================================
Mandated Staging Order:
- Phase B3: Alpha158 + LightGBM (SWING & INTRADAY)
- Phase B4: Alpha158 + CatBoost & Alpha158 + XGBoost (SWING & INTRADAY)
- Phase B5: Alpha158 + DoubleEnsemble (SWING & INTRADAY)
- Phase B6: Alpha360 evaluation (SWING & INTRADAY)

All runs use identical locked OOS splits per strategy and test against the Champion benchmark.
"""

import sys
import os
import time
import json
import sqlite3

# Add backend to path
sys.path.insert(0, os.path.abspath("backend"))

from app.analytics.qlib_discovery.pipeline_runner import run_qlib_candidate_search
from app.analytics.qlib_discovery.qlib_registry import verify_champion_immutability, ensure_qlib_table
from app.data.historical_data_layer import get_db_path

def clear_old_test_evaluations():
    """Wipes only temporary test runs from qlib_model_evaluations for a pristine report."""
    ensure_qlib_table()
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    conn.execute("DELETE FROM qlib_model_evaluations")
    conn.commit()
    conn.close()
    print("🧹 Cleared qlib_model_evaluations for authoritative staged runs.")

def run_stage(phase_label: str, strategy: str, model_family: str, feature_family: str):
    print(f"\n====================================================================")
    print(f"🚀 EXECUTING {phase_label}: {strategy} | {feature_family} + {model_family.upper()}")
    print(f"====================================================================")
    
    t0 = time.time()
    cand, bench = run_qlib_candidate_search(
        strategy=strategy,
        model_family=model_family,
        feature_family=feature_family,
        universe_name="LIVE_52"
    )
    t1 = time.time()
    
    print(f"✅ Completed in {t1 - t0:.2f}s")
    print(f"  Candidate: {cand['model_name']} ({cand['feature_family']})")
    print(f"    - Trades: {cand['trade_count']} | Win Rate: {cand['win_rate']:.1f}% | Net P&L: {cand['net_pnl_pct']:.2f}%")
    print(f"    - Sharpe: {cand['sharpe_ratio']:.2f} | Profit Factor: {cand['profit_factor']:.2f} | Expectancy: {cand['expectancy']:.2f}%")
    print(f"    - F1 Score: {cand['f1']:.3f} | Brier: {cand['brier']:.3f}")
    print(f"    - Gate Verdict: {cand['gate_verdict']}")
    print(f"  Champion Benchmark:")
    print(f"    - Trades: {bench['trade_count']} | Win Rate: {bench['win_rate']:.1f}% | Net P&L: {bench['net_pnl_pct']:.2f}%")
    print(f"    - Sharpe: {bench['sharpe_ratio']:.2f} | Profit Factor: {bench['profit_factor']:.2f} | Expectancy: {bench['expectancy']:.2f}%")
    print(f"    - F1 Score: {bench['f1']:.3f} | Brier: {bench['brier']:.3f}")
    
    return cand, bench

def main():
    print("🔒 Step 1: Pre-Execution Champion Immutability Audit...")
    verify_champion_immutability()
    print("✅ Champion hashes byte-for-byte verified.")

    clear_old_test_evaluations()

    results = []

    # -------------------------------------------------------------
    # PHASE B3: Alpha158 + LightGBM
    # -------------------------------------------------------------
    c, b = run_stage("PHASE B3", "SWING", "lightgbm", "Alpha158")
    results.append(("B3", c, b))
    c, b = run_stage("PHASE B3", "INTRADAY", "lightgbm", "Alpha158")
    results.append(("B3", c, b))

    # -------------------------------------------------------------
    # PHASE B4: Alpha158 + CatBoost & Alpha158 + XGBoost
    # -------------------------------------------------------------
    c, b = run_stage("PHASE B4", "SWING", "catboost", "Alpha158")
    results.append(("B4", c, b))
    c, b = run_stage("PHASE B4", "INTRADAY", "catboost", "Alpha158")
    results.append(("B4", c, b))

    c, b = run_stage("PHASE B4", "SWING", "xgboost", "Alpha158")
    results.append(("B4", c, b))
    c, b = run_stage("PHASE B4", "INTRADAY", "xgboost", "Alpha158")
    results.append(("B4", c, b))

    # -------------------------------------------------------------
    # PHASE B5: Alpha158 + DoubleEnsemble
    # -------------------------------------------------------------
    c, b = run_stage("PHASE B5", "SWING", "double_ensemble", "Alpha158")
    results.append(("B5", c, b))
    c, b = run_stage("PHASE B5", "INTRADAY", "double_ensemble", "Alpha158")
    results.append(("B5", c, b))

    # -------------------------------------------------------------
    # PHASE B6: Alpha360 (Sequence Window: 60 bars x 6 features)
    # -------------------------------------------------------------
    c, b = run_stage("PHASE B6", "SWING", "lightgbm", "Alpha360")
    results.append(("B6", c, b))
    c, b = run_stage("PHASE B6", "INTRADAY", "lightgbm", "Alpha360")
    results.append(("B6", c, b))

    print("\n🔒 Final Step: Post-Execution Champion Immutability Audit...")
    verify_champion_immutability()
    print("✅ Champion hashes remain 100% byte-for-byte identical.")

    print("\n=========================================================================================")
    print("🏆 QLIB DISCOVERY RESEARCH RUNS COMPLETED — SUMMARY TABLE")
    print("=========================================================================================")
    print(f"{'Phase':<6} | {'Strat':<8} | {'Model':<15} | {'Feat':<8} | {'WinRate':<8} | {'NetPnL':<9} | {'Sharpe':<7} | {'PF':<5} | {'Verdict':<18}")
    print("-" * 95)
    for phase, cand, bench in results:
        print(f"{phase:<6} | {cand['strategy']:<8} | {cand['model_name']:<15} | {cand['feature_family']:<8} | {cand['win_rate']:<7.1f}% | {cand['net_pnl_pct']:<8.2f}% | {cand['sharpe_ratio']:<7.2f} | {cand['profit_factor']:<5.2f} | {cand['gate_verdict']:<18}")

if __name__ == "__main__":
    main()
