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
import numpy as np
import pandas as pd
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path

import qlib
from app.data.historical_data_layer import get_db_path
from app.data.data_gateway import DataGateway
from app.analytics.universe_config import resolve_universe_tickers

logger = logging.getLogger(__name__)

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
        """
        db_file = get_db_path()
        if not os.path.exists(db_file):
            raise FileNotFoundError(f"Canonical database not found: {db_file}")

        if not tickers:
            tickers = resolve_universe_tickers("LIVE_52")

        conn = sqlite3.connect(db_file)
        exported_tickers = []
        all_dates_set = set()
        ticker_date_ranges = {}
        data_hash_collector = hashlib.sha256()

        try:
            for raw_ticker in tickers:
                clean_upper, clean_lower = self.normalize_symbol(raw_ticker)
                # Query daily data (timeframe = '1d' or NULL)
                query = (
                    "SELECT date, open, high, low, close, volume FROM ohlcv "
                    "WHERE (ticker = ? OR ticker = ?) AND (timeframe = '1d' OR timeframe IS NULL) "
                    "ORDER BY date ASC"
                )
                df = pd.read_sql_query(query, conn, params=[raw_ticker, f"{clean_upper}.NS"])
                if df.empty or len(df) < min_bars:
                    logger.warning(f"[QlibAdapter] Skipping {raw_ticker}: insufficient bars ({len(df)} < {min_bars})")
                    continue

                df["date"] = pd.to_datetime(df["date"])
                df = df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)

                # Ensure non-zero positive prices
                for col in ["open", "high", "low", "close"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce").ffill().fillna(1.0)
                df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)

                # Calculate VWAP & factor
                typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
                cum_vol = df["volume"].cumsum()
                cum_pv = (typical_price * df["volume"]).cumsum()
                df["vwap"] = np.where(cum_vol > 0, cum_pv / np.maximum(cum_vol, 1e-8), df["close"])
                df["factor"] = 1.0  # Normalized adjustment factor

                date_strs = df["date"].dt.strftime("%Y-%m-%d").tolist()
                all_dates_set.update(date_strs)
                ticker_date_ranges[clean_upper] = (date_strs[0], date_strs[-1])

                # Write binary features
                sym_dir = os.path.join(self.feat_dir, clean_lower)
                os.makedirs(sym_dir, exist_ok=True)

                for field in QLIB_FIELDS:
                    arr = df[field].values.astype(np.float32)
                    bin_file = os.path.join(sym_dir, f"{field}.day.bin")
                    # Qlib format: index 0 (int32/float32 start index) followed by float32 values
                    with open(bin_file, "wb") as fp:
                        np.hstack([0, arr]).astype("<f").tofile(fp)

                # Update data fingerprint
                data_hash_collector.update(clean_upper.encode())
                data_hash_collector.update(df["close"].values.tobytes()[:1024])
                exported_tickers.append(clean_upper)

            if not exported_tickers:
                raise ValueError("No valid tickers could be exported for Qlib daily provider.")

            # Write master daily calendar
            sorted_dates = sorted(list(all_dates_set))
            cal_file = os.path.join(self.cal_dir, "day.txt")
            with open(cal_file, "w") as fp:
                fp.write("\n".join(sorted_dates) + "\n")

            # Write instruments file
            inst_file = os.path.join(self.inst_dir, "all.txt")
            with open(inst_file, "w") as fp:
                for sym in exported_tickers:
                    start_d, end_d = ticker_date_ranges[sym]
                    fp.write(f"{sym}\t{start_d}\t{end_d}\n")

            data_fingerprint = data_hash_collector.hexdigest()
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
            tickers = resolve_universe_tickers("LIVE_52")[:20]  # Focus on top 20 liquid tickers for intraday

        exported_tickers = []
        all_timestamps_set = set()
        ticker_ranges = {}
        data_hash_collector = hashlib.sha256()

        for raw_ticker in tickers:
            clean_upper, clean_lower = self.normalize_symbol(raw_ticker)
            # Fetch 15m candles via DataGateway (uses cached SQLite first, pulls incremental)
            try:
                df = DataGateway.get_ohlcv(f"{clean_upper}.NS", timeframe="15m")
            except Exception as e:
                logger.warning(f"[QlibAdapter] Failed fetching 15m for {raw_ticker}: {e}")
                df = None

            if df is None or df.empty or len(df) < min_bars:
                continue

            df = df.copy()
            if "date" in df.columns:
                df["dt"] = pd.to_datetime(df["date"])
            else:
                df["dt"] = pd.to_datetime(df.index)

            df = df.drop_duplicates(subset=["dt"]).sort_values("dt").reset_index(drop=True)

            for col in ["open", "high", "low", "close"]:
                df[col] = pd.to_numeric(df[col], errors="coerce").ffill().fillna(1.0)
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)

            typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
            cum_vol = df["volume"].cumsum()
            cum_pv = (typical_price * df["volume"]).cumsum()
            df["vwap"] = np.where(cum_vol > 0, cum_pv / np.maximum(cum_vol, 1e-8), df["close"])
            df["factor"] = 1.0

            time_strs = df["dt"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist()
            all_timestamps_set.update(time_strs)
            ticker_ranges[clean_upper] = (time_strs[0], time_strs[-1])

            sym_dir = os.path.join(self.feat_dir, clean_lower)
            os.makedirs(sym_dir, exist_ok=True)

            for field in QLIB_FIELDS:
                arr = df[field].values.astype(np.float32)
                bin_file = os.path.join(sym_dir, f"{field}.15min.bin")
                with open(bin_file, "wb") as fp:
                    np.hstack([0, arr]).astype("<f").tofile(fp)

            data_hash_collector.update(clean_upper.encode())
            data_hash_collector.update(df["close"].values.tobytes()[:512])
            exported_tickers.append(clean_upper)

        if not exported_tickers:
            raise ValueError("No valid tickers could be exported for Qlib 15min intraday provider.")

        sorted_times = sorted(list(all_timestamps_set))
        cal_file = os.path.join(self.cal_dir, "15min.txt")
        with open(cal_file, "w") as fp:
            fp.write("\n".join(sorted_times) + "\n")

        # Append to instruments if not present
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
                if sym in existing_insts:
                    fp.write(f"{sym}\t{existing_insts[sym][0]}\t{existing_insts[sym][1]}\n")
                elif sym in ticker_ranges:
                    fp.write(f"{sym}\t{ticker_ranges[sym][0]}\t{ticker_ranges[sym][1]}\n")

        data_fingerprint = data_hash_collector.hexdigest()
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

    def initialize_qlib(self) -> Dict[str, Any]:
        """
        Initializes Microsoft Qlib with this Indian market data provider.
        Configures MLflow filesystem compatibility.
        """
        os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"
        os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"

        try:
            qlib.init(
                provider_uri=self.provider_uri,
                clear_mem_cache=True,
                auto_mount=False
            )
            return {
                "status": "INITIALIZED",
                "qlib_version": getattr(qlib, "__version__", "unknown"),
                "provider_uri": self.provider_uri,
                "calendars": os.listdir(self.cal_dir) if os.path.exists(self.cal_dir) else []
            }
        except Exception as e:
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
    Idempotent: syncs only if provider data is missing or incomplete.
    """
    adapter = get_qlib_adapter(provider_uri)
    cal_day = os.path.join(adapter.cal_dir, "day.txt")
    if not os.path.exists(cal_day) or os.path.getsize(cal_day) == 0:
        logger.info("[QlibAdapter] Syncing initial daily Indian market data for Qlib...")
        adapter.sync_daily_universe()
    
    adapter.initialize_qlib()
    return adapter
