"""Isolated SQLite ledger store for Option Sniffer Advance V2 Live Execution.

Invariants:
1. Dedicated SQLite database (`backend/data/option_sniffer_advance_v2_live.db` or test override).
2. Strictly refuses opening V1 `market_data.db`, V2 paper `option_sniffer_advance_v2.db`,
   or any shared canonical database.
3. Cold-start safe reads (`auto_create=False`): GET requests never create a new DB file.
4. WAL mode + `busy_timeout=5000` + `BEGIN IMMEDIATE` serialized writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

_IST = timezone(timedelta(hours=5, minutes=30))
_TEST_DB_OVERRIDE_LOCK = threading.RLock()
_TEST_DB_OVERRIDE: Optional[str] = None

FORBIDDEN_DB_BASENAMES = {
    "market_data.db",
    "option_sniffer_advance_v2.db",
    "candle_data.db",
    "trading_app.db",
}


class V2LiveDatabaseIsolationError(RuntimeError):
    """Raised when V2 live ledger attempts to access a forbidden V1 or V2 paper DB."""


def now_ist_iso() -> str:
    return datetime.now(_IST).isoformat(timespec="seconds")


def now_ist_date() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d")


def _backend_dir() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def get_default_v2_live_db_path() -> str:
    data_dir = os.path.join(_backend_dir(), "data")
    return os.path.join(data_dir, "option_sniffer_advance_v2_live.db")


def set_v2_live_test_db_override(db_path: Optional[str]) -> None:
    global _TEST_DB_OVERRIDE
    with _TEST_DB_OVERRIDE_LOCK:
        if db_path is None:
            _TEST_DB_OVERRIDE = None
        else:
            validate_v2_live_db_path(db_path)
            _TEST_DB_OVERRIDE = os.path.abspath(db_path)


def resolve_v2_live_db_path(explicit_path: Optional[str] = None) -> str:
    if explicit_path:
        return validate_v2_live_db_path(explicit_path)
    with _TEST_DB_OVERRIDE_LOCK:
        if _TEST_DB_OVERRIDE:
            return validate_v2_live_db_path(_TEST_DB_OVERRIDE)
    return validate_v2_live_db_path(get_default_v2_live_db_path())


def validate_v2_live_db_path(db_path: str) -> str:
    if not db_path or db_path == ":memory:":
        raise V2LiveDatabaseIsolationError("In-memory or empty DB path is not permitted for durable V2 live ledger.")
    abs_target = os.path.realpath(os.path.abspath(db_path))
    base_target = os.path.basename(abs_target).lower()
    if base_target in FORBIDDEN_DB_BASENAMES:
        raise V2LiveDatabaseIsolationError(
            f"Refusing to open protected non-live-V2 database basename '{base_target}' ({abs_target})."
        )
    data_dir = os.path.realpath(os.path.join(_backend_dir(), "data"))
    for forbidden_name in FORBIDDEN_DB_BASENAMES:
        forbidden_real = os.path.realpath(os.path.join(data_dir, forbidden_name))
        if abs_target == forbidden_real:
            raise V2LiveDatabaseIsolationError(
                f"Refusing to open protected database path: {abs_target}"
            )
    try:
        from app.analytics.option_sniffer_v2.v2_ledger_db import _DEFAULT_V2_DB_PATH, get_canonical_db_path

        for p_raw in (get_canonical_db_path(), _DEFAULT_V2_DB_PATH):
            p = os.path.realpath(os.path.abspath(p_raw))
            if abs_target == p:
                raise V2LiveDatabaseIsolationError(
                    f"Refusing to open shared V1/V2 paper DB from V2 live layer: {abs_target}"
                )
    except V2LiveDatabaseIsolationError:
        raise
    except Exception:
        pass
    return os.path.abspath(db_path)


def make_deterministic_intent_id(
    signal_id: str,
    book_id: str,
    leg_role: str,
    action: str,
    session_date: str,
) -> str:
    raw = f"{signal_id}|{book_id}|{leg_role}|{action}|{session_date}".encode("utf-8")
    return "osv2i_" + hashlib.sha256(raw).hexdigest()[:20]


class V2LiveLedgerStore:
    """Durable SQLite store for V2 isolated live arm state, intents, packages, and audit logs."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = resolve_v2_live_db_path(db_path)
        self._lock = threading.RLock()
        self._initialized = False

    def db_exists(self) -> bool:
        return os.path.exists(self.db_path)

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        validate_v2_live_db_path(self.db_path)
        if not readonly:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        if readonly:
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON;")
            conn.execute("PRAGMA busy_timeout=5000;")
            return conn
        conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def ensure_initialized(self) -> None:
        with self._lock:
            if self._initialized and os.path.exists(self.db_path):
                return
            conn = self._connect(readonly=False)
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS osv2_live_arm_state (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        armed INTEGER NOT NULL DEFAULT 0,
                        armed_at_iso TEXT,
                        armed_for_date TEXT,
                        selected_books_json TEXT NOT NULL DEFAULT '[]',
                        protocol_profile_id TEXT,
                        max_lots_per_book INTEGER NOT NULL DEFAULT 1,
                        max_daily_loss_inr REAL NOT NULL DEFAULT 2500.0,
                        max_open_packages INTEGER NOT NULL DEFAULT 1,
                        disarmed_at_iso TEXT,
                        disarm_reason TEXT,
                        updated_at_iso TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS osv2_live_intents (
                        intent_id TEXT PRIMARY KEY,
                        signal_id TEXT NOT NULL,
                        live_package_id TEXT NOT NULL,
                        book_id TEXT NOT NULL,
                        leg_role TEXT NOT NULL,
                        action TEXT NOT NULL,
                        session_date TEXT NOT NULL,
                        instrument_key TEXT NOT NULL,
                        trading_symbol TEXT NOT NULL,
                        side TEXT NOT NULL,
                        order_type TEXT NOT NULL,
                        product TEXT NOT NULL,
                        requested_qty INTEGER NOT NULL,
                        filled_qty INTEGER NOT NULL DEFAULT 0,
                        avg_fill_price REAL,
                        limit_price REAL,
                        trigger_price REAL,
                        market_protection REAL,
                        tag TEXT NOT NULL,
                        broker_order_id TEXT,
                        broker_gtt_id TEXT,
                        state TEXT NOT NULL,
                        error_detail TEXT,
                        created_ts_ms INTEGER NOT NULL,
                        updated_ts_ms INTEGER NOT NULL,
                        raw_response_json TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_osv2_live_intents_signal
                        ON osv2_live_intents(signal_id);
                    CREATE INDEX IF NOT EXISTS idx_osv2_live_intents_pkg
                        ON osv2_live_intents(live_package_id);
                    CREATE INDEX IF NOT EXISTS idx_osv2_live_intents_state
                        ON osv2_live_intents(state);

                    CREATE TABLE IF NOT EXISTS osv2_live_packages (
                        live_package_id TEXT PRIMARY KEY,
                        signal_id TEXT NOT NULL UNIQUE,
                        session_date TEXT NOT NULL,
                        book_id TEXT NOT NULL,
                        protocol_profile_id TEXT NOT NULL,
                        structure TEXT NOT NULL,
                        state TEXT NOT NULL,
                        entry_ts_ms INTEGER NOT NULL,
                        exit_ts_ms INTEGER,
                        exit_reason TEXT,
                        legs_json TEXT NOT NULL,
                        realized_gross_inr REAL DEFAULT 0.0,
                        realized_friction_inr REAL DEFAULT 0.0,
                        realized_net_inr REAL DEFAULT 0.0,
                        reconciled_at_iso TEXT,
                        unresolved_fault TEXT,
                        updated_at_iso TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_osv2_live_packages_state
                        ON osv2_live_packages(state);

                    CREATE TABLE IF NOT EXISTS osv2_live_audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts_iso TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        detail_json TEXT NOT NULL
                    );
                    """
                )
                conn.execute("BEGIN IMMEDIATE;")
                row = conn.execute("SELECT id FROM osv2_live_arm_state WHERE id=1;").fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO osv2_live_arm_state (
                            id, armed, armed_at_iso, armed_for_date, selected_books_json,
                            protocol_profile_id, max_lots_per_book, max_daily_loss_inr,
                            max_open_packages, disarmed_at_iso, disarm_reason, updated_at_iso
                        ) VALUES (1, 0, NULL, NULL, '[]', 'V2_BOOK_B_FROZEN_PAPER', 1, 2500.0, 1, ?, 'INITIAL_BOOT_DISARMED', ?);
                        """,
                        (now_ist_iso(), now_ist_iso()),
                    )
                conn.execute("COMMIT;")
                self._initialized = True
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        self.ensure_initialized()
        with self._lock:
            conn = self._connect(readonly=False)
            try:
                conn.execute("BEGIN IMMEDIATE;")
                yield conn
                conn.execute("COMMIT;")
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def force_boot_disarm_if_exists(self) -> bool:
        """If DB exists from a prior process, reset armed=0 on boot without creating a cold DB."""
        if not self.db_exists():
            return False
        ts = now_ist_iso()
        with self.write_tx() as conn:
            row = conn.execute("SELECT armed FROM osv2_live_arm_state WHERE id=1;").fetchone()
            was_armed = bool(row and int(row["armed"] or 0) == 1)
            conn.execute(
                """
                UPDATE osv2_live_arm_state
                SET armed=0, disarmed_at_iso=?, disarm_reason='PROCESS_BOOT_FAILSAFE_DISARM', updated_at_iso=?
                WHERE id=1;
                """,
                (ts, ts),
            )
            self._append_audit_in_tx(
                conn,
                "SYSTEM_BOOT_DISARMED",
                "SYSTEM",
                {"was_armed_prior_to_restart": was_armed, "ts_iso": ts},
            )
        return True

    def _append_audit_in_tx(
        self, conn: sqlite3.Connection, event_type: str, actor: str, detail: Dict[str, Any]
    ) -> None:
        conn.execute(
            "INSERT INTO osv2_live_audit_log (ts_iso, event_type, actor, detail_json) VALUES (?, ?, ?, ?);",
            (now_ist_iso(), str(event_type), str(actor), json.dumps(detail, sort_keys=True)),
        )

    def append_audit(self, event_type: str, actor: str, detail: Dict[str, Any]) -> None:
        with self.write_tx() as conn:
            self._append_audit_in_tx(conn, event_type, actor, detail)

    def get_arm_state(self) -> Dict[str, Any]:
        if not self.db_exists():
            return {
                "db_exists": False,
                "armed": False,
                "armed_at_iso": None,
                "armed_for_date": None,
                "selected_books": [],
                "protocol_profile_id": "V2_BOOK_B_FROZEN_PAPER",
                "max_lots_per_book": 1,
                "max_daily_loss_inr": 2500.0,
                "max_open_packages": 1,
                "disarmed_at_iso": None,
                "disarm_reason": "COLD_NOT_INITIALIZED_DISARMED",
                "updated_at_iso": None,
            }
        conn = self._connect(readonly=True)
        try:
            row = conn.execute("SELECT * FROM osv2_live_arm_state WHERE id=1;").fetchone()
            if not row:
                return {
                    "db_exists": True,
                    "armed": False,
                    "selected_books": [],
                    "protocol_profile_id": "V2_BOOK_B_FROZEN_PAPER",
                    "max_lots_per_book": 1,
                    "max_daily_loss_inr": 2500.0,
                    "max_open_packages": 1,
                    "disarm_reason": "MISSING_ROW_DISARMED",
                }
            return {
                "db_exists": True,
                "armed": bool(int(row["armed"] or 0)),
                "armed_at_iso": row["armed_at_iso"],
                "armed_for_date": row["armed_for_date"],
                "selected_books": json.loads(row["selected_books_json"] or "[]"),
                "protocol_profile_id": row["protocol_profile_id"],
                "max_lots_per_book": int(row["max_lots_per_book"] or 1),
                "max_daily_loss_inr": float(row["max_daily_loss_inr"] or 2500.0),
                "max_open_packages": int(row["max_open_packages"] or 1),
                "disarmed_at_iso": row["disarmed_at_iso"],
                "disarm_reason": row["disarm_reason"],
                "updated_at_iso": row["updated_at_iso"],
            }
        finally:
            conn.close()

    def set_arm_state(
        self,
        *,
        armed: bool,
        selected_books: List[str],
        protocol_profile_id: str,
        max_lots_per_book: int,
        max_daily_loss_inr: float,
        max_open_packages: int,
        reason: str,
        actor: str,
    ) -> Dict[str, Any]:
        ts = now_ist_iso()
        today = now_ist_date()
        with self.write_tx() as conn:
            conn.execute(
                """
                UPDATE osv2_live_arm_state
                SET armed=?,
                    armed_at_iso=CASE WHEN ?=1 THEN ? ELSE armed_at_iso END,
                    armed_for_date=CASE WHEN ?=1 THEN ? ELSE armed_for_date END,
                    selected_books_json=?,
                    protocol_profile_id=?,
                    max_lots_per_book=?,
                    max_daily_loss_inr=?,
                    max_open_packages=?,
                    disarmed_at_iso=CASE WHEN ?=0 THEN ? ELSE disarmed_at_iso END,
                    disarm_reason=CASE WHEN ?=0 THEN ? ELSE NULL END,
                    updated_at_iso=?
                WHERE id=1;
                """,
                (
                    1 if armed else 0,
                    1 if armed else 0,
                    ts,
                    1 if armed else 0,
                    today,
                    json.dumps(list(selected_books)),
                    protocol_profile_id,
                    int(max_lots_per_book),
                    float(max_daily_loss_inr),
                    int(max_open_packages),
                    1 if armed else 0,
                    ts,
                    1 if armed else 0,
                    reason,
                    ts,
                ),
            )
            self._append_audit_in_tx(
                conn,
                "LIVE_ARMED" if armed else "LIVE_DISARMED",
                actor,
                {
                    "selected_books": selected_books,
                    "protocol_profile_id": protocol_profile_id,
                    "max_lots_per_book": max_lots_per_book,
                    "max_daily_loss_inr": max_daily_loss_inr,
                    "max_open_packages": max_open_packages,
                    "reason": reason,
                },
            )
        return self.get_arm_state()

    def get_intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        if not self.db_exists():
            return None
        conn = self._connect(readonly=True)
        try:
            row = conn.execute("SELECT * FROM osv2_live_intents WHERE intent_id=?;", (intent_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_intents(
        self,
        *,
        live_package_id: Optional[str] = None,
        states: Optional[List[str]] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        if not self.db_exists():
            return []
        conn = self._connect(readonly=True)
        try:
            sql = "SELECT * FROM osv2_live_intents WHERE 1=1"
            params: List[Any] = []
            if live_package_id:
                sql += " AND live_package_id=?"
                params.append(live_package_id)
            if states:
                ph = ",".join("?" for _ in states)
                sql += f" AND state IN ({ph})"
                params.extend(states)
            sql += " ORDER BY created_ts_ms DESC LIMIT ?;"
            params.append(int(limit))
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def create_or_get_intent(self, intent: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
        """Atomically creates intent if absent, or returns existing intent (idempotency)."""
        with self.write_tx() as conn:
            existing = conn.execute(
                "SELECT * FROM osv2_live_intents WHERE intent_id=?;",
                (intent["intent_id"],),
            ).fetchone()
            if existing is not None:
                return dict(existing), False
            conn.execute(
                """
                INSERT INTO osv2_live_intents (
                    intent_id, signal_id, live_package_id, book_id, leg_role, action,
                    session_date, instrument_key, trading_symbol, side, order_type, product,
                    requested_qty, filled_qty, avg_fill_price, limit_price, trigger_price,
                    market_protection, tag, broker_order_id, broker_gtt_id, state, error_detail,
                    created_ts_ms, updated_ts_ms, raw_response_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    intent["intent_id"],
                    intent["signal_id"],
                    intent["live_package_id"],
                    intent["book_id"],
                    intent["leg_role"],
                    intent["action"],
                    intent["session_date"],
                    intent["instrument_key"],
                    intent["trading_symbol"],
                    intent["side"],
                    intent["order_type"],
                    intent.get("product", "D"),
                    int(intent["requested_qty"]),
                    int(intent.get("filled_qty", 0)),
                    intent.get("avg_fill_price"),
                    intent.get("limit_price"),
                    intent.get("trigger_price"),
                    intent.get("market_protection"),
                    intent["tag"],
                    intent.get("broker_order_id"),
                    intent.get("broker_gtt_id"),
                    intent["state"],
                    intent.get("error_detail"),
                    int(intent["created_ts_ms"]),
                    int(intent["updated_ts_ms"]),
                    json.dumps(intent.get("raw_response")) if intent.get("raw_response") is not None else None,
                ),
            )
            row = conn.execute(
                "SELECT * FROM osv2_live_intents WHERE intent_id=?;",
                (intent["intent_id"],),
            ).fetchone()
            return dict(row), True

    def update_intent(
        self,
        intent_id: str,
        *,
        state: str,
        updated_ts_ms: int,
        filled_qty: Optional[int] = None,
        avg_fill_price: Optional[float] = None,
        broker_order_id: Optional[str] = None,
        broker_gtt_id: Optional[str] = None,
        error_detail: Optional[str] = None,
        raw_response: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self.write_tx() as conn:
            conn.execute(
                """
                UPDATE osv2_live_intents
                SET state=?,
                    updated_ts_ms=?,
                    filled_qty=COALESCE(?, filled_qty),
                    avg_fill_price=COALESCE(?, avg_fill_price),
                    broker_order_id=COALESCE(?, broker_order_id),
                    broker_gtt_id=COALESCE(?, broker_gtt_id),
                    error_detail=?,
                    raw_response_json=COALESCE(?, raw_response_json)
                WHERE intent_id=?;
                """,
                (
                    state,
                    int(updated_ts_ms),
                    filled_qty,
                    avg_fill_price,
                    broker_order_id,
                    broker_gtt_id,
                    error_detail,
                    json.dumps(raw_response, sort_keys=True) if raw_response is not None else None,
                    intent_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM osv2_live_intents WHERE intent_id=?;", (intent_id,)
            ).fetchone()
            return dict(row)

    def upsert_package(self, pkg: Dict[str, Any]) -> None:
        with self.write_tx() as conn:
            conn.execute(
                """
                INSERT INTO osv2_live_packages (
                    live_package_id, signal_id, session_date, book_id, protocol_profile_id,
                    structure, state, entry_ts_ms, exit_ts_ms, exit_reason, legs_json,
                    realized_gross_inr, realized_friction_inr, realized_net_inr,
                    reconciled_at_iso, unresolved_fault, updated_at_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(live_package_id) DO UPDATE SET
                    state=excluded.state,
                    exit_ts_ms=excluded.exit_ts_ms,
                    exit_reason=excluded.exit_reason,
                    legs_json=excluded.legs_json,
                    realized_gross_inr=excluded.realized_gross_inr,
                    realized_friction_inr=excluded.realized_friction_inr,
                    realized_net_inr=excluded.realized_net_inr,
                    reconciled_at_iso=excluded.reconciled_at_iso,
                    unresolved_fault=excluded.unresolved_fault,
                    updated_at_iso=excluded.updated_at_iso;
                """,
                (
                    pkg["live_package_id"],
                    pkg["signal_id"],
                    pkg["session_date"],
                    pkg["book_id"],
                    pkg["protocol_profile_id"],
                    pkg["structure"],
                    pkg["state"],
                    int(pkg["entry_ts_ms"]),
                    pkg.get("exit_ts_ms"),
                    pkg.get("exit_reason"),
                    json.dumps(pkg.get("legs", {}), sort_keys=True),
                    float(pkg.get("realized_gross_inr", 0.0)),
                    float(pkg.get("realized_friction_inr", 0.0)),
                    float(pkg.get("realized_net_inr", 0.0)),
                    pkg.get("reconciled_at_iso"),
                    pkg.get("unresolved_fault"),
                    now_ist_iso(),
                ),
            )

    def get_package(self, live_package_id: str) -> Optional[Dict[str, Any]]:
        if not self.db_exists():
            return None
        conn = self._connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT * FROM osv2_live_packages WHERE live_package_id=?;",
                (live_package_id,),
            ).fetchone()
            if not row:
                return None
            out = dict(row)
            out["legs"] = json.loads(out.get("legs_json") or "{}")
            return out
        finally:
            conn.close()

    def get_package_by_signal(self, signal_id: str) -> Optional[Dict[str, Any]]:
        if not self.db_exists():
            return None
        conn = self._connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT * FROM osv2_live_packages WHERE signal_id=?;",
                (signal_id,),
            ).fetchone()
            if not row:
                return None
            out = dict(row)
            out["legs"] = json.loads(out.get("legs_json") or "{}")
            return out
        finally:
            conn.close()

    def list_packages(self, *, states: Optional[List[str]] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if not self.db_exists():
            return []
        conn = self._connect(readonly=True)
        try:
            sql = "SELECT * FROM osv2_live_packages WHERE 1=1"
            params: List[Any] = []
            if states:
                ph = ",".join("?" for _ in states)
                sql += f" AND state IN ({ph})"
                params.extend(states)
            sql += " ORDER BY entry_ts_ms DESC LIMIT ?;"
            params.append(int(limit))
            rows = []
            for r in conn.execute(sql, params).fetchall():
                d = dict(r)
                d["legs"] = json.loads(d.get("legs_json") or "{}")
                rows.append(d)
            return rows
        finally:
            conn.close()

    def list_audit_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        if not self.db_exists():
            return []
        conn = self._connect(readonly=True)
        try:
            rows = conn.execute(
                "SELECT * FROM osv2_live_audit_log ORDER BY id DESC LIMIT ?;",
                (int(limit),),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["detail"] = json.loads(d.get("detail_json") or "{}")
                except Exception:
                    d["detail"] = {}
                out.append(d)
            return out
        finally:
            conn.close()

    def count_unresolved_faults(self) -> Dict[str, Any]:
        if not self.db_exists():
            return {
                "ambiguous_intents": 0,
                "faulted_packages": 0,
                "total_blocking_faults": 0,
                "items": [],
            }
        ambiguous = self.list_intents(states=["UNKNOWN_AMBIGUOUS", "PENDING_SEND", "SENT"])
        faulted_pkgs = self.list_packages(
            states=["UNWINDING_INCOMPLETE", "MANUAL_INTERVENTION_REQUIRED", "RECONCILE_MISMATCH"]
        )
        items: List[Dict[str, Any]] = []
        for it in ambiguous:
            items.append(
                {
                    "kind": "INTENT_UNRESOLVED",
                    "id": it["intent_id"],
                    "state": it["state"],
                    "instrument_key": it["instrument_key"],
                    "detail": it.get("error_detail") or it["state"],
                }
            )
        for pk in faulted_pkgs:
            items.append(
                {
                    "kind": "PACKAGE_FAULT",
                    "id": pk["live_package_id"],
                    "state": pk["state"],
                    "instrument_key": pk.get("book_id"),
                    "detail": pk.get("unresolved_fault") or pk["state"],
                }
            )
        return {
            "ambiguous_intents": len(ambiguous),
            "faulted_packages": len(faulted_pkgs),
            "total_blocking_faults": len(items),
            "items": items,
        }
