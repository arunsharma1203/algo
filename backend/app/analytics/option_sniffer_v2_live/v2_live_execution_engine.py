"""Isolated Live Execution Engine for Option Sniffer Advance V2.

Safety Guarantees:
1. Always boots disarmed (`armed=False`); resets prior armed DB rows on startup.
2. Requires explicit human PIN confirmation (`hmac.compare_digest`) + confirmation flag.
   Rotating 4-digit human interlock PIN is displayed in operator `get_status()` UI modal
   and rotates on arm/disarm when dynamic.
3. Disarm stops new entries immediately while keeping autonomous ratchet-GTT synchronization,
   autonomous strategy exits, broker reconciliation, and manual emergency square-off active.
4. Enforces collision-free SHA-256 package IDs, Upstox deterministic intent IDs, bounded resting-order
   polling + cancel-before-terminal, `UNKNOWN_AMBIGUOUS` timeout halts, buy-wing-first spread execution,
   mandatory Upstox V3 `SINGLE` `ENTRY` GTT (with timeout GTT adoption or immediate flatten on failure, plus
   reconciliation-time GTT attachment), incremental Stage-1 partial exit persistence, and shared-account
   V1/foreign disambiguation preventing naked-short double-sells or V1 liquidation.
5. Uses canonical `app.analytics.common.options_accounting` with explicit `trade_date`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import os
import queue
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.common.options_accounting import (
    OptionLeg,
    compute_package_accounting,
    round_to_nse_tick,
)
from app.analytics.option_sniffer_v2_live.v2_live_ledger import (
    V2LiveLedgerStore,
    make_deterministic_intent_id,
    now_ist_date,
    now_ist_iso,
)
from app.analytics.option_sniffer_v2_live.v2_upstox_broker_client import (
    UPSTOX_VERIFIED_API_FACTS,
    BrokerOrderRejectedError,
    BrokerTimeoutAmbiguousError,
    UpstoxV2BrokerClient,
    V2BrokerClientProtocol,
    compute_smart_limit_collar,
    normalize_market_protection,
    prepare_upstox_v3_order_payload,
    sanitize_upstox_tag,
)

logger = logging.getLogger(__name__)

ALLOWED_LIVE_BOOKS = {"V2_BOOK_B_PAID_FLOW", "V2_BOOK_A_INTRADAY_CREDIT"}
LIVE_SCOPE_CONFIG: Dict[str, Tuple[List[str], float]] = {
    "BOOK_B_ONLY": (["V2_BOOK_B_PAID_FLOW"], 25000.0),
    "BOOK_A_ONLY": (["V2_BOOK_A_INTRADAY_CREDIT"], 75000.0),
    "AUTO_DUAL": (["V2_BOOK_B_PAID_FLOW", "V2_BOOK_A_INTRADAY_CREDIT"], 85000.0),
}


def _resolve_live_scope_and_floor(
    live_book_scope: Optional[str], selected_books: Optional[List[str]]
) -> Tuple[str, List[str], float]:
    if live_book_scope:
        scope_u = str(live_book_scope).upper().strip()
        if scope_u not in LIVE_SCOPE_CONFIG:
            raise ValueError(f"Unsupported live_book_scope: {live_book_scope!r}")
        books_def, floor_inr = LIVE_SCOPE_CONFIG[scope_u]
        if selected_books:
            dis = [b for b in selected_books if b not in ALLOWED_LIVE_BOOKS]
            if dis:
                raise ValueError(f"Book(s) {dis} are blocked from live execution due to unverified spec conflicts.")
        return scope_u, list(books_def), float(floor_inr)
    books = [str(b).strip() for b in (selected_books or []) if str(b).strip()]
    if not books:
        raise ValueError("At least one book must be selected to arm live execution.")
    dis = [b for b in books if b not in ALLOWED_LIVE_BOOKS]
    if dis:
        raise ValueError(f"Book(s) {dis} are blocked from live execution due to unverified spec conflicts.")
    bset = set(books)
    if "V2_BOOK_B_PAID_FLOW" in bset and "V2_BOOK_A_INTRADAY_CREDIT" in bset:
        return "AUTO_DUAL", books, 85000.0
    if "V2_BOOK_A_INTRADAY_CREDIT" in bset:
        return "BOOK_A_ONLY", books, 75000.0
    return "BOOK_B_ONLY", books, 25000.0
OPEN_LIKE_PACKAGE_STATES = [
    "OPENING_BUY_WING",
    "OPENING_SHORT",
    "OPEN",
    "CLOSING",
    "UNWINDING_INCOMPLETE",
    "MANUAL_INTERVENTION_REQUIRED",
    "RECONCILE_MISMATCH",
]
_TERMINAL_BROKER_ORDER_STATUSES = {"complete", "rejected", "cancelled", "canceled"}
_QUOTE_STALE_MS = 15_000

_ENGINE_LOCK = threading.RLock()
_ENGINE_INSTANCE: Optional["V2LiveExecutionEngine"] = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _generate_4digit_pin() -> str:
    env_pin = (os.environ.get("OSV2_LIVE_PIN") or "").strip()
    if len(env_pin) == 4 and env_pin.isdigit():
        return env_pin
    return f"{1000 + secrets.randbelow(9000)}"


def _make_live_package_id(signal_id: str, book_id: str, session_date: str) -> str:
    digest = hashlib.sha256(f"{signal_id}|{book_id}|{session_date}".encode("utf-8")).hexdigest()[:20]
    return f"osv2l_{digest}"


_IST_TZ = timezone(timedelta(hours=5, minutes=30))


def _parse_broker_order_ts_ms(order_ts: Any) -> Optional[int]:
    raw = str(order_ts or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_IST_TZ)
        else:
            dt = dt.astimezone(_IST_TZ)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


class V2LiveExecutionEngine:
    """Isolated V2 live order coordinator (paper engine and ledger untouched)."""

    def __init__(
        self,
        *,
        db_path: Optional[str] = None,
        broker_client: Optional[V2BrokerClientProtocol] = None,
        initial_pin: Optional[str] = None,
        log_pin_on_boot: bool = True,
        order_poll_attempts: int = 8,
        order_poll_interval_sec: float = 0.02,
        attach_paper_listener: bool = False,
        async_observer_dispatch: Optional[bool] = None,
    ):
        self._lock = threading.RLock()
        self.store = V2LiveLedgerStore(db_path)
        self.broker: V2BrokerClientProtocol = broker_client or UpstoxV2BrokerClient()
        self._has_custom_broker = broker_client is not None
        self.order_poll_attempts = max(1, int(order_poll_attempts))
        self.order_poll_interval_sec = max(0.0, float(order_poll_interval_sec))
        self._paper_svc_ref: Any = None

        # Real HTTP execution uses non-blocking observer worker so broker HTTP never stalls OSV2-QueueWorker
        self._async_observer_dispatch: bool = (
            bool(async_observer_dispatch)
            if async_observer_dispatch is not None
            else (not self._has_custom_broker)
        )
        self._observer_queue: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue(maxsize=256)
        self._observer_stop = threading.Event()
        self._observer_thread: Optional[threading.Thread] = None
        # Dedicated lock so non-blocking event enqueue NEVER stalls on self._lock held during HTTP I/O
        self._observer_start_lock = threading.Lock()

        env_pin = (os.environ.get("OSV2_LIVE_PIN") or "").strip()
        if initial_pin is not None:
            pin_s = str(initial_pin).strip()
            if not (len(pin_s) == 4 and pin_s.isdigit()):
                raise ValueError("Live PIN must be exactly 4 numeric digits.")
            self._session_pin = pin_s
            self._pin_is_fixed = True
        else:
            self._session_pin = _generate_4digit_pin()
            self._pin_is_fixed = bool(len(env_pin) == 4 and env_pin.isdigit())

        # Fail-safe: any pre-existing live DB boots disarmed without creating a cold DB
        if self.store.db_exists():
            self.store.force_boot_disarm_if_exists()
        if log_pin_on_boot:
            logger.warning(
                "[V2LiveExecutionEngine] Booted DISARMED (db=%s). Interactive human session PIN=%s",
                self.store.db_path,
                self._session_pin,
            )
        if attach_paper_listener:
            self.attach_to_paper_service()

    def _ensure_observer_worker_started(self) -> None:
        if not self._async_observer_dispatch:
            return
        t = self._observer_thread
        if t is not None and t.is_alive():
            return
        with self._observer_start_lock:
            if self._observer_thread is None or not self._observer_thread.is_alive():
                self._observer_stop.clear()
                self._observer_thread = threading.Thread(
                    target=self._observer_worker_loop,
                    name="OSV2Live-ObserverWorker",
                    daemon=True,
                )
                self._observer_thread.start()

    def _observer_worker_loop(self) -> None:
        while not self._observer_stop.is_set():
            try:
                ev = self._observer_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if ev is None:
                    return
                self._process_paper_lifecycle_event_sync(ev)
            except Exception as exc:
                logger.error("Async live observer event error: %s", exc)
            finally:
                self._observer_queue.task_done()

    def flush_observer_events(self, timeout_sec: float = 5.0) -> bool:
        """Waits cleanly for queued async observer events (used in tests/shutdown)."""
        if not self._async_observer_dispatch:
            return True
        deadline = time.monotonic() + float(timeout_sec)
        while time.monotonic() < deadline:
            if self._observer_queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._observer_queue.unfinished_tasks == 0

    def attach_to_paper_service(self, svc: Any = None) -> None:
        """Attaches passive lifecycle listener to OptionSnifferV2Service without mutating cold DBs."""
        try:
            if svc is None:
                from app.analytics.option_sniffer_v2.v2_service import get_option_sniffer_v2_service

                svc = get_option_sniffer_v2_service()
            if svc is not None and hasattr(svc, "register_lifecycle_listener"):
                if self._paper_svc_ref is not None and self._paper_svc_ref is not svc:
                    self.detach_from_paper_service()
                self._ensure_observer_worker_started()
                svc.register_lifecycle_listener(self.on_paper_lifecycle_event)
                self._paper_svc_ref = svc
        except Exception as exc:
            logger.debug("Could not attach V2 live engine to V2 paper service: %s", exc)

    def detach_from_paper_service(self) -> None:
        svc = self._paper_svc_ref
        if svc is not None and hasattr(svc, "unregister_lifecycle_listener"):
            try:
                svc.unregister_lifecycle_listener(self.on_paper_lifecycle_event)
            except Exception:
                pass
        self._paper_svc_ref = None
        if self._observer_thread is not None and self._observer_thread.is_alive():
            self._observer_stop.set()
            try:
                self._observer_queue.put_nowait(None)
            except queue.Full:
                pass
            self._observer_thread.join(timeout=1.5)
        self._observer_thread = None

    def _maybe_rotate_pin(self, force: bool = False) -> str:
        if force or not self._pin_is_fixed:
            self._session_pin = f"{1000 + secrets.randbelow(9000)}"
        return self._session_pin

    def rotate_pin(self) -> str:
        with self._lock:
            return self._maybe_rotate_pin(force=True)

    def _read_paper_baseline_summary(self) -> Dict[str, Any]:
        """Strictly read-only snapshot of V2 paper baseline metrics if V2 paper DB exists."""
        try:
            from app.analytics.option_sniffer_v2.v2_service import get_option_sniffer_v2_service

            svc = self._paper_svc_ref or get_option_sniffer_v2_service()
            if not svc.store.db_exists():
                return {
                    "available": False,
                    "reason": "PAPER_DB_COLD_NOT_INITIALIZED",
                    "simulation_mode_baseline": True,
                }
            ov = svc.get_overview(include_collateral=False)
            fwd = ov.get("forward_paper_summary") or {}
            dd_corr = fwd.get("drawdown_audit_correction") or {}
            return {
                "available": True,
                "simulation_mode_baseline": True,
                "config_hash": ov.get("config_hash"),
                "closed_trades": fwd.get("closed_trades", 0),
                "open_packages": fwd.get("open_count", 0),
                "realized_net_pnl_inr": fwd.get("net_pnl_inr", 0.0),
                "observed_mark_max_dd_inr": fwd.get("observed_mark_dd_inr", dd_corr.get("observed_mark_max_dd_inr")),
                "stress_max_dd_inr": fwd.get("stress_dd_inr", dd_corr.get("stress_max_dd_inr")),
                "feed_health": (ov.get("feed_hygiene") or {}).get("feed_consumer"),
                "regime_arbitration": ov.get("regime_arbitration"),
            }
        except Exception as exc:
            logger.debug("Read-only paper baseline snapshot unavailable: %s", exc)
            return {"available": False, "reason": str(exc), "simulation_mode_baseline": True}

    def _estimate_open_live_mtm_inr(
        self,
        open_pkgs: List[Dict[str, Any]],
        *,
        allow_broker_fallback: bool = True,
    ) -> Tuple[float, bool]:
        """
        Estimates unrealized MTM across open live legs with strict 15s quote freshness.
        Falls back to broker position `last_price` if local V2 quote cache is missing/stale;
        reports `has_unpriced_open=True` (never silent 0!) if no fresh price exists.
        """
        if not open_pkgs:
            return 0.0, False
        quotes: Dict[str, Dict[str, Any]] = {}
        try:
            from app.analytics.option_sniffer_v2.v2_service import get_option_sniffer_v2_service

            svc = self._paper_svc_ref or get_option_sniffer_v2_service()
            quotes = dict(getattr(svc.unified_engine, "latest_opt_quotes", {}) or {})
        except Exception:
            quotes = {}

        now_wall_ms = _now_ms()
        broker_pos_px: Optional[Dict[str, float]] = None

        def _lookup_mark(ikey: str, side_for_exit: str) -> Optional[float]:
            nonlocal broker_pos_px
            q = quotes.get(ikey) or {}
            q_ts = int(q.get("l1_book_timestamp_ms") or q.get("availability_ts_ms") or q.get("exchange_ltt_ms") or 0)
            is_fresh = q_ts > 0 and 0 <= (now_wall_ms - q_ts) <= _QUOTE_STALE_MS
            if is_fresh:
                px = float(
                    (q.get("raw_bid") or q.get("bid") or q.get("ltp") or 0.0)
                    if side_for_exit == "SELL"
                    else (q.get("raw_ask") or q.get("ask") or q.get("ltp") or 0.0)
                )
                if px > 0.0:
                    return px
            if allow_broker_fallback:
                if broker_pos_px is None:
                    broker_pos_px = {}
                    try:
                        for bp in self.broker.get_positions():
                            lp = float(bp.get("last_price") or 0.0)
                            if lp > 0.0:
                                broker_pos_px[str(bp.get("instrument_token") or "")] = lp
                    except Exception:
                        pass
                px_b = float(broker_pos_px.get(ikey) or 0.0)
                if px_b > 0.0:
                    return px_b
            return None

        total_mtm = 0.0
        has_unpriced = False
        for pkg in open_pkgs:
            legs = pkg.get("legs") or {}
            buy_leg = legs.get("buy_leg") or {}
            b_qty = int(buy_leg.get("qty") or 0) - int(buy_leg.get("exited_qty") or 0)
            b_ent = float(buy_leg.get("entry_price") or 0.0)
            if b_qty > 0 and b_ent > 0 and buy_leg.get("exit_price") is None:
                b_mark = _lookup_mark(str(buy_leg.get("instrument_key") or ""), "SELL")
                if b_mark is None:
                    has_unpriced = True
                else:
                    total_mtm += (b_mark - b_ent) * b_qty
            short_leg = legs.get("short_leg") or {}
            s_qty = int(short_leg.get("qty") or 0) - int(short_leg.get("exited_qty") or 0)
            s_ent = float(short_leg.get("entry_price") or 0.0)
            if s_qty > 0 and s_ent > 0 and short_leg.get("exit_price") is None:
                s_mark = _lookup_mark(str(short_leg.get("instrument_key") or ""), "BUY")
                if s_mark is None:
                    has_unpriced = True
                else:
                    total_mtm += (s_ent - s_mark) * s_qty
        return round(total_mtm, 2), has_unpriced

    def get_status(self, *, include_paper_baseline: bool = True) -> Dict[str, Any]:
        """Cold-start safe read-only status (zero DB creation; shows rotating UI confirmation code)."""
        arm_state = self.store.get_arm_state()
        faults = self.store.count_unresolved_faults()
        open_pkgs = self.store.list_packages(states=OPEN_LIKE_PACKAGE_STATES)
        closed_pkgs = self.store.list_packages(states=["CLOSED"], limit=50)
        today = now_ist_date()
        daily_realized = round(
            sum(float(p.get("realized_net_inr") or 0.0) for p in closed_pkgs if p.get("session_date") == today),
            2,
        )
        # Never call broker REST on read-only status when no open packages or cold DB
        daily_unrealized, has_unpriced = self._estimate_open_live_mtm_inr(
            open_pkgs, allow_broker_fallback=False
        )
        daily_combined = round(daily_realized + daily_unrealized, 2)
        sel_books = arm_state.get("selected_books") or []
        if "V2_BOOK_B_PAID_FLOW" in sel_books and "V2_BOOK_A_INTRADAY_CREDIT" in sel_books:
            cur_scope, cur_floor = "AUTO_DUAL", 85000.0
        elif "V2_BOOK_A_INTRADAY_CREDIT" in sel_books:
            cur_scope, cur_floor = "BOOK_A_ONLY", 75000.0
        elif "V2_BOOK_B_PAID_FLOW" in sel_books:
            cur_scope, cur_floor = "BOOK_B_ONLY", 25000.0
        else:
            cur_scope, cur_floor = "PAPER_ONLY", 0.0
        return {
            "service": "OPTION_SNIFFER_ADVANCE_V2_LIVE",
            "isolated_db_path": self.store.db_path,
            "db_exists": self.store.db_exists(),
            "simulation_mode_baseline_preserved": True,
            "execution_mode": "LIVE_ARMED" if arm_state.get("armed") else "DISARMED_PAPER_BASELINE_ONLY",
            "armed": bool(arm_state.get("armed")),
            "live_book_scope": cur_scope if arm_state.get("armed") else "PAPER_ONLY",
            "min_required_margin_inr": cur_floor,
            "pin_required": True,
            "current_pin": self._session_pin,
            "paper_observer_attached": self._paper_svc_ref is not None,
            "arm_state": arm_state,
            "open_packages": open_pkgs,
            "open_package_count": len(open_pkgs),
            "closed_package_count": len(closed_pkgs),
            "daily_realized_net_inr": daily_realized,
            "daily_unrealized_mtm_inr": daily_unrealized,
            "daily_unrealized_mtm_unpriced": has_unpriced,
            "daily_combined_net_inr": daily_combined,
            "unresolved_faults": faults,
            "paper_baseline_summary": self._read_paper_baseline_summary() if include_paper_baseline else None,
            "upstox_verified_api_facts": UPSTOX_VERIFIED_API_FACTS,
            "disclaimer": "An AI backtest or paper baseline does not prove future income. Broker orders execute only when armed by human PIN.",
        }

    def get_positions(self) -> Dict[str, Any]:
        return {
            "db_exists": self.store.db_exists(),
            "open_packages": self.store.list_packages(states=OPEN_LIKE_PACKAGE_STATES),
            "closed_packages": self.store.list_packages(states=["CLOSED"], limit=100),
            "recent_intents": self.store.list_intents(limit=100),
            "unresolved_faults": self.store.count_unresolved_faults(),
        }

    def get_audit_log(self, limit: int = 100) -> Dict[str, Any]:
        return {
            "db_exists": self.store.db_exists(),
            "entries": self.store.list_audit_log(limit=limit),
        }

    def arm_live(
        self,
        *,
        pin: str,
        human_confirmed: bool,
        selected_books: Optional[List[str]] = None,
        live_book_scope: Optional[str] = None,
        protocol_profile_id: str = "V2_BOOK_B_FROZEN_PAPER",
        max_lots_per_book: int = 1,
        max_daily_loss_inr: float = 2500.0,
        max_open_packages: int = 1,
        actor: str = "HUMAN_UI",
    ) -> Dict[str, Any]:
        with self._lock:
            if not human_confirmed:
                raise ValueError("Explicit human confirmation checkbox is required before arming live execution.")
            pin_clean = str(pin or "").strip()
            if not hmac.compare_digest(pin_clean, self._session_pin):
                if self.store.db_exists():
                    self.store.append_audit("ARM_REJECTED_BAD_PIN", actor, {"ts_iso": now_ist_iso()})
                raise PermissionError("Invalid 4-digit human security PIN.")

            scope_u, books, min_floor_inr = _resolve_live_scope_and_floor(live_book_scope, selected_books)
            if int(max_lots_per_book) < 1 or int(max_lots_per_book) > 10:
                raise ValueError("max_lots_per_book must be between 1 and 10.")
            if float(max_daily_loss_inr) <= 0:
                raise ValueError("max_daily_loss_inr must be positive.")
            if int(max_open_packages) < 1 or int(max_open_packages) > 5:
                raise ValueError("max_open_packages must be between 1 and 5.")

            faults = self.store.count_unresolved_faults()
            if faults["total_blocking_faults"] > 0:
                raise RuntimeError(
                    f"Cannot arm with {faults['total_blocking_faults']} unresolved fault(s); run reconciliation first."
                )

            funds_snapshot: Dict[str, Any] = {}
            try:
                funds_snapshot = self.broker.get_funds_and_margin()
            except Exception as exc:
                raise RuntimeError(f"Broker pre-flight funds check failed; refusing to arm: {exc}") from exc

            avail_margin = float(
                (((funds_snapshot or {}).get("data") or {}).get("equity") or {}).get("available_margin") or 0.0
            )
            req_floor = float(min_floor_inr) * (int(max_lots_per_book) if scope_u != "BOOK_B_ONLY" else 1)
            if avail_margin < req_floor:
                raise RuntimeError(
                    f"Insufficient available margin Rs{avail_margin:,.0f}; minimum Rs{req_floor:,.0f} required for live scope {scope_u}."
                )

            if not self._has_custom_broker and self._paper_svc_ref is None:
                self.attach_to_paper_service()

            state = self.store.set_arm_state(
                armed=True,
                selected_books=books,
                protocol_profile_id=protocol_profile_id,
                max_lots_per_book=int(max_lots_per_book),
                max_daily_loss_inr=float(max_daily_loss_inr),
                max_open_packages=int(max_open_packages),
                reason=f"ARMED_BY_HUMAN_PIN:{scope_u}",
                actor=actor,
            )
            self.store.append_audit(
                "PREFLIGHT_FUNDS_VERIFIED",
                actor,
                {
                    "funds": funds_snapshot,
                    "live_book_scope": scope_u,
                    "min_required_margin_inr": req_floor,
                    "armed_for_date": state.get("armed_for_date"),
                },
            )
            self._maybe_rotate_pin()
            return self.get_status()

    def disarm_live(
        self,
        *,
        reason: str = "HUMAN_DISARMED",
        actor: str = "HUMAN_UI",
        include_paper_baseline: bool = True,
    ) -> Dict[str, Any]:
        with self._lock:
            cur = self.store.get_arm_state()
            self.store.set_arm_state(
                armed=False,
                selected_books=cur.get("selected_books") or ["V2_BOOK_B_PAID_FLOW"],
                protocol_profile_id=cur.get("protocol_profile_id") or "V2_BOOK_B_FROZEN_PAPER",
                max_lots_per_book=int(cur.get("max_lots_per_book") or 1),
                max_daily_loss_inr=float(cur.get("max_daily_loss_inr") or 2500.0),
                max_open_packages=int(cur.get("max_open_packages") or 1),
                reason=reason,
                actor=actor,
            )
            self._maybe_rotate_pin()
            return self.get_status(include_paper_baseline=include_paper_baseline)

    def sos_flatten_all_and_disarm(
        self,
        *,
        reason: str = "OPERATOR_SOS_FLATTEN",
        actor: str = "HUMAN_UI_SOS",
    ) -> Dict[str, Any]:
        """Emergency operator panic control: disarm immediately, cancel working orders, and flatten open live packages."""
        with self._lock:
            self.disarm_live(reason=reason, actor=actor, include_paper_baseline=False)
            cancelled_orders: List[str] = []
            errors: List[Dict[str, Any]] = []
            try:
                for ord_row in self.broker.get_orders() or []:
                    oid = str(ord_row.get("order_id") or "")
                    st = str(ord_row.get("status") or "").lower().strip()
                    tag = str(ord_row.get("tag") or "")
                    if oid and tag.startswith("V2-") and st not in _TERMINAL_BROKER_ORDER_STATUSES:
                        try:
                            self.broker.cancel_order(oid)
                            cancelled_orders.append(oid)
                        except Exception as exc:
                            errors.append({"order_id": oid, "stage": "CANCEL_ORDER", "error": str(exc)})
            except Exception as exc:
                errors.append({"stage": "LIST_ORDERS", "error": str(exc)})

            flattened_ids: List[str] = []
            open_pkgs = self.store.list_packages(states=OPEN_LIKE_PACKAGE_STATES)
            for pkg in open_pkgs:
                pid = str(pkg["live_package_id"])
                try:
                    ex_res = self.exit_live_package(live_package_id=pid, reason=reason, actor=actor)
                    if ex_res.get("ok"):
                        flattened_ids.append(pid)
                    else:
                        errors.append({"live_package_id": pid, "stage": "EXIT_PACKAGE", "detail": ex_res})
                except Exception as exc:
                    errors.append({"live_package_id": pid, "stage": "EXIT_PACKAGE", "error": str(exc)})

            if self.store.db_exists():
                self.store.append_audit(
                    "LIVE_SOS_FLATTEN_EXECUTED",
                    actor,
                    {
                        "reason": reason,
                        "cancelled_orders": cancelled_orders,
                        "flattened_package_ids": flattened_ids,
                        "errors": errors,
                    },
                )
            st_now = self.get_status(include_paper_baseline=True)
            return {
                "ok": len(errors) == 0,
                "reason": reason,
                "armed": False,
                "cancelled_orders": cancelled_orders,
                "flattened_package_ids": flattened_ids,
                "flattened_count": len(flattened_ids),
                "errors": errors,
                "live_status": st_now,
            }

    def _compute_realized_pkg_accounting(self, pkg: Dict[str, Any]) -> Dict[str, Any]:
        legs = pkg.get("legs") or {}
        acc_legs: List[OptionLeg] = []
        buy_leg = legs.get("buy_leg") or {}
        if float(buy_leg.get("entry_price") or 0) > 0 and float(buy_leg.get("exit_price") or 0) > 0 and int(buy_leg.get("qty") or 0) > 0:
            acc_legs.append(
                OptionLeg(
                    option_type=str(buy_leg.get("option_type") or "CE"),
                    strike=float(buy_leg.get("strike") or 0.0),
                    qty=int(buy_leg["qty"]),
                    entry_price=float(buy_leg["entry_price"]),
                    exit_price=float(buy_leg["exit_price"]),
                    lot_size=int(buy_leg.get("lot_size") or 65),
                    label=str(buy_leg.get("trading_symbol") or "BUY_WING"),
                )
            )
        short_leg = legs.get("short_leg") or {}
        if float(short_leg.get("entry_price") or 0) > 0 and float(short_leg.get("exit_price") or 0) > 0 and int(short_leg.get("qty") or 0) > 0:
            acc_legs.append(
                OptionLeg(
                    option_type=str(short_leg.get("option_type") or "CE"),
                    strike=float(short_leg.get("strike") or 0.0),
                    qty=-abs(int(short_leg["qty"])),
                    entry_price=float(short_leg["entry_price"]),
                    exit_price=float(short_leg["exit_price"]),
                    lot_size=int(short_leg.get("lot_size") or 65),
                    label=str(short_leg.get("trading_symbol") or "SHORT_LEG"),
                )
            )
        excess_leg = legs.get("excess_unwound_buy_leg") or {}
        if float(excess_leg.get("entry_price") or 0) > 0 and float(excess_leg.get("exit_price") or 0) > 0 and int(excess_leg.get("qty") or 0) > 0:
            acc_legs.append(
                OptionLeg(
                    option_type=str(excess_leg.get("option_type") or "CE"),
                    strike=float(excess_leg.get("strike") or 0.0),
                    qty=int(excess_leg["qty"]),
                    entry_price=float(excess_leg["entry_price"]),
                    exit_price=float(excess_leg["exit_price"]),
                    lot_size=int(excess_leg.get("lot_size") or 65),
                    label="EXCESS_BUY_UNWIND",
                )
            )
        if not acc_legs:
            return {"gross_pnl": 0.0, "total_friction": 0.0, "net_pnl": 0.0}
        trade_dt = str(pkg.get("session_date") or "") or None
        return compute_package_accounting(acc_legs, trade_date=trade_dt)

    def _recompute_leg_exit_progress(self, live_package_id: str, leg_role: str) -> Tuple[int, float]:
        """Sums all settled partial/full EXIT fills for a package leg safely from intent ledger."""
        intents = self.store.list_intents(live_package_id=live_package_id, limit=500)
        tot_qty = 0
        tot_notional = 0.0
        for it in intents:
            if it["leg_role"] != leg_role:
                continue
            if not str(it["action"]).startswith("EXIT"):
                continue
            fqty = int(it.get("filled_qty") or 0)
            fpx = float(it.get("avg_fill_price") or 0.0)
            if fqty > 0 and fpx > 0.0:
                tot_qty += fqty
                tot_notional += fqty * fpx
        if tot_qty <= 0:
            return 0, 0.0
        return tot_qty, round_to_nse_tick(tot_notional / tot_qty)

    def _find_unclaimed_gtt_child_sell(
        self,
        *,
        live_package_id: str,
        ikey: str,
        entry_ts_ms: int,
        broker_orders: List[Dict[str, Any]],
        gtt_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Matches a triggered Upstox V3 GTT child SELL strictly against untagged / GTT-tagged
        post-entry completed orders, excluding ALL foreign V1 (`INTENT_...`) and claimed V2 orders.
        """
        claimed_ids = {
            str(it.get("broker_order_id") or "")
            for it in self.store.list_intents(limit=1000)
            if it.get("broker_order_id")
        }
        for p in self.store.list_packages(limit=500):
            if p["live_package_id"] == live_package_id:
                continue
            cid = str(((p.get("legs") or {}).get("buy_leg") or {}).get("gtt_child_order_id") or "")
            if cid:
                claimed_ids.add(cid)

        candidates: List[Tuple[str, str, Dict[str, Any]]] = []
        for bo in broker_orders:
            if str(bo.get("instrument_token") or "") != ikey:
                continue
            if str(bo.get("transaction_type") or "").upper() != "SELL":
                continue
            if str(bo.get("status") or "").lower() != "complete":
                continue
            oid_s = str(bo.get("order_id") or "")
            if oid_s and oid_s in claimed_ids:
                continue
            tag_s = str(bo.get("tag") or "").strip()
            parent_gtt = str(bo.get("parent_gtt_id") or "").strip()
            # Real Upstox V3 GTT child orders carry blank tag (never foreign V1/OS tags!)
            is_gtt_shape = (
                tag_s == ""
                or tag_s.startswith("GTT-")
                or (gtt_id and parent_gtt == str(gtt_id))
            )
            if not is_gtt_shape:
                continue
            ord_ms = _parse_broker_order_ts_ms(bo.get("order_timestamp"))
            if ord_ms is not None and entry_ts_ms > 0 and ord_ms < int(entry_ts_ms) - 1000:
                continue
            candidates.append((str(bo.get("order_timestamp") or ""), oid_s, bo))

        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[0], x[1]))
        return candidates[-1][2]

    def _fetch_order_snapshot(self, oid: str, tag: str) -> Optional[Dict[str, Any]]:
        if oid and hasattr(self.broker, "get_order_history"):
            try:
                hist = self.broker.get_order_history(oid)
                if hist:
                    return dict(hist[-1])
            except Exception as exc:
                logger.debug("get_order_history(%s) failed; falling back to get_orders(): %s", oid, exc)
        try:
            orders = self.broker.get_orders()
        except Exception as exc:
            logger.debug("get_orders() fallback failed during settlement: %s", exc)
            return None
        for o in orders:
            if (oid and str(o.get("order_id") or "") == oid) or (tag and str(o.get("tag") or "") == tag):
                return dict(o)
        return None

    def _settle_placed_order(
        self,
        *,
        oid: str,
        tag: str,
        requested_qty: int,
        fallback_px: float,
        inline_order: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        snap: Optional[Dict[str, Any]] = dict(inline_order) if inline_order else None
        if not (snap and str(snap.get("status") or "").lower().strip() in _TERMINAL_BROKER_ORDER_STATUSES):
            for attempt in range(self.order_poll_attempts):
                cur = self._fetch_order_snapshot(oid, tag)
                if cur is not None:
                    snap = cur
                    if str(snap.get("status") or "").lower().strip() in _TERMINAL_BROKER_ORDER_STATUSES:
                        break
                if attempt + 1 < self.order_poll_attempts and self.order_poll_interval_sec > 0:
                    time.sleep(self.order_poll_interval_sec)

        # Fail-closed invariant: NEVER leave an unfilled or partially filled resting order open on NSE!
        if snap is None or str(snap.get("status") or "").lower().strip() not in _TERMINAL_BROKER_ORDER_STATUSES:
            if oid:
                try:
                    self.broker.cancel_order(oid, is_emergency=True)
                except BrokerTimeoutAmbiguousError as exc:
                    return {
                        "state": "UNKNOWN_AMBIGUOUS",
                        "filled_qty": int((snap or {}).get("filled_quantity") or 0),
                        "avg_px": float((snap or {}).get("average_price") or fallback_px or 0.0),
                        "snapshot": snap,
                        "error": f"Cancel timeout during non-terminal settlement: {exc}",
                    }
                except BrokerOrderRejectedError as exc:
                    logger.debug("Cancel rejected on %s (%s); re-checking terminal status", oid, exc)

                cancel_polls = max(3, self.order_poll_attempts)
                for attempt in range(cancel_polls):
                    cur2 = self._fetch_order_snapshot(oid, tag)
                    if cur2 is not None:
                        snap = cur2
                        if str(snap.get("status") or "").lower().strip() in _TERMINAL_BROKER_ORDER_STATUSES:
                            break
                    if attempt + 1 < cancel_polls and self.order_poll_interval_sec > 0:
                        time.sleep(self.order_poll_interval_sec)

            if snap is None or str(snap.get("status") or "").lower().strip() not in _TERMINAL_BROKER_ORDER_STATUSES:
                return {
                    "state": "UNKNOWN_AMBIGUOUS",
                    "filled_qty": int((snap or {}).get("filled_quantity") or 0),
                    "avg_px": float((snap or {}).get("average_price") or fallback_px or 0.0),
                    "snapshot": snap,
                    "error": "ORDER_NOT_TERMINAL_AFTER_CANCEL",
                }

        b_status = str(snap.get("status") or "").lower().strip()
        filled_qty = int(snap.get("filled_quantity") or 0)
        avg_px = float(snap.get("average_price") or 0.0)
        if filled_qty > 0 and avg_px <= 0.0:
            avg_px = float(fallback_px or 0.0)

        if filled_qty >= int(requested_qty):
            st = "FILLED"
        elif filled_qty > 0:
            st = "PARTIAL_FILL"
        elif "cancel" in b_status:
            st = "CANCELLED"
        else:
            st = "REJECTED"

        return {
            "state": st,
            "filled_qty": filled_qty,
            "avg_px": avg_px,
            "snapshot": snap,
            "error": snap.get("status_message") if st == "REJECTED" else None,
        }

    def _place_single_order_with_durability(
        self,
        *,
        signal_id: str,
        live_package_id: str,
        book_id: str,
        leg_role: str,
        action: str,
        session_date: str,
        instrument_key: str,
        trading_symbol: str,
        side: str,
        order_type: str,
        qty: int,
        limit_price: Optional[float] = None,
        market_protection: Optional[float] = None,
        is_emergency: bool = False,
    ) -> Dict[str, Any]:
        ot = str(order_type or "LIMIT").upper()
        norm_mp = normalize_market_protection(ot, market_protection)
        eff_limit_px = round_to_nse_tick(float(limit_price)) if (ot not in ("MARKET", "SL-M") and limit_price) else 0.0

        intent_id = make_deterministic_intent_id(signal_id, book_id, leg_role, action, session_date)
        tag = sanitize_upstox_tag(f"V2-{intent_id[-14:]}")
        now_ms = _now_ms()
        rec, created = self.store.create_or_get_intent(
            {
                "intent_id": intent_id,
                "signal_id": signal_id,
                "live_package_id": live_package_id,
                "book_id": book_id,
                "leg_role": leg_role,
                "action": action,
                "session_date": session_date,
                "instrument_key": instrument_key,
                "trading_symbol": trading_symbol,
                "side": side,
                "order_type": ot,
                "product": "D",
                "requested_qty": int(qty),
                "limit_price": eff_limit_px if eff_limit_px > 0 else None,
                "market_protection": int(norm_mp) if norm_mp is not None else None,
                "tag": tag,
                "state": "PENDING_SEND",
                "created_ts_ms": now_ms,
                "updated_ts_ms": now_ms,
            }
        )
        if not created:
            return {"idempotent_replay": True, "intent": rec, "status": rec["state"]}

        payload = prepare_upstox_v3_order_payload(
            {
                "quantity": int(qty),
                "product": "D",
                "validity": "DAY",
                "price": eff_limit_px if ot not in ("MARKET", "SL-M") else 0,
                "tag": tag,
                "instrument_token": instrument_key,
                "trading_symbol": trading_symbol,
                "order_type": ot,
                "transaction_type": side,
                "disclosed_quantity": 0,
                "trigger_price": 0,
                "is_amo": False,
                **({"market_protection": int(norm_mp)} if norm_mp is not None else {}),
            }
        )

        try:
            resp = self.broker.place_order(payload, is_emergency=is_emergency)
        except BrokerTimeoutAmbiguousError as exc:
            updated = self.store.update_intent(
                intent_id,
                state="UNKNOWN_AMBIGUOUS",
                updated_ts_ms=_now_ms(),
                error_detail=str(exc),
            )
            self.store.append_audit(
                "ORDER_TIMEOUT_AMBIGUOUS",
                "ENGINE",
                {"intent_id": intent_id, "tag": tag, "instrument_key": instrument_key, "error": str(exc)},
            )
            return {"idempotent_replay": False, "intent": updated, "status": "UNKNOWN_AMBIGUOUS", "error": str(exc)}
        except BrokerOrderRejectedError as exc:
            updated = self.store.update_intent(
                intent_id,
                state="REJECTED",
                updated_ts_ms=_now_ms(),
                error_detail=str(exc),
            )
            self.store.append_audit(
                "ORDER_REJECTED",
                "ENGINE",
                {"intent_id": intent_id, "tag": tag, "instrument_key": instrument_key, "error": str(exc)},
            )
            return {"idempotent_replay": False, "intent": updated, "status": "REJECTED", "error": str(exc)}
        except Exception as exc:
            updated = self.store.update_intent(
                intent_id,
                state="UNKNOWN_AMBIGUOUS",
                updated_ts_ms=_now_ms(),
                error_detail=f"Unexpected broker error: {exc}",
            )
            self.store.append_audit(
                "ORDER_UNEXPECTED_ERROR_AMBIGUOUS",
                "ENGINE",
                {"intent_id": intent_id, "tag": tag, "instrument_key": instrument_key, "error": str(exc)},
            )
            return {"idempotent_replay": False, "intent": updated, "status": "UNKNOWN_AMBIGUOUS", "error": str(exc)}

        data = resp.get("data") or {}
        oid = str(data.get("order_id") or (data.get("order_ids") or [""])[0] or "")
        inline_order = data.get("order") or {}
        settled = self._settle_placed_order(
            oid=oid,
            tag=tag,
            requested_qty=int(qty),
            fallback_px=float(limit_price or 0.0),
            inline_order=inline_order if inline_order else None,
        )
        st = settled["state"]
        if st == "UNKNOWN_AMBIGUOUS":
            self.store.append_audit(
                "ORDER_SETTLEMENT_AMBIGUOUS",
                "ENGINE",
                {"intent_id": intent_id, "order_id": oid, "tag": tag, "error": settled.get("error")},
            )

        updated = self.store.update_intent(
            intent_id,
            state=st,
            updated_ts_ms=_now_ms(),
            filled_qty=int(settled["filled_qty"]),
            avg_fill_price=float(settled["avg_px"]),
            broker_order_id=oid or None,
            error_detail=settled.get("error"),
            raw_response=settled.get("snapshot") or resp,
        )
        return {"idempotent_replay": False, "intent": updated, "status": st, "error": settled.get("error")}

    def _unwind_buy_wing(
        self,
        *,
        pkg: Dict[str, Any],
        qty_to_unwind: int,
        unwind_price: float,
        reason: str,
        leg_role: str = "BUY_WING_UNWIND",
    ) -> Dict[str, Any]:
        buy_leg = pkg["legs"]["buy_leg"]
        prior_role_intents = [
            it
            for it in self.store.list_intents(live_package_id=pkg["live_package_id"], limit=250)
            if it["leg_role"] == leg_role and str(it.get("action") or "").startswith("UNWIND_")
        ]
        amb_prior = next(
            (it for it in prior_role_intents if it["state"] in ("UNKNOWN_AMBIGUOUS", "PENDING_SEND", "SENT")),
            None,
        )
        if amb_prior is not None:
            return {
                "ok": False,
                "status": "UNKNOWN_AMBIGUOUS",
                "error": "PRIOR_UNWIND_AMBIGUOUS",
                "intent": amb_prior,
            }
        att_idx = len(prior_role_intents) + 1
        action_name = f"UNWIND_{int(qty_to_unwind)}" if att_idx == 1 else f"UNWIND_{int(qty_to_unwind)}_ATT_{att_idx}"
        res = self._place_single_order_with_durability(
            signal_id=pkg["signal_id"],
            live_package_id=pkg["live_package_id"],
            book_id=pkg["book_id"],
            leg_role=leg_role,
            action=action_name,
            session_date=pkg["session_date"],
            instrument_key=buy_leg["instrument_key"],
            trading_symbol=buy_leg["trading_symbol"],
            side="SELL",
            order_type="MARKET",
            qty=int(qty_to_unwind),
            limit_price=None,
            market_protection=5,
            is_emergency=True,
        )
        intent = res["intent"]
        if res["status"] == "FILLED" and int(intent.get("filled_qty") or 0) == int(qty_to_unwind):
            return {
                "ok": True,
                "filled_qty": int(intent["filled_qty"]),
                "avg_price": float(intent["avg_fill_price"] or unwind_price),
                "intent": intent,
            }
        return {"ok": False, "status": res["status"], "error": res.get("error") or intent.get("error_detail"), "intent": intent}

    def _place_or_recover_protective_gtt(
        self,
        pkg: Dict[str, Any],
        buy_filled_qty: int,
        buy_avg_px: float,
    ) -> Dict[str, Any]:
        """
        Places mandatory Upstox V3 SINGLE ENTRY GTT protection for a SINGLE_LONG entry.
        If stop_trigger_price was absent/<=0, computes safe 20% / Rs 1,800 stop fallback.
        On transport timeout, checks get_gtt_orders() to adopt existing broker GTT before unwinding.
        """
        buy_leg = pkg["legs"]["buy_leg"]
        raw_trig = buy_leg.get("stop_trigger_price")
        if raw_trig is not None and float(raw_trig) > 0:
            stop_trig_r = round_to_nse_tick(float(raw_trig))
        else:
            risk_per_unit = min(float(buy_avg_px) * 0.20, 1800.0 / max(1, int(buy_filled_qty)))
            stop_trig_r = round_to_nse_tick(
                max(0.05, float(buy_avg_px) - risk_per_unit),
                mode="adverse_buy",
                allow_zero_settlement=False,
            )

        try:
            gtt_resp = self.broker.place_gtt_order(
                {
                    "type": "SINGLE",
                    "quantity": int(buy_filled_qty),
                    "product": "D",
                    "instrument_token": buy_leg["instrument_key"],
                    "transaction_type": "SELL",
                    "rules": [
                        {
                            "strategy": "ENTRY",
                            "trigger_type": "BELOW",
                            "trigger_price": stop_trig_r,
                            "market_protection": 5,
                        }
                    ],
                }
            )
            gdata = gtt_resp.get("data") or {}
            gid = str(gdata.get("gtt_order_id") or (gdata.get("gtt_order_ids") or [""])[0] or "").strip()
            if not gid:
                raise BrokerOrderRejectedError("Upstox GTT response missing gtt_order_id")
            return {"ok": True, "gtt_id": gid, "stop_trigger_price": stop_trig_r}
        except BrokerTimeoutAmbiguousError as exc:
            try:
                active_gtts = self.broker.get_gtt_orders()
            except Exception as exc2:
                return {
                    "ok": False,
                    "ambiguous_gtt": True,
                    "error": f"GTT timeout and get_gtt_orders failed: {exc} / {exc2}",
                }
            claimed_gids = {
                str(((p.get("legs") or {}).get("buy_leg") or {}).get("gtt_id") or "")
                for p in self.store.list_packages(states=OPEN_LIKE_PACKAGE_STATES)
                if p["live_package_id"] != pkg["live_package_id"]
            }
            for g in active_gtts:
                gid_cand = str(g.get("gtt_order_id") or "").strip()
                if (
                    gid_cand
                    and gid_cand not in claimed_gids
                    and str(g.get("instrument_token") or "") == str(buy_leg["instrument_key"])
                    and str(g.get("transaction_type") or "").upper() == "SELL"
                    and int(g.get("quantity") or 0) == int(buy_filled_qty)
                ):
                    self.store.append_audit(
                        "GTT_RECOVERED_AFTER_TIMEOUT",
                        "ENGINE",
                        {"live_package_id": pkg["live_package_id"], "gtt_id": gid_cand},
                    )
                    return {"ok": True, "gtt_id": gid_cand, "stop_trigger_price": stop_trig_r}
            return {"ok": False, "ambiguous_gtt": False, "error": f"GTT timeout confirmed absent on broker: {exc}"}
        except Exception as exc:
            return {"ok": False, "ambiguous_gtt": False, "error": str(exc)}

    def sync_protective_ratchet_stop(
        self,
        *,
        live_package_id: str,
        new_stop_trigger: float,
        actor: str = "RATCHET_SYNC",
    ) -> Dict[str, Any]:
        """Monotonically ratchets protective V3 SINGLE GTT trigger upward (allowed armed OR disarmed)."""
        with self._lock:
            pkg = self.store.get_package(live_package_id)
            if not pkg or pkg["state"] != "OPEN":
                return {"ok": False, "status": "PACKAGE_NOT_OPEN"}
            buy_leg = pkg["legs"].get("buy_leg") or {}
            gtt_id = buy_leg.get("gtt_id")
            qty = int(buy_leg.get("qty") or 0) - int(buy_leg.get("exited_qty") or 0)
            if not gtt_id or qty <= 0:
                return {"ok": False, "status": "NO_ACTIVE_GTT_OR_QTY"}
            new_trig_r = round_to_nse_tick(float(new_stop_trigger), mode="adverse_buy", allow_zero_settlement=False)
            cur_trig = float(buy_leg.get("stop_trigger_price") or 0.0)
            if cur_trig > 0 and int(round(new_trig_r * 100)) < int(round(cur_trig * 100)) + 5:
                return {"ok": True, "status": "UNCHANGED_NO_DOWNWARD_RATCHET", "stop_trigger_price": cur_trig}

            try:
                self.broker.modify_gtt_order(
                    {
                        "type": "SINGLE",
                        "quantity": qty,
                        "gtt_order_id": str(gtt_id),
                        "rules": [
                            {
                                "strategy": "ENTRY",
                                "trigger_type": "BELOW",
                                "trigger_price": new_trig_r,
                                "market_protection": 5,
                            }
                        ],
                    }
                )
            except Exception as exc:
                self.store.append_audit(
                    "GTT_RATCHET_MODIFY_WARNING",
                    actor,
                    {"live_package_id": live_package_id, "gtt_id": gtt_id, "target_trigger": new_trig_r, "error": str(exc)},
                )
                return {"ok": False, "status": "GTT_MODIFY_FAILED", "error": str(exc)}

            pkg["legs"]["buy_leg"]["stop_trigger_price"] = new_trig_r
            self.store.upsert_package(pkg)
            self.store.append_audit(
                "GTT_RATCHET_MODIFIED",
                actor,
                {"live_package_id": live_package_id, "gtt_id": gtt_id, "prev_trigger": cur_trig, "new_trigger": new_trig_r},
            )
            return {"ok": True, "status": "GTT_RATCHET_UPDATED", "stop_trigger_price": new_trig_r}

    def _check_and_enforce_daily_loss_limit(
        self, session_date: str, arm_state: Dict[str, Any], open_pkgs: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if not open_pkgs:
            has_unpriced = False
            daily_unrealized = 0.0
        else:
            daily_unrealized, has_unpriced = self._estimate_open_live_mtm_inr(open_pkgs, allow_broker_fallback=True)
        closed_today = [p for p in self.store.list_packages(states=["CLOSED"], limit=100) if p["session_date"] == session_date]
        daily_realized = sum(float(p.get("realized_net_inr") or 0.0) for p in closed_today)
        daily_net = round(daily_realized + daily_unrealized, 2)
        max_loss_lim = abs(float(arm_state.get("max_daily_loss_inr") or 2500.0))

        if daily_net <= -max_loss_lim:
            self.disarm_live(
                reason=f"AUTO_DISARM_DAILY_LOSS_LIMIT:{daily_net}",
                actor="SAFETY_GUARD",
                include_paper_baseline=False,
            )
            return {
                "ok": False,
                "status": "MAX_DAILY_LOSS_REACHED",
                "daily_net_inr": daily_net,
                "daily_realized_net_inr": round(daily_realized, 2),
                "daily_unrealized_mtm_inr": round(daily_unrealized, 2),
            }
        if has_unpriced and open_pkgs:
            return {
                "ok": False,
                "status": "UNPRICED_OPEN_LIVE_EXPOSURE_HALT",
                "daily_net_inr": daily_net,
            }
        return None

    def submit_entry_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            arm_state = self.store.get_arm_state()
            if not arm_state.get("armed"):
                return {"ok": False, "status": "DISARMED_NEW_ENTRIES_BLOCKED"}

            signal_id = str(signal.get("signal_id") or "").strip()
            book_id = str(signal.get("book_id") or "").strip()
            session_date = str(signal.get("session_date") or "").strip()
            structure = str(signal.get("structure") or "SINGLE_LONG").strip()
            if not signal_id or not book_id or not session_date:
                return {"ok": False, "status": "INVALID_SIGNAL_IDENTITY"}
            if book_id not in (arm_state.get("selected_books") or []) or book_id not in ALLOWED_LIVE_BOOKS:
                return {"ok": False, "status": "BOOK_NOT_ARMED_OR_BLOCKED"}

            if not bool(signal.get("quote_fresh_ok", True)):
                return {"ok": False, "status": "STALE_OR_MISSING_QUOTE"}

            # Idempotency check BEFORE fault or limit rejection on duplicate replays
            existing_pkg = self.store.get_package_by_signal(signal_id)
            buy_intent_id = make_deterministic_intent_id(signal_id, book_id, "BUY_WING", "ENTRY", session_date)
            existing_intent = self.store.get_intent(buy_intent_id)
            if existing_pkg is not None or existing_intent is not None:
                self.store.append_audit(
                    "DUPLICATE_SIGNAL_IGNORED",
                    "ENGINE",
                    {"signal_id": signal_id, "live_package_id": existing_pkg["live_package_id"] if existing_pkg else None},
                )
                return {
                    "ok": True,
                    "status": "DUPLICATE_SIGNAL_IDEMPOTENT",
                    "idempotent_replay": True,
                    "package": existing_pkg,
                    "intent": existing_intent,
                }

            faults = self.store.count_unresolved_faults()
            if faults["total_blocking_faults"] > 0:
                return {"ok": False, "status": "HALTED_UNRESOLVED_FAULT", "unresolved_faults": faults}

            open_pkgs = self.store.list_packages(states=OPEN_LIKE_PACKAGE_STATES)
            # Check daily realized + live unrealized MTM kill limit BEFORE slot check!
            loss_block = self._check_and_enforce_daily_loss_limit(session_date, arm_state, open_pkgs)
            if loss_block is not None:
                return loss_block

            if len(open_pkgs) >= int(arm_state.get("max_open_packages") or 1):
                return {"ok": False, "status": "MAX_OPEN_PACKAGES_REACHED"}

            buy_leg = dict(signal.get("buy_leg") or {})
            buy_qty = int(buy_leg.get("qty") or 0)
            lot_size = int(buy_leg.get("lot_size") or 65)
            max_qty = int(arm_state.get("max_lots_per_book") or 1) * lot_size
            if buy_qty <= 0 or float(buy_leg.get("limit_price") or 0) <= 0:
                return {"ok": False, "status": "INVALID_BUY_LEG"}
            if buy_qty > max_qty:
                return {"ok": False, "status": "LOT_LIMIT_EXCEEDED", "requested_qty": buy_qty, "max_qty": max_qty}

            live_package_id = _make_live_package_id(signal_id, book_id, session_date)
            pkg: Dict[str, Any] = {
                "live_package_id": live_package_id,
                "signal_id": signal_id,
                "session_date": session_date,
                "book_id": book_id,
                "protocol_profile_id": str(signal.get("protocol_profile_id") or arm_state.get("protocol_profile_id") or "V2_BOOK_B_FROZEN_PAPER"),
                "structure": structure,
                "state": "OPENING_BUY_WING",
                "entry_ts_ms": _now_ms(),
                "exit_ts_ms": None,
                "exit_reason": None,
                "legs": {
                    "buy_leg": {
                        "instrument_key": buy_leg["instrument_key"],
                        "trading_symbol": buy_leg["trading_symbol"],
                        "option_type": buy_leg.get("option_type", "CE"),
                        "strike": float(buy_leg.get("strike") or 0.0),
                        "lot_size": lot_size,
                        "requested_qty": buy_qty,
                        "qty": 0,
                        "exited_qty": 0,
                        "limit_price": round_to_nse_tick(float(buy_leg["limit_price"])),
                        "entry_price": 0.0,
                        "exit_price": None,
                        "stop_trigger_price": buy_leg.get("stop_trigger_price"),
                        "stop_limit_price": buy_leg.get("stop_limit_price"),
                        "gtt_id": None,
                        "gtt_child_order_id": None,
                    }
                },
                "realized_gross_inr": 0.0,
                "realized_friction_inr": 0.0,
                "realized_net_inr": 0.0,
                "reconciled_at_iso": None,
                "unresolved_fault": None,
            }
            if structure == "DEFINED_RISK_SPREAD":
                short_leg = dict(signal.get("short_leg") or {})
                if not short_leg.get("instrument_key") or float(short_leg.get("limit_price") or 0) <= 0:
                    return {"ok": False, "status": "INVALID_SHORT_LEG"}
                short_req_qty = int(short_leg.get("qty") or buy_qty)
                try:
                    span_res = self.broker.calculate_margin(
                        [
                            {
                                "instrument_key": buy_leg["instrument_key"],
                                "quantity": buy_qty,
                                "transaction_type": "BUY",
                                "product": "D",
                                "price": float(buy_leg["limit_price"]),
                            },
                            {
                                "instrument_key": short_leg["instrument_key"],
                                "quantity": short_req_qty,
                                "transaction_type": "SELL",
                                "product": "D",
                                "price": float(short_leg["limit_price"]),
                            },
                        ]
                    )
                    funds_res = self.broker.get_funds_and_margin()
                    req_span = float(((span_res or {}).get("data") or {}).get("required_margin") or 0.0)
                    avail_m = float(
                        (((funds_res or {}).get("data") or {}).get("equity") or {}).get("available_margin") or 0.0
                    )
                except Exception as exc:
                    return {"ok": False, "status": "LIVE_SPREAD_MARGIN_UNVERIFIED", "error": str(exc)}
                min_span_floor = max(75000.0 * max(1.0, float(short_req_qty) / float(lot_size)), 1.15 * req_span)
                if req_span <= 0.0 or avail_m < min_span_floor:
                    self.store.append_audit(
                        "LIVE_SPREAD_SPAN_REJECTED",
                        "SAFETY_GUARD",
                        {
                            "signal_id": signal_id,
                            "required_margin_inr": req_span,
                            "min_required_with_buffer_inr": round(min_span_floor, 2),
                            "available_margin_inr": avail_m,
                        },
                    )
                    return {
                        "ok": False,
                        "status": "LIVE_SPREAD_SPAN_MARGIN_INSUFFICIENT",
                        "required_margin_inr": req_span,
                        "available_margin_inr": avail_m,
                    }
                pkg["legs"]["short_leg"] = {
                    "instrument_key": short_leg["instrument_key"],
                    "trading_symbol": short_leg["trading_symbol"],
                    "option_type": short_leg.get("option_type", "CE"),
                    "strike": float(short_leg.get("strike") or 0.0),
                    "lot_size": int(short_leg.get("lot_size") or lot_size),
                    "requested_qty": short_req_qty,
                    "qty": 0,
                    "exited_qty": 0,
                    "limit_price": round_to_nse_tick(float(short_leg["limit_price"])),
                    "min_live_credit_pts": float(signal.get("min_live_credit_pts") or short_leg.get("min_live_credit_pts") or 24.0),
                    "entry_price": 0.0,
                    "exit_price": None,
                }
            self.store.upsert_package(pkg)

            # STEP 1: ALWAYS execute BUY_WING first (zero naked shorts)
            buy_res = self._place_single_order_with_durability(
                signal_id=signal_id,
                live_package_id=live_package_id,
                book_id=book_id,
                leg_role="BUY_WING",
                action="ENTRY",
                session_date=session_date,
                instrument_key=buy_leg["instrument_key"],
                trading_symbol=buy_leg["trading_symbol"],
                side="BUY",
                order_type=str(buy_leg.get("order_type") or "LIMIT"),
                qty=buy_qty,
                limit_price=float(buy_leg["limit_price"]),
                market_protection=buy_leg.get("market_protection"),
            )
            if buy_res["status"] == "UNKNOWN_AMBIGUOUS":
                pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                pkg["unresolved_fault"] = "BUY_WING_TIMEOUT_AMBIGUOUS"
                self.store.upsert_package(pkg)
                self.disarm_live(
                    reason="AUTO_DISARM_ORDER_TIMEOUT_AMBIGUOUS",
                    actor="SAFETY_GUARD",
                    include_paper_baseline=False,
                )
                return {"ok": False, "status": "UNKNOWN_AMBIGUOUS", "package": pkg, "intent": buy_res["intent"]}

            buy_filled_qty = int(buy_res["intent"].get("filled_qty") or 0)
            if buy_filled_qty <= 0 or buy_res["status"] in ("REJECTED", "CANCELLED"):
                pkg["state"] = "CLOSED"
                pkg["exit_ts_ms"] = _now_ms()
                pkg["exit_reason"] = (
                    "ENTRY_BUY_UNFILLED_CANCELLED" if buy_res["status"] == "CANCELLED" else "ENTRY_BUY_REJECTED"
                )
                self.store.upsert_package(pkg)
                ret_status = "BUY_LEG_REJECTED" if buy_res["status"] == "REJECTED" else "BUY_LEG_CANCELLED"
                return {"ok": False, "status": ret_status, "package": pkg, "intent": buy_res["intent"]}

            buy_avg_px = float(buy_res["intent"].get("avg_fill_price") or buy_leg["limit_price"])
            pkg["legs"]["buy_leg"]["qty"] = buy_filled_qty
            pkg["legs"]["buy_leg"]["entry_price"] = buy_avg_px

            if structure == "SINGLE_LONG":
                # Mandatory Upstox V3 SINGLE ENTRY GTT (adopts on timeout; auto-unwinds if confirmed absent/rejected)
                gtt_res = self._place_or_recover_protective_gtt(pkg, buy_filled_qty, buy_avg_px)
                if gtt_res["ok"]:
                    pkg["legs"]["buy_leg"]["gtt_id"] = gtt_res["gtt_id"]
                    pkg["legs"]["buy_leg"]["stop_trigger_price"] = gtt_res["stop_trigger_price"]
                elif gtt_res.get("ambiguous_gtt"):
                    pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                    pkg["unresolved_fault"] = str(gtt_res.get("error") or "GTT_PLACEMENT_TIMEOUT_AMBIGUOUS")
                    self.store.upsert_package(pkg)
                    self.disarm_live(
                        reason="AUTO_DISARM_GTT_TIMEOUT_AMBIGUOUS",
                        actor="SAFETY_GUARD",
                        include_paper_baseline=False,
                    )
                    return {"ok": False, "status": "MANUAL_INTERVENTION_REQUIRED", "package": pkg}
                else:
                    self.store.append_audit(
                        "GTT_PLACEMENT_FAILED_EMERGENCY_UNWIND",
                        "SAFETY_GUARD",
                        {"live_package_id": live_package_id, "error": gtt_res.get("error")},
                    )
                    unwind_res = self._unwind_buy_wing(
                        pkg=pkg,
                        qty_to_unwind=buy_filled_qty,
                        unwind_price=buy_avg_px,
                        reason="GTT_PROTECTION_FAILED",
                    )
                    return self._finalize_failed_short_entry(
                        pkg, buy_avg_px, buy_filled_qty, unwind_res, "GTT_PROTECTION_FAILED_FLATTENED"
                    )

                pkg["state"] = "OPEN"
                self.store.upsert_package(pkg)
                self.store.append_audit(
                    "LIVE_PACKAGE_OPENED",
                    "ENGINE",
                    {
                        "live_package_id": live_package_id,
                        "structure": structure,
                        "qty": buy_filled_qty,
                        "gtt_id": pkg["legs"]["buy_leg"].get("gtt_id"),
                    },
                )
                return {"ok": True, "status": "OPEN", "package": pkg, "buy_intent": buy_res["intent"]}

            # DEFINED_RISK_SPREAD: Cap short quantity strictly at confirmed_protected_qty + apply credit floor
            short_info = pkg["legs"]["short_leg"]
            s_lot = int(short_info.get("lot_size") or lot_size)
            short_target_qty = (min(int(short_info["requested_qty"]), buy_filled_qty) // s_lot) * s_lot
            if short_target_qty <= 0:
                unwind_res = self._unwind_buy_wing(
                    pkg=pkg, qty_to_unwind=buy_filled_qty, unwind_price=buy_avg_px, reason="PARTIAL_BUY_BELOW_ONE_LOT"
                )
                return self._finalize_failed_short_entry(pkg, buy_avg_px, buy_filled_qty, unwind_res, "PARTIAL_BUY_BELOW_LOT")

            min_live_cr = float(short_info.get("min_live_credit_pts") or 24.0)
            credit_floor_px = round_to_nse_tick(buy_avg_px + min_live_cr, mode="adverse_buy", allow_zero_settlement=False)
            short_limit_px = max(float(short_info["limit_price"]), credit_floor_px)
            pkg["legs"]["short_leg"]["limit_price"] = short_limit_px
            pkg["state"] = "OPENING_SHORT"
            self.store.upsert_package(pkg)

            short_res = self._place_single_order_with_durability(
                signal_id=signal_id,
                live_package_id=live_package_id,
                book_id=book_id,
                leg_role="SHORT_LEG",
                action="ENTRY",
                session_date=session_date,
                instrument_key=short_info["instrument_key"],
                trading_symbol=short_info["trading_symbol"],
                side="SELL",
                order_type=str(signal["short_leg"].get("order_type") or "LIMIT"),
                qty=short_target_qty,
                limit_price=short_limit_px,
                market_protection=signal["short_leg"].get("market_protection"),
            )
            if short_res["status"] == "UNKNOWN_AMBIGUOUS":
                pkg["state"] = "RECONCILE_MISMATCH"
                pkg["unresolved_fault"] = "SHORT_LEG_TIMEOUT_AMBIGUOUS"
                self.store.upsert_package(pkg)
                self.disarm_live(
                    reason="AUTO_DISARM_SHORT_TIMEOUT_AMBIGUOUS",
                    actor="SAFETY_GUARD",
                    include_paper_baseline=False,
                )
                return {"ok": False, "status": "SHORT_TIMEOUT_AMBIGUOUS", "package": pkg, "short_intent": short_res["intent"]}

            short_filled_qty = int(short_res["intent"].get("filled_qty") or 0)
            if short_filled_qty <= 0 or short_res["status"] in ("REJECTED", "CANCELLED"):
                unwind_res = self._unwind_buy_wing(
                    pkg=pkg, qty_to_unwind=buy_filled_qty, unwind_price=buy_avg_px, reason="SHORT_LEG_REJECTED"
                )
                return self._finalize_failed_short_entry(pkg, buy_avg_px, buy_filled_qty, unwind_res, "SHORT_LEG_REJECTED_UNWOUND")

            short_avg_px = float(short_res["intent"].get("avg_fill_price") or short_info["limit_price"])
            pkg["legs"]["short_leg"]["qty"] = short_filled_qty
            pkg["legs"]["short_leg"]["entry_price"] = short_avg_px

            # Strictly verify confirmed_protected_qty == short_filled_qty; unwind any excess buy wing
            if buy_filled_qty > short_filled_qty:
                excess = buy_filled_qty - short_filled_qty
                unwind_excess = self._unwind_buy_wing(
                    pkg=pkg,
                    qty_to_unwind=excess,
                    unwind_price=buy_avg_px,
                    reason="EXCESS_BUY_WING_UNWIND",
                    leg_role="BUY_WING_EXCESS_UNWIND",
                )
                if not unwind_excess["ok"]:
                    pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                    pkg["unresolved_fault"] = "EXCESS_BUY_WING_UNWIND_FAILED"
                    self.store.upsert_package(pkg)
                    self.disarm_live(
                        reason="AUTO_DISARM_EXCESS_UNWIND_FAIL",
                        actor="SAFETY_GUARD",
                        include_paper_baseline=False,
                    )
                    return {"ok": False, "status": "MANUAL_INTERVENTION_REQUIRED", "package": pkg}
                self._apply_excess_buy_unwind_to_pkg(pkg, excess, float(unwind_excess["avg_price"]))

            assert pkg["legs"]["short_leg"]["qty"] <= pkg["legs"]["buy_leg"]["qty"]
            pkg["state"] = "OPEN"
            self.store.upsert_package(pkg)
            self.store.append_audit(
                "LIVE_SPREAD_OPENED",
                "ENGINE",
                {
                    "live_package_id": live_package_id,
                    "buy_qty": pkg["legs"]["buy_leg"]["qty"],
                    "short_qty": pkg["legs"]["short_leg"]["qty"],
                },
            )
            return {"ok": True, "status": "OPEN", "package": pkg, "buy_intent": buy_res["intent"], "short_intent": short_res["intent"]}

    def _apply_excess_buy_unwind_to_pkg(
        self, pkg: Dict[str, Any], excess_qty: int, unwind_avg_price: float
    ) -> None:
        buy_leg = pkg["legs"]["buy_leg"]
        lot_size = int(buy_leg.get("lot_size") or 65)
        entry_px = float(buy_leg.get("entry_price") or unwind_avg_price)
        prev_ex = pkg["legs"].get("excess_unwound_buy_leg") or {}
        prev_q = int(prev_ex.get("qty") or 0)
        if prev_q > 0:
            comb_q = prev_q + int(excess_qty)
            prev_exit = float(prev_ex.get("exit_price") or unwind_avg_price)
            comb_exit = round_to_nse_tick(((prev_q * prev_exit) + (int(excess_qty) * float(unwind_avg_price))) / comb_q)
        else:
            comb_q = int(excess_qty)
            comb_exit = float(unwind_avg_price)
        pkg["legs"]["excess_unwound_buy_leg"] = {
            "instrument_key": buy_leg["instrument_key"],
            "trading_symbol": buy_leg["trading_symbol"],
            "option_type": buy_leg.get("option_type", "CE"),
            "strike": float(buy_leg.get("strike") or 0.0),
            "lot_size": lot_size,
            "qty": comb_q,
            "entry_price": entry_px,
            "exit_price": comb_exit,
        }
        pkg["legs"]["buy_leg"]["qty"] = max(0, int(buy_leg.get("qty") or 0) - int(excess_qty))

    def _finalize_failed_short_entry(
        self,
        pkg: Dict[str, Any],
        buy_avg_px: float,
        buy_filled_qty: int,
        unwind_res: Dict[str, Any],
        exit_reason: str,
    ) -> Dict[str, Any]:
        if unwind_res["ok"]:
            b_leg = pkg["legs"]["buy_leg"]
            orig_qty = int(b_leg.get("qty") or buy_filled_qty)
            prev_ex = int(b_leg.get("exited_qty") or 0)
            prev_vw = float(b_leg.get("partial_exit_vwap") or 0.0)
            unw_px = float(unwind_res["avg_price"])
            if prev_ex > 0 and prev_vw > 0.0 and prev_ex + int(buy_filled_qty) <= orig_qty:
                comb_vw = round_to_nse_tick(((prev_ex * prev_vw) + (int(buy_filled_qty) * unw_px)) / (prev_ex + int(buy_filled_qty)))
                b_leg["exit_price"] = comb_vw
                b_leg["exited_qty"] = prev_ex + int(buy_filled_qty)
            else:
                b_leg["exit_price"] = unw_px
                b_leg["exited_qty"] = int(buy_filled_qty)
            b_leg["gtt_id"] = None
            acc = self._compute_realized_pkg_accounting(pkg)
            pkg["state"] = "CLOSED"
            pkg["exit_ts_ms"] = _now_ms()
            pkg["exit_reason"] = exit_reason
            pkg["realized_gross_inr"] = float(acc["gross_pnl"])
            pkg["realized_friction_inr"] = float(acc["total_friction"])
            pkg["realized_net_inr"] = float(acc["net_pnl"])
            pkg["reconciled_at_iso"] = now_ist_iso()
            pkg["unresolved_fault"] = None
            self.store.upsert_package(pkg)
            self.store.append_audit(
                "BUY_WING_UNWOUND_SAFELY",
                "ENGINE",
                {"live_package_id": pkg["live_package_id"], "exit_reason": exit_reason, "net_pnl_inr": acc["net_pnl"]},
            )
            return {"ok": False, "status": exit_reason, "package": pkg}
        pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
        pkg["unresolved_fault"] = f"BUY_WING_UNWIND_FAILED: {unwind_res.get('error')}"
        self.store.upsert_package(pkg)
        self.disarm_live(
            reason="AUTO_DISARM_BUY_WING_UNWIND_FAILED",
            actor="SAFETY_GUARD",
            include_paper_baseline=False,
        )
        self.store.append_audit(
            "CRITICAL_MANUAL_INTERVENTION_REQUIRED",
            "SAFETY_GUARD",
            {"live_package_id": pkg["live_package_id"], "error": unwind_res.get("error")},
        )
        return {"ok": False, "status": "MANUAL_INTERVENTION_REQUIRED", "package": pkg}

    def _execute_two_stage_exit(
        self,
        *,
        pkg: Dict[str, Any],
        leg_role: str,
        leg_key: str,
        leg: Dict[str, Any],
        exit_side: str,
        target_qty: int,
        limit_price: Optional[float],
        attempt_num: int,
    ) -> Dict[str, Any]:
        """Executes Stage-1 LIMIT collar -> Stage-2 MARKET, persisting Stage-1 fills BEFORE Stage-2."""
        filled_total = 0
        weighted_notional = 0.0
        last_status = "REJECTED"
        rem_qty = int(target_qty)

        if limit_price is not None and float(limit_price) > 0 and rem_qty > 0:
            s1 = self._place_single_order_with_durability(
                signal_id=pkg["signal_id"],
                live_package_id=pkg["live_package_id"],
                book_id=pkg["book_id"],
                leg_role=leg_role,
                action=f"EXIT_ATT_{attempt_num}_S1",
                session_date=pkg["session_date"],
                instrument_key=leg["instrument_key"],
                trading_symbol=leg["trading_symbol"],
                side=exit_side,
                order_type="LIMIT",
                qty=rem_qty,
                limit_price=float(limit_price),
                market_protection=None,
                is_emergency=True,
            )
            last_status = s1["status"]
            if last_status == "UNKNOWN_AMBIGUOUS":
                return {"ok": False, "status": "UNKNOWN_AMBIGUOUS", "filled_qty": 0, "avg_price": 0.0}
            f1 = int(s1["intent"].get("filled_qty") or 0)
            if f1 > 0:
                px1 = float(s1["intent"].get("avg_fill_price") or limit_price)
                filled_total += f1
                weighted_notional += f1 * px1
                rem_qty -= f1
                # Immediately persist Stage-1 partial progress BEFORE Stage-2 runs!
                tot_ex, vw_ex = self._recompute_leg_exit_progress(pkg["live_package_id"], leg_role)
                pkg["legs"][leg_key]["exited_qty"] = tot_ex
                pkg["legs"][leg_key]["partial_exit_vwap"] = vw_ex
                self.store.upsert_package(pkg)

        if rem_qty > 0:
            s2 = self._place_single_order_with_durability(
                signal_id=pkg["signal_id"],
                live_package_id=pkg["live_package_id"],
                book_id=pkg["book_id"],
                leg_role=leg_role,
                action=f"EXIT_ATT_{attempt_num}_S2",
                session_date=pkg["session_date"],
                instrument_key=leg["instrument_key"],
                trading_symbol=leg["trading_symbol"],
                side=exit_side,
                order_type="MARKET",
                qty=rem_qty,
                limit_price=None,
                market_protection=5,
                is_emergency=True,
            )
            last_status = s2["status"]
            if last_status == "UNKNOWN_AMBIGUOUS":
                return {
                    "ok": False,
                    "status": "UNKNOWN_AMBIGUOUS",
                    "filled_qty": filled_total,
                    "avg_price": round_to_nse_tick(weighted_notional / filled_total) if filled_total > 0 else 0.0,
                }
            f2 = int(s2["intent"].get("filled_qty") or 0)
            if f2 > 0:
                px2 = float(s2["intent"].get("avg_fill_price") or leg.get("entry_price") or 0.0)
                filled_total += f2
                weighted_notional += f2 * px2
                tot_ex, vw_ex = self._recompute_leg_exit_progress(pkg["live_package_id"], leg_role)
                pkg["legs"][leg_key]["exited_qty"] = tot_ex
                pkg["legs"][leg_key]["partial_exit_vwap"] = vw_ex
                self.store.upsert_package(pkg)

        avg_vwap = round_to_nse_tick(weighted_notional / filled_total) if filled_total > 0 else 0.0
        return {
            "ok": filled_total >= int(target_qty),
            "status": "FILLED" if filled_total >= int(target_qty) else ("PARTIAL_FILL" if filled_total > 0 else last_status),
            "filled_qty": filled_total,
            "avg_price": avg_vwap,
        }

    def exit_live_package(
        self,
        *,
        live_package_id: str,
        reason: str = "MANUAL_HUMAN_SQUARE_OFF",
        exit_price: Optional[float] = None,
        short_exit_price: Optional[float] = None,
        actor: str = "HUMAN_UI",
    ) -> Dict[str, Any]:
        """Exit an open live package safely. Explicitly permitted even while DISARMED."""
        with self._lock:
            pkg = self.store.get_package(live_package_id)
            if not pkg:
                raise KeyError(f"Unknown live package: {live_package_id}")
            if pkg["state"] == "CLOSED":
                return {"ok": True, "status": "ALREADY_CLOSED", "package": pkg}

            pkg_intents = self.store.list_intents(live_package_id=live_package_id, limit=250)
            if any(it["state"] in ("UNKNOWN_AMBIGUOUS", "PENDING_SEND", "SENT") for it in pkg_intents):
                pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                pkg["unresolved_fault"] = "EXIT_BLOCKED_AMBIGUOUS_INTENT_RECONCILE_FIRST"
                self.store.upsert_package(pkg)
                return {"ok": False, "status": "EXIT_AMBIGUOUS_RECONCILE_FIRST", "package": pkg}

            pkg["state"] = "CLOSING"
            self.store.upsert_package(pkg)
            buy_leg = pkg["legs"].get("buy_leg") or {}
            short_leg = pkg["legs"].get("short_leg") or {}

            # 1. Check GTT state before cancelling, then cancel active protective GTT (if present)
            gtt_id = buy_leg.get("gtt_id")
            gtt_was_scheduled = False
            gtt_cancel_errored = False
            if gtt_id:
                try:
                    pre_gtts = self.broker.get_gtt_orders()
                    gtt_was_scheduled = any(str(g.get("gtt_order_id") or "") == str(gtt_id) for g in pre_gtts)
                except Exception:
                    gtt_was_scheduled = False
                try:
                    self.broker.cancel_gtt_order(str(gtt_id), is_emergency=True)
                except Exception as exc:
                    gtt_cancel_errored = True
                    self.store.append_audit(
                        "GTT_CANCEL_WARNING",
                        actor,
                        {"live_package_id": live_package_id, "gtt_id": gtt_id, "error": str(exc)},
                    )

            # 2. ALWAYS query live broker orders & net positions BEFORE placing ANY exit order
            broker_orders = self.broker.get_orders()
            broker_positions = self.broker.get_positions()
            pos_by_key = {str(bp.get("instrument_token") or ""): int(bp.get("quantity") or 0) for bp in broker_positions}

            # 3. For spreads, ALWAYS cover SHORT_LEG BEFORE selling BUY_WING
            if short_leg and int(short_leg.get("qty") or 0) > 0 and short_leg.get("exit_price") is None:
                s_already, s_vwap = self._recompute_leg_exit_progress(live_package_id, "SHORT_LEG")
                if s_already >= int(short_leg["qty"]):
                    pkg["legs"]["short_leg"]["exited_qty"] = s_already
                    pkg["legs"]["short_leg"]["partial_exit_vwap"] = s_vwap
                    pkg["legs"]["short_leg"]["exit_price"] = s_vwap
                else:
                    s_v2_rem = max(0, int(short_leg["qty"]) - s_already)
                    s_pos = pos_by_key.get(str(short_leg["instrument_key"]), 0)
                    s_to_cover = min(s_v2_rem, abs(s_pos)) if s_pos < 0 else 0
                    if s_to_cover <= 0:
                        pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                        pkg["unresolved_fault"] = "SHORT_POSITION_FLAT_WITHOUT_MATCHED_V2_EXIT"
                        self.store.upsert_package(pkg)
                        return {"ok": False, "status": "RECONCILE_REQUIRED_SHORT_FLAT", "package": pkg}
                    s_attempts = sum(
                        1 for it in pkg_intents if it["leg_role"] == "SHORT_LEG" and str(it["action"]).startswith("EXIT")
                    )
                    s_res = self._execute_two_stage_exit(
                        pkg=pkg,
                        leg_role="SHORT_LEG",
                        leg_key="short_leg",
                        leg=short_leg,
                        exit_side="BUY",
                        target_qty=s_to_cover,
                        limit_price=short_exit_price,
                        attempt_num=s_attempts + 1,
                    )
                    s_tot, s_vw = self._recompute_leg_exit_progress(live_package_id, "SHORT_LEG")
                    pkg["legs"]["short_leg"]["exited_qty"] = s_tot
                    pkg["legs"]["short_leg"]["partial_exit_vwap"] = s_vw
                    if s_res["status"] == "UNKNOWN_AMBIGUOUS":
                        pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                        pkg["unresolved_fault"] = "SHORT_EXIT_UNKNOWN_AMBIGUOUS"
                        self.store.upsert_package(pkg)
                        self.disarm_live(
                            reason="AUTO_DISARM_SHORT_EXIT_AMBIGUOUS",
                            actor="SAFETY_GUARD",
                            include_paper_baseline=False,
                        )
                        return {"ok": False, "status": "UNKNOWN_AMBIGUOUS", "package": pkg}
                    if s_tot < int(short_leg["qty"]):
                        pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                        pkg["unresolved_fault"] = f"SHORT_EXIT_FAILED: {s_res['status']}"
                        self.store.upsert_package(pkg)
                        return {"ok": False, "status": s_res["status"], "package": pkg}
                    pkg["legs"]["short_leg"]["exit_price"] = s_vw

            # 4. Close BUY_WING safely (prevents double-sell on triggered GTT & isolates foreign V1 shares)
            if buy_leg and int(buy_leg.get("qty") or 0) > 0 and buy_leg.get("exit_price") is None:
                b_already, b_vwap = self._recompute_leg_exit_progress(live_package_id, "BUY_WING")
                if b_already >= int(buy_leg["qty"]):
                    pkg["legs"]["buy_leg"]["exited_qty"] = b_already
                    pkg["legs"]["buy_leg"]["partial_exit_vwap"] = b_vwap
                    pkg["legs"]["buy_leg"]["exit_price"] = b_vwap
                    pkg["legs"]["buy_leg"]["gtt_id"] = None
                else:
                    b_ikey = str(buy_leg["instrument_key"])
                    account_pos_qty = max(0, pos_by_key.get(b_ikey, 0))
                    b_v2_rem = max(0, int(buy_leg["qty"]) - b_already)

                    # Detect GTT-triggered flatten EVEN when shared V1/manual keeps account_pos_qty > 0!
                    matched_gtt_sell = self._find_unclaimed_gtt_child_sell(
                        live_package_id=live_package_id,
                        ikey=b_ikey,
                        entry_ts_ms=int(pkg.get("entry_ts_ms") or 0),
                        broker_orders=broker_orders,
                        gtt_id=str(gtt_id) if gtt_id else None,
                    )
                    gtt_triggered_shared = bool(
                        pkg["structure"] == "SINGLE_LONG"
                        and b_v2_rem > 0
                        and (
                            matched_gtt_sell is not None
                            or account_pos_qty <= 0
                            or (gtt_id and (not gtt_was_scheduled or gtt_cancel_errored))
                        )
                        and (account_pos_qty <= 0 or matched_gtt_sell is not None)
                    )
                    if gtt_triggered_shared:
                        trig_px = float(
                            (matched_gtt_sell or {}).get("average_price")
                            or buy_leg.get("stop_trigger_price")
                            or buy_leg["entry_price"]
                        )
                        tot_b = b_already + b_v2_rem
                        comb_px = round_to_nse_tick(
                            ((b_already * b_vwap) + (b_v2_rem * trig_px)) / tot_b
                        ) if b_already > 0 else round_to_nse_tick(trig_px)
                        pkg["legs"]["buy_leg"]["exit_price"] = comb_px
                        pkg["legs"]["buy_leg"]["exited_qty"] = int(buy_leg["qty"])
                        pkg["legs"]["buy_leg"]["partial_exit_vwap"] = comb_px
                        pkg["legs"]["buy_leg"]["gtt_id"] = None
                        pkg["legs"]["buy_leg"]["gtt_child_order_id"] = (matched_gtt_sell or {}).get("order_id")
                        acc = self._compute_realized_pkg_accounting(pkg)
                        pkg["state"] = "CLOSED"
                        pkg["exit_ts_ms"] = _now_ms()
                        pkg["exit_reason"] = "CLOSED_BY_TRIGGERED_GTT_NO_DOUBLE_SELL"
                        pkg["realized_gross_inr"] = float(acc["gross_pnl"])
                        pkg["realized_friction_inr"] = float(acc["total_friction"])
                        pkg["realized_net_inr"] = float(acc["net_pnl"])
                        pkg["reconciled_at_iso"] = now_ist_iso()
                        pkg["unresolved_fault"] = None
                        self.store.upsert_package(pkg)
                        self.store.append_audit(
                            "GTT_RACE_PREVENTED_DOUBLE_SELL",
                            actor,
                            {
                                "live_package_id": live_package_id,
                                "gtt_id": gtt_id,
                                "child_order_id": (matched_gtt_sell or {}).get("order_id"),
                                "exit_price": comb_px,
                                "account_pos_qty_remaining": account_pos_qty,
                                "net_pnl_inr": acc["net_pnl"],
                            },
                        )
                        return {"ok": True, "status": "CLOSED_BY_TRIGGERED_GTT_NO_DOUBLE_SELL", "package": pkg}

                    target_sell_qty = min(b_v2_rem, account_pos_qty)
                    if target_sell_qty <= 0:
                        pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                        pkg["unresolved_fault"] = "BUY_POSITION_FLAT_WITHOUT_MATCHED_EXIT"
                        self.store.upsert_package(pkg)
                        return {"ok": False, "status": "RECONCILE_REQUIRED_BUY_FLAT", "package": pkg}

                    b_attempts = sum(
                        1 for it in pkg_intents if it["leg_role"] == "BUY_WING" and str(it["action"]).startswith("EXIT")
                    )
                    b_res = self._execute_two_stage_exit(
                        pkg=pkg,
                        leg_role="BUY_WING",
                        leg_key="buy_leg",
                        leg=buy_leg,
                        exit_side="SELL",
                        target_qty=target_sell_qty,
                        limit_price=exit_price,
                        attempt_num=b_attempts + 1,
                    )
                    b_tot, b_vw = self._recompute_leg_exit_progress(live_package_id, "BUY_WING")
                    pkg["legs"]["buy_leg"]["exited_qty"] = b_tot
                    pkg["legs"]["buy_leg"]["partial_exit_vwap"] = b_vw
                    if b_res["status"] == "UNKNOWN_AMBIGUOUS":
                        pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                        pkg["unresolved_fault"] = "BUY_EXIT_UNKNOWN_AMBIGUOUS"
                        self.store.upsert_package(pkg)
                        self.disarm_live(
                            reason="AUTO_DISARM_BUY_EXIT_AMBIGUOUS",
                            actor="SAFETY_GUARD",
                            include_paper_baseline=False,
                        )
                        return {"ok": False, "status": "UNKNOWN_AMBIGUOUS", "package": pkg}
                    if b_tot < int(buy_leg["qty"]):
                        pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                        pkg["unresolved_fault"] = f"BUY_EXIT_FAILED: {b_res['status']}"
                        self.store.upsert_package(pkg)
                        return {"ok": False, "status": b_res["status"], "package": pkg}
                    pkg["legs"]["buy_leg"]["exit_price"] = b_vw
                    pkg["legs"]["buy_leg"]["gtt_id"] = None

            acc = self._compute_realized_pkg_accounting(pkg)
            pkg["state"] = "CLOSED"
            pkg["exit_ts_ms"] = _now_ms()
            pkg["exit_reason"] = reason
            pkg["realized_gross_inr"] = float(acc["gross_pnl"])
            pkg["realized_friction_inr"] = float(acc["total_friction"])
            pkg["realized_net_inr"] = float(acc["net_pnl"])
            pkg["reconciled_at_iso"] = now_ist_iso()
            pkg["unresolved_fault"] = None
            self.store.upsert_package(pkg)
            self.store.append_audit(
                "LIVE_PACKAGE_CLOSED",
                actor,
                {"live_package_id": live_package_id, "reason": reason, "net_pnl_inr": acc["net_pnl"]},
            )
            return {"ok": True, "status": "CLOSED", "package": pkg}

    def on_paper_lifecycle_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Non-blocking paper -> live bridge: enqueues onto OSV2Live-ObserverWorker when async
        dispatch is enabled so broker HTTP timeouts NEVER stall V2 queue worker ticks.
        """
        if not isinstance(event, dict) or event.get("run_mode") != "FORWARD_PAPER":
            return None
        if self._async_observer_dispatch:
            self._ensure_observer_worker_started()
            try:
                self._observer_queue.put_nowait(dict(event))
                return {"ok": True, "status": "QUEUED_ASYNC"}
            except queue.Full:
                logger.error("OSV2Live-ObserverWorker queue full (256); dropping lifecycle event %s", event.get("event_type"))
                return {"ok": False, "status": "OBSERVER_QUEUE_FULL"}
        return self._process_paper_lifecycle_event_sync(event)

    def _process_paper_lifecycle_event_sync(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(event, dict) or event.get("run_mode") != "FORWARD_PAPER":
            return None
        etype = str(event.get("event_type") or "")

        if etype == "PACKAGE_OPENED":
            if not self.store.db_exists():
                return None
            arm_state = self.store.get_arm_state()
            if not arm_state.get("armed"):
                return {"ok": False, "status": "DISARMED_IGNORED"}
            pkg_snap = event.get("package_snapshot") or {}
            ls = pkg_snap.get("legs_state") or {}
            structure = str(event.get("structure") or "SINGLE_LONG")
            book_id_ev = str(event.get("book_id") or pkg_snap.get("book_id") or "")
            selected_live = set(arm_state.get("selected_books") or [])
            if book_id_ev not in selected_live or book_id_ev not in ALLOWED_LIVE_BOOKS:
                return {"ok": False, "status": f"BOOK_EXCLUDED_BY_LIVE_SCOPE:{book_id_ev}"}

            max_lots = max(1, int(arm_state.get("max_lots_per_book") or 1))
            if structure == "SINGLE_LONG":
                ll = ls.get("long_leg") or {}
                q_snap = event.get("quote_snapshot") or {}
                ref_px = float(q_snap.get("raw_ask") or q_snap.get("ask") or ll.get("entry_price") or 0.0)
                lot_sz = int(ll.get("lot_size") or 65)
                live_sig = {
                    "signal_id": str(event.get("signal_id") or pkg_snap.get("signal_id") or ""),
                    "session_date": str(event.get("session_date") or pkg_snap.get("session_date") or ""),
                    "book_id": book_id_ev,
                    "protocol_profile_id": str(arm_state.get("protocol_profile_id") or "V2_BOOK_B_FROZEN_PAPER"),
                    "structure": "SINGLE_LONG",
                    "quote_fresh_ok": True,
                    "buy_leg": {
                        "instrument_key": ll["instrument_key"],
                        "trading_symbol": ll.get("trading_symbol") or ll["instrument_key"],
                        "option_type": ll.get("option_type", "CE"),
                        "strike": float(ll.get("strike") or 0.0),
                        "qty": max_lots * lot_sz,
                        "lot_size": lot_sz,
                        "limit_price": compute_smart_limit_collar(ref_px, "BUY") if ref_px > 0 else 0.0,
                        "order_type": "LIMIT",
                        "stop_trigger_price": ls.get("hard_stop"),
                    },
                }
                return self.submit_entry_signal(live_sig)
            if structure == "DEFINED_RISK_SPREAD" and book_id_ev == "V2_BOOK_A_INTRADAY_CREDIT":
                wl = ls.get("wing_leg") or {}
                sl = ls.get("short_leg") or ls.get("short_target") or {}
                wq = event.get("wing_quote_snapshot") or {}
                sq = event.get("quote_snapshot") or {}
                w_ref = float(wq.get("raw_ask") or wq.get("ask") or wl.get("entry_price") or 0.0)
                s_ref = float(sq.get("raw_bid") or sq.get("bid") or sl.get("entry_price") or 0.0)
                lot_sz = int(wl.get("lot_size") or sl.get("lot_size") or 65)
                qty_l = max_lots * lot_sz
                min_cr = float(event.get("min_live_credit_pts") or 24.0)
                live_sig = {
                    "signal_id": str(event.get("signal_id") or pkg_snap.get("signal_id") or ""),
                    "session_date": str(event.get("session_date") or pkg_snap.get("session_date") or ""),
                    "book_id": book_id_ev,
                    "protocol_profile_id": str(arm_state.get("protocol_profile_id") or "V2_4_PAPER_DUAL_BOOK"),
                    "structure": "DEFINED_RISK_SPREAD",
                    "quote_fresh_ok": True,
                    "min_live_credit_pts": min_cr,
                    "buy_leg": {
                        "instrument_key": wl["instrument_key"],
                        "trading_symbol": wl.get("trading_symbol") or wl["instrument_key"],
                        "option_type": wl.get("option_type", "PE"),
                        "strike": float(wl.get("strike") or 0.0),
                        "qty": qty_l,
                        "lot_size": lot_sz,
                        "limit_price": compute_smart_limit_collar(w_ref, "BUY") if w_ref > 0 else 0.0,
                        "order_type": "LIMIT",
                    },
                    "short_leg": {
                        "instrument_key": sl["instrument_key"],
                        "trading_symbol": sl.get("trading_symbol") or sl["instrument_key"],
                        "option_type": sl.get("option_type", "PE"),
                        "strike": float(sl.get("strike") or 0.0),
                        "qty": qty_l,
                        "lot_size": lot_sz,
                        "limit_price": compute_smart_limit_collar(s_ref, "SELL") if s_ref > 0 else 0.0,
                        "min_live_credit_pts": min_cr,
                        "order_type": "LIMIT",
                    },
                }
                return self.submit_entry_signal(live_sig)
            return {"ok": False, "status": f"UNSUPPORTED_AUTONOMOUS_STRUCTURE:{structure}"}

        if etype == "RATCHET_UPDATED":
            if not self.store.db_exists():
                return None
            sig_id = str(event.get("signal_id") or "")
            pkg = self.store.get_package_by_signal(sig_id)
            res = None
            if pkg and pkg["state"] == "OPEN" and pkg["structure"] == "SINGLE_LONG" and event.get("new_ratchet_stop") is not None:
                res = self.sync_protective_ratchet_stop(
                    live_package_id=pkg["live_package_id"],
                    new_stop_trigger=float(event["new_ratchet_stop"]),
                    actor="V2_STRATEGY_RATCHET",
                )
            with self._lock:
                arm_state = self.store.get_arm_state()
                if arm_state.get("armed"):
                    open_pkgs = self.store.list_packages(states=OPEN_LIKE_PACKAGE_STATES)
                    self._check_and_enforce_daily_loss_limit(
                        str(event.get("session_date") or now_ist_date()), arm_state, open_pkgs
                    )
            return res

        if etype == "PACKAGE_CLOSED":
            if not self.store.db_exists():
                return None
            sig_id = str(event.get("signal_id") or "")
            pkg = self.store.get_package_by_signal(sig_id)
            if pkg and pkg["state"] in OPEN_LIKE_PACKAGE_STATES:
                q_map = event.get("quote_by_ikey") or {}
                buy_ikey = str((pkg["legs"].get("buy_leg") or {}).get("instrument_key") or "")
                qb = q_map.get(buy_ikey) or {}
                raw_bid = float(qb.get("raw_bid") or qb.get("bid") or 0.0)
                exit_collar = compute_smart_limit_collar(raw_bid, "SELL") if raw_bid > 0 else None
                short_leg_p = pkg["legs"].get("short_leg") or {}
                short_exit_collar = None
                if short_leg_p.get("instrument_key"):
                    qs = q_map.get(str(short_leg_p["instrument_key"])) or {}
                    raw_ask_s = float(qs.get("raw_ask") or qs.get("ask") or 0.0)
                    short_exit_collar = compute_smart_limit_collar(raw_ask_s, "BUY") if raw_ask_s > 0 else None
                return self.exit_live_package(
                    live_package_id=pkg["live_package_id"],
                    reason=f"AUTO_{event.get('exit_reason') or 'STRATEGY_EXIT'}",
                    exit_price=exit_collar,
                    short_exit_price=short_exit_collar,
                    actor="V2_STRATEGY_OBSERVER",
                )
            return None

        return None

    def reconcile_with_broker(self, *, actor: str = "RECONCILER") -> Dict[str, Any]:
        """Reconciles pending/ambiguous V2 intents, GTT protection, partial exits, and positions."""
        with self._lock:
            broker_orders = self.broker.get_orders()
            broker_positions = self.broker.get_positions()
            active_gtts = self.broker.get_gtt_orders()

            v2_orders_by_tag = {
                str(o.get("tag") or ""): o
                for o in broker_orders
                if str(o.get("tag") or "").startswith("V2-")
            }
            orders_by_id = {str(o.get("order_id") or ""): o for o in broker_orders if o.get("order_id")}
            pos_by_key = {str(p.get("instrument_token") or ""): int(p.get("quantity") or 0) for p in broker_positions}
            active_gtt_ids = {str(g.get("gtt_order_id") or "") for g in active_gtts if g.get("gtt_order_id")}

            resolved_intents: List[Dict[str, Any]] = []
            ambiguous_intents = self.store.list_intents(states=["UNKNOWN_AMBIGUOUS", "PENDING_SEND", "SENT"])
            refresh_positions_after_intents = False
            for intent in ambiguous_intents:
                matched = orders_by_id.get(str(intent.get("broker_order_id") or "")) or v2_orders_by_tag.get(str(intent["tag"]))
                pkg = self.store.get_package(intent["live_package_id"])
                if matched is not None:
                    m_status = str(matched.get("status") or "").lower().strip()
                    if m_status not in _TERMINAL_BROKER_ORDER_STATUSES:
                        settled = self._settle_placed_order(
                            oid=str(matched.get("order_id") or ""),
                            tag=str(intent["tag"]),
                            requested_qty=int(intent["requested_qty"]),
                            fallback_px=float(intent.get("limit_price") or 0.0),
                            inline_order=matched,
                        )
                        if settled["state"] == "UNKNOWN_AMBIGUOUS":
                            resolved_intents.append(
                                {"intent_id": intent["intent_id"], "resolution": "STILL_AMBIGUOUS_NON_TERMINAL"}
                            )
                            continue
                        new_state = settled["state"]
                        fqty = int(settled["filled_qty"])
                        fpx = float(settled["avg_px"])
                        matched = settled.get("snapshot") or matched
                    else:
                        fqty = int(matched.get("filled_quantity") or 0)
                        fpx = float(matched.get("average_price") or intent.get("limit_price") or 0.0)
                        if m_status == "complete" and fqty >= int(intent["requested_qty"]):
                            new_state = "FILLED"
                        elif fqty > 0:
                            new_state = "PARTIAL_FILL"
                        elif "cancel" in m_status:
                            new_state = "CANCELLED"
                        else:
                            new_state = "REJECTED"
                    self.store.update_intent(
                        intent["intent_id"],
                        state=new_state,
                        updated_ts_ms=_now_ms(),
                        filled_qty=fqty,
                        avg_fill_price=fpx,
                        broker_order_id=str(matched.get("order_id") or ""),
                        error_detail=None,
                        raw_response=matched,
                    )
                    resolved_intents.append({"intent_id": intent["intent_id"], "resolution": f"MATCHED_{new_state}"})

                    if pkg and intent["leg_role"] == "BUY_WING" and intent["action"] == "ENTRY":
                        if fqty > 0:
                            pkg["legs"]["buy_leg"]["qty"] = fqty
                            pkg["legs"]["buy_leg"]["entry_price"] = fpx
                            if pkg["structure"] == "SINGLE_LONG":
                                existing_gid = str(pkg["legs"]["buy_leg"].get("gtt_id") or "")
                                if existing_gid and existing_gid in active_gtt_ids:
                                    pkg["state"] = "OPEN"
                                    pkg["unresolved_fault"] = None
                                else:
                                    gtt_res = self._place_or_recover_protective_gtt(pkg, fqty, fpx)
                                    if gtt_res["ok"]:
                                        pkg["legs"]["buy_leg"]["gtt_id"] = gtt_res["gtt_id"]
                                        pkg["legs"]["buy_leg"]["stop_trigger_price"] = gtt_res["stop_trigger_price"]
                                        active_gtt_ids.add(str(gtt_res["gtt_id"]))
                                        pkg["state"] = "OPEN"
                                        pkg["unresolved_fault"] = None
                                    elif gtt_res.get("ambiguous_gtt"):
                                        pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                                        pkg["unresolved_fault"] = str(gtt_res.get("error") or "GTT_PLACEMENT_TIMEOUT_AMBIGUOUS")
                                    else:
                                        unw = self._unwind_buy_wing(
                                            pkg=pkg, qty_to_unwind=fqty, unwind_price=fpx, reason="RECONCILE_GTT_FAILED"
                                        )
                                        refresh_positions_after_intents = True
                                        self._finalize_failed_short_entry(pkg, fpx, fqty, unw, "RECONCILE_GTT_FAILED_UNWOUND")
                                        continue
                            elif pkg["structure"] == "DEFINED_RISK_SPREAD":
                                s_qty_cur = int((pkg["legs"].get("short_leg") or {}).get("qty") or 0)
                                if s_qty_cur <= 0:
                                    unw = self._unwind_buy_wing(
                                        pkg=pkg, qty_to_unwind=fqty, unwind_price=fpx, reason="RECONCILE_INCOMPLETE_SPREAD"
                                    )
                                    refresh_positions_after_intents = True
                                    self._finalize_failed_short_entry(pkg, fpx, fqty, unw, "RECONCILED_INCOMPLETE_SPREAD_UNWOUND")
                                    continue
                                pkg["state"] = "OPEN"
                                pkg["unresolved_fault"] = None
                            pkg["reconciled_at_iso"] = now_ist_iso()
                            self.store.upsert_package(pkg)
                        elif new_state in ("REJECTED", "CANCELLED") and int((pkg["legs"].get("buy_leg") or {}).get("qty") or 0) <= 0:
                            pkg["state"] = "CLOSED"
                            pkg["exit_ts_ms"] = _now_ms()
                            pkg["exit_reason"] = f"RECONCILED_ENTRY_{new_state}"
                            pkg["reconciled_at_iso"] = now_ist_iso()
                            pkg["unresolved_fault"] = None
                            self.store.upsert_package(pkg)
                    elif pkg and intent["leg_role"] == "SHORT_LEG" and intent["action"] == "ENTRY":
                        b_leg = pkg["legs"].get("buy_leg") or {}
                        b_qty = int(b_leg.get("qty") or 0)
                        b_px = float(b_leg.get("entry_price") or 0.0)
                        if fqty > 0:
                            pkg["legs"]["short_leg"]["qty"] = fqty
                            pkg["legs"]["short_leg"]["entry_price"] = fpx
                            if b_qty > fqty:
                                unw_ex = self._unwind_buy_wing(
                                    pkg=pkg,
                                    qty_to_unwind=(b_qty - fqty),
                                    unwind_price=b_px,
                                    reason="RECONCILE_EXCESS_BUY_UNWIND",
                                    leg_role="BUY_WING_EXCESS_UNWIND",
                                )
                                refresh_positions_after_intents = True
                                if unw_ex["ok"]:
                                    self._apply_excess_buy_unwind_to_pkg(pkg, (b_qty - fqty), float(unw_ex["avg_price"]))
                                    pkg["state"] = "OPEN"
                                    pkg["unresolved_fault"] = None
                                else:
                                    pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                                    pkg["unresolved_fault"] = f"RECONCILE_EXCESS_BUY_UNWIND_FAILED: {unw_ex.get('error') or unw_ex.get('status')}"
                            elif fqty > b_qty:
                                pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                                pkg["unresolved_fault"] = f"RECONCILE_SHORT_EXCEEDS_BUY:{fqty}>{b_qty}"
                            else:
                                pkg["state"] = "OPEN"
                                pkg["unresolved_fault"] = None
                            pkg["reconciled_at_iso"] = now_ist_iso()
                            self.store.upsert_package(pkg)
                        elif new_state in ("REJECTED", "CANCELLED") and b_qty > 0 and b_leg.get("exit_price") is None:
                            unw = self._unwind_buy_wing(
                                pkg=pkg, qty_to_unwind=b_qty, unwind_price=b_px, reason="RECONCILE_SHORT_REJECTED"
                            )
                            refresh_positions_after_intents = True
                            self._finalize_failed_short_entry(pkg, b_px, b_qty, unw, f"RECONCILED_SHORT_{new_state}_UNWOUND")
                    elif pkg and intent["leg_role"] == "BUY_WING_EXCESS_UNWIND" and fqty > 0:
                        b_leg = pkg["legs"].get("buy_leg") or {}
                        s_qty_cur = int((pkg["legs"].get("short_leg") or {}).get("qty") or 0)
                        if s_qty_cur > 0 and int(b_leg.get("qty") or 0) > s_qty_cur and not pkg["legs"].get("excess_unwound_buy_leg"):
                            deduct = min(fqty, int(b_leg["qty"]) - s_qty_cur)
                            self._apply_excess_buy_unwind_to_pkg(pkg, deduct, fpx)
                            pkg["reconciled_at_iso"] = now_ist_iso()
                            self.store.upsert_package(pkg)
                else:
                    self.store.update_intent(
                        intent["intent_id"],
                        state="EXPIRED_NOT_FOUND",
                        updated_ts_ms=_now_ms(),
                        error_detail="RECONCILED_NOT_FOUND_AT_BROKER",
                    )
                    resolved_intents.append({"intent_id": intent["intent_id"], "resolution": "EXPIRED_NOT_FOUND"})
                    if pkg and intent["action"] == "ENTRY":
                        if intent["leg_role"] == "BUY_WING" and pkg["state"] in ("MANUAL_INTERVENTION_REQUIRED", "OPENING_BUY_WING"):
                            pkg["state"] = "CLOSED"
                            pkg["exit_ts_ms"] = _now_ms()
                            pkg["exit_reason"] = "RECONCILED_ENTRY_NOT_RECEIVED"
                            pkg["reconciled_at_iso"] = now_ist_iso()
                            pkg["unresolved_fault"] = None
                            self.store.upsert_package(pkg)
                        elif intent["leg_role"] == "SHORT_LEG":
                            b_leg = pkg["legs"].get("buy_leg") or {}
                            b_qty = int(b_leg.get("qty") or 0)
                            b_px = float(b_leg.get("entry_price") or 0.0)
                            if b_qty > 0 and b_leg.get("exit_price") is None:
                                unw = self._unwind_buy_wing(
                                    pkg=pkg, qty_to_unwind=b_qty, unwind_price=b_px, reason="RECONCILE_SHORT_NOT_RECEIVED"
                                )
                                refresh_positions_after_intents = True
                                self._finalize_failed_short_entry(pkg, b_px, b_qty, unw, "RECONCILED_SHORT_NOT_RECEIVED_UNWOUND")

            if refresh_positions_after_intents:
                try:
                    broker_positions = self.broker.get_positions()
                    pos_by_key = {str(p.get("instrument_token") or ""): int(p.get("quantity") or 0) for p in broker_positions}
                except Exception:
                    pass

            # Reconcile all open-like packages: exit intent progress, GTT fills, and retryable states
            pkg_reconciliations: List[Dict[str, Any]] = []
            for pkg in self.store.list_packages(states=OPEN_LIKE_PACKAGE_STATES):
                pid = pkg["live_package_id"]
                buy_leg = pkg["legs"].get("buy_leg") or {}
                short_leg = pkg["legs"].get("short_leg") or {}
                ikey = str(buy_leg.get("instrument_key") or "")
                actual_qty = pos_by_key.get(ikey, 0)

                # Update exit progress from settled EXIT / BUY_WING_UNWIND intents (EXCLUDE BUY_WING_EXCESS_UNWIND!)
                if int(buy_leg.get("qty") or 0) > 0:
                    b_ex, b_vw = self._recompute_leg_exit_progress(pid, "BUY_WING")
                    for it in self.store.list_intents(live_package_id=pid, limit=250):
                        if (
                            it["leg_role"] == "BUY_WING_UNWIND"
                            and it["state"] in ("FILLED", "PARTIAL_FILL")
                            and int(it.get("filled_qty") or 0) > 0
                        ):
                            uw_q = int(it["filled_qty"])
                            uw_px = float(it.get("avg_fill_price") or buy_leg["entry_price"])
                            b_vw = round_to_nse_tick(((b_ex * b_vw) + (uw_q * uw_px)) / (b_ex + uw_q)) if b_ex > 0 else uw_px
                            b_ex += uw_q
                    pkg["legs"]["buy_leg"]["exited_qty"] = b_ex
                    if b_vw > 0:
                        pkg["legs"]["buy_leg"]["partial_exit_vwap"] = b_vw
                    if b_ex >= int(buy_leg["qty"]):
                        pkg["legs"]["buy_leg"]["exit_price"] = b_vw
                        pkg["legs"]["buy_leg"]["gtt_id"] = None

                if short_leg and int(short_leg.get("qty") or 0) > 0:
                    s_ex, s_vw = self._recompute_leg_exit_progress(pid, "SHORT_LEG")
                    pkg["legs"]["short_leg"]["exited_qty"] = s_ex
                    if s_vw > 0:
                        pkg["legs"]["short_leg"]["partial_exit_vwap"] = s_vw
                    if s_ex >= int(short_leg["qty"]):
                        pkg["legs"]["short_leg"]["exit_price"] = s_vw

                buy_done = int(buy_leg.get("qty") or 0) <= 0 or pkg["legs"]["buy_leg"].get("exit_price") is not None
                short_done = (not short_leg) or int(short_leg.get("qty") or 0) <= 0 or pkg["legs"]["short_leg"].get("exit_price") is not None
                if buy_done and short_done and int(buy_leg.get("qty") or 0) > 0:
                    acc = self._compute_realized_pkg_accounting(pkg)
                    pkg["state"] = "CLOSED"
                    pkg["exit_ts_ms"] = pkg.get("exit_ts_ms") or _now_ms()
                    pkg["exit_reason"] = pkg.get("exit_reason") or "RECONCILED_EXIT_COMPLETE"
                    pkg["realized_gross_inr"] = float(acc["gross_pnl"])
                    pkg["realized_friction_inr"] = float(acc["total_friction"])
                    pkg["realized_net_inr"] = float(acc["net_pnl"])
                    pkg["reconciled_at_iso"] = now_ist_iso()
                    pkg["unresolved_fault"] = None
                    self.store.upsert_package(pkg)
                    pkg_reconciliations.append({"live_package_id": pid, "status": "CLOSED_RECONCILED_EXITS"})
                    continue

                # Check GTT / broker exit strictly with untagged/GTT filter + timestamp lower bound
                gtt_id = buy_leg.get("gtt_id")
                gtt_missing_now = bool(gtt_id and str(gtt_id) not in active_gtt_ids)
                if (
                    pkg["state"] in ("OPEN", "CLOSING", "MANUAL_INTERVENTION_REQUIRED")
                    and pkg["structure"] == "SINGLE_LONG"
                    and int(buy_leg.get("qty") or 0) > 0
                    and (actual_qty <= 0 or gtt_missing_now)
                ):
                    child_sell = self._find_unclaimed_gtt_child_sell(
                        live_package_id=pid,
                        ikey=ikey,
                        entry_ts_ms=int(pkg.get("entry_ts_ms") or 0),
                        broker_orders=broker_orders,
                        gtt_id=str(gtt_id) if gtt_id else None,
                    )
                    if child_sell is not None:
                        b_alr = int(pkg["legs"]["buy_leg"].get("exited_qty") or 0)
                        b_vw0 = float(pkg["legs"]["buy_leg"].get("partial_exit_vwap") or 0.0)
                        rem_q = max(1, int(buy_leg["qty"]) - b_alr)
                        cs_px = float(child_sell.get("average_price") or buy_leg["entry_price"])
                        comb_px = round_to_nse_tick(((b_alr * b_vw0) + (rem_q * cs_px)) / (b_alr + rem_q)) if b_alr > 0 else round_to_nse_tick(cs_px)
                        pkg["legs"]["buy_leg"]["exit_price"] = comb_px
                        pkg["legs"]["buy_leg"]["exited_qty"] = int(buy_leg["qty"])
                        pkg["legs"]["buy_leg"]["gtt_id"] = None
                        pkg["legs"]["buy_leg"]["gtt_child_order_id"] = child_sell.get("order_id")
                        acc = self._compute_realized_pkg_accounting(pkg)
                        pkg["state"] = "CLOSED"
                        pkg["exit_ts_ms"] = _now_ms()
                        pkg["exit_reason"] = "RECONCILED_BROKER_EXIT_OR_GTT"
                        pkg["realized_gross_inr"] = float(acc["gross_pnl"])
                        pkg["realized_friction_inr"] = float(acc["total_friction"])
                        pkg["realized_net_inr"] = float(acc["net_pnl"])
                        pkg["reconciled_at_iso"] = now_ist_iso()
                        pkg["unresolved_fault"] = None
                        self.store.upsert_package(pkg)
                        pkg_reconciliations.append({"live_package_id": pid, "status": "CLOSED_FROM_BROKER_EXIT"})
                        continue

                # Hard structural & GTT invariants: NEVER promote incomplete spread or unprotected long to OPEN
                rem_amb = [
                    it
                    for it in self.store.list_intents(live_package_id=pid, limit=250)
                    if it["state"] in ("UNKNOWN_AMBIGUOUS", "PENDING_SEND", "SENT")
                ]
                buy_leg = pkg["legs"].get("buy_leg") or {}
                short_leg = pkg["legs"].get("short_leg") or {}
                if not rem_amb and int(buy_leg.get("qty") or 0) > 0 and actual_qty > 0:
                    if pkg["structure"] == "DEFINED_RISK_SPREAD":
                        b_q = int(buy_leg.get("qty") or 0)
                        s_q = int(short_leg.get("qty") or 0)
                        b_px = float(buy_leg.get("entry_price") or 0.0)
                        if s_q <= 0:
                            rem_b = max(1, b_q - int(buy_leg.get("exited_qty") or 0))
                            unw = self._unwind_buy_wing(
                                pkg=pkg, qty_to_unwind=rem_b, unwind_price=b_px, reason="RECONCILE_INCOMPLETE_SPREAD"
                            )
                            self._finalize_failed_short_entry(pkg, b_px, rem_b, unw, "RECONCILED_INCOMPLETE_SPREAD_UNWOUND")
                            pkg_reconciliations.append(
                                {"live_package_id": pid, "status": pkg["state"], "broker_pos_qty": 0 if unw["ok"] else actual_qty}
                            )
                            continue
                        elif b_q > s_q:
                            unw_ex = self._unwind_buy_wing(
                                pkg=pkg,
                                qty_to_unwind=(b_q - s_q),
                                unwind_price=b_px,
                                reason="RECONCILE_EXCESS_BUY_UNWIND",
                                leg_role="BUY_WING_EXCESS_UNWIND",
                            )
                            if unw_ex["ok"]:
                                self._apply_excess_buy_unwind_to_pkg(pkg, (b_q - s_q), float(unw_ex["avg_price"]))
                                pkg["state"] = "OPEN"
                                pkg["unresolved_fault"] = None
                            else:
                                pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                                pkg["unresolved_fault"] = f"RECONCILE_EXCESS_BUY_UNWIND_FAILED: {unw_ex.get('error') or unw_ex.get('status')}"
                        elif s_q > b_q:
                            pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                            pkg["unresolved_fault"] = f"RECONCILE_SPREAD_SHORT_EXCEEDS_BUY:{s_q}>{b_q}"
                        elif pkg["state"] in ("CLOSING", "MANUAL_INTERVENTION_REQUIRED", "RECONCILE_MISMATCH"):
                            pkg["state"] = "OPEN"
                            pkg["unresolved_fault"] = None
                    elif pkg["structure"] == "SINGLE_LONG":
                        cur_gid = str(buy_leg.get("gtt_id") or "")
                        rem_b_qty = max(1, int(buy_leg["qty"]) - int(buy_leg.get("exited_qty") or 0))
                        b_px = float(buy_leg.get("entry_price") or 0.0)
                        if not cur_gid or cur_gid not in active_gtt_ids:
                            gtt_res = self._place_or_recover_protective_gtt(pkg, rem_b_qty, b_px)
                            if gtt_res["ok"]:
                                pkg["legs"]["buy_leg"]["gtt_id"] = gtt_res["gtt_id"]
                                pkg["legs"]["buy_leg"]["stop_trigger_price"] = gtt_res["stop_trigger_price"]
                                active_gtt_ids.add(str(gtt_res["gtt_id"]))
                                pkg["state"] = "OPEN"
                                pkg["unresolved_fault"] = None
                                self.store.append_audit(
                                    "GTT_REATTACHED_DURING_RECONCILE",
                                    actor,
                                    {
                                        "live_package_id": pid,
                                        "gtt_id": gtt_res["gtt_id"],
                                        "qty": rem_b_qty,
                                        "stop_trigger_price": gtt_res["stop_trigger_price"],
                                    },
                                )
                            elif gtt_res.get("ambiguous_gtt"):
                                pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                                pkg["unresolved_fault"] = str(gtt_res.get("error") or "GTT_PLACEMENT_TIMEOUT_AMBIGUOUS")
                            else:
                                unw = self._unwind_buy_wing(
                                    pkg=pkg, qty_to_unwind=rem_b_qty, unwind_price=b_px, reason="RECONCILE_MISSING_GTT"
                                )
                                self._finalize_failed_short_entry(pkg, b_px, rem_b_qty, unw, "RECONCILE_MISSING_GTT_UNWOUND")
                                pkg_reconciliations.append(
                                    {"live_package_id": pid, "status": pkg["state"], "broker_pos_qty": 0 if unw["ok"] else actual_qty}
                                )
                                continue
                        elif pkg["state"] in ("CLOSING", "MANUAL_INTERVENTION_REQUIRED", "RECONCILE_MISMATCH"):
                            pkg["state"] = "OPEN"
                            pkg["unresolved_fault"] = None
                    elif pkg["state"] in ("CLOSING", "MANUAL_INTERVENTION_REQUIRED", "RECONCILE_MISMATCH"):
                        pkg["state"] = "OPEN"
                        pkg["unresolved_fault"] = None

                pkg["reconciled_at_iso"] = now_ist_iso()
                self.store.upsert_package(pkg)
                pkg_reconciliations.append({"live_package_id": pid, "status": pkg["state"], "broker_pos_qty": actual_qty})

            faults = self.store.count_unresolved_faults()
            if self.store.db_exists():
                self.store.append_audit(
                    "RECONCILE_COMPLETED",
                    actor,
                    {
                        "resolved_intents": resolved_intents,
                        "package_reconciliations": pkg_reconciliations,
                        "active_gtts_count": len(active_gtts),
                        "blocking_faults_remaining": faults["total_blocking_faults"],
                    },
                )
            return {
                "ok": True,
                "resolved_intents": resolved_intents,
                "package_reconciliations": pkg_reconciliations,
                "active_gtts_count": len(active_gtts),
                "unresolved_faults": faults,
            }


def get_option_sniffer_v2_live_engine(
    *,
    db_path: Optional[str] = None,
    broker_client: Optional[V2BrokerClientProtocol] = None,
    initial_pin: Optional[str] = None,
) -> V2LiveExecutionEngine:
    global _ENGINE_INSTANCE
    with _ENGINE_LOCK:
        if _ENGINE_INSTANCE is None:
            _ENGINE_INSTANCE = V2LiveExecutionEngine(
                db_path=db_path,
                broker_client=broker_client,
                initial_pin=initial_pin,
                attach_paper_listener=True,
            )
        return _ENGINE_INSTANCE


def reset_option_sniffer_v2_live_engine() -> None:
    global _ENGINE_INSTANCE
    with _ENGINE_LOCK:
        if _ENGINE_INSTANCE is not None:
            _ENGINE_INSTANCE.detach_from_paper_service()
        _ENGINE_INSTANCE = None
