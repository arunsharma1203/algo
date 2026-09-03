import os
import sqlite3
import logging
import subprocess
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import pandas as pd
import ta
import yfinance as yf

from app.data.validator import MarketDataValidator

logger = logging.getLogger(__name__)

# Global in-memory cache for point-in-time technical features
_FEATURE_CACHE: Dict[str, pd.DataFrame] = {}

from app.data.database import get_db_path, get_connection, get_readonly_connection, is_canonical_path

class HistoricalDataLayer:
    """
    Centralized Historical Market-Data Layer.
    Manages 10-year daily and accumulated 15m intraday OHLCV in SQLite with incremental syncing,
    duplicate prevention, in-memory feature caching, and source metadata preservation.
    """

    @classmethod
    def get_system_resource_profile(cls) -> Dict[str, Any]:
        """Audits hardware specifications on Apple Silicon (M1/M2/M3)."""
        profile = {
            "platform": "Apple Silicon (macOS)",
            "cpu_brand": "Apple M1 Pro",
            "total_logical_cpus": 8,
            "performance_cores": 6,
            "efficiency_cores": 2,
            "total_ram_gb": 16.0,
            "recommended_workers": 4,
            "recommended_ml_threads": 1,
            "rationale": "Dedicate 4 workers to M1 Performance Cores with OMP_NUM_THREADS=1, reserving 2 P-Cores + 2 E-Cores for FastAPI and OS."
        }
        try:
            brand = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"]).decode().strip()
            ncpu = int(subprocess.check_output(["sysctl", "-n", "hw.ncpu"]).decode().strip())
            p_cpu = int(subprocess.check_output(["sysctl", "-n", "hw.perflevel0.logicalcpu"]).decode().strip())
            e_cpu = int(subprocess.check_output(["sysctl", "-n", "hw.perflevel1.logicalcpu"]).decode().strip())
            mem_bytes = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).decode().strip())

            profile["cpu_brand"] = brand
            profile["total_logical_cpus"] = ncpu
            profile["performance_cores"] = p_cpu
            profile["efficiency_cores"] = e_cpu
            profile["total_ram_gb"] = round(mem_bytes / (1024**3), 1)
            profile["recommended_workers"] = max(1, p_cpu - 2)
        except Exception:
            pass
        return profile

    @classmethod
    def init_schema(cls):
        """Ensures all necessary historical tables and performance indexes exist."""
        db_path = get_db_path()
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            
            # Primary OHLCV table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ohlcv (
                    ticker TEXT,
                    date TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume INTEGER,
                    source TEXT DEFAULT 'yfinance',
                    hoard_timestamp TEXT,
                    timeframe TEXT DEFAULT '1d',
                    PRIMARY KEY (ticker, date, timeframe)
                )
            """)

            # Non-destructive migrations for existing ohlcv table
            for col_name, col_def in [
                ("timeframe", "TEXT DEFAULT '1d'"),
                ("source", "TEXT DEFAULT 'yfinance'"),
                ("hoard_timestamp", "TEXT")
            ]:
                try:
                    conn.execute(f"ALTER TABLE ohlcv ADD COLUMN {col_name} {col_def}")
                except Exception:
                    pass

            # Fast index on ticker and date
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ohlcv_lookup ON ohlcv (ticker, timeframe, date);")
            except Exception as e:
                logger.warning(f"Could not create ohlcv index: {e}")

            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ml_train_lookup ON ml_training_data (ticker, datetime);")
            except Exception as e:
                logger.warning(f"Could not create ml_train index: {e}")

            conn.commit()
        finally:
            conn.close()

    @classmethod
    def sync_ticker_daily_10y(cls, ticker: str, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Incrementally synchronizes up to 10 years of daily data for a given ticker.
        Backfills older history if fewer than 2000 bars exist, and fetches recent missing deltas.
        """
        cls.init_schema()
        clean_ticker = ticker.strip().upper()
        if not clean_ticker.endswith(('.NS', '.BO', '^NSEI', '^INDIAVIX')):
            clean_ticker += '.NS'

        db_path = get_db_path()
        conn = sqlite3.connect(db_path, timeout=30.0)

        try:
            cur = conn.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM ohlcv WHERE ticker = ? AND (timeframe = '1d' OR timeframe IS NULL)", (clean_ticker,))
            row = cur.fetchone()
            min_date, max_date, count = (row[0], row[1], row[2]) if row else (None, None, 0)

            ten_yr_cutoff = (datetime.now() - timedelta(days=365 * 10)).strftime('%Y-%m-%d')
            needs_full_backfill = bool(count < 2000 or not min_date or min_date > ten_yr_cutoff)

            if needs_full_backfill or force_refresh:
                logger.info(f"Full 10-year sync for {clean_ticker}...")
                raw_df = yf.download(clean_ticker, period="10y", interval="1d", progress=False)
            else:
                last_dt = pd.to_datetime(max_date)
                if (datetime.now() - last_dt).days < 2:
                    return {
                        "ticker": clean_ticker,
                        "status": "UP_TO_DATE",
                        "rows_synced": 0,
                        "total_rows": count,
                        "latest_date": max_date
                    }
                start_date = (last_dt - timedelta(days=5)).strftime('%Y-%m-%d')
                logger.info(f"Incremental sync for {clean_ticker} from {start_date}...")
                raw_df = yf.download(clean_ticker, start=start_date, interval="1d", progress=False)

            if raw_df is None or raw_df.empty:
                return {
                    "ticker": clean_ticker,
                    "status": "NO_DATA",
                    "rows_synced": 0,
                    "total_rows": count or 0,
                    "error": "Provider returned empty dataset"
                }

            # Normalize columns
            if isinstance(raw_df.columns, pd.MultiIndex):
                raw_df.columns = [col[0].lower() if isinstance(col, tuple) else str(col).lower() for col in raw_df.columns]
            else:
                raw_df.columns = [str(c).lower() for c in raw_df.columns]

            raw_df = raw_df.rename(columns={'open': 'open', 'high': 'high', 'low': 'low', 'close': 'close', 'volume': 'volume'})
            
            # Reset index to extract date
            raw_df = raw_df.reset_index()
            date_col = raw_df.columns[0]
            raw_df['date'] = pd.to_datetime(raw_df[date_col]).dt.strftime('%Y-%m-%d')

            # Clean and validate
            raw_df = raw_df[['date', 'open', 'high', 'low', 'close', 'volume']].dropna(subset=['open', 'high', 'low', 'close'])
            raw_df['ticker'] = clean_ticker
            raw_df['source'] = 'yfinance'
            raw_df['hoard_timestamp'] = datetime.now().isoformat()
            raw_df['timeframe'] = '1d'

            # Atomic Upsert into SQLite
            raw_df.to_sql('temp_sync_ohlcv', conn, if_exists='replace', index=False)
            conn.execute("""
                INSERT OR REPLACE INTO ohlcv (ticker, date, open, high, low, close, volume, source, hoard_timestamp, timeframe)
                SELECT ticker, date, open, high, low, close, volume, source, hoard_timestamp, timeframe FROM temp_sync_ohlcv
            """)
            conn.commit()

            # Invalidate in-memory feature cache for updated ticker
            cache_key = f"{clean_ticker}_1d"
            if cache_key in _FEATURE_CACHE:
                del _FEATURE_CACHE[cache_key]

            # Get final count
            final_count = conn.execute("SELECT COUNT(*) FROM ohlcv WHERE ticker = ? AND (timeframe = '1d' OR timeframe IS NULL)", (clean_ticker,)).fetchone()[0]
            new_max_date = conn.execute("SELECT MAX(date) FROM ohlcv WHERE ticker = ? AND (timeframe = '1d' OR timeframe IS NULL)", (clean_ticker,)).fetchone()[0]

            return {
                "ticker": clean_ticker,
                "status": "SYNCED",
                "rows_synced": len(raw_df),
                "total_rows": final_count,
                "latest_date": new_max_date
            }

        except Exception as e:
            logger.error(f"Error syncing {clean_ticker}: {e}")
            return {
                "ticker": clean_ticker,
                "status": "ERROR",
                "error": str(e)
            }
        finally:
            conn.close()

    @classmethod
    def get_historical_ohlcv(cls, ticker: str, timeframe: str = "1d",
                              start_date: Optional[str] = None, end_date: Optional[str] = None) -> pd.DataFrame:
        """
        Retrieves point-in-time historical OHLCV from SQLite cache.
        If cache is empty or incomplete, automatically attempts a transparent sync.
        """
        cls.init_schema()
        clean_ticker = ticker.strip().upper()
        if not clean_ticker.endswith(('.NS', '.BO', '^NSEI', '^INDIAVIX')):
            clean_ticker += '.NS'

        db_path = get_db_path()
        conn = sqlite3.connect(db_path, timeout=30.0)

        try:
            query = "SELECT date, open, high, low, close, volume, source FROM ohlcv WHERE ticker = ? AND (timeframe = ? OR timeframe IS NULL)"
            params = [clean_ticker, timeframe]

            if start_date:
                query += " AND date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND date <= ?"
                params.append(end_date)

            query += " ORDER BY date ASC"

            df = pd.read_sql_query(query, conn, params=params)
            
            if (df.empty or len(df) < 300) and timeframe == "1d":
                conn.close()
                cls.sync_ticker_daily_10y(clean_ticker)
                conn = sqlite3.connect(db_path, timeout=30.0)
                df = pd.read_sql_query(query, conn, params=params)

            if not df.empty:
                df['date'] = pd.to_datetime(df['date'])
                df.set_index('date', inplace=True)
                df.attrs['source'] = 'SQLite Historical Layer'

            return df

        finally:
            conn.close()

    @classmethod
    def get_cached_features(cls, ticker: str, timeframe: str = "1d") -> pd.DataFrame:
        """
        Returns precomputed technical feature matrix from in-memory cache.
        If missing, computes indicators once and stores them in memory.
        """
        clean_t = ticker.strip().upper()
        cache_key = f"{clean_t}_{timeframe}"
        if cache_key in _FEATURE_CACHE:
            return _FEATURE_CACHE[cache_key].copy()

        df = cls.get_historical_ohlcv(clean_t, timeframe=timeframe)
        if df.empty or len(df) < 100:
            return pd.DataFrame()

        df = df.copy()
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        macd = ta.trend.MACD(df['close'])
        df['macd'] = macd.macd()
        df['macd_diff'] = macd.macd_diff()
        df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
        df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
        df['returns'] = df['close'].pct_change()
        df['macro_ema'] = df['close'].ewm(span=20, adjust=False).mean()
        df['macro_bullish'] = df['close'] >= df['macro_ema']

        df['future_5d'] = df['close'].shift(-5)
        df['target'] = (((df['future_5d'] - df['close']) / df['close']) > 0.0).astype(int)

        clean = df.dropna(subset=['rsi', 'macd', 'macd_diff', 'adx', 'atr', 'target', 'macro_ema']).copy()
        _FEATURE_CACHE[cache_key] = clean
        return clean.copy()

    @classmethod
    def get_historical_coverage_report(cls, universe: List[str]) -> Dict[str, Any]:
        """
        Generates a comprehensive data-quality and coverage report across the requested universe.
        Audits start date, end date, total bars, missing bars, and 10-year depth percentage.
        """
        cls.init_schema()
        db_path = get_db_path()
        conn = sqlite3.connect(db_path, timeout=30.0)

        report_rows = []
        ten_year_cutoff = (datetime.now() - timedelta(days=365 * 10)).strftime('%Y-%m-%d')
        stocks_with_10y = 0
        total_daily_bars = 0

        try:
            for ticker in universe:
                clean_t = ticker.strip().upper()
                if not clean_t.endswith(('.NS', '.BO', '^NSEI', '^INDIAVIX')):
                    clean_t += '.NS'

                cur = conn.execute("""
                    SELECT MIN(date), MAX(date), COUNT(*) 
                    FROM ohlcv 
                    WHERE ticker = ? AND (timeframe = '1d' OR timeframe IS NULL)
                """, (clean_t,))
                row = cur.fetchone()

                min_d = row[0] if row else None
                max_d = row[1] if row else None
                count = row[2] if row else 0
                total_daily_bars += count

                has_10y = bool(min_d and min_d <= ten_year_cutoff and count >= 2000)
                if has_10y:
                    stocks_with_10y += 1

                quality_status = "VALID" if count >= 300 else ("INSUFFICIENT" if count > 0 else "UNAVAILABLE")

                report_rows.append({
                    "ticker": clean_t,
                    "first_available_date": min_d or "N/A",
                    "last_available_date": max_d or "N/A",
                    "total_daily_bars": count,
                    "has_10y_history": has_10y,
                    "quality_status": quality_status
                })

            universe_size = len(universe)
            coverage_pct = round((stocks_with_10y / universe_size * 100.0), 1) if universe_size > 0 else 0.0

            return {
                "universe_size": universe_size,
                "stocks_with_10y": stocks_with_10y,
                "coverage_pct_10y": coverage_pct,
                "total_daily_bars_stored": total_daily_bars,
                "ten_year_reference_date": ten_year_cutoff,
                "tickers_detail": report_rows
            }
        finally:
            conn.close()

    @classmethod
    def get_intraday_accumulated_report(cls) -> Dict[str, Any]:
        """Audits accumulated 15-minute observations in SQLite."""
        cls.init_schema()
        db_path = get_db_path()
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            cur = conn.execute("""
                SELECT COUNT(*), COUNT(DISTINCT ticker), MIN(datetime), MAX(datetime)
                FROM ml_training_data
            """)
            row = cur.fetchone()
            total_candles, distinct_tickers, min_dt, max_dt = row[0], row[1], row[2], row[3]

            days_span = 0
            if min_dt and max_dt:
                try:
                    d_start = pd.to_datetime(min_dt)
                    d_end = pd.to_datetime(max_dt)
                    days_span = (d_end - d_start).days
                except Exception:
                    days_span = 0

            return {
                "total_15m_candles": total_candles or 0,
                "distinct_tickers": distinct_tickers or 0,
                "oldest_candle": min_dt or "N/A",
                "newest_candle": max_dt or "N/A",
                "calendar_days_span": days_span,
                "source_mode": "Real-time DataHoarder SQLite Accumulator (No Synthetic Fabrication)"
            }
        finally:
            conn.close()
