"""
Causal HAR-RV, Same-Week Black-76 ATM Straddle IV, VRP Ratio, and Passive AIML Breach Model.

Zero lookahead bias:
- Daily priors (RV1, RV5, RV20, SMA20, VIX_SMA10) strictly load completed sessions `< current_session_date`.
- HAR-RV forecasts remaining session annualized volatility strictly from completed prefix `<= decision_ts`.
- Market DB reads are strictly read-only (`mode=ro`, `uri=True`) against `continuous_index_ohlcv`.
- Missing option quotes or insufficient warmup fail closed cleanly without synthetic prices.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_WEIGHTS_PATH = Path(__file__).resolve().parent / "v2_har_breach_weights_v1.json"
_WEIGHTS_CACHE: Optional[Dict[str, Any]] = None

_DEFAULT_MARKET_DB_PATH = (
    Path(__file__).resolve().parents[3] / "market_data.db"
)


def load_frozen_v2_model_weights() -> Dict[str, Any]:
    """Load frozen TRAIN-only HAR-RV and UnderlyingBreachProbabilityModel weights."""
    global _WEIGHTS_CACHE
    if _WEIGHTS_CACHE is None:
        with open(_WEIGHTS_PATH, "r", encoding="utf-8") as fh:
            _WEIGHTS_CACHE = json.load(fh)
    return _WEIGHTS_CACHE


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black76_straddle_price(F: float, K: float, T_yr: float, sigma: float) -> float:
    """Analytical Black-76 ATM call + put straddle value (zero dividend / forward carry in F)."""
    if T_yr <= 1e-8 or sigma <= 1e-6 or F <= 0.0 or K <= 0.0:
        return abs(F - K)
    vol_sqrt_t = sigma * math.sqrt(T_yr)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T_yr) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    call = F * _norm_cdf(d1) - K * _norm_cdf(d2)
    put = K * _norm_cdf(-d2) - F * _norm_cdf(-d1)
    return call + put


def compute_atm_straddle_iv_pct(
    atm_ce_mid: Optional[float],
    atm_pe_mid: Optional[float],
    atm_strike: Optional[float],
    rem_mins_today: float,
    trading_dte: int,
) -> Optional[float]:
    """
    Invert same-week ATM straddle price (CE mid + PE mid) into annualized % implied volatility.

    Uses synthetic forward F = K + (C - P) and annualized trading minutes
    `(rem_mins_today + trading_dte * 375.0) / (375.0 * 252.0)` to match the 308-session
    VRP calibration study cleanly. Fails closed (`None`) on missing, zero, or arbitrage-violating quotes.
    """
    if (
        atm_ce_mid is None
        or atm_pe_mid is None
        or atm_strike is None
        or atm_ce_mid <= 0.0
        or atm_pe_mid <= 0.0
        or atm_strike <= 0.0
    ):
        return None
    strad = float(atm_ce_mid) + float(atm_pe_mid)
    k = float(atm_strike)
    fwd = k + (float(atm_ce_mid) - float(atm_pe_mid))
    tot_trade_mins = max(15.0, float(rem_mins_today) + max(0, int(trading_dte)) * 375.0)
    t_yr = tot_trade_mins / (375.0 * 252.0)
    intrinsic = abs(fwd - k)
    if strad <= intrinsic + 0.05:
        return None
    lo, hi = 0.02, 1.50
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if black76_straddle_price(fwd, k, t_yr, mid) < strad:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi) * 100.0, 3)


def load_prior_nifty_daily_history_readonly(
    ledger_db: Any,
    current_session_date: str,
    market_db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Load strictly prior (`session_date < current_session_date`) NIFTY daily RV, Spot closes,
    and optional India VIX closes from `osv2_session_summaries` and read-only `market_data.db`.
    """
    by_date: Dict[str, Dict[str, Any]] = {}

    # 1. Read completed prior sessions from read-only market_data.db continuous_index_ohlcv
    mdb_file = Path(market_db_path) if market_db_path else _DEFAULT_MARKET_DB_PATH
    if mdb_file.exists():
        conn = None
        try:
            conn = sqlite3.connect(f"file:{mdb_file}?mode=ro", uri=True)
            rows = conn.execute(
                """
                SELECT timestamp, close
                FROM continuous_index_ohlcv
                WHERE ticker IN ('NIFTY', 'Nifty 50', 'NIFTY 50')
                  AND substr(timestamp, 1, 10) < ?
                  AND close > 0
                ORDER BY timestamp ASC
                """,
                (str(current_session_date),),
            ).fetchall()
            bars_by_dt: Dict[str, List[Tuple[str, float]]] = {}
            for ts_raw, close_val in rows:
                ts_norm = str(ts_raw)[:19].replace("T", " ")
                dt_k = ts_norm[:10]
                hm = ts_norm[11:16]
                if "09:15" <= hm <= "15:29":
                    bars_by_dt.setdefault(dt_k, []).append((ts_norm, float(close_val)))
            for dt_k, seq in bars_by_dt.items():
                if len(seq) < 120:
                    continue
                seq.sort(key=lambda x: x[0])
                r2_sum = 0.0
                for idx in range(1, len(seq)):
                    prev_c = seq[idx - 1][1]
                    cur_c = seq[idx][1]
                    if prev_c > 0.0 and cur_c > 0.0:
                        lr = math.log(cur_c / prev_c)
                        r2_sum += lr * lr
                n_ret = max(1, len(seq) - 1)
                rv_d = math.sqrt(252.0 * (374.0 / float(n_ret)) * r2_sum) * 100.0
                by_date[dt_k] = {
                    "session_date": dt_k,
                    "rv_ann_pct": round(rv_d, 4),
                    "spot_close": round(seq[-1][1], 4),
                    "vix_close": None,
                    "source": "market_data.db",
                }
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # 2. Overlay completed prior osv2_session_summaries from the V2 ledger
    if ledger_db is not None:
        try:
            if hasattr(ledger_db, "get_prior_session_summaries"):
                prior_rows = ledger_db.get_prior_session_summaries(current_session_date)
            else:
                prior_rows = []
            for r in prior_rows:
                dt_k = str(r.get("session_date") or "")[:10]
                if not dt_k or dt_k >= str(current_session_date):
                    continue
                rv_val = r.get("rv_ann_pct")
                sp_val = r.get("spot_close")
                vx_val = r.get("vix_close")
                existing = by_date.get(dt_k, {"session_date": dt_k, "source": "osv2_ledger"})
                if rv_val is not None and float(rv_val) > 0.0:
                    existing["rv_ann_pct"] = float(rv_val)
                if sp_val is not None and float(sp_val) > 0.0:
                    existing["spot_close"] = float(sp_val)
                if vx_val is not None and float(vx_val) > 0.0:
                    existing["vix_close"] = float(vx_val)
                if existing.get("rv_ann_pct") and existing.get("spot_close"):
                    by_date[dt_k] = existing
        except Exception:
            pass

    ordered = [by_date[d] for d in sorted(by_date.keys())]
    rvs = [float(x["rv_ann_pct"]) for x in ordered if x.get("rv_ann_pct") is not None]
    closes = [float(x["spot_close"]) for x in ordered if x.get("spot_close") is not None]
    vixes = [float(x["vix_close"]) for x in ordered if x.get("vix_close") is not None and float(x["vix_close"]) > 0.0]

    rv1 = rvs[-1] if len(rvs) >= 1 else None
    rv5 = (sum(rvs[-5:]) / 5.0) if len(rvs) >= 5 else None
    rv20 = (sum(rvs[-20:]) / 20.0) if len(rvs) >= 20 else None
    sma20 = (sum(closes[-20:]) / 20.0) if len(closes) >= 20 else None
    vix_sma10 = (sum(vixes[-10:]) / 10.0) if len(vixes) >= 10 else None

    return {
        "current_session_date": str(current_session_date),
        "session_dates": [x["session_date"] for x in ordered],
        "daily_rv_ann": rvs,
        "daily_closes": closes,
        "prior_days_count": len(rvs),
        "warmup_ready": len(rvs) >= 20,
        "rv1": round(rv1, 4) if rv1 is not None else None,
        "rv5": round(rv5, 4) if rv5 is not None else None,
        "rv20": round(rv20, 4) if rv20 is not None else None,
        "sma20": round(sma20, 4) if sma20 is not None else None,
        "vix_sma10": round(vix_sma10, 4) if vix_sma10 is not None else None,
    }


def compute_har_rv_forecast_pct(
    r2_sum_today: Optional[float],
    elapsed_bars_today: int,
    rv1: Optional[float],
    rv5: Optional[float],
    rv20: Optional[float],
    beta_key: str = "har_vrp_beta",
) -> Tuple[Optional[float], Optional[float]]:
    """
    Compute (morning_rv_ann_pct, har_rv_forecast_pct) strictly from completed prefix today and prior daily RV.
    Returns `(None, None)` when insufficient bars (< 20) or insufficient 20-day history (fail closed).
    """
    if (
        r2_sum_today is None
        or elapsed_bars_today < 20
        or rv1 is None
        or rv5 is None
        or rv20 is None
    ):
        return None, None
    weights = load_frozen_v2_model_weights()
    beta = weights.get(beta_key) or weights["har_vrp_beta"]
    rv_morn = math.sqrt(252.0 * (374.0 / float(elapsed_bars_today)) * max(0.0, float(r2_sum_today))) * 100.0
    har_pred = max(
        4.0,
        float(beta[0])
        + float(beta[1]) * rv_morn
        + float(beta[2]) * float(rv1)
        + float(beta[3]) * float(rv5)
        + float(beta[4]) * float(rv20),
    )
    return round(rv_morn, 4), round(har_pred, 4)


def compute_vrp_metrics(
    *,
    atm_ce_mid: Optional[float],
    atm_pe_mid: Optional[float],
    atm_strike: Optional[float],
    spot_close: Optional[float],
    ist_hhmm: int,
    trading_dte: int,
    r2_sum_today: Optional[float],
    elapsed_bars_today: int,
    prior_history: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute same-week Black-76 ATM Straddle IV%, causal HAR-RV%, VRP ratio, and expected 1-sigma moves."""
    hh = int(ist_hhmm) // 100
    mm = int(ist_hhmm) % 100
    now_mins = hh * 60 + mm
    rem_mins_today = max(15.0, float((15 * 60 + 30) - now_mins))
    rem_to_1515 = max(10.0, float((15 * 60 + 15) - now_mins))

    warmup_ready = bool(prior_history.get("warmup_ready"))
    rv_morn, har_rv_pct = compute_har_rv_forecast_pct(
        r2_sum_today=r2_sum_today,
        elapsed_bars_today=elapsed_bars_today,
        rv1=prior_history.get("rv1"),
        rv5=prior_history.get("rv5"),
        rv20=prior_history.get("rv20"),
        beta_key="har_vrp_beta",
    )
    atm_iv_pct = compute_atm_straddle_iv_pct(
        atm_ce_mid=atm_ce_mid,
        atm_pe_mid=atm_pe_mid,
        atm_strike=atm_strike,
        rem_mins_today=rem_mins_today,
        trading_dte=trading_dte,
    )

    vrp_ratio: Optional[float] = None
    vrp_diff_pct: Optional[float] = None
    har_1sigma_pts: Optional[float] = None
    iv_1sigma_pts: Optional[float] = None
    status = "OK"

    if not warmup_ready:
        status = f"WARMING_UP_{int(prior_history.get('prior_days_count') or 0)}D"
    elif elapsed_bars_today < 20 or har_rv_pct is None:
        status = "WARMING_UP_INTRADAY"
    elif atm_ce_mid is None or atm_pe_mid is None or atm_strike is None:
        status = "MISSING_ATM_STRADDLE"
    elif atm_iv_pct is None:
        status = "INVALID_ATM_QUOTES"
    else:
        vrp_ratio = round(atm_iv_pct / har_rv_pct, 4)
        vrp_diff_pct = round(atm_iv_pct - har_rv_pct, 3)

    if spot_close and spot_close > 0.0 and har_rv_pct is not None:
        har_1sigma_pts = round(float(spot_close) * (har_rv_pct / 100.0) * math.sqrt(rem_to_1515 / (375.0 * 252.0)), 2)
    if spot_close and spot_close > 0.0 and atm_iv_pct is not None:
        iv_1sigma_pts = round(float(spot_close) * (atm_iv_pct / 100.0) * math.sqrt(rem_to_1515 / (375.0 * 252.0)), 2)

    return {
        "status": status,
        "warmup_ready": warmup_ready,
        "prior_days_count": int(prior_history.get("prior_days_count") or 0),
        "morning_rv_pct": rv_morn,
        "har_rv_pct": har_rv_pct,
        "atm_strike": float(atm_strike) if atm_strike is not None else None,
        "atm_ce_mid": round(float(atm_ce_mid), 2) if atm_ce_mid is not None else None,
        "atm_pe_mid": round(float(atm_pe_mid), 2) if atm_pe_mid is not None else None,
        "atm_iv_pct": atm_iv_pct,
        "vrp_ratio": vrp_ratio,
        "vrp_diff_pct": vrp_diff_pct,
        "har_1sigma_pts": har_1sigma_pts,
        "iv_1sigma_pts": iv_1sigma_pts,
    }


def _sigmoid(z: float) -> float:
    if z >= 30.0:
        return 1.0
    if z <= -30.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


def compute_passive_aiml_breach_telemetry(
    *,
    spot_close: Optional[float],
    vwap: Optional[float],
    session_high: Optional[float],
    session_low: Optional[float],
    adx14: Optional[float],
    er20: Optional[float],
    atrr_5_20: Optional[float],
    live_vix: Optional[float],
    direction_bullish: bool,
    barrier_pts: float,
    ist_hhmm: int,
    r2_sum_today: Optional[float],
    elapsed_bars_today: int,
    prior_history: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Passive-only AIML Underlying Breach Probability score (`advisory_gate_applied = False`).
    Never alters signal triggers or position sizing. Fails closed cleanly if VIX or 20D/10D history is unavailable.
    """
    _, har_vol = compute_har_rv_forecast_pct(
        r2_sum_today=r2_sum_today,
        elapsed_bars_today=elapsed_bars_today,
        rv1=prior_history.get("rv1"),
        rv5=prior_history.get("rv5"),
        rv20=prior_history.get("rv20"),
        beta_key="har_breach_beta",
    )
    sma20 = prior_history.get("sma20")
    vix_sma10 = prior_history.get("vix_sma10")

    if (
        har_vol is None
        or spot_close is None
        or spot_close <= 0.0
        or vwap is None
        or session_high is None
        or session_low is None
        or adx14 is None
        or er20 is None
        or atrr_5_20 is None
        or live_vix is None
        or live_vix <= 0.0
        or sma20 is None
        or float(sma20) <= 0.0
        or vix_sma10 is None
        or float(vix_sma10) <= 0.0
    ):
        missing = []
        if vix_sma10 is None:
            missing.append("VIX_10D_HISTORY")
        if sma20 is None or har_vol is None:
            missing.append("SPOT_20D_HISTORY")
        if live_vix is None or live_vix <= 0.0:
            missing.append("LIVE_VIX")
        if atrr_5_20 is None:
            missing.append("ATRR_5_20")
        return {
            "passive_only": True,
            "advisory_gate_applied": False,
            "p_breach": None,
            "status": "MISSING_" + ("_".join(missing) if missing else "FEATURES"),
        }

    weights = load_frozen_v2_model_weights()
    bm = weights["breach_model"]
    means = bm["means"]
    stds = bm["stds"]
    w = bm["w"]
    b = float(bm["b"])
    z_clip = float(bm.get("z_clip", 5.0))

    hh = int(ist_hhmm) // 100
    mm = int(ist_hhmm) % 100
    rem_m = max(15.0, float((15 * 60 + 15) - (hh * 60 + mm)))
    sc = float(spot_close)
    har_1sig_pts = max(8.0, sc * (har_vol / 100.0) * math.sqrt(rem_m / (375.0 * 252.0)))
    full_day_1sig = max(15.0, sc * (har_vol / 100.0) * math.sqrt(1.0 / 252.0))
    sgn = 1.0 if direction_bullish else -1.0
    day_rng = max(0.0, float(session_high) - float(session_low))
    cushion_ext = ((sc - float(session_low)) if direction_bullish else (float(session_high) - sc)) / har_1sig_pts

    raw_vec = [
        float(barrier_pts) / har_1sig_pts,
        har_vol / 15.0,
        float(live_vix) / max(1.0, float(vix_sma10)),
        float(adx14) / 30.0,
        float(er20),
        float(atrr_5_20),
        ((sc - float(vwap)) * sgn) / har_1sig_pts,
        ((sc - float(sma20)) / float(sma20) * 100.0 * sgn),
        day_rng / full_day_1sig,
        cushion_ext,
    ]
    z_vec = []
    for idx, val in enumerate(raw_vec):
        s = float(stds[idx]) if float(stds[idx]) > 1e-9 else 1.0
        zv = (float(val) - float(means[idx])) / s
        z_vec.append(max(-z_clip, min(z_clip, zv)))

    logit = b + sum(float(w[idx]) * z_vec[idx] for idx in range(len(w)))
    p_breach = round(_sigmoid(logit), 4)
    return {
        "passive_only": True,
        "advisory_gate_applied": False,
        "p_breach": p_breach,
        "har_breach_vol_pct": har_vol,
        "har_1sigma_pts": round(har_1sig_pts, 2),
        "status": "OK",
    }
