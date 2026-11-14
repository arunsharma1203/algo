"""
Adversarial Offline Safety, Security, Durability, Tailscale-CORS & Complete-Overhead Suite
for Phase 1 (V3) S1 Ingress Measurement Probe.

Strict runtime isolation enforced BEFORE any SDK or application module is imported:
  - `sys.addaudithook` blocks all `socket.*`, `subprocess`, `os.system`, `urllib`, `sqlite3`,
    `market_data.db`, and any file write/mkdir/rename/unlink outside `_TEST_TMP_ROOT`.
  - Loads Upstox `MarketDataFeedV3_pb2` and `MarketDataStreamerV3` via isolated file specs / AST
    without executing `upstox_client/__init__.py` (preventing internal SDK loopback socket binds).
"""

import ast
import collections
import datetime
import gc
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple
from unittest import mock

# ──────────────────────────────────────────────────────────────────────────────
# 1. PRE-IMPORT ISOLATION SANDBOX & STRICT AUDIT HOOK
# ──────────────────────────────────────────────────────────────────────────────

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

sys.dont_write_bytecode = True
_TEST_TMP_ROOT = Path(tempfile.mkdtemp(prefix="s1_probe_v3_isolated_")).resolve()
os.environ["S1_PROBE_EXPORT_DIR"] = str(_TEST_TMP_ROOT / "default_singleton_dir")

_WRITE_OPEN_MASK = (
    os.O_WRONLY
    | os.O_RDWR
    | os.O_CREAT
    | os.O_TRUNC
    | os.O_APPEND
)
_AUDIT_VIOLATIONS: List[str] = []


def _is_inside_tmp(raw_path: Any) -> bool:
    if raw_path is None or isinstance(raw_path, int):
        return False
    try:
        p_str = os.fsdecode(raw_path)
    except Exception:
        p_str = str(raw_path)
    if p_str in ("/dev/null", "nul"):
        return True
    try:
        resolved = Path(p_str).resolve()
        return resolved == _TEST_TMP_ROOT or _TEST_TMP_ROOT in resolved.parents
    except Exception:
        return False


def _strict_audit_hook(event: str, args: Tuple[Any, ...]) -> None:
    if event.startswith("socket."):
        _AUDIT_VIOLATIONS.append(f"FORBIDDEN_SOCKET:{event}:{args[:2]}")
        raise RuntimeError(f"Forbidden network/socket operation in offline probe test: {event}")
    if event in ("subprocess.Popen", "os.system", "os.fork", "os.posix_spawn", "os.posix_spawnp", "urllib.Request"):
        _AUDIT_VIOLATIONS.append(f"FORBIDDEN_EXEC_OR_HTTP:{event}")
        raise RuntimeError(f"Forbidden external execution/HTTP in offline probe test: {event}")
    if event == "sqlite3.connect":
        _AUDIT_VIOLATIONS.append(f"FORBIDDEN_SQLITE:{args[:1]}")
        raise RuntimeError(f"Forbidden SQLite connection in offline probe test: {args[:1]}")
    if event == "open" and len(args) >= 2:
        target_path, mode, flags = args[0], args[1], (args[2] if len(args) >= 3 else 0)
        p_str = str(target_path)
        if "market_data.db" in p_str:
            _AUDIT_VIOLATIONS.append(f"FORBIDDEN_PROD_DB_OPEN:{p_str}")
            raise RuntimeError(f"Forbidden market_data.db access: {p_str}")
        is_write_mode = isinstance(mode, str) and any(ch in mode for ch in ("w", "a", "x", "+"))
        is_write_flags = isinstance(flags, int) and bool(flags & _WRITE_OPEN_MASK)
        if (is_write_mode or is_write_flags) and not _is_inside_tmp(target_path):
            _AUDIT_VIOLATIONS.append(f"FORBIDDEN_WRITE_OPEN:{p_str}:mode={mode}:flags={flags}")
            raise RuntimeError(f"Forbidden filesystem write outside temp root: {p_str}")
    if event in ("os.mkdir", "os.remove", "os.unlink", "os.rmdir") and len(args) >= 1:
        if not _is_inside_tmp(args[0]):
            _AUDIT_VIOLATIONS.append(f"FORBIDDEN_FS_MUTATION:{event}:{args[0]}")
            raise RuntimeError(f"Forbidden {event} outside temp root: {args[0]}")
    if event in ("os.rename", "os.replace") and len(args) >= 2:
        if not (_is_inside_tmp(args[0]) and _is_inside_tmp(args[1])):
            _AUDIT_VIOLATIONS.append(f"FORBIDDEN_FS_RENAME:{event}:{args[:2]}")
            raise RuntimeError(f"Forbidden {event} outside temp root: {args[:2]}")


sys.addaudithook(_strict_audit_hook)

# ──────────────────────────────────────────────────────────────────────────────
# 2. SOCKET-FREE UPSTOX PROTOBUF & STREAMER V3 AST LOADER
# ──────────────────────────────────────────────────────────────────────────────

from google.protobuf import json_format  # noqa: E402


def _load_upstox_proto_and_streamer_ast() -> Tuple[Any, Any]:
    spec = importlib.util.find_spec("upstox_client")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("upstox_client package not found in environment")
    pkg_dir = Path(list(spec.submodule_search_locations)[0])
    pb_path = pkg_dir / "feeder" / "proto" / "MarketDataFeedV3_pb2.py"
    streamer_base_path = pkg_dir / "feeder" / "streamer.py"
    streamer_v3_path = pkg_dir / "feeder" / "market_data_streamer_v3.py"

    # Register synthetic package shells so relative/submodule imports never run upstox_client/__init__.py
    for mod_name, mod_dir in (
        ("upstox_client", pkg_dir),
        ("upstox_client.feeder", pkg_dir / "feeder"),
        ("upstox_client.feeder.proto", pkg_dir / "feeder" / "proto"),
    ):
        if mod_name not in sys.modules:
            pkg_mod = types.ModuleType(mod_name)
            pkg_mod.__path__ = [str(mod_dir)]  # type: ignore[attr-defined]
            sys.modules[mod_name] = pkg_mod

    pb_mod_name = "upstox_client.feeder.proto.MarketDataFeedV3_pb2"
    pb_spec = importlib.util.spec_from_file_location(pb_mod_name, str(pb_path))
    assert pb_spec is not None and pb_spec.loader is not None
    pb_mod = importlib.util.module_from_spec(pb_spec)
    sys.modules[pb_mod_name] = pb_mod
    pb_spec.loader.exec_module(pb_mod)

    # Extract `Streamer` class AST and `MarketDataStreamerV3` class AST without importing feeder sockets
    from abc import ABC, abstractmethod

    ns: Dict[str, Any] = {
        "ABC": ABC,
        "abstractmethod": abstractmethod,
        "time": time,
        "MarketDataFeedV3_pb2": pb_mod,
        "pb": pb_mod,
        "json_format": json_format,
    }
    base_tree = ast.parse(streamer_base_path.read_text(encoding="utf-8"), filename=str(streamer_base_path))
    for node in base_tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Streamer":
            mod_ast = ast.Module(body=[node], type_ignores=[])
            exec(compile(mod_ast, str(streamer_base_path), "exec"), ns)
            break

    v3_tree = ast.parse(streamer_v3_path.read_text(encoding="utf-8"), filename=str(streamer_v3_path))
    for node in v3_tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MarketDataStreamerV3":
            mod_ast = ast.Module(body=[node], type_ignores=[])
            exec(compile(mod_ast, str(streamer_v3_path), "exec"), ns)
            break

    real_v3_cls = ns["MarketDataStreamerV3"]

    def _forbidden_broker_op(self: Any, *a: Any, **kw: Any) -> None:
        raise AssertionError("FORBIDDEN_BROKER_NETWORK_OPERATION")

    class OfflineVerifiedMarketDataStreamerV3(real_v3_cls):
        connect = _forbidden_broker_op
        disconnect = _forbidden_broker_op
        subscribe = _forbidden_broker_op
        unsubscribe = _forbidden_broker_op
        change_mode = _forbidden_broker_op

    return pb_mod, OfflineVerifiedMarketDataStreamerV3


pb, OfflineMarketDataStreamerV3 = _load_upstox_proto_and_streamer_ast()

import app.data.s1_ingress_probe as probe_mod  # noqa: E402
from app.data.s1_ingress_probe import (  # noqa: E402
    BoundedHistogram,
    DEFAULT_ALLOWED_LOCAL_ORIGINS,
    IST_TZ,
    LATENCY_BOUNDS_US,
    MAX_CONTROL_INTERVALS,
    MAX_EXPORT_STORAGE_BYTES,
    MAX_INBOX_SAMPLES,
    MAX_INBOX_WIRE_BYTES,
    PROBE_WRAPPER_VERSION,
    ProbeControlAction,
    ProbeControlError,
    S1IngressMeasurementProbe,
    S1MeasurementSummaryExporter,
    S1ProbeManagementGuard,
    S1_CORE_INSTRUMENT_KEYS,
    TAILSCALE_CORS_ORIGIN_REGEX,
    attach_probe_lifecycle_listeners,
    build_instrumented_streamer_class,
)


def _load_isolated_upstox_stream_service_cls() -> Any:
    """
    Loads `UpstoxStreamService` directly from `upstox_stream_service.py` AST with offline
    no-op provider/DB stubs so importing the test never triggers `urllib3` IPv6 socket probes
    or `universe_config` SQLite reads.
    """
    import logging

    def _forbid_io(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("FORBIDDEN_PROVIDER_OR_DB_CALL_IN_ISOLATED_TEST")

    class _DummyLimiter:
        def get_stats(self) -> Dict[str, Any]:
            return {"offline_stub": True}

    svc_path = _BACKEND_ROOT / "app" / "data" / "upstox_stream_service.py"
    svc_tree = ast.parse(svc_path.read_text(encoding="utf-8"), filename=str(svc_path))
    keep_nodes = [
        node
        for node in svc_tree.body
        if not (
            isinstance(node, ast.ImportFrom)
            and node.module
            in (
                "app.data.historical_data_layer",
                "app.data.upstox_provider",
                "app.analytics.universe_config",
                "numpy",
                "pandas",
            )
        )
        and not (isinstance(node, ast.Import) and any(alias.name in ("sqlite3", "numpy", "pandas") for alias in node.names))
    ]
    svc_ns: Dict[str, Any] = {
        "__file__": str(svc_path),
        "__name__": "isolated_upstox_stream_service",
        "get_db_path": _forbid_io,
        "get_upstox_token": _forbid_io,
        "get_instrument_key": _forbid_io,
        "fetch_upstox_bulk_quotes": _forbid_io,
        "get_upstox_limiter": lambda: _DummyLimiter(),
        "resolve_all_database_stocks": _forbid_io,
        "normalize_ticker": lambda s: str(s),
        "logging": logging,
    }
    exec(compile(ast.Module(body=keep_nodes, type_ignores=[]), str(svc_path), "exec"), svc_ns)
    return svc_ns["UpstoxStreamService"], svc_ns


UpstoxStreamService, _ISOLATED_SVC_NS = _load_isolated_upstox_stream_service_cls()

probe_mod._CACHED_PB_MODULE = pb


# ──────────────────────────────────────────────────────────────────────────────
# 3. REALISTIC WIRE-FRAME & ISOLATED STREAM-HARNESS BUILDERS
# ──────────────────────────────────────────────────────────────────────────────

def _build_realistic_wire_frame(
    *,
    target_instruments: int = 53,
    include_options: int = 12,
    depth_levels: int = 5,
    feed_type: int = 1,
    current_ts_ms: int = 1763102100123,
    lot_qty: int = 130,
) -> Tuple[bytes, Set[str], Dict[str, Dict[str, Any]]]:
    resp = pb.FeedResponse()
    resp.type = feed_type
    if current_ts_ms > 0:
        resp.currentTs = current_ts_ms

    subscribed: Set[str] = set()
    contracts: Dict[str, Dict[str, Any]] = {}
    ist_today = datetime.datetime.now(IST_TZ).date()
    exp_near = (ist_today + datetime.timedelta(days=3)).isoformat()
    exp_far = (ist_today + datetime.timedelta(days=14)).isoformat()

    # 1. 5 S1 core instruments
    for idx_key in ("NSE_INDEX|Nifty 50", "NSE_INDEX|India VIX"):
        subscribed.add(idx_key)
        f = resp.feeds[idx_key]
        iff = f.fullFeed.indexFF
        iff.ltpc.ltp = 24250.5 if "Nifty" in idx_key else 14.25
        iff.ltpc.ltt = current_ts_ms - 50
        iff.ltpc.cp = 24180.0 if "Nifty" in idx_key else 14.10
        c1 = iff.marketOHLC.ohlc.add()
        c1.interval = "I1"
        c1.open = iff.ltpc.ltp - 5.0
        c1.high = iff.ltpc.ltp + 5.0
        c1.low = iff.ltpc.ltp - 8.0
        c1.close = iff.ltpc.ltp
        c1.vol = 100000
        c1.ts = current_ts_ms - 1000

    for eq_idx, eq_key in enumerate(("NSE_EQ|INE002A01018", "NSE_EQ|INE040A01034", "NSE_EQ|INE090A01021")):
        subscribed.add(eq_key)
        f = resp.feeds[eq_key]
        mff = f.fullFeed.marketFF
        base_px = 1300.0 + eq_idx * 200.0
        mff.ltpc.ltp = base_px
        mff.ltpc.ltt = current_ts_ms - 25
        mff.ltpc.ltq = 50
        mff.ltpc.cp = base_px - 10.0
        for lvl in range(depth_levels):
            q = mff.marketLevel.bidAskQuote.add()
            q.bidP = base_px - 0.05 * (lvl + 1)
            q.askP = base_px + 0.05 * (lvl + 1)
            q.bidQ = 250 * (lvl + 1)
            q.askQ = 260 * (lvl + 1)
        c1 = mff.marketOHLC.ohlc.add()
        c1.interval = "I1"
        c1.open = base_px - 2.0
        c1.high = base_px + 3.0
        c1.low = base_px - 4.0
        c1.close = base_px
        c1.vol = 500000
        c1.ts = current_ts_ms - 1000

    # 2. NIFTY Options (mix of 1-7 DTE and >7 DTE)
    for opt_i in range(include_options):
        okey = f"NSE_FO|NIFTYOPT_{opt_i:04d}"
        subscribed.add(okey)
        is_near = (opt_i % 3 != 2)
        contracts[okey] = {
            "underlying": "NIFTY",
            "expiry": exp_near if is_near else exp_far,
            "lot_size": 65,
            "is_futures": False,
        }
        f = resp.feeds[okey]
        mff = f.fullFeed.marketFF
        px = 145.25 + opt_i * 3.5
        mff.ltpc.ltp = px
        mff.ltpc.ltt = current_ts_ms - 15
        mff.ltpc.ltq = 65
        mff.ltpc.cp = px - 8.0
        mff.oi = 1250000.0 + opt_i * 10000.0
        mff.optionGreeks.delta = 0.48
        mff.optionGreeks.theta = -12.4
        mff.optionGreeks.gamma = 0.0012
        mff.optionGreeks.vega = 11.8
        for lvl in range(depth_levels):
            q = mff.marketLevel.bidAskQuote.add()
            q.bidP = round(px - 0.05 * (lvl + 1), 2)
            q.askP = round(px + 0.05 * (lvl + 1), 2)
            q.bidQ = lot_qty * (lvl + 1)
            q.askQ = lot_qty * (lvl + 1)
        c1 = mff.marketOHLC.ohlc.add()
        c1.interval = "I1"
        c1.open = px - 1.0
        c1.high = px + 2.0
        c1.low = px - 2.0
        c1.close = px
        c1.vol = 850000
        c1.ts = current_ts_ms - 1000

    # 3. Remaining non-S1 equities to reach `target_instruments`
    remaining = max(0, target_instruments - len(subscribed))
    for eq_j in range(remaining):
        nkey = f"NSE_EQ|INE999{eq_j:06d}"
        subscribed.add(nkey)
        f = resp.feeds[nkey]
        mff = f.fullFeed.marketFF
        px = 520.0 + (eq_j % 90) * 5.25
        mff.ltpc.ltp = px
        mff.ltpc.ltt = current_ts_ms - 30
        mff.ltpc.ltq = 25
        mff.ltpc.cp = px - 3.0
        for lvl in range(depth_levels):
            q = mff.marketLevel.bidAskQuote.add()
            q.bidP = round(px - 0.05 * (lvl + 1), 2)
            q.askP = round(px + 0.05 * (lvl + 1), 2)
            q.bidQ = 100 * (lvl + 1)
            q.askQ = 120 * (lvl + 1)
        c1 = mff.marketOHLC.ohlc.add()
        c1.interval = "I1"
        c1.open = px - 1.0
        c1.high = px + 1.5
        c1.low = px - 1.5
        c1.close = px
        c1.vol = 120000
        c1.ts = current_ts_ms - 1000

    return resp.SerializeToString(), subscribed, contracts


def _build_compact_ltpc_wire_frame() -> Tuple[bytes, Set[str], Dict[str, Dict[str, Any]]]:
    resp = pb.FeedResponse()
    resp.type = 1
    resp.currentTs = 1763102100500
    k = "NSE_INDEX|Nifty 50"
    f = resp.feeds[k]
    f.ltpc.ltp = 24255.25
    f.ltpc.ltt = 1763102100480
    f.ltpc.cp = 24180.0
    return resp.SerializeToString(), {k}, {}


def _make_isolated_stream_harness(probe: S1IngressMeasurementProbe) -> UpstoxStreamService:
    """Builds an unstarted, zero-singleton, zero-DB UpstoxStreamService instance via `object.__new__`."""
    svc = object.__new__(UpstoxStreamService)
    svc._lock = threading.RLock()
    svc._is_running = False
    svc._is_connected = True
    svc._connected_at = "2026-11-14T09:15:00"
    svc._last_msg_ts = 0.0
    svc._total_messages = 0
    svc._total_ticks = 0
    svc._reconnect_count = 0
    svc._last_error = None
    svc._streamer = None
    svc._key_to_symbol = {
        "NSE_INDEX|Nifty 50": "^NSEI",
        "NSE_INDEX|India VIX": "^INDIAVIX",
        "NSE_EQ|INE002A01018": "RELIANCE.NS",
        "NSE_EQ|INE040A01034": "HDFCBANK.NS",
        "NSE_EQ|INE090A01021": "ICICIBANK.NS",
    }
    svc._symbol_to_key = {v: k for k, v in svc._key_to_symbol.items()}
    svc._additional_keys = set()
    svc._subscribed_ws_keys = set(svc._key_to_symbol.keys())
    svc._feed_listeners = []
    svc._live_quotes = {}
    svc._minute_bars = collections.defaultdict(dict)
    svc._completed_15m_bars = collections.defaultdict(list)
    svc._flushed_slots = set()
    svc._latest_frozen_snapshot = None
    svc._s1_ingress_probe = probe
    probe.set_universe_supplier(svc._supply_s1_probe_universe)
    probe.set_attachment_verifier(svc._inspect_s1_streamer_attachment)
    return svc


def _make_attached_streamer(probe: S1IngressMeasurementProbe, svc: UpstoxStreamService) -> Any:
    cls = build_instrumented_streamer_class(OfflineMarketDataStreamerV3, probe)
    dummy_api = types.SimpleNamespace(configuration=types.SimpleNamespace(access_token="offline-dummy"))
    st = cls(dummy_api, list(svc._key_to_symbol.keys()), "full")
    st.on("message", svc._handle_feed_message)
    attach_probe_lifecycle_listeners(st, probe)
    svc._streamer = st
    return st


# ──────────────────────────────────────────────────────────────────────────────
# 4. ADVERSARIAL REGRESSION & SAFETY UNIT TESTS
# ──────────────────────────────────────────────────────────────────────────────

class TestS1IngressProbeV3(unittest.TestCase):
    def _new_probe(self, tag: str, require_attached: bool = False) -> S1IngressMeasurementProbe:
        exp_dir = _TEST_TMP_ROOT / f"{tag}_{time.time_ns()}"
        p = S1IngressMeasurementProbe(export_dir=exp_dir, require_streamer_attached_default=require_attached)
        if not require_attached:
            p.set_attachment_verifier(lambda: {"wrapper_attached": True, "isolated": True})
        self.addCleanup(p.shutdown_worker)
        return p

    def test_p1_zero_mutex_complete_callback_timing_and_distribution_breaker(self) -> None:
        """
        Proves Priority 1 (`H2`/`H3`):
          1. `_wake_event` is completely eliminated (zero callback mutex/Condition acquisition).
          2. `SlowAppend` (`3 ms` delay inside `_inbox.append`) IS captured inside `full_probe_added_callback_us`
             and auto-disables the probe on frame 5.
          3. Rolling `p99 <= 100 us` distribution gate auto-disables even when individual samples stay `< 1000 us`.
          4. Fresh `0`-sample probe reports `passes_p99_le_100us_and_p999_le_1000us == False`.
        """
        p0 = self._new_probe("empty_gate")
        self.assertFalse(p0.enabled)
        self.assertFalse(hasattr(p0, "_wake_event"))
        s0 = p0.get_summary()
        gate0 = s0["codex_feasibility_metrics"]["callback_latency_gates_conservative"]
        self.assertEqual(gate0["sample_count"], 0)
        self.assertEqual(gate0["gate_status"], "INSUFFICIENT_SAMPLES")
        self.assertFalse(gate0["passes_p99_le_100us_and_p999_le_1000us"])

        # Reproduce Codex `unmeasured_callback_tail` (`SlowAppend` 3 ms inside `_inbox.append`)
        p_tail = self._new_probe("slow_append")
        p_tail._capture_enabled = True
        p_tail._active_epoch_seq = 1

        class SlowAppendDeque(collections.deque):
            def append(self, x: Any) -> None:
                time.sleep(0.003)
                super().append(x)

        p_tail._inbox = SlowAppendDeque()
        for _ in range(6):
            n = time.perf_counter_ns()
            p_tail.record_ingress_frame(
                raw_bytes=128,
                is_bytes=True,
                wire_type_int=1,
                wire_payload=b"\x08\x01",
                recv_wall_ns=time.time_ns(),
                t0_ns=n,
                t1_ns=n + 100,
                t2_ns=n + 200,
                t3_ns=n + 300,
            )
        # Frame 5 trips consecutive >1000us breaker; 6th call is dropped immediately!
        self.assertFalse(p_tail.enabled)
        self.assertTrue(p_tail._auto_disabled_on_fault)
        self.assertEqual(p_tail._auto_disabled_reason, "LATENCY_BUDGET_EXCEEDED")
        self.assertEqual(len(p_tail._inbox), 5)
        for item in list(p_tail._inbox):
            p_tail._worker_process_ok_frame(item)
        s_tail = p_tail._build_snapshot_from_aggregator()
        rep_full = s_tail["latencies_us"]["full_probe_added_callback_us"]
        self.assertGreaterEqual(rep_full["min"], 2500.0)
        self.assertGreaterEqual(rep_full["p99_upper_bound"], 2500.0)
        self.assertFalse(
            s_tail["codex_feasibility_metrics"]["callback_latency_gates_conservative"][
                "passes_p99_le_100us_and_p999_le_1000us"
            ]
        )

        # Rolling p99 > 100us distribution breaker (sub-1000us spikes that exceed 1% of rolling window)
        p_dist = self._new_probe("dist_breaker")
        p_dist.enable()
        for i in range(110):
            # 6 samples at 250us (>100us, <1000us), 104 samples at 5us -> 5.4% > 100us -> p99 > 100us!
            pre_ns = 250_000 if (i % 18 == 0) else 2_000
            n = time.perf_counter_ns()
            p_dist.record_ingress_frame(
                raw_bytes=64,
                is_bytes=True,
                wire_type_int=1,
                wire_payload=b"\x08\x01",
                recv_wall_ns=time.time_ns(),
                t0_ns=n,
                t1_ns=n + 100,
                t2_ns=n + 200,
                t3_ns=n + 300,
                pre_overhead_ns=pre_ns,
            )
        self.assertFalse(p_dist.enabled)
        self.assertTrue(p_dist._auto_disabled_on_fault)
        self.assertEqual(p_dist._auto_disabled_reason, "LATENCY_DISTRIBUTION_GATE_EXCEEDED")

    def test_p2_queue_bounds_heap_cap_immutability_and_worker_death_failsafe(self) -> None:
        """
        Proves Priority 2 (`H2`/`H4`/`M1`):
          1. `600` frames + `700` `disable()` calls NEVER exceed `MAX_INBOX_SAMPLES` (`256`).
          2. `512` frames retain `< 5.0 MiB` traced heap (`tracemalloc`) via both `wire_payload` and `data_dict`.
          3. Post-handoff `obj['feeds'].clear()` cannot mutate recorded observations.
          4. Unhandled worker exception latches `_worker_dead_latched`, sets `enabled=False`, `auto_disabled_on_fault=True`,
             `coverage_valid=False`, and `flush_for_test_or_export() -> False`.
        """
        # 1. Queue saturation + 700 disable controls
        p_q = self._new_probe("q_bounds")
        p_q._capture_enabled = True
        p_q._active_epoch_seq = 1
        for _ in range(600):
            n = time.perf_counter_ns()
            p_q.record_ingress_frame(
                raw_bytes=128,
                is_bytes=True,
                wire_type_int=1,
                wire_payload=b"\x08\x01",
                recv_wall_ns=time.time_ns(),
                t0_ns=n,
                t1_ns=n + 100,
                t2_ns=n + 200,
                t3_ns=n + 300,
            )
        self.assertEqual(len(p_q._inbox), MAX_INBOX_SAMPLES)
        self.assertEqual(p_q._inbox_overflow_drops, 600 - MAX_INBOX_SAMPLES)
        for _ in range(700):
            p_q.disable(flush_timeout_s=0.0)
        self.assertEqual(len(p_q._inbox), MAX_INBOX_SAMPLES)
        self.assertFalse(p_q.enabled)
        self.assertGreaterEqual(p_q._control_coalesced_count, 699)

        # 2. Heap retention (`tracemalloc`) on 512 decoded 53-instrument frames (both wire and dict paths)
        wire53, _, _ = _build_realistic_wire_frame(target_instruments=53, include_options=12)
        p_heap_wire = self._new_probe("heap_wire")
        p_heap_wire._capture_enabled = True
        p_heap_wire._active_epoch_seq = 1
        gc.collect()
        tracemalloc.start()
        base_w = tracemalloc.get_traced_memory()[0]
        for _ in range(512):
            n = time.perf_counter_ns()
            p_heap_wire.record_ingress_frame(
                raw_bytes=len(wire53),
                is_bytes=True,
                wire_type_int=1,
                wire_payload=bytes(wire53),
                recv_wall_ns=time.time_ns(),
                t0_ns=n,
                t1_ns=n + 100,
                t2_ns=n + 200,
                t3_ns=n + 300,
            )
        inc_wire = tracemalloc.get_traced_memory()[0] - base_w
        tracemalloc.stop()
        self.assertLessEqual(len(p_heap_wire._inbox), MAX_INBOX_SAMPLES)
        self.assertLessEqual(p_heap_wire._enqueued_wire_bytes_total, MAX_INBOX_WIRE_BYTES)
        self.assertLess(inc_wire, 5 * 1024 * 1024)

        p_heap_dict = self._new_probe("heap_dict")
        p_heap_dict._capture_enabled = True
        p_heap_dict._active_epoch_seq = 1
        gc.collect()
        tracemalloc.start()
        base_d = tracemalloc.get_traced_memory()[0]
        for _ in range(512):
            d_fresh = json_format.MessageToDict(pb.FeedResponse.FromString(wire53))
            n = time.perf_counter_ns()
            p_heap_dict.record_ingress_frame(
                raw_bytes=len(wire53),
                is_bytes=True,
                wire_type_int=1,
                data_dict=d_fresh,
                recv_wall_ns=time.time_ns(),
                t0_ns=n,
                t1_ns=n + 100,
                t2_ns=n + 200,
                t3_ns=n + 300,
            )
        inc_dict = tracemalloc.get_traced_memory()[0] - base_d
        tracemalloc.stop()
        self.assertLess(inc_dict, 5 * 1024 * 1024)

        # 3. Caller dict mutation immunity (`mutable_handoff`)
        p_mut = self._new_probe("mutable_handoff")
        p_mut._capture_enabled = True
        p_mut._active_epoch_seq = 1
        caller_dict: Dict[str, Any] = {"feeds": {"NSE_INDEX|Nifty 50": {"ltpc": {"ltp": 24200.0}}}}
        n = time.perf_counter_ns()
        p_mut.record_ingress_frame(
            raw_bytes=128,
            is_bytes=True,
            wire_type_int=1,
            data_dict=caller_dict,
            recv_wall_ns=time.time_ns(),
            t0_ns=n,
            t1_ns=n + 100,
            t2_ns=n + 200,
            t3_ns=n + 300,
        )
        caller_dict["feeds"].clear()
        p_mut._worker_process_ok_frame(p_mut._inbox.popleft())
        self.assertEqual(p_mut._agg_total_instrument_updates, 1)

        # 4. Control intervals bounded at `MAX_CONTROL_INTERVALS`
        p_ci = self._new_probe("ctrl_intervals")
        for i in range(MAX_CONTROL_INTERVALS + 10):
            if len(p_ci._agg_control_intervals) == MAX_CONTROL_INTERVALS:
                p_ci._agg_control_intervals_evicted += 1
            p_ci._agg_control_intervals.append({"enabled_utc": f"t{i}", "disabled_utc": f"t{i}d"})
        snap_ci = p_ci._build_snapshot_from_aggregator()
        self.assertEqual(len(snap_ci["control_intervals"]), MAX_CONTROL_INTERVALS)
        self.assertTrue(snap_ci["control_intervals_truncated"])
        self.assertEqual(snap_ci["control_intervals_evicted_count"], 10)

        # 5. Unhandled worker death auto-disables immediately & invalidates coverage
        p_wd = self._new_probe("worker_death")
        p_wd.enable()
        orig_build = p_wd._build_snapshot_from_aggregator

        def _boom(*a: Any, **kw: Any) -> Dict[str, Any]:
            raise RuntimeError("injected snapshot crash")

        p_wd._build_snapshot_from_aggregator = _boom  # type: ignore[method-assign]
        flush_ok = p_wd.flush_for_test_or_export(timeout_s=0.25)
        time.sleep(0.03)
        self.assertFalse(flush_ok)
        self.assertFalse(p_wd._worker_thread.is_alive())  # type: ignore[union-attr]
        self.assertFalse(p_wd.enabled)
        self.assertTrue(p_wd._auto_disabled_on_fault)
        self.assertIn("WORKER_TERMINATED", str(p_wd._auto_disabled_reason))
        p_wd._build_snapshot_from_aggregator = orig_build  # type: ignore[method-assign]
        sum_wd = p_wd.get_summary()
        self.assertFalse(sum_wd["failures_and_bounds"]["worker_alive"])
        self.assertFalse(sum_wd["delivery_coverage"]["coverage_valid"])

        # 6. Worker heartbeat stall watchdog auto-disables on callback
        p_st = self._new_probe("worker_stall")
        p_st._capture_enabled = True
        p_st._active_epoch_seq = 1
        p_st._worker_alive_flag = True
        p_st._worker_last_heartbeat_mono_ns = time.perf_counter_ns() - 6_000_000_000  # 6s > 5s stall limit
        n = time.perf_counter_ns()
        p_st.record_ingress_frame(
            raw_bytes=64,
            is_bytes=True,
            wire_type_int=1,
            wire_payload=b"\x08\x01",
            recv_wall_ns=time.time_ns(),
            t0_ns=n,
            t1_ns=n + 100,
            t2_ns=n + 200,
            t3_ns=n + 300,
        )
        self.assertFalse(p_st.enabled)
        self.assertEqual(p_st._auto_disabled_reason, "WORKER_HEARTBEAT_STALLED")

    def test_p3_durable_retention_exact_once_midnight_faults_and_5_session_capacity(self) -> None:
        """
        Proves Priority 3 (`H5`):
          1. `disable(); disable(); reset()` seals epoch ONCE (`merged_count == 1`, never `3`).
          2. Cross-midnight (`day=14` -> `day=15`) on same boot keeps contiguous valid hash chain.
          3. Secondary `os.replace` failure after ledger commit does NOT duplicate `export_seq` or corrupt chain.
          4. Primary ledger `fsync` failure rotates `stream_gen` cleanly.
          5. `reset()` fails closed (`409 UNEXPORTED_EPOCH_CANNOT_RESET`) when prior non-empty epoch failed export.
          6. Delayed in-flight `ep=1` frame arriving after `disable(); reset(); enable()` (`ep=2`) is rejected.
          7. Truncated ledger suffix is caught via `manifest_<boot_id>.json`.
          8. 5 full simulated `22,500s` (`6.25h`) sessions + 1 unsealed crash boot fit well below `64 MiB` (`< 20 MiB`).
        """
        # 1. Duplicate seal prevention (`1` real frame -> `count == 1`)
        p_dup = self._new_probe("dup_seal")
        p_dup.enable()
        n = time.perf_counter_ns()
        p_dup.record_ingress_frame(
            raw_bytes=128,
            is_bytes=True,
            wire_type_int=1,
            wire_payload=b"\x08\x01",
            recv_wall_ns=time.time_ns(),
            t0_ns=n,
            t1_ns=n + 100,
            t2_ns=n + 200,
            t3_ns=n + 300,
        )
        p_dup.flush_for_test_or_export()
        p_dup.disable()
        p_dup.disable()
        p_dup.reset()
        self.assertEqual(p_dup.get_summary()["durable_exporter"]["tail_state"], "IDLE_EMPTY_EPOCH")
        p_dup.shutdown_worker()
        v_dup = S1MeasurementSummaryExporter.load_and_verify_ledger_dir(p_dup._exporter.configured_dir)  # type: ignore[arg-type]
        self.assertEqual(len(v_dup["sealed_epoch_ids"]), 1)
        self.assertEqual(v_dup["merged_full_probe_added_callback_us"]["count"], 1)
        self.assertEqual(v_dup["duplicate_seal_warnings"], [])
        self.assertEqual(v_dup["corrupt_or_torn_tail_lines"], [])

        # 2. Cross-midnight hash chain on same boot
        real_dt = datetime.datetime

        class SteppedClock(real_dt):
            cur_day = 14

            @classmethod
            def now(cls, tz: Any = None) -> Any:
                return cls(2026, 11, cls.cur_day, 23, 59, 55, tzinfo=tz)

        mid_dir = _TEST_TMP_ROOT / f"midnight_{time.time_ns()}"
        exp_mid = S1MeasurementSummaryExporter(mid_dir, "sameboot1", 12345)
        with mock.patch.object(probe_mod.datetime, "datetime", SteppedClock):
            w1 = exp_mid.write_record("PERIODIC_CHECKPOINT", {"epoch_id": "sameboot1_ep0001"})
            SteppedClock.cur_day = 15
            w2 = exp_mid.write_record("EPOCH_SEALED", {"epoch_id": "sameboot1_ep0001"})
        self.assertTrue(w1 and w2)
        v_mid = S1MeasurementSummaryExporter.load_and_verify_ledger_dir(mid_dir)
        self.assertEqual(v_mid["verified_record_count"], 2)
        self.assertEqual(v_mid["ist_session_dates_observed"], ["2026-11-14", "2026-11-15"])
        self.assertEqual(v_mid["corrupt_or_torn_tail_lines"], [])
        self.assertEqual(v_mid["manifest_discrepancies"], [])

        # 3. Checkpoint `os.replace` failure after primary ledger commit
        ck_dir = _TEST_TMP_ROOT / f"ckpt_fault_{time.time_ns()}"
        exp_ck = S1MeasurementSummaryExporter(ck_dir, "bootck", 12345)
        with mock.patch.object(os, "replace", side_effect=OSError("injected rename error")):
            c1 = exp_ck.write_record("EPOCH_SEALED", {"epoch_id": "e1"}, update_checkpoint=True)
        c2 = exp_ck.write_record("EPOCH_SEALED", {"epoch_id": "e2"}, update_checkpoint=True)
        self.assertTrue(c1 and c2)
        self.assertEqual(exp_ck._last_durable_seq, 2)
        v_ck = S1MeasurementSummaryExporter.load_and_verify_ledger_dir(ck_dir)
        self.assertEqual(v_ck["verified_record_count"], 2)
        self.assertEqual(v_ck["corrupt_or_torn_tail_lines"], [])

        # 4. Primary ledger `os.fsync` failure rotates `stream_gen` cleanly
        gen_dir = _TEST_TMP_ROOT / f"gen_rot_{time.time_ns()}"
        exp_gen = S1MeasurementSummaryExporter(gen_dir, "bootgen", 12345)
        self.assertTrue(exp_gen.write_record("PERIODIC_CHECKPOINT", {"epoch_id": "eg1"}))
        with mock.patch.object(os, "fsync", side_effect=OSError("disk fsync error")):
            self.assertFalse(exp_gen.write_record("EPOCH_SEALED", {"epoch_id": "eg1"}))
        self.assertEqual(exp_gen._stream_gen, 2)
        self.assertTrue(exp_gen.write_record("EPOCH_SEALED", {"epoch_id": "eg1"}))
        v_gen = S1MeasurementSummaryExporter.load_and_verify_ledger_dir(gen_dir)
        self.assertEqual(v_gen["sealed_epoch_ids"], ["eg1"])

        # 5. `reset()` fails closed (`409`) when export failed — never discards unexported counts!
        p_fr = self._new_probe("failed_reset")
        p_fr.enable()
        n = time.perf_counter_ns()
        p_fr.record_ingress_frame(
            raw_bytes=128,
            is_bytes=True,
            wire_type_int=1,
            wire_payload=b"\x08\x01",
            recv_wall_ns=time.time_ns(),
            t0_ns=n,
            t1_ns=n + 100,
            t2_ns=n + 200,
            t3_ns=n + 300,
        )
        p_fr.flush_for_test_or_export()
        with mock.patch.object(p_fr._exporter, "write_record", return_value=False):
            d_res = p_fr.disable()
            self.assertFalse(d_res["sealed"])
            with self.assertRaises(ProbeControlError) as ctx_err:
                p_fr.reset()
            self.assertEqual(ctx_err.exception.status_code, 409)
            self.assertEqual(ctx_err.exception.code, "UNEXPORTED_EPOCH_CANNOT_RESET")
        self.assertEqual(p_fr._agg_ingress_attempts, 1)
        self.assertEqual(
            p_fr.get_summary()["durable_exporter"]["tail_state"],
            "UNEXPORTED_LIVE_OR_INCOMPLETE_TAIL",
        )

        # 6. Delayed in-flight prior-epoch observation race (`epoch_race.py`)
        p_race = self._new_probe("epoch_race")
        p_race.enable()  # epoch 1
        delayed_done = threading.Event()
        real_inbox = p_race._inbox

        class DelayedOnceInbox(collections.deque):
            def append(self, item: Any) -> None:
                if not delayed_done.is_set() and isinstance(item, list) and item[0] == 1:
                    delayed_done.set()
                    time.sleep(0.18)
                real_inbox.append(item)

        p_race._inbox = DelayedOnceInbox()
        t_sender = threading.Thread(
            target=lambda: p_race.record_ingress_frame(
                raw_bytes=128,
                is_bytes=True,
                wire_type_int=1,
                wire_payload=b"\x08\x01",
                recv_wall_ns=time.time_ns(),
                t0_ns=time.perf_counter_ns(),
                t1_ns=time.perf_counter_ns() + 100,
                t2_ns=time.perf_counter_ns() + 200,
                t3_ns=time.perf_counter_ns() + 300,
            )
        )
        t_sender.start()
        delayed_done.wait(timeout=0.5)
        p_race.disable()
        p_race.reset()
        p_race._inbox = real_inbox
        p_race.enable()  # epoch 2!
        t_sender.join()
        p_race.flush_for_test_or_export()
        sum_race = p_race.get_summary()
        self.assertEqual(sum_race["epoch_seq"], 2)
        self.assertEqual(sum_race["ingress_totals"]["decoded_ok_frames"], 0)
        self.assertGreaterEqual(sum_race["failures_and_bounds"]["post_disable_inflight_drops"], 1)

        # 7. Truncated suffix caught via `manifest_<boot_id>.json`
        tr_dir = _TEST_TMP_ROOT / f"trunc_manifest_{time.time_ns()}"
        exp_tr = S1MeasurementSummaryExporter(tr_dir, "boottrunc", 12345)
        exp_tr.write_record("PERIODIC_CHECKPOINT", {"epoch_id": "et1"})
        exp_tr.write_record("EPOCH_SEALED", {"epoch_id": "et1"})
        lfile = next(tr_dir.glob("ledger_*.jsonl"))
        first_line = lfile.read_bytes().splitlines(keepends=True)[0]
        lfile.write_bytes(first_line)
        v_tr = S1MeasurementSummaryExporter.load_and_verify_ledger_dir(tr_dir)
        self.assertEqual(len(v_tr["manifest_discrepancies"]), 1)
        self.assertEqual(v_tr["manifest_discrepancies"][0]["reason"], "TRUNCATED_OR_MISMATCHED_LEDGER_SUFFIX")

        # 8. Five full 22,500s (6.25h) sessions + 1 unsealed crash boot written to disk (`<< 64 MiB`)
        five_dir = _TEST_TMP_ROOT / f"five_full_sessions_{time.time_ns()}"
        wire53, sub53, con53 = _build_realistic_wire_frame(target_instruments=53, include_options=12)
        # Mock fsync during the 3,750-record capacity write so disk byte layout & hashes are 100% real without SSD wear
        with mock.patch.object(os, "fsync", return_value=None):
            for sess_idx in range(5):
                p_s = S1IngressMeasurementProbe(export_dir=five_dir, require_streamer_attached_default=False)
                p_s.set_attachment_verifier(lambda: {"wrapper_attached": True})
                p_s.set_universe_supplier(lambda: (sub53, con53))
                p_s.enable()
                n0 = time.perf_counter_ns()
                w0 = int(datetime.datetime(2026, 11, 16 + sess_idx, 9, 15, tzinfo=IST_TZ).timestamp() * 1e9)
                p_s.record_ingress_frame(
                    raw_bytes=len(wire53),
                    is_bytes=True,
                    wire_type_int=1,
                    wire_payload=wire53,
                    recv_wall_ns=w0,
                    t0_ns=n0,
                    t1_ns=n0 + 1000,
                    t2_ns=n0 + 2000,
                    t3_ns=n0 + 3000,
                )
                p_s.flush_for_test_or_export()
                # Emit all 375 1-minute rollups + all 125 180s periodic checkpoints for a 22,500s (6.25h) session!
                for m_idx in range(375):
                    p_s._worker_emit_completed_1m_rollup(
                        {
                            "epoch_minute": 29900000 + sess_idx * 1440 + m_idx,
                            "utc_minute": f"2026-11-{16 + sess_idx:02d}T03:45:00Z",
                            "frames": 480,
                            "bytes": 6200000,
                            "instruments": 25440,
                            "s1_updates": 8160,
                            "s1_option_books": 5760,
                            "partial_finalized": False,
                        }
                    )
                    if m_idx % 3 == 2:
                        ok_cp = p_s._worker_export_compact_record("PERIODIC_CHECKPOINT")
                        self.assertTrue(ok_cp)
                d_ok = p_s.disable()
                self.assertTrue(d_ok["sealed"])
                p_s.shutdown_worker()

            # Plus 6th boot crashing after periodic checkpoint without sealing
            p_crash = S1IngressMeasurementProbe(export_dir=five_dir, require_streamer_attached_default=False)
            p_crash.set_attachment_verifier(lambda: {"wrapper_attached": True})
            p_crash.enable()
            nc = time.perf_counter_ns()
            p_crash.record_ingress_frame(
                raw_bytes=len(wire53),
                is_bytes=True,
                wire_type_int=1,
                wire_payload=wire53,
                recv_wall_ns=time.time_ns(),
                t0_ns=nc,
                t1_ns=nc + 1000,
                t2_ns=nc + 2000,
                t3_ns=nc + 3000,
            )
            p_crash.flush_for_test_or_export(force_export=True)
            p_crash.shutdown_worker()

        actual_dir_bytes = sum(f.stat().st_size for f in five_dir.iterdir() if f.is_file())
        self.assertLess(actual_dir_bytes, 24 * 1024 * 1024)
        self.assertLess(actual_dir_bytes, MAX_EXPORT_STORAGE_BYTES)

        v_five = S1MeasurementSummaryExporter.load_and_verify_ledger_dir(five_dir)
        self.assertEqual(v_five["files_scanned"], 6)
        self.assertEqual(len(v_five["sealed_epoch_ids"]), 5)
        self.assertEqual(len(v_five["unsealed_crash_epoch_ids"]), 1)
        self.assertEqual(v_five["merged_full_probe_added_callback_us"]["count"], 5)
        self.assertEqual(v_five["merged_including_unsealed_crash_epochs_us"]["count"], 6)
        self.assertEqual(v_five["minute_rollup_count"], 5 * 376)
        self.assertGreaterEqual(v_five["control_audit_count"], 5)
        self.assertEqual(v_five["corrupt_or_torn_tail_lines"], [])
        self.assertEqual(v_five["manifest_discrepancies"], [])

    def test_p4_p5_emergency_disable_never_rate_limited_and_tailscale_cors(self) -> None:
        """
        Proves Priorities 4 & 5 (`H1`/`M5` + Tailscale CORS):
          1. Authenticated local `ADMIN` `disable` (`is_emergency_disable=True`) is NEVER rate-limited (`0` `429`s).
          2. Invalid actions do NOT drain write rate-limit tokens and return `422`.
          3. Malformed non-64-hex token hashes fail closed (`503`).
          4. Unauthenticated route calls fail BEFORE `get_stream_service()` is invoked.
          5. `/api/stream/status` (`get_status()["s1_ingress_probe"]`) exposes only minimal `enabled`/`auto_disabled_on_fault`.
          6. `TAILSCALE_CORS_ORIGIN_REGEX` preserves React UI access over Tailscale (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`,
             `*.ts.net`) while rejecting public/LAN origins, and `S1ProbeManagementGuard` remains strictly loopback-only.
        """
        admin_tok = "v3-admin-secret-token-0123456789abcdef"
        read_tok = "v3-reader-secret-token-0123456789abcdef"
        env_hashes = {
            "S1_PROBE_ADMIN_TOKEN_SHA256": hashlib.sha256(admin_tok.encode()).hexdigest(),
            "S1_PROBE_READ_TOKEN_SHA256": hashlib.sha256(read_tok.encode()).hexdigest(),
        }

        with mock.patch.dict(os.environ, env_hashes):
            guard = S1ProbeManagementGuard()
            hdr_admin = {"authorization": f"Bearer {admin_tok}"}
            hdr_read = {"authorization": f"Bearer {read_tok}"}

            # Drain all 3 write tokens
            for _ in range(3):
                self.assertEqual(
                    guard.authorize_request(client_host="127.0.0.1", headers=hdr_admin, require_write=True),
                    "ADMIN",
                )
            # 4th normal write (`enable`) IS rate-limited (`429`)
            with self.assertRaises(ProbeControlError) as rl_err:
                guard.authorize_request(client_host="127.0.0.1", headers=hdr_admin, require_write=True)
            self.assertEqual(rl_err.exception.status_code, 429)
            self.assertEqual(rl_err.exception.code, "RATE_LIMITED_CONTROL")

            # Emergency `disable` (`is_emergency_disable=True`) succeeds 25x consecutively with ZERO 429s!
            for _ in range(25):
                role = guard.authorize_request(
                    client_host="127.0.0.1",
                    headers=hdr_admin,
                    require_write=True,
                    is_emergency_disable=True,
                )
                self.assertEqual(role, "ADMIN")

            # Reader still cannot invoke emergency disable (`403 INSUFFICIENT_ROLE`)
            with self.assertRaises(ProbeControlError) as rd_err:
                guard.authorize_request(
                    client_host="127.0.0.1",
                    headers=hdr_read,
                    require_write=True,
                    is_emergency_disable=True,
                )
            self.assertEqual(rd_err.exception.status_code, 403)
            self.assertEqual(rd_err.exception.code, "INSUFFICIENT_ROLE")

            # Tailscale peer & Tailscale Origin are REJECTED on probe management plane (`403`)
            with self.assertRaises(ProbeControlError) as ts_peer_err:
                guard.authorize_request(
                    client_host="100.90.1.2",
                    headers=hdr_admin,
                    require_write=False,
                )
            self.assertEqual(ts_peer_err.exception.status_code, 403)
            self.assertEqual(ts_peer_err.exception.code, "NON_LOOPBACK_PEER")

            with self.assertRaises(ProbeControlError) as ts_orig_err:
                guard.authorize_request(
                    client_host="127.0.0.1",
                    headers={"authorization": f"Bearer {admin_tok}", "origin": "http://100.90.1.2:5173"},
                    require_write=False,
                )
            self.assertEqual(ts_orig_err.exception.status_code, 403)
            self.assertEqual(ts_orig_err.exception.code, "UNTRUSTED_ORIGIN")

        # Malformed non-hex SHA-256 env fails closed (`503`)
        with mock.patch.dict(
            os.environ,
            {"S1_PROBE_ADMIN_TOKEN_SHA256": "not-a-valid-64-char-hex", "S1_PROBE_READ_TOKEN_SHA256": ""},
        ):
            with self.assertRaises(ProbeControlError) as bad_cfg_err:
                S1ProbeManagementGuard().authorize_request(
                    client_host="127.0.0.1",
                    headers={"authorization": f"Bearer {admin_tok}"},
                    require_write=True,
                )
            self.assertEqual(bad_cfg_err.exception.status_code, 503)

        # Tailscale CORS regex verification (exact match against Starlette CORSMiddleware semantics)
        ts_re = re.compile(TAILSCALE_CORS_ORIGIN_REGEX)
        allowed_ui_origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://100.64.0.1:5173",
            "http://100.90.1.2:5173",
            "http://100.127.255.254:3000",
            "https://trading-desk.tailnet-123.ts.net",
            "http://macbook.ts.net:5173",
            "http://[fd7a:115c:a1e0::1]:5173",
        ]
        for orig in allowed_ui_origins:
            is_ok = (orig in DEFAULT_ALLOWED_LOCAL_ORIGINS) or bool(ts_re.match(orig))
            self.assertTrue(is_ok, f"Expected UI origin to be CORS-allowed: {orig}")

        rejected_origins = [
            "https://evil.example",
            "http://100.63.255.255:5173",
            "http://100.128.0.1:5173",
            "http://192.168.1.20:5173",
            "https://spoofed-ts.net",
            "http://100.90.1.2.evil.com:5173",
        ]
        for bad_orig in rejected_origins:
            is_ok = (bad_orig in DEFAULT_ALLOWED_LOCAL_ORIGINS) or bool(ts_re.match(bad_orig))
            self.assertFalse(is_ok, f"Expected untrusted origin to be CORS-rejected: {bad_orig}")

        # Verify `/api/stream/status` minimal public probe exposure
        p_pub = self._new_probe("pub_status")
        svc_pub = _make_isolated_stream_harness(p_pub)
        pub_status = svc_pub.get_status()["s1_ingress_probe"]
        self.assertEqual(set(pub_status.keys()), {"enabled", "auto_disabled_on_fault"})

        # Verify `main.py` route handler ordering & CORS wiring via AST extraction (zero DB/router side-effects!)
        main_src = (_BACKEND_ROOT / "app" / "main.py").read_text(encoding="utf-8")
        main_tree = ast.parse(main_src, filename="main.py")

        class _StubHTTPException(Exception):
            def __init__(self, status_code: int, detail: Any = None) -> None:
                super().__init__(str(detail))
                self.status_code = status_code
                self.detail = detail

        class _DummyApp:
            def get(self, *_a: Any, **_kw: Any) -> Any:
                return lambda fn: fn

            def post(self, *_a: Any, **_kw: Any) -> Any:
                return lambda fn: fn

        route_ns: Dict[str, Any] = {
            "app": _DummyApp(),
            "Request": Any,
            "Query": lambda default=None, **_kw: default,
            "HTTPException": _StubHTTPException,
        }
        cors_has_tailscale_regex = False
        for node in main_tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in (
                "get_s1_ingress_probe_summary",
                "control_s1_ingress_probe",
            ):
                exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), route_ns)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call_src = ast.get_source_segment(main_src, node) or ""
                if "CORSMiddleware" in call_src and "allow_origin_regex=_S1_TAILSCALE_CORS_REGEX" in call_src:
                    cors_has_tailscale_regex = True

        self.assertTrue(cors_has_tailscale_regex, "main.py CORSMiddleware must wire allow_origin_regex=_S1_TAILSCALE_CORS_REGEX")

        # Also verify against Starlette's real CORSMiddleware if present
        try:
            from starlette.middleware.cors import CORSMiddleware as StarletteCORS

            cors_mw = StarletteCORS(
                app=lambda *_a: None,
                allow_origins=sorted(DEFAULT_ALLOWED_LOCAL_ORIGINS),
                allow_origin_regex=TAILSCALE_CORS_ORIGIN_REGEX,
                allow_credentials=True,
            )
            self.assertTrue(cors_mw.is_allowed_origin("http://100.90.1.2:5173"))
            self.assertTrue(cors_mw.is_allowed_origin("https://desk.tailnet-abc.ts.net"))
            self.assertFalse(cors_mw.is_allowed_origin("https://evil.example"))
            self.assertFalse(cors_mw.is_allowed_origin("http://100.128.1.2:5173"))
        except ImportError:
            pass

        svc_calls = [0]

        def _counting_get_svc() -> Any:
            svc_calls[0] += 1
            return svc_pub

        fake_req_unauth = types.SimpleNamespace(
            client=types.SimpleNamespace(host="127.0.0.1"),
            headers={},
        )
        fake_upstox_svc_mod = types.ModuleType("app.data.upstox_stream_service")
        fake_upstox_svc_mod.get_stream_service = _counting_get_svc  # type: ignore[attr-defined]
        with mock.patch.dict(os.environ, env_hashes):
            with mock.patch.dict(sys.modules, {"app.data.upstox_stream_service": fake_upstox_svc_mod}):
                with self.assertRaises(_StubHTTPException) as unauth_exc:
                    route_ns["control_s1_ingress_probe"](fake_req_unauth, action="disable")
                self.assertEqual(unauth_exc.exception.status_code, 401)
                # Unauthenticated request rejected BEFORE `get_stream_service()` was ever touched!
                self.assertEqual(svc_calls[0], 0)

                # Invalid actions return 422 without draining `_write_limiter`, and emergency disable never 429s
                fresh_guard = S1ProbeManagementGuard()
                fake_req_admin = types.SimpleNamespace(
                    client=types.SimpleNamespace(host="127.0.0.1"),
                    headers={"authorization": f"Bearer {admin_tok}"},
                )
                with mock.patch.object(probe_mod, "_GLOBAL_MANAGEMENT_GUARD", fresh_guard):
                    for _ in range(10):
                        with self.assertRaises(_StubHTTPException) as inv_exc:
                            route_ns["control_s1_ingress_probe"](fake_req_admin, action="bogus_action")
                        self.assertEqual(inv_exc.exception.status_code, 422)
                    # Write tokens still intact after 10 invalid actions!
                    for _ in range(3):
                        res_f = route_ns["control_s1_ingress_probe"](fake_req_admin, action="export_flush")
                        self.assertIn("flushed", res_f)
                    # Limiter now exhausted for `export_flush`, yet `disable` STILL succeeds!
                    with self.assertRaises(_StubHTTPException) as rl_route_exc:
                        route_ns["control_s1_ingress_probe"](fake_req_admin, action="export_flush")
                    self.assertEqual(rl_route_exc.exception.status_code, 429)
                    for _ in range(5):
                        dis_res = route_ns["control_s1_ingress_probe"](fake_req_admin, action="disable")
                        self.assertFalse(dis_res["enabled"])

    def test_p6_coverage_historical_expiry_book_silence_and_zero_gap_o1(self) -> None:
        """
        Proves Priority 6 (`M2`/`M3`/`M4`):
          1. Separates `measurement_integrity_valid` (`True`) from `s1_required_inputs_complete` (`False`) when only NIFTY is subscribed.
          2. Flags `EMPTY_SUBSCRIBED_UNIVERSE` (`coverage_valid == False`) when supplier returns `(set(), {})`.
          3. Re-evaluates rotated-out historical option contracts by `expiry_iso` on frame receipt IST date (never frozen forever).
          4. Rejects `NaN` / crossed books and separates unverified lot size from `book_usable_1lot_updates`.
          5. Tracks never-delivered intended S1 keys, `per_instrument_worst_valid_book_silence_ms`, and `book_distinct_top_state_changes`.
          6. Handles `86,400s` (`24h`) idle gap in `O(1)` via `observe_many` with zero truncation cap.
        """
        # 1. Absent S1 cores (`only NIFTY subscribed`)
        p_abs = self._new_probe("absent_cores")
        p_abs._capture_enabled = True
        p_abs._active_epoch_seq = 1
        p_abs.set_universe_supplier(lambda: ({"NSE_INDEX|Nifty 50"}, {}))
        p_abs._worker_refresh_universe(force=True)
        n = time.perf_counter_ns()
        p_abs.record_ingress_frame(
            raw_bytes=80,
            is_bytes=True,
            wire_type_int=1,
            data_dict={"feeds": {"NSE_INDEX|Nifty 50": {"ltpc": {"ltp": 24200.0}}}},
            recv_wall_ns=time.time_ns(),
            t0_ns=n,
            t1_ns=n + 100,
            t2_ns=n + 200,
            t3_ns=n + 300,
        )
        p_abs._worker_process_ok_frame(p_abs._inbox.popleft())
        s_abs = p_abs._build_snapshot_from_aggregator()
        self.assertTrue(s_abs["delivery_coverage"]["measurement_integrity_valid"])
        self.assertFalse(s_abs["delivery_coverage"]["s1_required_inputs_complete"])
        self.assertIn("NSE_INDEX|India VIX", s_abs["delivery_coverage"]["missing_s1_required_inputs"])
        self.assertIn("NIFTY_OPTIONS_1_7_DTE_VERIFIED_1LOT_BOOK", s_abs["delivery_coverage"]["missing_s1_required_inputs"])

        # 2 & 3. Historical expiry re-evaluation across IST days + empty universe invalidation
        p_hist = self._new_probe("hist_expiry")
        p_hist._capture_enabled = True
        p_hist._active_epoch_seq = 1
        day0 = datetime.date(2026, 11, 16)
        old_key = "NSE_FO|NIFTY_EXPIRING_TOMORROW"
        exp_iso = (day0 + datetime.timedelta(days=1)).isoformat()
        u_box: List[Tuple[Set[str], Dict[str, Dict[str, Any]]]] = [
            ({old_key}, {old_key: {"underlying": "NIFTY", "expiry": exp_iso, "lot_size": 65}})
        ]
        p_hist.set_universe_supplier(lambda: u_box[0])
        p_hist._worker_refresh_universe(force=True)
        # Rotate contract out so universe becomes empty
        u_box[0] = (set(), {})
        p_hist._worker_refresh_universe(force=True)
        # On day0: DTE == 1 -> `NIFTY_OPTIONS_1_7_DTE`
        self.assertEqual(p_hist._classify_instrument_key(old_key, day0)[0], "NIFTY_OPTIONS_1_7_DTE")
        # On day0 + 2 days: DTE == -1 (expired!) -> `NIFTY_OPTIONS_OTHER_DTE` (NOT frozen forever!)
        self.assertEqual(
            p_hist._classify_instrument_key(old_key, day0 + datetime.timedelta(days=2))[0],
            "NIFTY_OPTIONS_OTHER_DTE",
        )
        s_empty_u = p_hist._build_snapshot_from_aggregator()
        self.assertFalse(s_empty_u["delivery_coverage"]["coverage_valid"])
        self.assertIn("EMPTY_SUBSCRIBED_UNIVERSE", s_empty_u["delivery_coverage"]["invalidation_reasons"])

        # 4 & 5. Seeded never-seen key + book silence during LTP-only updates + distinct book state changes + unverified lot
        p_bk = self._new_probe("book_semantics")
        p_bk._capture_enabled = True
        p_bk._active_epoch_seq = 1
        today_iso = (datetime.datetime.now(IST_TZ).date() + datetime.timedelta(days=3)).isoformat()
        opt_ver = "NSE_FO|OPT_VERIFIED"
        opt_unver = "NSE_FO|OPT_UNVERIFIED"
        never_key = "NSE_INDEX|India VIX"
        p_bk.set_universe_supplier(
            lambda: (
                {opt_ver, opt_unver, never_key},
                {
                    opt_ver: {"underlying": "NIFTY", "expiry": today_iso, "lot_size": 65},
                    opt_unver: {"underlying": "NIFTY", "expiry": today_iso},  # lot_size missing -> unverified!
                },
            )
        )
        p_bk._worker_refresh_universe(force=True)
        base_ns = time.perf_counter_ns()
        w_ns = time.time_ns()

        # Frame 1 & Frame 2 at t=0ms and t=100ms: identical valid book on `opt_ver`, unverified lot on `opt_unver`, NaN on another
        for step_idx in range(2):
            ts_ns = base_ns + step_idx * 100_000_000
            d_payload = {
                "feeds": {
                    opt_ver: {
                        "fullFeed": {
                            "marketFF": {
                                "ltpc": {"ltp": 150.0},
                                "marketLevel": {"bidAskQuote": [{"bidP": 149.5, "askP": 150.5, "bidQ": 130, "askQ": 130}]},
                            }
                        }
                    },
                    opt_unver: {
                        "fullFeed": {
                            "marketFF": {
                                "ltpc": {"ltp": 120.0},
                                "marketLevel": {"bidAskQuote": [{"bidP": 119.5, "askP": 120.5, "bidQ": 130, "askQ": 130}]},
                            }
                        }
                    },
                }
            }
            p_bk.record_ingress_frame(
                raw_bytes=256,
                is_bytes=True,
                wire_type_int=1,
                data_dict=d_payload,
                recv_wall_ns=w_ns + step_idx * 100_000_000,
                t0_ns=ts_ns,
                t1_ns=ts_ns + 100,
                t2_ns=ts_ns + 200,
                t3_ns=ts_ns + 300,
            )
            p_bk._worker_process_ok_frame(p_bk._inbox.popleft())

        # Frame 3 at t=2,600ms (2.5s later): LTP-ONLY update on `opt_ver` (no valid book for 2,500ms!)
        ts3 = base_ns + 2_600_000_000
        p_bk.record_ingress_frame(
            raw_bytes=96,
            is_bytes=True,
            wire_type_int=1,
            data_dict={"feeds": {opt_ver: {"ltpc": {"ltp": 151.0}}}},
            recv_wall_ns=w_ns + 2_600_000_000,
            t0_ns=ts3,
            t1_ns=ts3 + 100,
            t2_ns=ts3 + 200,
            t3_ns=ts3 + 300,
        )
        p_bk._worker_process_ok_frame(p_bk._inbox.popleft())
        p_bk._capture_enabled = False
        s_bk = p_bk._build_snapshot_from_aggregator()
        c_1_7 = s_bk["cohorts"]["NIFTY_OPTIONS_1_7_DTE"]
        prov = s_bk["codex_feasibility_metrics"]["s1_option_book_and_time_provenance"]
        # 2 verified 1-lot books (`opt_ver`), 2 unverified (`opt_unver`)
        self.assertEqual(c_1_7["book_usable_1lot_updates"], 2)
        self.assertEqual(c_1_7["book_1lot_unverified_lot_updates"], 2)
        # 4 valid book deliveries (`2` on `opt_ver` + `2` on `opt_unver`), but only `2` distinct top quotes (`1` each)!
        self.assertEqual(c_1_7["book_valid_2sided_delivery_updates"], 4)
        self.assertEqual(c_1_7["book_distinct_top_state_changes"], 2)
        self.assertEqual(prov["never_delivered_intended_s1_keys_count"], 1)
        self.assertIn(never_key, prov["never_delivered_intended_s1_keys_sample"])
        self.assertGreaterEqual(prov["per_instrument_worst_valid_book_silence_ms"], 2499.0)

        # 6. O(1) exact zero-gap accounting across an 86,400-second gap (< 5ms execution, exact count)
        p_gap = self._new_probe("o1_zero_gap")
        p_gap._capture_enabled = True
        p_gap._active_epoch_seq = 1
        t_g0 = time.perf_counter_ns()
        p_gap._worker_roll_time_buckets(
            slot_100ms=10, slot_1s=1, slot_1m=1, raw_bytes=100, n_insts=1, s1_updates=1, opt_books=0
        )
        p_gap._worker_roll_time_buckets(
            slot_100ms=864010, slot_1s=86401, slot_1m=1441, raw_bytes=100, n_insts=1, s1_updates=1, opt_books=0
        )
        gap_exec_ms = (time.perf_counter_ns() - t_g0) / 1_000_000.0
        self.assertLess(gap_exec_ms, 10.0)
        self.assertEqual(p_gap._agg_confirmed_zero_1s_bins, 86399)
        self.assertEqual(p_gap._agg_hist_1s_all_frames.count, 86400)

    def test_s205_and_model_registry_immutability(self) -> None:
        """Verifies byte-identical stream quotes/bars (`OFF` vs `ON`) and untouched model artifact SHA-256s."""
        model_paths = [
            _BACKEND_ROOT / "models" / "champion_ensemble.pkl",
            _BACKEND_ROOT / "app" / "analytics" / "option_sniffer_advance_s205" / "models" / "s205_meta_weights.json",
        ]
        before_hashes = {
            str(mp): hashlib.sha256(mp.read_bytes()).hexdigest() for mp in model_paths if mp.is_file()
        }

        wire, sub, con = _build_realistic_wire_frame(target_instruments=20, include_options=6)
        p_off = self._new_probe("iso_off")
        svc_off = _make_isolated_stream_harness(p_off)
        st_off = _make_attached_streamer(p_off, svc_off)
        seen_off: List[int] = []
        svc_off.register_feed_listener(lambda feeds: seen_off.append(len(feeds)))

        p_on = self._new_probe("iso_on")
        svc_on = _make_isolated_stream_harness(p_on)
        st_on = _make_attached_streamer(p_on, svc_on)
        p_on.set_universe_supplier(lambda: (sub, con))
        seen_on: List[int] = []
        svc_on.register_feed_listener(lambda feeds: seen_on.append(len(feeds)))
        p_on.enable()

        with mock.patch.object(_ISOLATED_SVC_NS["time"], "time", return_value=1763102100.5):
            st_off.handle_message(None, wire)
            st_on.handle_message(None, wire)

        p_on.flush_for_test_or_export()
        self.assertEqual(seen_off, [20])
        self.assertEqual(seen_off, seen_on)
        self.assertGreater(len(svc_off._live_quotes), 0)
        self.assertGreater(len(svc_off._minute_bars), 0)
        self.assertEqual(svc_off._live_quotes, svc_on._live_quotes)
        self.assertEqual(dict(svc_off._minute_bars), dict(svc_on._minute_bars))
        self.assertEqual(_AUDIT_VIOLATIONS, [])

        after_hashes = {
            str(mp): hashlib.sha256(mp.read_bytes()).hexdigest() for mp in model_paths if mp.is_file()
        }
        self.assertEqual(before_hashes, after_hashes)


# ──────────────────────────────────────────────────────────────────────────────
# 5. COMPLETE END-TO-END OVERHEAD, GIL INTERFERENCE & ACHIEVED-RATE BENCHMARK
# ──────────────────────────────────────────────────────────────────────────────

def _quantile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(math.ceil(q * len(sorted_vals))) - 1))
    return round(sorted_vals[idx], 2)


def run_synthetic_overhead_benchmark() -> Dict[str, Any]:
    """
    Measures COMPLETE incremental overhead with `gc.enable()` active:
      1. Exact payload byte tiers (`>= 1 KiB`, `>= 16 KiB`, `>= 64 KiB`) — matched-load `OFF` vs `ON`.
      2. External `5 ms` scheduling-canary thread wakeup jitter (`OFF` vs `ON`) to capture worker/GIL interference.
      3. Adversarial slow-supplier + concurrent `json.dumps(get_summary(include_recent_rings=True))` stress.
      4. Paced actual achieved arrival rates (`achieved_hz` & `max_schedule_lateness_ms`) on both compact LTPC
         (`1,000` & `5,000 msg/s`) and `13 KiB` 53-instrument full-book frames (`1,000 msg/s` target).
    """
    gc.enable()
    tiers_cfg = [
        ("small_ge_1kib", 5, 2, 1024, 1200),
        ("medium_ge_16kib", 70, 16, 16384, 800),
        ("large_ge_64kib", 280, 40, 65536, 300),
    ]
    tier_results: Dict[str, Any] = {}

    for label, n_inst, n_opt, min_bytes, iters in tiers_cfg:
        wire, sub, con = _build_realistic_wire_frame(target_instruments=n_inst, include_options=n_opt)
        assert len(wire) >= min_bytes, f"{label} wire size {len(wire)} < {min_bytes}"

        # Baseline OFF (with scheduling canary)
        p_off = S1IngressMeasurementProbe(
            export_dir=_TEST_TMP_ROOT / f"bench_off_{label}", require_streamer_attached_default=False
        )
        svc_off = _make_isolated_stream_harness(p_off)
        st_off = _make_attached_streamer(p_off, svc_off)
        for _ in range(30):
            st_off.handle_message(None, wire)

        stop_c_off = threading.Event()
        canary_off_us: List[float] = []

        def _canary_off() -> None:
            while not stop_c_off.is_set():
                t0 = time.perf_counter_ns()
                time.sleep(0.005)
                canary_off_us.append(max(0.0, (time.perf_counter_ns() - t0 - 5_000_000) / 1000.0))

        tc_off = threading.Thread(target=_canary_off, daemon=True)
        tc_off.start()
        off_cb_us: List[float] = []
        for _ in range(iters):
            t0 = time.perf_counter_ns()
            st_off.handle_message(None, wire)
            off_cb_us.append((time.perf_counter_ns() - t0) / 1000.0)
        stop_c_off.set()
        tc_off.join(timeout=0.5)
        p_off.shutdown_worker()

        # Probe ON (with live worker + concurrent reader + scheduling canary)
        p_on = S1IngressMeasurementProbe(
            export_dir=_TEST_TMP_ROOT / f"bench_on_{label}", require_streamer_attached_default=False
        )
        svc_on = _make_isolated_stream_harness(p_on)
        st_on = _make_attached_streamer(p_on, svc_on)
        p_on.set_universe_supplier(lambda: (sub, con))
        p_on.enable()
        for _ in range(30):
            st_on.handle_message(None, wire)
        p_on.flush_for_test_or_export()

        stop_c_on = threading.Event()
        canary_on_us: List[float] = []

        def _canary_on() -> None:
            while not stop_c_on.is_set():
                t0 = time.perf_counter_ns()
                time.sleep(0.005)
                canary_on_us.append(max(0.0, (time.perf_counter_ns() - t0 - 5_000_000) / 1000.0))

        tc_on = threading.Thread(target=_canary_on, daemon=True)
        tc_on.start()
        on_cb_us: List[float] = []
        for i in range(iters):
            t0 = time.perf_counter_ns()
            st_on.handle_message(None, wire)
            on_cb_us.append((time.perf_counter_ns() - t0) / 1000.0)
            if (i & 7) == 7:
                time.sleep(0.0005)  # Allow background worker slices to interleave realistically under GIL!
        stop_c_on.set()
        tc_on.join(timeout=0.5)
        p_on.flush_for_test_or_export()
        sum_on = p_on.get_summary()
        p_on.disable()
        p_on.shutdown_worker()

        off_cb_us.sort()
        on_cb_us.sort()
        canary_off_us.sort()
        canary_on_us.sort()
        rep_added = sum_on["latencies_us"]["full_probe_added_callback_us"]
        gate = sum_on["codex_feasibility_metrics"]["callback_latency_gates_conservative"]

        tier_results[label] = {
            "wire_bytes": len(wire),
            "instruments": n_inst,
            "iterations": iters,
            "off_total_cb_p50_us": _quantile(off_cb_us, 0.50),
            "off_total_cb_p99_us": _quantile(off_cb_us, 0.99),
            "on_total_cb_p50_us": _quantile(on_cb_us, 0.50),
            "on_total_cb_p99_us": _quantile(on_cb_us, 0.99),
            "matched_p50_delta_us": round(_quantile(on_cb_us, 0.50) - _quantile(off_cb_us, 0.50), 2),
            "reported_full_probe_added_p50_ub_us": rep_added["p50_upper_bound"],
            "reported_full_probe_added_p99_ub_us": rep_added["p99_upper_bound"],
            "reported_full_probe_added_p999_ub_us": rep_added["p999_upper_bound"],
            "reported_full_probe_added_max_us": rep_added["max"],
            "worker_slice_p99_ub_us": gate["worker_slice_p99_upper_bound_us"],
            "canary_5ms_jitter_p99_off_us": _quantile(canary_off_us, 0.99),
            "canary_5ms_jitter_p99_on_us": _quantile(canary_on_us, 0.99),
            "overflow_drops": sum_on["failures_and_bounds"]["inbox_overflow_drops"],
            "safety_gate_pass": gate["passes_p99_le_100us_and_p999_le_1000us"],
        }

    # Slow supplier + concurrent summary JSON reader stress (reproducing Codex `gil_and_reader`)
    wire53, sub53, con53 = _build_realistic_wire_frame(target_instruments=53, include_options=12)
    p_stress = S1IngressMeasurementProbe(
        export_dir=_TEST_TMP_ROOT / "bench_gil_stress", require_streamer_attached_default=False
    )
    svc_st = _make_isolated_stream_harness(p_stress)
    st_st = _make_attached_streamer(p_stress, svc_st)
    p_stress._agg_universe_refresh_interval_ns = 0  # Attempt to force 0ns refresh; hard 5s floor must protect GIL!
    sup_calls = [0]

    def _slow_supplier() -> Tuple[Set[str], Dict[str, Dict[str, Any]]]:
        sup_calls[0] += 1
        acc = 0
        for i in range(120000):
            acc += i
        return sub53, con53

    p_stress.set_universe_supplier(_slow_supplier)
    p_stress.enable()
    stop_rd = threading.Event()
    rd_count = [0]

    def _reader() -> None:
        while not stop_rd.is_set():
            json.dumps(p_stress.get_summary(include_recent_rings=True))
            rd_count[0] += 1
            time.sleep(0.002)

    t_rd = threading.Thread(target=_reader, daemon=True)
    t_rd.start()
    stress_cb_us: List[float] = []
    for _ in range(300):
        t0 = time.perf_counter_ns()
        st_st.handle_message(None, wire53)
        stress_cb_us.append((time.perf_counter_ns() - t0) / 1000.0)
    stop_rd.set()
    t_rd.join(timeout=0.5)
    p_stress.flush_for_test_or_export()
    sum_stress = p_stress.get_summary()
    p_stress.disable()
    p_stress.shutdown_worker()
    stress_cb_us.sort()

    # Paced actual achieved arrival-rate tests (reporting TRUE achieved Hz & schedule lateness)
    ltpc_wire, ltpc_sub, ltpc_con = _build_compact_ltpc_wire_frame()
    paced_rates: List[Dict[str, Any]] = []
    for profile_name, p_wire, p_sub, p_con, target_hz, count in (
        ("compact_ltpc_1000hz", ltpc_wire, ltpc_sub, ltpc_con, 1000, 300),
        ("compact_ltpc_5000hz", ltpc_wire, ltpc_sub, ltpc_con, 5000, 1000),
        ("full_53inst_13kib_1000hz_target", wire53, sub53, con53, 1000, 150),
    ):
        p_r = S1IngressMeasurementProbe(
            export_dir=_TEST_TMP_ROOT / f"rate_{profile_name}", require_streamer_attached_default=False
        )
        svc_r = _make_isolated_stream_harness(p_r)
        st_r = _make_attached_streamer(p_r, svc_r)
        p_r.set_universe_supplier(lambda: (p_sub, p_con))
        p_r.enable()
        start_ns = time.perf_counter_ns()
        lateness_ms: List[float] = []
        step_ns = int(1_000_000_000 / target_hz)
        for i in range(count):
            due_ns = start_ns + i * step_ns
            while time.perf_counter_ns() < due_ns:
                pass
            lateness_ms.append(max(0.0, (time.perf_counter_ns() - due_ns) / 1_000_000.0))
            st_r.handle_message(None, p_wire)
        elapsed_s = max(1e-9, (time.perf_counter_ns() - start_ns) / 1_000_000_000.0)
        p_r.flush_for_test_or_export()
        s_r = p_r.get_summary()
        p_r.disable()
        p_r.shutdown_worker()
        paced_rates.append(
            {
                "profile": profile_name,
                "wire_bytes": len(p_wire),
                "target_hz": target_hz,
                "achieved_hz": round(count / elapsed_s, 1),
                "max_schedule_lateness_ms": round(max(lateness_ms), 3),
                "overflow_drops": s_r["failures_and_bounds"]["inbox_overflow_drops"],
                "full_probe_added_p99_ub_us": s_r["latencies_us"]["full_probe_added_callback_us"]["p99_upper_bound"],
            }
        )

    # Exact 1-session on-disk footprint * 5 sessions capacity report
    cap_dir = _TEST_TMP_ROOT / "bench_1session_cap"
    with mock.patch.object(os, "fsync", return_value=None):
        p_cap = S1IngressMeasurementProbe(export_dir=cap_dir, require_streamer_attached_default=False)
        p_cap.set_attachment_verifier(lambda: {"wrapper_attached": True})
        p_cap.set_universe_supplier(lambda: (sub53, con53))
        p_cap.enable()
        nc0 = time.perf_counter_ns()
        p_cap.record_ingress_frame(
            raw_bytes=len(wire53),
            is_bytes=True,
            wire_type_int=1,
            wire_payload=wire53,
            recv_wall_ns=time.time_ns(),
            t0_ns=nc0,
            t1_ns=nc0 + 1000,
            t2_ns=nc0 + 2000,
            t3_ns=nc0 + 3000,
        )
        p_cap.flush_for_test_or_export()
        for m_idx in range(375):
            p_cap._worker_emit_completed_1m_rollup(
                {
                    "epoch_minute": 29900000 + m_idx,
                    "utc_minute": "2026-11-16T03:45:00Z",
                    "frames": 480,
                    "bytes": 6200000,
                    "instruments": 25440,
                    "s1_updates": 8160,
                    "s1_option_books": 5760,
                    "partial_finalized": False,
                }
            )
            if m_idx % 3 == 2:
                p_cap._worker_export_compact_record("PERIODIC_CHECKPOINT")
        p_cap.disable()
        p_cap.shutdown_worker()
    one_sess_bytes = sum(f.stat().st_size for f in cap_dir.iterdir() if f.is_file())

    return {
        "gc_enabled": gc.isenabled(),
        "tiers": tier_results,
        "slow_supplier_and_reader_stress": {
            "supplier_calls_during_300_frames": sup_calls[0],
            "concurrent_summary_reads": rd_count[0],
            "total_cb_p50_us": _quantile(stress_cb_us, 0.50),
            "total_cb_p99_us": _quantile(stress_cb_us, 0.99),
            "total_cb_max_us": round(stress_cb_us[-1], 2),
            "full_probe_added_p99_ub_us": sum_stress["latencies_us"]["full_probe_added_callback_us"]["p99_upper_bound"],
        },
        "paced_actual_rates": paced_rates,
        "five_session_storage_capacity": {
            "one_22500s_session_actual_disk_bytes": one_sess_bytes,
            "five_sessions_disk_bytes": one_sess_bytes * 5,
            "five_sessions_mib": round((one_sess_bytes * 5) / (1024.0 * 1024.0), 2),
            "quota_max_bytes": MAX_EXPORT_STORAGE_BYTES,
            "quota_utilization_pct": round((one_sess_bytes * 5 * 100.0) / float(MAX_EXPORT_STORAGE_BYTES), 2),
        },
        "audit_violations": list(_AUDIT_VIOLATIONS),
    }


# Backwards-compatible benchmark entrypoint alias
run_realistic_synthetic_benchmark = run_synthetic_overhead_benchmark


def _cleanup_tmp_root_abs(root: Path) -> None:
    if not root.exists():
        return
    for p in sorted(root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        try:
            if p.is_file() or p.is_symlink():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


if __name__ == "__main__":
    run_bench = "--benchmark" in sys.argv
    argv = [a for a in sys.argv if a != "--benchmark"]
    try:
        suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        if not result.wasSuccessful():
            sys.exit(1)
        if run_bench:
            bench = run_synthetic_overhead_benchmark()
            print("\n=== S1 INGRESS PROBE V3 COMPLETE OVERHEAD & INTERFERENCE BENCHMARK ===")
            print(json.dumps(bench, indent=2))
            for tname, tinfo in bench["tiers"].items():
                assert tinfo["safety_gate_pass"], f"Safety latency gate failed on {tname}: {tinfo}"
                assert tinfo["reported_full_probe_added_p99_ub_us"] <= 100.0, f"p99 > 100us on {tname}: {tinfo}"
                assert tinfo["reported_full_probe_added_p999_ub_us"] <= 1000.0, f"p999 > 1000us on {tname}: {tinfo}"
    finally:
        _cleanup_tmp_root_abs(_TEST_TMP_ROOT)
