"""
CONSENSUS ECONOMIC ENGINE & HARD SESSION GUARD
==============================================
Production-grade execution safety, economic break-even calculation,
entry cutoff gating, and rigorous trade accounting reconciliation.

Key Principles:
1. Hard Session Gate: Intraday entries impossible outside valid NSE cash trading hours (09:15-15:30 Mon-Fri).
2. Auditable Entry Cutoff: Configurable cutoff gate (default 15:00:00). Rejections logged as ENTRY_CUTOFF_REJECTED.
3. True Economic Break-Even: Position/size-aware calculation covering brokerage + slippage + statutory fees
   so that a breakeven exit produces guaranteed non-negative net P&L.
4. Explicit Terminal Exit Labels: ECONOMIC_BREAKEVEN, PROFIT_LOCK, STOP, TARGET, TIME_EXIT, SESSION_EXIT, INVALIDATED.
5. Strict Accounting Invariance: Reconciles all closed trades = all exit categories = gross P&L - fees = net P&L.
"""

import math
import logging
from datetime import datetime, time
from typing import Dict, Any, Tuple, Optional, List
import sqlite3

logger = logging.getLogger(__name__)

# Official Upstox / Indian Brokerage Cost Constants for Intraday Cash
BROKERAGE_PER_LEG = 20.0             # ₹20 flat per executed order leg (Upstox / Zerodha)
ROUNDTRIP_BROKERAGE = 40.0           # ₹40 flat for buy + sell
SLIPPAGE_BPS_PER_LEG = 5.0           # 5 basis points (0.05%) execution slippage per leg
ROUNDTRIP_SLIPPAGE_BPS = 10.0        # 10 basis points (0.10%) round-trip turnover
STATUTORY_TURNOVER_RATE = 0.00035    # STT (0.025% sell) + Exchange turnover (0.00325% both) + Stamp duty (0.003% buy)
GST_RATE = 0.18                      # 18% GST on brokerage + exchange turnover charges

# Operational Session Configuration
MARKET_OPEN_TIME = time(9, 15, 0)    # 09:15 IST
MARKET_CLOSE_TIME = time(15, 30, 0)  # 15:30 IST
DEFAULT_ENTRY_CUTOFF_TIME = time(15, 0, 0)  # Default 15:00 IST entry cutoff gate
MIN_DESIRED_NET_PROFIT = 10.0        # Minimum net profit (₹) required on economic breakeven exit


class ConsensusEconomicEngine:
    """
    Authoritative calculations for execution economics and session safety.
    """

    @classmethod
    def estimate_roundtrip_friction(cls, entry_price: float, shares: int) -> float:
        """
        Estimates total realistic round-trip friction in rupees for a given trade.
        Friction = Round-trip Brokerage (₹40)
                 + Round-trip Slippage (10 bps of position capital)
                 + Statutory STT/Turnover/Stamp Duty (~₹3.00)
                 + 18% GST on brokerage (~₹7.20).
        Total on standard ₹20,000 slot size: ~₹70.00 (35 bps of capital).
        """
        if entry_price <= 0 or shares <= 0:
            return ROUNDTRIP_BROKERAGE

        position_capital = entry_price * shares
        # 10 bps round-trip slippage on position value (5 bps on entry, 5 bps on exit)
        slippage = position_capital * (ROUNDTRIP_SLIPPAGE_BPS / 10000.0)
        # Turnovers: entry + exit
        turnover = position_capital * 2.0
        statutory = turnover * (STATUTORY_TURNOVER_RATE / 4.0)  # ~0.00875% effective
        gst = ROUNDTRIP_BROKERAGE * GST_RATE  # ₹7.20 GST
        total_fees = ROUNDTRIP_BROKERAGE + slippage + statutory + gst
        return round(total_fees, 2)

    @classmethod
    def calculate_economic_breakeven_price(
        cls,
        entry_price: float,
        shares: int,
        direction: str,
        min_desired_net_profit: float = 0.0
    ) -> float:
        """
        Calculates the true economic break-even exit price.
        Ensures that if the position exits at this price, net P&L is >= min_desired_net_profit.
        
        Gross Required = Total Round-Trip Friction + min_desired_net_profit
        Delta per Share = Gross Required / shares
        BUY:  BE Price = Entry + Delta per Share
        SELL: BE Price = Entry - Delta per Share
        """
        if entry_price <= 0 or shares <= 0:
            spread = round(entry_price * 0.0035, 2)  # 0.35% default fallback
            return round(entry_price + spread if direction.upper() in ("BUY", "BULLISH") else entry_price - spread, 2)

        friction = cls.estimate_roundtrip_friction(entry_price, shares)
        required_gross = friction + max(0.0, min_desired_net_profit)
        delta_per_share = required_gross / float(shares)

        # Enforce minimum 35 bps spread hurdle so small share sizes don't underestimate friction
        min_spread = entry_price * 0.0035
        delta_per_share = max(delta_per_share, min_spread)

        if direction.upper() in ("BUY", "BULLISH"):
            be_px = entry_price + delta_per_share
        else:
            be_px = entry_price - delta_per_share

        return round(be_px, 2)

    @classmethod
    def calculate_economic_hurdle_pct(cls, entry_price: float, shares: int) -> float:
        """
        Calculates the minimum percentage move required just to cover transaction costs.
        """
        if entry_price <= 0 or shares <= 0:
            return 0.35
        friction = cls.estimate_roundtrip_friction(entry_price, shares)
        position_capital = entry_price * shares
        return round((friction / position_capital) * 100.0, 3) if position_capital > 0 else 0.35

    @classmethod
    def is_valid_trading_session(
        cls,
        dt_val: Optional[Any] = None,
        force_override: bool = False
    ) -> Tuple[bool, str]:
        """
        Hard Session Guard:
        Verifies that dt_val is within valid NSE cash-market trading hours (09:15 to 15:30 IST, Mon-Fri).
        Rejects:
        - Weekend entries (Sat/Sun)
        - Pre-market entries (< 09:15 IST)
        - Post-market entries (> 15:30 IST)
        """
        if force_override:
            return True, "SESSION_OVERRIDE_ENABLED"

        if dt_val is None:
            now_dt = datetime.now()
        elif isinstance(dt_val, str):
            clean_s = dt_val.replace("T", " ")[:19]
            try:
                now_dt = datetime.strptime(clean_s, "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    now_dt = datetime.strptime(clean_s[:16], "%Y-%m-%d %H:%M")
                except Exception:
                    now_dt = datetime.now()
        elif isinstance(dt_val, datetime):
            now_dt = dt_val
        else:
            now_dt = datetime.now()

        # 1. Day of Week Guard
        if now_dt.weekday() >= 5:
            day_name = "Saturday" if now_dt.weekday() == 5 else "Sunday"
            return False, f"SESSION_CLOSED_WEEKEND: {day_name} is not a valid trading day"

        cur_t = now_dt.time()

        # 2. Pre-Market Guard
        if cur_t < MARKET_OPEN_TIME:
            return False, f"SESSION_PREMARKET_REJECTED: Time {cur_t} is before market open (09:15 IST)"

        # 3. Post-Market / After-Hours Guard
        if cur_t > MARKET_CLOSE_TIME:
            return False, f"SESSION_AFTERHOURS_REJECTED: Time {cur_t} is after market close (15:30 IST)"

        return True, "ACTIVE_SESSION"

    @classmethod
    def is_entry_cutoff_breached(
        cls,
        dt_val: Optional[Any] = None,
        cutoff_time: Optional[time] = None
    ) -> Tuple[bool, str]:
        """
        Configurable Entry Cutoff Gate:
        Rejects new intraday trade entries after cutoff_time (default 15:00:00 IST).
        Prevents entering positions with insufficient runway before the 15:15/15:30 close.
        """
        if dt_val is None:
            now_dt = datetime.now()
        elif isinstance(dt_val, str):
            clean_s = dt_val.replace("T", " ")[:19]
            try:
                now_dt = datetime.strptime(clean_s, "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    now_dt = datetime.strptime(clean_s[:16], "%Y-%m-%d %H:%M")
                except Exception:
                    now_dt = datetime.now()
        elif isinstance(dt_val, datetime):
            now_dt = dt_val
        else:
            now_dt = datetime.now()

        target_cutoff = cutoff_time or DEFAULT_ENTRY_CUTOFF_TIME
        cur_t = now_dt.time()

        if cur_t >= target_cutoff:
            return True, f"ENTRY_CUTOFF_REJECTED: Current time {cur_t} exceeds operational cutoff ({target_cutoff})"

        return False, "WITHIN_ENTRY_WINDOW"

    @classmethod
    def canonicalize_exit_outcome(cls, raw_outcome: Optional[str], net_pnl: Optional[float] = None) -> str:
        """
        Maps raw exit strings into explicit terminal exit categories:
        - TARGET
        - STOP
        - ECONOMIC_BREAKEVEN
        - PROFIT_LOCK
        - TIME_EXIT
        - SESSION_EXIT
        - MANUAL_EXIT
        - INVALIDATED
        """
        if not raw_outcome:
            return "SESSION_EXIT"

        clean = str(raw_outcome).upper().strip()

        if "TARGET" in clean:
            return "TARGET"
        if "PROFIT_LOCK" in clean:
            return "PROFIT_LOCK"
        if "BREAKEVEN" in clean or "ECONOMIC" in clean:
            if net_pnl is not None and net_pnl < 0:
                # If historical breakeven lost money, categorize transparently
                return "BREAKEVEN_LOSS"
            return "ECONOMIC_BREAKEVEN"
        if "SL" in clean or "STOP" in clean:
            return "STOP"
        if "SQUARE" in clean or "SESSION" in clean or "EOD" in clean:
            return "SESSION_EXIT"
        if "TIME" in clean:
            return "TIME_EXIT"
        if "MANUAL" in clean:
            return "MANUAL_EXIT"
        if "INVALID" in clean:
            return "INVALIDATED"

        return clean

    @classmethod
    def reconcile_trade_accounting(
        cls,
        session_id: str = "default_arena_session",
        conn: Optional[sqlite3.Connection] = None,
        tolerance_rupees: float = 0.50
    ) -> Dict[str, Any]:
        """
        Strict Trade Accounting Reconciliation:
        Reconciles:
        all closed trades = sum of all exit categories
        gross P&L - total fees = net P&L (within tolerance_rupees)
        Ensures zero unexplained rows or math leaks.
        """
        from app.data.historical_data_layer import get_db_path
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            should_close = True

        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, ticker, direction, entry_price, exit_price,
                       gross_pnl_rupees, fees_rupees, net_pnl_rupees, outcome, status
                FROM consensus_game_positions
                WHERE session_id = ? AND status = 'CLOSED'
                ORDER BY id ASC
            """, (session_id,))
            rows = cur.fetchall()

            total_closed = len(rows)
            total_gross = 0.0
            total_fees = 0.0
            total_net = 0.0

            exit_category_counts: Dict[str, int] = {}
            exit_category_gross: Dict[str, float] = {}
            exit_category_net: Dict[str, float] = {}

            discrepancies = []

            for r in rows:
                pid, ticker, direction, entry_px, exit_px, gross, fee, net, outcome, status = r
                gross = float(gross or 0.0)
                fee = float(fee or 0.0)
                net = float(net or 0.0)

                # Row-level math invariance check: gross - fee == net
                row_diff = abs((gross - fee) - net)
                if row_diff > tolerance_rupees:
                    discrepancies.append({
                        "position_id": pid,
                        "ticker": ticker,
                        "gross": gross,
                        "fee": fee,
                        "net": net,
                        "expected_net": round(gross - fee, 2),
                        "delta": round(row_diff, 2)
                    })

                total_gross += gross
                total_fees += fee
                total_net += net

                canonical_cat = cls.canonicalize_exit_outcome(outcome, net_pnl=net)
                exit_category_counts[canonical_cat] = exit_category_counts.get(canonical_cat, 0) + 1
                exit_category_gross[canonical_cat] = round(exit_category_gross.get(canonical_cat, 0.0) + gross, 2)
                exit_category_net[canonical_cat] = round(exit_category_net.get(canonical_cat, 0.0) + net, 2)

            total_gross = round(total_gross, 2)
            total_fees = round(total_fees, 2)
            total_net = round(total_net, 2)
            expected_net = round(total_gross - total_fees, 2)
            portfolio_diff = round(abs(expected_net - total_net), 2)

            is_reconciled = (portfolio_diff <= tolerance_rupees) and (len(discrepancies) == 0)

            return {
                "session_id": session_id,
                "total_closed_trades": total_closed,
                "total_gross_pnl": total_gross,
                "total_fees": total_fees,
                "total_net_pnl": total_net,
                "expected_net_pnl": expected_net,
                "reconciliation_delta": portfolio_diff,
                "is_reconciled": is_reconciled,
                "discrepancies_count": len(discrepancies),
                "discrepancies": discrepancies,
                "exit_categories": {
                    cat: {
                        "count": exit_category_counts[cat],
                        "gross_pnl": exit_category_gross[cat],
                        "net_pnl": exit_category_net[cat]
                    }
                    for cat in sorted(exit_category_counts.keys())
                }
            }
        finally:
            if should_close:
                conn.close()
