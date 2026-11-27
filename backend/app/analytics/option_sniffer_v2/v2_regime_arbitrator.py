"""
V2 Option Sniffer — Causal Multi-Book Regime Arbitrator (`v2_regime_arbitrator.py`).

Pure point-in-time (timestamp <= t) strategy regime classifier and portfolio conflict guard.
Strictly paper-only and simulation-safe: contains zero broker, live order, or arm/disarm references.

Empirical Governance Basis (`v2_research_report_v2_3.json`, 59 sessions, N_trials=170):
- Anti-Self-Hedge Guard: Never open a bullish package in one book while a bearish package is open
  in the other book (eliminates paying spread slippage & gamma bleed against oneself).
- Theta-Gamma DTE Partition:
  * Trading DTE == 1: Book B (`min_trading_dte=2`) is barred; Theta zone (Book A DTE<=4 if enabled).
  * Trading DTE >= 5: Intraday Book A (`DTE<=4` gate) is barred (`>4 DTE` spreads failed 2x friction
    across 59 sessions); Convexity-only zone (Book B `2..12 DTE`).
  * 2 <= Trading DTE <= 4:
      - Under active ORB30 breakout + ADX14 >= 22 + ER20 >= 0.32 (10:15..13:00 IST):
        Book B Paid Convexity takes priority under 1-slot budget (`TREND_EXPANSION_CONVEXITY_PRIORITY`).
      - Outside breakout / after 13:00 IST: Theta/range zone (Book A candidate if enabled).
- Note: Book B (`V2_PREREGISTRATION_V1.json`) remains the frozen baseline; intraday Book A (`DTE<=4`)
  remains a non-default research candidate (`RESEARCH_CANDIDATE_NOT_PROVEN_OOS`: Holdout 2x dual-book
  is -INR 3,056) and stays disabled unless explicitly opted in.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from app.analytics.option_sniffer_v2.v2_book_a_engine import compute_vix_prior_10d_sma
from app.analytics.option_sniffer_v2.v2_config import trading_sessions_between


def _is_pkg_bullish(pkg: Dict[str, Any]) -> Optional[bool]:
    ls = pkg.get("legs_state") or {}
    if isinstance(ls, dict):
        if "is_bull" in ls and ls["is_bull"] is not None:
            return bool(ls["is_bull"])
        ll = ls.get("long_leg") or {}
        if ll.get("option_type") in ("CE", "PE"):
            return ll.get("option_type") == "CE"
        sl = ls.get("short_leg") or ls.get("short_target") or {}
        if sl.get("option_type") in ("CE", "PE"):
            return sl.get("option_type") == "PE"
    dlabel = str(pkg.get("direction_label") or "")
    if "CE" in dlabel or "BULL" in dlabel:
        return True
    if "PE" in dlabel or "BEAR" in dlabel:
        return False
    return None


def extract_open_directional_books(open_packages: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Returns open book IDs grouped by net directional delta bias (`bull_books`, `bear_books`)."""
    bull_books: List[str] = []
    bear_books: List[str] = []
    for pkg in open_packages or []:
        bid = str(pkg.get("book_id") or "")
        if not bid:
            continue
        is_bull = _is_pkg_bullish(pkg)
        if is_bull is True:
            bull_books.append(bid)
        elif is_bull is False:
            bear_books.append(bid)
    return {"bull_books": bull_books, "bear_books": bear_books}


def evaluate_v2_regime_arbitration(
    *,
    session_date: str,
    ist_hhmm: int,
    expiries: Iterable[str],
    spot_close: float,
    spot_vwap: Optional[float],
    orb30_h: Optional[float],
    orb30_l: Optional[float],
    orb30_ready: bool,
    adx14: Optional[float],
    er20: Optional[float],
    vix_now: Optional[float],
    prior_vix_closes: List[float],
    open_packages: List[Dict[str, Any]],
    max_open_packages_total: int,
    book_a_enabled: bool,
    book_b_enabled: bool,
    observed_sessions: Optional[Set[str]] = None,
    extra_holidays: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Causal single-bar multi-book regime arbitrator evaluated strictly on completed bar data.
    """
    dte_map: Dict[str, int] = {}
    for exp_s in sorted({str(e) for e in expiries if e}):
        if exp_s < session_date:
            continue
        tdte = trading_sessions_between(
            session_date,
            exp_s,
            observed_sessions=observed_sessions,
            extra_holidays=extra_holidays,
        )
        if tdte >= 1:
            dte_map[exp_s] = tdte

    nearest_exp: Optional[str] = None
    nearest_trading_dte: Optional[int] = None
    if dte_map:
        nearest_exp = min(dte_map.keys(), key=lambda e: (dte_map[e], e))
        nearest_trading_dte = dte_map[nearest_exp]

    book_b_eligible_exp = next((e for e in sorted(dte_map.keys()) if 2 <= dte_map[e] <= 12), None)
    book_a_dte4_eligible_exp = next((e for e in sorted(dte_map.keys()) if 1 <= dte_map[e] <= 4), None)

    dirs = extract_open_directional_books(open_packages)
    allowed_bull = len(dirs["bear_books"]) == 0
    allowed_bear = len(dirs["bull_books"]) == 0

    vix_sma10 = compute_vix_prior_10d_sma(prior_vix_closes, 10)
    vix_cap_ok: Optional[bool] = None
    if vix_now is not None and vix_now > 0 and vix_sma10 is not None and vix_sma10 > 0:
        vix_cap_ok = bool(float(vix_now) <= float(vix_sma10) * 1.05)

    trend_strength_ok = bool(
        adx14 is not None and er20 is not None and float(adx14) >= 22.0 and float(er20) >= 0.32
    )
    is_orb_breakout = False
    if orb30_ready and orb30_h and orb30_l and spot_vwap and spot_close > 0:
        is_orb_breakout = bool(
            (spot_close > orb30_h and spot_close > spot_vwap)
            or (spot_close < orb30_l and spot_close < spot_vwap)
        )

    if not orb30_ready or spot_vwap is None or spot_close <= 0:
        regime_code = "WARMUP_OBSERVING"
        preferred_book: Optional[str] = None
        reason = "Awaiting ORB30/VWAP warmup completion."
    elif nearest_trading_dte == 1:
        regime_code = "EXPIRY_EVE_THETA_ZONE_DTE1"
        preferred_book = "V2_BOOK_A_PROPOSED_O3" if book_a_enabled else "NONE_BOOK_A_DISABLED"
        reason = "Trading DTE=1 violates Book B min_trading_dte=2; only Theta spread candidate zone applies (Book A currently disabled)."
    elif nearest_trading_dte is not None and nearest_trading_dte >= 5:
        regime_code = "HIGH_DTE_CONVEXITY_ONLY_ZONE"
        preferred_book = "V2_BOOK_B_PAID_FLOW" if book_b_enabled else "NONE"
        reason = f"Trading DTE={nearest_trading_dte} >= 5 excludes intraday Book A (DTE<=4 ceiling); Book B convexity eligible."
    else:
        # 2 <= nearest_trading_dte <= 4 (or no option chain keys cached yet)
        if is_orb_breakout and trend_strength_ok and 1015 <= ist_hhmm <= 1300:
            regime_code = "TREND_EXPANSION_CONVEXITY_PRIORITY"
            preferred_book = "V2_BOOK_B_PAID_FLOW" if book_b_enabled else "NONE"
            reason = (
                f"Active ORB30+VWAP breakout with ADX={float(adx14):.1f}, ER={float(er20):.2f} "
                "in 10:15..13:00 IST -> Book B paid convexity takes priority."
            )
        elif ist_hhmm > 1300:
            regime_code = "LATE_SESSION_POST_BOOK_B_WINDOW"
            preferred_book = "V2_BOOK_A_PROPOSED_O3" if book_a_enabled else "STANDBY_POST_WINDOW"
            reason = "Past 13:00 IST Book B cutoff; intraday Theta candidate zone (Book A disabled in frozen baseline)."
        else:
            regime_code = "RANGE_OR_THETA_DECAY_ZONE_DTE2_4"
            preferred_book = "V2_BOOK_A_PROPOSED_O3" if book_a_enabled else "STANDBY_AWAITING_BREAKOUT"
            reason = "No confirmed ADX>=22/ER>=0.32 ORB30 breakout yet; standing by."

    return {
        "session_date": session_date,
        "ist_hhmm": int(ist_hhmm),
        "regime_code": regime_code,
        "preferred_book": preferred_book,
        "reason": reason,
        "nearest_expiry": nearest_exp,
        "nearest_trading_dte": nearest_trading_dte,
        "dte_by_expiry": dte_map,
        "book_b_eligible_exp": book_b_eligible_exp,
        "book_a_dte4_eligible_exp": book_a_dte4_eligible_exp,
        "is_orb_breakout": is_orb_breakout,
        "trend_strength_ok": trend_strength_ok,
        "vix_now": round(float(vix_now), 2) if vix_now else None,
        "vix_sma10": vix_sma10,
        "vix_cap_ok": vix_cap_ok,
        "open_bull_books": dirs["bull_books"],
        "open_bear_books": dirs["bear_books"],
        "allowed_bull": allowed_bull,
        "allowed_bear": allowed_bear,
        "max_open_packages_total": int(max_open_packages_total),
        "book_a_enabled": bool(book_a_enabled),
        "book_b_enabled": bool(book_b_enabled),
        "governance_note": (
            "Book B frozen baseline active; Book A intraday DTE<=4 remains RESEARCH_CANDIDATE_NOT_PROVEN_OOS "
            "(59-session combined 2x Holdout -Rs 3,056) and stays opt-in only."
        ),
    }


def check_candidate_arbitration_gate(
    arb_state: Dict[str, Any],
    *,
    book_id: str,
    is_bull: bool,
    open_total: int,
    max_open_packages_total: int,
    book_a_enabled: bool,
    book_b_enabled: bool,
    candidate_dte: Optional[int] = None,
    enforce_intraday_dte4_on_book_a: bool = False,
) -> Tuple[bool, Optional[str]]:
    """
    Portfolio & multi-book conflict gate checked immediately before opening a live or paper package:
    1. Portfolio concurrent slot ceiling (`open_total < max_open_packages_total`).
    2. Anti-self-hedge opposing-direction block (`bull vs bear` across open books).
    3. Optional Book A `DTE <= 4` intraday envelope guard.
    4. Single-slot priority yield (`Book B` convexity preempts `Book A` during active trend expansion).
    """
    if int(open_total) >= int(max_open_packages_total):
        return False, "MAX_PORTFOLIO_OPEN_SLOTS_REACHED"

    if is_bull and not bool(arb_state.get("allowed_bull", True)):
        return False, "BLOCKED_ANTI_SELF_HEDGE_OPPOSING_BEAR_OPEN"
    if (not is_bull) and not bool(arb_state.get("allowed_bear", True)):
        return False, "BLOCKED_ANTI_SELF_HEDGE_OPPOSING_BULL_OPEN"

    if (
        book_id == "V2_BOOK_A_PROPOSED_O3"
        and enforce_intraday_dte4_on_book_a
        and candidate_dte is not None
        and int(candidate_dte) > 4
    ):
        return False, f"BOOK_A_DTE_EXCEEDS_4:{int(candidate_dte)}"

    if (
        int(max_open_packages_total) == 1
        and book_a_enabled
        and book_b_enabled
        and book_id == "V2_BOOK_A_PROPOSED_O3"
        and arb_state.get("regime_code") == "TREND_EXPANSION_CONVEXITY_PRIORITY"
    ):
        return False, "YIELDED_SINGLE_SLOT_TO_BOOK_B_CONVEXITY"

    return True, None
