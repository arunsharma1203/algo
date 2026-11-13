"""
S2_05 Pure Causal Context Builder, 4-Engine Gate Evaluator & Close-Only Ratchet Physics.

Implements the exact Hunt V7 `S2_05_V6B_ML_FIXED_SIM` specification:
  1. `V6B_S1_OI`     — Institutional Option Writer Continuation (Short Book, 09:45–13:45 IST)
  2. `V6B_S3_SPIKE`  — Morning IV Spike Trap Exhaustion (Short Book, 09:45–13:45 IST)
  3. `V6B_S4_Z2`     — 2-Sigma VWAP Exhaustion Reversal (Short Book, 09:45–13:45 IST)
  4. `V6B_LBASIS_ATM`— Sniper Trend & Basis Breakout (Long Book, 09:45–12:45 IST)

Every gate produces both:
  - Executable `:00` completed-bar `S205OrderCandidate` objects (when all gates + ML pass), and
  - Live `engine_readiness` checklists (`gates_passed / gates_total`, current vs threshold, ML score,
    and plain-English `when_order_fires` trigger guidance) for real-time frontend visibility.
"""

import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.option_sniffer_advance_s205.s205_meta_model import (
    S205CausalMetaModel,
    compute_adx_series_causal,
    compute_atrr_series_causal,
    compute_er_series_causal,
    compute_rsi14_series_causal,
    extract_causal_features_s205,
)


class LazyLiveOptStats:
    """
    Computes causal per-strike intraday telemetry `(vwap_list, oir_raw_list, peak_high_list,
    vol_surge_list, open_0915, oir_lag3_list)` strictly up to each bar index `i`.
    """

    def __init__(self, ts_list: List[str], day_eso: Dict[Tuple[str, float, str], Dict[str, tuple]]):
        self.ts_list = ts_list
        self.day_eso = day_eso
        self._cache: Dict[Tuple[str, float, str], Optional[tuple]] = {}

    def get(self, eso: Tuple[str, float, str], default: Any = None) -> Any:
        if eso in self._cache:
            val = self._cache[eso]
            return default if val is None else val
        bmap = self.day_eso.get(eso)
        if not bmap:
            self._cache[eso] = None
            return default

        vw_l: List[float] = []
        oir_raw_l: List[float] = []
        pk_l: List[float] = []
        vs_l: List[float] = []
        pv = vv = 0.0
        oi_base = 0.0
        pk_sf = 0.0
        b0 = bmap.get(self.ts_list[0]) if self.ts_list else None
        op_0915 = float(b0[0]) if (b0 and b0[4] >= 0 and b0[0] > 0) else 0.0
        recent_vols: List[float] = []

        for idx_t, ts_k in enumerate(self.ts_list):
            b = bmap.get(ts_k)
            if b is not None and (b[4] > 0 or b[3] > 0):
                vol = max(1.0, float(b[4]))
                pv += ((float(b[1]) + float(b[2]) + float(b[3])) / 3.0) * vol
                vv += vol
                if idx_t <= 20 and float(b[5]) > 0 and oi_base <= 0:
                    oi_base = float(b[5])
                if op_0915 <= 0.0 and idx_t <= 8 and float(b[0]) > 0:
                    op_0915 = float(b[0])
                if float(b[1]) > pk_sf:
                    pk_sf = float(b[1])
                recent_vols.append(vol)
                avg_v = vv / max(1, len(recent_vols))
                v5 = sum(recent_vols[-5:]) / min(5, len(recent_vols))
                vs = v5 / max(1.0, avg_v)
                oir = (float(b[5]) / oi_base) if (oi_base > 0 and float(b[5]) > 0) else (oir_raw_l[-1] if oir_raw_l else 1.0)
            else:
                vs = 1.0
                oir = oir_raw_l[-1] if oir_raw_l else 1.0
            vw_l.append((pv / vv) if vv > 0 else 0.0)
            oir_raw_l.append(oir)
            pk_l.append(pk_sf)
            vs_l.append(vs)

        oir_lag3_l = [oir_raw_l[max(0, i - 3)] for i in range(len(self.ts_list))]
        tup = (vw_l, oir_raw_l, pk_l, vs_l, op_0915, oir_lag3_l)
        self._cache[eso] = tup
        return tup


def build_causal_intraday_context(
    session_date: str,
    spot_bars: Dict[str, Tuple[float, float, float, float, float]],
    vix_bars: Dict[str, Tuple[float, float, float, float]],
    hw_15m_bars: Dict[str, List[Tuple[str, float, float, float, float]]],
    opt_bars: Dict[Tuple[str, float, str], Dict[str, Tuple[float, float, float, float, float, float]]],
    front_exp: str,
    next_exp: str,
    prior_day_stats: Dict[str, Any],
    strike_step: float = 50.0,
) -> Optional[Dict[str, Any]]:
    """Builds strictly point-in-time causal session context across all completed 1m bars."""
    if not spot_bars or not front_exp:
        return None

    bars_sorted = sorted(spot_bars.items())
    ts_list = [x[0] for x in bars_sorted]
    o_list = [float(x[1][0]) for x in bars_sorted]
    h_list = [float(x[1][1]) for x in bars_sorted]
    l_list = [float(x[1][2]) for x in bars_sorted]
    c_list = [float(x[1][3]) for x in bars_sorted]
    n = len(ts_list)
    if n == 0:
        return None

    d_dt = datetime.strptime(session_date[:10], "%Y-%m-%d").date()
    front_dte = max(0, (datetime.strptime(front_exp[:10], "%Y-%m-%d").date() - d_dt).days)
    eff_next_exp = next_exp if next_exp else front_exp
    next_dte = max(0, (datetime.strptime(eff_next_exp[:10], "%Y-%m-%d").date() - d_dt).days)

    tpb_series: List[float] = []
    run_tp = 0.0
    for i in range(n):
        run_tp += (h_list[i] + l_list[i] + c_list[i]) / 3.0
        tpb_series.append(run_tp / (i + 1))

    v_sorted = sorted(vix_bars.items())
    vix_open = float(v_sorted[0][1][0]) if v_sorted else float(prior_day_stats.get("last_vix_close") or 14.0)
    vix_map: Dict[str, float] = {}
    last_v = vix_open
    for ts_s in ts_list:
        if ts_s in vix_bars:
            last_v = float(vix_bars[ts_s][3])
        vix_map[ts_s] = last_v

    prior_vix = list(prior_day_stats.get("prior_vix_closes") or [])
    ivp_20d = (sum(1 for x in prior_vix if x <= vix_open) / len(prior_vix) * 100.0) if prior_vix else 50.0

    # Strictly completed 15m heavyweights (`b_dt + 15m <= dec_dt`)
    hw_scores: List[float] = []
    for ts_s in ts_list:
        dec_dt = datetime.strptime(ts_s[:19], "%Y-%m-%d %H:%M:%S") + timedelta(minutes=1)
        votes: List[float] = []
        for sym in ("RELIANCE", "HDFCBANK", "ICICIBANK"):
            valid: List[Tuple[float, float]] = []
            for h_ts, ho, _hh, _hl, hc in hw_15m_bars.get(sym, []):
                try:
                    b_dt = datetime.strptime(h_ts[:19], "%Y-%m-%d %H:%M:%S")
                    if b_dt + timedelta(minutes=15) <= dec_dt:
                        valid.append((float(ho), float(hc)))
                except Exception:
                    continue
            if valid and valid[0][0] > 0:
                pct = ((valid[-1][1] - valid[0][0]) / valid[0][0]) * 100.0
                votes.append(1.0 if pct > 0.08 else (-1.0 if pct < -0.08 else 0.0))
        hw_scores.append((sum(votes) / len(votes)) if votes else 0.0)

    orb15_idx = [i for i, ts in enumerate(ts_list) if "09:15" <= ts[11:16] <= "09:29"]
    orb30_idx = [i for i, ts in enumerate(ts_list) if "09:15" <= ts[11:16] <= "09:44"]
    orb15_h = max(h_list[i] for i in orb15_idx) if orb15_idx else max(h_list)
    orb15_l = min(l_list[i] for i in orb15_idx) if orb15_idx else min(l_list)
    orb30_h = max(h_list[i] for i in orb30_idx) if orb30_idx else max(h_list)
    orb30_l = min(l_list[i] for i in orb30_idx) if orb30_idx else min(l_list)

    adx_arr, pdi_arr, mdi_arr = compute_adx_series_causal(h_list, l_list, c_list, 14)
    er_arr = compute_er_series_causal(c_list, 20)
    atrr_arr = compute_atrr_series_causal(h_list, l_list, c_list, 5, 20)
    rsi14 = compute_rsi14_series_causal(c_list)

    vwap_z = [0.0] * n
    sq_dev = 0.0
    for i in range(n):
        dev = c_list[i] - tpb_series[i]
        sq_dev += dev * dev
        std_v = math.sqrt(sq_dev / (i + 1))
        vwap_z[i] = dev / max(8.0, std_v)

    opt_stats = LazyLiveOptStats(ts_list, opt_bars)
    anchor_k = float(round(o_list[0] / strike_step) * strike_step)

    def _build_straddle_vwap(exp_use: str) -> Tuple[List[float], List[float]]:
        s_px: List[float] = []
        s_vwap: List[float] = []
        run_pv = run_v = 0.0
        ce_d = opt_bars.get((exp_use, anchor_k, "CE"), {})
        pe_d = opt_bars.get((exp_use, anchor_k, "PE"), {})
        last_s = 0.0
        for ts_k in ts_list:
            cb = ce_d.get(ts_k)
            pb = pe_d.get(ts_k)
            if cb and pb and (cb[3] + pb[3]) > 0:
                last_s = float(cb[3]) + float(pb[3])
                vol_s = max(1.0, float(cb[4]) + float(pb[4]))
                run_pv += last_s * vol_s
                run_v += vol_s
            s_px.append(last_s)
            s_vwap.append((run_pv / run_v) if run_v > 0 else last_s)
        return s_px, s_vwap

    strad_front_px, strad_front_vwap = _build_straddle_vwap(front_exp)
    strad_next_px, strad_next_vwap = _build_straddle_vwap(eff_next_exp)

    exp_f = front_exp if front_dte >= 1 else eff_next_exp
    syn_basis = [0.0] * n
    oi_bull_diff = [0.0] * n
    last_basis = 0.0
    for i, ts_k in enumerate(ts_list):
        spot = c_list[i]
        atm_k = float(round(spot / strike_step) * strike_step)
        cb = opt_bars.get((exp_f, atm_k, "CE"), {}).get(ts_k)
        pb = opt_bars.get((exp_f, atm_k, "PE"), {}).get(ts_k)
        if cb and pb:
            last_basis = (atm_k + float(cb[3]) - float(pb[3])) - spot
        syn_basis[i] = last_basis

        oi_i = max(0, i - 3)
        ce_st = opt_stats.get((exp_f, atm_k, "CE"))
        pe_st = opt_stats.get((exp_f, atm_k, "PE"))
        ce_st_itm = opt_stats.get((exp_f, atm_k - strike_step, "CE"))
        pe_st_itm = opt_stats.get((exp_f, atm_k + strike_step, "PE"))
        pe_oir = 0.5 * ((pe_st[1][oi_i] if pe_st else 1.0) + (pe_st_itm[1][oi_i] if pe_st_itm else 1.0))
        ce_oir = 0.5 * ((ce_st[1][oi_i] if ce_st else 1.0) + (ce_st_itm[1][oi_i] if ce_st_itm else 1.0))
        oi_bull_diff[i] = pe_oir - ce_oir

    orb30_pct = ((orb30_h - orb30_l) / max(1.0, o_list[0])) * 100.0
    if front_dte <= 1:
        regime_tag = "R4_EXPIRY_0_1DTE"
    elif vix_open >= 16.0 or ivp_20d >= 75.0 or orb30_pct >= 0.65:
        regime_tag = "R3_HIGH_VOL_EXPANSION"
    elif vix_open <= 13.2 and orb30_pct <= 0.35:
        regime_tag = "R1_LOW_VOL_COMPRESSION"
    else:
        regime_tag = "R2_NORMAL_TWO_WAY"

    return {
        "date": session_date[:10],
        "underlying": "NIFTY",
        "strike_step": strike_step,
        "regime_tag": regime_tag,
        "pd_h": float(prior_day_stats.get("pd_h") or 0.0),
        "pd_l": float(prior_day_stats.get("pd_l") or 0.0),
        "pd_c": float(prior_day_stats.get("pd_c") or 0.0),
        "cpr_width_pct": float(prior_day_stats.get("cpr_width_pct") or 0.25),
        "is_nr4": bool(prior_day_stats.get("is_nr4", False)),
        "is_nr7": bool(prior_day_stats.get("is_nr7", False)),
        "ivp_20d": round(ivp_20d, 2),
        "front_exp": front_exp,
        "front_dte": front_dte,
        "next_exp": eff_next_exp,
        "next_dte": next_dte,
        "ts": ts_list,
        "o": o_list,
        "h": h_list,
        "l": l_list,
        "c": c_list,
        "tpb": tpb_series,
        "vix_open": vix_open,
        "vix_map": vix_map,
        "hw": hw_scores,
        "orb15_h": orb15_h,
        "orb15_l": orb15_l,
        "orb30_h": orb30_h,
        "orb30_l": orb30_l,
        "adx": adx_arr,
        "pdi": pdi_arr,
        "mdi": mdi_arr,
        "er": er_arr,
        "atrr": atrr_arr,
        "rsi14": rsi14,
        "vwap_z": vwap_z,
        "syn_basis": syn_basis,
        "oi_bull_diff": oi_bull_diff,
        "strad_front_px": strad_front_px,
        "strad_front_vwap": strad_front_vwap,
        "strad_next_px": strad_next_px,
        "strad_next_vwap": strad_next_vwap,
        "day_eso": opt_bars,
        "opt_stats": opt_stats,
    }


def compute_close_only_ratchet(
    is_credit: bool,
    entry_price: float,
    best_close_pts: float,
    prev_ratchet_stop: Optional[float],
    ratchet_stages: List[Tuple[float, float, float]],
) -> Optional[float]:
    """
    Updates trailing ratchet stop STRICTLY from 1-minute bar close (`best_close_pts`).
    Never uses intrabar highs/lows (eliminates V6 intrabar ratchet bias).
    """
    if entry_price <= 0:
        return prev_ratchet_stop
    ratchet = prev_ratchet_stop

    if is_credit:
        fav_pct = ((entry_price - best_close_pts) / entry_price) * 100.0
        for act_pct, dist_pct, lock_pct in ratchet_stages:
            if fav_pct >= act_pct:
                lock_lvl = entry_price * (1.0 - lock_pct / 100.0)
                if dist_pct > 0.0:
                    dist_lvl = best_close_pts * (1.0 + dist_pct / 100.0)
                    cand = round(min(lock_lvl, dist_lvl), 2)
                else:
                    cand = round(lock_lvl, 2)
                ratchet = cand if ratchet is None else min(ratchet, cand)
    else:
        fav_pct = ((best_close_pts - entry_price) / entry_price) * 100.0
        for act_pct, dist_pct, lock_pct in ratchet_stages:
            if fav_pct >= act_pct:
                lock_lvl = entry_price * (1.0 + lock_pct / 100.0)
                if dist_pct > 0.0:
                    dist_lvl = best_close_pts * (1.0 - dist_pct / 100.0)
                    cand = round(max(lock_lvl, dist_lvl), 2)
                else:
                    cand = round(lock_lvl, 2)
                ratchet = cand if ratchet is None else max(ratchet, cand)
    return ratchet


def evaluate_s205_bar_and_readiness(
    ctx: Optional[Dict[str, Any]],
    bar_idx: int,
    meta_model: S205CausalMetaModel,
    quote_lookup: Dict[Tuple[str, float, str], Dict[str, Any]],
    fut_quote: Optional[Dict[str, Any]],
    lot_size: int = 65,
    ist_hhmm: Optional[int] = None,
    session_blocked_reason: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Evaluates all 4 frozen `S2_05` (`V6B`) sub-engines at completed 1m bar `bar_idx`
    and returns `(executable_signals, engine_readiness_rows)`.
    """
    prm = meta_model.chosen_params
    oi_ratio_min = float(prm.get("oi_ratio_min", 1.05))
    opt_vwap_max = float(prm.get("opt_vwap_max", 0.94))
    sl_scale = float(prm.get("sl_scale", 1.0))
    p_min = float(meta_model.p_min)

    if ctx is None or bar_idx < 0 or bar_idx >= len(ctx["ts"]):
        empty_rows = []
        for eid, lbl, bk, win in (
            ("V6B_S1_OI", "S1 OI Writer Continuation", "SHORT_BOOK_SELL", "09:45–13:45"),
            ("V6B_S3_SPIKE", "S3 Morning IV Spike Trap", "SHORT_BOOK_SELL", "09:45–13:45"),
            ("V6B_S4_Z2", "S4 2-Sigma VWAP Exhaustion", "SHORT_BOOK_SELL", "09:45–13:45"),
            ("V6B_LBASIS_ATM", "L_Basis Trend Breakout", "LONG_BOOK_BUY", "09:45–12:45"),
        ):
            empty_rows.append(
                {
                    "engine_id": eid,
                    "label": lbl,
                    "book_type": bk,
                    "action_badge": "SELL 1 LOT" if bk == "SHORT_BOOK_SELL" else "BUY 1 LOT",
                    "window_ist": win,
                    "window_open": False,
                    "state": "WARMING_UP",
                    "candidate_contract": "Awaiting 09:15 IST live candles",
                    "gates_passed": 0,
                    "gates_total": 5,
                    "readiness_pct": 0,
                    "ml_prob": None,
                    "ml_p_min": p_min,
                    "gates": [],
                    "when_order_fires": "Waiting for NSE session 1m bars (09:15–09:45 ORB warmup).",
                }
            )
        return [], empty_rows

    ts = ctx["ts"][bar_idx]
    bar_hhmm = int(ts[11:13]) * 100 + int(ts[14:16])
    eff_hhmm = ist_hhmm if ist_hhmm is not None else bar_hhmm
    spot = ctx["c"][bar_idx]
    tpb = ctx["tpb"][bar_idx]
    step = float(ctx.get("strike_step", 50.0))
    atm = float(round(spot / step) * step)
    anchor_k = float(round(ctx["o"][0] / step) * step)

    adx_v = ctx["adx"][bar_idx]
    pdi_v = ctx["pdi"][bar_idx]
    mdi_v = ctx["mdi"][bar_idx]
    er_v = ctx["er"][bar_idx]
    rsi_v = ctx["rsi14"][bar_idx]
    vz_v = ctx["vwap_z"][bar_idx]
    hw_v = ctx["hw"][bar_idx]
    syn_b = ctx["syn_basis"][bar_idx]
    oib_v = ctx["oi_bull_diff"][bar_idx]

    fut_ltp = float((fut_quote or {}).get("fut_ltp") or 0.0)
    fut_atp = float((fut_quote or {}).get("fut_atp_vwap") or 0.0)
    has_live_fut = fut_ltp > 0 and fut_atp > 0

    executable: List[Dict[str, Any]] = []
    readiness_rows: List[Dict[str, Any]] = []

    def _finalize_row(
        engine_id: str,
        label: str,
        book_type: str,
        window_ist: str,
        win_open: bool,
        cand_eso: Optional[Tuple[str, float, str]],
        gates: List[Dict[str, Any]],
        is_credit: bool,
        is_bull: bool,
        ecode: int,
        trade_spec: Optional[Dict[str, Any]],
    ) -> None:
        q_info = quote_lookup.get(cand_eso, {}) if cand_eso else {}
        c_sym = q_info.get("trading_symbol") or (
            f"NIFTY {int(cand_eso[1])} {cand_eso[2]} ({cand_eso[0][5:]})" if cand_eso else "—"
        )
        c_ltp = float(q_info.get("ltp") or 0.0)
        if c_ltp <= 0 and cand_eso:
            ob_c = ctx["day_eso"].get(cand_eso, {}).get(ts)
            c_ltp = float(ob_c[3]) if ob_c else 0.0

        ml_res: Optional[Dict[str, Any]] = None
        if cand_eso:
            x_vec = extract_causal_features_s205(ctx, bar_idx, cand_eso, is_credit, is_bull, ecode, lot_size)
            ml_res = meta_model.explain_prediction(x_vec)
            gates.append(
                {
                    "name": "ML Meta-Prob",
                    "current": f"{ml_res['prob']:.2f}",
                    "target": f">= {p_min:.2f}",
                    "passed": bool(ml_res["passed"]),
                }
            )

        passed_cnt = sum(1 for g in gates if g["passed"])
        total_cnt = len(gates)
        missing = [g for g in gates if not g["passed"]]
        all_tech_and_ml = total_cnt > 0 and passed_cnt == total_cnt

        if session_blocked_reason:
            state = "RISK_PAUSED"
            hint = session_blocked_reason
        elif not win_open:
            if eff_hhmm < 945:
                mins_left = max(1, (9 * 60 + 45) - ((eff_hhmm // 100) * 60 + (eff_hhmm % 100)))
                state = "WARMUP_0945"
                hint = f"ORB30 warmup active — entry window opens at 09:45 IST (~{mins_left}m); {passed_cnt}/{total_cnt} pre-gates aligned."
            else:
                state = "WINDOW_CLOSED"
                hint = f"Entry window ({window_ist} IST) closed for today — managing open trails only."
        elif all_tech_and_ml:
            state = "READY_FIRE"
            hint = f"ALL {total_cnt}/{total_cnt} gates & ML ({ml_res['prob']:.2f} >= {p_min:.2f}) PASSED — executes at :00 candle close!"
            if trade_spec is not None and cand_eso is not None:
                trade_spec["ml_explain"] = ml_res
                trade_spec["ml_prob"] = ml_res["prob"] if ml_res else p_min
                executable.append(trade_spec)
        elif missing and len(missing) == 1 and missing[0]["name"] == "ML Meta-Prob":
            state = "ML_VETOED"
            hint = f"Technical gates met, vetoed by Causal ML ({missing[0]['current']} < {missing[0]['target']})."
        else:
            state = "WATCHING"
            top_need = ", ".join(f"{g['name']} ({g['current']} vs {g['target']})" for g in missing[:2])
            hint = f"{passed_cnt}/{total_cnt} gates met — awaiting {top_need}"

        readiness_rows.append(
            {
                "engine_id": engine_id,
                "label": label,
                "book_type": book_type,
                "action_badge": "SELL 1 LOT" if book_type == "SHORT_BOOK_SELL" else "BUY 1 LOT",
                "window_ist": window_ist,
                "window_open": win_open,
                "state": state,
                "candidate_contract": f"{c_sym}" + (f" @ ₹{c_ltp:.1f}" if c_ltp > 0 else ""),
                "gates_passed": passed_cnt,
                "gates_total": total_cnt,
                "readiness_pct": int(round((passed_cnt / max(1, total_cnt)) * 100)),
                "ml_prob": ml_res["prob"] if ml_res else None,
                "ml_p_min": p_min,
                "ml_components": ml_res,
                "gates": gates,
                "when_order_fires": hint,
            }
        )

    # ── 1. V6B_S1_OI (Institutional Option Writer Continuation — SHORT) ──
    exp_s = ctx["front_exp"] if ctx["front_dte"] >= 1 else ctx["next_exp"]
    s1_win = (bar_idx >= 30) and (945 <= bar_hhmm <= 1345)
    s1_bull_bias = spot >= tpb
    if s1_bull_bias:
        s1_eso = (exp_s, atm + step, "PE")
        spot_dist = spot - tpb
        spot_gate = {"name": "Spot vs VWAP", "current": f"{spot_dist:+.1f}pt", "target": ">= +10pt", "passed": spot >= tpb + 10.0}
        di_gate = {"name": "DI Trend", "current": f"+DI {pdi_v:.1f}/{mdi_v:.1f}", "target": "+DI > -DI", "passed": pdi_v > mdi_v}
        hw_gate = {"name": "15m Heavywt", "current": f"{hw_v:+.2f}", "target": ">= 0.00", "passed": hw_v >= 0.0}
    else:
        s1_eso = (exp_s, atm - step, "CE")
        spot_dist = spot - tpb
        spot_gate = {"name": "Spot vs VWAP", "current": f"{spot_dist:+.1f}pt", "target": "<= -10pt", "passed": spot <= tpb - 10.0}
        di_gate = {"name": "DI Trend", "current": f"-DI {mdi_v:.1f}/{pdi_v:.1f}", "target": "-DI > +DI", "passed": mdi_v > pdi_v}
        hw_gate = {"name": "15m Heavywt", "current": f"{hw_v:+.2f}", "target": "<= 0.00", "passed": hw_v <= 0.0}

    s1_ob = ctx["day_eso"].get(s1_eso, {}).get(ts)
    s1_st = ctx["opt_stats"].get(s1_eso)
    s1_vw_r = (s1_ob[3] / s1_st[0][bar_idx]) if (s1_ob and s1_st and s1_st[0][bar_idx] > 0) else 1.02
    s1_oir = s1_st[5][bar_idx] if s1_st else 1.00
    s1_liq_ok = bool(s1_ob and s1_ob[5] >= 3000 and s1_ob[3] >= 18.0)
    s1_gates = [
        spot_gate,
        di_gate,
        hw_gate,
        {"name": "Opt/VWAP", "current": f"{s1_vw_r:.3f}x", "target": f"<= {opt_vwap_max:.2f}x", "passed": bool(s1_ob and s1_vw_r <= opt_vwap_max and s1_liq_ok)},
        {"name": "3m OI Ratio", "current": f"{s1_oir:.3f}x", "target": f">= {oi_ratio_min:.2f}x", "passed": bool(s1_ob and s1_oir >= oi_ratio_min and s1_liq_ok)},
    ]
    s1_spec = {
        "engine_id": "V6B_S1_OI",
        "book_type": "SHORT_BOOK_SELL",
        "eso": s1_eso,
        "is_credit": True,
        "is_bull": s1_bull_bias,
        "sl_pct": 18.0 * sl_scale,
        "max_loss_inr": 2400.0 * sl_scale,
        "tp_pct": None,
        "ratchet_stages": [(12.0, 0.0, 3.5), (26.0, 11.0, 12.0), (46.0, 8.0, 26.0)],
        "max_hold_bars": 135,
        "time_stop_mins": 36,
        "time_min_gain_pct": 3.0,
        "opt_vwap_mult": 1.035,
        "vflip_pts": 12.0,
    }
    _finalize_row("V6B_S1_OI", "S1 OI Writer Continuation", "SHORT_BOOK_SELL", "09:45–13:45", s1_win, s1_eso, s1_gates, True, s1_bull_bias, 1, s1_spec)

    # ── 2. V6B_S3_SPIKE (Morning IV Spike Trap Exhaustion — SHORT) ──
    s3_win = (bar_idx >= 28) and (945 <= bar_hhmm <= 1345)
    best_s3_choice = None
    for ot_s3, bull_s3, sp_ok_s3, sp_tgt in (
        ("PE", True, spot >= tpb - 5.0, "Spot >= VWAP-5"),
        ("CE", False, spot <= tpb + 5.0, "Spot <= VWAP+5"),
    ):
        eso3 = (exp_s, anchor_k, ot_s3)
        ob3 = ctx["day_eso"].get(eso3, {}).get(ts)
        st3 = ctx["opt_stats"].get(eso3)
        op0 = st3[4] if st3 else 0.0
        pk3 = st3[2][bar_idx] if st3 else 0.0
        px3 = ob3[3] if ob3 else 0.0
        vw3 = st3[0][bar_idx] if st3 else 0.0
        pk_mult = (pk3 / op0) if op0 > 0 else 1.0
        pull_r = (px3 / pk3) if pk3 > 0 else 1.0
        vw_r3 = (px3 / vw3) if vw3 > 0 else 1.0
        liq3 = bool(ob3 and ob3[5] >= 3000 and px3 >= 18.0 and op0 >= 35.0)
        g3 = [
            {"name": "09:15 Spike", "current": f"{pk_mult:.2f}x", "target": ">= 1.35x", "passed": bool(op0 >= 35.0 and pk_mult >= 1.35)},
            {"name": "Pullback/Peak", "current": f"{pull_r:.2f}x", "target": "<= 0.84x", "passed": bool(liq3 and pull_r <= 0.84)},
            {"name": "Opt/VWAP", "current": f"{vw_r3:.2f}x", "target": "<= 0.98x", "passed": bool(liq3 and vw_r3 <= 0.98)},
            {"name": "Spot Align", "current": f"{spot - tpb:+.1f}pt", "target": sp_tgt, "passed": sp_ok_s3},
        ]
        sc3 = sum(1 for x in g3 if x["passed"])
        if best_s3_choice is None or sc3 > best_s3_choice[0]:
            best_s3_choice = (sc3, eso3, bull_s3, g3)

    _, s3_eso, s3_bull, s3_gates = best_s3_choice
    s3_spec = {
        "engine_id": "V6B_S3_SPIKE",
        "book_type": "SHORT_BOOK_SELL",
        "eso": s3_eso,
        "is_credit": True,
        "is_bull": s3_bull,
        "sl_pct": 20.0 * sl_scale,
        "max_loss_inr": 2100.0 * sl_scale,
        "tp_pct": None,
        "ratchet_stages": [(11.0, 0.0, 3.2), (24.0, 10.0, 12.0), (44.0, 8.0, 26.0)],
        "max_hold_bars": 120,
        "time_stop_mins": 34,
        "time_min_gain_pct": 3.0,
        "opt_vwap_mult": 1.03,
        "vflip_pts": 14.0,
    }
    _finalize_row("V6B_S3_SPIKE", "S3 Morning IV Spike Trap", "SHORT_BOOK_SELL", "09:45–13:45", s3_win, s3_eso, s3_gates, True, s3_bull, 3, s3_spec)

    # ── 3. V6B_S4_Z2 (2-Sigma VWAP Exhaustion Reversal — SHORT) ──
    s4_win = (bar_idx >= 28) and (945 <= bar_hhmm <= 1345)
    fade_overbought = vz_v >= 0.0
    if fade_overbought:
        s4_eso = (exp_s, atm, "CE")
        s4_bull = False
        rev_ok = (ctx["c"][bar_idx] < ctx["o"][bar_idx]) and (bar_idx >= 1 and ctx["c"][bar_idx] < ctx["h"][bar_idx - 1])
        z_gate = {"name": "VWAP Z-Score", "current": f"{vz_v:+.2f}σ", "target": ">= +2.00σ", "passed": vz_v >= 2.0}
        rsi_gate = {"name": "Wilder RSI14", "current": f"{rsi_v:.1f}", "target": ">= 68.0", "passed": rsi_v >= 68.0}
    else:
        s4_eso = (exp_s, atm, "PE")
        s4_bull = True
        rev_ok = (ctx["c"][bar_idx] > ctx["o"][bar_idx]) and (bar_idx >= 1 and ctx["c"][bar_idx] > ctx["l"][bar_idx - 1])
        z_gate = {"name": "VWAP Z-Score", "current": f"{vz_v:+.2f}σ", "target": "<= -2.00σ", "passed": vz_v <= -2.0}
        rsi_gate = {"name": "Wilder RSI14", "current": f"{rsi_v:.1f}", "target": "<= 32.0", "passed": rsi_v <= 32.0}

    s4_ob = ctx["day_eso"].get(s4_eso, {}).get(ts)
    s4_liq = bool(s4_ob and s4_ob[5] >= 3000 and s4_ob[3] >= 20.0)
    s4_gates = [
        {"name": "Range Regime", "current": f"ADX {adx_v:.1f}/ER {er_v:.2f}", "target": "<=25 / <=0.35", "passed": adx_v <= 25.0 and er_v <= 0.35},
        z_gate,
        rsi_gate,
        {"name": "Reversal Bar", "current": "CONFIRMED" if (rev_ok and s4_liq) else "PENDING", "target": "1m Reversal", "passed": bool(rev_ok and s4_liq)},
    ]
    s4_spec = {
        "engine_id": "V6B_S4_Z2",
        "book_type": "SHORT_BOOK_SELL",
        "eso": s4_eso,
        "is_credit": True,
        "is_bull": s4_bull,
        "sl_pct": 20.0 * sl_scale,
        "max_loss_inr": 2000.0 * sl_scale,
        "tp_pct": 22.0,
        "ratchet_stages": [(11.0, 0.0, 3.0)],
        "max_hold_bars": 50,
        "time_stop_mins": 32,
        "time_min_gain_pct": 2.5,
        "opt_vwap_mult": 0.0,
        "vflip_pts": 0.0,
    }
    _finalize_row("V6B_S4_Z2", "S4 2-Sigma VWAP Exhaustion", "SHORT_BOOK_SELL", "09:45–13:45", s4_win, s4_eso, s4_gates, True, s4_bull, 2, s4_spec)

    # ── 4. V6B_LBASIS_ATM (Sniper Trend & Basis Breakout — LONG) ──
    exp_l = ctx["front_exp"] if ctx["front_dte"] >= 2 else ctx["next_exp"]
    lb_win = (bar_idx >= 30) and (945 <= bar_hhmm <= 1245)
    orb_mid = 0.5 * (ctx["orb30_h"] + ctx["orb30_l"])
    lb_bull = spot >= orb_mid
    if lb_bull:
        lb_eso = (exp_l, atm, "CE")
        fut_vwap_ok = (not has_live_fut) or (fut_ltp >= fut_atp - 2.0)
        orb_gate = {"name": "ORB30 Break", "current": f"{spot:.0f}", "target": f">= {ctx['orb30_h'] + 8.0:.0f}", "passed": spot >= ctx["orb30_h"] + 8.0}
        bas_gate = {"name": "Basis + FutVWAP", "current": f"{syn_b:+.1f}pt", "target": ">= +6.0pt", "passed": syn_b >= 6.0 and fut_vwap_ok}
        oi_hw_gate = {"name": "OI Diff & HW", "current": f"OI {oib_v:+.2f}/HW {hw_v:+.2f}", "target": ">=+0.05/>=+0.33", "passed": oib_v >= 0.05 and hw_v >= 0.33}
    else:
        lb_eso = (exp_l, atm, "PE")
        fut_vwap_ok = (not has_live_fut) or (fut_ltp <= fut_atp + 2.0)
        orb_gate = {"name": "ORB30 Break", "current": f"{spot:.0f}", "target": f"<= {ctx['orb30_l'] - 8.0:.0f}", "passed": spot <= ctx["orb30_l"] - 8.0}
        bas_gate = {"name": "Basis + FutVWAP", "current": f"{syn_b:+.1f}pt", "target": "<= -6.0pt", "passed": syn_b <= -6.0 and fut_vwap_ok}
        oi_hw_gate = {"name": "OI Diff & HW", "current": f"OI {oib_v:+.2f}/HW {hw_v:+.2f}", "target": "<=-0.05/<=-0.33", "passed": oib_v <= -0.05 and hw_v <= -0.33}

    lb_ob = ctx["day_eso"].get(lb_eso, {}).get(ts)
    lb_liq = bool(lb_ob and lb_ob[5] >= 3000 and lb_ob[3] >= 15.0)
    lb_gates = [
        {"name": "Trend ADX/ER", "current": f"ADX {adx_v:.1f}/ER {er_v:.2f}", "target": ">=24 / >=0.36", "passed": adx_v >= 24.0 and er_v >= 0.36},
        orb_gate,
        bas_gate,
        {"name": "OI & Liquidity", "current": oi_hw_gate["current"], "target": oi_hw_gate["target"], "passed": bool(oi_hw_gate["passed"] and lb_liq)},
    ]
    lb_spec = {
        "engine_id": "V6B_LBASIS_ATM",
        "book_type": "LONG_BOOK_BUY",
        "eso": lb_eso,
        "is_credit": False,
        "is_bull": lb_bull,
        "sl_pct": 22.0 * sl_scale,
        "max_loss_inr": 2000.0 * sl_scale,
        "tp_pct": None,
        "ratchet_stages": [(18.0, 0.0, 5.0), (32.0, 14.0, 16.0), (55.0, 10.0, 35.0)],
        "max_hold_bars": 75,
        "time_stop_mins": 28,
        "time_min_gain_pct": 4.0,
        "opt_vwap_mult": 0.0,
        "vflip_pts": 12.0,
    }
    _finalize_row("V6B_LBASIS_ATM", "L_Basis Trend Breakout", "LONG_BOOK_BUY", "09:45–12:45", lb_win, lb_eso, lb_gates, False, lb_bull, 4, lb_spec)

    return executable, readiness_rows
