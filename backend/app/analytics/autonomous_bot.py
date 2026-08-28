import sqlite3
import pandas as pd
from datetime import datetime
import logging
import yfinance as yf

logger = logging.getLogger(__name__)

def log_alert(level, ticker, message):
    conn = sqlite3.connect('market_data.db')
    conn.execute(
        "INSERT INTO ml_alerts (timestamp, level, ticker, message) VALUES (?, ?, ?, ?)",
        (datetime.now().isoformat(), level, ticker, message)
    )
    conn.commit()
    conn.close()
    logger.info(f"[{level}] {ticker}: {message}")

def active_trade_tracker():
    """
    Runs periodically to babysit OPEN trades.
    Allows all 4 models to "talk to each other" and decide if an early exit is needed.
    """
    try:
        from app.api.ml_history import evaluate_ml_history
        from app.analytics.macro_engine import get_macro_regime
        from app.analytics.nlp_engine import analyze_sentiment
    except ImportError:
        return
        
    logger.info("Autonomous Bot: Waking up to cross-reference active trades across all ML models...")
    
    # 1. Get Open Trades
    try:
        history = evaluate_ml_history()
    except Exception as e:
        logger.error(f"Tracker failed to evaluate history: {e}")
        return
        
    open_trades = [t for t in history if t['outcome'] == 'OPEN']
    
    if not open_trades:
        return
        
    # 2. Get Global Macro State (Macro Engine)
    macro = get_macro_regime()
    nifty_trend = macro.get('nifty_trend_short', 'BULLISH')
    vix_status = macro.get('vix_status', 'NORMAL')
    
    for trade in open_trades:
        ticker = trade['ticker']
        direction = trade['direction']
        
        # 3. Get Live NLP Sentiment (NLP Engine)
        try:
            tick = yf.Ticker(ticker)
            news = tick.news
            headline = news[0]['title'] if news else ""
            sentiment_score = analyze_sentiment(headline, ticker) if headline else 0
        except:
            sentiment_score = 0
            
        # 4. Consensus Building Matrix
        panic_level = 0
        reasons = []
        
        if direction == "BULLISH":
            if nifty_trend == "BEARISH":
                panic_level += 30
                reasons.append("NIFTY Trend Collapsed")
            if vix_status == "HIGH":
                panic_level += 40
                reasons.append("VIX Spiking (Panic)")
            if sentiment_score < -15:
                panic_level += 40
                reasons.append(f"Negative News Break ({sentiment_score} score)")
                
        elif direction == "BEARISH":
            if nifty_trend == "BULLISH":
                panic_level += 30
                reasons.append("NIFTY Rallying")
            if sentiment_score > 15:
                panic_level += 40
                reasons.append(f"Positive News Break ({sentiment_score} score)")
                
        # 5. The Decision (Trigger Early Exit Alerts)
        if panic_level >= 70:
            # Models agree the trade is compromised
            msg = f"EARLY EXIT TRIGGERED. Consensus: {', '.join(reasons)}. Secure capital immediately."
            log_alert("CRITICAL", ticker, msg)
        elif panic_level >= 40:
            msg = f"WARNING. AI detected weakness: {', '.join(reasons)}. Tighten Stop Loss."
            log_alert("WARNING", ticker, msg)

if __name__ == "__main__":
    active_trade_tracker()
