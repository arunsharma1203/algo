"""
QLIB TRAINING JOB TRACKER
=========================
Authoritative SQLite persistence layer for Qlib training jobs.
Ensures training lifecycle state survives browser refreshes, server restarts,
and multi-client disconnections.
"""

import os
import json
import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)


class TrainingJobTracker:
    """
    Manages persistent records of Qlib training jobs in canonical SQLite database.
    """

    _db_path_override: Optional[str] = None

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self._db_path_override = db_path
            TrainingJobTracker._db_path_override = db_path
            self.init_schema()

    @classmethod
    def _get_connection(cls, timeout: float = 15.0) -> sqlite3.Connection:
        db_path = cls._db_path_override or get_db_path()
        conn = sqlite3.connect(db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 15000;")
        return conn

    @classmethod
    def init_schema(cls):
        """Initializes qlib_training_jobs and performance indexes."""
        conn = cls._get_connection()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS qlib_training_jobs (
                    job_id TEXT PRIMARY KEY,
                    strategy TEXT,
                    universe TEXT,
                    feature_family TEXT,
                    model_family TEXT,
                    status TEXT, -- QUEUED, RUNNING, VALIDATING, COMPLETED, FAILED, CANCELLED
                    stage TEXT,
                    stage_index INTEGER,
                    total_stages INTEGER,
                    stage_description TEXT,
                    progress_pct REAL,
                    elapsed_seconds REAL,
                    estimated_remaining_seconds REAL,
                    estimated_total_seconds REAL,
                    is_eta_uncertain INTEGER,
                    memory_rss_mb REAL,
                    peak_memory_rss_mb REAL,
                    rows_processed INTEGER,
                    features_count INTEGER,
                    instruments_count INTEGER,
                    error TEXT,
                    candidate_model_id TEXT,
                    metrics_json TEXT,
                    created_at TEXT,
                    started_at TEXT,
                    updated_at TEXT,
                    completed_at TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_qlib_jobs_status ON qlib_training_jobs(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_qlib_jobs_created ON qlib_training_jobs(created_at);")
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def create_job(
        cls,
        job_id: str,
        strategy: str,
        universe: str = "NIFTY_500_ESTABLISHED",
        feature_family: str = "Alpha158",
        model_family: str = "LGBModel",
        instruments_count: int = 0
    ) -> Dict[str, Any]:
        """Registers a newly started training job in persistent storage."""
        cls.init_schema()
        now_iso = datetime.now().isoformat()
        conn = cls._get_connection()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO qlib_training_jobs (
                    job_id, strategy, universe, feature_family, model_family,
                    status, stage, stage_index, total_stages, stage_description,
                    progress_pct, elapsed_seconds, estimated_remaining_seconds,
                    estimated_total_seconds, is_eta_uncertain, memory_rss_mb,
                    peak_memory_rss_mb, rows_processed, features_count,
                    instruments_count, error, candidate_model_id, metrics_json,
                    created_at, started_at, updated_at, completed_at
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    'RUNNING', 'INITIALIZING', 1, 11, 'Initializing Microsoft Qlib research environment',
                    5.0, 0.0, NULL,
                    NULL, 1, 0.0,
                    0.0, 0, 0,
                    ?, NULL, NULL, NULL,
                    ?, ?, ?, NULL
                )
            """, (
                job_id, strategy.upper(), universe, feature_family, model_family,
                instruments_count, now_iso, now_iso, now_iso
            ))
            conn.commit()
            logger.info(f"[TrainingJobTracker] Persisted new job {job_id} ({strategy}, {universe})")
            return cls.get_job(job_id)
        finally:
            conn.close()

    @classmethod
    def update_progress(
        cls,
        job_id: str,
        stage: str,
        stage_index: int = 1,
        total_stages: int = 10,
        stage_description: str = "",
        progress_pct: float = 0.0,
        elapsed_seconds: float = 0.0,
        estimated_remaining_seconds: Optional[float] = None,
        estimated_total_seconds: Optional[float] = None,
        is_eta_uncertain: bool = False,
        memory_rss_mb: float = 0.0,
        peak_memory_rss_mb: float = 0.0,
        rows_processed: int = 0,
        features_count: int = 0,
        instruments_count: Optional[int] = None,
        status: Optional[str] = None
    ) -> bool:
        """Updates progress metrics for an ongoing job."""
        now_iso = datetime.now().isoformat()
        conn = cls._get_connection()
        try:
            # Determine status if not explicitly given
            job_status = status
            if not job_status:
                if stage == "VALIDATING_MODEL":
                    job_status = "VALIDATING"
                elif stage == "COMPLETE":
                    job_status = "COMPLETED"
                else:
                    job_status = "RUNNING"

            update_sql = """
                UPDATE qlib_training_jobs SET
                    stage = ?,
                    stage_index = ?,
                    total_stages = ?,
                    stage_description = ?,
                    progress_pct = ?,
                    elapsed_seconds = ?,
                    estimated_remaining_seconds = ?,
                    estimated_total_seconds = ?,
                    is_eta_uncertain = ?,
                    memory_rss_mb = ?,
                    peak_memory_rss_mb = ?,
                    rows_processed = ?,
                    features_count = ?,
                    status = ?,
                    updated_at = ?
            """
            params = [
                stage, stage_index, total_stages, stage_description,
                progress_pct, elapsed_seconds, estimated_remaining_seconds,
                estimated_total_seconds, 1 if is_eta_uncertain else 0,
                memory_rss_mb, peak_memory_rss_mb, rows_processed,
                features_count, job_status, now_iso
            ]

            if instruments_count is not None and instruments_count > 0:
                update_sql += ", instruments_count = ?"
                params.append(instruments_count)

            update_sql += " WHERE job_id = ?"
            params.append(job_id)

            conn.execute(update_sql, params)
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"[TrainingJobTracker] Failed to update progress for {job_id}: {e}")
            return False
        finally:
            conn.close()

    @classmethod
    def complete_job(
        cls,
        job_id: str,
        candidate_model_id: str,
        metrics: Optional[Dict[str, Any]] = None,
        elapsed_seconds: Optional[float] = None
    ) -> bool:
        """Marks a job as successfully completed with candidate artifact details."""
        now_iso = datetime.now().isoformat()
        conn = cls._get_connection()
        try:
            metrics_str = json.dumps(metrics, default=str) if metrics else None
            conn.execute("""
                UPDATE qlib_training_jobs SET
                    status = 'COMPLETED',
                    stage = 'COMPLETE',
                    stage_index = 10,
                    progress_pct = 100.0,
                    stage_description = 'Training complete. Model artifact frozen in Challenger Vault.',
                    candidate_model_id = ?,
                    metrics_json = ?,
                    completed_at = ?,
                    updated_at = ?,
                    elapsed_seconds = COALESCE(?, elapsed_seconds),
                    estimated_remaining_seconds = 0.0
                WHERE job_id = ?
            """, (candidate_model_id, metrics_str, now_iso, now_iso, elapsed_seconds, job_id))
            conn.commit()
            logger.info(f"[TrainingJobTracker] Marked job {job_id} as COMPLETED (candidate: {candidate_model_id})")
            return True
        except Exception as e:
            logger.error(f"[TrainingJobTracker] Failed to complete job {job_id}: {e}")
            return False
        finally:
            conn.close()

    @classmethod
    def fail_job(cls, job_id: str, error: str, elapsed_seconds: Optional[float] = None) -> bool:
        """Marks a job as FAILED with the error reason."""
        now_iso = datetime.now().isoformat()
        conn = cls._get_connection()
        try:
            conn.execute("""
                UPDATE qlib_training_jobs SET
                    status = 'FAILED',
                    error = ?,
                    completed_at = ?,
                    updated_at = ?,
                    elapsed_seconds = COALESCE(?, elapsed_seconds),
                    estimated_remaining_seconds = 0.0
                WHERE job_id = ?
            """, (error, now_iso, now_iso, elapsed_seconds, job_id))
            conn.commit()
            logger.info(f"[TrainingJobTracker] Marked job {job_id} as FAILED: {error}")
            return True
        except Exception as e:
            logger.error(f"[TrainingJobTracker] Failed to mark job {job_id} as failed: {e}")
            return False
        finally:
            conn.close()

    @classmethod
    def get_job(cls, job_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single job by its ID."""
        cls.init_schema()
        conn = cls._get_connection()
        try:
            row = conn.execute("SELECT * FROM qlib_training_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None
            return cls._row_to_dict(row)
        finally:
            conn.close()

    @classmethod
    def cancel_all_active_jobs(cls, reason: str = "Operator cancelled or reset from UI") -> int:
        """Cancels all currently active/queued/running jobs in persistent storage."""
        cls.init_schema()
        now_iso = datetime.now().isoformat()
        conn = cls._get_connection()
        try:
            cur = conn.execute("""
                UPDATE qlib_training_jobs SET
                    status = 'CANCELLED',
                    error = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE status IN ('QUEUED', 'RUNNING', 'VALIDATING')
            """, (reason, now_iso, now_iso))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    @classmethod
    def get_active_job(cls, stale_timeout_seconds: float = 900.0) -> Optional[Dict[str, Any]]:
        """
        Returns the currently active job (status IN ('QUEUED', 'RUNNING', 'VALIDATING')).
        Automatically reaps stale jobs older than stale_timeout_seconds (default 15 minutes).
        Returns None if no job is actively executing.
        """
        cls.init_schema()
        conn = cls._get_connection()
        try:
            # First, reap any stale jobs that haven't been updated in stale_timeout_seconds
            now = datetime.now()
            rows = conn.execute("""
                SELECT job_id, updated_at, started_at FROM qlib_training_jobs 
                WHERE status IN ('QUEUED', 'RUNNING', 'VALIDATING')
            """).fetchall()
            reaped = False
            for r in rows:
                jid = r["job_id"]
                upd = r["updated_at"] or r["started_at"]
                if upd:
                    try:
                        upd_dt = datetime.fromisoformat(upd)
                        if (now - upd_dt).total_seconds() > stale_timeout_seconds:
                            conn.execute("""
                                UPDATE qlib_training_jobs SET
                                    status = 'CANCELLED',
                                    error = 'Automatically reaped: job timed out or server restarted',
                                    completed_at = ?,
                                    updated_at = ?
                                WHERE job_id = ?
                            """, (now.isoformat(), now.isoformat(), jid))
                            reaped = True
                    except Exception:
                        pass
            if reaped:
                conn.commit()

            row = conn.execute("""
                SELECT * FROM qlib_training_jobs 
                WHERE status IN ('QUEUED', 'RUNNING', 'VALIDATING')
                ORDER BY started_at DESC LIMIT 1
            """).fetchone()
            if not row:
                return None
            return cls._row_to_dict(row)
        finally:
            conn.close()

    @classmethod
    def get_latest_job(cls) -> Optional[Dict[str, Any]]:
        """Returns the most recent job regardless of status."""
        cls.init_schema()
        conn = cls._get_connection()
        try:
            row = conn.execute("""
                SELECT * FROM qlib_training_jobs 
                ORDER BY created_at DESC LIMIT 1
            """).fetchone()
            if not row:
                return None
            return cls._row_to_dict(row)
        finally:
            conn.close()

    @classmethod
    def list_jobs(cls, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns history of training jobs ordered by start time descending."""
        cls.init_schema()
        conn = cls._get_connection()
        try:
            rows = conn.execute("""
                SELECT * FROM qlib_training_jobs 
                ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [cls._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    @classmethod
    def _row_to_dict(cls, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        if d.get("metrics_json"):
            try:
                d["metrics"] = json.loads(d["metrics_json"])
            except Exception:
                d["metrics"] = None
        else:
            d["metrics"] = None
        d["is_eta_uncertain"] = bool(d.get("is_eta_uncertain", 0))
        return d
