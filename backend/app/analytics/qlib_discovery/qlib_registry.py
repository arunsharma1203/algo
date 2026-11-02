"""
QLIB MODEL REGISTRY & ISOLATED EVALUATION STORAGE
=================================================
Provenance:
    Implementation Version: qlib_research_v1
    Provenance Label: "QLIB-INSPIRED / QLIB-COMPATIBLE FEATURE IMPLEMENTATION"
    Isolation: Strictly dedicated table 'qlib_model_evaluations'
    Production Safety: 0 ml_trade_history impact, 0 heat, 0 broker, 0 Telegram
"""

import sqlite3
import json
import hashlib
from datetime import datetime
import os
import logging
from typing import Dict, Any, List, Optional
from app.data.historical_data_layer import get_db_path
from app.analytics.model_manager import ModelManager

logger = logging.getLogger(__name__)

CHAMPION_HASHES = {
    "intraday": "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91",
    "swing": "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"
}

def get_authoritative_champion_path(timeframe: str) -> str:
    """Resolves authoritative Champion model path regardless of process working directory."""
    return ModelManager.get_champion_paths(timeframe)[0]

class _AuthoritativeChampionPaths(dict):
    def __getitem__(self, item):
        return get_authoritative_champion_path(item)
    def get(self, item, default=None):
        try:
            return get_authoritative_champion_path(item)
        except Exception:
            return default
    def items(self):
        return [(tf, get_authoritative_champion_path(tf)) for tf in CHAMPION_HASHES]

CHAMPION_PATHS = _AuthoritativeChampionPaths()

def verify_champion_immutability() -> bool:
    """Verifies both Champion model hashes byte-for-byte using authoritative paths."""
    for tf, exp_hash in CHAMPION_HASHES.items():
        p = get_authoritative_champion_path(tf)
        if not os.path.exists(p):
            raise FileNotFoundError(f"Champion artifact missing at resolved path: '{p}' (cwd: '{os.getcwd()}')")
        with open(p, 'rb') as f:
            act_hash = hashlib.sha256(f.read()).hexdigest()
        if act_hash != exp_hash:
            raise RuntimeError(f"CHAMPION TAMPERING DETECTED! {tf.upper()} hash mismatch: expected {exp_hash}, got {act_hash}")
        logger.info(f"[ChampionGuard] Verified {tf.upper()} Champion immutability: {act_hash[:12]}... at {p}")
    return True

def ensure_qlib_table():
    """Initializes isolated evaluation table in SQLite."""
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS qlib_model_evaluations (
            evaluation_id TEXT PRIMARY KEY,
            timestamp TEXT,
            strategy TEXT,
            model_name TEXT,
            feature_family TEXT,
            feature_version TEXT,
            provenance TEXT,
            dataset_hash TEXT,
            oos_hash TEXT,
            model_artifact_hash TEXT,
            f1 REAL,
            precision REAL,
            recall REAL,
            brier REAL,
            trade_count INTEGER,
            win_rate REAL,
            net_pnl_pct REAL,
            expectancy REAL,
            profit_factor REAL,
            sharpe_ratio REAL,
            max_drawdown_pct REAL,
            gate_verdict TEXT,
            metrics_json TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_qlib_evaluation(record: Dict[str, Any]) -> str:
    """Persists a Qlib candidate evaluation record to SQLite."""
    ensure_qlib_table()
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    
    eval_id = record.get("evaluation_id") or f"qlib_eval_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    now_str = datetime.now().isoformat()
    
    conn.execute("""
        INSERT OR REPLACE INTO qlib_model_evaluations (
            evaluation_id, timestamp, strategy, model_name, feature_family,
            feature_version, provenance, dataset_hash, oos_hash, model_artifact_hash,
            f1, precision, recall, brier, trade_count, win_rate, net_pnl_pct,
            expectancy, profit_factor, sharpe_ratio, max_drawdown_pct,
            gate_verdict, metrics_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        eval_id,
        now_str,
        record.get("strategy"),
        record.get("model_name"),
        record.get("feature_family"),
        record.get("feature_version", "qlib_research_v1"),
        record.get("provenance", "QLIB-INSPIRED / QLIB-COMPATIBLE FEATURE IMPLEMENTATION"),
        record.get("dataset_hash", ""),
        record.get("oos_hash", ""),
        record.get("model_artifact_hash", ""),
        float(record.get("f1", 0.0)),
        float(record.get("precision", 0.0)),
        float(record.get("recall", 0.0)),
        float(record.get("brier", 0.0)),
        int(record.get("trade_count", 0)),
        float(record.get("win_rate", 0.0)),
        float(record.get("net_pnl_pct", 0.0)),
        float(record.get("expectancy", 0.0)),
        float(record.get("profit_factor", 0.0)),
        float(record.get("sharpe_ratio", 0.0)),
        float(record.get("max_drawdown_pct", 0.0)),
        record.get("gate_verdict", "RETAIN CHAMPION"),
        json.dumps(record.get("metrics_json", {}))
    ))
    conn.commit()
    conn.close()
    return eval_id

def get_qlib_evaluations(strategy: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves past evaluations sorted by timestamp DESC."""
    ensure_qlib_table()
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    query = "SELECT * FROM qlib_model_evaluations"
    params = []
    if strategy:
        query += " WHERE strategy = ?"
        params.append(strategy.upper())
    query += " ORDER BY timestamp DESC"
    
    cur = conn.execute(query, params)
    cols = [col[0] for col in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    
    for r in rows:
        if r.get('metrics_json'):
            try:
                r['metrics_json'] = json.loads(r['metrics_json'])
            except:
                pass
    return rows
