import json
import os
import sys
import tempfile
import time
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.analytics.common.options_accounting import OptionLeg, compute_package_accounting
from app.analytics.option_sniffer_v2.v2_book_b_engine import evaluate_book_b_entry_signal
from app.analytics.option_sniffer_v2.v2_config import (
    load_v2_preregistration,
    parse_ist_timestamp_ms,
)
from app.analytics.option_sniffer_v2.v2_dedicated_feed import evaluate_feed_architecture_options
from app.analytics.option_sniffer_v2.v2_feed_consumer import V2BoundedFeedConsumer
from app.analytics.option_sniffer_v2.v2_ledger_db import (
    _DEFAULT_V2_DB_PATH,
    V2LedgerStore,
    get_canonical_db_path,
    set_v2_test_db_override,
)
from app.analytics.option_sniffer_v2.v2_paper_simulator import (
    V2PaperSimulator,
    audit_historical_drawdown_separation,
)
from app.analytics.option_sniffer_v2.v2_regime_arbitrator import (
    check_candidate_arbitration_gate,
    evaluate_v2_regime_arbitration,
)
from app.analytics.option_sniffer_v2.v2_service import (
    OptionSnifferV2Service,
    get_option_sniffer_v2_service,
    reset_option_sniffer_v2_service,
)
from app.analytics.option_sniffer_v2.v2_unified_engine import V2UnifiedEventEngine
from app.analytics.option_sniffer_v2_live import (
    BrokerOrderRejectedError,
    MockV2BrokerClient,
    UpstoxV2BrokerClient,
    V2LiveDatabaseIsolationError,
    V2LiveExecutionEngine,
    V2LiveLedgerStore,
    get_option_sniffer_v2_live_engine,
    reset_option_sniffer_v2_live_engine,
    sanitize_upstox_tag,
    set_v2_live_test_db_override,
    validate_upstox_v3_gtt_payload,
)
from app.analytics.option_sniffer_v2_live.v2_upstox_broker_client import normalize_market_protection
from app.api import option_sniffer_v2_api, option_sniffer_v2_live_api


def _sample_single_signal(sig_id: str = "sig_test_001", ts_date: str = "2026-11-27") -> dict:
    return {
        "signal_id": sig_id,
        "session_date": ts_date,
        "book_id": "V2_BOOK_B_PAID_FLOW",
        "protocol_profile_id": "V2_BOOK_B_FROZEN_PAPER",
        "structure": "SINGLE_LONG",
        "quote_fresh_ok": True,
        "buy_leg": {
            "instrument_key": "NSE_FO|47836",
            "trading_symbol": "NIFTY 25700 PE 01 DEC 26",
            "option_type": "PE",
            "strike": 25700.0,
            "qty": 65,
            "lot_size": 65,
            "limit_price": 180.0,
            "order_type": "LIMIT",
            "stop_trigger_price": 152.0,
            "stop_limit_price": 150.5,
        },
    }


def _sample_spread_signal(sig_id: str = "sig_spread_001", buy_qty: int = 130) -> dict:
    return {
        "signal_id": sig_id,
        "session_date": "2026-11-27",
        "book_id": "V2_BOOK_B_PAID_FLOW",
        "protocol_profile_id": "V2_BOOK_B_FROZEN_PAPER",
        "structure": "DEFINED_RISK_SPREAD",
        "quote_fresh_ok": True,
        "buy_leg": {
            "instrument_key": "NSE_FO|BUY_WING_1",
            "trading_symbol": "NIFTY 25500 CE 01 DEC 26",
            "option_type": "CE",
            "strike": 25500.0,
            "qty": buy_qty,
            "lot_size": 65,
            "limit_price": 45.0,
            "order_type": "LIMIT",
        },
        "short_leg": {
            "instrument_key": "NSE_FO|SHORT_LEG_1",
            "trading_symbol": "NIFTY 25300 CE 01 DEC 26",
            "option_type": "CE",
            "strike": 25300.0,
            "qty": buy_qty,
            "lot_size": 65,
            "limit_price": 165.0,
            "order_type": "LIMIT",
        },
    }


class TestV2UpgradesAndIsolatedLiveLayer(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.v2_db = os.path.join(self._td.name, "v2_paper.db")
        self.live_db = os.path.join(self._td.name, "v2_live.db")
        reset_option_sniffer_v2_service()
        reset_option_sniffer_v2_live_engine()
        set_v2_test_db_override(self.v2_db)
        set_v2_live_test_db_override(self.live_db)
        self.cfg, self.cfg_hash = load_v2_preregistration()

    def tearDown(self) -> None:
        reset_option_sniffer_v2_service()
        reset_option_sniffer_v2_live_engine()
        set_v2_test_db_override(None)
        set_v2_live_test_db_override(None)
        self._td.cleanup()

    def test_01_nov25_unpriced_mtm_spike_drawdown_separation_and_stale_exposure(self) -> None:
        # Reproduce 25 Nov pattern: peak equity 205,357.65, observed +3,288.12 -> unpriced -16,192.38 -> +3,586.55
        eq_curve = [
            {"seq_no": 165, "event_kind": "BAR_MTM", "session_date": "2026-11-25", "ts_ms": 1795588200000, "conservative_mtm_inr": 0.0, "marked_equity_inr": 205357.65, "drawdown_inr": 0.0},
            {"seq_no": 166, "event_kind": "BAR_MTM", "session_date": "2026-11-25", "ts_ms": 1795588260000, "conservative_mtm_inr": 3288.12, "marked_equity_inr": 203288.12, "drawdown_inr": 2069.53},
            {"seq_no": 167, "event_kind": "BAR_MTM", "session_date": "2026-11-25", "ts_ms": 1795588320000, "conservative_mtm_inr": -16192.38, "marked_equity_inr": 183807.62, "drawdown_inr": 21550.03},
            {"seq_no": 168, "event_kind": "BAR_MTM", "session_date": "2026-11-25", "ts_ms": 1795588380000, "conservative_mtm_inr": 3586.55, "marked_equity_inr": 203586.55, "drawdown_inr": 1771.10},
        ]
        pkgs = [
            {
                "package_id": "pkg_nov25",
                "state": "CLOSED",
                "entry_ts_ms": 1795583820000,
                "exit_ts_ms": 1795588800000,
                "structural_max_loss_inr": 16192.38,
            }
        ]
        sep = audit_historical_drawdown_separation(eq_curve, pkgs, starting_capital_inr=200000.0)
        self.assertTrue(sep["has_unpriced_stress_inflation"])
        self.assertEqual(sep["unpriced_stress_spikes_count"], 1)
        self.assertEqual(sep["unpriced_stress_spikes"][0]["seq_no"], 167)
        self.assertAlmostEqual(sep["observed_mark_max_dd_inr"], 2069.53, places=2)
        self.assertAlmostEqual(sep["stress_max_dd_inr"], 21550.03, places=2)

        store = V2LedgerStore(self.v2_db, auto_create=True)
        try:
            inserted = store.record_metric_correction_idempotent(
                {
                    "correction_id": "corr_nov25_dd_001",
                    "correction_version": "v2.2.1",
                    "run_mode": "FORWARD_PAPER",
                    "config_hash": self.cfg_hash,
                    "target_table": "osv2_equity_curve",
                    "session_date": "2026-11-25",
                    "metric_name": "intraday_mtm_dd_inr",
                    "original_value": 21550.03,
                    "corrected_value": {"observed_mark_max_dd_inr": 2069.53, "stress_max_dd_inr": 21550.03},
                    "reason": "Separate unpriced gap stress from synchronized L5 observed DD without mutating ledger.",
                    "evidence": {"seq_no": 167},
                    "created_ts_ms": 1795588400000,
                }
            )
            self.assertTrue(inserted)
            self.assertEqual(len(store.load_metric_corrections("FORWARD_PAPER", self.cfg_hash)), 1)

            # Verify open unpriced exposure reports fresh=None, last_reliable, and conservative stress separately
            sim = V2PaperSimulator(self.cfg, self.cfg_hash, store, run_mode="FORWARD_PAPER")
            t_open = 1795583820000
            open_res = sim.open_book_b_paid_long(
                signal_id="sig_open_stale",
                candidate_variant="V2_FROZEN_COMBINED",
                session_date="2026-11-25",
                bar_ts="2026-11-25 10:47:00",
                decision_ts_ms=t_open,
                contract={"instrument_key": "NSE_FO|47847", "trading_symbol": "NIFTY 25500 CE 01 DEC 26", "expiry": "2026-12-01", "strike": 25500.0, "option_type": "CE", "lot_size": 65},
                quote={"instrument_key": "NSE_FO|47847", "bid": 240.0, "ask": 240.5, "raw_bid": 240.0, "raw_ask": 240.5, "l1_book_timestamp_ms": t_open - 100, "depth_timestamp_ms": t_open - 100, "depth_observed_on_tick": True, "l1_observed_on_tick": True, "source_ts_proven": True, "book_provenance": "OBSERVED_V3_L5", "depth_levels": [{"bid_p": 240.0, "bid_q": 650, "ask_p": 240.5, "ask_q": 650}]},
                spot_price=25650.0,
                basis_meta={"variant": "V2_FROZEN_COMBINED"},
                used_single_otm_fallback=False,
            )
            self.assertTrue(open_res["ok"], f"Expected open_book_b_paid_long to succeed: {open_res}")
            # Subsequent unpriced bar at +60s (stale L5 quote age 15,268ms > 5000ms)
            sim.mark_open_packages(
                quote_by_ikey={
                    "NSE_FO|47847": {
                        "instrument_key": "NSE_FO|47847",
                        "bid": 250.0,
                        "ask": 250.5,
                        "raw_bid": 250.0,
                        "raw_ask": 250.5,
                        "l1_book_timestamp_ms": t_open + 60_000 - 15268,
                        "depth_timestamp_ms": t_open + 60_000 - 15268,
                        "depth_observed_on_tick": True,
                        "l1_observed_on_tick": True,
                        "source_ts_proven": True,
                        "book_provenance": "OBSERVED_V3_L5",
                        "depth_levels": [{"bid_p": 250.0, "bid_q": 650, "ask_p": 250.5, "ask_q": 650}],
                    }
                },
                session_date="2026-11-25",
                bar_ts="2026-11-25 12:02:00",
                now_ms=t_open + 60_000,
            )
            svc = get_option_sniffer_v2_service(self.v2_db)
            pos = svc.get_positions()
            self.assertEqual(len(pos["forward_open_positions"]), 1)
            open_p = pos["forward_open_positions"][0]
            self.assertTrue(open_p["is_unpriced_exposure"])
            self.assertTrue(open_p["is_stale_mark"])
            self.assertIsNone(open_p["fresh_executable_mark_inr"])
            self.assertIsNotNone(open_p["last_reliable_mark_net_inr"])
            self.assertLess(open_p["conservative_stress_estimate_inr"], -10000.0)
            svc.store.close()
        finally:
            store.close()

    def test_02_nov26_feed_skew_open_basis_fail_reason_persistence(self) -> None:
        store = V2LedgerStore(self.v2_db, auto_create=True)
        try:
            sim = V2PaperSimulator(self.cfg, self.cfg_hash, store, run_mode="FORWARD_PAPER")
            eng = V2UnifiedEventEngine(self.cfg, self.cfg_hash, store, sim, run_mode="FORWARD_PAPER", persist_bars_and_checkpoints=True)
            t0 = parse_ist_timestamp_ms("2026-11-26T09:15:00+05:30")
            assert t0 is not None
            # Feed skewed Spot & Futures across 3 bars (09:15..09:17) where Spot-Fut skew = 18,000ms (>5,000ms)
            for m in range(4):
                bar_t = t0 + m * 60_000 + 30_000
                eng.ingest_event(
                    "INDEX",
                    "NIFTY",
                    {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25800.0 + m, "exchange_ltt_ms": bar_t, "availability_ts_ms": bar_t, "source_ts_proven": True},
                    availability_ts_ms=bar_t,
                )
                eng.ingest_event(
                    "FUTURES",
                    "NIFTY",
                    {"instrument_key": "NSE_FO|58875", "underlying": "NIFTY", "expiry": "2026-12-29", "fut_ltp": 25890.0 + m, "exchange_ltt_ms": bar_t - 18_000, "availability_ts_ms": bar_t, "source_ts_proven": True},
                    availability_ts_ms=bar_t,
                )
                eng.ingest_event("BAR_CLOSE_CHECK", "CLOCK", {"availability_ts_ms": t0 + (m + 1) * 60_000}, availability_ts_ms=t0 + (m + 1) * 60_000)

            self.assertIsNone(eng.open_basis_ref)
            self.assertGreater(sum(eng.open_basis_fail_counts.values()), 0)
            ckpts = store.load_runtime_checkpoints("FORWARD_PAPER", self.cfg_hash)
            ckpt26 = next((c for c in ckpts if c.get("session_date") == "2026-11-26"), None)
            self.assertIsNotNone(ckpt26)
            fail_counts = ckpt26.get("open_basis_fail_summary") or {}
            self.assertTrue(any("SKEW" in k for k in fail_counts), f"Expected skew failure reason in {fail_counts}")
        finally:
            store.close()

    def test_03_priority_queue_overflow_isolation_and_4stage_latency_plus_dedicated_feed_audit(self) -> None:
        fc = V2BoundedFeedConsumer(self.cfg_hash, maxlen=64)
        fc.set_priority_keys(["NSE_INDEX|Nifty 50", "NSE_FO|58875", "NSE_FO|ACTIVE_LEG"])
        base_ms = 1_795_750_000_000

        # Push 99 normal watchlist ticks -> forces normal queue overflow (cap 64), PLUS priority ticks
        events = [
            ("OPTION", f"NSE_FO|OPT_{i}", {"instrument_key": f"NSE_FO|OPT_{i}", "bid": 10.0, "exchange_ltt_ms": base_ms + i * 100 - 250, "availability_ts_ms": base_ms + i * 100})
            for i in range(99)
        ] + [
            ("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "spot_ltp": 25800.0, "exchange_ltt_ms": base_ms + 9000 - 10, "availability_ts_ms": base_ms + 9000}),
            ("OPTION", "NSE_FO|ACTIVE_LEG", {"instrument_key": "NSE_FO|ACTIVE_LEG", "bid": 180.0, "exchange_ltt_ms": base_ms + 9050 - 15, "availability_ts_ms": base_ms + 9050}),
        ]
        fc.push_events(events, receipt_ts_ms=base_ms + 9100)
        fc.record_skew_sample("SPOT_FUT", 120)
        fc.record_skew_sample("CROSS_LEG", 45)
        fc.record_processing_duration_ms(3.5)

        h = fc.get_health_metrics()
        self.assertEqual(h["dropped_normal_overflow_events"], 35)
        self.assertEqual(h["dropped_priority_overflow_events"], 0)
        batch = fc.drain_batch(max_items=10)
        self.assertEqual([b["priority_tier"] for b in batch[:2]], ["PRIORITY", "PRIORITY"])

        h2 = fc.get_health_metrics()
        for k in ("source_age_p95_ms", "queue_lag_p95_ms", "processing_lag_p95_ms", "spot_fut_skew_p99_ms", "cross_leg_skew_p99_ms"):
            self.assertIn(k, h2)
        arch = evaluate_feed_architecture_options(h2, shared_subscribed_keys_count=510, account_tier="NORMAL")
        self.assertEqual(arch["active_mode"], "SHARED_PASSIVE_PRIORITY")
        self.assertFalse(arch["dedicated_ws_enabled"])
        self.assertEqual(arch["upstox_limits"]["normal_max_concurrent_ws_connections"], 2)

    def test_04_opportunities_acceptance_rejection_dedup_and_parity(self) -> None:
        from backend.tests.test_option_sniffer_v2_comprehensive import TestV2ComprehensiveSuite

        helper = TestV2ComprehensiveSuite()
        helper.setUp()
        try:
            stream = helper._build_realistic_timed_stream()
        finally:
            helper.tearDown()

        svc = get_option_sniffer_v2_service(self.v2_db)
        try:
            svc.start_parallel_collector(stream_manager=None, run_archive_warmup=False, spawn_worker=False)
            for ev in stream:
                svc.on_live_feed_events([ev])
            while svc.feed_consumer.get_health_metrics()["current_queue_depth"] > 0:
                svc.process_queued_events(max_items=60)
            opps = svc.get_opportunities(limit=500)
            self.assertGreater(opps["count"], 5)
            statuses = opps["summary_by_status"]
            self.assertIn("ACCEPTED", statuses)
            self.assertTrue("REJECTED" in statuses or "DISABLED" in statuses or "BLOCKED" in statuses)
            row0 = opps["rows"][0]
            self.assertIsInstance(row0["gate_inputs"], dict)
            self.assertIn("spot_close", row0["gate_inputs"])
            # API GET endpoint parity
            api_res = option_sniffer_v2_api.get_v2_opportunities()
            self.assertEqual(api_res["count"], opps["count"])
        finally:
            svc.stop_v2_only()
            svc.store.close()

    def test_05_live_db_isolation_and_cold_start_non_creation(self) -> None:
        for bad_path in (get_canonical_db_path(), _DEFAULT_V2_DB_PATH, ":memory:", os.path.join(self._td.name, "market_data.db")):
            with self.assertRaises(V2LiveDatabaseIsolationError):
                V2LiveLedgerStore(bad_path)

        cold_live = os.path.join(self._td.name, "never_created_live.db")
        set_v2_live_test_db_override(cold_live)
        reset_option_sniffer_v2_live_engine()
        st = option_sniffer_v2_live_api.get_v2_live_status(include_paper_baseline=False)
        pos = option_sniffer_v2_live_api.get_v2_live_positions()
        aud = option_sniffer_v2_live_api.get_v2_live_audit_log()
        self.assertFalse(os.path.exists(cold_live), "Cold-start live GET unexpectedly created SQLite DB!")
        self.assertFalse(st["armed"])
        self.assertEqual(pos["open_packages"], [])
        self.assertEqual(aud["entries"], [])

    def test_06_human_pin_arm_disarm_blocks_new_entries_preserves_exits_and_no_profit_gate(self) -> None:
        broker = MockV2BrokerClient(available_margin_inr=150000.0)
        eng = get_option_sniffer_v2_live_engine(db_path=self.live_db, broker_client=broker, initial_pin="4829")

        # Bad PIN rejected
        with self.assertRaises(PermissionError):
            eng.arm_live(pin="0000", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        # Missing checkbox rejected
        with self.assertRaises(ValueError):
            eng.arm_live(pin="4829", human_confirmed=False, selected_books=["V2_BOOK_B_PAID_FLOW"])
        # Unverified Book A rejected
        with self.assertRaises(ValueError):
            eng.arm_live(pin="4829", human_confirmed=True, selected_books=["V2_BOOK_A_PROPOSED_O3"])

        # Arms cleanly with zero paper forward history (no artificial session/profit block on human authority)
        st = eng.arm_live(
            pin="4829",
            human_confirmed=True,
            selected_books=["V2_BOOK_B_PAID_FLOW"],
            max_lots_per_book=1,
            max_daily_loss_inr=3000.0,
            max_open_packages=2,
        )
        self.assertTrue(st["armed"])

        # Enter one trade
        r1 = eng.submit_entry_signal(_sample_single_signal("sig_arm_01"))
        self.assertTrue(r1["ok"])
        pkg_id = r1["package"]["live_package_id"]

        # Disarm while trade is open -> new entry blocked, manual square-off STILL succeeds
        dis = eng.disarm_live(reason="HUMAN_PAUSE", actor="HUMAN_UI")
        self.assertFalse(dis["armed"])
        blocked = eng.submit_entry_signal(_sample_single_signal("sig_arm_02"))
        self.assertEqual(blocked["status"], "DISARMED_NEW_ENTRIES_BLOCKED")

        ex = eng.exit_live_package(live_package_id=pkg_id, reason="HUMAN_UI_SQUARE_OFF", exit_price=205.0, actor="HUMAN_UI")
        self.assertTrue(ex["ok"])
        self.assertEqual(ex["package"]["state"], "CLOSED")
        expected_acc = compute_package_accounting(
            [OptionLeg(option_type="PE", strike=25700.0, qty=65, entry_price=180.0, exit_price=205.0)]
        )
        self.assertAlmostEqual(ex["package"]["realized_net_inr"], expected_acc["net_pnl"], places=2)

    def test_07_duplicate_signal_idempotency_sends_single_broker_order(self) -> None:
        broker = MockV2BrokerClient()
        eng = V2LiveExecutionEngine(db_path=self.live_db, broker_client=broker, initial_pin="1357", log_pin_on_boot=False)
        eng.arm_live(pin="1357", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])

        sig = _sample_single_signal("sig_dup_99")
        r1 = eng.submit_entry_signal(sig)
        r2 = eng.submit_entry_signal(sig)
        self.assertTrue(r1["ok"])
        self.assertEqual(r2["status"], "DUPLICATE_SIGNAL_IDEMPOTENT")
        place_calls = [c for c in broker.call_log if c["method"] == "place_order"]
        self.assertEqual(len(place_calls), 1)

    def test_08_broker_timeout_unknown_ambiguous_applied_vs_not_received_and_reconciliation(self) -> None:
        # Case A: Broker received & filled order before timeout
        broker_a = MockV2BrokerClient()
        broker_a.inject_timeout_order_applied = True
        eng_a = V2LiveExecutionEngine(db_path=self.live_db, broker_client=broker_a, initial_pin="2468", log_pin_on_boot=False)
        eng_a.arm_live(pin="2468", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"], max_open_packages=2)

        res_a = eng_a.submit_entry_signal(_sample_single_signal("sig_to_applied"))
        self.assertEqual(res_a["status"], "UNKNOWN_AMBIGUOUS")
        self.assertFalse(eng_a.get_status(include_paper_baseline=False)["armed"])
        with self.assertRaises(RuntimeError):
            eng_a.arm_live(pin="2468", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])

        rec_a = eng_a.reconcile_with_broker()
        self.assertEqual(rec_a["unresolved_faults"]["total_blocking_faults"], 0)
        pkg_a = eng_a.store.get_package_by_signal("sig_to_applied")
        self.assertEqual(pkg_a["state"], "OPEN")
        self.assertEqual(pkg_a["legs"]["buy_leg"]["qty"], 65)

        # Case B: Broker never received order before timeout
        live_db_b = os.path.join(self._td.name, "v2_live_b.db")
        broker_b = MockV2BrokerClient()
        broker_b.inject_timeout_order_not_received = True
        eng_b = V2LiveExecutionEngine(db_path=live_db_b, broker_client=broker_b, initial_pin="2468", log_pin_on_boot=False)
        eng_b.arm_live(pin="2468", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        res_b = eng_b.submit_entry_signal(_sample_single_signal("sig_to_absent"))
        self.assertEqual(res_b["status"], "UNKNOWN_AMBIGUOUS")
        rec_b = eng_b.reconcile_with_broker()
        self.assertEqual(rec_b["unresolved_faults"]["total_blocking_faults"], 0)
        pkg_b = eng_b.store.get_package_by_signal("sig_to_absent")
        self.assertEqual(pkg_b["state"], "CLOSED")
        self.assertEqual(pkg_b["exit_reason"], "RECONCILED_ENTRY_NOT_RECEIVED")

    def test_09_spread_buy_wing_first_partial_fill_unwind_and_unwind_fail_halt(self) -> None:
        # Subcase A: Partial fill on buy wing caps short qty strictly at confirmed buy qty
        broker1 = MockV2BrokerClient()
        broker1.inject_partial_fill_ratio["NSE_FO|BUY_WING_1"] = 0.5  # 130 -> 65
        eng1 = V2LiveExecutionEngine(db_path=self.live_db, broker_client=broker1, initial_pin="5555", log_pin_on_boot=False)
        eng1.arm_live(pin="5555", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"], max_lots_per_book=2)
        sp1 = eng1.submit_entry_signal(_sample_spread_signal("sig_sp_partial", buy_qty=130))
        self.assertTrue(sp1["ok"])
        self.assertEqual(sp1["package"]["legs"]["buy_leg"]["qty"], 65)
        self.assertEqual(sp1["package"]["legs"]["short_leg"]["qty"], 65)
        place_calls1 = [c for c in broker1.call_log if c["method"] == "place_order"]
        self.assertEqual(place_calls1[0]["side"], "BUY")
        self.assertEqual(place_calls1[1]["side"], "SELL")
        self.assertEqual(place_calls1[1]["qty"], 65)

        # Subcase B: Short leg rejected -> immediate buy wing unwind succeeds cleanly
        db2 = os.path.join(self._td.name, "v2_live_sp2.db")
        broker2 = MockV2BrokerClient()
        broker2.inject_reject_instrument_keys.add("NSE_FO|SHORT_LEG_1")
        eng2 = V2LiveExecutionEngine(db_path=db2, broker_client=broker2, initial_pin="5555", log_pin_on_boot=False)
        eng2.arm_live(pin="5555", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        sp2 = eng2.submit_entry_signal(_sample_spread_signal("sig_sp_reject", buy_qty=65))
        self.assertFalse(sp2["ok"])
        self.assertEqual(sp2["package"]["state"], "CLOSED")
        self.assertEqual(sp2["package"]["exit_reason"], "SHORT_LEG_REJECTED_UNWOUND")

        # Subcase C: Short leg rejected + buy wing unwind fails -> MANUAL_INTERVENTION_REQUIRED + auto-disarm
        db3 = os.path.join(self._td.name, "v2_live_sp3.db")
        broker3 = MockV2BrokerClient()
        broker3.inject_reject_instrument_keys.add("NSE_FO|SHORT_LEG_1")
        broker3.inject_unwind_fail = True
        eng3 = V2LiveExecutionEngine(db_path=db3, broker_client=broker3, initial_pin="5555", log_pin_on_boot=False)
        eng3.arm_live(pin="5555", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        sp3 = eng3.submit_entry_signal(_sample_spread_signal("sig_sp_fail_unw", buy_qty=65))
        self.assertEqual(sp3["status"], "MANUAL_INTERVENTION_REQUIRED")
        self.assertFalse(eng3.get_status(include_paper_baseline=False)["armed"])
        self.assertGreater(eng3.get_status(include_paper_baseline=False)["unresolved_faults"]["total_blocking_faults"], 0)

    def test_10_restart_boots_disarmed_and_recovers_open_position_without_rearm(self) -> None:
        broker = MockV2BrokerClient()
        eng1 = V2LiveExecutionEngine(db_path=self.live_db, broker_client=broker, initial_pin="7777", log_pin_on_boot=False)
        eng1.arm_live(pin="7777", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        entry = eng1.submit_entry_signal(_sample_single_signal("sig_restart_01"))
        pkg_id = entry["package"]["live_package_id"]

        # Simulate process restart on same DB & broker state
        eng2 = V2LiveExecutionEngine(db_path=self.live_db, broker_client=broker, initial_pin="8888", log_pin_on_boot=False)
        st2 = eng2.get_status(include_paper_baseline=False)
        self.assertFalse(st2["armed"], "Restart MUST boot disarmed!")
        self.assertEqual(st2["open_package_count"], 1)
        rec = eng2.reconcile_with_broker()
        self.assertEqual(rec["package_reconciliations"][0]["status"], "OPEN")
        # Manual square-off works without re-arming
        ex = eng2.exit_live_package(live_package_id=pkg_id, reason="POST_RESTART_SQUARE_OFF", exit_price=190.0)
        self.assertTrue(ex["ok"])
        self.assertEqual(ex["package"]["state"], "CLOSED")

    def test_11_upstox_gtt_cancel_trigger_race_prevents_double_sell(self) -> None:
        broker = MockV2BrokerClient()
        eng = V2LiveExecutionEngine(db_path=self.live_db, broker_client=broker, initial_pin="9191", log_pin_on_boot=False)
        eng.arm_live(pin="9191", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])

        res = eng.submit_entry_signal(_sample_single_signal("sig_gtt_race"))
        pkg = res["package"]
        gtt_id = pkg["legs"]["buy_leg"]["gtt_id"]
        self.assertIsNotNone(gtt_id)

        # Inject Upstox race: GTT triggers & fills child SELL @ 151.0 just during cancel_gtt_order()
        broker.inject_gtt_triggers_during_cancel[gtt_id] = 151.0
        place_before = len([c for c in broker.call_log if c["method"] == "place_order" and c["side"] == "SELL"])
        ex = eng.exit_live_package(live_package_id=pkg["live_package_id"], reason="MANUAL_EXIT_RACE", exit_price=155.0)
        place_after = len([c for c in broker.call_log if c["method"] == "place_order" and c["side"] == "SELL"])

        self.assertTrue(ex["ok"])
        self.assertEqual(ex["status"], "CLOSED_BY_TRIGGERED_GTT_NO_DOUBLE_SELL")
        self.assertEqual(place_after, place_before, "MUST NOT send a second SELL order when GTT already flattened position!")
        self.assertAlmostEqual(ex["package"]["legs"]["buy_leg"]["exit_price"], 151.0, places=2)

    def test_12_paper_vs_live_isolation_and_market_protection_payload_rules(self) -> None:
        # Tag constraints + market_protection rules
        tag = sanitize_upstox_tag("osv2_sig_very_long_identifier_123456")
        self.assertLessEqual(len(tag), 20)
        self.assertRegex(tag, r"^[A-Za-z0-9-]+$")
        self.assertIsNone(normalize_market_protection("LIMIT", 5.0))
        self.assertIsNone(normalize_market_protection("SL", 5.0))
        self.assertEqual(normalize_market_protection("MARKET", -1), -1.0)
        self.assertEqual(normalize_market_protection("SL-M", 5.0), 5.0)
        with self.assertRaises(ValueError):
            normalize_market_protection("MARKET", 35.0)

        # Ensure live execution engine NEVER creates or mutates paper V2 DB
        self.assertFalse(os.path.exists(self.v2_db))
        broker = MockV2BrokerClient()
        eng = V2LiveExecutionEngine(db_path=self.live_db, broker_client=broker, initial_pin="6420", log_pin_on_boot=False)
        eng.arm_live(pin="6420", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        eng.submit_entry_signal(_sample_single_signal("sig_isolation_only"))
        self.assertFalse(os.path.exists(self.v2_db), "Live execution must never create or mutate V2 paper DB!")
        self.assertTrue(os.path.exists(self.live_db))

    def test_13_autonomous_paper_to_live_observer_entry_ratchet_and_exit_even_after_disarm(self) -> None:
        from backend.tests.test_option_sniffer_v2_comprehensive import _mk_obs_quote

        svc = get_option_sniffer_v2_service(self.v2_db)
        broker = MockV2BrokerClient(available_margin_inr=200000.0)
        live_eng = V2LiveExecutionEngine(
            db_path=self.live_db,
            broker_client=broker,
            initial_pin="3141",
            log_pin_on_boot=False,
            order_poll_interval_sec=0.0,
        )
        live_eng.attach_to_paper_service(svc)
        live_eng.arm_live(pin="3141", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])

        try:
            svc.start_parallel_collector(stream_manager=None, run_archive_warmup=False, spawn_worker=False)
            t0 = parse_ist_timestamp_ms("2026-11-20T09:15:00+05:30")
            assert t0 is not None
            exp = "2026-11-26"
            spot = 25700.0
            # Build 09:15..10:22 stream: entry around 10:15, profit spike at 10:17..10:18 -> ratchet,
            # disarm BEFORE 10:19 -> crash at 10:20 triggers autonomous ratchet exit even while disarmed!
            for m in range(67):
                bar_open_ms = t0 + m * 60_000
                ts_tick = bar_open_ms + 57_000
                bar_close_check = bar_open_ms + 60_000
                if m < 30:
                    spot = 25700.0 + (m % 4) * 2.0
                    fut = spot + 20.0
                elif m < 65:
                    spot += 1.8
                    fut = spot + 24.5
                elif m == 65:
                    spot -= 2.0
                    fut = spot + 24.5
                else:
                    spot += 1.0
                    fut = spot + 24.0

                svc.on_live_feed_events(
                    [
                        ("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": round(spot, 2), "availability_ts_ms": ts_tick, "exchange_ltt_ms": ts_tick, "source_ts_proven": True}),
                        ("INDEX", "INDIA_VIX", {"instrument_key": "NSE_INDEX|India VIX", "underlying": "INDIA_VIX", "spot_ltp": 13.5, "availability_ts_ms": ts_tick, "exchange_ltt_ms": ts_tick, "source_ts_proven": True}),
                        ("FUTURES", "NIFTY", {"instrument_key": "NSE_FO|NIFTYFUT_W1", "underlying": "NIFTY", "trading_symbol": "NIFTY FUT 26 NOV 26", "expiry": exp, "fut_ltp": round(fut, 2), "availability_ts_ms": ts_tick, "exchange_ltt_ms": ts_tick, "source_ts_proven": True}),
                    ]
                )
                atm_k = round(spot / 50.0) * 50.0
                for k in (atm_k - 50.0, atm_k, atm_k + 50.0, 25750.0):
                    px = max(25.0, (spot - k) + 120.0)
                    # Pump +45% after entry bar (~60..64) so ratchet triggers cleanly, then dump at m==65
                    if 61 <= m <= 64 and int(k) == 25750:
                        px = round(px * 1.45, 2)
                    elif m >= 65 and int(k) == 25750:
                        px = 70.0
                    svc.on_live_feed_events(
                        [
                            ("OPTION", f"NSE_FO|CE_{int(k)}", _mk_obs_quote(f"NSE_FO|CE_{int(k)}", round(px - 0.25, 2), round(px + 0.25, 2), ts_tick, strike=k, ot="CE", expiry=exp)),
                            ("OPTION", f"NSE_FO|PE_{int(k)}", _mk_obs_quote(f"NSE_FO|PE_{int(k)}", 27.75, 28.25, ts_tick, strike=k, ot="PE", expiry=exp)),
                        ]
                    )
                svc.on_live_feed_events([("BAR_CLOSE_CHECK", "CLOCK", {"availability_ts_ms": bar_close_check})])
                svc.process_queued_events(max_items=60)

                if m == 63:
                    # Verify autonomous live entry ALREADY opened & ratchet GTT modified before disarm!
                    st_mid = live_eng.get_status(include_paper_baseline=False)
                    self.assertEqual(st_mid["open_package_count"], 1, f"Expected autonomous live entry by m=63, status={st_mid}")
                    # Disarm BEFORE autonomous exit fires!
                    live_eng.disarm_live(reason="HUMAN_PAUSE_MID_TRADE")

            # After m=65 dump, autonomous exit MUST have closed live package EVEN WHILE DISARMED!
            modify_gtts = [c for c in broker.call_log if c["method"] == "modify_gtt_order"]
            self.assertGreaterEqual(len(modify_gtts), 1, "Expected autonomous ratchet GTT modification!")
            st_end = live_eng.get_status(include_paper_baseline=False)
            self.assertFalse(st_end["armed"])
            self.assertEqual(st_end["open_package_count"], 0)
            self.assertEqual(st_end["closed_package_count"], 1)
            pos = live_eng.get_positions()["closed_packages"][0]
            self.assertEqual(pos["state"], "CLOSED")
            self.assertTrue(str(pos["exit_reason"]).startswith("AUTO_"), f"Expected AUTO_* exit reason, got {pos['exit_reason']}")
            net_broker_pos = next((int(p["quantity"]) for p in broker.get_positions() if p["instrument_token"] == "NSE_FO|CE_25750"), 0)
            self.assertEqual(net_broker_pos, 0)
        finally:
            live_eng.detach_from_paper_service()
            svc.stop_v2_only()
            svc.store.close()

    def test_14_async_resting_order_polls_to_fill_and_unfilled_cancels_before_terminal_no_orphan(self) -> None:
        # Subcase A: Resting LIMIT order fills on 2nd poll -> settled FILLED (never false REJECTED!)
        broker_a = MockV2BrokerClient()
        broker_a.omit_inline_order_in_place_ack = True
        broker_a.inject_resting_polls_before_fill["NSE_FO|47836"] = 2
        eng_a = V2LiveExecutionEngine(
            db_path=self.live_db, broker_client=broker_a, initial_pin="1122",
            log_pin_on_boot=False, order_poll_attempts=5, order_poll_interval_sec=0.0,
        )
        eng_a.arm_live(pin="1122", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        res_a = eng_a.submit_entry_signal(_sample_single_signal("sig_async_fill"))
        self.assertTrue(res_a["ok"], f"Expected async poll fill to succeed, got {res_a}")
        self.assertEqual(res_a["package"]["state"], "OPEN")
        self.assertEqual(res_a["buy_intent"]["filled_qty"], 65)

        # Subcase B: Permanently unfilled resting LIMIT order MUST call cancel_order before marking CANCELLED (no orphan!)
        db_b = os.path.join(self._td.name, "live_unfilled.db")
        broker_b = MockV2BrokerClient()
        broker_b.inject_unfilled_resting_keys.add("NSE_FO|47836:LIMIT")
        eng_b = V2LiveExecutionEngine(
            db_path=db_b, broker_client=broker_b, initial_pin="1122",
            log_pin_on_boot=False, order_poll_attempts=3, order_poll_interval_sec=0.0,
        )
        eng_b.arm_live(pin="1122", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        res_b = eng_b.submit_entry_signal(_sample_single_signal("sig_async_unfilled"))
        self.assertFalse(res_b["ok"])
        self.assertEqual(res_b["intent"]["state"], "CANCELLED")
        self.assertEqual(res_b["package"]["state"], "CLOSED")
        self.assertEqual(res_b["package"]["exit_reason"], "ENTRY_BUY_UNFILLED_CANCELLED")
        cancel_calls = [c for c in broker_b.call_log if c["method"] == "cancel_order"]
        self.assertEqual(len(cancel_calls), 1, "MUST cancel unfilled resting order on exchange before closing!")
        self.assertEqual(broker_b.get_orders()[0]["status"], "cancelled")

    def test_15_upstox_v3_strict_schema_udapi1141_gtt_cancel_id_int_mp_and_market_zero_price(self) -> None:
        # 1. market_protection is strict Python int (-1 or 1..25)
        mp_auto = normalize_market_protection("MARKET", -1)
        mp_5 = normalize_market_protection("SL-M", 5.0)
        self.assertIs(type(mp_auto), int)
        self.assertIs(type(mp_5), int)
        self.assertEqual(mp_auto, -1)
        self.assertEqual(mp_5, 5)

        # 2. Mock enforces strict Upstox V3 schemas (UDAPI1141 SL strategy on SINGLE, float mp, non-zero MARKET price)
        mock_b = MockV2BrokerClient()
        with self.assertRaises(BrokerOrderRejectedError):
            mock_b.place_order({"instrument_token": "NSE_FO|1", "transaction_type": "BUY", "quantity": 65, "order_type": "MARKET", "price": 120.0})
        with self.assertRaises(BrokerOrderRejectedError):
            mock_b.place_order({"instrument_token": "NSE_FO|1", "transaction_type": "BUY", "quantity": 65, "order_type": "MARKET", "price": 0, "market_protection": 5.0})
        with self.assertRaises(BrokerOrderRejectedError):
            validate_upstox_v3_gtt_payload({
                "type": "SINGLE", "quantity": 65, "product": "D", "instrument_token": "NSE_FO|1",
                "transaction_type": "SELL", "rules": [{"strategy": "SL", "trigger_type": "BELOW", "trigger_price": 150.0, "limit_price": 148.0}],
            })

        # 3. Verify real UpstoxV2BrokerClient transmits exact JSON bodies (`gtt_order_id`, `price=0`, `int mp`)
        captured: list[dict] = []
        client = UpstoxV2BrokerClient()
        client._request = lambda method, url, json_body=None, params=None, **kw: (captured.append({"method": method, "url": url, "json_body": json_body, "params": params}) or {"status": "success", "data": {}})  # type: ignore

        client.place_order({"instrument_token": "NSE_FO|1", "transaction_type": "SELL", "order_type": "MARKET", "quantity": 65, "price": 205.0, "market_protection": 5.0})
        client.place_gtt_order({"type": "SINGLE", "quantity": 65, "product": "D", "instrument_token": "NSE_FO|1", "trading_symbol": "IGNORED", "transaction_type": "SELL", "rules": [{"strategy": "ENTRY", "trigger_type": "BELOW", "trigger_price": 150.02, "market_protection": 5.0}]})
        client.modify_gtt_order({"type": "SINGLE", "quantity": 65, "gtt_order_id": "GTT_99", "rules": [{"strategy": "ENTRY", "trigger_type": "BELOW", "trigger_price": 170.0}]})
        client.cancel_gtt_order("GTT_99")

        self.assertEqual(captured[0]["json_body"]["price"], 0)
        self.assertIs(type(captured[0]["json_body"]["market_protection"]), int)
        self.assertEqual(captured[1]["json_body"]["rules"][0]["strategy"], "ENTRY")
        self.assertNotIn("trading_symbol", captured[1]["json_body"])
        self.assertNotIn("limit_price", captured[1]["json_body"]["rules"][0])
        self.assertEqual(captured[2]["json_body"]["gtt_order_id"], "GTT_99")
        self.assertEqual(captured[3]["json_body"], {"gtt_order_id": "GTT_99"})

    def test_16_real_gtt_trigger_without_parent_id_never_double_sells_naked_short(self) -> None:
        broker = MockV2BrokerClient()
        eng = V2LiveExecutionEngine(
            db_path=self.live_db, broker_client=broker, initial_pin="2233",
            log_pin_on_boot=False, order_poll_interval_sec=0.0,
        )
        eng.arm_live(pin="2233", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])

        res = eng.submit_entry_signal(_sample_single_signal("sig_real_gtt_race"))
        pkg = res["package"]
        gtt_id = pkg["legs"]["buy_leg"]["gtt_id"]
        self.assertIsNotNone(gtt_id)

        # MockV2BrokerClient now emits real Upstox child order with NO parent_gtt_id and empty tag=""!
        broker.inject_gtt_triggers_during_cancel[gtt_id] = 151.0
        sells_before = len([c for c in broker.call_log if c["method"] == "place_order" and c["side"] == "SELL"])
        ex = eng.exit_live_package(live_package_id=pkg["live_package_id"], reason="MANUAL_EXIT_RACE", exit_price=155.0)
        sells_after = len([c for c in broker.call_log if c["method"] == "place_order" and c["side"] == "SELL"])

        self.assertTrue(ex["ok"])
        self.assertEqual(ex["status"], "CLOSED_BY_TRIGGERED_GTT_NO_DOUBLE_SELL")
        self.assertEqual(sells_after, sells_before, "MUST NOT send a SELL when broker position is already flat!")
        pos_qty = next((int(p["quantity"]) for p in broker.get_positions() if p["instrument_token"] == "NSE_FO|47836"), 0)
        self.assertEqual(pos_qty, 0, "Account net position must remain 0, never -65 naked short!")

    def test_17_failed_first_square_off_allows_retry_exit_and_stage2_market_escalation(self) -> None:
        # Subcase A: Rejected first Square Off does NOT permanently lock out retry!
        broker = MockV2BrokerClient()
        eng = V2LiveExecutionEngine(
            db_path=self.live_db, broker_client=broker, initial_pin="4455",
            log_pin_on_boot=False, order_poll_interval_sec=0.0,
        )
        eng.arm_live(pin="4455", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        entry = eng.submit_entry_signal(_sample_single_signal("sig_retry_exit"))
        pkg_id = entry["package"]["live_package_id"]

        broker.inject_reject_instrument_keys.add("NSE_FO|47836")
        ex1 = eng.exit_live_package(live_package_id=pkg_id, reason="SQUARE_OFF_1", exit_price=185.0)
        self.assertFalse(ex1["ok"])
        self.assertEqual(ex1["package"]["state"], "MANUAL_INTERVENTION_REQUIRED")

        # Recovery retry after rejection clears MUST succeed cleanly!
        broker.inject_reject_instrument_keys.clear()
        ex2 = eng.exit_live_package(live_package_id=pkg_id, reason="SQUARE_OFF_RETRY", exit_price=188.0)
        self.assertTrue(ex2["ok"], f"Retry square-off must not be locked out by deterministic idempotency: {ex2}")
        self.assertEqual(ex2["package"]["state"], "CLOSED")
        self.assertAlmostEqual(ex2["package"]["legs"]["buy_leg"]["exit_price"], 188.0, places=2)

        # Subcase B: Stage-1 LIMIT unfilled cancels cleanly and escalates to Stage-2 MARKET (price=0, mp=5)
        db2 = os.path.join(self._td.name, "live_s2.db")
        broker2 = MockV2BrokerClient()
        eng2 = V2LiveExecutionEngine(
            db_path=db2, broker_client=broker2, initial_pin="4455",
            log_pin_on_boot=False, order_poll_attempts=2, order_poll_interval_sec=0.0,
        )
        eng2.arm_live(pin="4455", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        entry2 = eng2.submit_entry_signal(_sample_single_signal("sig_stage2_esc"))
        broker2.inject_unfilled_resting_keys.add("NSE_FO|47836:LIMIT")
        broker2.market_ltp_by_key["NSE_FO|47836"] = 192.0
        ex_esc = eng2.exit_live_package(live_package_id=entry2["package"]["live_package_id"], reason="ESC_EXIT", exit_price=195.0)
        self.assertTrue(ex_esc["ok"])
        self.assertAlmostEqual(ex_esc["package"]["legs"]["buy_leg"]["exit_price"], 192.0, places=2)
        sell_calls = [c for c in broker2.call_log if c["method"] == "place_order" and c["side"] == "SELL"]
        self.assertEqual([c["order_type"] for c in sell_calls], ["LIMIT", "MARKET"])
        self.assertEqual(sell_calls[1]["price"], 0.0)
        self.assertIs(type(sell_calls[1]["market_protection"]), int)

    def test_18_ui_pin_exposure_rotation_and_protective_gtt_failure_auto_flatten(self) -> None:
        # 1. Dynamic PIN appears in get_status() and rotates across arm/disarm
        broker = MockV2BrokerClient()
        eng = V2LiveExecutionEngine(
            db_path=self.live_db, broker_client=broker, initial_pin=None,
            log_pin_on_boot=False, order_poll_interval_sec=0.0,
        )
        st0 = eng.get_status(include_paper_baseline=False)
        pin0 = st0["current_pin"]
        self.assertRegex(pin0, r"^\d{4}$")

        # Inject GTT failure BEFORE arming -> verify failing protective GTT auto-flattens entry!
        broker.inject_gtt_place_fail = True
        st1 = eng.arm_live(pin=pin0, human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        pin1 = st1["current_pin"]
        self.assertTrue(st1["armed"])
        res = eng.submit_entry_signal(_sample_single_signal("sig_gtt_fail_unwind"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "GTT_PROTECTION_FAILED_FLATTENED")
        self.assertEqual(res["package"]["state"], "CLOSED")
        net_pos = next((int(p["quantity"]) for p in broker.get_positions() if p["instrument_token"] == "NSE_FO|47836"), 0)
        self.assertEqual(net_pos, 0, "Failed protective GTT MUST flatten long leg so zero unprotected risk sits open!")

        st2 = eng.disarm_live()
        pin2 = st2["current_pin"]
        self.assertRegex(pin2, r"^\d{4}$")
        self.assertTrue(pin1 != pin0 or pin2 != pin1, "Dynamic PIN should rotate cleanly across arm/disarm.")

    def test_19_causal_multi_book_regime_arbitrator_and_dte_slot_bounds(self) -> None:
        exp_t1 = "2026-11-24"
        exp_w = "2026-11-26"
        exp_far = "2026-12-29"

        # 1. Book B enforces max_trading_dte=12 (rejects far expiry even if >=2 DTE!)
        sig_far = evaluate_book_b_entry_signal(
            cfg=self.cfg, session_date="2026-11-20", bar_ts="2026-11-20 10:30:00",
            ist_hhmm=1030, decision_ts_ms=1795150800000,
            spot_close=25850.0, spot_vwap=25810.0, orb30_h=25820.0, orb30_l=25780.0,
            adx14=28.0, er20=0.45, spot_quote=None, fut_quote=None, open_basis_ref=None,
            expiries=[exp_far], contracts_lookup={}, quote_lookup={},
        )
        self.assertIsNone(sig_far["expiry"], "Far >12 DTE expiry must be excluded by Book B max_trading_dte=12")

        # 2. Arbitrator routes DTE=2..4 ORB breakout -> Book B Priority & enforces anti-self-hedge opposing lock
        arb = evaluate_v2_regime_arbitration(
            session_date="2026-11-20", ist_hhmm=1030, expiries=[exp_w],
            spot_close=25850.0, spot_vwap=25810.0, orb30_h=25820.0, orb30_l=25780.0,
            orb30_ready=True, adx14=28.0, er20=0.45, vix_now=14.0,
            prior_vix_closes=[14.5] * 10,
            open_packages=[{"book_id": "V2_BOOK_B_PAID_FLOW", "legs_state": {"is_bull": True, "long_leg": {"option_type": "CE"}}}],
            max_open_packages_total=2, book_a_enabled=True, book_b_enabled=True,
        )
        self.assertEqual(arb["regime_code"], "TREND_EXPANSION_CONVEXITY_PRIORITY")
        self.assertTrue(arb["allowed_bull"])
        self.assertFalse(arb["allowed_bear"])

        ok_bull, _ = check_candidate_arbitration_gate(
            arb, book_id="V2_BOOK_A_PROPOSED_O3", is_bull=True, open_total=1,
            max_open_packages_total=2, book_a_enabled=True, book_b_enabled=True,
            candidate_dte=3, enforce_intraday_dte4_on_book_a=True,
        )
        self.assertTrue(ok_bull)
        ok_bear, rsn_bear = check_candidate_arbitration_gate(
            arb, book_id="V2_BOOK_A_PROPOSED_O3", is_bull=False, open_total=1,
            max_open_packages_total=2, book_a_enabled=True, book_b_enabled=True,
            candidate_dte=3, enforce_intraday_dte4_on_book_a=True,
        )
        self.assertFalse(ok_bear)
        self.assertEqual(rsn_bear, "BLOCKED_ANTI_SELF_HEDGE_OPPOSING_BULL_OPEN")

        # 3. Verify regenerated v2_research_report_v2_3.json fixed Nov 20 1-DTE hallucination & added ARB audit
        rep_path = os.path.join(_REPO_ROOT, "backend", "app", "analytics", "option_sniffer_v2", "v2_research_report_v2_3.json")
        with open(rep_path, "r", encoding="utf-8") as fh:
            rep = json.load(fh)
        nov20 = next(r for r in rep["observed_l5_10day_audit"]["session_opportunities"] if r["session_date"] == "2026-11-20")
        self.assertEqual(nov20["quote_status"], "UNVERIFIED_MISSING_2DTE_L5_CHAIN")
        self.assertIsNone(nov20["counterfactual_net_1x_inr"])
        self.assertIn("ARB3_DUAL_SLOT_NON_CONFLICTING_DTE4", rep["dataset_59_research"]["multi_book_regime_arbitration_59_sessions"])

    def test_20_reconcile_entry_timeout_places_protective_gtt_and_gtt_timeout_adopts_existing(self) -> None:
        # Subcase A: BUY_WING ENTRY timeout -> reconcile recovers OPEN AND attaches protective GTT + 1-tick ratchet works
        broker_a = MockV2BrokerClient()
        broker_a.inject_timeout_order_applied = True
        eng_a = V2LiveExecutionEngine(
            db_path=self.live_db, broker_client=broker_a, initial_pin="3344",
            log_pin_on_boot=False, order_poll_interval_sec=0.0,
        )
        eng_a.arm_live(pin="3344", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        sig_a = _sample_single_signal("sig_rec_gtt_01")
        sig_a["buy_leg"]["stop_trigger_price"] = 128.05
        res_a = eng_a.submit_entry_signal(sig_a)
        self.assertEqual(res_a["status"], "UNKNOWN_AMBIGUOUS")

        rec = eng_a.reconcile_with_broker()
        self.assertEqual(rec["unresolved_faults"]["total_blocking_faults"], 0)
        pkg = eng_a.store.get_package_by_signal("sig_rec_gtt_01")
        self.assertEqual(pkg["state"], "OPEN")
        self.assertIsNotNone(pkg["legs"]["buy_leg"].get("gtt_id"), "Reconciled long MUST receive protective V3 GTT!")

        # Exact +0.05 1-tick IEEE-754 ratchet (128.05 -> 128.10) MUST NOT be rejected by float comparison!
        r_tick = eng_a.sync_protective_ratchet_stop(live_package_id=pkg["live_package_id"], new_stop_trigger=128.10)
        self.assertTrue(r_tick["ok"], f"Exact +0.05 tick ratchet failed: {r_tick}")
        self.assertEqual(r_tick["status"], "GTT_RATCHET_UPDATED")
        self.assertAlmostEqual(r_tick["stop_trigger_price"], 128.10, places=2)

        # Subcase B: GTT timeout AFTER creation on Upstox adopts existing GTT (no orphan GTT / no blind unwind)
        db_b = os.path.join(self._td.name, "live_gtt_adopt.db")
        broker_b = MockV2BrokerClient()
        broker_b.inject_gtt_timeout_applied = True
        eng_b = V2LiveExecutionEngine(
            db_path=db_b, broker_client=broker_b, initial_pin="3344",
            log_pin_on_boot=False, order_poll_interval_sec=0.0,
        )
        eng_b.arm_live(pin="3344", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        res_b = eng_b.submit_entry_signal(_sample_single_signal("sig_gtt_adopt_01"))
        self.assertTrue(res_b["ok"], f"Expected GTT timeout recovery to adopt created GTT cleanly: {res_b}")
        self.assertEqual(res_b["package"]["state"], "OPEN")
        self.assertIsNotNone(res_b["package"]["legs"]["buy_leg"].get("gtt_id"))
        self.assertEqual(len(broker_b.get_gtt_orders()), 1)

    def test_21_partial_stage1_exit_stage2_ambiguous_persists_vwap_and_shared_v1_account_isolation(self) -> None:
        # Subcase A: 2-lot (130 qty) where Stage-1 fills 65 @ 200 and Stage-2 MARKET times out (not received)
        # -> Stage-1 progress persisted -> reconcile restores OPEN -> retry sells ONLY remaining 65 @ 210 -> VWAP=205!
        broker_a = MockV2BrokerClient()
        eng_a = V2LiveExecutionEngine(
            db_path=self.live_db, broker_client=broker_a, initial_pin="5566",
            log_pin_on_boot=False, order_poll_interval_sec=0.0,
        )
        eng_a.arm_live(pin="5566", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"], max_lots_per_book=2)
        sig2 = _sample_single_signal("sig_p_s1_s2_01")
        sig2["buy_leg"]["qty"] = 130
        entry_res = eng_a.submit_entry_signal(sig2)
        self.assertTrue(entry_res["ok"])
        pkg_id = entry_res["package"]["live_package_id"]

        # Force Stage-1 SELL LIMIT to partial-fill only 65 of 130, and Stage-2 MARKET to time out (not received)
        broker_a.inject_partial_fill_ratio["NSE_FO|47836"] = 0.5
        broker_a.inject_timeout_order_not_received = True
        # Note: inject_timeout_order_not_received triggers on the FIRST place_order, wait: target ONLY S2 tag!
        broker_a.inject_timeout_order_not_received = False
        orig_place = broker_a.place_order

        def _place_with_s2_timeout(payload, *, is_emergency=False):
            if str(payload.get("order_type")).upper() == "MARKET" and str(payload.get("transaction_type")).upper() == "SELL":
                broker_a.inject_timeout_order_not_received = True
            return orig_place(payload, is_emergency=is_emergency)

        broker_a.place_order = _place_with_s2_timeout  # type: ignore
        ex1 = eng_a.exit_live_package(live_package_id=pkg_id, reason="PARTIAL_S1_FAIL_S2", exit_price=200.0)
        self.assertFalse(ex1["ok"])
        self.assertEqual(ex1["status"], "UNKNOWN_AMBIGUOUS")
        pkg_mid = eng_a.store.get_package(pkg_id)
        self.assertEqual(pkg_mid["legs"]["buy_leg"]["exited_qty"], 65, "Stage-1 fill MUST be persisted before Stage-2 timeout!")

        # Reconcile Stage-2 timeout (expired not found) -> package returns OPEN with 65 exited / 65 remaining
        broker_a.place_order = orig_place  # type: ignore
        broker_a.inject_partial_fill_ratio.clear()
        rec_a = eng_a.reconcile_with_broker()
        self.assertEqual(rec_a["unresolved_faults"]["total_blocking_faults"], 0)
        pkg_rec = eng_a.store.get_package(pkg_id)
        self.assertEqual(pkg_rec["state"], "OPEN")
        self.assertEqual(pkg_rec["legs"]["buy_leg"]["exited_qty"], 65)

        ex2 = eng_a.exit_live_package(live_package_id=pkg_id, reason="RETRY_REM_65", exit_price=210.0)
        self.assertTrue(ex2["ok"])
        self.assertEqual(ex2["package"]["state"], "CLOSED")
        self.assertEqual(ex2["package"]["legs"]["buy_leg"]["exited_qty"], 130)
        self.assertAlmostEqual(ex2["package"]["legs"]["buy_leg"]["exit_price"], 205.0, places=2)
        net_rem = next((int(p["quantity"]) for p in broker_a.get_positions() if p["instrument_token"] == "NSE_FO|47836"), 0)
        self.assertEqual(net_rem, 0)

        # Subcase B: Shared V1 + V2 Upstox account (+65 V1 + +65 V2 = 130). V2's GTT fires untagged SELL 65!
        # Account still shows +65 (V1's position!). V2 exit/reconcile MUST NOT liquidate V1!
        db_b = os.path.join(self._td.name, "live_shared_acct.db")
        broker_b = MockV2BrokerClient()
        # Seed foreign V1 BUY (+65, tag INTENT_V1_01) AND an older pre-entry foreign SELL (INTENT_V1_OLD)
        broker_b.place_order({"instrument_token": "NSE_FO|47836", "trading_symbol": "NIFTY 25700 PE", "transaction_type": "BUY", "order_type": "LIMIT", "quantity": 65, "price": 170.0, "tag": "INTENT-V1-01"})
        eng_b = V2LiveExecutionEngine(
            db_path=db_b, broker_client=broker_b, initial_pin="5566",
            log_pin_on_boot=False, order_poll_interval_sec=0.0,
        )
        eng_b.arm_live(pin="5566", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        res_v2 = eng_b.submit_entry_signal(_sample_single_signal("sig_shared_v1_v2"))
        self.assertTrue(res_v2["ok"])
        v2_pkg_id = res_v2["package"]["live_package_id"]
        v2_gtt = res_v2["package"]["legs"]["buy_leg"]["gtt_id"]

        # Trigger V2 GTT on broker (account drops 130 -> 65, leaving V1's 65 open!)
        broker_b.inject_gtt_triggers_during_cancel[v2_gtt] = 152.0
        sells_before = len([c for c in broker_b.call_log if c["method"] == "place_order" and c["side"] == "SELL"])
        ex_b = eng_b.exit_live_package(live_package_id=v2_pkg_id, reason="SHARED_ACCT_EXIT", exit_price=160.0)
        sells_after = len([c for c in broker_b.call_log if c["method"] == "place_order" and c["side"] == "SELL"])

        self.assertTrue(ex_b["ok"])
        self.assertEqual(ex_b["status"], "CLOSED_BY_TRIGGERED_GTT_NO_DOUBLE_SELL")
        self.assertEqual(sells_after, sells_before, "MUST NOT place SELL when GTT already closed V2 on shared account!")
        v1_rem_qty = next((int(p["quantity"]) for p in broker_b.get_positions() if p["instrument_token"] == "NSE_FO|47836"), 0)
        self.assertEqual(v1_rem_qty, 65, "Foreign V1 +65 position MUST remain untouched!")

    def test_22_async_observer_nonblocking_worker_unrealized_daily_kill_and_excess_unwind_pnl(self) -> None:
        # 1. Non-blocking async observer dispatch: slow 120ms broker returns from hook in <25ms
        broker_slow = MockV2BrokerClient()
        broker_slow.inject_order_delay_sec = 0.12
        eng_async = V2LiveExecutionEngine(
            db_path=self.live_db, broker_client=broker_slow, initial_pin="7788",
            log_pin_on_boot=False, order_poll_interval_sec=0.0, async_observer_dispatch=True,
        )
        try:
            eng_async.arm_live(pin="7788", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
            t_start = time.monotonic()
            disp = eng_async.on_paper_lifecycle_event({
                "event_type": "PACKAGE_OPENED",
                "run_mode": "FORWARD_PAPER",
                "signal_id": "sig_async_fast_01",
                "session_date": "2026-11-27",
                "book_id": "V2_BOOK_B_PAID_FLOW",
                "structure": "SINGLE_LONG",
                "package_snapshot": {
                    "signal_id": "sig_async_fast_01",
                    "session_date": "2026-11-27",
                    "book_id": "V2_BOOK_B_PAID_FLOW",
                    "legs_state": {
                        "hard_stop": 150.0,
                        "long_leg": {"instrument_key": "NSE_FO|47836", "trading_symbol": "NIFTY 25700 PE", "option_type": "PE", "strike": 25700.0, "lot_size": 65, "entry_price": 180.0},
                    },
                },
                "quote_snapshot": {"raw_ask": 180.0},
            })
            elapsed_ms = (time.monotonic() - t_start) * 1000.0
            self.assertEqual(disp["status"], "QUEUED_ASYNC")
            self.assertLess(elapsed_ms, 50.0, "Observer dispatch must be non-blocking when async worker is active!")
            self.assertTrue(eng_async.flush_observer_events(timeout_sec=3.0))
            self.assertEqual(eng_async.get_status(include_paper_baseline=False)["open_package_count"], 1)

            # 2. Live unrealized MTM loss via broker position fallback triggers MAX_DAILY_LOSS_REACHED BEFORE slot limit!
            broker_slow.inject_order_delay_sec = 0.0
            broker_slow._positions["NSE_FO|47836"]["last_price"] = 100.0  # -80 * 65 = -5200 <= -2500 max_daily_loss!
            kill_res = eng_async.submit_entry_signal(_sample_single_signal("sig_should_hit_daily_kill"))
            self.assertEqual(kill_res["status"], "MAX_DAILY_LOSS_REACHED")
            self.assertFalse(eng_async.get_status(include_paper_baseline=False)["armed"], "Daily MTM kill breach MUST auto-disarm!")
        finally:
            eng_async.detach_from_paper_service()

        # 3. Collision-free package IDs for long shared prefixes + spread excess unwind P&L + wire strip check
        db_c = os.path.join(self._td.name, "live_collide.db")
        broker_c = MockV2BrokerClient()
        eng_c = V2LiveExecutionEngine(
            db_path=db_c, broker_client=broker_c, initial_pin="7788",
            log_pin_on_boot=False, order_poll_interval_sec=0.0,
        )
        eng_c.arm_live(pin="7788", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"], max_lots_per_book=2, max_open_packages=3)
        r_a = eng_c.submit_entry_signal(_sample_single_signal("osv2_sig_identical_prefix_0001_A"))
        r_b = eng_c.submit_entry_signal(_sample_single_signal("osv2_sig_identical_prefix_0001_B"))
        self.assertTrue(r_a["ok"] and r_b["ok"])
        self.assertNotEqual(r_a["package"]["live_package_id"], r_b["package"]["live_package_id"])
        self.assertEqual(eng_c.get_status(include_paper_baseline=False)["open_package_count"], 2)

        # UpstoxV2BrokerClient.place_order strips non-wire trading_symbol
        sent_bodies: list[dict] = []
        uc = UpstoxV2BrokerClient()
        uc._request = lambda method, url, json_body=None, **kw: (sent_bodies.append(json_body) or {"status": "success", "data": {}})  # type: ignore
        uc.place_order({"instrument_token": "NSE_FO|47836", "trading_symbol": "STRIP_ME", "transaction_type": "BUY", "order_type": "LIMIT", "quantity": 65, "price": 180.0})
        self.assertNotIn("trading_symbol", sent_bodies[0])

    def test_23_reconcile_spread_short_leg_timeout_applied_vs_absent_and_missing_gtt_recovery(self) -> None:
        # Subcase A: SHORT_LEG ENTRY times out AFTER filling on Upstox -> reconcile recovers OPEN -> exit covers SHORT first (0 naked short)
        broker_a = MockV2BrokerClient()
        orig_place_a = broker_a.place_order
        injected_a = {"done": False}

        def _place_short_to_applied(payload, *, is_emergency=False):
            if str(payload.get("instrument_token")) == "NSE_FO|SHORT_LEG_1" and not injected_a["done"]:
                injected_a["done"] = True
                broker_a.inject_timeout_order_applied = True
            return orig_place_a(payload, is_emergency=is_emergency)

        broker_a.place_order = _place_short_to_applied  # type: ignore
        eng_a = V2LiveExecutionEngine(
            db_path=self.live_db, broker_client=broker_a, initial_pin="2301",
            log_pin_on_boot=False, order_poll_interval_sec=0.0,
        )
        eng_a.arm_live(pin="2301", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"], max_open_packages=2)
        res_a = eng_a.submit_entry_signal(_sample_spread_signal("sig_sp_short_to_app", buy_qty=65))
        self.assertFalse(res_a["ok"])
        self.assertEqual(res_a["status"], "SHORT_TIMEOUT_AMBIGUOUS")
        self.assertEqual(res_a["package"]["state"], "RECONCILE_MISMATCH")
        self.assertFalse(eng_a.get_status(include_paper_baseline=False)["armed"])

        rec_a = eng_a.reconcile_with_broker()
        self.assertEqual(rec_a["unresolved_faults"]["total_blocking_faults"], 0)
        pkg_a = eng_a.store.get_package_by_signal("sig_sp_short_to_app")
        self.assertEqual(pkg_a["state"], "OPEN")
        self.assertEqual(pkg_a["legs"]["short_leg"]["qty"], 65)
        self.assertEqual(pkg_a["legs"]["buy_leg"]["qty"], 65)
        self.assertIsNone(pkg_a.get("unresolved_fault"))

        calls_before_exit = len(broker_a.call_log)
        ex_a = eng_a.exit_live_package(
            live_package_id=pkg_a["live_package_id"], reason="CLEAN_SPREAD_EXIT", exit_price=48.0, short_exit_price=160.0
        )
        self.assertTrue(ex_a["ok"])
        self.assertEqual(ex_a["package"]["state"], "CLOSED")
        exit_place_calls = [c for c in broker_a.call_log[calls_before_exit:] if c["method"] == "place_order"]
        self.assertEqual(exit_place_calls[0]["instrument_token"], "NSE_FO|SHORT_LEG_1")
        self.assertEqual(exit_place_calls[0]["side"], "BUY")
        self.assertEqual(exit_place_calls[1]["instrument_token"], "NSE_FO|BUY_WING_1")
        self.assertEqual(exit_place_calls[1]["side"], "SELL")
        for bp in broker_a.get_positions():
            self.assertEqual(int(bp["quantity"]), 0, f"Expected zero leftover position for {bp['instrument_token']}")

        # Subcase B: SHORT_LEG ENTRY times out WITHOUT reaching Upstox -> reconcile unwinds BUY_WING safely
        db_b = os.path.join(self._td.name, "live_sp_short_absent.db")
        broker_b = MockV2BrokerClient()
        orig_place_b = broker_b.place_order
        injected_b = {"done": False}

        def _place_short_to_absent(payload, *, is_emergency=False):
            if str(payload.get("instrument_token")) == "NSE_FO|SHORT_LEG_1" and not injected_b["done"]:
                injected_b["done"] = True
                broker_b.inject_timeout_order_not_received = True
            return orig_place_b(payload, is_emergency=is_emergency)

        broker_b.place_order = _place_short_to_absent  # type: ignore
        eng_b = V2LiveExecutionEngine(
            db_path=db_b, broker_client=broker_b, initial_pin="2302",
            log_pin_on_boot=False, order_poll_interval_sec=0.0,
        )
        eng_b.arm_live(pin="2302", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        res_b = eng_b.submit_entry_signal(_sample_spread_signal("sig_sp_short_to_abs", buy_qty=65))
        self.assertEqual(res_b["status"], "SHORT_TIMEOUT_AMBIGUOUS")

        rec_b = eng_b.reconcile_with_broker()
        self.assertEqual(rec_b["unresolved_faults"]["total_blocking_faults"], 0)
        pkg_b = eng_b.store.get_package_by_signal("sig_sp_short_to_abs")
        self.assertEqual(pkg_b["state"], "CLOSED")
        self.assertEqual(pkg_b["exit_reason"], "RECONCILED_SHORT_NOT_RECEIVED_UNWOUND")
        for bp in broker_b.get_positions():
            self.assertEqual(int(bp["quantity"]), 0)

        # Subcase C: SINGLE_LONG external GTT vanishes while +65 stays open -> reconcile re-attaches fresh V3 GTT
        db_c = os.path.join(self._td.name, "live_missing_gtt.db")
        broker_c = MockV2BrokerClient()
        eng_c = V2LiveExecutionEngine(
            db_path=db_c, broker_client=broker_c, initial_pin="2303",
            log_pin_on_boot=False, order_poll_interval_sec=0.0,
        )
        eng_c.arm_live(pin="2303", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
        res_c = eng_c.submit_entry_signal(_sample_single_signal("sig_gtt_vanish"))
        self.assertTrue(res_c["ok"])
        orig_gid = res_c["package"]["legs"]["buy_leg"]["gtt_id"]
        broker_c._gtts.clear()

        rec_c = eng_c.reconcile_with_broker()
        self.assertEqual(rec_c["unresolved_faults"]["total_blocking_faults"], 0)
        pkg_c = eng_c.store.get_package_by_signal("sig_gtt_vanish")
        self.assertEqual(pkg_c["state"], "OPEN")
        new_gid = pkg_c["legs"]["buy_leg"]["gtt_id"]
        self.assertIsNotNone(new_gid)
        self.assertNotEqual(new_gid, orig_gid)
        self.assertIn(new_gid, {g["gtt_order_id"] for g in broker_c.get_gtt_orders()})

    def test_24_spread_excess_buy_unwind_survives_reconcile_and_burst_async_events_never_block_lock(self) -> None:
        # Subcase A: 2-lot spread (130 buy, 65 partial short) -> 65 excess unwound -> reconcile keeps OPEN -> exit closes remaining 65/65 cleanly
        broker_sp = MockV2BrokerClient()
        broker_sp.inject_partial_fill_ratio["NSE_FO|SHORT_LEG_1"] = 0.5
        eng_sp = V2LiveExecutionEngine(
            db_path=self.live_db, broker_client=broker_sp, initial_pin="2401",
            log_pin_on_boot=False, order_poll_interval_sec=0.0,
        )
        eng_sp.arm_live(
            pin="2401", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"],
            max_lots_per_book=2, max_open_packages=2,
        )
        res_sp = eng_sp.submit_entry_signal(_sample_spread_signal("sig_sp_excess_rec", buy_qty=130))
        self.assertTrue(res_sp["ok"])
        self.assertEqual(res_sp["package"]["legs"]["buy_leg"]["qty"], 65)
        self.assertEqual(res_sp["package"]["legs"]["short_leg"]["qty"], 65)
        self.assertEqual(res_sp["package"]["legs"]["excess_unwound_buy_leg"]["qty"], 65)

        broker_sp.inject_partial_fill_ratio.clear()
        rec_sp = eng_sp.reconcile_with_broker()
        self.assertEqual(rec_sp["unresolved_faults"]["total_blocking_faults"], 0)
        pkg_mid = eng_sp.store.get_package(res_sp["package"]["live_package_id"])
        self.assertEqual(pkg_mid["state"], "OPEN")
        self.assertIsNone(pkg_mid["legs"]["buy_leg"].get("exit_price"))
        self.assertEqual(int(pkg_mid["legs"]["buy_leg"].get("exited_qty") or 0), 0)

        ex_sp = eng_sp.exit_live_package(
            live_package_id=pkg_mid["live_package_id"], reason="SPREAD_EXIT_AFTER_RECONCILE", exit_price=48.0, short_exit_price=160.0
        )
        self.assertTrue(ex_sp["ok"])
        self.assertEqual(ex_sp["package"]["state"], "CLOSED")
        for bp in broker_sp.get_positions():
            self.assertEqual(int(bp["quantity"]), 0, f"Orphan position left open on {bp['instrument_token']}: {bp}")

        # Subcase B: Burst consecutive on_paper_lifecycle_event calls NEVER block on self._lock during slow HTTP I/O
        db_burst = os.path.join(self._td.name, "live_burst_async.db")
        broker_burst = MockV2BrokerClient()
        broker_burst.inject_order_delay_sec = 0.18
        eng_burst = V2LiveExecutionEngine(
            db_path=db_burst, broker_client=broker_burst, initial_pin="2402",
            log_pin_on_boot=False, order_poll_interval_sec=0.0, async_observer_dispatch=True,
        )
        try:
            eng_burst.arm_live(pin="2402", human_confirmed=True, selected_books=["V2_BOOK_B_PAID_FLOW"])
            d1 = eng_burst.on_paper_lifecycle_event({
                "event_type": "PACKAGE_OPENED",
                "run_mode": "FORWARD_PAPER",
                "signal_id": "sig_burst_01",
                "session_date": "2026-11-27",
                "book_id": "V2_BOOK_B_PAID_FLOW",
                "structure": "SINGLE_LONG",
                "package_snapshot": {
                    "signal_id": "sig_burst_01",
                    "session_date": "2026-11-27",
                    "book_id": "V2_BOOK_B_PAID_FLOW",
                    "legs_state": {
                        "hard_stop": 150.0,
                        "long_leg": {"instrument_key": "NSE_FO|47836", "trading_symbol": "NIFTY 25700 PE", "option_type": "PE", "strike": 25700.0, "lot_size": 65, "entry_price": 180.0},
                    },
                },
                "quote_snapshot": {"raw_ask": 180.0},
            })
            self.assertEqual(d1["status"], "QUEUED_ASYNC")
            # Sleep briefly so ObserverWorker holds self._lock inside 180ms broker.place_order()
            time.sleep(0.03)
            t2_start = time.monotonic()
            d2 = eng_burst.on_paper_lifecycle_event({
                "event_type": "RATCHET_UPDATED",
                "run_mode": "FORWARD_PAPER",
                "signal_id": "sig_burst_01",
                "session_date": "2026-11-27",
                "new_ratchet_stop": 160.0,
            })
            elapsed_ms_2 = (time.monotonic() - t2_start) * 1000.0
            self.assertEqual(d2["status"], "QUEUED_ASYNC")
            self.assertLess(elapsed_ms_2, 40.0, f"Event #2 blocked on worker lock ({elapsed_ms_2:.1f}ms)!")
            self.assertTrue(eng_burst.flush_observer_events(timeout_sec=3.0))
            pkg_b = eng_burst.store.get_package_by_signal("sig_burst_01")
            self.assertEqual(pkg_b["state"], "OPEN")
            self.assertAlmostEqual(pkg_b["legs"]["buy_leg"]["stop_trigger_price"], 160.0, places=2)
        finally:
            eng_burst.detach_from_paper_service()


if __name__ == "__main__":
    unittest.main()
