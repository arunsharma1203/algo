"""
Option Sniffer Advance: Institutional Dual-Book Paper Execution Lifecycle.

Strictly Sandboxed Paper-Only (`SIMULATION_ONLY = True`):
  - Book 1 (`LONG_BOOK_BUY`):
      * Entry: BUY 1 Lot at current ASK.
      * Exit: SELL 1 Lot at current BID.
      * Risk/Reward: Capped Stop-Loss (<= ₹2,200 / -20%) + Uncapped 4-Tier Chandelier Trailing Stop.
  - Book 2 (`SHORT_BOOK_SELL`):
      * Entry: SELL 1 Lot at current BID.
      * Exit: BUY 1 Lot at current ASK.
      * Risk/Reward: Capped Stop-Loss (<= ₹1,800 / +16% premium cap) + 4-Tier Reverse-Chandelier
        Premium Decay Ratchet (locks guaranteed credit profit and trails decay down toward ₹5-₹15).
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.analytics.option_sniffer.option_constants import (
    DEFAULT_LOT_SIZES,
    calculate_option_trade_costs,
    is_option_entry_window,
    is_option_trading_window,
    to_ist_naive,
)
from app.analytics.option_sniffer_advance.advance_constants import (
    BOOK_LONG_BUY,
    BOOK_SHORT_SELL,
    LONG_CHANDELIER_TIER1_DIST_PCT,
    LONG_CHANDELIER_TIER1_TRIGGER_PCT,
    LONG_CHANDELIER_TIER2_DIST_PCT,
    LONG_CHANDELIER_TIER2_TRIGGER_PCT,
    LONG_CHANDELIER_TIER3_5_DIST_PCT,
    LONG_CHANDELIER_TIER3_DIST_PCT,
    LONG_CHANDELIER_TIER3_TRIGGER_PCT,
    LONG_CHANDELIER_TIER4_DIST_PCT,
    LONG_CHANDELIER_TIER4_TRIGGER_PCT,
    LONG_EARLY_BREAKEVEN_FLOOR_PCT,
    LONG_EARLY_BREAKEVEN_TRIGGER_PCT,
    LONG_MAX_SINGLE_TRADE_LOSS_INR,
    LONG_STOP_LOSS_PCT,
    LONG_TARGET_GAIN_PCT,
    LONG_TARGET_LOCK_FLOOR_OFFSET_PCT,
    MAX_HOLDING_MINUTES,
    SHORT_EARLY_COST_LOCK_FLOOR_PCT,
    SHORT_EARLY_COST_LOCK_TRIGGER_PCT,
    SHORT_HARD_STOP_LOSS_PCT,
    SHORT_MAX_SINGLE_TRADE_LOSS_INR,
    SHORT_REV_CHANDELIER_TIER1_DIST_PCT,
    SHORT_REV_CHANDELIER_TIER1_TRIGGER_PCT,
    SHORT_REV_CHANDELIER_TIER2_DIST_PCT,
    SHORT_REV_CHANDELIER_TIER2_TRIGGER_PCT,
    SHORT_REV_CHANDELIER_TIER3_DIST_PCT,
    SHORT_REV_CHANDELIER_TIER3_TRIGGER_PCT,
    SHORT_REV_CHANDELIER_TIER4_DIST_PCT,
    SHORT_REV_CHANDELIER_TIER4_TRIGGER_PCT,
    calculate_short_option_trade_costs,
)
from app.analytics.option_sniffer_advance.advance_db import AdvanceDB

logger = logging.getLogger(__name__)


class AdvancePaperExecution:
    """Manages Dual-Book (1-Lot BUY & 1-Lot SELL) paper trade lifecycle and continuous surveillance."""

    @staticmethod
    def calculate_long_capped_sl_pct(
        entry_ask: float,
        lot_size: int,
        base_sl_pct: float = LONG_STOP_LOSS_PCT,
        max_loss_inr: float = LONG_MAX_SINGLE_TRADE_LOSS_INR,
    ) -> float:
        notional = float(entry_ask) * float(lot_size or 65)
        if notional <= 0 or max_loss_inr <= 0:
            return float(base_sl_pct)
        rupee_cap_pct = round((max_loss_inr / notional) * 100.0, 2)
        return float(min(base_sl_pct, max(8.0, rupee_cap_pct)))

    @staticmethod
    def calculate_short_capped_sl_pct(
        entry_bid: float,
        lot_size: int,
        base_sl_pct: float = SHORT_HARD_STOP_LOSS_PCT,
        max_loss_inr: float = SHORT_MAX_SINGLE_TRADE_LOSS_INR,
    ) -> float:
        """
        Clamps short option stop-loss % so a 1-lot short position never loses more than
        SHORT_MAX_SINGLE_TRADE_LOSS_INR (₹1,800) or SHORT_HARD_STOP_LOSS_PCT (+16.0%).
        """
        notional = float(entry_bid) * float(lot_size or 65)
        if notional <= 0 or max_loss_inr <= 0:
            return float(base_sl_pct)
        rupee_cap_pct = round((max_loss_inr / notional) * 100.0, 2)
        return float(min(base_sl_pct, max(8.0, rupee_cap_pct)))

    @staticmethod
    def calculate_long_chandelier_stop(
        entry_ask: float,
        current_peak: float,
        mfe_pct: float,
        target_hurdle: float = LONG_TARGET_GAIN_PCT,
    ) -> Optional[float]:
        """4-Tier Uncapped Chandelier Trailing Stop (ratchets upward below peak_bid)."""
        if mfe_pct < LONG_EARLY_BREAKEVEN_TRIGGER_PCT:
            return None

        floor_stop = entry_ask * (1.0 + LONG_EARLY_BREAKEVEN_FLOOR_PCT / 100.0)
        if target_hurdle > 0 and mfe_pct >= target_hurdle:
            locked_pct = max(LONG_EARLY_BREAKEVEN_FLOOR_PCT, target_hurdle - LONG_TARGET_LOCK_FLOOR_OFFSET_PCT)
            floor_stop = max(floor_stop, entry_ask * (1.0 + locked_pct / 100.0))

        if mfe_pct >= LONG_CHANDELIER_TIER4_TRIGGER_PCT:
            trail_stop = current_peak * (1.0 - LONG_CHANDELIER_TIER4_DIST_PCT / 100.0)
        elif target_hurdle > 0 and mfe_pct >= target_hurdle:
            trail_stop = current_peak * (1.0 - LONG_CHANDELIER_TIER3_5_DIST_PCT / 100.0)
        elif mfe_pct >= LONG_CHANDELIER_TIER3_TRIGGER_PCT:
            trail_stop = current_peak * (1.0 - LONG_CHANDELIER_TIER3_DIST_PCT / 100.0)
        elif mfe_pct >= LONG_CHANDELIER_TIER2_TRIGGER_PCT:
            trail_stop = current_peak * (1.0 - LONG_CHANDELIER_TIER2_DIST_PCT / 100.0)
        else:
            trail_stop = current_peak * (1.0 - LONG_CHANDELIER_TIER1_DIST_PCT / 100.0)

        return round(max(floor_stop, trail_stop), 2)

    @staticmethod
    def calculate_short_reverse_chandelier_ceiling(
        entry_bid: float,
        current_trough_ask: float,
        short_mfe_pct: float,
    ) -> Optional[float]:
        """
        4-Tier Reverse-Chandelier Premium Decay Ratchet for Short Option Positions:
        As the option decays (`current_trough_ask` drops below `entry_bid`), `short_mfe_pct` rises:
            short_mfe_pct = ((entry_bid - current_trough_ask) / entry_bid) * 100
        This method computes a downward-ratcheting CEILING price (`effective_ceiling_stop`):
          - If `current_ask >= effective_ceiling_stop`, we buy back to lock in the credit gain!
          - Early Cost-Lock (>= +10% decay): Ceiling at `entry_bid * (1 - 0.04)` (+4% guaranteed gross decay).
          - Tier 1 (>= +14% decay): Trail +5.5% above `current_trough_ask` (locks >= +8.5% decay).
          - Tier 2 (>= +22% decay): Trail +4.5% above `current_trough_ask` (locks >= +17.5% decay).
          - Tier 3 (>= +35% decay): Trail +3.5% above `current_trough_ask` (locks >= +31.5% decay).
          - Tier 4 (>= +50% decay): Trail +2.5% above `current_trough_ask` (rides crush toward ₹5-₹15).
        """
        if short_mfe_pct < SHORT_EARLY_COST_LOCK_TRIGGER_PCT:
            return None

        cost_lock_ceiling = entry_bid * (1.0 - SHORT_EARLY_COST_LOCK_FLOOR_PCT / 100.0)
        if short_mfe_pct < SHORT_REV_CHANDELIER_TIER1_TRIGGER_PCT:
            return round(cost_lock_ceiling, 2)

        if short_mfe_pct >= SHORT_REV_CHANDELIER_TIER4_TRIGGER_PCT:
            trail_ceiling = current_trough_ask * (1.0 + SHORT_REV_CHANDELIER_TIER4_DIST_PCT / 100.0)
        elif short_mfe_pct >= SHORT_REV_CHANDELIER_TIER3_TRIGGER_PCT:
            trail_ceiling = current_trough_ask * (1.0 + SHORT_REV_CHANDELIER_TIER3_DIST_PCT / 100.0)
        elif short_mfe_pct >= SHORT_REV_CHANDELIER_TIER2_TRIGGER_PCT:
            trail_ceiling = current_trough_ask * (1.0 + SHORT_REV_CHANDELIER_TIER2_DIST_PCT / 100.0)
        else:
            trail_ceiling = current_trough_ask * (1.0 + SHORT_REV_CHANDELIER_TIER1_DIST_PCT / 100.0)

        # For a short position, a tighter (more protective) stop is the LOWER ceiling price
        return round(min(cost_lock_ceiling, trail_ceiling), 2)

    @classmethod
    def open_trade(
        cls,
        signal: Dict[str, Any],
        as_of_time: Optional[datetime] = None,
        enforce_cutoff: bool = False,
    ) -> Optional[str]:
        """
        Opens a 1-Lot paper position in either Book 1 (`LONG_BOOK_BUY` at ASK)
        or Book 2 (`SHORT_BOOK_SELL` at BID).
        """
        now_dt = to_ist_naive(as_of_time)
        if enforce_cutoff or as_of_time is not None:
            if not is_option_trading_window(now_dt) or not is_option_entry_window(now_dt):
                return None

        cb = AdvanceDB.get_circuit_breaker_telemetry(as_of_time=now_dt)
        if cb.get("is_session_halted") or cb.get("is_pause_active"):
            logger.info(
                f"[AdvancePaperExecution] Blocked trade open due to sandbox circuit breaker: {cb.get('halt_reason')}"
            )
            return None

        book_type = signal.get("book_type", BOOK_LONG_BUY)
        u_sym = str(signal.get("underlying") or "NIFTY").upper()
        lot_size = int(signal.get("lot_size") or DEFAULT_LOT_SIZES.get(u_sym, 65))
        entry_bid = float(signal.get("option_bid") or 0.0)
        entry_ask = float(signal.get("option_ask") or 0.0)
        if entry_bid <= 0 or entry_ask <= 0:
            return None

        trade_id = f"adv_trd_{uuid.uuid4().hex[:10]}"
        if book_type == BOOK_SHORT_SELL:
            entry_price_used = entry_bid  # SELL to open at BID
            sl_pct = cls.calculate_short_capped_sl_pct(entry_bid, lot_size)
            tgt_pct = 35.0
            init_stop = round(entry_bid * (1.0 + sl_pct / 100.0), 2)
            est_margin = round(max(entry_bid * lot_size * 2.5, 45000.0), 2)
        else:
            entry_price_used = entry_ask  # BUY to open at ASK
            sl_pct = cls.calculate_long_capped_sl_pct(entry_ask, lot_size)
            tgt_pct = LONG_TARGET_GAIN_PCT
            init_stop = round(entry_ask * (1.0 - sl_pct / 100.0), 2)
            est_margin = round(entry_ask * lot_size, 2)

        trade_obj = {
            "trade_id": trade_id,
            "signal_id": signal.get("signal_id"),
            "book_type": book_type,
            "strategy_mode": signal.get("strategy_mode", "WAVE_1_GAMMA_SNIPER"),
            "direction": signal.get("direction", "BUY_CE"),
            "instrument_key": signal["instrument_key"],
            "trading_symbol": signal["trading_symbol"],
            "underlying": u_sym,
            "option_type": signal["option_type"],
            "strike": float(signal["strike"]),
            "expiry": signal["expiry"],
            "lot_size": lot_size,
            "quantity": lot_size,
            "entry_time": now_dt.isoformat(),
            "entry_bid": entry_bid,
            "entry_ask": entry_ask,
            "entry_price_used": entry_price_used,
            "entry_spot_price": float(signal.get("spot_price") or 0.0),
            "peak_price": entry_price_used,
            "trough_price": entry_price_used,
            "trailing_stop": init_stop,
            "stop_loss_pct": sl_pct,
            "target_pct": tgt_pct,
            "estimated_margin": est_margin,
            "regime_context": signal.get("regime_telemetry") or {},
        }

        if AdvanceDB.create_trade(trade_obj):
            side_label = "SHORT SELL (1 Lot)" if book_type == BOOK_SHORT_SELL else "LONG BUY (1 Lot)"
            logger.info(
                f"[AdvancePaperExecution] 🎯 Opened {side_label} [{trade_obj['strategy_mode']}]: "
                f"{trade_obj['trading_symbol']} Qty={lot_size} @ ₹{entry_price_used:.2f} (SL: {sl_pct:.1f}%)"
            )
            return trade_id
        return None

    @classmethod
    def update_open_trades(
        cls,
        live_quotes_map: Dict[str, Dict[str, Any]],
        underlying_quotes_map: Optional[Dict[str, Dict[str, Any]]] = None,
        as_of_time: Optional[datetime] = None,
        enforce_cutoff: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Surveils all open Long Book and Short Book paper trades against live quotes.
        """
        open_trades = AdvanceDB.get_open_trades()
        closed_list: List[Dict[str, Any]] = []
        now_dt = to_ist_naive(as_of_time)

        cb = AdvanceDB.get_circuit_breaker_telemetry(as_of_time=now_dt)
        is_cb_halted = bool(cb.get("is_session_halted"))

        for trade in open_trades:
            trade_id = trade["trade_id"]
            ikey = trade["instrument_key"]
            quote = live_quotes_map.get(ikey)
            if not quote:
                continue

            curr_bid = float(quote.get("bid") or quote.get("ltp") or 0.0)
            curr_ask = float(quote.get("ask") or quote.get("ltp") or curr_bid)
            if curr_bid <= 0 or curr_ask <= 0:
                continue

            book_type = trade.get("book_type", BOOK_LONG_BUY)
            lot_size = int(trade.get("lot_size") or 65)
            qty = int(trade.get("quantity") or lot_size)

            try:
                entry_dt = datetime.fromisoformat(str(trade["entry_time"]).replace("T", " ")[:19])
                duration_sec = max(0, int((now_dt - entry_dt).total_seconds()))
            except Exception:
                duration_sec = 0
            duration_min = duration_sec / 60.0

            if book_type == BOOK_SHORT_SELL:
                # ── SHORT BOOK SURVEILLANCE (Entry: SELL at BID, Exit: BUY at ASK) ──
                entry_bid = float(trade["entry_bid"])
                prev_peak_ask = float(trade.get("peak_price") or entry_bid)
                prev_trough_ask = float(trade.get("trough_price") or entry_bid)
                curr_peak_ask = max(prev_peak_ask, curr_ask)
                curr_trough_ask = min(prev_trough_ask, curr_ask)

                # For a Short Option:
                #   MFE % = Favorable Decay % = ((entry_bid - curr_trough_ask) / entry_bid) * 100
                #   MAE % = Adverse Spike %   = ((entry_bid - curr_peak_ask) / entry_bid) * 100 (negative when ask spikes above entry_bid)
                short_mfe_pct = round(((entry_bid - curr_trough_ask) / entry_bid) * 100.0, 2)
                short_mae_pct = round(((entry_bid - curr_peak_ask) / entry_bid) * 100.0, 2)
                curr_decay_pct = ((entry_bid - curr_ask) / entry_bid) * 100.0

                sl_pct = cls.calculate_short_capped_sl_pct(
                    entry_bid, qty, float(trade.get("stop_loss_pct") or SHORT_HARD_STOP_LOSS_PCT)
                )
                hard_sl_ceiling = round(entry_bid * (1.0 + sl_pct / 100.0), 2)
                rev_chandelier_ceiling = cls.calculate_short_reverse_chandelier_ceiling(
                    entry_bid, curr_trough_ask, short_mfe_pct
                )
                effective_ceiling = (
                    min(hard_sl_ceiling, rev_chandelier_ceiling)
                    if rev_chandelier_ceiling is not None
                    else hard_sl_ceiling
                )

                exit_reason = None
                if is_cb_halted:
                    exit_reason = "CIRCUIT_BREAKER_HALT"
                elif curr_decay_pct <= -sl_pct:
                    exit_reason = "SHORT_STOP_LOSS_HIT"
                elif rev_chandelier_ceiling is not None and curr_ask >= rev_chandelier_ceiling:
                    exit_reason = "SHORT_REVERSE_CHANDELIER_HIT"
                elif duration_min >= 15.0 and short_mfe_pct < 3.5 and curr_decay_pct < -3.0:
                    exit_reason = "SHORT_STAGNATION_EXIT"
                elif duration_min >= MAX_HOLDING_MINUTES:
                    exit_reason = "TIME_DECAY_EXPIRY"
                elif (enforce_cutoff or as_of_time is not None) and (
                    now_dt.hour > 15 or (now_dt.hour == 15 and now_dt.minute >= 15)
                ):
                    exit_reason = "SESSION_END_SQUAREOFF"

                if exit_reason:
                    c_info = cls.close_trade(
                        trade=trade,
                        exit_bid=curr_bid,
                        exit_ask=curr_ask,
                        exit_reason=exit_reason,
                        peak_price=curr_peak_ask,
                        trough_price=curr_trough_ask,
                        mfe_pct=short_mfe_pct,
                        mae_pct=short_mae_pct,
                        duration_sec=duration_sec,
                        as_of_time=now_dt,
                    )
                    if c_info:
                        closed_list.append(c_info)
                else:
                    cost_est = calculate_short_option_trade_costs(
                        sell_entry_bid=entry_bid,
                        buy_exit_ask=curr_ask,
                        quantity=qty,
                        trade_date=trade.get("entry_time"),
                    )
                    AdvanceDB.update_open_trade(
                        trade_id=trade_id,
                        peak_price=curr_peak_ask,
                        trough_price=curr_trough_ask,
                        mfe_pct=short_mfe_pct,
                        mae_pct=short_mae_pct,
                        trailing_stop=effective_ceiling,
                        gross_pnl=cost_est["gross_pnl"],
                        net_pnl=cost_est["net_pnl"],
                        net_pnl_pct=cost_est["return_on_premium_pct"],
                    )
            else:
                # ── LONG BOOK SURVEILLANCE (Entry: BUY at ASK, Exit: SELL at BID) ──
                entry_ask = float(trade["entry_ask"])
                prev_peak_bid = float(trade.get("peak_price") or entry_ask)
                prev_trough_bid = float(trade.get("trough_price") or entry_ask)
                curr_peak_bid = max(prev_peak_bid, curr_bid)
                curr_trough_bid = min(prev_trough_bid, curr_bid)

                mfe_pct = round(((curr_peak_bid - entry_ask) / entry_ask) * 100.0, 2)
                mae_pct = round(((curr_trough_bid - entry_ask) / entry_ask) * 100.0, 2)
                curr_pnl_pct = ((curr_bid - entry_ask) / entry_ask) * 100.0

                sl_pct = cls.calculate_long_capped_sl_pct(
                    entry_ask, qty, float(trade.get("stop_loss_pct") or LONG_STOP_LOSS_PCT)
                )
                hard_sl_floor = round(entry_ask * (1.0 - sl_pct / 100.0), 2)
                chandelier_floor = cls.calculate_long_chandelier_stop(
                    entry_ask,
                    curr_peak_bid,
                    mfe_pct,
                    float(trade.get("target_pct") or LONG_TARGET_GAIN_PCT),
                )
                effective_floor = (
                    max(hard_sl_floor, chandelier_floor) if chandelier_floor is not None else hard_sl_floor
                )

                exit_reason = None
                if is_cb_halted:
                    exit_reason = "CIRCUIT_BREAKER_HALT"
                elif curr_pnl_pct <= -sl_pct:
                    exit_reason = "STOP_LOSS_HIT"
                elif chandelier_floor is not None and curr_bid <= chandelier_floor:
                    exit_reason = "TRAILING_STOP_HIT"
                elif duration_min >= 12.0 and mfe_pct < 5.0 and curr_pnl_pct < 0.0:
                    exit_reason = "STAGNATION_TIME_STOP"
                elif duration_min >= MAX_HOLDING_MINUTES:
                    exit_reason = "TIME_DECAY_EXPIRY"
                elif (enforce_cutoff or as_of_time is not None) and (
                    now_dt.hour > 15 or (now_dt.hour == 15 and now_dt.minute >= 15)
                ):
                    exit_reason = "SESSION_END_SQUAREOFF"

                if exit_reason:
                    c_info = cls.close_trade(
                        trade=trade,
                        exit_bid=curr_bid,
                        exit_ask=curr_ask,
                        exit_reason=exit_reason,
                        peak_price=curr_peak_bid,
                        trough_price=curr_trough_bid,
                        mfe_pct=mfe_pct,
                        mae_pct=mae_pct,
                        duration_sec=duration_sec,
                        as_of_time=now_dt,
                    )
                    if c_info:
                        closed_list.append(c_info)
                else:
                    costs = calculate_option_trade_costs(
                        buy_price=entry_ask,
                        sell_price=curr_bid,
                        quantity=qty,
                        apply_slippage=True,
                        trade_date=trade.get("entry_time"),
                    )
                    gross = round((curr_bid - entry_ask) * qty, 2)
                    net = round(gross - costs["total_friction"], 2)
                    net_pct = round((net / (entry_ask * qty)) * 100.0, 2) if (entry_ask * qty) > 0 else 0.0
                    AdvanceDB.update_open_trade(
                        trade_id=trade_id,
                        peak_price=curr_peak_bid,
                        trough_price=curr_trough_bid,
                        mfe_pct=mfe_pct,
                        mae_pct=mae_pct,
                        trailing_stop=effective_floor,
                        gross_pnl=gross,
                        net_pnl=net,
                        net_pnl_pct=net_pct,
                    )

        return closed_list

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
        as_of_time: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Closes an open Long or Short paper trade with exact Indian F&O statutory charges."""
        trade_id = trade["trade_id"]
        book_type = trade.get("book_type", BOOK_LONG_BUY)
        qty = int(trade.get("quantity") or trade.get("lot_size") or 65)
        entry_bid = float(trade["entry_bid"])
        entry_ask = float(trade["entry_ask"])
        entry_time = trade.get("entry_time")

        if book_type == BOOK_SHORT_SELL:
            # Short Exit: BUY back at exit_ask
            exit_price_used = exit_ask
            res = calculate_short_option_trade_costs(
                sell_entry_bid=entry_bid,
                buy_exit_ask=exit_ask,
                quantity=qty,
                apply_slippage=True,
                trade_date=entry_time,
                entry_bid=entry_bid,
                entry_ask=entry_ask,
                exit_bid=exit_bid,
                exit_ask=exit_ask,
            )
            gross_pnl = res["gross_pnl"]
            charges = res["statutory_charges"]
            slippage = res["slippage"]
            net_pnl = res["net_pnl"]
            net_pnl_pct = res["return_on_premium_pct"]
        else:
            # Long Exit: SELL at exit_bid
            exit_price_used = exit_bid
            costs = calculate_option_trade_costs(
                buy_price=entry_ask,
                sell_price=exit_bid,
                quantity=qty,
                apply_slippage=True,
                trade_date=entry_time,
                buy_bid=entry_bid,
                buy_ask=entry_ask,
                sell_bid=exit_bid,
                sell_ask=exit_ask,
            )
            gross_pnl = round((exit_bid - entry_ask) * qty, 2)
            charges = costs["statutory_charges"]
            slippage = costs["slippage"]
            net_pnl = round(gross_pnl - costs["total_friction"], 2)
            net_pnl_pct = round((net_pnl / (entry_ask * qty)) * 100.0, 2) if (entry_ask * qty) > 0 else 0.0

        close_data = {
            "status": "CLOSED",
            "exit_time": to_ist_naive(as_of_time).isoformat(),
            "exit_bid": exit_bid,
            "exit_ask": exit_ask,
            "exit_price_used": exit_price_used,
            "peak_price": peak_price,
            "trough_price": trough_price,
            "mfe_pct": mfe_pct,
            "mae_pct": mae_pct,
            "gross_pnl": gross_pnl,
            "charges": charges,
            "slippage": slippage,
            "net_pnl": net_pnl,
            "net_pnl_pct": net_pnl_pct,
            "holding_duration_seconds": duration_sec,
            "exit_reason": exit_reason,
        }

        if AdvanceDB.close_trade(trade_id, close_data):
            merged = dict(trade)
            merged.update(close_data)
            return merged
        return None

