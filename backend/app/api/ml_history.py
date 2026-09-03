import sqlite3
import json
import math
import pandas as pd
import yfinance as yf
from datetime import datetime
from app.data.historical_data_layer import get_db_path

def ensure_ml_table():
    conn = sqlite3.connect(get_db_path(), timeout=30.0)
    for col, col_type in [
        ('trade_type', "TEXT DEFAULT 'INTRADAY'"),
        ('status', "TEXT DEFAULT 'OPEN'"),
        ('explanation', "TEXT"),
        ('outcome', "TEXT"),
        ('profit_pct', "REAL"),
        ('effective_entry', "REAL"),
        ('slippage_drag', "REAL"),
        ('ideal_profit_pct', "REAL"),
        ('exit_price', "REAL"),
        ('exit_time', "TEXT"),
        ('source', "TEXT DEFAULT 'MANUAL'"),
        ('position_type', "TEXT DEFAULT 'NOT_A_POSITION'"),
        ('tightened_sl', "REAL"),
        ('ai_guard_action', "TEXT"),
        ('risk_level', "TEXT DEFAULT 'NORMAL'"),
        ('risk_reasons', "TEXT"),
        ('risk_updated_at', "TEXT"),
        ('current_price', "REAL"),
        ('reference_price', "REAL"),
        ('model_candle_close', "REAL"),
        ('price_source', "TEXT"),
        ('price_timestamp', "TEXT"),
        ('price_is_fresh', "INTEGER DEFAULT 0")
    ]:
        try:
            conn.execute(f"ALTER TABLE ml_trade_history ADD COLUMN {col} {col_type}")
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
            explanation TEXT,
            outcome TEXT,
            profit_pct REAL,
            effective_entry REAL,
            slippage_drag REAL,
            ideal_profit_pct REAL,
            exit_price REAL,
            exit_time TEXT,
            source TEXT DEFAULT 'MANUAL',
            position_type TEXT DEFAULT 'NOT_A_POSITION',
            tightened_sl REAL,
            ai_guard_action TEXT,
            risk_level TEXT DEFAULT 'NORMAL',
            risk_reasons TEXT,
            risk_updated_at TEXT,
            current_price REAL,
            reference_price REAL,
            model_candle_close REAL,
            price_source TEXT,
            price_timestamp TEXT,
            price_is_fresh INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def save_ml_trade(ticker, is_bullish, entry, sl, tp1, tp2, confidence, trade_type='INTRADAY', explanation=None, source='MANUAL', position_type='NOT_A_POSITION'):
    """
    Persists a trade recommendation or position to ml_trade_history.
    
    Args:
        source: WHO created this record. 'MANUAL' (manual scan), 'AUTOPILOT', or 'BROKER'.
        position_type: WHAT this record represents. 'NOT_A_POSITION' (tracked recommendation), 
                       'PAPER_POSITION' (paper position), or 'LIVE_POSITION' (broker-executed).
    
    Returns:
        True if record was saved, False if deduplicated (skipped) or disallowed.
    """
    # ── HARD FAIL-SAFE: STRICT TICKER VALIDATION GATE ────────────────
    clean_ticker = str(ticker).strip().upper()
    is_system_test = (clean_ticker == "TESTSTOCK.NS" or source == "SYSTEM_TEST" or position_type == "SYSTEM_TEST")
    
    if not is_system_test:
        if clean_ticker.startswith(("CACHE_", "TEMP_", "DUMMY_", "MOCK_")) or "CACHE" in clean_ticker:
            import logging
            logging.getLogger(__name__).error(f"[save_ml_trade] Blocked malformed/cache ticker '{ticker}'. Ticker cannot enter ml_trade_history.")
            try:
                from app.analytics.master_logger import MasterLogger
                MasterLogger.log_event("DATA_GATE", "REJECTED_MALFORMED_TICKER", f"Blocked malformed/cache ticker: {ticker}", ticker=ticker, severity="ERROR")
            except Exception:
                pass
            return False

        import re
        if not re.match(r"^[A-Z0-9_\-]{1,20}(\.(NS|BO))?$", clean_ticker):
            import logging
            logging.getLogger(__name__).error(f"[save_ml_trade] Invalid ticker format '{ticker}'.")
            return False

    # ── HARD FAIL-SAFE: CASH-EQUITY SWING SHORT BAN ──────────────────
    if trade_type == 'SWING' and not is_bullish:
        import logging
        logging.getLogger(__name__).warning(f"[save_ml_trade] Refusing to persist BEARISH SWING trade for {ticker}. Cash shorts disallowed.")
        return False

    ensure_ml_table()
    conn = sqlite3.connect(get_db_path(), timeout=30.0)
    direction = "BULLISH" if is_bullish else "BEARISH"
    now = datetime.now()
    timestamp = now.isoformat()

    explanation_str = json.dumps(explanation) if explanation is not None else None
    ref_price = float(entry)
    m_close = float(entry)
    p_source = "Market Feed"
    p_ts = now.strftime("%H:%M:%S IST")
    p_fresh = 0

    if isinstance(explanation, dict):
        if "reference_price" in explanation:
            ref_price = float(explanation["reference_price"])
        if "model_candle_close" in explanation:
            m_close = float(explanation["model_candle_close"])
        if "price_source" in explanation:
            p_source = str(explanation["price_source"])
        if "price_timestamp" in explanation:
            p_ts = str(explanation["price_timestamp"])
        if "price_is_fresh" in explanation:
            p_fresh = 1 if explanation["price_is_fresh"] else 0

    # DEDUPLICATION LOGIC:
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
            
        if last_time.date() == now.date() and abs(last_confidence - float(confidence)) < 0.001:
            conn.close()
            return False

    conn.execute("""
        INSERT INTO ml_trade_history (
            timestamp, ticker, direction, entry, sl, tp1, tp2, confidence,
            trade_type, status, outcome, explanation, source, position_type,
            reference_price, model_candle_close, price_source, price_timestamp, price_is_fresh
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        timestamp, ticker, direction, float(entry), float(sl), float(tp1), float(tp2),
        float(confidence), trade_type, explanation_str, source, position_type,
        ref_price, m_close, p_source, p_ts, p_fresh
    ))
    conn.commit()
    conn.close()

    # Bust evaluation cache immediately so newly saved trades appear instantly
    global _EVAL_CACHE
    _EVAL_CACHE['data'] = None
    _EVAL_CACHE['timestamp'] = 0

    return True

_EVAL_CACHE = {
    'data': None,
    'timestamp': 0
}
_EVAL_CACHE_TTL = 15  # 15 seconds cache

_MARKET_DATA_CACHE = {
    'data': {},
    'timestamp': 0
}
_MARKET_DATA_TTL = 300  # 5 minutes cache

def evaluate_ml_history(force_refresh: bool = False):
    """
    Evaluates recorded ML trade history against subsequent market price action.
    Resolved trades are permanently stored in SQLite for instant retrieval.
    """
    import time as time_module
    epoch_now = time_module.time()
    if not force_refresh and _EVAL_CACHE['data'] is not None and (epoch_now - _EVAL_CACHE['timestamp']) < _EVAL_CACHE_TTL:
        return _EVAL_CACHE['data']

    ensure_ml_table()
    conn = sqlite3.connect(get_db_path(), timeout=30.0)
    df_trades = pd.read_sql_query("SELECT * FROM ml_trade_history ORDER BY id DESC", conn)
    conn.close()
    
    if df_trades.empty:
        _EVAL_CACHE['data'] = []
        _EVAL_CACHE['timestamp'] = epoch_now
        return []
        
    # Pre-fetch Macro State once for all trades
    from app.analytics.macro_engine import get_macro_regime
    macro = get_macro_regime()
    
    # 1. Identify which tickers need fresh candle data (unfinalized trades)
    unresolved_df = df_trades[df_trades['outcome'].isna() | (df_trades['outcome'] == 'OPEN') | (df_trades['status'] != 'CLOSED')]
    unresolved_tickers = unresolved_df['ticker'].unique().tolist() if not unresolved_df.empty else []
    
    market_data = {}
    if unresolved_tickers:
        # Check in-memory market data cache first
        if (epoch_now - _MARKET_DATA_CACHE['timestamp']) < _MARKET_DATA_TTL and all(t in _MARKET_DATA_CACHE['data'] for t in unresolved_tickers):
            market_data = _MARKET_DATA_CACHE['data']
        else:
            try:
                hist = yf.download(unresolved_tickers, period="60d", interval="15m", progress=False, timeout=8)
                if len(unresolved_tickers) == 1:
                    market_data[unresolved_tickers[0]] = hist
                else:
                    for ticker in unresolved_tickers:
                        if ticker in hist['Close']:
                            df_tick = pd.DataFrame({
                                'High': hist['High'][ticker],
                                'Low': hist['Low'][ticker],
                                'Close': hist['Close'][ticker]
                            })
                            market_data[ticker] = df_tick
                _MARKET_DATA_CACHE['data'].update(market_data)
                _MARKET_DATA_CACHE['timestamp'] = epoch_now
            except Exception as e:
                logger.warning(f"yfinance batch download warning: {e}")

    results = []
    finalized_updates = []

    for _, row in df_trades.iterrows():
        trade_id = row['id']
        ticker = row['ticker']
        entry_time_str = row['timestamp']
        try:
            entry_time = datetime.fromisoformat(entry_time_str).replace(tzinfo=None)
        except:
            entry_time = pd.to_datetime(entry_time_str).replace(tzinfo=None)
            
        direction = row['direction']
        sl = float(row['sl'])
        tp1 = float(row['tp1'])
        raw_entry = float(row['entry'])
        trade_type = row.get('trade_type', 'INTRADAY')
        
        slippage_pct = 0.08 if trade_type == 'INTRADAY' else 0.12
        effective_entry = raw_entry * (1 + slippage_pct / 100.0) if direction == "BULLISH" else raw_entry * (1 - slippage_pct / 100.0)
        
        explanation_data = None
        if 'explanation' in row and pd.notna(row['explanation']) and row['explanation']:
            try:
                explanation_data = json.loads(row['explanation'])
            except:
                explanation_data = None

        # 2. Check if this trade is invalidated (e.g. historical cash swing short)
        if row.get('status') == 'INVALIDATED':
            outcome = row.get('outcome') or 'SWING_CASH_SHORT_DISALLOWED'
            results.append({
                "id": trade_id,
                "timestamp": entry_time_str[:16].replace("T", " "),
                "ticker": ticker,
                "direction": direction,
                "entry": raw_entry,
                "effective_entry": round(effective_entry, 2),
                "slippage_pct": slippage_pct,
                "slippage_drag": 0.0,
                "ideal_profit_pct": 0.0,
                "sl": sl,
                "tp1": tp1,
                "confidence": row['confidence'],
                "outcome": outcome,
                "status": "INVALIDATED",
                "profit_pct": 0.0,
                "trade_type": trade_type,
                "explanation": explanation_data,
                "risk_audit": None,
                "current_price": raw_entry,
                "reference_price": float(row.get('reference_price', raw_entry) or raw_entry),
                "price_source": row.get('price_source', 'Candle Close') if pd.notna(row.get('price_source')) else 'Candle Close',
                "price_timestamp": row.get('price_timestamp', '') if pd.notna(row.get('price_timestamp')) else '',
                "price_is_fresh": bool(row.get('price_is_fresh', False)) if pd.notna(row.get('price_is_fresh')) else False,
                "source": row.get('source', 'MANUAL') if pd.notna(row.get('source')) else 'MANUAL',
                "position_type": row.get('position_type', 'NOT_A_POSITION') if pd.notna(row.get('position_type')) else 'NOT_A_POSITION',
                "tightened_sl": None,
                "ai_guard_action": None,
                "risk_level": "NORMAL"
            })
            continue

        # 3. Check if this trade is already finalized in the database
        saved_outcome = row.get('outcome')
        if row.get('status') == 'CLOSED' and pd.notna(saved_outcome) and saved_outcome not in ('OPEN', None):
            outcome = saved_outcome
            profit_pct = float(row.get('profit_pct', 0.0))
            ideal_profit_pct = float(row.get('ideal_profit_pct', profit_pct))
            saved_eff = row.get('effective_entry')
            if pd.notna(saved_eff):
                effective_entry = float(saved_eff)
            saved_drag = row.get('slippage_drag')
            slippage_drag = float(saved_drag) if pd.notna(saved_drag) else round(ideal_profit_pct - profit_pct, 2)
            current_price = raw_entry
            risk_audit_data = None
        else:
            outcome = "OPEN"
            ideal_profit_pct = 0.0
            profit_pct = 0.0
            current_price = raw_entry
            
            if ticker in market_data:
                df = market_data[ticker].dropna()
                df_future = df[df.index.tz_localize(None) >= entry_time]
                
                if not df_future.empty:
                    for timestamp_idx, f_row in df_future.iterrows():
                        high = float(f_row['High'])
                        low = float(f_row['Low'])
                        close = float(f_row['Close'])
                        t_time = timestamp_idx.replace(tzinfo=None)
                        
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
                        else:  # BEARISH
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
                                
                        # Intraday 3:15 PM Square-off Rule
                        if trade_type == 'INTRADAY':
                            if (t_time.date() == entry_time.date() and (t_time.hour > 15 or (t_time.hour == 15 and t_time.minute >= 15))) or (t_time.date() > entry_time.date()):
                                outcome = "SQUARED OFF (3:15 PM)"
                                if direction == "BULLISH":
                                    ideal_profit_pct = ((close - raw_entry) / raw_entry) * 100
                                    profit_pct = ((close - effective_entry) / effective_entry) * 100
                                else:
                                    ideal_profit_pct = ((raw_entry - close) / raw_entry) * 100
                                    profit_pct = ((effective_entry - close) / effective_entry) * 100
                                break
                    
                    if outcome == "OPEN":
                        current_price = float(df_future.iloc[-1]['Close'])
                        if direction == "BULLISH":
                            ideal_profit_pct = ((current_price - raw_entry) / raw_entry) * 100
                            profit_pct = ((current_price - effective_entry) / effective_entry) * 100
                        else:
                            ideal_profit_pct = ((raw_entry - current_price) / raw_entry) * 100
                            profit_pct = ((effective_entry - current_price) / effective_entry) * 100
            
            now_dt = datetime.now()
            # If Intraday and entry occurred during an actual trading session that has since closed
            if trade_type == 'INTRADAY' and outcome == 'OPEN':
                is_entry_weekday = (entry_time.weekday() < 5)
                is_now_weekday = (now_dt.weekday() < 5)
                if is_entry_weekday:
                    if entry_time.date() < now_dt.date() or (entry_time.date() == now_dt.date() and is_now_weekday and (now_dt.hour > 15 or (now_dt.hour == 15 and now_dt.minute >= 30))):
                        outcome = "SQUARED OFF (3:15 PM)"

            # ── SWING 5-TRADING-DAY HORIZON EXPIRATION ───────────────────
            # Swing trades that remain unresolved after 5 trading days are closed
            if trade_type == 'SWING' and outcome == 'OPEN':
                try:
                    trading_days_elapsed = int(np.busday_count(entry_time.date(), now_dt.date()))
                except Exception:
                    trading_days_elapsed = max(0, (now_dt.date() - entry_time.date()).days * 5 // 7)

                if trading_days_elapsed >= 5:
                    outcome = "SWING_HORIZON_REACHED"
                    if direction == "BULLISH":
                        ideal_profit_pct = ((current_price - raw_entry) / raw_entry) * 100
                        profit_pct = ((current_price - effective_entry) / effective_entry) * 100
                    else:
                        ideal_profit_pct = ((raw_entry - current_price) / raw_entry) * 100
                        profit_pct = ((effective_entry - current_price) / effective_entry) * 100
                    
            slippage_drag = round(ideal_profit_pct - profit_pct, 2)
            
            # Risk Audit for open trades
            risk_audit_data = None
            if outcome == "OPEN":
                try:
                    from app.analytics.autonomous_bot import evaluate_single_trade_risk
                    risk_audit_data = evaluate_single_trade_risk(
                        trade={
                            'id': trade_id,
                            'ticker': ticker,
                            'direction': direction,
                            'entry': raw_entry,
                            'sl': sl,
                            'trade_type': trade_type,
                            'confidence': row['confidence'],
                            'timestamp': entry_time_str
                        },
                        current_price=current_price
                    )
                except Exception as e:
                    risk_audit_data = None
            else:
                # Collect finalized trade for permanent DB persistence
                finalized_updates.append((
                    outcome,
                    round(profit_pct, 2),
                    round(effective_entry, 2),
                    slippage_drag,
                    round(ideal_profit_pct, 2),
                    'CLOSED',
                    trade_id
                ))

        explanation_data = None
        if 'explanation' in row and pd.notna(row['explanation']) and row['explanation']:
            try:
                explanation_data = json.loads(row['explanation'])
            except:
                explanation_data = None

        tightened_sl_val = row.get('tightened_sl') if 'tightened_sl' in row and pd.notna(row['tightened_sl']) else None
        if risk_audit_data and risk_audit_data.get('tightened_sl'):
            tightened_sl_val = risk_audit_data.get('tightened_sl')

        ai_guard_action_val = row.get('ai_guard_action') if 'ai_guard_action' in row and pd.notna(row['ai_guard_action']) else None
        if risk_audit_data:
            r_level = risk_audit_data.get('risk_level', 'NORMAL')
            ai_guard_action_val = "EXIT_ADVISORY" if r_level == "CRITICAL" else ("TIGHTEN_SL" if r_level == "WARNING" else "MAINTAIN")

        current_risk_level = risk_audit_data.get('risk_level', 'NORMAL') if risk_audit_data else (row.get('risk_level') if 'risk_level' in row and pd.notna(row['risk_level']) else 'NORMAL')

        def _clean_float(val, default=0.0):
            if val is None or pd.isna(val):
                return default
            try:
                f = float(val)
                if math.isnan(f) or math.isinf(f):
                    return default
                return f
            except:
                return default

        def _clean_optional_float(val, ndigits=2):
            if val is None or pd.isna(val):
                return None
            try:
                f = float(val)
                if math.isnan(f) or math.isinf(f):
                    return None
                return round(f, ndigits)
            except:
                return None

        clean_raw_entry = _clean_float(raw_entry, 0.0)
        clean_eff_entry = _clean_float(effective_entry, clean_raw_entry)
        clean_ref_price = _clean_float(row.get('reference_price'), clean_raw_entry)
        clean_curr_price = _clean_float(current_price, clean_raw_entry)
        clean_profit_pct = _clean_float(profit_pct, 0.0)
        clean_ideal_profit = _clean_float(ideal_profit_pct, 0.0)
        clean_slippage_drag = _clean_float(slippage_drag, 0.0)
        clean_slippage_pct = _clean_float(slippage_pct, 0.0)
        clean_conf = _clean_float(row.get('confidence'), 0.0)

        results.append({
            "id": trade_id,
            "timestamp": entry_time_str[:16].replace("T", " "),
            "ticker": ticker,
            "direction": direction,
            "entry": clean_raw_entry,
            "effective_entry": round(clean_eff_entry, 2),
            "slippage_pct": clean_slippage_pct,
            "slippage_drag": round(clean_slippage_drag, 2),
            "ideal_profit_pct": round(clean_ideal_profit, 2),
            "sl": _clean_float(sl, 0.0),
            "tp1": _clean_float(tp1, 0.0),
            "tp2": _clean_optional_float(row.get('tp2')),
            "confidence": clean_conf,
            "outcome": outcome,
            "status": 'CLOSED' if outcome != 'OPEN' else 'OPEN',
            "profit_pct": round(clean_profit_pct, 2),
            "trade_type": trade_type,
            "explanation": explanation_data,
            "risk_audit": risk_audit_data,
            "current_price": round(clean_curr_price, 2),
            "reference_price": round(clean_ref_price, 2),
            "price_source": str(row.get('price_source', 'Candle Close')) if pd.notna(row.get('price_source')) else 'Candle Close',
            "price_timestamp": str(row.get('price_timestamp', '')) if pd.notna(row.get('price_timestamp')) else '',
            "price_is_fresh": bool(row.get('price_is_fresh', False)) if pd.notna(row.get('price_is_fresh')) else False,
            "source": str(row.get('source', 'MANUAL')) if pd.notna(row.get('source')) else 'MANUAL',
            "position_type": str(row.get('position_type', 'NOT_A_POSITION')) if pd.notna(row.get('position_type')) else 'NOT_A_POSITION',
            "tightened_sl": _clean_optional_float(tightened_sl_val),
            "ai_guard_action": ai_guard_action_val,
            "risk_level": current_risk_level
        })

    # 3. Persist finalized trades into SQLite in a single atomic batch
    if finalized_updates:
        try:
            batch_conn = sqlite3.connect(get_db_path(), timeout=15.0)
            batch_conn.executemany("""
                UPDATE ml_trade_history 
                SET outcome = ?, profit_pct = ?, effective_entry = ?, slippage_drag = ?, ideal_profit_pct = ?, status = ?
                WHERE id = ?
            """, finalized_updates)
            batch_conn.commit()
            batch_conn.close()
        except Exception as e:
            logger.warning(f"Batch outcome persistence error: {e}")

    # Recursive sanitizer to guarantee zero NaN / Inf reach JSON serializer
    def _sanitize(obj):
        if isinstance(obj, float):
            return 0.0 if (math.isnan(obj) or math.isinf(obj)) else obj
        elif isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_sanitize(x) for x in obj]
        return obj

    clean_results = _sanitize(results)
    _EVAL_CACHE['data'] = clean_results
    _EVAL_CACHE['timestamp'] = time_module.time()
    return clean_results


def save_ml_training_data(ticker, df):
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
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
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
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


