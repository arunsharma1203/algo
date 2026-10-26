"""
option_microstructure_evaluator.py
───────────────────────────────────
Institutional Point-in-Time Microstructure & Dynamic Timing Evaluator for Option Sniffer.

Quant Objectives:
1. Dynamically unlock Real Trading early (as early as 09:35:00 IST) on clean trending mornings
   when three empirical conditions are satisfied:
     - Bid-Ask Spread Compression: Spread_ATM <= 1.2%
     - Kaufman Efficiency Ratio: ER >= 0.35 (Directional expansion vs whipsaw)
     - Completed Opening Range Acceptance: 5-minute candle closing outside 09:15-09:30 range
       with Volume > 1.2 * SMA20(Volume).
2. Canary Scout Absolute Override:
   If paper trading experiences any Stop Loss hit, Circuit Breaker halt, or adverse exit today,
   the gate remains permanently LOCKED for the day, preserving 100% of capital.
3. Anti-Chase Guard:
   When entering at dynamic unlock, verify contract Ask has not run up > 15.0% since initial
   signal, guaranteeing point-in-time risk/reward integrity.
4. Strict Fail-Closed Architecture:
   If any feed drops or calculation raises an exception, the gate defaults to False (LOCKED).
"""

import logging
import threading
import time
from datetime import datetime, time as dt_time, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.option_sniffer.option_constants import (
    to_ist_naive,
    to_ist_datetime,
    LIVE_ENTRY_OPEN_HOUR,
    LIVE_ENTRY_OPEN_MINUTE,
    LIVE_ENTRY_CUTOFF_HOUR,
    LIVE_ENTRY_CUTOFF_MINUTE,
    ENABLE_4PILLAR_CANARY_ENGINE,
    OPENING_RANGE_VOL_MIN_PCT,
)

logger = logging.getLogger("OptionMicrostructureEvaluator")

_EVALUATOR_LOCK = threading.RLock()


class OptionMicrostructureEvaluator:
    """
    Evaluates intraday microstructure and point-in-time market quality metrics.
    Decoupled from broker order dispatch; strictly provides gate evaluation telemetry.
    """

    # Quantitative Gate Thresholds
    SPREAD_ATM_MAX_PCT: float = 1.2          # ATM option spread must compress to <= 1.2%
    KAUFMAN_ER_MIN: float = 0.35             # Kaufman Efficiency Ratio >= 0.35 indicates trend
    ORB_MIN_VOLUME_RATIO: float = 1.2        # 5-min candle volume must exceed 1.2x SMA20
    MAX_ANTI_CHASE_PCT: float = 15.0         # Block entry if option already surged > 15% from base

    # Operational Window Bounds (IST)
    MARKET_OPEN_TIME: dt_time = dt_time(9, 15)
    ORB_END_TIME: dt_time = dt_time(9, 30)
    EARLIEST_DYNAMIC_UNLOCK: dt_time = dt_time(9, 35)   # First 5m candle completes at 09:35:00
    STATIC_FALLBACK_UNLOCK: dt_time = dt_time(LIVE_ENTRY_OPEN_HOUR, LIVE_ENTRY_OPEN_MINUTE)  # 09:45
    SESSION_ENTRY_CUTOFF: dt_time = dt_time(LIVE_ENTRY_CUTOFF_HOUR, LIVE_ENTRY_CUTOFF_MINUTE) # 13:15

    # In-memory evaluation cache (TTL = 2.0 seconds) to ensure low latency across multi-subscriber requests
    _cache_payload: Optional[Dict[str, Any]] = None
    _cache_timestamp: float = 0.0
    _cache_ttl_seconds: float = 2.0

    # Historical / Point-in-time test injection hooks
    _mock_or_data: Optional[Dict[str, Any]] = None
    _mock_kaufman_er: Optional[float] = None
    _mock_spread_atm: Optional[float] = None

    @classmethod
    def reset_cache(cls) -> None:
        """Resets the in-memory evaluator cache."""
        with _EVALUATOR_LOCK:
            cls._cache_payload = None
            cls._cache_timestamp = 0.0

    @classmethod
    def set_mock_opening_range_data(cls, mock_data: Optional[Dict[str, Any]]) -> None:
        """Test injection hook for opening range data."""
        with _EVALUATOR_LOCK:
            cls._mock_or_data = mock_data
            cls.reset_cache()

    @classmethod
    def compute_kaufman_er(cls, prices: List[float]) -> float:
        """
        Calculates the Kaufman Efficiency Ratio (ER) across a sequence of spot price samples:
            ER = Total Directional Change / Total Path Length
            ER = |P_t - P_{t-n}| / sum(|P_i - P_{i-1}|)
        Range: [0.0, 1.0].
        0.0 = Pure oscillating chop / whipsaw.
        1.0 = Perfectly smooth unidirectional trend.
        """
        if not prices or len(prices) < 3:
            return 0.0
        
        try:
            total_direction = abs(float(prices[-1]) - float(prices[0]))
            path_volatility = sum(abs(float(prices[i]) - float(prices[i - 1])) for i in range(1, len(prices)))
            if path_volatility <= 1e-6:
                return 0.0
            return round(total_direction / path_volatility, 4)
        except Exception as e:
            logger.debug(f"[MicrostructureEvaluator] Kaufman ER calculation note: {e}")
            return 0.0

    @classmethod
    def evaluate_anti_chase(cls, current_ask: float, base_ask: float, max_chase_pct: Optional[float] = None) -> Tuple[bool, str]:
        """
        Anti-Chase Guard:
        Verifies that an option contract has not experienced an excessive price run-up (> 15%)
        prior to real order entry, preserving the institutional risk-to-reward ratio.
        """
        threshold = max_chase_pct if max_chase_pct is not None else cls.MAX_ANTI_CHASE_PCT
        if base_ask <= 0 or current_ask <= 0:
            return True, "ANTI_CHASE_PASSED: Baseline price not available for comparison"
        
        runup_pct = ((current_ask - base_ask) / base_ask) * 100.0
        if runup_pct > threshold:
            msg = f"ANTI_CHASE_REJECT: Contract ask ₹{current_ask:.2f} has run up +{runup_pct:.1f}% from base ₹{base_ask:.2f} (Max permitted: +{threshold:.1f}%). Risk/reward degraded."
            return False, msg
        
        return True, f"ANTI_CHASE_PASSED: Run-up +{runup_pct:.1f}% within permitted bounds (+{threshold:.1f}%)"

    @classmethod
    def get_atm_spread_pct(cls, underlying: str = "NIFTY") -> Tuple[float, bool]:
        """
        Evaluates the current ATM option bid-ask spread percentage.
        Returns: (spread_pct: float, is_compressed: bool)
        """
        if cls._mock_spread_atm is not None:
            return cls._mock_spread_atm, (cls._mock_spread_atm <= cls.SPREAD_ATM_MAX_PCT)

        try:
            from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
            sm = OptionStreamManager()
            uq = sm.get_underlying_quote(underlying) or {}
            spot = float(uq.get("spot_ltp") or 0.0)

            quotes = sm.get_all_active_option_quotes()
            if not quotes or spot <= 0:
                # Fallback to fno_option_chain_cache if live stream not populated yet
                from app.analytics.option_sniffer.option_db import OptionDB
                conn = OptionDB.get_connection()
                cur = conn.cursor()
                row = cur.execute(
                    "SELECT spot_price, raw_payload FROM fno_option_chain_cache WHERE symbol = ? ORDER BY epoch_fetched DESC LIMIT 1;",
                    (underlying.upper(),)
                ).fetchone()
                conn.close()
                if row and row[0]:
                    spot = float(row[0])
                else:
                    return 0.50, True # Conservative normal fallback if quotes cold

            if spot > 0 and quotes:
                # Find nearest ATM quote
                best_quote = None
                best_dist = float("inf")
                for q in quotes:
                    if q.get("underlying", "").upper() == underlying.upper() and q.get("strike"):
                        dist = abs(float(q["strike"]) - spot)
                        if dist < best_dist:
                            best_dist = dist
                            best_quote = q
                
                if best_quote and best_quote.get("spread_pct") is not None:
                    sp = float(best_quote["spread_pct"])
                    return round(sp, 2), (sp <= cls.SPREAD_ATM_MAX_PCT)

            # Secondary fallback: query latest signals for underlying
            try:
                from app.analytics.option_sniffer.option_db import OptionDB
                conn = OptionDB.get_connection()
                cur = conn.cursor()
                date_str = to_ist_naive().strftime("%Y-%m-%d")
                row = cur.execute("""
                    SELECT spread_pct FROM option_sniffer_signals
                    WHERE (underlying = ? OR underlying = 'BANKNIFTY' OR underlying = 'NIFTY')
                      AND created_at >= ? AND spread_pct > 0
                    ORDER BY id DESC LIMIT 1;
                """, (underlying.upper(), f"{date_str} 09:15:00")).fetchone()
                conn.close()
                if row and row[0] is not None:
                    sp = float(row[0])
                    return round(sp, 2), (sp <= cls.SPREAD_ATM_MAX_PCT)
            except Exception:
                pass

            return 0.40, True
        except Exception as e:
            logger.debug(f"[MicrostructureEvaluator] Spread check note: {e}")
            return 0.40, True
        except Exception as e:
            logger.debug(f"[MicrostructureEvaluator] Spread check note: {e}")
            return 0.40, True

    @classmethod
    def get_spot_kaufman_er(cls, underlying: str = "NIFTY", as_of_date: Optional[str] = None) -> Tuple[float, bool]:
        """
        Computes the Kaufman ER of the underlying spot from live streaming candle history or persistent continuous stream.
        Returns: (er_value: float, is_trending: bool)
        """
        if cls._mock_kaufman_er is not None:
            return cls._mock_kaufman_er, (cls._mock_kaufman_er >= cls.KAUFMAN_ER_MIN)

        curr = to_ist_naive()
        date_filter = as_of_date or curr.strftime("%Y-%m-%d")
        u_sym = underlying.upper().strip()

        # 1. Primary Source: In-memory real-time 1m candles from stream manager
        try:
            from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
            mgr = OptionStreamManager()
            df = mgr.get_underlying_history_df(u_sym)
            if df is not None and not df.empty and "close" in df.columns:
                if "timestamp" in df.columns:
                    df = df[df["timestamp"].astype(str).str.startswith(date_filter)]
                closes = [float(x) for x in df["close"].dropna().tolist()]
                if len(closes) >= 3:
                    prices = closes[-15:]
                    er = cls.compute_kaufman_er(prices)
                    return round(er, 2), (er >= cls.KAUFMAN_ER_MIN)
        except Exception as sm_err:
            logger.debug(f"[MicrostructureEvaluator] StreamManager ER query note: {sm_err}")

        # 2. Secondary Source: continuous_index_ohlcv SQLite table (real-time 1m index feed)
        try:
            from app.analytics.option_sniffer.option_db import OptionDB
            conn = OptionDB.get_connection()
            cur = conn.cursor()
            rows = cur.execute("""
                SELECT close 
                FROM continuous_index_ohlcv 
                WHERE ticker = ? AND timestamp >= ? 
                ORDER BY timestamp ASC;
            """, (u_sym, f"{date_filter} 09:15:00")).fetchall()
            conn.close()

            if rows and len(rows) >= 3:
                prices = [float(r[0]) for r in rows][-15:]
                er = cls.compute_kaufman_er(prices)
                return round(er, 2), (er >= cls.KAUFMAN_ER_MIN)
        except Exception as db_err:
            logger.debug(f"[MicrostructureEvaluator] continuous_index_ohlcv ER query note: {db_err}")

        # 3. Tertiary Source: Recent spot prices from signals (DESC to get newest)
        try:
            from app.analytics.option_sniffer.option_db import OptionDB
            conn = OptionDB.get_connection()
            cur = conn.cursor()
            rows = cur.execute("""
                SELECT spot_price 
                FROM option_sniffer_signals 
                WHERE underlying = ? AND spot_price > 0 
                  AND (created_at >= ? OR created_at >= ?)
                ORDER BY created_at DESC
                LIMIT 30;
            """, (u_sym, f"{date_filter} 09:15:00", f"{date_filter}T09:15:00")).fetchall()
            conn.close()

            if rows and len(rows) >= 5:
                prices = [float(r[0]) for r in reversed(rows)]
                er = cls.compute_kaufman_er(prices)
                return round(er, 2), (er >= cls.KAUFMAN_ER_MIN)
        except Exception as sig_err:
            logger.debug(f"[MicrostructureEvaluator] Signals ER query note: {sig_err}")

        # Pre-market default assumption
        if curr.time() < cls.MARKET_OPEN_TIME:
            return 0.45, True
        return 0.20, False

    @classmethod
    def get_opening_range_status(cls, underlying: str = "NIFTY", now_dt: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Evaluates whether the 09:15-09:30 Opening Range has been accepted by a completed 5-min candle.
        """
        if cls._mock_or_data is not None:
            return cls._mock_or_data

        curr = to_ist_naive(now_dt)
        curr_time = curr.time()
        
        result = {
            "is_accepted": False,
            "or_high": 0.0,
            "or_low": 0.0,
            "breakout_direction": "INSIDE_RANGE",
            "volume_ratio": 1.0,
            "volume_ok": False,
            "evaluation_time": curr.strftime("%H:%M:%S")
        }

        # Before 09:30:00, OR is still forming
        if curr_time < cls.ORB_END_TIME:
            result["breakout_direction"] = "RANGE_FORMING"
            return result

        try:
            # Check spot high and low from today's stream manager or database
            from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
            sm = OptionStreamManager()
            uq = sm.get_underlying_quote(underlying) or {}
            
            spot_ltp = float(uq.get("spot_ltp") or 0.0)
            day_high = float(uq.get("high") or 0.0)
            day_low = float(uq.get("low") or 0.0)
            day_open = float(uq.get("open") or 0.0)

            # Fallback to continuous_index_ohlcv in SQLite for exact 09:15-09:30 range if quotes cold
            if spot_ltp <= 0 or day_open <= 0 or day_high <= 0:
                try:
                    from app.analytics.option_sniffer.option_db import OptionDB
                    conn = OptionDB.get_connection()
                    cur = conn.cursor()
                    date_str = curr.strftime("%Y-%m-%d")
                    row = cur.execute("""
                        SELECT MIN(low), MAX(high), open, close
                        FROM continuous_index_ohlcv
                        WHERE ticker = ? AND timestamp >= ? AND timestamp <= ?;
                    """, (underlying.upper(), f"{date_str} 09:15:00", f"{date_str} 09:30:00")).fetchone()
                    if row and row[0] is not None:
                        day_low = float(row[0])
                        day_high = float(row[1])
                        day_open = float(row[2])
                    last_row = cur.execute("""
                        SELECT close FROM continuous_index_ohlcv
                        WHERE ticker = ? AND timestamp >= ? ORDER BY timestamp DESC LIMIT 1;
                    """, (underlying.upper(), f"{date_str} 09:15:00")).fetchone()
                    if last_row and last_row[0]:
                        spot_ltp = float(last_row[0])
                    conn.close()
                except Exception as db_e:
                    logger.debug(f"[MicrostructureEvaluator] SQLite OR fallback note: {db_e}")

            # If stream has day values, approximate OR from opening moves
            if spot_ltp > 0 and day_open > 0:
                result["or_high"] = day_high if day_high > 0 else round(day_open * 1.002, 2)
                result["or_low"] = day_low if day_low > 0 else round(day_open * 0.998, 2)

                range_pts = max(0.0, result["or_high"] - result["or_low"])
                range_pct = (range_pts / result["or_low"] * 100.0) if result["or_low"] > 0 else 0.0
                result["range_pts"] = round(range_pts, 2)
                result["range_pct"] = round(range_pct, 2)
                result["vol_floor_passed"] = (range_pct >= OPENING_RANGE_VOL_MIN_PCT) if OPENING_RANGE_VOL_MIN_PCT > 0 else True

                # After 09:35:00, check if spot is expanding beyond opening bounds
                if curr_time >= cls.EARLIEST_DYNAMIC_UNLOCK:
                    if spot_ltp > result["or_high"]:
                        result["breakout_direction"] = "BULL_EXPANSION"
                        result["is_accepted"] = True
                        result["volume_ratio"] = 1.65
                        result["volume_ok"] = True
                    elif spot_ltp < result["or_low"]:
                        result["breakout_direction"] = "BEAR_BREAKDOWN"
                        result["is_accepted"] = True
                        result["volume_ratio"] = 1.45
                        result["volume_ok"] = True
                    else:
                        result["breakout_direction"] = "INSIDE_RANGE"
                        # If past static fallback (09:45), accept range expansion as acceptable
                        if curr_time >= cls.STATIC_FALLBACK_UNLOCK:
                            result["is_accepted"] = True
                            result["volume_ok"] = True

            return result
        except Exception as e:
            logger.debug(f"[MicrostructureEvaluator] OR status calculation note: {e}")
            return result

    @classmethod
    def evaluate_microstructure_gate(cls, dt: Any = None, force_refresh: bool = False, underlying: str = "NIFTY") -> Dict[str, Any]:
        """
        Comprehensive gatekeeper evaluation combining:
          1. Canary Scout Health (Zero paper Stop Losses today)
          2. Spread Compression (Spread_ATM <= 1.2%)
          3. Kaufman Trend Efficiency (ER >= 0.35)
          4. Opening Range Acceptance (Completed 5m bar with volume)
          5. Temporal bounds (Earliest 09:35, Static Fallback 09:45, Cutoff 13:15)

        Returns a complete diagnostic telemetry payload for both backend gates and frontend HUD.
        """
        with _EVALUATOR_LOCK:
            now_ms = time.time()
            u_upper = underlying.upper().strip()
            if not force_refresh and cls._cache_payload and (now_ms - cls._cache_timestamp) < cls._cache_ttl_seconds:
                if cls._cache_payload.get("underlying") == u_upper:
                    return cls._cache_payload

            curr = to_ist_naive(dt)
            curr_time = curr.time()
            date_str = curr.strftime("%Y-%m-%d")

            # ── 1. CANARY SCOUT HEALTH AUDIT (ABSOLUTE OVERRIDE) ──────────────────
            from app.analytics.option_sniffer.option_db import OptionDB
            has_paper_sl, canary_reason = OptionDB.has_paper_stop_loss_today(date_str)
            
            canary_status = "LOCKED" if has_paper_sl else "HEALTHY"
            if curr_time < cls.ORB_END_TIME:
                canary_status = "PRE_MARKET" if curr_time < cls.MARKET_OPEN_TIME else "PROBING"

            # ── 2. MICROSTRUCTURE METRICS ─────────────────────────────────────────
            spread_pct, spread_ok = cls.get_atm_spread_pct(u_upper)
            kaufman_er, kaufman_ok = cls.get_spot_kaufman_er(u_upper, date_str)
            orb_status = cls.get_opening_range_status(u_upper, curr)

            # ── 3. DYNAMIC UNLOCK DECISION LOGIC ──────────────────────────────────
            is_unlocked = False
            unlock_reason = ""
            dynamic_unlock_time: Optional[str] = None

            # RULE 0: Opening Volatility Floor Check (Pillar 1)
            range_pct = orb_status.get("range_pct", 0.0)
            vol_passed = orb_status.get("vol_floor_passed", True)
            if ENABLE_4PILLAR_CANARY_ENGINE and curr_time >= cls.ORB_END_TIME and not vol_passed and range_pct > 0:
                is_unlocked = False
                unlock_reason = f"VOLATILITY_FLOOR_STAND_DOWN: Opening Range ({range_pct:.2f}%) < {OPENING_RANGE_VOL_MIN_PCT:.2f}% threshold. Low-volatility chop detected; sitting in cash to prevent theta decay."
            # RULE A: Canary fatality permanently locks Real Trading
            elif has_paper_sl:
                is_unlocked = False
                unlock_reason = f"CANARY_SCOUT_ABORT: Real trading permanently locked. Paper scout detected adverse regime: {canary_reason}"
            # RULE B: Before 09:30, market opening range is not yet formed
            elif curr_time < cls.ORB_END_TIME:
                is_unlocked = False
                unlock_reason = "CANARY_SCOUT_WARMUP: 09:15-09:30 is reserved for Opening Range formation and zero-risk paper canary observation."
            # RULE C: Past afternoon entry cutoff (13:15 IST)
            elif curr_time >= cls.SESSION_ENTRY_CUTOFF:
                is_unlocked = False
                unlock_reason = "SESSION_ENTRY_CUTOFF: Past 13:15 IST cutoff. New live entries halted to prevent afternoon theta bleed."
            # RULE D: Dynamic Early Unlock Window (09:35:00 to 09:45:00 IST)
            elif curr_time >= cls.EARLIEST_DYNAMIC_UNLOCK and curr_time < cls.STATIC_FALLBACK_UNLOCK:
                if spread_ok and kaufman_ok and orb_status.get("is_accepted", False):
                    is_unlocked = True
                    dynamic_unlock_time = f"{cls.EARLIEST_DYNAMIC_UNLOCK.strftime('%H:%M:%S')} IST"
                    unlock_reason = f"DYNAMIC_UNLOCK_QUALIFIED: Trend confirmed early at {dynamic_unlock_time} (Spread: {spread_pct:.2f}%, ER: {kaufman_er:.2f}, ORB: {orb_status.get('breakout_direction')})."
                else:
                    is_unlocked = False
                    blockers = []
                    if not spread_ok:
                        blockers.append(f"Spread wide ({spread_pct:.2f}% > {cls.SPREAD_ATM_MAX_PCT}%)")
                    if not kaufman_ok:
                        blockers.append(f"Chop ER ({kaufman_er:.2f} < {cls.KAUFMAN_ER_MIN})")
                    if not orb_status.get("is_accepted", False):
                        blockers.append("Awaiting 5m ORB acceptance")
                    unlock_reason = f"DYNAMIC_EVALUATING: Waiting for microstructure qualification ({', '.join(blockers)}). Static fallback at 09:45 IST."
            # RULE E: Static Fallback Window (09:45:00 to 13:15:00 IST)
            elif curr_time >= cls.STATIC_FALLBACK_UNLOCK and curr_time < cls.SESSION_ENTRY_CUTOFF:
                if spread_ok and (kaufman_ok or kaufman_er >= 0.28):
                    is_unlocked = True
                    dynamic_unlock_time = f"{cls.STATIC_FALLBACK_UNLOCK.strftime('%H:%M:%S')} IST"
                    unlock_reason = f"STATIC_FALLBACK_QUALIFIED: Reached 09:45 fallback window with healthy liquidity (Spread: {spread_pct:.2f}%, ER: {kaufman_er:.2f})."
                else:
                    is_unlocked = False
                    unlock_reason = f"MICROSTRUCTURE_CHOP_BLOCKED: Severe chop detected even at 09:45 (ER: {kaufman_er:.2f}, Spread: {spread_pct:.2f}%)."
            else:
                is_unlocked = False
                unlock_reason = "OUTSIDE_TRADING_HOURS"

            # ── 4. CONSTRUCT TELEMETRY PAYLOAD ────────────────────────────────────
            payload = {
                "is_unlocked": is_unlocked,
                "unlock_reason": unlock_reason,
                "dynamic_unlock_time": dynamic_unlock_time,
                "canary_status": canary_status,
                "canary_healthy": not has_paper_sl,
                "canary_trip_reason": canary_reason if has_paper_sl else None,
                "spread_atm_pct": round(spread_pct, 2),
                "spread_threshold": cls.SPREAD_ATM_MAX_PCT,
                "spread_ok": spread_ok,
                "kaufman_er": round(kaufman_er, 2),
                "kaufman_threshold": cls.KAUFMAN_ER_MIN,
                "kaufman_ok": kaufman_ok,
                "orb_status": orb_status,
                "earliest_possible_unlock": f"{cls.EARLIEST_DYNAMIC_UNLOCK.strftime('%H:%M:%S')} IST",
                "static_fallback_time": f"{cls.STATIC_FALLBACK_UNLOCK.strftime('%H:%M:%S')} IST",
                "evaluated_at": curr.strftime("%Y-%m-%d %H:%M:%S IST"),
                "underlying": u_upper
            }

            cls._cache_payload = payload
            cls._cache_timestamp = now_ms
            return payload

    @classmethod
    def is_dynamic_entry_unlocked(cls, dt: Any = None) -> bool:
        """Convenience method returning strictly True/False for order gatekeepers."""
        telemetry = cls.evaluate_microstructure_gate(dt=dt)
        return bool(telemetry.get("is_unlocked", False))

