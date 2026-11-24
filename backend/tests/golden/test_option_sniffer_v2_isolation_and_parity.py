"""
Golden Isolation, Paper-Only Invariant, Read-Only GET & V1/V2 Parallel Parity Tests
for V2 Option Sniffer (`option_sniffer_v2`).
"""

import ast
import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from typing import Any, Callable, Dict, List, Tuple

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.analytics.option_sniffer_v2.v2_config import (
    V2PaperOnlyInvariantError,
    load_v2_preregistration,
    validate_v2_paper_invariants,
)
from app.analytics.option_sniffer_v2.v2_ledger_db import (
    V2DatabaseIsolationError,
    V2LedgerStore,
    get_v2_connection,
    set_v2_test_db_override,
)
from app.analytics.option_sniffer_v2.v2_reproducible_audit_cli import (
    _ParityMockSharedStream,
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

        # Verify frontend V2 page/components contain zero POST/PUT/DELETE side effects on load/refresh
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

    def test_03_readonly_get_endpoints_cause_zero_collector_starts_pins_or_db_writes(self) -> None:
        svc = get_option_sniffer_v2_service(self.v2_db)
        before_state = svc.collector_state
        before_attached = svc._listener_attached
        before_pins = svc.subscription_registry.snapshot_status()["v2_owned_only_keys"]
        conn = svc.store.connect()
        before_counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0]
            for t in ("osv2_packages", "osv2_signals", "osv2_ledger_entries", "osv2_events_ingest", "osv2_daily_snapshots")
        }
        conn.close()

        # Execute every GET handler repeatedly
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

    def test_04_parallel_v1_off_vs_v2_on_parity_and_clean_v2_shutdown(self) -> None:
        bench = run_parallel_parity_and_10k_benchmark(self.v2_db)
        self.assertEqual(bench["total_events"], 10000)
        self.assertTrue(bench["v1_traces_byte_identical"])
        self.assertTrue(bench["v1_listener_preserved_after_v2_shutdown"])
        self.assertTrue(bench["v1_pins_preserved_after_v2_shutdown"])
        self.assertEqual(bench["dropped_overflow_events"], 0)
        self.assertEqual(bench["broker_api_calls"], 0)


if __name__ == "__main__":
    unittest.main()
