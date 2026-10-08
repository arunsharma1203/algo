"""
Option Sniffer: Paper Execution & Trade Lifecycle Engine.

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
from typing import Dict, Any, List, Optional

from app.analytics.option_sniffer.option_constants import (
    calculate_option_trade_costs,
    STOP_LOSS_PCT,
    TARGET_GAIN_PCT,
    TRAILING_STOP_TRIGGER_PCT,
    TRAILING_STOP_DISTANCE_PCT,
    MAX_HOLDING_MINUTES,
    OPTION_MODEL_VERSION,
    OPTION_SIGNAL_VERSION,
)
from app.analytics.option_sniffer.option_db import OptionDB
from app.analytics.telegram_notifier import send_telegram_message

logger = logging.getLogger(__name__)


class OptionPaperExecution:
    """Manages paper-trade execution lifecycle and continuous position surveillance."""

    @classmethod
    def open_trade(cls, signal: Dict[str, Any]) -> Optional[str]:
        """
        Executes a paper order entry strictly at current ask price.
        Records entry in SQLite and logs event.
        """
        trade_id = f"opt_paper_{uuid.uuid4().hex[:8]}"
        entry_ask = float(signal.get("option_ask") or 0.0)
        entry_bid = float(signal.get("option_bid") or 0.0)

        if entry_ask <= 0:
            logger.warning(f"[OptionPaperExecution] Cannot enter trade without valid ask price: {signal}")
            return None

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
            "entry_time": datetime.now().isoformat(),
            "entry_bid": entry_bid,
            "entry_ask": entry_ask,
            "model_version": signal.get("model_version", OPTION_MODEL_VERSION),
            "signal_version": signal.get("signal_version", OPTION_SIGNAL_VERSION),
            "data_freshness": signal.get("data_freshness", "REAL_TIME"),
        }

        success = OptionDB.create_paper_trade(trade_obj)
        if success:
            logger.info(
                f"[OptionPaperExecution] 📝 Paper BUY opened: {trade_obj['trading_symbol']} "
                f"Qty={lot_size} @ ASK ₹{entry_ask:.2f} (Trade ID: {trade_id})"
            )
            return trade_id
        return None

    @classmethod
    def update_open_trades(
        cls,
        live_quotes_map: Dict[str, Dict[str, Any]],
        as_of_time: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """
        Monitors open paper positions against incoming live order book quotes.
        Updates MFE, MAE, and triggers rule-based paper exits (SL, TP, Trailing Stop, Time Decay).
        """
        open_trades = OptionDB.get_open_paper_trades()
        closed_in_sweep: List[Dict[str, Any]] = []

        now_dt = as_of_time or datetime.now()

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

            # 1. Take Profit Hurdle (+35%)
            if current_pnl_pct >= TARGET_GAIN_PCT:
                exit_reason = "TARGET_MET"

            # 2. Hard Stop Loss Hurdle (-20%)
            elif current_pnl_pct <= -STOP_LOSS_PCT:
                exit_reason = "STOP_LOSS_HIT"

            # 3. Trailing Stop Ratchet: If MFE reached +15% and price dropped 10% from peak
            elif mfe_pct >= TRAILING_STOP_TRIGGER_PCT and current_bid <= (current_peak * (1.0 - TRAILING_STOP_DISTANCE_PCT / 100.0)):
                exit_reason = "TRAILING_STOP_HIT"

            # 4. Intraday Max Holding Time Exceeded (45 min)
            elif duration_min >= MAX_HOLDING_MINUTES:
                exit_reason = "TIME_DECAY_EXPIRY"

            # 5. Session End Square-off (>= 15:20 IST)
            elif now_dt.hour == 15 and now_dt.minute >= 20:
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

        # Realistic Indian F&O Upstox Transaction Costs
        cost_breakdown = calculate_option_trade_costs(
            buy_price=entry_ask,
            sell_price=exit_bid,
            quantity=quantity,
            apply_slippage=True
        )

        gross_pnl = round((exit_bid - entry_ask) * quantity, 2)
        net_pnl = round(gross_pnl - cost_breakdown["total_friction"], 2)
        net_pnl_pct = round((net_pnl / (entry_ask * quantity) * 100.0), 2) if (entry_ask * quantity) > 0 else 0.0

        close_data = {
            "status": "CLOSED",
            "exit_time": datetime.now().isoformat(),
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
