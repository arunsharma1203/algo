import sqlite3
import json
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)

class MasterLogger:
    """
    Centralized, high-performance master logger for all trading system events.
    Records structured events for manual scans, autonomous sweeps, model inferences,
    Telegram dispatches, research lifecycles, and risk events into SQLite.
    """
    _table_initialized = False

    @classmethod
    def _ensure_table(cls, conn: sqlite3.Connection):
        if cls._table_initialized:
            return
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_master_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                category TEXT NOT NULL,
                event_type TEXT NOT NULL,
                ticker TEXT,
                universe TEXT,
                message TEXT NOT NULL,
                details_json TEXT,
                severity TEXT DEFAULT 'INFO'
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ame_timestamp ON app_master_events(timestamp);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ame_category ON app_master_events(category);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ame_ticker ON app_master_events(ticker);")
        cls._table_initialized = True

    @classmethod
    def log_event(
        cls,
        category: str,
        event_type: str,
        message: str,
        ticker: Optional[str] = None,
        universe: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        severity: str = "INFO"
    ) -> bool:
        """
        Logs a structured application-level event. Never raises an exception.
        Categories: 'SCAN_MANUAL', 'SCAN_AUTONOMOUS', 'DECISION_ENGINE', 'TELEGRAM',
                    'RESEARCH', 'SCHEDULER', 'AI_GUARD', 'PROMOTION'
        """
        conn = None
        try:
            db_path = get_db_path()
            conn = sqlite3.connect(db_path, timeout=10.0)
            conn.execute("PRAGMA busy_timeout = 10000;")
            cls._ensure_table(conn)
            
            now_iso = datetime.now().isoformat()
            details_str = json.dumps(details, default=str) if details else None
            
            conn.execute("""
                INSERT INTO app_master_events (
                    timestamp, category, event_type, ticker, universe, message, details_json, severity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (now_iso, category, event_type, ticker, universe, message, details_str, severity))
            conn.commit()
            
            log_fn = getattr(logger, severity.lower(), logger.info)
            log_fn(f"[{category}:{event_type}] {message} {f'({ticker})' if ticker else ''}")
            return True
        except Exception as e:
            logger.error(f"[MasterLogger] Failed to write event: {e}")
            return False
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @classmethod
    def get_events(
        cls,
        category: Optional[str] = None,
        event_type: Optional[str] = None,
        ticker: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Queries master audit events with flexible filtering."""
        conn = None
        try:
            db_path = get_db_path()
            conn = sqlite3.connect(db_path, timeout=10.0)
            conn.execute("PRAGMA busy_timeout = 10000;")
            conn.row_factory = sqlite3.Row
            cls._ensure_table(conn)
            
            query = "SELECT * FROM app_master_events WHERE 1=1"
            params = []
            
            if category:
                query += " AND category = ?"
                params.append(category)
            if event_type:
                query += " AND event_type = ?"
                params.append(event_type)
            if ticker:
                query += " AND ticker = ?"
                params.append(ticker.upper())
            if severity:
                query += " AND severity = ?"
                params.append(severity.upper())
                
            query += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            
            rows = conn.execute(query, params).fetchall()
            events = []
            for r in rows:
                ev = dict(r)
                if ev.get("details_json"):
                    try:
                        ev["details"] = json.loads(ev["details_json"])
                    except Exception:
                        ev["details"] = ev["details_json"]
                events.append(ev)
            return events
        except Exception as e:
            logger.error(f"[MasterLogger] Query failed: {e}")
            return []
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

