"""
DataGateway: Centralized Single-Source-of-Truth Market Data Gateway.

Authoritative layer for all market data ingestion, OHLCV retrieval, real-time quote caching,
and point-in-time data validation. Routes requests transparently between HistoricalDataLayer
(SQLite 10-year cache / 15m accumulated cache) and MarketDataProvider (Upstox sub-second feed /
yfinance fallback) with strict validation.
"""

import logging
import sqlite3
from datetime import datetime, time
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd

from app.analytics.universe_config import normalize_ticker, validate_ticker
from app.data.validator import MarketDataValidator
from app.data.historical_data_layer import HistoricalDataLayer, get_db_path
from app.data.market_provider import get_live_quote_with_meta, fetch_candles, fetch_live_quote

logger = logging.getLogger(__name__)

class DataGateway:
    """
    Central Authoritative Market Data Gateway.
    Guarantees consistent schema, point-in-time validation, and caching across all workflows.
    """

    @classmethod
    def normalize_ticker(cls, ticker: str) -> str:
        """Enforces canonical ticker symbol format (.NS default)."""
        return normalize_ticker(ticker)

    @classmethod
    def validate_ticker(cls, ticker: str) -> Tuple[bool, str, Optional[str]]:
        """Validates ticker against authoritative universe (500+ NSE symbols)."""
        return validate_ticker(ticker)

    @classmethod
    def is_market_open(cls, force_override: bool = False) -> bool:
        """
        Checks if Indian Equity Cash Market (NSE/BSE) is currently open.
        Active Trading Hours: Monday-Friday, 09:15 to 15:30 IST.
        """
        if force_override:
            return True
        now = datetime.now()
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        market_open = time(9, 15)
        market_close = time(15, 30)
        return market_open <= now.time() <= market_close

    @classmethod
    def persist_intraday_candles(
        cls,
        ticker: str,
        df: pd.DataFrame,
        timeframe: str = "15m",
        source: str = "yfinance"
    ) -> int:
        """
        Persists and deduplicates intraday candles into canonical SQLite ohlcv storage.
        Tracks ticker, timestamp, timeframe, OHLCV, source, hoard_timestamp (ingestion time).
        Returns number of candles persisted.
        """
        if df is None or df.empty:
            return 0

        canonical_ticker = cls.normalize_ticker(ticker)
        if not canonical_ticker:
            return 0

        # Standardize column names
        work_df = df.copy()
        work_df.columns = [str(c).lower() for c in work_df.columns]

        # Extract date/datetime
        if "date" in work_df.columns:
            date_series = work_df["date"]
        elif "datetime" in work_df.columns:
            date_series = work_df["datetime"]
        elif work_df.index.name in ("Date", "Datetime", "date", "datetime") or isinstance(work_df.index, pd.DatetimeIndex):
            date_series = work_df.index.to_series()
        else:
            return 0

        required_cols = ["open", "high", "low", "close"]
        for col in required_cols:
            if col not in work_df.columns:
                return 0

        now_iso = datetime.now().isoformat()
        db_path = get_db_path()
        conn = sqlite3.connect(db_path, timeout=30.0)
        c = conn.cursor()

        rows_to_insert = []
        for idx_val, row in work_df.iterrows():
            raw_dt = date_series.loc[idx_val] if hasattr(date_series, 'loc') else row.get("date")
            try:
                dt_obj = pd.to_datetime(raw_dt)
                date_str = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                date_str = str(raw_dt).strip()

            try:
                o = float(row["open"])
                h = float(row["high"])
                l = float(row["low"])
                cl = float(row["close"])
                v = int(row.get("volume", 0)) if pd.notnull(row.get("volume")) else 0
            except (ValueError, TypeError):
                continue

            rows_to_insert.append((
                canonical_ticker,
                date_str,
                o, h, l, cl, v,
                timeframe,
                source,
                now_iso
            ))

        if not rows_to_insert:
            conn.close()
            return 0

        try:
            c.executemany("""
                INSERT OR REPLACE INTO ohlcv (
                    ticker, date, open, high, low, close, volume, timeframe, source, hoard_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, rows_to_insert)
            conn.commit()
            return len(rows_to_insert)
        except Exception as e:
            logger.warning(f"[DataGateway] Failed to persist intraday candles for {canonical_ticker}: {e}")
            conn.rollback()
            return 0
        finally:
            conn.close()

    @classmethod
    def get_ohlcv(
        cls,
        ticker: str,
        timeframe: str = "1d",
        period: Optional[str] = None,
        use_cache: bool = True,
        fetch_incremental: bool = True,
        min_rows: int = 30
    ) -> pd.DataFrame:
        """
        Retrieves canonical OHLCV DataFrame for the given ticker and timeframe.
        For intraday (15m): loads cached candles from SQLite, transparently fetches
        fresh incremental candles, deduplicates, persists to SQLite, and returns
        the merged dataset with freshness metadata.
        """
        canonical_ticker = cls.normalize_ticker(ticker)
        if not canonical_ticker:
            logger.warning(f"[DataGateway] Empty or invalid ticker passed to get_ohlcv: '{ticker}'")
            return pd.DataFrame()

        tf = timeframe.lower()
        df = pd.DataFrame()

        # 1. Attempt historical cache read if use_cache is True
        if use_cache:
            try:
                df = HistoricalDataLayer.get_historical_ohlcv(
                    canonical_ticker,
                    timeframe=tf,
                    start_date=None,
                    end_date=None
                )
            except Exception as e:
                logger.debug(f"[DataGateway] Cache retrieval failed for {canonical_ticker} ({tf}): {e}")

        # 2. Check if live/incremental fetch is required
        if tf in ("15m", "intraday"):
            if df is None or df.empty or len(df) < min_rows:
                # Full fetch and persist
                try:
                    fetch_period = period or "60d"
                    df_fetched = fetch_candles(canonical_ticker, interval="15m", period=fetch_period)
                    if df_fetched is not None and not df_fetched.empty:
                        cls.persist_intraday_candles(canonical_ticker, df_fetched, timeframe="15m")
                        df = HistoricalDataLayer.get_historical_ohlcv(canonical_ticker, timeframe="15m")
                except Exception as e:
                    logger.warning(f"[DataGateway] 15m full fetch failed for {canonical_ticker}: {e}")
            elif fetch_incremental:
                # Incremental fetch to augment existing cache
                try:
                    df_recent = fetch_candles(canonical_ticker, interval="15m", period="5d")
                    if df_recent is not None and not df_recent.empty:
                        cls.persist_intraday_candles(canonical_ticker, df_recent, timeframe="15m")
                        df = HistoricalDataLayer.get_historical_ohlcv(canonical_ticker, timeframe="15m")
                except Exception as e:
                    logger.debug(f"[DataGateway] 15m incremental fetch failed for {canonical_ticker}: {e}")
        else:
            if df is None or df.empty or len(df) < min_rows:
                try:
                    fetch_period = period or ("1y" if tf == "1d" else "60d")
                    df_fetched = fetch_candles(canonical_ticker, interval=tf, period=fetch_period)
                    if df_fetched is not None and not df_fetched.empty:
                        df = df_fetched
                except Exception as e:
                    logger.warning(f"[DataGateway] Live fetch failed for {canonical_ticker} ({tf}): {e}")

        if df is None or df.empty:
            return pd.DataFrame()

        # 3. Ensure canonical column names
        col_map = {c: str(c).lower() for c in df.columns}
        df = df.rename(columns=col_map)

        if "date" in df.columns and "datetime" not in df.columns:
            df["datetime"] = df["date"]
        elif "datetime" in df.columns and "date" not in df.columns:
            df["date"] = df["datetime"]

        return df

    @classmethod
    def get_live_quote(cls, ticker: str) -> Dict[str, Any]:
        """
        Retrieves authoritative real-time LTP and source metadata for a single symbol.
        """
        canonical_ticker = cls.normalize_ticker(ticker)
        if not canonical_ticker:
            return {"ticker": ticker, "price": None, "source": "NONE", "timestamp": None, "is_fresh": False}

        try:
            meta = get_live_quote_with_meta(canonical_ticker)
            price = meta.get("price")
            return {
                "ticker": canonical_ticker,
                "price": float(price) if price is not None else None,
                "source": meta.get("source", "UNKNOWN"),
                "timestamp": meta.get("timestamp", datetime.now().isoformat()),
                "is_fresh": bool(meta.get("is_fresh", True if meta.get("source") == "upstox" else False)),
                "raw_meta": meta
            }
        except Exception as e:
            logger.warning(f"[DataGateway] Error fetching live quote for {canonical_ticker}: {e}")
            return {"ticker": canonical_ticker, "price": None, "source": "ERROR", "timestamp": None, "is_fresh": False}

    @classmethod
    def get_batch_quotes(cls, tickers: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Fetches live quotes for a list of tickers with error isolation.
        """
        results = {}
        for t in tickers:
            canonical = cls.normalize_ticker(t)
            if not canonical:
                continue
            results[canonical] = cls.get_live_quote(canonical)
        return results

    @classmethod
    def get_cached_features(cls, ticker: str, timeframe: str = "1d") -> pd.DataFrame:
        """
        Retrieves pre-computed point-in-time features from HistoricalDataLayer feature cache.
        """
        canonical = cls.normalize_ticker(ticker)
        return HistoricalDataLayer.get_cached_features(canonical, timeframe=timeframe)

    @classmethod
    def sync_ticker(cls, ticker: str, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Synchronizes 10-year daily historical data for a ticker into SQLite storage.
        """
        canonical = cls.normalize_ticker(ticker)
        return HistoricalDataLayer.sync_ticker_daily_10y(canonical, force_refresh=force_refresh)

    @classmethod
    def validate_data(
        cls,
        df: pd.DataFrame,
        ticker: str = "UNKNOWN",
        timeframe: str = "15m",
        min_rows: int = 30
    ) -> Dict[str, Any]:
        """
        Validates OHLCV DataFrame using MarketDataValidator.
        """
        canonical = cls.normalize_ticker(ticker)
        return MarketDataValidator.validate_ohlcv(
            df=df,
            ticker=canonical,
            timeframe=timeframe,
            min_rows=min_rows
        )
