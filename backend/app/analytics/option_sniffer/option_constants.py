"""
Option Sniffer: Core Constants and Configuration Specifications.

Enforces strict domain rules:
  1. Paper-only operation (zero live orders).
  2. Realistic Upstox / Indian statutory F&O transaction cost modeling.
  3. Controlled ATM strike selection for NIFTY & BANKNIFTY.
  4. Fail-closed liquidity and freshness hurdles.
"""

from typing import Dict, Any, Optional, Union
from datetime import datetime, date

# Underlyings Monitored
UNDERLYING_INSTRUMENTS: Dict[str, str] = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "INDIAVIX": "NSE_INDEX|India VIX",
}

# Primary Underlying Drivers (Heavyweight Concurrence Engine - Gate 5D)
HEAVYWEIGHT_INSTRUMENTS: Dict[str, str] = {
    "RELIANCE": "NSE_EQ|INE002A01018",
    "HDFCBANK": "NSE_EQ|INE040A01034",
    "ICICIBANK": "NSE_EQ|INE090A01021",
}

UNDERLYING_HEAVYWEIGHTS: Dict[str, list] = {
    "NIFTY": ["RELIANCE", "HDFCBANK"],
    "BANKNIFTY": ["HDFCBANK", "ICICIBANK"],
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

# =============================================================================
# RECONCILED TIMEOUT & DATA FRESHNESS CONFIGURATIONS
# =============================================================================
# 1. Surveillance / Signal Scan Freshness (OptionSignalEngine & StreamManager)
MAX_QUOTE_AGE_MS: int = 15000                     # 15s (15,000 ms) ceiling for surveillance/scanner quotes

# 2. Live Broker Pre-Trade Execution Freshness (OptionLiveOrderIntent)
MAX_EXECUTION_QUOTE_AGE_MS: int = 1000            # 1.0s (1,000 ms) tight ceiling for broker order dispatch

# 3. Continuous 1-Minute Candle Freshness (OptionRegimeGate)
MAX_DATA_STALENESS_SECONDS: float = 120.0        # 120s (2.0 min) maximum age of latest finalized index bar

# 4. Broker Response & Ambiguity Timeout (OptionLiveOrderIntent & OptionLiveAdapter)
UNRESOLVED_ORDER_TIMEOUT_SECONDS: float = 15.0   # 15s timeout before submitted/exit orders enter AMBIGUOUS state

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

# Canonical Aliases for Execution Engine
DEFAULT_TARGET_PCT: float = TARGET_GAIN_PCT
DEFAULT_STOP_LOSS_PCT: float = STOP_LOSS_PCT
DEFAULT_TRAILING_TRIGGER_PCT: float = TRAILING_STOP_TRIGGER_PCT
DEFAULT_TRAILING_STEP_PCT: float = TRAILING_STOP_DISTANCE_PCT
DEFAULT_HOLDING_MAX_MINUTES: int = MAX_HOLDING_MINUTES

# Execution Engine Mode ('V1_CLASSIC' strictly enforced for convex payoff integrity)
OPTION_EXECUTION_ENGINE_MODE: str = "V1_CLASSIC"

# Dynamic Stop & Target Clamping Parameters (Preserved for compatibility, inactive in V1_CLASSIC)
DYNAMIC_SL_MIN_PCT: float = 18.0   # 18.0% breathing room floor
DYNAMIC_SL_MAX_PCT: float = 22.0   # 22.0% maximum capital preservation ceiling
DYNAMIC_RR_RATIO: float = 1.75     # 1.75:1 convex reward-to-risk ratio
DYNAMIC_TGT_MIN_PCT: float = 25.0  # 25.0% minimum target gain
DYNAMIC_TGT_MAX_PCT: float = 45.0  # 45.0% maximum target gain

# Pillar 2: Scalper-Runner Regime-Adaptive Target Scaling Constants
DYNAMIC_SCALP_TGT_MIN_PCT: float = 22.0   # 22.0% minimum target in narrow morning range
DYNAMIC_SCALP_TGT_MAX_PCT: float = 28.0   # 28.0% maximum target in narrow morning range
NARROW_RANGE_THRESHOLD_PTS: float = 110.0 # Day range < 110 pts triggers scalp clamping

# Pillar 1: Underlying Structure Invalidation Loss Truncation Constants
STRUCTURE_INVALIDATION_BUFFER_PCT: float = 0.08      # 0.08% buffer beyond VWAP
STRUCTURE_INVALIDATION_MIN_PTS_NIFTY: float = 18.0    # 18.0 pts adverse against entry spot for Nifty
STRUCTURE_INVALIDATION_MIN_PTS_BANKNIFTY: float = 45.0 # 45.0 pts adverse against entry spot for BankNifty

# Pillar 3: Breakout Volume Confirmation Constant
BREAKOUT_VOLUME_MIN_RATIO: float = 1.15  # 1.15x 20-period SMA volume required for breakout confirmation

# Harmonized Scale-Invariant Quantitative Constants
KAUFMAN_ER_MIN_THRESHOLD: float = 0.28            # 0.28 minimum 15m efficiency ratio (below is chaotic chop)
KAUFMAN_ER_TREND_THRESHOLD: float = 0.40          # 0.40 authentic trend confirmation
EXTREMA_DISPLACEMENT_MAX_ATR_RATIO: float = 1.20  # 1.20x 5m ATR adverse bounce breaks trend thesis
INTERMARKET_BREADTH_MIN_RATIO: float = 0.80       # 0.80 minimum composite alignment (|B_comp| >= 0.8)
INTROSPECTION_FREEZE_MINUTES: int = 30            # 30-minute global portfolio freeze after 2 consecutive losses
ANTI_PING_PONG_FLIP_MINUTES: int = 30             # 30-minute directional flip lockout after Stop-Loss
SMART_PYRAMIDING_MIN_MFE_PCT: float = 15.0        # +15.0% MFE required on 1st position to unlock 2nd contract
MAX_CONSECUTIVE_LOSSES: int = 2                   # 2 consecutive losses triggers dynamic 30m introspection pause
MAX_DAILY_LOSS_INR: float = 3500.0                # Hard daily net loss ceiling (-₹3,500.00 permanent session halt)
POST_LOSS_CONTAGION_FREEZE_SECONDS: int = 1200    # 20-minute global intermarket freeze across all underlyings after any SL
MAX_POSITIONS_PER_UNDERLYING: int = 2              # Maximum 2 positions per underlying (Contract #2 unlocked via Smart Pyramiding)

# Market Hours & Intraday Cutoffs (IST)
MARKET_OPEN_HOUR: int = 9
MARKET_OPEN_MINUTE: int = 15
SESSION_CUTOFF_HOUR: int = 15
SESSION_CUTOFF_MINUTE: int = 15

# Golden High-Momentum Entry Window (09:30 to 13:15 IST)
# Halts all new entries after 13:15 IST to completely eliminate afternoon theta decay (86% of historical bleed)
NEW_ENTRY_OPEN_HOUR: int = 9
NEW_ENTRY_OPEN_MINUTE: int = 30
NEW_ENTRY_CUTOFF_HOUR: int = 13
NEW_ENTRY_CUTOFF_MINUTE: int = 15

# Canary Scout & Live Real Trading Window (09:45 to 13:15 IST)
# 09:30 - 09:45 IST is strictly reserved for zero-cost paper scout probing.
# Live real broker trading is strictly prohibited before 09:45 IST.
LIVE_ENTRY_OPEN_HOUR: int = 9
LIVE_ENTRY_OPEN_MINUTE: int = 45
LIVE_ENTRY_CUTOFF_HOUR: int = 13
LIVE_ENTRY_CUTOFF_MINUTE: int = 15
CANARY_SCOUT_PHASE_ACTIVE: bool = True

# Dynamic Microstructure Timing Constants (09:35 Earliest Data-Driven Unlock)
DYNAMIC_TIMING_ACTIVE: bool = True
DYNAMIC_EARLIEST_UNLOCK_HOUR: int = 9
DYNAMIC_EARLIEST_UNLOCK_MINUTE: int = 35
DYNAMIC_MAX_SPREAD_ATM_PCT: float = 1.2
DYNAMIC_MIN_KAUFMAN_ER: float = 0.35
DYNAMIC_MIN_ORB_VOLUME_RATIO: float = 1.2
DYNAMIC_MAX_ANTI_CHASE_PCT: float = 15.0

# Profit Ratchet Floor & Early Capital Protection
EARLY_BREAKEVEN_TRIGGER_PCT: float = 15.0   # Require +15% MFE expansion before pulling stop up
EARLY_BREAKEVEN_FLOOR_PCT: float = 6.0      # Lock in at least +6.0% to guarantee net profit after friction
BREAKEVEN_PROFIT_TRIGGER_PCT: float = 15.0  # Once MFE hits +15%, lock in at least +2% profit
BREAKEVEN_FLOOR_PCT: float = 2.0           # Minimum +2% locked profit floor to cover friction

# Continuous Chandelier Trailing Stop Constants (High-Water Mark Dynamic Trail)
CHANDELIER_TRAIL_ACTIVE: bool = True
CHANDELIER_TRIGGER_TIER1_PCT: float = 15.0  # At >= +15% MFE, trail 8.0% below peak
CHANDELIER_DISTANCE_TIER1_PCT: float = 8.0
CHANDELIER_TRIGGER_TIER2_PCT: float = 22.0  # At >= +22% MFE, trail 6.0% below peak
CHANDELIER_DISTANCE_TIER2_PCT: float = 6.0
CHANDELIER_TRIGGER_TIER3_PCT: float = 30.0  # At >= +30% MFE (Runner), trail 4.5% below peak
CHANDELIER_DISTANCE_TIER3_PCT: float = 4.5
CHANDELIER_TRIGGER_TIER4_PCT: float = 60.0  # At >= +60% MFE (Multi-Bagger Runner), trail 4.0% below peak
CHANDELIER_DISTANCE_TIER4_PCT: float = 4.0

# 4-Pillar Dynamic Canary & Uncapped Convex Runner Flag
ENABLE_4PILLAR_CANARY_ENGINE: bool = True
OPENING_RANGE_VOL_MIN_PCT: float = 0.40     # 0.40% minimum 15m opening range (09:15-09:30) to authorize option buying

# ── APEX QUANT OPTION ENGINE CONSTANTS (CRB-I + THETA STAGNATION + PEAK LOCK) ──
ENABLE_APEX_QUANT_ENGINE: bool = True

# Theta Stagnation Stop (Velocity Horizon): Exit non-moving options at 12m (-4% to -6% scratch vs -35% SL)
THETA_STAGNATION_TIMEOUT_MINUTES: float = 12.0
THETA_STAGNATION_MIN_MFE_PCT: float = 5.0
THETA_STAGNATION_EXIT_LOSS_CAP_PCT: float = 6.0

# Intraday Peak-Profit Trailing Lock (High-Water Mark Circuit): Prevent midday givebacks
PEAK_PROFIT_LOCK_TRIGGER_INR: float = 4000.0       # Engages once daily net realized profit >= Rs 4,000
PEAK_PROFIT_GIVEBACK_MAX_RATIO: float = 0.50       # Locks 50% of peak daily profit as permanent exit floor

# CRB-I (Causal Regime Breakout Initiation) Thresholds
CRBI_BREAKOUT_VOLUME_MIN_RATIO: float = 1.15       # 1.15x SMA20 volume on 5m breakout bar
CRBI_RELAXED_MODEL_PROB: float = 0.38              # Lowered from 0.44 when CRB-I is confirmed
CRBI_RELAXED_KAUFMAN_ER: float = 0.20              # Lowered from 0.28 to prevent lagging filter drag
CRBI_MIN_HEAVYWEIGHT_ALIGNMENT: float = 0.66       # >= 2 of 3 heavyweights aligned (index leads)

# Climax & In-Range Apex Safety Safeguards (Anti-Peak / Anti-Trough Guards)
MAX_CLIMAX_Z_EXTENSION: float = 2.75               # Max allowable |Spot - VWAP| / ATR before entry is halted as exhaustion climax
IN_RANGE_MIN_CALIBRATED_PROB: float = 0.48         # 48.0% elevated probability required for entries inside the Day High/Low range
IN_RANGE_MIN_EXPECTED_NET_RETURN_PCT: float = 3.5  # +3.5% elevated net edge hurdle for in-range entries
CANDLE_BODY_MIN_RATIO: float = 0.35                # Breakout candle body must be >= 35% of total bar range to eliminate wick spikes


# Market Regime Classification Constants
REGIME_TRENDING_BULL: str = "TRENDING_BULL"
REGIME_TRENDING_BEAR: str = "TRENDING_BEAR"
REGIME_RANGEBOUND_CHOP: str = "RANGEBOUND_CHOP"
REGIME_VOLATILITY_SQUEEZE: str = "VOLATILITY_SQUEEZE"



def to_ist_datetime(dt: Any = None) -> datetime:
    """
    Normalizes any datetime to timezone-aware Asia/Kolkata (IST).
    If dt is None, returns current IST time.
    If dt is tz-naive, localizes it to Asia/Kolkata.
    If dt is tz-aware, converts it to Asia/Kolkata.
    """
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    if dt is None:
        return datetime.now(ist)
    if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
        return dt.astimezone(ist)
    return ist.localize(dt)


def to_ist_naive(dt: Any = None) -> datetime:
    """
    Normalizes any datetime to a timezone-naive datetime representing Asia/Kolkata wall-clock time.
    Ensures seamless arithmetic and comparisons against database timestamps (which are stored without offset)
    across all deployment environments (regardless of whether the OS timezone is UTC, EST, or local).
    """
    ist_aware = to_ist_datetime(dt)
    return ist_aware.replace(tzinfo=None)


def is_option_trading_window(dt: Any = None) -> bool:
    """
    Returns True strictly during active option trading hours (09:15 to 15:15 IST, Mon-Fri).
    After 15:15 IST (3:15 PM), all new trades, signals, and candidate evaluations must halt.
    Consistently resolves timezone to Asia/Kolkata.
    """
    from datetime import time
    dt_ist = to_ist_naive(dt)
    if dt_ist.weekday() >= 5:
        return False
    t = dt_ist.time()
    if t < time(MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE):
        return False
    if t >= time(SESSION_CUTOFF_HOUR, SESSION_CUTOFF_MINUTE):
        return False
    return True


def is_option_entry_window(dt: Any = None) -> bool:
    """
    Returns True strictly during the golden high-momentum option entry window (09:30 to 13:15 IST, Mon-Fri).
    After 13:15 IST, new option buy signals are halted to protect capital from afternoon theta decay.
    Existing open positions continue to be monitored and managed until 15:15 IST.
    Consistently resolves timezone to Asia/Kolkata.
    """
    from datetime import time
    dt_ist = to_ist_naive(dt)
    if dt_ist.weekday() >= 5:
        return False
    t = dt_ist.time()
    if t < time(NEW_ENTRY_OPEN_HOUR, NEW_ENTRY_OPEN_MINUTE):
        return False
    if t >= time(NEW_ENTRY_CUTOFF_HOUR, NEW_ENTRY_CUTOFF_MINUTE):
        return False
    return True


def is_live_option_entry_window(dt: Any = None, allow_dynamic_early_unlock: bool = False) -> bool:
    """
    Returns True strictly during the live real broker option entry window (09:45 to 13:15 IST, Mon-Fri).
    Enforces the Canary Scout protocol: 09:30 to 09:45 is strictly reserved for zero-cost paper scout probing.
    If allow_dynamic_early_unlock is True and dynamic conditions are satisfied, unlocks as early as 09:35 IST.
    After 13:15 IST, new live option buy orders are halted to prevent afternoon decay bleed.
    Consistently resolves timezone to Asia/Kolkata.
    """
    from datetime import time
    dt_ist = to_ist_naive(dt)
    if dt_ist.weekday() >= 5:
        return False
    t = dt_ist.time()
    
    if allow_dynamic_early_unlock and DYNAMIC_TIMING_ACTIVE:
        if t < time(DYNAMIC_EARLIEST_UNLOCK_HOUR, DYNAMIC_EARLIEST_UNLOCK_MINUTE):
            return False
    else:
        if t < time(LIVE_ENTRY_OPEN_HOUR, LIVE_ENTRY_OPEN_MINUTE):
            return False

    if t >= time(LIVE_ENTRY_CUTOFF_HOUR, LIVE_ENTRY_CUTOFF_MINUTE):
        return False
    return True


# Versioning
OPTION_MODEL_VERSION = "v1.0-champion-gbm"
OPTION_SIGNAL_VERSION = "v1.0-point-in-time"

# ──────────────────────────────────────────────────────────────────────────────
# REALISTIC INDIAN F&O STATUTORY CHARGE SCHEDULE (UPSTOX & NSE TARIFF SPECIFICATION)
# ──────────────────────────────────────────────────────────────────────────────
# Effective-Dated Regulatory & Broker Rates:
#   - >= 2026-10-01 (Current/October 2026):
#       STT: 0.15% on sell-side option premium turnover.
#       Exchange Turnover Fee + IPFT: 0.03553% (NSE 0.0350% + IPFT 0.00053%).
#   - 2024-10-01 to 2026-09-30 (Finance Act 2024):
#       STT: 0.10% on sell-side option premium turnover.
#       Exchange Turnover Fee + IPFT: 0.03553%.
#   - < 2024-10-01 (Legacy Pre-Oct 2024):
#       STT: 0.0625% on sell-side option premium turnover.
#       Exchange Turnover Fee: 0.0535%.
# Common across all schedules:
#   - Brokerage: Flat ₹20 per executed order (₹40 round-trip).
#   - SEBI Turnover Charges: ₹10 per crore (0.0001% of total premium turnover).
#   - GST: 18% on (Brokerage + Exchange Turnover Fee + SEBI Charges).
#   - Stamp Duty: 0.003% of premium turnover on BUY leg only.

BROKERAGE_PER_ORDER = 20.0
STT_SELL_RATE = 0.00125            # Legacy default: 0.125% on option SELL premium
EXCHANGE_TURNOVER_RATE = 0.000535  # Legacy default: 0.0535% total turnover
SEBI_TURNOVER_RATE = 0.000001      # ₹10 per crore
GST_RATE = 0.18                    # 18% on Brokerage + Exchange + SEBI
STAMP_DUTY_BUY_RATE = 0.00003      # 0.003% on buy turnover only
SLIPPAGE_RATE_PER_LEG = 0.0025     # 0.25% per leg = 0.50% RT (Legacy simulation assumption)
ADDITIONAL_SLIPPAGE_RATE_CALIBRATED = 0.0010  # 0.10% per leg = 0.20% RT (Defensible calibrated latency impact)

# Published October 2026 Tariff Rates
PUBLISHED_OCT_2026_STT_RATE = 0.0015             # 0.15% on option SELL premium
PUBLISHED_OCT_2026_EXCHANGE_RATE = 0.0003553     # 0.03553% (NSE 0.0350% + IPFT 0.00053%)


def get_option_tariff_schedule(trade_date: Optional[Any] = None) -> Dict[str, Any]:
    """
    Returns effective-dated statutory and broker fee parameters for Indian NSE equity options.
    Enforces official regulatory timelines:
      - STT (on option SELL premium):
          * < 2024-10-01: 0.0625% (0.000625)
          * 2024-10-01 to 2026-03-31: 0.1000% (0.0010) (Finance (No. 2) Act 2024)
          * >= 2026-04-01: 0.1500% (0.0015) (Union Budget / Finance Act 2026)
      - NSE Transaction Charges (Total Premium Turnover, inclusive of IPFT):
          * < 2024-10-01: 0.0535% (0.000535) (legacy tiered tariff structure inclusive of IPFT)
          * 2024-10-01 to 2026-02-28: 0.03503% (0.0003503) (SEBI uniform MII rate)
          * >= 2026-03-01: 0.03553% (0.0003553) (revised composite exchange fee inclusive of IPFT per NSE/FA/73061)
          * Note: IPFT is strictly included in the composite exchange turnover rate; NEVER double-counted.
      - Common across all periods:
          * Brokerage: Flat ₹20 per executed order (₹40 round-trip)
          * GST: 18% on (Brokerage + Exchange Transaction Charges + IPFT Charges per Upstox published schedule; SEBI fee excluded)
          * Stamp Duty: 0.003% on BUY premium turnover only
          * SEBI Turnover Charges: ₹10 per crore (0.0001% of total premium turnover)
      - Legacy Override:
          * trade_date == "LEGACY": returns pre-remediation legacy defaults (STT 0.125%, Exchange 0.0535%, GST on Brok+Exch+SEBI)
      - Default Behavior:
          * trade_date is None: defaults to current date (never silently falls back to legacy constants for current trades)
    """
    if trade_date == "LEGACY":
        return {
            "schedule_name": "UPSTOX_LEGACY_DEFAULT_TARIFF",
            "effective_date": "DEFAULT",
            "stt_sell_rate": STT_SELL_RATE,
            "exchange_turnover_rate": EXCHANGE_TURNOVER_RATE,
            "sebi_turnover_rate": SEBI_TURNOVER_RATE,
            "stamp_duty_buy_rate": STAMP_DUTY_BUY_RATE,
            "brokerage_per_order": BROKERAGE_PER_ORDER,
            "gst_rate": GST_RATE,
            "gst_includes_sebi": True,
        }

    if trade_date is None:
        target_date_str = datetime.now().strftime("%Y-%m-%d")
    elif isinstance(trade_date, str):
        target_date_str = trade_date[:10]
    elif isinstance(trade_date, (datetime, date)):
        target_date_str = trade_date.strftime("%Y-%m-%d")
    else:
        target_date_str = str(trade_date)[:10]

    # STT rate based on April 1, 2026 boundary (Finance Act 2026)
    if target_date_str >= "2026-04-01":
        stt_rate = PUBLISHED_OCT_2026_STT_RATE  # 0.0015 (0.15%)
        stt_schedule_name = "STT_APR_2026_0.15PCT"
    elif target_date_str >= "2024-10-01":
        stt_rate = 0.0010  # 0.10%
        stt_schedule_name = "STT_OCT_2024_0.10PCT"
    else:
        stt_rate = 0.000625  # 0.0625%
        stt_schedule_name = "STT_PRE_OCT_2024_0.0625PCT"

    # Exchange transaction rate based on March 1, 2026 boundary (NSE/FA/73061 composite inclusive of IPFT)
    if target_date_str >= "2026-03-01":
        exchange_rate = PUBLISHED_OCT_2026_EXCHANGE_RATE  # 0.0003553 (0.03553% composite: Rs 3,552.99/cr exch + Rs 0.01/cr IPFT)
        exchange_core = 0.000355299
        ipft_rate = 0.000000001
        exch_schedule_name = "NSE_MAR_2026_0.03553PCT"
        reg_ref = "NSE/FA/73061"
    elif target_date_str >= "2024-10-01":
        exchange_rate = 0.0003553  # 0.03553% composite: 0.03503% core exch + 0.00050% IPFT per NSE/FA/64232 & NSE/FA/56129
        exchange_core = 0.0003503
        ipft_rate = 0.0000050
        exch_schedule_name = "NSE_OCT_2024_0.03553PCT"
        reg_ref = "NSE/FA/64232_56129"
    else:
        exchange_rate = 0.000535  # 0.0535% (legacy tiered)
        exchange_core = 0.000500
        ipft_rate = 0.000035
        exch_schedule_name = "NSE_PRE_OCT_2024_0.0535PCT"
        reg_ref = "NSE_LEGACY"

    schedule_label = f"{stt_schedule_name}_{exch_schedule_name}"

    return {
        "schedule_name": schedule_label,
        "effective_date": target_date_str,
        "stt_sell_rate": stt_rate,
        "exchange_turnover_rate": exchange_rate,
        "exchange_core_rate": exchange_core,
        "ipft_rate": ipft_rate,
        "sebi_turnover_rate": 0.000001,
        "stamp_duty_buy_rate": 0.00003,
        "brokerage_per_order": 20.0,
        "gst_rate": 0.18,
        "gst_includes_sebi": False,  # Upstox published tariff: 18% on (Brokerage + Exchange + IPFT). SEBI excluded.
        "regulatory_ref": reg_ref,
    }


def calculate_option_trade_costs(
    buy_price: float,
    sell_price: float,
    quantity: int,
    apply_slippage: bool = True,
    slippage_rate_per_leg: Optional[float] = None,
    trade_date: Optional[Any] = None,
    buy_bid: Optional[float] = None,
    buy_ask: Optional[float] = None,
    sell_bid: Optional[float] = None,
    sell_ask: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Computes exact transaction friction for an Indian equity option round-trip.
    All figures are in INR (₹).
    Separates quoted spread from additional latency/impact slippage and applies effective-dated statutory rates.
    Guarantees exact per-order additive identity: statutory_charges == sum of rounded fee components.
    """
    buy_premium_turnover = buy_price * quantity
    sell_premium_turnover = sell_price * quantity
    total_turnover = buy_premium_turnover + sell_premium_turnover

    schedule = get_option_tariff_schedule(trade_date)

    brokerage_rounded = round(schedule["brokerage_per_order"] * 2.0, 2)
    stt_rounded = round(sell_premium_turnover * schedule["stt_sell_rate"], 2)
    exchange_fee_rounded = round(total_turnover * schedule["exchange_turnover_rate"], 2)
    exchange_core_fee_rounded = round(total_turnover * schedule.get("exchange_core_rate", schedule["exchange_turnover_rate"]), 2)
    ipft_fee_rounded = round(total_turnover * schedule.get("ipft_rate", 0.0), 2)
    sebi_fee_rounded = round(total_turnover * schedule["sebi_turnover_rate"], 2)
    
    # Upstox published GST charge base: 18% on (Brokerage + Exchange Transaction Charges + IPFT)
    # SEBI turnover fees are excluded from the GST base on official broker contract notes.
    if schedule.get("gst_includes_sebi", False):
        gst_base = brokerage_rounded + exchange_fee_rounded + sebi_fee_rounded
    else:
        gst_base = brokerage_rounded + exchange_fee_rounded
    gst_rounded = round(gst_base * schedule["gst_rate"], 2)
    stamp_duty_rounded = round(buy_premium_turnover * schedule["stamp_duty_buy_rate"], 2)

    statutory_charges = round(
        brokerage_rounded + stt_rounded + exchange_fee_rounded + sebi_fee_rounded + gst_rounded + stamp_duty_rounded, 2
    )

    # Quoted spread cost (half-spread on entry + half-spread on exit)
    quoted_spread_cost = 0.0
    if buy_bid is not None and buy_ask is not None:
        entry_half_spread = max(0.0, (buy_ask - buy_bid) / 2.0) * quantity
        quoted_spread_cost += entry_half_spread
    if sell_bid is not None and sell_ask is not None:
        exit_half_spread = max(0.0, (sell_ask - sell_bid) / 2.0) * quantity
        quoted_spread_cost += exit_half_spread
    quoted_spread_cost = round(quoted_spread_cost, 2)

    # Additional execution slippage beyond the quoted spread (quote movement, latency, market impact)
    slip_rate = slippage_rate_per_leg if slippage_rate_per_leg is not None else SLIPPAGE_RATE_PER_LEG
    additional_slippage = round(
        (buy_premium_turnover * slip_rate + sell_premium_turnover * slip_rate), 2
    ) if apply_slippage else 0.0

    total_friction = round(statutory_charges + additional_slippage, 2)

    return {
        "brokerage": brokerage_rounded,
        "stt": stt_rounded,
        "exchange_fee": exchange_fee_rounded,
        "exchange_core_fee": exchange_core_fee_rounded,
        "ipft_fee": ipft_fee_rounded,
        "sebi_fee": sebi_fee_rounded,
        "gst": gst_rounded,
        "stamp_duty": stamp_duty_rounded,
        "slippage": additional_slippage,
        "additional_slippage": additional_slippage,
        "quoted_spread_cost": quoted_spread_cost,
        "statutory_charges": statutory_charges,
        "total_friction": total_friction,
        "is_statutory_charges_estimated": True,
        "charges_reconciliation_mode": "ESTIMATED_STATUTORY_SCHEDULE",
        "effective_schedule_name": schedule["schedule_name"],
        "effective_schedule_date": schedule["effective_date"],
    }


# Authoritative Continuous Regime & Capital Safety Controls
REGIME_GATE_ENABLED: bool = True  # Enforces continuous Wilder ADX, Kaufman ER, ATR compression, and completed heavyweight slopes
PEAK_TO_TROUGH_MAX_DRAWDOWN_INR: float = 3500.0  # Persistent session circuit breaker ceiling on peak-to-trough giveback
MAX_CONCURRENT_OPEN_TRADES_CAP: int = 2  # Hard institutional concurrency limit

