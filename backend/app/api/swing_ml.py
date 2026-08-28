import json
import time
from datetime import datetime
import pandas as pd
import yfinance as yf
import ta
import numpy as np
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.svm import SVC
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.ensemble import VotingClassifier

router = APIRouter()

from app.api.intraday_ml import INDIAN_STOCK_UNIVERSE

def format_sse(data: dict) -> str:
    return f"{json.dumps(data)}\n"

def run_swing_scan(custom_tickers: list = None):
    try:
        universe = INDIAN_STOCK_UNIVERSE.copy()
        if custom_tickers:
            for t in custom_tickers:
                if t and t not in universe:
                    universe.append(t)
        
        yield format_sse({"type": "system", "message": f"Initializing Swing Trade ML Pipeline for {len(universe)} symbols..."})
        yield format_sse({"type": "info", "message": "Establishing connection to historical daily data vaults...", "progress": 5})
        
        best_conviction = None
        best_score = 0
        total = len(universe)
        progress_step = 80 / max(1, total)
        current_progress = 10
        
        scan_history = []
        
        yield format_sse({"type": "info", "message": "Analyzing Macro Market Regime (NIFTY 50 & INDIA VIX)...", "progress": 5})
        
        from app.analytics.macro_engine import get_macro_regime
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
                yield format_sse({"type": "info", "message": f"⚠️ VIX Spike Detected! (INDIA VIX = {vix_val:.2f}). High volatility warning. -10 Penalty applied."})
            elif vix_status == "LOW":
                yield format_sse({"type": "info", "message": f"💤 VIX is extremely low ({vix_val:.2f}). Markets are very quiet."})
            else:
                yield format_sse({"type": "info", "message": f"📊 VIX is NORMAL ({vix_val:.2f}). Safe environment for Swing Trades."})
        else:
            yield format_sse({"type": "error", "message": f"Failed to fetch Macro indicators. Defaulting to Neutral Regime."})
            
        yield format_sse({"type": "system", "message": f"Bulk fetching 5 years of daily data for {len(universe)} symbols..."})
        try:
            bulk_data = yf.download(universe, period="5y", interval="1d", progress=False)
        except Exception as e:
            yield format_sse({"type": "error", "message": f"Bulk fetch failed: {str(e)}"})
            return
            
        yield format_sse({"type": "system", "message": f"Data fetched successfully. Commencing AI modeling..."})
        
        for idx, ticker in enumerate(universe):
            try:
                # Extract ticker data from bulk MultiIndex
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
                        
                df = df.dropna(how='all')
                if df.empty or len(df) < 200:
                    continue
                
                df = df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
                
                # Feature Engineering                # Calculate indicators
                df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
                macd = ta.trend.MACD(df['close'])
                df['macd'] = macd.macd()
                df['macd_diff'] = macd.macd_diff()
                df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
                df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
                
                df['future_5d'] = df['close'].shift(-5)
                df['future_return'] = (df['future_5d'] - df['close']) / df['close']
                # Swing ML Target: Will it go up at least 3% over the next 5 days?
                df['target'] = (df['future_return'] > 0.03).astype(int)
                
                ml_df = df.dropna().copy()
                if len(ml_df) < 500:
                    continue
                    
                features = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']
                X = ml_df[features].values
                y = ml_df['target'].values
                
                # Load Bayesian Tuned Hyperparameters
                from app.analytics.optuna_tuner import load_best_params
                hp = load_best_params()

                # Random Forest (Optuna Tuned)
                rf_clf = RandomForestClassifier(
                    n_estimators=hp.get('rf_n_estimators', 100),
                    max_depth=hp.get('rf_max_depth', 5),
                    min_samples_split=hp.get('rf_min_samples_split', 2),
                    random_state=42
                )
                
                # Gradient Boosting (Optuna Tuned)
                gb_clf = GradientBoostingClassifier(
                    n_estimators=hp.get('gb_n_estimators', 100),
                    learning_rate=hp.get('gb_learning_rate', 0.1),
                    max_depth=hp.get('gb_max_depth', 3),
                    random_state=42
                )
                
                # Support Vector Machine (Optuna Tuned)
                svm_clf = make_pipeline(StandardScaler(), SVC(C=hp.get('svm_c', 1.0), probability=True, random_state=42))
                
                # Ensemble Voting Classifier
                ensemble = VotingClassifier(
                    estimators=[('rf', rf_clf), ('gb', gb_clf), ('svm', svm_clf)],
                    voting='soft'
                )
                
                ensemble.fit(X, y)
                
                # Get the absolute latest data point (live candle)
                latest_data = df.iloc[-1]
                latest_features = latest_data[features].values.reshape(1, -1)
                
                # Replace NaNs with 0 in live data to prevent crashes
                latest_features = np.nan_to_num(latest_features)
                
                # Predict
                prob = ensemble.predict_proba(latest_features)[0]
                bullish_prob = prob[1] * 100 
                
                # Calculate Conviction Score (Max 150)
                technical_bonus = 0
                if latest_data['rsi'] < 40 and latest_data['macd_diff'] > 0:
                    technical_bonus += 20
                if latest_data['adx'] > 25:
                    technical_bonus += 15
                    
                score = bullish_prob + technical_bonus
                
                # Record to history
                current_price = float(latest_data['close'])
                atr = float(latest_data['atr'])
                atr_pct_val = (atr / current_price * 100) if current_price > 0 else 2.0
                
                vol_sma20 = df['volume'].rolling(20).mean().iloc[-1] if 'volume' in df.columns else 0
                vol_ratio = float(latest_data['volume'] / vol_sma20) if (vol_sma20 and vol_sma20 > 0) else 1.0
                
                # Swing Trading Stop Loss (Wider: 2x ATR)
                sl = current_price - (atr * 2)
                # Swing Target 1 (1:1.5)
                tp1 = current_price + (atr * 3)
                # Swing Target 2 (1:3)
                tp2 = current_price + (atr * 6)
                
                scan_history.append({
                    "ticker": ticker,
                    "score": round(score, 1),
                    "action": "BUY" if score > 75 else "HOLD",
                    "prob": round(bullish_prob, 1),
                    "price": current_price
                })
                
                # Apply Bearish Macro Penalty
                if regime == "BEARISH":
                    score -= 20
                    
                if score > best_score:
                    best_score = score
                    best_conviction = {
                        "ticker": ticker,
                        "action": "BUY",
                        "is_bullish": True,
                        "score": score,
                        "prob": bullish_prob,
                        "entry": current_price,
                        "sl": sl,
                        "tp1": tp1,
                        "tp2": tp2,
                        "atr_pct": round(atr_pct_val, 2),
                        "volume_ratio": round(vol_ratio, 2),
                        "rsi": float(latest_data['rsi']),
                        "macd_diff": float(latest_data['macd_diff']),
                        "adx": float(latest_data['adx']),
                        "timestamp": datetime.now().isoformat()
                    }
                    yield format_sse({"type": "info", "message": f"🔥 New high conviction swing setup found: {ticker} (Score: {score:.1f})"})
                
                current_progress += progress_step
                yield format_sse({"progress": int(current_progress)})
                
            except Exception as e:
                yield format_sse({"type": "error", "message": f"Failed evaluating {ticker}: {str(e)}"})
        
        yield format_sse({"type": "system", "message": "Swing Market Sweep Complete. Finalizing neural pathways...", "progress": 95})
        time.sleep(1)
        
        if best_conviction:
            yield format_sse({"type": "info", "message": f"Running NLP Sentiment Analysis on live news for {best_conviction['ticker']}...", "progress": 96})
            import asyncio
            # In sync context, we can just call it
            from app.analytics.nlp_engine import nlp_engine
            nlp_result = nlp_engine.analyze_ticker_news(best_conviction['ticker'])
            
            sentiment_score = nlp_result['score']
            headline = nlp_result['headline']
            
            # NLP adjustments:
            penalty = 0
            if sentiment_score < -50:
                penalty = -15
                yield format_sse({"type": "info", "message": f"🚨 CRITICAL NLP WARNING: Highly negative news detected (Sentiment: {sentiment_score}). Applying -15 Conviction Penalty."})
                yield format_sse({"type": "info", "message": f"🗞️ Latest Headline: \"{headline}\""})
            elif sentiment_score < -20:
                penalty = -5
                yield format_sse({"type": "info", "message": f"⚠️ Mildly negative news detected (Sentiment: {sentiment_score}). Applying -5 Conviction Penalty."})
            elif sentiment_score > 50:
                penalty = 5
                yield format_sse({"type": "info", "message": f"🔥 NLP Catalyst Detected! Highly positive news (Sentiment: {sentiment_score}). Giving +5 Conviction Boost."})
                yield format_sse({"type": "info", "message": f"🗞️ Latest Headline: \"{headline}\""})
            else:
                yield format_sse({"type": "info", "message": f"News Sentiment is NEUTRAL (Score: {sentiment_score}). Proceeding with pure technicals."})
                
            best_conviction['score'] += penalty
            best_conviction['nlp_sentiment'] = sentiment_score
            best_conviction['nlp_headline'] = headline


            # ==========================================
            # META-LEARNER / MULTI-FACTOR TELEMETRY INJECTION
            # ==========================================
            try:
                from app.analytics.meta_learner import meta_learner
                yield format_sse({"type": "info", "message": "Invoking Meta-Learner (Layer 2) with Volume/Volatility/Macro telemetry..."})
                
                adjusted_score, meta_message, telemetry = meta_learner.evaluate_new_trade(
                    ticker=best_conviction['ticker'],
                    direction="BULLISH" if best_conviction['is_bullish'] else "BEARISH",
                    trade_type="SWING",
                    base_confidence=best_conviction['score'],
                    nlp_sentiment=best_conviction.get('nlp_sentiment', 0),
                    macro_state=macro,
                    atr_pct=best_conviction.get('atr_pct', 2.0),
                    volume_ratio=best_conviction.get('volume_ratio', 1.0)
                )
                
                best_conviction['score'] = adjusted_score
                best_conviction['meta_learner_msg'] = meta_message
                best_conviction['telemetry'] = telemetry
                
                yield format_sse({"type": "info", "message": f"🤖 {meta_message}"})
            except Exception as e:
                yield format_sse({"type": "error", "message": f"Meta-Learner Offline: {e}"})

            # ==========================================
            # PROBABILITY CALIBRATION LAYER
            # ==========================================
            try:
                from app.analytics.calibration import calibrator
                calibrated_score, raw_score, calib_meta = calibrator.calibrate(best_conviction['score'])
                best_conviction['raw_score'] = raw_score
                best_conviction['score'] = calibrated_score
                best_conviction['calibration'] = calib_meta
                yield format_sse({"type": "info", "message": f"📊 Calibrated Win Probability: {calibrated_score}% (Raw Score: {raw_score}%)"})
            except Exception as e:
                best_conviction['raw_score'] = best_conviction['score']
                best_conviction['calibration'] = {"raw_score": best_conviction['score'], "calibrated_score": best_conviction['score']}
                
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
                "adjustments": best_conviction.get("telemetry", {}).get("adjustments_breakdown", {}),
                "meta_message": best_conviction.get("meta_learner_msg", "")
            }

            # Save to SQLite History
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
            
            # Push to Telegram!
            try:
                from app.analytics.telegram_notifier import send_telegram_message
                trade_type = 'SWING'
                direction = '🟢 BUY' if best_conviction['is_bullish'] else '🔴 SHORT'
                
                lines = [
                    f"<b>🎯 NEW {trade_type} ALERT: {best_conviction['ticker']}</b>",
                    f"<b>Action:</b> {direction}",
                    f"<b>Entry:</b> ₹{best_conviction['entry']:.2f}",
                    f"<b>Stop Loss:</b> ₹{best_conviction['sl']:.2f}",
                    f"<b>Target:</b> ₹{best_conviction['tp1']:.2f}",
                    f"<b>Conviction:</b> {best_conviction['score']:.1f}/100"
                ]
                send_telegram_message("\n".join(lines))
            except Exception as e:
                pass
                
            # Sort history
            scan_history = sorted(scan_history, key=lambda x: x['score'], reverse=True)
            best_conviction['history'] = scan_history
            
            yield format_sse({"type": "system", "message": "Scan Complete! Trade parameters loaded into execution module.", "progress": 100})
            yield format_sse({"type": "result", "data": best_conviction})
        else:
            yield format_sse({"type": "error", "message": "No high probability swing trade setups found in the current market environment.", "progress": 100})
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        yield format_sse({"type": "error", "message": f"Fatal Swing Scanner Error: {str(e)}", "progress": 100})

@router.get("/swing-scan")
def trigger_swing_scan(custom_tickers: str = None):
    tickers = custom_tickers.split(',') if custom_tickers else None
    return StreamingResponse(run_swing_scan(tickers), media_type="text/event-stream")
