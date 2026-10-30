import pandas as pd
import time as time_module
import logging
import sqlite3
from typing import Dict, Any, Tuple, Optional
from datetime import datetime

from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)

_MACRO_CACHE = {
    'data': None,
    'timestamp': 0
}
_MACRO_CACHE_TTL = 180  # 3 minutes cache for fresh intraday tracking

def get_macro_regime(force_refresh: bool = False) -> Dict[str, Any]:
    """
    Dual-Timeframe Rule-Based Macro Regime Engine.
    Evaluates:
      1. Intraday Session Regime (Today's Change vs Prev Close & Open) for INTRADAY trading.
      2. Multi-Day Structural Regime (20-day EMA, 200-day SMA, VIX) for SWING trading.
    
    Architecture Note:
    Deterministic, rule-based quantitative regime engine.
    Operates strictly on real market data without lookahead.
    """
    now = time_module.time()
    if not force_refresh and _MACRO_CACHE['data'] is not None and (now - _MACRO_CACHE['timestamp']) < _MACRO_CACHE_TTL:
        return _MACRO_CACHE['data']

    macro_data = {
        'engine_type': 'Dual-Timeframe Macro Regime Engine',
        'nifty_trend_long': 'BULLISH',
        'nifty_trend_short': 'BULLISH',
        'nifty_trend_intraday': 'BULLISH',
        'nifty_trend_daily_20ema': 'BULLISH',
        'regime_verdict': 'NEUTRAL_CHOP',
        'intraday_regime_verdict': 'INTRADAY_NEUTRAL_CHOP',
        'vix_status': 'NORMAL',
        'nifty_close': 0.0,
        'nifty_open': 0.0,
        'nifty_prev_close': 0.0,
        'intraday_change_pct': 0.0,
        'intraday_open_change_pct': 0.0,
        'sma_200': 0.0,
        'ema_20': 0.0,
        'vix_close': 15.0,
        'distance_ema20_pct': 0.0,
        'distance_sma200_pct': 0.0,
        'status': 'active',
        'error': None
    }
    
    try:
        # 1. Fetch NIFTY 50 Daily OHLCV or compute Full-Universe Market Breadth from canonical SQLite storage
        nifty = None
        universe_breadth = None
        try:
            conn = sqlite3.connect(get_db_path(), timeout=5.0)
            local_df = pd.read_sql_query(
                "SELECT date, open, high, low, close, volume FROM ohlcv "
                "WHERE ticker IN ('^NSEI', 'NIFTY 50.NS', 'NIFTY.NS', 'NIFTY50.NS', 'NSE_INDEX|Nifty 50') AND timeframe = '1d' "
                "ORDER BY date ASC",
                conn
            )
            if not local_df.empty and len(local_df) >= 20:
                nifty = local_df
            else:
                # Full-Universe Market Breadth across all 510 equities in SQLite
                b_row = conn.execute("""
                    SELECT 
                        COUNT(*),
                        SUM(CASE WHEN close > open THEN 1 ELSE 0 END),
                        SUM(CASE WHEN close < open THEN 1 ELSE 0 END),
                        AVG((close - open) / open * 100.0)
                    FROM (
                        SELECT ticker, open, close
                        FROM ohlcv
                        WHERE timeframe = '1d' AND date = (SELECT MAX(date) FROM ohlcv WHERE timeframe = '1d')
                    )
                """).fetchone()

                sma_b_row = conn.execute("""
                    WITH ranked_candles AS (
                        SELECT ticker, close,
                               ROW_NUMBER() OVER(PARTITION BY ticker ORDER BY date DESC) as rn
                        FROM ohlcv
                        WHERE timeframe = '1d'
                    ),
                    recent_20 AS (
                        SELECT ticker, close, rn
                        FROM ranked_candles
                        WHERE rn <= 20
                    ),
                    ticker_stats AS (
                        SELECT ticker,
                               MAX(CASE WHEN rn = 1 THEN close END) as latest_close,
                               AVG(close) as sma_20
                        FROM recent_20
                        GROUP BY ticker
                        HAVING COUNT(*) >= 15
                    )
                    SELECT 
                        COUNT(*),
                        SUM(CASE WHEN latest_close > sma_20 THEN 1 ELSE 0 END)
                    FROM ticker_stats
                """).fetchone()

                if b_row and b_row[0] and b_row[0] > 0:
                    tot = b_row[0]
                    adv = b_row[1] or 0
                    dec = b_row[2] or 0
                    avg_ret = round(float(b_row[3] or 0.0), 2)
                    adv_pct = round((adv / tot) * 100.0, 1)

                    tot_sma = (sma_b_row[0] if sma_b_row and sma_b_row[0] else 1)
                    above_sma = (sma_b_row[1] if sma_b_row and sma_b_row[1] else 0)
                    sma_pct = round((above_sma / tot_sma) * 100.0, 1)

                    universe_breadth = {
                        "total_universe": tot,
                        "advancing": adv,
                        "declining": dec,
                        "advance_pct": adv_pct,
                        "avg_session_return": avg_ret,
                        "breadth_above_20d_pct": sma_pct
                    }
            conn.close()
        except Exception as le:
            logger.warning(f"Local SQLite macro fetch error: {le}")

        if nifty is not None and not nifty.empty and len(nifty) >= 20:
            if 'Close' in nifty.columns:
                close_series = nifty['Close'].squeeze()
                open_series = nifty['Open'].squeeze() if 'Open' in nifty.columns else None
            elif 'close' in nifty.columns:
                close_series = nifty['close'].squeeze()
                open_series = nifty['open'].squeeze() if 'open' in nifty.columns else None
            else:
                close_series = None
                open_series = None

            if close_series is not None and len(close_series) >= 20:
                close_series = pd.Series(close_series).dropna()
                if len(close_series) >= 200:
                    macro_data['sma_200'] = float(close_series.rolling(window=200).mean().iloc[-1])
                else:
                    macro_data['sma_200'] = float(close_series.rolling(window=len(close_series)).mean().iloc[-1])
                    
                macro_data['ema_20'] = float(close_series.ewm(span=20, adjust=False).mean().iloc[-1])
                macro_data['nifty_close'] = float(close_series.iloc[-1])
                
                # Structural Multi-Day Trend (Swing)
                macro_data['nifty_trend_long'] = "BULLISH" if macro_data['nifty_close'] >= macro_data['sma_200'] else "BEARISH"
                macro_data['nifty_trend_daily_20ema'] = "BULLISH" if macro_data['nifty_close'] >= macro_data['ema_20'] else "BEARISH"

                if macro_data['ema_20'] > 0:
                    macro_data['distance_ema20_pct'] = round(((macro_data['nifty_close'] - macro_data['ema_20']) / macro_data['ema_20']) * 100.0, 2)
                if macro_data['sma_200'] > 0:
                    macro_data['distance_sma200_pct'] = round(((macro_data['nifty_close'] - macro_data['sma_200']) / macro_data['sma_200']) * 100.0, 2)

                # ── INTRADAY SESSION PERFORMANCE EVALUATION ───────────────
                if len(close_series) >= 2:
                    prev_close = float(close_series.iloc[-2])
                    today_close = float(close_series.iloc[-1])
                    today_open = float(open_series.iloc[-1]) if (open_series is not None and len(open_series) > 0) else today_close

                    macro_data['nifty_prev_close'] = prev_close
                    macro_data['nifty_open'] = today_open

                    change_pct = round(((today_close - prev_close) / prev_close) * 100.0, 2)
                    open_change_pct = round(((today_close - today_open) / today_open) * 100.0, 2) if today_open > 0 else 0.0

                    macro_data['intraday_change_pct'] = change_pct
                    macro_data['intraday_open_change_pct'] = open_change_pct

                    if (change_pct >= 0.20 and open_change_pct >= 0.0) or (open_change_pct >= 0.30 and change_pct >= 0.0):
                        macro_data['intraday_regime_verdict'] = "INTRADAY_BULL_MOMENTUM"
                        macro_data['nifty_trend_intraday'] = "BULLISH"
                    elif (change_pct <= -0.20 and open_change_pct <= 0.0) or (open_change_pct <= -0.30 and change_pct <= 0.0):
                        macro_data['intraday_regime_verdict'] = "INTRADAY_BEAR_PRESSURE"
                        macro_data['nifty_trend_intraday'] = "BEARISH"
                    else:
                        macro_data['intraday_regime_verdict'] = "INTRADAY_NEUTRAL_CHOP"
                        macro_data['nifty_trend_intraday'] = "NEUTRAL"

                    macro_data['nifty_trend_short'] = (
                        macro_data['nifty_trend_intraday']
                        if macro_data['nifty_trend_intraday'] in ("BULLISH", "BEARISH")
                        else ("BULLISH" if macro_data['nifty_close'] >= macro_data.get('nifty_open', macro_data['nifty_close']) else "BEARISH")
                    )

        elif universe_breadth is not None:
            # Rigorous Full-Universe Breadth Integration across all 510 stocks
            macro_data['universe_breadth'] = universe_breadth
            sma_pct = universe_breadth["breadth_above_20d_pct"]
            adv_pct = universe_breadth["advance_pct"]
            avg_ret = universe_breadth["avg_session_return"]

            # Multi-Day Structural Regime derived from 510-stock SMA20 breadth
            if sma_pct >= 55.0:
                macro_data['nifty_trend_long'] = "BULLISH"
                macro_data['nifty_trend_daily_20ema'] = "BULLISH"
            elif sma_pct <= 42.0:
                macro_data['nifty_trend_long'] = "BEARISH"
                macro_data['nifty_trend_daily_20ema'] = "BEARISH"
            else:
                macro_data['nifty_trend_long'] = "NEUTRAL"
                macro_data['nifty_trend_daily_20ema'] = "NEUTRAL"

            macro_data['distance_ema20_pct'] = round(sma_pct - 50.0, 2)
            macro_data['intraday_change_pct'] = avg_ret
            macro_data['intraday_open_change_pct'] = avg_ret

            # Intraday Session Regime derived from 510-stock Advance/Decline ratio
            if adv_pct >= 60.0 and avg_ret >= 0.15:
                macro_data['intraday_regime_verdict'] = "INTRADAY_BULL_MOMENTUM"
                macro_data['nifty_trend_intraday'] = "BULLISH"
            elif adv_pct <= 40.0 and avg_ret <= -0.15:
                macro_data['intraday_regime_verdict'] = "INTRADAY_BEAR_PRESSURE"
                macro_data['nifty_trend_intraday'] = "BEARISH"
            else:
                macro_data['intraday_regime_verdict'] = "INTRADAY_NEUTRAL_CHOP"
                macro_data['nifty_trend_intraday'] = "NEUTRAL"

            macro_data['nifty_trend_short'] = macro_data['nifty_trend_intraday']
            
        # 2. Fetch INDIA VIX from canonical SQLite storage
        try:
            conn = sqlite3.connect(get_db_path(), timeout=5.0)
            vix_df = pd.read_sql_query(
                "SELECT date, close FROM ohlcv "
                "WHERE ticker IN ('^INDIAVIX', 'INDIAVIX.NS', 'INDIA VIX.NS', 'NSE_INDEX|India VIX') AND timeframe = '1d' "
                "ORDER BY date DESC LIMIT 1",
                conn
            )
            conn.close()
            if not vix_df.empty and vix_df['close'].iloc[0] is not None:
                vix_close = float(vix_df['close'].iloc[0])
                macro_data['vix_close'] = vix_close
                if vix_close < 12.0:
                    macro_data['vix_status'] = "LOW"
                elif vix_close > 22.0:
                    macro_data['vix_status'] = "HIGH"
                else:
                    macro_data['vix_status'] = "NORMAL"
        except Exception:
            pass

        # 2b. Live Upstox Real-Time Quotes Refresh for NIFTY & VIX (Single HTTP call)
        try:
            from app.data.upstox_provider import upstox_request, get_upstox_token, UPSTOX_BASE_URL, get_upstox_limiter
            limiter = get_upstox_limiter()
            if not limiter.is_circuit_open():
                tok = get_upstox_token()
                if tok and len(tok) > 10:
                    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
                    res = upstox_request("GET", f"{UPSTOX_BASE_URL}/market-quote/quotes", headers=headers, params={"instrument_key": "NSE_INDEX|Nifty 50,NSE_INDEX|India VIX"}, timeout=4.0)
                    if res.status_code == 200:
                        d = res.json().get("data", {})
                        nifty_q = d.get("NSE_INDEX:Nifty 50", {})
                        vix_q = d.get("NSE_INDEX:India VIX", {})
                        
                        live_nifty = nifty_q.get("last_price")
                        nifty_ohlc = nifty_q.get("ohlc", {}) or {}
                        nifty_net_chg = nifty_q.get("net_change")
                        
                        live_vix = vix_q.get("last_price")

                        if live_nifty and float(live_nifty) > 0:
                            n_close = float(live_nifty)
                            macro_data['nifty_close'] = n_close
                            
                            n_open = float(nifty_ohlc.get("open", 0.0))
                            if n_open > 0:
                                macro_data['nifty_open'] = n_open
                            else:
                                n_open = macro_data.get('nifty_open', n_close)

                            n_prev_close = (n_close - float(nifty_net_chg)) if (nifty_net_chg is not None) else float(nifty_ohlc.get("close", 0.0))
                            if n_prev_close > 0:
                                macro_data['nifty_prev_close'] = n_prev_close
                            else:
                                n_prev_close = macro_data.get('nifty_prev_close', n_close)

                            # Recalculate true real-time intraday metrics from live Upstox ticks
                            c_pct = round(((n_close - n_prev_close) / n_prev_close) * 100.0, 2) if n_prev_close > 0 else 0.0
                            o_chg_pct = round(((n_close - n_open) / n_open) * 100.0, 2) if n_open > 0 else 0.0

                            macro_data['intraday_change_pct'] = c_pct
                            macro_data['intraday_open_change_pct'] = o_chg_pct

                            # Institutional Intraday Regime Classification:
                            # Directional momentum requires congruence between daily return (vs prev close) and session drift (vs open).
                            # A negative daily return (c_pct < 0) cannot be Bull Momentum, and a positive daily return cannot be Bear Pressure.
                            # Conflicting drift (gap-down bounce or gap-up fade) is classified as NEUTRAL_CHOP, permitting both long and short setups.
                            if (c_pct >= 0.08 and o_chg_pct >= 0.0) or (o_chg_pct >= 0.25 and c_pct >= 0.0):
                                macro_data['intraday_regime_verdict'] = "INTRADAY_BULL_MOMENTUM"
                                macro_data['nifty_trend_intraday'] = "BULLISH"
                            elif (c_pct <= -0.08 and o_chg_pct <= 0.0) or (o_chg_pct <= -0.25 and c_pct <= 0.0):
                                macro_data['intraday_regime_verdict'] = "INTRADAY_BEAR_PRESSURE"
                                macro_data['nifty_trend_intraday'] = "BEARISH"
                            else:
                                macro_data['intraday_regime_verdict'] = "INTRADAY_NEUTRAL_CHOP"
                                macro_data['nifty_trend_intraday'] = "NEUTRAL"

                            # Strictly decouple intraday session trend from multi-week daily 20 EMA
                            macro_data['nifty_trend_short'] = (
                                macro_data['nifty_trend_intraday']
                                if macro_data['nifty_trend_intraday'] in ("BULLISH", "BEARISH")
                                else ("BULLISH" if n_close >= n_open else "BEARISH")
                            )

                            if macro_data.get('ema_20', 0) > 0:
                                macro_data['nifty_trend_daily_20ema'] = "BULLISH" if n_close >= macro_data['ema_20'] else "BEARISH"

                        if live_vix and float(live_vix) > 0:
                            v_close = float(live_vix)
                            macro_data['vix_close'] = v_close
                            macro_data['vix_status'] = "LOW" if v_close < 12.0 else ("HIGH" if v_close > 22.0 else "NORMAL")
        except Exception as e:
            logger.debug(f"[MacroEngine] Upstox live index refresh note: {e}")

        # 3. Derive Multi-Day Structural Regime Verdict (For SWING)
        # BEAR_DISTRIBUTION: Nifty is below 20 EMA on daily chart
        # BULL_EXPANSION: Nifty is above 20 EMA and above 200 SMA on daily chart
        # NEUTRAL_CHOP: Transitional zone or conflicting trend
        if macro_data['nifty_trend_daily_20ema'] == "BEARISH":
            macro_data['regime_verdict'] = "BEAR_DISTRIBUTION"
        elif macro_data['nifty_trend_daily_20ema'] == "BULLISH" and macro_data['nifty_trend_long'] == "BULLISH":
            macro_data['regime_verdict'] = "BULL_EXPANSION"
        else:
            macro_data['regime_verdict'] = "NEUTRAL_CHOP"

        _MACRO_CACHE['data'] = macro_data
        _MACRO_CACHE['timestamp'] = now
    except Exception as e:
        logger.warning(f"Macro regime fetch warning: {e}")
        macro_data['error'] = str(e)
        macro_data['status'] = 'fallback_neutral'
        if _MACRO_CACHE['data'] is not None:
            return _MACRO_CACHE['data']
        
    return macro_data


def is_direction_permitted(
    direction: str, 
    trade_type: str = "INTRADAY", 
    macro_state: Optional[Dict[str, Any]] = None
) -> Tuple[bool, str]:
    """
    Evaluates whether a trade direction is permissible under the prevailing macro market regime.
    
    Rules:
    - For INTRADAY:
        - In INTRADAY_BULL_MOMENTUM: Permits LONG setups, suppresses counter-trend SHORT setups.
        - In INTRADAY_BEAR_PRESSURE: Permits SHORT setups, suppresses counter-trend LONG setups.
        - In INTRADAY_NEUTRAL_CHOP: Permits BOTH directions.
    - For SWING:
        - In BEAR_DISTRIBUTION: Suppresses multi-day LONG setups (preserves cash equity capital).
        - In BULL_EXPANSION: Suppresses counter-trend SHORT setups.
        - In NEUTRAL_CHOP: Permits standard breakout setups.
    
    Returns:
        (is_permitted: bool, reason: str)
    """
    if macro_state is None:
        macro_state = get_macro_regime()

    dir_norm = str(direction).upper().strip()
    is_bullish = dir_norm in ("BUY", "BULLISH", "CALL", "LONG")
    is_bearish = dir_norm in ("SELL", "BEARISH", "PUT", "SHORT")
    tt = str(trade_type or "INTRADAY").upper().strip()

    if tt == "INTRADAY":
        intra_verdict = macro_state.get('intraday_regime_verdict')
        if not intra_verdict:
            # Fallback if caller provided a simplified macro_state without intraday fields
            reg = macro_state.get('regime_verdict', 'NEUTRAL_CHOP')
            if reg == "BULL_EXPANSION":
                intra_verdict = "INTRADAY_BULL_MOMENTUM"
            elif reg == "BEAR_DISTRIBUTION":
                intra_verdict = "INTRADAY_BEAR_PRESSURE"
            else:
                intra_verdict = "INTRADAY_NEUTRAL_CHOP"

        chg_pct = macro_state.get('intraday_change_pct', 0.0)

        if intra_verdict == "INTRADAY_BULL_MOMENTUM" and is_bearish:
            return False, f"NIFTY 50 in INTRADAY_BULL_MOMENTUM ({chg_pct:+.2f}% today). Counter-trend short setups suppressed."
        elif intra_verdict == "INTRADAY_BEAR_PRESSURE" and is_bullish:
            return False, f"NIFTY 50 in INTRADAY_BEAR_PRESSURE ({chg_pct:+.2f}% today). Counter-trend long setups suppressed."

        return True, f"Intraday direction {dir_norm} permitted under {intra_verdict} ({chg_pct:+.2f}% today)."

    else:
        # Multi-day Swing Horizon
        verdict = macro_state.get('regime_verdict', 'NEUTRAL_CHOP')
        dist_ema20 = macro_state.get('distance_ema20_pct', 0.0)

        if verdict == "BEAR_DISTRIBUTION" and is_bullish:
            return False, f"NIFTY 50 in BEAR_DISTRIBUTION ({dist_ema20:+.2f}% vs daily 20 EMA). Counter-trend swing longs suppressed."

        if verdict == "BULL_EXPANSION" and is_bearish:
            return False, f"NIFTY 50 in BULL_EXPANSION ({dist_ema20:+.2f}% vs daily 20 EMA). Counter-trend swing shorts suppressed."

        return True, f"Swing direction {dir_norm} permitted under {verdict} macro regime."
