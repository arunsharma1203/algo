"""
V2 Option Sniffer — Book B (Paid Flow Convexity) & V1 Stop/Cap Overshoot Reconciler.

Trace & Improvements over V1 (`SHADOW_BOOK_B_PAID_FLOW`):
  1. Reconciles V1's `22%` / `Rs 2,200` stop formula (`s205_shadow_collector.py:1660-1665`),
     showing exactly why `ROUND_FLOOR` tick rounding + exit spread/slippage + gap + `Rs 67-86`
     statutory charges can cause realized loss to overshoot the planned Rs 2,200 gross cap.
  2. Freezes V2 Book B candidate:
       - `10:15-13:00 IST` entry window (skips opening 09:45-10:14 fakeout traps).
       - Intraday change in futures-minus-spot basis (`basis_t - basis_open_ref`) from
         synchronized observations of the SAME futures contract (`abs(ts_fut - ts_spot) <= 5000ms`).
         Requires `delta_basis >= +3.0` (bullish CE) or `<= -3.0` (bearish PE).
         Missing opening reference or mid-day futures contract change fails closed.
       - Separately records weekly synthetic basis (`K + CE_mid - PE_mid - Spot`) as a
         distinct research variant (`RESEARCH_VARIANT_WEEKLY_SYN_BASIS`).
       - One paid option lot, `<= Rs 11,500` outlay cap; if ATM fails ONLY `CashCap`, evaluates
         exact 1 strike farther OTM (`+-50pt`) under identical liquidity, OBI, freshness & risk checks.
       - Planned loss = `min(20% of filled outlay, Rs 1,800)` using `adverse_buy` ceiling tick on
         hard stop so planned gross loss stays strictly `<= budget`; reports planned loss,
         actual fill loss, slippage/gap overshoot, and fees separately.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.common.options_accounting import round_to_nse_tick
from app.analytics.option_sniffer_v2.v2_config import (
    trading_sessions_between,
)


def compute_close_only_long_ratchet(
    entry_price: float,
    best_close_pts: float,
    prev_ratchet_stop: Optional[float],
    ratchet_stages: List[Tuple[float, float, float]],
) -> Optional[float]:
    """
    Pure close-only trailing ratchet stop updated strictly from completed bar close.
    Never inspects intrabar high/low wicks.
    """
    if entry_price <= 0:
        return prev_ratchet_stop
    ratchet = prev_ratchet_stop
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
from app.analytics.option_sniffer_v2.v2_data_contract import (
    check_cross_leg_skew,
    check_spot_futures_sync,
    sweep_observed_l5_book,
    validate_nifty_option_identity,
)
from app.analytics.option_sniffer_v2.v2_paper_simulator import V2PaperSimulator


def reconcile_v1_book_b_stop_and_overshoot(
    v1_ro_conn: Optional[sqlite3.Connection] = None,
    v1_prereg_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Traces V1 Book B's stop formula from SHADOW_PREREGISTRATION.json -> s205_shadow_collector.py
    and reconciles every closed V1 Book B trade in `option_sniffer_advance_s205_shadow_packages`.
    """
    default_prereg = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "option_sniffer_advance_s205",
            "SHADOW_PREREGISTRATION.json",
        )
    )
    target_prereg = v1_prereg_path or default_prereg
    with open(target_prereg, "r", encoding="utf-8") as fh:
        v1_cfg = json.load(fh)
    b_cfg = v1_cfg["shadow_books"]["SHADOW_BOOK_B_PAID_FLOW"]
    lot_sz = int(v1_cfg["account_envelope"]["lot_size_nifty"])

    formula_audit = {
        "v1_config_hard_sl_pct": float(b_cfg["hard_sl_pct"]),
        "v1_config_max_planned_stop_loss_inr": float(b_cfg["max_planned_stop_loss_inr"]),
        "v1_code_location": "backend/app/analytics/option_sniffer_advance_s205/s205_shadow_collector.py:1660-1665",
        "v1_code_formula": (
            "capped_sl_pct = round(max(6.0, min(22.0, (2200.0 / outlay_inr) * 100.0)), 2); "
            "hard_stop_px = round_to_nse_tick(entry_px * (1.0 - capped_sl_pct / 100.0), mode='adverse_sell')"
        ),
        "overshoot_mechanisms": [
            {
                "source": "1_adverse_sell_tick_rounding",
                "detail": "Rounding hard_stop_px DOWN via ROUND_FLOOR adds up to Rs 0.0499 * 65 = Rs 3.25 gross loss above Rs 2,200 before execution.",
            },
            {
                "source": "2_exit_sweep_and_1tick_adverse_slippage",
                "detail": "Hard-SL trigger checks swept bid <= hard_stop_px, so exit fill absorbs bid-ask spread + 1 adverse tick (-Rs 0.05) + any 1m/intrabar gap below hard_stop_px.",
            },
            {
                "source": "3_uncapped_statutory_charges",
                "detail": "V1 planned cap (Rs 2,200) covers gross premium move only; brokerage (Rs 40) + sell STT (0.15%) + NSE/SEBI/GST/Stamp (~Rs 67-86) sit strictly on top (~Rs 2,275+ net loss even on a flat trigger fill).",
            },
            {
                "source": "4_single_trade_breach_above_Rs10000_outlay",
                "detail": "At outlay = Rs 11,500 (entry = Rs 176.90), 22% = Rs 2,530 is clamped to 19.13% (Rs 2,199.95 gross planned) + ~Rs 75 fees = ~Rs 2,275 + gap/slippage. Counterfactual Nov-16 09:46 uncapped 22% at Rs 15,194 lost Rs 3,593.",
            },
        ],
    }

    reconciled_trades: List[Dict[str, Any]] = []
    if v1_ro_conn is not None:
        rows = v1_ro_conn.execute(
            """
            SELECT package_id, session_date, expiry, entry_bar_ts, exit_bar_ts, exit_reason,
                   legs_state_json, accounting_json, gross_pnl_inr, total_charges_inr, net_pnl_inr,
                   cash_reserved_inr
            FROM option_sniffer_advance_s205_shadow_packages
            WHERE book_id = 'SHADOW_BOOK_B_PAID_FLOW'
            ORDER BY entry_bar_ts ASC;
            """
        ).fetchall()
        for r in rows:
            ls = json.loads(r["legs_state_json"] or "{}")
            ll = ls.get("long_leg") or {}
            epx = float(ll.get("entry_price") or 0.0)
            outlay = round(epx * lot_sz, 2)
            hard_stop = float(ls.get("hard_stop") or 0.0)
            planned_gross = round(max(0.0, (epx - hard_stop) * lot_sz), 2)
            ratchet_stop = ls.get("ratchet_stop")
            acc = json.loads(r["accounting_json"]) if r["accounting_json"] else {}
            exit_px_eff = float((acc.get("legs") or [{}])[0].get("exit_price_effective") or 0.0)
            ratchet_overshoot_pts = (
                round(max(0.0, float(ratchet_stop) - exit_px_eff), 2)
                if ratchet_stop is not None and r["exit_reason"] == "RATCHET_STOP_HIT" and exit_px_eff > 0
                else 0.0
            )
            reconciled_trades.append(
                {
                    "package_id": r["package_id"],
                    "session_date": r["session_date"],
                    "symbol": ll.get("trading_symbol"),
                    "entry_price": epx,
                    "outlay_inr": outlay,
                    "hard_stop_px": hard_stop,
                    "planned_hard_stop_loss_inr": planned_gross,
                    "ratchet_stop_px": ratchet_stop,
                    "exit_price_effective": exit_px_eff,
                    "exit_reason": r["exit_reason"],
                    "ratchet_tick_gap_overshoot_pts": ratchet_overshoot_pts,
                    "ratchet_tick_gap_overshoot_inr": round(ratchet_overshoot_pts * lot_sz, 2),
                    "gross_pnl_inr": float(r["gross_pnl_inr"]),
                    "total_charges_inr": float(r["total_charges_inr"]),
                    "net_pnl_inr": float(r["net_pnl_inr"]),
                }
            )

    return {
        "formula_audit": formula_audit,
        "reconciled_v1_book_b_trades": reconciled_trades,
    }


def compute_weekly_synthetic_basis_pts(
    spot_close: float,
    atm_strike: float,
    atm_ce_quote: Optional[Dict[str, Any]],
    atm_pe_quote: Optional[Dict[str, Any]],
    max_skew_ms: int = 5000,
) -> Optional[float]:
    """
    Research-only weekly put-call parity synthetic basis: `(K + CE_mid - PE_mid) - Spot`.
    Fails closed (`None`) if ATM CE/PE are missing or skewed.
    """
    if spot_close <= 0 or atm_strike <= 0 or not atm_ce_quote or not atm_pe_quote:
        return None
    if not validate_nifty_option_identity(atm_ce_quote)[0] or not validate_nifty_option_identity(atm_pe_quote)[0]:
        return None
    skew_ok, _, _ = check_cross_leg_skew(atm_ce_quote, atm_pe_quote, max_skew_ms=max_skew_ms, require_depth_ts=False)
    if not skew_ok:
        return None
    ce_bid = float(atm_ce_quote.get("raw_bid") or atm_ce_quote.get("bid") or 0.0)
    ce_ask = float(atm_ce_quote.get("raw_ask") or atm_ce_quote.get("ask") or 0.0)
    pe_bid = float(atm_pe_quote.get("raw_bid") or atm_pe_quote.get("bid") or 0.0)
    pe_ask = float(atm_pe_quote.get("raw_ask") or atm_pe_quote.get("ask") or 0.0)
    if ce_bid <= 0 or ce_ask < ce_bid or pe_bid <= 0 or pe_ask < pe_bid:
        return None
    ce_mid = 0.5 * (ce_bid + ce_ask)
    pe_mid = 0.5 * (pe_bid + pe_ask)
    return round((atm_strike + ce_mid - pe_mid) - spot_close, 4)


def evaluate_book_b_entry_signal(
    cfg: Dict[str, Any],
    session_date: str,
    bar_ts: str,
    ist_hhmm: int,
    decision_ts_ms: int,
    spot_close: float,
    spot_vwap: Optional[float],
    orb30_h: Optional[float],
    orb30_l: Optional[float],
    adx14: Optional[float],
    er20: Optional[float],
    spot_quote: Optional[Dict[str, Any]],
    fut_quote: Optional[Dict[str, Any]],
    open_basis_ref: Optional[Dict[str, Any]],
    expiries: List[str],
    contracts_lookup: Dict[Tuple[str, float, str], Dict[str, Any]],
    quote_lookup: Dict[Tuple[str, float, str], Dict[str, Any]],
    observed_sessions: Optional[set[str]] = None,
    extra_holidays: Optional[List[str]] = None,
    # Isolated ablation toggles (default to V2 frozen spec!)
    variant_code: str = "V2_BOOK_B_FROZEN",
    use_basis_delta: bool = True,
    use_weekly_syn_basis: bool = False,
    window_start_override: Optional[int] = None,
    window_end_override: Optional[int] = None,
    allow_single_otm_fallback: Optional[bool] = None,
    min_raw_basis_abs_v1: float = 5.0,
    min_er_override: Optional[float] = None,
    min_adx_override: Optional[float] = None,
    min_obi_override: Optional[float] = None,
    vrp_ratio: Optional[float] = None,
    atm_iv_pct: Optional[float] = None,
    har_rv_pct: Optional[float] = None,
    enforce_vrp_gate: bool = False,
    max_vrp_ratio: float = 1.15,
    vrp_status: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Evaluates V2 Book B (and single-factor ablation variants cleanly when flags are explicitly passed).
    """
    cfg_b = cfg["books"]["V2_BOOK_B_PAID_FLOW"]
    hyg = cfg["quote_hygiene"]

    win_start = int(window_start_override if window_start_override is not None else cfg_b["entry_window_start_hhmm"])
    win_end = int(window_end_override if window_end_override is not None else cfg_b["entry_window_end_hhmm"])
    win_ok = win_start <= ist_hhmm <= win_end

    if orb30_h is None or orb30_l is None or orb30_h <= 0 or orb30_l <= 0 or spot_close <= 0 or not spot_vwap or spot_vwap <= 0:
        return {
            "book_id": cfg_b["book_id"],
            "candidate_variant": variant_code,
            "passed": False,
            "state": "WARMUP",
            "reject_reason": "ORB30_OR_VWAP_INCOMPLETE",
            "gates": [],
        }

    is_bull = spot_close > orb30_h and spot_close > spot_vwap
    is_bear = spot_close < orb30_l and spot_close < spot_vwap
    dir_ok = is_bull or is_bear
    ot = "CE" if (is_bull or (not is_bear and spot_close >= spot_vwap)) else "PE"

    target_exp: Optional[str] = None
    max_tdte = int(cfg_b.get("max_trading_dte", 12))
    for exp_s in sorted(set(expiries)):
        if exp_s < session_date:
            continue
        tdte = trading_sessions_between(session_date, exp_s, observed_sessions=observed_sessions, extra_holidays=extra_holidays)
        if int(cfg_b["min_trading_dte"]) <= tdte <= max_tdte:
            target_exp = exp_s
            break

    atm = round(spot_close / 50.0) * 50.0
    atm_ce_q = quote_lookup.get((target_exp or "", atm, "CE")) if target_exp else None
    atm_pe_q = quote_lookup.get((target_exp or "", atm, "PE")) if target_exp else None
    syn_weekly_basis = compute_weekly_synthetic_basis_pts(
        spot_close, atm, atm_ce_q, atm_pe_q, max_skew_ms=int(hyg["max_cross_leg_skew_ms"])
    )

    # Basis check: V2 Delta Basis vs V1 Raw Basis vs Weekly Synthetic Research Variant
    sync_fut = check_spot_futures_sync(
        spot_quote=spot_quote,
        fut_quote=fut_quote,
        max_skew_ms=int(hyg["max_spot_fut_sync_skew_ms"]),
        max_age_ms=int(hyg["max_entry_quote_age_ms"]),
        now_ms=decision_ts_ms,
        expected_fut_contract=open_basis_ref if use_basis_delta else None,
    )
    basis_ok = False
    basis_reason = "UNCHECKED"
    delta_basis: Optional[float] = None

    if use_weekly_syn_basis:
        if syn_weekly_basis is None:
            basis_reason = "MISSING_WEEKLY_SYN_BASIS"
        else:
            delta_basis = syn_weekly_basis
            basis_ok = (syn_weekly_basis >= float(cfg_b["min_intraday_basis_change_abs_pts"])) if ot == "CE" else (syn_weekly_basis <= -float(cfg_b["min_intraday_basis_change_abs_pts"]))
            basis_reason = f"SynW={syn_weekly_basis:+.1f}pt"
    elif use_basis_delta:
        if not open_basis_ref or not open_basis_ref.get("ok"):
            basis_reason = str((open_basis_ref or {}).get("reason") or "MISSING_OPEN_BASIS_REF")
        elif not sync_fut.get("ok"):
            basis_reason = str(sync_fut.get("reason"))
        elif (
            sync_fut["fut_instrument_key"] != open_basis_ref.get("fut_instrument_key")
            or sync_fut.get("fut_expiry") != open_basis_ref.get("fut_expiry")
        ):
            basis_reason = "FUTURES_CONTRACT_ROLLED_MID_SESSION"
        else:
            delta_basis = round(float(sync_fut["basis_pts"]) - float(open_basis_ref["basis_pts"]), 4)
            req = float(cfg_b["min_intraday_basis_change_abs_pts"])
            basis_ok = (delta_basis >= req) if ot == "CE" else (delta_basis <= -req)
            basis_reason = f"dBasis={delta_basis:+.1f}pt (now={sync_fut['basis_pts']:+.1f}, open={open_basis_ref['basis_pts']:+.1f})"
    else:
        if not sync_fut.get("ok"):
            basis_reason = str(sync_fut.get("reason"))
        else:
            raw_b = float(sync_fut["basis_pts"])
            basis_ok = (raw_b >= min_raw_basis_abs_v1) if ot == "CE" else (raw_b <= -min_raw_basis_abs_v1)
            basis_reason = f"rawBasis={raw_b:+.1f}pt"

    adx_min = float(min_adx_override if min_adx_override is not None else cfg_b["min_adx14"])
    er_min = float(min_er_override if min_er_override is not None else cfg_b["min_er20"])
    obi_min = float(min_obi_override if min_obi_override is not None else cfg_b["min_l5_obi"])
    if adx14 is None or er20 is None:
        adx_ok = False
        adx_cur = "WARMUP"
    else:
        adx_ok = bool(float(adx14) >= adx_min and float(er20) >= er_min)
        adx_cur = f"{float(adx14):.1f}/{float(er20):.2f}"

    def _eval_strike(k_val: float) -> Dict[str, Any]:
        meta = contracts_lookup.get((target_exp or "", k_val, ot)) if target_exp else None
        q = quote_lookup.get((target_exp or "", k_val, ot)) if target_exp else None
        if not meta or not q:
            return {"ok": False, "l5_ok": False, "cash_ok": False, "reason": "MISSING_CONTRACT_OR_QUOTE", "outlay": 0.0}
        id_ok, id_rsn = validate_nifty_option_identity(meta)
        if not id_ok:
            return {"ok": False, "l5_ok": False, "cash_ok": False, "reason": id_rsn, "outlay": 0.0}
        lot_sz = int(meta["lot_size"])
        sw = sweep_observed_l5_book(
            quote=q,
            side="BUY",
            target_qty=int(cfg_b["lot_count"]) * lot_sz,
            lot_size=lot_sz,
            adverse_ticks=int(hyg["adverse_latency_ticks"]),
            max_age_ms=int(hyg["max_entry_quote_age_ms"]),
            now_ms=decision_ts_ms,
            max_spread_pct=float(hyg["max_primary_spread_pct"]),
            require_observed_l5=bool(hyg["require_observed_l5_book_for_entry"]),
        )
        obi = float(q.get("book_obi_l5") if q.get("book_obi_l5") is not None else -999.0)
        l5_ok = bool(sw.get("ok") and obi >= obi_min)
        outlay = round(float(sw["fill_price"]) * lot_sz, 2) if sw.get("ok") else 0.0
        cash_ok = bool(sw.get("ok") and 0.0 < outlay <= float(cfg_b["max_entry_outlay_inr"]))
        rsn = "OK" if (l5_ok and cash_ok) else (sw.get("reason") if not sw.get("ok") else (f"LOW_OBI:{obi:+.2f}" if not l5_ok else f"CASHCAP:{outlay:.0f}"))
        return {
            "ok": bool(l5_ok and cash_ok),
            "l5_ok": l5_ok,
            "cash_ok": cash_ok,
            "reason": rsn,
            "contract": meta,
            "quote": q,
            "sweep": sw,
            "outlay": outlay,
            "obi": obi,
        }

    atm_eval = _eval_strike(atm)
    chosen_eval = atm_eval
    used_otm = False

    allow_otm = bool(cfg_b["allow_single_otm_step_on_cash_cap_only"] if allow_single_otm_fallback is None else allow_single_otm_fallback)
    # Only fall back 1 strike OTM if trend, window, basis, ADX/ER, and ATM L5 depth ALL passed, and ONLY CashCap failed!
    if (
        allow_otm
        and target_exp is not None
        and win_ok
        and dir_ok
        and basis_ok
        and adx_ok
        and atm_eval.get("l5_ok")
        and (not atm_eval.get("cash_ok"))
    ):
        step = float(cfg_b["otm_step_pts"])
        otm_k = (atm + step) if ot == "CE" else (atm - step)
        otm_eval = _eval_strike(otm_k)
        chosen_eval = otm_eval
        used_otm = True

    gates = [
        {"name": "Window", "passed": win_ok, "current": f"{ist_hhmm:04d}", "target": f"{win_start:04d}..{win_end:04d}"},
        {"name": "ORB30+VWAP", "passed": dir_ok, "current": f"{spot_close:.1f}", "target": f">{orb30_h:.0f} or <{orb30_l:.0f}"},
        {"name": "BasisGate", "passed": basis_ok, "current": basis_reason, "target": ">=+3 / <=-3" if use_basis_delta else "abs>=5"},
        {"name": "ADX/ER", "passed": adx_ok, "current": adx_cur, "target": f">={adx_min:.0f}/{er_min:.2f}"},
        {"name": "ObsL5Depth", "passed": bool(chosen_eval.get("l5_ok")), "current": chosen_eval.get("reason", "NA"), "target": f">=1lot & OBI>={obi_min:.2f}"},
        {"name": "CashCap", "passed": bool(chosen_eval.get("cash_ok")), "current": f"Rs{chosen_eval.get('outlay', 0):,.0f}", "target": f"<=Rs{cfg_b['max_entry_outlay_inr']:,.0f}"},
    ]
    if enforce_vrp_gate:
        vrp_ok = vrp_ratio is not None and float(vrp_ratio) <= float(max_vrp_ratio)
        vrp_cur = f"{float(vrp_ratio):.2f}" if vrp_ratio is not None else str(vrp_status or "NA")
        gates.append({"name": "VRPRatio", "passed": vrp_ok, "current": vrp_cur, "target": f"<={float(max_vrp_ratio):.2f}"})

    all_passed = all(g["passed"] for g in gates)
    reject_reason = None if all_passed else next((f"{g['name']}:{g['current']}" for g in gates if not g["passed"]), "BLOCKED")
    return {
        "book_id": cfg_b["book_id"],
        "candidate_variant": variant_code,
        "passed": all_passed,
        "state": "READY_FIRE" if all_passed else "WATCHING",
        "reject_reason": reject_reason,
        "option_type": ot,
        "expiry": target_exp,
        "used_single_otm_fallback": used_otm,
        "contract": chosen_eval.get("contract"),
        "quote": chosen_eval.get("quote"),
        "basis_meta": {
            "variant": variant_code,
            "delta_basis_pts": delta_basis,
            "current_basis_pts": sync_fut.get("basis_pts") if sync_fut.get("ok") else None,
            "open_basis_pts": (open_basis_ref or {}).get("basis_pts"),
            "fut_instrument_key": sync_fut.get("fut_instrument_key"),
            "weekly_synthetic_basis_pts": syn_weekly_basis,
        },
        "vrp_meta": {
            "vrp_ratio": vrp_ratio,
            "atm_iv_pct": atm_iv_pct,
            "har_rv_pct": har_rv_pct,
            "enforce_vrp_gate": enforce_vrp_gate,
            "max_vrp_ratio": max_vrp_ratio,
            "vrp_status": vrp_status,
        },
        "gates": gates,
    }


def manage_open_book_b_on_bar(
    sim: V2PaperSimulator,
    pkg: Dict[str, Any],
    session_date: str,
    bar_ts: str,
    ist_hhmm: int,
    decision_ts_ms: int,
    spot_close: float,
    spot_vwap: float,
    quote_by_ikey: Dict[str, Dict[str, Any]],
    stop_pct_override: Optional[float] = None,
    stop_max_inr_override: Optional[float] = None,
    stagnation_mins_override: Optional[int] = None,
    stagnation_min_gain_override: Optional[float] = None,
    vflip_pts_override: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Manages open Book B Paid Flow trade strictly on completed bar close + executable bid."""
    ls = dict(pkg["legs_state"])
    ll = ls["long_leg"]
    lq = quote_by_ikey.get(ll["instrument_key"])
    cfg_b = sim.config["books"]["V2_BOOK_B_PAID_FLOW"]
    hyg = sim.config["quote_hygiene"]

    sw = sweep_observed_l5_book(
        quote=lq,
        side="SELL",
        target_qty=int(ll["qty"]),
        lot_size=int(ll["lot_size"]),
        adverse_ticks=sim._effective_slippage_ticks(),
        max_age_ms=int(hyg["max_exit_quote_age_ms"]),
        now_ms=decision_ts_ms,
        max_spread_pct=None,
        require_observed_l5=False,
        allow_fresh_l1_only=True,
    )
    if not sw.get("ok"):
        if ist_hhmm >= int(cfg_b["eod_squareoff_hhmm"]):
            return sim._mark_unresolved_or_unpriced(pkg, decision_ts_ms, str(sw.get("reason")), bar_ts)
        return None

    close_px = float(sw["fill_price"])
    entry_px = float(ll["entry_price"])
    gain_pct = ((close_px - entry_px) / max(0.05, entry_px)) * 100.0
    held_mins = max(0, int((decision_ts_ms - int(pkg["entry_ts_ms"])) // 60000))

    # Allow optional stop override ONLY for historical V1 ablation parity comparison
    hard_stop_px = float(ls["hard_stop"])
    if stop_pct_override is not None and stop_max_inr_override is not None:
        outlay = entry_px * int(ll["qty"])
        capped_pct = round(max(6.0, min(float(stop_pct_override), (float(stop_max_inr_override) / max(1.0, outlay)) * 100.0)), 2)
        hard_stop_px = round_to_nse_tick(entry_px * (1.0 - capped_pct / 100.0), mode="adverse_sell")

    ratchet_px = ls.get("ratchet_stop")
    eff_stop = max(hard_stop_px, float(ratchet_px)) if ratchet_px is not None else hard_stop_px

    stag_mins = int(stagnation_mins_override if stagnation_mins_override is not None else cfg_b["stagnation_mins"])
    stag_gain = float(stagnation_min_gain_override if stagnation_min_gain_override is not None else cfg_b["stagnation_min_gain_pct"])
    vf_pts = float(vflip_pts_override if vflip_pts_override is not None else cfg_b["vflip_pts"])

    is_bull = bool(ls.get("is_bull", True))
    vflip_hit = held_mins >= 4 and (
        (is_bull and spot_close < spot_vwap - vf_pts)
        or ((not is_bull) and spot_close > spot_vwap + vf_pts)
    )

    exit_reason: Optional[str] = None
    if ist_hhmm >= int(cfg_b["eod_squareoff_hhmm"]):
        exit_reason = "EOD_SQUAREOFF_1515"
    elif close_px <= eff_stop:
        exit_reason = (
            "CLOSE_ONLY_RATCHET_STOP"
            if (ratchet_px is not None and float(ratchet_px) > hard_stop_px + 0.01)
            else "PLANNED_HARD_SL_HIT"
        )
    elif held_mins >= stag_mins and gain_pct < stag_gain:
        exit_reason = "STAGNATION_TIME_STOP"
    elif vflip_hit:
        exit_reason = "SPOT_VWAP_FLIP_EXIT"

    if exit_reason:
        return sim.close_package_market_or_expiry(
            package_id=pkg["package_id"],
            session_date=session_date,
            bar_ts=bar_ts,
            decision_ts_ms=decision_ts_ms,
            exit_reason=exit_reason,
            long_or_wing_quote=lq,
            reference_spot=spot_close,
        )

    prev_best = float(ls.get("best_close_pts") or entry_px)
    best_close = max(prev_best, close_px)
    stages = [(float(s[0]), float(s[1]), float(s[2])) for s in cfg_b["ratchet_stages"]]
    new_r = compute_close_only_long_ratchet(entry_px, best_close, ratchet_px, stages)
    ls["best_close_pts"] = best_close
    ls["ratchet_stop"] = new_r
    upd = {**pkg, "legs_state": ls}
    sim.open_packages[pkg["package_id"]] = upd
    sim._all_pkgs_by_id[pkg["package_id"]] = upd
    if sim.run_mode == "FORWARD_PAPER" or new_r != ratchet_px or best_close != prev_best:
        sim.store.execute_atomic_transition([], [], upd, None)
    return None
