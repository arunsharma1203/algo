"""
S1 Prospective Data-Recording Programme — Phase 1 (V2): Hardened Measurement-Only Ingress Probe.

Architecture & Safety Invariants:
  1. Strictly Default-OFF (`enabled = False` unconditionally at construction; no implicit env auto-enable).
  2. Lock-free O(1) live callback handoff: the WebSocket receive callback acquires ZERO mutexes,
     performs ZERO universe refreshes/sorting/disk I/O, and appends a compact observation tuple to
     a bounded inbox (`MAX_INBOX_SAMPLES = 512`, `MAX_INBOX_WIRE_BYTES = 8 MiB`).
  3. Instant nonblocking disable (`_capture_enabled = False` atomic boolean store) + automatic
     health & latency-budget circuit breakers (`p99 <= 100 us`, `p99.9 <= 1000 us` gates).
  4. Total observer fault isolation: outer `try/except` guards surround every callback, lifecycle,
     and stream-service hook while preserving original SDK decode/convert/emit exceptions untouched.
  5. Single-owner background aggregator & asynchronous hash-chained JSONL summary exporter
     preserving mergeable multi-session summaries across restarts without raw tick archiving.
  6. Authenticated, loopback-restricted, CSRF/Origin-guarded, rate-limited management plane with
     separate READ vs ADMIN roles and fail-closed active wrapper verification (`M5`).
"""

import bisect
import collections
import datetime
import enum
import hashlib
import hmac
import json
import math
import os
import threading
import time
import uuid
import weakref
from pathlib import Path
from typing import Any, Callable, Deque, Dict, FrozenSet, List, Optional, Set, Tuple

SCHEMA_VERSION: str = "S1_INGRESS_SUMMARY_V2"
PROBE_WRAPPER_VERSION: str = "S1_STREAMER_WRAPPER_V2"
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

# Strict container, payload, and storage bounds (M1 / H2 / H5)
MAX_INBOX_SAMPLES: int = 512
MAX_INBOX_WIRE_BYTES: int = 8 * 1024 * 1024       # 8 MiB max queued wire bytes
MAX_KEY_CHARS: int = 128
MAX_TRACKED_SEEN_KEYS: int = 4096
MAX_TRACKED_OPTION_COHORT_KEYS: int = 512
MAX_PER_INSTRUMENT_STATS_KEYS: int = 1024
MAX_HISTORICAL_IDENTITY_KEYS: int = 2048
MAX_UNIVERSE_KEYS_SNAPSHOT: int = 4096
MAX_UNIVERSE_CONTRACTS_SNAPSHOT: int = 2048
MAX_UNIVERSE_EPOCHS_RING: int = 64
MAX_CONTROL_AUDIT_RING: int = 128
RING_100MS_MAXLEN: int = 600
RING_1S_MAXLEN: int = 900
RING_1M_MAXLEN: int = 480
LIFECYCLE_RING_MAXLEN: int = 64

MAX_CONSECUTIVE_PROBE_FAULTS: int = 5
MAX_CONSECUTIVE_LATENCY_BREACHES: int = 5         # > 1000 us added callback cost
LATENCY_SINGLE_BREACH_US: float = 1000.0
LATENCY_SEVERE_STALL_US: float = 5000.0
MAX_SEVERE_LATENCY_STALLS: int = 3
CLOCK_STEP_TOLERANCE_MS: float = 250.0

MAX_EXPORT_STORAGE_BYTES: int = 64 * 1024 * 1024  # 64 MiB hard cap on summary storage dir
MAX_EXPORT_RECORD_BYTES: int = 262144             # 256 KiB max single summary JSONL record
DEFAULT_EXPORT_INTERVAL_NS: int = 30_000_000_000  # 30s periodic checkpoint

# Fixed histogram upper bounds (preallocated integer list bins)
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

LOOPBACK_HOSTS: FrozenSet[str] = frozenset({"127.0.0.1", "::1", "localhost"})

# Event sample tags for lock-free inbox
_EVT_FRAME_OK = 1
_EVT_FRAME_ERR = 2
_EVT_DOWNSTREAM_SEG = 3
_EVT_PERIODIC_STAGE = 4
_EVT_LIFECYCLE = 5
_EVT_SEAL_EPOCH = 6


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


class BoundedHistogram:
    """
    Fixed-bin histogram over preallocated Python list counters.
    Note: Python float/int arithmetic allocates transient scalar objects in CPython,
    while container length is strictly fixed (`len(bounds) + 1`).

    Provides BOTH:
      - `pXX_upper_bound`: conservative upper bin edge of the nearest-rank `ceil(q * N)`
        observation (never underestimates threshold compliance via linear interpolation).
      - `pXX_interpolated_est`: diagnostic bin-interpolated estimate (explicitly labeled).
      - `exceed_100_count` & `exceed_1000_count`: exact integer exceedance counters.
      - `bin_counts` & `bounds`: raw mergeable bins for multi-session aggregation.
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
            raise ValueError("Incompatible histogram bin_counts for merge")
        if isinstance(bounds, list) and tuple(float(x) for x in bounds) != self._bounds:
            raise ValueError("Incompatible histogram bounds for merge")
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
        """
        Conservative nearest-rank upper bound: returns `min(self._max, bin_upper_edge)`
        for the bin containing rank `max(1, ceil(q * count))`. Never underestimates.
        """
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

    def summary(self, decimals: int = 2, include_bins: bool = False) -> Dict[str, Any]:
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
        if include_bins:
            out["bounds"] = list(self._bounds)
            out["bin_counts"] = list(self._counts)
        return out


class CohortCounters:
    """Fixed-schema counters separating raw field presence from price/qty/1-lot validity (M3)."""

    __slots__ = (
        "updates",
        "ltpc_field_present",
        "ltpc_positive_updates",
        "book_field_present",
        "book_nonempty_updates",
        "book_valid_2sided_updates",
        "book_2sided_qty_updates",
        "book_usable_1lot_updates",
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
        self.reset()

    def reset(self) -> None:
        self.updates: int = 0
        self.ltpc_field_present: int = 0
        self.ltpc_positive_updates: int = 0
        self.book_field_present: int = 0
        self.book_nonempty_updates: int = 0
        self.book_valid_2sided_updates: int = 0
        self.book_2sided_qty_updates: int = 0
        self.book_usable_1lot_updates: int = 0
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
            "book_valid_2sided_updates": self.book_valid_2sided_updates,
            "book_2sided_qty_updates": self.book_2sided_qty_updates,
            "book_usable_1lot_updates": self.book_usable_1lot_updates,
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
                round(self.book_valid_2sided_updates / float(self.updates), 6)
                if self.updates > 0
                else 0.0
            ),
            "usable_1lot_book_ratio": (
                round(self.book_usable_1lot_updates / float(self.updates), 6)
                if self.updates > 0
                else 0.0
            ),
            "cohort_any_update_max_gap_ms": round(self.cohort_any_max_gap_ms, 2),
        }


class PerInstrumentStats:
    """Bounded per-S1-instrument receipt and inter-arrival gap tracker (M2)."""

    __slots__ = (
        "cohort",
        "updates",
        "valid_books",
        "usable_1lot_books",
        "first_mono_ns",
        "last_mono_ns",
        "last_book_mono_ns",
        "max_update_gap_ms",
        "max_book_gap_ms",
    )

    def __init__(self, cohort: str, mono_ns: int) -> None:
        self.cohort: str = cohort
        self.updates: int = 0
        self.valid_books: int = 0
        self.usable_1lot_books: int = 0
        self.first_mono_ns: int = mono_ns
        self.last_mono_ns: int = mono_ns
        self.last_book_mono_ns: int = 0
        self.max_update_gap_ms: float = 0.0
        self.max_book_gap_ms: float = 0.0

    def on_update(self, mono_ns: int, has_valid_book: bool, has_1lot_book: bool) -> None:
        if self.updates > 0 and mono_ns > self.last_mono_ns:
            gap_ms = (mono_ns - self.last_mono_ns) / 1_000_000.0
            if gap_ms > self.max_update_gap_ms:
                self.max_update_gap_ms = gap_ms
        self.updates += 1
        self.last_mono_ns = mono_ns
        if has_valid_book:
            if self.last_book_mono_ns > 0 and mono_ns > self.last_book_mono_ns:
                bgap_ms = (mono_ns - self.last_book_mono_ns) / 1_000_000.0
                if bgap_ms > self.max_book_gap_ms:
                    self.max_book_gap_ms = bgap_ms
            self.valid_books += 1
            self.last_book_mono_ns = mono_ns
        if has_1lot_book:
            self.usable_1lot_books += 1


class S1MeasurementSummaryExporter:
    """
    Asynchronous, hash-chained compact summary exporter (H5).
    Writes ONLY compact epoch/session summaries and control-audit records — never raw ticks.
    Strictly bounded by `MAX_EXPORT_STORAGE_BYTES` (64 MiB) and runs exclusively outside
    market-data callbacks.
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
        self._max_storage_bytes: int = max_storage_bytes
        self._lock = threading.Lock()
        self._export_seq: int = 0
        self._prev_record_sha256: str = "0" * 64
        self._last_durable_seq: int = 0
        self._last_durable_utc: Optional[str] = None
        self._last_durable_mono_ns: int = 0
        self._export_failures: int = 0
        self._last_export_error: Optional[str] = None
        self._storage_bytes_used: int = 0
        self._quota_exhausted: bool = False

    @property
    def configured_dir(self) -> Optional[Path]:
        return self._export_dir

    def _compute_dir_bytes_unlocked(self, target_dir: Path) -> int:
        total = 0
        if not target_dir.exists():
            return 0
        for entry in target_dir.iterdir():
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
        return total

    def write_record(self, record_kind: str, payload: Dict[str, Any], update_checkpoint: bool = False) -> bool:
        if self._export_dir is None:
            return False
        t_now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        ist_date = datetime.datetime.now(IST_TZ).date().isoformat()
        with self._lock:
            try:
                self._export_dir.mkdir(parents=True, exist_ok=True)
                used = self._compute_dir_bytes_unlocked(self._export_dir)
                self._storage_bytes_used = used

                next_seq = self._export_seq + 1
                envelope_body = {
                    "schema_version": SCHEMA_VERSION,
                    "record_kind": record_kind,
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

                if used + len(line_bytes) > self._max_storage_bytes:
                    self._quota_exhausted = True
                    self._export_failures += 1
                    self._last_export_error = "EXPORT_STORAGE_QUOTA_EXCEEDED"
                    return False

                ledger_path = self._export_dir / f"ledger_{ist_date}_{self._boot_id}.jsonl"
                fd = os.open(str(ledger_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    written = 0
                    mv = memoryview(line_bytes)
                    while written < len(line_bytes):
                        n = os.write(fd, mv[written:])
                        if n <= 0:
                            raise OSError("Partial zero-byte write on summary ledger")
                        written += n
                    os.fsync(fd)
                finally:
                    os.close(fd)

                if update_checkpoint:
                    ckpt_path = self._export_dir / f"last_checkpoint_{self._boot_id}.json"
                    tmp_path = self._export_dir / f".tmp_ckpt_{self._boot_id}_{next_seq}.json"
                    tmp_fd = os.open(
                        str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
                    )
                    try:
                        os.write(tmp_fd, line_bytes)
                        os.fsync(tmp_fd)
                    finally:
                        os.close(tmp_fd)
                    os.replace(str(tmp_path), str(ckpt_path))

                self._export_seq = next_seq
                self._prev_record_sha256 = rec_sha
                self._last_durable_seq = next_seq
                self._last_durable_utc = t_now_utc
                self._last_durable_mono_ns = time.perf_counter_ns()
                self._storage_bytes_used = used + len(line_bytes)
                self._last_export_error = None
                return True
            except Exception as exc:
                self._export_failures += 1
                self._last_export_error = f"{type(exc).__name__}:{str(exc)[:160]}"
                return False

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
                "last_durable_seq": self._last_durable_seq,
                "last_durable_utc": self._last_durable_utc,
                "export_lag_ms": lag_ms,
                "export_failures": self._export_failures,
                "quota_exhausted": self._quota_exhausted,
                "storage_bytes_used": self._storage_bytes_used,
                "max_storage_bytes": self._max_storage_bytes,
                "last_export_error": self._last_export_error,
            }

    @staticmethod
    def load_and_verify_ledger_dir(export_dir: Path) -> Dict[str, Any]:
        """
        Recovers persisted multi-session ledgers across restarts without truncating files.
        Verifies per-record SHA-256 and hash chains; flags torn crash tails explicitly.
        Merges raw latency histogram bins across sealed epochs.
        """
        merged_callback_added = BoundedHistogram(LATENCY_BOUNDS_US)
        verified_records: List[Dict[str, Any]] = []
        corrupt_or_torn_lines: List[Dict[str, Any]] = []
        sealed_epochs: List[str] = []

        if not export_dir.exists():
            return {
                "files_scanned": 0,
                "verified_record_count": 0,
                "sealed_epoch_ids": [],
                "corrupt_or_torn_tail_lines": [],
                "merged_full_probe_added_callback_us": merged_callback_added.summary(include_bins=True),
            }

        ledger_files = sorted(export_dir.glob("ledger_*.jsonl"))
        for lf in ledger_files:
            prev_sha = "0" * 64
            expected_seq = 1
            raw_lines = lf.read_bytes().splitlines()
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

                    if rec.get("record_kind") == "EPOCH_SEALED":
                        pl = rec.get("payload") or {}
                        ep_id = pl.get("epoch_id")
                        if ep_id:
                            sealed_epochs.append(str(ep_id))
                        lat_block = (pl.get("latencies_us") or {}).get("full_probe_added_callback_us")
                        if isinstance(lat_block, dict) and "bin_counts" in lat_block:
                            merged_callback_added.merge_from_dict(lat_block)
                except Exception as parse_err:
                    corrupt_or_torn_lines.append(
                        {"file": lf.name, "line": line_no, "reason": f"TORN_OR_INVALID_JSON:{type(parse_err).__name__}"}
                    )
                    break

        return {
            "files_scanned": len(ledger_files),
            "verified_record_count": len(verified_records),
            "sealed_epoch_ids": sealed_epochs,
            "corrupt_or_torn_tail_lines": corrupt_or_torn_lines,
            "merged_full_probe_added_callback_us": merged_callback_added.summary(include_bins=True),
        }


class _TokenBucketLimiter:
    """Bounded thread-safe token bucket for management API rate limiting."""

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
    Security & access-control guard for S1 probe summary/control endpoints (H1):
      - Direct loopback peer check (`127.0.0.1`, `::1`, `localhost`), rejecting forwarded headers.
      - Rejects ambient browser `Cookie` headers, cross-site `Sec-Fetch-Site`, and untrusted `Origin`.
      - Separate SHA-256 constant-time bearer tokens for `READ` vs `ADMIN` (write) roles;
        fails closed (`503`) if no token hash is configured.
      - Separate token-bucket rate limiters for summary reads vs state-changing controls.
    """

    def __init__(self) -> None:
        self._read_limiter = _TokenBucketLimiter(rate_per_sec=0.2, capacity=4.0)   # 12/min, burst 4
        self._write_limiter = _TokenBucketLimiter(rate_per_sec=0.1, capacity=2.0)  # 6/min, burst 2

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
    ) -> str:
        norm_headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

        # 1. Reject proxy-forwarded requests to prevent accidental reverse-proxy exposure
        for fwd_hdr in ("x-forwarded-for", "x-real-ip", "forwarded"):
            if fwd_hdr in norm_headers:
                raise ProbeControlError(403, "PROXY_HEADER_FORBIDDEN", "Forwarded proxy requests are forbidden")

        # 2. Enforce direct loopback peer
        peer = (client_host or "").strip().lower()
        if peer not in LOOPBACK_HOSTS:
            raise ProbeControlError(403, "NON_LOOPBACK_PEER", "Management endpoints restricted to loopback peer")

        # 3. CSRF / Browser ambient-credential defense
        if "cookie" in norm_headers:
            raise ProbeControlError(403, "COOKIE_AUTH_FORBIDDEN", "Ambient Cookie headers are rejected")
        sec_site = norm_headers.get("sec-fetch-site", "").strip().lower()
        if sec_site and sec_site not in ("none", "same-origin"):
            raise ProbeControlError(403, "CROSS_SITE_FORBIDDEN", "Cross-site browser requests are forbidden")
        origin = norm_headers.get("origin", "").strip()
        if origin and origin not in DEFAULT_ALLOWED_LOCAL_ORIGINS:
            raise ProbeControlError(403, "UNTRUSTED_ORIGIN", "Origin not allowlisted for management access")

        # 4. Configured SHA-256 token verification (fail-closed when unconfigured)
        admin_hash = os.environ.get("S1_PROBE_ADMIN_TOKEN_SHA256", "").strip().lower()
        read_hash = os.environ.get("S1_PROBE_READ_TOKEN_SHA256", "").strip().lower()
        if len(admin_hash) != 64 and (require_write or len(read_hash) != 64):
            raise ProbeControlError(
                503,
                "PROBE_AUTH_UNCONFIGURED",
                "S1 probe management token SHA-256 is not configured; access denied (fail-closed)",
            )

        raw_token = self._extract_bearer_token(norm_headers)
        if not raw_token:
            raise ProbeControlError(401, "MISSING_BEARER_TOKEN", "Missing or malformed management bearer token")

        presented_sha = hashlib.sha256(raw_token.encode("utf-8")).hexdigest().lower()
        is_admin = len(admin_hash) == 64 and hmac.compare_digest(presented_sha, admin_hash)
        is_reader = len(read_hash) == 64 and hmac.compare_digest(presented_sha, read_hash)

        if require_write:
            if not is_admin:
                if is_reader:
                    raise ProbeControlError(403, "INSUFFICIENT_ROLE", "Write control requires ADMIN token")
                raise ProbeControlError(403, "INVALID_TOKEN", "Invalid management bearer token")
            if not self._write_limiter.allow():
                raise ProbeControlError(429, "RATE_LIMITED_CONTROL", "Probe control rate limit exceeded")
            return "ADMIN"

        if not (is_admin or is_reader):
            raise ProbeControlError(403, "INVALID_TOKEN", "Invalid management bearer token")
        if not self._read_limiter.allow():
            raise ProbeControlError(429, "RATE_LIMITED_READ", "Probe summary read rate limit exceeded")
        return "ADMIN" if is_admin else "READ"


class S1IngressMeasurementProbe:
    """
    Phase 1 (V2) S1 Ingress Measurement Probe.
    - Receive callbacks never acquire locks; they append bounded tuples to `_inbox`.
    - Single background worker (`_worker_loop`) owns all mutable aggregation structures,
      refreshes universe metadata off-callback, publishes immutable `_published_snapshot`
      dicts for lock-free HTTP reads, and exports hash-chained summaries asynchronously.
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
            default_root = Path(__file__).resolve().parent.parent.parent / "data" / "s1_probe_summaries"
            export_dir = default_root
        self._exporter = S1MeasurementSummaryExporter(
            export_dir=export_dir, boot_id=self._boot_id, process_id=self._process_id
        )

        # Control-plane mutex (NEVER acquired by live market-data or lifecycle callbacks!)
        self._control_lock = threading.Lock()
        self._universe_supplier: Optional[Callable[[], Tuple[Set[str], Dict[str, Dict[str, Any]]]]] = None
        self._attachment_verifier: Optional[Callable[[], Dict[str, Any]]] = None
        self._registered_streamers: List[weakref.ReferenceType[Any]] = []
        self._control_audit_ring: Deque[Dict[str, Any]] = collections.deque(maxlen=MAX_CONTROL_AUDIT_RING)

        # Lock-free hot-path flags & single-writer byte counters
        self._capture_enabled: bool = False  # Strictly OFF at construction
        self._active_epoch_seq: int = 0
        self._inbox: Deque[Tuple[Any, ...]] = collections.deque()
        self._wake_event = threading.Event()
        self._enqueued_wire_bytes_total: int = 0
        self._dequeued_wire_bytes_total: int = 0
        self._inbox_overflow_drops: int = 0
        self._inbox_overflow_bytes: int = 0
        self._post_disable_inflight_drops: int = 0
        self._observer_boundary_faults: int = 0
        self._consecutive_callback_faults: int = 0
        self._consecutive_latency_breaches: int = 0
        self._severe_latency_stalls: int = 0
        self._last_callback_stage_hook_ns: int = 0
        self._auto_disabled_on_fault: bool = False
        self._auto_disabled_reason: Optional[str] = None

        # Worker thread (started lazily ONLY when enabled)
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_stop: bool = False

        # Aggregator-owned state (mutated ONLY on worker thread, or before worker starts)
        self._init_aggregator_state(epoch_seq=0)
        self._published_snapshot: Dict[str, Any] = self._build_snapshot_from_aggregator(include_rings=True)
        self._published_compact: Dict[str, Any] = self._build_compact_from_snapshot(self._published_snapshot)

    # ──────────────────────────────────────────────────────────────────────────
    # AGGREGATOR-OWNED STATE INITIALIZATION (SINGLE-OWNER: WORKER THREAD)
    # ──────────────────────────────────────────────────────────────────────────

    def _init_aggregator_state(self, epoch_seq: int) -> None:
        now_ms = int(time.time() * 1000)
        now_mono = time.perf_counter_ns()
        self._agg_epoch_seq: int = epoch_seq
        self._agg_epoch_id: str = f"{self._boot_id}_ep{epoch_seq:04d}"
        self._agg_created_ms: int = now_ms
        self._agg_enabled_since_utc: Optional[str] = None
        self._agg_disabled_at_utc: Optional[str] = None
        self._agg_active_start_mono_ns: int = 0
        self._agg_accumulated_enabled_ns: int = 0
        self._agg_control_intervals: List[Dict[str, Any]] = []

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
        self._agg_epoch_sealed_and_exported: bool = True
        self._agg_last_checkpoint_ns: int = now_mono
        self._agg_last_snapshot_pub_ns: int = 0

        # Pre-decode ingress & error accounting (M3)
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

        # Universe & bounded historical classification state (M1 / M2)
        self._agg_last_universe_refresh_ns: int = 0
        self._agg_universe_refresh_interval_ns: int = 5_000_000_000
        self._agg_consecutive_worker_faults: int = 0
        self._agg_consecutive_supplier_faults: int = 0
        self._agg_worker_internal_errors: int = 0
        self._agg_universe_epoch_seq: int = 0
        self._agg_current_subscribed_keys: Set[str] = set()
        self._agg_seen_in_current_u_epoch: Set[str] = set()
        self._agg_universe_epochs: Deque[Dict[str, Any]] = collections.deque(maxlen=MAX_UNIVERSE_EPOCHS_RING)
        self._agg_current_contract_meta: Dict[str, Tuple[str, int]] = {}  # ikey -> (cohort, lot_size)
        self._agg_historical_identity: collections.OrderedDict[str, Tuple[str, int]] = collections.OrderedDict()
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
        self._agg_hist_bar_flush_15m_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_freeze_snapshot_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_pending_stage_hook_ns: int = 0

        # Rolling buckets + explicit zero-interval tracking (M4 / H5)
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
    # ATTACHMENT & UNIVERSE HOOKS (M5)
    # ──────────────────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._capture_enabled

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
    # MANAGEMENT PLANE CONTROLS (NONBLOCKING DISABLE, FAIL-CLOSED ENABLE/RESET)
    # ──────────────────────────────────────────────────────────────────────────

    def _ensure_worker_started_unlocked(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._worker_stop = False
        t = threading.Thread(
            target=self._worker_loop,
            name=f"S1ProbeAggregator-{self._probe_id}",
            daemon=True,
        )
        self._worker_thread = t
        t.start()

    def record_control_audit(
        self,
        *,
        action: str,
        actor_role: str,
        peer: str,
        outcome: str,
        detail: Optional[str] = None,
    ) -> None:
        entry = {
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
            if require_streamer_attached_default_check(require_attached_streamer)
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
            self._ensure_worker_started_unlocked()
            if not self._capture_enabled:
                if self._active_epoch_seq == 0:
                    self._active_epoch_seq = 1
                self._consecutive_callback_faults = 0
                self._consecutive_latency_breaches = 0
                self._severe_latency_stalls = 0
                self._auto_disabled_on_fault = False
                self._auto_disabled_reason = None
                done_evt = threading.Event()
                self._inbox.append(
                    (
                        _EVT_LIFECYCLE,
                        self._active_epoch_seq,
                        "probe_enabled",
                        datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        time.perf_counter_ns(),
                        done_evt,
                    )
                )
                self._capture_enabled = True
                self._wake_event.set()
                done_evt.wait(timeout=0.25)

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
        # CRITICAL H2: flip `_capture_enabled` IMMEDIATELY without acquiring ANY lock!
        self._capture_enabled = False
        seal_done = threading.Event()
        seal_mono_ns = time.perf_counter_ns()
        seal_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            self._inbox.append(
                (_EVT_SEAL_EPOCH, self._active_epoch_seq, reason, seal_utc, seal_mono_ns, False, seal_done)
            )
            self._wake_event.set()
        except Exception:
            pass

        if self._worker_thread is not None and self._worker_thread.is_alive() and flush_timeout_s > 0:
            seal_done.wait(timeout=flush_timeout_s)

        self.record_control_audit(
            action="disable", actor_role=actor_role, peer=peer, outcome="DISABLED", detail=reason
        )
        return {
            "enabled": self._capture_enabled,
            "epoch_seq": self._active_epoch_seq,
            "sealed": seal_done.is_set(),
            "disabled_utc": seal_utc,
        }

    def reset(
        self,
        *,
        flush_timeout_s: float = 1.0,
        actor_role: str = "INTERNAL",
        peer: str = "local",
    ) -> Dict[str, Any]:
        # H1: Permit reset ONLY while disabled, and ONLY after sealing/exporting previous epoch!
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
            next_epoch = self._active_epoch_seq + 1
            self._active_epoch_seq = next_epoch
            seal_done = threading.Event()
            self._inbox.append(
                (
                    _EVT_SEAL_EPOCH,
                    next_epoch,
                    "RESET_TO_NEW_EPOCH",
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    time.perf_counter_ns(),
                    True,
                    seal_done,
                )
            )
            self._wake_event.set()
            seal_done.wait(timeout=flush_timeout_s)

        self.record_control_audit(
            action="reset", actor_role=actor_role, peer=peer, outcome="EPOCH_SEALED_AND_RESET"
        )
        return {
            "enabled": self._capture_enabled,
            "new_epoch_seq": self._active_epoch_seq,
            "previous_epoch_sealed": seal_done.is_set(),
        }

    def flush_for_test_or_export(self, timeout_s: float = 1.0, force_export: bool = False) -> bool:
        """Drains queued samples on the worker thread and refreshes the immutable snapshot."""
        with self._control_lock:
            self._ensure_worker_started_unlocked()
            done_evt = threading.Event()
            self._inbox.append(
                (
                    _EVT_LIFECYCLE,
                    self._active_epoch_seq,
                    "__sync_barrier_export__" if force_export else "__sync_barrier__",
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    time.perf_counter_ns(),
                    done_evt,
                )
            )
            self._wake_event.set()
        return done_evt.wait(timeout=timeout_s)

    def shutdown_worker(self, timeout_s: float = 1.0) -> None:
        self._capture_enabled = False
        self._worker_stop = True
        self._wake_event.set()
        wt = self._worker_thread
        if wt is not None and wt.is_alive():
            wt.join(timeout=timeout_s)

    # ──────────────────────────────────────────────────────────────────────────
    # LOCK-FREE LIVE CALLBACK ENTRYPOINTS (ZERO MUTEXES, ZERO I/O, FAIL-OPEN)
    # ──────────────────────────────────────────────────────────────────────────

    def note_observer_fault(self, boundary: str, exc: Optional[BaseException] = None) -> None:
        """Last-resort health latch called by outer callback guards (H4)."""
        try:
            self._observer_boundary_faults += 1
            self._consecutive_callback_faults += 1
            if self._consecutive_callback_faults >= MAX_CONSECUTIVE_PROBE_FAULTS:
                self._capture_enabled = False
                self._auto_disabled_on_fault = True
                self._auto_disabled_reason = f"OBSERVER_FAULT:{boundary}"
                self._wake_event.set()
        except Exception:
            self._capture_enabled = False

    def _check_latency_circuit_breaker(self, full_added_us: float) -> None:
        if full_added_us > LATENCY_SINGLE_BREACH_US:
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
                self._wake_event.set()
        else:
            self._consecutive_latency_breaches = 0

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
            ep = self._active_epoch_seq
            in_flight_bytes = self._enqueued_wire_bytes_total - self._dequeued_wire_bytes_total
            b_len = raw_bytes if (is_bytes and raw_bytes > 0) else 0
            if len(self._inbox) >= MAX_INBOX_SAMPLES or (in_flight_bytes + b_len) > MAX_INBOX_WIRE_BYTES:
                self._inbox_overflow_drops += 1
                self._inbox_overflow_bytes += b_len
                return
            self._enqueued_wire_bytes_total += b_len
            self._inbox.append(
                (_EVT_FRAME_ERR, ep, stage, b_len, is_bytes, wire_type_int, data_dict, recv_wall_ns, t0_ns)
            )
            self._wake_event.set()
        except Exception as exc:
            self.note_observer_fault("record_ingress_error_attempt", exc)

    def record_ingress_frame(
        self,
        *,
        raw_bytes: int,
        is_bytes: bool,
        wire_type_int: int = -1,
        data_dict: Any,
        recv_wall_ns: int = 0,
        recv_wall_ms: int = 0,
        t0_ns: int,
        t1_ns: int,
        t2_ns: int,
        t3_ns: int,
        pre_overhead_ns: int = 0,
    ) -> None:
        if not self._capture_enabled:
            self._post_disable_inflight_drops += 1
            return
        t_handoff_start_ns = time.perf_counter_ns()
        try:
            ep = self._active_epoch_seq
            wall_ns = recv_wall_ns if recv_wall_ns > 0 else (recv_wall_ms * 1_000_000)
            b_len = raw_bytes if (is_bytes and raw_bytes > 0) else 0
            in_flight_bytes = self._enqueued_wire_bytes_total - self._dequeued_wire_bytes_total

            if len(self._inbox) >= MAX_INBOX_SAMPLES or (in_flight_bytes + b_len) > MAX_INBOX_WIRE_BYTES:
                self._inbox_overflow_drops += 1
                self._inbox_overflow_bytes += b_len
                self._wake_event.set()
                return

            if not self._capture_enabled:
                self._post_disable_inflight_drops += 1
                return

            t_handoff_end_ns = time.perf_counter_ns()
            handoff_ns = max(0, t_handoff_end_ns - t_handoff_start_ns)
            self._enqueued_wire_bytes_total += b_len
            self._inbox.append(
                (
                    _EVT_FRAME_OK,
                    ep,
                    b_len,
                    is_bytes,
                    wire_type_int,
                    data_dict,
                    wall_ns,
                    t0_ns,
                    t1_ns,
                    t2_ns,
                    t3_ns,
                    pre_overhead_ns,
                    handoff_ns,
                )
            )
            self._consecutive_callback_faults = 0
            cb_hook_ns = self._last_callback_stage_hook_ns
            self._last_callback_stage_hook_ns = 0
            self._check_latency_circuit_breaker((pre_overhead_ns + handoff_ns + cb_hook_ns) / 1000.0)
            if len(self._inbox) == 1:
                self._wake_event.set()
        except Exception as exc:
            self.note_observer_fault("record_ingress_frame", exc)

    def record_downstream_segments(self, listener_ns: int, equity_loop_ns: int, hook_overhead_ns: int = 0) -> None:
        if not self._capture_enabled:
            return
        t_seg0 = time.perf_counter_ns()
        try:
            if len(self._inbox) < MAX_INBOX_SAMPLES:
                t_seg1 = time.perf_counter_ns()
                tot_hk_ns = max(0, hook_overhead_ns) + max(0, t_seg1 - t_seg0)
                self._last_callback_stage_hook_ns = tot_hk_ns
                self._inbox.append(
                    (_EVT_DOWNSTREAM_SEG, self._active_epoch_seq, listener_ns, equity_loop_ns, tot_hk_ns)
                )
            else:
                self._last_callback_stage_hook_ns = max(0, hook_overhead_ns) + max(0, time.perf_counter_ns() - t_seg0)
        except Exception as exc:
            self.note_observer_fault("record_downstream_segments", exc)

    def record_periodic_decision_stage(self, stage: str, duration_ns: int) -> None:
        if not self._capture_enabled or duration_ns < 0:
            return
        try:
            if len(self._inbox) < MAX_INBOX_SAMPLES:
                self._inbox.append((_EVT_PERIODIC_STAGE, self._active_epoch_seq, stage, duration_ns))
                self._wake_event.set()
        except Exception as exc:
            self.note_observer_fault("record_periodic_decision_stage", exc)

    def record_ws_lifecycle(self, event: str, detail: Any = None) -> None:
        if not self._capture_enabled:
            return
        try:
            if len(self._inbox) < MAX_INBOX_SAMPLES:
                self._inbox.append(
                    (_EVT_LIFECYCLE, self._active_epoch_seq, str(event)[:40], detail, time.perf_counter_ns(), None)
                )
                self._wake_event.set()
        except Exception as exc:
            self.note_observer_fault("record_ws_lifecycle", exc)

    # ──────────────────────────────────────────────────────────────────────────
    # READ-ONLY SNAPSHOT ACCESSORS (ZERO CALLBACK CONTENTION — H2)
    # ──────────────────────────────────────────────────────────────────────────

    def get_compact_status(self) -> Dict[str, Any]:
        snap = self._published_compact
        out = dict(snap)
        out["enabled"] = self._capture_enabled
        out["auto_disabled_on_fault"] = self._auto_disabled_on_fault
        out["auto_disabled_reason"] = self._auto_disabled_reason
        out["inbox_overflow_drops"] = self._inbox_overflow_drops
        return out

    def get_summary(self, include_recent_rings: bool = False) -> Dict[str, Any]:
        snap = self._published_snapshot
        if include_recent_rings:
            out = dict(snap)
        else:
            out = {
                k: v
                for k, v in snap.items()
                if k not in ("recent_1m_rollups", "recent_1s_rates", "recent_100ms_bursts")
            }
        out["enabled"] = self._capture_enabled
        out["auto_disabled_on_fault"] = self._auto_disabled_on_fault
        out["auto_disabled_reason"] = self._auto_disabled_reason
        out["recent_control_audit"] = list(self._control_audit_ring)[-20:]
        return out

    # ──────────────────────────────────────────────────────────────────────────
    # BACKGROUND AGGREGATOR & EXPORTER WORKER (OFF-CALLBACK EXECUTION)
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _classify_dte_days_ist(
        contract_meta: Dict[str, Any], ist_today: datetime.date
    ) -> Tuple[Optional[int], bool]:
        """
        M2 fix: Expiry date in IST (`Asia/Kolkata`) is strictly authoritative.
        Cached `dte` is used ONLY as a flagged fallback when `expiry` is absent.
        """
        exp_raw = str(contract_meta.get("expiry") or "").strip()
        if len(exp_raw) >= 10:
            try:
                exp_date = datetime.date.fromisoformat(exp_raw[:10])
                return (exp_date - ist_today).days, False
            except ValueError:
                pass
        raw_dte = contract_meta.get("dte")
        if raw_dte is not None:
            try:
                return int(math.ceil(float(raw_dte))), True
            except (ValueError, TypeError):
                pass
        return None, False

    def _worker_refresh_universe(self, force: bool = False) -> None:
        supplier = self._universe_supplier
        if supplier is None:
            return
        now_ns = time.perf_counter_ns()
        if not force and (now_ns - self._agg_last_universe_refresh_ns) < self._agg_universe_refresh_interval_ns:
            return
        self._agg_last_universe_refresh_ns = now_ns
        try:
            raw_sub, raw_contracts = supplier()
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

            new_meta: Dict[str, Tuple[str, int]] = {}
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
                try:
                    lot_sz = max(1, int(meta.get("lot_size") or (65 if underlying == "NIFTY" else 1)))
                except (ValueError, TypeError):
                    lot_sz = 65 if underlying == "NIFTY" else 1

                if underlying != "NIFTY":
                    cls_tag = ("NON_S1_UNIVERSE", lot_sz)
                elif meta.get("is_futures"):
                    cls_tag = ("NIFTY_FUTURES", lot_sz)
                else:
                    dte_days, used_fallback = self._classify_dte_days_ist(meta, ist_today)
                    if used_fallback:
                        self._agg_dte_fallback_count += 1
                    if dte_days is not None and 1 <= dte_days <= 7:
                        cls_tag = ("NIFTY_OPTIONS_1_7_DTE", lot_sz)
                    else:
                        cls_tag = ("NIFTY_OPTIONS_OTHER_DTE", lot_sz)

                new_meta[ikey] = cls_tag
                self._agg_historical_identity[ikey] = cls_tag
                self._agg_historical_identity.move_to_end(ikey)
                while len(self._agg_historical_identity) > MAX_HISTORICAL_IDENTITY_KEYS:
                    self._agg_historical_identity.popitem(last=False)

            if bounded_sub != self._agg_current_subscribed_keys:
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
                self._agg_seen_in_current_u_epoch = set()

            self._agg_current_contract_meta = new_meta
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
        # 100ms bucket + confirmed zero bins
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

        # 1s bucket + confirmed zero 1s bins included in session 1s rate histogram (M4)
        if self._agg_cur_1s_slot != slot_1s:
            if self._agg_cur_1s_slot >= 0:
                f1 = self._agg_cur_1s_frames
                b1 = self._agg_cur_1s_bytes
                i1 = self._agg_cur_1s_insts
                su1 = self._agg_cur_1s_s1_updates
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
                        "s1_updates": su1,
                        "s1_option_books": ob1,
                        "partial_finalized": False,
                    }
                )
                if slot_1s > self._agg_cur_1s_slot + 1:
                    gap_1s = slot_1s - self._agg_cur_1s_slot - 1
                    self._agg_confirmed_zero_1s_bins += gap_1s
                    for _ in range(min(gap_1s, 3600)):
                        self._agg_hist_1s_all_frames.observe(0.0)
                        self._agg_hist_1s_all_bytes.observe(0.0)
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

        # 1m session rollup bucket
        if self._agg_cur_1m_slot != slot_1m:
            if self._agg_cur_1m_slot >= 0 and self._agg_cur_1m_frames > 0:
                if len(self._agg_ring_1m) == RING_1M_MAXLEN:
                    self._agg_ring_1m_evictions += 1
                self._agg_ring_1m.append(
                    {
                        "minute_epoch_ms": self._agg_cur_1m_slot * 60_000,
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
        """Flushes unfinished active 100ms/1s/1m accumulators on disable/seal (H5)."""
        if self._agg_cur_100ms_slot >= 0 and self._agg_cur_100ms_frames > 0:
            self._agg_hist_100ms_nonempty_frames.observe(float(self._agg_cur_100ms_frames))
            self._agg_hist_100ms_nonempty_bytes.observe(float(self._agg_cur_100ms_bytes))
            if len(self._agg_ring_100ms) == RING_100MS_MAXLEN:
                self._agg_ring_100ms_evictions += 1
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
            if len(self._agg_ring_1s) == RING_1S_MAXLEN:
                self._agg_ring_1s_evictions += 1
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
            if len(self._agg_ring_1m) == RING_1M_MAXLEN:
                self._agg_ring_1m_evictions += 1
            self._agg_ring_1m.append(
                {
                    "minute_epoch_ms": self._agg_cur_1m_slot * 60_000,
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

    def _classify_instrument_key(self, ikey: str) -> Tuple[str, int, bool]:
        """Returns `(cohort_name, lot_size, is_nifty_option)` using current + historical epochs."""
        core_c = S1_CORE_INSTRUMENT_KEYS.get(ikey)
        if core_c is not None:
            return core_c, 1, False
        cur_m = self._agg_current_contract_meta.get(ikey)
        if cur_m is not None:
            c_name, lot = cur_m
            return c_name, lot, c_name in ("NIFTY_OPTIONS_1_7_DTE", "NIFTY_OPTIONS_OTHER_DTE")
        hist_m = self._agg_historical_identity.get(ikey)
        if hist_m is not None:
            c_name, lot = hist_m
            return c_name, lot, c_name in ("NIFTY_OPTIONS_1_7_DTE", "NIFTY_OPTIONS_OTHER_DTE")
        if ikey.startswith(("NSE_FO|", "BSE_FO|", "NFO|")):
            return "UNKNOWN_FO_UNCLASSIFIED", 65, False
        return "NON_S1_UNIVERSE", 1, False

    def _worker_process_ok_frame(self, item: Tuple[Any, ...]) -> None:
        (
            _,
            _ep,
            raw_bytes,
            is_bytes,
            wire_type_int,
            data_dict,
            wall_ns,
            t0_ns,
            t1_ns,
            t2_ns,
            t3_ns,
            pre_ns,
            handoff_ns,
        ) = item

        self._worker_refresh_universe(force=False)
        wall_ms = wall_ns // 1_000_000

        # Clock step check (wall vs monotonic delta — M4)
        if self._agg_prev_mono_ns > 0 and t0_ns >= self._agg_prev_mono_ns:
            mono_delta_ms = (t0_ns - self._agg_prev_mono_ns) / 1_000_000.0
            wall_delta_ms = (wall_ns - self._agg_prev_wall_ns) / 1_000_000.0
            if abs(wall_delta_ms - mono_delta_ms) > CLOCK_STEP_TOLERANCE_MS:
                self._agg_clock_step_events += 1
                self._agg_clock_certification_valid = False
                self._agg_invalidation_reasons.add("WALL_CLOCK_STEP_DETECTED")
        self._agg_prev_mono_ns = t0_ns
        self._agg_prev_wall_ns = wall_ns

        # Transactional per-instrument scan first so no partial cohort mutation occurs on error (H4)
        parsed_rows: List[Tuple[str, str, int, bool, Dict[str, int]]] = []
        env_enum_label = "unknown"
        dict_omitted_enum0 = False
        cur_ts_lag_ms: Optional[float] = None
        has_cur_ts = False

        if wire_type_int == 0:
            env_enum_label = "initial_feed"
        elif wire_type_int == 1:
            env_enum_label = "live_feed"
        elif wire_type_int == 2:
            env_enum_label = "market_info"

        if isinstance(data_dict, dict):
            d_type = data_dict.get("type")
            if d_type is None and wire_type_int == 0:
                dict_omitted_enum0 = True
            elif isinstance(d_type, str) and env_enum_label == "unknown":
                if d_type in ("initial_feed", "live_feed", "market_info"):
                    env_enum_label = d_type

            raw_cur_ts = data_dict.get("currentTs")
            if raw_cur_ts is not None:
                try:
                    cur_ts_ms = int(raw_cur_ts)
                    has_cur_ts = True
                    cur_ts_lag_ms = (wall_ns / 1_000_000.0) - float(cur_ts_ms)
                except (ValueError, TypeError):
                    pass

            feeds = data_dict.get("feeds")
        else:
            feeds = None

        n_insts = len(feeds) if isinstance(feeds, dict) else 0
        oversized_in_frame = 0

        if n_insts > 0 and isinstance(feeds, dict):
            for ikey_raw, feed_obj in feeds.items():
                ikey = str(ikey_raw)
                if len(ikey) > MAX_KEY_CHARS:
                    oversized_in_frame += 1
                    continue

                cohort_name, lot_size, is_nifty_opt = self._classify_instrument_key(ikey)
                flags = {
                    "ltpc_present": 0,
                    "ltpc_pos": 0,
                    "book_present": 0,
                    "book_nonempty": 0,
                    "book_valid": 0,
                    "book_2sided_q": 0,
                    "book_1lot": 0,
                    "book_l5": 0,
                    "book_only": 0,
                    "book_bad": 0,
                    "oi_present": 0,
                    "oi_pos": 0,
                    "greeks": 0,
                    "i1": 0,
                    "cand_ts": 0,
                    "arm": 0,  # 1=full_mkt, 2=full_idx, 3=flwg, 4=top_ltpc, 0=other
                }

                if isinstance(feed_obj, dict):
                    full_feed = feed_obj.get("fullFeed")
                    flwg = feed_obj.get("firstLevelWithGreeks")
                    top_ltpc = feed_obj.get("ltpc")

                    ltpc_dict = None
                    quotes_list = None
                    first_depth = None
                    raw_oi_present = False
                    raw_oi_val = None
                    greeks_dict = None
                    ohlc_container = None

                    if isinstance(full_feed, dict):
                        m_ff = full_feed.get("marketFF")
                        i_ff = full_feed.get("indexFF")
                        if isinstance(m_ff, dict):
                            flags["arm"] = 1
                            ltpc_dict = m_ff.get("ltpc")
                            if "marketLevel" in m_ff and isinstance(m_ff["marketLevel"], dict):
                                ml = m_ff["marketLevel"]
                                if len(ml) > 1:
                                    for tk in UNVERIFIED_QUOTE_TS_CANDIDATE_KEYS:
                                        if tk in ml:
                                            flags["cand_ts"] = 1
                                            break
                                if "bidAskQuote" in ml:
                                    flags["book_present"] = 1
                                    quotes_list = ml.get("bidAskQuote")
                            if "oi" in m_ff and m_ff.get("oi") is not None:
                                raw_oi_present = True
                                raw_oi_val = m_ff.get("oi")
                            greeks_dict = m_ff.get("optionGreeks")
                            ohlc_container = m_ff.get("marketOHLC")
                        elif isinstance(i_ff, dict):
                            flags["arm"] = 2
                            ltpc_dict = i_ff.get("ltpc")
                            ohlc_container = i_ff.get("marketOHLC")
                    elif isinstance(flwg, dict):
                        # M3 fix: full support for Upstox V3 `firstLevelWithGreeks` arm
                        flags["arm"] = 3
                        ltpc_dict = flwg.get("ltpc")
                        if "firstDepth" in flwg and isinstance(flwg["firstDepth"], dict):
                            flags["book_present"] = 1
                            first_depth = flwg["firstDepth"]
                        if "oi" in flwg and flwg.get("oi") is not None:
                            raw_oi_present = True
                            raw_oi_val = flwg.get("oi")
                        greeks_dict = flwg.get("optionGreeks")
                    elif isinstance(top_ltpc, dict):
                        flags["arm"] = 4
                        ltpc_dict = top_ltpc

                    if isinstance(ltpc_dict, dict) and "ltp" in ltpc_dict and ltpc_dict.get("ltp") is not None:
                        flags["ltpc_present"] = 1
                        try:
                            if float(ltpc_dict["ltp"]) > 0.0:
                                flags["ltpc_pos"] = 1
                        except (ValueError, TypeError):
                            pass

                    top_quote_dict = None
                    quote_levels_count = 0
                    if isinstance(quotes_list, list):
                        if len(quotes_list) > 0 and isinstance(quotes_list[0], dict):
                            flags["book_nonempty"] = 1
                            top_quote_dict = quotes_list[0]
                            quote_levels_count = len(quotes_list)
                        else:
                            flags["book_bad"] = 1
                    elif isinstance(first_depth, dict):
                        if len(first_depth) > 0:
                            flags["book_nonempty"] = 1
                            top_quote_dict = first_depth
                            quote_levels_count = 1
                        else:
                            flags["book_bad"] = 1

                    if top_quote_dict is not None:
                        if flags["ltpc_pos"] == 0:
                            flags["book_only"] = 1
                        for tk in UNVERIFIED_QUOTE_TS_CANDIDATE_KEYS:
                            if tk in top_quote_dict:
                                flags["cand_ts"] = 1
                                break
                        try:
                            bp = float(top_quote_dict.get("bidP") or top_quote_dict.get("bid_price") or 0.0)
                            ap = float(top_quote_dict.get("askP") or top_quote_dict.get("ask_price") or 0.0)
                            bq = int(top_quote_dict.get("bidQ") or top_quote_dict.get("bid_qty") or 0)
                            aq = int(top_quote_dict.get("askQ") or top_quote_dict.get("ask_qty") or 0)
                        except (ValueError, TypeError):
                            bp, ap, bq, aq = 0.0, 0.0, 0, 0

                        if bp > 0.0 and ap >= bp:
                            flags["book_valid"] = 1
                            if bq > 0 and aq > 0:
                                flags["book_2sided_q"] = 1
                            if bq >= lot_size and aq >= lot_size:
                                flags["book_1lot"] = 1
                            if quote_levels_count >= 5:
                                flags["book_l5"] = 1
                        else:
                            flags["book_bad"] = 1

                    if raw_oi_present:
                        flags["oi_present"] = 1
                        try:
                            if float(raw_oi_val) > 0.0:
                                flags["oi_pos"] = 1
                        except (ValueError, TypeError):
                            pass

                    if isinstance(greeks_dict, dict) and len(greeks_dict) > 0:
                        flags["greeks"] = 1

                    if isinstance(ohlc_container, dict):
                        olist = ohlc_container.get("ohlc")
                        if isinstance(olist, list):
                            for ci in olist:
                                if isinstance(ci, dict) and ci.get("interval") == "I1":
                                    flags["i1"] = 1
                                    break

                parsed_rows.append((ikey, cohort_name, lot_size, is_nifty_opt, flags))

        # Commit frame atomically into aggregator state
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

        self._agg_wire_enum_type_counts[env_enum_label] = (
            self._agg_wire_enum_type_counts.get(env_enum_label, 0) + 1
        )
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
            self._agg_invalidation_reasons.add("OVERSIZED_FEED_KEY")

        self._agg_total_instrument_updates += len(parsed_rows)
        self._agg_hist_inst_per_frame.observe(float(len(parsed_rows)))

        frame_s1_updates = 0
        frame_opt_books = 0

        for ikey, cohort_name, _lot, is_nifty_opt, fl in parsed_rows:
            if ikey not in self._agg_seen_all_keys:
                if len(self._agg_seen_all_keys) < MAX_TRACKED_SEEN_KEYS:
                    self._agg_seen_all_keys.add(ikey)
                else:
                    self._agg_seen_key_cap_drops += 1
                    self._agg_coverage_valid = False
                    self._agg_invalidation_reasons.add("SEEN_ALL_KEYS_CAP_EXCEEDED")

            if self._agg_current_subscribed_keys:
                if ikey in self._agg_current_subscribed_keys:
                    if len(self._agg_seen_in_current_u_epoch) < MAX_UNIVERSE_KEYS_SNAPSHOT:
                        self._agg_seen_in_current_u_epoch.add(ikey)
                else:
                    self._agg_delivered_outside_subscribed_count += 1

            if cohort_name in S1_CORE_INSTRUMENT_KEYS.values():
                self._agg_seen_s1_core_keys.add(ikey)
            elif cohort_name == "NIFTY_OPTIONS_1_7_DTE":
                if ikey not in self._agg_seen_opt_1_7_keys:
                    if len(self._agg_seen_opt_1_7_keys) < MAX_TRACKED_OPTION_COHORT_KEYS:
                        self._agg_seen_opt_1_7_keys.add(ikey)
                    else:
                        self._agg_opt_cohort_key_cap_drops += 1
                        self._agg_coverage_valid = False
                        self._agg_invalidation_reasons.add("OPT_1_7_KEY_CAP_EXCEEDED")
            elif cohort_name == "NIFTY_OPTIONS_OTHER_DTE":
                if ikey not in self._agg_seen_opt_other_keys:
                    if len(self._agg_seen_opt_other_keys) < MAX_TRACKED_OPTION_COHORT_KEYS:
                        self._agg_seen_opt_other_keys.add(ikey)
                    else:
                        self._agg_opt_cohort_key_cap_drops += 1
                        self._agg_coverage_valid = False
                        self._agg_invalidation_reasons.add("OPT_OTHER_KEY_CAP_EXCEEDED")
            elif cohort_name == "UNKNOWN_FO_UNCLASSIFIED":
                self._agg_coverage_valid = False
                self._agg_invalidation_reasons.add("UNKNOWN_FO_CONTRACT_OBSERVED")

            c_obj = self._agg_cohorts[cohort_name]
            if c_obj.updates > 0 and t0_ns > c_obj.last_seen_mono_ns:
                cgap = (t0_ns - c_obj.last_seen_mono_ns) / 1_000_000.0
                if cgap > c_obj.cohort_any_max_gap_ms:
                    c_obj.cohort_any_max_gap_ms = cgap
            else:
                c_obj.first_seen_mono_ns = t0_ns
            c_obj.last_seen_mono_ns = t0_ns
            c_obj.updates += 1

            c_obj.ltpc_field_present += fl["ltpc_present"]
            c_obj.ltpc_positive_updates += fl["ltpc_pos"]
            c_obj.book_field_present += fl["book_present"]
            c_obj.book_nonempty_updates += fl["book_nonempty"]
            c_obj.book_valid_2sided_updates += fl["book_valid"]
            c_obj.book_2sided_qty_updates += fl["book_2sided_q"]
            c_obj.book_usable_1lot_updates += fl["book_1lot"]
            c_obj.book_l5_updates += fl["book_l5"]
            c_obj.book_only_no_ltp_updates += fl["book_only"]
            c_obj.book_empty_or_crossed_updates += fl["book_bad"]
            c_obj.oi_field_present += fl["oi_present"]
            c_obj.oi_positive_updates += fl["oi_pos"]
            c_obj.greeks_updates += fl["greeks"]
            c_obj.i1_candle_updates += fl["i1"]
            c_obj.unverified_candidate_quote_ts_fields += fl["cand_ts"]

            arm = fl["arm"]
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

            is_s1 = cohort_name not in ("NON_S1_UNIVERSE", "UNKNOWN_FO_UNCLASSIFIED")
            if is_s1:
                frame_s1_updates += 1
                if is_nifty_opt:
                    if fl["book_valid"]:
                        self._agg_s1_option_valid_books += 1
                        frame_opt_books += 1
                        if len(self._agg_seen_opt_valid_book_keys) < MAX_TRACKED_OPTION_COHORT_KEYS:
                            self._agg_seen_opt_valid_book_keys.add(ikey)
                    if fl["book_1lot"]:
                        self._agg_s1_option_1lot_books += 1
                        if len(self._agg_seen_opt_1lot_book_keys) < MAX_TRACKED_OPTION_COHORT_KEYS:
                            self._agg_seen_opt_1lot_book_keys.add(ikey)
                elif fl["book_valid"]:
                    self._agg_s1_non_option_books += 1

                # Per-S1-instrument gap & silence tracking (M2)
                pstat = self._agg_per_s1_inst.get(ikey)
                if pstat is None:
                    if len(self._agg_per_s1_inst) < MAX_PER_INSTRUMENT_STATS_KEYS:
                        pstat = PerInstrumentStats(cohort=cohort_name, mono_ns=t0_ns)
                        self._agg_per_s1_inst[ikey] = pstat
                    else:
                        self._agg_per_inst_key_cap_drops += 1
                        self._agg_coverage_valid = False
                        self._agg_invalidation_reasons.add("PER_INSTRUMENT_KEY_CAP_EXCEEDED")
                if pstat is not None:
                    pstat.on_update(t0_ns, bool(fl["book_valid"]), bool(fl["book_1lot"]))

        if frame_s1_updates > 0:
            self._agg_s1_relevant_frames += 1
            self._agg_s1_relevant_updates += frame_s1_updates

        self._worker_roll_time_buckets(
            slot_100ms=t0_ns // 100_000_000,
            slot_1s=t0_ns // 1_000_000_000,
            slot_1m=wall_ms // 60_000,
            raw_bytes=raw_bytes if is_bytes else 0,
            n_insts=len(parsed_rows),
            s1_updates=frame_s1_updates,
            opt_books=frame_opt_books,
        )

        # Latency accounting with stage-hook subtraction to prevent app misattribution (H3)
        stage_hook_ns = self._agg_pending_stage_hook_ns
        self._agg_pending_stage_hook_ns = 0

        if t1_ns >= t0_ns:
            self._agg_hist_sdk_decode_us.observe((t1_ns - t0_ns) / 1000.0)
        if t2_ns >= t1_ns:
            self._agg_hist_sdk_dict_us.observe((t2_ns - t1_ns) / 1000.0)
        if t3_ns >= t2_ns:
            raw_ds_ns = t3_ns - t2_ns
            app_ds_ns = max(0, raw_ds_ns - stage_hook_ns)
            self._agg_hist_raw_downstream_us.observe(raw_ds_ns / 1000.0)
            self._agg_hist_app_downstream_us.observe(app_ds_ns / 1000.0)

        full_added_ns = max(0, pre_ns + handoff_ns + stage_hook_ns)
        self._agg_hist_ingress_handoff_us.observe(handoff_ns / 1000.0)
        self._agg_hist_full_probe_added_us.observe(full_added_ns / 1000.0)
        if t3_ns >= t0_ns:
            self._agg_hist_total_callback_us.observe(((t3_ns - t0_ns) + pre_ns + handoff_ns) / 1000.0)

        self._agg_epoch_sealed_and_exported = False

    def _worker_process_err_frame(self, item: Tuple[Any, ...]) -> None:
        _, _ep, stage, raw_bytes, is_bytes, _wire_type_int, _data_dict, _wall_ns, _t0_ns = item
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
        self._agg_epoch_sealed_and_exported = False

    def _worker_loop(self) -> None:
        while not self._worker_stop:
            self._wake_event.wait(timeout=0.25)
            self._wake_event.clear()

            processed = 0
            while True:
                try:
                    item = self._inbox.popleft()
                except IndexError:
                    break
                processed += 1
                if (processed & 7) == 0:
                    time.sleep(0)  # Yield CPython GIL periodically during large bursts
                kind = item[0]
                if kind == _EVT_FRAME_OK:
                    b_len = int(item[2])
                    self._dequeued_wire_bytes_total += b_len
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
                    _, _ep, l_ns, eq_ns, hk_ns = item
                    if l_ns >= 0:
                        self._agg_hist_listener_us.observe(l_ns / 1000.0)
                    if eq_ns >= 0:
                        self._agg_hist_equity_us.observe(eq_ns / 1000.0)
                    if hk_ns >= 0:
                        self._agg_hist_stage_hook_us.observe(hk_ns / 1000.0)
                        self._agg_pending_stage_hook_ns = hk_ns
                elif kind == _EVT_PERIODIC_STAGE:
                    _, _ep, stg, dur_ns = item
                    if stg == "bar_flush_15m":
                        self._agg_hist_bar_flush_15m_us.observe(dur_ns / 1000.0)
                    elif stg == "freeze_snapshot":
                        self._agg_hist_freeze_snapshot_us.observe(dur_ns / 1000.0)
                elif kind == _EVT_LIFECYCLE:
                    _, ep_seq, ev, detail, mono_ns, done_evt = item
                    if ev == "probe_enabled":
                        self._agg_epoch_seq = ep_seq
                        self._agg_epoch_id = f"{self._boot_id}_ep{ep_seq:04d}"
                        self._agg_enabled_since_utc = str(detail)
                        self._agg_active_start_mono_ns = mono_ns
                        self._agg_control_intervals.append({"enabled_utc": str(detail), "disabled_utc": None})
                        self._worker_refresh_universe(force=True)
                    elif ev in ("__sync_barrier__", "__sync_barrier_export__"):
                        self._worker_refresh_universe(force=True)
                        if ev == "__sync_barrier_export__":
                            snap = self._build_snapshot_from_aggregator(include_rings=True, include_hist_bins=True)
                            if self._exporter.write_record("PERIODIC_CHECKPOINT", snap, update_checkpoint=True):
                                self._agg_last_exported_frames = self._agg_ingress_attempts
                    else:
                        self._worker_record_lifecycle_event(ev, detail, mono_ns)
                    if done_evt is not None:
                        snap = self._build_snapshot_from_aggregator(include_rings=True)
                        self._published_snapshot = snap
                        self._published_compact = self._build_compact_from_snapshot(snap)
                        done_evt.set()
                elif kind == _EVT_SEAL_EPOCH:
                    _, target_ep, reason, seal_utc, seal_mono_ns, reset_after, seal_done = item
                    if self._agg_active_start_mono_ns > 0 and seal_mono_ns > self._agg_active_start_mono_ns:
                        self._agg_accumulated_enabled_ns += (seal_mono_ns - self._agg_active_start_mono_ns)
                        self._agg_active_start_mono_ns = 0
                    self._agg_disabled_at_utc = seal_utc
                    if self._agg_control_intervals and self._agg_control_intervals[-1]["disabled_utc"] is None:
                        self._agg_control_intervals[-1]["disabled_utc"] = seal_utc
                        self._agg_control_intervals[-1]["reason"] = reason
                    self._worker_finalize_active_buckets()
                    seal_snap = self._build_snapshot_from_aggregator(include_rings=True, include_hist_bins=True)
                    seal_snap["seal_reason"] = reason
                    ok = self._exporter.write_record("EPOCH_SEALED", seal_snap, update_checkpoint=True)
                    if ok:
                        self._agg_last_exported_frames = self._agg_ingress_attempts
                        self._agg_epoch_sealed_and_exported = True
                    else:
                        self._agg_epoch_sealed_and_exported = (self._exporter.configured_dir is None)

                    if reset_after:
                        self._init_aggregator_state(epoch_seq=target_ep)

                    pub = self._build_snapshot_from_aggregator(include_rings=True)
                    self._published_snapshot = pub
                    self._published_compact = self._build_compact_from_snapshot(pub)
                    if seal_done is not None:
                        seal_done.set()

            now_ns = time.perf_counter_ns()
            if self._capture_enabled and (now_ns - self._agg_last_checkpoint_ns) >= DEFAULT_EXPORT_INTERVAL_NS:
                self._agg_last_checkpoint_ns = now_ns
                ckpt_snap = self._build_snapshot_from_aggregator(include_rings=True, include_hist_bins=True)
                if self._exporter.write_record("PERIODIC_CHECKPOINT", ckpt_snap, update_checkpoint=True):
                    self._agg_last_exported_frames = self._agg_ingress_attempts

            if (processed > 0 or self._capture_enabled) and (now_ns - self._agg_last_snapshot_pub_ns) >= 250_000_000:
                self._agg_last_snapshot_pub_ns = now_ns
                snap = self._build_snapshot_from_aggregator(include_rings=True)
                self._published_snapshot = snap
                self._published_compact = self._build_compact_from_snapshot(snap)

    def _worker_record_lifecycle_event(self, ev: str, detail: Any, mono_ns: int) -> None:
        wall_ms = int(time.time() * 1000)
        try:
            detail_str = str(detail)[:160] if detail is not None else None
        except Exception:
            detail_str = "<UNFORMATTABLE_DETAIL>"

        if ev == "open":
            self._agg_ws_open_count += 1
            if self._agg_last_disconnect_mono_ns > 0 and mono_ns > self._agg_last_disconnect_mono_ns:
                gap_ms = (mono_ns - self._agg_last_disconnect_mono_ns) / 1_000_000.0
                if gap_ms > self._agg_max_disconnect_gap_ms:
                    self._agg_max_disconnect_gap_ms = gap_ms
                self._agg_last_disconnect_mono_ns = 0
        elif ev == "close":
            self._agg_ws_close_count += 1
            if self._agg_last_disconnect_mono_ns == 0:
                self._agg_last_disconnect_mono_ns = mono_ns
        elif ev == "error":
            self._agg_ws_error_count += 1
        elif ev == "reconnecting":
            self._agg_ws_reconnecting_count += 1
            if self._agg_last_disconnect_mono_ns == 0:
                self._agg_last_disconnect_mono_ns = mono_ns
        elif ev == "autoReconnectStopped":
            self._agg_ws_auto_reconnect_stopped_count += 1

        if len(self._agg_lifecycle_events) == LIFECYCLE_RING_MAXLEN:
            self._agg_lifecycle_evictions += 1
        self._agg_lifecycle_events.append({"ts_ms": wall_ms, "event": ev, "detail": detail_str})

    # ──────────────────────────────────────────────────────────────────────────
    # IMMUTABLE SNAPSHOT CONSTRUCTION (PUBLISHED ATOMICALLY BY WORKER)
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _read_passive_s205_telemetry_off_callback() -> Dict[str, Any]:
        try:
            import sys
            mod = sys.modules.get("app.analytics.option_sniffer_advance_s205.s205_upstox_engine")
            if mod is None:
                return {"loaded": False}
            fn = getattr(mod, "get_s205_orchestrator_if_loaded", None)
            orch = fn() if callable(fn) else None
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

    def _build_snapshot_from_aggregator(
        self, include_rings: bool = True, include_hist_bins: bool = False
    ) -> Dict[str, Any]:
        now_mono_ns = time.perf_counter_ns()
        enabled_ns = self._agg_accumulated_enabled_ns
        if self._agg_active_start_mono_ns > 0 and now_mono_ns > self._agg_active_start_mono_ns:
            enabled_ns += (now_mono_ns - self._agg_active_start_mono_ns)
        enabled_span_s = enabled_ns / 1_000_000_000.0

        frame_span_s = (
            (self._agg_last_frame_mono_ns - self._agg_first_frame_mono_ns) / 1_000_000_000.0
            if self._agg_decoded_ok_frames >= 2 and self._agg_last_frame_mono_ns > self._agg_first_frame_mono_ns
            else 0.0
        )
        effective_span_s = enabled_span_s if enabled_span_s > 0.0 else frame_span_s
        sustained_frames_s = (self._agg_ingress_attempts / effective_span_s) if effective_span_s > 0 else 0.0
        sustained_bytes_s = (self._agg_ingress_wire_bytes / effective_span_s) if effective_span_s > 0 else 0.0

        # Epoch-bounded intersection coverage (M2: strictly <= 1.0, never > 100%)
        intended_sub = self._agg_current_subscribed_keys
        delivered_in_intended = self._agg_seen_in_current_u_epoch & intended_sub
        current_u_coverage = (
            round(len(delivered_in_intended) / float(len(intended_sub)), 6)
            if intended_sub
            else 0.0
        )

        core_intended = set(S1_CORE_INSTRUMENT_KEYS.keys()) & intended_sub if intended_sub else set()
        core_delivered_in_intended = self._agg_seen_s1_core_keys & core_intended

        sub_opt_1_7 = {
            k for k, (c, _) in self._agg_current_contract_meta.items()
            if c == "NIFTY_OPTIONS_1_7_DTE" and (not intended_sub or k in intended_sub)
        }
        sub_opt_other = {
            k for k, (c, _) in self._agg_current_contract_meta.items()
            if c == "NIFTY_OPTIONS_OTHER_DTE" and (not intended_sub or k in intended_sub)
        }

        # Per-instrument worst gap AND trailing silence since last tick (M2)
        obs_end_ns = self._agg_last_frame_mono_ns if self._agg_last_frame_mono_ns > 0 else now_mono_ns
        worst_inst_update_gap_ms = 0.0
        worst_inst_silence_ms = 0.0
        worst_inst_key: Optional[str] = None
        for ik, pst in self._agg_per_s1_inst.items():
            if pst.max_update_gap_ms > worst_inst_update_gap_ms:
                worst_inst_update_gap_ms = pst.max_update_gap_ms
            trail_ms = max(0.0, (obs_end_ns - pst.last_mono_ns) / 1_000_000.0) if obs_end_ns >= pst.last_mono_ns else 0.0
            eff_absence = max(pst.max_update_gap_ms, trail_ms)
            if eff_absence > worst_inst_silence_ms:
                worst_inst_silence_ms = eff_absence
                worst_inst_key = ik

        overflow_drops = self._inbox_overflow_drops
        cov_valid = self._agg_coverage_valid and (overflow_drops == 0)
        inv_reasons = sorted(
            self._agg_invalidation_reasons | ({"INBOX_OVERFLOW"} if overflow_drops > 0 else set())
        )

        unexported_tail = max(0, self._agg_ingress_attempts - self._agg_last_exported_frames)
        exp_status = self._exporter.status_snapshot(now_mono_ns)
        exp_status["epoch_id"] = self._agg_epoch_id
        exp_status["unexported_tail_frames"] = unexported_tail
        exp_status["tail_state"] = (
            "COMPLETE_SEALED"
            if (not self._capture_enabled and unexported_tail == 0 and self._agg_epoch_sealed_and_exported)
            else "UNEXPORTED_LIVE_OR_INCOMPLETE_TAIL"
        )

        full_added_sum = self._agg_hist_full_probe_added_us.summary(include_bins=include_hist_bins)
        handoff_sum = self._agg_hist_ingress_handoff_us.summary(include_bins=include_hist_bins)
        app_ds_sum = self._agg_hist_app_downstream_us.summary(include_bins=include_hist_bins)
        total_cb_sum = self._agg_hist_total_callback_us.summary(include_bins=include_hist_bins)
        frame_bytes_sum = self._agg_hist_frame_bytes.summary(include_bins=include_hist_bins)
        rate_1s_bytes_sum = self._agg_hist_1s_all_bytes.summary(include_bins=include_hist_bins)

        opt_1_7_c = self._agg_cohorts["NIFTY_OPTIONS_1_7_DTE"]
        opt_other_c = self._agg_cohorts["NIFTY_OPTIONS_OTHER_DTE"]
        tot_opt_updates = opt_1_7_c.updates + opt_other_c.updates
        tot_opt_1lot_books = opt_1_7_c.book_usable_1lot_updates + opt_other_c.book_usable_1lot_updates

        s1_update_share = (
            self._agg_s1_relevant_updates / float(self._agg_total_instrument_updates)
            if self._agg_total_instrument_updates > 0
            else 0.0
        )
        p95_ub_bytes_s = rate_1s_bytes_sum["p95_upper_bound"] if rate_1s_bytes_sum["count"] > 0 else sustained_bytes_s
        session_s = 22500.0

        codex_feasibility = {
            "coverage_valid": cov_valid,
            "invalidation_reasons": inv_reasons,
            "clock_certification_valid": self._agg_clock_certification_valid,
            "wall_clock_step_events": self._agg_clock_step_events,
            "enabled_monotonic_span_seconds": round(enabled_span_s, 3),
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
                "full_probe_added_p99_upper_bound_us": full_added_sum["p99_upper_bound"],
                "full_probe_added_p999_upper_bound_us": full_added_sum["p999_upper_bound"],
                "full_probe_added_exceed_100us_count": full_added_sum["exceed_100_count"],
                "full_probe_added_exceed_1000us_count": full_added_sum["exceed_1000_count"],
                "passes_p99_le_100us_and_p999_le_1000us": (
                    full_added_sum["p99_upper_bound"] <= 100.0
                    and full_added_sum["p999_upper_bound"] <= 1000.0
                ),
                "app_downstream_dispatch_p99_upper_bound_us": app_ds_sum["p99_upper_bound"],
                "total_callback_p99_upper_bound_us": total_cb_sum["p99_upper_bound"],
            },
            "s1_option_book_and_time_provenance": {
                "s1_core_intended_subscribed_count": len(core_intended),
                "s1_core_delivered_in_intended_count": len(core_delivered_in_intended),
                "nifty_options_1_7_dte_subscribed": len(sub_opt_1_7),
                "nifty_options_1_7_dte_delivered_in_subscribed": len(self._agg_seen_opt_1_7_keys & sub_opt_1_7),
                "nifty_options_valid_2sided_book_updates": self._agg_s1_option_valid_books,
                "nifty_options_usable_1lot_book_updates": self._agg_s1_option_1lot_books,
                "non_option_s1_equity_or_futures_book_updates": self._agg_s1_non_option_books,
                "nifty_option_usable_1lot_book_ratio": (
                    round(tot_opt_1lot_books / float(tot_opt_updates), 6) if tot_opt_updates > 0 else 0.0
                ),
                "per_instrument_worst_update_gap_ms": round(worst_inst_update_gap_ms, 2),
                "per_instrument_worst_absence_or_silence_ms": round(worst_inst_silence_ms, 2),
                "per_instrument_worst_silence_key": worst_inst_key,
                "provider_current_ts_present_ratio": (
                    round(self._agg_frames_with_current_ts / float(self._agg_decoded_ok_frames), 6)
                    if self._agg_decoded_ok_frames > 0
                    else 0.0
                ),
                "unverified_candidate_quote_ts_field_observations": sum(
                    c.unverified_candidate_quote_ts_fields for c in self._agg_cohorts.values()
                ),
                "time_provenance_qualification": (
                    "Provider envelope currentTs lag mixes clock offset, transport, and scheduling; "
                    "candidate ts/ltt fields are unverified and NEVER treated as exchange book time."
                ),
            },
            "heuristic_22500s_session_projections_unqualified_for_final_sizing": {
                "qualification": (
                    "Measured wire bytes cover full envelopes; S1 share uses update-count proportion "
                    "(not measured per-cohort protobuf byte slices), and 2.8x JSON/base64 is a prototype "
                    "encoding assumption."
                ),
                "measured_full_envelope_sustained_gib_formula": "sustained_wire_bytes_per_sec * 22500 / 2^30",
                "measured_full_envelope_sustained_gib": round(
                    (sustained_bytes_s * session_s) / float(1024 ** 3), 4
                ),
                "measured_full_envelope_p95_ub_gib": round(
                    (p95_ub_bytes_s * session_s) / float(1024 ** 3), 4
                ),
                "assumed_2_8x_json_base64_est_gib": round(
                    (sustained_bytes_s * 2.8 * session_s) / float(1024 ** 3), 4
                ),
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
            "control_intervals": list(self._agg_control_intervals),
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
                "coverage_valid": cov_valid,
                "invalidation_reasons": inv_reasons,
                "universe_epoch_seq": self._agg_universe_epoch_seq,
                "intended_subscribed_keys_current_epoch": len(intended_sub),
                "delivered_in_intended_current_epoch": len(delivered_in_intended),
                "current_epoch_coverage_ratio": current_u_coverage,
                "delivered_outside_current_subscribed_count": self._agg_delivered_outside_subscribed_count,
                "lifetime_unique_keys_capped": len(self._agg_seen_all_keys),
                "s1_core_intended_subscribed": sorted(core_intended),
                "s1_core_delivered_in_intended": sorted(core_delivered_in_intended),
                "s1_core_missing_from_intended": sorted(core_intended - core_delivered_in_intended),
                "nifty_options_1_7_dte_subscribed": len(sub_opt_1_7),
                "nifty_options_1_7_dte_delivered_in_subscribed": len(self._agg_seen_opt_1_7_keys & sub_opt_1_7),
                "nifty_options_other_dte_subscribed": len(sub_opt_other),
                "nifty_options_other_dte_delivered_in_subscribed": len(self._agg_seen_opt_other_keys & sub_opt_other),
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
                "post_disable_inflight_drops": self._post_disable_inflight_drops,
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
            res["recent_1s_rates"] = list(self._agg_ring_1s)[-60:]
            res["recent_100ms_bursts"] = list(self._agg_ring_100ms)[-100:]
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
            "ingress_attempts_total": tot["ingress_attempts_total"],
            "ingress_wire_bytes_total": tot["ingress_wire_bytes_total"],
            "sustained_frames_per_sec": cf["sustained_ingress_attempts_per_sec"],
            "sustained_bytes_per_sec": cf["sustained_wire_bytes_per_sec"],
            "peak_100ms_frames": cf["peak_100ms_frames"],
            "peak_1s_frames": cf["peak_1s_frames"],
            "s1_option_usable_1lot_books": tot["s1_option_usable_1lot_books"],
            "full_probe_added_p99_ub_us": gates["full_probe_added_p99_upper_bound_us"],
        }


def require_streamer_attached_default_check(val: Optional[bool]) -> bool:
    return val is None


def build_instrumented_streamer_class(base_streamer_cls: Any, probe: S1IngressMeasurementProbe) -> Any:
    """
    Lightweight pass-through subclass of `upstox_client.MarketDataStreamerV3`.
    - Disabled fast path: single boolean check (`not p._capture_enabled`) -> `super().handle_message`.
    - Enabled path: counts wire bytes BEFORE decode, preserves `FeedResponse.type` enum `0`,
      never hides/retries/duplicates SDK exceptions, and guards all probe-only work inside
      outer `try/except` blocks (`H4`).
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
                raw_bytes = len(message) if is_bytes else 0
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

            wire_type_int = -1
            try:
                wire_type_int = int(getattr(decoded_data, "type", -1))
            except Exception:
                wire_type_int = -1

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

            try:
                self.emit(self.Event["MESSAGE"], data_dict)
            except Exception:
                try:
                    p.record_ingress_error_attempt(
                        stage="emit_message",
                        raw_bytes=raw_bytes,
                        is_bytes=is_bytes,
                        recv_wall_ns=recv_wall_ns,
                        t0_ns=t0_ns,
                        wire_type_int=wire_type_int,
                        data_dict=data_dict,
                    )
                except Exception as obs_e:
                    try:
                        p.note_observer_fault("emit_error_hook", obs_e)
                    except Exception:
                        pass
                raise
            t3_ns = time.perf_counter_ns()

            # Outer observer-only boundary guard (H4): NEVER allow a probe fault to escape after delivery
            try:
                p.record_ingress_frame(
                    raw_bytes=raw_bytes,
                    is_bytes=is_bytes,
                    wire_type_int=wire_type_int,
                    data_dict=data_dict,
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
    """Attaches fully fault-isolated passive SDK lifecycle observers (H4)."""
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
