"""
Option Sniffer Advance: Institutional Dual-Book Constants, Statutory Cost Models & Regime Parameters.

STRICT SANDBOX INVARIANT:
  - 100% Paper-Only Execution (SIMULATION_ONLY = True).
  - Zero live broker order hooks.
  - Uses isolated SQLite tables (`option_sniffer_advance_*`).
  - Zero additional Upstox REST API calls (reads shared in-memory OptionStreamManager feed).
"""

from datetime import datetime
from typing import Dict, Any, Optional

from app.analytics.option_sniffer.option_constants import (
    DEFAULT_LOT_SIZES,
    STRIKE_INTERVALS,
    UNDERLYING_INSTRUMENTS,
    calculate_option_trade_costs,
    is_option_trading_window,
    is_option_entry_window,
    to_ist_naive,
    to_ist_datetime,
)

# ── SANDBOX SAFETY INVARIANTS ────────────────────────────────────────────────
SIMULATION_ONLY: bool = True
LIVE_ORDER_PLACEMENT_ENABLED: bool = False
ADVANCE_ENGINE_VERSION: str = "ADVANCE_DUAL_BOOK_V1.0"

# ── DUAL-BOOK STRATEGY MODES & DIRECTIONS ────────────────────────────────────
BOOK_LONG_BUY: str = "LONG_BOOK_BUY"
BOOK_SHORT_SELL: str = "SHORT_BOOK_SELL"

DIRECTION_BUY_CE: str = "BUY_CE"
DIRECTION_BUY_PE: str = "BUY_PE"
DIRECTION_SELL_CE: str = "SELL_CE"
DIRECTION_SELL_PE: str = "SELL_PE"
DIRECTION_NO_TRADE: str = "NO_TRADE"

STRATEGY_WAVE1_GAMMA_SNIPER: str = "WAVE_1_GAMMA_SNIPER"
STRATEGY_CONTRACTION_THETA_SELLER: str = "CONTRACTION_THETA_SELLER"
STRATEGY_WAVE2_EXHAUSTION_SELLER: str = "WAVE_2_EXHAUSTION_SELLER"
STRATEGY_DIAGONAL_OTM_WING_SELLER: str = "DIAGONAL_OTM_WING_SELLER"

# ── BOOK 1: LONG CONVEXITY SNIPER (1 LOT BUY) PARAMETERS ─────────────────────
LONG_BASE_LOTS: int = 1
LONG_STOP_LOSS_PCT: float = 20.0
LONG_TARGET_GAIN_PCT: float = 35.0
LONG_MAX_SINGLE_TRADE_LOSS_INR: float = 2200.0
LONG_EARLY_BREAKEVEN_TRIGGER_PCT: float = 15.0
LONG_EARLY_BREAKEVEN_FLOOR_PCT: float = 6.0

# 4-Tier Uncapped Chandelier Trailing Stop (Long Book)
LONG_CHANDELIER_TIER1_TRIGGER_PCT: float = 15.0
LONG_CHANDELIER_TIER1_DIST_PCT: float = 8.0
LONG_CHANDELIER_TIER2_TRIGGER_PCT: float = 22.0
LONG_CHANDELIER_TIER2_DIST_PCT: float = 6.0
LONG_CHANDELIER_TIER3_TRIGGER_PCT: float = 30.0
LONG_CHANDELIER_TIER3_DIST_PCT: float = 4.5
LONG_CHANDELIER_TIER3_5_DIST_PCT: float = 5.5
LONG_CHANDELIER_TIER4_TRIGGER_PCT: float = 60.0
LONG_CHANDELIER_TIER4_DIST_PCT: float = 4.0
LONG_TARGET_LOCK_FLOOR_OFFSET_PCT: float = 1.0

# ── BOOK 2: SHORT THETA / VRP / EXHAUSTION HARVESTER (1 LOT SELL) PARAMETERS ─
SHORT_BASE_LOTS: int = 1
SHORT_HARD_STOP_LOSS_PCT: float = 16.0          # Capped max % rise in option premium
SHORT_MAX_SINGLE_TRADE_LOSS_INR: float = 1800.0 # Capped max ₹ loss per 1-lot short trade
SHORT_MIN_ENTRY_BID_PRICE: float = 35.0         # Never short tiny penny options with tail gamma risk
SHORT_MAX_ITM_STRIKES: float = 1.2              # Never short deep ITM (> 1.2 strikes ITM) unless ER <= 0.15

# 4-Tier Reverse-Chandelier Premium Decay Ratchet (Short Book)
# Tracks Short MFE % = ((Entry_Bid - Trough_Ask) / Entry_Bid) * 100
# Locks ceiling stop downward above Trough_Ask so winning decay trades run uncapped toward ₹5-₹15!
SHORT_EARLY_COST_LOCK_TRIGGER_PCT: float = 10.0  # At +10% decay, lock ceiling at Entry_Bid * (1 - 0.04)
SHORT_EARLY_COST_LOCK_FLOOR_PCT: float = 4.0     # Guarantees +4.0% gross decay (covers all charges + slippage)
SHORT_REV_CHANDELIER_TIER1_TRIGGER_PCT: float = 14.0
SHORT_REV_CHANDELIER_TIER1_DIST_PCT: float = 5.5 # Guarantees >= +8.5% decay locked
SHORT_REV_CHANDELIER_TIER2_TRIGGER_PCT: float = 22.0
SHORT_REV_CHANDELIER_TIER2_DIST_PCT: float = 4.5 # Guarantees >= +17.5% decay locked
SHORT_REV_CHANDELIER_TIER3_TRIGGER_PCT: float = 35.0
SHORT_REV_CHANDELIER_TIER3_DIST_PCT: float = 3.5 # Guarantees >= +31.5% decay locked
SHORT_REV_CHANDELIER_TIER4_TRIGGER_PCT: float = 50.0
SHORT_REV_CHANDELIER_TIER4_DIST_PCT: float = 2.5 # Rides full theta/IV crush collapse up to +85%+

# ── DUAL-BOOK REGIME ROUTER THRESHOLDS (CAUSAL POINT-IN-TIME) ────────────────
MIN_CAUSAL_WARMUP_BARS: int = 28                 # 09:15 -> 09:43 IST (28 completed 1m bars for Wilder ADX(14))
CONTRACTION_ATR_RATIO_MAX: float = 0.85          # ATR(5m)/ATR(20m) <= 0.85 indicates volatility contraction
CONTRACTION_ER_MAX: float = 0.40                 # Kaufman ER < 0.40 during contraction favors Short Theta
POST_HARVEST_EXHAUSTION_ATR_RATIO_MAX: float = 0.93
POST_HARVEST_EXHAUSTION_ER_MAX: float = 0.20
POST_HARVEST_COOLDOWN_SECONDS: int = 3600        # 60m window after Wave-1 >= +15% MFE winner
DIAGONAL_MIN_LONG_ELAPSED_SECONDS: int = 480     # Wait >= 8m after Wave-1 Long entry before fading far-OTM wing
DIAGONAL_MIN_OTM_STRIKES: float = 1.5            # Candidate strike > 1.5 strikes OTM while Long ATM is open

# ── PORTFOLIO & CIRCUIT BREAKER SAFEGUARDS ───────────────────────────────────
MAX_LONG_POSITIONS_PER_UNDERLYING: int = 1
MAX_SHORT_POSITIONS_PER_UNDERLYING: int = 1
MAX_TOTAL_OPEN_TRADES: int = 4
MAX_DAILY_COMBINED_LOSS_INR: float = 3500.0
MAX_CONSECUTIVE_LOSSES: int = 2
INTROSPECTION_PAUSE_MINUTES: int = 30
MAX_HOLDING_MINUTES: int = 45


def calculate_short_option_trade_costs(
    sell_entry_bid: float,
    buy_exit_ask: float,
    quantity: int,
    apply_slippage: bool = True,
    trade_date: Optional[str] = None,
    entry_bid: Optional[float] = None,
    entry_ask: Optional[float] = None,
    exit_bid: Optional[float] = None,
    exit_ask: Optional[float] = None,
) -> Dict[str, float]:
    """
    Computes exact Indian F&O Upstox statutory charges & slippage for a SHORT option trade:
      - Open Short: SELL at BID (`sell_entry_bid`)
      - Close Short: BUY at ASK (`buy_exit_ask`)
      - Gross PnL = (sell_entry_bid - buy_exit_ask) * quantity
      - Statutory charges: ₹20/order x 2 = ₹40 brokerage, STT on SELL leg,
        Stamp Duty on BUY leg, NSE Exchange Txn on both legs, SEBI charges, 18% GST.
    """
    base = calculate_option_trade_costs(
        buy_price=buy_exit_ask,
        sell_price=sell_entry_bid,
        quantity=quantity,
        apply_slippage=apply_slippage,
        trade_date=trade_date,
        buy_bid=exit_bid or buy_exit_ask,
        buy_ask=exit_ask or buy_exit_ask,
        sell_bid=entry_bid or sell_entry_bid,
        sell_ask=entry_ask or sell_entry_bid,
    )
    gross_pnl = round((sell_entry_bid - buy_exit_ask) * quantity, 2)
    net_pnl = round(gross_pnl - base["total_friction"], 2)
    estimated_span_margin = round(max(sell_entry_bid * quantity * 2.5, 45000.0), 2)
    return {
        **base,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "estimated_span_margin": estimated_span_margin,
        "return_on_premium_pct": round((net_pnl / (sell_entry_bid * quantity) * 100.0), 2)
        if (sell_entry_bid * quantity) > 0
        else 0.0,
        "return_on_margin_pct": round((net_pnl / estimated_span_margin * 100.0), 2)
        if estimated_span_margin > 0
        else 0.0,
    }

