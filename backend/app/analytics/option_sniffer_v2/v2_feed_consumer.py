"""
V2 Option Sniffer — Bounded Passive Feed Consumer & V2-Owned Subscription Registry.

Safeguards:
  - Uses passive fan-out on the shared `OptionStreamManager` (`_advance_feed_listeners`);
    never opens a second Upstox WebSocket or HTTP pool and never evicts V1 keys.
  - Bounded `collections.deque(maxlen=4096)` ingress ensures V2 can NEVER stall V1's tick loop.
  - Detects duplicate events, late/out-of-order events, 1m sequence/bar gaps, and queue overflow drops.
  - Separately tracks `v2_owned_only` WebSocket keys so V2 yields first under quota/subscription pressure
    and cleans up strictly its own pins on V2 shutdown.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import hashlib
import logging
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


def build_event_dedup_key(
    kind: str,
    instrument_key: str,
    exchange_ts_ms: int,
    payload: Dict[str, Any],
) -> str:
    """Stable deterministic event fingerprint to guarantee idempotent ingestion on replay/reconnect."""
    raw = (
        f"{kind}|{instrument_key}|{int(exchange_ts_ms)}|"
        f"{payload.get('ltp') or payload.get('spot_ltp') or payload.get('fut_ltp')}|"
        f"{payload.get('raw_bid') or payload.get('bid')}|"
        f"{payload.get('raw_ask') or payload.get('ask')}|"
        f"{payload.get('depth_timestamp_ms') or payload.get('l1_timestamp_ms') or payload.get('l1_book_timestamp_ms') or 0}|"
        f"{payload.get('volume') or payload.get('fut_volume_vtt') or 0}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class V2SubscriptionRegistry:
    """
    Manages additive instrument subscriptions owned strictly by V2 with reference counting,
    explicit V1 headroom reservation, and yield-first eviction under subscription pressure.
    """

    def __init__(
        self,
        ws_total_soft_cap: int = 1400,
        v1_reserved_headroom: int = 100,
        max_v2_additive_pins: int = 8,
    ) -> None:
        self._lock = threading.RLock()
        self.ws_total_soft_cap = int(ws_total_soft_cap)
        self.v1_reserved_headroom = int(v1_reserved_headroom)
        self.max_v2_additive_pins = int(max_v2_additive_pins)
        self._v2_refs: Dict[str, Set[str]] = {}  # ikey -> {package_id, ...}
        self._v2_owned_only_keys: Set[str] = set()
        self._evicted_v2_keys: Set[str] = set()
        self._pressure_yield_count: int = 0

    def snapshot_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "tracked_v2_keys": sorted(self._v2_refs.keys()),
                "v2_owned_only_keys": sorted(self._v2_owned_only_keys),
                "evicted_v2_keys": sorted(self._evicted_v2_keys),
                "pressure_yield_count": self._pressure_yield_count,
                "ws_total_soft_cap": self.ws_total_soft_cap,
                "v1_reserved_headroom": self.v1_reserved_headroom,
                "max_v2_additive_pins": self.max_v2_additive_pins,
            }

    def acquire_for_package(
        self,
        package_id: str,
        contracts: List[Dict[str, Any]],
        stream_manager: Any = None,
        existing_stream_keys_fn: Optional[Callable[[], Set[str]]] = None,
        subscribe_additive_fn: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Records V2 package reference counts for required keys. Uses single-lock atomic owner-aware
        acquisition on `stream_manager` when available, otherwise respects `existing_stream_keys_fn` headroom.
        """
        if stream_manager is not None and hasattr(stream_manager, "acquire_owned_pins"):
            res = stream_manager.acquire_owned_pins(
                owner="V2",
                contracts=contracts,
                max_owner_pins=self.max_v2_additive_pins,
                soft_cap=self.ws_total_soft_cap,
                v1_headroom=self.v1_reserved_headroom,
            )
            with self._lock:
                for c in contracts:
                    ik = str(c.get("instrument_key") or "").strip()
                    if not ik or ik in res.get("skipped_due_to_headroom", []):
                        continue
                    self._evicted_v2_keys.discard(ik)
                    self._v2_refs.setdefault(ik, set()).add(package_id)
                for ik in res.get("pinned_exclusively", []):
                    self._v2_owned_only_keys.add(ik)
            return {
                "pinned_v2_only": res.get("pinned_exclusively", []),
                "reused_v1_keys": res.get("reused_existing_keys", []),
                "skipped_due_to_v1_headroom": res.get("skipped_due_to_headroom", []),
            }

        existing = existing_stream_keys_fn() if existing_stream_keys_fn else set()
        to_pin: List[Dict[str, Any]] = []
        skipped_headroom: List[str] = []

        with self._lock:
            for c in contracts:
                ik = str(c.get("instrument_key") or "").strip()
                if not ik:
                    continue
                self._evicted_v2_keys.discard(ik)
                refs = self._v2_refs.setdefault(ik, set())
                refs.add(package_id)
                if ik in existing or ik in self._v2_owned_only_keys:
                    continue
                projected_total = len(existing) + len(self._v2_owned_only_keys) + len(to_pin) + 1
                if (
                    len(self._v2_owned_only_keys) + len(to_pin) >= self.max_v2_additive_pins
                    or projected_total + self.v1_reserved_headroom > self.ws_total_soft_cap
                ):
                    skipped_headroom.append(ik)
                    continue
                tagged = dict(c)
                tagged["v2_owned_only"] = True
                to_pin.append(tagged)

        if to_pin and subscribe_additive_fn is not None:
            subscribe_additive_fn(to_pin)
            with self._lock:
                for c in to_pin:
                    self._v2_owned_only_keys.add(str(c["instrument_key"]))

        return {
            "pinned_v2_only": [c["instrument_key"] for c in to_pin],
            "reused_v1_keys": [str(c.get("instrument_key")) for c in contracts if str(c.get("instrument_key")) in existing],
            "skipped_due_to_v1_headroom": skipped_headroom,
        }

    def release_for_package(
        self,
        package_id: str,
        stream_manager: Any = None,
        unpin_v2_only_fn: Optional[Callable[[List[str]], None]] = None,
    ) -> List[str]:
        """Atomically releases V2 package refs; never strips V1 protection or V1-acquired keys."""
        to_release_candidates: List[str] = []
        released: List[str] = []
        with self._lock:
            empty_keys: List[str] = []
            for ik, pkgs in self._v2_refs.items():
                pkgs.discard(package_id)
                if not pkgs:
                    empty_keys.append(ik)
            for ik in empty_keys:
                self._v2_refs.pop(ik, None)
                to_release_candidates.append(ik)
                if ik in self._v2_owned_only_keys:
                    self._v2_owned_only_keys.discard(ik)
                    released.append(ik)

        if stream_manager is not None and hasattr(stream_manager, "release_owned_pins") and to_release_candidates:
            res = stream_manager.release_owned_pins("V2", to_release_candidates)
            return list(res.get("released_from_stream", []))

        if released and unpin_v2_only_fn is not None:
            unpin_v2_only_fn(released)
        return released

    def yield_under_pressure(
        self,
        stream_manager: Any = None,
        unpin_v2_only_fn: Optional[Callable[[List[str]], None]] = None,
        v1_active_keys: Optional[Set[str]] = None,
    ) -> List[str]:
        """
        V2 yields first under subscription or limiter pressure: drops ONLY exclusively V2-owned keys
        not needed by V1, and records evicted keys so open V2 packages flag unpriced exposure.
        """
        if stream_manager is not None and hasattr(stream_manager, "yield_owner_pins_under_pressure"):
            victims = list(stream_manager.yield_owner_pins_under_pressure("V2"))
            with self._lock:
                for ik in victims:
                    self._v2_owned_only_keys.discard(ik)
                    if ik in self._v2_refs:
                        self._evicted_v2_keys.add(ik)
                if victims:
                    self._pressure_yield_count += 1
            return victims

        v1_keys = v1_active_keys or set()
        with self._lock:
            victims = sorted(ik for ik in self._v2_owned_only_keys if ik not in v1_keys)
            for ik in victims:
                self._v2_owned_only_keys.discard(ik)
                if ik in self._v2_refs:
                    self._evicted_v2_keys.add(ik)
            if victims:
                self._pressure_yield_count += 1
        if victims and unpin_v2_only_fn is not None:
            unpin_v2_only_fn(victims)
        return victims

    def is_key_evicted(self, instrument_key: str) -> bool:
        with self._lock:
            return str(instrument_key) in self._evicted_v2_keys

    def release_all_v2_owned(
        self,
        stream_manager: Any = None,
        unpin_v2_only_fn: Optional[Callable[[List[str]], None]] = None,
    ) -> List[str]:
        """Clean V2-only shutdown helper: releases strictly V2-owned pins, never V1 pins."""
        if stream_manager is not None and hasattr(stream_manager, "release_owned_pins"):
            res = stream_manager.release_owned_pins("V2", None)
            with self._lock:
                self._v2_owned_only_keys.clear()
                self._v2_refs.clear()
            return list(res.get("released_from_stream", []))

        with self._lock:
            keys = sorted(self._v2_owned_only_keys)
            self._v2_owned_only_keys.clear()
            self._v2_refs.clear()
        if keys and unpin_v2_only_fn is not None:
            unpin_v2_only_fn(keys)
        return keys


class V2BoundedFeedConsumer:
    """
    Bounded passive event queue consuming Upstox V3 feed events or archived events without
    blocking the producer thread.
    """

    def __init__(self, config_hash: str, maxlen: int = 4096) -> None:
        self.config_hash = str(config_hash)
        self.maxlen = max(64, int(maxlen))
        self._lock = threading.Lock()
        self._queue: Deque[Dict[str, Any]] = deque()
        self._wake_event = threading.Event()

        # Sequence / dedup / lag telemetry
        self._recent_dedup_keys: Set[str] = set()
        self._dedup_fifo: Deque[str] = deque(maxlen=16384)
        self._max_exchange_ts_by_key: Dict[str, int] = {}
        self._last_spot_bar_minute: Optional[str] = None
        self._enqueued_count: int = 0
        self._processed_count: int = 0
        self._duplicate_count: int = 0
        self._late_count: int = 0
        self._gap_count: int = 0
        self._dropped_overflow_count: int = 0
        self._uncertainty_reasons: Set[str] = set()
        self._max_queue_depth_seen: int = 0
        self._lag_samples_ms: Deque[float] = deque(maxlen=2000)

    def push_events(
        self,
        events: List[Tuple[str, str, Dict[str, Any]]],
        receipt_ts_ms: Optional[int] = None,
    ) -> int:
        """
        Non-blocking O(k) ingress hook compatible with `OptionStreamManager._advance_feed_listeners`.
        Drops oldest queued items ONLY on extreme saturation (`len >= maxlen`) and raises explicit
        `UNCERTAINTY_QUEUE_OVERFLOW` rather than blocking the live Upstox WebSocket thread.
        """
        if not events:
            return 0
        now_ms = int(receipt_ts_ms if receipt_ts_ms is not None else time.time() * 1000)
        accepted = 0
        with self._lock:
            for kind, key_or_sym, payload in events:
                if not isinstance(payload, dict):
                    continue
                ikey = str(payload.get("instrument_key") or key_or_sym or "").strip()
                ex_ts = int(
                    payload.get("exchange_ltt_ms")
                    or payload.get("l1_book_timestamp_ms")
                    or payload.get("timestamp_ms")
                    or now_ms
                )
                dedup_k = build_event_dedup_key(str(kind), ikey, ex_ts, payload)
                if dedup_k in self._recent_dedup_keys:
                    self._duplicate_count += 1
                    continue

                if len(self._dedup_fifo) == self._dedup_fifo.maxlen:
                    evicted = self._dedup_fifo.popleft()
                    self._recent_dedup_keys.discard(evicted)
                self._dedup_fifo.append(dedup_k)
                self._recent_dedup_keys.add(dedup_k)

                prev_ex = self._max_exchange_ts_by_key.get(ikey, 0)
                seq_flag = "OK"
                if prev_ex > 0 and ex_ts < prev_ex:
                    self._late_count += 1
                    seq_flag = "LATE_OUT_OF_ORDER"
                else:
                    self._max_exchange_ts_by_key[ikey] = max(prev_ex, ex_ts)

                if len(self._queue) >= self.maxlen:
                    self._queue.popleft()
                    self._dropped_overflow_count += 1
                    self._uncertainty_reasons.add("UNCERTAINTY_QUEUE_OVERFLOW")

                avail_ms = int(payload.get("availability_ts_ms") or now_ms)
                self._queue.append(
                    {
                        "dedup_key": dedup_k,
                        "kind": str(kind),
                        "symbol_or_key": str(key_or_sym),
                        "instrument_key": ikey,
                        "exchange_ts_ms": ex_ts,
                        "receipt_ts_ms": now_ms,
                        "availability_ts_ms": avail_ms,
                        "enqueued_perf_ns": time.perf_counter_ns(),
                        "sequence_flag": seq_flag,
                        "config_hash": self.config_hash,
                        "payload": dict(payload),
                    }
                )
                accepted += 1
                self._enqueued_count += 1
            if len(self._queue) > self._max_queue_depth_seen:
                self._max_queue_depth_seen = len(self._queue)
        if accepted:
            self._wake_event.set()
        return accepted

    def drain_batch(self, max_items: int = 500) -> List[Dict[str, Any]]:
        """Pops up to `max_items` queued envelopes in FIFO order for downstream V2 processing."""
        batch: List[Dict[str, Any]] = []
        now_ns = time.perf_counter_ns()
        with self._lock:
            while self._queue and len(batch) < max_items:
                item = self._queue.popleft()
                lag_ms = max(0.0, (now_ns - item["enqueued_perf_ns"]) / 1_000_000.0)
                self._lag_samples_ms.append(lag_ms)
                self._processed_count += 1
                batch.append(item)
            if not self._queue:
                self._wake_event.clear()
        return batch

    def check_minute_bar_continuity(self, bar_ts: str) -> bool:
        """
        Detects missing >1 minute gaps inside continuous 09:15-15:30 trading sessions.
        Returns True if continuous (or first bar of session), False if gap detected.
        """
        cur_dt = datetime.strptime(bar_ts[:19], "%Y-%m-%d %H:%M:%S")
        with self._lock:
            prev = self._last_spot_bar_minute
            self._last_spot_bar_minute = bar_ts[:19]
            if prev is None or prev[:10] != bar_ts[:10]:
                return True
            prev_dt = datetime.strptime(prev, "%Y-%m-%d %H:%M:%S")
            delta_sec = int((cur_dt - prev_dt).total_seconds())
            if delta_sec > 60:
                self._gap_count += 1
                self._uncertainty_reasons.add(f"MINUTE_BAR_GAP_{delta_sec}S")
                return False
            return True

    def clear_uncertainty(self, reason_prefix: Optional[str] = None) -> None:
        with self._lock:
            if reason_prefix is None:
                self._uncertainty_reasons.clear()
            else:
                self._uncertainty_reasons = {
                    r for r in self._uncertainty_reasons if not r.startswith(reason_prefix)
                }

    def get_health_metrics(self) -> Dict[str, Any]:
        with self._lock:
            lags = sorted(self._lag_samples_ms)
            p50 = lags[len(lags) // 2] if lags else 0.0
            p99 = lags[min(len(lags) - 1, int(0.99 * len(lags)))] if lags else 0.0
            return {
                "queue_capacity": self.maxlen,
                "current_queue_depth": len(self._queue),
                "max_queue_depth_seen": self._max_queue_depth_seen,
                "enqueued_total": self._enqueued_count,
                "processed_total": self._processed_count,
                "duplicate_events_suppressed": self._duplicate_count,
                "late_out_of_order_events": self._late_count,
                "sequence_bar_gaps": self._gap_count,
                "dropped_overflow_events": self._dropped_overflow_count,
                "queue_lag_p50_ms": round(p50, 4),
                "queue_lag_p99_ms": round(p99, 4),
                "uncertainty_blocks": sorted(self._uncertainty_reasons),
            }
