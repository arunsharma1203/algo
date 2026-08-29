import logging
from datetime import datetime
from app.api.intraday_ml import INDIAN_STOCK_UNIVERSE
import yfinance as yf
from app.api.ml_history import save_ml_training_data
import time
import pandas as pd
import ta

logger = logging.getLogger(__name__)

def hoard_intraday_data(data_source: str = 'yfinance', api_key: str = ''):
    """
    Background job to fetch 15m data for the universe, compute features, and cache it.
    """
    logger.info("Starting background intraday data hoarder job...")
    start_time = time.time()
    
    success_count = 0
    fail_count = 0
    
    for ticker in INDIAN_STOCK_UNIVERSE:
        try:
            import requests
            df = pd.DataFrame()
            
            if data_source == 'groww':
                headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                symbol = ticker.replace('.NS', '')
                # Trying standard official endpoint
                res = requests.get(f"https://api.groww.in/v1/historical/candle/range?exchange=NSE&segment=CASH&trading_symbol={symbol}&interval_in_minutes=15", headers=headers)
                
                if res.status_code != 200:
                    error_msg = res.text[:200] if res.text else res.reason
                    raise Exception(f"Groww API Error: {res.status_code} - {error_msg}")
                
                data = res.json()
                candles = data.get('candles', data.get('data', []))
                
                if not candles:
                    raise Exception(f"Groww API Success but could not locate 'candles' array in JSON: {str(data)[:200]}")
                    
                # Assuming [timestamp, open, high, low, close, volume]
                if isinstance(candles[0], list):
                    df = pd.DataFrame(candles, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
                    # Groww usually returns epoch timestamps
                    if len(str(df['datetime'].iloc[0])) == 10:
                        df['datetime'] = pd.to_datetime(df['datetime'], unit='s')
                    else:
                        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
                else:
                    df = pd.DataFrame(candles)
            
            elif data_source == 'upstox':
                from app.data.upstox_provider import fetch_upstox_candles
                df = fetch_upstox_candles(ticker, interval='15m', period='60d')
                    
            elif data_source == 'dhan':
                # DhanHQ SDK API implementation (REST)
                headers = {"access-token": api_key, "client-id": "SWING_AI"}
                res = requests.post("https://api.dhan.co/charts/intraday", json={"securityId": ticker, "exchangeSegment": "NSE_EQ", "instrument": "EQUITY"}, headers=headers)
                if res.status_code != 200:
                    raise Exception(f"Dhan API Error: {res.status_code} {res.reason} - Verify your API key.")
                    
            elif data_source == 'zerodha':
                # Zerodha Kite Connect API implementation (REST)
                headers = {"X-Kite-Version": "3", "Authorization": f"token {api_key}"}
                res = requests.get(f"https://api.kite.trade/instruments/historical/123456/15minute", headers=headers)
                if res.status_code != 200:
                    raise Exception(f"Zerodha API Error: {res.status_code} {res.reason} - Verify your API key.")
                    
            else:
                # Default Yahoo Finance (Free)
                df = yf.download(ticker, period="60d", interval="15m", progress=False)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.droplevel(1)
            
            if df.empty:
                fail_count += 1
                continue
                
            df = df.rename(columns={
                'Open': 'open', 'High': 'high', 'Low': 'low', 
                'Close': 'close', 'Volume': 'volume'
            })
            
            df = df.reset_index()
            # Ensure the first column is datetime
            df = df.rename(columns={df.columns[0]: 'datetime'})
            
            # Technical Indicators
            df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
            macd = ta.trend.MACD(df['close'])
            df['macd'] = macd.macd()
            df['macd_diff'] = macd.macd_diff()
            df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
            df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
            
            # ML Target
            df['returns'] = df['close'].pct_change()
            df['target'] = (df['returns'].shift(-1) > 0).astype(int) 
            
            ml_df = df.dropna().copy()
            ml_df['datetime'] = ml_df['datetime'].astype(str)
            
            ml_df.attrs['source'] = data_source
            save_ml_training_data(ticker, ml_df)
            success_count += 1
            time.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Failed to hoard data for {ticker}: {e}")
            fail_count += 1
            
    elapsed = round(time.time() - start_time, 2)
    logger.info(f"Hoarding complete. Success: {success_count}, Failed: {fail_count}, Time: {elapsed}s")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    hoard_intraday_data()

import json
def format_sse(data: dict) -> str:
    return f"{json.dumps(data)}\n"

def hoard_intraday_data_stream(data_source='yfinance', api_key=''):
    """
    Generator for streaming hoarder logs to frontend.
    """
    start_time = time.time()
    success_count = 0
    fail_count = 0
    
    total = len(INDIAN_STOCK_UNIVERSE)
    yield format_sse({"type": "info", "message": f"Starting Data Hoarder for {total} stocks..."})
    


    for i, ticker in enumerate(INDIAN_STOCK_UNIVERSE):
        try:
            import requests
            df = pd.DataFrame()
            
            if data_source == 'groww':
                headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                symbol = ticker.replace('.NS', '')
                # Trying standard official endpoint
                res = requests.get(f"https://api.groww.in/v1/historical/candle/range?exchange=NSE&segment=CASH&trading_symbol={symbol}&interval_in_minutes=15", headers=headers)
                
                if res.status_code != 200:
                    error_msg = res.text[:200] if res.text else res.reason
                    raise Exception(f"Groww API Error: {res.status_code} - {error_msg}")
                
                data = res.json()
                candles = data.get('candles', data.get('data', []))
                
                if not candles:
                    raise Exception(f"Groww API Success but could not locate 'candles' array in JSON: {str(data)[:200]}")
                    
                # Assuming [timestamp, open, high, low, close, volume]
                if isinstance(candles[0], list):
                    df = pd.DataFrame(candles, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
                    # Groww usually returns epoch timestamps
                    if len(str(df['datetime'].iloc[0])) == 10:
                        df['datetime'] = pd.to_datetime(df['datetime'], unit='s')
                    else:
                        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
                else:
                    df = pd.DataFrame(candles)
            
            elif data_source == 'upstox':
                from app.data.upstox_provider import fetch_upstox_candles
                df = fetch_upstox_candles(ticker, interval='15m', period='60d')
                    
            elif data_source == 'dhan':
                # DhanHQ SDK API implementation (REST)
                headers = {"access-token": api_key, "client-id": "SWING_AI"}
                res = requests.post("https://api.dhan.co/charts/intraday", json={"securityId": ticker, "exchangeSegment": "NSE_EQ", "instrument": "EQUITY"}, headers=headers)
                if res.status_code != 200:
                    raise Exception(f"Dhan API Error: {res.status_code} {res.reason} - Verify your API key.")
                    
            elif data_source == 'zerodha':
                # Zerodha Kite Connect API implementation (REST)
                headers = {"X-Kite-Version": "3", "Authorization": f"token {api_key}"}
                res = requests.get(f"https://api.kite.trade/instruments/historical/123456/15minute", headers=headers)
                if res.status_code != 200:
                    raise Exception(f"Zerodha API Error: {res.status_code} {res.reason} - Verify your API key.")
                    
            else:
                # Default Yahoo Finance (Free)
                df = yf.download(ticker, period="60d", interval="15m", progress=False)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.droplevel(1)
            
            if df.empty:
                fail_count += 1
                yield format_sse({"type": "error", "message": f"[{i+1}/{total}] {ticker} - No data found."})
                continue
                
            df = df.rename(columns={
                'Open': 'open', 'High': 'high', 'Low': 'low', 
                'Close': 'close', 'Volume': 'volume'
            })
            
            df = df.reset_index()
            df = df.rename(columns={df.columns[0]: 'datetime'})
            
            # Technical Indicators
            df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
            macd = ta.trend.MACD(df['close'])
            df['macd'] = macd.macd()
            df['macd_diff'] = macd.macd_diff()
            df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
            df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
            
            # ML Target
            df['returns'] = df['close'].pct_change()
            df['target'] = (df['returns'].shift(-1) > 0).astype(int) 
            
            ml_df = df.dropna().copy()
            ml_df['datetime'] = ml_df['datetime'].astype(str)
            
            ml_df.attrs['source'] = data_source
            save_ml_training_data(ticker, ml_df)
            success_count += 1
            yield format_sse({"type": "info", "message": f"[{i+1}/{total}] {ticker} - Hoarded {len(ml_df)} rows."})
            time.sleep(0.5)
            
        except Exception as e:
            fail_count += 1
            yield format_sse({"type": "error", "message": f"[{i+1}/{total}] {ticker} - Failed: {str(e)}"})
            
    elapsed = round(time.time() - start_time, 2)
    yield format_sse({"type": "success", "message": f"Complete. Success: {success_count}, Failed: {fail_count}, Time: {elapsed}s"})
