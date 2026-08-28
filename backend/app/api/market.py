from fastapi import APIRouter, HTTPException
from app.data.market_data import fetch_historical_data
from app.indicators.engine import apply_indicators
from app.models.strategy import IndicatorDef
import pandas as pd
from datetime import datetime, timedelta
import requests
import time

router = APIRouter()

@router.get("/search")
async def search_tickers(q: str):
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=15&newsCount=0"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        results = []
        for quote in data.get('quotes', []):
            # Only include equities, and prioritize Indian exchanges (NSI/NSE, BSE) 
            # Or allow all equities but add a flag. Let's include all but highlight Indian ones.
            if quote.get('quoteType') == 'EQUITY':
                symbol = quote.get('symbol')
                name = quote.get('longname') or quote.get('shortname')
                exchange = quote.get('exchDisp')
                
                # If they explicitly search Indian stocks, or we just return them
                results.append({
                    "symbol": symbol,
                    "name": name,
                    "exchange": exchange
                })
                
        # Optional: Sort so Indian stocks (.NS, .BO) appear first
        results.sort(key=lambda x: 0 if x['symbol'].endswith('.NS') or x['symbol'].endswith('.BO') else 1)
        
        return results
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/dump-stats")
def get_dump_stats():
    import sqlite3
    import os
    
    db_path = 'market_data.db'
    if not os.path.exists(db_path):
        return {"status": "empty", "message": "Cache database not found."}
        
    try:
        conn = sqlite3.connect(db_path)
        
        # Total rows in ohlcv
        cur = conn.execute("SELECT COUNT(*) FROM ohlcv")
        total_rows = cur.fetchone()[0]
        
        # Stats per ticker
        cur = conn.execute("""
            SELECT ticker, COUNT(*) as rows, MIN(date) as min_date, MAX(date) as max_date 
            FROM ohlcv 
            GROUP BY ticker
            ORDER BY rows DESC
        """)
        tickers = []
        for row in cur.fetchall():
            tickers.append({
                "ticker": row[0],
                "rows": row[1],
                "min_date": row[2],
                "max_date": row[3]
            })
            
        # Fetch log history
        cur = conn.execute("SELECT ticker, start_date, end_date FROM fetch_log ORDER BY end_date DESC LIMIT 100")
        fetch_logs = []
        for row in cur.fetchall():
            fetch_logs.append({
                "ticker": row[0],
                "start_date": row[1],
                "end_date": row[2]
            })
            
        # ML Training Rows
        try:
            cur = conn.execute("SELECT COUNT(*) FROM ml_training_data")
            ml_training_rows = cur.fetchone()[0]
        except:
            ml_training_rows = 0
            
        # ML Trades
        try:
            cur = conn.execute("SELECT COUNT(*) FROM ml_trade_history")
            ml_trades = cur.fetchone()[0]
        except:
            ml_trades = 0
            
        # DB Size
        size_bytes = os.path.getsize(db_path)
        db_size_mb = round(size_bytes / (1024 * 1024), 2)
        
        conn.close()
        
        return {
            "status": "success",
            "total_rows": total_rows,
            "tickers": tickers,
            "fetch_logs": fetch_logs,
            "db_size_mb": db_size_mb,
            "ml_training_rows": ml_training_rows,
            "ml_trade_count": ml_trades
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/latest/{ticker}")
async def get_latest_data(ticker: str):
    try:
        start_time = time.time()
        
        today = datetime.now()
        end_date = today + timedelta(days=1)
        start_date = today - timedelta(days=365)
        
        df = fetch_historical_data(
            ticker, 
            start_date.strftime('%Y-%m-%d'), 
            end_date.strftime('%Y-%m-%d')
        )
        
        source_str = df.attrs.get('source', 'Yahoo Finance')
        
        indicators = [
            IndicatorDef(name='sma', type='sma', params={'period': 20}),
            IndicatorDef(name='sma', type='sma', params={'period': 50}),
            IndicatorDef(name='sma', type='sma', params={'period': 200}),
            IndicatorDef(name='ema', type='ema', params={'period': 20}),
            IndicatorDef(name='ema', type='ema', params={'period': 50}),
            IndicatorDef(name='ema', type='ema', params={'period': 200}),
            IndicatorDef(name='rsi', type='rsi', params={'period': 14}),
            IndicatorDef(name='macd', type='macd', params={'fast': 12, 'slow': 26, 'signal': 9}),
            IndicatorDef(name='adx', type='adx', params={'period': 14}),
            IndicatorDef(name='bbands', type='bollinger', params={'period': 20, 'std_dev': 2.0}),
            IndicatorDef(name='stoch', type='stoch', params={'k_period': 14, 'd_period': 3}),
            IndicatorDef(name='atr', type='atr', params={'period': 14}),
            IndicatorDef(name='vwap', type='vwap', params={})
        ]
        
        df_indicators = apply_indicators(df, indicators)
        
        # Rename columns to match WatchlistScanner hardcoded keys
        rename_map = {
            "ema_period20": "ema_20",
            "ema_period50": "ema_50",
            "ema_period200": "ema_200",
            "rsi_period14": "rsi_14",
            "macd_fast12_signal9_slow26": "macd",
            "adx_period14": "adx",
            "bb_upper_period20_std_dev2.0": "bb_upper",
            "bb_lower_period20_std_dev2.0": "bb_lower",
            "stoch_k_k_period14_d_period3": "stoch_k",
            "stoch_d_k_period14_d_period3": "stoch_d"
        }
        df_indicators.rename(columns=rename_map, inplace=True)
        
        latest = df_indicators.iloc[-1].to_dict()
        prev = df_indicators.iloc[-2].to_dict()
        
        def safe_float(val):
            import math
            if isinstance(val, str):
                return val
            try:
                return float(val) if not pd.isna(val) and not math.isnan(val) else None
            except:
                return None
            
        clean_latest = {k: safe_float(v) if k != 'date' else str(v) for k, v in latest.items()}
        clean_prev = {k: safe_float(v) if k != 'date' else str(v) for k, v in prev.items()}
        
        if clean_latest.get('close') and clean_prev.get('close'):
            clean_latest['change_pct'] = ((clean_latest['close'] - clean_prev['close']) / clean_prev['close']) * 100
        else:
            clean_latest['change_pct'] = 0.0
            
        clean_latest['metadata'] = {
            "source": source_str,
            "processing_time_ms": round((time.time() - start_time) * 1000, 2)
        }
        
        return clean_latest
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
        
@router.delete("/clear-cache/{ticker}")
async def clear_cache(ticker: str):
    import sqlite3
    import os
    db_path = 'market_data.db'
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM ohlcv WHERE ticker = ?", (ticker,))
        conn.execute("DELETE FROM fetch_log WHERE ticker = ?", (ticker,))
        conn.commit()
        conn.close()
    return {"status": "success", "message": f"Cache cleared for {ticker}"}
