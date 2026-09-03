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
    Aggregates risk across genuine portfolio positions (PAPER_POSITION, LIVE_POSITION)
    to enforce a portfolio-level total heat ceiling.
    
    Scanner recommendations (NOT_A_POSITION) are tracked but contribute 0% heat.
    Research and forward simulation are in separate tables and never contribute.
    """
    from app.data.historical_data_layer import get_db_path
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    
    # Query user-defined heat cap if not specified
    if max_heat_cap_pct is None:
        try:
            cur = conn.execute("SELECT value FROM app_settings WHERE key = 'portfolio_max_heat_cap'")
            row = cur.fetchone()
            max_heat_cap_pct = float(row[0]) if row else 6.0
        except:
            max_heat_cap_pct = 6.0

    try:
        # GENUINE POSITIONS: only PAPER_POSITION and LIVE_POSITION contribute to heat
        df_positions = pd.read_sql_query(
            "SELECT ticker, entry, sl, direction, trade_type, source, position_type "
            "FROM ml_trade_history "
            "WHERE status = 'OPEN' AND position_type IN ('PAPER_POSITION', 'LIVE_POSITION')",
            conn
        )
    except:
        df_positions = pd.DataFrame()

    try:
        # VIRTUAL RECOMMENDATIONS: tracked but 0% heat contribution
        virtual_counts = pd.read_sql_query(
            "SELECT source, COUNT(*) as cnt "
            "FROM ml_trade_history "
            "WHERE status = 'OPEN' AND position_type = 'NOT_A_POSITION' "
            "GROUP BY source",
            conn
        )
    except:
        virtual_counts = pd.DataFrame()
    
    conn.close()

    # Parse virtual recommendation breakdown
    manual_recs = 0
    autopilot_recs = 0
    other_recs = 0
    if not virtual_counts.empty:
        for _, row in virtual_counts.iterrows():
            src = str(row.get('source', '')).upper()
            cnt = int(row.get('cnt', 0))
            if src in ('MANUAL', 'SCANNER'):
                manual_recs += cnt
            elif src == 'AUTOPILOT':
                autopilot_recs += cnt
            else:
                other_recs += cnt

    total_virtual = manual_recs + autopilot_recs + other_recs

    if df_positions.empty:
        actual_positions = 0
        total_risk = 0.0
        heat_pct = 0.0
    else:
        actual_positions = len(df_positions)
        total_risk = 0.0
        for _, row in df_positions.iterrows():
            entry = float(row.get('entry', 0.0))
            sl = float(row.get('sl', 0.0))
            risk_per_trade = abs(entry - sl)
            total_risk += risk_per_trade
        # ~1.5% per genuine open position
        heat_pct = round((actual_positions * 1.5), 1)

    remaining_heat = max(0.0, round(max_heat_cap_pct - heat_pct, 1))

    if heat_pct >= max_heat_cap_pct:
        status = "MAX_REACHED"
        msg = f"Maximum Portfolio Heat ({max_heat_cap_pct}%) reached across {actual_positions} genuine positions. De-risk before adding new exposure."
    elif heat_pct >= (max_heat_cap_pct * 0.75):
        status = "WARNING"
        msg = f"Approaching Portfolio Risk Cap ({heat_pct}% / {max_heat_cap_pct}%)."
    else:
        status = "NORMAL"
        msg = f"Healthy portfolio risk allocation ({heat_pct}% / {max_heat_cap_pct}%)."

    discovery_status = "BLOCKED" if status == "MAX_REACHED" else "ENABLED"

    return {
        "open_positions": actual_positions,
        "actual_positions": actual_positions,
        "virtual_recommendations": total_virtual,
        "manual_recommendations": manual_recs,
        "autopilot_recommendations": autopilot_recs,
        "total_risk_amount": round(total_risk, 2),
        "current_heat_pct": heat_pct,
        "max_heat_cap_pct": max_heat_cap_pct,
        "remaining_heat_pct": remaining_heat,
        "status": status,
        "message": msg,
        "discovery_status": discovery_status,
        "block_reason": msg if status == "MAX_REACHED" else None
    }

