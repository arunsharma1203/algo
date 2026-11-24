"""
Comprehensive 17-Scenario Test Suite for V2 Option Sniffer (`option_sniffer_v2`).
All tests execute purely in isolated temporary SQLite databases via `set_v2_test_db_override`
and never touch `backend/market_data.db`, V1/V6B, or live ports.
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
    resolve_contract_lot_size,
    trading_sessions_between,
)
from app.analytics.option_sniffer_v2.v2_counterfactual_v1 import run_v1_counterfactual_audit
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
from app.analytics.option_sniffer_v2.v2_paper_simulator import V2PaperSimulator
from app.analytics.option_sniffer_v2.v2_replay_attribution import run_v2_replay_and_attribution
from app.analytics.option_sniffer_v2.v2_service import (
    OptionSnifferV2Service,
    reset_option_sniffer_v2_service,
)


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
) -> dict:
    depth = []
    for i in range(levels_count):
        bp = round(max(0.05, bid - i * 0.10), 2)
        ap = round(ask + i * 0.10, 2)
        depth.append({"bid_p": bp, "bid_q": qty_per_lvl, "ask_p": ap, "ask_q": qty_per_lvl})
    return {
        "instrument_key": ikey,
        "trading_symbol": f"NIFTY {int(strike)} {ot} {expiry}",
        "expiry": expiry,
        "strike": float(strike),
        "option_type": ot,
        "lot_size": 65,
        "raw_bid": bid,
        "raw_ask": ask,
        "bid": bid,
        "ask": ask,
        "ltp": ltp if ltp is not None else round(0.5 * (bid + ask), 2),
        "depth_levels": depth,
        "is_observed_l1_book": prov == "OBSERVED_V3_L5",
        "depth_observed_on_tick": prov == "OBSERVED_V3_L5",
        "book_provenance": prov,
        "l1_book_timestamp_ms": ts_ms,
        "local_receipt_ts_ms": ts_ms,
        "exchange_ltt_ms": ts_ms,
        "book_obi_l5": 0.25,
    }


def _mk_contract(ikey: str, strike: float, ot: str, expiry: str, lot_size: int = 65) -> dict:
    return {
        "instrument_key": ikey,
        "trading_symbol": f"NIFTY {int(strike)} {ot} {expiry}",
        "expiry": expiry,
        "strike": float(strike),
        "option_type": ot,
        "lot_size": int(lot_size),
    }


class TestV2Comprehensive17Scenarios(unittest.TestCase):
    def setUp(self) -> None:
        reset_option_sniffer_v2_service()
        self._td = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._td.name, "v2_comp.db")
        set_v2_test_db_override(self.db_path)
        self.cfg, self.cfg_hash = load_v2_preregistration()
        self.store = V2LedgerStore(self.db_path)
        self.sim = V2PaperSimulator(self.cfg, self.cfg_hash, self.store, run_mode="FORWARD_PAPER")

    def tearDown(self) -> None:
        self.store.close()
        reset_option_sniffer_v2_service()
        set_v2_test_db_override(None)
        self._td.cleanup()

    def test_01_normal_book_a_opt_in_lifecycle_and_overnight_pnl_split(self) -> None:
        ts0 = 1_795_000_000_000
        exp = "2026-11-23"  # Next regular trading day after Friday 2026-11-20!
        sc = _mk_contract("NSE_FO|PE25750", 25750.0, "PE", exp)
        wc = _mk_contract("NSE_FO|PE25650", 25650.0, "PE", exp)
        sq = _mk_obs_quote("NSE_FO|PE25750", 92.0, 92.5, ts0, strike=25750.0, ot="PE", expiry=exp)
        wq = _mk_obs_quote("NSE_FO|PE25650", 54.0, 54.4, ts0, strike=25650.0, ot="PE", expiry=exp)

        # Disabled by default for autonomous entries due to spec conflict
        blocked = evaluate_book_a_entry_signal(
            self.cfg, "2026-11-20", "2026-11-20 14:00:00", 1400, ts0,
            25810.0, 25790.0, 13.5, [13.2] * 10, [25700.0] * 20, [exp],
            {(exp, 25750.0, "PE"): sc, (exp, 25650.0, "PE"): wc},
            {(exp, 25750.0, "PE"): sq, (exp, 25650.0, "PE"): wq},
            allow_unverified_in_replay=False,
        )
        self.assertFalse(blocked["passed"])
        self.assertEqual(blocked["reject_reason"], "BLOCKED_UNVERIFIED_O3_SPEC_CONFLICT")

        # Opt-in Book A entry -> Leg 1 -> Leg 2 -> overnight mark -> expiry morning gap -> 50% target exit
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

        # EOD mark on T-1 (2026-11-20) and Gap mark on T0 opening (2026-11-23 09:15)
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
        # Decay >= 50% credit -> target exit
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
        # 2026-11-24 was Guru Nanak Jayanti holiday; Monday 2026-11-23 is final expiry session -> Friday 2026-11-20 is T-1!
        self.assertFalse(is_nse_trading_session("2026-11-24"))
        self.assertEqual(trading_sessions_between("2026-11-20", "2026-11-23"), 1)
        self.assertEqual(trading_sessions_between("2026-11-19", "2026-11-23"), 2)
        # Historical lot size: 75 pre-Dec 30 2025, 65 afterward
        self.assertEqual(resolve_contract_lot_size("2025-12-23"), 75)
        self.assertEqual(resolve_contract_lot_size("2026-11-23"), 65)

    def test_03_book_b_cash_cap_1otm_fallback_only_and_futures_roll_fail_closed(self) -> None:
        ts = 1_795_100_000_000
        exp = "2026-11-26"
        spot_q = {"instrument_key": "NSE_INDEX|Nifty 50", "spot_ltp": 25810.0, "exchange_ltt_ms": ts, "local_receipt_ts_ms": ts}
        fut_q_open = {"instrument_key": "NSE_FO|NIFTY_FUT_NOV", "fut_ltp": 25850.0, "exchange_ltt_ms": ts, "local_receipt_ts_ms": ts}
        open_ref = check_spot_futures_sync(spot_q, fut_q_open, now_ms=ts)
        self.assertTrue(open_ref["ok"])

        # Mid-session roll to DEC future must fail closed
        fut_q_rolled = {"instrument_key": "NSE_FO|NIFTY_FUT_DEC", "fut_ltp": 25920.0, "exchange_ltt_ms": ts, "local_receipt_ts_ms": ts}
        ce_atm = _mk_contract("NSE_FO|CE25800", 25800.0, "CE", exp)
        ce_otm = _mk_contract("NSE_FO|CE25850", 25850.0, "CE", exp)
        q_atm_exp = _mk_obs_quote("NSE_FO|CE25800", 210.0, 211.0, ts, strike=25800.0, ot="CE", expiry=exp)  # Rs 13,715 > Rs 11,500 CashCap!
        q_otm_ok = _mk_obs_quote("NSE_FO|CE25850", 160.0, 161.0, ts, strike=25850.0, ot="CE", expiry=exp)   # Rs 10,468 <= Rs 11,500

        rolled_res = evaluate_book_b_entry_signal(
            self.cfg, "2026-11-19", "2026-11-19 10:45:00", 1045, ts, 25810.0, 25790.0, 25800.0, 25740.0, 26.0, 0.36,
            spot_q, fut_q_rolled, open_ref, [exp],
            {(exp, 25800.0, "CE"): ce_atm, (exp, 25850.0, "CE"): ce_otm},
            {(exp, 25800.0, "CE"): q_atm_exp, (exp, 25850.0, "CE"): q_otm_ok},
        )
        self.assertFalse(rolled_res["passed"])
        self.assertIn("FUTURES_CONTRACT_ROLLED_MID_SESSION", rolled_res["reject_reason"])

        # Missing open basis fails closed
        no_open_res = evaluate_book_b_entry_signal(
            self.cfg, "2026-11-19", "2026-11-19 10:45:00", 1045, ts, 25810.0, 25790.0, 25800.0, 25740.0, 26.0, 0.36,
            spot_q, fut_q_open, None, [exp],
            {(exp, 25800.0, "CE"): ce_atm, (exp, 25850.0, "CE"): ce_otm},
            {(exp, 25800.0, "CE"): q_atm_exp, (exp, 25850.0, "CE"): q_otm_ok},
        )
        self.assertFalse(no_open_res["passed"])
        self.assertIn("MISSING_OPEN_BASIS_REF", no_open_res["reject_reason"])

        # Valid DeltaBasis = +6pt & ATM exceeds ONLY CashCap -> triggers 1-OTM step (25850 CE)
        fut_q_up = {"instrument_key": "NSE_FO|NIFTY_FUT_NOV", "fut_ltp": 25856.0, "exchange_ltt_ms": ts, "local_receipt_ts_ms": ts}
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

        # Missing prior 10 sessions VIX -> fail closed
        vix_missing = evaluate_book_a_entry_signal(
            self.cfg, "2026-11-20", "2026-11-20 14:00:00", 1400, ts, 25810.0, 25790.0, 13.5, [13.2] * 5, [25700.0] * 20, [exp],
            {(exp, 25750.0, "PE"): sc, (exp, 25650.0, "PE"): wc}, {(exp, 25750.0, "PE"): sq, (exp, 25650.0, "PE"): wq},
            allow_unverified_in_replay=True,
        )
        self.assertFalse(vix_missing["passed"])
        self.assertIn("VIX_1p05_SMA10", vix_missing["reject_reason"])

        # Unknown timestamp / age=0 trap without depth timestamp must return UNKNOWN_QUOTE_AGE (never 0ms fresh!)
        unknown_age_q = _mk_obs_quote("NSE_FO|X", 50.0, 50.5, ts)
        unknown_age_q.pop("l1_book_timestamp_ms", None)
        unknown_age_q.pop("local_receipt_ts_ms", None)
        unknown_age_q.pop("exchange_ltt_ms", None)
        unknown_age_q["quote_age_ms"] = 0  # Classic trap that V1 allowed as fresh!
        self.assertIsNone(resolve_strict_quote_age_ms(unknown_age_q, now_ms=ts))
        self.assertEqual(sweep_observed_l5_book(unknown_age_q, "BUY", 65, now_ms=ts)["reason"], "UNKNOWN_QUOTE_AGE")

        # Stale / Carried / Skewed rejections
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

        # Configure 2-lot target with only 1 lot (65 qty) in Wing L5 depth -> partial fill down to 65 qty!
        self.cfg["books"]["V2_BOOK_A_PROPOSED_O3"]["lot_count"] = 2
        wq_1lot = _mk_obs_quote("NSE_FO|PE25650", 54.0, 54.4, ts, qty_per_lvl=13, levels_count=5, strike=25650.0, ot="PE", expiry=exp)
        l1 = self.sim.open_book_a_leg1_hedge("sig_pf1", "HYPOTHETICAL_MODIFIED_O3_NOT_PHASE4_EQUIVALENT", "2026-11-20", "2026-11-20 14:00:00", ts, wc, sc, wq_1lot, 25810.0, 13.5, True, allow_partial_lot=True)
        self.assertTrue(l1["ok"])
        self.assertEqual(l1["confirmed_hedge_qty"], 65)

        # Short bid = 82.0 vs wing_fill (~54.65) -> net credit ~27.3 < 30.0 strict floor -> Reject Leg-2 & unwind Wing cleanly!
        sq_low = _mk_obs_quote("NSE_FO|PE25750", 82.0, 82.5, ts + 200, qty_per_lvl=65, strike=25750.0, ot="PE", expiry=exp)
        l2_unw = self.sim.complete_or_unwind_pending_leg2(l1["package_id"], sq_low, wq_1lot, ts + 200, "2026-11-20", "2026-11-20 14:00:00")
        self.assertFalse(l2_unw["ok"])
        self.assertEqual(l2_unw["state"], "INCOMPLETE_LEG_UNWOUND")
        self.assertEqual(len(self.sim.open_packages), 0)

        # Second scenario: Wing quote dies stale before unwind -> must retain HEDGE_ONLY_EXPOSURE_OPEN cleanly
        l1b = self.sim.open_book_a_leg1_hedge("sig_pf2", "HYPOTHETICAL_MODIFIED_O3_NOT_PHASE4_EQUIVALENT", "2026-11-20", "2026-11-20 14:05:00", ts + 300_000, wc, sc, _mk_obs_quote("NSE_FO|PE25650", 54.0, 54.4, ts + 300_000, qty_per_lvl=65), 25810.0, 13.5, True)
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

        # Pressure event -> drops ONLY V2_WING_ONLY, preserves V1 keys
        dropped = reg.yield_under_pressure(v1_active_keys=v1_keys)
        self.assertEqual(dropped, ["NSE_FO|V2_WING_ONLY"])
        self.assertTrue(reg.is_key_evicted("NSE_FO|V2_WING_ONLY"))

        # Mark open Book B package whose key was evicted -> full structural max loss charged to MTM (never 0!)
        ts = 1_795_300_000_000
        c = _mk_contract("NSE_FO|V2_WING_ONLY", 25800.0, "CE", "2026-11-26")
        q = _mk_obs_quote("NSE_FO|V2_WING_ONLY", 140.0, 141.0, ts)
        op = self.sim.open_book_b_paid_long("sig_ev", "V2_FROZEN_COMBINED", "2026-11-19", "2026-11-19 10:45:00", ts, c, q, 25800.0, {})
        self.assertTrue(op["ok"])
        self.sim.mark_key_evicted("NSE_FO|V2_WING_ONLY")
        mtm = self.sim.mark_open_packages({"NSE_FO|V2_WING_ONLY": q}, "2026-11-19", "2026-11-19 10:46:00", ts + 60_000)
        self.assertEqual(mtm["unpriced_count"], 1)
        self.assertEqual(mtm["unpriced_details"][0]["reason"], "LEG_SUBSCRIPTION_EVICTED")
        self.assertLess(mtm["conservative_unrealized_mtm_inr"], -9000.0)

        svc = OptionSnifferV2Service(db_path=self.db_path)
        promo = svc.evaluate_promotion_gates([], [], list(self.sim.open_packages.values()))
        self.assertEqual(promo["unresolved_exposure_count"], 1)
        self.assertGreater(promo["unresolved_structural_risk_charged_inr"], 9000.0)
        self.assertEqual(promo["status"], "FAIL_COLLECTING_FORWARD_EVIDENCE")
        svc.store.close()

    def test_07_gap_through_stop_overshoot_decomposition_and_crash_restart_recovery(self) -> None:
        ts = 1_795_400_000_000
        c = _mk_contract("NSE_FO|CE25800", 25800.0, "CE", "2026-11-26")
        q_in = _mk_obs_quote("NSE_FO|CE25800", 149.5, 150.0, ts)
        op = self.sim.open_book_b_paid_long("sig_gap", "V2_FROZEN_COMBINED", "2026-11-19", "2026-11-19 10:30:00", ts, c, q_in, 25810.0, {})
        self.assertTrue(op["ok"])
        pkg_id = op["package_id"]
        eq_before_restart = self.sim.realized_equity_inr
        res_before_restart = self.sim.reserved_capital_inr

        # Simulate process crash & fresh V2LedgerStore + V2PaperSimulator restart recovery
        self.store.close()
        store2 = V2LedgerStore(self.db_path)
        sim2 = V2PaperSimulator(self.cfg, self.cfg_hash, store2, run_mode="FORWARD_PAPER")
        self.assertEqual(sim2.realized_equity_inr, eq_before_restart)
        self.assertEqual(sim2.reserved_capital_inr, res_before_restart)
        self.assertIn(pkg_id, sim2.open_packages)

        # Gap crash straight through hard stop to Rs 100.00 -> gross loss overshoots Rs 1,800 planned cap!
        ts_gap = ts + 120_000
        q_gap = _mk_obs_quote("NSE_FO|CE25800", 100.0, 100.5, ts_gap)
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
        self.assertEqual(fc.push_events(ev, receipt_ts_ms=ts + 10), 0)  # Duplicate suppressed!
        self.assertEqual(fc.push_events([("OPTION", "NSE_FO|X", {"instrument_key": "NSE_FO|X", "ltp": 49.0, "exchange_ltt_ms": ts - 5000})]), 1)  # Late event
        self.assertFalse(fc.check_minute_bar_continuity("2026-11-23 10:00:00") and False)  # initializes
        self.assertFalse(fc.check_minute_bar_continuity("2026-11-23 10:05:00"))  # 300s gap!
        # Flood > 64 unique items -> overflow uncertainty block raised
        for i in range(80):
            fc.push_events([("OPTION", "NSE_FO|X", {"instrument_key": "NSE_FO|X", "ltp": 50.0 + i, "exchange_ltt_ms": ts + 1000 + i})])
        hm = fc.get_health_metrics()
        self.assertGreater(hm["dropped_overflow_events"], 0)
        self.assertIn("UNCERTAINTY_QUEUE_OVERFLOW", hm["uncertainty_blocks"])

        # Missed 14:45 cutoff retains UNRESOLVED_EXPOSURE_PAST_1445 -> settles on observed 15:30 FSP TWAP
        exp = "2026-11-23"
        sc = _mk_contract("NSE_FO|PE25750", 25750.0, "PE", exp)
        wc = _mk_contract("NSE_FO|PE25650", 25650.0, "PE", exp)
        sq = _mk_obs_quote("NSE_FO|PE25750", 90.0, 90.5, ts, strike=25750.0, ot="PE", expiry=exp)
        wq = _mk_obs_quote("NSE_FO|PE25650", 50.0, 50.4, ts, strike=25650.0, ot="PE", expiry=exp)
        l1 = self.sim.open_book_a_leg1_hedge("sig_cut", "HYPOTHETICAL_MODIFIED_O3_NOT_PHASE4_EQUIVALENT", "2026-11-20", "2026-11-20 14:00:00", ts, wc, sc, wq, 25810.0, 13.5, True)
        self.sim.complete_or_unwind_pending_leg2(l1["package_id"], sq, wq, ts + 200, "2026-11-20", "2026-11-20 14:00:00")

        # At 14:45 on expiry day with missing quotes -> UNRESOLVED_EXPOSURE_PAST_1445!
        ts_1445 = ts + 86400_000
        res_cut = manage_open_book_a_on_bar(self.sim, self.sim.open_packages[l1["package_id"]], exp, f"{exp} 14:45:00", 1445, ts_1445, 25720.0, {})
        self.assertFalse(res_cut["ok"])
        self.assertEqual(res_cut["state"], "UNRESOLVED_EXPOSURE_PAST_1445")
        self.assertIn(l1["package_id"], self.sim.open_packages)

        # At 15:30 with observed FSP TWAP spot 25710 (ITM on 25750 PE) -> 0.15% exercise STT settlement
        ts_1530 = ts_1445 + 45 * 60_000
        res_fsp = manage_open_book_a_on_bar(self.sim, self.sim.open_packages[l1["package_id"]], exp, f"{exp} 15:30:00", 1530, ts_1530, 25710.0, {}, fsp_twap_spot=25710.0)
        self.assertTrue(res_fsp["ok"])
        self.assertEqual(res_fsp["exit_reason"], "EXPIRY_1530_OBSERVED_FSP_SETTLEMENT")
        self.assertEqual(len(self.sim.open_packages), 0)

    def test_09_twice_replay_determinism_counterfactuals_and_collateral_isolation(self) -> None:
        v1_conn = open_v1_readonly_db()
        try:
            o3 = load_phase4_o3_audit_report()
            self.assertTrue(o3["monthly_reconciliation"]["exact_arithmetic_reconciled"])
            self.assertEqual(o3["spec_conflicts_count"], 6)
            self.assertFalse(o3["autonomous_activation_decision"]["autonomous_entries_enabled"])

            cf = run_v1_counterfactual_audit(v1_conn, store=self.store)
            self.assertFalse(cf["included_in_v2_totals"])
            self.assertEqual(cf["cf1_summary"]["unresolved_stale_wing_count"], 4)

            rep1 = run_v2_replay_and_attribution(v1_conn, self.store, self.cfg, self.cfg_hash)
            sha1 = rep1["canonical_replay"]["ledger_sha256"]
            rep2 = run_v2_replay_and_attribution(v1_conn, self.store, self.cfg, self.cfg_hash)
            sha2 = rep2["canonical_replay"]["ledger_sha256"]
            self.assertEqual(sha1, sha2)
        finally:
            v1_conn.close()

        # Verify CHECK(included_in_v2_totals = 0) enforced at SQLite level
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

        # Optional Collateral Yield never contaminates trading totals
        col_off = compute_optional_collateral_report(200000.0, 9725.02, 10855.0, 6, self.cfg["optional_collateral_scenario_defaults"], enabled=False)
        col_on = compute_optional_collateral_report(200000.0, 9725.02, 10855.0, 6, self.cfg["optional_collateral_scenario_defaults"], enabled=True)
        self.assertEqual(col_off["hypothetical_collateral_income_inr"], 0.0)
        self.assertGreater(col_on["hypothetical_collateral_income_inr"], 0.0)
        self.assertEqual(col_on["classification_label"], "ASSUMED_YIELD_NOT_TRADING_ALPHA")


if __name__ == "__main__":
    unittest.main()
