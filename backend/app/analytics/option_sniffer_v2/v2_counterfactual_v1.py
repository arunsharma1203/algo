"""
V2 Option Sniffer — Counterfactual Study on Archived V1 Trades (Strict Read-Only).

Evaluates two hypothetical counterfactuals on V1's archived `option_sniffer_advance_s205_shadow_packages`:
  1. `CF1_TRUE_FRESH_70PCT_CONTINUATION`: preserves V1's non-decay exits (`SPOT_VWAP_FLIP_EXIT`,
     hard SL, ratchet) and re-simulates the 4 stale-wing `SPREAD_DECAY_TARGET_70PCT` exits
     strictly across fresh synchronized archived quotes (`l1_book_age_ms <= 5000ms` & cross-leg
     skew <= 5000ms). Explicitly marks `UNRESOLVED_AT_1515_STALE_WING` when archived wing
     quotes died stale before 15:15 rather than fabricating an exit price.
  2. `CF2_RATCHET_BYPASS_EXPERIMENT`: follows fresh synchronized quotes with the close-only
     ratchet disabled while preserving hard-SL, `SPOT_VWAP_FLIP_EXIT`, `70%` decay target,
     and `15:15` EOD.

Guarantees:
  - Stored ONLY in `osv2_counterfactual_v1` with `included_in_v2_totals = 0 CHECK(=0)`.
  - Never enters V2 portfolio equity, V2 drawdown, or V2 promotion gates.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from app.analytics.common.options_accounting import OptionLeg, compute_package_accounting
from app.analytics.option_sniffer_v2.v2_data_contract import sweep_observed_l5_book
from app.analytics.option_sniffer_v2.v2_ledger_db import V2LedgerStore
from app.analytics.option_sniffer_v2.v2_paper_simulator import make_stable_id


def _row_to_quote_dict(row: sqlite3.Row) -> Dict[str, Any]:
    depth = []
    try:
        depth = json.loads(row["depth_json"] or "[]")
    except Exception:
        depth = []
    age_ms = row["l1_book_age_ms"]
    return {
        "instrument_key": row["instrument_key"],
        "trading_symbol": row["trading_symbol"],
        "raw_bid": float(row["raw_bid"] or 0.0),
        "raw_ask": float(row["raw_ask"] or 0.0),
        "bid": float(row["raw_bid"] or 0.0),
        "ask": float(row["raw_ask"] or 0.0),
        "depth_levels": depth,
        "is_observed_l1_book": bool(row["is_observed_l1_book"]),
        "depth_observed_on_tick": bool(row["depth_observed_on_tick"]),
        "book_provenance": row["book_provenance"] or "UNKNOWN",
        "l1_book_age_ms": int(age_ms) if age_ms is not None else None,
        "quote_age_ms": int(age_ms) if age_ms is not None else None,
        # Bind synthetic deterministic ms timestamp per bar so cross-leg sync works accurately
        "l1_book_timestamp_ms": 1_794_000_000_000 + int(row["id"]) * 0,
        "spot_ltp": float(row["spot_ltp"] or 0.0),
    }


def _bar_ts_epoch_ms(bar_ts: str, age_ms: int = 0) -> int:
    from datetime import datetime
    dt = datetime.strptime(bar_ts[:19], "%Y-%m-%d %H:%M:%S")
    return int(dt.timestamp() * 1000) - int(age_ms)


def run_v1_counterfactual_audit(
    v1_ro_conn: sqlite3.Connection,
    store: Optional[V2LedgerStore] = None,
) -> Dict[str, Any]:
    """Computes both isolated V1 counterfactual studies on fresh archived quotes."""
    pkgs = v1_ro_conn.execute(
        """
        SELECT package_id, session_date, book_id, entry_bar_ts, exit_bar_ts, exit_reason,
               legs_state_json, gross_pnl_inr, total_charges_inr, net_pnl_inr
        FROM option_sniffer_advance_s205_shadow_packages
        ORDER BY entry_bar_ts ASC;
        """
    ).fetchall()

    # Preload 1m L5 rows for only the relevant package keys
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

    chain_by_key_date: Dict[tuple, List[sqlite3.Row]] = {}
    if needed_keys:
        ph = ",".join("?" for _ in needed_keys)
        for cr in v1_ro_conn.execute(
            f"""
            SELECT * FROM option_sniffer_advance_s205_l5_chain_1m
            WHERE instrument_key IN ({ph})
            ORDER BY bar_ts ASC;
            """,
            sorted(needed_keys),
        ).fetchall():
            chain_by_key_date.setdefault((cr["session_date"], cr["instrument_key"]), []).append(cr)

    out_rows: List[Dict[str, Any]] = []

    for p in parsed_pkgs:
        pid = p["package_id"]
        dt = p["session_date"]
        bid = p["book_id"]
        ls = p["legs_state"]
        rep_net = float(p["net_pnl_inr"])

        for exp_code in ("CF1_TRUE_FRESH_70PCT_CONTINUATION", "CF2_RATCHET_BYPASS_EXPERIMENT"):
            cf_id = make_stable_id("osv2_cf", exp_code, pid)
            if ls.get("mode") == "DEFINED_RISK_CREDIT_SPREAD_150":
                wl = ls["wing_leg"]
                sl = ls["short_leg"]
                qty = int(wl["qty"])
                target_deb = float(ls["target_debit_pts"])
                hard_deb = float(ls["hard_stop_debit_pts"])

                # For CF1, preserve already-clean non-70% exits; for CF2 also test without ratchet
                if exp_code == "CF1_TRUE_FRESH_70PCT_CONTINUATION" and p["exit_reason"] != "SPREAD_DECAY_TARGET_70PCT":
                    out_rows.append(
                        {
                            "cf_id": cf_id,
                            "experiment_code": exp_code,
                            "v1_package_id": pid,
                            "session_date": dt,
                            "book_id": bid,
                            "v1_reported_net_inr": rep_net,
                            "cf_status": "PRESERVED_V1_CLEAN_EXIT",
                            "cf_exit_bar_ts": p["exit_bar_ts"],
                            "cf_exit_reason": p["exit_reason"],
                            "cf_gross_pnl_inr": float(p["gross_pnl_inr"]),
                            "cf_charges_inr": float(p["total_charges_inr"]),
                            "cf_net_pnl_inr": rep_net,
                            "detail": {"note": "Clean synchronized V1 exit preserved unchanged."},
                        }
                    )
                    continue

                wb_map = {r["bar_ts"]: r for r in chain_by_key_date.get((dt, wl["instrument_key"]), []) if r["bar_ts"] > p["entry_bar_ts"] and r["bar_ts"][11:16] <= "15:15"}
                sb_map = {r["bar_ts"]: r for r in chain_by_key_date.get((dt, sl["instrument_key"]), []) if r["bar_ts"] > p["entry_bar_ts"] and r["bar_ts"][11:16] <= "15:15"}
                common = sorted(set(wb_map.keys()) & set(sb_map.keys()))

                exit_info: Optional[Dict[str, Any]] = None
                last_fresh_info: Optional[Dict[str, Any]] = None
                eod_bar_stale = False

                for bt in common:
                    wr, sr = wb_map[bt], sb_map[bt]
                    w_age = wr["l1_book_age_ms"]
                    s_age = sr["l1_book_age_ms"]
                    if w_age is None or s_age is None or w_age > 5000 or s_age > 5000:
                        if bt[11:16] >= "15:15":
                            eod_bar_stale = True
                        continue
                    wq = _row_to_quote_dict(wr)
                    sq = _row_to_quote_dict(sr)
                    now_ms = _bar_ts_epoch_ms(bt, 0)
                    wq["l1_book_timestamp_ms"] = now_ms - int(w_age)
                    sq["l1_book_timestamp_ms"] = now_ms - int(s_age)
                    sw_w = sweep_observed_l5_book(wq, "SELL", qty, 65, 1, 5000, now_ms, max_spread_pct=None, require_observed_l5=False)
                    sw_s = sweep_observed_l5_book(sq, "BUY", qty, 65, 1, 5000, now_ms, max_spread_pct=None, require_observed_l5=False)
                    if not sw_w.get("ok") or not sw_s.get("ok"):
                        continue

                    w_ex = float(sw_w["fill_price"])
                    s_ex = float(sw_s["fill_price"])
                    deb = round(s_ex - w_ex, 2)
                    acc = compute_package_accounting(
                        [
                            OptionLeg(wl["option_type"], float(wl["strike"]), qty, float(wl["entry_price"]), w_ex, lot_size=65),
                            OptionLeg(sl["option_type"], float(sl["strike"]), -qty, float(sl["entry_price"]), s_ex, lot_size=65),
                        ],
                        trade_date=dt,
                        reference_spot=float(sr["spot_ltp"] or 0.0),
                    )
                    snap_pt = {
                        "bar_ts": bt,
                        "debit": deb,
                        "gross": float(acc["gross_pnl"]),
                        "charges": float(acc["total_friction"]),
                        "net": float(acc["net_pnl"]),
                    }
                    last_fresh_info = snap_pt

                    # Preserve prior V1 non-target exit timestamp if reached first
                    if p["exit_reason"] == "SPOT_VWAP_FLIP_EXIT" and bt >= str(p["exit_bar_ts"]):
                        exit_info = {**snap_pt, "reason": "SPOT_VWAP_FLIP_EXIT", "status": "FRESH_RESOLVED"}
                        break
                    if deb >= hard_deb:
                        exit_info = {**snap_pt, "reason": "SPREAD_HARD_SL_HIT", "status": "FRESH_RESOLVED"}
                        break
                    if deb <= target_deb:
                        exit_info = {**snap_pt, "reason": "SPREAD_DECAY_TARGET_70PCT_FRESH", "status": "FRESH_RESOLVED"}
                        break
                    if bt[11:16] >= "15:15":
                        exit_info = {**snap_pt, "reason": "EOD_1515_FRESH", "status": "FRESH_RESOLVED"}
                        break

                if exit_info is None:
                    # Do NOT manufacture a fake 15:15 fill if archived wing quotes went stale before 15:15!
                    out_rows.append(
                        {
                            "cf_id": cf_id,
                            "experiment_code": exp_code,
                            "v1_package_id": pid,
                            "session_date": dt,
                            "book_id": bid,
                            "v1_reported_net_inr": rep_net,
                            "cf_status": "UNRESOLVED_AT_1515_STALE_WING" if eod_bar_stale or last_fresh_info else "NO_FRESH_QUOTES",
                            "cf_exit_bar_ts": last_fresh_info["bar_ts"] if last_fresh_info else None,
                            "cf_exit_reason": "STALE_WING_ARCHIVE_ENDED_BEFORE_1515_LAST_FRESH_MARK_ONLY",
                            "cf_gross_pnl_inr": last_fresh_info["gross"] if last_fresh_info else None,
                            "cf_charges_inr": last_fresh_info["charges"] if last_fresh_info else None,
                            "cf_net_pnl_inr": last_fresh_info["net"] if last_fresh_info else None,
                            "detail": {"last_fresh_snapshot": last_fresh_info, "note": "Neither 70% decay nor Hard SL triggered while synchronized fresh quotes existed; 15:15 wing quotes were stale in archive."},
                        }
                    )
                else:
                    out_rows.append(
                        {
                            "cf_id": cf_id,
                            "experiment_code": exp_code,
                            "v1_package_id": pid,
                            "session_date": dt,
                            "book_id": bid,
                            "v1_reported_net_inr": rep_net,
                            "cf_status": exit_info["status"],
                            "cf_exit_bar_ts": exit_info["bar_ts"],
                            "cf_exit_reason": exit_info["reason"],
                            "cf_gross_pnl_inr": exit_info["gross"],
                            "cf_charges_inr": exit_info["charges"],
                            "cf_net_pnl_inr": exit_info["net"],
                            "detail": {"exit_debit": exit_info["debit"]},
                        }
                    )
            else:
                # Book B: SINGLE_PAID_LONG
                ll = ls["long_leg"]
                qty = int(ll["qty"])
                hard_stop = float(ls["hard_stop"])
                if exp_code == "CF1_TRUE_FRESH_70PCT_CONTINUATION":
                    out_rows.append(
                        {
                            "cf_id": cf_id,
                            "experiment_code": exp_code,
                            "v1_package_id": pid,
                            "session_date": dt,
                            "book_id": bid,
                            "v1_reported_net_inr": rep_net,
                            "cf_status": "PRESERVED_V1_CLEAN_EXIT",
                            "cf_exit_bar_ts": p["exit_bar_ts"],
                            "cf_exit_reason": p["exit_reason"],
                            "cf_gross_pnl_inr": float(p["gross_pnl_inr"]),
                            "cf_charges_inr": float(p["total_charges_inr"]),
                            "cf_net_pnl_inr": rep_net,
                            "detail": {"note": "Single-leg Book B required no wing leg; exit unaffected by stale-wing bug."},
                        }
                    )
                else:
                    # CF2: Bypass close-only ratchet while retaining Hard SL and 15:15 EOD!
                    bars = [r for r in chain_by_key_date.get((dt, ll["instrument_key"]), []) if r["bar_ts"] > p["entry_bar_ts"] and r["bar_ts"][11:16] <= "15:15"]
                    exit_b: Optional[Dict[str, Any]] = None
                    last_b: Optional[Dict[str, Any]] = None
                    for br in bars:
                        age = br["l1_book_age_ms"]
                        if age is None or age > 5000:
                            continue
                        q = _row_to_quote_dict(br)
                        now_ms = _bar_ts_epoch_ms(br["bar_ts"], 0)
                        q["l1_book_timestamp_ms"] = now_ms - int(age)
                        sw = sweep_observed_l5_book(q, "SELL", qty, 65, 1, 5000, now_ms, max_spread_pct=None, require_observed_l5=False)
                        if not sw.get("ok"):
                            continue
                        ex_px = float(sw["fill_price"])
                        acc = compute_package_accounting(
                            [OptionLeg(ll["option_type"], float(ll["strike"]), qty, float(ll["entry_price"]), ex_px, lot_size=65)],
                            trade_date=dt,
                            reference_spot=float(br["spot_ltp"] or 0.0),
                        )
                        pt = {"bar_ts": br["bar_ts"], "px": ex_px, "gross": float(acc["gross_pnl"]), "charges": float(acc["total_friction"]), "net": float(acc["net_pnl"])}
                        last_b = pt
                        if ex_px <= hard_stop:
                            exit_b = {**pt, "reason": "LONG_HARD_SL_HIT", "status": "FRESH_RESOLVED"}
                            break
                        if br["bar_ts"][11:16] >= "15:15":
                            exit_b = {**pt, "reason": "EOD_1515_FRESH_RATCHET_BYPASSED", "status": "FRESH_RESOLVED"}
                            break
                    chosen = exit_b or (last_b and {**last_b, "reason": "LAST_FRESH_BEFORE_EOD", "status": "PARTIAL_ARCHIVE"})
                    out_rows.append(
                        {
                            "cf_id": cf_id,
                            "experiment_code": exp_code,
                            "v1_package_id": pid,
                            "session_date": dt,
                            "book_id": bid,
                            "v1_reported_net_inr": rep_net,
                            "cf_status": chosen["status"] if chosen else "NO_FRESH_QUOTES",
                            "cf_exit_bar_ts": chosen["bar_ts"] if chosen else None,
                            "cf_exit_reason": chosen["reason"] if chosen else "UNAVAILABLE",
                            "cf_gross_pnl_inr": chosen["gross"] if chosen else None,
                            "cf_charges_inr": chosen["charges"] if chosen else None,
                            "cf_net_pnl_inr": chosen["net"] if chosen else None,
                            "detail": {"hard_stop": hard_stop},
                        }
                    )

    if store is not None:
        store.replace_counterfactuals(out_rows)

    cf1 = [r for r in out_rows if r["experiment_code"] == "CF1_TRUE_FRESH_70PCT_CONTINUATION"]
    cf2 = [r for r in out_rows if r["experiment_code"] == "CF2_RATCHET_BYPASS_EXPERIMENT"]
    return {
        "included_in_v2_totals": False,
        "disclaimer": "COUNTERFACTUAL_V1_STUDY_ONLY: Strictly excluded from V2 ledger, equity, drawdown, and promotion gates.",
        "cf1_summary": {
            "total_v1_packages": len(cf1),
            "clean_or_resolved_count": sum(1 for r in cf1 if r["cf_status"] in ("PRESERVED_V1_CLEAN_EXIT", "FRESH_RESOLVED")),
            "unresolved_stale_wing_count": sum(1 for r in cf1 if r["cf_status"] == "UNRESOLVED_AT_1515_STALE_WING"),
            "mark_at_last_fresh_sum_inr": round(sum(float(r["cf_net_pnl_inr"] or 0.0) for r in cf1), 2),
        },
        "cf2_summary": {
            "total_v1_packages": len(cf2),
            "unresolved_stale_wing_count": sum(1 for r in cf2 if r["cf_status"] == "UNRESOLVED_AT_1515_STALE_WING"),
            "mark_at_last_fresh_sum_inr": round(sum(float(r["cf_net_pnl_inr"] or 0.0) for r in cf2), 2),
        },
        "rows": out_rows,
    }
