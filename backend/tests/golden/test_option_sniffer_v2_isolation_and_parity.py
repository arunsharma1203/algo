"""
Golden Isolation, Paper-Only Invariant, Cold-Start GET, Thread-Safe SQLite Booking,
Real OptionStreamManager Owner-Pinning & V1/V2 Parallel Parity Tests for V2 Option Sniffer.
"""

import ast
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from typing import Any, Dict, List

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.analytics.option_sniffer_v2.v2_config import (
    V2PaperOnlyInvariantError,
    load_v2_preregistration,
    parse_ist_timestamp_ms,
    validate_v2_paper_invariants,
)
from app.analytics.option_sniffer_v2.v2_ledger_db import (
    V2DatabaseIsolationError,
    get_v2_connection,
    set_v2_test_db_override,
)
from app.analytics.option_sniffer_v2.v2_reproducible_audit_cli import (
    run_parallel_parity_and_10k_benchmark,
)
from app.analytics.option_sniffer_v2.v2_service import (
    OptionSnifferV2Service,
    get_option_sniffer_v2_service,
    reset_option_sniffer_v2_service,
)
from app.api import option_sniffer_v2_api
from app.data.database import get_canonical_db_path


FORBIDDEN_LIVE_IMPORT_TOKENS = (
    "option_live_adapter",
    "option_live_order_intent",
    "shadow_mirror_executor",
    "upstox_live",
    "fetch_single_quote_emergency_rest",
    "/live/arm",
    "/live/disarm",
    "place_order",
    "modify_order",
    "cancel_order",
)


class _DummyWsAdapter:
    def __init__(self) -> None:
        self.subscribed: List[List[str]] = []
        self.unsubscribed: List[List[str]] = []

    def subscribe(self, keys: List[str], mode: str = "full_d30") -> None:
        self.subscribed.append(sorted(keys))

    def unsubscribe(self, keys: List[str]) -> None:
        self.unsubscribed.append(sorted(keys))


def _import_option_stream_manager() -> Any:
    try:
        from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

        return OptionStreamManager
    except ModuleNotFoundError:
        # System /usr/bin/python3 lacks optional numpy/pandas pulled by V1 stream/db helpers;
        # import real OptionStreamManager locking/pinning logic with scoped leaf stubs and restore sys.modules.
        prev_stream = sys.modules.get("app.data.upstox_stream_service")
        prev_db = sys.modules.get("app.analytics.option_sniffer.option_db")
        stub_stream = types.ModuleType("app.data.upstox_stream_service")
        stub_stream.get_stream_service = lambda: None  # type: ignore
        stub_db = types.ModuleType("app.analytics.option_sniffer.option_db")
        stub_db.OptionDB = object  # type: ignore
        sys.modules["app.data.upstox_stream_service"] = stub_stream
        sys.modules["app.analytics.option_sniffer.option_db"] = stub_db
        try:
            sys.modules.pop("app.analytics.option_sniffer.option_stream_manager", None)
            from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

            return OptionStreamManager
        finally:
            if prev_stream is not None:
                sys.modules["app.data.upstox_stream_service"] = prev_stream
            else:
                sys.modules.pop("app.data.upstox_stream_service", None)
            if prev_db is not None:
                sys.modules["app.analytics.option_sniffer.option_db"] = prev_db
            else:
                sys.modules.pop("app.analytics.option_sniffer.option_db", None)


def _make_isolated_stream_manager() -> Any:
    """Builds a truly isolated non-singleton OptionStreamManager instance without touching shared daemon."""
    OptionStreamManagerCls = _import_option_stream_manager()
    mgr = object.__new__(OptionStreamManagerCls)
    mgr._lock = threading.RLock()
    mgr.is_running = True
    mgr._initialized = True
    mgr._key_to_contract = {}
    mgr._symbol_to_key = {}
    mgr._pinned_extra_keys = set()
    mgr._pin_owners_by_key = {}
    mgr._v1_core_chain_keys = set()
    mgr._advance_feed_listeners = []
    mgr._streamer = _DummyWsAdapter()
    mgr._subscribe_keys_queue = set()
    mgr._unsubscribe_keys_queue = set()
    mgr._underlying_quotes = {}
    mgr._option_quotes = {}
    mgr._futures_quotes = {}
    mgr._last_tick_ts = 0.0
    return mgr


class TestV2OptionSnifferIsolationAndParity(unittest.TestCase):
    def setUp(self) -> None:
        reset_option_sniffer_v2_service()
        self._td = tempfile.TemporaryDirectory()
        self.v2_db = os.path.join(self._td.name, "v2_golden.db")
        set_v2_test_db_override(self.v2_db)

    def tearDown(self) -> None:
        reset_option_sniffer_v2_service()
        set_v2_test_db_override(None)
        self._td.cleanup()

    def test_01_ast_dependency_graph_excludes_all_live_execution_and_arm_paths(self) -> None:
        v2_dir = os.path.join(_BACKEND_DIR, "app", "analytics", "option_sniffer_v2")
        targets = [
            os.path.join(v2_dir, fn)
            for fn in sorted(os.listdir(v2_dir))
            if fn.endswith(".py")
        ] + [os.path.join(_BACKEND_DIR, "app", "api", "option_sniffer_v2_api.py")]

        for path in targets:
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src, filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for bad in FORBIDDEN_LIVE_IMPORT_TOKENS:
                            self.assertNotIn(bad, alias.name, f"Forbidden import {alias.name} in {path}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    for bad in FORBIDDEN_LIVE_IMPORT_TOKENS:
                        self.assertNotIn(bad, mod, f"Forbidden import-from {mod} in {path}")
                elif isinstance(node, ast.Attribute):
                    for bad in ("fetch_single_quote_emergency_rest", "place_order", "modify_order", "cancel_order"):
                        self.assertNotEqual(node.attr, bad, f"Forbidden call/attribute {node.attr} in {path}")

        fe_files = [
            os.path.join(_REPO_ROOT, "frontend", "src", "pages", "OptionSnifferAdvanceV2.jsx"),
        ]
        fe_dir = os.path.join(_REPO_ROOT, "frontend", "src", "pages", "optionSnifferV2")
        for fn in sorted(os.listdir(fe_dir)):
            if fn.endswith((".js", ".jsx")):
                fe_files.append(os.path.join(fe_dir, fn))
        for fp in fe_files:
            with open(fp, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertNotIn("method: 'POST'", content)
            self.assertNotIn('method: "POST"', content)
            self.assertNotIn("/live/arm", content)

    def test_02_paper_only_invariants_cannot_be_toggled_live_and_db_is_isolated(self) -> None:
        cfg, _ = load_v2_preregistration()
        self.assertTrue(cfg["simulation_mode"])
        self.assertFalse(cfg["live_capable_by_config"])
        self.assertEqual(cfg["execution_adapter"], "LOCAL_PAPER_SIMULATOR_ONLY")

        for field, bad_val in (
            ("simulation_mode", False),
            ("live_capable_by_config", True),
            ("execution_adapter", "UPSTOX_LIVE"),
            ("naked_short_allowed", True),
            ("allow_live_arm", True),
            ("allow_broker_orders", True),
        ):
            mut = dict(cfg)
            mut[field] = bad_val
            with self.assertRaises(V2PaperOnlyInvariantError):
                validate_v2_paper_invariants(mut)

        with self.assertRaises(V2DatabaseIsolationError):
            get_v2_connection(get_canonical_db_path())

    def test_03a_cold_start_get_leaves_db_file_nonexistent_and_zero_mutations(self) -> None:
        cold_db = os.path.join(self._td.name, "never_created_cold.db")
        set_v2_test_db_override(cold_db)
        svc = get_option_sniffer_v2_service(cold_db)
        self.assertFalse(os.path.exists(cold_db))

        ov = option_sniffer_v2_api.get_v2_overview(include_collateral=False)
        pos = option_sniffer_v2_api.get_v2_positions()
        cmp_d = option_sniffer_v2_api.get_v2_daily_comparison()
        rep = option_sniffer_v2_api.get_v2_replay_audit()
        cf = option_sniffer_v2_api.get_v2_counterfactuals()
        diag = option_sniffer_v2_api.get_v2_diagnostics()

        self.assertFalse(os.path.exists(cold_db), "Cold-start GET created V2 SQLite file unexpectedly!")
        self.assertEqual(ov["collection_status"]["collector_state"], "COLD_NOT_INITIALIZED")
        self.assertFalse(ov["collection_status"]["db_exists"])
        self.assertFalse(cf["included_in_v2_totals"])
        self.assertEqual(pos["forward_open_positions"], [])
        self.assertIsNotNone(cmp_d["v1_reported_summary"])
        self.assertIn("phase4_o3_audit", rep)
        self.assertTrue(diag["protected_artifacts_integrity"]["all_unchanged"])

    def test_03b_readonly_get_endpoints_cause_zero_collector_starts_pins_or_db_writes(self) -> None:
        svc = get_option_sniffer_v2_service(self.v2_db)
        svc.store.ensure_initialized()
        before_state = svc.collector_state
        before_attached = svc._listener_attached
        before_pins = svc.subscription_registry.snapshot_status()["v2_owned_only_keys"]
        conn = svc.store.connect()
        before_counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0]
            for t in ("osv2_packages", "osv2_signals", "osv2_ledger_entries", "osv2_events_ingest", "osv2_daily_snapshots")
        }
        conn.close()

        for _ in range(3):
            ov = option_sniffer_v2_api.get_v2_overview(include_collateral=True)
            pos = option_sniffer_v2_api.get_v2_positions()
            cmp_d = option_sniffer_v2_api.get_v2_daily_comparison()
            rep = option_sniffer_v2_api.get_v2_replay_audit()
            cf = option_sniffer_v2_api.get_v2_counterfactuals()
            diag = option_sniffer_v2_api.get_v2_diagnostics()
            self.assertTrue(ov["simulation_mode"])
            self.assertFalse(ov["live_capable_by_config"])
            self.assertIn("forward_open_positions", pos)
            self.assertIn("archive_daily_comparison", cmp_d)
            self.assertIn("phase4_o3_audit", rep)
            self.assertFalse(cf["included_in_v2_totals"])
            self.assertTrue(diag["protected_artifacts_integrity"]["all_unchanged"])

        self.assertEqual(svc.collector_state, before_state)
        self.assertEqual(svc._listener_attached, before_attached)
        self.assertEqual(svc.subscription_registry.snapshot_status()["v2_owned_only_keys"], before_pins)

        conn2 = svc.store.connect()
        after_counts = {
            t: conn2.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0]
            for t in ("osv2_packages", "osv2_signals", "osv2_ledger_entries", "osv2_events_ingest", "osv2_daily_snapshots")
        }
        conn2.close()
        self.assertEqual(before_counts, after_counts)

    def test_04_frontend_client_url_matches_backend_routes_and_no_fabrication(self) -> None:
        # Execute real frontend ESM module against Node & verify exact FastAPI routes + 404/partial-failure behavior
        route_paths = {r["path"] for r in getattr(option_sniffer_v2_api.router, "routes", []) if r.get("method") == "GET"}
        script = """
        globalThis.window = { location: { hostname: '127.0.0.1' } };
        const mod = await import('./frontend/src/pages/optionSnifferV2/v2ApiClient.js');
        if (mod.V2_API_ROOT.includes('/api/api')) throw new Error('Double /api prefix detected: ' + mod.V2_API_ROOT);
        const generated = Object.values(mod.V2_ENDPOINTS).map(p => new URL(mod.buildV2EndpointUrl(p)).pathname);
        const res1 = await mod.fetchAllV2EndpointsReadOnly({
          fetchFn: async (url) => {
            if (url.includes('/diagnostics') || url.includes('/overview')) return { ok: false, status: 404 };
            return { ok: true, status: 200, json: async () => ({ ok: true }) };
          }
        });
        if (res1.overview.data !== null || !res1.overview.unavailable) throw new Error('404 overview fabricated data!');
        if (res1.diagnostics.data !== null || !res1.diagnostics.unavailable) throw new Error('404 diagnostics fabricated data!');
        if (res1.positions.status !== 'fresh') throw new Error('Healthy sibling endpoint dropped on partial failure!');
        console.log(JSON.stringify({ root: mod.V2_API_ROOT, generated }));
        """
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        import json as _json
        payload = _json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["root"], "http://127.0.0.1:8000/api/option-sniffer-advance-v2")
        self.assertEqual(set(payload["generated"]), route_paths)

        # Ensure error handlers in OptionSnifferAdvanceV2.jsx never blame pending restart on 404
        with open(os.path.join(_REPO_ROOT, "frontend", "src", "pages", "OptionSnifferAdvanceV2.jsx"), "r", encoding="utf-8") as fh:
            page_src = fh.read()
        self.assertNotIn("pending_restart", page_src)
        self.assertNotIn("has not been restarted", page_src)
        self.assertNotIn("all_unchanged: true", page_src)

    def test_05_parallel_v1_off_vs_v2_on_parity_and_clean_v2_shutdown(self) -> None:
        bench = run_parallel_parity_and_10k_benchmark(self.v2_db)
        self.assertEqual(bench["total_events"], 10000)
        self.assertTrue(bench["v1_traces_byte_identical"])
        self.assertTrue(bench["v1_listener_preserved_after_v2_shutdown"])
        self.assertTrue(bench["v1_pins_preserved_after_v2_shutdown"])
        self.assertEqual(bench["dropped_overflow_events"], 0)
        self.assertEqual(bench["broker_api_calls"], 0)

    def test_06_multithreaded_startup_worker_http_concurrency_and_booking_fault_halt(self) -> None:
        svc = get_option_sniffer_v2_service(self.v2_db)
        svc.start_parallel_collector(stream_manager=_make_isolated_stream_manager(), run_archive_warmup=False, spawn_worker=True)

        errors: List[Exception] = []
        base_ms = parse_ist_timestamp_ms("2026-11-20T09:15:00+05:30")
        assert base_ms is not None

        def worker_feeder() -> None:
            try:
                for m in range(6):
                    ts_tick = base_ms + m * 60_000 + 30_000
                    svc.on_live_feed_events(
                        [
                            ("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25800.0 + m, "availability_ts_ms": ts_tick, "exchange_ltt_ms": ts_tick, "source_ts_proven": True}),
                            ("FUTURES", "NIFTY", {"instrument_key": "NSE_FO|NIFTYFUTNOV", "underlying": "NIFTY", "expiry": "2026-11-26", "fut_ltp": 25840.0 + m, "availability_ts_ms": ts_tick, "exchange_ltt_ms": ts_tick, "source_ts_proven": True}),
                        ]
                    )
                    svc.process_queued_events()
            except Exception as exc:
                errors.append(exc)

        def archive_writer() -> None:
            try:
                with svc.store.writer_session():
                    for k in range(5):
                        svc.store.upsert_daily_snapshot(
                            {
                                "run_mode": "REPLAY_V2_1_CAUSAL",
                                "config_hash": svc.config_hash,
                                "session_date": f"2026-11-1{6 + k}",
                                "sod_equity_inr": 200000.0,
                                "eod_realized_equity_inr": 200000.0,
                                "eod_mtm_equity_inr": 200000.0,
                                "intraday_min_mtm_equity_inr": 200000.0,
                                "peak_reserved_capital_inr": 0.0,
                                "closed_trades": 0,
                                "open_overnight_packages": 0,
                                "unresolved_exposure_count": 0,
                                "net_pnl_inr": 0.0,
                                "session_state": "COMPLETE_CLEAN",
                            }
                        )
            except Exception as exc:
                errors.append(exc)

        def http_reader() -> None:
            try:
                for _ in range(8):
                    _ = option_sniffer_v2_api.get_v2_overview()
                    _ = option_sniffer_v2_api.get_v2_positions()
                    _ = option_sniffer_v2_api.get_v2_diagnostics()
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=archive_writer),
            threading.Thread(target=worker_feeder),
            threading.Thread(target=http_reader),
            threading.Thread(target=http_reader),
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10.0)
        svc.stop_v2_only()

        self.assertEqual(errors, [], f"Concurrent SQLite threads raised errors: {errors}")

        # Verify persistent write failure latches DEGRADED_BOOKING_FAULT without phantom in-memory booking
        orig_exec = svc.store.execute_atomic_transition

        def _failing_transition(*args: Any, **kwargs: Any) -> None:
            raise sqlite3.OperationalError("simulated disk full during transition")

        svc.store.execute_atomic_transition = _failing_transition  # type: ignore
        c = {"instrument_key": "NSE_FO|CE25800", "trading_symbol": "NIFTY 25800 CE 26 NOV 26", "underlying": "NIFTY", "expiry": "2026-11-26", "strike": 25800.0, "option_type": "CE", "lot_size": 65}
        q = {
            **c, "raw_bid": 120.0, "raw_ask": 120.5, "bid": 120.0, "ask": 120.5,
            "depth_levels": [{"bid_p": 120.0, "bid_q": 195, "ask_p": 120.5, "ask_q": 195}],
            "is_observed_l1_book": True, "depth_observed_on_tick": True, "book_provenance": "OBSERVED_V3_L5",
            "l1_book_timestamp_ms": base_ms, "depth_timestamp_ms": base_ms, "local_receipt_ts_ms": base_ms,
        }
        res_fail = svc.simulator.open_book_b_paid_long("sig_fault", "V2_FROZEN_COMBINED", "2026-11-20", "2026-11-20 10:30:00", base_ms, c, q, 25800.0, {})
        self.assertFalse(res_fail["ok"])
        self.assertIn("BOOKING_FAILURE_HALTED", res_fail["reason"])
        self.assertTrue(svc.simulator.is_booking_halted())
        self.assertEqual(len(svc.simulator.open_packages), 0)
        self.assertEqual(svc.simulator.reserved_capital_inr, 0.0)

        # Second booking attempt while halted must be refused outright before touching store
        svc.store.execute_atomic_transition = orig_exec  # type: ignore
        res_halted = svc.simulator.open_book_b_paid_long("sig_fault2", "V2_FROZEN_COMBINED", "2026-11-20", "2026-11-20 10:31:00", base_ms + 60_000, c, q, 25800.0, {})
        self.assertFalse(res_halted["ok"])
        self.assertEqual(res_halted["reason"], "BLOCKED_BY_BOOKING_FAULT")
        self.assertEqual(svc.get_overview()["collection_status"]["collector_state"], "DEGRADED_BOOKING_FAULT")

        rec = option_sniffer_v2_api.post_v2_reconcile_booking_fault()
        self.assertTrue(rec["reconciled"])
        self.assertFalse(svc.simulator.is_booking_halted())

    def test_07_real_option_stream_manager_atomic_owner_pinning_and_v1_parity(self) -> None:
        mgr = _make_isolated_stream_manager()
        shared_contract = {"instrument_key": "NSE_FO|SHARED_25800CE", "trading_symbol": "NIFTY 25800 CE 26 NOV 26", "expiry": "2026-11-26"}
        v2_only_contract = {"instrument_key": "NSE_FO|V2_ONLY_25900CE", "trading_symbol": "NIFTY 25900 CE 26 NOV 26", "expiry": "2026-11-26"}

        # Sequence A: V2 acquires first -> V1 acquires same key -> V2 releases -> key stays pinned for V1!
        acq1 = mgr.acquire_owned_pins("V2", [shared_contract, v2_only_contract], max_owner_pins=8, soft_cap=100, v1_headroom=10)
        self.assertIn("NSE_FO|SHARED_25800CE", acq1["pinned_exclusively"])
        mgr.pin_extra_contracts([{**shared_contract, "is_pinned_active_leg": True}], owner="V1")
        self.assertTrue(mgr._key_to_contract["NSE_FO|SHARED_25800CE"].get("is_pinned_active_leg"))

        rel_v2 = mgr.release_owned_pins("V2", None)
        self.assertIn("NSE_FO|V2_ONLY_25900CE", rel_v2["released_from_stream"])
        self.assertNotIn("NSE_FO|V2_ONLY_25900CE", mgr._key_to_contract)
        self.assertNotIn("NSE_FO|SHARED_25800CE", rel_v2["released_from_stream"])
        self.assertIn("NSE_FO|SHARED_25800CE", rel_v2["retained_for_other_owner"])
        self.assertIn("NSE_FO|SHARED_25800CE", mgr._key_to_contract)
        self.assertEqual(mgr._pin_owners_by_key.get("NSE_FO|SHARED_25800CE"), {"V1"})
        self.assertTrue(mgr._key_to_contract["NSE_FO|SHARED_25800CE"].get("is_pinned_active_leg"))

        # Duplicate V2 release is clean no-op
        rel_dup = mgr.release_owned_pins("V2", None)
        self.assertEqual(rel_dup["released_from_stream"], [])

        # Sequence B: V1 already owns key -> V2 acquires -> pressure yield drops ONLY V2-only, preserves V1!
        mgr.acquire_owned_pins("V2", [shared_contract, v2_only_contract], max_owner_pins=8, soft_cap=100, v1_headroom=10)
        victims = mgr.yield_owner_pins_under_pressure("V2")
        self.assertEqual(victims, ["NSE_FO|V2_ONLY_25900CE"])
        self.assertIn("NSE_FO|SHARED_25800CE", mgr._key_to_contract)

        # V1 unpins shared_contract while V2 still holds it -> retained for V2; then V2 releases -> unpinned cleanly!
        mgr.unpin_extra_contracts(["NSE_FO|SHARED_25800CE"], owner="V1")
        self.assertIn("NSE_FO|SHARED_25800CE", mgr._key_to_contract)
        mgr.release_owned_pins("V2", ["NSE_FO|SHARED_25800CE"])
        self.assertNotIn("NSE_FO|SHARED_25800CE", mgr._pin_owners_by_key)


if __name__ == "__main__":
    unittest.main()
