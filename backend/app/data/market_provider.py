import logging
import sqlite3
import pandas as pd
import yfinance as yf
from typing import Dict, Any, Optional
from app.data.upstox_provider import fetch_upstox_candles, fetch_upstox_ltp, test_upstox_connection

logger = logging.getLogger(__name__)

def get_active_data_source() -> str:
    """Reads configured market data source from SQLite."""
    try:
        conn = sqlite3.connect('market_data.db', timeout=5.0)
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = 'market_data_source'")
        row = cur.fetchone()
        conn.close()
        return row[0] if row and row[0] else 'yfinance'
    except Exception as e:
        logger.warning(f"Failed to read market_data_source: {e}")
        return 'yfinance'

def fetch_candles(ticker: str, interval: str = "15m", period: str = "60d") -> pd.DataFrame:
    """
    Unified candle fetcher routing to Upstox (live real-time) or YFinance (fallback/historical).
    """
    source = get_active_data_source()
    
    if source == 'upstox':
        try:
            df = fetch_upstox_candles(ticker, interval=interval, period=period)
            if not df.empty and len(df) > 10:
                logger.info(f"Loaded {len(df)} real-time candles for {ticker} from Upstox.")
                return df
            else:
                logger.warning(f"Upstox returned empty candles for {ticker}; falling back to YFinance.")
        except Exception as e:
            logger.warning(f"Upstox candle fetch failed for {ticker}: {e}. Falling back to YFinance.")
            
    # Fallback / Default: Yahoo Finance
    try:
        clean_ticker = ticker if ticker.endswith(('.NS', '.BO')) else f"{ticker}.NS"
        df = yf.download(clean_ticker, period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        if df.empty:
            return pd.DataFrame()
            
        df = df.rename(columns={
            'Open': 'open', 'High': 'high', 'Low': 'low', 
            'Close': 'close', 'Volume': 'volume'
        })
        df = df.reset_index()
        df = df.rename(columns={df.columns[0]: 'datetime'})
        return df
    except Exception as e:
        logger.error(f"YFinance download failed for {ticker}: {e}")
        return pd.DataFrame()

def fetch_live_quote(ticker: str) -> Optional[float]:
    """Fetches real-time LTP quote with Upstox or fallback."""
    source = get_active_data_source()
    if source == 'upstox':
        price = fetch_upstox_ltp(ticker)
        if price is not None and price > 0:
            return price
            
    # Fallback to fast YFinance
    try:
        clean_ticker = ticker if ticker.endswith(('.NS', '.BO')) else f"{ticker}.NS"
        t = yf.Ticker(clean_ticker)
        fast = t.fast_info
        if hasattr(fast, 'last_price') and fast.last_price:
            return float(fast.last_price)
    except Exception as e:
        logger.warning(f"Fallback quote failed for {ticker}: {e}")
    return None
