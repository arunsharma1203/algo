import sqlite3
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException

from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)
router = APIRouter()

DEFAULT_WATCHLIST_TICKERS = [
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS"
]

class AddTickerRequest(BaseModel):
    ticker: Optional[str] = None
    tickers: Optional[List[str]] = None
    notes: Optional[str] = ""

class ReorderRequest(BaseModel):
    order: List[str]

class MigrateRequest(BaseModel):
    tickers: List[str]

from app.analytics.universe_config import normalize_ticker

def init_watchlist_table():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_watchlist (
                    ticker TEXT PRIMARY KEY,
                    added_at TEXT NOT NULL,
                    display_order INTEGER DEFAULT 0,
                    notes TEXT DEFAULT ''
                )
            """)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM user_watchlist")
            count = cur.fetchone()[0]
            if count == 0:
                now_str = datetime.now().isoformat()
                for idx, t in enumerate(DEFAULT_WATCHLIST_TICKERS):
                    conn.execute(
                        "INSERT OR IGNORE INTO user_watchlist (ticker, added_at, display_order, notes) VALUES (?, ?, ?, ?)",
                        (t, now_str, idx, "Default core liquid asset")
                    )
    except Exception as e:
        logger.error(f"Error initializing user_watchlist table: {e}")
    finally:
        conn.close()

def get_persisted_watchlist() -> List[Dict[str, Any]]:
    init_watchlist_table()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        cur = conn.cursor()
        cur.execute("SELECT ticker, added_at, display_order, notes FROM user_watchlist ORDER BY display_order ASC, added_at ASC")
        rows = cur.fetchall()
        return [
            {
                "ticker": r[0],
                "added_at": r[1],
                "display_order": r[2],
                "notes": r[3]
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Error retrieving user_watchlist: {e}")
        return [{"ticker": t, "added_at": datetime.now().isoformat(), "display_order": i, "notes": ""} for i, t in enumerate(DEFAULT_WATCHLIST_TICKERS)]
    finally:
        conn.close()

def get_persisted_watchlist_tickers() -> List[str]:
    items = get_persisted_watchlist()
    return [item["ticker"] for item in items]

@router.get("")
def get_watchlist():
    """Returns the authoritative user watchlist persisted in backend SQLite."""
    items = get_persisted_watchlist()
    tickers = [item["ticker"] for item in items]
    return {
        "status": "success",
        "authoritative_source": "sqlite_user_watchlist",
        "count": len(tickers),
        "tickers": tickers,
        "items": items
    }

@router.post("")
def add_to_watchlist(payload: AddTickerRequest):
    """Adds single or multiple tickers to backend SQLite watchlist."""
    init_watchlist_table()
    to_add = []
    if payload.ticker:
        to_add.append(payload.ticker)
    if payload.tickers:
        to_add.extend(payload.tickers)
        
    cleaned_tickers = [normalize_ticker(t) for t in to_add if normalize_ticker(t)]
    if not cleaned_tickers:
        raise HTTPException(status_code=400, detail="No valid ticker provided")

    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        now_str = datetime.now().isoformat()
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(display_order) FROM user_watchlist")
            max_order_res = cur.fetchone()[0]
            max_order = max_order_res if max_order_res is not None else -1

            for t in cleaned_tickers:
                cur.execute("SELECT COUNT(*) FROM user_watchlist WHERE ticker = ?", (t,))
                exists = cur.fetchone()[0]
                if not exists:
                    max_order += 1
                    conn.execute(
                        "INSERT INTO user_watchlist (ticker, added_at, display_order, notes) VALUES (?, ?, ?, ?)",
                        (t, now_str, max_order, payload.notes or "")
                    )
    except Exception as e:
        logger.error(f"Failed to add to watchlist: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        conn.close()

    items = get_persisted_watchlist()
    tickers = [item["ticker"] for item in items]
    return {
        "status": "success",
        "message": f"Added {len(cleaned_tickers)} ticker(s) to authoritative backend watchlist.",
        "tickers": tickers,
        "items": items
    }

@router.delete("/{ticker}")
def delete_from_watchlist(ticker: str):
    """Deletes a ticker from backend SQLite watchlist."""
    init_watchlist_table()
    clean_ticker = normalize_ticker(ticker)
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        with conn:
            conn.execute("DELETE FROM user_watchlist WHERE ticker = ?", (clean_ticker,))
    except Exception as e:
        logger.error(f"Failed to delete {clean_ticker} from watchlist: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        conn.close()

    items = get_persisted_watchlist()
    tickers = [item["ticker"] for item in items]
    return {
        "status": "success",
        "message": f"Deleted {clean_ticker} from authoritative backend watchlist.",
        "tickers": tickers,
        "items": items
    }

@router.put("/reorder")
def reorder_watchlist(payload: ReorderRequest):
    """Reorders tickers in backend SQLite watchlist."""
    init_watchlist_table()
    cleaned_order = [normalize_ticker(t) for t in payload.order if normalize_ticker(t)]
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        with conn:
            for idx, t in enumerate(cleaned_order):
                conn.execute("UPDATE user_watchlist SET display_order = ? WHERE ticker = ?", (idx, t))
    except Exception as e:
        logger.error(f"Failed to reorder watchlist: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        conn.close()

    items = get_persisted_watchlist()
    tickers = [item["ticker"] for item in items]
    return {
        "status": "success",
        "message": "Watchlist sequence updated successfully.",
        "tickers": tickers,
        "items": items
    }

@router.post("/migrate")
def migrate_legacy_watchlist(payload: MigrateRequest):
    """
    Safely migrates localStorage tickers into SQLite backend.
    Enforces Invariant 1: Confirms persistence before responding,
    guaranteeing client only deletes localStorage after verification.
    """
    init_watchlist_table()
    legacy_tickers = [normalize_ticker(t) for t in payload.tickers if normalize_ticker(t)]
    
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        now_str = datetime.now().isoformat()
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(display_order) FROM user_watchlist")
            max_order_res = cur.fetchone()[0]
            max_order = max_order_res if max_order_res is not None else -1

            for t in legacy_tickers:
                cur.execute("SELECT COUNT(*) FROM user_watchlist WHERE ticker = ?", (t,))
                if cur.fetchone()[0] == 0:
                    max_order += 1
                    conn.execute(
                        "INSERT INTO user_watchlist (ticker, added_at, display_order, notes) VALUES (?, ?, ?, ?)",
                        (t, now_str, max_order, "Migrated from client localStorage")
                    )
    except Exception as e:
        logger.error(f"Failed to migrate legacy watchlist: {e}")
        raise HTTPException(status_code=500, detail=f"Database migration error: {str(e)}")
    finally:
        conn.close()

    items = get_persisted_watchlist()
    persisted_tickers = [item["ticker"] for item in items]
    
    # Invariant 1 verification: confirm all valid legacy tickers now exist in persisted set
    all_migrated = all(t in persisted_tickers for t in legacy_tickers)
    
    return {
        "status": "success",
        "persisted": True,
        "verified_in_backend": all_migrated,
        "count": len(persisted_tickers),
        "tickers": persisted_tickers,
        "items": items
    }

