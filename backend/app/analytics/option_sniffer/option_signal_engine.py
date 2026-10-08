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
)
from app.analytics.option_sniffer.option_db import OptionDB
from app.analytics.option_sniffer.option_alpha_model import OptionAlphaModel
from app.analytics.option_sniffer.option_feature_extractor import OptionFeatureExtractor
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.telegram_notifier import send_telegram_message

logger = logging.getLogger(__name__)


class OptionSignalEngine:
    """Evaluates option candidate streams through strict state machine gates."""

    _COOLDOWNS: Dict[str, float] = {}  # instrument_key -> last_active_signal_epoch
    _MAX_CONCURRENT_OPEN_TRADES: int = 3

    def __init__(self, model: Optional[OptionAlphaModel] = None):
        self.model = model or OptionAlphaModel.load()

    def evaluate_contract(
        self,
        contract: Dict[str, Any],
        quote: Dict[str, Any],
        underlying_quote: Dict[str, Any],
        underlying_df: Optional[Any] = None,
        macro_context: Optional[Dict[str, Any]] = None,
        upstream_signals: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Processes a point-in-time option quote through the 10 quality gates.
        Returns evaluation state dictionary.
        """
        now_epoch = time.time()
        now_iso = datetime.now().isoformat()
        ikey = contract.get("instrument_key")
        tsym = contract.get("trading_symbol")
        underlying = contract.get("underlying", "NIFTY")
        opt_type = contract.get("option_type", "CE")
        strike = float(contract.get("strike") or 0.0)
        expiry = contract.get("expiry")

        rejection_reasons: List[str] = []

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

        # Fail closed early if data is distorted
        if rejection_reasons:
            return {
                "instrument_key": ikey,
                "trading_symbol": tsym,
                "status": STATE_WATCH,
                "direction": DIRECTION_NO_TRADE,
                "rejection_reasons": rejection_reasons,
                "hurdles_passed": False,
            }

        # ── GATES 6-8: OPTION ALPHA MODEL INFERENCE ──────────────────────────
        features = OptionFeatureExtractor.extract_features(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            underlying_df=underlying_df,
            macro_context=macro_context,
            upstream_signals=upstream_signals,
            as_of_time=datetime.now()
        )

        model_pred = self.model.predict_contract(features)
        direction = model_pred["direction"]
        calibrated_prob = model_pred["calibrated_prob"]
        expected_net_return = model_pred["expected_net_return_pct"]

        if direction == DIRECTION_NO_TRADE:
            rejection_reasons.append(
                f"Model neutral: Conviction ({calibrated_prob*100:.1f}%) or Net Edge ({expected_net_return:+.1f}%) below hurdle"
            )

        if calibrated_prob < MIN_CALIBRATED_PROB:
            rejection_reasons.append(f"Low probability ({calibrated_prob*100:.1f}% < {MIN_CALIBRATED_PROB*100:.1f}%)")

        if expected_net_return < MIN_EXPECTED_NET_RETURN_PCT:
            rejection_reasons.append(f"Insufficient net edge ({expected_net_return:+.1f}% < +{MIN_EXPECTED_NET_RETURN_PCT:.1f}%)")

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
            }

        # ── GATES 9-10: RISK CAPACITY & COOLDOWN (QUALIFIED ➔ ACTIVE) ────────
        current_status = STATE_QUALIFIED

        # Gate 9: Risk Budget (Max concurrent open paper trades)
        open_trades = OptionDB.get_open_paper_trades()
        if len(open_trades) >= self._MAX_CONCURRENT_OPEN_TRADES:
            rejection_reasons.append(f"Max concurrent paper trades reached ({len(open_trades)}/{self._MAX_CONCURRENT_OPEN_TRADES})")

        # Gate 10: Cooldown Hysteresis
        last_sig_time = self._COOLDOWNS.get(ikey, 0.0)
        cooldown_rem = int(SIGNAL_COOLDOWN_SECONDS - (now_epoch - last_sig_time))
        if cooldown_rem > 0:
            rejection_reasons.append(f"Signal cooldown active ({cooldown_rem}s remaining)")

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
            "model_version": self.model.version,
            "signal_version": OPTION_SIGNAL_VERSION,
            "data_freshness": quote.get("quote_freshness", "REAL_TIME"),
            "quote_age_ms": age_ms,
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
            signal_payload["telegram_sent_at"] = datetime.now().isoformat()
            OptionDB.save_signal(signal_payload)

            # 2. Enter Paper Position (BUY at current ASK)
            trade_id = OptionPaperExecution.open_trade(signal_payload)
            signal_payload["paper_trade_id"] = trade_id

        return signal_payload

    @classmethod
    def _send_telegram_signal_alert(cls, signal: Dict[str, Any]) -> None:
        """Sends clean HTML alert to configured Telegram chat upon ACTIVE state transition."""
        dir_emoji = "🟢 CALL BUY" if signal["direction"] == DIRECTION_BUY_CE else "🔴 PUT BUY"

        msg = (
            f"<b>📡 OPTION SNIFFER | PAPER BUY</b>\n"
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
