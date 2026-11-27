"""
V2 Option Sniffer (`option_sniffer_v2`) — Isolated Parallel Collector, Unified Engine Service
& Strictly Read-Only Operator Queries.

Hard Guarantees:
  - Strictly paper-only (`simulation_mode=True`, `live_capable_by_config=False`).
  - Zero imports of live order placement, broker modification/cancellation, mirroring, or arm/disarm paths.
  - Zero calls to `fetch_single_quote_emergency_rest`.
  - Cold-start `GET` paths operate with `auto_create=False` (`0` DB file creation, `0` schema writes,
    `0` collector mutations).
  - Forward collection and causal replay execute through the exact same `V2UnifiedEventEngine`.
  - Atomic single-lock owner-aware pin acquisition/release on shared `OptionStreamManager`.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import logging
import os
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.analytics.option_sniffer_v2.v2_book_b_engine import reconcile_v1_book_b_stop_and_overshoot
from app.analytics.option_sniffer_v2.v2_collateral_report import compute_optional_collateral_report
from app.analytics.option_sniffer_v2.v2_config import (
    EXISTING_V2_SYSTEMS_INVENTORY,
    bar_open_label_from_ms,
    format_ist_iso,
    is_nse_trading_session,
    load_v2_preregistration,
    parse_ist_timestamp_ms,
    to_ist_aware,
    trading_sessions_between,
)
from app.data.database import is_testing_environment
from app.analytics.option_sniffer_v2.v2_counterfactual_v1 import (
    run_v1_counterfactual_audit,
    summarize_counterfactual_rows,
)
from app.analytics.option_sniffer_v2.v2_data_contract import (
    UPSTOX_ENTITLEMENT_FIELD_INVENTORY,
    check_spot_futures_sync,
    resolve_strict_quote_age_ms,
)
from app.analytics.option_sniffer_v2.v2_feed_consumer import (
    V2BoundedFeedConsumer,
    V2SubscriptionRegistry,
)
from app.analytics.option_sniffer_v2.v2_dedicated_feed import (
    compute_v2_priority_keys,
    evaluate_feed_architecture_options,
)
from app.analytics.option_sniffer_v2.v2_ledger_db import (
    V2LedgerStore,
    open_v1_readonly_db,
)
from app.analytics.option_sniffer_v2.v2_o3_reproducer import load_phase4_o3_audit_report
from app.analytics.option_sniffer_v2.v2_paper_simulator import (
    OPEN_EXPOSURE_STATES,
    V2PaperSimulator,
    audit_historical_drawdown_separation,
)
from app.analytics.option_sniffer_v2.v2_replay_attribution import (
    _compute_series_stats,
    load_superseded_v2_0_evidence,
    run_v2_replay_and_attribution,
)
from app.analytics.option_sniffer_v2.v2_unified_engine import V2UnifiedEventEngine

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


class OptionSnifferV2Service:
    """
    Isolated coordinator for V2 Option Sniffer (`option_sniffer_v2`).
    Never interferes with V1/V6B execution, rate limiters, WebSocket sockets, or `market_data.db`.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        intraday_candle_fetcher_fn: Optional[Callable[[str], List[Any]]] = None,
        contract_catalog_fn: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    ) -> None:
        self._op_lock = threading.RLock()
        self.config, self.config_hash = load_v2_preregistration()
        self.replay_run_mode: str = str(self.config.get("replay_run_mode") or "REPLAY_V2_2_CAUSAL")
        self.engine_version: str = str(self.config.get("engine_version") or "2.2.0-causal")
        self.intraday_candle_fetcher_fn = intraday_candle_fetcher_fn
        self.contract_catalog_fn = contract_catalog_fn
        self._last_intraday_backfill_epoch: float = 0.0
        self._last_dte2_pin_check_epoch: float = 0.0
        self._last_dte2_pin_atm: float = 0.0
        self._dte2_watchlist_pkg_id: str = "__V2_BOOK_B_DTE2_WATCHLIST__"

        # Never auto-create DB file during cold init / GET!
        self.store = V2LedgerStore(db_path=db_path, auto_create=False)
        self.simulator = V2PaperSimulator(
            config=self.config,
            config_hash=self.config_hash,
            store=self.store,
            run_mode="FORWARD_PAPER",
        )
        self.simulator.recover_state_from_db()

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
        self.feed_consumer.set_priority_keys(
            compute_v2_priority_keys(list(self.simulator.open_packages.values()))
        )
        self.protected_baseline_hashes = compute_protected_hashes_readonly()

        self.collector_state: str = "STANDBY_NOT_STARTED"
        self.collector_detail: str = "Collector not attached; GET views operate purely read-only."
        self._listener_attached: bool = False
        self._stream_mgr_ref: Any = None
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._archive_thread: Optional[threading.Thread] = None

        self.unified_engine = V2UnifiedEventEngine(
            cfg=self.config,
            cfg_hash=self.config_hash,
            store=self.store,
            simulator=self.simulator,
            run_mode="FORWARD_PAPER",
            persist_bars_and_checkpoints=True,
            on_package_opened_cb=self._on_package_opened,
            on_package_closed_cb=self._on_package_closed,
            on_skew_sample_cb=self.feed_consumer.record_skew_sample,
        )

        # Cached read-only archive report populated only by explicit CLI/control actions
        self._archive_audit_cache: Optional[Dict[str, Any]] = None

    # ──────────────────────────────────────────────────────────────────────────
    # LIFECYCLE: PASSIVE FAN-OUT START, OWNER PINS & V2-ONLY SHUTDOWN
    # ──────────────────────────────────────────────────────────────────────────

    def _get_stream_manager(self) -> Any:
        try:
            from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

            if hasattr(OptionStreamManager, "get_instance"):
                return OptionStreamManager.get_instance()
            return OptionStreamManager()
        except Exception as exc:
            logger.debug(f"[V2] OptionStreamManager unavailable: {exc}")
            return None

    @staticmethod
    def _is_stream_mgr_active(mgr: Any) -> bool:
        if mgr is None:
            return False
        return bool(
            getattr(mgr, "is_running", False)
            or getattr(mgr, "_is_registered_with_streamer", False)
        )

    def _active_stream_mgr(self) -> Any:
        return self._stream_mgr_ref or self._get_stream_manager()

    def _existing_v1_stream_keys(self) -> Set[str]:
        mgr = self._active_stream_mgr()
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
        mgr = self._active_stream_mgr()
        if mgr is None or not contracts:
            return
        try:
            if hasattr(mgr, "acquire_owned_pins"):
                hyg = self.config["quote_hygiene"]
                mgr.acquire_owned_pins(
                    owner="V2",
                    contracts=contracts,
                    max_owner_pins=int(hyg["max_v2_additive_pins"]),
                    soft_cap=int(hyg["ws_total_soft_cap"]),
                    v1_headroom=int(hyg["v1_reserved_ws_headroom"]),
                )
            else:
                mgr.pin_extra_contracts(contracts, owner="V2")
        except TypeError:
            mgr.pin_extra_contracts(contracts)
        except Exception as exc:
            logger.warning(f"[V2] Additive v2-only pin failed (non-fatal): {exc}")

    def _unpin_v2_only_keys(self, keys: List[str]) -> None:
        mgr = self._active_stream_mgr()
        if mgr is None or not keys:
            return
        try:
            if hasattr(mgr, "release_owned_pins"):
                mgr.release_owned_pins("V2", keys)
            elif hasattr(mgr, "unpin_extra_contracts"):
                try:
                    mgr.unpin_extra_contracts(keys, owner="V2")
                except TypeError:
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

    def _on_package_opened(self, package_id: str, contracts: List[Dict[str, Any]]) -> None:
        mgr = self._stream_mgr_ref
        self.subscription_registry.acquire_for_package(
            package_id=package_id,
            contracts=contracts,
            stream_manager=mgr,
            existing_stream_keys_fn=self._existing_v1_stream_keys,
            subscribe_additive_fn=self._subscribe_v2_only_additive,
        )
        self.feed_consumer.set_priority_keys(
            compute_v2_priority_keys(list(self.simulator.open_packages.values()))
        )

    def _on_package_closed(self, package_id: str) -> None:
        mgr = self._stream_mgr_ref
        self.subscription_registry.release_for_package(
            package_id=package_id,
            stream_manager=mgr,
            unpin_v2_only_fn=self._unpin_v2_only_keys,
        )
        self.feed_consumer.set_priority_keys(
            compute_v2_priority_keys(list(self.simulator.open_packages.values()))
        )

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
            self.store.ensure_initialized()
            mgr = stream_manager or self._get_stream_manager()
            self._stream_mgr_ref = mgr
            self._stop_event.clear()

            if mgr is not None and not self._listener_attached:
                try:
                    mgr.register_advance_feed_listener(self.on_live_feed_events)
                    self._listener_attached = True
                except Exception as exc:
                    logger.warning(f"[V2] Passive listener registration failed: {exc}")

            is_stream_live = self._is_stream_mgr_active(mgr)
            if self.simulator.is_booking_halted():
                self.collector_state = "DEGRADED_BOOKING_FAULT"
                self.collector_detail = f"Booking halt latched: {self.simulator.booking_fault}"
            elif self._listener_attached and is_stream_live:
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
            archive = self._archive_thread
            mgr = self._active_stream_mgr()

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
                stream_manager=mgr,
                unpin_v2_only_fn=self._unpin_v2_only_keys,
            )
            self.collector_state = "STOPPED_V2_ONLY"
            self.collector_detail = (
                "V2 collector detached cleanly; V1 OptionStreamManager and V1/V6B remain running."
            )

        if worker is not None and worker.is_alive():
            worker.join(timeout=1.5)
        if archive is not None and archive.is_alive() and archive is not threading.current_thread():
            archive.join(timeout=1.5)

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

    def _default_fetch_intraday_1m_candles(self, instrument_key: str) -> List[Any]:
        """Fetches intraday 1m index candles through the shared Upstox rate limiter."""
        try:
            from app.data.upstox_provider import UPSTOX_BASE_URL, get_upstox_token, upstox_request

            token = get_upstox_token()
            if not token:
                return []
            enc = urllib.parse.quote(str(instrument_key), safe="")
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            url = f"{UPSTOX_BASE_URL}/historical-candle/intraday/{enc}/1minute"
            res = upstox_request("GET", url, headers=headers, timeout=6.0)
            if res.status_code == 200:
                return list((res.json().get("data") or {}).get("candles") or [])
        except Exception as exc:
            logger.debug(f"[V2] Intraday 1m candle backfill skipped ({instrument_key}): {exc}")
        return []

    def _default_fetch_nifty_option_catalog(self) -> List[Dict[str, Any]]:
        """Reads cached NIFTY option contract master via shared OptionContractDiscovery."""
        try:
            from app.analytics.option_sniffer.option_contract_discovery import OptionContractDiscovery

            return list(OptionContractDiscovery.fetch_raw_contracts("NIFTY", force_refresh=False) or [])
        except Exception as exc:
            logger.debug(f"[V2] NIFTY option catalog read skipped: {exc}")
            return []

    def _maybe_backfill_intraday_spot_bars_locked(
        self,
        now_ms: int,
        inside_writer_session: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Causal 1m Spot & VIX backfill for late logins or morning restarts so ORB30/ADX/ER
        recover cleanly without lookahead (`bar_close_ms <= now_ms` strictly enforced).
        """
        fetcher = self.intraday_candle_fetcher_fn
        enabled = (fetcher is not None) or (
            self._listener_attached
            and self._is_stream_mgr_active(self._stream_mgr_ref)
            and not is_testing_environment()
        )
        if not enabled or now_ms <= 0:
            return None

        session_d, _, _, _, now_hhmm = bar_open_label_from_ms(now_ms)
        if not is_nse_trading_session(session_d) or not (916 <= now_hhmm <= 1530):
            return None
        if self.unified_engine.session_date not in (None, session_d):
            return None

        open_915_ms = parse_ist_timestamp_ms(f"{session_d}T09:15:00+05:30")
        expected_completed = max(0, (int(now_ms) - open_915_ms) // 60_000)
        if expected_completed < 1:
            return None

        bars = self.unified_engine.completed_bars if self.unified_engine.session_date == session_d else []
        has_0915 = any(b["bar_open_ts"][11:16] == "09:15" for b in bars)
        needs_bf = (not has_0915) or (len(bars) < expected_completed) or (self.unified_engine.sequence_gap_count > 0)
        if not needs_bf:
            return None

        now_epoch = time.time()
        if fetcher is None and self._last_intraday_backfill_epoch > 0.0 and (now_epoch - self._last_intraday_backfill_epoch) < 60.0:
            return None
        self._last_intraday_backfill_epoch = now_epoch

        fn = fetcher or self._default_fetch_intraday_1m_candles
        spot_raw = fn("NSE_INDEX|Nifty 50") or []
        vix_raw = fn("NSE_INDEX|India VIX") or []

        def _parse_rows(rows: List[Any]) -> List[Tuple[Any, float, float, float, float]]:
            out: List[Tuple[Any, float, float, float, float]] = []
            for r in rows:
                try:
                    if isinstance(r, (list, tuple)) and len(r) >= 5:
                        out.append((r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])))
                except Exception:
                    continue
            return out

        spot_tuples = _parse_rows(spot_raw)
        vix_tuples = _parse_rows(vix_raw)
        if not spot_tuples:
            return None

        if inside_writer_session:
            res_bf = self.unified_engine.backfill_missing_completed_bars(
                session_d, spot_tuples, vix_tuples, now_ms=now_ms
            )
        else:
            self.store.ensure_initialized()
            with self.store.writer_session():
                res_bf = self.unified_engine.backfill_missing_completed_bars(
                    session_d, spot_tuples, vix_tuples, now_ms=now_ms
                )

        if int(res_bf.get("sequence_gap_count", 1) or 0) == 0 and int(res_bf.get("inserted_count") or 0) > 0:
            self.feed_consumer.clear_uncertainty("MINUTE_BAR_GAP_")
        return res_bf

    def _maybe_ensure_book_b_dte2_watchlist_pins_locked(self, now_ms: int) -> None:
        """
        Ensures Book B's earliest `DTE >= 2` ATM +/- 50 strikes are passively streamed when
        V1's universe only tracks a `T-1`/`T-0` expiry (e.g., Friday before Monday expiry).
        """
        cat_fn = self.contract_catalog_fn
        enabled = (cat_fn is not None) or (
            self._listener_attached
            and self._is_stream_mgr_active(self._stream_mgr_ref)
            and not is_testing_environment()
        )
        if not enabled or now_ms <= 0:
            return

        mgr = self._stream_mgr_ref
        session_d, _, _, _, now_hhmm = bar_open_label_from_ms(now_ms)
        if not is_nse_trading_session(session_d) or now_hhmm > 1305:
            # Release Book B watchlist pins after 13:05 entry window closes
            self.subscription_registry.sync_for_package(
                package_id=self._dte2_watchlist_pkg_id,
                contracts=[],
                stream_manager=mgr,
                existing_stream_keys_fn=self._existing_v1_stream_keys,
                subscribe_additive_fn=self._subscribe_v2_only_additive,
                unpin_v2_only_fn=self._unpin_v2_only_keys,
            )
            return
        if now_hhmm < 915:
            return

        spot_px = float(
            (self.unified_engine.latest_spot_q or {}).get("spot_ltp")
            or self.unified_engine.last_indicators.get("spot_close")
            or 0.0
        )
        if spot_px <= 0.0:
            return
        atm = round(spot_px / 50.0) * 50.0

        now_epoch = time.time()
        if (
            self._last_dte2_pin_check_epoch > 0.0
            and abs(atm - self._last_dte2_pin_atm) < 50.0
            and (now_epoch - self._last_dte2_pin_check_epoch) < 60.0
        ):
            return
        self._last_dte2_pin_check_epoch = now_epoch
        self._last_dte2_pin_atm = atm

        min_dte = int(self.config["books"]["V2_BOOK_B_PAID_FLOW"]["min_trading_dte"])
        max_dte = int(self.config["books"]["V2_BOOK_B_PAID_FLOW"].get("max_trading_dte", 12))
        target_strikes = (atm - 50.0, atm, atm + 50.0)

        v1_covered_by_tuple: Dict[Tuple[str, float, str], Dict[str, Any]] = {}
        if mgr is not None and hasattr(mgr, "_lock") and hasattr(mgr, "_key_to_contract"):
            with mgr._lock:
                for _, meta in (mgr._key_to_contract or {}).items():
                    if not isinstance(meta, dict):
                        continue
                    if meta.get("v2_owned_only") or meta.get("is_futures") or meta.get("is_underlying"):
                        continue
                    if str(meta.get("underlying") or "").upper() != "NIFTY":
                        continue
                    exp_s = str(meta.get("expiry") or "").strip()[:10]
                    ot_s = str(meta.get("option_type") or "").upper().strip()
                    try:
                        stk_f = float(meta.get("strike") or 0.0)
                    except (TypeError, ValueError):
                        stk_f = 0.0
                    if exp_s and ot_s in ("CE", "PE") and stk_f > 0.0:
                        v1_covered_by_tuple[(exp_s, stk_f, ot_s)] = dict(meta)

        v1_dte2_expiries = sorted(
            {
                exp
                for (exp, _, _) in v1_covered_by_tuple.keys()
                if exp >= session_d and min_dte <= trading_sessions_between(session_d, exp) <= max_dte
            }
        )
        if v1_dte2_expiries:
            v1_exp = v1_dte2_expiries[0]
            if all((v1_exp, k, ot) in v1_covered_by_tuple for k in target_strikes for ot in ("CE", "PE")):
                # V1 already covers all 6 strikes of earliest DTE >= 2 expiry -> 0 V2 pins needed!
                self.subscription_registry.sync_for_package(
                    package_id=self._dte2_watchlist_pkg_id,
                    contracts=[],
                    stream_manager=mgr,
                    existing_stream_keys_fn=self._existing_v1_stream_keys,
                    subscribe_additive_fn=self._subscribe_v2_only_additive,
                    unpin_v2_only_fn=self._unpin_v2_only_keys,
                )
                return

        raw_catalog = cat_fn() if cat_fn is not None else self._default_fetch_nifty_option_catalog()
        if not raw_catalog:
            return

        catalog_by_tuple: Dict[Tuple[str, float, str], Dict[str, Any]] = {}
        default_lot = int(self.config["account_envelope"]["lot_size_default_nifty"])
        for item in raw_catalog:
            if not isinstance(item, dict):
                continue
            exp_s = str(item.get("expiry") or "").strip()[:10]
            ot_s = str(item.get("instrument_type") or item.get("option_type") or "").upper().strip()
            ik = str(item.get("instrument_key") or "").strip()
            if not ik or not exp_s or exp_s < session_d or ot_s not in ("CE", "PE"):
                continue
            try:
                stk_f = float(
                    item.get("strike_price") if item.get("strike_price") is not None else (item.get("strike") or 0.0)
                )
            except (TypeError, ValueError):
                continue
            if stk_f <= 0.0:
                continue
            u_val = str(item.get("underlying_symbol") or item.get("underlying") or "NIFTY").upper().strip()
            if u_val != "NIFTY":
                continue
            catalog_by_tuple[(exp_s, stk_f, ot_s)] = {
                "instrument_key": ik,
                "trading_symbol": str(item.get("trading_symbol") or f"NIFTY {int(stk_f)} {ot_s} {exp_s}"),
                "underlying": "NIFTY",
                "expiry": exp_s,
                "strike": stk_f,
                "option_type": ot_s,
                "lot_size": int(item.get("lot_size") or default_lot),
            }

        cand_expiries = sorted(
            {
                exp
                for (exp, _, _) in catalog_by_tuple.keys()
                if exp >= session_d and min_dte <= trading_sessions_between(session_d, exp) <= max_dte
            }
        )
        if not cand_expiries:
            return
        target_exp = cand_expiries[0]

        missing_contracts: List[Dict[str, Any]] = []
        for k in target_strikes:
            for ot in ("CE", "PE"):
                tup = (target_exp, k, ot)
                if tup in v1_covered_by_tuple:
                    continue
                if tup in catalog_by_tuple:
                    missing_contracts.append(catalog_by_tuple[tup])

        res_sync = self.subscription_registry.sync_for_package(
            package_id=self._dte2_watchlist_pkg_id,
            contracts=missing_contracts,
            stream_manager=mgr,
            existing_stream_keys_fn=self._existing_v1_stream_keys,
            subscribe_additive_fn=self._subscribe_v2_only_additive,
            unpin_v2_only_fn=self._unpin_v2_only_keys,
        )
        skipped = set(res_sync.get("skipped_due_to_v1_headroom", []))
        for c in missing_contracts:
            if c["instrument_key"] not in skipped:
                self.unified_engine.opt_contracts[(c["expiry"], float(c["strike"]), c["option_type"])] = dict(c)

    def process_queued_events(
        self,
        now_dt: Optional[datetime] = None,
        persist_ingest: bool = False,
        max_items: int = 500,
    ) -> Dict[str, Any]:
        """Drains bounded feed queue and routes normalized events through shared `V2UnifiedEventEngine`."""
        batch = self.feed_consumer.drain_batch(max_items=max_items)
        now_ms = (
            int(to_ist_aware(now_dt).timestamp() * 1000)
            if now_dt is not None
            else int(time.time() * 1000)
        )

        if not batch:
            with self._op_lock:
                res_bar = None
                bb = self.unified_engine.building_bar
                if bb is not None and now_ms >= int(bb["bar_close_ms"]):
                    with self.store.writer_session():
                        res_bar = self.unified_engine.ingest_event(
                            kind="BAR_CLOSE_CHECK",
                            symbol_or_key="CLOCK",
                            payload={"availability_ts_ms": now_ms},
                            availability_ts_ms=now_ms,
                            uncertainty_blocks=self.feed_consumer.get_health_metrics()["uncertainty_blocks"],
                        )
                self._maybe_backfill_intraday_spot_bars_locked(now_ms, inside_writer_session=False)
                self._maybe_ensure_book_b_dte2_watchlist_pins_locked(now_ms)
                return {"processed": 0, "bar_evaluated": bool(res_bar), "result": res_bar}

        with self._op_lock:
            self.store.ensure_initialized()
            with self.store.writer_session():
                if persist_ingest:
                    self.store.persist_ingest_events(batch, "FORWARD_PAPER", now_ms)

                health = self.feed_consumer.get_health_metrics()
                self.unified_engine.overflow_drop_count = int(health.get("dropped_overflow_events") or 0)

                for k in self.subscription_registry.snapshot_status()["evicted_v2_keys"]:
                    self.simulator.mark_key_evicted(k)

                # Attach authoritative availability timestamps cleanly
                prepared: List[Tuple[int, int, Dict[str, Any]]] = []
                for idx, env in enumerate(batch):
                    p = env["payload"]
                    has_explicit_avail = (
                        p.get("availability_ts_ms") is not None
                        or p.get("local_receipt_ts_ms") is not None
                    )
                    avail_ms = (
                        int(p.get("availability_ts_ms") or p.get("local_receipt_ts_ms"))
                        if has_explicit_avail
                        else (now_ms if now_dt is not None else int(env.get("availability_ts_ms") or now_ms))
                    )
                    prepared.append((avail_ms, idx, env))
                prepared.sort(key=lambda item: (item[0], item[1]))

                last_bar_res: Optional[Dict[str, Any]] = None
                blocks = health.get("uncertainty_blocks", [])
                proc_t0_ns = time.perf_counter_ns()
                for avail_ms, _, env in prepared:
                    res_ev = self.unified_engine.ingest_event(
                        kind=env["kind"],
                        symbol_or_key=env["symbol_or_key"],
                        payload=env["payload"],
                        availability_ts_ms=avail_ms,
                        uncertainty_blocks=blocks,
                    )
                    if res_ev is not None:
                        last_bar_res = res_ev
                self.feed_consumer.record_processing_duration_ms(
                    (time.perf_counter_ns() - proc_t0_ns) / 1_000_000.0
                )

                if (
                    self.unified_engine.building_bar is not None
                    and (
                        now_dt is not None
                        or self.unified_engine.building_bar["session_date"]
                        == bar_open_label_from_ms(now_ms)[0]
                    )
                    and now_ms >= int(self.unified_engine.building_bar["bar_close_ms"])
                ):
                    close_res = self.unified_engine.ingest_event(
                        kind="BAR_CLOSE_CHECK",
                        symbol_or_key="CLOCK",
                        payload={"availability_ts_ms": now_ms},
                        availability_ts_ms=now_ms,
                        uncertainty_blocks=blocks,
                    )
                    if close_res is not None:
                        last_bar_res = close_res

                ref_now_ms = (
                    now_ms
                    if now_dt is not None
                    else (
                        prepared[-1][0]
                        if (self.intraday_candle_fetcher_fn is not None or self.contract_catalog_fn is not None)
                        else now_ms
                    )
                )
                self._maybe_backfill_intraday_spot_bars_locked(ref_now_ms, inside_writer_session=True)
                self._maybe_ensure_book_b_dte2_watchlist_pins_locked(ref_now_ms)

            if self.simulator.is_booking_halted():
                self.collector_state = "DEGRADED_BOOKING_FAULT"
                self.collector_detail = f"Booking halt latched: {self.simulator.booking_fault}"

            return {
                "processed": len(prepared),
                "bar_evaluated": last_bar_res is not None and last_bar_res.get("status") != "POST_SESSION_FLUSH_IGNORED",
                "last_decision": last_bar_res,
            }

    def reconcile_booking_fault(self) -> Dict[str, Any]:
        """Explicit paper-only reconciliation clearing a latched SQLite booking fault."""
        with self._op_lock:
            self.store.ensure_initialized()
            res = self.simulator.reconcile_after_booking_fault()
            if not self.simulator.is_booking_halted():
                is_live = self._is_stream_mgr_active(self._stream_mgr_ref)
                if self._listener_attached:
                    self.collector_state = "PARALLEL_LIVE_COLLECTION_ACTIVE" if is_live else "STANDBY_STREAM_INACTIVE"
                    self.collector_detail = "Paper booking fault reconciled from ledger; collector restored."
                else:
                    self.collector_state = "STANDBY_NOT_STARTED"
                    self.collector_detail = "Paper booking fault reconciled from ledger."
            return {
                "reconciled": not self.simulator.is_booking_halted(),
                "collector_state": self.collector_state,
                **res,
            }

    # ──────────────────────────────────────────────────────────────────────────
    # EXPLICIT ARCHIVE REPLAY & COUNTERFACTUAL EXECUTION (NOT CALLED ON GET)
    # ──────────────────────────────────────────────────────────────────────────

    def run_and_persist_archive_audit(self, v1_db_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Explicit command / control action to run read-only causal archive replay & V1 counterfactuals
        and persist into V2's isolated SQLite DB (under `self.replay_run_mode`).
        """
        self.store.ensure_initialized()
        v1_conn = open_v1_readonly_db(v1_db_path)
        try:
            with self.store.writer_session():
                cf = run_v1_counterfactual_audit(v1_conn, store=self.store)
                rep = run_v2_replay_and_attribution(v1_conn, self.store, self.config, self.config_hash)
            o3 = load_phase4_o3_audit_report()
            overshoot = reconcile_v1_book_b_stop_and_overshoot(v1_conn)
        finally:
            v1_conn.close()

        bundle = {
            "replay_status": "ARCHIVE_REPLAY_PERSISTED",
            "engine_version": self.engine_version,
            "replay_run_mode": self.replay_run_mode,
            "phase4_o3_audit": o3,
            "v1_book_b_overshoot_audit": overshoot,
            "counterfactuals": cf,
            "replay_and_attribution": rep,
        }
        with self._op_lock:
            self._archive_audit_cache = bundle
        return bundle

    # ──────────────────────────────────────────────────────────────────────────
    # STRICTLY READ-ONLY QUERY METHODS (ZERO DB CREATION / ZERO WRITES ON GET)
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
            sod = eq
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
                    "sod_equity_inr": sod,
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
        fwd_equity_curve: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        gates_cfg = self.config["promotion_gates"]
        start_cap = float(self.config["account_envelope"]["starting_capital_inr"])
        fwd_stats = _compute_series_stats(fwd_closed, fwd_daily, start_cap, equity_curve=fwd_equity_curve)

        sessions_observed = sum(
            1 for d in fwd_daily if str(d.get("session_state") or "COMPLETE_CLEAN") == "COMPLETE_CLEAN"
        )
        all_sessions_count = len(fwd_daily)
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
            "all_sessions_recorded": all_sessions_count,
            "unresolved_exposure_count": len(unresolved_open),
            "unresolved_structural_risk_charged_inr": unresolved_structural_loss,
            "forward_stats": fwd_stats,
            "checks": checks,
        }

    def _compute_feed_hygiene_snapshot(self) -> Dict[str, Any]:
        now_ms = int(time.time() * 1000)
        sq = self.unified_engine.latest_spot_q
        fq = self.unified_engine.latest_fut_q
        spot_age = resolve_strict_quote_age_ms(sq, now_ms=now_ms, require_observed_l5=False)
        fut_age = resolve_strict_quote_age_ms(fq, now_ms=now_ms, require_observed_l5=False)
        sync_res = (
            check_spot_futures_sync(sq, fq, now_ms=now_ms)
            if (sq and fq)
            else {"ok": False, "verifiable": False, "reason": "NO_LIVE_SPOT_OR_FUT_OBSERVED"}
        )
        return {
            "feed_consumer": self.feed_consumer.get_health_metrics(),
            "subscription_registry": self.subscription_registry.snapshot_status(),
            "spot_age_ms": spot_age,
            "futures_age_ms": fut_age,
            "spot_fut_sync": sync_res,
            "open_basis_reference": self.unified_engine.open_basis_ref,
            "open_basis_samples_count": int(
                (self.unified_engine.open_basis_ref or {}).get("sample_count")
                or (1 if (self.unified_engine.open_basis_ref or {}).get("ok") else 0)
            ),
            "completed_bars_today": len(self.unified_engine.completed_bars),
            "orb30_ready": bool(self.unified_engine.last_indicators.get("orb30_ready")),
            "foreign_instrument_rejected_count": self.unified_engine.foreign_instrument_rejected_count,
            "unverifiable_basis_events": self.unified_engine.unverifiable_basis_events,
            "late_event_count": self.unified_engine.late_event_count,
        }

    def _compute_collection_and_alerts(
        self,
        fwd_open: List[Dict[str, Any]],
        hygiene: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        now_ms = int(time.time() * 1000)
        last_evt_ms = int(self.unified_engine.last_usable_event_ts_ms or 0)
        last_evt_ist = format_ist_iso(last_evt_ms) if last_evt_ms > 0 else None
        freshness_ms = max(0, now_ms - last_evt_ms) if last_evt_ms > 0 else None
        db_exists = self.store.db_exists()
        max_fresh_ms = int(self.config["quote_hygiene"]["max_entry_quote_age_ms"])

        is_halted = self.simulator.is_booking_halted()
        receiving_usable = bool(
            self._listener_attached
            and last_evt_ms > 0
            and freshness_ms is not None
            and freshness_ms <= max(15_000, max_fresh_ms * 3)
        )
        if is_halted:
            eff_state = "DEGRADED_BOOKING_FAULT"
            eff_detail = f"Booking halt active until paper ledger reconciliation: {self.simulator.booking_fault}"
        elif self.collector_state == "STOPPED_V2_ONLY":
            eff_state = "STOPPED_V2_ONLY"
            eff_detail = self.collector_detail
        elif self._listener_attached:
            if receiving_usable:
                eff_state = "ACTIVE_RECEIVING_DATA"
                eff_detail = f"Receiving fresh events (last usable event {last_evt_ist})."
            elif self._is_stream_mgr_active(self._stream_mgr_ref):
                eff_state = "IDLE_NO_RECENT_EVENTS"
                eff_detail = "Listener attached to running stream, but no recent usable NIFTY events received."
            else:
                eff_state = "STANDBY_STREAM_INACTIVE"
                eff_detail = "Listener attached cleanly; waiting for active market stream events."
        elif not db_exists:
            eff_state = "COLD_NOT_INITIALIZED"
            eff_detail = "Collector not started and V2 ledger DB not yet created; GET remains strictly read-only."
        else:
            eff_state = self.collector_state
            eff_detail = self.collector_detail

        alerts: List[Dict[str, Any]] = []
        if is_halted:
            alerts.append(
                {
                    "severity": "critical",
                    "code": "BOOKING_FAULT_HALTED",
                    "message": eff_detail,
                }
            )
        unres = [
            p
            for p in fwd_open
            if p.get("is_unpriced_exposure") or p["state"] in ("HEDGE_ONLY_EXPOSURE_OPEN", "UNRESOLVED_EXPOSURE_PAST_1445")
        ]
        if unres:
            risk_sum = round(sum(abs(float(p.get("structural_max_loss_inr") or 0.0)) for p in unres), 2)
            alerts.append(
                {
                    "severity": "critical",
                    "code": "UNRESOLVED_EXPOSURE",
                    "message": f"{len(unres)} unresolved/unpriced position(s); conservative structural risk INR {risk_sum:,.2f} charged to MTM DD.",
                }
            )
        evicted = (hygiene.get("subscription_registry") or {}).get("evicted_v2_keys") or []
        if evicted:
            alerts.append(
                {
                    "severity": "warning",
                    "code": "SUBSCRIPTION_PRESSURE_YIELD",
                    "message": f"V2 yielded {len(evicted)} V2-owned key(s) under WS pressure ({', '.join(evicted)}).",
                }
            )
        if self._listener_attached and not receiving_usable:
            alerts.append(
                {
                    "severity": "warning",
                    "code": "FEED_IDLE_OR_STALE",
                    "message": (
                        f"Last usable event was {round(freshness_ms / 1000.0, 1)}s ago ({last_evt_ist}); market data stale or closed."
                        if freshness_ms is not None
                        else "No usable NIFTY market events received yet in current listener session."
                    ),
                }
            )

        status_obj = {
            "collector_state": eff_state,
            "collector_detail": eff_detail,
            "listener_attached": self._listener_attached,
            "receiving_usable_data": receiving_usable,
            "last_usable_event_ts_ms": last_evt_ms if last_evt_ms > 0 else None,
            "last_usable_event_ist": last_evt_ist,
            "data_freshness_ms": freshness_ms,
            "db_path": self.store.db_path,
            "db_exists": db_exists,
            "booking_halted": is_halted,
            "booking_fault": self.simulator.booking_fault,
        }
        return status_obj, alerts

    def _build_book_status_rows(
        self,
        fwd_open: List[Dict[str, Any]],
        fwd_closed: List[Dict[str, Any]],
        fwd_signals: List[Dict[str, Any]],
        fwd_fills: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        cfg_a = self.config["books"]["V2_BOOK_A_PROPOSED_O3"]
        cfg_b = self.config["books"]["V2_BOOK_B_PAID_FLOW"]
        last_dec = self.unified_engine.latest_decision_by_book
        open_by_book = {b: [p for p in fwd_open if p["book_id"] == b] for b in (cfg_a["book_id"], cfg_b["book_id"])}
        closed_by_book = {b: [p for p in fwd_closed if p["book_id"] == b] for b in (cfg_a["book_id"], cfg_b["book_id"])}
        fills_by_pkg: Dict[str, List[Dict[str, Any]]] = {}
        for f in fwd_fills:
            fills_by_pkg.setdefault(f["package_id"], []).append(f)

        def _last_fill_for_book(bid: str) -> Optional[Dict[str, Any]]:
            pkgs = closed_by_book.get(bid, []) + open_by_book.get(bid, [])
            best_fill: Optional[Dict[str, Any]] = None
            for p in pkgs:
                for f in fills_by_pkg.get(p["package_id"], []):
                    if best_fill is None or int(f.get("ts_ms") or 0) > int(best_fill.get("ts_ms") or 0):
                        best_fill = f
            return best_fill

        dec_a = last_dec.get(cfg_a["book_id"]) or next(
            (s for s in fwd_signals if s["book_id"] == cfg_a["book_id"]), None
        )
        dec_b = last_dec.get(cfg_b["book_id"]) or next(
            (s for s in fwd_signals if s["book_id"] == cfg_b["book_id"]), None
        )

        rows = [
            {
                "book_id": cfg_a["book_id"],
                "book_label": "Book A (Phase-4 O3 T-1->T0 Credit Spread)",
                "config_summary": "Disabled (T-1 Mondays only • Unverified 15m vs L5 O3 spec)",
                "enabled": False,
                "eligibility": "DISABLED_UNVERIFIED",
                "eligibility_reason": "BLOCKED_UNVERIFIED_O3_SPEC_CONFLICT (Phase-4 O3 uses 15m Open[t+1], 70% decay, 100pt grid vs V2 50% / 50pt draft).",
                "current_position": open_by_book[cfg_a["book_id"]][0]["direction_label"] if open_by_book[cfg_a["book_id"]] else "NONE",
                "latest_decision": (dec_a or {}).get("state") or "DISABLED_UNVERIFIED",
                "latest_rejection_reason": (dec_a or {}).get("reject_reason") or "BLOCKED_UNVERIFIED_O3_SPEC_CONFLICT",
                "last_fill": _last_fill_for_book(cfg_a["book_id"]),
            },
            {
                "book_id": cfg_b["book_id"],
                "book_label": "Book B (Paid Convexity Flow: Median Open-Basis + 10:15-13:00)",
                "config_summary": (
                    f"CashCap Rs{int(float(cfg_b['max_entry_outlay_inr'])):,} (+1-OTM) • "
                    f"Max Planned SL Rs{int(float(cfg_b['planned_loss_max_inr'])):,} • "
                    f"{int(cfg_b['entry_window_start_hhmm']):04d}-{int(cfg_b['entry_window_end_hhmm']):04d} IST"
                ),
                "enabled": True,
                "eligibility": (
                    "HALTED_BOOKING_FAULT"
                    if self.simulator.is_booking_halted()
                    else ("IN_POSITION" if open_by_book[cfg_b["book_id"]] else ((dec_b or {}).get("state") or "WAITING_STREAM"))
                ),
                "eligibility_reason": (
                    str((self.simulator.booking_fault or {}).get("error"))
                    if self.simulator.is_booking_halted()
                    else ((dec_b or {}).get("reject_reason") or "ACTIVE_ELIGIBLE_1015_1300_IST")
                ),
                "current_position": open_by_book[cfg_b["book_id"]][0]["direction_label"] if open_by_book[cfg_b["book_id"]] else "NONE",
                "latest_decision": (dec_b or {}).get("state") or ("TRIGGERED" if (dec_b or {}).get("passed") else "NO_BARS_EVALUATED"),
                "latest_rejection_reason": (dec_b or {}).get("reject_reason"),
                "last_fill": _last_fill_for_book(cfg_b["book_id"]),
            },
        ]
        return rows

    def get_overview(
        self,
        include_collateral: bool = False,
        collateral_yield_pa: float = 6.0,
        haircut_pct: float = 10.0,
    ) -> Dict[str, Any]:
        """Purely read-only overview endpoint (`0` DB writes, `0` DB creation, `0` subscriptions)."""
        fwd_pkgs = self.store.load_packages("FORWARD_PAPER", self.config_hash)
        fwd_open = [p for p in fwd_pkgs if p["state"] in OPEN_EXPOSURE_STATES]
        fwd_closed = [p for p in fwd_pkgs if p["state"] not in OPEN_EXPOSURE_STATES]
        fwd_daily = self.store.load_daily_snapshots("FORWARD_PAPER", self.config_hash)
        fwd_eq = self.store.load_equity_curve("FORWARD_PAPER", self.config_hash)
        fwd_signals = self.store.load_signals("FORWARD_PAPER", self.config_hash, limit=10)
        fwd_fills = self.store.load_fills("FORWARD_PAPER", self.config_hash)

        rep_pkgs = self.store.load_packages(self.replay_run_mode, self.config_hash)
        rep_closed = [p for p in rep_pkgs if p["state"] not in OPEN_EXPOSURE_STATES]
        rep_daily = self.store.load_daily_snapshots(self.replay_run_mode, self.config_hash)
        rep_eq = self.store.load_equity_curve(self.replay_run_mode, self.config_hash)

        start_cap = float(self.config["account_envelope"]["starting_capital_inr"])
        rep_stats = _compute_series_stats(rep_closed, rep_daily, start_cap, equity_curve=rep_eq)
        promo = self.evaluate_promotion_gates(fwd_daily, fwd_closed, fwd_open, fwd_equity_curve=fwd_eq)
        hygiene = self._compute_feed_hygiene_snapshot()
        col_status, alerts = self._compute_collection_and_alerts(fwd_open, hygiene)
        book_rows = self._build_book_status_rows(fwd_open, fwd_closed, fwd_signals, fwd_fills)

        dd_audit = audit_historical_drawdown_separation(fwd_eq, fwd_pkgs, start_cap)
        metric_corrections = self.store.load_metric_corrections("FORWARD_PAPER", self.config_hash)
        fwd_opps = self.store.load_opportunities("FORWARD_PAPER", self.config_hash, limit=500)
        opp_counts = {
            "total": len(fwd_opps),
            "accepted": sum(1 for o in fwd_opps if o.get("decision_status") == "ACCEPTED"),
            "rejected": sum(1 for o in fwd_opps if o.get("decision_status") == "REJECTED"),
            "blocked": sum(1 for o in fwd_opps if o.get("decision_status") == "BLOCKED"),
            "breakout_bars_recorded": sum(1 for o in fwd_opps if int(o.get("is_breakout_bar") or 0) == 1),
        }

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
            "engine_version": self.engine_version,
            "replay_run_mode": self.replay_run_mode,
            "config_hash": self.config_hash,
            "simulation_mode": True,
            "live_capable_by_config": False,
            "execution_adapter": self.config["execution_adapter"],
            "result_boundary_notice": (
                f"FORWARD_PAPER and {self.replay_run_mode} metrics are strictly separated. "
                "V1 counterfactuals and superseded v2.0.0 runs are excluded from all V2 totals."
            ),
            "collection_status": col_status,
            "active_alerts": alerts,
            "book_status_rows": book_rows,
            "books": {
                "V2_BOOK_A_PROPOSED_O3": {
                    **self.config["books"]["V2_BOOK_A_PROPOSED_O3"],
                    "status_banner": "DISABLED FOR AUTONOMOUS FORWARD ENTRIES: UNVERIFIED_SPEC_CONFLICT",
                },
                "V2_BOOK_B_PAID_FLOW": {
                    **self.config["books"]["V2_BOOK_B_PAID_FLOW"],
                    "status_banner": "ACTIVE PAPER HYPOTHESIS (Median Open-Basis + 10:15-13:00 + CashCap Rs18.5k + 20%/Rs1800 + 1-OTM)",
                },
            },
            "forward_paper_summary": {
                **promo["forward_stats"],
                "open_count": len(fwd_open),
                "unresolved_exposure_count": promo["unresolved_exposure_count"],
                "unresolved_structural_risk_charged_inr": promo["unresolved_structural_risk_charged_inr"],
                "realized_equity_inr": self.simulator.realized_equity_inr,
                "reserved_capital_inr": self.simulator.reserved_capital_inr,
                "booking_halted": self.simulator.is_booking_halted(),
                "observed_mark_dd_inr": dd_audit["observed_mark_max_dd_inr"],
                "observed_mark_dd_pct": dd_audit["observed_mark_max_dd_pct_start"],
                "observed_running_peak_inr": dd_audit["observed_mark_running_peak_inr"],
                "stress_dd_inr": dd_audit["stress_max_dd_inr"],
                "stress_dd_pct": dd_audit["stress_max_dd_pct_start"],
                "has_unpriced_stress_inflation": dd_audit["has_unpriced_stress_inflation"],
                "drawdown_audit_correction": {
                    "audit_version": "v2.2.1-dd-separation",
                    "historical_rows_mutated": False,
                    **dd_audit,
                    "persisted_corrections": metric_corrections,
                },
                "canonical_ledger_sha256": self.store.compute_canonical_ledger_digest("FORWARD_PAPER", self.config_hash),
            },
            "opportunity_summary": opp_counts,
            "open_basis_fail_summary": self.unified_engine.open_basis_fail_counts,
            "stress_kill_warnings_by_date": self.unified_engine.stress_kill_warnings_by_date,
            "archive_replay_summary": {
                **rep_stats,
                "persisted_sessions": len(rep_daily),
                "replay_run_mode": self.replay_run_mode,
                "canonical_ledger_sha256": (
                    self.store.compute_canonical_ledger_digest(self.replay_run_mode, self.config_hash) if rep_daily else None
                ),
            },
            "superseded_v2_0_replay": load_superseded_v2_0_evidence(),
            "promotion_gates": promo,
            "feed_hygiene": hygiene,
            "optional_collateral_reporting": {
                "forward_paper": col_forward,
                "archive_replay": col_replay,
            },
        }

    def get_positions(self) -> Dict[str, Any]:
        """Purely read-only positions, fills, consumed rungs & event timelines endpoint."""
        fwd_pkgs = self.store.load_packages("FORWARD_PAPER", self.config_hash)
        rep_pkgs = self.store.load_packages(self.replay_run_mode, self.config_hash)
        fwd_fills = self.store.load_fills("FORWARD_PAPER", self.config_hash)
        rep_fills = self.store.load_fills(self.replay_run_mode, self.config_hash)
        fwd_sigs = {s["signal_id"]: s for s in self.store.load_signals("FORWARD_PAPER", self.config_hash, limit=200)}
        rep_sigs = {s["signal_id"]: s for s in self.store.load_signals(self.replay_run_mode, self.config_hash, limit=200)}

        fills_fwd_by_pkg: Dict[str, List[Dict[str, Any]]] = {}
        for f in fwd_fills:
            fills_fwd_by_pkg.setdefault(f["package_id"], []).append(f)
        fills_rep_by_pkg: Dict[str, List[Dict[str, Any]]] = {}
        for f in rep_fills:
            fills_rep_by_pkg.setdefault(f["package_id"], []).append(f)

        now_ms = int(time.time() * 1000)
        fwd_open: List[Dict[str, Any]] = []
        fwd_closed: List[Dict[str, Any]] = []
        for p in fwd_pkgs:
            sig = fwd_sigs.get(p.get("signal_id") or "")
            enriched = {
                **p,
                "fills": fills_fwd_by_pkg.get(p["package_id"], []),
                "signal_gates": (sig or {}).get("gates", []),
                "signal_provenance": (sig or {}).get("provenance", {}),
                "entry_bar_ist": format_ist_iso(int(p.get("entry_ts_ms") or 0)) if p.get("entry_ts_ms") else p.get("entry_bar_ts"),
                "exit_bar_ist": format_ist_iso(int(p.get("exit_ts_ms") or 0)) if p.get("exit_ts_ms") else p.get("exit_bar_ts"),
            }
            if p["state"] in OPEN_EXPOSURE_STATES:
                ls = p["legs_state"]
                since_ms = p.get("unpriced_since_ts_ms")
                age_unpriced = max(0, now_ms - int(since_ms)) if since_ms else None
                last_rel_ms = p.get("last_reliable_mark_ts_ms")
                mark_age = max(0, now_ms - int(last_rel_ms)) if last_rel_ms else None
                is_unpriced = bool(p.get("is_unpriced_exposure"))
                is_stale = is_unpriced or (
                    mark_age is not None
                    and mark_age > int(self.config["quote_hygiene"]["max_exit_quote_age_ms"])
                )
                stress_est = (
                    -abs(float(p.get("structural_max_loss_inr") or 0.0))
                    if is_unpriced
                    else p.get("last_mark_net_pnl_inr")
                )
                fwd_open.append(
                    {
                        **enriched,
                        "unpriced_age_ms": age_unpriced,
                        "mark_age_ms": mark_age,
                        "fresh_executable_mark_inr": (None if is_unpriced else p.get("last_mark_net_pnl_inr")),
                        "fresh_mark_ts_ms": (None if is_unpriced else p.get("last_mark_ts_ms")),
                        "is_stale_mark": is_stale,
                        "conservative_stress_estimate_inr": stress_est,
                        "conservative_worst_loss_inr": stress_est,
                        "legs_count": 2 if ls.get("short_leg") else 1,
                    }
                )
            else:
                fwd_closed.append(enriched)

        rep_closed = [
            {
                **p,
                "fills": fills_rep_by_pkg.get(p["package_id"], []),
                "signal_gates": (rep_sigs.get(p.get("signal_id") or "") or {}).get("gates", []),
            }
            for p in rep_pkgs
            if p["state"] not in OPEN_EXPOSURE_STATES
        ]

        return {
            "simulation_mode": True,
            "engine_version": self.engine_version,
            "replay_run_mode": self.replay_run_mode,
            "forward_open_positions": fwd_open,
            "forward_closed_trades": fwd_closed,
            "forward_fills": fwd_fills,
            "replay_closed_trades": rep_closed,
            "replay_fills": rep_fills,
        }

    def get_daily_comparison(self) -> Dict[str, Any]:
        """Purely read-only V1 vs V2 daily table (`REPLAY_V2_1_CAUSAL`, `FORWARD_PAPER`, `V1_REPORTED`)."""
        v1 = self._read_v1_summary_readonly()
        rep_daily = self.store.load_daily_snapshots(self.replay_run_mode, self.config_hash)
        fwd_daily = self.store.load_daily_snapshots("FORWARD_PAPER", self.config_hash)
        rep_pkgs = self.store.load_packages(self.replay_run_mode, self.config_hash)
        fwd_pkgs = self.store.load_packages("FORWARD_PAPER", self.config_hash)
        rep_eq = self.store.load_equity_curve(self.replay_run_mode, self.config_hash)
        fwd_eq = self.store.load_equity_curve("FORWARD_PAPER", self.config_hash)

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
                rep_trades = rr.get("closed_trades")
                v_outcome = (
                    "REPLAY_NOT_YET_RUN"
                    if not rr
                    else (
                        "INSUFFICIENT_SOURCE_TS_OR_CONTRACT_EVIDENCE"
                        if int(rep_trades or 0) == 0
                        else "VERIFIED"
                    )
                )
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
                        "v2_session_state": rr.get("session_state"),
                        "result_origin": self.replay_run_mode if rr else "REPLAY_NOT_YET_RUN",
                        "verification_outcome": v_outcome,
                    }
                )

        return {
            "engine_version": self.engine_version,
            "replay_run_mode": self.replay_run_mode,
            "archive_limitation_note": (
                "Historical V1 archive snapshots (2026-11-16..2026-11-23) lack separate Spot/Futures "
                "source timestamps and Futures contract metadata, so strict causal archive replay records "
                "0 verified basis trades; live streaming populates those fields continuously."
            ),
            "v1_reported_summary": v1["summary"],
            "v2_replay_summary": _compute_series_stats(
                [p for p in rep_pkgs if p["state"] not in OPEN_EXPOSURE_STATES],
                rep_daily,
                float(self.config["account_envelope"]["starting_capital_inr"]),
                equity_curve=rep_eq,
            ),
            "v2_forward_paper_summary": _compute_series_stats(
                [p for p in fwd_pkgs if p["state"] not in OPEN_EXPOSURE_STATES],
                fwd_daily,
                float(self.config["account_envelope"]["starting_capital_inr"]),
                equity_curve=fwd_eq,
            ),
            "superseded_v2_0_replay": load_superseded_v2_0_evidence(),
            "archive_daily_comparison": daily_comp,
            "forward_daily_records": fwd_daily,
        }

    def get_counterfactuals(self) -> Dict[str, Any]:
        """Purely read-only counterfactual rows (`included_in_v2_totals = 0`)."""
        rows = self.store.load_counterfactuals()
        return {
            **summarize_counterfactual_rows(rows),
            "persisted_count": len(rows),
        }

    def get_replay_audit(self) -> Dict[str, Any]:
        """Purely read-only O3 audit, overshoot, causal archive proof & superseded disclosure."""
        v1_conn = open_v1_readonly_db()
        try:
            overshoot = reconcile_v1_book_b_stop_and_overshoot(v1_conn)
        finally:
            v1_conn.close()
        o3 = load_phase4_o3_audit_report()
        with self._op_lock:
            cache = self._archive_audit_cache
        rep_attr = (cache or {}).get("replay_and_attribution") or {}
        rep_daily = self.store.load_daily_snapshots(self.replay_run_mode, self.config_hash)
        return {
            "engine_version": self.engine_version,
            "replay_run_mode": self.replay_run_mode,
            "phase4_o3_audit": o3,
            "v1_book_b_overshoot_audit": overshoot,
            "causal_verification_outcome": rep_attr.get("causal_verification_outcome"),
            "archive_timing_evidence": rep_attr.get("archive_timing_evidence"),
            "superseded_v2_0_replay": rep_attr.get("superseded_v2_0_replay") or load_superseded_v2_0_evidence(),
            "ablation_study": rep_attr.get("ablation_study", []),
            "sensitivity": rep_attr.get("sensitivity", {}),
            "canonical_replay_sha256": (
                rep_attr.get("canonical_replay", {}).get("ledger_sha256")
                or self.store.compute_canonical_ledger_digest(self.replay_run_mode, self.config_hash)
            ),
            "persisted_replay_sessions": len(rep_daily),
            "archive_replay_populated": bool(rep_attr) or len(rep_daily) > 0,
        }

    def get_diagnostics(self) -> Dict[str, Any]:
        """Purely read-only isolation, entitlement field inventory, hashes, and activation guide."""
        current_hashes = compute_protected_hashes_readonly()
        hash_matches = all(
            current_hashes.get(k) == v for k, v in self.protected_baseline_hashes.items()
        )
        fwd_pkgs = self.store.load_packages("FORWARD_PAPER", self.config_hash)
        fwd_open = [p for p in fwd_pkgs if p["state"] in OPEN_EXPOSURE_STATES]
        hygiene = self._compute_feed_hygiene_snapshot()
        col_status, alerts = self._compute_collection_and_alerts(fwd_open, hygiene)
        shared_keys_count = 0
        try:
            mgr = self._stream_mgr_ref
            if mgr is not None and hasattr(mgr, "_key_to_contract"):
                shared_keys_count = len(getattr(mgr, "_key_to_contract", {}) or {})
        except Exception:
            shared_keys_count = 0
        return {
            "service_name": "V2 Option Sniffer",
            "protocol_id": self.config["protocol_id"],
            "engine_version": self.engine_version,
            "replay_run_mode": self.replay_run_mode,
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
            "collector_status": col_status,
            "active_alerts": alerts,
            "feed_hygiene": hygiene,
            "feed_architecture_comparison": evaluate_feed_architecture_options(
                health_metrics=hygiene.get("feed_consumer"),
                shared_subscribed_keys_count=shared_keys_count,
            ),
            "metric_corrections": self.store.load_metric_corrections("FORWARD_PAPER", self.config_hash),
            "recent_signals_forward": self.store.load_signals("FORWARD_PAPER", self.config_hash, limit=50),
            "recent_signals_replay": self.store.load_signals(self.replay_run_mode, self.config_hash, limit=50),
            "operator_activation_commands": {
                "restart_shared_backend_for_live_fanout": (
                    "Stop running uvicorn :8000 cleanly during non-market window, then restart: "
                    "cd backend && venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
                ),
                "detach_v2_only_without_stopping_v1": (
                    "POST /api/option-sniffer-advance-v2/control/shutdown-v2-only"
                ),
                "reconcile_booking_fault": (
                    "POST /api/option-sniffer-advance-v2/control/reconcile-booking-fault"
                ),
                "reproducible_cli_audit": (
                    "PYTHONPATH=backend /usr/bin/python3 -B -m "
                    "app.analytics.option_sniffer_v2.v2_reproducible_audit_cli"
                ),
            },
        }

    def get_opportunities(
        self,
        session_date: Optional[str] = None,
        book_id: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Strictly read-only endpoint returning accepted & rejected V2 gate evaluations without DB writes."""
        rows = self.store.load_opportunities(
            run_mode="FORWARD_PAPER",
            config_hash=self.config_hash,
            session_date=session_date,
            book_id=book_id,
            limit=limit,
        )
        by_status: Dict[str, int] = {}
        by_reason: Dict[str, int] = {}
        by_avail: Dict[str, int] = {}
        by_session: Dict[str, Dict[str, int]] = {}
        for r in rows:
            st = str(r.get("decision_status") or "UNKNOWN")
            by_status[st] = by_status.get(st, 0) + 1
            rsn = str(r.get("primary_reject_reason") or "ACCEPTED")
            by_reason[rsn] = by_reason.get(rsn, 0) + 1
            av = str(r.get("execution_availability") or "UNKNOWN")
            by_avail[av] = by_avail.get(av, 0) + 1
            dt_s = str(r.get("session_date") or "")
            sb = by_session.setdefault(dt_s, {"accepted": 0, "rejected": 0, "blocked": 0, "disabled": 0})
            k_s = st.lower()
            if k_s in sb:
                sb[k_s] += 1
        return {
            "engine_version": self.engine_version,
            "config_hash": self.config_hash,
            "db_exists": self.store.db_exists(),
            "count": len(rows),
            "summary_by_status": by_status,
            "summary_by_primary_reason": by_reason,
            "summary_by_execution_availability": by_avail,
            "summary_by_session": by_session,
            "rows": rows,
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
