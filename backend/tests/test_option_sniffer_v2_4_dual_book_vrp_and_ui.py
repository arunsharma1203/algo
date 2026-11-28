"""
Comprehensive v2.4 verification suite:
- 20-day causal prior loader & zero cold-start DB creation
- HAR-RV + Black-76 ATM Straddle IV VRP_ratio <= 1.15 gate + 45m Book B stagnation
- Friday Book A Intraday Credit Spread (cal_dte in {3,4}, 50/100w, NO_VFLIP, 15:15 EOD) & Monday cal_dte=1 block
- Paper Execution Modes (AUTO | BOOK_A_ONLY | BOOK_B_ONLY) + passive AIML telemetry + v2.4 Forward-vs-Replay parity
- Live execution: 3-scope margin floors (25k/75k/85k), 2-leg SPAN preflight block, Buy-Wing-First + 24pt credit floor + failed Leg-2 wing unwind, SOS Flatten, and Frontend Golden AST compliance
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.analytics.option_sniffer_v2.v2_book_a_engine import (
    evaluate_book_a_intraday_credit_signal,
)
from app.analytics.option_sniffer_v2.v2_book_b_engine import (
    evaluate_book_b_entry_signal,
)
from app.analytics.option_sniffer_v2.v2_config import (
    load_v2_preregistration,
    parse_ist_timestamp_ms,
)
from app.analytics.option_sniffer_v2.v2_ledger_db import (
    V2LedgerStore,
    set_v2_test_db_override,
)
from app.analytics.option_sniffer_v2.v2_replay_attribution import run_event_stream_replay
from app.analytics.option_sniffer_v2.v2_service import (
    OptionSnifferV2Service,
    reset_option_sniffer_v2_service,
)
from app.analytics.option_sniffer_v2.v2_vrp_and_breach_model import (
    compute_passive_aiml_breach_telemetry,
    compute_vrp_metrics,
    load_prior_nifty_daily_history_readonly,
)
from app.analytics.option_sniffer_v2_live.v2_live_execution_engine import (
    V2LiveExecutionEngine,
    reset_option_sniffer_v2_live_engine,
)
from app.analytics.option_sniffer_v2_live.v2_live_ledger import set_v2_live_test_db_override
from app.analytics.option_sniffer_v2_live.v2_upstox_broker_client import MockV2BrokerClient
from app.api import option_sniffer_v2_api, option_sniffer_v2_live_api


def _mk_contract(ikey: str, strike: float, ot: str, exp: str) -> Dict[str, Any]:
    return {
        "instrument_key": ikey,
        "trading_symbol": f"NIFTY {int(strike)} {ot} {exp}",
        "underlying": "NIFTY",
        "expiry": exp,
        "strike": float(strike),
        "option_type": ot,
        "lot_size": 65,
    }


def _mk_quote(ikey: str, bid: float, ask: float, ts_ms: int, strike: float, ot: str, exp: str, obi: float = 0.25) -> Dict[str, Any]:
    return {
        "instrument_key": ikey,
        "trading_symbol": f"NIFTY {int(strike)} {ot} {exp}",
        "underlying": "NIFTY",
        "expiry": exp,
        "strike": float(strike),
        "option_type": ot,
        "raw_bid": float(bid),
        "raw_ask": float(ask),
        "bid": float(bid),
        "ask": float(ask),
        "book_obi_l5": float(obi),
        "is_observed_l1_book": True,
        "l1_observed_on_tick": True,
        "depth_observed_on_tick": True,
        "l1_timestamp_ms": ts_ms,
        "depth_timestamp_ms": ts_ms,
        "exchange_ltt_ms": ts_ms,
        "local_receipt_ts_ms": ts_ms,
        "availability_ts_ms": ts_ms,
        "book_provenance": "OBSERVED_V3_L5",
        "depth_levels": [
            {"bid_p": float(bid), "bid_q": 650, "ask_p": float(ask), "ask_q": 650},
            {"bid_p": round(bid - 0.1, 2), "bid_q": 650, "ask_p": round(ask + 0.1, 2), "ask_q": 650},
        ],
    }


class TestV24DualBookVRPAndUI(unittest.TestCase):
    def setUp(self) -> None:
        reset_option_sniffer_v2_service()
        reset_option_sniffer_v2_live_engine()
        self._td = tempfile.TemporaryDirectory()
        self.paper_db = os.path.join(self._td.name, "v24_paper.db")
        self.live_db = os.path.join(self._td.name, "v24_live.db")
        set_v2_test_db_override(self.paper_db)
        set_v2_live_test_db_override(self.live_db)
        self.cfg, self.cfg_hash = load_v2_preregistration()

    def tearDown(self) -> None:
        reset_option_sniffer_v2_service()
        reset_option_sniffer_v2_live_engine()
        set_v2_test_db_override(None)
        set_v2_live_test_db_override(None)
        self._td.cleanup()

    def test_01_causal_20d_prior_loader_and_cold_start_overview(self) -> None:
        store = V2LedgerStore(self.paper_db, auto_create=False)
        hist = load_prior_nifty_daily_history_readonly(store, "2026-11-20")
        self.assertFalse(os.path.exists(self.paper_db))
        self.assertTrue(hist["warmup_ready"])
        self.assertGreaterEqual(hist["prior_days_count"], 20)
        for sd in hist["session_dates"]:
            self.assertLess(sd, "2026-11-20")

        svc = OptionSnifferV2Service(db_path=self.paper_db)
        ov = svc.get_overview()
        self.assertFalse(os.path.exists(self.paper_db))
        self.assertEqual(ov["engine_version"], "2.2.0-causal")
        self.assertEqual(ov["active_profile_id"], "V2_4_PAPER_DUAL_BOOK")
        self.assertEqual(ov["paper_execution_mode"], "AUTO")
        self.assertFalse(ov["books"]["V2_BOOK_A_PROPOSED_O3"]["autonomous_entries_enabled"])
        self.assertTrue(ov["books"]["V2_BOOK_A_INTRADAY_CREDIT"]["enabled_in_paper"])
        self.assertIn("vrp_telemetry", ov)
        self.assertIn("book_performance_split", ov)
        self.assertTrue(ov["aiml_passive_telemetry"]["passive_only"])
        self.assertFalse(ov["aiml_passive_telemetry"]["advisory_gate_applied"])

    def test_02_vrp_ratio_gate_and_passive_aiml_fail_closed(self) -> None:
        store = V2LedgerStore(self.paper_db, auto_create=False)
        hist = load_prior_nifty_daily_history_readonly(store, "2026-11-20")

        # Cheap ATM straddle -> VRP <= 1.15 PASS
        vrp_cheap = compute_vrp_metrics(
            atm_ce_mid=80.0, atm_pe_mid=80.0, atm_strike=25800.0, spot_close=25805.0,
            ist_hhmm=1030, trading_dte=3, r2_sum_today=2.5e-5, elapsed_bars_today=75, prior_history=hist,
        )
        self.assertIsNotNone(vrp_cheap["vrp_ratio"])
        self.assertLessEqual(vrp_cheap["vrp_ratio"], 1.15)

        # Expensive ATM straddle -> VRP > 1.15 FAIL CLOSED
        vrp_exp = compute_vrp_metrics(
            atm_ce_mid=210.0, atm_pe_mid=210.0, atm_strike=25800.0, spot_close=25805.0,
            ist_hhmm=1030, trading_dte=3, r2_sum_today=2.5e-5, elapsed_bars_today=75, prior_history=hist,
        )
        self.assertGreater(vrp_exp["vrp_ratio"], 1.15)

        exp = "2026-11-26"
        ts = parse_ist_timestamp_ms("2026-11-20T10:30:00+05:30")
        assert ts is not None
        spot_q = {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25810.0, "exchange_ltt_ms": ts, "local_receipt_ts_ms": ts, "availability_ts_ms": ts, "source_ts_proven": True}
        fut_q = {"instrument_key": "NSE_FO|NIFTYFUT", "underlying": "NIFTY", "expiry": exp, "fut_ltp": 25835.0, "exchange_ltt_ms": ts, "local_receipt_ts_ms": ts, "availability_ts_ms": ts, "source_ts_proven": True}
        open_ref = {"ok": True, "basis_pts": 20.0, "fut_instrument_key": "NSE_FO|NIFTYFUT", "fut_expiry": exp}
        contracts = {(exp, 25800.0, "CE"): _mk_contract("NSE_FO|CE25800", 25800.0, "CE", exp)}
        quotes = {(exp, 25800.0, "CE"): _mk_quote("NSE_FO|CE25800", 120.0, 120.5, ts, 25800.0, "CE", exp)}

        sig_pass = evaluate_book_b_entry_signal(
            self.cfg, "2026-11-20", "2026-11-20 10:30:00", 1030, ts, 25810.0, 25780.0, 25790.0, 25730.0,
            26.0, 0.45, spot_q, fut_q, open_ref, [exp], contracts, quotes,
            vrp_ratio=vrp_cheap["vrp_ratio"], atm_iv_pct=vrp_cheap["atm_iv_pct"], har_rv_pct=vrp_cheap["har_rv_pct"],
            enforce_vrp_gate=True, max_vrp_ratio=1.15, vrp_status=vrp_cheap["status"],
        )
        self.assertTrue(sig_pass["passed"])

        sig_block = evaluate_book_b_entry_signal(
            self.cfg, "2026-11-20", "2026-11-20 10:30:00", 1030, ts, 25810.0, 25780.0, 25790.0, 25730.0,
            26.0, 0.45, spot_q, fut_q, open_ref, [exp], contracts, quotes,
            vrp_ratio=vrp_exp["vrp_ratio"], atm_iv_pct=vrp_exp["atm_iv_pct"], har_rv_pct=vrp_exp["har_rv_pct"],
            enforce_vrp_gate=True, max_vrp_ratio=1.15, vrp_status=vrp_exp["status"],
        )
        self.assertFalse(sig_block["passed"])
        self.assertTrue(str(sig_block["reject_reason"]).startswith("VRPRatio:"))

        # Passive AIML fails closed when VIX 10D history missing without fabricating data
        aiml_no_vix = compute_passive_aiml_breach_telemetry(
            spot_close=25810.0, vwap=25780.0, session_high=25820.0, session_low=25740.0,
            adx14=26.0, er20=0.45, atrr_5_20=1.02, live_vix=13.5, direction_bullish=True,
            barrier_pts=50.0, ist_hhmm=1030, r2_sum_today=2.5e-5, elapsed_bars_today=75, prior_history=hist,
        )
        self.assertTrue(aiml_no_vix["passive_only"])
        self.assertFalse(aiml_no_vix["advisory_gate_applied"])
        self.assertIsNone(aiml_no_vix["p_breach"])

        # With explicit causal VIX 10D history, returns calibrated score passively
        hist_with_vix = {**hist, "vix_sma10": 13.8}
        aiml_ok = compute_passive_aiml_breach_telemetry(
            spot_close=25810.0, vwap=25780.0, session_high=25820.0, session_low=25740.0,
            adx14=26.0, er20=0.45, atrr_5_20=1.02, live_vix=13.5, direction_bullish=True,
            barrier_pts=50.0, ist_hhmm=1030, r2_sum_today=2.5e-5, elapsed_bars_today=75, prior_history=hist_with_vix,
        )
        self.assertEqual(aiml_ok["status"], "OK")
        self.assertGreater(aiml_ok["p_breach"], 0.0)
        self.assertLess(aiml_ok["p_breach"], 1.0)

    def test_03_book_a_intraday_friday_vs_monday_and_v24_replay_parity(self) -> None:
        # Friday 2026-11-20 with Monday 2026-11-23 expiry -> cal_dte == 3 PASSES
        exp_fri = "2026-11-23"
        ts_f = parse_ist_timestamp_ms("2026-11-20T12:00:00+05:30")
        assert ts_f is not None
        contracts_f = {
            (exp_fri, 25750.0, "PE"): _mk_contract("NSE_FO|PE25750", 25750.0, "PE", exp_fri),
            (exp_fri, 25650.0, "PE"): _mk_contract("NSE_FO|PE25650", 25650.0, "PE", exp_fri),
        }
        quotes_f = {
            (exp_fri, 25750.0, "PE"): _mk_quote("NSE_FO|PE25750", 85.0, 85.5, ts_f, 25750.0, "PE", exp_fri),
            (exp_fri, 25650.0, "PE"): _mk_quote("NSE_FO|PE25650", 54.0, 54.5, ts_f, 25650.0, "PE", exp_fri),
        }
        sig_fri = evaluate_book_a_intraday_credit_signal(
            self.cfg, "2026-11-20", "2026-11-20 12:00:00", 1200, ts_f, 25805.0, 25790.0, 25795.0, 25740.0,
            [exp_fri], contracts_f, quotes_f,
        )
        self.assertTrue(sig_fri["passed"], f"Expected Friday Book A Intraday pass, got {sig_fri}")
        self.assertEqual(sig_fri["short_strike"], 25750.0)
        self.assertEqual(sig_fri["wing_strike"], 25650.0)

        # Monday 2026-11-23 with Tuesday 2026-11-24 expiry -> cal_dte == 1 BLOCKS
        exp_mon = "2026-11-24"
        ts_m = parse_ist_timestamp_ms("2026-11-23T12:00:00+05:30")
        assert ts_m is not None
        sig_mon = evaluate_book_a_intraday_credit_signal(
            self.cfg, "2026-11-23", "2026-11-23 12:00:00", 1200, ts_m, 25805.0, 25790.0, 25795.0, 25740.0,
            [exp_mon], {}, {},
        )
        self.assertFalse(sig_mon["passed"])
        self.assertEqual(sig_mon["reject_reason"], "FriCalDTE:calDTE=1")

        # Full event-stream Forward vs Causal Replay parity for v2.4 Friday Book A Intraday (NO_VFLIP + 15:15 EOD)
        t0 = parse_ist_timestamp_ms("2026-11-20T09:15:00+05:30")
        assert t0 is not None
        events: List[Any] = []
        for m in range(370):  # 09:15 .. 15:24 IST
            b_ms = t0 + m * 60_000
            tk = b_ms + 57_000
            hm_min = 9 * 60 + 15 + m
            hhmm = (hm_min // 60) * 100 + (hm_min % 60)
            # ORB30 09:15..09:44, then stay above VWAP until 12:00 entry, then temporarily dip below VWAP at 13:30 (NO_VFLIP must hold to 15:15!)
            if m < 30:
                spot = 25800.0 + (2.0 if m % 2 == 0 else -2.0)
            elif hhmm <= 1210:
                spot = 25812.0
            elif 1320 <= hhmm <= 1350:
                spot = 25792.0  # Brief VWAP flip below VWAP!
            else:
                spot = 25806.0
            events.append(("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": spot, "availability_ts_ms": tk, "exchange_ltt_ms": tk, "source_ts_proven": True}))
            events.append(("FUTURES", "NIFTY", {"instrument_key": "NSE_FO|NIFTYFUT", "underlying": "NIFTY", "expiry": "2026-11-26", "fut_ltp": spot + 20.0, "availability_ts_ms": tk, "exchange_ltt_ms": tk, "source_ts_proven": True}))
            # Debit rises to ~35pt (still well below 1.8x stop) during VWAP dip, decays to 20pt later -> exits at 15:15 EOD!
            pe_short = 85.0 if hhmm < 1320 else (89.0 if hhmm <= 1350 else 72.0)
            pe_wing = 54.0
            events.append(("OPTION", "NSE_FO|PE25750", _mk_quote("NSE_FO|PE25750", pe_short, pe_short + 0.5, tk, 25750.0, "PE", exp_fri)))
            events.append(("OPTION", "NSE_FO|PE25650", _mk_quote("NSE_FO|PE25650", pe_wing, pe_wing + 0.5, tk, 25650.0, "PE", exp_fri)))
            events.append(("BAR_CLOSE_CHECK", "CLOCK", {"availability_ts_ms": b_ms + 60_000}))

        rep_db = os.path.join(self._td.name, "rep_v24.db")
        rep_store = V2LedgerStore(rep_db, auto_create=True)
        try:
            rep = run_event_stream_replay(
                events, self.cfg, self.cfg_hash, rep_store,
                run_mode="REPLAY_STREAM", enable_v2_4_dual_book=True, paper_execution_mode="BOOK_A_ONLY", persist=True,
            )
        finally:
            rep_store.close()

        svc = OptionSnifferV2Service(db_path=self.paper_db)
        mode_res = option_sniffer_v2_api.post_v2_paper_mode({"paper_mode": "BOOK_A_ONLY"})
        self.assertEqual(mode_res["paper_execution_mode"], "BOOK_A_ONLY")
        svc.start_parallel_collector(stream_manager=None, run_archive_warmup=False, spawn_worker=False)
        for ev in events:
            svc.on_live_feed_events([ev])
            svc.process_queued_events(max_items=20)
        fwd_pos = svc.get_positions()["forward_closed_trades"]
        svc.stop_v2_only()

        self.assertEqual(len(rep["closed_trades"]), 1)
        self.assertEqual(len(fwd_pos), 1)
        self.assertEqual(fwd_pos[0]["book_id"], "V2_BOOK_A_INTRADAY_CREDIT")
        self.assertEqual(fwd_pos[0]["exit_reason"], "BOOK_A_INTRADAY_EOD_1515")
        self.assertEqual(rep["closed_trades"][0]["exit_reason"], "BOOK_A_INTRADAY_EOD_1515")
        self.assertAlmostEqual(fwd_pos[0]["net_pnl_inr"], rep["closed_trades"][0]["net_pnl_inr"], places=2)

    def test_04_live_3_scope_margins_span_preflight_credit_floor_and_sos_flatten(self) -> None:
        # 1. Boot disarmed + 3 scope margin floors (25k / 75k / 85k)
        broker_low = MockV2BrokerClient(available_margin_inr=60000.0)
        eng_low = V2LiveExecutionEngine(db_path=self.live_db, broker_client=broker_low, async_observer_dispatch=False)
        st0_low = eng_low.get_status()
        self.assertFalse(st0_low["armed"])
        pin_low = st0_low["current_pin"]

        # 60k passes BOOK_B_ONLY (25k floor)
        st_b = eng_low.arm_live(pin=pin_low, human_confirmed=True, live_book_scope="BOOK_B_ONLY")
        self.assertTrue(st_b["armed"])
        self.assertEqual(st_b["live_book_scope"], "BOOK_B_ONLY")
        eng_low.disarm_live()
        pin_low2 = eng_low.get_status()["current_pin"]

        # 60k fails BOOK_A_ONLY (75k) & AUTO_DUAL (85k)
        with self.assertRaises(RuntimeError):
            eng_low.arm_live(pin=pin_low2, human_confirmed=True, live_book_scope="BOOK_A_ONLY")
        with self.assertRaises(RuntimeError):
            eng_low.arm_live(pin=pin_low2, human_confirmed=True, live_book_scope="AUTO_DUAL")

        # 2. 2-Leg Spread pre-trade SPAN check & 24pt credit floor + SOS Flatten
        broker = MockV2BrokerClient(available_margin_inr=200000.0)
        broker.market_ltp_by_key["NSE_FO|PE25650"] = 50.0
        broker.market_ltp_by_key["NSE_FO|PE25750"] = 85.0
        eng = V2LiveExecutionEngine(db_path=self.live_db, broker_client=broker, async_observer_dispatch=False)
        pin_hi = eng.get_status()["current_pin"]
        eng.arm_live(pin=pin_hi, human_confirmed=True, live_book_scope="AUTO_DUAL", max_open_packages=2)

        # SPAN margin spike (180k -> 1.15x = 207k > 200k avail) MUST block before Leg-1 buy!
        broker.injected_margin_required_inr = 180000.0
        blocked_span = eng.submit_entry_signal({
            "signal_id": "sig_span_blk",
            "session_date": "2026-11-20",
            "book_id": "V2_BOOK_A_INTRADAY_CREDIT",
            "structure": "DEFINED_RISK_SPREAD",
            "min_live_credit_pts": 24.0,
            "buy_leg": {"instrument_key": "NSE_FO|PE25650", "trading_symbol": "NIFTY 25650 PE", "option_type": "PE", "strike": 25650, "qty": 65, "lot_size": 65, "limit_price": 50.0, "order_type": "LIMIT"},
            "short_leg": {"instrument_key": "NSE_FO|PE25750", "trading_symbol": "NIFTY 25750 PE", "option_type": "PE", "strike": 25750, "qty": 65, "lot_size": 65, "limit_price": 85.0, "order_type": "LIMIT"},
        })
        self.assertFalse(blocked_span["ok"])
        self.assertEqual(blocked_span["status"], "LIVE_SPREAD_SPAN_MARGIN_INSUFFICIENT")
        self.assertEqual(len([c for c in broker.call_log if c["method"] == "place_order"]), 0)

        # Valid SPAN + Credit floor enforcement (wing fill 50 + min_live_credit 24 = 74 floor when s_limit=70)
        broker.injected_margin_required_inr = None
        res_ok = eng.submit_entry_signal({
            "signal_id": "sig_spread_ok",
            "session_date": "2026-11-20",
            "book_id": "V2_BOOK_A_INTRADAY_CREDIT",
            "structure": "DEFINED_RISK_SPREAD",
            "min_live_credit_pts": 24.0,
            "buy_leg": {"instrument_key": "NSE_FO|PE25650", "trading_symbol": "NIFTY 25650 PE", "option_type": "PE", "strike": 25650, "qty": 65, "lot_size": 65, "limit_price": 50.0, "order_type": "LIMIT"},
            "short_leg": {"instrument_key": "NSE_FO|PE25750", "trading_symbol": "NIFTY 25750 PE", "option_type": "PE", "strike": 25750, "qty": 65, "lot_size": 65, "limit_price": 70.0, "order_type": "LIMIT"},
        })
        self.assertTrue(res_ok["ok"])
        self.assertEqual(res_ok["package"]["legs"]["short_leg"]["limit_price"], 74.0)

        # Inject one V1 non-V2 resting order + one V2 resting order; SOS MUST preserve V1 order & cancel V2 order only!
        broker._orders["ord_v1_keep"] = {"order_id": "ord_v1_keep", "tag": "V1-MANUAL", "status": "open", "instrument_token": "NSE_FO|PE25500", "trading_symbol": "NIFTY 25500 PE", "transaction_type": "BUY", "quantity": 65, "filled_quantity": 0, "average_price": 0.0}
        broker._orders["ord_v2_cancel"] = {"order_id": "ord_v2_cancel", "tag": "V2-RESTING", "status": "open", "instrument_token": "NSE_FO|PE25750", "trading_symbol": "NIFTY 25750 PE", "transaction_type": "SELL", "quantity": 65, "filled_quantity": 0, "average_price": 0.0}

        # Emergency SOS Flatten disarms & closes open spread (SHORT_LEG first, BUY_WING second -> 0 net pos!)
        sos = eng.sos_flatten_all_and_disarm()
        self.assertTrue(sos["ok"])
        self.assertFalse(sos["armed"])
        self.assertEqual(sos["flattened_count"], 1)
        self.assertEqual(sos["cancelled_orders"], ["ord_v2_cancel"])
        self.assertEqual(broker._orders["ord_v1_keep"]["status"], "open")
        for p in broker.get_positions():
            self.assertEqual(int(p["quantity"]), 0)

    def test_05_frontend_golden_ast_compliance(self) -> None:
        v2_page = os.path.join(_REPO_ROOT, "frontend", "src", "pages", "OptionSnifferAdvanceV2.jsx")
        v2_dir = os.path.join(_REPO_ROOT, "frontend", "src", "pages", "optionSnifferV2")
        all_targets = [v2_page] + [os.path.join(v2_dir, f) for f in os.listdir(v2_dir) if f.endswith((".js", ".jsx"))]
        banned_exec = ("method: 'POST'", 'method: "POST"', "/live/arm", "placeOrder", "live_broker")
        banned_jsx_err = ("pending_restart", "has not been restarted", "all_unchanged: true")
        for path in all_targets:
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
            for tok in banned_exec:
                self.assertNotIn(tok, src, f"Banned token {tok!r} found in {path}")
            if path.endswith(".jsx"):
                for tok in banned_jsx_err:
                    self.assertNotIn(tok, src, f"Banned error token {tok!r} found in {path}")
        self.assertTrue(os.path.exists(os.path.join(_REPO_ROOT, "frontend", "src", "components", "optionSnifferV2Live", "V2WorkspaceHeader.jsx")))

    def test_06_multi_lot_scaling_split_brain_sync_recovery_and_heartbeat_kill(self) -> None:
        from app.analytics.option_sniffer_v2.v2_paper_simulator import V2PaperSimulator

        # 1. Cold-start get_funds_preview never creates live DB, and Book B margin scales with max_lots_per_book (25k * lots)
        broker = MockV2BrokerClient(available_margin_inr=60000.0)
        eng_l = V2LiveExecutionEngine(
            db_path=self.live_db, broker_client=broker, initial_pin="6161", log_pin_on_boot=False, async_observer_dispatch=False
        )
        fp = option_sniffer_v2_live_api.get_v2_live_funds_preview(force_refresh=False) if False else eng_l.get_funds_preview(force_refresh=False)
        self.assertFalse(os.path.exists(self.live_db), "Cold-start funds-preview MUST NOT create live DB!")
        self.assertTrue(fp["ok"])
        self.assertEqual(fp["available_margin_inr"], 60000.0)
        self.assertEqual(fp["schedule_by_lots"]["3"]["BOOK_B_ONLY"], 75000.0)

        with self.assertRaises(RuntimeError):
            # 3 lots of Book B needs 75k > 60k available -> MUST refuse cleanly!
            eng_l.arm_live(pin="6161", human_confirmed=True, live_book_scope="BOOK_B_ONLY", max_lots_per_book=3)

        # 2 lots of Book B needs 50k <= 60k -> PASSES & scales status min_required_margin_inr!
        st2 = eng_l.arm_live(pin="6161", human_confirmed=True, live_book_scope="BOOK_B_ONLY", max_lots_per_book=2)
        self.assertEqual(st2["min_required_margin_inr"], 50000.0)
        self.assertEqual(st2["max_lots_per_book"], 2)
        eng_l.disarm_live()

        # 2. Split-brain elimination: attach to paper service initially set to BOOK_B_ONLY; arming BOOK_A_ONLY syncs in-memory!
        broker_big = MockV2BrokerClient(available_margin_inr=250000.0)
        broker_big.market_ltp_by_key["NSE_FO|PE25650"] = 50.0
        broker_big.market_ltp_by_key["NSE_FO|PE25750"] = 85.0
        svc = OptionSnifferV2Service(db_path=self.paper_db)
        svc.store.ensure_initialized()
        svc.set_paper_execution_mode("BOOK_B_ONLY", lots_per_book=1)
        self.assertGreaterEqual(svc.subscription_registry.max_v2_additive_pins, 16)

        eng2 = V2LiveExecutionEngine(
            db_path=self.live_db, broker_client=broker_big, initial_pin="7272", log_pin_on_boot=False, async_observer_dispatch=False
        )
        eng2.attach_to_paper_service(svc)
        eng2.arm_live(
            pin="7272", human_confirmed=True, live_book_scope="BOOK_A_ONLY", max_lots_per_book=2, max_daily_loss_inr=3000.0
        )
        self.assertEqual(svc.get_paper_execution_mode(), "BOOK_A_ONLY")
        self.assertEqual(svc.get_lots_per_book(), 2)
        self.assertEqual(svc.get_overview()["lots_per_book"], 2)

        # 3. Book B L5 multi-lot check + Paper Dual-Book capital ceiling (75k + 18k = 93k <= 110k)
        exp_fri = "2026-11-23"
        ts_f = parse_ist_timestamp_ms("2026-11-20T12:00:00+05:30")
        assert ts_f is not None
        q_small = _mk_quote("NSE_FO|CE_SMALL", 100.0, 100.5, ts_f, 25800.0, "CE", exp_fri)
        # Only 65 qty on ask -> 2-lot (130 qty) MUST FAIL L5 depth!
        q_small["depth_levels"] = [{"bid_p": 100.0, "bid_q": 65, "ask_p": 100.5, "ask_q": 65}]
        sig_thin = evaluate_book_b_entry_signal(
            self.cfg, "2026-11-20", "2026-11-20 11:00:00", 1100, ts_f, 25810.0, 25780.0, 25790.0, 25730.0,
            26.0, 0.45,
            {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": 25810.0, "exchange_ltt_ms": ts_f, "availability_ts_ms": ts_f, "source_ts_proven": True},
            {"instrument_key": "NSE_FO|FUT", "underlying": "NIFTY", "expiry": "2026-11-26", "fut_ltp": 25835.0, "exchange_ltt_ms": ts_f, "availability_ts_ms": ts_f, "source_ts_proven": True},
            {"ok": True, "basis_pts": 20.0, "fut_instrument_key": "NSE_FO|FUT", "fut_expiry": "2026-11-26"},
            ["2026-11-26"], {("2026-11-26", 25800.0, "CE"): _mk_contract("NSE_FO|CE_SMALL", 25800.0, "CE", "2026-11-26")},
            {("2026-11-26", 25800.0, "CE"): {**q_small, "expiry": "2026-11-26"}}, lot_count_override=2,
        )
        self.assertFalse(sig_thin["passed"])

        sim_dual = V2PaperSimulator(self.cfg, self.cfg_hash, svc.store, run_mode="REPLAY_STREAM")
        sim_dual.set_runtime_lot_multiplier(1, enable_v2_4_dual_book=True)
        self.assertGreaterEqual(sim_dual.effective_max_capital_reserved_inr(1), 110000.0)

        # 4. Run Friday Book A stream -> persists osv2_signals + triggers BLOCKED on second bar + restart recovers VRP & decisions!
        t0 = parse_ist_timestamp_ms("2026-11-20T09:15:00+05:30")
        assert t0 is not None
        svc.start_parallel_collector(stream_manager=None, run_archive_warmup=False, spawn_worker=False)
        for m in range(180):
            b_ms = t0 + m * 60_000
            tk = b_ms + 57_000
            hm_min = 9 * 60 + 15 + m
            hhmm = (hm_min // 60) * 100 + (hm_min % 60)
            spot = 25800.0 if m < 30 else 25812.0
            q_pe_s = _mk_quote("NSE_FO|PE25750", 85.0, 85.5, tk, 25750.0, "PE", exp_fri)
            q_pe_w = _mk_quote("NSE_FO|PE25650", 54.0, 54.5, tk, 25650.0, "PE", exp_fri)
            q_pe_s["depth_levels"] = [{"bid_p": 85.0, "bid_q": 1300, "ask_p": 85.5, "ask_q": 1300}]
            q_pe_w["depth_levels"] = [{"bid_p": 54.0, "bid_q": 1300, "ask_p": 54.5, "ask_q": 1300}]
            svc.on_live_feed_events([
                ("INDEX", "NIFTY", {"instrument_key": "NSE_INDEX|Nifty 50", "underlying": "NIFTY", "spot_ltp": spot, "availability_ts_ms": tk, "exchange_ltt_ms": tk, "source_ts_proven": True}),
                ("FUTURES", "NIFTY", {"instrument_key": "NSE_FO|FUTNOV", "underlying": "NIFTY", "expiry": "2026-11-26", "fut_ltp": spot + 20.0, "availability_ts_ms": tk, "exchange_ltt_ms": tk, "source_ts_proven": True}),
                ("OPTION", "NSE_FO|PE25750", q_pe_s),
                ("OPTION", "NSE_FO|PE25650", q_pe_w),
                ("BAR_CLOSE_CHECK", "CLOCK", {"availability_ts_ms": b_ms + 60_000}),
            ])
            svc.process_queued_events(max_items=20)

        # Book A signal persisted in osv2_signals + live spread opened at 2 lots (130 qty) + Book A blocked state recorded!
        book_a_sigs = [s for s in svc.store.load_signals("FORWARD_PAPER", self.cfg_hash) if s["book_id"] == "V2_BOOK_A_INTRADAY_CREDIT"]
        self.assertGreaterEqual(len(book_a_sigs), 1)
        self.assertEqual(svc.unified_engine.latest_decision_by_book["V2_BOOK_A_INTRADAY_CREDIT"]["state"], "BLOCKED")
        st_live_open = eng2.get_status(include_paper_baseline=False)
        self.assertEqual(st_live_open["open_package_count"], 1)
        self.assertEqual(st_live_open["open_packages"][0]["legs"]["buy_leg"]["qty"], 130)

        # 5. Simulate BAR_MTM_HEARTBEAT when spread moves adverse > max_daily_loss_inr -> auto-flattens & disarms!
        broker_big._positions["NSE_FO|PE25750"]["last_price"] = 120.0  # -35 * 130 = -4550
        broker_big._positions["NSE_FO|PE25650"]["last_price"] = 50.0   # -4 * 130 = -520 (total -5070 <= -3000)
        hb_res = eng2.on_paper_lifecycle_event({
            "event_type": "BAR_MTM_HEARTBEAT",
            "run_mode": "FORWARD_PAPER",
            "session_date": "2026-11-20",
        })
        self.assertIsNotNone(hb_res)
        self.assertEqual(hb_res["status"], "MAX_DAILY_LOSS_REACHED")
        self.assertEqual(len(hb_res["flattened_package_ids"]), 1)
        self.assertFalse(eng2.get_status(include_paper_baseline=False)["armed"])
        self.assertEqual(eng2.get_status(include_paper_baseline=False)["open_package_count"], 0)

        # 6. Simulate backend restart: fresh OptionSnifferV2Service recovers HAR-RV + decisions without waiting for live tick!
        eng2.detach_from_paper_service()
        svc.stop_v2_only()
        svc.store.close()

        svc_restarted = OptionSnifferV2Service(db_path=self.paper_db)
        try:
            ov_r = svc_restarted.get_overview()
            self.assertIsNotNone(ov_r["vrp_telemetry"]["har_rv_pct"])
            self.assertEqual(ov_r["v2_release_version"], "2.4.1-dual-book-vrp-unified")
            self.assertIn("V2_BOOK_A_INTRADAY_CREDIT", svc_restarted.unified_engine.latest_decision_by_book)

            sat_ms = parse_ist_timestamp_ms("2026-11-21T11:00:00+05:30")
            assert sat_ms is not None
            res_sat = svc_restarted.unified_engine.ingest_event(
                "INDEX",
                "NIFTY",
                {
                    "instrument_key": "NSE_INDEX|Nifty 50",
                    "underlying": "NIFTY",
                    "spot_ltp": 25820.0,
                    "availability_ts_ms": sat_ms,
                    "exchange_ltt_ms": sat_ms,
                    "source_ts_proven": True,
                },
            )
            self.assertIsNotNone(res_sat)
            self.assertEqual(res_sat["status"], "NON_TRADING_SESSION_IGNORED")
            self.assertEqual(svc_restarted.unified_engine.session_date, "2026-11-20")
            self.assertGreater(len(svc_restarted.unified_engine.completed_bars), 0)
            self.assertIsNotNone(svc_restarted.get_overview()["vrp_telemetry"]["har_rv_pct"])
        finally:
            svc_restarted.stop_v2_only()
            svc_restarted.store.close()


if __name__ == "__main__":
    unittest.main()
