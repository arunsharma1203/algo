"""
QLIB DATASET HEALTH & UNIVERSE INSPECTOR
========================================
Authoritative inspector for database health, universe eligibility,
bar depth validation, missingness estimation, and Qlib provider synchronization.

Strict Invariants:
- Zero hardcoded symbol counts (dynamically queries SQLite and Qlib provider).
- Explains every rejected symbol with explicit categorized reason.
- Evaluates real calendar missingness and data freshness.
- No synthetic prices or demo data.
"""

import os
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

from app.data.historical_data_layer import get_db_path
from app.analytics.universe_config import (
    resolve_nifty_500_established,
    resolve_universe_tickers,
    validate_ticker,
    LIVE_UNIVERSE
)
from app.qlib_engine.india_data_adapter import get_qlib_adapter

logger = logging.getLogger(__name__)


class DatasetInspector:
    """
    Inspects canonical SQLite market_data.db and the Qlib binary provider
    to compute honest, dynamic health metrics for research datasets.
    """

    @classmethod
    def get_database_summary(cls, timeframe: str = "1d") -> Dict[str, Any]:
        """
        Queries the authoritative SQLite ohlcv table for overall bar counts,
        earliest date, latest date, and unique symbols.
        """
        db_path = get_db_path()
        if not os.path.exists(db_path):
            return {
                "total_symbols": 0,
                "total_bars": 0,
                "earliest_date": None,
                "latest_date": None,
                "error": f"Database file not found at {db_path}"
            }

        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            cur = conn.cursor()
            tf_clause = "(timeframe = '1d' OR timeframe IS NULL)" if timeframe == "1d" else "timeframe = ?"
            params = [] if timeframe == "1d" else [timeframe]

            cur.execute(
                f"SELECT COUNT(DISTINCT ticker), COUNT(*), MIN(date), MAX(date) FROM ohlcv WHERE {tf_clause}",
                params
            )
            row = cur.fetchone()
            total_symbols = int(row[0]) if row and row[0] is not None else 0
            total_bars = int(row[1]) if row and row[1] is not None else 0
            earliest_date = str(row[2]).split()[0] if row and row[2] else None
            latest_date = str(row[3]).split()[0] if row and row[3] else None

            return {
                "total_symbols": total_symbols,
                "total_bars": total_bars,
                "earliest_date": earliest_date,
                "latest_date": latest_date,
                "timeframe": timeframe
            }
        finally:
            conn.close()

    @classmethod
    def inspect_universe(
        cls,
        universe_name: str = "NIFTY_500_ESTABLISHED",
        custom_tickers: Optional[List[str]] = None,
        min_bars_override: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Dynamically evaluates eligibility and rejection reasons for a specified universe.
        Supported universes: NIFTY_500_ESTABLISHED, FNO, NIFTY_50, CUSTOM.
        """
        clean_name = universe_name.strip().upper()
        min_bars = min_bars_override or (1200 if clean_name == "NIFTY_500_ESTABLISHED" else 100)

        db_path = get_db_path()
        if not os.path.exists(db_path):
            return {
                "universe": clean_name,
                "instruments_discovered": 0,
                "eligible_count": 0,
                "rejected_count": 0,
                "eligible_symbols": [],
                "rejections": {},
                "min_bars_required": min_bars,
                "error": "Database not found"
            }

        if clean_name in ("NIFTY_500_ESTABLISHED", "NIFTY_ESTABLISHED", "ESTABLISHED_500"):
            res = resolve_nifty_500_established(min_bars=min_bars)
            symbols = res.get("symbols", [])
            rejections = res.get("rejections", {})
            return {
                "universe": "NIFTY_500_ESTABLISHED",
                "display_name": "NIFTY 500 Established Equities (>=1,200 Bars)",
                "instruments_discovered": len(symbols) + len(rejections),
                "eligible_count": len(symbols),
                "rejected_count": len(rejections),
                "eligible_symbols": symbols,
                "rejections": rejections,
                "min_bars_required": min_bars,
                "freshness_cutoff": res.get("freshness_cutoff"),
                "overall_max_date": res.get("overall_max_date")
            }

        elif clean_name in ("FNO", "FNO_UNIVERSE", "NSE_FNO"):
            from app.qlib_engine.fno_universe import resolve_fno_universe
            fno_res = resolve_fno_universe()
            fno_symbols = [f"{s}.NS" for s in fno_res.get("symbols", [])]
            conn = sqlite3.connect(db_path, timeout=10.0)
            eligible = []
            rejections = {}
            try:
                cur = conn.cursor()
                for sym in fno_symbols:
                    cur.execute(
                        "SELECT COUNT(*), MIN(close), MAX(date) FROM ohlcv WHERE (ticker = ? OR ticker = ?) AND (timeframe = '1d' OR timeframe IS NULL)",
                        (sym, sym.replace(".NS", ""))
                    )
                    row = cur.fetchone()
                    cnt = row[0] if row else 0
                    min_c = row[1] if row else None
                    if cnt == 0:
                        rejections[sym] = "NO_DATA_IN_DATABASE"
                    elif cnt < min_bars:
                        rejections[sym] = f"INSUFFICIENT_BARS ({cnt} < {min_bars})"
                    elif min_c is None or min_c <= 0:
                        rejections[sym] = f"NON_POSITIVE_CLOSE (min={min_c})"
                    else:
                        eligible.append(sym)
            finally:
                conn.close()

            return {
                "universe": "FNO",
                "display_name": "NSE Derivatives (F&O) Universe",
                "instruments_discovered": len(fno_symbols),
                "eligible_count": len(eligible),
                "rejected_count": len(rejections),
                "eligible_symbols": sorted(eligible),
                "rejections": rejections,
                "min_bars_required": min_bars,
                "source": fno_res.get("source", "NSE_OFFICIAL")
            }

        elif clean_name in ("NIFTY_50", "NIFTY50"):
            tickers = resolve_universe_tickers("NIFTY_50")
            conn = sqlite3.connect(db_path, timeout=10.0)
            eligible = []
            rejections = {}
            try:
                cur = conn.cursor()
                for sym in tickers:
                    cur.execute(
                        "SELECT COUNT(*), MIN(close) FROM ohlcv WHERE (ticker = ? OR ticker = ?) AND (timeframe = '1d' OR timeframe IS NULL)",
                        (sym, sym.replace(".NS", ""))
                    )
                    row = cur.fetchone()
                    cnt = row[0] if row else 0
                    min_c = row[1] if row else None
                    if cnt == 0:
                        rejections[sym] = "NO_DATA_IN_DATABASE"
                    elif cnt < min_bars:
                        rejections[sym] = f"INSUFFICIENT_BARS ({cnt} < {min_bars})"
                    elif min_c is None or min_c <= 0:
                        rejections[sym] = f"NON_POSITIVE_CLOSE (min={min_c})"
                    else:
                        eligible.append(sym)
            finally:
                conn.close()

            return {
                "universe": "NIFTY_50",
                "display_name": "NIFTY 50 Blue-Chip Equities",
                "instruments_discovered": len(tickers),
                "eligible_count": len(eligible),
                "rejected_count": len(rejections),
                "eligible_symbols": sorted(eligible),
                "rejections": rejections,
                "min_bars_required": min_bars
            }

        elif clean_name == "CUSTOM":
            raw_list = custom_tickers or []
            conn = sqlite3.connect(db_path, timeout=10.0)
            eligible = []
            rejections = {}
            try:
                cur = conn.cursor()
                for sym in raw_list:
                    formatted_sym = sym if sym.endswith((".NS", ".BO")) else f"{sym}.NS"
                    cur.execute(
                        "SELECT COUNT(*), MIN(close) FROM ohlcv WHERE (ticker = ? OR ticker = ?) AND (timeframe = '1d' OR timeframe IS NULL)",
                        (formatted_sym, formatted_sym.replace(".NS", ""))
                    )
                    row = cur.fetchone()
                    cnt = row[0] if row else 0
                    min_c = row[1] if row else None
                    if cnt == 0:
                        rejections[formatted_sym] = "NO_DATA_IN_DATABASE"
                    elif cnt < min_bars:
                        rejections[formatted_sym] = f"INSUFFICIENT_BARS ({cnt} < {min_bars})"
                    elif min_c is None or min_c <= 0:
                        rejections[formatted_sym] = f"NON_POSITIVE_CLOSE (min={min_c})"
                    else:
                        eligible.append(formatted_sym)
            finally:
                conn.close()

            return {
                "universe": "CUSTOM",
                "display_name": f"Custom Basket ({len(raw_list)} Symbols)",
                "instruments_discovered": len(raw_list),
                "eligible_count": len(eligible),
                "rejected_count": len(rejections),
                "eligible_symbols": sorted(eligible),
                "rejections": rejections,
                "min_bars_required": min_bars
            }

        else:
            tickers = resolve_universe_tickers(clean_name)
            return {
                "universe": clean_name,
                "display_name": f"Universe: {clean_name}",
                "instruments_discovered": len(tickers),
                "eligible_count": len(tickers),
                "rejected_count": 0,
                "eligible_symbols": sorted(tickers),
                "rejections": {},
                "min_bars_required": min_bars
            }

    @classmethod
    def get_provider_sync_status(cls) -> Dict[str, Any]:
        """
        Inspects the physical Qlib binary provider directory:
        calendars/day.txt, instruments/all.txt, and features/ directories.
        """
        adapter = get_qlib_adapter()
        provider_dir = adapter.provider_uri

        cal_day = os.path.join(provider_dir, "calendars", "day.txt")
        inst_all = os.path.join(provider_dir, "instruments", "all.txt")
        feat_dir = os.path.join(provider_dir, "features")

        cal_exists = os.path.exists(cal_day) and os.path.getsize(cal_day) > 0
        inst_exists = os.path.exists(inst_all) and os.path.getsize(inst_all) > 0
        feat_exists = os.path.exists(feat_dir) and os.path.isdir(feat_dir)

        cal_count = 0
        if cal_exists:
            with open(cal_day, "r") as fp:
                cal_count = sum(1 for line in fp if line.strip())

        inst_count = 0
        if inst_exists:
            with open(inst_all, "r") as fp:
                inst_count = sum(1 for line in fp if line.strip())

        feat_symbols_count = len(os.listdir(feat_dir)) if feat_exists else 0

        latest_mtime = None
        if os.path.exists(cal_day):
            latest_mtime = datetime.fromtimestamp(os.path.getmtime(cal_day)).isoformat()

        is_synced = cal_exists and inst_exists and feat_symbols_count > 0

        return {
            "provider_uri": provider_dir,
            "is_synchronized": is_synced,
            "status": "SYNCHRONIZED" if is_synced else "OUT_OF_SYNC",
            "calendar_trading_days": cal_count,
            "instruments_mapped": inst_count,
            "feature_directories_count": feat_symbols_count,
            "last_provider_update": latest_mtime
        }

    @classmethod
    def calculate_missingness(cls, eligible_symbols: List[str]) -> Dict[str, Any]:
        """
        Estimates missing-data percentage across eligible symbols against the
        canonical trading calendar dates.
        """
        if not eligible_symbols:
            return {"missingness_pct": 0.0, "total_expected_bars": 0, "total_actual_bars": 0}

        db_path = get_db_path()
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(DISTINCT date) FROM ohlcv 
                WHERE timeframe = '1d' OR timeframe IS NULL
            """)
            row = cur.fetchone()
            total_calendar_dates = row[0] if row else 0

            stripped_symbols = [s.replace(".NS", "") for s in eligible_symbols]
            all_params = eligible_symbols + stripped_symbols
            p_str = ",".join("?" for _ in all_params)

            cur.execute(f"""
                SELECT COUNT(*) FROM ohlcv 
                WHERE ticker IN ({p_str}) AND (timeframe = '1d' OR timeframe IS NULL)
            """, all_params)
            row = cur.fetchone()
            actual_bars = row[0] if row else 0

            expected_bars = total_calendar_dates * len(eligible_symbols)
            if expected_bars <= 0:
                missing_pct = 0.0
            else:
                missing_bars = max(0, expected_bars - actual_bars)
                missing_pct = round((missing_bars / expected_bars) * 100.0, 2)

            return {
                "missingness_pct": missing_pct,
                "total_expected_bars": expected_bars,
                "total_actual_bars": actual_bars,
                "calendar_trading_days": total_calendar_dates
            }
        except Exception as e:
            logger.warning(f"[DatasetInspector] Error calculating missingness: {e}")
            return {"missingness_pct": 0.0, "error": str(e)}
        finally:
            conn.close()

    @classmethod
    def get_dataset_health(cls, universe_name: str = "NIFTY_500_ESTABLISHED") -> Dict[str, Any]:
        """
        Aggregates complete dataset health telemetry for Section A of the Qlib Control Room.
        """
        adapter = get_qlib_adapter()
        db_summary = cls.get_database_summary("1d")
        uni_inspection = cls.inspect_universe(universe_name)
        sync_status = cls.get_provider_sync_status()
        freshness = adapter.check_freshness("1d")
        missingness = cls.calculate_missingness(uni_inspection.get("eligible_symbols", []))

        return {
            "status": "ONLINE",
            "universe_name": universe_name,
            "display_name": uni_inspection.get("display_name"),
            "total_unique_symbols": db_summary.get("total_symbols", 0),
            "eligible_symbols_count": uni_inspection.get("eligible_count", 0),
            "rejected_symbols_count": uni_inspection.get("rejected_count", 0),
            "rejections": uni_inspection.get("rejections", {}),
            "min_history_bars": uni_inspection.get("min_bars_required", 1200),
            "actual_earliest_date": db_summary.get("earliest_date"),
            "actual_latest_date": db_summary.get("latest_date"),
            "total_daily_bars": db_summary.get("total_bars", 0),
            "missing_data_pct": missingness.get("missingness_pct", 0.0),
            "missingness_details": missingness,
            "data_freshness": freshness,
            "qlib_provider_sync": sync_status,
            "inspected_at": datetime.now().isoformat()
        }

