import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.analytics.common.options_accounting import OptionLeg, compute_package_accounting
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
from app.analytics.option_sniffer_v2.v2_service import (
    OptionSnifferV2Service,
    get_option_sniffer_v2_service,
    reset_option_sniffer_v2_service,
)
from app.analytics.option_sniffer_v2.v2_unified_engine import V2UnifiedEventEngine
from app.analytics.option_sniffer_v2_live import (
    MockV2BrokerClient,
    V2LiveDatabaseIsolationError,
    V2LiveExecutionEngine,
    V2LiveLedgerStore,
    get_option_sniffer_v2_live_engine,
    reset_option_sniffer_v2_live_engine,
    sanitize_upstox_tag,
    set_v2_live_test_db_override,
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


if __name__ == "__main__":
    unittest.main()
