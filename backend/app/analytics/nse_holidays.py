"""
NSE TRADING HOLIDAY CALENDAR & TRADING DAY UTILITIES
====================================================
Authoritative module for National Stock Exchange of India (NSE) trading sessions.
Calculates exact trading days, excluding Saturdays, Sundays, and official exchange holidays.

Used by:
- Qlib Swing recommendation lifecycle (strict 5-trading-day holding horizon)
- Market data freshness validators
- Historical data synchronizer
"""

from datetime import date, datetime, timedelta
from typing import List, Union, Set, Optional
import numpy as np

# Official NSE Equity & Derivatives Trading Holidays (2024 - 2026)
# Reference: NSE India Official Holiday Circulars
NSE_TRADING_HOLIDAYS: List[str] = [
    # 2024
    "2024-01-22",  # Special Holiday (Ayodhya Pran Pratishtha)
    "2024-01-26",  # Republic Day
    "2024-03-08",  # Mahashivratri
    "2024-03-25",  # Holi
    "2024-03-29",  # Good Friday
    "2024-04-11",  # Id-Ul-Fitr (Ramadan Eid)
    "2024-04-17",  # Shri Ram Navami
    "2024-05-01",  # Maharashtra Day
    "2024-05-20",  # General Parliamentary Elections (Mumbai)
    "2024-06-17",  # Bakri Id / Eid ul-Adha
    "2024-07-17",  # Muharram
    "2024-08-15",  # Independence Day
    "2024-10-02",  # Mahatma Gandhi Jayanti
    "2024-11-01",  # Diwali Laxmi Pujan
    "2024-11-15",  # Gurunanak Jayanti
    "2024-11-20",  # Maharashtra Assembly Elections
    "2024-12-25",  # Christmas

    # 2025
    "2025-01-26",  # Republic Day
    "2025-02-26",  # Mahashivratri
    "2025-03-14",  # Holi
    "2025-03-31",  # Id-Ul-Fitr
    "2025-04-10",  # Mahavir Jayanti
    "2025-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2025-04-18",  # Good Friday
    "2025-05-01",  # Maharashtra Day
    "2025-06-07",  # Bakri Id
    "2025-07-06",  # Muharram
    "2025-08-15",  # Independence Day
    "2025-08-27",  # Ganesh Chaturthi
    "2025-10-02",  # Mahatma Gandhi Jayanti / Dussehra
    "2025-10-21",  # Diwali Laxmi Pujan
    "2025-10-22",  # Diwali Balipratipada
    "2025-11-05",  # Gurunanak Jayanti
    "2025-12-25",  # Christmas

    # 2026
    "2026-01-26",  # Republic Day
    "2026-03-04",  # Holi
    "2026-03-16",  # Mahashivratri
    "2026-03-20",  # Id-Ul-Fitr
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-27",  # Bakri Id
    "2026-06-26",  # Muharram
    "2026-08-15",  # Independence Day
    "2026-09-15",  # Ganesh Chaturthi
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-11-09",  # Diwali Laxmi Pujan
    "2026-11-24",  # Gurunanak Jayanti
    "2026-12-25",  # Christmas
]

_NSE_HOLIDAYS_SET: Set[str] = set(NSE_TRADING_HOLIDAYS)


def get_nse_holidays() -> List[str]:
    """Returns sorted list of ISO date strings for NSE trading holidays."""
    return sorted(NSE_TRADING_HOLIDAYS)


def is_nse_holiday(dt: Union[date, datetime, str]) -> bool:
    """
    Returns True if the given date is an NSE trading holiday or weekend.
    """
    if isinstance(dt, str):
        try:
            d = datetime.fromisoformat(dt[:10]).date()
        except Exception:
            return False
    elif isinstance(dt, datetime):
        d = dt.date()
    else:
        d = dt

    # Check weekend (5 = Saturday, 6 = Sunday)
    if d.weekday() >= 5:
        return True

    return d.isoformat() in _NSE_HOLIDAYS_SET


def is_trading_day(dt: Union[date, datetime, str]) -> bool:
    """Returns True if the date is an active NSE trading day (weekday and not holiday)."""
    return not is_nse_holiday(dt)


def count_nse_trading_days(
    start_date: Union[date, datetime, str],
    end_date: Union[date, datetime, str]
) -> int:
    """
    Counts the exact number of NSE trading sessions elapsed between start_date and end_date.
    Excludes weekends and official NSE trading holidays using numpy busday_count.

    Example:
    Entry Friday -> Saturday = 0, Sunday = 0, Monday (trading) = 1, Tuesday = 2.
    """
    def _to_date(val):
        if isinstance(val, str):
            return datetime.fromisoformat(val[:10]).date()
        if isinstance(val, datetime):
            return val.date()
        return val

    d_start = _to_date(start_date)
    d_end = _to_date(end_date)

    if d_end <= d_start:
        return 0

    try:
        # np.busday_count counts dates in range [d_start, d_end)
        count = int(np.busday_count(d_start, d_end, holidays=NSE_TRADING_HOLIDAYS))
        return max(0, count)
    except Exception:
        # Fail-safe iteration if busday_count encounters invalid input
        cur = d_start + timedelta(days=1)
        cnt = 0
        while cur <= d_end:
            if is_trading_day(cur):
                cnt += 1
            cur += timedelta(days=1)
        return cnt


def calculate_swing_expiry_date(
    start_date: Union[date, datetime, str],
    horizon_trading_days: int = 5,
    target_trading_days: Optional[int] = None
) -> date:
    """
    Calculates the exact calendar date on which a swing trade reaches its N-trading-day horizon.
    """
    if target_trading_days is not None:
        horizon_trading_days = target_trading_days
    def _to_date(val):
        if isinstance(val, str):
            return datetime.fromisoformat(val[:10]).date()
        if isinstance(val, datetime):
            return val.date()
        return val

    d_cur = _to_date(start_date)
    days_counted = 0
    while days_counted < horizon_trading_days:
        d_cur += timedelta(days=1)
        if is_trading_day(d_cur):
            days_counted += 1
    return d_cur

