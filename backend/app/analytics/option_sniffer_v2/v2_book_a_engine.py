"""
V2 Option Sniffer — Book A (Proposed Phase-4 O3 T-1 -> T0 Peak-Theta Credit Spread).

Strict Governance Invariant:
  - By default (`V2_PREREGISTRATION_V1.json`), `V2_BOOK_A_PROPOSED_O3` has
    `autonomous_entries_enabled = False` and fails closed with `UNVERIFIED_SPEC_CONFLICT`
    because 6 material specifications differ from Phase-4 O3 (`5b7455b48c242682...`).
  - Research replay may opt in explicitly via `allow_unverified_book_a_in_replay=True`,
    tagging all resulting signals/trades as `HYPOTHETICAL_MODIFIED_O3_NOT_PHASE4_EQUIVALENT`.
  - When holding overnight across `T-1 -> T0`:
      * Reserves capital across the session boundary (`reserved_capital_inr`).
      * Tracks entry-day MTM, `T0 09:15` opening overnight gap, worst gap loss, and expiry intraday P&L separately.
      * Forces exit at `14:45 IST` on expiry day; if quotes are missing/stale/skewed at `14:45`,
        NEVER manufactures a cutoff fill — persists `UNRESOLVED_EXPOSURE_PAST_1445` and closes on
        the next fresh synchronized quote (`< 15:30`) or `15:30` observed NSE FSP settlement.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from app.analytics.option_sniffer_v2.v2_config import (
    is_nse_trading_session,
    trading_sessions_between,
)
from app.analytics.option_sniffer_v2.v2_data_contract import (
    check_cross_leg_skew,
    sweep_observed_l5_book,
)
from app.analytics.option_sniffer_v2.v2_paper_simulator import (
    V2PaperSimulator,
    make_stable_id,
)


def compute_vix_prior_10d_sma(prior_vix_closes: List[float], lookback: int = 10) -> Optional[float]:
    """Strict causal prior-N-completed-session INDIA_VIX SMA; returns None if < N sessions exist."""
    if not prior_vix_closes or len(prior_vix_closes) < lookback:
        return None
    tail = [float(x) for x in prior_vix_closes[-lookback:]]
    if any(x <= 0.0 for x in tail):
        return None
    return round(sum(tail) / float(lookback), 4)


def compute_daily_sma(prior_spot_closes: List[float], lookback: int = 20) -> Optional[float]:
    if not prior_spot_closes or len(prior_spot_closes) < lookback:
        return None
    tail = [float(x) for x in prior_spot_closes[-lookback:]]
    if any(x <= 0.0 for x in tail):
        return None
    return round(sum(tail) / float(lookback), 4)


def evaluate_book_a_entry_signal(
    cfg: Dict[str, Any],
    session_date: str,
    bar_ts: str,
    ist_hhmm: int,
    decision_ts_ms: int,
    spot_close: float,
    spot_vwap: float,
    vix_now: Optional[float],
    prior_vix_closes: List[float],
    prior_spot_closes: List[float],
    expiries: List[str],
    contracts_lookup: Dict[Tuple[str, float, str], Dict[str, Any]],
    quote_lookup: Dict[Tuple[str, float, str], Dict[str, Any]],
    observed_sessions: Optional[Set[str]] = None,
    extra_holidays: Optional[List[str]] = None,
    allow_unverified_in_replay: bool = False,
) -> Dict[str, Any]:
    """
    Evaluates all frozen gates for `V2_BOOK_A_PROPOSED_O3`. Fails closed on missing VIX/SMA,
    non-T-1 trading-session DTE, missing/stale/skewed L5 books, or insufficient credit.
    """
    cfg_a = cfg["books"]["V2_BOOK_A_PROPOSED_O3"]
    hyg = cfg["quote_hygiene"]

    if not bool(cfg_a.get("autonomous_entries_enabled", False)) and not allow_unverified_in_replay:
        return {
            "book_id": cfg_a["book_id"],
            "candidate_variant": cfg_a["verification_status"],
            "passed": False,
            "state": "DISABLED_UNVERIFIED",
            "reject_reason": "BLOCKED_UNVERIFIED_O3_SPEC_CONFLICT",
            "gates": [{"name": "SpecVerification", "passed": False, "current": cfg_a["verification_status"], "target": "VERIFIED"}],
        }

    variant_tag = (
        "HYPOTHETICAL_MODIFIED_O3_NOT_PHASE4_EQUIVALENT"
        if not bool(cfg_a.get("autonomous_entries_enabled", False))
        else "VERIFIED_O3"
    )

    # 1. Verify current session is a valid NSE trading session & find contract with trading_dte == 1
    if not is_nse_trading_session(session_date, observed_sessions=observed_sessions, extra_holidays=extra_holidays):
        return {
            "book_id": cfg_a["book_id"],
            "candidate_variant": variant_tag,
            "passed": False,
            "state": "NON_TRADING_SESSION",
            "reject_reason": "EXCHANGE_HOLIDAY_OR_MUHURAT",
            "gates": [],
        }

    target_exp: Optional[str] = None
    for exp_s in sorted(set(expiries)):
        if exp_s < session_date:
            continue
        tdte = trading_sessions_between(
            session_date,
            exp_s,
            observed_sessions=observed_sessions,
            extra_holidays=extra_holidays,
        )
        if tdte == int(cfg_a["entry_session_dte"]):
            target_exp = exp_s
            break

    win_ok = int(cfg_a["entry_window_start_hhmm"]) <= ist_hhmm <= int(cfg_a["entry_window_end_hhmm"])
    vix_sma10 = compute_vix_prior_10d_sma(prior_vix_closes, int(cfg_a["vix_sma_lookback_sessions"]))
    vix_ok = (vix_now is not None and vix_now > 0 and vix_sma10 is not None and vix_now <= vix_sma10 * float(cfg_a["vix_cap_mult"]))
    spot_sma20 = compute_daily_sma(prior_spot_closes, int(cfg_a["trend_daily_sma_sessions"]))

    is_bull = bool(spot_sma20 is not None and spot_close >= spot_sma20 and spot_close >= spot_vwap)
    is_bear = bool(spot_sma20 is not None and spot_close < spot_sma20 and spot_close < spot_vwap)
    trend_ok = is_bull or is_bear

    ot = "PE" if is_bull else "CE"
    step = float(cfg_a["strike_grid_step_pts"])
    atm = round(spot_close / step) * step
    off = float(cfg_a["short_otm_offset_pts"])
    width = float(cfg_a["spread_width_pts"])
    short_k = (atm - off) if ot == "PE" else (atm + off)
    wing_k = (short_k - width) if ot == "PE" else (short_k + width)

    short_contract = contracts_lookup.get((target_exp or "", short_k, ot)) if target_exp else None
    wing_contract = contracts_lookup.get((target_exp or "", wing_k, ot)) if target_exp else None
    short_quote = quote_lookup.get((target_exp or "", short_k, ot)) if target_exp else None
    wing_quote = quote_lookup.get((target_exp or "", wing_k, ot)) if target_exp else None

    l5_ok = False
    l5_detail = "NO_T1_EXPIRY"
    est_credit = 0.0
    skew_ms: Optional[int] = None

    if target_exp and wing_contract and short_contract:
        skew_ok, skew_ms, skew_rsn = check_cross_leg_skew(
            wing_quote, short_quote, max_skew_ms=int(hyg["max_cross_leg_skew_ms"])
        )
        if not skew_ok:
            l5_detail = skew_rsn
        else:
            lot_sz = int(wing_contract["lot_size"])
            qty = int(cfg_a["lot_count"]) * lot_sz
            sw_w = sweep_observed_l5_book(
                wing_quote,
                "BUY",
                qty,
                lot_sz,
                int(hyg["adverse_latency_ticks"]),
                int(hyg["max_entry_quote_age_ms"]),
                decision_ts_ms,
                float(hyg["max_wing_spread_pct"]),
                float(hyg["max_wing_spread_pts"]),
                require_observed_l5=bool(hyg["require_observed_l5_book_for_entry"]),
            )
            sw_s = sweep_observed_l5_book(
                short_quote,
                "SELL",
                qty,
                lot_sz,
                int(hyg["adverse_latency_ticks"]),
                int(hyg["max_entry_quote_age_ms"]),
                decision_ts_ms,
                float(hyg["max_primary_spread_pct"]),
                require_observed_l5=bool(hyg["require_observed_l5_book_for_entry"]),
            )
            if sw_w.get("ok") and sw_s.get("ok"):
                est_credit = round(float(sw_s["fill_price"]) - float(sw_w["fill_price"]), 2)
                l5_ok = True
                l5_detail = f"W={sw_w['fill_price']:.2f}/S={sw_s['fill_price']:.2f}"
            else:
                l5_detail = f"W:{sw_w.get('reason')}|S:{sw_s.get('reason')}"
    elif target_exp:
        l5_detail = "MISSING_CONTRACT_META"

    credit_ok = bool(l5_ok and est_credit >= float(cfg_a["min_net_credit_pts"]))

    gates = [
        {
            "name": "HolidayAdjDTE1",
            "passed": target_exp is not None,
            "current": target_exp or "NONE",
            "target": "trading_dte == 1",
        },
        {
            "name": "Window1330_1500",
            "passed": win_ok,
            "current": f"{ist_hhmm:04d}",
            "target": "1330..1500 IST",
        },
        {
            "name": "VIX_1p05_SMA10",
            "passed": vix_ok,
            "current": f"{vix_now:.2f}/{vix_sma10:.2f}" if (vix_now and vix_sma10) else "MISSING",
            "target": "<= 1.05x SMA10",
        },
        {
            "name": "Trend_SMA20_VWAP",
            "passed": trend_ok,
            "current": f"{spot_close:.1f} vs SMA20={spot_sma20 or 'NA'} VWAP={spot_vwap:.1f}",
            "target": "Both >= (PE) or Both < (CE)",
        },
        {
            "name": "SyncObsL5_Credit30",
            "passed": credit_ok,
            "current": f"{est_credit:.2f}pt ({l5_detail})",
            "target": f">= {cfg_a['min_net_credit_pts']:.1f}pt",
        },
    ]

    all_passed = all(g["passed"] for g in gates)
    reject_reason = None if all_passed else next((f"{g['name']}:{g['current']}" for g in gates if not g["passed"]), "BLOCKED")
    return {
        "book_id": cfg_a["book_id"],
        "candidate_variant": variant_tag,
        "passed": all_passed,
        "state": "READY_FIRE" if all_passed else "WATCHING",
        "reject_reason": reject_reason,
        "expiry": target_exp,
        "option_type": ot,
        "is_bull": is_bull,
        "short_contract": short_contract,
        "wing_contract": wing_contract,
        "short_quote": short_quote,
        "wing_quote": wing_quote,
        "skew_ms": skew_ms,
        "est_credit_pts": est_credit,
        "gates": gates,
    }


def manage_open_book_a_on_bar(
    sim: V2PaperSimulator,
    pkg: Dict[str, Any],
    session_date: str,
    bar_ts: str,
    ist_hhmm: int,
    decision_ts_ms: int,
    spot_close: float,
    quote_by_ikey: Dict[str, Dict[str, Any]],
    fsp_twap_spot: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    Manages an open or unresolved Book A spread on bar close:
      - Forces cutoff exit at >= 14:45 IST on expiry day (persisting `UNRESOLVED_EXPOSURE_PAST_1445`
        if quotes are unavailable/stale/skewed, recovering on next fresh quote or observed `15:30` FSP).
      - Evaluates close-only hard stop (1.65x), 45%-decay breakeven ratchet, and 50% credit target.
    """
    ls = dict(pkg["legs_state"])
    if pkg["state"] == "PENDING_LEG2_SHORT":
        wl = ls["wing_leg"]
        st = ls["short_target"]
        return sim.complete_or_unwind_pending_leg2(
            package_id=pkg["package_id"],
            short_quote=quote_by_ikey.get(st["instrument_key"]),
            wing_quote=quote_by_ikey.get(wl["instrument_key"]),
            decision_ts_ms=decision_ts_ms,
            session_date=session_date,
            bar_ts=bar_ts,
        )

    wl = ls["wing_leg"]
    sl = ls.get("short_leg")
    wq = quote_by_ikey.get(wl["instrument_key"])
    sq = quote_by_ikey.get(sl["instrument_key"]) if sl else None
    cfg_a = sim.config["books"]["V2_BOOK_A_PROPOSED_O3"]
    hyg = sim.config["quote_hygiene"]

    # Hedge-only recovery when Wing unwind previously failed on stale quote
    if pkg["state"] == "HEDGE_ONLY_EXPOSURE_OPEN":
        return sim.close_package_market_or_expiry(
            package_id=pkg["package_id"],
            session_date=session_date,
            bar_ts=bar_ts,
            decision_ts_ms=decision_ts_ms,
            exit_reason="HEDGE_ONLY_DELAYED_RECOVERY",
            long_or_wing_quote=wq,
            reference_spot=spot_close,
        )

    is_expiry_day = (pkg.get("expiry") or "") <= session_date
    # 15:30 expiry cash settlement only when real FSP spot observation is available
    if is_expiry_day and ist_hhmm >= 1530 and fsp_twap_spot is not None and fsp_twap_spot > 0:
        return sim.close_package_market_or_expiry(
            package_id=pkg["package_id"],
            session_date=session_date,
            bar_ts=bar_ts,
            decision_ts_ms=decision_ts_ms,
            exit_reason="EXPIRY_1530_OBSERVED_FSP_SETTLEMENT",
            long_or_wing_quote=wq,
            short_quote=sq,
            settlement_fsp_spot=fsp_twap_spot,
            reference_spot=fsp_twap_spot,
        )

    # Forced cutoff >= 14:45 IST on expiry day (also resolves UNRESOLVED_EXPOSURE_PAST_1445 on next fresh quote!)
    if is_expiry_day and ist_hhmm >= int(cfg_a["expiry_exit_cutoff_hhmm"]):
        rsn = (
            "DELAYED_RECOVERY_AFTER_1445_CUTOFF"
            if pkg["state"] == "UNRESOLVED_EXPOSURE_PAST_1445"
            else "EXPIRY_PRE_FSP_1445_EXIT"
        )
        return sim.close_package_market_or_expiry(
            package_id=pkg["package_id"],
            session_date=session_date,
            bar_ts=bar_ts,
            decision_ts_ms=decision_ts_ms,
            exit_reason=rsn,
            long_or_wing_quote=wq,
            short_quote=sq,
            reference_spot=spot_close,
        )

    # Normal intraday close-only stop / breakeven ratchet / 50% credit target
    if sl is None:
        return None
    skew_ok, _, _ = check_cross_leg_skew(wq, sq, max_skew_ms=int(hyg["max_cross_leg_skew_ms"]))
    if not skew_ok:
        return None
    w_qty = int(wl["qty"])
    lot_sz = int(wl["lot_size"])
    sw_w = sweep_observed_l5_book(wq, "SELL", w_qty, lot_sz, sim._effective_slippage_ticks(), int(hyg["max_exit_quote_age_ms"]), decision_ts_ms, max_spread_pct=None, require_observed_l5=False)
    sw_s = sweep_observed_l5_book(sq, "BUY", w_qty, lot_sz, sim._effective_slippage_ticks(), int(hyg["max_exit_quote_age_ms"]), decision_ts_ms, max_spread_pct=None, require_observed_l5=False)
    if not sw_w.get("ok") or not sw_s.get("ok"):
        return None

    cur_debit = round(float(sw_s["fill_price"]) - float(sw_w["fill_price"]), 2)
    init_credit = float(ls["net_entry_credit_pts"])
    target_debit = float(ls["target_debit_pts"])
    hard_debit = float(ls["hard_stop_debit_pts"])
    ratchet_debit = ls.get("ratchet_max_debit_pts")

    # 45% credit decay tightens close-only stop to breakeven
    decay_pct = ((init_credit - cur_debit) / max(0.05, init_credit)) * 100.0
    if decay_pct >= float(cfg_a["breakeven_ratchet_decay_pct"]):
        ratchet_debit = init_credit if ratchet_debit is None else min(float(ratchet_debit), init_credit)
        ls["ratchet_max_debit_pts"] = ratchet_debit
        ls["best_close_credit_decay_pts"] = min(float(ls.get("best_close_credit_decay_pts") or init_credit), max(0.05, cur_debit))
        upd = {**pkg, "legs_state": ls}
        sim.open_packages[pkg["package_id"]] = upd
        sim.store.execute_atomic_transition([], [], upd, None)

    eff_stop = min(hard_debit, float(ratchet_debit)) if ratchet_debit is not None else hard_debit
    exit_reason: Optional[str] = None
    if cur_debit <= target_debit:
        exit_reason = "CREDIT_DECAY_TARGET_50PCT"
    elif cur_debit >= eff_stop:
        exit_reason = (
            "CLOSE_ONLY_BREAKEVEN_RATCHET"
            if (ratchet_debit is not None and float(ratchet_debit) < hard_debit - 0.01)
            else "CLOSE_ONLY_STOP_1P65X"
        )

    if exit_reason:
        return sim.close_package_market_or_expiry(
            package_id=pkg["package_id"],
            session_date=session_date,
            bar_ts=bar_ts,
            decision_ts_ms=decision_ts_ms,
            exit_reason=exit_reason,
            long_or_wing_quote=wq,
            short_quote=sq,
            reference_spot=spot_close,
        )
    return None
