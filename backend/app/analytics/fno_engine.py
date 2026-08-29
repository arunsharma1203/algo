import time
import logging
import random
from typing import Dict, Any, Optional, List

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as cffi_requests
    HAS_CURL_CFFI = False

logger = logging.getLogger(__name__)

_FNO_CACHE = {
    'data': {},
    'timestamp': 0
}
_FNO_CACHE_TTL = 180  # 3 minutes cache

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15"
]

def _create_nse_session():
    """Initializes a browser-impersonating session for NSE India."""
    if HAS_CURL_CFFI:
        session = cffi_requests.Session(impersonate="chrome120")
    else:
        session = cffi_requests.Session()

    session.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session

def fetch_nse_option_chain(symbol: str = "NIFTY", expiry: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetches official live NSE India Option Chain using option-chain-v3 API.
    
    CRITICAL RULE:
    If NSE data is unreachable or fails, returns a clean 'unavailable' payload.
    NEVER fabricates mock PCR or fictitious Open Interest levels.
    """
    global _FNO_CACHE
    epoch_now = time.time()
    clean_sym = symbol.replace(".NS", "").replace(".BO", "").strip().upper()
    cache_key = f"{clean_sym}_{expiry or 'nearest'}"
    
    # 1. Check cache
    if cache_key in _FNO_CACHE['data'] and (epoch_now - _FNO_CACHE['timestamp']) < _FNO_CACHE_TTL:
        return _FNO_CACHE['data'][cache_key]

    is_index = clean_sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")
    type_param = "Indices" if is_index else "Equity"

    headers = {
        "Referer": "https://www.nseindia.com/option-chain",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin"
    }

    session = _create_nse_session()

    try:
        # Warmup session with landing pages to establish valid Akamai/Cloudflare cookies
        session.get("https://www.nseindia.com", timeout=5.0)
        session.get("https://www.nseindia.com/option-chain", timeout=5.0)

        # 2. Fetch contract info to obtain available expiry dates
        info_url = f"https://www.nseindia.com/api/option-chain-contract-info?symbol={clean_sym}"
        info_res = session.get(info_url, headers=headers, timeout=5.0)

        expiry_dates = []
        if info_res.status_code == 200 and len(info_res.content) > 10:
            try:
                info_data = info_res.json()
                expiry_dates = info_data.get("expiryDates", [])
            except Exception:
                expiry_dates = []

        target_expiry = expiry
        if not target_expiry and expiry_dates:
            target_expiry = expiry_dates[0]

        # 3. Query option-chain-v3 API
        if target_expiry:
            url = f"https://www.nseindia.com/api/option-chain-v3?type={type_param}&symbol={clean_sym}&expiry={target_expiry}"
        else:
            url = f"https://www.nseindia.com/api/option-chain-v3?type={type_param}&symbol={clean_sym}"

        res = session.get(url, headers=headers, timeout=6.0)
        
        if res.status_code == 200 and len(res.content) > 50:
            raw_data = res.json()
            parsed = parse_option_chain_data(raw_data, clean_sym, expiry_dates, target_expiry)
            _FNO_CACHE['data'][cache_key] = parsed
            _FNO_CACHE['timestamp'] = epoch_now
            return parsed

    except Exception as e:
        logger.warning(f"NSE Option Chain live fetch note for {clean_sym}: {e}")

    # Fallback to previously cached entry if available
    if cache_key in _FNO_CACHE['data']:
        return _FNO_CACHE['data'][cache_key]

    # Clean fail-closed payload without fabricated values
    return {
        "symbol": clean_sym,
        "status": "unavailable",
        "underlying_price": None,
        "pcr": None,
        "pcr_sentiment": "Option Chain Offline / Unavailable",
        "bias": "NEUTRAL",
        "max_pain": None,
        "total_ce_oi": 0,
        "total_pe_oi": 0,
        "call_walls": [],
        "put_walls": [],
        "buildup": "UNAVAILABLE",
        "expiry_dates": [],
        "selected_expiry": None,
        "is_live_nse": False
    }

def parse_option_chain_data(
    raw_data: dict,
    symbol: str,
    expiry_dates: Optional[List[str]] = None,
    selected_expiry: Optional[str] = None
) -> Dict[str, Any]:
    records = raw_data.get('records', {})
    data_list = records.get('data', [])
    underlying_price = float(records.get('underlyingValue', 0.0))
    all_expiries = expiry_dates or records.get('expiryDates', [])

    total_ce_oi = 0
    total_pe_oi = 0
    ce_strikes = []
    pe_strikes = []
    all_strikes = []

    for item in data_list:
        strike = item.get('strikePrice', 0)
        all_strikes.append(strike)

        if 'CE' in item:
            ce = item['CE']
            ce_oi = ce.get('openInterest', 0)
            total_ce_oi += ce_oi
            ce_strikes.append((strike, ce_oi, ce.get('changeinOpenInterest', 0)))

        if 'PE' in item:
            pe = item['PE']
            pe_oi = pe.get('openInterest', 0)
            total_pe_oi += pe_oi
            pe_strikes.append((strike, pe_oi, pe.get('changeinOpenInterest', 0)))

    # 1. Put-Call Ratio
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0
    if pcr >= 1.25:
        pcr_sentiment = "STRONG BULLISH FLOOR (Heavy Put Writing)"
        bias = "BULLISH"
    elif pcr <= 0.80:
        pcr_sentiment = "BEARISH CEILING (Heavy Call Writing)"
        bias = "BEARISH"
    else:
        pcr_sentiment = "NEUTRAL / BALANCED"
        bias = "NEUTRAL"

    # 2. Max Pain Strike
    max_pain = compute_max_pain(ce_strikes, pe_strikes, all_strikes, underlying_price)

    # 3. Top 3 OI Walls
    ce_strikes.sort(key=lambda x: x[1], reverse=True)
    pe_strikes.sort(key=lambda x: x[1], reverse=True)

    call_walls = [{"strike": s[0], "oi": s[1], "oi_change": s[2]} for s in ce_strikes[:3]]
    put_walls = [{"strike": s[0], "oi": s[1], "oi_change": s[2]} for s in pe_strikes[:3]]

    # 4. Buildup Classification
    buildup = "LONG BUILDUP" if bias == "BULLISH" else ("SHORT BUILDUP" if bias == "BEARISH" else "CONSOLIDATION")

    return {
        "symbol": symbol,
        "status": "live",
        "underlying_price": round(underlying_price, 2),
        "pcr": pcr,
        "pcr_sentiment": pcr_sentiment,
        "bias": bias,
        "max_pain": max_pain,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "call_walls": call_walls,
        "put_walls": put_walls,
        "buildup": buildup,
        "expiry_dates": all_expiries[:8],
        "selected_expiry": selected_expiry,
        "is_live_nse": True
    }

def compute_max_pain(ce_data, pe_data, strikes, underlying) -> float:
    if not strikes:
        return underlying

    unique_strikes = sorted(list(set(strikes)))
    ce_dict = {s[0]: s[1] for s in ce_data}
    pe_dict = {s[0]: s[1] for s in pe_data}

    min_loss = float('inf')
    max_pain_strike = unique_strikes[len(unique_strikes)//2] if unique_strikes else underlying

    for S in unique_strikes:
        total_buyer_payout = 0
        for K, oi in ce_dict.items():
            if S > K:
                total_buyer_payout += oi * (S - K)
        for K, oi in pe_dict.items():
            if S < K:
                total_buyer_payout += oi * (K - S)

        if total_buyer_payout < min_loss:
            min_loss = total_buyer_payout
            max_pain_strike = S

    return float(max_pain_strike)
