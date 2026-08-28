import sqlite3
import pandas as pd
from datetime import datetime, time
import time as time_module
import logging
import yfinance as yf

logger = logging.getLogger(__name__)

# In-memory alert cache for deduplication & cooldown
# key -> {'last_alerted_at': float(epoch_seconds), 'last_level': str}
ALERT_CACHE = {}
ALERT_COOLDOWN_SECONDS = 7200  # 2-hour cooldown for identical risk state

def is_market_open(force_override: bool = False) -> bool:
    """
    Checks if Indian Equity Cash Market (NSE/BSE) is open.
    Active Hours: Monday-Friday, 09:15 to 15:30 IST.
    """
    if force_override:
        return True
        
    now = datetime.now()
    # Weekday: 0 = Monday, 4 = Friday, 5 = Saturday, 6 = Sunday
    if now.weekday() >= 5:
        return False
        
    market_open = time(9, 15)
    market_close = time(15, 30)
    current_time = now.time()
    
    return market_open <= current_time <= market_close

def calculate_tightened_stop_loss(direction: str, entry: float, sl: float, current_price: float):
    """
    Calculates the exact mathematical recommended tightened stop-loss.
    - If in profit: Trails SL to Entry Price (Breakeven) or locks partial profit.
    - If in drawdown/near entry: Moves SL up to halve the initial risk distance (50% risk reduction).
    """
    entry = float(entry)
    sl = float(sl)
    current_price = float(current_price) if current_price else entry
    
    if direction == "BULLISH":
        if current_price > entry:
            tightened_sl = max(sl, entry)
            risk_reduction_pct = 100.0
            mode = "BREAKEVEN (Risk-Free)"
        else:
            # Move SL halfway closer to entry
            tightened_sl = sl + (entry - sl) * 0.5
            risk_reduction_pct = 50.0
            mode = "50% DOWNSIDE CUT"
    else:  # BEARISH
        if current_price < entry:
            tightened_sl = min(sl, entry)
            risk_reduction_pct = 100.0
            mode = "BREAKEVEN (Risk-Free)"
        else:
            tightened_sl = sl - (sl - entry) * 0.5
            risk_reduction_pct = 50.0
            mode = "50% UPSIDE CUT"
            
    return round(tightened_sl, 2), risk_reduction_pct, mode

def evaluate_single_trade_risk(trade: dict, macro: dict = None, current_price: float = None, fetch_live_nlp: bool = False) -> dict:
    """
    Runs multi-model consensus on an individual trade and produces a granular risk audit.
    When fetch_live_nlp is False, uses pre-cached/stored NLP sentiment for sub-millisecond execution.
    """
    try:
        from app.analytics.macro_engine import get_macro_regime
        from app.analytics.nlp_engine import nlp_engine
        from app.analytics.meta_learner import meta_learner
    except Exception as e:
        logger.error(f"Error loading risk engines: {e}")
        return {}

    ticker = trade.get('ticker', '')
    direction = trade.get('direction', 'BULLISH')
    entry = float(trade.get('entry', 0.0))
    sl = float(trade.get('sl', 0.0))
    trade_type = trade.get('trade_type', 'INTRADAY')
    base_confidence = float(trade.get('confidence', 70.0))
    
    if macro is None:
        macro = get_macro_regime()
        
    nifty_trend = macro.get('nifty_trend_short', 'BULLISH')
    vix_status = macro.get('vix_status', 'NORMAL')
    vix_close = macro.get('vix_close', 15.0)
    
    # 1. NLP Sentiment (Fast in-memory extraction or live on background sweep)
    sentiment_score = 0
    headline = "Recent filings analyzed"
    
    exp = trade.get('explanation')
    if isinstance(exp, dict):
        sentiment_score = exp.get('nlp_sentiment', 0)
        headline = exp.get('nlp_headline', headline)
    elif trade.get('nlp_sentiment') is not None:
        sentiment_score = trade.get('nlp_sentiment', 0)
        headline = trade.get('nlp_headline', headline)
        
    if fetch_live_nlp:
        try:
            nlp_res = nlp_engine.analyze_ticker_news(ticker)
            sentiment_score = nlp_res.get('score', sentiment_score)
            headline = nlp_res.get('headline', headline)
        except:
            pass
        
    # 2. Layer-2 Meta-Learner Telemetry
    meta_veto = False
    meta_adjustment = 0
    meta_msg = "Model aligned"
    try:
        _, _, meta_telemetry = meta_learner.evaluate_new_trade(
            ticker=ticker,
            direction=direction,
            trade_type=trade_type,
            base_confidence=base_confidence,
            nlp_sentiment=sentiment_score,
            macro_state=macro
        )
        meta_adjustment = meta_telemetry.get('total_adjustment', 0)
        if meta_adjustment <= -12:
            meta_veto = True
            meta_msg = f"Conviction shifted by {meta_adjustment} pts due to market headwind"
        else:
            meta_msg = f"Conviction shift: {meta_adjustment:+} pts"
    except Exception as e:
        meta_msg = f"Meta-learner unavailable: {e}"

    # 3. Consensus & Attribution Breakdown Matrix
    panic_level = 0
    reasons = []
    
    model_breakdown = {
        "Macro Regime": {
            "name": "NIFTY 50 Macro Regime",
            "status": "BEARISH" if nifty_trend == "BEARISH" else "BULLISH",
            "triggered": False,
            "points": 0,
            "detail": f"NIFTY Trend is {nifty_trend} (vs 20-EMA)"
        },
        "Layer-2 Meta-Learner": {
            "name": "Layer-2 Meta-Learner",
            "status": "VETO" if meta_veto else "PASS",
            "triggered": False,
            "points": 0,
            "detail": meta_msg
        },
        "FinBERT NLP": {
            "name": "FinBERT News Sentiment",
            "status": "BEARISH" if sentiment_score < -15 else ("BULLISH" if sentiment_score > 15 else "NEUTRAL"),
            "triggered": False,
            "points": 0,
            "detail": f"Score: {sentiment_score:+} | Headline: \"{headline[:60]}...\"" if headline else f"Score: {sentiment_score:+}"
        },
        "India VIX": {
            "name": "India VIX Volatility Spike",
            "status": vix_status,
            "triggered": False,
            "points": 0,
            "detail": f"VIX @ {vix_close:.1f} ({vix_status})"
        }
    }

    if direction == "BULLISH":
        if nifty_trend == "BEARISH":
            panic_level += 30
            reasons.append("NIFTY Short-Term Trend Collapsed")
            model_breakdown["Macro Regime"]["triggered"] = True
            model_breakdown["Macro Regime"]["points"] = 30
            
        if vix_status == "HIGH":
            panic_level += 40
            reasons.append(f"India VIX Spiking Panic ({vix_close:.1f})")
            model_breakdown["India VIX"]["triggered"] = True
            model_breakdown["India VIX"]["points"] = 40
            
        if sentiment_score < -15:
            panic_level += 40
            reasons.append(f"Breaking Negative News ({sentiment_score:+} Score)")
            model_breakdown["FinBERT NLP"]["triggered"] = True
            model_breakdown["FinBERT NLP"]["points"] = 40
            
    elif direction == "BEARISH":
        if nifty_trend == "BULLISH":
            panic_level += 30
            reasons.append("NIFTY Broad Rally (Headwind)")
            model_breakdown["Macro Regime"]["triggered"] = True
            model_breakdown["Macro Regime"]["points"] = 30
            
        if sentiment_score > 15:
            panic_level += 40
            reasons.append(f"Positive News Surge ({sentiment_score:+} Score)")
            model_breakdown["FinBERT NLP"]["triggered"] = True
            model_breakdown["FinBERT NLP"]["points"] = 40

    if meta_veto:
        panic_level += 35
        reasons.append("Layer-2 Meta-Learner Veto Triggered")
        model_breakdown["Layer-2 Meta-Learner"]["triggered"] = True
        model_breakdown["Layer-2 Meta-Learner"]["points"] = 35

    # Determine Risk Level
    if panic_level >= 70:
        risk_level = "CRITICAL"
    elif panic_level >= 40:
        risk_level = "WARNING"
    else:
        risk_level = "NORMAL"

    tightened_sl, risk_reduction_pct, sl_mode = calculate_tightened_stop_loss(
        direction=direction,
        entry=entry,
        sl=sl,
        current_price=current_price or entry
    )

    return {
        "ticker": ticker,
        "direction": direction,
        "panic_level": panic_level,
        "risk_level": risk_level,
        "reasons": reasons,
        "entry": entry,
        "original_sl": sl,
        "tightened_sl": tightened_sl,
        "risk_reduction_pct": risk_reduction_pct,
        "sl_mode": sl_mode,
        "model_breakdown": model_breakdown,
        "evaluated_at": datetime.now().isoformat()
    }

def log_alert(level: str, ticker: str, message: str, audit_data: dict = None):
    conn = sqlite3.connect('market_data.db', timeout=30.0)
    conn.execute(
        "INSERT INTO ml_alerts (timestamp, level, ticker, message) VALUES (?, ?, ?, ?)",
        (datetime.now().isoformat(), level, ticker, message)
    )
    conn.commit()
    conn.close()
    logger.info(f"[{level}] {ticker}: {message}")
    
    # Push to Telegram with Actionable SL and Model Rationale
    try:
        from app.analytics.telegram_notifier import send_telegram_message
        emoji = '🚨' if level == 'CRITICAL' else '⚠️' if level == 'WARNING' else 'ℹ️'
        
        tightened_sl_text = ""
        if audit_data and audit_data.get('tightened_sl'):
            tightened_sl_text = (
                f"\n💰 <b>Entry:</b> ₹{audit_data['entry']:.2f} | <b>Original SL:</b> ₹{audit_data['original_sl']:.2f}"
                f"\n🎯 <b>Recommended Tightened SL:</b> ₹{audit_data['tightened_sl']:.2f} "
                f"<i>({audit_data.get('sl_mode', '50% Risk Cut')})</i>"
            )
            
        tg = (
            f"<b>{emoji} AUTONOMOUS AI ALERT: {ticker}</b>\n"
            f"<b>Level:</b> {level} (Threat Score: {audit_data.get('panic_level', 40)}/100)\n"
            f"<b>Verdict:</b> {message}"
            f"{tightened_sl_text}\n\n"
            f"<i>📊 Check Live AI Trade Evaluator for full Model Audit breakdown.</i>"
        )
        send_telegram_message(tg)
    except Exception as e:
        logger.error(f'Failed to trigger telegram notifier: {e}')

def active_trade_tracker(force_run: bool = False):
    """
    Runs periodically (every 5 min) to babysit OPEN trades.
    Gated by active market hours (09:15 to 15:30 IST Mon-Fri) unless forced.
    Applies deduplication & cooldown to prevent alert spam.
    """
    if not is_market_open(force_override=force_run):
        logger.info("Autonomous Bot: Market is CLOSED (Active hours: Mon-Fri 09:15 - 15:30 IST). Sleeping.")
        return

    try:
        from app.api.ml_history import evaluate_ml_history
        from app.analytics.macro_engine import get_macro_regime
    except Exception as e:
        logger.error(f"Autonomous Bot dependency load failed: {e}")
        return
        
    logger.info("Autonomous Bot: Sweeping active trades across all 4 ML models...")
    
    try:
        history = evaluate_ml_history()
    except Exception as e:
        logger.error(f"Tracker failed to evaluate history: {e}")
        return
        
    open_trades = [t for t in history if t['outcome'] == 'OPEN']
    
    if not open_trades:
        return
        
    macro = get_macro_regime()
    now_epoch = time_module.time()
    
    for trade in open_trades:
        trade_key = f"{trade.get('id', trade.get('ticker'))}_{trade.get('timestamp')}"
        audit = evaluate_single_trade_risk(trade, macro=macro)
        risk_level = audit.get('risk_level', 'NORMAL')
        ticker = trade['ticker']
        
        if risk_level in ['WARNING', 'CRITICAL']:
            # Check Alert Deduplication & Cooldown Cache
            cached_alert = ALERT_CACHE.get(trade_key)
            if cached_alert:
                time_since_last = now_epoch - cached_alert['last_alerted_at']
                # If within cooldown and risk level has not escalated (e.g. WARNING -> CRITICAL), skip
                if time_since_last < ALERT_COOLDOWN_SECONDS and cached_alert['last_level'] == risk_level:
                    logger.info(f"Skipping duplicate alert for {ticker} (cooldown active: {int(time_since_last)}s / {ALERT_COOLDOWN_SECONDS}s)")
                    continue
            
            # Record in cache
            ALERT_CACHE[trade_key] = {
                'last_alerted_at': now_epoch,
                'last_level': risk_level
            }
            
            reasons_str = ', '.join(audit.get('reasons', []))
            if risk_level == "CRITICAL":
                msg = f"EARLY EXIT TRIGGERED. Consensus: {reasons_str}. Secure capital immediately."
            else:
                msg = f"AI detected market weakness: {reasons_str}. Move SL to ₹{audit['tightened_sl']:.2f}."
                
            log_alert(risk_level, ticker, msg, audit_data=audit)

if __name__ == "__main__":
    active_trade_tracker(force_run=True)
