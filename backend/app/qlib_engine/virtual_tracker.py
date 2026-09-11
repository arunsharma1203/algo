"""
QLIB VIRTUAL RECOMMENDATION TRACKER
===================================
Tracks generated Qlib recommendations honestly as virtual research signals.

Invariants:
- 100% physically isolated in `qlib_virtual_recommendations` table.
- 0 portfolio heat consumed (never touches `ml_trade_history` live positions).
- 0 broker execution (zero live broker orders).
- Strictly preserves provenance: model_id, model_hash, feature_version, engine='QLIB'.
"""

import os
import json
import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)


def init_qlib_tracking_schema():
    """Initializes dedicated Qlib virtual recommendation table."""
    db_file = get_db_path()
    conn = sqlite3.connect(db_file)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS qlib_virtual_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                strategy TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                target_price REAL NOT NULL,
                confidence REAL NOT NULL,
                status TEXT DEFAULT 'OPEN',
                outcome TEXT,
                pnl_pct REAL,
                exit_price REAL,
                exit_time TEXT,
                model_id TEXT NOT NULL,
                model_hash TEXT NOT NULL,
                feature_version TEXT NOT NULL,
                data_timestamp TEXT,
                fno_contract_info TEXT,
                engine TEXT DEFAULT 'QLIB'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_qlib_rec_strategy ON qlib_virtual_recommendations(strategy, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_qlib_rec_timestamp ON qlib_virtual_recommendations(timestamp)")
        conn.commit()
    finally:
        conn.close()


class QlibVirtualTracker:
    """
    Records and monitors Qlib virtual recommendations.
    """

    @classmethod
    def record_recommendation(
        cls,
        ticker: str,
        strategy: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        target_price: float,
        confidence: float,
        model_id: str,
        model_hash: str,
        feature_version: str,
        data_timestamp: Optional[str] = None,
        fno_contract_info: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Records a new virtual recommendation in SQLite.
        """
        init_qlib_tracking_schema()
        db_file = get_db_path()
        conn = sqlite3.connect(db_file)
        now_iso = datetime.now().isoformat()

        fno_json = json.dumps(fno_contract_info) if fno_contract_info else None

        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO qlib_virtual_recommendations (
                    timestamp, ticker, strategy, direction,
                    entry_price, stop_loss, target_price, confidence,
                    status, model_id, model_hash, feature_version,
                    data_timestamp, fno_contract_info, engine
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, 'QLIB')
            """, (
                now_iso, ticker.upper().strip(), strategy.upper().strip(), direction.upper().strip(),
                round(float(entry_price), 2), round(float(stop_loss), 2), round(float(target_price), 2),
                round(float(confidence), 4), model_id, model_hash, feature_version,
                data_timestamp or now_iso, fno_json
            ))
            conn.commit()
            rec_id = cursor.lastrowid

            return {
                "id": rec_id,
                "timestamp": now_iso,
                "ticker": ticker.upper().strip(),
                "strategy": strategy.upper().strip(),
                "direction": direction.upper().strip(),
                "entry_price": round(float(entry_price), 2),
                "stop_loss": round(float(stop_loss), 2),
                "target_price": round(float(target_price), 2),
                "confidence": round(float(confidence), 4),
                "status": "OPEN",
                "model_id": model_id,
                "model_hash": model_hash,
                "engine": "QLIB"
            }
        finally:
            conn.close()

    @classmethod
    def get_recommendations(
        cls,
        strategy: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Queries recorded recommendations."""
        init_qlib_tracking_schema()
        db_file = get_db_path()
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        try:
            if strategy:
                rows = conn.execute(
                    "SELECT * FROM qlib_virtual_recommendations WHERE strategy = ? ORDER BY id DESC LIMIT ?",
                    (strategy.upper(), limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM qlib_virtual_recommendations ORDER BY id DESC LIMIT ?",
                    (limit,)
                ).fetchall()

            res = []
            for r in rows:
                d = dict(r)
                if d.get("fno_contract_info"):
                    try:
                        d["fno_contract_info"] = json.loads(d["fno_contract_info"])
                    except Exception:
                        pass
                res.append(d)
            return res
        finally:
            conn.close()

    @classmethod
    def get_performance_summary(cls, strategy: Optional[str] = None) -> Dict[str, Any]:
        """
        Computes performance metrics from virtual trade history.
        If no completed trades exist, returns honest N/A.
        """
        init_qlib_tracking_schema()
        db_file = get_db_path()
        conn = sqlite3.connect(db_file)
        try:
            base_q = "FROM qlib_virtual_recommendations WHERE 1=1"
            params = []
            if strategy:
                base_q += " AND strategy = ?"
                params.append(strategy.upper())

            total_recs = conn.execute(f"SELECT COUNT(*) {base_q}", params).fetchone()[0]
            open_recs = conn.execute(f"SELECT COUNT(*) {base_q} AND status = 'OPEN'", params).fetchone()[0]
            closed_recs = conn.execute(f"SELECT COUNT(*) {base_q} AND status != 'OPEN'", params).fetchone()[0]

            if closed_recs == 0:
                return {
                    "strategy": strategy or "ALL",
                    "total_recommendations": total_recs,
                    "open_trades": open_recs,
                    "completed_trades": 0,
                    "win_rate": None,
                    "profit_factor": None,
                    "net_pnl_pct": None,
                    "display_status": "N/A — no completed virtual trades yet"
                }

            row = conn.execute(f"""
                SELECT 
                    COUNT(CASE WHEN pnl_pct > 0 THEN 1 END) as wins,
                    COUNT(CASE WHEN pnl_pct < 0 THEN 1 END) as losses,
                    AVG(pnl_pct) as avg_pnl,
                    SUM(pnl_pct) as total_pnl,
                    SUM(CASE WHEN pnl_pct > 0 THEN pnl_pct ELSE 0 END) as gross_win,
                    ABS(SUM(CASE WHEN pnl_pct < 0 THEN pnl_pct ELSE 0 END)) as gross_loss
                {base_q} AND status != 'OPEN'
            """, params).fetchone()

            wins = row[0] or 0
            losses = row[1] or 0
            win_rate = round((wins / closed_recs) * 100.0, 2)
            gross_win = row[4] or 0.0
            gross_loss = row[5] or 0.0
            pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_win > 0 else 1.0)

            return {
                "strategy": strategy or "ALL",
                "total_recommendations": total_recs,
                "open_trades": open_recs,
                "completed_trades": closed_recs,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "profit_factor": pf,
                "net_pnl_pct": round(row[3] or 0.0, 2),
                "avg_trade_pnl_pct": round(row[2] or 0.0, 2)
            }
        finally:
            conn.close()
