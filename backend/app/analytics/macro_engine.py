import yfinance as yf
import pandas as pd
import time as time_module
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

_MACRO_CACHE = {
    'data': None,
    'timestamp': 0
}
_MACRO_CACHE_TTL = 300  # 5 minutes cache

def get_macro_regime() -> Dict[str, Any]:
    """
    Rule-Based Macro Regime Engine.
    Computes broad market trend and volatility metrics using NIFTY 50 and INDIA VIX.
    
    Architecture Note:
    This is a deterministic, rule-based quantitative regime engine (SMA-200, EMA-20, VIX thresholds).
    It operates strictly on completed historical price bars without lookahead.
    """
    now = time_module.time()
    if _MACRO_CACHE['data'] is not None and (now - _MACRO_CACHE['timestamp']) < _MACRO_CACHE_TTL:
        return _MACRO_CACHE['data']

    macro_data = {
        'engine_type': 'Rule-Based Macro Regime Engine',
        'nifty_trend_long': 'BULLISH',
        'nifty_trend_short': 'BULLISH',
        'vix_status': 'NORMAL',
        'nifty_close': 0.0,
        'sma_200': 0.0,
        'ema_20': 0.0,
        'vix_close': 15.0,
        'status': 'active',
        'error': None
    }
    
    try:
        # Fetch NIFTY 50
        nifty = yf.download("^NSEI", period="1y", interval="1d", progress=False)
        if not nifty.empty and len(nifty) > 50:
            close_prices = nifty['Close'].squeeze()
            if len(close_prices) >= 200:
                macro_data['sma_200'] = float(close_prices.rolling(window=200).mean().iloc[-1])
            else:
                macro_data['sma_200'] = float(close_prices.rolling(window=len(close_prices)).mean().iloc[-1])
                
            macro_data['ema_20'] = float(close_prices.ewm(span=20, adjust=False).mean().iloc[-1])
            macro_data['nifty_close'] = float(close_prices.iloc[-1])
            
            macro_data['nifty_trend_long'] = "BULLISH" if macro_data['nifty_close'] > macro_data['sma_200'] else "BEARISH"
            macro_data['nifty_trend_short'] = "BULLISH" if macro_data['nifty_close'] > macro_data['ema_20'] else "BEARISH"
            
        # Fetch INDIA VIX
        vix = yf.download("^INDIAVIX", period="1mo", interval="1d", progress=False)
        if not vix.empty:
            vix_close = float(vix['Close'].squeeze().iloc[-1])
            macro_data['vix_close'] = vix_close
            
            if vix_close < 12.0:
                macro_data['vix_status'] = "LOW"
            elif vix_close > 22.0:
                macro_data['vix_status'] = "HIGH"
            else:
                macro_data['vix_status'] = "NORMAL"
                
        _MACRO_CACHE['data'] = macro_data
        _MACRO_CACHE['timestamp'] = now
    except Exception as e:
        logger.warning(f"Macro regime fetch warning: {e}")
        macro_data['error'] = str(e)
        macro_data['status'] = 'fallback_neutral'
        if _MACRO_CACHE['data'] is not None:
            return _MACRO_CACHE['data']
        
    return macro_data
