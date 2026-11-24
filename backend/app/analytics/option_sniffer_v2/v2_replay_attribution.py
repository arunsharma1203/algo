"""
V2 Option Sniffer — Archived L5 Replay, Single-Factor Book B Attribution,
Sensitivity Suite (`2x` Charges, `2x` Slippage, Ex-Best-Session) & V1/V2 Daily Comparison.

Strictly causal (`bar_ts <= t`) and strictly read-only on `backend/market_data.db`.
Persists canonical replay into V2's isolated SQLite DB (`run_mode = 'REPLAY'`).
"""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.common.options_accounting import OptionLeg, compute_package_accounting
from app.analytics.option_sniffer_v2.v2_book_a_engine import (
    evaluate_book_a_entry_signal,
    manage_open_book_a_on_bar,
)
from app.analytics.option_sniffer_v2.v2_book_b_engine import (
    evaluate_book_b_entry_signal,
    manage_open_book_b_on_bar,
)
from app.analytics.option_sniffer_v2.v2_data_contract import check_spot_futures_sync
from app.analytics.option_sniffer_v2.v2_ledger_db import V2LedgerStore
from app.analytics.option_sniffer_v2.v2_paper_simulator import (
    OPEN_EXPOSURE_STATES,
    V2PaperSimulator,
    make_stable_id,
)


def _bar_ts_ms(bar_ts: str, age_ms: int = 0) -> int:
    dt = datetime.strptime(bar_ts[:19], "%Y-%m-%d %H:%M:%S")
    return int(dt.timestamp() * 1000) - int(age_ms)


def _parse_adx_er_from_snap(readiness_json: str) -> Tuple[float, float]:
    try:
        rows = json.loads(readiness_json or "[]")
        for er in rows:
            if er.get("engine_id") == "SHADOW_BOOK_B_PAID_FLOW":
                for g in er.get("gates") or []:
                    if g.get("name") == "ADX/ER":
                        cur = str(g.get("current") or "0/0")
                        a_s, e_s = cur.split("/", 1)
                        return float(a_s), float(e_s)
    except Exception:
        pass
    return 0.0, 0.0


def _compute_series_stats(
    closed_trades: List[Dict[str, Any]],
    daily_records: List[Dict[str, Any]],
    starting_capital_inr: float,
) -> Dict[str, Any]:
    gross_wins = sum(float(t["gross_pnl_inr"]) for t in closed_trades if float(t["net_pnl_inr"]) > 0)
    gross_losses = abs(sum(float(t["gross_pnl_inr"]) for t in closed_trades if float(t["net_pnl_inr"]) < 0))
    net_wins = sum(float(t["net_pnl_inr"]) for t in closed_trades if float(t["net_pnl_inr"]) > 0)
    net_losses = abs(sum(float(t["net_pnl_inr"]) for t in closed_trades if float(t["net_pnl_inr"]) < 0))

    pf = round(net_wins / net_losses, 3) if net_losses > 0 else (99.0 if net_wins > 0 else 0.0)
    if net_wins == 0 and net_losses == 0:
        pf = 0.0

    # Closed equity drawdown
    eq_c = float(starting_capital_inr)
    peak_c = eq_c
    closed_dd_inr = 0.0
    for t in closed_trades:
        eq_c = round(eq_c + float(t["net_pnl_inr"]), 2)
        if eq_c > peak_c:
            peak_c = eq_c
        dd = round(peak_c - eq_c, 2)
        if dd > closed_dd_inr:
            closed_dd_inr = dd

    # Intraday & EOD MTM drawdown (includes conservative worst-case unresolved risk)
    peak_m = float(starting_capital_inr)
    mtm_dd_inr = 0.0
    peak_cap = 0.0
    for d in daily_records:
        # Strict intraday chronology within session `d`: intraday minimum happens BEFORE EOD close
        for val in (float(d["intraday_min_mtm_equity_inr"]), float(d["eod_mtm_equity_inr"])):
            if val > peak_m:
                peak_m = val
            dd = round(peak_m - val, 2)
            if dd > mtm_dd_inr:
                mtm_dd_inr = dd
        peak_cap = max(peak_cap, float(d["peak_reserved_capital_inr"]))

    tot_gross = round(sum(float(t["gross_pnl_inr"]) for t in closed_trades), 2)
    tot_fric = round(sum(float(t["total_friction_inr"]) for t in closed_trades), 2)
    tot_net = round(sum(float(t["net_pnl_inr"]) for t in closed_trades), 2)
    wins = sum(1 for t in closed_trades if float(t["net_pnl_inr"]) > 0)

    return {
        "trade_count": len(closed_trades),
        "wins": wins,
        "win_rate_pct": round(100.0 * wins / len(closed_trades), 2) if closed_trades else 0.0,
        "profit_factor": pf,
        "gross_pnl_inr": tot_gross,
        "total_charges_inr": tot_fric,
        "net_pnl_inr": tot_net,
        "closed_dd_inr": round(closed_dd_inr, 2),
        "closed_dd_pct": round(100.0 * closed_dd_inr / starting_capital_inr, 2),
        "intraday_mtm_dd_inr": round(mtm_dd_inr, 2),
        "intraday_mtm_dd_pct": round(100.0 * mtm_dd_inr / starting_capital_inr, 2),
        "peak_capital_used_inr": round(peak_cap, 2),
        "gross_wins_inr": round(gross_wins, 2),
        "gross_losses_inr": round(gross_losses, 2),
    }


def _load_archived_l5_dataset(v1_ro_conn: sqlite3.Connection) -> Dict[str, Any]:
    """Loads archived 1m snapshots & 1m L5 chain rows strictly read-only without future leakage."""
    snaps_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for r in v1_ro_conn.execute(
        """
        SELECT bar_ts, spot_ltp, fut_ltp, real_basis_pts, india_vix, readiness_json
        FROM option_sniffer_advance_s205_l5_snapshots
        ORDER BY bar_ts ASC;
        """
    ).fetchall():
        dt = r["bar_ts"][:10]
        if dt > "2026-11-23":
            continue
        adx14, er20 = _parse_adx_er_from_snap(r["readiness_json"])
        snaps_by_date.setdefault(dt, []).append(
            {
                "bar_ts": r["bar_ts"],
                "spot_ltp": float(r["spot_ltp"] or 0.0),
                "fut_ltp": float(r["fut_ltp"] or 0.0),
                "real_basis_pts": float(r["real_basis_pts"]) if r["real_basis_pts"] is not None else None,
                "india_vix": float(r["india_vix"]) if r["india_vix"] is not None else None,
                "adx14": adx14,
                "er20": er20,
            }
        )

    chain_by_bar: Dict[str, Dict[Tuple[str, float, str], Tuple[Dict[str, Any], Dict[str, Any]]]] = {}
    for cr in v1_ro_conn.execute(
        """
        SELECT session_date, bar_ts, instrument_key, trading_symbol, expiry, strike, option_type,
               raw_bid, raw_ask, ltp, depth_json, is_observed_l1_book, depth_observed_on_tick,
               book_provenance, l1_book_age_ms, book_obi_l5
        FROM option_sniffer_advance_s205_l5_chain_1m
        WHERE session_date BETWEEN '2026-11-16' AND '2026-11-23'
        ORDER BY bar_ts ASC;
        """
    ).fetchall():
        bt = cr["bar_ts"]
        now_ms = _bar_ts_ms(bt, 0)
        age_raw = cr["l1_book_age_ms"]
        age_ms = int(age_raw) if age_raw is not None else None
        l1_ts = (now_ms - age_ms) if (age_ms is not None and age_ms != 999999) else 0
        meta = {
            "instrument_key": cr["instrument_key"],
            "trading_symbol": cr["trading_symbol"],
            "expiry": cr["expiry"],
            "strike": float(cr["strike"]),
            "option_type": cr["option_type"],
            "lot_size": 65,
        }
        q = {
            "instrument_key": cr["instrument_key"],
            "trading_symbol": cr["trading_symbol"],
            "expiry": cr["expiry"],
            "strike": float(cr["strike"]),
            "option_type": cr["option_type"],
            "raw_bid": float(cr["raw_bid"] or 0.0),
            "raw_ask": float(cr["raw_ask"] or 0.0),
            "bid": float(cr["raw_bid"] or 0.0),
            "ask": float(cr["raw_ask"] or 0.0),
            "ltp": float(cr["ltp"] or 0.0),
            "depth_levels": None,
            "depth_json_raw": cr["depth_json"],
            "is_observed_l1_book": bool(cr["is_observed_l1_book"]),
            "depth_observed_on_tick": bool(cr["depth_observed_on_tick"]),
            "book_provenance": cr["book_provenance"] or "UNKNOWN",
            "l1_book_age_ms": age_ms,
            "quote_age_ms": age_ms,
            "l1_book_timestamp_ms": l1_ts,
            "local_receipt_ts_ms": l1_ts if l1_ts > 0 else now_ms,
            "exchange_ltt_ms": l1_ts if l1_ts > 0 else now_ms,
            "book_obi_l5": float(cr["book_obi_l5"] or 0.0),
        }
        chain_by_bar.setdefault(bt, {})[(cr["expiry"], float(cr["strike"]), cr["option_type"])] = (meta, q)

    bar_packs: Dict[str, Tuple[Dict[Any, Any], Dict[Any, Any], Dict[str, Any], List[str]]] = {}
    for bt, bmap in chain_by_bar.items():
        c_lk = {k: v[0] for k, v in bmap.items()}
        q_lk = {k: v[1] for k, v in bmap.items()}
        q_by_i = {v[0]["instrument_key"]: v[1] for v in bmap.values()}
        exps = sorted({k[0] for k in bmap.keys()})
        bar_packs[bt] = (c_lk, q_lk, q_by_i, exps)

    # Prior completed daily NIFTY closes strictly < session_date from continuous_index_ohlcv
    prior_daily_nifty: Dict[str, List[float]] = {}
    daily_closes: List[Tuple[str, float]] = []
    for r in v1_ro_conn.execute(
        """
        SELECT substr(timestamp,1,10) as dt, MAX(timestamp) as max_ts
        FROM continuous_index_ohlcv
        WHERE ticker='NIFTY' AND dt != '2026-11-08'
        GROUP BY dt ORDER BY dt ASC;
        """
    ).fetchall():
        c_row = v1_ro_conn.execute(
            "SELECT close FROM continuous_index_ohlcv WHERE ticker='NIFTY' AND timestamp=? LIMIT 1;",
            (r["max_ts"],),
        ).fetchone()
        if c_row:
            daily_closes.append((r["dt"], float(c_row[0])))

    for dt in sorted(snaps_by_date.keys()):
        prior_daily_nifty[dt] = [c for d, c in daily_closes if d < dt]

    # Observed FSP windows for settlement fallback checks
    fsp_by_expiry: Dict[str, float] = {}
    for r in v1_ro_conn.execute("SELECT expiry_date, spot_1500_1530_twap FROM option_sniffer_advance_s205_expiry_fsp_windows;").fetchall():
        if r["spot_1500_1530_twap"]:
            fsp_by_expiry[r["expiry_date"]] = float(r["spot_1500_1530_twap"])

    return {
        "dates": sorted(snaps_by_date.keys()),
        "snaps_by_date": snaps_by_date,
        "chain_by_bar": chain_by_bar,
        "bar_packs": bar_packs,
        "prior_daily_nifty": prior_daily_nifty,
        "fsp_by_expiry": fsp_by_expiry,
    }


def _run_single_replay(
    dataset: Dict[str, Any],
    cfg: Dict[str, Any],
    cfg_hash: str,
    store: V2LedgerStore,
    run_mode: str,
    book_b_params: Dict[str, Any],
    enable_book_a_opt_in: bool = False,
    prior_vix_override_by_date: Optional[Dict[str, List[float]]] = None,
    cost_multiplier: float = 1.0,
    extra_adverse_slippage_ticks: int = 0,
) -> Dict[str, Any]:
    store.clear_run_mode(run_mode, cfg_hash)
    sim = V2PaperSimulator(
        config=cfg,
        config_hash=cfg_hash,
        store=store,
        run_mode=run_mode,
        cost_multiplier=cost_multiplier,
        extra_adverse_slippage_ticks=extra_adverse_slippage_ticks,
    )

    daily_records: List[Dict[str, Any]] = []
    vix_eod_history: List[float] = []
    signals_summary: Dict[str, int] = {}

    for dt in dataset["dates"]:
        bars = dataset["snaps_by_date"][dt]
        sod_eq = sim.realized_equity_inr + sum(float(p.get("last_mark_net_pnl_inr") or 0.0) for p in sim.open_packages.values())
        sim.intraday_min_equity_by_date[dt] = sod_eq
        sim.peak_reserved_by_date[dt] = sim.reserved_capital_inr
        trades_opened_today = 0

        # Strictly causal intraday accumulators reset at 09:15
        cum_spot = 0.0
        cnt_spot = 0
        orb_hi: Optional[float] = None
        orb_lo: Optional[float] = None
        orb_complete = False
        open_basis_ref: Optional[Dict[str, Any]] = None
        last_vix: Optional[float] = None

        prior_vix = list((prior_vix_override_by_date or {}).get(dt, vix_eod_history))
        prior_spots = list(dataset["prior_daily_nifty"].get(dt, []))

        for idx, b in enumerate(bars):
            bt = b["bar_ts"]
            hm = bt[11:16]
            hhmm = int(hm[:2]) * 100 + int(hm[3:5])
            now_ms = _bar_ts_ms(bt, 0)
            spot_c = b["spot_ltp"]
            fut_c = b["fut_ltp"]
            if b["india_vix"] is not None and b["india_vix"] > 0:
                last_vix = b["india_vix"]

            if spot_c > 0:
                cum_spot += spot_c
                cnt_spot += 1
            spot_vwap = (cum_spot / cnt_spot) if cnt_spot > 0 else spot_c

            # Strictly causal ORB30: accumulates highs/lows over 09:15..09:44, becomes usable at >= 09:45
            if hhmm < 945 and spot_c > 0:
                orb_hi = spot_c if orb_hi is None else max(orb_hi, spot_c)
                orb_lo = spot_c if orb_lo is None else min(orb_lo, spot_c)
            elif hhmm >= 945 and orb_hi is not None and orb_lo is not None:
                orb_complete = True

            spot_q = {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "spot_ltp": spot_c,
                "local_receipt_ts_ms": now_ms,
                "timestamp_ms": now_ms,
                "quote_age_ms": 0,
            }
            fut_q = {
                "instrument_key": f"NSE_FUT|NIFTY|{dt[:7]}",
                "fut_ltp": fut_c,
                "local_receipt_ts_ms": now_ms,
                "timestamp_ms": now_ms,
                "quote_age_ms": 0,
            }
            if open_basis_ref is None and 915 <= hhmm <= 930:
                ref_chk = check_spot_futures_sync(spot_q, fut_q, max_skew_ms=5000, max_age_ms=5000, now_ms=now_ms)
                if ref_chk.get("ok"):
                    open_basis_ref = ref_chk

            contracts_lk, quotes_lk, quote_by_ikey, expiries = dataset["bar_packs"].get(bt, ({}, {}, {}, []))

            # Step 1: Mark & Manage existing open packages FIRST
            mtm_info = sim.mark_open_packages(
                quote_by_ikey=quote_by_ikey,
                session_date=dt,
                bar_ts=bt,
                now_ms=now_ms,
                is_opening_bar=(idx == 0),
                is_eod_bar=(idx == len(bars) - 1),
            )
            kill_lim = float(cfg["account_envelope"]["max_daily_mtm_kill_inr"])
            slots = sim.active_slots_and_reserves(bt, dt)
            if (slots["realized_today_inr"] + mtm_info["conservative_unrealized_mtm_inr"]) <= kill_lim:
                sim.daily_kill_triggered_by_date[dt] = f"DAILY_KILL_MTM_LE_{kill_lim}"

            for pid, pkg in list(sim.open_packages.items()):
                if pkg["book_id"] == "V2_BOOK_B_PAID_FLOW":
                    manage_open_book_b_on_bar(
                        sim=sim,
                        pkg=pkg,
                        session_date=dt,
                        bar_ts=bt,
                        ist_hhmm=hhmm,
                        decision_ts_ms=now_ms,
                        spot_close=spot_c,
                        spot_vwap=spot_vwap,
                        quote_by_ikey=quote_by_ikey,
                        stop_pct_override=book_b_params.get("stop_pct_override"),
                        stop_max_inr_override=book_b_params.get("stop_max_inr_override"),
                        stagnation_mins_override=book_b_params.get("stagnation_mins_override"),
                        stagnation_min_gain_override=book_b_params.get("stagnation_min_gain_override"),
                    )
                else:
                    manage_open_book_a_on_bar(
                        sim=sim,
                        pkg=pkg,
                        session_date=dt,
                        bar_ts=bt,
                        ist_hhmm=hhmm,
                        decision_ts_ms=now_ms,
                        spot_close=spot_c,
                        quote_by_ikey=quote_by_ikey,
                        fsp_twap_spot=dataset["fsp_by_expiry"].get(dt),
                    )

            # Step 2: Check Book B / Book A new entries strictly after close & slot-causality check
            slots = sim.active_slots_and_reserves(bt, dt)
            env = cfg["account_envelope"]
            if (
                dt not in sim.daily_kill_triggered_by_date
                and mtm_info["unpriced_count"] == 0
                and slots["open_total"] < int(env["max_open_packages_total"])
                and slots["trades_today_total"] < int(env["max_total_trades_per_day"])
            ):
                # 2a. Book B check
                cfg_b = cfg["books"]["V2_BOOK_B_PAID_FLOW"]
                if (
                    slots["open_by_book"].get(cfg_b["book_id"], 0) < int(cfg_b["max_open"])
                    and slots["trades_today_by_book"].get(cfg_b["book_id"], 0) < int(cfg_b["max_trades_day"])
                    and now_ms >= sim.cooldown_until_ms.get(cfg_b["book_id"], 0)
                ):
                    sig_b = evaluate_book_b_entry_signal(
                        cfg=cfg,
                        session_date=dt,
                        bar_ts=bt,
                        ist_hhmm=hhmm,
                        decision_ts_ms=now_ms,
                        spot_close=spot_c,
                        spot_vwap=spot_vwap,
                        orb30_h=orb_hi if orb_complete else None,
                        orb30_l=orb_lo if orb_complete else None,
                        adx14=b["adx14"],
                        er20=b["er20"],
                        spot_quote=spot_q,
                        fut_quote=fut_q,
                        open_basis_ref=open_basis_ref,
                        expiries=expiries,
                        contracts_lookup=contracts_lk,
                        quote_lookup=quotes_lk,
                        variant_code=book_b_params["variant_code"],
                        use_basis_delta=book_b_params["use_basis_delta"],
                        use_weekly_syn_basis=book_b_params.get("use_weekly_syn_basis", False),
                        window_start_override=book_b_params.get("window_start"),
                        window_end_override=book_b_params.get("window_end"),
                        allow_single_otm_fallback=book_b_params.get("allow_otm_fallback"),
                        min_er_override=book_b_params.get("min_er_override"),
                    )
                    if sig_b["passed"] and sig_b.get("contract") and sig_b.get("quote"):
                        sid = make_stable_id("osv2_sig", run_mode, book_b_params["variant_code"], bt, sig_b["contract"]["instrument_key"])
                        store.record_signal_idempotent(
                            {
                                "signal_id": sid,
                                "run_mode": run_mode,
                                "config_hash": cfg_hash,
                                "session_date": dt,
                                "bar_ts": bt,
                                "decision_ts_ms": now_ms,
                                "book_id": cfg_b["book_id"],
                                "candidate_variant": book_b_params["variant_code"],
                                "direction": sig_b["option_type"],
                                "status": "TRIGGERED",
                                "reject_reason": None,
                                "gates": sig_b["gates"],
                                "provenance": sig_b["basis_meta"],
                            }
                        )
                        res_open = sim.open_book_b_paid_long(
                            signal_id=sid,
                            candidate_variant=book_b_params["variant_code"],
                            session_date=dt,
                            bar_ts=bt,
                            decision_ts_ms=now_ms,
                            contract=sig_b["contract"],
                            quote=sig_b["quote"],
                            spot_price=spot_c,
                            basis_meta=sig_b["basis_meta"],
                            used_single_otm_fallback=sig_b["used_single_otm_fallback"],
                        )
                        if res_open.get("ok"):
                            trades_opened_today += 1
                    elif sig_b.get("reject_reason"):
                        r_k = str(sig_b["reject_reason"]).split(":")[0]
                        signals_summary[r_k] = signals_summary.get(r_k, 0) + 1

                # 2b. Book A check (strictly disabled by default unless allow_unverified_in_replay=True!)
                cfg_a = cfg["books"]["V2_BOOK_A_PROPOSED_O3"]
                slots = sim.active_slots_and_reserves(bt, dt)
                if (
                    slots["open_by_book"].get(cfg_a["book_id"], 0) < int(cfg_a["max_open"])
                    and now_ms >= sim.cooldown_until_ms.get(cfg_a["book_id"], 0)
                ):
                    sig_a = evaluate_book_a_entry_signal(
                        cfg=cfg,
                        session_date=dt,
                        bar_ts=bt,
                        ist_hhmm=hhmm,
                        decision_ts_ms=now_ms,
                        spot_close=spot_c,
                        spot_vwap=spot_vwap,
                        vix_now=last_vix,
                        prior_vix_closes=prior_vix,
                        prior_spot_closes=prior_spots,
                        expiries=expiries,
                        contracts_lookup=contracts_lk,
                        quote_lookup=quotes_lk,
                        allow_unverified_in_replay=enable_book_a_opt_in,
                    )
                    if sig_a["passed"]:
                        sid_a = make_stable_id("osv2_sig", run_mode, sig_a["candidate_variant"], bt, sig_a["short_contract"]["instrument_key"])
                        store.record_signal_idempotent(
                            {
                                "signal_id": sid_a,
                                "run_mode": run_mode,
                                "config_hash": cfg_hash,
                                "session_date": dt,
                                "bar_ts": bt,
                                "decision_ts_ms": now_ms,
                                "book_id": cfg_a["book_id"],
                                "candidate_variant": sig_a["candidate_variant"],
                                "direction": sig_a["option_type"],
                                "status": "TRIGGERED",
                                "reject_reason": None,
                                "gates": sig_a["gates"],
                                "provenance": {"expiry": sig_a["expiry"]},
                            }
                        )
                        l1 = sim.open_book_a_leg1_hedge(
                            signal_id=sid_a,
                            candidate_variant=sig_a["candidate_variant"],
                            session_date=dt,
                            bar_ts=bt,
                            decision_ts_ms=now_ms,
                            wing_contract=sig_a["wing_contract"],
                            short_contract=sig_a["short_contract"],
                            wing_quote=sig_a["wing_quote"],
                            spot_price=spot_c,
                            vix_val=last_vix,
                            is_bull=sig_a["is_bull"],
                        )
                        if l1.get("ok"):
                            sim.complete_or_unwind_pending_leg2(
                                package_id=l1["package_id"],
                                short_quote=sig_a["short_quote"],
                                wing_quote=sig_a["wing_quote"],
                                decision_ts_ms=now_ms + 250,
                                session_date=dt,
                                bar_ts=bt,
                            )

        if last_vix is not None and last_vix > 0:
            vix_eod_history.append(last_vix)

        eod_mtm = round(
            sim.realized_equity_inr + sum(float(p.get("last_mark_net_pnl_inr") or 0.0) for p in sim.open_packages.values()),
            2,
        )
        all_pkgs_now = store.load_packages(run_mode, cfg_hash)
        closed_dt = [p for p in all_pkgs_now if p["state"] not in OPEN_EXPOSURE_STATES and (p.get("exit_bar_ts") or "")[:10] == dt]
        day_net = round(sum(float(p["net_pnl_inr"]) for p in closed_dt), 2)
        d_row = {
            "run_mode": run_mode,
            "config_hash": cfg_hash,
            "session_date": dt,
            "sod_equity_inr": round(sod_eq, 2),
            "eod_realized_equity_inr": round(sim.realized_equity_inr, 2),
            "eod_mtm_equity_inr": eod_mtm,
            "intraday_min_mtm_equity_inr": round(sim.intraday_min_equity_by_date.get(dt, eod_mtm), 2),
            "peak_reserved_capital_inr": round(sim.peak_reserved_by_date.get(dt, sim.reserved_capital_inr), 2),
            "closed_trades": len(closed_dt),
            "open_overnight_packages": sum(1 for p in sim.open_packages.values()),
            "unresolved_exposure_count": sum(1 for p in sim.open_packages.values() if p.get("is_unpriced_exposure")),
            "net_pnl_inr": day_net,
        }
        store.upsert_daily_snapshot(d_row)
        daily_records.append(d_row)

    all_pkgs = store.load_packages(run_mode, cfg_hash)
    closed = [p for p in all_pkgs if p["state"] not in OPEN_EXPOSURE_STATES]
    stats = _compute_series_stats(closed, daily_records, sim.starting_capital_inr)
    digest = store.compute_canonical_ledger_digest(run_mode, cfg_hash)
    return {
        "variant_code": book_b_params["variant_code"],
        "run_mode": run_mode,
        "config_hash": cfg_hash,
        "ledger_sha256": digest,
        "stats": stats,
        "daily_records": daily_records,
        "closed_trades": closed,
        "open_packages": list(sim.open_packages.values()),
        "reject_reason_counts": signals_summary,
    }


def run_v2_replay_and_attribution(
    v1_ro_conn: sqlite3.Connection,
    store: V2LedgerStore,
    cfg: Dict[str, Any],
    cfg_hash: str,
) -> Dict[str, Any]:
    """
    Executes:
      1. Canonical V2 baseline replay (`run_mode='REPLAY'`) persisted to V2 DB.
      2. Isolated 6-step Book B single-factor ablation + weekly synthetic research variant.
      3. 2x charges, 2x slippage, Ex-Best-Session, and Opt-In Book A stress checks.
      4. Clean Daily V1 vs V2 comparison table.
    """
    dataset = _load_archived_l5_dataset(v1_ro_conn)

    # 1. Canonical V2 Replay (persisted under run_mode = 'REPLAY')
    v2_frozen_params = {
        "variant_code": "V2_FROZEN_COMBINED",
        "use_basis_delta": True,
        "window_start": 1015,
        "window_end": 1300,
        "allow_otm_fallback": True,
        "min_er_override": 0.32,
    }
    canonical = _run_single_replay(
        dataset=dataset,
        cfg=cfg,
        cfg_hash=cfg_hash,
        store=store,
        run_mode="REPLAY",
        book_b_params=v2_frozen_params,
        enable_book_a_opt_in=False,
        cost_multiplier=1.0,
        extra_adverse_slippage_ticks=0,
    )

    # 2. Single-Factor Book B Attribution (isolated temporary run_modes starting with 'ABLATION_')
    ablation_specs = [
        {
            "variant_code": "B0_V1_BASELINE",
            "label": "B0: V1 Book B Baseline (09:45-12:45, raw|B|>=5, ER>=0.28, SL 22%/Rs2200, ATM only)",
            "use_basis_delta": False,
            "window_start": 945,
            "window_end": 1245,
            "allow_otm_fallback": False,
            "min_er_override": 0.28,
            "stop_pct_override": 22.0,
            "stop_max_inr_override": 2200.0,
            "stagnation_mins_override": 45,
            "stagnation_min_gain_override": 3.0,
        },
        {
            "variant_code": "B1_BASIS_DELTA_ONLY",
            "label": "B1: Basis Delta Only (DeltaBasis >= +-3pt vs B0)",
            "use_basis_delta": True,
            "window_start": 945,
            "window_end": 1245,
            "allow_otm_fallback": False,
            "min_er_override": 0.28,
            "stop_pct_override": 22.0,
            "stop_max_inr_override": 2200.0,
            "stagnation_mins_override": 45,
            "stagnation_min_gain_override": 3.0,
        },
        {
            "variant_code": "B2_WINDOW_1015_1300_ONLY",
            "label": "B2: Window Only (10:15-13:00 IST vs B0)",
            "use_basis_delta": False,
            "window_start": 1015,
            "window_end": 1300,
            "allow_otm_fallback": False,
            "min_er_override": 0.28,
            "stop_pct_override": 22.0,
            "stop_max_inr_override": 2200.0,
            "stagnation_mins_override": 45,
            "stagnation_min_gain_override": 3.0,
        },
        {
            "variant_code": "B3_STOP_20PCT_1800_ONLY",
            "label": "B3: Stop Only (min(20%, Rs1800) vs B0)",
            "use_basis_delta": False,
            "window_start": 945,
            "window_end": 1245,
            "allow_otm_fallback": False,
            "min_er_override": 0.28,
        },
        {
            "variant_code": "B4_1OTM_FALLBACK_ONLY",
            "label": "B4: 1-OTM Fallback Only (+-50pt on CashCap vs B0)",
            "use_basis_delta": False,
            "window_start": 945,
            "window_end": 1245,
            "allow_otm_fallback": True,
            "min_er_override": 0.28,
            "stop_pct_override": 22.0,
            "stop_max_inr_override": 2200.0,
            "stagnation_mins_override": 45,
            "stagnation_min_gain_override": 3.0,
        },
        {
            **v2_frozen_params,
            "variant_code": "B5_V2_COMBINED",
            "label": "B5: V2 Combined Frozen Book B (DeltaBasis + 10:15-13:00 + 20%/Rs1800 + 1-OTM)",
        },
        {
            **v2_frozen_params,
            "variant_code": "B6_RESEARCH_WEEKLY_SYN_BASIS",
            "label": "B6: Research Variant (Weekly Synthetic Basis >= +-3pt)",
            "use_basis_delta": False,
            "use_weekly_syn_basis": True,
        },
    ]

    ablation_rows: List[Dict[str, Any]] = []
    b0_net = 0.0
    for spec in ablation_specs:
        r = _run_single_replay(
            dataset=dataset,
            cfg=cfg,
            cfg_hash=cfg_hash,
            store=store,
            run_mode=f"ABLATION_{spec['variant_code']}",
            book_b_params=spec,
        )
        if spec["variant_code"] == "B0_V1_BASELINE":
            b0_net = r["stats"]["net_pnl_inr"]
        ablation_rows.append(
            {
                "variant_code": spec["variant_code"],
                "label": spec["label"],
                "stats": r["stats"],
                "incremental_net_vs_b0_inr": round(r["stats"]["net_pnl_inr"] - b0_net, 2),
                "trades": [
                    {
                        "session_date": t["session_date"],
                        "symbol": t["legs_state"]["long_leg"]["trading_symbol"],
                        "entry_bar_ts": t["entry_bar_ts"],
                        "exit_bar_ts": t["exit_bar_ts"],
                        "exit_reason": t["exit_reason"],
                        "planned_loss_inr": t["planned_loss_inr"],
                        "actual_gross_fill_loss_inr": t["actual_gross_fill_loss_inr"],
                        "slippage_and_gap_overshoot_inr": t["slippage_and_gap_overshoot_inr"],
                        "total_friction_inr": t["total_friction_inr"],
                        "net_pnl_inr": t["net_pnl_inr"],
                        "used_single_otm_fallback": t["legs_state"].get("used_single_otm_fallback", False),
                    }
                    for t in r["closed_trades"]
                ],
            }
        )

    # 3. Stress & Sensitivity Suite (2x charges, 2x slippage, Ex-Best-Session, Opt-In Book A fail-closed vs seeded VIX)
    stress_2x_charges = _run_single_replay(
        dataset, cfg, cfg_hash, store, "STRESS_2X_CHARGES", v2_frozen_params, cost_multiplier=2.0
    )
    stress_2x_slip = _run_single_replay(
        dataset, cfg, cfg_hash, store, "STRESS_2X_SLIPPAGE", v2_frozen_params, extra_adverse_slippage_ticks=1
    )
    opt_in_book_a_strict = _run_single_replay(
        dataset, cfg, cfg_hash, store, "STRESS_BOOK_A_STRICT_NO_VIX", v2_frozen_params, enable_book_a_opt_in=True
    )

    # Ex-best-session sensitivity on canonical V2 replay
    by_day_net: Dict[str, float] = {}
    for t in canonical["closed_trades"]:
        by_day_net[t["session_date"]] = by_day_net.get(t["session_date"], 0.0) + float(t["net_pnl_inr"])
    best_day = max(by_day_net.items(), key=lambda kv: kv[1])[0] if by_day_net else None
    ex_best_trades = [t for t in canonical["closed_trades"] if t["session_date"] != best_day] if best_day else []
    ex_best_net = round(sum(float(t["net_pnl_inr"]) for t in ex_best_trades), 2)

    # 4. Build clean V1 vs V2 Daily Comparison across dataset dates (read-only from V1 SQLite)
    v1_rows = v1_ro_conn.execute(
        """
        SELECT package_id, session_date, book_id, net_pnl_inr, total_charges_inr,
               cash_reserved_inr, exit_reason, entry_bar_ts, exit_bar_ts
        FROM option_sniffer_advance_s205_shadow_packages
        ORDER BY entry_bar_ts ASC;
        """
    ).fetchall()
    v1_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for vr in v1_rows:
        v1_by_date.setdefault(vr["session_date"], []).append(dict(vr))

    v2_by_date_trades: Dict[str, List[Dict[str, Any]]] = {}
    for vt in canonical["closed_trades"]:
        v2_by_date_trades.setdefault(vt["session_date"], []).append(vt)

    v1_daily_eq = 200000.0
    v1_peak = 200000.0
    daily_comparison: List[Dict[str, Any]] = []
    for drec in canonical["daily_records"]:
        dt = drec["session_date"]
        v1_t = v1_by_date.get(dt, [])
        v1_net_d = round(sum(float(x["net_pnl_inr"]) for x in v1_t), 2)
        v1_cap_d = round(sum(float(x["cash_reserved_inr"]) for x in v1_t), 2)
        v1_daily_eq = round(v1_daily_eq + v1_net_d, 2)
        v1_peak = max(v1_peak, v1_daily_eq)
        v1_dd_d = round(v1_peak - v1_daily_eq, 2)
        v1_stale_cnt = sum(1 for x in v1_t if x["book_id"] == "SHADOW_BOOK_A_SPREAD_150" and x["exit_reason"] == "SPREAD_DECAY_TARGET_70PCT")

        v2_t = v2_by_date_trades.get(dt, [])
        daily_comparison.append(
            {
                "session_date": dt,
                "v1_reported_net_pnl_inr": v1_net_d,
                "v1_reported_trades": len(v1_t),
                "v1_stale_wing_contaminated_count": v1_stale_cnt,
                "v1_capital_reserved_inr": v1_cap_d,
                "v1_closed_dd_inr": v1_dd_d,
                "v2_replay_net_pnl_inr": drec["net_pnl_inr"],
                "v2_replay_trades": drec["closed_trades"],
                "v2_replay_peak_capital_inr": drec["peak_reserved_capital_inr"],
                "v2_replay_intraday_min_eq_inr": drec["intraday_min_mtm_equity_inr"],
                "result_origin": "ARCHIVE_REPLAY",
            }
        )

    v1_all_stats = _compute_series_stats(
        [{"gross_pnl_inr": r["net_pnl_inr"] + r["total_charges_inr"], "total_friction_inr": r["total_charges_inr"], "net_pnl_inr": r["net_pnl_inr"]} for r in v1_by_date_flat(v1_rows)],
        [
            {"eod_mtm_equity_inr": 200000.0 + sum(float(x["net_pnl_inr"]) for d_k in dataset["dates"][: i + 1] for x in v1_by_date.get(d_k, [])),
             "intraday_min_mtm_equity_inr": 200000.0 + sum(float(x["net_pnl_inr"]) for d_k in dataset["dates"][: i + 1] for x in v1_by_date.get(d_k, [])),
             "peak_reserved_capital_inr": sum(float(x["cash_reserved_inr"]) for x in v1_by_date.get(dt, []))}
            for i, dt in enumerate(dataset["dates"])
        ],
        200000.0,
    )

    return {
        "canonical_replay": canonical,
        "ablation_study": ablation_rows,
        "sensitivity": {
            "baseline_1x": canonical["stats"],
            "stress_2x_charges": stress_2x_charges["stats"],
            "stress_2x_slippage": stress_2x_slip["stats"],
            "ex_best_session": {"excluded_session": best_day, "remaining_trades": len(ex_best_trades), "net_pnl_inr": ex_best_net},
            "book_a_opt_in_strict_no_prior_vix": {
                "book_a_trades": sum(1 for t in opt_in_book_a_strict["closed_trades"] if t["book_id"] == "V2_BOOK_A_PROPOSED_O3"),
                "reason": "Fail-closed: 0 prior completed VIX closes exist in market_data.db prior to 2026-11-16 (<10 required for SMA10).",
                "stats": opt_in_book_a_strict["stats"],
            },
        },
        "v1_reported_summary": v1_all_stats,
        "daily_comparison": daily_comparison,
    }


def v1_by_date_flat(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]
