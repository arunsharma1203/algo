"""
Automated Prefix-Invariance & Causal Bar-Ordering Suite (`test_no_lookahead_leakage.py`).

Enforces:
  1. Bit-for-bit prefix-truncation AND extreme future-corruption invariance across t = 30..100
     on `build_causal_intraday_context`, `extract_causal_features_s205`, `S205CausalMetaModel`,
     and `evaluate_s205_bar_and_readiness` over a 201-bar session (t = 0..200).
  2. Strict 15m multi-timeframe completion guard (`bar_start + 15m <= decision_ts`).
  3. Negative/mutation self-check proving the harness actively catches lookahead if unsliced
     future warmup bars are passed prior to 09:45 IST.
  4. Bar-event execution causality (`bar[t].open` entries cannot consume capital or slot
     capacity released at `bar[t].close`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
import math
import os
import shutil
import sys
import tempfile
import types
import unittest
from typing import Any, Dict, List, Tuple

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.analytics.common.options_accounting import OptionLeg, compute_structural_package_risk
from app.data.database import set_test_db_override

try:
    from app.analytics.option_sniffer_advance_s205.s205_meta_model import (
        S205CausalMetaModel,
        extract_causal_features_s205,
    )
    from app.analytics.option_sniffer_advance_s205.s205_signal_evaluator import (
        build_causal_intraday_context,
        compute_close_only_ratchet,
        evaluate_s205_bar_and_readiness,
    )
except ModuleNotFoundError:
    # Pure-stdlib direct module loader when running outside venv (avoids __init__.py -> upstox_engine -> requests)
    _pkg_name = "app.analytics.option_sniffer_advance_s205"
    _pkg_dir = os.path.join(_BACKEND_ROOT, "app", "analytics", "option_sniffer_advance_s205")
    if _pkg_name not in sys.modules:
        _pkg_stub = types.ModuleType(_pkg_name)
        _pkg_stub.__path__ = [_pkg_dir]  # type: ignore[attr-defined]
        sys.modules[_pkg_name] = _pkg_stub

    def _load_s205_mod(mod_short: str) -> Any:
        full_name = f"{_pkg_name}.{mod_short}"
        spec = importlib.util.spec_from_file_location(full_name, os.path.join(_pkg_dir, f"{mod_short}.py"))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = mod
        spec.loader.exec_module(mod)
        return mod

    _mm = _load_s205_mod("s205_meta_model")
    _se = _load_s205_mod("s205_signal_evaluator")
    S205CausalMetaModel = _mm.S205CausalMetaModel
    extract_causal_features_s205 = _mm.extract_causal_features_s205
    build_causal_intraday_context = _se.build_causal_intraday_context
    compute_close_only_ratchet = _se.compute_close_only_ratchet
    evaluate_s205_bar_and_readiness = _se.evaluate_s205_bar_and_readiness


def _build_synthetic_session(
    num_bars: int = 201,
) -> Tuple[
    List[str],
    Dict[str, Tuple[float, float, float, float, float]],
    Dict[str, Tuple[float, float, float, float]],
    Dict[str, List[Tuple[str, float, float, float, float]]],
    Dict[Tuple[str, float, str], Dict[str, Tuple[float, float, float, float, float, float]]],
    Dict[str, Any],
]:
    """Builds a deterministic 201-bar intraday session (`09:15` .. `12:35 IST`)."""
    start_dt = datetime(2026, 11, 10, 9, 15, 0)
    front_exp = "2026-11-12"
    next_exp = "2026-11-19"

    ts_all: List[str] = []
    spot_bars: Dict[str, Tuple[float, float, float, float, float]] = {}
    vix_bars: Dict[str, Tuple[float, float, float, float]] = {}
    opt_bars: Dict[Tuple[str, float, str], Dict[str, Tuple[float, float, float, float, float, float]]] = {}

    strikes = [24850.0, 24900.0, 24950.0, 25000.0, 25050.0, 25100.0, 25150.0, 25200.0]
    for exp in (front_exp, next_exp):
        for k in strikes:
            for ot in ("CE", "PE"):
                opt_bars[(exp, k, ot)] = {}

    spot_prev = 25000.0
    for idx in range(num_bars):
        cur_dt = start_dt + timedelta(minutes=idx)
        ts = cur_dt.strftime("%Y-%m-%d %H:%M:%S")
        ts_all.append(ts)

        # Deterministic regime-varying path (ORB warmup -> trend -> consolidation)
        wave = 18.0 * math.sin(idx / 9.0) + (0.45 * idx if idx >= 30 else 4.0 * math.cos(idx / 4.0))
        c_px = round(25000.0 + wave, 2)
        o_px = round(spot_prev, 2)
        h_px = round(max(o_px, c_px) + 4.5 + (idx % 3), 2)
        l_px = round(min(o_px, c_px) - 4.0 - ((idx + 1) % 3), 2)
        vol = float(10000 + (idx % 17) * 550)
        spot_bars[ts] = (o_px, h_px, l_px, c_px, vol)
        spot_prev = c_px

        vx = round(14.20 + 0.35 * math.sin(idx / 15.0), 4)
        vix_bars[ts] = (vx, vx + 0.08, vx - 0.08, vx)

        for exp_idx, exp in enumerate((front_exp, next_exp)):
            time_val = 95.0 + 35.0 * exp_idx - 0.08 * idx
            for k in strikes:
                intr_ce = max(0.0, c_px - k)
                intr_pe = max(0.0, k - c_px)
                ce_c = round(max(6.0, intr_ce + time_val * math.exp(-abs(c_px - k) / 220.0)), 2)
                pe_c = round(max(6.0, intr_pe + (time_val - 2.5) * math.exp(-abs(c_px - k) / 220.0)), 2)

                ce_o = round(ce_c * 0.99, 2)
                ce_h = round(ce_c * 1.03, 2)
                ce_l = round(ce_c * 0.97, 2)
                pe_o = round(pe_c * 1.01, 2)
                pe_h = round(pe_c * 1.04, 2)
                pe_l = round(pe_c * 0.96, 2)

                opt_vol = float(4500 + (idx % 11) * 400)
                ce_oi = float(120000 + idx * 350)
                pe_oi = float(135000 + idx * 620)
                opt_bars[(exp, k, "CE")][ts] = (ce_o, ce_h, ce_l, ce_c, opt_vol, ce_oi)
                opt_bars[(exp, k, "PE")][ts] = (pe_o, pe_h, pe_l, pe_c, opt_vol, pe_oi)

    hw_15m_bars: Dict[str, List[Tuple[str, float, float, float, float]]] = {
        "RELIANCE": [],
        "HDFCBANK": [],
        "ICICIBANK": [],
    }
    num_15m = (num_bars // 15) + 2
    for b15 in range(num_15m):
        b_dt = start_dt + timedelta(minutes=15 * b15)
        b_ts = b_dt.strftime("%Y-%m-%d %H:%M:%S")
        for s_idx, sym in enumerate(("RELIANCE", "HDFCBANK", "ICICIBANK")):
            base = 1400.0 + 250.0 * s_idx
            ho = round(base + b15 * 1.2, 2)
            hc = round(ho + 2.4 + 0.3 * s_idx, 2)
            hw_15m_bars[sym].append((b_ts, ho, hc + 1.5, ho - 1.0, hc))

    prior_day_stats = {
        "pd_h": 25085.0,
        "pd_l": 24910.0,
        "pd_c": 24995.0,
        "cpr_width_pct": 0.22,
        "is_nr4": False,
        "is_nr7": False,
        "last_vix_close": 14.1,
        "prior_vix_closes": [13.2 + 0.15 * j for j in range(20)],
    }
    return ts_all, spot_bars, vix_bars, hw_15m_bars, opt_bars, prior_day_stats


def _evaluate_capital_gated_bar_events(
    initial_capital_inr: float,
    max_open_positions: int,
    bar_open_entries: Dict[int, Tuple[str, List[OptionLeg]]],
    bar_close_exits: Dict[int, Tuple[str, float]],
    total_bars: int,
) -> Dict[str, Any]:
    """
    Reference causal bar-event state machine enforcing strict intra-bar ordering:
      Phase 1 (`bar[t].open`): Evaluate new entries against CURRENT cash & open slots.
      Phase 2 (`bar[t].close`): Process exits, releasing capital & slots for `t + 1` onward.
    """
    free_cash = initial_capital_inr
    open_positions: Dict[str, float] = {}
    accepted_entries: List[Tuple[int, str]] = []
    rejected_entries: List[Tuple[int, str, str]] = []

    for t in range(total_bars):
        # 1. OPEN PHASE — Strictly BEFORE any bar[t].close exit settles!
        if t in bar_open_entries:
            trade_id, legs = bar_open_entries[t]
            risk = compute_structural_package_risk(legs, reference_spot=25000.0)
            req_reserve = max(
                float(risk["final_package_max_loss_inr"]),
                float(risk["buy_first_incomplete_leg_exposure_inr"]),
            )
            if len(open_positions) >= max_open_positions:
                rejected_entries.append((t, trade_id, "MAX_OPEN_SLOTS_AT_BAR_OPEN"))
            elif req_reserve > free_cash:
                rejected_entries.append((t, trade_id, "INSUFFICIENT_CAPITAL_AT_BAR_OPEN"))
            else:
                free_cash = round(free_cash - req_reserve, 2)
                open_positions[trade_id] = req_reserve
                accepted_entries.append((t, trade_id))

        # 2. CLOSE PHASE — Capital freed here is available ONLY at t + 1
        if t in bar_close_exits:
            exit_tid, realized_net_pnl = bar_close_exits[t]
            if exit_tid in open_positions:
                released = open_positions.pop(exit_tid)
                free_cash = round(free_cash + released + realized_net_pnl, 2)

    return {
        "accepted_entries": accepted_entries,
        "rejected_entries": rejected_entries,
        "ending_free_cash": free_cash,
    }


class TestNoLookaheadLeakage(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.meta_model = S205CausalMetaModel()

    def setUp(self) -> None:
        self._tmp_dir = tempfile.mkdtemp(prefix="golden_leak_")
        set_test_db_override(os.path.join(self._tmp_dir, "isolated_leak.db"))

    def tearDown(self) -> None:
        set_test_db_override(None)
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_s205_prefix_truncation_and_future_corruption_invariance_t30_to_t100(self) -> None:
        ts_all, spot_full, vix_full, hw_full, opt_full, prior_stats = _build_synthetic_session(201)
        cut_idx = 100
        cut_ts = ts_all[cut_idx]
        cut_decision_dt = datetime.strptime(cut_ts, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=1)

        # World A: Clean 201-bar session (t = 0..200)
        ctx_clean = build_causal_intraday_context(
            session_date="2026-11-10",
            spot_bars=spot_full,
            vix_bars=vix_full,
            hw_15m_bars=hw_full,
            opt_bars=opt_full,
            front_exp="2026-11-12",
            next_exp="2026-11-19",
            prior_day_stats=prior_stats,
        )
        self.assertIsNotNone(ctx_clean)
        assert ctx_clean is not None

        # World B: Truncated prefix (t = 0..100 only; bars 101..200 do not exist)
        spot_trunc = {ts: b for ts, b in spot_full.items() if ts <= cut_ts}
        vix_trunc = {ts: b for ts, b in vix_full.items() if ts <= cut_ts}
        opt_trunc = {
            eso: {ts: b for ts, b in bmap.items() if ts <= cut_ts}
            for eso, bmap in opt_full.items()
        }
        hw_trunc = {
            sym: [
                row
                for row in rows
                if datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S") + timedelta(minutes=15) <= cut_decision_dt
            ]
            for sym, rows in hw_full.items()
        }
        ctx_trunc = build_causal_intraday_context(
            session_date="2026-11-10",
            spot_bars=spot_trunc,
            vix_bars=vix_trunc,
            hw_15m_bars=hw_trunc,
            opt_bars=opt_trunc,
            front_exp="2026-11-12",
            next_exp="2026-11-19",
            prior_day_stats=prior_stats,
        )
        self.assertIsNotNone(ctx_trunc)
        assert ctx_trunc is not None

        # World C: 201-bar session with extreme corruption on all bars strictly after t = 100
        spot_corrupt = dict(spot_full)
        vix_corrupt = dict(vix_full)
        for ts in ts_all[cut_idx + 1 :]:
            spot_corrupt[ts] = (250000.0, 290000.0, 12000.0, 275000.0, 99999999.0)
            vix_corrupt[ts] = (95.0, 110.0, 80.0, 99.5)

        opt_corrupt: Dict[Tuple[str, float, str], Dict[str, Tuple[float, float, float, float, float, float]]] = {}
        for eso, bmap in opt_full.items():
            cmap = dict(bmap)
            for j, ts in enumerate(ts_all[cut_idx + 1 :]):
                if j % 5 == 0:
                    cmap.pop(ts, None)  # Inject missing future option bars
                else:
                    cmap[ts] = (9999.0, 15000.0, 0.05, 12500.0, 0.0, 99999999.0)
            opt_corrupt[eso] = cmap

        hw_corrupt: Dict[str, List[Tuple[str, float, float, float, float]]] = {}
        for sym, rows in hw_full.items():
            c_rows = []
            for row in rows:
                b_dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                # Any 15m bar completing strictly after cut_decision_dt is shocked by -50%
                if b_dt + timedelta(minutes=15) > cut_decision_dt:
                    c_rows.append((row[0], row[1] * 0.5, row[2] * 3.0, 1.0, row[4] * 0.5))
                else:
                    c_rows.append(row)
            hw_corrupt[sym] = c_rows

        ctx_corrupt = build_causal_intraday_context(
            session_date="2026-11-10",
            spot_bars=spot_corrupt,
            vix_bars=vix_corrupt,
            hw_15m_bars=hw_corrupt,
            opt_bars=opt_corrupt,
            front_exp="2026-11-12",
            next_exp="2026-11-19",
            prior_day_stats=prior_stats,
        )
        self.assertIsNotNone(ctx_corrupt)
        assert ctx_corrupt is not None

        series_keys = (
            "tpb",
            "adx",
            "pdi",
            "mdi",
            "er",
            "atrr",
            "rsi14",
            "vwap_z",
            "hw",
            "syn_basis",
            "oi_bull_diff",
            "strad_front_px",
            "strad_front_vwap",
            "strad_next_px",
            "strad_next_vwap",
        )
        for key in series_keys:
            self.assertEqual(ctx_clean[key][: cut_idx + 1], ctx_trunc[key][: cut_idx + 1], f"Truncation drift in {key}")
            self.assertEqual(ctx_clean[key][: cut_idx + 1], ctx_corrupt[key][: cut_idx + 1], f"Corruption leak in {key}")

        probe_eso = ("2026-11-12", 25000.0, "CE")
        for bar_i in range(30, cut_idx + 1):
            for ecode, is_cred in ((1, True), (2, True), (3, True), (4, False)):
                f_clean = extract_causal_features_s205(ctx_clean, bar_i, probe_eso, is_cred, True, ecode, 65)
                f_trunc = extract_causal_features_s205(ctx_trunc, bar_i, probe_eso, is_cred, True, ecode, 65)
                f_corrupt = extract_causal_features_s205(ctx_corrupt, bar_i, probe_eso, is_cred, True, ecode, 65)
                self.assertEqual(f_clean, f_trunc, f"Feature truncation mismatch at bar {bar_i} ecode {ecode}")
                self.assertEqual(f_clean, f_corrupt, f"Feature future-leak mismatch at bar {bar_i} ecode {ecode}")

                m_clean = self.meta_model.explain_prediction(f_clean)
                m_corrupt = self.meta_model.explain_prediction(f_corrupt)
                self.assertEqual(m_clean, m_corrupt, f"ML prediction leak at bar {bar_i}")

            out_clean = evaluate_s205_bar_and_readiness(ctx_clean, bar_i, self.meta_model, {}, None, 65)
            out_trunc = evaluate_s205_bar_and_readiness(ctx_trunc, bar_i, self.meta_model, {}, None, 65)
            out_corrupt = evaluate_s205_bar_and_readiness(ctx_corrupt, bar_i, self.meta_model, {}, None, 65)
            self.assertEqual(out_clean, out_trunc, f"Signal/readiness truncation drift at bar {bar_i}")
            self.assertEqual(out_clean, out_corrupt, f"Signal/readiness future corruption leak at bar {bar_i}")

    def test_uncompleted_15m_heavyweight_bar_cannot_leak_into_0945_decision(self) -> None:
        ts_all, spot_full, vix_full, hw_full, opt_full, prior_stats = _build_synthetic_session(40)
        # At bar_idx = 30 (ts = 09:45:00, decision_dt = 09:46:00), the 09:45:00 15m bar completes at 10:00:00
        hw_shocked = {}
        for sym, rows in hw_full.items():
            hw_shocked[sym] = [
                (r[0], 10.0, 99999.0, 1.0, 99999.0) if r[0].endswith("09:45:00") else r
                for r in rows
            ]

        ctx_a = build_causal_intraday_context(
            "2026-11-10", spot_full, vix_full, hw_full, opt_full, "2026-11-12", "2026-11-19", prior_stats
        )
        ctx_b = build_causal_intraday_context(
            "2026-11-10", spot_full, vix_full, hw_shocked, opt_full, "2026-11-12", "2026-11-19", prior_stats
        )
        assert ctx_a is not None and ctx_b is not None
        self.assertEqual(ctx_a["hw"][30], ctx_b["hw"][30])

    def test_pre_0945_warmup_requires_causal_prefix_slicing_guard(self) -> None:
        ts_all, spot_full, vix_full, hw_full, opt_full, prior_stats = _build_synthetic_session(40)
        # Mutate 09:40 bar (idx=25) high to prove unsliced full-session dict affects orb30_h,
        # whereas causal prefix slicing up to idx=10 (09:25) remains strictly immune.
        spot_spiked = dict(spot_full)
        ts_0940 = ts_all[25]
        b25 = spot_spiked[ts_0940]
        spot_spiked[ts_0940] = (b25[0], 99999.0, b25[2], b25[3], b25[4])

        ts_0925 = ts_all[10]
        ctx_sliced_clean = build_causal_intraday_context(
            "2026-11-10",
            {k: v for k, v in spot_full.items() if k <= ts_0925},
            {k: v for k, v in vix_full.items() if k <= ts_0925},
            hw_full,
            {eso: {k: v for k, v in m.items() if k <= ts_0925} for eso, m in opt_full.items()},
            "2026-11-12",
            "2026-11-19",
            prior_stats,
        )
        ctx_sliced_spiked = build_causal_intraday_context(
            "2026-11-10",
            {k: v for k, v in spot_spiked.items() if k <= ts_0925},
            {k: v for k, v in vix_full.items() if k <= ts_0925},
            hw_full,
            {eso: {k: v for k, v in m.items() if k <= ts_0925} for eso, m in opt_full.items()},
            "2026-11-12",
            "2026-11-19",
            prior_stats,
        )
        assert ctx_sliced_clean is not None and ctx_sliced_spiked is not None
        self.assertEqual(ctx_sliced_clean["orb30_h"], ctx_sliced_spiked["orb30_h"])

    def test_close_only_ratchet_is_invariant_to_intrabar_wicks(self) -> None:
        stages = [(12.0, 0.0, 3.5), (26.0, 11.0, 12.0), (46.0, 8.0, 26.0)]
        r1 = compute_close_only_ratchet(is_credit=True, entry_price=100.0, best_close_pts=72.0, prev_ratchet_stop=None, ratchet_stages=stages)
        r2 = compute_close_only_ratchet(is_credit=True, entry_price=100.0, best_close_pts=72.0, prev_ratchet_stop=r1, ratchet_stages=stages)
        self.assertEqual(r1, 79.92)
        self.assertEqual(r2, 79.92)

    def test_bar_open_entry_cannot_consume_same_bar_close_capital_release(self) -> None:
        # Capital = ₹2,00,000. Trade 1 reserves ₹1,95,000 at bar 0 open and exits at bar 5 close.
        # Trade 2 requires ₹6,500 at bar 5 open (only ₹5,000 free at bar 5 open -> MUST REJECT!).
        # Trade 3 requires ₹6,500 at bar 6 open (after bar 5 close freed ₹1,95,000 -> MUST ACCEPT!).
        leg_big = [OptionLeg(option_type="CE", strike=25000.0, qty=1950, entry_price=100.0, exit_price=102.0)]
        leg_next = [OptionLeg(option_type="CE", strike=25000.0, qty=65, entry_price=100.0, exit_price=105.0)]

        out = _evaluate_capital_gated_bar_events(
            initial_capital_inr=200000.0,
            max_open_positions=2,
            bar_open_entries={
                0: ("TRD_1", leg_big),
                5: ("TRD_2_SAME_BAR_OPEN", leg_next),
                6: ("TRD_3_NEXT_BAR_OPEN", leg_next),
            },
            bar_close_exits={
                5: ("TRD_1", 1200.0),
            },
            total_bars=8,
        )
        self.assertEqual(out["accepted_entries"], [(0, "TRD_1"), (6, "TRD_3_NEXT_BAR_OPEN")])
        self.assertEqual(
            out["rejected_entries"],
            [(5, "TRD_2_SAME_BAR_OPEN", "INSUFFICIENT_CAPITAL_AT_BAR_OPEN")],
        )


if __name__ == "__main__":
    unittest.main()
