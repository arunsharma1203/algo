"""
Phase 1 S1 Market-Data Workload Meter (V4 — Minimal Safe Design).

Scope & Non-Negotiable Architecture Invariants:
  1. Workload Meter Only: Counts incoming WebSocket frames, raw wire byte lengths,
     frame-size peaks/exceedances, `len(already-decoded feeds)`, fixed monotonic time
     buckets (`100ms`, `1s`, `60s`), lifecycle/reconnect events, and callback/stage timings.
  2. Zero Second Protobuf Parse & Zero Per-Instrument Loops: Delegates directly to
     `super().handle_message(ws, message)` for 100% original Upstox SDK V3 delivery.
     No protobuf re-decoding, no per-instrument classification, no raw-frame byte retention.
  3. Lock-Free, Wakeup-Free Callback with Checked Readiness Marker:
     - Zero `threading.Lock` and zero `threading.Event.set()` on the market-data callback.
     - Scalar-only bounded observation queue (`MAX_INBOX_SAMPLES = 4096`, `< 1 MiB` RAM).
     - Complete entry-to-exit callback timing (`t_cb_exit` sampled AFTER `_inbox.append`).
     - Readiness slot initialized to `-1` and set to `>= 0` after `t_cb_exit`; worker never
       consumes an unready `-1` as `0.0 us` and explicitly counts unavailable timings if a
       producer faults or stalls past grace.
  4. Strict Disable Dominance (`_control_gen`):
     - Emergency `disable` never waits on `_admin_op_lock` and cannot be rate-limited.
     - Monotonic `_control_gen` / `_cancelled_below_gen` checked by BOTH `enable()` caller
       and worker initialization guarantees `disable()` always overrides older pending enables.
  5. Exact Approved Frontend Origins:
     - No broad `*.ts.net` or `100.64.0.0/10` regexes. Only exact localhost origins plus
       this host's exact Tailscale frontend origins (and validated `CORS_ALLOWED_ORIGINS`).
     - Probe management endpoints remain strictly loopback-only (`127.0.0.1`, `::1`, `localhost`).
  6. Bounded, Truthful, Recoverable Asynchronous Export:
     - Zero disk scan during default-OFF module/probe construction.
     - Per-`(boot_id, stream_gen)` hash-chained JSONL ledgers.
     - Real on-disk byte accounting across all ledger, checkpoint, manifest, and temp files
       on both success and failure paths (`MAX_EXPORT_STORAGE_BYTES = 64 MiB`).
     - Control-audit cursors advance ONLY after verified durable write + `fsync`.
     - Bounded retry + explicit `missing_durable_intervals` tracking (`SEALED_WITH_DURABLE_GAPS`).
"""

from __future__ import annotations

import bisect
import collections
import copy
import datetime
import enum
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
import urllib.parse
import uuid
import weakref
from pathlib import Path
from typing import Any, Callable, Deque, Dict, FrozenSet, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION: str = "S1_WORKLOAD_METER_V4"
PROBE_WRAPPER_VERSION: str = "S1_STREAMER_WRAPPER_V4"
IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")

# Bounded memory, queue, worker, and storage constants
MAX_INBOX_SAMPLES: int = 4096                       # Scalar-only tuples/lists (< 1 MiB total heap)
MAX_PENDING_MINUTE_EXPORTS: int = 64                # Bounded retry queue for 60s buckets
MAX_MISSING_EXPORT_INTERVALS: int = 64              # Bounded explicit gap log per epoch
MAX_CONTROL_INTERVALS: int = 64
MAX_CONTROL_AUDIT_RING: int = 128
RING_100MS_MAXLEN: int = 100
RING_1S_MAXLEN: int = 120
RING_1M_MAXLEN: int = 60
LIFECYCLE_RING_MAXLEN: int = 64

WORKER_POLL_SLEEP_S: float = 0.050                  # 50 ms infrequent background drain (~0.05% CPU)
WORKER_BACKLOG_SLEEP_S: float = 0.005               # 5 ms cooperative drain (>25,000 Hz capacity, <200 us/slice)
WORKER_MAX_BATCH_ITEMS: int = 128                   # Micro-batch (< 200 us of scalar integer math per GIL turn)
WORKER_STALL_TIMEOUT_NS: int = 5_000_000_000        # 5.0 s worker heartbeat watchdog
UNREADY_CELL_GRACE_NS: int = 250_000_000            # 250 ms grace before marking interrupted cell timing unavailable
SNAPSHOT_PUBLISH_INTERVAL_NS: int = 1_000_000_000   # 1.0 s snapshot refresh cadence

MAX_CONSECUTIVE_PROBE_FAULTS: int = 5
MAX_CONSECUTIVE_LATENCY_BREACHES: int = 5           # > 1000 us probe-added callback cost
LATENCY_P99_BUDGET_US: float = 100.0
LATENCY_SINGLE_BREACH_US: float = 1000.0
LATENCY_SEVERE_STALL_US: float = 5000.0
MAX_SEVERE_LATENCY_STALLS: int = 3
ROLLING_LATENCY_WINDOW: int = 256
MIN_ROLLING_SAMPLES_FOR_P99_TRIP: int = 100

MAX_EXPORT_STORAGE_BYTES: int = 64 * 1024 * 1024    # 64 MiB hard cap across all files in export_dir
MAX_EXPORT_RECORD_BYTES: int = 262144               # 256 KiB max single JSONL record
DEFAULT_EXPORT_INTERVAL_NS: int = 180_000_000_000   # 180 s periodic checkpoint (+ 60 s MINUTE_WINDOW)

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

FEEDS_PER_FRAME_BOUNDS: Tuple[float, ...] = (
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

# Strictly local origins allowed for BOTH app CORS and loopback probe administration
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

# Exact approved frontend origins for THIS host's verified Tailscale node only (dashboard CORS only;
# NEVER permitted on loopback-restricted probe management endpoints).
APPROVED_TAILSCALE_FRONTEND_ORIGINS: FrozenSet[str] = frozenset({
    "http://100.97.41.35:5173",
    "http://100.97.41.35:4173",
    "http://[fd7a:115c:a1e0::7c01:29bb]:5173",
    "http://[fd7a:115c:a1e0::7c01:29bb]:4173",
    "http://aruns-macbook-pro.tailadf4b0.ts.net:5173",
    "http://aruns-macbook-pro.tailadf4b0.ts.net:4173",
    "https://aruns-macbook-pro.tailadf4b0.ts.net",
})

LOOPBACK_HOSTS: FrozenSet[str] = frozenset({"127.0.0.1", "::1", "localhost"})
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Scalar inbox event tags
_EVT_FRAME_OK = 1
_EVT_FRAME_ERR = 2
_EVT_PERIODIC_STAGE = 3
_EVT_LIFECYCLE = 4


def is_exact_http_origin(candidate: str) -> bool:
    """Validates an exact `http(s)://host[:port]` Origin with no wildcard, path, query, or userinfo."""
    s = (candidate or "").strip()
    if not s or "*" in s or "?" in s or "#" in s or " " in s:
        return False
    try:
        parsed = urllib.parse.urlsplit(s)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc or parsed.username or parsed.password:
        return False
    if parsed.path not in ("", "/"):
        return False
    if s.endswith("/"):
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    return bool(parsed.hostname)


def resolve_cors_allowed_origins(extra_csv: Optional[str] = None) -> List[str]:
    """
    Resolves exact allowed frontend Origins for FastAPI CORSMiddleware:
      - Exact localhost dev/preview origins
      - Exact host Tailscale node dev/preview origins
      - Validated exact origins from `CORS_ALLOWED_ORIGINS` (wildcards/malformed entries ignored)
    """
    allowed: Set[str] = set(DEFAULT_ALLOWED_LOCAL_ORIGINS) | set(APPROVED_TAILSCALE_FRONTEND_ORIGINS)
    raw_csv = extra_csv if extra_csv is not None else os.environ.get("CORS_ALLOWED_ORIGINS", "")
    for item in (raw_csv or "").split(","):
        cleaned = item.strip()
        if cleaned and is_exact_http_origin(cleaned):
            allowed.add(cleaned)
    return sorted(allowed)


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
            raise OSError("Zero-byte write while flushing durable workload record")
        written += n


class BoundedHistogram:
    """Fixed-bin histogram supporting O(1) `observe` and O(1) `observe_many` for zero-bucket gaps."""

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

    def merge_from_dict(self, exported: Dict[str, Any]) -> None:
        bins = exported.get("bin_counts")
        bounds = exported.get("bounds")
        if not isinstance(bins, list) or len(bins) != len(self._counts):
            return
        if bounds is not None and [float(x) for x in bounds] != list(self._bounds):
            return
        c_add = int(exported.get("count", 0))
        if c_add <= 0:
            return
        for i, b_val in enumerate(bins):
            self._counts[i] += int(b_val)
        self._count += c_add
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

    def summary(self, decimals: int = 2, include_bins: Any = False) -> Dict[str, Any]:
        if self._count == 0:
            out: Dict[str, Any] = {
                "count": 0,
                "sum": 0.0,
                "min": 0.0,
                "mean": 0.0,
                "p50_upper_bound": 0.0,
                "p95_upper_bound": 0.0,
                "p99_upper_bound": 0.0,
                "p999_upper_bound": 0.0,
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
                "p95_upper_bound": round(self.quantile_upper_bound(0.95), decimals),
                "p99_upper_bound": round(self.quantile_upper_bound(0.99), decimals),
                "p999_upper_bound": round(self.quantile_upper_bound(0.999), decimals),
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


class _TokenBucketLimiter:
    def __init__(self, rate_per_sec: float, burst: float) -> None:
        self._rate = float(rate_per_sec)
        self._burst = float(burst)
        self._tokens = float(burst)
        self._last_ns = time.perf_counter_ns()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now_ns = time.perf_counter_ns()
            elapsed_s = max(0.0, (now_ns - self._last_ns) / 1_000_000_000.0)
            self._last_ns = now_ns
            self._tokens = min(self._burst, self._tokens + elapsed_s * self._rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class S1ProbeManagementGuard:
    """
    Loopback-restricted bearer token authenticator for probe endpoints.
    - Rejects proxy headers, non-loopback peers, Cookie headers, cross-site fetches, and non-local Origins.
    - Never rate-limits authenticated loopback ADMIN emergency `disable`.
    """

    def __init__(self) -> None:
        self._read_limiter = _TokenBucketLimiter(rate_per_sec=2.0, burst=12.0)
        self._write_limiter = _TokenBucketLimiter(rate_per_sec=4.0 / 60.0, burst=4.0)

    @staticmethod
    def _extract_bearer_token(norm_headers: Dict[str, str]) -> Optional[str]:
        auth = norm_headers.get("authorization", "").strip()
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
            if tok:
                return tok
        alt = norm_headers.get("x-s1-probe-token", "").strip()
        return alt if alt else None

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
            if not is_emergency_disable and not self._write_limiter.allow():
                raise ProbeControlError(429, "RATE_LIMITED_CONTROL", "Probe control rate limit exceeded")
            return "ADMIN"

        if not (is_admin or is_reader):
            raise ProbeControlError(403, "INVALID_TOKEN", "Invalid management bearer token")
        if not self._read_limiter.allow():
            raise ProbeControlError(429, "RATE_LIMITED_READ", "Probe summary read rate limit exceeded")
        return "ADMIN" if is_admin else "READ"


class S1MeasurementSummaryExporter:
    """
    Bounded, asynchronous, hash-chained JSONL summary exporter (V4).
    - Zero filesystem access until first write or explicit recovery call.
    - Real on-disk byte accounting across ALL files in `export_dir` (`ledger_*.jsonl`,
      `manifest.json`, `last_checkpoint.json`, and any residual `*.tmp.*` files) before AND
      after every write/failure path. Never deletes existing artifacts to reclaim quota.
    - Primary ledger `fsync` commits `_export_seq` / `_prev_record_sha256` independently of
      secondary hint files; primary write/`fsync` failure rotates `_stream_gen` cleanly.
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
        self._max_storage_bytes: int = int(max_storage_bytes)
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
        self._quota_exhausted: bool = False

    @property
    def configured_dir(self) -> Optional[Path]:
        return self._export_dir

    @staticmethod
    def _actual_dir_bytes(target_dir: Path) -> int:
        if not target_dir.exists():
            return 0
        total = 0
        try:
            for entry in os.scandir(target_dir):
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += int(entry.stat(follow_symlinks=False).st_size)
                except OSError:
                    pass
        except OSError:
            pass
        return total

    def _atomic_write_secondary_unlocked(self, target_dir: Path, filename: str, raw_bytes: bytes) -> None:
        cur_used = self._actual_dir_bytes(target_dir)
        # During atomic write, tmp + existing destination coexist transiently on disk
        if cur_used + len(raw_bytes) > self._max_storage_bytes:
            raise OSError("Secondary hint skipped to preserve strict directory byte quota")
        dest_path = target_dir / filename
        tmp_path = target_dir / f".{filename}.tmp.{self._process_id}.{uuid.uuid4().hex[:8]}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(tmp_path), flags, 0o600)
        try:
            _write_all_fd(fd, raw_bytes)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp_path), str(dest_path))
        _fsync_directory_best_effort(target_dir)

    def write_record(
        self,
        record_kind: str,
        payload: Dict[str, Any],
        *,
        update_checkpoint: bool = True,
    ) -> bool:
        if self._export_dir is None:
            return False

        with self._lock:
            target_dir = self._export_dir
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(str(target_dir), 0o700)
                except Exception:
                    pass
                actual_before = self._actual_dir_bytes(target_dir)
                self._storage_bytes_used = actual_before

                now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
                candidate_seq = self._export_seq + 1
                gen_num = self._stream_gen
                ledger_name = f"ledger_{self._boot_ist_date}_{self._boot_id}_g{gen_num:02d}.jsonl"
                ledger_path = target_dir / ledger_name

                body_without_hash = {
                    "schema_version": SCHEMA_VERSION,
                    "record_kind": record_kind,
                    "export_seq": candidate_seq,
                    "stream_gen": gen_num,
                    "boot_id": self._boot_id,
                    "process_id": self._process_id,
                    "exported_at_utc": now_utc,
                    "prev_record_sha256": self._prev_record_sha256,
                    "payload": payload,
                }
                canonical_bytes = json.dumps(
                    body_without_hash, separators=(",", ":"), sort_keys=True
                ).encode("utf-8")
                rec_sha = hashlib.sha256(canonical_bytes).hexdigest()
                full_record = dict(body_without_hash)
                full_record["record_sha256"] = rec_sha
                line_bytes = (
                    json.dumps(full_record, separators=(",", ":"), sort_keys=True) + "\n"
                ).encode("utf-8")

                if len(line_bytes) > MAX_EXPORT_RECORD_BYTES:
                    self._export_failures += 1
                    self._last_export_error = f"RECORD_TOO_LARGE:{len(line_bytes)}"
                    return False

                if actual_before + len(line_bytes) > self._max_storage_bytes:
                    self._quota_exhausted = True
                    self._export_failures += 1
                    self._last_export_error = (
                        f"STORAGE_QUOTA_EXCEEDED:{actual_before + len(line_bytes)}>{self._max_storage_bytes}"
                    )
                    return False

                is_new_file = not ledger_path.exists()
                open_flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(str(ledger_path), open_flags, 0o600)
                try:
                    _write_all_fd(fd, line_bytes)
                    os.fsync(fd)
                except Exception as primary_exc:
                    # Rotate generation so subsequent writes never append after an un-fsynced tail!
                    self._stream_gen += 1
                    self._prev_record_sha256 = "0" * 64
                    self._export_failures += 1
                    self._last_export_error = f"PRIMARY_WRITE_OR_FSYNC_FAILED:{type(primary_exc).__name__}:{primary_exc}"
                    return False
                finally:
                    os.close(fd)
                    self._storage_bytes_used = self._actual_dir_bytes(target_dir)

                if is_new_file:
                    _fsync_directory_best_effort(target_dir)

                # Primary record is now durable on disk; commit sequence & hash chain immediately
                self._export_seq = candidate_seq
                self._prev_record_sha256 = rec_sha
                self._last_durable_seq = candidate_seq
                self._last_durable_utc = now_utc
                self._last_durable_mono_ns = time.perf_counter_ns()
                self._quota_exhausted = False
                self._last_export_error = None

                gen_meta = self._generation_manifest.get(ledger_name)
                if gen_meta is None:
                    gen_meta = {
                        "file": ledger_name,
                        "stream_gen": gen_num,
                        "first_seq": candidate_seq,
                        "last_seq": candidate_seq,
                        "records": 1,
                        "last_sha256": rec_sha,
                    }
                    self._generation_manifest[ledger_name] = gen_meta
                else:
                    gen_meta["last_seq"] = candidate_seq
                    gen_meta["records"] = int(gen_meta["records"]) + 1
                    gen_meta["last_sha256"] = rec_sha

                if record_kind == "EPOCH_SEALED":
                    ep_id = str(payload.get("epoch_id") or "")
                    if ep_id and ep_id not in self._sealed_epochs_recorded:
                        self._sealed_epochs_recorded.append(ep_id)
                        if len(self._sealed_epochs_recorded) > 128:
                            self._sealed_epochs_recorded = self._sealed_epochs_recorded[-128:]

                if update_checkpoint:
                    try:
                        cp_bytes = json.dumps(full_record, separators=(",", ":"), sort_keys=True).encode("utf-8")
                        self._atomic_write_secondary_unlocked(target_dir, "last_checkpoint.json", cp_bytes)
                        manifest_doc = {
                            "schema_version": SCHEMA_VERSION,
                            "boot_id": self._boot_id,
                            "process_id": self._process_id,
                            "updated_at_utc": now_utc,
                            "last_durable_seq": self._last_durable_seq,
                            "generations": list(self._generation_manifest.values()),
                            "sealed_epochs": list(self._sealed_epochs_recorded),
                        }
                        mf_bytes = json.dumps(manifest_doc, separators=(",", ":"), sort_keys=True).encode("utf-8")
                        self._atomic_write_secondary_unlocked(target_dir, "manifest.json", mf_bytes)
                    except Exception as sec_exc:
                        self._checkpoint_warnings += 1
                        self._last_checkpoint_error = f"{type(sec_exc).__name__}:{sec_exc}"

                return True
            except Exception as outer_exc:
                self._export_failures += 1
                self._last_export_error = f"{type(outer_exc).__name__}:{outer_exc}"
                return False
            finally:
                self._storage_bytes_used = self._actual_dir_bytes(target_dir)

    def status_snapshot(self, now_mono_ns: int) -> Dict[str, Any]:
        with self._lock:
            if self._export_dir is not None and self._export_dir.exists():
                self._storage_bytes_used = self._actual_dir_bytes(self._export_dir)
            age_ms = (
                round(max(0.0, (now_mono_ns - self._last_durable_mono_ns) / 1_000_000.0), 2)
                if self._last_durable_mono_ns > 0
                else None
            )
            return {
                "configured_dir": str(self._export_dir) if self._export_dir is not None else None,
                "boot_id": self._boot_id,
                "stream_gen": self._stream_gen,
                "last_durable_seq": self._last_durable_seq,
                "last_durable_utc": self._last_durable_utc,
                "last_durable_age_ms": age_ms,
                "export_failures": self._export_failures,
                "checkpoint_warnings": self._checkpoint_warnings,
                "last_export_error": self._last_export_error,
                "last_checkpoint_error": self._last_checkpoint_error,
                "storage_bytes_used": self._storage_bytes_used,
                "storage_quota_max_bytes": self._max_storage_bytes,
                "quota_exhausted": self._quota_exhausted,
            }

    @classmethod
    def load_and_verify_ledger_dir(cls, export_dir: Path) -> Dict[str, Any]:
        """
        Independently verifies all `ledger_*.jsonl` files in `export_dir`:
          - Validates per-file SHA-256 hash chains and isolates torn/un-fsynced tails.
          - Deduplicates epochs (`EPOCH_SEALED` authoritative over `PERIODIC_CHECKPOINT`).
          - Distinguishes complete sealed epochs from gapped seals (`missing_durable_intervals_count > 0`)
            and unsealed crash epochs (checkpoint-only).
          - Collects all durably exported `MINUTE_WINDOW` records and `control_audit_delta` entries.
        """
        target = Path(export_dir)
        actual_bytes = cls._actual_dir_bytes(target)
        if not target.exists():
            return {
                "files_scanned": 0,
                "valid_records": 0,
                "corrupt_or_torn_records": 0,
                "actual_directory_bytes": 0,
                "complete_sealed_epochs": [],
                "gapped_sealed_epochs": [],
                "unsealed_crash_epochs": [],
                "missing_durable_intervals_by_epoch": {},
                "minute_windows_recovered": 0,
                "control_audit_entries_recovered": 0,
                "merged_probe_added_callback_us": BoundedHistogram(LATENCY_BOUNDS_US).summary(),
            }

        ledger_files = sorted(p for p in target.iterdir() if p.is_file() and p.name.startswith("ledger_") and p.name.endswith(".jsonl"))
        valid_records = 0
        corrupt_records = 0
        seq_gap_warnings: List[str] = []

        sealed_by_epoch: Dict[str, Dict[str, Any]] = {}
        latest_cp_by_epoch: Dict[str, Dict[str, Any]] = {}
        minute_by_epoch_seq: Dict[Tuple[str, int], Dict[str, Any]] = {}
        control_audits_by_id: Dict[str, Dict[str, Any]] = {}

        last_seq_by_boot: Dict[str, int] = {}

        for lf in ledger_files:
            prev_sha = "0" * 64
            try:
                raw_lines = lf.read_bytes().splitlines()
            except OSError:
                corrupt_records += 1
                continue

            for line_idx, raw_line in enumerate(raw_lines):
                if not raw_line.strip():
                    continue
                try:
                    rec = json.loads(raw_line.decode("utf-8"))
                    claimed_sha = str(rec.get("record_sha256", ""))
                    claimed_prev = str(rec.get("prev_record_sha256", ""))
                    if claimed_prev != prev_sha:
                        corrupt_records += 1
                        break
                    check_copy = dict(rec)
                    check_copy.pop("record_sha256", None)
                    computed_sha = hashlib.sha256(
                        json.dumps(check_copy, separators=(",", ":"), sort_keys=True).encode("utf-8")
                    ).hexdigest()
                    if not hmac.compare_digest(claimed_sha, computed_sha):
                        corrupt_records += 1
                        break
                except Exception:
                    corrupt_records += 1
                    break

                prev_sha = claimed_sha
                valid_records += 1

                boot_id = str(rec.get("boot_id", ""))
                exp_seq = int(rec.get("export_seq", 0))
                prev_boot_seq = last_seq_by_boot.get(boot_id, 0)
                if prev_boot_seq > 0 and exp_seq != prev_boot_seq + 1:
                    seq_gap_warnings.append(f"{lf.name}:line={line_idx + 1}:expected={prev_boot_seq + 1}:got={exp_seq}")
                if exp_seq > prev_boot_seq:
                    last_seq_by_boot[boot_id] = exp_seq

                kind = str(rec.get("record_kind", ""))
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    continue

                for aud in payload.get("control_audit_delta") or []:
                    if isinstance(aud, dict):
                        aid = f"{boot_id}:{aud.get('audit_seq', len(control_audits_by_id))}"
                        control_audits_by_id[aid] = aud

                if kind == "MINUTE_WINDOW":
                    ep_id = str(payload.get("epoch_id", ""))
                    l_seq = int(payload.get("logical_minute_seq", 0))
                    if ep_id and l_seq > 0:
                        minute_by_epoch_seq[(ep_id, l_seq)] = payload
                elif kind == "PERIODIC_CHECKPOINT":
                    ep_id = str(payload.get("epoch_id", ""))
                    if ep_id:
                        prev_cp = latest_cp_by_epoch.get(ep_id)
                        if prev_cp is None or int(rec.get("export_seq", 0)) >= int(prev_cp.get("_export_seq", 0)):
                            cp_copy = dict(payload)
                            cp_copy["_export_seq"] = exp_seq
                            latest_cp_by_epoch[ep_id] = cp_copy
                elif kind == "EPOCH_SEALED":
                    ep_id = str(payload.get("epoch_id", ""))
                    if ep_id and ep_id not in sealed_by_epoch:
                        sealed_by_epoch[ep_id] = payload

        complete_sealed: List[str] = []
        gapped_sealed: List[str] = []
        missing_by_epoch: Dict[str, List[Dict[str, Any]]] = {}
        merged_hist = BoundedHistogram(LATENCY_BOUNDS_US)

        for ep_id, seal_payload in sealed_by_epoch.items():
            miss_cnt = int(seal_payload.get("missing_durable_intervals_count", 0))
            miss_list = list(seal_payload.get("missing_durable_intervals") or [])
            dur_valid = bool(seal_payload.get("durable_completeness_valid", miss_cnt == 0))
            if miss_cnt > 0 or not dur_valid:
                gapped_sealed.append(ep_id)
                missing_by_epoch[ep_id] = miss_list
            else:
                complete_sealed.append(ep_id)
            lat = (seal_payload.get("latencies_us") or {}).get("probe_added_callback_us")
            if isinstance(lat, dict):
                merged_hist.merge_from_dict(lat)

        unsealed_crash = sorted(set(latest_cp_by_epoch.keys()) - set(sealed_by_epoch.keys()))

        return {
            "files_scanned": len(ledger_files),
            "valid_records": valid_records,
            "corrupt_or_torn_records": corrupt_records,
            "sequence_gap_warnings": seq_gap_warnings,
            "actual_directory_bytes": actual_bytes,
            "complete_sealed_epochs": sorted(complete_sealed),
            "gapped_sealed_epochs": sorted(gapped_sealed),
            "unsealed_crash_epochs": unsealed_crash,
            "missing_durable_intervals_by_epoch": missing_by_epoch,
            "minute_windows_recovered": len(minute_by_epoch_seq),
            "control_audit_entries_recovered": len(control_audits_by_id),
            "control_audit_entries": list(control_audits_by_id.values()),
            "merged_probe_added_callback_us": merged_hist.summary(),
        }


class S1IngressMeasurementProbe:
    """
    Phase 1 (V4) S1 Market-Data Workload Meter.
      - Default-OFF; zero disk I/O during construction.
      - Uses original SDK delivery (`super().handle_message`), zero protobuf re-decode,
        zero per-instrument classification, zero raw-frame queue.
      - Versioned `_control_gen` guarantees `disable` always overrides pending `enable`s.
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

        # `_admin_op_lock` serializes enable/reset/export_flush only; NEVER acquired by `disable()`!
        self._admin_op_lock = threading.Lock()
        # `_state_lock` protects microsecond control-generation transitions (NEVER held across I/O or waits;
        # NEVER acquired by the market-data callback!).
        self._state_lock = threading.Lock()
        self._audit_lock = threading.Lock()

        self._control_gen: int = 0
        self._cancelled_below_gen: int = 0
        self._desired_enabled: bool = False
        self._capture_enabled: bool = False
        self._active_epoch_seq: int = 0
        self._next_epoch_seq: int = 1
        self._callback_trip_seq: int = 0

        self._attachment_verifier: Optional[Callable[[], Dict[str, Any]]] = None
        self._universe_supplier: Optional[Callable[..., Any]] = None  # Kept as no-op shim for compatibility
        self._registered_streamers: List[weakref.ReferenceType[Any]] = []
        self._cached_attachment: Dict[str, Any] = {
            "streamer_present": False,
            "wrapper_attached": False,
            "registered_wrapper_instances": 0,
            "wrapper_version_expected": PROBE_WRAPPER_VERSION,
        }

        self._control_audit_ring: Deque[Dict[str, Any]] = collections.deque(maxlen=MAX_CONTROL_AUDIT_RING)
        self._control_audit_next_seq: int = 1
        self._control_audit_exported_seq: int = 0

        # Lock-free hot-path observation inbox (holds ONLY tiny scalar lists/tuples)
        self._tls = threading.local()
        self._inbox: Deque[Any] = collections.deque()
        self._inbox_overflow_drops: int = 0
        self._stage_event_drops: int = 0
        self._lifecycle_event_drops: int = 0
        self._post_disable_inflight_drops: int = 0
        self._observer_boundary_faults: int = 0
        self._timing_unavailable_total: int = 0

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

        # Coalesced control mailbox (outside `_inbox`)
        self._pending_enable_cmd: Optional[Tuple[int, int, str, int, threading.Event]] = None
        self._pending_seal_cmd: Optional[Tuple[int, str, str, int, threading.Event]] = None
        self._pending_reset_cmd: Optional[Tuple[int, threading.Event]] = None
        self._pending_sync_barrier: Optional[Tuple[bool, List[bool], threading.Event]] = None

        # Worker lifecycle & heartbeat watchdog
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_stop: bool = False
        self._worker_alive_flag: bool = False
        self._worker_dead_latched: bool = False
        self._worker_last_heartbeat_mono_ns: int = 0

        # Single-writer worker aggregator state
        self._init_aggregator_state(epoch_seq=0)
        self._agg_epoch_closed: bool = True
        self._agg_epoch_durable: bool = True
        self._published_snapshot: Dict[str, Any] = self._build_snapshot_from_aggregator(
            include_rings=True, include_hist_bins=False
        )

    def _reset_rolling_breaker(self) -> None:
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

    def _init_aggregator_state(self, epoch_seq: int) -> None:
        self._agg_epoch_seq: int = epoch_seq
        self._agg_epoch_id: str = f"{self._boot_id}-e{epoch_seq:04d}"
        self._agg_epoch_closed: bool = False
        self._agg_epoch_durable: bool = (epoch_seq == 0)
        self._agg_sealed_for_epoch_seq: int = 0

        self._agg_enabled_since_utc: Optional[str] = None
        self._agg_disabled_at_utc: Optional[str] = None
        self._agg_active_start_mono_ns: int = 0
        self._agg_accumulated_enabled_ns: int = 0
        self._agg_disconnected_since_mono_ns: int = 0
        self._agg_accumulated_disconnected_ns: int = 0
        self._agg_connected_state: bool = True
        self._agg_last_disconnect_mono_ns: int = 0
        self._agg_max_disconnect_gap_ms: float = 0.0

        self._agg_control_intervals: Deque[Dict[str, Any]] = collections.deque(maxlen=MAX_CONTROL_INTERVALS)
        self._agg_control_intervals_evicted: int = 0

        # Frame, byte, and feed-count workload totals
        self._agg_ingress_frames_total: int = 0
        self._agg_decoded_ok_frames: int = 0
        self._agg_sdk_or_listener_errors: int = 0
        self._agg_non_bytes_frames: int = 0
        self._agg_wire_bytes_total: int = 0
        self._agg_decoded_feeds_total: int = 0
        self._agg_max_frame_bytes: int = 0
        self._agg_min_frame_bytes: int = 0
        self._agg_max_feeds_per_frame: int = 0
        self._agg_frames_gt_16kib: int = 0
        self._agg_frames_gt_64kib: int = 0
        self._agg_frames_gt_256kib: int = 0
        self._agg_frames_gt_1mib: int = 0
        self._agg_first_frame_mono_ns: int = 0
        self._agg_last_frame_mono_ns: int = 0
        self._agg_timing_unavailable_count: int = 0

        # Fixed monotonic bucket accumulators (`100ms`, `1s`, `60s`)
        self._agg_cur_100ms_slot: int = -1
        self._agg_cur_100ms_frames: int = 0
        self._agg_cur_100ms_bytes: int = 0
        self._agg_cur_100ms_feeds: int = 0
        self._agg_peak_100ms_frames: int = 0
        self._agg_peak_100ms_bytes: int = 0
        self._agg_peak_100ms_feeds: int = 0
        self._agg_confirmed_zero_100ms_buckets: int = 0
        self._agg_ring_100ms: Deque[Dict[str, Any]] = collections.deque(maxlen=RING_100MS_MAXLEN)

        self._agg_cur_1s_slot: int = -1
        self._agg_cur_1s_frames: int = 0
        self._agg_cur_1s_bytes: int = 0
        self._agg_cur_1s_feeds: int = 0
        self._agg_peak_1s_frames: int = 0
        self._agg_peak_1s_bytes: int = 0
        self._agg_peak_1s_feeds: int = 0
        self._agg_confirmed_zero_1s_buckets: int = 0
        self._agg_ring_1s: Deque[Dict[str, Any]] = collections.deque(maxlen=RING_1S_MAXLEN)

        self._agg_cur_1m_slot: int = -1
        self._agg_cur_1m_frames: int = 0
        self._agg_cur_1m_bytes: int = 0
        self._agg_cur_1m_feeds: int = 0
        self._agg_cur_1m_max_bytes: int = 0
        self._agg_logical_minute_seq: int = 0
        self._agg_durable_minute_count: int = 0
        self._agg_ring_1m: Deque[Dict[str, Any]] = collections.deque(maxlen=RING_1M_MAXLEN)
        self._pending_minute_exports: Deque[Dict[str, Any]] = collections.deque()
        self._agg_missing_export_intervals: Deque[Dict[str, Any]] = collections.deque(
            maxlen=MAX_MISSING_EXPORT_INTERVALS
        )
        self._agg_missing_export_intervals_count: int = 0
        self._agg_epoch_export_failures: int = 0

        # Compact histograms
        self._agg_hist_frame_bytes = BoundedHistogram(FRAME_SIZE_BOUNDS_BYTES)
        self._agg_hist_feeds_per_frame = BoundedHistogram(FEEDS_PER_FRAME_BOUNDS)
        self._agg_hist_100ms_nonempty_frames = BoundedHistogram(MSG_RATE_BOUNDS_PER_SEC)
        self._agg_hist_100ms_nonempty_bytes = BoundedHistogram(BYTE_RATE_BOUNDS_PER_SEC)
        self._agg_hist_1s_all_frames = BoundedHistogram(MSG_RATE_BOUNDS_PER_SEC)
        self._agg_hist_1s_all_bytes = BoundedHistogram(BYTE_RATE_BOUNDS_PER_SEC)

        self._agg_hist_probe_added_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_sdk_and_app_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_total_callback_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_listener_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_equity_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_bar_flush_15m_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_freeze_snapshot_us = BoundedHistogram(LATENCY_BOUNDS_US)
        self._agg_hist_worker_slice_us = BoundedHistogram(LATENCY_BOUNDS_US)

        # Lifecycle & errors
        self._agg_ws_open_count: int = 0
        self._agg_ws_close_count: int = 0
        self._agg_ws_error_count: int = 0
        self._agg_ws_reconnecting_count: int = 0
        self._agg_ws_auto_reconnect_stopped_count: int = 0
        self._agg_lifecycle_events: Deque[Dict[str, Any]] = collections.deque(maxlen=LIFECYCLE_RING_MAXLEN)
        self._agg_worker_internal_errors: int = 0
        self._agg_invalidation_reasons: Set[str] = set()

        now_ns = time.perf_counter_ns()
        self._agg_last_checkpoint_ns: int = now_ns
        self._agg_last_snapshot_pub_ns: int = now_ns
        self._agg_last_exported_frames: int = 0

    # ── Registration & Control Audit ───────────────────────────────────────

    def set_attachment_verifier(self, fn: Optional[Callable[[], Dict[str, Any]]]) -> None:
        self._attachment_verifier = fn

    def set_universe_supplier(self, fn: Optional[Callable[..., Any]]) -> None:
        # Retained as no-op compatibility hook; V4 workload meter never polls per-instrument universes.
        self._universe_supplier = fn

    def register_streamer_instance(self, streamer_obj: Any) -> None:
        try:
            self._registered_streamers = [r for r in self._registered_streamers if r() is not None]
            self._registered_streamers.append(weakref.ref(streamer_obj))
        except Exception:
            pass

    def inspect_attachment(self, *, refresh: bool = True) -> Dict[str, Any]:
        live_instances = [r() for r in self._registered_streamers if r() is not None]
        if not refresh:
            cached = dict(self._cached_attachment)
            cached["registered_wrapper_instances"] = len(live_instances)
            return cached
        if self._attachment_verifier is not None:
            try:
                info = dict(self._attachment_verifier())
                info["registered_wrapper_instances"] = len(live_instances)
                self._cached_attachment = dict(info)
                return info
            except Exception as exc:
                err_info = {
                    "wrapper_attached": False,
                    "registered_wrapper_instances": len(live_instances),
                    "verifier_error": f"{type(exc).__name__}:{exc}",
                }
                self._cached_attachment = dict(err_info)
                return err_info
        attached = any(
            getattr(inst, "_s1_probe", None) is self
            and getattr(inst, "_s1_probe_wrapper_version", None) == PROBE_WRAPPER_VERSION
            for inst in live_instances
        )
        fallback_info = {
            "streamer_present": len(live_instances) > 0,
            "wrapper_attached": attached,
            "registered_wrapper_instances": len(live_instances),
            "wrapper_version_expected": PROBE_WRAPPER_VERSION,
        }
        self._cached_attachment = dict(fallback_info)
        return fallback_info

    def record_control_audit(
        self,
        *,
        action: str,
        actor_role: str,
        peer: str,
        outcome: str,
        detail: Optional[str] = None,
    ) -> None:
        with self._audit_lock:
            seq = self._control_audit_next_seq
            self._control_audit_next_seq += 1
            self._control_audit_ring.append(
                {
                    "audit_seq": seq,
                    "ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "action": str(action)[:48],
                    "actor_role": str(actor_role)[:16],
                    "peer": str(peer)[:48],
                    "outcome": str(outcome)[:64],
                    "detail": str(detail)[:160] if detail else None,
                }
            )

    def _peek_unexported_control_audit(self) -> Tuple[List[Dict[str, Any]], int]:
        """Reads unexported audit entries WITHOUT advancing cursor until write_record succeeds."""
        with self._audit_lock:
            unexported = [
                dict(entry)
                for entry in self._control_audit_ring
                if int(entry["audit_seq"]) > self._control_audit_exported_seq
            ]
            new_cursor = int(unexported[-1]["audit_seq"]) if unexported else self._control_audit_exported_seq
            return unexported, new_cursor

    def _commit_exported_control_audit(self, committed_seq: int) -> None:
        with self._audit_lock:
            if committed_seq > self._control_audit_exported_seq:
                self._control_audit_exported_seq = committed_seq

    # ── Worker Lifecycle ───────────────────────────────────────────────────

    def _ensure_worker_started(self) -> None:
        th = self._worker_thread
        if th is not None and th.is_alive() and not self._worker_dead_latched:
            return
        self._worker_stop = False
        self._worker_dead_latched = False
        self._worker_last_heartbeat_mono_ns = time.perf_counter_ns()
        new_th = threading.Thread(
            target=self._worker_loop_guarded,
            name="S1WorkloadMeterWorker",
            daemon=True,
        )
        self._worker_thread = new_th
        new_th.start()

    # ── Admin Control Operations (Disable Dominates Pending Enables!) ──────

    def enable(
        self,
        *,
        actor_role: str = "LOCAL",
        peer: str = "local",
        require_streamer_attached: Optional[bool] = None,
    ) -> Dict[str, Any]:
        req_attached = (
            self._require_streamer_attached_default
            if require_streamer_attached is None
            else bool(require_streamer_attached)
        )
        with self._admin_op_lock:
            with self._state_lock:
                if self._capture_enabled and self._desired_enabled and not self._worker_dead_latched:
                    self.record_control_audit(
                        action="enable", actor_role=actor_role, peer=peer, outcome="NOOP_ALREADY_ENABLED"
                    )
                    return self.get_summary(include_recent_rings=False)
                self._control_gen += 1
                my_gen = self._control_gen
                self._desired_enabled = True
                trip_seq_before = self._callback_trip_seq

            # Attachment verification runs OUTSIDE `_state_lock` so emergency `disable()` never waits!
            att = self.inspect_attachment()
            if req_attached and not att.get("wrapper_attached", False):
                with self._state_lock:
                    if self._control_gen == my_gen:
                        self._desired_enabled = False
                self.record_control_audit(
                    action="enable",
                    actor_role=actor_role,
                    peer=peer,
                    outcome="REJECTED_UNATTACHED",
                    detail=str(att.get("streamer_class")),
                )
                raise ProbeControlError(
                    409,
                    "STREAMER_NOT_INSTRUMENTED",
                    "InstrumentedMarketDataStreamerV3 is not attached to an active streamer instance",
                )

            self._ensure_worker_started()
            en_done = threading.Event()
            en_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
            en_mono = time.perf_counter_ns()

            with self._state_lock:
                # Check if an emergency `disable()` or callback fault trip occurred during attachment/init!
                if (
                    self._control_gen != my_gen
                    or not self._desired_enabled
                    or my_gen <= self._cancelled_below_gen
                    or self._callback_trip_seq != trip_seq_before
                ):
                    self._capture_enabled = False
                    self.record_control_audit(
                        action="enable",
                        actor_role=actor_role,
                        peer=peer,
                        outcome="SUPERSEDED_BY_DISABLE",
                    )
                    return self.get_summary(include_recent_rings=False)

                # Reset fault latches for the fresh epoch before worker flips `_capture_enabled`
                self._auto_disabled_on_fault = False
                self._auto_disabled_reason = None
                self._observer_boundary_faults = 0
                self._inbox_overflow_drops = 0
                self._stage_event_drops = 0
                self._lifecycle_event_drops = 0
                self._timing_unavailable_total = 0
                self._reset_rolling_breaker()

                ep_seq = self._next_epoch_seq
                self._next_epoch_seq += 1
                self._pending_enable_cmd = (my_gen, ep_seq, en_utc, en_mono, en_done)

            en_done.wait(timeout=2.0)
            with self._state_lock:
                actually_enabled = bool(self._capture_enabled and self._control_gen == my_gen)
            self.record_control_audit(
                action="enable",
                actor_role=actor_role,
                peer=peer,
                outcome="ENABLED" if actually_enabled else "SUPERSEDED_BY_DISABLE",
                detail=f"epoch_seq={ep_seq}",
            )
            return self.get_summary(include_recent_rings=False)

    def disable(
        self,
        *,
        actor_role: str = "LOCAL",
        peer: str = "local",
        reason: str = "MANUAL_DISABLE",
        wait_for_seal_s: float = 2.0,
    ) -> Dict[str, Any]:
        """
        Emergency-safe disable:
          - NEVER acquires `_admin_op_lock` (cannot be blocked by a slow/stuck `enable()`).
          - Atomically increments `_control_gen` and `_cancelled_below_gen` so ALL older pending
            enables (both in caller and in worker) are strictly dominated and cannot set capture ON.
        """
        seal_done: Optional[threading.Event] = None
        superseded_en_ev: Optional[threading.Event] = None
        seal_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        seal_mono = time.perf_counter_ns()

        with self._state_lock:
            self._control_gen += 1
            self._cancelled_below_gen = self._control_gen
            self._desired_enabled = False
            self._capture_enabled = False

            if self._pending_enable_cmd is not None:
                superseded_en_ev = self._pending_enable_cmd[4]
                self._pending_enable_cmd = None

            target_ep = self._active_epoch_seq
            if (
                target_ep > 0
                and self._worker_thread is not None
                and self._worker_thread.is_alive()
                and not self._worker_dead_latched
                and self._agg_sealed_for_epoch_seq != target_ep
            ):
                seal_done = threading.Event()
                self._pending_seal_cmd = (target_ep, str(reason)[:64], seal_utc, seal_mono, seal_done)

        if superseded_en_ev is not None:
            superseded_en_ev.set()

        self.record_control_audit(
            action="disable", actor_role=actor_role, peer=peer, outcome="DISABLED", detail=reason
        )

        if seal_done is not None and wait_for_seal_s > 0.0:
            seal_done.wait(timeout=wait_for_seal_s)

        # Never invoke slow/contested attachment_verifier synchronously on emergency disable!
        return self.get_summary(include_recent_rings=False, refresh_attachment=False)

    def reset(self, *, actor_role: str = "LOCAL", peer: str = "local") -> Dict[str, Any]:
        with self._admin_op_lock:
            with self._state_lock:
                if self._capture_enabled or self._desired_enabled:
                    self.record_control_audit(
                        action="reset", actor_role=actor_role, peer=peer, outcome="REJECTED_WHILE_ENABLED"
                    )
                    raise ProbeControlError(
                        409,
                        "CANNOT_RESET_WHILE_ENABLED",
                        "Disable probe and seal active epoch before resetting counters",
                    )

            # Synchronize worker state before inspecting durability
            self._worker_sync_barrier(force_export=False)
            if self._agg_ingress_frames_total > 0 and not self._agg_epoch_durable:
                self.record_control_audit(
                    action="reset", actor_role=actor_role, peer=peer, outcome="REJECTED_UNEXPORTED_EPOCH"
                )
                raise ProbeControlError(
                    409,
                    "UNEXPORTED_EPOCH_CANNOT_RESET",
                    "Active/closed epoch has unexported observations; refuse destructive reset",
                )

            if self._worker_thread is not None and self._worker_thread.is_alive() and not self._worker_dead_latched:
                rst_done = threading.Event()
                self._pending_reset_cmd = (self._active_epoch_seq, rst_done)
                rst_done.wait(timeout=2.0)
            else:
                self._init_aggregator_state(epoch_seq=self._active_epoch_seq)
                self._agg_epoch_closed = True
                self._agg_epoch_durable = True
                self._published_snapshot = self._build_snapshot_from_aggregator(
                    include_rings=True, include_hist_bins=False
                )

            self._auto_disabled_on_fault = False
            self._auto_disabled_reason = None
            self._observer_boundary_faults = 0
            self._inbox_overflow_drops = 0
            self._stage_event_drops = 0
            self._lifecycle_event_drops = 0
            self._post_disable_inflight_drops = 0
            self._timing_unavailable_total = 0
            self._reset_rolling_breaker()

            self.record_control_audit(action="reset", actor_role=actor_role, peer=peer, outcome="RESET_OK")
            return self.get_summary(include_recent_rings=False)

    def _worker_sync_barrier(self, *, force_export: bool, timeout_s: float = 2.0) -> Tuple[bool, bool]:
        """Returns `(barrier_completed, export_succeeded)` distinctly."""
        th = self._worker_thread
        if th is None or not th.is_alive() or self._worker_dead_latched:
            if not force_export:
                return True, True
            return True, False
        res_box = [not force_export]
        done_ev = threading.Event()
        self._pending_sync_barrier = (bool(force_export), res_box, done_ev)
        completed = done_ev.wait(timeout=timeout_s)
        return bool(completed), bool(completed and res_box[0])

    def export_summary(self, *, actor_role: str = "LOCAL", peer: str = "local") -> Dict[str, Any]:
        with self._admin_op_lock:
            self.record_control_audit(
                action="export_flush", actor_role=actor_role, peer=peer, outcome="REQUESTED"
            )
            if not self._worker_dead_latched:
                self._ensure_worker_started()
            barrier_ok, export_ok = self._worker_sync_barrier(force_export=True, timeout_s=2.5)
            self.record_control_audit(
                action="export_flush",
                actor_role=actor_role,
                peer=peer,
                outcome="EXPORT_OK" if export_ok else "EXPORT_FAILED",
            )
            snap = self.get_summary(include_recent_rings=False)
            snap["barrier_completed"] = barrier_ok
            snap["export_succeeded"] = export_ok
            snap["flushed"] = bool(barrier_ok and export_ok)
            return snap

    def shutdown_flush(self, timeout_s: float = 2.0) -> Dict[str, Any]:
        if self._capture_enabled or self._desired_enabled or (
            self._agg_epoch_seq > 0 and self._agg_sealed_for_epoch_seq != self._agg_epoch_seq
        ):
            self.disable(actor_role="SYSTEM", peer="shutdown", reason="PROCESS_SHUTDOWN", wait_for_seal_s=timeout_s)
        self._worker_stop = True
        th = self._worker_thread
        if th is not None and th.is_alive():
            th.join(timeout=timeout_s)
        return self.get_summary(include_recent_rings=False)

    # ── Read-Only Status & Live Health Overlay ─────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(self._capture_enabled)

    def flush_for_test_or_export(self, timeout_s: float = 2.0, force_export: bool = True) -> bool:
        """Synchronizes pending worker observations; when `force_export=True`, returns True ONLY if durable export succeeded."""
        barrier_ok, export_ok = self._worker_sync_barrier(force_export=force_export, timeout_s=timeout_s)
        return bool(barrier_ok and export_ok) if force_export else bool(barrier_ok)

    def get_public_minimal_status(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self._capture_enabled),
            "auto_disabled_on_fault": bool(self._auto_disabled_on_fault),
        }

    def get_summary(self, include_recent_rings: bool = False, *, refresh_attachment: Optional[bool] = None) -> Dict[str, Any]:
        snap = copy.deepcopy(self._published_snapshot)
        if not include_recent_rings:
            snap.pop("recent_1m_buckets", None)
            snap.pop("recent_1s_buckets", None)
            snap.pop("recent_100ms_buckets", None)
        do_refresh = (not self._admin_op_lock.locked()) if refresh_attachment is None else bool(refresh_attachment)
        return self._overlay_live_health(snap, refresh_attachment=do_refresh)

    def _overlay_live_health(self, snap: Dict[str, Any], *, refresh_attachment: bool = True) -> Dict[str, Any]:
        now_ns = time.perf_counter_ns()
        snap["enabled"] = bool(self._capture_enabled)
        snap["attachment"] = self.inspect_attachment(refresh=refresh_attachment)

        worker_stalled = bool(
            self._capture_enabled
            and self._worker_last_heartbeat_mono_ns > 0
            and (now_ns - self._worker_last_heartbeat_mono_ns) > WORKER_STALL_TIMEOUT_NS
        )
        worker_alive = bool(
            self._worker_thread is not None
            and self._worker_thread.is_alive()
            and not self._worker_dead_latched
            and not worker_stalled
        )

        fb = snap.setdefault("failures_and_bounds", {})
        fb["worker_alive"] = worker_alive
        fb["worker_dead_latched"] = bool(self._worker_dead_latched)
        fb["worker_heartbeat_stalled"] = worker_stalled
        fb["auto_disabled_on_fault"] = bool(self._auto_disabled_on_fault)
        fb["auto_disabled_reason"] = self._auto_disabled_reason
        fb["observer_boundary_faults"] = int(self._observer_boundary_faults)
        fb["inbox_pending_samples"] = len(self._inbox)
        fb["inbox_overflow_drops"] = int(self._inbox_overflow_drops)
        fb["stage_event_drops"] = int(self._stage_event_drops)
        fb["lifecycle_event_drops"] = int(self._lifecycle_event_drops)
        fb["post_disable_inflight_drops"] = int(self._post_disable_inflight_drops)
        fb["timing_unavailable_count"] = max(
            int(fb.get("timing_unavailable_count", 0)), int(self._timing_unavailable_total)
        )

        reasons: Set[str] = set(snap.get("workload_meter_qualification", {}).get("invalidation_reasons") or [])
        if self._auto_disabled_on_fault:
            reasons.add(f"AUTO_DISABLED:{self._auto_disabled_reason or 'UNKNOWN'}")
        if self._observer_boundary_faults > 0:
            reasons.add("OBSERVER_BOUNDARY_FAULT")
        if self._inbox_overflow_drops > 0:
            reasons.add("INBOX_OVERFLOW_DROP")
        if self._stage_event_drops > 0:
            reasons.add("STAGE_EVENT_DROP")
        if self._lifecycle_event_drops > 0:
            reasons.add("LIFECYCLE_EVENT_DROP")
        if self._timing_unavailable_total > 0:
            reasons.add("TIMING_SAMPLE_UNAVAILABLE")
        if self._worker_dead_latched or worker_stalled:
            reasons.add("WORKER_DEAD_OR_STALLED")

        operating_clean = (len(reasons - {"DURABLE_EXPORT_GAP"}) == 0)
        meas_valid = operating_clean
        dur_valid = bool(
            snap.get("workload_meter_qualification", {}).get("durable_completeness_valid", True)
            and "DURABLE_EXPORT_GAP" not in reasons
        )

        wmq = snap.setdefault("workload_meter_qualification", {})
        wmq["operating_health_clean"] = operating_clean
        wmq["measurement_integrity_valid"] = meas_valid
        wmq["durable_completeness_valid"] = dur_valid
        wmq["invalidation_reasons"] = sorted(reasons)

        icb = wmq.setdefault("internal_callback_budget", {})
        sample_cnt = int(icb.get("sample_count", 0))
        p99_ub = float(icb.get("probe_added_p99_upper_bound_us", 0.0))
        p999_ub = float(icb.get("probe_added_p999_upper_bound_us", 0.0))
        raw_within = (
            sample_cnt >= 1
            and p99_ub <= LATENCY_P99_BUDGET_US
            and p999_ub <= LATENCY_SINGLE_BREACH_US
        )
        if sample_cnt == 0:
            icb["status"] = "INSUFFICIENT_SAMPLES"
            icb["within_budget_and_healthy"] = False
        elif not meas_valid:
            icb["status"] = "UNQUALIFIED_HEALTH_DEGRADED"
            icb["within_budget_and_healthy"] = False
        elif raw_within:
            icb["status"] = "WITHIN_INTERNAL_CALLBACK_BUDGET"
            icb["within_budget_and_healthy"] = True
        else:
            icb["status"] = "EXCEEDED_INTERNAL_CALLBACK_BUDGET"
            icb["within_budget_and_healthy"] = False

        snap["durable_exporter"] = self._exporter.status_snapshot(now_ns)
        snap["durable_exporter"]["tail_state"] = wmq.get("tail_state", "IDLE_EMPTY_EPOCH")
        snap["durable_exporter"]["missing_durable_intervals_count"] = int(
            wmq.get("missing_durable_intervals_count", 0)
        )
        return snap

    # ── Lock-Free Callback Hot Path (Zero Locks, Zero Event.set, Zero Copies) ──

    def _trip_auto_disable_from_callback(self, reason: str) -> None:
        # Lock-free attribute updates under CPython GIL; never acquires `_state_lock`!
        self._callback_trip_seq += 1
        self._capture_enabled = False
        self._desired_enabled = False
        self._auto_disabled_on_fault = True
        self._auto_disabled_reason = str(reason)[:96]

    def _check_worker_stall_in_callback(self) -> bool:
        if self._worker_dead_latched:
            self._trip_auto_disable_from_callback("WORKER_DEAD")
            return True
        hb = self._worker_last_heartbeat_mono_ns
        if hb > 0 and (time.perf_counter_ns() - hb) > WORKER_STALL_TIMEOUT_NS:
            self._trip_auto_disable_from_callback("WORKER_HEARTBEAT_STALL")
            return True
        return False

    def _check_rolling_latency_breaker(self, full_added_ns: int) -> None:
        added_us = full_added_ns / 1000.0
        if added_us > LATENCY_SEVERE_STALL_US:
            self._severe_latency_stalls += 1
            if self._severe_latency_stalls >= MAX_SEVERE_LATENCY_STALLS:
                self._trip_auto_disable_from_callback(
                    f"SEVERE_CALLBACK_STALL:{added_us:.1f}us"
                )
                return

        if added_us > LATENCY_SINGLE_BREACH_US:
            self._consecutive_latency_breaches += 1
            if self._consecutive_latency_breaches >= MAX_CONSECUTIVE_LATENCY_BREACHES:
                self._trip_auto_disable_from_callback(
                    f"CONSECUTIVE_LATENCY_BREACH:{added_us:.1f}us"
                )
                return
        else:
            self._consecutive_latency_breaches = 0

        gt100 = 1 if added_us > LATENCY_P99_BUDGET_US else 0
        gt1000 = 1 if added_us > LATENCY_SINGLE_BREACH_US else 0
        idx = self._rolling_idx
        if self._rolling_count == ROLLING_LATENCY_WINDOW:
            self._rolling_gt100_sum -= self._rolling_gt100_ring[idx]
            self._rolling_gt1000_sum -= self._rolling_gt1000_ring[idx]
        else:
            self._rolling_count += 1
        self._rolling_gt100_ring[idx] = gt100
        self._rolling_gt1000_ring[idx] = gt1000
        self._rolling_gt100_sum += gt100
        self._rolling_gt1000_sum += gt1000
        self._rolling_idx = (idx + 1) % ROLLING_LATENCY_WINDOW

        if self._rolling_count >= MIN_ROLLING_SAMPLES_FOR_P99_TRIP:
            # p99 > 100us when > 1% of window exceeds 100us (e.g. >= 3 out of 256)
            max_allowed_gt100 = max(1, int(math.floor(0.01 * self._rolling_count)))
            if self._rolling_gt100_sum > max_allowed_gt100:
                self._trip_auto_disable_from_callback(
                    f"ROLLING_P99_BREACH:{self._rolling_gt100_sum}/{self._rolling_count}>100us"
                )
                return
            if self._rolling_count >= ROLLING_LATENCY_WINDOW and self._rolling_gt1000_sum >= 2:
                self._trip_auto_disable_from_callback(
                    f"ROLLING_P999_BREACH:{self._rolling_gt1000_sum}/{self._rolling_count}>1000us"
                )

    def _clear_tls_stage_segments(self) -> None:
        self._tls.stage_seg = None

    def record_downstream_segments(
        self,
        listener_ns: int,
        equity_ingest_ns: int,
        *,
        hook_overhead_ns: int = 0,
    ) -> None:
        if not self._capture_enabled:
            return
        t0 = time.perf_counter_ns()
        l_ns = max(0, int(listener_ns))
        e_ns = max(0, int(equity_ingest_ns))
        hk_ns = max(0, int(hook_overhead_ns)) + max(0, time.perf_counter_ns() - t0)
        self._tls.stage_seg = (l_ns, e_ns, hk_ns)

    def record_periodic_decision_stage(self, stage_name: str, duration_ns: int) -> None:
        if not self._capture_enabled:
            return
        if len(self._inbox) >= MAX_INBOX_SAMPLES:
            self._stage_event_drops += 1
            return
        self._inbox.append((_EVT_PERIODIC_STAGE, self._active_epoch_seq, str(stage_name), max(0, int(duration_ns))))

    def record_lifecycle_event(self, event_name: str, detail: Any = None) -> None:
        if not self._capture_enabled:
            return
        if len(self._inbox) >= MAX_INBOX_SAMPLES:
            self._lifecycle_event_drops += 1
            return
        self._inbox.append(
            (_EVT_LIFECYCLE, self._active_epoch_seq, str(event_name), detail, time.perf_counter_ns())
        )

    def note_observer_fault(self, where: str, exc: BaseException) -> None:
        self._observer_boundary_faults += 1
        self._consecutive_callback_faults += 1
        if self._consecutive_callback_faults >= MAX_CONSECUTIVE_PROBE_FAULTS:
            self._trip_auto_disable_from_callback(f"OBSERVER_FAULT:{where}:{type(exc).__name__}")

    def on_ingress_frame_ok(
        self,
        *,
        mono_ns: int,
        message_obj: Any,
        decoded_feeds_len: int,
        sdk_and_app_ns: int,
        cb_entry_ns: int,
    ) -> None:
        if not self._capture_enabled:
            return
        if self._check_worker_stall_in_callback():
            return

        if isinstance(message_obj, (bytes, bytearray, memoryview)):
            wire_len = len(message_obj)
            non_bytes = 0
        else:
            wire_len = 0
            non_bytes = 1

        seg = getattr(self._tls, "stage_seg", None)
        self._tls.stage_seg = None
        if seg is not None:
            listener_ns, equity_ns, stage_hk_ns = seg
        else:
            listener_ns, equity_ns, stage_hk_ns = -1, -1, 0

        net_sdk_and_app_ns = max(0, int(sdk_and_app_ns) - stage_hk_ns)
        ep_seq = self._active_epoch_seq

        if len(self._inbox) >= MAX_INBOX_SAMPLES:
            self._inbox_overflow_drops += 1
            t_cb_exit = time.perf_counter_ns()
            full_added_ns = max(0, (t_cb_exit - cb_entry_ns) - net_sdk_and_app_ns)
            self._check_rolling_latency_breaker(full_added_ns)
            return

        # Index 8 (`ready_added_ns`) starts at `-1` (NOT READY) so consumer can NEVER read a fake `0.0 us`!
        cell = [
            _EVT_FRAME_OK,
            ep_seq,
            int(mono_ns),
            int(wire_len),
            int(non_bytes),
            max(0, int(decoded_feeds_len)),
            net_sdk_and_app_ns,
            listener_ns,
            equity_ns,
            -1,
        ]
        self._inbox.append(cell)
        t_cb_exit = time.perf_counter_ns()
        full_added_ns = max(0, (t_cb_exit - cb_entry_ns) - net_sdk_and_app_ns)
        cell[9] = full_added_ns  # Atomic single-slot readiness publication (`>= 0`)
        self._consecutive_callback_faults = 0
        self._check_rolling_latency_breaker(full_added_ns)

    def on_ingress_frame_error(
        self,
        *,
        mono_ns: int,
        message_obj: Any,
        sdk_elapsed_ns: int,
        cb_entry_ns: int,
    ) -> None:
        if not self._capture_enabled:
            return
        if self._check_worker_stall_in_callback():
            return
        wire_len = len(message_obj) if isinstance(message_obj, (bytes, bytearray, memoryview)) else 0
        non_bytes = 0 if isinstance(message_obj, (bytes, bytearray, memoryview)) else 1
        if len(self._inbox) >= MAX_INBOX_SAMPLES:
            self._inbox_overflow_drops += 1
            return
        cell = [
            _EVT_FRAME_ERR,
            self._active_epoch_seq,
            int(mono_ns),
            int(wire_len),
            int(non_bytes),
            max(0, int(sdk_elapsed_ns)),
            -1,
        ]
        self._inbox.append(cell)
        t_cb_exit = time.perf_counter_ns()
        cell[6] = max(0, (t_cb_exit - cb_entry_ns) - max(0, int(sdk_elapsed_ns)))

    # ── Infrequent Background Scalar Worker & Durable Exporter ─────────────

    def _worker_loop_guarded(self) -> None:
        self._worker_alive_flag = True
        try:
            self._worker_loop_inner()
        except BaseException as fatal_exc:
            self._worker_dead_latched = True
            self._trip_auto_disable_from_callback(f"WORKER_FATAL:{type(fatal_exc).__name__}")
            self._agg_invalidation_reasons.add("WORKER_TERMINATED")
            try:
                self._published_snapshot = self._build_snapshot_from_aggregator(
                    include_rings=True, include_hist_bins=False
                )
            except Exception:
                pass
        finally:
            self._worker_alive_flag = False
            for attr in ("_pending_enable_cmd", "_pending_seal_cmd", "_pending_reset_cmd", "_pending_sync_barrier"):
                cmd = getattr(self, attr, None)
                if cmd is not None:
                    setattr(self, attr, None)
                    ev = cmd[-1]
                    if isinstance(ev, threading.Event):
                        ev.set()

    def _worker_loop_inner(self) -> None:
        while not self._worker_stop:
            self._worker_last_heartbeat_mono_ns = time.perf_counter_ns()

            # 1. Handle enable transition (strictly guarded by `_control_gen` disable dominance!)
            en_cmd = self._pending_enable_cmd
            if en_cmd is not None:
                self._pending_enable_cmd = None
                cmd_gen, ep_seq, en_utc, en_mono, en_done = en_cmd
                self._init_aggregator_state(epoch_seq=ep_seq)
                with self._state_lock:
                    if (
                        self._control_gen == cmd_gen
                        and self._desired_enabled
                        and cmd_gen > self._cancelled_below_gen
                        and not self._auto_disabled_on_fault
                    ):
                        self._active_epoch_seq = ep_seq
                        self._agg_epoch_closed = False
                        self._agg_epoch_durable = False
                        self._agg_enabled_since_utc = en_utc
                        self._agg_active_start_mono_ns = en_mono
                        self._agg_control_intervals.append({"enabled_utc": en_utc, "disabled_utc": None})
                        self._capture_enabled = True
                    else:
                        # A concurrent `disable()` dominated this pending enable!
                        self._capture_enabled = False
                        self._agg_epoch_closed = True
                        self._agg_epoch_durable = True
                self._worker_publish_snapshot()
                en_done.set()

            # 2. Drain scalar observations
            processed = self._worker_drain_batch(drain_all=False, force_finalize=False)

            # 3. Retry/flush any completed 60s minute buckets asynchronously
            if self._pending_minute_exports:
                self._worker_flush_pending_minutes(at_seal=False)

            # 4. Handle sync / force-export barrier (`export_flush` reports actual export success!)
            sync_cmd = self._pending_sync_barrier
            if sync_cmd is not None:
                self._pending_sync_barrier = None
                force_exp, res_box, sync_done = sync_cmd
                self._worker_drain_batch(drain_all=True, force_finalize=False)
                if force_exp:
                    min_ok = self._worker_flush_pending_minutes(at_seal=False)
                    if self._agg_epoch_seq > 0:
                        cp_ok = self._worker_export_summary_record("PERIODIC_CHECKPOINT")
                        res_box[0] = bool(min_ok and cp_ok)
                    else:
                        # Even before first epoch, verify exporter write health on explicit flush
                        cp_ok = self._worker_export_summary_record("PERIODIC_CHECKPOINT")
                        res_box[0] = bool(min_ok and cp_ok)
                else:
                    res_box[0] = True
                self._worker_publish_snapshot()
                sync_done.set()

            # 5. Handle idempotent epoch seal (`disable`)
            seal_cmd = self._pending_seal_cmd
            if seal_cmd is not None:
                self._pending_seal_cmd = None
                target_ep, reason, seal_utc, seal_mono, seal_done = seal_cmd
                self._worker_drain_batch(drain_all=True, force_finalize=True)
                if (
                    target_ep == self._agg_epoch_seq
                    and target_ep > 0
                    and self._agg_sealed_for_epoch_seq != target_ep
                ):
                    if self._agg_active_start_mono_ns > 0 and seal_mono > self._agg_active_start_mono_ns:
                        self._agg_accumulated_enabled_ns += (seal_mono - self._agg_active_start_mono_ns)
                        self._agg_active_start_mono_ns = 0
                    if self._agg_disconnected_since_mono_ns > 0 and seal_mono > self._agg_disconnected_since_mono_ns:
                        self._agg_accumulated_disconnected_ns += (seal_mono - self._agg_disconnected_since_mono_ns)
                        self._agg_disconnected_since_mono_ns = 0
                    self._agg_disabled_at_utc = seal_utc
                    self._agg_epoch_closed = True
                    if self._agg_control_intervals and self._agg_control_intervals[-1]["disabled_utc"] is None:
                        self._agg_control_intervals[-1]["disabled_utc"] = seal_utc
                        self._agg_control_intervals[-1]["reason"] = reason

                    self._worker_finalize_active_buckets()
                    self._worker_flush_pending_minutes(at_seal=True)

                    seal_ok = self._worker_export_summary_record("EPOCH_SEALED", {"seal_reason": reason})
                    if seal_ok or self._exporter.configured_dir is None:
                        self._agg_sealed_for_epoch_seq = target_ep
                        self._agg_epoch_durable = True
                    else:
                        self._agg_epoch_durable = False
                    self._worker_publish_snapshot()
                seal_done.set()

            # 6. Handle reset (only after verified durable seal or empty epoch)
            rst_cmd = self._pending_reset_cmd
            if rst_cmd is not None:
                self._pending_reset_cmd = None
                _ep, rst_done = rst_cmd
                if self._agg_epoch_durable or self._agg_ingress_frames_total == 0:
                    self._init_aggregator_state(epoch_seq=self._active_epoch_seq)
                    self._agg_epoch_closed = True
                    self._agg_epoch_durable = True
                    self._worker_publish_snapshot()
                rst_done.set()

            # 7. Periodic checkpoint & 1s published snapshot refresh
            now_ns = time.perf_counter_ns()
            if (
                self._capture_enabled
                and self._agg_epoch_seq > 0
                and not self._agg_epoch_closed
                and (now_ns - self._agg_last_checkpoint_ns) >= DEFAULT_EXPORT_INTERVAL_NS
            ):
                self._agg_last_checkpoint_ns = now_ns
                self._worker_flush_pending_minutes(at_seal=False)
                self._worker_export_summary_record("PERIODIC_CHECKPOINT")

            if (processed > 0 or self._capture_enabled) and (
                now_ns - self._agg_last_snapshot_pub_ns
            ) >= SNAPSHOT_PUBLISH_INTERVAL_NS:
                self._worker_publish_snapshot()

            qlen = len(self._inbox)
            if self._pending_enable_cmd is not None or self._pending_seal_cmd is not None or self._pending_sync_barrier is not None:
                continue
            if qlen >= WORKER_MAX_BATCH_ITEMS:
                time.sleep(WORKER_BACKLOG_SLEEP_S)
            else:
                time.sleep(WORKER_POLL_SLEEP_S)

    def _worker_drain_batch(self, *, drain_all: bool, force_finalize: bool) -> int:
        if not self._inbox:
            return 0
        t_slice0 = time.perf_counter_ns()
        processed = 0
        limit = MAX_INBOX_SAMPLES + 64 if drain_all else WORKER_MAX_BATCH_ITEMS

        while self._inbox and processed < limit:
            head = self._inbox[0]
            kind = head[0]
            now_ns = time.perf_counter_ns()

            # Checked readiness marker without locks or busy-waiting:
            if kind == _EVT_FRAME_OK and head[9] < 0:
                if force_finalize:
                    time.sleep(0.001)
                if head[9] < 0:
                    age_ns = now_ns - int(head[2])
                    if not force_finalize and age_ns < UNREADY_CELL_GRACE_NS:
                        # Producer is still completing the 2-line post-append tail; wait for next tick!
                        break
            elif kind == _EVT_FRAME_ERR and head[6] < 0:
                if force_finalize:
                    time.sleep(0.001)
                if head[6] < 0 and not force_finalize and (now_ns - int(head[2])) < UNREADY_CELL_GRACE_NS:
                    break

            item = self._inbox.popleft()
            processed += 1

            # Allow white-box direct worker unit tests when `_worker_thread` is None and `_agg_epoch_seq == 0`
            item_ep = int(item[1])
            if self._worker_thread is None and self._agg_epoch_seq == 0 and item_ep >= 0:
                self._agg_epoch_seq = item_ep
                self._agg_epoch_closed = False

            if item_ep != self._agg_epoch_seq or self._agg_epoch_closed:
                self._post_disable_inflight_drops += 1
                continue

            if kind == _EVT_FRAME_OK:
                self._worker_apply_ok_frame(item)
            elif kind == _EVT_FRAME_ERR:
                self._worker_apply_err_frame(item)
            elif kind == _EVT_PERIODIC_STAGE:
                _, _, stg, dur_ns = item
                if stg == "bar_flush_15m":
                    self._agg_hist_bar_flush_15m_us.observe(dur_ns / 1000.0)
                elif stg == "freeze_snapshot":
                    self._agg_hist_freeze_snapshot_us.observe(dur_ns / 1000.0)
            elif kind == _EVT_LIFECYCLE:
                _, _, ev, detail, mono_ns = item
                self._worker_apply_lifecycle(ev, detail, mono_ns)

        if processed > 0:
            self._agg_hist_worker_slice_us.observe((time.perf_counter_ns() - t_slice0) / 1000.0)
        return processed

    def _worker_advance_time_buckets(self, mono_ns: int, wire_len: int, feeds_len: int) -> None:
        # 1. 100ms bucket (`O(1)` gap math, zero loops)
        s100 = mono_ns // 100_000_000
        if self._agg_cur_100ms_slot < 0:
            self._agg_cur_100ms_slot = s100
            self._agg_cur_100ms_frames = 1
            self._agg_cur_100ms_bytes = wire_len
            self._agg_cur_100ms_feeds = feeds_len
        elif s100 == self._agg_cur_100ms_slot:
            self._agg_cur_100ms_frames += 1
            self._agg_cur_100ms_bytes += wire_len
            self._agg_cur_100ms_feeds += feeds_len
        elif s100 > self._agg_cur_100ms_slot:
            self._flush_100ms_bucket_in_mem()
            gap100 = s100 - self._agg_cur_100ms_slot - 1
            if gap100 > 0:
                self._agg_confirmed_zero_100ms_buckets += gap100
            self._agg_cur_100ms_slot = s100
            self._agg_cur_100ms_frames = 1
            self._agg_cur_100ms_bytes = wire_len
            self._agg_cur_100ms_feeds = feeds_len
        else:
            self._agg_cur_100ms_frames += 1
            self._agg_cur_100ms_bytes += wire_len
            self._agg_cur_100ms_feeds += feeds_len

        if self._agg_cur_100ms_frames > self._agg_peak_100ms_frames:
            self._agg_peak_100ms_frames = self._agg_cur_100ms_frames
        if self._agg_cur_100ms_bytes > self._agg_peak_100ms_bytes:
            self._agg_peak_100ms_bytes = self._agg_cur_100ms_bytes
        if self._agg_cur_100ms_feeds > self._agg_peak_100ms_feeds:
            self._agg_peak_100ms_feeds = self._agg_cur_100ms_feeds

        # 2. 1s rate bucket (`O(1)` `observe_many` for zero-gap seconds)
        s1 = mono_ns // 1_000_000_000
        if self._agg_cur_1s_slot < 0:
            self._agg_cur_1s_slot = s1
            self._agg_cur_1s_frames = 1
            self._agg_cur_1s_bytes = wire_len
            self._agg_cur_1s_feeds = feeds_len
        elif s1 == self._agg_cur_1s_slot:
            self._agg_cur_1s_frames += 1
            self._agg_cur_1s_bytes += wire_len
            self._agg_cur_1s_feeds += feeds_len
        elif s1 > self._agg_cur_1s_slot:
            self._flush_1s_bucket_in_mem()
            gap1s = s1 - self._agg_cur_1s_slot - 1
            if gap1s > 0:
                self._agg_confirmed_zero_1s_buckets += gap1s
                self._agg_hist_1s_all_frames.observe_many(0.0, gap1s)
                self._agg_hist_1s_all_bytes.observe_many(0.0, gap1s)
            self._agg_cur_1s_slot = s1
            self._agg_cur_1s_frames = 1
            self._agg_cur_1s_bytes = wire_len
            self._agg_cur_1s_feeds = feeds_len
        else:
            self._agg_cur_1s_frames += 1
            self._agg_cur_1s_bytes += wire_len
            self._agg_cur_1s_feeds += feeds_len

        if self._agg_cur_1s_frames > self._agg_peak_1s_frames:
            self._agg_peak_1s_frames = self._agg_cur_1s_frames
        if self._agg_cur_1s_bytes > self._agg_peak_1s_bytes:
            self._agg_peak_1s_bytes = self._agg_cur_1s_bytes
        if self._agg_cur_1s_feeds > self._agg_peak_1s_feeds:
            self._agg_peak_1s_feeds = self._agg_cur_1s_feeds

        # 3. 60s minute bucket (queued for bounded durable export on completion)
        s60 = mono_ns // 60_000_000_000
        if self._agg_cur_1m_slot < 0:
            self._agg_cur_1m_slot = s60
            self._agg_cur_1m_frames = 1
            self._agg_cur_1m_bytes = wire_len
            self._agg_cur_1m_feeds = feeds_len
            self._agg_cur_1m_max_bytes = wire_len
        elif s60 == self._agg_cur_1m_slot:
            self._agg_cur_1m_frames += 1
            self._agg_cur_1m_bytes += wire_len
            self._agg_cur_1m_feeds += feeds_len
            if wire_len > self._agg_cur_1m_max_bytes:
                self._agg_cur_1m_max_bytes = wire_len
        elif s60 > self._agg_cur_1m_slot:
            self._flush_1m_bucket_to_export_queue()
            self._agg_cur_1m_slot = s60
            self._agg_cur_1m_frames = 1
            self._agg_cur_1m_bytes = wire_len
            self._agg_cur_1m_feeds = feeds_len
            self._agg_cur_1m_max_bytes = wire_len
        else:
            self._agg_cur_1m_frames += 1
            self._agg_cur_1m_bytes += wire_len
            self._agg_cur_1m_feeds += feeds_len
            if wire_len > self._agg_cur_1m_max_bytes:
                self._agg_cur_1m_max_bytes = wire_len

    def _flush_100ms_bucket_in_mem(self) -> None:
        if self._agg_cur_100ms_slot < 0 or self._agg_cur_100ms_frames <= 0:
            return
        self._agg_hist_100ms_nonempty_frames.observe(float(self._agg_cur_100ms_frames))
        self._agg_hist_100ms_nonempty_bytes.observe(float(self._agg_cur_100ms_bytes))
        self._agg_ring_100ms.append(
            {
                "slot_100ms": self._agg_cur_100ms_slot,
                "frames": self._agg_cur_100ms_frames,
                "wire_bytes": self._agg_cur_100ms_bytes,
                "decoded_feeds": self._agg_cur_100ms_feeds,
            }
        )

    def _flush_1s_bucket_in_mem(self) -> None:
        if self._agg_cur_1s_slot < 0 or self._agg_cur_1s_frames <= 0:
            return
        self._agg_hist_1s_all_frames.observe(float(self._agg_cur_1s_frames))
        self._agg_hist_1s_all_bytes.observe(float(self._agg_cur_1s_bytes))
        self._agg_ring_1s.append(
            {
                "slot_1s": self._agg_cur_1s_slot,
                "frames": self._agg_cur_1s_frames,
                "wire_bytes": self._agg_cur_1s_bytes,
                "decoded_feeds": self._agg_cur_1s_feeds,
            }
        )

    def _flush_1m_bucket_to_export_queue(self) -> None:
        if self._agg_cur_1m_slot < 0 or self._agg_cur_1m_frames <= 0:
            return
        self._agg_logical_minute_seq += 1
        rec = {
            "epoch_id": self._agg_epoch_id,
            "epoch_seq": self._agg_epoch_seq,
            "logical_minute_seq": self._agg_logical_minute_seq,
            "minute_slot": self._agg_cur_1m_slot,
            "frames": self._agg_cur_1m_frames,
            "wire_bytes": self._agg_cur_1m_bytes,
            "max_frame_bytes": self._agg_cur_1m_max_bytes,
            "decoded_feeds": self._agg_cur_1m_feeds,
        }
        self._agg_ring_1m.append(rec)
        if self._exporter.configured_dir is not None:
            if len(self._pending_minute_exports) >= MAX_PENDING_MINUTE_EXPORTS:
                evicted = self._pending_minute_exports.popleft()
                self._record_missing_minute_interval(evicted, "RETRY_QUEUE_CAPACITY_EVICTED")
            self._pending_minute_exports.append(rec)
        self._agg_cur_1m_slot = -1
        self._agg_cur_1m_frames = 0
        self._agg_cur_1m_bytes = 0
        self._agg_cur_1m_feeds = 0
        self._agg_cur_1m_max_bytes = 0

    def _worker_finalize_active_buckets(self) -> None:
        self._flush_100ms_bucket_in_mem()
        self._agg_cur_100ms_slot = -1
        self._agg_cur_100ms_frames = 0
        self._flush_1s_bucket_in_mem()
        self._agg_cur_1s_slot = -1
        self._agg_cur_1s_frames = 0
        self._flush_1m_bucket_to_export_queue()

    def _record_missing_minute_interval(self, min_rec: Dict[str, Any], reason: str) -> None:
        self._agg_missing_export_intervals_count += 1
        self._agg_invalidation_reasons.add("DURABLE_EXPORT_GAP")
        self._agg_missing_export_intervals.append(
            {
                "logical_minute_seq": int(min_rec.get("logical_minute_seq", 0)),
                "minute_slot": int(min_rec.get("minute_slot", -1)),
                "frames": int(min_rec.get("frames", 0)),
                "wire_bytes": int(min_rec.get("wire_bytes", 0)),
                "reason": str(reason)[:120],
            }
        )

    def _worker_flush_pending_minutes(self, *, at_seal: bool) -> bool:
        if self._exporter.configured_dir is None:
            self._pending_minute_exports.clear()
            return True
        all_ok = True
        if not at_seal:
            while self._pending_minute_exports:
                rec = self._pending_minute_exports[0]
                if self._exporter.write_record("MINUTE_WINDOW", rec, update_checkpoint=False):
                    self._pending_minute_exports.popleft()
                    self._agg_durable_minute_count += 1
                else:
                    self._agg_epoch_export_failures += 1
                    all_ok = False
                    break
            return all_ok

        # At epoch seal: try every pending minute once more; any still-failing minute is recorded
        # explicitly in `missing_durable_intervals` while allowing surviving minutes & seal to flush!
        remaining = list(self._pending_minute_exports)
        self._pending_minute_exports.clear()
        for rec in remaining:
            if self._exporter.write_record("MINUTE_WINDOW", rec, update_checkpoint=False):
                self._agg_durable_minute_count += 1
            else:
                self._agg_epoch_export_failures += 1
                err = self._exporter._last_export_error or "MINUTE_EXPORT_FAILED_AT_SEAL"
                self._record_missing_minute_interval(rec, err)
                all_ok = False
        return all_ok

    def _worker_apply_ok_frame(self, item: List[Any]) -> None:
        (
            _,
            _ep_seq,
            mono_ns,
            wire_len,
            non_bytes,
            feeds_len,
            sdk_app_ns,
            listener_ns,
            equity_ns,
            ready_added_ns,
        ) = item

        self._agg_ingress_frames_total += 1
        self._agg_decoded_ok_frames += 1
        self._agg_wire_bytes_total += wire_len
        self._agg_decoded_feeds_total += feeds_len
        if non_bytes:
            self._agg_non_bytes_frames += 1

        if self._agg_first_frame_mono_ns == 0 or mono_ns < self._agg_first_frame_mono_ns:
            self._agg_first_frame_mono_ns = mono_ns
        if mono_ns > self._agg_last_frame_mono_ns:
            self._agg_last_frame_mono_ns = mono_ns

        if wire_len > self._agg_max_frame_bytes:
            self._agg_max_frame_bytes = wire_len
        if self._agg_min_frame_bytes == 0 or (wire_len > 0 and wire_len < self._agg_min_frame_bytes):
            self._agg_min_frame_bytes = wire_len
        if feeds_len > self._agg_max_feeds_per_frame:
            self._agg_max_feeds_per_frame = feeds_len

        if wire_len > 16384:
            self._agg_frames_gt_16kib += 1
            if wire_len > 65536:
                self._agg_frames_gt_64kib += 1
                if wire_len > 262144:
                    self._agg_frames_gt_256kib += 1
                    if wire_len > 1048576:
                        self._agg_frames_gt_1mib += 1

        self._agg_hist_frame_bytes.observe(float(wire_len))
        self._agg_hist_feeds_per_frame.observe(float(feeds_len))
        self._worker_advance_time_buckets(mono_ns, wire_len, feeds_len)

        sdk_us = sdk_app_ns / 1000.0
        self._agg_hist_sdk_and_app_us.observe(sdk_us)
        if listener_ns >= 0:
            self._agg_hist_listener_us.observe(listener_ns / 1000.0)
        if equity_ns >= 0:
            self._agg_hist_equity_us.observe(equity_ns / 1000.0)

        if ready_added_ns >= 0:
            added_us = ready_added_ns / 1000.0
            self._agg_hist_probe_added_us.observe(added_us)
            self._agg_hist_total_callback_us.observe((sdk_app_ns + ready_added_ns) / 1000.0)
        else:
            # Never fabricate a 0.0 us timing sample when readiness marker was not completed!
            self._agg_timing_unavailable_count += 1
            self._timing_unavailable_total += 1
            self._agg_invalidation_reasons.add("TIMING_SAMPLE_UNAVAILABLE")

    def _worker_apply_err_frame(self, item: List[Any]) -> None:
        _, _ep_seq, mono_ns, wire_len, non_bytes, sdk_elapsed_ns, ready_added_ns = item
        self._agg_ingress_frames_total += 1
        self._agg_sdk_or_listener_errors += 1
        self._agg_wire_bytes_total += wire_len
        if non_bytes:
            self._agg_non_bytes_frames += 1
        if self._agg_first_frame_mono_ns == 0 or mono_ns < self._agg_first_frame_mono_ns:
            self._agg_first_frame_mono_ns = mono_ns
        if mono_ns > self._agg_last_frame_mono_ns:
            self._agg_last_frame_mono_ns = mono_ns
        if wire_len > self._agg_max_frame_bytes:
            self._agg_max_frame_bytes = wire_len
        self._agg_hist_frame_bytes.observe(float(wire_len))
        self._worker_advance_time_buckets(mono_ns, wire_len, 0)
        self._agg_hist_sdk_and_app_us.observe(sdk_elapsed_ns / 1000.0)
        if ready_added_ns >= 0:
            self._agg_hist_probe_added_us.observe(ready_added_ns / 1000.0)
            self._agg_hist_total_callback_us.observe((sdk_elapsed_ns + ready_added_ns) / 1000.0)
        else:
            self._agg_timing_unavailable_count += 1
            self._timing_unavailable_total += 1
            self._agg_invalidation_reasons.add("TIMING_SAMPLE_UNAVAILABLE")

    def _worker_apply_lifecycle(self, ev: str, detail: Any, mono_ns: int) -> None:
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

        self._agg_lifecycle_events.append({"event": ev, "wall_ms": wall_ms, "detail": detail_str})

    def _worker_publish_snapshot(self) -> None:
        self._published_snapshot = self._build_snapshot_from_aggregator(
            include_rings=True, include_hist_bins=False
        )
        self._agg_last_snapshot_pub_ns = time.perf_counter_ns()

    def _worker_export_summary_record(
        self, record_kind: str, extra_fields: Optional[Dict[str, Any]] = None
    ) -> bool:
        bins_mode: Any = True if record_kind == "EPOCH_SEALED" else "compact"
        export_snap = self._build_snapshot_from_aggregator(include_rings=False, include_hist_bins=bins_mode)
        audit_delta, new_audit_cursor = self._peek_unexported_control_audit()
        export_snap["control_audit_delta"] = audit_delta
        if extra_fields:
            export_snap.update(extra_fields)
        ok = self._exporter.write_record(record_kind, export_snap, update_checkpoint=True)
        if ok:
            # Commit control-audit cursor ONLY after primary ledger write + fsync succeeded!
            self._commit_exported_control_audit(new_audit_cursor)
            self._agg_last_exported_frames = self._agg_ingress_frames_total
        else:
            self._agg_epoch_export_failures += 1
        return ok

    def _build_snapshot_from_aggregator(
        self, include_rings: bool = False, include_hist_bins: Any = False
    ) -> Dict[str, Any]:
        now_mono_ns = time.perf_counter_ns()
        live_enabled_ns = self._agg_accumulated_enabled_ns
        if self._capture_enabled and self._agg_active_start_mono_ns > 0 and now_mono_ns > self._agg_active_start_mono_ns:
            live_enabled_ns += (now_mono_ns - self._agg_active_start_mono_ns)
        enabled_span_s = max(0.0, live_enabled_ns / 1_000_000_000.0)

        live_disconn_ns = self._agg_accumulated_disconnected_ns
        if (
            self._capture_enabled
            and self._agg_disconnected_since_mono_ns > 0
            and now_mono_ns > self._agg_disconnected_since_mono_ns
        ):
            live_disconn_ns += (now_mono_ns - self._agg_disconnected_since_mono_ns)
        disconn_span_s = min(enabled_span_s, max(0.0, live_disconn_ns / 1_000_000_000.0))
        connected_span_s = max(0.0, enabled_span_s - disconn_span_s)

        frame_span_s = (
            (self._agg_last_frame_mono_ns - self._agg_first_frame_mono_ns) / 1_000_000_000.0
            if self._agg_ingress_frames_total >= 2 and self._agg_last_frame_mono_ns > self._agg_first_frame_mono_ns
            else 0.0
        )
        effective_span_s = (
            connected_span_s
            if connected_span_s > 0.0
            else (enabled_span_s if enabled_span_s > 0.0 else frame_span_s)
        )
        sustained_frames_s = (self._agg_ingress_frames_total / effective_span_s) if effective_span_s > 0.0 else 0.0
        sustained_bytes_s = (self._agg_wire_bytes_total / effective_span_s) if effective_span_s > 0.0 else 0.0

        reasons: Set[str] = set(self._agg_invalidation_reasons)
        if self._auto_disabled_on_fault:
            reasons.add(f"AUTO_DISABLED:{self._auto_disabled_reason or 'UNKNOWN'}")
        if self._observer_boundary_faults > 0:
            reasons.add("OBSERVER_BOUNDARY_FAULT")
        if self._inbox_overflow_drops > 0:
            reasons.add("INBOX_OVERFLOW_DROP")
        if self._stage_event_drops > 0:
            reasons.add("STAGE_EVENT_DROP")
        if self._lifecycle_event_drops > 0:
            reasons.add("LIFECYCLE_EVENT_DROP")
        if self._timing_unavailable_total > 0 or self._agg_timing_unavailable_count > 0:
            reasons.add("TIMING_SAMPLE_UNAVAILABLE")
        if self._worker_dead_latched:
            reasons.add("WORKER_DEAD_OR_STALLED")
        if self._agg_missing_export_intervals_count > 0:
            reasons.add("DURABLE_EXPORT_GAP")

        operating_clean = (len(reasons - {"DURABLE_EXPORT_GAP"}) == 0)
        meas_valid = operating_clean
        dur_valid = (
            self._agg_missing_export_intervals_count == 0
            and len(self._pending_minute_exports) == 0
        )

        unexported_tail = max(0, self._agg_ingress_frames_total - self._agg_last_exported_frames)
        if not self._capture_enabled and self._agg_epoch_closed:
            if self._agg_epoch_seq == 0 and self._agg_ingress_frames_total == 0:
                tail_state = "IDLE_EMPTY_EPOCH"
            elif self._agg_missing_export_intervals_count > 0:
                tail_state = "SEALED_WITH_DURABLE_GAPS"
            elif self._agg_epoch_durable and unexported_tail == 0:
                tail_state = "COMPLETE_SEALED"
            else:
                tail_state = "UNEXPORTED_SEAL_FAILED"
        else:
            tail_state = "LIVE_UNSEALED_EPOCH"

        probe_added_sum = self._agg_hist_probe_added_us.summary(include_bins=include_hist_bins)
        sdk_app_sum = self._agg_hist_sdk_and_app_us.summary(include_bins=include_hist_bins)
        total_cb_sum = self._agg_hist_total_callback_us.summary(include_bins=include_hist_bins)
        worker_slice_sum = self._agg_hist_worker_slice_us.summary(include_bins=include_hist_bins)
        frame_bytes_sum = self._agg_hist_frame_bytes.summary(include_bins=include_hist_bins)
        rate_1s_bytes_sum = self._agg_hist_1s_all_bytes.summary(include_bins=include_hist_bins)

        sample_cnt = probe_added_sum["count"]
        raw_within = (
            sample_cnt >= 1
            and probe_added_sum["p99_upper_bound"] <= LATENCY_P99_BUDGET_US
            and probe_added_sum["p999_upper_bound"] <= LATENCY_SINGLE_BREACH_US
        )
        if sample_cnt == 0:
            budget_status = "INSUFFICIENT_SAMPLES"
            within_and_healthy = False
        elif not meas_valid:
            budget_status = "UNQUALIFIED_HEALTH_DEGRADED"
            within_and_healthy = False
        elif raw_within:
            budget_status = "WITHIN_INTERNAL_CALLBACK_BUDGET"
            within_and_healthy = True
        else:
            budget_status = "EXCEEDED_INTERNAL_CALLBACK_BUDGET"
            within_and_healthy = False

        exp_status = self._exporter.status_snapshot(now_mono_ns)
        exp_status["tail_state"] = tail_state
        exp_status["unexported_tail_frames"] = unexported_tail
        exp_status["missing_durable_intervals_count"] = self._agg_missing_export_intervals_count

        res: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "probe_id": self._probe_id,
            "boot_id": self._boot_id,
            "process_id": self._process_id,
            "probe_code_sha256": PROBE_CODE_SHA256,
            "epoch_id": self._agg_epoch_id,
            "epoch_seq": self._agg_epoch_seq,
            "enabled": bool(self._capture_enabled),
            "attachment": self.inspect_attachment(refresh=False),
            "control_intervals": [dict(x) for x in self._agg_control_intervals],
            "missing_durable_intervals_count": self._agg_missing_export_intervals_count,
            "missing_durable_intervals": [dict(x) for x in self._agg_missing_export_intervals],
            "durable_completeness_valid": dur_valid,
            "ingress_workload": {
                "ingress_frames_total": self._agg_ingress_frames_total,
                "decoded_ok_frames": self._agg_decoded_ok_frames,
                "sdk_or_listener_errors": self._agg_sdk_or_listener_errors,
                "non_bytes_frames": self._agg_non_bytes_frames,
                "raw_wire_bytes_total": self._agg_wire_bytes_total,
                "decoded_feed_entries_total": self._agg_decoded_feeds_total,
                "max_frame_bytes": self._agg_max_frame_bytes,
                "min_frame_bytes": self._agg_min_frame_bytes,
                "max_feeds_per_frame": self._agg_max_feeds_per_frame,
                "frames_gt_16kib": self._agg_frames_gt_16kib,
                "frames_gt_64kib": self._agg_frames_gt_64kib,
                "frames_gt_256kib": self._agg_frames_gt_256kib,
                "frames_gt_1mib": self._agg_frames_gt_1mib,
            },
            "time_bucket_rates": {
                "enabled_monotonic_seconds": round(enabled_span_s, 3),
                "connected_enabled_seconds": round(connected_span_s, 3),
                "disconnected_enabled_seconds": round(disconn_span_s, 3),
                "first_to_last_frame_span_seconds": round(frame_span_s, 3),
                "sustained_frames_per_sec": round(sustained_frames_s, 3),
                "sustained_wire_bytes_per_sec": round(sustained_bytes_s, 2),
                "peak_100ms_frames": self._agg_peak_100ms_frames,
                "peak_100ms_bytes": self._agg_peak_100ms_bytes,
                "peak_100ms_feeds": self._agg_peak_100ms_feeds,
                "confirmed_zero_100ms_buckets": self._agg_confirmed_zero_100ms_buckets,
                "peak_1s_frames": self._agg_peak_1s_frames,
                "peak_1s_bytes": self._agg_peak_1s_bytes,
                "peak_1s_feeds": self._agg_peak_1s_feeds,
                "confirmed_zero_1s_buckets": self._agg_confirmed_zero_1s_buckets,
                "completed_1m_buckets_durable": self._agg_durable_minute_count,
                "pending_1m_buckets_in_retry_queue": len(self._pending_minute_exports),
            },
            "distributions": {
                "frame_bytes": frame_bytes_sum,
                "feeds_per_frame": self._agg_hist_feeds_per_frame.summary(include_bins=include_hist_bins),
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
            "latencies_us": {
                "probe_added_callback_us": probe_added_sum,
                "sdk_and_app_callback_us": sdk_app_sum,
                "total_callback_us": total_cb_sum,
                "listener_broadcast_us": self._agg_hist_listener_us.summary(include_bins=include_hist_bins),
                "equity_ingest_us": self._agg_hist_equity_us.summary(include_bins=include_hist_bins),
                "bar_flush_15m_us": self._agg_hist_bar_flush_15m_us.summary(include_bins=include_hist_bins),
                "freeze_snapshot_us": self._agg_hist_freeze_snapshot_us.summary(include_bins=include_hist_bins),
                "worker_drain_slice_us": worker_slice_sum,
            },
            "lifecycle_and_reconnects": {
                "ws_open_count": self._agg_ws_open_count,
                "ws_close_count": self._agg_ws_close_count,
                "ws_error_count": self._agg_ws_error_count,
                "ws_reconnecting_count": self._agg_ws_reconnecting_count,
                "ws_auto_reconnect_stopped_count": self._agg_ws_auto_reconnect_stopped_count,
                "max_disconnect_gap_ms": round(self._agg_max_disconnect_gap_ms, 2),
                "recent_events": [dict(x) for x in self._agg_lifecycle_events],
            },
            "failures_and_bounds": {
                "worker_alive": bool(
                    self._worker_thread is not None and self._worker_thread.is_alive() and not self._worker_dead_latched
                ),
                "worker_dead_latched": bool(self._worker_dead_latched),
                "auto_disabled_on_fault": bool(self._auto_disabled_on_fault),
                "auto_disabled_reason": self._auto_disabled_reason,
                "observer_boundary_faults": int(self._observer_boundary_faults),
                "worker_internal_errors": int(self._agg_worker_internal_errors),
                "epoch_export_failures": int(self._agg_epoch_export_failures),
                "inbox_pending_samples": len(self._inbox),
                "inbox_max_samples": MAX_INBOX_SAMPLES,
                "inbox_overflow_drops": int(self._inbox_overflow_drops),
                "stage_event_drops": int(self._stage_event_drops),
                "lifecycle_event_drops": int(self._lifecycle_event_drops),
                "post_disable_inflight_drops": int(self._post_disable_inflight_drops),
                "timing_unavailable_count": max(
                    int(self._agg_timing_unavailable_count), int(self._timing_unavailable_total)
                ),
            },
            "durable_exporter": exp_status,
            "workload_meter_qualification": {
                "scope": "WORKLOAD_METER_ONLY_NO_S1_FIELD_REPLAY",
                "operating_health_clean": operating_clean,
                "measurement_integrity_valid": meas_valid,
                "durable_completeness_valid": dur_valid,
                "tail_state": tail_state,
                "missing_durable_intervals_count": self._agg_missing_export_intervals_count,
                "invalidation_reasons": sorted(reasons),
                "internal_callback_budget": {
                    "sample_count": sample_cnt,
                    "status": budget_status,
                    "probe_added_p99_upper_bound_us": probe_added_sum["p99_upper_bound"],
                    "probe_added_p999_upper_bound_us": probe_added_sum["p999_upper_bound"],
                    "probe_added_exceed_100us_count": probe_added_sum["exceed_100_count"],
                    "probe_added_exceed_1000us_count": probe_added_sum["exceed_1000_count"],
                    "within_budget_and_healthy": within_and_healthy,
                    "qualification": (
                        "Internal callback timer measures entry-to-exit probe callback overhead only; "
                        "whole-process scheduling and end-to-end dispatch impact must be verified via "
                        "external matched-load OFF vs ON benchmarks."
                    ),
                },
            },
        }
        if include_rings:
            res["recent_1m_buckets"] = [dict(x) for x in self._agg_ring_1m]
            res["recent_1s_buckets"] = [dict(x) for x in self._agg_ring_1s]
            res["recent_100ms_buckets"] = [dict(x) for x in self._agg_ring_100ms]
        return res


def build_instrumented_streamer_class(base_streamer_cls: Any, probe: S1IngressMeasurementProbe) -> Any:
    """
    Minimal pass-through subclass of `upstox_client.MarketDataStreamerV3`.
      - Delegates 100% of protobuf decoding, dict conversion, and listener emission to
        `super().handle_message(ws, message)` (and `super()._on_message` if a custom test stub uses `_on_message`).
      - Captures `len(feeds)` in `O(1)` inside `emit("message", data_dict)` without retaining `data_dict`
        or `message` bytes.
    """
    has_handle_message = hasattr(base_streamer_cls, "handle_message")

    class InstrumentedMarketDataStreamerV3(base_streamer_cls):
        _s1_probe = probe
        _s1_probe_wrapper_version = PROBE_WRAPPER_VERSION

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._s1_last_feeds_len: int = 0
            probe.register_streamer_instance(self)

        def emit(self, event: Any, *args: Any) -> Any:
            if probe._capture_enabled and event == "message" and args and isinstance(args[0], dict):
                try:
                    feeds_obj = args[0].get("feeds")
                    self._s1_last_feeds_len = len(feeds_obj) if isinstance(feeds_obj, dict) else 0
                except Exception as p_exc:
                    self._s1_last_feeds_len = 0
                    probe.note_observer_fault("emit_feeds_len", p_exc)
            return super().emit(event, *args)

        def _invoke_measured(self, super_fn: Callable[[Any, Any], Any], ws: Any, message: Any) -> Any:
            if not probe._capture_enabled:
                return super_fn(ws, message)

            t_cb_entry = time.perf_counter_ns()
            self._s1_last_feeds_len = 0
            probe._clear_tls_stage_segments()

            t_sdk_start = time.perf_counter_ns()
            try:
                res = super_fn(ws, message)
            except BaseException as sdk_exc:
                t_sdk_err = time.perf_counter_ns()
                try:
                    probe.on_ingress_frame_error(
                        mono_ns=t_sdk_start,
                        message_obj=message,
                        sdk_elapsed_ns=max(0, t_sdk_err - t_sdk_start),
                        cb_entry_ns=t_cb_entry,
                    )
                except Exception:
                    pass
                raise

            t_sdk_end = time.perf_counter_ns()
            try:
                probe.on_ingress_frame_ok(
                    mono_ns=t_sdk_start,
                    message_obj=message,
                    decoded_feeds_len=self._s1_last_feeds_len,
                    sdk_and_app_ns=max(0, t_sdk_end - t_sdk_start),
                    cb_entry_ns=t_cb_entry,
                )
            except Exception as p_exc:
                probe.note_observer_fault("handle_message_post", p_exc)
            return res

        if has_handle_message:
            def handle_message(self, ws: Any, message: Any) -> Any:
                return self._invoke_measured(super().handle_message, ws, message)
        else:
            def _on_message(self, ws: Any, message: Any) -> Any:
                return self._invoke_measured(super()._on_message, ws, message)

    return InstrumentedMarketDataStreamerV3


def attach_probe_lifecycle_listeners(streamer: Any, probe: S1IngressMeasurementProbe) -> None:
    for ev_name in ("open", "close", "error", "reconnecting", "autoReconnectStopped"):
        def _make_cb(bound_ev: str) -> Callable[..., None]:
            def _cb(*args: Any, **_kwargs: Any) -> None:
                if not probe._capture_enabled:
                    return
                try:
                    detail = args[0] if len(args) == 1 else (args if args else None)
                    probe.record_lifecycle_event(bound_ev, detail)
                except Exception as p_exc:
                    probe.note_observer_fault(f"lifecycle_{bound_ev}", p_exc)
            return _cb
        try:
            streamer.on(ev_name, _make_cb(ev_name))
        except Exception:
            pass


_PROBE_SINGLETON_LOCK = threading.Lock()
_PROBE_SINGLETON: Optional[S1IngressMeasurementProbe] = None
_GUARD_SINGLETON: Optional[S1ProbeManagementGuard] = None


def peek_s1_ingress_probe() -> Optional[S1IngressMeasurementProbe]:
    return _PROBE_SINGLETON


def get_s1_ingress_probe() -> S1IngressMeasurementProbe:
    global _PROBE_SINGLETON
    if _PROBE_SINGLETON is None:
        with _PROBE_SINGLETON_LOCK:
            if _PROBE_SINGLETON is None:
                _PROBE_SINGLETON = S1IngressMeasurementProbe()
    return _PROBE_SINGLETON


def get_s1_probe_management_guard() -> S1ProbeManagementGuard:
    global _GUARD_SINGLETON
    if _GUARD_SINGLETON is None:
        with _PROBE_SINGLETON_LOCK:
            if _GUARD_SINGLETON is None:
                _GUARD_SINGLETON = S1ProbeManagementGuard()
    return _GUARD_SINGLETON
