import yfinance as yf
import pandas as pd
import sqlite3
from datetime import datetime

CACHE_DB = 'market_data.db'

def get_db_connection():
    conn = sqlite3.connect(CACHE_DB)
    # The actual market data
    conn.execute('''
        CREATE TABLE IF NOT EXISTS ohlcv (
            ticker TEXT,
            date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            PRIMARY KEY (ticker, date)
        )
    ''')
    # A table to track contiguous blocks of data we have successfully fetched
    conn.execute('''
        CREATE TABLE IF NOT EXISTS fetch_log (
            ticker TEXT,
            start_date TEXT,
            end_date TEXT
        )
    ''')
    return conn

def is_range_cached(conn, ticker: str, start_date: str, end_date: str) -> bool:
    # We check if there's any single fetch_log entry that completely covers the requested range
    # (For MVP, we just check if it's completely enveloped by a past fetch. If we wanted to get fancy,
    # we could merge ranges, but just re-fetching and upserting the whole new range is foolproof and fine).
    query = "SELECT start_date, end_date FROM fetch_log WHERE ticker = ?"
    cur = conn.execute(query, (ticker,))
    for row in cur.fetchall():
        cached_start, cached_end = row[0], row[1]
        if cached_start <= start_date and cached_end >= end_date:
            return True
    return False

def fetch_historical_data(ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
    if not ticker.endswith('.NS') and not ticker.endswith('.BO'):
        ticker += '.NS'
        
    conn = get_db_connection()
    
    # Check if we already have this exact period (or a larger enveloping period) in our foolproof log
    already_cached = is_range_cached(conn, ticker, start_date, end_date)
    
    if already_cached:
        # 100% foolproof cache hit
        query = f"SELECT * FROM ohlcv WHERE ticker = '{ticker}' AND date >= '{start_date}' AND date <= '{end_date} 23:59:59' ORDER BY date ASC"
        df_cache = pd.read_sql(query, conn)
        conn.close()
        df_cache['date'] = pd.to_datetime(df_cache['date'])
        df_cache.attrs['source'] = 'Local Database (Cache Hit)'
        return df_cache
        
    # Not fully cached, we must fetch from YFinance
    try:
        df = yf.download(ticker, start=start_date, end=end_date)
        if df.empty:
            raise ValueError(f"No data found for {ticker}")
            
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            
        df.reset_index(inplace=True)
        df.columns = [col.lower() for col in df.columns]
        
        for col in ['date', 'open', 'high', 'low', 'close', 'volume']:
            if col not in df.columns:
                if col.capitalize() in df.columns:
                    df.rename(columns={col.capitalize(): col}, inplace=True)
                elif col.upper() in df.columns:
                    df.rename(columns={col.upper(): col}, inplace=True)
        
        # Save to DB with REPLACE to absolutely ensure overlaps are overwritten perfectly (foolproof)
        df_save = df.copy()
        df_save['ticker'] = ticker
        df_save['date'] = df_save['date'].astype(str)
        df_save.to_sql('temp_ohlcv', conn, if_exists='replace', index=False)
        
        conn.execute('''
            INSERT OR REPLACE INTO ohlcv (ticker, date, open, high, low, close, volume)
            SELECT ticker, date, open, high, low, close, volume FROM temp_ohlcv
        ''')
        
        # Log this successful contiguous fetch
        conn.execute("INSERT INTO fetch_log (ticker, start_date, end_date) VALUES (?, ?, ?)", 
                    (ticker, start_date, end_date))
        conn.commit()
        
        df.attrs['source'] = 'Yahoo Finance (Live)'
        
    except Exception as e:
        # Fallback to whatever is in the cache if YFinance is offline or fails
        query = f"SELECT * FROM ohlcv WHERE ticker = '{ticker}' AND date >= '{start_date}' AND date <= '{end_date} 23:59:59' ORDER BY date ASC"
        df = pd.read_sql(query, conn)
        df['date'] = pd.to_datetime(df['date'])
        df.attrs['source'] = 'Local Database (Fallback)'
        if df.empty:
            raise ValueError(f"No data found for {ticker}, and not in cache.")
    
    finally:
        conn.close()

    return df
