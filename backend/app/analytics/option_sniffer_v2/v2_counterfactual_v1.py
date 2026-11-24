"""
V2 Option Sniffer — Counterfactual Study on Archived V1 Trades (Fix #9 Causal & Conservative).

Evaluates two isolated hypothetical counterfactuals on V1's archived `option_sniffer_advance_s205_shadow_packages`:
  1. `CF1_TRUE_FRESH_70PCT_CONTINUATION`:
       - Does NOT auto-close Book A spreads at 70% credit capture on stale wings.
       - Pre-arm checks causal `SPOT_VWAP_FLIP_EXIT`, `SPREAD_HARD_SL_HIT`, and `15:15` EOD.
       - Arms at `<= 30%` entry credit (`>= 70%` decay captured), then holds targeting `85%`
         (`deb <= 15%` of credit) while protecting `55%` credit locked (`deb >= 45%` of credit),
         filling protective exits at actual executable fresh synchronized debit (honoring gaps).
       - Separates `REALIZED_OUTCOME` from `UNRESOLVED_OBSERVATION` (`UNRESOLVED_STALE_BEFORE_ARMING`,
         `UNRESOLVED_STALE_AFTER_ARMING`) so stale archives never fabricate realized P&L.
  2. `CF2_RATCHET_BYPASS_EXPERIMENT`:
       - Bypasses ONLY ordinary close-only profit ratchet while evaluating causal `SPOT_VWAP_FLIP_EXIT`
         (including after V1's original exit timestamp), hard stop, spread 70% target (Book A),
         and `15:15` EOD strictly on fresh executable synchronized quotes (`allow_fresh_l1_only=True`).

Guarantees:
  - Stored ONLY in `osv2_counterfactual_v1` with `included_in_v2_totals = 0 CHECK(=0)`.
  - Never enters V2 portfolio equity, V2 drawdown, or V2 promotion gates.
"""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.common.options_accounting import OptionLeg, compute_package_accounting
from app.analytics.option_sniffer_v2.v2_config import (
    IST_TZ,
    ist_hhmm_from_ms,
    parse_ist_timestamp_ms,
)
from app.analytics.option_sniffer_v2.v2_data_contract import (
    check_cross_leg_skew,
    sweep_observed_l5_book,
)
from app.analytics.option_sniffer_v2.v2_ledger_db import V2LedgerStore
from app.analytics.option_sniffer_v2.v2_paper_simulator import make_stable_id

_VFLIP_PTS = 18.0


def _bar_open_epoch_ms(bar_ts: str) -> int:
    ms = parse_ist_timestamp_ms(bar_ts)
    if ms is not None:
        return ms
    dt = datetime.strptime(bar_ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST_TZ)
    return int(dt.timestamp() * 1000)


def _row_to_quote_dict(row: Any) -> Dict[str, Any]:
    d = dict(row) if not isinstance(row, dict) else row
    depth = d.get("depth_levels")
    if depth is None:
        try:
            depth = json.loads(d.get("depth_json") or "[]")
        except Exception:
            depth = []
    avail_raw = d.get("recorded_at") or d.get("bar_ts")
    avail_ms = int(d.get("availability_ts_ms") or parse_ist_timestamp_ms(avail_raw) or 0)
    age_raw = d.get("l1_book_age_ms")
    age_ms = int(age_raw) if age_raw is not None else None
    l1_ts = d.get("l1_book_timestamp_ms")
    if l1_ts is None and avail_ms > 0 and age_ms is not None and 0 <= age_ms < 999999:
        l1_ts = avail_ms - age_ms
    depth_obs = bool(d.get("depth_observed_on_tick"))
    depth_ts = d.get("depth_timestamp_ms") if d.get("depth_timestamp_ms") is not None else (l1_ts if depth_obs else None)
    return {
        "instrument_key": d.get("instrument_key"),
        "trading_symbol": d.get("trading_symbol"),
        "underlying": d.get("underlying") or "NIFTY",
        "expiry": d.get("expiry"),
        "strike": float(d["strike"]) if d.get("strike") is not None else None,
        "option_type": d.get("option_type"),
        "raw_bid": float(d.get("raw_bid") or d.get("bid") or 0.0),
        "raw_ask": float(d.get("raw_ask") or d.get("ask") or 0.0),
        "bid": float(d.get("raw_bid") or d.get("bid") or 0.0),
        "ask": float(d.get("raw_ask") or d.get("ask") or 0.0),
        "l1_bid_qty_observed": int(d.get("l1_bid_qty_observed") if d.get("l1_bid_qty_observed") is not None else (d.get("bid_qty") or 0)),
        "l1_ask_qty_observed": int(d.get("l1_ask_qty_observed") if d.get("l1_ask_qty_observed") is not None else (d.get("ask_qty") or 0)),
        "depth_levels": depth,
        "depth_json_raw": d.get("depth_json"),
        "is_observed_l1_book": bool(d.get("is_observed_l1_book")),
        "l1_observed_on_tick": bool(d.get("l1_observed_on_tick") if d.get("l1_observed_on_tick") is not None else d.get("is_observed_l1_book")),
        "depth_observed_on_tick": depth_obs,
        "depth_rungs_carried": bool(d.get("depth_rungs_carried") if d.get("depth_rungs_carried") is not None else (not depth_obs)),
        "book_provenance": d.get("book_provenance") or "UNKNOWN",
        "l1_book_age_ms": age_ms,
        "l1_timestamp_ms": l1_ts,
        "l1_book_timestamp_ms": l1_ts,
        "depth_timestamp_ms": depth_ts,
        "availability_ts_ms": avail_ms,
        "local_receipt_ts_ms": avail_ms,
        "spot_ltp": float(d.get("spot_ltp") or 0.0),
    }


def _build_causal_spot_vwap_by_session(v1_ro_conn: sqlite3.Connection) -> Dict[str, List[Tuple[int, str, float, Optional[float]]]]:
    """
    Computes strictly causal running TPB spot VWAP (`n >= 3`) ordered by archive availability timestamp
    for every snapshot bar up to 15:16 IST.
    """
    by_dt: Dict[str, List[Tuple[int, str, float, Optional[float]]]] = {}
    cum_spot: Dict[str, float] = {}
    cnt_spot: Dict[str, int] = {}
    for r in v1_ro_conn.execute(
        """
        SELECT bar_ts, timestamp, spot_ltp
        FROM option_sniffer_advance_s205_l5_snapshots
        ORDER BY bar_ts ASC;
        """
    ).fetchall():
        dt = str(r["bar_ts"])[:10]
        if dt > "2026-11-23":
            continue
        avail_ms = parse_ist_timestamp_ms(r["timestamp"] or r["bar_ts"])
        spot_c = float(r["spot_ltp"] or 0.0)
        if avail_ms is None or spot_c <= 0:
            continue
        cum_spot[dt] = cum_spot.get(dt, 0.0) + spot_c
        cnt_spot[dt] = cnt_spot.get(dt, 0) + 1
        tpb = round(cum_spot[dt] / float(cnt_spot[dt]), 4) if cnt_spot[dt] >= 3 else None
        by_dt.setdefault(dt, []).append((avail_ms, str(r["bar_ts"]), spot_c, tpb))
    return by_dt


def _causal_spot_and_vwap_at(
    spot_series: List[Tuple[int, str, float, Optional[float]]],
    avail_ms: int,
    fallback_spot: float = 0.0,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """
    Returns `(prev_spot, prev_tpb, cur_spot, cur_tpb)` strictly for snapshot bars with
    `snap_avail_ms <= avail_ms`.
    """
    hist = [item for item in spot_series if item[0] <= avail_ms]
    if not hist:
        return None, None, (fallback_spot if fallback_spot > 0 else None), None
    if len(hist) == 1:
        return None, None, hist[-1][2], hist[-1][3]
    return hist[-2][2], hist[-2][3], hist[-1][2], hist[-1][3]


def _check_causal_vwap_flip(
    is_bull: bool,
    held_mins: int,
    spot_now: Optional[float],
    tpb_now: Optional[float],
    spot_prev: Optional[float],
    tpb_prev: Optional[float],
    explicit_bar_flip: Optional[bool] = None,
    v1_exit_reason: Optional[str] = None,
    v1_exit_bar_ts: Optional[str] = None,
    cur_bar_ts: str = "",
) -> bool:
    if explicit_bar_flip is True:
        return True
    if explicit_bar_flip is False:
        return False
    if held_mins >= 4 and spot_now and tpb_now and spot_prev and tpb_prev:
        if is_bull and spot_now < (tpb_now - _VFLIP_PTS) and spot_prev < (tpb_prev - _VFLIP_PTS):
            return True
        if (not is_bull) and spot_now > (tpb_now + _VFLIP_PTS) and spot_prev > (tpb_prev + _VFLIP_PTS):
            return True
    if tpb_now is None and v1_exit_reason == "SPOT_VWAP_FLIP_EXIT" and v1_exit_bar_ts and cur_bar_ts == v1_exit_bar_ts:
        return True
    return False


def simulate_cf1_spread_trajectory(
    entry_bar_ts: str,
    session_date: str,
    entry_credit_pts: float,
    hard_stop_debit_pts: float,
    wing_leg: Dict[str, Any],
    short_leg: Dict[str, Any],
    paired_bars: List[Dict[str, Any]],
    v1_exit_reason: str = "",
    v1_exit_bar_ts: str = "",
    lot_size: int = 65,
    max_quote_age_ms: int = 5000,
) -> Dict[str, Any]:
    """
    Pure CF1 evaluator:
      - Pre-arm: causal SPOT_VWAP_FLIP_EXIT, SPREAD_HARD_SL_HIT, arms at 70% decay (debit <= 0.30*credit),
        or 15:15 EOD (`CF1_EOD_1515_PRE_ARM`).
      - Post-arm: targets 85% decay (debit <= 0.15*credit) -> `CF1_TARGET_85PCT_HIT`,
        protects 55% lock (debit >= 0.45*credit) -> `CF1_PROTECTIVE_55PCT_LOCK_HIT` filled at actual
        executable synchronized debit (never capping at theoretical stop), causal `SPOT_VWAP_FLIP_POST_ARM`,
        `SPREAD_HARD_SL_HIT`, or 15:15 EOD (`CF1_EOD_1515_BETWEEN_55_AND_85`).
      - Separates `UNRESOLVED_OBSERVATION` (`UNRESOLVED_STALE_AFTER_ARMING` / `UNRESOLVED_STALE_BEFORE_ARMING`)
        from `REALIZED_OUTCOME`.
    """
    qty = int(wing_leg["qty"])
    entry_ms = _bar_open_epoch_ms(entry_bar_ts)
    is_bull = str(short_leg["option_type"]).upper() == "PE"

    arm_70_deb = round(float(entry_credit_pts) * 0.30, 2)
    target_85_deb = round(float(entry_credit_pts) * 0.15, 2)
    lock_55_deb = round(float(entry_credit_pts) * 0.45, 2)

    armed_70pct = False
    armed_bar_ts: Optional[str] = None
    last_fresh: Optional[Dict[str, Any]] = None
    eod_stale = False
    prev_spot_from_bars: Optional[float] = None
    prev_tpb_from_bars: Optional[float] = None

    for item in paired_bars:
        bt = str(item["bar_ts"])
        hm = bt[11:16]
        hhmm = int(hm[:2]) * 100 + int(hm[3:5])
        wq = _row_to_quote_dict(item["wing_quote"])
        sq = _row_to_quote_dict(item["short_quote"])
        avail_ms = int(item.get("avail_ms") or wq.get("availability_ts_ms") or sq.get("availability_ts_ms") or 0)
        if avail_ms <= 0:
            avail_ms = _bar_open_epoch_ms(bt) + 60_000
        if ist_hhmm_from_ms(avail_ms) > 1530:
            continue

        sw_w = sweep_observed_l5_book(
            wq,
            "SELL",
            qty,
            lot_size,
            1,
            max_quote_age_ms,
            avail_ms,
            max_spread_pct=None,
            require_observed_l5=False,
            allow_fresh_l1_only=True,
        )
        sw_s = sweep_observed_l5_book(
            sq,
            "BUY",
            qty,
            lot_size,
            1,
            max_quote_age_ms,
            avail_ms,
            max_spread_pct=None,
            require_observed_l5=False,
            allow_fresh_l1_only=True,
        )
        if not sw_w.get("ok") or not sw_s.get("ok"):
            if hhmm >= 1515:
                eod_stale = True
            continue
        skew_ok, _, _ = check_cross_leg_skew(
            wq,
            sq,
            max_skew_ms=max_quote_age_ms,
            consumed_ts_a=sw_w.get("consumed_ts_ms"),
            consumed_ts_b=sw_s.get("consumed_ts_ms"),
            require_depth_ts=False,
        )
        if not skew_ok:
            if hhmm >= 1515:
                eod_stale = True
            continue

        w_ex = float(sw_w["fill_price"])
        s_ex = float(sw_s["fill_price"])
        deb = round(s_ex - w_ex, 2)
        ref_spot = float(item.get("spot_ltp") or sq.get("spot_ltp") or 0.0)
        acc = compute_package_accounting(
            [
                OptionLeg(wing_leg["option_type"], float(wing_leg["strike"]), qty, float(wing_leg["entry_price"]), w_ex, lot_size=lot_size),
                OptionLeg(short_leg["option_type"], float(short_leg["strike"]), -qty, float(short_leg["entry_price"]), s_ex, lot_size=lot_size),
            ],
            trade_date=session_date,
            reference_spot=ref_spot,
        )
        snap = {
            "bar_ts": bt,
            "debit": deb,
            "gross": float(acc["gross_pnl"]),
            "charges": float(acc["total_friction"]),
            "net": float(acc["net_pnl"]),
            "consumed_ts_ms": max(int(sw_w["consumed_ts_ms"]), int(sw_s["consumed_ts_ms"])),
        }
        last_fresh = snap

        held_mins = max(0, (_bar_open_epoch_ms(bt) - entry_ms) // 60_000)
        s_now = item.get("spot_ltp") or (sq.get("spot_ltp") or None)
        t_now = item.get("causal_tpb_vwap")
        s_prev = item.get("prev_spot_ltp") if "prev_spot_ltp" in item else prev_spot_from_bars
        t_prev = item.get("prev_tpb_vwap") if "prev_tpb_vwap" in item else prev_tpb_from_bars
        if s_now and t_now is not None:
            prev_spot_from_bars = float(s_now)
            prev_tpb_from_bars = float(t_now)

        vflip = _check_causal_vwap_flip(
            is_bull=is_bull,
            held_mins=held_mins,
            spot_now=float(s_now) if s_now else None,
            tpb_now=float(t_now) if t_now is not None else None,
            spot_prev=float(s_prev) if s_prev else None,
            tpb_prev=float(t_prev) if t_prev is not None else None,
            explicit_bar_flip=item.get("vwap_flipped"),
            v1_exit_reason=v1_exit_reason,
            v1_exit_bar_ts=v1_exit_bar_ts,
            cur_bar_ts=bt,
        )

        if not armed_70pct:
            if vflip:
                return _realized_cf_result("CF1_SPOT_VWAP_FLIP_EXIT", "REALIZED_OUTCOME", snap, armed_70pct, armed_bar_ts, lock_55_deb, target_85_deb)
            if deb >= float(hard_stop_debit_pts):
                return _realized_cf_result("SPREAD_HARD_SL_HIT", "REALIZED_OUTCOME", snap, armed_70pct, armed_bar_ts, lock_55_deb, target_85_deb)
            if deb <= target_85_deb:
                armed_70pct = True
                armed_bar_ts = bt
                return _realized_cf_result("CF1_TARGET_85PCT_HIT", "REALIZED_OUTCOME", snap, armed_70pct, armed_bar_ts, lock_55_deb, target_85_deb)
            if deb <= arm_70_deb:
                armed_70pct = True
                armed_bar_ts = bt
                if hhmm >= 1515:
                    return _realized_cf_result("CF1_EOD_1515_BETWEEN_55_AND_85", "REALIZED_OUTCOME", snap, armed_70pct, armed_bar_ts, lock_55_deb, target_85_deb)
                continue
            if hhmm >= 1515:
                return _realized_cf_result("CF1_EOD_1515_PRE_ARM", "REALIZED_OUTCOME", snap, armed_70pct, armed_bar_ts, lock_55_deb, target_85_deb)
        else:
            if vflip:
                return _realized_cf_result("CF1_SPOT_VWAP_FLIP_POST_ARM", "REALIZED_OUTCOME", snap, armed_70pct, armed_bar_ts, lock_55_deb, target_85_deb)
            if deb >= float(hard_stop_debit_pts):
                return _realized_cf_result("SPREAD_HARD_SL_HIT", "REALIZED_OUTCOME", snap, armed_70pct, armed_bar_ts, lock_55_deb, target_85_deb)
            if deb >= lock_55_deb:
                return _realized_cf_result("CF1_PROTECTIVE_55PCT_LOCK_HIT", "REALIZED_OUTCOME", snap, armed_70pct, armed_bar_ts, lock_55_deb, target_85_deb)
            if deb <= target_85_deb:
                return _realized_cf_result("CF1_TARGET_85PCT_HIT", "REALIZED_OUTCOME", snap, armed_70pct, armed_bar_ts, lock_55_deb, target_85_deb)
            if hhmm >= 1515:
                return _realized_cf_result("CF1_EOD_1515_BETWEEN_55_AND_85", "REALIZED_OUTCOME", snap, armed_70pct, armed_bar_ts, lock_55_deb, target_85_deb)

    # Loop ended without realized exit
    if armed_70pct:
        st = "UNRESOLVED_STALE_AFTER_ARMING"
    elif last_fresh or eod_stale:
        st = "UNRESOLVED_STALE_BEFORE_ARMING"
    else:
        st = "NO_FRESH_QUOTES"
    return {
        "cf_status": st,
        "outcome_class": "UNRESOLVED_OBSERVATION",
        "cf_exit_bar_ts": None,
        "cf_exit_reason": f"{st}_NO_SYNTHETIC_FILL",
        "cf_gross_pnl_inr": None,
        "cf_charges_inr": None,
        "cf_net_pnl_inr": None,
        "last_fresh_mark_net_inr": last_fresh["net"] if last_fresh else None,
        "last_fresh_bar_ts": last_fresh["bar_ts"] if last_fresh else None,
        "detail": {
            "armed_70pct": armed_70pct,
            "armed_bar_ts": armed_bar_ts,
            "arm_70_debit": arm_70_deb,
            "target_85_debit": target_85_deb,
            "protective_55_debit": lock_55_deb,
            "last_fresh_snapshot": last_fresh,
            "eod_bar_stale": eod_stale,
        },
    }


def _realized_cf_result(
    reason: str,
    status: str,
    snap: Dict[str, Any],
    armed_70pct: bool,
    armed_bar_ts: Optional[str],
    lock_55_deb: float,
    target_85_deb: float,
) -> Dict[str, Any]:
    return {
        "cf_status": status,
        "outcome_class": "REALIZED_OUTCOME",
        "cf_exit_bar_ts": snap["bar_ts"],
        "cf_exit_reason": reason,
        "cf_gross_pnl_inr": snap["gross"],
        "cf_charges_inr": snap["charges"],
        "cf_net_pnl_inr": snap["net"],
        "last_fresh_mark_net_inr": snap["net"],
        "last_fresh_bar_ts": snap["bar_ts"],
        "detail": {
            "armed_70pct": armed_70pct,
            "armed_bar_ts": armed_bar_ts,
            "exit_debit": snap["debit"],
            "target_85_debit": target_85_deb,
            "protective_55_debit": lock_55_deb,
        },
    }


def simulate_cf2_trajectory(
    mode: str,
    entry_bar_ts: str,
    session_date: str,
    legs_state: Dict[str, Any],
    bars: List[Dict[str, Any]],
    v1_exit_reason: str = "",
    v1_exit_bar_ts: str = "",
    lot_size: int = 65,
    max_quote_age_ms: int = 5000,
) -> Dict[str, Any]:
    """
    Pure CF2 evaluator (bypass ONLY ordinary profit ratchet):
      - Evaluates causal `SPOT_VWAP_FLIP_EXIT` (including after V1's original exit timestamp),
        hard stop, Book A 70% decay target on fresh synchronized depth, and 15:15 EOD.
    """
    entry_ms = _bar_open_epoch_ms(entry_bar_ts)
    last_fresh: Optional[Dict[str, Any]] = None
    eod_stale = False
    prev_spot_from_bars: Optional[float] = None
    prev_tpb_from_bars: Optional[float] = None

    if mode == "DEFINED_RISK_CREDIT_SPREAD_150":
        wl = legs_state["wing_leg"]
        sl = legs_state["short_leg"]
        qty = int(wl["qty"])
        target_deb = float(legs_state["target_debit_pts"])
        hard_deb = float(legs_state["hard_stop_debit_pts"])
        is_bull = str(sl["option_type"]).upper() == "PE"

        for item in bars:
            bt = str(item["bar_ts"])
            hhmm = int(bt[11:13]) * 100 + int(bt[14:16])
            wq = _row_to_quote_dict(item["wing_quote"])
            sq = _row_to_quote_dict(item["short_quote"])
            avail_ms = int(item.get("avail_ms") or wq.get("availability_ts_ms") or _bar_open_epoch_ms(bt) + 60_000)
            if ist_hhmm_from_ms(avail_ms) > 1530:
                continue
            sw_w = sweep_observed_l5_book(
                wq, "SELL", qty, lot_size, 1, max_quote_age_ms, avail_ms,
                max_spread_pct=None, require_observed_l5=False, allow_fresh_l1_only=True,
            )
            sw_s = sweep_observed_l5_book(
                sq, "BUY", qty, lot_size, 1, max_quote_age_ms, avail_ms,
                max_spread_pct=None, require_observed_l5=False, allow_fresh_l1_only=True,
            )
            if not sw_w.get("ok") or not sw_s.get("ok"):
                if hhmm >= 1515:
                    eod_stale = True
                continue
            skew_ok, _, _ = check_cross_leg_skew(
                wq, sq, max_skew_ms=max_quote_age_ms,
                consumed_ts_a=sw_w.get("consumed_ts_ms"),
                consumed_ts_b=sw_s.get("consumed_ts_ms"),
                require_depth_ts=False,
            )
            if not skew_ok:
                if hhmm >= 1515:
                    eod_stale = True
                continue
            w_ex = float(sw_w["fill_price"])
            s_ex = float(sw_s["fill_price"])
            deb = round(s_ex - w_ex, 2)
            ref_spot = float(item.get("spot_ltp") or sq.get("spot_ltp") or 0.0)
            acc = compute_package_accounting(
                [
                    OptionLeg(wl["option_type"], float(wl["strike"]), qty, float(wl["entry_price"]), w_ex, lot_size=lot_size),
                    OptionLeg(sl["option_type"], float(sl["strike"]), -qty, float(sl["entry_price"]), s_ex, lot_size=lot_size),
                ],
                trade_date=session_date,
                reference_spot=ref_spot,
            )
            snap = {"bar_ts": bt, "debit": deb, "gross": float(acc["gross_pnl"]), "charges": float(acc["total_friction"]), "net": float(acc["net_pnl"])}
            last_fresh = snap

            held_mins = max(0, (_bar_open_epoch_ms(bt) - entry_ms) // 60_000)
            s_now = item.get("spot_ltp") or (sq.get("spot_ltp") or None)
            t_now = item.get("causal_tpb_vwap")
            s_prev = item.get("prev_spot_ltp") if "prev_spot_ltp" in item else prev_spot_from_bars
            t_prev = item.get("prev_tpb_vwap") if "prev_tpb_vwap" in item else prev_tpb_from_bars
            if s_now and t_now is not None:
                prev_spot_from_bars = float(s_now)
                prev_tpb_from_bars = float(t_now)
            vflip = _check_causal_vwap_flip(
                is_bull, held_mins,
                float(s_now) if s_now else None,
                float(t_now) if t_now is not None else None,
                float(s_prev) if s_prev else None,
                float(t_prev) if t_prev is not None else None,
                explicit_bar_flip=item.get("vwap_flipped"),
                v1_exit_reason=v1_exit_reason,
                v1_exit_bar_ts=v1_exit_bar_ts,
                cur_bar_ts=bt,
            )
            if vflip:
                return _realized_cf_result("SPOT_VWAP_FLIP_EXIT", "REALIZED_OUTCOME", snap, False, None, 0.0, target_deb)
            if deb >= hard_deb:
                return _realized_cf_result("SPREAD_HARD_SL_HIT", "REALIZED_OUTCOME", snap, False, None, 0.0, target_deb)
            if deb <= target_deb:
                return _realized_cf_result("SPREAD_DECAY_TARGET_70PCT_FRESH", "REALIZED_OUTCOME", snap, False, None, 0.0, target_deb)
            if hhmm >= 1515:
                return _realized_cf_result("EOD_1515_FRESH_RATCHET_BYPASSED", "REALIZED_OUTCOME", snap, False, None, 0.0, target_deb)

        st = "UNRESOLVED_STALE_BEFORE_ARMING" if (last_fresh or eod_stale) else "NO_FRESH_QUOTES"
        return {
            "cf_status": st,
            "outcome_class": "UNRESOLVED_OBSERVATION",
            "cf_exit_bar_ts": None,
            "cf_exit_reason": f"{st}_NO_SYNTHETIC_FILL",
            "cf_gross_pnl_inr": None,
            "cf_charges_inr": None,
            "cf_net_pnl_inr": None,
            "last_fresh_mark_net_inr": last_fresh["net"] if last_fresh else None,
            "last_fresh_bar_ts": last_fresh["bar_ts"] if last_fresh else None,
            "detail": {"last_fresh_snapshot": last_fresh, "eod_bar_stale": eod_stale},
        }

    # SINGLE_PAID_LONG (Book B)
    ll = legs_state["long_leg"]
    qty = int(ll["qty"])
    hard_stop = float(legs_state.get("hard_stop") if legs_state.get("hard_stop") is not None else (ll.get("hard_stop") or 0.0))
    is_bull = bool(legs_state["is_bull"]) if "is_bull" in legs_state else (str(ll["option_type"]).upper() == "CE")
    for item in bars:
        bt = str(item["bar_ts"])
        hhmm = int(bt[11:13]) * 100 + int(bt[14:16])
        q = _row_to_quote_dict(item["long_quote"])
        avail_ms = int(item.get("avail_ms") or q.get("availability_ts_ms") or _bar_open_epoch_ms(bt) + 60_000)
        if ist_hhmm_from_ms(avail_ms) > 1530:
            continue
        sw = sweep_observed_l5_book(
            q, "SELL", qty, lot_size, 1, max_quote_age_ms, avail_ms,
            max_spread_pct=None, require_observed_l5=False, allow_fresh_l1_only=True,
        )
        if not sw.get("ok"):
            if hhmm >= 1515:
                eod_stale = True
            continue
        ex_px = float(sw["fill_price"])
        ref_spot = float(item.get("spot_ltp") or q.get("spot_ltp") or 0.0)
        acc = compute_package_accounting(
            [OptionLeg(ll["option_type"], float(ll["strike"]), qty, float(ll["entry_price"]), ex_px, lot_size=lot_size)],
            trade_date=session_date,
            reference_spot=ref_spot,
        )
        snap = {"bar_ts": bt, "px": ex_px, "debit": ex_px, "gross": float(acc["gross_pnl"]), "charges": float(acc["total_friction"]), "net": float(acc["net_pnl"])}
        last_fresh = snap

        held_mins = max(0, (_bar_open_epoch_ms(bt) - entry_ms) // 60_000)
        s_now = item.get("spot_ltp") or (q.get("spot_ltp") or None)
        t_now = item.get("causal_tpb_vwap")
        s_prev = item.get("prev_spot_ltp") if "prev_spot_ltp" in item else prev_spot_from_bars
        t_prev = item.get("prev_tpb_vwap") if "prev_tpb_vwap" in item else prev_tpb_from_bars
        if s_now and t_now is not None:
            prev_spot_from_bars = float(s_now)
            prev_tpb_from_bars = float(t_now)
        vflip = _check_causal_vwap_flip(
            is_bull, held_mins,
            float(s_now) if s_now else None,
            float(t_now) if t_now is not None else None,
            float(s_prev) if s_prev else None,
            float(t_prev) if t_prev is not None else None,
            explicit_bar_flip=item.get("vwap_flipped"),
            v1_exit_reason=v1_exit_reason,
            v1_exit_bar_ts=v1_exit_bar_ts,
            cur_bar_ts=bt,
        )
        if ex_px <= hard_stop:
            return {
                "cf_status": "REALIZED_OUTCOME",
                "outcome_class": "REALIZED_OUTCOME",
                "cf_exit_bar_ts": bt,
                "cf_exit_reason": "LONG_HARD_SL_HIT",
                "cf_gross_pnl_inr": snap["gross"],
                "cf_charges_inr": snap["charges"],
                "cf_net_pnl_inr": snap["net"],
                "last_fresh_mark_net_inr": snap["net"],
                "last_fresh_bar_ts": bt,
                "detail": {"hard_stop": hard_stop, "exit_px": ex_px},
            }
        if vflip:
            return {
                "cf_status": "REALIZED_OUTCOME",
                "outcome_class": "REALIZED_OUTCOME",
                "cf_exit_bar_ts": bt,
                "cf_exit_reason": "SPOT_VWAP_FLIP_EXIT",
                "cf_gross_pnl_inr": snap["gross"],
                "cf_charges_inr": snap["charges"],
                "cf_net_pnl_inr": snap["net"],
                "last_fresh_mark_net_inr": snap["net"],
                "last_fresh_bar_ts": bt,
                "detail": {"hard_stop": hard_stop, "exit_px": ex_px},
            }
        if hhmm >= 1515:
            return {
                "cf_status": "REALIZED_OUTCOME",
                "outcome_class": "REALIZED_OUTCOME",
                "cf_exit_bar_ts": bt,
                "cf_exit_reason": "EOD_1515_FRESH_RATCHET_BYPASSED",
                "cf_gross_pnl_inr": snap["gross"],
                "cf_charges_inr": snap["charges"],
                "cf_net_pnl_inr": snap["net"],
                "last_fresh_mark_net_inr": snap["net"],
                "last_fresh_bar_ts": bt,
                "detail": {"hard_stop": hard_stop, "exit_px": ex_px},
            }

    st = "UNRESOLVED_STALE_RATCHET_BYPASS" if (last_fresh or eod_stale) else "NO_FRESH_QUOTES"
    return {
        "cf_status": st,
        "outcome_class": "UNRESOLVED_OBSERVATION",
        "cf_exit_bar_ts": None,
        "cf_exit_reason": f"{st}_NO_SYNTHETIC_FILL",
        "cf_gross_pnl_inr": None,
        "cf_charges_inr": None,
        "cf_net_pnl_inr": None,
        "last_fresh_mark_net_inr": last_fresh["net"] if last_fresh else None,
        "last_fresh_bar_ts": last_fresh["bar_ts"] if last_fresh else None,
        "detail": {"hard_stop": hard_stop, "last_fresh_snapshot": last_fresh, "eod_bar_stale": eod_stale},
    }


def run_v1_counterfactual_audit(
    v1_ro_conn: sqlite3.Connection,
    store: Optional[V2LedgerStore] = None,
) -> Dict[str, Any]:
    """Computes both isolated V1 counterfactual studies strictly causally on fresh archived quotes."""
    pkgs = v1_ro_conn.execute(
        """
        SELECT package_id, session_date, book_id, entry_bar_ts, exit_bar_ts, exit_reason,
               legs_state_json, gross_pnl_inr, total_charges_inr, net_pnl_inr
        FROM option_sniffer_advance_s205_shadow_packages
        ORDER BY entry_bar_ts ASC;
        """
    ).fetchall()

    needed_keys = set()
    parsed_pkgs: List[Dict[str, Any]] = []
    for r in pkgs:
        d = dict(r)
        ls = json.loads(d["legs_state_json"] or "{}")
        d["legs_state"] = ls
        for lk in ("wing_leg", "short_leg", "long_leg"):
            if isinstance(ls.get(lk), dict) and ls[lk].get("instrument_key"):
                needed_keys.add(str(ls[lk]["instrument_key"]))
        parsed_pkgs.append(d)

    chain_by_key_date: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
    if needed_keys:
        ph = ",".join("?" for _ in needed_keys)
        for cr in v1_ro_conn.execute(
            f"""
            SELECT * FROM option_sniffer_advance_s205_l5_chain_1m
            WHERE instrument_key IN ({ph})
            ORDER BY bar_ts ASC, recorded_at ASC;
            """,
            sorted(needed_keys),
        ).fetchall():
            chain_by_key_date.setdefault((cr["session_date"], cr["instrument_key"]), []).append(cr)

    spot_vwap_by_dt = _build_causal_spot_vwap_by_session(v1_ro_conn)
    out_rows: List[Dict[str, Any]] = []

    for p in parsed_pkgs:
        pid = p["package_id"]
        dt = p["session_date"]
        bid = p["book_id"]
        ls = p["legs_state"]
        rep_net = float(p["net_pnl_inr"])
        spot_series = spot_vwap_by_dt.get(dt, [])

        # Build chronological post-entry bar series
        if ls.get("mode") == "DEFINED_RISK_CREDIT_SPREAD_150":
            wl = ls["wing_leg"]
            sl = ls["short_leg"]
            wb_map = {r["bar_ts"]: r for r in chain_by_key_date.get((dt, wl["instrument_key"]), []) if r["bar_ts"] > p["entry_bar_ts"] and r["bar_ts"][11:16] <= "15:15"}
            sb_map = {r["bar_ts"]: r for r in chain_by_key_date.get((dt, sl["instrument_key"]), []) if r["bar_ts"] > p["entry_bar_ts"] and r["bar_ts"][11:16] <= "15:15"}
            common = sorted(set(wb_map.keys()) & set(sb_map.keys()))
            paired_bars: List[Dict[str, Any]] = []
            for bt in common:
                wr, sr = wb_map[bt], sb_map[bt]
                avail_w = parse_ist_timestamp_ms(wr["recorded_at"] or bt) or (_bar_open_epoch_ms(bt) + 60_000)
                avail_s = parse_ist_timestamp_ms(sr["recorded_at"] or bt) or (_bar_open_epoch_ms(bt) + 60_000)
                avail_ms = max(avail_w, avail_s)
                s_prev, t_prev, s_now, t_now = _causal_spot_and_vwap_at(
                    spot_series, avail_ms, fallback_spot=float(sr["spot_ltp"] or 0.0)
                )
                paired_bars.append(
                    {
                        "bar_ts": bt,
                        "avail_ms": avail_ms,
                        "wing_quote": wr,
                        "short_quote": sr,
                        "spot_ltp": s_now,
                        "causal_tpb_vwap": t_now,
                        "prev_spot_ltp": s_prev,
                        "prev_tpb_vwap": t_prev,
                    }
                )

            entry_credit_pts = float(ls.get("net_entry_credit_pts") or round(float(sl["entry_price"]) - float(wl["entry_price"]), 2))
            res_cf1 = simulate_cf1_spread_trajectory(
                entry_bar_ts=p["entry_bar_ts"],
                session_date=dt,
                entry_credit_pts=entry_credit_pts,
                hard_stop_debit_pts=float(ls["hard_stop_debit_pts"]),
                wing_leg=wl,
                short_leg=sl,
                paired_bars=paired_bars,
                v1_exit_reason=p["exit_reason"],
                v1_exit_bar_ts=p["exit_bar_ts"],
            )
            out_rows.append(
                {
                    "cf_id": make_stable_id("osv2_cf", "CF1_TRUE_FRESH_70PCT_CONTINUATION", pid),
                    "experiment_code": "CF1_TRUE_FRESH_70PCT_CONTINUATION",
                    "v1_package_id": pid,
                    "session_date": dt,
                    "book_id": bid,
                    "v1_reported_net_inr": rep_net,
                    "cf_status": res_cf1["cf_status"],
                    "outcome_class": res_cf1["outcome_class"],
                    "cf_exit_bar_ts": res_cf1["cf_exit_bar_ts"],
                    "cf_exit_reason": res_cf1["cf_exit_reason"],
                    "cf_gross_pnl_inr": res_cf1["cf_gross_pnl_inr"],
                    "cf_charges_inr": res_cf1["cf_charges_inr"],
                    "cf_net_pnl_inr": res_cf1["cf_net_pnl_inr"],
                    "detail": {
                        **res_cf1["detail"],
                        "last_fresh_mark_net_inr": res_cf1["last_fresh_mark_net_inr"],
                        "last_fresh_bar_ts": res_cf1["last_fresh_bar_ts"],
                    },
                }
            )

            res_cf2 = simulate_cf2_trajectory(
                mode="DEFINED_RISK_CREDIT_SPREAD_150",
                entry_bar_ts=p["entry_bar_ts"],
                session_date=dt,
                legs_state=ls,
                bars=paired_bars,
                v1_exit_reason=p["exit_reason"],
                v1_exit_bar_ts=p["exit_bar_ts"],
            )
            out_rows.append(
                {
                    "cf_id": make_stable_id("osv2_cf", "CF2_RATCHET_BYPASS_EXPERIMENT", pid),
                    "experiment_code": "CF2_RATCHET_BYPASS_EXPERIMENT",
                    "v1_package_id": pid,
                    "session_date": dt,
                    "book_id": bid,
                    "v1_reported_net_inr": rep_net,
                    "cf_status": res_cf2["cf_status"],
                    "outcome_class": res_cf2["outcome_class"],
                    "cf_exit_bar_ts": res_cf2["cf_exit_bar_ts"],
                    "cf_exit_reason": res_cf2["cf_exit_reason"],
                    "cf_gross_pnl_inr": res_cf2["cf_gross_pnl_inr"],
                    "cf_charges_inr": res_cf2["cf_charges_inr"],
                    "cf_net_pnl_inr": res_cf2["cf_net_pnl_inr"],
                    "detail": {
                        **res_cf2["detail"],
                        "last_fresh_mark_net_inr": res_cf2["last_fresh_mark_net_inr"],
                        "last_fresh_bar_ts": res_cf2["last_fresh_bar_ts"],
                    },
                }
            )
        else:
            ll = ls["long_leg"]
            out_rows.append(
                {
                    "cf_id": make_stable_id("osv2_cf", "CF1_TRUE_FRESH_70PCT_CONTINUATION", pid),
                    "experiment_code": "CF1_TRUE_FRESH_70PCT_CONTINUATION",
                    "v1_package_id": pid,
                    "session_date": dt,
                    "book_id": bid,
                    "v1_reported_net_inr": rep_net,
                    "cf_status": "PRESERVED_V1_NON_SPREAD",
                    "outcome_class": "REALIZED_OUTCOME",
                    "cf_exit_bar_ts": p["exit_bar_ts"],
                    "cf_exit_reason": p["exit_reason"],
                    "cf_gross_pnl_inr": float(p["gross_pnl_inr"]),
                    "cf_charges_inr": float(p["total_charges_inr"]),
                    "cf_net_pnl_inr": rep_net,
                    "detail": {"last_fresh_mark_net_inr": rep_net, "note": "Single-leg Book B unaffected by stale-wing decay bug."},
                }
            )
            single_bars: List[Dict[str, Any]] = []
            for br in chain_by_key_date.get((dt, ll["instrument_key"]), []):
                if br["bar_ts"] <= p["entry_bar_ts"] or br["bar_ts"][11:16] > "15:15":
                    continue
                avail_ms = parse_ist_timestamp_ms(br["recorded_at"] or br["bar_ts"]) or (_bar_open_epoch_ms(br["bar_ts"]) + 60_000)
                s_prev, t_prev, s_now, t_now = _causal_spot_and_vwap_at(
                    spot_series, avail_ms, fallback_spot=float(br["spot_ltp"] or 0.0)
                )
                single_bars.append(
                    {
                        "bar_ts": br["bar_ts"],
                        "avail_ms": avail_ms,
                        "long_quote": br,
                        "spot_ltp": s_now,
                        "causal_tpb_vwap": t_now,
                        "prev_spot_ltp": s_prev,
                        "prev_tpb_vwap": t_prev,
                    }
                )
            res_cf2 = simulate_cf2_trajectory(
                mode="SINGLE_PAID_LONG",
                entry_bar_ts=p["entry_bar_ts"],
                session_date=dt,
                legs_state=ls,
                bars=single_bars,
                v1_exit_reason=p["exit_reason"],
                v1_exit_bar_ts=p["exit_bar_ts"],
            )
            out_rows.append(
                {
                    "cf_id": make_stable_id("osv2_cf", "CF2_RATCHET_BYPASS_EXPERIMENT", pid),
                    "experiment_code": "CF2_RATCHET_BYPASS_EXPERIMENT",
                    "v1_package_id": pid,
                    "session_date": dt,
                    "book_id": bid,
                    "v1_reported_net_inr": rep_net,
                    "cf_status": res_cf2["cf_status"],
                    "outcome_class": res_cf2["outcome_class"],
                    "cf_exit_bar_ts": res_cf2["cf_exit_bar_ts"],
                    "cf_exit_reason": res_cf2["cf_exit_reason"],
                    "cf_gross_pnl_inr": res_cf2["cf_gross_pnl_inr"],
                    "cf_charges_inr": res_cf2["cf_charges_inr"],
                    "cf_net_pnl_inr": res_cf2["cf_net_pnl_inr"],
                    "detail": {
                        **res_cf2["detail"],
                        "last_fresh_mark_net_inr": res_cf2["last_fresh_mark_net_inr"],
                        "last_fresh_bar_ts": res_cf2["last_fresh_bar_ts"],
                    },
                }
            )

    if store is not None:
        store.replace_counterfactuals(out_rows)

    return summarize_counterfactual_rows(out_rows)


def summarize_counterfactual_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    def _exp_summary(exp_code: str) -> Dict[str, Any]:
        sub = [r for r in rows if r["experiment_code"] == exp_code]
        realized = [r for r in sub if (r.get("detail") or {}).get("last_fresh_mark_net_inr") is not None and r.get("cf_net_pnl_inr") is not None and (r.get("outcome_class") == "REALIZED_OUTCOME" or r.get("cf_status") in ("REALIZED_OUTCOME", "PRESERVED_V1_NON_SPREAD", "PRESERVED_V1_CLEAN_EXIT", "FRESH_RESOLVED"))]
        unresolved = [r for r in sub if r not in realized]
        return {
            "total_v1_packages": len(sub),
            "realized_count": len(realized),
            "unresolved_count": len(unresolved),
            "unresolved_stale_after_arming_count": sum(1 for r in unresolved if r["cf_status"] == "UNRESOLVED_STALE_AFTER_ARMING"),
            "unresolved_stale_before_arming_count": sum(1 for r in unresolved if r["cf_status"] in ("UNRESOLVED_STALE_BEFORE_ARMING", "UNRESOLVED_AT_1515_STALE_WING")),
            "realized_net_pnl_sum_inr": round(sum(float(r["cf_net_pnl_inr"] or 0.0) for r in realized), 2),
            "unresolved_last_fresh_mark_sum_inr": round(
                sum(float((r.get("detail") or {}).get("last_fresh_mark_net_inr") or 0.0) for r in unresolved), 2
            ),
        }

    return {
        "included_in_v2_totals": False,
        "disclaimer": "COUNTERFACTUAL_V1_STUDY_ONLY: Strictly excluded from V2 ledger, equity, drawdown, and promotion gates.",
        "cf1_summary": _exp_summary("CF1_TRUE_FRESH_70PCT_CONTINUATION"),
        "cf2_summary": _exp_summary("CF2_RATCHET_BYPASS_EXPERIMENT"),
        "rows": rows,
    }
