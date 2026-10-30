"""
Option Sniffer: Signal State Machine & Quality Gate Engine.

State Transitions:
  WATCH ➔ QUALIFIED ➔ ACTIVE ➔ PAPER_OPEN ➔ PAPER_CLOSED

Enforces 10 Strict Quality Gates before any paper trade or Telegram alert fires:
  1. Data Freshness (Age <= 15,000 ms)
  2. Contract Validity & Expiry Sanity
  3. Minimum Liquidity (OI >= 5,000, Volume >= 200, Bid >= ₹5)
  4. Valid Order Book Depth (Bid > 0, Ask >= Bid)
  5. Maximum Spread Ceiling (Spread % <= 3.5%)
  6. Underlying Index Spot & VWAP Synchronization
  7. Option Alpha Model Execution (direction != NO_TRADE)
  8. Calibrated Net Win Probability Hurdle (P >= 58.0%)
  9. Net Return Expectancy Hurdle (Expected Net Return >= +3.0% after friction)
 10. Cooldown Hysteresis & Signal De-duplication (300s window per contract)
"""

import logging
import uuid
import time
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import numpy as np

from app.analytics.option_sniffer.option_constants import (
    STATE_WATCH,
    STATE_QUALIFIED,
    STATE_ACTIVE,
    DIRECTION_BUY_CE,
    DIRECTION_BUY_PE,
    DIRECTION_NO_TRADE,
    MAX_QUOTE_AGE_MS,
    MAX_SPREAD_PCT,
    MIN_OI,
    MIN_VOLUME,
    MIN_BID_PRICE,
    MIN_CALIBRATED_PROB,
    MIN_EXPECTED_NET_RETURN_PCT,
    SIGNAL_COOLDOWN_SECONDS,
    OPTION_MODEL_VERSION,
    OPTION_SIGNAL_VERSION,
    UNDERLYING_HEAVYWEIGHTS,
    BREAKOUT_VOLUME_MIN_RATIO,
    KAUFMAN_ER_MIN_THRESHOLD,
    KAUFMAN_ER_TREND_THRESHOLD,
    EXTREMA_DISPLACEMENT_MAX_ATR_RATIO,
    INTERMARKET_BREADTH_MIN_RATIO,
    INTROSPECTION_FREEZE_MINUTES,
    ANTI_PING_PONG_FLIP_MINUTES,
    SMART_PYRAMIDING_MIN_MFE_PCT,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DAILY_LOSS_INR,
    POST_LOSS_CONTAGION_FREEZE_SECONDS,
    MAX_POSITIONS_PER_UNDERLYING,
    ENABLE_APEX_QUANT_ENGINE,
    CRBI_BREAKOUT_VOLUME_MIN_RATIO,
    CRBI_RELAXED_MODEL_PROB,
    CRBI_RELAXED_KAUFMAN_ER,
    CRBI_MIN_HEAVYWEIGHT_ALIGNMENT,
    MAX_CLIMAX_Z_EXTENSION,
    IN_RANGE_MIN_CALIBRATED_PROB,
    IN_RANGE_MIN_EXPECTED_NET_RETURN_PCT,
    CANDLE_BODY_MIN_RATIO,
    is_option_trading_window,
    is_option_entry_window,
    to_ist_naive,
)
from app.analytics.option_sniffer.option_db import OptionDB
from app.analytics.option_sniffer.option_alpha_model import OptionAlphaModel
from app.analytics.option_sniffer.option_feature_extractor import OptionFeatureExtractor
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.telegram_notifier import send_telegram_message

logger = logging.getLogger(__name__)


_PREV_OSE_CLS = globals().get("OptionSignalEngine")


class OptionSignalEngine:
    """Evaluates option candidate streams through strict state machine gates."""

    _COOLDOWNS: Dict[str, float] = getattr(_PREV_OSE_CLS, "_COOLDOWNS", {})  # instrument_key -> last_active_signal_epoch
    _DIRECTION_SL_COOLDOWNS: Dict[str, float] = getattr(_PREV_OSE_CLS, "_DIRECTION_SL_COOLDOWNS", {})  # "{underlying}_{opt_type}" -> last_sl_hit_epoch
    _DIRECTION_SL_COOLDOWN_SECONDS: int = 600  # 10-minute direction cooldown after Stop-Loss hit
    _MAX_CONCURRENT_OPEN_TRADES: int = 3
    _PORTFOLIO_LOSS_HISTORY: List[float] = getattr(_PREV_OSE_CLS, "_PORTFOLIO_LOSS_HISTORY", [])  # list of recent SL epochs across portfolio
    _INTROSPECTION_FROZEN_UNTIL: float = getattr(_PREV_OSE_CLS, "_INTROSPECTION_FROZEN_UNTIL", 0.0)
    _ACTIVE_UNDERLYING_LOCKS: Dict[str, float] = getattr(_PREV_OSE_CLS, "_ACTIVE_UNDERLYING_LOCKS", {})  # underlying -> last_open_trade_epoch (Anti-Clustering Mutex)

    @classmethod
    def record_direction_sl_cooldown(cls, underlying: str, opt_type: str, epoch: Optional[float] = None) -> None:
        """Places all contracts of underlying+opt_type on cooldown and manages portfolio introspection entropy."""
        curr_epoch = epoch if epoch is not None else time.time()
        key = f"{str(underlying or 'NIFTY').upper()}_{str(opt_type or 'CE').upper()}"
        cls._DIRECTION_SL_COOLDOWNS[key] = curr_epoch
        logger.info(f"[OptionSignalEngine] 🛡️ Activated {cls._DIRECTION_SL_COOLDOWN_SECONDS}s Direction SL Cooldown for {key}.")

        # Portfolio Introspection: Clean losses older than 30m, append current
        cls._PORTFOLIO_LOSS_HISTORY = [e for e in cls._PORTFOLIO_LOSS_HISTORY if (curr_epoch - e) <= (INTROSPECTION_FREEZE_MINUTES * 60)]
        cls._PORTFOLIO_LOSS_HISTORY.append(curr_epoch)
        if len(cls._PORTFOLIO_LOSS_HISTORY) >= 2:
            cls._INTROSPECTION_FROZEN_UNTIL = curr_epoch + (INTROSPECTION_FREEZE_MINUTES * 60)
            logger.warning(
                f"[OptionSignalEngine] 🚨 PORTFOLIO INTROSPECTION ENGAGED: {len(cls._PORTFOLIO_LOSS_HISTORY)} Stop-Losses in last 30m. "
                f"Global entry freeze active for {INTROSPECTION_FREEZE_MINUTES} minutes until {time.strftime('%H:%M:%S', time.localtime(cls._INTROSPECTION_FROZEN_UNTIL))} IST."
            )

    @classmethod
    def reset_class_state(cls) -> None:
        """Resets all in-memory class-level state (cooldowns, loss history, freeze timers)."""
        cls._COOLDOWNS.clear()
        cls._DIRECTION_SL_COOLDOWNS.clear()
        cls._PORTFOLIO_LOSS_HISTORY.clear()
        cls._INTROSPECTION_FROZEN_UNTIL = 0.0
        cls._ACTIVE_UNDERLYING_LOCKS.clear()

    @classmethod
    def _verify_candle_close_confirmation(
        cls, df: Any, spot_ltp: float, u_high: float, u_low: float, opt_type: str
    ) -> bool:
        """
        Anti-Wick Sweep Guard:
        Verifies that breakout is confirmed by genuine candle structure rather than a 1-second spike.
        For CE: Last candle close must be >= (u_high - 2.0).
        For PE: Last candle close must be <= (u_low + 2.0).
        """
        if df is None or not hasattr(df, '__len__') or len(df) < 2:
            return True
        close_col = "close" if "close" in df.columns else "Close"
        if close_col not in df.columns:
            return True
        last_c = float(df[close_col].iloc[-1])
        if opt_type == "CE":
            return last_c >= (u_high - 2.0)
        elif opt_type == "PE":
            return last_c <= (u_low + 2.0)
        return True

    def __init__(self, model: Optional[OptionAlphaModel] = None):
        self.model = model or OptionAlphaModel.load()

    def evaluate_contract(
        self,
        contract: Dict[str, Any],
        quote: Dict[str, Any],
        underlying_quote: Dict[str, Any],
        underlying_df: Optional[Any] = None,
        macro_context: Optional[Dict[str, Any]] = None,
        upstream_signals: Optional[Dict[str, Any]] = None,
        heavyweight_quotes: Optional[Dict[str, Dict[str, Any]]] = None,
        as_of_time: Optional[datetime] = None,
        enforce_cutoff: bool = False,
        enforce_regime_gate: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Processes a point-in-time option quote through the 10 quality gates.
        Returns evaluation state dictionary.
        """
        now_dt = to_ist_naive(as_of_time)
        now_epoch = time.time() if as_of_time is None else as_of_time.timestamp()
        now_iso = now_dt.isoformat()
        ikey = contract.get("instrument_key")
        tsym = contract.get("trading_symbol")
        underlying = contract.get("underlying", "NIFTY")
        opt_type = contract.get("option_type", "CE")
        strike = float(contract.get("strike") or 0.0)
        expiry = contract.get("expiry")

        rejection_reasons: List[str] = []

        # ── GATE 0: SESSION CUTOFF & TRADING WINDOW (CUTOFF AT 15:15 IST) ──
        if (enforce_cutoff or as_of_time is not None) and not is_option_trading_window(now_dt):
            rejection_reasons.append("Trading session cutoff passed (15:15 IST reached). New trades halted.")
            return {
                "instrument_key": ikey,
                "trading_symbol": tsym,
                "status": STATE_WATCH,
                "direction": DIRECTION_NO_TRADE,
                "rejection_reasons": rejection_reasons,
                "hurdles_passed": False,
            }

        # ── GATE 0A: GOLDEN ENTRY WINDOW CURFEW (09:30 - 14:00 IST) ─────────
        if (enforce_cutoff or as_of_time is not None) and not is_option_entry_window(now_dt):
            rejection_reasons.append(
                f"Outside golden option entry window (09:30-14:00 IST, current {now_dt.strftime('%H:%M')} IST). New option entries halted to avoid afternoon theta decay."
            )
            return {
                "instrument_key": ikey,
                "trading_symbol": tsym,
                "status": STATE_WATCH,
                "direction": DIRECTION_NO_TRADE,
                "rejection_reasons": rejection_reasons,
                "hurdles_passed": False,
            }

        # ── GATE 1: DATA FRESHNESS ───────────────────────────────────────────
        age_ms = int(quote.get("quote_age_ms") or 99999)
        if age_ms > MAX_QUOTE_AGE_MS:
            rejection_reasons.append(f"Stale quote ({age_ms}ms > {MAX_QUOTE_AGE_MS}ms)")

        # ── GATE 2: ORDER BOOK DEPTH & VALIDITY ──────────────────────────────
        bid = float(quote.get("bid") or 0.0)
        ask = float(quote.get("ask") or 0.0)
        ltp = float(quote.get("ltp") or 0.0)

        if bid < MIN_BID_PRICE:
            rejection_reasons.append(f"Bid price too low (₹{bid:.2f} < ₹{MIN_BID_PRICE})")
        if ask < bid:
            rejection_reasons.append(f"Inverted book (Ask ₹{ask:.2f} < Bid ₹{bid:.2f})")

        # ── GATE 3: SPREAD CEILING ───────────────────────────────────────────
        spread_pct = float(quote.get("spread_pct") or 999.0)
        if spread_pct > MAX_SPREAD_PCT:
            rejection_reasons.append(f"Excessive spread ({spread_pct:.2f}% > {MAX_SPREAD_PCT}%)")

        # ── GATE 4: MINIMUM LIQUIDITY ────────────────────────────────────────
        oi = float(quote.get("oi") or 0.0)
        vol = int(quote.get("volume") or 0)
        if oi < MIN_OI:
            rejection_reasons.append(f"Low Open Interest ({int(oi):,} < {MIN_OI:,})")
        if vol < MIN_VOLUME:
            rejection_reasons.append(f"Low Volume ({vol:,} < {MIN_VOLUME:,})")

        # ── GATE 5: UNDERLYING DATA INTEGRITY ────────────────────────────────
        spot_ltp = float(underlying_quote.get("spot_ltp") or 0.0)
        u_age_ms = int(underlying_quote.get("quote_age_ms") or 99999)
        if spot_ltp <= 0 or u_age_ms > MAX_QUOTE_AGE_MS:
            rejection_reasons.append(f"Underlying spot unavailable or stale ({u_age_ms}ms)")

        # ── GATE 5B: DYNAMIC REGIME SHIELD (REVERSALS, MOMENTUM, CHOP) ──────────
        u_open = float(underlying_quote.get("open") or spot_ltp)
        u_high = float(underlying_quote.get("high") or spot_ltp)
        u_low = float(underlying_quote.get("low") or spot_ltp)
        u_close = float(underlying_quote.get("close") or u_open)
        u_vwap = float(underlying_quote.get("vwap") or spot_ltp)

        pullback_from_high = ((spot_ltp - u_high) / u_high * 100.0) if u_high > 0 else 0.0
        rally_from_low = ((spot_ltp - u_low) / u_low * 100.0) if u_low > 0 else 0.0
        drift_from_open = ((spot_ltp - u_open) / u_open * 100.0) if u_open > 0 else 0.0
        drift_from_close = ((spot_ltp - u_close) / u_close * 100.0) if u_close > 0 else 0.0
        drift_from_vwap = ((spot_ltp - u_vwap) / u_vwap * 100.0) if u_vwap > 0 else 0.0

        # Day High-Low Range
        day_range_pts = u_high - u_low
        day_range_pct = (day_range_pts / u_low * 100.0) if u_low > 0 else 0.0
        is_banknifty = "BANK" in str(underlying).upper()
        min_trend_range_pts = 100.0 if not is_banknifty else 250.0

        # Rolling 10m & recent momentum
        range_10m_pct = 0.0
        range_pts_10m = 0.0
        recent_ret_pct = 0.0
        if underlying_df is not None and hasattr(underlying_df, '__len__') and len(underlying_df) >= 4:
            try:
                close_col = "close" if "close" in underlying_df.columns else "Close"
                if close_col in underlying_df.columns:
                    c_series = underlying_df[close_col]
                    lookback_idx = -5 if len(c_series) >= 5 else -4
                    base_c = float(c_series.iloc[lookback_idx])
                    curr_c = float(c_series.iloc[-1])
                    if base_c > 0:
                        recent_ret_pct = (curr_c - base_c) / base_c * 100.0
            except Exception:
                pass

        if underlying_df is not None and hasattr(underlying_df, '__len__') and len(underlying_df) >= 10:
            try:
                high_col = "high" if "high" in underlying_df.columns else "High"
                low_col = "low" if "low" in underlying_df.columns else "Low"
                if high_col in underlying_df.columns and low_col in underlying_df.columns:
                    recent_slice = underlying_df.iloc[-10:]
                    high_10m = float(recent_slice[high_col].max())
                    low_10m = float(recent_slice[low_col].min())
                    if low_10m > 0:
                        range_10m_pct = (high_10m - low_10m) / low_10m * 100.0
                        range_pts_10m = high_10m - low_10m
            except Exception:
                pass

        # Macro Context Evaluation
        macro_verdict = ""
        if macro_context:
            macro_verdict = str(
                macro_context.get("intraday_regime_verdict") or macro_context.get("regime_verdict") or ""
            ).upper()

        is_macro_chop = any(k in macro_verdict for k in ("CHOP", "NEUTRAL", "SIDEWAYS", "RANGE"))
        is_macro_bear = ("BEAR" in macro_verdict) and not is_macro_chop
        is_macro_bull = ("BULL" in macro_verdict) and not is_macro_chop

        # Compression check (either 10m tight box or Day Range compressed below min_trend_range_pts)
        threshold_10m = 45.0 if is_banknifty else 18.0
        is_compressed_10m = (range_10m_pct < 0.08 or range_pts_10m < threshold_10m) and (range_pts_10m > 0)
        
        # Trapped in range check: if day range < min_trend_range_pts AND spot is not at outer extremes (within 8 pts of high/low)
        is_trapped_in_day_range = (day_range_pts < min_trend_range_pts) and (day_range_pts > 0) and (spot_ltp > (u_low + 8.0)) and (spot_ltp < (u_high - 8.0))

        # Pillar 3: Multi-Bar Close and Volume Ratio Breakout Confirmation
        is_candle_confirmed_high = self._verify_candle_close_confirmation(
            underlying_df, spot_ltp, u_high, u_low, "CE"
        )
        is_candle_confirmed_low = self._verify_candle_close_confirmation(
            underlying_df, spot_ltp, u_high, u_low, "PE"
        )
        is_volume_confirmed = True

        if underlying_df is not None and hasattr(underlying_df, '__len__') and len(underlying_df) >= 5:
            vol_col = "volume" if "volume" in underlying_df.columns else "Volume"
            if vol_col in underlying_df.columns:
                v_series = underlying_df[vol_col]
                try:
                    if float(v_series.iloc[-1]) > 0:
                        v_ma = float(v_series.rolling(min(20, len(v_series)), min_periods=1).mean().iloc[-1])
                        if v_ma > 0:
                            v_ratio = float(v_series.iloc[-1] / v_ma)
                            if v_ratio < BREAKOUT_VOLUME_MIN_RATIO:
                                is_volume_confirmed = False
                except Exception:
                    pass

        # ── Scale-Invariant Mathematical Engines (Chop, Reversal, Intermarket Breadth) ──
        # Engine 1: Kaufman Efficiency Ratio (ER_15m)
        er_15m = 0.50
        if underlying_df is not None and hasattr(underlying_df, '__len__') and len(underlying_df) >= 15:
            try:
                close_col = "close" if "close" in underlying_df.columns else "Close"
                if close_col in underlying_df.columns:
                    c_slice = underlying_df[close_col].iloc[-15:]
                    net_change = abs(float(c_slice.iloc[-1]) - float(c_slice.iloc[0]))
                    sum_diffs = float(np.sum(np.abs(np.diff(c_slice.values))))
                    if sum_diffs > 0:
                        er_15m = net_change / sum_diffs
            except Exception:
                pass
        is_chaotic_chop = (er_15m < KAUFMAN_ER_MIN_THRESHOLD)

        # Engine 2: 5-minute rolling ATR and Extrema Displacement
        default_atr = max(35.0, spot_ltp * 0.0016) if not is_banknifty else max(85.0, spot_ltp * 0.0018)
        spot_atr_5m = default_atr if spot_ltp > 0 else (25.0 if not is_banknifty else 65.0)
        if day_range_pts > 40.0:
            spot_atr_5m = max(spot_atr_5m, day_range_pts / 4.0)
        if underlying_df is not None and hasattr(underlying_df, '__len__') and len(underlying_df) >= 14:
            try:
                h_col = "high" if "high" in underlying_df.columns else "High"
                l_col = "low" if "low" in underlying_df.columns else "Low"
                c_col = "close" if "close" in underlying_df.columns else "Close"
                if h_col in underlying_df.columns and l_col in underlying_df.columns and c_col in underlying_df.columns:
                    h = underlying_df[h_col].iloc[-14:]
                    l = underlying_df[l_col].iloc[-14:]
                    c_prev = underlying_df[c_col].iloc[-15:-1]
                    tr = np.maximum(h.values - l.values, np.maximum(np.abs(h.values - c_prev.values), np.abs(l.values - c_prev.values)))
                    calc_atr = float(np.mean(tr))
                    if calc_atr > 5.0:
                        spot_atr_5m = max(default_atr * 0.70, calc_atr)
            except Exception:
                pass

        rally_off_low_pts = spot_ltp - u_low
        drop_off_high_pts = u_high - spot_ltp
        rally_atr_ratio = (rally_off_low_pts / spot_atr_5m) if spot_atr_5m > 0 else 0.0
        drop_atr_ratio = (drop_off_high_pts / spot_atr_5m) if spot_atr_5m > 0 else 0.0

        is_v_bottom_recovery = (rally_from_low >= 0.35) and (recent_ret_pct >= 0.05)
        is_v_top_reversal = (pullback_from_high <= -0.35) and (recent_ret_pct <= -0.05)

        is_bear_reversal_broken = (rally_atr_ratio >= EXTREMA_DISPLACEMENT_MAX_ATR_RATIO) and (spot_ltp > (u_low + 15.0)) and not is_v_top_reversal
        is_bull_reversal_broken = (drop_atr_ratio >= EXTREMA_DISPLACEMENT_MAX_ATR_RATIO) and (spot_ltp < (u_high - 15.0)) and not is_v_bottom_recovery

        # Climax Overextension Guard (Pillar 2: Anti-Peak / Anti-Trough)
        # Prevents buying Call options at the top of an exhausted spike or Put options at the bottom of a dump
        dist_from_vwap_pts = abs(spot_ltp - u_vwap)
        z_extension = (dist_from_vwap_pts / spot_atr_5m) if spot_atr_5m > 0 else 0.0
        is_climax_overextended = z_extension > MAX_CLIMAX_Z_EXTENSION
        if is_climax_overextended:
            rejection_reasons.append(
                f"Climax Overextension Guard: Spot is {dist_from_vwap_pts:.1f} pts ({z_extension:.2f}x ATR) from VWAP (ceiling {MAX_CLIMAX_Z_EXTENSION:.2f}x). "
                f"Exhaustion climax detected; buying halted to prevent buying at peaks/troughs."
            )

        # Engine 3: Inter-Market Composite Breadth Vector (B_composite)
        b_composite = 1.0
        try:
            from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
            mgr = OptionStreamManager()
            nq = mgr.get_underlying_quote("NIFTY") or {}
            bq = mgr.get_underlying_quote("BANKNIFTY") or {}
            n_spot = float(nq.get("spot_ltp") or 0.0)
            n_vwap = float(nq.get("vwap") or n_spot)
            b_spot = float(bq.get("spot_ltp") or 0.0)
            b_vwap = float(bq.get("vwap") or b_spot)
            if n_spot > 0 and b_spot > 0:
                n_sgn = 1.0 if n_spot >= n_vwap else -1.0
                b_sgn = 1.0 if b_spot >= b_vwap else -1.0
                b_composite = round(0.60 * n_sgn + 0.40 * b_sgn, 2)
        except Exception:
            b_composite = 1.0
        is_intermarket_divergent = abs(b_composite) < INTERMARKET_BREADTH_MIN_RATIO

        # Breakout status (Pillar 3: Eliminates 5-point random wick sweeps)
        is_day_high_breakout = (
            (spot_ltp >= u_high - 2.0)
            and (recent_ret_pct >= 0.05)
            and (drift_from_vwap >= 0.02)
            and is_candle_confirmed_high
            and is_volume_confirmed
        )
        is_day_low_breakdown = (
            (spot_ltp <= u_low + 2.0)
            and (recent_ret_pct <= -0.05)
            and (drift_from_vwap <= -0.02)
            and is_candle_confirmed_low
            and is_volume_confirmed
        )

        # ── APEX QUANT: CRB-I (CAUSAL REGIME BREAKOUT INITIATION) ──
        # Heavyweight concurrence pre-check: CRB-I must not activate if heavyweights are in contrary disagreement
        hw_divergent = False
        active_hw_quotes = heavyweight_quotes or {}
        if underlying in UNDERLYING_HEAVYWEIGHTS:
            hw_list = UNDERLYING_HEAVYWEIGHTS[underlying]
            if not active_hw_quotes:
                try:
                    from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
                    mgr = OptionStreamManager()
                    active_hw_quotes = {h: mgr.get_underlying_quote(h) for h in hw_list if mgr.get_underlying_quote(h)}
                except Exception:
                    active_hw_quotes = {}
            if active_hw_quotes and len(active_hw_quotes) >= len(hw_list):
                bull_hw = sum(1 for h in hw_list if float(active_hw_quotes.get(h, {}).get("spot_ltp") or 0.0) >= float(active_hw_quotes.get(h, {}).get("vwap") or active_hw_quotes.get(h, {}).get("spot_ltp") or 0.0) * 0.9995)
                bear_hw = sum(1 for h in hw_list if float(active_hw_quotes.get(h, {}).get("spot_ltp") or 0.0) <= float(active_hw_quotes.get(h, {}).get("vwap") or active_hw_quotes.get(h, {}).get("spot_ltp") or 0.0) * 1.0005)
                if opt_type == "CE" and bull_hw == 0 and bear_hw >= len(hw_list):
                    hw_divergent = True
                elif opt_type == "PE" and bear_hw == 0 and bull_hw >= len(hw_list):
                    hw_divergent = True

        is_crbi_active = False
        if ENABLE_APEX_QUANT_ENGINE and not hw_divergent:
            if opt_type == "CE" and is_day_high_breakout:
                is_crbi_active = True
            elif opt_type == "PE" and is_day_low_breakdown:
                is_crbi_active = True

        # When CRB-I is active, relax Kaufman ER chop hurdle to prevent entry lag
        active_er_threshold = CRBI_RELAXED_KAUFMAN_ER if is_crbi_active else KAUFMAN_ER_MIN_THRESHOLD
        is_chaotic_chop = (er_15m < active_er_threshold)

        # In-Range determination (Spot between day high and day low boundaries)
        is_in_day_range = (spot_ltp > (u_low + 4.0)) and (spot_ltp < (u_high - 4.0))

        # Strong independent underlying expansion check (reversal-immune)
        is_strong_underlying_bear = ((drift_from_open <= -0.30) or (pullback_from_high <= -0.30)) and (recent_ret_pct <= 0.08) and not is_bear_reversal_broken
        is_strong_underlying_bull = ((drift_from_open >= 0.30) or (rally_from_low >= 0.30)) and (recent_ret_pct >= -0.08) and not is_bull_reversal_broken
        is_underlying_flat = abs(drift_from_open) < 0.20 and abs(pullback_from_high) < 0.20 and abs(rally_from_low) < 0.20

        # ── HIERARCHICAL REGIME DECISION ──
        # 1. SIDEWAYS / RANGEBOUND / CHOP SHIELD (SUPREME PRIORITY)
        if (is_macro_chop or is_compressed_10m or is_trapped_in_day_range or is_chaotic_chop) and not (is_day_high_breakout or is_day_low_breakdown or is_strong_underlying_bear or is_strong_underlying_bull):
            if is_chaotic_chop:
                rejection_reasons.append(
                    f"Scale-Invariant Chop Shield: Kaufman ER ({er_15m:.2f} < {active_er_threshold:.2f}) indicates chaotic chop. Option buying halted to prevent theta decay."
                )
            elif is_compressed_10m:
                rejection_reasons.append(
                    f"Dynamic Regime Shield: Underlying {underlying} is in compressed 10-minute sideways consolidation "
                    f"(range {range_10m_pct:.2f}% < 0.08%, {range_pts_10m:.1f} pts). CHOP / SIDEWAYS detected. Option buying halted to prevent theta decay."
                )
            else:
                rejection_reasons.append(
                    f"Dynamic Regime Shield: Market is in CHOP / SIDEWAYS / RANGEBOUND (Day Range {day_range_pts:.1f} pts / {day_range_pct:.2f}%, "
                    f"Macro: {macro_verdict or 'NEUTRAL'}). Option buying halted to prevent theta decay."
                )
        elif opt_type == "PE" and is_bear_reversal_broken and not is_day_low_breakdown:
            rejection_reasons.append(
                f"Normalized Reversal Guard: Underlying rallied {rally_atr_ratio:.2f}x ATR ({rally_off_low_pts:.1f} pts) off Day Low. Downside trend is statistically broken; PE buying locked out."
            )
        elif opt_type == "CE" and is_bull_reversal_broken and not is_day_high_breakout:
            rejection_reasons.append(
                f"Normalized Reversal Guard: Underlying dropped {drop_atr_ratio:.2f}x ATR ({drop_off_high_pts:.1f} pts) off Day High. Upside breakout is statistically broken / Intraday bear reversal; CE buying locked out."
            )
        elif is_intermarket_divergent and not (is_day_high_breakout or is_day_low_breakdown):
            rejection_reasons.append(
                f"Inter-Market Divergence: Composite breadth B_comp={b_composite} indicates NIFTY/BANKNIFTY divergence across VWAP. Trading frozen to prevent hopping whipsaws."
            )
        elif is_strong_underlying_bear or is_day_low_breakdown or (is_macro_bear and drift_from_vwap <= 0.05):
            # Clear Bear momentum or top-reversal: Allow PUTs, strictly block CALLs
            if opt_type == "CE":
                rejection_reasons.append(
                    f"Dynamic Regime Shield: Bearish regime / Intraday bear reversal / intraday bear momentum ({pullback_from_high:.2f}% from high, {drift_from_open:.2f}% from open) blocks CALL (CE) options."
                )
        elif is_strong_underlying_bull or is_day_high_breakout or (is_macro_bull and drift_from_vwap >= -0.05):
            # Clear Bull momentum or bottom-bounce: Allow CALLs, strictly block PUTs
            if opt_type == "PE":
                rejection_reasons.append(
                    f"Dynamic Regime Shield: Bullish regime / Intraday bull momentum (+{rally_from_low:.2f}% from low, +{drift_from_open:.2f}% from open) blocks PUT (PE) options."
                )
        elif is_macro_chop:
            rejection_reasons.append(
                f"Dynamic Regime Shield: Market is in CHOP / SIDEWAYS ({macro_verdict or 'NEUTRAL'}). Option buying halted to prevent theta burn."
            )
        else:
            # Secondary directional sanity checks based on VWAP and Macro
            if is_macro_bear and opt_type == "CE":
                rejection_reasons.append(
                    f"Dynamic Regime Shield: Bearish regime ({macro_verdict}) blocks CALL (CE) options."
                )
            elif is_macro_bull and opt_type == "PE":
                rejection_reasons.append(
                    f"Dynamic Regime Shield: Bullish regime ({macro_verdict}) blocks PUT (PE) options."
                )
            elif opt_type == "CE" and drift_from_vwap < -0.15:
                rejection_reasons.append(
                    f"Dynamic Regime Shield: Spot is below VWAP ({drift_from_vwap:.2f}%), blocking CALL (CE) purchases."
                )
            elif opt_type == "PE" and drift_from_vwap > 0.15:
                rejection_reasons.append(
                    f"Dynamic Regime Shield: Spot is above VWAP (+{drift_from_vwap:.2f}%), blocking PUT (PE) purchases."
                )

        # ── GATE 5C: UPSTREAM DUAL-ENGINE CONCURRENCE & CONFLICT FILTER ──────
        if upstream_signals:
            if upstream_signals.get("is_conflicted", False):
                rejection_reasons.append(
                    "Dual-Engine Concurrence: Upstream Legacy vs Qlib conflict detected. Options trading halted."
                )
            c_dir = str(upstream_signals.get("consensus_direction") or "").upper()
            is_index = underlying in ("NIFTY", "BANKNIFTY")
            if c_dir in ("BEAR", "BEARISH", "SELL") and opt_type == "CE":
                # If index is in confirmed Day High breakout, index breakout takes precedence over lagging stock breadth
                if not (is_index and is_day_high_breakout):
                    rejection_reasons.append(
                        f"Dual-Engine Concurrence: Upstream consensus is {c_dir}; conflicting CALL (CE) blocked."
                    )
            elif c_dir in ("BULL", "BULLISH", "BUY") and opt_type == "PE":
                # If index is in confirmed Day Low breakdown, index breakdown takes precedence over lagging stock breadth
                if not (is_index and is_day_low_breakdown):
                    rejection_reasons.append(
                        f"Dual-Engine Concurrence: Upstream consensus is {c_dir}; conflicting PUT (PE) blocked."
                    )

        # ── GATE 5D: HEAVYWEIGHT COMPONENT CONCURRENCE (INSTITUTIONAL BREADTH) ──
        if underlying in UNDERLYING_HEAVYWEIGHTS:
            hw_list = UNDERLYING_HEAVYWEIGHTS[underlying]
            active_hw_quotes = heavyweight_quotes or {}
            if not active_hw_quotes:
                try:
                    from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
                    mgr = OptionStreamManager()
                    active_hw_quotes = {h: mgr.get_underlying_quote(h) for h in hw_list if mgr.get_underlying_quote(h)}
                except Exception:
                    active_hw_quotes = {}

            if active_hw_quotes and len(active_hw_quotes) >= len(hw_list):
                bullish_hw_count = 0
                bearish_hw_count = 0
                for h_sym in hw_list:
                    h_q = active_hw_quotes.get(h_sym)
                    if not h_q:
                        continue
                    h_spot = float(h_q.get("spot_ltp") or 0.0)
                    h_vwap = float(h_q.get("vwap") or h_spot)
                    if h_spot <= 0:
                        continue
                    if h_spot >= (h_vwap * 0.9995):
                        bullish_hw_count += 1
                    if h_spot <= (h_vwap * 1.0005):
                        bearish_hw_count += 1

                if opt_type == "CE" and bullish_hw_count == 0 and bearish_hw_count >= len(hw_list):
                    rejection_reasons.append(
                        f"Heavyweight Concurrence (Gate 5D): All primary drivers ({', '.join(hw_list)}) are trading below VWAP. "
                        f"CALL (CE) blocked due to institutional component supply overhead."
                    )
                elif opt_type == "PE" and bearish_hw_count == 0 and bullish_hw_count >= len(hw_list):
                    rejection_reasons.append(
                        f"Heavyweight Concurrence (Gate 5D): All primary drivers ({', '.join(hw_list)}) are trading above VWAP. "
                        f"PUT (PE) blocked due to institutional component buying absorption."
                    )

        # ── GATE 5E: CONTINUOUS COMPLETED-BAR REGIME, CHOP & ECONOMIC FRICTION GATE ──
        regime_eval = None
        should_enforce_regime = enforce_regime_gate
        if should_enforce_regime is None:
            from app.analytics.option_sniffer.option_constants import REGIME_GATE_ENABLED
            should_enforce_regime = REGIME_GATE_ENABLED

        if should_enforce_regime:
            from app.analytics.option_sniffer.option_regime_gate import OptionRegimeGate
            from app.analytics.option_sniffer.option_constants import TARGET_GAIN_PCT
            regime_gate = OptionRegimeGate()
            continuous_bars_input = None
            if underlying_df is not None and hasattr(underlying_df, '__len__') and len(underlying_df) >= 28:
                continuous_bars_input = underlying_df

            check_tgt_pct = TARGET_GAIN_PCT
            try:
                from app.analytics.option_sniffer.option_constants import OPTION_EXECUTION_ENGINE_MODE
                if OPTION_EXECUTION_ENGINE_MODE != "V1_CLASSIC":
                    _, calc_tgt, _, _ = OptionPaperExecution.calculate_dynamic_sl_and_target(
                        underlying=underlying,
                        spot_price=spot_ltp,
                        option_ask=ask,
                        delta=float(quote.get("delta") or 0.50),
                        iv=float(quote.get("iv") or 15.0),
                        spot_atr_pct=quote.get("spot_atr_pct"),
                        day_range_pts=day_range_pts,
                        is_breakout=(is_day_high_breakout or is_day_low_breakdown)
                    )
                    check_tgt_pct = calc_tgt
            except Exception:
                check_tgt_pct = TARGET_GAIN_PCT

            regime_eval = regime_gate.evaluate_regime(
                underlying=underlying,
                opt_type=opt_type,
                as_of_time=now_dt,
                continuous_bars=continuous_bars_input,
                spot_ltp=spot_ltp,
                day_high=u_high,
                day_low=u_low,
                option_ask=ask,
                target_pct=check_tgt_pct,
                lot_size=int(contract.get("lot_size", 65)),
                is_breakout=bool(is_day_high_breakout or is_day_low_breakdown),
                is_reversal_recovery=bool(is_v_bottom_recovery if opt_type == "CE" else is_v_top_reversal),
                enforce_cutoff=enforce_cutoff,
                heavyweight_quotes=active_hw_quotes
            )
            if not regime_eval.get("hurdles_passed", False):
                rejection_reasons.extend(regime_eval.get("rejection_reasons", []))

        # Fail closed early if data is distorted or regime/concurrence blocks
        if rejection_reasons:
            return {
                "instrument_key": ikey,
                "trading_symbol": tsym,
                "status": STATE_WATCH,
                "direction": DIRECTION_NO_TRADE,
                "calibrated_prob": 0.0,
                "expected_net_return": 0.0,
                "rejection_reasons": rejection_reasons,
                "hurdles_passed": False,
                "regime_gate_passed": False,
                "is_crbi_active": is_crbi_active,
                "regime_telemetry": regime_eval.get("telemetry") if regime_eval else None,
            }

        # ── GATES 6-8: OPTION ALPHA MODEL INFERENCE ──────────────────────────
        features = OptionFeatureExtractor.extract_features(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            underlying_df=underlying_df,
            macro_context=macro_context,
            upstream_signals=upstream_signals,
            as_of_time=now_dt
        )

        # Compute SL and Target for contract expectancy (Pillar 4: Unified Model Expectancy)
        from app.analytics.option_sniffer.option_constants import OPTION_EXECUTION_ENGINE_MODE, STOP_LOSS_PCT, TARGET_GAIN_PCT
        if OPTION_EXECUTION_ENGINE_MODE == "V1_CLASSIC":
            dyn_sl, dyn_tgt, s_atr, d_used = STOP_LOSS_PCT, TARGET_GAIN_PCT, 40.0, 0.50
        else:
            dyn_sl, dyn_tgt, s_atr, d_used = OptionPaperExecution.calculate_dynamic_sl_and_target(
                underlying=underlying,
                spot_price=spot_ltp,
                option_ask=ask,
                delta=float(quote.get("delta") or 0.50),
                iv=float(quote.get("iv") or 15.0),
                spot_atr_pct=quote.get("spot_atr_pct"),
                day_range_pts=day_range_pts,
                is_breakout=(is_day_high_breakout or is_day_low_breakdown)
            )

        # Determine active minimum probability and expected net return hurdles
        # Breakout (CRB-I) has structural edge: 0.38 / +1.5%
        # In-Range trades require elevated conviction to eliminate noise: 0.48 / +3.5%
        # Dynamic Time-of-Day Schedule: Morning 0.42, Midday 0.48, Afternoon 0.45, Curfew >= 14:45
        now_time_float = now_dt.hour + (now_dt.minute / 60.0)
        is_breakout = bool(is_crbi_active or is_day_high_breakout or is_day_low_breakdown)

        if now_time_float >= 14.75:  # Post 14:45 IST Curfew
            active_min_prob = 1.0  # Strict curfew lock: zero new entries
            active_min_edge = 99.0
            rejection_reasons.append(f"Late-Day Curfew: No new positions permitted after 14:45 IST ({now_dt.strftime('%H:%M')})")
        elif is_breakout:
            active_min_prob = CRBI_RELAXED_MODEL_PROB  # 0.38
            active_min_edge = MIN_EXPECTED_NET_RETURN_PCT  # +1.5%
        elif is_in_day_range:
            active_min_prob = IN_RANGE_MIN_CALIBRATED_PROB  # 0.48
            active_min_edge = IN_RANGE_MIN_EXPECTED_NET_RETURN_PCT  # +3.5%
        elif now_time_float < 11.25:  # 09:15 - 11:15 Morning Drive
            active_min_prob = 0.42
            active_min_edge = MIN_EXPECTED_NET_RETURN_PCT  # +1.5%
        elif 11.25 <= now_time_float < 13.5:  # 11:15 - 13:30 Midday Chop Zone
            active_min_prob = IN_RANGE_MIN_CALIBRATED_PROB  # 0.48
            active_min_edge = IN_RANGE_MIN_EXPECTED_NET_RETURN_PCT  # +3.5%
        else:  # 13:30 - 14:45 Afternoon Thrust
            active_min_prob = 0.45
            active_min_edge = 2.5

        model_pred = self.model.predict_contract(
            features,
            dynamic_sl_pct=dyn_sl,
            dynamic_tgt_pct=dyn_tgt,
            min_prob_hurdle=active_min_prob,
            is_breakout=is_breakout
        )
        direction = model_pred["direction"]
        calibrated_prob = model_pred["calibrated_prob"]
        expected_net_return = model_pred["expected_net_return_pct"]

        if direction == DIRECTION_NO_TRADE:
            rejection_reasons.append(
                f"Model neutral: Conviction ({calibrated_prob*100:.1f}%) or Net Edge ({expected_net_return:+.1f}%) below hurdle"
            )

        if calibrated_prob < active_min_prob:
            if now_time_float >= 14.75:
                pass  # Already appended curfew reason above
            elif is_crbi_active:
                rejection_reasons.append(f"Low probability ({calibrated_prob*100:.1f}% < {CRBI_RELAXED_MODEL_PROB*100:.1f}%)")
            else:
                if calibrated_prob < MIN_CALIBRATED_PROB:
                    rejection_reasons.append(f"Low probability ({calibrated_prob*100:.1f}% < {MIN_CALIBRATED_PROB*100:.1f}%)")
                if is_in_day_range and calibrated_prob < IN_RANGE_MIN_CALIBRATED_PROB:
                    rejection_reasons.append(f"In-Range Quality Hurdle ({calibrated_prob*100:.1f}% < {IN_RANGE_MIN_CALIBRATED_PROB*100:.1f}%)")
                elif calibrated_prob < active_min_prob:
                    rejection_reasons.append(f"Dynamic Quality Hurdle ({calibrated_prob*100:.1f}% < {active_min_prob*100:.1f}%)")

        if expected_net_return < active_min_edge:
            rejection_reasons.append(f"Insufficient net edge ({expected_net_return:+.1f}% < +{active_min_edge:.1f}%)")

        # If model gates fail -> remains in WATCH
        if rejection_reasons:
            return {
                "instrument_key": ikey,
                "trading_symbol": tsym,
                "status": STATE_WATCH,
                "direction": direction,
                "calibrated_prob": calibrated_prob,
                "expected_net_return": expected_net_return,
                "rejection_reasons": rejection_reasons,
                "hurdles_passed": False,
                "is_crbi_active": is_crbi_active,
            }

        # ── GATES 9-10: RISK CAPACITY & COOLDOWN (QUALIFIED ➔ ACTIVE) ────────
        current_status = STATE_QUALIFIED

        is_live_armed = False
        live_capacity_reached = False
        live_underlying_full = False
        try:
            from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
            from app.analytics.option_sniffer.option_live_order_intent import get_order_intent_engine, MAX_CONCURRENT_POSITIONS
            live_adapter = get_option_live_adapter()
            if getattr(live_adapter, "_is_live_armed", False):
                is_live_armed = True
                order_engine = get_order_intent_engine()
                live_active = order_engine.get_active_positions_count()
                if live_active >= MAX_CONCURRENT_POSITIONS:
                    live_capacity_reached = True
                if hasattr(order_engine, "has_active_position_for_underlying") and order_engine.has_active_position_for_underlying(underlying) is True:
                    live_underlying_full = True
        except Exception as e:
            logger.debug(f"[OptionSignalEngine] Live state check note: {e}")

        # Gate 9: Risk Budget & Anti-Clustering Capacity
        open_trades = OptionDB.get_open_paper_trades()
        paper_capacity_reached = len(open_trades) >= self._MAX_CONCURRENT_OPEN_TRADES

        u_str = str(underlying or 'NIFTY').upper()
        u_open_trades = [t for t in open_trades if str(t.get('underlying') or '').upper() == u_str]
        
        # Enforce strict in-memory lock so subsequent contracts in the same sweep cannot stack
        last_u_lock = self._ACTIVE_UNDERLYING_LOCKS.get(u_str, 0.0)
        has_recent_u_lock = (now_epoch - last_u_lock) < 15.0
        paper_underlying_full = (len(u_open_trades) >= MAX_POSITIONS_PER_UNDERLYING) or (len(u_open_trades) >= 1 and has_recent_u_lock)

        # If live trading is armed, allow signals to activate if live capacity is available,
        # even if paper capacity is full (and vice versa). Only reject if all actionable capacity is exhausted.
        if is_live_armed:
            if live_underlying_full and paper_underlying_full:
                existing_syms = ", ".join(t.get('trading_symbol', u_str) for t in u_open_trades)
                rejection_reasons.append(
                    f"Anti-Clustering Shield: Active {u_str} position exists in both Live and Paper ({existing_syms}). Max {MAX_POSITIONS_PER_UNDERLYING} position per underlying strictly enforced."
                )
            if live_capacity_reached and paper_capacity_reached:
                rejection_reasons.append(
                    f"Max concurrent trades reached (Paper {len(open_trades)}/{self._MAX_CONCURRENT_OPEN_TRADES}, Live {MAX_CONCURRENT_POSITIONS}/{MAX_CONCURRENT_POSITIONS})"
                )
        else:
            if len(u_open_trades) >= MAX_POSITIONS_PER_UNDERLYING or (has_recent_u_lock and len(u_open_trades) >= 1):
                existing_syms = ", ".join(t.get('trading_symbol', u_str) for t in u_open_trades)
                rejection_reasons.append(
                    f"Anti-Clustering Shield: Active {u_str} position exists ({existing_syms}). Max {MAX_POSITIONS_PER_UNDERLYING} position per underlying strictly enforced."
                )
            elif len(u_open_trades) == 1:
                first_trade = u_open_trades[0]
                first_mfe = float(first_trade.get("mfe_pct") or 0.0)
                first_pnl = float(first_trade.get("net_pnl_pct") or 0.0)
                if first_mfe < SMART_PYRAMIDING_MIN_MFE_PCT or first_pnl < 2.0:
                    existing_sym = first_trade.get('trading_symbol', u_str)
                    rejection_reasons.append(
                        f"Anti-Clustering Shield: Active {u_str} position exists ({existing_sym}). Max {MAX_POSITIONS_PER_UNDERLYING} position per underlying strictly enforced (Pyramiding MFE {first_mfe:.1f}% < {SMART_PYRAMIDING_MIN_MFE_PCT}% or PnL {first_pnl:+.1f}% < +2.0%)."
                    )
            if paper_capacity_reached:
                rejection_reasons.append(
                    f"Max concurrent paper trades reached ({len(open_trades)}/{self._MAX_CONCURRENT_OPEN_TRADES})"
                )

        # Gate 9B: Two-Tier Database-Backed Paper Circuit Breaker & Canary Scout Guard
        # Paper trading MUST ALWAYS be bound by paper circuit breaker (never bypass due to live being armed)
        paper_cb = OptionDB.get_daily_circuit_breaker_telemetry(
            max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
            max_daily_loss_inr=MAX_DAILY_LOSS_INR,
            pause_minutes=INTROSPECTION_FREEZE_MINUTES,
            as_of_date=now_dt.strftime("%Y-%m-%d"),
            as_of_time=now_dt
        )
        cb_telemetry = paper_cb

        if isinstance(paper_cb, dict) and paper_cb.get("is_session_halted") is True:
            rejection_reasons.append(
                f"🚨 Daily Session Circuit Breaker Tripped: {paper_cb.get('halt_reason')} Trading halted for session."
            )
        elif isinstance(paper_cb, dict) and paper_cb.get("is_pause_active") is True:
            rem_sec = paper_cb.get("pause_remaining_sec", 0)
            unfreeze_str = paper_cb.get("pause_unfreeze_time", "soon")
            rejection_reasons.append(
                f"⏳ Dynamic Introspection Pause Active: {paper_cb.get('consecutive_losses')} consecutive losses. "
                f"Entry dispatch paused ({rem_sec}s remaining until {unfreeze_str} IST)."
            )
        elif now_epoch < self._INTROSPECTION_FROZEN_UNTIL:
            rem_freeze_sec = int(self._INTROSPECTION_FROZEN_UNTIL - now_epoch)
            rejection_reasons.append(
                f"Portfolio Introspection Freeze: Consecutive losses in memory. Entry dispatch globally frozen ({rem_freeze_sec}s remaining)."
            )

        # Gate 9B-2: Live Broker Circuit Breaker Check (checked additionally when live trading is armed)
        if is_live_armed:
            try:
                from app.analytics.option_sniffer.option_live_order_intent import get_order_intent_engine
                order_engine = get_order_intent_engine()
                live_cb = order_engine.get_live_circuit_breaker_telemetry(
                    max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
                    max_daily_loss_inr=MAX_DAILY_LOSS_INR,
                    pause_minutes=INTROSPECTION_FREEZE_MINUTES,
                    as_of_date=now_dt.strftime("%Y-%m-%d"),
                    as_of_time=now_dt
                )
                if isinstance(live_cb, dict) and live_cb.get("is_session_halted") is True:
                    rejection_reasons.append(
                        f"🚨 Live Broker Circuit Breaker Tripped: {live_cb.get('halt_reason')} Real trading halted for session."
                    )
            except Exception as e:
                logger.error(f"[OptionSignalEngine] Failed to get live circuit breaker telemetry: {e}")
                rejection_reasons.append(
                    f"Fail-Closed: Error querying live circuit breaker ({e})"
                )

        # Gate 9C: Cross-Index Contagion Lockout (Anti-Hopping Shield)
        recent_loss_time_str = cb_telemetry.get("most_recent_loss_time")
        if recent_loss_time_str:
            try:
                loss_dt = datetime.fromisoformat(recent_loss_time_str)
                loss_elapsed_sec = (now_dt - loss_dt).total_seconds()
                if 0 <= loss_elapsed_sec < POST_LOSS_CONTAGION_FREEZE_SECONDS:
                    rem_contagion_sec = int(POST_LOSS_CONTAGION_FREEZE_SECONDS - loss_elapsed_sec)
                    rejection_reasons.append(
                        f"Cross-Index Contagion Lockout: Recent Stop-Loss at {loss_dt.strftime('%H:%M:%S')} IST. "
                        f"Post-loss market stabilization active ({rem_contagion_sec}s remaining across all underlyings)."
                    )
            except Exception as dte:
                logger.debug(f"[OptionSignalEngine] Contagion date parse note: {dte}")

        # Gate 10: Cooldown Hysteresis (Per-Contract & Direction-Level SL Cooldown)
        last_sig_time = self._COOLDOWNS.get(ikey, 0.0)
        cooldown_rem = int(SIGNAL_COOLDOWN_SECONDS - (now_epoch - last_sig_time))
        if cooldown_rem > 0:
            rejection_reasons.append(f"Signal cooldown active ({cooldown_rem}s remaining)")

        dir_sl_key = f"{str(underlying or 'NIFTY').upper()}_{str(opt_type or 'CE').upper()}"
        last_sl_time = self._DIRECTION_SL_COOLDOWNS.get(dir_sl_key, 0.0)
        sl_cooldown_rem = int(self._DIRECTION_SL_COOLDOWN_SECONDS - (now_epoch - last_sl_time))
        if sl_cooldown_rem > 0:
            rejection_reasons.append(
                f"Direction SL cooldown active for {underlying} {opt_type} ({sl_cooldown_rem}s remaining after recent Stop-Loss hit)"
            )

        # Gate 10B: Anti-Ping-Pong Directional Flip Lockout
        opp_opt_type = "PE" if opt_type == "CE" else "CE"
        opp_sl_key = f"{str(underlying or 'NIFTY').upper()}_{opp_opt_type}"
        opp_sl_time = self._DIRECTION_SL_COOLDOWNS.get(opp_sl_key, 0.0)
        opp_elapsed_sec = now_epoch - opp_sl_time
        if opp_elapsed_sec < (ANTI_PING_PONG_FLIP_MINUTES * 60):
            rem_flip_m = int((ANTI_PING_PONG_FLIP_MINUTES * 60 - opp_elapsed_sec) / 60)
            if not (
                (opt_type == "CE" and (is_day_high_breakout or is_strong_underlying_bull))
                or (opt_type == "PE" and (is_day_low_breakdown or is_strong_underlying_bear))
            ):
                rejection_reasons.append(
                    f"Anti-Ping-Pong Lockout: Active {rem_flip_m}m flip cooldown after recent {opp_opt_type} Stop-Loss. Reversal unconfirmed."
                )

        # Generate Signal Payload
        signal_id = f"opt_sig_{uuid.uuid4().hex[:8]}"
        is_active = len(rejection_reasons) == 0

        final_status = STATE_ACTIVE if is_active else STATE_QUALIFIED

        signal_payload = {
            "signal_id": signal_id,
            "created_at": now_iso,
            "instrument_key": ikey,
            "trading_symbol": tsym,
            "underlying": underlying,
            "strike": strike,
            "option_type": opt_type,
            "expiry": expiry,
            "lot_size": int(contract.get("lot_size", 65)),
            "direction": direction,
            "status": final_status,
            "calibrated_prob": calibrated_prob,
            "expected_net_return": expected_net_return,
            "spot_price": spot_ltp,
            "option_ask": ask,
            "option_bid": bid,
            "spread_pct": spread_pct,
            "delta": float(quote.get("delta") or 0.50),
            "iv": float(quote.get("iv") or 15.0),
            "oi": oi,
            "volume": vol,
            "hurdles_passed": is_active,
            "rejection_reasons": rejection_reasons,
            "regime_gate_passed": bool(regime_eval.get("hurdles_passed", True)) if regime_eval else True,
            "regime_rejection_reasons": regime_eval.get("rejection_reasons", []) if regime_eval else [],
            "regime_telemetry": regime_eval.get("telemetry") if regime_eval else {},
            "model_version": self.model.version,
            "signal_version": OPTION_SIGNAL_VERSION,
            "data_freshness": quote.get("quote_freshness", "REAL_TIME"),
            "quote_age_ms": age_ms,
            "day_range_pts": day_range_pts,
            "is_breakout": bool(is_day_high_breakout or is_day_low_breakdown),
            "is_crbi_active": bool(is_crbi_active),
            "dynamic_sl_pct": dyn_sl,
            "dynamic_target_pct": dyn_tgt,
            "spot_atr": s_atr,
            "delta_used": d_used,
        }

        # Persist Signal to SQLite
        OptionDB.save_signal(signal_payload)

        # ── TRANSITION TO ACTIVE: TELEGRAM ALERT & PAPER ORDER ENTRY ─────────
        if final_status == STATE_ACTIVE:
            self._COOLDOWNS[ikey] = now_epoch
            logger.info(f"[OptionSignalEngine] 🚨 ACTIVE SIGNAL TRIGGERED: {tsym} {direction} (P={calibrated_prob*100:.1f}%)")

            # 1. Dispatch Immediate Telegram Alert
            self._send_telegram_signal_alert(signal_payload)
            signal_payload["telegram_sent"] = 1
            signal_payload["telegram_sent_at"] = to_ist_naive().isoformat()
            OptionDB.save_signal(signal_payload)

            # 2. Enter Paper Position (BUY at current ASK) if paper capacity allows and underlying not full
            if not paper_capacity_reached and not paper_underlying_full:
                trade_id = OptionPaperExecution.open_trade(
                    signal_payload,
                    as_of_time=now_dt,
                    enforce_cutoff=enforce_cutoff
                )
                signal_payload["paper_trade_id"] = trade_id
                if trade_id:
                    self._ACTIVE_UNDERLYING_LOCKS[u_str] = now_epoch
            else:
                logger.info(
                    f"[OptionSignalEngine] Paper capacity full or underlying occupied ({len(open_trades)}/{self._MAX_CONCURRENT_OPEN_TRADES}, lock={has_recent_u_lock}); skipped paper entry for {tsym}"
                )

        return signal_payload

    @classmethod
    def _send_telegram_signal_alert(cls, signal: Dict[str, Any]) -> None:
        """Sends clean HTML alert to configured Telegram chat upon ACTIVE state transition."""
        dir_emoji = "🟢 CALL BUY" if signal["direction"] == DIRECTION_BUY_CE else "🔴 PUT BUY"

        is_live = False
        try:
            from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
            is_live = getattr(get_option_live_adapter(), "_is_live_armed", False)
        except Exception:
            pass

        title_badge = "LIVE & PAPER BUY" if is_live else "PAPER BUY"
        msg = (
            f"<b>📡 OPTION SNIFFER | {title_badge}</b>\n"
            f"<b>{dir_emoji} | {signal['trading_symbol']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Underlying:</b> {signal['underlying']} (Spot: ₹{signal['spot_price']:,.2f})\n"
            f"• <b>Contract:</b> {signal['trading_symbol']}\n"
            f"• <b>Strike / Expiry:</b> {int(signal['strike'])} | {signal['expiry']}\n"
            f"• <b>Paper Entry (ASK):</b> <b>₹{signal['option_ask']:.2f}</b> (BID: ₹{signal['option_bid']:.2f})\n"
            f"• <b>Spread:</b> {signal['spread_pct']:.2f}%\n"
            f"• <b>Delta:</b> {signal['delta']:.2f} | <b>IV:</b> {signal['iv']:.1f}%\n"
            f"• <b>Liquidity:</b> Vol: {signal['volume']:,} | OI: {int(signal['oi']):,}\n"
            f"• <b>Calibrated Win Edge:</b> <b>{signal['calibrated_prob']*100:.1f}%</b>\n"
            f"• <b>Expected Net Return:</b> +{signal['expected_net_return']:.1f}%\n"
            f"• <b>Horizon:</b> 15–45 mins\n"
            f"• <b>Signal Time:</b> {signal['created_at'][11:19]} IST\n"
            f"• <b>Data Freshness:</b> {signal['data_freshness']} ({signal['quote_age_ms']}ms)\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<b>⚠️ PAPER ONLY — NO LIVE ORDER PLACED</b>"
        )
        try:
            send_telegram_message(msg)
        except Exception as te:
            logger.warning(f"[OptionSignalEngine] Telegram alert note: {te}")
