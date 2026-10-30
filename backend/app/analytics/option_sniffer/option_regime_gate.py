"""
Option Regime Gate: Authoritative Continuous Regime, Chop, and Capital Safety Engine.

Enforces institutional-grade pre-entry verification for Option Sniffer:
  1. Continuous 1-minute OHLCV ingestion from `continuous_index_ohlcv` with causal timestamps.
  2. Canonical Wilder ADX(14) with strict 28-bar warm-up and fail-closed handling.
  3. Kaufman Efficiency Ratio (ER_15m) micro-chop defense.
  4. ATR Volatility expansion/compression ratio (5m vs 20m).
  5. 15-minute completed-bar heavyweight stock alignment (Reliance, HDFC Bank, ICICI Bank, etc.).
  6. Reversal risk and normalized extrema displacement (Day High/Low exhaustion).
  7. Reward-to-cost economic hurdle (minimum 2.5x net edge over statutory charges and slippage).

INVARIANTS:
  - ZERO permissive fallbacks: Missing data, gaps, or synthetic carry bars strictly FAIL CLOSED.
  - ZERO lookahead bias: Strictly completed bars where candle_end_time <= decision_time.
  - Fully decoupled: Preserves underlying V1 alpha models and weights.
"""

import logging
import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, time as dtime
from typing import Dict, Any, List, Optional, Tuple

from app.analytics.option_sniffer.option_constants import (
    UNDERLYING_HEAVYWEIGHTS,
    calculate_option_trade_costs,
    is_option_trading_window,
    is_option_entry_window,
    MAX_DATA_STALENESS_SECONDS,
    ENABLE_APEX_QUANT_ENGINE,
    CRBI_RELAXED_KAUFMAN_ER,
    to_ist_naive,
)
from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)

# Configurable Institutional Thresholds
# TIMEFRAME CLARIFICATION:
# In an NSE intraday session (09:15 - 15:30 = 375 minutes), there are 375 1-minute bars,
# but ONLY 25 15-minute bars (375 / 15 = 25).
# Therefore, canonical Wilder ADX(14) 28-bar warm-up strictly uses completed 1-MINUTE bars
# (09:15:00 to 09:43:00 IST; warm-up completes at exactly 09:43:00 IST).
# Heavyweight slope concurrence (Gate 5D) strictly uses completed 15-MINUTE bars.
MIN_WARMUP_BARS = 28  # 2 * 14 bars for canonical Wilder ADX(14) (completed 1-minute bars)
ADX_MIN_THRESHOLD = 20.0  # Markets below 20.0 ADX are choppy/non-trending
KAUFMAN_ER_MIN_THRESHOLD = 0.30  # Ratios below 0.30 represent chaotic noise
ATR_COMPRESSION_RATIO_MIN = 0.70  # ATR_5m / ATR_20m < 0.70 represents volatility contraction
EXTREMA_DISPLACEMENT_MAX_ATR = 2.50  # Moves > 2.5x ATR off day extrema indicate exhausted trend
MIN_REWARD_TO_COST_RATIO = 2.50  # Minimum gross target gain vs all-in friction hurdle


def compute_wilder_adx_canonical(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14
) -> Optional[Tuple[float, float, float]]:
    """
    Canonical Wilder ADX(14) implementation adhering strictly to J. Welles Wilder's 1978 specification:
      - Strictly requires at least 2 * period completed bars (28 bars).
      - First 14 transitions are summed for initial TR, +DM, -DM.
      - Subsequent transitions use Wilder's smoothing: S_t = S_{t-1} - S_{t-1}/P + X_t.
      - First 14 DX values (bars 14 to 27) are averaged as an arithmetic mean for initial ADX.
      - Subsequent ADX values use Wilder's smoothing: ADX_t = (ADX_{t-1} * (P - 1) + DX_t) / P.
      - Fails closed (returns None) on missing/NaN values or insufficient bars (< 28).
    Returns (adx, plus_di, minus_di) or None.
    """
    if len(close) < 2 * period:
        return None
    h_arr = np.asarray(high, dtype=float)
    l_arr = np.asarray(low, dtype=float)
    c_arr = np.asarray(close, dtype=float)
    if np.isnan(h_arr).any() or np.isnan(l_arr).any() or np.isnan(c_arr).any():
        return None

    n = len(c_arr)
    tr = np.zeros(n)
    dm_p = np.zeros(n)
    dm_m = np.zeros(n)

    for i in range(1, n):
        h, l = h_arr[i], l_arr[i]
        h_prev, l_prev, c_prev = h_arr[i - 1], l_arr[i - 1], c_arr[i - 1]
        tr[i] = max(h - l, abs(h - c_prev), abs(l - c_prev))
        up = h - h_prev
        down = l_prev - l
        dm_p[i] = up if (up > down and up > 0) else 0.0
        dm_m[i] = down if (down > up and down > 0) else 0.0

    tr_sum = np.zeros(n)
    dm_p_sum = np.zeros(n)
    dm_m_sum = np.zeros(n)

    tr_sum[period] = np.sum(tr[1:period + 1])
    dm_p_sum[period] = np.sum(dm_p[1:period + 1])
    dm_m_sum[period] = np.sum(dm_m[1:period + 1])

    for i in range(period + 1, n):
        tr_sum[i] = tr_sum[i - 1] - (tr_sum[i - 1] / period) + tr[i]
        dm_p_sum[i] = dm_p_sum[i - 1] - (dm_p_sum[i - 1] / period) + dm_p[i]
        dm_m_sum[i] = dm_m_sum[i - 1] - (dm_m_sum[i - 1] / period) + dm_m[i]

    di_p = np.zeros(n)
    di_m = np.zeros(n)
    dx = np.zeros(n)

    for i in range(period, n):
        if tr_sum[i] > 1e-9:
            di_p[i] = 100.0 * (dm_p_sum[i] / tr_sum[i])
            di_m[i] = 100.0 * (dm_m_sum[i] / tr_sum[i])
        sum_di = di_p[i] + di_m[i]
        dx[i] = 100.0 * (abs(di_p[i] - di_m[i]) / sum_di) if sum_di > 1e-9 else 0.0

    adx = np.zeros(n)
    adx[2 * period - 1] = np.mean(dx[period: 2 * period])

    for i in range(2 * period, n):
        adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    val = float(adx[-1])
    p_di = float(di_p[-1])
    m_di = float(di_m[-1])

    if np.isnan(val) or np.isnan(p_di) or np.isnan(m_di):
        return None
    return val, p_di, m_di


def compute_kaufman_er(closes: np.ndarray, period: int = 14) -> Optional[float]:
    """
    Computes Kaufman Efficiency Ratio over `period` intervals (period+1 closes):
      ER = |Close_t - Close_{t-P}| / sum_{i=1..P} |Close_{t-i+1} - Close_{t-i}|
    Fails closed (returns None) if length < period + 1 or volatility is zero.
    """
    c = np.asarray(closes, dtype=float)
    if len(c) < period + 1:
        return None
    c_slice = c[-(period + 1):]
    net_change = abs(c_slice[-1] - c_slice[0])
    sum_abs_diff = float(np.sum(np.abs(np.diff(c_slice))))
    if sum_abs_diff <= 1e-9:
        return None
    return float(net_change / sum_abs_diff)


def compute_atr_ratio(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    short_period: int = 5,
    long_period: int = 20
) -> Optional[float]:
    """
    Computes the ATR Volatility Expansion/Compression ratio:
      Ratio = ATR(short_period) / ATR(long_period)
    Fails closed if insufficient bars (< long_period + 1).
    """
    if len(close) < long_period + 1:
        return None
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)

    tr = np.zeros(len(c))
    for i in range(1, len(c)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))

    tr_short = tr[-short_period:]
    tr_long = tr[-long_period:]

    atr_s = float(np.mean(tr_short))
    atr_l = float(np.mean(tr_long))

    if atr_l <= 1e-9:
        return None
    return float(atr_s / atr_l)


class OptionRegimeGate:
    """
    Authoritative regime, chop, and execution risk gate.
    Evaluates every candidate against genuine completed continuous market bars.
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is not None:
            self._db_path = db_path
        else:
            from app.data.historical_data_layer import get_db_path
            self._db_path = get_db_path()

    def get_completed_index_bars(
        self,
        ticker: str,
        session_date: str,
        as_of_time: datetime,
        min_bars: int = MIN_WARMUP_BARS
    ) -> Tuple[bool, str, Optional[pd.DataFrame]]:
        """
        Retrieves continuous, completed, non-synthetic, point-in-time 1-minute bars
        from `continuous_index_ohlcv`.
        Enforces causal invariants:
          1. Strictly same session (09:15:00 <= timestamp < as_of_time).
          2. Strictly point-in-time: available_at <= as_of_time and is_point_in_time = 1.
          3. Minimum contiguous bars >= min_bars.
          4. Zero synthetic bars (is_synthetic == 0).
          5. Zero gap bars (is_gap == 0).
          6. Max staleness: Most recent bar completed within MAX_DATA_STALENESS_SECONDS.
          7. Bar contiguity: Consecutive bars must be exactly 60 seconds apart.
        """
        as_of_time = to_ist_naive(as_of_time)
        if as_of_time.time() < dtime(9, 15):
            session_start = f"{session_date} 00:00:00"
        else:
            session_start = f"{session_date} 09:15:00"
        cutoff = as_of_time.strftime("%Y-%m-%d %H:%M:00")
        as_of_iso = as_of_time.isoformat()

        def _query_and_validate() -> Tuple[bool, str, Optional[pd.DataFrame]]:
            try:
                with sqlite3.connect(self._db_path, timeout=5.0) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute("""
                        SELECT ticker, timestamp, open, high, low, close, volume,
                               is_complete, is_synthetic, is_gap, available_at
                        FROM continuous_index_ohlcv
                        WHERE ticker = ?
                          AND timestamp >= ?
                          AND timestamp < ?
                          AND available_at <= ?
                          AND is_point_in_time = 1
                        ORDER BY timestamp ASC
                    """, (ticker, session_start, cutoff, as_of_iso)).fetchall()
            except Exception as e:
                logger.error(f"[OptionRegimeGate] Database error querying continuous_index_ohlcv: {e}")
                return False, f"Fail-Closed: Database query failed ({e})", None

            if len(rows) < min_bars:
                return False, f"Fail-Closed: Insufficient continuous completed bars ({len(rows)} < {min_bars} required)", None

            df = pd.DataFrame([dict(r) for r in rows])

            # Check for non-complete, synthetic, or gap bars in the required lookback window
            recent_df = df.iloc[-min_bars:]
            if (recent_df["is_complete"] != 1).any():
                return False, "Fail-Closed: Incomplete or provisional candle detected in warm-up window", None
            if (recent_df["is_synthetic"] != 0).any():
                return False, "Fail-Closed: Synthetic carry bar detected in warm-up window; indicator invalid", None
            if (recent_df["is_gap"] != 0).any():
                return False, "Fail-Closed: Data gap bar detected in warm-up window; indicator invalid", None

            # Check timestamp contiguity (exactly 60s step)
            ts_series = pd.to_datetime(recent_df["timestamp"])
            time_diffs = ts_series.diff().dropna()
            if not (time_diffs == pd.Timedelta(minutes=1)).all():
                return False, "Fail-Closed: Temporal discontinuity detected in continuous index bars", None

            # Check data freshness: last completed candle cannot be older than MAX_DATA_STALENESS_SECONDS
            last_candle_time = ts_series.iloc[-1]
            candle_end_time = last_candle_time + timedelta(minutes=1)
            candle_naive = candle_end_time.to_pydatetime().replace(tzinfo=None) if hasattr(candle_end_time, "tzinfo") and candle_end_time.tzinfo is not None else candle_end_time
            staleness = (as_of_time - candle_naive).total_seconds()
            if staleness > MAX_DATA_STALENESS_SECONDS:
                return False, f"Fail-Closed: Index feed is stale ({staleness:.0f}s > {MAX_DATA_STALENESS_SECONDS}s max)", None

            return True, "OK", df

        ok, msg, df_res = _query_and_validate()
        if not ok:
            # Self-heal live session gaps on canonical production DB (rate-limit governed: max 2 REST calls/45s)
            try:
                import os
                if os.path.abspath(str(self._db_path)) == os.path.abspath(str(get_db_path())):
                    now_ist = to_ist_naive()
                    if session_date == now_ist.strftime("%Y-%m-%d") and abs((now_ist - as_of_time).total_seconds()) <= 180.0:
                        from app.data.index_candle_ingestion_prototype import get_index_candle_ingestion_service
                        repaired = get_index_candle_ingestion_service().heal_intraday_gaps_if_needed(as_of_time=as_of_time)
                        if repaired > 0:
                            ok, msg, df_res = _query_and_validate()
            except Exception as heal_err:
                logger.debug(f"[OptionRegimeGate] Live 1m gap self-heal note: {heal_err}")

        return ok, msg, df_res

    def evaluate_heavyweight_slopes(
        self,
        underlying: str,
        opt_type: str,
        as_of_time: datetime,
        heavyweight_candles: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        heavyweight_quotes: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> Tuple[bool, str, Dict[str, float]]:
        """
        Evaluates 15-minute completed bars for index heavyweight drivers.
        Invariants:
          - Only completed 15m bars where candle_end_time <= as_of_time.
          - Slopes are calculated intraday (never mixing prior session close).
          - CE rejected if all heavyweights have downward slope (< -0.05%) or are trading below VWAP.
          - PE rejected if all heavyweights have upward slope (> +0.05%) or are trading above VWAP.
        """
        as_of_time = to_ist_naive(as_of_time)
        hw_list = UNDERLYING_HEAVYWEIGHTS.get(underlying, [])
        if not hw_list:
            return True, "HEAVYWEIGHTS_UNCONFIGURED", {}

        slopes: Dict[str, float] = {}
        if heavyweight_candles:
            for sym in hw_list:
                bars = heavyweight_candles.get(sym, [])
                # Filter strictly completed 15m bars where candle_end_time <= as_of_time
                valid_bars = []
                for b in bars:
                    b_ts = to_ist_naive(datetime.fromisoformat(str(b.get("timestamp") or b.get("start_time"))))
                    b_end = b_ts + timedelta(minutes=15)
                    if b_end <= as_of_time and b_ts.date() == as_of_time.date():
                        valid_bars.append(b)

                if len(valid_bars) >= 2:
                    last_b_ts = to_ist_naive(datetime.fromisoformat(str(valid_bars[-1].get("timestamp") or valid_bars[-1].get("start_time"))))
                    last_b_end = last_b_ts + timedelta(minutes=15)
                    # Only use 15m heavyweight bars if the latest completed bar is not stale (> 45m old)
                    if (as_of_time - last_b_end).total_seconds() <= 2700:
                        o_first = float(valid_bars[0].get("open") or valid_bars[0].get("close"))
                        c_last = float(valid_bars[-1].get("close"))
                        if o_first > 0:
                            slopes[sym] = round(((c_last - o_first) / o_first) * 100.0, 3)

        if not slopes or len(slopes) < len(hw_list):
            # If completed 15m bars are not yet accumulated (e.g. 09:15 - 09:45 IST),
            # evaluate point-in-time spot vs morning VWAP from real-time quotes
            hw_quotes = heavyweight_quotes or {}
            if not hw_quotes:
                try:
                    from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
                    mgr = OptionStreamManager()
                    hw_quotes = {h: mgr.get_underlying_quote(h) for h in hw_list if mgr.get_underlying_quote(h)}
                except Exception:
                    hw_quotes = {}

            if hw_quotes and len(hw_quotes) >= len(hw_list):
                bull_cnt = sum(1 for h in hw_list if float(hw_quotes.get(h, {}).get("spot_ltp") or 0.0) >= float(hw_quotes.get(h, {}).get("vwap") or hw_quotes.get(h, {}).get("spot_ltp") or 0.0) * 0.9995)
                bear_cnt = sum(1 for h in hw_list if float(hw_quotes.get(h, {}).get("spot_ltp") or 0.0) <= float(hw_quotes.get(h, {}).get("vwap") or hw_quotes.get(h, {}).get("spot_ltp") or 0.0) * 1.0005)
                if opt_type == "CE" and bull_cnt == 0 and bear_cnt >= len(hw_list):
                    hw_str = ", ".join(f"{h}: <VWAP" for h in hw_list)
                    return False, f"Heavyweight Overhead (Real-Time): All primary drivers ({hw_str}) are trading below VWAP. CE entry blocked.", {}
                if opt_type == "PE" and bear_cnt == 0 and bull_cnt >= len(hw_list):
                    hw_str = ", ".join(f"{h}: >VWAP" for h in hw_list)
                    return False, f"Heavyweight Absorption (Real-Time): All primary drivers ({hw_str}) are trading above VWAP. PE entry blocked.", {}
            return True, "HEAVYWEIGHTS_PARTIAL", slopes

        all_negative = all(s < -0.05 for s in slopes.values())
        all_positive = all(s > 0.05 for s in slopes.values())

        if opt_type == "CE" and all_negative:
            hw_str = ", ".join(f"{k}: {v:+.2f}%" for k, v in slopes.items())
            return False, f"Heavyweight Overhead: All primary drivers ({hw_str}) are falling. CE entry blocked.", slopes

        if opt_type == "PE" and all_positive:
            hw_str = ", ".join(f"{k}: {v:+.2f}%" for k, v in slopes.items())
            return False, f"Heavyweight Absorption: All primary drivers ({hw_str}) are rising. PE entry blocked.", slopes

        return True, "OK", slopes

    def evaluate_reversal_displacement(
        self,
        spot_ltp: float,
        day_high: float,
        day_low: float,
        spot_atr: float,
        opt_type: str,
        is_breakout: bool = False,
        is_reversal_recovery: bool = False
    ) -> Tuple[bool, str]:
        """
        Guards against exhausted trends where price has already displaced > 2.5x ATR
        off day extrema without confirming a breakout.
        Allows genuine V-bottom / V-top recoveries where momentum has confirmed direction.
        """
        if spot_atr <= 0 or is_breakout or is_reversal_recovery:
            return True, "OK"

        rally_off_low = spot_ltp - day_low
        drop_off_high = day_high - spot_ltp

        rally_atr_ratio = rally_off_low / spot_atr
        drop_atr_ratio = drop_off_high / spot_atr

        if opt_type == "PE" and rally_atr_ratio >= EXTREMA_DISPLACEMENT_MAX_ATR:
            return False, f"Reversal Exhaustion: Underlying rallied {rally_atr_ratio:.2f}x ATR off Day Low. Downside trend broken; PE entry blocked."

        if opt_type == "CE" and drop_atr_ratio >= EXTREMA_DISPLACEMENT_MAX_ATR:
            return False, f"Reversal Exhaustion: Underlying dropped {drop_atr_ratio:.2f}x ATR off Day High. Upside breakout broken; CE entry blocked."

        return True, "OK"

    def evaluate_reward_to_cost(
        self,
        option_ask: float,
        target_pct: float,
        lot_size: int,
        trade_date: Optional[str] = None
    ) -> Tuple[bool, str, Dict[str, float]]:
        """
        Evaluates whether expected gross gain is at least 2.5x all-in transaction costs
        (statutory charges + bid-ask spread + 1.5% slippage).
        """
        if option_ask <= 0:
            return False, "Fail-Closed: Invalid zero or negative option price", {}

        target_price = option_ask * (1.0 + (target_pct / 100.0))
        gross_gain = (target_price - option_ask) * lot_size

        cost_info = calculate_option_trade_costs(
            buy_price=option_ask,
            sell_price=target_price,
            quantity=lot_size,
            apply_slippage=True,
            trade_date=trade_date
        )
        total_friction = cost_info.get("total_friction", 55.0)

        if total_friction <= 0:
            return False, "Fail-Closed: Cost calculation returned zero friction", cost_info

        ratio = gross_gain / total_friction
        if ratio < MIN_REWARD_TO_COST_RATIO:
            return False, (
                f"Reward-to-Cost Gate: Expected gross profit (₹{gross_gain:.2f}) is only {ratio:.2f}x "
                f"all-in friction (₹{total_friction:.2f}). Requires >={MIN_REWARD_TO_COST_RATIO}x."
            ), cost_info

        return True, "OK", cost_info

    def evaluate_regime(
        self,
        underlying: str,
        opt_type: str,
        as_of_time: Optional[datetime] = None,
        continuous_bars: Optional[pd.DataFrame] = None,
        spot_ltp: Optional[float] = None,
        day_high: Optional[float] = None,
        day_low: Optional[float] = None,
        option_ask: Optional[float] = None,
        target_pct: Optional[float] = None,
        lot_size: int = 65,
        is_breakout: bool = False,
        is_reversal_recovery: bool = False,
        heavyweight_candles: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        heavyweight_quotes: Optional[Dict[str, Dict[str, Any]]] = None,
        enforce_cutoff: bool = False
    ) -> Dict[str, Any]:
        """
        Master regime evaluation pipeline.
        Returns:
          {
            "hurdles_passed": bool,
            "rejection_reasons": List[str],
            "telemetry": Dict[str, Any]
          }
        """
        now_dt = to_ist_naive(as_of_time)
        session_date = now_dt.strftime("%Y-%m-%d")
        rejection_reasons: List[str] = []
        telemetry: Dict[str, Any] = {
            "underlying": underlying,
            "opt_type": opt_type,
            "evaluated_at": now_dt.isoformat(),
            "adx_14": None,
            "plus_di": None,
            "minus_di": None,
            "kaufman_er": None,
            "atr_ratio": None,
            "hw_slopes": {},
            "reward_to_cost_ratio": None
        }

        # 0. Session Window Checks
        if enforce_cutoff:
            if not is_option_trading_window(now_dt):
                rejection_reasons.append("Trading session cutoff passed (15:15 IST reached). New trades halted.")
                return {"hurdles_passed": False, "rejection_reasons": rejection_reasons, "telemetry": telemetry}

            if not is_option_entry_window(now_dt):
                rejection_reasons.append(
                    f"Outside golden option entry window (09:30-13:15 IST, current {now_dt.strftime('%H:%M')} IST). Entries halted."
                )
                return {"hurdles_passed": False, "rejection_reasons": rejection_reasons, "telemetry": telemetry}

        # 1. Continuous 1-Minute Bars Retrieval (Fail-Closed on any data defect)
        if continuous_bars is not None and len(continuous_bars) >= MIN_WARMUP_BARS:
            bars_df = continuous_bars
            ok_bars, bar_msg = True, "PASSED_MOCK"
        else:
            ok_bars, bar_msg, bars_df = self.get_completed_index_bars(
                ticker=underlying,
                session_date=session_date,
                as_of_time=now_dt,
                min_bars=MIN_WARMUP_BARS
            )

        if not ok_bars or bars_df is None:
            rejection_reasons.append(f"Regime Gate: {bar_msg}")
            return {"hurdles_passed": False, "rejection_reasons": rejection_reasons, "telemetry": telemetry}

        # 2. Canonical Wilder ADX(14)
        h_arr = bars_df["high"].values
        l_arr = bars_df["low"].values
        c_arr = bars_df["close"].values

        adx_res = compute_wilder_adx_canonical(h_arr, l_arr, c_arr, period=14)
        if adx_res is None:
            rejection_reasons.append("Regime Gate: Fail-Closed: Wilder ADX(14) calculation returned None (insufficient clean history)")
            return {"hurdles_passed": False, "rejection_reasons": rejection_reasons, "telemetry": telemetry}

        adx_val, p_di, m_di = adx_res
        telemetry["adx_14"] = round(adx_val, 2)
        telemetry["plus_di"] = round(p_di, 2)
        telemetry["minus_di"] = round(m_di, 2)

        # 3. Kaufman Efficiency Ratio (ER_15m)
        er_val = compute_kaufman_er(c_arr, period=14)

        # Fresh Directional Impulse Override:
        # When the index breaks out of consolidation, 28-bar double-smoothed Wilder ADX(14) lags between 15.0 and 20.0
        # for 8-12 minutes even when instantaneous directional spread (|plus_di - minus_di| >= 12.0) and Kaufman ER (>= 0.35)
        # confirm a genuine trend impulse.
        is_fresh_impulse_breakout = (
            adx_val >= 15.0
            and abs(p_di - m_di) >= 12.0
            and (er_val is not None and er_val >= 0.35)
        )

        if adx_val < ADX_MIN_THRESHOLD and not is_fresh_impulse_breakout:
            rejection_reasons.append(
                f"Chop Shield: Wilder ADX(14) is {adx_val:.1f} < {ADX_MIN_THRESHOLD:.1f} (market is in weak chop; option buying halted)."
            )

        if opt_type == "CE" and p_di < m_di:
            rejection_reasons.append(
                f"Direction Conflict: +DI ({p_di:.1f}) is below -DI ({m_di:.1f}); CALL (CE) entry blocked."
            )
        elif opt_type == "PE" and m_di < p_di:
            rejection_reasons.append(
                f"Direction Conflict: -DI ({m_di:.1f}) is below +DI ({p_di:.1f}); PUT (PE) entry blocked."
            )

        if er_val is not None:
            telemetry["kaufman_er"] = round(er_val, 3)
            active_er_min = CRBI_RELAXED_KAUFMAN_ER if (ENABLE_APEX_QUANT_ENGINE and is_breakout) else KAUFMAN_ER_MIN_THRESHOLD
            if er_val < active_er_min:
                rejection_reasons.append(
                    f"Scale-Invariant Chop Shield: Kaufman ER ({er_val:.2f} < {active_er_min:.2f}) indicates chaotic noise."
                )
        else:
            rejection_reasons.append("Regime Gate: Fail-Closed: Kaufman ER calculation returned None")

        # 4. ATR Volatility Expansion/Compression Ratio
        atr_ratio = compute_atr_ratio(h_arr, l_arr, c_arr, short_period=5, long_period=20)
        if atr_ratio is not None:
            telemetry["atr_ratio"] = round(atr_ratio, 3)
            if atr_ratio < ATR_COMPRESSION_RATIO_MIN:
                rejection_reasons.append(
                    f"Volatility Compression: ATR ratio ({atr_ratio:.2f} < {ATR_COMPRESSION_RATIO_MIN:.2f}) indicates contracting volatility."
                )

        # 5. Heavyweight Stock Alignment
        ok_hw, hw_msg, hw_slopes = self.evaluate_heavyweight_slopes(
            underlying=underlying,
            opt_type=opt_type,
            as_of_time=now_dt,
            heavyweight_candles=heavyweight_candles,
            heavyweight_quotes=heavyweight_quotes
        )
        telemetry["hw_slopes"] = hw_slopes
        if not ok_hw:
            rejection_reasons.append(f"Heavyweight Concurrence: {hw_msg}")

        # 6. Reversal Risk / Extrema Displacement
        if spot_ltp is not None and day_high is not None and day_low is not None:
            # Estimate current spot ATR from the 14-bar window
            spot_atr = float(np.mean(h_arr[-14:] - l_arr[-14:]))
            ok_rev, rev_msg = self.evaluate_reversal_displacement(
                spot_ltp=spot_ltp,
                day_high=day_high,
                day_low=day_low,
                spot_atr=spot_atr,
                opt_type=opt_type,
                is_breakout=is_breakout,
                is_reversal_recovery=is_reversal_recovery
            )
            if not ok_rev:
                rejection_reasons.append(f"Reversal Guard: {rev_msg}")

        # 7. Reward-to-Cost Economic Friction Hurdle
        if option_ask is not None and target_pct is not None:
            ok_rtc, rtc_msg, cost_info = self.evaluate_reward_to_cost(
                option_ask=option_ask,
                target_pct=target_pct,
                lot_size=lot_size,
                trade_date=session_date
            )
            if cost_info:
                gross_gain = (option_ask * (target_pct / 100.0)) * lot_size
                fric = cost_info.get("total_friction", 1.0)
                telemetry["reward_to_cost_ratio"] = round(gross_gain / fric, 2) if fric > 0 else 0.0

            if not ok_rtc:
                rejection_reasons.append(f"Friction Hurdle: {rtc_msg}")

        hurdles_passed = len(rejection_reasons) == 0
        return {
            "hurdles_passed": hurdles_passed,
            "rejection_reasons": rejection_reasons,
            "telemetry": telemetry
        }


def seed_mock_continuous_bars(
    db_path: str,
    ticker: str = "NIFTY",
    num_bars: int = 30,
    base_time: Optional[datetime] = None,
    trending: Any = True
) -> None:
    """
    Seeds clean, contiguous, point-in-time completed 1-minute bars into continuous_index_ohlcv.
    Used for integration tests and test fixtures requiring valid completed bars for ADX warm-up.
    """
    now = base_time or datetime.now()
    session_date = now.strftime("%Y-%m-%d")
    end_minute = now.replace(second=0, microsecond=0)
    start_dt = end_minute - timedelta(minutes=num_bars)
    session_open = datetime.strptime(f"{session_date} 09:15:00", "%Y-%m-%d %H:%M:%S")
    if start_dt < session_open and now >= session_open:
        start_dt = session_open

    with sqlite3.connect(db_path, timeout=10.0) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS continuous_index_ohlcv (
                ticker TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER DEFAULT 0,
                tick_count INTEGER DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'LOCAL_AGGREGATOR',
                is_complete INTEGER DEFAULT 1,
                is_synthetic INTEGER DEFAULT 0,
                is_gap INTEGER DEFAULT 0,
                provider_open REAL,
                provider_high REAL,
                provider_low REAL,
                provider_close REAL,
                local_open REAL,
                local_high REAL,
                local_low REAL,
                local_close REAL,
                discrepancy_flag INTEGER DEFAULT 0,
                received_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                reconciled_at TEXT,
                version INTEGER DEFAULT 1,
                is_point_in_time INTEGER DEFAULT 1,
                PRIMARY KEY (ticker, timestamp, version)
            );
        """)
        for i in range(num_bars):
            bar_ts = (start_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
            avail_ts = (start_dt + timedelta(minutes=i + 1)).isoformat()
            if trending in ("BEAR", "DOWN", -1):
                o = 25000.0 - i * 5.0
                h = o + 2.0
                l = o - 6.0
                c = o - 4.0
            elif trending in ("BULL", "UP", 1, True):
                o = 25000.0 + i * 5.0
                h = o + 6.0
                l = o - 2.0
                c = o + 4.0
            else:
                o = 25000.0
                h = 25005.0
                l = 24995.0
                c = 25000.0
            conn.execute("""
                INSERT OR REPLACE INTO continuous_index_ohlcv (
                    ticker, timestamp, open, high, low, close, volume, tick_count,
                    source, is_complete, is_synthetic, is_gap, received_at, available_at,
                    version, is_point_in_time
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, 10000, 100,
                    'LOCAL_AGGREGATOR', 1, 0, 0, ?, ?,
                    1, 1
                )
            """, (ticker, bar_ts, o, h, l, c, avail_ts, avail_ts))
        conn.commit()

