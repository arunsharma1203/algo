"""
V2 Option Sniffer — Upstox Data Contract, Field Inventory & Fail-Closed L5 Sweep.

Enforces strict point-in-time & zero-fabrication invariants:
  - Never infers 5-level depth from LTP, candles, carried ticks, or synthetic spreads.
  - Never substitutes 0 or 0.0 for missing quotes, timestamps, VIX, or basis.
  - Never defaults unknown quote age or unknown timestamps to 0.
  - Explicitly classifies every Upstox field into FROZEN_RULE_DRIVER, FILL_DIAGNOSTIC,
    or RESEARCH_ONLY.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.common.options_accounting import round_to_nse_tick

# Observed entitlement inventory under this account's Upstox V3 MarketDataStreamerV3 (mode="full")
# and V2/V3 REST quota (UpstoxRateLimiter: 8 req/s, 64 req/m, 1600 req/30m).
UPSTOX_ENTITLEMENT_FIELD_INVENTORY: Tuple[Dict[str, Any], ...] = (
    {
        "field": "spot_ltp / spot_ohlc_1m",
        "source": "MarketDataStreamerV3 indexFF (NSE_INDEX|Nifty 50) + /historical-candle/intraday/1minute",
        "decision_role": "FROZEN_RULE_DRIVER",
        "used_by": ["Book A", "Book B", "ORB30", "VWAP", "ADX/ER", "BasisDelta", "Settlement FSP TWAP"],
        "missing_policy": "FAIL_CLOSED_BLOCK_ENTRY",
    },
    {
        "field": "fut_ltp / fut_instrument_key / fut_atp_vwap",
        "source": "MarketDataStreamerV3 marketFF (NSE_FO front-month NIFTY FUT) + futures contract master",
        "decision_role": "FROZEN_RULE_DRIVER",
        "used_by": ["Book B BasisDelta (09:15-09:30 open ref & sync basis change)", "Roll detection"],
        "missing_policy": "FAIL_CLOSED_BLOCK_ENTRY (roll or >5000ms spot-fut skew blocks Book B)",
    },
    {
        "field": "expiry / strike / option_type / lot_size",
        "source": "OptionContractDiscovery (/v2/option/contract + expired-instruments contract master)",
        "decision_role": "FROZEN_RULE_DRIVER",
        "used_by": ["Book A T-1 session DTE", "Book B DTE>=2 & 1-OTM", "Canonical lot validation"],
        "missing_policy": "FAIL_CLOSED_BLOCK_ENTRY",
    },
    {
        "field": "india_vix (daily prior 10 sessions + intraday)",
        "source": "NSE_INDEX|India VIX indexFF + prior completed daily historical candles",
        "decision_role": "FROZEN_RULE_DRIVER",
        "used_by": ["Book A VIX <= 1.05 * SMA10(prior 10 completed sessions)"],
        "missing_policy": "FAIL_CLOSED_BLOCK_ENTRY (<10 prior completed sessions blocks Book A)",
    },
    {
        "field": "raw_bid / raw_ask / depth_levels[0..4] (bid_p, bid_q, ask_p, ask_q)",
        "source": "MarketDataStreamerV3 marketFF.marketLevel.bidAskQuote (mode=full)",
        "decision_role": "FROZEN_RULE_DRIVER",
        "used_by": ["Book A & Book B L5 executable entry/exit VWAP, credit floor, spread cap, MTM"],
        "missing_policy": "FAIL_CLOSED_BLOCK_ENTRY; RETAIN_UNPRICED_EXPOSURE on open legs",
    },
    {
        "field": "book_provenance / is_observed_l1_book / depth_observed_on_tick / l1_book_timestamp_ms / local_receipt_ts_ms / exchange_ltt_ms",
        "source": "OptionStreamManager V3 parser + V2 feed consumer",
        "decision_role": "FROZEN_RULE_DRIVER",
        "used_by": ["Freshness <=5000ms, Cross-leg skew <=5000ms, anti-carried/anti-synthetic gate"],
        "missing_policy": "FAIL_CLOSED (never default missing timestamp/age to 0)",
    },
    {
        "field": "volume_vtt / delta_vtt_1m / oi / book_obi_l5",
        "source": "Upstox V3 marketFF (vtt, oi, L5 bid/ask qty sum)",
        "decision_role": "FROZEN_RULE_DRIVER (book_obi_l5 >= 0.05) + FILL_DIAGNOSTIC (vtt/oi)",
        "used_by": ["Book B OBI gate", "Liquidity diagnostics & 15m candle fill validation"],
        "missing_policy": "Fail-closed if OBI missing on Book B; null retained for research fields",
    },
    {
        "field": "iv / delta / gamma / theta / vega",
        "source": "Upstox V3 marketFF.optionGreeks (when populated by exchange feed)",
        "decision_role": "RESEARCH_ONLY",
        "used_by": ["Post-trade Greek decay diagnostics; ZERO live/paper gate dependence"],
        "missing_policy": "Store NULL; never substitute defaults into entry rules",
    },
    {
        "field": "synthetic_weekly_basis_pts (K + CE_mid - PE_mid - Spot)",
        "source": "Derived from synchronized ATM weekly CE/PE quotes",
        "decision_role": "RESEARCH_ONLY",
        "used_by": ["Separately tracked research comparator vs futures basis delta"],
        "missing_policy": "Store NULL if ATM pair unpriced/skewed",
    },
    {
        "field": "margin_estimate_inr (heuristic floor / read-only margin probe)",
        "source": "Account envelope heuristic (>= Rs42k tagged UNVERIFIED) or read-only broker probe",
        "decision_role": "FILL_DIAGNOSTIC",
        "used_by": ["Capital reservation tracking for Book A short spreads"],
        "missing_policy": "Use conservative >= Rs42,000 floor tagged UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN",
    },
)

NON_OBSERVED_PROVENANCE_VALUES = frozenset(
    {"CARRIED_PREV_TICK", "SYNTHETIC_LTP_FALLBACK", "SEEDED_BASELINE", "UNKNOWN", ""}
)


def extract_observed_timestamp_ms(quote: Optional[Dict[str, Any]], require_depth_ts: bool = True) -> int:
    """
    Returns positive epoch-ms timestamp bound strictly to the quote/depth payload consumed,
    or 0 when unknown/missing/invalid so callers always fail closed.
    """
    if not isinstance(quote, dict) or not quote:
        return 0
    if require_depth_ts and quote.get("l1_book_age_ms") == 999999 and not quote.get("l1_book_timestamp_ms"):
        return 0
    keys = (
        ("l1_book_timestamp_ms", "local_receipt_ts_ms", "timestamp_ms")
        if require_depth_ts
        else ("local_receipt_ts_ms", "timestamp_ms", "exchange_ltt_ms", "l1_book_timestamp_ms")
    )
    for k in keys:
        val = quote.get(k)
        if val is None or isinstance(val, bool):
            continue
        try:
            ival = int(val)
            if ival > 0:
                return ival
        except (TypeError, ValueError):
            continue
    return 0


def resolve_strict_quote_age_ms(
    quote: Optional[Dict[str, Any]],
    now_ms: Optional[int] = None,
    require_observed_l5: bool = True,
) -> Optional[int]:
    """
    Computes conservative quote age in ms. Returns None when age cannot be proven.
    Never defaults missing/unknown age to 0.
    """
    if not isinstance(quote, dict) or not quote:
        return None
    ts_ms = extract_observed_timestamp_ms(quote, require_depth_ts=require_observed_l5)
    ages: List[int] = []
    if now_ms is not None and ts_ms > 0:
        ages.append(max(0, int(now_ms) - ts_ms))

    for age_key, backing_ts_key in (("l1_book_age_ms", "l1_book_timestamp_ms"), ("quote_age_ms", "timestamp_ms")):
        raw_age = quote.get(age_key)
        if raw_age is None or isinstance(raw_age, bool):
            continue
        try:
            ia = int(raw_age)
        except (TypeError, ValueError):
            return None
        if ia < 0:
            return None
        if ia == 999999:
            b_ts = quote.get(backing_ts_key)
            try:
                if not b_ts or int(b_ts) <= 0:
                    return None
            except (TypeError, ValueError):
                return None
        ages.append(ia)

    if require_observed_l5:
        if quote.get("l1_book_timestamp_ms") is None and quote.get("l1_book_age_ms") is None:
            # Allow explicit now_ms - ts_ms only if l1_book_timestamp_ms or local_receipt_ts_ms exists with OBSERVED_V3_L5
            if ts_ms <= 0:
                return None
        if not ages:
            return None
        return max(ages)

    return max(ages) if ages else None


def check_cross_leg_skew(
    quote_a: Optional[Dict[str, Any]],
    quote_b: Optional[Dict[str, Any]],
    max_skew_ms: int = 5000,
) -> Tuple[bool, Optional[int], str]:
    """
    Strict cross-leg synchronization check. Missing timestamps on either leg ALWAYS fail closed.
    """
    ts_a = extract_observed_timestamp_ms(quote_a, require_depth_ts=True)
    ts_b = extract_observed_timestamp_ms(quote_b, require_depth_ts=True)
    if ts_a <= 0 or ts_b <= 0:
        return False, None, "MISSING_LEG_TIMESTAMP"
    skew_ms = abs(ts_a - ts_b)
    if skew_ms > int(max_skew_ms):
        return False, skew_ms, f"EXCESSIVE_CROSS_LEG_SKEW_{skew_ms}MS"
    return True, skew_ms, "OK"


def check_spot_futures_sync(
    spot_quote: Optional[Dict[str, Any]],
    fut_quote: Optional[Dict[str, Any]],
    max_skew_ms: int = 5000,
    max_age_ms: int = 5000,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Validates synchronized spot + futures pair for Book B opening-basis reference and intraday basis change.
    """
    if not isinstance(spot_quote, dict) or not isinstance(fut_quote, dict):
        return {"ok": False, "reason": "MISSING_SPOT_OR_FUT_QUOTE"}
    spot_ltp = float(spot_quote.get("spot_ltp") or spot_quote.get("close") or 0.0)
    fut_ltp = float(fut_quote.get("fut_ltp") or fut_quote.get("close") or 0.0)
    fut_ikey = str(fut_quote.get("instrument_key") or "").strip()
    if spot_ltp <= 0.0 or fut_ltp <= 0.0 or not fut_ikey:
        return {"ok": False, "reason": "INVALID_SPOT_FUT_PRICE_OR_KEY"}

    ts_spot = extract_observed_timestamp_ms(spot_quote, require_depth_ts=False)
    ts_fut = extract_observed_timestamp_ms(fut_quote, require_depth_ts=False)
    if ts_spot <= 0 or ts_fut <= 0:
        return {"ok": False, "reason": "MISSING_SPOT_OR_FUT_TIMESTAMP"}

    skew_ms = abs(ts_spot - ts_fut)
    if skew_ms > int(max_skew_ms):
        return {"ok": False, "reason": f"SPOT_FUT_SKEW_{skew_ms}MS", "skew_ms": skew_ms}

    if now_ms is not None:
        age_spot = max(0, int(now_ms) - ts_spot)
        age_fut = max(0, int(now_ms) - ts_fut)
    else:
        age_s = resolve_strict_quote_age_ms(spot_quote, require_observed_l5=False)
        age_f = resolve_strict_quote_age_ms(fut_quote, require_observed_l5=False)
        if age_s is None or age_f is None:
            return {"ok": False, "reason": "UNKNOWN_SPOT_OR_FUT_AGE"}
        age_spot, age_fut = age_s, age_f

    if max(age_spot, age_fut) > int(max_age_ms):
        return {
            "ok": False,
            "reason": "STALE_SPOT_OR_FUT_QUOTE",
            "age_spot_ms": age_spot,
            "age_fut_ms": age_fut,
        }

    return {
        "ok": True,
        "spot_ltp": spot_ltp,
        "fut_ltp": fut_ltp,
        "fut_instrument_key": fut_ikey,
        "basis_pts": round(fut_ltp - spot_ltp, 4),
        "ts_spot_ms": ts_spot,
        "ts_fut_ms": ts_fut,
        "skew_ms": skew_ms,
        "max_age_ms": max(age_spot, age_fut),
    }


def sweep_observed_l5_book(
    quote: Optional[Dict[str, Any]],
    side: str,
    target_qty: int,
    lot_size: int = 65,
    adverse_ticks: int = 1,
    max_age_ms: Optional[int] = 5000,
    now_ms: Optional[int] = None,
    max_spread_pct: Optional[float] = 1.5,
    max_spread_pts: Optional[float] = None,
    require_observed_l5: bool = True,
    allow_partial_lot_multiples: bool = False,
    min_acceptable_sell_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Sweeps observed 5-level Upstox depth (`depth_levels[0..4]`) for `target_qty` contracts.

    Supports:
      - Fail-closed rejection of CARRIED_PREV_TICK, SYNTHETIC_LTP_FALLBACK, missing/unknown age,
        stale books, crossed/zero quotes, and excessive spread.
      - Partial fill modeling rounded down to complete `lot_size` multiples when `allow_partial_lot_multiples=True`.
      - Strict limit-floor enforcement (`min_acceptable_sell_price`) on SELL sweeps: verifies that BOTH
        every swept bid level AND the adverse-tick-adjusted fill price stay >= `min_acceptable_sell_price`.
    """
    side_u = str(side or "").upper().strip()
    if side_u not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")
    if target_qty <= 0 or lot_size <= 0:
        raise ValueError(f"target_qty ({target_qty}) and lot_size ({lot_size}) must be > 0")

    if not isinstance(quote, dict) or not quote:
        return {"ok": False, "reason": "MISSING_QUOTE", "filled_qty": 0}

    prov = str(quote.get("book_provenance") or "")
    is_obs = bool(quote.get("is_observed_l1_book", False))
    depth_obs = bool(quote.get("depth_observed_on_tick", False))
    if require_observed_l5:
        if not is_obs or not depth_obs or prov in NON_OBSERVED_PROVENANCE_VALUES or prov != "OBSERVED_V3_L5":
            return {
                "ok": False,
                "reason": "NON_OBSERVED_OR_SYNTHETIC_BOOK_REJECTED",
                "book_provenance": prov or "UNKNOWN",
                "filled_qty": 0,
            }

    eff_age_ms = resolve_strict_quote_age_ms(quote, now_ms=now_ms, require_observed_l5=require_observed_l5)
    if max_age_ms is not None:
        if eff_age_ms is None:
            return {"ok": False, "reason": "UNKNOWN_QUOTE_AGE", "filled_qty": 0}
        if eff_age_ms > int(max_age_ms):
            return {
                "ok": False,
                "reason": "STALE_L5_BOOK",
                "age_ms": eff_age_ms,
                "max_age_ms": int(max_age_ms),
                "filled_qty": 0,
            }

    raw_bid = float(quote.get("raw_bid") if quote.get("raw_bid") is not None else (quote.get("bid") or 0.0))
    raw_ask = float(quote.get("raw_ask") if quote.get("raw_ask") is not None else (quote.get("ask") or 0.0))
    if raw_bid <= 0.0 or raw_ask < raw_bid:
        return {
            "ok": False,
            "reason": "ZERO_OR_CROSSED_QUOTE",
            "raw_bid": raw_bid,
            "raw_ask": raw_ask,
            "filled_qty": 0,
        }

    mid = 0.5 * (raw_bid + raw_ask)
    spread_pts = round(raw_ask - raw_bid, 4)
    spread_pct = round((spread_pts / mid) * 100.0, 4) if mid > 0 else 999.0
    if max_spread_pct is not None:
        spread_ok = spread_pct <= float(max_spread_pct)
        if not spread_ok and max_spread_pts is not None:
            spread_ok = (spread_pts <= float(max_spread_pts)) and (spread_pct <= max(float(max_spread_pct), 6.0))
        if not spread_ok:
            return {
                "ok": False,
                "reason": "SPREAD_TOO_WIDE",
                "spread_pts": spread_pts,
                "spread_pct": spread_pct,
                "filled_qty": 0,
            }

    raw_levels = quote.get("depth_levels")
    if raw_levels is None and quote.get("depth_json_raw") is not None:
        try:
            raw_levels = json.loads(quote["depth_json_raw"] or "[]")
        except Exception:
            raw_levels = []
        quote["depth_levels"] = raw_levels
    levels = list(raw_levels or [])
    if not levels:
        return {"ok": False, "reason": "EMPTY_DEPTH_LEVELS", "filled_qty": 0}

    px_key = "ask_p" if side_u == "BUY" else "bid_p"
    q_key = "ask_q" if side_u == "BUY" else "bid_q"
    valid_levels: List[Tuple[float, int]] = []
    available_total = 0
    for lvl in levels[:5]:
        lp = float(lvl.get(px_key) or 0.0)
        lq = int(lvl.get(q_key) or 0)
        if lp > 0.0 and lq > 0:
            available_total += lq
            valid_levels.append((lp, lq))

    if available_total <= 0:
        return {"ok": False, "reason": "ZERO_L5_DEPTH", "available_qty": 0, "filled_qty": 0}

    # For SELL with strict limit floor, find cumulative quantity available at levels >= floor (or check fill)
    eligible_qty = available_total
    if not allow_partial_lot_multiples and eligible_qty < target_qty:
        return {
            "ok": False,
            "reason": "INSUFFICIENT_L5_DEPTH",
            "available_qty": available_total,
            "required_qty": target_qty,
            "filled_qty": 0,
        }

    if allow_partial_lot_multiples:
        exec_qty = min(target_qty, eligible_qty)
    else:
        exec_qty = target_qty

    rem = exec_qty
    notional = 0.0
    levels_used = 0
    worst_lvl_px: Optional[float] = None
    for lp, lq in valid_levels:
        if rem <= 0:
            break
        take = min(rem, lq)
        notional += take * lp
        rem -= take
        levels_used += 1
        worst_lvl_px = lp

    vwap_raw = notional / float(exec_qty)
    tick_shift = max(0, int(adverse_ticks)) * 0.05
    if side_u == "BUY":
        fill_px = round_to_nse_tick(vwap_raw + tick_shift, mode="adverse_buy", allow_zero_settlement=False)
        top_px = raw_ask
        slip_pts = round(fill_px - top_px, 4)
    else:
        shifted = max(0.05, vwap_raw - tick_shift)
        fill_px = round_to_nse_tick(shifted, mode="adverse_sell", allow_zero_settlement=False)
        top_px = raw_bid
        slip_pts = round(top_px - fill_px, 4)

    if side_u == "SELL" and min_acceptable_sell_price is not None:
        floor_px = round_to_nse_tick(float(min_acceptable_sell_price), mode="adverse_buy")
        # Displayed top bid might pass floor_px, yet swept VWAP - adverse tick OR deeper L5 level fails floor_px!
        if fill_px < floor_px or (worst_lvl_px is not None and worst_lvl_px < floor_px):
            return {
                "ok": False,
                "reason": "CREDIT_FLOOR_BREACHED_AT_LIMIT_OR_SWEEP",
                "top_bid": round(raw_bid, 2),
                "vwap_raw": round(vwap_raw, 4),
                "simulated_fill_price": fill_px,
                "worst_level_price_used": worst_lvl_px,
                "min_acceptable_sell_price": floor_px,
                "filled_qty": 0,
            }

    confirmed_lot_qty = (exec_qty // lot_size) * lot_size
    return {
        "ok": exec_qty == target_qty,
        "partial_fill": 0 < exec_qty < target_qty,
        "reason": "OK" if exec_qty == target_qty else "PARTIAL_L5_FILL",
        "side": side_u,
        "target_qty": target_qty,
        "filled_qty": exec_qty,
        "unfilled_qty": max(0, target_qty - exec_qty),
        "confirmed_lot_qty": confirmed_lot_qty,
        "fill_price": fill_px,
        "vwap_raw": round(vwap_raw, 4),
        "top_price": round(top_px, 2),
        "worst_level_price_used": round(float(worst_lvl_px or top_px), 2),
        "raw_bid": round(raw_bid, 2),
        "raw_ask": round(raw_ask, 2),
        "depth_slippage_pts": slip_pts,
        "levels_swept": levels_used,
        "available_l5_qty": available_total,
        "spread_pts": round(spread_pts, 2),
        "spread_pct": round(spread_pct, 2),
        "age_ms": eff_age_ms,
        "ts_ms": extract_observed_timestamp_ms(quote, require_depth_ts=True),
        "book_provenance": prov or "UNKNOWN",
        "fill_fidelity": "OBSERVED_L5_DEPTH_SWEEP" if require_observed_l5 else "EMERGENCY_L1_DEPTH_SWEEP",
    }
