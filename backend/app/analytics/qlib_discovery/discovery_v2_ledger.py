"""
QLIB SIGNAL DISCOVERY V2 EXPERIMENT LEDGER
==========================================
Isolated research persistence for QLib Signal Discovery V2 (Regime + Cross-Sectional Validation).
Guarantees:
- Zero writes to ml_trade_history or live trading operational tables.
- Cryptographic SHA-256 fingerprinting.
- Dedicated schema for long-only portfolios, multi-tier cost stress, walk-forward windows,
  regime robustness, and frozen locked OOS evaluation.
"""

import json
import sqlite3
import hashlib
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)

PROVENANCE_LABEL = "QLIB-INSPIRED / QLIB-COMPATIBLE FEATURE IMPLEMENTATION"
ENGINE_VERSION = "v2.0-regime-validation"

def init_v2_ledger_schema():
    """Initializes dedicated research tables in SQLite for V2."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")

        # 1. Master research V2 experiments
        cur.execute("""
            CREATE TABLE IF NOT EXISTS research_v2_experiments (
                experiment_id TEXT PRIMARY KEY,
                parent_v1_id TEXT,
                timestamp TEXT,
                universe TEXT,
                universe_mode TEXT DEFAULT 'CURRENT_CONSTITUENTS_RETROSPECTIVE',
                timeframe TEXT DEFAULT '1d',
                primary_target TEXT DEFAULT 'rank_5d',
                feature_family TEXT DEFAULT 'Alpha158',
                feature_subset TEXT DEFAULT 'ALL_158',
                model_family TEXT,
                seed INTEGER DEFAULT 42,
                top_k_selected TEXT,
                horizon_days INTEGER DEFAULT 5,
                config_hash TEXT,
                dataset_hash TEXT,
                fingerprint TEXT UNIQUE,
                status TEXT,
                verdict TEXT,
                metrics_json TEXT
            )
        """)

        # 2. Long-only portfolio simulations
        cur.execute("""
            CREATE TABLE IF NOT EXISTS research_v2_portfolios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT,
                top_k_mode TEXT,
                friction_pct REAL,
                horizon_days INTEGER,
                total_return_pct REAL,
                cagr_pct REAL,
                sharpe_ratio REAL,
                sortino_ratio REAL,
                max_drawdown_pct REAL,
                calmar_ratio REAL,
                win_rate_pct REAL,
                profit_factor REAL,
                expectancy_pct REAL,
                turnover_one_way_pct REAL,
                annualized_turnover_pct REAL,
                FOREIGN KEY(experiment_id) REFERENCES research_v2_experiments(experiment_id)
            )
        """)

        # 3. Regime breakdowns for ranking signal and Top-K portfolio
        cur.execute("""
            CREATE TABLE IF NOT EXISTS research_v2_regimes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT,
                regime_name TEXT,
                sample_count INTEGER,
                rank_ic REAL,
                top_k_return_pct REAL,
                spread_pct REAL,
                win_rate REAL,
                FOREIGN KEY(experiment_id) REFERENCES research_v2_experiments(experiment_id)
            )
        """)

        # 4. Walk-forward stability windows
        cur.execute("""
            CREATE TABLE IF NOT EXISTS research_v2_walk_forward (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT,
                window_index INTEGER,
                start_date TEXT,
                end_date TEXT,
                rank_ic REAL,
                spread_pct REAL,
                monotonicity REAL,
                stability_class TEXT,
                FOREIGN KEY(experiment_id) REFERENCES research_v2_experiments(experiment_id)
            )
        """)

        # 5. Single-pass Locked OOS Evaluation
        cur.execute("""
            CREATE TABLE IF NOT EXISTS research_v2_oos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT,
                candidate_name TEXT,
                oos_dates TEXT,
                rank_ic REAL,
                monotonicity REAL,
                spread_pct REAL,
                cagr_pct REAL,
                sharpe REAL,
                max_drawdown_pct REAL,
                turnover_pct REAL,
                verdict TEXT,
                report_json TEXT,
                FOREIGN KEY(experiment_id) REFERENCES research_v2_experiments(experiment_id)
            )
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_res_v2_exp_ts ON research_v2_experiments(timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_res_v2_port_exp ON research_v2_portfolios(experiment_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_res_v2_reg_exp ON research_v2_regimes(experiment_id);")

        conn.commit()
    finally:
        conn.close()

def compute_v2_fingerprint(
    universe: str,
    sorted_tickers: List[str],
    horizon_days: int,
    target_name: str,
    feature_subset: str,
    model_family: str,
    top_k_mode: str,
    friction_pct: float,
    seed: int,
    dataset_hash: str,
    engine_version: str = "v2.0-regime-validation"
) -> str:
    """
    Computes a deterministic SHA-256 fingerprint for V2 research configuration.
    """
    payload = {
        "universe": universe,
        "sorted_tickers": sorted(sorted_tickers),
        "horizon_days": horizon_days,
        "target_name": target_name,
        "feature_subset": feature_subset,
        "model_family": model_family,
        "top_k_mode": top_k_mode,
        "friction_pct": friction_pct,
        "seed": seed,
        "dataset_hash": dataset_hash,
        "engine_version": engine_version
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def save_v2_experiment_record(record: Dict[str, Any]) -> str:
    """Inserts or updates a V2 master research experiment."""
    init_v2_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO research_v2_experiments (
                experiment_id, parent_v1_id, timestamp, universe, universe_mode,
                timeframe, primary_target, feature_family, feature_subset,
                model_family, seed, top_k_selected, horizon_days, config_hash,
                dataset_hash, fingerprint, status, verdict, metrics_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["experiment_id"],
            record.get("parent_v1_id", "res_exp_20260905_125748_3344c7a6"),
            record.get("timestamp", datetime.now().isoformat()),
            record.get("universe", "LIVE_52"),
            record.get("universe_mode", "CURRENT_CONSTITUENTS_RETROSPECTIVE"),
            record.get("timeframe", "1d"),
            record.get("primary_target", "rank_5d"),
            record.get("feature_family", "Alpha158"),
            record.get("feature_subset", "ALL_158"),
            record.get("model_family", "double_ensemble"),
            record.get("seed", 42),
            record.get("top_k_selected", "TOP_10"),
            record.get("horizon_days", 5),
            record.get("config_hash", ""),
            record.get("dataset_hash", ""),
            record.get("fingerprint", ""),
            record.get("status", "COMPLETED"),
            record.get("verdict", "WEAK SIGNAL"),
            json.dumps(record.get("metrics_json", {}))
        ))
        conn.commit()
        return record["experiment_id"]
    finally:
        conn.close()

def save_v2_portfolio_results(experiment_id: str, portfolios: List[Dict[str, Any]]):
    """Persists long-only portfolio simulation outcomes."""
    init_v2_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        for p in portfolios:
            cur.execute("""
                INSERT INTO research_v2_portfolios (
                    experiment_id, top_k_mode, friction_pct, horizon_days,
                    total_return_pct, cagr_pct, sharpe_ratio, sortino_ratio,
                    max_drawdown_pct, calmar_ratio, win_rate_pct, profit_factor,
                    expectancy_pct, turnover_one_way_pct, annualized_turnover_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                experiment_id,
                p.get("top_k_mode", "TOP_10"),
                p.get("friction_pct", 0.15),
                p.get("horizon_days", 5),
                p.get("total_return_pct", 0.0),
                p.get("cagr_pct", 0.0),
                p.get("sharpe_ratio", 0.0),
                p.get("sortino_ratio", 0.0),
                p.get("max_drawdown_pct", 0.0),
                p.get("calmar_ratio", 0.0),
                p.get("win_rate_pct", 0.0),
                p.get("profit_factor", 0.0),
                p.get("expectancy_pct", 0.0),
                p.get("turnover_one_way_per_rebalance_pct", 0.0),
                p.get("annualized_turnover_pct", 0.0)
            ))
        conn.commit()
    finally:
        conn.close()

def save_v2_regime_results(experiment_id: str, regimes: List[Dict[str, Any]]):
    """Persists regime analysis for V2."""
    init_v2_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        for r in regimes:
            cur.execute("""
                INSERT INTO research_v2_regimes (
                    experiment_id, regime_name, sample_count, rank_ic, top_k_return_pct, spread_pct, win_rate
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                experiment_id,
                r.get("regime_name", "UNKNOWN"),
                r.get("sample_count", 0),
                r.get("rank_ic", 0.0),
                r.get("top_k_return_pct", 0.0),
                r.get("spread_pct", 0.0),
                r.get("win_rate", 0.0)
            ))
        conn.commit()
    finally:
        conn.close()

def save_v2_walk_forward(experiment_id: str, windows: List[Dict[str, Any]]):
    """Persists rolling walk-forward window outcomes."""
    init_v2_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        for w in windows:
            cur.execute("""
                INSERT INTO research_v2_walk_forward (
                    experiment_id, window_index, start_date, end_date, rank_ic, spread_pct, monotonicity, stability_class
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                experiment_id,
                w.get("window_index", 0),
                w.get("start_date", ""),
                w.get("end_date", ""),
                w.get("rank_ic", 0.0),
                w.get("spread_pct", 0.0),
                w.get("monotonicity", 0.0),
                w.get("stability_class", "REGIME-DEPENDENT")
            ))
        conn.commit()
    finally:
        conn.close()

def save_v2_oos_result(experiment_id: str, oos_data: Dict[str, Any]):
    """Persists single-pass locked OOS holdout result."""
    init_v2_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO research_v2_oos (
                experiment_id, candidate_name, oos_dates, rank_ic, monotonicity,
                spread_pct, cagr_pct, sharpe, max_drawdown_pct, turnover_pct,
                verdict, report_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            experiment_id,
            oos_data.get("candidate_name", "DoubleEnsemble (Top-10)"),
            oos_data.get("oos_dates", "2023-09-04 to 2026-09-04"),
            oos_data.get("rank_ic", 0.0),
            oos_data.get("monotonicity", 0.0),
            oos_data.get("spread_pct", 0.0),
            oos_data.get("cagr_pct", 0.0),
            oos_data.get("sharpe", 0.0),
            oos_data.get("max_drawdown_pct", 0.0),
            oos_data.get("turnover_pct", 0.0),
            oos_data.get("verdict", "WEAK SIGNAL"),
            json.dumps(oos_data)
        ))
        conn.commit()
    finally:
        conn.close()

def get_latest_v2_discovery_run() -> Optional[Dict[str, Any]]:
    """Retrieves the latest completed V2 discovery run."""
    init_v2_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT experiment_id, parent_v1_id, timestamp, universe, universe_mode,
                   timeframe, primary_target, feature_family, feature_subset,
                   model_family, seed, top_k_selected, horizon_days, config_hash,
                   dataset_hash, fingerprint, status, verdict, metrics_json
            FROM research_v2_experiments
            ORDER BY timestamp DESC LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            return None

        exp_id = row[0]
        cur.execute("SELECT top_k_mode, friction_pct, horizon_days, total_return_pct, cagr_pct, sharpe_ratio, sortino_ratio, max_drawdown_pct, calmar_ratio, win_rate_pct, profit_factor, expectancy_pct, turnover_one_way_pct, annualized_turnover_pct FROM research_v2_portfolios WHERE experiment_id = ?", (exp_id,))
        portfolios = []
        for p in cur.fetchall():
            portfolios.append({
                "top_k_mode": p[0],
                "friction_pct": p[1],
                "horizon_days": p[2],
                "total_return_pct": p[3],
                "cagr_pct": p[4],
                "sharpe_ratio": p[5],
                "sortino_ratio": p[6],
                "max_drawdown_pct": p[7],
                "calmar_ratio": p[8],
                "win_rate_pct": p[9],
                "profit_factor": p[10],
                "expectancy_pct": p[11],
                "turnover_one_way_per_rebalance_pct": p[12],
                "annualized_turnover_pct": p[13]
            })

        cur.execute("SELECT regime_name, sample_count, rank_ic, top_k_return_pct, spread_pct, win_rate FROM research_v2_regimes WHERE experiment_id = ?", (exp_id,))
        regimes = []
        for r in cur.fetchall():
            regimes.append({
                "regime_name": r[0],
                "sample_count": r[1],
                "rank_ic": r[2],
                "top_k_return_pct": r[3],
                "spread_pct": r[4],
                "win_rate": r[5]
            })

        cur.execute("SELECT window_index, start_date, end_date, rank_ic, spread_pct, monotonicity, stability_class FROM research_v2_walk_forward WHERE experiment_id = ? ORDER BY window_index ASC", (exp_id,))
        walk_windows = []
        for w in cur.fetchall():
            walk_windows.append({
                "window_index": w[0],
                "start_date": w[1],
                "end_date": w[2],
                "rank_ic": w[3],
                "spread_pct": w[4],
                "monotonicity": w[5],
                "stability_class": w[6]
            })

        cur.execute("SELECT candidate_name, oos_dates, rank_ic, monotonicity, spread_pct, cagr_pct, sharpe, max_drawdown_pct, turnover_pct, verdict, report_json FROM research_v2_oos WHERE experiment_id = ?", (exp_id,))
        oos_row = cur.fetchone()
        oos_data = None
        if oos_row:
            oos_data = {
                "candidate_name": oos_row[0],
                "oos_dates": oos_row[1],
                "rank_ic": oos_row[2],
                "monotonicity": oos_row[3],
                "spread_pct": oos_row[4],
                "cagr_pct": oos_row[5],
                "sharpe": oos_row[6],
                "max_drawdown_pct": oos_row[7],
                "turnover_pct": oos_row[8],
                "verdict": oos_row[9],
                "details": json.loads(oos_row[10]) if oos_row[10] else {}
            }

        return {
            "experiment_id": row[0],
            "parent_v1_id": row[1],
            "timestamp": row[2],
            "universe": row[3],
            "universe_mode": row[4],
            "timeframe": row[5],
            "primary_target": row[6],
            "feature_family": row[7],
            "feature_subset": row[8],
            "model_family": row[9],
            "seed": row[10],
            "top_k_selected": row[11],
            "horizon_days": row[12],
            "config_hash": row[13],
            "dataset_hash": row[14],
            "fingerprint": row[15],
            "status": row[16],
            "verdict": row[17],
            "metrics": json.loads(row[18]) if row[18] else {},
            "portfolios": portfolios,
            "regimes": regimes,
            "walk_forward": walk_windows,
            "oos_result": oos_data
        }
    finally:
        conn.close()

def list_all_v2_experiments() -> List[Dict[str, Any]]:
    """Lists all completed V2 discovery experiments."""
    init_v2_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT experiment_id, parent_v1_id, timestamp, universe,
                   primary_target, feature_subset, model_family,
                   top_k_selected, horizon_days, verdict
            FROM research_v2_experiments
            ORDER BY timestamp DESC
        """)
        rows = cur.fetchall()
        return [
            {
                "experiment_id": r[0],
                "parent_v1_id": r[1],
                "timestamp": r[2],
                "universe": r[3],
                "primary_target": r[4],
                "feature_subset": r[5],
                "model_family": r[6],
                "top_k_selected": r[7],
                "horizon_days": r[8],
                "verdict": r[9]
            }
            for r in rows
        ]
    finally:
        conn.close()

