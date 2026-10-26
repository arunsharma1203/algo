"""
TRADE ECONOMICS & REGULATORY FRICTION ENGINE
=============================================
Itemizes statutory regulatory fees, exchange transaction charges, GST,
brokerage, and execution slippage according to Indian regulatory schedules.
Computes realized net P&L and economic ground-truth labels for ML retraining.
"""

from typing import Dict, Any, Optional, Union
import logging

logger = logging.getLogger(__name__)

# Statutory & Brokerage Schedules (India Equity)
INTRADAY_BROKERAGE_PCT = 0.0600     # 0.03% entry + 0.03% exit (or ₹20/order max)
INTRADAY_STT_PCT = 0.0250           # 0.025% on sell turnover
SWING_STT_PCT = 0.1000              # 0.1% on buy and sell turnover (delivery)
EXCHANGE_TURNOVER_PCT = 0.0069      # NSE ~0.00345% round-trip
SEBI_TURNOVER_PCT = 0.0002          # ₹10 per crore round-trip
STAMP_DUTY_PCT = 0.0030             # 0.003% buy side
GST_RATE = 0.18                     # 18% on (brokerage + exchange + sebi)
DEFAULT_SLIPPAGE_PCT = 0.0800       # 4 bps entry + 4 bps exit execution drag


def compute_trade_friction_pct(
    entry_price: float = 100.0,
    exit_price: float = 100.0,
    trade_type: str = "INTRADAY",
    slippage_pct: Optional[float] = None
) -> Dict[str, Any]:
    """
    Computes an itemized breakdown of transaction friction and total round-trip percentage.
    """
    tt = str(trade_type or "INTRADAY").upper().strip()
    is_swing = "SWING" in tt or "DELIVERY" in tt

    brokerage = INTRADAY_BROKERAGE_PCT
    stt = SWING_STT_PCT if is_swing else INTRADAY_STT_PCT
    exchange = EXCHANGE_TURNOVER_PCT
    sebi = SEBI_TURNOVER_PCT
    stamp = STAMP_DUTY_PCT
    gst = round((brokerage + exchange + sebi) * GST_RATE, 4)
    slippage = DEFAULT_SLIPPAGE_PCT if slippage_pct is None else float(slippage_pct)

    total_pct = round(brokerage + stt + exchange + sebi + stamp + gst + slippage, 4)

    return {
        "brokerage_pct": brokerage,
        "stt_pct": stt,
        "exchange_pct": exchange,
        "gst_pct": gst,
        "sebi_pct": sebi,
        "stamp_pct": stamp,
        "slippage_pct": slippage,
        "total_friction_pct": total_pct,
        "is_swing": is_swing
    }


def compute_realized_net_pnl(trade: Union[Dict[str, Any], Any]) -> Dict[str, Any]:
    """
    Computes realized economic net P&L and assigns a ground-truth economic label.

    Returns:
        {
            "gross_pnl_pct": float,
            "net_pnl_pct": float,
            "friction_pct": float,
            "friction_breakdown": dict,
            "economic_outcome": 'NET_PROFITABLE' | 'NET_LOSING' | 'NEUTRAL' | 'UNRESOLVED',
            "binary_label": 1 | 0 | None,
            "usable_for_training": bool
        }
    """
    if hasattr(trade, "to_dict"):
        t_dict = trade.to_dict()
    elif isinstance(trade, dict):
        t_dict = trade
    else:
        t_dict = dict(trade)

    status = str(t_dict.get("status") or "").upper().strip()
    outcome = str(t_dict.get("outcome") or "").upper().strip()
    direction = str(t_dict.get("direction") or "BUY").upper().strip()
    trade_type = str(t_dict.get("trade_type") or "INTRADAY").upper().strip()

    entry = t_dict.get("entry") or t_dict.get("entry_price")
    exit_p = t_dict.get("exit_price")

    # Guard 1: Unresolved trades cannot be used for empirical labeling
    if status != "CLOSED" or entry is None or exit_p is None:
        return {
            "gross_pnl_pct": 0.0,
            "net_pnl_pct": 0.0,
            "friction_pct": 0.0,
            "friction_breakdown": {},
            "economic_outcome": "UNRESOLVED",
            "binary_label": None,
            "usable_for_training": False
        }

    try:
        entry = float(entry)
        exit_p = float(exit_p)
    except (ValueError, TypeError):
        return {
            "gross_pnl_pct": 0.0,
            "net_pnl_pct": 0.0,
            "friction_pct": 0.0,
            "friction_breakdown": {},
            "economic_outcome": "UNRESOLVED",
            "binary_label": None,
            "usable_for_training": False
        }

    if entry <= 0:
        return {
            "gross_pnl_pct": 0.0,
            "net_pnl_pct": 0.0,
            "friction_pct": 0.0,
            "friction_breakdown": {},
            "economic_outcome": "UNRESOLVED",
            "binary_label": None,
            "usable_for_training": False
        }

    # Directional calculation
    is_long = direction in ("BUY", "BULLISH", "LONG", "1")
    if is_long:
        gross_pnl_pct = ((exit_p / entry) - 1.0) * 100.0
    else:
        gross_pnl_pct = (1.0 - (exit_p / entry)) * 100.0

    friction = compute_trade_friction_pct(entry_price=entry, exit_price=exit_p, trade_type=trade_type)
    friction_pct = friction["total_friction_pct"]
    net_pnl_pct = round(gross_pnl_pct - friction_pct, 4)

    # Classify outcome
    if outcome == "BREAKEVEN HIT" or abs(net_pnl_pct) < 0.05:
        economic_outcome = "NEUTRAL"
        binary_label = None
        usable_for_training = False
    elif net_pnl_pct > 0.0:
        economic_outcome = "NET_PROFITABLE"
        binary_label = 1
        usable_for_training = True
    else:
        economic_outcome = "NET_LOSING"
        binary_label = 0
        usable_for_training = True

    return {
        "gross_pnl_pct": round(gross_pnl_pct, 4),
        "net_pnl_pct": net_pnl_pct,
        "friction_pct": friction_pct,
        "friction_breakdown": friction,
        "economic_outcome": economic_outcome,
        "binary_label": binary_label,
        "usable_for_training": usable_for_training
    }
