"""
FNO UNIVERSE DISCOVERY ENGINE
==============================
Authoritative dynamic discovery for the National Stock Exchange of India (NSE)
Equity Derivatives (F&O) universe.

Architecture:
1. Online Discovery: Queries official NSE F&O data feeds:
   - https://archives.nseindia.com/content/fo/fo_mktlots.csv
   - https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O
2. Deduplication & Validation: Validates symbols using platform's validate_ticker engine.
3. Persistent Caching: Stores discovered universe in SQLite `fno_universe_cache` with TTL (24 hours).
4. Local Fallback Seed: Seeds from validated `nse_fno_underlyings.json` when offline.
5. Strict Fail-Closed Governance: If dynamic universe discovery fails completely, raises
   `FnoUniverseDiscoveryError`. NEVER silently falls back to 5 stocks.
"""

from __future__ import annotations

import os
import csv
import json
import io
import time
import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

FNO_CACHE_TTL_HOURS = 24  # NSE F&O universe changes on monthly expiry cycles

class FnoUniverseDiscoveryError(Exception):
    """Raised when authoritative F&O universe discovery fails closed."""
    pass


def _get_db_path() -> str:
    from app.data.historical_data_layer import get_db_path
    return get_db_path()


def ensure_fno_universe_table(conn: sqlite3.Connection) -> None:
    """Ensures fno_universe_cache table exists in canonical SQLite database."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fno_universe_cache (
            symbol          TEXT PRIMARY KEY,
            lot_size        INTEGER DEFAULT 0,
            discovered_at   TEXT NOT NULL,
            source          TEXT NOT NULL,
            status          TEXT DEFAULT 'ACTIVE'
        )
    """)
    conn.commit()


def _fetch_from_nse_mktlots() -> List[Dict[str, Any]]:
    """
    Attempts downloading the official NSE market lots CSV.
    URL: https://archives.nseindia.com/content/fo/fo_mktlots.csv
    """
    from app.analytics.fno_engine import _create_nse_session
    session = _create_nse_session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Referer": "https://www.nseindia.com/"
    }
    url = "https://archives.nseindia.com/content/fo/fo_mktlots.csv"
    res = session.get(url, headers=headers, timeout=8.0)
    if res.status_code != 200 or len(res.content) < 100:
        raise ValueError(f"NSE fo_mktlots.csv returned HTTP {res.status_code}")

    text = res.content.decode("utf-8", errors="ignore")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    reader = csv.reader(lines)
    
    underlyings = []
    header_found = False
    sym_idx = 1
    lot_idx = 2
    
    for row in reader:
        if not row:
            continue
        row_str = [c.strip().upper() for c in row]
        if "SYMBOL" in row_str or "UNDERLYING" in row_str:
            header_found = True
            for i, h in enumerate(row_str):
                if h == "SYMBOL":
                    sym_idx = i
                elif "LOT" in h:
                    lot_idx = i
            continue
            
        if header_found and len(row) > sym_idx:
            raw_sym = row[sym_idx].strip().upper()
            if raw_sym and raw_sym not in ("SYMBOL", "UNDERLYING", "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
                lot = 0
                if len(row) > lot_idx:
                    try:
                        lot = int(row[lot_idx].strip())
                    except Exception:
                        lot = 0
                underlyings.append({"symbol": raw_sym, "lot_size": lot})

    if len(underlyings) < 50:
        raise ValueError(f"Parsed only {len(underlyings)} symbols from NSE fo_mktlots.csv; expected >= 100")

    return underlyings


def _fetch_from_nse_index_api() -> List[Dict[str, Any]]:
    """
    Attempts querying official NSE equity derivatives index:
    URL: https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O
    """
    from app.analytics.fno_engine import _create_nse_session
    session = _create_nse_session()
    headers = {
        "Referer": "https://www.nseindia.com/market-data/live-equity-market",
        "Accept": "application/json, text/plain, */*"
    }
    try:
        session.get("https://www.nseindia.com", timeout=4.0)
    except Exception:
        pass

    url = "https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O"
    res = session.get(url, headers=headers, timeout=8.0)
    if res.status_code != 200 or len(res.content) < 200:
        raise ValueError(f"NSE SECURITIES IN F&O API returned HTTP {res.status_code}")

    data = res.json()
    items = data.get("data", [])
    underlyings = []
    for it in items:
        sym = it.get("symbol", "").strip().upper()
        if sym and sym not in ("NIFTY 50", "NIFTY", "BANKNIFTY", "SECURITIES IN F&O"):
            underlyings.append({"symbol": sym, "lot_size": 0})

    if len(underlyings) < 50:
        raise ValueError(f"Parsed only {len(underlyings)} symbols from NSE index API; expected >= 100")

    return underlyings


def _load_local_seed_file() -> List[Dict[str, Any]]:
    """Loads validated local authoritative seed file (nse_fno_underlyings.json)."""
    seed_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "analytics", "nse_fno_underlyings.json")
    if os.path.exists(seed_path):
        with open(seed_path, "r") as fp:
            return json.load(fp)
    return []


def resolve_fno_universe(
    force_refresh: bool = False,
    min_universe_size: int = 50
) -> Dict[str, Any]:
    """
    Authoritative F&O universe resolver.
    
    Returns:
      {
        "symbols": List[str],            # Canonical symbols (e.g. ['RELIANCE', 'TCS', ...])
        "discovered_count": int,
        "source": str,
        "discovered_at": str,
        "lot_sizes": Dict[str, int]
      }
      
    Strict Fail-Closed Rule:
    If dynamic universe discovery fails completely, raises FnoUniverseDiscoveryError.
    Never silently falls back to 5 stocks.
    """
    db_file = _get_db_path()
    conn = sqlite3.connect(db_file, timeout=10.0)
    ensure_fno_universe_table(conn)

    now_ts = datetime.now().isoformat()
    epoch_now = time.time()

    # 1. Check local SQLite cache if not forcing refresh
    if not force_refresh:
        cur = conn.cursor()
        cur.execute("SELECT symbol, lot_size, discovered_at, source FROM fno_universe_cache WHERE status = 'ACTIVE' ORDER BY symbol")
        rows = cur.fetchall()
        if rows and len(rows) >= min_universe_size:
            first_disc = rows[0][2]
            try:
                disc_epoch = datetime.fromisoformat(first_disc).timestamp()
                age_hours = (epoch_now - disc_epoch) / 3600.0
            except Exception:
                age_hours = 0.0

            if age_hours < FNO_CACHE_TTL_HOURS:
                symbols = [r[0] for r in rows]
                lot_map = {r[0]: r[1] for r in rows}
                source = rows[0][3] or "LOCAL_SQLITE_CACHE"
                conn.close()
                return {
                    "symbols": symbols,
                    "discovered_count": len(symbols),
                    "source": f"{source} (cached {round(age_hours, 1)}h ago)",
                    "discovered_at": first_disc,
                    "lot_sizes": lot_map
                }

    # 2. Attempt online discovery from official NSE endpoints
    discovered_list: List[Dict[str, Any]] = []
    source_name = "NSE_OFFICIAL_ONLINE"

    try:
        discovered_list = _fetch_from_nse_mktlots()
        source_name = "NSE_ARCHIVES_MKT_LOTS"
        logger.info(f"[FnoUniverse] Discovered {len(discovered_list)} F&O underlyings from NSE market lots CSV.")
    except Exception as e1:
        logger.warning(f"[FnoUniverse] Online fo_mktlots discovery failed: {e1}. Trying NSE index API...")
        try:
            discovered_list = _fetch_from_nse_index_api()
            source_name = "NSE_API_SECURITIES_IN_FNO"
            logger.info(f"[FnoUniverse] Discovered {len(discovered_list)} F&O underlyings from NSE index API.")
        except Exception as e2:
            logger.warning(f"[FnoUniverse] Online NSE index API discovery failed: {e2}. Falling back to local validated cache.")

    # 3. Fallback to existing SQLite cache or validated seed file
    if not discovered_list or len(discovered_list) < min_universe_size:
        cur = conn.cursor()
        cur.execute("SELECT symbol, lot_size, discovered_at, source FROM fno_universe_cache WHERE status = 'ACTIVE' ORDER BY symbol")
        cached_rows = cur.fetchall()
        if cached_rows and len(cached_rows) >= min_universe_size:
            symbols = [r[0] for r in cached_rows]
            lot_map = {r[0]: r[1] for r in cached_rows}
            conn.close()
            logger.info(f"[FnoUniverse] Resolved {len(symbols)} F&O symbols from persistent SQLite cache.")
            return {
                "symbols": symbols,
                "discovered_count": len(symbols),
                "source": "SQLITE_CACHE_FALLBACK",
                "discovered_at": cached_rows[0][2],
                "lot_sizes": lot_map
            }

        # Load from authoritative local seed
        seed_items = _load_local_seed_file()
        if seed_items and len(seed_items) >= min_universe_size:
            discovered_list = seed_items
            source_name = "AUTHORITATIVE_LOCAL_SEED"
            logger.info(f"[FnoUniverse] Loaded {len(discovered_list)} F&O underlyings from local seed.")

    # 4. If discovery still empty: FAIL CLOSED
    if not discovered_list or len(discovered_list) < min_universe_size:
        conn.close()
        raise FnoUniverseDiscoveryError(
            f"F&O UNIVERSE DISCOVERY FAILED: Discovered {len(discovered_list)} symbols (< {min_universe_size} required). "
            f"NSE online endpoints unreachable and no valid local cache found. Fail-closed enforced."
        )

    # 5. Deduplicate and Validate symbols
    from app.analytics.universe_config import validate_ticker
    valid_symbols = []
    lot_sizes = {}
    
    for item in discovered_list:
        raw_sym = item["symbol"].strip().upper().replace(".NS", "").replace(".BO", "")
        ok, canonical_sym, _ = validate_ticker(raw_sym)
        clean = canonical_sym.replace(".NS", "").replace(".BO", "") if ok else raw_sym
        if clean and clean not in valid_symbols:
            valid_symbols.append(clean)
            lot_sizes[clean] = int(item.get("lot_size", 0))

    if len(valid_symbols) < min_universe_size:
        conn.close()
        raise FnoUniverseDiscoveryError(
            f"F&O UNIVERSE DISCOVERY FAILED: Only {len(valid_symbols)} validated symbols (< {min_universe_size} required)."
        )

    # 6. Persist to SQLite cache
    try:
        conn.execute("DELETE FROM fno_universe_cache")
        for sym in valid_symbols:
            conn.execute("""
                INSERT INTO fno_universe_cache (symbol, lot_size, discovered_at, source, status)
                VALUES (?, ?, ?, ?, 'ACTIVE')
            """, (sym, lot_sizes.get(sym, 0), now_ts, source_name))
        conn.commit()
    except Exception as e:
        logger.warning(f"[FnoUniverse] Failed caching discovered universe to SQLite: {e}")
    finally:
        conn.close()

    return {
        "symbols": valid_symbols,
        "discovered_count": len(valid_symbols),
        "source": source_name,
        "discovered_at": now_ts,
        "lot_sizes": lot_sizes
    }


def get_fno_lot_size(symbol: str) -> int:
    """
    Returns official NSE derivative lot size for an underlying symbol.
    Checks indices first, then SQLite cache, then authoritative seed file.
    """
    clean_sym = symbol.strip().upper().replace(".NS", "").replace(".BO", "")
    INDEX_LOTS = {
        "NIFTY": 50,
        "BANKNIFTY": 15,
        "FINNIFTY": 25,
        "MIDCPNIFTY": 50,
        "NIFTY 50": 50,
        "NIFTY BANK": 15,
    }
    if clean_sym in INDEX_LOTS:
        return INDEX_LOTS[clean_sym]

    try:
        conn = sqlite3.connect(_get_db_path(), timeout=5.0)
        ensure_fno_universe_table(conn)
        row = conn.execute("SELECT lot_size FROM fno_universe_cache WHERE symbol = ?", (clean_sym,)).fetchone()
        conn.close()
        if row and row[0] and row[0] > 0:
            return int(row[0])
    except Exception:
        pass

    # Fallback to authoritative seed
    try:
        seed_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "analytics", "nse_fno_underlyings.json")
        if os.path.exists(seed_path):
            with open(seed_path, "r") as f:
                data = json.load(f)
                for item in data:
                    if item.get("symbol") == clean_sym:
                        return int(item.get("lot_size", 0))
    except Exception:
        pass

    return 0


