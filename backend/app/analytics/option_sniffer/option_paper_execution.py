"""
Option Sniffer: Institutional Dynamic Paper Execution Lifecycle

Strictly Paper-Only:
  - Zero live order API calls.
  - Paper Entry: BUY at current ASK.
  - Paper Exit: SELL at current BID.
  - Complete lifecycle tracking: Entry, Exit, MFE, MAE, Gross P&L, Upstox statutory fees,
    slippage, Net P&L, holding duration, and Telegram closure reports.
"""

import logging
import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from app.analytics.option_sniffer.option_constants import (
    calculate_option_trade_costs,
    STOP_LOSS_PCT,
    TARGET_GAIN_PCT,
    TRAILING_STOP_TRIGGER_PCT,
    TRAILING_STOP_DISTANCE_PCT,
    BREAKEVEN_PROFIT_TRIGGER_PCT,
    BREAKEVEN_FLOOR_PCT,
    EARLY_BREAKEVEN_TRIGGER_PCT,
    EARLY_BREAKEVEN_FLOOR_PCT,
    CHANDELIER_TRAIL_ACTIVE,
    CHANDELIER_TRIGGER_TIER1_PCT,
    CHANDELIER_DISTANCE_TIER1_PCT,
    CHANDELIER_TRIGGER_TIER2_PCT,
    CHANDELIER_DISTANCE_TIER2_PCT,
    CHANDELIER_TRIGGER_TIER3_PCT,
    CHANDELIER_DISTANCE_TIER3_PCT,
    CHANDELIER_TRIGGER_TIER4_PCT,
    CHANDELIER_DISTANCE_TIER4_PCT,
    TARGET_LOCK_FLOOR_OFFSET_PCT,
    CHANDELIER_DISTANCE_TIER3_5_PCT,
    MAX_SINGLE_TRADE_LOSS_INR,
    ENABLE_4PILLAR_CANARY_ENGINE,
    MAX_HOLDING_MINUTES,
    OPTION_MODEL_VERSION,
    OPTION_SIGNAL_VERSION,
    is_option_trading_window,
    is_option_entry_window,
    to_ist_naive,
    OPTION_EXECUTION_ENGINE_MODE,
    DYNAMIC_SL_MIN_PCT,
    DYNAMIC_SL_MAX_PCT,
    DYNAMIC_RR_RATIO,
    DYNAMIC_TGT_MIN_PCT,
    DYNAMIC_TGT_MAX_PCT,
    DYNAMIC_SCALP_TGT_MIN_PCT,
    DYNAMIC_SCALP_TGT_MAX_PCT,
    NARROW_RANGE_THRESHOLD_PTS,
    STRUCTURE_INVALIDATION_BUFFER_PCT,
    STRUCTURE_INVALIDATION_MIN_PTS_NIFTY,
    STRUCTURE_INVALIDATION_MIN_PTS_BANKNIFTY,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DAILY_LOSS_INR,
    INTROSPECTION_FREEZE_MINUTES,
    ENABLE_APEX_QUANT_ENGINE,
    THETA_STAGNATION_TIMEOUT_MINUTES,
    THETA_STAGNATION_MIN_MFE_PCT,
    THETA_STAGNATION_EXIT_LOSS_CAP_PCT,
    HOUSE_MONEY_SCALE_IN_ACTIVE,
    HOUSE_MONEY_SCALE_IN_TRIGGER_MFE_PCT,
    HOUSE_MONEY_UNIFIED_FLOOR_PCT,
    HOUSE_MONEY_MAX_NOTIONAL_INR,
    POST_HARVEST_MIN_MFE_PCT,
)
from app.analytics.option_sniffer.option_db import OptionDB
from app.analytics.telegram_notifier import send_telegram_message

logger = logging.getLogger(__name__)


class OptionPaperExecution:
    """Manages paper-trade execution lifecycle and continuous position surveillance."""

    @classmethod
    def calculate_dynamic_sl_and_target(
        cls,
        underlying: str,
        spot_price: float,
        option_ask: float,
        delta: Optional[float] = None,
        iv: Optional[float] = None,
        spot_atr_pct: Optional[float] = None,
        day_range_pts: Optional[float] = None,
        is_breakout: bool = False
    ) -> Tuple[float, float, float, float]:
        """
        Computes institutional underlying-anchored dynamic Stop-Loss and Target percentages.
        Anchored to Underlying Spot 15m ATR mapped through option Delta:
            Distance_opt = Spot_ATR * |Delta|
            Dynamic_SL% = (Distance_opt / Option_Ask) * 100
        Clamped to [DYNAMIC_SL_MIN_PCT, DYNAMIC_SL_MAX_PCT] (12.0% - 24.0%).

        Pillar 2: Regime-Adaptive Target Scaling:
            - Rangebound/Narrow Day (day_range_pts < 110.0 and not breakout):
              Clamps dynamic target to Scalp Mode (+18.0% to +22.0%) to capture the empirical
              71.9% 30-pt pop before mean reversion.
            - Trend Expansion Day (day_range_pts >= 110.0 or confirmed breakout):
              Allows dynamic target to expand to Runner Mode (+25.0% to +45.0%).

        Returns: (final_sl_pct, final_tgt_pct, spot_atr_points, delta_used)
        """
        # 1. Delta used (clamped between 0.35 and 0.70 for ATM/near-ATM options)
        raw_delta = abs(float(delta if delta is not None else 0.50))
        if raw_delta <= 0.01 or raw_delta > 1.0:
            raw_delta = 0.50
        delta_used = round(max(0.35, min(0.70, raw_delta)), 2)

        # 2. Underlying 15m Spot ATR in points
        is_banknifty = "BANK" in underlying.upper()
        if spot_atr_pct and spot_atr_pct > 0 and spot_price > 0:
            intraday_atr_pct = max(0.12, min(0.35, spot_atr_pct * 0.22))
            spot_atr_points = round(spot_price * (intraday_atr_pct / 100.0), 1)
        else:
            spot_atr_points = 100.0 if is_banknifty else 40.0

        # Enforce realistic bounds on Spot ATR points
        if is_banknifty:
            spot_atr_points = max(70.0, min(160.0, spot_atr_points))
        else:
            spot_atr_points = max(30.0, min(65.0, spot_atr_points))

        # 3. First-order Taylor mapping to option premium space
        option_stop_distance = spot_atr_points * delta_used

        # 4. Convert to Percentage of Entry Ask
        if option_ask > 0:
            raw_sl_pct = round((option_stop_distance / option_ask) * 100.0, 1)
        else:
            raw_sl_pct = STOP_LOSS_PCT

        # 5. Volatility & Liquidity Clamping
        final_sl_pct = max(DYNAMIC_SL_MIN_PCT, min(DYNAMIC_SL_MAX_PCT, raw_sl_pct))

        # 6. Target Scaling (Pillar 2: Regime-Adaptive Target Scaling)
        raw_tgt_pct = round(final_sl_pct * DYNAMIC_RR_RATIO, 1)
        narrow_thresh = (NARROW_RANGE_THRESHOLD_PTS * 2.5) if is_banknifty else NARROW_RANGE_THRESHOLD_PTS
        if day_range_pts is not None and day_range_pts < narrow_thresh and not is_breakout:
            # Rangebound / Narrow Day: Scalp mode clamped to +18.0% to +22.0%
            final_tgt_pct = max(DYNAMIC_SCALP_TGT_MIN_PCT, min(DYNAMIC_SCALP_TGT_MAX_PCT, raw_tgt_pct))
        else:
            # Trend Expansion or Open Range: Runner mode clamped to +25.0% to +45.0%
            final_tgt_pct = max(DYNAMIC_TGT_MIN_PCT, min(DYNAMIC_TGT_MAX_PCT, raw_tgt_pct))

        return final_sl_pct, final_tgt_pct, spot_atr_points, delta_used

    @classmethod
    def open_trade(
        cls,
        signal: Dict[str, Any],
        as_of_time: Optional[datetime] = None,
        enforce_cutoff: bool = False
    ) -> Optional[str]:
        """
        Executes a paper order entry strictly at current ask price.
        Records entry in SQLite and logs event.
        Rejects any entry at or past 15:15 IST cutoff when enforce_cutoff is True or as_of_time is provided.
        """
        trade_id = f"opt_paper_{uuid.uuid4().hex[:8]}"
        entry_ask = float(signal.get("option_ask") or 0.0)
        entry_bid = float(signal.get("option_bid") or 0.0)

        if entry_ask <= 0:
            logger.warning(f"[OptionPaperExecution] Cannot enter trade without valid ask price: {signal}")
            return None

        # Intraday cutoff guards: Reject new entries outside trading hours or outside golden entry window
        now_dt = to_ist_naive(as_of_time)
        if (enforce_cutoff or as_of_time is not None):
            if not is_option_trading_window(now_dt):
                logger.info(
                    f"[OptionPaperExecution] Blocked open_trade after 15:15 IST cutoff: {signal.get('trading_symbol')}"
                )
                return None
            if not is_option_entry_window(now_dt):
                logger.info(
                    f"[OptionPaperExecution] Blocked open_trade outside golden entry window (09:30-13:15 IST): {signal.get('trading_symbol')}"
                )
                return None

        # Hard Circuit Breaker Interlock: Never open paper trade if paper session circuit breaker is halted
        cb = OptionDB.get_daily_circuit_breaker_telemetry(as_of_time=now_dt)
        if isinstance(cb, dict) and cb.get("is_session_halted"):
            logger.warning(
                f"[OptionPaperExecution] 🛑 Blocked paper trade open: Circuit Breaker is HALTED ({cb.get('halt_reason')})"
            )
            return None

        # Assign Dynamic or Classic SL and Target based on engine mode
        engine_mode = OPTION_EXECUTION_ENGINE_MODE
        if engine_mode == "DYNAMIC_V2":
            dyn_sl, dyn_tgt, s_atr, d_used = cls.calculate_dynamic_sl_and_target(
                underlying=signal.get("underlying", "NIFTY"),
                spot_price=float(signal.get("spot_price") or 0.0),
                option_ask=entry_ask,
                delta=signal.get("delta"),
                iv=signal.get("iv"),
                spot_atr_pct=signal.get("spot_atr_pct"),
                day_range_pts=signal.get("day_range_pts"),
                is_breakout=bool(signal.get("is_breakout", False))
            )
        else:
            dyn_sl, dyn_tgt, s_atr, d_used = STOP_LOSS_PCT, TARGET_GAIN_PCT, 40.0, 0.50

        lot_size = int(signal.get("lot_size") or 65)
        dyn_sl = cls.calculate_rupee_capped_sl_pct(entry_ask, lot_size, dyn_sl)
        trade_obj = {
            "trade_id": trade_id,
            "signal_id": signal.get("signal_id"),
            "instrument_key": signal["instrument_key"],
            "trading_symbol": signal["trading_symbol"],
            "underlying": signal["underlying"],
            "option_type": signal["option_type"],
            "strike": signal["strike"],
            "expiry": signal["expiry"],
            "lot_size": lot_size,
            "quantity": lot_size,
            "entry_time": now_dt.isoformat(),
            "entry_bid": entry_bid,
            "entry_ask": entry_ask,
            "entry_spot_price": float(signal.get("spot_price") or 0.0),
            "model_version": signal.get("model_version", OPTION_MODEL_VERSION),
            "signal_version": signal.get("signal_version", OPTION_SIGNAL_VERSION),
            "data_freshness": signal.get("data_freshness", "REAL_TIME"),
            "engine_version": engine_mode,
            "dynamic_sl_pct": dyn_sl,
            "dynamic_target_pct": dyn_tgt,
            "spot_atr": s_atr,
            "delta_used": d_used,
        }

        success = OptionDB.create_paper_trade(trade_obj)
        if success:
            logger.info(
                f"[OptionPaperExecution] 📝 Paper BUY opened ({engine_mode}): {trade_obj['trading_symbol']} "
                f"Qty={lot_size} @ ASK ₹{entry_ask:.2f} | SL: -{dyn_sl:.1f}% | TGT: +{dyn_tgt:.1f}% "
                f"(Spot ATR: {s_atr} pts, Delta: {d_used}) (Trade ID: {trade_id})"
            )
            return trade_id
        return None

    @classmethod
    def calculate_rupee_capped_sl_pct(
        cls,
        entry_ask: float,
        lot_size: int,
        base_sl_pct: float = STOP_LOSS_PCT,
        max_loss_inr: float = MAX_SINGLE_TRADE_LOSS_INR
    ) -> float:
        """
        Clamps percentage stop-loss so a single 1-lot option trade on a high-premium strike
        never exceeds max_loss_inr (default ₹2,200.00), protecting the ₹3,500 daily portfolio cap.
        """
        notional = float(entry_ask) * float(lot_size or 65)
        if notional <= 0 or max_loss_inr <= 0:
            return float(base_sl_pct)
        rupee_cap_pct = round((max_loss_inr / notional) * 100.0, 2)
        return float(min(base_sl_pct, max(8.0, rupee_cap_pct)))

    @classmethod
    def calculate_chandelier_stop(
        cls,
        entry_ask: float,
        current_peak: float,
        mfe_pct: float,
        target_hurdle: Optional[float] = None
    ) -> Optional[float]:
        """
        Computes continuous ATR/volatility-calibrated chandelier trailing stop.
        Dynamically tightens distance below peak as MFE expands into profit runner territory.
        When target_hurdle is provided and mfe_pct >= target_hurdle (+35%), enforces a
        Target-Lock guaranteed floor at (target_hurdle - TARGET_LOCK_FLOOR_OFFSET_PCT)% (+34.0%)
        while allowing the option to continue running uncapped via Tier 3.5/4 trailing ratchets.
        Tier 4 (Multi-Bagger >= +60%): Trail 4.0% below peak.
        Tier 3.5 (Target-Locked Runner >= target_hurdle): Floor at target_hurdle - 1.0%, trail 5.5% below peak.
        Tier 3 (Runner >= +30%): Trail 4.5% below peak (guarantees >= +24.15% locked).
        Tier 2 (Expansion >= +22%): Trail 6.0% below peak (guarantees >= +14.68% locked).
        Tier 1 (Trend Initiation >= +15%): Trail 8.0% below peak (guarantees >= +5.80% locked).
        Early Breakeven (>= +15.0%): Floor at Entry + 6.0%.
        """
        min_trigger = min(EARLY_BREAKEVEN_TRIGGER_PCT, CHANDELIER_TRIGGER_TIER1_PCT)
        if mfe_pct < min_trigger:
            return None

        # Breakeven floor (+6.0%) guaranteed once EARLY_BREAKEVEN_TRIGGER_PCT (+15%) is reached
        floor_stop = entry_ask * (1.0 + EARLY_BREAKEVEN_FLOOR_PCT / 100.0) if mfe_pct >= EARLY_BREAKEVEN_TRIGGER_PCT else 0.0

        # Target-Lock Floor: once MFE reaches target_hurdle (e.g. +35%), lock in (target_hurdle - 1.0%) floor (+34.0%)
        if target_hurdle is not None and target_hurdle > 0 and mfe_pct >= target_hurdle:
            locked_target_pct = max(EARLY_BREAKEVEN_FLOOR_PCT, target_hurdle - TARGET_LOCK_FLOOR_OFFSET_PCT)
            target_floor_stop = entry_ask * (1.0 + locked_target_pct / 100.0)
            floor_stop = max(floor_stop, target_floor_stop)

        if not CHANDELIER_TRAIL_ACTIVE:
            return floor_stop if floor_stop > 0 else None

        if mfe_pct >= CHANDELIER_TRIGGER_TIER4_PCT:
            trail_stop = current_peak * (1.0 - CHANDELIER_DISTANCE_TIER4_PCT / 100.0)
        elif target_hurdle is not None and target_hurdle > 0 and mfe_pct >= target_hurdle:
            trail_stop = current_peak * (1.0 - CHANDELIER_DISTANCE_TIER3_5_PCT / 100.0)
        elif mfe_pct >= CHANDELIER_TRIGGER_TIER3_PCT:
            trail_stop = current_peak * (1.0 - CHANDELIER_DISTANCE_TIER3_PCT / 100.0)
        elif mfe_pct >= CHANDELIER_TRIGGER_TIER2_PCT:
            trail_stop = current_peak * (1.0 - CHANDELIER_DISTANCE_TIER2_PCT / 100.0)
        elif mfe_pct >= CHANDELIER_TRIGGER_TIER1_PCT:
            trail_stop = current_peak * (1.0 - CHANDELIER_DISTANCE_TIER1_PCT / 100.0)
        else:
            trail_stop = floor_stop

        return max(floor_stop, trail_stop)

    @classmethod
    def evaluate_exit_conditions(
        cls,
        trade: Dict[str, Any],
        current_bid: float,
        quote: Dict[str, Any],
        now_dt: datetime,
        underlying_quotes_map: Optional[Dict[str, Any]] = None,
        enforce_cutoff: bool = False,
        is_aggregate_halt: bool = False
    ) -> Optional[str]:
        """
        Evaluates rule-based exit criteria for an active paper trade:
          1. Portfolio Aggregate Circuit Breaker Halt
          2. Take Profit Target (Dynamic, Classic, or Target-Locked Uncapped Runner)
          3. Hard Stop Loss (Dynamic, Classic, or Per-Trade Rupee Capped)
          4. Chandelier Trailing Stop & Ratchet Protection
          5. Underlying Structure Invalidation (Pillar 1: Loss Truncation)
             - Enforces 180s grace period for structural invalidation ONLY
             - Validates benchmark against session benchmark (or valid VWAP != spot_ltp)
             - Requires dual confirmation: adverse spot vs benchmark AND adverse spot >= min_pts
          6. Theta Stagnation Stop (Apex Quant Velocity Protection)
          7. Intraday Max Holding Time (45 min)
          8. Session End Square-off (>= 15:15 IST)
        """
        entry_ask = float(trade.get("initial_entry_ask") or trade["entry_ask"])
        is_scaled_in = int(trade.get("scale_in_count") or 0) >= 1
        prev_peak = float(trade.get("peak_price") or entry_ask)
        prev_mfe_pct = round(((prev_peak - entry_ask) / entry_ask * 100.0), 2) if entry_ask > 0 else 0.0
        current_peak = max(prev_peak, current_bid)
        current_trough = min(float(trade.get("trough_price") or entry_ask), current_bid)

        mfe_pct = round(((current_peak - entry_ask) / entry_ask * 100.0), 2)
        mae_pct = round(((current_trough - entry_ask) / entry_ask * 100.0), 2)

        try:
            entry_dt = datetime.fromisoformat(trade["entry_time"])
            duration_sec = int((now_dt - entry_dt).total_seconds())
            duration_min = duration_sec / 60.0
        except Exception:
            duration_sec = 0
            duration_min = 0.0

        current_pnl_pct = ((current_bid - entry_ask) / entry_ask) * 100.0
        exit_reason = None

        # 0. Portfolio Aggregate Loss Ceiling Breach -> Immediate Emergency Square-Off
        if is_aggregate_halt:
            return "CIRCUIT_BREAKER_HALT"

        # Determine dynamic or classic exit hurdles
        engine_ver = trade.get("engine_version") or OPTION_EXECUTION_ENGINE_MODE
        if engine_ver == "DYNAMIC_V2" and trade.get("dynamic_target_pct") and trade.get("dynamic_sl_pct"):
            target_hurdle = float(trade["dynamic_target_pct"])
            stop_hurdle = float(trade["dynamic_sl_pct"])
            ratchet_trigger = min(TRAILING_STOP_TRIGGER_PCT, max(12.0, round(target_hurdle * 0.50, 1)))
        else:
            target_hurdle = float(trade.get("dynamic_target_pct") or TARGET_GAIN_PCT)
            stop_hurdle = float(trade.get("dynamic_sl_pct") or STOP_LOSS_PCT)
            ratchet_trigger = TRAILING_STOP_TRIGGER_PCT

        # Enforce Per-Trade Rupee Risk Cap anchored to initial 1-lot risk (so House-Money scale-in does not distort base SL)
        base_lot_qty = int(trade.get("lot_size") or trade.get("quantity") or 0)
        if base_lot_qty > 0 and entry_ask > 0:
            stop_hurdle = cls.calculate_rupee_capped_sl_pct(entry_ask, base_lot_qty, stop_hurdle)

        # 1. Take Profit Hurdle (Uncapped Target-Lock Runner engages when UNCAPPED_CONVEX_RUNNER or progressively trailed)
        is_uncapped_mode = (
            engine_ver == "UNCAPPED_CONVEX_RUNNER"
            or bool(trade.get("uncapped_runner"))
            or (ENABLE_4PILLAR_CANARY_ENGINE and engine_ver == "V1_CLASSIC" and prev_mfe_pct >= EARLY_BREAKEVEN_TRIGGER_PCT)
        )
        if not is_uncapped_mode and current_pnl_pct >= target_hurdle:
            return "TARGET_MET"

        # 2. Hard Stop Loss Hurdle (Dynamic, Classic, or Rupee-Capped)
        elif current_pnl_pct <= -stop_hurdle:
            return "STOP_LOSS_HIT"

        # 3. Trailing Stop / Ratchet Protection (Includes Unified House-Money Floor at +8.5% when scaled in)
        elif engine_ver == "V1_CLASSIC" and not ENABLE_4PILLAR_CANARY_ENGINE:
            if mfe_pct >= TRAILING_STOP_TRIGGER_PCT and current_bid <= (current_peak * (1.0 - TRAILING_STOP_DISTANCE_PCT / 100.0)):
                return "TRAILING_STOP_HIT"
        elif mfe_pct >= min(EARLY_BREAKEVEN_TRIGGER_PCT, ratchet_trigger) or is_scaled_in:
            chandelier_stop = cls.calculate_chandelier_stop(
                entry_ask,
                current_peak,
                mfe_pct,
                target_hurdle=target_hurdle if is_uncapped_mode else None
            )
            classic_ratchet = 0.0
            if mfe_pct >= ratchet_trigger:
                peak_trail_stop = current_peak * (1.0 - TRAILING_STOP_DISTANCE_PCT / 100.0)
                breakeven_floor = entry_ask * (1.0 + BREAKEVEN_FLOOR_PCT / 100.0)
                classic_ratchet = max(peak_trail_stop, breakeven_floor)
            elif mfe_pct >= EARLY_BREAKEVEN_TRIGGER_PCT:
                classic_ratchet = entry_ask * (1.0 + EARLY_BREAKEVEN_FLOOR_PCT / 100.0)

            blended_entry_ask = float(trade.get("entry_ask") or entry_ask)
            house_money_floor = (
                max(
                    entry_ask * (1.0 + HOUSE_MONEY_UNIFIED_FLOOR_PCT / 100.0),
                    blended_entry_ask * 1.015,
                )
                if is_scaled_in
                else 0.0
            )
            effective_stop = max(chandelier_stop or 0.0, classic_ratchet, house_money_floor)
            if effective_stop and current_bid <= effective_stop:
                return "TRAILING_STOP_HIT"

        # 4. Underlying Structure Invalidation Exit (Pillar 1: Classic VWAP Invalidation)
        # Reverted to earlier baseline behavior per quant directive:
        # Requires genuine broker VWAP distinct from spot_ltp. When broker VWAP is unavailable
        # or identical to spot (typical Upstox index behavior), structural exit is dormant.
        # This completely eliminates premature 18-point whipsaw exits on healthy pullbacks,
        # allowing winning runners to breathe while relying on the 20% hard SL, 12m stagnation stop,
        # and ₹3,500 daily portfolio ceiling.
        if not exit_reason and underlying_quotes_map and duration_sec >= 180:
            u_sym = (trade.get("underlying") or "NIFTY").upper().strip()
            u_quote = underlying_quotes_map.get(u_sym)
            if u_quote:
                curr_spot = float(u_quote.get("spot_ltp") or 0.0)
                curr_vwap = float(u_quote.get("vwap") or 0.0)
                entry_spot = float(trade.get("entry_spot_price") or 0.0)
                is_bn = "BANK" in u_sym
                min_pts = STRUCTURE_INVALIDATION_MIN_PTS_BANKNIFTY if is_bn else STRUCTURE_INVALIDATION_MIN_PTS_NIFTY

                # Earlier baseline: requires valid broker VWAP != spot_ltp
                if curr_vwap > 0 and abs(curr_vwap - curr_spot) > 0.01:
                    if trade.get("option_type") == "CE":
                        if curr_spot < (curr_vwap * (1.0 - STRUCTURE_INVALIDATION_BUFFER_PCT / 100.0)):
                            if entry_spot > 0 and curr_spot <= (entry_spot - min_pts):
                                return "STRUCTURE_INVALIDATION_BEAR"
                    elif trade.get("option_type") == "PE":
                        if curr_spot > (curr_vwap * (1.0 + STRUCTURE_INVALIDATION_BUFFER_PCT / 100.0)):
                            if entry_spot > 0 and curr_spot >= (entry_spot + min_pts):
                                return "STRUCTURE_INVALIDATION_BULL"

        # 5. Theta Stagnation Stop (Apex Quant Velocity Protection)
        if not exit_reason and ENABLE_APEX_QUANT_ENGINE:
            if (
                duration_min >= THETA_STAGNATION_TIMEOUT_MINUTES
                and mfe_pct < THETA_STAGNATION_MIN_MFE_PCT
                and current_pnl_pct < 0
            ):
                return "STAGNATION_TIME_STOP"

        # 6. Intraday Max Holding Time Exceeded (45 min)
        if not exit_reason and duration_min >= MAX_HOLDING_MINUTES:
            return "TIME_DECAY_EXPIRY"

        # 7. Session End Square-off (>= 15:15 IST or post-market)
        elif (enforce_cutoff or now_dt is not None) and not exit_reason and ((now_dt.hour > 15) or (now_dt.hour == 15 and now_dt.minute >= 15)):
            if enforce_cutoff:
                return "SESSION_END_SQUAREOFF"

        return exit_reason

    @classmethod
    def maybe_scale_in_house_money(
        cls,
        trade: Dict[str, Any],
        current_bid: float,
        quote: Dict[str, Any],
        mfe_pct: float,
        now_dt: datetime
    ) -> bool:
        """
        Step 2 Institutional Same-Contract House-Money Scale-In:
        When an open 1-lot trade entered during the Golden Sniper Impulse Window (09:43-11:30 IST)
        reaches MFE >= +15.0% and is actively holding gains (>= +12.0% above initial entry),
        scales in Lot #2 (+1 lot) on the SAME contract and locks a Unified +8.5% Breakeven Floor.
        """
        if not HOUSE_MONEY_SCALE_IN_ACTIVE:
            return False
        if int(trade.get("scale_in_count") or 0) >= 1:
            return False

        lot_size = int(trade.get("lot_size") or 65)
        current_qty = int(trade.get("quantity") or lot_size)
        if current_qty != lot_size:
            return False

        init_entry_ask = float(trade.get("initial_entry_ask") or trade["entry_ask"])
        if init_entry_ask < 35.0 or (2 * lot_size * init_entry_ask) > HOUSE_MONEY_MAX_NOTIONAL_INR:
            return False

        try:
            et_dt = datetime.fromisoformat(str(trade["entry_time"]))
            et_mins = et_dt.hour * 60 + et_dt.minute
            if not ((9 * 60 + 43) <= et_mins <= (11 * 60 + 30)):
                return False
        except Exception:
            return False

        if mfe_pct < HOUSE_MONEY_SCALE_IN_TRIGGER_MFE_PCT:
            return False

        # Ensure current price is inside the fresh breakout initiation zone (>= +12.0% and <= +19.0% above initial entry)
        # so we never dilute blended cost basis by chasing a late parabolic spike.
        min_active_bid = init_entry_ask * (1.0 + (HOUSE_MONEY_UNIFIED_FLOOR_PCT + 3.5) / 100.0)
        max_initiation_bid = init_entry_ask * 1.19
        if current_bid < min_active_bid or current_bid > max_initiation_bid:
            return False

        scale_in_price = round(float(quote.get("ask") or (current_bid * 1.0025)), 2)
        new_qty = lot_size * 2
        blended_entry_ask = round((init_entry_ask + scale_in_price) / 2.0, 2)
        scale_in_time = now_dt.isoformat()

        ok = OptionDB.scale_in_paper_trade(
            trade_id=trade["trade_id"],
            new_quantity=new_qty,
            blended_entry_ask=blended_entry_ask,
            initial_entry_ask=init_entry_ask,
            scale_in_price=scale_in_price,
            scale_in_time=scale_in_time
        )
        if ok:
            trade["initial_entry_ask"] = init_entry_ask
            trade["entry_ask"] = blended_entry_ask
            trade["entry_price_used"] = blended_entry_ask
            trade["quantity"] = new_qty
            trade["scale_in_count"] = 1
            trade["scale_in_price"] = scale_in_price
            trade["scale_in_time"] = scale_in_time
            logger.info(
                f"[OptionPaperExecution] 🚀 House-Money Scale-In (Lot #2) executed on {trade['trading_symbol']}: "
                f"Qty {lot_size} -> {new_qty} @ ₹{scale_in_price:.2f} (Initial: ₹{init_entry_ask:.2f}, Blended: ₹{blended_entry_ask:.2f}, "
                f"Unified Floor: ₹{init_entry_ask * (1.0 + HOUSE_MONEY_UNIFIED_FLOOR_PCT / 100.0):.2f})"
            )
            return True
        return False

    @classmethod
    def update_open_trades(
        cls,
        live_quotes_map: Dict[str, Dict[str, Any]],
        underlying_quotes_map: Optional[Dict[str, Dict[str, Any]]] = None,
        as_of_time: Optional[datetime] = None,
        enforce_cutoff: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Monitors open paper positions against incoming live order book quotes.
        Updates MFE, MAE, and triggers rule-based paper exits:
          1. Take Profit Target (Dynamic or Classic)
          2. Stop Loss (Dynamic or Classic)
          3. Continuous Chandelier Trailing Stop & Early Breakeven Protection
          4. Underlying Structure Invalidation Exit (Immediate Loss Truncation)
          5. Time Decay Expiry (45 min)
          6. Session End Square-off (>= 15:15 IST)
        """
        open_trades = OptionDB.get_open_paper_trades()
        closed_in_sweep: List[Dict[str, Any]] = []

        now_dt = to_ist_naive(as_of_time)
        date_str = now_dt.strftime("%Y-%m-%d")

        # Check aggregate daily circuit breaker telemetry across realized + floating unrealized P&L
        cb_telemetry = OptionDB.get_daily_circuit_breaker_telemetry(
            max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
            max_daily_loss_inr=MAX_DAILY_LOSS_INR,
            pause_minutes=INTROSPECTION_FREEZE_MINUTES,
            as_of_date=date_str,
            as_of_time=now_dt,
            live_quotes_map=live_quotes_map,
            enforce_halt_latch=True
        )
        is_aggregate_halt = bool(
            cb_telemetry.get("is_session_halted")
            and (cb_telemetry.get("is_max_loss_limit_hit") or cb_telemetry.get("is_peak_lock_tripped"))
        )

        for trade in open_trades:
            trade_id = trade["trade_id"]
            ikey = trade["instrument_key"]
            quote = live_quotes_map.get(ikey)
            if not quote:
                continue

            current_bid = float(quote.get("bid") or quote.get("ltp") or 0.0)
            if current_bid <= 0:
                continue

            anchor_entry_ask = float(trade.get("initial_entry_ask") or trade["entry_ask"])
            current_peak = max(float(trade.get("peak_price") or anchor_entry_ask), current_bid)
            current_trough = min(float(trade.get("trough_price") or anchor_entry_ask), current_bid)

            mfe_pct = round(((current_peak - anchor_entry_ask) / anchor_entry_ask * 100.0), 2)
            mae_pct = round(((current_trough - anchor_entry_ask) / anchor_entry_ask * 100.0), 2)

            try:
                entry_dt = datetime.fromisoformat(trade["entry_time"])
                duration_sec = int((now_dt - entry_dt).total_seconds())
            except Exception:
                duration_sec = 0

            exit_reason = cls.evaluate_exit_conditions(
                trade=trade,
                current_bid=current_bid,
                quote=quote,
                now_dt=now_dt,
                underlying_quotes_map=underlying_quotes_map,
                enforce_cutoff=(enforce_cutoff or as_of_time is not None),
                is_aggregate_halt=is_aggregate_halt
            )

            if exit_reason:
                close_info = cls.close_trade(
                    trade=trade,
                    exit_bid=current_bid,
                    exit_ask=float(quote.get("ask") or current_bid),
                    exit_reason=exit_reason,
                    peak_price=current_peak,
                    trough_price=current_trough,
                    mfe_pct=mfe_pct,
                    mae_pct=mae_pct,
                    duration_sec=duration_sec,
                    as_of_time=now_dt
                )
                if close_info:
                    closed_in_sweep.append(close_info)
            else:
                # Check if Lot #1 qualifies for Same-Contract House-Money Scale-In (Lot #2)
                cls.maybe_scale_in_house_money(
                    trade=trade,
                    current_bid=current_bid,
                    quote=quote,
                    mfe_pct=mfe_pct,
                    now_dt=now_dt
                )
                OptionDB.update_trade_extrema(
                    trade_id=trade_id,
                    peak_price=current_peak,
                    trough_price=current_trough,
                    mfe_pct=mfe_pct,
                    mae_pct=mae_pct
                )

        return closed_in_sweep

    @classmethod
    def close_trade(
        cls,
        trade: Dict[str, Any],
        exit_bid: float,
        exit_ask: float,
        exit_reason: str,
        peak_price: float,
        trough_price: float,
        mfe_pct: float,
        mae_pct: float,
        duration_sec: int,
        as_of_time: Optional[datetime] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Executes a paper order exit strictly at current bid price.
        Computes exact statutory charges and dispatches Telegram notification.
        """
        trade_id = trade["trade_id"]
        entry_ask = float(trade["entry_ask"])
        quantity = int(trade["quantity"])

        entry_bid = float(trade.get("entry_bid") or entry_ask)
        entry_time = trade.get("entry_time")
        is_scaled_in = int(trade.get("scale_in_count") or 0) >= 1 and float(trade.get("scale_in_price") or 0.0) > 0

        if is_scaled_in:
            init_ask = float(trade.get("initial_entry_ask") or entry_ask)
            scale_ask = float(trade.get("scale_in_price") or entry_ask)
            base_lot = int(trade.get("lot_size") or (quantity // 2))
            scale_lot = max(1, quantity - base_lot)

            cost_1 = calculate_option_trade_costs(
                buy_price=init_ask,
                sell_price=exit_bid,
                quantity=base_lot,
                apply_slippage=True,
                trade_date=entry_time,
                buy_bid=entry_bid,
                buy_ask=init_ask,
                sell_bid=exit_bid,
                sell_ask=exit_ask,
            )
            cost_2 = calculate_option_trade_costs(
                buy_price=scale_ask,
                sell_price=exit_bid,
                quantity=scale_lot,
                apply_slippage=True,
                trade_date=entry_time,
                buy_bid=scale_ask,
                buy_ask=scale_ask,
                sell_bid=exit_bid,
                sell_ask=exit_ask,
            )
            cost_breakdown = {
                "statutory_charges": round(cost_1["statutory_charges"] + cost_2["statutory_charges"], 2),
                "slippage": round(cost_1["slippage"] + cost_2["slippage"], 2),
                "total_friction": round(cost_1["total_friction"] + cost_2["total_friction"], 2),
            }
            gross_pnl = round(((exit_bid - init_ask) * base_lot) + ((exit_bid - scale_ask) * scale_lot), 2)
            total_cost_basis = (init_ask * base_lot) + (scale_ask * scale_lot)
            net_pnl = round(gross_pnl - cost_breakdown["total_friction"], 2)
            net_pnl_pct = round((net_pnl / total_cost_basis * 100.0), 2) if total_cost_basis > 0 else 0.0
        else:
            # Realistic Indian F&O Upstox Transaction Costs (Effective-Dated Tariff Schedule)
            cost_breakdown = calculate_option_trade_costs(
                buy_price=entry_ask,
                sell_price=exit_bid,
                quantity=quantity,
                apply_slippage=True,
                trade_date=entry_time,
                buy_bid=entry_bid,
                buy_ask=entry_ask,
                sell_bid=exit_bid,
                sell_ask=exit_ask,
            )
            gross_pnl = round((exit_bid - entry_ask) * quantity, 2)
            net_pnl = round(gross_pnl - cost_breakdown["total_friction"], 2)
            net_pnl_pct = round((net_pnl / (entry_ask * quantity) * 100.0), 2) if (entry_ask * quantity) > 0 else 0.0

        close_data = {
            "status": "CLOSED",
            "exit_time": to_ist_naive(as_of_time).isoformat(),
            "exit_bid": exit_bid,
            "exit_ask": exit_ask,
            "exit_price_used": exit_bid,
            "peak_price": peak_price,
            "trough_price": trough_price,
            "mfe_pct": mfe_pct,
            "mae_pct": mae_pct,
            "gross_pnl": gross_pnl,
            "charges": cost_breakdown["statutory_charges"],
            "slippage": cost_breakdown["slippage"],
            "net_pnl": net_pnl,
            "net_pnl_pct": net_pnl_pct,
            "holding_duration_seconds": duration_sec,
            "exit_reason": exit_reason,
            "telegram_exit_sent": 1,
        }

        success = OptionDB.close_paper_trade(trade_id, close_data)
        if success:
            if exit_reason in ("STOP_LOSS_HIT", "STRUCTURE_INVALIDATION_BEAR", "STRUCTURE_INVALIDATION_BULL"):
                try:
                    from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
                    OptionSignalEngine.record_direction_sl_cooldown(
                        trade.get("underlying", "NIFTY"),
                        trade.get("option_type", "CE")
                    )
                except Exception as cd_err:
                    logger.debug(f"[OptionPaperExecution] Direction SL cooldown note: {cd_err}")
            elif mfe_pct >= POST_HARVEST_MIN_MFE_PCT and net_pnl > 0:
                try:
                    from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
                    OptionSignalEngine.record_direction_harvest_cooldown(
                        trade.get("underlying", "NIFTY"),
                        trade.get("option_type", "CE"),
                        as_of_time=as_of_time
                    )
                except Exception as hc_err:
                    logger.debug(f"[OptionPaperExecution] Direction harvest cooldown note: {hc_err}")
            logger.info(
                f"[OptionPaperExecution] 🛑 Paper SELL closed: {trade['trading_symbol']} "
                f"@ BID ₹{exit_bid:.2f} | Reason: {exit_reason} | Net P&L: ₹{net_pnl:.2f} ({net_pnl_pct:+.1f}%)"
            )
            # Dispatch Telegram Closure Notification
            cls._send_telegram_closure_alert(trade, close_data, cost_breakdown)
            merged = dict(trade)
            merged.update(close_data)
            return merged
        return None

    @classmethod
    def _send_telegram_closure_alert(
        cls,
        trade: Dict[str, Any],
        close_data: Dict[str, Any],
        cost_breakdown: Dict[str, Any]
    ) -> None:
        """Sends clean HTML closure notification to configured Telegram chat."""
        outcome_emoji = "🟢 WIN" if close_data["net_pnl"] > 0 else "🔴 LOSS"
        mins = max(1, close_data["holding_duration_seconds"] // 60)

        msg = (
            f"<b>📡 OPTION SNIFFER | PAPER TRADE CLOSED</b>\n"
            f"<b>{outcome_emoji} | {trade['trading_symbol']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Contract:</b> {trade['underlying']} {int(trade['strike'])} {trade['option_type']}\n"
            f"• <b>Paper Entry:</b> ₹{trade['entry_ask']:.2f} (ASK)\n"
            f"• <b>Paper Exit:</b> ₹{close_data['exit_bid']:.2f} (BID)\n"
            f"• <b>Duration:</b> {mins} mins\n"
            f"• <b>Gross P&L:</b> ₹{close_data['gross_pnl']:+,.2f}\n"
            f"• <b>Friction/Charges:</b> ₹{cost_breakdown['total_friction']:.2f}\n"
            f"• <b>Net P&L:</b> <b>₹{close_data['net_pnl']:+,.2f} ({close_data['net_pnl_pct']:+.1f}%)</b>\n"
            f"• <b>MFE (Peak):</b> +{close_data['mfe_pct']:.1f}%\n"
            f"• <b>MAE (Trough):</b> {close_data['mae_pct']:.1f}%\n"
            f"• <b>Exit Reason:</b> {close_data['exit_reason']}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<b>⚠️ PAPER ONLY — NO LIVE ORDER PLACED</b>"
        )
        try:
            send_telegram_message(msg)
        except Exception as te:
            logger.warning(f"[OptionPaperExecution] Telegram closure dispatch note: {te}")
