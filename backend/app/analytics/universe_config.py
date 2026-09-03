import sqlite3
import os
from typing import Dict, List, Any, Optional
from app.api.intraday_ml import INDIAN_STOCK_UNIVERSE

# 1. Production Live Universe (52 Large-Cap & Liquid Momentum Equities)
# PRESERVED UNTOUCHED FOR LIVE SCANNING
LIVE_UNIVERSE = list(INDIAN_STOCK_UNIVERSE)

# 2. Benchmark 5 Universe (Production Training Benchmark)
BENCHMARK_5_UNIVERSE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS"
]

# 3. NIFTY 50 Universe (Top 50 Blue-Chip Equities)
NIFTY_50_UNIVERSE = list(INDIAN_STOCK_UNIVERSE[:50])

# 4. Expanded Research Universe (100 Liquid Equities with 10-Year Historical Depth)
RESEARCH_100_UNIVERSE = list(set(INDIAN_STOCK_UNIVERSE + [
    "ASIANPAINT.NS", "AXISBANK.NS", "BAJAJ-AUTO.NS", "BAJFINANCE.NS", "BAJAJFINSV.NS",
    "BHARTIARTL.NS", "BPCL.NS", "BRITANNIA.NS", "CIPLA.NS", "COALINDIA.NS",
    "DIVISLAB.NS", "DRREDDY.NS", "EICHERMOT.NS", "GRASIM.NS", "HCLTECH.NS",
    "HDFCBANK.NS", "HDFCLIFE.NS", "HEROMOTOCO.NS", "HINDALCO.NS", "HINDUNILVR.NS",
    "ICICIBANK.NS", "INDUSINDBK.NS", "INFY.NS", "ITC.NS", "JSWSTEEL.NS",
    "KOTAKBANK.NS", "LT.NS", "M&M.NS", "MARUTI.NS", "NESTLEIND.NS",
    "NTPC.NS", "ONGC.NS", "POWERGRID.NS", "RELIANCE.NS", "SBILIFE.NS",
    "SBIN.NS", "SUNPHARMA.NS", "TATACONSUM.NS", "TATAMOTORS.NS", "TATASTEEL.NS",
    "TCS.NS", "TECHM.NS", "TITAN.NS", "ULTRACEMCO.NS", "WIPRO.NS",
    "AMBUJACEM.NS", "AUROPHARMA.NS", "BANKBARODA.NS", "BOSCHLTD.NS", "CANBK.NS",
    "CHOLAFIN.NS", "COLPAL.NS", "DABUR.NS", "DLF.NS", "GAIL.NS",
    "GODREJCP.NS", "HAVELLS.NS", "ICICIGI.NS", "ICICIPRULI.NS", "INDIGO.NS",
    "JINDALSTEL.NS", "LICI.NS", "LUPIN.NS", "MARICO.NS", "MUTHOOTFIN.NS",
    "NAUKRI.NS", "PIDILITIND.NS", "PFC.NS", "PNB.NS", "RECLTD.NS",
    "SIEMENS.NS", "SRF.NS", "TORNTPHARM.NS", "TVSMOTOR.NS", "VEDL.NS",
    "VOLTAS.NS", "ZYDUSLIFE.NS", "ABB.NS", "ADANIPOWER.NS", "BEL.NS",
    "HAL.NS", "JIOFIN.NS", "TRENT.NS", "VBL.NS", "ZOMATO.NS"
]))

# 5. NIFTY 500 Universe (500 Broad Market Equities)
NIFTY_500_JSON_PATH = os.path.join(os.path.dirname(__file__), "nifty500_tickers.json")
if os.path.exists(NIFTY_500_JSON_PATH):
    try:
        import json
        with open(NIFTY_500_JSON_PATH, "r") as f:
            NIFTY_500_UNIVERSE = json.load(f)
    except Exception:
        NIFTY_500_UNIVERSE = list(RESEARCH_100_UNIVERSE)
else:
    NIFTY_500_UNIVERSE = list(RESEARCH_100_UNIVERSE)

# 6. Preset Definitions
UNIVERSE_PRESETS: Dict[str, Dict[str, Any]] = {
    "NIFTY_500": {
        "name": "NIFTY 500 (500 Stocks - Broad Market)",
        "tickers": NIFTY_500_UNIVERSE,
        "description": "The 500 top liquid constituents of the NSE Nifty 500 index covering 95% of Indian equity market cap.",
        "survivorship_bias": "LOW-MODERATE — Broad index constituents."
    },
    "BENCHMARK_5": {
        "name": "Production Training Benchmark (5 Heavyweights)",
        "tickers": BENCHMARK_5_UNIVERSE,
        "description": "The 5 primary sector heavyweights used for automated Champion model training and Optuna tuning.",
        "survivorship_bias": "MODERATE — Uses current top 5 market leaders."
    },
    "NIFTY_50": {
        "name": "NIFTY 50 Bluechips (50 Stocks)",
        "tickers": NIFTY_50_UNIVERSE,
        "description": "The 50 large-cap benchmark constituents of the Nifty 50 Index.",
        "survivorship_bias": "LOW-MODERATE — Standard large-cap index constituents."
    },
    "LIVE_52": {
        "name": "Live Scanner Universe (52 Stocks)",
        "tickers": LIVE_UNIVERSE,
        "description": "The 52 high-liquidity stocks evaluated by the real-time Intraday and Swing ML scanners.",
        "survivorship_bias": "MODERATE — Evaluates surviving 2026 constituents back into 2021."
    },
    "RESEARCH_100": {
        "name": "Expanded Research Universe (100 Stocks)",
        "tickers": RESEARCH_100_UNIVERSE,
        "description": "Broad 100-stock liquid universe for multi-asset research and portfolio walk-forward testing.",
        "survivorship_bias": "MODERATE-HIGH — Retrospective selection of current 100 liquid stocks."
    },
    "ALL_117": {
        "name": "All Locally Available Equities (117 Stocks)",
        "tickers": [], # Dynamically populated from database
        "description": "Every equity symbol with complete historical data cached in the local database.",
        "survivorship_bias": "VARIABLE — All locally synchronized NSE assets."
    },
    "CUSTOM": {
        "name": "Custom User-Selected Universe",
        "tickers": [],
        "description": "User-defined custom basket of stocks.",
        "survivorship_bias": "USER DEFINED"
    }
}

def get_available_db_tickers() -> List[str]:
    """Retrieves all distinct tickers with daily data in the local canonical database."""
    try:
        from app.data.historical_data_layer import get_db_path
        db_path = get_db_path()
        if not os.path.exists(db_path):
            return list(LIVE_UNIVERSE)
        conn = sqlite3.connect(db_path, timeout=5.0)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT ticker FROM ohlcv WHERE timeframe = '1d' ORDER BY ticker")
        tickers = [r[0] for r in cur.fetchall()]
        conn.close()
        return tickers if tickers else list(LIVE_UNIVERSE)
    except Exception:
        return list(LIVE_UNIVERSE)

def get_universe(name: str = "BENCHMARK_5", custom_tickers: Optional[List[str]] = None) -> Dict[str, Any]:
    """Retrieves universe configuration and survivorship bias disclosures."""
    clean_name = name.strip().upper()
    if clean_name == "CUSTOM" and custom_tickers:
        return {
            "name": f"Custom Basket ({len(custom_tickers)} Symbols)",
            "tickers": custom_tickers,
            "description": "Custom user-selected basket of stocks.",
            "survivorship_bias": "USER DEFINED"
        }
    if clean_name == "ALL_117":
        db_tickers = get_available_db_tickers()
        return {
            "name": f"All Locally Available Equities ({len(db_tickers)} Stocks)",
            "tickers": db_tickers,
            "description": "Every equity symbol with complete historical data cached in the local database.",
            "survivorship_bias": "VARIABLE — All locally synchronized NSE assets."
        }
    if clean_name in UNIVERSE_PRESETS:
        return UNIVERSE_PRESETS[clean_name]
    return UNIVERSE_PRESETS["BENCHMARK_5"]

def get_universe_coverage(name: str = "BENCHMARK_5", custom_tickers: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Computes local database coverage for a universe or custom ticker list.
    Returns configured count, available count, missing count, coverage pct, and lists.
    """
    u_info = get_universe(name, custom_tickers=custom_tickers)
    configured_tickers = custom_tickers if (name.upper() == "CUSTOM" and custom_tickers) else u_info.get("tickers", [])
    if not configured_tickers and name.upper() == "ALL_117":
        configured_tickers = get_available_db_tickers()

    db_tickers_set = set(get_available_db_tickers())
    available = [t for t in configured_tickers if t in db_tickers_set]
    missing = [t for t in configured_tickers if t not in db_tickers_set]
    total = len(configured_tickers)
    cov_pct = round((len(available) / total * 100.0), 1) if total > 0 else 100.0

    return {
        "universe": name.upper(),
        "total_configured": total,
        "available_count": len(available),
        "missing_count": len(missing),
        "coverage_pct": cov_pct,
        "available_symbols": available,
        "missing_symbols": missing,
        "all_db_symbols": sorted(list(db_tickers_set))
    }

def resolve_universe_tickers(universe_name: str = "NIFTY_500", custom_tickers: Optional[List[str]] = None, single_stock: Optional[str] = None) -> List[str]:
    """
    Authoritative universe resolution engine for the entire platform.
    Used across Intraday, Swing, Research, Walk-Forward, and Autopilot sweeps.
    Supports NIFTY_500, NIFTY_50, BENCHMARK_5, LIVE_52, RESEARCH_100, ALL_117, CUSTOM, and SINGLE_STOCK.
    """
    clean_name = universe_name.strip().upper() if universe_name else "NIFTY_500"
    
    if clean_name in ("SINGLE_STOCK", "SINGLE") or single_stock:
        target = single_stock or (custom_tickers[0] if custom_tickers else "RELIANCE.NS")
        clean_stock = target.strip().upper()
        if not clean_stock.endswith((".NS", ".BO")):
            clean_stock = f"{clean_stock}.NS"
        return [clean_stock]
        
    if clean_name == "CUSTOM" and custom_tickers:
        return [t.strip().upper() if t.endswith(('.NS', '.BO')) else f"{t.strip().upper()}.NS" for t in custom_tickers if t]
        
    u_info = get_universe(clean_name, custom_tickers=custom_tickers)
    tickers = u_info.get("tickers", [])
    if not tickers and clean_name == "ALL_117":
        tickers = get_available_db_tickers()
    return tickers if tickers else list(LIVE_UNIVERSE)


