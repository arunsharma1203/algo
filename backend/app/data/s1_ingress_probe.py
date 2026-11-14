"""
S1 Prospective Data-Recording Programme — Phase 1 (V3): Hardened Measurement-Only Ingress Probe.

Simplest Maintainable Safety Architecture:
  1. Strictly Default-OFF (`enabled = False` unconditionally at construction; no env auto-enable).
  2. Zero-Mutex, Zero-Wakeup Callback Path:
     - Receive callbacks acquire ZERO locks and call ZERO condition/event wakeups (`10 ms` poll worker).
     - Complete entry-to-exit callback timing (`t_cb1` captured AFTER `_inbox.append`, written into
       preallocated cell slot) drives both immediate spike and rolling `p99 <= 100 us` / `p99.9 <= 1000 us`
       auto-disable circuit breakers.
     - Immutable wire-byte handoff (`bytes`, `<= 256` frames, `<= 4 MiB` actual RAM) eliminates caller
       `data_dict` retention (`~146 MiB` -> `< 4.2 MiB`) and post-handoff mutation races.
  3. Micro-Sliced C-Protobuf Worker + Fail-Safe Liveness Latch:
     - Parses wire bytes directly via C++/upb `MarketDataFeedV3_pb2.FeedResponse` (`<= 1.5 ms` slice per
       `10 ms` poll tick, `>= 5 s` universe refresh floor with slow-supplier backoff).
     - Top-level `try/except/finally` worker death latch + callback heartbeat watchdog auto-disable
       immediately on worker crash or stall without affecting live stream delivery.
  4. Exact-Once Multi-Session Durable Retention (`< 12 MiB` across 5 full sessions `<< 64 MiB` cap):
     - Per-boot-generation hash-chained JSONL ledgers (cross-midnight safe) + atomic manifest/checkpoint.
     - Disjoint monotonic epochs (`1 enable..disable = 1 epoch`), idempotent single seal, disjoint
       incremental `MINUTE_ROLLUP` records + compact `60 s` checkpoints, durable `control_audit`, and
       fail-closed `reset()` when prior epoch is unexported.
  5. Loopback-Restricted RBAC Management Plane + Guaranteed Non-Rate-Limited Emergency Disable +
     Tailscale-Compatible App CORS Regex (`100.64.0.0/10` & `*.ts.net` for UI, loopback-only for probe).
"""

import bisect
import collections
import copy
import datetime
import enum
import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
import uuid
import weakref
from pathlib import Path
from typing import Any, Callable, Deque, Dict, FrozenSet, List, NamedTuple, Optional, Set, Tuple

SCHEMA_VERSION: str = "S1_INGRESS_SUMMARY_V3"
PROBE_WRAPPER_VERSION: str = "S1_STREAMER_WRAPPER_V3"
IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")

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
    "UNKNOWN_FO_UNCLASSIFIED",
    "NON_S1_UNIVERSE",
)

# Strict bounded memory, queue, worker, and storage constants
MAX_INBOX_SAMPLES: int = 256
MAX_INBOX_WIRE_BYTES: int = 4 * 1024 * 1024        # 4 MiB hard cap on queued immutable bytes
MAX_INLINE_DICT_FEEDS: int = 256                   # Max synthetic dict keys extracted if no wire bytes
MAX_KEY_CHARS: int = 128
MAX_TRACKED_SEEN_KEYS: int = 4096
MAX_TRACKED_OPTION_COHORT_KEYS: int = 512
MAX_PER_INSTRUMENT_STATS_KEYS: int = 1024
MAX_HISTORICAL_IDENTITY_KEYS: int = 2048
MAX_UNIVERSE_KEYS_SNAPSHOT: int = 4096
MAX_UNIVERSE_CONTRACTS_SNAPSHOT: int = 2048
MAX_UNIVERSE_EPOCHS_RING: int = 64
MAX_CONTROL_INTERVALS: int = 64
MAX_CONTROL_AUDIT_RING: int = 128
RING_100MS_MAXLEN: int = 100
RING_1S_MAXLEN: int = 120
RING_1M_MAXLEN: int = 60
LIFECYCLE_RING_MAXLEN: int = 64

WORKER_POLL_SLEEP_S: float = 0.005                 # 5 ms idle poll interval (zero callback Event.set!)
WORKER_MAX_SLICE_NS: int = 600_000                 # 600 us micro-slice cap so worker never starves callback GIL
WORKER_MAX_BATCH_ITEMS: int = 64                   # Up to 64 tiny LTPC frames within the 600 us budget
WORKER_STALL_TIMEOUT_NS: int = 5_000_000_000       # 5.0 s heartbeat watchdog
MIN_UNIVERSE_REFRESH_INTERVAL_NS: int = 5_000_000_000   # Hard 5 s minimum floor while streaming
SLOW_SUPPLIER_BACKOFF_NS: int = 30_000_000_000     # 30 s backoff if supplier > 15 ms
SLOW_SUPPLIER_THRESHOLD_NS: int = 15_000_000
SNAPSHOT_PUBLISH_INTERVAL_NS: int = 1_000_000_000  # 1.0 s published snapshot rebuild cadence

MAX_CONSECUTIVE_PROBE_FAULTS: int = 5
MAX_CONSECUTIVE_LATENCY_BREACHES: int = 5          # > 1000 us full added callback cost
LATENCY_P99_BUDGET_US: float = 100.0
LATENCY_SINGLE_BREACH_US: float = 1000.0
LATENCY_SEVERE_STALL_US: float = 5000.0
MAX_SEVERE_LATENCY_STALLS: int = 3
ROLLING_LATENCY_WINDOW: int = 256
MIN_ROLLING_SAMPLES_FOR_P99_TRIP: int = 100
MIN_SAMPLES_FOR_PRODUCTION_GATE: int = 50
CLOCK_STEP_TOLERANCE_MS: float = 250.0

MAX_EXPORT_STORAGE_BYTES: int = 64 * 1024 * 1024   # 64 MiB hard cap across 5+ sessions
MAX_EXPORT_RECORD_BYTES: int = 262144              # 256 KiB max single JSONL record
DEFAULT_EXPORT_INTERVAL_NS: int = 180_000_000_000  # 180 s full checkpoint (+ 60 s incremental MINUTE_ROLLUP)

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
    0.0, 1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 200.0, 350.0, 500.0,
    750.0, 1000.0, 1500.0, 2000.0, 3000.0, 5000.0, 10000.0,
)

BYTE_RATE_BOUNDS_PER_SEC: Tuple[float, ...] = (
    0.0, 1024.0, 4096.0, 16384.0, 65536.0, 131072.0, 262144.0, 524288.0,
    1048576.0, 2097152.0, 4194304.0, 8388608.0, 16777216.0, 33554432.0, 67108864.0,
)

CLOCK_LAG_BOUNDS_MS: Tuple[float, ...] = (
    -5000.0, -1000.0, -250.0, -50.0, 0.0, 10.0, 25.0, 50.0,
    100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 15000.0, 60000.0,
)

UNVERIFIED_QUOTE_TS_CANDIDATE_KEYS: Tuple[str, ...] = (
    "ts", "timestamp", "ltt", "exchangeTs", "quoteTs", "bookTs",
)

DEFAULT_ALLOWED_LOCAL_ORIGINS: FrozenSet[str] = frozenset({
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
})

# Tailscale CGNAT (100.64.0.0/10), Tailscale IPv6 ULA (fd7a:115c:a1e0::/48), and MagicDNS (*.ts.net)
TAILSCALE_CORS_ORIGIN_REGEX: str = (
    r"^https?://(?:"
    r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"|\[fd7a:115c:a1e0:[0-9a-fA-F:]+\]"
    r"|[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.ts\.net"
    r")(?::\d{2,5})?$"
)

LOOPBACK_HOSTS: FrozenSet[str] = frozenset({"127.0.0.1", "::1", "localhost"})
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Data-only inbox tags (controls NEVER enter `_inbox`)
_EVT_FRAME_OK = 1
_EVT_FRAME_ERR = 2
_EVT_DOWNSTREAM_SEG = 3
_EVT_PERIODIC_STAGE = 4
_EVT_LIFECYCLE = 5

_PAYLOAD_WIRE_BYTES = 1
_PAYLOAD_FROZEN_TUPLES = 2


class ProbeControlAction(str, enum.Enum):
    ENABLE = "enable"
    DISABLE = "disable"
    RESET = "reset"
    EXPORT_FLUSH = "export_flush"


class ProbeControlError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ContractIdentityMeta(NamedTuple):
    underlying: str
    expiry_iso: str
    cached_dte: Optional[int]
    is_futures: bool
    lot_size: int
    lot_verified: bool
    rev_seq: int


def _compute_file_sha256(path_obj: Optional[Path]) -> Optional[str]:
    if path_obj is None:
        return None
    try:
        if path_obj.is_file():
            return hashlib.sha256(path_obj.read_bytes()).hexdigest()
    except Exception:
        return None
    return None


PROBE_CODE_SHA256: str = _compute_file_sha256(Path(__file__).resolve()) or "UNKNOWN"

_CACHED_PB_MODULE: Any = None


def _get_pb_module() -> Any:
    global _CACHED_PB_MODULE
    if _CACHED_PB_MODULE is None:
        from upstox_client.feeder.proto import MarketDataFeedV3_pb2 as _pb
        _CACHED_PB_MODULE = _pb
    return _CACHED_PB_MODULE


def _fsync_directory_best_effort(dir_path: Path) -> None:
    try:
        dfd = os.open(str(dir_path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        pass


def _write_all_fd(fd: int, data: bytes) -> None:
    mv = memoryview(data)
    written = 0
    total = len(data)
    while written < total:
        n = os.write(fd, mv[written:])
        if n <= 0:
            raise OSError("Zero-byte write while flushing durable probe record")
        written += n


class BoundedHistogram:
    """
    Fixed-bin histogram over preallocated list counters.
    Supports `observe_many(value, n)` in O(1) for exact zero-gap accounting without loops,
    conservative nearest-rank `pXX_upper_bound`, exact exceedance counters, and lossless bin merges.
    """

    __slots__ = (
        "_bounds",
        "_counts",
        "_count",
        "_sum",
        "_min",
        "_max",
        "_exceed_100",
        "_exceed_1000",
    )

    def __init__(self, bounds: Tuple[float, ...]) -> None:
        self._bounds: Tuple[float, ...] = bounds
        self._counts: List[int] = [0] * (len(bounds) + 1)
        self._count: int = 0
        self._sum: float = 0.0
        self._min: float = math.inf
        self._max: float = -math.inf
        self._exceed_100: int = 0
        self._exceed_1000: int = 0

    def observe(self, value: float) -> None:
        if not math.isfinite(value):
            return
        idx = bisect.bisect_left(self._bounds, value)
        self._counts[idx] += 1
        self._count += 1
        self._sum += value
        if value < self._min:
            self._min = value
        if value > self._max:
            self._max = value
        if value > 100.0:
            self._exceed_100 += 1
            if value > 1000.0:
                self._exceed_1000 += 1

    def observe_many(self, value: float, count: int) -> None:
        if count <= 0 or not math.isfinite(value):
            return
        idx = bisect.bisect_left(self._bounds, value)
        self._counts[idx] += count
        self._count += count
        self._sum += value * count
        if value < self._min:
            self._min = value
        if value > self._max:
            self._max = value
        if value > 100.0:
            self._exceed_100 += count
            if value > 1000.0:
                self._exceed_1000 += count

    def reset(self) -> None:
        for i in range(len(self._counts)):
            self._counts[i] = 0
        self._count = 0
        self._sum = 0.0
        self._min = math.inf
        self._max = -math.inf
        self._exceed_100 = 0
        self._exceed_1000 = 0

    @property
    def count(self) -> int:
        return self._count

    @property
    def exceed_100_count(self) -> int:
        return self._exceed_100

    @property
    def exceed_1000_count(self) -> int:
        return self._exceed_1000

    def merge_from_dict(self, exported: Dict[str, Any]) -> None:
        bins = exported.get("bin_counts")
        bounds = exported.get("bounds")
        if not isinstance(bins, list) or len(bins) != len(self._counts):
            return
        if isinstance(bounds, list) and tuple(float(x) for x in bounds) != self._bounds:
            return
        cnt = int(exported.get("count", 0))
        if cnt <= 0:
            return
        for i, v in enumerate(bins):
            self._counts[i] += int(v)
        self._count += cnt
        self._sum += float(exported.get("sum", 0.0))
        mn = float(exported.get("min", math.inf))
        mx = float(exported.get("max", -math.inf))
        if mn < self._min:
            self._min = mn
        if mx > self._max:
            self._max = mx
        self._exceed_100 += int(exported.get("exceed_100_count", 0))
        self._exceed_1000 += int(exported.get("exceed_1000_count", 0))

    def quantile_upper_bound(self, q: float) -> float:
        if self._count == 0:
            return 0.0
        if q <= 0.0:
            return float(self._min)
        if q >= 1.0:
            return float(self._max)
        rank = max(1, int(math.ceil(q * self._count)))
        cum = 0
        n_bounds = len(self._bounds)
        for i, c in enumerate(self._counts):
            if c == 0:
                continue
            cum += c
            if cum >= rank:
                if i < n_bounds:
                    return float(min(self._max, self._bounds[i]))
                return float(self._max)
        return float(self._max)

    def quantile_interpolated_est(self, q: float) -> float:
        if self._count == 0:
            return 0.0
        if q <= 0.0:
            return float(self._min)
        if q >= 1.0:
            return float(self._max)
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
                low = min(self._min, bounds[0]) if i == 0 else max(self._min, bounds[i - 1])
                high = min(self._max, bounds[i]) if i < n_bounds else self._max
                if high <= low or c <= 1:
                    return float(high)
                return float(low + ((target - prev_cum) / float(c)) * (high - low))
        return float(self._max)

    def summary(self, decimals: int = 2, include_bins: Any = False) -> Dict[str, Any]:
        if self._count == 0:
            out: Dict[str, Any] = {
                "count": 0,
                "sum": 0.0,
                "min": 0.0,
                "mean": 0.0,
                "p50_upper_bound": 0.0,
                "p90_upper_bound": 0.0,
                "p95_upper_bound": 0.0,
                "p99_upper_bound": 0.0,
                "p999_upper_bound": 0.0,
                "p99_interpolated_est": 0.0,
                "max": 0.0,
                "exceed_100_count": 0,
                "exceed_1000_count": 0,
            }
        else:
            out = {
                "count": self._count,
                "sum": round(self._sum, decimals + 2),
                "min": round(self._min, decimals),
                "mean": round(self._sum / float(self._count), decimals),
                "p50_upper_bound": round(self.quantile_upper_bound(0.50), decimals),
                "p90_upper_bound": round(self.quantile_upper_bound(0.90), decimals),
                "p95_upper_bound": round(self.quantile_upper_bound(0.95), decimals),
                "p99_upper_bound": round(self.quantile_upper_bound(0.99), decimals),
                "p999_upper_bound": round(self.quantile_upper_bound(0.999), decimals),
                "p99_interpolated_est": round(self.quantile_interpolated_est(0.99), decimals),
                "max": round(self._max, decimals),
                "exceed_100_count": self._exceed_100,
                "exceed_1000_count": self._exceed_1000,
            }
        if include_bins == "compact":
            if self._count > 0:
                out["bin_counts"] = list(self._counts)
        elif include_bins:
            out["bounds"] = list(self._bounds)
            out["bin_counts"] = list(self._counts)
        return out


class CohortCounters:
    __slots__ = (
        "updates",
        "ltpc_field_present",
        "ltpc_positive_updates",
        "book_field_present",
        "book_nonempty_updates",
        "book_valid_2sided_updates",
        "book_distinct_top_changes",
        "book_2sided_qty_updates",
        "book_usable_1lot_verified_updates",
        "book_1lot_unverified_lot_updates",
        "book_l5_updates",
        "book_only_no_ltp_updates",
        "book_empty_or_crossed_updates",
        "oi_field_present",
        "oi_positive_updates",
        "greeks_updates",
        "i1_candle_updates",
        "unverified_candidate_quote_ts_fields",
        "schema_full_market",
        "schema_full_index",
        "schema_first_level_greeks",
        "schema_top_ltpc",
        "schema_empty_or_other",
        "first_seen_mono_ns",
        "last_seen_mono_ns",
        "cohort_any_max_gap_ms",
    )

    def __init__(self) -> None:
        self.updates: int = 0
        self.ltpc_field_present: int = 0
        self.ltpc_positive_updates: int = 0
        self.book_field_present: int = 0
        self.book_nonempty_updates: int = 0
        self.book_valid_2sided_updates: int = 0
        self.book_distinct_top_changes: int = 0
        self.book_2sided_qty_updates: int = 0
        self.book_usable_1lot_verified_updates: int = 0
        self.book_1lot_unverified_lot_updates: int = 0
        self.book_l5_updates: int = 0
        self.book_only_no_ltp_updates: int = 0
        self.book_empty_or_crossed_updates: int = 0
        self.oi_field_present: int = 0
        self.oi_positive_updates: int = 0
        self.greeks_updates: int = 0
        self.i1_candle_updates: int = 0
        self.unverified_candidate_quote_ts_fields: int = 0
        self.schema_full_market: int = 0
        self.schema_full_index: int = 0
        self.schema_first_level_greeks: int = 0
        self.schema_top_ltpc: int = 0
        self.schema_empty_or_other: int = 0
        self.first_seen_mono_ns: int = 0
        self.last_seen_mono_ns: int = 0
        self.cohort_any_max_gap_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "updates": self.updates,
            "ltpc_field_present": self.ltpc_field_present,
            "ltpc_positive_updates": self.ltpc_positive_updates,
            "book_field_present": self.book_field_present,
            "book_nonempty_updates": self.book_nonempty_updates,
            "book_valid_2sided_delivery_updates": self.book_valid_2sided_updates,
            "book_valid_2sided_updates": self.book_valid_2sided_updates,
            "book_distinct_top_state_changes": self.book_distinct_top_changes,
            "book_2sided_qty_updates": self.book_2sided_qty_updates,
            "book_usable_1lot_updates": self.book_usable_1lot_verified_updates,
            "book_1lot_unverified_lot_updates": self.book_1lot_unverified_lot_updates,
            "book_l5_updates": self.book_l5_updates,
            "book_only_no_ltp_updates": self.book_only_no_ltp_updates,
            "book_empty_or_crossed_updates": self.book_empty_or_crossed_updates,
            "oi_field_present": self.oi_field_present,
            "oi_positive_updates": self.oi_positive_updates,
            "greeks_updates": self.greeks_updates,
            "i1_candle_updates": self.i1_candle_updates,
            "unverified_candidate_quote_ts_fields": self.unverified_candidate_quote_ts_fields,
            "schema_arms": {
                "fullFeed_marketFF": self.schema_full_market,
                "fullFeed_indexFF": self.schema_full_index,
                "firstLevelWithGreeks": self.schema_first_level_greeks,
                "top_ltpc": self.schema_top_ltpc,
                "empty_or_other": self.schema_empty_or_other,
            },
            "valid_book_ratio": (
                round(self.book_valid_2sided_updates / float(self.updates), 6) if self.updates > 0 else 0.0
            ),
            "usable_1lot_book_ratio": (
                round(self.book_usable_1lot_verified_updates / float(self.updates), 6) if self.updates > 0 else 0.0
            ),
            "cohort_any_update_max_gap_ms": round(self.cohort_any_max_gap_ms, 2),
        }


class PerInstrumentStats:
    """Bounded per-S1-instrument delivery, book-freshness, distinct-quote, and silence tracker."""

    __slots__ = (
        "cohort",
        "seeded_mono_ns",
        "updates",
        "valid_books",
        "distinct_books",
        "usable_1lot_books",
        "first_mono_ns",
        "last_mono_ns",
        "last_book_mono_ns",
        "last_1lot_book_mono_ns",
        "max_update_gap_ms",
        "max_book_gap_ms",
        "max_1lot_book_gap_ms",
        "last_top_quote",
    )

    def __init__(self, cohort: str, seeded_mono_ns: int) -> None:
        self.cohort: str = cohort
        self.seeded_mono_ns: int = seeded_mono_ns
        self.updates: int = 0
        self.valid_books: int = 0
        self.distinct_books: int = 0
        self.usable_1lot_books: int = 0
        self.first_mono_ns: int = 0
        self.last_mono_ns: int = 0
        self.last_book_mono_ns: int = 0
        self.last_1lot_book_mono_ns: int = 0
        self.max_update_gap_ms: float = 0.0
        self.max_book_gap_ms: float = 0.0
        self.max_1lot_book_gap_ms: float = 0.0
        self.last_top_quote: Optional[Tuple[float, float, int, int]] = None

    def on_update(
        self,
        cohort: str,
        mono_ns: int,
        has_valid_book: bool,
        has_1lot_book: bool,
        top_quote: Optional[Tuple[float, float, int, int]],
    ) -> bool:
        self.cohort = cohort
        if self.updates == 0:
            self.first_mono_ns = mono_ns
            if self.seeded_mono_ns > 0 and mono_ns > self.seeded_mono_ns:
                init_gap = (mono_ns - self.seeded_mono_ns) / 1_000_000.0
                if init_gap > self.max_update_gap_ms:
                    self.max_update_gap_ms = init_gap
        elif mono_ns > self.last_mono_ns:
            gap_ms = (mono_ns - self.last_mono_ns) / 1_000_000.0
            if gap_ms > self.max_update_gap_ms:
                self.max_update_gap_ms = gap_ms
        self.updates += 1
        self.last_mono_ns = mono_ns

        is_distinct_book = False
        if has_valid_book:
            if self.valid_books == 0:
                if self.seeded_mono_ns > 0 and mono_ns > self.seeded_mono_ns:
                    self.max_book_gap_ms = max(self.max_book_gap_ms, (mono_ns - self.seeded_mono_ns) / 1_000_000.0)
            elif self.last_book_mono_ns > 0 and mono_ns > self.last_book_mono_ns:
                bgap_ms = (mono_ns - self.last_book_mono_ns) / 1_000_000.0
                if bgap_ms > self.max_book_gap_ms:
                    self.max_book_gap_ms = bgap_ms
            self.valid_books += 1
            self.last_book_mono_ns = mono_ns
            if top_quote is not None and top_quote != self.last_top_quote:
                self.last_top_quote = top_quote
                self.distinct_books += 1
                is_distinct_book = True

        if has_1lot_book:
            if self.usable_1lot_books == 0:
                if self.seeded_mono_ns > 0 and mono_ns > self.seeded_mono_ns:
                    self.max_1lot_book_gap_ms = max(
                        self.max_1lot_book_gap_ms, (mono_ns - self.seeded_mono_ns) / 1_000_000.0
                    )
            elif self.last_1lot_book_mono_ns > 0 and mono_ns > self.last_1lot_book_mono_ns:
                lgap_ms = (mono_ns - self.last_1lot_book_mono_ns) / 1_000_000.0
                if lgap_ms > self.max_1lot_book_gap_ms:
                    self.max_1lot_book_gap_ms = lgap_ms
            self.usable_1lot_books += 1
            self.last_1lot_book_mono_ns = mono_ns
        return is_distinct_book


class S1MeasurementSummaryExporter:
    """
    Asynchronous, hash-chained multi-session summary exporter (V3).
    Key durability invariants:
      1. Per-(boot_id, stream_gen) JSONL file (`ledger_<boot_ist_date>_<boot_id>_g<gen:02d>.jsonl`):
         crossing IST midnight within the same boot continues seamlessly in the same file without
         breaking per-file hash chains.
      2. Primary ledger append + fsync advances `_export_seq` and `_prev_record_sha256` IMMEDIATELY,
         independently of secondary `last_checkpoint` / `manifest` atomic rename hints.
      3. If primary ledger write/fsync fails mid-stream, `_stream_gen` increments so future writes
         start a clean generation file rather than appending to a potentially torn tail.
      4. Incremental `_storage_bytes_used` accounting (including manifest & checkpoint overhead)
         enforces `MAX_EXPORT_STORAGE_BYTES` (`64 MiB`) in O(1) without rescanning directories.
    """

    def __init__(
        self,
        export_dir: Optional[Path],
        boot_id: str,
        process_id: int,
        max_storage_bytes: int = MAX_EXPORT_STORAGE_BYTES,
    ) -> None:
        self._export_dir: Optional[Path] = Path(export_dir) if export_dir is not None else None
        self._boot_id: str = boot_id
        self._process_id: int = process_id
        self._boot_ist_date: str = datetime.datetime.now(IST_TZ).date().isoformat()
        self._max_storage_bytes: int = max_storage_bytes
        self._lock = threading.Lock()

        self._stream_gen: int = 1
        self._export_seq: int = 0
        self._prev_record_sha256: str = "0" * 64
        self._generation_manifest: Dict[str, Dict[str, Any]] = {}
        self._sealed_epochs_recorded: List[str] = []

        self._last_durable_seq: int = 0
        self._last_durable_utc: Optional[str] = None
        self._last_durable_mono_ns: int = 0
        self._export_failures: int = 0
        self._checkpoint_warnings: int = 0
        self._last_export_error: Optional[str] = None
        self._last_checkpoint_error: Optional[str] = None
        self._storage_bytes_used: int = 0
        self._storage_initialized: bool = False
        self._quota_exhausted: bool = False

    @property
    def configured_dir(self) -> Optional[Path]:
        return self._export_dir

    def _init_storage_bytes_unlocked(self, target_dir: Path) -> None:
        if self._storage_initialized:
            return
        total = 0
        if target_dir.exists():
            for entry in target_dir.iterdir():
                if entry.is_file():
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        pass
        self._storage_bytes_used = total
        self._storage_initialized = True

    def _atomic_write_json_unlocked(self, final_path: Path, tmp_path: Path, raw_bytes: bytes) -> int:
        old_sz = final_path.stat().st_size if final_path.exists() else 0
        tfd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            _write_all_fd(tfd, raw_bytes)
            os.fsync(tfd)
        finally:
            os.close(tfd)
        os.replace(str(tmp_path), str(final_path))
        _fsync_directory_best_effort(final_path.parent)
        return len(raw_bytes) - old_sz

    def write_record(self, record_kind: str, payload: Dict[str, Any], update_checkpoint: bool = False) -> bool:
        """Returns True iff the primary hash-chained JSONL ledger committed durably."""
        if self._export_dir is None:
            return False
        now_dt_ist = datetime.datetime.now(IST_TZ)
        t_now_utc = now_dt_ist.astimezone(datetime.timezone.utc).isoformat()
        ist_date = now_dt_ist.date().isoformat()

        with self._lock:
            try:
                self._export_dir.mkdir(parents=True, exist_ok=True)
                self._init_storage_bytes_unlocked(self._export_dir)

                next_seq = self._export_seq + 1
                ledger_name = f"ledger_{self._boot_ist_date}_{self._boot_id}_g{self._stream_gen:02d}.jsonl"
                envelope_body = {
                    "schema_version": SCHEMA_VERSION,
                    "record_kind": record_kind,
                    "stream_gen": self._stream_gen,
                    "export_seq": next_seq,
                    "exported_utc": t_now_utc,
                    "ist_session_date": ist_date,
                    "process_id": self._process_id,
                    "boot_id": self._boot_id,
                    "probe_code_sha256": PROBE_CODE_SHA256,
                    "prev_record_sha256": self._prev_record_sha256,
                    "payload": payload,
                }
                canonical_bytes = json.dumps(
                    envelope_body, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                rec_sha = hashlib.sha256(canonical_bytes).hexdigest()
                full_record = dict(envelope_body)
                full_record["record_sha256"] = rec_sha
                line_bytes = (
                    json.dumps(full_record, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8")

                if len(line_bytes) > MAX_EXPORT_RECORD_BYTES:
                    self._export_failures += 1
                    self._last_export_error = f"EXPORT_RECORD_TOO_LARGE:{len(line_bytes)}"
                    return False

                ckpt_overhead = (len(line_bytes) + 2048) if update_checkpoint else 1024
                if self._storage_bytes_used + len(line_bytes) + ckpt_overhead > self._max_storage_bytes:
                    self._quota_exhausted = True
                    self._export_failures += 1
                    self._last_export_error = "EXPORT_STORAGE_QUOTA_EXCEEDED"
                    return False

                ledger_path = self._export_dir / ledger_name
                is_new_file = not ledger_path.exists()
                lfd = os.open(str(ledger_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    _write_all_fd(lfd, line_bytes)
                    os.fsync(lfd)
                except Exception:
                    # Quarantine ambiguous tail by rotating generation for any subsequent attempt
                    self._stream_gen += 1
                    self._export_seq = 0
                    self._prev_record_sha256 = "0" * 64
                    raise
                finally:
                    os.close(lfd)
                if is_new_file:
                    _fsync_directory_best_effort(self._export_dir)

                # PRIMARY LEDGER COMMIT SUCCEEDED: advance chain state unconditionally!
                self._export_seq = next_seq
                self._prev_record_sha256 = rec_sha
                self._last_durable_seq = next_seq
                self._last_durable_utc = t_now_utc
                self._last_durable_mono_ns = time.perf_counter_ns()
                self._storage_bytes_used += len(line_bytes)
                self._last_export_error = None

                if record_kind == "EPOCH_SEALED":
                    ep_id = str((payload or {}).get("epoch_id") or "")
                    if ep_id and ep_id not in self._sealed_epochs_recorded:
                        self._sealed_epochs_recorded.append(ep_id)

                self._generation_manifest[ledger_name] = {
                    "stream_gen": self._stream_gen,
                    "expected_records": next_seq,
                    "last_record_sha256": rec_sha,
                    "last_ist_date": ist_date,
                }
            except Exception as exc:
                self._export_failures += 1
                self._last_export_error = f"{type(exc).__name__}:{str(exc)[:160]}"
                return False

            # Secondary best-effort checkpoint & manifest updates (NEVER corrupt ledger sequence on failure)
            try:
                manifest_body = {
                    "schema_version": SCHEMA_VERSION,
                    "boot_id": self._boot_id,
                    "process_id": self._process_id,
                    "updated_utc": t_now_utc,
                    "generations": self._generation_manifest,
                    "sealed_epoch_ids": list(self._sealed_epochs_recorded),
                }
                m_bytes = json.dumps(manifest_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
                m_delta = self._atomic_write_json_unlocked(
                    self._export_dir / f"manifest_{self._boot_id}.json",
                    self._export_dir / f".tmp_manifest_{self._boot_id}.json",
                    m_bytes,
                )
                self._storage_bytes_used = max(0, self._storage_bytes_used + m_delta)

                if update_checkpoint:
                    c_delta = self._atomic_write_json_unlocked(
                        self._export_dir / f"last_checkpoint_{self._boot_id}.json",
                        self._export_dir / f".tmp_ckpt_{self._boot_id}_{next_seq}.json",
                        line_bytes,
                    )
                    self._storage_bytes_used = max(0, self._storage_bytes_used + c_delta)
                self._last_checkpoint_error = None
            except Exception as ck_exc:
                self._checkpoint_warnings += 1
                self._last_checkpoint_error = f"{type(ck_exc).__name__}:{str(ck_exc)[:160]}"

            return True

    def status_snapshot(self, now_mono_ns: int) -> Dict[str, Any]:
        with self._lock:
            lag_ms = (
                round((now_mono_ns - self._last_durable_mono_ns) / 1_000_000.0, 2)
                if self._last_durable_mono_ns > 0 and now_mono_ns >= self._last_durable_mono_ns
                else None
            )
            return {
                "configured": self._export_dir is not None,
                "export_dir": str(self._export_dir) if self._export_dir is not None else None,
                "stream_gen": self._stream_gen,
                "last_durable_seq": self._last_durable_seq,
                "last_durable_utc": self._last_durable_utc,
                "export_lag_ms": lag_ms,
                "export_failures": self._export_failures,
                "checkpoint_warnings": self._checkpoint_warnings,
                "quota_exhausted": self._quota_exhausted,
                "storage_bytes_used": self._storage_bytes_used,
                "max_storage_bytes": self._max_storage_bytes,
                "last_export_error": self._last_export_error,
                "last_checkpoint_error": self._last_checkpoint_error,
            }

    @staticmethod
    def load_and_verify_ledger_dir(export_dir: Path) -> Dict[str, Any]:
        """
        Verifies per-generation hash chains + boot manifests, detects missing files or truncated
        suffixes, deduplicates epochs strictly by `epoch_id`, and separates sealed epochs from
        checkpoint-only crash epochs.
        """
        merged_sealed = BoundedHistogram(LATENCY_BOUNDS_US)
        merged_with_unsealed = BoundedHistogram(LATENCY_BOUNDS_US)
        verified_records: List[Dict[str, Any]] = []
        corrupt_or_torn_lines: List[Dict[str, Any]] = []
        manifest_discrepancies: List[Dict[str, Any]] = []
        duplicate_seal_warnings: List[str] = []

        if not export_dir.exists():
            return {
                "files_scanned": 0,
                "verified_record_count": 0,
                "ist_session_dates_observed": [],
                "sealed_epoch_ids": [],
                "unsealed_crash_epoch_ids": [],
                "duplicate_seal_warnings": [],
                "manifest_discrepancies": [],
                "corrupt_or_torn_tail_lines": [],
                "minute_rollup_count": 0,
                "control_audit_count": 0,
                "merged_full_probe_added_callback_us": merged_sealed.summary(include_bins=True),
                "merged_including_unsealed_crash_epochs_us": merged_with_unsealed.summary(include_bins=True),
            }

        ledger_files = sorted(export_dir.glob("ledger_*.jsonl"))
        file_last_state: Dict[str, Tuple[int, str]] = {}
        ist_dates: Set[str] = set()
        sealed_payload_by_epoch: Dict[str, Dict[str, Any]] = {}
        latest_ckpt_by_epoch: Dict[str, Dict[str, Any]] = {}
        minute_rollups: int = 0
        control_audit_count: int = 0

        for lf in ledger_files:
            prev_sha = "0" * 64
            expected_seq = 1
            try:
                raw_lines = lf.read_bytes().splitlines()
            except Exception as exc:
                corrupt_or_torn_lines.append({"file": lf.name, "line": 0, "reason": f"READ_ERR:{exc}"})
                continue

            for line_no, raw_line in enumerate(raw_lines, start=1):
                if not raw_line.strip():
                    continue
                try:
                    rec = json.loads(raw_line.decode("utf-8"))
                    claimed_sha = rec.pop("record_sha256", None)
                    recomputed = hashlib.sha256(
                        json.dumps(rec, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest()
                    if (
                        not claimed_sha
                        or claimed_sha != recomputed
                        or rec.get("prev_record_sha256") != prev_sha
                        or int(rec.get("export_seq", -1)) != expected_seq
                    ):
                        corrupt_or_torn_lines.append(
                            {"file": lf.name, "line": line_no, "reason": "HASH_OR_SEQ_MISMATCH"}
                        )
                        break
                    prev_sha = claimed_sha
                    expected_seq += 1
                    rec["record_sha256"] = claimed_sha
                    verified_records.append(rec)
                    file_last_state[lf.name] = (expected_seq - 1, claimed_sha)

                    s_date = rec.get("ist_session_date")
                    if s_date:
                        ist_dates.add(str(s_date))

                    rk = rec.get("record_kind")
                    pl = rec.get("payload") or {}
                    control_audit_count += len(pl.get("control_audit_delta") or ())
                    if rk == "MINUTE_ROLLUP":
                        minute_rollups += 1
                    elif rk == "PERIODIC_CHECKPOINT":
                        ep_id = str(pl.get("epoch_id") or "")
                        if ep_id:
                            latest_ckpt_by_epoch[ep_id] = pl
                    elif rk == "EPOCH_SEALED":
                        ep_id = str(pl.get("epoch_id") or "")
                        if ep_id:
                            if ep_id in sealed_payload_by_epoch:
                                duplicate_seal_warnings.append(ep_id)
                            else:
                                sealed_payload_by_epoch[ep_id] = pl
                except Exception as parse_err:
                    corrupt_or_torn_lines.append(
                        {"file": lf.name, "line": line_no, "reason": f"TORN_OR_INVALID_JSON:{type(parse_err).__name__}"}
                    )
                    break

        # Cross-check manifests to catch missing whole files or truncated suffixes
        for mf in sorted(export_dir.glob("manifest_*.json")):
            try:
                mdata = json.loads(mf.read_text(encoding="utf-8"))
                for fname, ginfo in (mdata.get("generations") or {}).items():
                    exp_n = int(ginfo.get("expected_records", 0))
                    exp_sha = str(ginfo.get("last_record_sha256") or "")
                    actual = file_last_state.get(fname)
                    if actual is None:
                        manifest_discrepancies.append({"manifest": mf.name, "file": fname, "reason": "MISSING_LEDGER_FILE"})
                    elif actual[0] < exp_n or actual[1] != exp_sha:
                        manifest_discrepancies.append(
                            {
                                "manifest": mf.name,
                                "file": fname,
                                "reason": "TRUNCATED_OR_MISMATCHED_LEDGER_SUFFIX",
                                "expected_seq": exp_n,
                                "actual_seq": actual[0],
                            }
                        )
            except Exception as m_err:
                manifest_discrepancies.append({"manifest": mf.name, "reason": f"CORRUPT_MANIFEST:{type(m_err).__name__}"})

        sealed_ids = sorted(sealed_payload_by_epoch.keys())
        unsealed_ids = sorted(ep for ep in latest_ckpt_by_epoch.keys() if ep not in sealed_payload_by_epoch)

        for ep_id in sealed_ids:
            lat_b = (sealed_payload_by_epoch[ep_id].get("latencies_us") or {}).get("full_probe_added_callback_us")
            if isinstance(lat_b, dict) and "bin_counts" in lat_b:
                merged_sealed.merge_from_dict(lat_b)
                merged_with_unsealed.merge_from_dict(lat_b)

        for ep_id in unsealed_ids:
            lat_b = (latest_ckpt_by_epoch[ep_id].get("latencies_us") or {}).get("full_probe_added_callback_us")
            if isinstance(lat_b, dict) and "bin_counts" in lat_b:
                merged_with_unsealed.merge_from_dict(lat_b)

        return {
            "files_scanned": len(ledger_files),
            "verified_record_count": len(verified_records),
            "ist_session_dates_observed": sorted(ist_dates),
            "sealed_epoch_ids": sealed_ids,
            "unsealed_crash_epoch_ids": unsealed_ids,
            "duplicate_seal_warnings": duplicate_seal_warnings,
            "manifest_discrepancies": manifest_discrepancies,
            "corrupt_or_torn_tail_lines": corrupt_or_torn_lines,
            "minute_rollup_count": minute_rollups,
            "control_audit_count": control_audit_count,
            "merged_full_probe_added_callback_us": merged_sealed.summary(include_bins=True),
            "merged_including_unsealed_crash_epochs_us": merged_with_unsealed.summary(include_bins=True),
        }


class _TokenBucketLimiter:
    def __init__(self, rate_per_sec: float, capacity: float) -> None:
        self._rate = rate_per_sec
        self._capacity = capacity
        self._tokens = capacity
        self._last_ns = time.perf_counter_ns()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now_ns = time.perf_counter_ns()
        with self._lock:
            elapsed = max(0.0, (now_ns - self._last_ns) / 1_000_000_000.0)
            self._last_ns = now_ns
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class S1ProbeManagementGuard:
    """
    Loopback-only RBAC guard for S1 probe endpoints:
      - Enforces direct loopback peer, rejects proxy headers, rejects Cookies/CSRF/non-local Origin.
      - Validates configured SHA-256 hashes as strict 64-hex strings; fails closed (`503`) if absent/malformed.
      - Emergency `disable` (`is_emergency_disable=True`) is NEVER blocked by `_write_limiter`!
    """

    def __init__(self) -> None:
        self._read_limiter = _TokenBucketLimiter(rate_per_sec=0.5, capacity=6.0)
        self._write_limiter = _TokenBucketLimiter(rate_per_sec=0.2, capacity=3.0)

    @staticmethod
    def _extract_bearer_token(headers: Dict[str, str]) -> Optional[str]:
        auth_val = headers.get("authorization") or ""
        if auth_val.lower().startswith("bearer "):
            tok = auth_val[7:].strip()
            if 16 <= len(tok) <= 256:
                return tok
        alt_tok = (headers.get("x-s1-probe-token") or "").strip()
        if 16 <= len(alt_tok) <= 256:
            return alt_tok
        return None

    def authorize_request(
        self,
        *,
        client_host: Optional[str],
        headers: Dict[str, str],
        require_write: bool,
        is_emergency_disable: bool = False,
    ) -> str:
        norm_headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

        for fwd_hdr in ("x-forwarded-for", "x-real-ip", "forwarded"):
            if fwd_hdr in norm_headers:
                raise ProbeControlError(403, "PROXY_HEADER_FORBIDDEN", "Forwarded proxy requests are forbidden")

        peer = (client_host or "").strip().lower()
        if peer not in LOOPBACK_HOSTS:
            raise ProbeControlError(403, "NON_LOOPBACK_PEER", "Management endpoints restricted to loopback peer")

        if "cookie" in norm_headers:
            raise ProbeControlError(403, "COOKIE_AUTH_FORBIDDEN", "Ambient Cookie headers are rejected")
        sec_site = norm_headers.get("sec-fetch-site", "").strip().lower()
        if sec_site and sec_site not in ("none", "same-origin"):
            raise ProbeControlError(403, "CROSS_SITE_FORBIDDEN", "Cross-site browser requests are forbidden")
        origin = norm_headers.get("origin", "").strip()
        if origin and origin not in DEFAULT_ALLOWED_LOCAL_ORIGINS:
            raise ProbeControlError(403, "UNTRUSTED_ORIGIN", "Origin not allowlisted for local probe management")

        admin_hash = os.environ.get("S1_PROBE_ADMIN_TOKEN_SHA256", "").strip().lower()
        read_hash = os.environ.get("S1_PROBE_READ_TOKEN_SHA256", "").strip().lower()
        admin_valid = bool(_HEX64_RE.match(admin_hash))
        read_valid = bool(_HEX64_RE.match(read_hash))

        if not admin_valid and (require_write or not read_valid):
            raise ProbeControlError(
                503,
                "PROBE_AUTH_UNCONFIGURED",
                "Valid 64-hex S1 probe token SHA-256 is not configured; access denied (fail-closed)",
            )

        raw_token = self._extract_bearer_token(norm_headers)
        if not raw_token:
            raise ProbeControlError(401, "MISSING_BEARER_TOKEN", "Missing or malformed management bearer token")

        presented_sha = hashlib.sha256(raw_token.encode("utf-8")).hexdigest().lower()
        is_admin = admin_valid and hmac.compare_digest(presented_sha, admin_hash)
        is_reader = read_valid and hmac.compare_digest(presented_sha, read_hash)

        if require_write:
            if not is_admin:
                if is_reader:
                    raise ProbeControlError(403, "INSUFFICIENT_ROLE", "Write control requires ADMIN token")
                raise ProbeControlError(403, "INVALID_TOKEN", "Invalid management bearer token")
            # CRITICAL PRIORITY 4: Authenticated ADMIN emergency disable CANNOT be rate-limited!
            if not is_emergency_disable and not self._write_limiter.allow():
                raise ProbeControlError(429, "RATE_LIMITED_CONTROL", "Probe control rate limit exceeded")
            return "ADMIN"

        if not (is_admin or is_reader):
            raise ProbeControlError(403, "INVALID_TOKEN", "Invalid management bearer token")
        if not self._read_limiter.allow():
            raise ProbeControlError(429, "RATE_LIMITED_READ", "Probe summary read rate limit exceeded")
        return "ADMIN" if is_admin else "READ"


class S1IngressMeasurementProbe:
    """
    Phase 1 (V3) S1 Ingress Measurement Probe.
      - Zero callback locks, zero callback wakeups (`Event.set()` eliminated).
      - Complete entry-to-exit callback timing including `_inbox.append` tail.
      - Immutable `bytes` wire handoff (`<= 256` frames, `<= 4 MiB` actual RAM).
      - Separate coalesced control state outside `_inbox` + worker death/stall watchdog.
    """

    def __init__(
        self,
        export_dir: Optional[Path] = None,
        require_streamer_attached_default: bool = True,
    ) -> None:
        self._probe_id: str = uuid.uuid4().hex[:12]
        self._boot_id: str = uuid.uuid4().hex[:16]
        self._process_id: int = os.getpid()
        self._boot_utc: str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._require_streamer_attached_default: bool = bool(require_streamer_attached_default)

        if export_dir is None:
            env_dir = os.environ.get("S1_PROBE_EXPORT_DIR", "").strip()
            if env_dir:
                export_dir = Path(env_dir)
            else:
                export_dir = Path(__file__).resolve().parent.parent.parent / "data" / "s1_probe_summaries"
        self._exporter = S1MeasurementSummaryExporter(
            export_dir=export_dir, boot_id=self._boot_id, process_id=self._process_id
        )

        # Startup recovery scan of prior boots (lightweight verification summary)
        try:
            self._startup_recovery_summary: Dict[str, Any] = S1MeasurementSummaryExporter.load_and_verify_ledger_dir(
                export_dir
            )
            # Omit bulky bin arrays in cached startup status
            self._startup_recovery_summary.pop("merged_full_probe_added_callback_us", None)
            self._startup_recovery_summary.pop("merged_including_unsealed_crash_epochs_us", None)
        except Exception:
            self._startup_recovery_summary = {"files_scanned": 0, "error": "STARTUP_SCAN_FAILED"}

        self._control_lock = threading.Lock()
        self._universe_supplier: Optional[Callable[[], Tuple[Set[str], Dict[str, Dict[str, Any]]]]] = None
        self._attachment_verifier: Optional[Callable[[], Dict[str, Any]]] = None
        self._registered_streamers: List[weakref.ReferenceType[Any]] = []
        self._control_audit_ring: Deque[Dict[str, Any]] = collections.deque(maxlen=MAX_CONTROL_AUDIT_RING)
        self._control_audit_exported_idx: int = 0
        self._control_audit_total_seq: int = 0

        # Lock-free hot-path state
        self._capture_enabled: bool = False
        self._active_epoch_seq: int = 0
        self._frame_seq_counter: int = 0
        self._tls = threading.local()
        self._inbox: Deque[Any] = collections.deque()

        self._enqueued_wire_bytes_total: int = 0
        self._dequeued_wire_bytes_total: int = 0
        self._inbox_overflow_drops: int = 0
        self._inbox_overflow_bytes: int = 0
        self._stage_event_drops: int = 0
        self._lifecycle_event_drops: int = 0
        self._post_disable_inflight_drops: int = 0
        self._control_coalesced_count: int = 0
        self._observer_boundary_faults: int = 0

        # Rolling O(1) latency circuit breaker state
        self._consecutive_callback_faults: int = 0
        self._consecutive_latency_breaches: int = 0
        self._severe_latency_stalls: int = 0
        self._rolling_gt100_ring: List[int] = [0] * ROLLING_LATENCY_WINDOW
        self._rolling_gt1000_ring: List[int] = [0] * ROLLING_LATENCY_WINDOW
        self._rolling_idx: int = 0
        self._rolling_count: int = 0
        self._rolling_gt100_sum: int = 0
        self._rolling_gt1000_sum: int = 0
        self._auto_disabled_on_fault: bool = False
        self._auto_disabled_reason: Optional[str] = None

        # Coalesced control mailbox (NEVER enters `_inbox`!)
        self._pending_enable_epoch: Optional[Tuple[int, str, int, threading.Event]] = None
        self._pending_seal_cmd: Optional[Tuple[int, str, str, int, threading.Event]] = None
        self._pending_reset_cmd: Optional[Tuple[int, threading.Event]] = None
        self._pending_sync_barrier: Optional[Tuple[bool, threading.Event]] = None

        # Worker lifecycle & heartbeat watchdog
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_stop: bool = False
        self._worker_alive_flag: bool = False
        self._worker_dead_latched: bool = False
        self._worker_last_heartbeat_mono_ns: int = 0

        self._init_aggregator_state(epoch_seq=0)
        self._published_snapshot: Dict[str, Any] = self._build_snapshot_from_aggregator(include_rings=True)
        self._published_compact: Dict[str, Any] = self._build_compact_from_snapshot(self._published_snapshot)

    # ──────────────────────────────────────────────────────────────────────────
    # AGGREGATOR-OWNED STATE INITIALIZATION
    # ──────────────────────────────────────────────────────────────────────────

    def _init_aggregator_state(self, epoch_seq: int) -> None:
        now_ms = int(time.time() * 1000)
        now_mono = time.perf_counter_ns()
        self._agg_epoch_seq: int = epoch_seq
        self._agg_epoch_id: str = f"{self._boot_id}_ep{epoch_seq:04d}"
        self._agg_epoch_closed: bool = (epoch_seq == 0)
        self._agg_sealed_for_epoch_seq: int = 0
        self._agg_epoch_durable: bool = True
        self._agg_created_ms: int = now_ms
        self._agg_enabled_since_utc: Optional[str] = None
        self._agg_disabled_at_utc: Optional[str] = None
        self._agg_active_start_mono_ns: int = 0
        self._agg_accumulated_enabled_ns: int = 0
        self._agg_connected_state: bool = True
        self._agg_disconnected_since_mono_ns: int = 0
        self._agg_accumulated_disconnected_ns: int = 0

        self._agg_control_intervals: Deque[Dict[str, Any]] = collections.deque(maxlen=MAX_CONTROL_INTERVALS)
        self._agg_control_intervals_evicted: int = 0

        self._agg_coverage_valid: bool = True
        self._agg_invalidation_reasons: Set[str] = set()
        self._agg_clock_certification_valid: bool = True
        self._agg_clock_step_events: int = 0
        self._agg_prev_wall_ns: int = 0
        self._agg_prev_mono_ns: int = 0

        self._agg_first_frame_ms: Optional[int] = None
        self._agg_last_frame_ms: Optional[int] = None
        self._agg_first_frame_mono_ns: int = 0
        self._agg_last_frame_mono_ns: int = 0
        self._agg_last_exported_frames: int = 0
        self._agg_last_checkpoint_ns: int = now_mono
        self._agg_last_snapshot_pub_ns: int = 0

        # Pre-decode ingress & error accounting
        self._agg_ingress_attempts: int = 0
        self._agg_ingress_wire_bytes: int = 0
        self._agg_non_bytes_frames: int = 0
        self._agg_decoded_ok_frames: int = 0
        self._agg_sdk_decode_errors: int = 0
        self._agg_sdk_dict_errors: int = 0
        self._agg_downstream_emit_errors: int = 0
        self._agg_incomplete_frames_discarded: int = 0
        self._agg_frames_gt_64kib: int = 0
        self._agg_frames_gt_1mib: int = 0

        self._agg_total_instrument_updates: int = 0
        self._agg_s1_relevant_frames: int = 0
        self._agg_s1_relevant_updates: int = 0
        self._agg_s1_option_valid_books: int = 0
        self._agg_s1_option_distinct_books: int = 0
        self._agg_s1_option_1lot_books: int = 0
        self._agg_s1_non_option_books: int = 0

        self._agg_wire_enum_type_counts: Dict[str, int] = {
            "initial_feed": 0,
            "live_feed": 0,
            "market_info": 0,
            "unknown": 0,
        }
        self._agg_dict_type_omitted_enum0: int = 0
        self._agg_frames_with_current_ts: int = 0
        self._agg_frames_missing_current_ts: int = 0

        # Universe & contract revision tracking
        self._agg_last_universe_refresh_ns: int = 0
        self._agg_effective_refresh_interval_ns: int = MIN_UNIVERSE_REFRESH_INTERVAL_NS
        self._agg_universe_refresh_interval_ns: int = MIN_UNIVERSE_REFRESH_INTERVAL_NS
        self._agg_supplier_slow_warnings: int = 0
        self._agg_consecutive_worker_faults: int = 0
        self._agg_consecutive_supplier_faults: int = 0
        self._agg_worker_internal_errors: int = 0
        self._agg_universe_epoch_seq: int = 0
        self._agg_contract_rev_seq: int = 0
        self._agg_current_subscribed_keys: Set[str] = set()
        self._agg_current_meta_sig: int = 0
        self._agg_seen_in_current_u_epoch: Set[str] = set()
        self._agg_u_epoch_seen_core: Set[str] = set()
        self._agg_u_epoch_seen_opt_1_7: Set[str] = set()
        self._agg_u_epoch_seen_opt_other: Set[str] = set()
        self._agg_u_epoch_seen_opt_1lot: Set[str] = set()
        self._agg_universe_epochs: Deque[Dict[str, Any]] = collections.deque(maxlen=MAX_UNIVERSE_EPOCHS_RING)
        self._agg_current_contract_meta: Dict[str, ContractIdentityMeta] = {}
        self._agg_historical_identity: collections.OrderedDict[str, ContractIdentityMeta] = collections.OrderedDict()
        self._agg_dte_fallback_count: int = 0
        self._agg_oversized_key_drops: int = 0
        self._agg_seen_key_cap_drops: int = 0
        self._agg_opt_cohort_key_cap_drops: int = 0
        self._agg_per_inst_key_cap_drops: int = 0
        self._agg_universe_snapshot_cap_drops: int = 0
        self._agg_delivered_outside_subscribed_count: int = 0

        self._agg_seen_all_keys: Set[str] = set()
        self._agg_seen_s1_core_keys: Set[str] = set()
        self._agg_seen_opt_1_7_keys: Set[str] = set()
        self._agg_seen_opt_other_keys: Set[str] = set()
        self._agg_seen_opt_valid_book_keys: Set[str] = set()
        self._agg_seen_opt_1lot_book_keys: Set[str] = set()

        self._agg_cohorts: Dict[str, CohortCounters] = {name: CohortCounters() for name in COHORT_NAMES}
        self._agg_per_s1_inst: Dict[str, PerInstrumentStats] = {}
        self._agg_stage_hooks_by_seq: collections.OrderedDict[int, int] = collections.OrderedDict()

        # Histograms
        self._agg_hist_frame_bytes = BoundedHistogram(FRAME_SIZE_BOUNDS_BYTES)
        self._agg_hist_inst_per_frame = BoundedHistogram(INSTRUMENTS_PER_FRAME_BOUNDS)
        self._agg_hist_provider_lag_ms = BoundedHistogram(CLOCK_LAG_BOUNDS_MS)
        self._agg_hist_100ms_nonempty_frames = BoundedHistogram(MSG_RATE_BOUNDS_PER_SEC)
        self._agg_hist_100ms_nonempty_bytes = BoundedHistogram(BYTE_RATE_BOUNDS_PER_SEC)
        self._agg_hist_1s_all_frames = BoundedHistogram(MSG_RATE_BOUNDS_PER_SEC)
        self._agg_hist_1s_all_bytes = BoundedHistogram(BYTE_RATE_BOUNDS_PER_SEC)

        self._agg_hist_sdk_decode_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_sdk_dict_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_app_downstream_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_raw_downstream_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_listener_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_equity_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_stage_hook_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_ingress_handoff_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_full_probe_added_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_total_callback_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_worker_slice_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_bar_flush_15m_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_freeze_snapshot_us = BoundedHistogram(LATENCY_BOUNDS_US)

        # Rolling buckets
        self._agg_cur_100ms_slot: int = -1
        self._agg_cur_100ms_frames: int = 0
        self._agg_cur_100ms_bytes: int = 0
        self._agg_cur_100ms_insts: int = 0
        self._agg_cur_100ms_opt_books: int = 0
        self._agg_confirmed_zero_100ms_bins: int = 0
        self._agg_peak_100ms_frames: int = 0
        self._agg_peak_100ms_bytes: int = 0
        self._agg_peak_100ms_insts: int = 0
        self._agg_peak_100ms_opt_books: int = 0
        self._agg_ring_100ms_evictions: int = 0
        self._agg_ring_100ms: Deque[Dict[str, Any]] = collections.deque(maxlen=RING_100MS_MAXLEN)

        self._agg_cur_1s_slot: int = -1
        self._agg_cur_1s_frames: int = 0
        self._agg_cur_1s_bytes: int = 0
        self._agg_cur_1s_insts: int = 0
        self._agg_cur_1s_s1_updates: int = 0
        self._agg_cur_1s_opt_books: int = 0
        self._agg_confirmed_zero_1s_bins: int = 0
        self._agg_peak_1s_frames: int = 0
        self._agg_peak_1s_bytes: int = 0
        self._agg_peak_1s_insts: int = 0
        self._agg_peak_1s_s1_updates: int = 0
        self._agg_peak_1s_opt_books: int = 0
        self._agg_ring_1s_evictions: int = 0
        self._agg_ring_1s: Deque[Dict[str, Any]] = collections.deque(maxlen=RING_1S_MAXLEN)

        self._agg_cur_1m_slot: int = -1
        self._agg_cur_1m_frames: int = 0
        self._agg_cur_1m_bytes: int = 0
        self._agg_cur_1m_insts: int = 0
        self._agg_cur_1m_s1_updates: int = 0
        self._agg_cur_1m_opt_books: int = 0
        self._agg_ring_1m_evictions: int = 0
        self._agg_ring_1m: Deque[Dict[str, Any]] = collections.deque(maxlen=RING_1M_MAXLEN)

        # Lifecycle events
        self._agg_ws_open_count: int = 0
        self._agg_ws_close_count: int = 0
        self._agg_ws_error_count: int = 0
        self._agg_ws_reconnecting_count: int = 0
        self._agg_ws_auto_reconnect_stopped_count: int = 0
        self._agg_last_disconnect_mono_ns: int = 0
        self._agg_max_disconnect_gap_ms: float = 0.0
        self._agg_lifecycle_evictions: int = 0
        self._agg_lifecycle_events: Deque[Dict[str, Any]] = collections.deque(maxlen=LIFECYCLE_RING_MAXLEN)

    # ──────────────────────────────────────────────────────────────────────────
    # ATTACHMENT & UNIVERSE HOOKS
    # ──────────────────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._capture_enabled

    def next_frame_seq(self) -> int:
        self._frame_seq_counter += 1
        return self._frame_seq_counter

    def set_universe_supplier(
        self, supplier: Callable[[], Tuple[Set[str], Dict[str, Dict[str, Any]]]]
    ) -> None:
        self._universe_supplier = supplier

    def set_attachment_verifier(self, verifier: Callable[[], Dict[str, Any]]) -> None:
        self._attachment_verifier = verifier

    def register_streamer_instance(self, streamer: Any) -> None:
        try:
            with self._control_lock:
                alive = [r for r in self._registered_streamers if r() is not None]
                alive.append(weakref.ref(streamer))
                self._registered_streamers = alive[-8:]
        except Exception:
            pass

    def inspect_attachment(self) -> Dict[str, Any]:
        if self._attachment_verifier is not None:
            try:
                info = dict(self._attachment_verifier() or {})
                info.setdefault("probe_id", self._probe_id)
                info.setdefault("boot_id", self._boot_id)
                info.setdefault("probe_code_sha256", PROBE_CODE_SHA256)
                info.setdefault("wrapper_version_expected", PROBE_WRAPPER_VERSION)
                return info
            except Exception as exc:
                return {
                    "wrapper_attached": False,
                    "error": f"{type(exc).__name__}:{str(exc)[:120]}",
                    "probe_id": self._probe_id,
                    "boot_id": self._boot_id,
                    "probe_code_sha256": PROBE_CODE_SHA256,
                }
        live_wrappers = []
        with self._control_lock:
            for ref in self._registered_streamers:
                obj = ref()
                if (
                    obj is not None
                    and getattr(obj, "_s1_probe", None) is self
                    and getattr(obj, "_s1_probe_wrapper_version", None) == PROBE_WRAPPER_VERSION
                ):
                    live_wrappers.append(type(obj).__name__)
        return {
            "wrapper_attached": len(live_wrappers) > 0,
            "attached_streamer_classes": live_wrappers,
            "probe_id": self._probe_id,
            "boot_id": self._boot_id,
            "probe_code_sha256": PROBE_CODE_SHA256,
            "wrapper_version_expected": PROBE_WRAPPER_VERSION,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # MANAGEMENT CONTROLS (COALESCED MAILBOX OUTSIDE `_inbox` — NEVER OVERFLOWS)
    # ──────────────────────────────────────────────────────────────────────────

    def _ensure_worker_started_unlocked(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._worker_stop = False
        self._worker_dead_latched = False
        self._worker_last_heartbeat_mono_ns = time.perf_counter_ns()
        self._worker_alive_flag = True
        t = threading.Thread(
            target=self._worker_loop_guarded,
            name=f"S1ProbeAggregator-{self._probe_id}",
            daemon=True,
        )
        self._worker_thread = t
        t.start()

    def _reset_rolling_latency_breaker(self) -> None:
        self._consecutive_callback_faults = 0
        self._consecutive_latency_breaches = 0
        self._severe_latency_stalls = 0
        for i in range(ROLLING_LATENCY_WINDOW):
            self._rolling_gt100_ring[i] = 0
            self._rolling_gt1000_ring[i] = 0
        self._rolling_idx = 0
        self._rolling_count = 0
        self._rolling_gt100_sum = 0
        self._rolling_gt1000_sum = 0

    def record_control_audit(
        self,
        *,
        action: str,
        actor_role: str,
        peer: str,
        outcome: str,
        detail: Optional[str] = None,
    ) -> None:
        self._control_audit_total_seq += 1
        entry = {
            "audit_seq": self._control_audit_total_seq,
            "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "action": str(action)[:48],
            "actor_role": str(actor_role)[:24],
            "peer": str(peer)[:64],
            "outcome": str(outcome)[:48],
            "epoch_seq": self._active_epoch_seq,
            "detail": str(detail)[:160] if detail else None,
        }
        self._control_audit_ring.append(entry)

    def enable(
        self,
        *,
        require_attached_streamer: Optional[bool] = None,
        actor_role: str = "INTERNAL",
        peer: str = "local",
    ) -> Dict[str, Any]:
        req_attach = (
            self._require_streamer_attached_default
            if require_attached_streamer is None
            else bool(require_attached_streamer)
        )
        attach_info = self.inspect_attachment()
        if req_attach and not attach_info.get("wrapper_attached", False):
            self.record_control_audit(
                action="enable",
                actor_role=actor_role,
                peer=peer,
                outcome="REJECTED_UNATTACHED",
                detail="Active streamer does not carry S1 wrapper",
            )
            raise ProbeControlError(
                409,
                "STREAMER_WRAPPER_NOT_ATTACHED",
                "Cannot enable probe: active Upstox streamer does not have InstrumentedMarketDataStreamerV3 attached",
            )

        with self._control_lock:
            if not self._capture_enabled:
                if not self._agg_epoch_durable and self._agg_ingress_attempts > 0:
                    raise ProbeControlError(
                        409,
                        "PREVIOUS_EPOCH_UNEXPORTED",
                        "Cannot start a new epoch while prior non-empty epoch failed durable export",
                    )
                self._ensure_worker_started_unlocked()
                next_ep = self._active_epoch_seq + 1
                self._active_epoch_seq = next_ep
                self._reset_rolling_latency_breaker()
                self._auto_disabled_on_fault = False
                self._auto_disabled_reason = None
                done_evt = threading.Event()
                self._pending_enable_epoch = (
                    next_ep,
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    time.perf_counter_ns(),
                    done_evt,
                )
                done_evt.wait(timeout=0.5)
                if not done_evt.is_set() or self._worker_dead_latched:
                    self._capture_enabled = False
                    raise ProbeControlError(500, "WORKER_INIT_FAILED", "Probe worker failed during enable")
                self._capture_enabled = True

        self.record_control_audit(action="enable", actor_role=actor_role, peer=peer, outcome="ENABLED")
        return {
            "enabled": self._capture_enabled,
            "epoch_seq": self._active_epoch_seq,
            "attachment": attach_info,
        }

    def disable(
        self,
        *,
        flush_timeout_s: float = 0.5,
        actor_role: str = "INTERNAL",
        peer: str = "local",
        reason: str = "MANUAL_DISABLE",
    ) -> Dict[str, Any]:
        # Flip capture flag IMMEDIATELY with zero lock acquisition!
        self._capture_enabled = False
        seal_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        seal_mono_ns = time.perf_counter_ns()
        target_ep = self._active_epoch_seq

        seal_done: Optional[threading.Event] = None
        wt = self._worker_thread
        if wt is not None and wt.is_alive() and target_ep > 0:
            existing = self._pending_seal_cmd
            if existing is not None and not existing[4].is_set():
                self._control_coalesced_count += 1
                seal_done = existing[4]
            else:
                seal_done = threading.Event()
                self._pending_seal_cmd = (target_ep, reason, seal_utc, seal_mono_ns, seal_done)
            if flush_timeout_s > 0 and seal_done is not None:
                seal_done.wait(timeout=flush_timeout_s)
        else:
            self._control_coalesced_count += 1

        self.record_control_audit(
            action="disable", actor_role=actor_role, peer=peer, outcome="DISABLED", detail=reason
        )
        durable_ok = bool(
            (target_ep == 0)
            or (
                seal_done is not None
                and seal_done.is_set()
                and self._agg_sealed_for_epoch_seq == target_ep
                and self._agg_epoch_durable
            )
        )
        return {
            "enabled": self._capture_enabled,
            "epoch_seq": target_ep,
            "sealed": durable_ok,
            "disabled_utc": seal_utc,
        }

    def reset(
        self,
        *,
        flush_timeout_s: float = 1.0,
        actor_role: str = "INTERNAL",
        peer: str = "local",
    ) -> Dict[str, Any]:
        if self._capture_enabled:
            self.record_control_audit(
                action="reset", actor_role=actor_role, peer=peer, outcome="REJECTED_WHILE_ENABLED"
            )
            raise ProbeControlError(
                409,
                "RESET_FORBIDDEN_WHILE_ENABLED",
                "Cannot reset S1 probe while enabled; disable first so the active epoch is sealed",
            )

        with self._control_lock:
            self._ensure_worker_started_unlocked()
            # Ensure active epoch (if any) is durably sealed FIRST; never discard unexported counts!
            cur_ep = self._active_epoch_seq
            if cur_ep > 0 and self._agg_sealed_for_epoch_seq != cur_ep:
                s_evt = threading.Event()
                self._pending_seal_cmd = (
                    cur_ep,
                    "SEAL_BEFORE_RESET",
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    time.perf_counter_ns(),
                    s_evt,
                )
                s_evt.wait(timeout=flush_timeout_s)

            if not self._agg_epoch_durable and self._agg_ingress_attempts > 0:
                self.record_control_audit(
                    action="reset",
                    actor_role=actor_role,
                    peer=peer,
                    outcome="REJECTED_UNEXPORTED_EPOCH",
                )
                raise ProbeControlError(
                    409,
                    "UNEXPORTED_EPOCH_CANNOT_RESET",
                    "Cannot reset probe: previous epoch failed durable export; counts preserved in memory",
                )

            r_evt = threading.Event()
            self._pending_reset_cmd = (cur_ep, r_evt)
            r_evt.wait(timeout=flush_timeout_s)

        self.record_control_audit(
            action="reset", actor_role=actor_role, peer=peer, outcome="CLEARED_AFTER_DURABLE_SEAL"
        )
        return {
            "enabled": self._capture_enabled,
            "cleared_epoch_seq": cur_ep,
            "new_epoch_seq": self._active_epoch_seq,
            "previous_epoch_sealed": bool(self._agg_epoch_durable),
        }

    def flush_for_test_or_export(self, timeout_s: float = 1.0, force_export: bool = False) -> bool:
        with self._control_lock:
            if self._worker_dead_latched:
                return False
            self._ensure_worker_started_unlocked()
            done_evt = threading.Event()
            self._pending_sync_barrier = (force_export, done_evt)
        ok = done_evt.wait(timeout=timeout_s)
        return bool(ok and not self._worker_dead_latched)

    def shutdown_worker(self, timeout_s: float = 1.0) -> None:
        self._capture_enabled = False
        self._worker_stop = True
        wt = self._worker_thread
        if wt is not None and wt.is_alive():
            wt.join(timeout=timeout_s)
        self._worker_alive_flag = False

    # ──────────────────────────────────────────────────────────────────────────
    # ZERO-LOCK, ZERO-WAKEUP LIVE CALLBACK ENTRYPOINTS
    # ──────────────────────────────────────────────────────────────────────────

    def note_observer_fault(self, boundary: str, exc: Optional[BaseException] = None) -> None:
        try:
            self._observer_boundary_faults += 1
            self._consecutive_callback_faults += 1
            if self._consecutive_callback_faults >= MAX_CONSECUTIVE_PROBE_FAULTS:
                self._capture_enabled = False
                self._auto_disabled_on_fault = True
                self._auto_disabled_reason = f"OBSERVER_FAULT:{boundary}"
        except Exception:
            self._capture_enabled = False

    def _on_callback_latency_observed_ns(self, full_added_ns: int) -> None:
        full_added_us = full_added_ns / 1000.0
        gt100 = 1 if full_added_us > LATENCY_P99_BUDGET_US else 0
        gt1000 = 1 if full_added_us > LATENCY_SINGLE_BREACH_US else 0

        idx = self._rolling_idx
        if self._rolling_count >= ROLLING_LATENCY_WINDOW:
            self._rolling_gt100_sum -= self._rolling_gt100_ring[idx]
            self._rolling_gt1000_sum -= self._rolling_gt1000_ring[idx]
        else:
            self._rolling_count += 1
        self._rolling_gt100_ring[idx] = gt100
        self._rolling_gt1000_ring[idx] = gt1000
        self._rolling_gt100_sum += gt100
        self._rolling_gt1000_sum += gt1000
        self._rolling_idx = (idx + 1) % ROLLING_LATENCY_WINDOW

        if gt1000:
            self._consecutive_latency_breaches += 1
            if full_added_us > LATENCY_SEVERE_STALL_US:
                self._severe_latency_stalls += 1
            if (
                self._consecutive_latency_breaches >= MAX_CONSECUTIVE_LATENCY_BREACHES
                or self._severe_latency_stalls >= MAX_SEVERE_LATENCY_STALLS
            ):
                self._capture_enabled = False
                self._auto_disabled_on_fault = True
                self._auto_disabled_reason = "LATENCY_BUDGET_EXCEEDED"
                return
        else:
            self._consecutive_latency_breaches = 0

        # Enforce advertised rolling p99 <= 100 us / p99.9 <= 1000 us distribution gate!
        if self._rolling_count >= MIN_ROLLING_SAMPLES_FOR_P99_TRIP:
            if (self._rolling_gt100_sum / float(self._rolling_count)) > 0.01 or self._rolling_gt1000_sum >= 2:
                self._capture_enabled = False
                self._auto_disabled_on_fault = True
                self._auto_disabled_reason = "LATENCY_DISTRIBUTION_GATE_EXCEEDED"

    def _check_worker_health_on_callback(self, now_ns: int) -> bool:
        if self._worker_dead_latched:
            self._capture_enabled = False
            self._auto_disabled_on_fault = True
            self._auto_disabled_reason = self._auto_disabled_reason or "WORKER_DEAD"
            return False
        hb = self._worker_last_heartbeat_mono_ns
        if self._worker_alive_flag and hb > 0 and (now_ns - hb) > WORKER_STALL_TIMEOUT_NS:
            self._capture_enabled = False
            self._auto_disabled_on_fault = True
            self._auto_disabled_reason = "WORKER_HEARTBEAT_STALLED"
            return False
        return True

    @staticmethod
    def _extract_immutable_dict_snapshot(data_dict: Any) -> Tuple[Optional[str], Optional[float], Tuple[Any, ...], int]:
        """
        Used ONLY when `record_ingress_frame` is invoked without `wire_payload` (e.g. unit tests).
        Extracts immutable scalar tuples immediately so caller `data_dict` can be mutated/cleared
        freely and zero nested `dict` graphs are retained in `_inbox`.
        """
        if not isinstance(data_dict, dict):
            return None, None, (), 0
        raw_type = data_dict.get("type")
        type_str = str(raw_type) if raw_type is not None else None
        cur_ts_val: Optional[float] = None
        if data_dict.get("currentTs") is not None:
            try:
                cur_ts_val = float(data_dict["currentTs"])
            except (ValueError, TypeError):
                cur_ts_val = None

        feeds = data_dict.get("feeds")
        if not isinstance(feeds, dict) or not feeds:
            return type_str, cur_ts_val, (), 0

        rows: List[Tuple[Any, ...]] = []
        oversized = 0
        for idx, (raw_k, fobj) in enumerate(feeds.items()):
            if idx >= MAX_INLINE_DICT_FEEDS:
                oversized += 1
                continue
            ikey = str(raw_k)
            if len(ikey) > MAX_KEY_CHARS:
                oversized += 1
                continue
            arm = 0
            ltp_p = 0
            ltp_pos = 0
            bk_p = 0
            bk_ne = 0
            bp = 0.0
            ap = 0.0
            bq = 0
            aq = 0
            lvls = 0
            oi_p = 0
            oi_pos = 0
            grk = 0
            i1 = 0
            cts = 0
            if isinstance(fobj, dict):
                ff = fobj.get("fullFeed")
                flwg = fobj.get("firstLevelWithGreeks")
                t_ltpc = fobj.get("ltpc")
                ltpc_d = None
                qlist = None
                fdepth = None
                ohlc_d = None
                if isinstance(ff, dict):
                    mff = ff.get("marketFF")
                    iff = ff.get("indexFF")
                    if isinstance(mff, dict):
                        arm = 1
                        ltpc_d = mff.get("ltpc")
                        ml = mff.get("marketLevel")
                        if isinstance(ml, dict):
                            for tk in UNVERIFIED_QUOTE_TS_CANDIDATE_KEYS:
                                if tk in ml:
                                    cts = 1
                                    break
                            if "bidAskQuote" in ml:
                                bk_p = 1
                                qlist = ml.get("bidAskQuote")
                        if "oi" in mff and mff.get("oi") is not None:
                            oi_p = 1
                            try:
                                if float(mff["oi"]) > 0.0:
                                    oi_pos = 1
                            except (ValueError, TypeError):
                                pass
                        if isinstance(mff.get("optionGreeks"), dict) and len(mff["optionGreeks"]) > 0:
                            grk = 1
                        ohlc_d = mff.get("marketOHLC")
                    elif isinstance(iff, dict):
                        arm = 2
                        ltpc_d = iff.get("ltpc")
                        ohlc_d = iff.get("marketOHLC")
                elif isinstance(flwg, dict):
                    arm = 3
                    ltpc_d = flwg.get("ltpc")
                    if "firstDepth" in flwg and isinstance(flwg["firstDepth"], dict):
                        bk_p = 1
                        fdepth = flwg["firstDepth"]
                    if "oi" in flwg and flwg.get("oi") is not None:
                        oi_p = 1
                        try:
                            if float(flwg["oi"]) > 0.0:
                                oi_pos = 1
                        except (ValueError, TypeError):
                            pass
                    if isinstance(flwg.get("optionGreeks"), dict) and len(flwg["optionGreeks"]) > 0:
                        grk = 1
                elif isinstance(t_ltpc, dict):
                    arm = 4
                    ltpc_d = t_ltpc

                if isinstance(ltpc_d, dict) and "ltp" in ltpc_d and ltpc_d.get("ltp") is not None:
                    ltp_p = 1
                    try:
                        lv = float(ltpc_d["ltp"])
                        if math.isfinite(lv) and lv > 0.0:
                            ltp_pos = 1
                    except (ValueError, TypeError):
                        pass

                top_q = None
                if isinstance(qlist, list) and len(qlist) > 0 and isinstance(qlist[0], dict):
                    bk_ne = 1
                    top_q = qlist[0]
                    lvls = len(qlist)
                elif isinstance(fdepth, dict) and len(fdepth) > 0:
                    bk_ne = 1
                    top_q = fdepth
                    lvls = 1

                if top_q is not None:
                    for tk in UNVERIFIED_QUOTE_TS_CANDIDATE_KEYS:
                        if tk in top_q:
                            cts = 1
                            break
                    try:
                        bp = float(top_q.get("bidP") or top_q.get("bid_price") or 0.0)
                        ap = float(top_q.get("askP") or top_q.get("ask_price") or 0.0)
                        bq = int(top_q.get("bidQ") or top_q.get("bid_qty") or 0)
                        aq = int(top_q.get("askQ") or top_q.get("ask_qty") or 0)
                    except (ValueError, TypeError):
                        bp, ap, bq, aq = 0.0, 0.0, 0, 0

                if isinstance(ohlc_d, dict) and isinstance(ohlc_d.get("ohlc"), list):
                    for ci in ohlc_d["ohlc"]:
                        if isinstance(ci, dict) and ci.get("interval") == "I1":
                            i1 = 1
                            break

            flags = (
                (arm & 0x7)
                | (ltp_p << 3)
                | (ltp_pos << 4)
                | (bk_p << 5)
                | (bk_ne << 6)
                | (oi_p << 7)
                | (oi_pos << 8)
                | (grk << 9)
                | (i1 << 10)
                | (cts << 11)
                | (min(lvls, 255) << 12)
            )
            if bk_ne:
                rows.append((ikey, flags, bp, ap, bq, aq))
            else:
                rows.append((ikey, flags))
        return type_str, cur_ts_val, tuple(rows), oversized

    def record_ingress_error_attempt(
        self,
        *,
        stage: str,
        raw_bytes: int,
        is_bytes: bool,
        recv_wall_ns: int,
        t0_ns: int,
        wire_type_int: int = -1,
        data_dict: Any = None,
    ) -> None:
        if not self._capture_enabled:
            return
        try:
            if not self._check_worker_health_on_callback(t0_ns):
                return
            ep = self._active_epoch_seq
            b_len = raw_bytes if (is_bytes and raw_bytes > 0) else 0
            in_flight_bytes = self._enqueued_wire_bytes_total - self._dequeued_wire_bytes_total
            if len(self._inbox) >= MAX_INBOX_SAMPLES or (in_flight_bytes + b_len) > MAX_INBOX_WIRE_BYTES:
                self._inbox_overflow_drops += 1
                self._inbox_overflow_bytes += b_len
                return
            self._enqueued_wire_bytes_total += b_len
            self._inbox.append((_EVT_FRAME_ERR, ep, stage, b_len, is_bytes, wire_type_int, recv_wall_ns, t0_ns))
        except Exception as exc:
            self.note_observer_fault("record_ingress_error_attempt", exc)

    def record_ingress_frame(
        self,
        *,
        raw_bytes: int,
        is_bytes: bool,
        wire_type_int: int = -1,
        data_dict: Any = None,
        wire_payload: Optional[bytes] = None,
        frame_seq: int = 0,
        dict_omitted_type_enum0: bool = False,
        recv_wall_ns: int = 0,
        recv_wall_ms: int = 0,
        t0_ns: int,
        t1_ns: int,
        t2_ns: int,
        t3_ns: int,
        pre_overhead_ns: int = 0,
    ) -> None:
        t_cb0 = time.perf_counter_ns()
        if not self._capture_enabled:
            self._post_disable_inflight_drops += 1
            return
        try:
            if not self._check_worker_health_on_callback(t_cb0):
                return
            ep = self._active_epoch_seq
            if frame_seq <= 0:
                frame_seq = self.next_frame_seq()
            wall_ns = recv_wall_ns if recv_wall_ns > 0 else (recv_wall_ms * 1_000_000)

            if wire_payload is not None:
                imm_bytes = bytes(wire_payload)
                acct_bytes = len(imm_bytes)
                payload_kind = _PAYLOAD_WIRE_BYTES
                payload_obj: Any = (imm_bytes, bool(dict_omitted_type_enum0))
            else:
                imm_snap = self._extract_immutable_dict_snapshot(data_dict)
                acct_bytes = max(raw_bytes if (is_bytes and raw_bytes > 0) else 0, len(imm_snap[2]) * 96 + 128)
                payload_kind = _PAYLOAD_FROZEN_TUPLES
                payload_obj = imm_snap

            b_wire = raw_bytes if (is_bytes and raw_bytes > 0) else (len(wire_payload) if wire_payload is not None else 0)
            in_flight_bytes = self._enqueued_wire_bytes_total - self._dequeued_wire_bytes_total
            if len(self._inbox) >= MAX_INBOX_SAMPLES or (in_flight_bytes + acct_bytes) > MAX_INBOX_WIRE_BYTES:
                self._inbox_overflow_drops += 1
                self._inbox_overflow_bytes += b_wire
                t_drop = time.perf_counter_ns()
                self._on_callback_latency_observed_ns(max(0, pre_overhead_ns + (t_drop - t_cb0)))
                return

            if not self._capture_enabled or self._active_epoch_seq != ep:
                self._post_disable_inflight_drops += 1
                return

            matched_hk_ns = 0
            matched_seg: Optional[Tuple[int, int, int]] = None
            tls_quad = getattr(self._tls, "last_stage_hook", None)
            if tls_quad is not None:
                self._tls.last_stage_hook = None
                if tls_quad[0] in (0, frame_seq):
                    if len(tls_quad) == 4:
                        matched_hk_ns = tls_quad[3]
                        matched_seg = (tls_quad[1], tls_quad[2], tls_quad[3])
                    else:
                        matched_hk_ns = tls_quad[1]

            self._enqueued_wire_bytes_total += acct_bytes
            # Preallocate mutable list cell so index 15 (`handoff_ns`) & 16 (`full_added_ns`) are populated AFTER `append`!
            cell = [
                _EVT_FRAME_OK,
                ep,
                frame_seq,
                acct_bytes,
                b_wire,
                is_bytes,
                wire_type_int,
                payload_kind,
                payload_obj,
                wall_ns,
                t0_ns,
                t1_ns,
                t2_ns,
                t3_ns,
                (matched_hk_ns, matched_seg),
                0,
                0,
            ]
            self._inbox.append(cell)
            t_cb1 = time.perf_counter_ns()
            handoff_ns = max(0, t_cb1 - t_cb0)
            full_added_ns = max(0, pre_overhead_ns + matched_hk_ns + handoff_ns)
            cell[15] = handoff_ns
            cell[16] = full_added_ns

            self._consecutive_callback_faults = 0
            self._on_callback_latency_observed_ns(full_added_ns)
        except Exception as exc:
            self.note_observer_fault("record_ingress_frame", exc)

    def record_downstream_segments(self, listener_ns: int, equity_loop_ns: int, hook_overhead_ns: int = 0) -> None:
        if not self._capture_enabled:
            return
        t_seg0 = time.perf_counter_ns()
        try:
            fseq = int(getattr(self._tls, "active_frame_seq", 0) or 0)
            if fseq > 0:
                # Zero-append inline TLS piggyback onto the enclosing frame's `_EVT_FRAME_OK` cell!
                tot_hk = max(0, hook_overhead_ns) + max(0, time.perf_counter_ns() - t_seg0)
                self._tls.last_stage_hook = (fseq, listener_ns, equity_loop_ns, tot_hk)
                return
            if len(self._inbox) < MAX_INBOX_SAMPLES:
                seg_cell = [_EVT_DOWNSTREAM_SEG, self._active_epoch_seq, fseq, listener_ns, equity_loop_ns, 0]
                self._inbox.append(seg_cell)
                tot_hk = max(0, hook_overhead_ns) + max(0, time.perf_counter_ns() - t_seg0)
                seg_cell[5] = tot_hk
                self._tls.last_stage_hook = (fseq, tot_hk)
            else:
                self._stage_event_drops += 1
                tot_hk = max(0, hook_overhead_ns) + max(0, time.perf_counter_ns() - t_seg0)
                self._tls.last_stage_hook = (fseq, tot_hk)
        except Exception as exc:
            self.note_observer_fault("record_downstream_segments", exc)

    def record_periodic_decision_stage(self, stage: str, duration_ns: int) -> None:
        if not self._capture_enabled or duration_ns < 0:
            return
        try:
            if len(self._inbox) < MAX_INBOX_SAMPLES:
                self._inbox.append((_EVT_PERIODIC_STAGE, self._active_epoch_seq, stage, duration_ns))
            else:
                self._stage_event_drops += 1
        except Exception as exc:
            self.note_observer_fault("record_periodic_decision_stage", exc)

    def record_ws_lifecycle(self, event: str, detail: Any = None) -> None:
        if not self._capture_enabled:
            return
        try:
            if len(self._inbox) < MAX_INBOX_SAMPLES:
                self._inbox.append((_EVT_LIFECYCLE, self._active_epoch_seq, str(event)[:40], detail, time.perf_counter_ns()))
            else:
                self._lifecycle_event_drops += 1
        except Exception as exc:
            self.note_observer_fault("record_ws_lifecycle", exc)

    # ──────────────────────────────────────────────────────────────────────────
    # DEEP-IMMUTABLE SNAPSHOT READERS (ZERO CONTENTION, LIVE HEALTH OVERLAY)
    # ──────────────────────────────────────────────────────────────────────────

    def get_public_minimal_status(self) -> Dict[str, Any]:
        """Minimal safe status for unauthenticated `/api/stream/status`."""
        return {
            "enabled": self._capture_enabled,
            "auto_disabled_on_fault": self._auto_disabled_on_fault,
        }

    def get_compact_status(self) -> Dict[str, Any]:
        out = copy.deepcopy(self._published_compact)
        out["enabled"] = self._capture_enabled
        out["worker_alive"] = bool(self._worker_thread is not None and self._worker_thread.is_alive())
        out["auto_disabled_on_fault"] = self._auto_disabled_on_fault
        out["auto_disabled_reason"] = self._auto_disabled_reason
        out["inbox_overflow_drops"] = self._inbox_overflow_drops
        return out

    def get_summary(self, include_recent_rings: bool = False) -> Dict[str, Any]:
        snap = self._published_snapshot
        if include_recent_rings:
            out = copy.deepcopy(snap)
        else:
            out = {
                k: copy.deepcopy(v)
                for k, v in snap.items()
                if k not in ("recent_1m_rollups", "recent_1s_rates", "recent_100ms_bursts")
            }
        worker_alive = bool(self._worker_thread is not None and self._worker_thread.is_alive() and not self._worker_dead_latched)
        out["enabled"] = self._capture_enabled
        out["auto_disabled_on_fault"] = self._auto_disabled_on_fault
        out["auto_disabled_reason"] = self._auto_disabled_reason
        fb = out["failures_and_bounds"]
        fb["worker_alive"] = worker_alive
        fb["worker_dead_latched"] = self._worker_dead_latched
        fb["auto_disabled_on_fault"] = self._auto_disabled_on_fault
        fb["auto_disabled_reason"] = self._auto_disabled_reason
        fb["inbox_pending_samples"] = len(self._inbox)
        fb["inbox_overflow_drops"] = self._inbox_overflow_drops
        fb["inbox_overflow_bytes"] = self._inbox_overflow_bytes
        fb["stage_event_drops"] = self._stage_event_drops
        fb["lifecycle_event_drops"] = self._lifecycle_event_drops
        fb["post_disable_inflight_drops"] = self._post_disable_inflight_drops
        fb["control_coalesced_count"] = self._control_coalesced_count
        if self._worker_dead_latched or self._inbox_overflow_drops > 0:
            out["delivery_coverage"]["coverage_valid"] = False
            out["delivery_coverage"]["measurement_integrity_valid"] = False
            out["codex_feasibility_metrics"]["coverage_valid"] = False
            out["codex_feasibility_metrics"]["callback_latency_gates_conservative"]["passes_p99_le_100us_and_p999_le_1000us"] = False
        out["recent_control_audit"] = copy.deepcopy(list(self._control_audit_ring)[-20:])
        return out

    # ──────────────────────────────────────────────────────────────────────────
    # BACKGROUND POLLING AGGREGATOR & EXPORTER WORKER
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _classify_dte_days_for_date(meta: ContractIdentityMeta, ist_date: datetime.date) -> Tuple[Optional[int], bool]:
        if len(meta.expiry_iso) >= 10:
            try:
                exp_date = datetime.date.fromisoformat(meta.expiry_iso[:10])
                return (exp_date - ist_date).days, False
            except ValueError:
                pass
        if meta.cached_dte is not None:
            return meta.cached_dte, True
        return None, False

    def _classify_instrument_key(
        self, ikey: str, receipt_ist_date: Optional[datetime.date] = None
    ) -> Tuple[str, int, bool, bool]:
        """Returns `(cohort_name, lot_size, lot_verified, is_nifty_option)` evaluated at `receipt_ist_date`."""
        if receipt_ist_date is None:
            receipt_ist_date = datetime.datetime.now(IST_TZ).date()
        core = S1_CORE_INSTRUMENT_KEYS.get(ikey)
        if core is not None:
            return core, 1, True, False

        cmeta = self._agg_current_contract_meta.get(ikey) or self._agg_historical_identity.get(ikey)
        if cmeta is not None:
            if cmeta.underlying != "NIFTY":
                return "NON_S1_UNIVERSE", cmeta.lot_size, cmeta.lot_verified, False
            if cmeta.is_futures:
                return "NIFTY_FUTURES", cmeta.lot_size, cmeta.lot_verified, False
            dte_days, _ = self._classify_dte_days_for_date(cmeta, receipt_ist_date)
            if dte_days is not None and 1 <= dte_days <= 7:
                return "NIFTY_OPTIONS_1_7_DTE", cmeta.lot_size, cmeta.lot_verified, True
            return "NIFTY_OPTIONS_OTHER_DTE", cmeta.lot_size, cmeta.lot_verified, True

        if ikey.startswith(("NSE_FO|", "BSE_FO|", "NFO|")):
            return "UNKNOWN_FO_UNCLASSIFIED", 65, False, False
        return "NON_S1_UNIVERSE", 1, True, False

    def _worker_seed_intended_s1_stats(self, mono_ns: int, ist_date: datetime.date) -> None:
        for ikey in self._agg_current_subscribed_keys:
            cohort, _lot, _lv, _is_opt = self._classify_instrument_key(ikey, ist_date)
            if cohort in ("NON_S1_UNIVERSE", "UNKNOWN_FO_UNCLASSIFIED"):
                continue
            if ikey not in self._agg_per_s1_inst and len(self._agg_per_s1_inst) < MAX_PER_INSTRUMENT_STATS_KEYS:
                self._agg_per_s1_inst[ikey] = PerInstrumentStats(cohort=cohort, seeded_mono_ns=mono_ns)

    def _worker_refresh_universe(self, force: bool = False) -> None:
        supplier = self._universe_supplier
        if supplier is None:
            return
        now_ns = time.perf_counter_ns()
        eff_interval = max(MIN_UNIVERSE_REFRESH_INTERVAL_NS, self._agg_effective_refresh_interval_ns)
        if not force and self._agg_last_universe_refresh_ns > 0 and (now_ns - self._agg_last_universe_refresh_ns) < eff_interval:
            return
        self._agg_last_universe_refresh_ns = now_ns
        t_sup0 = time.perf_counter_ns()
        try:
            raw_sub, raw_contracts = supplier()
            sup_ns = time.perf_counter_ns() - t_sup0
            if sup_ns > SLOW_SUPPLIER_THRESHOLD_NS:
                self._agg_supplier_slow_warnings += 1
                self._agg_effective_refresh_interval_ns = SLOW_SUPPLIER_BACKOFF_NS
            else:
                self._agg_effective_refresh_interval_ns = max(
                    MIN_UNIVERSE_REFRESH_INTERVAL_NS, self._agg_universe_refresh_interval_ns
                )

            if not isinstance(raw_sub, (set, frozenset, list, tuple)) or not isinstance(raw_contracts, dict):
                raise TypeError("Universe supplier returned invalid container types")

            ist_today = datetime.datetime.now(IST_TZ).date()
            bounded_sub: Set[str] = set()
            for k in raw_sub:
                ks = str(k)
                if len(ks) > MAX_KEY_CHARS:
                    self._agg_oversized_key_drops += 1
                    self._agg_coverage_valid = False
                    self._agg_invalidation_reasons.add("OVERSIZED_UNIVERSE_KEY")
                    continue
                if len(bounded_sub) >= MAX_UNIVERSE_KEYS_SNAPSHOT:
                    self._agg_universe_snapshot_cap_drops += 1
                    self._agg_coverage_valid = False
                    self._agg_invalidation_reasons.add("UNIVERSE_SUBSCRIBED_CAP_EXCEEDED")
                    break
                bounded_sub.add(ks)

            self._agg_contract_rev_seq += 1
            rev_id = self._agg_contract_rev_seq
            new_meta: Dict[str, ContractIdentityMeta] = {}
            sig_parts: List[Tuple[str, str, bool, int]] = []

            for ikey_raw, meta in raw_contracts.items():
                ikey = str(ikey_raw)
                if len(ikey) > MAX_KEY_CHARS or not isinstance(meta, dict):
                    continue
                if len(new_meta) >= MAX_UNIVERSE_CONTRACTS_SNAPSHOT:
                    self._agg_universe_snapshot_cap_drops += 1
                    self._agg_coverage_valid = False
                    self._agg_invalidation_reasons.add("UNIVERSE_CONTRACT_CAP_EXCEEDED")
                    break
                if meta.get("is_underlying"):
                    continue
                underlying = str(meta.get("underlying") or "").upper()
                exp_iso = str(meta.get("expiry") or "").strip()[:10]
                cached_dte: Optional[int] = None
                if meta.get("dte") is not None:
                    try:
                        cached_dte = int(math.ceil(float(meta["dte"])))
                    except (ValueError, TypeError):
                        cached_dte = None
                if not exp_iso and cached_dte is not None and underlying == "NIFTY" and not meta.get("is_futures"):
                    self._agg_dte_fallback_count += 1

                lot_verified = False
                raw_lot = meta.get("lot_size")
                if raw_lot is not None:
                    try:
                        parsed_lot = int(raw_lot)
                        if parsed_lot > 0:
                            lot_sz = parsed_lot
                            lot_verified = True
                        else:
                            lot_sz = 65 if underlying == "NIFTY" else 1
                    except (ValueError, TypeError):
                        lot_sz = 65 if underlying == "NIFTY" else 1
                else:
                    lot_sz = 65 if underlying == "NIFTY" else 1

                is_fut = bool(meta.get("is_futures"))
                rec = ContractIdentityMeta(
                    underlying=underlying,
                    expiry_iso=exp_iso,
                    cached_dte=cached_dte,
                    is_futures=is_fut,
                    lot_size=lot_sz,
                    lot_verified=lot_verified,
                    rev_seq=rev_id,
                )
                new_meta[ikey] = rec
                sig_parts.append((ikey, exp_iso, is_fut, lot_sz))
                self._agg_historical_identity[ikey] = rec
                self._agg_historical_identity.move_to_end(ikey)
                while len(self._agg_historical_identity) > MAX_HISTORICAL_IDENTITY_KEYS:
                    self._agg_historical_identity.popitem(last=False)

            sig_parts.sort()
            new_sig = hash(tuple(sig_parts))
            if bounded_sub != self._agg_current_subscribed_keys or new_sig != self._agg_current_meta_sig:
                if self._agg_universe_epoch_seq > 0:
                    sub_prev = self._agg_current_subscribed_keys
                    seen_prev = self._agg_seen_in_current_u_epoch & sub_prev
                    self._agg_universe_epochs.append(
                        {
                            "u_epoch": self._agg_universe_epoch_seq,
                            "intended_subscribed": len(sub_prev),
                            "delivered_in_intended": len(seen_prev),
                            "coverage_ratio": round(len(seen_prev) / float(len(sub_prev)), 6) if sub_prev else 0.0,
                        }
                    )
                self._agg_universe_epoch_seq += 1
                self._agg_current_subscribed_keys = bounded_sub
                self._agg_current_meta_sig = new_sig
                self._agg_seen_in_current_u_epoch = set()
                self._agg_u_epoch_seen_core = set()
                self._agg_u_epoch_seen_opt_1_7 = set()
                self._agg_u_epoch_seen_opt_other = set()
                self._agg_u_epoch_seen_opt_1lot = set()

            self._agg_current_contract_meta = new_meta
            self._worker_seed_intended_s1_stats(now_ns, ist_today)
            self._agg_consecutive_supplier_faults = 0
        except Exception as exc:
            self._agg_worker_internal_errors += 1
            self._agg_consecutive_supplier_faults += 1
            self._agg_coverage_valid = False
            self._agg_invalidation_reasons.add("UNIVERSE_SUPPLIER_FAULT")
            if self._agg_consecutive_supplier_faults >= MAX_CONSECUTIVE_PROBE_FAULTS:
                self._capture_enabled = False
                self._auto_disabled_on_fault = True
                self._auto_disabled_reason = f"UNIVERSE_SUPPLIER_FAULT:{type(exc).__name__}"

    def _collect_unexported_control_audit(self) -> List[Dict[str, Any]]:
        ring_snap = list(self._control_audit_ring)
        fresh = [e for e in ring_snap if int(e.get("audit_seq", 0)) > self._control_audit_exported_idx]
        if fresh:
            self._control_audit_exported_idx = int(fresh[-1]["audit_seq"])
        return fresh

    def _worker_emit_completed_1m_rollup(self, entry_1m: Dict[str, Any]) -> None:
        if len(self._agg_ring_1m) == RING_1M_MAXLEN:
            self._agg_ring_1m_evictions += 1
        self._agg_ring_1m.append(entry_1m)
        if self._exporter.configured_dir is not None:
            pl = {
                "epoch_id": self._agg_epoch_id,
                "minute_rollup": entry_1m,
                "control_audit_delta": self._collect_unexported_control_audit(),
            }
            self._exporter.write_record("MINUTE_ROLLUP", pl, update_checkpoint=False)

    def _worker_roll_time_buckets(
        self,
        *,
        slot_100ms: int,
        slot_1s: int,
        slot_1m: int,
        raw_bytes: int,
        n_insts: int,
        s1_updates: int,
        opt_books: int,
    ) -> None:
        if self._agg_cur_100ms_slot != slot_100ms:
            if self._agg_cur_100ms_slot >= 0:
                f100 = self._agg_cur_100ms_frames
                b100 = self._agg_cur_100ms_bytes
                i100 = self._agg_cur_100ms_insts
                ob100 = self._agg_cur_100ms_opt_books
                if f100 > 0:
                    self._agg_hist_100ms_nonempty_frames.observe(float(f100))
                    self._agg_hist_100ms_nonempty_bytes.observe(float(b100))
                    if len(self._agg_ring_100ms) == RING_100MS_MAXLEN:
                        self._agg_ring_100ms_evictions += 1
                    self._agg_ring_100ms.append(
                        {
                            "slot_100ms": self._agg_cur_100ms_slot,
                            "frames": f100,
                            "bytes": b100,
                            "instruments": i100,
                            "s1_option_books": ob100,
                            "partial_finalized": False,
                        }
                    )
                if slot_100ms > self._agg_cur_100ms_slot + 1:
                    self._agg_confirmed_zero_100ms_bins += (slot_100ms - self._agg_cur_100ms_slot - 1)
            self._agg_cur_100ms_slot = slot_100ms
            self._agg_cur_100ms_frames = 0
            self._agg_cur_100ms_bytes = 0
            self._agg_cur_100ms_insts = 0
            self._agg_cur_100ms_opt_books = 0

        self._agg_cur_100ms_frames += 1
        self._agg_cur_100ms_bytes += raw_bytes
        self._agg_cur_100ms_insts += n_insts
        self._agg_cur_100ms_opt_books += opt_books
        if self._agg_cur_100ms_frames > self._agg_peak_100ms_frames:
            self._agg_peak_100ms_frames = self._agg_cur_100ms_frames
        if self._agg_cur_100ms_bytes > self._agg_peak_100ms_bytes:
            self._agg_peak_100ms_bytes = self._agg_cur_100ms_bytes
        if self._agg_cur_100ms_insts > self._agg_peak_100ms_insts:
            self._agg_peak_100ms_insts = self._agg_cur_100ms_insts
        if self._agg_cur_100ms_opt_books > self._agg_peak_100ms_opt_books:
            self._agg_peak_100ms_opt_books = self._agg_cur_100ms_opt_books

        if self._agg_cur_1s_slot != slot_1s:
            if self._agg_cur_1s_slot >= 0:
                f1 = self._agg_cur_1s_frames
                b1 = self._agg_cur_1s_bytes
                i1 = self._agg_cur_1s_insts
                s1u = self._agg_cur_1s_s1_updates
                ob1 = self._agg_cur_1s_opt_books
                self._agg_hist_1s_all_frames.observe(float(f1))
                self._agg_hist_1s_all_bytes.observe(float(b1))
                if len(self._agg_ring_1s) == RING_1S_MAXLEN:
                    self._agg_ring_1s_evictions += 1
                self._agg_ring_1s.append(
                    {
                        "slot_1s": self._agg_cur_1s_slot,
                        "frames": f1,
                        "bytes": b1,
                        "instruments": i1,
                        "s1_updates": s1u,
                        "s1_option_books": ob1,
                        "partial_finalized": False,
                    }
                )
                if slot_1s > self._agg_cur_1s_slot + 1:
                    zero_gap = slot_1s - self._agg_cur_1s_slot - 1
                    self._agg_confirmed_zero_1s_bins += zero_gap
                    # Exact O(1) bulk zero addition — zero arbitrary 3,600 cap and zero loop overhead!
                    self._agg_hist_1s_all_frames.observe_many(0.0, zero_gap)
                    self._agg_hist_1s_all_bytes.observe_many(0.0, zero_gap)
            self._agg_cur_1s_slot = slot_1s
            self._agg_cur_1s_frames = 0
            self._agg_cur_1s_bytes = 0
            self._agg_cur_1s_insts = 0
            self._agg_cur_1s_s1_updates = 0
            self._agg_cur_1s_opt_books = 0

        self._agg_cur_1s_frames += 1
        self._agg_cur_1s_bytes += raw_bytes
        self._agg_cur_1s_insts += n_insts
        self._agg_cur_1s_s1_updates += s1_updates
        self._agg_cur_1s_opt_books += opt_books
        if self._agg_cur_1s_frames > self._agg_peak_1s_frames:
            self._agg_peak_1s_frames = self._agg_cur_1s_frames
        if self._agg_cur_1s_bytes > self._agg_peak_1s_bytes:
            self._agg_peak_1s_bytes = self._agg_cur_1s_bytes
        if self._agg_cur_1s_insts > self._agg_peak_1s_insts:
            self._agg_peak_1s_insts = self._agg_cur_1s_insts
        if self._agg_cur_1s_s1_updates > self._agg_peak_1s_s1_updates:
            self._agg_peak_1s_s1_updates = self._agg_cur_1s_s1_updates
        if self._agg_cur_1s_opt_books > self._agg_peak_1s_opt_books:
            self._agg_peak_1s_opt_books = self._agg_cur_1s_opt_books

        if self._agg_cur_1m_slot != slot_1m:
            if self._agg_cur_1m_slot >= 0 and self._agg_cur_1m_frames > 0:
                self._worker_emit_completed_1m_rollup(
                    {
                        "epoch_minute": self._agg_cur_1m_slot,
                        "utc_minute": datetime.datetime.fromtimestamp(
                            self._agg_cur_1m_slot * 60, tz=datetime.timezone.utc
                        ).strftime("%Y-%m-%dT%H:%M:00Z"),
                        "frames": self._agg_cur_1m_frames,
                        "bytes": self._agg_cur_1m_bytes,
                        "instruments": self._agg_cur_1m_insts,
                        "s1_updates": self._agg_cur_1m_s1_updates,
                        "s1_option_books": self._agg_cur_1m_opt_books,
                        "partial_finalized": False,
                    }
                )
            self._agg_cur_1m_slot = slot_1m
            self._agg_cur_1m_frames = 0
            self._agg_cur_1m_bytes = 0
            self._agg_cur_1m_insts = 0
            self._agg_cur_1m_s1_updates = 0
            self._agg_cur_1m_opt_books = 0

        self._agg_cur_1m_frames += 1
        self._agg_cur_1m_bytes += raw_bytes
        self._agg_cur_1m_insts += n_insts
        self._agg_cur_1m_s1_updates += s1_updates
        self._agg_cur_1m_opt_books += opt_books

    def _worker_finalize_active_buckets(self) -> None:
        if self._agg_cur_100ms_slot >= 0 and self._agg_cur_100ms_frames > 0:
            self._agg_hist_100ms_nonempty_frames.observe(float(self._agg_cur_100ms_frames))
            self._agg_hist_100ms_nonempty_bytes.observe(float(self._agg_cur_100ms_bytes))
            self._agg_ring_100ms.append(
                {
                    "slot_100ms": self._agg_cur_100ms_slot,
                    "frames": self._agg_cur_100ms_frames,
                    "bytes": self._agg_cur_100ms_bytes,
                    "instruments": self._agg_cur_100ms_insts,
                    "s1_option_books": self._agg_cur_100ms_opt_books,
                    "partial_finalized": True,
                }
            )
            self._agg_cur_100ms_slot = -1
            self._agg_cur_100ms_frames = 0

        if self._agg_cur_1s_slot >= 0 and self._agg_cur_1s_frames > 0:
            self._agg_hist_1s_all_frames.observe(float(self._agg_cur_1s_frames))
            self._agg_hist_1s_all_bytes.observe(float(self._agg_cur_1s_bytes))
            self._agg_ring_1s.append(
                {
                    "slot_1s": self._agg_cur_1s_slot,
                    "frames": self._agg_cur_1s_frames,
                    "bytes": self._agg_cur_1s_bytes,
                    "instruments": self._agg_cur_1s_insts,
                    "s1_updates": self._agg_cur_1s_s1_updates,
                    "s1_option_books": self._agg_cur_1s_opt_books,
                    "partial_finalized": True,
                }
            )
            self._agg_cur_1s_slot = -1
            self._agg_cur_1s_frames = 0

        if self._agg_cur_1m_slot >= 0 and self._agg_cur_1m_frames > 0:
            self._worker_emit_completed_1m_rollup(
                {
                    "epoch_minute": self._agg_cur_1m_slot,
                    "utc_minute": datetime.datetime.fromtimestamp(
                        self._agg_cur_1m_slot * 60, tz=datetime.timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:00Z"),
                    "frames": self._agg_cur_1m_frames,
                    "bytes": self._agg_cur_1m_bytes,
                    "instruments": self._agg_cur_1m_insts,
                    "s1_updates": self._agg_cur_1m_s1_updates,
                    "s1_option_books": self._agg_cur_1m_opt_books,
                    "partial_finalized": True,
                }
            )
            self._agg_cur_1m_slot = -1
            self._agg_cur_1m_frames = 0

    def _parse_wire_bytes_c_proto(
        self, wire_bytes: bytes, omitted_enum0_flag: bool
    ) -> Tuple[int, bool, Optional[float], List[Tuple[Any, ...]], int]:
        pb = _get_pb_module()
        fr = pb.FeedResponse.FromString(wire_bytes)
        wtype = int(fr.type)
        cur_ts: Optional[float] = float(fr.currentTs) if fr.currentTs != 0 else None
        rows: List[Tuple[Any, ...]] = []
        oversized = 0

        for idx, (raw_k, fobj) in enumerate(fr.feeds.items()):
            if (idx & 63) == 63:
                time.sleep(0)  # Yield CPython GIL every 64 instruments (~120 us)
            ikey = str(raw_k)
            if len(ikey) > MAX_KEY_CHARS:
                oversized += 1
                continue
            arm = 0
            ltp_p = 0
            ltp_pos = 0
            bk_p = 0
            bk_ne = 0
            bp = 0.0
            ap = 0.0
            bq = 0
            aq = 0
            lvls = 0
            oi_p = 0
            oi_pos = 0
            grk = 0
            i1 = 0
            cts = 0

            union_arm = fobj.WhichOneof("FeedUnion")
            if union_arm == "fullFeed":
                ff_arm = fobj.fullFeed.WhichOneof("FullFeedUnion")
                if ff_arm == "marketFF":
                    arm = 1
                    mff = fobj.fullFeed.marketFF
                    if mff.HasField("ltpc"):
                        ltp_p = 1
                        if math.isfinite(mff.ltpc.ltp) and mff.ltpc.ltp > 0.0:
                            ltp_pos = 1
                    if mff.HasField("marketLevel"):
                        bk_p = 1
                        qlist = mff.marketLevel.bidAskQuote
                        lvls = len(qlist)
                        if lvls > 0:
                            bk_ne = 1
                            q0 = qlist[0]
                            bp = float(q0.bidP)
                            ap = float(q0.askP)
                            bq = int(q0.bidQ)
                            aq = int(q0.askQ)
                    # Note: proto3 scalar `oi` omits 0.0 on wire; positive values prove presence & positivity
                    if mff.oi != 0.0 and math.isfinite(mff.oi):
                        oi_p = 1
                        if mff.oi > 0.0:
                            oi_pos = 1
                    if mff.HasField("optionGreeks"):
                        grk = 1
                    if mff.HasField("marketOHLC"):
                        for ci in mff.marketOHLC.ohlc:
                            if ci.interval == "I1":
                                i1 = 1
                                break
                elif ff_arm == "indexFF":
                    arm = 2
                    iff = fobj.fullFeed.indexFF
                    if iff.HasField("ltpc"):
                        ltp_p = 1
                        if math.isfinite(iff.ltpc.ltp) and iff.ltpc.ltp > 0.0:
                            ltp_pos = 1
                    if iff.HasField("marketOHLC"):
                        for ci in iff.marketOHLC.ohlc:
                            if ci.interval == "I1":
                                i1 = 1
                                break
            elif union_arm == "firstLevelWithGreeks":
                arm = 3
                fl = fobj.firstLevelWithGreeks
                if fl.HasField("ltpc"):
                    ltp_p = 1
                    if math.isfinite(fl.ltpc.ltp) and fl.ltpc.ltp > 0.0:
                        ltp_pos = 1
                if fl.HasField("firstDepth"):
                    bk_p = 1
                    bk_ne = 1
                    lvls = 1
                    bp = float(fl.firstDepth.bidP)
                    ap = float(fl.firstDepth.askP)
                    bq = int(fl.firstDepth.bidQ)
                    aq = int(fl.firstDepth.askQ)
                if fl.oi != 0.0 and math.isfinite(fl.oi):
                    oi_p = 1
                    if fl.oi > 0.0:
                        oi_pos = 1
                if fl.HasField("optionGreeks"):
                    grk = 1
            elif union_arm == "ltpc":
                arm = 4
                ltp_p = 1
                if math.isfinite(fobj.ltpc.ltp) and fobj.ltpc.ltp > 0.0:
                    ltp_pos = 1

            flags = (
                (arm & 0x7)
                | (ltp_p << 3)
                | (ltp_pos << 4)
                | (bk_p << 5)
                | (bk_ne << 6)
                | (oi_p << 7)
                | (oi_pos << 8)
                | (grk << 9)
                | (i1 << 10)
                | (cts << 11)
                | (min(lvls, 255) << 12)
            )
            if bk_ne:
                rows.append((ikey, flags, bp, ap, bq, aq))
            else:
                rows.append((ikey, flags))
        return wtype, omitted_enum0_flag, cur_ts, rows, oversized

    def _worker_process_ok_frame(self, item: List[Any]) -> None:
        (
            _,
            item_ep,
            frame_seq,
            _acct_bytes,
            raw_bytes,
            is_bytes,
            wire_type_int,
            payload_kind,
            payload_obj,
            wall_ns,
            t0_ns,
            t1_ns,
            t2_ns,
            t3_ns,
            cb_stage_hk_ns,
            handoff_ns,
            full_added_ns,
        ) = item

        # Support direct white-box unit test replay when worker was never started (`_agg_epoch_seq == 0`)
        if (
            self._agg_epoch_seq == 0
            and self._worker_thread is None
            and item_ep == self._active_epoch_seq
            and item_ep > 0
        ):
            self._agg_epoch_seq = item_ep
            self._agg_epoch_id = f"{self._boot_id}_ep{item_ep:04d}"
            self._agg_epoch_closed = False

        # STRICT EPOCH PROVENANCE GUARD: reject delayed in-flight observations from prior/closed epochs!
        if item_ep != self._agg_epoch_seq or self._agg_epoch_closed:
            self._post_disable_inflight_drops += 1
            return

        self._worker_refresh_universe(force=False)

        if self._agg_prev_wall_ns > 0 and self._agg_prev_mono_ns > 0 and t0_ns >= self._agg_prev_mono_ns:
            d_wall_ms = (wall_ns - self._agg_prev_wall_ns) / 1_000_000.0
            d_mono_ms = (t0_ns - self._agg_prev_mono_ns) / 1_000_000.0
            if abs(d_wall_ms - d_mono_ms) > CLOCK_STEP_TOLERANCE_MS:
                self._agg_clock_step_events += 1
                self._agg_clock_certification_valid = False
        self._agg_prev_wall_ns = wall_ns
        self._agg_prev_mono_ns = t0_ns
        wall_ms = int(wall_ns // 1_000_000) if wall_ns > 0 else int(time.time() * 1000)
        receipt_ist_date = datetime.datetime.fromtimestamp(wall_ms / 1000.0, tz=IST_TZ).date()

        if payload_kind == _PAYLOAD_WIRE_BYTES:
            w_bytes, omitted_flag = payload_obj
            eff_type_int, dict_omitted_enum0, cur_ts_f, raw_rows, oversized_in_frame = self._parse_wire_bytes_c_proto(
                w_bytes, omitted_flag
            )
            if wire_type_int >= 0:
                eff_type_int = wire_type_int
            env_label = {0: "initial_feed", 1: "live_feed", 2: "market_info"}.get(eff_type_int, "unknown")
        else:
            dict_type_str, cur_ts_f, tup_rows, oversized_in_frame = payload_obj
            raw_rows = list(tup_rows)
            dict_omitted_enum0 = (wire_type_int == 0 and dict_type_str is None)
            if wire_type_int in (0, 1, 2):
                env_label = {0: "initial_feed", 1: "live_feed", 2: "market_info"}[wire_type_int]
            elif dict_type_str in ("initial_feed", "live_feed", "market_info"):
                env_label = dict_type_str
            else:
                env_label = "unknown"

        has_cur_ts = cur_ts_f is not None
        cur_ts_lag_ms = (wall_ms - cur_ts_f) if cur_ts_f is not None else None

        # Commit frame transactionally
        if self._agg_first_frame_ms is None:
            self._agg_first_frame_ms = wall_ms
            self._agg_first_frame_mono_ns = t0_ns
        self._agg_last_frame_ms = wall_ms
        self._agg_last_frame_mono_ns = t0_ns

        self._agg_ingress_attempts += 1
        self._agg_decoded_ok_frames += 1
        if not is_bytes:
            self._agg_non_bytes_frames += 1
        else:
            self._agg_ingress_wire_bytes += raw_bytes
            self._agg_hist_frame_bytes.observe(float(raw_bytes))
            if raw_bytes > 65536:
                self._agg_frames_gt_64kib += 1
            if raw_bytes > 1048576:
                self._agg_frames_gt_1mib += 1

        self._agg_wire_enum_type_counts[env_label] = self._agg_wire_enum_type_counts.get(env_label, 0) + 1
        if dict_omitted_enum0:
            self._agg_dict_type_omitted_enum0 += 1

        if has_cur_ts and cur_ts_lag_ms is not None:
            self._agg_frames_with_current_ts += 1
            self._agg_hist_provider_lag_ms.observe(cur_ts_lag_ms)
        else:
            self._agg_frames_missing_current_ts += 1

        if oversized_in_frame > 0:
            self._agg_oversized_key_drops += oversized_in_frame
            self._agg_coverage_valid = False
            self._agg_invalidation_reasons.add("OVERSIZED_OR_EXCESS_FEED_KEY")

        self._agg_total_instrument_updates += len(raw_rows)
        self._agg_hist_inst_per_frame.observe(float(len(raw_rows)))

        frame_s1_updates = 0
        frame_opt_books = 0

        for row_idx, r_item in enumerate(raw_rows):
            if (row_idx & 63) == 63:
                time.sleep(0)  # Cooperative GIL yield every 64 rows so live callbacks never stall >150 us
            ikey = r_item[0]
            flags = r_item[1]
            arm = flags & 0x7
            ltp_p = (flags >> 3) & 1
            ltp_pos = (flags >> 4) & 1
            bk_p = (flags >> 5) & 1
            bk_ne = (flags >> 6) & 1
            oi_p = (flags >> 7) & 1
            oi_pos = (flags >> 8) & 1
            grk = (flags >> 9) & 1
            i1 = (flags >> 10) & 1
            cts = (flags >> 11) & 1
            lvls = (flags >> 12) & 0xFF
            if bk_ne and len(r_item) >= 6:
                bp, ap, bq, aq = r_item[2], r_item[3], r_item[4], r_item[5]
            else:
                bp, ap, bq, aq = 0.0, 0.0, 0, 0
            cohort_name, lot_sz, lot_verified, is_nifty_opt = self._classify_instrument_key(ikey, receipt_ist_date)

            if ikey not in self._agg_seen_all_keys:
                if len(self._agg_seen_all_keys) < MAX_TRACKED_SEEN_KEYS:
                    self._agg_seen_all_keys.add(ikey)
                else:
                    self._agg_seen_key_cap_drops += 1
                    self._agg_coverage_valid = False
                    self._agg_invalidation_reasons.add("SEEN_ALL_KEYS_CAP_EXCEEDED")

            in_cur_sub = False
            if self._agg_current_subscribed_keys:
                if ikey in self._agg_current_subscribed_keys:
                    in_cur_sub = True
                    if len(self._agg_seen_in_current_u_epoch) < MAX_UNIVERSE_KEYS_SNAPSHOT:
                        self._agg_seen_in_current_u_epoch.add(ikey)
                else:
                    self._agg_delivered_outside_subscribed_count += 1

            c_obj = self._agg_cohorts[cohort_name]
            if c_obj.updates > 0 and t0_ns > c_obj.last_seen_mono_ns:
                cgap = (t0_ns - c_obj.last_seen_mono_ns) / 1_000_000.0
                if cgap > c_obj.cohort_any_max_gap_ms:
                    c_obj.cohort_any_max_gap_ms = cgap
            else:
                c_obj.first_seen_mono_ns = t0_ns
            c_obj.last_seen_mono_ns = t0_ns
            c_obj.updates += 1

            if arm == 1:
                c_obj.schema_full_market += 1
            elif arm == 2:
                c_obj.schema_full_index += 1
            elif arm == 3:
                c_obj.schema_first_level_greeks += 1
            elif arm == 4:
                c_obj.schema_top_ltpc += 1
            else:
                c_obj.schema_empty_or_other += 1

            c_obj.ltpc_field_present += ltp_p
            c_obj.ltpc_positive_updates += ltp_pos
            c_obj.book_field_present += bk_p
            c_obj.book_nonempty_updates += bk_ne

            bk_valid = 0
            bk_2q = 0
            bk_1lot_ver = 0
            bk_1lot_unver = 0
            top_q_tuple: Optional[Tuple[float, float, int, int]] = None
            if bk_ne:
                if ltp_pos == 0:
                    c_obj.book_only_no_ltp_updates += 1
                if math.isfinite(bp) and math.isfinite(ap) and bp > 0.0 and ap >= bp:
                    bk_valid = 1
                    top_q_tuple = (bp, ap, bq, aq)
                    if bq > 0 and aq > 0:
                        bk_2q = 1
                    if bq >= lot_sz and aq >= lot_sz:
                        if lot_verified:
                            bk_1lot_ver = 1
                        else:
                            bk_1lot_unver = 1
                    if lvls >= 5:
                        c_obj.book_l5_updates += 1
                else:
                    c_obj.book_empty_or_crossed_updates += 1
            elif bk_p:
                c_obj.book_empty_or_crossed_updates += 1

            c_obj.book_valid_2sided_updates += bk_valid
            c_obj.book_2sided_qty_updates += bk_2q
            c_obj.book_usable_1lot_verified_updates += bk_1lot_ver
            c_obj.book_1lot_unverified_lot_updates += bk_1lot_unver
            c_obj.oi_field_present += oi_p
            c_obj.oi_positive_updates += oi_pos
            c_obj.greeks_updates += grk
            c_obj.i1_candle_updates += i1
            c_obj.unverified_candidate_quote_ts_fields += cts

            if cohort_name not in ("NON_S1_UNIVERSE", "UNKNOWN_FO_UNCLASSIFIED"):
                frame_s1_updates += 1
                if ikey in S1_CORE_INSTRUMENT_KEYS:
                    self._agg_seen_s1_core_keys.add(ikey)
                    if in_cur_sub:
                        self._agg_u_epoch_seen_core.add(ikey)
                elif cohort_name == "NIFTY_OPTIONS_1_7_DTE":
                    if ikey not in self._agg_seen_opt_1_7_keys:
                        if len(self._agg_seen_opt_1_7_keys) < MAX_TRACKED_OPTION_COHORT_KEYS:
                            self._agg_seen_opt_1_7_keys.add(ikey)
                        else:
                            self._agg_opt_cohort_key_cap_drops += 1
                            self._agg_coverage_valid = False
                            self._agg_invalidation_reasons.add("OPT_1_7_COHORT_CAP_EXCEEDED")
                    if in_cur_sub and len(self._agg_u_epoch_seen_opt_1_7) < MAX_TRACKED_OPTION_COHORT_KEYS:
                        self._agg_u_epoch_seen_opt_1_7.add(ikey)
                elif cohort_name == "NIFTY_OPTIONS_OTHER_DTE":
                    if ikey not in self._agg_seen_opt_other_keys:
                        if len(self._agg_seen_opt_other_keys) < MAX_TRACKED_OPTION_COHORT_KEYS:
                            self._agg_seen_opt_other_keys.add(ikey)
                        else:
                            self._agg_opt_cohort_key_cap_drops += 1
                            self._agg_coverage_valid = False
                            self._agg_invalidation_reasons.add("OPT_OTHER_COHORT_CAP_EXCEEDED")
                    if in_cur_sub and len(self._agg_u_epoch_seen_opt_other) < MAX_TRACKED_OPTION_COHORT_KEYS:
                        self._agg_u_epoch_seen_opt_other.add(ikey)

                pstat = self._agg_per_s1_inst.get(ikey)
                if pstat is None:
                    if len(self._agg_per_s1_inst) < MAX_PER_INSTRUMENT_STATS_KEYS:
                        pstat = PerInstrumentStats(cohort=cohort_name, seeded_mono_ns=0)
                        self._agg_per_s1_inst[ikey] = pstat
                    else:
                        self._agg_per_inst_key_cap_drops += 1
                        self._agg_coverage_valid = False
                        self._agg_invalidation_reasons.add("PER_INSTRUMENT_KEY_CAP_EXCEEDED")
                is_distinct = False
                if pstat is not None:
                    is_distinct = pstat.on_update(
                        cohort_name, t0_ns, bool(bk_valid), bool(bk_1lot_ver), top_q_tuple
                    )
                if is_distinct:
                    c_obj.book_distinct_top_changes += 1

                if is_nifty_opt:
                    if bk_valid:
                        self._agg_s1_option_valid_books += 1
                        if is_distinct:
                            self._agg_s1_option_distinct_books += 1
                        frame_opt_books += 1
                        if len(self._agg_seen_opt_valid_book_keys) < MAX_TRACKED_OPTION_COHORT_KEYS:
                            self._agg_seen_opt_valid_book_keys.add(ikey)
                    if bk_1lot_ver:
                        self._agg_s1_option_1lot_books += 1
                        if len(self._agg_seen_opt_1lot_book_keys) < MAX_TRACKED_OPTION_COHORT_KEYS:
                            self._agg_seen_opt_1lot_book_keys.add(ikey)
                        if in_cur_sub and len(self._agg_u_epoch_seen_opt_1lot) < MAX_TRACKED_OPTION_COHORT_KEYS:
                            self._agg_u_epoch_seen_opt_1lot.add(ikey)
                elif bk_valid:
                    self._agg_s1_non_option_books += 1

        if frame_s1_updates > 0:
            self._agg_s1_relevant_frames += 1
            self._agg_s1_relevant_updates += frame_s1_updates

        self._worker_roll_time_buckets(
            slot_100ms=t0_ns // 100_000_000,
            slot_1s=t0_ns // 1_000_000_000,
            slot_1m=wall_ms // 60_000,
            raw_bytes=raw_bytes if is_bytes else 0,
            n_insts=len(raw_rows),
            s1_updates=frame_s1_updates,
            opt_books=frame_opt_books,
        )

        # Pair downstream hook overhead & piggybacked stage segments strictly by `frame_seq`
        if isinstance(cb_stage_hk_ns, tuple):
            inline_hk_ns, inline_seg = cb_stage_hk_ns
            if inline_seg is not None:
                l_ns, eq_ns, hk_ns = inline_seg
                if l_ns >= 0:
                    self._agg_hist_listener_us.observe(l_ns / 1000.0)
                if eq_ns >= 0:
                    self._agg_hist_equity_us.observe(eq_ns / 1000.0)
                if hk_ns >= 0:
                    self._agg_hist_stage_hook_us.observe(hk_ns / 1000.0)
            seq_hk_ns = self._agg_stage_hooks_by_seq.pop(frame_seq, inline_hk_ns)
        else:
            seq_hk_ns = self._agg_stage_hooks_by_seq.pop(frame_seq, int(cb_stage_hk_ns))
        if t1_ns >= t0_ns:
            self._agg_hist_sdk_decode_us.observe((t1_ns - t0_ns) / 1000.0)
        if t2_ns >= t1_ns:
            self._agg_hist_sdk_dict_us.observe((t2_ns - t1_ns) / 1000.0)
        if t3_ns >= t2_ns:
            raw_ds_ns = t3_ns - t2_ns
            app_ds_ns = max(0, raw_ds_ns - seq_hk_ns)
            self._agg_hist_raw_downstream_us.observe(raw_ds_ns / 1000.0)
            self._agg_hist_app_downstream_us.observe(app_ds_ns / 1000.0)

        self._agg_hist_ingress_handoff_us.observe(handoff_ns / 1000.0)
        self._agg_hist_full_probe_added_us.observe(full_added_ns / 1000.0)
        if t3_ns >= t0_ns:
            self._agg_hist_total_callback_us.observe(((t3_ns - t0_ns) + full_added_ns - seq_hk_ns) / 1000.0)

        self._agg_epoch_durable = False

    def _worker_process_err_frame(self, item: Tuple[Any, ...]) -> None:
        _, item_ep, stage, raw_bytes, is_bytes, _wire_type_int, _wall_ns, _t0_ns = item
        if item_ep != self._agg_epoch_seq or self._agg_epoch_closed:
            self._post_disable_inflight_drops += 1
            return
        self._agg_ingress_attempts += 1
        if not is_bytes:
            self._agg_non_bytes_frames += 1
        else:
            self._agg_ingress_wire_bytes += raw_bytes
            self._agg_hist_frame_bytes.observe(float(raw_bytes))
            if raw_bytes > 65536:
                self._agg_frames_gt_64kib += 1
            if raw_bytes > 1048576:
                self._agg_frames_gt_1mib += 1

        if stage == "decode_protobuf":
            self._agg_sdk_decode_errors += 1
        elif stage == "message_to_dict":
            self._agg_sdk_dict_errors += 1
        elif stage == "emit_message":
            self._agg_downstream_emit_errors += 1
        self._agg_epoch_durable = False

    def _worker_drain_data_batch(self, drain_all: bool = False) -> int:
        t_slice0 = time.perf_counter_ns()
        processed = 0
        while True:
            if not drain_all:
                if processed >= WORKER_MAX_BATCH_ITEMS:
                    break
                if processed > 0 and (time.perf_counter_ns() - t_slice0) >= WORKER_MAX_SLICE_NS:
                    break
            try:
                item = self._inbox.popleft()
            except IndexError:
                break
            processed += 1
            kind = item[0]
            if kind == _EVT_FRAME_OK:
                acct_b = int(item[3])
                self._dequeued_wire_bytes_total += acct_b
                try:
                    self._worker_process_ok_frame(item)
                    self._agg_consecutive_worker_faults = 0
                except Exception as exc:
                    self._agg_incomplete_frames_discarded += 1
                    self._agg_worker_internal_errors += 1
                    self._agg_consecutive_worker_faults += 1
                    self._agg_coverage_valid = False
                    self._agg_invalidation_reasons.add("INCOMPLETE_FRAME_DISCARDED")
                    if self._agg_consecutive_worker_faults >= MAX_CONSECUTIVE_PROBE_FAULTS:
                        self._capture_enabled = False
                        self._auto_disabled_on_fault = True
                        self._auto_disabled_reason = f"WORKER_FRAME_FAULT:{type(exc).__name__}"
            elif kind == _EVT_FRAME_ERR:
                b_len = int(item[3])
                self._dequeued_wire_bytes_total += b_len
                try:
                    self._worker_process_err_frame(item)
                except Exception:
                    self._agg_worker_internal_errors += 1
            elif kind == _EVT_DOWNSTREAM_SEG:
                _, item_ep, fseq, l_ns, eq_ns, hk_ns = item
                if item_ep == self._agg_epoch_seq and not self._agg_epoch_closed:
                    if l_ns >= 0:
                        self._agg_hist_listener_us.observe(l_ns / 1000.0)
                    if eq_ns >= 0:
                        self._agg_hist_equity_us.observe(eq_ns / 1000.0)
                    if hk_ns >= 0:
                        self._agg_hist_stage_hook_us.observe(hk_ns / 1000.0)
                        if fseq > 0:
                            self._agg_stage_hooks_by_seq[fseq] = hk_ns
                            while len(self._agg_stage_hooks_by_seq) > 512:
                                self._agg_stage_hooks_by_seq.popitem(last=False)
            elif kind == _EVT_PERIODIC_STAGE:
                _, item_ep, stg, dur_ns = item
                if item_ep == self._agg_epoch_seq and not self._agg_epoch_closed:
                    if stg == "bar_flush_15m":
                        self._agg_hist_bar_flush_15m_us.observe(dur_ns / 1000.0)
                    elif stg == "freeze_snapshot":
                        self._agg_hist_freeze_snapshot_us.observe(dur_ns / 1000.0)
            elif kind == _EVT_LIFECYCLE:
                _, item_ep, ev, detail, mono_ns = item
                if item_ep == self._agg_epoch_seq and not self._agg_epoch_closed:
                    self._worker_record_lifecycle_event(ev, detail, mono_ns)

        if processed > 0:
            self._agg_hist_worker_slice_us.observe((time.perf_counter_ns() - t_slice0) / 1000.0)
        return processed

    def _worker_publish_snapshot(self) -> None:
        snap = self._build_snapshot_from_aggregator(include_rings=True, include_hist_bins=False)
        self._published_snapshot = snap
        self._published_compact = self._build_compact_from_snapshot(snap)
        self._agg_last_snapshot_pub_ns = time.perf_counter_ns()

    def _worker_export_compact_record(self, record_kind: str, extra_fields: Optional[Dict[str, Any]] = None) -> bool:
        # Compact export excludes rolling rings (`1m` rollups are exported incrementally once!)
        bins_mode: Any = True if record_kind == "EPOCH_SEALED" else "compact"
        export_snap = self._build_snapshot_from_aggregator(include_rings=False, include_hist_bins=bins_mode)
        export_snap["control_audit_delta"] = self._collect_unexported_control_audit()
        if extra_fields:
            export_snap.update(extra_fields)
        ok = self._exporter.write_record(record_kind, export_snap, update_checkpoint=True)
        if ok:
            self._agg_last_exported_frames = self._agg_ingress_attempts
        return ok

    def _worker_loop_guarded(self) -> None:
        """Top-level fail-safe worker wrapper: NEVER leaves capture latched ON if worker crashes!"""
        try:
            self._worker_loop_inner()
        except BaseException as fatal_exc:
            self._capture_enabled = False
            self._worker_dead_latched = True
            self._auto_disabled_on_fault = True
            self._auto_disabled_reason = f"WORKER_TERMINATED:{type(fatal_exc).__name__}"
            self._agg_coverage_valid = False
            self._agg_invalidation_reasons.add("WORKER_TERMINATED")
        finally:
            self._worker_alive_flag = False
            # Release any waiting control callers so they never hang on worker exit
            for attr in ("_pending_enable_epoch", "_pending_seal_cmd", "_pending_reset_cmd", "_pending_sync_barrier"):
                cmd = getattr(self, attr, None)
                if cmd is not None:
                    setattr(self, attr, None)
                    ev = cmd[-1]
                    if isinstance(ev, threading.Event):
                        ev.set()

    def _worker_loop_inner(self) -> None:
        while not self._worker_stop:
            self._worker_last_heartbeat_mono_ns = time.perf_counter_ns()

            # 1. Handle enable epoch transition
            en_cmd = self._pending_enable_epoch
            if en_cmd is not None:
                self._pending_enable_epoch = None
                ep_seq, en_utc, en_mono, en_done = en_cmd
                self._init_aggregator_state(epoch_seq=ep_seq)
                self._agg_epoch_closed = False
                self._agg_enabled_since_utc = en_utc
                self._agg_active_start_mono_ns = en_mono
                if len(self._agg_control_intervals) == MAX_CONTROL_INTERVALS:
                    self._agg_control_intervals_evicted += 1
                self._agg_control_intervals.append({"enabled_utc": en_utc, "disabled_utc": None})
                self._worker_refresh_universe(force=True)
                self._capture_enabled = True
                self._worker_publish_snapshot()
                en_done.set()

            # 2. Drain bounded micro-slice of incoming observations
            processed = self._worker_drain_data_batch(drain_all=False)

            # 3. Handle sync/export barrier
            sync_cmd = self._pending_sync_barrier
            if sync_cmd is not None:
                self._pending_sync_barrier = None
                force_exp, sync_done = sync_cmd
                self._worker_drain_data_batch(drain_all=True)
                self._worker_refresh_universe(force=True)
                if force_exp and self._agg_epoch_seq > 0:
                    self._worker_export_compact_record("PERIODIC_CHECKPOINT")
                self._worker_publish_snapshot()
                sync_done.set()

            # 4. Handle idempotent epoch seal (`disable`)
            seal_cmd = self._pending_seal_cmd
            if seal_cmd is not None:
                self._pending_seal_cmd = None
                target_ep, reason, seal_utc, seal_mono_ns, seal_done = seal_cmd
                self._worker_drain_data_batch(drain_all=True)
                if target_ep == self._agg_epoch_seq and target_ep > 0 and self._agg_sealed_for_epoch_seq != target_ep:
                    if self._agg_active_start_mono_ns > 0 and seal_mono_ns > self._agg_active_start_mono_ns:
                        self._agg_accumulated_enabled_ns += (seal_mono_ns - self._agg_active_start_mono_ns)
                        self._agg_active_start_mono_ns = 0
                    if self._agg_disconnected_since_mono_ns > 0 and seal_mono_ns > self._agg_disconnected_since_mono_ns:
                        self._agg_accumulated_disconnected_ns += (seal_mono_ns - self._agg_disconnected_since_mono_ns)
                        self._agg_disconnected_since_mono_ns = 0
                    self._agg_disabled_at_utc = seal_utc
                    self._agg_epoch_closed = True
                    if self._agg_control_intervals and self._agg_control_intervals[-1]["disabled_utc"] is None:
                        self._agg_control_intervals[-1]["disabled_utc"] = seal_utc
                        self._agg_control_intervals[-1]["reason"] = reason
                    self._worker_finalize_active_buckets()
                    ok = self._worker_export_compact_record("EPOCH_SEALED", {"seal_reason": reason})
                    if ok or self._exporter.configured_dir is None:
                        self._agg_sealed_for_epoch_seq = target_ep
                        self._agg_epoch_durable = True
                    else:
                        self._agg_epoch_durable = False
                    self._worker_publish_snapshot()
                seal_done.set()

            # 5. Handle reset (ONLY clears after verified durable seal)
            rst_cmd = self._pending_reset_cmd
            if rst_cmd is not None:
                self._pending_reset_cmd = None
                _cleared_ep, rst_done = rst_cmd
                if self._agg_epoch_durable or self._agg_ingress_attempts == 0:
                    self._init_aggregator_state(epoch_seq=self._active_epoch_seq)
                    self._agg_epoch_closed = True
                    self._agg_epoch_durable = True
                    self._worker_publish_snapshot()
                rst_done.set()

            # 6. Periodic checkpoint & snapshot refresh (guaranteed deadline even under sustained load!)
            now_ns = time.perf_counter_ns()
            if (
                self._capture_enabled
                and self._agg_epoch_seq > 0
                and not self._agg_epoch_closed
                and (now_ns - self._agg_last_checkpoint_ns) >= DEFAULT_EXPORT_INTERVAL_NS
            ):
                self._agg_last_checkpoint_ns = now_ns
                self._worker_export_compact_record("PERIODIC_CHECKPOINT")

            if (processed > 0 or self._capture_enabled) and (
                now_ns - self._agg_last_snapshot_pub_ns
            ) >= SNAPSHOT_PUBLISH_INTERVAL_NS:
                self._worker_publish_snapshot()

            qlen = len(self._inbox)
            in_flight_b = self._enqueued_wire_bytes_total - self._dequeued_wire_bytes_total
            if qlen == 0 and self._pending_enable_epoch is None and self._pending_seal_cmd is None:
                time.sleep(WORKER_POLL_SLEEP_S)
            elif qlen >= 8 or in_flight_b >= 262144:
                # Drain burst backlog without 2-3 ms OS timer-slack stalls while still yielding GIL
                time.sleep(0)
            else:
                # Gentle cooperative sleep under normal low-rate streaming
                time.sleep(0.0005)

    def _worker_record_lifecycle_event(self, ev: str, detail: Any, mono_ns: int) -> None:
        wall_ms = int(time.time() * 1000)
        try:
            detail_str = str(detail)[:160] if detail is not None else None
        except Exception:
            detail_str = "<UNFORMATTABLE_DETAIL>"

        if ev == "open":
            self._agg_ws_open_count += 1
            self._agg_connected_state = True
            if self._agg_last_disconnect_mono_ns > 0 and mono_ns > self._agg_last_disconnect_mono_ns:
                gap_ms = (mono_ns - self._agg_last_disconnect_mono_ns) / 1_000_000.0
                if gap_ms > self._agg_max_disconnect_gap_ms:
                    self._agg_max_disconnect_gap_ms = gap_ms
                self._agg_last_disconnect_mono_ns = 0
            if self._agg_disconnected_since_mono_ns > 0 and mono_ns > self._agg_disconnected_since_mono_ns:
                self._agg_accumulated_disconnected_ns += (mono_ns - self._agg_disconnected_since_mono_ns)
                self._agg_disconnected_since_mono_ns = 0
        elif ev == "close":
            self._agg_ws_close_count += 1
            self._agg_connected_state = False
            if self._agg_last_disconnect_mono_ns == 0:
                self._agg_last_disconnect_mono_ns = mono_ns
            if self._agg_disconnected_since_mono_ns == 0:
                self._agg_disconnected_since_mono_ns = mono_ns
        elif ev == "error":
            self._agg_ws_error_count += 1
        elif ev == "reconnecting":
            self._agg_ws_reconnecting_count += 1
            self._agg_connected_state = False
            if self._agg_last_disconnect_mono_ns == 0:
                self._agg_last_disconnect_mono_ns = mono_ns
            if self._agg_disconnected_since_mono_ns == 0:
                self._agg_disconnected_since_mono_ns = mono_ns
        elif ev == "autoReconnectStopped":
            self._agg_ws_auto_reconnect_stopped_count += 1

        if len(self._agg_lifecycle_events) == LIFECYCLE_RING_MAXLEN:
            self._agg_lifecycle_evictions += 1
        self._agg_lifecycle_events.append({"event": ev, "wall_ms": wall_ms, "detail": detail_str})

    @staticmethod
    def _read_passive_s205_telemetry_off_callback() -> Dict[str, Any]:
        out: Dict[str, Any] = {"loaded": False}
        try:
            import sys
            mod = sys.modules.get("app.analytics.option_sniffer_advance_live.engine")
            if mod is None:
                return out
            cls = getattr(mod, "AdvanceOptionSnifferEngine", None)
            inst = getattr(cls, "_instance", None) if cls is not None else None
            if inst is None:
                return out
            out["loaded"] = True
            out["running"] = bool(getattr(inst, "_running", False))
            out["last_eval_wall_ms"] = getattr(inst, "_last_eval_duration_ms", None)
            out["last_scan_cycle_ms"] = getattr(inst, "_last_scan_duration_ms", None)
        except Exception:
            pass
        return out

    def _build_snapshot_from_aggregator(
        self, include_rings: bool = False, include_hist_bins: Any = False
    ) -> Dict[str, Any]:
        now_mono_ns = time.perf_counter_ns()
        ist_today = (
            datetime.datetime.fromtimestamp(self._agg_last_frame_ms / 1000.0, tz=IST_TZ).date()
            if self._agg_last_frame_ms is not None
            else datetime.datetime.now(IST_TZ).date()
        )
        live_enabled_ns = self._agg_accumulated_enabled_ns
        if self._capture_enabled and self._agg_active_start_mono_ns > 0 and now_mono_ns > self._agg_active_start_mono_ns:
            live_enabled_ns += (now_mono_ns - self._agg_active_start_mono_ns)
        enabled_span_s = max(0.0, live_enabled_ns / 1_000_000_000.0)

        live_disconn_ns = self._agg_accumulated_disconnected_ns
        if self._capture_enabled and self._agg_disconnected_since_mono_ns > 0 and now_mono_ns > self._agg_disconnected_since_mono_ns:
            live_disconn_ns += (now_mono_ns - self._agg_disconnected_since_mono_ns)
        disconn_span_s = min(enabled_span_s, max(0.0, live_disconn_ns / 1_000_000_000.0))
        connected_span_s = max(0.0, enabled_span_s - disconn_span_s)

        frame_span_s = (
            (self._agg_last_frame_mono_ns - self._agg_first_frame_mono_ns) / 1_000_000_000.0
            if self._agg_decoded_ok_frames >= 2 and self._agg_last_frame_mono_ns > self._agg_first_frame_mono_ns
            else 0.0
        )
        effective_span_s = connected_span_s if connected_span_s > 0.0 else (enabled_span_s if enabled_span_s > 0.0 else frame_span_s)
        sustained_frames_s = (self._agg_ingress_attempts / effective_span_s) if effective_span_s > 0 else 0.0
        sustained_bytes_s = (self._agg_ingress_wire_bytes / effective_span_s) if effective_span_s > 0 else 0.0

        intended_sub = self._agg_current_subscribed_keys
        delivered_in_intended = self._agg_seen_in_current_u_epoch & intended_sub
        current_u_coverage = (
            round(len(delivered_in_intended) / float(len(intended_sub)), 6) if intended_sub else 0.0
        )

        core_all = set(S1_CORE_INSTRUMENT_KEYS.keys())
        core_intended = core_all & intended_sub if intended_sub else set()
        core_delivered_u_epoch = self._agg_u_epoch_seen_core & core_intended

        sub_opt_1_7: Set[str] = set()
        sub_opt_other: Set[str] = set()
        for k, cm in self._agg_current_contract_meta.items():
            if intended_sub and k not in intended_sub:
                continue
            if cm.underlying == "NIFTY" and not cm.is_futures:
                dd, _ = self._classify_dte_days_for_date(cm, ist_today)
                if dd is not None and 1 <= dd <= 7:
                    sub_opt_1_7.add(k)
                else:
                    sub_opt_other.add(k)

        # Age silence against `now_mono_ns` while enabled & connected so total feed silence ages live!
        if self._capture_enabled and self._agg_connected_state:
            obs_end_ns = max(now_mono_ns, self._agg_last_frame_mono_ns)
        else:
            obs_end_ns = self._agg_last_frame_mono_ns if self._agg_last_frame_mono_ns > 0 else now_mono_ns

        worst_inst_update_gap_ms = 0.0
        worst_inst_silence_ms = 0.0
        worst_inst_key: Optional[str] = None
        worst_book_silence_ms = 0.0
        worst_book_key: Optional[str] = None
        never_delivered_s1_keys: List[str] = []

        for ik, pst in self._agg_per_s1_inst.items():
            if pst.updates == 0:
                never_delivered_s1_keys.append(ik)
                base_ns = pst.seeded_mono_ns if pst.seeded_mono_ns > 0 else self._agg_active_start_mono_ns
                unseen_ms = max(0.0, (obs_end_ns - base_ns) / 1_000_000.0) if (base_ns > 0 and obs_end_ns > base_ns) else 0.0
                if unseen_ms > worst_inst_silence_ms:
                    worst_inst_silence_ms = unseen_ms
                    worst_inst_key = ik
                continue
            if pst.max_update_gap_ms > worst_inst_update_gap_ms:
                worst_inst_update_gap_ms = pst.max_update_gap_ms
            trail_ms = max(0.0, (obs_end_ns - pst.last_mono_ns) / 1_000_000.0) if obs_end_ns >= pst.last_mono_ns else 0.0
            eff_absence = max(pst.max_update_gap_ms, trail_ms)
            if eff_absence > worst_inst_silence_ms:
                worst_inst_silence_ms = eff_absence
                worst_inst_key = ik

            if pst.valid_books > 0:
                b_trail = max(0.0, (obs_end_ns - pst.last_book_mono_ns) / 1_000_000.0) if obs_end_ns >= pst.last_book_mono_ns else 0.0
                eff_bgap = max(pst.max_book_gap_ms, b_trail)
            else:
                b_ref = pst.seeded_mono_ns if pst.seeded_mono_ns > 0 else pst.first_mono_ns
                eff_bgap = max(0.0, (obs_end_ns - b_ref) / 1_000_000.0) if (b_ref > 0 and obs_end_ns >= b_ref) else 0.0
            if eff_bgap > worst_book_silence_ms:
                worst_book_silence_ms = eff_bgap
                worst_book_key = ik

        overflow_drops = self._inbox_overflow_drops
        empty_supplied_universe = (
            self._universe_supplier is not None
            and self._agg_universe_epoch_seq > 0
            and len(intended_sub) == 0
        )
        meas_valid = (
            self._agg_coverage_valid
            and not empty_supplied_universe
            and overflow_drops == 0
            and self._stage_event_drops == 0
            and not self._worker_dead_latched
        )
        inv_reasons = sorted(
            self._agg_invalidation_reasons
            | ({"EMPTY_SUBSCRIBED_UNIVERSE"} if empty_supplied_universe else set())
            | ({"INBOX_OVERFLOW"} if overflow_drops > 0 else set())
            | ({"STAGE_EVENT_DROPPED"} if self._stage_event_drops > 0 else set())
        )

        # Distinct frozen-S1 input sufficiency check (NEVER conflated with measurement integrity!)
        missing_s1_inputs: List[str] = sorted(core_all - core_delivered_u_epoch)
        if len(self._agg_u_epoch_seen_opt_1_7 & self._agg_u_epoch_seen_opt_1lot) == 0:
            missing_s1_inputs.append("NIFTY_OPTIONS_1_7_DTE_VERIFIED_1LOT_BOOK")
        s1_inputs_complete = (len(missing_s1_inputs) == 0)

        unexported_tail = max(0, self._agg_ingress_attempts - self._agg_last_exported_frames)
        exp_status = self._exporter.status_snapshot(now_mono_ns)
        exp_status["epoch_id"] = self._agg_epoch_id
        exp_status["unexported_tail_frames"] = unexported_tail
        if (
            not self._capture_enabled
            and self._agg_epoch_closed
            and unexported_tail == 0
            and self._agg_epoch_durable
        ):
            if self._agg_sealed_for_epoch_seq == self._agg_epoch_seq and self._agg_epoch_seq > 0:
                exp_status["tail_state"] = "COMPLETE_SEALED"
            else:
                exp_status["tail_state"] = "IDLE_EMPTY_EPOCH"
        else:
            exp_status["tail_state"] = "UNEXPORTED_LIVE_OR_INCOMPLETE_TAIL"

        full_added_sum = self._agg_hist_full_probe_added_us.summary(include_bins=include_hist_bins)
        handoff_sum = self._agg_hist_ingress_handoff_us.summary(include_bins=include_hist_bins)
        app_ds_sum = self._agg_hist_app_downstream_us.summary(include_bins=include_hist_bins)
        total_cb_sum = self._agg_hist_total_callback_us.summary(include_bins=include_hist_bins)
        worker_slice_sum = self._agg_hist_worker_slice_us.summary(include_bins=include_hist_bins)
        frame_bytes_sum = self._agg_hist_frame_bytes.summary(include_bins=include_hist_bins)
        rate_1s_bytes_sum = self._agg_hist_1s_all_bytes.summary(include_bins=include_hist_bins)
        if self._worker_alive_flag:
            time.sleep(0)

        sample_cnt = full_added_sum["count"]
        thresholds_met = (
            sample_cnt >= 1
            and meas_valid
            and self._agg_clock_certification_valid
            and full_added_sum["p99_upper_bound"] <= LATENCY_P99_BUDGET_US
            and full_added_sum["p999_upper_bound"] <= LATENCY_SINGLE_BREACH_US
        )
        prod_gate_certified = thresholds_met and (sample_cnt >= MIN_SAMPLES_FOR_PRODUCTION_GATE)

        opt_1_7_c = self._agg_cohorts["NIFTY_OPTIONS_1_7_DTE"]
        opt_other_c = self._agg_cohorts["NIFTY_OPTIONS_OTHER_DTE"]
        tot_opt_updates = opt_1_7_c.updates + opt_other_c.updates
        tot_opt_1lot_books = opt_1_7_c.book_usable_1lot_verified_updates + opt_other_c.book_usable_1lot_verified_updates

        s1_update_share = (
            self._agg_s1_relevant_updates / float(self._agg_total_instrument_updates)
            if self._agg_total_instrument_updates > 0
            else 0.0
        )
        p95_ub_bytes_s = rate_1s_bytes_sum["p95_upper_bound"] if rate_1s_bytes_sum["count"] > 0 else sustained_bytes_s
        session_s = 22500.0

        codex_feasibility = {
            "coverage_valid": meas_valid,
            "measurement_integrity_valid": meas_valid,
            "s1_required_inputs_complete": s1_inputs_complete,
            "missing_s1_required_inputs": missing_s1_inputs,
            "invalidation_reasons": inv_reasons,
            "clock_certification_valid": self._agg_clock_certification_valid,
            "wall_clock_step_events": self._agg_clock_step_events,
            "enabled_monotonic_span_seconds": round(enabled_span_s, 3),
            "connected_enabled_seconds": round(connected_span_s, 3),
            "disconnected_enabled_seconds": round(disconn_span_s, 3),
            "first_to_last_frame_span_seconds": round(frame_span_s, 3),
            "sustained_ingress_attempts_per_sec": round(sustained_frames_s, 3),
            "sustained_wire_bytes_per_sec": round(sustained_bytes_s, 2),
            "peak_100ms_frames": self._agg_peak_100ms_frames,
            "peak_100ms_bytes": self._agg_peak_100ms_bytes,
            "peak_1s_frames": self._agg_peak_1s_frames,
            "peak_1s_bytes": self._agg_peak_1s_bytes,
            "confirmed_zero_1s_intervals": self._agg_confirmed_zero_1s_bins,
            "confirmed_zero_100ms_intervals": self._agg_confirmed_zero_100ms_bins,
            "min_writer_drain_bytes_per_sec_2x_peak": int(self._agg_peak_1s_bytes * 2),
            "slot_64kib_fit": {
                "frames_exceeding_64kib": self._agg_frames_gt_64kib,
                "frames_exceeding_1mib": self._agg_frames_gt_1mib,
                "max_frame_bytes": int(frame_bytes_sum["max"]),
                "fits_64kib_slot": (self._agg_frames_gt_64kib == 0),
                "fits_1mib_hard_cap": (self._agg_frames_gt_1mib == 0),
            },
            "callback_latency_gates_conservative": {
                "sample_count": sample_cnt,
                "min_production_certification_samples": MIN_SAMPLES_FOR_PRODUCTION_GATE,
                "gate_status": (
                    "INSUFFICIENT_SAMPLES"
                    if sample_cnt == 0
                    else ("PASS_PRODUCTION_CERTIFIED" if prod_gate_certified else ("PASS_SAMPLE_BOUNDED" if thresholds_met else "FAIL"))
                ),
                "full_probe_added_p99_upper_bound_us": full_added_sum["p99_upper_bound"],
                "full_probe_added_p999_upper_bound_us": full_added_sum["p999_upper_bound"],
                "full_probe_added_exceed_100us_count": full_added_sum["exceed_100_count"],
                "full_probe_added_exceed_1000us_count": full_added_sum["exceed_1000_count"],
                "passes_p99_le_100us_and_p999_le_1000us": thresholds_met,
                "production_certified_50plus_samples": prod_gate_certified,
                "worker_slice_p99_upper_bound_us": worker_slice_sum["p99_upper_bound"],
                "app_downstream_dispatch_p99_upper_bound_us": app_ds_sum["p99_upper_bound"],
                "total_callback_p99_upper_bound_us": total_cb_sum["p99_upper_bound"],
                "qualification": (
                    "full_probe_added_callback_us times complete callback entry-to-exit probe overhead "
                    "(including _inbox.append). Worker GIL interference must be validated via matched-load "
                    "ON vs OFF total callback & scheduling-lag benchmarks."
                ),
            },
            "s1_option_book_and_time_provenance": {
                "s1_core_intended_subscribed_count": len(core_intended),
                "s1_core_delivered_in_current_epoch_count": len(core_delivered_u_epoch),
                "never_delivered_intended_s1_keys_count": len(never_delivered_s1_keys),
                "never_delivered_intended_s1_keys_sample": sorted(never_delivered_s1_keys)[:20],
                "nifty_options_valid_2sided_delivery_updates": self._agg_s1_option_valid_books,
                "nifty_options_distinct_top_book_changes": self._agg_s1_option_distinct_books,
                "nifty_options_usable_1lot_verified_book_updates": self._agg_s1_option_1lot_books,
                "non_option_s1_equity_or_futures_book_updates": self._agg_s1_non_option_books,
                "nifty_option_usable_1lot_book_ratio": (
                    round(tot_opt_1lot_books / float(tot_opt_updates), 6) if tot_opt_updates > 0 else 0.0
                ),
                "per_instrument_worst_update_gap_ms": round(worst_inst_update_gap_ms, 2),
                "per_instrument_worst_absence_or_silence_ms": round(worst_inst_silence_ms, 2),
                "per_instrument_worst_silence_key": worst_inst_key,
                "per_instrument_worst_valid_book_silence_ms": round(worst_book_silence_ms, 2),
                "per_instrument_worst_valid_book_key": worst_book_key,
                "provider_current_ts_present_ratio": (
                    round(self._agg_frames_with_current_ts / float(self._agg_decoded_ok_frames), 6)
                    if self._agg_decoded_ok_frames > 0
                    else 0.0
                ),
                "unverified_candidate_quote_ts_field_observations": sum(
                    c.unverified_candidate_quote_ts_fields for c in self._agg_cohorts.values()
                ),
                "time_and_proto3_qualification": (
                    "Receipt time is a local observed-book proxy; SDK subscription ACKs and exchange book "
                    "timestamps are unavailable. Upstox proto3 omits scalar oi=0.0 on binary wire."
                ),
            },
            "heuristic_22500s_session_projections_unqualified_for_final_sizing": {
                "qualification": (
                    "Full-envelope wire bytes extrapolated to 22,500s; S1 update share is count-based "
                    "and 2.8x JSON/base64 is a prototype assumption."
                ),
                "measured_full_envelope_sustained_gib": round((sustained_bytes_s * session_s) / float(1024 ** 3), 4),
                "measured_full_envelope_p95_ub_gib": round((p95_ub_bytes_s * session_s) / float(1024 ** 3), 4),
                "assumed_2_8x_json_base64_est_gib": round((sustained_bytes_s * 2.8 * session_s) / float(1024 ** 3), 4),
                "update_proportion_heuristic_s1_gib": round(
                    (sustained_bytes_s * s1_update_share * session_s) / float(1024 ** 3), 4
                ),
            },
        }

        res: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "probe_id": self._probe_id,
            "boot_id": self._boot_id,
            "process_id": self._process_id,
            "probe_code_sha256": PROBE_CODE_SHA256,
            "epoch_id": self._agg_epoch_id,
            "epoch_seq": self._agg_epoch_seq,
            "enabled": self._capture_enabled,
            "attachment": self.inspect_attachment(),
            "startup_recovery_summary": dict(self._startup_recovery_summary),
            "control_intervals": list(self._agg_control_intervals),
            "control_intervals_truncated": (self._agg_control_intervals_evicted > 0),
            "control_intervals_evicted_count": self._agg_control_intervals_evicted,
            "ingress_totals": {
                "ingress_attempts_total": self._agg_ingress_attempts,
                "ingress_wire_bytes_total": self._agg_ingress_wire_bytes,
                "decoded_ok_frames": self._agg_decoded_ok_frames,
                "sdk_decode_errors": self._agg_sdk_decode_errors,
                "sdk_dict_errors": self._agg_sdk_dict_errors,
                "downstream_emit_errors": self._agg_downstream_emit_errors,
                "incomplete_frames_discarded": self._agg_incomplete_frames_discarded,
                "instrument_updates": self._agg_total_instrument_updates,
                "s1_relevant_frames": self._agg_s1_relevant_frames,
                "s1_relevant_updates": self._agg_s1_relevant_updates,
                "s1_option_valid_2sided_books": self._agg_s1_option_valid_books,
                "s1_option_distinct_top_book_changes": self._agg_s1_option_distinct_books,
                "s1_option_usable_1lot_books": self._agg_s1_option_1lot_books,
                "s1_non_option_equity_futures_books": self._agg_s1_non_option_books,
                "wire_envelope_types": dict(self._agg_wire_enum_type_counts),
                "decoded_dict_type_omitted_default_enum0": self._agg_dict_type_omitted_enum0,
            },
            "distributions": {
                "frame_bytes": frame_bytes_sum,
                "instruments_per_frame": self._agg_hist_inst_per_frame.summary(include_bins=include_hist_bins),
                "unverified_receipt_minus_provider_current_ts_ms": self._agg_hist_provider_lag_ms.summary(
                    include_bins=include_hist_bins
                ),
                "burst_100ms_nonempty_frames": self._agg_hist_100ms_nonempty_frames.summary(
                    include_bins=include_hist_bins
                ),
                "burst_100ms_nonempty_bytes": self._agg_hist_100ms_nonempty_bytes.summary(
                    include_bins=include_hist_bins
                ),
                "rate_1s_including_zeros_frames": self._agg_hist_1s_all_frames.summary(
                    include_bins=include_hist_bins
                ),
                "rate_1s_including_zeros_bytes": rate_1s_bytes_sum,
            },
            "burst_peaks_and_active_accumulators": {
                "peak_100ms_frames": self._agg_peak_100ms_frames,
                "peak_100ms_bytes": self._agg_peak_100ms_bytes,
                "peak_100ms_instruments": self._agg_peak_100ms_insts,
                "peak_100ms_s1_option_books": self._agg_peak_100ms_opt_books,
                "peak_1s_frames": self._agg_peak_1s_frames,
                "peak_1s_bytes": self._agg_peak_1s_bytes,
                "peak_1s_instruments": self._agg_peak_1s_insts,
                "peak_1s_s1_updates": self._agg_peak_1s_s1_updates,
                "peak_1s_s1_option_books": self._agg_peak_1s_opt_books,
                "active_unfinished_1m_frames": self._agg_cur_1m_frames if self._agg_cur_1m_slot >= 0 else 0,
                "confirmed_zero_100ms_bins": self._agg_confirmed_zero_100ms_bins,
                "confirmed_zero_1s_bins": self._agg_confirmed_zero_1s_bins,
            },
            "delivery_coverage": {
                "coverage_valid": meas_valid,
                "measurement_integrity_valid": meas_valid,
                "s1_required_inputs_complete": s1_inputs_complete,
                "missing_s1_required_inputs": missing_s1_inputs,
                "invalidation_reasons": inv_reasons,
                "universe_epoch_seq": self._agg_universe_epoch_seq,
                "contract_revision_seq": self._agg_contract_rev_seq,
                "intended_subscribed_keys_current_epoch": len(intended_sub),
                "delivered_in_intended_current_epoch": len(delivered_in_intended),
                "current_epoch_coverage_ratio": current_u_coverage,
                "delivered_outside_current_subscribed_count": self._agg_delivered_outside_subscribed_count,
                "lifetime_unique_keys_capped": len(self._agg_seen_all_keys),
                "s1_core_intended_subscribed": sorted(core_intended),
                "s1_core_delivered_in_intended": sorted(core_delivered_u_epoch),
                "s1_core_missing_from_intended": sorted(core_intended - core_delivered_u_epoch),
                "nifty_options_1_7_dte_subscribed": len(sub_opt_1_7),
                "nifty_options_1_7_dte_delivered_in_subscribed": len(self._agg_u_epoch_seen_opt_1_7 & sub_opt_1_7),
                "nifty_options_other_dte_subscribed": len(sub_opt_other),
                "nifty_options_other_dte_delivered_in_subscribed": len(self._agg_u_epoch_seen_opt_other & sub_opt_other),
                "nifty_options_with_valid_2sided_book": len(self._agg_seen_opt_valid_book_keys),
                "nifty_options_with_usable_1lot_book": len(self._agg_seen_opt_1lot_book_keys),
                "dte_fallback_without_expiry_count": self._agg_dte_fallback_count,
                "prior_universe_epochs": list(self._agg_universe_epochs),
            },
            "cohorts": {name: self._agg_cohorts[name].to_dict() for name in COHORT_NAMES},
            "latencies_us": {
                "sdk_decode_us": self._agg_hist_sdk_decode_us.summary(include_bins=include_hist_bins),
                "sdk_dict_convert_us": self._agg_hist_sdk_dict_us.summary(include_bins=include_hist_bins),
                "app_downstream_dispatch_us": app_ds_sum,
                "raw_downstream_including_hook_us": self._agg_hist_raw_downstream_us.summary(
                    include_bins=include_hist_bins
                ),
                "listener_broadcast_us": self._agg_hist_listener_us.summary(include_bins=include_hist_bins),
                "equity_ingest_us": self._agg_hist_equity_us.summary(include_bins=include_hist_bins),
                "downstream_stage_hook_us": self._agg_hist_stage_hook_us.summary(include_bins=include_hist_bins),
                "callback_ingress_handoff_us": handoff_sum,
                "full_probe_added_callback_us": full_added_sum,
                "worker_slice_duration_us": worker_slice_sum,
                "total_callback_us": total_cb_sum,
                "bar_flush_15m_us": self._agg_hist_bar_flush_15m_us.summary(include_bins=include_hist_bins),
                "freeze_snapshot_us": self._agg_hist_freeze_snapshot_us.summary(include_bins=include_hist_bins),
                "s205_passive_telemetry": self._read_passive_s205_telemetry_off_callback(),
            },
            "lifecycle_and_reconnects": {
                "ws_open_count": self._agg_ws_open_count,
                "ws_close_count": self._agg_ws_close_count,
                "ws_error_count": self._agg_ws_error_count,
                "ws_reconnecting_count": self._agg_ws_reconnecting_count,
                "ws_auto_reconnect_stopped_count": self._agg_ws_auto_reconnect_stopped_count,
                "max_disconnect_gap_ms": round(self._agg_max_disconnect_gap_ms, 2),
                "recent_events": list(self._agg_lifecycle_events),
            },
            "failures_and_bounds": {
                "worker_alive": bool(self._worker_thread is not None and self._worker_thread.is_alive() and not self._worker_dead_latched),
                "worker_dead_latched": self._worker_dead_latched,
                "supplier_slow_warnings": self._agg_supplier_slow_warnings,
                "non_bytes_payload_count": self._agg_non_bytes_frames,
                "sdk_decode_errors": self._agg_sdk_decode_errors,
                "sdk_dict_errors": self._agg_sdk_dict_errors,
                "downstream_emit_errors": self._agg_downstream_emit_errors,
                "incomplete_frames_discarded": self._agg_incomplete_frames_discarded,
                "observer_boundary_faults": self._observer_boundary_faults,
                "worker_internal_errors": self._agg_worker_internal_errors,
                "auto_disabled_on_fault": self._auto_disabled_on_fault,
                "auto_disabled_reason": self._auto_disabled_reason,
                "inbox_pending_samples": len(self._inbox),
                "inbox_max_samples": MAX_INBOX_SAMPLES,
                "inbox_in_flight_wire_bytes": max(
                    0, self._enqueued_wire_bytes_total - self._dequeued_wire_bytes_total
                ),
                "inbox_max_wire_bytes": MAX_INBOX_WIRE_BYTES,
                "inbox_overflow_drops": overflow_drops,
                "inbox_overflow_bytes": self._inbox_overflow_bytes,
                "stage_event_drops": self._stage_event_drops,
                "lifecycle_event_drops": self._lifecycle_event_drops,
                "post_disable_inflight_drops": self._post_disable_inflight_drops,
                "control_coalesced_count": self._control_coalesced_count,
                "oversized_key_drops": self._agg_oversized_key_drops,
                "seen_key_cap_drops": self._agg_seen_key_cap_drops,
                "opt_cohort_key_cap_drops": self._agg_opt_cohort_key_cap_drops,
                "per_inst_key_cap_drops": self._agg_per_inst_key_cap_drops,
                "universe_snapshot_cap_drops": self._agg_universe_snapshot_cap_drops,
                "ring_100ms_len": len(self._agg_ring_100ms),
                "ring_100ms_evictions": self._agg_ring_100ms_evictions,
                "ring_1s_len": len(self._agg_ring_1s),
                "ring_1s_evictions": self._agg_ring_1s_evictions,
                "ring_1m_len": len(self._agg_ring_1m),
                "ring_1m_evictions": self._agg_ring_1m_evictions,
                "lifecycle_ring_evictions": self._agg_lifecycle_evictions,
            },
            "durable_exporter": exp_status,
            "codex_feasibility_metrics": codex_feasibility,
        }

        if include_rings:
            res["recent_1m_rollups"] = list(self._agg_ring_1m)
            res["recent_1s_rates"] = list(self._agg_ring_1s)
            res["recent_100ms_bursts"] = list(self._agg_ring_100ms)
        return res

    @staticmethod
    def _build_compact_from_snapshot(snap: Dict[str, Any]) -> Dict[str, Any]:
        tot = snap["ingress_totals"]
        cf = snap["codex_feasibility_metrics"]
        gates = cf["callback_latency_gates_conservative"]
        return {
            "probe_id": snap["probe_id"],
            "epoch_id": snap["epoch_id"],
            "enabled": snap["enabled"],
            "wrapper_attached": snap["attachment"].get("wrapper_attached", False),
            "coverage_valid": cf["coverage_valid"],
            "s1_required_inputs_complete": cf["s1_required_inputs_complete"],
            "ingress_attempts_total": tot["ingress_attempts_total"],
            "ingress_wire_bytes_total": tot["ingress_wire_bytes_total"],
            "sustained_frames_per_sec": cf["sustained_ingress_attempts_per_sec"],
            "sustained_bytes_per_sec": cf["sustained_wire_bytes_per_sec"],
            "peak_100ms_frames": cf["peak_100ms_frames"],
            "peak_1s_frames": cf["peak_1s_frames"],
            "s1_option_usable_1lot_books": tot["s1_option_usable_1lot_books"],
            "full_probe_added_p99_ub_us": gates["full_probe_added_p99_upper_bound_us"],
        }


def build_instrumented_streamer_class(base_streamer_cls: Any, probe: S1IngressMeasurementProbe) -> Any:
    """
    Lightweight pass-through subclass of `upstox_client.MarketDataStreamerV3`.
    - Passes immutable `wire_payload=bytes(message)` (`0` copy when `message` is `bytes`) so
      caller `data_dict` is NEVER retained by the probe (`< 4.2 MiB` RAM, immune to mutation).
    - Pairs `frame_seq` via thread-local storage across `self.emit(...)`.
    """
    from google.protobuf import json_format

    class InstrumentedMarketDataStreamerV3(base_streamer_cls):
        _s1_probe = probe
        _s1_probe_wrapper_version = PROBE_WRAPPER_VERSION

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            p = self._s1_probe
            if p is not None:
                try:
                    p.register_streamer_instance(self)
                except Exception:
                    pass

        def handle_message(self, ws: Any, message: Any) -> Any:
            p = self._s1_probe
            try:
                active = (p is not None and p._capture_enabled)
            except Exception:
                active = False
            if not active:
                return super().handle_message(ws, message)

            try:
                t_pre0 = time.perf_counter_ns()
                recv_wall_ns = time.time_ns()
                is_bytes = isinstance(message, (bytes, bytearray, memoryview))
                wire_bytes: Optional[bytes] = bytes(message) if is_bytes else None
                raw_bytes = len(wire_bytes) if wire_bytes is not None else 0
                frame_seq = p.next_frame_seq()
                t0_ns = time.perf_counter_ns()
                pre_ns = max(0, t0_ns - t_pre0)
            except Exception as pre_exc:
                try:
                    p.note_observer_fault("handle_message_pre", pre_exc)
                except Exception:
                    pass
                return super().handle_message(ws, message)

            try:
                decoded_data = self.decode_protobuf(message)
            except Exception:
                try:
                    p.record_ingress_error_attempt(
                        stage="decode_protobuf",
                        raw_bytes=raw_bytes,
                        is_bytes=is_bytes,
                        recv_wall_ns=recv_wall_ns,
                        t0_ns=t0_ns,
                    )
                except Exception as obs_e:
                    try:
                        p.note_observer_fault("decode_error_hook", obs_e)
                    except Exception:
                        pass
                raise
            t1_ns = time.perf_counter_ns()

            t_w0 = time.perf_counter_ns()
            wire_type_int = -1
            try:
                wire_type_int = int(getattr(decoded_data, "type", -1))
            except Exception:
                wire_type_int = -1
            pre_ns += max(0, time.perf_counter_ns() - t_w0)

            try:
                data_dict = json_format.MessageToDict(decoded_data)
            except Exception:
                try:
                    p.record_ingress_error_attempt(
                        stage="message_to_dict",
                        raw_bytes=raw_bytes,
                        is_bytes=is_bytes,
                        recv_wall_ns=recv_wall_ns,
                        t0_ns=t0_ns,
                        wire_type_int=wire_type_int,
                    )
                except Exception as obs_e:
                    try:
                        p.note_observer_fault("dict_error_hook", obs_e)
                    except Exception:
                        pass
                raise
            t2_ns = time.perf_counter_ns()

            t_seq0 = time.perf_counter_ns()
            omitted_enum0 = bool(wire_type_int == 0 and isinstance(data_dict, dict) and "type" not in data_dict)
            try:
                p._tls.active_frame_seq = frame_seq
                p._tls.last_stage_hook = None
            except Exception:
                pass
            pre_ns += max(0, time.perf_counter_ns() - t_seq0)

            try:
                self.emit(self.Event["MESSAGE"], data_dict)
            except Exception:
                try:
                    p._tls.active_frame_seq = 0
                    p.record_ingress_error_attempt(
                        stage="emit_message",
                        raw_bytes=raw_bytes,
                        is_bytes=is_bytes,
                        recv_wall_ns=recv_wall_ns,
                        t0_ns=t0_ns,
                        wire_type_int=wire_type_int,
                    )
                except Exception as obs_e:
                    try:
                        p.note_observer_fault("emit_error_hook", obs_e)
                    except Exception:
                        pass
                raise
            finally:
                try:
                    p._tls.active_frame_seq = 0
                except Exception:
                    pass
            t3_ns = time.perf_counter_ns()

            try:
                p.record_ingress_frame(
                    raw_bytes=raw_bytes,
                    is_bytes=is_bytes,
                    wire_type_int=wire_type_int,
                    wire_payload=wire_bytes,
                    data_dict=None if wire_bytes is not None else data_dict,
                    frame_seq=frame_seq,
                    dict_omitted_type_enum0=omitted_enum0,
                    recv_wall_ns=recv_wall_ns,
                    t0_ns=t0_ns,
                    t1_ns=t1_ns,
                    t2_ns=t2_ns,
                    t3_ns=t3_ns,
                    pre_overhead_ns=pre_ns,
                )
            except Exception as post_exc:
                try:
                    p.note_observer_fault("handle_message_post_emit", post_exc)
                except Exception:
                    pass

    return InstrumentedMarketDataStreamerV3


def attach_probe_lifecycle_listeners(streamer: Any, probe: S1IngressMeasurementProbe) -> None:
    if streamer is None or probe is None or not hasattr(streamer, "on"):
        return

    def _make_observer(ev_name: str) -> Callable[..., None]:
        def _obs(*args: Any, **_kwargs: Any) -> None:
            try:
                if not probe._capture_enabled:
                    return
                raw_detail = args[0] if len(args) == 1 else (args if args else None)
                probe.record_ws_lifecycle(ev_name, detail=raw_detail)
            except Exception as exc:
                try:
                    probe.note_observer_fault(f"lifecycle_{ev_name}", exc)
                except Exception:
                    pass

        return _obs

    for ev in ("open", "close", "error", "reconnecting", "autoReconnectStopped"):
        try:
            streamer.on(ev, _make_observer(ev))
        except Exception:
            pass


_GLOBAL_S1_INGRESS_PROBE: Optional[S1IngressMeasurementProbe] = None
_GLOBAL_MANAGEMENT_GUARD: Optional[S1ProbeManagementGuard] = None
_PROBE_INIT_LOCK = threading.Lock()


def peek_s1_ingress_probe() -> Optional[S1IngressMeasurementProbe]:
    """Non-instantiating peek so unauthenticated requests never construct services or probes."""
    return _GLOBAL_S1_INGRESS_PROBE


def get_s1_ingress_probe() -> S1IngressMeasurementProbe:
    global _GLOBAL_S1_INGRESS_PROBE
    if _GLOBAL_S1_INGRESS_PROBE is None:
        with _PROBE_INIT_LOCK:
            if _GLOBAL_S1_INGRESS_PROBE is None:
                _GLOBAL_S1_INGRESS_PROBE = S1IngressMeasurementProbe()
    return _GLOBAL_S1_INGRESS_PROBE


def get_s1_probe_management_guard() -> S1ProbeManagementGuard:
    global _GLOBAL_MANAGEMENT_GUARD
    if _GLOBAL_MANAGEMENT_GUARD is None:
        with _PROBE_INIT_LOCK:
            if _GLOBAL_MANAGEMENT_GUARD is None:
                _GLOBAL_MANAGEMENT_GUARD = S1ProbeManagementGuard()
    return _GLOBAL_MANAGEMENT_GUARD
