"""
Layer 5 — Funded-Cash Arena Execution & Human-Interlocked Upstox Live Bridge
============================================================================
Enforces all institutional execution invariants:
  1. Hard-locked `SIMULATION_MODE = True` (`_is_live_armed = False`) on every boot/reload (`AGENTS.md` Rule 2).
  2. Sole human operator arm/disarm authority via 4-digit confirmation PIN (`AGENTS.md` Rule 6).
  3. Whole shares only (`int(shares) >= 1`), `0x` leverage, strict **pending-order cash reservation**:
       `invested_notional_inr + pending_reserved_cash_inr <= current_equity_inr` at all times!
  4. Post-Only Passive Limit (`15m` TIF) fill verification requiring strict post-order price trade-through,
     releasing `pending_reserved_cash_inr` immediately upon expiry or cancellation.
  5. Mandatory `15:15:00 IST` NSE `MIS` square-off, structural stop monitoring, `-1.25%` daily loss circuit breaker,
     and 1-click `SOS Panic Exit`.
"""

import json
import logging
import os
import random
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.analytics.consensus_v3 import ENGINE_VERSION, PROTOCOL_ID, resolve_v3_db_path as get_db_path
from app.analytics.consensus_v3.math_and_costs import (
    compute_nse_cnc_charges,
    compute_nse_mis_charges,
    compute_whole_shares,
    round_nse_eq_tick,
)

logger = logging.getLogger(__name__)

MODE_DUAL_FUNDED_ARENA = "DUAL_FUNDED_CASH_ARENA"
MODE_BOOK1_SWING_CNC = "BOOK1_SWING_CNC_ONLY"
MODE_BOOK2_INTRADAY_MIS = "BOOK2_INTRADAY_MIS_SNIFFER"
VALID_ARENA_MODES = (
    MODE_DUAL_FUNDED_ARENA,
    MODE_BOOK1_SWING_CNC,
    MODE_BOOK2_INTRADAY_MIS,
)

DEFAULT_FUNDED_CAPITAL_INR = 400000.0
DAILY_LOSS_HALT_FRACTION = 0.0125  # -1.25% daily realized loss halt (-₹5,000 on ₹4,00,000)

_ARENA_BRIDGE_LOCK = threading.RLock()
_ARENA_BRIDGE_INSTANCE: Optional["ConsensusArenaExecutionBridge"] = None


def init_consensus_v3_tables(db_path: Optional[str] = None) -> None:
    target_db = db_path or get_db_path()
    with _ARENA_BRIDGE_LOCK:
        with sqlite3.connect(target_db, timeout=12.0) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS consensus_v3_arena_state (
                    session_id TEXT PRIMARY KEY,
                    funded_capital_inr REAL NOT NULL DEFAULT 400000.0,
                    realized_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    peak_equity_inr REAL NOT NULL DEFAULT 400000.0,
                    max_drawdown_pct REAL NOT NULL DEFAULT 0.0,
                    execution_mode TEXT NOT NULL DEFAULT 'DUAL_FUNDED_CASH_ARENA',
                    book1_max_slots INTEGER NOT NULL DEFAULT 8,
                    book2_max_slots INTEGER NOT NULL DEFAULT 4,
                    last_book1_rebalance_date TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS consensus_v3_pending_orders (
                    order_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    book TEXT NOT NULL,
                    product_type TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    instrument_key TEXT,
                    direction TEXT NOT NULL,
                    shares INTEGER NOT NULL,
                    signal_close_px REAL NOT NULL,
                    limit_price REAL NOT NULL,
                    reserved_cash_inr REAL NOT NULL,
                    stop_loss_px REAL NOT NULL,
                    scheduled_exit_hm TEXT,
                    status TEXT NOT NULL DEFAULT 'PENDING_RESERVED',
                    execution_env TEXT NOT NULL DEFAULT 'PAPER_SHADOW',
                    broker_order_id TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution_note TEXT,
                    details_json TEXT
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS consensus_v3_positions (
                    position_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    order_id TEXT,
                    book TEXT NOT NULL,
                    product_type TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    instrument_key TEXT,
                    direction TEXT NOT NULL,
                    shares INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    invested_notional_inr REAL NOT NULL,
                    stop_loss_px REAL NOT NULL,
                    scheduled_exit TEXT,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    execution_env TEXT NOT NULL DEFAULT 'PAPER_SHADOW',
                    current_price REAL NOT NULL,
                    unrealized_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    entry_time TEXT NOT NULL,
                    exit_price REAL,
                    exit_time TEXT,
                    exit_reason TEXT,
                    gross_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    fees_inr REAL NOT NULL DEFAULT 0.0,
                    net_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    details_json TEXT
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS consensus_v3_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    book TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    abstain_reason TEXT,
                    limit_or_ref_px REAL NOT NULL,
                    stop_loss_px REAL,
                    e_qlib_net_bps REAL,
                    e_legacy_net_bps REAL,
                    disagreement_bps REAL,
                    edge_penalized_bps REAL,
                    legacy_win_prob REAL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS consensus_v3_live_intents (
                    intent_id TEXT PRIMARY KEY,
                    order_or_pos_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    product_type TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    shares INTEGER NOT NULL,
                    price REAL NOT NULL,
                    state TEXT NOT NULL,
                    broker_order_id TEXT,
                    rejection_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS consensus_v3_risk_ledger (
                    session_date TEXT PRIMARY KEY,
                    realized_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    fills_today INTEGER NOT NULL DEFAULT 0,
                    circuit_breaker_tripped INTEGER NOT NULL DEFAULT 0,
                    trip_reason TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cv3_pending_status ON consensus_v3_pending_orders(session_id, status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cv3_pos_status ON consensus_v3_positions(session_id, status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cv3_signals_time ON consensus_v3_signals(evaluated_at DESC);")
            conn.commit()


class ConsensusArenaExecutionBridge:
    """
    Stateful Funded-Cash Execution Engine + Isolated Human-Interlocked Upstox Live Bridge.
    Boots strictly DISARMED (`simulation_mode = True`) on every process start!
    """

    def __init__(self, db_path: Optional[str] = None, session_id: str = "default_v3_arena"):
        self.db_path = db_path or get_db_path()
        self.session_id = session_id
        self._lock = threading.RLock()
        init_consensus_v3_tables(self.db_path)

        # Hard-locked paper safety defaults (AGENTS.md Rules 2 & 6)
        self._is_live_armed: bool = False
        self._armed_for_date: str = ""
        self._armed_at_iso: str = ""
        self._current_pin: str = f"{random.randint(1000, 9999)}"
        self._ensure_arena_state()

    @property
    def simulation_mode(self) -> bool:
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self._is_live_armed and self._armed_for_date != today_str:
            # Auto-disarm across overnight date rollovers!
            self._is_live_armed = False
            self._armed_for_date = ""
        return not self._is_live_armed

    @property
    def current_pin(self) -> str:
        return self._current_pin

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=12.0)
        c.row_factory = sqlite3.Row
        return c

    def _ensure_arena_state(self) -> None:
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT session_id FROM consensus_v3_arena_state WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
            if not row:
                conn.execute(
                    """
                    INSERT INTO consensus_v3_arena_state (
                        session_id, funded_capital_inr, realized_pnl_inr, peak_equity_inr,
                        max_drawdown_pct, execution_mode, book1_max_slots, book2_max_slots, updated_at
                    ) VALUES (?, ?, 0.0, ?, 0.0, ?, 8, 4, ?)
                    """,
                    (self.session_id, DEFAULT_FUNDED_CAPITAL_INR, DEFAULT_FUNDED_CAPITAL_INR, MODE_DUAL_FUNDED_ARENA, now_s),
                )
                conn.commit()

    def configure_arena_capital(
        self,
        funded_capital_inr: float,
        execution_mode: str = MODE_DUAL_FUNDED_ARENA,
        reset_ledger: bool = False,
    ) -> Dict[str, Any]:
        cap = max(25000.0, float(funded_capital_inr))
        mode = str(execution_mode or MODE_DUAL_FUNDED_ARENA).upper().strip()
        if mode not in VALID_ARENA_MODES:
            return {"status": "ERROR", "message": f"Invalid mode '{mode}'. Choose from {VALID_ARENA_MODES}."}
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._conn() as conn:
            if reset_ledger and self.simulation_mode:
                conn.execute("DELETE FROM consensus_v3_pending_orders WHERE session_id = ?", (self.session_id,))
                conn.execute("DELETE FROM consensus_v3_positions WHERE session_id = ?", (self.session_id,))
                conn.execute(
                    """
                    UPDATE consensus_v3_arena_state
                    SET funded_capital_inr = ?, realized_pnl_inr = 0.0, peak_equity_inr = ?,
                        max_drawdown_pct = 0.0, execution_mode = ?, last_book1_rebalance_date = NULL, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (cap, cap, mode, now_s, self.session_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE consensus_v3_arena_state
                    SET funded_capital_inr = ?, peak_equity_inr = MAX(peak_equity_inr, ? + realized_pnl_inr),
                        execution_mode = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (cap, cap, mode, now_s, self.session_id),
                )
            conn.commit()
        return self.get_arena_accounting()

    def set_execution_mode(self, mode: str) -> Dict[str, Any]:
        clean = str(mode or "").upper().strip()
        if clean not in VALID_ARENA_MODES:
            return {"status": "ERROR", "message": f"Invalid execution_mode '{clean}'. Must be in {VALID_ARENA_MODES}."}
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._conn() as conn:
            st = conn.execute(
                "SELECT funded_capital_inr FROM consensus_v3_arena_state WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
            cap = float(st["funded_capital_inr"]) if st else DEFAULT_FUNDED_CAPITAL_INR
            conn.execute(
                "UPDATE consensus_v3_arena_state SET execution_mode = ?, updated_at = ? WHERE session_id = ?",
                (clean, now_s, self.session_id),
            )
            conn.commit()
        return {
            "status": "UPDATED",
            "execution_mode": clean,
            "required_margin_for_mode": cap,
            "message": f"Execution mode updated to {clean}.",
        }

    def get_arena_accounting(self, now_dt: Optional[datetime] = None) -> Dict[str, Any]:
        ts = now_dt or datetime.now()
        today_s = ts.strftime("%Y-%m-%d")
        with self._conn() as conn:
            st = conn.execute(
                "SELECT * FROM consensus_v3_arena_state WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
            funded = float(st["funded_capital_inr"])
            realized = float(st["realized_pnl_inr"])
            mode = str(st["execution_mode"])
            b1_max = int(st["book1_max_slots"])
            b2_max = int(st["book2_max_slots"])

            open_rows = conn.execute(
                "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND status = 'OPEN' ORDER BY entry_time DESC",
                (self.session_id,),
            ).fetchall()
            pend_rows = conn.execute(
                "SELECT * FROM consensus_v3_pending_orders WHERE session_id = ? AND status = 'PENDING_RESERVED' ORDER BY created_at DESC",
                (self.session_id,),
            ).fetchall()
            risk_row = conn.execute(
                "SELECT * FROM consensus_v3_risk_ledger WHERE session_date = ?",
                (today_s,),
            ).fetchone()

        invested_inr = sum(float(r["invested_notional_inr"]) for r in open_rows)
        unrealized_inr = sum(float(r["unrealized_pnl_inr"]) for r in open_rows)
        pending_reserved_inr = sum(float(r["reserved_cash_inr"]) for r in pend_rows)
        current_equity_inr = funded + realized + unrealized_inr
        free_unreserved_cash_inr = max(0.0, current_equity_inr - invested_inr - pending_reserved_inr)

        b1_open = sum(1 for r in open_rows if r["book"] == "BOOK1_SWING_CNC_CORE")
        b2_open = sum(1 for r in open_rows if r["book"] == "BOOK2_INTRADAY_MIS_SNIFFER")
        b2_pend = sum(1 for r in pend_rows if r["book"] == "BOOK2_INTRADAY_MIS_SNIFFER")

        max_daily_loss_inr = -round(funded * DAILY_LOSS_HALT_FRACTION, 2)
        today_realized = float(risk_row["realized_pnl_inr"]) if risk_row else 0.0
        fills_today = int(risk_row["fills_today"]) if risk_row else 0
        cb_tripped = bool(risk_row["circuit_breaker_tripped"]) if risk_row else (today_realized <= max_daily_loss_inr)

        return {
            "session_id": self.session_id,
            "simulation_mode": self.simulation_mode,
            "is_live_armed": not self.simulation_mode,
            "execution_mode": mode,
            "funded_capital_inr": round(funded, 2),
            "current_equity_inr": round(current_equity_inr, 2),
            "realized_pnl_inr": round(realized, 2),
            "unrealized_pnl_inr": round(unrealized_inr, 2),
            "invested_notional_inr": round(invested_inr, 2),
            "pending_reserved_cash_inr": round(pending_reserved_inr, 2),
            "free_unreserved_cash_inr": round(free_unreserved_cash_inr, 2),
            "cash_conservation_invariant_ok": (invested_inr + pending_reserved_inr) <= (current_equity_inr + 1.0),
            "book1_slots": {"open": b1_open, "max": b1_max, "slot_cap_inr": round(funded / max(1, b1_max), 2)},
            "book2_slots": {"open": b2_open, "pending": b2_pend, "max": b2_max, "slot_cap_inr": round(funded / max(1, b2_max), 2)},
            "circuit_breaker": {
                "session_date": today_s,
                "today_realized_pnl_inr": round(today_realized, 2),
                "max_daily_loss_inr": max_daily_loss_inr,
                "fills_today": fills_today,
                "max_fills_per_day": 6,
                "is_session_halted": cb_tripped,
                "trip_reason": risk_row["trip_reason"] if (risk_row and risk_row["trip_reason"]) else None,
            },
            "max_drawdown_pct": round(float(st["max_drawdown_pct"]), 2),
            "last_book1_rebalance_date": st["last_book1_rebalance_date"],
        }

    def get_live_status(self) -> Dict[str, Any]:
        """Provides `ArmInterlockModal`-compatible status payload mirroring `OptionSnifferAdvance`."""
        acct = self.get_arena_accounting()
        avail_margin = None
        token_valid = False
        ws_connected = None
        blockers: List[str] = []

        try:
            from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
            adapter = get_option_live_adapter()
            rd = adapter.get_readiness_status()
            token_valid = bool(rd.get("token_valid"))
            avail_margin = rd.get("available_margin")
            if not token_valid:
                blockers.append("UPSTOX_ACCESS_TOKEN_INVALID_OR_EXPIRED")
        except Exception:
            blockers.append("UPSTOX_ADAPTER_UNAVAILABLE")

        try:
            from app.data.upstox_stream_service import get_stream_service
            ws_connected = bool(get_stream_service().get_status().get("connected"))
        except Exception:
            ws_connected = None

        req_cash = acct["funded_capital_inr"]
        margin_ok = isinstance(avail_margin, (int, float)) and float(avail_margin) >= req_cash
        if not margin_ok:
            blockers.append(f"INSUFFICIENT_BROKER_CASH_FOR_FUNDED_ARENA (Need ₹{req_cash:,.0f})")
        if acct["circuit_breaker"]["is_session_halted"]:
            blockers.append("DAILY_LOSS_CIRCUIT_BREAKER_TRIPPED")

        with self._conn() as conn:
            live_open = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND status = 'OPEN' AND execution_env = 'LIVE_UPSTOX'",
                    (self.session_id,),
                ).fetchall()
            ]

        return {
            "engine_version": ENGINE_VERSION,
            "protocol_id": PROTOCOL_ID,
            "simulation_mode": self.simulation_mode,
            "is_armed": not self.simulation_mode,
            "armed_at": self._armed_at_iso or None,
            "execution_mode": acct["execution_mode"],
            "current_pin": self._current_pin,
            "available_margin": avail_margin,
            "required_margin_for_mode": req_cash,
            "mode_margin_ok": margin_ok,
            "active_live_positions": live_open,
            "circuit_breaker": acct["circuit_breaker"],
            "accounting": acct,
            "stream_health": {"websocket_connected": ws_connected},
            "readiness": {
                "token_valid": token_valid,
                "live_trading_eligible": len(blockers) == 0,
                "blockers": blockers,
            },
        }

    def arm_live_trading(self, pin: str, execution_mode: Optional[str] = None) -> Dict[str, Any]:
        """
        HUMAN-EXCLUSIVE INTERLOCK (`AGENTS.md` Rule 6):
        Never called by automated daemons, scripts, or AI agents.
        """
        with self._lock:
            if execution_mode:
                m_res = self.set_execution_mode(execution_mode)
                if m_res.get("status") == "ERROR":
                    return m_res

            clean_pin = str(pin or "").strip()
            if not clean_pin or clean_pin != self._current_pin:
                return {"status": "REJECTED", "message": "Invalid 4-digit human confirmation PIN."}

            st = self.get_live_status()
            if not st["readiness"]["live_trading_eligible"]:
                return {
                    "status": "BLOCKED",
                    "message": f"Live readiness blockers active: {', '.join(st['readiness']['blockers'])}",
                    "blockers": st["readiness"]["blockers"],
                }

            now_dt = datetime.now()
            self._is_live_armed = True
            self._armed_for_date = now_dt.strftime("%Y-%m-%d")
            self._armed_at_iso = now_dt.isoformat()
            self._current_pin = f"{random.randint(1000, 9999)}"
            logger.warning(f"[ConsensusV3LiveBridge] HUMAN ARMED LIVE TRADING ({st['execution_mode']}) at {self._armed_at_iso}")
            return {
                "status": "ARMED",
                "simulation_mode": False,
                "execution_mode": st["execution_mode"],
                "armed_at": self._armed_at_iso,
                "message": f"Consensus V3 Live Bridge ARMED ({st['execution_mode']}).",
            }

    def disarm_live_trading(self) -> Dict[str, Any]:
        with self._lock:
            self._is_live_armed = False
            self._armed_for_date = ""
            self._armed_at_iso = ""
            self._current_pin = f"{random.randint(1000, 9999)}"
            return {
                "status": "DISARMED",
                "simulation_mode": True,
                "message": "Disarmed to 100% Paper Shadow Mode (`simulation_mode = true`).",
            }

    def _route_upstox_equity_order(
        self,
        order_or_pos_id: str,
        action: str,
        product_type: str,
        order_type: str,
        ticker: str,
        instrument_key: str,
        shares: int,
        price: float,
        is_emergency: bool = False,
    ) -> Dict[str, Any]:
        """Routes an idempotent cash-equity order to Upstox ONLY when human-armed (`not self.simulation_mode`)."""
        if self.simulation_mode and not is_emergency:
            return {"routed": False, "state": "PAPER_SHADOW_ONLY", "broker_order_id": None}

        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        intent_id = f"cv3_{action.lower()}_{order_or_pos_id}"
        if not instrument_key or not instrument_key.startswith("NSE_EQ|"):
            return {"routed": False, "state": "MISSING_NSE_EQ_KEY", "broker_order_id": None}

        payload = {
            "quantity": int(shares),
            "product": "I" if product_type == "MIS" else "D",
            "validity": "DAY",
            "price": round_nse_eq_tick(price) if order_type == "LIMIT" else 0.0,
            "tag": f"cv3_{order_or_pos_id[:12]}",
            "instrument_token": instrument_key,
            "order_type": order_type,
            "transaction_type": action.upper(),
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
        }

        state = "SUBMITTED"
        broker_oid = None
        rej = None
        try:
            from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
            adapter = get_option_live_adapter()
            resp = adapter._upstox_http("POST", "/order/place", payload=payload, is_emergency=is_emergency)
            broker_oid = str((resp or {}).get("data", {}).get("order_id") or "")
            if not broker_oid:
                state = "REJECTED"
                rej = str(resp)[:240]
        except Exception as err:
            state = "REJECTED"
            rej = str(err)[:240]

        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO consensus_v3_live_intents (
                    intent_id, order_or_pos_id, action, product_type, order_type, ticker,
                    instrument_key, shares, price, state, broker_order_id, rejection_reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id, order_or_pos_id, action.upper(), product_type, order_type, ticker,
                    instrument_key, int(shares), float(price), state, broker_oid, rej, now_s, now_s,
                ),
            )
            conn.commit()
        return {"routed": state == "SUBMITTED", "state": state, "broker_order_id": broker_oid, "error": rej}

    def reserve_book2_pending_limit_orders(
        self,
        approved_signals: List[Dict[str, Any]],
        now_dt: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Places 15-minute Post-Only Limit Reservations in `consensus_v3_pending_orders`:
          - Immediately locks `shares * limit_entry_px` in `pending_reserved_cash_inr`.
          - Enforces `invested + pending_reserved <= current_equity` (`0x` leverage!).
          - Never places duplicate orders for the same ticker on the same day.
        """
        ts = now_dt or datetime.now()
        today_s = ts.strftime("%Y-%m-%d")
        now_s = ts.strftime("%Y-%m-%d %H:%M:%S")
        exp_s = (ts + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")

        created_orders: List[Dict[str, Any]] = []
        with self._lock:
            acct = self.get_arena_accounting(now_dt=ts)
            if acct["execution_mode"] == MODE_BOOK1_SWING_CNC:
                return []
            if acct["circuit_breaker"]["is_session_halted"]:
                return []
            if acct["circuit_breaker"]["fills_today"] >= 6:
                return []

            avail_b2_slots = acct["book2_slots"]["max"] - (acct["book2_slots"]["open"] + acct["book2_slots"]["pending"])
            if avail_b2_slots <= 0:
                return []

            free_cash = acct["free_unreserved_cash_inr"]
            slot_cap = acct["book2_slots"]["slot_cap_inr"]
            exec_env = "PAPER_SHADOW" if self.simulation_mode else "LIVE_UPSTOX"

            with self._conn() as conn:
                already_today = {
                    r[0]
                    for r in conn.execute(
                        """
                        SELECT ticker FROM consensus_v3_pending_orders WHERE session_id = ? AND substr(created_at,1,10) = ?
                        UNION
                        SELECT ticker FROM consensus_v3_positions WHERE session_id = ? AND substr(entry_time,1,10) = ?
                        """,
                        (self.session_id, today_s, self.session_id, today_s),
                    ).fetchall()
                }

                for sig in approved_signals:
                    if avail_b2_slots <= 0:
                        break
                    ticker = sig["ticker"]
                    if ticker in already_today:
                        continue

                    lim_px = round_nse_eq_tick(float(sig["limit_entry_px"]))
                    shares = compute_whole_shares(
                        allocatable_cash_inr=free_cash,
                        price_inr=lim_px,
                        max_slot_inr=slot_cap,
                        liquidity_cap_inr=float(sig.get("liq_1pct_bar_inr", slot_cap)),
                    )
                    if shares < 1:
                        continue

                    reserved_inr = round(shares * lim_px, 2)
                    oid = f"ord_b2_{today_s.replace('-', '')}_{uuid.uuid4().hex[:8]}"
                    broker_oid = None

                    if exec_env == "LIVE_UPSTOX":
                        route_res = self._route_upstox_equity_order(
                            order_or_pos_id=oid,
                            action=sig["direction"],
                            product_type="MIS",
                            order_type="LIMIT",
                            ticker=ticker,
                            instrument_key=sig.get("instrument_key", ""),
                            shares=shares,
                            price=lim_px,
                        )
                        if not route_res["routed"]:
                            continue
                        broker_oid = route_res["broker_order_id"]

                    conn.execute(
                        """
                        INSERT INTO consensus_v3_pending_orders (
                            order_id, session_id, book, product_type, ticker, symbol, instrument_key,
                            direction, shares, signal_close_px, limit_price, reserved_cash_inr,
                            stop_loss_px, scheduled_exit_hm, status, execution_env, broker_order_id,
                            created_at, expires_at, details_json
                        ) VALUES (?, ?, ?, 'MIS', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_RESERVED', ?, ?, ?, ?, ?)
                        """,
                        (
                            oid, self.session_id, sig["book"], ticker, sig["symbol"], sig.get("instrument_key", ""),
                            sig["direction"], shares, float(sig["signal_close_px"]), lim_px, reserved_inr,
                            float(sig["stop_loss_px"]), sig.get("scheduled_exit_hm", "15:15"),
                            exec_env, broker_oid, now_s, exp_s, json.dumps(sig),
                        ),
                    )
                    free_cash -= reserved_inr
                    avail_b2_slots -= 1
                    already_today.add(ticker)
                    created_orders.append({
                        "order_id": oid,
                        "ticker": ticker,
                        "direction": sig["direction"],
                        "shares": shares,
                        "limit_price": lim_px,
                        "reserved_cash_inr": reserved_inr,
                        "expires_at": exp_s,
                        "execution_env": exec_env,
                    })
                conn.commit()
        return created_orders

    def reconcile_orders_and_positions(
        self,
        live_quotes: Optional[Dict[str, Dict[str, Any]]] = None,
        now_dt: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Causal 1-Minute Reconciler:
          1. Evaluates `PENDING_RESERVED` limit orders:
             - Fills if post-creation price trades strictly through `limit_price` (`>= 3 bps`), moving to `OPEN` position.
             - Expires after `expires_at` (`15m` TIF) or past `14:55 IST`, immediately releasing `reserved_cash_inr`!
          2. Evaluates `OPEN` positions:
             - Updates live MTM (`unrealized_pnl_inr`) & high-water mark drawdown.
             - Triggers structural `STOP_HIT` or mandatory `15:15:00 IST` NSE `MIS` square-off!
             - Updates `consensus_v3_risk_ledger` and trips `-1.25%` daily loss circuit breaker if breached.
        """
        ts = now_dt or datetime.now()
        now_s = ts.strftime("%Y-%m-%d %H:%M:%S")
        hm_now = ts.strftime("%H:%M")
        today_s = ts.strftime("%Y-%m-%d")

        filled_cnt, expired_cnt, closed_cnt = 0, 0, 0
        with self._lock, self._conn() as conn:
            pend = conn.execute(
                "SELECT * FROM consensus_v3_pending_orders WHERE session_id = ? AND status = 'PENDING_RESERVED'",
                (self.session_id,),
            ).fetchall()
            open_pos = conn.execute(
                "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND status = 'OPEN'",
                (self.session_id,),
            ).fetchall()

            tickers_needed = list({r["ticker"] for r in pend} | {r["ticker"] for r in open_pos})
            quotes = live_quotes
            if quotes is None and tickers_needed:
                try:
                    from app.data.market_provider import get_bulk_live_quotes_with_meta
                    quotes = get_bulk_live_quotes_with_meta(tickers_needed)
                except Exception:
                    quotes = {}
            quotes = quotes or {}

            def _latest_px_and_bar(sym: str, after_ts: str):
                q = quotes.get(sym) or quotes.get(sym.replace(".NS", ""))
                ltp = float(q["price"]) if (q and isinstance(q.get("price"), (int, float)) and q["price"] > 0) else None
                brow = conn.execute(
                    "SELECT open, high, low, close FROM ohlcv WHERE ticker = ? AND timeframe = '15m' AND date > ? AND date <= ? ORDER BY date DESC LIMIT 1",
                    (sym, after_ts, now_s),
                ).fetchone()
                return ltp, brow

            # 1. Reconcile Pending Limit Reservations
            for pr in pend:
                oid = pr["order_id"]
                sym = pr["ticker"]
                lim = float(pr["limit_price"])
                sdir = pr["direction"]
                ltp, brow = _latest_px_and_bar(sym, pr["created_at"])

                is_filled = False
                post_only_violated = False
                if brow is not None:
                    bo, bh, bl, bc = float(brow[0]), float(brow[1]), float(brow[2]), float(brow[3])
                    if sdir == "SELL":
                        if bo >= lim:
                            post_only_violated = True
                        elif bh >= lim * 1.0003:
                            is_filled = True
                    else:
                        if bo <= lim:
                            post_only_violated = True
                        elif bl <= lim * 0.9997:
                            is_filled = True
                if not post_only_violated and not is_filled and ltp is not None:
                    if sdir == "SELL" and ltp >= lim * 1.0003:
                        is_filled = True
                    elif sdir == "BUY" and ltp <= lim * 0.9997:
                        is_filled = True

                if post_only_violated:
                    conn.execute(
                        "UPDATE consensus_v3_pending_orders SET status = 'CANCELLED_POSTONLY_GAP', resolved_at = ?, resolution_note = 'Next bar gapped past post-only limit' WHERE order_id = ?",
                        (now_s, oid),
                    )
                    expired_cnt += 1
                    continue

                if is_filled:
                    pid = f"pos_{oid[4:]}"
                    sh = int(pr["shares"])
                    notional = round(sh * lim, 2)
                    conn.execute(
                        "UPDATE consensus_v3_pending_orders SET status = 'FILLED', resolved_at = ?, resolution_note = 'Limit trade-through verified' WHERE order_id = ?",
                        (now_s, oid),
                    )
                    conn.execute(
                        """
                        INSERT INTO consensus_v3_positions (
                            position_id, session_id, order_id, book, product_type, ticker, symbol,
                            instrument_key, direction, shares, entry_price, invested_notional_inr,
                            stop_loss_px, scheduled_exit, status, execution_env, current_price,
                            unrealized_pnl_inr, entry_time, details_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, 0.0, ?, ?)
                        """,
                        (
                            pid, self.session_id, oid, pr["book"], pr["product_type"], sym, pr["symbol"],
                            pr["instrument_key"], sdir, sh, lim, notional, float(pr["stop_loss_px"]),
                            pr["scheduled_exit_hm"] or "15:15", pr["execution_env"], lim, now_s, pr["details_json"],
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO consensus_v3_risk_ledger (session_date, realized_pnl_inr, fills_today, circuit_breaker_tripped, updated_at)
                        VALUES (?, 0.0, 1, 0, ?)
                        ON CONFLICT(session_date) DO UPDATE SET fills_today = fills_today + 1, updated_at = excluded.updated_at
                        """,
                        (today_s, now_s),
                    )
                    filled_cnt += 1
                    continue

                if now_s >= str(pr["expires_at"]) or (pr["product_type"] == "MIS" and hm_now >= "14:55"):
                    conn.execute(
                        "UPDATE consensus_v3_pending_orders SET status = 'EXPIRED_UNFILLED', resolved_at = ?, resolution_note = '15m TIF expired; reserved cash released' WHERE order_id = ?",
                        (now_s, oid),
                    )
                    expired_cnt += 1

            # Refresh open positions including newly filled
            open_pos = conn.execute(
                "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND status = 'OPEN'",
                (self.session_id,),
            ).fetchall()

            for pos in open_pos:
                pid = pos["position_id"]
                sym = pos["ticker"]
                sdir = pos["direction"]
                sh = int(pos["shares"])
                epx = float(pos["entry_price"])
                sl = float(pos["stop_loss_px"])
                prod = pos["product_type"]
                ltp, brow = _latest_px_and_bar(sym, pos["entry_time"])

                curr_px = ltp
                if curr_px is None and brow is not None:
                    curr_px = float(brow[3])
                if curr_px is None:
                    curr_px = float(pos["current_price"])

                exit_reason = None
                exit_px = curr_px
                if prod == "MIS" and sym and str(pos["entry_time"])[:10] < today_s:
                    exit_reason = "OVERDUE_SESSION_SQUAREOFF"
                elif prod == "MIS" and hm_now >= "15:15":
                    exit_reason = "MANDATORY_1515_MIS_SQUAREOFF"
                elif sdir == "SELL" and ((brow is not None and float(brow[1]) >= sl) or curr_px >= sl):
                    exit_reason = "STRUCTURAL_STOP_HIT"
                    exit_px = max(sl, curr_px) if ltp is not None else sl
                elif sdir == "BUY" and sl > 0 and ((brow is not None and float(brow[2]) <= sl) or curr_px <= sl):
                    exit_reason = "STRUCTURAL_STOP_HIT"
                    exit_px = min(sl, curr_px) if ltp is not None else sl

                if exit_reason:
                    self._close_position_in_tx(conn, pos, exit_px, exit_reason, now_s, today_s)
                    closed_cnt += 1
                else:
                    unreal = ((curr_px - epx) if sdir == "BUY" else (epx - curr_px)) * sh
                    conn.execute(
                        "UPDATE consensus_v3_positions SET current_price = ?, unrealized_pnl_inr = ? WHERE position_id = ?",
                        (round(curr_px, 2), round(unreal, 2), pid),
                    )

            # Update Peak Equity & Max Drawdown
            st = conn.execute("SELECT * FROM consensus_v3_arena_state WHERE session_id = ?", (self.session_id,)).fetchone()
            rem_unreal = conn.execute(
                "SELECT COALESCE(SUM(unrealized_pnl_inr), 0.0) FROM consensus_v3_positions WHERE session_id = ? AND status = 'OPEN'",
                (self.session_id,),
            ).fetchone()[0]
            eq_now = float(st["funded_capital_inr"]) + float(st["realized_pnl_inr"]) + float(rem_unreal)
            pk = max(float(st["peak_equity_inr"]), eq_now)
            dd = max(float(st["max_drawdown_pct"]), ((pk - eq_now) / max(1.0, pk)) * 100.0)
            conn.execute(
                "UPDATE consensus_v3_arena_state SET peak_equity_inr = ?, max_drawdown_pct = ?, updated_at = ? WHERE session_id = ?",
                (round(pk, 2), round(dd, 2), now_s, self.session_id),
            )
            conn.commit()

        return {"filled": filled_cnt, "expired": expired_cnt, "closed": closed_cnt}

    def _close_position_in_tx(
        self,
        conn: sqlite3.Connection,
        pos: sqlite3.Row,
        exit_px: float,
        exit_reason: str,
        now_s: str,
        today_s: str,
        is_emergency: bool = False,
    ) -> Dict[str, Any]:
        pid = pos["position_id"]
        sdir = pos["direction"]
        sh = int(pos["shares"])
        epx = float(pos["entry_price"])
        prod = pos["product_type"]
        clean_xpx = round_nse_eq_tick(exit_px)

        if pos["execution_env"] == "LIVE_UPSTOX":
            opp = "BUY" if sdir == "SELL" else "SELL"
            self._route_upstox_equity_order(
                order_or_pos_id=pid,
                action=opp,
                product_type=prod,
                order_type="MARKET",
                ticker=pos["ticker"],
                instrument_key=pos["instrument_key"] or "",
                shares=sh,
                price=clean_xpx,
                is_emergency=is_emergency,
            )

        gross = ((clean_xpx - epx) if sdir == "BUY" else (epx - clean_xpx)) * sh
        if prod == "MIS":
            fees = compute_nse_mis_charges(epx, clean_xpx, sh, sdir, True, True, 1.0)["total_cost_inr"]
        else:
            fees = compute_nse_cnc_charges(epx, clean_xpx, sh, True, 1.0)["total_cost_inr"]
        net = round(gross - fees, 2)

        conn.execute(
            """
            UPDATE consensus_v3_positions
            SET status = 'CLOSED', current_price = ?, unrealized_pnl_inr = 0.0,
                exit_price = ?, exit_time = ?, exit_reason = ?,
                gross_pnl_inr = ?, fees_inr = ?, net_pnl_inr = ?
            WHERE position_id = ? AND status = 'OPEN'
            """,
            (clean_xpx, clean_xpx, now_s, exit_reason, round(gross, 2), fees, net, pid),
        )
        conn.execute(
            "UPDATE consensus_v3_arena_state SET realized_pnl_inr = realized_pnl_inr + ?, updated_at = ? WHERE session_id = ?",
            (net, now_s, self.session_id),
        )

        st = conn.execute("SELECT funded_capital_inr FROM consensus_v3_arena_state WHERE session_id = ?", (self.session_id,)).fetchone()
        halt_floor = -round(float(st["funded_capital_inr"]) * DAILY_LOSS_HALT_FRACTION, 2)
        conn.execute(
            """
            INSERT INTO consensus_v3_risk_ledger (session_date, realized_pnl_inr, fills_today, circuit_breaker_tripped, updated_at)
            VALUES (?, ?, 0, 0, ?)
            ON CONFLICT(session_date) DO UPDATE SET
                realized_pnl_inr = realized_pnl_inr + excluded.realized_pnl_inr,
                updated_at = excluded.updated_at
            """,
            (today_s, net, now_s),
        )
        r_led = conn.execute("SELECT realized_pnl_inr FROM consensus_v3_risk_ledger WHERE session_date = ?", (today_s,)).fetchone()
        if r_led and float(r_led[0]) <= halt_floor:
            conn.execute(
                "UPDATE consensus_v3_risk_ledger SET circuit_breaker_tripped = 1, trip_reason = ? WHERE session_date = ?",
                (f"Daily realized loss ₹{float(r_led[0]):.0f} breached halt ₹{halt_floor:.0f}", today_s),
            )
        return {"position_id": pid, "exit_px": clean_xpx, "net_pnl_inr": net, "fees_inr": fees}

    def manual_exit_position(self, position_id: str, reason: str = "MANUAL_OPERATOR_EXIT") -> Dict[str, Any]:
        now_dt = datetime.now()
        now_s = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        today_s = now_dt.strftime("%Y-%m-%d")
        with self._lock, self._conn() as conn:
            pos = conn.execute(
                "SELECT * FROM consensus_v3_positions WHERE position_id = ? AND session_id = ? AND status = 'OPEN'",
                (position_id, self.session_id),
            ).fetchone()
            if not pos:
                return {"status": "NOT_FOUND", "message": f"Open position {position_id} not found."}
            xpx = float(pos["current_price"] or pos["entry_price"])
            try:
                from app.data.market_provider import get_live_quote_with_meta
                qm = get_live_quote_with_meta(pos["ticker"])
                if qm and isinstance(qm.get("price"), (int, float)) and qm["price"] > 0:
                    xpx = float(qm["price"])
            except Exception:
                pass
            res = self._close_position_in_tx(conn, pos, xpx, reason, now_s, today_s)
            conn.commit()
        return {"status": "CLOSED", **res}

    def execute_sos_panic_exit(self) -> Dict[str, Any]:
        """Emergency 1-Click Flatten & Disarm: cancels all pending reservations, closes all open positions, disarms live bridge."""
        now_dt = datetime.now()
        now_s = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        today_s = now_dt.strftime("%Y-%m-%d")
        flattened = []
        with self._lock:
            self.disarm_live_trading()
            with self._conn() as conn:
                conn.execute(
                    """
                    UPDATE consensus_v3_pending_orders
                    SET status = 'CANCELLED_SOS', resolved_at = ?, resolution_note = 'SOS Panic Exit cancelled pending reservation'
                    WHERE session_id = ? AND status = 'PENDING_RESERVED'
                    """,
                    (now_s, self.session_id),
                )
                open_rows = conn.execute(
                    "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND status = 'OPEN'",
                    (self.session_id,),
                ).fetchall()
                for pos in open_rows:
                    xpx = float(pos["current_price"] or pos["entry_price"])
                    flattened.append(self._close_position_in_tx(conn, pos, xpx, "SOS_PANIC_EXIT", now_s, today_s, is_emergency=True))
                conn.commit()
        return {
            "status": "SOS_FLATTENED_AND_DISARMED",
            "simulation_mode": True,
            "flattened_count": len(flattened),
            "flattened_positions": flattened,
        }

    def get_positions_and_reservations(self, limit: int = 100) -> Dict[str, Any]:
        with self._conn() as conn:
            pend = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM consensus_v3_pending_orders WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                    (self.session_id, limit),
                ).fetchall()
            ]
            open_p = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND status = 'OPEN' ORDER BY entry_time DESC",
                    (self.session_id,),
                ).fetchall()
            ]
            closed_p = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND status = 'CLOSED' ORDER BY exit_time DESC LIMIT ?",
                    (self.session_id, limit),
                ).fetchall()
            ]
        return {
            "accounting": self.get_arena_accounting(),
            "pending_reserved_orders": [p for p in pend if p["status"] == "PENDING_RESERVED"],
            "recent_order_reservations": pend,
            "open_positions": open_p,
            "closed_positions": closed_p,
        }

    def get_recent_live_intents(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM consensus_v3_live_intents ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            ]


def get_consensus_v3_bridge() -> ConsensusArenaExecutionBridge:
    global _ARENA_BRIDGE_INSTANCE
    with _ARENA_BRIDGE_LOCK:
        if _ARENA_BRIDGE_INSTANCE is None:
            _ARENA_BRIDGE_INSTANCE = ConsensusArenaExecutionBridge()
        return _ARENA_BRIDGE_INSTANCE
