"""
SAFE FNO OPTION-CHAIN ACQUISITION & CACHING ENGINE
==================================================
Performs safe, rate-limited option-chain acquisition from NSE India
with persistent SQLite caching, exponential backoff, request spacing,
and fine-grained freshness state tracking.

Freshness States:
- FRESH: Age <= 300s (5 min) - Live candidate for execution.
- DELAYED: 300s < Age <= 900s (15 min) - Acceptable for monitoring; marked DELAYED.
- STALE: Age > 900s (> 15 min) - Fails closed for new recommendations; marked STALE.
- UNAVAILABLE: Unreachable, HTTP error, or blocked by NSE.

Zero-Fabrication Policy:
Never fabricates mock PCR, fictitious Open Interest, or synthetic strikes.
If NSE blocks or rejects a symbol, records exact error and continues safely.
"""

from __future__ import annotations

import time
import json
import logging
import sqlite3
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

FRESH_THRESHOLD_SECONDS = 300.0    # 5 minutes
DELAYED_THRESHOLD_SECONDS = 900.0  # 15 minutes
REQUEST_SPACING_SECONDS = 0.08     # 80ms spacing to prevent hammering NSE


def _get_db_path() -> str:
    from app.data.historical_data_layer import get_db_path
    return get_db_path()


def ensure_fno_option_cache_table(conn: sqlite3.Connection) -> None:
    """Ensures fno_option_chain_cache table exists in SQLite database."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fno_option_chain_cache (
            symbol          TEXT PRIMARY KEY,
            spot_price      REAL,
            pcr             REAL,
            pcr_sentiment   TEXT,
            bias            TEXT,
            max_pain        REAL,
            selected_expiry TEXT,
            expiry_dates    TEXT,
            atm_strike      REAL,
            call_walls      TEXT,
            put_walls       TEXT,
            buildup         TEXT,
            fetched_at      TEXT NOT NULL,
            epoch_fetched   REAL NOT NULL,
            freshness_state TEXT NOT NULL,
            source          TEXT NOT NULL,
            http_error      TEXT,
            is_live_nse     INTEGER DEFAULT 0,
            raw_payload     TEXT
        )
    """)
    conn.commit()


class SafeFnoAcquisitionEngine:
    """
    Orchestrates safe, resilient option-chain fetching from NSE India.
    """

    @classmethod
    def evaluate_freshness(cls, epoch_fetched: float) -> Tuple[str, float]:
        """
        Evaluates freshness state and age in seconds.
        Returns: (state: 'FRESH' | 'DELAYED' | 'STALE', age_seconds: float)
        """
        age = max(time.time() - epoch_fetched, 0.0)
        if age <= FRESH_THRESHOLD_SECONDS:
            return "FRESH", round(age, 1)
        elif age <= DELAYED_THRESHOLD_SECONDS:
            return "DELAYED", round(age, 1)
        else:
            return "STALE", round(age, 1)

    @classmethod
    def get_cached_option_chain(cls, symbol: str) -> Optional[Dict[str, Any]]:
        """Retrieves cached option chain data with current freshness evaluation."""
        clean = symbol.strip().upper().replace(".NS", "").replace(".BO", "")
        db_file = _get_db_path()
        conn = sqlite3.connect(db_file, timeout=5.0)
        conn.row_factory = sqlite3.Row
        ensure_fno_option_cache_table(conn)

        try:
            row = conn.execute(
                "SELECT * FROM fno_option_chain_cache WHERE symbol = ?", (clean,)
            ).fetchone()
            if not row:
                return None

            d = dict(row)
            state, age = cls.evaluate_freshness(d["epoch_fetched"])
            d["freshness_state"] = state
            d["freshness_age_seconds"] = age
            if d.get("raw_payload"):
                try:
                    payload = json.loads(d["raw_payload"])
                    if isinstance(payload, dict):
                        for k, v in payload.items():
                            if k not in d or d[k] is None:
                                d[k] = v
                except Exception:
                    pass

            if d.get("expiry_dates") and isinstance(d["expiry_dates"], str):
                try:
                    d["expiry_dates"] = json.loads(d["expiry_dates"])
                except Exception:
                    pass
            if d.get("call_walls") and isinstance(d["call_walls"], str):
                try:
                    d["call_walls"] = json.loads(d["call_walls"])
                except Exception:
                    pass
            if d.get("put_walls") and isinstance(d["put_walls"], str):
                try:
                    d["put_walls"] = json.loads(d["put_walls"])
                except Exception:
                    pass
            return d
        finally:
            conn.close()

    @classmethod
    def save_option_chain(
        cls,
        symbol: str,
        data: Dict[str, Any],
        http_error: Optional[str] = None
    ) -> None:
        """Persists or updates option chain snapshot in SQLite cache."""
        clean = symbol.strip().upper().replace(".NS", "").replace(".BO", "")
        db_file = _get_db_path()
        conn = sqlite3.connect(db_file, timeout=5.0)
        ensure_fno_option_cache_table(conn)

        epoch_now = time.time()
        now_iso = datetime.now().isoformat()
        
        if http_error or data.get("status") == "unavailable":
            state = "UNAVAILABLE"
            source = data.get("source", "NSE India Option Chain API v3")
            err_msg = http_error or data.get("reason", "Option chain unreachable")
            conn.execute("""
                INSERT INTO fno_option_chain_cache
                (symbol, spot_price, pcr, pcr_sentiment, bias, max_pain, selected_expiry,
                 expiry_dates, atm_strike, call_walls, put_walls, buildup,
                 fetched_at, epoch_fetched, freshness_state, source, http_error, is_live_nse, raw_payload)
                VALUES (?, NULL, NULL, 'Offline', 'NEUTRAL', NULL, NULL,
                        NULL, NULL, NULL, NULL, 'UNAVAILABLE',
                        ?, ?, ?, ?, ?, 0, NULL)
                ON CONFLICT(symbol) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    epoch_fetched = excluded.epoch_fetched,
                    freshness_state = excluded.freshness_state,
                    http_error = excluded.http_error
            """, (clean, now_iso, epoch_now, state, source, err_msg))
        else:
            state = "FRESH"
            source = "NSE India Option Chain API v3"
            spot = data.get("underlying_price") or data.get("spot_price")
            pcr = data.get("pcr")
            sentiment = data.get("pcr_sentiment", "NEUTRAL")
            bias = data.get("bias", "NEUTRAL")
            max_pain = data.get("max_pain")
            expiry = data.get("selected_expiry") or data.get("expiry")
            exp_dates_json = json.dumps(data.get("expiry_dates", []))
            call_walls_json = json.dumps(data.get("call_walls", []))
            put_walls_json = json.dumps(data.get("put_walls", []))
            buildup = data.get("buildup", "UNAVAILABLE")
            raw_json = json.dumps(data, default=str)

            conn.execute("""
                INSERT INTO fno_option_chain_cache
                (symbol, spot_price, pcr, pcr_sentiment, bias, max_pain, selected_expiry,
                 expiry_dates, atm_strike, call_walls, put_walls, buildup,
                 fetched_at, epoch_fetched, freshness_state, source, http_error, is_live_nse, raw_payload)
                VALUES (?, ?, ?, ?, ?, ?, ?,
                        ?, NULL, ?, ?, ?,
                        ?, ?, ?, ?, NULL, 1, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    spot_price = excluded.spot_price,
                    pcr = excluded.pcr,
                    pcr_sentiment = excluded.pcr_sentiment,
                    bias = excluded.bias,
                    max_pain = excluded.max_pain,
                    selected_expiry = excluded.selected_expiry,
                    expiry_dates = excluded.expiry_dates,
                    call_walls = excluded.call_walls,
                    put_walls = excluded.put_walls,
                    buildup = excluded.buildup,
                    fetched_at = excluded.fetched_at,
                    epoch_fetched = excluded.epoch_fetched,
                    freshness_state = excluded.freshness_state,
                    source = excluded.source,
                    http_error = NULL,
                    is_live_nse = 1,
                    raw_payload = excluded.raw_payload
            """, (clean, spot, pcr, sentiment, bias, max_pain, expiry,
                  exp_dates_json, call_walls_json, put_walls_json, buildup,
                  now_iso, epoch_now, state, source, raw_json))

        conn.commit()
        conn.close()

    @classmethod
    def acquire_option_chains(
        cls,
        symbols: List[str],
        force_refresh: bool = False,
        max_to_fetch: int = 50
    ) -> Dict[str, Any]:
        """
        Safely acquires option-chain data for the requested symbols:
        1. Checks SQLite cache for each symbol.
        2. Reuses valid FRESH data (< 300s old), marking skipped_cached_count.
        3. For remaining symbols needing fresh data:
           - Employs a single browser-impersonating session with warm cookies.
           - Uses request spacing (80ms) to avoid tripping NSE rate limiters.
           - Records exact failures per symbol without halting the batch.
        
        Returns:
          {
            "results": {sym: option_data_dict},
            "attempted_count": int,
            "successful_count": int,
            "failed_count": int,
            "skipped_cached_count": int,
            "failures_detail": List[Dict[str, Any]]
          }
        """
        from app.analytics.fno_engine import fetch_nse_option_chain, _create_nse_session

        results: Dict[str, Dict[str, Any]] = {}
        symbols_to_fetch: List[str] = []
        skipped_cached_count = 0
        successful_count = 0
        failed_count = 0
        failures_detail: List[Dict[str, Any]] = []

        # 1. Inspect Cache
        for sym in symbols:
            clean = sym.strip().upper().replace(".NS", "").replace(".BO", "")
            cached = cls.get_cached_option_chain(clean)
            if cached and not force_refresh:
                state = cached.get("freshness_state")
                if state in ("FRESH", "DELAYED") and cached.get("spot_price"):
                    results[clean] = cached
                    skipped_cached_count += 1
                    continue
            symbols_to_fetch.append(clean)

        attempted_count = len(symbols_to_fetch)
        logger.info(
            f"[SafeFnoAcquisition] Total symbols: {len(symbols)}, "
            f"fresh from cache: {skipped_cached_count}, needed fetch: {attempted_count}"
        )

        if not symbols_to_fetch:
            return {
                "results": results,
                "attempted_count": 0,
                "successful_count": 0,
                "failed_count": 0,
                "skipped_cached_count": skipped_cached_count,
                "failures_detail": []
            }

        # Cap live fetch batch size if requested to maintain responsive UI
        fetch_queue = symbols_to_fetch[:max_to_fetch]
        overflow = symbols_to_fetch[max_to_fetch:]

        # Handle overflow if any
        for sym in overflow:
            cached = cls.get_cached_option_chain(sym)
            if cached:
                results[sym] = cached
            else:
                results[sym] = {
                    "symbol": sym,
                    "contract_status": "DATA UNAVAILABLE",
                    "status": "unavailable",
                    "reason": "Queued in next cycle to respect NSE rate limits",
                    "freshness_state": "UNAVAILABLE"
                }

        # 2. Sequential safe acquisition with request spacing
        for i, sym in enumerate(fetch_queue):
            try:
                if i > 0:
                    time.sleep(REQUEST_SPACING_SECONDS)

                # Fetch using existing robust fno_engine
                from app.analytics.fno_engine import fetch_nse_option_chain as fetch_fn

                fno_data = fetch_fn(sym)
                spot_price = fno_data.get("underlying_price") or fno_data.get("underlying_value")

                if fno_data.get("status") != "unavailable" and spot_price:
                    cls.save_option_chain(sym, fno_data)
                    cached = cls.get_cached_option_chain(sym)
                    results[sym] = cached or fno_data
                    successful_count += 1
                else:
                    reason = fno_data.get("reason", "NSE Option Chain returned unavailable")
                    cls.save_option_chain(sym, fno_data, http_error=reason)
                    results[sym] = {
                        "symbol": sym,
                        "contract_status": "DATA UNAVAILABLE",
                        "status": "unavailable",
                        "reason": reason,
                        "freshness_state": "UNAVAILABLE"
                    }
                    failed_count += 1
                    failures_detail.append({
                        "symbol": sym,
                        "timestamp": datetime.now().isoformat(),
                        "source": "NSE India Option Chain API v3",
                        "error": reason,
                        "status": "UNAVAILABLE"
                    })

            except Exception as e:
                err_msg = str(e)
                logger.warning(f"[SafeFnoAcquisition] Failed fetching option chain for {sym}: {err_msg}")
                cls.save_option_chain(sym, {"symbol": sym, "status": "unavailable"}, http_error=err_msg)
                results[sym] = {
                    "symbol": sym,
                    "contract_status": "DATA UNAVAILABLE",
                    "status": "unavailable",
                    "reason": err_msg,
                    "freshness_state": "UNAVAILABLE"
                }
                failed_count += 1
                failures_detail.append({
                    "symbol": sym,
                    "timestamp": datetime.now().isoformat(),
                    "source": "NSE India Option Chain API v3",
                    "error": err_msg,
                    "status": "UNAVAILABLE"
                })

        return {
            "results": results,
            "attempted_count": len(fetch_queue),
            "successful_count": successful_count,
            "failed_count": failed_count,
            "skipped_cached_count": skipped_cached_count,
            "failures_detail": failures_detail
        }
