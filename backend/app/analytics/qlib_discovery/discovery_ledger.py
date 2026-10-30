"""
QLIB SIGNAL DISCOVERY EXPERIMENT LEDGER
=======================================
Isolated SQLite research persistence for QLib Signal Discovery Engine V1.
Enforces:
- Complete research isolation from ml_trade_history and operational tables.
- Deterministic experiment fingerprinting (SHA-256).
- Structured schema for experiments, feature statistics, model metrics,
  regime breakdowns, decile distributions, and frozen OOS evaluations.
- Historical survivorship bias governance tracking.
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
FEATURE_VERSION = "qlib_research_v1"

def init_discovery_ledger_schema():
    """Initializes dedicated research tables in SQLite with idempotent migrations."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")

        # 1. Master research experiments
        cur.execute("""
            CREATE TABLE IF NOT EXISTS research_experiments (
                experiment_id TEXT PRIMARY KEY,
                timestamp TEXT,
                parent_experiment_id TEXT,
                universe TEXT,
                universe_mode TEXT DEFAULT 'CURRENT_CONSTITUENTS_RETROSPECTIVE',
                timeframe TEXT,
                horizon_days INTEGER,
                target_type TEXT,
                feature_family TEXT,
                model_family TEXT,
                seed INTEGER,
                config_hash TEXT,
                dataset_hash TEXT,
                feature_hash TEXT,
                fingerprint TEXT UNIQUE,
                status TEXT,
                verdict TEXT,
                metrics_json TEXT
            )
        """)

        # 2. Feature-level discovery statistics
        cur.execute("""
            CREATE TABLE IF NOT EXISTS research_feature_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT,
                feature_name TEXT,
                ic_mean REAL,
                ic_std REAL,
                icir REAL,
                rank_ic_mean REAL,
                rank_icir REAL,
                positive_ic_pct REAL,
                stability_class TEXT,
                cluster_id TEXT,
                window_ics_json TEXT,
                FOREIGN KEY(experiment_id) REFERENCES research_experiments(experiment_id)
            )
        """)

        # 3. Model validation & comparative metrics
        cur.execute("""
            CREATE TABLE IF NOT EXISTS research_model_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT,
                model_name TEXT,
                seed INTEGER,
                f1 REAL,
                precision REAL,
                recall REAL,
                brier REAL,
                sharpe REAL,
                win_rate REAL,
                trade_count INTEGER,
                max_dd_pct REAL,
                expectancy_pct REAL,
                profit_factor REAL,
                turnover REAL,
                metrics_json TEXT,
                FOREIGN KEY(experiment_id) REFERENCES research_experiments(experiment_id)
            )
        """)

        # 4. Market regime performance breakdowns
        cur.execute("""
            CREATE TABLE IF NOT EXISTS research_regime_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT,
                regime_name TEXT,
                sample_count INTEGER,
                ic_mean REAL,
                rank_ic_mean REAL,
                spread_pct REAL,
                win_rate REAL,
                FOREIGN KEY(experiment_id) REFERENCES research_experiments(experiment_id)
            )
        """)

        # 5. Cross-sectional decile returns
        cur.execute("""
            CREATE TABLE IF NOT EXISTS research_decile_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT,
                decile INTEGER,
                avg_return_pct REAL,
                median_return_pct REAL,
                hit_rate REAL,
                sharpe REAL,
                FOREIGN KEY(experiment_id) REFERENCES research_experiments(experiment_id)
            )
        """)

        # 6. Locked OOS validation results
        cur.execute("""
            CREATE TABLE IF NOT EXISTS research_oos_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT,
                candidate_name TEXT,
                oos_hash TEXT,
                ic REAL,
                rank_ic REAL,
                sharpe REAL,
                trade_count INTEGER,
                win_rate REAL,
                max_dd_pct REAL,
                verdict TEXT,
                report_json TEXT,
                FOREIGN KEY(experiment_id) REFERENCES research_experiments(experiment_id)
            )
        """)

        # Indexes for fast lookup
        cur.execute("CREATE INDEX IF NOT EXISTS idx_res_exp_timestamp ON research_experiments(timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_res_feat_exp ON research_feature_stats(experiment_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_res_model_exp ON research_model_results(experiment_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_res_regime_exp ON research_regime_results(experiment_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_res_decile_exp ON research_decile_results(experiment_id);")

        conn.commit()
    finally:
        conn.close()

def compute_experiment_fingerprint(
    universe: str,
    sorted_tickers: List[str],
    timeframe: str,
    horizon_days: int,
    target_type: str,
    feature_family: str,
    model_family: str,
    seed: int,
    start_date: str,
    end_date: str,
    dataset_hash: str,
    engine_version: str = "v1.0"
) -> str:
    """
    Computes a deterministic SHA-256 fingerprint for a research configuration.
    Guarantees cache purity and prevents collision between distinct research runs.
    """
    payload = {
        "universe": universe,
        "sorted_tickers": sorted(sorted_tickers),
        "timeframe": timeframe,
        "horizon_days": horizon_days,
        "target_type": target_type,
        "feature_family": feature_family,
        "model_family": model_family,
        "seed": seed,
        "start_date": start_date,
        "end_date": end_date,
        "dataset_hash": dataset_hash,
        "engine_version": engine_version
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def save_experiment_record(record: Dict[str, Any]) -> str:
    """Inserts or updates a master research experiment in the isolated ledger."""
    init_discovery_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO research_experiments (
                experiment_id, timestamp, parent_experiment_id, universe, universe_mode,
                timeframe, horizon_days, target_type, feature_family, model_family,
                seed, config_hash, dataset_hash, feature_hash, fingerprint,
                status, verdict, metrics_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["experiment_id"],
            record.get("timestamp", datetime.now().isoformat()),
            record.get("parent_experiment_id"),
            record.get("universe", "LIVE_52"),
            record.get("universe_mode", "CURRENT_CONSTITUENTS_RETROSPECTIVE"),
            record.get("timeframe", "1d"),
            record.get("horizon_days", 5),
            record.get("target_type", "classification"),
            record.get("feature_family", "Alpha158"),
            record.get("model_family", "lightgbm"),
            record.get("seed", 42),
            record.get("config_hash", ""),
            record.get("dataset_hash", ""),
            record.get("feature_hash", ""),
            record.get("fingerprint", ""),
            record.get("status", "COMPLETED"),
            record.get("verdict", "NO ROBUST SIGNAL"),
            json.dumps(record.get("metrics_json", {}))
        ))
        conn.commit()
        return record["experiment_id"]
    finally:
        conn.close()

def save_feature_stats(experiment_id: str, feature_stats: List[Dict[str, Any]]):
    """Persists feature-level statistical properties into the research ledger."""
    init_discovery_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        for f in feature_stats:
            cur.execute("""
                INSERT INTO research_feature_stats (
                    experiment_id, feature_name, ic_mean, ic_std, icir,
                    rank_ic_mean, rank_icir, positive_ic_pct, stability_class,
                    cluster_id, window_ics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                experiment_id,
                f["feature_name"],
                f.get("ic_mean", 0.0),
                f.get("ic_std", 0.0),
                f.get("icir", 0.0),
                f.get("rank_ic_mean", 0.0),
                f.get("rank_icir", 0.0),
                f.get("positive_ic_pct", 0.0),
                f.get("stability_class", "NO_SIGNAL"),
                f.get("cluster_id", "CLUSTER_NONE"),
                json.dumps(f.get("window_ics", []))
            ))
        conn.commit()
    finally:
        conn.close()

def save_decile_results(experiment_id: str, deciles: List[Dict[str, Any]]):
    """Persists cross-sectional decile evaluation findings."""
    init_discovery_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        for d in deciles:
            cur.execute("""
                INSERT INTO research_decile_results (
                    experiment_id, decile, avg_return_pct, median_return_pct, hit_rate, sharpe
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                experiment_id,
                d.get("decile", 1),
                d.get("avg_return_pct", 0.0),
                d.get("median_return_pct", 0.0),
                d.get("hit_rate", 0.0),
                d.get("sharpe", 0.0)
            ))
        conn.commit()
    finally:
        conn.close()

def save_regime_results(experiment_id: str, regimes: List[Dict[str, Any]]):
    """Persists regime robustness evaluation findings."""
    init_discovery_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        for r in regimes:
            cur.execute("""
                INSERT INTO research_regime_results (
                    experiment_id, regime_name, sample_count, ic_mean, rank_ic_mean, spread_pct, win_rate
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                experiment_id,
                r.get("regime_name", "UNKNOWN"),
                r.get("sample_count", 0),
                r.get("ic_mean", 0.0),
                r.get("rank_ic_mean", 0.0),
                r.get("spread_pct", 0.0),
                r.get("win_rate", 0.0)
            ))
        conn.commit()
    finally:
        conn.close()

def save_oos_result(experiment_id: str, oos_res: Dict[str, Any]):
    """Persists the single frozen locked OOS evaluation."""
    init_discovery_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO research_oos_results (
                experiment_id, candidate_name, oos_hash, ic, rank_ic, sharpe,
                trade_count, win_rate, max_dd_pct, verdict, report_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            experiment_id,
            oos_res.get("candidate_name", "Frozen Candidate"),
            oos_res.get("oos_hash", ""),
            oos_res.get("ic", 0.0),
            oos_res.get("rank_ic", 0.0),
            oos_res.get("sharpe", 0.0),
            oos_res.get("trade_count", 0),
            oos_res.get("win_rate", 0.0),
            oos_res.get("max_dd_pct", 0.0),
            oos_res.get("verdict", "NO ROBUST SIGNAL"),
            json.dumps(oos_res)
        ))
        conn.commit()
    finally:
        conn.close()

def get_latest_discovery_run() -> Optional[Dict[str, Any]]:
    """Retrieves the most recent complete discovery run from the ledger."""
    init_discovery_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT experiment_id, timestamp, universe, universe_mode, timeframe,
                   horizon_days, target_type, feature_family, model_family, seed,
                   config_hash, dataset_hash, fingerprint, status, verdict, metrics_json
            FROM research_experiments
            ORDER BY timestamp DESC LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            return None
        
        exp_id = row[0]
        # Fetch associated feature stats
        cur.execute("SELECT feature_name, ic_mean, ic_std, icir, rank_ic_mean, rank_icir, positive_ic_pct, stability_class, cluster_id, window_ics_json FROM research_feature_stats WHERE experiment_id = ?", (exp_id,))
        feats = []
        for f_row in cur.fetchall():
            feats.append({
                "feature_name": f_row[0],
                "ic_mean": f_row[1],
                "ic_std": f_row[2],
                "icir": f_row[3],
                "rank_ic_mean": f_row[4],
                "rank_icir": f_row[5],
                "positive_ic_pct": f_row[6],
                "stability_class": f_row[7],
                "cluster_id": f_row[8],
                "window_ics": json.loads(f_row[9]) if f_row[9] else []
            })
            
        # Fetch associated deciles
        cur.execute("SELECT decile, avg_return_pct, median_return_pct, hit_rate, sharpe FROM research_decile_results WHERE experiment_id = ? ORDER BY decile ASC", (exp_id,))
        deciles = []
        for d_row in cur.fetchall():
            deciles.append({
                "decile": d_row[0],
                "avg_return_pct": d_row[1],
                "median_return_pct": d_row[2],
                "hit_rate": d_row[3],
                "sharpe": d_row[4]
            })

        # Fetch associated regimes
        cur.execute("SELECT regime_name, sample_count, ic_mean, rank_ic_mean, spread_pct, win_rate FROM research_regime_results WHERE experiment_id = ?", (exp_id,))
        regimes = []
        for r_row in cur.fetchall():
            regimes.append({
                "regime_name": r_row[0],
                "sample_count": r_row[1],
                "ic_mean": r_row[2],
                "rank_ic_mean": r_row[3],
                "spread_pct": r_row[4],
                "win_rate": r_row[5]
            })

        # Fetch OOS result
        cur.execute("SELECT candidate_name, oos_hash, ic, rank_ic, sharpe, trade_count, win_rate, max_dd_pct, verdict, report_json FROM research_oos_results WHERE experiment_id = ?", (exp_id,))
        oos_row = cur.fetchone()
        oos_result = None
        if oos_row:
            oos_result = {
                "candidate_name": oos_row[0],
                "oos_hash": oos_row[1],
                "ic": oos_row[2],
                "rank_ic": oos_row[3],
                "sharpe": oos_row[4],
                "trade_count": oos_row[5],
                "win_rate": oos_row[6],
                "max_dd_pct": oos_row[7],
                "verdict": oos_row[8],
                "details": json.loads(oos_row[9]) if oos_row[9] else {}
            }

        return {
            "experiment_id": row[0],
            "timestamp": row[1],
            "universe": row[2],
            "universe_mode": row[3],
            "timeframe": row[4],
            "horizon_days": row[5],
            "target_type": row[6],
            "feature_family": row[7],
            "model_family": row[8],
            "seed": row[9],
            "config_hash": row[10],
            "dataset_hash": row[11],
            "fingerprint": row[12],
            "status": row[13],
            "verdict": row[14],
            "metrics": json.loads(row[15]) if row[15] else {},
            "feature_stats": feats,
            "decile_results": deciles,
            "regime_results": regimes,
            "oos_result": oos_result
        }
    finally:
        conn.close()

def list_all_experiments() -> List[Dict[str, Any]]:
    """Lists past experiments with summary metadata."""
    init_discovery_ledger_schema()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT experiment_id, timestamp, universe, timeframe, horizon_days, target_type,
                   model_family, status, verdict
            FROM research_experiments
            ORDER BY timestamp DESC LIMIT 50
        """)
        rows = cur.fetchall()
        return [{
            "experiment_id": r[0],
            "timestamp": r[1],
            "universe": r[2],
            "timeframe": r[3],
            "horizon_days": r[4],
            "target_type": r[5],
            "model_family": r[6],
            "status": r[7],
            "verdict": r[8]
        } for r in rows]
    finally:
        conn.close()
