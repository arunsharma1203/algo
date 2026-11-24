"""
V2 Option Sniffer (`option_sniffer_v2`) — Isolated Parallel Collector, Replay Service & Read-Only Queries.

Hard Guarantees:
  - Strictly paper-only (`simulation_mode=True`, `live_capable_by_config=False`).
  - Zero imports of live order placement, broker modification/cancellation, mirroring, or arm/disarm paths.
  - Zero calls to `fetch_single_quote_emergency_rest`.
  - Passive fan-out listener via `OptionStreamManager.register_advance_feed_listener`; V2-only shutdown
    detaches only V2's listener and releases only `v2_owned_only` pins while V1 continues unaffected.
  - Read-only GET views never start the collector, subscribe instruments, or mutate SQLite tables.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.analytics.option_sniffer_v2.v2_book_a_engine import manage_open_book_a_on_bar
from app.analytics.option_sniffer_v2.v2_book_b_engine import (
    evaluate_book_b_entry_signal,
    manage_open_book_b_on_bar,
    reconcile_v1_book_b_stop_and_overshoot,
)
from app.analytics.option_sniffer_v2.v2_collateral_report import compute_optional_collateral_report
from app.analytics.option_sniffer_v2.v2_config import (
    EXISTING_V2_SYSTEMS_INVENTORY,
    load_v2_preregistration,
)
from app.analytics.option_sniffer_v2.v2_counterfactual_v1 import run_v1_counterfactual_audit
from app.analytics.option_sniffer_v2.v2_data_contract import (
    UPSTOX_ENTITLEMENT_FIELD_INVENTORY,
    check_spot_futures_sync,
    extract_observed_timestamp_ms,
    resolve_strict_quote_age_ms,
)
from app.analytics.option_sniffer_v2.v2_feed_consumer import (
    V2BoundedFeedConsumer,
    V2SubscriptionRegistry,
)
from app.analytics.option_sniffer_v2.v2_ledger_db import (
    V2LedgerStore,
    open_v1_readonly_db,
)
from app.analytics.option_sniffer_v2.v2_o3_reproducer import load_phase4_o3_audit_report
from app.analytics.option_sniffer_v2.v2_paper_simulator import (
    OPEN_EXPOSURE_STATES,
    V2PaperSimulator,
    make_stable_id,
)
from app.analytics.option_sniffer_v2.v2_replay_attribution import (
    _compute_series_stats,
    run_v2_replay_and_attribution,
)

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)

_PROTECTED_FILES: Tuple[str, ...] = (
    "backend/app/analytics/option_sniffer_advance_s205/SHADOW_PREREGISTRATION.json",
    "backend/app/analytics/option_sniffer_advance_s205/s205_meta_weights.json",
    "backend/models/swing/champion_ensemble.pkl",
    "backend/models/intraday/champion_ensemble.pkl",
    "backend/models/versions/swing/v1.0-champion/model.pkl",
    "backend/models/versions/intraday/v1.0-champion/model.pkl",
)


def _sha256_file(abs_path: str) -> Optional[str]:
    if not os.path.exists(abs_path):
        return None
    h = hashlib.sha256()
    with open(abs_path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_protected_hashes_readonly() -> Dict[str, Optional[str]]:
    return {rel: _sha256_file(os.path.join(_REPO_ROOT, rel)) for rel in _PROTECTED_FILES}


def _now_ist_dt() -> datetime:
    """Returns current Asia/Kolkata naive datetime without third-party tz dependencies."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    except Exception:
        # Fallback UTC + 05:30 fixed IST offset
        from datetime import timezone, timedelta

        return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).replace(tzinfo=None)


class OptionSnifferV2Service:
    """
    Isolated coordinator for V2 Option Sniffer (`option_sniffer_v2`).
    Never interferes with V1/V6B execution, rate limiters, WebSocket sockets, or `market_data.db`.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._op_lock = threading.RLock()
        self.config, self.config_hash = load_v2_preregistration()
        self.store = V2LedgerStore(db_path=db_path)
        self.simulator = V2PaperSimulator(
            config=self.config,
            config_hash=self.config_hash,
            store=self.store,
            run_mode="FORWARD_PAPER",
        )
        hyg = self.config["quote_hygiene"]
        self.feed_consumer = V2BoundedFeedConsumer(
            config_hash=self.config_hash,
            maxlen=int(hyg["bounded_ingress_queue_max"]),
        )
        self.subscription_registry = V2SubscriptionRegistry(
            ws_total_soft_cap=int(hyg["ws_total_soft_cap"]),
            v1_reserved_headroom=int(hyg["v1_reserved_ws_headroom"]),
            max_v2_additive_pins=int(hyg["max_v2_additive_pins"]),
        )
        self.protected_baseline_hashes = compute_protected_hashes_readonly()

        self.collector_state: str = "STANDBY_NOT_STARTED"
        self.collector_detail: str = "Collector not attached; GET views operate purely read-only."
        self._listener_attached: bool = False
        self._stream_mgr_ref: Any = None
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._archive_thread: Optional[threading.Thread] = None

        # Forward-paper intraday causal state
        self._fwd_session_date: Optional[str] = None
        self._fwd_spot_cum: float = 0.0
        self._fwd_spot_cnt: int = 0
        self._fwd_orb_hi: Optional[float] = None
        self._fwd_orb_lo: Optional[float] = None
        self._fwd_orb_complete: bool = False
        self._fwd_open_basis_ref: Optional[Dict[str, Any]] = None
        self._fwd_last_bar_ts: Optional[str] = None
        self._fwd_latest_spot_q: Optional[Dict[str, Any]] = None
        self._fwd_latest_fut_q: Optional[Dict[str, Any]] = None
        self._fwd_latest_vix_q: Optional[Dict[str, Any]] = None
        self._fwd_latest_opt_quotes: Dict[str, Dict[str, Any]] = {}
        self._fwd_opt_contracts: Dict[Tuple[str, float, str], Dict[str, Any]] = {}
        self._fwd_last_spot_closes: List[float] = []
        self._fwd_prev_spot_c: Optional[float] = None
        self._fwd_net_disp: float = 0.0
        self._fwd_path_sum: float = 0.0

        # Cached read-only archive report populated only by explicit CLI/control actions
        self._archive_audit_cache: Optional[Dict[str, Any]] = None

    # ──────────────────────────────────────────────────────────────────────────
    # LIFECYCLE: PASSIVE FAN-OUT START & V2-ONLY SHUTDOWN
    # ──────────────────────────────────────────────────────────────────────────

    def _get_stream_manager(self) -> Any:
        try:
            from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

            return OptionStreamManager.get_instance()
        except Exception as exc:
            logger.debug(f"[V2] OptionStreamManager import unavailable: {exc}")
            return None

    def _existing_v1_stream_keys(self) -> Set[str]:
        mgr = self._stream_mgr_ref or self._get_stream_manager()
        if mgr is None:
            return set()
        try:
            with mgr._lock:
                return set(getattr(mgr, "_key_to_contract", {}).keys()) - set(
                    self.subscription_registry.snapshot_status()["v2_owned_only_keys"]
                )
        except Exception:
            return set()

    def _subscribe_v2_only_additive(self, contracts: List[Dict[str, Any]]) -> None:
        mgr = self._stream_mgr_ref or self._get_stream_manager()
        if mgr is None or not contracts:
            return
        try:
            mgr.pin_extra_contracts(contracts)
        except Exception as exc:
            logger.warning(f"[V2] Additive v2-only pin failed (non-fatal): {exc}")

    def _unpin_v2_only_keys(self, keys: List[str]) -> None:
        mgr = self._stream_mgr_ref or self._get_stream_manager()
        if mgr is None or not keys:
            return
        try:
            if hasattr(mgr, "unpin_extra_contracts"):
                mgr.unpin_extra_contracts(keys)
            elif hasattr(mgr, "_lock"):
                key_set = set(keys)
                with mgr._lock:
                    for attr in ("_pinned_extra_keys", "_extra_keys"):
                        col = getattr(mgr, attr, None)
                        if isinstance(col, set):
                            col.difference_update(key_set)
                    ktc = getattr(mgr, "_key_to_contract", None)
                    if isinstance(ktc, dict):
                        for k in keys:
                            c = ktc.get(k)
                            if isinstance(c, dict) and c.get("v2_owned_only"):
                                ktc.pop(k, None)
        except Exception as exc:
            logger.warning(f"[V2] V2-only unpin cleanup warning: {exc}")

    def start_parallel_collector(
        self,
        stream_manager: Any = None,
        run_archive_warmup: bool = False,
        spawn_worker: bool = True,
    ) -> Dict[str, Any]:
        """
        Attaches V2's non-blocking passive listener to shared OptionStreamManager.
        Never starts a second WebSocket or REST poll loop, and never alters V1 pins.
        """
        with self._op_lock:
            mgr = stream_manager or self._get_stream_manager()
            self._stream_mgr_ref = mgr
            self._stop_event.clear()

            if mgr is not None and not self._listener_attached:
                try:
                    mgr.register_advance_feed_listener(self.on_live_feed_events)
                    self._listener_attached = True
                except Exception as exc:
                    logger.warning(f"[V2] Passive listener registration failed: {exc}")

            is_stream_live = bool(mgr is not None and getattr(mgr, "is_running", False))
            if self._listener_attached and is_stream_live:
                self.collector_state = "PARALLEL_LIVE_COLLECTION_ACTIVE"
                self.collector_detail = (
                    "Passive fan-out listener attached to shared active OptionStreamManager; "
                    "zero competing WebSocket or REST loops."
                )
            elif self._listener_attached:
                self.collector_state = "STANDBY_STREAM_INACTIVE"
                self.collector_detail = (
                    "Passive listener attached, waiting for shared OptionStreamManager stream ticks "
                    "(market closed or WebSocket standby)."
                )
            else:
                self.collector_state = "STANDBY_NO_STREAM_MANAGER"
                self.collector_detail = (
                    "OptionStreamManager instance unavailable in current process; archive replay remains available."
                )

            if spawn_worker and (self._worker_thread is None or not self._worker_thread.is_alive()):
                self._worker_thread = threading.Thread(
                    target=self._worker_loop,
                    name="OSV2-QueueWorker",
                    daemon=True,
                )
                self._worker_thread.start()

            if run_archive_warmup and (self._archive_thread is None or not self._archive_thread.is_alive()):
                self._archive_thread = threading.Thread(
                    target=self._safe_archive_warmup,
                    name="OSV2-ArchiveWarmup",
                    daemon=True,
                )
                self._archive_thread.start()

            return {
                "collector_state": self.collector_state,
                "collector_detail": self.collector_detail,
                "listener_attached": self._listener_attached,
                "simulation_mode": True,
                "live_capable_by_config": False,
            }

    def _safe_archive_warmup(self) -> None:
        try:
            self.run_and_persist_archive_audit()
        except Exception as exc:
            logger.warning(f"[V2] Archive warmup skipped: {exc}")

    def stop_v2_only(self) -> Dict[str, Any]:
        """
        Clean V2-only shutdown: stops V2 worker thread, detaches ONLY V2's passive listener,
        and releases ONLY V2-owned additive pins. V1/V6B remain completely running and unaffected.
        """
        with self._op_lock:
            self._stop_event.set()
            self.feed_consumer._wake_event.set()
            worker = self._worker_thread
            mgr = self._stream_mgr_ref or self._get_stream_manager()

            if mgr is not None and self._listener_attached:
                try:
                    if hasattr(mgr, "unregister_advance_feed_listener"):
                        mgr.unregister_advance_feed_listener(self.on_live_feed_events)
                    elif hasattr(mgr, "_lock") and hasattr(mgr, "_advance_feed_listeners"):
                        with mgr._lock:
                            listeners = getattr(mgr, "_advance_feed_listeners", [])
                            if self.on_live_feed_events in listeners:
                                listeners.remove(self.on_live_feed_events)
                    elif hasattr(mgr, "_advance_feed_listeners"):
                        listeners = getattr(mgr, "_advance_feed_listeners", [])
                        if self.on_live_feed_events in listeners:
                            listeners.remove(self.on_live_feed_events)
                except Exception as exc:
                    logger.warning(f"[V2] Passive listener detach warning: {exc}")
                self._listener_attached = False

            released_v2_keys = self.subscription_registry.release_all_v2_owned(
                unpin_v2_only_fn=self._unpin_v2_only_keys
            )
            self.collector_state = "STOPPED_V2_ONLY"
            self.collector_detail = (
                "V2 collector detached cleanly; V1 OptionStreamManager and V1/V6B remain running."
            )

        if worker is not None and worker.is_alive():
            worker.join(timeout=1.5)

        return {
            "collector_state": self.collector_state,
            "listener_attached": self._listener_attached,
            "released_v2_owned_keys": released_v2_keys,
        }

    def on_live_feed_events(self, events: List[Tuple[str, str, Dict[str, Any]]]) -> None:
        """Non-blocking producer hook invoked from shared OptionStreamManager._advance_feed_listeners."""
        self.feed_consumer.push_events(events)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            self.feed_consumer._wake_event.wait(timeout=0.5)
            if self._stop_event.is_set():
                break
            try:
                self.process_queued_events()
            except Exception as exc:
                logger.warning(f"[V2] Worker batch error: {exc}")

    def _reset_fwd_session_if_needed(self, session_date: str) -> None:
        if self._fwd_session_date != session_date:
            self._fwd_session_date = session_date
            self._fwd_spot_cum = 0.0
            self._fwd_spot_cnt = 0
            self._fwd_orb_hi = None
            self._fwd_orb_lo = None
            self._fwd_orb_complete = False
            self._fwd_open_basis_ref = None
            self._fwd_latest_spot_q = None
            self._fwd_latest_fut_q = None
            self._fwd_latest_vix_q = None
            self._fwd_latest_opt_quotes.clear()
            self._fwd_opt_contracts.clear()
            self._fwd_prev_spot_c = None
            self._fwd_net_disp = 0.0
            self._fwd_path_sum = 0.0

    def process_queued_events(
        self,
        now_dt: Optional[datetime] = None,
        persist_ingest: bool = False,
        max_items: int = 500,
    ) -> Dict[str, Any]:
        """Drains bounded feed queue and evaluates completed minute bars strictly causally."""
        batch = self.feed_consumer.drain_batch(max_items=max_items)
        if not batch:
            return {"processed": 0}

        cur_dt = now_dt or _now_ist_dt()
        decision_ts_ms = int(cur_dt.timestamp() * 1000)
        session_date = cur_dt.strftime("%Y-%m-%d")
        bar_ts = cur_dt.strftime("%Y-%m-%d %H:%M:00")
        hm = bar_ts[11:16]
        hhmm = int(hm[:2]) * 100 + int(hm[3:5])

        with self._op_lock:
            self._reset_fwd_session_if_needed(session_date)
            if persist_ingest:
                self.store.persist_ingest_events(batch, "FORWARD_PAPER", decision_ts_ms)

            for env in batch:
                kind = env["kind"]
                sym_or_key = env["symbol_or_key"]
                p = env["payload"]
                if kind == "INDEX":
                    if sym_or_key == "NIFTY" or "Nifty 50" in str(p.get("instrument_key") or ""):
                        self._fwd_latest_spot_q = dict(p)
                    elif "VIX" in str(sym_or_key).upper() or "Vix" in str(p.get("instrument_key") or ""):
                        self._fwd_latest_vix_q = dict(p)
                elif kind == "FUTURES" and ("NIFTY" in str(sym_or_key).upper()):
                    self._fwd_latest_fut_q = dict(p)
                elif kind == "OPTION":
                    ikey = str(p.get("instrument_key") or sym_or_key or "")
                    if ikey:
                        self._fwd_latest_opt_quotes[ikey] = dict(p)
                        exp = str(p.get("expiry") or "")
                        stk = p.get("strike")
                        ot = str(p.get("option_type") or "")
                        if exp and stk is not None and ot in ("CE", "PE"):
                            self._fwd_opt_contracts[(exp, float(stk), ot)] = {
                                "instrument_key": ikey,
                                "trading_symbol": p.get("trading_symbol") or ikey,
                                "expiry": exp,
                                "strike": float(stk),
                                "option_type": ot,
                                "lot_size": int(p.get("lot_size") or self.config["account_envelope"]["lot_size_default_nifty"]),
                            }

            if self._fwd_last_bar_ts is None:
                self._fwd_last_bar_ts = bar_ts
                return {"processed": len(batch), "bar_evaluated": False}

            if bar_ts <= self._fwd_last_bar_ts:
                return {"processed": len(batch), "bar_evaluated": False}

            # Minute rolled over -> evaluate strictly causal snapshot for completed minute
            self.feed_consumer.check_minute_bar_continuity(bar_ts)
            self._fwd_last_bar_ts = bar_ts
            return {
                "processed": len(batch),
                "bar_evaluated": True,
                **self._evaluate_forward_bar_locked(session_date, bar_ts, hhmm, decision_ts_ms),
            }

    def _evaluate_forward_bar_locked(
        self,
        session_date: str,
        bar_ts: str,
        hhmm: int,
        now_ms: int,
    ) -> Dict[str, Any]:
        if hhmm < 915 or hhmm > 1530:
            return {"status": "OUTSIDE_REGULAR_SESSION"}

        spot_c = float((self._fwd_latest_spot_q or {}).get("spot_ltp") or 0.0)
        if spot_c > 0:
            self._fwd_spot_cum += spot_c
            self._fwd_spot_cnt += 1
            if self._fwd_prev_spot_c is not None:
                delta = spot_c - self._fwd_prev_spot_c
                self._fwd_net_disp += delta
                self._fwd_path_sum += abs(delta)
            self._fwd_prev_spot_c = spot_c
            if hhmm < 945:
                self._fwd_orb_hi = spot_c if self._fwd_orb_hi is None else max(self._fwd_orb_hi, spot_c)
                self._fwd_orb_lo = spot_c if self._fwd_orb_lo is None else min(self._fwd_orb_lo, spot_c)
            elif self._fwd_orb_hi is not None and self._fwd_orb_lo is not None:
                self._fwd_orb_complete = True

        spot_vwap = (self._fwd_spot_cum / self._fwd_spot_cnt) if self._fwd_spot_cnt > 0 else spot_c
        er20 = abs(self._fwd_net_disp) / max(1e-6, self._fwd_path_sum) if self._fwd_path_sum > 0 else 0.0

        if self._fwd_open_basis_ref is None and 915 <= hhmm <= 930 and self._fwd_latest_spot_q and self._fwd_latest_fut_q:
            chk = check_spot_futures_sync(
                self._fwd_latest_spot_q,
                self._fwd_latest_fut_q,
                max_skew_ms=int(self.config["quote_hygiene"]["max_spot_fut_sync_skew_ms"]),
                max_age_ms=int(self.config["quote_hygiene"]["max_entry_quote_age_ms"]),
                now_ms=now_ms,
            )
            if chk.get("ok"):
                self._fwd_open_basis_ref = chk

        # Reconcile evicted keys before MTM
        for k in self.subscription_registry.snapshot_status()["evicted_v2_keys"]:
            self.simulator.mark_key_evicted(k)

        mtm_info = self.simulator.mark_open_packages(
            quote_by_ikey=self._fwd_latest_opt_quotes,
            session_date=session_date,
            bar_ts=bar_ts,
            now_ms=now_ms,
            is_opening_bar=(hhmm == 915),
            is_eod_bar=(hhmm >= 1515),
        )
        kill_lim = float(self.config["account_envelope"]["max_daily_mtm_kill_inr"])
        slots = self.simulator.active_slots_and_reserves(bar_ts, session_date)
        if (slots["realized_today_inr"] + mtm_info["conservative_unrealized_mtm_inr"]) <= kill_lim:
            self.simulator.daily_kill_triggered_by_date[session_date] = f"DAILY_KILL_MTM_LE_{kill_lim}"

        for pid, pkg in list(self.simulator.open_packages.items()):
            res_close = None
            if pkg["book_id"] == "V2_BOOK_B_PAID_FLOW":
                res_close = manage_open_book_b_on_bar(
                    sim=self.simulator,
                    pkg=pkg,
                    session_date=session_date,
                    bar_ts=bar_ts,
                    ist_hhmm=hhmm,
                    decision_ts_ms=now_ms,
                    spot_close=spot_c,
                    spot_vwap=spot_vwap,
                    quote_by_ikey=self._fwd_latest_opt_quotes,
                )
            else:
                res_close = manage_open_book_a_on_bar(
                    sim=self.simulator,
                    pkg=pkg,
                    session_date=session_date,
                    bar_ts=bar_ts,
                    ist_hhmm=hhmm,
                    decision_ts_ms=now_ms,
                    spot_close=spot_c,
                    quote_by_ikey=self._fwd_latest_opt_quotes,
                )
            if isinstance(res_close, dict) and res_close.get("state") in ("CLOSED", "INCOMPLETE_LEG_UNWOUND"):
                self.subscription_registry.release_for_package(pid, unpin_v2_only_fn=self._unpin_v2_only_keys)

        health = self.feed_consumer.get_health_metrics()
        env = self.config["account_envelope"]
        cfg_b = self.config["books"]["V2_BOOK_B_PAID_FLOW"]
        slots = self.simulator.active_slots_and_reserves(bar_ts, session_date)
        entered = False

        if (
            not health["uncertainty_blocks"]
            and session_date not in self.simulator.daily_kill_triggered_by_date
            and mtm_info["unpriced_count"] == 0
            and slots["open_total"] < int(env["max_open_packages_total"])
            and slots["trades_today_total"] < int(env["max_total_trades_per_day"])
            and slots["open_by_book"].get(cfg_b["book_id"], 0) < int(cfg_b["max_open"])
            and slots["trades_today_by_book"].get(cfg_b["book_id"], 0) < int(cfg_b["max_trades_day"])
            and now_ms >= self.simulator.cooldown_until_ms.get(cfg_b["book_id"], 0)
        ):
            quotes_lk = {
                k: self._fwd_latest_opt_quotes[c["instrument_key"]]
                for k, c in self._fwd_opt_contracts.items()
                if c["instrument_key"] in self._fwd_latest_opt_quotes
            }
            expiries = sorted({k[0] for k in self._fwd_opt_contracts.keys()})
            sig_b = evaluate_book_b_entry_signal(
                cfg=self.config,
                session_date=session_date,
                bar_ts=bar_ts,
                ist_hhmm=hhmm,
                decision_ts_ms=now_ms,
                spot_close=spot_c,
                spot_vwap=spot_vwap,
                orb30_h=self._fwd_orb_hi if self._fwd_orb_complete else None,
                orb30_l=self._fwd_orb_lo if self._fwd_orb_complete else None,
                adx14=25.0 if er20 >= 0.32 else 0.0,
                er20=er20,
                spot_quote=self._fwd_latest_spot_q,
                fut_quote=self._fwd_latest_fut_q,
                open_basis_ref=self._fwd_open_basis_ref,
                expiries=expiries,
                contracts_lookup=self._fwd_opt_contracts,
                quote_lookup=quotes_lk,
                variant_code="V2_FROZEN_COMBINED",
                use_basis_delta=True,
            )
            if sig_b["passed"] and sig_b.get("contract") and sig_b.get("quote"):
                sid = make_stable_id("osv2_sig", "FORWARD_PAPER", "V2_FROZEN_COMBINED", bar_ts, sig_b["contract"]["instrument_key"])
                self.store.record_signal_idempotent(
                    {
                        "signal_id": sid,
                        "run_mode": "FORWARD_PAPER",
                        "config_hash": self.config_hash,
                        "session_date": session_date,
                        "bar_ts": bar_ts,
                        "decision_ts_ms": now_ms,
                        "book_id": cfg_b["book_id"],
                        "candidate_variant": "V2_FROZEN_COMBINED",
                        "direction": sig_b["option_type"],
                        "status": "TRIGGERED",
                        "reject_reason": None,
                        "gates": sig_b["gates"],
                        "provenance": sig_b["basis_meta"],
                    }
                )
                op = self.simulator.open_book_b_paid_long(
                    signal_id=sid,
                    candidate_variant="V2_FROZEN_COMBINED",
                    session_date=session_date,
                    bar_ts=bar_ts,
                    decision_ts_ms=now_ms,
                    contract=sig_b["contract"],
                    quote=sig_b["quote"],
                    spot_price=spot_c,
                    basis_meta=sig_b["basis_meta"],
                    used_single_otm_fallback=sig_b["used_single_otm_fallback"],
                )
                if op.get("ok"):
                    entered = True
                    self.subscription_registry.acquire_for_package(
                        package_id=op["package_id"],
                        contracts=[sig_b["contract"]],
                        existing_stream_keys_fn=self._existing_v1_stream_keys,
                        subscribe_additive_fn=self._subscribe_v2_only_additive,
                    )

        return {"status": "EVALUATED", "entered_book_b": entered, "mtm": mtm_info}

    # ──────────────────────────────────────────────────────────────────────────
    # EXPLICIT ARCHIVE REPLAY & COUNTERFACTUAL EXECUTION (NOT CALLED ON GET)
    # ──────────────────────────────────────────────────────────────────────────

    def run_and_persist_archive_audit(self, v1_db_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Explicit command / control action to run read-only archive replay & V1 counterfactuals
        and persist the reproducible V2 ledger under `run_mode='REPLAY'`.
        """
        v1_conn = open_v1_readonly_db(v1_db_path)
        try:
            cf = run_v1_counterfactual_audit(v1_conn, store=self.store)
            rep = run_v2_replay_and_attribution(v1_conn, self.store, self.config, self.config_hash)
            o3 = load_phase4_o3_audit_report()
            overshoot = reconcile_v1_book_b_stop_and_overshoot(v1_conn)
        finally:
            v1_conn.close()

        bundle = {
            "replay_status": "ARCHIVE_REPLAY_PERSISTED",
            "phase4_o3_audit": o3,
            "v1_book_b_overshoot_audit": overshoot,
            "counterfactuals": cf,
            "replay_and_attribution": rep,
        }
        with self._op_lock:
            self._archive_audit_cache = bundle
        return bundle

    # ──────────────────────────────────────────────────────────────────────────
    # STRICTLY READ-ONLY QUERY METHODS (ZERO MUTATIONS / ZERO SIDE EFFECTS ON GET)
    # ──────────────────────────────────────────────────────────────────────────

    def _read_v1_summary_readonly(self) -> Dict[str, Any]:
        v1_conn = open_v1_readonly_db()
        try:
            rows = [
                dict(r)
                for r in v1_conn.execute(
                    """
                    SELECT package_id, session_date, book_id, net_pnl_inr, total_charges_inr,
                           gross_pnl_inr, cash_reserved_inr, exit_reason, entry_bar_ts, exit_bar_ts
                    FROM option_sniffer_advance_s205_shadow_packages
                    ORDER BY entry_bar_ts ASC;
                    """
                ).fetchall()
            ]
        finally:
            v1_conn.close()

        dates = sorted({r["session_date"] for r in rows})
        v1_daily: List[Dict[str, Any]] = []
        eq = float(self.config["account_envelope"]["starting_capital_inr"])
        for dt in dates:
            day_rows = [r for r in rows if r["session_date"] == dt]
            net_d = round(sum(float(r["net_pnl_inr"]) for r in day_rows), 2)
            cap_d = round(sum(float(r["cash_reserved_inr"]) for r in day_rows), 2)
            eq = round(eq + net_d, 2)
            stale_cnt = sum(
                1 for r in day_rows if r["book_id"] == "SHADOW_BOOK_A_SPREAD_150" and r["exit_reason"] == "SPREAD_DECAY_TARGET_70PCT"
            )
            v1_daily.append(
                {
                    "session_date": dt,
                    "v1_reported_net_pnl_inr": net_d,
                    "v1_reported_trades": len(day_rows),
                    "v1_stale_wing_contaminated_count": stale_cnt,
                    "v1_capital_reserved_inr": cap_d,
                    "eod_mtm_equity_inr": eq,
                    "intraday_min_mtm_equity_inr": eq,
                    "peak_reserved_capital_inr": cap_d,
                }
            )
        stats = _compute_series_stats(
            [
                {
                    "gross_pnl_inr": float(r["gross_pnl_inr"]),
                    "total_friction_inr": float(r["total_charges_inr"]),
                    "net_pnl_inr": float(r["net_pnl_inr"]),
                }
                for r in rows
            ],
            v1_daily,
            float(self.config["account_envelope"]["starting_capital_inr"]),
        )
        return {"rows": rows, "daily": v1_daily, "summary": stats}

    def evaluate_promotion_gates(
        self,
        fwd_daily: List[Dict[str, Any]],
        fwd_closed: List[Dict[str, Any]],
        fwd_open: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        gates_cfg = self.config["promotion_gates"]
        start_cap = float(self.config["account_envelope"]["starting_capital_inr"])
        fwd_stats = _compute_series_stats(fwd_closed, fwd_daily, start_cap)

        sessions_observed = len(fwd_daily)
        closed_count = len(fwd_closed)
        book_a_overnight = sum(
            1
            for p in fwd_closed
            if p["book_id"] == "V2_BOOK_A_PROPOSED_O3" and ((p.get("exit_bar_ts") or "")[:10] > p["session_date"])
        )
        unresolved_open = [
            p
            for p in fwd_open
            if p.get("is_unpriced_exposure") or p["state"] in ("HEDGE_ONLY_EXPOSURE_OPEN", "UNRESOLVED_EXPOSURE_PAST_1445")
        ]
        unresolved_structural_loss = round(
            sum(abs(float(p.get("structural_max_loss_inr") or 0.0)) for p in unresolved_open),
            2,
        )

        # Conservative forward 2x charges net check
        tot_fric = float(fwd_stats["total_charges_inr"])
        net_2x = round(float(fwd_stats["gross_pnl_inr"]) - (2.0 * tot_fric), 2)

        checks = [
            {
                "gate": "Clean Forward Sessions >= 60",
                "required": int(gates_cfg["min_clean_forward_sessions"]),
                "observed": sessions_observed,
                "passed": sessions_observed >= int(gates_cfg["min_clean_forward_sessions"]),
            },
            {
                "gate": "Forward Closed Trades >= 30",
                "required": int(gates_cfg["min_forward_trades_total"]),
                "observed": closed_count,
                "passed": closed_count >= int(gates_cfg["min_forward_trades_total"]),
            },
            {
                "gate": "Book A Overnight Episodes >= 15 (if Book A verified & enabled)",
                "required": int(gates_cfg["min_book_a_overnight_episodes"]),
                "observed": book_a_overnight,
                "passed": book_a_overnight >= int(gates_cfg["min_book_a_overnight_episodes"]),
            },
            {
                "gate": "2x Charges Forward Net PnL > Rs 0",
                "required": "> 0",
                "observed": net_2x,
                "passed": closed_count > 0 and net_2x > 0.0,
            },
            {
                "gate": "Forward Conservative MTM DD <= 8.0% Starting Capital",
                "required": f"<= {gates_cfg['max_mtm_drawdown_pct_of_starting_capital']}%",
                "observed": f"{fwd_stats['intraday_mtm_dd_pct']}%",
                "passed": fwd_stats["intraday_mtm_dd_pct"] <= float(gates_cfg["max_mtm_drawdown_pct_of_starting_capital"]),
            },
            {
                "gate": "Zero Unresolved / Unpriced Open Exposure",
                "required": 0,
                "observed": len(unresolved_open),
                "passed": len(unresolved_open) == 0,
                "worst_case_risk_charged_inr": unresolved_structural_loss,
            },
        ]
        all_passed = all(c["passed"] for c in checks)
        return {
            "status": "PASSED_HUMAN_REVIEW_ELIGIBLE" if all_passed else "FAIL_COLLECTING_FORWARD_EVIDENCE",
            "side_effect": gates_cfg["gate_pass_execution_side_effect"],
            "sessions_progress": f"{sessions_observed}/{gates_cfg['min_clean_forward_sessions']}",
            "unresolved_exposure_count": len(unresolved_open),
            "unresolved_structural_risk_charged_inr": unresolved_structural_loss,
            "forward_stats": fwd_stats,
            "checks": checks,
        }

    def _compute_feed_hygiene_snapshot(self) -> Dict[str, Any]:
        now_ms = int(time.time() * 1000)
        spot_age = resolve_strict_quote_age_ms(self._fwd_latest_spot_q, now_ms=now_ms, require_observed_l5=False)
        fut_age = resolve_strict_quote_age_ms(self._fwd_latest_fut_q, now_ms=now_ms, require_observed_l5=False)
        sync_res = (
            check_spot_futures_sync(self._fwd_latest_spot_q, self._fwd_latest_fut_q, now_ms=now_ms)
            if (self._fwd_latest_spot_q and self._fwd_latest_fut_q)
            else {"ok": False, "reason": "NO_LIVE_SPOT_OR_FUT_OBSERVED"}
        )
        return {
            "feed_consumer": self.feed_consumer.get_health_metrics(),
            "subscription_registry": self.subscription_registry.snapshot_status(),
            "spot_age_ms": spot_age,
            "futures_age_ms": fut_age,
            "spot_fut_sync": sync_res,
            "open_basis_reference": self._fwd_open_basis_ref,
        }

    def get_overview(
        self,
        include_collateral: bool = False,
        collateral_yield_pa: float = 6.0,
        haircut_pct: float = 10.0,
    ) -> Dict[str, Any]:
        """Purely read-only overview endpoint (`0` DB writes, `0` collector starts, `0` subscriptions)."""
        fwd_pkgs = self.store.load_packages("FORWARD_PAPER", self.config_hash)
        fwd_open = [p for p in fwd_pkgs if p["state"] in OPEN_EXPOSURE_STATES]
        fwd_closed = [p for p in fwd_pkgs if p["state"] not in OPEN_EXPOSURE_STATES]
        fwd_daily = self.store.load_daily_snapshots("FORWARD_PAPER", self.config_hash)

        rep_pkgs = self.store.load_packages("REPLAY", self.config_hash)
        rep_closed = [p for p in rep_pkgs if p["state"] not in OPEN_EXPOSURE_STATES]
        rep_daily = self.store.load_daily_snapshots("REPLAY", self.config_hash)

        start_cap = float(self.config["account_envelope"]["starting_capital_inr"])
        rep_stats = _compute_series_stats(rep_closed, rep_daily, start_cap)
        promo = self.evaluate_promotion_gates(fwd_daily, fwd_closed, fwd_open)

        col_cfg = dict(self.config["optional_collateral_scenario_defaults"])
        col_cfg["assumed_gross_yield_pa_pct"] = float(collateral_yield_pa)
        col_cfg["broker_haircut_pct"] = float(haircut_pct)

        col_forward = compute_optional_collateral_report(
            starting_capital_inr=start_cap,
            trading_net_pnl_inr=promo["forward_stats"]["net_pnl_inr"],
            peak_reserved_margin_inr=promo["forward_stats"]["peak_capital_used_inr"],
            calendar_days_observed=max(1, len(fwd_daily)),
            collateral_cfg=col_cfg,
            enabled=include_collateral,
        )
        col_replay = compute_optional_collateral_report(
            starting_capital_inr=start_cap,
            trading_net_pnl_inr=rep_stats["net_pnl_inr"],
            peak_reserved_margin_inr=rep_stats["peak_capital_used_inr"],
            calendar_days_observed=max(1, len(rep_daily)),
            collateral_cfg=col_cfg,
            enabled=include_collateral,
        )

        return {
            "service_name": "V2 Option Sniffer",
            "package": "option_sniffer_v2",
            "protocol_id": self.config["protocol_id"],
            "config_hash": self.config_hash,
            "simulation_mode": True,
            "live_capable_by_config": False,
            "execution_adapter": self.config["execution_adapter"],
            "result_boundary_notice": (
                "FORWARD_PAPER and ARCHIVE_REPLAY metrics are strictly separated. "
                "V1 counterfactual experiments are excluded from all V2 totals."
            ),
            "collection_status": {
                "collector_state": self.collector_state,
                "collector_detail": self.collector_detail,
                "listener_attached": self._listener_attached,
                "db_path": self.store.db_path,
                "archive_replay_persisted": len(rep_daily) > 0,
            },
            "books": {
                "V2_BOOK_A_PROPOSED_O3": {
                    **self.config["books"]["V2_BOOK_A_PROPOSED_O3"],
                    "status_banner": "DISABLED FOR AUTONOMOUS FORWARD ENTRIES: UNVERIFIED_SPEC_CONFLICT",
                },
                "V2_BOOK_B_PAID_FLOW": {
                    **self.config["books"]["V2_BOOK_B_PAID_FLOW"],
                    "status_banner": "ACTIVE PAPER HYPOTHESIS (D-BASIS + 10:15-13:00 + 20%/Rs1800 + 1-OTM)",
                },
            },
            "forward_paper_summary": {
                **promo["forward_stats"],
                "open_count": len(fwd_open),
                "unresolved_exposure_count": promo["unresolved_exposure_count"],
                "unresolved_structural_risk_charged_inr": promo["unresolved_structural_risk_charged_inr"],
                "realized_equity_inr": self.simulator.realized_equity_inr,
                "reserved_capital_inr": self.simulator.reserved_capital_inr,
                "canonical_ledger_sha256": self.store.compute_canonical_ledger_digest("FORWARD_PAPER", self.config_hash),
            },
            "archive_replay_summary": {
                **rep_stats,
                "persisted_sessions": len(rep_daily),
                "canonical_ledger_sha256": (
                    self.store.compute_canonical_ledger_digest("REPLAY", self.config_hash) if rep_daily else None
                ),
            },
            "promotion_gates": promo,
            "feed_hygiene": self._compute_feed_hygiene_snapshot(),
            "optional_collateral_reporting": {
                "forward_paper": col_forward,
                "archive_replay": col_replay,
            },
        }

    def get_positions(self) -> Dict[str, Any]:
        """Purely read-only positions & fills endpoint."""
        fwd_pkgs = self.store.load_packages("FORWARD_PAPER", self.config_hash)
        rep_pkgs = self.store.load_packages("REPLAY", self.config_hash)
        fwd_fills = self.store.load_fills("FORWARD_PAPER", self.config_hash)
        rep_fills = self.store.load_fills("REPLAY", self.config_hash)

        now_ms = int(time.time() * 1000)
        fwd_open: List[Dict[str, Any]] = []
        fwd_closed: List[Dict[str, Any]] = []
        for p in fwd_pkgs:
            if p["state"] in OPEN_EXPOSURE_STATES:
                ls = p["legs_state"]
                since_ms = p.get("unpriced_since_ts_ms")
                age_unpriced = max(0, now_ms - int(since_ms)) if since_ms else None
                fwd_open.append(
                    {
                        **p,
                        "unpriced_age_ms": age_unpriced,
                        "conservative_worst_loss_inr": (
                            -abs(float(p.get("structural_max_loss_inr") or 0.0))
                            if p.get("is_unpriced_exposure")
                            else p.get("last_mark_net_pnl_inr")
                        ),
                        "legs_count": 2 if ls.get("short_leg") else 1,
                    }
                )
            else:
                fwd_closed.append(p)

        return {
            "simulation_mode": True,
            "forward_open_positions": fwd_open,
            "forward_closed_trades": fwd_closed,
            "forward_fills": fwd_fills,
            "replay_closed_trades": [p for p in rep_pkgs if p["state"] not in OPEN_EXPOSURE_STATES],
            "replay_fills": rep_fills,
        }

    def get_daily_comparison(self) -> Dict[str, Any]:
        """Purely read-only V1 vs V2 daily table (`FORWARD_PAPER`, `REPLAY`, and `V1_REPORTED` separate)."""
        v1 = self._read_v1_summary_readonly()
        rep_daily = self.store.load_daily_snapshots("REPLAY", self.config_hash)
        fwd_daily = self.store.load_daily_snapshots("FORWARD_PAPER", self.config_hash)
        rep_pkgs = self.store.load_packages("REPLAY", self.config_hash)
        fwd_pkgs = self.store.load_packages("FORWARD_PAPER", self.config_hash)

        with self._op_lock:
            cache = self._archive_audit_cache

        if cache and cache.get("replay_and_attribution"):
            daily_comp = cache["replay_and_attribution"]["daily_comparison"]
        else:
            v1_by_dt = {d["session_date"]: d for d in v1["daily"]}
            rep_by_dt = {d["session_date"]: d for d in rep_daily}
            all_dates = sorted(set(v1_by_dt.keys()) | set(rep_by_dt.keys()))
            daily_comp = []
            for dt in all_dates:
                vr = v1_by_dt.get(dt, {})
                rr = rep_by_dt.get(dt, {})
                daily_comp.append(
                    {
                        "session_date": dt,
                        "v1_reported_net_pnl_inr": vr.get("v1_reported_net_pnl_inr", 0.0),
                        "v1_reported_trades": vr.get("v1_reported_trades", 0),
                        "v1_stale_wing_contaminated_count": vr.get("v1_stale_wing_contaminated_count", 0),
                        "v1_capital_reserved_inr": vr.get("v1_capital_reserved_inr", 0.0),
                        "v2_replay_net_pnl_inr": rr.get("net_pnl_inr"),
                        "v2_replay_trades": rr.get("closed_trades"),
                        "v2_replay_peak_capital_inr": rr.get("peak_reserved_capital_inr"),
                        "v2_replay_intraday_min_eq_inr": rr.get("intraday_min_mtm_equity_inr"),
                        "result_origin": "ARCHIVE_REPLAY" if rr else "REPLAY_NOT_YET_RUN",
                    }
                )

        return {
            "v1_reported_summary": v1["summary"],
            "v2_replay_summary": _compute_series_stats(
                [p for p in rep_pkgs if p["state"] not in OPEN_EXPOSURE_STATES],
                rep_daily,
                float(self.config["account_envelope"]["starting_capital_inr"]),
            ),
            "v2_forward_paper_summary": _compute_series_stats(
                [p for p in fwd_pkgs if p["state"] not in OPEN_EXPOSURE_STATES],
                fwd_daily,
                float(self.config["account_envelope"]["starting_capital_inr"]),
            ),
            "archive_daily_comparison": daily_comp,
            "forward_daily_records": fwd_daily,
        }

    def get_counterfactuals(self) -> Dict[str, Any]:
        """Purely read-only counterfactual rows (`included_in_v2_totals = 0`)."""
        rows = self.store.load_counterfactuals()
        cf1 = [r for r in rows if r["experiment_code"] == "CF1_TRUE_FRESH_70PCT_CONTINUATION"]
        cf2 = [r for r in rows if r["experiment_code"] == "CF2_RATCHET_BYPASS_EXPERIMENT"]
        return {
            "included_in_v2_totals": False,
            "disclaimer": "COUNTERFACTUAL_V1_STUDY_ONLY: Strictly excluded from all V2 totals and promotion gates.",
            "persisted_count": len(rows),
            "cf1_summary": {
                "total_v1_packages": len(cf1),
                "clean_or_resolved_count": sum(1 for r in cf1 if r["cf_status"] in ("PRESERVED_V1_CLEAN_EXIT", "FRESH_RESOLVED")),
                "unresolved_stale_wing_count": sum(1 for r in cf1 if r["cf_status"] == "UNRESOLVED_AT_1515_STALE_WING"),
                "mark_at_last_fresh_sum_inr": round(sum(float(r["cf_net_pnl_inr"] or 0.0) for r in cf1), 2),
            },
            "cf2_summary": {
                "total_v1_packages": len(cf2),
                "unresolved_stale_wing_count": sum(1 for r in cf2 if r["cf_status"] == "UNRESOLVED_AT_1515_STALE_WING"),
                "mark_at_last_fresh_sum_inr": round(sum(float(r["cf_net_pnl_inr"] or 0.0) for r in cf2), 2),
            },
            "rows": rows,
        }

    def get_replay_audit(self) -> Dict[str, Any]:
        """Purely read-only Phase-4 O3, Book B overshoot, and single-factor attribution view."""
        v1_conn = open_v1_readonly_db()
        try:
            overshoot = reconcile_v1_book_b_stop_and_overshoot(v1_conn)
        finally:
            v1_conn.close()
        o3 = load_phase4_o3_audit_report()
        with self._op_lock:
            cache = self._archive_audit_cache
        rep_attr = (cache or {}).get("replay_and_attribution")
        return {
            "phase4_o3_audit": o3,
            "v1_book_b_overshoot_audit": overshoot,
            "ablation_study": (rep_attr or {}).get("ablation_study", []),
            "sensitivity": (rep_attr or {}).get("sensitivity", {}),
            "canonical_replay_sha256": (
                (rep_attr or {}).get("canonical_replay", {}).get("ledger_sha256")
                or self.store.compute_canonical_ledger_digest("REPLAY", self.config_hash)
            ),
            "archive_replay_populated": bool(rep_attr),
        }

    def get_diagnostics(self) -> Dict[str, Any]:
        """Purely read-only isolation, entitlement field inventory, hashes, and activation guide."""
        current_hashes = compute_protected_hashes_readonly()
        hash_matches = all(
            current_hashes.get(k) == v for k, v in self.protected_baseline_hashes.items()
        )
        return {
            "service_name": "V2 Option Sniffer",
            "protocol_id": self.config["protocol_id"],
            "config_hash": self.config_hash,
            "simulation_mode": True,
            "live_capable_by_config": False,
            "execution_adapter": self.config["execution_adapter"],
            "existing_v2_systems_inventory": EXISTING_V2_SYSTEMS_INVENTORY,
            "upstox_entitlement_field_inventory": list(UPSTOX_ENTITLEMENT_FIELD_INVENTORY),
            "protected_artifacts_integrity": {
                "all_unchanged": hash_matches,
                "hashes": current_hashes,
            },
            "collector_status": {
                "collector_state": self.collector_state,
                "collector_detail": self.collector_detail,
                "listener_attached": self._listener_attached,
                "db_path": self.store.db_path,
            },
            "feed_hygiene": self._compute_feed_hygiene_snapshot(),
            "recent_signals_forward": self.store.load_signals("FORWARD_PAPER", self.config_hash, limit=50),
            "recent_signals_replay": self.store.load_signals("REPLAY", self.config_hash, limit=50),
            "operator_activation_commands": {
                "restart_shared_backend_for_live_fanout": (
                    "Stop running uvicorn :8000 cleanly during non-market window, then restart: "
                    "cd backend && venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
                ),
                "detach_v2_only_without_stopping_v1": (
                    "POST /api/option-sniffer-advance-v2/control/shutdown-v2-only"
                ),
                "reproducible_cli_audit": (
                    "PYTHONPATH=backend /usr/bin/python3 -B -m "
                    "app.analytics.option_sniffer_v2.v2_reproducible_audit_cli"
                ),
            },
        }


_SERVICE_LOCK = threading.Lock()
_SERVICE_INSTANCE: Optional[OptionSnifferV2Service] = None


def get_option_sniffer_v2_service(db_path: Optional[str] = None) -> OptionSnifferV2Service:
    global _SERVICE_INSTANCE
    with _SERVICE_LOCK:
        if _SERVICE_INSTANCE is None:
            _SERVICE_INSTANCE = OptionSnifferV2Service(db_path=db_path)
        return _SERVICE_INSTANCE


def reset_option_sniffer_v2_service() -> None:
    """Test-only helper cleanly shutting down & clearing the V2 singleton."""
    global _SERVICE_INSTANCE
    with _SERVICE_LOCK:
        inst = _SERVICE_INSTANCE
        _SERVICE_INSTANCE = None
    if inst is not None:
        inst.stop_v2_only()
        inst.store.close()
