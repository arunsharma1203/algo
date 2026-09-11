import os
import sqlite3
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Authoritative canonical database path
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_CANONICAL_DB_PATH = os.path.abspath(os.path.join(_BACKEND_DIR, "market_data.db"))

# Test isolation override (in-memory or temp file for test isolation)
_TEST_DB_PATH: Optional[str] = None
_AUTO_TEST_DB_PATH: Optional[str] = None
_ALLOW_PRODUCTION_WRITE_IN_TEST: bool = False

class ProductionDatabaseWriteBlockedError(RuntimeError):
    """Raised when an operation attempts to write to the canonical production database during automated testing."""
    pass

def set_test_db_override(path: Optional[str]) -> None:
    """
    Sets a thread-safe / in-process database path override for isolated unit testing.
    When set, all database operations route to this isolated path.
    Pass None to reset back to canonical production database.
    """
    global _TEST_DB_PATH
    _TEST_DB_PATH = path

def get_canonical_db_path() -> str:
    """
    Always returns the absolute path to the authoritative production market_data.db.
    Used by safety verification to ensure tests never audit a mock instead of production.
    """
    return _CANONICAL_DB_PATH

def is_testing_environment() -> bool:
    """
    Detects if execution is running in an automated test environment.
    Checks:
    1. TESTING environment variable is set to 'true', '1', or 'yes'.
    2. PYTEST_CURRENT_TEST is present.
    3. Active execution stack or sys.argv contains pytest or unittest.
    """
    if os.environ.get("ALLOW_PRODUCTION_MUTATION_DANGEROUS") == "true":
        return False
    if os.environ.get("TESTING", "").lower() in ("true", "1", "yes"):
        return True
    if "PYTEST_CURRENT_TEST" in os.environ:
        return True
    import sys
    if any("pytest" in arg or "unittest" in arg for arg in sys.argv):
        return True
    if "unittest" in sys.modules or "pytest" in sys.modules:
        import inspect
        for frame in inspect.stack()[:10]:
            if "unittest" in frame.filename or "pytest" in frame.filename:
                return True
    return False

def _init_auto_test_db() -> str:
    """Provisions a fast, isolated temporary SQLite database cloned from production DDL."""
    global _AUTO_TEST_DB_PATH
    if _AUTO_TEST_DB_PATH and os.path.exists(_AUTO_TEST_DB_PATH):
        return _AUTO_TEST_DB_PATH

    import tempfile
    tfile = tempfile.NamedTemporaryFile(prefix="auto_isolated_test_db_", suffix=".db", delete=False)
    _AUTO_TEST_DB_PATH = tfile.name
    tfile.close()

    try:
        if os.path.exists(_CANONICAL_DB_PATH):
            src = sqlite3.connect(f"file:{_CANONICAL_DB_PATH}?mode=ro", uri=True, timeout=5.0)
            dst = sqlite3.connect(_AUTO_TEST_DB_PATH)
            tables = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL").fetchall()
            for sql in tables:
                try:
                    dst.execute(sql[0])
                except Exception:
                    pass
            dst.commit()
            dst.close()
            src.close()
    except Exception as e:
        logger.warning(f"[DATABASE ISOLATION] Could not clone DDL to auto-test DB: {e}")

    logger.info(f"[DATABASE ISOLATION LOCKDOWN] Routed to auto-isolated sandbox: {_AUTO_TEST_DB_PATH}")
    return _AUTO_TEST_DB_PATH

def get_db_path() -> str:
    """
    Returns the active database path.
    Guaranteed:
    1. Returns _TEST_DB_PATH if set via set_test_db_override() for test isolation.
    2. Returns RESEARCH_TEST_DB_PATH environment variable if set.
    3. If in test environment without an override, auto-provisions an isolated sandbox DB
       to strictly prevent accidental pollution of backend/market_data.db.
    4. Defaults strictly to _CANONICAL_DB_PATH (backend/market_data.db) in live production.
    """
    if _TEST_DB_PATH:
        return _TEST_DB_PATH
    env_override = os.environ.get("RESEARCH_TEST_DB_PATH")
    if env_override:
        return env_override
    if is_testing_environment():
        return _init_auto_test_db()
    return _CANONICAL_DB_PATH

def is_test_db_active() -> bool:
    """Returns True if a test database override or test environment is currently active."""
    return bool(_TEST_DB_PATH or os.environ.get("RESEARCH_TEST_DB_PATH") or is_testing_environment())

def get_connection(timeout: float = 30.0) -> sqlite3.Connection:
    """
    Returns a standard SQLite connection to the active database with WAL mode enabled.
    Enforces strict write protection: fails closed if write attempted on canonical DB in test mode.
    """
    db_path = get_db_path()
    if is_testing_environment() and is_canonical_path(db_path) and not _ALLOW_PRODUCTION_WRITE_IN_TEST:
        raise ProductionDatabaseWriteBlockedError(
            f"FATAL: Attempted to open writable connection to canonical production database "
            f"({_CANONICAL_DB_PATH}) during test execution! Tests must use isolated test DB."
        )
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
