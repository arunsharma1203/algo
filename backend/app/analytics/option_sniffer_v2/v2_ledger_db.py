"""
V2 Option Sniffer — Dedicated Isolated SQLite Ledger & Read-Only V1 Accessor.

Guarantees:
  - Writes ONLY to V2's dedicated database (`backend/data/option_sniffer_advance_v2.db`,
    or an isolated test database override).
  - Never opens or migrates `backend/market_data.db` in read-write mode; V1 reads
    strictly use SQLite URI `?mode=ro` + `PRAGMA query_only = ON`.
  - Thread-owned SQLite connections (`check_same_thread=True` ALWAYS enforced; zero
    cross-thread shared `sqlite3.Connection`), short transactions, and cold-start read
    methods never create the DB file or execute writes.
  - Persists signals, order intents, fills, package state transitions, atomic ledger
    cash/reserve deltas, completed 1m bars, equity curve points, runtime risk checkpoints,
    daily session qualification snapshots, and strictly segregated V1 counterfactual rows.
"""

from __future__ import annotations

import atexit
from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple

from app.data.database import get_canonical_db_path, is_testing_environment

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_DEFAULT_V2_DB_PATH = os.path.join(_REPO_ROOT, "backend", "data", "option_sniffer_advance_v2.db")

_V2_TEST_DB_OVERRIDE: Optional[str] = None
_AUTO_V2_TEST_DB_PATH: Optional[str] = None
_DB_INIT_LOCK = threading.Lock()


class V2DatabaseIsolationError(RuntimeError):
    """Raised if V2 attempts to write to V1's canonical `market_data.db` or non-isolated test DB."""


def set_v2_test_db_override(path: Optional[str]) -> None:
    global _AUTO_V2_TEST_DB_PATH
    global _V2_TEST_DB_OVERRIDE
    _V2_TEST_DB_OVERRIDE = path


def _cleanup_auto_test_db() -> None:
    global _AUTO_V2_TEST_DB_PATH
    if _AUTO_V2_TEST_DB_PATH and os.path.exists(_AUTO_V2_TEST_DB_PATH):
        try:
            os.remove(_AUTO_V2_TEST_DB_PATH)
        except Exception:
            pass


atexit.register(_cleanup_auto_test_db)


def get_v2_db_path() -> str:
    global _AUTO_V2_TEST_DB_PATH
    if _V2_TEST_DB_OVERRIDE:
        return _V2_TEST_DB_OVERRIDE
    env_p = os.environ.get("V2_SNIFFER_DB_PATH")
    if env_p:
        return env_p
    if is_testing_environment():
        if not _AUTO_V2_TEST_DB_PATH:
            fd, tpath = tempfile.mkstemp(prefix="osv2_isolated_test_", suffix=".db")
            os.close(fd)
            _AUTO_V2_TEST_DB_PATH = tpath
        return _AUTO_V2_TEST_DB_PATH
    return _DEFAULT_V2_DB_PATH


def validate_v2_target_path(db_path: Optional[str] = None) -> str:
    target = os.path.abspath(db_path or get_v2_db_path())
    v1_canon = os.path.abspath(get_canonical_db_path())
    if target == v1_canon:
        raise V2DatabaseIsolationError(
            f"FATAL: V2 refused writable connection to V1 canonical DB ({v1_canon})."
        )
    if is_testing_environment() and target == os.path.abspath(_DEFAULT_V2_DB_PATH):
        raise V2DatabaseIsolationError(
            "FATAL: Test execution attempted to write to runtime V2 DB without isolated override."
        )
    return target


def get_v2_connection(db_path: Optional[str] = None, timeout: float = 15.0) -> sqlite3.Connection:
    """Opens a thread-owned read-write SQLite connection (`check_same_thread=True` preserved)."""
    target = validate_v2_target_path(db_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    conn = sqlite3.connect(target, timeout=timeout, isolation_level=None, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def get_v2_readonly_connection(db_path: str, timeout: float = 10.0) -> sqlite3.Connection:
    """Opens a short-lived strictly read-only handle (`?mode=ro` + `PRAGMA query_only = ON`) without creating files."""
    resolved = os.path.abspath(db_path)
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=timeout, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON;")
    return conn


def open_v1_readonly_db(v1_db_path: Optional[str] = None, timeout: float = 10.0) -> sqlite3.Connection:
    """Strictly read-only URI handle (`?mode=ro` + `PRAGMA query_only = ON`) to inspect V1 archive."""
    resolved = os.path.abspath(v1_db_path or get_canonical_db_path())
    if not os.path.exists(resolved):
        raise FileNotFoundError(f"V1 read-only database not found: {resolved}")
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=timeout, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON;")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, col_name: str, col_def: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table});").fetchall()}
    if col_name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def};")


def init_v2_tables(db_path: Optional[str] = None) -> str:
    """Initializes dedicated `osv2_*` schema in V2's isolated SQLite database."""
    with _DB_INIT_LOCK:
        target = validate_v2_target_path(db_path)
        conn = get_v2_connection(target)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS osv2_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS osv2_events_ingest (
                    event_dedup_key TEXT PRIMARY KEY,
                    run_mode TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    exchange_ts_ms INTEGER NOT NULL,
                    receipt_ts_ms INTEGER NOT NULL,
                    decision_ts_ms INTEGER NOT NULL,
                    sequence_flag TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS osv2_signals (
                    signal_id TEXT PRIMARY KEY,
                    run_mode TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    bar_ts TEXT NOT NULL,
                    decision_ts_ms INTEGER NOT NULL,
                    book_id TEXT NOT NULL,
                    candidate_variant TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reject_reason TEXT,
                    gates_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_osv2_signals_mode_dt
                    ON osv2_signals(run_mode, session_date, book_id);

                CREATE TABLE IF NOT EXISTS osv2_order_intents (
                    intent_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL,
                    package_id TEXT NOT NULL,
                    run_mode TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    leg_role TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    side TEXT NOT NULL,
                    target_qty INTEGER NOT NULL,
                    limit_price REAL,
                    filled_qty INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    reject_reason TEXT,
                    created_ts_ms INTEGER NOT NULL,
                    updated_ts_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS osv2_fills (
                    fill_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL UNIQUE,
                    package_id TEXT NOT NULL,
                    run_mode TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    fill_price REAL NOT NULL,
                    raw_top_price REAL NOT NULL,
                    vwap_raw REAL NOT NULL,
                    slippage_pts REAL NOT NULL,
                    fill_fidelity TEXT NOT NULL,
                    exchange_ts_ms INTEGER NOT NULL,
                    receipt_ts_ms INTEGER NOT NULL,
                    decision_ts_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS osv2_packages (
                    package_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL UNIQUE,
                    run_mode TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    book_id TEXT NOT NULL,
                    candidate_variant TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    state TEXT NOT NULL,
                    fill_fidelity TEXT NOT NULL,
                    direction_label TEXT NOT NULL,
                    entry_bar_ts TEXT NOT NULL,
                    entry_ts_ms INTEGER NOT NULL,
                    leg2_confirmed_ts_ms INTEGER,
                    exit_bar_ts TEXT,
                    exit_ts_ms INTEGER,
                    exit_reason TEXT,
                    legs_json TEXT NOT NULL,
                    accounting_json TEXT,
                    reserved_capital_inr REAL NOT NULL DEFAULT 0.0,
                    structural_max_loss_inr REAL NOT NULL DEFAULT 0.0,
                    planned_loss_inr REAL NOT NULL DEFAULT 0.0,
                    actual_gross_fill_loss_inr REAL NOT NULL DEFAULT 0.0,
                    slippage_and_gap_overshoot_inr REAL NOT NULL DEFAULT 0.0,
                    gross_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    brokerage_inr REAL NOT NULL DEFAULT 0.0,
                    stt_inr REAL NOT NULL DEFAULT 0.0,
                    statutory_charges_inr REAL NOT NULL DEFAULT 0.0,
                    total_friction_inr REAL NOT NULL DEFAULT 0.0,
                    net_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    entry_day_mtm_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    overnight_gap_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    expiry_intraday_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    worst_gap_loss_inr REAL NOT NULL DEFAULT 0.0,
                    is_unpriced_exposure INTEGER NOT NULL DEFAULT 0,
                    unpriced_reason TEXT,
                    unpriced_since_ts_ms INTEGER,
                    last_mark_net_pnl_inr REAL,
                    last_mark_ts_ms INTEGER,
                    last_reliable_mark_net_inr REAL,
                    last_reliable_mark_ts_ms INTEGER,
                    events_timeline_json TEXT,
                    transition_seq INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_osv2_pkg_mode_state
                    ON osv2_packages(run_mode, state, session_date);

                CREATE TABLE IF NOT EXISTS osv2_ledger_entries (
                    entry_id TEXT PRIMARY KEY,
                    package_id TEXT NOT NULL,
                    transition_seq INTEGER NOT NULL,
                    run_mode TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    session_date TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    cash_delta_inr REAL NOT NULL,
                    reserved_delta_inr REAL NOT NULL,
                    realized_equity_after_inr REAL NOT NULL,
                    reserved_after_inr REAL NOT NULL,
                    UNIQUE(package_id, event_type)
                );

                CREATE TABLE IF NOT EXISTS osv2_daily_snapshots (
                    run_mode TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    sod_equity_inr REAL NOT NULL,
                    eod_realized_equity_inr REAL NOT NULL,
                    eod_mtm_equity_inr REAL NOT NULL,
                    intraday_min_mtm_equity_inr REAL NOT NULL,
                    peak_reserved_capital_inr REAL NOT NULL,
                    closed_trades INTEGER NOT NULL,
                    open_overnight_packages INTEGER NOT NULL,
                    unresolved_exposure_count INTEGER NOT NULL,
                    net_pnl_inr REAL NOT NULL,
                    session_state TEXT NOT NULL DEFAULT 'PARTIAL_SESSION',
                    running_peak_equity_inr REAL NOT NULL DEFAULT 200000.0,
                    max_mtm_dd_inr REAL NOT NULL DEFAULT 0.0,
                    max_mtm_dd_pct_start REAL NOT NULL DEFAULT 0.0,
                    max_mtm_dd_pct_peak REAL NOT NULL DEFAULT 0.0,
                    gap_count INTEGER NOT NULL DEFAULT 0,
                    overflow_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(run_mode, config_hash, session_date)
                );

                CREATE TABLE IF NOT EXISTS osv2_completed_bars_1m (
                    run_mode TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    bar_open_ts TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    spot_o REAL NOT NULL,
                    spot_h REAL NOT NULL,
                    spot_l REAL NOT NULL,
                    spot_c REAL NOT NULL,
                    vix_c REAL,
                    bar_close_ts_ms INTEGER NOT NULL,
                    decision_ts_ms INTEGER NOT NULL,
                    PRIMARY KEY(run_mode, config_hash, bar_open_ts)
                );

                CREATE TABLE IF NOT EXISTS osv2_runtime_checkpoints (
                    run_mode TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    updated_ts_ms INTEGER NOT NULL,
                    daily_kill_reason TEXT,
                    cooldown_json TEXT NOT NULL DEFAULT '{}',
                    bar_locks_json TEXT NOT NULL DEFAULT '{}',
                    open_basis_ref_json TEXT,
                    last_event_ts_ms INTEGER NOT NULL DEFAULT 0,
                    last_bar_open_ts TEXT,
                    session_started_from_open INTEGER NOT NULL DEFAULT 0,
                    sequence_gap_count INTEGER NOT NULL DEFAULT 0,
                    overflow_drop_count INTEGER NOT NULL DEFAULT 0,
                    booking_fault_json TEXT,
                    running_peak_equity_inr REAL NOT NULL DEFAULT 200000.0,
                    max_mtm_dd_inr REAL NOT NULL DEFAULT 0.0,
                    intraday_min_mtm_equity_inr REAL NOT NULL DEFAULT 200000.0,
                    peak_reserved_inr REAL NOT NULL DEFAULT 0.0,
                    PRIMARY KEY(run_mode, config_hash, session_date)
                );

                CREATE TABLE IF NOT EXISTS osv2_equity_curve (
                    run_mode TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    seq_no INTEGER NOT NULL,
                    session_date TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    event_kind TEXT NOT NULL,
                    realized_equity_inr REAL NOT NULL,
                    conservative_mtm_inr REAL NOT NULL,
                    marked_equity_inr REAL NOT NULL,
                    running_peak_equity_inr REAL NOT NULL,
                    drawdown_inr REAL NOT NULL,
                    PRIMARY KEY(run_mode, config_hash, seq_no)
                );

                CREATE TABLE IF NOT EXISTS osv2_counterfactual_v1 (
                    cf_id TEXT PRIMARY KEY,
                    experiment_code TEXT NOT NULL,
                    v1_package_id TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    book_id TEXT NOT NULL,
                    v1_reported_net_inr REAL NOT NULL,
                    cf_status TEXT NOT NULL,
                    cf_exit_bar_ts TEXT,
                    cf_exit_reason TEXT,
                    cf_gross_pnl_inr REAL,
                    cf_charges_inr REAL,
                    cf_net_pnl_inr REAL,
                    included_in_v2_totals INTEGER NOT NULL DEFAULT 0 CHECK(included_in_v2_totals = 0),
                    detail_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS osv2_opportunities (
                    opportunity_id TEXT PRIMARY KEY,
                    run_mode TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    bar_ts TEXT NOT NULL,
                    decision_ts_ms INTEGER NOT NULL,
                    book_id TEXT NOT NULL,
                    candidate_variant TEXT NOT NULL,
                    direction TEXT,
                    decision_status TEXT NOT NULL,
                    primary_reject_reason TEXT,
                    failed_gates_json TEXT NOT NULL,
                    gate_inputs_json TEXT NOT NULL,
                    contract_identity_json TEXT,
                    quote_timestamps_json TEXT,
                    execution_availability TEXT NOT NULL,
                    is_breakout_bar INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_osv2_opp_mode_dt
                    ON osv2_opportunities(run_mode, config_hash, session_date, book_id);

                CREATE TABLE IF NOT EXISTS osv2_metric_corrections (
                    correction_id TEXT PRIMARY KEY,
                    correction_version TEXT NOT NULL,
                    run_mode TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    target_table TEXT NOT NULL,
                    session_date TEXT,
                    metric_name TEXT NOT NULL,
                    original_value_json TEXT NOT NULL,
                    corrected_value_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_ts_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS osv2_session_summaries (
                    session_date TEXT PRIMARY KEY,
                    rv_ann_pct REAL,
                    spot_close REAL,
                    vix_close REAL,
                    bars_count INTEGER NOT NULL DEFAULT 0,
                    updated_ts_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS osv2_operator_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    updated_ts_ms INTEGER NOT NULL
                );
                """
            )
            # Idempotent additive migrations for older V2 DBs
            _ensure_column(conn, "osv2_packages", "last_reliable_mark_net_inr", "REAL")
            _ensure_column(conn, "osv2_packages", "last_reliable_mark_ts_ms", "INTEGER")
            _ensure_column(conn, "osv2_packages", "events_timeline_json", "TEXT")
            _ensure_column(conn, "osv2_daily_snapshots", "session_state", "TEXT NOT NULL DEFAULT 'PARTIAL_SESSION'")
            _ensure_column(conn, "osv2_daily_snapshots", "running_peak_equity_inr", "REAL NOT NULL DEFAULT 200000.0")
            _ensure_column(conn, "osv2_daily_snapshots", "max_mtm_dd_inr", "REAL NOT NULL DEFAULT 0.0")
            _ensure_column(conn, "osv2_daily_snapshots", "max_mtm_dd_pct_start", "REAL NOT NULL DEFAULT 0.0")
            _ensure_column(conn, "osv2_daily_snapshots", "max_mtm_dd_pct_peak", "REAL NOT NULL DEFAULT 0.0")
            _ensure_column(conn, "osv2_daily_snapshots", "gap_count", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "osv2_daily_snapshots", "overflow_count", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "osv2_daily_snapshots", "observed_max_mtm_dd_inr", "REAL")
            _ensure_column(conn, "osv2_daily_snapshots", "stress_max_mtm_dd_inr", "REAL")
            _ensure_column(conn, "osv2_daily_snapshots", "observed_intraday_min_equity_inr", "REAL")
            _ensure_column(conn, "osv2_daily_snapshots", "stress_intraday_min_equity_inr", "REAL")
            _ensure_column(conn, "osv2_daily_snapshots", "open_basis_fail_summary_json", "TEXT")
            _ensure_column(conn, "osv2_runtime_checkpoints", "observed_running_peak_inr", "REAL")
            _ensure_column(conn, "osv2_runtime_checkpoints", "observed_max_mtm_dd_inr", "REAL")
            _ensure_column(conn, "osv2_runtime_checkpoints", "stress_max_mtm_dd_inr", "REAL")
            _ensure_column(conn, "osv2_runtime_checkpoints", "open_basis_fail_summary_json", "TEXT")
            _ensure_column(conn, "osv2_equity_curve", "observed_mtm_inr", "REAL")
            _ensure_column(conn, "osv2_equity_curve", "last_reliable_mtm_inr", "REAL")
            _ensure_column(conn, "osv2_equity_curve", "stress_mtm_inr", "REAL")
            _ensure_column(conn, "osv2_equity_curve", "observed_marked_equity_inr", "REAL")
            _ensure_column(conn, "osv2_equity_curve", "observed_running_peak_inr", "REAL")
            _ensure_column(conn, "osv2_equity_curve", "observed_drawdown_inr", "REAL")
            _ensure_column(conn, "osv2_equity_curve", "stress_marked_equity_inr", "REAL")
            _ensure_column(conn, "osv2_equity_curve", "stress_drawdown_inr", "REAL")
            _ensure_column(conn, "osv2_equity_curve", "unpriced_open_count", "INTEGER DEFAULT 0")
            return target
        finally:
            conn.close()


class V2LedgerStore:
    """
    Thread-safe SQLite repository enforcing thread-owned connections (`check_same_thread=True`),
    short transactions, cold-start non-creating reads, and serialized atomic writes.
    """

    def __init__(self, db_path: Optional[str] = None, auto_create: bool = False) -> None:
        self._write_lock = threading.RLock()
        self._lock = self._write_lock
        self._thread_local = threading.local()
        self.db_path = validate_v2_target_path(db_path)
        self._schema_ready: bool = False
        if auto_create or os.path.exists(self.db_path):
            self.ensure_initialized()

    def db_exists(self) -> bool:
        return os.path.exists(self.db_path)

    def ensure_initialized(self) -> str:
        with self._write_lock:
            self.db_path = validate_v2_target_path(self.db_path)
            if not self._schema_ready or not os.path.exists(self.db_path):
                init_v2_tables(self.db_path)
                self._schema_ready = True
            return self.db_path

    ensure_schema = ensure_initialized

    def connect(self) -> sqlite3.Connection:
        """Explicit thread-owned writable connection (initializes schema if needed)."""
        self.ensure_initialized()
        return get_v2_connection(self.db_path)

    def _connect_readonly_if_exists(self) -> Optional[sqlite3.Connection]:
        if not os.path.exists(self.db_path):
            return None
        return get_v2_readonly_connection(self.db_path)

    @contextmanager
    def writer_session(self) -> Iterator[None]:
        """
        Optional thread-local connection scope for single-thread replays so batched writes
        avoid open/close churn WITHOUT ever sharing a SQLite connection across threads.
        """
        self.ensure_initialized()
        prev = getattr(self._thread_local, "writer_conn", None)
        if prev is not None:
            yield
            return
        conn = get_v2_connection(self.db_path)
        self._thread_local.writer_conn = conn
        try:
            yield
        finally:
            self._thread_local.writer_conn = None
            conn.close()

    @contextmanager
    def _write_conn(self) -> Iterator[sqlite3.Connection]:
        """Yields a thread-owned read-write connection under `_write_lock` and closes non-scoped handles cleanly."""
        self.ensure_initialized()
        scoped = getattr(self._thread_local, "writer_conn", None)
        if scoped is not None:
            yield scoped
            return
        conn = get_v2_connection(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        """Closes any thread-local writer session handle on the calling thread."""
        scoped = getattr(self._thread_local, "writer_conn", None)
        if scoped is not None:
            self._thread_local.writer_conn = None
            scoped.close()

    def clear_run_mode(self, run_mode: str, config_hash: Optional[str] = None) -> None:
        """Clears only non-forward replay/test rows for deterministic re-runs."""
        if run_mode == "FORWARD_PAPER":
            raise ValueError("Refusing to bulk-clear live FORWARD_PAPER ledger rows.")
        with self._write_lock:
            if not os.path.exists(self.db_path):
                return
            with self._write_conn() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    for tbl in (
                        "osv2_events_ingest",
                        "osv2_fills",
                        "osv2_order_intents",
                        "osv2_ledger_entries",
                        "osv2_packages",
                        "osv2_signals",
                        "osv2_opportunities",
                        "osv2_metric_corrections",
                        "osv2_daily_snapshots",
                        "osv2_completed_bars_1m",
                        "osv2_runtime_checkpoints",
                        "osv2_equity_curve",
                    ):
                        if config_hash:
                            conn.execute(
                                f"DELETE FROM {tbl} WHERE run_mode = ? AND config_hash = ?;",
                                (run_mode, config_hash),
                            )
                        else:
                            conn.execute(f"DELETE FROM {tbl} WHERE run_mode = ?;", (run_mode,))
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

    def record_signal_idempotent(self, row: Dict[str, Any]) -> bool:
        with self._write_lock:
            with self._write_conn() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO osv2_signals (
                            signal_id, run_mode, config_hash, session_date, bar_ts, decision_ts_ms,
                            book_id, candidate_variant, direction, status, reject_reason,
                            gates_json, provenance_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            row["signal_id"],
                            row["run_mode"],
                            row["config_hash"],
                            row["session_date"],
                            row["bar_ts"],
                            int(row["decision_ts_ms"]),
                            row["book_id"],
                            row["candidate_variant"],
                            row["direction"],
                            row["status"],
                            row.get("reject_reason"),
                            json.dumps(row["gates"], sort_keys=True),
                            json.dumps(row["provenance"], sort_keys=True),
                        ),
                    )
                    conn.execute("COMMIT;")
                    return cur.rowcount > 0
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

    def record_opportunity_idempotent(self, row: Dict[str, Any]) -> bool:
        """Persists accepted or rejected gate evaluation with full inputs & quote provenance."""
        with self._write_lock:
            with self._write_conn() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO osv2_opportunities (
                            opportunity_id, run_mode, config_hash, protocol_version,
                            session_date, bar_ts, decision_ts_ms, book_id,
                            candidate_variant, direction, decision_status, primary_reject_reason,
                            failed_gates_json, gate_inputs_json, contract_identity_json,
                            quote_timestamps_json, execution_availability, is_breakout_bar
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            row["opportunity_id"],
                            row["run_mode"],
                            row["config_hash"],
                            str(row.get("protocol_version") or "2.2.0-causal"),
                            row["session_date"],
                            row["bar_ts"],
                            int(row["decision_ts_ms"]),
                            row["book_id"],
                            row["candidate_variant"],
                            row.get("direction"),
                            row["decision_status"],
                            row.get("primary_reject_reason"),
                            json.dumps(row.get("failed_gates") or [], sort_keys=True),
                            json.dumps(row.get("gate_inputs") or {}, sort_keys=True, default=str),
                            json.dumps(row.get("contract_identity"), sort_keys=True) if row.get("contract_identity") is not None else None,
                            json.dumps(row.get("quote_timestamps"), sort_keys=True) if row.get("quote_timestamps") is not None else None,
                            str(row.get("execution_availability") or "NOT_EVALUATED"),
                            int(bool(row.get("is_breakout_bar", False))),
                        ),
                    )
                    conn.execute("COMMIT;")
                    return cur.rowcount > 0
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

    def record_metric_correction_idempotent(self, corr: Dict[str, Any]) -> bool:
        """Persists a versioned, non-destructive metric correction audit record."""
        with self._write_lock:
            with self._write_conn() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO osv2_metric_corrections (
                            correction_id, correction_version, run_mode, config_hash,
                            target_table, session_date, metric_name, original_value_json,
                            corrected_value_json, reason, evidence_json, created_ts_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            corr["correction_id"],
                            corr["correction_version"],
                            corr["run_mode"],
                            corr["config_hash"],
                            corr["target_table"],
                            corr.get("session_date"),
                            corr["metric_name"],
                            json.dumps(corr["original_value"], sort_keys=True),
                            json.dumps(corr["corrected_value"], sort_keys=True),
                            corr["reason"],
                            json.dumps(corr.get("evidence") or {}, sort_keys=True),
                            int(corr["created_ts_ms"]),
                        ),
                    )
                    conn.execute("COMMIT;")
                    return cur.rowcount > 0
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

    @staticmethod
    def _insert_equity_point(conn: sqlite3.Connection, ep: Dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO osv2_equity_curve (
                run_mode, config_hash, seq_no, session_date, ts_ms, event_kind,
                realized_equity_inr, conservative_mtm_inr, marked_equity_inr,
                running_peak_equity_inr, drawdown_inr,
                observed_mtm_inr, last_reliable_mtm_inr, stress_mtm_inr,
                observed_marked_equity_inr, observed_running_peak_inr, observed_drawdown_inr,
                stress_marked_equity_inr, stress_drawdown_inr, unpriced_open_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                ep["run_mode"],
                ep["config_hash"],
                int(ep["seq_no"]),
                ep["session_date"],
                int(ep["ts_ms"]),
                ep["event_kind"],
                float(ep["realized_equity_inr"]),
                float(ep["conservative_mtm_inr"]),
                float(ep["marked_equity_inr"]),
                float(ep["running_peak_equity_inr"]),
                float(ep["drawdown_inr"]),
                float(ep["observed_mtm_inr"]) if ep.get("observed_mtm_inr") is not None else None,
                float(ep["last_reliable_mtm_inr"]) if ep.get("last_reliable_mtm_inr") is not None else None,
                float(ep["stress_mtm_inr"]) if ep.get("stress_mtm_inr") is not None else float(ep["conservative_mtm_inr"]),
                float(ep["observed_marked_equity_inr"]) if ep.get("observed_marked_equity_inr") is not None else None,
                float(ep["observed_running_peak_inr"]) if ep.get("observed_running_peak_inr") is not None else None,
                float(ep["observed_drawdown_inr"]) if ep.get("observed_drawdown_inr") is not None else None,
                float(ep["stress_marked_equity_inr"]) if ep.get("stress_marked_equity_inr") is not None else float(ep["marked_equity_inr"]),
                float(ep["stress_drawdown_inr"]) if ep.get("stress_drawdown_inr") is not None else float(ep["drawdown_inr"]),
                int(ep.get("unpriced_open_count") or 0),
            ),
        )

    def persist_ingest_events(self, envelopes: List[Dict[str, Any]], run_mode: str, decision_ts_ms: int) -> int:
        if not envelopes:
            return 0
        with self._write_lock:
            with self._write_conn() as conn:
                inserted = 0
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    for env in envelopes:
                        cur = conn.execute(
                            """
                            INSERT OR IGNORE INTO osv2_events_ingest (
                                event_dedup_key, run_mode, config_hash, instrument_key,
                                event_kind, exchange_ts_ms, receipt_ts_ms, decision_ts_ms,
                                sequence_flag, payload_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                env["dedup_key"],
                                run_mode,
                                env["config_hash"],
                                env["instrument_key"],
                                env["kind"],
                                int(env["exchange_ts_ms"]),
                                int(env["receipt_ts_ms"]),
                                int(decision_ts_ms),
                                env["sequence_flag"],
                                json.dumps(env["payload"], sort_keys=True, default=str),
                            ),
                        )
                        inserted += max(0, cur.rowcount)
                    conn.execute("COMMIT;")
                    return inserted
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

    def execute_atomic_transition(
        self,
        intents: List[Dict[str, Any]],
        fills: List[Dict[str, Any]],
        package_upsert: Optional[Dict[str, Any]],
        ledger_entry: Optional[Dict[str, Any]],
        equity_point: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Atomically commits intent + fill + package state + idempotent ledger entry + optional equity curve point
        inside a single `BEGIN IMMEDIATE` SQLite transaction. Returns False if ledger_entry already exists.
        """
        with self._write_lock:
            with self._write_conn() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    if ledger_entry is not None:
                        cur_le = conn.execute(
                            """
                            INSERT OR IGNORE INTO osv2_ledger_entries (
                                entry_id, package_id, transition_seq, run_mode, config_hash,
                                ts_ms, session_date, event_type, cash_delta_inr, reserved_delta_inr,
                                realized_equity_after_inr, reserved_after_inr
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                ledger_entry["entry_id"],
                                ledger_entry["package_id"],
                                int(ledger_entry["transition_seq"]),
                                ledger_entry["run_mode"],
                                ledger_entry["config_hash"],
                                int(ledger_entry["ts_ms"]),
                                ledger_entry["session_date"],
                                ledger_entry["event_type"],
                                float(ledger_entry["cash_delta_inr"]),
                                float(ledger_entry["reserved_delta_inr"]),
                                float(ledger_entry["realized_equity_after_inr"]),
                                float(ledger_entry["reserved_after_inr"]),
                            ),
                        )
                        if cur_le.rowcount == 0:
                            conn.execute("ROLLBACK;")
                            return False

                    for it in intents:
                        conn.execute(
                            """
                            INSERT INTO osv2_order_intents (
                                intent_id, signal_id, package_id, run_mode, config_hash, leg_role,
                                instrument_key, side, target_qty, limit_price, filled_qty,
                                state, reject_reason, created_ts_ms, updated_ts_ms
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(intent_id) DO UPDATE SET
                                filled_qty=excluded.filled_qty,
                                state=excluded.state,
                                reject_reason=excluded.reject_reason,
                                updated_ts_ms=excluded.updated_ts_ms;
                            """,
                            (
                                it["intent_id"],
                                it["signal_id"],
                                it["package_id"],
                                it["run_mode"],
                                it["config_hash"],
                                it["leg_role"],
                                it["instrument_key"],
                                it["side"],
                                int(it["target_qty"]),
                                it.get("limit_price"),
                                int(it.get("filled_qty") or 0),
                                it["state"],
                                it.get("reject_reason"),
                                int(it["created_ts_ms"]),
                                int(it["updated_ts_ms"]),
                            ),
                        )

                    for fl in fills:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO osv2_fills (
                                fill_id, intent_id, package_id, run_mode, config_hash,
                                instrument_key, side, qty, fill_price, raw_top_price, vwap_raw,
                                slippage_pts, fill_fidelity, exchange_ts_ms, receipt_ts_ms, decision_ts_ms
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                fl["fill_id"],
                                fl["intent_id"],
                                fl["package_id"],
                                fl["run_mode"],
                                fl["config_hash"],
                                fl["instrument_key"],
                                fl["side"],
                                int(fl["qty"]),
                                float(fl["fill_price"]),
                                float(fl["raw_top_price"]),
                                float(fl["vwap_raw"]),
                                float(fl["slippage_pts"]),
                                fl["fill_fidelity"],
                                int(fl["exchange_ts_ms"]),
                                int(fl["receipt_ts_ms"]),
                                int(fl["decision_ts_ms"]),
                            ),
                        )

                    if package_upsert is not None:
                        p = package_upsert
                        conn.execute(
                            """
                            INSERT INTO osv2_packages (
                                package_id, signal_id, run_mode, config_hash, book_id,
                                candidate_variant, session_date, expiry, state, fill_fidelity,
                                direction_label, entry_bar_ts, entry_ts_ms, leg2_confirmed_ts_ms,
                                exit_bar_ts, exit_ts_ms, exit_reason, legs_json, accounting_json,
                                reserved_capital_inr, structural_max_loss_inr, planned_loss_inr,
                                actual_gross_fill_loss_inr, slippage_and_gap_overshoot_inr,
                                gross_pnl_inr, brokerage_inr, stt_inr, statutory_charges_inr,
                                total_friction_inr, net_pnl_inr, entry_day_mtm_pnl_inr,
                                overnight_gap_pnl_inr, expiry_intraday_pnl_inr, worst_gap_loss_inr,
                                is_unpriced_exposure, unpriced_reason, unpriced_since_ts_ms,
                                last_mark_net_pnl_inr, last_mark_ts_ms,
                                last_reliable_mark_net_inr, last_reliable_mark_ts_ms,
                                events_timeline_json, transition_seq
                            ) VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                            )
                            ON CONFLICT(package_id) DO UPDATE SET
                                state=excluded.state,
                                leg2_confirmed_ts_ms=excluded.leg2_confirmed_ts_ms,
                                exit_bar_ts=excluded.exit_bar_ts,
                                exit_ts_ms=excluded.exit_ts_ms,
                                exit_reason=excluded.exit_reason,
                                legs_json=excluded.legs_json,
                                accounting_json=excluded.accounting_json,
                                reserved_capital_inr=excluded.reserved_capital_inr,
                                structural_max_loss_inr=excluded.structural_max_loss_inr,
                                planned_loss_inr=excluded.planned_loss_inr,
                                actual_gross_fill_loss_inr=excluded.actual_gross_fill_loss_inr,
                                slippage_and_gap_overshoot_inr=excluded.slippage_and_gap_overshoot_inr,
                                gross_pnl_inr=excluded.gross_pnl_inr,
                                brokerage_inr=excluded.brokerage_inr,
                                stt_inr=excluded.stt_inr,
                                statutory_charges_inr=excluded.statutory_charges_inr,
                                total_friction_inr=excluded.total_friction_inr,
                                net_pnl_inr=excluded.net_pnl_inr,
                                entry_day_mtm_pnl_inr=excluded.entry_day_mtm_pnl_inr,
                                overnight_gap_pnl_inr=excluded.overnight_gap_pnl_inr,
                                expiry_intraday_pnl_inr=excluded.expiry_intraday_pnl_inr,
                                worst_gap_loss_inr=excluded.worst_gap_loss_inr,
                                is_unpriced_exposure=excluded.is_unpriced_exposure,
                                unpriced_reason=excluded.unpriced_reason,
                                unpriced_since_ts_ms=excluded.unpriced_since_ts_ms,
                                last_mark_net_pnl_inr=excluded.last_mark_net_pnl_inr,
                                last_mark_ts_ms=excluded.last_mark_ts_ms,
                                last_reliable_mark_net_inr=excluded.last_reliable_mark_net_inr,
                                last_reliable_mark_ts_ms=excluded.last_reliable_mark_ts_ms,
                                events_timeline_json=excluded.events_timeline_json,
                                transition_seq=excluded.transition_seq;
                            """,
                            (
                                p["package_id"],
                                p["signal_id"],
                                p["run_mode"],
                                p["config_hash"],
                                p["book_id"],
                                p["candidate_variant"],
                                p["session_date"],
                                p["expiry"],
                                p["state"],
                                p["fill_fidelity"],
                                p["direction_label"],
                                p["entry_bar_ts"],
                                int(p["entry_ts_ms"]),
                                p.get("leg2_confirmed_ts_ms"),
                                p.get("exit_bar_ts"),
                                p.get("exit_ts_ms"),
                                p.get("exit_reason"),
                                json.dumps(p["legs_state"], sort_keys=True),
                                json.dumps(p.get("accounting"), sort_keys=True) if p.get("accounting") is not None else None,
                                float(p.get("reserved_capital_inr") or 0.0),
                                float(p.get("structural_max_loss_inr") or 0.0),
                                float(p.get("planned_loss_inr") or 0.0),
                                float(p.get("actual_gross_fill_loss_inr") or 0.0),
                                float(p.get("slippage_and_gap_overshoot_inr") or 0.0),
                                float(p.get("gross_pnl_inr") or 0.0),
                                float(p.get("brokerage_inr") or 0.0),
                                float(p.get("stt_inr") or 0.0),
                                float(p.get("statutory_charges_inr") or 0.0),
                                float(p.get("total_friction_inr") or 0.0),
                                float(p.get("net_pnl_inr") or 0.0),
                                float(p.get("entry_day_mtm_pnl_inr") or 0.0),
                                float(p.get("overnight_gap_pnl_inr") or 0.0),
                                float(p.get("expiry_intraday_pnl_inr") or 0.0),
                                float(p.get("worst_gap_loss_inr") or 0.0),
                                int(p.get("is_unpriced_exposure") or 0),
                                p.get("unpriced_reason"),
                                p.get("unpriced_since_ts_ms"),
                                p.get("last_mark_net_pnl_inr"),
                                p.get("last_mark_ts_ms"),
                                p.get("last_reliable_mark_net_inr"),
                                p.get("last_reliable_mark_ts_ms"),
                                json.dumps(p.get("events_timeline") or [], sort_keys=True),
                                int(p.get("transition_seq") or 0),
                            ),
                        )

                    if equity_point is not None:
                        self._insert_equity_point(conn, equity_point)
                    conn.execute("COMMIT;")
                    return True
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

    def append_equity_point(self, ep: Dict[str, Any]) -> None:
        with self._write_lock:
            with self._write_conn() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    self._insert_equity_point(conn, ep)
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

    def save_completed_bar(self, row: Dict[str, Any]) -> None:
        with self._write_lock:
            with self._write_conn() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO osv2_completed_bars_1m (
                            run_mode, config_hash, bar_open_ts, session_date,
                            spot_o, spot_h, spot_l, spot_c, vix_c, bar_close_ts_ms, decision_ts_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            row["run_mode"],
                            row["config_hash"],
                            row["bar_open_ts"],
                            row["session_date"],
                            float(row["spot_o"]),
                            float(row["spot_h"]),
                            float(row["spot_l"]),
                            float(row["spot_c"]),
                            float(row["vix_c"]) if row.get("vix_c") is not None else None,
                            int(row["bar_close_ts_ms"]),
                            int(row["decision_ts_ms"]),
                        ),
                    )
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

    def save_runtime_checkpoint(self, row: Dict[str, Any]) -> None:
        with self._write_lock:
            with self._write_conn() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO osv2_runtime_checkpoints (
                            run_mode, config_hash, session_date, updated_ts_ms,
                            daily_kill_reason, cooldown_json, bar_locks_json,
                            open_basis_ref_json, last_event_ts_ms, last_bar_open_ts,
                            session_started_from_open, sequence_gap_count, overflow_drop_count,
                            booking_fault_json, running_peak_equity_inr, max_mtm_dd_inr,
                            intraday_min_mtm_equity_inr, peak_reserved_inr,
                            observed_running_peak_inr, observed_max_mtm_dd_inr,
                            stress_max_mtm_dd_inr, open_basis_fail_summary_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            row["run_mode"],
                            row["config_hash"],
                            row["session_date"],
                            int(row["updated_ts_ms"]),
                            row.get("daily_kill_reason"),
                            json.dumps(row.get("cooldown") or {}, sort_keys=True),
                            json.dumps(row.get("bar_locks") or {}, sort_keys=True),
                            json.dumps(row["open_basis_ref"], sort_keys=True) if row.get("open_basis_ref") is not None else None,
                            int(row.get("last_event_ts_ms") or 0),
                            row.get("last_bar_open_ts"),
                            int(bool(row.get("session_started_from_open", False))),
                            int(row.get("sequence_gap_count") or 0),
                            int(row.get("overflow_drop_count") or 0),
                            json.dumps(row["booking_fault"], sort_keys=True) if row.get("booking_fault") is not None else None,
                            float(row.get("running_peak_equity_inr") or 200000.0),
                            float(row.get("max_mtm_dd_inr") or 0.0),
                            float(row.get("intraday_min_mtm_equity_inr") or 200000.0),
                            float(row.get("peak_reserved_inr") or 0.0),
                            float(row["observed_running_peak_inr"]) if row.get("observed_running_peak_inr") is not None else None,
                            float(row["observed_max_mtm_dd_inr"]) if row.get("observed_max_mtm_dd_inr") is not None else None,
                            float(row["stress_max_mtm_dd_inr"]) if row.get("stress_max_mtm_dd_inr") is not None else None,
                            json.dumps(row["open_basis_fail_summary"], sort_keys=True) if row.get("open_basis_fail_summary") is not None else None,
                        ),
                    )
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

    def upsert_daily_snapshot(self, row: Dict[str, Any]) -> None:
        with self._write_lock:
            with self._write_conn() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO osv2_daily_snapshots (
                            run_mode, config_hash, session_date, sod_equity_inr,
                            eod_realized_equity_inr, eod_mtm_equity_inr, intraday_min_mtm_equity_inr,
                            peak_reserved_capital_inr, closed_trades, open_overnight_packages,
                            unresolved_exposure_count, net_pnl_inr,
                            session_state, running_peak_equity_inr, max_mtm_dd_inr,
                            max_mtm_dd_pct_start, max_mtm_dd_pct_peak, gap_count, overflow_count,
                            observed_max_mtm_dd_inr, stress_max_mtm_dd_inr,
                            observed_intraday_min_equity_inr, stress_intraday_min_equity_inr,
                            open_basis_fail_summary_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            row["run_mode"],
                            row["config_hash"],
                            row["session_date"],
                            float(row["sod_equity_inr"]),
                            float(row["eod_realized_equity_inr"]),
                            float(row["eod_mtm_equity_inr"]),
                            float(row["intraday_min_mtm_equity_inr"]),
                            float(row["peak_reserved_capital_inr"]),
                            int(row["closed_trades"]),
                            int(row["open_overnight_packages"]),
                            int(row["unresolved_exposure_count"]),
                            float(row["net_pnl_inr"]),
                            str(row.get("session_state") or "PARTIAL_SESSION"),
                            float(row.get("running_peak_equity_inr") or row["eod_mtm_equity_inr"]),
                            float(row.get("max_mtm_dd_inr") or 0.0),
                            float(row.get("max_mtm_dd_pct_start") or 0.0),
                            float(row.get("max_mtm_dd_pct_peak") or 0.0),
                            int(row.get("gap_count") or 0),
                            int(row.get("overflow_count") or 0),
                            float(row["observed_max_mtm_dd_inr"]) if row.get("observed_max_mtm_dd_inr") is not None else None,
                            float(row["stress_max_mtm_dd_inr"]) if row.get("stress_max_mtm_dd_inr") is not None else None,
                            float(row["observed_intraday_min_equity_inr"]) if row.get("observed_intraday_min_equity_inr") is not None else None,
                            float(row["stress_intraday_min_equity_inr"]) if row.get("stress_intraday_min_equity_inr") is not None else None,
                            json.dumps(row["open_basis_fail_summary"], sort_keys=True) if row.get("open_basis_fail_summary") is not None else None,
                        ),
                    )
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

    def replace_counterfactuals(self, rows: List[Dict[str, Any]]) -> None:
        with self._write_lock:
            with self._write_conn() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    conn.execute("DELETE FROM osv2_counterfactual_v1;")
                    for r in rows:
                        conn.execute(
                            """
                            INSERT INTO osv2_counterfactual_v1 (
                                cf_id, experiment_code, v1_package_id, session_date, book_id,
                                v1_reported_net_inr, cf_status, cf_exit_bar_ts, cf_exit_reason,
                                cf_gross_pnl_inr, cf_charges_inr, cf_net_pnl_inr,
                                included_in_v2_totals, detail_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?);
                            """,
                            (
                                r["cf_id"],
                                r["experiment_code"],
                                r["v1_package_id"],
                                r["session_date"],
                                r["book_id"],
                                float(r["v1_reported_net_inr"]),
                                r["cf_status"],
                                r.get("cf_exit_bar_ts"),
                                r.get("cf_exit_reason"),
                                r.get("cf_gross_pnl_inr"),
                                r.get("cf_charges_inr"),
                                r.get("cf_net_pnl_inr"),
                                json.dumps(r.get("detail") or {}, sort_keys=True),
                            ),
                        )
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

    # ── COLD-START SAFE READ-ONLY QUERIES (NEVER CREATE DB FILE) ─────────────

    def load_packages(
        self,
        run_mode: Optional[str] = None,
        config_hash: Optional[str] = None,
        states: Optional[Tuple[str, ...]] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return []
        try:
            clauses: List[str] = []
            params: List[Any] = []
            if run_mode:
                clauses.append("run_mode = ?")
                params.append(run_mode)
            if config_hash:
                clauses.append("config_hash = ?")
                params.append(config_hash)
            if states:
                ph = ",".join("?" for _ in states)
                clauses.append(f"state IN ({ph})")
                params.extend(states)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM osv2_packages {where} ORDER BY entry_bar_ts ASC, package_id ASC;",
                params,
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["legs_state"] = json.loads(d.pop("legs_json") or "{}")
                d["accounting"] = json.loads(d["accounting_json"]) if d.get("accounting_json") else None
                d["events_timeline"] = json.loads(d.pop("events_timeline_json") or "[]")
                out.append(d)
            return out
        finally:
            conn.close()

    def load_ledger_entries(self, run_mode: Optional[str] = None, config_hash: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return []
        try:
            clauses: List[str] = []
            params: List[Any] = []
            if run_mode:
                clauses.append("run_mode = ?")
                params.append(run_mode)
            if config_hash:
                clauses.append("config_hash = ?")
                params.append(config_hash)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM osv2_ledger_entries {where} ORDER BY ts_ms ASC, package_id ASC, transition_seq ASC;",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def load_daily_snapshots(
        self,
        run_mode: Optional[str] = None,
        config_hash: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return []
        try:
            clauses: List[str] = []
            params: List[Any] = []
            if run_mode:
                clauses.append("run_mode = ?")
                params.append(run_mode)
            if config_hash:
                clauses.append("config_hash = ?")
                params.append(config_hash)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM osv2_daily_snapshots {where} ORDER BY session_date ASC;",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def load_completed_bars(
        self,
        run_mode: str,
        config_hash: str,
        session_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return []
        try:
            if session_date:
                rows = conn.execute(
                    "SELECT * FROM osv2_completed_bars_1m WHERE run_mode = ? AND config_hash = ? AND session_date = ? ORDER BY bar_open_ts ASC;",
                    (run_mode, config_hash, session_date),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM osv2_completed_bars_1m WHERE run_mode = ? AND config_hash = ? ORDER BY bar_open_ts ASC;",
                    (run_mode, config_hash),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def load_runtime_checkpoints(
        self,
        run_mode: str,
        config_hash: str,
    ) -> List[Dict[str, Any]]:
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT * FROM osv2_runtime_checkpoints WHERE run_mode = ? AND config_hash = ? ORDER BY session_date ASC;",
                (run_mode, config_hash),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["cooldown"] = json.loads(d.pop("cooldown_json") or "{}")
                d["bar_locks"] = json.loads(d.pop("bar_locks_json") or "{}")
                d["open_basis_ref"] = json.loads(d["open_basis_ref_json"]) if d.get("open_basis_ref_json") else None
                d["booking_fault"] = json.loads(d["booking_fault_json"]) if d.get("booking_fault_json") else None
                d["open_basis_fail_summary"] = json.loads(d["open_basis_fail_summary_json"]) if d.get("open_basis_fail_summary_json") else None
                out.append(d)
            return out
        finally:
            conn.close()

    def load_opportunities(
        self,
        run_mode: Optional[str] = None,
        config_hash: Optional[str] = None,
        session_date: Optional[str] = None,
        book_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return []
        try:
            tbl = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='osv2_opportunities';"
            ).fetchone()
            if not tbl:
                return []
            clauses: List[str] = []
            params: List[Any] = []
            if run_mode:
                clauses.append("run_mode = ?")
                params.append(run_mode)
            if config_hash:
                clauses.append("config_hash = ?")
                params.append(config_hash)
            if session_date:
                clauses.append("session_date = ?")
                params.append(session_date)
            if book_id:
                clauses.append("book_id = ?")
                params.append(book_id)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM osv2_opportunities {where} ORDER BY decision_ts_ms DESC, opportunity_id ASC LIMIT ?;",
                params + [int(limit)],
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["failed_gates"] = json.loads(d.pop("failed_gates_json") or "[]")
                d["gate_inputs"] = json.loads(d.pop("gate_inputs_json") or "{}")
                d["contract_identity"] = json.loads(d.pop("contract_identity_json") or "null")
                d["quote_timestamps"] = json.loads(d.pop("quote_timestamps_json") or "null")
                out.append(d)
            return out
        finally:
            conn.close()

    def load_metric_corrections(
        self,
        run_mode: Optional[str] = None,
        config_hash: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return []
        try:
            tbl = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='osv2_metric_corrections';"
            ).fetchone()
            if not tbl:
                return []
            clauses: List[str] = []
            params: List[Any] = []
            if run_mode:
                clauses.append("run_mode = ?")
                params.append(run_mode)
            if config_hash:
                clauses.append("config_hash = ?")
                params.append(config_hash)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM osv2_metric_corrections {where} ORDER BY created_ts_ms ASC, correction_id ASC;",
                params,
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["original_value"] = json.loads(d.pop("original_value_json") or "null")
                d["corrected_value"] = json.loads(d.pop("corrected_value_json") or "null")
                d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
                out.append(d)
            return out
        finally:
            conn.close()

    def load_equity_curve(
        self,
        run_mode: str,
        config_hash: str,
    ) -> List[Dict[str, Any]]:
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT * FROM osv2_equity_curve WHERE run_mode = ? AND config_hash = ? ORDER BY seq_no ASC;",
                (run_mode, config_hash),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def load_counterfactuals(self, experiment_code: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return []
        try:
            if experiment_code:
                rows = conn.execute(
                    "SELECT * FROM osv2_counterfactual_v1 WHERE experiment_code = ? ORDER BY session_date ASC, v1_package_id ASC;",
                    (experiment_code,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM osv2_counterfactual_v1 ORDER BY experiment_code ASC, session_date ASC, v1_package_id ASC;"
                ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["detail"] = json.loads(d.pop("detail_json") or "{}")
                out.append(d)
            return out
        finally:
            conn.close()

    def load_signals(
        self,
        run_mode: Optional[str] = None,
        config_hash: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return []
        try:
            clauses: List[str] = []
            params: List[Any] = []
            if run_mode:
                clauses.append("run_mode = ?")
                params.append(run_mode)
            if config_hash:
                clauses.append("config_hash = ?")
                params.append(config_hash)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM osv2_signals {where} ORDER BY bar_ts DESC, signal_id ASC LIMIT ?;",
                params + [int(limit)],
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["gates"] = json.loads(d.pop("gates_json") or "{}")
                d["provenance"] = json.loads(d.pop("provenance_json") or "{}")
                out.append(d)
            return out
        finally:
            conn.close()

    def load_fills(
        self,
        run_mode: Optional[str] = None,
        config_hash: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return []
        try:
            clauses: List[str] = []
            params: List[Any] = []
            if run_mode:
                clauses.append("run_mode = ?")
                params.append(run_mode)
            if config_hash:
                clauses.append("config_hash = ?")
                params.append(config_hash)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM osv2_fills {where} ORDER BY decision_ts_ms ASC, fill_id ASC;",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def compute_canonical_ledger_digest(self, run_mode: str, config_hash: str) -> Optional[str]:
        """Deterministic SHA-256 of packages + ledger_entries + fills + daily_snapshots; returns None if DB absent."""
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return None
        try:
            pkgs = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT package_id, signal_id, book_id, candidate_variant, session_date, expiry,
                           state, fill_fidelity, direction_label, entry_bar_ts, exit_bar_ts, exit_reason,
                           reserved_capital_inr, structural_max_loss_inr, planned_loss_inr,
                           actual_gross_fill_loss_inr, slippage_and_gap_overshoot_inr,
                           gross_pnl_inr, brokerage_inr, stt_inr, statutory_charges_inr,
                           total_friction_inr, net_pnl_inr, entry_day_mtm_pnl_inr,
                           overnight_gap_pnl_inr, expiry_intraday_pnl_inr, worst_gap_loss_inr,
                           is_unpriced_exposure, transition_seq
                    FROM osv2_packages
                    WHERE run_mode = ? AND config_hash = ?
                    ORDER BY package_id ASC;
                    """,
                    (run_mode, config_hash),
                ).fetchall()
            ]
            entries = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT entry_id, package_id, transition_seq, session_date, event_type,
                           cash_delta_inr, reserved_delta_inr, realized_equity_after_inr, reserved_after_inr
                    FROM osv2_ledger_entries
                    WHERE run_mode = ? AND config_hash = ?
                    ORDER BY entry_id ASC;
                    """,
                    (run_mode, config_hash),
                ).fetchall()
            ]
            fills = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT fill_id, intent_id, package_id, instrument_key, side, qty, fill_price, fill_fidelity
                    FROM osv2_fills
                    WHERE run_mode = ? AND config_hash = ?
                    ORDER BY fill_id ASC;
                    """,
                    (run_mode, config_hash),
                ).fetchall()
            ]
            snaps = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT session_date, sod_equity_inr, eod_realized_equity_inr, eod_mtm_equity_inr,
                           intraday_min_mtm_equity_inr, peak_reserved_capital_inr, closed_trades,
                           open_overnight_packages, unresolved_exposure_count, net_pnl_inr,
                           session_state, running_peak_equity_inr, max_mtm_dd_inr
                    FROM osv2_daily_snapshots
                    WHERE run_mode = ? AND config_hash = ?
                    ORDER BY session_date ASC;
                    """,
                    (run_mode, config_hash),
                ).fetchall()
            ]
            blob = json.dumps(
                {"packages": pkgs, "ledger_entries": entries, "fills": fills, "daily_snapshots": snaps},
                sort_keys=True,
                separators=(",", ":"),
            )
            return hashlib.sha256(blob.encode("utf-8")).hexdigest()
        finally:
            conn.close()

    def get_prior_session_summaries(self, before_session_date: str) -> List[Dict[str, Any]]:
        """Read-only lookup of completed prior sessions strictly `< before_session_date`."""
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return []
        try:
            tbl = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='osv2_session_summaries';"
            ).fetchone()
            if tbl is None:
                return []
            rows = conn.execute(
                """
                SELECT session_date, rv_ann_pct, spot_close, vix_close, bars_count, updated_ts_ms
                FROM osv2_session_summaries
                WHERE session_date < ?
                ORDER BY session_date ASC;
                """,
                (str(before_session_date),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def upsert_session_summary(
        self,
        *,
        session_date: str,
        rv_ann_pct: Optional[float],
        spot_close: Optional[float],
        vix_close: Optional[float],
        bars_count: int,
        updated_ts_ms: int,
    ) -> None:
        with self._write_lock:
            with self._write_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO osv2_session_summaries (
                        session_date, rv_ann_pct, spot_close, vix_close, bars_count, updated_ts_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_date) DO UPDATE SET
                        rv_ann_pct = COALESCE(excluded.rv_ann_pct, osv2_session_summaries.rv_ann_pct),
                        spot_close = COALESCE(excluded.spot_close, osv2_session_summaries.spot_close),
                        vix_close = COALESCE(excluded.vix_close, osv2_session_summaries.vix_close),
                        bars_count = MAX(osv2_session_summaries.bars_count, excluded.bars_count),
                        updated_ts_ms = excluded.updated_ts_ms;
                    """,
                    (
                        str(session_date)[:10],
                        float(rv_ann_pct) if rv_ann_pct is not None else None,
                        float(spot_close) if spot_close is not None else None,
                        float(vix_close) if vix_close is not None else None,
                        int(bars_count),
                        int(updated_ts_ms),
                    ),
                )
                conn.commit()

    def get_operator_settings(self) -> Dict[str, Any]:
        """Read-only lookup of persisted V2 operator settings; defaults without creating DB."""
        defaults: Dict[str, Any] = {"paper_execution_mode": "AUTO"}
        conn = self._connect_readonly_if_exists()
        if conn is None:
            return defaults
        try:
            tbl = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='osv2_operator_settings';"
            ).fetchone()
            if tbl is None:
                return defaults
            rows = conn.execute(
                "SELECT setting_key, setting_value FROM osv2_operator_settings;"
            ).fetchall()
            out = dict(defaults)
            for r in rows:
                out[str(r["setting_key"])] = str(r["setting_value"])
            return out
        finally:
            conn.close()

    def set_operator_setting(self, setting_key: str, setting_value: str, updated_ts_ms: int) -> None:
        with self._write_lock:
            with self._write_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO osv2_operator_settings (setting_key, setting_value, updated_ts_ms)
                    VALUES (?, ?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET
                        setting_value = excluded.setting_value,
                        updated_ts_ms = excluded.updated_ts_ms;
                    """,
                    (str(setting_key), str(setting_value), int(updated_ts_ms)),
                )
                conn.commit()
