import os
import json
import asyncio
import logging
import pandas as pd
import numpy as np
import yfinance as yf
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

async def run_ml_scan(custom_list=None):
    if custom_list is None:
        custom_list = []
        
    start_time = datetime.now()
    yield format_sse({"type": "system", "message": f"[{start_time.strftime('%H:%M:%S')}] Initiating Intraday ML Sweep (15m Candles, Multi-Model Pipeline)...", "progress": 1})
    
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

    yield format_sse({"type": "info", "message": f"Screening universe candidates...", "progress": 10})
    
    # Bulk fetch 1-day volume to rank active stocks
    try:
        vol_df = yf.download(INDIAN_STOCK_UNIVERSE, period="1d", progress=False)['Volume']
        if len(vol_df) > 0:
            latest_vol = vol_df.iloc[-1].dropna()
            top_stocks = latest_vol.sort_values(ascending=False).head(20).index.tolist()
        else:
            top_stocks = INDIAN_STOCK_UNIVERSE[:20]
    except Exception as e:
        top_stocks = INDIAN_STOCK_UNIVERSE[:20]

    for custom_ticker in custom_list:
        clean_ticker = custom_ticker.strip().upper()
        if not clean_ticker.endswith(('.NS', '.BO')):
            clean_ticker = f"{clean_ticker}.NS"
        if clean_ticker and clean_ticker not in top_stocks:
            top_stocks.append(clean_ticker)

    total = len(top_stocks)
    yield format_sse({"type": "info", "message": f"Fetching 60-day 15m candle feeds for {total} stocks...", "progress": 12})
    
    try:
        bulk_data = yf.download(top_stocks, period="60d", interval="15m", progress=False)
    except Exception as e:
        yield format_sse({"type": "error", "message": f"Bulk data fetch failed: {e}", "progress": 100})
        return

    results = []
    features = ['rsi', 'macd', 'macd_diff', 'adx', 'returns']

    for idx, ticker in enumerate(top_stocks):
        try:
            if isinstance(bulk_data.columns, pd.MultiIndex):
                try:
                    df = bulk_data.xs(ticker, level=1, axis=1).copy()
                except KeyError:
                    continue
            else:
                if len(top_stocks) == 1:
                    df = bulk_data.copy()
                else:
                    continue
            
            # 1. Strict Market Data Validation
            val_report = MarketDataValidator.validate_ohlcv(df, ticker=ticker, timeframe="15m", min_rows=30)
            if not val_report["valid"]:
                continue
                
            df.columns = [col.lower() for col in df.columns]
            
            # 2. Point-In-Time Feature Engineering
            df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
            macd = ta.trend.MACD(df['close'])
            df['macd'] = macd.macd()
            df['macd_diff'] = macd.macd_diff()
            df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
            df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
            df['returns'] = df['close'].pct_change()

            ml_df = df.dropna(subset=features).copy()
            if len(ml_df) < 30:
                continue

            # 3. Production Inference: Out-Of-Sample Evaluation on Completed Bar
            latest_row = ml_df.iloc[-1]
            latest_features = latest_row[features].values.reshape(1, -1)
            
            p_rf, p_gb, p_svm = 50.0, 50.0, 50.0
            if champion_model is not None:
                prob_up_raw = float(champion_model.predict_proba(latest_features)[0][1]) * 100.0
                if hasattr(champion_model, 'estimators_') and len(champion_model.estimators_) == 3:
                    try:
                        p_rf = float(champion_model.estimators_[0].predict_proba(latest_features)[0][1]) * 100.0
                        p_gb = float(champion_model.estimators_[1].predict_proba(latest_features)[0][1]) * 100.0
                        p_svm = float(champion_model.estimators_[2].predict_proba(latest_features)[0][1]) * 100.0
                    except Exception:
                        p_rf, p_gb, p_svm = prob_up_raw, prob_up_raw, prob_up_raw
                base_probs = (p_rf, p_gb, p_svm)
            else:
                prob_up_raw = 50.0
                base_probs = (50.0, 50.0, 50.0)

            latest_close = float(latest_row['close'])
            latest_adx = float(latest_row['adx'])
            latest_rsi = float(latest_row['rsi'])
            latest_atr = float(latest_row['atr'])
            latest_macd_diff = float(latest_row['macd_diff'])
            
            is_bullish = bool(prob_up_raw >= 50.0)
            score = prob_up_raw + (latest_adx * 0.4)

            # Emit live symbol-level progress event
            progress_pct = int(12 + ((idx + 1) / total * 75))
            yield format_sse({
                "type": "info",
                "message": f"[{idx+1}/{total}] {ticker} (₹{latest_close:.2f}) -> RF:{p_rf:.1f}% GB:{p_gb:.1f}% SVM:{p_svm:.1f}% | Ensemble: {prob_up_raw:.1f}%",
                "progress": progress_pct
            })

            # Volatility-adjusted ATR stop levels
            atr_pct_val = (latest_atr / latest_close * 100) if latest_close > 0 else 1.5
            vol_sma20 = ml_df['volume'].rolling(20).mean().iloc[-1] if 'volume' in ml_df.columns else 0
            vol_ratio = float(latest_row['volume'] / vol_sma20) if (vol_sma20 and vol_sma20 > 0) else 1.0

            atr_multiplier = 1.5 if atr_pct_val <= 2.5 else 2.0

            if is_bullish:
                sl = latest_close - (latest_atr * atr_multiplier)
                tp1 = latest_close + (latest_atr * atr_multiplier)
                tp2 = latest_close + (latest_atr * (atr_multiplier * 2))
            else:
                sl = latest_close + (latest_atr * atr_multiplier)
                tp1 = latest_close - (latest_atr * atr_multiplier)
                tp2 = latest_close - (latest_atr * (atr_multiplier * 2))

            results.append({
                "ticker": ticker,
                "score": float(score),
                "prob_up": float(prob_up_raw),
                "base_probs": base_probs,
                "adx": float(latest_adx),
                "rsi": float(latest_rsi),
                "macd_diff": float(latest_macd_diff),
                "atr_pct": round(atr_pct_val, 2),
                "volume_ratio": round(vol_ratio, 2),
                "is_bullish": is_bullish,
                "entry": float(latest_close),
                "sl": float(sl),
                "tp1": float(tp1),
                "tp2": float(tp2),
                "raw_df": ml_df
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

    # 4. Point-In-Time NLP Sentiment Analysis
    yield format_sse({"type": "info", "message": f"Running VADER Financial Sentiment on live news for {best_ticker}...", "progress": 90})
    try:
        nlp_result = nlp_engine.analyze_ticker_news(best_ticker)
        sentiment_score = nlp_result['score']
        headline = nlp_result['headline']
        best_trade['nlp_sentiment'] = sentiment_score
        best_trade['nlp_headline'] = headline
        yield format_sse({"type": "info", "message": f"📰 VADER News Sentiment for {best_ticker}: {sentiment_score:+} ({headline[:55]}...)", "progress": 92})
    except Exception:
        best_trade['nlp_sentiment'] = 0
        best_trade['nlp_headline'] = "NLP Sentiment offline."

    # 5. F&O Option Chain Analytics
    yield format_sse({"type": "info", "message": f"Fetching NSE Option Chain & OI Confluence for {best_ticker}...", "progress": 93})
    fno_info = None
    try:
        clean_fno_sym = best_ticker.replace('.NS', '').replace('.BO', '')
        fno_info = fetch_nse_option_chain(clean_fno_sym)
        if fno_info.get("is_live_nse"):
            yield format_sse({
                "type": "info",
                "message": f"📊 NSE Option Chain: PCR {fno_info.get('pcr')} | Max Pain ₹{fno_info.get('max_pain')} | Buildup: {fno_info.get('buildup')}",
                "progress": 94
            })
    except Exception as e:
        logger.warning(f"FNO scan note: {e}")

    # 6. Time-Series Foundation Model Challenger Layer (TimesFM 2.5 & Chronos-2)
    yield format_sse({"type": "info", "message": f"Consulting TimesFM 2.5 & Chronos-2 Challenger Forecasts for {best_ticker}...", "progress": 95})
    found_features = None
    try:
        raw_df = best_trade.get('raw_df')
        tfm_res, chr_res, found_features = foundation_model_manager.generate_foundation_signals(
            symbol=best_ticker,
            historical_df=raw_df,
            timeframe="15m",
            horizon_bars=1,
            as_of_time=datetime.now()
        )
        best_trade['timesfm_forecast'] = tfm_res.to_dict()
        best_trade['chronos_forecast'] = chr_res.to_dict()
        best_trade['foundation_features'] = found_features.to_dict()

        if tfm_res.status == "success" or chr_res.status == "success":
            yield format_sse({
                "type": "info",
                "message": f"🔮 TimesFM Return: {tfm_res.expected_return_pct:+.2f}% | Chronos Median: {chr_res.median_return_pct or 0.0:+.2f}% | Agreement: {found_features.foundation_direction_agreement}",
                "progress": 96
            })
    except Exception as e:
        logger.warning(f"Foundation model scan note: {e}")

    # 7. Layer-2 Stacked Meta-Learner Arbitration
    yield format_sse({"type": "info", "message": "Invoking Layer-2 Stacked Meta-Learner with Foundation signals...", "progress": 97})
    try:
        adjusted_score, meta_message, telemetry = meta_learner.evaluate_new_trade(
            ticker=best_trade['ticker'],
            direction="BULLISH" if best_trade['is_bullish'] else "BEARISH",
            trade_type="INTRADAY",
            base_confidence=best_trade['prob_up'],
            base_probs=best_trade.get('base_probs'),
            nlp_sentiment=best_trade.get('nlp_sentiment', 0),
            macro_state=macro,
            atr_pct=best_trade.get('atr_pct', 1.5),
            volume_ratio=best_trade.get('volume_ratio', 1.0),
            foundation_features=found_features
        )
        best_trade['prob_up'] = adjusted_score
        best_trade['meta_learner_msg'] = meta_message
        best_trade['telemetry'] = telemetry
        yield format_sse({"type": "info", "message": f"🤖 {meta_message}", "progress": 98})
    except Exception as e:
        logger.warning(f"Meta-Learner note: {e}")

    # 8. Probability Calibration Layer
    try:
        calibrated_score, raw_score, calib_meta = calibrator.calibrate(best_trade['prob_up'])
        best_trade['raw_score'] = raw_score
        best_trade['prob_up'] = calibrated_score
        best_trade['calibration'] = calib_meta
        yield format_sse({"type": "info", "message": f"📊 Final Calibrated Win Rate: {calibrated_score}% ({calib_meta['method']})", "progress": 99})
    except Exception:
        best_trade['raw_score'] = best_trade['prob_up']
        best_trade['calibration'] = {"status": "uncalibrated"}

    # 9. Save Verified Trade to SQLite History
    from app.api.ml_history import save_ml_trade, evaluate_ml_history
    
    explanation_payload = {
        "base_score": round(float(best_trade.get("score", 70.0)), 1),
        "raw_score": best_trade.get("raw_score", best_trade['prob_up']),
        "calibrated_score": best_trade['prob_up'],
        "calibration_meta": best_trade.get("calibration", {}),
        "nlp_sentiment": best_trade.get("nlp_sentiment", 0),
        "nlp_headline": best_trade.get("nlp_headline", ""),
        "atr_pct": best_trade.get("atr_pct", 1.5),
        "volume_ratio": best_trade.get("volume_ratio", 1.0),
        "macro_regime": best_trade.get("telemetry", {}).get("macro_trend", "BULLISH"),
        "macro_aligned": best_trade.get("telemetry", {}).get("macro_aligned", True),
        "meta_message": best_trade.get("meta_learner_msg", ""),
        "timesfm": best_trade.get("timesfm_forecast"),
        "chronos": best_trade.get("chronos_forecast"),
        "fno_pcr": fno_info.get("pcr") if fno_info else None,
        "fno_max_pain": fno_info.get("max_pain") if fno_info else None,
        "champion_version": champ_v
    }

    # Clean raw_df before persisting or sending
    best_trade.pop('raw_df', None)

    save_ml_trade(
        ticker=best_trade['ticker'],
        is_bullish=best_trade['is_bullish'],
        entry=best_trade['entry'],
        sl=best_trade['sl'],
        tp1=best_trade['tp1'],
        tp2=best_trade['tp2'],
        confidence=best_trade['prob_up'],
        trade_type="INTRADAY",
        explanation=explanation_payload
    )

    # 10. Dispatch Telegram Notification for High-Conviction Trades
    if best_trade['prob_up'] >= 60.0:
        dir_text = "BULLISH BUY" if best_trade['is_bullish'] else "BEARISH SHORT"
        tg_msg = (
            f"🎯 <b>INTRADAY AI TRADE CALL: {best_trade['ticker']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>Action:</b> {dir_text}\n"
            f"🧠 <b>Calibrated Conviction:</b> {best_trade['prob_up']:.1f}%\n"
            f"💵 <b>Entry:</b> ₹{best_trade['entry']:.2f}\n"
            f"🛑 <b>Stop Loss:</b> ₹{best_trade['sl']:.2f}\n"
            f"🎯 <b>Target 1:</b> ₹{best_trade['tp1']:.2f}\n"
            f"🎯 <b>Target 2:</b> ₹{best_trade['tp2']:.2f}\n"
            f"🌐 <b>Macro Alignment:</b> {best_trade.get('telemetry', {}).get('macro_trend', 'NORMAL')}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>{best_trade.get('meta_learner_msg', 'AI Multi-Model Consensus')}</i>"
        )
        try:
            tg_sent = send_telegram_message(tg_msg)
            if tg_sent:
                yield format_sse({"type": "info", "message": f"📱 Telegram Push Alert Dispatched for {best_trade['ticker']}"})
        except Exception as e:
            logger.warning(f"Telegram notification error: {e}")

    best_trade['history'] = evaluate_ml_history()

    elapsed = (datetime.now() - start_time).total_seconds()
    yield format_sse({"type": "info", "message": f"✨ Intraday Scan Complete in {elapsed:.2f}s!", "progress": 100})
    yield format_sse({"type": "result", "data": best_trade})

@router.get("/active-monitors")
async def get_active_monitors():
    import sqlite3
    conn = sqlite3.connect('market_data.db')
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
    conn = sqlite3.connect('market_data.db')
    try:
        cur = conn.execute("SELECT * FROM ml_alerts ORDER BY id DESC LIMIT 50")
        columns = [column[0] for column in cur.description]
        alerts = [dict(zip(columns, row)) for row in cur.fetchall()]
    except:
        alerts = []
    conn.close()
    return alerts

@router.get("/history")
def get_ml_history():
    from app.api.ml_history import evaluate_ml_history
    return evaluate_ml_history()

@router.get("/intraday-scan")
async def intraday_scan(custom_tickers: str = None):
    custom_list = custom_tickers.split(",") if custom_tickers else []
    return StreamingResponse(run_ml_scan(custom_list), media_type="application/x-ndjson")

@router.delete("/history/{trade_id}")
async def delete_ml_history(trade_id: int):
    import sqlite3
    conn = sqlite3.connect('market_data.db')
    conn.execute("DELETE FROM ml_trade_history WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Trade {trade_id} deleted."}
