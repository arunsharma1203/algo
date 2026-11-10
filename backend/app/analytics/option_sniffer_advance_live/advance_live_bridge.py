"""
Option Sniffer Advance: Isolated Live Broker Execution Bridge (SQLite WAL Follower).

STRICT ARCHITECTURAL GUARANTEES:
  1. ZERO CORE FILES TOUCHED: Does not modify a single line in `backend/app/analytics/option_sniffer_advance/*`.
  2. HARD-LOCKED PAPER DEFAULT: Boots with `_is_live_armed = False` and `SIMULATION_MODE = True`.
  3. EXCLUSIVE HUMAN AUTHORITY (AGENTS.md Rule 6): Can only be armed/disarmed via the 4-digit PIN UI modal.
  4. ZERO ALPHA LOSS ON MAIN OPTION SNIFFER: Reuses cached 30s Upstox readiness telemetry from `OptionLiveAdapter`
     and uses isolated SQLite tables (`option_advance_live_order_intents`, `option_advance_live_risk_ledger`).
  5. CAPITAL-MATCHED EXECUTION MODES:
     - `LONG_BOOK_ONLY` (Default — Recommended for ₹55,000 Upstox Wallet; 100% Book 1 Alpha, 0 wing drag).
     - `DUAL_BOOK_NAKED` (For ₹1,65,000+ Upstox Wallet; 100% Book 1 + Book 2 Single-Leg Alpha).
     - `DUAL_BOOK_HEDGED` (For ₹45,000–₹55,000 Upstox Wallet; Book 1 + 2-Leg 200-pt OTM Hedged Short Spread).
"""

import json
import logging
import os
import random
import sqlite3
import threading
import time
from datetime import datetime, time as dt_time
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.option_sniffer.option_constants import (
    calculate_option_trade_costs,
    to_ist_naive,
)
from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
from app.analytics.option_sniffer.option_live_order_intent import (
    get_order_intent_engine,
    sanitize_upstox_tag,
)


def round_to_tick(price: float, tick: float = 0.05) -> float:
    """Rounds an NSE F&O option price to the nearest ₹0.05 tick."""
    if price <= 0:
        return 0.05
    return round(round(float(price) / tick) * tick, 2)
from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
from app.analytics.option_sniffer_advance.advance_constants import (
    BOOK_LONG_BUY,
    BOOK_SHORT_SELL,
    MAX_DAILY_COMBINED_LOSS_INR,
    calculate_short_option_trade_costs,
)
from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)

# Hard-Locked Safety Defaults
SIMULATION_MODE: bool = True
MODE_LONG_BOOK_ONLY: str = "LONG_BOOK_ONLY"
MODE_DUAL_BOOK_NAKED: str = "DUAL_BOOK_NAKED"
MODE_DUAL_BOOK_HEDGED: str = "DUAL_BOOK_HEDGED"
VALID_EXECUTION_MODES = (
    MODE_LONG_BOOK_ONLY,
    MODE_DUAL_BOOK_NAKED,
    MODE_DUAL_BOOK_HEDGED,
)

MIN_MARGIN_LONG_ONLY_INR: float = 18000.0
MIN_MARGIN_DUAL_HEDGED_INR: float = 42000.0
MIN_MARGIN_DUAL_NAKED_INR: float = 150000.0

_BRIDGE_LOCK = globals().get("_BRIDGE_LOCK") or threading.RLock()
_PREV_BRIDGE_INSTANCE = globals().get("_BRIDGE_INSTANCE")
_BRIDGE_INSTANCE: Optional["AdvanceLiveBridge"] = _PREV_BRIDGE_INSTANCE


def init_advance_live_tables(db_path: Optional[str] = None) -> None:
    """Creates isolated SQLite tables for Option Sniffer Advance live order intents and risk ledger."""
    target_db = db_path or get_db_path()
    with _BRIDGE_LOCK:
        try:
            with sqlite3.connect(target_db, timeout=10.0) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS option_advance_live_order_intents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_intent_id TEXT UNIQUE NOT NULL,
                        advance_trade_id TEXT UNIQUE NOT NULL,
                        signal_id TEXT,
                        execution_mode TEXT NOT NULL,
                        book_type TEXT NOT NULL,
                        strategy_mode TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        action TEXT NOT NULL,
                        instrument_key TEXT NOT NULL,
                        trading_symbol TEXT NOT NULL,
                        underlying TEXT NOT NULL,
                        strike REAL NOT NULL,
                        option_type TEXT NOT NULL,
                        expiry TEXT NOT NULL,
                        lot_size INTEGER NOT NULL,
                        target_quantity INTEGER NOT NULL,
                        filled_quantity INTEGER DEFAULT 0,
                        limit_price REAL NOT NULL,
                        average_fill_price REAL DEFAULT 0.0,
                        state TEXT NOT NULL,
                        broker_order_id TEXT,
                        stop_loss_trigger REAL DEFAULT 0.0,
                        stop_loss_limit REAL DEFAULT 0.0,
                        broker_sl_order_id TEXT,
                        hedge_wing_strike REAL DEFAULT 0.0,
                        hedge_wing_instrument_key TEXT,
                        hedge_wing_broker_order_id TEXT,
                        exit_state TEXT DEFAULT 'NONE',
                        exit_broker_order_id TEXT,
                        exit_fill_price REAL DEFAULT 0.0,
                        exit_reason TEXT,
                        paper_entry_price REAL DEFAULT 0.0,
                        paper_exit_price REAL DEFAULT 0.0,
                        paper_net_pnl REAL DEFAULT 0.0,
                        gross_pnl REAL DEFAULT 0.0,
                        statutory_charges REAL DEFAULT 0.0,
                        realized_pnl REAL DEFAULT 0.0,
                        rejection_reason TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_adv_live_intents_trade_id
                    ON option_advance_live_order_intents(advance_trade_id);
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS option_advance_live_risk_ledger (
                        session_date TEXT PRIMARY KEY,
                        realized_pnl_inr REAL DEFAULT 0.0,
                        consecutive_losses INTEGER DEFAULT 0,
                        circuit_breaker_tripped INTEGER DEFAULT 0,
                        trip_reason TEXT,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
                conn.commit()
        except Exception as e:
            logger.error(f"[AdvanceLiveBridge] Failed to initialize isolated live tables: {e}")


class AdvanceLiveBridge:
    """
    Isolated SQLite WAL Follower & Broker Execution Bridge for Option Sniffer Advance.
    Watches `option_sniffer_advance_trades` in read-only mode without modifying any core Advance files.
    """

    def __init__(self, db_path: Optional[str] = None, auto_start_daemon: bool = True):
        self._db_path = db_path
        self._auto_start_daemon = auto_start_daemon
        init_advance_live_tables(self._db_path)
        self._is_live_armed: bool = False
        self._execution_mode: str = MODE_LONG_BOOK_ONLY
        self._armed_for_date: str = ""
        self._armed_at_epoch: float = 0.0
        self._armed_at_iso: str = ""
        self._current_pin: str = f"{random.randint(1000, 9999)}"
        self._follower_thread: Optional[threading.Thread] = None
        self._follower_running: bool = False
        self._op_lock = threading.RLock()
        self._last_broker_reconcile_mono: float = -999999.0
        self._last_broker_reconcile_sim_dt: Optional[datetime] = None
        self._reconciled_once_intent_ids: set = set()
        self._last_priority_quote_refresh_ts: float = -999999.0

    @property
    def db_path(self) -> str:
        return self._db_path or get_db_path()

    @property
    def is_live_armed(self) -> bool:
        return self._is_live_armed

    @property
    def SIMULATION_MODE(self) -> bool:
        return not self._is_live_armed

    @property
    def execution_mode(self) -> str:
        return self._execution_mode

    @property
    def current_pin(self) -> str:
        return self._current_pin

    @property
    def _live_adapter(self):
        return get_option_live_adapter()

    def _get_rw_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_ro_conn(self) -> sqlite3.Connection:
        uri = f"file:{os.path.abspath(self.db_path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.execute("PRAGMA query_only = ON;")
        conn.row_factory = sqlite3.Row
        return conn

    def generate_new_pin(self) -> str:
        self._current_pin = f"{random.randint(1000, 9999)}"
        return self._current_pin

    def set_execution_mode(self, mode: str) -> Dict[str, Any]:
        clean_mode = str(mode or "").upper().strip()
        if clean_mode not in VALID_EXECUTION_MODES:
            return {
                "status": "ERROR",
                "execution_mode": self._execution_mode,
                "message": f"Invalid execution_mode '{clean_mode}'. Must be one of {VALID_EXECUTION_MODES}.",
            }
        with self._op_lock:
            self._execution_mode = clean_mode
        return {
            "status": "UPDATED",
            "execution_mode": self._execution_mode,
            "required_margin_for_mode": self.get_mode_margin_requirement(clean_mode),
            "message": f"Execution mode updated to {clean_mode}.",
        }

    def get_circuit_breaker_status(self, session_date: Optional[str] = None) -> Dict[str, Any]:
        today = session_date or to_ist_naive().strftime("%Y-%m-%d")
        realized_pnl = 0.0
        unrealized_pnl = 0.0
        consec_losses = 0
        tripped = False
        reason = None
        try:
            with self._get_ro_conn() as conn:
                cur = conn.cursor()
                latch_row = cur.execute(
                    """
                    SELECT circuit_breaker_tripped, trip_reason
                    FROM option_advance_live_risk_ledger
                    WHERE session_date = ? LIMIT 1;
                    """,
                    (today,),
                ).fetchone()
                if latch_row and int(latch_row[0] or 0) == 1:
                    tripped = True
                    reason = str(latch_row[1] or "Daily Live Loss Ceiling Reached")

                rows = cur.execute(
                    """
                    SELECT realized_pnl FROM option_advance_live_order_intents
                    WHERE SUBSTR(created_at, 1, 10) = ?
                      AND exit_state = 'CONFIRMED_FLAT'
                    ORDER BY id ASC;
                    """,
                    (today,),
                ).fetchall()
                for r in rows:
                    pnl = float(r[0] or 0.0)
                    realized_pnl += pnl
                    if pnl < 0:
                        consec_losses += 1
                    else:
                        consec_losses = 0

            for pos in self.get_active_live_positions():
                if str(pos.get("created_at") or "")[:10] == today:
                    unrealized_pnl += float(pos.get("unrealized_pnl") or 0.0)

            total_exposure_pnl = realized_pnl + min(0.0, unrealized_pnl)
            if realized_pnl <= -MAX_DAILY_COMBINED_LOSS_INR:
                tripped = True
                reason = f"Daily Live Loss Ceiling Reached (₹{realized_pnl:,.2f} <= -₹{MAX_DAILY_COMBINED_LOSS_INR:,.2f})"
            elif total_exposure_pnl <= -MAX_DAILY_COMBINED_LOSS_INR:
                tripped = True
                reason = (
                    f"Daily Live Total Exposure Ceiling Reached "
                    f"(₹{total_exposure_pnl:,.2f} [Realized ₹{realized_pnl:,.2f} + Open MTM ₹{unrealized_pnl:,.2f}] "
                    f"<= -₹{MAX_DAILY_COMBINED_LOSS_INR:,.2f})"
                )

            if tripped:
                now_iso = to_ist_naive().strftime("%Y-%m-%d %H:%M:%S")
                with self._get_rw_conn() as rw_conn:
                    rw_conn.execute(
                        """
                        INSERT INTO option_advance_live_risk_ledger
                        (session_date, realized_pnl_inr, consecutive_losses, circuit_breaker_tripped, trip_reason, updated_at)
                        VALUES (?, ?, ?, 1, ?, ?)
                        ON CONFLICT(session_date) DO UPDATE SET
                            realized_pnl_inr = excluded.realized_pnl_inr,
                            consecutive_losses = excluded.consecutive_losses,
                            circuit_breaker_tripped = 1,
                            trip_reason = excluded.trip_reason,
                            updated_at = excluded.updated_at;
                        """,
                        (today, round(realized_pnl, 2), consec_losses, reason, now_iso),
                    )
                    rw_conn.commit()
        except Exception as e:
            logger.debug(f"[AdvanceLiveBridge] Circuit breaker check note: {e}")

        return {
            "session_date": today,
            "realized_pnl_inr": round(realized_pnl, 2),
            "unrealized_pnl_inr": round(unrealized_pnl, 2),
            "total_exposure_pnl_inr": round(realized_pnl + min(0.0, unrealized_pnl), 2),
            "max_daily_loss_inr": MAX_DAILY_COMBINED_LOSS_INR,
            "consecutive_losses": consec_losses,
            "is_session_halted": tripped,
            "halt_reason": reason,
        }

    def get_mode_margin_requirement(self, mode: Optional[str] = None) -> float:
        m = mode or self._execution_mode
        if m == MODE_DUAL_BOOK_NAKED:
            return MIN_MARGIN_DUAL_NAKED_INR
        if m == MODE_DUAL_BOOK_HEDGED:
            return MIN_MARGIN_DUAL_HEDGED_INR
        return MIN_MARGIN_LONG_ONLY_INR

    def get_live_status(self) -> Dict[str, Any]:
        """Returns comprehensive operational status for the Option Sniffer Advance Live Bridge."""
        now = to_ist_naive()
        today = now.strftime("%Y-%m-%d")

        with self._op_lock:
            if self._is_live_armed:
                if self._armed_for_date != today:
                    logger.info("[AdvanceLiveBridge] 1-day session expired on date rollover. Reverting to Paper Mode.")
                    self._is_live_armed = False
                elif now.time() >= dt_time(15, 15):
                    logger.info("[AdvanceLiveBridge] Session cutoff (15:15 IST) reached. Reverting to Paper Mode.")
                    self._is_live_armed = False
                else:
                    cb = self.get_circuit_breaker_status(today)
                    if cb.get("is_session_halted"):
                        logger.warning(f"🚨 [AdvanceLiveBridge] Auto-disarming due to live circuit breaker: {cb.get('halt_reason')}")
                        self._is_live_armed = False

        # Reuse cached 30s Upstox readiness snapshot from OptionLiveAdapter (0 extra REST calls)
        base_adapter = self._live_adapter
        readiness = dict(base_adapter.fetch_account_readiness(force_refresh=False) or {})
        avail_margin = float(readiness.get("available_margin") or 0.0)
        req_margin = self.get_mode_margin_requirement(self._execution_mode)
        mode_margin_ok = avail_margin >= req_margin

        active_positions = self.get_active_live_positions()
        order_intents = self.get_recent_order_intents(limit=25)
        pnl_summary = self.get_live_pnl_summary()
        cb_status = self.get_circuit_breaker_status(today)

        return {
            "is_live_armed": self._is_live_armed,
            "simulation_mode": not self._is_live_armed,
            "execution_mode": self._execution_mode,
            "armed_for_date": self._armed_for_date,
            "armed_at_iso": self._armed_at_iso,
            "session_cutoff": "15:15 IST",
            "current_pin": self._current_pin,
            "available_margin": avail_margin,
            "required_margin_for_mode": req_margin,
            "required_margin_for_mode_inr": req_margin,
            "mode_margin_ok": mode_margin_ok,
            "readiness": readiness,
            "account_readiness": readiness,
            "circuit_breaker": cb_status,
            "active_positions": active_positions,
            "active_live_positions": active_positions,
            "active_positions_count": len(active_positions),
            "order_intents": order_intents,
            "recent_order_intents": order_intents,
            "pnl_summary": pnl_summary,
            "available_modes": [
                {
                    "mode": MODE_LONG_BOOK_ONLY,
                    "label": "Book 1 Only (1-Lot BUY — Recommended for ₹55k Wallet)",
                    "required_margin_inr": MIN_MARGIN_LONG_ONLY_INR,
                    "alpha_retention": "100% of Long Book Alpha (Zero Hedge-Wing Drag)",
                },
                {
                    "mode": MODE_DUAL_BOOK_NAKED,
                    "label": "Full Dual-Book Naked (1-Lot BUY + 1-Lot SELL — Requires ₹1.65L+)",
                    "required_margin_inr": MIN_MARGIN_DUAL_NAKED_INR,
                    "alpha_retention": "100% of Combined Dual-Book Paper Alpha",
                },
                {
                    "mode": MODE_DUAL_BOOK_HEDGED,
                    "label": "Full Dual-Book Hedged (1-Lot BUY + 200-pt OTM Spread — ₹55k Wallet)",
                    "required_margin_inr": MIN_MARGIN_DUAL_HEDGED_INR,
                    "alpha_retention": "100% Long Book + ~50% Short Book (200-pt Wing Drag)",
                },
            ],
        }

    def arm_live_trading(
        self,
        pin: str,
        execution_mode: Optional[str] = None,
        now_dt: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Arms Option Sniffer Advance live broker execution for today after verifying:
          1. 4-digit security PIN (exclusive human authority).
          2. Session cutoff (< 15:15 IST).
          3. Circuit breaker health.
          4. Upstox account readiness and mode-specific margin requirement.
        """
        with self._op_lock:
            if not pin or str(pin).strip() != self._current_pin:
                return {
                    "status": "ARM_REJECTED",
                    "is_live_armed": False,
                    "execution_mode": self._execution_mode,
                    "message": "INVALID_CODE: The 4-digit verification PIN entered is invalid.",
                }

            if execution_mode:
                mode_res = self.set_execution_mode(execution_mode)
                if mode_res.get("status") == "ERROR":
                    return {
                        "status": "ARM_REJECTED",
                        "is_live_armed": False,
                        "execution_mode": self._execution_mode,
                        "message": mode_res.get("message", "Invalid execution mode"),
                    }

            now = to_ist_naive(now_dt)
            if now.time() >= dt_time(15, 15):
                self._is_live_armed = False
                return {
                    "status": "ARM_REJECTED",
                    "is_live_armed": False,
                    "execution_mode": self._execution_mode,
                    "message": "SESSION_CLOSED: Past 15:15 IST cutoff. Real trading cannot be armed after market close.",
                }

            cb = self.get_circuit_breaker_status(now.strftime("%Y-%m-%d"))
            if cb.get("is_session_halted"):
                self._is_live_armed = False
                return {
                    "status": "ARM_REJECTED",
                    "is_live_armed": False,
                    "execution_mode": self._execution_mode,
                    "message": f"LIVE_CIRCUIT_BREAKER_TRIPPED: {cb.get('halt_reason')}",
                }

            base_adapter = self._live_adapter
            readiness = dict(base_adapter.fetch_account_readiness(force_refresh=True) or {})
            is_ready = bool(readiness.get("live_trading_eligible") or readiness.get("ready_for_live"))
            if not is_ready:
                self._is_live_armed = False
                blockers = " | ".join(readiness.get("blockers") or ["Upstox pre-flight checks failed"])
                return {
                    "status": "ARM_REJECTED",
                    "is_live_armed": False,
                    "execution_mode": self._execution_mode,
                    "message": f"PREFLIGHT_BLOCKED: {blockers}",
                    "readiness": readiness,
                }

            avail_margin = float(readiness.get("available_margin") or 0.0)
            req_margin = self.get_mode_margin_requirement(self._execution_mode)
            if avail_margin < req_margin:
                self._is_live_armed = False
                return {
                    "status": "ARM_REJECTED",
                    "is_live_armed": False,
                    "execution_mode": self._execution_mode,
                    "message": (
                        f"Insufficient Upstox margin for '{self._execution_mode}': requires >= ₹{req_margin:,.0f} "
                        f"available margin (found ₹{avail_margin:,.2f}). Use '{MODE_LONG_BOOK_ONLY}' for ₹55k wallets."
                    ),
                    "readiness": readiness,
                }

            self._is_live_armed = True
            self._armed_for_date = now.strftime("%Y-%m-%d")
            self._armed_at_epoch = time.time()
            self._armed_at_iso = now.strftime("%Y-%m-%d %H:%M:%S")
            self.generate_new_pin()
            if self._auto_start_daemon:
                self.start_follower_daemon()

            logger.info(
                f"⚡ [AdvanceLiveBridge] Option Sniffer Advance ARMED in mode [{self._execution_mode}] "
                f"for {self._armed_for_date} at {self._armed_at_iso}."
            )
            return {
                "status": "ARMED",
                "is_live_armed": True,
                "execution_mode": self._execution_mode,
                "available_margin": avail_margin,
                "required_margin": req_margin,
                "message": f"ARMED_FOR_TODAY ({self._execution_mode}): Option Sniffer Advance Live Bridge is active until 15:15 IST.",
                "readiness": readiness,
            }

    def disarm_live_trading(self) -> Dict[str, Any]:
        """Disarms Option Sniffer Advance live trading and reverts immediately to Paper Sandbox."""
        with self._op_lock:
            self._is_live_armed = False
            self._armed_at_epoch = 0.0
            self._armed_at_iso = ""
            new_pin = self.generate_new_pin()
            logger.info("🛡️ [AdvanceLiveBridge] Option Sniffer Advance DISARMED. Reverted to Paper Sandbox Mode.")
            return {
                "status": "DISARMED",
                "is_live_armed": False,
                "execution_mode": self._execution_mode,
                "next_pin": new_pin,
                "message": "DISARMED: Option Sniffer Advance reverted to 100% Paper Sandbox Mode.",
            }

    def start_background_follower(self) -> None:
        self.start_follower_daemon()

    def stop_background_follower(self) -> None:
        with self._op_lock:
            self._follower_running = False

    def start_follower_daemon(self) -> None:
        with self._op_lock:
            if self._follower_running and self._follower_thread and self._follower_thread.is_alive():
                return
            self._follower_running = True
            self._follower_thread = threading.Thread(
                target=self._follower_loop,
                name="AdvanceLiveSQLiteFollower",
                daemon=True,
            )
            self._follower_thread.start()

    def _follower_loop(self) -> None:
        while self._follower_running:
            try:
                has_active = len(self.get_active_live_positions()) > 0
                if self._is_live_armed or has_active:
                    self.sync_once_with_advance_paper_table()
                    time.sleep(0.25)
                else:
                    time.sleep(2.0)
            except Exception as e:
                logger.debug(f"[AdvanceLiveBridge] Follower loop note: {e}")
                time.sleep(1.0)

    def _prioritize_advance_real_time_ticks(
        self,
        active_intents: List[Dict[str, Any]],
        now_dt: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Isolated Real-Time Tick & Stream Priority Guard for Option Sniffer Advance:
          1. Pins all open Advance paper trades and active live intents (`instrument_key` + `hedge_wing_instrument_key`)
             into `OptionStreamManager._key_to_contract` and `UpstoxStreamService` WebSocket V3 so Advance's open
             strikes never drop out of `get_all_active_option_quotes()` when NIFTY/BANKNIFTY shifts by 2+ strikes.
          2. If any open Advance trade's quote is older than 2,500ms (2.5s) while armed or holding a live position,
             proactively calls `refresh_quotes_via_rest(min_interval_sec=2.5)` and feeds the fresh quote directly into
             `AdvancePaperExecution.evaluate_subsecond_tick_guard()` so Chandelier/Reverse-Chandelier and Hard SL
             triggers never wait 10s for Main Option Sniffer's sweep.
        """
        pinned_keys: List[str] = []
        refreshed_via_rest = False
        if not self._is_live_armed and not active_intents:
            return {"pinned_keys": pinned_keys, "refreshed_via_rest": refreshed_via_rest}

        priority_contracts: Dict[str, Dict[str, Any]] = {}
        for intent in active_intents:
            ikey = str(intent.get("instrument_key") or "").strip()
            if ikey:
                priority_contracts[ikey] = {
                    "instrument_key": ikey,
                    "trading_symbol": intent.get("trading_symbol") or ikey,
                    "underlying": intent.get("underlying") or "NIFTY",
                    "strike": float(intent.get("strike") or 0.0),
                    "option_type": intent.get("option_type") or "CE",
                    "expiry": intent.get("expiry") or "",
                    "lot_size": int(intent.get("lot_size") or 65),
                }
            hw_key = str(intent.get("hedge_wing_instrument_key") or "").strip()
            if hw_key and hw_key not in priority_contracts:
                priority_contracts[hw_key] = {
                    "instrument_key": hw_key,
                    "trading_symbol": f"{intent.get('underlying', 'NIFTY')} HEDGE WING",
                    "underlying": intent.get("underlying") or "NIFTY",
                    "strike": float(intent.get("hedge_wing_strike") or 0.0),
                    "option_type": intent.get("option_type") or "CE",
                    "expiry": intent.get("expiry") or "",
                    "lot_size": int(intent.get("lot_size") or 65),
                }

        try:
            with self._get_ro_conn() as ro_conn:
                open_p_rows = ro_conn.execute(
                    """
                    SELECT instrument_key, trading_symbol, underlying, strike, option_type, expiry, lot_size
                    FROM option_sniffer_advance_trades
                    WHERE status = 'OPEN';
                    """
                ).fetchall()
                for r in open_p_rows:
                    pk = str(r["instrument_key"] or "").strip()
                    if pk and pk not in priority_contracts:
                        priority_contracts[pk] = {
                            "instrument_key": pk,
                            "trading_symbol": r["trading_symbol"] or pk,
                            "underlying": r["underlying"] or "NIFTY",
                            "strike": float(r["strike"] or 0.0),
                            "option_type": r["option_type"] or "CE",
                            "expiry": r["expiry"] or "",
                            "lot_size": int(r["lot_size"] or 65),
                        }
        except Exception:
            pass

        if not priority_contracts:
            return {"pinned_keys": pinned_keys, "refreshed_via_rest": refreshed_via_rest}

        try:
            stream_mgr = OptionStreamManager()
            k2c = getattr(stream_mgr, "_key_to_contract", None)
            if isinstance(k2c, dict):
                lock_ctx = getattr(stream_mgr, "_lock", None)
                if lock_ctx is not None and hasattr(lock_ctx, "__enter__"):
                    with lock_ctx:
                        for ik, cmeta in priority_contracts.items():
                            if ik not in k2c:
                                k2c[ik] = cmeta
                                pinned_keys.append(ik)
                else:
                    for ik, cmeta in priority_contracts.items():
                        if ik not in k2c:
                            k2c[ik] = cmeta
                            pinned_keys.append(ik)

                if pinned_keys:
                    try:
                        from app.data.upstox_stream_service import get_stream_service
                        stream_svc = get_stream_service()
                        if hasattr(stream_svc, "subscribe_additional_keys"):
                            stream_svc.subscribe_additional_keys(pinned_keys)
                    except Exception:
                        pass

            # Check quote freshness for all priority open contracts (2,500ms threshold)
            has_stale_priority_quote = False
            for ik in priority_contracts:
                q_obj = stream_mgr.get_option_quote(ik)
                if not isinstance(q_obj, dict):
                    has_stale_priority_quote = True
                    break
                q_age = q_obj.get("quote_age_ms")
                if isinstance(q_age, (int, float)) and float(q_age) > 2500.0:
                    has_stale_priority_quote = True
                    break

            now_mono = time.monotonic()
            if has_stale_priority_quote and (now_mono - self._last_priority_quote_refresh_ts) >= 2.5:
                self._last_priority_quote_refresh_ts = now_mono
                refresh_cnt = 0
                if hasattr(stream_mgr, "refresh_quotes_via_rest"):
                    raw_cnt = stream_mgr.refresh_quotes_via_rest(min_interval_sec=2.5)
                    refresh_cnt = int(raw_cnt) if isinstance(raw_cnt, (int, float)) else 1
                    refreshed_via_rest = refresh_cnt > 0
                if refreshed_via_rest:
                    try:
                        from app.analytics.option_sniffer_advance.advance_paper_execution import AdvancePaperExecution
                        for ik in priority_contracts:
                            fresh_q = stream_mgr.get_option_quote(ik)
                            if not isinstance(fresh_q, dict):
                                continue
                            f_bid = float(fresh_q.get("bid") or 0.0)
                            f_ask = float(fresh_q.get("ask") or f_bid)
                            f_ltp = float(fresh_q.get("ltp") or f_bid)
                            f_age = float(fresh_q.get("quote_age_ms") or 0.0)
                            f_spread_pct = float(
                                fresh_q.get("spread_pct")
                                if fresh_q.get("spread_pct") is not None
                                else (((f_ask - f_bid) / f_ltp * 100.0) if f_ltp > 0 else 999.0)
                            )
                            is_quote_valid = (
                                bool(fresh_q.get("is_valid", True))
                                and f_ltp > 0.0
                                and f_bid > 0.0
                                and f_ask >= f_bid
                                and f_spread_pct <= 3.5
                                and f_age <= 2500.0
                            )
                            if is_quote_valid:
                                AdvancePaperExecution.evaluate_subsecond_tick_guard(ik, fresh_q)
                    except Exception:
                        pass
        except Exception as prio_err:
            logger.debug(f"[AdvanceLiveBridge] Priority tick guard note: {prio_err}")

        return {"pinned_keys": pinned_keys, "refreshed_via_rest": refreshed_via_rest}

    def sync_once_with_advance_paper_table(
        self,
        now_dt: Optional[datetime] = None,
        as_of_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Single deterministic pass of the SQLite WAL Follower:
          0. Enforces 15:15 IST session cutoff and date-rollover auto-disarm directly inside the background follower.
          0B. Prioritizes Option Sniffer Advance open contracts for real-time ticks & 2.5s stale-quote fallback.
          1A. Sub-250ms Local SQLite Exit Fast-Path FIRST (Zero network blocking before checking paper exits).
          1B. Throttled (3.0s) Broker Order-Book Reconciliation for Still-Open Live Positions.
          2. If `_is_live_armed` is True, checks for any newly opened (`status = 'OPEN'` and `entry_time >= _armed_at_iso`)
             trade in `option_sniffer_advance_trades` and mirrors it to Upstox according to `_execution_mode`.
        """
        now_dt = to_ist_naive(now_dt or as_of_time)
        today_str = now_dt.strftime("%Y-%m-%d")
        is_past_cutoff = now_dt.time() >= dt_time(15, 15)
        mirrored_entries: List[str] = []
        mirrored_exits: List[str] = []
        skipped_by_mode: List[Dict[str, Any]] = []

        with self._op_lock:
            if self._is_live_armed:
                if self._armed_for_date and self._armed_for_date != today_str:
                    logger.info("[AdvanceLiveBridge] 1-day session expired on date rollover in follower sync. Reverting to Paper Mode.")
                    self._is_live_armed = False
                elif is_past_cutoff:
                    logger.info("[AdvanceLiveBridge] Session cutoff (15:15 IST) reached in follower sync. Reverting to Paper Mode.")
                    self._is_live_armed = False

        # 0B. Prioritize Option Sniffer Advance open contracts for real-time ticks & 2.5s stale-quote fallback
        active_intents = self.get_active_live_positions()
        self._prioritize_advance_real_time_ticks(active_intents, now_dt=now_dt)

        # 1A. Sub-250ms Local SQLite Exit Fast-Path FIRST (Zero network blocking before checking paper exits)
        if active_intents:
            try:
                with self._get_ro_conn() as ro_conn:
                    ro_cur = ro_conn.cursor()
                    for intent in active_intents:
                        adv_tid = intent.get("advance_trade_id")
                        prow = None
                        if adv_tid:
                            prow = ro_cur.execute(
                                """
                                SELECT status, exit_price_used, exit_bid, exit_ask, exit_reason, net_pnl
                                FROM option_sniffer_advance_trades
                                WHERE trade_id = ? LIMIT 1;
                                """,
                                (adv_tid,),
                            ).fetchone()
                        if prow and str(prow["status"] or "").upper() == "CLOSED":
                            res = self._execute_mirrored_live_exit(
                                intent=intent,
                                paper_row=dict(prow),
                                now_dt=now_dt,
                            )
                            if res.get("exited"):
                                mirrored_exits.append(intent["order_intent_id"])
                        elif is_past_cutoff:
                            # Force-flatten any open live position at 15:15 IST cutoff even if paper row hasn't closed yet
                            res = self._execute_mirrored_live_exit(
                                intent=intent,
                                paper_row={
                                    "exit_price_used": intent.get("current_bid") if intent.get("action") == "BUY" else intent.get("current_ask"),
                                    "exit_reason": "SESSION_CUTOFF_1515_AUTO_EXIT",
                                    "net_pnl": intent.get("unrealized_pnl", 0.0),
                                },
                                now_dt=now_dt,
                            )
                            if res.get("exited"):
                                mirrored_exits.append(intent["order_intent_id"])
            except Exception as ex_err:
                logger.error(f"[AdvanceLiveBridge] Exit sync error: {ex_err}")

        # 1B. Throttled (3.0s) Broker Order-Book Reconciliation for Still-Open Live Positions
        # Prevents 0.25s /order/retrieve-all polling from exhausting the 64 req/min Upstox rate limit
        still_open_intents = self.get_active_live_positions()
        if still_open_intents:
            has_unreconciled_new_intent = any(
                str(i.get("order_intent_id") or "") not in self._reconciled_once_intent_ids
                for i in still_open_intents
            )
            now_mono = time.monotonic()
            mono_elapsed = now_mono - self._last_broker_reconcile_mono
            sim_elapsed = (
                abs((now_dt - self._last_broker_reconcile_sim_dt).total_seconds())
                if (now_dt and self._last_broker_reconcile_sim_dt)
                else 999999.0
            )
            if has_unreconciled_new_intent or (mono_elapsed >= 3.0) or (sim_elapsed >= 3.0):
                self._last_broker_reconcile_mono = now_mono
                self._last_broker_reconcile_sim_dt = now_dt
                for i in still_open_intents:
                    iid = str(i.get("order_intent_id") or "")
                    if iid:
                        self._reconciled_once_intent_ids.add(iid)
                self._reconcile_open_live_intents_with_broker(still_open_intents, now_dt=now_dt)

        # 2. Check Entries if Armed
        if not self._is_live_armed:
            return {
                "status": "STANDBY_PAPER",
                "entries_mirrored": mirrored_entries,
                "exits_mirrored": mirrored_exits,
                "skipped_by_mode": skipped_by_mode,
            }

        cb = self.get_circuit_breaker_status(today_str)
        if cb.get("is_session_halted"):
            self._is_live_armed = False
            return {
                "status": "HALTED_CIRCUIT_BREAKER",
                "entries_mirrored": mirrored_entries,
                "exits_mirrored": mirrored_exits,
                "skipped_by_mode": skipped_by_mode,
            }

        try:
            with self._get_ro_conn() as ro_conn:
                ro_cur = ro_conn.cursor()
                open_rows = ro_cur.execute(
                    """
                    SELECT * FROM option_sniffer_advance_trades
                    WHERE status = 'OPEN'
                      AND SUBSTR(entry_time, 1, 10) = ?
                    ORDER BY id ASC;
                    """,
                    (today_str,),
                ).fetchall()
        except Exception as e:
            logger.debug(f"[AdvanceLiveBridge] Read open paper trades note: {e}")
            open_rows = []

        armed_ts_norm = str(self._armed_at_iso or "").strip().replace("T", " ")[:19]
        for prow in open_rows:
            pdict = dict(prow)
            adv_tid = str(pdict.get("trade_id") or "")
            if not adv_tid:
                continue

            if self._has_intent_for_advance_trade(adv_tid):
                continue

            book_type = str(pdict.get("book_type") or BOOK_LONG_BUY)
            entry_ts_norm = str(pdict.get("entry_time") or "").strip().replace("T", " ")[:19]
            if armed_ts_norm and entry_ts_norm and entry_ts_norm < armed_ts_norm:
                # Never retroactively mirror a stale paper trade that opened BEFORE the user armed live trading
                skipped_by_mode.append(
                    {
                        "trade_id": adv_tid,
                        "book_type": book_type,
                        "reason": "PRE_ARM_OPEN_TRADE_SKIPPED",
                        "entry_time": entry_ts_norm,
                        "armed_at": armed_ts_norm,
                    }
                )
                continue

            if self._execution_mode == MODE_LONG_BOOK_ONLY and book_type != BOOK_LONG_BUY:
                # In LONG_BOOK_ONLY mode (for ₹55k wallet), Book 2 stays strictly in Paper Sandbox
                skipped_by_mode.append(
                    {
                        "trade_id": adv_tid,
                        "book_type": book_type,
                        "reason": "LONG_BOOK_ONLY_SKIP_SHORT",
                    }
                )
                continue

            res = self._execute_mirrored_live_entry(pdict, now_dt=now_dt)
            if res.get("order_intent_id"):
                mirrored_entries.append(res["order_intent_id"])

        return {
            "status": "ARMED_SYNC",
            "entries_mirrored": mirrored_entries,
            "exits_mirrored": mirrored_exits,
            "skipped_by_mode": skipped_by_mode,
        }

    def _has_intent_for_advance_trade(self, advance_trade_id: str) -> bool:
        try:
            with self._get_ro_conn() as conn:
                row = conn.execute(
                    "SELECT 1 FROM option_advance_live_order_intents WHERE advance_trade_id = ? LIMIT 1;",
                    (advance_trade_id,),
                ).fetchone()
                return row is not None
        except Exception:
            return False

    def _flatten_hedge_wing_if_present(
        self,
        hedge_wing_ikey: Optional[str],
        hedge_wing_oid: Optional[str],
        qty: int,
        intent_id: str,
    ) -> None:
        """
        Safeguard #4: Cancels or flattens the 200-pt OTM protective BUY wing whenever:
          - The main SELL leg fails or is rejected on entry (prevents an untracked orphan wing),
          - The main SELL leg's Exchange-Resting SL-L order triggers on Upstox, or
          - The main SELL leg is closed via software/SOS exit.
        """
        if not hedge_wing_ikey and not hedge_wing_oid:
            return
        engine = get_order_intent_engine()
        if hedge_wing_oid:
            try:
                engine._call_upstox_broker_api(
                    "DELETE",
                    "/order/cancel",
                    params={"order_id": str(hedge_wing_oid)},
                    is_emergency=True,
                )
            except Exception as h_cancel_err:
                logger.debug(f"[AdvanceLiveBridge] Cancel hedge wing note: {h_cancel_err}")
        if hedge_wing_ikey:
            try:
                hq = {}
                try:
                    stream_mgr = OptionStreamManager()
                    hq = stream_mgr.get_option_quote(str(hedge_wing_ikey)) or {}
                except Exception:
                    hq = {}
                if not isinstance(hq, dict):
                    hq = {}
                raw_h_bid = hq.get("bid") if (hq.get("bid") is not None and float(hq.get("bid") or 0.0) > 0) else hq.get("ltp")
                h_bid = float(raw_h_bid if (raw_h_bid is not None and float(raw_h_bid or 0.0) > 0) else 2.0)
                h_exit_limit = round_to_tick(max(0.05, h_bid * 0.90))
                hedge_exit_payload = {
                    "quantity": int(qty),
                    "product": "I",
                    "validity": "DAY",
                    "price": float(h_exit_limit),
                    "tag": sanitize_upstox_tag(f"hx-{intent_id[-14:]}"),
                    "instrument_token": str(hedge_wing_ikey),
                    "order_type": "LIMIT",
                    "transaction_type": "SELL",
                    "disclosed_quantity": 0,
                    "trigger_price": 0.0,
                    "is_amo": False,
                }
                engine._call_upstox_broker_api(
                    "POST", "/order/place", payload=hedge_exit_payload, is_emergency=True
                )
            except Exception as he:
                logger.warning(f"[AdvanceLiveBridge] Hedge wing flatten note: {he}")

    def _execute_mirrored_live_entry(
        self,
        paper_trade: Dict[str, Any],
        now_dt: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Places an emergency-priority Upstox V2 entry order mirroring `paper_trade`."""
        now_dt = to_ist_naive(now_dt)
        now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        adv_tid = str(paper_trade["trade_id"])
        book_type = str(paper_trade.get("book_type") or BOOK_LONG_BUY)
        action = "SELL" if book_type == BOOK_SHORT_SELL else "BUY"
        ikey = str(paper_trade["instrument_key"])
        tsym = str(paper_trade["trading_symbol"])
        u_sym = str(paper_trade.get("underlying") or "NIFTY").upper()
        lot_size = int(paper_trade.get("lot_size") or 65)
        qty = int(paper_trade.get("quantity") or lot_size)
        paper_entry = float(paper_trade.get("entry_price_used") or 0.0)
        sl_pct = float(paper_trade.get("stop_loss_pct") or (16.0 if action == "SELL" else 20.0))

        # Marketable limit buffer (0.6% above Ask for BUY; 0.6% below Bid for SELL)
        if action == "BUY":
            limit_price = round_to_tick(paper_entry * 1.006)
            sl_trigger = round_to_tick(paper_entry * (1.0 - sl_pct / 100.0))
            sl_limit = round_to_tick(sl_trigger * 0.985)
        else:
            limit_price = round_to_tick(max(0.05, paper_entry * 0.994))
            sl_trigger = round_to_tick(paper_entry * (1.0 + sl_pct / 100.0))
            sl_limit = round_to_tick(sl_trigger * 1.015)

        intent_id = f"adv_live_{adv_tid[-10:]}"
        order_tag = sanitize_upstox_tag(intent_id[:20])
        engine = get_order_intent_engine()

        # Optional 2-Leg Hedged Spread execution when MODE_DUAL_BOOK_HEDGED is selected for SHORT_BOOK_SELL
        hedge_wing_strike = 0.0
        hedge_wing_ikey = None
        hedge_wing_oid = None
        if self._execution_mode == MODE_DUAL_BOOK_HEDGED and book_type == BOOK_SHORT_SELL:
            try:
                ctx = json.loads(str(paper_trade.get("regime_context_json") or "{}") or "{}")
                opt_t = str(paper_trade.get("option_type") or "CE").upper()
                base_strike = float(paper_trade.get("strike") or 0.0)
                wing_step = 500.0 if "BANK" in u_sym else 200.0
                hedge_wing_strike = float(
                    ctx.get("hedge_wing_strike")
                    or (base_strike + wing_step if opt_t == "CE" else base_strike - wing_step)
                )
                stream_mgr = OptionStreamManager()
                for q_cand in stream_mgr.get_all_active_option_quotes():
                    if (
                        str(q_cand.get("underlying") or "").upper() == u_sym
                        and str(q_cand.get("option_type") or "").upper() == opt_t
                        and abs(float(q_cand.get("strike") or 0.0) - hedge_wing_strike) < 1.0
                    ):
                        hedge_wing_ikey = str(q_cand.get("instrument_key") or "")
                        raw_w_ask = q_cand.get("ask") if (q_cand.get("ask") is not None and float(q_cand.get("ask") or 0.0) > 0) else q_cand.get("ltp")
                        wing_ask = float(raw_w_ask if (raw_w_ask is not None and float(raw_w_ask or 0.0) > 0) else 25.0)
                        wing_payload = {
                            "quantity": qty,
                            "product": "I",
                            "validity": "DAY",
                            "price": float(round_to_tick(wing_ask * 1.02)),
                            "tag": sanitize_upstox_tag(f"hw-{intent_id[-14:]}"),
                            "instrument_token": hedge_wing_ikey,
                            "order_type": "LIMIT",
                            "transaction_type": "BUY",
                            "disclosed_quantity": 0,
                            "trigger_price": 0.0,
                            "is_amo": False,
                        }
                        w_ok, _, w_data = engine._call_upstox_broker_api(
                            "POST", "/order/place", payload=wing_payload, is_emergency=True
                        )
                        if w_ok and isinstance(w_data, dict) and w_data.get("status") == "success":
                            hedge_wing_oid = str(w_data.get("data", {}).get("order_id") or f"UPSTOX_HW_{int(time.time()*1000)}")
                        break
            except Exception as hw_err:
                logger.debug(f"[AdvanceLiveBridge] Hedge wing placement note: {hw_err}")

        payload = {
            "quantity": qty,
            "product": "I",
            "validity": "DAY",
            "price": float(limit_price),
            "tag": order_tag,
            "instrument_token": ikey,
            "order_type": "LIMIT",
            "transaction_type": action,
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
        }

        success, status_code, data = engine._call_upstox_broker_api(
            "POST", "/order/place", payload=payload, is_emergency=True
        )

        broker_sl_oid: Optional[str] = None
        if success and isinstance(data, dict) and data.get("status") == "success":
            broker_oid = str(data.get("data", {}).get("order_id") or f"UPSTOX_ADV_{int(time.time()*1000)}")
            state = "FILLED"
            avg_fill = paper_entry
            rejection_reason = None

            # Safeguard #3: Place Linked Exchange-Resting Stop-Loss Limit (SL-L) Order on Upstox
            # Protects the position on Upstox's exchange servers even if local Wi-Fi/power drops mid-trade.
            try:
                sl_tx_type = "SELL" if action == "BUY" else "BUY"
                sl_payload = {
                    "quantity": qty,
                    "product": "I",
                    "validity": "DAY",
                    "price": float(sl_limit),
                    "tag": sanitize_upstox_tag(f"sl-{intent_id[-14:]}"),
                    "instrument_token": ikey,
                    "order_type": "SL",
                    "transaction_type": sl_tx_type,
                    "disclosed_quantity": 0,
                    "trigger_price": float(sl_trigger),
                    "is_amo": False,
                }
                sl_ok, _, sl_data = engine._call_upstox_broker_api(
                    "POST", "/order/place", payload=sl_payload, is_emergency=True
                )
                if sl_ok and isinstance(sl_data, dict) and sl_data.get("status") == "success":
                    broker_sl_oid = str(
                        sl_data.get("data", {}).get("order_id") or f"UPSTOX_ADV_SL_{int(time.time()*1000)}"
                    )
            except Exception as sl_err:
                logger.warning(f"[AdvanceLiveBridge] Exchange SL-L placement note for {intent_id}: {sl_err}")
        else:
            # If the protective BUY hedge wing was placed right before the main SELL leg failed/rejected,
            # immediately cancel and flatten the hedge wing so it never becomes an untracked orphan!
            if hedge_wing_ikey or hedge_wing_oid:
                self._flatten_hedge_wing_if_present(
                    hedge_wing_ikey=hedge_wing_ikey,
                    hedge_wing_oid=hedge_wing_oid,
                    qty=qty,
                    intent_id=intent_id,
                )
            broker_oid = None
            state = "REJECTED"
            avg_fill = 0.0
            rejection_reason = str((data or {}).get("error") or f"HTTP_{status_code}")

        with self._get_rw_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO option_advance_live_order_intents (
                    order_intent_id, advance_trade_id, signal_id, execution_mode,
                    book_type, strategy_mode, direction, action,
                    instrument_key, trading_symbol, underlying, strike, option_type, expiry,
                    lot_size, target_quantity, filled_quantity, limit_price, average_fill_price,
                    state, broker_order_id, stop_loss_trigger, stop_loss_limit, broker_sl_order_id,
                    hedge_wing_strike, hedge_wing_instrument_key, hedge_wing_broker_order_id,
                    paper_entry_price, exit_state, rejection_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    intent_id,
                    adv_tid,
                    paper_trade.get("signal_id"),
                    self._execution_mode,
                    book_type,
                    str(paper_trade.get("strategy_mode") or ""),
                    str(paper_trade.get("direction") or ""),
                    action,
                    ikey,
                    tsym,
                    u_sym,
                    float(paper_trade.get("strike") or 0.0),
                    str(paper_trade.get("option_type") or "CE"),
                    str(paper_trade.get("expiry") or ""),
                    lot_size,
                    qty,
                    qty if state == "FILLED" else 0,
                    limit_price,
                    avg_fill,
                    state,
                    broker_oid,
                    sl_trigger,
                    sl_limit,
                    broker_sl_oid,
                    hedge_wing_strike,
                    hedge_wing_ikey,
                    hedge_wing_oid,
                    paper_entry,
                    "NONE",
                    rejection_reason,
                    now_iso,
                    now_iso,
                ),
            )
            conn.commit()

        return {
            "order_intent_id": intent_id,
            "state": state,
            "broker_order_id": broker_oid,
            "broker_sl_order_id": broker_sl_oid,
            "rejection_reason": rejection_reason,
        }

    def _reconcile_open_live_intents_with_broker(
        self,
        active_intents: List[Dict[str, Any]],
        now_dt: Optional[datetime] = None,
    ) -> None:
        """
        Safeguard #2: Reconciles open live intents against Upstox `/order/retrieve-all`:
          1. If the resting exchange `broker_sl_order_id` was triggered & completed on Upstox,
             immediately marks `exit_state = 'CONFIRMED_FLAT'` (`EXCHANGE_SL_ORDER_TRIGGERED`)
             and flattens any protective hedge wing so the bridge never fires a duplicate exit order.
          2. If an entry `broker_order_id` was rejected/cancelled on the exchange with 0 fills,
             cancels any resting SL, flattens any hedge wing, and marks `state = 'CANCELLED'`, `exit_state = 'CONFIRMED_FLAT'`.
          3. If an entry `broker_order_id` is still `'open'` (0 fills on exchange), transitions `state = 'SUBMITTED'`,
             `filled_quantity = 0`, and cancels any premature SL until the entry fills.
          4. If the entry order completed with an actual exchange `average_price`, updates `state = 'FILLED'`,
             `average_fill_price`, and ensures a resting SL-L is active.
        """
        if not active_intents:
            return
        try:
            engine = get_order_intent_engine()
            ok, _, ob_data = engine._call_upstox_broker_api("GET", "/order/retrieve-all", is_emergency=False)
            if not ok or not isinstance(ob_data, dict):
                return
            orders_list = ob_data.get("data")
            if not isinstance(orders_list, list) or not orders_list:
                return

            order_map = {
                str(o.get("order_id")): o
                for o in orders_list
                if isinstance(o, dict) and o.get("order_id")
            }
            if not order_map:
                return

            now_iso = to_ist_naive(now_dt).strftime("%Y-%m-%d %H:%M:%S")
            for intent in active_intents:
                intent_id = str(intent.get("order_intent_id") or "")
                action = str(intent.get("action") or "BUY").upper()
                qty = int(intent.get("filled_quantity") or intent.get("target_quantity") or 65)
                target_qty = int(intent.get("target_quantity") or qty or 65)
                entry_fill = float(intent.get("average_fill_price") or intent.get("limit_price") or 0.0)

                # A. Check if Exchange-Resting SL-L Order triggered and completed on Upstox
                sl_oid = str(intent.get("broker_sl_order_id") or "")
                if sl_oid and sl_oid in order_map:
                    sl_ord = order_map[sl_oid]
                    sl_status = str(sl_ord.get("status") or "").lower()
                    if sl_status == "complete":
                        sl_fill = float(sl_ord.get("average_price") or intent.get("stop_loss_trigger") or entry_fill)
                        if action == "SELL" and intent.get("hedge_wing_instrument_key"):
                            self._flatten_hedge_wing_if_present(
                                hedge_wing_ikey=str(intent.get("hedge_wing_instrument_key")),
                                hedge_wing_oid=intent.get("hedge_wing_broker_order_id"),
                                qty=target_qty,
                                intent_id=intent_id,
                            )
                        if action == "BUY":
                            gross = round((sl_fill - entry_fill) * target_qty, 2)
                            costs = calculate_option_trade_costs(
                                buy_price=entry_fill,
                                sell_price=sl_fill,
                                quantity=target_qty,
                                apply_slippage=False,
                                trade_date=now_iso[:10],
                            )
                            charges = float(costs.get("statutory_charges") or 55.0)
                            net_pnl = round(gross - charges, 2)
                        else:
                            res_c = calculate_short_option_trade_costs(
                                sell_entry_bid=entry_fill,
                                buy_exit_ask=sl_fill,
                                quantity=target_qty,
                                apply_slippage=False,
                                trade_date=now_iso[:10],
                            )
                            gross = float(res_c.get("gross_pnl") or 0.0)
                            charges = float(res_c.get("statutory_charges") or 55.0)
                            net_pnl = round(gross - charges, 2)

                        with self._get_rw_conn() as conn:
                            conn.execute(
                                """
                                UPDATE option_advance_live_order_intents
                                SET exit_state = 'CONFIRMED_FLAT',
                                    exit_broker_order_id = ?,
                                    exit_fill_price = ?,
                                    exit_reason = 'EXCHANGE_SL_ORDER_TRIGGERED',
                                    gross_pnl = ?,
                                    statutory_charges = ?,
                                    realized_pnl = ?,
                                    updated_at = ?
                                WHERE order_intent_id = ?;
                                """,
                                (sl_oid, sl_fill, gross, charges, net_pnl, now_iso, intent_id),
                            )
                            conn.commit()
                        continue

                # B. Check Entry Order actual fill price, working open state, or exchange cancellation/rejection
                entry_oid = str(intent.get("broker_order_id") or "")
                if entry_oid and entry_oid in order_map:
                    ent_ord = order_map[entry_oid]
                    ent_status = str(ent_ord.get("status") or "").lower()
                    exch_avg = float(ent_ord.get("average_price") or 0.0)
                    exch_qty = int(ent_ord.get("filled_quantity") or 0)
                    if ent_status in ("rejected", "cancelled") and exch_qty == 0:
                        if sl_oid:
                            try:
                                engine._call_upstox_broker_api(
                                    "DELETE",
                                    "/order/cancel",
                                    params={"order_id": str(sl_oid)},
                                    is_emergency=True,
                                )
                            except Exception:
                                pass
                        if action == "SELL" and (intent.get("hedge_wing_instrument_key") or intent.get("hedge_wing_broker_order_id")):
                            self._flatten_hedge_wing_if_present(
                                hedge_wing_ikey=intent.get("hedge_wing_instrument_key"),
                                hedge_wing_oid=intent.get("hedge_wing_broker_order_id"),
                                qty=target_qty,
                                intent_id=intent_id,
                            )
                        rej_msg = str(ent_ord.get("status_message") or f"Exchange {ent_status}")
                        with self._get_rw_conn() as conn:
                            conn.execute(
                                """
                                UPDATE option_advance_live_order_intents
                                SET state = 'CANCELLED',
                                    filled_quantity = 0,
                                    exit_state = 'CONFIRMED_FLAT',
                                    rejection_reason = ?,
                                    updated_at = ?
                                WHERE order_intent_id = ?;
                                """,
                                (rej_msg, now_iso, intent_id),
                            )
                            conn.commit()
                    elif ent_status in ("open", "trigger pending", "validation pending", "put order req received") and exch_qty == 0:
                        # Entry order has NOT filled yet on NSE: cancel any premature SL-L and mark state = SUBMITTED (0 filled)
                        if sl_oid:
                            try:
                                engine._call_upstox_broker_api(
                                    "DELETE",
                                    "/order/cancel",
                                    params={"order_id": str(sl_oid)},
                                    is_emergency=True,
                                )
                            except Exception:
                                pass
                        with self._get_rw_conn() as conn:
                            conn.execute(
                                """
                                UPDATE option_advance_live_order_intents
                                SET state = 'SUBMITTED',
                                    filled_quantity = 0,
                                    broker_sl_order_id = NULL,
                                    updated_at = ?
                                WHERE order_intent_id = ?;
                                """,
                                (now_iso, intent_id),
                            )
                            conn.commit()
                    elif ent_status == "complete" or exch_qty > 0:
                        eff_avg = exch_avg if exch_avg > 0 else entry_fill
                        eff_qty = exch_qty if exch_qty > 0 else target_qty
                        new_sl_oid = sl_oid or None
                        if not new_sl_oid:
                            try:
                                sl_tx_type = "SELL" if action == "BUY" else "BUY"
                                sl_payload = {
                                    "quantity": eff_qty,
                                    "product": "I",
                                    "validity": "DAY",
                                    "price": float(intent.get("stop_loss_limit") or 0.05),
                                    "tag": sanitize_upstox_tag(f"sl-{intent_id[-14:]}"),
                                    "instrument_token": str(intent.get("instrument_key") or ""),
                                    "order_type": "SL",
                                    "transaction_type": sl_tx_type,
                                    "disclosed_quantity": 0,
                                    "trigger_price": float(intent.get("stop_loss_trigger") or 0.05),
                                    "is_amo": False,
                                }
                                sl_ok, _, sl_data = engine._call_upstox_broker_api(
                                    "POST", "/order/place", payload=sl_payload, is_emergency=True
                                )
                                if sl_ok and isinstance(sl_data, dict) and sl_data.get("status") == "success":
                                    new_sl_oid = str(sl_data.get("data", {}).get("order_id") or f"UPSTOX_ADV_SL_{int(time.time()*1000)}")
                            except Exception:
                                pass
                        with self._get_rw_conn() as conn:
                            conn.execute(
                                """
                                UPDATE option_advance_live_order_intents
                                SET state = 'FILLED',
                                    average_fill_price = ?,
                                    filled_quantity = ?,
                                    broker_sl_order_id = COALESCE(?, broker_sl_order_id),
                                    updated_at = ?
                                WHERE order_intent_id = ?;
                                """,
                                (eff_avg, eff_qty, new_sl_oid, now_iso, intent_id),
                            )
                            conn.commit()
        except Exception as rec_err:
            logger.debug(f"[AdvanceLiveBridge] Order book reconciliation note: {rec_err}")

    def _execute_mirrored_live_exit(
        self,
        intent: Dict[str, Any],
        paper_row: Dict[str, Any],
        now_dt: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Fires an emergency-priority Upstox V2 exit order the instant the parent Paper trade closes.
        Includes:
          - Unfilled entry guard: if entry order never filled (`state == 'SUBMITTED'` and `filled_quantity == 0`),
            cancels the entry order instead of sending an opposite exit order.
          - Safe exit price resolution BEFORE cancelling the resting `broker_sl_order_id`.
          - Simultaneous SL-fill race guard: if cancelling `broker_sl_order_id` indicates the SL already completed
            on the exchange, reconciles and marks `CONFIRMED_FLAT` without firing a duplicate exit order.
          - Emergency retry with wider 5% (or 15% on SOS without stream quote) marketable limit buffer.
          - Fail-Safe `EXIT_RETRY_PENDING` state if both attempts fail so the 250ms follower loop retries.
        """
        now_dt = to_ist_naive(now_dt)
        now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        intent_id = str(intent["order_intent_id"])
        action = str(intent.get("action") or "BUY").upper()
        exit_tx_type = "SELL" if action == "BUY" else "BUY"
        qty = int(intent.get("filled_quantity") or intent.get("target_quantity") or 65)
        ikey = str(intent["instrument_key"])
        entry_fill = float(intent.get("average_fill_price") or intent.get("limit_price") or 100.0)
        exit_reason = str(paper_row.get("exit_reason") or "MIRRORED_PAPER_EXIT")
        engine = get_order_intent_engine()

        # 0A. Unfilled Entry Guard: If entry order is still SUBMITTED with 0 filled quantity, cancel entry order!
        if str(intent.get("state") or "").upper() == "SUBMITTED" and int(intent.get("filled_quantity") or 0) == 0:
            entry_oid = intent.get("broker_order_id")
            if entry_oid:
                try:
                    engine._call_upstox_broker_api(
                        "DELETE", "/order/cancel", params={"order_id": str(entry_oid)}, is_emergency=True
                    )
                except Exception:
                    pass
            broker_sl_oid = intent.get("broker_sl_order_id")
            if broker_sl_oid:
                try:
                    engine._call_upstox_broker_api(
                        "DELETE", "/order/cancel", params={"order_id": str(broker_sl_oid)}, is_emergency=True
                    )
                except Exception:
                    pass
            if action == "SELL" and (intent.get("hedge_wing_instrument_key") or intent.get("hedge_wing_broker_order_id")):
                self._flatten_hedge_wing_if_present(
                    hedge_wing_ikey=intent.get("hedge_wing_instrument_key"),
                    hedge_wing_oid=intent.get("hedge_wing_broker_order_id"),
                    qty=int(intent.get("target_quantity") or 65),
                    intent_id=intent_id,
                )
            with self._get_rw_conn() as conn:
                conn.execute(
                    """
                    UPDATE option_advance_live_order_intents
                    SET state = 'CANCELLED',
                        filled_quantity = 0,
                        exit_state = 'CONFIRMED_FLAT',
                        exit_reason = 'UNFILLED_ENTRY_CANCELLED_ON_EXIT',
                        realized_pnl = 0.0,
                        updated_at = ?
                    WHERE order_intent_id = ?;
                    """,
                    (now_iso, intent_id),
                )
                conn.commit()
            return {"exited": True, "order_intent_id": intent_id, "realized_pnl": 0.0, "cancelled_unfilled": True}

        # 0B. Resolve exit price safely BEFORE cancelling the resting SL-L order (prevents any NoneType crash after SL cancel)
        raw_exit_px = paper_row.get("exit_price_used")
        paper_exit = float(raw_exit_px) if (raw_exit_px is not None and float(raw_exit_px or 0.0) > 0) else 0.0
        has_live_exit_quote = paper_exit > 0
        if paper_exit <= 0:
            q: Dict[str, Any] = {}
            try:
                stream_mgr = OptionStreamManager()
                q_res = stream_mgr.get_option_quote(ikey)
                if isinstance(q_res, dict):
                    q = q_res
            except Exception:
                q = {}
            side_px = q.get("bid") if exit_tx_type == "SELL" else q.get("ask")
            cand_px = side_px if (side_px is not None and float(side_px or 0.0) > 0) else q.get("ltp")
            if cand_px is not None and float(cand_px or 0.0) > 0:
                paper_exit = float(cand_px)
                has_live_exit_quote = True
            else:
                paper_exit = float(entry_fill if entry_fill > 0 else 100.0)

        # 0C. Cancel resting Exchange SL-L order & guard against simultaneous SL-fill race condition
        broker_sl_oid = intent.get("broker_sl_order_id")
        if broker_sl_oid:
            sl_cancel_ok = True
            sl_cancel_data: Any = {}
            try:
                sl_cancel_ok, _, sl_cancel_data = engine._call_upstox_broker_api(
                    "DELETE",
                    "/order/cancel",
                    params={"order_id": str(broker_sl_oid)},
                    is_emergency=True,
                )
            except Exception as sl_cancel_err:
                logger.debug(f"[AdvanceLiveBridge] Cancel resting SL note for {broker_sl_oid}: {sl_cancel_err}")

            sl_err_text = str(sl_cancel_data or "").lower()
            if not sl_cancel_ok and ("complete" in sl_err_text or "already" in sl_err_text or "filled" in sl_err_text):
                # Exchange SL-L already filled milliseconds before software exit! Do NOT send a duplicate exit order!
                self._reconcile_open_live_intents_with_broker([intent], now_dt=now_dt)
                still_open = [p for p in self.get_active_live_positions() if p.get("order_intent_id") == intent_id]
                if not still_open:
                    return {"exited": True, "order_intent_id": intent_id, "already_closed_by_sl": True}

                sl_fill = float(intent.get("stop_loss_trigger") or paper_exit or entry_fill)
                if action == "SELL" and intent.get("hedge_wing_instrument_key"):
                    self._flatten_hedge_wing_if_present(
                        hedge_wing_ikey=str(intent.get("hedge_wing_instrument_key")),
                        hedge_wing_oid=intent.get("hedge_wing_broker_order_id"),
                        qty=qty,
                        intent_id=intent_id,
                    )
                if action == "BUY":
                    gross = round((sl_fill - entry_fill) * qty, 2)
                    costs = calculate_option_trade_costs(
                        buy_price=entry_fill,
                        sell_price=sl_fill,
                        quantity=qty,
                        apply_slippage=False,
                        trade_date=now_iso[:10],
                    )
                    charges = float(costs.get("statutory_charges") or 55.0)
                    net_pnl = round(gross - charges, 2)
                else:
                    res_c = calculate_short_option_trade_costs(
                        sell_entry_bid=entry_fill,
                        buy_exit_ask=sl_fill,
                        quantity=qty,
                        apply_slippage=False,
                        trade_date=now_iso[:10],
                    )
                    gross = float(res_c.get("gross_pnl") or 0.0)
                    charges = float(res_c.get("statutory_charges") or 55.0)
                    net_pnl = round(gross - charges, 2)

                with self._get_rw_conn() as conn:
                    conn.execute(
                        """
                        UPDATE option_advance_live_order_intents
                        SET exit_state = 'CONFIRMED_FLAT',
                            exit_broker_order_id = ?,
                            exit_fill_price = ?,
                            exit_reason = 'EXCHANGE_SL_ORDER_TRIGGERED',
                            paper_exit_price = ?,
                            paper_net_pnl = ?,
                            gross_pnl = ?,
                            statutory_charges = ?,
                            realized_pnl = ?,
                            rejection_reason = NULL,
                            updated_at = ?
                        WHERE order_intent_id = ?;
                        """,
                        (
                            str(broker_sl_oid),
                            sl_fill,
                            paper_exit,
                            float(paper_row.get("net_pnl") or 0.0),
                            gross,
                            charges,
                            net_pnl,
                            now_iso,
                            intent_id,
                        ),
                    )
                    conn.commit()
                logger.info(
                    f"🛡️ [AdvanceLiveBridge] Zero-Duplicate-Exit Guard: Exchange SL-L ({broker_sl_oid}) already completed "
                    f"for {intent_id}; suppressed duplicate software exit order."
                )
                return {"exited": True, "order_intent_id": intent_id, "realized_pnl": net_pnl, "already_closed_by_sl": True}

        is_emergency_reason = exit_reason in ("SOS_PANIC_EXIT", "SESSION_CUTOFF_1515_AUTO_EXIT")
        if is_emergency_reason and not has_live_exit_quote:
            # When stream quote is missing during an emergency SOS/15:15 exit, use a wider 15% marketable collar
            # so the limit order still crosses the NSE order book even if the option moved sharply from entry.
            if exit_tx_type == "SELL":
                exit_limit = round_to_tick(max(0.05, paper_exit * 0.85))
            else:
                exit_limit = round_to_tick(paper_exit * 1.15)
        else:
            if exit_tx_type == "SELL":
                exit_limit = round_to_tick(max(0.05, paper_exit * 0.985))
            else:
                exit_limit = round_to_tick(paper_exit * 1.015)

        payload = {
            "quantity": qty,
            "product": "I",
            "validity": "DAY",
            "price": float(exit_limit),
            "tag": sanitize_upstox_tag(f"ex-{intent_id[-14:]}"),
            "instrument_token": ikey,
            "order_type": "LIMIT",
            "transaction_type": exit_tx_type,
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
        }

        success, status_code, data = engine._call_upstox_broker_api(
            "POST", "/order/place", payload=payload, is_emergency=True
        )
        exit_ok = bool(success and isinstance(data, dict) and data.get("status") == "success")

        # Safeguard #1A: Immediate Emergency Retry with wider 5% marketable collar if primary exit call failed
        if not exit_ok:
            retry_limit = (
                round_to_tick(max(0.05, paper_exit * 0.95))
                if exit_tx_type == "SELL"
                else round_to_tick(paper_exit * 1.05)
            )
            retry_payload = dict(payload, price=float(retry_limit), tag=sanitize_upstox_tag(f"exr-{intent_id[-13:]}"))
            success, status_code, data = engine._call_upstox_broker_api(
                "POST", "/order/place", payload=retry_payload, is_emergency=True
            )
            exit_ok = bool(success and isinstance(data, dict) and data.get("status") == "success")

        # Safeguard #1B: Never mark CONFIRMED_FLAT if both exit HTTP calls failed!
        # Keep in EXIT_RETRY_PENDING so `get_active_live_positions()` returns it on the next 250ms tick.
        if not exit_ok:
            err_reason = str((data or {}).get("error") or f"EXIT_HTTP_{status_code}")
            with self._get_rw_conn() as conn:
                conn.execute(
                    """
                    UPDATE option_advance_live_order_intents
                    SET exit_state = 'EXIT_RETRY_PENDING',
                        rejection_reason = ?,
                        updated_at = ?
                    WHERE order_intent_id = ?;
                    """,
                    (err_reason, now_iso, intent_id),
                )
                conn.commit()
            logger.error(
                f"⚠️ [AdvanceLiveBridge] Exit order failed for {intent_id} ({err_reason}); "
                f"kept in EXIT_RETRY_PENDING for automatic 250ms retry."
            )
            return {
                "exited": False,
                "retry_pending": True,
                "order_intent_id": intent_id,
                "error": err_reason,
            }

        exit_oid = str(data.get("data", {}).get("order_id") or f"UPSTOX_ADV_EX_{int(time.time()*1000)}")

        # If this was a hedged short trade (DUAL_BOOK_HEDGED), also flatten the 200-pt OTM protective BUY wing
        hedge_wing_ikey = intent.get("hedge_wing_instrument_key")
        if hedge_wing_ikey and action == "SELL":
            self._flatten_hedge_wing_if_present(
                hedge_wing_ikey=str(hedge_wing_ikey),
                hedge_wing_oid=intent.get("hedge_wing_broker_order_id"),
                qty=qty,
                intent_id=intent_id,
            )

        exit_fill = paper_exit
        if action == "BUY":
            gross = round((exit_fill - entry_fill) * qty, 2)
            costs = calculate_option_trade_costs(
                buy_price=entry_fill,
                sell_price=exit_fill,
                quantity=qty,
                apply_slippage=False,
                trade_date=now_iso[:10],
            )
            charges = float(costs.get("statutory_charges") or 55.0)
            net_pnl = round(gross - charges, 2)
        else:
            res_c = calculate_short_option_trade_costs(
                sell_entry_bid=entry_fill,
                buy_exit_ask=exit_fill,
                quantity=qty,
                apply_slippage=False,
                trade_date=now_iso[:10],
            )
            gross = float(res_c.get("gross_pnl") or 0.0)
            charges = float(res_c.get("statutory_charges") or 55.0)
            net_pnl = round(gross - charges, 2)

        paper_net = float(paper_row.get("net_pnl") or 0.0)

        with self._get_rw_conn() as conn:
            conn.execute(
                """
                UPDATE option_advance_live_order_intents
                SET exit_state = 'CONFIRMED_FLAT',
                    exit_broker_order_id = ?,
                    exit_fill_price = ?,
                    exit_reason = ?,
                    paper_exit_price = ?,
                    paper_net_pnl = ?,
                    gross_pnl = ?,
                    statutory_charges = ?,
                    realized_pnl = ?,
                    rejection_reason = NULL,
                    updated_at = ?
                WHERE order_intent_id = ?;
                """,
                (
                    exit_oid,
                    exit_fill,
                    exit_reason,
                    paper_exit,
                    paper_net,
                    gross,
                    charges,
                    net_pnl,
                    now_iso,
                    intent_id,
                ),
            )
            conn.commit()

        logger.info(
            f"⚡ [AdvanceLiveBridge] Mirrored Exit ({exit_reason}) for {intent.get('trading_symbol')}: "
            f"Entry ₹{entry_fill:.2f} -> Exit ₹{exit_fill:.2f} | Net PnL ₹{net_pnl:+.2f}"
        )
        return {"exited": True, "order_intent_id": intent_id, "realized_pnl": net_pnl}

    def execute_sos_panic_exit(self) -> Dict[str, Any]:
        """
        Emergency 1-Click SOS Panic Exit:
        Disarms live bridge and immediately flattens all open Advance live positions.
        """
        with self._op_lock:
            self._is_live_armed = False
            active = self.get_active_live_positions()
            flattened = []
            now_dt = to_ist_naive()
            for pos in active:
                res = self._execute_mirrored_live_exit(
                    intent=pos,
                    paper_row={
                        "exit_price_used": pos.get("current_bid") if pos.get("action") == "BUY" else pos.get("current_ask"),
                        "exit_reason": "SOS_PANIC_EXIT",
                        "net_pnl": pos.get("unrealized_pnl", 0.0),
                    },
                    now_dt=now_dt,
                )
                if res.get("exited"):
                    flattened.append(pos["order_intent_id"])

            self.generate_new_pin()
            return {
                "status": "SOS_EXECUTED_AND_DISARMED",
                "is_live_armed": False,
                "positions_flattened": len(flattened),
                "flattened_intent_ids": flattened,
                "message": f"SOS Panic Square-Off completed. Flattened {len(flattened)} position(s) and disarmed to Paper Sandbox.",
            }

    def get_active_live_positions(self) -> List[Dict[str, Any]]:
        """Returns currently open live positions from `option_advance_live_order_intents` enriched with live MTM."""
        try:
            with self._get_ro_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM option_advance_live_order_intents
                    WHERE state IN ('SUBMITTED', 'PARTIALLY_FILLED', 'FILLED')
                      AND exit_state != 'CONFIRMED_FLAT'
                    ORDER BY id DESC;
                    """
                ).fetchall()
                positions = [dict(r) for r in rows]

            if positions:
                stream_mgr = OptionStreamManager()
                for pos in positions:
                    ikey = pos.get("instrument_key")
                    q = stream_mgr.get_option_quote(ikey) if ikey else None
                    if q:
                        cbid = float(q.get("bid") or q.get("ltp") or 0.0)
                        cask = float(q.get("ask") or q.get("ltp") or cbid)
                        pos["current_bid"] = round(cbid, 2)
                        pos["current_ask"] = round(cask, 2)
                        entry_f = float(pos.get("average_fill_price") or pos.get("limit_price") or 0.0)
                        qty = int(pos.get("filled_quantity") or pos.get("target_quantity") or 65)
                        if entry_f > 0 and qty > 0:
                            if pos.get("action") == "BUY" and cbid > 0:
                                gross = (cbid - entry_f) * qty
                            elif pos.get("action") == "SELL" and cask > 0:
                                gross = (entry_f - cask) * qty
                            else:
                                gross = 0.0
                            pos["unrealized_pnl"] = round(gross - 65.0, 2)
            return positions
        except Exception as e:
            logger.debug(f"[AdvanceLiveBridge] Active positions query note: {e}")
            return []

    def get_recent_order_intents(self, limit: int = 25) -> List[Dict[str, Any]]:
        try:
            with self._get_ro_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM option_advance_live_order_intents ORDER BY id DESC LIMIT ?;",
                    (int(limit),),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    def get_live_pnl_summary(self) -> Dict[str, Any]:
        today = to_ist_naive().strftime("%Y-%m-%d")
        realized_gross = 0.0
        statutory = 0.0
        realized_net = 0.0
        wins = 0
        losses = 0
        closed_count = 0
        try:
            with self._get_ro_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT gross_pnl, statutory_charges, realized_pnl
                    FROM option_advance_live_order_intents
                    WHERE exit_state = 'CONFIRMED_FLAT'
                      AND SUBSTR(created_at, 1, 10) = ?;
                    """,
                    (today,),
                ).fetchall()
                for r in rows:
                    g = float(r[0] or 0.0)
                    s = float(r[1] or 0.0)
                    n = float(r[2] or 0.0)
                    realized_gross += g
                    statutory += s
                    realized_net += n
                    closed_count += 1
                    if n > 0:
                        wins += 1
                    else:
                        losses += 1
        except Exception:
            pass

        active = self.get_active_live_positions()
        unrealized = sum(float(p.get("unrealized_pnl") or 0.0) for p in active)
        return {
            "session_date": today,
            "realized_gross_pnl": round(realized_gross, 2),
            "statutory_charges": round(statutory, 2),
            "realized_net_pnl": round(realized_net, 2),
            "unrealized_net_pnl": round(unrealized, 2),
            "total_net_pnl": round(realized_net + unrealized, 2),
            "closed_trades_count": closed_count,
            "win_count": wins,
            "loss_count": losses,
            "win_rate": round((wins / closed_count) * 100.0, 1) if closed_count > 0 else 0.0,
            "active_positions_count": len(active),
        }

    def get_calendar_performance(self, book_filter: str = "ALL") -> Dict[str, Any]:
        """
        Read-only Day-by-Day & Month-by-Month P&L Calendar aggregation from `option_sniffer_advance_trades`
        (or `option_advance_live_order_intents` when `book_filter == 'LIVE_BROKER'`).
        Touches 0 bytes of core Option Sniffer Advance files (`mode=ro`).
        """
        flt = str(book_filter or "ALL").upper().strip()
        cur_month = to_ist_naive().strftime("%Y-%m")
        months_set = {cur_month}
        days_map: Dict[str, Dict[str, Any]] = {}

        try:
            with self._get_ro_conn() as conn:
                if flt == "LIVE_BROKER":
                    rows = conn.execute(
                        """
                        SELECT
                            order_intent_id AS trade_id,
                            signal_id,
                            book_type,
                            strategy_mode,
                            direction,
                            instrument_key,
                            trading_symbol,
                            underlying,
                            option_type,
                            strike,
                            expiry,
                            lot_size,
                            target_quantity AS quantity,
                            created_at AS entry_time,
                            updated_at AS exit_time,
                            average_fill_price AS entry_price_used,
                            exit_fill_price AS exit_price_used,
                            0.0 AS mfe_pct,
                            0.0 AS mae_pct,
                            gross_pnl,
                            statutory_charges AS charges,
                            0.0 AS slippage,
                            realized_pnl AS net_pnl,
                            exit_reason
                        FROM option_advance_live_order_intents
                        WHERE exit_state = 'CONFIRMED_FLAT'
                        ORDER BY created_at ASC;
                        """
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT *
                        FROM option_sniffer_advance_trades
                        WHERE status = 'CLOSED'
                        ORDER BY entry_time ASC;
                        """
                    ).fetchall()
        except Exception as e:
            logger.debug(f"[AdvanceLiveBridge] Calendar query note: {e}")
            rows = []

        for r in rows:
            t = dict(r)
            btype = str(t.get("book_type") or BOOK_LONG_BUY)
            entry_t = str(t.get("entry_time") or "")
            day_str = entry_t[:10]
            if len(day_str) != 10:
                continue
            month_str = day_str[:7]
            months_set.add(month_str)

            if flt in (BOOK_LONG_BUY, BOOK_SHORT_SELL) and btype != flt:
                continue

            t["entry_price"] = float(t.get("entry_price_used") or 0.0)
            t["exit_price"] = float(t.get("exit_price_used") or 0.0)
            t["strike_price"] = float(t.get("strike") or 0.0)
            net = float(t.get("net_pnl") or 0.0)
            gross = float(t.get("gross_pnl") or 0.0)
            charges = float(t.get("charges") or 0.0) + float(t.get("slippage") or 0.0)

            if day_str not in days_map:
                days_map[day_str] = {
                    "date": day_str,
                    "month": month_str,
                    "trade_count": 0,
                    "wins": 0,
                    "losses": 0,
                    "win_rate": 0.0,
                    "gross_pnl_rupees": 0.0,
                    "charges_rupees": 0.0,
                    "net_pnl_rupees": 0.0,
                    "long_book_net_pnl": 0.0,
                    "short_book_net_pnl": 0.0,
                    "long_book_trades": 0,
                    "short_book_trades": 0,
                    "trades": [],
                }

            d_rec = days_map[day_str]
            d_rec["trade_count"] += 1
            if net > 0:
                d_rec["wins"] += 1
            else:
                d_rec["losses"] += 1
            d_rec["gross_pnl_rupees"] = round(d_rec["gross_pnl_rupees"] + gross, 2)
            d_rec["charges_rupees"] = round(d_rec["charges_rupees"] + charges, 2)
            d_rec["net_pnl_rupees"] = round(d_rec["net_pnl_rupees"] + net, 2)
            if btype == BOOK_LONG_BUY:
                d_rec["long_book_net_pnl"] = round(d_rec["long_book_net_pnl"] + net, 2)
                d_rec["long_book_trades"] += 1
            else:
                d_rec["short_book_net_pnl"] = round(d_rec["short_book_net_pnl"] + net, 2)
                d_rec["short_book_trades"] += 1
            d_rec["trades"].append(t)

        days_list: List[Dict[str, Any]] = []
        months_summary: Dict[str, Dict[str, Any]] = {}
        for m_str in sorted(months_set, reverse=True):
            months_summary[m_str] = {
                "month": m_str,
                "total_net_pnl": 0.0,
                "long_book_net_pnl": 0.0,
                "short_book_net_pnl": 0.0,
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "green_days": 0,
                "red_days": 0,
                "trading_days": 0,
                "_gross_wins": 0.0,
                "_gross_losses": 0.0,
            }

        for day_str in sorted(days_map.keys()):
            d_rec = days_map[day_str]
            tc = d_rec["trade_count"]
            d_rec["win_rate"] = round((d_rec["wins"] / tc) * 100.0, 1) if tc > 0 else 0.0
            days_list.append(d_rec)

            m_str = d_rec["month"]
            ms = months_summary[m_str]
            ms["total_net_pnl"] = round(ms["total_net_pnl"] + d_rec["net_pnl_rupees"], 2)
            ms["long_book_net_pnl"] = round(ms["long_book_net_pnl"] + d_rec["long_book_net_pnl"], 2)
            ms["short_book_net_pnl"] = round(ms["short_book_net_pnl"] + d_rec["short_book_net_pnl"], 2)
            ms["total_trades"] += tc
            ms["wins"] += d_rec["wins"]
            ms["losses"] += d_rec["losses"]
            ms["trading_days"] += 1
            if d_rec["net_pnl_rupees"] >= 0:
                ms["green_days"] += 1
            else:
                ms["red_days"] += 1
            for tr in d_rec["trades"]:
                n_val = float(tr.get("net_pnl") or 0.0)
                if n_val > 0:
                    ms["_gross_wins"] += n_val
                else:
                    ms["_gross_losses"] += abs(n_val)

        for ms in months_summary.values():
            mtc = ms["total_trades"]
            ms["win_rate"] = round((ms["wins"] / mtc) * 100.0, 1) if mtc > 0 else 0.0
            gw = ms.pop("_gross_wins", 0.0)
            gl = ms.pop("_gross_losses", 0.0)
            ms["profit_factor"] = round(gw / gl, 2) if gl > 0 else (round(gw, 2) if gw > 0 else 0.0)

        return {
            "book_filter": flt,
            "available_months": sorted(months_set, reverse=True),
            "default_month": cur_month,
            "months_summary": months_summary,
            "days": days_list,
        }


if _PREV_BRIDGE_INSTANCE is not None:
    _PREV_BRIDGE_INSTANCE.__class__ = AdvanceLiveBridge
    if not hasattr(_PREV_BRIDGE_INSTANCE, "_last_broker_reconcile_mono"):
        _PREV_BRIDGE_INSTANCE._last_broker_reconcile_mono = -999999.0
    if not hasattr(_PREV_BRIDGE_INSTANCE, "_last_broker_reconcile_sim_dt"):
        _PREV_BRIDGE_INSTANCE._last_broker_reconcile_sim_dt = None
    if not hasattr(_PREV_BRIDGE_INSTANCE, "_reconciled_once_intent_ids"):
        _PREV_BRIDGE_INSTANCE._reconciled_once_intent_ids = set()
    if not hasattr(_PREV_BRIDGE_INSTANCE, "_last_priority_quote_refresh_ts"):
        _PREV_BRIDGE_INSTANCE._last_priority_quote_refresh_ts = -999999.0
    _BRIDGE_INSTANCE = _PREV_BRIDGE_INSTANCE


def get_advance_live_bridge() -> AdvanceLiveBridge:
    global _BRIDGE_INSTANCE
    with _BRIDGE_LOCK:
        if _BRIDGE_INSTANCE is None:
            _BRIDGE_INSTANCE = AdvanceLiveBridge()
        elif _BRIDGE_INSTANCE.__class__ is not AdvanceLiveBridge:
            _BRIDGE_INSTANCE.__class__ = AdvanceLiveBridge
        return _BRIDGE_INSTANCE
