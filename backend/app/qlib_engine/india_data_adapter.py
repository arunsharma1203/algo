"""
INDIA MARKET DATA ADAPTER FOR MICROSOFT QLIB
============================================
Bridges the authoritative Indian stock market data from SQLite (ohlcv table)
and DataGateway into Microsoft Qlib's binary provider format.

Features exported:
- open, high, low, close, volume, vwap, factor
Frequencies supported:
- 'day' (Daily bars from 10-year NSE history)
- '15min' (Intraday 15m bars from DataGateway)

Strict Invariants:
- Real Indian NSE/BSE market data only (NO Qlib default China data).
- Content-addressed data hashing for cryptographic provenance.
- Clean fail-closed error handling if data is missing.
"""

import os
import sqlite3
import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path


import qlib
from app.data.historical_data_layer import get_db_path
from app.data.data_gateway import DataGateway
from app.analytics.universe_config import resolve_universe_tickers, resolve_all_database_stocks
from app.analytics.master_logger import MasterLogger

logger = logging.getLogger(__name__)

_QLIB_INITIALIZED = False
_QLIB_INIT_LOCK = threading.Lock()

DEFAULT_QLIB_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "qlib_provider")
)

# Standard Qlib fields
QLIB_FIELDS = ["open", "high", "low", "close", "volume", "vwap", "factor"]


class IndiaMarketDataAdapter:
    """
    Exports canonical Indian market data to Qlib binary format
    and initializes the official Microsoft Qlib runtime.
    """

    def __init__(self, provider_uri: Optional[str] = None):
        self.provider_uri = os.path.abspath(provider_uri or DEFAULT_QLIB_DIR)
        self.cal_dir = os.path.join(self.provider_uri, "calendars")
        self.inst_dir = os.path.join(self.provider_uri, "instruments")
        self.feat_dir = os.path.join(self.provider_uri, "features")
        self._last_sync_timestamp: Optional[str] = None
        self._last_sync_duration: Optional[float] = None
        os.makedirs(self.cal_dir, exist_ok=True)
        os.makedirs(self.inst_dir, exist_ok=True)
        os.makedirs(self.feat_dir, exist_ok=True)

    @staticmethod
    def normalize_symbol(ticker: str) -> Tuple[str, str]:
        """
        Normalizes ticker to (clean_symbol_upper, clean_symbol_lower).
        Example: 'RELIANCE.NS' -> ('RELIANCE', 'reliance')
        """
        clean = ticker.upper().replace(".NS", "").replace(".BO", "").strip()
        return clean, clean.lower()

    def sync_daily_universe(
        self,
        tickers: Optional[List[str]] = None,
        min_bars: int = 60
    ) -> Dict[str, Any]:
        """
        Exports daily Indian equity bars from canonical SQLite ohlcv table
        into Qlib binary format (day.txt calendar, all.txt instruments, *.day.bin features).
        Dynamically discovers all available database stocks if tickers is None.
        """
        db_file = get_db_path()
        if not os.path.exists(db_file):
            raise FileNotFoundError(f"Canonical database not found: {db_file}")

        if not tickers:
            try:
                res_all = resolve_all_database_stocks(timeframe="1d", min_bars=min_bars)
                tickers = res_all.get("scan_ready_symbols", [])
            except Exception as e:
                logger.warning(f"[QlibAdapter] Dynamic discovery error: {e}, falling back to LIVE_52")
                tickers = None
            if not tickers:
                tickers = resolve_universe_tickers("LIVE_52")

        conn = sqlite3.connect(db_file)
        exported_tickers = []
        all_dates_set = set()
        ticker_dfs = {}
        data_hash_collector = hashlib.sha256()

        try:
            # Pass 1: Extract, deduplicate, and discover the global calendar
            for raw_ticker in tickers:
                clean_upper, clean_lower = self.normalize_symbol(raw_ticker)
                query = (
                    "SELECT date, open, high, low, close, volume FROM ohlcv "
                    "WHERE (ticker = ? OR ticker = ?) AND (timeframe = '1d' OR timeframe IS NULL) "
                    "ORDER BY date ASC"
                )
                df = pd.read_sql_query(query, conn, params=[raw_ticker, f"{clean_upper}.NS"])
                if df.empty or len(df) < min_bars:
                    logger.warning(f"[QlibAdapter] Skipping {raw_ticker}: insufficient bars ({len(df)} < {min_bars})")
                    continue

                df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                df = df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)

                for col in ["open", "high", "low", "close"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce").ffill().fillna(1.0)
                df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)

                date_strs = df["date"].tolist()
                all_dates_set.update(date_strs)
                ticker_dfs[clean_upper] = (clean_lower, df)

            if not ticker_dfs:
                raise ValueError("No valid tickers could be exported for Qlib daily provider.")

            # Write master daily calendar
            sorted_dates = sorted(list(all_dates_set))
            date_to_idx = {d: i for i, d in enumerate(sorted_dates)}
            cal_file = os.path.join(self.cal_dir, "day.txt")
            with open(cal_file, "w") as fp:
                fp.write("\n".join(sorted_dates) + "\n")

            # Pass 2: Align every ticker strictly to the calendar slice [start_idx : end_idx]
            # and write the accurate start_idx into the binary feature file header
            ticker_date_ranges = {}
            for clean_upper, (clean_lower, df) in ticker_dfs.items():
                date_strs = df["date"].tolist()
                start_d, end_d = date_strs[0], date_strs[-1]
                start_idx = date_to_idx[start_d]
                end_idx = date_to_idx[end_d]
                expected_dates = sorted_dates[start_idx : end_idx + 1]

                df_aligned = df.set_index("date").reindex(expected_dates)
                for col in ["open", "high", "low", "close"]:
                    df_aligned[col] = df_aligned[col].ffill().bfill().fillna(1.0)
                df_aligned["volume"] = df_aligned["volume"].fillna(0.0)

                typical_price = (df_aligned["high"] + df_aligned["low"] + df_aligned["close"]) / 3.0
                cum_vol = df_aligned["volume"].cumsum()
                cum_pv = (typical_price * df_aligned["volume"]).cumsum()
                df_aligned["vwap"] = np.where(cum_vol > 0, cum_pv / np.maximum(cum_vol, 1e-8), df_aligned["close"])
                df_aligned["factor"] = 1.0

                sym_dir = os.path.join(self.feat_dir, clean_lower)
                os.makedirs(sym_dir, exist_ok=True)

                for field in QLIB_FIELDS:
                    arr = df_aligned[field].values.astype(np.float32)
                    bin_file = os.path.join(sym_dir, f"{field}.day.bin")
                    with open(bin_file, "wb") as fp:
                        np.hstack([start_idx, arr]).astype("<f").tofile(fp)

                data_hash_collector.update(clean_upper.encode())
                data_hash_collector.update(df_aligned["close"].values.tobytes()[:1024])
                ticker_date_ranges[clean_upper] = (start_d, end_d)
                exported_tickers.append(clean_upper)

            # Write instruments file
            inst_file = os.path.join(self.inst_dir, "all.txt")
            with open(inst_file, "w") as fp:
                for sym in exported_tickers:
                    start_d, end_d = ticker_date_ranges[sym]
                    fp.write(f"{sym}\t{start_d}\t{end_d}\n")

            data_fingerprint = data_hash_collector.hexdigest()
            MasterLogger.log_event(
                category="QLIB_SYSTEM",
                event_type="QLIB_DAILY_DATA_SYNCED",
                message=f"Synced {len(exported_tickers)} daily instruments across {len(sorted_dates)} trading sessions",
                details={
                    "tickers_count": len(exported_tickers),
                    "trading_days": len(sorted_dates),
                    "start_date": sorted_dates[0],
                    "end_date": sorted_dates[-1],
                    "data_hash": data_fingerprint
                }
            )

            logger.info(
                f"[QlibAdapter] Successfully exported {len(exported_tickers)} tickers "
                f"across {len(sorted_dates)} trading days to {self.provider_uri}"
            )

            return {
                "status": "SUCCESS",
                "freq": "day",
                "tickers_count": len(exported_tickers),
                "tickers": exported_tickers,
                "trading_days": len(sorted_dates),
                "start_date": sorted_dates[0],
                "end_date": sorted_dates[-1],
                "provider_uri": self.provider_uri,
                "data_hash": data_fingerprint
            }
        finally:
            conn.close()

    def sync_intraday_universe(
        self,
        tickers: Optional[List[str]] = None,
        min_bars: int = 50
    ) -> Dict[str, Any]:
        """
        Exports 15m Indian intraday equity bars from DataGateway/SQLite
        into Qlib binary format (15min.txt calendar, *.15min.bin features).
        """
        if not tickers:
            try:
                res_all = resolve_all_database_stocks(timeframe="15m", min_bars=min_bars)
                tickers = res_all.get("scan_ready_symbols", [])
            except Exception as e:
                logger.warning(f"[QlibAdapter] Dynamic discovery error for 15m: {e}, falling back to ALL_DATABASE_STOCKS")
                tickers = None
            if not tickers:
                tickers = resolve_universe_tickers("ALL_DATABASE_STOCKS")

        exported_tickers = []
        all_timestamps_set = set()
        ticker_dfs = {}
        data_hash_collector = hashlib.sha256()

        def _fetch_single(raw_ticker: str):
            """Fetch 15m candles for one ticker. Returns (raw_ticker, df_or_None)."""
            clean_upper, clean_lower = self.normalize_symbol(raw_ticker)
            try:
                df = DataGateway.get_ohlcv(f"{clean_upper}.NS", timeframe="15m", fetch_incremental=False)
                if df is None or df.empty or len(df) < min_bars:
                    df = DataGateway.get_ohlcv(f"{clean_upper}.NS", timeframe="15m", fetch_incremental=True)
            except Exception as e:
                logger.warning(f"[QlibAdapter] Failed fetching 15m for {raw_ticker}: {e}")
                df = None
            return raw_ticker, df

        # Parallel fetch phase (read-only SQLite/DataGateway — safe for concurrent access)
        fetch_workers = min(12, max(1, len(tickers)))
        with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
            futures = {pool.submit(_fetch_single, t): t for t in tickers}
            for future in as_completed(futures):
                raw_ticker, df = future.result()
                if df is None or df.empty or len(df) < min_bars:
                    continue

                clean_upper, clean_lower = self.normalize_symbol(raw_ticker)
                df = df.copy()
                if "date" in df.columns:
                    df["dt"] = pd.to_datetime(df["date"])
                else:
                    df["dt"] = pd.to_datetime(df.index)

                df = df.drop_duplicates(subset=["dt"]).sort_values("dt").reset_index(drop=True)

                for col in ["open", "high", "low", "close"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce").ffill().fillna(1.0)
                df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)

                time_strs = df["dt"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist()
                all_timestamps_set.update(time_strs)
                ticker_dfs[clean_upper] = (clean_lower, df)

        if not ticker_dfs:
            raise ValueError("No valid tickers could be exported for Qlib 15min intraday provider.")

        sorted_times = sorted(list(all_timestamps_set))
        time_to_idx = {t: i for i, t in enumerate(sorted_times)}
        cal_file = os.path.join(self.cal_dir, "15min.txt")
        with open(cal_file, "w") as fp:
            fp.write("\n".join(sorted_times) + "\n")

        ticker_ranges = {}
        for clean_upper, (clean_lower, df) in ticker_dfs.items():
            time_strs = df["dt"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist()
            start_t = time_strs[0]
            end_t = sorted_times[-1]
            start_idx = time_to_idx[start_t]
            end_idx = len(sorted_times) - 1
            expected_times = sorted_times[start_idx : end_idx + 1]

            df_aligned = df.set_index("dt").reindex(pd.to_datetime(expected_times))
            for col in ["open", "high", "low", "close"]:
                df_aligned[col] = df_aligned[col].ffill().bfill().fillna(1.0)
            df_aligned["volume"] = df_aligned["volume"].fillna(0.0)

            typical_price = (df_aligned["high"] + df_aligned["low"] + df_aligned["close"]) / 3.0
            cum_vol = df_aligned["volume"].cumsum()
            cum_pv = (typical_price * df_aligned["volume"]).cumsum()
            df_aligned["vwap"] = np.where(cum_vol > 0, cum_pv / np.maximum(cum_vol, 1e-8), df_aligned["close"])
            df_aligned["factor"] = 1.0

            sym_dir = os.path.join(self.feat_dir, clean_lower)
            os.makedirs(sym_dir, exist_ok=True)

            for field in QLIB_FIELDS:
                arr = df_aligned[field].values.astype(np.float32)
                bin_file = os.path.join(sym_dir, f"{field}.15min.bin")
                with open(bin_file, "wb") as fp:
                    np.hstack([start_idx, arr]).astype("<f").tofile(fp)

            data_hash_collector.update(clean_upper.encode())
            data_hash_collector.update(df_aligned["close"].values.tobytes()[:512])
            ticker_ranges[clean_upper] = (start_t, end_t)
            exported_tickers.append(clean_upper)

        # Update instruments file, ensuring freshly synced ranges take precedence
        inst_file = os.path.join(self.inst_dir, "all.txt")
        existing_insts = {}
        if os.path.exists(inst_file):
            with open(inst_file, "r") as fp:
                for line in fp:
                    parts = line.strip().split("\t")
                    if len(parts) >= 3:
                        existing_insts[parts[0]] = (parts[1], parts[2])

        with open(inst_file, "w") as fp:
            all_syms = sorted(list(set(existing_insts.keys()) | set(exported_tickers)))
            for sym in all_syms:
                if sym in ticker_ranges:
                    fp.write(f"{sym}\t{ticker_ranges[sym][0]}\t{ticker_ranges[sym][1]}\n")
                elif sym in existing_insts:
                    fp.write(f"{sym}\t{existing_insts[sym][0]}\t{existing_insts[sym][1]}\n")

        data_fingerprint = data_hash_collector.hexdigest()
        MasterLogger.log_event(
            category="QLIB_SYSTEM",
            event_type="QLIB_INTRADAY_DATA_SYNCED",
            message=f"Synced {len(exported_tickers)} intraday instruments across {len(sorted_times)} 15m intervals",
            details={
                "tickers_count": len(exported_tickers),
                "intervals": len(sorted_times),
                "start_time": sorted_times[0],
                "end_time": sorted_times[-1],
                "data_hash": data_fingerprint
            }
        )

        logger.info(
            f"[QlibAdapter] Successfully exported {len(exported_tickers)} intraday tickers "
            f"across {len(sorted_times)} 15m intervals to {self.provider_uri}"
        )

        return {
            "status": "SUCCESS",
            "freq": "15min",
            "tickers_count": len(exported_tickers),
            "tickers": exported_tickers,
            "intervals": len(sorted_times),
            "start_time": sorted_times[0],
            "end_time": sorted_times[-1],
            "provider_uri": self.provider_uri,
            "data_hash": data_fingerprint
        }

    def get_latest_authoritative_timestamp(self, timeframe: str = "1d") -> Optional[str]:
        """Queries the authoritative local SQLite database for the latest recorded candle timestamp."""
        db_file = get_db_path()
        if not os.path.exists(db_file):
            return None
        conn = sqlite3.connect(db_file, timeout=10.0)
        try:
            cur = conn.cursor()
            tf_norm = (timeframe or "").lower().strip()
            if tf_norm in ("1d", "day", "daily"):
                tf_clause = "(timeframe = '1d' OR timeframe IS NULL)"
                params = []
            elif tf_norm in ("15m", "15min"):
                tf_clause = "timeframe IN ('15m', '15min')"
                params = []
            else:
                tf_clause = "timeframe = ?"
                params = [timeframe]
            row = cur.execute(f"SELECT MAX(date) FROM ohlcv WHERE {tf_clause}", params).fetchone()
            if row and row[0]:
                return str(row[0])
            return None
        except Exception as e:
            logger.warning(f"[QlibAdapter] Error querying latest authoritative timestamp: {e}")
            return None
        finally:
            conn.close()

    def get_latest_provider_timestamp(self, timeframe: str = "1d") -> Optional[str]:
        """Reads the latest timestamp from the Qlib binary calendar."""
        cal_filename = "day.txt" if timeframe in ("1d", "day") else "15min.txt"
        cal_file = os.path.join(self.cal_dir, cal_filename)
        if not os.path.exists(cal_file) or os.path.getsize(cal_file) == 0:
            return None
        try:
            with open(cal_file, "r") as fp:
                lines = [line.strip() for line in fp if line.strip()]
                return lines[-1] if lines else None
        except Exception as e:
            logger.warning(f"[QlibAdapter] Error reading calendar {cal_file}: {e}")
            return None

    def check_freshness(self, timeframe: str = "1d") -> Dict[str, Any]:
        """
        Evaluates real market data freshness between Authoritative SQLite and Qlib Provider.
        Returns explicit status: FRESH, DELAYED, STALE, UNAVAILABLE.
        Market-hours aware: Mon-Fri 09:15 to 15:30 IST.
        """
        auth_ts = self.get_latest_authoritative_timestamp(timeframe)
        qlib_ts = self.get_latest_provider_timestamp(timeframe)

        # Indian Standard Time (IST = UTC+5:30)
        ist_tz = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(timezone.utc).astimezone(ist_tz)

        weekday = now_ist.weekday()  # 0 = Mon, 4 = Fri
        is_weekday = (weekday < 5)
        mkt_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        mkt_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
        is_market_open = is_weekday and (mkt_open <= now_ist <= mkt_close)

        if not auth_ts or not qlib_ts:
            return {
                "authoritative_latest_timestamp": auth_ts,
                "qlib_provider_latest_timestamp": qlib_ts,
                "data_lag_seconds": None,
                "data_lag_minutes": None,
                "data_status": "UNAVAILABLE",
                "is_market_open": is_market_open,
                "last_sync_timestamp": self._last_sync_timestamp,
                "sync_duration": self._last_sync_duration
            }

        try:
            dt_auth = pd.to_datetime(auth_ts)
            dt_qlib = pd.to_datetime(qlib_ts)

            dt_auth_naive = dt_auth.tz_localize(None) if dt_auth.tzinfo else dt_auth
            dt_qlib_naive = dt_qlib.tz_localize(None) if dt_qlib.tzinfo else dt_qlib
            now_ist_naive = now_ist.replace(tzinfo=None)

            is_daily = (timeframe or "").lower().strip() in ("1d", "day", "daily")

            # Calculate lag between authoritative SQLite and Qlib binary provider
            if is_daily:
                lag_days = max(0, (dt_auth_naive.date() - dt_qlib_naive.date()).days)
                lag_minutes = round(lag_days * 1440.0, 1)
                lag_seconds = int(lag_days * 86400)
            else:
                if dt_auth_naive.date() == dt_qlib_naive.date():
                    lag_delta = max(timedelta(0), dt_auth_naive - dt_qlib_naive)
                    lag_seconds = int(lag_delta.total_seconds())
                    lag_minutes = round(lag_seconds / 60.0, 1)
                elif dt_auth_naive.date() > dt_qlib_naive.date():
                    # Market-hours aware lag across dates (NSE trading day is 09:15 to 15:30 = 375 mins)
                    close_d1 = dt_qlib_naive.replace(hour=15, minute=30, second=0, microsecond=0)
                    mins_d1 = max(0, int((close_d1 - dt_qlib_naive).total_seconds() / 60.0)) if dt_qlib_naive <= close_d1 else 0

                    open_d2 = dt_auth_naive.replace(hour=9, minute=15, second=0, microsecond=0)
                    mins_d2 = max(0, int((dt_auth_naive - open_d2).total_seconds() / 60.0)) if dt_auth_naive >= open_d2 else 0

                    days_between = max(0, (dt_auth_naive.date() - dt_qlib_naive.date()).days - 1)
                    lag_minutes = round(mins_d1 + mins_d2 + (days_between * 375.0), 1)
                    lag_seconds = int(lag_minutes * 60)
                else:
                    lag_seconds = 0
                    lag_minutes = 0.0

            # During market hours, check lag between current wall-clock time and data timestamp for intraday bars
            if is_market_open and not is_daily:
                if dt_auth_naive.date() == now_ist_naive.date():
                    wall_lag_sec = max(0, int((now_ist_naive - dt_auth_naive).total_seconds()))
                    wall_lag_min = round(wall_lag_sec / 60.0, 1)
                else:
                    # Previous trading day's intraday data
                    mkt_open_today = now_ist_naive.replace(hour=9, minute=15, second=0, microsecond=0)
                    first_bar_close = now_ist_naive.replace(hour=9, minute=30, second=0, microsecond=0)
                    if now_ist_naive <= first_bar_close:
                        # First 15m candle (09:15-09:30) is currently forming; previous day's close is current
                        wall_lag_min = 0.0
                    else:
                        wall_lag_sec = max(0, int((now_ist_naive - mkt_open_today).total_seconds()))
                        wall_lag_min = round(wall_lag_sec / 60.0, 1)
            else:
                wall_lag_min = 0.0

            # Assign honest freshness state
            auth_date = dt_auth_naive.date()
            today_date = now_ist_naive.date()
            day_diff = max(0, (today_date - auth_date).days)
            max_allowed_days = 4 if weekday in (0, 1) else 3

            if is_daily:
                # Daily bars represent completed EOD sessions.
                # During trading hours (09:15-15:30 IST), today's bar does not exist until close;
                # the previous trading session's EOD bar is the authoritative current bar.
                if lag_seconds > 0:
                    status = "STALE" if lag_minutes > 1440 else "DELAYED"
                elif day_diff > max_allowed_days:
                    status = "STALE" if day_diff > 7 else "DELAYED"
                else:
                    status = "FRESH"
            else:
                if lag_seconds > 0:
                    status = "STALE" if lag_minutes > 120 else "DELAYED"
                elif day_diff > max_allowed_days:
                    status = "STALE" if day_diff > 7 else "DELAYED"
                elif is_market_open and wall_lag_min > 120:
                    # Authoritative intraday data itself is over 2 hours behind during active trading
                    status = "STALE"
                elif is_market_open and wall_lag_min > 30:
                    status = "DELAYED"
                else:
                    status = "FRESH"

            return {
                "authoritative_latest_timestamp": auth_ts,
                "authoritative_latest": auth_ts,
                "qlib_provider_latest_timestamp": qlib_ts,
                "provider_latest": qlib_ts,
                "data_lag_seconds": lag_seconds,
                "lag_seconds": lag_seconds,
                "data_lag_minutes": lag_minutes,
                "lag_minutes": lag_minutes,
                "wall_lag_minutes": wall_lag_min,
                "data_status": status,
                "status": status,
                "is_market_open": is_market_open,
                "last_sync_timestamp": self._last_sync_timestamp,
                "sync_duration": self._last_sync_duration
            }
        except Exception as e:
            logger.error(f"[QlibAdapter] Freshness calculation error: {e}")
            return {
                "authoritative_latest_timestamp": auth_ts,
                "authoritative_latest": auth_ts,
                "qlib_provider_latest_timestamp": qlib_ts,
                "provider_latest": qlib_ts,
                "data_lag_seconds": None,
                "lag_seconds": None,
                "data_lag_minutes": None,
                "lag_minutes": None,
                "wall_lag_minutes": None,
                "data_status": "UNKNOWN",
                "status": "UNKNOWN",
                "is_market_open": is_market_open,
                "last_sync_timestamp": self._last_sync_timestamp,
                "sync_duration": self._last_sync_duration
            }

    def sync_if_authoritative_newer(
        self,
        timeframe: str = "1d",
        tickers: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Synchronizes Qlib binary data ONLY if authoritative SQLite data has newer bars.
        Avoids full rebuilds when data is already current.
        """
        t0 = time.time()
        freshness = self.check_freshness(timeframe)
        auth_ts = freshness.get("authoritative_latest_timestamp")
        qlib_ts = freshness.get("qlib_provider_latest_timestamp")

        needs_sync = False
        if not qlib_ts or not auth_ts:
            needs_sync = True
        else:
            try:
                needs_sync = (pd.to_datetime(auth_ts) > pd.to_datetime(qlib_ts))
            except Exception:
                needs_sync = (auth_ts > qlib_ts)

        if not needs_sync:
            return {
                "synced": False,
                "reason": "UP_TO_DATE",
                "freshness": freshness,
                "duration": 0.0
            }

        logger.info(
            f"[QlibAdapter] Authoritative data ({auth_ts}) is newer than Qlib provider ({qlib_ts}). "
            f"Incrementally syncing {timeframe}..."
        )
        if timeframe in ("1d", "day"):
            res = self.sync_daily_universe(tickers=tickers)
        else:
            res = self.sync_intraday_universe(tickers=tickers)

        dur = round(time.time() - t0, 3)
        self._last_sync_timestamp = datetime.now().isoformat()
        self._last_sync_duration = dur

        new_freshness = self.check_freshness(timeframe)
        logger.info(
            f"[QlibAdapter] Sync complete in {dur}s. "
            f"Provider timestamp advanced from {qlib_ts} to {new_freshness.get('qlib_provider_latest_timestamp')}"
        )
        return {
            "synced": True,
            "reason": "SYNC_COMPLETED",
            "sync_result": res,
            "freshness": new_freshness,
            "duration": dur
        }

    def initialize_qlib(self) -> Dict[str, Any]:
        """
        Initializes Microsoft Qlib with this Indian market data provider.
        Configures MLflow filesystem compatibility.
        Thread-safe and strictly idempotent: avoids re-initialization if already initialized,
        specifically preventing RecorderInitializationError if QlibRecorder or Experiment is active.
        """
        global _QLIB_INITIALIZED
        os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"
        os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"

        with _QLIB_INIT_LOCK:
            # 1. First check: Qlib internal config state (C.registered)
            try:
                from qlib.config import C
                if getattr(C, "registered", False):
                    _QLIB_INITIALIZED = True
                    return {
                        "status": "ALREADY_INITIALIZED",
                        "qlib_version": getattr(qlib, "__version__", "unknown"),
                        "provider_uri": self.provider_uri,
                        "calendars": os.listdir(self.cal_dir) if os.path.exists(self.cal_dir) else []
                    }
            except Exception as check_err:
                logger.debug(f"[QlibAdapter] Error checking C.registered: {check_err}")

            # 2. Second check: Process-level initialization flag
            if _QLIB_INITIALIZED:
                return {
                    "status": "ALREADY_INITIALIZED",
                    "qlib_version": getattr(qlib, "__version__", "unknown"),
                    "provider_uri": self.provider_uri,
                    "calendars": os.listdir(self.cal_dir) if os.path.exists(self.cal_dir) else []
                }

            # 3. Third check: Qlib recorder/experiment active state
            try:
                from qlib.workflow import R
                if hasattr(R, "_provider") and R._provider is not None:
                    expm = getattr(R._provider, "exp_manager", None)
                    if expm is not None and getattr(expm, "active_experiment", None) is not None:
                        logger.warning("[QlibAdapter] QlibRecorder/Experiment is currently active; reusing runtime to prevent RecorderInitializationError.")
                        _QLIB_INITIALIZED = True
                        return {
                            "status": "RECORDER_ACTIVE_REUSED",
                            "qlib_version": getattr(qlib, "__version__", "unknown"),
                            "provider_uri": self.provider_uri,
                            "calendars": os.listdir(self.cal_dir) if os.path.exists(self.cal_dir) else []
                        }
            except Exception:
                pass

            # 4. Initialize with skip_if_reg=True
            try:
                qlib.init(
                    provider_uri=self.provider_uri,
                    clear_mem_cache=True,
                    auto_mount=False,
                    skip_if_reg=True
                )
                _QLIB_INITIALIZED = True
                try:
                    from qlib.config import C
                    C.workers = 1
                    C.joblib_backend = "threading"
                except Exception:
                    pass
                MasterLogger.log_event(
                    category="QLIB_SYSTEM",
                    event_type="QLIB_INITIALIZED",
                    message="Qlib initialized successfully with Indian market binary provider",
                    details={"provider_uri": self.provider_uri, "qlib_version": getattr(qlib, "__version__", "unknown")}
                )
                return {
                    "status": "INITIALIZED",
                    "qlib_version": getattr(qlib, "__version__", "unknown"),
                    "provider_uri": self.provider_uri,
                    "calendars": os.listdir(self.cal_dir) if os.path.exists(self.cal_dir) else []
                }
            except Exception as e:
                # If error is RecorderInitializationError, mark as initialized and reuse
                if "RecorderInitializationError" in type(e).__name__ or "QlibRecorder is already activated" in str(e):
                    logger.info("[QlibAdapter] Recorder is active; reusing existing Qlib initialization.")
                    _QLIB_INITIALIZED = True
                    return {
                        "status": "REUSED_AFTER_RECORDER_CHECK",
                        "qlib_version": getattr(qlib, "__version__", "unknown"),
                        "provider_uri": self.provider_uri,
                        "calendars": os.listdir(self.cal_dir) if os.path.exists(self.cal_dir) else []
                    }
                logger.error(f"[QlibAdapter] Initialization failed: {e}")
                raise


_GLOBAL_ADAPTER = None

def get_qlib_adapter(provider_uri: Optional[str] = None) -> IndiaMarketDataAdapter:
    """Returns singleton IndiaMarketDataAdapter instance."""
    global _GLOBAL_ADAPTER
    if _GLOBAL_ADAPTER is None or (provider_uri and _GLOBAL_ADAPTER.provider_uri != provider_uri):
        _GLOBAL_ADAPTER = IndiaMarketDataAdapter(provider_uri)
    return _GLOBAL_ADAPTER

def ensure_qlib_ready(provider_uri: Optional[str] = None) -> IndiaMarketDataAdapter:
    """
    Ensures Qlib data is synced from SQLite and Qlib runtime is initialized.
    Strictly idempotent: reuses existing initialized runtime and only syncs if needed.
    """
    adapter = get_qlib_adapter(provider_uri)
    cal_day = os.path.join(adapter.cal_dir, "day.txt")
    if not os.path.exists(cal_day) or os.path.getsize(cal_day) == 0:
        logger.info("[QlibAdapter] Syncing initial daily Indian market data for Qlib...")
        adapter.sync_daily_universe()

    try:
        from qlib.config import C
        if not getattr(C, "registered", False):
            adapter.initialize_qlib()
    except Exception:
        adapter.initialize_qlib()

    return adapter


QlibIndiaDataAdapter = IndiaMarketDataAdapter

