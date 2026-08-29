import logging
import sqlite3
import yfinance as yf
import pandas as pd
import numpy as np
import ta
from datetime import datetime
from typing import Optional

from app.data.validator import MarketDataValidator
from app.analytics.model_manager import ModelManager
from app.analytics.calibration import calibrator
from app.analytics.macro_engine import get_macro_regime
from app.analytics.kelly_sizer import get_portfolio_heat_status
from app.api.ml_history import save_ml_trade

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
    Uses verified production Intraday Champion model to scan high-volume leaders.
    Strictly fails closed without trading if data or model validation fails.
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

    # 1. Portfolio Heat Safety & Risk Ceiling Check
    try:
        from app.analytics.telegram_notifier import send_telegram_message
    except Exception:
        def send_telegram_message(msg): pass

    heat = get_portfolio_heat_status()
    if heat.get('status') == 'MAX_REACHED':
        msg = (
            f"🛡️ *AUTOPILOT RISK MANAGER: HEAT CEILING ACTIVE*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ Current portfolio risk is `{heat['current_heat_pct']}%` (Cap: `{heat['max_heat_cap_pct']}%`).\n"
            f"📊 Active open trades: `{heat['open_positions']}`\n"
            f"⏸️ Autopilot discovery has paused new order dispatches to protect capital.\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        send_telegram_message(msg)
        logger.warning(f"[Autopilot] Skipped order dispatch due to active portfolio heat ({heat['current_heat_pct']}% >= {heat['max_heat_cap_pct']}%).")
        return

    # 2. Load Persisted Intraday Champion Model Artifact
    champion_model, champion_meta = ModelManager.load_champion("intraday")
    if champion_model is None:
        logger.error("[Autopilot] No active Intraday Champion model found. Aborting sweep safely.")
        return

    macro = get_macro_regime()
    nifty_trend = macro.get('nifty_trend_short', 'BULLISH')
    
    priority_tickers = [
        "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
        "BHARTIARTL.NS", "SBIN.NS", "ITC.NS", "LT.NS", "KOTAKBANK.NS",
        "TATAMOTORS.NS", "AXISBANK.NS", "MARUTI.NS", "SUNPHARMA.NS", "TITAN.NS",
        "BAJFINANCE.NS", "JSWSTEEL.NS", "TATASTEEL.NS", "POWERGRID.NS", "NTPC.NS"
    ]
    
    features = ['rsi', 'macd', 'macd_diff', 'adx', 'returns']
    discovered_count = 0

    for ticker in priority_tickers:
        try:
            df = yf.download(ticker, period="60d", interval="15m", progress=False)
            if df.empty:
                continue

            # Validate structural data quality
            val_report = MarketDataValidator.validate_ohlcv(df, ticker=ticker, timeframe="15m", min_rows=50)
            if not val_report["valid"]:
                continue

            df.columns = [col.lower() for col in df.columns]
            
            # Technical Indicators
            df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
            macd = ta.trend.MACD(df['close'])
            df['macd'] = macd.macd()
            df['macd_diff'] = macd.macd_diff()
            df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
            df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
            df['returns'] = df['close'].pct_change()
            
            ml_df = df.dropna(subset=features).copy()
            if len(ml_df) < 50:
                continue

            latest_row = ml_df.iloc[-1]
            latest_features = latest_row[features].values.reshape(1, -1)
            
            # Predict using Persisted Champion
            raw_prob = float(champion_model.predict_proba(latest_features)[0][1]) * 100.0
            calibrated_prob, _, calib_meta = calibrator.calibrate(raw_prob)
            
            current_price = float(latest_row['close'])
            atr = float(latest_row['atr'])
            
            # Filter for High-Conviction Setups (Calibrated >= 70% for Long, <= 30% for Short)
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

            # Macro Alignment Gate
            if (direction == "BULLISH" and nifty_trend == "BEARISH") or (direction == "BEARISH" and nifty_trend == "BULLISH"):
                continue

            # Save trade to database
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
                    "calibrated_win_rate": calibrated_prob,
                    "calibration_status": calib_meta.get("calibration_status", "uncalibrated")
                }
            )
            discovered_count += 1

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
