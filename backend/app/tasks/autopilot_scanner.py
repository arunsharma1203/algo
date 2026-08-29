import logging
import sqlite3
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

def is_autopilot_enabled() -> bool:
    """Checks if Autopilot Scanning is enabled in app_settings table."""
    try:
        conn = sqlite3.connect('market_data.db', timeout=5.0)
        cur = conn.execute("SELECT value FROM app_settings WHERE key = 'autopilot_enabled'")
        row = cur.fetchone()
        conn.close()
        if row and row[0] == 'false':
            return False
        return True
    except:
        return True

def run_scheduled_autopilot_sweep(session_name: str = "Morning Momentum"):
    """
    Autonomous Discovery Scanner executed at 09:30, 11:30, and 13:30 IST.
    Discovers high-probability setups and auto-broadcasts them to Telegram.
    """
    from app.analytics.autonomous_bot import is_market_open, log_alert
    
    now = datetime.now()
    logger.info(f"⏰ [Autopilot] Executing {session_name} scheduled sweep at {now.strftime('%H:%M:%S')} IST...")
    
    if not is_market_open():
        logger.info(f"⏸️ [Autopilot] Market is closed. Skipping {session_name} sweep.")
        return

    if not is_autopilot_enabled():
        logger.info(f"⏸️ [Autopilot] Autopilot mode is disabled in settings. Skipping sweep.")
        return

    # 1. Run Intraday & Swing ML Discovery
    from app.analytics.macro_engine import get_macro_regime
    macro = get_macro_regime()
    nifty_trend = macro.get('nifty_trend_short', 'BULLISH')
    
    # Priority Tickers for fast automated scan
    priority_tickers = [
        "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
        "BHARTIARTL.NS", "SBIN.NS", "ITC.NS", "LT.NS", "KOTAKBANK.NS",
        "TATAMOTORS.NS", "AXISBANK.NS", "MARUTI.NS", "SUNPHARMA.NS", "TITAN.NS",
        "BAJFINANCE.NS", "JSWSTEEL.NS", "TATASTEEL.NS", "POWERGRID.NS", "NTPC.NS"
    ]
    
    from app.api.intraday_ml import fetch_15m_data, extract_intraday_features, train_or_load_intraday_model
    from app.api.ml_history import save_ml_trade
    from app.analytics.calibration import calibrator
    from app.analytics.telegram_notifier import send_telegram_message

    model = train_or_load_intraday_model()
    discovered_count = 0

    for ticker in priority_tickers:
        try:
            df = fetch_15m_data(ticker)
            if df.empty or len(df) < 50:
                continue
                
            features_df = extract_intraday_features(df)
            if features_df.empty:
                continue
                
            latest_row = features_df.iloc[-1:]
            feature_cols = ['rsi', 'macd_diff', 'adx', 'returns', 'vol_ratio', 'atr_pct']
            X_live = latest_row[feature_cols].fillna(0)
            
            # Predict Probabilities
            raw_prob = float(model.predict_proba(X_live)[0][1]) * 100.0
            calibrated_prob, _, _ = calibrator.calibrate(raw_prob)
            
            current_price = float(df.iloc[-1]['Close'])
            atr = float(latest_row['atr'].iloc[0]) if 'atr' in latest_row else current_price * 0.015
            
            # Filter for High-Conviction Trades (Calibrated >= 70%)
            if calibrated_prob >= 70.0:
                is_bullish = True
                direction = "BULLISH"
                sl = round(current_price - (1.5 * atr), 2)
                tp1 = round(current_price + (2.5 * atr), 2)
                tp2 = round(current_price + (4.0 * atr), 2)
            elif calibrated_prob <= 30.0:
                is_bullish = False
                direction = "BEARISH"
                sl = round(current_price + (1.5 * atr), 2)
                tp1 = round(current_price - (2.5 * atr), 2)
                tp2 = round(current_price - (4.0 * atr), 2)
                calibrated_prob = round(100.0 - calibrated_prob, 1)
            else:
                continue

            # Macro Alignment Gate: Avoid counter-trend intraday trades
            if (direction == "BULLISH" and nifty_trend == "BEARISH") or (direction == "BEARISH" and nifty_trend == "BULLISH"):
                continue

            # Auto-save trade to database
            save_ml_trade(
                ticker=ticker,
                is_bullish=is_bullish,
                entry=current_price,
                sl=sl,
                tp1=tp1,
                tp2=tp2,
                confidence=calibrated_prob,
                trade_type='INTRADAY',
                explanation={
                    "source": f"Autopilot {session_name}",
                    "macro_nifty": nifty_trend,
                    "calibrated_win_rate": calibrated_prob
                }
            )
            discovered_count += 1

            # Dispatch Autonomous Telegram Alert Card
            msg = (
                f"🤖 *AUTONOMOUS AI TRADE DISCOVERY* ({session_name})\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 *Ticker:* `{ticker}` ({direction})\n"
                f"🧠 *AI Calibrated Win Rate:* `{calibrated_prob:.1f}%`\n"
                f"💵 *Entry:* `₹{current_price:.2f}`\n"
                f"🛑 *Stop Loss:* `₹{sl:.2f}`\n"
                f"🎯 *Target 1:* `₹{tp1:.2f}` (Risk:Reward ~ 1:1.7)\n"
                f"🎯 *Target 2:* `₹{tp2:.2f}`\n"
                f"📊 *NIFTY Macro:* `{nifty_trend}` Alignment Confirmed\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ _Discovered automatically by Antigravity Autopilot Engine._"
            )
            send_telegram_message(msg)
            log_alert("INFO", ticker, f"Autopilot discovered {direction} setup ({calibrated_prob:.1f}% confidence).")
            logger.info(f"✅ [Autopilot] Auto-dispatched {ticker} ({direction}) with {calibrated_prob:.1f}% conviction.")
            
        except Exception as e:
            logger.error(f"[Autopilot] Error evaluating {ticker}: {e}")

    logger.info(f"✨ [Autopilot] {session_name} sweep completed. Discovered {discovered_count} high-conviction trades.")
