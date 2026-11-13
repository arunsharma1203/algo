"""
S1 Prospective Data-Recording Programme — Phase 1: Measurement-Only Ingress Probe.

Provides minimal, bounded, default-OFF, zero-IO instrumentation at the existing
Upstox V3 SDK market-data ingress (`MarketDataStreamerV3.handle_message`) and
shared `UpstoxStreamService` dispatch path.

Strict Safety Guarantees:
  1. Default-OFF (`enabled = False` unless explicitly toggled at runtime or env-configured).
  2. Independently enableable/disableable at runtime WITHOUT restarting or reconnecting
     the Upstox WebSocket stream, altering subscriptions, or issuing any HTTP/broker calls.
  3. Zero synchronous disk, SQLite, network, or logging I/O inside the receive callback.
  4. Strictly bounded memory (fixed-bucket histograms + fixed-maxlen circular deques +
     capped seen-key set). Does NOT store a raw market-data archive.
  5. Fail-open execution: any internal probe fault increments explicit failure counters
     and auto-disables the probe after repeated consecutive faults without ever raising
     into SDK message decoding or trading listeners.
  6. Zero modification to S2_05 models, strategy logic, normalization, timestamps, or
     live/paper execution controls.
"""

import bisect
import collections
import datetime
import math
import os
import threading
import time
from typing import Any, Callable, Deque, Dict, FrozenSet, List, Optional, Set, Tuple

# Canonical Upstox V3 instrument keys for S1 core dependencies
S1_CORE_INSTRUMENT_KEYS: Dict[str, str] = {
    "NSE_INDEX|Nifty 50": "NIFTY",
    "NSE_INDEX|India VIX": "INDIAVIX",
    "NSE_EQ|INE002A01018": "RELIANCE",
    "NSE_EQ|INE040A01034": "HDFCBANK",
    "NSE_EQ|INE090A01021": "ICICIBANK",
}

COHORT_NAMES: Tuple[str, ...] = (
    "NIFTY",
    "INDIAVIX",
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "NIFTY_OPTIONS_1_7_DTE",
    "NIFTY_OPTIONS_OTHER_DTE",
    "NIFTY_FUTURES",
    "NON_S1_UNIVERSE",
)

S1_RELEVANT_COHORTS: FrozenSet[str] = frozenset({
    "NIFTY",
    "INDIAVIX",
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "NIFTY_OPTIONS_1_7_DTE",
    "NIFTY_OPTIONS_OTHER_DTE",
    "NIFTY_FUTURES",
})

# Bounded ring and set limits
RING_100MS_MAXLEN: int = 600          # 60s of non-empty 100ms micro-burst slots
RING_1S_MAXLEN: int = 900             # 15m of 1-second rate slots
RING_1M_MAXLEN: int = 480             # 8h of 1-minute session rollup slots
LIFECYCLE_RING_MAXLEN: int = 64       # Recent WS open/close/error/reconnect events
MAX_TRACKED_SEEN_KEYS: int = 4096     # Hard cap on unique instrument key set (~650 expected)
MAX_CONSECUTIVE_PROBE_FAULTS: int = 5 # Auto-disable threshold on internal probe exceptions

# Fixed histogram upper bounds
LATENCY_BOUNDS_US: Tuple[float, ...] = (
    1.0, 2.0, 5.0, 10.0, 20.0, 35.0, 50.0, 75.0, 100.0,
    150.0, 200.0, 300.0, 500.0, 750.0, 1000.0,
    1500.0, 2000.0, 3000.0, 5000.0, 7500.0, 10000.0,
    15000.0, 20000.0, 30000.0, 50000.0, 75000.0, 100000.0,
    250000.0, 500000.0, 1000000.0,
)

FRAME_SIZE_BOUNDS_BYTES: Tuple[float, ...] = (
    64.0, 128.0, 256.0, 512.0, 1024.0, 2048.0, 4096.0, 8192.0,
    16384.0, 32768.0, 65536.0, 131072.0, 262144.0, 524288.0, 1048576.0,
)

INSTRUMENTS_PER_FRAME_BOUNDS: Tuple[float, ...] = (
    0.0, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 384.0, 512.0, 640.0, 1024.0,
)

MSG_RATE_BOUNDS_PER_SEC: Tuple[float, ...] = (
    1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 200.0, 350.0, 500.0,
    750.0, 1000.0, 1500.0, 2000.0, 3000.0, 5000.0, 10000.0,
)

BYTE_RATE_BOUNDS_PER_SEC: Tuple[float, ...] = (
    1024.0, 4096.0, 16384.0, 65536.0, 131072.0, 262144.0, 524288.0,
    1048576.0, 2097152.0, 4194304.0, 8388608.0, 16777216.0, 33554432.0, 67108864.0,
)

CLOCK_LAG_BOUNDS_MS: Tuple[float, ...] = (
    -5000.0, -1000.0, -250.0, -50.0, 0.0, 10.0, 25.0, 50.0,
    100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 15000.0, 60000.0,
)

QUOTE_EXCHANGE_TS_CANDIDATE_KEYS: Tuple[str, ...] = (
    "ts", "timestamp", "ltt", "exchangeTs", "quoteTs", "bookTs",
)


class BoundedHistogram:
    """
    Zero-allocation fixed-bucket histogram with exact count, sum, min, max,
    and O(B) quantile interpolation over preallocated integer bins.
    """

    __slots__ = ("_bounds", "_counts", "_count", "_sum", "_min", "_max")

    def __init__(self, bounds: Tuple[float, ...]) -> None:
        self._bounds: Tuple[float, ...] = bounds
        self._counts: List[int] = [0] * (len(bounds) + 1)
        self._count: int = 0
        self._sum: float = 0.0
        self._min: float = math.inf
        self._max: float = -math.inf

    def observe(self, value: float) -> None:
        idx = bisect.bisect_left(self._bounds, value)
        self._counts[idx] += 1
        self._count += 1
        self._sum += value
        if value < self._min:
            self._min = value
        if value > self._max:
            self._max = value

    def reset(self) -> None:
        for i in range(len(self._counts)):
            self._counts[i] = 0
        self._count = 0
        self._sum = 0.0
        self._min = math.inf
        self._max = -math.inf

    @property
    def count(self) -> int:
        return self._count

    def quantile(self, q: float) -> float:
        if self._count == 0:
            return 0.0
        if q <= 0.0:
            return self._min
        if q >= 1.0:
            return self._max
        target = q * self._count
        cum = 0
        bounds = self._bounds
        n_bounds = len(bounds)
        for i, c in enumerate(self._counts):
            if c == 0:
                continue
            prev_cum = cum
            cum += c
            if cum >= target:
                if i == 0:
                    low = min(self._min, bounds[0])
                    high = min(self._max, bounds[0])
                elif i < n_bounds:
                    low = max(self._min, bounds[i - 1])
                    high = min(self._max, bounds[i])
                else:
                    low = max(self._min, bounds[-1])
                    high = self._max
                if high <= low or c <= 1:
                    return float(high)
                frac = (target - prev_cum) / float(c)
                return float(low + frac * (high - low))
        return float(self._max)

    def fraction_le(self, threshold: float) -> float:
        if self._count == 0:
            return 1.0
        idx = bisect.bisect_left(self._bounds, threshold)
        if idx < len(self._bounds) and self._bounds[idx] == threshold:
            le_count = sum(self._counts[: idx + 1])
        else:
            le_count = sum(self._counts[:idx])
        return round(le_count / float(self._count), 6)

    def summary(self, decimals: int = 2) -> Dict[str, Any]:
        if self._count == 0:
            return {
                "count": 0,
                "min": 0.0,
                "mean": 0.0,
                "p50": 0.0,
                "p90": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "p999": 0.0,
                "max": 0.0,
            }
        return {
            "count": self._count,
            "min": round(self._min, decimals),
            "mean": round(self._sum / float(self._count), decimals),
            "p50": round(self.quantile(0.50), decimals),
            "p90": round(self.quantile(0.90), decimals),
            "p95": round(self.quantile(0.95), decimals),
            "p99": round(self.quantile(0.99), decimals),
            "p999": round(self.quantile(0.999), decimals),
            "max": round(self._max, decimals),
        }


class CohortCounters:
    """Fixed-schema counters for an instrument cohort (zero dynamic keys)."""

    __slots__ = (
        "updates",
        "ltpc_updates",
        "book_updates",
        "book_2sided_qty_updates",
        "book_l5_updates",
        "book_only_no_ltp_updates",
        "book_invalid_or_crossed_updates",
        "oi_updates",
        "greeks_updates",
        "i1_candle_updates",
        "quote_exchange_ts_updates",
        "first_seen_ms",
        "last_seen_ms",
        "max_inter_update_gap_ms",
    )

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.updates: int = 0
        self.ltpc_updates: int = 0
        self.book_updates: int = 0
        self.book_2sided_qty_updates: int = 0
        self.book_l5_updates: int = 0
        self.book_only_no_ltp_updates: int = 0
        self.book_invalid_or_crossed_updates: int = 0
        self.oi_updates: int = 0
        self.greeks_updates: int = 0
        self.i1_candle_updates: int = 0
        self.quote_exchange_ts_updates: int = 0
        self.first_seen_ms: int = 0
        self.last_seen_ms: int = 0
        self.max_inter_update_gap_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "updates": self.updates,
            "ltpc_updates": self.ltpc_updates,
            "book_updates": self.book_updates,
            "book_2sided_qty_updates": self.book_2sided_qty_updates,
            "book_l5_updates": self.book_l5_updates,
            "book_only_no_ltp_updates": self.book_only_no_ltp_updates,
            "book_invalid_or_crossed_updates": self.book_invalid_or_crossed_updates,
            "oi_updates": self.oi_updates,
            "greeks_updates": self.greeks_updates,
            "i1_candle_updates": self.i1_candle_updates,
            "quote_exchange_ts_updates": self.quote_exchange_ts_updates,
            "book_bearing_ratio": round(self.book_updates / float(self.updates), 6) if self.updates > 0 else 0.0,
            "first_seen_ms": self.first_seen_ms or None,
            "last_seen_ms": self.last_seen_ms or None,
            "max_inter_update_gap_ms": self.max_inter_update_gap_ms,
        }


class S1IngressMeasurementProbe:
    """
    Bounded in-memory measurement probe for Phase 1 of the S1 prospective
    data-recording programme.

    Default state is strictly DISABLED (`enabled = False`).
    """

    def __init__(self, default_enabled: Optional[bool] = None) -> None:
        if default_enabled is None:
            env_val = os.environ.get("S1_INGRESS_PROBE_ENABLED", "").strip().lower()
            default_enabled = env_val in ("1", "true", "yes")

        self._lock = threading.RLock()
        self._enabled: bool = bool(default_enabled)
        self._universe_supplier: Optional[Callable[[], Tuple[Set[str], Dict[str, Dict[str, Any]]]]] = None
        self._last_universe_refresh_ns: int = 0
        self._universe_refresh_interval_ns: int = 5_000_000_000  # 5.0 seconds

        # Cached read-only universe classification maps
        self._subscribed_keys_snapshot: Set[str] = set()
        self._nifty_opt_1_7_keys: Set[str] = set()
        self._nifty_opt_other_keys: Set[str] = set()
        self._nifty_futures_keys: Set[str] = set()

        self._init_counters()

    def _init_counters(self) -> None:
        now_ms = int(time.time() * 1000)
        self._created_or_reset_ms: int = now_ms
        self._enabled_since_ms: Optional[int] = now_ms if self._enabled else None
        self._disabled_at_ms: Optional[int] = None
        self._first_frame_ms: Optional[int] = None
        self._last_frame_ms: Optional[int] = None
        self._first_frame_mono_ns: int = 0
        self._last_frame_mono_ns: int = 0

        # Frame & byte totals
        self._total_frames: int = 0
        self._total_raw_bytes: int = 0
        self._total_instrument_updates: int = 0
        self._s1_relevant_frames: int = 0
        self._s1_relevant_updates: int = 0
        self._s1_book_bearing_updates: int = 0
        self._frames_gt_64kib: int = 0
        self._frames_gt_1mib: int = 0
        self._frames_with_current_ts: int = 0
        self._frames_missing_current_ts: int = 0

        # Envelope type breakdown (bounded fixed keys + 'other')
        self._envelope_type_counts: Dict[str, int] = {
            "live_feed": 0,
            "initial_feed": 0,
            "market_info": 0,
            "missing": 0,
            "other": 0,
        }

        # Explicit failure / drop / eviction counters
        self._non_bytes_payload_count: int = 0
        self._sdk_decode_errors: int = 0
        self._sdk_dict_errors: int = 0
        self._downstream_emit_errors: int = 0
        self._probe_internal_errors: int = 0
        self._consecutive_probe_faults: int = 0
        self._auto_disabled_on_fault: bool = False
        self._seen_key_cap_drops: int = 0
        self._ring_100ms_evictions: int = 0
        self._ring_1s_evictions: int = 0
        self._ring_1m_evictions: int = 0
        self._lifecycle_ring_evictions: int = 0

        # Connection / reconnect telemetry
        self._ws_open_count: int = 0
        self._ws_close_count: int = 0
        self._ws_error_count: int = 0
        self._ws_reconnecting_count: int = 0
        self._ws_auto_reconnect_stopped_count: int = 0
        self._last_disconnect_mono_ns: int = 0
        self._max_disconnect_gap_ms: float = 0.0
        self._lifecycle_events: Deque[Dict[str, Any]] = collections.deque(maxlen=LIFECYCLE_RING_MAXLEN)

        # Bounded unique key delivery sets
        self._seen_all_keys: Set[str] = set()
        self._seen_s1_core_keys: Set[str] = set()
        self._seen_nifty_opt_1_7_keys: Set[str] = set()
        self._seen_nifty_opt_other_keys: Set[str] = set()
        self._seen_nifty_opt_book_keys: Set[str] = set()

        # Per-cohort counters
        self._cohorts: Dict[str, CohortCounters] = {name: CohortCounters() for name in COHORT_NAMES}

        # Fixed-bucket distributions
        self._hist_frame_bytes = BoundedHistogram(FRAME_SIZE_BOUNDS_BYTES)
        self._hist_instruments_per_frame = BoundedHistogram(INSTRUMENTS_PER_FRAME_BOUNDS)
        self._hist_provider_clock_lag_ms = BoundedHistogram(CLOCK_LAG_BOUNDS_MS)
        self._hist_100ms_burst_frames = BoundedHistogram(MSG_RATE_BOUNDS_PER_SEC)
        self._hist_100ms_burst_bytes = BoundedHistogram(BYTE_RATE_BOUNDS_PER_SEC)
        self._hist_1s_frames = BoundedHistogram(MSG_RATE_BOUNDS_PER_SEC)
        self._hist_1s_bytes = BoundedHistogram(BYTE_RATE_BOUNDS_PER_SEC)

        # Callback & downstream latency histograms (microseconds)
        self._hist_sdk_decode_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._hist_sdk_dict_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._hist_downstream_dispatch_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._hist_listener_broadcast_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._hist_equity_ingest_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._hist_probe_self_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._hist_total_callback_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._hist_bar_flush_15m_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._hist_freeze_snapshot_us = BoundedHistogram(LATENCY_BOUNDS_US)

        # Rolling window accumulators (100ms, 1s, 1m)
        self._cur_100ms_slot: int = -1
        self._cur_100ms_frames: int = 0
        self._cur_100ms_bytes: int = 0
        self._cur_100ms_insts: int = 0
        self._cur_100ms_s1_book: int = 0

        self._peak_100ms_frames: int = 0
        self._peak_100ms_bytes: int = 0
        self._peak_100ms_insts: int = 0
        self._peak_100ms_s1_book: int = 0
        self._ring_100ms: Deque[Tuple[int, int, int, int, int]] = collections.deque(maxlen=RING_100MS_MAXLEN)

        self._cur_1s_slot: int = -1
        self._cur_1s_frames: int = 0
        self._cur_1s_bytes: int = 0
        self._cur_1s_insts: int = 0
        self._cur_1s_s1_updates: int = 0
        self._cur_1s_s1_book: int = 0

        self._peak_1s_frames: int = 0
        self._peak_1s_bytes: int = 0
        self._peak_1s_insts: int = 0
        self._peak_1s_s1_updates: int = 0
        self._peak_1s_s1_book: int = 0
        self._ring_1s: Deque[Tuple[int, int, int, int, int, int]] = collections.deque(maxlen=RING_1S_MAXLEN)

        self._cur_1m_slot: int = -1
        self._cur_1m_frames: int = 0
        self._cur_1m_bytes: int = 0
        self._cur_1m_insts: int = 0
        self._cur_1m_s1_updates: int = 0
        self._cur_1m_s1_book: int = 0
        self._ring_1m: Deque[Dict[str, int]] = collections.deque(maxlen=RING_1M_MAXLEN)

    # ──────────────────────────────────────────────────────────────────────────
    # RUNTIME CONTROL (NO STREAM RESTART REQUIRED)
    # ──────────────────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self, reset_counters: bool = False) -> Dict[str, Any]:
        with self._lock:
            if reset_counters:
                self._enabled = True
                self._init_counters()
            else:
                self._enabled = True
                self._auto_disabled_on_fault = False
                self._consecutive_probe_faults = 0
                if self._enabled_since_ms is None:
                    self._enabled_since_ms = int(time.time() * 1000)
                self._refresh_universe_snapshot_unlocked(force=True)
            return {"enabled": self._enabled, "enabled_since_ms": self._enabled_since_ms}

    def disable(self) -> Dict[str, Any]:
        with self._lock:
            self._enabled = False
            self._disabled_at_ms = int(time.time() * 1000)
            return {"enabled": self._enabled, "disabled_at_ms": self._disabled_at_ms}

    def reset(self) -> Dict[str, Any]:
        with self._lock:
            was_enabled = self._enabled
            self._init_counters()
            self._enabled = was_enabled
            if was_enabled:
                self._enabled_since_ms = int(time.time() * 1000)
                self._refresh_universe_snapshot_unlocked(force=True)
            return {"enabled": self._enabled, "reset_at_ms": self._created_or_reset_ms}

    def set_universe_supplier(
        self,
        supplier: Callable[[], Tuple[Set[str], Dict[str, Dict[str, Any]]]],
    ) -> None:
        self._universe_supplier = supplier

    @staticmethod
    def _classify_dte_days(contract_meta: Dict[str, Any], today_date: datetime.date) -> Optional[int]:
        raw_dte = contract_meta.get("dte")
        if raw_dte is not None:
            try:
                return int(math.ceil(float(raw_dte)))
            except (ValueError, TypeError):
                pass
        exp_raw = str(contract_meta.get("expiry") or "").strip()
        if len(exp_raw) >= 10:
            try:
                exp_date = datetime.date.fromisoformat(exp_raw[:10])
                return (exp_date - today_date).days
            except ValueError:
                return None
        return None

    def _refresh_universe_snapshot_unlocked(self, force: bool = False) -> None:
        supplier = self._universe_supplier
        if supplier is None:
            return
        now_ns = time.perf_counter_ns()
        if not force and (now_ns - self._last_universe_refresh_ns) < self._universe_refresh_interval_ns:
            return
        self._last_universe_refresh_ns = now_ns
        try:
            subscribed_keys, key_to_contract = supplier()
            today_d = datetime.date.today()
            opt_1_7: Set[str] = set()
            opt_other: Set[str] = set()
            fut_keys: Set[str] = set()
            for ikey, meta in (key_to_contract or {}).items():
                if not isinstance(meta, dict):
                    continue
                if meta.get("is_underlying"):
                    continue
                underlying = str(meta.get("underlying") or "").upper()
                if underlying != "NIFTY":
                    continue
                if meta.get("is_futures"):
                    fut_keys.add(ikey)
                    continue
                dte_days = self._classify_dte_days(meta, today_d)
                if dte_days is not None and 1 <= dte_days <= 7:
                    opt_1_7.add(ikey)
                else:
                    opt_other.add(ikey)
            self._subscribed_keys_snapshot = set(subscribed_keys or ())
            self._nifty_opt_1_7_keys = opt_1_7
            self._nifty_opt_other_keys = opt_other
            self._nifty_futures_keys = fut_keys
        except Exception:
            self._probe_internal_errors += 1

    # ──────────────────────────────────────────────────────────────────────────
    # LIFECYCLE & FAILURE RECORDING
    # ──────────────────────────────────────────────────────────────────────────

    def record_sdk_error(self, stage: str) -> None:
        if not self._enabled:
            return
        try:
            with self._lock:
                if stage == "decode_protobuf":
                    self._sdk_decode_errors += 1
                elif stage == "message_to_dict":
                    self._sdk_dict_errors += 1
                elif stage == "emit_message":
                    self._downstream_emit_errors += 1
        except Exception:
            pass

    def record_ws_lifecycle(self, event: str, detail: Optional[str] = None) -> None:
        if not self._enabled:
            return
        try:
            mono_ns = time.perf_counter_ns()
            wall_ms = int(time.time() * 1000)
            with self._lock:
                if event == "open":
                    self._ws_open_count += 1
                    if self._last_disconnect_mono_ns > 0:
                        gap_ms = (mono_ns - self._last_disconnect_mono_ns) / 1_000_000.0
                        if gap_ms > self._max_disconnect_gap_ms:
                            self._max_disconnect_gap_ms = gap_ms
                        self._last_disconnect_mono_ns = 0
                elif event == "close":
                    self._ws_close_count += 1
                    if self._last_disconnect_mono_ns == 0:
                        self._last_disconnect_mono_ns = mono_ns
                elif event == "error":
                    self._ws_error_count += 1
                elif event == "reconnecting":
                    self._ws_reconnecting_count += 1
                    if self._last_disconnect_mono_ns == 0:
                        self._last_disconnect_mono_ns = mono_ns
                elif event == "autoReconnectStopped":
                    self._ws_auto_reconnect_stopped_count += 1

                if len(self._lifecycle_events) == LIFECYCLE_RING_MAXLEN:
                    self._lifecycle_ring_evictions += 1
                self._lifecycle_events.append({
                    "ts_ms": wall_ms,
                    "event": event,
                    "detail": (str(detail)[:160] if detail is not None else None),
                })
        except Exception:
            self._probe_internal_errors += 1

    def record_downstream_segments(self, listener_ns: int, equity_loop_ns: int) -> None:
        if not self._enabled:
            return
        try:
            with self._lock:
                if listener_ns >= 0:
                    self._hist_listener_broadcast_us.observe(listener_ns / 1000.0)
                if equity_loop_ns >= 0:
                    self._hist_equity_ingest_us.observe(equity_loop_ns / 1000.0)
        except Exception:
            self._probe_internal_errors += 1

    def record_periodic_decision_stage(self, stage: str, duration_ns: int) -> None:
        if not self._enabled or duration_ns < 0:
            return
        try:
            dur_us = duration_ns / 1000.0
            with self._lock:
                if stage == "bar_flush_15m":
                    self._hist_bar_flush_15m_us.observe(dur_us)
                elif stage == "freeze_snapshot":
                    self._hist_freeze_snapshot_us.observe(dur_us)
        except Exception:
            self._probe_internal_errors += 1

    # ──────────────────────────────────────────────────────────────────────────
    # HOT-PATH INGRESS FRAME RECORDING (BOUNDED, ZERO I/O, FAIL-OPEN)
    # ──────────────────────────────────────────────────────────────────────────

    def _roll_windows_unlocked(
        self,
        slot_100ms: int,
        slot_1s: int,
        slot_1m: int,
        raw_bytes: int,
        n_insts: int,
        frame_s1_updates: int,
        frame_s1_book: int,
    ) -> None:
        # 100ms micro-burst bucket
        if self._cur_100ms_slot != slot_100ms:
            if self._cur_100ms_slot >= 0 and self._cur_100ms_frames > 0:
                f100 = self._cur_100ms_frames
                b100 = self._cur_100ms_bytes
                i100 = self._cur_100ms_insts
                sb100 = self._cur_100ms_s1_book
                if f100 > self._peak_100ms_frames:
                    self._peak_100ms_frames = f100
                if b100 > self._peak_100ms_bytes:
                    self._peak_100ms_bytes = b100
                if i100 > self._peak_100ms_insts:
                    self._peak_100ms_insts = i100
                if sb100 > self._peak_100ms_s1_book:
                    self._peak_100ms_s1_book = sb100
                self._hist_100ms_burst_frames.observe(float(f100))
                self._hist_100ms_burst_bytes.observe(float(b100))
                if len(self._ring_100ms) == RING_100MS_MAXLEN:
                    self._ring_100ms_evictions += 1
                self._ring_100ms.append((self._cur_100ms_slot, f100, b100, i100, sb100))
            self._cur_100ms_slot = slot_100ms
            self._cur_100ms_frames = 0
            self._cur_100ms_bytes = 0
            self._cur_100ms_insts = 0
            self._cur_100ms_s1_book = 0

        self._cur_100ms_frames += 1
        self._cur_100ms_bytes += raw_bytes
        self._cur_100ms_insts += n_insts
        self._cur_100ms_s1_book += frame_s1_book
        if self._cur_100ms_frames > self._peak_100ms_frames:
            self._peak_100ms_frames = self._cur_100ms_frames
        if self._cur_100ms_bytes > self._peak_100ms_bytes:
            self._peak_100ms_bytes = self._cur_100ms_bytes
        if self._cur_100ms_insts > self._peak_100ms_insts:
            self._peak_100ms_insts = self._cur_100ms_insts
        if self._cur_100ms_s1_book > self._peak_100ms_s1_book:
            self._peak_100ms_s1_book = self._cur_100ms_s1_book

        # 1-second rate bucket
        if self._cur_1s_slot != slot_1s:
            if self._cur_1s_slot >= 0 and self._cur_1s_frames > 0:
                f1 = self._cur_1s_frames
                b1 = self._cur_1s_bytes
                i1 = self._cur_1s_insts
                su1 = self._cur_1s_s1_updates
                sb1 = self._cur_1s_s1_book
                if f1 > self._peak_1s_frames:
                    self._peak_1s_frames = f1
                if b1 > self._peak_1s_bytes:
                    self._peak_1s_bytes = b1
                if i1 > self._peak_1s_insts:
                    self._peak_1s_insts = i1
                if su1 > self._peak_1s_s1_updates:
                    self._peak_1s_s1_updates = su1
                if sb1 > self._peak_1s_s1_book:
                    self._peak_1s_s1_book = sb1
                self._hist_1s_frames.observe(float(f1))
                self._hist_1s_bytes.observe(float(b1))
                if len(self._ring_1s) == RING_1S_MAXLEN:
                    self._ring_1s_evictions += 1
                self._ring_1s.append((self._cur_1s_slot, f1, b1, i1, su1, sb1))
            self._cur_1s_slot = slot_1s
            self._cur_1s_frames = 0
            self._cur_1s_bytes = 0
            self._cur_1s_insts = 0
            self._cur_1s_s1_updates = 0
            self._cur_1s_s1_book = 0

        self._cur_1s_frames += 1
        self._cur_1s_bytes += raw_bytes
        self._cur_1s_insts += n_insts
        self._cur_1s_s1_updates += frame_s1_updates
        self._cur_1s_s1_book += frame_s1_book
        if self._cur_1s_frames > self._peak_1s_frames:
            self._peak_1s_frames = self._cur_1s_frames
        if self._cur_1s_bytes > self._peak_1s_bytes:
            self._peak_1s_bytes = self._cur_1s_bytes
        if self._cur_1s_insts > self._peak_1s_insts:
            self._peak_1s_insts = self._cur_1s_insts
        if self._cur_1s_s1_updates > self._peak_1s_s1_updates:
            self._peak_1s_s1_updates = self._cur_1s_s1_updates
        if self._cur_1s_s1_book > self._peak_1s_s1_book:
            self._peak_1s_s1_book = self._cur_1s_s1_book

        # 1-minute session rollup bucket
        if self._cur_1m_slot != slot_1m:
            if self._cur_1m_slot >= 0 and self._cur_1m_frames > 0:
                if len(self._ring_1m) == RING_1M_MAXLEN:
                    self._ring_1m_evictions += 1
                self._ring_1m.append({
                    "minute_epoch_ms": self._cur_1m_slot * 60_000,
                    "frames": self._cur_1m_frames,
                    "bytes": self._cur_1m_bytes,
                    "instruments": self._cur_1m_insts,
                    "s1_updates": self._cur_1m_s1_updates,
                    "s1_book_updates": self._cur_1m_s1_book,
                })
            self._cur_1m_slot = slot_1m
            self._cur_1m_frames = 0
            self._cur_1m_bytes = 0
            self._cur_1m_insts = 0
            self._cur_1m_s1_updates = 0
            self._cur_1m_s1_book = 0

        self._cur_1m_frames += 1
        self._cur_1m_bytes += raw_bytes
        self._cur_1m_insts += n_insts
        self._cur_1m_s1_updates += frame_s1_updates
        self._cur_1m_s1_book += frame_s1_book

    def record_ingress_frame(
        self,
        *,
        raw_bytes: int,
        is_bytes: bool,
        data_dict: Any,
        recv_wall_ms: int,
        t0_ns: int,
        t1_ns: int,
        t2_ns: int,
        t3_ns: int,
    ) -> None:
        if not self._enabled:
            return
        t_probe_start_ns = time.perf_counter_ns()
        try:
            with self._lock:
                if not self._enabled:
                    return
                self._refresh_universe_snapshot_unlocked(force=False)

                if self._first_frame_ms is None:
                    self._first_frame_ms = recv_wall_ms
                    self._first_frame_mono_ns = t0_ns
                self._last_frame_ms = recv_wall_ms
                self._last_frame_mono_ns = t0_ns

                self._total_frames += 1
                if not is_bytes:
                    self._non_bytes_payload_count += 1
                else:
                    self._total_raw_bytes += raw_bytes
                    self._hist_frame_bytes.observe(float(raw_bytes))
                    if raw_bytes > 65536:
                        self._frames_gt_64kib += 1
                    if raw_bytes > 1048576:
                        self._frames_gt_1mib += 1

                if not isinstance(data_dict, dict):
                    self._envelope_type_counts["other"] += 1
                    n_insts = 0
                    feeds = None
                else:
                    env_type = data_dict.get("type")
                    if not env_type:
                        self._envelope_type_counts["missing"] += 1
                    elif env_type in self._envelope_type_counts:
                        self._envelope_type_counts[env_type] += 1
                    else:
                        self._envelope_type_counts["other"] += 1

                    raw_cur_ts = data_dict.get("currentTs")
                    if raw_cur_ts is not None:
                        try:
                            cur_ts_ms = int(raw_cur_ts)
                            self._frames_with_current_ts += 1
                            self._hist_provider_clock_lag_ms.observe(float(recv_wall_ms - cur_ts_ms))
                        except (ValueError, TypeError):
                            self._frames_missing_current_ts += 1
                    else:
                        self._frames_missing_current_ts += 1

                    feeds = data_dict.get("feeds")
                    n_insts = len(feeds) if isinstance(feeds, dict) else 0

                self._total_instrument_updates += n_insts
                self._hist_instruments_per_frame.observe(float(n_insts))

                frame_s1_updates = 0
                frame_s1_book = 0

                if n_insts > 0 and isinstance(feeds, dict):
                    seen_all = self._seen_all_keys
                    seen_core = self._seen_s1_core_keys
                    seen_opt_1_7 = self._seen_nifty_opt_1_7_keys
                    seen_opt_other = self._seen_nifty_opt_other_keys
                    seen_opt_book = self._seen_nifty_opt_book_keys
                    opt_1_7 = self._nifty_opt_1_7_keys
                    opt_other = self._nifty_opt_other_keys
                    fut_keys = self._nifty_futures_keys
                    cohorts = self._cohorts
                    core_get = S1_CORE_INSTRUMENT_KEYS.get
                    non_s1_stats = cohorts["NON_S1_UNIVERSE"]
                    non_s1_frame_count = 0

                    for ikey, feed_obj in feeds.items():
                        if ikey not in seen_all:
                            if len(seen_all) < MAX_TRACKED_SEEN_KEYS:
                                seen_all.add(ikey)
                            else:
                                self._seen_key_cap_drops += 1

                        # Cohort classification (O(1) set/dict lookup)
                        core_name = core_get(ikey)
                        if core_name is not None:
                            c_stats = cohorts[core_name]
                            seen_core.add(ikey)
                            is_nifty_opt = False
                        elif ikey in opt_1_7:
                            c_stats = cohorts["NIFTY_OPTIONS_1_7_DTE"]
                            seen_opt_1_7.add(ikey)
                            is_nifty_opt = True
                        elif ikey in opt_other:
                            c_stats = cohorts["NIFTY_OPTIONS_OTHER_DTE"]
                            seen_opt_other.add(ikey)
                            is_nifty_opt = True
                        elif ikey in fut_keys:
                            c_stats = cohorts["NIFTY_FUTURES"]
                            is_nifty_opt = False
                        else:
                            # Ultra-fast path for ~504 non-S1 equities / non-S1 indices
                            non_s1_frame_count += 1
                            if isinstance(feed_obj, dict):
                                ff_outer = feed_obj.get("fullFeed")
                                if isinstance(ff_outer, dict):
                                    m_ff = ff_outer.get("marketFF")
                                    if isinstance(m_ff, dict):
                                        ltpc_ns = m_ff.get("ltpc")
                                        if isinstance(ltpc_ns, dict):
                                            ltp_ns = ltpc_ns.get("ltp")
                                            if isinstance(ltp_ns, (int, float)) and ltp_ns > 0.0:
                                                non_s1_stats.ltpc_updates += 1
                                        ml_ns = m_ff.get("marketLevel")
                                        if isinstance(ml_ns, dict):
                                            q_ns = ml_ns.get("bidAskQuote")
                                            if isinstance(q_ns, list) and q_ns:
                                                tq_ns = q_ns[0]
                                                if isinstance(tq_ns, dict):
                                                    bp_ns = tq_ns.get("bidP")
                                                    ap_ns = tq_ns.get("askP")
                                                    if (
                                                        isinstance(bp_ns, (int, float))
                                                        and isinstance(ap_ns, (int, float))
                                                        and bp_ns > 0.0
                                                        and ap_ns >= bp_ns
                                                    ):
                                                        non_s1_stats.book_updates += 1
                            continue

                        # Full microstructure accounting for S1-relevant instruments
                        c_stats.updates += 1
                        if c_stats.first_seen_ms == 0:
                            c_stats.first_seen_ms = recv_wall_ms
                        elif recv_wall_ms > c_stats.last_seen_ms:
                            gap = recv_wall_ms - c_stats.last_seen_ms
                            if gap > c_stats.max_inter_update_gap_ms:
                                c_stats.max_inter_update_gap_ms = gap
                        c_stats.last_seen_ms = recv_wall_ms
                        frame_s1_updates += 1

                        if not isinstance(feed_obj, dict):
                            continue

                        full_feed = feed_obj.get("fullFeed")
                        if isinstance(full_feed, dict):
                            market_ff = full_feed.get("marketFF")
                            index_ff = full_feed.get("indexFF")
                            ff = market_ff if isinstance(market_ff, dict) else (
                                index_ff if isinstance(index_ff, dict) else None
                            )
                        else:
                            market_ff = None
                            ff = None

                        ltpc = ff.get("ltpc") if isinstance(ff, dict) else feed_obj.get("ltpc")
                        has_ltp = False
                        if isinstance(ltpc, dict):
                            raw_ltp = ltpc.get("ltp")
                            if isinstance(raw_ltp, (int, float)):
                                if raw_ltp > 0.0:
                                    has_ltp = True
                                    c_stats.ltpc_updates += 1
                            elif raw_ltp is not None:
                                try:
                                    if float(raw_ltp) > 0.0:
                                        has_ltp = True
                                        c_stats.ltpc_updates += 1
                                except (ValueError, TypeError):
                                    pass

                        if isinstance(market_ff, dict):
                            m_level = market_ff.get("marketLevel")
                            if isinstance(m_level, dict):
                                if len(m_level) > 1:
                                    for ts_k in QUOTE_EXCHANGE_TS_CANDIDATE_KEYS:
                                        if ts_k in m_level:
                                            c_stats.quote_exchange_ts_updates += 1
                                            break
                                quotes = m_level.get("bidAskQuote")
                                if isinstance(quotes, list) and quotes:
                                    if not has_ltp:
                                        c_stats.book_only_no_ltp_updates += 1
                                    top_q = quotes[0]
                                    if isinstance(top_q, dict):
                                        if len(top_q) > 6 or "ts" in top_q or "ltt" in top_q or "timestamp" in top_q:
                                            for ts_k in QUOTE_EXCHANGE_TS_CANDIDATE_KEYS:
                                                if ts_k in top_q:
                                                    c_stats.quote_exchange_ts_updates += 1
                                                    break
                                        bp_raw = top_q.get("bidP") or top_q.get("bid_price") or 0.0
                                        ap_raw = top_q.get("askP") or top_q.get("ask_price") or 0.0
                                        bq_raw = top_q.get("bidQ") or top_q.get("bid_qty") or 0
                                        aq_raw = top_q.get("askQ") or top_q.get("ask_qty") or 0
                                        try:
                                            bp = bp_raw if isinstance(bp_raw, (int, float)) else float(bp_raw)
                                            ap = ap_raw if isinstance(ap_raw, (int, float)) else float(ap_raw)
                                            bq = bq_raw if isinstance(bq_raw, int) else int(bq_raw)
                                            aq = aq_raw if isinstance(aq_raw, int) else int(aq_raw)
                                        except (ValueError, TypeError):
                                            bp, ap, bq, aq = 0.0, 0.0, 0, 0

                                        if bp > 0.0 and ap >= bp:
                                            c_stats.book_updates += 1
                                            if bq > 0 and aq > 0:
                                                c_stats.book_2sided_qty_updates += 1
                                            if len(quotes) >= 5:
                                                c_stats.book_l5_updates += 1
                                            frame_s1_book += 1
                                            if is_nifty_opt and len(seen_opt_book) < MAX_TRACKED_SEEN_KEYS:
                                                seen_opt_book.add(ikey)
                                        else:
                                            c_stats.book_invalid_or_crossed_updates += 1

                            raw_oi = market_ff.get("oi")
                            if isinstance(raw_oi, (int, float)):
                                if raw_oi > 0:
                                    c_stats.oi_updates += 1
                            elif raw_oi is not None:
                                try:
                                    if float(raw_oi) > 0.0:
                                        c_stats.oi_updates += 1
                                except (ValueError, TypeError):
                                    pass

                            greeks = market_ff.get("optionGreeks")
                            if isinstance(greeks, dict) and greeks:
                                c_stats.greeks_updates += 1

                        if isinstance(ff, dict):
                            m_ohlc = ff.get("marketOHLC")
                            if isinstance(m_ohlc, dict):
                                ohlc_list = m_ohlc.get("ohlc")
                                if isinstance(ohlc_list, list):
                                    for c_item in ohlc_list:
                                        if isinstance(c_item, dict) and c_item.get("interval") == "I1":
                                            c_stats.i1_candle_updates += 1
                                            break

                    if non_s1_frame_count > 0:
                        non_s1_stats.updates += non_s1_frame_count
                        if non_s1_stats.first_seen_ms == 0:
                            non_s1_stats.first_seen_ms = recv_wall_ms
                        elif recv_wall_ms > non_s1_stats.last_seen_ms:
                            gap = recv_wall_ms - non_s1_stats.last_seen_ms
                            if gap > non_s1_stats.max_inter_update_gap_ms:
                                non_s1_stats.max_inter_update_gap_ms = gap
                        non_s1_stats.last_seen_ms = recv_wall_ms

                if frame_s1_updates > 0:
                    self._s1_relevant_frames += 1
                    self._s1_relevant_updates += frame_s1_updates
                if frame_s1_book > 0:
                    self._s1_book_bearing_updates += frame_s1_book

                slot_100ms = t0_ns // 100_000_000
                slot_1s = t0_ns // 1_000_000_000
                slot_1m = recv_wall_ms // 60_000
                self._roll_windows_unlocked(
                    slot_100ms=slot_100ms,
                    slot_1s=slot_1s,
                    slot_1m=slot_1m,
                    raw_bytes=raw_bytes if is_bytes else 0,
                    n_insts=n_insts,
                    frame_s1_updates=frame_s1_updates,
                    frame_s1_book=frame_s1_book,
                )

                # Latency segment observations (µs)
                if t1_ns >= t0_ns:
                    self._hist_sdk_decode_us.observe((t1_ns - t0_ns) / 1000.0)
                if t2_ns >= t1_ns:
                    self._hist_sdk_dict_us.observe((t2_ns - t1_ns) / 1000.0)
                if t3_ns >= t2_ns:
                    self._hist_downstream_dispatch_us.observe((t3_ns - t2_ns) / 1000.0)

                t_probe_end_ns = time.perf_counter_ns()
                self._hist_probe_self_us.observe((t_probe_end_ns - t_probe_start_ns) / 1000.0)
                self._hist_total_callback_us.observe((t_probe_end_ns - t0_ns) / 1000.0)
                self._consecutive_probe_faults = 0
        except Exception:
            self._probe_internal_errors += 1
            self._consecutive_probe_faults += 1
            if self._consecutive_probe_faults >= MAX_CONSECUTIVE_PROBE_FAULTS:
                self._enabled = False
                self._auto_disabled_on_fault = True
                self._disabled_at_ms = int(time.time() * 1000)

    # ──────────────────────────────────────────────────────────────────────────
    # COMPACT SUMMARIES & CODEX FEASIBILITY DIAGNOSTICS
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _read_passive_s205_telemetry() -> Dict[str, Any]:
        """Read-only observation of S2_05 orchestrator telemetry without mutating S2_05."""
        try:
            from app.analytics.option_sniffer_advance_s205.s205_upstox_engine import (
                get_s205_orchestrator_if_loaded,
            )
            orch = get_s205_orchestrator_if_loaded()
            if orch is None:
                return {"loaded": False}
            return {
                "loaded": True,
                "is_running": bool(getattr(orch, "_is_running", False)),
                "sweep_count": int(getattr(orch, "_sweep_count", 0)),
                "last_eval_latency_ms": float(getattr(orch, "_last_eval_latency_ms", 0.0)),
                "last_evaluated_bar_ts": getattr(orch, "_last_evaluated_bar_ts", None),
            }
        except Exception:
            return {"loaded": False}

    def get_compact_status(self) -> Dict[str, Any]:
        with self._lock:
            elapsed_s = (
                max(0.001, (self._last_frame_mono_ns - self._first_frame_mono_ns) / 1_000_000_000.0)
                if self._total_frames >= 2 and self._last_frame_mono_ns > self._first_frame_mono_ns
                else 0.0
            )
            return {
                "enabled": self._enabled,
                "auto_disabled_on_fault": self._auto_disabled_on_fault,
                "total_frames": self._total_frames,
                "total_raw_bytes": self._total_raw_bytes,
                "sustained_frames_per_sec": round(self._total_frames / elapsed_s, 2) if elapsed_s > 0 else 0.0,
                "sustained_bytes_per_sec": round(self._total_raw_bytes / elapsed_s, 2) if elapsed_s > 0 else 0.0,
                "peak_100ms_frames": self._peak_100ms_frames,
                "peak_100ms_bytes": self._peak_100ms_bytes,
                "peak_1s_frames": self._peak_1s_frames,
                "peak_1s_bytes": self._peak_1s_bytes,
                "s1_relevant_updates": self._s1_relevant_updates,
                "s1_book_bearing_updates": self._s1_book_bearing_updates,
                "probe_self_overhead_p99_us": self._hist_probe_self_us.summary().get("p99", 0.0),
                "probe_internal_errors": self._probe_internal_errors,
            }

    def get_summary(self, include_recent_rings: bool = False) -> Dict[str, Any]:
        s205_passive = self._read_passive_s205_telemetry()
        with self._lock:
            self._refresh_universe_snapshot_unlocked(force=True)
            elapsed_s = (
                (self._last_frame_mono_ns - self._first_frame_mono_ns) / 1_000_000_000.0
                if self._total_frames >= 2 and self._last_frame_mono_ns > self._first_frame_mono_ns
                else 0.0
            )
            sustained_msg_s = (self._total_frames / elapsed_s) if elapsed_s > 0 else 0.0
            sustained_bytes_s = (self._total_raw_bytes / elapsed_s) if elapsed_s > 0 else 0.0

            sub_total = len(self._subscribed_keys_snapshot)
            sub_opt_1_7 = len(self._nifty_opt_1_7_keys)
            sub_opt_other = len(self._nifty_opt_other_keys)
            sub_opt_all = sub_opt_1_7 + sub_opt_other
            seen_opt_all = len(self._seen_nifty_opt_1_7_keys) + len(self._seen_nifty_opt_other_keys)

            nifty_opt_1_7_stats = self._cohorts["NIFTY_OPTIONS_1_7_DTE"]
            nifty_opt_other_stats = self._cohorts["NIFTY_OPTIONS_OTHER_DTE"]
            total_nifty_opt_updates = nifty_opt_1_7_stats.updates + nifty_opt_other_stats.updates
            total_nifty_opt_books = nifty_opt_1_7_stats.book_updates + nifty_opt_other_stats.book_updates
            total_quote_ts_seen = sum(c.quote_exchange_ts_updates for c in self._cohorts.values())

            session_seconds_6h15m = 22500.0
            p95_bytes_s = self._hist_1s_bytes.quantile(0.95) if self._hist_1s_bytes.count > 0 else sustained_bytes_s
            s1_update_share = (
                self._s1_relevant_updates / float(self._total_instrument_updates)
                if self._total_instrument_updates > 0
                else 0.0
            )

            probe_self_summary = self._hist_probe_self_us.summary()
            total_cb_summary = self._hist_total_callback_us.summary()
            downstream_summary = self._hist_downstream_dispatch_us.summary()
            frame_bytes_summary = self._hist_frame_bytes.summary()

            codex_feasibility = {
                "observation_span_seconds": round(elapsed_s, 3),
                "sustained_frames_per_sec": round(sustained_msg_s, 3),
                "sustained_bytes_per_sec": round(sustained_bytes_s, 2),
                "peak_100ms_frames": self._peak_100ms_frames,
                "peak_100ms_bytes": self._peak_100ms_bytes,
                "peak_1s_frames": self._peak_1s_frames,
                "peak_1s_bytes": self._peak_1s_bytes,
                "p95_1s_bytes_per_sec": round(p95_bytes_s, 2),
                "min_writer_drain_bytes_per_sec_2x_peak": int(self._peak_1s_bytes * 2),
                "slot_64kib_fit": {
                    "frames_exceeding_64kib": self._frames_gt_64kib,
                    "frames_exceeding_1mib": self._frames_gt_1mib,
                    "max_frame_bytes": int(frame_bytes_summary["max"]),
                    "fits_64kib_slot": (self._frames_gt_64kib == 0),
                    "fits_1mib_hard_cap": (self._frames_gt_1mib == 0),
                },
                "ring_slot_headroom": {
                    "peak_100ms_frames_vs_64_slots": round(self._peak_100ms_frames / 64.0, 4),
                    "peak_1s_frames_vs_256_slots": round(self._peak_1s_frames / 256.0, 4),
                    "peak_1s_frames_vs_1024_slots": round(self._peak_1s_frames / 1024.0, 4),
                },
                "projected_22500s_session_storage": {
                    "raw_protobuf_sustained_gib": round(
                        (sustained_bytes_s * session_seconds_6h15m) / float(1024 ** 3), 4
                    ),
                    "raw_protobuf_p95_gib": round(
                        (p95_bytes_s * session_seconds_6h15m) / float(1024 ** 3), 4
                    ),
                    "json_base64_prototype_est_gib_2_8x": round(
                        (sustained_bytes_s * 2.8 * session_seconds_6h15m) / float(1024 ** 3), 4
                    ),
                    "s1_proportional_raw_est_gib": round(
                        (sustained_bytes_s * s1_update_share * session_seconds_6h15m) / float(1024 ** 3), 4
                    ),
                },
                "callback_latency_gates": {
                    "probe_self_p99_us": probe_self_summary["p99"],
                    "probe_self_p999_us": probe_self_summary["p999"],
                    "probe_self_le_100us_ratio": self._hist_probe_self_us.fraction_le(100.0),
                    "probe_self_le_1000us_ratio": self._hist_probe_self_us.fraction_le(1000.0),
                    "downstream_dispatch_p99_us": downstream_summary["p99"],
                    "total_callback_p99_us": total_cb_summary["p99"],
                    "total_callback_p999_us": total_cb_summary["p999"],
                },
                "s1_universe_and_book_provenance": {
                    "s1_update_share_of_stream": round(s1_update_share, 6),
                    "s1_core_subscribed_count": len(S1_CORE_INSTRUMENT_KEYS),
                    "s1_core_delivered_count": len(self._seen_s1_core_keys),
                    "nifty_options_1_7_dte_subscribed": sub_opt_1_7,
                    "nifty_options_1_7_dte_delivered": len(self._seen_nifty_opt_1_7_keys),
                    "nifty_options_all_subscribed": sub_opt_all,
                    "nifty_options_all_delivered": seen_opt_all,
                    "nifty_options_with_valid_book": len(self._seen_nifty_opt_book_keys),
                    "nifty_option_book_bearing_update_ratio": (
                        round(total_nifty_opt_books / float(total_nifty_opt_updates), 6)
                        if total_nifty_opt_updates > 0
                        else 0.0
                    ),
                    "provider_current_ts_present_ratio": (
                        round(self._frames_with_current_ts / float(self._total_frames), 6)
                        if self._total_frames > 0
                        else 0.0
                    ),
                    "dedicated_quote_exchange_ts_observed_updates": total_quote_ts_seen,
                },
            }

            result: Dict[str, Any] = {
                "enabled": self._enabled,
                "created_or_reset_ms": self._created_or_reset_ms,
                "enabled_since_ms": self._enabled_since_ms,
                "disabled_at_ms": self._disabled_at_ms,
                "first_frame_ms": self._first_frame_ms,
                "last_frame_ms": self._last_frame_ms,
                "ingress_totals": {
                    "frames": self._total_frames,
                    "raw_bytes": self._total_raw_bytes,
                    "instrument_updates": self._total_instrument_updates,
                    "s1_relevant_frames": self._s1_relevant_frames,
                    "s1_relevant_updates": self._s1_relevant_updates,
                    "s1_book_bearing_updates": self._s1_book_bearing_updates,
                    "envelope_types": dict(self._envelope_type_counts),
                },
                "distributions": {
                    "frame_bytes": frame_bytes_summary,
                    "instruments_per_frame": self._hist_instruments_per_frame.summary(),
                    "provider_clock_lag_ms": self._hist_provider_clock_lag_ms.summary(),
                    "burst_100ms_frames": self._hist_100ms_burst_frames.summary(),
                    "burst_100ms_bytes": self._hist_100ms_burst_bytes.summary(),
                    "rate_1s_frames": self._hist_1s_frames.summary(),
                    "rate_1s_bytes": self._hist_1s_bytes.summary(),
                },
                "burst_peaks": {
                    "peak_100ms_frames": self._peak_100ms_frames,
                    "peak_100ms_bytes": self._peak_100ms_bytes,
                    "peak_100ms_instruments": self._peak_100ms_insts,
                    "peak_100ms_s1_book_updates": self._peak_100ms_s1_book,
                    "peak_1s_frames": self._peak_1s_frames,
                    "peak_1s_bytes": self._peak_1s_bytes,
                    "peak_1s_instruments": self._peak_1s_insts,
                    "peak_1s_s1_updates": self._peak_1s_s1_updates,
                    "peak_1s_s1_book_updates": self._peak_1s_s1_book,
                },
                "delivery_coverage": {
                    "subscribed_total_keys": sub_total,
                    "delivered_unique_keys": len(self._seen_all_keys),
                    "total_coverage_ratio": (
                        round(len(self._seen_all_keys) / float(sub_total), 6) if sub_total > 0 else 0.0
                    ),
                    "s1_core_delivered_keys": sorted(self._seen_s1_core_keys),
                    "s1_core_missing_keys": sorted(
                        set(S1_CORE_INSTRUMENT_KEYS.keys()) - self._seen_s1_core_keys
                    ),
                    "nifty_options_1_7_dte_subscribed": sub_opt_1_7,
                    "nifty_options_1_7_dte_delivered": len(self._seen_nifty_opt_1_7_keys),
                    "nifty_options_other_dte_subscribed": sub_opt_other,
                    "nifty_options_other_dte_delivered": len(self._seen_nifty_opt_other_keys),
                    "nifty_options_book_delivered": len(self._seen_nifty_opt_book_keys),
                },
                "cohorts": {name: self._cohorts[name].to_dict() for name in COHORT_NAMES},
                "latencies_us": {
                    "sdk_decode_us": self._hist_sdk_decode_us.summary(),
                    "sdk_dict_convert_us": self._hist_sdk_dict_us.summary(),
                    "downstream_dispatch_us": downstream_summary,
                    "listener_broadcast_us": self._hist_listener_broadcast_us.summary(),
                    "equity_ingest_us": self._hist_equity_ingest_us.summary(),
                    "probe_self_overhead_us": probe_self_summary,
                    "total_callback_us": total_cb_summary,
                    "bar_flush_15m_us": self._hist_bar_flush_15m_us.summary(),
                    "freeze_snapshot_us": self._hist_freeze_snapshot_us.summary(),
                    "s205_passive_telemetry": s205_passive,
                },
                "lifecycle_and_reconnects": {
                    "ws_open_count": self._ws_open_count,
                    "ws_close_count": self._ws_close_count,
                    "ws_error_count": self._ws_error_count,
                    "ws_reconnecting_count": self._ws_reconnecting_count,
                    "ws_auto_reconnect_stopped_count": self._ws_auto_reconnect_stopped_count,
                    "max_disconnect_gap_ms": round(self._max_disconnect_gap_ms, 2),
                    "recent_events": list(self._lifecycle_events),
                },
                "failures_and_bounds": {
                    "non_bytes_payload_count": self._non_bytes_payload_count,
                    "sdk_decode_errors": self._sdk_decode_errors,
                    "sdk_dict_errors": self._sdk_dict_errors,
                    "downstream_emit_errors": self._downstream_emit_errors,
                    "probe_internal_errors": self._probe_internal_errors,
                    "auto_disabled_on_fault": self._auto_disabled_on_fault,
                    "seen_key_cap_drops": self._seen_key_cap_drops,
                    "ring_100ms_len": len(self._ring_100ms),
                    "ring_100ms_maxlen": RING_100MS_MAXLEN,
                    "ring_100ms_evictions": self._ring_100ms_evictions,
                    "ring_1s_len": len(self._ring_1s),
                    "ring_1s_maxlen": RING_1S_MAXLEN,
                    "ring_1s_evictions": self._ring_1s_evictions,
                    "ring_1m_len": len(self._ring_1m),
                    "ring_1m_maxlen": RING_1M_MAXLEN,
                    "ring_1m_evictions": self._ring_1m_evictions,
                    "lifecycle_ring_evictions": self._lifecycle_ring_evictions,
                },
                "codex_feasibility_metrics": codex_feasibility,
            }

            if include_recent_rings:
                result["recent_1m_rollups"] = list(self._ring_1m)
                result["recent_1s_rates"] = [
                    {
                        "slot_1s": r[0],
                        "frames": r[1],
                        "bytes": r[2],
                        "instruments": r[3],
                        "s1_updates": r[4],
                        "s1_book_updates": r[5],
                    }
                    for r in list(self._ring_1s)[-60:]
                ]
                result["recent_100ms_bursts"] = [
                    {
                        "slot_100ms": r[0],
                        "frames": r[1],
                        "bytes": r[2],
                        "instruments": r[3],
                        "s1_book_updates": r[4],
                    }
                    for r in list(self._ring_100ms)[-100:]
                ]

            return result


def build_instrumented_streamer_class(base_streamer_cls: Any, probe: S1IngressMeasurementProbe) -> Any:
    """
    Creates a lightweight pass-through subclass of `upstox_client.MarketDataStreamerV3`
    whose disabled fast path (`not probe._enabled`) executes a single boolean check
    and delegates directly to `super().handle_message(ws, message)`.

    Allows enabling and disabling measurement at runtime without reconnecting or
    re-subscribing the active WebSocket stream.
    """
    from google.protobuf import json_format

    class InstrumentedMarketDataStreamerV3(base_streamer_cls):
        _s1_probe = probe

        def handle_message(self, ws: Any, message: Any) -> Any:
            p = self._s1_probe
            if p is None or not p._enabled:
                return super().handle_message(ws, message)

            t0_ns = time.perf_counter_ns()
            recv_wall_ms = int(time.time() * 1000)
            is_bytes = isinstance(message, (bytes, bytearray, memoryview))
            raw_bytes = len(message) if is_bytes else 0

            try:
                decoded_data = self.decode_protobuf(message)
            except Exception:
                p.record_sdk_error("decode_protobuf")
                raise
            t1_ns = time.perf_counter_ns()

            try:
                data_dict = json_format.MessageToDict(decoded_data)
            except Exception:
                p.record_sdk_error("message_to_dict")
                raise
            t2_ns = time.perf_counter_ns()

            try:
                self.emit(self.Event["MESSAGE"], data_dict)
            except Exception:
                p.record_sdk_error("emit_message")
                raise
            t3_ns = time.perf_counter_ns()

            p.record_ingress_frame(
                raw_bytes=raw_bytes,
                is_bytes=is_bytes,
                data_dict=data_dict,
                recv_wall_ms=recv_wall_ms,
                t0_ns=t0_ns,
                t1_ns=t1_ns,
                t2_ns=t2_ns,
                t3_ns=t3_ns,
            )

    return InstrumentedMarketDataStreamerV3


def attach_probe_lifecycle_listeners(streamer: Any, probe: S1IngressMeasurementProbe) -> None:
    """Attaches passive non-interfering SDK event observers for reconnect/close accounting."""
    if streamer is None or probe is None or not hasattr(streamer, "on"):
        return

    def _safe_on(ev_name: str, fn: Callable[..., None]) -> None:
        try:
            streamer.on(ev_name, fn)
        except Exception:
            pass

    _safe_on("open", lambda *a, **kw: probe.record_ws_lifecycle("open"))
    _safe_on("close", lambda *a, **kw: probe.record_ws_lifecycle("close", detail=str(a) if a else None))
    _safe_on("error", lambda *a, **kw: probe.record_ws_lifecycle("error", detail=str(a[0]) if a else None))
    _safe_on("reconnecting", lambda *a, **kw: probe.record_ws_lifecycle("reconnecting", detail=str(a[0]) if a else None))
    _safe_on(
        "autoReconnectStopped",
        lambda *a, **kw: probe.record_ws_lifecycle("autoReconnectStopped", detail=str(a[0]) if a else None),
    )


_GLOBAL_S1_INGRESS_PROBE: Optional[S1IngressMeasurementProbe] = None
_PROBE_INIT_LOCK = threading.Lock()


def get_s1_ingress_probe() -> S1IngressMeasurementProbe:
    """Returns the process-wide default-OFF S1IngressMeasurementProbe singleton."""
    global _GLOBAL_S1_INGRESS_PROBE
    if _GLOBAL_S1_INGRESS_PROBE is None:
        with _PROBE_INIT_LOCK:
            if _GLOBAL_S1_INGRESS_PROBE is None:
                _GLOBAL_S1_INGRESS_PROBE = S1IngressMeasurementProbe()
    return _GLOBAL_S1_INGRESS_PROBE
