import os
import sqlite3
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# The single authoritative package-relative root directory of backend
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_CANONICAL_DB_PATH = os.path.abspath(os.path.join(_BACKEND_DIR, "market_data.db"))

def get_db_path() -> str:
    """
    Returns the single authoritative, canonical absolute path to backend/market_data.db.
    Guaranteed:
    1. Package-relative / project-relative (never depends on CWD).
    2. Returns an absolute normalized path.
    3. Never falls back to ./market_data.db or parent directories.
    4. Safe across all execution contexts (CLI, FastAPI, APScheduler, multiprocessing, tests).
    """
    return _CANONICAL_DB_PATH

def get_connection(timeout: float = 30.0) -> sqlite3.Connection:
    """
    Returns a standard SQLite connection to the canonical database with WAL mode enabled.
    """
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def get_readonly_connection(timeout: float = 5.0) -> sqlite3.Connection:
    """
    Returns a strictly READ-ONLY SQLite connection using the file URI mode.
    Guarantees no modifications, no lock escalation, and no journal mutations.
    """
    db_path = get_db_path()
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=timeout)

def is_canonical_path(path: str) -> bool:
    """Checks if a given path matches the authoritative canonical database path."""
    if not path:
        return False
    return os.path.abspath(os.path.normpath(path)) == _CANONICAL_DB_PATH
