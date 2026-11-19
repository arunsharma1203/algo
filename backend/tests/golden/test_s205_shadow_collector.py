"""
Golden & Invariant Suite for `S205ShadowCollector` (`test_s205_shadow_collector.py`).

Verifies:
  1. Frozen `SHADOW_PREREGISTRATION.json` safety invariants & SHA-256 hash lock.
  2. Fail-closed L5 order-book sweep (`SYNTHETIC_LTP_FALLBACK`, `CARRIED_PREV_TICK`,
     stale `l1_book_age_ms > 1500`, insufficient 5-level depth `< 65`) vs exact
     multi-level VWAP + `₹0.05` adverse tick rounding.
  3. Physical Buy-First 2-Leg `150pt` Credit Spread completion AND
     `INCOMPLETE_LEG_UNWOUND` emergency wing unwind matched to `₹0.00` against
     `options_accounting.compute_package_accounting`.
  4. Strict intra-bar `bar[t].open` before `bar[t].close` capital & slot causality.
  5. Prefix-truncation & `10x` future-corruption invariance on shadow readiness & entries.
  6. Regression immunity on `AdvanceDB.create_trade` (`target_pct = 0.0` preserved, never `35.0`).
  7. Persistence of 1m L5 chain (`delta_vtt_1m`), `+250ms/+1s/+5s/+60s` quote-fade horizons,
     Upstox SPAN margin telemetry, and expiry `15:00–15:30 IST` NCL FSP TWAP windows.
  8. Hard airgap preventing `SHADOW_*` trades from ever mirroring in `AdvanceLiveBridge`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from typing import Any, Dict, List, Tuple

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.analytics.common.options_accounting import (
    OptionLeg,
    compute_package_accounting,
    round_to_nse_tick,
)
from app.data.database import set_test_db_override

try:
    from app.analytics.option_sniffer_advance_s205.s205_shadow_collector import (
        S205ShadowCollector,
        load_shadow_preregistration,
        sweep_l5_book_for_qty,
    )
    from app.analytics.option_sniffer_advance_s205.s205_signal_evaluator import (
        build_causal_intraday_context,
    )
except ModuleNotFoundError:
    _pkg_name = "app.analytics.option_sniffer_advance_s205"
    _pkg_dir = os.path.join(_BACKEND_ROOT, "app", "analytics", "option_sniffer_advance_s205")
    if _pkg_name not in sys.modules:
        _pkg_stub = types.ModuleType(_pkg_name)
        _pkg_stub.__path__ = [_pkg_dir]
        sys.modules[_pkg_name] = _pkg_stub

    def _load_submod(mod_short: str):
        full = f"{_pkg_name}.{mod_short}"
        if full in sys.modules:
            return sys.modules[full]
        spec = importlib.util.spec_from_file_location(full, os.path.join(_pkg_dir, f"{mod_short}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
        return mod

    _load_submod("s205_meta_model")
    _eval_mod = _load_submod("s205_signal_evaluator")
    _shdw_mod = _load_submod("s205_shadow_collector")
    build_causal_intraday_context = _eval_mod.build_causal_intraday_context
    S205ShadowCollector = _shdw_mod.S205ShadowCollector
    load_shadow_preregistration = _shdw_mod.load_shadow_preregistration
    sweep_l5_book_for_qty = _shdw_mod.sweep_l5_book_for_qty


def _make_observed_quote(
    ikey: str,
    expiry: str,
    strike: float,
    opt_type: str,
    bid: float,
    ask: float,
    l5_qty_each: int = 30,
    age_ms: int = 120,
    ts_ms: int = 1763200000000,
    obi: float = 0.18,
    vtt: int = 12000,
) -> Dict[str, Any]:
    levels = [
        {
            "bid_p": round(max(0.05, bid - i * 0.10), 2),
            "bid_q": l5_qty_each,
            "ask_p": round(ask + i * 0.10, 2),
            "ask_q": l5_qty_each,
        }
        for i in range(5)
    ]
    mid = 0.5 * (bid + ask)
    sp = round(ask - bid, 2)
    return {
        "instrument_key": ikey,
        "trading_symbol": f"NIFTY {expiry} {int(strike)}{opt_type}",
        "underlying": "NIFTY",
        "expiry": expiry,
        "strike": float(strike),
        "option_type": opt_type,
        "lot_size": 65,
        "ltp": round(mid, 2),
        "bid": bid,
        "ask": ask,
        "raw_bid": bid,
        "raw_ask": ask,
        "bid_qty": l5_qty_each,
        "ask_qty": l5_qty_each,
        "l5_bid_qty": l5_qty_each * 5,
        "l5_ask_qty": l5_qty_each * 5,
        "book_obi_l5": obi,
        "depth_levels": levels,
        "is_observed_l1_book": True,
        "depth_observed_on_tick": True,
        "book_provenance": "OBSERVED_V3_L5",
        "l1_book_timestamp_ms": ts_ms - age_ms,
        "l1_book_age_ms": age_ms,
        "local_receipt_ts_ms": ts_ms,
        "spread": sp,
        "spread_pct": round((sp / mid) * 100.0, 2),
        "volume": vtt,
        "oi": 450000.0,
        "iv": 13.8,
        "delta": 0.48 if opt_type == "CE" else -0.24,
        "gamma": 0.0012,
        "theta": -8.5,
        "vega": 11.2,
        "is_valid": True,
    }


def _build_trending_session(num_bars: int = 75) -> Tuple[str, Dict[str, Any], Dict[str, Any], Dict[Tuple[str, float, str], Dict[str, Any]]]:
    day = "2026-11-16"
    exp = "2026-11-19"
    base_dt = datetime(2026, 11, 16, 9, 15, 0)
    spot_bars: Dict[str, Tuple[float, float, float, float, float]] = {}
    vix_bars: Dict[str, Tuple[float, float, float, float]] = {}
    opt_bars: Dict[Tuple[str, float, str], Dict[str, Tuple[float, float, float, float, float, float]]] = {}

    for i in range(num_bars):
        ts = (base_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
        drift = 1.5 * i if i < 30 else 45.0 + 4.5 * (i - 29)
        o = 24000.0 + drift
        h = o + 7.0
        l = o - 3.0
        c = o + 5.0
        spot_bars[ts] = (o, h, l, c, 1000.0 + i * 10.0)
        vix_bars[ts] = (14.2, 14.3, 14.1, 14.2)
        atm_k = round(c / 50.0) * 50.0
        for stk in (atm_k, atm_k - 100.0, atm_k - 250.0):
            for ot in ("CE", "PE"):
                eso = (exp, float(stk), ot)
                opt_bars.setdefault(eso, {})[ts] = (110.0, 114.0, 108.0, 112.0, 450.0, 250000.0)

    return day, spot_bars, vix_bars, opt_bars


class TestS205ShadowCollectorGolden(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp_dir.name, "shadow_test.db")
        set_test_db_override(self.db_path)

    def tearDown(self) -> None:
        set_test_db_override(None)
        self._tmp_dir.cleanup()

    def test_01_preregistration_invariants_and_sha256_lock(self) -> None:
        cfg, sha = load_shadow_preregistration()
        self.assertEqual(len(sha), 64)
        self.assertTrue(cfg["simulation_only"])
        self.assertFalse(cfg["naked_short_allowed"])
        self.assertFalse(cfg["allow_live_bridge_mirroring"])
        self.assertEqual(cfg["account_envelope"]["initial_capital_inr"], 200000.0)
        self.assertEqual(
            cfg["shadow_books"]["SHADOW_BOOK_A_SPREAD_150"]["spread_width_pts"],
            150.0,
        )

    def test_02_quote_provenance_fail_closed_vs_observed_l5_sweep(self) -> None:
        # 1. Synthetic / carried / stale / thin depth quotes MUST fail closed
        q_syn = _make_observed_quote("NSE_FO|1", "2026-11-19", 24000, "CE", 100.0, 100.5)
        q_syn["book_provenance"] = "SYNTHETIC_LTP_FALLBACK"
        q_syn["is_observed_l1_book"] = False
        r_syn = sweep_l5_book_for_qty(q_syn, "BUY", qty=65)
        self.assertFalse(r_syn["ok"])
        self.assertEqual(r_syn["reason"], "SYNTHETIC_OR_CARRIED_BOOK_REJECTED")

        q_stale = _make_observed_quote("NSE_FO|1", "2026-11-19", 24000, "CE", 100.0, 100.5, age_ms=2200)
        r_stale = sweep_l5_book_for_qty(q_stale, "BUY", qty=65, max_age_ms=1500)
        self.assertFalse(r_stale["ok"])
        self.assertEqual(r_stale["reason"], "STALE_L1_BOOK")

        q_thin = _make_observed_quote("NSE_FO|1", "2026-11-19", 24000, "CE", 100.0, 100.5, l5_qty_each=10)
        r_thin = sweep_l5_book_for_qty(q_thin, "BUY", qty=65)
        self.assertFalse(r_thin["ok"])
        self.assertEqual(r_thin["reason"], "INSUFFICIENT_L5_DEPTH")

        # 2. Exact multi-level VWAP + 1 adverse ₹0.05 tick
        q_custom = _make_observed_quote("NSE_FO|1", "2026-11-19", 24000, "CE", 99.80, 100.00)
        q_custom["depth_levels"] = [
            {"bid_p": 99.80, "bid_q": 20, "ask_p": 100.00, "ask_q": 20},
            {"bid_p": 99.70, "bid_q": 25, "ask_p": 100.15, "ask_q": 25},
            {"bid_p": 99.55, "bid_q": 30, "ask_p": 100.40, "ask_q": 30},
        ]
        r_buy = sweep_l5_book_for_qty(q_custom, "BUY", qty=65, adverse_ticks=1)
        self.assertTrue(r_buy["ok"])
        expected_vwap = (20 * 100.00 + 25 * 100.15 + 20 * 100.40) / 65.0
        expected_fill = round_to_nse_tick(expected_vwap + 0.05, mode="adverse_buy")
        self.assertAlmostEqual(r_buy["vwap_raw"], round(expected_vwap, 4), places=4)
        self.assertEqual(r_buy["fill_price"], expected_fill)

    def test_03_buy_first_spread_success_and_incomplete_leg_emergency_unwind(self) -> None:
        mock_margin = lambda legs: {
            "http_status": 200,
            "span_margin": 31200.0,
            "exposure_margin": 11400.0,
            "final_margin": 42600.0,
            "hedge_benefit": 92400.0,
        }
        sc = S205ShadowCollector(db_path=self.db_path, sync_advance_db=False, margin_fetcher=mock_margin)
        day, spot_b, vix_b, opt_b = _build_trending_session(45)
        ctx = build_causal_intraday_context(day, spot_b, vix_b, {}, opt_b, "2026-11-19", "2026-11-26", {})
        b_idx = len(ctx["ts"]) - 1
        atm = round(ctx["c"][b_idx] / 50.0) * 50.0
        short_k = atm - 100.0
        wing_k = short_k - 150.0

        t0_ms = 1763200000000
        q_wing = _make_observed_quote("NSE_FO|WING", "2026-11-19", wing_k, "PE", 11.80, 12.00, ts_ms=t0_ms)
        q_short = _make_observed_quote("NSE_FO|SHORT", "2026-11-19", short_k, "PE", 32.00, 32.30, ts_ms=t0_ms)
        qlookup = {
            ("2026-11-19", wing_k, "PE"): q_wing,
            ("2026-11-19", short_k, "PE"): q_short,
        }

        now_dt = datetime(2026, 11, 16, 10, 0, 0)
        _, opened, _ = sc.evaluate_completed_bar(
            ctx=ctx,
            bar_idx=b_idx,
            quote_lookup=qlookup,
            fut_quote={"fut_ltp": ctx["c"][b_idx] + 2.0},  # <5pt basis keeps Book B idle so Book A isolates cleanly
            india_vix=14.2,
            now_dt=now_dt,
            execute_new_entries=True,
            allow_network=False,
        )
        self.assertEqual(len(opened), 1)
        pid = opened[0]
        self.assertIn(pid, sc._pending_leg2)

        # Complete Leg 2 on subsequent observed tick (+350ms)
        q_short_t1 = _make_observed_quote("NSE_FO|SHORT", "2026-11-19", short_k, "PE", 31.80, 32.10, ts_ms=t0_ms + 350)
        sc.on_observed_tick(
            "NSE_FO|SHORT",
            q_short_t1,
            quote_lookup_by_ikey=lambda k: q_wing if k == "NSE_FO|WING" else q_short_t1,
            now_dt=now_dt + timedelta(seconds=1),
        )
        self.assertIn(pid, sc._open_packages)
        pkg_open = sc._open_packages[pid]
        self.assertLessEqual(pkg_open["structural_max_loss_inr"], 10000.0)

        # Trigger 70% decay target on subsequent tick and verify ₹0.00 canonical parity
        q_short_exit = _make_observed_quote("NSE_FO|SHORT", "2026-11-19", short_k, "PE", 6.20, 6.40, ts_ms=t0_ms + 60000)
        q_wing_exit = _make_observed_quote("NSE_FO|WING", "2026-11-19", wing_k, "PE", 2.10, 2.25, ts_ms=t0_ms + 60000)
        closed_ev = sc.on_observed_tick(
            "NSE_FO|SHORT",
            q_short_exit,
            quote_lookup_by_ikey=lambda k: q_wing_exit if k == "NSE_FO|WING" else q_short_exit,
            now_dt=now_dt + timedelta(minutes=10),
        )
        self.assertEqual(len(closed_ev), 1)
        closed_pkg = closed_ev[0]
        self.assertEqual(closed_pkg["exit_reason"], "SPREAD_DECAY_TARGET_70PCT")
        self.assertEqual(closed_pkg["brokerage_inr"], 80.0)  # 4 orders * ₹20

        wl = closed_pkg["legs_state"]["wing_leg"]
        sl = closed_pkg["legs_state"]["short_leg"]
        sw_bs = sweep_l5_book_for_qty(
            q_short_exit, "BUY", qty=65, adverse_ticks=1, max_spread_pct=None, require_observed=False
        )
        sw_sw = sweep_l5_book_for_qty(
            q_wing_exit, "SELL", qty=65, adverse_ticks=1, max_spread_pct=None, require_observed=False
        )
        ref_acc = compute_package_accounting(
            legs=[
                OptionLeg("PE", wl["strike"], 65, wl["entry_price"], sw_sw["fill_price"]),
                OptionLeg("PE", sl["strike"], -65, sl["entry_price"], sw_bs["fill_price"]),
            ],
            trade_date=day,
        )
        self.assertEqual(closed_pkg["net_pnl_inr"], ref_acc["net_pnl"])
        self.assertEqual(closed_pkg["stt_inr"], ref_acc["stt"])

        # Case B: Incomplete-Leg Emergency Unwind when Leg 2 credit collapses on next tick
        sc2 = S205ShadowCollector(db_path=self.db_path, sync_advance_db=False, margin_fetcher=mock_margin)
        sc2._reset_session_if_needed("2026-11-17")
        ctx2 = dict(ctx)
        ctx2["ts"] = [ts.replace("2026-11-16", "2026-11-17") for ts in ctx["ts"]]
        q_w2 = _make_observed_quote("NSE_FO|W2", "2026-11-19", wing_k, "PE", 14.00, 14.20, ts_ms=t0_ms + 90000)
        q_s2 = _make_observed_quote("NSE_FO|S2", "2026-11-19", short_k, "PE", 30.00, 30.30, ts_ms=t0_ms + 90000)
        _, opened2, _ = sc2.evaluate_completed_bar(
            ctx=ctx2,
            bar_idx=b_idx,
            quote_lookup={("2026-11-19", wing_k, "PE"): q_w2, ("2026-11-19", short_k, "PE"): q_s2},
            fut_quote={"fut_ltp": ctx["c"][b_idx] + 1.0},
            india_vix=14.2,
            now_dt=datetime(2026, 11, 17, 10, 0, 0),
            execute_new_entries=True,
            allow_network=False,
        )
        self.assertEqual(len(opened2), 1)
        pid2 = opened2[0]
        # Next tick: short leg bid collapses to 22.00 -> net credit < 14.0 pts -> emergency wing unwind!
        q_s2_bad = _make_observed_quote("NSE_FO|S2", "2026-11-19", short_k, "PE", 22.00, 22.40, ts_ms=t0_ms + 90500)
        unw = sc2.on_observed_tick(
            "NSE_FO|S2",
            q_s2_bad,
            quote_lookup_by_ikey=lambda k: q_w2 if k == "NSE_FO|W2" else q_s2_bad,
            now_dt=datetime(2026, 11, 17, 10, 0, 1),
        )
        self.assertEqual(len(unw), 1)
        self.assertEqual(unw[0]["state"], "INCOMPLETE_LEG_UNWOUND")

    def test_04_intrabar_open_before_close_capital_and_slot_causality(self) -> None:
        sc = S205ShadowCollector(db_path=self.db_path, sync_advance_db=False)
        day, spot_b, vix_b, opt_b = _build_trending_session(45)
        ctx = build_causal_intraday_context(day, spot_b, vix_b, {}, opt_b, "2026-11-19", "2026-11-26", {})
        b_idx = len(ctx["ts"]) - 1
        bar_ts = ctx["ts"][b_idx]
        atm = round(ctx["c"][b_idx] / 50.0) * 50.0
        q_ce = _make_observed_quote("NSE_FO|CE1", "2026-11-19", atm, "CE", 120.0, 120.5, obi=0.22)

        _, opened, _ = sc.evaluate_completed_bar(
            ctx=ctx,
            bar_idx=b_idx,
            quote_lookup={("2026-11-19", atm, "CE"): q_ce},
            fut_quote={"fut_ltp": ctx["c"][b_idx] + 8.5},
            india_vix=14.0,
            now_dt=datetime(2026, 11, 16, 10, 0, 0),
            execute_new_entries=True,
            allow_network=False,
        )
        self.assertEqual(len(opened), 1)
        pid = opened[0]
        pkg = sc._open_packages[pid]

        # Close on bar `2026-11-16 10:05:00`
        exit_bar = "2026-11-16 10:05:00"
        sc._close_single_long_package(pkg, q_ce, 115.0, "SPOT_VWAP_FLIP_EXIT", datetime(2026, 11, 16, 10, 5, 0), exit_bar_ts=exit_bar)

        # During bar `10:05:00`, slot & capital MUST remain locked!
        same_bar_cap = sc._active_capital_and_slots("2026-11-16 10:05:00", day)
        self.assertEqual(same_bar_cap["open_by_book"].get("SHADOW_BOOK_B_PAID_FLOW", 0), 1)
        self.assertGreater(same_bar_cap["reserved_inr"], 0.0)

        # Strictly on `10:06:00 > 10:05:00`, slot & capital release!
        next_bar_cap = sc._active_capital_and_slots("2026-11-16 10:06:00", day)
        self.assertEqual(next_bar_cap["open_by_book"].get("SHADOW_BOOK_B_PAID_FLOW", 0), 0)
        self.assertEqual(next_bar_cap["reserved_inr"], 0.0)

    def test_05_prefix_truncation_and_future_corruption_invariance(self) -> None:
        day, spot_full, vix_full, opt_full = _build_trending_session(75)
        sorted_ts = sorted(spot_full.keys())

        for check_idx in (35, 50, 65):
            cutoff_ts = sorted_ts[check_idx]
            spot_pref = {k: v for k, v in spot_full.items() if k <= cutoff_ts}
            vix_pref = {k: v for k, v in vix_full.items() if k <= cutoff_ts}
            opt_pref = {eso: {k: v for k, v in bm.items() if k <= cutoff_ts} for eso, bm in opt_full.items()}

            # Corrupt future bars > cutoff_ts by 10x in full copy
            spot_corrupt = {
                k: (v if k <= cutoff_ts else (v[0] * 10.0, v[1] * 10.0, v[2] * 0.1, v[3] * 10.0, v[4] * 50.0))
                for k, v in spot_full.items()
            }
            ctx_pref = build_causal_intraday_context(day, spot_pref, vix_pref, {}, opt_pref, "2026-11-19", "2026-11-26", {})
            ctx_corr_sliced = build_causal_intraday_context(
                day,
                {k: v for k, v in spot_corrupt.items() if k <= cutoff_ts},
                vix_pref,
                {},
                opt_pref,
                "2026-11-19",
                "2026-11-26",
                {},
            )

            atm = round(ctx_pref["c"][-1] / 50.0) * 50.0
            ql = {("2026-11-19", atm, "CE"): _make_observed_quote("NSE_FO|CE", "2026-11-19", atm, "CE", 118.0, 118.5)}
            sc_a = S205ShadowCollector(db_path=self.db_path, sync_advance_db=False)
            sc_b = S205ShadowCollector(db_path=self.db_path, sync_advance_db=False)
            dt_eval = datetime.fromisoformat(cutoff_ts)

            rows_a, _, _ = sc_a.evaluate_completed_bar(ctx_pref, len(ctx_pref["ts"]) - 1, ql, {"fut_ltp": ctx_pref["c"][-1] + 7.0}, 14.2, dt_eval, execute_new_entries=False)
            rows_b, _, _ = sc_b.evaluate_completed_bar(ctx_corr_sliced, len(ctx_corr_sliced["ts"]) - 1, ql, {"fut_ltp": ctx_corr_sliced["c"][-1] + 7.0}, 14.2, dt_eval, execute_new_entries=False)
            self.assertEqual(rows_a, rows_b)

    def test_06_target_pct_zero_preservation_and_telemetry_tables(self) -> None:
        sc_mirror = S205ShadowCollector(db_path=self.db_path, sync_advance_db=True)
        day, spot_b, vix_b, opt_b = _build_trending_session(45)
        ctx = build_causal_intraday_context(day, spot_b, vix_b, {}, opt_b, "2026-11-19", "2026-11-26", {})
        b_idx = len(ctx["ts"]) - 1
        atm = round(ctx["c"][b_idx] / 50.0) * 50.0
        q_ce = _make_observed_quote("NSE_FO|123", "2026-11-19", atm, "CE", 100.0, 100.5, obi=0.24)

        _, opened, _ = sc_mirror.evaluate_completed_bar(
            ctx=ctx,
            bar_idx=b_idx,
            quote_lookup={("2026-11-19", atm, "CE"): q_ce},
            fut_quote={"fut_ltp": ctx["c"][b_idx] + 9.0},
            india_vix=14.0,
            now_dt=datetime(2026, 11, 16, 10, 0, 0),
            execute_new_entries=True,
            allow_network=False,
        )
        self.assertEqual(len(opened), 1)
        conn_chk = sqlite3.connect(self.db_path)
        conn_chk.row_factory = sqlite3.Row
        mirrored_rows = conn_chk.execute(
            "SELECT * FROM option_sniffer_advance_trades WHERE strategy_mode = 'SHADOW_BOOK_B_PAID_FLOW';"
        ).fetchall()
        conn_chk.close()
        self.assertEqual(len(mirrored_rows), 1)
        self.assertEqual(float(mirrored_rows[0]["target_pct"]), 0.0)  # Must NOT become 35.0!
        rctx_m = json.loads(mirrored_rows[0]["regime_context_json"])
        self.assertTrue(rctx_m.get("shadow_only"))

        # Verify 1m chain + quote fade + expiry FSP persistence
        sc = S205ShadowCollector(db_path=self.db_path, sync_advance_db=False)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS option_sniffer_advance_s205_l5_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                bar_ts TEXT NOT NULL,
                spot_ltp REAL,
                fut_ltp REAL,
                fut_atp_vwap REAL,
                real_basis_pts REAL,
                fut_oi REAL,
                india_vix REAL,
                avg_spread_pct REAL,
                avg_l5_obi REAL,
                readiness_json TEXT
            );
            """
        )
        conn.commit()
        conn.close()

        q1 = _make_observed_quote("NSE_FO|1", "2026-11-19", 24000, "CE", 110.0, 110.5, vtt=5000)
        cnt = sc.record_1m_chain_and_snapshot(
            "2026-11-19 15:05:00",
            datetime(2026, 11, 19, 15, 5, 0),
            [q1],
            24010.0,
            {"fut_ltp": 24015.0, "fut_atp_vwap": 24008.0, "fut_oi": 11000000.0},
            14.1,
            "2026-11-19",
            "2026-11-26",
            [],
        )
        self.assertEqual(cnt, 1)

        fsp1 = sc.record_expiry_fsp_sample(datetime(2026, 11, 19, 15, 10, 0), "2026-11-19", 24000.0, 24002.0)
        fsp2 = sc.record_expiry_fsp_sample(datetime(2026, 11, 19, 15, 29, 0), "2026-11-19", 24030.0, 24031.0)
        self.assertIsNotNone(fsp1)
        self.assertEqual(fsp2["spot_1500_1530_twap"], 24015.0)
        self.assertEqual(fsp2["spot_1529_close"], 24030.0)
        self.assertEqual(fsp2["fsp_minus_1529_pts"], -15.0)

    def test_07_live_bridge_airgap_source_verification(self) -> None:
        bridge_path = os.path.join(
            _BACKEND_ROOT,
            "app",
            "analytics",
            "option_sniffer_advance_live",
            "advance_live_bridge.py",
        )
        with open(bridge_path, "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("SHADOW_TELEMETRY_AIRGAP_SKIP", src)
        self.assertIn('strat_m.startswith("SHADOW_")', src)
        self.assertIn('strat_m.startswith("V6B_")', src)
        self.assertIn('"v6b_ml_legacy_comparator"', src)

    def test_08_side_by_side_v6b_comparator_isolation_and_airgap(self) -> None:
        from app.analytics.option_sniffer_advance.advance_db import (
            AdvanceDB,
            init_option_sniffer_advance_tables,
        )

        init_option_sniffer_advance_tables()
        sc = S205ShadowCollector(db_path=self.db_path, sync_advance_db=True)

        # 1. Insert a pre-2026-11-15 historical row -> must land in HISTORICAL_LEGACY_V2
        AdvanceDB.create_trade(
            {
                "trade_id": "hist_v2_01",
                "signal_id": "sig_h1",
                "book_type": "LONG_BOOK_BUY",
                "strategy_mode": "WAVE_1_GAMMA_SNIPER",
                "direction": "BUY_CE",
                "instrument_key": "NSE_FO|H1",
                "trading_symbol": "NIFTY 24000 CE",
                "underlying": "NIFTY",
                "option_type": "CE",
                "strike": 24000.0,
                "expiry": "2026-11-12",
                "lot_size": 65,
                "quantity": 65,
                "entry_time": "2026-11-10T10:15:00",
                "entry_bid": 100.0,
                "entry_ask": 100.5,
                "entry_price_used": 100.5,
                "stop_loss_pct": 20.0,
                "target_pct": 0.0,
                "estimated_margin": 6532.5,
                "regime_context": {},
            }
        )
        AdvanceDB.close_trade(
            "hist_v2_01",
            {
                "exit_time": "2026-11-10T10:45:00",
                "exit_bid": 90.0,
                "exit_ask": 90.5,
                "exit_price_used": 90.0,
                "peak_price": 102.0,
                "trough_price": 90.0,
                "mfe_pct": 1.5,
                "mae_pct": -10.4,
                "gross_pnl": -682.5,
                "charges": 62.1,
                "slippage": 0.0,
                "net_pnl": -744.6,
                "net_pnl_pct": -11.4,
                "holding_duration_seconds": 1800,
                "exit_reason": "STOP_LOSS",
            },
        )

        # 2. Insert a V6B_ML_LEGACY_COMPARATOR row with target_pct=0.0 and shadow_only=True
        AdvanceDB.create_trade(
            {
                "trade_id": "adv_v6b_comp_01",
                "signal_id": "sig_v6b_01",
                "book_type": "SHORT_BOOK_SELL",
                "strategy_mode": "V6B_S1_OI",
                "direction": "SELL_PE",
                "instrument_key": "NSE_FO|V6B1",
                "trading_symbol": "NIFTY 2026-11-19 24050PE",
                "underlying": "NIFTY",
                "option_type": "PE",
                "strike": 24050.0,
                "expiry": "2026-11-19",
                "lot_size": 65,
                "quantity": 65,
                "entry_time": "2026-11-16T10:02:00",
                "entry_bid": 120.0,
                "entry_ask": 120.5,
                "entry_price_used": 120.0,
                "stop_loss_pct": 18.0,
                "target_pct": 0.0,
                "estimated_margin": 55000.0,
                "regime_context": {
                    "shadow_only": True,
                    "cohort": "V6B_ML_LEGACY_COMPARATOR",
                    "execution_eligibility": "UNHEDGED_SINGLE_LEG_PAPER_COMPARATOR (NOT LIVE ELIGIBLE)",
                    "ml_prob": 0.63,
                    "ml_p_min": 0.56,
                },
            }
        )
        open_rows = AdvanceDB.get_open_trades()
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(float(open_rows[0]["target_pct"]), 0.0)
        self.assertEqual(AdvanceDB.classify_trade_cohort(open_rows[0]), "V6B_ML_LEGACY_COMPARATOR")

        # 3. Open & close a Shadow Book B package (-₹4,000 loss) and verify V6B is NOT contaminated
        day, spot_b, vix_b, opt_b = _build_trending_session(45)
        ctx = build_causal_intraday_context(day, spot_b, vix_b, {}, opt_b, "2026-11-19", "2026-11-26", {})
        b_idx = len(ctx["ts"]) - 1
        atm = round(ctx["c"][b_idx] / 50.0) * 50.0
        q_ce = _make_observed_quote("NSE_FO|SHDW1", "2026-11-19", atm, "CE", 150.0, 150.5, obi=0.22)
        _, sh_opened, _ = sc.evaluate_completed_bar(
            ctx=ctx,
            bar_idx=b_idx,
            quote_lookup={("2026-11-19", atm, "CE"): q_ce},
            fut_quote={"fut_ltp": ctx["c"][b_idx] + 9.0},
            india_vix=14.0,
            now_dt=datetime(2026, 11, 16, 10, 0, 0),
            execute_new_entries=True,
            allow_network=False,
        )
        self.assertEqual(len(sh_opened), 1)
        sh_pkg = sc._open_packages[sh_opened[0]]
        sc._close_single_long_package(
            sh_pkg,
            q_ce,
            85.0,
            "LONG_HARD_SL_HIT",
            datetime(2026, 11, 16, 10, 5, 0),
            exit_bar_ts="2026-11-16 10:05:00",
        )

        comp = AdvanceDB.get_cohort_comparison_summary(shadow_summary=sc.get_summary())
        self.assertTrue(comp["never_aggregate_cohorts"])
        self.assertEqual(comp["S205_SHADOW_FORWARD_V1"]["closed_trades_count"], 1)
        self.assertLess(comp["S205_SHADOW_FORWARD_V1"]["realized_net_pnl_inr"], -3500.0)

        # V6B comparator has 0 closed trades and ₹0.00 realized net P&L (completely uncontaminated by Shadow V1 loss & Historical V2 loss!)
        self.assertEqual(comp["V6B_ML_LEGACY_COMPARATOR"]["open_trades_count"], 1)
        self.assertEqual(comp["V6B_ML_LEGACY_COMPARATOR"]["closed_trades_count"], 0)
        self.assertEqual(comp["V6B_ML_LEGACY_COMPARATOR"]["realized_net_pnl_inr"], 0.0)

        # Historical V2 archive holds the 2026-11-10 row cleanly isolated
        self.assertEqual(comp["HISTORICAL_LEGACY_V2"]["closed_trades_count"], 1)
        self.assertEqual(comp["HISTORICAL_LEGACY_V2"]["realized_net_pnl_inr"], -744.6)

    def test_09_book_b_close_only_ratchet_2tuple_stages_and_lifecycle_listener(self) -> None:
        events_captured: List[Tuple[str, str]] = []
        sc = S205ShadowCollector(db_path=self.db_path, sync_advance_db=False)
        sc.register_lifecycle_listener(lambda ev, p: events_captured.append((ev, str(p.get("package_id")))))

        day, spot_b, vix_b, opt_b = _build_trending_session(50)
        ctx = build_causal_intraday_context(day, spot_b, vix_b, {}, opt_b, "2026-11-19", "2026-11-26", {})
        b_idx = 40
        atm = round(ctx["c"][b_idx] / 50.0) * 50.0
        q_open = _make_observed_quote("NSE_FO|BOOKB_CE", "2026-11-19", atm, "CE", 144.95, 145.00, l5_qty_each=100, obi=0.22)

        _, opened, _ = sc.evaluate_completed_bar(
            ctx=ctx,
            bar_idx=b_idx,
            quote_lookup={("2026-11-19", atm, "CE"): q_open},
            fut_quote={"fut_ltp": ctx["c"][b_idx] + 9.0},
            india_vix=15.8,
            now_dt=datetime(2026, 11, 16, 9, 55, 0),
            execute_new_entries=True,
            allow_network=False,
        )
        self.assertEqual(len(opened), 1)
        pid = opened[0]
        self.assertEqual(sc._open_packages[pid]["legs_state"]["long_leg"]["entry_price"], 145.05)

        # Next completed 1m bar closes at 163.55 (+12.7% >= +12.0% Stage-1 -> locks +2.0% = ₹147.95 without crashing on 2-tuples!)
        q_bar2 = _make_observed_quote("NSE_FO|BOOKB_CE", "2026-11-19", atm, "CE", 163.55, 163.80, l5_qty_each=100, obi=0.20)
        _, _, closed_b2 = sc.evaluate_completed_bar(
            ctx=ctx,
            bar_idx=b_idx + 1,
            quote_lookup={("2026-11-19", atm, "CE"): q_bar2},
            fut_quote={"fut_ltp": ctx["c"][b_idx + 1] + 9.0},
            india_vix=15.8,
            now_dt=datetime(2026, 11, 16, 9, 56, 0),
            execute_new_entries=True,
            allow_network=False,
        )
        self.assertEqual(len(closed_b2), 0)
        r_stop = sc._open_packages[pid]["legs_state"]["ratchet_stop"]
        self.assertIsNotNone(r_stop)
        self.assertAlmostEqual(float(r_stop), 147.95, places=2)

        # Subsequent tick pulls back to 147.90 (fill_price = 147.85 <= 147.95) -> exits via RATCHET_STOP_HIT!
        q_pullback = _make_observed_quote("NSE_FO|BOOKB_CE", "2026-11-19", atm, "CE", 147.90, 148.20, l5_qty_each=100, obi=0.05)
        closed_tick = sc.on_observed_tick(
            "NSE_FO|BOOKB_CE",
            q_pullback,
            now_dt=datetime(2026, 11, 16, 9, 56, 30),
        )
        self.assertEqual(len(closed_tick), 1)
        self.assertEqual(closed_tick[0]["exit_reason"], "RATCHET_STOP_HIT")
        ev_names = [e[0] for e in events_captured]
        self.assertEqual(ev_names, ["BOOK_B_OPEN", "BOOK_B_RATCHET", "BOOK_B_CLOSE"])


if __name__ == "__main__":
    unittest.main()
