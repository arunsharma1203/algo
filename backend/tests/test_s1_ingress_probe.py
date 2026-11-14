"""
Isolated Adversarial Unit & Synthetic Benchmark Suite for S1 Ingress Probe V2.

Isolation Guarantees:
  - Installs a process-level `sys.addaudithook` enforcing ZERO network sockets,
    ZERO `backend/market_data.db` opens, and ZERO filesystem writes outside the
    ephemeral test temporary directory.
  - Compiles `UpstoxStreamService` and `main.py` S1 probe route handlers via AST
    without executing module-level singletons (`init_db_wal`, `_GLOBAL_STREAM_SERVICE`).
"""

import ast
import collections
import datetime
import gc
import hashlib
import json
import os
import random
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Isolated temp root allowed for durable exporter tests
_TEST_TMP_ROOT = Path(tempfile.mkdtemp(prefix="s1_probe_v2_isolated_")).resolve()
_AUDIT_VIOLATIONS: List[str] = []


def _strict_offline_audit_hook(event: str, args: Tuple[Any, ...]) -> None:
    if event in ("socket.connect", "socket.bind", "urllib.Request"):
        _AUDIT_VIOLATIONS.append(f"FORBIDDEN_NETWORK:{event}:{args}")
        raise PermissionError(f"Network call forbidden in isolated S1 probe test: {event}")
    if event == "sqlite3.connect" and args:
        target = str(args[0])
        if "market_data.db" in target:
            _AUDIT_VIOLATIONS.append(f"FORBIDDEN_PROD_DB:{target}")
            raise PermissionError(f"Production SQLite access forbidden: {target}")
    if event == "open" and len(args) >= 2:
        p_raw, mode = args[0], str(args[1] or "r")
        if isinstance(p_raw, (str, os.PathLike)):
            p_str = str(p_raw)
            if "market_data.db" in p_str:
                _AUDIT_VIOLATIONS.append(f"FORBIDDEN_PROD_DB_OPEN:{p_str}")
                raise PermissionError(f"Opening market_data.db is forbidden: {p_str}")
            if any(m in mode for m in ("w", "a", "x", "+")):
                try:
                    resolved = Path(p_str).resolve()
                    if not str(resolved).startswith(str(_TEST_TMP_ROOT)):
                        _AUDIT_VIOLATIONS.append(f"FORBIDDEN_WRITE:{resolved}:{mode}")
                        raise PermissionError(f"Write outside isolated temp dir forbidden: {resolved}")
                except PermissionError:
                    raise
                except Exception:
                    pass


from fastapi import HTTPException
from google.protobuf import json_format
import upstox_client
from upstox_client.feeder.proto import MarketDataFeedV3_pb2 as pb

sys.addaudithook(_strict_offline_audit_hook)

from app.data.s1_ingress_probe import (
    BoundedHistogram,
    DEFAULT_ALLOWED_LOCAL_ORIGINS,
    IST_TZ,
    LATENCY_BOUNDS_US,
    MAX_EXPORT_STORAGE_BYTES,
    MAX_INBOX_SAMPLES,
    MAX_INBOX_WIRE_BYTES,
    MAX_KEY_CHARS,
    MAX_TRACKED_OPTION_COHORT_KEYS,
    PROBE_WRAPPER_VERSION,
    ProbeControlAction,
    ProbeControlError,
    S1IngressMeasurementProbe,
    S1MeasurementSummaryExporter,
    S1ProbeManagementGuard,
    S1_CORE_INSTRUMENT_KEYS,
    attach_probe_lifecycle_listeners,
    build_instrumented_streamer_class,
)


def _load_isolated_stream_service_class() -> type:
    """Loads `UpstoxStreamService` class via AST without instantiating global singletons or DBs."""
    src_path = BACKEND_DIR / "app" / "data" / "upstox_stream_service.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))
    cls_nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "UpstoxStreamService"]
    assert len(cls_nodes) == 1, "UpstoxStreamService class not found"
    mod_ast = ast.Module(body=cls_nodes, type_ignores=[])
    ast.fix_missing_locations(mod_ast)

    import logging

    class _FakeLimiter:
        def get_stats(self) -> Dict[str, Any]:
            return {"requests_in_window": 0, "isolated_stub": True}

    ns: Dict[str, Any] = {
        "__name__": "isolated_upstox_stream_service",
        "collections": collections,
        "datetime": datetime.datetime,
        "timedelta": datetime.timedelta,
        "dtime": datetime.time,
        "time": time,
        "threading": threading,
        "logging": logging,
        "logger": logging.getLogger("isolated_stream_test"),
        "Dict": Dict,
        "Any": Any,
        "List": List,
        "Optional": Optional,
        "Tuple": Tuple,
        "Set": Set,
        "MARKET_OPEN_TIME": datetime.time(9, 15),
        "MARKET_CLOSE_TIME": datetime.time(15, 30),
        "MAX_QUOTE_AGE_MS": 15000,
        "PROBE_WRAPPER_VERSION": PROBE_WRAPPER_VERSION,
        "S1IngressMeasurementProbe": S1IngressMeasurementProbe,
        "attach_probe_lifecycle_listeners": attach_probe_lifecycle_listeners,
        "build_instrumented_streamer_class": build_instrumented_streamer_class,
        "get_s1_ingress_probe": lambda: (_ for _ in ()).throw(AssertionError("Must inject isolated probe")),
        "get_upstox_limiter": lambda: _FakeLimiter(),
        "normalize_ticker": lambda s: s if s.endswith(".NS") else f"{s}.NS",
    }
    exec(compile(mod_ast, str(src_path), "exec"), ns)
    return ns["UpstoxStreamService"]


def _load_isolated_main_endpoints(probe: S1IngressMeasurementProbe, guard: S1ProbeManagementGuard):
    """Loads the real FastAPI route functions & CORS config from `backend/app/main.py` via AST."""
    main_path = BACKEND_DIR / "app" / "main.py"
    tree = ast.parse(main_path.read_text(encoding="utf-8"), filename=str(main_path))

    fn_nodes: List[ast.stmt] = []
    cors_call_found: Optional[ast.Call] = None
    cors_env_nodes: List[ast.stmt] = []

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in (
            "get_s1_ingress_probe_summary",
            "control_s1_ingress_probe",
        ):
            stripped = ast.FunctionDef(
                name=node.name,
                args=node.args,
                body=node.body,
                decorator_list=[],
                returns=node.returns,
                type_comment=node.type_comment,
            )
            ast.copy_location(stripped, node)
            fn_nodes.append(stripped)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in ("_EXTRA_CORS_ORIGINS", "_RESOLVED_CORS_ORIGINS"):
                    cors_env_nodes.append(node)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            if (
                isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "add_middleware"
                and node.value.args
                and isinstance(node.value.args[0], ast.Name)
                and node.value.args[0].id == "CORSMiddleware"
            ):
                cors_call_found = node.value

    assert len(fn_nodes) == 2, "Expected both S1 probe endpoint functions in main.py"
    assert cors_call_found is not None, "Expected CORSMiddleware configuration in main.py"

    # Inject temporary fake modules only during endpoint call via custom wrapper
    from fastapi import Query, Request

    mod_ast = ast.Module(body=cors_env_nodes + fn_nodes, type_ignores=[])
    ast.fix_missing_locations(mod_ast)
    ns: Dict[str, Any] = {
        "os": os,
        "HTTPException": HTTPException,
        "Query": lambda default=None, **_kw: default,
        "Request": Request,
        "_S1_LOCAL_ORIGINS": DEFAULT_ALLOWED_LOCAL_ORIGINS,
    }
    exec(compile(mod_ast, str(main_path), "exec"), ns)

    class _StubSvc:
        def _get_s1_probe(self) -> S1IngressMeasurementProbe:
            return probe

    def _invoke(fn_name: str, **kwargs: Any) -> Any:
        orig_probe_mod = sys.modules.get("app.data.s1_ingress_probe")
        orig_uss_mod = sys.modules.get("app.data.upstox_stream_service")
        stub_uss = types.ModuleType("app.data.upstox_stream_service")
        stub_uss.get_stream_service = lambda: _StubSvc()  # type: ignore[attr-defined]

        stub_prb = types.ModuleType("app.data.s1_ingress_probe")
        stub_prb.ProbeControlAction = ProbeControlAction  # type: ignore[attr-defined]
        stub_prb.ProbeControlError = ProbeControlError  # type: ignore[attr-defined]
        stub_prb.get_s1_probe_management_guard = lambda: guard  # type: ignore[attr-defined]
        sys.modules["app.data.upstox_stream_service"] = stub_uss
        sys.modules["app.data.s1_ingress_probe"] = stub_prb
        try:
            return ns[fn_name](**kwargs)
        finally:
            if orig_uss_mod is not None:
                sys.modules["app.data.upstox_stream_service"] = orig_uss_mod
            else:
                sys.modules.pop("app.data.upstox_stream_service", None)
            if orig_probe_mod is not None:
                sys.modules["app.data.s1_ingress_probe"] = orig_probe_mod
            else:
                sys.modules.pop("app.data.s1_ingress_probe", None)

    cors_kw = {kw.arg: kw.value for kw in cors_call_found.keywords}
    return _invoke, ns["_RESOLVED_CORS_ORIGINS"], cors_kw


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, host: str = "127.0.0.1", headers: Optional[Dict[str, str]] = None) -> None:
        self.client = _FakeClient(host) if host else None
        self.headers = headers or {}


def _make_isolated_stream_harness(probe: S1IngressMeasurementProbe) -> Any:
    StreamCls = _load_isolated_stream_service_class()
    svc = object.__new__(StreamCls)
    svc._lock = threading.RLock()
    svc._is_running = False
    svc._is_connected = True
    svc._streamer = None
    svc._connected_at = "2026-11-13T09:15:00"
    svc._last_msg_ts = 0.0
    svc._total_messages = 0
    svc._total_ticks = 0
    svc._reconnect_count = 0
    svc._last_error = None
    svc._key_to_symbol = {
        "NSE_EQ|INE002A01018": "RELIANCE.NS",
        "NSE_EQ|INE040A01034": "HDFCBANK.NS",
        "NSE_EQ|INE090A01021": "ICICIBANK.NS",
    }
    svc._symbol_to_key = {v: k for k, v in svc._key_to_symbol.items()}
    svc._live_quotes = {}
    svc._minute_bars = collections.defaultdict(dict)
    svc._completed_15m_bars = collections.defaultdict(dict)
    svc._flushed_slots = set()
    svc._latest_frozen_snapshot = None
    svc._feed_listeners = []
    svc._additional_keys = {"NSE_INDEX|Nifty 50", "NSE_INDEX|India VIX"}
    svc._subscribed_ws_keys = set(svc._key_to_symbol.keys()) | set(svc._additional_keys)
    svc._s1_ingress_probe = probe
    probe.set_universe_supplier(svc._supply_s1_probe_universe)
    probe.set_attachment_verifier(svc._inspect_s1_streamer_attachment)
    return svc


def _build_realistic_wire_frame(
    *,
    target_instruments: int,
    include_options: int = 8,
    feed_type: int = 1,
    current_ts_ms: int = 1763010000500,
    use_first_level_greeks: bool = False,
) -> Tuple[bytes, Set[str], Dict[str, Dict[str, Any]]]:
    """Constructs an authentic serialized Upstox V3 protobuf `FeedResponse` payload."""
    resp = pb.FeedResponse()
    resp.type = feed_type
    if current_ts_ms > 0:
        resp.currentTs = current_ts_ms

    ist_today = datetime.datetime.now(IST_TZ).date()
    exp_3d = (ist_today + datetime.timedelta(days=3)).isoformat()
    exp_14d = (ist_today + datetime.timedelta(days=14)).isoformat()

    sub_keys: Set[str] = set(S1_CORE_INSTRUMENT_KEYS.keys())
    contracts: Dict[str, Dict[str, Any]] = {}

    # 1. NIFTY & VIX indices
    for idx_key, ltp_v in (("NSE_INDEX|Nifty 50", 24250.5), ("NSE_INDEX|India VIX", 14.35)):
        fd = resp.feeds[idx_key]
        fd.fullFeed.indexFF.ltpc.ltp = ltp_v
        fd.fullFeed.indexFF.ltpc.ltt = current_ts_ms - 15
        fd.fullFeed.indexFF.ltpc.cp = ltp_v * 0.995
        c1 = fd.fullFeed.indexFF.marketOHLC.ohlc.add()
        c1.interval = "I1"
        c1.open = ltp_v - 2.0
        c1.high = ltp_v + 3.0
        c1.low = ltp_v - 4.0
        c1.close = ltp_v
        c1.ts = current_ts_ms - 60000

    # 2. Core heavyweights (5-level depth)
    for eq_key, base_px in (
        ("NSE_EQ|INE002A01018", 1285.4),
        ("NSE_EQ|INE040A01034", 1742.1),
        ("NSE_EQ|INE090A01021", 1310.8),
    ):
        fd = resp.feeds[eq_key]
        mff = fd.fullFeed.marketFF
        mff.ltpc.ltp = base_px
        mff.ltpc.ltt = current_ts_ms - 10
        mff.ltpc.ltq = 25
        mff.ltpc.cp = base_px - 5.0
        mff.atp = base_px - 1.2
        mff.vtt = 1_500_000
        for lvl in range(5):
            q = mff.marketLevel.bidAskQuote.add()
            q.bidP = round(base_px - 0.05 * (lvl + 1), 2)
            q.bidQ = 100 * (lvl + 1)
            q.askP = round(base_px + 0.05 * (lvl + 1), 2)
            q.askQ = 120 * (lvl + 1)
        c1 = mff.marketOHLC.ohlc.add()
        c1.interval = "I1"
        c1.open = base_px - 1.0
        c1.high = base_px + 1.5
        c1.low = base_px - 2.0
        c1.close = base_px
        c1.vol = 12000
        c1.ts = current_ts_ms - 60000

    # 3. NIFTY options (1-7 DTE and >7 DTE)
    for opt_idx in range(include_options):
        ikey = f"NSE_FO|{45000 + opt_idx}"
        sub_keys.add(ikey)
        is_short = (opt_idx % 4 != 3)
        contracts[ikey] = {
            "underlying": "NIFTY",
            "expiry": exp_3d if is_short else exp_14d,
            "dte": 3 if is_short else 14,
            "lot_size": 65,
            "option_type": "CE" if opt_idx % 2 == 0 else "PE",
            "strike": 24000 + (opt_idx * 50),
        }
        fd = resp.feeds[ikey]
        px = 145.0 + opt_idx * 3.5
        if use_first_level_greeks:
            fl = fd.firstLevelWithGreeks
            fl.ltpc.ltp = px
            fl.ltpc.ltt = current_ts_ms - 5
            fl.ltpc.cp = px - 8.0
            fl.firstDepth.bidP = round(px - 0.10, 2)
            fl.firstDepth.bidQ = 130
            fl.firstDepth.askP = round(px + 0.10, 2)
            fl.firstDepth.askQ = 195
            fl.oi = 0.0 if opt_idx == 0 else 250000.0
            fl.optionGreeks.delta = 0.48
            fl.optionGreeks.theta = -11.5
        else:
            mff = fd.fullFeed.marketFF
            mff.ltpc.ltp = px
            mff.ltpc.ltt = current_ts_ms - 5
            mff.ltpc.cp = px - 8.0
            mff.oi = 0.0 if opt_idx == 0 else 325000.0
            mff.optionGreeks.delta = 0.51
            mff.optionGreeks.gamma = 0.0012
            mff.optionGreeks.theta = -12.4
            mff.optionGreeks.vega = 11.2
            for lvl in range(5):
                q = mff.marketLevel.bidAskQuote.add()
                q.bidP = round(px - 0.05 * (lvl + 1), 2)
                q.bidQ = 130 * (lvl + 1)
                q.askP = round(px + 0.05 * (lvl + 1), 2)
                q.askQ = 130 * (lvl + 1)

    # 4. Remaining non-S1 equities to reach `target_instruments`
    built = len(resp.feeds)
    for eq_i in range(max(0, target_instruments - built)):
        ek = f"NSE_EQ|SYNTH{eq_i:05d}"
        sub_keys.add(ek)
        fd = resp.feeds[ek]
        mff = fd.fullFeed.marketFF
        px = 500.0 + (eq_i % 400)
        mff.ltpc.ltp = px
        mff.ltpc.ltt = current_ts_ms - 20
        mff.ltpc.ltq = 10
        mff.ltpc.cp = px - 2.0
        mff.atp = px - 0.5
        mff.vtt = 95000
        for lvl in range(5):
            q = mff.marketLevel.bidAskQuote.add()
            q.bidP = round(px - 0.05 * (lvl + 1), 2)
            q.bidQ = 50 * (lvl + 1)
            q.askP = round(px + 0.05 * (lvl + 1), 2)
            q.askQ = 60 * (lvl + 1)
        c1 = mff.marketOHLC.ohlc.add()
        c1.interval = "I1"
        c1.open = px - 1.0
        c1.high = px + 2.0
        c1.low = px - 2.0
        c1.close = px
        c1.vol = 1500
        c1.ts = current_ts_ms - 60000

    return resp.SerializeToString(), sub_keys, contracts


def _make_offline_api_client_stub() -> Any:
    return types.SimpleNamespace(
        configuration=types.SimpleNamespace(access_token="offline-synthetic-token")
    )


def _make_attached_streamer(probe: S1IngressMeasurementProbe, svc: Any) -> Any:
    """Constructs an offline `InstrumentedMarketDataStreamerV3` without opening sockets."""
    WrappedCls = build_instrumented_streamer_class(upstox_client.MarketDataStreamerV3, probe)
    api_client = _make_offline_api_client_stub()
    st = WrappedCls(api_client, ["NSE_INDEX|Nifty 50"], mode="full")
    st.on("message", svc._handle_feed_message)
    attach_probe_lifecycle_listeners(st, probe)
    svc._streamer = st
    return st


class TestS1IngressProbeV2Remediation(unittest.TestCase):
    def setUp(self) -> None:
        self._case_dir = Path(tempfile.mkdtemp(dir=str(_TEST_TMP_ROOT)))
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "S1_INGRESS_PROBE_ENABLED",
                "S1_PROBE_ADMIN_TOKEN_SHA256",
                "S1_PROBE_READ_TOKEN_SHA256",
                "CORS_ALLOWED_ORIGINS",
            )
        }
        self.admin_token = "admin-secret-token-0123456789abcdef"
        self.read_token = "reader-secret-token-fedcba9876543210"
        os.environ["S1_PROBE_ADMIN_TOKEN_SHA256"] = hashlib.sha256(self.admin_token.encode()).hexdigest()
        os.environ["S1_PROBE_READ_TOKEN_SHA256"] = hashlib.sha256(self.read_token.encode()).hexdigest()

    def tearDown(self) -> None:
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.assertEqual(_AUDIT_VIOLATIONS, [], f"Isolation violations detected: {_AUDIT_VIOLATIONS}")

    # ──────────────────────────────────────────────────────────────────────────
    # H1: MANAGEMENT SECURITY, LOOPBACK, CSRF, CORS, ROLES, VALIDATION
    # ──────────────────────────────────────────────────────────────────────────

    def test_h1_default_off_ignores_env_and_enforces_auth_roles_csrf_loopback_and_strict_actions(self) -> None:
        os.environ["S1_INGRESS_PROBE_ENABLED"] = "true"
        probe = S1IngressMeasurementProbe(export_dir=self._case_dir / "exp")
        self.addCleanup(probe.shutdown_worker)
        # Environment auto-enable is strictly removed
        self.assertFalse(probe.enabled)

        svc = _make_isolated_stream_harness(probe)
        _make_attached_streamer(probe, svc)
        guard = S1ProbeManagementGuard()
        invoke, cors_origins, cors_kw = _load_isolated_main_endpoints(probe, guard)

        # Verify wildcard CORS with credentials is eliminated
        self.assertNotIn("*", cors_origins)
        self.assertIn("http://localhost:5173", cors_origins)
        self.assertTrue(ast.literal_eval(cors_kw["allow_credentials"]))

        # 1. Fail-closed (503) when tokens unconfigured
        os.environ.pop("S1_PROBE_ADMIN_TOKEN_SHA256", None)
        os.environ.pop("S1_PROBE_READ_TOKEN_SHA256", None)
        with self.assertRaises(HTTPException) as ctx503:
            invoke("get_s1_ingress_probe_summary", request=_FakeRequest("127.0.0.1"))
        self.assertEqual(ctx503.exception.status_code, 503)

        os.environ["S1_PROBE_ADMIN_TOKEN_SHA256"] = hashlib.sha256(self.admin_token.encode()).hexdigest()
        os.environ["S1_PROBE_READ_TOKEN_SHA256"] = hashlib.sha256(self.read_token.encode()).hexdigest()

        # 2. Missing bearer token -> 401
        with self.assertRaises(HTTPException) as ctx401:
            invoke("get_s1_ingress_probe_summary", request=_FakeRequest("127.0.0.1"))
        self.assertEqual(ctx401.exception.status_code, 401)

        admin_hdrs = {"Authorization": f"Bearer {self.admin_token}"}
        read_hdrs = {"Authorization": f"Bearer {self.read_token}"}

        # 3. Non-loopback peer -> 403
        with self.assertRaises(HTTPException) as ctx_lan:
            invoke("control_s1_ingress_probe", request=_FakeRequest("192.168.1.44", admin_hdrs), action="enable")
        self.assertEqual(ctx_lan.exception.status_code, 403)

        # 4. Reverse-proxy forwarded header -> 403
        with self.assertRaises(HTTPException) as ctx_fwd:
            invoke(
                "get_s1_ingress_probe_summary",
                request=_FakeRequest("127.0.0.1", {**read_hdrs, "X-Forwarded-For": "203.0.113.9"}),
            )
        self.assertEqual(ctx_fwd.exception.status_code, 403)

        # 5. Ambient Cookie / cross-site Sec-Fetch-Site / untrusted Origin -> 403
        for bad_hdr in (
            {"Cookie": "session=abc"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Origin": "https://attacker.example"},
        ):
            with self.assertRaises(HTTPException) as ctx_csrf:
                invoke("control_s1_ingress_probe", request=_FakeRequest("127.0.0.1", {**admin_hdrs, **bad_hdr}), action="enable")
            self.assertEqual(ctx_csrf.exception.status_code, 403)

        # 6. READ role can read summary, but CANNOT invoke control (403 INSUFFICIENT_ROLE)
        summary_ok = invoke("get_s1_ingress_probe_summary", request=_FakeRequest("127.0.0.1", read_hdrs))
        self.assertEqual(summary_ok["schema_version"], "S1_INGRESS_SUMMARY_V2")
        with self.assertRaises(HTTPException) as ctx_role:
            invoke("control_s1_ingress_probe", request=_FakeRequest("127.0.0.1", read_hdrs), action="enable")
        self.assertEqual(ctx_role.exception.status_code, 403)

        # 7. ADMIN enables probe; invalid action raises 422 and NEVER falls through to reset()!
        en_res = invoke("control_s1_ingress_probe", request=_FakeRequest("127.0.0.1", admin_hdrs), action="enable")
        self.assertTrue(en_res["enabled"])

        guard_unlimited = S1ProbeManagementGuard()
        guard_unlimited._write_limiter._capacity = 50.0
        guard_unlimited._write_limiter._tokens = 50.0
        invoke2, _, _ = _load_isolated_main_endpoints(probe, guard_unlimited)

        raw_wire, sub_k, contracts = _build_realistic_wire_frame(target_instruments=6, include_options=1)
        probe.set_universe_supplier(lambda: (sub_k, contracts))
        svc._streamer.handle_message(None, raw_wire)
        probe.flush_for_test_or_export()
        self.assertEqual(probe.get_summary()["ingress_totals"]["ingress_attempts_total"], 1)

        with self.assertRaises(HTTPException) as ctx_bad_act:
            invoke2("control_s1_ingress_probe", request=_FakeRequest("127.0.0.1", admin_hdrs), action="arbitrary_typo")
        self.assertEqual(ctx_bad_act.exception.status_code, 422)
        # Counters MUST remain intact (no destructive reset fallthrough!)
        self.assertTrue(probe.enabled)
        self.assertEqual(probe.get_summary()["ingress_totals"]["ingress_attempts_total"], 1)

        # 8. Reset while enabled is forbidden (409)
        with self.assertRaises(HTTPException) as ctx_rst_en:
            invoke2("control_s1_ingress_probe", request=_FakeRequest("127.0.0.1", admin_hdrs), action="reset")
        self.assertEqual(ctx_rst_en.exception.status_code, 409)

        # 9. Disable seals epoch, after which reset succeeds cleanly
        dis_res = invoke2("control_s1_ingress_probe", request=_FakeRequest("127.0.0.1", admin_hdrs), action="disable")
        self.assertFalse(dis_res["enabled"])
        self.assertTrue(dis_res["sealed"])
        rst_res = invoke2("control_s1_ingress_probe", request=_FakeRequest("127.0.0.1", admin_hdrs), action="reset")
        self.assertEqual(rst_res["new_epoch_seq"], 2)

        # 10. Rate limit enforcement (burst of 2 writes on fresh guard -> 3rd immediate call returns 429)
        guard_rl = S1ProbeManagementGuard()
        invoke_rl, _, _ = _load_isolated_main_endpoints(probe, guard_rl)
        invoke_rl("control_s1_ingress_probe", request=_FakeRequest("127.0.0.1", admin_hdrs), action="enable")
        invoke_rl("control_s1_ingress_probe", request=_FakeRequest("127.0.0.1", admin_hdrs), action="disable")
        with self.assertRaises(HTTPException) as ctx_429:
            invoke_rl("control_s1_ingress_probe", request=_FakeRequest("127.0.0.1", admin_hdrs), action="enable")
        self.assertEqual(ctx_429.exception.status_code, 429)

    # ──────────────────────────────────────────────────────────────────────────
    # H2: ZERO READER-LOCK CALLBACK CONTENTION & NONBLOCKING DISABLE
    # ──────────────────────────────────────────────────────────────────────────

    def test_h2_callback_and_disable_never_block_behind_control_or_summary_readers(self) -> None:
        probe = S1IngressMeasurementProbe(export_dir=self._case_dir / "h2")
        self.addCleanup(probe.shutdown_worker)
        svc = _make_isolated_stream_harness(probe)
        st = _make_attached_streamer(probe, svc)

        raw_wire, sub_k, contracts = _build_realistic_wire_frame(target_instruments=53, include_options=12)
        probe.set_universe_supplier(lambda: (sub_k, contracts))
        probe.enable()

        # Hold `_control_lock` for 200 ms on a separate thread while pushing live frames AND calling disable()!
        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def _slow_control_holder() -> None:
            with probe._control_lock:
                lock_acquired.set()
                release_lock.wait(timeout=1.0)

        holder = threading.Thread(target=_slow_control_holder, daemon=True)
        holder.start()
        self.assertTrue(lock_acquired.wait(timeout=0.5))

        try:
            t0 = time.perf_counter()
            for _ in range(20):
                st.handle_message(None, raw_wire)
            cb_elapsed_ms = (time.perf_counter() - t0) * 1000.0
            # 20 full 16KiB SDK decodes (~1.3ms each) must finish well below the 1000ms held `_control_lock`!
            self.assertLess(cb_elapsed_ms, 200.0)

            t_dis0 = time.perf_counter()
            probe.disable(flush_timeout_s=0.0)
            dis_elapsed_ms = (time.perf_counter() - t_dis0) * 1000.0
            self.assertFalse(probe.enabled)
            self.assertLess(dis_elapsed_ms, 10.0)
        finally:
            release_lock.set()
            holder.join(timeout=1.0)

        self.assertFalse(probe.enabled)

    # ──────────────────────────────────────────────────────────────────────────
    # H3 & M4: CONSERVATIVE HISTOGRAM BOUNDS, STAGE HOOK SUBTRACTION, CLOCK STEP
    # ──────────────────────────────────────────────────────────────────────────

    def test_h3_m4_conservative_upper_bound_percentiles_hook_subtraction_and_clock_step(self) -> None:
        # Codex exact counterexample: 98 samples at 75.01 us + 2 samples at 150.0 us
        hist = BoundedHistogram(LATENCY_BOUNDS_US)
        for _ in range(98):
            hist.observe(75.01)
        for _ in range(2):
            hist.observe(150.0)
        s = hist.summary(include_bins=True)
        self.assertEqual(s["exceed_100_count"], 2)
        self.assertEqual(s["exceed_1000_count"], 0)
        # Conservative nearest-rank upper bound for p99 MUST be >= 150.0 (never under-reported as 125.0!)
        self.assertEqual(s["p99_upper_bound"], 150.0)
        self.assertGreaterEqual(s["p999_upper_bound"], 150.0)

        # Verify stage hook overhead is subtracted from `app_downstream_dispatch_us` and added to `full_probe_added_callback_us`
        probe = S1IngressMeasurementProbe(export_dir=self._case_dir / "h3", require_streamer_attached_default=False)
        self.addCleanup(probe.shutdown_worker)
        probe.enable()
        probe.record_downstream_segments(listener_ns=40_000, equity_loop_ns=50_000, hook_overhead_ns=30_000)
        probe.record_ingress_frame(
            raw_bytes=512,
            is_bytes=True,
            wire_type_int=1,
            data_dict={"feeds": {}},
            recv_wall_ns=1_763_010_000_000_000_000,
            t0_ns=1_000_000_000,
            t1_ns=1_000_010_000,
            t2_ns=1_000_020_000,
            t3_ns=1_000_150_000,  # raw downstream = 130 us
            pre_overhead_ns=2_000,
        )
        # Inject +600ms wall-clock jump while monotonic advances only +10ms -> invalidates clock certification!
        probe.record_ingress_frame(
            raw_bytes=512,
            is_bytes=True,
            wire_type_int=1,
            data_dict={"feeds": {}},
            recv_wall_ns=1_763_010_000_610_000_000,
            t0_ns=1_010_000_000,
            t1_ns=1_010_010_000,
            t2_ns=1_010_020_000,
            t3_ns=1_010_100_000,
            pre_overhead_ns=1_000,
        )
        probe.flush_for_test_or_export()
        snap = probe.get_summary()
        # Raw downstream on frame 1 was 130 us; after subtracting >=30 us hook overhead, max app downstream <= 100 us!
        self.assertLessEqual(snap["latencies_us"]["app_downstream_dispatch_us"]["max"], 100.0)
        self.assertGreaterEqual(snap["latencies_us"]["full_probe_added_callback_us"]["max"], 32.0)
        self.assertFalse(snap["codex_feasibility_metrics"]["clock_certification_valid"])
        self.assertGreaterEqual(snap["codex_feasibility_metrics"]["wall_clock_step_events"], 1)

        # Verify latency circuit breaker auto-disables on 3 severe stalls (>5000 us)
        for i in range(3):
            probe.record_ingress_frame(
                raw_bytes=128,
                is_bytes=True,
                wire_type_int=1,
                data_dict={"feeds": {}},
                recv_wall_ns=1_763_010_001_000_000_000 + i * 1_000_000,
                t0_ns=2_000_000_000 + i * 1_000_000,
                t1_ns=2_000_005_000 + i * 1_000_000,
                t2_ns=2_000_010_000 + i * 1_000_000,
                t3_ns=2_000_020_000 + i * 1_000_000,
                pre_overhead_ns=6_000_000,  # 6000 us > 5000 us severe stall threshold
            )
        self.assertFalse(probe.enabled)
        self.assertEqual(probe.get_compact_status()["auto_disabled_reason"], "LATENCY_BUDGET_EXCEEDED")

    # ──────────────────────────────────────────────────────────────────────────
    # H4: TOTAL OBSERVER FAULT ISOLATION & SUPPLIER AUTO-DISABLE
    # ──────────────────────────────────────────────────────────────────────────

    def test_h4_probe_and_lifecycle_and_supplier_faults_never_interrupt_delivery(self) -> None:
        probe = S1IngressMeasurementProbe(export_dir=self._case_dir / "h4")
        self.addCleanup(probe.shutdown_worker)
        svc = _make_isolated_stream_harness(probe)
        st = _make_attached_streamer(probe, svc)

        delivered_count = [0]
        svc.register_feed_listener(lambda _f: delivered_count.__setitem__(0, delivered_count[0] + 1))

        raw_wire, sub_k, contracts = _build_realistic_wire_frame(target_instruments=6, include_options=1)
        probe.set_universe_supplier(lambda: (sub_k, contracts))
        probe.enable()

        # 1. Lifecycle listener receives unformattable object whose `__str__` raises RuntimeError
        class _BadStr:
            def __str__(self) -> str:
                raise RuntimeError("unformattable error payload")

        st.emit("error", _BadStr())  # Must NOT raise!

        # 2. Monkeypatch `record_ingress_frame` to raise RuntimeError directly -> outer guard catches & auto-disables at 5!
        orig_rec = probe.record_ingress_frame

        def _boom_rec(**_kw: Any) -> None:
            raise RuntimeError("injected post-emit probe crash")

        probe.record_ingress_frame = _boom_rec  # type: ignore[method-assign]
        try:
            for _ in range(6):
                st.handle_message(None, raw_wire)
        finally:
            probe.record_ingress_frame = orig_rec  # type: ignore[method-assign]

        # Every single frame was delivered to the trading listener exactly once without exception!
        self.assertEqual(delivered_count[0], 6)
        self.assertFalse(probe.enabled)
        self.assertTrue(probe.get_compact_status()["auto_disabled_on_fault"])

        # 3. Re-enable and verify 7 consecutive `universe_supplier` failures interleaved with frames auto-disable at 5!
        probe._agg_universe_refresh_interval_ns = 0

        def _broken_supplier() -> Tuple[Set[str], Dict[str, Dict[str, Any]]]:
            raise ValueError("universe supplier broken")

        probe.set_universe_supplier(_broken_supplier)
        probe.enable()  # Refresh #1 fails inside enable
        for _ in range(6):
            st.handle_message(None, raw_wire)
            probe.flush_for_test_or_export()  # Forces refresh #2..#7
        self.assertFalse(probe.enabled)
        self.assertTrue(probe.get_summary()["failures_and_bounds"]["auto_disabled_on_fault"])
        self.assertIn("UNIVERSE_SUPPLIER_FAULT", str(probe.get_summary()["failures_and_bounds"]["auto_disabled_reason"]))

        # 4. Corrupt protobuf still raises original DecodeError once and records pre-decode wire bytes & error counter
        probe.set_universe_supplier(lambda: (sub_k, contracts))
        probe.enable()
        with self.assertRaises(Exception):
            st.handle_message(None, b"\xff\xff\xff\x00corrupt_protobuf_wire")
        probe.flush_for_test_or_export()
        tot = probe.get_summary()["ingress_totals"]
        self.assertGreaterEqual(tot["sdk_decode_errors"], 1)
        self.assertEqual(
            tot["decoded_ok_frames"] + tot["sdk_decode_errors"] + tot["sdk_dict_errors"] + tot["downstream_emit_errors"],
            tot["ingress_attempts_total"],
        )

    # ──────────────────────────────────────────────────────────────────────────
    # H5: ASYNCHRONOUS MULTI-SESSION RETENTION, BUCKET FINALIZATION, TORN/QUOTA
    # ──────────────────────────────────────────────────────────────────────────

    def test_h5_multi_session_durable_ledger_bucket_finalization_torn_tail_and_disk_full(self) -> None:
        ledger_dir = self._case_dir / "multi_session_ledger"

        # Simulate 2 independent process boots sealing epochs into the same directory
        for session_idx in range(2):
            p = S1IngressMeasurementProbe(export_dir=ledger_dir, require_streamer_attached_default=False)
            svc = _make_isolated_stream_harness(p)
            st = _make_attached_streamer(p, svc)
            raw_wire, sub_k, contracts = _build_realistic_wire_frame(
                target_instruments=10, include_options=2, feed_type=0 if session_idx == 0 else 1
            )
            p.set_universe_supplier(lambda: (sub_k, contracts))
            p.enable()
            for _ in range(5):
                st.handle_message(None, raw_wire)
            dis = p.disable(flush_timeout_s=1.0)
            self.assertTrue(dis["sealed"])
            snap = p.get_summary(include_recent_rings=True)
            # Unfinished 1m/1s/100ms buckets MUST be finalized on disable (`0` stranded frames!)
            self.assertEqual(snap["burst_peaks_and_active_accumulators"]["active_unfinished_1m_frames"], 0)
            self.assertGreaterEqual(len(snap["recent_1m_rollups"]), 1)
            self.assertEqual(snap["durable_exporter"]["tail_state"], "COMPLETE_SEALED")
            p.shutdown_worker()

        # Append a simulated torn/truncated JSON line to the last ledger file
        ledger_files = sorted(ledger_dir.glob("ledger_*.jsonl"))
        self.assertEqual(len(ledger_files), 2)
        with open(ledger_files[-1], "ab") as f:
            f.write(b'{"schema_version":"S1_INGRESS_SUMMARY_V2","torn_crash_line":')

        verified = S1MeasurementSummaryExporter.load_and_verify_ledger_dir(ledger_dir)
        self.assertEqual(len(verified["sealed_epoch_ids"]), 2)
        self.assertEqual(len(verified["corrupt_or_torn_tail_lines"]), 1)
        # Merged raw histogram across both sessions has 5 + 5 = 10 callback samples!
        self.assertEqual(verified["merged_full_probe_added_callback_us"]["count"], 10)

        # Simulate disk-full / storage quota exhaustion -> live callback never blocks and window is NOT marked COMPLETE_SEALED!
        quota_dir = self._case_dir / "quota_full"
        p_full = S1IngressMeasurementProbe(export_dir=quota_dir, require_streamer_attached_default=False)
        self.addCleanup(p_full.shutdown_worker)
        p_full._exporter._max_storage_bytes = 64  # Tiny 64-byte quota to trigger storage cap immediately
        p_full.enable()
        p_full.record_ingress_frame(
            raw_bytes=256,
            is_bytes=True,
            wire_type_int=1,
            data_dict={"feeds": {}},
            recv_wall_ns=time.time_ns(),
            t0_ns=1000,
            t1_ns=2000,
            t2_ns=3000,
            t3_ns=4000,
        )
        p_full.disable(flush_timeout_s=1.0)
        full_snap = p_full.get_summary()
        self.assertTrue(full_snap["durable_exporter"]["quota_exhausted"])
        self.assertGreaterEqual(full_snap["durable_exporter"]["export_failures"], 1)
        self.assertEqual(full_snap["durable_exporter"]["tail_state"], "UNEXPORTED_LIVE_OR_INCOMPLETE_TAIL")

    # ──────────────────────────────────────────────────────────────────────────
    # M1, M2, M3, M5: BOUNDS, IST DTE, ROTATION, SILENCE, PROTOBUF ARMS, ATTACHMENT
    # ──────────────────────────────────────────────────────────────────────────

    def test_m1_m2_m3_m5_bounds_ist_expiry_rotation_silence_flwg_enum0_and_attachment(self) -> None:
        # M5: `enable()` fails closed (409) when streamer wrapper is NOT attached!
        probe_unattached = S1IngressMeasurementProbe(export_dir=self._case_dir / "m5")
        self.addCleanup(probe_unattached.shutdown_worker)
        svc_unattached = _make_isolated_stream_harness(probe_unattached)
        with self.assertRaises(ProbeControlError) as ctx_m5:
            probe_unattached.enable()
        self.assertEqual(ctx_m5.exception.status_code, 409)
        self.assertEqual(ctx_m5.exception.code, "STREAMER_WRAPPER_NOT_ATTACHED")
        del svc_unattached

        # Setup attached probe for M1/M2/M3 verification
        probe = S1IngressMeasurementProbe(export_dir=self._case_dir / "m123")
        self.addCleanup(probe.shutdown_worker)
        svc = _make_isolated_stream_harness(probe)
        st = _make_attached_streamer(probe, svc)

        ist_today = datetime.datetime.now(IST_TZ).date()
        today_str = ist_today.isoformat()
        plus3_str = (ist_today + datetime.timedelta(days=3)).isoformat()

        # Universe Epoch 1:
        # - NSE_FO|TODAY has stale cached `dte: 3`, but `expiry == today_ist` -> MUST classify as OTHER_DTE (0 DTE), NOT 1-7 DTE!
        # - NSE_FO|ROT_A and NSE_FO|ROT_B are 3-DTE NIFTY options
        current_u = [
            (
                {"NSE_INDEX|Nifty 50", "NSE_FO|TODAY", "NSE_FO|ROT_A", "NSE_FO|ROT_B"},
                {
                    "NSE_FO|TODAY": {"underlying": "NIFTY", "dte": 3, "expiry": today_str, "lot_size": 65},
                    "NSE_FO|ROT_A": {"underlying": "NIFTY", "dte": 3, "expiry": plus3_str, "lot_size": 65},
                    "NSE_FO|ROT_B": {"underlying": "NIFTY", "dte": 3, "expiry": plus3_str, "lot_size": 65},
                },
            )
        ]
        probe._agg_universe_refresh_interval_ns = 0
        probe.set_universe_supplier(lambda: current_u[0])
        probe.enable()

        # Send `initial_feed` (enum 0, omitted by MessageToDict) + `firstLevelWithGreeks` + `oi=0` + unknown NSE_FO key!
        resp0 = pb.FeedResponse()
        resp0.type = 0  # initial_feed
        resp0.currentTs = 1763010000000
        for k in ("NSE_FO|TODAY", "NSE_FO|ROT_A", "NSE_FO|ROT_B", "NSE_FO|MYSTERY_UNSUBSCRIBED"):
            fl = resp0.feeds[k].firstLevelWithGreeks
            fl.ltpc.ltp = 120.0
            fl.firstDepth.bidP = 119.9
            fl.firstDepth.bidQ = 65
            fl.firstDepth.askP = 120.1
            fl.firstDepth.askQ = 65
            fl.oi = 150000.0 if k == "NSE_FO|ROT_A" else 0.0
            fl.optionGreeks.delta = 0.49
        st.handle_message(None, resp0.SerializeToString())
        # Also send an explicit decoded dict carrying `"oi": 0` (proto3 wire omits default 0.0 scalar, while dicts preserve `"oi": 0`)
        probe.record_ingress_frame(
            raw_bytes=160,
            is_bytes=True,
            wire_type_int=1,
            data_dict={
                "type": "live_feed",
                "feeds": {
                    "NSE_FO|ROT_B": {
                        "firstLevelWithGreeks": {
                            "ltpc": {"ltp": 120.5},
                            "firstDepth": {"bidP": 120.4, "bidQ": 65, "askP": 120.6, "askQ": 65},
                            "oi": 0,
                        }
                    }
                },
            },
            recv_wall_ns=time.time_ns(),
            t0_ns=time.perf_counter_ns(),
            t1_ns=time.perf_counter_ns() + 1000,
            t2_ns=time.perf_counter_ns() + 2000,
            t3_ns=time.perf_counter_ns() + 3000,
        )
        probe.flush_for_test_or_export()

        s1 = probe.get_summary()
        # Enum 0 (`initial_feed`) captured despite protobuf3 default omission!
        self.assertEqual(s1["ingress_totals"]["wire_envelope_types"]["initial_feed"], 1)
        self.assertEqual(s1["ingress_totals"]["decoded_dict_type_omitted_default_enum0"], 1)
        # NSE_FO|TODAY classified as OTHER_DTE (1 update), ROT_A & ROT_B as 1_7_DTE (3 updates), MYSTERY as UNKNOWN_FO_UNCLASSIFIED (1 update)!
        self.assertEqual(s1["cohorts"]["NIFTY_OPTIONS_OTHER_DTE"]["updates"], 1)
        self.assertEqual(s1["cohorts"]["NIFTY_OPTIONS_1_7_DTE"]["updates"], 3)
        self.assertEqual(s1["cohorts"]["UNKNOWN_FO_UNCLASSIFIED"]["updates"], 1)
        # `firstLevelWithGreeks` book + `oi=0` presence vs positivity distinguished!
        c17 = s1["cohorts"]["NIFTY_OPTIONS_1_7_DTE"]
        self.assertEqual(c17["schema_arms"]["firstLevelWithGreeks"], 3)
        self.assertEqual(c17["book_usable_1lot_updates"], 3)
        self.assertEqual(c17["oi_field_present"], 2)
        self.assertEqual(c17["oi_positive_updates"], 1)
        # Dynamic S1 core intended count == 1 (`NSE_INDEX|Nifty 50`), NOT hardcoded 5!
        self.assertEqual(s1["delivery_coverage"]["s1_core_intended_subscribed"], ["NSE_INDEX|Nifty 50"])

        # Rotate Universe Epoch 2 -> shrink intended set to ONLY `{"NSE_FO|ROT_B"}`!
        current_u[0] = (
            {"NSE_FO|ROT_B"},
            {"NSE_FO|ROT_B": {"underlying": "NIFTY", "dte": 3, "expiry": plus3_str, "lot_size": 65}},
        )
        # Deliver update ONLY on ROT_B 100 seconds later (monotonic simulated) while ROT_A remains silent!
        probe.record_ingress_frame(
            raw_bytes=256,
            is_bytes=True,
            wire_type_int=1,
            data_dict={
                "type": "live_feed",
                "feeds": {
                    "NSE_FO|ROT_B": {"ltpc": {"ltp": 125.0}},
                    "NSE_FO|ROT_A": {"ltpc": {"ltp": 121.0}},  # Late packet for rotated-out strike!
                },
            },
            recv_wall_ns=time.time_ns(),
            t0_ns=time.perf_counter_ns() + 100_000_000_000,  # +100s later
            t1_ns=time.perf_counter_ns() + 100_000_005_000,
            t2_ns=time.perf_counter_ns() + 100_000_010_000,
            t3_ns=time.perf_counter_ns() + 100_000_020_000,
        )
        # Now send another frame +99s later where ONLY ROT_B ticks and ROT_A is silent!
        base_later = time.perf_counter_ns() + 199_000_000_000
        probe.record_ingress_frame(
            raw_bytes=128,
            is_bytes=True,
            wire_type_int=1,
            data_dict={"type": "live_feed", "feeds": {"NSE_FO|ROT_B": {"ltpc": {"ltp": 126.0}}}},
            recv_wall_ns=time.time_ns(),
            t0_ns=base_later,
            t1_ns=base_later + 5_000,
            t2_ns=base_later + 10_000,
            t3_ns=base_later + 20_000,
        )
        probe.flush_for_test_or_export()
        s2 = probe.get_summary()
        # Coverage MUST stay <= 1.0 (never 200%!) and ROT_A retained its NIFTY_OPTIONS_1_7_DTE identity via historical LRU!
        self.assertLessEqual(s2["delivery_coverage"]["current_epoch_coverage_ratio"], 1.0)
        self.assertEqual(s2["cohorts"]["NIFTY_OPTIONS_1_7_DTE"]["schema_arms"]["top_ltpc"], 3)
        # Per-instrument worst silence catches ROT_A's 99s silence (>= 98,000 ms)!
        self.assertGreaterEqual(
            s2["codex_feasibility_metrics"]["s1_option_book_and_time_provenance"]["per_instrument_worst_absence_or_silence_ms"],
            98_000.0,
        )

        # M1: Flood > 600 unique option keys + oversized key -> sets stay strictly bounded and `coverage_valid` flips False!
        many_contracts = {
            f"NSE_FO|FLOOD_{i}": {"underlying": "NIFTY", "expiry": plus3_str, "lot_size": 65}
            for i in range(620)
        }
        current_u[0] = (set(many_contracts.keys()), many_contracts)
        big_feeds = {f"NSE_FO|FLOOD_{i}": {"ltpc": {"ltp": 10.0}} for i in range(620)}
        big_feeds["X" * (MAX_KEY_CHARS + 25)] = {"ltpc": {"ltp": 10.0}}
        probe.record_ingress_frame(
            raw_bytes=4096,
            is_bytes=True,
            wire_type_int=1,
            data_dict={"type": "live_feed", "feeds": big_feeds},
            recv_wall_ns=time.time_ns(),
            t0_ns=base_later + 1_000_000,
            t1_ns=base_later + 1_005_000,
            t2_ns=base_later + 1_010_000,
            t3_ns=base_later + 1_020_000,
        )
        probe.flush_for_test_or_export()
        self.assertLessEqual(len(probe._agg_seen_opt_1_7_keys), MAX_TRACKED_OPTION_COHORT_KEYS)
        s3 = probe.get_summary()
        self.assertFalse(s3["delivery_coverage"]["coverage_valid"])
        self.assertGreaterEqual(s3["failures_and_bounds"]["oversized_key_drops"], 1)
        self.assertGreaterEqual(s3["failures_and_bounds"]["opt_cohort_key_cap_drops"], 1)

    def test_h5_enospc_and_inbox_wire_byte_starvation(self) -> None:
        import errno
        from unittest import mock

        # 1. Simulate OS-level ENOSPC during ledger write -> never raises into callback or worker!
        p_enospc = S1IngressMeasurementProbe(export_dir=self._case_dir / "enospc", require_streamer_attached_default=False)
        self.addCleanup(p_enospc.shutdown_worker)
        p_enospc.enable()
        p_enospc.record_ingress_frame(
            raw_bytes=512,
            is_bytes=True,
            wire_type_int=1,
            data_dict={"feeds": {}},
            recv_wall_ns=time.time_ns(),
            t0_ns=1000,
            t1_ns=2000,
            t2_ns=3000,
            t3_ns=4000,
        )
        with mock.patch("os.write", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            p_enospc.disable(flush_timeout_s=1.0)
        esnap = p_enospc.get_summary()
        self.assertGreaterEqual(esnap["durable_exporter"]["export_failures"], 1)
        self.assertIn("OSError", str(esnap["durable_exporter"]["last_export_error"]))
        self.assertEqual(esnap["durable_exporter"]["tail_state"], "UNEXPORTED_LIVE_OR_INCOMPLETE_TAIL")

        # 2. Simulate starved worker + multi-MiB frames exceeding `MAX_INBOX_WIRE_BYTES` (8 MiB)
        p_starve = S1IngressMeasurementProbe(export_dir=self._case_dir / "starve", require_streamer_attached_default=False)
        self.addCleanup(p_starve.shutdown_worker)
        # Flip capture flag without starting worker thread to deterministically starve `_inbox`
        p_starve._capture_enabled = True
        p_starve._active_epoch_seq = 1
        for _ in range(5):
            p_starve.record_ingress_frame(
                raw_bytes=2 * 1024 * 1024,  # 2 MiB per frame -> 5 frames = 10 MiB > 8 MiB cap!
                is_bytes=True,
                wire_type_int=1,
                data_dict={"feeds": {}},
                recv_wall_ns=time.time_ns(),
                t0_ns=1000,
                t1_ns=2000,
                t2_ns=3000,
                t3_ns=4000,
            )
        in_flight = p_starve._enqueued_wire_bytes_total - p_starve._dequeued_wire_bytes_total
        self.assertLessEqual(in_flight, MAX_INBOX_WIRE_BYTES)
        self.assertEqual(p_starve._inbox_overflow_drops, 1)
        p_starve.flush_for_test_or_export()
        ssnap = p_starve.get_summary()
        self.assertFalse(ssnap["delivery_coverage"]["coverage_valid"])
        self.assertIn("INBOX_OVERFLOW", ssnap["delivery_coverage"]["invalidation_reasons"])


def run_realistic_synthetic_benchmark() -> Dict[str, Any]:
    """
    H3 Realistic Benchmark Suite:
      - GC ENABLED throughout (`gc.isenabled() == True`).
      - Payloads: `1 KiB` (4 inst), `16 KiB` (53 inst), `64 KiB` (203 inst).
      - Interleaved randomized block order across `baseline` (unwrapped SDK),
        `wrapped_off` (default-OFF), and `wrapped_on` (enabled + concurrent reader + universe refresh).
      - Also runs paced `1,000 msg/s` and `5,000 msg/s` burst verification on `16 KiB` frames.
    """
    gc.enable()
    assert gc.isenabled(), "GC must remain enabled for realistic H3 validation"

    bench_dir = Path(tempfile.mkdtemp(dir=str(_TEST_TMP_ROOT), prefix="bench_"))
    payload_specs = [
        ("1KiB_4inst", 4, 1),
        ("16KiB_53inst", 53, 12),
        ("64KiB_203inst", 203, 30),
    ]

    results: Dict[str, Any] = {"gc_enabled": gc.isenabled(), "profiles": {}, "paced_runs": {}}

    for label, n_inst, n_opt in payload_specs:
        wire_bytes, sub_k, contracts = _build_realistic_wire_frame(
            target_instruments=n_inst, include_options=n_opt
        )
        probe = S1IngressMeasurementProbe(export_dir=bench_dir / label)
        probe._agg_universe_refresh_interval_ns = 50_000_000  # Force frequent cold refreshes (every 50ms) off-callback!
        svc_base = _make_isolated_stream_harness(
            S1IngressMeasurementProbe(export_dir=bench_dir / f"{label}_dummy", require_streamer_attached_default=False)
        )
        svc_wrap = _make_isolated_stream_harness(probe)

        client = _make_offline_api_client_stub()

        base_st = upstox_client.MarketDataStreamerV3(client, ["NSE_INDEX|Nifty 50"], mode="full")
        base_st.on("message", svc_base._handle_feed_message)
        wrap_st = _make_attached_streamer(probe, svc_wrap)
        probe.set_universe_supplier(lambda sk=sub_k, ct=contracts: (set(sk), dict(ct)))

        # Warmup
        for _ in range(80):
            base_st.handle_message(None, wire_bytes)
            wrap_st.handle_message(None, wire_bytes)

        # Concurrent summary reader thread to induce real reader/control activity during `wrapped_on`
        stop_reader = threading.Event()
        reader_calls = [0]

        def _concurrent_reader() -> None:
            while not stop_reader.is_set():
                probe.get_summary(include_recent_rings=True)
                probe.get_compact_status()
                reader_calls[0] += 1
                time.sleep(0.002)

        reader_th = threading.Thread(target=_concurrent_reader, daemon=True)
        reader_th.start()

        base_samples_us: List[float] = []
        off_samples_us: List[float] = []
        on_samples_us: List[float] = []

        rng = random.Random(20261113 + n_inst)
        blocks = ["base", "off", "on"] * 6  # 18 interleaved blocks * 100 msgs = 600 msgs per mode
        rng.shuffle(blocks)

        try:
            for mode in blocks:
                if mode == "base":
                    for _ in range(100):
                        t0 = time.perf_counter_ns()
                        base_st.handle_message(None, wire_bytes)
                        base_samples_us.append((time.perf_counter_ns() - t0) / 1000.0)
                elif mode == "off":
                    if probe.enabled:
                        probe.disable(flush_timeout_s=0.2)
                    for _ in range(100):
                        t0 = time.perf_counter_ns()
                        wrap_st.handle_message(None, wire_bytes)
                        off_samples_us.append((time.perf_counter_ns() - t0) / 1000.0)
                else:
                    if not probe.enabled:
                        probe.enable()
                    for _ in range(100):
                        t0 = time.perf_counter_ns()
                        wrap_st.handle_message(None, wire_bytes)
                        on_samples_us.append((time.perf_counter_ns() - t0) / 1000.0)
                        # Drain worker if approaching inbox cap during tight unpaced synthetic loop
                        if len(probe._inbox) > (MAX_INBOX_SAMPLES // 2):
                            probe.flush_for_test_or_export()
        finally:
            stop_reader.set()
            reader_th.join(timeout=1.0)

        if probe.enabled:
            probe.disable(flush_timeout_s=1.0)
        snap = probe.get_summary()
        probe.shutdown_worker()
        svc_base._s1_ingress_probe.shutdown_worker()

        def _pct(arr: List[float], q: float) -> float:
            s = sorted(arr)
            idx = min(len(s) - 1, max(0, int(q * len(s) + 0.999999) - 1))
            return round(s[idx], 2)

        gates = snap["codex_feasibility_metrics"]["callback_latency_gates_conservative"]
        results["profiles"][label] = {
            "wire_bytes": len(wire_bytes),
            "instruments": n_inst,
            "concurrent_reader_polls": reader_calls[0],
            "baseline_p50_us": _pct(base_samples_us, 0.50),
            "baseline_p99_us": _pct(base_samples_us, 0.99),
            "wrapped_off_p50_us": _pct(off_samples_us, 0.50),
            "wrapped_off_p99_us": _pct(off_samples_us, 0.99),
            "wrapped_on_total_p50_us": _pct(on_samples_us, 0.50),
            "wrapped_on_total_p99_us": _pct(on_samples_us, 0.99),
            "median_delta_on_minus_base_us": round(_pct(on_samples_us, 0.50) - _pct(base_samples_us, 0.50), 2),
            "probe_added_p99_upper_bound_us": gates["full_probe_added_p99_upper_bound_us"],
            "probe_added_p999_upper_bound_us": gates["full_probe_added_p999_upper_bound_us"],
            "probe_added_exceed_100us_count": gates["full_probe_added_exceed_100us_count"],
            "probe_added_exceed_1000us_count": gates["full_probe_added_exceed_1000us_count"],
            "meets_p99_le_100us": gates["full_probe_added_p99_upper_bound_us"] <= 100.0,
            "meets_p999_le_1000us": gates["full_probe_added_p999_upper_bound_us"] <= 1000.0,
            "passes_safety_gates": gates["passes_p99_le_100us_and_p999_le_1000us"],
            "inbox_overflow_drops": snap["failures_and_bounds"]["inbox_overflow_drops"],
        }

    # Paced 1,000 msg/s and 5,000 msg/s burst tests on 16 KiB (53-instrument) frames
    wire16, sub16, con16 = _build_realistic_wire_frame(target_instruments=53, include_options=12)
    for target_rate in (1000, 5000):
        probe_p = S1IngressMeasurementProbe(export_dir=bench_dir / f"paced_{target_rate}")
        svc_p = _make_isolated_stream_harness(probe_p)
        st_p = _make_attached_streamer(probe_p, svc_p)
        probe_p.set_universe_supplier(lambda: (sub16, con16))
        probe_p.enable()

        total_msgs = 400 if target_rate == 1000 else 1000
        interval_ns = int(1_000_000_000 / target_rate)
        t_start = time.perf_counter_ns()
        for i in range(total_msgs):
            target_ns = t_start + i * interval_ns
            while time.perf_counter_ns() < target_ns:
                pass
            st_p.handle_message(None, wire16)
        probe_p.disable(flush_timeout_s=1.5)
        psnap = probe_p.get_summary()
        probe_p.shutdown_worker()
        pgates = psnap["codex_feasibility_metrics"]["callback_latency_gates_conservative"]
        results["paced_runs"][f"{target_rate}_msg_per_s"] = {
            "messages_sent": total_msgs,
            "decoded_ok_frames": psnap["ingress_totals"]["decoded_ok_frames"],
            "inbox_overflow_drops": psnap["failures_and_bounds"]["inbox_overflow_drops"],
            "probe_added_p99_upper_bound_us": pgates["full_probe_added_p99_upper_bound_us"],
            "probe_added_p999_upper_bound_us": pgates["full_probe_added_p999_upper_bound_us"],
            "meets_p99_le_100us": pgates["full_probe_added_p99_upper_bound_us"] <= 100.0,
            "meets_p999_le_1000us": pgates["full_probe_added_p999_upper_bound_us"] <= 1000.0,
            "passes_safety_gates": pgates["passes_p99_le_100us_and_p999_le_1000us"],
        }

    return results


if __name__ == "__main__":
    if "--benchmark" in sys.argv:
        sys.argv.remove("--benchmark")
        bench_out = run_realistic_synthetic_benchmark()
        print("BENCHMARK_JSON_START")
        print(json.dumps(bench_out, indent=2))
        print("BENCHMARK_JSON_END")
        for prof_name, prof_metrics in bench_out["profiles"].items():
            assert prof_metrics["passes_safety_gates"], f"Benchmark profile failed safety gate: {prof_name}: {prof_metrics}"
        for rate_name, rate_metrics in bench_out["paced_runs"].items():
            assert rate_metrics["passes_safety_gates"], f"Paced run failed safety gate: {rate_name}: {rate_metrics}"
    unittest.main()
