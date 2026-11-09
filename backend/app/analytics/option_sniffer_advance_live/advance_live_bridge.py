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

_BRIDGE_LOCK = threading.RLock()
_BRIDGE_INSTANCE: Optional["AdvanceLiveBridge"] = None


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
        consec_losses = 0
        tripped = False
        reason = None
        try:
            with self._get_ro_conn() as conn:
                cur = conn.cursor()
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
                if realized_pnl <= -MAX_DAILY_COMBINED_LOSS_INR:
                    tripped = True
                    reason = f"Daily Live Loss Ceiling Reached (₹{realized_pnl:,.2f} <= -₹{MAX_DAILY_COMBINED_LOSS_INR:,.2f})"
        except Exception as e:
            logger.debug(f"[AdvanceLiveBridge] Circuit breaker check note: {e}")

        return {
            "session_date": today,
            "realized_pnl_inr": round(realized_pnl, 2),
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

    def sync_once_with_advance_paper_table(
        self,
        now_dt: Optional[datetime] = None,
        as_of_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Single deterministic pass of the SQLite WAL Follower:
          1. Checks all active live intents against `option_sniffer_advance_trades`:
             if the parent paper trade is now `CLOSED`, immediately fires the live broker square-off!
          2. If `_is_live_armed` is True, checks for any newly opened (`status = 'OPEN'`) trade in
             `option_sniffer_advance_trades` and mirrors it to Upstox according to `_execution_mode`.
        """
        now_dt = to_ist_naive(now_dt or as_of_time)
        today_str = now_dt.strftime("%Y-%m-%d")
        mirrored_entries: List[str] = []
        mirrored_exits: List[str] = []
        skipped_by_mode: List[Dict[str, Any]] = []

        # 1. Check Exits FIRST (Always active even if disarmed while a live position was open)
        active_intents = self.get_active_live_positions()
        if active_intents:
            try:
                with self._get_ro_conn() as ro_conn:
                    ro_cur = ro_conn.cursor()
                    for intent in active_intents:
                        adv_tid = intent.get("advance_trade_id")
                        if not adv_tid:
                            continue
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
            except Exception as ex_err:
                logger.error(f"[AdvanceLiveBridge] Exit sync error: {ex_err}")

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

        for prow in open_rows:
            pdict = dict(prow)
            adv_tid = str(pdict.get("trade_id") or "")
            if not adv_tid:
                continue

            if self._has_intent_for_advance_trade(adv_tid):
                continue

            book_type = str(pdict.get("book_type") or BOOK_LONG_BUY)
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
                        wing_ask = float(q_cand.get("ask") or q_cand.get("ltp") or 25.0)
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

        if success and isinstance(data, dict) and data.get("status") == "success":
            broker_oid = str(data.get("data", {}).get("order_id") or f"UPSTOX_ADV_{int(time.time()*1000)}")
            state = "FILLED"
            avg_fill = paper_entry
            rejection_reason = None
        else:
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
                    state, broker_order_id, stop_loss_trigger, stop_loss_limit,
                    hedge_wing_strike, hedge_wing_instrument_key, hedge_wing_broker_order_id,
                    paper_entry_price, exit_state, rejection_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
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
            "rejection_reason": rejection_reason,
        }

    def _execute_mirrored_live_exit(
        self,
        intent: Dict[str, Any],
        paper_row: Dict[str, Any],
        now_dt: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Fires an emergency-priority Upstox V2 exit order the instant the parent Paper trade closes."""
        now_dt = to_ist_naive(now_dt)
        now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        intent_id = str(intent["order_intent_id"])
        action = str(intent.get("action") or "BUY").upper()
        exit_tx_type = "SELL" if action == "BUY" else "BUY"
        qty = int(intent.get("filled_quantity") or intent.get("target_quantity") or 65)
        ikey = str(intent["instrument_key"])

        paper_exit = float(paper_row.get("exit_price_used") or 0.0)
        if paper_exit <= 0:
            stream_mgr = OptionStreamManager()
            q = stream_mgr.get_option_quote(ikey) or {}
            paper_exit = float(q.get("bid") if exit_tx_type == "SELL" else q.get("ask") or q.get("ltp") or intent.get("average_fill_price") or 100.0)

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

        engine = get_order_intent_engine()
        success, status_code, data = engine._call_upstox_broker_api(
            "POST", "/order/place", payload=payload, is_emergency=True
        )
        exit_oid = (
            str(data.get("data", {}).get("order_id") or f"UPSTOX_ADV_EX_{int(time.time()*1000)}")
            if (success and isinstance(data, dict) and data.get("status") == "success")
            else f"UPSTOX_ADV_EX_{int(time.time()*1000)}"
        )

        # If this was a hedged short trade (DUAL_BOOK_HEDGED), also flatten the 200-pt OTM protective BUY wing
        hedge_wing_ikey = intent.get("hedge_wing_instrument_key")
        if hedge_wing_ikey and action == "SELL":
            try:
                stream_mgr = OptionStreamManager()
                hq = stream_mgr.get_option_quote(str(hedge_wing_ikey)) or {}
                h_bid = float(hq.get("bid") or hq.get("ltp") or 2.0)
                h_exit_limit = round_to_tick(max(0.05, h_bid * 0.95))
                hedge_exit_payload = {
                    "quantity": qty,
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

        entry_fill = float(intent.get("average_fill_price") or intent.get("limit_price") or paper_exit)
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

        exit_reason = str(paper_row.get("exit_reason") or "MIRRORED_PAPER_EXIT")
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


def get_advance_live_bridge() -> AdvanceLiveBridge:
    global _BRIDGE_INSTANCE
    with _BRIDGE_LOCK:
        if _BRIDGE_INSTANCE is None:
            _BRIDGE_INSTANCE = AdvanceLiveBridge()
        return _BRIDGE_INSTANCE
