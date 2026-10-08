"""
Option Sniffer: Core Constants and Configuration Specifications.

Enforces strict domain rules:
  1. Paper-only operation (zero live orders).
  2. Realistic Upstox / Indian statutory F&O transaction cost modeling.
  3. Controlled ATM strike selection for NIFTY & BANKNIFTY.
  4. Fail-closed liquidity and freshness hurdles.
"""

from typing import Dict, Any

# Underlyings Monitored
UNDERLYING_INSTRUMENTS: Dict[str, str] = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "INDIAVIX": "NSE_INDEX|India VIX",
}

# Strike Parameters
ATM_STRIKE_WINDOW: int = 5  # Monitored: ATM-5 to ATM+5 (11 strikes per expiry)
STRIKE_INTERVALS: Dict[str, float] = {
    "NIFTY": 50.0,
    "BANKNIFTY": 100.0,
}

# Standard Lot Sizes (Contract metadata provides authoritative override)
DEFAULT_LOT_SIZES: Dict[str, int] = {
    "NIFTY": 65,
    "BANKNIFTY": 15,
}

# Signal State Machine Lifecycle
STATE_WATCH = "WATCH"
STATE_QUALIFIED = "QUALIFIED"
STATE_ACTIVE = "ACTIVE"
STATE_PAPER_OPEN = "PAPER_OPEN"
STATE_PAPER_CLOSED = "PAPER_CLOSED"

# Signal Directions
DIRECTION_BUY_CE = "BUY_CE"
DIRECTION_BUY_PE = "BUY_PE"
DIRECTION_NO_TRADE = "NO_TRADE"

# Liquidity & Risk Quality Hurdle Gates
MAX_QUOTE_AGE_MS: int = 15000            # 15s data freshness ceiling
MAX_SPREAD_PCT: float = 3.5              # Maximum bid-ask spread % allowable
MIN_OI: int = 5000                       # Minimum Open Interest
MIN_VOLUME: int = 200                    # Minimum daily volume
MIN_BID_PRICE: float = 5.0               # Minimum bid to avoid illiquid micro-penny contracts
MAX_SINGLE_TRADE_SLIPPAGE_PCT: float = 0.50  # 0.50% execution slippage assumption

# Model Conviction Hurdles (Calibrated to 1.75:1 Convex R:R and Indian Statutory Friction)
MIN_CALIBRATED_PROB: float = 0.44        # 44.0% minimum calibrated net win probability (breakeven is 40.0% at 1.75:1 R:R)
MIN_EXPECTED_NET_RETURN_PCT: float = 2.0 # Minimum 2.0% expected net edge after all friction
SIGNAL_COOLDOWN_SECONDS: int = 300       # 5 minutes minimum cooldown per contract

# Trade Exit Management
MAX_HOLDING_MINUTES: int = 45            # Intraday holding limit (time decay stop)
STOP_LOSS_PCT: float = 20.0              # 20.0% max loss from entry ask
TARGET_GAIN_PCT: float = 35.0            # 35.0% take profit target
TRAILING_STOP_TRIGGER_PCT: float = 15.0  # Activate ratchet trailing stop after +15%
TRAILING_STOP_DISTANCE_PCT: float = 10.0 # Trail 10% below highest peak price

# Versioning
OPTION_MODEL_VERSION = "v1.0-champion-gbm"
OPTION_SIGNAL_VERSION = "v1.0-point-in-time"

# ──────────────────────────────────────────────────────────────────────────────
# REALISTIC INDIAN F&O STATUTORY CHARGE SCHEDULE (UPSTOX BROKERAGE SPECIFICATION)
# ──────────────────────────────────────────────────────────────────────────────
# 1. Brokerage: Flat ₹20 per executed order.
# 2. STT (Securities Transaction Tax): 0.125% on option SELL side turnover.
# 3. Exchange Turnover Fee: 0.0535% of premium turnover (both buy and sell legs).
# 4. SEBI Turnover Charges: ₹10 per crore (0.0001% of turnover).
# 5. GST: 18% on (Brokerage + Exchange Turnover Fee + SEBI Charges).
# 6. Stamp Duty: 0.003% of premium turnover on BUY leg only.
BROKERAGE_PER_ORDER = 20.0
STT_SELL_RATE = 0.00125
EXCHANGE_TURNOVER_RATE = 0.000535
SEBI_TURNOVER_RATE = 0.000001
GST_RATE = 0.18
STAMP_DUTY_BUY_RATE = 0.00003
SLIPPAGE_RATE_PER_LEG = 0.0025  # 0.25% slippage on entry and exit


def calculate_option_trade_costs(
    buy_price: float,
    sell_price: float,
    quantity: int,
    apply_slippage: bool = True
) -> Dict[str, float]:
    """
    Computes exact, realistic transaction friction for an Indian equity option round-trip.
    All figures are in INR (₹).
    """
    buy_premium_turnover = buy_price * quantity
    sell_premium_turnover = sell_price * quantity
    total_turnover = buy_premium_turnover + sell_premium_turnover

    # 1. Brokerage (₹20 entry + ₹20 exit = ₹40 total)
    brokerage = BROKERAGE_PER_ORDER * 2.0

    # 2. STT (0.125% on sell leg)
    stt = sell_premium_turnover * STT_SELL_RATE

    # 3. Exchange Turnover Fee
    exchange_fee = total_turnover * EXCHANGE_TURNOVER_RATE

    # 4. SEBI Charges
    sebi_fee = total_turnover * SEBI_TURNOVER_RATE

    # 5. GST (18% on Brokerage + Exchange Fee + SEBI Fee)
    gst = (brokerage + exchange_fee + sebi_fee) * GST_RATE

    # 6. Stamp Duty (0.003% on buy leg)
    stamp_duty = buy_premium_turnover * STAMP_DUTY_BUY_RATE

    # 7. Slippage (0.25% per leg modeled explicitly)
    slippage = (buy_premium_turnover * SLIPPAGE_RATE_PER_LEG + sell_premium_turnover * SLIPPAGE_RATE_PER_LEG) if apply_slippage else 0.0

    statutory_charges = round(brokerage + stt + exchange_fee + sebi_fee + gst + stamp_duty, 2)
    total_friction = round(statutory_charges + slippage, 2)

    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_fee": round(exchange_fee, 2),
        "sebi_fee": round(sebi_fee, 2),
        "gst": round(gst, 2),
        "stamp_duty": round(stamp_duty, 2),
        "slippage": round(slippage, 2),
        "statutory_charges": statutory_charges,
        "total_friction": total_friction,
    }
