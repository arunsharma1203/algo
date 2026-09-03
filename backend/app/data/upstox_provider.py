import logging
import sqlite3
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

UPSTOX_BASE_URL = "https://api.upstox.com/v2"

# Built-in High-Speed Instrument Key Map for Nifty 100 / Key Universe
# Format: Ticker Symbol -> Instrument Key (or dynamic lookup fallback)
NSE_INSTRUMENT_MAP = {
    "RELIANCE": "NSE_EQ|INE002A01018",
    "TCS": "NSE_EQ|INE467B01029",
    "HDFCBANK": "NSE_EQ|INE040A01034",
    "ICICIBANK": "NSE_EQ|INE090A01021",
    "INFY": "NSE_EQ|INE009A01021",
    "SBIN": "NSE_EQ|INE062A01020",
    "BHARTIARTL": "NSE_EQ|INE397D01024",
    "ITC": "NSE_EQ|INE154A01025",
    "LT": "NSE_EQ|INE018A01030",
    "BAJFINANCE": "NSE_EQ|INE296A01024",
    "HCLTECH": "NSE_EQ|INE860A01027",
    "MARUTI": "NSE_EQ|INE585B01010",
    "SUNPHARMA": "NSE_EQ|INE044A01036",
    "TATAMOTORS": "NSE_EQ|INE155A01022",
    "KOTAKBANK": "NSE_EQ|INE237A01028",
    "ONGC": "NSE_EQ|INE213A01029",
    "NTPC": "NSE_EQ|INE733E01010",
    "AXISBANK": "NSE_EQ|INE238A01034",
    "WIPRO": "NSE_EQ|INE075A01022",
    "M&M": "NSE_EQ|INE101A01026",
    "ULTRACEMCO": "NSE_EQ|INE481G01011",
    "POWERGRID": "NSE_EQ|INE752E01010",
    "TITAN": "NSE_EQ|INE280A01028",
    "ASIANPAINT": "NSE_EQ|INE021A01026",
    "BAJAJFINSV": "NSE_EQ|INE918I01018",
    "NESTLEIND": "NSE_EQ|INE239A01016",
    "JSWSTEEL": "NSE_EQ|INE019A01038",
    "TATASTEEL": "NSE_EQ|INE081A01020",
    "ADANIENT": "NSE_EQ|INE423A01024",
    "ADANIPORTS": "NSE_EQ|INE742F01042",
    "GRASIM": "NSE_EQ|INE047A01021",
    "TECHM": "NSE_EQ|INE669C01036",
    "HINDALCO": "NSE_EQ|INE038A01020",
    "DIVISLAB": "NSE_EQ|INE361B01024",
    "SBILIFE": "NSE_EQ|INE123W01016",
    "LTIM": "NSE_EQ|INE214T01019",
    "BAJAJ-AUTO": "NSE_EQ|INE917I01010",
    "EICHERMOT": "NSE_EQ|INE066A01013",
    "INDUSINDBK": "NSE_EQ|INE095A01012",
    "DRREDDY": "NSE_EQ|INE089A01023",
    "CIPLA": "NSE_EQ|INE059A01026",
    "APOLLOHOSP": "NSE_EQ|INE437A01024",
    "TATACOMM": "NSE_EQ|INE151A01013",
    "HDFCLIFE": "NSE_EQ|INE795G01014",
    "BRITANNIA": "NSE_EQ|INE216A01030",
    "COALINDIA": "NSE_EQ|INE522F01014",
    "HEROMOTOCO": "NSE_EQ|INE158A01026",
    "TATACONSUM": "NSE_EQ|INE192A01025",
    "BPCL": "NSE_EQ|INE029A01011",
    "UPL": "NSE_EQ|INE628A01036",
    "ZOMATO": "NSE_EQ|INE758T01015",
    "JIOFIN": "NSE_EQ|INE758E01017",
    "TRENT": "NSE_EQ|INE849A01020",
    "HAL": "NSE_EQ|INE066F01012",
    "BEL": "NSE_EQ|INE263A01024",
    "BHEL": "NSE_EQ|INE257A01026",
    "PFC": "NSE_EQ|INE134E01011",
    "RECLTD": "NSE_EQ|INE020B01018",
    "IRFC": "NSE_EQ|INE053F01010",
    "RVNL": "NSE_EQ|INE415G01027",
    "MAZDOCK": "NSE_EQ|INE249Z01012",
    "IREDA": "NSE_EQ|INE202E01016",
    "NHPC": "NSE_EQ|INE848E01016",
    "SJVN": "NSE_EQ|INE002L01015",
    "SUZLON": "NSE_EQ|INE040H01021",
    "PAYTM": "NSE_EQ|INE982J01020",
    "NYKAA": "NSE_EQ|INE388Y01029",
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service"
}

def get_upstox_config() -> Dict[str, str]:
    """Reads Upstox credentials from database (Market Read-Only & Algo Read-Trade)."""
    from app.data.historical_data_layer import get_db_path
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM app_settings WHERE key LIKE 'upstox_%'")
    rows = cur.fetchall()
    conn.close()
    
    cfg = {
        "api_key": "",
        "api_secret": "",
        "access_token": "",      # Read-only Market Data & Analytics Token
        "market_token": "",      # Alias for analytics token
        "algo_token": "",        # Read + Trade Algo Execution Token
        "redirect_uri": "http://localhost:8000/api/settings/upstox/callback"
    }
    for k, v in rows:
        field = k.replace("upstox_", "")
        if field in cfg:
            cfg[field] = v or ""
            
    # Normalize aliases
    if cfg["market_token"] and not cfg["access_token"]:
        cfg["access_token"] = cfg["market_token"]
    elif cfg["access_token"] and not cfg["market_token"]:
        cfg["market_token"] = cfg["access_token"]
        
    return cfg

def get_instrument_key(symbol: str) -> str:
    """Translates generic symbol (e.g. RELIANCE.NS, RELIANCE, NIFTY) to Upstox Instrument Key."""
    clean = symbol.replace('.NS', '').replace('.BO', '').strip().upper()
    if clean in NSE_INSTRUMENT_MAP:
        return NSE_INSTRUMENT_MAP[clean]
    # Fallback to direct symbol format if not in fast dictionary
    return f"NSE_EQ|{clean}"

def test_upstox_connection(token: Optional[str] = None, token_type: str = "market") -> Dict[str, Any]:
    """Tests connectivity to Upstox API v2 for Market Data or Algo Trading Token."""
    if not token:
        cfg = get_upstox_config()
        token = cfg.get("algo_token") if token_type == "algo" else cfg.get("access_token")
        
    if not token or not token.strip():
        token_label = "Algo Trading (Read+Trade)" if token_type == "algo" else "Market Data (Read-Only)"
        return {"status": "error", "message": f"No {token_label} Token provided. Please enter your token."}
        
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token.strip()}"
    }
    
    try:
        url = f"{UPSTOX_BASE_URL}/user/profile"
        res = requests.get(url, headers=headers, timeout=5)
        
        if res.status_code == 200:
            data = res.json()
            user_data = data.get("data", {})
            user_name = user_data.get("user_name", "Upstox Trader")
            user_id = user_data.get("user_id", "")
            token_role = "Algo Trading & Execution (Read+Trade)" if token_type == "algo" else "Market Data & Analytics (Read-Only)"
            return {
                "status": "success",
                "message": f"Verified Upstox {token_role} as {user_name} ({user_id}).",
                "user_name": user_name,
                "user_id": user_id,
                "token_type": token_type,
                "is_active": True
            }
        else:
            return {
                "status": "error",
                "message": f"Upstox API Error ({res.status_code}): {res.text[:200]}",
                "is_active": False
            }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Connection error: {str(e)}",
            "is_active": False
        }

def fetch_upstox_candles(ticker: str, interval: str = "15m", period: str = "60d") -> pd.DataFrame:
    """
    Fetches real-time candles from Upstox API v2 and returns standard DataFrame.
    Intervals supported: 1m, 15m, 30m, 1d (day)
    """
    cfg = get_upstox_config()
    token = cfg.get("access_token")
    if not token:
        raise Exception("Upstox access token not configured.")
        
    inst_key = get_instrument_key(ticker)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token.strip()}"
    }
    
    # Map interval
    upstox_interval = "15minute"
    if interval in ("1m", "1min", "1minute"):
        upstox_interval = "1minute"
    elif interval in ("5m", "5min", "5minute"):
        upstox_interval = "5minute"
    elif interval in ("30m", "30min"):
        upstox_interval = "30minute"
    elif interval in ("1d", "1day", "D", "day"):
        upstox_interval = "day"
        
    to_date = datetime.now().strftime("%Y-%m-%d")
    days_back = 60
    if period in ("5y", "2y", "1y"):
        days_back = 365 * 2
    elif period == "60d":
        days_back = 60
    elif period == "30d":
        days_back = 30
        
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    
    url = f"{UPSTOX_BASE_URL}/historical-candle/{inst_key}/{upstox_interval}/{to_date}/{from_date}"
    res = requests.get(url, headers=headers, timeout=8)
    
    if res.status_code != 200:
        raise Exception(f"Upstox API Error ({res.status_code}): {res.text[:200]}")
        
    data = res.json()
    candles = data.get("data", {}).get("candles", [])
    if not candles:
        return pd.DataFrame()
        
    # Upstox returns: [timestamp, open, high, low, close, volume, open_interest]
    df = pd.DataFrame(candles, columns=['datetime', 'open', 'high', 'low', 'close', 'volume', 'oi'])
    df['datetime'] = pd.to_datetime(df['datetime'])
    df = df.sort_values('datetime').reset_index(drop=True)
    df.drop(columns=['oi'], inplace=True, errors='ignore')
    return df

def fetch_upstox_ltp(ticker: str) -> Optional[float]:
    """Fetches real-time LTP quote for a ticker."""
    cfg = get_upstox_config()
    token = cfg.get("access_token")
    if not token:
        return None
        
    inst_key = get_instrument_key(ticker)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token.strip()}"
    }
    
    try:
        url = f"{UPSTOX_BASE_URL}/market-quote/ltp?instrument_key={inst_key}"
        res = requests.get(url, headers=headers, timeout=4)
        if res.status_code == 200:
            data = res.json()
            quotes = data.get("data", {})
            for key, val in quotes.items():
                if "last_price" in val:
                    return float(val["last_price"])
    except Exception as e:
        logger.warning(f"Upstox LTP fetch error for {ticker}: {e}")
    return None
