import sqlite3
import os
from typing import Dict, List, Any, Optional, Tuple, Set
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

# 5b. Autopilot Priority Universe (20 High-Volume Liquid Equities)
# INTENTIONALLY A SMALLER OPERATIONAL POOL FOR HIGH-FREQUENCY AUTONOMOUS EXECUTION
AUTOPILOT_PRIORITY_20_UNIVERSE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "BHARTIARTL.NS", "SBIN.NS", "ITC.NS", "LT.NS", "KOTAKBANK.NS",
    "TATAMOTORS.NS", "AXISBANK.NS", "MARUTI.NS", "SUNPHARMA.NS", "TITAN.NS",
    "BAJFINANCE.NS", "JSWSTEEL.NS", "TATASTEEL.NS", "POWERGRID.NS", "NTPC.NS"
]

# 5c. Liquid Sector Universes
BANK_NIFTY_UNIVERSE = [
    "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "KOTAKBANK.NS", "AXISBANK.NS",
    "INDUSINDBK.NS", "BANKBARODA.NS", "PNB.NS", "AUBANK.NS", "FEDERALBNK.NS",
    "BANDHANBNK.NS", "IDFCFIRSTB.NS"
]

NIFTY_IT_UNIVERSE = [
    "TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS", "TECHM.NS",
    "LTIM.NS", "PERSISTENT.NS", "COFORGE.NS", "MPHASIS.NS", "LTTS.NS"
]

# 6. Preset Definitions
UNIVERSE_PRESETS: Dict[str, Dict[str, Any]] = {
    "WATCHLIST": {
        "name": "User Custom Watchlist (Backend SQLite)",
        "tickers": [], # Dynamically loaded from user_watchlist table
        "description": "User's personal operational watchlist stored authoritatively in local backend SQLite database.",
        "survivorship_bias": "USER DEFINED"
    },
    "BANK_NIFTY": {
        "name": "NIFTY Bank (12 Liquid Banking Heavyweights)",
        "tickers": BANK_NIFTY_UNIVERSE,
        "description": "The liquid benchmark banking constituents of the Nifty Bank index.",
        "survivorship_bias": "LOW-MODERATE"
    },
    "NIFTY_IT": {
        "name": "NIFTY IT (10 Technology Leaders)",
        "tickers": NIFTY_IT_UNIVERSE,
        "description": "The premier technology leaders and IT software export constituents.",
        "survivorship_bias": "LOW-MODERATE"
    },
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
    "AUTOPILOT_PRIORITY_20": {
        "name": "Autopilot Priority Universe (20 Stocks)",
        "tickers": AUTOPILOT_PRIORITY_20_UNIVERSE,
        "description": "Intentionally smaller operational universe for high-frequency autonomous execution without API rate-limit bottlenecks.",
        "survivorship_bias": "MODERATE — Top 20 mega-caps by liquidity and market cap."
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
        "name": "All Locally Available Equities (Dynamic Local DB - Legacy Alias)",
        "tickers": [], # Dynamically populated from database
        "description": "Legacy alias for ALL_COLLECTED. Every equity symbol with complete historical data cached in the local database.",
        "survivorship_bias": "VARIABLE — All locally synchronized NSE assets."
    },
    "ALL_COLLECTED": {
        "name": "All Collected Sources (NIFTY 500 + Watchlist + Local DB)",
        "tickers": [], # Dynamically populated from all sources
        "description": "Comprehensive union of Nifty 500 constituents, custom watchlists, and all cached local database equities.",
        "survivorship_bias": "MINIMAL — Maximum available Indian equity breadth."
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
    if clean_name in ("WATCHLIST", "USER_WATCHLIST"):
        if custom_tickers:
            tickers = [t if t.endswith(('.NS', '.BO')) else f"{t}.NS" for t in custom_tickers if t]
        else:
            try:
                from app.api.watchlist import get_persisted_watchlist_tickers
                tickers = get_persisted_watchlist_tickers()
            except Exception:
                tickers = list(BENCHMARK_5_UNIVERSE)
        return {
            "name": f"User Watchlist ({len(tickers)} Symbols - Backend SQLite)",
            "tickers": tickers,
            "description": "User's personal operational watchlist stored authoritatively in local backend SQLite database.",
            "survivorship_bias": "USER DEFINED"
        }
    if clean_name in ("NIFTY_BANK", "BANK_NIFTY"):
        return UNIVERSE_PRESETS["BANK_NIFTY"]
    if clean_name in ("NIFTY_IT", "IT"):
        return UNIVERSE_PRESETS["NIFTY_IT"]
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
            "name": f"All Locally Available Equities ({len(db_tickers)} Stocks - Legacy Alias)",
            "tickers": db_tickers,
            "description": "Legacy alias for ALL_COLLECTED. Every equity symbol with complete historical data cached in the local database.",
            "survivorship_bias": "VARIABLE — All locally synchronized NSE assets."
        }
    if clean_name == "ALL_COLLECTED":
        db_tickers = get_available_db_tickers()
        custom_clean = [t if t.endswith(('.NS', '.BO')) else f"{t}.NS" for t in (custom_tickers or []) if t]
        merged = list(dict.fromkeys(NIFTY_500_UNIVERSE + db_tickers + list(LIVE_UNIVERSE) + custom_clean))
        return {
            "name": f"All Collected Sources ({len(merged)} Stocks)",
            "tickers": merged,
            "description": "Comprehensive union of Nifty 500 constituents, custom watchlists, and all cached local database equities.",
            "survivorship_bias": "MINIMAL — Maximum available Indian equity breadth."
        }
    if clean_name in UNIVERSE_PRESETS:
        return UNIVERSE_PRESETS[clean_name]
    return UNIVERSE_PRESETS["NIFTY_500"]

def get_universe_coverage(name: str = "NIFTY_500", custom_tickers: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Computes local database coverage for a universe or custom ticker list.
    Returns configured count, available count, missing count, coverage pct, and lists.
    """
    u_info = get_universe(name, custom_tickers=custom_tickers)
    configured_tickers = custom_tickers if (name.upper() == "CUSTOM" and custom_tickers) else u_info.get("tickers", [])
    if not configured_tickers and name.upper() in ("ALL_117", "ALL_COLLECTED"):
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

_CACHED_KNOWN_SYMBOLS: Optional[set] = None

def get_authoritative_universe_symbols() -> set:
    """
    Returns the authoritative set of recognized NSE/BSE symbols across all defined universes.
    Caches the result in-memory with lazy reload.
    """
    global _CACHED_KNOWN_SYMBOLS
    if _CACHED_KNOWN_SYMBOLS is not None:
        return _CACHED_KNOWN_SYMBOLS

    symbols = set()
    if NIFTY_500_UNIVERSE:
        symbols.update(NIFTY_500_UNIVERSE)
    if LIVE_UNIVERSE:
        symbols.update(LIVE_UNIVERSE)
    if RESEARCH_100_UNIVERSE:
        symbols.update(RESEARCH_100_UNIVERSE)
    if BANK_NIFTY_UNIVERSE:
        symbols.update(BANK_NIFTY_UNIVERSE)
    if NIFTY_IT_UNIVERSE:
        symbols.update(NIFTY_IT_UNIVERSE)
    
    db_syms = get_available_db_tickers()
    if db_syms:
        symbols.update(db_syms)

    _CACHED_KNOWN_SYMBOLS = symbols
    return _CACHED_KNOWN_SYMBOLS

def invalidate_universe_cache() -> None:
    """Resets the cached symbol set so newly ingested database symbols are recognized."""
    global _CACHED_KNOWN_SYMBOLS
    _CACHED_KNOWN_SYMBOLS = None

def normalize_ticker(ticker: str) -> str:
    """
    Authoritative single-source-of-truth ticker normalizer.
    Converts string to uppercase, strips whitespace, preserves indices (^NSEI, ^INDIAVIX),
    and appends .NS default suffix if no .BO or .NS suffix is present.
    """
    if not ticker or not isinstance(ticker, str):
        return ""
    t = ticker.strip().upper()
    if not t:
        return ""
    if t.startswith("^"):
        return t
    if t.endswith((".NS", ".BO")):
        return t
    return f"{t}.NS"

def validate_ticker(ticker: str) -> Tuple[bool, str, Optional[str]]:
    """
    Authoritative single-ticker validator.
    Distinguishes:
      - SYNTAX / FORMAT VALIDITY
      - REAL / KNOWN INSTRUMENT (in authoritative 500+ Indian equity universe)
      - INVALID / UNKNOWN INSTRUMENT (e.g. DSDSDS.NS)
    
    Returns:
      (is_valid: bool, canonical_ticker: str, error_message: Optional[str])
    """
    import re
    if not ticker or not isinstance(ticker, str):
        return False, "", "Ticker symbol cannot be empty or non-string."

    raw = ticker.strip().upper()
    if not raw:
        return False, "", "Ticker symbol cannot be blank."

    # Reject mock/cache/temp leaks immediately
    if raw.startswith(("CACHE_", "TEMP_", "MOCK_")):
        return False, "", f"INVALID_TICKER: Mock/temporary symbol '{raw}' cannot enter research or trading."

    # Allow TESTSTOCK.NS strictly for pipeline tests
    if raw in ("TESTSTOCK.NS", "TESTSTOCK"):
        return True, "TESTSTOCK.NS", None

    # Canonicalize exchange suffix (.NS default, preserve .BO)
    canonical = raw
    if not (canonical.endswith(".NS") or canonical.endswith(".BO")):
        canonical = f"{canonical}.NS"

    # Syntax check
    if not re.match(r"^[A-Z0-9_\-&]+(\.NS|\.BO)$", canonical):
        return False, "", f"INVALID_TICKER: Syntax error in symbol '{raw}'. Does not match exchange equity format."

    # Authoritative universe check (514+ symbols)
    known = get_authoritative_universe_symbols()
    
    # Check exact match
    if canonical in known:
        return True, canonical, None

    # Check without suffix (in case universe stored raw symbol)
    base_sym = canonical.split(".")[0]
    if base_sym in known or f"{base_sym}.NS" in known or f"{base_sym}.BO" in known:
        resolved = f"{base_sym}.NS" if f"{base_sym}.NS" in known else canonical
        return True, resolved, None

    # Fail closed: Symbol is unknown in the authoritative universe
    return False, "", f"INVALID_TICKER: Symbol '{canonical}' is not a recognized instrument in the authoritative universe."

def validate_ticker_list(raw_tickers: Any) -> Tuple[bool, List[str], Optional[str]]:
    """
    Authoritative multi-ticker validator for comma-separated or list inputs.
    FAILS CLOSED: If ANY ticker in the input list is invalid (e.g. RELIANCE,TCS,DSDSDS),
    the entire submission is rejected with an explicit error listing all invalid tickers.
    
    Returns:
      (is_valid: bool, canonical_tickers: List[str], error_message: Optional[str])
    """
    if raw_tickers is None:
        return False, [], "No ticker symbols provided."

    parts = []
    if isinstance(raw_tickers, str):
        parts = [s.strip() for s in raw_tickers.replace(";", ",").split(",") if s.strip()]
    elif isinstance(raw_tickers, (list, tuple, set)):
        parts = [str(s).strip() for s in raw_tickers if str(s).strip()]
    else:
        return False, [], f"Invalid tickers input type: {type(raw_tickers)}"

    if not parts:
        return False, [], "INVALID_TICKER: Ticker list cannot be empty."

    valid_symbols = []
    invalid_symbols = []

    for item in parts:
        ok, clean_sym, err = validate_ticker(item)
        if ok:
            if clean_sym not in valid_symbols:
                valid_symbols.append(clean_sym)
        else:
            invalid_symbols.append(item.strip().upper())

    if invalid_symbols:
        return False, [], f"INVALID_TICKER: The following ticker(s) are unrecognized in the authoritative universe: {', '.join(invalid_symbols)}"

    return True, valid_symbols, None

def resolve_universe_tickers(universe_name: str = "NIFTY_500", custom_tickers: Optional[List[str]] = None, single_stock: Optional[str] = None) -> List[str]:
    """
    Authoritative universe resolution engine for the entire platform.
    Used across Intraday, Swing, Research, Walk-Forward, and Autopilot sweeps.
    Supports NIFTY_500, NIFTY_50, BENCHMARK_5, AUTOPILOT_PRIORITY_20, LIVE_52, RESEARCH_100, ALL_COLLECTED, ALL_117, CUSTOM, WATCHLIST, BANK_NIFTY, NIFTY_IT, and SINGLE_STOCK.
    FAILS CLOSED on invalid single stock or custom tickers.
    """
    clean_name = universe_name.strip().upper() if universe_name else "NIFTY_500"
    
    if clean_name in ("SINGLE_STOCK", "SINGLE") or single_stock:
        target = single_stock or (custom_tickers[0] if custom_tickers else "RELIANCE.NS")
        ok, clean_stock, err = validate_ticker(target)
        if not ok:
            raise ValueError(f"Universe resolution failed: {err}")
        return [clean_stock]
        
    if clean_name in ("WATCHLIST", "USER_WATCHLIST"):
        if custom_tickers:
            ok, clean_list, err = validate_ticker_list(custom_tickers)
            if not ok:
                raise ValueError(f"Watchlist universe validation failed: {err}")
            return clean_list
        try:
            from app.api.watchlist import get_persisted_watchlist_tickers
            db_tickers = get_persisted_watchlist_tickers()
            return db_tickers if db_tickers else list(BENCHMARK_5_UNIVERSE)
        except Exception:
            return list(BENCHMARK_5_UNIVERSE)

    if clean_name in ("NIFTY_BANK", "BANK_NIFTY"):
        return list(BANK_NIFTY_UNIVERSE)
    if clean_name in ("NIFTY_IT", "IT"):
        return list(NIFTY_IT_UNIVERSE)
    if clean_name in ("NIFTY_100", "RESEARCH_100"):
        u_info = get_universe("RESEARCH_100", custom_tickers=custom_tickers)
        return u_info.get("tickers", [])
    if clean_name == "NIFTY_200":
        u_500 = get_universe("NIFTY_500", custom_tickers=custom_tickers)
        return u_500.get("tickers", [])[:200]

    if clean_name == "CUSTOM":
        if not custom_tickers:
            raise ValueError("INVALID_TICKER: Custom universe requires non-empty custom_tickers.")
        ok, clean_list, err = validate_ticker_list(custom_tickers)
        if not ok:
            raise ValueError(f"Custom universe validation failed: {err}")
        return clean_list

    if clean_name not in UNIVERSE_PRESETS and clean_name not in ("ALL_117", "ALL_COLLECTED"):
        raise ValueError(f"INVALID_TICKER: Unknown universe preset '{clean_name}'.")

    u_info = get_universe(clean_name, custom_tickers=custom_tickers)
    tickers = u_info.get("tickers", [])
    if not tickers and clean_name in ("ALL_117", "ALL_COLLECTED"):
        tickers = get_available_db_tickers()
    if not tickers:
        raise ValueError(f"INVALID_TICKER: Universe '{clean_name}' resolved to zero valid symbols.")
    return tickers



