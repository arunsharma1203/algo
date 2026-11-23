"""
Isolated Shadow V1 Live & Dry-Run Broker Mirror Executor (`shadow_mirror_executor.py`).

Architectural & Principal Quant Safeguards:
  1. ZERO CORE LOGIC MUTATIONS: Keeps `SHADOW_PREREGISTRATION.json`, `s205_signal_evaluator.py`,
     `s205_meta_model.py`, `options_accounting.py`, and `champion_ensemble.pkl` 100% untouched.
  2. HARD DRY-RUN DEFAULT & GATEWAY FIREWALL: Boots strictly in `DRY_RUN` (`is_live_armed = False`).
     `POST /order/place`, `PUT /order/modify`, and `DELETE /order/cancel` are physically unreachable
     unless `execution_tier == "LIVE_ARMED"` AND the human operator explicitly armed the matching
     mode via the 4-digit PIN UI modal (`AGENTS.md` Rule 6).
  3. PERMANENT V6B AIRGAP: Rejects any `book_id` outside `("SHADOW_BOOK_A_SPREAD_150", "SHADOW_BOOK_B_PAID_FLOW")`.
     `V6B_ML_LEGACY_COMPARATOR` can NEVER pass this gateway.
  4. ATOMIC SETTLED-ORDER 2-LEG STATE MACHINE (Book A):
     - Strict live quote hygiene (`bid > 0 and ask >= bid`; zero synthetic fallback on live entry/Leg-2).
     - Entry: `BUY Wing` -> `/order/history` (`entries[-1]`) `complete (65/65)` -> live short bid
       preserves `min_net_credit >= 14.0` -> `SELL Short` -> `complete (65/65)`.
     - Unconfirmed-cancel invariant (`terminal=True` required before any stage escalation or retry):
       an unsettled `PENDING:<oid>` is ALWAYS reconciled before any replacement order can fire,
       and partial fills are credited atomically once and only once upon `terminal=True`.
     - Short-before-Wing invariant: whether unwinding a partial Leg-2 abort, exiting normally, or
       flattening an orphan short, `wing` is NEVER sold while `short_rem > 0`. Failed unwinds
       transition to `EXIT_RETRY_PENDING` (never fabricating synthetic `0.95x/1.05x` terminal fills).
  5. ANTI-DOUBLE-SELL SL-L PROTOCOL (Book B):
     - Entry: `BUY Long` -> `complete (65/65)` -> place & verify exchange `SL-L` (`hard_stop_px`);
       if NSE rejects the SL (e.g. gap below trigger), immediately executes protective flatten!
     - Exit: `DELETE /order/cancel` on `broker_sl_oid` -> confirm `terminal=True` -> persist any
       partial SL fill immediately to SQLite -> only sell exact remaining `qty - exited_qty`.
  6. CANONICAL ACCOUNTING: 100% of fees, STT (`0.15%` Apr-2026), ticks (`₹0.05`), and net P&L
     (including watchdog external/orphan closes) use `app.analytics.common.options_accounting`.
"""

from __future__ import annotations

import copy
from datetime import datetime, time as dt_time
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.analytics.common.options_accounting import (
    OptionLeg,
    compute_package_accounting,
    round_to_nse_tick,
)

logger = logging.getLogger(__name__)


def _to_ist_naive_local(dt_val: Optional[datetime] = None) -> datetime:
    try:
        from app.analytics.option_sniffer.option_constants import to_ist_naive as _t_ist

        return _t_ist(dt_val)
    except Exception:
        if dt_val is None:
            return datetime.now()
        return dt_val.replace(tzinfo=None) if getattr(dt_val, "tzinfo", None) is not None else dt_val


def _sanitize_tag_local(raw: str) -> str:
    try:
        from app.analytics.option_sniffer.option_live_order_intent import sanitize_upstox_tag as _san

        return _san(raw)
    except Exception:
        cleaned = "".join(ch for ch in str(raw or "") if ch.isalnum() or ch in ("-", "_"))
        return cleaned[:20] or "shmir"


def _resolve_db_path_local() -> str:
    try:
        from app.data.historical_data_layer import get_db_path as _h_db

        return _h_db()
    except Exception:
        from app.data.database import get_db_path as _db_p

        return _db_p()


to_ist_naive = _to_ist_naive_local
sanitize_upstox_tag = _sanitize_tag_local
get_db_path = _resolve_db_path_local


def _lazy_get_option_quote(ikey: str) -> Optional[Dict[str, Any]]:
    from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

    return OptionStreamManager().get_option_quote(ikey)


def _lazy_fetch_readiness(force_refresh: bool) -> Dict[str, Any]:
    from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter

    return dict(get_option_live_adapter().fetch_account_readiness(force_refresh=force_refresh) or {})


def _lazy_broker_api_call(*args: Any, **kwargs: Any) -> Tuple[bool, int, Dict[str, Any]]:
    from app.analytics.option_sniffer.option_live_order_intent import get_order_intent_engine

    return get_order_intent_engine()._call_upstox_broker_api(*args, **kwargs)


BOOK_ID_SPREAD_A: str = "SHADOW_BOOK_A_SPREAD_150"
BOOK_ID_PAID_B: str = "SHADOW_BOOK_B_PAID_FLOW"
ALLOWED_SHADOW_BOOKS = (BOOK_ID_SPREAD_A, BOOK_ID_PAID_B)

MODE_SHADOW_DRY_RUN_AB: str = "SHADOW_DRY_RUN_AB"
MODE_SHADOW_LIVE_BOOK_A: str = "SHADOW_LIVE_BOOK_A"
MODE_SHADOW_LIVE_BOOK_B: str = "SHADOW_LIVE_BOOK_B"
MODE_SHADOW_LIVE_AB: str = "SHADOW_LIVE_AB"

SHADOW_MIRROR_MODES = (
    MODE_SHADOW_DRY_RUN_AB,
    MODE_SHADOW_LIVE_BOOK_A,
    MODE_SHADOW_LIVE_BOOK_B,
    MODE_SHADOW_LIVE_AB,
)

MIN_MARGIN_SHADOW_DRY_RUN_INR: float = 0.0
MIN_MARGIN_SHADOW_BOOK_B_INR: float = 25000.0
MIN_MARGIN_SHADOW_BOOK_A_INR: float = 75000.0
MIN_MARGIN_SHADOW_AB_INR: float = 85000.0

SHADOW_LOT_QTY: int = 65
SHADOW_MIN_NET_CREDIT_PTS: float = 14.0
SHADOW_BOOK_B_MAX_OUTLAY_INR: float = 11500.0
SHADOW_MTM_CUSHION_INR: float = 10000.0
SHADOW_MAX_LEG2_ABORTS_PER_DAY: int = 1

PENDING_OID_PREFIX: str = "PENDING:"

TRANSITIONAL_ENTRY_STATES = ("LEG1_SUBMITTED", "LEG2_SUBMITTED")
ACTIVE_LIVE_STATES = (
    "LEG1_SUBMITTED",
    "LEG2_SUBMITTED",
    "OPEN_HEDGED",
    "OPEN_LONG",
    "EXITING_SHORT",
    "EXITING_WING",
    "EXITING_LONG",
    "EXIT_RETRY_PENDING",
)
TERMINAL_STATES = (
    "CONFIRMED_FLAT",
    "ABORTED_CLEAN",
    "ABORTED_PARTIAL_UNWOUND",
    "LEG2_CREDIT_FADE_UNWOUND",
    "LEG2_ABORT_UNWOUND",
    "LEG2_PARTIAL_UNWOUND",
    "PREFLIGHT_BLOCKED",
    "DRY_RUN_CLOSED",
)


def init_shadow_mirror_tables(db_path: Optional[str] = None) -> None:
    """Creates additive WAL-safe table `option_advance_live_shadow_packages`."""
    resolved = db_path or get_db_path()
    conn = sqlite3.connect(resolved, timeout=15.0)
    try:
        with conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_advance_live_shadow_packages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mirror_id TEXT NOT NULL UNIQUE,
                    shadow_package_id TEXT NOT NULL UNIQUE,
                    advance_trade_id TEXT,
                    session_date TEXT NOT NULL,
                    book_id TEXT NOT NULL,
                    execution_tier TEXT NOT NULL,
                    bridge_mode TEXT NOT NULL,
                    state TEXT NOT NULL,
                    direction_label TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    qty INTEGER NOT NULL DEFAULT 65,
                    leg1_instrument_key TEXT,
                    leg1_trading_symbol TEXT,
                    leg1_strike REAL,
                    leg1_option_type TEXT,
                    leg1_side TEXT,
                    leg1_entry_oid TEXT,
                    leg1_entry_limit REAL,
                    leg1_entry_fill REAL,
                    leg1_filled_qty INTEGER NOT NULL DEFAULT 0,
                    leg1_exit_oid TEXT,
                    leg1_exit_limit REAL,
                    leg1_exit_fill REAL,
                    leg1_exited_qty INTEGER NOT NULL DEFAULT 0,
                    leg2_instrument_key TEXT,
                    leg2_trading_symbol TEXT,
                    leg2_strike REAL,
                    leg2_option_type TEXT,
                    leg2_side TEXT,
                    leg2_entry_oid TEXT,
                    leg2_entry_limit REAL,
                    leg2_entry_fill REAL,
                    leg2_filled_qty INTEGER NOT NULL DEFAULT 0,
                    leg2_exit_oid TEXT,
                    leg2_exit_limit REAL,
                    leg2_exit_fill REAL,
                    leg2_exited_qty INTEGER NOT NULL DEFAULT 0,
                    broker_sl_oid TEXT,
                    broker_sl_trigger REAL,
                    broker_sl_limit REAL,
                    broker_sl_status TEXT,
                    shadow_entry_ref REAL,
                    shadow_exit_ref REAL,
                    shadow_net_pnl_inr REAL,
                    gross_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    statutory_charges_inr REAL NOT NULL DEFAULT 0.0,
                    realized_net_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    live_minus_shadow_drift_inr REAL,
                    exit_reason TEXT,
                    error_reason TEXT,
                    preflight_json TEXT,
                    payload_audit_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_live_shadow_pkg_session_tier "
                "ON option_advance_live_shadow_packages(session_date, execution_tier, state);"
            )
    finally:
        conn.close()


def compute_smart_limit_collar(ref_price: float, side: str, stage: int = 1) -> float:
    """
    2-Stage Smart Marketable LIMIT collar rounded strictly to NSE ₹0.05 ticks:
      - Stage 1 (t=0s): +/- max(₹0.50, 1.2%)
      - Stage 2 (t=1s): +/- max(₹1.00, 2.5%)
      - Stage 3 (emergency): +/- max(₹2.00, 5.0%)
    """
    px = max(0.05, float(ref_price or 0.05))
    side_u = str(side or "").upper().strip()
    if stage <= 1:
        buf = max(0.50, px * 0.012)
    elif stage == 2:
        buf = max(1.00, px * 0.025)
    else:
        buf = max(2.00, px * 0.050)

    if side_u == "BUY":
        return round_to_nse_tick(px + buf, mode="adverse_buy")
    return round_to_nse_tick(max(0.05, px - buf), mode="adverse_sell")


class ShadowMirrorExecutor:
    """
    Isolated execution state machine for mirroring `S205_SHADOW_FORWARD_V1`
    (`SHADOW_BOOK_A_SPREAD_150` and `SHADOW_BOOK_B_PAID_FLOW`) in either `DRY_RUN`
    (zero broker order mutations) or `LIVE_ARMED` mode.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        bridge_state_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        disarm_callback: Optional[Callable[[str], None]] = None,
        broker_api_fn: Optional[Callable[..., Tuple[bool, int, Dict[str, Any]]]] = None,
        quote_lookup_fn: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        readiness_fn: Optional[Callable[[], Dict[str, Any]]] = None,
        poll_interval_sec: float = 0.30,
        poll_max_attempts: int = 8,
        auto_start_worker: bool = True,
    ) -> None:
        self._db_path_override = db_path
        self._bridge_state_fn = bridge_state_fn
        self._disarm_callback = disarm_callback
        self._broker_api_fn = broker_api_fn
        self._quote_lookup_fn = quote_lookup_fn
        self._readiness_fn = readiness_fn
        self._poll_interval_sec = max(0.001, float(poll_interval_sec))
        self._poll_max_attempts = max(1, int(poll_max_attempts))

        self._lock = threading.RLock()
        self._pkg_locks: Dict[str, threading.Lock] = {}
        self._queue: "queue.Queue[Tuple[str, Dict[str, Any]]]" = queue.Queue(maxsize=256)
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        self._mutation_call_count: int = 0
        self._last_ratchet_modify_epoch: Dict[str, float] = {}
        self._exit_quote_fail_streak: Dict[str, int] = {}
        self._pending_disarm_reasons: List[str] = []
        self._last_watchdog_epoch: float = 0.0
        self._last_db_sync_epoch: float = 0.0
        self._last_worker_idle_mono: float = 0.0

        init_shadow_mirror_tables(self._db_path())
        self._boot_recovery_pending: bool = self._detect_non_terminal_rows_on_boot()

        if auto_start_worker:
            self.start_worker()

    @property
    def mutation_call_count(self) -> int:
        return self._mutation_call_count

    def _db_path(self) -> str:
        return self._db_path_override or get_db_path()

    def _get_conn(self, readonly: bool = False) -> sqlite3.Connection:
        p = self._db_path()
        if readonly and os.path.exists(p):
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10.0)
            conn.execute("PRAGMA query_only = ON;")
        else:
            conn = sqlite3.connect(p, timeout=15.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_pkg_lock(self, shadow_pkg_id: str) -> threading.Lock:
        with self._lock:
            lk = self._pkg_locks.get(shadow_pkg_id)
            if lk is None:
                lk = threading.Lock()
                self._pkg_locks[shadow_pkg_id] = lk
            return lk

    def _detect_non_terminal_rows_on_boot(self) -> bool:
        try:
            with self._get_conn(readonly=True) as conn:
                placeholders = ",".join(["?"] * len(ACTIVE_LIVE_STATES))
                rows = conn.execute(
                    f"""
                    SELECT mirror_id, shadow_package_id, book_id, state
                    FROM option_advance_live_shadow_packages
                    WHERE execution_tier = 'LIVE_ARMED' AND state IN ({placeholders});
                    """,
                    ACTIVE_LIVE_STATES,
                ).fetchall()
                if rows:
                    logger.warning(
                        f"[ShadowMirrorExecutor] Boot recovery queued {len(rows)} non-terminal LIVE_ARMED package(s): "
                        f"{[dict(r) for r in rows]}"
                    )
                    return True
        except Exception as e:
            logger.debug(f"[ShadowMirrorExecutor] Boot recovery check note: {e}")
        return False

    def start_worker(self) -> None:
        with self._lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="ShadowMirrorExecutorWorker",
                daemon=True,
            )
            self._worker_thread.start()

    def stop_worker(self) -> None:
        self._stop_event.set()

    def enqueue_shadow_event(self, event_type: str, package_snapshot: Dict[str, Any]) -> bool:
        try:
            snap_copy = copy.deepcopy(package_snapshot)
            self._queue.put_nowait((str(event_type), snap_copy))
            return True
        except Exception as e:
            logger.warning(f"[ShadowMirrorExecutor] Queue drop on {event_type}: {e}")
            return False

    def _worker_loop(self) -> None:
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        while not self._stop_event.is_set():
            try:
                ev_type, snap = self._queue.get(timeout=0.5)
            except queue.Empty:
                now_mono = time.monotonic()
                if (now_mono - self._last_worker_idle_mono) >= 2.0:
                    self._last_worker_idle_mono = now_mono
                    try:
                        self.sync_with_shadow_db_once()
                    except Exception as sync_err:
                        logger.debug(f"[ShadowMirrorExecutor] Worker idle sync note: {sync_err}")
                continue
            try:
                self.process_shadow_event_sync(ev_type, snap)
            except Exception as e:
                logger.error(f"[ShadowMirrorExecutor] Error handling {ev_type}: {e}", exc_info=True)
            finally:
                self._queue.task_done()

    # ──────────────────────────────────────────────────────────────────────────
    # BRIDGE STATE, QUOTE HYGIENE & BROKER GATEWAY FIREWALL
    # ──────────────────────────────────────────────────────────────────────────

    def _get_bridge_state(self, include_cb: bool = False) -> Dict[str, Any]:
        if self._bridge_state_fn is not None:
            try:
                return dict(self._bridge_state_fn(include_cb) or {})
            except TypeError:
                try:
                    return dict(self._bridge_state_fn() or {})
                except Exception:
                    pass
            except Exception:
                pass
        return {
            "is_live_armed": False,
            "execution_mode": MODE_SHADOW_DRY_RUN_AB,
            "armed_at_iso": "",
            "armed_for_date": "",
            "is_session_halted": False,
            "daily_total_exposure_pnl_inr": 0.0,
            "max_daily_loss_inr": 3500.0,
        }

    def _resolve_execution_tier_for_book(self, book_id: str) -> Tuple[str, str]:
        st = self._get_bridge_state(include_cb=False)
        is_armed = bool(st.get("is_live_armed", False))
        mode = str(st.get("execution_mode") or MODE_SHADOW_DRY_RUN_AB).upper().strip()
        if not is_armed or mode == MODE_SHADOW_DRY_RUN_AB:
            return "DRY_RUN", mode
        if book_id == BOOK_ID_SPREAD_A and mode in (MODE_SHADOW_LIVE_BOOK_A, MODE_SHADOW_LIVE_AB):
            return "LIVE_ARMED", mode
        if book_id == BOOK_ID_PAID_B and mode in (MODE_SHADOW_LIVE_BOOK_B, MODE_SHADOW_LIVE_AB):
            return "LIVE_ARMED", mode
        return "DRY_RUN", mode

    def _get_quote(self, ikey: str) -> Dict[str, Any]:
        if ikey and self._quote_lookup_fn is not None:
            try:
                q = self._quote_lookup_fn(ikey)
                if isinstance(q, dict) and q:
                    return q
            except Exception:
                pass
        if ikey:
            try:
                q = _lazy_get_option_quote(ikey)
                if isinstance(q, dict) and q:
                    return q
            except Exception:
                pass
        return {}

    def _queue_disarm_trigger(self, reason: str) -> None:
        rsn = str(reason or "SHADOW_MIRROR_FAILSAFE").strip()
        with self._lock:
            if rsn not in self._pending_disarm_reasons:
                self._pending_disarm_reasons.append(rsn)

    def _flush_pending_disarms(self) -> None:
        """Invokes disarm callback strictly OUTSIDE `_pkg_locks` to guarantee zero AB-BA lock inversion."""
        with self._lock:
            pending = list(self._pending_disarm_reasons)
            self._pending_disarm_reasons.clear()
        for rsn in pending:
            if self._disarm_callback:
                try:
                    self._disarm_callback(rsn)
                except Exception as exc:
                    logger.error(f"[ShadowMirrorExecutor] Disarm callback error ({rsn}): {exc}")

    @staticmethod
    def _extract_quote_age_ms(quote: Optional[Dict[str, Any]]) -> Optional[int]:
        """
        Extracts conservative quote/book age in ms.
        Returns `None` when age is missing, negative, unparseable, or an unbacked `999999` sentinel —
        NEVER defaults unknown age to `0`!
        """
        if not isinstance(quote, dict) or not quote:
            return None

        q_age: Optional[int] = None
        if quote.get("quote_age_ms") is not None:
            try:
                qa = int(quote["quote_age_ms"])
                has_q_ts = any(
                    isinstance(quote.get(k), (int, float)) and int(quote.get(k) or 0) > 0
                    for k in ("timestamp_ms", "local_receipt_ts_ms", "l1_book_timestamp_ms")
                )
                if qa >= 0 and not (qa == 999999 and not has_q_ts):
                    q_age = qa
            except (ValueError, TypeError):
                return None

        l1_age: Optional[int] = None
        has_l1_field = quote.get("l1_book_age_ms") is not None
        if has_l1_field:
            try:
                la = int(quote["l1_book_age_ms"])
                has_l1_ts = isinstance(quote.get("l1_book_timestamp_ms"), (int, float)) and int(quote.get("l1_book_timestamp_ms") or 0) > 0
                if la >= 0 and not (la == 999999 and not has_l1_ts):
                    l1_age = la
            except (ValueError, TypeError):
                return None

        prov = str(quote.get("book_provenance") or "")
        if prov == "EMERGENCY_REST_L1":
            return q_age
        if has_l1_field and l1_age is None:
            # Explicit unbacked `l1_book_age_ms=999999` sentinel MUST NOT be masked by `q_age`!
            return None
        if q_age is not None and l1_age is not None:
            return max(q_age, l1_age)
        return q_age if q_age is not None else l1_age

    def _get_fresh_exit_quote(
        self, ikey: str, max_age_ms: int = 5000
    ) -> Tuple[bool, Dict[str, Any], str]:
        """
        Fail-closed fresh exit quote resolver (`<= max_age_ms`) for LIVE_ARMED limit-collar pricing.
        1. Validates cached WebSocket quote via `_extract_strict_live_bid_ask`.
        2. If stale/missing/unknown, executes emergency REST `GET /market-quote/quotes` (`is_emergency=True`).
        3. If emergency REST fails, times out, or returns zero/crossed depth, returns
           `(False, {}, "STALE_EXIT_QUOTE_UNRECOVERABLE:...")` — NEVER returns the stale quote!
        """
        q = self._get_quote(ikey)
        ok_ba, _, _, ws_rsn = self._extract_strict_live_bid_ask(q, max_age_ms=max_age_ms)
        if ok_ba:
            return True, q, "WS_FRESH"

        rest_detail = "NO_IKEY"
        if ikey:
            try:
                ok_r, sc_r, rdata = self._broker_query(
                    "/market-quote/quotes",
                    params={"instrument_key": str(ikey)},
                    is_emergency=True,
                )
                if ok_r and sc_r == 200 and isinstance(rdata, dict):
                    dmap = rdata.get("data")
                    if isinstance(dmap, dict) and dmap:
                        matched: Optional[Dict[str, Any]] = None
                        colon_ik = str(ikey).replace("|", ":")
                        pipe_ik = str(ikey).replace(":", "|")
                        for k, v in dmap.items():
                            if not isinstance(v, dict):
                                continue
                            tok = str(v.get("instrument_token") or k)
                            if tok in (ikey, colon_ik, pipe_ik) or k in (ikey, colon_ik, pipe_ik):
                                matched = v
                                break
                        if matched is None:
                            first_v = next(iter(dmap.values()), None)
                            if isinstance(first_v, dict):
                                matched = first_v
                        if isinstance(matched, dict):
                            buy_d = (matched.get("depth") or {}).get("buy") or []
                            sell_d = (matched.get("depth") or {}).get("sell") or []
                            b_px = float((buy_d[0] if buy_d else {}).get("price") or 0.0)
                            a_px = float((sell_d[0] if sell_d else {}).get("price") or 0.0)
                            ltp_px = float(matched.get("last_price") or 0.0)
                            now_ms = int(time.time() * 1000)
                            fresh_q = {
                                "instrument_key": str(ikey),
                                "bid": b_px,
                                "ask": a_px,
                                "raw_bid": b_px,
                                "raw_ask": a_px,
                                "ltp": ltp_px if ltp_px > 0.0 else round(0.5 * (b_px + a_px), 2),
                                "quote_age_ms": 0,
                                "timestamp_ms": now_ms,
                                "local_receipt_ts_ms": now_ms,
                                "book_provenance": "EMERGENCY_REST_L1",
                            }
                            ok_rest_ba, _, _, rest_ba_rsn = self._extract_strict_live_bid_ask(
                                fresh_q, max_age_ms=max_age_ms
                            )
                            if ok_rest_ba:
                                return True, fresh_q, "EMERGENCY_REST_FRESH"
                            rest_detail = f"REST_INVALID_BOOK:{rest_ba_rsn}"
                        else:
                            rest_detail = "REST_NO_MATCHED_QUOTE"
                    else:
                        rest_detail = "REST_EMPTY_DATA"
                else:
                    rest_detail = f"REST_HTTP_{sc_r}"
            except Exception as exc:
                rest_detail = f"REST_EXC:{type(exc).__name__}"
                logger.warning(f"[ShadowMirrorExecutor] Emergency exit quote REST refresh note ({ikey}): {exc}")
        return False, {}, f"STALE_EXIT_QUOTE_UNRECOVERABLE:ws={ws_rsn}|rest={rest_detail}"

    @staticmethod
    def _extract_strict_live_bid_ask(
        quote: Optional[Dict[str, Any]],
        max_age_ms: int = 5000,
    ) -> Tuple[bool, float, float, str]:
        """
        Fail-closed live quote validator (`.agents/rules/no-data-leakage.md` Rule 4):
        NEVER invents bid/ask from `ltp` or `entry_price` when quotes are missing, unknown-age,
        stale, zero (`bid <= 0`), or crossed (`ask < bid`).
        """
        if not isinstance(quote, dict) or not quote:
            return False, 0.0, 0.0, "MISSING_LIVE_QUOTE"
        prov = str(quote.get("book_provenance") or "")
        if prov == "SYNTHETIC_LTP_FALLBACK":
            return False, 0.0, 0.0, "SYNTHETIC_LTP_FALLBACK_REJECTED"
        if str(quote.get("quote_freshness") or "") == "SEEDED_BASELINE":
            return False, 0.0, 0.0, "SEEDED_BASELINE_REJECTED"
        rb_raw = quote.get("raw_bid") if quote.get("raw_bid") is not None else quote.get("bid")
        ra_raw = quote.get("raw_ask") if quote.get("raw_ask") is not None else quote.get("ask")
        if rb_raw is None or ra_raw is None:
            return False, 0.0, 0.0, "MISSING_BID_OR_ASK"
        rb = float(rb_raw)
        ra = float(ra_raw)
        if rb <= 0.0:
            return False, 0.0, 0.0, f"ZERO_OR_NEGATIVE_BID:{rb}"
        if ra < rb:
            return False, 0.0, 0.0, f"CROSSED_QUOTE:{rb}>{ra}"
        if max_age_ms is not None and max_age_ms >= 0:
            q_age = ShadowMirrorExecutor._extract_quote_age_ms(quote)
            if q_age is None:
                return False, 0.0, 0.0, "UNKNOWN_LIVE_QUOTE_AGE"
            if q_age > max_age_ms:
                return False, 0.0, 0.0, f"STALE_LIVE_QUOTE:{q_age}ms"
        return True, round(rb, 2), round(ra, 2), "OK"

    @staticmethod
    def _extract_mtm_or_exit_bid_ask(quote: Optional[Dict[str, Any]], fallback_px: float) -> Tuple[float, float]:
        """Used ONLY for emergency exits / DRY_RUN simulation / MTM when live quotes degrade."""
        q = quote if isinstance(quote, dict) else {}
        rb = float(q.get("raw_bid") if q.get("raw_bid") is not None else (q.get("bid") or 0.0))
        ra = float(q.get("raw_ask") if q.get("raw_ask") is not None else (q.get("ask") or 0.0))
        ltp = float(q.get("ltp") or fallback_px or 0.05)
        bid = rb if rb > 0 else max(0.05, ltp)
        ask = ra if (ra > 0 and ra >= bid) else max(bid, ltp)
        return round(bid, 2), round(ask, 2)

    def _fetch_readiness(self, force_refresh: bool = False) -> Dict[str, Any]:
        if self._readiness_fn is not None:
            try:
                return dict(self._readiness_fn() or {})
            except Exception:
                return {}
        try:
            return _lazy_fetch_readiness(force_refresh=force_refresh)
        except Exception:
            return {}

    def _broker_query(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        is_emergency: bool = False,
    ) -> Tuple[bool, int, Dict[str, Any]]:
        fn = self._broker_api_fn or _lazy_broker_api_call
        return fn("GET", endpoint, payload=None, params=params, is_emergency=is_emergency)

    def _broker_mutate(
        self,
        execution_tier: str,
        book_id: str,
        is_entry: bool,
        method: str,
        endpoint: str,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, int, Dict[str, Any]]:
        """SINGLE HARD FIREWALL FOR ALL UPSTOX ORDER MUTATIONS (`POST /order/place`, `PUT /order/modify`, `DELETE /order/cancel`)."""
        if book_id not in ALLOWED_SHADOW_BOOKS:
            return False, 403, {"error": f"FORBIDDEN_BOOK_AIRGAP:{book_id}"}
        if execution_tier != "LIVE_ARMED":
            return False, 0, {"error": "DRY_RUN_FIREWALL_BLOCKED"}
        if is_entry:
            tier_now, _ = self._resolve_execution_tier_for_book(book_id)
            if tier_now != "LIVE_ARMED":
                return False, 403, {"error": "BRIDGE_DISARMED_BEFORE_ENTRY"}

        with self._lock:
            self._mutation_call_count += 1

        fn = self._broker_api_fn or _lazy_broker_api_call
        return fn(method, endpoint, payload=payload, params=params, is_emergency=True)

    def _poll_order_until_terminal(
        self,
        order_id: str,
        target_qty: int = SHADOW_LOT_QTY,
        max_attempts: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Polls `GET /order/history?order_id=...` reading STRICTLY `entries[-1]` (latest state transition).
        Returns `terminal=True` ONLY when Upstox confirms a terminal order status (`complete`, `cancelled`, `rejected`).
        """
        attempts = max_attempts if max_attempts is not None else self._poll_max_attempts
        last_status = "unknown"
        filled_qty = 0
        avg_price = 0.0
        rej_reason = ""

        for idx in range(attempts):
            ok, _, data = self._broker_query("/order/history", params={"order_id": str(order_id)}, is_emergency=True)
            if ok and isinstance(data, dict):
                raw_d = data.get("data")
                latest: Optional[Dict[str, Any]] = None
                if isinstance(raw_d, list) and len(raw_d) > 0:
                    cand = raw_d[-1]
                    if isinstance(cand, dict):
                        latest = cand
                elif isinstance(raw_d, dict):
                    latest = raw_d

                if latest is not None:
                    last_status = str(latest.get("status") or "").lower().strip()
                    filled_qty = int(latest.get("filled_quantity") or 0)
                    avg_price = float(latest.get("average_price") or 0.0)
                    rej_reason = str(latest.get("status_message") or latest.get("status_message_raw") or "")

                    if last_status == "complete" and filled_qty >= target_qty:
                        if avg_price <= 0.0:
                            avg_price = self._fallback_trade_avg_price(order_id, default_px=float(latest.get("price") or 0.0))
                        return {
                            "terminal": True,
                            "status": "complete",
                            "filled_qty": filled_qty,
                            "avg_price": avg_price,
                            "reason": "",
                        }
                    if last_status in ("rejected", "cancelled", "canceled"):
                        norm_st = "cancelled" if last_status in ("cancelled", "canceled") else "rejected"
                        if filled_qty > 0 and avg_price <= 0.0:
                            avg_price = self._fallback_trade_avg_price(order_id, default_px=float(latest.get("price") or 0.0))
                        return {
                            "terminal": True,
                            "status": norm_st,
                            "filled_qty": filled_qty,
                            "avg_price": avg_price,
                            "reason": rej_reason or norm_st.upper(),
                        }
            if idx + 1 < attempts:
                time.sleep(self._poll_interval_sec)

        return {
            "terminal": False,
            "status": last_status,
            "filled_qty": filled_qty,
            "avg_price": avg_price,
            "reason": rej_reason or "POLL_TIMEOUT",
        }

    def _fallback_trade_avg_price(self, order_id: str, default_px: float = 0.0) -> float:
        ok, _, tdata = self._broker_query("/order/trades", params={"order_id": str(order_id)}, is_emergency=True)
        if ok and isinstance(tdata, dict):
            rows = tdata.get("data")
            if isinstance(rows, list) and rows:
                tot_q = 0
                tot_v = 0.0
                for tr in rows:
                    if isinstance(tr, dict):
                        q = int(tr.get("quantity") or 0)
                        p = float(tr.get("average_price") or tr.get("price") or 0.0)
                        if q > 0 and p > 0:
                            tot_q += q
                            tot_v += q * p
                if tot_q > 0:
                    return round(tot_v / tot_q, 2)
        return round(default_px, 2)

    def _cancel_and_confirm_final_qty(
        self,
        execution_tier: str,
        book_id: str,
        order_id: str,
        target_qty: int = SHADOW_LOT_QTY,
    ) -> Dict[str, Any]:
        """Cancels an open order and polls `/order/history` (`entries[-1]`) for terminal confirmation."""
        clean_oid = str(order_id)[len(PENDING_OID_PREFIX):] if str(order_id).startswith(PENDING_OID_PREFIX) else str(order_id)
        self._broker_mutate(
            execution_tier=execution_tier,
            book_id=book_id,
            is_entry=False,
            method="DELETE",
            endpoint="/order/cancel",
            params={"order_id": clean_oid},
        )
        return self._poll_order_until_terminal(clean_oid, target_qty=target_qty, max_attempts=4)

    # ──────────────────────────────────────────────────────────────────────────
    # UNIFIED SETTLED-ORDER COLLARED LEG EXECUTOR (FIXES P0-1, P0-2, P0-3)
    # ──────────────────────────────────────────────────────────────────────────

    def _execute_settled_exit_leg(
        self,
        mirror_id: str,
        spid: str,
        book_id: str,
        leg_index: int,
        ikey: str,
        side: str,
        target_total_qty: int,
        fallback_px: float,
        stages: Tuple[int, ...] = (1, 2, 3),
        tag_prefix: str = "ex",
        now_iso: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes a leg close/unwind with mathematically guaranteed partial-fill & cancel invariants:
          1. If `leg{N}_exit_oid` starts with `"PENDING:<oid>"`, reconciles/cancels that working order FIRST.
             If `not fin["terminal"]`, returns `ok=False` immediately WITHOUT placing any duplicate order!
          2. Credits `filled_qty` into `leg{N}_exited_qty` + weighted `leg{N}_exit_fill` ATOMICALLY ONCE
             when and only when an order reaches `terminal=True` (clearing the `"PENDING:"` prefix).
          3. Never fabricates synthetic `0.95x / 1.05x` fills: if `cum_qty < target_total_qty`, returns
             `ok=False` so caller keeps the package in `EXIT_RETRY_PENDING`.
        """
        oid_col = "leg1_exit_oid" if leg_index == 1 else "leg2_exit_oid"
        lim_col = "leg1_exit_limit" if leg_index == 1 else "leg2_exit_limit"
        fill_col = "leg1_exit_fill" if leg_index == 1 else "leg2_exit_fill"
        qty_col = "leg1_exited_qty" if leg_index == 1 else "leg2_exited_qty"

        row = self._existing_mirror_row(spid)
        if not row:
            return {"ok": False, "cum_qty": 0, "avg_fill": 0.0, "last_oid": None, "reason": "MISSING_ROW"}

        cum_qty = int(row.get(qty_col) or 0)
        cur_avg = float(row.get(fill_col) or 0.0)
        cum_notional = cum_qty * cur_avg
        stored_oid = str(row.get(oid_col) or "")

        # Step A: Reconcile any unsettled prior working order BEFORE submitting anything new!
        if stored_oid.startswith(PENDING_OID_PREFIX):
            raw_prev = stored_oid[len(PENDING_OID_PREFIX):]
            rem_prev = max(1, target_total_qty - cum_qty)
            fin_prev = self._cancel_and_confirm_final_qty("LIVE_ARMED", book_id, raw_prev, target_qty=rem_prev)
            if not fin_prev["terminal"]:
                return {
                    "ok": False,
                    "terminal_confirmed": False,
                    "cum_qty": cum_qty,
                    "avg_fill": cur_avg,
                    "last_oid": stored_oid,
                    "reason": "PRIOR_EXIT_CANCEL_UNCONFIRMED",
                }
            fq_prev = min(rem_prev, max(0, int(fin_prev["filled_qty"])))
            if fq_prev > 0:
                px_prev = float(fin_prev["avg_price"] or row.get(lim_col) or fallback_px)
                cum_notional += fq_prev * px_prev
                cum_qty += fq_prev
                cur_avg = round(cum_notional / float(cum_qty), 4)
            stored_oid = raw_prev
            self._update_mirror_fields(
                mirror_id,
                now_iso=now_iso,
                **{oid_col: stored_oid, fill_col: cur_avg if cum_qty > 0 else None, qty_col: cum_qty},
            )

        if cum_qty >= target_total_qty:
            return {"ok": True, "terminal_confirmed": True, "cum_qty": cum_qty, "avg_fill": cur_avg, "last_oid": stored_oid or None, "reason": ""}

        # Step B: Collared execution strictly for remaining `target_total_qty - cum_qty`
        # HARD FAILSAFE (Finding #1): NEVER price a live exit collar from a stale/unrecoverable quote!
        streak_key = f"{mirror_id}:{ikey}"
        ok_fresh, q_live, q_rsn = self._get_fresh_exit_quote(ikey, max_age_ms=5000)
        ok_ba, bid, ask, ba_rsn = (
            self._extract_strict_live_bid_ask(q_live, max_age_ms=5000)
            if ok_fresh
            else (False, 0.0, 0.0, q_rsn)
        )
        if not ok_fresh or not ok_ba:
            fail_rsn = q_rsn if not ok_fresh else f"STALE_EXIT_QUOTE_UNRECOVERABLE:{ba_rsn}"
            with self._lock:
                streak = int(self._exit_quote_fail_streak.get(streak_key, 0)) + 1
                self._exit_quote_fail_streak[streak_key] = streak
            if streak >= 3:
                logger.critical(
                    f"🚨 [ShadowMirrorExecutor] {streak} consecutive unrecoverable exit-quote failures on "
                    f"{mirror_id} ({ikey}): {fail_rsn} — escalating auto-disarm for new entries while keeping EXIT_RETRY_PENDING!"
                )
                if streak == 3:
                    self._queue_disarm_trigger("UNRECOVERABLE_STALE_EXIT_QUOTE")
            else:
                logger.error(
                    f"[ShadowMirrorExecutor] Unrecoverable exit quote on {mirror_id} ({ikey}, streak={streak}): "
                    f"{fail_rsn} — holding EXIT_RETRY_PENDING without placing stale LIMIT order."
                )
            return {
                "ok": False,
                "terminal_confirmed": True,
                "cum_qty": cum_qty,
                "avg_fill": cur_avg,
                "last_oid": stored_oid or None,
                "reason": fail_rsn,
            }

        with self._lock:
            self._exit_quote_fail_streak[streak_key] = 0
        anchor_base_age_s = float(self._extract_quote_age_ms(q_live) or 0) / 1000.0
        anchor_mono = time.monotonic()
        ref_px = ask if side.upper() == "BUY" else bid
        last_reason = "EXIT_UNFILLED"

        for stg in stages:
            rem = target_total_qty - cum_qty
            if rem <= 0:
                break
            if stg >= 2:
                refreshed_ok = False
                ok_r, q_retry, r_rsn = self._get_fresh_exit_quote(ikey, max_age_ms=2500)
                if ok_r:
                    ok_rba, b_r, a_r, rba_rsn = self._extract_strict_live_bid_ask(q_retry, max_age_ms=2500)
                    if ok_rba:
                        ref_px = a_r if side.upper() == "BUY" else b_r
                        anchor_base_age_s = float(self._extract_quote_age_ms(q_retry) or 0) / 1000.0
                        anchor_mono = time.monotonic()
                        refreshed_ok = True
                    else:
                        r_rsn = f"STALE_EXIT_QUOTE_UNRECOVERABLE:{rba_rsn}"
                if not refreshed_ok and (anchor_base_age_s + (time.monotonic() - anchor_mono)) > 5.0:
                    with self._lock:
                        streak = int(self._exit_quote_fail_streak.get(streak_key, 0)) + 1
                        self._exit_quote_fail_streak[streak_key] = streak
                    if streak >= 3:
                        if streak == 3:
                            self._queue_disarm_trigger("UNRECOVERABLE_STALE_EXIT_QUOTE")
                    return {
                        "ok": False,
                        "terminal_confirmed": True,
                        "cum_qty": cum_qty,
                        "avg_fill": cur_avg,
                        "last_oid": stored_oid or None,
                        "reason": r_rsn,
                    }
            lim_px = compute_smart_limit_collar(ref_px, side, stage=stg)
            pl = {
                "quantity": int(rem),
                "product": "I",
                "validity": "DAY",
                "price": float(lim_px),
                "tag": sanitize_upstox_tag(f"{tag_prefix}{stg}-{spid[-8:]}"),
                "instrument_token": ikey,
                "order_type": "LIMIT",
                "transaction_type": side.upper(),
                "disclosed_quantity": 0,
                "trigger_price": 0.0,
                "is_amo": False,
            }
            ok, sc, d = self._broker_mutate("LIVE_ARMED", book_id, False, "POST", "/order/place", payload=pl)
            oid = str((d or {}).get("data", {}).get("order_id") or "") if (ok and isinstance(d, dict) and d.get("status") == "success") else ""
            if not oid:
                last_reason = str((d or {}).get("error") or f"EXIT_PLACE_HTTP_{sc}")
                continue

            # Persist `PENDING:<oid>` BEFORE polling so a crash or timeout never loses track of `oid`!
            pending_tag = f"{PENDING_OID_PREFIX}{oid}"
            self._update_mirror_fields(mirror_id, now_iso=now_iso, **{oid_col: pending_tag, lim_col: lim_px})

            poll = self._poll_order_until_terminal(oid, target_qty=rem, max_attempts=4)
            if not poll["terminal"]:
                fin = self._cancel_and_confirm_final_qty("LIVE_ARMED", book_id, oid, target_qty=rem)
            else:
                fin = poll

            # CRITICAL P0-1 INVARIANT: If cancel is STILL unconfirmed (`terminal == False`), STOP IMMEDIATELY!
            if not fin["terminal"]:
                return {
                    "ok": False,
                    "terminal_confirmed": False,
                    "cum_qty": cum_qty,
                    "avg_fill": cur_avg,
                    "last_oid": pending_tag,
                    "reason": f"CANCEL_UNCONFIRMED:{fin.get('reason')}",
                }

            fq = min(rem, max(0, int(fin["filled_qty"])))
            if fq > 0:
                px_used = float(fin["avg_price"] or lim_px)
                cum_notional += fq * px_used
                cum_qty += fq
                cur_avg = round(cum_notional / float(cum_qty), 4)

            stored_oid = oid
            self._update_mirror_fields(
                mirror_id,
                now_iso=now_iso,
                **{oid_col: stored_oid, fill_col: cur_avg if cum_qty > 0 else None, qty_col: cum_qty},
            )
            last_reason = str(fin.get("reason") or last_reason)

            if cum_qty >= target_total_qty:
                return {
                    "ok": True,
                    "terminal_confirmed": True,
                    "cum_qty": cum_qty,
                    "avg_fill": cur_avg,
                    "last_oid": stored_oid,
                    "reason": "",
                }

        return {
            "ok": False,
            "terminal_confirmed": True,
            "cum_qty": cum_qty,
            "avg_fill": cur_avg,
            "last_oid": stored_oid or None,
            "reason": last_reason,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # EVENT DISPATCHER
    # ──────────────────────────────────────────────────────────────────────────

    def process_shadow_event_sync(
        self,
        event_type: str,
        pkg: Dict[str, Any],
        now_dt: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(pkg, dict):
            return None
        book_id = str(pkg.get("book_id") or "").strip()
        if book_id not in ALLOWED_SHADOW_BOOKS:
            return None
        spid = str(pkg.get("package_id") or "").strip()
        if not spid:
            return None

        now_ist = to_ist_naive(now_dt)
        result: Optional[Dict[str, Any]] = None
        with self._get_pkg_lock(spid):
            if event_type == "BOOK_A_OPEN" and book_id == BOOK_ID_SPREAD_A:
                result = self._handle_book_a_open(pkg, now_ist)
            elif event_type == "BOOK_B_OPEN" and book_id == BOOK_ID_PAID_B:
                result = self._handle_book_b_open(pkg, now_ist)
            elif event_type == "BOOK_B_RATCHET" and book_id == BOOK_ID_PAID_B:
                result = self._handle_book_b_ratchet(pkg, now_ist)
            elif event_type in ("BOOK_A_CLOSE", "BOOK_B_CLOSE"):
                result = self._handle_package_close(pkg, now_ist)
        self._flush_pending_disarms()
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # PREFLIGHT GATES
    # ──────────────────────────────────────────────────────────────────────────

    def _count_live_book_stats_today(self, session_date: str, book_id: str) -> Tuple[int, int, int]:
        with self._get_conn(readonly=True) as conn:
            placeholders = ",".join(["?"] * len(ACTIVE_LIVE_STATES))
            open_cnt = int(
                (
                    conn.execute(
                        f"""
                        SELECT COUNT(*) FROM option_advance_live_shadow_packages
                        WHERE session_date = ? AND book_id = ? AND execution_tier = 'LIVE_ARMED'
                          AND state IN ({placeholders});
                        """,
                        (session_date, book_id, *ACTIVE_LIVE_STATES),
                    ).fetchone()
                    or [0]
                )[0]
            )
            trades_cnt = int(
                (
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM option_advance_live_shadow_packages
                        WHERE session_date = ? AND book_id = ? AND execution_tier = 'LIVE_ARMED'
                          AND state IN ('OPEN_HEDGED','OPEN_LONG','EXITING_SHORT','EXITING_WING',
                                        'EXITING_LONG','EXIT_RETRY_PENDING','CONFIRMED_FLAT');
                        """,
                        (session_date, book_id),
                    ).fetchone()
                    or [0]
                )[0]
            )
            aborts_cnt = int(
                (
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM option_advance_live_shadow_packages
                        WHERE session_date = ? AND book_id = ? AND execution_tier = 'LIVE_ARMED'
                          AND (state IN ('LEG2_CREDIT_FADE_UNWOUND','LEG2_ABORT_UNWOUND','LEG2_PARTIAL_UNWOUND')
                               OR COALESCE(exit_reason, '') LIKE 'LEG2_%');
                        """,
                        (session_date, book_id),
                    ).fetchone()
                    or [0]
                )[0]
            )
        return open_cnt, trades_cnt, aborts_cnt

    def _run_entry_preflight(
        self,
        pkg: Dict[str, Any],
        book_id: str,
        execution_tier: str,
        required_cash_inr: float,
        now_ist: datetime,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        st = self._get_bridge_state(include_cb=(execution_tier == "LIVE_ARMED"))
        session_date = str(pkg.get("session_date") or now_ist.strftime("%Y-%m-%d"))[:10]
        entry_ts_norm = str(pkg.get("leg2_confirmed_time") or pkg.get("entry_time") or "").replace("T", " ")[:19]
        armed_ts_norm = str(st.get("armed_at_iso") or "").replace("T", " ")[:19]

        readiness = self._fetch_readiness(force_refresh=(execution_tier == "LIVE_ARMED"))
        avail_margin = float(readiness.get("available_margin") or 0.0)
        live_elig = bool(readiness.get("live_trading_eligible") or readiness.get("ready_for_live"))

        details = {
            "execution_tier": execution_tier,
            "available_margin_inr": avail_margin,
            "required_cash_with_cushion_inr": round(required_cash_inr, 2),
            "live_trading_eligible": live_elig,
            "entry_ts_norm": entry_ts_norm,
            "armed_ts_norm": armed_ts_norm,
        }

        if execution_tier != "LIVE_ARMED":
            return True, "DRY_RUN_PASS", details

        if now_ist.time() >= dt_time(15, 15):
            return False, "PAST_1515_SESSION_CUTOFF", details
        if bool(st.get("is_session_halted", False)):
            return False, f"CIRCUIT_BREAKER_HALTED:{st.get('halt_reason')}", details
        if bool(st.get("has_stale_positions", False)):
            return False, "STALE_OPEN_POSITION_MTM_BLOCK", details
        max_loss = float(st.get("max_daily_loss_inr") or 3500.0)
        if float(st.get("daily_total_exposure_pnl_inr") or 0.0) <= -abs(max_loss):
            return False, "DAILY_LOSS_CEILING_REACHED", details
        if armed_ts_norm and entry_ts_norm and entry_ts_norm < armed_ts_norm:
            return False, f"PRE_ARM_SIGNAL_SKIPPED:{entry_ts_norm}<{armed_ts_norm}", details

        open_cnt, trades_cnt, aborts_cnt = self._count_live_book_stats_today(session_date, book_id)
        if open_cnt >= 1:
            return False, f"BOOK_MAX_OPEN_REACHED:{open_cnt}", details
        if trades_cnt >= 1:
            return False, f"BOOK_MAX_DAILY_TRADES_REACHED:{trades_cnt}", details
        if book_id == BOOK_ID_SPREAD_A and aborts_cnt >= SHADOW_MAX_LEG2_ABORTS_PER_DAY:
            return False, f"MAX_LEG2_ABORTS_REACHED:{aborts_cnt}", details

        if not live_elig:
            return False, "UPSTOX_ACCOUNT_NOT_ELIGIBLE", details
        if avail_margin < required_cash_inr:
            return False, f"INSUFFICIENT_MARGIN:{avail_margin:.2f}<{required_cash_inr:.2f}", details

        return True, "LIVE_PREFLIGHT_OK", details

    # ──────────────────────────────────────────────────────────────────────────
    # IN-FLIGHT ENTRY RECONCILIATION (CRASH RECOVERY + UNCONFIRMED CANCEL GUARD)
    # ──────────────────────────────────────────────────────────────────────────

    def _existing_mirror_row(self, shadow_pkg_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM option_advance_live_shadow_packages WHERE shadow_package_id = ? LIMIT 1;",
                (shadow_pkg_id,),
            ).fetchone()
            return dict(row) if row else None

    def _reconcile_in_flight_entries_for_row(self, spid: str, now_ist: datetime) -> Optional[Dict[str, Any]]:
        """
        Resolves any interrupted `LEG1_SUBMITTED` / `LEG2_SUBMITTED` or `PENDING:<oid>` entry order
        so crash-recovery, `sync_with_shadow_db_once`, and `SOS Panic Exit` never strand live exposure.
        """
        row = self._existing_mirror_row(spid)
        if not row or row["execution_tier"] != "LIVE_ARMED":
            return row

        mirror_id = str(row["mirror_id"])
        book_id = str(row["book_id"])
        qty = int(row["qty"])
        now_iso = now_ist.strftime("%Y-%m-%d %H:%M:%S")

        e1 = str(row.get("leg1_entry_oid") or "")
        e2 = str(row.get("leg2_entry_oid") or "")

        if e1.startswith(PENDING_OID_PREFIX) or row["state"] == "LEG1_SUBMITTED":
            if not e1:
                return self._update_mirror_fields(mirror_id, state="ABORTED_CLEAN", error_reason="STRANDED_LEG1_NO_OID", now_iso=now_iso)
            raw1 = e1[len(PENDING_OID_PREFIX):] if e1.startswith(PENDING_OID_PREFIX) else e1
            fin1 = self._cancel_and_confirm_final_qty("LIVE_ARMED", book_id, raw1, target_qty=qty)
            if not fin1["terminal"]:
                return self._update_mirror_fields(mirror_id, leg1_entry_oid=f"{PENDING_OID_PREFIX}{raw1}", now_iso=now_iso)
            fq1 = int(fin1["filled_qty"])
            if fq1 <= 0:
                return self._update_mirror_fields(mirror_id, state="ABORTED_CLEAN", leg1_entry_oid=raw1, error_reason="RECOVERED_LEG1_ZERO_FILL", now_iso=now_iso)
            fill1 = float(fin1["avg_price"] or row.get("leg1_entry_limit") or 0.05)
            if book_id == BOOK_ID_PAID_B and fq1 >= qty and row.get("broker_sl_oid"):
                return self._update_mirror_fields(mirror_id, state="OPEN_LONG", leg1_entry_oid=raw1, leg1_entry_fill=fill1, leg1_filled_qty=qty, now_iso=now_iso)
            # Partial Leg-1, unhedged Book A Leg-1, or Book B Leg-1 recovered WITHOUT exchange SL -> mark EXIT_RETRY_PENDING to protective-flatten!
            rec_reason = (
                "RECOVERED_UNPROTECTED_LONG_FLATTEN"
                if (book_id == BOOK_ID_PAID_B and fq1 >= qty and not row.get("broker_sl_oid"))
                else "RECOVERED_INCOMPLETE_ENTRY_UNWIND"
            )
            return self._update_mirror_fields(
                mirror_id,
                state="EXIT_RETRY_PENDING",
                leg1_entry_oid=raw1,
                leg1_entry_fill=fill1,
                leg1_filled_qty=fq1,
                exit_reason=rec_reason,
                error_reason="CRASH_RECOVERED_WITHOUT_EXCHANGE_SL" if rec_reason == "RECOVERED_UNPROTECTED_LONG_FLATTEN" else "CRASH_RECOVERED_INCOMPLETE_ENTRY",
                now_iso=now_iso,
            )

        if e2.startswith(PENDING_OID_PREFIX) or row["state"] == "LEG2_SUBMITTED":
            if not e2:
                return self._update_mirror_fields(
                    mirror_id, state="EXIT_RETRY_PENDING", exit_reason="LEG2_ABORT_UNWOUND", error_reason="STRANDED_LEG2_NO_OID", now_iso=now_iso
                )
            raw2 = e2[len(PENDING_OID_PREFIX):] if e2.startswith(PENDING_OID_PREFIX) else e2
            fin2 = self._cancel_and_confirm_final_qty("LIVE_ARMED", book_id, raw2, target_qty=qty)
            if not fin2["terminal"]:
                return self._update_mirror_fields(mirror_id, leg2_entry_oid=f"{PENDING_OID_PREFIX}{raw2}", now_iso=now_iso)
            fq2 = int(fin2["filled_qty"])
            fill2 = float(fin2["avg_price"] or row.get("leg2_entry_limit") or 0.05) if fq2 > 0 else None
            if fq2 >= qty:
                return self._update_mirror_fields(
                    mirror_id, state="OPEN_HEDGED", leg2_entry_oid=raw2, leg2_entry_fill=fill2, leg2_filled_qty=qty, now_iso=now_iso
                )
            rsn = "LEG2_PARTIAL_UNWOUND" if fq2 > 0 else "LEG2_ABORT_UNWOUND"
            return self._update_mirror_fields(
                mirror_id,
                state="EXIT_RETRY_PENDING",
                leg2_entry_oid=raw2,
                leg2_entry_fill=fill2,
                leg2_filled_qty=fq2,
                exit_reason=rsn,
                now_iso=now_iso,
            )

        return row

    # ──────────────────────────────────────────────────────────────────────────
    # BOOK A: 150PT ATOMIC BUY-FIRST / SHORT-FIRST-EXIT SPREAD
    # ──────────────────────────────────────────────────────────────────────────

    def _handle_book_a_open(self, pkg: Dict[str, Any], now_ist: datetime) -> Dict[str, Any]:
        spid = str(pkg["package_id"])
        existing = self._existing_mirror_row(spid)
        if existing is not None:
            return existing

        ls = pkg.get("legs_state") if isinstance(pkg.get("legs_state"), dict) else json.loads(pkg.get("legs_state_json") or "{}")
        wl = ls["wing_leg"]
        sl = ls["short_leg"]
        qty = abs(int(sl.get("qty") or SHADOW_LOT_QTY))
        tier, bmode = self._resolve_execution_tier_for_book(BOOK_ID_SPREAD_A)
        mirror_id = f"shmir_{spid[-10:]}"
        now_iso = now_ist.strftime("%Y-%m-%d %H:%M:%S")

        q_wing = self._get_quote(str(wl["instrument_key"]))
        q_short = self._get_quote(str(sl["instrument_key"]))

        if tier == "LIVE_ARMED":
            w_ok, w_bid, w_ask, w_qrsn = self._extract_strict_live_bid_ask(q_wing)
            s_ok, s_bid, s_ask, s_qrsn = self._extract_strict_live_bid_ask(q_short)
            quotes_valid = w_ok and s_ok
            quote_block_reason = "" if quotes_valid else f"QUOTE_HYGIENE_BLOCK:wing={w_qrsn},short={s_qrsn}"
            if not quotes_valid:
                w_bid, w_ask = self._extract_mtm_or_exit_bid_ask(q_wing, float(wl["entry_price"]))
                s_bid, s_ask = self._extract_mtm_or_exit_bid_ask(q_short, float(sl["entry_price"]))
        else:
            quotes_valid = True
            quote_block_reason = ""
            w_bid, w_ask = self._extract_mtm_or_exit_bid_ask(q_wing, float(wl["entry_price"]))
            s_bid, s_ask = self._extract_mtm_or_exit_bid_ask(q_short, float(sl["entry_price"]))

        w_limit_1 = compute_smart_limit_collar(w_ask, "BUY", stage=1)
        s_limit_1 = compute_smart_limit_collar(s_bid, "SELL", stage=1)

        verified_m = float(pkg.get("upstox_final_margin_inr") or 0.0)
        base_margin = verified_m if verified_m > 0 else 45000.0
        req_cash = max(55000.0, base_margin + (w_ask * qty) + SHADOW_MTM_CUSHION_INR)

        pf_ok, pf_reason, pf_details = self._run_entry_preflight(pkg, BOOK_ID_SPREAD_A, tier, req_cash, now_ist)
        if pf_ok and not quotes_valid:
            pf_ok = False
            pf_reason = quote_block_reason

        wing_payload = {
            "quantity": qty,
            "product": "I",
            "validity": "DAY",
            "price": float(w_limit_1),
            "tag": sanitize_upstox_tag(f"sma1-{spid[-10:]}"),
            "instrument_token": str(wl["instrument_key"]),
            "order_type": "LIMIT",
            "transaction_type": "BUY",
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
        }
        short_payload = {
            "quantity": qty,
            "product": "I",
            "validity": "DAY",
            "price": float(s_limit_1),
            "tag": sanitize_upstox_tag(f"sma2-{spid[-10:]}"),
            "instrument_token": str(sl["instrument_key"]),
            "order_type": "LIMIT",
            "transaction_type": "SELL",
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
        }

        base_row = {
            "mirror_id": mirror_id,
            "shadow_package_id": spid,
            "advance_trade_id": str(pkg.get("advance_trade_id") or ""),
            "session_date": str(pkg.get("session_date") or now_ist.strftime("%Y-%m-%d"))[:10],
            "book_id": BOOK_ID_SPREAD_A,
            "execution_tier": tier,
            "bridge_mode": bmode,
            "state": "PREFLIGHT_BLOCKED" if not pf_ok else ("DRY_RUN_OPEN" if tier == "DRY_RUN" else "LEG1_SUBMITTED"),
            "direction_label": str(pkg.get("direction_label") or f"SPREAD_SELL_{sl['option_type']}"),
            "expiry": str(pkg.get("expiry") or sl["expiry"])[:10],
            "qty": qty,
            "leg1_instrument_key": str(wl["instrument_key"]),
            "leg1_trading_symbol": str(wl.get("trading_symbol") or ""),
            "leg1_strike": float(wl["strike"]),
            "leg1_option_type": str(wl["option_type"]),
            "leg1_side": "BUY",
            "leg1_entry_oid": None,
            "leg1_entry_limit": w_limit_1,
            "leg1_entry_fill": round_to_nse_tick(w_ask + 0.05, "adverse_buy") if tier == "DRY_RUN" else None,
            "leg1_filled_qty": qty if tier == "DRY_RUN" else 0,
            "leg2_instrument_key": str(sl["instrument_key"]),
            "leg2_trading_symbol": str(sl.get("trading_symbol") or ""),
            "leg2_strike": float(sl["strike"]),
            "leg2_option_type": str(sl["option_type"]),
            "leg2_side": "SELL",
            "leg2_entry_oid": None,
            "leg2_entry_limit": s_limit_1,
            "leg2_entry_fill": round_to_nse_tick(max(0.05, s_bid - 0.05), "adverse_sell") if tier == "DRY_RUN" else None,
            "leg2_filled_qty": qty if tier == "DRY_RUN" else 0,
            "shadow_entry_ref": float(ls.get("net_entry_credit_pts") or 0.0),
            "error_reason": None if pf_ok else pf_reason,
            "preflight_json": json.dumps(pf_details),
            "payload_audit_json": json.dumps({"leg1_entry_payload": wing_payload, "leg2_entry_payload": short_payload}),
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        self._insert_mirror_row(base_row)

        if not pf_ok or tier == "DRY_RUN":
            return base_row

        # ── LIVE_ARMED STEP 1: Submit BUY Protective Wing First ──────────────
        ok1, sc1, d1 = self._broker_mutate(tier, BOOK_ID_SPREAD_A, True, "POST", "/order/place", payload=wing_payload)
        oid1 = str((d1 or {}).get("data", {}).get("order_id") or "") if (ok1 and isinstance(d1, dict) and d1.get("status") == "success") else ""
        if not oid1:
            err = str((d1 or {}).get("error") or f"LEG1_PLACE_HTTP_{sc1}")
            return self._update_mirror_fields(mirror_id, state="ABORTED_CLEAN", error_reason=err, now_iso=now_iso)

        self._update_mirror_fields(mirror_id, leg1_entry_oid=f"{PENDING_OID_PREFIX}{oid1}", now_iso=now_iso)
        poll1 = self._poll_order_until_terminal(oid1, target_qty=qty)

        if not poll1["terminal"] or poll1["status"] != "complete" or int(poll1["filled_qty"]) < qty:
            fin1 = poll1 if poll1["terminal"] else self._cancel_and_confirm_final_qty(tier, BOOK_ID_SPREAD_A, oid1, target_qty=qty)
            if not fin1["terminal"]:
                # Cancel unconfirmed -> keep in LEG1_SUBMITTED with PENDING:<oid1> so sync/watchdog resolves safely!
                return self._update_mirror_fields(
                    mirror_id, error_reason=f"LEG1_CANCEL_UNCONFIRMED:{fin1['reason']}", now_iso=now_iso
                )
            fq1 = int(fin1["filled_qty"])
            if fq1 <= 0:
                return self._update_mirror_fields(
                    mirror_id, state="ABORTED_CLEAN", leg1_entry_oid=oid1, error_reason=f"LEG1_UNFILLED:{fin1['reason']}", now_iso=now_iso
                )
            fill1 = float(fin1["avg_price"] or w_limit_1)
            self._update_mirror_fields(
                mirror_id,
                state="EXIT_RETRY_PENDING",
                leg1_entry_oid=oid1,
                leg1_entry_fill=fill1,
                leg1_filled_qty=fq1,
                exit_reason="ABORTED_PARTIAL_UNWOUND",
                error_reason=fin1["reason"],
                now_iso=now_iso,
            )
            return self._unwind_incomplete_or_close(spid, now_ist, terminal_state_override="ABORTED_PARTIAL_UNWOUND")

        actual_wing_fill = float(poll1["avg_price"] or w_ask)
        self._update_mirror_fields(
            mirror_id, state="LEG2_SUBMITTED", leg1_entry_oid=oid1, leg1_entry_fill=actual_wing_fill, leg1_filled_qty=qty, now_iso=now_iso
        )

        # ── LIVE_ARMED STEP 2: Strict Live Short Quote & Credit Floor Check ──
        q_short_now = self._get_quote(str(sl["instrument_key"]))
        s_now_ok, s_bid_now, s_ask_now, s_now_rsn = self._extract_strict_live_bid_ask(q_short_now)
        live_net_cr = round(s_bid_now - actual_wing_fill, 2) if s_now_ok else -999.0
        if (not s_now_ok) or live_net_cr < SHADOW_MIN_NET_CREDIT_PTS:
            err_msg = f"SHORT_QUOTE_INVALID:{s_now_rsn}" if not s_now_ok else f"NET_CREDIT_BELOW_14:{live_net_cr:.2f}"
            self._update_mirror_fields(
                mirror_id,
                state="EXIT_RETRY_PENDING",
                exit_reason=f"CREDIT_FLOOR_FADED:{live_net_cr:.2f}" if s_now_ok else err_msg,
                error_reason=err_msg,
                now_iso=now_iso,
            )
            return self._unwind_incomplete_or_close(spid, now_ist, terminal_state_override="LEG2_CREDIT_FADE_UNWOUND")

        short_payload["price"] = float(compute_smart_limit_collar(s_bid_now, "SELL", stage=1))
        ok2, sc2, d2 = self._broker_mutate(tier, BOOK_ID_SPREAD_A, True, "POST", "/order/place", payload=short_payload)
        oid2 = str((d2 or {}).get("data", {}).get("order_id") or "") if (ok2 and isinstance(d2, dict) and d2.get("status") == "success") else ""

        fin2: Dict[str, Any] = {"terminal": True, "status": "rejected", "filled_qty": 0, "avg_price": 0.0, "reason": f"LEG2_HTTP_{sc2}"}
        if oid2:
            self._update_mirror_fields(
                mirror_id, leg2_entry_oid=f"{PENDING_OID_PREFIX}{oid2}", leg2_entry_limit=short_payload["price"], now_iso=now_iso
            )
            poll2 = self._poll_order_until_terminal(oid2, target_qty=qty, max_attempts=max(2, self._poll_max_attempts // 2))
            if poll2["terminal"]:
                fin2 = poll2
            else:
                fin_tmp = self._cancel_and_confirm_final_qty(tier, BOOK_ID_SPREAD_A, oid2, target_qty=qty)
                # CRITICAL P0-1 FIX: ONLY attempt Stage-2 chase if `fin_tmp["terminal"] is True` AND `filled_qty == 0`!
                if not fin_tmp["terminal"]:
                    return self._update_mirror_fields(
                        mirror_id, error_reason=f"LEG2_CANCEL_UNCONFIRMED:{fin_tmp['reason']}", now_iso=now_iso
                    )
                if fin_tmp["status"] == "complete" and int(fin_tmp["filled_qty"]) >= qty:
                    fin2 = fin_tmp
                elif int(fin_tmp["filled_qty"]) == 0:
                    self._update_mirror_fields(mirror_id, leg2_entry_oid=oid2, now_iso=now_iso)
                    s2_px = compute_smart_limit_collar(s_bid_now, "SELL", stage=2)
                    if (s2_px - actual_wing_fill) >= SHADOW_MIN_NET_CREDIT_PTS:
                        chase_payload = dict(short_payload, price=float(s2_px), tag=sanitize_upstox_tag(f"sma2c-{spid[-9:]}"))
                        ok2c, _, d2c = self._broker_mutate(tier, BOOK_ID_SPREAD_A, True, "POST", "/order/place", payload=chase_payload)
                        oid2c = str((d2c or {}).get("data", {}).get("order_id") or "") if (ok2c and isinstance(d2c, dict) and d2c.get("status") == "success") else ""
                        if oid2c:
                            oid2 = oid2c
                            self._update_mirror_fields(
                                mirror_id, leg2_entry_oid=f"{PENDING_OID_PREFIX}{oid2}", leg2_entry_limit=s2_px, now_iso=now_iso
                            )
                            p2c = self._poll_order_until_terminal(oid2, target_qty=qty, max_attempts=max(2, self._poll_max_attempts // 2))
                            fin2 = p2c if p2c["terminal"] else self._cancel_and_confirm_final_qty(tier, BOOK_ID_SPREAD_A, oid2, target_qty=qty)
                            if not fin2["terminal"]:
                                return self._update_mirror_fields(
                                    mirror_id, error_reason=f"LEG2_CHASE_CANCEL_UNCONFIRMED:{fin2['reason']}", now_iso=now_iso
                                )
                        else:
                            fin2 = fin_tmp
                    else:
                        fin2 = fin_tmp
                else:
                    fin2 = fin_tmp

        fq2 = int(fin2.get("filled_qty") or 0)
        if fin2.get("status") == "complete" and fq2 >= qty:
            actual_short_fill = float(fin2["avg_price"] or s_bid_now)
            return self._update_mirror_fields(
                mirror_id,
                state="OPEN_HEDGED",
                leg2_entry_oid=oid2,
                leg2_entry_fill=actual_short_fill,
                leg2_filled_qty=qty,
                error_reason=None,
                now_iso=now_iso,
            )

        # Leg-2 rejected or partially filled -> persist exact fq2 and run atomic Short-First -> Wing-Second unwind!
        fill2_p = float(fin2.get("avg_price") or short_payload["price"]) if fq2 > 0 else None
        target_term = "LEG2_PARTIAL_UNWOUND" if fq2 > 0 else "LEG2_ABORT_UNWOUND"
        self._update_mirror_fields(
            mirror_id,
            state="EXIT_RETRY_PENDING",
            leg2_entry_oid=oid2 or None,
            leg2_entry_fill=fill2_p,
            leg2_filled_qty=fq2,
            exit_reason=target_term,
            error_reason=str(fin2.get("reason") or "LEG2_REJECTED"),
            now_iso=now_iso,
        )
        return self._unwind_incomplete_or_close(spid, now_ist, terminal_state_override=target_term)

    def _unwind_incomplete_or_close(
        self,
        spid: str,
        now_ist: datetime,
        terminal_state_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Delegates any incomplete entry unwind through `_handle_package_close` (guaranteeing Short-First
        before Wing, settled-only partial fill tracking, zero synthetic prices, and `EXIT_RETRY_PENDING`
        on any transient broker failure), applying `terminal_state_override` ONLY once 100% flat!
        """
        row = self._existing_mirror_row(spid)
        if not row:
            return {}
        res = self._handle_package_close({"package_id": spid, "book_id": row["book_id"]}, now_ist, override_reason=row.get("exit_reason"))
        if res and res.get("state") == "CONFIRMED_FLAT" and terminal_state_override:
            return self._update_mirror_fields(
                str(res["mirror_id"]),
                state=terminal_state_override,
                now_iso=now_ist.strftime("%Y-%m-%d %H:%M:%S"),
            )
        return res or row

    # ──────────────────────────────────────────────────────────────────────────
    # BOOK B: 1-LOT PAID LONG + VERIFIED SL-L + CANCEL-FIRST ANTI-DOUBLE-SELL
    # ──────────────────────────────────────────────────────────────────────────

    def _handle_book_b_open(self, pkg: Dict[str, Any], now_ist: datetime) -> Dict[str, Any]:
        spid = str(pkg["package_id"])
        existing = self._existing_mirror_row(spid)
        if existing is not None:
            return existing

        ls = pkg.get("legs_state") if isinstance(pkg.get("legs_state"), dict) else json.loads(pkg.get("legs_state_json") or "{}")
        ll = ls["long_leg"]
        qty = abs(int(ll.get("qty") or SHADOW_LOT_QTY))
        sl_pct = float(ls.get("stop_loss_pct") or 22.0)
        tier, bmode = self._resolve_execution_tier_for_book(BOOK_ID_PAID_B)
        mirror_id = f"shmir_{spid[-10:]}"
        now_iso = now_ist.strftime("%Y-%m-%d %H:%M:%S")

        q_long = self._get_quote(str(ll["instrument_key"]))
        if tier == "LIVE_ARMED":
            q_ok, l_bid, l_ask, q_rsn = self._extract_strict_live_bid_ask(q_long)
            if not q_ok:
                l_bid, l_ask = self._extract_mtm_or_exit_bid_ask(q_long, float(ll["entry_price"]))
        else:
            q_ok, q_rsn = True, "OK"
            l_bid, l_ask = self._extract_mtm_or_exit_bid_ask(q_long, float(ll["entry_price"]))

        l_limit_1 = compute_smart_limit_collar(l_ask, "BUY", stage=1)
        est_outlay = round(l_ask * qty, 2)

        req_cash = max(22000.0, est_outlay + SHADOW_MTM_CUSHION_INR)
        pf_ok, pf_reason, pf_details = self._run_entry_preflight(pkg, BOOK_ID_PAID_B, tier, req_cash, now_ist)
        if pf_ok and not q_ok:
            pf_ok = False
            pf_reason = f"QUOTE_HYGIENE_BLOCK:{q_rsn}"
        if pf_ok and tier == "LIVE_ARMED" and est_outlay > SHADOW_BOOK_B_MAX_OUTLAY_INR * 1.04:
            pf_ok = False
            pf_reason = f"BOOK_B_OUTLAY_CAP_EXCEEDED:{est_outlay:.2f}"

        entry_payload = {
            "quantity": qty,
            "product": "I",
            "validity": "DAY",
            "price": float(l_limit_1),
            "tag": sanitize_upstox_tag(f"smb1-{spid[-10:]}"),
            "instrument_token": str(ll["instrument_key"]),
            "order_type": "LIMIT",
            "transaction_type": "BUY",
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
        }
        dry_fill = round_to_nse_tick(l_ask + 0.05, "adverse_buy")
        dry_sl_trig = round_to_nse_tick(dry_fill * (1.0 - sl_pct / 100.0), "adverse_sell")
        dry_sl_lim = round_to_nse_tick(max(0.05, dry_sl_trig * 0.985), "adverse_sell")

        base_row = {
            "mirror_id": mirror_id,
            "shadow_package_id": spid,
            "advance_trade_id": str(pkg.get("advance_trade_id") or ""),
            "session_date": str(pkg.get("session_date") or now_ist.strftime("%Y-%m-%d"))[:10],
            "book_id": BOOK_ID_PAID_B,
            "execution_tier": tier,
            "bridge_mode": bmode,
            "state": "PREFLIGHT_BLOCKED" if not pf_ok else ("DRY_RUN_OPEN" if tier == "DRY_RUN" else "LEG1_SUBMITTED"),
            "direction_label": str(pkg.get("direction_label") or f"BUY_{ll['option_type']}"),
            "expiry": str(pkg.get("expiry") or ll["expiry"])[:10],
            "qty": qty,
            "leg1_instrument_key": str(ll["instrument_key"]),
            "leg1_trading_symbol": str(ll.get("trading_symbol") or ""),
            "leg1_strike": float(ll["strike"]),
            "leg1_option_type": str(ll["option_type"]),
            "leg1_side": "BUY",
            "leg1_entry_oid": None,
            "leg1_entry_limit": l_limit_1,
            "leg1_entry_fill": dry_fill if tier == "DRY_RUN" else None,
            "leg1_filled_qty": qty if tier == "DRY_RUN" else 0,
            "broker_sl_oid": None,
            "broker_sl_trigger": dry_sl_trig if tier == "DRY_RUN" else None,
            "broker_sl_limit": dry_sl_lim if tier == "DRY_RUN" else None,
            "broker_sl_status": "DRY_RUN_PLANNED" if tier == "DRY_RUN" else None,
            "shadow_entry_ref": float(ll["entry_price"]),
            "error_reason": None if pf_ok else pf_reason,
            "preflight_json": json.dumps({**pf_details, "stop_loss_pct": sl_pct}),
            "payload_audit_json": json.dumps({"entry_payload": entry_payload}),
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        self._insert_mirror_row(base_row)

        if not pf_ok or tier == "DRY_RUN":
            return base_row

        ok, sc, d = self._broker_mutate(tier, BOOK_ID_PAID_B, True, "POST", "/order/place", payload=entry_payload)
        oid = str((d or {}).get("data", {}).get("order_id") or "") if (ok and isinstance(d, dict) and d.get("status") == "success") else ""
        if not oid:
            return self._update_mirror_fields(
                mirror_id, state="ABORTED_CLEAN", error_reason=str((d or {}).get("error") or f"ENTRY_HTTP_{sc}"), now_iso=now_iso
            )

        self._update_mirror_fields(mirror_id, leg1_entry_oid=f"{PENDING_OID_PREFIX}{oid}", now_iso=now_iso)
        poll = self._poll_order_until_terminal(oid, target_qty=qty)
        if not poll["terminal"] or poll["status"] != "complete" or int(poll["filled_qty"]) < qty:
            fin = poll if poll["terminal"] else self._cancel_and_confirm_final_qty(tier, BOOK_ID_PAID_B, oid, target_qty=qty)
            if not fin["terminal"]:
                return self._update_mirror_fields(
                    mirror_id, error_reason=f"ENTRY_CANCEL_UNCONFIRMED:{fin['reason']}", now_iso=now_iso
                )
            fq = int(fin["filled_qty"])
            if fq <= 0:
                return self._update_mirror_fields(
                    mirror_id, state="ABORTED_CLEAN", leg1_entry_oid=oid, error_reason=f"ENTRY_UNFILLED:{fin['reason']}", now_iso=now_iso
                )
            fill_p = float(fin["avg_price"] or l_limit_1)
            self._update_mirror_fields(
                mirror_id,
                state="EXIT_RETRY_PENDING",
                leg1_entry_oid=oid,
                leg1_entry_fill=fill_p,
                leg1_filled_qty=fq,
                exit_reason="ABORTED_PARTIAL_UNWOUND",
                error_reason=fin["reason"],
                now_iso=now_iso,
            )
            return self._unwind_incomplete_or_close(spid, now_ist, terminal_state_override="ABORTED_PARTIAL_UNWOUND")

        actual_fill = float(poll["avg_price"] or l_ask)
        sl_trig = round_to_nse_tick(actual_fill * (1.0 - sl_pct / 100.0), "adverse_sell")
        sl_lim = round_to_nse_tick(max(0.05, sl_trig * 0.985), "adverse_sell")
        sl_payload = {
            "quantity": qty,
            "product": "I",
            "validity": "DAY",
            "price": float(sl_lim),
            "tag": sanitize_upstox_tag(f"smbsl-{spid[-9:]}"),
            "instrument_token": str(ll["instrument_key"]),
            "order_type": "SL",
            "transaction_type": "SELL",
            "disclosed_quantity": 0,
            "trigger_price": float(sl_trig),
            "is_amo": False,
        }
        sl_ok, _, sl_d = self._broker_mutate(tier, BOOK_ID_PAID_B, False, "POST", "/order/place", payload=sl_payload)
        sl_oid = str((sl_d or {}).get("data", {}).get("order_id") or "") if (sl_ok and isinstance(sl_d, dict) and sl_d.get("status") == "success") else None

        # P1-2 FIX: Immediately verify SL status on Upstox so asynchronous SL rejection (`trigger >= LTP`) never leaves an unprotected live long!
        if sl_oid:
            sl_check = self._poll_order_until_terminal(sl_oid, target_qty=qty, max_attempts=1)
            if sl_check["status"] == "complete" and int(sl_check["filled_qty"]) >= qty:
                sl_px = float(sl_check["avg_price"] or sl_trig)
                acc = compute_package_accounting(
                    legs=[OptionLeg(str(ll["option_type"]), float(ll["strike"]), qty, actual_fill, sl_px, "MARKET_ORDER")],
                    trade_date=base_row["session_date"],
                )
                return self._update_mirror_fields(
                    mirror_id,
                    state="CONFIRMED_FLAT",
                    leg1_entry_oid=oid,
                    leg1_entry_fill=actual_fill,
                    leg1_filled_qty=qty,
                    leg1_exit_oid=sl_oid,
                    leg1_exit_fill=sl_px,
                    leg1_exited_qty=qty,
                    broker_sl_oid=sl_oid,
                    broker_sl_trigger=sl_trig,
                    broker_sl_limit=sl_lim,
                    broker_sl_status="FILLED_ON_EXCHANGE",
                    gross_pnl_inr=float(acc["gross_pnl"]),
                    statutory_charges_inr=float(acc["total_friction"]),
                    realized_net_pnl_inr=float(acc["net_pnl"]),
                    exit_reason="BROKER_SL_IMMEDIATE_FILL",
                    now_iso=now_iso,
                )
            if sl_check["status"] in ("rejected", "cancelled"):
                sl_oid = None

        if not sl_oid:
            # Exchange rejected or failed protective SL -> immediately execute protective flatten!
            self._update_mirror_fields(
                mirror_id,
                state="EXIT_RETRY_PENDING",
                leg1_entry_oid=oid,
                leg1_entry_fill=actual_fill,
                leg1_filled_qty=qty,
                broker_sl_oid=None,
                broker_sl_status="SL_REJECTED_FLATTENING",
                exit_reason="SL_REJECTED_PROTECTIVE_FLATTEN",
                error_reason="EXCHANGE_SL_REJECTED",
                now_iso=now_iso,
            )
            return self._handle_package_close({"package_id": spid, "book_id": BOOK_ID_PAID_B}, now_ist, override_reason="SL_REJECTED_PROTECTIVE_FLATTEN")

        return self._update_mirror_fields(
            mirror_id,
            state="OPEN_LONG",
            leg1_entry_oid=oid,
            leg1_entry_fill=actual_fill,
            leg1_filled_qty=qty,
            broker_sl_oid=sl_oid,
            broker_sl_trigger=sl_trig,
            broker_sl_limit=sl_lim,
            broker_sl_status="OPEN",
            now_iso=now_iso,
        )

    def _handle_book_b_ratchet(self, pkg: Dict[str, Any], now_ist: datetime) -> Optional[Dict[str, Any]]:
        spid = str(pkg["package_id"])
        row = self._existing_mirror_row(spid)
        if not row:
            return None
        ls = pkg.get("legs_state") if isinstance(pkg.get("legs_state"), dict) else json.loads(pkg.get("legs_state_json") or "{}")
        raw_r = ls.get("ratchet_stop")
        if raw_r is None:
            return row

        new_trig = round_to_nse_tick(float(raw_r), "adverse_sell")
        cur_trig = float(row.get("broker_sl_trigger") or 0.0)
        if new_trig <= cur_trig + 0.049:
            return row

        new_lim = round_to_nse_tick(max(0.05, new_trig * 0.985), "adverse_sell")
        now_iso = now_ist.strftime("%Y-%m-%d %H:%M:%S")
        mirror_id = str(row["mirror_id"])

        if row["execution_tier"] == "DRY_RUN" and row["state"] == "DRY_RUN_OPEN":
            return self._update_mirror_fields(
                mirror_id,
                broker_sl_trigger=new_trig,
                broker_sl_limit=new_lim,
                broker_sl_status="DRY_RUN_RATCHETED",
                now_iso=now_iso,
            )

        if row["execution_tier"] != "LIVE_ARMED" or row["state"] != "OPEN_LONG" or not row.get("broker_sl_oid"):
            return row

        now_ep = time.time()
        last_mod = self._last_ratchet_modify_epoch.get(mirror_id, 0.0)
        if (now_ep - last_mod) < 5.0:
            return row

        mod_payload = {
            "order_id": str(row["broker_sl_oid"]),
            "quantity": int(row["qty"]),
            "validity": "DAY",
            "price": float(new_lim),
            "order_type": "SL",
            "disclosed_quantity": 0,
            "trigger_price": float(new_trig),
        }
        ok, _, d = self._broker_mutate(
            "LIVE_ARMED", BOOK_ID_PAID_B, False, "PUT", "/order/modify", payload=mod_payload
        )
        if ok and isinstance(d, dict) and d.get("status") == "success":
            self._last_ratchet_modify_epoch[mirror_id] = now_ep
            return self._update_mirror_fields(
                mirror_id,
                broker_sl_trigger=new_trig,
                broker_sl_limit=new_lim,
                broker_sl_status="RATCHET_MODIFIED",
                now_iso=now_iso,
            )
        return row

    # ──────────────────────────────────────────────────────────────────────────
    # UNIFIED PACKAGE EXIT (SHORT-FIRST FOR A; CANCEL-SL-FIRST FOR B)
    # ──────────────────────────────────────────────────────────────────────────

    def _handle_package_close(
        self,
        pkg: Dict[str, Any],
        now_ist: datetime,
        override_reason: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        spid = str(pkg["package_id"])
        row = self._existing_mirror_row(spid)
        if not row:
            return None
        if row["state"] in TERMINAL_STATES:
            return row

        # First settle any in-flight `LEG1_SUBMITTED` / `LEG2_SUBMITTED` / `PENDING:<entry_oid>`!
        if row["execution_tier"] == "LIVE_ARMED" and (
            row["state"] in TRANSITIONAL_ENTRY_STATES
            or str(row.get("leg1_entry_oid") or "").startswith(PENDING_OID_PREFIX)
            or str(row.get("leg2_entry_oid") or "").startswith(PENDING_OID_PREFIX)
        ):
            row = self._reconcile_in_flight_entries_for_row(spid, now_ist)
            if not row or row["state"] in TERMINAL_STATES:
                return row
            if (
                str(row.get("leg1_entry_oid") or "").startswith(PENDING_OID_PREFIX)
                or str(row.get("leg2_entry_oid") or "").startswith(PENDING_OID_PREFIX)
            ):
                return row

        mirror_id = str(row["mirror_id"])
        now_iso = now_ist.strftime("%Y-%m-%d %H:%M:%S")
        exit_rsn = str(override_reason or pkg.get("exit_reason") or row.get("exit_reason") or "SHADOW_MIRROR_EXIT")
        sh_net = float(pkg.get("net_pnl_inr")) if pkg.get("net_pnl_inr") is not None else row.get("shadow_net_pnl_inr")

        # ── DRY_RUN CLOSE ─────────────────────────────────────────────────────
        if row["execution_tier"] == "DRY_RUN":
            if row["state"] != "DRY_RUN_OPEN":
                return row
            if row["book_id"] == BOOK_ID_SPREAD_A:
                qs = self._get_quote(str(row["leg2_instrument_key"]))
                qw = self._get_quote(str(row["leg1_instrument_key"]))
                _, s_ask = self._extract_mtm_or_exit_bid_ask(qs, float(row["leg2_entry_fill"] or 20.0))
                w_bid, _ = self._extract_mtm_or_exit_bid_ask(qw, float(row["leg1_entry_fill"] or 5.0))
                s_ex = round_to_nse_tick(s_ask + 0.05, "adverse_buy")
                w_ex = round_to_nse_tick(max(0.05, w_bid - 0.05), "adverse_sell")
                acc = compute_package_accounting(
                    legs=[
                        OptionLeg(str(row["leg1_option_type"]), float(row["leg1_strike"]), int(row["qty"]), float(row["leg1_entry_fill"]), w_ex, "MARKET_ORDER"),
                        OptionLeg(str(row["leg2_option_type"]), float(row["leg2_strike"]), -int(row["qty"]), float(row["leg2_entry_fill"]), s_ex, "MARKET_ORDER"),
                    ],
                    trade_date=str(row["session_date"]),
                )
                drift = round(float(acc["net_pnl"]) - float(sh_net), 2) if sh_net is not None else None
                return self._update_mirror_fields(
                    mirror_id,
                    state="DRY_RUN_CLOSED",
                    leg1_exit_fill=w_ex,
                    leg1_exited_qty=int(row["qty"]),
                    leg2_exit_fill=s_ex,
                    leg2_exited_qty=int(row["qty"]),
                    shadow_net_pnl_inr=sh_net,
                    gross_pnl_inr=float(acc["gross_pnl"]),
                    statutory_charges_inr=float(acc["total_friction"]),
                    realized_net_pnl_inr=float(acc["net_pnl"]),
                    live_minus_shadow_drift_inr=drift,
                    exit_reason=exit_rsn,
                    now_iso=now_iso,
                )
            ql = self._get_quote(str(row["leg1_instrument_key"]))
            l_bid, _ = self._extract_mtm_or_exit_bid_ask(ql, float(row["leg1_entry_fill"] or 50.0))
            l_ex = round_to_nse_tick(max(0.05, l_bid - 0.05), "adverse_sell")
            acc = compute_package_accounting(
                legs=[OptionLeg(str(row["leg1_option_type"]), float(row["leg1_strike"]), int(row["qty"]), float(row["leg1_entry_fill"]), l_ex, "MARKET_ORDER")],
                trade_date=str(row["session_date"]),
            )
            drift = round(float(acc["net_pnl"]) - float(sh_net), 2) if sh_net is not None else None
            return self._update_mirror_fields(
                mirror_id,
                state="DRY_RUN_CLOSED",
                leg1_exit_fill=l_ex,
                leg1_exited_qty=int(row["qty"]),
                broker_sl_status="DRY_RUN_CANCELLED",
                shadow_net_pnl_inr=sh_net,
                gross_pnl_inr=float(acc["gross_pnl"]),
                statutory_charges_inr=float(acc["total_friction"]),
                realized_net_pnl_inr=float(acc["net_pnl"]),
                live_minus_shadow_drift_inr=drift,
                exit_reason=exit_rsn,
                now_iso=now_iso,
            )

        # ── LIVE_ARMED BOOK A EXIT (STRICT SHORT-FIRST -> THEN WING) ──────────
        if row["book_id"] == BOOK_ID_SPREAD_A:
            s_filled_total = int(row["leg2_filled_qty"] or 0)
            w_filled_total = int(row["leg1_filled_qty"] or 0)

            # Step 1: Close 100% of filled short contracts FIRST!
            if s_filled_total > int(row["leg2_exited_qty"] or 0) or str(row.get("leg2_exit_oid") or "").startswith(PENDING_OID_PREFIX):
                self._update_mirror_fields(mirror_id, state="EXITING_SHORT", exit_reason=exit_rsn, now_iso=now_iso)
                s_res = self._execute_settled_exit_leg(
                    mirror_id=mirror_id,
                    spid=spid,
                    book_id=BOOK_ID_SPREAD_A,
                    leg_index=2,
                    ikey=str(row["leg2_instrument_key"]),
                    side="BUY",
                    target_total_qty=s_filled_total,
                    fallback_px=float(row["leg2_entry_fill"] or 30.0),
                    stages=(1, 2, 3),
                    tag_prefix="smasx",
                    now_iso=now_iso,
                )
                # HARD INVARIANT: NEVER touch long protective wing while short `cum_qty < s_filled_total`!
                if not s_res["ok"] or int(s_res["cum_qty"]) < s_filled_total:
                    return self._update_mirror_fields(
                        mirror_id,
                        state="EXIT_RETRY_PENDING",
                        exit_reason=exit_rsn,
                        error_reason=f"SHORT_EXIT_PENDING_WING_INTACT:{s_res['reason']}",
                        now_iso=now_iso,
                    )

            # Step 2: Now and ONLY now (`short` 100% closed), sell 100% of filled wing contracts!
            if w_filled_total > 0:
                self._update_mirror_fields(mirror_id, state="EXITING_WING", exit_reason=exit_rsn, now_iso=now_iso)
                w_res = self._execute_settled_exit_leg(
                    mirror_id=mirror_id,
                    spid=spid,
                    book_id=BOOK_ID_SPREAD_A,
                    leg_index=1,
                    ikey=str(row["leg1_instrument_key"]),
                    side="SELL",
                    target_total_qty=w_filled_total,
                    fallback_px=float(row["leg1_entry_fill"] or 10.0),
                    stages=(1, 2, 3),
                    tag_prefix="smawx",
                    now_iso=now_iso,
                )
                if not w_res["ok"] or int(w_res["cum_qty"]) < w_filled_total:
                    return self._update_mirror_fields(
                        mirror_id,
                        state="EXIT_RETRY_PENDING",
                        exit_reason=exit_rsn,
                        error_reason=f"WING_EXIT_PENDING:{w_res['reason']}",
                        now_iso=now_iso,
                    )

            fresh = self._existing_mirror_row(spid) or row
            legs_for_acc: List[OptionLeg] = []
            if w_filled_total > 0:
                legs_for_acc.append(
                    OptionLeg(
                        str(fresh["leg1_option_type"]),
                        float(fresh["leg1_strike"]),
                        w_filled_total,
                        float(fresh["leg1_entry_fill"]),
                        float(fresh["leg1_exit_fill"]),
                        "MARKET_ORDER",
                    )
                )
            if s_filled_total > 0:
                legs_for_acc.append(
                    OptionLeg(
                        str(fresh["leg2_option_type"]),
                        float(fresh["leg2_strike"]),
                        -s_filled_total,
                        float(fresh["leg2_entry_fill"]),
                        float(fresh["leg2_exit_fill"]),
                        "MARKET_ORDER",
                    )
                )
            acc = compute_package_accounting(legs=legs_for_acc, trade_date=str(fresh["session_date"]))
            drift = round(float(acc["net_pnl"]) - float(sh_net), 2) if sh_net is not None else None
            return self._update_mirror_fields(
                mirror_id,
                state="CONFIRMED_FLAT",
                shadow_net_pnl_inr=sh_net,
                gross_pnl_inr=float(acc["gross_pnl"]),
                statutory_charges_inr=float(acc["total_friction"]),
                realized_net_pnl_inr=float(acc["net_pnl"]),
                live_minus_shadow_drift_inr=drift,
                exit_reason=exit_rsn,
                error_reason=None,
                now_iso=now_iso,
            )

        # ── LIVE_ARMED BOOK B EXIT (CANCEL SL-L -> PERSIST PARTIAL SL -> SELL REMAINDER) ──
        l_filled_total = int(row["leg1_filled_qty"] or 0)
        sl_oid = row.get("broker_sl_oid")
        sl_status_final = str(row.get("broker_sl_status") or "")

        if sl_oid and sl_status_final not in ("CANCELLED_CONFIRMED", "FILLED_ON_EXCHANGE"):
            fin_sl = self._cancel_and_confirm_final_qty("LIVE_ARMED", BOOK_ID_PAID_B, str(sl_oid), target_qty=l_filled_total)
            if not fin_sl["terminal"]:
                # Fail-closed: NEVER fire software SELL while resting SL cancel is unconfirmed!
                return self._update_mirror_fields(
                    mirror_id,
                    state="EXIT_RETRY_PENDING",
                    exit_reason=exit_rsn,
                    error_reason="SL_CANCEL_UNCONFIRMED_RETRY_PENDING",
                    now_iso=now_iso,
                )

            sl_fq = min(l_filled_total, max(0, int(fin_sl.get("filled_qty") or 0)))
            sl_px = float(fin_sl.get("avg_price") or row.get("broker_sl_trigger") or row.get("leg1_entry_fill") or 0.05)

            if fin_sl["status"] == "complete" and sl_fq >= l_filled_total:
                acc = compute_package_accounting(
                    legs=[OptionLeg(str(row["leg1_option_type"]), float(row["leg1_strike"]), l_filled_total, float(row["leg1_entry_fill"]), sl_px, "MARKET_ORDER")],
                    trade_date=str(row["session_date"]),
                )
                drift = round(float(acc["net_pnl"]) - float(sh_net), 2) if sh_net is not None else None
                return self._update_mirror_fields(
                    mirror_id,
                    state="CONFIRMED_FLAT",
                    leg1_exit_oid=str(sl_oid),
                    leg1_exit_fill=sl_px,
                    leg1_exited_qty=l_filled_total,
                    broker_sl_status="FILLED_ON_EXCHANGE",
                    shadow_net_pnl_inr=sh_net,
                    gross_pnl_inr=float(acc["gross_pnl"]),
                    statutory_charges_inr=float(acc["total_friction"]),
                    realized_net_pnl_inr=float(acc["net_pnl"]),
                    live_minus_shadow_drift_inr=drift,
                    exit_reason=f"BROKER_SL_FILLED ({exit_rsn})",
                    error_reason=None,
                    now_iso=now_iso,
                )

            # P0-2 FIX: Atomically persist any partial SL fill (`sl_fq > 0`) into `leg1_exited_qty` + `leg1_exit_fill`
            # BEFORE marking `CANCELLED_CONFIRMED`, so retries NEVER forget `sl_fq` and double-sell!
            sl_status_final = "CANCELLED_CONFIRMED"
            prev_ex_q = int(row.get("leg1_exited_qty") or 0)
            prev_ex_px = float(row.get("leg1_exit_fill") or 0.0)
            new_ex_q = min(l_filled_total, prev_ex_q + sl_fq)
            if sl_fq > 0 and new_ex_q > 0:
                new_ex_px = round(((prev_ex_q * prev_ex_px) + (sl_fq * sl_px)) / float(new_ex_q), 2)
            else:
                new_ex_px = prev_ex_px if prev_ex_q > 0 else None
            row = self._update_mirror_fields(
                mirror_id,
                broker_sl_status=sl_status_final,
                leg1_exited_qty=new_ex_q,
                leg1_exit_fill=new_ex_px,
                now_iso=now_iso,
            )

        if l_filled_total > int(row.get("leg1_exited_qty") or 0) or str(row.get("leg1_exit_oid") or "").startswith(PENDING_OID_PREFIX):
            self._update_mirror_fields(mirror_id, state="EXITING_LONG", exit_reason=exit_rsn, now_iso=now_iso)
            l_res = self._execute_settled_exit_leg(
                mirror_id=mirror_id,
                spid=spid,
                book_id=BOOK_ID_PAID_B,
                leg_index=1,
                ikey=str(row["leg1_instrument_key"]),
                side="SELL",
                target_total_qty=l_filled_total,
                fallback_px=float(row["leg1_entry_fill"] or 50.0),
                stages=(1, 2, 3),
                tag_prefix="smbex",
                now_iso=now_iso,
            )
            if not l_res["ok"] or int(l_res["cum_qty"]) < l_filled_total:
                return self._update_mirror_fields(
                    mirror_id,
                    state="EXIT_RETRY_PENDING",
                    exit_reason=exit_rsn,
                    error_reason=f"LONG_EXIT_PENDING:{l_res['reason']}",
                    now_iso=now_iso,
                )

        fresh_b = self._existing_mirror_row(spid) or row
        acc_b = compute_package_accounting(
            legs=[
                OptionLeg(
                    str(fresh_b["leg1_option_type"]),
                    float(fresh_b["leg1_strike"]),
                    l_filled_total,
                    float(fresh_b["leg1_entry_fill"]),
                    float(fresh_b["leg1_exit_fill"]),
                    "MARKET_ORDER",
                )
            ],
            trade_date=str(fresh_b["session_date"]),
        )
        drift_b = round(float(acc_b["net_pnl"]) - float(sh_net), 2) if sh_net is not None else None
        return self._update_mirror_fields(
            mirror_id,
            state="CONFIRMED_FLAT",
            broker_sl_status=sl_status_final or "NONE",
            shadow_net_pnl_inr=sh_net,
            gross_pnl_inr=float(acc_b["gross_pnl"]),
            statutory_charges_inr=float(acc_b["total_friction"]),
            realized_net_pnl_inr=float(acc_b["net_pnl"]),
            live_minus_shadow_drift_inr=drift_b,
            exit_reason=exit_rsn,
            error_reason=None,
            now_iso=now_iso,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # CONTINUOUS RECONCILIATION, 5s ORPHAN WATCHDOG & SOS FLATTEN
    # ──────────────────────────────────────────────────────────────────────────

    def sync_with_shadow_db_once(self, now_dt: Optional[datetime] = None) -> Dict[str, Any]:
        now_ep = time.time()
        if (now_ep - self._last_db_sync_epoch) < 1.0 and now_dt is None and not self._boot_recovery_pending:
            return {"status": "THROTTLED"}
        self._last_db_sync_epoch = now_ep
        now_ist = to_ist_naive(now_dt)
        today_s = now_ist.strftime("%Y-%m-%d")

        actions: List[str] = []
        try:
            with self._get_conn(readonly=True) as conn:
                has_sh_tbl = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='option_sniffer_advance_s205_shadow_packages';"
                ).fetchone()
                sh_rows = (
                    conn.execute(
                        "SELECT * FROM option_sniffer_advance_s205_shadow_packages WHERE session_date = ? ORDER BY entry_time ASC;",
                        (today_s,),
                    ).fetchall()
                    if has_sh_tbl
                    else []
                )
                placeholders = ",".join(["?"] * len(ACTIVE_LIVE_STATES))
                open_live_rows = conn.execute(
                    f"SELECT * FROM option_advance_live_shadow_packages WHERE execution_tier = 'LIVE_ARMED' AND state IN ({placeholders});",
                    ACTIVE_LIVE_STATES,
                ).fetchall()
                open_dry_rows = conn.execute(
                    "SELECT * FROM option_advance_live_shadow_packages WHERE execution_tier = 'DRY_RUN' AND state = 'DRY_RUN_OPEN';"
                ).fetchall()

            sh_map = {str(r["package_id"]): dict(r) for r in sh_rows}

            # 0. Boot / Interrupted Transitional State Recovery (P1-4 Fix)
            for r in open_live_rows:
                rd = dict(r)
                spid = str(rd["shadow_package_id"])
                if (
                    self._boot_recovery_pending
                    or rd["state"] in TRANSITIONAL_ENTRY_STATES
                    or str(rd.get("leg1_entry_oid") or "").startswith(PENDING_OID_PREFIX)
                    or str(rd.get("leg2_entry_oid") or "").startswith(PENDING_OID_PREFIX)
                ):
                    with self._get_pkg_lock(spid):
                        self._reconcile_in_flight_entries_for_row(spid, now_ist)
                    actions.append(f"RECOVER_TRANSITIONAL:{spid}")
            self._boot_recovery_pending = False

            # 1. Catch any missed OPEN, BOOK_B_RATCHET, or CLOSED transitions
            for spid, sdict in sh_map.items():
                mir = self._existing_mirror_row(spid)
                if mir is None and sdict.get("state") == "OPEN":
                    ev = "BOOK_A_OPEN" if sdict.get("book_id") == BOOK_ID_SPREAD_A else "BOOK_B_OPEN"
                    self.process_shadow_event_sync(ev, sdict, now_dt=now_ist)
                    actions.append(f"SYNC_OPEN:{spid}")
                elif mir is not None and mir["state"] not in TERMINAL_STATES:
                    if sdict.get("state") == "CLOSED":
                        ev = "BOOK_A_CLOSE" if sdict.get("book_id") == BOOK_ID_SPREAD_A else "BOOK_B_CLOSE"
                        self.process_shadow_event_sync(ev, sdict, now_dt=now_ist)
                        actions.append(f"SYNC_CLOSE:{spid}")
                    elif sdict.get("state") == "OPEN" and sdict.get("book_id") == BOOK_ID_PAID_B:
                        # P2-2 Fix: Reconcile any missed Book B ratchet update!
                        try:
                            ls_s = json.loads(sdict.get("legs_state_json") or "{}")
                            r_stop = ls_s.get("ratchet_stop")
                            if r_stop is not None and float(r_stop) > float(mir.get("broker_sl_trigger") or 0.0) + 0.049:
                                self.process_shadow_event_sync("BOOK_B_RATCHET", {**sdict, "legs_state": ls_s}, now_dt=now_ist)
                                actions.append(f"SYNC_RATCHET:{spid}")
                        except Exception:
                            pass

            # 2. Retry any EXIT_RETRY_PENDING / EXITING_* or enforce 15:15 IST EOD hard cutoff
            with self._get_conn(readonly=True) as conn:
                refreshed_live = conn.execute(
                    f"SELECT * FROM option_advance_live_shadow_packages WHERE execution_tier = 'LIVE_ARMED' AND state IN ({placeholders});",
                    ACTIVE_LIVE_STATES,
                ).fetchall()
            is_eod = now_ist.time() >= dt_time(15, 15)
            for r in list(refreshed_live) + list(open_dry_rows):
                rdict = dict(r)
                spid = str(rdict["shadow_package_id"])
                if rdict["state"] in ("EXIT_RETRY_PENDING", "EXITING_SHORT", "EXITING_WING", "EXITING_LONG") or is_eod:
                    src_pkg = sh_map.get(spid) or {"package_id": spid, "book_id": rdict["book_id"]}
                    rsn = "EOD_1515_FAILSAFE_EXIT" if is_eod and rdict["state"] in ("OPEN_HEDGED", "OPEN_LONG", "DRY_RUN_OPEN") else (rdict.get("exit_reason") or "EXIT_RETRY")
                    with self._get_pkg_lock(spid):
                        r_out = self._handle_package_close(src_pkg, now_ist, override_reason=rsn)
                        # Preserve incomplete-leg abort state labels once flat if triggered by entry abort
                        orig_rsn = str(rdict.get("exit_reason") or "")
                        if r_out and r_out.get("state") == "CONFIRMED_FLAT" and orig_rsn in (
                            "ABORTED_PARTIAL_UNWOUND",
                            "LEG2_CREDIT_FADE_UNWOUND",
                            "LEG2_ABORT_UNWOUND",
                            "LEG2_PARTIAL_UNWOUND",
                        ):
                            self._update_mirror_fields(str(r_out["mirror_id"]), state=orig_rsn, now_iso=now_ist.strftime("%Y-%m-%d %H:%M:%S"))
                    actions.append(f"RETRY_OR_EOD:{spid}")

            # 3. 5s Broker Position Watchdog (ONLY when at least 1 LIVE_ARMED package is active)
            if refreshed_live and ((now_ep - self._last_watchdog_epoch) >= 5.0 or now_dt is not None):
                self._last_watchdog_epoch = now_ep
                wd_actions = self._run_broker_position_watchdog([dict(r) for r in refreshed_live], now_ist)
                actions.extend(wd_actions)
        except Exception as e:
            logger.debug(f"[ShadowMirrorExecutor] sync_with_shadow_db_once note: {e}")
        finally:
            self._flush_pending_disarms()

        return {"status": "OK", "actions": actions}

    def _run_broker_position_watchdog(
        self,
        open_live_rows: List[Dict[str, Any]],
        now_ist: datetime,
    ) -> List[str]:
        notes: List[str] = []
        ok, _, pdata = self._broker_query("/portfolio/short-term-positions", is_emergency=False)
        if not ok or not isinstance(pdata, dict) or not isinstance(pdata.get("data"), list):
            return notes

        pos_qty_by_ikey: Dict[str, int] = {}
        pos_sell_px_by_ikey: Dict[str, float] = {}
        pos_buy_px_by_ikey: Dict[str, float] = {}
        for item in pdata["data"]:
            if isinstance(item, dict):
                ik = str(item.get("instrument_token") or item.get("instrument_key") or "")
                q = int(item.get("quantity") or 0)
                if ik:
                    pos_qty_by_ikey[ik] = q
                    if float(item.get("sell_price") or 0.0) > 0:
                        pos_sell_px_by_ikey[ik] = float(item["sell_price"])
                    if float(item.get("buy_price") or 0.0) > 0:
                        pos_buy_px_by_ikey[ik] = float(item["buy_price"])

        now_iso = now_ist.strftime("%Y-%m-%d %H:%M:%S")
        disarm_triggers: List[str] = []

        for row in open_live_rows:
            if row["state"] not in ("OPEN_HEDGED", "OPEN_LONG"):
                continue
            spid = str(row["shadow_package_id"])
            mirror_id = str(row["mirror_id"])
            with self._get_pkg_lock(spid):
                fresh = self._existing_mirror_row(spid)
                if not fresh or fresh["state"] not in ("OPEN_HEDGED", "OPEN_LONG"):
                    continue

                if fresh["book_id"] == BOOK_ID_SPREAD_A:
                    w_ik = str(fresh["leg1_instrument_key"])
                    s_ik = str(fresh["leg2_instrument_key"])
                    w_q = pos_qty_by_ikey.get(w_ik, 0)
                    s_q = pos_qty_by_ikey.get(s_ik, 0)
                    qty_f = int(fresh["qty"])

                    if w_q == 0 and s_q == 0:
                        # P1-3 FIX: Compute canonical P&L via `options_accounting` on external flat!
                        qw = self._get_quote(w_ik)
                        qs = self._get_quote(s_ik)
                        wb, _ = self._extract_mtm_or_exit_bid_ask(qw, float(fresh["leg1_entry_fill"]))
                        _, sa = self._extract_mtm_or_exit_bid_ask(qs, float(fresh["leg2_entry_fill"]))
                        w_ex_px = pos_sell_px_by_ikey.get(w_ik, wb)
                        s_ex_px = pos_buy_px_by_ikey.get(s_ik, sa)
                        acc = compute_package_accounting(
                            legs=[
                                OptionLeg(str(fresh["leg1_option_type"]), float(fresh["leg1_strike"]), qty_f, float(fresh["leg1_entry_fill"]), w_ex_px, "MARKET_ORDER"),
                                OptionLeg(str(fresh["leg2_option_type"]), float(fresh["leg2_strike"]), -qty_f, float(fresh["leg2_entry_fill"]), s_ex_px, "MARKET_ORDER"),
                            ],
                            trade_date=str(fresh["session_date"]),
                        )
                        self._update_mirror_fields(
                            mirror_id,
                            state="CONFIRMED_FLAT",
                            leg1_exit_fill=w_ex_px,
                            leg1_exited_qty=qty_f,
                            leg2_exit_fill=s_ex_px,
                            leg2_exited_qty=qty_f,
                            gross_pnl_inr=float(acc["gross_pnl"]),
                            statutory_charges_inr=float(acc["total_friction"]),
                            realized_net_pnl_inr=float(acc["net_pnl"]),
                            exit_reason="BROKER_EXTERNAL_FLAT_SYNCED",
                            now_iso=now_iso,
                        )
                        notes.append(f"EXTERNAL_FLAT:{spid}")

                    elif s_q < 0 and w_q < abs(s_q):
                        # P0-3 + P1-3 FIX: Orphan short detected! Record externally closed wing qty, run atomic
                        # Short-First `_handle_package_close` (stays EXIT_RETRY_PENDING if short rebuy fails!),
                        # compute canonical P&L, and disarm OUTSIDE `_pkg_locks` to prevent AB-BA lock inversion!
                        ext_w_closed = max(0, qty_f - max(0, w_q))
                        qw = self._get_quote(w_ik)
                        wb, _ = self._extract_mtm_or_exit_bid_ask(qw, float(fresh["leg1_entry_fill"]))
                        w_ext_px = pos_sell_px_by_ikey.get(w_ik, wb)
                        self._update_mirror_fields(
                            mirror_id,
                            state="EXIT_RETRY_PENDING",
                            leg1_exited_qty=ext_w_closed,
                            leg1_exit_fill=w_ext_px if ext_w_closed > 0 else fresh.get("leg1_exit_fill"),
                            leg2_exited_qty=max(0, qty_f - abs(s_q)),
                            exit_reason="ORPHAN_SHORT_EMERGENCY_FLATTENED",
                            error_reason=f"ORPHAN_DETECTED:short={s_q},wing={w_q}",
                            now_iso=now_iso,
                        )
                        self._handle_package_close(
                            {"package_id": spid, "book_id": BOOK_ID_SPREAD_A},
                            now_ist,
                            override_reason="ORPHAN_SHORT_EMERGENCY_FLATTENED",
                        )
                        disarm_triggers.append("ORPHAN_SHORT_EMERGENCY_FLATTENED")
                        notes.append(f"ORPHAN_FLATTENED:{spid}")

                elif fresh["book_id"] == BOOK_ID_PAID_B:
                    l_ik = str(fresh["leg1_instrument_key"])
                    l_q = pos_qty_by_ikey.get(l_ik, 0)
                    if l_q == 0:
                        sl_oid = fresh.get("broker_sl_oid")
                        ql = self._get_quote(l_ik)
                        lb, _ = self._extract_mtm_or_exit_bid_ask(ql, float(fresh["leg1_entry_fill"]))
                        ex_px = pos_sell_px_by_ikey.get(l_ik, float(fresh.get("broker_sl_trigger") or lb))
                        if sl_oid:
                            fin_sl = self._cancel_and_confirm_final_qty("LIVE_ARMED", BOOK_ID_PAID_B, str(sl_oid), target_qty=int(fresh["qty"]))
                            if float(fin_sl.get("avg_price") or 0.0) > 0:
                                ex_px = float(fin_sl["avg_price"])
                        acc = compute_package_accounting(
                            legs=[OptionLeg(str(fresh["leg1_option_type"]), float(fresh["leg1_strike"]), int(fresh["qty"]), float(fresh["leg1_entry_fill"]), ex_px, "MARKET_ORDER")],
                            trade_date=str(fresh["session_date"]),
                        )
                        self._update_mirror_fields(
                            mirror_id,
                            state="CONFIRMED_FLAT",
                            leg1_exit_fill=ex_px,
                            leg1_exited_qty=int(fresh["qty"]),
                            broker_sl_status="EXTERNAL_OR_SL_FLAT",
                            gross_pnl_inr=float(acc["gross_pnl"]),
                            statutory_charges_inr=float(acc["total_friction"]),
                            realized_net_pnl_inr=float(acc["net_pnl"]),
                            exit_reason="BROKER_POSITION_FLAT_SYNCED",
                            now_iso=now_iso,
                        )
                        notes.append(f"BOOK_B_FLAT_SYNC:{spid}")

        # Invoke disarm callback AFTER releasing `_pkg_locks` (zero AB-BA lock inversion!)
        for rsn in disarm_triggers:
            if self._disarm_callback:
                self._disarm_callback(rsn)
        self._flush_pending_disarms()

        return notes

    def flatten_all_open_live_packages(self, reason: str = "SOS_PANIC_EXIT", now_dt: Optional[datetime] = None) -> List[str]:
        """Flattens ALL active/transitional `LIVE_ARMED` Shadow packages safely."""
        now_ist = to_ist_naive(now_dt)
        flattened: List[str] = []
        placeholders = ",".join(["?"] * len(ACTIVE_LIVE_STATES))
        with self._get_conn(readonly=True) as conn:
            rows = conn.execute(
                f"SELECT * FROM option_advance_live_shadow_packages WHERE execution_tier = 'LIVE_ARMED' AND state IN ({placeholders});",
                ACTIVE_LIVE_STATES,
            ).fetchall()
        for r in rows:
            rd = dict(r)
            spid = str(rd["shadow_package_id"])
            with self._get_pkg_lock(spid):
                res = self._handle_package_close(
                    {"package_id": spid, "book_id": rd["book_id"]},
                    now_ist,
                    override_reason=reason,
                )
                if res and res.get("state") in TERMINAL_STATES:
                    flattened.append(str(rd["mirror_id"]))
        self._flush_pending_disarms()
        return flattened

    # ──────────────────────────────────────────────────────────────────────────
    # BRIDGE & UI TELEMETRY ACCESSORS
    # ──────────────────────────────────────────────────────────────────────────

    def get_active_live_positions_for_bridge(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            placeholders = ",".join(["?"] * len(ACTIVE_LIVE_STATES))
            with self._get_conn(readonly=True) as conn:
                rows = conn.execute(
                    f"""
                    SELECT * FROM option_advance_live_shadow_packages
                    WHERE execution_tier = 'LIVE_ARMED' AND state IN ({placeholders})
                    ORDER BY id DESC;
                    """,
                    ACTIVE_LIVE_STATES,
                ).fetchall()
            for r in rows:
                d = dict(r)
                w_open = max(0, int(d.get("leg1_filled_qty") or 0) - int(d.get("leg1_exited_qty") or 0))
                s_open = max(0, int(d.get("leg2_filled_qty") or 0) - int(d.get("leg2_exited_qty") or 0))
                qty = max(w_open, s_open, int(d.get("qty") or SHADOW_LOT_QTY))
                unreal = 0.0
                est_fees = 0.0
                c_bid = None
                c_ask = None
                w_age_ms: Optional[int] = None
                s_age_ms: Optional[int] = None
                quote_stale = False
                if d["book_id"] == BOOK_ID_SPREAD_A and (w_open > 0 or s_open > 0):
                    legs_u: List[OptionLeg] = []
                    if w_open > 0 and d.get("leg1_entry_fill"):
                        qw = self._get_quote(str(d["leg1_instrument_key"]))
                        w_age_ms = self._extract_quote_age_ms(qw)
                        if w_age_ms is None or w_age_ms > 10000:
                            quote_stale = True
                        wb, _ = self._extract_mtm_or_exit_bid_ask(qw, float(d["leg1_entry_fill"]))
                        c_bid = wb
                        legs_u.append(OptionLeg(str(d["leg1_option_type"]), float(d["leg1_strike"]), w_open, float(d["leg1_entry_fill"]), wb, "MARKET_ORDER"))
                    if s_open > 0 and d.get("leg2_entry_fill"):
                        qs = self._get_quote(str(d["leg2_instrument_key"]))
                        s_age_ms = self._extract_quote_age_ms(qs)
                        if s_age_ms is None or s_age_ms > 10000:
                            quote_stale = True
                        _, sa = self._extract_mtm_or_exit_bid_ask(qs, float(d["leg2_entry_fill"]))
                        c_ask = sa
                        legs_u.append(OptionLeg(str(d["leg2_option_type"]), float(d["leg2_strike"]), -s_open, float(d["leg2_entry_fill"]), sa, "MARKET_ORDER"))
                    if legs_u:
                        acc = compute_package_accounting(legs=legs_u, trade_date=str(d["session_date"]))
                        unreal = float(acc["net_pnl"])
                        est_fees = float(acc["total_friction"])
                    tsym = f"{d.get('leg2_trading_symbol') or ''} / +{int(float(d.get('leg1_strike') or 0))}{d.get('leg1_option_type') or ''}"
                elif d["book_id"] == BOOK_ID_PAID_B and w_open > 0 and d.get("leg1_entry_fill"):
                    ql = self._get_quote(str(d["leg1_instrument_key"]))
                    w_age_ms = self._extract_quote_age_ms(ql)
                    if w_age_ms is None or w_age_ms > 10000:
                        quote_stale = True
                    lb, la = self._extract_mtm_or_exit_bid_ask(ql, float(d["leg1_entry_fill"]))
                    c_bid = lb
                    c_ask = la
                    acc = compute_package_accounting(
                        legs=[OptionLeg(str(d["leg1_option_type"]), float(d["leg1_strike"]), w_open, float(d["leg1_entry_fill"]), lb, "MARKET_ORDER")],
                        trade_date=str(d["session_date"]),
                    )
                    unreal = float(acc["net_pnl"])
                    est_fees = float(acc["total_friction"])
                    tsym = str(d["leg1_trading_symbol"] or "")
                else:
                    tsym = str(d.get("leg1_trading_symbol") or d.get("direction_label") or "")

                out.append(
                    {
                        "order_intent_id": d["mirror_id"],
                        "mirror_id": d["mirror_id"],
                        "shadow_package_id": d["shadow_package_id"],
                        "advance_trade_id": d.get("advance_trade_id"),
                        "execution_mode": d["bridge_mode"],
                        "book_type": "SHORT_BOOK_SELL" if d["book_id"] == BOOK_ID_SPREAD_A else "LONG_BOOK_BUY",
                        "strategy_mode": d["book_id"],
                        "direction": d["direction_label"],
                        "action": "SELL" if d["book_id"] == BOOK_ID_SPREAD_A else "BUY",
                        "instrument_key": d.get("leg2_instrument_key") or d.get("leg1_instrument_key"),
                        "trading_symbol": tsym,
                        "underlying": "NIFTY",
                        "strike": d.get("leg2_strike") or d.get("leg1_strike"),
                        "option_type": d.get("leg2_option_type") or d.get("leg1_option_type"),
                        "expiry": d["expiry"],
                        "lot_size": SHADOW_LOT_QTY,
                        "target_quantity": qty,
                        "filled_quantity": w_open if d["book_id"] == BOOK_ID_PAID_B else max(w_open, s_open),
                        "average_fill_price": d.get("leg2_entry_fill") or d.get("leg1_entry_fill"),
                        "hedge_wing_strike": d.get("leg1_strike") if d["book_id"] == BOOK_ID_SPREAD_A else 0.0,
                        "hedge_wing_instrument_key": d.get("leg1_instrument_key") if d["book_id"] == BOOK_ID_SPREAD_A else None,
                        "hedge_wing_broker_order_id": d.get("leg1_entry_oid") if d["book_id"] == BOOK_ID_SPREAD_A else None,
                        "broker_sl_order_id": d.get("broker_sl_oid"),
                        "stop_loss_trigger": d.get("broker_sl_trigger"),
                        "state": "FILLED" if (w_open > 0 or s_open > 0) else "SUBMITTED",
                        "exit_state": d["state"],
                        "current_bid": c_bid,
                        "current_ask": c_ask,
                        "wing_quote_age_ms": w_age_ms,
                        "short_quote_age_ms": s_age_ms,
                        "quote_stale": quote_stale,
                        "statutory_charges": round(est_fees, 2),
                        "unrealized_pnl": round(unreal, 2),
                        "is_shadow_mirror": True,
                        "created_at": d["created_at"],
                        "updated_at": d["updated_at"],
                    }
                )
        except Exception as e:
            logger.debug(f"[ShadowMirrorExecutor] Active positions format note: {e}")
        return out

    def get_today_live_closed_events(self, session_date: str) -> List[Tuple[str, float, float, float]]:
        """Returns chronological `(updated_at, gross, charges, net)` tuples for `LIVE_ARMED` closed/unwound rows."""
        with self._get_conn(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT updated_at, gross_pnl_inr, statutory_charges_inr, realized_net_pnl_inr
                FROM option_advance_live_shadow_packages
                WHERE session_date = ?
                  AND execution_tier = 'LIVE_ARMED'
                  AND state IN ('CONFIRMED_FLAT','ABORTED_PARTIAL_UNWOUND',
                                'LEG2_CREDIT_FADE_UNWOUND','LEG2_ABORT_UNWOUND','LEG2_PARTIAL_UNWOUND')
                ORDER BY updated_at ASC, id ASC;
                """,
                (session_date,),
            ).fetchall()
        return [(str(r[0] or ""), float(r[1] or 0.0), float(r[2] or 0.0), float(r[3] or 0.0)) for r in rows]

    def get_today_live_realized_totals(self, session_date: str) -> Dict[str, Any]:
        ev = self.get_today_live_closed_events(session_date)
        gross = sum(x[1] for x in ev)
        chg = sum(x[2] for x in ev)
        net_list = [x[3] for x in ev]
        consec_l = 0
        for n in net_list:
            if n < 0:
                consec_l += 1
            else:
                consec_l = 0
        return {
            "gross_pnl": round(gross, 2),
            "charges": round(chg, 2),
            "net_pnl": round(sum(net_list), 2),
            "closed_count": len(net_list),
            "wins": sum(1 for n in net_list if n > 0),
            "losses": sum(1 for n in net_list if n <= 0),
            "consecutive_losses": consec_l,
            "events": ev,
        }

    def get_closed_live_rows_for_calendar(self) -> List[Dict[str, Any]]:
        """Returns `LIVE_ARMED` closed/unwound rows normalized for `AdvanceLiveBridge.get_calendar_performance(LIVE_BROKER)`."""
        out: List[Dict[str, Any]] = []
        try:
            with self._get_conn(readonly=True) as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM option_advance_live_shadow_packages
                    WHERE execution_tier = 'LIVE_ARMED'
                      AND state IN ('CONFIRMED_FLAT','ABORTED_PARTIAL_UNWOUND',
                                    'LEG2_CREDIT_FADE_UNWOUND','LEG2_ABORT_UNWOUND','LEG2_PARTIAL_UNWOUND')
                    ORDER BY created_at ASC;
                    """
                ).fetchall()
            for r in rows:
                d = dict(r)
                is_a = d["book_id"] == BOOK_ID_SPREAD_A
                ent_ref = round(float(d.get("leg2_entry_fill") or 0.0) - float(d.get("leg1_entry_fill") or 0.0), 2) if (is_a and d.get("leg2_entry_fill")) else float(d.get("leg1_entry_fill") or 0.0)
                ex_ref = round(float(d.get("leg2_exit_fill") or 0.0) - float(d.get("leg1_exit_fill") or 0.0), 2) if (is_a and d.get("leg2_exit_fill")) else float(d.get("leg1_exit_fill") or 0.0)
                out.append(
                    {
                        "trade_id": d["mirror_id"],
                        "signal_id": d["shadow_package_id"],
                        "book_type": "SHORT_BOOK_SELL" if is_a else "LONG_BOOK_BUY",
                        "strategy_mode": d["book_id"],
                        "direction": d["direction_label"],
                        "instrument_key": d.get("leg2_instrument_key") or d.get("leg1_instrument_key"),
                        "trading_symbol": d.get("leg2_trading_symbol") or d.get("leg1_trading_symbol"),
                        "underlying": "NIFTY",
                        "option_type": d.get("leg2_option_type") or d.get("leg1_option_type"),
                        "strike": d.get("leg2_strike") or d.get("leg1_strike"),
                        "expiry": d["expiry"],
                        "lot_size": SHADOW_LOT_QTY,
                        "quantity": int(d.get("qty") or SHADOW_LOT_QTY),
                        "entry_time": d["created_at"],
                        "exit_time": d["updated_at"],
                        "entry_price_used": ent_ref,
                        "exit_price_used": ex_ref,
                        "mfe_pct": 0.0,
                        "mae_pct": 0.0,
                        "gross_pnl": float(d.get("gross_pnl_inr") or 0.0),
                        "charges": float(d.get("statutory_charges_inr") or 0.0),
                        "slippage": 0.0,
                        "net_pnl": float(d.get("realized_net_pnl_inr") or 0.0),
                        "exit_reason": d.get("exit_reason") or d["state"],
                    }
                )
        except Exception:
            pass
        return out

    def get_recent_packages(self, limit: int = 25, tier_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            with self._get_conn(readonly=True) as conn:
                if tier_filter:
                    rows = conn.execute(
                        "SELECT * FROM option_advance_live_shadow_packages WHERE execution_tier = ? ORDER BY id DESC LIMIT ?;",
                        (str(tier_filter).upper(), int(limit)),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM option_advance_live_shadow_packages ORDER BY id DESC LIMIT ?;",
                        (int(limit),),
                    ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    def get_mirror_status(self, session_date: Optional[str] = None) -> Dict[str, Any]:
        today = session_date or to_ist_naive().strftime("%Y-%m-%d")
        tier_a, bmode = self._resolve_execution_tier_for_book(BOOK_ID_SPREAD_A)
        tier_b, _ = self._resolve_execution_tier_for_book(BOOK_ID_PAID_B)

        india_vix: Optional[float] = None
        front_expiry: Optional[str] = None
        front_dte: Optional[int] = None
        dry_closed_cnt = 0
        live_closed_cnt = 0
        try:
            with self._get_conn(readonly=True) as conn:
                dry_closed_cnt = int(
                    (
                        conn.execute(
                            "SELECT COUNT(*) FROM option_advance_live_shadow_packages WHERE session_date = ? AND execution_tier = 'DRY_RUN' AND state = 'DRY_RUN_CLOSED';",
                            (today,),
                        ).fetchone()
                        or [0]
                    )[0]
                )
                live_closed_cnt = int(
                    (
                        conn.execute(
                            "SELECT COUNT(*) FROM option_advance_live_shadow_packages WHERE session_date = ? AND execution_tier = 'LIVE_ARMED' AND state = 'CONFIRMED_FLAT';",
                            (today,),
                        ).fetchone()
                        or [0]
                    )[0]
                )
                has_l5 = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='option_sniffer_advance_s205_l5_chain_1m';"
                ).fetchone()
                if has_l5:
                    r_vix = conn.execute(
                        "SELECT india_vix, expiry, session_date FROM option_sniffer_advance_s205_l5_chain_1m ORDER BY id DESC LIMIT 1;"
                    ).fetchone()
                    if r_vix:
                        if r_vix[0] is not None and float(r_vix[0]) > 0:
                            india_vix = round(float(r_vix[0]), 2)
                        min_exp = conn.execute(
                            "SELECT MIN(expiry) FROM option_sniffer_advance_s205_l5_chain_1m WHERE session_date = ?;",
                            (str(r_vix[2]),),
                        ).fetchone()
                        exp_s = str((min_exp[0] if min_exp and min_exp[0] else r_vix[1]) or "")[:10]
                        if exp_s:
                            front_expiry = exp_s
                            front_dte = max(
                                0,
                                (datetime.strptime(exp_s, "%Y-%m-%d").date() - datetime.strptime(today, "%Y-%m-%d").date()).days,
                            )
        except Exception:
            pass

        recent = self.get_recent_packages(limit=20)
        return {
            "bridge_mode": bmode,
            "book_a_tier": tier_a,
            "book_b_tier": tier_b,
            "v6b_comparator_airgapped": True,
            "mutation_call_count": self._mutation_call_count,
            "regime_banners": {
                "india_vix": india_vix,
                "vix_high_warning": bool(india_vix is not None and india_vix > 18.0),
                "vix_threshold": 18.0,
                "front_expiry": front_expiry,
                "front_dte": front_dte,
                "is_expiry_day": bool(front_dte == 0) if front_dte is not None else None,
            },
            "dry_run_completed_count": dry_closed_cnt,
            "live_completed_count": live_closed_cnt,
            "recent_packages": recent,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # SQLITE PERSISTENCE HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    def _insert_mirror_row(self, row: Dict[str, Any]) -> None:
        cols = list(row.keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_sql = ", ".join(cols)
        with self._get_conn(readonly=False) as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO option_advance_live_shadow_packages ({col_sql}) VALUES ({placeholders});",
                tuple(row[c] for c in cols),
            )
            conn.commit()

    def _update_mirror_fields(self, mirror_id: str, now_iso: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
        fields["updated_at"] = now_iso or to_ist_naive().strftime("%Y-%m-%d %H:%M:%S")
        assigns = ", ".join(f"{k} = ?" for k in fields.keys())
        vals = list(fields.values()) + [mirror_id]
        with self._get_conn(readonly=False) as conn:
            conn.execute(
                f"UPDATE option_advance_live_shadow_packages SET {assigns} WHERE mirror_id = ?;",
                tuple(vals),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM option_advance_live_shadow_packages WHERE mirror_id = ? LIMIT 1;",
                (mirror_id,),
            ).fetchone()
            return dict(updated) if updated else {"mirror_id": mirror_id, **fields}


def get_shadow_mirror_executor() -> ShadowMirrorExecutor:
    """Returns the single process-wide `ShadowMirrorExecutor` owned by `AdvanceLiveBridge` (P2-1 Fix)."""
    from app.analytics.option_sniffer_advance_live.advance_live_bridge import get_advance_live_bridge

    return get_advance_live_bridge().shadow_mirror
