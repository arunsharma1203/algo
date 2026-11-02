import os
import json
import asyncio
import logging
import pandas as pd
import numpy as np
import ta
from datetime import datetime
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.ml_lab import save_feature_importance
from app.analytics.model_manager import ModelManager
from app.data.validator import MarketDataValidator
from app.analytics.macro_engine import get_macro_regime
from app.analytics.nlp_engine import nlp_engine
from app.analytics.meta_learner import meta_learner
from app.analytics.calibration import calibrator
from app.analytics.foundation_models.manager import foundation_model_manager
from app.analytics.fno_engine import fetch_nse_option_chain
from app.analytics.telegram_notifier import send_telegram_message

logger = logging.getLogger(__name__)
router = APIRouter()

INDIAN_STOCK_UNIVERSE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", 
    "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "LT.NS", "BAJFINANCE.NS",
    "HCLTECH.NS", "MARUTI.NS", "SUNPHARMA.NS", "KOTAKBANK.NS",
    "ONGC.NS", "NTPC.NS", "AXISBANK.NS", "WIPRO.NS", "M&M.NS",
    "ULTRACEMCO.NS", "POWERGRID.NS", "TITAN.NS", "ASIANPAINT.NS", "BAJAJFINSV.NS",
    "NESTLEIND.NS", "JSWSTEEL.NS", "TATASTEEL.NS", "ADANIENT.NS", "ADANIPORTS.NS",
    "GRASIM.NS", "TECHM.NS", "HINDALCO.NS", "DIVISLAB.NS", "SBILIFE.NS",
    "BAJAJ-AUTO.NS", "EICHERMOT.NS", "INDUSINDBK.NS", "DRREDDY.NS",
    "CIPLA.NS", "APOLLOHOSP.NS", "TATACOMM.NS", "HDFCLIFE.NS", "BRITANNIA.NS",
    "COALINDIA.NS", "HEROMOTOCO.NS", "TATACONSUM.NS", "BPCL.NS", "UPL.NS",
    "JIOFIN.NS", "TRENT.NS", "HAL.NS", "BEL.NS"
]

def format_sse(data: dict) -> str:
    return json.dumps(data, default=str) + "\n"

def get_or_init_intraday_champion():
    """Loads the persisted Intraday Champion model artifact."""
    model, meta = ModelManager.load_champion("intraday")
    return model, meta

async def run_ml_scan(custom_list=None, universe_preset: str = "NIFTY_500"):
    clean_custom = [t.strip().upper() for t in (custom_list or []) if t and t.strip()]
        
    start_time = datetime.now()
    from app.analytics.universe_config import resolve_universe_tickers
    clean_preset = universe_preset.strip().upper() if universe_preset else "NIFTY_500"
    if clean_preset == "CUSTOM" and clean_custom:
        candidate_pool = [t if t.endswith(('.NS', '.BO')) else f"{t}.NS" for t in clean_custom]
        universe_label = f"CUSTOM BASKET ({len(candidate_pool)} stocks)"
    elif clean_preset in ("WATCHLIST", "USER_WATCHLIST"):
        candidate_pool = resolve_universe_tickers("WATCHLIST", custom_tickers=clean_custom if clean_custom else None)
        universe_label = f"USER WATCHLIST ({len(candidate_pool)} stocks)"
    else:
        candidate_pool = resolve_universe_tickers(clean_preset, custom_tickers=clean_custom if clean_custom else None)
        universe_label = f"{clean_preset} ({len(candidate_pool)} stocks)"

    yield format_sse({"type": "system", "message": f"[{start_time.strftime('%H:%M:%S')}] Initiating Intraday ML Sweep across {len(candidate_pool)} symbols ({universe_label})...", "progress": 1})
    
    # 1. Macro Regime Engine
    yield format_sse({"type": "info", "message": "Analyzing Macro Market Regime (NIFTY 50 & INDIA VIX)...", "progress": 2})
    macro = get_macro_regime()
    
    nifty_trend = macro['nifty_trend_short']
    vix_status = macro['vix_status']
    vix_val = macro['vix_close']
    
    if macro['nifty_close'] > 0:
        if nifty_trend == "BULLISH":
            yield format_sse({"type": "info", "message": f"📈 Intraday Bias is BULLISH (NIFTY {macro['nifty_close']:.2f} > 20 EMA {macro['ema_20']:.2f}).", "progress": 4})
        else:
            yield format_sse({"type": "info", "message": f"📉 Intraday Bias is BEARISH (NIFTY {macro['nifty_close']:.2f} < 20 EMA {macro['ema_20']:.2f}).", "progress": 4})
            
        if vix_status == "HIGH":
            yield format_sse({"type": "info", "message": f"⚠️ VIX Spike Detected! (INDIA VIX = {vix_val:.2f}). High volatility friction applied.", "progress": 5})
        else:
            yield format_sse({"type": "info", "message": f"📊 VIX is NORMAL ({vix_val:.2f}). Optimal conditions for Intraday.", "progress": 5})

    # 2. Load Persisted Intraday Champion Artifact
    yield format_sse({"type": "info", "message": "Loading verified Production Intraday Champion Model...", "progress": 6})
    try:
        champion_model, champion_meta = get_or_init_intraday_champion()
        champ_v = champion_meta.get("version", "v1.0-champion")
        f1_score_val = champion_meta.get('champion_f1', 0.685)
        yield format_sse({"type": "info", "message": f"⚡ Loaded Intraday Champion {champ_v} (Validation F1: {f1_score_val:.4f})", "progress": 8})
    except Exception as e:
        yield format_sse({"type": "error", "message": f"Failed to load Champion model: {e}", "progress": 100})
        return

    yield format_sse({"type": "info", "message": f"Screening volume and momentum activity across {len(candidate_pool)} universe symbols...", "progress": 10})
    
    # Bulk fetch 1-day volume from local SQLite to rank active stocks fast
    if len(candidate_pool) > 50:
        try:
            from app.data.historical_data_layer import get_db_path
            import sqlite3
            conn = sqlite3.connect(get_db_path(), timeout=5.0)
            rows = conn.execute("""
                SELECT ticker, volume FROM ohlcv 
                WHERE (timeframe = '1d' OR timeframe IS NULL) 
                ORDER BY date DESC, volume DESC
            """).fetchall()
            conn.close()
            vol_by_sym = {}
            for r in rows:
                sym_clean = r[0].strip().upper()
                if sym_clean not in vol_by_sym and r[1] is not None:
                    vol_by_sym[sym_clean] = float(r[1])
            sorted_candidates = sorted(candidate_pool, key=lambda s: vol_by_sym.get(s, vol_by_sym.get(f"{s}.NS", 0.0)), reverse=True)
            top_stocks = sorted_candidates[:50]
        except Exception:
            top_stocks = candidate_pool[:50]
    else:
        top_stocks = candidate_pool

    # Ensure custom list tickers are included
    for custom_ticker in clean_custom:
        clean_ticker = custom_ticker if custom_ticker.endswith(('.NS', '.BO')) else f"{custom_ticker}.NS"
        if clean_ticker not in top_stocks:
            top_stocks.append(clean_ticker)

    total = len(top_stocks)
    yield format_sse({"type": "info", "message": f"Fetching 60-day 15m candle feeds for top {total} active liquid symbols...", "progress": 15})

    from app.data.historical_data_layer import HistoricalDataLayer
    from app.data.market_provider import fetch_candles
    from app.data.data_gateway import DataGateway
    from app.analytics.decision_engine import evaluate_ticker

    results = []
    features = ['rsi', 'macd', 'macd_diff', 'adx', 'returns']

    for idx, ticker in enumerate(top_stocks):
        try:
            clean_t = ticker.replace(".NS", "").replace(".BO", "").strip().upper()
            df = HistoricalDataLayer.get_historical_ohlcv(clean_t, timeframe="15m")
            if df is None or len(df) < 50:
                df = fetch_candles(clean_t, interval="15m", period="60d")
                if df is not None and not df.empty:
                    try:
                        DataGateway.persist_intraday_candles(clean_t, df, timeframe="15m", source="upstox")
                    except Exception as persist_err:
                        logger.debug(f"[IntradayML] Could not persist 15m candles for {clean_t}: {persist_err}")

            if df is None or len(df) < 30:
                continue

            # Standardize columns
            df.columns = [str(c).lower() for c in df.columns]

            # Use SHARED DECISION ENGINE for screening pass (skip enrichment for speed)
            screen_result = evaluate_ticker(
                ticker=ticker,
                df=df,
                champion_model=champion_model,
                champion_meta=champion_meta,
                trade_type="INTRADAY",
                source="MANUAL",
                macro_state=macro,
                skip_enrichment=True,
            )

            progress_pct = int(18 + ((idx + 1) / total * 70))

            if not screen_result.qualified:
                if (idx + 1) % 5 == 0 or (idx + 1) == total:
                    yield format_sse({
                        "type": "info",
                        "message": f"🔍 Evaluated {idx+1}/{total} active symbols ({ticker} screened)...",
                        "progress": progress_pct
                    })
                continue

            # Emit live symbol-level progress event
            yield format_sse({
                "type": "info",
                "message": f"🎯 [{idx+1}/{total}] {ticker} (₹{screen_result.entry:.2f}) -> RF:{screen_result.base_probs[0]:.1f}% GB:{screen_result.base_probs[1]:.1f}% SVM:{screen_result.base_probs[2]:.1f}% | Ensemble: {screen_result.raw_confidence:.1f}%",
                "progress": progress_pct
            })

            is_bull = screen_result.is_bullish
            base_score = float(screen_result.score)
            directional_conviction = base_score if is_bull else round(100.0 - base_score, 1)

            results.append({
                "ticker": ticker,
                "score": directional_conviction,
                "directional_conviction": directional_conviction,
                "prob_up": float(screen_result.raw_confidence),
                "base_probs": screen_result.base_probs,
                "adx": screen_result.adx,
                "rsi": screen_result.rsi,
                "macd_diff": screen_result.macd_diff,
                "atr_pct": screen_result.atr_pct,
                "volume_ratio": screen_result.volume_ratio,
                "is_bullish": screen_result.is_bullish,
                "entry": screen_result.entry,
                "sl": screen_result.sl,
                "tp1": screen_result.tp1,
                "tp2": screen_result.tp2,
                "raw_df": df
            })
            
        except Exception as e:
            logger.warning(f"Error screening {ticker}: {e}")
            continue

    if not results:
        yield format_sse({"type": "error", "message": "Scan completed but no valid setups met quality gates.", "progress": 100})
        return

    results.sort(key=lambda x: x['score'], reverse=True)
    best_trade = results[0]
    best_ticker = best_trade['ticker']

    # ── Full enrichment pass on BEST TRADE using SHARED DECISION ENGINE ──
    yield format_sse({"type": "info", "message": f"Running full AI pipeline on best candidate {best_ticker}...", "progress": 90})
    
    from app.analytics.decision_engine import evaluate_ticker
    final_result = evaluate_ticker(
        ticker=best_ticker,
        df=best_trade['raw_df'],
        champion_model=champion_model,
        champion_meta=champion_meta,
        trade_type="INTRADAY",
        source="MANUAL",
        macro_state=macro,
        skip_enrichment=False,  # Full pipeline: NLP, F&O, Foundation, Meta-Learner, Calibration
    )

    # Report enrichment results
    pc = final_result.pipeline_components
    if pc.get('nlp_finbert') is True or pc.get('nlp_vader') is True:
        yield format_sse({"type": "info", "message": f"📰 FinBERT News Sentiment for {best_ticker}: {final_result.nlp_sentiment:+} ({final_result.nlp_headline[:55]}...)", "progress": 92})
    if pc.get('fno') is True and final_result.fno_info:
        yield format_sse({"type": "info", "message": f"📊 NSE Option Chain: PCR {final_result.fno_info.get('pcr')} | Max Pain ₹{final_result.fno_info.get('max_pain')}", "progress": 94})
    if pc.get('timesfm') is True or pc.get('chronos') is True:
        yield format_sse({"type": "info", "message": f"🔮 Foundation Models consulted (TimesFM: {pc.get('timesfm')}, Chronos: {pc.get('chronos')})", "progress": 96})
    if pc.get('meta_learner') is True:
        yield format_sse({"type": "info", "message": f"🤖 {final_result.meta_learner_msg}", "progress": 98})
    if pc.get('calibration') is True:
        yield format_sse({"type": "info", "message": f"📊 Final Calibrated Win Rate: {final_result.confidence:.1f}%", "progress": 99})

    # Calculate directional calibrated conviction (so Short setups display honest win-rate e.g. 100 - 15 = 85%)
    calibrated_conviction = final_result.confidence if final_result.is_bullish else round(100.0 - final_result.confidence, 1)
    raw_conviction = final_result.raw_confidence if final_result.is_bullish else round(100.0 - final_result.raw_confidence, 1)

    # Merge enrichment results into best_trade dict for frontend compatibility
    best_trade['prob_up'] = calibrated_conviction
    best_trade['score'] = calibrated_conviction
    best_trade['raw_score'] = raw_conviction
    best_trade['is_bullish'] = final_result.is_bullish
    best_trade['entry'] = final_result.entry
    best_trade['sl'] = final_result.sl
    best_trade['tp1'] = final_result.tp1
    best_trade['tp2'] = final_result.tp2
    best_trade['meta_learner_msg'] = final_result.meta_learner_msg
    best_trade['telemetry'] = final_result.telemetry
    best_trade['calibration'] = final_result.calibration
    best_trade['nlp_sentiment'] = final_result.nlp_sentiment
    best_trade['nlp_headline'] = final_result.nlp_headline
    best_trade['pipeline_components'] = final_result.pipeline_components
    best_trade['reference_price'] = final_result.reference_price
    best_trade['model_candle_close'] = final_result.model_candle_close
    best_trade['price_source'] = final_result.price_source
    best_trade['price_timestamp'] = final_result.price_timestamp
    best_trade['price_is_fresh'] = final_result.price_is_fresh
    best_trade['entry_price_source'] = getattr(final_result, 'entry_price_source', final_result.price_source)
    best_trade['entry_price_timestamp'] = getattr(final_result, 'entry_price_timestamp', final_result.price_timestamp)
    best_trade['entry_price_age_ms'] = getattr(final_result, 'entry_price_age_ms', None)
    best_trade['quote_freshness'] = getattr(final_result, 'quote_freshness', 'UNKNOWN')
    best_trade['qualified'] = final_result.qualified
    best_trade['rejection_reason'] = final_result.rejection_reason

    if final_result.timesfm_forecast:
        best_trade['timesfm_forecast'] = final_result.timesfm_forecast
    if final_result.chronos_forecast:
        best_trade['chronos_forecast'] = final_result.chronos_forecast

    # Clean raw_df before persisting or sending
    best_trade.pop('raw_df', None)

    # 9. Save as NOT_A_POSITION recommendation via shared persistence ONLY if qualified
    from app.api.ml_history import save_ml_trade, evaluate_ml_history

    was_saved = False
    if final_result.qualified:
        was_saved = save_ml_trade(
            ticker=best_trade['ticker'],
            is_bullish=best_trade['is_bullish'],
            entry=best_trade['entry'],
            sl=best_trade['sl'],
            tp1=best_trade['tp1'],
            tp2=best_trade['tp2'],
            confidence=calibrated_conviction,
            trade_type="INTRADAY",
            explanation=final_result.explanation,
            source='MANUAL',
            position_type='NOT_A_POSITION',
            engine='LEGACY',
            entry_price_source=best_trade['entry_price_source'],
            entry_price_timestamp=best_trade['entry_price_timestamp'],
            entry_price_age_ms=best_trade['entry_price_age_ms'],
            quote_freshness=best_trade['quote_freshness']
        )
    else:
        yield format_sse({
            "type": "warning",
            "message": f"⚠️ Candidate {best_trade['ticker']} disqualified: {final_result.rejection_reason}. Live recommendation suppressed."
        })

    if was_saved:
        yield format_sse({"type": "info", "message": f"💾 Saved new recommendation for {best_trade['ticker']} (LTP: ₹{best_trade['entry']:.2f}, {best_trade['entry_price_source']}) to Trade History"})
        try:
            from app.analytics.master_logger import MasterLogger
            MasterLogger.log_event("SCAN_MANUAL", "PERSISTED", f"Saved recommendation for {best_trade['ticker']}", ticker=best_trade['ticker'], details=best_trade)
        except Exception:
            pass
    elif final_result.qualified:
        yield format_sse({"type": "info", "message": f"ℹ️ Identical recommendation for {best_trade['ticker']} already logged today (deduplicated - record preserved)"})
        try:
            from app.analytics.master_logger import MasterLogger
            MasterLogger.log_event("SCAN_MANUAL", "DEDUPLICATED", f"Recommendation for {best_trade['ticker']} deduplicated (already logged today)", ticker=best_trade['ticker'])
        except Exception:
            pass

    # 10. Dispatch Telegram Notification
    if was_saved and calibrated_conviction >= 60.0:
        dir_text = "BULLISH BUY" if best_trade['is_bullish'] else "BEARISH SHORT"
        tg_msg = (
            f"🎯 <b>INTRADAY AI TRADE CALL: {best_trade['ticker']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>Action:</b> {dir_text}\n"
            f"🧠 <b>Calibrated Conviction:</b> {calibrated_conviction:.1f}%\n"
            f"💵 <b>Entry (Live LTP):</b> ₹{best_trade['entry']:.2f} ({best_trade['entry_price_source']})\n"
            f"🛑 <b>Stop Loss:</b> ₹{best_trade['sl']:.2f}\n"
            f"🎯 <b>Target 1:</b> ₹{best_trade['tp1']:.2f}\n"
            f"🎯 <b>Target 2:</b> ₹{best_trade['tp2']:.2f}\n"
            f"🌐 <b>Macro Alignment:</b> {best_trade.get('telemetry', {}).get('macro_trend', 'NORMAL')}\n"
            f"📡 <b>Source:</b> MANUAL SCAN (LEGACY ENGINE)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>{best_trade.get('meta_learner_msg', 'AI Multi-Model Consensus')}</i>"
        )
        try:
            tg_sent = send_telegram_message(tg_msg)
            if tg_sent:
                yield format_sse({"type": "info", "message": f"📱 Telegram Push Alert Dispatched for {best_trade['ticker']}"})
                try:
                    from app.analytics.master_logger import MasterLogger
                    MasterLogger.log_event("TELEGRAM", "DISPATCHED", f"Telegram alert sent for {best_trade['ticker']}", ticker=best_trade['ticker'])
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Telegram notification error: {e}")
    else:
        if not final_result.qualified:
            reason = f"Safety gate rejected: {final_result.rejection_reason}"
        elif not was_saved:
            reason = "Trade call already broadcasted today (duplicate suppressed)"
        else:
            reason = f"Calibrated confidence ({calibrated_conviction:.1f}%) below 60.0% alert gate"
        yield format_sse({"type": "info", "message": f"ℹ️ Telegram alert suppressed: {reason}"})
        try:
            from app.analytics.master_logger import MasterLogger
            MasterLogger.log_event("TELEGRAM", "SUPPRESSED", f"Telegram alert suppressed for {best_trade['ticker']}: {reason}", ticker=best_trade['ticker'])
        except Exception:
            pass

    best_trade['telegram_sent'] = bool(was_saved and best_trade['prob_up'] >= 60.0)
    best_trade['was_saved'] = was_saved
    best_trade['history'] = evaluate_ml_history(force_refresh=True, engine='LEGACY')

    elapsed = (datetime.now() - start_time).total_seconds()
    try:
        from app.analytics.master_logger import MasterLogger
        MasterLogger.log_event("SCAN_MANUAL", "COMPLETED", f"Intraday scan completed in {elapsed:.2f}s", ticker=best_trade.get('ticker'), details={"elapsed": elapsed, "top_ticker": best_trade.get('ticker')})
    except Exception:
        pass

    yield format_sse({"type": "info", "message": f"✨ Intraday Scan Complete in {elapsed:.2f}s!", "progress": 100})
    yield format_sse({"type": "result", "data": best_trade})

@router.get("/active-monitors")
async def get_active_monitors():
    import sqlite3
    from app.data.historical_data_layer import get_db_path
    conn = sqlite3.connect(get_db_path(), timeout=10.0)
    try:
        cur = conn.execute("SELECT ticker, trade_type, entry, direction FROM ml_trade_history WHERE status = 'OPEN' ORDER BY id DESC")
        columns = [column[0] for column in cur.description]
        trades = [dict(zip(columns, row)) for row in cur.fetchall()]
    except:
        trades = []
    conn.close()
    return trades

@router.get("/alerts")
async def get_ml_alerts():
    import sqlite3
    from app.data.historical_data_layer import get_db_path
    conn = sqlite3.connect(get_db_path(), timeout=10.0)
    try:
        cur = conn.execute("SELECT * FROM ml_alerts ORDER BY id DESC LIMIT 50")
        columns = [column[0] for column in cur.description]
        alerts = [dict(zip(columns, row)) for row in cur.fetchall()]
    except:
        alerts = []
    conn.close()
    return alerts

@router.get("/history")
def get_ml_history(force_refresh: bool = False):
    from app.analytics.position_monitor import PositionMonitorService
    return PositionMonitorService.evaluate_all(force_refresh=force_refresh)

@router.get("/intraday-scan")
async def intraday_scan(custom_tickers: str = None, universe: str = "NIFTY_500"):
    custom_list = custom_tickers.split(",") if custom_tickers else []
    return StreamingResponse(run_ml_scan(custom_list, universe_preset=universe), media_type="application/x-ndjson")

@router.delete("/history/{trade_id}")
async def delete_ml_history(trade_id: int):
    import sqlite3
    from app.data.historical_data_layer import get_db_path
    conn = sqlite3.connect(get_db_path(), timeout=10.0)
    conn.execute("DELETE FROM ml_trade_history WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Trade {trade_id} deleted."}

@router.post("/intraday/squareoff")
def trigger_intraday_squareoff(force: bool = False):
    """
    Manually triggers 15:15 IST automated square-off for open Legacy Intraday trades.
    If force=True, executes even on non-trading days or outside normal trading hours.
    """
    from app.analytics.intraday_squareoff import execute_intraday_squareoff
    result = execute_intraday_squareoff(force=force)
    return {"status": "success", "data": result}

@router.post("/intraday/reconcile")
def trigger_intraday_reconciliation():
    """
    Manually triggers reconciliation of overdue Legacy Intraday trades from prior dates or past 15:15 IST.
    """
    from app.analytics.intraday_squareoff import reconcile_overdue_intraday_positions
    result = reconcile_overdue_intraday_positions()
    return {"status": "success", "data": result}

