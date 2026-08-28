from fastapi import APIRouter
from fastapi.responses import StreamingResponse
import yfinance as yf
import pandas as pd
import numpy as np
import ta
import json
import asyncio
from sklearn.ensemble import RandomForestClassifier, HistGradientBoostingClassifier
from sklearn.svm import SVC
from app.api.ml_lab import save_feature_importance

router = APIRouter()

# Expanded Universe: Nifty 100 + prominent Midcaps + "Volume Shockers"
INDIAN_STOCK_UNIVERSE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", 
    "SBI.NS", "BHARTIARTL.NS", "ITC.NS", "LT.NS", "BAJFINANCE.NS",
    "HCLTECH.NS", "MARUTI.NS", "SUNPHARMA.NS", "TATAMOTORS.NS", "KOTAKBANK.NS",
    "ONGC.NS", "NTPC.NS", "AXISBANK.NS", "WIPRO.NS", "M&M.NS",
    "ULTRACEMCO.NS", "POWERGRID.NS", "TITAN.NS", "ASIANPAINT.NS", "BAJAJFINSV.NS",
    "NESTLEIND.NS", "JSWSTEEL.NS", "TATASTEEL.NS", "ADANIENT.NS", "ADANIPORTS.NS",
    "GRASIM.NS", "TECHM.NS", "HINDALCO.NS", "DIVISLAB.NS", "SBILIFE.NS",
    "LTIM.NS", "BAJAJ-AUTO.NS", "EICHERMOT.NS", "INDUSINDBK.NS", "DRREDDY.NS",
    "CIPLA.NS", "APOLLOHOSP.NS", "TATACOMM.NS", "HDFCLIFE.NS", "BRITANNIA.NS",
    "COALINDIA.NS", "HEROMOTOCO.NS", "TATACONSUM.NS", "BPCL.NS", "UPL.NS",
    "ZOMATO.NS", "JIOFIN.NS", "TRENT.NS", "HAL.NS", "BEL.NS",
    "BHEL.NS", "PFC.NS", "RECLTD.NS", "IRFC.NS", "RVNL.NS",
    "MAZDOCK.NS", "IREDA.NS", "NHPC.NS", "SJVN.NS", "SUZLON.NS",
    "IDEA.NS", "YESBANK.NS", "PNB.NS", "BANKBARODA.NS", "UNIONBANK.NS",
    "CANBK.NS", "IDFCFIRSTB.NS", "BSE.NS", "CDSL.NS", "MCX.NS",
    "IEX.NS", "AWL.NS", "ATGL.NS", "AMBUJACEM.NS", "ACC.NS",
    "DIXON.NS", "POLYCAB.NS", "KALYANKJIL.NS", "HUDCO.NS", "NBCC.NS",
    "MMTC.NS", "IRCTC.NS", "GMRINFRA.NS", "INDIGO.NS", "TVSMOTOR.NS",
    "MOTHERSON.NS", "BOSCHLTD.NS", "MRF.NS", "PIDILITIND.NS", "PAGEIND.NS",
    "NAUKRI.NS", "PAYTM.NS", "NYKAA.NS", "DELHIVERY.NS", "POLICYBZR.NS"
]

def format_sse(data: dict) -> str:
    return json.dumps(data) + "\n"

async def run_ml_scan(custom_list=None):
    if custom_list is None:
        custom_list = []
        
    yield format_sse({"type": "info", "message": "Analyzing Macro Market Regime (NIFTY 50 & INDIA VIX)...", "progress": 1})
    from app.analytics.macro_engine import get_macro_regime
    macro = get_macro_regime()
    
    nifty_trend = macro['nifty_trend_short']
    vix_status = macro['vix_status']
    vix_val = macro['vix_close']
    
    if macro['nifty_close'] > 0:
        if nifty_trend == "BULLISH":
            yield format_sse({"type": "info", "message": f"📈 Intraday Bias is BULLISH (NIFTY {macro['nifty_close']:.2f} > 20 EMA {macro['ema_20']:.2f})."})
        else:
            yield format_sse({"type": "info", "message": f"📉 Intraday Bias is BEARISH (NIFTY {macro['nifty_close']:.2f} < 20 EMA {macro['ema_20']:.2f})."})
            
        if vix_status == "HIGH":
            yield format_sse({"type": "info", "message": f"⚠️ VIX Spike Detected! (INDIA VIX = {vix_val:.2f}). Intraday noise will be extreme. -15 Penalty applied."})
        elif vix_status == "LOW":
            yield format_sse({"type": "info", "message": f"💤 VIX is extremely low ({vix_val:.2f}). Intraday breakouts may fail. -10 Penalty applied."})
        else:
            yield format_sse({"type": "info", "message": f"📊 VIX is NORMAL ({vix_val:.2f}). Optimal conditions for Intraday."})

    yield format_sse({"type": "info", "message": f"Fetching live volumes for an expanded universe of {len(INDIAN_STOCK_UNIVERSE)} stocks (including mid/small caps)...", "progress": 2})
    
    # Bulk fetch 1-day volume to find the most heavily traded stocks today
    try:
        vol_df = yf.download(INDIAN_STOCK_UNIVERSE, period="1d", progress=False)['Volume']
        # Take the most recent row
        if len(vol_df) > 0:
            latest_vol = vol_df.iloc[-1]
            # Sort descending and get top 25 volume shockers
            top_stocks = latest_vol.sort_values(ascending=False).head(25).index.tolist()
            yield format_sse({"type": "info", "message": f"Identified top 25 high-volume momentum stocks across all caps.", "progress": 5})
        else:
            top_stocks = INDIAN_STOCK_UNIVERSE[:25]
    except Exception as e:
        print("Volume fetch error:", e)
        top_stocks = INDIAN_STOCK_UNIVERSE[:25]

    # Append custom watchlist stocks if they aren't already in top_stocks
    for custom_ticker in custom_list:
        clean_ticker = custom_ticker.strip()
        if clean_ticker and clean_ticker not in top_stocks:
            top_stocks.append(clean_ticker)
            
    yield format_sse({"type": "info", "message": f"Added custom stock scanner symbols. Total scanning universe: {len(top_stocks)} stocks.", "progress": 6})

    await asyncio.sleep(0.5)
    
    results = []
    total = len(top_stocks)
    
    yield format_sse({"type": "info", "message": f"Bulk fetching 60-day deep 15m history for {total} stocks...", "progress": 10})
    try:
        bulk_data = yf.download(top_stocks, period="60d", interval="15m", progress=False)
    except Exception as e:
        yield format_sse({"type": "error", "message": f"Bulk fetch failed: {e}", "progress": 100})
        return
        
    for idx, ticker in enumerate(top_stocks):
        progress = 15 + int((idx / total) * 75)
        
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
            
            if df.empty or len(df) < 500:
                continue
                
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
                
            df.reset_index(inplace=True)
            df.columns = [col.lower() for col in df.columns]
            
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col.capitalize() in df.columns:
                    df.rename(columns={col.capitalize(): col}, inplace=True)
                elif col.upper() in df.columns:
                    df.rename(columns={col.upper(): col}, inplace=True)
            
            yield format_sse({"type": "info", "message": f"Calculating complex features (MACD/RSI/ADX) on {len(df)} candles for {ticker}...", "progress": progress + 1})
            
            # Technical Indicators
            df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
            macd = ta.trend.MACD(df['close'])
            df['macd'] = macd.macd()
            df['macd_diff'] = macd.macd_diff()
            df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
            df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
            
            # ML Target: 1 if next candle returns positive, 0 otherwise
            df['returns'] = df['close'].pct_change()
            df['target'] = (df['returns'].shift(-1) > 0).astype(int) 
            
            ml_df = df.dropna().copy()
            if len(ml_df) < 100:
                continue
                
            from app.api.ml_history import get_ml_training_data, save_ml_training_data
            
            # Fetch accumulated historical data (overcomes YFinance 60-day limit over time)
            hist_ml_df = get_ml_training_data(ticker)
            if not hist_ml_df.empty:
                # Combine historical and fresh data
                ml_df['datetime'] = ml_df['datetime'].astype(str)
                combined_df = pd.concat([hist_ml_df, ml_df]).drop_duplicates(subset=['datetime'], keep='last').sort_values('datetime')
                train_df = combined_df.copy()
            else:
                train_df = ml_df.copy()
                train_df['datetime'] = train_df['datetime'].astype(str)
                
            # Save the fresh data back to DB to build our dataset
            save_ml_training_data(ticker, train_df)
                
            features = ['rsi', 'macd', 'macd_diff', 'adx', 'returns']
            X = train_df[features].values
            y = train_df['target'].values
            
            yield format_sse({"type": "info", "message": f"Training Ensemble Committee (RF + GB + SVM) on {len(X)} historical rows...", "progress": progress + 2})
            
            # 1. Random Forest
            rf_clf = RandomForestClassifier(n_estimators=100, random_state=42, max_depth=5)
            rf_clf.fit(X, y)
            
            # 2. Gradient Boosting
            gb_clf = HistGradientBoostingClassifier(random_state=42, max_depth=5)
            gb_clf.fit(X, y)
            
            # 3. Support Vector Machine
            svm_clf = SVC(probability=True, random_state=42)
            svm_clf.fit(X, y)
            
            # Save Feature Importances from RF
            try:
                importances = rf_clf.feature_importances_
                save_feature_importance(ticker, importances, features)
            except:
                pass
            
            # Predict
            latest_features = ml_df[features].iloc[-1].values.reshape(1, -1)
            prob_rf = rf_clf.predict_proba(latest_features)[0][1]
            prob_gb = gb_clf.predict_proba(latest_features)[0][1]
            prob_svm = svm_clf.predict_proba(latest_features)[0][1]
            
            # Ensemble Vote
            prob_up = (prob_rf + prob_gb + prob_svm) / 3.0
            
            latest_close = ml_df['close'].iloc[-1]
            latest_adx = ml_df['adx'].iloc[-1]
            latest_rsi = ml_df['rsi'].iloc[-1]
            latest_atr = ml_df['atr'].iloc[-1]
            latest_macd_diff = ml_df['macd_diff'].iloc[-1]
            
            score = (prob_up * 100) + (latest_adx * 0.5)
            is_bullish = bool(prob_up > 0.5)
            
            # Dynamic ATR sizing based on recent volatility
            recent_returns_std = ml_df['returns'].tail(10).std()
            baseline_std = 0.002 # Baseline 0.2% volatility per 15m candle
            
            atr_multiplier = 1.5
            if recent_returns_std > baseline_std * 1.5:
                atr_multiplier = 2.0 # Widen stops for high volatility
            elif recent_returns_std < baseline_std * 0.5:
                atr_multiplier = 1.2 # Tighten stops for low volatility
                
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
                "prob_up": float(prob_up * 100),
                "adx": float(latest_adx),
                "rsi": float(latest_rsi),
                "macd_diff": float(latest_macd_diff),
                "is_bullish": is_bullish,
                "entry": float(latest_close),
                "sl": float(sl),
                "tp1": float(tp1),
                "tp2": float(tp2),
            })
            
        except Exception as e:
            continue

    scanned_tickers = [r['ticker'] for r in results]
    scanned_str = ", ".join(scanned_tickers)
    yield format_sse({"type": "info", "message": f"Successfully deep-scanned {len(results)} stocks: {scanned_str}", "progress": 94})
    
    yield format_sse({"type": "info", "message": "Ranking results and extracting highest conviction trade...", "progress": 95})
    await asyncio.sleep(1)
    
    results.sort(key=lambda x: x['score'], reverse=True)
    
    if len(results) == 0:
        yield format_sse({"type": "error", "message": "Scan completed but no suitable setups found.", "progress": 100})
        return
        
    best_trade = results[0]
    best_ticker = best_trade['ticker']
    best_trade['scanned_tickers'] = scanned_tickers
    
    yield format_sse({"type": "info", "message": f"Running NLP Sentiment Analysis on live news for {best_ticker}...", "progress": 96})
    
    try:
        from app.analytics.nlp_engine import nlp_engine
        nlp_result = nlp_engine.analyze_ticker_news(best_ticker)
        sentiment_score = nlp_result['score']
        headline = nlp_result['headline']
        
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
            
        best_trade['score'] += penalty
        best_trade['nlp_sentiment'] = sentiment_score
        best_trade['nlp_headline'] = headline
    except Exception as e:
        yield format_sse({"type": "error", "message": f"NLP Engine failed: {e}. Skipping sentiment check."})
        best_trade['nlp_sentiment'] = 0
        best_trade['nlp_headline'] = "NLP Analysis failed."

    yield format_sse({"type": "info", "message": f"Winner finalized: {best_ticker}. Saving to AI Trade History...", "progress": 98})
    
    # ==========================================
    # META-LEARNER / FEEDBACK LOOP INJECTION
    # ==========================================
    try:
        from app.analytics.meta_learner import meta_learner
        yield format_sse({"type": "info", "message": "Invoking Meta-Learner (Layer 2) to cross-reference historical AI mistakes..."})
        
        adjusted_score, meta_message = meta_learner.evaluate_new_trade(
            ticker=best_trade['ticker'],
            direction="BULLISH" if best_trade['is_bullish'] else "BEARISH",
            trade_type="INTRADAY",
            base_confidence=best_trade['prob_up'],
            nlp_sentiment=best_trade.get('nlp_sentiment', 0)
        )
        
        best_trade['prob_up'] = adjusted_score
        best_trade['meta_learner_msg'] = meta_message
        
        yield format_sse({"type": "info", "message": f"🤖 {meta_message}"})
    except Exception as e:
        yield format_sse({"type": "error", "message": f"Meta-Learner Offline: {e}"})

    # Save the real ML trade to history
    from app.api.ml_history import save_ml_trade, evaluate_ml_history
    save_ml_trade(
        ticker=best_trade['ticker'],
        is_bullish=best_trade['is_bullish'],
        entry=best_trade['entry'],
        sl=best_trade['sl'],
        tp1=best_trade['tp1'],
        tp2=best_trade['tp2'],
        confidence=best_trade['prob_up']
    )
    
    # Fetch global history evaluated against live prices
    history = evaluate_ml_history()
    best_trade['history'] = history

    yield format_sse({"type": "info", "message": "Scan complete!", "progress": 100})
    yield format_sse({"type": "result", "data": best_trade})

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
async def get_ml_history():
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
