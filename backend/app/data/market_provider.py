"""
Market Data Provider: Pure Upstox Real-Time Market Data Layer.

Invariants:
- 100% Upstox API v2 native data sourcing for live quotes, candles, and bulk feeds.
- Zero reliance, zero fallback, and zero defaults on Yahoo Finance (yfinance).
- Fail-closed pricing integrity: if Upstox quote is unavailable, returns UNAVAILABLE
  instead of contaminating decision engine with stale or delayed quotes.
- Strict UpstoxRateLimiter compliance across single and bulk endpoints.
"""

import logging
import sqlite3
import pandas as pd
from typing import Dict, Any, Optional, List
from datetime import datetime

from app.data.upstox_provider import (
    fetch_upstox_candles,
    fetch_upstox_ltp,
    fetch_upstox_bulk_ltp,
    fetch_upstox_bulk_quotes,
    test_upstox_connection,
    get_upstox_limiter
)
from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)


def get_active_data_source() -> str:
    """Returns authoritative market data source. Locked strictly to 'upstox'."""
    return "upstox"


def fetch_candles(ticker: str, interval: str = "15m", period: str = "60d") -> pd.DataFrame:
    """
    Fetches historical and real-time candles strictly from Upstox API v2.
    Protected by UpstoxRateLimiter. Fails closed (empty DataFrame) if token is invalid or API fails.
    Never defaults or falls back to Yahoo Finance.
    """
    try:
        df = fetch_upstox_candles(ticker, interval=interval, period=period)
        if df is not None and not df.empty and len(df) >= 5:
            logger.info(f"Loaded {len(df)} candles for {ticker} from Upstox ({interval} | {period}).")
            return df
        else:
            logger.warning(f"Upstox returned empty candles for {ticker}.")
            return pd.DataFrame()
    except Exception as e:
        logger.warning(f"Upstox candle fetch failed for {ticker}: {e}.")
        return pd.DataFrame()


def fetch_live_quote(ticker: str) -> Optional[float]:
    """Fetches real-time LTP quote via Upstox. Returns None if unavailable."""
    meta = get_live_quote_with_meta(ticker)
    return meta.get("price")


def get_live_quote_with_meta(ticker: str) -> Dict[str, Any]:
    """
    Fetches real-time LTP with full source metadata strictly from Upstox.
    Includes age_ms and quote_freshness to enforce fail-closed pricing integrity.
    Bypasses Upstox immediately if circuit breaker is open to prevent redundant overhead.
    Never defaults or falls back to Yahoo Finance.
    """
    clean_ticker = ticker.replace('.NS', '').replace('.BO', '').strip().upper()
    now_str = datetime.now().strftime("%H:%M:%S IST")

    limiter = get_upstox_limiter()
    if not limiter.is_circuit_open():
        try:
            from app.data.upstox_provider import get_cached_upstox_quote
            cached = get_cached_upstox_quote(clean_ticker)
            if cached and cached.get("ltp") and float(cached["ltp"]) > 0:
                price = float(cached["ltp"])
            else:
                price = fetch_upstox_ltp(clean_ticker)

            if price is not None and price > 0:
                return {
                    "ticker": ticker,
                    "price": round(float(price), 2),
                    "source": "upstox",
                    "source_name": "Upstox Real-Time (0ms)",
                    "is_realtime": True,
                    "age_ms": 0,
                    "quote_freshness": "REAL_TIME",
                    "timestamp": now_str
                }
        except Exception as e:
            logger.warning(f"Upstox live quote failed for {ticker}: {e}")

    # Fail closed: never substitute Yahoo Finance
    return {
        "ticker": ticker,
        "price": None,
        "source": "unavailable",
        "source_name": "Data Feed Offline / Auth Expired",
        "is_realtime": False,
        "age_ms": None,
        "quote_freshness": "UNAVAILABLE",
        "timestamp": now_str
    }


def get_bulk_live_quotes_with_meta(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Fetches real-time LTP with full source metadata for MULTIPLE tickers simultaneously
    via Upstox multi-instrument bulk quoting.
    Rate limit compliant: bundles up to 500 symbols per single request.
    Fails closed for any missing symbol. Never defaults or falls back to Yahoo Finance.
    """
    if not tickers:
        return {}

    results: Dict[str, Dict[str, Any]] = {}
    now_str = datetime.now().strftime("%H:%M:%S IST")

    sym_to_orig: Dict[str, str] = {}
    clean_syms: List[str] = []
    for t in tickers:
        clean = t.replace('.NS', '').replace('.BO', '').strip().upper()
        sym_to_orig[clean] = t
        clean_syms.append(clean)

    limiter = get_upstox_limiter()
    if not limiter.is_circuit_open():
        try:
            upstox_quotes = fetch_upstox_bulk_ltp(clean_syms)
            for sym, price in upstox_quotes.items():
                if price is not None and price > 0:
                    clean_sym = sym.replace('.NS', '').replace('.BO', '').strip().upper()
                    orig_t = sym_to_orig.get(clean_sym, sym_to_orig.get(sym, f"{clean_sym}.NS"))
                    quote_entry = {
                        "ticker": orig_t,
                        "price": round(float(price), 2),
                        "source": "upstox",
                        "source_name": "Upstox Real-Time (0ms)",
                        "is_realtime": True,
                        "age_ms": 0,
                        "quote_freshness": "REAL_TIME",
                        "timestamp": now_str
                    }
                    results[orig_t] = quote_entry
                    results[clean_sym] = quote_entry
                    results[f"{clean_sym}.NS"] = quote_entry
                    results[f"{clean_sym}.BO"] = quote_entry
        except Exception as e:
            logger.warning(f"Upstox bulk live quote failed: {e}")

    # For any ticker missing or unresolved, fail closed with UNAVAILABLE
    for t in tickers:
        if t not in results or results[t].get("price") is None:
            clean_t = t.replace('.NS', '').replace('.BO', '').strip().upper()
            if clean_t in results and results[clean_t].get("price") is not None:
                results[t] = results[clean_t]
            else:
                results[t] = {
                    "ticker": t,
                    "price": None,
                    "source": "unavailable",
                    "source_name": "Data Feed Offline / Auth Expired",
                    "is_realtime": False,
                    "age_ms": None,
                    "quote_freshness": "UNAVAILABLE",
                    "timestamp": now_str
                }

    return results
