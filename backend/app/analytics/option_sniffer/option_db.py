"""
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
from app.analytics.option_sniffer.option_constants import (
    to_ist_naive,
    ENABLE_APEX_QUANT_ENGINE,
    PEAK_PROFIT_LOCK_TRIGGER_INR,
    PEAK_PROFIT_GIVEBACK_MAX_RATIO,
)

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
                    telegram_exit_sent INTEGER DEFAULT 0,
                    engine_version TEXT DEFAULT 'V1_CLASSIC',
                    dynamic_sl_pct REAL,
                    dynamic_target_pct REAL,
                    spot_atr REAL,
                    delta_used REAL,
                    entry_spot_price REAL
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ospt_status ON option_sniffer_paper_trades(status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ospt_entry_time ON option_sniffer_paper_trades(entry_time);")

            # Dynamic Paper Engine 2.0 additive column migration
            paper_cols = [c[1] for c in cur.execute("PRAGMA table_info(option_sniffer_paper_trades);").fetchall()]
            if "engine_version" not in paper_cols:
                cur.execute("ALTER TABLE option_sniffer_paper_trades ADD COLUMN engine_version TEXT DEFAULT 'V1_CLASSIC';")
            if "dynamic_sl_pct" not in paper_cols:
                cur.execute("ALTER TABLE option_sniffer_paper_trades ADD COLUMN dynamic_sl_pct REAL;")
            if "dynamic_target_pct" not in paper_cols:
                cur.execute("ALTER TABLE option_sniffer_paper_trades ADD COLUMN dynamic_target_pct REAL;")
            if "spot_atr" not in paper_cols:
                cur.execute("ALTER TABLE option_sniffer_paper_trades ADD COLUMN spot_atr REAL;")
            if "delta_used" not in paper_cols:
                cur.execute("ALTER TABLE option_sniffer_paper_trades ADD COLUMN delta_used REAL;")
            if "entry_spot_price" not in paper_cols:
                cur.execute("ALTER TABLE option_sniffer_paper_trades ADD COLUMN entry_spot_price REAL;")

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

            # 8. Paper Risk Ledger & Persistent Daily Halt Latch
            cur.execute("""
                CREATE TABLE IF NOT EXISTS option_sniffer_paper_risk_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_date TEXT UNIQUE NOT NULL,
                    starting_capital REAL DEFAULT 15000.0,
                    realized_pnl REAL DEFAULT 0.0,
                    unrealized_pnl REAL DEFAULT 0.0,
                    total_exposure REAL DEFAULT 0.0,
                    circuit_breaker_tripped INTEGER DEFAULT 0,
                    trip_reason TEXT,
                    halt_latch INTEGER DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
            """)

            # 9. Continuous Index OHLCV (Authoritative completed-bar storage for Wilder ADX & Regime Gate)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS continuous_index_ohlcv (
                    ticker TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER DEFAULT 0,
                    tick_count INTEGER DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'LOCAL_AGGREGATOR',
                    is_complete INTEGER DEFAULT 1,
                    is_synthetic INTEGER DEFAULT 0,
                    is_gap INTEGER DEFAULT 0,
                    provider_open REAL,
                    provider_high REAL,
                    provider_low REAL,
                    provider_close REAL,
                    local_open REAL,
                    local_high REAL,
                    local_low REAL,
                    local_close REAL,
                    discrepancy_flag INTEGER DEFAULT 0,
                    received_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    reconciled_at TEXT,
                    version INTEGER DEFAULT 1,
                    is_point_in_time INTEGER DEFAULT 1,
                    PRIMARY KEY (ticker, timestamp, version)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_index_ohlcv_time ON continuous_index_ohlcv(ticker, timestamp);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_index_ohlcv_pit ON continuous_index_ohlcv(ticker, timestamp, available_at, is_point_in_time);")

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
        target_db = getattr(OptionDB, "_db_path_override", None) or get_db_path()
        conn = sqlite3.connect(target_db, timeout=15.0)
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
        """Retrieves active monitored option contracts (strictly non-expired)."""
        today_str = to_ist_naive().strftime("%Y-%m-%d")
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                query = "SELECT * FROM option_sniffer_candidates WHERE is_active = 1 AND expiry >= ?"
                params = [today_str]
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
    def deactivate_expired_candidates(cls, today_str: Optional[str] = None) -> int:
        """Sets is_active = 0 for candidate contracts whose expiry is strictly before today."""
        today_str = today_str or to_ist_naive().strftime("%Y-%m-%d")
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    cur = conn.execute(
                        "UPDATE option_sniffer_candidates SET is_active = 0 WHERE expiry < ? AND is_active = 1;",
                        (today_str,)
                    )
                    count = cur.rowcount
                conn.close()
                return count
            except Exception as e:
                logger.error(f"[OptionDB] Failed to deactivate expired candidates: {e}")
                return 0

    @classmethod
    def sync_active_candidates(cls, active_instrument_keys: List[str]) -> int:
        """
        Idempotently synchronizes option_sniffer_candidates:
        Ensures only current active_instrument_keys remain is_active = 1,
        and all unmonitored or far-month contracts are marked is_active = 0.
        """
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    if not active_instrument_keys:
                        cur = conn.execute("UPDATE option_sniffer_candidates SET is_active = 0 WHERE is_active = 1;")
                    else:
                        placeholders = ",".join(["?"] * len(active_instrument_keys))
                        cur = conn.execute(
                            f"UPDATE option_sniffer_candidates SET is_active = 0 WHERE is_active = 1 AND instrument_key NOT IN ({placeholders});",
                            tuple(active_instrument_keys)
                        )
                    count = cur.rowcount
                conn.close()
                return count
            except Exception as e:
                logger.error(f"[OptionDB] Failed to sync active candidates: {e}")
                return 0

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
                            trough_price, model_version, signal_version, data_freshness,
                            engine_version, dynamic_sl_pct, dynamic_target_pct, spot_atr, delta_used,
                            entry_spot_price
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        trade.get("data_freshness", "REAL_TIME"),
                        trade.get("engine_version", "DYNAMIC_V2"),
                        trade.get("dynamic_sl_pct"),
                        trade.get("dynamic_target_pct"),
                        trade.get("spot_atr"),
                        trade.get("delta_used"),
                        trade.get("entry_spot_price")
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
                    cur = conn.execute("""
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
                        WHERE trade_id = ? AND status = 'OPEN'
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
                    if cur.rowcount == 0:
                        logger.warning(f"[OptionDB] Attempted to close trade {trade_id} but status is not OPEN.")
                        return False
                conn.close()
                cls.log_event("TRADE", "CLOSED", f"Closed paper trade {trade_id} @ ₹{close_data['exit_bid']:.2f} (Net ₹{close_data.get('net_pnl', 0.0):.2f})", close_data)
                return True
            except Exception as e:
                logger.error(f"[OptionDB] Failed to close paper trade {trade_id}: {e}")
                return False

    @classmethod
    def update_trade_extrema(
        cls,
        trade_id: str,
        peak_price: float,
        trough_price: float,
        mfe_pct: Optional[float] = None,
        mae_pct: Optional[float] = None
    ) -> bool:
        """Updates peak and trough prices, plus MFE/MAE excursions on an open trade."""
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    if mfe_pct is not None or mae_pct is not None:
                        conn.execute("""
                            UPDATE option_sniffer_paper_trades
                            SET peak_price = ?, trough_price = ?, mfe_pct = ?, mae_pct = ?
                            WHERE trade_id = ?
                        """, (peak_price, trough_price, mfe_pct, mae_pct, trade_id))
                    else:
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
    def update_open_trade_metrics(
        cls,
        trade_id: str,
        peak_price: float,
        trough_price: float,
        mfe_pct: float,
        mae_pct: float
    ) -> bool:
        """Updates peak, trough, MFE, and MAE for an active open trade."""
        return cls.update_trade_extrema(trade_id, peak_price, trough_price, mfe_pct=mfe_pct, mae_pct=mae_pct)

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
                today_str = to_ist_naive().strftime("%Y-%m-%d")

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
    def get_paper_circuit_breaker_latch(cls, session_date: str) -> Optional[Dict[str, Any]]:
        """Queries persistent paper risk ledger for today's halt latch status."""
        try:
            conn = cls.get_connection()
            cur = conn.execute("""
                CREATE TABLE IF NOT EXISTS option_sniffer_paper_risk_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_date TEXT UNIQUE NOT NULL,
                    starting_capital REAL DEFAULT 15000.0,
                    realized_pnl REAL DEFAULT 0.0,
                    unrealized_pnl REAL DEFAULT 0.0,
                    total_exposure REAL DEFAULT 0.0,
                    circuit_breaker_tripped INTEGER DEFAULT 0,
                    trip_reason TEXT,
                    halt_latch INTEGER DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
            """)
            row = conn.execute(
                "SELECT session_date, realized_pnl, unrealized_pnl, total_exposure, circuit_breaker_tripped, trip_reason, halt_latch, updated_at "
                "FROM option_sniffer_paper_risk_ledger WHERE session_date = ?;",
                (session_date,)
            ).fetchone()
            conn.close()
            if row:
                return dict(row)
            return None
        except Exception as e:
            logger.error(f"[OptionDB] Failed to query paper risk ledger latch: {e}")
            return None

    @classmethod
    def persist_paper_circuit_breaker_latch(
        cls,
        session_date: str,
        realized_pnl: float,
        unrealized_pnl: float,
        total_exposure: float,
        trip_reason: str,
        halt_latch: int = 1
    ) -> bool:
        """Persists the daily halt latch to guarantee restart & reboot immunity."""
        now_iso = to_ist_naive().strftime("%Y-%m-%d %H:%M:%S")
        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                with conn:
                    conn.execute("""
                        INSERT INTO option_sniffer_paper_risk_ledger 
                        (session_date, starting_capital, realized_pnl, unrealized_pnl, total_exposure, circuit_breaker_tripped, trip_reason, halt_latch, updated_at)
                        VALUES (?, 15000.0, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_date) DO UPDATE SET
                            realized_pnl = excluded.realized_pnl,
                            unrealized_pnl = excluded.unrealized_pnl,
                            total_exposure = excluded.total_exposure,
                            circuit_breaker_tripped = excluded.circuit_breaker_tripped,
                            trip_reason = excluded.trip_reason,
                            halt_latch = excluded.halt_latch,
                            updated_at = excluded.updated_at;
                    """, (session_date, realized_pnl, unrealized_pnl, total_exposure, 1 if halt_latch else 0, trip_reason, halt_latch, now_iso))
                conn.close()
                return True
            except Exception as e:
                logger.error(f"[OptionDB] Failed to persist paper circuit breaker latch: {e}")
                return False

    @classmethod
    def get_daily_circuit_breaker_telemetry(
        cls,
        max_consecutive_losses: int = 2,
        max_daily_loss_inr: float = 3500.0,
        pause_minutes: int = 30,
        as_of_date: Optional[str] = None,
        as_of_time: Optional[datetime] = None,
        live_quotes_map: Optional[Dict[str, Dict[str, Any]]] = None,
        enforce_halt_latch: bool = True
    ) -> Dict[str, Any]:
        """
        Audits today's closed paper trades directly from SQLite option_sniffer_paper_trades.
        Guarantees crash-immune, reboot-immune persistence of consecutive losses and daily drawdown.
        Monitors both Realized P&L and Floating Unrealized P&L across all open positions.
        Implements Two-Tier Dynamic Architecture:
          Tier 1: Dynamic Introspection Pause (30m cooldown after 2 consecutive losses).
          Tier 2: Hard Daily Capital Ceiling (-₹3,500 permanent session shutdown with SQLite halt latch).
        """
        curr_dt = to_ist_naive(as_of_time)
        date_str = as_of_date or curr_dt.strftime("%Y-%m-%d")

        # 0. Check persistent halt latch in SQLite first (restart & reboot immunity)
        latch_record = None
        is_prior_latch_tripped = False
        if enforce_halt_latch:
            latch_record = cls.get_paper_circuit_breaker_latch(date_str)
            is_prior_latch_tripped = bool(latch_record and latch_record.get("halt_latch") == 1)

        try:
            with _DB_LOCK:
                conn = cls.get_connection()
                day_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
                from datetime import timedelta
                next_date_str = (day_dt + timedelta(days=1)).strftime("%Y-%m-%d")
                day_start = f"{date_str} 00:00:00"
                next_day_start = f"{next_date_str} 00:00:00"

                # 1. Closed paper trades today
                rows = conn.execute("""
                    SELECT trade_id, trading_symbol, underlying, exit_time, exit_reason, net_pnl, net_pnl_pct
                    FROM option_sniffer_paper_trades
                    WHERE status = 'CLOSED' AND exit_time >= ? AND exit_time < ?
                    ORDER BY exit_time DESC
                """, (day_start, next_day_start)).fetchall()

                # 2. Open paper trades (Authoritative open position quantities)
                open_cols = [c[1] for c in conn.execute("PRAGMA table_info(option_sniffer_paper_trades);").fetchall()]
                entry_col = "entry_ask" if "entry_ask" in open_cols else ("entry_price" if "entry_price" in open_cols else "entry_price_used")
                ikey_col = "instrument_key" if "instrument_key" in open_cols else "trade_id"
                qty_col = "quantity" if "quantity" in open_cols else "1"
                time_col = "entry_time" if "entry_time" in open_cols else "created_at"

                open_rows = conn.execute(f"""
                    SELECT trade_id, {ikey_col} AS instrument_key, {entry_col} AS entry_ask, {qty_col} AS quantity, {time_col} AS entry_time
                    FROM option_sniffer_paper_trades
                    WHERE status = 'OPEN'
                """).fetchall()
                conn.close()

                closed_today = [dict(r) for r in rows]
                open_trades_list = [dict(r) for r in open_rows]

            # Release _DB_LOCK before querying OptionStreamManager to prevent AB-BA lock inversion
            total_closed = len(closed_today)
            today_net_pnl = sum(float(t.get("net_pnl") or 0.0) for t in closed_today)
            total_losses = sum(1 for t in closed_today if float(t.get("net_pnl") or 0.0) < 0)
            total_wins = sum(1 for t in closed_today if float(t.get("net_pnl") or 0.0) > 0)

            # Count consecutive losses starting from the most recent closed trade
            consecutive_losses = 0
            for t in closed_today:
                pnl = float(t.get("net_pnl") or 0.0)
                if pnl < 0:
                    consecutive_losses += 1
                else:
                    break

            most_recent_loss_time = None
            for t in closed_today:
                if float(t.get("net_pnl") or 0.0) < 0:
                    most_recent_loss_time = t.get("exit_time")
                    break

            # 3. Calculate Unrealized MTM across open paper trades
            unrealized_net_pnl = 0.0
            data_integrity_warning = False

            stream_mgr = None
            try:
                from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
                stream_mgr = OptionStreamManager()
            except Exception:
                stream_mgr = None

            for ot in open_trades_list:
                ikey = ot["instrument_key"]
                entry_ask = float(ot["entry_ask"])
                qty = int(ot["quantity"])
                q = None
                if live_quotes_map is not None:
                    q = live_quotes_map.get(ikey)
                elif stream_mgr is not None:
                    try:
                        q = stream_mgr.get_option_quote(ikey)
                    except Exception:
                        q = None

                bid = float(q.get("bid") or q.get("ltp") or 0.0) if q else 0.0
                if live_quotes_map is not None and bid <= 0:
                    data_integrity_warning = True
                elif bid > 0:
                    gross_mtm = round((bid - entry_ask) * qty, 2)
                    from app.analytics.option_sniffer.option_constants import calculate_option_trade_costs
                    cost_est = calculate_option_trade_costs(
                        buy_price=entry_ask,
                        sell_price=bid,
                        quantity=qty,
                        apply_slippage=True,
                        trade_date=ot.get("entry_time")
                    )
                    net_mtm = round(gross_mtm - cost_est["total_friction"], 2)
                    unrealized_net_pnl += net_mtm

            total_net_pnl_exposure = round(today_net_pnl + unrealized_net_pnl, 2)

            # Chronological Peak-to-Trough Drawdown Calculation
            sorted_by_exit = sorted(closed_today, key=lambda t: str(t.get("exit_time") or ""))
            cum_pnl = 0.0
            peak_pnl = 0.0
            max_drawdown = 0.0
            for t in sorted_by_exit:
                cum_pnl += float(t.get("net_pnl") or 0.0)
                if cum_pnl > peak_pnl:
                    peak_pnl = cum_pnl
                dd = peak_pnl - cum_pnl
                if dd > max_drawdown:
                    max_drawdown = dd

            # Include open positions exposure into current peak-to-trough drawdown
            current_total_pnl = today_net_pnl + unrealized_net_pnl
            current_drawdown = max(0.0, peak_pnl - current_total_pnl)
            if current_drawdown > max_drawdown:
                max_drawdown = current_drawdown

            # ── APEX QUANT: INTRADAY PEAK-PROFIT TRAILING LOCK ──
            is_peak_lock_tripped = False
            peak_profit_floor = 0.0
            if ENABLE_APEX_QUANT_ENGINE and peak_pnl >= PEAK_PROFIT_LOCK_TRIGGER_INR:
                peak_profit_floor = round(peak_pnl * (1.0 - PEAK_PROFIT_GIVEBACK_MAX_RATIO), 2)
                if (today_net_pnl <= peak_profit_floor or total_net_pnl_exposure <= peak_profit_floor) and total_closed > 0:
                    is_peak_lock_tripped = True

            is_consecutive_limit_hit = consecutive_losses >= max_consecutive_losses
            is_realized_limit_hit = today_net_pnl <= -abs(max_daily_loss_inr)
            is_drawdown_limit_hit = max_drawdown >= abs(max_daily_loss_inr)
            is_exposure_limit_hit = total_net_pnl_exposure <= -abs(max_daily_loss_inr)
            is_max_loss_limit_hit = is_realized_limit_hit or is_drawdown_limit_hit or is_exposure_limit_hit or is_prior_latch_tripped

            # If restart occurred without local closed trade rows but latch was persisted
            if is_prior_latch_tripped and total_closed == 0 and latch_record:
                today_net_pnl = latch_record.get("realized_pnl", 0.0)
                unrealized_net_pnl = latch_record.get("unrealized_pnl", 0.0)
                total_net_pnl_exposure = latch_record.get("total_exposure", 0.0)

            # Dynamic Introspection Pause calculation
            is_pause_active = False
            pause_remaining_sec = 0
            pause_unfreeze_time = None
            if is_consecutive_limit_hit and most_recent_loss_time:
                try:
                    loss_dt = to_ist_naive(datetime.fromisoformat(most_recent_loss_time))
                    elapsed_sec = (curr_dt - loss_dt).total_seconds()
                    total_pause_sec = pause_minutes * 60
                    if 0 <= elapsed_sec < total_pause_sec:
                        is_pause_active = True
                        pause_remaining_sec = int(total_pause_sec - elapsed_sec)
                        from datetime import timedelta
                        pause_unfreeze_time = (loss_dt + timedelta(minutes=pause_minutes)).strftime("%H:%M:%S")
                except Exception as pe:
                    logger.debug(f"[OptionDB] Introspection pause parse note: {pe}")

            is_session_halted = is_max_loss_limit_hit or data_integrity_warning or is_peak_lock_tripped
            halt_reason = None
            if is_max_loss_limit_hit:
                if is_prior_latch_tripped and latch_record and latch_record.get("trip_reason"):
                    halt_reason = latch_record.get("trip_reason")
                elif is_realized_limit_hit:
                    halt_reason = f"Daily max loss ceiling reached (Net: ₹{today_net_pnl:,.2f} <= -₹{max_daily_loss_inr:,.2f}). Trading permanently halted for session."
                elif is_exposure_limit_hit:
                    halt_reason = f"Daily max loss ceiling reached (Total Exposure: ₹{total_net_pnl_exposure:,.2f} <= -₹{max_daily_loss_inr:,.2f} [Realized: ₹{today_net_pnl:,.2f}, Unrealized: ₹{unrealized_net_pnl:,.2f}]). Trading permanently halted for session."
                elif is_drawdown_limit_hit:
                    halt_reason = f"Peak-to-trough drawdown ceiling reached (Drawdown: ₹{max_drawdown:,.2f} >= ₹{max_daily_loss_inr:,.2f} from peak ₹{peak_pnl:,.2f}). Trading permanently halted for session."
                if enforce_halt_latch and not is_prior_latch_tripped:
                    cls.persist_paper_circuit_breaker_latch(
                        session_date=date_str,
                        realized_pnl=today_net_pnl,
                        unrealized_pnl=unrealized_net_pnl,
                        total_exposure=total_net_pnl_exposure,
                        trip_reason=halt_reason,
                        halt_latch=1
                    )
            elif is_peak_lock_tripped:
                halt_reason = (
                    f"Peak-Profit Trailing Lock Engaged: Daily profit peaked at ₹{peak_pnl:,.2f}. "
                    f"Session locked at ₹{today_net_pnl:,.2f} to protect 50% of morning harvest (Floor: ₹{peak_profit_floor:,.2f})."
                )
                if enforce_halt_latch and not is_prior_latch_tripped:
                    cls.persist_paper_circuit_breaker_latch(
                        session_date=date_str,
                        realized_pnl=today_net_pnl,
                        unrealized_pnl=unrealized_net_pnl,
                        total_exposure=total_net_pnl_exposure,
                        trip_reason=halt_reason,
                        halt_latch=1
                    )
            elif data_integrity_warning:
                halt_reason = f"Fail-Closed: Missing or stale quotes for {len(open_trades_list)} active open paper trade(s). Trading halted for session."
            elif is_pause_active:
                halt_reason = f"Dynamic Introspection Pause Active: {consecutive_losses} consecutive losses. Paused for {pause_minutes}m ({pause_remaining_sec}s remaining until {pause_unfreeze_time} IST)."
            elif is_consecutive_limit_hit:
                halt_reason = f"Introspection cooldown completed for {consecutive_losses} consecutive losses. Re-entry allowed on verified trend breakout."

            return {
                "date": date_str,
                "total_closed_today": total_closed,
                "wins_today": total_wins,
                "losses_today": total_losses,
                "consecutive_losses": consecutive_losses,
                "today_net_pnl": round(today_net_pnl, 2),
                "unrealized_net_pnl": round(unrealized_net_pnl, 2),
                "unrealized_pnl": round(unrealized_net_pnl, 2),
                "total_net_pnl_exposure": total_net_pnl_exposure,
                "peak_pnl": round(peak_pnl, 2),
                "max_drawdown": round(max_drawdown, 2),
                "is_drawdown_limit_hit": is_drawdown_limit_hit,
                "is_realized_limit_hit": is_realized_limit_hit,
                "is_exposure_limit_hit": is_exposure_limit_hit,
                "open_positions_count": len(open_trades_list),
                "is_consecutive_limit_hit": is_consecutive_limit_hit,
                "is_max_loss_limit_hit": is_max_loss_limit_hit,
                "is_pause_active": is_pause_active,
                "pause_remaining_sec": pause_remaining_sec,
                "pause_unfreeze_time": pause_unfreeze_time,
                "is_session_halted": is_session_halted,
                "halt_reason": halt_reason,
                "halt_latch_active": (is_max_loss_limit_hit or is_peak_lock_tripped),
                "is_peak_lock_tripped": is_peak_lock_tripped,
                "peak_profit_floor": peak_profit_floor,
                "most_recent_loss_time": most_recent_loss_time,
                "max_consecutive_losses": max_consecutive_losses,
                "max_daily_loss_inr": max_daily_loss_inr,
                "pause_minutes": pause_minutes,
                "is_statutory_charges_estimated": True,
                "charges_reconciliation_mode": "ESTIMATED_STATUTORY_SCHEDULE",
            }
        except Exception as e:
            logger.error(f"[OptionDB] Failed to audit daily circuit breaker: {e}")
            return {
                "date": date_str,
                "total_closed_today": 0,
                "wins_today": 0,
                "losses_today": 0,
                "consecutive_losses": 0,
                "today_net_pnl": 0.0,
                "unrealized_net_pnl": 0.0,
                "total_net_pnl_exposure": 0.0,
                "open_positions_count": 0,
                "is_consecutive_limit_hit": False,
                "is_max_loss_limit_hit": True,
                "is_pause_active": False,
                "pause_remaining_sec": 0,
                "pause_unfreeze_time": None,
                "is_session_halted": True,
                "halt_reason": f"Fail-Closed: Error auditing daily circuit breaker ({e})",
                "halt_latch_active": True,
                "most_recent_loss_time": None,
                "max_consecutive_losses": max_consecutive_losses,
                "max_daily_loss_inr": max_daily_loss_inr,
                "pause_minutes": pause_minutes,
                "is_statutory_charges_estimated": True,
                "charges_reconciliation_mode": "ESTIMATED_STATUTORY_SCHEDULE",
            }

    @classmethod
    def has_paper_stop_loss_today(cls, as_of_date: Optional[str] = None) -> Tuple[bool, str]:
        """
        Authoritative Canary Scout health audit.
        Checks if paper trading experienced any Stop Loss hits, adverse exits, or circuit breaker trips today.
        Returns: (has_loss: bool, reason: str)
        """
        curr_dt = to_ist_naive()
        date_str = as_of_date or curr_dt.strftime("%Y-%m-%d")

        # 1. Check paper circuit breaker halt latch in SQLite first
        latch_record = cls.get_paper_circuit_breaker_latch(date_str)
        if latch_record and latch_record.get("halt_latch") == 1:
            trip_reason = latch_record.get("trip_reason") or "Paper Circuit Breaker Halt Latch is ACTIVE"
            return True, f"Paper circuit breaker tripped: {trip_reason}"

        with _DB_LOCK:
            try:
                conn = cls.get_connection()
                day_start = f"{date_str} 00:00:00"
                from datetime import datetime as dt_cls, timedelta
                day_dt = dt_cls.strptime(date_str, "%Y-%m-%d").date()
                next_day_start = f"{(day_dt + timedelta(days=1)).strftime('%Y-%m-%d')} 00:00:00"

                # 2. Check for any closed trades with net loss or SL/CB exit reason
                cur = conn.cursor()
                rows = cur.execute("""
                    SELECT trade_id, trading_symbol, exit_reason, net_pnl, exit_time
                    FROM option_sniffer_paper_trades
                    WHERE status = 'CLOSED' 
                      AND exit_time >= ? AND exit_time < ?
                      AND (
                          net_pnl < 0 
                          OR exit_reason IN ('STOP_LOSS_HIT', 'CIRCUIT_BREAKER_HALT', 'STRUCTURE_INVALIDATION')
                      )
                    ORDER BY exit_time DESC;
                """, (day_start, next_day_start)).fetchall()
                conn.close()

                if rows:
                    worst_or_recent = rows[0]
                    t_id = worst_or_recent[0]
                    sym = worst_or_recent[1]
                    reason = worst_or_recent[2]
                    pnl = float(worst_or_recent[3] or 0.0)
                    t_time = worst_or_recent[4]
                    return True, f"Canary trade {t_id} ({sym}) suffered {reason} with PnL ₹{pnl:,.2f} at {t_time}"

                return False, "Canary scout healthy: zero paper stop losses today"
            except Exception as e:
                logger.error(f"[OptionDB] Error auditing paper stop loss today: {e}")
                # Fail-closed
                return True, f"Fail-closed: Error auditing paper stop loss ({e})"

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
