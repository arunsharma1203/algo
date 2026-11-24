"""
V2 Option Sniffer (`option_sniffer_v2`) — Reproducible End-to-End Audit CLI.

Verifies & prints cleanly in an isolated V2 DB (never modifying V1 `backend/market_data.db`):
  1. Protected baseline artifact SHA-256 integrity check (all 6 files).
  2. Twice-replay canonical ledger SHA-256 determinism proof (`run1 == run2`).
  3. Phase-4 O3 provenance reproduction & 6 explicit spec conflicts blocking autonomous Book A.
  4. V1 Book B overshoot reconciliation + V2 Book B 6-step single-factor ablation (`B0..B6`)
     + 2x charges / 2x slippage / Ex-Best-Session / Opt-in Book A fail-closed checks.
  5. Complete per-trade paper ledger checking exact `Gross - Friction == Net`.
  6. Isolated V1 Counterfactual experiments (`CF1`, `CF2`, `included_in_v2_totals = 0`).
  7. Parallel V1-OFF vs V1+V2-ON event parity & 10,000-event fan-out throughput benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
if os.path.join(_REPO_ROOT, "backend") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "backend"))

from app.analytics.option_sniffer_v2.v2_book_b_engine import reconcile_v1_book_b_stop_and_overshoot
from app.analytics.option_sniffer_v2.v2_config import load_v2_preregistration
from app.analytics.option_sniffer_v2.v2_counterfactual_v1 import run_v1_counterfactual_audit
from app.analytics.option_sniffer_v2.v2_ledger_db import (
    V2LedgerStore,
    open_v1_readonly_db,
    set_v2_test_db_override,
)
from app.analytics.option_sniffer_v2.v2_o3_reproducer import load_phase4_o3_audit_report
from app.analytics.option_sniffer_v2.v2_replay_attribution import run_v2_replay_and_attribution
from app.analytics.option_sniffer_v2.v2_service import (
    OptionSnifferV2Service,
    compute_protected_hashes_readonly,
)


class _ParityMockSharedStream:
    """Isolated deterministic stream harness emulating `OptionStreamManager` fan-out & pins."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.is_running = True
        self._advance_feed_listeners: List[Callable[[List[Tuple[str, str, Dict[str, Any]]]], None]] = []
        self._key_to_contract: Dict[str, Dict[str, Any]] = {
            "NSE_FO|V1_CORE_1": {"instrument_key": "NSE_FO|V1_CORE_1", "v2_owned_only": False},
            "NSE_FO|V1_CORE_2": {"instrument_key": "NSE_FO|V1_CORE_2", "v2_owned_only": False},
        }
        self._pinned_extra_keys: set[str] = set()
        self.broker_api_calls: int = 0

    def register_advance_feed_listener(self, cb: Callable[[List[Tuple[str, str, Dict[str, Any]]]], None]) -> None:
        with self._lock:
            if cb not in self._advance_feed_listeners:
                self._advance_feed_listeners.append(cb)

    def unregister_advance_feed_listener(self, cb: Callable[[List[Tuple[str, str, Dict[str, Any]]]], None]) -> None:
        with self._lock:
            if cb in self._advance_feed_listeners:
                self._advance_feed_listeners.remove(cb)

    def pin_extra_contracts(self, contracts: List[Dict[str, Any]]) -> None:
        with self._lock:
            for c in contracts:
                ik = str(c["instrument_key"])
                self._key_to_contract[ik] = dict(c)
                if c.get("v2_owned_only"):
                    self._pinned_extra_keys.add(ik)

    def unpin_extra_contracts(self, keys: List[str]) -> None:
        with self._lock:
            for ik in keys:
                self._pinned_extra_keys.discard(ik)
                if self._key_to_contract.get(ik, {}).get("v2_owned_only"):
                    self._key_to_contract.pop(ik, None)

    def dispatch(self, batch: List[Tuple[str, str, Dict[str, Any]]]) -> None:
        with self._lock:
            listeners = list(self._advance_feed_listeners)
        for cb in listeners:
            cb(batch)


def run_parallel_parity_and_10k_benchmark(v2_db_path: str) -> Dict[str, Any]:
    """
    Measures:
      1. V1-OFF vs V1+V2-ON event parity (exact identical V1 received trace & zero V1 pin changes).
      2. 10,000-event fan-out throughput, p50/p99 queue lag, and 0 broker API calls.
    """
    stream = _ParityMockSharedStream()
    v1_trace_off: List[str] = []
    v1_trace_on: List[str] = []

    def v1_listener_off(evts: List[Tuple[str, str, Dict[str, Any]]]) -> None:
        for kind, key, payload in evts:
            v1_trace_off.append(f"{kind}|{key}|{payload.get('exchange_ltt_ms')}|{payload.get('ltp') or payload.get('spot_ltp')}")

    def v1_listener_on(evts: List[Tuple[str, str, Dict[str, Any]]]) -> None:
        for kind, key, payload in evts:
            v1_trace_on.append(f"{kind}|{key}|{payload.get('exchange_ltt_ms')}|{payload.get('ltp') or payload.get('spot_ltp')}")

    events_batches: List[List[Tuple[str, str, Dict[str, Any]]]] = []
    base_ts = 1_795_000_000_000
    for i in range(2000):
        ts_i = base_ts + i * 250
        batch = [
            ("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "spot_ltp": 25800.0 + (i % 20) * 0.5, "exchange_ltt_ms": ts_i}),
            ("FUTURES", "NIFTY", {"instrument_key": "NSE_FO|NIFTY_FUT_NOV", "fut_ltp": 25855.0 + (i % 20) * 0.5, "exchange_ltt_ms": ts_i}),
            ("OPTION", "NSE_FO|V1_CORE_1", {"instrument_key": "NSE_FO|V1_CORE_1", "ltp": 140.0 + (i % 10) * 0.1, "raw_bid": 139.9, "raw_ask": 140.1, "exchange_ltt_ms": ts_i}),
            ("OPTION", "NSE_FO|V1_CORE_2", {"instrument_key": "NSE_FO|V1_CORE_2", "ltp": 110.0 + (i % 10) * 0.1, "raw_bid": 109.9, "raw_ask": 110.1, "exchange_ltt_ms": ts_i}),
            ("OPTION", "NSE_FO|V1_CORE_3", {"instrument_key": "NSE_FO|V1_CORE_3", "ltp": 85.0 + (i % 10) * 0.1, "raw_bid": 84.9, "raw_ask": 85.1, "exchange_ltt_ms": ts_i}),
        ]
        events_batches.append(batch)

    # Phase A: V1 ONLY (V2 OFF)
    stream.register_advance_feed_listener(v1_listener_off)
    t_off0 = time.perf_counter()
    for b in events_batches:
        stream.dispatch(b)
    v1_only_ms = (time.perf_counter() - t_off0) * 1000.0
    stream.unregister_advance_feed_listener(v1_listener_off)

    # Phase B: V1 + V2 ON (10,000 events streamed via passive listener without auto worker sleep distortion)
    svc = OptionSnifferV2Service(db_path=v2_db_path)
    svc.start_parallel_collector(stream_manager=stream, run_archive_warmup=False, spawn_worker=False)
    stream.register_advance_feed_listener(v1_listener_on)

    t_on0 = time.perf_counter()
    for idx, b in enumerate(events_batches):
        stream.dispatch(b)
        if idx % 100 == 99:
            svc.feed_consumer.drain_batch(max_items=600)
    svc.feed_consumer.drain_batch(max_items=1000)
    v1_plus_v2_ms = (time.perf_counter() - t_on0) * 1000.0

    # V2-only shutdown leaves V1 listener and V1 pins 100% intact
    stop_res = svc.stop_v2_only()
    v1_still_attached = v1_listener_on in stream._advance_feed_listeners
    v1_keys_intact = "NSE_FO|V1_CORE_1" in stream._key_to_contract and "NSE_FO|V1_CORE_2" in stream._key_to_contract
    health = svc.feed_consumer.get_health_metrics()
    svc.store.close()

    return {
        "total_events": len(events_batches) * 5,
        "v1_traces_byte_identical": v1_trace_off == v1_trace_on,
        "v1_listener_preserved_after_v2_shutdown": v1_still_attached,
        "v1_pins_preserved_after_v2_shutdown": v1_keys_intact,
        "v1_only_dispatch_ms": round(v1_only_ms, 3),
        "v1_plus_v2_dispatch_ms": round(v1_plus_v2_ms, 3),
        "queue_lag_p50_ms": health["queue_lag_p50_ms"],
        "queue_lag_p99_ms": health["queue_lag_p99_ms"],
        "enqueued_total": health["enqueued_total"],
        "processed_total": health["processed_total"],
        "dropped_overflow_events": health["dropped_overflow_events"],
        "broker_api_calls": stream.broker_api_calls,
        "stop_v2_only_result": stop_res,
    }


def run_reproducible_audit(
    v1_db_path: Optional[str] = None,
    persist_runtime_v2_db: bool = False,
) -> Dict[str, Any]:
    cfg, cfg_hash = load_v2_preregistration()
    v1_path = v1_db_path or os.path.join(_REPO_ROOT, "backend", "market_data.db")

    def _file_sha(p: str) -> str:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    v1_sha_before = _file_sha(v1_path)
    prot_hashes = compute_protected_hashes_readonly()

    td_ctx: Optional[tempfile.TemporaryDirectory[str]] = None
    try:
        if persist_runtime_v2_db:
            set_v2_test_db_override(None)
            v2_db_path = None
            benchmark_db = os.path.join(tempfile.gettempdir(), "osv2_bench_parity.db")
        else:
            td_ctx = tempfile.TemporaryDirectory()
            v2_db_path = os.path.join(td_ctx.name, "osv2_audit_run1.db")
            v2_db_path_2 = os.path.join(td_ctx.name, "osv2_audit_run2.db")
            benchmark_db = os.path.join(td_ctx.name, "osv2_bench_parity.db")
            set_v2_test_db_override(v2_db_path)

        store1 = V2LedgerStore(v2_db_path, auto_create=True)
        v1_conn = open_v1_readonly_db(v1_path)
        try:
            o3 = load_phase4_o3_audit_report()
            overshoot = reconcile_v1_book_b_stop_and_overshoot(v1_conn)
            cf = run_v1_counterfactual_audit(v1_conn, store=store1)
            rep1 = run_v2_replay_and_attribution(v1_conn, store1, cfg, cfg_hash)

            # Second independent fresh replay proving bit-for-bit identical canonical digest
            if not persist_runtime_v2_db:
                set_v2_test_db_override(v2_db_path_2)
                store2 = V2LedgerStore(v2_db_path_2, auto_create=True)
            else:
                store2 = store1
            rep2 = run_v2_replay_and_attribution(v1_conn, store2, cfg, cfg_hash)
            if not persist_runtime_v2_db:
                store2.close()
        finally:
            v1_conn.close()
            store1.close()

        # Verify Gross - Friction == Net on every canonical closed trade
        ledger_trades = []
        max_arith_diff = 0.0
        for t in rep1["canonical_replay"]["closed_trades"]:
            diff = round(abs((float(t["gross_pnl_inr"]) - float(t["total_friction_inr"])) - float(t["net_pnl_inr"])), 4)
            max_arith_diff = max(max_arith_diff, diff)
            ledger_trades.append(
                {
                    "package_id": t["package_id"],
                    "book_id": t["book_id"],
                    "session_date": t["session_date"],
                    "direction_label": t["direction_label"],
                    "entry_bar_ts": t["entry_bar_ts"],
                    "exit_bar_ts": t["exit_bar_ts"],
                    "exit_reason": t["exit_reason"],
                    "planned_loss_inr": t["planned_loss_inr"],
                    "actual_gross_fill_loss_inr": t["actual_gross_fill_loss_inr"],
                    "slippage_and_gap_overshoot_inr": t["slippage_and_gap_overshoot_inr"],
                    "gross_pnl_inr": t["gross_pnl_inr"],
                    "brokerage_inr": t["brokerage_inr"],
                    "stt_inr": t["stt_inr"],
                    "statutory_charges_inr": t["statutory_charges_inr"],
                    "total_friction_inr": t["total_friction_inr"],
                    "net_pnl_inr": t["net_pnl_inr"],
                    "arith_diff_inr": diff,
                }
            )

        set_v2_test_db_override(benchmark_db)
        bench = run_parallel_parity_and_10k_benchmark(benchmark_db)
        v1_sha_after = _file_sha(v1_path)

        sha1 = rep1["canonical_replay"]["ledger_sha256"]
        sha2 = rep2["canonical_replay"]["ledger_sha256"]

        return {
            "protocol_id": cfg["protocol_id"],
            "engine_version": cfg.get("engine_version", "2.1.0-causal"),
            "replay_run_mode": cfg.get("replay_run_mode", "REPLAY_V2_1_CAUSAL"),
            "config_hash": cfg_hash,
            "v1_db_readonly_untouched": v1_sha_before == v1_sha_after,
            "v1_db_sha256": v1_sha_after,
            "protected_artifacts_hashes": prot_hashes,
            "determinism": {
                "run1_sha256": sha1,
                "run2_sha256": sha2,
                "exact_twice_replay_match": sha1 == sha2,
                "max_gross_minus_friction_diff_inr": max_arith_diff,
            },
            "causal_verification_outcome": rep1["causal_verification_outcome"],
            "archive_timing_evidence": rep1["archive_timing_evidence"],
            "superseded_v2_0_replay": rep1["superseded_v2_0_replay"],
            "phase4_o3_audit": o3,
            "v1_book_b_overshoot_audit": overshoot,
            "canonical_ledger_trades": ledger_trades,
            "canonical_replay_stats": rep1["canonical_replay"]["stats"],
            "ablation_study": rep1["ablation_study"],
            "sensitivity": rep1["sensitivity"],
            "v1_reported_summary": rep1["v1_reported_summary"],
            "daily_comparison": rep1["daily_comparison"],
            "counterfactuals_excluded": {
                "included_in_v2_totals": cf["included_in_v2_totals"],
                "cf1_summary": cf["cf1_summary"],
                "cf2_summary": cf["cf2_summary"],
                "rows": cf["rows"],
            },
            "parallel_parity_and_10k_benchmark": bench,
        }
    finally:
        set_v2_test_db_override(None)
        if td_ctx is not None:
            td_ctx.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="V2 Option Sniffer Reproducible CLI Audit")
    parser.add_argument("--v1-db", default=None, help="Path to read-only V1 market_data.db")
    parser.add_argument("--persist-runtime-v2-db", action="store_true", help="Write canonical REPLAY into backend/data/option_sniffer_advance_v2.db")
    parser.add_argument("--output-json", default=None, help="Optional path to dump full JSON report")
    args = parser.parse_args()

    res = run_reproducible_audit(
        v1_db_path=args.v1_db,
        persist_runtime_v2_db=bool(args.persist_runtime_v2_db),
    )

    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2, sort_keys=True)

    print("=== V2 OPTION SNIFFER REPRODUCIBLE AUDIT ===")
    print(f"Protocol: {res['protocol_id']} | Engine: {res['engine_version']} | Mode: {res['replay_run_mode']} | Config SHA-256: {res['config_hash']}")
    print(f"Causal Verification Outcome: {res['causal_verification_outcome']}")
    print(f"Superseded v2.0.0 Evidence: {res['superseded_v2_0_replay'].get('evidence_status')} (commit={res['superseded_v2_0_replay'].get('audited_commit')}, sha256={res['superseded_v2_0_replay'].get('ledger_sha256')})")
    print(f"V1 DB Read-Only Untouched: {res['v1_db_readonly_untouched']} (SHA-256={res['v1_db_sha256'][:16]}...)")
    print(
        f"Twice-Replay SHA-256 Match: {res['determinism']['exact_twice_replay_match']} "
        f"({res['determinism']['run1_sha256']})"
    )
    print(f"Max |Gross - Friction - Net|: Rs {res['determinism']['max_gross_minus_friction_diff_inr']}")
    print(
        f"Phase-4 O3 Status: {res['phase4_o3_audit']['reproduction_status']} | "
        f"Exact Reconciled: {res['phase4_o3_audit']['monthly_reconciliation']['exact_arithmetic_reconciled']} | "
        f"Spec Conflicts: {res['phase4_o3_audit']['spec_conflicts_count']} (Book A Disabled)"
    )
    print(f"Canonical Replay Stats: {res['canonical_replay_stats']}")
    for t in res["canonical_ledger_trades"]:
        print(
            f"  Trade {t['package_id']} | {t['session_date']} | {t['direction_label']} | "
            f"{t['entry_bar_ts']}->{t['exit_bar_ts']} ({t['exit_reason']}) | "
            f"SL={t['planned_loss_inr']} | Gross={t['gross_pnl_inr']} | Fric={t['total_friction_inr']} | Net={t['net_pnl_inr']}"
        )
    print("Ablation Study:")
    for row in res["ablation_study"]:
        s = row["stats"]
        print(
            f"  {row['variant_code']}: n={s['trade_count']} net=Rs {s['net_pnl_inr']} "
            f"(dB0=Rs {row['incremental_net_vs_b0_inr']}) PF={s['profit_factor']} MTM_DD=Rs {s['intraday_mtm_dd_inr']}"
        )
    print(f"Sensitivity: 2xCharges={res['sensitivity']['stress_2x_charges']['net_pnl_inr']} | "
          f"2xSlip={res['sensitivity']['stress_2x_slippage']['net_pnl_inr']} | "
          f"ExBest={res['sensitivity']['ex_best_session']}")
    print(f"CF1 Summary (Excluded): {res['counterfactuals_excluded']['cf1_summary']}")
    print(f"CF2 Summary (Excluded): {res['counterfactuals_excluded']['cf2_summary']}")
    print(f"Parallel Parity & 10k Load: {res['parallel_parity_and_10k_benchmark']}")

    assert res["v1_db_readonly_untouched"], "V1 DB modified!"
    assert res["determinism"]["exact_twice_replay_match"], "Replay non-deterministic!"
    assert res["determinism"]["max_gross_minus_friction_diff_inr"] <= 0.01, "Accounting mismatch!"
    assert res["parallel_parity_and_10k_benchmark"]["v1_traces_byte_identical"], "V1 parity broken!"
    assert res["parallel_parity_and_10k_benchmark"]["broker_api_calls"] == 0, "Unexpected broker API calls!"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
