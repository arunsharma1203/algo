"""
Option Sniffer: Dedicated Additive Database Schema & Storage Manager.

Maintains strict database separation:
  - Connects strictly to canonical database via get_db_path().
  - Creates SEPARATE additive tables for Option Sniffer only.
  - NEVER modifies or alters existing tables (ml_trade_history, consensus_trade_history, etc.).
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)

_DB_LOCK = threading.RLock()


def init_option_sniffer_tables() -> None:
    """
    Initializes all dedicated additive tables for the Option Sniffer subsystem.
    Thread-safe and idempotent (CREATE TABLE IF NOT EXISTS).
    """
    with _DB_LOCK:
        try:
            conn = sqlite3.connect(get_db_path(), timeout=10.0)
            cur = conn.cursor()

            # 1. Option Candidates Catalog (Tracked ATM/near-ATM options)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS option_sniffer_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_key TEXT UNIQUE NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    strike REAL NOT NULL,
                    option_type TEXT NOT NULL,
                    lot_size INTEGER NOT NULL,
                    discovered_at TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    metadata_json TEXT
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_osc_underlying ON option_sniffer_candidates(underlying, is_active);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_osc_expiry ON option_sniffer_candidates(expiry);")

            # 2. Option Model Predictions (Point-in-time model outputs)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS option_sniffer_predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prediction_id TEXT UNIQUE NOT NULL,
                    timestamp TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    strike REAL NOT NULL,
                    option_type TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    spot_price REAL NOT NULL,
                    option_ltp REAL NOT NULL,
                    bid REAL,
                    ask REAL,
                    spread_pct REAL,
                    delta REAL,
                    gamma REAL,
                    theta REAL,
                    vega REAL,
                    iv REAL,
                    raw_score REAL,
                    calibrated_prob REAL NOT NULL,
                    expected_net_return REAL NOT NULL,
                    direction TEXT NOT NULL,
                    features_json TEXT,
                    model_version TEXT NOT NULL
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_osp_time ON option_sniffer_predictions(timestamp);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_osp_contract ON option_sniffer_predictions(instrument_key);")

            # 3. Option Sniffer Signals (State machine tracked signals)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS option_sniffer_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    strike REAL NOT NULL,
                    option_type TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    calibrated_prob REAL NOT NULL,
                    expected_net_return REAL NOT NULL,
                    spot_price REAL NOT NULL,
                    option_ask REAL NOT NULL,
                    option_bid REAL NOT NULL,
                    spread_pct REAL NOT NULL,
                    delta REAL,
                    iv REAL,
                    oi REAL,
                    volume INTEGER,
                    hurdles_passed INTEGER DEFAULT 1,
                    rejection_reasons TEXT,
                    telegram_sent INTEGER DEFAULT 0,
                    telegram_sent_at TEXT,
                    updated_at TEXT NOT NULL
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_oss_status ON option_sniffer_signals(status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_oss_created ON option_sniffer_signals(created_at);")

            # 4. Paper Execution Trades (Full paper trade lifecycle & P&L)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS option_sniffer_paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT UNIQUE NOT NULL,
                    signal_id TEXT,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    option_type TEXT NOT NULL,
                    strike REAL NOT NULL,
                    expiry TEXT NOT NULL,
                    lot_size INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    entry_time TEXT NOT NULL,
                    entry_bid REAL,
                    entry_ask REAL NOT NULL,
                    entry_price_used REAL NOT NULL,
                    exit_time TEXT,
                    exit_bid REAL,
                    exit_ask REAL,
                    exit_price_used REAL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    peak_price REAL,
                    trough_price REAL,
                    mae_pct REAL,
                    mfe_pct REAL,
                    gross_pnl REAL DEFAULT 0.0,
                    charges REAL DEFAULT 0.0,
                    slippage REAL DEFAULT 0.0,
                    net_pnl REAL DEFAULT 0.0,
                    net_pnl_pct REAL DEFAULT 0.0,
                    holding_duration_seconds INTEGER,
                    exit_reason TEXT,
                    model_version TEXT,
                    signal_version TEXT,
                    data_freshness TEXT,
                    telegram_exit_sent INTEGER DEFAULT 0
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ospt_status ON option_sniffer_paper_trades(status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ospt_entry_time ON option_sniffer_paper_trades(entry_time);")

            # 5. Operational Events Log
            cur.execute("""
                CREATE TABLE IF NOT EXISTS option_sniffer_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    category TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ose_time ON option_sniffer_events(timestamp);")

            # 6. Periodic P&L Snapshots
            cur.execute("""
                CREATE TABLE IF NOT EXISTS option_sniffer_pnl_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    date TEXT NOT NULL,
                    open_trades_count INTEGER NOT NULL,
                    closed_trades_count INTEGER NOT NULL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    total_charges REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    win_rate REAL,
                    profit_factor REAL
                );
            """)

            # 7. Streaming & Ingestion Health
            cur.execute("""
                CREATE TABLE IF NOT EXISTS option_sniffer_data_health (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    websocket_connected INTEGER NOT NULL,
                    subscribed_keys_count INTEGER NOT NULL,
                    message_rate_per_sec REAL,
                    last_message_latency_ms INTEGER,
                    stale_contracts_count INTEGER,
                    status TEXT NOT NULL,
                    details_json TEXT
                );
            """)

            conn.commit()
            conn.close()
            logger.info("✅ Option Sniffer dedicated additive SQLite schema initialized.")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Option Sniffer database schema: {e}", exc_info=True)
            raise


# Auto-initialize tables upon module import
init_option_sniffer_tables()


class OptionDB:
    """Thread-safe accessor for all Option Sniffer persistence operations."""

    @staticmethod
    def get_connection() -> sqlite3.Connection:
        conn = sqlite3.connect(get_db_path(), timeout=15.0)
        conn.execute("PRAGMA busy_timeout = 15000;")
        conn.row_factory = sqlite3.Row
        return conn

    @classmethod
    def save_candidate_contract(cls, candidate: Dict[str, Any]) -> bool:
        """Upserts an option candidate contract in the catalog."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    conn.execute("""
                        INSERT INTO option_sniffer_candidates (
                            instrument_key, trading_symbol, underlying, expiry, strike,
                            option_type, lot_size, discovered_at, is_active, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(instrument_key) DO UPDATE SET
                            is_active = excluded.is_active,
                            metadata_json = excluded.metadata_json,
                            lot_size = excluded.lot_size
                    """, (
                        candidate["instrument_key"],
                        candidate["trading_symbol"],
                        candidate["underlying"],
                        candidate["expiry"],
                        candidate["strike"],
                        candidate["option_type"],
                        candidate["lot_size"],
                        candidate.get("discovered_at") or datetime.now().isoformat(),
                        1 if candidate.get("is_active", True) else 0,
                        json.dumps(candidate.get("metadata", {}))
                    ))
                conn.close()
                return True
            except Exception as e:
                logger.error(f"[OptionDB] Failed to save candidate contract {candidate.get('instrument_key')}: {e}")
                return False

    @classmethod
    def get_active_candidates(cls, underlying: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves active monitored option contracts."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                query = "SELECT * FROM option_sniffer_candidates WHERE is_active = 1"
                params = []
                if underlying:
                    query += " AND underlying = ?"
                    params.append(underlying.upper().strip())
                query += " ORDER BY expiry ASC, strike ASC"
                rows = conn.execute(query, params).fetchall()
                conn.close()
                return [dict(r) for r in rows]
            except Exception as e:
                logger.error(f"[OptionDB] Failed to get active candidates: {e}")
                return []

    @classmethod
    def log_prediction(cls, pred: Dict[str, Any]) -> bool:
        """Records point-in-time option model prediction."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    conn.execute("""
                        INSERT OR REPLACE INTO option_sniffer_predictions (
                            prediction_id, timestamp, instrument_key, trading_symbol, underlying,
                            strike, option_type, expiry, spot_price, option_ltp, bid, ask,
                            spread_pct, delta, gamma, theta, vega, iv, raw_score, calibrated_prob,
                            expected_net_return, direction, features_json, model_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        pred["prediction_id"],
                        pred.get("timestamp") or datetime.now().isoformat(),
                        pred["instrument_key"],
                        pred["trading_symbol"],
                        pred["underlying"],
                        pred["strike"],
                        pred["option_type"],
                        pred["expiry"],
                        pred["spot_price"],
                        pred["option_ltp"],
                        pred.get("bid"),
                        pred.get("ask"),
                        pred.get("spread_pct"),
                        pred.get("delta"),
                        pred.get("gamma"),
                        pred.get("theta"),
                        pred.get("vega"),
                        pred.get("iv"),
                        pred.get("raw_score"),
                        pred["calibrated_prob"],
                        pred["expected_net_return"],
                        pred["direction"],
                        json.dumps(pred.get("features", {})),
                        pred.get("model_version", "v1.0")
                    ))
                conn.close()
                return True
            except Exception as e:
                logger.error(f"[OptionDB] Failed to log prediction: {e}")
                return False

    @classmethod
    def get_recent_predictions(cls, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieves recent option predictions."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                rows = conn.execute("SELECT * FROM option_sniffer_predictions ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
                conn.close()
                return [dict(r) for r in rows]
            except Exception as e:
                logger.error(f"[OptionDB] Failed to get predictions: {e}")
                return []

    @classmethod
    def save_signal(cls, signal: Dict[str, Any]) -> bool:
        """Saves or updates an option sniffer signal."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                now_iso = datetime.now().isoformat()
                with conn:
                    conn.execute("""
                        INSERT INTO option_sniffer_signals (
                            signal_id, created_at, instrument_key, trading_symbol, underlying,
                            strike, option_type, expiry, direction, status, calibrated_prob,
                            expected_net_return, spot_price, option_ask, option_bid, spread_pct,
                            delta, iv, oi, volume, hurdles_passed, rejection_reasons,
                            telegram_sent, telegram_sent_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(signal_id) DO UPDATE SET
                            status = excluded.status,
                            option_ask = excluded.option_ask,
                            option_bid = excluded.option_bid,
                            spread_pct = excluded.spread_pct,
                            calibrated_prob = excluded.calibrated_prob,
                            expected_net_return = excluded.expected_net_return,
                            hurdles_passed = excluded.hurdles_passed,
                            rejection_reasons = excluded.rejection_reasons,
                            telegram_sent = CASE WHEN excluded.telegram_sent = 1 THEN 1 ELSE option_sniffer_signals.telegram_sent END,
                            telegram_sent_at = COALESCE(excluded.telegram_sent_at, option_sniffer_signals.telegram_sent_at),
                            updated_at = excluded.updated_at
                    """, (
                        signal["signal_id"],
                        signal.get("created_at") or now_iso,
                        signal["instrument_key"],
                        signal["trading_symbol"],
                        signal["underlying"],
                        signal["strike"],
                        signal["option_type"],
                        signal["expiry"],
                        signal["direction"],
                        signal["status"],
                        signal["calibrated_prob"],
                        signal["expected_net_return"],
                        signal["spot_price"],
                        signal["option_ask"],
                        signal["option_bid"],
                        signal["spread_pct"],
                        signal.get("delta"),
                        signal.get("iv"),
                        signal.get("oi"),
                        signal.get("volume"),
                        1 if signal.get("hurdles_passed", True) else 0,
                        json.dumps(signal.get("rejection_reasons", [])),
                        1 if signal.get("telegram_sent") else 0,
                        signal.get("telegram_sent_at"),
                        now_iso
                    ))
                conn.close()
                return True
            except Exception as e:
                logger.error(f"[OptionDB] Failed to save signal {signal.get('signal_id')}: {e}")
                return False

    @classmethod
    def get_recent_signals(cls, limit: int = 50, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves recent option signals."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                query = "SELECT * FROM option_sniffer_signals"
                params = []
                if status:
                    query += " WHERE status = ?"
                    params.append(status)
                query += " ORDER BY created_at DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(query, params).fetchall()
                conn.close()
                return [dict(r) for r in rows]
            except Exception as e:
                logger.error(f"[OptionDB] Failed to get signals: {e}")
                return []

    @classmethod
    def create_paper_trade(cls, trade: Dict[str, Any]) -> bool:
        """Opens a new paper trade at current ask price."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    conn.execute("""
                        INSERT OR REPLACE INTO option_sniffer_paper_trades (
                            trade_id, signal_id, instrument_key, trading_symbol, underlying,
                            option_type, strike, expiry, lot_size, quantity, entry_time,
                            entry_bid, entry_ask, entry_price_used, status, peak_price,
                            trough_price, model_version, signal_version, data_freshness
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
                    """, (
                        trade["trade_id"],
                        trade.get("signal_id"),
                        trade["instrument_key"],
                        trade["trading_symbol"],
                        trade["underlying"],
                        trade["option_type"],
                        trade["strike"],
                        trade["expiry"],
                        trade["lot_size"],
                        trade["quantity"],
                        trade.get("entry_time") or datetime.now().isoformat(),
                        trade.get("entry_bid"),
                        trade["entry_ask"],
                        trade["entry_ask"],  # Explicit rule: BUY at ASK
                        trade["entry_ask"],  # Initial peak price
                        trade["entry_ask"],  # Initial trough price
                        trade.get("model_version"),
                        trade.get("signal_version"),
                        trade.get("data_freshness", "REAL_TIME")
                    ))
                conn.close()
                cls.log_event("TRADE", "OPEN", f"Opened paper trade {trade['trade_id']} for {trade['trading_symbol']} @ ₹{trade['entry_ask']:.2f}", trade)
                return True
            except Exception as e:
                logger.error(f"[OptionDB] Failed to create paper trade {trade.get('trade_id')}: {e}")
                return False

    @classmethod
    def close_paper_trade(cls, trade_id: str, close_data: Dict[str, Any]) -> bool:
        """Closes an active paper trade at current bid price."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    conn.execute("""
                        UPDATE option_sniffer_paper_trades SET
                            status = 'CLOSED',
                            exit_time = ?,
                            exit_bid = ?,
                            exit_ask = ?,
                            exit_price_used = ?,
                            peak_price = ?,
                            trough_price = ?,
                            mae_pct = ?,
                            mfe_pct = ?,
                            gross_pnl = ?,
                            charges = ?,
                            slippage = ?,
                            net_pnl = ?,
                            net_pnl_pct = ?,
                            holding_duration_seconds = ?,
                            exit_reason = ?,
                            telegram_exit_sent = ?
                        WHERE trade_id = ?
                    """, (
                        close_data.get("exit_time") or datetime.now().isoformat(),
                        close_data["exit_bid"],
                        close_data.get("exit_ask"),
                        close_data["exit_bid"],  # Explicit rule: SELL at BID
                        close_data.get("peak_price"),
                        close_data.get("trough_price"),
                        close_data.get("mae_pct"),
                        close_data.get("mfe_pct"),
                        close_data.get("gross_pnl", 0.0),
                        close_data.get("charges", 0.0),
                        close_data.get("slippage", 0.0),
                        close_data.get("net_pnl", 0.0),
                        close_data.get("net_pnl_pct", 0.0),
                        close_data.get("holding_duration_seconds", 0),
                        close_data.get("exit_reason", "MANUAL"),
                        1 if close_data.get("telegram_exit_sent") else 0,
                        trade_id
                    ))
                conn.close()
                cls.log_event("TRADE", "CLOSED", f"Closed paper trade {trade_id} @ ₹{close_data['exit_bid']:.2f} (Net ₹{close_data.get('net_pnl', 0.0):.2f})", close_data)
                return True
            except Exception as e:
                logger.error(f"[OptionDB] Failed to close paper trade {trade_id}: {e}")
                return False

    @classmethod
    def update_trade_extrema(cls, trade_id: str, peak_price: float, trough_price: float) -> bool:
        """Updates peak and trough prices on an open trade."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    conn.execute("""
                        UPDATE option_sniffer_paper_trades
                        SET peak_price = ?, trough_price = ?
                        WHERE trade_id = ?
                    """, (peak_price, trough_price, trade_id))
                conn.close()
                return True
            except Exception as e:
                logger.error(f"[OptionDB] Failed to update trade extrema for {trade_id}: {e}")
                return False

    @classmethod
    def get_open_paper_trades(cls) -> List[Dict[str, Any]]:
        """Retrieves currently open paper positions."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                rows = conn.execute("SELECT * FROM option_sniffer_paper_trades WHERE status = 'OPEN' ORDER BY entry_time DESC").fetchall()
                conn.close()
                return [dict(r) for r in rows]
            except Exception as e:
                logger.error(f"[OptionDB] Failed to get open trades: {e}")
                return []

    @classmethod
    def get_closed_paper_trades(cls, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieves closed paper trades history."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                rows = conn.execute("SELECT * FROM option_sniffer_paper_trades WHERE status = 'CLOSED' ORDER BY exit_time DESC LIMIT ?", (limit,)).fetchall()
                conn.close()
                return [dict(r) for r in rows]
            except Exception as e:
                logger.error(f"[OptionDB] Failed to get closed trades: {e}")
                return []

    @classmethod
    def get_pnl_summary(cls) -> Dict[str, Any]:
        """Calculates cumulative and today's paper P&L summary."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                today_str = datetime.now().strftime("%Y-%m-%d")

                # All closed
                c_row = conn.execute("""
                    SELECT
                        COUNT(*) as total_closed,
                        SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
                        SUM(CASE WHEN net_pnl < 0 THEN 1 ELSE 0 END) as losses,
                        SUM(gross_pnl) as total_gross,
                        SUM(charges) as total_charges,
                        SUM(slippage) as total_slippage,
                        SUM(net_pnl) as total_net,
                        AVG(holding_duration_seconds) as avg_duration,
                        MAX(mfe_pct) as max_mfe,
                        MIN(mae_pct) as max_mae
                    FROM option_sniffer_paper_trades
                    WHERE status = 'CLOSED'
                """).fetchone()

                # Today's closed
                t_row = conn.execute("""
                    SELECT
                        COUNT(*) as today_closed,
                        SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as today_wins,
                        SUM(net_pnl) as today_net
                    FROM option_sniffer_paper_trades
                    WHERE status = 'CLOSED' AND exit_time >= ?
                """, (f"{today_str} 00:00:00",)).fetchone()

                # Open positions count
                open_cnt = conn.execute("SELECT COUNT(*) FROM option_sniffer_paper_trades WHERE status = 'OPEN'").fetchone()[0]

                conn.close()

                total_closed = c_row[0] or 0
                wins = c_row[1] or 0
                losses = c_row[2] or 0
                win_rate = round((wins / total_closed * 100.0), 1) if total_closed > 0 else 0.0

                pos_net = 0.0
                neg_net = 0.0
                if total_closed > 0:
                    conn2 = cls.get_connection()
                    rows = conn2.execute("SELECT net_pnl FROM option_sniffer_paper_trades WHERE status = 'CLOSED'").fetchall()
                    conn2.close()
                    for r in rows:
                        val = float(r[0] or 0.0)
                        if val > 0:
                            pos_net += val
                        elif val < 0:
                            neg_net += abs(val)

                profit_factor = round(pos_net / neg_net, 2) if neg_net > 0 else (round(pos_net, 2) if pos_net > 0 else 1.0)

                return {
                    "total_closed_trades": total_closed,
                    "wins": wins,
                    "losses": losses,
                    "win_rate_pct": win_rate,
                    "profit_factor": profit_factor,
                    "gross_pnl": round(float(c_row[3] or 0.0), 2),
                    "total_charges": round(float(c_row[4] or 0.0), 2),
                    "total_slippage": round(float(c_row[5] or 0.0), 2),
                    "total_net_pnl": round(float(c_row[6] or 0.0), 2),
                    "avg_duration_seconds": int(c_row[7] or 0),
                    "max_mfe_pct": round(float(c_row[8] or 0.0), 2),
                    "max_mae_pct": round(float(c_row[9] or 0.0), 2),
                    "today_closed_trades": t_row[0] or 0,
                    "today_wins": t_row[1] or 0,
                    "today_net_pnl": round(float(t_row[2] or 0.0), 2),
                    "open_trades_count": open_cnt,
                }
            except Exception as e:
                logger.error(f"[OptionDB] Failed to get P&L summary: {e}")
                return {
                    "total_closed_trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0,
                    "profit_factor": 1.0, "total_net_pnl": 0.0, "open_trades_count": 0
                }

    @classmethod
    def log_event(cls, category: str, event_type: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        """Appends an event to option_sniffer_events."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    conn.execute("""
                        INSERT INTO option_sniffer_events (timestamp, category, event_type, message, details_json)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        datetime.now().isoformat(),
                        category,
                        event_type,
                        message,
                        json.dumps(details or {})
                    ))
                conn.close()
            except Exception as e:
                logger.debug(f"[OptionDB] Failed to log event: {e}")

    @classmethod
    def record_data_health(cls, health: Dict[str, Any]) -> None:
        """Records data ingestion and WebSocket health snapshot."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    conn.execute("""
                        INSERT INTO option_sniffer_data_health (
                            timestamp, websocket_connected, subscribed_keys_count,
                            message_rate_per_sec, last_message_latency_ms,
                            stale_contracts_count, status, details_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        datetime.now().isoformat(),
                        1 if health.get("websocket_connected") else 0,
                        health.get("subscribed_keys_count", 0),
                        health.get("message_rate_per_sec", 0.0),
                        health.get("last_message_latency_ms", 0),
                        health.get("stale_contracts_count", 0),
                        health.get("status", "HEALTHY"),
                        json.dumps(health.get("details", {}))
                    ))
                conn.close()
            except Exception as e:
                logger.debug(f"[OptionDB] Failed to record data health: {e}")
