"""
Comprehensive End-to-End Test Suite for V2 Option Sniffer (`option_sniffer_v2`).
Covers Fixes #3-#9 alongside original institutional risk, accounting, FSP,
restart recovery, and isolation scenarios inside isolated temporary SQLite DBs.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.analytics.common.options_accounting import OptionLeg, compute_package_accounting
from app.analytics.option_sniffer_v2.v2_book_a_engine import (
    evaluate_book_a_entry_signal,
    manage_open_book_a_on_bar,
)
from app.analytics.option_sniffer_v2.v2_book_b_engine import (
    evaluate_book_b_entry_signal,
    manage_open_book_b_on_bar,
)
from app.analytics.option_sniffer_v2.v2_collateral_report import compute_optional_collateral_report
from app.analytics.option_sniffer_v2.v2_config import (
    V2PaperOnlyInvariantError,
    is_nse_trading_session,
    load_v2_preregistration,
    parse_ist_timestamp_ms,
    resolve_contract_lot_size,
    trading_sessions_between,
)
from app.analytics.option_sniffer_v2.v2_counterfactual_v1 import (
    run_v1_counterfactual_audit,
    simulate_cf1_spread_trajectory,
    simulate_cf2_trajectory,
)
from app.analytics.option_sniffer_v2.v2_data_contract import (
    check_cross_leg_skew,
    check_spot_futures_sync,
    resolve_strict_quote_age_ms,
    sweep_observed_l5_book,
)
from app.analytics.option_sniffer_v2.v2_feed_consumer import (
    V2BoundedFeedConsumer,
    V2SubscriptionRegistry,
)
from app.analytics.option_sniffer_v2.v2_ledger_db import (
    V2LedgerStore,
    open_v1_readonly_db,
    set_v2_test_db_override,
)
from app.analytics.option_sniffer_v2.v2_o3_reproducer import load_phase4_o3_audit_report
from app.analytics.option_sniffer_v2.v2_paper_simulator import (
    V2PaperSimulator,
    compute_chronological_drawdown,
)
from app.analytics.option_sniffer_v2.v2_replay_attribution import (
    run_event_stream_replay,
    run_v2_replay_and_attribution,
)
from app.analytics.option_sniffer_v2.v2_service import (
    OptionSnifferV2Service,
    reset_option_sniffer_v2_service,
)
from app.analytics.option_sniffer_v2.v2_unified_engine import V2UnifiedEventEngine


def _mk_obs_quote(
    ikey: str,
    bid: float,
    ask: float,
    ts_ms: int,
    qty_per_lvl: int = 195,
    levels_count: int = 5,
    prov: str = "OBSERVED_V3_L5",
    strike: float = 25800.0,
    ot: str = "PE",
    expiry: str = "2026-11-23",
    ltp: float | None = None,
    underlying: str = "NIFTY",
) -> dict:
    depth = []
    for i in range(levels_count):
        bp = round(max(0.05, bid - i * 0.10), 2)
        ap = round(ask + i * 0.10, 2)
        depth.append({"bid_p": bp, "bid_q": qty_per_lvl, "ask_p": ap, "ask_q": qty_per_lvl})
    is_obs = prov == "OBSERVED_V3_L5"
    return {
        "instrument_key": ikey,
        "trading_symbol": f"{underlying} {int(strike)} {ot} {expiry}",
        "underlying": underlying,
        "expiry": expiry,
        "strike": float(strike),
        "option_type": ot,
        "lot_size": 65,
        "raw_bid": bid,
        "raw_ask": ask,
        "bid": bid,
        "ask": ask,
        "l1_bid_qty_observed": qty_per_lvl,
        "l1_ask_qty_observed": qty_per_lvl,
        "ltp": ltp if ltp is not None else round(0.5 * (bid + ask), 2),
        "depth_levels": depth,
        "is_observed_l1_book": is_obs,
        "l1_observed_on_tick": is_obs,
        "depth_observed_on_tick": is_obs,
        "depth_rungs_carried": not is_obs,
        "book_provenance": prov,
        "l1_timestamp_ms": ts_ms,
        "l1_book_timestamp_ms": ts_ms,
        "depth_timestamp_ms": ts_ms if is_obs else None,
        "local_receipt_ts_ms": ts_ms,
        "availability_ts_ms": ts_ms,
        "exchange_ltt_ms": ts_ms,
        "book_obi_l5": 0.25,
    }


def _mk_contract(ikey: str, strike: float, ot: str, expiry: str, lot_size: int = 65, underlying: str = "NIFTY") -> dict:
    return {
        "instrument_key": ikey,
        "trading_symbol": f"{underlying} {int(strike)} {ot} {expiry}",
        "underlying": underlying,
        "expiry": expiry,
        "strike": float(strike),
        "option_type": ot,
        "lot_size": int(lot_size),
    }


class TestV2ComprehensiveSuite(unittest.TestCase):
    def setUp(self) -> None:
        reset_option_sniffer_v2_service()
        self._td = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._td.name, "v2_comp.db")
        set_v2_test_db_override(self.db_path)
        self.cfg, self.cfg_hash = load_v2_preregistration()
        self.store = V2LedgerStore(self.db_path, auto_create=True)
        self.sim = V2PaperSimulator(self.cfg, self.cfg_hash, self.store, run_mode="FORWARD_PAPER")

    def tearDown(self) -> None:
        self.store.close()
        reset_option_sniffer_v2_service()
        set_v2_test_db_override(None)
        self._td.cleanup()

    def test_01_normal_book_a_opt_in_lifecycle_and_overnight_pnl_split(self) -> None:
        ts0 = 1_795_000_000_000
        exp = "2026-11-23"
        sc = _mk_contract("NSE_FO|PE25750", 25750.0, "PE", exp)
        wc = _mk_contract("NSE_FO|PE25650", 25650.0, "PE", exp)
        sq = _mk_obs_quote("NSE_FO|PE25750", 92.0, 92.5, ts0, strike=25750.0, ot="PE", expiry=exp)
        wq = _mk_obs_quote("NSE_FO|PE25650", 54.0, 54.4, ts0, strike=25650.0, ot="PE", expiry=exp)

        blocked = evaluate_book_a_entry_signal(
            self.cfg, "2026-11-20", "2026-11-20 14:00:00", 1400, ts0,
            25810.0, 25790.0, 13.5, [13.2] * 10, [25700.0] * 20, [exp],
            {(exp, 25750.0, "PE"): sc, (exp, 25650.0, "PE"): wc},
            {(exp, 25750.0, "PE"): sq, (exp, 25650.0, "PE"): wq},
            allow_unverified_in_replay=False,
        )
        self.assertFalse(blocked["passed"])
        self.assertEqual(blocked["reject_reason"], "BLOCKED_UNVERIFIED_O3_SPEC_CONFLICT")

        sig = evaluate_book_a_entry_signal(
            self.cfg, "2026-11-20", "2026-11-20 14:00:00", 1400, ts0,
            25810.0, 25790.0, 13.5, [13.2] * 10, [25700.0] * 20, [exp],
            {(exp, 25750.0, "PE"): sc, (exp, 25650.0, "PE"): wc},
            {(exp, 25750.0, "PE"): sq, (exp, 25650.0, "PE"): wq},
            allow_unverified_in_replay=True,
        )
        self.assertTrue(sig["passed"])
        l1 = self.sim.open_book_a_leg1_hedge(
            "sig_a1", sig["candidate_variant"], "2026-11-20", "2026-11-20 14:00:00", ts0, wc, sc, wq, 25810.0, 13.5, True
        )
        self.assertTrue(l1["ok"])
        l2 = self.sim.complete_or_unwind_pending_leg2(l1["package_id"], sq, wq, ts0 + 250, "2026-11-20", "2026-11-20 14:00:00")
        self.assertTrue(l2["ok"])
        self.assertGreater(self.sim.reserved_capital_inr, 0.0)

        ts_eod = ts0 + 75 * 60_000
        self.sim.mark_open_packages(
            {
                "NSE_FO|PE25750": _mk_obs_quote("NSE_FO|PE25750", 90.0, 90.4, ts_eod, strike=25750.0, ot="PE", expiry=exp),
                "NSE_FO|PE25650": _mk_obs_quote("NSE_FO|PE25650", 53.5, 53.8, ts_eod, strike=25650.0, ot="PE", expiry=exp),
            },
            "2026-11-20", "2026-11-20 15:25:00", ts_eod, is_eod_bar=True,
        )
        ts_open_t0 = ts_eod + 3600_000
        self.sim.mark_open_packages(
            {
                "NSE_FO|PE25750": _mk_obs_quote("NSE_FO|PE25750", 80.0, 80.3, ts_open_t0, strike=25750.0, ot="PE", expiry=exp),
                "NSE_FO|PE25650": _mk_obs_quote("NSE_FO|PE25650", 48.0, 48.3, ts_open_t0, strike=25650.0, ot="PE", expiry=exp),
            },
            exp, f"{exp} 09:15:00", ts_open_t0, is_opening_bar=True,
        )
        ts_ex = ts_open_t0 + 600_000
        pkg = self.sim.open_packages[l1["package_id"]]
        res_ex = manage_open_book_a_on_bar(
            self.sim, pkg, exp, f"{exp} 09:25:00", 925, ts_ex, 25890.0,
            {
                "NSE_FO|PE25750": _mk_obs_quote("NSE_FO|PE25750", 24.0, 24.3, ts_ex, strike=25750.0, ot="PE", expiry=exp),
                "NSE_FO|PE25650": _mk_obs_quote("NSE_FO|PE25650", 12.0, 12.3, ts_ex, strike=25650.0, ot="PE", expiry=exp),
            },
        )
        self.assertTrue(res_ex["ok"])
        self.assertEqual(res_ex["exit_reason"], "CREDIT_DECAY_TARGET_50PCT")
        loaded = self.store.load_packages("FORWARD_PAPER", self.cfg_hash)[0]
        self.assertAlmostEqual(
            loaded["entry_day_mtm_pnl_inr"] + loaded["overnight_gap_pnl_inr"] + loaded["expiry_intraday_pnl_inr"],
            loaded["net_pnl_inr"],
            places=2,
        )
        self.assertEqual(self.sim.reserved_capital_inr, 0.0)

    def test_02_holiday_adjusted_expiry_and_historical_lot_size(self) -> None:
        self.assertFalse(is_nse_trading_session("2026-11-24"))
        self.assertEqual(trading_sessions_between("2026-11-20", "2026-11-23"), 1)
        self.assertEqual(trading_sessions_between("2026-11-19", "2026-11-23"), 2)
        self.assertEqual(resolve_contract_lot_size("2025-12-23"), 75)
        self.assertEqual(resolve_contract_lot_size("2026-11-23"), 65)

    def test_03_book_b_cash_cap_1otm_fallback_only_and_futures_roll_fail_closed(self) -> None:
        ts = 1_795_100_000_000
        exp = "2026-11-26"
        spot_q = {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25810.0, "exchange_ltt_ms": ts, "local_receipt_ts_ms": ts, "source_ts_proven": True}
        fut_q_open = {"instrument_key": "NSE_FO|NIFTY_FUT_NOV", "underlying": "NIFTY", "trading_symbol": "NIFTY FUT 26 NOV 26", "expiry": "2026-11-26", "fut_ltp": 25850.0, "exchange_ltt_ms": ts, "local_receipt_ts_ms": ts, "source_ts_proven": True}
        open_ref = check_spot_futures_sync(spot_q, fut_q_open, now_ms=ts)
        self.assertTrue(open_ref["ok"])

        # Mid-session roll to DEC future must fail closed
        fut_q_rolled = {"instrument_key": "NSE_FO|NIFTY_FUT_DEC", "underlying": "NIFTY", "trading_symbol": "NIFTY FUT 31 DEC 26", "expiry": "2026-12-31", "fut_ltp": 25920.0, "exchange_ltt_ms": ts, "local_receipt_ts_ms": ts, "source_ts_proven": True}
        ce_atm = _mk_contract("NSE_FO|CE25800", 25800.0, "CE", exp)
        ce_otm = _mk_contract("NSE_FO|CE25850", 25850.0, "CE", exp)
        q_atm_exp = _mk_obs_quote("NSE_FO|CE25800", 300.0, 301.0, ts, strike=25800.0, ot="CE", expiry=exp)
        q_otm_ok = _mk_obs_quote("NSE_FO|CE25850", 260.0, 261.0, ts, strike=25850.0, ot="CE", expiry=exp)

        rolled_res = evaluate_book_b_entry_signal(
            self.cfg, "2026-11-19", "2026-11-19 10:45:00", 1045, ts, 25810.0, 25790.0, 25800.0, 25740.0, 26.0, 0.36,
            spot_q, fut_q_rolled, open_ref, [exp],
            {(exp, 25800.0, "CE"): ce_atm, (exp, 25850.0, "CE"): ce_otm},
            {(exp, 25800.0, "CE"): q_atm_exp, (exp, 25850.0, "CE"): q_otm_ok},
        )
        self.assertFalse(rolled_res["passed"])
        self.assertIn("FUTURES_CONTRACT_ROLLED_MID_SESSION", rolled_res["reject_reason"])

        no_open_res = evaluate_book_b_entry_signal(
            self.cfg, "2026-11-19", "2026-11-19 10:45:00", 1045, ts, 25810.0, 25790.0, 25800.0, 25740.0, 26.0, 0.36,
            spot_q, fut_q_open, None, [exp],
            {(exp, 25800.0, "CE"): ce_atm, (exp, 25850.0, "CE"): ce_otm},
            {(exp, 25800.0, "CE"): q_atm_exp, (exp, 25850.0, "CE"): q_otm_ok},
        )
        self.assertFalse(no_open_res["passed"])
        self.assertIn("MISSING_OPEN_BASIS_REF", no_open_res["reject_reason"])

        fut_q_up = {"instrument_key": "NSE_FO|NIFTY_FUT_NOV", "underlying": "NIFTY", "trading_symbol": "NIFTY FUT 26 NOV 26", "expiry": "2026-11-26", "fut_ltp": 25856.0, "exchange_ltt_ms": ts, "local_receipt_ts_ms": ts, "source_ts_proven": True}
        ok_otm = evaluate_book_b_entry_signal(
            self.cfg, "2026-11-19", "2026-11-19 10:45:00", 1045, ts, 25810.0, 25790.0, 25800.0, 25740.0, 26.0, 0.36,
            spot_q, fut_q_up, open_ref, [exp],
            {(exp, 25800.0, "CE"): ce_atm, (exp, 25850.0, "CE"): ce_otm},
            {(exp, 25800.0, "CE"): q_atm_exp, (exp, 25850.0, "CE"): q_otm_ok},
        )
        self.assertTrue(ok_otm["passed"])
        self.assertTrue(ok_otm["used_single_otm_fallback"])
        self.assertEqual(ok_otm["contract"]["strike"], 25850.0)

    def test_04_missing_vix_stale_unknown_age_carried_and_skew_fail_closed(self) -> None:
        ts = 1_795_100_000_000
        exp = "2026-11-23"
        sc = _mk_contract("NSE_FO|PE25750", 25750.0, "PE", exp)
        wc = _mk_contract("NSE_FO|PE25650", 25650.0, "PE", exp)
        sq = _mk_obs_quote("NSE_FO|PE25750", 92.0, 92.5, ts, strike=25750.0, ot="PE", expiry=exp)
        wq = _mk_obs_quote("NSE_FO|PE25650", 54.0, 54.4, ts, strike=25650.0, ot="PE", expiry=exp)

        vix_missing = evaluate_book_a_entry_signal(
            self.cfg, "2026-11-20", "2026-11-20 14:00:00", 1400, ts, 25810.0, 25790.0, 13.5, [13.2] * 5, [25700.0] * 20, [exp],
            {(exp, 25750.0, "PE"): sc, (exp, 25650.0, "PE"): wc}, {(exp, 25750.0, "PE"): sq, (exp, 25650.0, "PE"): wq},
            allow_unverified_in_replay=True,
        )
        self.assertFalse(vix_missing["passed"])
        self.assertIn("VIX_1p05_SMA10", vix_missing["reject_reason"])

        unknown_age_q = _mk_obs_quote("NSE_FO|X", 50.0, 50.5, ts)
        for k in ("l1_timestamp_ms", "l1_book_timestamp_ms", "depth_timestamp_ms", "local_receipt_ts_ms", "availability_ts_ms", "exchange_ltt_ms"):
            unknown_age_q.pop(k, None)
        unknown_age_q["quote_age_ms"] = 0
        self.assertIsNone(resolve_strict_quote_age_ms(unknown_age_q, now_ms=ts))
        self.assertEqual(sweep_observed_l5_book(unknown_age_q, "BUY", 65, now_ms=ts)["reason"], "UNKNOWN_QUOTE_AGE")

        carried_q = _mk_obs_quote("NSE_FO|X", 50.0, 50.5, ts, prov="CARRIED_PREV_TICK")
        self.assertEqual(sweep_observed_l5_book(carried_q, "BUY", 65, now_ms=ts)["reason"], "NON_OBSERVED_OR_SYNTHETIC_BOOK_REJECTED")
        stale_q = _mk_obs_quote("NSE_FO|X", 50.0, 50.5, ts - 7500)
        self.assertEqual(sweep_observed_l5_book(stale_q, "BUY", 65, now_ms=ts, max_age_ms=5000)["reason"], "STALE_L5_BOOK")

        skew_ok, skew_ms, skew_rsn = check_cross_leg_skew(wq, _mk_obs_quote("NSE_FO|PE25750", 90.0, 91.0, ts - 6500), max_skew_ms=5000)
        self.assertFalse(skew_ok)
        self.assertEqual(skew_ms, 6500)
        self.assertIn("CROSS_LEG_SKEW", skew_rsn)

    def test_05_partial_leg1_fill_strict_credit_floor_unwind_and_hedge_only_open(self) -> None:
        ts = 1_795_200_000_000
        exp = "2026-11-23"
        sc = _mk_contract("NSE_FO|PE25750", 25750.0, "PE", exp, lot_size=65)
        wc = _mk_contract("NSE_FO|PE25650", 25650.0, "PE", exp, lot_size=65)

        self.cfg["books"]["V2_BOOK_A_PROPOSED_O3"]["lot_count"] = 2
        wq_1lot = _mk_obs_quote("NSE_FO|PE25650", 54.0, 54.4, ts, qty_per_lvl=13, levels_count=5, strike=25650.0, ot="PE", expiry=exp)
        l1 = self.sim.open_book_a_leg1_hedge("sig_pf1", "HYPOTHETICAL_MODIFIED_O3_NOT_PHASE4_EQUIVALENT", "2026-11-20", "2026-11-20 14:00:00", ts, wc, sc, wq_1lot, 25810.0, 13.5, True, allow_partial_lot=True)
        self.assertTrue(l1["ok"])
        self.assertEqual(l1["confirmed_hedge_qty"], 65)

        sq_low = _mk_obs_quote("NSE_FO|PE25750", 82.0, 82.5, ts + 200, qty_per_lvl=65, strike=25750.0, ot="PE", expiry=exp)
        l2_unw = self.sim.complete_or_unwind_pending_leg2(l1["package_id"], sq_low, wq_1lot, ts + 200, "2026-11-20", "2026-11-20 14:00:00")
        self.assertFalse(l2_unw["ok"])
        self.assertEqual(l2_unw["state"], "INCOMPLETE_LEG_UNWOUND")
        self.assertEqual(len(self.sim.open_packages), 0)

        l1b = self.sim.open_book_a_leg1_hedge("sig_pf2", "HYPOTHETICAL_MODIFIED_O3_NOT_PHASE4_EQUIVALENT", "2026-11-20", "2026-11-20 14:05:00", ts + 300_000, wc, sc, _mk_obs_quote("NSE_FO|PE25650", 54.0, 54.4, ts + 300_000, qty_per_lvl=65, strike=25650.0, ot="PE", expiry=exp), 25810.0, 13.5, True)
        self.assertTrue(l1b["ok"])
        l2_hold = self.sim.complete_or_unwind_pending_leg2(l1b["package_id"], sq_low, None, ts + 300_200, "2026-11-20", "2026-11-20 14:05:00")
        self.assertFalse(l2_hold["ok"])
        self.assertEqual(l2_hold["state"], "HEDGE_ONLY_EXPOSURE_OPEN")
        self.assertIn(l1b["package_id"], self.sim.open_packages)

    def test_06_subscription_eviction_v2_yields_first_and_conservative_mtm_charges_max_loss(self) -> None:
        reg = V2SubscriptionRegistry(ws_total_soft_cap=10, v1_reserved_headroom=2, max_v2_additive_pins=4)
        v1_keys = {"NSE_FO|V1_A", "NSE_FO|V1_B"}
        acq = reg.acquire_for_package(
            "pkg_1",
            [{"instrument_key": "NSE_FO|V1_A"}, {"instrument_key": "NSE_FO|V2_WING_ONLY"}],
            existing_stream_keys_fn=lambda: v1_keys,
            subscribe_additive_fn=lambda cs: None,
        )
        self.assertEqual(acq["reused_v1_keys"], ["NSE_FO|V1_A"])
        self.assertEqual(acq["pinned_v2_only"], ["NSE_FO|V2_WING_ONLY"])

        dropped = reg.yield_under_pressure(v1_active_keys=v1_keys)
        self.assertEqual(dropped, ["NSE_FO|V2_WING_ONLY"])
        self.assertTrue(reg.is_key_evicted("NSE_FO|V2_WING_ONLY"))

        ts = 1_795_300_000_000
        c = _mk_contract("NSE_FO|V2_WING_ONLY", 25800.0, "CE", "2026-11-26")
        q = _mk_obs_quote("NSE_FO|V2_WING_ONLY", 140.0, 141.0, ts, strike=25800.0, ot="CE", expiry="2026-11-26")
        op = self.sim.open_book_b_paid_long("sig_ev", "V2_FROZEN_COMBINED", "2026-11-19", "2026-11-19 10:45:00", ts, c, q, 25800.0, {})
        self.assertTrue(op["ok"])
        # Mark reliably once so last_reliable_mark is populated before eviction
        self.sim.mark_open_packages({"NSE_FO|V2_WING_ONLY": q}, "2026-11-19", "2026-11-19 10:45:00", ts)
        self.sim.mark_key_evicted("NSE_FO|V2_WING_ONLY")
        mtm = self.sim.mark_open_packages({"NSE_FO|V2_WING_ONLY": q}, "2026-11-19", "2026-11-19 10:46:00", ts + 60_000)
        self.assertEqual(mtm["unpriced_count"], 1)
        self.assertEqual(mtm["unpriced_details"][0]["reason"], "LEG_SUBSCRIPTION_EVICTED")
        self.assertLess(mtm["conservative_unrealized_mtm_inr"], -9000.0)
        pkg_ev = self.sim.open_packages[op["package_id"]]
        self.assertIsNotNone(pkg_ev.get("last_reliable_mark_ts_ms"))
        self.assertIsNotNone(pkg_ev.get("last_reliable_mark_net_inr"))

        svc = OptionSnifferV2Service(db_path=self.db_path)
        promo = svc.evaluate_promotion_gates([], [], list(self.sim.open_packages.values()))
        self.assertEqual(promo["unresolved_exposure_count"], 1)
        self.assertGreater(promo["unresolved_structural_risk_charged_inr"], 9000.0)
        self.assertEqual(promo["status"], "FAIL_COLLECTING_FORWARD_EVIDENCE")
        svc.store.close()

    def test_07_gap_through_stop_overshoot_decomposition_and_crash_restart_recovery(self) -> None:
        ts = 1_795_400_000_000
        c = _mk_contract("NSE_FO|CE25800", 25800.0, "CE", "2026-11-26")
        q_in = _mk_obs_quote("NSE_FO|CE25800", 149.5, 150.0, ts, strike=25800.0, ot="CE", expiry="2026-11-26")
        op = self.sim.open_book_b_paid_long("sig_gap", "V2_FROZEN_COMBINED", "2026-11-19", "2026-11-19 10:30:00", ts, c, q_in, 25810.0, {})
        self.assertTrue(op["ok"])
        pkg_id = op["package_id"]
        eq_before_restart = self.sim.realized_equity_inr
        res_before_restart = self.sim.reserved_capital_inr

        self.store.close()
        store2 = V2LedgerStore(self.db_path, auto_create=True)
        sim2 = V2PaperSimulator(self.cfg, self.cfg_hash, store2, run_mode="FORWARD_PAPER")
        sim2.recover_state_from_db()
        self.assertEqual(sim2.realized_equity_inr, eq_before_restart)
        self.assertEqual(sim2.reserved_capital_inr, res_before_restart)
        self.assertIn(pkg_id, sim2.open_packages)

        ts_gap = ts + 120_000
        q_gap = _mk_obs_quote("NSE_FO|CE25800", 100.0, 100.5, ts_gap, strike=25800.0, ot="CE", expiry="2026-11-26")
        ex = manage_open_book_b_on_bar(sim2, sim2.open_packages[pkg_id], "2026-11-19", "2026-11-19 10:32:00", 1032, ts_gap, 25710.0, 25750.0, {"NSE_FO|CE25800": q_gap})
        self.assertTrue(ex["ok"])
        self.assertEqual(ex["exit_reason"], "PLANNED_HARD_SL_HIT")
        self.assertGreater(ex["slippage_and_gap_overshoot_inr"], 1000.0)
        closed_pkg = store2.load_packages("FORWARD_PAPER", self.cfg_hash)[0]
        self.assertAlmostEqual(closed_pkg["gross_pnl_inr"] - closed_pkg["total_friction_inr"], closed_pkg["net_pnl_inr"], places=2)
        store2.close()

    def test_08_duplicate_late_gap_queue_overflow_and_missed_1445_cutoff_fsp(self) -> None:
        fc = V2BoundedFeedConsumer(self.cfg_hash, maxlen=64)
        ts = 1_795_500_000_000
        ev = [("OPTION", "NSE_FO|X", {"instrument_key": "NSE_FO|X", "ltp": 50.0, "exchange_ltt_ms": ts})]
        self.assertEqual(fc.push_events(ev, receipt_ts_ms=ts), 1)
        self.assertEqual(fc.push_events(ev, receipt_ts_ms=ts + 10), 0)
        self.assertEqual(fc.push_events([("OPTION", "NSE_FO|X", {"instrument_key": "NSE_FO|X", "ltp": 49.0, "exchange_ltt_ms": ts - 5000})]), 1)
        self.assertFalse(fc.check_minute_bar_continuity("2026-11-23 10:00:00") and False)
        self.assertFalse(fc.check_minute_bar_continuity("2026-11-23 10:05:00"))
        for i in range(80):
            fc.push_events([("OPTION", "NSE_FO|X", {"instrument_key": "NSE_FO|X", "ltp": 50.0 + i, "exchange_ltt_ms": ts + 1000 + i})])
        hm = fc.get_health_metrics()
        self.assertGreater(hm["dropped_overflow_events"], 0)
        self.assertIn("UNCERTAINTY_QUEUE_OVERFLOW", hm["uncertainty_blocks"])

        exp = "2026-11-23"
        sc = _mk_contract("NSE_FO|PE25750", 25750.0, "PE", exp)
        wc = _mk_contract("NSE_FO|PE25650", 25650.0, "PE", exp)
        sq = _mk_obs_quote("NSE_FO|PE25750", 90.0, 90.5, ts, strike=25750.0, ot="PE", expiry=exp)
        wq = _mk_obs_quote("NSE_FO|PE25650", 50.0, 50.4, ts, strike=25650.0, ot="PE", expiry=exp)
        l1 = self.sim.open_book_a_leg1_hedge("sig_cut", "HYPOTHETICAL_MODIFIED_O3_NOT_PHASE4_EQUIVALENT", "2026-11-20", "2026-11-20 14:00:00", ts, wc, sc, wq, 25810.0, 13.5, True)
        self.sim.complete_or_unwind_pending_leg2(l1["package_id"], sq, wq, ts + 200, "2026-11-20", "2026-11-20 14:00:00")

        ts_1445 = ts + 86400_000
        res_cut = manage_open_book_a_on_bar(self.sim, self.sim.open_packages[l1["package_id"]], exp, f"{exp} 14:45:00", 1445, ts_1445, 25720.0, {})
        self.assertFalse(res_cut["ok"])
        self.assertEqual(res_cut["state"], "UNRESOLVED_EXPOSURE_PAST_1445")
        self.assertIn(l1["package_id"], self.sim.open_packages)

        ts_1530 = ts_1445 + 45 * 60_000
        res_fsp = manage_open_book_a_on_bar(self.sim, self.sim.open_packages[l1["package_id"]], exp, f"{exp} 15:30:00", 1530, ts_1530, 25710.0, {}, fsp_twap_spot=25710.0)
        self.assertTrue(res_fsp["ok"])
        self.assertEqual(res_fsp["exit_reason"], "EXPIRY_1530_OBSERVED_FSP_SETTLEMENT")
        self.assertEqual(len(self.sim.open_packages), 0)

    def test_09_causal_replay_timing_proofs_superseded_v20_and_counterfactuals(self) -> None:
        v1_conn = open_v1_readonly_db()
        try:
            o3 = load_phase4_o3_audit_report()
            self.assertTrue(o3["monthly_reconciliation"]["exact_arithmetic_reconciled"])
            self.assertEqual(o3["spec_conflicts_count"], 6)
            self.assertFalse(o3["autonomous_activation_decision"]["autonomous_entries_enabled"])

            cf = run_v1_counterfactual_audit(v1_conn, store=self.store)
            self.assertFalse(cf["included_in_v2_totals"])
            self.assertGreaterEqual(cf["cf1_summary"]["total_v1_packages"], 8)
            self.assertGreaterEqual(cf["cf1_summary"]["realized_count"], 5)
            self.assertEqual(
                cf["cf1_summary"]["total_v1_packages"],
                cf["cf1_summary"]["realized_count"] + cf["cf1_summary"]["unresolved_count"],
            )

            rep1 = run_v2_replay_and_attribution(v1_conn, self.store, self.cfg, self.cfg_hash)
            self.assertEqual(rep1["replay_run_mode"], "REPLAY_V2_2_CAUSAL")
            self.assertEqual(rep1["causal_verification_outcome"]["verified_futures_basis_trades"], 0)
            self.assertGreater(rep1["causal_verification_outcome"]["unverifiable_basis_events"], 1000)
            self.assertGreaterEqual(rep1["causal_verification_outcome"]["late_events_rejected"], 22)
            self.assertGreater(rep1["causal_verification_outcome"]["post_session_flushes_ignored"], 0)
            self.assertEqual(
                rep1["archive_timing_evidence"]["bar_2026_11_20_1030"]["earliest_recorded_at"],
                "2026-11-20T10:31:00.109787",
            )
            self.assertEqual(
                rep1["archive_timing_evidence"]["delayed_2026_11_20_0940"]["delayed_recorded_at"],
                "2026-11-20T09:44:10.039544",
            )
            self.assertEqual(rep1["superseded_v2_0_replay"]["evidence_status"], "SUPERSEDED_V2_0_NONCAUSAL_SYNTHETIC_SYNC")

            sha1 = rep1["canonical_replay"]["ledger_sha256"]
            rep2 = run_v2_replay_and_attribution(v1_conn, self.store, self.cfg, self.cfg_hash)
            sha2 = rep2["canonical_replay"]["ledger_sha256"]
            self.assertEqual(sha1, sha2)
        finally:
            v1_conn.close()

        conn = self.store.connect()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO osv2_counterfactual_v1 (
                    cf_id, experiment_code, v1_package_id, session_date, book_id,
                    v1_reported_net_inr, cf_status, included_in_v2_totals, detail_json
                ) VALUES ('bad_cf', 'X', 'p1', '2026-11-19', 'B', 0, 'X', 1, '{}');
                """
            )
        conn.close()

        col_off = compute_optional_collateral_report(200000.0, 9725.02, 10855.0, 6, self.cfg["optional_collateral_scenario_defaults"], enabled=False)
        col_on = compute_optional_collateral_report(200000.0, 9725.02, 10855.0, 6, self.cfg["optional_collateral_scenario_defaults"], enabled=True)
        self.assertEqual(col_off["hypothetical_collateral_income_inr"], 0.0)
        self.assertGreater(col_on["hypothetical_collateral_income_inr"], 0.0)

    def _build_realistic_timed_stream(self, session_date: str = "2026-11-20", exp: str = "2026-11-26") -> list:
        """
        Builds a realistic strictly timed event stream (09:15..11:30 IST):
          - Tests warmup period (bars < 09:45 & < 28 bars), interleaved foreign BANKNIFTY/FINNIFTY ticks,
          - Captures valid NIFTY open_basis_ref at 09:16..09:20,
          - Tests boundary tick reversal (10:29 bullish close BEFORE 10:30 reversal drop),
          - Triggers Book B entry after >=28 bars + ER20 >= 0.32 + DeltaBasis >= +3.0 + Spot > TPB > ORB30_H.
        """
        t0 = parse_ist_timestamp_ms(f"{session_date}T09:15:00+05:30")
        assert t0 is not None
        events = []
        spot = 25700.0
        for m in range(95):  # 09:15..10:49 IST (95 1m bars)
            bar_open_ms = t0 + m * 60_000
            ts_tick = bar_open_ms + 57_000  # 3,000ms fresh (<= 5,000ms entry cutoff) at bar_close
            bar_close_check = bar_open_ms + 60_000

            # Interleave foreign underlyings every 10 bars (must all be rejected!)
            if m in (5, 25, 65):
                events.append(("INDEX", "BANKNIFTY", {"instrument_key": "NSE_INDEX|Nifty Bank", "underlying": "BANKNIFTY", "spot_ltp": 52000.0, "availability_ts_ms": ts_tick}))
                events.append(("FUTURES", "FINNIFTY", {"instrument_key": "NSE_FO|FINNIFTYFUT", "underlying": "FINNIFTY", "expiry": exp, "fut_ltp": 24000.0, "availability_ts_ms": ts_tick, "exchange_ltt_ms": ts_tick, "source_ts_proven": True}))
                events.append(("OPTION", "NSE_FO|BANKNIFTY_CE", _mk_obs_quote("NSE_FO|BANKNIFTY_CE", 300.0, 302.0, ts_tick, strike=52000.0, ot="CE", expiry=exp, underlying="BANKNIFTY")))

            if m < 30:
                spot = 25700.0 + (m % 4) * 2.0  # Choppy ORB30 range 25700..25706
                fut = spot + 20.0               # Open basis = +20 pt
            elif m < 65:
                spot += 1.8                     # Clean steady trend -> high ER20 >= 0.6 & ADX14 >= 20
                fut = spot + 24.5               # DeltaBasis = +4.5 pt >= +3.0 pt!
            elif m == 65:
                # Exit trigger via adverse gap below stop at bar 65 (10:20) after holding winning/opening bar
                spot -= 2.0
                fut = spot + 24.5
            else:
                spot += 1.0
                fut = spot + 24.0

            events.append(("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": round(spot, 2), "availability_ts_ms": ts_tick, "exchange_ltt_ms": ts_tick, "source_ts_proven": True}))
            events.append(("FUTURES", "NIFTY", {"instrument_key": "NSE_FO|NIFTYFUT_W1", "underlying": "NIFTY", "trading_symbol": "NIFTY FUT 26 NOV 26", "expiry": "2026-11-26", "fut_ltp": round(fut, 2), "availability_ts_ms": ts_tick, "exchange_ltt_ms": ts_tick, "source_ts_proven": True}))

            atm_k = round(spot / 50.0) * 50.0
            for k in (atm_k - 50.0, atm_k, atm_k + 50.0, 25750.0):
                px = max(25.0, (spot - k) + 120.0) if m != 65 else 70.0
                events.append(
                    (
                        "OPTION",
                        f"NSE_FO|CE_{int(k)}",
                        _mk_obs_quote(f"NSE_FO|CE_{int(k)}", round(px - 0.25, 2), round(px + 0.25, 2), ts_tick, strike=k, ot="CE", expiry=exp),
                    )
                )
            events.append(("BAR_CLOSE_CHECK", "CLOCK", {"availability_ts_ms": bar_close_check}))
        return events

    def test_10_unified_engine_boundary_tick_warmup_foreign_and_forward_replay_parity(self) -> None:
        # Part A: Exact bar-boundary reversal check (bar t bullish close must finalize BEFORE tick t+1 drop!)
        eng = V2UnifiedEventEngine(self.cfg, self.cfg_hash, self.store, self.sim, run_mode="FORWARD_PAPER", persist_bars_and_checkpoints=False)
        t0 = parse_ist_timestamp_ms("2026-11-20T09:15:00+05:30")
        assert t0 is not None
        eng.ingest_event("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25800.0}, availability_ts_ms=t0 + 10_000)
        eng.ingest_event("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25845.0}, availability_ts_ms=t0 + 55_000)
        self.assertIsNone(eng.last_indicators["tpb_vwap"])  # Warmup fail-closed before 3 bars & <28 ADX
        self.assertIsNone(eng.last_indicators["adx14"])
        self.assertIsNone(eng.last_indicators["er20"])
        self.assertIsNone(eng.last_indicators["orb30_h"])
        # First tick of minute 09:16 plunges to 25710 -> must finalize 09:15 BEFORE 25710 mutates bar!
        eng.ingest_event("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25710.0}, availability_ts_ms=t0 + 62_000)
        self.assertEqual(eng.completed_bars[0]["spot_c"], 25845.0)
        self.assertGreaterEqual(eng.completed_bars[0]["spot_l"], 25800.0)

        # Part B: Exact Forward Paper (Service) vs Causal Replay (run_event_stream_replay) parity on identical stream
        events = self._build_realistic_timed_stream()
        rep_store_db = os.path.join(self._td.name, "parity_rep.db")
        rep_store = V2LedgerStore(rep_store_db, auto_create=True)
        try:
            rep = run_event_stream_replay(events, self.cfg, self.cfg_hash, rep_store, run_mode="REPLAY_STREAM", persist=True)
        finally:
            rep_store.close()

        fwd_db = os.path.join(self._td.name, "parity_fwd.db")
        set_v2_test_db_override(fwd_db)
        reset_option_sniffer_v2_service()
        svc = OptionSnifferV2Service(db_path=fwd_db)
        try:
            svc.start_parallel_collector(stream_manager=None, run_archive_warmup=False, spawn_worker=False)
            for ev in events:
                svc.on_live_feed_events([ev])
            # Drain in chunks cleanly as live worker does
            while svc.feed_consumer.get_health_metrics()["current_queue_depth"] > 0:
                svc.process_queued_events(max_items=60)
            fwd_pkgs = svc.store.load_packages("FORWARD_PAPER", self.cfg_hash)
            fwd_sigs = svc.store.load_signals("FORWARD_PAPER", self.cfg_hash, limit=100)
        finally:
            svc.stop_v2_only()
            svc.store.close()

        self.assertGreater(rep["foreign_instrument_rejected_count"], 0)
        self.assertEqual(rep["foreign_instrument_rejected_count"], svc.unified_engine.foreign_instrument_rejected_count)
        self.assertGreater(len(rep["closed_trades"]), 0, "Expected at least 1 Book B trade in realistic stream!")
        self.assertEqual(len(rep["closed_trades"]), len([p for p in fwd_pkgs if p["state"] == "CLOSED"]))
        for rt, ft in zip(rep["closed_trades"], [p for p in fwd_pkgs if p["state"] == "CLOSED"]):
            self.assertEqual(rt["entry_bar_ts"], ft["entry_bar_ts"])
            self.assertEqual(rt["exit_bar_ts"], ft["exit_bar_ts"])
            self.assertEqual(rt["exit_reason"], ft["exit_reason"])
            self.assertAlmostEqual(rt["net_pnl_inr"], ft["net_pnl_inr"], places=2)
        self.assertEqual(len(fwd_sigs), len(rep["closed_trades"]))

    def test_11_fresh_l1_strips_carried_l2_l5_and_fails_excess_qty(self) -> None:
        ts = 1_795_600_000_000
        # Observed fresh L1 (qty=65) + carried stale L2-L5 depth rungs!
        q_mixed = {
            "instrument_key": "NSE_FO|PE25700",
            "trading_symbol": "NIFTY 25700 PE 26 NOV 26",
            "underlying": "NIFTY",
            "expiry": "2026-11-26",
            "strike": 25700.0,
            "option_type": "PE",
            "raw_bid": 100.0,
            "raw_ask": 100.5,
            "bid": 100.0,
            "ask": 100.5,
            "l1_bid_qty_observed": 65,
            "l1_ask_qty_observed": 65,
            "is_observed_l1_book": True,
            "l1_observed_on_tick": True,
            "l1_timestamp_ms": ts,
            "depth_observed_on_tick": False,
            "depth_rungs_carried": True,
            "depth_timestamp_ms": ts - 25_000,
            "book_provenance": "CARRIED_PREV_DEPTH",
            "depth_levels": [
                {"bid_p": 100.0, "bid_q": 65, "ask_p": 100.5, "ask_q": 65},
                {"bid_p": 99.8, "bid_q": 260, "ask_p": 100.7, "ask_q": 260},
            ],
        }
        # Strict L5 entry rejects carried depth
        sw_ent = sweep_observed_l5_book(q_mixed, "BUY", 65, 65, 1, 5000, ts, require_observed_l5=True)
        self.assertFalse(sw_ent["ok"])

        # Exit with allow_fresh_l1_only=True succeeds for qty=65 stripping carried L2-L5
        sw_ex1 = sweep_observed_l5_book(q_mixed, "SELL", 65, 65, 1, 5000, ts, max_spread_pct=None, require_observed_l5=False, allow_fresh_l1_only=True)
        self.assertTrue(sw_ex1["ok"])
        self.assertTrue(sw_ex1["carried_l2_l5_stripped"])
        self.assertEqual(len(sw_ex1["consumed_rungs"]), 1)
        self.assertEqual(sw_ex1["consumed_ts_ms"], ts)

        # Exit for qty=130 fails because carried L2-L5 was stripped and L1 only holds 65 units!
        sw_ex2 = sweep_observed_l5_book(q_mixed, "SELL", 130, 65, 1, 5000, ts, max_spread_pct=None, require_observed_l5=False, allow_fresh_l1_only=True)
        self.assertFalse(sw_ex2["ok"])
        self.assertIn("INSUFFICIENT_OBSERVED_L1_SIZE", sw_ex2["reason"])

    def test_12_chronological_drawdown_session_states_and_4point_restart_parity(self) -> None:
        dd = compute_chronological_drawdown([200000.0, 220000.0, 201000.0], starting_capital_inr=200000.0)
        self.assertEqual(dd["running_peak_equity_inr"], 220000.0)
        self.assertEqual(dd["max_dd_inr"], 19000.0)
        self.assertEqual(dd["max_dd_pct_of_starting_capital"], 9.5)
        self.assertEqual(dd["max_dd_pct_of_peak_equity"], 8.64)

        # 4-point restart parity over realistic stream
        events = self._build_realistic_timed_stream()
        n = len(events)
        pts = [int(n * 0.2), int(n * 0.45), int(n * 0.7), int(n * 0.85)]

        unint_db = os.path.join(self._td.name, "unint.db")
        st_u = V2LedgerStore(unint_db, auto_create=True)
        sim_u = V2PaperSimulator(self.cfg, self.cfg_hash, st_u, run_mode="FORWARD_PAPER")
        eng_u = V2UnifiedEventEngine(self.cfg, self.cfg_hash, st_u, sim_u, run_mode="FORWARD_PAPER")
        with st_u.writer_session():
            for ev in events:
                eng_u.ingest_event(ev[0], ev[1], ev[2], availability_ts_ms=ev[2].get("availability_ts_ms"))
            eng_u.flush()
        u_pkgs = st_u.load_packages("FORWARD_PAPER", self.cfg_hash)
        st_u.close()

        rest_db = os.path.join(self._td.name, "restored.db")
        chunks = [events[:pts[0]], events[pts[0]:pts[1]], events[pts[1]:pts[2]], events[pts[2]:pts[3]], events[pts[3]:]]
        for ci, chunk in enumerate(chunks):
            st_r = V2LedgerStore(rest_db, auto_create=True)
            sim_r = V2PaperSimulator(self.cfg, self.cfg_hash, st_r, run_mode="FORWARD_PAPER")
            sim_r.recover_state_from_db()
            eng_r = V2UnifiedEventEngine(self.cfg, self.cfg_hash, st_r, sim_r, run_mode="FORWARD_PAPER")
            with st_r.writer_session():
                for ev in chunk:
                    eng_r.ingest_event(ev[0], ev[1], ev[2], availability_ts_ms=ev[2].get("availability_ts_ms"))
                if ci == len(chunks) - 1:
                    eng_r.flush()
            last_peak = sim_r.running_peak_equity_inr
            last_dd = sim_r.max_mtm_dd_inr
            last_eq = sim_r.realized_equity_inr
            st_r.close()

        st_r_check = V2LedgerStore(rest_db, auto_create=False)
        r_pkgs = st_r_check.load_packages("FORWARD_PAPER", self.cfg_hash)
        st_r_check.close()

        self.assertAlmostEqual(sim_u.realized_equity_inr, last_eq, places=2)
        self.assertAlmostEqual(sim_u.running_peak_equity_inr, last_peak, places=2)
        self.assertAlmostEqual(sim_u.max_mtm_dd_inr, last_dd, places=2)
        self.assertEqual(len(u_pkgs), len(r_pkgs))
        for up, rp in zip(u_pkgs, r_pkgs):
            self.assertEqual(up["package_id"], rp["package_id"])
            self.assertEqual(up["exit_reason"], rp["exit_reason"])
            self.assertAlmostEqual(up["net_pnl_inr"], rp["net_pnl_inr"], places=2)

    def test_13_cf1_arming_target_85_protective_55_gap_vwap_flip_and_stale(self) -> None:
        exp = "2026-11-23"
        wl = {"option_type": "PE", "strike": 25650.0, "qty": 65, "entry_price": 40.0}
        sl = {"option_type": "PE", "strike": 25750.0, "qty": 65, "entry_price": 140.0}
        # Entry credit = 100.0 pt => 70% arm debit <= 30.0, 85% target <= 15.0, 55% lock >= 45.0, hard_sl = 180.0
        t0 = parse_ist_timestamp_ms("2026-11-23T10:15:00+05:30")
        assert t0 is not None

        def _mk_pair(mins: int, short_ask: float, wing_bid: float, spot: float = 25900.0, tpb: float = 25800.0, fresh: bool = True) -> dict:
            bt_s = f"2026-11-23 {10 + (15 + mins) // 60:02d}:{(15 + mins) % 60:02d}:00"
            av = t0 + mins * 60_000 + 30_000
            q_ts = av if fresh else av - 20_000
            return {
                "bar_ts": bt_s,
                "avail_ms": av,
                "spot_ltp": spot,
                "causal_tpb_vwap": tpb,
                "wing_quote": _mk_obs_quote("NSE_FO|W", wing_bid, wing_bid + 0.5, q_ts, strike=25650.0, ot="PE", expiry=exp),
                "short_quote": _mk_obs_quote("NSE_FO|S", short_ask - 0.5, short_ask, q_ts, strike=25750.0, ot="PE", expiry=exp),
            }

        # 1. Arm at 70% (deb=25 <= 30) then hit 85% target (deb=12 <= 15)
        r_85 = simulate_cf1_spread_trajectory(
            "2026-11-23 10:15:00", "2026-11-23", 100.0, 180.0, wl, sl,
            [_mk_pair(5, 45.0, 20.0), _mk_pair(10, 30.0, 18.0)],
        )
        self.assertEqual(r_85["outcome_class"], "REALIZED_OUTCOME")
        self.assertEqual(r_85["cf_exit_reason"], "CF1_TARGET_85PCT_HIT")

        # 2. Arm at 70% (deb=25) -> adverse gap to deb=55 (>= 45 protective lock) fills at actual 55 (not capped 45!)
        r_gap = simulate_cf1_spread_trajectory(
            "2026-11-23 10:15:00", "2026-11-23", 100.0, 180.0, wl, sl,
            [_mk_pair(5, 45.0, 20.0), _mk_pair(10, 75.0, 20.0)],
        )
        self.assertEqual(r_gap["cf_exit_reason"], "CF1_PROTECTIVE_55PCT_LOCK_HIT")
        self.assertGreater(r_gap["detail"]["exit_debit"], 45.0)

        # 3. Post-arm -> 15:15 EOD between 55% and 85% (deb=22)
        bt_1515 = {**_mk_pair(10, 42.0, 20.0), "bar_ts": "2026-11-23 15:15:00", "avail_ms": parse_ist_timestamp_ms("2026-11-23T15:15:30+05:30")}
        bt_1515["wing_quote"]["l1_timestamp_ms"] = bt_1515["avail_ms"]
        bt_1515["wing_quote"]["l1_book_timestamp_ms"] = bt_1515["avail_ms"]
        bt_1515["wing_quote"]["depth_timestamp_ms"] = bt_1515["avail_ms"]
        bt_1515["short_quote"]["l1_timestamp_ms"] = bt_1515["avail_ms"]
        bt_1515["short_quote"]["l1_book_timestamp_ms"] = bt_1515["avail_ms"]
        bt_1515["short_quote"]["depth_timestamp_ms"] = bt_1515["avail_ms"]
        r_eod = simulate_cf1_spread_trajectory(
            "2026-11-23 10:15:00", "2026-11-23", 100.0, 180.0, wl, sl,
            [_mk_pair(5, 45.0, 20.0), bt_1515],
        )
        self.assertEqual(r_eod["cf_exit_reason"], "CF1_EOD_1515_BETWEEN_55_AND_85")

        # 4. Stale after arming -> UNRESOLVED_STALE_AFTER_ARMING with cf_net_pnl_inr=None!
        r_stale_arm = simulate_cf1_spread_trajectory(
            "2026-11-23 10:15:00", "2026-11-23", 100.0, 180.0, wl, sl,
            [_mk_pair(5, 45.0, 20.0), _mk_pair(10, 42.0, 20.0, fresh=False)],
        )
        self.assertEqual(r_stale_arm["cf_status"], "UNRESOLVED_STALE_AFTER_ARMING")
        self.assertEqual(r_stale_arm["outcome_class"], "UNRESOLVED_OBSERVATION")
        self.assertIsNone(r_stale_arm["cf_net_pnl_inr"])

        # 5. Causal post-exit SPOT_VWAP_FLIP_EXIT triggered AFTER original V1 exit timestamp!
        r_flip = simulate_cf1_spread_trajectory(
            "2026-11-23 10:15:00", "2026-11-23", 100.0, 180.0, wl, sl,
            [
                _mk_pair(5, 65.0, 20.0, spot=25770.0, tpb=25800.0),
                _mk_pair(10, 68.0, 20.0, spot=25760.0, tpb=25800.0),
            ],
            v1_exit_reason="SPREAD_DECAY_TARGET_70PCT",
            v1_exit_bar_ts="2026-11-23 10:18:00",
        )
        self.assertEqual(r_flip["cf_exit_reason"], "CF1_SPOT_VWAP_FLIP_EXIT")

    def test_14_median_open_basis_rejects_0915_auction_outlier_and_recovers_from_db(self) -> None:
        t0 = parse_ist_timestamp_ms("2026-11-20T09:15:00+05:30")
        assert t0 is not None
        eng = V2UnifiedEventEngine(self.cfg, self.cfg_hash, self.store, self.sim, run_mode="FORWARD_PAPER", persist_bars_and_checkpoints=True)
        # 09:15 has extreme opening auction noise (-20.0 pt); 09:16..09:19 are tight [+22, +24, +25, +26]
        basis_vals = [-20.0, 22.0, 24.0, 25.0, 26.0]
        with self.store.writer_session():
            for m, b_pt in enumerate(basis_vals):
                ts_m = t0 + m * 60_000 + 57_000
                eng.ingest_event("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25800.0, "exchange_ltt_ms": ts_m, "source_ts_proven": True}, availability_ts_ms=ts_m)
                eng.ingest_event("FUTURES", "NIFTY", {"instrument_key": "NSE_FO|NIFTYFUT_D", "underlying": "NIFTY", "trading_symbol": "NIFTY FUT 29 DEC 26", "expiry": "2026-12-29", "fut_ltp": 25800.0 + b_pt, "exchange_ltt_ms": ts_m, "source_ts_proven": True}, availability_ts_ms=ts_m)
                eng.ingest_event("BAR_CLOSE_CHECK", "CLOCK", {"availability_ts_ms": t0 + (m + 1) * 60_000}, availability_ts_ms=t0 + (m + 1) * 60_000)

        self.assertIsNotNone(eng.open_basis_ref)
        self.assertTrue(eng.open_basis_ref["ok"])
        self.assertEqual(eng.open_basis_ref["sample_count"], 5)
        self.assertEqual(eng.open_basis_ref["basis_pts"], 24.0)  # Outlier -20 cleanly rejected by median!

        # Recover into fresh store/engine instance and add 09:20 (+28.0) -> 6-sample median = (24 + 25)/2 = 24.5
        sim2 = V2PaperSimulator(self.cfg, self.cfg_hash, self.store, run_mode="FORWARD_PAPER")
        sim2.recover_state_from_db()
        eng2 = V2UnifiedEventEngine(self.cfg, self.cfg_hash, self.store, sim2, run_mode="FORWARD_PAPER", persist_bars_and_checkpoints=True)
        self.assertEqual(len(eng2.open_basis_samples), 5)
        ts_5 = t0 + 5 * 60_000 + 57_000
        with self.store.writer_session():
            eng2.ingest_event("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25800.0, "exchange_ltt_ms": ts_5, "source_ts_proven": True}, availability_ts_ms=ts_5)
            eng2.ingest_event("FUTURES", "NIFTY", {"instrument_key": "NSE_FO|NIFTYFUT_D", "underlying": "NIFTY", "trading_symbol": "NIFTY FUT 29 DEC 26", "expiry": "2026-12-29", "fut_ltp": 25828.0, "exchange_ltt_ms": ts_5, "source_ts_proven": True}, availability_ts_ms=ts_5)
            eng2.ingest_event("BAR_CLOSE_CHECK", "CLOCK", {"availability_ts_ms": t0 + 6 * 60_000}, availability_ts_ms=t0 + 6 * 60_000)
        self.assertEqual(eng2.open_basis_ref["sample_count"], 6)
        self.assertEqual(eng2.open_basis_ref["basis_pts"], 24.5)

        # Mid-window futures contract roll must fail closed
        ts_6 = t0 + 6 * 60_000 + 57_000
        with self.store.writer_session():
            eng2.ingest_event("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25800.0, "exchange_ltt_ms": ts_6, "source_ts_proven": True}, availability_ts_ms=ts_6)
            eng2.ingest_event("FUTURES", "NIFTY", {"instrument_key": "NSE_FO|NIFTYFUT_JAN", "underlying": "NIFTY", "trading_symbol": "NIFTY FUT 25 JAN 27", "expiry": "2027-01-25", "fut_ltp": 25910.0, "exchange_ltt_ms": ts_6, "source_ts_proven": True}, availability_ts_ms=ts_6)
            eng2.ingest_event("BAR_CLOSE_CHECK", "CLOCK", {"availability_ts_ms": t0 + 7 * 60_000}, availability_ts_ms=t0 + 7 * 60_000)
        self.assertFalse(eng2.open_basis_ref["ok"])
        self.assertEqual(eng2.open_basis_ref["reason"], "FUTURES_CONTRACT_ROLLED_MID_SESSION")

    def test_15_causal_intraday_spot_backfill_heals_late_0918_startup_without_lookahead(self) -> None:
        from datetime import datetime
        from app.analytics.option_sniffer_v2.v2_config import IST_TZ

        t_base = parse_ist_timestamp_ms("2026-11-20T09:15:00+05:30")
        assert t_base is not None
        mock_spot_candles = []
        mock_vix_candles = []
        for m in range(32):  # 09:15 .. 09:46 inclusive (30 completed before 09:45:20 + 09:45 in-flight + 09:46 future!)
            hh = 9 + (15 + m) // 60
            mm = (15 + m) % 60
            ts_str = f"2026-11-20T{hh:02d}:{mm:02d}:00+05:30"
            px = 25400.0 + m * 2.0
            mock_spot_candles.append([ts_str, px, px + 3.0, px - 2.0, px + 1.0, 0])
            mock_vix_candles.append([ts_str, 17.5, 17.6, 17.4, 17.5, 0])

        def _fetcher(ikey: str) -> list:
            return mock_spot_candles if "Nifty 50" in ikey else mock_vix_candles

        svc = OptionSnifferV2Service(db_path=self.db_path, intraday_candle_fetcher_fn=_fetcher)
        try:
            svc.start_parallel_collector(stream_manager=None, run_archive_warmup=False, spawn_worker=False)
            # Late start: only push 09:44:20 tick (missed 09:15..09:43!) and evaluate at 09:45:20 IST
            ts_0944 = parse_ist_timestamp_ms("2026-11-20T09:44:20+05:30")
            assert ts_0944 is not None
            svc.on_live_feed_events([("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25459.0, "availability_ts_ms": ts_0944})])
            svc.process_queued_events(now_dt=datetime(2026, 11, 20, 9, 45, 20, tzinfo=IST_TZ))

            self.assertEqual(len(svc.unified_engine.completed_bars), 30)
            self.assertEqual(svc.unified_engine.completed_bars[0]["bar_open_ts"], "2026-11-20 09:15:00")
            self.assertEqual(svc.unified_engine.completed_bars[-1]["bar_open_ts"], "2026-11-20 09:44:00")
            self.assertEqual(svc.unified_engine.sequence_gap_count, 0)
            self.assertTrue(svc.unified_engine.session_started_from_open)
            self.assertTrue(svc.unified_engine.last_indicators["orb30_ready"])

            ov = svc.get_overview()
            self.assertEqual(ov["feed_hygiene"]["completed_bars_today"], 30)
            self.assertTrue(ov["feed_hygiene"]["orb30_ready"])
        finally:
            svc.stop_v2_only()
            svc.store.close()

    def test_16_v2_dte2_watchlist_additive_pinning_on_t1_friday_and_zero_pin_when_v1_covers(self) -> None:
        from datetime import datetime
        from app.analytics.option_sniffer_v2.v2_config import IST_TZ
        from tests.golden.test_option_sniffer_v2_isolation_and_parity import _make_isolated_stream_manager

        mgr = _make_isolated_stream_manager()
        # Part A: Friday 2026-11-20 -> V1 only tracks 2026-11-23 (T-1, DTE=1), leaving DTE>=2 empty!
        with mgr._lock:
            mgr._key_to_contract["NSE_FO|V1_T1_CE"] = {
                "instrument_key": "NSE_FO|V1_T1_CE",
                "trading_symbol": "NIFTY 25400 CE 23 NOV 26",
                "underlying": "NIFTY",
                "expiry": "2026-11-23",
                "strike": 25400.0,
                "option_type": "CE",
                "lot_size": 65,
            }
            mgr._v1_core_chain_keys.add("NSE_FO|V1_T1_CE")

        cat = []
        for stk in (25350.0, 25400.0, 25450.0, 25500.0):
            for ot in ("CE", "PE"):
                cat.append({
                    "instrument_key": f"NSE_FO|DEC01_{int(stk)}_{ot}",
                    "trading_symbol": f"NIFTY {int(stk)} {ot} 01 DEC 26",
                    "underlying_symbol": "NIFTY",
                    "expiry": "2026-12-01",
                    "strike_price": stk,
                    "instrument_type": ot,
                    "lot_size": 65,
                })

        svc = OptionSnifferV2Service(db_path=self.db_path, contract_catalog_fn=lambda: cat)
        try:
            svc.start_parallel_collector(stream_manager=mgr, run_archive_warmup=False, spawn_worker=False)
            ts_1015 = parse_ist_timestamp_ms("2026-11-20T10:15:30+05:30")
            svc.on_live_feed_events([("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25405.0, "availability_ts_ms": ts_1015})])
            svc.process_queued_events(now_dt=datetime(2026, 11, 20, 10, 15, 30, tzinfo=IST_TZ))

            wl_keys = set(svc.subscription_registry.snapshot_status()["v2_owned_only_keys"])
            self.assertEqual(len(wl_keys), 6)
            self.assertIn("NSE_FO|DEC01_25350_CE", wl_keys)
            self.assertIn("NSE_FO|DEC01_25450_PE", wl_keys)
            self.assertIn(("2026-12-01", 25400.0, "CE"), svc.unified_engine.opt_contracts)
            self.assertIn("NSE_FO|V1_T1_CE", mgr._key_to_contract)

            # Spot moves to 25455 -> drops 25350, retains 25400 & 25450, adds 25500
            ts_1017 = parse_ist_timestamp_ms("2026-11-20T10:17:30+05:30")
            svc.on_live_feed_events([("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25455.0, "availability_ts_ms": ts_1017})])
            svc.process_queued_events(now_dt=datetime(2026, 11, 20, 10, 17, 30, tzinfo=IST_TZ))
            wl_keys_2 = set(svc.subscription_registry.snapshot_status()["v2_owned_only_keys"])
            self.assertEqual(len(wl_keys_2), 6)
            self.assertNotIn("NSE_FO|DEC01_25350_CE", wl_keys_2)
            self.assertIn("NSE_FO|DEC01_25500_CE", wl_keys_2)

            # After 13:05 window closes -> watchlist released automatically, V1 key untouched
            svc.process_queued_events(now_dt=datetime(2026, 11, 20, 13, 10, 0, tzinfo=IST_TZ))
            self.assertEqual(svc.subscription_registry.snapshot_status()["v2_owned_only_keys"], [])
            self.assertIn("NSE_FO|V1_T1_CE", mgr._key_to_contract)

            # Part B: When V1 ALREADY subscribes all 6 ATM+/-50 strikes -> 0 V2 pins added!
            with mgr._lock:
                for c in cat:
                    if c["strike_price"] in (25400.0, 25450.0, 25500.0):
                        mgr._key_to_contract[c["instrument_key"]] = {
                            "instrument_key": c["instrument_key"],
                            "trading_symbol": c["trading_symbol"],
                            "underlying": "NIFTY",
                            "expiry": "2026-12-01",
                            "strike": c["strike_price"],
                            "option_type": c["instrument_type"],
                            "lot_size": 65,
                        }
                        mgr._v1_core_chain_keys.add(c["instrument_key"])
            svc._last_dte2_pin_check_epoch = 0.0
            ts_1020 = parse_ist_timestamp_ms("2026-11-20T10:20:30+05:30")
            svc.on_live_feed_events([("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25455.0, "availability_ts_ms": ts_1020})])
            svc.process_queued_events(now_dt=datetime(2026, 11, 20, 10, 20, 30, tzinfo=IST_TZ))
            self.assertEqual(svc.subscription_registry.snapshot_status()["v2_owned_only_keys"], [])
        finally:
            svc.stop_v2_only()
            svc.store.close()

    def test_17_cash_cap_18500_allows_4dte_entry_while_clamping_planned_stop_to_1800(self) -> None:
        ts = parse_ist_timestamp_ms("2026-11-25T10:45:00+05:30")
        assert ts is not None
        exp = "2026-12-01"  # 4 trading DTE from 2026-11-25
        spot_q = {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25360.0, "exchange_ltt_ms": ts, "local_receipt_ts_ms": ts, "source_ts_proven": True}
        fut_q = {"instrument_key": "NSE_FO|58875", "underlying": "NIFTY", "trading_symbol": "NIFTY FUT 29 DEC 26", "expiry": "2026-12-29", "fut_ltp": 25545.0, "exchange_ltt_ms": ts, "local_receipt_ts_ms": ts, "source_ts_proven": True}
        open_ref = {"ok": True, "fut_instrument_key": "NSE_FO|58875", "fut_expiry": "2026-12-29", "basis_pts": 180.0, "sample_count": 15}
        ce_atm = _mk_contract("NSE_FO|CE25350", 25350.0, "CE", exp)
        # Outlay = (271.0 + 0.05) * 65 = Rs 17,618.25 -> passes new Rs 18,500 CashCap!
        q_atm = _mk_obs_quote("NSE_FO|CE25350", 269.0, 271.0, ts, strike=25350.0, ot="CE", expiry=exp)

        sig = evaluate_book_b_entry_signal(
            self.cfg, "2026-11-25", "2026-11-25 10:45:00", 1045, ts, 25360.0, 25340.0, 25350.0, 25300.0, 26.0, 0.36,
            spot_q, fut_q, open_ref, [exp],
            {(exp, 25350.0, "CE"): ce_atm},
            {(exp, 25350.0, "CE"): q_atm},
        )
        self.assertTrue(sig["passed"])
        self.assertFalse(sig["used_single_otm_fallback"])

        op = self.sim.open_book_b_paid_long("sig_4dte", "V2_FROZEN_COMBINED", "2026-11-25", "2026-11-25 10:45:00", ts, ce_atm, q_atm, 25360.0, sig["basis_meta"])
        self.assertTrue(op["ok"])
        pkg = self.sim.open_packages[op["package_id"]]
        self.assertGreater(pkg["reserved_capital_inr"], 17000.0)
        self.assertLessEqual(pkg["reserved_capital_inr"], 18500.0)
        self.assertLessEqual(pkg["planned_loss_inr"], 1800.0)
        ll = pkg["legs_state"]["long_leg"]
        self.assertLessEqual((ll["entry_price"] - pkg["legs_state"]["hard_stop"]) * ll["qty"], 1800.0)

    def test_18_pre_0915_midnight_and_preopen_ticks_rejected_without_gap_or_db_pollution(self):
        import os
        import tempfile
        from app.analytics.option_sniffer_v2.v2_config import (
            load_v2_preregistration,
            parse_ist_timestamp_ms,
        )
        from app.analytics.option_sniffer_v2.v2_ledger_db import V2LedgerStore
        from app.analytics.option_sniffer_v2.v2_paper_simulator import V2PaperSimulator
        from app.analytics.option_sniffer_v2.v2_unified_engine import (
            V2UnifiedEventEngine,
            compute_causal_bar_indicators,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg, cfg_hash = load_v2_preregistration()
            store = V2LedgerStore(os.path.join(tmp_dir, "v2_pre0915.db"))
            sim = V2PaperSimulator(cfg, cfg_hash, store, run_mode="FORWARD_PAPER")
            eng = V2UnifiedEventEngine(
                cfg,
                cfg_hash,
                store,
                sim,
                run_mode="FORWARD_PAPER",
                persist_bars_and_checkpoints=True,
            )

            pre_ticks = [
                ("INDEX", "NIFTY_SPOT", {"spot_ltp": 25349.65, "instrument_key": "NSE_INDEX|Nifty 50"}, "2026-11-25 00:01:26"),
                ("INDEX", "NIFTY_SPOT", {"spot_ltp": 25360.0, "instrument_key": "NSE_INDEX|Nifty 50"}, "2026-11-25 09:07:30"),
                ("INDEX", "INDIA_VIX", {"spot_ltp": 17.7, "instrument_key": "NSE_INDEX|India VIX"}, "2026-11-25 09:14:50"),
                ("INDEX", "NIFTY_SPOT", {"spot_ltp": 25365.0, "instrument_key": "NSE_INDEX|Nifty 50"}, "2026-11-25 09:14:55"),
            ]
            for kind, key, payload, ts_str in pre_ticks:
                ms = parse_ist_timestamp_ms(ts_str)
                res = eng.ingest_event(kind, key, {**payload, "availability_ts_ms": ms}, availability_ts_ms=ms)
                self.assertEqual(res.get("status"), "PRE_SESSION_IGNORED")

            bf_pre = eng.backfill_missing_completed_bars(
                "2026-11-25",
                [("2026-11-25 09:14:00", 25350.0, 25360.0, 25340.0, 25355.0)],
                now_ms=parse_ist_timestamp_ms("2026-11-25 09:14:55"),
            )
            self.assertEqual(bf_pre["inserted_count"], 0)
            self.assertIsNone(eng.session_date)
            self.assertIsNone(eng.building_bar)
            self.assertIsNone(eng.latest_spot_q)
            self.assertIsNone(eng.latest_vix_q)
            self.assertEqual(len(eng.completed_bars), 0)
            self.assertEqual(eng.sequence_gap_count, 0)
            self.assertEqual(
                store.load_completed_bars("FORWARD_PAPER", cfg_hash, session_date="2026-11-25"),
                [],
            )

            # Regular market open at 09:15 followed by 09:16 cleanly finalizes 09:15 with zero gaps
            t_915 = parse_ist_timestamp_ms("2026-11-25 09:15:05")
            eng.ingest_event(
                "INDEX",
                "NIFTY_SPOT",
                {"spot_ltp": 25370.0, "instrument_key": "NSE_INDEX|Nifty 50", "availability_ts_ms": t_915},
                availability_ts_ms=t_915,
            )
            t_916 = parse_ist_timestamp_ms("2026-11-25 09:16:02")
            eng.ingest_event(
                "INDEX",
                "NIFTY_SPOT",
                {"spot_ltp": 25375.0, "instrument_key": "NSE_INDEX|Nifty 50", "availability_ts_ms": t_916},
                availability_ts_ms=t_916,
            )
            self.assertEqual(eng.session_date, "2026-11-25")
            self.assertTrue(eng.session_started_from_open)
            self.assertEqual(eng.sequence_gap_count, 0)
            self.assertEqual(len(eng.completed_bars), 1)
            self.assertEqual(eng.completed_bars[0]["bar_open_ts"], "2026-11-25 09:15:00")
            db_bars = store.load_completed_bars("FORWARD_PAPER", cfg_hash, session_date="2026-11-25")
            self.assertEqual(len(db_bars), 1)
            self.assertEqual(db_bars[0]["bar_open_ts"], "2026-11-25 09:15:00")

            # Verify indicator filter ignores any hypothetical stray non-session bar
            ind = compute_causal_bar_indicators(
                [
                    {"bar_open_ts": "2026-11-25 00:01:00", "spot_h": 25349.65, "spot_l": 25349.65, "spot_c": 25349.65},
                    db_bars[0],
                ]
            )
            self.assertEqual(ind["bars_count"], 1)

            # Verify 15:30:05 closes 15:29 cleanly without opening a 15:30 building bar
            store_eod = V2LedgerStore(os.path.join(tmp_dir, "v2_eod.db"))
            sim_eod = V2PaperSimulator(cfg, cfg_hash, store_eod, run_mode="FORWARD_PAPER")
            eng_eod = V2UnifiedEventEngine(
                cfg,
                cfg_hash,
                store_eod,
                sim_eod,
                run_mode="FORWARD_PAPER",
                persist_bars_and_checkpoints=True,
            )
            t_1529 = parse_ist_timestamp_ms("2026-11-25 15:29:05")
            eng_eod.ingest_event(
                "INDEX",
                "NIFTY_SPOT",
                {"spot_ltp": 25400.0, "instrument_key": "NSE_INDEX|Nifty 50", "availability_ts_ms": t_1529},
                availability_ts_ms=t_1529,
            )
            t_1530 = parse_ist_timestamp_ms("2026-11-25 15:30:05")
            res_eod = eng_eod.ingest_event(
                "INDEX",
                "NIFTY_SPOT",
                {"spot_ltp": 25402.0, "instrument_key": "NSE_INDEX|Nifty 50", "availability_ts_ms": t_1530},
                availability_ts_ms=t_1530,
            )
            self.assertEqual(res_eod.get("bar_open_ts"), "2026-11-25 15:29:00")
            self.assertIsNone(eng_eod.building_bar)
            self.assertEqual(len(eng_eod.completed_bars), 1)
            self.assertEqual(eng_eod.completed_bars[0]["bar_open_ts"], "2026-11-25 15:29:00")
            store.close()
            store_eod.close()


if __name__ == "__main__":
    unittest.main()
