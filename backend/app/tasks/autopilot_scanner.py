import logging
import sqlite3
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional

from app.data.validator import MarketDataValidator
from app.analytics.model_manager import ModelManager
from app.analytics.macro_engine import get_macro_regime
from app.analytics.kelly_sizer import get_portfolio_heat_status
from app.api.ml_history import save_ml_trade
from app.analytics.decision_engine import evaluate_ticker

logger = logging.getLogger(__name__)

def is_autopilot_enabled() -> bool:
    """Checks if Autopilot Scanning is enabled in app_settings table."""
    try:
        from app.data.historical_data_layer import get_db_path
        conn = sqlite3.connect(get_db_path(), timeout=5.0)
        cur = conn.execute("SELECT value FROM app_settings WHERE key = 'autopilot_enabled'")
        row = cur.fetchone()
        conn.close()
        if row and row[0] == 'false':
            return False
        return True
    except:
        return True

_LAST_HEAT_ALERT_TIME = 0.0
HEAT_ALERT_COOLDOWN = 14400.0  # 4 hours cooldown to prevent spamming

def run_scheduled_autopilot_sweep(session_name: str = "Morning Momentum"):
    """
    Executes an autonomous market scan across priority universe using
    the AUTHORITATIVE shared decision engine (same brain as manual scans).
    """
    global _LAST_HEAT_ALERT_TIME
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
        now_epoch = time.time()
        if (now_epoch - _LAST_HEAT_ALERT_TIME) >= HEAT_ALERT_COOLDOWN:
            _LAST_HEAT_ALERT_TIME = now_epoch
            msg = (
                f"🛡️ <b>AUTOPILOT RISK MANAGER: HEAT CEILING ACTIVE</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ Current portfolio risk is <code>{heat['current_heat_pct']}%</code> (Cap: <code>{heat['max_heat_cap_pct']}%</code>).\n"
                f"📊 Genuine positions: <code>{heat.get('actual_positions', 0)}</code>\n"
                f"📋 Virtual recommendations: <code>{heat.get('virtual_recommendations', 0)}</code>\n"
                f"⏸️ Autopilot discovery has paused new order dispatches to protect capital.\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            send_telegram_message(msg)
        logger.warning(f"[Autopilot] Skipped order dispatch due to active portfolio heat ({heat['current_heat_pct']}% >= {heat['max_heat_cap_pct']}%).")
        return

    # 2. Load Persisted Champion Ensemble
    champion_model = ModelManager.load_champion('intraday')
    champion_meta = ModelManager.get_champion_metadata('intraday')
    if not champion_model:
        logger.error("[Autopilot] Aborting sweep: Champion intraday model could not be loaded.")
        return

    # 3. Pre-fetch Macro State (shared across all tickers)
    macro = get_macro_regime()
    
    priority_tickers = [
        "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
        "BHARTIARTL.NS", "SBIN.NS", "ITC.NS", "LT.NS", "KOTAKBANK.NS",
        "TATAMOTORS.NS", "AXISBANK.NS", "MARUTI.NS", "SUNPHARMA.NS", "TITAN.NS",
        "BAJFINANCE.NS", "JSWSTEEL.NS", "TATASTEEL.NS", "POWERGRID.NS", "NTPC.NS"
    ]
    
    try:
        from app.analytics.master_logger import MasterLogger
        MasterLogger.log_event(
            "SCAN_AUTONOMOUS", "SWEEP_STARTED",
            f"Autopilot starting scheduled sweep '{session_name}' ({len(priority_tickers)} priority symbols)",
            universe="AUTOPILOT_POOL"
        )
    except Exception:
        pass

    scanned_count = 0
    data_valid_count = 0
    conviction_qualified_count = 0
    macro_aligned_count = 0
    discovered_count = 0
    ticker_errors = []

    for ticker in priority_tickers:
        scanned_count += 1
        try:
            # Wrap per-ticker download so one bad ticker (e.g. TATAMOTORS.NS) doesn't kill the sweep
            try:
                df = yf.download(ticker, period="60d", interval="15m", progress=False)
            except Exception as dl_err:
                ticker_errors.append((ticker, f"Download error: {dl_err}"))
                logger.warning(f"[Autopilot] Data acquisition failed for {ticker}: {dl_err}")
                continue

            if df is None or df.empty or len(df) < 30:
                ticker_errors.append((ticker, "Empty or insufficient OHLCV candles"))
                logger.debug(f"[Autopilot] Insufficient data for {ticker}")
                continue

            data_valid_count += 1

            # ── Use SHARED DECISION ENGINE (same brain as manual) ──────────
            result = evaluate_ticker(
                ticker=ticker,
                df=df,
                champion_model=champion_model,
                champion_meta=champion_meta,
                trade_type='INTRADAY',
                source='AUTOPILOT',
                macro_state=macro,
                qualification_threshold=0.0,
                skip_enrichment=False,
            )

            if not result.qualified:
                continue

            # ── Autopilot Conviction Gate (≥70% for Long, ≤30% for Short) ──
            if result.is_bullish and result.confidence < 70.0:
                continue
            if not result.is_bullish and result.confidence > 30.0:
                continue
            
            conviction_qualified_count += 1

            # For bearish, flip the confidence display
            display_confidence = result.confidence
            if not result.is_bullish:
                display_confidence = round(100.0 - result.confidence, 1)

            # ── Macro Alignment Gate ───────────────────────────────────────
            nifty_trend = macro.get('nifty_trend_short', 'BULLISH')
            if (result.direction == "BULLISH" and nifty_trend == "BEARISH") or \
               (result.direction == "BEARISH" and nifty_trend == "BULLISH"):
                continue

            macro_aligned_count += 1

            # ── Save as NOT_A_POSITION recommendation ─────────────────────
            was_saved = save_ml_trade(
                ticker=ticker,
                is_bullish=result.is_bullish,
                entry=result.entry,
                sl=result.sl,
                tp1=result.tp1,
                tp2=result.tp2,
                confidence=display_confidence,
                trade_type='INTRADAY',
                explanation=result.explanation,
                source='AUTOPILOT',
                position_type='NOT_A_POSITION'
            )

            if not was_saved:
                logger.info(f"[Autopilot] Duplicate signal for {ticker} — skipping Telegram.")
                continue

            discovered_count += 1

            msg = (
                f"🤖 <b>AUTONOMOUS AI TRADE DISCOVERY</b> ({session_name})\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>Ticker:</b> <code>{ticker}</code> ({result.direction})\n"
                f"🧠 <b>AI Calibrated Win Rate:</b> <code>{display_confidence:.1f}%</code>\n"
                f"💵 <b>Entry (LTP):</b> <code>₹{result.entry:.2f}</code>\n"
                f"🛑 <b>Stop Loss:</b> <code>₹{result.sl:.2f}</code>\n"
                f"🎯 <b>Target 1:</b> <code>₹{result.tp1:.2f}</code>\n"
                f"🎯 <b>Target 2:</b> <code>₹{result.tp2:.2f}</code>\n"
                f"📊 <b>NIFTY Macro:</b> <code>{nifty_trend}</code> Alignment Confirmed\n"
                f"📡 <b>Pipeline:</b> RF✓ GB✓ SVC✓ Meta✓ Cal✓"
                f" NLP:{'✓' if result.pipeline_components.get('nlp_vader') is True else '✗'}"
                f" FM:{'✓' if result.pipeline_components.get('timesfm') is True else '✗'}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <i>Discovered by Autopilot — same AI brain as manual scan.</i>"
            )
            send_telegram_message(msg)
            log_alert("INFO", ticker, f"Autopilot discovered {result.direction} setup ({display_confidence:.1f}% confidence).")
            logger.info(f"✅ [Autopilot] Auto-dispatched {ticker} ({result.direction}) with {display_confidence:.1f}% conviction.")
            
        except Exception as e:
            ticker_errors.append((ticker, str(e)))
            logger.error(f"[Autopilot] Error evaluating {ticker}: {e}")

    logger.info(
        f"✨ [Autopilot] {session_name} sweep completed. "
        f"Scanned: {scanned_count} | Valid Data: {data_valid_count} | "
        f"Conviction ≥70%: {conviction_qualified_count} | Macro-Aligned: {macro_aligned_count} | "
        f"Dispatched: {discovered_count} | Errors: {len(ticker_errors)}"
    )

    try:
        from app.analytics.master_logger import MasterLogger
        MasterLogger.log_event(
            "SCAN_AUTONOMOUS", "SWEEP_COMPLETED",
            f"Autopilot {session_name} sweep completed. Scanned: {scanned_count}, Qualified: {conviction_qualified_count}, Dispatched: {discovered_count}",
            universe="AUTOPILOT_POOL",
            details={
                "session": session_name,
                "scanned": scanned_count,
                "valid_data": data_valid_count,
                "qualified": conviction_qualified_count,
                "dispatched": discovered_count,
                "errors": len(ticker_errors)
            }
        )
    except Exception:
        pass
