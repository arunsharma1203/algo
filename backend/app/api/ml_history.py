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

    # DETERMINISTIC DEDUPLICATION LOGIC:
    # Check if an active OPEN setup or recommendation already exists for this (ticker, trade_type, direction)
    cur = conn.execute("""
        SELECT id, timestamp, confidence FROM ml_trade_history 
        WHERE ticker = ? AND trade_type = ? AND direction = ? AND (status = 'OPEN' OR outcome = 'OPEN')
        ORDER BY id DESC LIMIT 1
    """, (ticker, trade_type, direction))
    
    last_trade = cur.fetchone()
    if last_trade:
        import pandas as pd
        trade_id, last_time_str, last_confidence = last_trade[0], last_trade[1], last_trade[2]
        try:
            last_time = datetime.fromisoformat(last_time_str)
        except Exception:
            last_time = pd.to_datetime(last_time_str)
            
        # If an active setup exists on the same calendar day:
        if last_time.date() == now.date():
            # Update the existing active setup with latest confidence and pricing metadata without creating duplicate rows
            conn.execute("""
                UPDATE ml_trade_history
                SET confidence = ?, explanation = ?, reference_price = ?, model_candle_close = ?,
                    price_source = ?, price_timestamp = ?, price_is_fresh = ?
                WHERE id = ?
            """, (float(confidence), explanation_str, ref_price, m_close, p_source, p_ts, p_fresh, trade_id))
            conn.commit()
            conn.close()

            try:
                from app.analytics.master_logger import MasterLogger
                MasterLogger.log_event(
                    "ML_HISTORY", "DUPLICATE_SUPPRESSED",
                    f"Suppressed duplicate OPEN setup for {ticker} ({trade_type} {direction}). Updated active setup #{trade_id} (confidence: {last_confidence:.1f}% -> {float(confidence):.1f}%).",
                    ticker=ticker,
                    details={"ticker": ticker, "trade_type": trade_type, "direction": direction, "trade_id": trade_id, "prev_conf": last_confidence, "new_conf": float(confidence)}
                )
            except Exception:
                pass
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
    'timestamp': 0,
    'db_path': None
}
_EVAL_CACHE_TTL = 60  # 60 seconds cache

_MARKET_DATA_CACHE = {
    'data': {},
    'timestamp': 0
}
_MARKET_DATA_TTL = 600  # 10 minutes cache

def evaluate_ml_history(force_refresh: bool = False):
    """
    Evaluates recorded ML trade history against subsequent market price action.
    Resolved trades are permanently stored in SQLite for instant retrieval.
    """
    import time as time_module
    epoch_now = time_module.time()
    current_db = get_db_path()
    if not force_refresh and _EVAL_CACHE['data'] is not None and _EVAL_CACHE.get('db_path') == current_db and (epoch_now - _EVAL_CACHE['timestamp']) < _EVAL_CACHE_TTL:
        return _EVAL_CACHE['data']

    ensure_ml_table()
    conn = sqlite3.connect(current_db, timeout=10.0)
    conn.execute("PRAGMA busy_timeout = 10000;")
    df_trades = pd.read_sql_query("SELECT * FROM ml_trade_history ORDER BY id DESC", conn)
    conn.close()
    
    if df_trades.empty:
        _EVAL_CACHE['data'] = []
        _EVAL_CACHE['timestamp'] = epoch_now
        _EVAL_CACHE['db_path'] = current_db
        return []
        
    # Pre-fetch Macro State once for all trades
    from app.analytics.macro_engine import get_macro_regime
    macro = get_macro_regime()
    
    # 1. Identify which tickers need fresh candle data (unfinalized OPEN trades only)
    is_unresolved = (df_trades['status'] == 'OPEN') & (df_trades['outcome'].isna() | (df_trades['outcome'] == '') | (df_trades['outcome'] == 'OPEN'))
    unresolved_df = df_trades[is_unresolved]
    raw_tickers = unresolved_df['ticker'].unique().tolist() if not unresolved_df.empty else []
    
    # Strictly sanitize tickers to prevent downloading test artifacts or malformed symbols
    unresolved_tickers = [
        t for t in raw_tickers 
        if isinstance(t, str) and not t.startswith(("CACHE_", "TEMP_", "DUMMY_", "MOCK_")) and "." in t
    ]
    
    market_data = {}
    live_quotes = {}
    if unresolved_tickers:
        # Expire cache if TTL reached
        if (epoch_now - _MARKET_DATA_CACHE['timestamp']) > _MARKET_DATA_TTL:
            _MARKET_DATA_CACHE['data'].clear()

        needed_tickers = [t for t in unresolved_tickers if t not in _MARKET_DATA_CACHE['data']]
        if needed_tickers:
            # Batch download 60-day 15m candles covering both Intraday and Swing time horizons
            try:
                hist = yf.download(needed_tickers, period="60d", interval="15m", progress=False, timeout=10)
                if len(needed_tickers) == 1:
                    t = needed_tickers[0]
                    if hist is not None and not hist.empty:
                        if isinstance(hist.columns, pd.MultiIndex):
                            hist.columns = [c[0] for c in hist.columns]
                        _MARKET_DATA_CACHE['data'][t] = hist
                    else:
                        _MARKET_DATA_CACHE['data'][t] = pd.DataFrame()
                else:
                    for ticker in needed_tickers:
                        if hasattr(hist, 'columns') and 'Close' in hist and ticker in hist['Close']:
                            df_tick = pd.DataFrame({
                                'High': hist['High'][ticker],
                                'Low': hist['Low'][ticker],
                                'Close': hist['Close'][ticker]
                            })
                            _MARKET_DATA_CACHE['data'][ticker] = df_tick
                        else:
                            _MARKET_DATA_CACHE['data'][ticker] = pd.DataFrame()
            except Exception as e:
                logger.warning(f"yfinance 15m batch download warning: {e}")
                for ticker in needed_tickers:
                    if ticker not in _MARKET_DATA_CACHE['data']:
                        _MARKET_DATA_CACHE['data'][ticker] = pd.DataFrame()

            # For any ticker where 15m returned empty, fallback to recent daily candles
            empty_tickers = [t for t in needed_tickers if _MARKET_DATA_CACHE['data'].get(t) is None or _MARKET_DATA_CACHE['data'][t].empty]
            if empty_tickers:
                try:
                    hist_daily = yf.download(empty_tickers, period="60d", interval="1d", progress=False, timeout=8)
                    if len(empty_tickers) == 1:
                        t = empty_tickers[0]
                        if hist_daily is not None and not hist_daily.empty:
                            if isinstance(hist_daily.columns, pd.MultiIndex):
                                hist_daily.columns = [c[0] for c in hist_daily.columns]
                            _MARKET_DATA_CACHE['data'][t] = hist_daily
                    else:
                        for ticker in empty_tickers:
                            if hasattr(hist_daily, 'columns') and 'Close' in hist_daily and ticker in hist_daily['Close']:
                                df_tick = pd.DataFrame({
                                    'High': hist_daily['High'][ticker],
                                    'Low': hist_daily['Low'][ticker],
                                    'Close': hist_daily['Close'][ticker]
                                })
                                _MARKET_DATA_CACHE['data'][ticker] = df_tick
                except Exception as e:
                    logger.warning(f"yfinance daily fallback warning: {e}")

            _MARKET_DATA_CACHE['timestamp'] = epoch_now

        market_data = _MARKET_DATA_CACHE['data']

        # Pre-fetch live quotes for all unresolved tickers for authoritative fresh LTP
        from app.data.market_provider import get_live_quote_with_meta
        for t in unresolved_tickers:
            try:
                live_quotes[t] = get_live_quote_with_meta(t)
            except Exception as e:
                logger.warning(f"Live quote fetch error for {t}: {e}")
                live_quotes[t] = None

    results = []
    finalized_updates = []
    open_updates = []

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
                "exit_price": raw_entry,
                "exit_time": entry_time_str,
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
            raw_prof = row.get('profit_pct')
            profit_pct = float(raw_prof) if pd.notna(raw_prof) and raw_prof is not None else 0.0
            raw_ideal = row.get('ideal_profit_pct')
            ideal_profit_pct = float(raw_ideal) if pd.notna(raw_ideal) and raw_ideal is not None else profit_pct
            saved_eff = row.get('effective_entry')
            if pd.notna(saved_eff) and saved_eff is not None:
                effective_entry = float(saved_eff)
            saved_drag = row.get('slippage_drag')
            slippage_drag = float(saved_drag) if pd.notna(saved_drag) and saved_drag is not None else round(ideal_profit_pct - profit_pct, 2)
            exit_price_val = row.get('exit_price')
            if exit_price_val is None or pd.isna(exit_price_val):
                exit_price_val = sl if outcome == 'SL HIT' else (tp1 if outcome == 'TARGET MET' else raw_entry)
            exit_time_val = row.get('exit_time')
            current_price = float(exit_price_val) if exit_price_val is not None else raw_entry
            risk_audit_data = None
            price_source_val = str(row.get('price_source', 'Archived')) if pd.notna(row.get('price_source')) else 'Archived'
            price_timestamp_val = str(row.get('price_timestamp', '')) if pd.notna(row.get('price_timestamp')) else ''
            price_is_fresh_val = False
        else:
            outcome = "OPEN"
            ideal_profit_pct = 0.0
            profit_pct = 0.0
            current_price = raw_entry
            exit_price_val = None
            exit_time_val = None
            price_source_val = "Model Candle Close"
            price_timestamp_val = ""
            price_is_fresh_val = False

            quote = live_quotes.get(ticker)
            if quote and quote.get("price") and float(quote["price"]) > 0:
                current_price = float(quote["price"])
                price_source_val = quote.get("source_name", "Live Market Feed")
                price_timestamp_val = quote.get("timestamp", datetime.now().strftime("%H:%M:%S IST"))
                price_is_fresh_val = bool(quote.get("is_realtime", False))

            if ticker in market_data:
                df = market_data[ticker].dropna()
                if not df.empty:
                    if hasattr(df.index, 'tz') and df.index.tz is not None:
                        idx_naive = df.index.tz_localize(None)
                    else:
                        idx_naive = df.index

                    # Detect if daily or intraday
                    is_daily = False
                    if len(idx_naive) >= 2:
                        bar_delta = (idx_naive[1] - idx_naive[0]).total_seconds()
                        is_daily = (bar_delta >= 43200)
                    elif len(idx_naive) == 1:
                        is_daily = (idx_naive[0].hour == 0 and idx_naive[0].minute == 0)

                    if is_daily:
                        df_future = df[idx_naive.normalize() >= pd.Timestamp(entry_time).normalize()]
                    else:
                        df_future = df[idx_naive >= entry_time]

                    if not df_future.empty:
                        col_map = {str(c).lower(): c for c in df_future.columns}
                        h_col = col_map.get('high', 'High')
                        l_col = col_map.get('low', 'Low')
                        c_col = col_map.get('close', 'Close')

                        for timestamp_idx, f_row in df_future.iterrows():
                            high = float(f_row[h_col]) if h_col in f_row else raw_entry
                            low = float(f_row[l_col]) if l_col in f_row else raw_entry
                            close = float(f_row[c_col]) if c_col in f_row else raw_entry
                            t_time = timestamp_idx.replace(tzinfo=None) if hasattr(timestamp_idx, 'replace') else pd.to_datetime(timestamp_idx).tz_localize(None)

                            if direction == "BULLISH":
                                if low <= sl:
                                    outcome = "SL HIT"
                                    exit_price_val = sl
                                    exit_time_val = str(t_time)
                                    ideal_profit_pct = ((sl - raw_entry) / raw_entry) * 100
                                    profit_pct = ((sl - effective_entry) / effective_entry) * 100
                                    break
                                elif high >= tp1:
                                    outcome = "TARGET MET"
                                    exit_price_val = tp1
                                    exit_time_val = str(t_time)
                                    ideal_profit_pct = ((tp1 - raw_entry) / raw_entry) * 100
                                    profit_pct = ((tp1 - effective_entry) / effective_entry) * 100
                                    break
                            else:  # BEARISH
                                if high >= sl:
                                    outcome = "SL HIT"
                                    exit_price_val = sl
                                    exit_time_val = str(t_time)
                                    ideal_profit_pct = ((raw_entry - sl) / raw_entry) * 100
                                    profit_pct = ((effective_entry - sl) / effective_entry) * 100
                                    break
                                elif low <= tp1:
                                    outcome = "TARGET MET"
                                    exit_price_val = tp1
                                    exit_time_val = str(t_time)
                                    ideal_profit_pct = ((raw_entry - tp1) / raw_entry) * 100
                                    profit_pct = ((effective_entry - tp1) / effective_entry) * 100
                                    break

                            # Intraday 3:15 PM Square-off Rule
                            if trade_type == 'INTRADAY':
                                if (t_time.date() == entry_time.date() and (t_time.hour > 15 or (t_time.hour == 15 and t_time.minute >= 15))) or (t_time.date() > entry_time.date()):
                                    outcome = "SQUARED OFF (3:15 PM)"
                                    exit_price_val = close
                                    exit_time_val = str(t_time)
                                    if direction == "BULLISH":
                                        ideal_profit_pct = ((close - raw_entry) / raw_entry) * 100
                                        profit_pct = ((close - effective_entry) / effective_entry) * 100
                                    else:
                                        ideal_profit_pct = ((raw_entry - close) / raw_entry) * 100
                                        profit_pct = ((effective_entry - close) / effective_entry) * 100
                                    break

                        # If still OPEN after candle traversal, use fresh LTP for mark-to-market and live breach check
                        if outcome == "OPEN":
                            if not (quote and quote.get("price") and float(quote["price"]) > 0):
                                current_price = float(df_future.iloc[-1][c_col]) if c_col in df_future.columns else raw_entry
                                price_source_val = "Candle Close"
                                price_timestamp_val = str(df_future.index[-1])
                                price_is_fresh_val = False

                            # Real-time SL/TP breach check on live LTP
                            if direction == "BULLISH":
                                if current_price <= sl:
                                    outcome = "SL HIT"
                                    exit_price_val = sl
                                    exit_time_val = datetime.now().isoformat()
                                    ideal_profit_pct = ((sl - raw_entry) / raw_entry) * 100
                                    profit_pct = ((sl - effective_entry) / effective_entry) * 100
                                elif current_price >= tp1:
                                    outcome = "TARGET MET"
                                    exit_price_val = tp1
                                    exit_time_val = datetime.now().isoformat()
                                    ideal_profit_pct = ((tp1 - raw_entry) / raw_entry) * 100
                                    profit_pct = ((tp1 - effective_entry) / effective_entry) * 100
                                else:
                                    ideal_profit_pct = ((current_price - raw_entry) / raw_entry) * 100
                                    profit_pct = ((current_price - effective_entry) / effective_entry) * 100
                            else:  # BEARISH
                                if current_price >= sl:
                                    outcome = "SL HIT"
                                    exit_price_val = sl
                                    exit_time_val = datetime.now().isoformat()
                                    ideal_profit_pct = ((raw_entry - sl) / raw_entry) * 100
                                    profit_pct = ((effective_entry - sl) / effective_entry) * 100
                                elif current_price <= tp1:
                                    outcome = "TARGET MET"
                                    exit_price_val = tp1
                                    exit_time_val = datetime.now().isoformat()
                                    ideal_profit_pct = ((raw_entry - tp1) / raw_entry) * 100
                                    profit_pct = ((effective_entry - tp1) / effective_entry) * 100
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
                        exit_price_val = current_price
                        exit_time_val = now_dt.isoformat()

            # ── SWING 5-TRADING-DAY HORIZON EXPIRATION ───────────────────
            # Swing trades that remain unresolved after 5 trading days are closed
            if trade_type == 'SWING' and outcome == 'OPEN':
                try:
                    trading_days_elapsed = int(np.busday_count(entry_time.date(), now_dt.date()))
                except Exception:
                    trading_days_elapsed = max(0, (now_dt.date() - entry_time.date()).days * 5 // 7)

                if trading_days_elapsed >= 5:
                    outcome = "SWING_HORIZON_REACHED"
                    exit_price_val = current_price
                    exit_time_val = now_dt.isoformat()
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
                open_updates.append((
                    round(current_price, 2),
                    price_source_val,
                    price_timestamp_val,
                    1 if price_is_fresh_val else 0,
                    trade_id
                ))
            else:
                # Collect finalized trade for permanent DB persistence
                finalized_updates.append((
                    outcome,
                    round(profit_pct, 2),
                    round(effective_entry, 2),
                    slippage_drag,
                    round(ideal_profit_pct, 2),
                    'CLOSED',
                    round(exit_price_val, 2) if exit_price_val is not None else None,
                    str(exit_time_val) if exit_time_val is not None else None,
                    round(current_price, 2) if current_price is not None else None,
                    trade_id
                ))

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
            "exit_price": round(float(exit_price_val), 2) if exit_price_val is not None else None,
            "exit_time": str(exit_time_val) if exit_time_val is not None else None,
            "price_source": price_source_val,
            "price_timestamp": price_timestamp_val,
            "price_is_fresh": price_is_fresh_val,
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
                SET outcome = ?, profit_pct = ?, effective_entry = ?, slippage_drag = ?, ideal_profit_pct = ?,
                    status = ?, exit_price = ?, exit_time = ?, current_price = ?
                WHERE id = ?
            """, finalized_updates)
            batch_conn.commit()
            batch_conn.close()
        except Exception as e:
            logger.warning(f"Batch outcome persistence error: {e}")

    # 4. Persist updated live mark-to-market prices for active open trades
    if open_updates:
        try:
            batch_conn = sqlite3.connect(get_db_path(), timeout=15.0)
            batch_conn.executemany("""
                UPDATE ml_trade_history 
                SET current_price = ?, price_source = ?, price_timestamp = ?, price_is_fresh = ?
                WHERE id = ?
            """, open_updates)
            batch_conn.commit()
            batch_conn.close()
        except Exception as e:
            logger.warning(f"Batch open price persistence error: {e}")

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
    _EVAL_CACHE['db_path'] = current_db
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


