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
                    f"[OptionPaperExecution] Blocked open_trade outside golden entry window (09:30-14:00 IST): {signal.get('trading_symbol')}"
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
    def calculate_chandelier_stop(cls, entry_ask: float, current_peak: float, mfe_pct: float) -> Optional[float]:
        """
        Computes continuous ATR/volatility-calibrated chandelier trailing stop.
        Dynamically tightens distance below peak as MFE expands into profit runner territory.
        Tier 3 (Runner >= +30%): Trail 4.5% below peak (guarantees >= +24.15% locked).
        Tier 2 (Expansion >= +22%): Trail 6.0% below peak (guarantees >= +14.68% locked).
        Tier 1 (Trend Initiation >= +15%): Trail 8.0% below peak (guarantees >= +5.80% locked).
        Early Breakeven (>= +18.0%): Floor at Entry + 6.0%.
        """
        min_trigger = min(EARLY_BREAKEVEN_TRIGGER_PCT, CHANDELIER_TRIGGER_TIER1_PCT)
        if mfe_pct < min_trigger:
            return None

        # Breakeven floor (+6.0%) guaranteed once EARLY_BREAKEVEN_TRIGGER_PCT (+18%) is reached
        floor_stop = entry_ask * (1.0 + EARLY_BREAKEVEN_FLOOR_PCT / 100.0) if mfe_pct >= EARLY_BREAKEVEN_TRIGGER_PCT else 0.0

        if not CHANDELIER_TRAIL_ACTIVE:
            return floor_stop if floor_stop > 0 else None

        if mfe_pct >= CHANDELIER_TRIGGER_TIER4_PCT:
            trail_stop = current_peak * (1.0 - CHANDELIER_DISTANCE_TIER4_PCT / 100.0)
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

            entry_ask = float(trade["entry_ask"])
            current_peak = max(float(trade.get("peak_price") or entry_ask), current_bid)
            current_trough = min(float(trade.get("trough_price") or entry_ask), current_bid)

            mfe_pct = round(((current_peak - entry_ask) / entry_ask * 100.0), 2)
            mae_pct = round(((current_trough - entry_ask) / entry_ask * 100.0), 2)

            # Calculate elapsed holding time
            try:
                entry_dt = datetime.fromisoformat(trade["entry_time"])
                duration_sec = int((now_dt - entry_dt).total_seconds())
                duration_min = duration_sec / 60.0
            except Exception:
                duration_sec = 0
                duration_min = 0.0

            # Current unrealized P&L if closed now at BID
            current_pnl_pct = ((current_bid - entry_ask) / entry_ask) * 100.0

            exit_reason = None

            # Portfolio Aggregate Loss Ceiling Breach -> Immediate Emergency Square-Off
            if is_aggregate_halt:
                exit_reason = "CIRCUIT_BREAKER_HALT"

            # Determine dynamic or classic exit hurdles
            engine_ver = trade.get("engine_version") or OPTION_EXECUTION_ENGINE_MODE
            if engine_ver == "DYNAMIC_V2" and trade.get("dynamic_target_pct") and trade.get("dynamic_sl_pct"):
                target_hurdle = float(trade["dynamic_target_pct"])
                stop_hurdle = float(trade["dynamic_sl_pct"])
                # Ratchet trigger: activates at +15% canonical trigger or halfway to target if tighter
                ratchet_trigger = min(TRAILING_STOP_TRIGGER_PCT, max(12.0, round(target_hurdle * 0.50, 1)))
            else:
                target_hurdle = TARGET_GAIN_PCT
                stop_hurdle = STOP_LOSS_PCT
                ratchet_trigger = TRAILING_STOP_TRIGGER_PCT

            # 1. Take Profit Hurdle (Uncapped in UNCAPPED_CONVEX_RUNNER mode, dynamic/static in others)
            is_uncapped_mode = (engine_ver == "UNCAPPED_CONVEX_RUNNER")
            if not is_uncapped_mode and current_pnl_pct >= target_hurdle:
                exit_reason = "TARGET_MET"

            # 2. Hard Stop Loss Hurdle (Dynamic or Classic)
            elif current_pnl_pct <= -stop_hurdle:
                exit_reason = "STOP_LOSS_HIT"

            # 3. Trailing Stop / Ratchet Protection
            elif engine_ver == "V1_CLASSIC" and not ENABLE_4PILLAR_CANARY_ENGINE:
                if mfe_pct >= TRAILING_STOP_TRIGGER_PCT and current_bid <= (current_peak * (1.0 - TRAILING_STOP_DISTANCE_PCT / 100.0)):
                    exit_reason = "TRAILING_STOP_HIT"
            elif mfe_pct >= min(EARLY_BREAKEVEN_TRIGGER_PCT, ratchet_trigger):
                chandelier_stop = cls.calculate_chandelier_stop(entry_ask, current_peak, mfe_pct)
                # Determine effective stop combining continuous chandelier with classic ratchet
                classic_ratchet = 0.0
                if mfe_pct >= ratchet_trigger:
                    peak_trail_stop = current_peak * (1.0 - TRAILING_STOP_DISTANCE_PCT / 100.0)
                    breakeven_floor = entry_ask * (1.0 + BREAKEVEN_FLOOR_PCT / 100.0)
                    classic_ratchet = max(peak_trail_stop, breakeven_floor)
                elif mfe_pct >= EARLY_BREAKEVEN_TRIGGER_PCT:
                    classic_ratchet = entry_ask * (1.0 + EARLY_BREAKEVEN_FLOOR_PCT / 100.0)

                effective_stop = max(chandelier_stop or 0.0, classic_ratchet)
                if effective_stop and current_bid <= effective_stop:
                    exit_reason = "TRAILING_STOP_HIT"

            # 4. Underlying Structure Invalidation Exit (Pillar 1: Immediate Loss Truncation)
            # Defeats the 35.9% Reversal Trap: If underlying structure breaks, exit at BID
            # immediately without waiting for -15% to -24% hard option stop.
            if not exit_reason and underlying_quotes_map:
                u_sym = (trade.get("underlying") or "NIFTY").upper().strip()
                u_quote = underlying_quotes_map.get(u_sym)
                if u_quote:
                    curr_spot = float(u_quote.get("spot_ltp") or 0.0)
                    curr_vwap = float(u_quote.get("vwap") or 0.0)
                    entry_spot = float(trade.get("entry_spot_price") or 0.0)
                    is_bn = "BANK" in u_sym
                    min_pts = STRUCTURE_INVALIDATION_MIN_PTS_BANKNIFTY if is_bn else STRUCTURE_INVALIDATION_MIN_PTS_NIFTY

                    if trade.get("option_type") == "CE":
                        # Bearish Invalidation: Spot drops below VWAP by > buffer AND is below entry spot by >= min_pts
                        if curr_vwap > 0 and curr_spot < (curr_vwap * (1.0 - STRUCTURE_INVALIDATION_BUFFER_PCT / 100.0)):
                            if entry_spot > 0 and curr_spot <= (entry_spot - min_pts):
                                exit_reason = "STRUCTURE_INVALIDATION_BEAR"
                    elif trade.get("option_type") == "PE":
                        # Bullish Invalidation: Spot rises above VWAP by > buffer AND is above entry spot by >= min_pts
                        if curr_vwap > 0 and curr_spot > (curr_vwap * (1.0 + STRUCTURE_INVALIDATION_BUFFER_PCT / 100.0)):
                            if entry_spot > 0 and curr_spot >= (entry_spot + min_pts):
                                exit_reason = "STRUCTURE_INVALIDATION_BULL"

            # 5. Theta Stagnation Stop (Apex Quant Velocity Protection)
            # Exit non-moving positions (MFE < +5%) after 12 minutes at current market bid
            # Scratching at small loss (-4% to -6%) instead of taking full -20% or -35% stop loss
            if not exit_reason and ENABLE_APEX_QUANT_ENGINE:
                if (
                    duration_min >= THETA_STAGNATION_TIMEOUT_MINUTES
                    and mfe_pct < THETA_STAGNATION_MIN_MFE_PCT
                    and current_pnl_pct < 0
                ):
                    exit_reason = "STAGNATION_TIME_STOP"

            # 6. Intraday Max Holding Time Exceeded (45 min)
            if not exit_reason and duration_min >= MAX_HOLDING_MINUTES:
                exit_reason = "TIME_DECAY_EXPIRY"

            # 7. Session End Square-off (>= 15:15 IST or post-market)
            elif (enforce_cutoff or as_of_time is not None) and not exit_reason and ((now_dt.hour > 15) or (now_dt.hour == 15 and now_dt.minute >= 15)):
                exit_reason = "SESSION_END_SQUAREOFF"

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
                    duration_sec=duration_sec
                )
                if close_info:
                    closed_in_sweep.append(close_info)
            else:
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
        duration_sec: int
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
            "exit_time": to_ist_naive().isoformat(),
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
