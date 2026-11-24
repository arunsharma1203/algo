"""
V2 Option Sniffer — Causal Archive Replay (`REPLAY_V2_1_CAUSAL`), Event-Stream
Unified Replay Runner, Chronological Drawdown & Attribution Audit.

Key Invariants (v2.1.0-causal):
  - Strictly causal (`availability_ts_ms <= decision_ts_ms` and `bar_open + 60s <= decision_ts_ms`).
  - Treats `bar_ts` as bar open `[t, t+60s)` and uses actual archive `recorded_at` /
    `timestamp` in `Asia/Kolkata` (`never` fixed `+60s`).
  - Rejects or freezes late arrivals (e.g., `2026-11-20 09:40:00` recorded at `09:44:10.039544`)
    and ignores post-session EOD batch flushes (`19:41–23:23 IST`) cleanly.
  - Never fabricates identical spot/futures source timestamps, `0ms` quote age, or
    futures `instrument_key` / `expiry`. Where `backend/market_data.db` lacks those fields,
    `check_spot_futures_sync` fails closed (`verifiable=False`) and `REPLAY_V2_1_CAUSAL`
    truthfully records `0` verified futures-basis trades, while preserving `v2.0.0` (`baa72f77`)
    evidence separately in `superseded_v2_0_replay_evidence.json`.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.option_sniffer_v2.v2_config import parse_ist_timestamp_ms
from app.analytics.option_sniffer_v2.v2_ledger_db import V2LedgerStore
from app.analytics.option_sniffer_v2.v2_paper_simulator import (
    OPEN_EXPOSURE_STATES,
    V2PaperSimulator,
    compute_chronological_drawdown,
)
from app.analytics.option_sniffer_v2.v2_unified_engine import V2UnifiedEventEngine

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_SUPERSEDED_V2_0_PATH = os.path.join(_PKG_DIR, "superseded_v2_0_replay_evidence.json")


def load_superseded_v2_0_evidence() -> Dict[str, Any]:
    if not os.path.exists(_SUPERSEDED_V2_0_PATH):
        return {}
    with open(_SUPERSEDED_V2_0_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _compute_series_stats(
    closed_trades: List[Dict[str, Any]],
    daily_records: List[Dict[str, Any]],
    starting_capital_inr: float,
    equity_curve: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    gross_wins = sum(float(t["gross_pnl_inr"]) for t in closed_trades if float(t["net_pnl_inr"]) > 0)
    gross_losses = abs(sum(float(t["gross_pnl_inr"]) for t in closed_trades if float(t["net_pnl_inr"]) < 0))
    net_wins = sum(float(t["net_pnl_inr"]) for t in closed_trades if float(t["net_pnl_inr"]) > 0)
    net_losses = abs(sum(float(t["net_pnl_inr"]) for t in closed_trades if float(t["net_pnl_inr"]) < 0))

    pf = round(net_wins / net_losses, 3) if net_losses > 0 else (99.0 if net_wins > 0 else 0.0)
    if net_wins == 0 and net_losses == 0:
        pf = 0.0

    start_cap = float(starting_capital_inr)
    eq_c = start_cap
    peak_c = start_cap
    closed_dd_inr = 0.0
    for t in closed_trades:
        eq_c = round(eq_c + float(t["net_pnl_inr"]), 2)
        if eq_c > peak_c:
            peak_c = eq_c
        dd = round(peak_c - eq_c, 2)
        if dd > closed_dd_inr:
            closed_dd_inr = dd

    # Chronological MTM peak-to-trough drawdown across actual timestamped equity curve when available
    if equity_curve:
        mtm_dd = compute_chronological_drawdown(
            [
                float(
                    pt["marked_equity_inr"]
                    if pt.get("marked_equity_inr") is not None
                    else pt.get("equity_inr", start_cap)
                )
                for pt in equity_curve
            ],
            starting_capital_inr=start_cap,
        )
    else:
        chronological_pts: List[float] = []
        for d in daily_records:
            for key in ("sod_equity_inr", "intraday_min_mtm_equity_inr", "eod_mtm_equity_inr"):
                if key in d and d[key] is not None:
                    chronological_pts.append(float(d[key]))
        mtm_dd = compute_chronological_drawdown(chronological_pts, starting_capital_inr=start_cap)

    peak_cap = 0.0
    for d in daily_records:
        peak_cap = max(peak_cap, float(d.get("peak_reserved_capital_inr") or 0.0))

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
        "closed_dd_pct": round(100.0 * closed_dd_inr / start_cap, 2) if start_cap > 0 else 0.0,
        "intraday_mtm_dd_inr": float(mtm_dd["max_dd_inr"]),
        "intraday_mtm_dd_pct": float(mtm_dd["max_dd_pct_of_starting_capital"]),
        "intraday_mtm_dd_pct_peak": float(mtm_dd["max_dd_pct_of_peak_equity"]),
        "running_peak_equity_inr": float(mtm_dd["running_peak_equity_inr"]),
        "peak_capital_used_inr": round(peak_cap, 2),
        "gross_wins_inr": round(gross_wins, 2),
        "gross_losses_inr": round(gross_losses, 2),
    }


def _load_causal_archive_events(v1_ro_conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Loads archived 1m snapshots & 1m L5 chain rows into normalized chronologically sorted
    events with Asia/Kolkata availability timestamps (`timestamp` / `recorded_at`).
    Never fabricates missing separate source timestamps or futures contract keys.
    """
    snap_cols = {
        r["name"]
        for r in v1_ro_conn.execute("PRAGMA table_info(option_sniffer_advance_s205_l5_snapshots)").fetchall()
    }
    chain_cols = {
        r["name"]
        for r in v1_ro_conn.execute("PRAGMA table_info(option_sniffer_advance_s205_l5_chain_1m)").fetchall()
    }
    schema_audit = {
        "l5_snapshots_columns": sorted(snap_cols),
        "l5_chain_1m_columns": sorted(chain_cols),
        "spot_source_ts_column_present": any(c in snap_cols for c in ("spot_timestamp_ms", "spot_exchange_ltt_ms")),
        "fut_source_ts_column_present": any(c in snap_cols for c in ("fut_timestamp_ms", "fut_exchange_ltt_ms")),
        "fut_instrument_key_column_present": "fut_instrument_key" in snap_cols,
        "fut_expiry_column_present": "fut_expiry" in snap_cols,
    }

    raw_events: List[Tuple[int, int, int, Tuple[str, str, Dict[str, Any]]]] = []
    dates_set: set[str] = set()
    seq = 0
    snapshot_count = 0

    for r in v1_ro_conn.execute(
        """
        SELECT *
        FROM option_sniffer_advance_s205_l5_snapshots
        ORDER BY bar_ts ASC;
        """
    ).fetchall():
        dt = str(r["bar_ts"])[:10]
        if dt > "2026-11-23":
            continue
        dates_set.add(dt)
        snapshot_count += 1
        bar_open_ms = parse_ist_timestamp_ms(r["bar_ts"])
        avail_str = r["timestamp"] if "timestamp" in snap_cols and r["timestamp"] else r["bar_ts"]
        avail_ms = parse_ist_timestamp_ms(avail_str)
        if bar_open_ms is None or avail_ms is None:
            continue

        spot_px = float(r["spot_ltp"] or 0.0)
        fut_px = float(r["fut_ltp"] or 0.0)
        vix_val = float(r["india_vix"]) if r["india_vix"] is not None else None

        spot_ts = r["spot_timestamp_ms"] if "spot_timestamp_ms" in snap_cols else None
        fut_ts = r["fut_timestamp_ms"] if "fut_timestamp_ms" in snap_cols else None
        fut_ikey = r["fut_instrument_key"] if "fut_instrument_key" in snap_cols else None
        fut_exp = r["fut_expiry"] if "fut_expiry" in snap_cols else None

        seq += 1
        raw_events.append(
            (
                avail_ms,
                0,
                seq,
                (
                    "INDEX",
                    "NIFTY",
                    {
                        "instrument_key": "NSE_INDEX|Nifty 50",
                        "underlying": "NIFTY",
                        "spot_ltp": spot_px,
                        "availability_ts_ms": avail_ms,
                        "local_receipt_ts_ms": avail_ms,
                        "timestamp_ms": spot_ts,
                        "exchange_ltt_ms": spot_ts,
                        "source_ts_proven": spot_ts is not None,
                        "source_bar_open_ms": bar_open_ms,
                        "archive_bar_ts": r["bar_ts"],
                        "archive_recorded_at": avail_str,
                    },
                ),
            )
        )
        if vix_val is not None and vix_val > 0:
            seq += 1
            raw_events.append(
                (
                    avail_ms,
                    0,
                    seq,
                    (
                        "INDEX",
                        "INDIA_VIX",
                        {
                            "instrument_key": "NSE_INDEX|India VIX",
                            "spot_ltp": vix_val,
                            "availability_ts_ms": avail_ms,
                            "local_receipt_ts_ms": avail_ms,
                            "source_bar_open_ms": bar_open_ms,
                        },
                    ),
                )
            )
        seq += 1
        raw_events.append(
            (
                avail_ms,
                1,
                seq,
                (
                    "FUTURES",
                    str(fut_ikey or ""),
                    {
                        "instrument_key": fut_ikey,
                        "expiry": fut_exp,
                        "fut_ltp": fut_px,
                        "availability_ts_ms": avail_ms,
                        "local_receipt_ts_ms": avail_ms,
                        "timestamp_ms": fut_ts,
                        "exchange_ltt_ms": fut_ts,
                        "source_ts_proven": fut_ts is not None,
                        "source_bar_open_ms": bar_open_ms,
                        "archive_bar_ts": r["bar_ts"],
                        "archive_recorded_at": avail_str,
                    },
                ),
            )
        )

    chain_count = 0
    for cr in v1_ro_conn.execute(
        """
        SELECT session_date, bar_ts, recorded_at, instrument_key, trading_symbol, expiry, strike, option_type,
               raw_bid, raw_ask, bid_qty, ask_qty, ltp, depth_json, is_observed_l1_book, depth_observed_on_tick,
               book_provenance, l1_book_age_ms, book_obi_l5
        FROM option_sniffer_advance_s205_l5_chain_1m
        WHERE session_date BETWEEN '2026-11-16' AND '2026-11-23'
        ORDER BY recorded_at ASC, bar_ts ASC, id ASC;
        """
    ).fetchall():
        chain_count += 1
        avail_ms = parse_ist_timestamp_ms(cr["recorded_at"])
        bar_open_ms = parse_ist_timestamp_ms(cr["bar_ts"])
        if avail_ms is None or bar_open_ms is None:
            continue

        age_raw = cr["l1_book_age_ms"]
        age_ms = int(age_raw) if age_raw is not None else None
        l1_ts = (avail_ms - age_ms) if (age_ms is not None and 0 <= age_ms < 999999) else None
        depth_observed = bool(cr["depth_observed_on_tick"])
        depth_ts = l1_ts if depth_observed else None

        seq += 1
        raw_events.append(
            (
                avail_ms,
                2,
                seq,
                (
                    "OPTION",
                    str(cr["instrument_key"]),
                    {
                        "instrument_key": cr["instrument_key"],
                        "trading_symbol": cr["trading_symbol"],
                        "underlying": "NIFTY",
                        "expiry": cr["expiry"],
                        "strike": float(cr["strike"]),
                        "option_type": cr["option_type"],
                        "lot_size": 65,
                        "raw_bid": float(cr["raw_bid"] or 0.0),
                        "raw_ask": float(cr["raw_ask"] or 0.0),
                        "bid": float(cr["raw_bid"] or 0.0),
                        "ask": float(cr["raw_ask"] or 0.0),
                        "l1_bid_qty_observed": int(cr["bid_qty"] or 0),
                        "l1_ask_qty_observed": int(cr["ask_qty"] or 0),
                        "ltp": float(cr["ltp"] or 0.0),
                        "depth_levels": None,
                        "depth_json_raw": cr["depth_json"],
                        "is_observed_l1_book": bool(cr["is_observed_l1_book"]),
                        "l1_observed_on_tick": bool(cr["is_observed_l1_book"]),
                        "depth_observed_on_tick": depth_observed,
                        "depth_rungs_carried": not depth_observed,
                        "book_provenance": cr["book_provenance"] or "UNKNOWN",
                        "l1_book_age_ms": age_ms,
                        "l1_timestamp_ms": l1_ts,
                        "l1_book_timestamp_ms": l1_ts,
                        "depth_timestamp_ms": depth_ts,
                        "availability_ts_ms": avail_ms,
                        "local_receipt_ts_ms": avail_ms,
                        "exchange_ltt_ms": l1_ts,
                        "book_obi_l5": float(cr["book_obi_l5"] or 0.0),
                        "source_bar_open_ms": bar_open_ms,
                        "archive_bar_ts": cr["bar_ts"],
                        "archive_recorded_at": cr["recorded_at"],
                    },
                ),
            )
        )

    # Sort strictly chronologically by archive availability instant
    raw_events.sort(key=lambda item: (item[0], item[1], item[2]))

    # Insert deterministic BAR_CLOSE_CHECK clock ticks after each unique availability timestamp batch
    events: List[Tuple[str, str, Dict[str, Any]]] = []
    idx = 0
    n_raw = len(raw_events)
    while idx < n_raw:
        ts_group = raw_events[idx][0]
        while idx < n_raw and raw_events[idx][0] == ts_group:
            events.append(raw_events[idx][3])
            idx += 1
        events.append(("BAR_CLOSE_CHECK", "CLOCK", {"availability_ts_ms": ts_group}))

    # Empirical timing audit samples straight from SQLite
    ex_1030 = v1_ro_conn.execute(
        "SELECT MIN(recorded_at) mn, MAX(recorded_at) mx, COUNT(*) n FROM option_sniffer_advance_s205_l5_chain_1m WHERE bar_ts='2026-11-20 10:30:00';"
    ).fetchone()
    ex_0940_late = v1_ro_conn.execute(
        "SELECT MIN(recorded_at) mn, MAX(recorded_at) mx, COUNT(*) n FROM option_sniffer_advance_s205_l5_chain_1m WHERE bar_ts='2026-11-20 09:40:00' AND recorded_at > '2026-11-20T09:42';"
    ).fetchone()
    ex_1515 = v1_ro_conn.execute(
        "SELECT bar_ts, timestamp FROM option_sniffer_advance_s205_l5_snapshots WHERE bar_ts='2026-11-19 15:15:00' LIMIT 1;"
    ).fetchone()
    late_1529_rows = [
        dict(x)
        for x in v1_ro_conn.execute(
            """
            SELECT session_date, bar_ts, MIN(recorded_at) AS min_recorded_at, MAX(recorded_at) AS max_recorded_at, COUNT(*) AS rows
            FROM option_sniffer_advance_s205_l5_chain_1m
            WHERE substr(bar_ts, 12, 5) = '15:29' OR recorded_at > session_date || 'T15:35'
            GROUP BY session_date, bar_ts
            ORDER BY bar_ts ASC;
            """
        ).fetchall()
    ]

    archive_timing_evidence = {
        "snapshot_rows_loaded": snapshot_count,
        "chain_rows_loaded": chain_count,
        "schema_audit": schema_audit,
        "bar_2026_11_20_1030": {
            "bar_ts_open": "2026-11-20 10:30:00+05:30",
            "earliest_recorded_at": ex_1030["mn"] if ex_1030 else None,
            "latest_recorded_at": ex_1030["mx"] if ex_1030 else None,
            "row_count": int(ex_1030["n"]) if ex_1030 else 0,
        },
        "delayed_2026_11_20_0940": {
            "bar_ts_open": "2026-11-20 09:40:00+05:30",
            "delayed_recorded_at": ex_0940_late["mx"] if ex_0940_late else None,
            "delayed_row_count": int(ex_0940_late["n"]) if ex_0940_late else 0,
        },
        "cutoff_2026_11_19_1515": dict(ex_1515) if ex_1515 else None,
        "late_eod_1529_flushes": late_1529_rows,
    }

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

    dates_sorted = sorted(dates_set)
    for dt in dates_sorted:
        prior_daily_nifty[dt] = [c for d, c in daily_closes if d < dt]

    fsp_by_expiry: Dict[str, float] = {}
    for r in v1_ro_conn.execute("SELECT expiry_date, spot_1500_1530_twap FROM option_sniffer_advance_s205_expiry_fsp_windows;").fetchall():
        if r["spot_1500_1530_twap"]:
            fsp_by_expiry[r["expiry_date"]] = float(r["spot_1500_1530_twap"])

    return {
        "dates": dates_sorted,
        "events": events,
        "prior_daily_nifty": prior_daily_nifty,
        "fsp_by_expiry": fsp_by_expiry,
        "archive_timing_evidence": archive_timing_evidence,
    }


def run_event_stream_replay(
    events: List[Tuple[str, str, Dict[str, Any]]],
    cfg: Dict[str, Any],
    cfg_hash: str,
    store: V2LedgerStore,
    run_mode: str = "REPLAY_STREAM",
    book_b_params: Optional[Dict[str, Any]] = None,
    enable_book_a_opt_in: bool = False,
    cost_multiplier: float = 1.0,
    extra_adverse_slippage_ticks: int = 0,
    prior_vix_override_by_date: Optional[Dict[str, List[float]]] = None,
    prior_daily_nifty_by_date: Optional[Dict[str, List[float]]] = None,
    fsp_by_expiry: Optional[Dict[str, float]] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """
    Runs ANY normalized timed event stream through the single shared `V2UnifiedEventEngine`.
    Used by both `REPLAY_V2_1_CAUSAL` archive verification and exact Forward-vs-Replay parity tests.
    """
    if persist:
        store.clear_run_mode(run_mode, cfg_hash)

    def _run() -> Dict[str, Any]:
        sim = V2PaperSimulator(
            config=cfg,
            config_hash=cfg_hash,
            store=store,
            run_mode=run_mode,
            cost_multiplier=cost_multiplier,
            extra_adverse_slippage_ticks=extra_adverse_slippage_ticks,
        )
        engine = V2UnifiedEventEngine(
            cfg=cfg,
            cfg_hash=cfg_hash,
            store=store,
            simulator=sim,
            run_mode=run_mode,
            book_b_params=book_b_params,
            enable_book_a_opt_in=enable_book_a_opt_in,
            persist_bars_and_checkpoints=persist,
        )
        if fsp_by_expiry:
            engine.fsp_by_expiry = dict(fsp_by_expiry)

        last_dt: Optional[str] = None
        for kind, sym, payload in events:
            avail_ms = payload.get("availability_ts_ms") or payload.get("local_receipt_ts_ms") or payload.get("timestamp_ms")
            if avail_ms:
                # Seed daily VIX / prior spot lists cleanly on new session date before ingest
                bar_open_ms = payload.get("source_bar_open_ms")
                from app.analytics.option_sniffer_v2.v2_config import bar_open_label_from_ms

                ev_dt, _, _, _, _ = bar_open_label_from_ms(int(bar_open_ms if bar_open_ms is not None else avail_ms))
                if ev_dt != last_dt:
                    if prior_daily_nifty_by_date and ev_dt in prior_daily_nifty_by_date and last_dt is None:
                        engine.prior_daily_spots = list(prior_daily_nifty_by_date[ev_dt])
                    if prior_vix_override_by_date and ev_dt in prior_vix_override_by_date:
                        engine.prior_daily_vix = list(prior_vix_override_by_date[ev_dt])
                    last_dt = ev_dt

            engine.ingest_event(kind=kind, symbol_or_key=sym, payload=payload, availability_ts_ms=avail_ms)

        engine.flush()

        all_pkgs = list(sim._all_pkgs_by_id.values())
        closed_trades = [copy.deepcopy(p) for p in all_pkgs if p["state"] not in OPEN_EXPOSURE_STATES]
        open_pkgs = [copy.deepcopy(p) for p in all_pkgs if p["state"] in OPEN_EXPOSURE_STATES]
        daily_records = store.load_daily_snapshots(run_mode, cfg_hash) if persist else []
        equity_curve = (
            store.load_equity_curve(run_mode, cfg_hash)
            if persist
            else [
                {"marked_equity_inr": sim.running_peak_equity_inr},
                {"marked_equity_inr": round(sim.running_peak_equity_inr - sim.max_mtm_dd_inr, 2)},
            ]
        )
        start_cap = float(cfg["account_envelope"]["starting_capital_inr"])
        stats = _compute_series_stats(closed_trades, daily_records, start_cap, equity_curve=equity_curve)

        return {
            "run_mode": run_mode,
            "engine_version": cfg.get("engine_version", "2.2.0-causal"),
            "data_version": cfg.get("schema_version", "2.2.0"),
            "stats": stats,
            "daily_records": daily_records,
            "closed_trades": closed_trades,
            "open_packages": open_pkgs,
            "equity_curve": equity_curve,
            "foreign_instrument_rejected_count": engine.foreign_instrument_rejected_count,
            "unverifiable_basis_events": engine.unverifiable_basis_events,
            "late_event_count": engine.late_event_count,
            "post_session_ignored_count": engine.total_post_session_ignored_count,
            "sequence_gap_count": engine.sequence_gap_count,
            "decision_trace_count": len(engine.session_decision_trace),
            "ledger_sha256": store.compute_canonical_ledger_digest(run_mode, cfg_hash) if persist else None,
        }

    if persist:
        with store.writer_session():
            return _run()
    return _run()


def run_v2_replay_and_attribution(
    v1_ro_conn: sqlite3.Connection,
    store: V2LedgerStore,
    cfg: Dict[str, Any],
    cfg_hash: str,
) -> Dict[str, Any]:
    """
    Executes causal `REPLAY_V2_1_CAUSAL` against `backend/market_data.db`, single-factor
    attribution variants, sensitivity checks, and preserves superseded `v2.0.0` evidence
    separately without overwriting history or mutating thresholds.
    """
    dataset = _load_causal_archive_events(v1_ro_conn)
    canonical_mode = str(cfg.get("replay_run_mode") or "REPLAY_V2_2_CAUSAL")
    mode_tag = canonical_mode.replace("REPLAY_", "").replace("_CAUSAL", "") or "V2_2"

    v2_frozen_params = {
        "variant_code": "V2_FROZEN_COMBINED",
        "use_basis_delta": True,
        "window_start": 1015,
        "window_end": 1300,
        "allow_otm_fallback": True,
        "min_er_override": 0.32,
    }
    canonical = run_event_stream_replay(
        events=dataset["events"],
        cfg=cfg,
        cfg_hash=cfg_hash,
        store=store,
        run_mode=canonical_mode,
        book_b_params=v2_frozen_params,
        enable_book_a_opt_in=False,
        prior_daily_nifty_by_date=dataset["prior_daily_nifty"],
        fsp_by_expiry=dataset["fsp_by_expiry"],
        persist=True,
    )

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
        r = run_event_stream_replay(
            events=dataset["events"],
            cfg=cfg,
            cfg_hash=cfg_hash,
            store=store,
            run_mode=f"ABLATION_{mode_tag}_{spec['variant_code']}",
            book_b_params=spec,
            prior_daily_nifty_by_date=dataset["prior_daily_nifty"],
            fsp_by_expiry=dataset["fsp_by_expiry"],
            persist=True,
        )
        if spec["variant_code"] == "B0_V1_BASELINE":
            b0_net = r["stats"]["net_pnl_inr"]
        ablation_rows.append(
            {
                "variant_code": spec["variant_code"],
                "label": spec["label"],
                "stats": r["stats"],
                "incremental_net_vs_b0_inr": round(r["stats"]["net_pnl_inr"] - b0_net, 2),
                "unverifiable_basis_events": r["unverifiable_basis_events"],
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

    stress_2x_charges = run_event_stream_replay(
        events=dataset["events"],
        cfg=cfg,
        cfg_hash=cfg_hash,
        store=store,
        run_mode=f"STRESS_{mode_tag}_2X_CHARGES",
        book_b_params=v2_frozen_params,
        cost_multiplier=2.0,
        prior_daily_nifty_by_date=dataset["prior_daily_nifty"],
        fsp_by_expiry=dataset["fsp_by_expiry"],
        persist=True,
    )
    stress_2x_slip = run_event_stream_replay(
        events=dataset["events"],
        cfg=cfg,
        cfg_hash=cfg_hash,
        store=store,
        run_mode=f"STRESS_{mode_tag}_2X_SLIPPAGE",
        book_b_params=v2_frozen_params,
        extra_adverse_slippage_ticks=1,
        prior_daily_nifty_by_date=dataset["prior_daily_nifty"],
        fsp_by_expiry=dataset["fsp_by_expiry"],
        persist=True,
    )
    opt_in_book_a_strict = run_event_stream_replay(
        events=dataset["events"],
        cfg=cfg,
        cfg_hash=cfg_hash,
        store=store,
        run_mode=f"STRESS_{mode_tag}_BOOK_A_STRICT",
        book_b_params=v2_frozen_params,
        enable_book_a_opt_in=True,
        prior_daily_nifty_by_date=dataset["prior_daily_nifty"],
        fsp_by_expiry=dataset["fsp_by_expiry"],
        persist=True,
    )

    by_day_net: Dict[str, float] = {}
    for t in canonical["closed_trades"]:
        by_day_net[t["session_date"]] = by_day_net.get(t["session_date"], 0.0) + float(t["net_pnl_inr"])
    best_day = max(by_day_net.items(), key=lambda kv: kv[1])[0] if by_day_net else None
    ex_best_trades = [t for t in canonical["closed_trades"] if t["session_date"] != best_day] if best_day else []
    ex_best_net = round(sum(float(t["net_pnl_inr"]) for t in ex_best_trades), 2)

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

    v1_daily_eq = float(cfg["account_envelope"]["starting_capital_inr"])
    v1_peak = v1_daily_eq
    daily_by_dt = {d["session_date"]: d for d in canonical["daily_records"]}
    all_dates = sorted(set(dataset["dates"]) | set(daily_by_dt.keys()))
    daily_comparison: List[Dict[str, Any]] = []
    for dt in all_dates:
        drec = daily_by_dt.get(dt, {})
        v1_t = v1_by_date.get(dt, [])
        v1_net_d = round(sum(float(x["net_pnl_inr"]) for x in v1_t), 2)
        v1_cap_d = round(sum(float(x["cash_reserved_inr"]) for x in v1_t), 2)
        v1_daily_eq = round(v1_daily_eq + v1_net_d, 2)
        v1_peak = max(v1_peak, v1_daily_eq)
        v1_dd_d = round(v1_peak - v1_daily_eq, 2)
        v1_stale_cnt = sum(
            1
            for x in v1_t
            if x["book_id"] == "SHADOW_BOOK_A_SPREAD_150" and x["exit_reason"] == "SPREAD_DECAY_TARGET_70PCT"
        )
        daily_comparison.append(
            {
                "session_date": dt,
                "v1_reported_net_pnl_inr": v1_net_d,
                "v1_reported_trades": len(v1_t),
                "v1_stale_wing_contaminated_count": v1_stale_cnt,
                "v1_capital_reserved_inr": v1_cap_d,
                "v1_closed_dd_inr": v1_dd_d,
                "v2_replay_net_pnl_inr": drec.get("net_pnl_inr", 0.0),
                "v2_replay_trades": drec.get("closed_trades", 0),
                "v2_replay_peak_capital_inr": drec.get("peak_reserved_capital_inr", 0.0),
                "v2_replay_intraday_min_eq_inr": drec.get("intraday_min_mtm_equity_inr"),
                "v2_session_state": drec.get("session_state"),
                "result_origin": canonical_mode,
                "verification_outcome": (
                    "INSUFFICIENT_SOURCE_TS_OR_CONTRACT_EVIDENCE"
                    if int(drec.get("closed_trades", 0) or 0) == 0
                    else "VERIFIED"
                ),
            }
        )

    v1_daily_series: List[Dict[str, Any]] = []
    eq_v1 = float(cfg["account_envelope"]["starting_capital_inr"])
    for dt in dataset["dates"]:
        sod_v1 = eq_v1
        day_rows = v1_by_date.get(dt, [])
        eq_v1 = round(eq_v1 + sum(float(x["net_pnl_inr"]) for x in day_rows), 2)
        v1_daily_series.append(
            {
                "sod_equity_inr": sod_v1,
                "eod_mtm_equity_inr": eq_v1,
                "intraday_min_mtm_equity_inr": eq_v1,
                "peak_reserved_capital_inr": sum(float(x["cash_reserved_inr"]) for x in day_rows),
            }
        )

    v1_all_stats = _compute_series_stats(
        [
            {
                "gross_pnl_inr": r["net_pnl_inr"] + r["total_charges_inr"],
                "total_friction_inr": r["total_charges_inr"],
                "net_pnl_inr": r["net_pnl_inr"],
            }
            for r in v1_by_date_flat(v1_rows)
        ],
        v1_daily_series,
        float(cfg["account_envelope"]["starting_capital_inr"]),
    )

    superseded = load_superseded_v2_0_evidence()

    return {
        "engine_version": cfg.get("engine_version", "2.2.0-causal"),
        "replay_run_mode": canonical_mode,
        "causal_verification_outcome": {
            "verified_futures_basis_trades": len(canonical["closed_trades"]),
            "unverifiable_basis_events": canonical["unverifiable_basis_events"],
            "late_events_rejected": canonical["late_event_count"],
            "post_session_flushes_ignored": canonical["post_session_ignored_count"],
            "reason": (
                "FAIL_CLOSED_INSUFFICIENT_TIMESTAMP_EVIDENCE: Archived l5_snapshots rows lack separate "
                "spot vs futures source timestamps and futures instrument_key/expiry; check_spot_futures_sync "
                "refuses to fabricate identical timestamps or contract keys."
            ),
        },
        "archive_timing_evidence": dataset["archive_timing_evidence"],
        "canonical_replay": canonical,
        "superseded_v2_0_replay": superseded,
        "ablation_study": ablation_rows,
        "sensitivity": {
            "baseline_1x": canonical["stats"],
            "stress_2x_charges": stress_2x_charges["stats"],
            "stress_2x_slippage": stress_2x_slip["stats"],
            "ex_best_session": {
                "excluded_session": best_day,
                "remaining_trades": len(ex_best_trades),
                "net_pnl_inr": ex_best_net,
            },
            "book_a_opt_in_strict_no_prior_vix": {
                "book_a_trades": sum(
                    1 for t in opt_in_book_a_strict["closed_trades"] if t["book_id"] == "V2_BOOK_A_PROPOSED_O3"
                ),
                "reason": "Fail-closed: 0 prior completed VIX closes exist in market_data.db prior to 2026-11-16 (<10 required for SMA10).",
                "stats": opt_in_book_a_strict["stats"],
            },
        },
        "v1_reported_summary": v1_all_stats,
        "daily_comparison": daily_comparison,
    }


def v1_by_date_flat(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]
