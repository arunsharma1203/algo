"""
Trading Constants and Single Source of Truth (SSOT) for Market Scheduling,
Timezone Normalization, and Intraday Runway Gating.
"""

import math
from datetime import datetime, time
from typing import Optional, Tuple
import pytz
import numpy as np

# Canonical Market Timezone (Indian Standard Time, UTC+05:30)
IST_TZ = pytz.timezone("Asia/Kolkata")

# Canonical Trading Hours (IST)
MARKET_OPEN_TIME = time(9, 15)
MARKET_CLOSE_TIME = time(15, 30)

# Intraday Execution Boundaries
HARD_CUTOFF_TIME_IST = time(13, 45)  # No new intraday setups permitted past 13:45 IST
SQUAREOFF_TIME_IST = time(15, 15)    # Mandatory broker MIS liquidation begins at 15:15 IST

# Intraday Bar Characteristics
MINUTES_PER_SESSION = 375            # 09:15 to 15:30 = 6 hours 15 minutes = 375 minutes
BARS_PER_SESSION_15M = 25            # 375 / 15 = 25 fifteen-minute bars per day


def get_ist_now() -> datetime:
    """Returns the current timezone-aware timestamp in Indian Standard Time (Asia/Kolkata)."""
    return datetime.now(IST_TZ)


def to_ist_datetime(dt: datetime) -> datetime:
    """Converts any datetime (naive or timezone-aware) into timezone-aware IST."""
    if dt.tzinfo is None:
        return IST_TZ.localize(dt)
    return dt.astimezone(IST_TZ)


def evaluate_intraday_runway(
    entry_price: float,
    target_price: float,
    atr_15m: float,
    now_dt: Optional[datetime] = None
) -> Tuple[bool, str]:
    """
    Evaluates whether an intraday setup has sufficient session runway to achieve its
    profit target before the mandatory 15:15 IST square-off.

    Rules:
    1. Hard Cutoff: Entries at or past 13:45 IST are strictly rejected regardless of volatility.
    2. Dynamic Runway: Calculates required bars based on ATR distance to target:
         target_distance = abs(target - entry)
         atr_units = target_distance / atr_15m
         bars_required = ceil(atr_units * 2.0)  # 2x empirical drift buffer
         bars_remaining = (15:15 IST - current_time) / 15 mins
       If bars_remaining < bars_required, the setup is rejected with INSUFFICIENT_RUNWAY.
    """
    now = now_dt or get_ist_now()
    now_ist = to_ist_datetime(now)
    now_time = now_ist.time()

    # 1. Hard Cutoff Check (13:45 IST)
    if now_time >= HARD_CUTOFF_TIME_IST:
        return False, (
            f"INTRADAY_TIME_EXHAUSTED: Scan time ({now_time.strftime('%H:%M')} IST) "
            f"past {HARD_CUTOFF_TIME_IST.strftime('%H:%M')} IST cutoff. "
            f"Mandatory square-off at {SQUAREOFF_TIME_IST.strftime('%H:%M')} IST."
        )

    # 2. Dynamic Runway Check
    mins_to_squareoff = (15 * 60 + 15) - (now_ist.hour * 60 + now_ist.minute)
    if mins_to_squareoff <= 0:
        return False, "INTRADAY_TIME_EXHAUSTED: Session past 15:15 square-off."

    bars_remaining = max(0, mins_to_squareoff // 15)

    safe_atr = max(float(atr_15m), 1e-4) if atr_15m is not None else 1e-4
    target_dist = abs(float(target_price) - float(entry_price))
    atr_units = target_dist / safe_atr
    # Target requires time to develop; require at least 2.0 bars per ATR unit of distance
    bars_required = max(2, int(math.ceil(atr_units * 2.0)))

    if bars_remaining < bars_required:
        return False, (
            f"INSUFFICIENT_RUNWAY: Target requires {bars_required} bars ({bars_required * 15}m "
            f"at ATR ₹{safe_atr:.2f}), but only {bars_remaining} bars ({mins_to_squareoff}m) "
            f"remain before 15:15 IST square-off."
        )

    return True, "OK"

