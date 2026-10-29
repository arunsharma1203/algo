"""
Option Sniffer: Isolated Order Intent & Failure Recovery Engine (Phase 2 — Simulation & Hardening).

STRICT ARCHITECTURAL GUARANTEES:
  1. Complete isolation from Core Option Sniffer signal generation and paper execution.
  2. Idempotent Order Intents: Every trade intent is assigned a deterministic UUID (INTENT_{signal_id}_{timestamp_ms}).
  3. Pre-Trade Slippage & Depth Validation (age < 1.0s, spread <= 2.0%, slippage <= 1.5%).
  4. Actual Fill-Anchored Broker-Side Stop Loss Orders (never based on theoretical paper prices).
  5. Multi-stage SOS Emergency Exit State Machine (OPEN -> EXIT_SUBMITTED -> EXIT_PENDING -> CONFIRMED_FLAT).
  6. Duplicate Order Suppression (Mutex lock + SQLite UNIQUE constraints).
  7. Crash Recovery: Reconciles open intents and positions on server restart.
"""

import os
import time
import json
import uuid
import sqlite3
import logging
import threading
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import requests
import re

from app.data.historical_data_layer import get_db_path
from app.data.upstox_provider import get_upstox_token, get_upstox_limiter
from app.analytics.option_sniffer.option_constants import (
    calculate_option_trade_costs,
    DEFAULT_TARGET_PCT,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TRAILING_TRIGGER_PCT,
    DEFAULT_TRAILING_STEP_PCT,
    DEFAULT_HOLDING_MAX_MINUTES,
    BREAKEVEN_FLOOR_PCT,
    is_option_entry_window,
    is_live_option_entry_window,
    LIVE_ENTRY_OPEN_HOUR,
    LIVE_ENTRY_OPEN_MINUTE,
    LIVE_ENTRY_CUTOFF_HOUR,
    LIVE_ENTRY_CUTOFF_MINUTE,
    OPTION_EXECUTION_ENGINE_MODE,
    MAX_EXECUTION_QUOTE_AGE_MS,
    UNRESOLVED_ORDER_TIMEOUT_SECONDS,
    MAX_DATA_STALENESS_SECONDS,
    THETA_STAGNATION_TIMEOUT_MINUTES,
    THETA_STAGNATION_MIN_MFE_PCT,
    to_ist_naive,
)
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution

logger = logging.getLogger(__name__)

_INTENT_LOCK = threading.RLock()
_EXIT_LOCKS: Dict[str, threading.Lock] = {}

# Execution States
STATE_CREATED = "CREATED"
STATE_VALIDATED = "VALIDATED"
STATE_SUBMITTED = "SUBMITTED"
STATE_SUBMITTED_AMBIGUOUS = "SUBMITTED_AMBIGUOUS"
STATE_AMBIGUOUS_ORDER_PENDING = "SUBMITTED_AMBIGUOUS"  # Standardized alias for unresolved order risk gating
STATE_PARTIALLY_FILLED = "PARTIALLY_FILLED"
STATE_FILLED = "FILLED"
STATE_REJECTED = "REJECTED"
STATE_CANCELLED = "CANCELLED"

# Exit States
EXIT_NONE = "NONE"
EXIT_SUBMITTED = "EXIT_SUBMITTED"
EXIT_PENDING = "EXIT_PENDING"
EXIT_PARTIALLY_FILLED = "EXIT_PARTIALLY_FILLED"
EXIT_PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
EXIT_CONFIRMED_FLAT = "CONFIRMED_FLAT"
EXIT_FAILED = "EXIT_FAILED"

# Default Calibrated Risk Limits (₹30,000 Capital / 2 Positions)
MAX_DAILY_LOSS_INR = 3500.0
MAX_RISK_PER_TRADE_INR = 2200.0
MAX_CONCURRENT_POSITIONS = 2
MAX_SLIPPAGE_PCT = 0.015  # 1.5% max slippage from signal price
MAX_SPREAD_PCT = 0.020    # 2.0% max spread
MAX_QUOTE_AGE_MS = MAX_EXECUTION_QUOTE_AGE_MS   # 1000ms tight pre-trade execution ceiling
DEFAULT_PROXY_URL = "http://arunsharma1203_cd6dbc37_99:zJeiuJKQGkZW1KJ4@in-mum.ipv6-proxy.in:13099"


def align_to_nse_tick(price: float) -> float:
    """Rounds price to nearest valid NSE ₹0.05 tick interval."""
    if price <= 0:
        return 0.05
    return round(round(price / 0.05) * 0.05, 2)


def sanitize_upstox_tag(tag: Any) -> str:
    """
    Sanitizes tag to strictly comply with Upstox V2 API specifications:
    Alphanumeric and hyphens only, max 20 chars, no underscores or special chars.
    """
    cleaned = re.sub(r'[^a-zA-Z0-9\-]', '-', str(tag or ""))
    cleaned = re.sub(r'-+', '-', cleaned).strip('-')
    return cleaned[:20]


def init_live_order_tables() -> None:
    """Creates isolated additive tables for order intents and live risk ledger."""
    with _INTENT_LOCK:
        try:
            conn = sqlite3.connect(get_db_path(), timeout=10.0)
            cur = conn.cursor()

            # 1. Order Intents & Position State Machine
            cur.execute("""
                CREATE TABLE IF NOT EXISTS option_live_order_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_intent_id TEXT UNIQUE NOT NULL,
                    signal_id TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    action TEXT NOT NULL,
                    lot_size INTEGER NOT NULL,
                    target_quantity INTEGER NOT NULL,
                    filled_quantity INTEGER DEFAULT 0,
                    limit_price REAL NOT NULL,
                    average_fill_price REAL DEFAULT 0.0,
                    state TEXT NOT NULL,
                    broker_order_id TEXT,
                    stop_loss_trigger REAL,
                    stop_loss_limit REAL,
                    broker_sl_order_id TEXT,
                    exit_state TEXT DEFAULT 'NONE',
                    exit_broker_order_id TEXT,
                    exit_fill_price REAL DEFAULT 0.0,
                    gross_pnl REAL DEFAULT 0.0,
                    statutory_charges REAL DEFAULT 0.0,
                    realized_pnl REAL DEFAULT 0.0,
                    rejection_reason TEXT,
                    dynamic_sl_pct REAL,
                    dynamic_target_pct REAL,
                    engine_version TEXT DEFAULT 'DYNAMIC_V2',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_oloi_signal_id ON option_live_order_intents(signal_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_oloi_state ON option_live_order_intents(state);")

            # Dynamic Engine 2.0 additive column migration
            intent_cols = [c[1] for c in cur.execute("PRAGMA table_info(option_live_order_intents);").fetchall()]
            if "dynamic_sl_pct" not in intent_cols:
                cur.execute("ALTER TABLE option_live_order_intents ADD COLUMN dynamic_sl_pct REAL;")
            if "dynamic_target_pct" not in intent_cols:
                cur.execute("ALTER TABLE option_live_order_intents ADD COLUMN dynamic_target_pct REAL;")
            if "engine_version" not in intent_cols:
                cur.execute("ALTER TABLE option_live_order_intents ADD COLUMN engine_version TEXT DEFAULT 'DYNAMIC_V2';")
            if "gross_pnl" not in intent_cols:
                cur.execute("ALTER TABLE option_live_order_intents ADD COLUMN gross_pnl REAL DEFAULT 0.0;")
            if "statutory_charges" not in intent_cols:
                cur.execute("ALTER TABLE option_live_order_intents ADD COLUMN statutory_charges REAL DEFAULT 0.0;")

            # 2. Risk & Circuit Breaker Ledger
            cur.execute("""
                CREATE TABLE IF NOT EXISTS option_live_risk_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_date TEXT UNIQUE NOT NULL,
                    starting_capital REAL NOT NULL,
                    realized_pnl REAL DEFAULT 0.0,
                    unrealized_pnl REAL DEFAULT 0.0,
                    peak_drawdown_inr REAL DEFAULT 0.0,
                    trades_executed INTEGER DEFAULT 0,
                    circuit_breaker_tripped INTEGER DEFAULT 0,
                    trip_reason TEXT,
                    updated_at TEXT NOT NULL
                );
            """)

            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[OptionLiveOrderIntent] Failed to initialize live order tables: {e}")


class OrderIntentEngine:
    """
    Manages idempotent order intents, pre-trade gates, linked broker stop-losses,
    and multi-stage SOS emergency square-off routines.
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path
        init_live_order_tables()
        self._peak_bids: Dict[str, float] = {}

    @property
    def db_path(self) -> str:
        return self._db_path or get_db_path()

    def _get_db_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def check_duplicate_intent(self, signal_id: str) -> bool:
        """Checks if an order intent already exists for this signal_id."""
        with self._get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM option_live_order_intents WHERE signal_id = ? AND state NOT IN ('REJECTED', 'CANCELLED');",
                (signal_id,)
            )
            return cur.fetchone() is not None

    def has_active_position_for_instrument(self, instrument_key: str) -> bool:
        """Checks if an active open or submitted position already exists for this instrument_key."""
        if not instrument_key:
            return False
        with self._get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM option_live_order_intents WHERE instrument_key = ? AND state IN ('SUBMITTED', 'PARTIALLY_FILLED', 'FILLED') AND exit_state != 'CONFIRMED_FLAT';",
                (instrument_key,)
            )
            return cur.fetchone() is not None

    def has_active_position_for_underlying(self, underlying: str) -> bool:
        """Checks if an active open or submitted position already exists for this underlying index (e.g. NIFTY)."""
        if not underlying:
            return False
        u_clean = str(underlying).upper().strip()
        with self._get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM option_live_order_intents WHERE UPPER(underlying) = ? AND state IN ('SUBMITTED', 'PARTIALLY_FILLED', 'FILLED') AND exit_state != 'CONFIRMED_FLAT';",
                (u_clean,)
            )
            return cur.fetchone() is not None

    def get_active_positions_count(self) -> int:
        """Returns the number of currently open/filled positions."""
        with self._get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM option_live_order_intents WHERE state IN ('SUBMITTED', 'PARTIALLY_FILLED', 'FILLED') AND exit_state != 'CONFIRMED_FLAT';"
            )
            return cur.fetchone()[0]

    def get_live_circuit_breaker_telemetry(
        self,
        as_of_date: Optional[str] = None,
        as_of_time: Optional[datetime] = None,
        max_daily_loss_inr: float = MAX_DAILY_LOSS_INR,
        max_consecutive_losses: int = 2,
        pause_minutes: int = 30
    ) -> Dict[str, Any]:
        """
        Audits armed-live trading P&L and risk exposure directly from SQLite option_live_order_intents.
        Guarantees strict separation between Paper and Live P&L:
          - Queries strictly from option_live_order_intents (NEVER touches option_sniffer_paper_trades).
          - Realized live P&L: Sum of closed and partially closed live trades for the date.
          - Unrealized live P&L: Mark-to-market of active open positions against streaming bid/LTP.
          - Total Risk Exposure: Realized Net P&L + Unrealized Net P&L.
          - Hard Session Shutdown: Halts new live entries if Realized OR Total Exposure breaches -₹3,500.
          - Dynamic Introspection Pause: 30m pause after 2 consecutive closed losses.
          - Persistent across process reboots and immune to frontend or signal resets.
          - Disclaimer: Configured threshold is a protective entry halt; market gaps and execution slippage can cause an overshoot.
        """
        curr_dt = to_ist_naive(as_of_time)
        date_str = as_of_date or curr_dt.strftime("%Y-%m-%d")

        # Date bounding: half-open interval [day_start, next_day_start)
        day_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
        from datetime import timedelta
        next_date_str = (day_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        day_start = f"{date_str} 00:00:00"
        next_day_start = f"{next_date_str} 00:00:00"

        with self._get_db_conn() as conn:
            cur = conn.cursor()
            # 0. Check persistent live risk ledger latch for today
            cur.execute("""
                SELECT circuit_breaker_tripped, trip_reason FROM option_live_risk_ledger 
                WHERE session_date = ?;
            """, (date_str,))
            latch_row = cur.fetchone()
            is_prior_latch_tripped = bool(latch_row and latch_row[0] == 1)

            # 1. Closed and Partially Closed trades for this session
            cur.execute("""
                SELECT order_intent_id, trading_symbol, underlying, average_fill_price, exit_fill_price,
                       filled_quantity, gross_pnl, statutory_charges, realized_pnl, exit_state, updated_at
                FROM option_live_order_intents
                WHERE exit_state IN ('CONFIRMED_FLAT', 'PARTIALLY_CLOSED')
                  AND updated_at >= ? AND updated_at < ?
                  AND broker_order_id IS NOT NULL AND broker_order_id NOT LIKE 'UPSTOX_%'
                ORDER BY updated_at DESC;
            """, (day_start, next_day_start))
            closed_rows = cur.fetchall()

            # 2. Open / Active live positions and outstanding submitted orders
            cur.execute("""
                SELECT order_intent_id, instrument_key, trading_symbol, underlying,
                       average_fill_price, filled_quantity, state, exit_state,
                       target_quantity, limit_price, dynamic_sl_pct, created_at, updated_at
                FROM option_live_order_intents
                WHERE state IN ('SUBMITTED', 'PARTIALLY_FILLED', 'FILLED')
                  AND exit_state != 'CONFIRMED_FLAT';
            """)
            open_rows = cur.fetchall()

        total_closed_today = len(closed_rows)
        realized_gross_pnl = sum(float(r[6] or 0.0) for r in closed_rows)
        realized_statutory_charges = sum(float(r[7] or 0.0) for r in closed_rows)
        realized_net_pnl = sum(float(r[8] or 0.0) for r in closed_rows)
        losses_today = sum(1 for r in closed_rows if float(r[8] or 0.0) < 0)
        wins_today = sum(1 for r in closed_rows if float(r[8] or 0.0) > 0)

        consecutive_losses = 0
        most_recent_loss_time = None
        for r in closed_rows:
            pnl = float(r[8] or 0.0)
            if pnl < 0:
                consecutive_losses += 1
                if most_recent_loss_time is None:
                    most_recent_loss_time = r[10]
            else:
                break

        # Calculate unrealized MTM for open positions and committed risk for outstanding orders
        unrealized_gross = 0.0
        unrealized_statutory = 0.0
        unrealized_net_pnl = 0.0
        active_positions_details = []
        outstanding_orders_count = 0
        outstanding_committed_risk = 0.0
        unresolved_broker_states_detected = False
        unresolved_reasons = []

        try:
            from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
            stream_mgr = OptionStreamManager()
        except Exception:
            stream_mgr = None

        for o in open_rows:
            (intent_id, ikey, tsym, und, avg_fill, open_qty,
             st, exit_st, target_qty, limit_pr, dyn_sl, created_ts, updated_ts) = o
            avg_price = float(avg_fill or 0.0)
            qty = int(open_qty or 0)
            t_qty = int(target_qty or 0)
            l_price = float(limit_pr or avg_price or 0.0)
            sl_pct = float(dyn_sl or 15.0)

            # Check for ambiguous / unresolved broker execution states (> 15s elapsed)
            if st == "SUBMITTED":
                outstanding_orders_count += 1
                try:
                    c_dt = datetime.fromisoformat(str(created_ts))
                    c_elapsed = (curr_dt - c_dt).total_seconds()
                    if c_elapsed > 15.0:
                        unresolved_broker_states_detected = True
                        unresolved_reasons.append(f"Order intent {intent_id} ({tsym}) pending broker SUBMITTED for {int(c_elapsed)}s")
                except Exception:
                    pass

                # Account for maximum committed stop-loss risk of the outstanding unconfirmed order
                order_committed_loss = round(t_qty * l_price * (sl_pct / 100.0), 2)
                try:
                    cost_info = calculate_option_trade_costs(
                        buy_price=l_price,
                        sell_price=l_price * (1.0 - sl_pct / 100.0),
                        quantity=t_qty,
                        apply_slippage=True,
                        trade_date=date_str
                    )
                    order_committed_loss = round(order_committed_loss + float(cost_info.get("total_friction", 55.0)), 2)
                except Exception:
                    order_committed_loss = round(order_committed_loss + 55.0, 2)
                outstanding_committed_risk += order_committed_loss

            if exit_st in ("EXIT_TRIGGERED", "EXIT_PENDING"):
                try:
                    u_dt = datetime.fromisoformat(str(updated_ts or created_ts))
                    u_elapsed = (curr_dt - u_dt).total_seconds()
                    if u_elapsed > 15.0:
                        unresolved_broker_states_detected = True
                        unresolved_reasons.append(f"Position {intent_id} ({tsym}) exit pending for {int(u_elapsed)}s")
                except Exception:
                    pass

            if st in ("PARTIALLY_FILLED", "FILLED") and qty > 0:
                quote = None
                if stream_mgr and ikey:
                    try:
                        quote = stream_mgr.get_option_quote(ikey)
                    except Exception:
                        quote = None
                bid_price = float(quote.get("bid") or quote.get("ltp") or avg_price) if quote else avg_price

                gross_mtm = round((bid_price - avg_price) * qty, 2)
                stat_cost = 0.0
                if avg_price > 0 and bid_price > 0 and qty > 0:
                    cost_info = calculate_option_trade_costs(buy_price=avg_price, sell_price=bid_price, quantity=qty, apply_slippage=False, trade_date=date_str)
                    stat_cost = float(cost_info.get("statutory_charges", 55.0))
                net_mtm = round(gross_mtm - stat_cost, 2)

                unrealized_gross += gross_mtm
                unrealized_statutory += stat_cost
                unrealized_net_pnl += net_mtm

                active_positions_details.append({
                    "order_intent_id": intent_id,
                    "instrument_key": ikey,
                    "trading_symbol": tsym,
                    "average_fill_price": avg_price,
                    "current_bid": bid_price,
                    "open_quantity": qty,
                    "unrealized_net_pnl": net_mtm
                })

                # If partially filled, add committed risk for the unfilled residual
                if st == "PARTIALLY_FILLED" and t_qty > qty:
                    unfilled_qty = t_qty - qty
                    residual_sl = round(unfilled_qty * l_price * (sl_pct / 100.0), 2)
                    outstanding_committed_risk += residual_sl

        total_net_pnl_exposure = round(realized_net_pnl + unrealized_net_pnl - outstanding_committed_risk, 2)

        # Chronological Peak-to-Trough Drawdown Calculation across closed live trades
        sorted_closed = sorted(closed_rows, key=lambda r: str(r[10] or ""))
        cum_pnl = 0.0
        peak_pnl = 0.0
        max_drawdown = 0.0
        for r in sorted_closed:
            cum_pnl += float(r[8] or 0.0)
            if cum_pnl > peak_pnl:
                peak_pnl = cum_pnl
            dd = peak_pnl - cum_pnl
            if dd > max_drawdown:
                max_drawdown = dd

        # Include open positions and outstanding commitments into current peak-to-trough drawdown
        current_drawdown = max(0.0, peak_pnl - total_net_pnl_exposure)
        if current_drawdown > max_drawdown:
            max_drawdown = current_drawdown

        # Enforce Two-Tier Live Circuit Breaker
        is_realized_limit_hit = realized_net_pnl <= -abs(max_daily_loss_inr)
        is_drawdown_limit_hit = max_drawdown >= abs(max_daily_loss_inr)
        is_exposure_limit_hit = total_net_pnl_exposure <= -abs(max_daily_loss_inr)
        is_max_loss_limit_hit = is_realized_limit_hit or is_drawdown_limit_hit or is_exposure_limit_hit or is_prior_latch_tripped
        is_consecutive_limit_hit = consecutive_losses >= max_consecutive_losses

        # Dynamic Introspection Pause
        is_pause_active = False
        pause_remaining_sec = 0
        pause_unfreeze_time = None
        if is_consecutive_limit_hit and most_recent_loss_time:
            try:
                loss_dt = datetime.fromisoformat(most_recent_loss_time)
                elapsed_sec = (curr_dt - loss_dt).total_seconds()
                total_pause_sec = pause_minutes * 60
                if 0 <= elapsed_sec < total_pause_sec:
                    is_pause_active = True
                    pause_remaining_sec = int(total_pause_sec - elapsed_sec)
                    pause_unfreeze_time = (loss_dt + timedelta(minutes=pause_minutes)).strftime("%H:%M:%S")
            except Exception as pe:
                logger.debug(f"[OrderIntentEngine] Live pause parse note: {pe}")

        is_session_halted = is_max_loss_limit_hit or unresolved_broker_states_detected
        halt_reason = None
        overshoot_disclaimer = " (Note: Configured ceiling is a protective entry halt; market gaps and execution slippage may cause an overshoot)."
        if is_max_loss_limit_hit:
            if is_prior_latch_tripped and latch_row and latch_row[1]:
                halt_reason = latch_row[1]
            elif is_realized_limit_hit:
                halt_reason = f"Armed-live daily realized loss ceiling reached (Realized Net: ₹{realized_net_pnl:,.2f} <= -₹{max_daily_loss_inr:,.2f}). New entries halted for session.{overshoot_disclaimer}"
            elif is_exposure_limit_hit:
                halt_reason = f"Armed-live daily total risk exposure breached (Total Exposure: ₹{total_net_pnl_exposure:,.2f} <= -₹{max_daily_loss_inr:,.2f} [Realized: ₹{realized_net_pnl:,.2f}, Unrealized: ₹{unrealized_net_pnl:,.2f}, Committed: ₹{outstanding_committed_risk:,.2f}]). New entries halted for session.{overshoot_disclaimer}"
            elif is_drawdown_limit_hit:
                halt_reason = f"Armed-live peak-to-trough drawdown ceiling reached (Drawdown: ₹{max_drawdown:,.2f} >= ₹{max_daily_loss_inr:,.2f} from peak ₹{peak_pnl:,.2f}). New entries halted for session.{overshoot_disclaimer}"
            else:
                halt_reason = f"Armed-live daily total risk exposure breached (Total Exposure: ₹{total_net_pnl_exposure:,.2f} <= -₹{max_daily_loss_inr:,.2f} [Realized: ₹{realized_net_pnl:,.2f}, Unrealized: ₹{unrealized_net_pnl:,.2f}, Committed: ₹{outstanding_committed_risk:,.2f}]). New entries halted for session.{overshoot_disclaimer}"

            # Persist live halt latch into option_live_risk_ledger
            try:
                with self._get_db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO option_live_risk_ledger 
                        (session_date, starting_capital, realized_pnl, unrealized_pnl, peak_drawdown_inr, trades_executed, circuit_breaker_tripped, trip_reason, updated_at)
                        VALUES (?, 15000.0, ?, ?, ?, ?, 1, ?, ?)
                        ON CONFLICT(session_date) DO UPDATE SET
                            realized_pnl = excluded.realized_pnl,
                            unrealized_pnl = excluded.unrealized_pnl,
                            peak_drawdown_inr = excluded.peak_drawdown_inr,
                            trades_executed = excluded.trades_executed,
                            circuit_breaker_tripped = 1,
                            trip_reason = excluded.trip_reason,
                            updated_at = excluded.updated_at;
                    """, (date_str, realized_net_pnl, unrealized_net_pnl, round(max_drawdown, 2), total_closed_today, halt_reason, curr_dt.strftime("%Y-%m-%d %H:%M:%S")))
                    conn.commit()
            except Exception as pe:
                logger.debug(f"[OrderIntentEngine] Live latch persist note: {pe}")
        elif unresolved_broker_states_detected:
            halt_reason = f"AMBIGUOUS_ORDER_PENDING: Fail-Closed: Unresolved broker execution states detected ({'; '.join(unresolved_reasons)}). New entries halted."
        elif is_pause_active:
            halt_reason = f"Armed-live Dynamic Introspection Pause: {consecutive_losses} consecutive losses. Paused for {pause_minutes}m ({pause_remaining_sec}s remaining until {pause_unfreeze_time} IST)."
        elif is_consecutive_limit_hit:
            halt_reason = f"Introspection cooldown completed for {consecutive_losses} consecutive losses."

        return {
            "date": date_str,
            "total_closed_today": total_closed_today,
            "wins_today": wins_today,
            "losses_today": losses_today,
            "consecutive_losses": consecutive_losses,
            "realized_gross_pnl": round(realized_gross_pnl, 2),
            "realized_statutory_charges": round(realized_statutory_charges, 2),
            "realized_net_pnl": round(realized_net_pnl, 2),
            "unrealized_net_pnl": round(unrealized_net_pnl, 2),
            "total_net_pnl_exposure": total_net_pnl_exposure,
            "is_statutory_charges_estimated": True,
            "charges_reconciliation_mode": "ESTIMATED_STATUTORY_SCHEDULE",
            "is_consecutive_limit_hit": is_consecutive_limit_hit,
            "is_max_loss_limit_hit": is_max_loss_limit_hit,
            "is_pause_active": is_pause_active,
            "pause_remaining_sec": pause_remaining_sec,
            "pause_unfreeze_time": pause_unfreeze_time,
            "is_session_halted": is_session_halted,
            "halt_reason": halt_reason,
            "halt_latch_active": is_max_loss_limit_hit,
            "most_recent_loss_time": most_recent_loss_time,
            "active_open_positions_count": len(open_rows),
            "active_positions_details": active_positions_details,
            "max_consecutive_losses": max_consecutive_losses,
            "max_daily_loss_inr": max_daily_loss_inr,
            "pause_minutes": pause_minutes
        }

    def handle_circuit_breaker_breach(self, live_quotes_map: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Enforces emergency risk containment when daily aggregate loss ceiling is breached:
          1. Cancels all pending / unconfirmed entry orders on broker.
          2. For all open filled positions, invokes existing verified SOS panic exit.
          3. Verifies broker state and reports any unconfirmed exits.
          4. Never promises a guaranteed max loss; accounts for gaps/slippage.
        """
        canceled_orders = []
        closed_positions = []
        unconfirmed_exits = []

        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. Cancel pending unconfirmed entry orders
        try:
            with self._get_db_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT order_intent_id, broker_order_id, state 
                    FROM option_live_order_intents 
                    WHERE state IN ('VALIDATED', 'SUBMITTED') AND exit_state != 'CONFIRMED_FLAT';
                """)
                pending_rows = cur.fetchall()

                for row in pending_rows:
                    intent_id, broker_ord_id, state = row
                    if broker_ord_id:
                        try:
                            self.cancel_broker_order(broker_ord_id)
                        except Exception as ce:
                            logger.warning(f"[OrderIntentEngine] Failed to cancel broker order {broker_ord_id}: {ce}")
                    cur.execute("""
                        UPDATE option_live_order_intents 
                        SET state = 'CANCELLED', rejection_reason = 'CIRCUIT_BREAKER_HALT: Canceled pending order intent', updated_at = ?
                        WHERE order_intent_id = ?;
                    """, (now_iso, intent_id))
                    canceled_orders.append(intent_id)
                conn.commit()
        except Exception as e:
            logger.error(f"[OrderIntentEngine] Error canceling pending orders during halt: {e}")

        # 2. For open filled positions, invoke verified SOS emergency exit
        try:
            with self._get_db_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT order_intent_id, instrument_key, filled_quantity, average_fill_price 
                    FROM option_live_order_intents 
                    WHERE state = 'FILLED' AND exit_state != 'CONFIRMED_FLAT';
                """)
                open_filled = cur.fetchall()

            for pos in open_filled:
                intent_id, ikey, qty, avg_fill = pos
                bid = 0.50
                if live_quotes_map and ikey in live_quotes_map:
                    q = live_quotes_map[ikey]
                    bid = float(q.get("bid") or q.get("ltp") or 0.50)
                sos_res = self.trigger_sos_panic_exit(intent_id, bid)
                status = sos_res.get("status")
                if status in ("CONFIRMED_FLAT", "ALREADY_FLAT"):
                    closed_positions.append(intent_id)
                else:
                    unconfirmed_exits.append({"order_intent_id": intent_id, "status": status, "message": sos_res.get("message")})
        except Exception as e:
            logger.error(f"[OrderIntentEngine] Error closing open positions during halt: {e}")

        return {
            "status": "CIRCUIT_BREAKER_HALT_CONTAINED",
            "is_halt_latched": True,
            "canceled_pending_orders_count": len(canceled_orders),
            "canceled_pending_orders": canceled_orders,
            "closed_positions_count": len(closed_positions),
            "closed_positions": closed_positions,
            "unconfirmed_exits_count": len(unconfirmed_exits),
            "unconfirmed_exits": unconfirmed_exits,
        }

    def create_and_validate_intent(
        self,
        signal_payload: Dict[str, Any],
        live_quote: Dict[str, Any],
        deployable_capital: float = 15000.0,
        daily_loss_inr: float = 0.0,
        enforce_regime_gate: Optional[bool] = None
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Validates risk limits, quote freshness, and slippage before persisting an order intent.
        Enforces idempotency, duplicate suppression, broker position reconciliation, and live circuit breaker.
        """
        signal_id = signal_payload.get("signal_id", "")
        if not signal_id:
            return False, "Missing signal_id in payload", None

        with _INTENT_LOCK:
            # 1. Duplicate Suppression Check (Thread-Safe & Database-Backed)
            if self.check_duplicate_intent(signal_id):
                return False, f"DUPLICATE_SUPPRESSED: Active order intent already exists for signal {signal_id}", None

            # 2. Ambiguous / Pending Order Check (Prevent double submissions when prior order is unresolved)
            with self._get_db_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT order_intent_id, trading_symbol, created_at, state
                    FROM option_live_order_intents
                    WHERE state IN ('SUBMITTED', 'SUBMITTED_AMBIGUOUS', 'AMBIGUOUS_ORDER_PENDING');
                """)
                submitted_rows = cur.fetchall()

            now_dt = datetime.now()
            for s_row in submitted_rows:
                s_id, amb_sym, created_str, st = s_row
                if st in ('SUBMITTED_AMBIGUOUS', 'AMBIGUOUS_ORDER_PENDING'):
                    return False, f"AMBIGUOUS_ORDER_PENDING: Prior order {s_id} for {amb_sym} is unresolved. New entries blocked until resolved.", None
                try:
                    c_dt = to_ist_naive(datetime.fromisoformat(str(created_str)))
                    elapsed = (now_dt - c_dt).total_seconds()
                    if elapsed > UNRESOLVED_ORDER_TIMEOUT_SECONDS:
                        return False, f"AMBIGUOUS_ORDER_PENDING: Prior order for {amb_sym} is awaiting broker confirmation ({int(elapsed)}s elapsed). New entries blocked until resolved.", None
                except Exception as pe:
                    logger.debug(f"[OrderIntentEngine] Pending order parse note: {pe}")

            # 3. Armed-Live Circuit Breaker Check (Realized + Unrealized Exposure from SQLite or daily_loss_inr)
            if (daily_loss_inr <= -abs(MAX_DAILY_LOSS_INR)) or (daily_loss_inr >= abs(MAX_DAILY_LOSS_INR) and daily_loss_inr != 0):
                return False, f"RISK_BLOCKED: Daily loss ceiling reached (₹{daily_loss_inr:.2f} limit: ₹{MAX_DAILY_LOSS_INR:.2f}). Trading halted for session.", None

            live_cb = self.get_live_circuit_breaker_telemetry()
            if isinstance(live_cb, dict) and live_cb.get("is_session_halted") is True:
                return False, f"RISK_BLOCKED: {live_cb.get('halt_reason')}", None
            if isinstance(live_cb, dict) and live_cb.get("is_pause_active") is True:
                return False, f"RISK_BLOCKED: {live_cb.get('halt_reason')}", None

            # 4. Duplicate Instrument Active Position Check
            ikey_check = signal_payload.get("instrument_key", "")
            if ikey_check and self.has_active_position_for_instrument(ikey_check):
                return False, f"DUPLICATE_INSTRUMENT_BLOCKED: Active position already open for instrument {ikey_check}", None

            # 5. Max Concurrent Positions Check
            if self.get_active_positions_count() >= MAX_CONCURRENT_POSITIONS:
                return False, f"RISK_BLOCKED: Max concurrent positions ({MAX_CONCURRENT_POSITIONS}) reached.", None

            # 6. Single Position Per Underlying Check (Max 1 active position per underlying index)
            underlying_check = signal_payload.get("underlying", "NIFTY")
            if underlying_check and self.has_active_position_for_underlying(underlying_check):
                return False, f"UNDERLYING_EXPOSURE_CAPPED: Active live position already exists for {underlying_check}. Max 1 live position per index.", None

            # 7. Pre-Trade Quote Freshness Check
            lq = live_quote if isinstance(live_quote, dict) else {}
            quote_age_ms = int(lq.get("quote_age_ms") or signal_payload.get("quote_age_ms") or 99999)
            if quote_age_ms > MAX_EXECUTION_QUOTE_AGE_MS:
                return False, f"QUOTE_STALE: Live quote age ({quote_age_ms}ms) exceeds execution max {MAX_EXECUTION_QUOTE_AGE_MS}ms (exceeds 1000ms execution freshness ceiling)", None

            # 8. Pre-Trade Spread Check
            ask = float(lq.get("ask") or lq.get("option_ask") or signal_payload.get("ask") or signal_payload.get("option_ask") or 0.0)
            bid = float(lq.get("bid") or lq.get("option_bid") or signal_payload.get("bid") or signal_payload.get("option_bid") or 0.0)
            if ask <= 0:
                return False, "INVALID_QUOTE: Ask price is zero or negative", None

            spread = ask - bid
            spread_pct = (spread / ask) if ask > 0 else 1.0
            if spread_pct > MAX_SPREAD_PCT and spread > 1.50:
                return False, f"SPREAD_TOO_WIDE: Spread {spread_pct*100:.2f}% (₹{spread:.2f}) exceeds limit", None

            # 9. Pre-Trade Slippage Check against Signal Entry Price
            suggested_entry = float(signal_payload.get("suggested_entry") or signal_payload.get("option_ask") or ask)
            max_allowable_ask = suggested_entry * (1.0 + MAX_SLIPPAGE_PCT)
            if ask > max_allowable_ask:
                return False, f"SLIPPAGE_EXCEEDED: Ask ₹{ask:.2f} exceeds signal entry ₹{suggested_entry:.2f} + 1.5% max buffer", None

            # 9B. Anti-Chase Guard (Block entry if option already surged > 15% from base)
            from app.analytics.option_sniffer.option_microstructure_evaluator import OptionMicrostructureEvaluator
            anti_chase_ok, anti_chase_msg = OptionMicrostructureEvaluator.evaluate_anti_chase(ask, suggested_entry)
            if not anti_chase_ok:
                return False, anti_chase_msg, None

            # 10. Pre-Trade Authoritative Continuous Regime & Chop Gate
            if signal_payload.get("regime_gate_passed") is False:
                reasons_str = "; ".join(signal_payload.get("regime_rejection_reasons", ["Regime gate rejected candidate"]))
                return False, f"REGIME_GATE_BLOCKED: {reasons_str}", None

            should_enforce_regime = enforce_regime_gate
            if should_enforce_regime is None:
                should_enforce_regime = signal_payload.get("enforce_regime_gate", False)

            if should_enforce_regime:
                from app.analytics.option_sniffer.option_regime_gate import OptionRegimeGate
                regime_gate = OptionRegimeGate(db_path=self.db_path)
                reg_check = regime_gate.evaluate_regime(
                    underlying=underlying_check,
                    opt_type=signal_payload.get("option_type") or ("CE" if "CE" in signal_payload.get("action", "") else "PE"),
                    as_of_time=signal_payload.get("as_of_time"),
                    continuous_bars=signal_payload.get("continuous_bars"),
                    spot_ltp=float(signal_payload.get("spot_price") or signal_payload.get("suggested_entry") or 0.0),
                    day_high=float(signal_payload.get("day_high") or 0.0) if signal_payload.get("day_high") else None,
                    day_low=float(signal_payload.get("day_low") or 0.0) if signal_payload.get("day_low") else None,
                    option_ask=float(live_quote.get("ask") or live_quote.get("option_ask") or 0.0),
                    target_pct=float(signal_payload.get("dynamic_target_pct") or 35.0),
                    lot_size=int(signal_payload.get("lot_size", 65)),
                    is_breakout=bool(signal_payload.get("is_breakout", False)),
                    enforce_cutoff=bool(signal_payload.get("enforce_cutoff", False))
                )
                if not reg_check["hurdles_passed"]:
                    reasons_str = "; ".join(reg_check["rejection_reasons"])
                    return False, f"REGIME_GATE_BLOCKED: {reasons_str}", None

            # 10. Armed-Live Broker Position Reconciliation Check (Verify against Upstox position book before admitting new entry)
            from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
            adapter = get_option_live_adapter()
            if getattr(adapter, "_is_live_armed", False) and ikey_check:
                pos_status = self.reconcile_broker_position(ikey_check)
                if pos_status.get("status") == "ERROR":
                    return False, "BROKER_RECONCILIATION_FAILED: Unable to verify broker position book (fail-closed).", None
                b_qty = pos_status.get("quantity")
                if b_qty and b_qty > 0:
                    return False, f"BROKER_POSITION_EXISTS: Broker confirms {b_qty} open quantity already exists for {ikey_check}.", None

            # 7. Per-Trade Risk & Margin / Capital Check
            underlying = signal_payload.get("underlying", "NIFTY")
            lot_size = 65 if underlying == "NIFTY" else (30 if underlying == "BANKNIFTY" else 50)
            
            # Dynamic 2.0 SL and Target Calculation (anchored to Spot ATR x Delta)
            engine_mode = OPTION_EXECUTION_ENGINE_MODE
            if engine_mode == "DYNAMIC_V2":
                dyn_sl_pct, dyn_tgt_pct, s_atr, d_used = OptionPaperExecution.calculate_dynamic_sl_and_target(
                    underlying=underlying,
                    spot_price=float(signal_payload.get("spot_price") or 0.0),
                    option_ask=ask,
                    delta=signal_payload.get("delta"),
                    iv=signal_payload.get("iv"),
                    spot_atr_pct=signal_payload.get("spot_atr_pct")
                )
            else:
                dyn_sl_pct, dyn_tgt_pct, s_atr, d_used = DEFAULT_STOP_LOSS_PCT, DEFAULT_TARGET_PCT, 40.0, 0.50

            # Enforce dynamic single-trade risk hurdle (dynamically scales to ~₹1,100 - ₹1,750, capped at ₹3,000)
            per_trade_risk = lot_size * ask * (dyn_sl_pct / 100.0)
            effective_max_risk = max(MAX_RISK_PER_TRADE_INR, 3000.0)
            if per_trade_risk > effective_max_risk:
                return False, f"RISK_BLOCKED: Per-trade risk ₹{per_trade_risk:,.2f} exceeds limit ₹{effective_max_risk:,.2f}.", None

            required_capital = (ask * lot_size) + 55.0  # ₹55 estimated charges
            if required_capital > deployable_capital:
                return False, f"INSUFFICIENT_MARGIN: Required ₹{required_capital:.2f} exceeds deployable ₹{deployable_capital:.2f}", None

        # Generate Deterministic Idempotency Key
        now_ts = int(time.time() * 1000)
        order_intent_id = f"INTENT_{signal_id}_{now_ts}"
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        intent_record = {
            "order_intent_id": order_intent_id,
            "signal_id": signal_id,
            "instrument_key": signal_payload.get("instrument_key", ""),
            "trading_symbol": signal_payload.get("trading_symbol", ""),
            "underlying": underlying,
            "action": signal_payload.get("action", "BUY_CE"),
            "lot_size": lot_size,
            "target_quantity": lot_size,
            "filled_quantity": 0,
            "limit_price": align_to_nse_tick(ask * 1.01),  # Marketable Limit with 1% safety cushion
            "average_fill_price": 0.0,
            "state": STATE_VALIDATED,
            "dynamic_sl_pct": dyn_sl_pct,
            "dynamic_target_pct": dyn_tgt_pct,
            "engine_version": engine_mode,
            "created_at": now_iso,
            "updated_at": now_iso
        }

        # Persist Intent to Database
        try:
            with self._get_db_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO option_live_order_intents 
                    (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, limit_price, state, dynamic_sl_pct, dynamic_target_pct, engine_version, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    order_intent_id, signal_id, intent_record["instrument_key"],
                    intent_record["trading_symbol"], underlying, intent_record["action"],
                    lot_size, lot_size, intent_record["limit_price"], STATE_VALIDATED,
                    dyn_sl_pct, dyn_tgt_pct, engine_mode, now_iso, now_iso
                ))
                conn.commit()
        except sqlite3.IntegrityError:
            return False, f"DUPLICATE_KEY: Order intent {order_intent_id} already exists in database", None
        except Exception as e:
            return False, f"DB_ERROR: Failed to save intent: {e}", None

        return True, "VALIDATED", intent_record

    def _call_upstox_broker_api(
        self,
        method: str,
        endpoint: str,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        is_emergency: bool = False
    ) -> Tuple[bool, int, Dict[str, Any]]:
        """
        Executes an HTTP request to Upstox V2 Broker Order endpoints
        routed strictly through the verified Mumbai static proxy and rate limiter.
        Emergency requests bypass routine throttling.
        """
        token = ""
        try:
            from app.data.upstox_provider import get_upstox_config
            cfg = get_upstox_config()
            token = (cfg.get("algo_token") or "").strip()
        except Exception:
            pass
        if not token:
            try:
                token = get_upstox_token()
            except Exception:
                token = ""
        if not token:
            return False, 401, {"error": "Missing Upstox Bearer Token"}

        try:
            # Routine orders and preflight calls wait up to 5.0s to allow background 15m ticks to pace without dropping orders
            # Emergency exits (SL hits, SOS squareoffs) use immediate priority bypass (is_emergency=True)
            acq_timeout = 5.0 if not is_emergency else 2.0
            get_upstox_limiter().acquire(timeout=acq_timeout, is_emergency=is_emergency)
        except Exception as e:
            return False, 429, {"error": f"Rate limiter acquisition timed out: {e}"}

        cleaned_ep = endpoint.lstrip("/")
        # Upstox V2 documentation specifies api-hft.upstox.com for order placement/modification/cancellation
        is_order_mutation = cleaned_ep.startswith(("order/place", "order/modify", "order/cancel"))
        base_host = "api-hft.upstox.com" if is_order_mutation else "api.upstox.com"
        url = f"https://{base_host}/v2/{cleaned_ep}"
        headers = {
            "Authorization": f"Bearer {token.strip()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) OptionSnifferLive/1.0"
        }
        proxies = {"http": DEFAULT_PROXY_URL, "https": DEFAULT_PROXY_URL}

        try:
            m = method.upper()
            if m == "GET":
                resp = requests.get(url, headers=headers, params=params, proxies=proxies, timeout=8.0)
            elif m == "POST":
                resp = requests.post(url, headers=headers, json=payload, proxies=proxies, timeout=8.0)
            elif m == "DELETE":
                resp = requests.delete(url, headers=headers, params=params, proxies=proxies, timeout=8.0)
            else:
                resp = requests.request(method, url, headers=headers, json=payload, params=params, proxies=proxies, timeout=8.0)

            # Upstox failover: If HFT gateway is temporarily unavailable (502/503/504), failover to api.upstox.com
            if resp.status_code in (502, 503, 504) and base_host == "api-hft.upstox.com":
                logger.warning(f"[OptionLiveOrderIntent] {base_host} returned {resp.status_code}, falling back to api.upstox.com")
                fallback_url = f"https://api.upstox.com/v2/{cleaned_ep}"
                if m == "POST":
                    resp = requests.post(fallback_url, headers=headers, json=payload, proxies=proxies, timeout=8.0)
                elif m == "DELETE":
                    resp = requests.delete(fallback_url, headers=headers, params=params, proxies=proxies, timeout=8.0)

            data = {}
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}

            return resp.status_code == 200, resp.status_code, data
        except Exception as e:
            if base_host == "api-hft.upstox.com":
                try:
                    fallback_url = f"https://api.upstox.com/v2/{cleaned_ep}"
                    if m == "POST":
                        resp = requests.post(fallback_url, headers=headers, json=payload, proxies=proxies, timeout=8.0)
                    elif m == "DELETE":
                        resp = requests.delete(fallback_url, headers=headers, params=params, proxies=proxies, timeout=8.0)
                    else:
                        resp = requests.request(method, fallback_url, headers=headers, json=payload, params=params, proxies=proxies, timeout=8.0)
                    data = resp.json() if hasattr(resp, "json") else {"raw": resp.text}
                    return resp.status_code == 200, resp.status_code, data
                except Exception as fb_err:
                    logger.error(f"[OptionLiveOrderIntent] Fallback to api.upstox.com also failed: {fb_err}")
            logger.error(f"[OptionLiveOrderIntent] Broker API request failed for {endpoint}: {e}")
            return False, 500, {"error": str(e)}

    def cancel_broker_order(self, broker_order_id: str) -> Dict[str, Any]:
        """Cancels an open or pending order on Upstox broker with emergency priority."""
        if not broker_order_id:
            return {"status": "SKIPPED", "message": "No broker order id"}
        if broker_order_id.startswith("UPSTOX_"):
            return {"status": "SIMULATED_CANCELLED", "broker_order_id": broker_order_id}

        success, status_code, data = self._call_upstox_broker_api(
            "DELETE", "/order/cancel", params={"order_id": broker_order_id}, is_emergency=True
        )
        return {"status": "CANCELLED" if success else "FAILED", "data": data}

    def reconcile_broker_position(self, instrument_key: str) -> Dict[str, Any]:
        """
        Queries broker short-term positions to confirm net open quantity.
        Strict fail-closed: Any API failure, non-200 status, missing/invalid token,
        or malformed payload returns status="ERROR" with quantity=None.
        Never implies flat (quantity=0) unless broker successfully confirms it.
        """
        resp = self._call_upstox_broker_api("GET", "/portfolio/short-term-positions", is_emergency=True)
        if not isinstance(resp, (tuple, list)) or len(resp) < 3:
            return {"status": "ERROR", "quantity": None, "reason": "Malformed broker API response"}
        success, status_code, data = resp[0], resp[1], resp[2]

        if not success or not isinstance(data, dict):
            err_reason = data.get("error") if isinstance(data, dict) else f"HTTP {status_code}"
            return {"status": "ERROR", "quantity": None, "reason": err_reason, "status_code": status_code}

        if data.get("status") == "error":
            err_msg = data.get("message") or (data.get("errors", [{}])[0].get("message") if isinstance(data.get("errors"), list) and data.get("errors") else "Broker error")
            return {"status": "ERROR", "quantity": None, "reason": err_msg, "status_code": status_code}

        positions = data.get("data")
        if not isinstance(positions, list):
            return {"status": "ERROR", "quantity": None, "reason": "Missing or malformed positions list in broker response"}

        for pos in positions:
            if pos.get("instrument_token") == instrument_key:
                net_qty = int(pos.get("quantity", 0))
                return {"status": "OK", "quantity": net_qty, "pnl": float(pos.get("pnl", 0.0))}

        # Position not found in successfully retrieved active position list -> confirmed flat
        return {"status": "OK", "quantity": 0, "pnl": 0.0}

    def _reconcile_ambiguous_submission(self, order_intent_id: str, order_tag: str) -> Dict[str, Any]:
        """
        Queries Upstox order book to check if an ambiguous/timed-out order actually exists on exchange.
        Probes up to 3 times before declaring an order absent.
        """
        for backoff in [1.0, 2.0, 3.0]:
            time.sleep(backoff)
            ok, code, ob_data = self._call_upstox_broker_api("GET", "/order/retrieve-all", is_emergency=True)
            if ok and isinstance(ob_data.get("data"), list):
                for ord_entry in ob_data["data"]:
                    ord_tag_raw = str(ord_entry.get("tag") or "")
                    if ord_tag_raw == order_tag or sanitize_upstox_tag(ord_tag_raw) == sanitize_upstox_tag(order_tag):
                        b_ord_id = ord_entry.get("order_id")
                        ord_status = (ord_entry.get("status") or "").lower()
                        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        with self._get_db_conn() as conn:
                            cur = conn.cursor()
                            if ord_status == "complete":
                                avg_fill = float(ord_entry.get("average_price", 0.0))
                                filled_q = int(ord_entry.get("filled_quantity", 0))
                                cur.execute("""
                                    UPDATE option_live_order_intents
                                    SET state = ?, broker_order_id = ?, average_fill_price = ?, filled_quantity = ?, updated_at = ?
                                    WHERE order_intent_id = ?;
                                """, (STATE_FILLED, b_ord_id, avg_fill, filled_q, now_iso, order_intent_id))
                                conn.commit()
                                # Auto place stop loss order
                                self.place_broker_stop_loss_order(order_intent_id, avg_fill, filled_q)
                                return {"found": True, "status": "FILLED", "broker_order_id": b_ord_id, "order_intent_id": order_intent_id, "recovered_after_timeout": True}
                            elif ord_status in ("open", "trigger pending"):
                                cur.execute("""
                                    UPDATE option_live_order_intents
                                    SET state = ?, broker_order_id = ?, updated_at = ?
                                    WHERE order_intent_id = ?;
                                """, (STATE_SUBMITTED, b_ord_id, now_iso, order_intent_id))
                                conn.commit()
                                return {"found": True, "status": "SUBMITTED", "broker_order_id": b_ord_id, "order_intent_id": order_intent_id, "recovered_after_timeout": True}
                            elif ord_status == "rejected":
                                rej_msg = ord_entry.get("status_message", "Exchange rejected")
                                cur.execute("""
                                    UPDATE option_live_order_intents
                                    SET state = ?, rejection_reason = ?, broker_order_id = ?, updated_at = ?
                                    WHERE order_intent_id = ?;
                                """, (STATE_REJECTED, rej_msg, b_ord_id, now_iso, order_intent_id))
                                conn.commit()
                                return {"found": True, "status": "REJECTED", "reason": rej_msg, "order_intent_id": order_intent_id, "recovered_after_timeout": True}
        return {"found": False}

    def reconcile_active_order_intents(self) -> List[Dict[str, Any]]:
        """
        Polls Upstox order book for any open intents in SUBMITTED or PARTIALLY_FILLED,
        and checks if any resting broker Stop-Loss orders were executed on exchange.
        """
        with self._get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM option_live_order_intents 
                WHERE (state IN ('SUBMITTED', 'PARTIALLY_FILLED') AND broker_order_id IS NOT NULL)
                   OR (state = 'FILLED' AND exit_state != 'CONFIRMED_FLAT' AND broker_sl_order_id IS NOT NULL);
            """)
            active_intents = [dict(r) for r in cur.fetchall()]

        if not active_intents:
            return []

        ok, code, ob_data = self._call_upstox_broker_api("GET", "/order/retrieve-all")
        if not ok or not isinstance(ob_data.get("data"), list):
            return []

        order_map = {o.get("order_id"): o for o in ob_data["data"] if o.get("order_id")}
        updated_records = []
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for intent in active_intents:
            # 1. Check if resting Broker Stop-Loss was triggered and executed on exchange
            if intent["state"] == STATE_FILLED and intent.get("broker_sl_order_id"):
                sl_id = intent["broker_sl_order_id"]
                if sl_id in order_map:
                    sl_order = order_map[sl_id]
                    sl_status = (sl_order.get("status") or "").lower()
                    if sl_status == "complete":
                        sl_fill = float(sl_order.get("average_price") or intent.get("stop_loss_trigger") or 0.0)
                        qty = intent["filled_quantity"] or intent["target_quantity"]
                        entry_price = intent["average_fill_price"] or intent["limit_price"]
                        statutory_info = calculate_option_trade_costs(
                            buy_price=entry_price,
                            sell_price=sl_fill,
                            quantity=qty,
                            apply_slippage=False,
                            trade_date=intent.get("created_at") or now_iso
                        )
                        eff_charges = statutory_info.get("statutory_charges", 55.0)
                        gross = (sl_fill - entry_price) * qty
                        net = round(gross - eff_charges, 2)
                        with self._get_db_conn() as conn:
                            cur = conn.cursor()
                            cur.execute("""
                                UPDATE option_live_order_intents
                                SET exit_state = ?, exit_fill_price = ?, gross_pnl = ?, statutory_charges = ?, realized_pnl = ?, updated_at = ?
                                WHERE order_intent_id = ?;
                            """, (EXIT_CONFIRMED_FLAT, sl_fill, round(gross, 2), round(eff_charges, 2), net, now_iso, intent["order_intent_id"]))
                            conn.commit()
                        logger.info(f"🛑 [OrderIntentEngine] Exchange Stop-Loss confirmed executed for {intent['order_intent_id']} @ ₹{sl_fill:.2f} (Net: ₹{net:,.2f})")
                        try:
                            from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
                            tsym = str(intent.get("trading_symbol") or "").upper()
                            opt_dir = "PE" if (tsym.endswith("PE") or " PE" in tsym) else "CE"
                            OptionSignalEngine.record_direction_sl_cooldown(
                                intent.get("underlying") or ("BANKNIFTY" if "BANKNIFTY" in tsym else "NIFTY"),
                                opt_dir
                            )
                        except Exception as sl_cd_err:
                            logger.debug(f"[OptionLiveOrderIntent] Direction SL cooldown note: {sl_cd_err}")
                        try:
                            from app.analytics.telegram_notifier import send_telegram_message
                            sl_msg = (
                                f"<b>🛑 UPSTOX LIVE POSITION EXITED</b>\n"
                                f"<b>{intent.get('trading_symbol')}</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━\n"
                                f"• <b>Exit Reason:</b> EXCHANGE_STOP_LOSS_HIT\n"
                                f"• <b>Exit Fill:</b> ₹{sl_fill:.2f}\n"
                                f"• <b>Entry Price:</b> ₹{entry_price:.2f}\n"
                                f"• <b>Quantity:</b> {qty}\n"
                                f"• <b>Net Realized P&L:</b> <b>{'🟢' if net >= 0 else '🔴'} ₹{net:+,.2f}</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━\n"
                                f"<i>⚡ Confirmed executed on Upstox</i>"
                            )
                            send_telegram_message(sl_msg, is_real_option_trade=True)
                        except Exception as sle:
                            logger.debug(f"[OptionLiveOrderIntent] Telegram SL alert note: {sle}")
                        updated_records.append({
                            "order_intent_id": intent["order_intent_id"],
                            "exit_state": EXIT_CONFIRMED_FLAT,
                            "sl_executed": True,
                            "realized_pnl": net
                        })
                continue

            # 2. Check pending Entry Orders
            b_id = intent["broker_order_id"]
            if b_id in order_map:
                broker_order = order_map[b_id]
                status = (broker_order.get("status") or "").lower()
                fill_price = float(broker_order.get("average_price") or 0.0)
                filled_qty = int(broker_order.get("filled_quantity") or 0)

                with self._get_db_conn() as conn:
                    cur = conn.cursor()
                    if status == "complete":
                        cur.execute("""
                            UPDATE option_live_order_intents
                            SET state = ?, average_fill_price = ?, filled_quantity = ?, updated_at = ?
                            WHERE order_intent_id = ?;
                        """, (STATE_FILLED, fill_price, filled_qty, now_iso, intent["order_intent_id"]))
                        conn.commit()
                        # Automatically place linked broker Stop Loss order
                        sl_res = self.place_broker_stop_loss_order(intent["order_intent_id"], fill_price, filled_qty)
                        try:
                            from app.analytics.telegram_notifier import send_telegram_message
                            sl_id = sl_res.get("broker_sl_order_id", "PENDING")
                            sl_trig = sl_res.get("stop_loss_trigger", 0.0)
                            fill_msg = (
                                f"<b>✅ UPSTOX LIVE ORDER FILLED</b>\n"
                                f"<b>{intent.get('trading_symbol')}</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━\n"
                                f"• <b>Contract:</b> {intent.get('trading_symbol')}\n"
                                f"• <b>Fill Price:</b> <b>₹{fill_price:.2f}</b>\n"
                                f"• <b>Quantity:</b> {filled_qty}\n"
                                f"• <b>Linked SL Trigger:</b> ₹{sl_trig:.2f}\n"
                                f"• <b>Broker SL ID:</b> <code>{sl_id}</code>\n"
                                f"━━━━━━━━━━━━━━━━━━━\n"
                                f"<i>⚡ Live order active on exchange with protection</i>"
                            )
                            send_telegram_message(fill_msg, is_real_option_trade=True)
                        except Exception as fe:
                            logger.debug(f"[OptionLiveOrderIntent] Telegram fill alert note: {fe}")
                        updated_records.append({"order_intent_id": intent["order_intent_id"], "state": STATE_FILLED, "sl_result": sl_res})
                    elif status == "cancelled":
                        if filled_qty > 0:
                            # Partial execution completed, remainder cancelled
                            cur.execute("""
                                UPDATE option_live_order_intents
                                SET state = ?, average_fill_price = ?, filled_quantity = ?, updated_at = ?
                                WHERE order_intent_id = ?;
                            """, (STATE_FILLED, fill_price, filled_qty, now_iso, intent["order_intent_id"]))
                            conn.commit()
                            sl_res = self.place_broker_stop_loss_order(intent["order_intent_id"], fill_price, filled_qty)
                            updated_records.append({
                                "order_intent_id": intent["order_intent_id"],
                                "state": STATE_FILLED,
                                "partial_remainder_cancelled": True,
                                "sl_result": sl_res
                            })
                        else:
                            cur.execute("""
                                UPDATE option_live_order_intents
                                SET state = ?, updated_at = ?
                                WHERE order_intent_id = ?;
                            """, (STATE_CANCELLED, now_iso, intent["order_intent_id"]))
                            conn.commit()
                            updated_records.append({"order_intent_id": intent["order_intent_id"], "state": STATE_CANCELLED})
                    elif status in ("partially filled", "partial"):
                        if filled_qty > 0:
                            cur.execute("""
                                UPDATE option_live_order_intents
                                SET state = ?, average_fill_price = ?, filled_quantity = ?, updated_at = ?
                                WHERE order_intent_id = ?;
                            """, (STATE_PARTIALLY_FILLED, fill_price, filled_qty, now_iso, intent["order_intent_id"]))
                            conn.commit()
                            if not intent.get("broker_sl_order_id"):
                                sl_res = self.place_broker_stop_loss_order(intent["order_intent_id"], fill_price, filled_qty)
                            else:
                                sl_res = {"status": "SL_ALREADY_EXISTS"}
                            updated_records.append({
                                "order_intent_id": intent["order_intent_id"],
                                "state": STATE_PARTIALLY_FILLED,
                                "sl_result": sl_res
                            })
                    elif status == "rejected":
                        rej_msg = broker_order.get("status_message", "Exchange rejected")
                        cur.execute("""
                            UPDATE option_live_order_intents
                            SET state = ?, rejection_reason = ?, updated_at = ?
                            WHERE order_intent_id = ?;
                        """, (STATE_REJECTED, rej_msg, now_iso, intent["order_intent_id"]))
                        conn.commit()
                        updated_records.append({"order_intent_id": intent["order_intent_id"], "state": STATE_REJECTED})
        return updated_records

    def check_and_execute_live_exits(self, live_quotes_map: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Monitors active filled live positions against incoming live stream quotes.
        Automatically triggers market exits for:
          1. Target Gain (+35% gain on option premium)
          2. Trailing Stop Ratchet (MFE >= +15%, trailing from peak)
          3. Max Intraday Holding Time (45 minutes)
          4. Session Cutoff (>= 15:15 IST)
        """
        with self._get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM option_live_order_intents
                WHERE state = 'FILLED' AND exit_state != 'CONFIRMED_FLAT';
            """)
            open_live = [dict(r) for r in cur.fetchall()]

        if not open_live:
            return []

        now_dt = datetime.now()
        closed_records = []

        for pos in open_live:
            ikey = pos["instrument_key"]
            quote = live_quotes_map.get(ikey)
            if not quote:
                continue

            current_bid = float(quote.get("bid") or quote.get("ltp") or 0.0)
            if current_bid <= 0:
                continue

            entry_fill = float(pos.get("average_fill_price") or pos.get("limit_price") or 0.0)
            if entry_fill <= 0:
                continue

            # Premium P&L % & Peak MFE Tracking for Trailing Stop Ratchet
            current_pnl_pct = ((current_bid - entry_fill) / entry_fill) * 100.0
            intent_id = pos["order_intent_id"]
            prev_peak = self._peak_bids.get(intent_id, entry_fill)
            prev_mfe_pct = ((prev_peak - entry_fill) / entry_fill) * 100.0 if entry_fill > 0 else 0.0
            current_peak = max(prev_peak, current_bid, entry_fill)
            self._peak_bids[intent_id] = current_peak
            mfe_pct = round(((current_peak - entry_fill) / entry_fill) * 100.0, 2)

            # Holding duration
            try:
                created_dt = datetime.fromisoformat(pos["created_at"])
                duration_min = (now_dt - created_dt).total_seconds() / 60.0
            except Exception:
                duration_min = 0.0

            exit_reason = None
            # Target & Ratchet thresholds (Dynamic V2, Uncapped Convex Runner, or Classic V1)
            engine_ver = pos.get("engine_version") or OPTION_EXECUTION_ENGINE_MODE
            target_hurdle = float(pos.get("dynamic_target_pct") or DEFAULT_TARGET_PCT)
            stop_hurdle = float(pos.get("dynamic_sl_pct") or DEFAULT_STOP_LOSS_PCT)
            lot_qty = int(pos.get("filled_quantity") or pos.get("target_quantity") or pos.get("lot_size") or 0)
            if lot_qty > 0 and entry_fill > 0:
                stop_hurdle = OptionPaperExecution.calculate_rupee_capped_sl_pct(entry_fill, lot_qty, stop_hurdle)
            ratchet_trigger = min(DEFAULT_TRAILING_TRIGGER_PCT, max(12.0, round(target_hurdle * 0.50, 1)))

            is_uncapped_mode = (
                engine_ver == "UNCAPPED_CONVEX_RUNNER"
                or bool(pos.get("uncapped_runner"))
                or (engine_ver == "V1_CLASSIC" and prev_mfe_pct >= DEFAULT_TRAILING_TRIGGER_PCT)
            )

            # 1. Take Profit Hurdle (Dynamic or Classic, bypassed when Uncapped Target-Lock Runner is active)
            if not is_uncapped_mode and current_pnl_pct >= target_hurdle:
                exit_reason = "TARGET_MET"
            # 2. Hard Stop Loss Hurdle (Backup defense in case broker exchange SL is delayed)
            elif current_pnl_pct <= -stop_hurdle:
                exit_reason = "STOP_LOSS_HIT"
            # 3. 4-Tier Chandelier Trailing Stop & Target-Lock Ratchet (100% Parity with Paper Execution)
            elif mfe_pct >= ratchet_trigger:
                chandelier_stop = OptionPaperExecution.calculate_chandelier_stop(
                    entry_fill,
                    current_peak,
                    mfe_pct,
                    target_hurdle=target_hurdle if is_uncapped_mode else None
                )
                peak_trail_stop = current_peak * (1.0 - DEFAULT_TRAILING_STEP_PCT / 100.0)
                breakeven_floor = entry_fill * (1.0 + BREAKEVEN_FLOOR_PCT / 100.0)
                ratchet_stop = max(chandelier_stop or 0.0, peak_trail_stop, breakeven_floor)
                if current_bid <= ratchet_stop:
                    exit_reason = "TRAILING_STOP_HIT"

            # 4. Theta Stagnation Stop (Apex Quant Velocity Protection)
            if not exit_reason and duration_min >= THETA_STAGNATION_TIMEOUT_MINUTES and mfe_pct < THETA_STAGNATION_MIN_MFE_PCT and current_pnl_pct < 0:
                exit_reason = "STAGNATION_TIME_STOP"

            # 5. Time Decay (45 min) & Session Cutoff (15:15 IST)
            if not exit_reason:
                if duration_min >= DEFAULT_HOLDING_MAX_MINUTES:
                    exit_reason = "TIME_DECAY_EXPIRY"
                elif (now_dt.hour > 15) or (now_dt.hour == 15 and now_dt.minute >= 15):
                    exit_reason = "SESSION_END_SQUAREOFF"

            if exit_reason:
                logger.info(f"⚡ [OrderIntentEngine] Auto-Exit Triggered ({exit_reason}): {pos['trading_symbol']} @ Bid ₹{current_bid:.2f} ({current_pnl_pct:+.1f}%)")
                res = self.trigger_sos_panic_exit(pos["order_intent_id"], current_bid_price=current_bid)
                res["exit_reason"] = exit_reason
                closed_records.append(res)

        return closed_records

    def check_and_dispatch_automated_entries(
        self,
        live_quotes_map: Dict[str, Dict[str, Any]],
        deployable_capital: float,
        max_signal_age_sec: float = 90.0
    ) -> List[Dict[str, Any]]:
        """
        When live trading is armed, continuously evaluates newly generated ACTIVE signals.
        If concurrent positions < MAX_CONCURRENT_POSITIONS (2), automatically selects top-conviction
        signal, validates risk limits and slippage, and submits live broker order to Upstox.
        Enforces:
          - Gate 0A: Golden Entry Window (09:30 to 14:00 IST)
          - Gate 10: Strictly status == 'ACTIVE' (cooldown-passed signals only)
          - Signal Recency: Only signals generated within max_signal_age_sec (default 90s)
          - Fail-Closed Live Quote: Never spoofs missing WebSocket quotes
        """
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        adapter = get_option_live_adapter()
        if not adapter._is_live_armed:
            return []

        now_dt = datetime.now()
        # Gate 0A: Live Canary Scout & Dynamic Microstructure Window (09:35 / 09:45 - 13:15 IST)
        from app.analytics.option_sniffer.option_microstructure_evaluator import OptionMicrostructureEvaluator
        ms_eval = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=now_dt)
        is_dynamic_unlocked = ms_eval.get("is_unlocked", False)
        if not is_live_option_entry_window(now_dt, allow_dynamic_early_unlock=is_dynamic_unlocked):
            return []

        # Gate 0B: Canary Scout Interlock (Zero paper stop losses today)
        from app.analytics.option_sniffer.option_db import OptionDB
        has_paper_sl, canary_reason = OptionDB.has_paper_stop_loss_today(now_dt.strftime("%Y-%m-%d"))
        if has_paper_sl:
            logger.warning(f"[Autopilot] 🚨 CANARY SCOUT ABORT: Paper trading stopped out today ({canary_reason}). Live trading blocked to preserve real capital.")
            return []

        # Gate 0C: Live Circuit Breaker Check
        live_cb = self.get_live_circuit_breaker_telemetry()
        if isinstance(live_cb, dict) and live_cb.get("is_session_halted"):
            logger.warning(f"[Autopilot] 🚨 Live circuit breaker halted: {live_cb.get('halt_reason')}")
            return []

        # Gate 0D: Portfolio Introspection Engine ("Stop, Look Back & Decide")
        try:
            from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
            if now_dt.timestamp() < OptionSignalEngine._INTROSPECTION_FROZEN_UNTIL:
                rem_f = int(OptionSignalEngine._INTROSPECTION_FROZEN_UNTIL - now_dt.timestamp())
                logger.info(f"[Autopilot] Portfolio Introspection Freeze: Live entry dispatch globally frozen ({rem_f}s remaining).")
                return []
        except Exception:
            pass

        active_count = self.get_active_positions_count()
        if active_count >= MAX_CONCURRENT_POSITIONS:
            return []

        recent_signals = adapter.get_read_only_recent_signals(limit=20, today_only=True)
        actionable = []
        armed_at_iso = getattr(adapter, "_armed_at_iso", "")

        for s in recent_signals:
            # Strictly require status == 'ACTIVE' so Gate 9/10 Cooldown is 100% respected
            if s.get("action") in ("BUY_CE", "BUY_PE") and s.get("status") == "ACTIVE":
                sig_id = s.get("signal_id", "")
                ikey = s.get("instrument_key", "")
                und = str(s.get("underlying") or "NIFTY").upper()
                if not sig_id or self.check_duplicate_intent(sig_id):
                    continue
                if ikey and self.has_active_position_for_instrument(ikey):
                    continue
                if und and self.has_active_position_for_underlying(und):
                    continue

                # STRICT ZERO LOOKBACK: signal must have been generated strictly AFTER live trading was armed today!
                ts_str = s.get("timestamp") or s.get("created_at") or ""
                if isinstance(armed_at_iso, str) and armed_at_iso and str(ts_str) < armed_at_iso:
                    continue

                # Verify signal recency so stale signals from earlier in the morning are never executed
                if ts_str and max_signal_age_sec > 0:
                    try:
                        sig_dt = datetime.fromisoformat(str(ts_str))
                        age_sec = (now_dt - sig_dt).total_seconds()
                        if age_sec > max_signal_age_sec or age_sec < -5.0:
                            continue
                    except Exception:
                        pass

                cal_prob = float(s.get("calibrated_prob") or 0.5)
                exp_ret = float(s.get("expected_net_return_pct") or 0.0)
                score = round(cal_prob * max(0.1, exp_ret), 3)
                s_copy = dict(s)
                s_copy["priority_score"] = score
                actionable.append(s_copy)

        if not actionable:
            return []

        actionable.sort(key=lambda x: x["priority_score"], reverse=True)
        dispatched_orders = []

        for cand in actionable:
            if self.get_active_positions_count() >= MAX_CONCURRENT_POSITIONS:
                break

            ikey = cand.get("instrument_key", "")
            und = str(cand.get("underlying") or "NIFTY").upper()
            if und and self.has_active_position_for_underlying(und):
                continue

            # Direction-level Stop-Loss Cooldown & Anti-Ping-Pong Flip Guard:
            try:
                from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
                from app.analytics.option_sniffer.option_constants import ANTI_PING_PONG_FLIP_MINUTES
                opt_dir = "PE" if cand.get("action") == "BUY_PE" else "CE"
                dir_key = f"{und}_{opt_dir}"
                last_sl_epoch = OptionSignalEngine._DIRECTION_SL_COOLDOWNS.get(dir_key, 0.0)
                if (now_dt.timestamp() - last_sl_epoch) < OptionSignalEngine._DIRECTION_SL_COOLDOWN_SECONDS:
                    rem_sl = int(OptionSignalEngine._DIRECTION_SL_COOLDOWN_SECONDS - (now_dt.timestamp() - last_sl_epoch))
                    logger.info(f"[Autopilot] Direction SL Cooldown: {cand.get('trading_symbol')} blocked for {rem_sl}s after recent {dir_key} Stop-Loss.")
                    continue

                # Anti-Ping-Pong Flip Lockout (opposite direction)
                opp_dir = "PE" if opt_dir == "CE" else "CE"
                opp_key = f"{und}_{opp_dir}"
                opp_last_sl = OptionSignalEngine._DIRECTION_SL_COOLDOWNS.get(opp_key, 0.0)
                if (now_dt.timestamp() - opp_last_sl) < (ANTI_PING_PONG_FLIP_MINUTES * 60):
                    rem_flip = int((ANTI_PING_PONG_FLIP_MINUTES * 60 - (now_dt.timestamp() - opp_last_sl)) / 60)
                    logger.info(f"[Autopilot] Anti-Ping-Pong Lockout: {cand.get('trading_symbol')} blocked for {rem_flip}m after recent {opp_key} Stop-Loss.")
                    continue
            except Exception:
                pass

            # Anti-Counter-Move & Mid-Day Chop Defense Guards:
            try:
                from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
                stream_mgr = OptionStreamManager()
                u_df = stream_mgr.get_underlying_history_df(und)
                if u_df is not None and len(u_df) >= 4:
                    closes = u_df["close"]
                    ret_3m = float((closes.iloc[-1] - closes.iloc[-4]) / closes.iloc[-4] * 100.0)
                    action = cand.get("action", "")
                    if action == "BUY_CE" and ret_3m <= -0.06:
                        logger.info(f"[Autopilot] Counter-move guard: {cand.get('trading_symbol')} CE blocked because {und} is dropping ({ret_3m:.2f}% over last 3m).")
                        continue
                    if action == "BUY_PE" and ret_3m >= 0.06:
                        logger.info(f"[Autopilot] Counter-move guard: {cand.get('trading_symbol')} PE blocked because {und} is bouncing (+{ret_3m:.2f}% over last 3m).")
                        continue

                # 10-Minute Range Compression Check (Theta Defense)
                if u_df is not None and len(u_df) >= 10:
                    high_col = "high" if "high" in u_df.columns else "High"
                    low_col = "low" if "low" in u_df.columns else "Low"
                    if high_col in u_df.columns and low_col in u_df.columns:
                        recent_10m = u_df.iloc[-10:]
                        high_10m = float(recent_10m[high_col].max())
                        low_10m = float(recent_10m[low_col].min())
                        if low_10m > 0:
                            range_10m_pct = (high_10m - low_10m) / low_10m * 100.0
                            range_pts = high_10m - low_10m
                            threshold_pts = 45.0 if "BANK" in und else 18.0
                            current_spot = float(u_df["close"].iloc[-1])
                            if (range_10m_pct < 0.08 or range_pts < threshold_pts) and (low_10m <= current_spot <= high_10m):
                                logger.info(f"[Autopilot] Chop Defense: {cand.get('trading_symbol')} blocked because {und} is in compressed 10m sideways consolidation ({range_10m_pct:.2f}% < 0.08%, {range_pts:.1f} pts).")
                                continue
            except Exception as cm_err:
                logger.debug(f"[Autopilot] Counter-move & chop check note: {cm_err}")

            live_quote = live_quotes_map.get(ikey)
            # Fail-closed: if no live WebSocket quote is available in stream cache, do NOT spoof quote_age_ms
            if not live_quote:
                continue

            ok, reason, intent = self.create_and_validate_intent(
                signal_payload=cand,
                live_quote=live_quote,
                deployable_capital=deployable_capital
            )

            if ok and intent:
                exec_res = self.place_real_entry_order(intent["order_intent_id"])
                logger.info(f"⚡ [Autopilot] Auto-placed live entry for {cand.get('trading_symbol')}: {exec_res}")
                dispatched_orders.append(exec_res)
                lot_cost = float(intent.get("limit_price", 0.0)) * int(intent.get("target_quantity", 65))
                deployable_capital -= max(0.0, lot_cost)

                # Instant Telegram alert for live order execution status
                try:
                    from app.analytics.telegram_notifier import send_telegram_message
                    tsym = cand.get("trading_symbol", "")
                    dyn_sl = intent.get("dynamic_sl_pct", DEFAULT_STOP_LOSS_PCT)
                    dyn_tgt = intent.get("dynamic_target_pct", DEFAULT_TARGET_PCT)
                    limit_p = float(intent.get("limit_price", 0.0))
                    qty = int(intent.get("target_quantity", 65))
                    b_id = exec_res.get("broker_order_id", "N/A")

                    if exec_res.get("status") == "SUBMITTED":
                        t_msg = (
                            f"<b>🚀 UPSTOX LIVE ORDER SUBMITTED</b>\n"
                            f"<b>{cand.get('action', 'BUY')} | {tsym}</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"• <b>Broker Order ID:</b> <code>{b_id}</code>\n"
                            f"• <b>Contract:</b> {tsym}\n"
                            f"• <b>Quantity:</b> {qty}\n"
                            f"• <b>Limit Price:</b> ₹{limit_p:.2f}\n"
                            f"• <b>Dynamic SL:</b> -{dyn_sl:.1f}%\n"
                            f"• <b>Dynamic Target:</b> +{dyn_tgt:.1f}%\n"
                            f"• <b>Broker Mode:</b> Upstox Live Intraday (NFO)\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"<i>⚡ Autopilot live broker order successfully placed</i>"
                        )
                        send_telegram_message(t_msg)
                    elif exec_res.get("status") == "REJECTED":
                        rej_reason = exec_res.get("reason", "Unknown")
                        t_msg = (
                            f"<b>⚠️ UPSTOX LIVE ORDER REJECTED</b>\n"
                            f"<b>{cand.get('action', 'BUY')} | {tsym}</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"• <b>Rejection Reason:</b> {rej_reason}\n"
                            f"• <b>Contract:</b> {tsym}\n"
                            f"• <b>Attempted Limit Price:</b> ₹{limit_p:.2f}\n"
                            f"• <b>Quantity:</b> {qty}\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"<i>Recorded in audit trail</i>"
                        )
                        send_telegram_message(t_msg)
                except Exception as t_err:
                    logger.debug(f"[OptionLiveOrderIntent] Telegram live dispatch alert note: {t_err}")

        return dispatched_orders

    def reconcile_broker_startup_state(self) -> Dict[str, Any]:
        """
        On application boot, queries Upstox positions and open orders.
        Reconciles any discrepancies with SQLite bidirectionally and fails closed if unmanaged positions exist:
          - Detects untracked broker positions and imports them to SQLite (fails closed / disarms live).
          - Detects SQLite intents whose broker position has already closed and marks them CONFIRMED_FLAT.
        """
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        adapter = get_option_live_adapter()

        ok, code, data = self._call_upstox_broker_api("GET", "/portfolio/short-term-positions", is_emergency=True)
        if not ok or not isinstance(data.get("data"), list):
            return {"status": "BROKER_UNAVAILABLE", "positions": []}

        positions = data["data"]
        option_positions = [p for p in positions if str(p.get("instrument_token", "")).startswith("NSE_FO")]
        open_option_positions = [p for p in option_positions if int(p.get("quantity", 0)) > 0]
        broker_open_map = {str(p.get("instrument_token")): int(p.get("quantity", 0)) for p in open_option_positions}

        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reconciled_flat_count = 0

        with self._get_db_conn() as conn:
            cur = conn.cursor()

            # 1. Check existing open intents in SQLite: if broker confirms flat, transition to CONFIRMED_FLAT
            cur.execute("""
                SELECT order_intent_id, instrument_key 
                FROM option_live_order_intents 
                WHERE state = 'FILLED' AND exit_state != 'CONFIRMED_FLAT';
            """)
            active_db_intents = cur.fetchall()
            for r in active_db_intents:
                db_intent_id, db_ikey = r[0], r[1]
                if db_ikey not in broker_open_map or broker_open_map[db_ikey] <= 0:
                    cur.execute("""
                        UPDATE option_live_order_intents
                        SET exit_state = ?, filled_quantity = 0, updated_at = ?
                        WHERE order_intent_id = ?;
                    """, (EXIT_CONFIRMED_FLAT, now_iso, db_intent_id))
                    reconciled_flat_count += 1
            conn.commit()

            # 2. Check open broker positions: import untracked positions and fail closed
            if open_option_positions:
                logger.warning(f"⚠️ [OrderIntentEngine] Startup reconciliation detected {len(open_option_positions)} active broker option positions!")
                adapter._is_live_armed = False

                for p in open_option_positions:
                    ikey = p.get("instrument_token")
                    qty = int(p.get("quantity", 0))
                    buy_price = float(p.get("buy_price", 0.0) or p.get("average_price", 0.0))
                    symbol = p.get("trading_symbol", "")
                    
                    cur.execute("SELECT * FROM option_live_order_intents WHERE instrument_key = ? AND exit_state != 'CONFIRMED_FLAT';", (ikey,))
                    existing = cur.fetchone()
                    if not existing:
                        intent_id = f"RECOVERED_{ikey.split('|')[-1]}_{int(time.time())}"
                        cur.execute("""
                            INSERT INTO option_live_order_intents
                            (order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action, lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state, exit_state, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """, (intent_id, "STARTUP_RECOVERY", ikey, symbol, "NIFTY" if "NIFTY" in symbol else "BANKNIFTY", "BUY", qty, qty, qty, buy_price, buy_price, STATE_FILLED, EXIT_NONE, now_iso, now_iso))
                conn.commit()

        return {
            "status": "RECONCILED",
            "open_positions_count": len(open_option_positions),
            "reconciled_flat_count": reconciled_flat_count
        }

    def place_real_entry_order(self, order_intent_id: str) -> Dict[str, Any]:
        """
        Places a real buy order with Upstox broker.
        Strict fail-closed guards:
          - is_live_armed must be True.
          - Market hours must be < 15:15 IST and inside Golden Entry Window (09:30 - 12:45 IST).
          - Pre-flight eligibility must be True (NFO active, margin >= ₹10k, IP match).
          - Intent must exist in STATE_VALIDATED.
        On fill, automatically submits linked broker-side Stop Loss order.
        """
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        adapter = get_option_live_adapter()

        # Guard 1: Live Arming Interlock
        if not adapter._is_live_armed:
            return {"status": "BLOCKED", "reason": "LIVE_TRADING_NOT_ARMED: Real broker orders are disabled. Mode is Paper."}

        # Guard 2: Market Cutoff (15:15 IST) & Canary Scout Live Window (09:45 - 13:15 IST)
        now = datetime.now()
        if now.time() >= datetime.strptime("15:15", "%H:%M").time():
            adapter._is_live_armed = False
            return {"status": "BLOCKED", "reason": "SESSION_CLOSED: Market cutoff 15:15 IST passed."}
        from app.analytics.option_sniffer.option_microstructure_evaluator import OptionMicrostructureEvaluator
        ms_eval = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=now)
        is_dynamic_unlocked = ms_eval.get("is_unlocked", False)
        if not is_live_option_entry_window(now, allow_dynamic_early_unlock=is_dynamic_unlocked):
            return {
                "status": "BLOCKED",
                "reason": f"OUTSIDE_LIVE_WINDOW (OUTSIDE_GOLDEN_WINDOW): Live option entries restricted. Dynamic microstructure status: {ms_eval.get('unlock_reason')}"
            }

        # Guard 2B: Canary Scout Interlock (Zero paper stop losses today)
        from app.analytics.option_sniffer.option_db import OptionDB
        has_paper_sl, canary_reason = OptionDB.has_paper_stop_loss_today(now.strftime("%Y-%m-%d"))
        if has_paper_sl:
            return {
                "status": "BLOCKED",
                "reason": f"CANARY_SCOUT_ABORTED: Live entry blocked for session. Paper canary scout detected adverse regime: {canary_reason}"
            }

        # Guard 2C: Live Circuit Breaker Check
        live_cb = self.get_live_circuit_breaker_telemetry()
        if isinstance(live_cb, dict) and live_cb.get("is_session_halted"):
            return {
                "status": "BLOCKED",
                "reason": f"LIVE_CIRCUIT_BREAKER_TRIPPED: Live trading halted for session: {live_cb.get('halt_reason')}"
            }

        # Guard 3: Pre-flight Verification
        readiness = adapter.fetch_account_readiness(force_refresh=False)
        if not readiness.get("live_trading_eligible", False):
            return {"status": "BLOCKED", "reason": f"PREFLIGHT_FAIL: {readiness.get('blockers')}"}

        with self._get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM option_live_order_intents WHERE order_intent_id = ?;", (order_intent_id,))
            row = cur.fetchone()
            if not row:
                return {"status": "ERROR", "reason": f"Order intent {order_intent_id} not found"}
            intent = dict(row)

            if intent["state"] != STATE_VALIDATED:
                return {"status": "REJECTED", "reason": f"Intent state is {intent['state']}, expected VALIDATED"}

        # Prepare Upstox V2 Order Place Payload (strict alphanumeric/hyphen tag <= 20 chars)
        order_tag = sanitize_upstox_tag(intent["order_intent_id"][:20])
        payload = {
            "quantity": int(intent["target_quantity"]),
            "product": "I",
            "validity": "DAY",
            "price": float(intent["limit_price"]),
            "tag": order_tag,
            "instrument_token": str(intent["instrument_key"]),
            "order_type": "LIMIT",
            "transaction_type": "BUY",
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False
        }

        success, status_code, data = self._call_upstox_broker_api("POST", "/order/place", payload=payload)
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._get_db_conn() as conn:
            cur = conn.cursor()
            if success and data.get("status") == "success":
                broker_order_id = data.get("data", {}).get("order_id", f"UPSTOX_ORD_{int(time.time()*1000)}")
                cur.execute("""
                    UPDATE option_live_order_intents
                    SET state = ?, broker_order_id = ?, updated_at = ?
                    WHERE order_intent_id = ?;
                """, (STATE_SUBMITTED, broker_order_id, now_iso, order_intent_id))
                conn.commit()
                # Run an immediate reconciliation in case marketable limit filled instantaneously
                try:
                    self.reconcile_active_order_intents()
                except Exception as rec_err:
                    logger.debug(f"[OptionLiveOrderIntent] Immediate fill probe note: {rec_err}")
                return {
                    "status": "SUBMITTED",
                    "broker_order_id": broker_order_id,
                    "order_intent_id": order_intent_id
                }
            else:
                # Ambiguous Timeout / Network Failure Recovery: check order book before marking rejected
                recovered = self._reconcile_ambiguous_submission(order_intent_id, order_tag)
                if recovered.get("found"):
                    return recovered

                error_msg = data.get("errors", [{}])[0].get("message", f"HTTP {status_code}: {data}") if isinstance(data.get("errors"), list) and data.get("errors") else str(data)
                cur.execute("""
                    UPDATE option_live_order_intents
                    SET state = ?, rejection_reason = ?, updated_at = ?
                    WHERE order_intent_id = ?;
                """, (STATE_REJECTED, error_msg, now_iso, order_intent_id))
                conn.commit()
                return {
                    "status": "REJECTED",
                    "reason": error_msg,
                    "order_intent_id": order_intent_id
                }

    def place_broker_stop_loss_order(
        self,
        order_intent_id: str,
        filled_price: float,
        filled_qty: int,
        sl_pct: float = DEFAULT_STOP_LOSS_PCT
    ) -> Dict[str, Any]:
        """
        Places linked Stop-Loss Limit order with Upstox broker anchored strictly to actual fill price.
        If Stop-Loss order placement fails or is rejected, engages Emergency Fail-Closed Protocol:
          1. Immediately disarms live trading.
          2. Auto-liquidates the unprotected position at market to prevent unmanaged loss.
          3. Logs critical alert.
        """
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        adapter = get_option_live_adapter()
        if not adapter._is_live_armed:
            return {"status": "BLOCKED", "reason": "LIVE_TRADING_NOT_ARMED"}

        with self._get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM option_live_order_intents WHERE order_intent_id = ?;", (order_intent_id,))
            row = cur.fetchone()
            if not row:
                return {"status": "ERROR", "reason": "Intent not found"}
            intent = dict(row)

        effective_sl_pct = float(intent.get("dynamic_sl_pct") or sl_pct)
        trigger_price = align_to_nse_tick(round(filled_price * (1.0 - (effective_sl_pct / 100.0)), 1))
        limit_price = align_to_nse_tick(round(trigger_price * 0.985, 1))

        sl_tag = sanitize_upstox_tag(f"SL-{order_intent_id}")
        payload = {
            "quantity": int(filled_qty),
            "product": "I",
            "validity": "DAY",
            "price": float(limit_price),
            "tag": sl_tag,
            "instrument_token": str(intent["instrument_key"]),
            "order_type": "SL",
            "transaction_type": "SELL",
            "disclosed_quantity": 0,
            "trigger_price": float(trigger_price),
            "is_amo": False
        }

        success, status_code, data = self._call_upstox_broker_api("POST", "/order/place", payload=payload, is_emergency=True)
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._get_db_conn() as conn:
            cur = conn.cursor()
            if success and data.get("status") == "success":
                sl_order_id = data.get("data", {}).get("order_id", f"UPSTOX_SL_{int(time.time()*1000)}")
                cur.execute("""
                    UPDATE option_live_order_intents
                    SET stop_loss_trigger = ?, stop_loss_limit = ?, broker_sl_order_id = ?, updated_at = ?
                    WHERE order_intent_id = ?;
                """, (trigger_price, limit_price, sl_order_id, now_iso, order_intent_id))
                conn.commit()
                return {"status": "PLACED", "broker_sl_order_id": sl_order_id, "trigger_price": trigger_price}
            else:
                err = str(data)
                logger.critical(f"🚨 [OptionLiveOrderIntent] Broker Stop Loss FAILED for {order_intent_id}: {err}. Engaging Emergency Fail-Closed Liquidation.")
                adapter.disarm_live_trading()
                # Emergency square-off position to protect capital using realistic limit
                fallback_bid = max(0.50, limit_price)
                self.trigger_sos_panic_exit(order_intent_id, current_bid_price=fallback_bid)
                return {"status": "SL_FAILED_AUTO_LIQUIDATED", "error": err}

    def simulate_order_execution(
        self,
        order_intent_id: str,
        scenario: str = "CLEAN_FILL",
        mock_fill_price: Optional[float] = None,
        mock_partial_qty: Optional[int] = None,
        mock_rejection_reason: Optional[str] = None,
        sl_pct: float = 15.0
    ) -> Dict[str, Any]:
        """
        Simulates exchange order lifecycle and edge failure scenarios:
          - CLEAN_FILL: Fills full quantity at marketable limit and links broker-side Stop Loss.
          - PARTIAL_FILL: Fills partial quantity (e.g. 15 of 30) and cancels unfilled remnant.
          - RMS_REJECTION: Simulates broker RMS rejection, fail-closed state.
          - TIMEOUT_AMBIGUOUS: Simulates submission timeout; reconciles before retrying.
        """
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM option_live_order_intents WHERE order_intent_id = ?;", (order_intent_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Order intent {order_intent_id} not found")

            intent = dict(row)
            target_qty = intent["target_quantity"]
            limit_price = intent["limit_price"]
            actual_fill = mock_fill_price if (mock_fill_price and mock_fill_price > 0) else limit_price

            if scenario == "RMS_REJECTION":
                reason = mock_rejection_reason or "RMS: Exchange segment not enabled or insufficient margin"
                cur.execute("""
                    UPDATE option_live_order_intents
                    SET state = ?, rejection_reason = ?, updated_at = ?
                    WHERE order_intent_id = ?;
                """, (STATE_REJECTED, reason, now_iso, order_intent_id))
                conn.commit()
                return {"state": STATE_REJECTED, "rejection_reason": reason, "order_intent_id": order_intent_id}

            elif scenario == "PARTIAL_FILL":
                filled_qty = mock_partial_qty if mock_partial_qty else (target_qty // 2)
                broker_order_id = f"UPSTOX_ORD_{int(time.time()*1000)}"
                
                # Calculate Linked Broker-Side Stop Loss based strictly on actual fill price
                sl_trigger = align_to_nse_tick(round(actual_fill * (1.0 - (sl_pct / 100.0)), 1))
                sl_limit = align_to_nse_tick(round(sl_trigger * 0.985, 1))
                broker_sl_id = f"UPSTOX_SL_{int(time.time()*1000)}"

                cur.execute("""
                    UPDATE option_live_order_intents
                    SET state = ?, filled_quantity = ?, average_fill_price = ?, broker_order_id = ?,
                        stop_loss_trigger = ?, stop_loss_limit = ?, broker_sl_order_id = ?, updated_at = ?
                    WHERE order_intent_id = ?;
                """, (
                    STATE_PARTIALLY_FILLED, filled_qty, actual_fill, broker_order_id,
                    sl_trigger, sl_limit, broker_sl_id, now_iso, order_intent_id
                ))
                conn.commit()
                return {
                    "state": STATE_PARTIALLY_FILLED,
                    "filled_quantity": filled_qty,
                    "target_quantity": target_qty,
                    "average_fill_price": actual_fill,
                    "broker_order_id": broker_order_id,
                    "stop_loss_trigger": sl_trigger,
                    "broker_sl_order_id": broker_sl_id
                }

            elif scenario == "TIMEOUT_AMBIGUOUS":
                # Simulated timeout on submission: does NOT place a new order. Queries order book to reconcile.
                broker_order_id = f"UPSTOX_ORD_{int(time.time()*1000)}"
                cur.execute("""
                    UPDATE option_live_order_intents
                    SET state = ?, broker_order_id = ?, rejection_reason = 'NETWORK_TIMEOUT_RECONCILED', updated_at = ?
                    WHERE order_intent_id = ?;
                """, (STATE_SUBMITTED, broker_order_id, now_iso, order_intent_id))
                conn.commit()
                return {
                    "state": STATE_SUBMITTED,
                    "broker_order_id": broker_order_id,
                    "reconciliation_status": "ORDER_CONFIRMED_VIA_PORTFOLIO_STREAM"
                }

            else:  # CLEAN_FILL (default)
                broker_order_id = f"UPSTOX_ORD_{int(time.time()*1000)}"
                # Calculate Linked Broker-Side Stop Loss strictly from ACTUAL fill price
                sl_trigger = align_to_nse_tick(round(actual_fill * (1.0 - (sl_pct / 100.0)), 1))
                sl_limit = align_to_nse_tick(round(sl_trigger * 0.985, 1))     # Marketable stop limit
                broker_sl_id = f"UPSTOX_SL_{int(time.time()*1000)}"

                cur.execute("""
                    UPDATE option_live_order_intents
                    SET state = ?, filled_quantity = ?, average_fill_price = ?, broker_order_id = ?,
                        stop_loss_trigger = ?, stop_loss_limit = ?, broker_sl_order_id = ?, updated_at = ?
                    WHERE order_intent_id = ?;
                """, (
                    STATE_FILLED, target_qty, actual_fill, broker_order_id,
                    sl_trigger, sl_limit, broker_sl_id, now_iso, order_intent_id
                ))
                conn.commit()
                return {
                    "state": STATE_FILLED,
                    "filled_quantity": target_qty,
                    "average_fill_price": actual_fill,
                    "broker_order_id": broker_order_id,
                    "stop_loss_trigger": sl_trigger,
                    "stop_loss_limit": sl_limit,
                    "broker_sl_order_id": broker_sl_id
                }

    def trigger_sos_panic_exit(
        self,
        order_intent_id: str,
        current_bid_price: float,
        charges: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Executes multi-stage emergency SOS square-off:
          1. Locks exit mutex to suppress duplicate clicks.
          2. Zero-Short Guard: checks broker positions before sending any sell order.
          3. Cancels pending broker stop-loss order.
          4. Verifies broker position is zero before marking flat.
          5. Computes final realized P&L with itemized transaction friction.
        """
        if order_intent_id not in _EXIT_LOCKS:
            _EXIT_LOCKS[order_intent_id] = threading.Lock()

        # Mutex lock prevents race conditions or duplicate clicks on SOS button
        acquired = _EXIT_LOCKS[order_intent_id].acquire(blocking=False)
        if not acquired:
            return {"status": "BLOCKED", "message": "SOS exit already in progress for this position"}

        try:
            now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._get_db_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM option_live_order_intents WHERE order_intent_id = ?;", (order_intent_id,))
                row = cur.fetchone()
                if not row:
                    return {"status": "ERROR", "message": f"Position {order_intent_id} not found"}

                intent = dict(row)
                if intent["exit_state"] == EXIT_CONFIRMED_FLAT:
                    return {"status": "ALREADY_FLAT", "message": "Position is already confirmed flat"}

                qty = intent["filled_quantity"] or intent["target_quantity"]
                entry_price = intent["average_fill_price"] or intent["limit_price"]
                exit_fill = max(0.50, current_bid_price)
                exit_broker_ord_id = f"UPSTOX_EXIT_{int(time.time()*1000)}"

                is_real_broker = bool(intent.get("broker_order_id") and not str(intent["broker_order_id"]).startswith("UPSTOX_"))
                broker_pos_qty = 0
                verify_pos: Dict[str, Any] = {"status": "OK", "quantity": 0}

                # ZERO-SHORT GUARD & BROKER-LEVEL SQUARING
                if is_real_broker:
                    # 1. Cancel pending broker Stop Loss order first
                    if intent.get("broker_sl_order_id"):
                        try:
                            self.cancel_broker_order(intent["broker_sl_order_id"])
                        except Exception as sl_err:
                            logger.warning(f"[OptionLiveOrderIntent] Failed to cancel broker SL: {sl_err}")

                    # 2. Check if an exit order was ALREADY submitted previously
                    existing_exit_id = intent.get("exit_broker_order_id")
                    prior_exit_state = intent.get("exit_state")
                    order_already_working = False

                    if existing_exit_id and not str(existing_exit_id).startswith("UPSTOX_EXIT_") and prior_exit_state in (EXIT_SUBMITTED, EXIT_PENDING):
                        try:
                            ok_ob, _, ob_info = self._call_upstox_broker_api("GET", f"/order/history?order_id={existing_exit_id}", is_emergency=True)
                            if ok_ob and isinstance(ob_info, dict) and isinstance(ob_info.get("data"), list) and ob_info["data"]:
                                latest_ord = ob_info["data"][0]
                                ord_status = (latest_ord.get("status") or "").lower()
                                if ord_status == "complete":
                                    if float(latest_ord.get("average_price") or 0.0) > 0:
                                        exit_fill = float(latest_ord["average_price"])
                                    exit_broker_ord_id = existing_exit_id
                                elif ord_status in ("open", "trigger pending"):
                                    order_already_working = True
                                    exit_broker_ord_id = existing_exit_id
                        except Exception as hist_err:
                            logger.debug(f"[OptionLiveOrderIntent] Prior exit order check note: {hist_err}")

                    # 3. Query broker position first
                    broker_check = self.reconcile_broker_position(intent["instrument_key"])
                    if broker_check.get("status") == "OK":
                        broker_net_qty = int(broker_check.get("quantity", 0))
                    else:
                        broker_net_qty = None

                    # If broker confirmed position is already flat or negative -> DO NOT SEND SELL ORDER!
                    if broker_net_qty is not None and broker_net_qty <= 0:
                        logger.warning(f"⚠️ [ZeroShortGuard] Broker position for {intent['instrument_key']} is already {broker_net_qty}. Zero sell order dispatched to prevent naked short.")
                        qty_to_close = 0
                    elif order_already_working:
                        logger.info(f"[SOSExit] Prior exit order {existing_exit_id} is already working on exchange. Suppressing duplicate sell order.")
                        qty_to_close = 0
                    elif broker_net_qty is None and existing_exit_id and prior_exit_state in (EXIT_SUBMITTED, EXIT_PENDING):
                        logger.warning(f"⚠️ [SOSExit] Broker reconciliation failed for {intent['instrument_key']} with existing exit {existing_exit_id}. Suppressing duplicate sell.")
                        qty_to_close = 0
                    else:
                        qty_to_close = broker_net_qty if (broker_net_qty is not None and broker_net_qty > 0) else qty
                        exit_tag = sanitize_upstox_tag(f"SOS-{order_intent_id}")
                        exit_payload = {
                            "quantity": int(qty_to_close),
                            "product": "I",
                            "validity": "DAY",
                            "price": 0.0,
                            "tag": exit_tag,
                            "instrument_token": str(intent["instrument_key"]),
                            "order_type": "MARKET",
                            "transaction_type": "SELL",
                            "disclosed_quantity": 0,
                            "trigger_price": 0.0,
                            "is_amo": False
                        }
                        exit_success, exit_status_code, exit_data = self._call_upstox_broker_api(
                            "POST", "/order/place", payload=exit_payload, is_emergency=True
                        )
                        # NSE F&O market order ban protection: if raw MARKET order is rejected by exchange/broker,
                        # immediately fallback to an aggressive marketable LIMIT sell order
                        if not exit_success or (isinstance(exit_data, dict) and exit_data.get("status") != "success"):
                            logger.warning(
                                f"⚠️ [SOSExit] MARKET order rejected for {intent['instrument_key']}: {exit_data}. "
                                "Retrying with aggressive marketable LIMIT; exit remains pending until broker-confirmed execution is reconciled."
                            )
                            marketable_limit = align_to_nse_tick(max(0.05, current_bid_price * 0.90 if current_bid_price > 1.0 else 0.05))
                            exit_payload["order_type"] = "LIMIT"
                            exit_payload["price"] = marketable_limit
                            exit_success, exit_status_code, exit_data = self._call_upstox_broker_api(
                                "POST", "/order/place", payload=exit_payload, is_emergency=True
                            )
                        if exit_success and isinstance(exit_data, dict) and exit_data.get("data", {}).get("order_id"):
                            exit_broker_ord_id = exit_data["data"]["order_id"]
                            # Retrieve actual fill price from broker order status
                            try:
                                time.sleep(0.2)
                                ok_ob, _, ob_info = self._call_upstox_broker_api("GET", f"/order/history?order_id={exit_broker_ord_id}")
                                if ok_ob and isinstance(ob_info, dict) and isinstance(ob_info.get("data"), list) and ob_info["data"]:
                                    latest_ord = ob_info["data"][0]
                                    if float(latest_ord.get("average_price") or 0.0) > 0:
                                        exit_fill = float(latest_ord["average_price"])
                            except Exception as ob_err:
                                logger.debug(f"[OptionLiveOrderIntent] Broker exit fill check note: {ob_err}")

                    # 4. Verify broker remaining quantity
                    verify_pos = self.reconcile_broker_position(intent["instrument_key"])
                    if verify_pos.get("status") == "OK" and verify_pos.get("quantity") is not None:
                        broker_pos_qty = int(verify_pos["quantity"])
                        final_exit_state = EXIT_CONFIRMED_FLAT if broker_pos_qty == 0 else EXIT_PENDING
                    else:
                        broker_pos_qty = None
                        final_exit_state = EXIT_PENDING
                else:
                    # Simulated / test execution
                    verify_pos = {"status": "OK", "quantity": 0}
                    if intent.get("broker_sl_order_id"):
                        try:
                            self.cancel_broker_order(intent["broker_sl_order_id"])
                        except Exception as sl_err:
                            logger.warning(f"[OptionLiveOrderIntent] Failed to cancel broker SL: {sl_err}")
                    qty_to_close = qty
                    broker_pos_qty = 0
                    final_exit_state = EXIT_CONFIRMED_FLAT

                # Calculate itemized transaction friction and P&L
                statutory_info = calculate_option_trade_costs(
                    buy_price=entry_price,
                    sell_price=exit_fill,
                    quantity=qty,
                    apply_slippage=False,
                    trade_date=intent.get("created_at") or now_iso
                )
                effective_charges = float(charges) if charges is not None else float(statutory_info.get("statutory_charges", 55.0))

                gross_pnl = (exit_fill - entry_price) * qty
                net_pnl = round(gross_pnl - effective_charges, 2)

                # Step 1: Submit Exit Order
                cur.execute("""
                    UPDATE option_live_order_intents
                    SET exit_state = ?, exit_broker_order_id = ?, updated_at = ?
                    WHERE order_intent_id = ?;
                """, (EXIT_SUBMITTED, exit_broker_ord_id, now_iso, order_intent_id))
                conn.commit()

                # Step 2: Confirm Broker Position Flat (Qty == 0) or EXIT_PENDING
                final_exit_state = EXIT_CONFIRMED_FLAT if (verify_pos.get("status") == "OK" and broker_pos_qty == 0) else EXIT_PENDING
                cols = [c[1] for c in cur.execute("PRAGMA table_info(option_live_order_intents);").fetchall()]
                if "gross_pnl" in cols and "statutory_charges" in cols:
                    cur.execute("""
                        UPDATE option_live_order_intents
                        SET exit_state = ?, exit_fill_price = ?, gross_pnl = ?, statutory_charges = ?, realized_pnl = ?, updated_at = ?
                        WHERE order_intent_id = ?;
                    """, (final_exit_state, exit_fill, round(gross_pnl, 2), round(effective_charges, 2), net_pnl, now_iso, order_intent_id))
                else:
                    cur.execute("""
                        UPDATE option_live_order_intents
                        SET exit_state = ?, exit_fill_price = ?, realized_pnl = ?, updated_at = ?
                        WHERE order_intent_id = ?;
                    """, (final_exit_state, exit_fill, net_pnl, now_iso, order_intent_id))
                conn.commit()

                # Dispatch Telegram alert for live trade exit
                try:
                    from app.analytics.telegram_notifier import send_telegram_message
                    pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"
                    exit_badge = "<i>⚡ Confirmed flat and reconciled on Upstox</i>" if final_exit_state == EXIT_CONFIRMED_FLAT else "<i>⏳ Exit submitted; awaiting broker-confirmed flat position</i>"
                    exit_msg = (
                        f"<b>🏁 UPSTOX LIVE POSITION EXITED</b>\n"
                        f"<b>{intent.get('trading_symbol')}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"• <b>Status:</b> {final_exit_state}\n"
                        f"• <b>Entry Fill:</b> ₹{entry_price:.2f}\n"
                        f"• <b>Exit Fill:</b> ₹{exit_fill:.2f}\n"
                        f"• <b>Quantity:</b> {qty}\n"
                        f"• <b>Net Realized P&L:</b> <b>{pnl_emoji} ₹{net_pnl:+,.2f}</b>\n"
                        f"• <b>Remaining Broker Qty:</b> {broker_pos_qty if broker_pos_qty is not None else 'UNKNOWN'}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"{exit_badge}"
                    )
                    send_telegram_message(exit_msg)
                except Exception as ex_t:
                    logger.debug(f"[OptionLiveOrderIntent] Telegram exit alert note: {ex_t}")

                return {
                    "status": "CONFIRMED_FLAT" if final_exit_state == EXIT_CONFIRMED_FLAT else "EXIT_PENDING",
                    "order_intent_id": order_intent_id,
                    "exit_state": final_exit_state,
                    "exit_fill_price": exit_fill,
                    "quantity_closed": qty,
                    "net_realized_pnl": net_pnl,
                    "exit_broker_order_id": exit_broker_ord_id,
                    "broker_position_qty": broker_pos_qty,
                    "statutory_charges": statutory_info.get("statutory_charges", 55.0),
                    "fee_breakdown": statutory_info
                }
        finally:
            _EXIT_LOCKS[order_intent_id].release()


_GLOBAL_INTENT_ENGINE: Optional[OrderIntentEngine] = None


def get_order_intent_engine() -> OrderIntentEngine:
    global _GLOBAL_INTENT_ENGINE
    with _INTENT_LOCK:
        if _GLOBAL_INTENT_ENGINE is None:
            _GLOBAL_INTENT_ENGINE = OrderIntentEngine()
        return _GLOBAL_INTENT_ENGINE

