import sqlite3
import json
import pandas as pd
import yfinance as yf
from datetime import datetime

def ensure_ml_table():
    conn = sqlite3.connect('market_data.db')
    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN trade_type TEXT DEFAULT 'INTRADAY'")
    except:
        pass
        
    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN status TEXT DEFAULT 'OPEN'")
    except:
        pass

    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN explanation TEXT")
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
            confidence REAL,
            status TEXT DEFAULT 'OPEN',
            trade_type TEXT DEFAULT 'INTRADAY',
            explanation TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_ml_trade(ticker, is_bullish, entry, sl, tp1, tp2, confidence, trade_type='INTRADAY', explanation=None):
    ensure_ml_table()
    conn = sqlite3.connect('market_data.db')
    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN trade_type TEXT DEFAULT 'INTRADAY'")
    except:
        pass
        
    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN status TEXT DEFAULT 'OPEN'")
    except:
        pass

    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN explanation TEXT")
    except:
        pass

    direction = "BULLISH" if is_bullish else "BEARISH"
    now = datetime.now()
    timestamp = now.isoformat()

    explanation_str = json.dumps(explanation) if explanation is not None else None

    # DEDUPLICATION LOGIC:
    # Skip saving if it's the exact same trade on the exact same day WITH the exact same confidence score.
    cur = conn.execute("""
        SELECT timestamp, confidence FROM ml_trade_history 
        WHERE ticker = ? AND trade_type = ? AND direction = ?
        ORDER BY id DESC LIMIT 1
    """, (ticker, trade_type, direction))
    
    last_trade = cur.fetchone()
    if last_trade:
        import pandas as pd
        last_time_str = last_trade[0]
        last_confidence = last_trade[1]
        try:
            last_time = datetime.fromisoformat(last_time_str)
        except:
            last_time = pd.to_datetime(last_time_str)
            
        # Only skip if it's the exact same day AND the EXACT SAME confidence score.
        if last_time.date() == now.date() and abs(last_confidence - float(confidence)) < 0.001:
            conn.close()
            return
    

    
    conn.execute("""
        INSERT INTO ml_trade_history (timestamp, ticker, direction, entry, sl, tp1, tp2, confidence, trade_type, explanation)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (timestamp, ticker, direction, float(entry), float(sl), float(tp1), float(tp2), float(confidence), trade_type, explanation_str))
    conn.commit()
    conn.close()

_EVAL_CACHE = {
    'data': None,
    'timestamp': 0
}
_EVAL_CACHE_TTL = 30  # 30 seconds cache

def evaluate_ml_history(force_refresh: bool = False):
    """
    Evaluates recorded ML trade history against subsequent market price action.
    Cached for 30s for instant UI loading without blocking on network requests.
    """
    import time as time_module
    epoch_now = time_module.time()
    if not force_refresh and _EVAL_CACHE['data'] is not None and (epoch_now - _EVAL_CACHE['timestamp']) < _EVAL_CACHE_TTL:
        return _EVAL_CACHE['data']

    ensure_ml_table()
    conn = sqlite3.connect('market_data.db', timeout=30.0)
    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN trade_type TEXT DEFAULT 'INTRADAY'")
    except:
        pass
        
    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN status TEXT DEFAULT 'OPEN'")
    except:
        pass

    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN explanation TEXT")
    except:
        pass
    df_trades = pd.read_sql_query("SELECT * FROM ml_trade_history ORDER BY timestamp DESC", conn)
    conn.close()
    
    if df_trades.empty:
        _EVAL_CACHE['data'] = []
        _EVAL_CACHE['timestamp'] = now
        return []
        
    results = []
    
    # Pre-fetch Macro State once for all trades
    from app.analytics.macro_engine import get_macro_regime
    macro = get_macro_regime()
    
    # Group by ticker to batch fetch data ONLY for currently OPEN trades (sub-second performance)
    open_trades_df = df_trades[df_trades['status'] != 'CLOSED']
    open_tickers = open_trades_df['ticker'].unique().tolist() if not open_trades_df.empty else []
    
    market_data = {}
    if open_tickers:
        try:
            hist = yf.download(open_tickers, period="5d", interval="15m", progress=False, timeout=5)
            if len(open_tickers) == 1:
                market_data[open_tickers[0]] = hist
            else:
                for ticker in open_tickers:
                    if ticker in hist['Close']:
                        df_tick = pd.DataFrame({
                            'High': hist['High'][ticker],
                            'Low': hist['Low'][ticker],
                            'Close': hist['Close'][ticker]
                        })
                        market_data[ticker] = df_tick
        except Exception as e:
            logger.warning(f"Fast yfinance download failed: {e}")
            pass

    closed_ids_to_update = []
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
        raw_entry = float(row['entry'])
        trade_type = row.get('trade_type', 'INTRADAY')
        
        # Realistic Execution Slippage Friction Modeling (Execution delay + Bid-Ask Spread)
        slippage_pct = 0.08 if trade_type == 'INTRADAY' else 0.12
        effective_entry = raw_entry * (1 + slippage_pct / 100.0) if direction == "BULLISH" else raw_entry * (1 - slippage_pct / 100.0)
        
        outcome = "OPEN"
        ideal_profit_pct = 0.0
        profit_pct = 0.0
        
        # Check against recent data
        if ticker in market_data:
            df = market_data[ticker].dropna()
            
            # Filter data AFTER the entry time (skipping identical entry candle to model 5-min execution lag)
            df_future = df[df.index.tz_localize(None) >= entry_time.replace(tzinfo=None)]
            
            if not df_future.empty:
                for timestamp_idx, f_row in df_future.iterrows():
                    high = f_row['High']
                    low = f_row['Low']
                    close = f_row['Close']
                    
                    if direction == "BULLISH":
                        if low <= sl:
                            outcome = "SL HIT"
                            ideal_profit_pct = ((sl - raw_entry) / raw_entry) * 100
                            profit_pct = ((sl - effective_entry) / effective_entry) * 100
                            break
                        elif high >= tp1:
                            outcome = "TARGET MET"
                            ideal_profit_pct = ((tp1 - raw_entry) / raw_entry) * 100
                            profit_pct = ((tp1 - effective_entry) / effective_entry) * 100
                            break
                    else:
                        if high >= sl:
                            outcome = "SL HIT"
                            ideal_profit_pct = ((raw_entry - sl) / raw_entry) * 100
                            profit_pct = ((effective_entry - sl) / effective_entry) * 100
                            break
                        elif low <= tp1:
                            outcome = "TARGET MET"
                            ideal_profit_pct = ((raw_entry - tp1) / raw_entry) * 100
                            profit_pct = ((effective_entry - tp1) / effective_entry) * 100
                            break
                            
                    # Intraday Square-off at 3:15 PM (15:15)
                    # If we haven't hit TP or SL by the end of the day, force exit
                    if trade_type == 'INTRADAY' and timestamp_idx.hour >= 15 and timestamp_idx.minute >= 15:
                        outcome = "SQUARED OFF (3:15 PM)"
                        if direction == "BULLISH":
                            ideal_profit_pct = ((close - raw_entry) / raw_entry) * 100
                            profit_pct = ((close - effective_entry) / effective_entry) * 100
                        else:
                            ideal_profit_pct = ((raw_entry - close) / raw_entry) * 100
                            profit_pct = ((effective_entry - close) / effective_entry) * 100
                        break
                
                if outcome == "OPEN":
                    current_price = df_future.iloc[-1]['Close']
                    if direction == "BULLISH":
                        ideal_profit_pct = ((current_price - raw_entry) / raw_entry) * 100
                        profit_pct = ((current_price - effective_entry) / effective_entry) * 100
                    else:
                        ideal_profit_pct = ((raw_entry - current_price) / raw_entry) * 100
                        profit_pct = ((effective_entry - current_price) / effective_entry) * 100
        
        
        # Auto-close INTRADAY trades if market is closed (IST > 15:30 or < 9:00)
        from datetime import datetime
        now = datetime.now()
        
        if trade_type == 'INTRADAY' and outcome == 'OPEN':
            if now.hour > 15 or (now.hour == 15 and now.minute >= 30) or now.hour < 9:
                outcome = 'MARKET_CLOSED'
        
        # Collect closed IDs for a single batch update at the end
        if outcome != 'OPEN':
            closed_ids_to_update.append(row['id'])
                
        explanation_data = None
        if 'explanation' in row and pd.notna(row['explanation']) and row['explanation']:
            try:
                explanation_data = json.loads(row['explanation'])
            except:
                explanation_data = None

        risk_audit_data = None
        if outcome == "OPEN":
            try:
                from app.analytics.autonomous_bot import evaluate_single_trade_risk
                risk_audit_data = evaluate_single_trade_risk(
                    trade={
                        'id': row['id'],
                        'ticker': ticker,
                        'direction': direction,
                        'entry': raw_entry,
                        'sl': row['sl'],
                        'trade_type': trade_type,
                        'confidence': row['confidence'],
                        'timestamp': entry_time_str
                    },
                    current_price=current_price if 'current_price' in locals() else raw_entry
                )
            except Exception as e:
                risk_audit_data = None

        results.append({
            "id": row['id'],
            "timestamp": entry_time_str[:16].replace("T", " "),
            "ticker": ticker,
            "direction": direction,
            "entry": raw_entry,
            "effective_entry": round(effective_entry, 2),
            "slippage_pct": slippage_pct,
            "slippage_drag": round(ideal_profit_pct - profit_pct, 2),
            "ideal_profit_pct": round(ideal_profit_pct, 2),
            "sl": row['sl'],
            "tp1": row['tp1'],
            "confidence": row['confidence'],
            "outcome": outcome,
            "profit_pct": round(profit_pct, 2),
            "trade_type": trade_type,
            "explanation": explanation_data,
            "risk_audit": risk_audit_data
        })
        
    if closed_ids_to_update:
        try:
            batch_conn = sqlite3.connect('market_data.db', timeout=10.0)
            batch_conn.executemany("UPDATE ml_trade_history SET status = 'CLOSED' WHERE id = ?", [(cid,) for cid in closed_ids_to_update])
            batch_conn.commit()
            batch_conn.close()
        except Exception as e:
            logger.warning(f"Batch status sync failed: {e}")

    _EVAL_CACHE['data'] = results
    _EVAL_CACHE['timestamp'] = time_module.time()
    return results


def save_ml_training_data(ticker, df):
    import sqlite3
    conn = sqlite3.connect('market_data.db')
    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN trade_type TEXT DEFAULT 'INTRADAY'")
    except:
        pass
        
    try:
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN status TEXT DEFAULT 'OPEN'")
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
        conn.execute("ALTER TABLE ml_trade_history ADD COLUMN status TEXT DEFAULT 'OPEN'")
    except:
        pass
    try:
        df = pd.read_sql_query("SELECT * FROM ml_training_data WHERE ticker = ? ORDER BY datetime ASC", conn, params=(ticker,))
    except:
        df = pd.DataFrame()
    conn.close()
    return df


