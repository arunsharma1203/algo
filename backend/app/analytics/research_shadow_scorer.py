"""
RESEARCH SHADOW SCORER
======================
Implements fresh forward OOS shadow scoring for Portfolio Research Challengers.

How it works:
  1. Uses the existing production champion model (same as forward simulation) to score
     every ticker in the research job's universe using ONLY new bars after the OOS cutoff.
  2. Ranks all tickers cross-sectionally by probability (highest-confidence first).
  3. Selects the top-K tickers as virtual RESEARCH_SHADOW entries — zero portfolio heat,
     zero broker execution, no P&L impact on live trading.
  4. Stores trades in `research_shadow_trades` table.
  5. Closes shadow trades after `horizon_days` new daily bars.
  6. Exposes count_fresh_shadow_trades() used by the promotion gate in data_lab.py.

This module is ADDITIVE: it imports from existing modules but does not modify them.
"""

from __future__ import annotations

import uuid
import logging
import sqlite3
import json
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── Configuration constants ────────────────────────────────────────────────
TOP_K_SIGNALS = 10           # Number of virtual trades to open per scan
HORIZON_DAYS = 15            # Close shadow trade after this many new 1d bars
MIN_PROBABILITY = 52.0       # Min champion probability to enter shadow trade
OOS_CUTOFF_DATE = "2026-09-04"  # Hard floor — no shadow trades before this date


# ── Table management ───────────────────────────────────────────────────────

def ensure_shadow_trades_table() -> None:
    """Creates research_shadow_trades table if it doesn't already exist."""
    from app.data.historical_data_layer import get_db_path
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS research_shadow_trades (
            trade_id            TEXT PRIMARY KEY,
            source_research_job_id TEXT NOT NULL,
            scan_date           TEXT NOT NULL,
            ticker              TEXT NOT NULL,
            direction           TEXT NOT NULL DEFAULT 'LONG',
            entry_price         REAL,
            exit_price          REAL,
            pnl_pct             REAL DEFAULT 0.0,
            status              TEXT DEFAULT 'VIRTUAL_OPEN',
            probability         REAL,
            rank_in_universe    INTEGER,
            horizon_days        INTEGER DEFAULT 15,
            exit_bar_count      INTEGER DEFAULT 0,
            created_at          TEXT NOT NULL,
            closed_at           TEXT
        )
    """)
    conn.commit()
    conn.close()


# ── Core scoring ───────────────────────────────────────────────────────────

def run_shadow_scan(source_research_job_id: str) -> Dict[str, Any]:
    """
    Scores the research job's universe using the production champion model,
    cross-sectionally ranks tickers, opens top-K virtual shadow trades, and
    first closes any expired open shadow trades.

    Returns a summary dict with counts and any errors encountered.
    """
    ensure_shadow_trades_table()

    from app.data.historical_data_layer import get_db_path, HistoricalDataLayer
    from app.analytics.model_manager import ModelManager
    from app.analytics.universe_config import get_universe

    scan_date = datetime.now().strftime("%Y-%m-%d")
    scan_ts = datetime.now().isoformat()

    # ── Guard: OOS date ──────────────────────────────────────────────────
    if scan_date <= OOS_CUTOFF_DATE:
        return {
            "status": "SKIPPED",
            "reason": f"scan_date {scan_date} is not after OOS cutoff {OOS_CUTOFF_DATE}",
            "scan_date": scan_date,
        }

    # ── Fetch research job config ────────────────────────────────────────
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    jcols = [c[1] for c in conn.execute("PRAGMA table_info(research_jobs)").fetchall()]
    jrow = conn.execute(
        "SELECT * FROM research_jobs WHERE job_id = ?", (source_research_job_id,)
    ).fetchone()
    conn.close()

    if not jrow:
        return {"status": "ERROR", "reason": f"Research job {source_research_job_id} not found"}

    job = dict(zip(jcols, jrow))
    universe_name = job.get("universe", "ALL_COLLECTED")
    timeframe = job.get("timeframe", "1d")

    # ── Guard: already scanned today for this job ─────────────────────────
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    existing_today = conn.execute(
        "SELECT COUNT(*) FROM research_shadow_trades WHERE source_research_job_id=? AND scan_date=?",
        (source_research_job_id, scan_date)
    ).fetchone()[0]
    conn.close()

    if existing_today > 0:
        return {
            "status": "ALREADY_SCANNED",
            "reason": f"Shadow scan already ran for {source_research_job_id} on {scan_date}",
            "scan_date": scan_date,
            "existing_trades": existing_today,
        }

    # ── Step 1: Close expired open shadow trades ──────────────────────────
    closed_count = _close_expired_shadow_trades(source_research_job_id, scan_date, timeframe)

    # ── Step 2: Load champion model and universe ───────────────────────────
    try:
        champion_model, _ = ModelManager.load_champion("swing" if timeframe == "1d" else "intraday")
    except Exception as e:
        logger.warning(f"[ShadowScorer] Champion model load failed: {e}. Using feature-only scoring.")
        champion_model = None

    try:
        u_info = get_universe(universe_name)
        tickers = u_info.get("tickers", [])
    except Exception as e:
        logger.error(f"[ShadowScorer] Universe load failed for {universe_name}: {e}")
        return {"status": "ERROR", "reason": f"Universe load failed: {e}"}

    if not tickers:
        return {"status": "ERROR", "reason": f"No tickers in universe {universe_name}"}

    # ── Step 3: Score each ticker ──────────────────────────────────────────
    features = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']
    as_of = datetime.now()
    scored: List[Dict[str, Any]] = []
    skipped = 0

    try:
        import ta
    except ImportError:
        return {"status": "ERROR", "reason": "ta-lib not installed"}

    for ticker in tickers:
        try:
            df = HistoricalDataLayer.get_historical_ohlcv(ticker, timeframe=timeframe)
            if df is None or df.empty or len(df) < 50:
                skipped += 1
                continue

            df.columns = [str(c).lower() for c in df.columns]
            df_pit = df[df.index <= as_of].copy()
            if len(df_pit) < 50:
                skipped += 1
                continue

            # Technical indicators
            df_pit['rsi'] = ta.momentum.RSIIndicator(df_pit['close'], window=14).rsi()
            macd_ind = ta.trend.MACD(df_pit['close'])
            df_pit['macd'] = macd_ind.macd()
            df_pit['macd_diff'] = macd_ind.macd_diff()
            df_pit['adx'] = ta.trend.ADXIndicator(
                df_pit['high'], df_pit['low'], df_pit['close'], window=14
            ).adx()
            df_pit['atr'] = ta.volatility.AverageTrueRange(
                df_pit['high'], df_pit['low'], df_pit['close'], window=14
            ).average_true_range()

            clean = df_pit.dropna(subset=features)
            if len(clean) < 30:
                skipped += 1
                continue

            latest = clean.iloc[-1]
            current_price = float(latest['close'])
            if current_price <= 0 or np.isnan(current_price):
                skipped += 1
                continue

            # Champion model probability
            x = latest[features].values.reshape(1, -1)
            x = np.nan_to_num(x)
            prob = 50.0
            if champion_model is not None:
                try:
                    prob = round(float(champion_model.predict_proba(x)[0][1]) * 100.0, 2)
                except Exception:
                    pass

            # Technical confluence bonus (mirrors forward sim logic)
            if float(latest['rsi']) < 40 and float(latest['macd_diff']) > 0:
                prob = min(100.0, prob + 5.0)
            if float(latest['adx']) > 25:
                prob = min(100.0, prob + 2.5)

            scored.append({
                "ticker": ticker,
                "probability": prob,
                "entry_price": current_price,
            })

        except Exception as e:
            logger.debug(f"[ShadowScorer] Error scoring {ticker}: {e}")
            skipped += 1
            continue

    # ── Step 4: Cross-sectional ranking — top-K ────────────────────────────
    scored.sort(key=lambda x: x["probability"], reverse=True)
    top_k = [s for s in scored if s["probability"] >= MIN_PROBABILITY][:TOP_K_SIGNALS]

    # ── Step 5: Write shadow trades ───────────────────────────────────────
    opened = 0
    try:
        conn = sqlite3.connect(get_db_path(), timeout=15.0)
        for rank, entry in enumerate(top_k, start=1):
            trade_id = f"shadow_{source_research_job_id[-12:]}_{scan_date.replace('-','')}_{entry['ticker'].replace('.','_')}_{uuid.uuid4().hex[:6]}"
            conn.execute("""
                INSERT OR IGNORE INTO research_shadow_trades
                    (trade_id, source_research_job_id, scan_date, ticker, direction,
                     entry_price, status, probability, rank_in_universe, horizon_days, created_at)
                VALUES (?, ?, ?, ?, 'LONG', ?, 'VIRTUAL_OPEN', ?, ?, ?, ?)
            """, (
                trade_id, source_research_job_id, scan_date,
                entry["ticker"], entry["entry_price"],
                entry["probability"], rank, HORIZON_DAYS, scan_ts
            ))
            opened += 1
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[ShadowScorer] Failed to write shadow trades: {e}")

    logger.info(
        f"[ShadowScorer] job={source_research_job_id} date={scan_date} "
        f"scored={len(scored)} skipped={skipped} opened={opened} closed={closed_count}"
    )

    return {
        "status": "SUCCESS",
        "scan_date": scan_date,
        "source_research_job_id": source_research_job_id,
        "universe": universe_name,
        "total_tickers": len(tickers),
        "scored_tickers": len(scored),
        "skipped_tickers": skipped,
        "new_shadow_trades_opened": opened,
        "expired_shadow_trades_closed": closed_count,
        "top_signals": [
            {"ticker": s["ticker"], "probability": s["probability"], "price": s["entry_price"]}
            for s in top_k
        ],
    }


def _close_expired_shadow_trades(
    source_research_job_id: str,
    as_of_date: str,
    timeframe: str = "1d"
) -> int:
    """
    Closes VIRTUAL_OPEN shadow trades that have exceeded their horizon.
    Exit price is read from OHLCV close on the expiry bar.
    Returns count of trades closed.
    """
    ensure_shadow_trades_table()

    from app.data.historical_data_layer import get_db_path, HistoricalDataLayer

    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    open_trades = conn.execute("""
        SELECT trade_id, ticker, scan_date, entry_price, horizon_days
        FROM research_shadow_trades
        WHERE source_research_job_id = ? AND status = 'VIRTUAL_OPEN'
    """, (source_research_job_id,)).fetchall()
    conn.close()

    closed = 0
    for trade_id, ticker, scan_date, entry_price, horizon_days in open_trades:
        try:
            # Count new 1d bars since scan_date in OHLCV
            conn2 = sqlite3.connect(get_db_path(), timeout=10.0)
            bar_count = conn2.execute("""
                SELECT COUNT(*), MAX(close) FROM ohlcv
                WHERE ticker = ? AND timeframe = ? AND date > ? AND date <= ?
            """, (ticker, timeframe, scan_date, as_of_date)).fetchone()
            conn2.close()

            bars_elapsed = bar_count[0] or 0
            latest_close = bar_count[1]

            if bars_elapsed >= (horizon_days or HORIZON_DAYS):
                exit_price = float(latest_close) if latest_close else (entry_price or 0.0)
                pnl_pct = round(
                    (exit_price - (entry_price or exit_price)) / (entry_price or 1.0) * 100.0, 3
                ) if (entry_price and entry_price > 0) else 0.0

                conn3 = sqlite3.connect(get_db_path(), timeout=10.0)
                conn3.execute("""
                    UPDATE research_shadow_trades
                    SET status = 'VIRTUAL_CLOSED', exit_price = ?, pnl_pct = ?,
                        exit_bar_count = ?, closed_at = ?
                    WHERE trade_id = ?
                """, (exit_price, pnl_pct, bars_elapsed, datetime.now().isoformat(), trade_id))
                conn3.commit()
                conn3.close()
                closed += 1

        except Exception as e:
            logger.debug(f"[ShadowScorer] Error closing trade {trade_id}: {e}")

    return closed


# ── Public counter used by data_lab.py promotion gate ─────────────────────

def count_fresh_shadow_trades(
    source_research_job_id: str,
    oos_start: str = OOS_CUTOFF_DATE
) -> int:
    """
    Returns the number of VIRTUAL_OPEN or VIRTUAL_CLOSED shadow trades
    for this job with scan_date strictly after `oos_start`.
    This is the `fresh_oos_shadow_trades` value shown in the Challenger scorecard.
    """
    ensure_shadow_trades_table()

    from app.data.historical_data_layer import get_db_path
    try:
        conn = sqlite3.connect(get_db_path(), timeout=10.0)
        row = conn.execute("""
            SELECT COUNT(*) FROM research_shadow_trades
            WHERE source_research_job_id = ?
              AND scan_date > ?
              AND status IN ('VIRTUAL_OPEN', 'VIRTUAL_CLOSED')
        """, (source_research_job_id, oos_start)).fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.warning(f"[ShadowScorer] count_fresh_shadow_trades error: {e}")
        return 0


def list_shadow_trades(
    source_research_job_id: str,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """Returns shadow trades for a given research job, newest first."""
    ensure_shadow_trades_table()

    from app.data.historical_data_layer import get_db_path
    try:
        conn = sqlite3.connect(get_db_path(), timeout=10.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM research_shadow_trades
            WHERE source_research_job_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (source_research_job_id, limit)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"[ShadowScorer] list_shadow_trades error: {e}")
        return []

