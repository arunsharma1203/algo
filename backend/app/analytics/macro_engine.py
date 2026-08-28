import yfinance as yf
import pandas as pd

def get_macro_regime():
    """
    Fetches NIFTY 50 and INDIA VIX to determine the broad market environment.
    """
    macro_data = {
        'nifty_trend_long': 'BULLISH',
        'nifty_trend_short': 'BULLISH',
        'vix_status': 'NORMAL',
        'nifty_close': 0.0,
        'sma_200': 0.0,
        'ema_20': 0.0,
        'vix_close': 15.0,
        'error': None
    }
    
    try:
        # Fetch NIFTY 50
        nifty = yf.download("^NSEI", period="1y", interval="1d", progress=False)
        if not nifty.empty and len(nifty) > 200:
            close_prices = nifty['Close'].squeeze()
            macro_data['sma_200'] = float(close_prices.rolling(window=200).mean().iloc[-1])
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
                
    except Exception as e:
        macro_data['error'] = str(e)
        
    return macro_data
