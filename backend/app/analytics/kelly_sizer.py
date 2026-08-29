import sqlite3
import pandas as pd
from typing import Dict, Any

def calculate_kelly_position_size(
    capital: float,
    entry: float,
    sl: float,
    tp1: float,
    win_prob: float,
    kelly_mode: str = "HALF", # 'QUARTER', 'HALF', 'FULL'
    max_risk_cap_pct: float = 5.0
) -> Dict[str, Any]:
    """
    Computes mathematically optimal Fractional Kelly position sizing based on
    the AI model's calibrated win probability and the trade's Risk:Reward ratio.
    """
    risk_per_share = abs(entry - sl)
    reward_per_share = abs(tp1 - entry)
    
    if risk_per_share <= 0:
        return {
            "quantity": 0,
            "allocated_risk_pct": 0.0,
            "risk_amount": 0.0,
            "position_value": 0.0,
            "full_kelly_pct": 0.0,
            "adjusted_kelly_pct": 0.0,
            "reward_risk_ratio": 0.0,
            "is_positive_edge": False
        }

    b = reward_per_share / risk_per_share
    p = max(0.01, min(0.99, win_prob / 100.0 if win_prob > 1 else win_prob))
    q = 1.0 - p

    # Full Kelly Formula: f* = (b*p - q) / b
    full_kelly = (b * p - q) / b
    
    # Kelly fraction multiplier
    fraction_map = {"QUARTER": 0.25, "HALF": 0.50, "FULL": 1.0}
    multiplier = fraction_map.get(kelly_mode.upper(), 0.50)

    if full_kelly <= 0:
        # Edge is non-positive according to Kelly
        adj_kelly = 0.005 # Minimal 0.5% probe
        is_positive_edge = False
    else:
        adj_kelly = full_kelly * multiplier
        is_positive_edge = True

    # Clamp within single trade safety cap (e.g. max 5% portfolio risk)
    clamped_risk_pct = max(0.5, min(max_risk_cap_pct, adj_kelly * 100.0))
    risk_amount = capital * (clamped_risk_pct / 100.0)
    
    # Calculate quantity
    qty = int(risk_amount / risk_per_share)
    max_qty_by_capital = int(capital / entry) if entry > 0 else 0
    qty = max(1, min(qty, max_qty_by_capital))
    
    actual_risk = round(qty * risk_per_share, 2)
    position_val = round(qty * entry, 2)

    return {
        "quantity": qty,
        "allocated_risk_pct": round((actual_risk / capital) * 100.0, 2) if capital > 0 else 0.0,
        "risk_amount": actual_risk,
        "position_value": position_val,
        "full_kelly_pct": round(full_kelly * 100.0, 1),
        "adjusted_kelly_pct": round(clamped_risk_pct, 2),
        "reward_risk_ratio": round(b, 2),
        "is_positive_edge": is_positive_edge,
        "kelly_mode": kelly_mode
    }

def get_portfolio_heat_status(capital: float = 100000.0, max_heat_cap_pct: float = None) -> Dict[str, Any]:
    """
    Aggregates risk across all active open positions in ml_trade_history
    to enforce a portfolio-level total heat ceiling.
    """
    conn = sqlite3.connect('market_data.db', timeout=5.0)
    
    # Query user-defined heat cap if not specified
    if max_heat_cap_pct is None:
        try:
            cur = conn.execute("SELECT value FROM app_settings WHERE key = 'portfolio_max_heat_cap'")
            row = cur.fetchone()
            max_heat_cap_pct = float(row[0]) if row else 6.0
        except:
            max_heat_cap_pct = 6.0

    try:
        df_open = pd.read_sql_query("SELECT ticker, entry, sl, direction, trade_type FROM ml_trade_history WHERE status = 'OPEN'", conn)
    except:
        df_open = pd.DataFrame()
    conn.close()

    if df_open.empty:
        return {
            "open_positions": 0,
            "total_risk_amount": 0.0,
            "current_heat_pct": 0.0,
            "max_heat_cap_pct": max_heat_cap_pct,
            "remaining_heat_pct": max_heat_cap_pct,
            "status": "NORMAL",
            "message": "All portfolio risk capacity available."
        }

    total_risk = 0.0
    for _, row in df_open.iterrows():
        entry = float(row.get('entry', 0.0))
        sl = float(row.get('sl', 0.0))
        risk_per_trade = abs(entry - sl)
        total_risk += risk_per_trade

    # Approximate assuming standard 1-unit lot allocation
    heat_pct = round((len(df_open) * 1.5), 1)  # ~1.5% per open trade
    remaining_heat = max(0.0, round(max_heat_cap_pct - heat_pct, 1))

    if heat_pct >= max_heat_cap_pct:
        status = "MAX_REACHED"
        msg = f"Maximum Portfolio Heat ({max_heat_cap_pct}%) reached across {len(df_open)} open positions. De-risk before adding new exposure."
    elif heat_pct >= (max_heat_cap_pct * 0.75):
        status = "WARNING"
        msg = f"Approaching Portfolio Risk Cap ({heat_pct}% / {max_heat_cap_pct}%)."
    else:
        status = "NORMAL"
        msg = f"Healthy portfolio risk allocation ({heat_pct}% / {max_heat_cap_pct}%)."

    return {
        "open_positions": len(df_open),
        "total_risk_amount": round(total_risk, 2),
        "current_heat_pct": heat_pct,
        "max_heat_cap_pct": max_heat_cap_pct,
        "remaining_heat_pct": remaining_heat,
        "status": status,
        "message": msg
    }
