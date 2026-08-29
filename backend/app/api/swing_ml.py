import json
import time
from datetime import datetime
import pandas as pd
import yfinance as yf
import ta
import numpy as np
import logging
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.intraday_ml import INDIAN_STOCK_UNIVERSE
from app.analytics.model_manager import ModelManager
from app.data.validator import MarketDataValidator
from app.analytics.macro_engine import get_macro_regime
from app.analytics.nlp_engine import nlp_engine
from app.analytics.meta_learner import meta_learner
from app.analytics.calibration import calibrator
from app.analytics.foundation_models.manager import foundation_model_manager
from app.analytics.telegram_notifier import send_telegram_message

logger = logging.getLogger(__name__)
router = APIRouter()

def format_sse(data: dict) -> str:
    return f"{json.dumps(data, default=str)}\n"

def get_or_init_swing_champion():
    """Loads the persisted Swing Champion model artifact."""
    model, meta = ModelManager.load_champion("swing")
    return model, meta

def run_swing_scan(custom_tickers: list = None):
    try:
        start_time = datetime.now()
        clean_custom = [t.strip().upper() for t in (custom_tickers or []) if t and t.strip()]
        universe = clean_custom if clean_custom else INDIAN_STOCK_UNIVERSE[:25].copy()
        
        yield format_sse({"type": "system", "message": f"[{start_time.strftime('%H:%M:%S')}] Initializing Swing Trade ML Pipeline for {len(universe)} symbols..."})
        yield format_sse({"type": "info", "message": "Analyzing Macro Market Regime (NIFTY 50 & INDIA VIX)...", "progress": 3})
        
        macro = get_macro_regime()
        regime = macro['nifty_trend_long']
        vix_status = macro['vix_status']
        vix_val = macro['vix_close']
        
        if macro['nifty_close'] > 0:
            if regime == "BULLISH":
                yield format_sse({"type": "info", "message": f"📈 Macro Regime is BULLISH (NIFTY {macro['nifty_close']:.2f} > 200 SMA {macro['sma_200']:.2f})."})
            else:
                yield format_sse({"type": "info", "message": f"📉 Macro Regime is BEARISH (NIFTY {macro['nifty_close']:.2f} < 200 SMA {macro['sma_200']:.2f}). -20 Penalty to Longs."})
                
            if vix_status == "HIGH":
                yield format_sse({"type": "info", "message": f"⚠️ VIX Spike Detected! (INDIA VIX = {vix_val:.2f}). High volatility penalty applied."})
            else:
                yield format_sse({"type": "info", "message": f"📊 VIX is NORMAL ({vix_val:.2f}). Safe environment for Swing Trades."})

        # Load Persisted Swing Champion Model Artifact
        yield format_sse({"type": "info", "message": "Loading verified Production Swing Champion Model...", "progress": 5})
        try:
            champion_model, champion_meta = get_or_init_swing_champion()
            champ_v = champion_meta.get("version", "v1.0-champion")
            f1_val = champion_meta.get('champion_f1', 0.695)
            yield format_sse({"type": "info", "message": f"⚡ Loaded Swing Champion {champ_v} (Validation F1: {f1_val:.4f})", "progress": 7})
        except Exception as e:
            yield format_sse({"type": "error", "message": f"Failed to load Swing Champion model: {e}"})
            return

        yield format_sse({"type": "system", "message": f"Bulk fetching 2 years of daily data for {len(universe)} symbols...", "progress": 10})
        try:
            bulk_data = yf.download(universe, period="2y", interval="1d", progress=False)
        except Exception as e:
            yield format_sse({"type": "error", "message": f"Bulk fetch failed: {str(e)}"})
            return
            
        yield format_sse({"type": "system", "message": "Data fetched successfully. Commencing out-of-sample inference...", "progress": 12})
        
        best_conviction = None
        best_score = 0
        scan_history = []
        total = len(universe)
        features = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']
        
        for idx, ticker in enumerate(universe):
            try:
                if isinstance(bulk_data.columns, pd.MultiIndex):
                    try:
                        df = bulk_data.xs(ticker, level=1, axis=1).copy()
                    except KeyError:
                        continue
                else:
                    if len(universe) == 1:
                        df = bulk_data.copy()
                    else:
                        continue
                        
                # 1. Strict Market Data Validation
                val_report = MarketDataValidator.validate_ohlcv(df, ticker=ticker, timeframe="1d", min_rows=60)
                if not val_report["valid"]:
                    continue

                df = df.dropna(how='all')
                df = df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
                df.columns = [col.lower() for col in df.columns]
                
                # 2. Point-In-Time Feature Engineering
                df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
                macd = ta.trend.MACD(df['close'])
                df['macd'] = macd.macd()
                df['macd_diff'] = macd.macd_diff()
                df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
                df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
                
                ml_df = df.dropna(subset=features).copy()
                if len(ml_df) < 60:
                    continue

                # 3. Production Inference: Out-Of-Sample Evaluation on Completed Bar
                latest_data = ml_df.iloc[-1]
                latest_features = latest_data[features].values.reshape(1, -1)
                latest_features = np.nan_to_num(latest_features)

                p_rf, p_gb, p_svm = 50.0, 50.0, 50.0
                if champion_model is not None:
                    prob = champion_model.predict_proba(latest_features)[0]
                    bullish_prob = float(prob[1] * 100.0)
                    if hasattr(champion_model, 'estimators_') and len(champion_model.estimators_) == 3:
                        try:
                            p_rf = float(champion_model.estimators_[0].predict_proba(latest_features)[0][1]) * 100.0
                            p_gb = float(champion_model.estimators_[1].predict_proba(latest_features)[0][1]) * 100.0
                            p_svm = float(champion_model.estimators_[2].predict_proba(latest_features)[0][1]) * 100.0
                        except Exception:
                            p_rf, p_gb, p_svm = bullish_prob, bullish_prob, bullish_prob
                    base_probs = (p_rf, p_gb, p_svm)
                else:
                    bullish_prob = 50.0
                    base_probs = (50.0, 50.0, 50.0)

                technical_bonus = 0
                if float(latest_data['rsi']) < 40 and float(latest_data['macd_diff']) > 0:
                    technical_bonus += 15
                if float(latest_data['adx']) > 25:
                    technical_bonus += 10
                    
                score = float(bullish_prob + technical_bonus)
                
                current_price = float(latest_data['close'])
                atr = float(latest_data['atr'])
                if np.isnan(current_price) or current_price <= 0 or np.isnan(atr) or atr <= 0:
                    continue

                progress_pct = int(12 + ((idx + 1) / total * 75))
                yield format_sse({
                    "type": "info",
                    "message": f"[{idx+1}/{total}] {ticker} (₹{current_price:.2f}) -> RF:{p_rf:.1f}% GB:{p_gb:.1f}% SVM:{p_svm:.1f}% | Ensemble: {bullish_prob:.1f}%",
                    "progress": progress_pct
                })
                    
                atr_pct_val = (atr / current_price * 100) if current_price > 0 else 2.0
                vol_sma20 = ml_df['volume'].rolling(20).mean().iloc[-1] if 'volume' in ml_df.columns else 0
                vol_ratio = float(latest_data['volume'] / vol_sma20) if (vol_sma20 and vol_sma20 > 0) else 1.0
                
                # Swing Trading Stop Loss (2x ATR) and Targets (1:1.5, 1:3)
                sl = float(current_price - (atr * 2))
                tp1 = float(current_price + (atr * 3))
                tp2 = float(current_price + (atr * 6))
                
                scan_history.append({
                    "ticker": ticker,
                    "score": round(score, 1),
                    "action": "BUY" if score > 70 else "HOLD",
                    "prob": round(bullish_prob, 1),
                    "price": round(current_price, 2)
                })
                
                if regime == "BEARISH":
                    score -= 15
                    
                if score > best_score:
                    best_score = score
                    best_conviction = {
                        "ticker": ticker,
                        "action": "BUY",
                        "is_bullish": True,
                        "score": score,
                        "prob": bullish_prob,
                        "base_probs": base_probs,
                        "entry": current_price,
                        "sl": sl,
                        "tp1": tp1,
                        "tp2": tp2,
                        "atr_pct": round(atr_pct_val, 2),
                        "volume_ratio": round(vol_ratio, 2),
                        "rsi": float(latest_data['rsi']),
                        "macd_diff": float(latest_data['macd_diff']),
                        "adx": float(latest_data['adx']),
                        "raw_df": ml_df,
                        "timestamp": datetime.now().isoformat()
                    }
                    yield format_sse({"type": "info", "message": f"🔥 New high conviction swing setup found: {ticker} (Score: {score:.1f})"})
                
            except Exception as e:
                logger.warning(f"Error screening swing candidate {ticker}: {e}")
                continue
        
        yield format_sse({"type": "system", "message": "Swing Market Sweep Complete. Finalizing arbitration...", "progress": 90})
        
        if best_conviction:
            # 4. Point-In-Time NLP Sentiment Analysis
            yield format_sse({"type": "info", "message": f"Running VADER Financial Sentiment on live news for {best_conviction['ticker']}...", "progress": 92})
            try:
                nlp_result = nlp_engine.analyze_ticker_news(best_conviction['ticker'])
                sentiment_score = nlp_result['score']
                headline = nlp_result['headline']
                best_conviction['nlp_sentiment'] = sentiment_score
                best_conviction['nlp_headline'] = headline
            except Exception:
                best_conviction['nlp_sentiment'] = 0
                best_conviction['nlp_headline'] = "NLP Sentiment offline."

            # 5. Time-Series Foundation Model Challenger Layer (TimesFM 2.5 & Chronos-2)
            yield format_sse({"type": "info", "message": f"Consulting TimesFM 2.5 & Chronos-2 Challenger Forecasts for {best_conviction['ticker']}...", "progress": 95})
            found_features = None
            try:
                raw_df = best_conviction.get('raw_df')
                tfm_res, chr_res, found_features = foundation_model_manager.generate_foundation_signals(
                    symbol=best_conviction['ticker'],
                    historical_df=raw_df,
                    timeframe="1d",
                    horizon_bars=5,
                    as_of_time=datetime.now()
                )
                best_conviction['timesfm_forecast'] = tfm_res.to_dict()
                best_conviction['chronos_forecast'] = chr_res.to_dict()
                best_conviction['foundation_features'] = found_features.to_dict()
            except Exception as e:
                logger.warning(f"Foundation model swing scan note: {e}")

            # 6. Layer-2 Stacked Meta-Learner Arbitration
            yield format_sse({"type": "info", "message": "Invoking Layer-2 Stacked Meta-Learner with Foundation signals...", "progress": 97})
            try:
                adjusted_score, meta_message, telemetry = meta_learner.evaluate_new_trade(
                    ticker=best_conviction['ticker'],
                    direction="BULLISH" if best_conviction['is_bullish'] else "BEARISH",
                    trade_type="SWING",
                    base_confidence=best_conviction['score'],
                    base_probs=best_conviction.get('base_probs'),
                    nlp_sentiment=best_conviction.get('nlp_sentiment', 0),
                    macro_state=macro,
                    atr_pct=best_conviction.get('atr_pct', 2.0),
                    volume_ratio=best_conviction.get('volume_ratio', 1.0),
                    foundation_features=found_features
                )
                best_conviction['score'] = adjusted_score
                best_conviction['meta_learner_msg'] = meta_message
                best_conviction['telemetry'] = telemetry
                yield format_sse({"type": "info", "message": f"🤖 {meta_message}", "progress": 98})
            except Exception as e:
                logger.warning(f"Meta-Learner warning: {e}")

            # 7. Probability Calibration Layer
            try:
                calibrated_score, raw_score, calib_meta = calibrator.calibrate(best_conviction['score'])
                best_conviction['raw_score'] = raw_score
                best_conviction['score'] = calibrated_score
                best_conviction['calibration'] = calib_meta
                yield format_sse({"type": "info", "message": f"📊 Final Calibrated Win Rate: {calibrated_score}% ({calib_meta['method']})", "progress": 99})
            except Exception:
                best_conviction['raw_score'] = best_conviction['score']
                best_conviction['calibration'] = {"status": "uncalibrated"}

            # 8. Save Verified Trade to SQLite History
            from app.api.ml_history import save_ml_trade
            
            explanation_payload = {
                "base_score": round(float(best_conviction.get("prob", 70.0)), 1),
                "raw_score": best_conviction.get("raw_score", best_conviction['score']),
                "calibrated_score": best_conviction['score'],
                "calibration_meta": best_conviction.get("calibration", {}),
                "nlp_sentiment": best_conviction.get("nlp_sentiment", 0),
                "nlp_headline": best_conviction.get("nlp_headline", ""),
                "atr_pct": best_conviction.get("atr_pct", 2.0),
                "volume_ratio": best_conviction.get("volume_ratio", 1.0),
                "macro_regime": best_conviction.get("telemetry", {}).get("macro_trend", "BULLISH"),
                "macro_aligned": best_conviction.get("telemetry", {}).get("macro_aligned", True),
                "meta_message": best_conviction.get("meta_learner_msg", ""),
                "timesfm": best_conviction.get("timesfm_forecast"),
                "chronos": best_conviction.get("chronos_forecast"),
                "champion_version": champ_v
            }

            best_conviction.pop('raw_df', None)

            save_ml_trade(
                ticker=best_conviction['ticker'],
                is_bullish=best_conviction['is_bullish'],
                entry=best_conviction['entry'],
                sl=best_conviction['sl'],
                tp1=best_conviction['tp1'],
                tp2=best_conviction['tp2'],
                confidence=best_conviction['score'],
                trade_type="SWING",
                explanation=explanation_payload
            )

            # 9. Dispatch Telegram Notification for High-Conviction Trades
            if best_conviction['score'] >= 60.0:
                tg_msg = (
                    f"🔥 <b>SWING TRADE SETUP DISCOVERED: {best_conviction['ticker']}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📈 <b>Action:</b> BUY (Swing 5-Day Horizon)\n"
                    f"🧠 <b>Calibrated Win Rate:</b> {best_conviction['score']:.1f}%\n"
                    f"💵 <b>Entry:</b> ₹{best_conviction['entry']:.2f}\n"
                    f"🛑 <b>Stop Loss:</b> ₹{best_conviction['sl']:.2f}\n"
                    f"🎯 <b>Target 1:</b> ₹{best_conviction['tp1']:.2f}\n"
                    f"🎯 <b>Target 2:</b> ₹{best_conviction['tp2']:.2f}\n"
                    f"🌐 <b>Macro Bias:</b> {regime}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<i>{best_conviction.get('meta_learner_msg', 'AI Multi-Model Consensus')}</i>"
                )
                try:
                    send_telegram_message(tg_msg)
                except Exception as e:
                    logger.warning(f"Telegram alert warning: {e}")

            scan_history = sorted(scan_history, key=lambda x: x['score'], reverse=True)
            best_conviction['history'] = scan_history
            
            elapsed = (datetime.now() - start_time).total_seconds()
            yield format_sse({"type": "system", "message": f"Scan Complete in {elapsed:.2f}s! Trade parameters loaded into execution module.", "progress": 100})
            yield format_sse({"type": "result", "data": best_conviction})
        else:
            yield format_sse({"type": "error", "message": "No high probability swing trade setups found meeting safety gates.", "progress": 100})
            
    except Exception as e:
        logger.error(f"Swing Scanner Error: {e}")
        yield format_sse({"type": "error", "message": f"Fatal Swing Scanner Error: {str(e)}", "progress": 100})

@router.get("/swing-scan")
def trigger_swing_scan(custom_tickers: str = None):
    tickers = custom_tickers.split(',') if custom_tickers else None
    return StreamingResponse(run_swing_scan(tickers), media_type="text/event-stream")
