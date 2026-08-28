import sqlite3
import pandas as pd
import yfinance as yf
from datetime import datetime

def ensure_ml_table():
    conn = sqlite3.connect('market_data.db')
    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN trade_type TEXT DEFAULT 'INTRADAY'")
    except:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ml_trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            ticker TEXT,
            direction TEXT,
            entry REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            confidence REAL
        )
    """)
    conn.commit()
    conn.close()

def save_ml_trade(ticker, is_bullish, entry, sl, tp1, tp2, confidence, trade_type='INTRADAY'):
    ensure_ml_table()
    conn = sqlite3.connect('market_data.db')
    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN trade_type TEXT DEFAULT 'INTRADAY'")
    except:
        pass
    direction = "BULLISH" if is_bullish else "BEARISH"
    timestamp = datetime.now().isoformat()
    
    conn.execute("""
        INSERT INTO ml_trade_history (timestamp, ticker, direction, entry, sl, tp1, tp2, confidence, trade_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (timestamp, ticker, direction, float(entry), float(sl), float(tp1), float(tp2), float(confidence), trade_type))
    conn.commit()
    conn.close()

def evaluate_ml_history():
    ensure_ml_table()
    conn = sqlite3.connect('market_data.db')
    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN trade_type TEXT DEFAULT 'INTRADAY'")
    except:
        pass
    df_trades = pd.read_sql_query("SELECT * FROM ml_trade_history ORDER BY timestamp DESC", conn)
    conn.close()
    
    if df_trades.empty:
        return []
        
    results = []
    
    # Group by ticker to batch fetch data
    tickers = df_trades['ticker'].unique().tolist()
    
    # We only need data from the oldest trade timestamp, or just last 5 days
    # Fetched 60d to ensure Swing Trades have enough history to evaluate
    market_data = {}
    try:
        # bulk fetch
        if tickers:
            hist = yf.download(tickers, period="60d", interval="15m", progress=False)
            if len(tickers) == 1:
                market_data[tickers[0]] = hist
            else:
                for ticker in tickers:
                    if ticker in hist['Close']:
                        df_tick = pd.DataFrame({
                            'High': hist['High'][ticker],
                            'Low': hist['Low'][ticker],
                            'Close': hist['Close'][ticker]
                        })
                        market_data[ticker] = df_tick
    except:
        pass

    for _, row in df_trades.iterrows():
        ticker = row['ticker']
        entry_time_str = row['timestamp']
        try:
            entry_time = datetime.fromisoformat(entry_time_str)
        except:
            entry_time = pd.to_datetime(entry_time_str)
            
        direction = row['direction']
        sl = float(row['sl'])
        tp1 = float(row['tp1'])
        
        outcome = "OPEN"
        profit_pct = 0.0
        
        # Check against recent data
        if ticker in market_data:
            df = market_data[ticker].dropna()
            
            # Filter data AFTER the entry time
            # Note: entry_time might not match timezone, naive comparison for now
            df_future = df[df.index.tz_localize(None) >= entry_time.replace(tzinfo=None)]
            
            if not df_future.empty:
                for timestamp_idx, f_row in df_future.iterrows():
                    high = f_row['High']
                    low = f_row['Low']
                    close = f_row['Close']
                    
                    if direction == "BULLISH":
                        if low <= sl:
                            outcome = "SL HIT"
                            profit_pct = ((sl - row['entry']) / row['entry']) * 100
                            break
                        elif high >= tp1:
                            outcome = "TARGET MET"
                            profit_pct = ((tp1 - row['entry']) / row['entry']) * 100
                            break
                    else:
                        if high >= sl:
                            outcome = "SL HIT"
                            profit_pct = ((row['entry'] - sl) / row['entry']) * 100
                            break
                        elif low <= tp1:
                            outcome = "TARGET MET"
                            profit_pct = ((row['entry'] - tp1) / row['entry']) * 100
                            break
                            
                    # Intraday Square-off at 3:15 PM (15:15)
                    # If we haven't hit TP or SL by the end of the day, force exit
                    trade_type = row.get('trade_type', 'INTRADAY')
                    if trade_type == 'INTRADAY' and timestamp_idx.hour >= 15 and timestamp_idx.minute >= 15:
                        outcome = "SQUARED OFF (3:15 PM)"
                        if direction == "BULLISH":
                            profit_pct = ((close - row['entry']) / row['entry']) * 100
                        else:
                            profit_pct = ((row['entry'] - close) / row['entry']) * 100
                        break
                
                if outcome == "OPEN":
                    current_price = df_future.iloc[-1]['Close']
                    if direction == "BULLISH":
                        profit_pct = ((current_price - row['entry']) / row['entry']) * 100
                    else:
                        profit_pct = ((row['entry'] - current_price) / row['entry']) * 100
        
        results.append({
            "id": row['id'],
            "timestamp": entry_time_str[:16].replace("T", " "),
            "ticker": ticker,
            "direction": direction,
            "entry": row['entry'],
            "sl": row['sl'],
            "tp1": row['tp1'],
            "confidence": row['confidence'],
            "outcome": outcome,
            "profit_pct": round(profit_pct, 2)
        ,
            "trade_type": row.get("trade_type", "INTRADAY")
        })
        
    return results


def save_ml_training_data(ticker, df):
    import sqlite3
    conn = sqlite3.connect('market_data.db')
    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN trade_type TEXT DEFAULT 'INTRADAY'")
    except:
        pass
    
    # Ensure table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ml_training_data (
            datetime TEXT,
            ticker TEXT,
            close REAL,
            rsi REAL,
            macd REAL,
            macd_diff REAL,
            adx REAL,
            atr REAL,
            returns REAL,
            target INTEGER,
            PRIMARY KEY (ticker, datetime)
        )
    """)
    

    # Safely migrate existing tables to include auditing columns
    try:
        conn.execute("ALTER TABLE ml_training_data ADD COLUMN source TEXT DEFAULT 'yfinance'")
        conn.execute("ALTER TABLE ml_training_data ADD COLUMN hoard_timestamp TEXT")
    except Exception:
        pass # Columns already exist
        
    # Prepare df
    save_df = df[['datetime', 'close', 'rsi', 'macd', 'macd_diff', 'adx', 'atr', 'returns', 'target']].copy()
    save_df['ticker'] = ticker
    save_df['source'] = df.attrs.get('source', 'yfinance')
    from datetime import datetime as dt
    save_df['hoard_timestamp'] = dt.now().isoformat()
    
    # Convert datetime to string if not already
    save_df['datetime'] = save_df['datetime'].astype(str)
    
    # Write to temp
    save_df.to_sql('temp_ml_data', conn, if_exists='replace', index=False)
    
    # Insert or replace
    conn.execute("""
        INSERT OR REPLACE INTO ml_training_data (datetime, ticker, close, rsi, macd, macd_diff, adx, atr, returns, target, source, hoard_timestamp)
        SELECT datetime, ticker, close, rsi, macd, macd_diff, adx, atr, returns, target, source, hoard_timestamp FROM temp_ml_data
    """)
    conn.commit()
    conn.close()

def get_ml_training_data(ticker):
    import sqlite3
    import pandas as pd
    conn = sqlite3.connect('market_data.db')
    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN trade_type TEXT DEFAULT 'INTRADAY'")
    except:
        pass
    try:
        df = pd.read_sql_query("SELECT * FROM ml_training_data WHERE ticker = ? ORDER BY datetime ASC", conn, params=(ticker,))
    except:
        df = pd.DataFrame()
    conn.close()
    return df


