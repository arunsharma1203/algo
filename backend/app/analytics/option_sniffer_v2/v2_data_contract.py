"""
V2 Option Sniffer — Upstox Data Contract, Exact Instrument Identity & Executable Depth Sweep.

Enforces strict point-in-time & zero-fabrication invariants:
  - Never infers 5-level depth from LTP, candles, carried ticks, or synthetic spreads.
  - Never allows LTP or message receipt time (`local_receipt_ts_ms`) to refresh carried L2-L5 depth.
  - Never substitutes 0 or 0.0 for missing quotes, timestamps, VIX, or basis.
  - Rejects foreign underlyings (`BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `NIFTYNXT50`) across
    spot, futures, and options, and rejects unverifiable missing source timestamps/contracts.
  - Explicitly binds filled price, filled quantity, and consumed timestamp to the actual rungs swept.
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
        "used_by": ["Book A", "Book B", "ORB30", "TPB VWAP", "ADX14", "ER20", "Settlement FSP TWAP"],
        "missing_policy": "FAIL_CLOSED_BLOCK_ENTRY",
    },
    {
        "field": "fut_ltp / fut_instrument_key / fut_expiry / fut_atp_vwap",
        "source": "MarketDataStreamerV3 marketFF (NSE_FO exact NIFTY front FUT) + futures contract master",
        "decision_role": "FROZEN_RULE_DRIVER",
        "used_by": ["Book B BasisDelta (09:15-09:30 open ref & sync basis change)", "Exact NIFTY & Roll validation"],
        "missing_policy": "FAIL_CLOSED_UNVERIFIABLE (foreign underlying, roll, missing key/expiry, or missing source ts blocks Book B)",
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
        "field": "raw_bid / raw_ask / depth_levels[0..4] / depth_timestamp_ms / l1_timestamp_ms",
        "source": "MarketDataStreamerV3 marketFF.marketLevel.bidAskQuote (mode=full)",
        "decision_role": "FROZEN_RULE_DRIVER",
        "used_by": ["Book A & Book B executable entry/exit VWAP, L1-only strip when L2-L5 carried, MTM, ratchets, cutoffs"],
        "missing_policy": "FAIL_CLOSED_BLOCK_ENTRY; RETAIN_UNPRICED_EXPOSURE with last_reliable_mark on open legs",
    },
    {
        "field": "book_provenance / depth_rungs_carried / l1_observed_on_tick / exchange_ltt_ms",
        "source": "OptionStreamManager V3 additive depth telemetry + V2 feed consumer",
        "decision_role": "FROZEN_RULE_DRIVER",
        "used_by": ["Freshness <=5000ms bound strictly to consumed depth/L1 ts, Cross-leg skew <=5000ms"],
        "missing_policy": "FAIL_CLOSED (never default missing timestamp/age to 0; carried L2-L5 stripped/rejected)",
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
_FOREIGN_UNDERLYING_TOKENS = ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX")


def _is_exact_nifty_symbol(sym: str) -> bool:
    u = str(sym or "").upper().strip()
    if not u:
        return False
    for bad in _FOREIGN_UNDERLYING_TOKENS:
        if bad in u:
            return False
    if u == "NIFTY":
        return True
    if u.startswith("NIFTY ") or u.startswith("NIFTY_"):
        return True
    # Exchange trading symbol formats like NIFTY26NOVFUT or NIFTY26N2425800CE
    if u.startswith("NIFTY") and (len(u) > 5 and u[5].isdigit()):
        return True
    return False


def validate_nifty_spot_identity(spot_quote: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """Strict underlying identity validation for NIFTY spot index quotes."""
    if not isinstance(spot_quote, dict) or not spot_quote:
        return False, "MISSING_SPOT_QUOTE"
    u = str(spot_quote.get("underlying") or "").upper().strip()
    ikey = str(spot_quote.get("instrument_key") or "").strip()
    sym = str(spot_quote.get("trading_symbol") or spot_quote.get("symbol") or "").upper().strip()
    if u:
        if u != "NIFTY":
            return False, f"FOREIGN_SPOT_UNDERLYING:{u}"
    elif sym and not _is_exact_nifty_symbol(sym):
        return False, f"FOREIGN_SPOT_SYMBOL:{sym}"
    if ikey and ikey != "NSE_INDEX|Nifty 50":
        return False, f"FOREIGN_SPOT_INSTRUMENT_KEY:{ikey}"
    if not ikey and not u:
        return False, "MISSING_SPOT_IDENTITY"
    return True, "OK"


def validate_nifty_futures_identity(
    fut_quote: Optional[Dict[str, Any]],
    expected_contract: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Validates exact NIFTY futures underlying, contract instrument_key, and expiry.
    If `expected_contract` is supplied (session open ref), rejects mid-session contract changes/rolls.
    """
    if not isinstance(fut_quote, dict) or not fut_quote:
        return False, "MISSING_FUT_QUOTE"
    u = str(fut_quote.get("underlying") or "").upper().strip()
    ikey = str(fut_quote.get("instrument_key") or "").strip()
    exp = str(fut_quote.get("expiry") or "").strip()[:10]
    tsym = str(fut_quote.get("trading_symbol") or fut_quote.get("symbol") or "").upper().strip()

    for bad in _FOREIGN_UNDERLYING_TOKENS:
        if bad in u or bad in tsym or bad in ikey.upper():
            return False, f"FOREIGN_FUTURES_UNDERLYING:{u or tsym or ikey}"

    if u and u != "NIFTY":
        return False, f"FOREIGN_FUTURES_UNDERLYING:{u}"
    if tsym and not _is_exact_nifty_symbol(tsym):
        return False, f"INVALID_NIFTY_FUTURES_SYMBOL:{tsym}"
    if not u and not tsym:
        return False, "UNVERIFIABLE_MISSING_FUT_UNDERLYING"

    if not ikey or not exp or len(exp) != 10:
        return False, "UNVERIFIABLE_MISSING_FUT_CONTRACT_OR_EXPIRY"

    if expected_contract:
        exp_ikey = str(expected_contract.get("fut_instrument_key") or expected_contract.get("instrument_key") or "").strip()
        exp_exp = str(expected_contract.get("fut_expiry") or expected_contract.get("expiry") or "").strip()[:10]
        if exp_ikey and ikey != exp_ikey:
            return False, "FUTURES_CONTRACT_ROLLED_MID_SESSION"
        if exp_exp and exp != exp_exp:
            return False, "FUTURES_CONTRACT_ROLLED_MID_SESSION"
    return True, "OK"


def validate_nifty_option_identity(contract_or_quote: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """Ensures only intended NIFTY option contracts enter option lookup or valuation."""
    if not isinstance(contract_or_quote, dict) or not contract_or_quote:
        return False, "MISSING_OPTION_CONTRACT"
    u = str(contract_or_quote.get("underlying") or "").upper().strip()
    tsym = str(contract_or_quote.get("trading_symbol") or "").upper().strip()
    ikey = str(contract_or_quote.get("instrument_key") or "").strip()
    for bad in _FOREIGN_UNDERLYING_TOKENS:
        if bad in u or bad in tsym or bad in ikey.upper():
            return False, f"FOREIGN_OPTION_UNDERLYING:{u or tsym}"
    if u and u != "NIFTY":
        return False, f"FOREIGN_OPTION_UNDERLYING:{u}"
    if tsym and not _is_exact_nifty_symbol(tsym):
        return False, f"FOREIGN_OPTION_SYMBOL:{tsym}"
    exp = str(contract_or_quote.get("expiry") or "").strip()
    ot = str(contract_or_quote.get("option_type") or "").upper().strip()
    try:
        stk = float(contract_or_quote.get("strike") or 0.0)
    except (TypeError, ValueError):
        stk = 0.0
    if not ikey or (tsym and (not exp or ot not in ("CE", "PE") or stk <= 0.0)):
        return False, "INVALID_OPTION_CONTRACT_META"
    return True, "OK"


def _int_pos(val: Any) -> int:
    if val is None or isinstance(val, bool):
        return 0
    try:
        iv = int(val)
        return iv if iv > 0 else 0
    except (TypeError, ValueError):
        return 0


def _resolve_depth_and_l1_timestamps_ms(quote: Dict[str, Any]) -> Tuple[int, int]:
    """
    Returns `(observed_full_depth_ts_ms, observed_l1_ts_ms)` strictly from data provenance.
    NEVER uses `local_receipt_ts_ms` alone to refresh carried L2-L5 depth.
    """
    if quote.get("l1_book_age_ms") == 999999 and not (quote.get("depth_timestamp_ms") or quote.get("l1_book_timestamp_ms")):
        return 0, 0

    carried = bool(quote.get("depth_rungs_carried", False))
    prov = str(quote.get("book_provenance") or "")
    depth_obs = bool(quote.get("depth_observed_on_tick", False))

    depth_ts = 0
    if not carried and prov == "OBSERVED_V3_L5" and depth_obs:
        depth_ts = _int_pos(quote.get("depth_timestamp_ms")) or _int_pos(quote.get("l1_book_timestamp_ms"))

    l1_ts = 0
    l1_obs = quote.get("l1_observed_on_tick")
    if l1_obs is True:
        l1_ts = _int_pos(quote.get("l1_timestamp_ms")) or _int_pos(quote.get("l1_book_timestamp_ms"))
    elif l1_obs is None and not carried and prov == "OBSERVED_V3_L5" and bool(quote.get("is_observed_l1_book", False)):
        l1_ts = _int_pos(quote.get("l1_timestamp_ms")) or _int_pos(quote.get("l1_book_timestamp_ms"))

    return depth_ts, l1_ts


def extract_observed_timestamp_ms(quote: Optional[Dict[str, Any]], require_depth_ts: bool = True) -> int:
    """
    Returns positive epoch-ms timestamp bound strictly to the quote/depth payload consumed,
    or 0 when unknown/missing/invalid so callers always fail closed.
    """
    if not isinstance(quote, dict) or not quote:
        return 0
    if "spot_ltp" in quote or "fut_ltp" in quote or "source_timestamp_ms" in quote:
        if quote.get("source_ts_proven") is False:
            return 0
        for k in ("source_timestamp_ms", "exchange_ltt_ms", "timestamp_ms", "local_receipt_ts_ms"):
            iv = _int_pos(quote.get(k))
            if iv > 0:
                return iv
        return 0

    depth_ts, l1_ts = _resolve_depth_and_l1_timestamps_ms(quote)
    if require_depth_ts:
        return depth_ts
    return depth_ts or l1_ts


def resolve_strict_quote_age_ms(
    quote: Optional[Dict[str, Any]],
    now_ms: Optional[int] = None,
    require_observed_l5: bool = True,
) -> Optional[int]:
    """
    Computes conservative quote age in ms bound strictly to observed depth (or L1 when allowed).
    Returns None when age/provenance cannot be proven; NEVER defaults unknown age to 0.
    """
    if not isinstance(quote, dict) or not quote:
        return None
    ts_ms = extract_observed_timestamp_ms(quote, require_depth_ts=require_observed_l5)
    if ts_ms <= 0:
        return None

    ages: List[int] = []
    if now_ms is not None:
        if ts_ms > int(now_ms):
            return None
        ages.append(max(0, int(now_ms) - ts_ms))

    # For option quotes, check reported book age against observed timestamp consistency
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
            b_ts = _int_pos(quote.get(backing_ts_key))
            if b_ts <= 0:
                return None
        # Only honor reported age_ms when not carried unless backing ts matches depth/L1 ts
        if not quote.get("depth_rungs_carried", False):
            ages.append(ia)

    return max(ages) if ages else None


def check_cross_leg_skew(
    quote_a: Optional[Dict[str, Any]],
    quote_b: Optional[Dict[str, Any]],
    max_skew_ms: int = 5000,
    consumed_ts_a: Optional[int] = None,
    consumed_ts_b: Optional[int] = None,
    require_depth_ts: bool = True,
) -> Tuple[bool, Optional[int], str]:
    """
    Strict cross-leg synchronization check using the actual consumed depth/L1 timestamps.
    Missing or carried timestamps on either leg ALWAYS fail closed.
    """
    ts_a = int(consumed_ts_a) if consumed_ts_a is not None else extract_observed_timestamp_ms(quote_a, require_depth_ts=require_depth_ts)
    ts_b = int(consumed_ts_b) if consumed_ts_b is not None else extract_observed_timestamp_ms(quote_b, require_depth_ts=require_depth_ts)
    if ts_a <= 0 or ts_b <= 0:
        return False, None, "MISSING_OR_UNPROVEN_LEG_TIMESTAMP"
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
    expected_fut_contract: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Validates NIFTY spot + exact NIFTY futures identity, separate source timestamps,
    availability at `now_ms`, freshness, and cross-feed skew. Fails closed as unverifiable
    if source timestamps or futures contract metadata are missing or synthetic.
    """
    sp_id_ok, sp_id_rsn = validate_nifty_spot_identity(spot_quote)
    if not sp_id_ok:
        return {"ok": False, "verifiable": False, "reason": sp_id_rsn}

    ft_id_ok, ft_id_rsn = validate_nifty_futures_identity(fut_quote, expected_contract=expected_fut_contract)
    if not ft_id_ok:
        return {"ok": False, "verifiable": False, "reason": ft_id_rsn}

    spot_ltp = float(spot_quote.get("spot_ltp") or spot_quote.get("close") or 0.0)
    fut_ltp = float(fut_quote.get("fut_ltp") or fut_quote.get("close") or 0.0)
    if spot_ltp <= 0.0 or fut_ltp <= 0.0:
        return {"ok": False, "verifiable": False, "reason": "INVALID_SPOT_OR_FUT_PRICE"}

    if spot_quote.get("source_ts_proven") is False or fut_quote.get("source_ts_proven") is False:
        return {"ok": False, "verifiable": False, "reason": "UNVERIFIABLE_MISSING_SOURCE_TS_OR_CONTRACT"}

    if now_ms is not None:
        sp_avail = _int_pos(spot_quote.get("availability_ts_ms"))
        ft_avail = _int_pos(fut_quote.get("availability_ts_ms"))
        if (sp_avail > 0 and sp_avail > int(now_ms)) or (ft_avail > 0 and ft_avail > int(now_ms)):
            return {"ok": False, "verifiable": False, "reason": "FUTURE_SPOT_OR_FUT_NOT_AVAILABLE"}

    ts_spot = extract_observed_timestamp_ms(spot_quote, require_depth_ts=False)
    ts_fut = extract_observed_timestamp_ms(fut_quote, require_depth_ts=False)
    if ts_spot <= 0 or ts_fut <= 0:
        return {"ok": False, "verifiable": False, "reason": "UNVERIFIABLE_MISSING_SOURCE_TS_OR_CONTRACT"}

    skew_ms = abs(ts_spot - ts_fut)
    if skew_ms > int(max_skew_ms):
        return {"ok": False, "verifiable": True, "reason": f"SPOT_FUT_SKEW_{skew_ms}MS", "skew_ms": skew_ms}

    if now_ms is not None:
        age_spot = max(0, int(now_ms) - ts_spot)
        age_fut = max(0, int(now_ms) - ts_fut)
    else:
        age_s = resolve_strict_quote_age_ms(spot_quote, require_observed_l5=False)
        age_f = resolve_strict_quote_age_ms(fut_quote, require_observed_l5=False)
        if age_s is None or age_f is None:
            return {"ok": False, "verifiable": False, "reason": "UNKNOWN_SPOT_OR_FUT_AGE"}
        age_spot, age_fut = age_s, age_f

    if max(age_spot, age_fut) > int(max_age_ms):
        return {
            "ok": False,
            "verifiable": True,
            "reason": "STALE_SPOT_OR_FUT_QUOTE",
            "age_spot_ms": age_spot,
            "age_fut_ms": age_fut,
        }

    return {
        "ok": True,
        "verifiable": True,
        "spot_ltp": spot_ltp,
        "fut_ltp": fut_ltp,
        "fut_instrument_key": str(fut_quote["instrument_key"]).strip(),
        "fut_expiry": str(fut_quote["expiry"]).strip()[:10],
        "fut_trading_symbol": str(fut_quote.get("trading_symbol") or fut_quote["instrument_key"]).strip(),
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
    allow_fresh_l1_only: Optional[bool] = None,
    allow_partial_lot_multiples: bool = False,
    min_acceptable_sell_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Sweeps observed option liquidity strictly binding consumed price, quantity, and timestamp
    to genuine depth provenance:
      - Never allows `local_receipt_ts_ms` or LTP to refresh carried L2-L5 depth.
      - Entries (`require_observed_l5=True`) require full non-carried `OBSERVED_V3_L5` depth.
      - Exits / MTM / ratchets / unwinds / cutoffs / counterfactuals (`require_observed_l5=False`)
        can fall back ONLY to fresh independently-observed L1 top-of-book (`allow_fresh_l1_only=True`)
        and strictly strip carried L2-L5 rungs so no carried size is ever consumed.
    """
    side_u = str(side or "").upper().strip()
    if side_u not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")
    if target_qty <= 0 or lot_size <= 0:
        raise ValueError(f"target_qty ({target_qty}) and lot_size ({lot_size}) must be > 0")

    if not isinstance(quote, dict) or not quote:
        return {"ok": False, "reason": "MISSING_QUOTE", "filled_qty": 0}

    id_ok, id_rsn = validate_nifty_option_identity(quote)
    if not id_ok:
        return {"ok": False, "reason": id_rsn, "filled_qty": 0}

    avail_ms = _int_pos(quote.get("availability_ts_ms"))
    if now_ms is not None and avail_ms > int(now_ms):
        return {"ok": False, "reason": "FUTURE_QUOTE_NOT_YET_AVAILABLE", "filled_qty": 0}

    prov = str(quote.get("book_provenance") or "")
    if prov in NON_OBSERVED_PROVENANCE_VALUES and prov != "CARRIED_PREV_TICK":
        return {
            "ok": False,
            "reason": "NON_OBSERVED_OR_SYNTHETIC_BOOK_REJECTED",
            "book_provenance": prov or "UNKNOWN",
            "filled_qty": 0,
        }

    depth_ts, l1_ts = _resolve_depth_and_l1_timestamps_ms(quote)
    allow_l1_fallback = (not require_observed_l5) if allow_fresh_l1_only is None else bool(allow_fresh_l1_only)

    # Check if full observed non-carried L5 depth is fresh at `now_ms`
    full_l5_fresh = False
    full_l5_age: Optional[int] = None
    if depth_ts > 0 and prov == "OBSERVED_V3_L5" and not quote.get("depth_rungs_carried", False):
        if now_ms is not None:
            if depth_ts > int(now_ms):
                return {"ok": False, "reason": "FUTURE_DEPTH_TIMESTAMP", "filled_qty": 0}
            full_l5_age = max(0, int(now_ms) - depth_ts)
        else:
            raw_a = _int_pos(quote.get("l1_book_age_ms"))
            full_l5_age = raw_a if quote.get("l1_book_age_ms") is not None else 0
        if max_age_ms is None or (full_l5_age is not None and full_l5_age <= int(max_age_ms)):
            # Also enforce explicit reported depth age if provided on non-carried snapshot
            rep_age = quote.get("l1_book_age_ms")
            if rep_age is not None and not isinstance(rep_age, bool):
                try:
                    ia = int(rep_age)
                    if ia < 0 or ia == 999999 or (max_age_ms is not None and ia > int(max_age_ms)):
                        full_l5_age = ia
                    else:
                        full_l5_age = max(full_l5_age or 0, ia)
                        full_l5_fresh = True
                except (TypeError, ValueError):
                    full_l5_fresh = False
            else:
                full_l5_fresh = True

    use_l1_only_mode = False
    consumed_ts_ms = 0
    eff_age_ms: Optional[int] = None

    if full_l5_fresh:
        consumed_ts_ms = depth_ts
        eff_age_ms = full_l5_age
    else:
        if require_observed_l5 or not allow_l1_fallback:
            reason_fail = (
                "NON_OBSERVED_OR_SYNTHETIC_BOOK_REJECTED"
                if (prov in NON_OBSERVED_PROVENANCE_VALUES or quote.get("depth_rungs_carried"))
                else ("UNKNOWN_QUOTE_AGE" if depth_ts <= 0 else "STALE_L5_BOOK")
            )
            return {
                "ok": False,
                "reason": reason_fail,
                "age_ms": full_l5_age,
                "book_provenance": prov or "UNKNOWN",
                "filled_qty": 0,
            }
        # L1-only mode on exits/MTM/ratchet/unwind/cutoff: MUST have fresh observed top-of-book L1!
        if l1_ts <= 0 or prov in NON_OBSERVED_PROVENANCE_VALUES:
            reason_l1 = (
                "NON_OBSERVED_OR_SYNTHETIC_BOOK_REJECTED"
                if prov in NON_OBSERVED_PROVENANCE_VALUES
                else "UNKNOWN_QUOTE_AGE"
            )
            return {"ok": False, "reason": reason_l1, "filled_qty": 0}
        if now_ms is not None:
            if l1_ts > int(now_ms):
                return {"ok": False, "reason": "FUTURE_L1_TIMESTAMP", "filled_qty": 0}
            l1_age = max(0, int(now_ms) - l1_ts)
        else:
            l1_age = 0
        if max_age_ms is not None and l1_age > int(max_age_ms):
            return {"ok": False, "reason": "STALE_L5_BOOK", "age_ms": l1_age, "filled_qty": 0}
        use_l1_only_mode = True
        consumed_ts_ms = l1_ts
        eff_age_ms = l1_age

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

    px_key = "ask_p" if side_u == "BUY" else "bid_p"
    q_key = "ask_q" if side_u == "BUY" else "bid_q"
    valid_levels: List[Tuple[float, int]] = []
    available_total = 0

    if use_l1_only_mode:
        # Strictly strip carried/stale L2-L5 rungs! Use ONLY independently observed L1 size!
        obs_q = (
            _int_pos(quote.get("l1_ask_qty_observed")) or _int_pos(quote.get("ask_qty"))
            if side_u == "BUY"
            else _int_pos(quote.get("l1_bid_qty_observed")) or _int_pos(quote.get("bid_qty"))
        )
        l1_px = raw_ask if side_u == "BUY" else raw_bid
        if obs_q <= 0:
            raw_levels = quote.get("observed_rungs_on_tick") or []
            if raw_levels:
                obs_q = _int_pos(raw_levels[0].get(q_key))
                l1_px = float(raw_levels[0].get(px_key) or l1_px)
        if obs_q <= 0 or l1_px <= 0.0:
            return {
                "ok": False,
                "reason": "ZERO_OBSERVED_L1_SIZE_CARRIED_L2_L5_STRIPPED",
                "available_qty": 0,
                "filled_qty": 0,
            }
        valid_levels = [(l1_px, obs_q)]
        available_total = obs_q
    else:
        raw_levels = quote.get("observed_rungs_on_tick")
        if raw_levels is None:
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
        for lvl in levels[:5]:
            lp = float(lvl.get(px_key) or 0.0)
            lq = int(lvl.get(q_key) or 0)
            if lp > 0.0 and lq > 0:
                available_total += lq
                valid_levels.append((lp, lq))

    if available_total <= 0:
        return {"ok": False, "reason": "ZERO_L5_DEPTH", "available_qty": 0, "filled_qty": 0}

    eligible_qty = available_total
    if not allow_partial_lot_multiples and eligible_qty < target_qty:
        return {
            "ok": False,
            "reason": "INSUFFICIENT_OBSERVED_L1_SIZE" if use_l1_only_mode else "INSUFFICIENT_L5_DEPTH",
            "available_qty": available_total,
            "required_qty": target_qty,
            "filled_qty": 0,
        }

    exec_qty = min(target_qty, eligible_qty) if allow_partial_lot_multiples else target_qty
    rem = exec_qty
    notional = 0.0
    levels_used = 0
    worst_lvl_px: Optional[float] = None
    consumed_rungs: List[Dict[str, Any]] = []
    for lp, lq in valid_levels:
        if rem <= 0:
            break
        take = min(rem, lq)
        notional += take * lp
        rem -= take
        levels_used += 1
        worst_lvl_px = lp
        consumed_rungs.append({"price": round(lp, 2), "qty_taken": int(take), "book_qty": int(lq)})

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
        "consumed_rungs": consumed_rungs,
        "available_l5_qty": available_total,
        "spread_pts": round(spread_pts, 2),
        "spread_pct": round(spread_pct, 2),
        "age_ms": eff_age_ms,
        "ts_ms": consumed_ts_ms,
        "consumed_ts_ms": consumed_ts_ms,
        "carried_l2_l5_stripped": use_l1_only_mode,
        "book_provenance": prov or "UNKNOWN",
        "fill_fidelity": (
            "OBSERVED_L5_DEPTH_SWEEP"
            if not use_l1_only_mode
            else "OBSERVED_L1_ONLY_TOP_OF_BOOK"
        ),
    }
