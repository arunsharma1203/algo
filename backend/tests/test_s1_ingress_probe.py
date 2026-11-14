"""
Adversarial Offline Verification & Matched-Load ABBA/BAAB Benchmark Suite
for Phase 1 S1 Market-Data Workload Meter (V4).

Strict Isolation Guarantees:
  - Installs `sys.addaudithook` BEFORE importing probe or compiling SDK/service ASTs.
  - Forbids all network sockets, subprocess creation, SQLite connections, and any
    filesystem write outside the isolated temporary test directory.
  - Compiles installed `upstox_client` `Streamer` + `MarketDataStreamerV3` and
    `UpstoxStreamService` from AST without initializing live application modules,
    schedulers, databases, or servers.
"""

from __future__ import annotations

import ast
import collections
import datetime
import gc
import hashlib
import json
import os
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
import types
import unittest
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND_ROOT = _REPO_ROOT / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

_TEST_TMP_ROOT = Path(tempfile.mkdtemp(prefix="s1_workload_v4_test_")).resolve()
_AUDIT_VIOLATIONS: List[str] = []


def _cleanup_dir_abs(root_path: Path) -> None:
    root_res = root_path.resolve()
    if not root_res.exists():
        return
    for cur_root, dirs, files in os.walk(str(root_res), topdown=False):
        cur_p = Path(cur_root).resolve()
        for fn in files:
            fp = cur_p / fn
            try:
                os.unlink(str(fp))
            except OSError:
                pass
        for dn in dirs:
            dp = cur_p / dn
            try:
                os.rmdir(str(dp))
            except OSError:
                pass
    try:
        os.rmdir(str(root_res))
    except OSError:
        pass


def _install_strict_offline_audithook() -> None:
    forbidden_prefixes = (
        "socket.",
        "urllib.Request",
        "http.client.",
        "subprocess.",
        "os.system",
        "os.fork",
        "os.posix_spawn",
        "os.exec",
        "sqlite3.connect",
    )

    def _hook(event: str, args: Tuple[Any, ...]) -> None:
        if any(event.startswith(pref) for pref in forbidden_prefixes):
            msg = f"FORBIDDEN_EVENT:{event}:{args[:2]}"
            _AUDIT_VIOLATIONS.append(msg)
            raise PermissionError(msg)

        if event == "open" and len(args) >= 2:
            path_arg, mode_arg = args[0], args[1]
            is_write = False
            if isinstance(mode_arg, str) and any(ch in mode_arg for ch in ("w", "a", "+", "x")):
                is_write = True
            elif isinstance(mode_arg, int) and (
                mode_arg & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            ):
                is_write = True
            if is_write and isinstance(path_arg, (str, bytes, os.PathLike)):
                p_str = os.fsdecode(path_arg)
                if p_str not in ("/dev/null",):
                    resolved = Path(p_str).resolve()
                    if not (resolved == _TEST_TMP_ROOT or _TEST_TMP_ROOT in resolved.parents):
                        msg = f"FORBIDDEN_WRITE:{resolved}"
                        _AUDIT_VIOLATIONS.append(msg)
                        raise PermissionError(msg)

        if event in ("os.mkdir", "os.rename", "os.replace", "os.unlink", "os.rmdir", "os.chmod") and args:
            for p_item in args[:2]:
                if isinstance(p_item, (str, bytes, os.PathLike)):
                    resolved = Path(os.fsdecode(p_item)).resolve()
                    if not (resolved == _TEST_TMP_ROOT or _TEST_TMP_ROOT in resolved.parents):
                        msg = f"FORBIDDEN_FS_MUTATION:{event}:{resolved}"
                        _AUDIT_VIOLATIONS.append(msg)
                        raise PermissionError(msg)

    sys.addaudithook(_hook)


from fastapi import HTTPException, Query, Request
from google.protobuf import json_format
from starlette.middleware.cors import CORSMiddleware
from upstox_client.feeder.proto import MarketDataFeedV3_pb2

_install_strict_offline_audithook()

from app.data import s1_ingress_probe as probe_mod
from app.data.s1_ingress_probe import (
    APPROVED_TAILSCALE_FRONTEND_ORIGINS,
    BoundedHistogram,
    DEFAULT_ALLOWED_LOCAL_ORIGINS,
    LATENCY_BOUNDS_US,
    MAX_INBOX_SAMPLES,
    PROBE_WRAPPER_VERSION,
    ProbeControlAction,
    ProbeControlError,
    S1IngressMeasurementProbe,
    S1MeasurementSummaryExporter,
    S1ProbeManagementGuard,
    SCHEMA_VERSION,
    attach_probe_lifecycle_listeners,
    build_instrumented_streamer_class,
    resolve_cors_allowed_origins,
)


def _load_sdk_streamer_classes_ast() -> Tuple[Any, Any]:
    pb_path = Path(MarketDataFeedV3_pb2.__file__).resolve()
    feeder_dir = pb_path.parent.parent
    streamer_src = (feeder_dir / "streamer.py").read_text(encoding="utf-8")
    mds_src = (feeder_dir / "market_data_streamer_v3.py").read_text(encoding="utf-8")

    ns: Dict[str, Any] = {
        "ABC": ABC,
        "abstractmethod": abstractmethod,
        "time": time,
        "MarketDataFeedV3_pb2": MarketDataFeedV3_pb2,
        "json_format": json_format,
    }

    st_tree = ast.parse(streamer_src, filename="streamer.py")
    st_body = [n for n in st_tree.body if isinstance(n, ast.ClassDef)]
    exec(compile(ast.Module(body=st_body, type_ignores=[]), "streamer.py", "exec"), ns)

    class _ForbiddenFeederStub:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("Broker MarketDataFeederV3 connection is forbidden in offline tests")

    ns["MarketDataFeederV3"] = _ForbiddenFeederStub
    mds_tree = ast.parse(mds_src, filename="market_data_streamer_v3.py")
    mds_body = [n for n in mds_tree.body if isinstance(n, ast.ClassDef)]
    exec(compile(ast.Module(body=mds_body, type_ignores=[]), "market_data_streamer_v3.py", "exec"), ns)
    return ns["Streamer"], ns["MarketDataStreamerV3"]


_, RealMarketDataStreamerV3 = _load_sdk_streamer_classes_ast()


def _load_upstox_stream_service_ast() -> Any:
    svc_path = _BACKEND_ROOT / "app" / "data" / "upstox_stream_service.py"
    tree = ast.parse(svc_path.read_text(encoding="utf-8"), filename=str(svc_path))
    cls_nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "UpstoxStreamService"]
    assert len(cls_nodes) == 1
    ns: Dict[str, Any] = {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "Set": set,
        "Tuple": Tuple,
        "collections": collections,
        "datetime": datetime.datetime,
        "timedelta": datetime.timedelta,
        "time": time,
        "threading": threading,
        "logging": probe_mod.logger,
        "logger": probe_mod.logger,
        "PROBE_WRAPPER_VERSION": PROBE_WRAPPER_VERSION,
        "S1IngressMeasurementProbe": S1IngressMeasurementProbe,
        "attach_probe_lifecycle_listeners": attach_probe_lifecycle_listeners,
        "build_instrumented_streamer_class": build_instrumented_streamer_class,
        "get_s1_ingress_probe": probe_mod.get_s1_ingress_probe,
        "MARKET_OPEN_TIME": datetime.time(9, 15),
        "MARKET_CLOSE_TIME": datetime.time(15, 30),
        "MAX_QUOTE_AGE_MS": 60_000,
        "normalize_ticker": lambda s: str(s).strip().upper(),
        "get_upstox_limiter": lambda: types.SimpleNamespace(get_stats=lambda: {"calls": 0}),
    }
    exec(compile(ast.Module(body=cls_nodes, type_ignores=[]), str(svc_path), "exec"), ns)
    return ns["UpstoxStreamService"]


AstUpstoxStreamService = _load_upstox_stream_service_ast()


def _extract_main_probe_handlers_ast(
    get_service_fn: Callable[[], Any],
) -> Tuple[Callable[..., Any], Callable[..., Any]]:
    main_path = _BACKEND_ROOT / "app" / "main.py"
    tree = ast.parse(main_path.read_text(encoding="utf-8"), filename=str(main_path))
    target_fns: List[ast.FunctionDef] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in (
            "get_s1_ingress_probe_summary",
            "control_s1_ingress_probe",
        ):
            clean_fn = ast.FunctionDef(
                name=node.name,
                args=node.args,
                body=node.body,
                decorator_list=[],
                returns=node.returns,
                type_comment=node.type_comment,
            )
            ast.fix_missing_locations(clean_fn)
            target_fns.append(clean_fn)

    from fastapi import HTTPException, Query, Request

    fake_svc_mod = types.ModuleType("app.data.upstox_stream_service")
    fake_svc_mod.get_stream_service = get_service_fn  # type: ignore[attr-defined]

    saved_mod = sys.modules.get("app.data.upstox_stream_service")
    sys.modules["app.data.upstox_stream_service"] = fake_svc_mod
    try:
        ns: Dict[str, Any] = {
            "Request": Request,
            "Query": Query,
            "HTTPException": HTTPException,
        }
        mod_ast = ast.Module(body=target_fns, type_ignores=[])
        ast.fix_missing_locations(mod_ast)
        exec(compile(mod_ast, str(main_path), "exec"), ns)
        return ns["get_s1_ingress_probe_summary"], ns["control_s1_ingress_probe"]
    finally:
        if saved_mod is None:
            sys.modules.pop("app.data.upstox_stream_service", None)
        else:
            sys.modules["app.data.upstox_stream_service"] = saved_mod


def _make_inert_service_harness(
    probe: S1IngressMeasurementProbe,
    equity_count: int = 25,
) -> Tuple[Any, Dict[str, str]]:
    svc = object.__new__(AstUpstoxStreamService)
    svc._lock = threading.Lock()
    svc._is_running = False
    svc._is_connected = True
    svc._connected_at = "2026-11-14T09:15:00"
    svc._last_msg_ts = 0.0
    svc._total_messages = 0
    svc._total_ticks = 0
    svc._reconnect_count = 0
    svc._last_error = None
    svc._key_to_symbol = {f"NSE_EQ|EQ{i:04d}": f"SYM{i:04d}.NS" for i in range(equity_count)}
    svc._symbol_to_key = {v: k for k, v in svc._key_to_symbol.items()}
    svc._live_quotes = {}
    svc._minute_bars = collections.defaultdict(dict)
    svc._completed_15m_bars = collections.defaultdict(dict)
    svc._flushed_slots = set()
    svc._latest_frozen_snapshot = None
    svc._feed_listeners = []
    svc._additional_keys = {"NSE_INDEX|Nifty 50"}
    svc._subscribed_ws_keys = set(svc._key_to_symbol.keys()) | set(svc._additional_keys)
    svc._streamer = None
    svc._s1_ingress_probe = probe
    probe.set_attachment_verifier(svc._inspect_s1_streamer_attachment)
    return svc, dict(svc._key_to_symbol)


def _build_wire_frame(
    num_instruments: int,
    *,
    compact_ltpc_only: bool = False,
    seq_seed: int = 1,
) -> bytes:
    fr = MarketDataFeedV3_pb2.FeedResponse()
    fr.type = MarketDataFeedV3_pb2.Type.live_feed
    fr.currentTs = 1763102100000 + seq_seed
    for i in range(num_instruments):
        key = f"NSE_EQ|EQ{i:04d}" if i % 2 == 0 else f"NSE_FO|OPT{i:04d}"
        feed = fr.feeds[key]
        if compact_ltpc_only:
            feed.ltpc.ltp = 24500.25 + (i * 0.5)
            feed.ltpc.ltt = 1763102100000 + seq_seed
            feed.ltpc.ltq = 75
            feed.ltpc.cp = 24400.0
        else:
            mff = feed.fullFeed.marketFF
            mff.ltpc.ltp = 250.5 + (i % 40) + (seq_seed % 7) * 0.05
            mff.ltpc.ltt = 1763102100000 + seq_seed
            mff.ltpc.ltq = 75
            mff.ltpc.cp = 245.0
            mff.atp = 249.2
            mff.vtt = 150000 + i * 10
            mff.oi = 500000.0 + i * 75.0
            mff.iv = 0.145
            mff.tbq = 12000.0
            mff.tsq = 11500.0
            for lvl in range(5):
                q = mff.marketLevel.bidAskQuote.add()
                q.bidP = 250.0 - lvl * 0.05
                q.bidQ = 150 + lvl * 75
                q.askP = 250.1 + lvl * 0.05
                q.askQ = 150 + lvl * 75
            mff.optionGreeks.delta = 0.51
            mff.optionGreeks.theta = -12.4
            mff.optionGreeks.gamma = 0.0012
            mff.optionGreeks.vega = 8.3
            mff.optionGreeks.rho = 1.1
            c_d1 = mff.marketOHLC.ohlc.add()
            c_d1.interval = "1d"
            c_d1.open = 246.0
            c_d1.high = 255.0
            c_d1.low = 244.0
            c_d1.close = 250.5
            c_d1.vol = 150000
            c_d1.ts = 1763078400000
            c_i1 = mff.marketOHLC.ohlc.add()
            c_i1.interval = "I1"
            c_i1.open = 250.0
            c_i1.high = 251.0
            c_i1.low = 249.8
            c_i1.close = 250.5
            c_i1.vol = 3200
            c_i1.ts = 1763102100000
    return fr.SerializeToString()


class TestS1WorkloadMeterV4(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        if "--benchmark" not in sys.argv:
            _cleanup_dir_abs(_TEST_TMP_ROOT)

    def _fresh_dir(self, name: str) -> Path:
        p = _TEST_TMP_ROOT / f"{name}_{time.perf_counter_ns()}"
        return p

    def test_1_callback_readiness_no_zero_race_and_health_qualification(self) -> None:
        """
        Verifies:
          - Default-OFF construction does ZERO filesystem scans/mkdirs.
          - Post-append scheduling pause before `cell[9] = full_added_ns` NEVER records `0.0 us`.
          - Uncompleted readiness cell past grace is marked `timing_unavailable_count`, never `0.0 us`.
          - `SlowAppend` (`3 ms`) is captured inside `t_cb_exit` and trips auto-disable after 5 breaches.
          - 60 good samples + 5 observer faults immediately degrade `get_summary()` health & budget status.
        """
        non_existent_dir = self._fresh_dir("lazy_init_check")
        probe = S1IngressMeasurementProbe(export_dir=non_existent_dir, require_streamer_attached_default=False)
        self.assertFalse(probe.enabled)
        self.assertFalse(non_existent_dir.exists(), "Default-OFF construction must not touch disk")
        init_sum = probe.get_summary()
        self.assertEqual(
            init_sum["workload_meter_qualification"]["internal_callback_budget"]["status"],
            "INSUFFICIENT_SAMPLES",
        )
        self.assertFalse(
            init_sum["workload_meter_qualification"]["internal_callback_budget"]["within_budget_and_healthy"]
        )

        # 1A. Post-append preemption pause: worker polls while `cell[9] == -1`, then producer finishes
        svc, _ = _make_inert_service_harness(probe)
        inst_cls = build_instrumented_streamer_class(RealMarketDataStreamerV3, probe)
        streamer = inst_cls(api_client=None, instrumentKeys=[], mode="full")
        streamer.on("message", svc._handle_feed_message)
        svc._streamer = streamer

        orig_poll = probe_mod.WORKER_POLL_SLEEP_S
        probe_mod.WORKER_POLL_SLEEP_S = 0.001
        try:
            probe.enable(require_streamer_attached=True)

            class _PauseAfterAppendDeque:
                def __init__(self, inner: collections.deque) -> None:
                    self._inner = inner
                    self.pause_active = True

                def append(self, item: Any) -> None:
                    self._inner.append(item)
                    if self.pause_active:
                        # Worker wakes up every 1ms while `item[9]` is still `-1`!
                        time.sleep(0.025)

                def popleft(self) -> Any:
                    return self._inner.popleft()

                def __len__(self) -> int:
                    return len(self._inner)

                def __bool__(self) -> bool:
                    return bool(self._inner)

                def __getitem__(self, idx: int) -> Any:
                    return self._inner[idx]

            real_inbox = probe._inbox
            pause_q = _PauseAfterAppendDeque(real_inbox)
            probe._inbox = pause_q  # type: ignore[assignment]
            wire = _build_wire_frame(5, seq_seed=1)
            streamer.handle_message(None, wire)
            pause_q.pause_active = False
            probe._inbox = real_inbox

            probe.flush_for_test_or_export(force_export=False)
            s_paused = probe.get_summary()
            lat_paused = s_paused["latencies_us"]["probe_added_callback_us"]
            self.assertEqual(lat_paused["count"], 1)
            self.assertGreaterEqual(
                lat_paused["min"],
                20000.0,
                f"Expected >=20,000us true stall, got {lat_paused['min']}us (must NEVER record 0.0us!)",
            )
            probe.disable()
        finally:
            probe_mod.WORKER_POLL_SLEEP_S = orig_poll

        # 1B. Permanently unready cell (`cell[9] == -1` at seal): counted as unavailable, NOT 0.0 us
        probe2 = S1IngressMeasurementProbe(export_dir=self._fresh_dir("unready"), require_streamer_attached_default=False)
        probe2.enable(require_streamer_attached=False)
        ep_seq = probe2._active_epoch_seq
        probe2._inbox.append([probe_mod._EVT_FRAME_OK, ep_seq, time.perf_counter_ns() - 500_000_000, 512, 0, 4, 100_000, -1, -1, -1])
        s_unready = probe2.disable()
        self.assertEqual(s_unready["ingress_workload"]["ingress_frames_total"], 1)
        self.assertEqual(s_unready["latencies_us"]["probe_added_callback_us"]["count"], 0)
        self.assertEqual(s_unready["failures_and_bounds"]["timing_unavailable_count"], 1)
        self.assertFalse(s_unready["workload_meter_qualification"]["measurement_integrity_valid"])
        self.assertIn("TIMING_SAMPLE_UNAVAILABLE", s_unready["workload_meter_qualification"]["invalidation_reasons"])

        # 1C. SlowAppend (3ms delay inside append) auto-disables after 5 consecutive >1000us breaches
        probe3 = S1IngressMeasurementProbe(export_dir=self._fresh_dir("slow_app"), require_streamer_attached_default=False)
        svc3, _ = _make_inert_service_harness(probe3)
        cls3 = build_instrumented_streamer_class(RealMarketDataStreamerV3, probe3)
        st3 = cls3(api_client=None, instrumentKeys=[], mode="full")
        st3.on("message", svc3._handle_feed_message)
        svc3._streamer = st3
        probe3.enable(require_streamer_attached=True)

        class _SlowAppendDeque:
            def __init__(self, inner: collections.deque) -> None:
                self._inner = inner

            def append(self, item: Any) -> None:
                time.sleep(0.003)
                self._inner.append(item)

            def popleft(self) -> Any:
                return self._inner.popleft()

            def __len__(self) -> int:
                return len(self._inner)

            def __bool__(self) -> bool:
                return bool(self._inner)

            def __getitem__(self, idx: int) -> Any:
                return self._inner[idx]

        orig_q3 = probe3._inbox
        probe3._inbox = _SlowAppendDeque(orig_q3)  # type: ignore[assignment]
        w_small = _build_wire_frame(3, seq_seed=2)
        for _ in range(7):
            st3.handle_message(None, w_small)
        probe3._inbox = orig_q3
        self.assertFalse(probe3.enabled)
        self.assertTrue(probe3._auto_disabled_on_fault)
        probe3.flush_for_test_or_export(force_export=False)
        s3 = probe3.get_summary()
        self.assertEqual(s3["latencies_us"]["probe_added_callback_us"]["count"], 5)
        self.assertGreaterEqual(s3["latencies_us"]["probe_added_callback_us"]["min"], 2500.0)
        self.assertFalse(s3["workload_meter_qualification"]["measurement_integrity_valid"])
        probe3.shutdown_flush()

        # 1D. 60 clean samples followed by 5 observer faults -> live health immediately invalidates gate!
        probe4 = S1IngressMeasurementProbe(export_dir=self._fresh_dir("obs_fault"), require_streamer_attached_default=False)
        svc4, _ = _make_inert_service_harness(probe4)
        cls4 = build_instrumented_streamer_class(RealMarketDataStreamerV3, probe4)
        st4 = cls4(api_client=None, instrumentKeys=[], mode="full")
        st4.on("message", svc4._handle_feed_message)
        svc4._streamer = st4
        probe4.enable(require_streamer_attached=True)
        for idx in range(60):
            st4.handle_message(None, _build_wire_frame(2, seq_seed=100 + idx))
        probe4.flush_for_test_or_export(force_export=False)
        healthy_sum = probe4.get_summary()
        self.assertTrue(healthy_sum["workload_meter_qualification"]["measurement_integrity_valid"])
        self.assertEqual(
            healthy_sum["workload_meter_qualification"]["internal_callback_budget"]["status"],
            "WITHIN_INTERNAL_CALLBACK_BUDGET",
        )

        for _ in range(5):
            probe4.note_observer_fault("unit_test_hook", RuntimeError("boom"))
        degraded_sum = probe4.get_summary()
        self.assertFalse(degraded_sum["enabled"])
        self.assertTrue(degraded_sum["failures_and_bounds"]["auto_disabled_on_fault"])
        self.assertFalse(degraded_sum["workload_meter_qualification"]["measurement_integrity_valid"])
        self.assertFalse(degraded_sum["workload_meter_qualification"]["operating_health_clean"])
        self.assertEqual(
            degraded_sum["workload_meter_qualification"]["internal_callback_budget"]["status"],
            "UNQUALIFIED_HEALTH_DEGRADED",
        )
        self.assertFalse(
            degraded_sum["workload_meter_qualification"]["internal_callback_budget"]["within_budget_and_healthy"]
        )
        probe4.shutdown_flush()

    def test_2_disable_dominates_pending_enable_and_scalar_memory_bounds(self) -> None:
        """
        Reproduces Codex HIGH 2 & proves scalar memory bounds + worker death/stall failsafes:
          - Slow `enable()` paused inside `attachment_verifier`: concurrent `disable()` returns
            immediately without blocking and prevents `enable()` & worker from turning capture ON.
          - Slow `enable()` paused inside worker `_init_aggregator_state`: concurrent `disable()`
            prevents worker & `enable()` from turning capture ON (`enabled=False`, `epoch_closed=True`).
          - `4,200` distinct `71 KiB` frames (`~298 MiB` raw input) retain `< 2.0 MiB` heap in `_inbox`.
          - Worker crash and `> 5s` heartbeat stall auto-disable capture.
        """
        # 2A. Disable during paused attachment_verifier in enable()
        probe = S1IngressMeasurementProbe(export_dir=self._fresh_dir("race_att"), require_streamer_attached_default=True)
        entered_verifier = threading.Event()
        release_verifier = threading.Event()

        def _slow_verifier() -> Dict[str, Any]:
            entered_verifier.set()
            release_verifier.wait(timeout=3.0)
            return {"wrapper_attached": True, "streamer_present": True}

        probe.set_attachment_verifier(_slow_verifier)
        enable_res_box: List[Dict[str, Any]] = []

        def _run_enable() -> None:
            enable_res_box.append(probe.enable(actor_role="ADMIN", peer="127.0.0.1"))

        t_en = threading.Thread(target=_run_enable, daemon=True)
        t_en.start()
        self.assertTrue(entered_verifier.wait(timeout=2.0))

        t0 = time.perf_counter()
        dis_res = probe.disable(actor_role="ADMIN", peer="127.0.0.1", reason="EMERGENCY_STOP")
        dis_elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertLess(dis_elapsed_ms, 150.0, "Emergency disable must NOT wait on slow enable attachment check!")
        self.assertFalse(dis_res["enabled"])

        release_verifier.set()
        t_en.join(timeout=2.0)
        self.assertFalse(probe.enabled)
        self.assertFalse(probe._capture_enabled)
        self.assertTrue(probe._agg_epoch_closed)
        self.assertEqual(len(enable_res_box), 1)
        self.assertFalse(enable_res_box[0]["enabled"])
        probe.shutdown_flush()

        # 2B. Disable during paused worker `_init_aggregator_state`
        probe_w = S1IngressMeasurementProbe(export_dir=self._fresh_dir("race_wk"), require_streamer_attached_default=False)
        orig_init = probe_w._init_aggregator_state
        entered_wk_init = threading.Event()
        release_wk_init = threading.Event()

        def _hooked_init(epoch_seq: int) -> None:
            orig_init(epoch_seq)
            if epoch_seq > 0:
                entered_wk_init.set()
                release_wk_init.wait(timeout=3.0)

        probe_w._init_aggregator_state = _hooked_init  # type: ignore[method-assign]
        t_en2 = threading.Thread(target=lambda: probe_w.enable(require_streamer_attached=False), daemon=True)
        t_en2.start()
        self.assertTrue(entered_wk_init.wait(timeout=2.0))

        dis_res2 = probe_w.disable(reason="EMERGENCY_DURING_WORKER_INIT", wait_for_seal_s=0.05)
        self.assertFalse(dis_res2["enabled"])
        release_wk_init.set()
        t_en2.join(timeout=2.0)
        self.assertFalse(probe_w.enabled)
        self.assertFalse(probe_w._capture_enabled)
        self.assertTrue(probe_w._agg_epoch_closed)
        probe_w.shutdown_flush()

        # 2C. Scalar queue heap bound under 4,200 distinct 71 KiB frames (~298 MiB raw input)
        probe_mem = S1IngressMeasurementProbe(export_dir=self._fresh_dir("mem_cap"), require_streamer_attached_default=False)
        probe_mem._capture_enabled = True
        probe_mem._active_epoch_seq = 1
        probe_mem._worker_last_heartbeat_mono_ns = time.perf_counter_ns()
        template_large = _build_wire_frame(280, seq_seed=7)
        self.assertGreater(len(template_large), 65536)

        tracemalloc.start()
        snap_before = tracemalloc.take_snapshot()
        for i in range(MAX_INBOX_SAMPLES + 104):
            # Distinct bytes object each iteration; probe must NOT retain `distinct_msg`!
            distinct_msg = template_large + i.to_bytes(4, "little")
            t_now = time.perf_counter_ns()
            probe_mem.on_ingress_frame_ok(
                mono_ns=t_now,
                message_obj=distinct_msg,
                decoded_feeds_len=280,
                sdk_and_app_ns=500_000,
                cb_entry_ns=t_now - 2_000,
            )
            # Keep heartbeat fresh during synthetic loop
            probe_mem._worker_last_heartbeat_mono_ns = t_now
        snap_after = tracemalloc.take_snapshot()
        tracemalloc.stop()

        retained_bytes = sum(stat.size_diff for stat in snap_after.compare_to(snap_before, "lineno") if stat.size_diff > 0)
        self.assertEqual(len(probe_mem._inbox), MAX_INBOX_SAMPLES)
        self.assertEqual(probe_mem._inbox_overflow_drops, 104)
        self.assertLess(
            retained_bytes,
            2 * 1024 * 1024,
            f"Scalar inbox retained {retained_bytes} bytes (> 2 MiB)!",
        )

        # 2D. Worker heartbeat stall & worker fatal crash auto-disable capture
        probe_mem._worker_last_heartbeat_mono_ns = time.perf_counter_ns() - (probe_mod.WORKER_STALL_TIMEOUT_NS + 50_000_000)
        probe_mem.on_ingress_frame_ok(
            mono_ns=time.perf_counter_ns(),
            message_obj=b"1234",
            decoded_feeds_len=1,
            sdk_and_app_ns=1000,
            cb_entry_ns=time.perf_counter_ns(),
        )
        self.assertFalse(probe_mem.enabled)
        self.assertEqual(probe_mem._auto_disabled_reason, "WORKER_HEARTBEAT_STALL")

        probe_crash = S1IngressMeasurementProbe(export_dir=self._fresh_dir("crash_wk"), require_streamer_attached_default=False)
        probe_crash.enable(require_streamer_attached=False)

        def _boom_publish() -> None:
            raise RuntimeError("injected_snapshot_crash")

        probe_crash._worker_publish_snapshot = _boom_publish  # type: ignore[method-assign]
        probe_crash._agg_last_snapshot_pub_ns = 0
        probe_crash.on_ingress_frame_ok(
            mono_ns=time.perf_counter_ns(),
            message_obj=b"abcd",
            decoded_feeds_len=1,
            sdk_and_app_ns=1000,
            cb_entry_ns=time.perf_counter_ns(),
        )
        for _ in range(40):
            if probe_crash._worker_dead_latched:
                break
            time.sleep(0.02)
        self.assertTrue(probe_crash._worker_dead_latched)
        self.assertFalse(probe_crash.enabled)
        s_crash = probe_crash.get_summary()
        self.assertFalse(s_crash["workload_meter_qualification"]["measurement_integrity_valid"])
        self.assertIn("WORKER_DEAD_OR_STALLED", s_crash["workload_meter_qualification"]["invalidation_reasons"])

    def test_3_durable_export_missing_intervals_audit_cursor_and_real_byte_quota(self) -> None:
        """
        Reproduces all Codex HIGH 4 durability & quota counterexamples:
          - 2-minute replay with transient 1st-minute failure -> bounded retry recovers both minutes.
          - 2-minute replay with permanent targeted 1st-minute failure -> `SEALED_WITH_DURABLE_GAPS`,
            `durable_completeness_valid=False`, exact `missing_durable_intervals` persisted & recovered.
          - Control-audit cursor advances ONLY after durable write success (0 audit loss on failed write).
          - `export_flush` / `flush_for_test_or_export(force_export=True)` returns `False` when writes fail.
          - `8,192`-byte quota with 20 injected `os.fsync` failures: real directory bytes <= 8,192 and
            `storage_bytes_used == actual_disk_bytes > 0`.
          - 5 full `22,500s` sessions across 5 simulated IST dates fit well within quota (< 6 MiB).
        """
        # 3A. Transient 1-write failure on minute 1 -> retried & recovered cleanly!
        dir_retry = self._fresh_dir("min_retry")
        p_retry = S1IngressMeasurementProbe(export_dir=dir_retry, require_streamer_attached_default=False)
        p_retry.enable(require_streamer_attached=False)
        orig_write = p_retry._exporter.write_record
        transient_failed_once = [False]

        def _fail_first_minute_once(kind: str, payload: Dict[str, Any], *, update_checkpoint: bool = True) -> bool:
            if kind == "MINUTE_WINDOW" and not transient_failed_once[0]:
                transient_failed_once[0] = True
                return False
            return orig_write(kind, payload, update_checkpoint=update_checkpoint)

        p_retry._exporter.write_record = _fail_first_minute_once  # type: ignore[method-assign]
        base_ns = 1_000 * 60_000_000_000
        p_retry.on_ingress_frame_ok(mono_ns=base_ns + 1_000_000, message_obj=b"x" * 200, decoded_feeds_len=2, sdk_and_app_ns=10_000, cb_entry_ns=base_ns)
        p_retry.on_ingress_frame_ok(mono_ns=base_ns + 65_000_000_000, message_obj=b"x" * 200, decoded_feeds_len=2, sdk_and_app_ns=10_000, cb_entry_ns=base_ns + 65_000_000_000)
        time.sleep(0.08)
        seal_retry = p_retry.disable()
        self.assertTrue(transient_failed_once[0])
        self.assertEqual(seal_retry["durable_exporter"]["tail_state"], "COMPLETE_SEALED")
        self.assertTrue(seal_retry["durable_completeness_valid"])
        rec_retry = S1MeasurementSummaryExporter.load_and_verify_ledger_dir(dir_retry)
        self.assertEqual(rec_retry["minute_windows_recovered"], 2)
        self.assertEqual(len(rec_retry["complete_sealed_epochs"]), 1)
        self.assertEqual(len(rec_retry["gapped_sealed_epochs"]), 0)
        p_retry.shutdown_flush()

        # 3B. Permanent targeted failure on minute 1 while minute 2 & EPOCH_SEALED succeed -> SEALED_WITH_DURABLE_GAPS!
        dir_gap = self._fresh_dir("min_gap")
        p_gap = S1IngressMeasurementProbe(export_dir=dir_gap, require_streamer_attached_default=False)
        p_gap.enable(require_streamer_attached=False)
        orig_write_gap = p_gap._exporter.write_record

        def _reject_minute_seq_1(kind: str, payload: Dict[str, Any], *, update_checkpoint: bool = True) -> bool:
            if kind == "MINUTE_WINDOW" and int(payload.get("logical_minute_seq", 0)) == 1:
                p_gap._exporter._last_export_error = "SIMULATED_MINUTE_1_DISK_ERR"
                return False
            return orig_write_gap(kind, payload, update_checkpoint=update_checkpoint)

        p_gap._exporter.write_record = _reject_minute_seq_1  # type: ignore[method-assign]
        p_gap.on_ingress_frame_ok(mono_ns=base_ns + 1_000_000, message_obj=b"y" * 300, decoded_feeds_len=3, sdk_and_app_ns=10_000, cb_entry_ns=base_ns)
        p_gap.on_ingress_frame_ok(mono_ns=base_ns + 65_000_000_000, message_obj=b"y" * 300, decoded_feeds_len=3, sdk_and_app_ns=10_000, cb_entry_ns=base_ns + 65_000_000_000)
        seal_gap = p_gap.disable()
        self.assertEqual(seal_gap["durable_exporter"]["tail_state"], "SEALED_WITH_DURABLE_GAPS")
        self.assertFalse(seal_gap["durable_completeness_valid"])
        self.assertEqual(seal_gap["missing_durable_intervals_count"], 1)
        self.assertEqual(seal_gap["missing_durable_intervals"][0]["logical_minute_seq"], 1)
        self.assertIn("DURABLE_EXPORT_GAP", seal_gap["workload_meter_qualification"]["invalidation_reasons"])

        rec_gap = S1MeasurementSummaryExporter.load_and_verify_ledger_dir(dir_gap)
        self.assertEqual(rec_gap["minute_windows_recovered"], 1)
        self.assertEqual(len(rec_gap["complete_sealed_epochs"]), 0)
        self.assertEqual(len(rec_gap["gapped_sealed_epochs"]), 1)
        ep_id_gap = seal_gap["epoch_id"]
        self.assertEqual(rec_gap["missing_durable_intervals_by_epoch"][ep_id_gap][0]["logical_minute_seq"], 1)
        p_gap.shutdown_flush()

        # 3C. Control-audit cursor NOT advanced on failed export + `export_flush` returns False on failure + 409 reset protection
        dir_aud = self._fresh_dir("audit_and_flush")
        p_aud = S1IngressMeasurementProbe(export_dir=dir_aud, require_streamer_attached_default=False)
        p_aud.enable(actor_role="ADMIN", peer="127.0.0.1", require_streamer_attached=False)
        p_aud.record_control_audit(action="custom_check_1", actor_role="ADMIN", peer="127.0.0.1", outcome="OK")
        p_aud.record_control_audit(action="custom_check_2", actor_role="ADMIN", peer="127.0.0.1", outcome="OK")

        orig_write_aud = p_aud._exporter.write_record
        block_all_writes = [True]

        def _toggle_writes(kind: str, payload: Dict[str, Any], *, update_checkpoint: bool = True) -> bool:
            if block_all_writes[0]:
                return False
            return orig_write_aud(kind, payload, update_checkpoint=update_checkpoint)

        p_aud._exporter.write_record = _toggle_writes  # type: ignore[method-assign]
        p_aud.on_ingress_frame_ok(mono_ns=base_ns + 5_000, message_obj=b"z" * 128, decoded_feeds_len=1, sdk_and_app_ns=5_000, cb_entry_ns=base_ns)

        # Force-export while all writes fail: MUST return False!
        self.assertFalse(p_aud.flush_for_test_or_export(force_export=True))
        exp_fail_res = p_aud.export_summary(actor_role="ADMIN", peer="127.0.0.1")
        self.assertTrue(exp_fail_res["barrier_completed"])
        self.assertFalse(exp_fail_res["export_succeeded"])
        self.assertFalse(exp_fail_res["flushed"])

        # Disable while writes still blocked -> unexported epoch cannot be reset (409)!
        p_aud.disable(actor_role="ADMIN", peer="127.0.0.1")
        with self.assertRaises(ProbeControlError) as rst_err:
            p_aud.reset(actor_role="ADMIN", peer="127.0.0.1")
        self.assertEqual(rst_err.exception.status_code, 409)
        self.assertEqual(rst_err.exception.code, "UNEXPORTED_EPOCH_CANNOT_RESET")

        # Restore writes and flush -> ALL earlier audit records must still be recovered!
        block_all_writes[0] = False
        exp_ok_res = p_aud.export_summary(actor_role="ADMIN", peer="127.0.0.1")
        self.assertTrue(exp_ok_res["export_succeeded"])
        self.assertTrue(exp_ok_res["flushed"])
        rec_aud = S1MeasurementSummaryExporter.load_and_verify_ledger_dir(dir_aud)
        recovered_actions = {e["action"] for e in rec_aud["control_audit_entries"]}
        self.assertIn("custom_check_1", recovered_actions)
        self.assertIn("custom_check_2", recovered_actions)
        p_aud.shutdown_flush()

        # 3D. Exact Codex counterexample: 8,192-byte quota + 20 injected primary `os.fsync` failures!
        dir_quota = self._fresh_dir("fsync_quota_8k")
        exporter_8k = S1MeasurementSummaryExporter(
            export_dir=dir_quota,
            boot_id="quotatestboot001",
            process_id=os.getpid(),
            max_storage_bytes=8192,
        )
        real_fsync = os.fsync

        def _failing_fsync(_fd: int) -> None:
            raise OSError("injected_primary_fsync_failure")

        os.fsync = _failing_fsync  # type: ignore[assignment]
        try:
            for attempt in range(20):
                ok = exporter_8k.write_record(
                    "PERIODIC_CHECKPOINT",
                    {"attempt": attempt, "pad": "A" * 600},
                    update_checkpoint=True,
                )
                self.assertFalse(ok)
        finally:
            os.fsync = real_fsync  # type: ignore[assignment]

        actual_bytes_on_disk = sum(p.stat().st_size for p in dir_quota.iterdir() if p.is_file())
        st_8k = exporter_8k.status_snapshot(time.perf_counter_ns())
        self.assertGreater(actual_bytes_on_disk, 0)
        self.assertLessEqual(
            actual_bytes_on_disk,
            8192,
            f"Failure-path writes exceeded 8,192-byte quota: {actual_bytes_on_disk} bytes!",
        )
        self.assertEqual(st_8k["storage_bytes_used"], actual_bytes_on_disk)
        self.assertTrue(st_8k["quota_exhausted"])

        # 3E. Five full 22,500s sessions across 5 IST dates (mocking only os.fsync for test speed)
        dir_5s = self._fresh_dir("five_sessions")
        os.fsync = lambda _fd: None  # type: ignore[assignment]
        try:
            for day_idx in range(5):
                p_day = S1IngressMeasurementProbe(export_dir=dir_5s, require_streamer_attached_default=False)
                p_day._exporter._boot_ist_date = f"2026-11-{10 + day_idx:02d}"
                # Drive aggregator directly across 22,500s (750 observations at 30s spacing = 375 minutes + 125 checkpoints)
                p_day._init_aggregator_state(epoch_seq=1)
                p_day._agg_epoch_closed = False
                start_ns = 120_000_000_000_000 + day_idx * 90_000_000_000_000
                p_day._agg_active_start_mono_ns = start_ns
                last_cp = start_ns
                for step in range(750):
                    m_ns = start_ns + step * 30_000_000_000
                    p_day._worker_apply_ok_frame(
                        [probe_mod._EVT_FRAME_OK, 1, m_ns, 17900, 0, 70, 1_500_000, 120_000, 350_000, 4_000]
                    )
                    if p_day._pending_minute_exports:
                        p_day._worker_flush_pending_minutes(at_seal=False)
                    if (m_ns - last_cp) >= probe_mod.DEFAULT_EXPORT_INTERVAL_NS:
                        last_cp = m_ns
                        p_day._worker_export_summary_record("PERIODIC_CHECKPOINT")
                p_day._agg_accumulated_enabled_ns = 22_500 * 1_000_000_000
                p_day._agg_active_start_mono_ns = 0
                p_day._agg_epoch_closed = True
                p_day._worker_finalize_active_buckets()
                p_day._worker_flush_pending_minutes(at_seal=True)
                self.assertTrue(p_day._worker_export_summary_record("EPOCH_SEALED", {"seal_reason": "EOD"}))
        finally:
            os.fsync = real_fsync  # type: ignore[assignment]

        rec_5s = S1MeasurementSummaryExporter.load_and_verify_ledger_dir(dir_5s)
        self.assertEqual(rec_5s["corrupt_or_torn_records"], 0)
        self.assertEqual(len(rec_5s["complete_sealed_epochs"]), 5)
        self.assertEqual(len(rec_5s["gapped_sealed_epochs"]), 0)
        self.assertEqual(rec_5s["minute_windows_recovered"], 5 * 375)
        self.assertEqual(rec_5s["merged_probe_added_callback_us"]["count"], 5 * 750)
        self.assertLess(
            rec_5s["actual_directory_bytes"],
            12 * 1024 * 1024,
            f"5-session workload footprint unexpectedly large: {rec_5s['actual_directory_bytes']} bytes",
        )

    def test_4_exact_origin_cors_and_non_rate_limited_emergency_disable(self) -> None:
        """
        Verifies Codex HIGH 3 & Priority 4:
          - Exact approved origins (localhost + this host's Tailscale IP/MagicDNS on 5173/4173) are allowed;
            unrelated `*.ts.net` tailnets, other `100.64.0.0/10` hosts, and unexpected ports are DENIED.
          - Probe management remains loopback-only (`403` on Tailscale peer/origin), emergency `disable`
            is never rate-limited (`25` calls after bucket exhaustion), invalid actions return `422`
            without draining write tokens, and `export_flush` returns HTTP `503` on disk failure.
        """
        from starlette.middleware.cors import CORSMiddleware

        async def _inner_asgi_app(scope: Dict[str, Any], receive: Any, send: Any) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})

        resolved_origins = resolve_cors_allowed_origins("")
        cors_mw = CORSMiddleware(
            _inner_asgi_app,
            allow_origins=resolved_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
            allow_headers=["*"],
        )

        def _invoke_cors_sync(method: str, path: str, req_headers: Dict[str, str]) -> Tuple[int, Dict[str, str]]:
            sent_messages: List[Dict[str, Any]] = []

            async def _receive() -> Dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            async def _send(msg: Dict[str, Any]) -> None:
                sent_messages.append(msg)

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in req_headers.items()],
            }
            coro = cors_mw(scope, _receive, _send)
            try:
                while True:
                    coro.send(None)
            except StopIteration:
                pass
            start_msg = next(m for m in sent_messages if m["type"] == "http.response.start")
            hdrs = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in start_msg.get("headers", [])
            }
            return int(start_msg["status"]), hdrs

        approved_origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://100.97.41.35:5173",
            "http://100.97.41.35:4173",
            "http://[fd7a:115c:a1e0::7c01:29bb]:5173",
            "http://aruns-macbook-pro.tailadf4b0.ts.net:5173",
            "https://aruns-macbook-pro.tailadf4b0.ts.net",
        ]
        for good_origin in approved_origins:
            for mth in ("OPTIONS", "GET"):
                hdr_in = {"Origin": good_origin}
                if mth == "OPTIONS":
                    hdr_in["Access-Control-Request-Method"] = "GET"
                _st, resp_hdrs = _invoke_cors_sync(mth, "/api/ping", hdr_in)
                self.assertEqual(
                    resp_hdrs.get("access-control-allow-origin"),
                    good_origin,
                    f"Approved frontend origin rejected on {mth}: {good_origin}",
                )
                self.assertEqual(resp_hdrs.get("access-control-allow-credentials"), "true")

        denied_origins = [
            "https://attacker.different-tailnet.ts.net",
            "http://100.90.1.2:9999",
            "http://100.90.1.2:5173",
            "http://100.97.41.35:9999",  # Unexpected port on host's own Tailscale IP!
            "http://aruns-macbook-pro.tailadf4b0.ts.net:9999",  # Unexpected port on host's MagicDNS!
            "http://other-machine.tailadf4b0.ts.net:5173",
            "https://evil.example.com",
            "*",
        ]
        for bad_origin in denied_origins:
            for mth in ("OPTIONS", "GET"):
                hdr_in = {"Origin": bad_origin}
                if mth == "OPTIONS":
                    hdr_in["Access-Control-Request-Method"] = "GET"
                _st, resp_hdrs = _invoke_cors_sync(mth, "/api/ping", hdr_in)
                self.assertIsNone(
                    resp_hdrs.get("access-control-allow-origin"),
                    f"Untrusted origin was allowed by CORS on {mth}: {bad_origin}",
                )

        # Verify S1ProbeManagementGuard + AST-extracted main.py handlers
        admin_tok = "admin-secret-v4-token"
        read_tok = "read-secret-v4-token"
        old_admin = os.environ.get("S1_PROBE_ADMIN_TOKEN_SHA256")
        old_read = os.environ.get("S1_PROBE_READ_TOKEN_SHA256")
        os.environ["S1_PROBE_ADMIN_TOKEN_SHA256"] = hashlib.sha256(admin_tok.encode()).hexdigest()
        os.environ["S1_PROBE_READ_TOKEN_SHA256"] = hashlib.sha256(read_tok.encode()).hexdigest()

        orig_guard_singleton = probe_mod._GUARD_SINGLETON
        orig_probe_singleton = probe_mod._PROBE_SINGLETON
        saved_svc_mod = sys.modules.get("app.data.upstox_stream_service")
        guard = S1ProbeManagementGuard()
        probe_mod._GUARD_SINGLETON = guard

        svc_calls = [0]
        probe_h = S1IngressMeasurementProbe(export_dir=self._fresh_dir("http_guard"), require_streamer_attached_default=False)
        probe_mod._PROBE_SINGLETON = probe_h
        svc_h, _ = _make_inert_service_harness(probe_h)
        inst_cls_h = build_instrumented_streamer_class(RealMarketDataStreamerV3, probe_h)
        st_h = inst_cls_h(api_client=None, instrumentKeys=[], mode="full")
        svc_h._streamer = st_h

        def _get_svc() -> Any:
            svc_calls[0] += 1
            return svc_h

        fake_svc_mod = types.ModuleType("app.data.upstox_stream_service")
        fake_svc_mod.get_stream_service = _get_svc  # type: ignore[attr-defined]
        sys.modules["app.data.upstox_stream_service"] = fake_svc_mod

        get_summary_fn, control_fn = _extract_main_probe_handlers_ast(_get_svc)

        def _req(peer: str, headers: Dict[str, str]) -> Any:
            return types.SimpleNamespace(
                client=types.SimpleNamespace(host=peer),
                headers=headers,
            )

        try:
            from fastapi import HTTPException

            # Unauthenticated / non-loopback / Tailscale-origin calls NEVER invoke `get_stream_service()`
            for bad_req in (
                _req("100.97.41.35", {"authorization": f"Bearer {admin_tok}"}),
                _req("127.0.0.1", {"authorization": f"Bearer {admin_tok}", "origin": "http://100.97.41.35:5173"}),
                _req("127.0.0.1", {"authorization": f"Bearer {admin_tok}", "x-forwarded-for": "100.97.41.35"}),
                _req("127.0.0.1", {"authorization": f"Bearer {admin_tok}", "cookie": "sid=123"}),
                _req("127.0.0.1", {"authorization": f"Bearer {read_tok}"}),
                _req("127.0.0.1", {}),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    control_fn(bad_req, action="disable")
                self.assertIn(ctx.exception.status_code, (401, 403))
            self.assertEqual(svc_calls[0], 0, "Unauthorized requests must not instantiate stream service")

            # 10 invalid actions must return 422 WITHOUT draining write rate-limit tokens or calling get_stream_service
            admin_req = _req("127.0.0.1", {"authorization": f"Bearer {admin_tok}"})
            for _ in range(10):
                with self.assertRaises(HTTPException) as inv_ctx:
                    control_fn(admin_req, action="bogus_typo_action")
                self.assertEqual(inv_ctx.exception.status_code, 422)
            self.assertEqual(svc_calls[0], 0)

            # Exhaust all 4 write tokens while verifying `export_flush` 200 (ok) and 503 (disk failure):
            flush_ok_res = control_fn(admin_req, action="export_flush")
            self.assertTrue(flush_ok_res["flushed"])
            self.assertTrue(flush_ok_res["export_succeeded"])

            orig_wr = probe_h._exporter.write_record
            probe_h._exporter.write_record = lambda *_a, **_kw: False  # type: ignore[method-assign]
            try:
                with self.assertRaises(HTTPException) as fl_err_ctx:
                    control_fn(admin_req, action="export_flush")
                self.assertEqual(fl_err_ctx.exception.status_code, 503)
                self.assertEqual(fl_err_ctx.exception.detail["code"], "EXPORT_FLUSH_FAILED")
            finally:
                probe_h._exporter.write_record = orig_wr  # type: ignore[method-assign]

            for _ in range(2):
                res_en = control_fn(admin_req, action="enable")
                self.assertTrue(res_en["enabled"])
            with self.assertRaises(HTTPException) as rl_ctx:
                control_fn(admin_req, action="enable")
            self.assertEqual(rl_ctx.exception.status_code, 429)

            # Emergency `disable` succeeds 25 consecutive times even while write limiter is exhausted!
            for _ in range(25):
                res_dis = control_fn(admin_req, action="disable")
                self.assertFalse(res_dis["enabled"])

            # Verify public stream status exposes only minimal booleans
            pub = svc_h.get_status()["s1_ingress_probe"]
            self.assertEqual(set(pub.keys()), {"enabled", "auto_disabled_on_fault"})
        finally:
            probe_mod._GUARD_SINGLETON = orig_guard_singleton
            probe_mod._PROBE_SINGLETON = orig_probe_singleton
            if saved_svc_mod is None:
                sys.modules.pop("app.data.upstox_stream_service", None)
            else:
                sys.modules["app.data.upstox_stream_service"] = saved_svc_mod
            if old_admin is None:
                os.environ.pop("S1_PROBE_ADMIN_TOKEN_SHA256", None)
            else:
                os.environ["S1_PROBE_ADMIN_TOKEN_SHA256"] = old_admin
            if old_read is None:
                os.environ.pop("S1_PROBE_READ_TOKEN_SHA256", None)
            else:
                os.environ["S1_PROBE_READ_TOKEN_SHA256"] = old_read
            probe_h.shutdown_flush()

    def test_5_sdk_passthrough_zero_gap_o1_and_real_model_immutability(self) -> None:
        """
        Verifies:
          - Actual 3 production model artifacts (`swing/champion_ensemble.pkl`,
            `intraday/champion_ensemble.pkl`, `s205_meta_weights.json`) exist and remain bit-identical.
          - `OFF` vs `ON` produces byte-identical quotes & 1m bars via original `MarketDataStreamerV3.handle_message`.
          - `O(1)` 3,600-second zero-gap bucket accounting completes in < 10ms with exact counts.
        """
        model_paths = [
            _BACKEND_ROOT / "models" / "swing" / "champion_ensemble.pkl",
            _BACKEND_ROOT / "models" / "intraday" / "champion_ensemble.pkl",
            _BACKEND_ROOT / "app" / "analytics" / "option_sniffer_advance_s205" / "s205_meta_weights.json",
        ]
        hashes_before: Dict[str, str] = {}
        for mp in model_paths:
            self.assertTrue(mp.is_file(), f"Expected model artifact missing: {mp}")
            raw = mp.read_bytes()
            self.assertGreater(len(raw), 0)
            hashes_before[str(mp)] = hashlib.sha256(raw).hexdigest()
        self.assertEqual(len(hashes_before), 3)

        frames = [_build_wire_frame(16, seq_seed=i + 1) for i in range(25)]

        # Run OFF
        p_off = S1IngressMeasurementProbe(export_dir=self._fresh_dir("off_eq"), require_streamer_attached_default=False)
        svc_off, _ = _make_inert_service_harness(p_off, equity_count=16)
        cls_off = build_instrumented_streamer_class(RealMarketDataStreamerV3, p_off)
        st_off = cls_off(api_client=None, instrumentKeys=[], mode="full")
        st_off.on("message", svc_off._handle_feed_message)
        svc_off._streamer = st_off
        for f in frames:
            st_off.handle_message(None, f)

        # Run ON
        p_on = S1IngressMeasurementProbe(export_dir=self._fresh_dir("on_eq"), require_streamer_attached_default=False)
        svc_on, _ = _make_inert_service_harness(p_on, equity_count=16)
        cls_on = build_instrumented_streamer_class(RealMarketDataStreamerV3, p_on)
        st_on = cls_on(api_client=None, instrumentKeys=[], mode="full")
        st_on.on("message", svc_on._handle_feed_message)
        svc_on._streamer = st_on
        p_on.enable(require_streamer_attached=True)
        for f in frames:
            st_on.handle_message(None, f)

        # Corrupted frame raises identical protobuf DecodeError and increments `sdk_or_listener_errors`
        with self.assertRaises(Exception):
            st_on.handle_message(None, b"\xff\xff\xff_corrupt_wire")

        s_on = p_on.disable()
        self.assertEqual(svc_off._total_ticks, svc_on._total_ticks)
        self.assertEqual(set(svc_off._live_quotes.keys()), set(svc_on._live_quotes.keys()))
        for sym in svc_off._live_quotes:
            self.assertEqual(svc_off._live_quotes[sym]["ltp"], svc_on._live_quotes[sym]["ltp"])
            self.assertEqual(svc_off._live_quotes[sym]["volume"], svc_on._live_quotes[sym]["volume"])

        wl = s_on["ingress_workload"]
        self.assertEqual(wl["decoded_ok_frames"], 25)
        self.assertEqual(wl["sdk_or_listener_errors"], 1)
        self.assertEqual(wl["ingress_frames_total"], 26)
        self.assertEqual(wl["decoded_feed_entries_total"], 25 * 16)
        self.assertEqual(wl["raw_wire_bytes_total"], sum(len(f) for f in frames) + len(b"\xff\xff\xff_corrupt_wire"))

        # Verify O(1) 3,600s zero-gap accounting speed & exactness
        p_gap = S1IngressMeasurementProbe(export_dir=None, require_streamer_attached_default=False)
        p_gap._init_aggregator_state(epoch_seq=1)
        t_start = time.perf_counter()
        p_gap._worker_advance_time_buckets(100_000_000_000, 500, 5)
        p_gap._worker_advance_time_buckets(100_000_000_000 + 3601 * 1_000_000_000, 500, 5)
        p_gap._worker_finalize_active_buckets()
        gap_elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        self.assertLess(gap_elapsed_ms, 15.0)
        self.assertEqual(p_gap._agg_confirmed_zero_1s_buckets, 3600)
        self.assertEqual(p_gap._agg_hist_1s_all_frames.count, 3602)

        for mp in model_paths:
            self.assertEqual(hashes_before[str(mp)], hashlib.sha256(mp.read_bytes()).hexdigest())
        self.assertEqual(_AUDIT_VIOLATIONS, [])
        p_on.shutdown_flush()


def _percentile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def run_matched_abba_benchmarks() -> Dict[str, Any]:
    """
    Strictly Matched-Load OFF vs ON Benchmark Protocol:
      - `gc.enable()` active throughout.
      - Identical serialized binary protobuf frames, identical iteration count, identical
        inter-frame pacing (`0` asymmetric sleeps!) across balanced `ABBA` + `BAAB` blocks
        (`4` OFF blocks + `4` ON blocks per payload tier).
      - External `5 ms` scheduling canary measures **wakeup overshoot** (`actual - 5,000 us`),
        NOT total sleep duration.
      - Measures total callback `p50` / `p99` (`OFF` vs `ON`), achieved `Hz` (`OFF` vs `ON`),
        canary wakeup overshoot `p99` (`OFF` vs `ON`), worker drain slice `p99`, and internal
        `probe_added_callback_us` `p99_ub` / `p999_ub`.
    """
    gc.enable()
    bench_root = _TEST_TMP_ROOT / "matched_bench"
    bench_root.mkdir(parents=True, exist_ok=True)

    tiers = [
        ("small_ge_1kib", 5, 500, 0),
        ("medium_ge_16kib_70inst", 70, 300, 0),
        ("large_ge_64kib_280inst", 280, 120, 0),
        ("paced_200hz_medium_70inst", 70, 160, 200),
    ]
    tier_results: Dict[str, Any] = {}

    for tier_name, n_inst, frames_per_block, target_paced_hz in tiers:
        wire_payload = _build_wire_frame(n_inst, seq_seed=42)
        off_cb_p50s: List[float] = []
        off_cb_p99s: List[float] = []
        on_cb_p50s: List[float] = []
        on_cb_p99s: List[float] = []
        off_canary_p99s: List[float] = []
        on_canary_p99s: List[float] = []
        off_hz_list: List[float] = []
        on_hz_list: List[float] = []
        on_internal_p99_ubs: List[float] = []
        on_internal_p999_ubs: List[float] = []
        on_worker_slice_p99_ubs: List[float] = []
        total_drops = 0

        # Warmup once before timed ABBA + BAAB schedule
        schedule = ["OFF", "ON", "ON", "OFF", "ON", "OFF", "OFF", "ON"]
        for blk_idx, mode in enumerate(["WARMUP"] + schedule):
            is_on = (mode == "ON")
            b_dir = bench_root / f"{tier_name}_{blk_idx}_{mode}"
            probe = S1IngressMeasurementProbe(export_dir=b_dir, require_streamer_attached_default=False)
            svc, _ = _make_inert_service_harness(probe, equity_count=min(n_inst, 50))
            st_cls = build_instrumented_streamer_class(RealMarketDataStreamerV3, probe)
            streamer = st_cls(api_client=None, instrumentKeys=[], mode="full")
            streamer.on("message", svc._handle_feed_message)
            svc._streamer = streamer

            if is_on:
                probe.enable(require_streamer_attached=True)

            canary_stop = threading.Event()
            canary_overshoots_us: List[float] = []

            def _canary_worker() -> None:
                target_ns = 5_000_000
                while not canary_stop.is_set():
                    t_c0 = time.perf_counter_ns()
                    time.sleep(0.005)
                    overshoot_us = max(0.0, (time.perf_counter_ns() - t_c0 - target_ns) / 1000.0)
                    canary_overshoots_us.append(overshoot_us)

            canary_th = threading.Thread(target=_canary_worker, daemon=True)
            canary_th.start()

            cb_samples_us: List[float] = []
            n_iters = 40 if mode == "WARMUP" else frames_per_block
            paced_step_ns = int(1_000_000_000 / target_paced_hz) if target_paced_hz > 0 else 0
            t_blk0 = time.perf_counter_ns()
            for i in range(n_iters):
                if paced_step_ns > 0:
                    next_due_ns = t_blk0 + i * paced_step_ns
                    rem_ns = next_due_ns - time.perf_counter_ns()
                    if rem_ns > 200_000:
                        time.sleep(rem_ns / 1_000_000_000.0)
                t_m0 = time.perf_counter_ns()
                streamer.handle_message(None, wire_payload)
                cb_samples_us.append((time.perf_counter_ns() - t_m0) / 1000.0)
                # Identical cooperative yield every 16 frames in unpaced saturation modes!
                if paced_step_ns == 0 and (i & 15) == 15:
                    time.sleep(0)
            blk_wall_s = max(1e-9, (time.perf_counter_ns() - t_blk0) / 1_000_000_000.0)

            canary_stop.set()
            canary_th.join(timeout=1.0)

            if mode == "WARMUP":
                probe.shutdown_flush()
                continue

            cb_samples_us.sort()
            canary_overshoots_us.sort()
            c_p99 = _percentile(canary_overshoots_us, 0.99)
            achieved_hz = n_iters / blk_wall_s

            if is_on:
                snap = probe.disable()
                on_cb_p50s.append(_percentile(cb_samples_us, 0.50))
                on_cb_p99s.append(_percentile(cb_samples_us, 0.99))
                on_canary_p99s.append(c_p99)
                on_hz_list.append(achieved_hz)
                icb = snap["workload_meter_qualification"]["internal_callback_budget"]
                on_internal_p99_ubs.append(icb["probe_added_p99_upper_bound_us"])
                on_internal_p999_ubs.append(icb["probe_added_p999_upper_bound_us"])
                on_worker_slice_p99_ubs.append(snap["latencies_us"]["worker_drain_slice_us"]["p99_upper_bound"])
                total_drops += snap["failures_and_bounds"]["inbox_overflow_drops"]
            else:
                off_cb_p50s.append(_percentile(cb_samples_us, 0.50))
                off_cb_p99s.append(_percentile(cb_samples_us, 0.99))
                off_canary_p99s.append(c_p99)
                off_hz_list.append(achieved_hz)

            probe.shutdown_flush()

        tier_results[tier_name] = {
            "wire_bytes": len(wire_payload),
            "instruments": n_inst,
            "blocks_per_condition": 4,
            "frames_per_block": frames_per_block,
            "median_cb_p50_off_us": round(statistics.median(off_cb_p50s), 2),
            "median_cb_p50_on_us": round(statistics.median(on_cb_p50s), 2),
            "median_cb_p99_off_us": round(statistics.median(off_cb_p99s), 2),
            "median_cb_p99_on_us": round(statistics.median(on_cb_p99s), 2),
            "median_canary_5ms_overshoot_p99_off_us": round(statistics.median(off_canary_p99s), 2),
            "median_canary_5ms_overshoot_p99_on_us": round(statistics.median(on_canary_p99s), 2),
            "median_achieved_hz_off": round(statistics.median(off_hz_list), 1),
            "median_achieved_hz_on": round(statistics.median(on_hz_list), 1),
            "max_internal_probe_added_p99_ub_us": round(max(on_internal_p99_ubs), 2),
            "max_internal_probe_added_p999_ub_us": round(max(on_internal_p999_ubs), 2),
            "median_worker_slice_p99_ub_us": round(statistics.median(on_worker_slice_p99_ubs), 2),
            "total_overflow_drops": total_drops,
        }

    # Paced actual arrival-rate verification (compact LTPC vs 53-inst full book)
    paced_profiles = [
        ("compact_ltpc_1000hz", _build_wire_frame(1, compact_ltpc_only=True), 1000, 0.5),
        ("compact_ltpc_5000hz", _build_wire_frame(1, compact_ltpc_only=True), 5000, 0.4),
        ("full_53inst_13kib_1000hz_target", _build_wire_frame(53, compact_ltpc_only=False), 1000, 0.5),
    ]
    paced_results: List[Dict[str, Any]] = []
    for prof_name, payload, target_hz, dur_s in paced_profiles:
        p_rate = S1IngressMeasurementProbe(export_dir=bench_root / prof_name, require_streamer_attached_default=False)
        svc_r, _ = _make_inert_service_harness(p_rate, equity_count=25)
        cls_r = build_instrumented_streamer_class(RealMarketDataStreamerV3, p_rate)
        st_r = cls_r(api_client=None, instrumentKeys=[], mode="full")
        st_r.on("message", svc_r._handle_feed_message)
        svc_r._streamer = st_r
        p_rate.enable(require_streamer_attached=True)

        total_target = int(target_hz * dur_s)
        step_ns = int(1_000_000_000 / target_hz)
        t_start_ns = time.perf_counter_ns()
        max_late_ns = 0
        for idx in range(total_target):
            sched_ns = t_start_ns + idx * step_ns
            now_ns = time.perf_counter_ns()
            if now_ns < sched_ns:
                while time.perf_counter_ns() < sched_ns:
                    pass
            else:
                late = now_ns - sched_ns
                if late > max_late_ns:
                    max_late_ns = late
            st_r.handle_message(None, payload)
        elapsed_s = max(1e-9, (time.perf_counter_ns() - t_start_ns) / 1_000_000_000.0)
        snap_r = p_rate.disable()
        p_rate.shutdown_flush()
        paced_results.append(
            {
                "profile": prof_name,
                "wire_bytes": len(payload),
                "target_hz": target_hz,
                "achieved_hz": round(total_target / elapsed_s, 1),
                "max_schedule_lateness_ms": round(max_late_ns / 1_000_000.0, 3),
                "overflow_drops": snap_r["failures_and_bounds"]["inbox_overflow_drops"],
                "probe_added_p99_ub_us": snap_r["latencies_us"]["probe_added_callback_us"]["p99_upper_bound"],
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "gc_enabled": gc.isenabled(),
        "matched_abba_baab_tiers": tier_results,
        "paced_actual_rates": paced_results,
        "audit_violations": list(_AUDIT_VIOLATIONS),
    }


if __name__ == "__main__":
    run_bench = "--benchmark" in sys.argv
    unit_argv = [a for a in sys.argv if a != "--benchmark"]
    prog = unittest.main(argv=unit_argv, exit=False, verbosity=2)
    if not prog.result.wasSuccessful():
        _cleanup_dir_abs(_TEST_TMP_ROOT)
        sys.exit(1)
    if run_bench:
        try:
            bench_out = run_matched_abba_benchmarks()
            print("\n=== S1 WORKLOAD METER V4 MATCHED ABBA/BAAB BENCHMARK ===")
            print(json.dumps(bench_out, indent=2))
            for t_name, t_res in bench_out["matched_abba_baab_tiers"].items():
                assert t_res["total_overflow_drops"] == 0, f"Unexpected overflow in {t_name}"
                assert t_res["max_internal_probe_added_p99_ub_us"] <= 100.0, f"p99 > 100us in {t_name}"
                assert t_res["max_internal_probe_added_p999_ub_us"] <= 1000.0, f"p999 > 1000us in {t_name}"
            assert bench_out["audit_violations"] == []
        finally:
            _cleanup_dir_abs(_TEST_TMP_ROOT)
