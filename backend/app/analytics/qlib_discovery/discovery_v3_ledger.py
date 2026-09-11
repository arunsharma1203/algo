"""
QLIB SIGNAL DISCOVERY V3: RESEARCH LEDGER
========================================
Stores immutable V3 experimental records, turnover trade-off frontiers,
hysteresis comparisons, and governance verdicts in SQLite.
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
ENGINE_VERSION = "v3.0-economic-efficiency"

def ensure_v3_tables() -> None:
    """Creates SQLite tables for V3 research if they do not exist."""
    conn = sqlite3.connect(get_db_path())
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS research_v3_experiments (
        experiment_id TEXT PRIMARY KEY,
        parent_v2_id TEXT,
        timestamp TEXT,
        universe TEXT,
        horizon_days INTEGER,
        entry_top_k INTEGER,
        exit_top_k INTEGER,
        config_hash TEXT,
        dataset_hash TEXT,
        fingerprint TEXT,
        status TEXT,
        verdict TEXT,
        metrics_json TEXT
    );
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS research_v3_frontier (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        experiment_id TEXT,
        archetype TEXT,
        turnover_pct REAL,
        cagr_gross REAL,
        friction_drag_bps REAL,
        cagr_net REAL,
        sharpe REAL,
        survives INTEGER,
        FOREIGN KEY(experiment_id) REFERENCES research_v3_experiments(experiment_id)
    );
    """)

    conn.commit()
    conn.close()

def compute_v3_fingerprint(
    experiment_id: str,
    dataset_hash: str,
    config_hash: str,
    verdict: str
) -> str:
    """Computes deterministic SHA-256 fingerprint for V3 experiment."""
    raw = f"{experiment_id}|{dataset_hash}|{config_hash}|{verdict}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def save_v3_experiment_record(
    experiment_id: str,
    parent_v2_id: str,
    universe: str,
    horizon_days: int,
    entry_top_k: int,
    exit_top_k: int,
    config_hash: str,
    dataset_hash: str,
    fingerprint: str,
    status: str,
    verdict: str,
    metrics: Dict[str, Any]
) -> None:
    """Persists a complete V3 experiment run."""
    ensure_v3_tables()
    conn = sqlite3.connect(get_db_path())
    c = conn.cursor()

    now_iso = datetime.now().isoformat()
    metrics_str = json.dumps(metrics)

    c.execute("""
    INSERT OR REPLACE INTO research_v3_experiments (
        experiment_id, parent_v2_id, timestamp, universe,
        horizon_days, entry_top_k, exit_top_k, config_hash,
        dataset_hash, fingerprint, status, verdict, metrics_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        experiment_id, parent_v2_id, now_iso, universe,
        horizon_days, entry_top_k, exit_top_k, config_hash,
        dataset_hash, fingerprint, status, verdict, metrics_str
    ))

    # Persist frontier rows if available
    frontier_rows = metrics.get("turnover_frontier", [])
    for row in frontier_rows:
        c.execute("""
        INSERT INTO research_v3_frontier (
            experiment_id, archetype, turnover_pct, cagr_gross,
            friction_drag_bps, cagr_net, sharpe, survives
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            experiment_id, row.get("archetype", ""),
            row.get("turnover_pct", 0.0), row.get("cagr_gross", 0.0),
            row.get("friction_drag_bps", 0.0), row.get("cagr_net", 0.0),
            row.get("sharpe", 0.0), 1 if row.get("survives") else 0
        ))

    conn.commit()
    conn.close()
    logger.info(f"[DiscoveryV3Ledger] Saved experiment {experiment_id} with verdict {verdict}")

def get_latest_v3_discovery_run() -> Optional[Dict[str, Any]]:
    """Retrieves the latest completed V3 discovery run."""
    ensure_v3_tables()
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
    SELECT * FROM research_v3_experiments
    ORDER BY timestamp DESC
    LIMIT 1;
    """)
    row = c.fetchone()
    conn.close()

    if not row:
        return None

    res = dict(row)
    try:
        res["metrics"] = json.loads(res["metrics_json"])
    except Exception:
        res["metrics"] = {}

    return res

def list_all_v3_experiments() -> List[Dict[str, Any]]:
    """Lists past V3 discovery experiments."""
    ensure_v3_tables()
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
    SELECT experiment_id, parent_v2_id, timestamp, universe, horizon_days,
           entry_top_k, exit_top_k, fingerprint, status, verdict
    FROM research_v3_experiments
    ORDER BY timestamp DESC;
    """)
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]
