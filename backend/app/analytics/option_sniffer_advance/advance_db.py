"""
Option Sniffer Advance: 100% Isolated Sandboxed Database Layer.

Maintains strict database separation:
  - Creates SEPARATE additive tables (`option_sniffer_advance_*`) for Option Sniffer Advance only.
  - NEVER writes to or alters `option_sniffer_paper_trades`, `option_sniffer_signals`,
    `ml_trade_history`, or `consensus_trade_history`.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.analytics.option_sniffer_advance.advance_constants import (
    BOOK_LONG_BUY,
    BOOK_SHORT_SELL,
    INTROSPECTION_PAUSE_MINUTES,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DAILY_COMBINED_LOSS_INR,
    to_ist_naive,
)
from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)

_ADVANCE_DB_LOCK = threading.RLock()


def init_option_sniffer_advance_tables() -> None:
    """
    Initializes dedicated additive tables for Option Sniffer Advance.
    Thread-safe and idempotent.
    """
    with _ADVANCE_DB_LOCK:
        try:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            cur = conn.cursor()

            # 1. Dual-Book Signals (Long Sniper & Short Theta/VRP/Exhaustion Signals)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS option_sniffer_advance_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL,
                    book_type TEXT NOT NULL,
                    strategy_mode TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    strike REAL NOT NULL,
                    option_type TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    lot_size INTEGER NOT NULL,
                    spot_price REAL NOT NULL,
                    option_bid REAL NOT NULL,
                    option_ask REAL NOT NULL,
                    spread_pct REAL,
                    delta REAL,
                    gamma REAL,
                    theta REAL,
                    vega REAL,
                    iv REAL,
                    oi REAL,
                    volume REAL,
                    otm_strikes REAL,
                    adx_14 REAL,
                    kaufman_er REAL,
                    atr_ratio REAL,
                    net_gex REAL,
                    vrp REAL,
                    synthetic_basis_bps REAL,
                    pcr_oi REAL,
                    calibrated_prob REAL,
                    expected_net_return REAL,
                    hurdles_passed INTEGER DEFAULT 1,
                    rejection_reasons TEXT,
                    regime_telemetry_json TEXT
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_osas_created ON option_sniffer_advance_signals(created_at DESC);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_osas_book ON option_sniffer_advance_signals(book_type, status);"
            )

            # 2. Dual-Book Paper Trades (Tracks both 1-Lot BUY and 1-Lot SELL paper positions)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS option_sniffer_advance_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT UNIQUE NOT NULL,
                    signal_id TEXT,
                    book_type TEXT NOT NULL,
                    strategy_mode TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    option_type TEXT NOT NULL,
                    strike REAL NOT NULL,
                    expiry TEXT NOT NULL,
                    lot_size INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    entry_time TEXT NOT NULL,
                    entry_bid REAL NOT NULL,
                    entry_ask REAL NOT NULL,
                    entry_price_used REAL NOT NULL,
                    entry_spot_price REAL,
                    exit_time TEXT,
                    exit_bid REAL,
                    exit_ask REAL,
                    exit_price_used REAL,
                    status TEXT DEFAULT 'OPEN',
                    peak_price REAL,
                    trough_price REAL,
                    trailing_stop REAL,
                    mfe_pct REAL DEFAULT 0.0,
                    mae_pct REAL DEFAULT 0.0,
                    stop_loss_pct REAL,
                    target_pct REAL,
                    gross_pnl REAL DEFAULT 0.0,
                    charges REAL DEFAULT 0.0,
                    slippage REAL DEFAULT 0.0,
                    net_pnl REAL DEFAULT 0.0,
                    net_pnl_pct REAL DEFAULT 0.0,
                    estimated_margin REAL DEFAULT 0.0,
                    holding_duration_seconds INTEGER DEFAULT 0,
                    exit_reason TEXT,
                    regime_context_json TEXT
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_osat_status ON option_sniffer_advance_trades(status, book_type);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_osat_entry ON option_sniffer_advance_trades(entry_time DESC);"
            )

            # 3. Cross-Sectional Chain Intelligence Snapshots
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS option_sniffer_advance_chain_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    spot_price REAL NOT NULL,
                    synthetic_future REAL,
                    synthetic_basis_bps REAL,
                    atm_iv REAL,
                    rv_15m_ann REAL,
                    vrp REAL,
                    iv_skew_25d REAL,
                    net_gex REAL,
                    gex_regime TEXT,
                    pcr_oi REAL,
                    pcr_volume REAL,
                    call_wall_strike REAL,
                    put_wall_strike REAL,
                    max_pain_strike REAL,
                    chain_obi REAL,
                    adx_14 REAL,
                    kaufman_er REAL,
                    atr_ratio REAL,
                    active_strategy_mode TEXT
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_osacs_ts ON option_sniffer_advance_chain_snapshots(underlying, timestamp DESC);"
            )

            # 4. Sandbox Operational Events
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS option_sniffer_advance_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT
                );
                """
            )

            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[AdvanceDB] Failed to initialize Option Sniffer Advance tables: {e}")


class AdvanceDB:
    """Thread-safe data access layer for Option Sniffer Advance."""

    @staticmethod
    def get_connection() -> sqlite3.Connection:
        conn = sqlite3.connect(get_db_path(), timeout=15.0)
        conn.row_factory = sqlite3.Row
        return conn

    @classmethod
    def log_event(
        cls,
        category: str,
        severity: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        with _ADVANCE_DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    conn.execute(
                        """
                        INSERT INTO option_sniffer_advance_events (timestamp, category, severity, message, details_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            to_ist_naive().isoformat(),
                            category,
                            severity,
                            message,
                            json.dumps(details or {}),
                        ),
                    )
                conn.close()
            except Exception as e:
                logger.debug(f"[AdvanceDB] Event log note: {e}")

    @classmethod
    def save_signal(cls, sig: Dict[str, Any]) -> bool:
        with _ADVANCE_DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO option_sniffer_advance_signals (
                            signal_id, created_at, book_type, strategy_mode, direction, status,
                            instrument_key, trading_symbol, underlying, strike, option_type, expiry,
                            lot_size, spot_price, option_bid, option_ask, spread_pct, delta, gamma,
                            theta, vega, iv, oi, volume, otm_strikes, adx_14, kaufman_er, atr_ratio,
                            net_gex, vrp, synthetic_basis_bps, pcr_oi, calibrated_prob,
                            expected_net_return, hurdles_passed, rejection_reasons, regime_telemetry_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sig["signal_id"],
                            sig.get("created_at") or to_ist_naive().isoformat(),
                            sig.get("book_type", BOOK_LONG_BUY),
                            sig.get("strategy_mode", "WAVE_1_GAMMA_SNIPER"),
                            sig.get("direction", "BUY_CE"),
                            sig.get("status", "WATCH"),
                            sig["instrument_key"],
                            sig["trading_symbol"],
                            sig["underlying"],
                            float(sig["strike"]),
                            sig["option_type"],
                            sig["expiry"],
                            int(sig.get("lot_size") or 65),
                            float(sig.get("spot_price") or 0.0),
                            float(sig.get("option_bid") or 0.0),
                            float(sig.get("option_ask") or 0.0),
                            float(sig.get("spread_pct") or 0.0),
                            float(sig.get("delta") or 0.0),
                            float(sig.get("gamma") or 0.0),
                            float(sig.get("theta") or 0.0),
                            float(sig.get("vega") or 0.0),
                            float(sig.get("iv") or 0.0),
                            float(sig.get("oi") or 0.0),
                            float(sig.get("volume") or 0.0),
                            float(sig.get("otm_strikes") or 0.0),
                            float(sig.get("adx_14") or 0.0),
                            float(sig.get("kaufman_er") or 0.0),
                            float(sig.get("atr_ratio") or 1.0),
                            float(sig.get("net_gex") or 0.0),
                            float(sig.get("vrp") or 0.0),
                            float(sig.get("synthetic_basis_bps") or 0.0),
                            float(sig.get("pcr_oi") or 1.0),
                            float(sig.get("calibrated_prob") or 0.5),
                            float(sig.get("expected_net_return") or 0.0),
                            1 if sig.get("hurdles_passed") else 0,
                            json.dumps(sig.get("rejection_reasons") or []),
                            json.dumps(sig.get("regime_telemetry") or {}),
                        ),
                    )
                conn.close()
                return True
            except Exception as e:
                logger.error(f"[AdvanceDB] Failed to save signal: {e}")
                return False

    @classmethod
    def get_recent_signals(
        cls,
        limit: int = 50,
        book_type: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with _ADVANCE_DB_LOCK:
            try:
                conn = cls.get_connection()
                q = "SELECT * FROM option_sniffer_advance_signals WHERE 1=1"
                params: List[Any] = []
                if book_type:
                    q += " AND book_type = ?"
                    params.append(book_type)
                if status:
                    q += " AND status = ?"
                    params.append(status)
                q += " ORDER BY created_at DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(q, tuple(params)).fetchall()
                conn.close()
                out = []
                for r in rows:
                    d = dict(r)
                    try:
                        d["rejection_reasons"] = json.loads(d.get("rejection_reasons") or "[]")
                    except Exception:
                        d["rejection_reasons"] = []
                    try:
                        d["regime_telemetry"] = json.loads(d.get("regime_telemetry_json") or "{}")
                    except Exception:
                        d["regime_telemetry"] = {}
                    out.append(d)
                return out
            except Exception as e:
                logger.error(f"[AdvanceDB] Failed to fetch signals: {e}")
                return []

    @classmethod
    def create_trade(cls, trade: Dict[str, Any]) -> bool:
        with _ADVANCE_DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO option_sniffer_advance_trades (
                            trade_id, signal_id, book_type, strategy_mode, direction,
                            instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                            lot_size, quantity, entry_time, entry_bid, entry_ask, entry_price_used,
                            entry_spot_price, status, peak_price, trough_price, trailing_stop,
                            stop_loss_pct, target_pct, estimated_margin, regime_context_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            trade["trade_id"],
                            trade.get("signal_id"),
                            trade["book_type"],
                            trade["strategy_mode"],
                            trade["direction"],
                            trade["instrument_key"],
                            trade["trading_symbol"],
                            trade["underlying"],
                            trade["option_type"],
                            float(trade["strike"]),
                            trade["expiry"],
                            int(trade["lot_size"]),
                            int(trade["quantity"]),
                            trade["entry_time"],
                            float(trade["entry_bid"]),
                            float(trade["entry_ask"]),
                            float(trade["entry_price_used"]),
                            float(trade.get("entry_spot_price") or 0.0),
                            float(trade.get("peak_price") or trade["entry_price_used"]),
                            float(trade.get("trough_price") or trade["entry_price_used"]),
                            trade.get("trailing_stop"),
                            float(trade.get("stop_loss_pct") or 16.0),
                            float(trade.get("target_pct") or 35.0),
                            float(trade.get("estimated_margin") or 0.0),
                            json.dumps(trade.get("regime_context") or {}),
                        ),
                    )
                conn.close()
                return True
            except Exception as e:
                logger.error(f"[AdvanceDB] Failed to create trade: {e}")
                return False

    @classmethod
    def update_open_trade(
        cls,
        trade_id: str,
        peak_price: float,
        trough_price: float,
        mfe_pct: float,
        mae_pct: float,
        trailing_stop: Optional[float] = None,
        gross_pnl: Optional[float] = None,
        net_pnl: Optional[float] = None,
        net_pnl_pct: Optional[float] = None,
    ) -> bool:
        with _ADVANCE_DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    conn.execute(
                        """
                        UPDATE option_sniffer_advance_trades
                        SET peak_price = ?,
                            trough_price = ?,
                            mfe_pct = ?,
                            mae_pct = ?,
                            trailing_stop = COALESCE(?, trailing_stop),
                            gross_pnl = COALESCE(?, gross_pnl),
                            net_pnl = COALESCE(?, net_pnl),
                            net_pnl_pct = COALESCE(?, net_pnl_pct)
                        WHERE trade_id = ? AND status = 'OPEN'
                        """,
                        (
                            peak_price,
                            trough_price,
                            mfe_pct,
                            mae_pct,
                            trailing_stop,
                            gross_pnl,
                            net_pnl,
                            net_pnl_pct,
                            trade_id,
                        ),
                    )
                conn.close()
                return True
            except Exception as e:
                logger.error(f"[AdvanceDB] Failed to update open trade {trade_id}: {e}")
                return False

    @classmethod
    def close_trade(cls, trade_id: str, close_data: Dict[str, Any]) -> bool:
        with _ADVANCE_DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    cur = conn.execute(
                        """
                        UPDATE option_sniffer_advance_trades
                        SET status = 'CLOSED',
                            exit_time = ?,
                            exit_bid = ?,
                            exit_ask = ?,
                            exit_price_used = ?,
                            peak_price = ?,
                            trough_price = ?,
                            mfe_pct = ?,
                            mae_pct = ?,
                            gross_pnl = ?,
                            charges = ?,
                            slippage = ?,
                            net_pnl = ?,
                            net_pnl_pct = ?,
                            holding_duration_seconds = ?,
                            exit_reason = ?
                        WHERE trade_id = ? AND status = 'OPEN'
                        """,
                        (
                            close_data["exit_time"],
                            close_data["exit_bid"],
                            close_data["exit_ask"],
                            close_data["exit_price_used"],
                            close_data["peak_price"],
                            close_data["trough_price"],
                            close_data["mfe_pct"],
                            close_data["mae_pct"],
                            close_data["gross_pnl"],
                            close_data["charges"],
                            close_data["slippage"],
                            close_data["net_pnl"],
                            close_data["net_pnl_pct"],
                            close_data["holding_duration_seconds"],
                            close_data["exit_reason"],
                            trade_id,
                        ),
                    )
                    if cur.rowcount == 0:
                        return False
                conn.close()
                return True
            except Exception as e:
                logger.error(f"[AdvanceDB] Failed to close trade {trade_id}: {e}")
                return False

    @classmethod
    def get_open_trades(cls, book_type: Optional[str] = None) -> List[Dict[str, Any]]:
        with _ADVANCE_DB_LOCK:
            try:
                conn = cls.get_connection()
                if book_type:
                    rows = conn.execute(
                        "SELECT * FROM option_sniffer_advance_trades WHERE status = 'OPEN' AND book_type = ? ORDER BY entry_time ASC",
                        (book_type,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM option_sniffer_advance_trades WHERE status = 'OPEN' ORDER BY entry_time ASC"
                    ).fetchall()
                conn.close()
                return [dict(r) for r in rows]
            except Exception as e:
                logger.error(f"[AdvanceDB] Failed to get open trades: {e}")
                return []

    @classmethod
    def get_closed_trades(cls, limit: int = 100, book_type: Optional[str] = None) -> List[Dict[str, Any]]:
        with _ADVANCE_DB_LOCK:
            try:
                conn = cls.get_connection()
                if book_type:
                    rows = conn.execute(
                        "SELECT * FROM option_sniffer_advance_trades WHERE status = 'CLOSED' AND book_type = ? ORDER BY exit_time DESC LIMIT ?",
                        (book_type, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM option_sniffer_advance_trades WHERE status = 'CLOSED' ORDER BY exit_time DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                conn.close()
                return [dict(r) for r in rows]
            except Exception as e:
                logger.error(f"[AdvanceDB] Failed to get closed trades: {e}")
                return []

    @classmethod
    def get_recent_harvested_long_wave(
        cls,
        underlying: str,
        option_type: str,
        as_of_time: Optional[datetime] = None,
        min_mfe_pct: float = 15.0,
        cooldown_seconds: int = 3600,
    ) -> Optional[Dict[str, Any]]:
        """
        Checks if a Wave-1 Long trade on (underlying, option_type) closed as a winner
        with MFE >= min_mfe_pct within the last `cooldown_seconds` on the same day.
        Checks both `option_sniffer_advance_trades` AND read-only `option_sniffer_paper_trades`
        so Option Sniffer Advance immediately detects Wave-1 harvests in real time!
        """
        now_dt = to_ist_naive(as_of_time)
        date_str = now_dt.strftime("%Y-%m-%d")
        u_str = str(underlying or "NIFTY").upper().strip()
        o_str = str(option_type or "CE").upper().strip()

        with _ADVANCE_DB_LOCK:
            try:
                conn = cls.get_connection()
                candidates = []
                # 1. Check Advance Long Book
                r1 = conn.execute(
                    """
                    SELECT trade_id, trading_symbol, exit_time, mfe_pct, net_pnl
                    FROM option_sniffer_advance_trades
                    WHERE status = 'CLOSED'
                      AND book_type = ?
                      AND UPPER(underlying) = ?
                      AND UPPER(option_type) = ?
                      AND exit_time >= ?
                      AND COALESCE(mfe_pct, 0) >= ?
                      AND COALESCE(net_pnl, 0) > 0
                    ORDER BY exit_time DESC LIMIT 1
                    """,
                    (BOOK_LONG_BUY, u_str, o_str, f"{date_str} 00:00:00", min_mfe_pct),
                ).fetchone()
                if r1:
                    candidates.append(dict(r1))

                # 2. Also read-only check main Option Sniffer closed trades for same-day Wave-1 harvest
                try:
                    r2 = conn.execute(
                        """
                        SELECT trade_id, trading_symbol, exit_time, mfe_pct, net_pnl
                        FROM option_sniffer_paper_trades
                        WHERE status = 'CLOSED'
                          AND UPPER(underlying) = ?
                          AND UPPER(option_type) = ?
                          AND exit_time >= ?
                          AND COALESCE(mfe_pct, 0) >= ?
                          AND COALESCE(net_pnl, 0) > 0
                        ORDER BY exit_time DESC LIMIT 1
                        """,
                        (u_str, o_str, f"{date_str} 00:00:00", min_mfe_pct),
                    ).fetchone()
                    if r2:
                        candidates.append(dict(r2))
                except Exception:
                    pass

                conn.close()

                for row in candidates:
                    exit_str = str(row["exit_time"]).replace("T", " ")[:19]
                    exit_dt = datetime.fromisoformat(exit_str)
                    elapsed = (now_dt - exit_dt).total_seconds()
                    if 0 <= elapsed < cooldown_seconds:
                        return {
                            "trade_id": row["trade_id"],
                            "trading_symbol": row["trading_symbol"],
                            "exit_time": row["exit_time"],
                            "mfe_pct": float(row["mfe_pct"] or 0.0),
                            "net_pnl": float(row["net_pnl"] or 0.0),
                            "elapsed_seconds": int(elapsed),
                            "remaining_seconds": int(cooldown_seconds - elapsed),
                        }
                return None
            except Exception as e:
                logger.debug(f"[AdvanceDB] Harvested wave check note: {e}")
                return None

    @classmethod
    def get_dual_book_pnl_summary(cls) -> Dict[str, Any]:
        """Computes comprehensive P&L breakdown across Long Book, Short Book, and Combined Portfolio."""
        with _ADVANCE_DB_LOCK:
            try:
                conn = cls.get_connection()
                rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM option_sniffer_advance_trades WHERE status = 'CLOSED'"
                    ).fetchall()
                ]
                open_rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM option_sniffer_advance_trades WHERE status = 'OPEN'"
                    ).fetchall()
                ]
                conn.close()

                def _summarize(subset: List[Dict[str, Any]]) -> Dict[str, Any]:
                    cnt = len(subset)
                    wins = sum(1 for x in subset if float(x.get("net_pnl") or 0.0) > 0)
                    losses = sum(1 for x in subset if float(x.get("net_pnl") or 0.0) <= 0)
                    gross = round(sum(float(x.get("gross_pnl") or 0.0) for x in subset), 2)
                    charges = round(sum(float(x.get("charges") or 0.0) for x in subset), 2)
                    slippage = round(sum(float(x.get("slippage") or 0.0) for x in subset), 2)
                    net = round(sum(float(x.get("net_pnl") or 0.0) for x in subset), 2)
                    gw = sum(float(x.get("net_pnl") or 0.0) for x in subset if float(x.get("net_pnl") or 0.0) > 0)
                    gl = abs(sum(float(x.get("net_pnl") or 0.0) for x in subset if float(x.get("net_pnl") or 0.0) <= 0))
                    pf = round(gw / gl, 2) if gl > 0 else (99.9 if gw > 0 else 0.0)
                    wr = round((wins / cnt) * 100.0, 1) if cnt > 0 else 0.0
                    return {
                        "total_closed_trades": cnt,
                        "wins": wins,
                        "losses": losses,
                        "win_rate_pct": wr,
                        "profit_factor": pf,
                        "gross_pnl": gross,
                        "total_charges": charges,
                        "total_slippage": slippage,
                        "total_net_pnl": net,
                    }

                long_closed = [x for x in rows if x.get("book_type") == BOOK_LONG_BUY]
                short_closed = [x for x in rows if x.get("book_type") == BOOK_SHORT_SELL]

                today_str = to_ist_naive().strftime("%Y-%m-%d")
                today_closed = [x for x in rows if str(x.get("exit_time") or "").startswith(today_str)]
                today_net = round(sum(float(x.get("net_pnl") or 0.0) for x in today_closed), 2)

                return {
                    "combined": _summarize(rows),
                    "long_book": _summarize(long_closed),
                    "short_book": _summarize(short_closed),
                    "today_closed_trades": len(today_closed),
                    "today_net_pnl": today_net,
                    "open_trades_count": len(open_rows),
                    "open_long_count": sum(1 for x in open_rows if x.get("book_type") == BOOK_LONG_BUY),
                    "open_short_count": sum(1 for x in open_rows if x.get("book_type") == BOOK_SHORT_SELL),
                }
            except Exception as e:
                logger.error(f"[AdvanceDB] PnL summary error: {e}")
                return {
                    "combined": {"total_closed_trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0, "profit_factor": 0.0, "total_net_pnl": 0.0},
                    "long_book": {"total_closed_trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0, "profit_factor": 0.0, "total_net_pnl": 0.0},
                    "short_book": {"total_closed_trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0, "profit_factor": 0.0, "total_net_pnl": 0.0},
                    "today_closed_trades": 0,
                    "today_net_pnl": 0.0,
                    "open_trades_count": 0,
                }

    @classmethod
    def get_circuit_breaker_telemetry(cls, as_of_time: Optional[datetime] = None) -> Dict[str, Any]:
        now_dt = to_ist_naive(as_of_time)
        date_str = now_dt.strftime("%Y-%m-%d")
        with _ADVANCE_DB_LOCK:
            try:
                conn = cls.get_connection()
                rows = [
                    dict(r)
                    for r in conn.execute(
                        """
                        SELECT net_pnl, exit_time, book_type
                        FROM option_sniffer_advance_trades
                        WHERE status = 'CLOSED' AND exit_time >= ? AND exit_time <= ?
                        ORDER BY exit_time ASC
                        """,
                        (f"{date_str} 00:00:00", f"{date_str}T23:59:59"),
                    ).fetchall()
                ]
                conn.close()

                today_net = round(sum(float(r.get("net_pnl") or 0.0) for r in rows), 2)
                wins = sum(1 for r in rows if float(r.get("net_pnl") or 0.0) > 0)
                losses = sum(1 for r in rows if float(r.get("net_pnl") or 0.0) <= 0)

                consec_losses = 0
                last_loss_time = None
                for r in reversed(rows):
                    if float(r.get("net_pnl") or 0.0) <= 0:
                        consec_losses += 1
                        if last_loss_time is None:
                            last_loss_time = r.get("exit_time")
                    else:
                        break

                is_max_loss = today_net <= -abs(MAX_DAILY_COMBINED_LOSS_INR)
                is_pause = False
                pause_rem = 0
                if consec_losses >= MAX_CONSECUTIVE_LOSSES and last_loss_time:
                    try:
                        lt = datetime.fromisoformat(str(last_loss_time).replace("T", " ")[:19])
                        el = (now_dt - lt).total_seconds()
                        if 0 <= el < (INTROSPECTION_PAUSE_MINUTES * 60):
                            is_pause = True
                            pause_rem = int((INTROSPECTION_PAUSE_MINUTES * 60) - el)
                    except Exception:
                        pass

                return {
                    "date": date_str,
                    "total_closed_today": len(rows),
                    "wins_today": wins,
                    "losses_today": losses,
                    "today_net_pnl": today_net,
                    "consecutive_losses": consec_losses,
                    "is_max_loss_limit_hit": is_max_loss,
                    "is_pause_active": is_pause,
                    "pause_remaining_sec": pause_rem,
                    "is_session_halted": is_max_loss,
                    "max_daily_loss_inr": MAX_DAILY_COMBINED_LOSS_INR,
                    "halt_reason": (
                        f"Daily combined loss ceiling reached (₹{today_net:,.2f})"
                        if is_max_loss
                        else (f"Introspection pause active ({pause_rem}s remaining)" if is_pause else None)
                    ),
                }
            except Exception as e:
                logger.error(f"[AdvanceDB] Circuit breaker telemetry error: {e}")
                return {
                    "date": date_str,
                    "total_closed_today": 0,
                    "today_net_pnl": 0.0,
                    "consecutive_losses": 0,
                    "is_session_halted": False,
                    "is_pause_active": False,
                }

    @classmethod
    def save_chain_snapshot(cls, snap: Dict[str, Any]) -> None:
        with _ADVANCE_DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    conn.execute(
                        """
                        INSERT INTO option_sniffer_advance_chain_snapshots (
                            timestamp, underlying, spot_price, synthetic_future, synthetic_basis_bps,
                            atm_iv, rv_15m_ann, vrp, iv_skew_25d, net_gex, gex_regime,
                            pcr_oi, pcr_volume, call_wall_strike, put_wall_strike, max_pain_strike,
                            chain_obi, adx_14, kaufman_er, atr_ratio, active_strategy_mode
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snap.get("timestamp") or to_ist_naive().isoformat(),
                            snap.get("underlying", "NIFTY"),
                            float(snap.get("spot_price") or 0.0),
                            float(snap.get("synthetic_future") or 0.0),
                            float(snap.get("synthetic_basis_bps") or 0.0),
                            float(snap.get("atm_iv") or 0.0),
                            float(snap.get("rv_15m_ann") or 0.0),
                            float(snap.get("vrp") or 0.0),
                            float(snap.get("iv_skew_25d") or 0.0),
                            float(snap.get("net_gex") or 0.0),
                            snap.get("gex_regime", "NEUTRAL"),
                            float(snap.get("pcr_oi") or 1.0),
                            float(snap.get("pcr_volume") or 1.0),
                            float(snap.get("call_wall_strike") or 0.0),
                            float(snap.get("put_wall_strike") or 0.0),
                            float(snap.get("max_pain_strike") or 0.0),
                            float(snap.get("chain_obi") or 0.0),
                            float(snap.get("adx_14") or 0.0),
                            float(snap.get("kaufman_er") or 0.0),
                            float(snap.get("atr_ratio") or 1.0),
                            snap.get("active_strategy_mode", "MONITORING"),
                        ),
                    )
                conn.close()
            except Exception as e:
                logger.debug(f"[AdvanceDB] Chain snapshot note: {e}")
