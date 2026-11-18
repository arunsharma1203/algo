"""
Layer 5 — Funded-Cash Arena Execution, Smart Portfolio Manager & Upstox Live Bridge
===================================================================================
Enforces all institutional execution, capital-fencing, and risk invariants:
  1. Hard-locked `SIMULATION_MODE = True` (`_is_live_armed = False`) on every boot/reload (`AGENTS.md` Rule 2).
  2. Sole human operator arm/disarm authority via 4-digit confirmation PIN (`AGENTS.md` Rule 6).
  3. Hard `1.0x` Cash-Only (`0x` leverage) lock on all Book-1 `CNC` delivery positions & `BOOK1_SWING_CNC_ONLY` mode;
     leveraged `MIS` buying power applies exclusively to Book-2 intraday positions with stop-loss dollar risk caps.
  4. Dual-Ledger Session Isolation (`PAPER_SHADOW` vs `LIVE_UPSTOX`), Option Sniffer Margin Firewall,
     Time-Weighted `Unit NAV` (`100.0` base) mid-cycle top-up accounting, and 4-Gate Smart PMS Inflow Deployment
     (sector-capped, bounded inverse-volatility sizing, zero averaging down on losers).
  5. Live Upstox HFT order routing (collared marketable `LIMIT`, `<=20`-char unique tags), `/v2/order/cancel`,
     `/v2/order/retrieve-all` real fill reconciliation, SEBI `80%` T+0 `CNC` sale-proceeds haircut,
     `EXIT_RETRY_PENDING` fail-closed exit verification, Day-2 re-arm cash+invested accounting,
     and Intra-Cycle Bought-Share Breakdown + Live FinBERT Regulatory Shock Sentinel.
"""

import json
import logging
import math
import os
import random
import re
import sqlite3
import statistics
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.consensus_v3 import ENGINE_VERSION, PROTOCOL_ID, resolve_v3_db_path as get_db_path
from app.analytics.consensus_v3.baseline_engine import resolve_fno_sector
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

MARGIN_MODE_CASH_1X = "CASH_1X"
MARGIN_MODE_MIS_2X = "MIS_2X"
MARGIN_MODE_MIS_3X = "MIS_3X"
MARGIN_MODE_UPSTOX_MAX_5X = "UPSTOX_VAR_ELM_MAX_5X"
VALID_MARGIN_MODES = (
    MARGIN_MODE_CASH_1X,
    MARGIN_MODE_MIS_2X,
    MARGIN_MODE_MIS_3X,
    MARGIN_MODE_UPSTOX_MAX_5X,
)
MARGIN_MODE_CAP_MAP = {
    MARGIN_MODE_CASH_1X: 1.0,
    MARGIN_MODE_MIS_2X: 2.0,
    MARGIN_MODE_MIS_3X: 3.0,
    MARGIN_MODE_UPSTOX_MAX_5X: 5.0,
}

TOPUP_MODE_AUTO_4GATE = "AUTO_4GATE_PMS"
TOPUP_MODE_HOLD_20D = "HOLD_FOR_20D_REBALANCE"
VALID_TOPUP_MODES = (
    TOPUP_MODE_AUTO_4GATE,
    TOPUP_MODE_HOLD_20D,
)

MIN_FUNDED_CAPITAL_INR = 10000.0
DEFAULT_FUNDED_CAPITAL_INR = 400000.0
DAILY_LOSS_HALT_FRACTION = 0.0125  # -1.25% daily realized loss halt (-₹5,000 on ₹4,00,000; -₹625 on ₹50,000)
SEBI_SAME_DAY_CNC_SALE_CREDIT_FRACTION = 0.80  # SEBI 80% T+0 credit on CNC delivery sales (20% blocked until T+1)
BOOK2_MAX_L1_SPREAD_BPS = 15.0


def _compute_book2_slot_cap_inr(funded_inr: float, b2_max: int) -> float:
    """
    Computes concentrated 1x-notional Book 2 slot cap (`<= funded_inr / b2_max`):
      - ₹50,000 (1 slot) -> ₹48,000
      - ₹90,000 (2 slots) -> ₹45,000
      - ₹1,00,000 (2 slots) -> ₹47,500 (leaves 5% buffer so 2x ~21% VaR+ELM fits cleanly inside 20% cash reserve)
      - >=₹1,40,000 -> `funded_inr / b2_max`
    """
    if b2_max <= 2 and funded_inr <= 120000.0:
        per_slot = 47500.0 if b2_max == 2 else 48000.0
        return round(min(per_slot, funded_inr / max(1, b2_max)), 2)
    return round(funded_inr / max(1, b2_max), 2)


def compute_optimal_slot_counts(
    funded_capital_inr: float,
    mis_leverage_cap: float = 1.0,
    execution_mode: str = MODE_DUAL_FUNDED_ARENA,
    intraday_cash_reserve_pct: Optional[float] = None,
) -> Tuple[int, int]:
    """
    Auto-scales Book 1 (`1.0x CNC`) from `3 -> 8` diversified slots as equity capital grows
    (`₹50k CNC-only -> 3 slots`, `₹75k -> 4`, `₹1L -> 5`, `₹1.25L -> 6`, `>=₹1.6L -> 8`),
    while concentrating Book 2 (`1.0x`-notional MIS) into `1–2 >=₹45,000` slots on `₹50k–₹1L`
    accounts so every trade clears Upstox's ₹20 flat brokerage cap efficiently.
    """
    cap = max(MIN_FUNDED_CAPITAL_INR, float(funded_capital_inr))
    mode = str(execution_mode or MODE_DUAL_FUNDED_ARENA).upper().strip()
    _ = 1.0 if mode == MODE_BOOK1_SWING_CNC else max(1.0, min(5.0, float(mis_leverage_cap)))

    if mode == MODE_BOOK1_SWING_CNC:
        eff_res_pct = 0.0
    elif mode == MODE_BOOK2_INTRADAY_MIS:
        eff_res_pct = 100.0
    else:
        eff_res_pct = 20.0 if intraday_cash_reserve_pct is None else max(0.0, min(50.0, float(intraday_cash_reserve_pct)))

    b1_budget = cap if mode == MODE_BOOK2_INTRADAY_MIS else cap * (1.0 - eff_res_pct / 100.0)
    if b1_budget < 28000.0:
        b1_slots = 1
    elif b1_budget < 45000.0:
        b1_slots = 2
    elif b1_budget < 72000.0:
        b1_slots = 3
    elif b1_budget < 95000.0:
        b1_slots = 4
    elif b1_budget < 115000.0:
        b1_slots = 5
    elif b1_budget < 155000.0:
        b1_slots = 6
    else:
        b1_slots = 8

    # Book 2 MIS total notional is capped at <= 1.0x funded equity (`cap`), concentrating into
    # 1-2 >=₹45,000 slots on ₹50k-₹1L accounts.
    if cap < 90000.0:
        b2_slots = 1
    elif cap < 140000.0:
        b2_slots = 2
    elif cap < 190000.0:
        b2_slots = 3
    else:
        b2_slots = 4
    return b1_slots, b2_slots


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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS consensus_v3_capital_flows (
                    flow_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    flow_type TEXT NOT NULL,
                    amount_inr REAL NOT NULL,
                    unit_nav_at_flow REAL NOT NULL,
                    units_delta REAL NOT NULL,
                    deploy_mode TEXT,
                    note TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cv3_pending_status ON consensus_v3_pending_orders(session_id, status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cv3_pos_status ON consensus_v3_positions(session_id, status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cv3_signals_time ON consensus_v3_signals(evaluated_at DESC);")

            # Additive non-destructive schema migrations
            additive_cols = [
                ("consensus_v3_arena_state", "intraday_mis_leverage", "REAL NOT NULL DEFAULT 1.0"),
                ("consensus_v3_arena_state", "margin_mode", "TEXT NOT NULL DEFAULT 'CASH_1X'"),
                ("consensus_v3_arena_state", "max_risk_per_trade_pct", "REAL NOT NULL DEFAULT 0.50"),
                ("consensus_v3_arena_state", "nav_units", "REAL NOT NULL DEFAULT 4000.0"),
                ("consensus_v3_arena_state", "unit_nav", "REAL NOT NULL DEFAULT 100.0"),
                ("consensus_v3_arena_state", "net_contributions_inr", "REAL NOT NULL DEFAULT 400000.0"),
                ("consensus_v3_arena_state", "topup_deploy_mode", "TEXT NOT NULL DEFAULT 'AUTO_4GATE_PMS'"),
                ("consensus_v3_arena_state", "intraday_cash_reserve_pct", "REAL NOT NULL DEFAULT 20.0"),
                ("consensus_v3_arena_state", "option_sniffer_reserve_inr", "REAL NOT NULL DEFAULT 0.0"),
                ("consensus_v3_arena_state", "ddpi_confirmed", "INTEGER NOT NULL DEFAULT 0"),
                ("consensus_v3_pending_orders", "exposure_notional_inr", "REAL"),
                ("consensus_v3_pending_orders", "effective_leverage", "REAL NOT NULL DEFAULT 1.0"),
                ("consensus_v3_pending_orders", "margin_source", "TEXT NOT NULL DEFAULT 'CASH_1X_EXACT'"),
                ("consensus_v3_positions", "margin_used_inr", "REAL"),
                ("consensus_v3_positions", "effective_leverage", "REAL NOT NULL DEFAULT 1.0"),
                ("consensus_v3_positions", "margin_source", "TEXT NOT NULL DEFAULT 'CASH_1X_EXACT'"),
            ]
            for tbl_name, col_name, col_def in additive_cols:
                try:
                    cur.execute(f"ALTER TABLE {tbl_name} ADD COLUMN {col_name} {col_def}")
                except sqlite3.OperationalError:
                    pass
            conn.commit()


class ConsensusArenaExecutionBridge:
    """
    Stateful Funded-Cash Execution Engine + Isolated Human-Interlocked Upstox Live Bridge.
    Boots strictly DISARMED (`simulation_mode = True`) on every process start!
    """

    def __init__(self, db_path: Optional[str] = None, session_id: str = "default_v3_arena"):
        self.db_path = db_path or get_db_path()
        self.shadow_session_id = session_id
        self.live_session_id = "live_v3_upstox" if session_id == "default_v3_arena" else f"{session_id}__live_upstox"
        self._lock = threading.RLock()
        self._margin_cache: Dict[str, tuple] = {}
        self._live_orders_cache: Tuple[float, Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]] = (0.0, {}, {})
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
    def session_id(self) -> str:
        """
        Dynamic active ledger pointer:
          - Points to `live_session_id` whenever Live Bridge is armed OR real `LIVE_UPSTOX` positions/orders
            remain open (so Day-2 disarmed monitoring & UI visibility never orphan real holdings!).
          - Points to `shadow_session_id` in normal Paper Shadow operation.
        """
        if not self.simulation_mode:
            return self.live_session_id
        try:
            with self._conn() as conn:
                if self._has_open_live_exposure(conn):
                    return self.live_session_id
        except Exception:
            pass
        return self.shadow_session_id

    @property
    def current_pin(self) -> str:
        return self._current_pin

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=12.0)
        c.row_factory = sqlite3.Row
        return c

    def _risk_date_key(self, sid: str, today_s: str) -> str:
        return today_s if sid == self.shadow_session_id else f"{today_s}__LIVE"

    def _has_open_live_exposure(self, conn: sqlite3.Connection) -> bool:
        p_cnt = conn.execute(
            """
            SELECT COUNT(*) FROM consensus_v3_positions
            WHERE (session_id = ? OR execution_env = 'LIVE_UPSTOX')
              AND execution_env = 'LIVE_UPSTOX'
              AND status IN ('OPEN', 'EXIT_RETRY_PENDING')
            """,
            (self.live_session_id,),
        ).fetchone()[0]
        if p_cnt > 0:
            return True
        o_cnt = conn.execute(
            """
            SELECT COUNT(*) FROM consensus_v3_pending_orders
            WHERE (session_id = ? OR execution_env = 'LIVE_UPSTOX')
              AND execution_env = 'LIVE_UPSTOX'
              AND status = 'PENDING_RESERVED'
            """,
            (self.live_session_id,),
        ).fetchone()[0]
        return o_cnt > 0

    def _ensure_arena_state(self) -> None:
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            for sid in (self.shadow_session_id, self.live_session_id):
                row = conn.execute(
                    "SELECT * FROM consensus_v3_arena_state WHERE session_id = ?",
                    (sid,),
                ).fetchone()
                if not row:
                    b1_s, b2_s = compute_optimal_slot_counts(DEFAULT_FUNDED_CAPITAL_INR, 1.0, MODE_DUAL_FUNDED_ARENA, 20.0)
                    init_units = round(DEFAULT_FUNDED_CAPITAL_INR / 100.0, 6)
                    conn.execute(
                        """
                        INSERT INTO consensus_v3_arena_state (
                            session_id, funded_capital_inr, realized_pnl_inr, peak_equity_inr,
                            max_drawdown_pct, execution_mode, book1_max_slots, book2_max_slots,
                            intraday_mis_leverage, margin_mode, max_risk_per_trade_pct,
                            nav_units, unit_nav, net_contributions_inr, topup_deploy_mode,
                            intraday_cash_reserve_pct, option_sniffer_reserve_inr, ddpi_confirmed,
                            updated_at
                        ) VALUES (?, ?, 0.0, ?, 0.0, ?, ?, ?, 1.0, ?, 0.50, ?, 100.0, ?, ?, 20.0, 0.0, 0, ?)
                        """,
                        (
                            sid,
                            DEFAULT_FUNDED_CAPITAL_INR,
                            DEFAULT_FUNDED_CAPITAL_INR,
                            MODE_DUAL_FUNDED_ARENA,
                            b1_s,
                            b2_s,
                            MARGIN_MODE_CASH_1X,
                            init_units,
                            DEFAULT_FUNDED_CAPITAL_INR,
                            TOPUP_MODE_AUTO_4GATE,
                            now_s,
                        ),
                    )
                else:
                    # Heal legacy peak_equity_inr mismatch if capital was resized before without resetting peak
                    rkeys = row.keys()
                    cap_now = float(row["funded_capital_inr"])
                    real_now = float(row["realized_pnl_inr"])
                    nav_u = float(row["nav_units"]) if ("nav_units" in rkeys and row["nav_units"]) else 0.0
                    if nav_u <= 0.0 or abs(cap_now - DEFAULT_FUNDED_CAPITAL_INR) > 1.0 and abs(nav_u - 4000.0) < 1e-3:
                        u_nav = 100.0
                        new_u = round(max(1.0, cap_now) / u_nav, 6)
                        conn.execute(
                            "UPDATE consensus_v3_arena_state SET nav_units = ?, net_contributions_inr = ? WHERE session_id = ?",
                            (new_u, cap_now, sid),
                        )
                    closed_cnt = conn.execute(
                        "SELECT COUNT(*) FROM consensus_v3_positions WHERE session_id = ? AND status = 'CLOSED'",
                        (sid,),
                    ).fetchone()[0]
                    if closed_cnt == 0 and real_now == 0.0 and float(row["peak_equity_inr"]) > cap_now * 1.25:
                        conn.execute(
                            "UPDATE consensus_v3_arena_state SET peak_equity_inr = ?, max_drawdown_pct = 0.0 WHERE session_id = ?",
                            (cap_now, sid),
                        )
                    mode_now = str(row["execution_mode"] or MODE_DUAL_FUNDED_ARENA)
                    lev_now = float(row["intraday_mis_leverage"]) if ("intraday_mis_leverage" in rkeys and row["intraday_mis_leverage"]) else 1.0
                    res_pct_now = float(row["intraday_cash_reserve_pct"]) if ("intraday_cash_reserve_pct" in rkeys and row["intraday_cash_reserve_pct"] is not None) else 20.0
                    _, exp_b2 = compute_optimal_slot_counts(cap_now, lev_now, mode_now, res_pct_now)
                    if int(row["book2_max_slots"]) != exp_b2:
                        conn.execute(
                            "UPDATE consensus_v3_arena_state SET book2_max_slots = ? WHERE session_id = ?",
                            (exp_b2, sid),
                        )
            conn.commit()

    def configure_arena_capital(
        self,
        funded_capital_inr: float,
        execution_mode: str = MODE_DUAL_FUNDED_ARENA,
        reset_ledger: bool = False,
        margin_mode: Optional[str] = None,
        intraday_mis_leverage: Optional[float] = None,
        max_risk_per_trade_pct: Optional[float] = None,
        book1_max_slots: Optional[int] = None,
        intraday_cash_reserve_pct: Optional[float] = None,
        option_sniffer_reserve_inr: Optional[float] = None,
        topup_deploy_mode: Optional[str] = None,
        ddpi_confirmed: Optional[bool] = None,
    ) -> Dict[str, Any]:
        cap = max(MIN_FUNDED_CAPITAL_INR, float(funded_capital_inr))
        mode = str(execution_mode or MODE_DUAL_FUNDED_ARENA).upper().strip()
        if mode not in VALID_ARENA_MODES:
            return {"status": "ERROR", "message": f"Invalid mode '{mode}'. Choose from {VALID_ARENA_MODES}."}

        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._conn() as conn:
            has_live = self._has_open_live_exposure(conn)
            if reset_ledger and (not self.simulation_mode or has_live):
                return {
                    "status": "ERROR",
                    "code": "LIVE_POSITIONS_ACTIVE",
                    "message": "Cannot reset ledger while Live Bridge is ARMED or LIVE_UPSTOX positions/orders are active.",
                }

            active_sid = self.live_session_id if (not self.simulation_mode or has_live) else self.shadow_session_id
            cur_st = conn.execute(
                "SELECT * FROM consensus_v3_arena_state WHERE session_id = ?",
                (active_sid,),
            ).fetchone()
            cur_keys = cur_st.keys() if cur_st else []
            existing_mm = str(cur_st["margin_mode"]) if (cur_st and "margin_mode" in cur_keys and cur_st["margin_mode"]) else MARGIN_MODE_CASH_1X
            existing_lev = float(cur_st["intraday_mis_leverage"]) if (cur_st and "intraday_mis_leverage" in cur_keys and cur_st["intraday_mis_leverage"]) else 1.0
            existing_risk = float(cur_st["max_risk_per_trade_pct"]) if (cur_st and "max_risk_per_trade_pct" in cur_keys and cur_st["max_risk_per_trade_pct"]) else 0.50
            existing_res_pct = float(cur_st["intraday_cash_reserve_pct"]) if (cur_st and "intraday_cash_reserve_pct" in cur_keys and cur_st["intraday_cash_reserve_pct"] is not None) else 20.0
            existing_sniffer_res = float(cur_st["option_sniffer_reserve_inr"]) if (cur_st and "option_sniffer_reserve_inr" in cur_keys and cur_st["option_sniffer_reserve_inr"] is not None) else 0.0
            existing_topup_mode = str(cur_st["topup_deploy_mode"]) if (cur_st and "topup_deploy_mode" in cur_keys and cur_st["topup_deploy_mode"]) else TOPUP_MODE_AUTO_4GATE
            existing_ddpi = int(cur_st["ddpi_confirmed"]) if (cur_st and "ddpi_confirmed" in cur_keys and cur_st["ddpi_confirmed"] is not None) else 0

            # HARD REGULATORY RULE: CNC equity delivery NEVER permits leverage (strictly 1.0x Cash / 0x Leverage!)
            if mode == MODE_BOOK1_SWING_CNC:
                mm = MARGIN_MODE_CASH_1X
                lev = 1.0
            elif margin_mode is not None:
                mm = str(margin_mode).upper().strip()
                if mm not in VALID_MARGIN_MODES:
                    return {"status": "ERROR", "message": f"Invalid margin_mode '{mm}'. Choose from {VALID_MARGIN_MODES}."}
                lev = MARGIN_MODE_CAP_MAP[mm]
            elif intraday_mis_leverage is not None:
                lev = max(1.0, min(5.0, float(intraday_mis_leverage)))
                if lev <= 1.01:
                    mm = MARGIN_MODE_CASH_1X
                elif lev <= 2.01:
                    mm = MARGIN_MODE_MIS_2X
                elif lev <= 3.01:
                    mm = MARGIN_MODE_MIS_3X
                else:
                    mm = MARGIN_MODE_UPSTOX_MAX_5X
            else:
                mm = existing_mm
                lev = existing_lev

            if mode == MODE_BOOK1_SWING_CNC:
                res_pct = 0.0
            elif intraday_cash_reserve_pct is not None:
                res_pct = max(0.0, min(50.0, float(intraday_cash_reserve_pct)))
            else:
                res_pct = existing_res_pct if existing_res_pct > 0 else 20.0

            sniffer_res = (
                max(0.0, float(option_sniffer_reserve_inr))
                if option_sniffer_reserve_inr is not None
                else existing_sniffer_res
            )
            t_mode = str(topup_deploy_mode or existing_topup_mode).upper().strip()
            if t_mode not in VALID_TOPUP_MODES:
                t_mode = TOPUP_MODE_AUTO_4GATE
            ddpi_val = (1 if ddpi_confirmed else 0) if ddpi_confirmed is not None else existing_ddpi

            risk_pct = (
                max(0.10, min(2.00, float(max_risk_per_trade_pct)))
                if max_risk_per_trade_pct is not None
                else existing_risk
            )
            auto_b1, auto_b2 = compute_optimal_slot_counts(cap, lev, mode, res_pct)
            b1_slots = max(1, min(12, int(book1_max_slots))) if book1_max_slots is not None else auto_b1
            b2_slots = auto_b2

            if reset_ledger:
                # Strictly wipe ONLY shadow ledger (live ledger is already guarded above)
                conn.execute("DELETE FROM consensus_v3_pending_orders WHERE session_id = ?", (self.shadow_session_id,))
                conn.execute("DELETE FROM consensus_v3_positions WHERE session_id = ?", (self.shadow_session_id,))
                conn.execute("DELETE FROM consensus_v3_capital_flows WHERE session_id = ?", (self.shadow_session_id,))
                conn.execute("DELETE FROM consensus_v3_risk_ledger WHERE session_date NOT LIKE '%__LIVE'")
                init_u = round(cap / 100.0, 6)
                conn.execute(
                    """
                    UPDATE consensus_v3_arena_state
                    SET funded_capital_inr = ?, realized_pnl_inr = 0.0, peak_equity_inr = ?,
                        max_drawdown_pct = 0.0, execution_mode = ?, book1_max_slots = ?, book2_max_slots = ?,
                        intraday_mis_leverage = ?, margin_mode = ?, max_risk_per_trade_pct = ?,
                        nav_units = ?, unit_nav = 100.0, net_contributions_inr = ?,
                        topup_deploy_mode = ?, intraday_cash_reserve_pct = ?,
                        option_sniffer_reserve_inr = ?, ddpi_confirmed = ?,
                        last_book1_rebalance_date = NULL, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        cap, cap, mode, b1_slots, b2_slots, lev, mm, risk_pct,
                        init_u, cap, t_mode, res_pct, sniffer_res, ddpi_val,
                        now_s, self.shadow_session_id,
                    ),
                )
                # Also mirror wallet settings cleanly onto untouched live_session_id if it has 0 trades
                live_pos_total = conn.execute(
                    "SELECT COUNT(*) FROM consensus_v3_positions WHERE session_id = ?",
                    (self.live_session_id,),
                ).fetchone()[0]
                if live_pos_total == 0:
                    conn.execute(
                        """
                        UPDATE consensus_v3_arena_state
                        SET funded_capital_inr = ?, realized_pnl_inr = 0.0, peak_equity_inr = ?,
                            max_drawdown_pct = 0.0, execution_mode = ?, book1_max_slots = ?, book2_max_slots = ?,
                            intraday_mis_leverage = ?, margin_mode = ?, max_risk_per_trade_pct = ?,
                            nav_units = ?, unit_nav = 100.0, net_contributions_inr = ?,
                            topup_deploy_mode = ?, intraday_cash_reserve_pct = ?,
                            option_sniffer_reserve_inr = ?, ddpi_confirmed = ?,
                            last_book1_rebalance_date = NULL, updated_at = ?
                        WHERE session_id = ?
                        """,
                        (
                            cap, cap, mode, b1_slots, b2_slots, lev, mm, risk_pct,
                            init_u, cap, t_mode, res_pct, sniffer_res, ddpi_val,
                            now_s, self.live_session_id,
                        ),
                    )
            else:
                target_sids = [active_sid]
                if active_sid == self.shadow_session_id:
                    live_pos_total = conn.execute(
                        "SELECT COUNT(*) FROM consensus_v3_positions WHERE session_id = ?",
                        (self.live_session_id,),
                    ).fetchone()[0]
                    if live_pos_total == 0:
                        target_sids.append(self.live_session_id)

                for sid_item in target_sids:
                    row_s = conn.execute("SELECT * FROM consensus_v3_arena_state WHERE session_id = ?", (sid_item,)).fetchone()
                    old_cap = float(row_s["funded_capital_inr"])
                    real_p = float(row_s["realized_pnl_inr"])
                    old_u = float(row_s["nav_units"]) if ("nav_units" in row_s.keys() and row_s["nav_units"]) else round(old_cap / 100.0, 6)
                    unr_p = float(
                        conn.execute(
                            "SELECT COALESCE(SUM(unrealized_pnl_inr), 0.0) FROM consensus_v3_positions WHERE session_id = ? AND status IN ('OPEN', 'EXIT_RETRY_PENDING')",
                            (sid_item,),
                        ).fetchone()[0]
                    )
                    eq_before = old_cap + real_p + unr_p
                    cur_unav = (eq_before / old_u) if old_u > 1e-6 else 100.0
                    delta_cap = cap - old_cap
                    new_u = max(1e-4, round(old_u + (delta_cap / max(1.0, cur_unav)), 6))
                    new_eq = cap + real_p + unr_p
                    closed_cnt = conn.execute(
                        "SELECT COUNT(*) FROM consensus_v3_positions WHERE session_id = ? AND status = 'CLOSED'",
                        (sid_item,),
                    ).fetchone()[0]
                    if closed_cnt == 0 and abs(real_p) < 1e-6 and unr_p >= 0.0:
                        new_pk = max(cap, new_eq)
                        new_dd = 0.0
                    else:
                        new_pk = max(new_eq, float(row_s["peak_equity_inr"]) + delta_cap)
                        new_dd = max(0.0, ((new_pk - new_eq) / max(1.0, new_pk)) * 100.0)

                    conn.execute(
                        """
                        UPDATE consensus_v3_arena_state
                        SET funded_capital_inr = ?, peak_equity_inr = ?, max_drawdown_pct = ?,
                            execution_mode = ?, book1_max_slots = ?, book2_max_slots = ?,
                            intraday_mis_leverage = ?, margin_mode = ?, max_risk_per_trade_pct = ?,
                            nav_units = ?, unit_nav = ?, net_contributions_inr = ?,
                            topup_deploy_mode = ?, intraday_cash_reserve_pct = ?,
                            option_sniffer_reserve_inr = ?, ddpi_confirmed = ?, updated_at = ?
                        WHERE session_id = ?
                        """,
                        (
                            cap, round(new_pk, 2), round(new_dd, 2),
                            mode, b1_slots, b2_slots, lev, mm, risk_pct,
                            new_u, round(cur_unav, 4), cap,
                            t_mode, res_pct, sniffer_res, ddpi_val, now_s, sid_item,
                        ),
                    )
            conn.commit()
        return self.get_arena_accounting()

    def top_up_arena_capital(
        self,
        added_cash_inr: float,
        deploy_mode: str = TOPUP_MODE_AUTO_4GATE,
        option_sniffer_reserve_inr: Optional[float] = None,
        note: str = "USER_EQUITY_TOPUP",
    ) -> Dict[str, Any]:
        """
        Issues time-weighted `Unit NAV` shares for mid-cycle capital inflows (`added_cash_inr >= ₹1,000`)
        so return % and max drawdown % are never distorted by cash deposits, and auto-scales Book 1 slots (`3 -> 8`).
        """
        inflow = round(float(added_cash_inr or 0.0), 2)
        if inflow < 1000.0:
            return {"status": "ERROR", "message": "Top-up amount must be at least ₹1,000."}
        d_mode = str(deploy_mode or TOPUP_MODE_AUTO_4GATE).upper().strip()
        if d_mode not in VALID_TOPUP_MODES:
            d_mode = TOPUP_MODE_AUTO_4GATE

        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._conn() as conn:
            sid = self.live_session_id if (not self.simulation_mode or self._has_open_live_exposure(conn)) else self.shadow_session_id
            st = conn.execute("SELECT * FROM consensus_v3_arena_state WHERE session_id = ?", (sid,)).fetchone()
            old_cap = float(st["funded_capital_inr"])
            real_p = float(st["realized_pnl_inr"])
            old_u = float(st["nav_units"]) if ("nav_units" in st.keys() and st["nav_units"]) else round(old_cap / 100.0, 6)
            unr_p = float(
                conn.execute(
                    "SELECT COALESCE(SUM(unrealized_pnl_inr), 0.0) FROM consensus_v3_positions WHERE session_id = ? AND status IN ('OPEN', 'EXIT_RETRY_PENDING')",
                    (sid,),
                ).fetchone()[0]
            )
            eq_before = old_cap + real_p + unr_p
            cur_unav = (eq_before / old_u) if old_u > 1e-6 else 100.0
            issued_units = round(inflow / max(1.0, cur_unav), 6)
            new_u = round(old_u + issued_units, 6)
            new_cap = round(old_cap + inflow, 2)
            new_pk = round(float(st["peak_equity_inr"]) + inflow, 2)
            mode = str(st["execution_mode"])
            lev = 1.0 if mode == MODE_BOOK1_SWING_CNC else float(st["intraday_mis_leverage"] or 1.0)
            res_pct = 0.0 if mode == MODE_BOOK1_SWING_CNC else float(st["intraday_cash_reserve_pct"] if "intraday_cash_reserve_pct" in st.keys() else 20.0)
            sniffer_res = (
                max(0.0, float(option_sniffer_reserve_inr))
                if option_sniffer_reserve_inr is not None
                else float(st["option_sniffer_reserve_inr"] if "option_sniffer_reserve_inr" in st.keys() else 0.0)
            )
            b1_slots, b2_slots = compute_optimal_slot_counts(new_cap, lev, mode, res_pct)

            conn.execute(
                """
                UPDATE consensus_v3_arena_state
                SET funded_capital_inr = ?, peak_equity_inr = ?, book1_max_slots = ?, book2_max_slots = ?,
                    nav_units = ?, unit_nav = ?, net_contributions_inr = net_contributions_inr + ?,
                    topup_deploy_mode = ?, option_sniffer_reserve_inr = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (new_cap, new_pk, b1_slots, b2_slots, new_u, round(cur_unav, 4), inflow, d_mode, sniffer_res, now_s, sid),
            )
            fid = f"flow_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO consensus_v3_capital_flows (
                    flow_id, session_id, flow_type, amount_inr, unit_nav_at_flow, units_delta, deploy_mode, note, created_at
                ) VALUES (?, ?, 'TOP_UP_DEPOSIT', ?, ?, ?, ?, ?, ?)
                """,
                (fid, sid, inflow, round(cur_unav, 4), issued_units, d_mode, note, now_s),
            )
            conn.commit()

        return {
            "status": "TOPPED_UP",
            "flow_id": fid,
            "added_cash_inr": inflow,
            "unit_nav_preserved": round(cur_unav, 4),
            "issued_units": issued_units,
            "deploy_mode": d_mode,
            "accounting": self.get_arena_accounting(),
        }

    @staticmethod
    def _count_trading_days_since(
        conn: sqlite3.Connection,
        last_reb_date: str,
        end_date: str,
    ) -> int:
        """
        Counts distinct trading sessions strictly `(last_reb_date, end_date]` across BOTH
        `1d` historical bars (stopped at 2026-09-24) and live `15m` bars (`>= 2026-09-24`),
        remaining compatible with single-ticker (`1d`-only or `15m`-only) synthetic test DBs.
        """
        last_s = str(last_reb_date or "")[:10]
        end_s = str(end_date or "")[:10]
        if not last_s or not end_s or last_s >= end_s:
            return 0
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT substr(date,1,10))
            FROM ohlcv
            WHERE timeframe IN ('1d', '15m')
              AND date > ?
              AND date <= ?
            """,
            (f"{last_s} 23:59:59", f"{end_s} 23:59:59"),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    @staticmethod
    def _fetch_bulk_quotes_safe(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
        clean_list = [t for t in dict.fromkeys(tickers) if t]
        if not clean_list:
            return {}
        try:
            from app.data.market_provider import get_bulk_live_quotes_with_meta
            return get_bulk_live_quotes_with_meta(clean_list) or {}
        except Exception:
            return {}

    @staticmethod
    def _resolve_book1_exec_price(
        conn: sqlite3.Connection,
        ticker: str,
        fallback_px: float,
        today_s: str,
        now_s: str,
        quotes: Optional[Dict[str, Dict[str, Any]]] = None,
        require_live_quote: bool = False,
    ) -> Tuple[Optional[float], str]:
        """
        Resolves causal Book 1 execution/valuation price without leaking yesterday's close:
          1. Live Upstox quote from `quotes` (required & fail-closed when `require_live_quote=True`).
          2. Causal session open / completed post-close bar for `today_s` strictly `<= now_s`:
             - If `hm_now >= "15:30"`, prefer today's latest `15m` close (or `1d` close) if present.
             - Otherwise during `09:15..15:29`, use today's `09:15` `15m` bar `open` (or `1d` `open` in synthetic `1d` DBs).
          3. `fallback_px` ONLY in `PAPER_SHADOW` (`require_live_quote=False`) when neither live quote nor today's bar exists.
        """
        qmap = quotes or {}
        bare = str(ticker).replace(".NS", "").replace(".BO", "").strip().upper()
        q = qmap.get(ticker) or qmap.get(bare) or qmap.get(f"{bare}.NS")
        if q and isinstance(q.get("price"), (int, float)) and float(q["price"]) > 0.0:
            if q.get("source") != "unavailable" and q.get("is_realtime", True):
                return round_nse_eq_tick(float(q["price"])), "LIVE_UPSTOX_QUOTE"

        if require_live_quote:
            return None, "MISSING_LIVE_QUOTE"

        hm_now = now_s[11:16] if len(now_s) >= 16 else "09:15"
        if hm_now >= "09:15":
            if hm_now >= "15:30":
                row_close_15m = conn.execute(
                    "SELECT close FROM ohlcv WHERE ticker = ? AND timeframe = '15m' AND date >= ? AND date <= ? ORDER BY date DESC LIMIT 1",
                    (ticker, f"{today_s} 09:15:00", now_s),
                ).fetchone()
                if row_close_15m and float(row_close_15m[0] or 0.0) > 0.0:
                    return round_nse_eq_tick(float(row_close_15m[0])), "TODAY_15M_CLOSE"

            row_open_15m = conn.execute(
                "SELECT open FROM ohlcv WHERE ticker = ? AND timeframe = '15m' AND date >= ? AND date <= ? ORDER BY date ASC LIMIT 1",
                (ticker, f"{today_s} 09:15:00", now_s),
            ).fetchone()
            if row_open_15m and float(row_open_15m[0] or 0.0) > 0.0:
                return round_nse_eq_tick(float(row_open_15m[0])), "TODAY_0915_OPEN"

            row_1d = conn.execute(
                "SELECT open, close FROM ohlcv WHERE ticker = ? AND timeframe = '1d' AND substr(date,1,10) = ? LIMIT 1",
                (ticker, today_s),
            ).fetchone()
            if row_1d:
                px_1d = float((row_1d[1] if hm_now >= "15:30" else row_1d[0]) or 0.0)
                if px_1d > 0.0:
                    return round_nse_eq_tick(px_1d), "TODAY_1D_BAR"

        fb = round_nse_eq_tick(float(fallback_px)) if float(fallback_px or 0.0) > 0.0 else None
        return fb, "FALLBACK_REF"

    def deploy_midcycle_inflow_4gate(
        self,
        scored_snapshot: Dict[str, Any],
        arb_b1: Dict[str, Any],
        now_dt: Optional[datetime] = None,
        live_quotes: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Institutional 4-Gate Mid-Cycle Capital Deployment Engine:
          - Gate 1: `deployable_b1_cash >= ₹14,000` above `intraday_cash_reserve_pct` (keeps CDSL DP + STT friction <= 0.35%).
          - Gate 2: Macro Exposure Shield allows exposure (`target_slots >= 1`).
          - Gate 3: `>= 4` trading days left before next scheduled `20d` rebalance (prevents double-STT churn right before rebalance).
          - Gate 4: Priority 1 = unheld sector-diversified `approved_rebalance_leaders`;
                    Priority 2 = pyramid ONLY held winning leaders (`unrealized_pnl >= 0` & `qlib_pct_rank >= 0.88`) up to `slot_cap_inr`.
                    NEVER averages down on losing holdings and NEVER resets `last_book1_rebalance_date`!
        """
        ts = now_dt or datetime.now()
        now_s = ts.strftime("%Y-%m-%d %H:%M:%S")
        today_s = ts.strftime("%Y-%m-%d")
        as_of_date = str(arb_b1.get("as_of_date") or scored_snapshot.get("as_of_date") or today_s)

        with self._lock:
            acct = self.get_arena_accounting(now_dt=ts)
            if acct["execution_mode"] == MODE_BOOK2_INTRADAY_MIS:
                return {"status": "SKIPPED_MODE_BOOK2_ONLY", "opened": 0, "pyramided": 0}
            if acct.get("topup_deploy_mode") == TOPUP_MODE_HOLD_20D:
                return {"status": "HELD_FOR_SCHEDULED_20D_REBALANCE", "opened": 0, "pyramided": 0}
            if acct["circuit_breaker"]["is_session_halted"]:
                return {"status": "SKIPPED_CIRCUIT_BREAKER", "opened": 0, "pyramided": 0}

            b1_alloc_cash = float(acct["book1_deployable_free_cash_inr"])
            if b1_alloc_cash < 14000.0:
                return {
                    "status": "GATE1_BELOW_MIN_ECONOMIC_LOT",
                    "book1_deployable_free_cash_inr": b1_alloc_cash,
                    "opened": 0,
                    "pyramided": 0,
                }

            b1_max = int(acct["book1_slots"]["max"])
            shield = arb_b1.get("macro_shield") or {}
            shield_ratio = max(0.0, min(1.0, int(shield.get("target_slots", 8)) / 8.0))
            target_slots = int(round(b1_max * shield_ratio))
            if target_slots <= 0:
                return {"status": "GATE2_MACRO_BEAR_SHIELD_ACTIVE", "opened": 0, "pyramided": 0}

            sid = acct["session_id"]
            if self.simulation_mode and sid == self.live_session_id:
                return {"status": "SKIPPED_DISARMED_LIVE_SESSION", "opened": 0, "pyramided": 0}
            last_reb = acct.get("last_book1_rebalance_date")
            with self._conn() as conn:
                if last_reb:
                    tdays_elapsed = self._count_trading_days_since(conn, str(last_reb)[:10], today_s)
                    if (20 - tdays_elapsed) < 4:
                        return {
                            "status": "GATE3_NEAR_SCHEDULED_REBALANCE_HOLD",
                            "days_remaining_in_cycle": max(0, 20 - tdays_elapsed),
                            "opened": 0,
                            "pyramided": 0,
                        }

                open_b1 = conn.execute(
                    "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND book = 'BOOK1_SWING_CNC_CORE' AND status IN ('OPEN', 'EXIT_RETRY_PENDING')",
                    (sid,),
                ).fetchall()

                held_by_sym = {r["ticker"]: r for r in open_b1}
                sector_counts: Dict[str, int] = {}
                for r in open_b1:
                    sec = resolve_fno_sector(r["symbol"])
                    sector_counts[sec] = sector_counts.get(sec, 0) + 1
                max_per_sector = 1 if target_slots <= 3 else 2

                slot_cap_inr = float(acct["book1_slots"]["slot_cap_inr"])
                exec_env = "PAPER_SHADOW" if self.simulation_mode else "LIVE_UPSTOX"
                empty_slots = max(0, target_slots - len(open_b1))

                opened_records = []
                pyramided_records = []
                tickers_needed = [l["ticker"] for l in (arb_b1.get("approved_rebalance_leaders") or [])] + [r["ticker"] for r in open_b1]
                quotes = live_quotes if live_quotes is not None else self._fetch_bulk_quotes_safe(tickers_needed)

                # Priority 1: Fill empty slots with unheld sector-diversified leaders
                for leader in (arb_b1.get("approved_rebalance_leaders") or []):
                    if empty_slots <= 0 or b1_alloc_cash < 10000.0:
                        break
                    sym = leader["ticker"]
                    if sym in held_by_sym:
                        continue
                    sec = str(leader.get("sector") or resolve_fno_sector(leader["symbol"]))
                    if sec != "DIVERSIFIED_FNO" and sector_counts.get(sec, 0) >= max_per_sector:
                        continue

                    vol60 = max(18.0, min(65.0, float(leader.get("vol60_ann_pct") or 30.0)))
                    vol_mult = max(0.85, min(1.15, 30.0 / vol60))
                    adj_slot_cap = min(b1_alloc_cash, slot_cap_inr * vol_mult)
                    epx, _ = self._resolve_book1_exec_price(
                        conn, sym, float(leader["close_px"]), today_s, now_s,
                        quotes=quotes, require_live_quote=(exec_env == "LIVE_UPSTOX"),
                    )
                    if epx is None or epx <= 0.0:
                        continue
                    sh = compute_whole_shares(
                        allocatable_cash_inr=b1_alloc_cash,
                        price_inr=epx,
                        max_slot_inr=adj_slot_cap,
                        liquidity_cap_inr=slot_cap_inr * 10.0,
                    )
                    if sh < 1 or (sh * epx) < 8000.0:
                        continue

                    notional = round(sh * epx, 2)
                    pid = f"pos_b1_{today_s.replace('-', '')}_{uuid.uuid4().hex[:8]}"
                    if exec_env == "LIVE_UPSTOX":
                        r_res = self._route_upstox_equity_order(
                            order_or_pos_id=pid,
                            action="BUY",
                            product_type="CNC",
                            order_type="MARKET",
                            ticker=sym,
                            instrument_key=leader.get("instrument_key", ""),
                            shares=sh,
                            price=epx,
                        )
                        if not r_res["routed"]:
                            continue
                        if r_res.get("avg_fill_price"):
                            epx = round_nse_eq_tick(float(r_res["avg_fill_price"]))
                            notional = round(sh * epx, 2)

                    disaster_sl = round_nse_eq_tick(epx * 0.86)
                    conn.execute(
                        """
                        INSERT INTO consensus_v3_positions (
                            position_id, session_id, order_id, book, product_type, ticker, symbol,
                            instrument_key, direction, shares, entry_price, invested_notional_inr,
                            margin_used_inr, effective_leverage, margin_source,
                            stop_loss_px, scheduled_exit, status, execution_env, current_price,
                            unrealized_pnl_inr, entry_time, details_json
                        ) VALUES (?, ?, NULL, 'BOOK1_SWING_CNC_CORE', 'CNC', ?, ?, ?, 'BUY', ?, ?, ?, ?, 1.0, 'CASH_1X_EXACT', ?, '20D_SCHEDULED_REBALANCE', 'OPEN', ?, ?, 0.0, ?, ?)
                        """,
                        (
                            pid, sid, sym, leader["symbol"], leader.get("instrument_key", ""),
                            sh, epx, notional, notional, disaster_sl, exec_env, epx, now_s, json.dumps(leader),
                        ),
                    )
                    b1_alloc_cash -= notional
                    empty_slots -= 1
                    held_by_sym[sym] = True
                    sector_counts[sec] = sector_counts.get(sec, 0) + 1
                    opened_records.append({"position_id": pid, "ticker": sym, "sector": sec, "shares": sh, "entry_price": epx, "notional_inr": notional})

                # Priority 2: Pyramid ONLY held winning top-12% leaders (`qlib_pct_rank >= 0.88` & `curr_px >= entry_price`)
                if empty_slots == 0 and b1_alloc_cash >= 14000.0:
                    cand_map = {c["ticker"]: c for c in (scored_snapshot.get("candidates") or [])}
                    for pos in open_b1:
                        if b1_alloc_cash < 10000.0:
                            break
                        if pos["status"] != "OPEN":
                            continue
                        sym = pos["ticker"]
                        cinfo = cand_map.get(sym)
                        if not cinfo or not cinfo.get("sma_alignment_ok", False) or float(cinfo.get("qlib_pct_rank", 0.0)) < 0.88:
                            continue
                        epx_old = float(pos["entry_price"])
                        cpx, _ = self._resolve_book1_exec_price(
                            conn, sym, float(cinfo["close"]), today_s, now_s,
                            quotes=quotes, require_live_quote=(exec_env == "LIVE_UPSTOX"),
                        )
                        if cpx is None or cpx <= 0.0:
                            continue
                        if cpx < epx_old:
                            # HARD PMS RULE: NEVER average down on losing momentum positions!
                            continue
                        cur_notional = float(pos["shares"]) * cpx
                        room_inr = max(0.0, slot_cap_inr - cur_notional)
                        if room_inr < 8000.0:
                            continue
                        add_sh = compute_whole_shares(
                            allocatable_cash_inr=b1_alloc_cash,
                            price_inr=cpx,
                            max_slot_inr=room_inr,
                            liquidity_cap_inr=slot_cap_inr * 10.0,
                        )
                        if add_sh < 1 or (add_sh * cpx) < 8000.0:
                            continue

                        add_notional = round(add_sh * cpx, 2)
                        pyr_id = f"pyr_{pos['position_id'][-10:]}_{uuid.uuid4().hex[:4]}"
                        if exec_env == "LIVE_UPSTOX":
                            r_res = self._route_upstox_equity_order(
                                order_or_pos_id=pyr_id,
                                action="BUY",
                                product_type="CNC",
                                order_type="MARKET",
                                ticker=sym,
                                instrument_key=pos["instrument_key"] or "",
                                shares=add_sh,
                                price=cpx,
                            )
                            if not r_res["routed"]:
                                continue
                            if r_res.get("avg_fill_price"):
                                cpx = round_nse_eq_tick(float(r_res["avg_fill_price"]))
                                add_notional = round(add_sh * cpx, 2)

                        new_sh = int(pos["shares"]) + add_sh
                        new_notional = round(float(pos["invested_notional_inr"]) + add_notional, 2)
                        new_avg_epx = round(new_notional / new_sh, 2)
                        new_sl = max(float(pos["stop_loss_px"]), round_nse_eq_tick(new_avg_epx * 0.86))
                        new_unr = round((cpx - new_avg_epx) * new_sh, 2)
                        conn.execute(
                            """
                            UPDATE consensus_v3_positions
                            SET shares = ?, entry_price = ?, invested_notional_inr = ?, margin_used_inr = ?,
                                stop_loss_px = ?, current_price = ?, unrealized_pnl_inr = ?
                            WHERE position_id = ?
                            """,
                            (new_sh, new_avg_epx, new_notional, new_notional, new_sl, cpx, new_unr, pos["position_id"]),
                        )
                        b1_alloc_cash -= add_notional
                        pyramided_records.append({"position_id": pos["position_id"], "ticker": sym, "added_shares": add_sh, "fill_price": cpx, "added_notional_inr": add_notional})

                if last_reb is None and opened_records:
                    conn.execute(
                        "UPDATE consensus_v3_arena_state SET last_book1_rebalance_date = ?, updated_at = ? WHERE session_id = ?",
                        (today_s, now_s, sid),
                    )
                conn.commit()

        return {
            "status": "DEPLOYED_MIDCYCLE_4GATE" if (opened_records or pyramided_records) else "NO_QUALIFYING_SLOT_OR_PYRAMID",
            "opened": len(opened_records),
            "pyramided": len(pyramided_records),
            "opened_positions": opened_records,
            "pyramided_positions": pyramided_records,
        }

    def estimate_mis_margin_per_share(
        self,
        instrument_key: str,
        limit_px: float,
        direction: str,
        adr_pct: float,
        margin_mode: str,
        mis_leverage_cap: float,
        allow_broker_probe: bool = True,
    ) -> tuple:
        """
        Determines per-share cash margin required for Book-2 NSE `MIS` Intraday orders:
          - Queries Upstox `/v2/charges/margin` (`product="I"`) when live token is active (`UPSTOX_API_VAR_ELM`),
            otherwise applies conservative SEBI `VaR + ELM` floor (`max(20%, 14% + 3.5 * ADR%)`) explicitly tagged
            `UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN` per `.agents/rules/no-data-leakage.md` Rule 4.
          - Under `CASH_1X` (`1.0x`-notional MIS), `user_min_margin_frac = 0.20` (reserves `~20%-33%` unpledged
            wallet cash while `reserve_book2_pending_limit_orders` caps total Book-2 notional at `<= 1.0x` equity).
        """
        px = max(0.05, float(limit_px))
        cap_lev = max(1.0, min(5.0, float(mis_leverage_cap)))
        user_min_margin_frac = (1.0 / cap_lev) if cap_lev > 1.001 else 0.20
        cache_key = f"{instrument_key}_{direction}"
        now_ep = time.time()

        if allow_broker_probe and instrument_key and instrument_key.startswith("NSE_EQ|"):
            cached = self._margin_cache.get(cache_key)
            if cached and (now_ep - cached[0]) < 300.0:
                broker_frac, src = cached[1], cached[2]
                eff_frac = max(user_min_margin_frac, broker_frac)
                return round(px * eff_frac, 4), round(1.0 / eff_frac, 2), src
            try:
                import requests
                from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
                from app.data.upstox_provider import get_upstox_config, get_upstox_limiter, get_upstox_token

                cfg = get_upstox_config()
                tok = (cfg.get("algo_token") or "").strip() or get_upstox_token()
                if tok and len(tok) > 20:
                    adapter = get_option_live_adapter()
                    proxy_url = adapter.get_proxy_url()
                    try:
                        get_upstox_limiter().acquire(timeout=1.5)
                    except Exception:
                        pass
                    resp = requests.post(
                        "https://api.upstox.com/v2/charges/margin",
                        headers={
                            "Authorization": f"Bearer {tok}",
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                        },
                        json={
                            "instruments": [
                                {
                                    "instrument_key": instrument_key,
                                    "quantity": 1,
                                    "transaction_type": direction.upper(),
                                    "product": "I",
                                    "price": round_nse_eq_tick(px),
                                }
                            ]
                        },
                        proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
                        timeout=3.5,
                    )
                    if resp.status_code == 200:
                        mdata = (resp.json() or {}).get("data") or {}
                        req_m = float(mdata.get("required_margin") or mdata.get("final_margin") or 0.0)
                        if req_m > 0.0:
                            broker_frac = max(0.20, min(1.0, req_m / px))
                            self._margin_cache[cache_key] = (now_ep, broker_frac, "UPSTOX_API_VAR_ELM")
                            eff_frac = max(user_min_margin_frac, broker_frac)
                            return round(px * eff_frac, 4), round(1.0 / eff_frac, 2), "UPSTOX_API_VAR_ELM"
            except Exception:
                pass

        # Offline / fallback SEBI VaR+ELM floor (explicitly tagged per anti-hallucination rule)
        est_var_elm_frac = max(0.20, min(0.50, 0.14 + 3.5 * (max(1.0, float(adr_pct or 2.0)) / 100.0)))
        eff_frac = max(user_min_margin_frac, est_var_elm_frac)
        return round(px * eff_frac, 4), round(1.0 / eff_frac, 2), "UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN"

    def set_execution_mode(self, mode: str) -> Dict[str, Any]:
        clean = str(mode or "").upper().strip()
        if clean not in VALID_ARENA_MODES:
            return {"status": "ERROR", "message": f"Invalid execution_mode '{clean}'. Must be in {VALID_ARENA_MODES}."}
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._conn() as conn:
            for sid in (self.shadow_session_id, self.live_session_id):
                st = conn.execute(
                    "SELECT * FROM consensus_v3_arena_state WHERE session_id = ?",
                    (sid,),
                ).fetchone()
                if not st:
                    continue
                cap = float(st["funded_capital_inr"])
                if clean == MODE_BOOK1_SWING_CNC:
                    mm = MARGIN_MODE_CASH_1X
                    lev = 1.0
                    res_pct = 0.0
                else:
                    mm = str(st["margin_mode"] or MARGIN_MODE_CASH_1X)
                    lev = float(st["intraday_mis_leverage"] or 1.0)
                    cur_res = float(st["intraday_cash_reserve_pct"] if "intraday_cash_reserve_pct" in st.keys() else 20.0)
                    res_pct = cur_res if cur_res > 0.0 else 20.0
                b1_s, b2_s = compute_optimal_slot_counts(cap, lev, clean, res_pct)
                conn.execute(
                    """
                    UPDATE consensus_v3_arena_state
                    SET execution_mode = ?, margin_mode = ?, intraday_mis_leverage = ?,
                        intraday_cash_reserve_pct = ?, book1_max_slots = ?, book2_max_slots = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (clean, mm, lev, res_pct, b1_s, b2_s, now_s, sid),
                )
            conn.commit()
        acct = self.get_arena_accounting()
        return {
            "status": "UPDATED",
            "execution_mode": clean,
            "margin_mode": acct["margin_mode"],
            "intraday_mis_leverage": acct["intraday_mis_leverage"],
            "required_margin_for_mode": acct["funded_capital_inr"],
            "message": (
                f"Execution mode updated to {clean} (1.0x Cash Locked — 0x leverage mandatory for CNC)."
                if clean == MODE_BOOK1_SWING_CNC
                else f"Execution mode updated to {clean}."
            ),
        }

    def set_ddpi_confirmation(self, confirmed: bool) -> Dict[str, Any]:
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        val = 1 if confirmed else 0
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE consensus_v3_arena_state SET ddpi_confirmed = ?, updated_at = ? WHERE session_id IN (?, ?)",
                (val, now_s, self.shadow_session_id, self.live_session_id),
            )
            conn.commit()
        return {"status": "UPDATED", "ddpi_confirmed": bool(confirmed)}

    def get_arena_accounting(self, now_dt: Optional[datetime] = None, session_override: Optional[str] = None) -> Dict[str, Any]:
        ts = now_dt or datetime.now()
        today_s = ts.strftime("%Y-%m-%d")
        with self._conn() as conn:
            sid = session_override or (
                self.live_session_id if (not self.simulation_mode or self._has_open_live_exposure(conn)) else self.shadow_session_id
            )
            st = conn.execute(
                "SELECT * FROM consensus_v3_arena_state WHERE session_id = ?",
                (sid,),
            ).fetchone()
            st_keys = st.keys() if st else []
            funded = float(st["funded_capital_inr"])
            realized = float(st["realized_pnl_inr"])
            mode = str(st["execution_mode"])
            b1_max = int(st["book1_max_slots"])
            b2_max = int(st["book2_max_slots"])
            mis_lev = 1.0 if mode == MODE_BOOK1_SWING_CNC else (
                float(st["intraday_mis_leverage"]) if ("intraday_mis_leverage" in st_keys and st["intraday_mis_leverage"]) else 1.0
            )
            m_mode = MARGIN_MODE_CASH_1X if mode == MODE_BOOK1_SWING_CNC else (
                str(st["margin_mode"]) if ("margin_mode" in st_keys and st["margin_mode"]) else MARGIN_MODE_CASH_1X
            )
            risk_pct = float(st["max_risk_per_trade_pct"]) if ("max_risk_per_trade_pct" in st_keys and st["max_risk_per_trade_pct"]) else 0.50
            nav_units = float(st["nav_units"]) if ("nav_units" in st_keys and st["nav_units"]) else round(funded / 100.0, 6)
            net_contrib = float(st["net_contributions_inr"]) if ("net_contributions_inr" in st_keys and st["net_contributions_inr"]) else funded
            t_mode = str(st["topup_deploy_mode"]) if ("topup_deploy_mode" in st_keys and st["topup_deploy_mode"]) else TOPUP_MODE_AUTO_4GATE
            res_pct = 0.0 if mode == MODE_BOOK1_SWING_CNC else (
                float(st["intraday_cash_reserve_pct"]) if ("intraday_cash_reserve_pct" in st_keys and st["intraday_cash_reserve_pct"] is not None) else 20.0
            )
            sniffer_res = float(st["option_sniffer_reserve_inr"]) if ("option_sniffer_reserve_inr" in st_keys and st["option_sniffer_reserve_inr"] is not None) else 0.0
            ddpi_ok = bool(int(st["ddpi_confirmed"])) if ("ddpi_confirmed" in st_keys and st["ddpi_confirmed"] is not None) else False

            open_rows = conn.execute(
                "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND status IN ('OPEN', 'EXIT_RETRY_PENDING') ORDER BY entry_time DESC",
                (sid,),
            ).fetchall()
            pend_rows = conn.execute(
                "SELECT * FROM consensus_v3_pending_orders WHERE session_id = ? AND status = 'PENDING_RESERVED' ORDER BY created_at DESC",
                (sid,),
            ).fetchall()
            risk_row = conn.execute(
                "SELECT * FROM consensus_v3_risk_ledger WHERE session_date = ?",
                (self._risk_date_key(sid, today_s),),
            ).fetchone()

        invested_notional_inr = sum(float(r["invested_notional_inr"]) for r in open_rows)
        invested_margin_inr = sum(
            float(r["margin_used_inr"]) if ("margin_used_inr" in r.keys() and r["margin_used_inr"] is not None) else float(r["invested_notional_inr"])
            for r in open_rows
        )
        unrealized_inr = sum(float(r["unrealized_pnl_inr"]) for r in open_rows)
        pending_reserved_inr = sum(float(r["reserved_cash_inr"]) for r in pend_rows)
        pending_notional_inr = sum(
            float(r["exposure_notional_inr"]) if ("exposure_notional_inr" in r.keys() and r["exposure_notional_inr"] is not None) else round(float(r["shares"]) * float(r["limit_price"]), 2)
            for r in pend_rows
        )
        current_equity_inr = funded + realized + unrealized_inr
        unit_nav = round(current_equity_inr / max(1e-6, nav_units), 4)
        twr_return_pct = round((unit_nav / 100.0 - 1.0) * 100.0, 2)

        free_unreserved_cash_inr = max(0.0, current_equity_inr - invested_margin_inr - pending_reserved_inr)
        intraday_reserve_floor_inr = round(current_equity_inr * (res_pct / 100.0), 2) if mode == MODE_DUAL_FUNDED_ARENA else 0.0
        b1_deployable_free_cash_inr = max(0.0, round(free_unreserved_cash_inr - intraday_reserve_floor_inr, 2))
        b1_total_budget_inr = funded if mode == MODE_BOOK1_SWING_CNC else round(funded * (1.0 - res_pct / 100.0), 2)
        intraday_buying_power_inr = round(free_unreserved_cash_inr * mis_lev, 2)

        b1_open = sum(1 for r in open_rows if r["book"] == "BOOK1_SWING_CNC_CORE")
        b2_open = sum(1 for r in open_rows if r["book"] == "BOOK2_INTRADAY_MIS_SNIFFER")
        b2_pend = sum(1 for r in pend_rows if r["book"] == "BOOK2_INTRADAY_MIS_SNIFFER")
        exit_retry_cnt = sum(1 for r in open_rows if r["status"] == "EXIT_RETRY_PENDING")

        max_daily_loss_inr = -round(funded * DAILY_LOSS_HALT_FRACTION, 2)
        today_realized = float(risk_row["realized_pnl_inr"]) if risk_row else 0.0
        fills_today = int(risk_row["fills_today"]) if risk_row else 0
        cb_tripped = bool(risk_row["circuit_breaker_tripped"]) if risk_row else (today_realized <= max_daily_loss_inr)

        b2_open_notional_inr = sum(
            float(r["invested_notional_inr"]) for r in open_rows if r["book"] == "BOOK2_INTRADAY_MIS_SNIFFER"
        )
        b2_pend_notional_inr = sum(
            (
                float(r["exposure_notional_inr"])
                if ("exposure_notional_inr" in r.keys() and r["exposure_notional_inr"] is not None)
                else round(float(r["shares"]) * float(r["limit_price"]), 2)
            )
            for r in pend_rows
            if r["book"] == "BOOK2_INTRADAY_MIS_SNIFFER"
        )
        b2_total_notional_inr = round(b2_open_notional_inr + b2_pend_notional_inr, 2)
        b2_remaining_notional_cap_inr = max(0.0, round(current_equity_inr - b2_total_notional_inr, 2))
        b2_slot_cap_1x = _compute_book2_slot_cap_inr(funded, b2_max)
        return {
            "session_id": sid,
            "shadow_session_id": self.shadow_session_id,
            "live_session_id": self.live_session_id,
            "active_ledger": "LIVE_UPSTOX" if sid == self.live_session_id else "PAPER_SHADOW",
            "simulation_mode": self.simulation_mode,
            "is_live_armed": not self.simulation_mode,
            "execution_mode": mode,
            "margin_mode": m_mode,
            "cnc_leverage_locked_1x": True,
            "intraday_mis_leverage": round(mis_lev, 2),
            "max_risk_per_trade_pct": round(risk_pct, 2),
            "funded_capital_inr": round(funded, 2),
            "net_contributions_inr": round(net_contrib, 2),
            "nav_units": round(nav_units, 4),
            "unit_nav": unit_nav,
            "time_weighted_return_pct": twr_return_pct,
            "topup_deploy_mode": t_mode,
            "intraday_cash_reserve_pct": round(res_pct, 1),
            "intraday_cash_reserve_inr": intraday_reserve_floor_inr,
            "book1_deployable_free_cash_inr": b1_deployable_free_cash_inr,
            "option_sniffer_reserve_inr": round(sniffer_res, 2),
            "ddpi_confirmed": ddpi_ok,
            "exit_retry_pending_count": exit_retry_cnt,
            "current_equity_inr": round(current_equity_inr, 2),
            "realized_pnl_inr": round(realized, 2),
            "unrealized_pnl_inr": round(unrealized_inr, 2),
            "invested_notional_inr": round(invested_notional_inr, 2),
            "invested_margin_inr": round(invested_margin_inr, 2),
            "pending_reserved_cash_inr": round(pending_reserved_inr, 2),
            "pending_notional_inr": round(pending_notional_inr, 2),
            "free_unreserved_cash_inr": round(free_unreserved_cash_inr, 2),
            "intraday_mis_buying_power_inr": intraday_buying_power_inr,
            "cash_conservation_invariant_ok": (invested_margin_inr + pending_reserved_inr) <= (current_equity_inr + 1.0),
            "book1_slots": {
                "open": b1_open,
                "max": b1_max,
                "slot_cap_inr": round(b1_total_budget_inr / max(1, b1_max), 2),
            },
            "book2_slots": {
                "open": b2_open,
                "pending": b2_pend,
                "max": b2_max,
                "slot_cap_inr": b2_slot_cap_1x,
                "cash_slot_cap_inr": round(funded / max(1, b2_max), 2),
                "open_and_pending_notional_inr": b2_total_notional_inr,
                "remaining_notional_cap_inr": b2_remaining_notional_cap_inr,
                "mis_notional_within_1x_equity_ok": b2_total_notional_inr <= (current_equity_inr + 1.0),
            },
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

    def get_shadow_gate_progress(self) -> Dict[str, Any]:
        """Tracks Book-2 `15m` Sniffer prospective shadow sessions & L1 passive fill parity toward the 60-session gate."""
        with self._conn() as conn:
            sig_days = {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT substr(evaluated_at,1,10) FROM consensus_v3_signals WHERE book = 'BOOK2_INTRADAY_MIS_SNIFFER'"
                ).fetchall()
                if r[0]
            }
            ord_rows = conn.execute(
                "SELECT status FROM consensus_v3_pending_orders WHERE session_id = ? AND book = 'BOOK2_INTRADAY_MIS_SNIFFER'",
                (self.shadow_session_id,),
            ).fetchall()
            closed_b2 = conn.execute(
                "SELECT net_pnl_inr FROM consensus_v3_positions WHERE session_id = ? AND book = 'BOOK2_INTRADAY_MIS_SNIFFER' AND status = 'CLOSED'",
                (self.shadow_session_id,),
            ).fetchall()

        total_res = sum(1 for r in ord_rows if r["status"] != "PENDING_RESERVED")
        filled = sum(1 for r in ord_rows if r["status"] == "FILLED")
        sess_cnt = len(sig_days)
        return {
            "target_sessions": 60,
            "completed_sessions": sess_cnt,
            "remaining_sessions": max(0, 60 - sess_cnt),
            "progress_pct": round(min(100.0, sess_cnt / 60.0 * 100.0), 1),
            "book2_shadow_orders_resolved": total_res,
            "book2_shadow_orders_filled": filled,
            "book2_shadow_fill_rate_pct": round(filled / max(1, total_res) * 100.0, 1) if total_res else None,
            "book2_shadow_closed_trades": len(closed_b2),
            "book2_shadow_net_pnl_inr": round(sum(float(r[0]) for r in closed_b2), 2),
            "gate_ready_for_live_mis": sess_cnt >= 60,
        }

    def _is_option_sniffer_armed(self) -> Tuple[bool, str]:
        """Read-only check across OptionSnifferAdvance & Legacy OptionSniffer bridges (`0` mutation!)."""
        try:
            from app.analytics.option_sniffer_advance_live import get_advance_live_bridge
            adv = get_advance_live_bridge()
            if getattr(adv, "is_live_armed", False):
                return True, "OPTION_SNIFFER_ADVANCE_ARMED"
        except Exception:
            pass
        try:
            from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
            leg = get_option_live_adapter()
            if getattr(leg, "_is_live_armed", False):
                return True, "OPTION_SNIFFER_LEGACY_ARMED"
        except Exception:
            pass
        return False, "DISARMED"

    def _fetch_upstox_readiness(self) -> Dict[str, Any]:
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        return get_option_live_adapter().fetch_account_readiness(force_refresh=False) or {}

    def _check_option_sniffer_margin_firewall(self, order_cash_inr: float) -> Dict[str, Any]:
        """
        Ensures Consensus V3 live equity BUY/entry orders NEVER encroach on Option Sniffer's reserved
        Upstox cash margin (`option_sniffer_reserve_inr`).
        """
        acct = self.get_arena_accounting()
        sniffer_armed, sniffer_src = self._is_option_sniffer_armed()
        reserve_floor = float(acct.get("option_sniffer_reserve_inr") or 0.0)
        if sniffer_armed and reserve_floor <= 0.0:
            # Default safety floor when Option Sniffer is armed without explicit override
            reserve_floor = 90000.0

        if reserve_floor <= 0.0:
            return {"allowed": True, "reserve_floor_inr": 0.0, "sniffer_armed": sniffer_armed}

        try:
            rd = self._fetch_upstox_readiness()
            avail = rd.get("available_margin")
            if isinstance(avail, (int, float)):
                post_order_avail = float(avail) - float(order_cash_inr)
                if post_order_avail < reserve_floor:
                    return {
                        "allowed": False,
                        "sniffer_armed": sniffer_armed,
                        "sniffer_source": sniffer_src,
                        "available_margin_inr": float(avail),
                        "reserve_floor_inr": reserve_floor,
                        "reason": (
                            f"Option Sniffer Firewall blocked ₹{order_cash_inr:,.0f} equity order: "
                            f"Upstox free margin ₹{float(avail):,.0f} would drop to ₹{post_order_avail:,.0f} "
                            f"(< ₹{reserve_floor:,.0f} reserved for Option Sniffer)."
                        ),
                    }
        except Exception as err:
            if sniffer_armed:
                return {
                    "allowed": False,
                    "sniffer_armed": True,
                    "reason": f"Option Sniffer is ARMED and broker margin probe failed ({err}) — fail-closed.",
                }
        return {"allowed": True, "reserve_floor_inr": reserve_floor, "sniffer_armed": sniffer_armed}

    def sync_live_upstox_holdings(self) -> Dict[str, Any]:
        """
        Read-only reconciliation of `LIVE_UPSTOX` `CNC` holdings against Upstox `/v2/portfolio/long-term-holdings`
        and `/v2/portfolio/short-term-positions` (`NSE_EQ|` instruments only — zero touch on F&O options!).
        """
        with self._conn() as conn:
            live_rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM consensus_v3_positions
                    WHERE session_id = ? AND execution_env = 'LIVE_UPSTOX' AND status IN ('OPEN', 'EXIT_RETRY_PENDING')
                    """,
                    (self.live_session_id,),
                ).fetchall()
            ]
        if not live_rows:
            return {"status": "NO_LIVE_EQUITY_POSITIONS", "verified_count": 0, "discrepancies": []}

        try:
            import requests
            from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
            from app.data.upstox_provider import get_upstox_config, get_upstox_limiter, get_upstox_token

            cfg = get_upstox_config()
            tok = (cfg.get("algo_token") or "").strip() or get_upstox_token()
            if not tok:
                return {"status": "TOKEN_UNAVAILABLE", "verified_count": 0, "discrepancies": []}
            proxy_url = get_option_live_adapter().get_proxy_url()
            proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
            headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}

            broker_qty_by_ikey: Dict[str, int] = {}
            for url in (
                "https://api.upstox.com/v2/portfolio/long-term-holdings",
                "https://api.upstox.com/v2/portfolio/short-term-positions",
            ):
                try:
                    get_upstox_limiter().acquire(timeout=1.5)
                except Exception:
                    pass
                r = requests.get(url, headers=headers, proxies=proxies, timeout=4.5)
                if r.status_code == 200:
                    for item in (r.json() or {}).get("data") or []:
                        ik = str(item.get("instrument_token") or "")
                        if ik.startswith("NSE_EQ|"):
                            q = int(float(item.get("quantity") or 0)) + int(float(item.get("t1_quantity") or 0))
                            broker_qty_by_ikey[ik] = max(broker_qty_by_ikey.get(ik, 0), q)

            discrepancies = []
            verified = 0
            for lr in live_rows:
                ik = str(lr.get("instrument_key") or "")
                db_q = int(lr["shares"])
                br_q = broker_qty_by_ikey.get(ik, 0)
                if br_q >= db_q:
                    verified += 1
                else:
                    discrepancies.append({
                        "symbol": lr["symbol"],
                        "instrument_key": ik,
                        "db_shares": db_q,
                        "upstox_shares": br_q,
                    })
            return {"status": "SYNCED", "verified_count": verified, "discrepancies": discrepancies}
        except Exception as err:
            return {"status": "SYNC_ERROR", "error": str(err)[:160], "verified_count": 0, "discrepancies": []}

    def get_live_status(self) -> Dict[str, Any]:
        """Provides `ArmInterlockModal`-compatible status payload mirroring `OptionSnifferAdvance`."""
        live_acct = self.get_arena_accounting(session_override=self.live_session_id)
        acct = self.get_arena_accounting()
        avail_margin = None
        token_valid = False
        ws_connected = None
        blockers: List[str] = []

        try:
            rd = self._fetch_upstox_readiness()
            token_valid = bool(rd.get("token_valid"))
            avail_margin = rd.get("available_margin")
            if not token_valid:
                blockers.append("UPSTOX_ACCESS_TOKEN_INVALID_OR_EXPIRED")
            if rd.get("is_read_only_token"):
                blockers.append("UPSTOX_READ_ONLY_TOKEN_CANNOT_PLACE_ORDERS")
            if rd.get("static_ip_matched") is False:
                blockers.append("UPSTOX_STATIC_PROXY_IP_MISMATCH")
        except Exception:
            blockers.append("UPSTOX_ADAPTER_UNAVAILABLE")

        try:
            from app.data.upstox_stream_service import get_stream_service
            ws_connected = bool(get_stream_service().get_status().get("connected"))
        except Exception:
            ws_connected = None

        with self._conn() as conn:
            live_open = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM consensus_v3_positions
                    WHERE session_id = ? AND status IN ('OPEN', 'EXIT_RETRY_PENDING') AND execution_env = 'LIVE_UPSTOX'
                    """,
                    (self.live_session_id,),
                ).fetchall()
            ]

        # Day-2 Cash + Invested Equity Accounting + Option Sniffer Margin Firewall!
        live_invested_inr = sum(float(r.get("margin_used_inr") or r.get("invested_notional_inr") or 0.0) for r in live_open)
        sniffer_armed, sniffer_src = self._is_option_sniffer_armed()
        sniffer_res_inr = float(live_acct.get("option_sniffer_reserve_inr") or 0.0)
        if sniffer_armed and sniffer_res_inr <= 0.0:
            sniffer_res_inr = 90000.0

        rem_equity_cash_needed = max(
            0.0,
            float(live_acct["funded_capital_inr"]) + float(live_acct["realized_pnl_inr"]) - live_invested_inr,
        )
        req_broker_cash = round(rem_equity_cash_needed + sniffer_res_inr, 2)
        margin_ok = isinstance(avail_margin, (int, float)) and (float(avail_margin) + 1.0) >= req_broker_cash
        if not margin_ok:
            if sniffer_res_inr > 0:
                blockers.append(
                    f"INSUFFICIENT_BROKER_CASH_WITH_SNIFFER_RESERVE (Need ₹{req_broker_cash:,.0f} = "
                    f"₹{rem_equity_cash_needed:,.0f} Uninvested Equity + ₹{sniffer_res_inr:,.0f} Option Sniffer Fence)"
                )
            else:
                blockers.append(f"INSUFFICIENT_BROKER_CASH_FOR_FUNDED_ARENA (Need ₹{req_broker_cash:,.0f})")

        if live_acct["circuit_breaker"]["is_session_halted"]:
            blockers.append("DAILY_LOSS_CIRCUIT_BREAKER_TRIPPED")

        return {
            "engine_version": ENGINE_VERSION,
            "protocol_id": PROTOCOL_ID,
            "simulation_mode": self.simulation_mode,
            "is_armed": not self.simulation_mode,
            "armed_at": self._armed_at_iso or None,
            "execution_mode": acct["execution_mode"],
            "margin_mode": acct["margin_mode"],
            "intraday_mis_leverage": acct["intraday_mis_leverage"],
            "ddpi_confirmed": acct["ddpi_confirmed"],
            "option_sniffer_firewall": {
                "option_sniffer_armed": sniffer_armed,
                "option_sniffer_source": sniffer_src,
                "reserved_margin_inr": sniffer_res_inr,
            },
            "current_pin": self._current_pin,
            "available_margin": avail_margin,
            "live_invested_equity_inr": round(live_invested_inr, 2),
            "required_margin_for_mode": req_broker_cash,
            "configured_sleeve_capital_inr": live_acct["funded_capital_inr"],
            "mode_margin_ok": margin_ok,
            "active_live_positions": live_open,
            "shadow_gate_progress": self.get_shadow_gate_progress(),
            "circuit_breaker": acct["circuit_breaker"],
            "accounting": acct,
            "stream_health": {"websocket_connected": ws_connected},
            "readiness": {
                "token_valid": token_valid,
                "ddpi_confirmed": acct["ddpi_confirmed"],
                "live_trading_eligible": len(blockers) == 0,
                "blockers": blockers,
            },
        }

    def arm_live_trading(
        self,
        pin: str,
        execution_mode: Optional[str] = None,
        ddpi_confirmed: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        HUMAN-EXCLUSIVE INTERLOCK (`AGENTS.md` Rule 6):
        Never called by automated daemons, scripts, or AI agents.
        """
        with self._lock:
            if ddpi_confirmed is not None:
                self.set_ddpi_confirmation(bool(ddpi_confirmed))
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
                "message": "Disarmed to 100% Paper Shadow Mode (`simulation_mode = true`). Existing LIVE_UPSTOX positions remain actively monitored for protective exits.",
            }

    @staticmethod
    def _sanitize_upstox_tag(order_or_pos_id: str, action: str = "O") -> str:
        """Upstox requires alphanumeric/hyphen tags strictly `<= 20` chars; preserves unique trailing UUID hex."""
        clean = re.sub(r"[^A-Za-z0-9]", "", str(order_or_pos_id or ""))
        act = (str(action or "O")[:1]).upper()
        tail = clean[-14:] if len(clean) > 14 else clean
        return f"cv3{act}-{tail}"[:20]

    def _cancel_upstox_order(self, broker_order_id: str) -> Dict[str, Any]:
        boid = str(broker_order_id or "").strip()
        if not boid:
            return {"cancelled": True, "http_status": 200, "note": "NO_BROKER_OID"}
        try:
            import requests
            from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
            from app.data.upstox_provider import get_upstox_config, get_upstox_limiter, get_upstox_token

            cfg = get_upstox_config()
            tok = (cfg.get("algo_token") or "").strip() or get_upstox_token()
            if not tok:
                return {"cancelled": False, "http_status": 0, "error": "MISSING_TOKEN"}
            proxy_url = get_option_live_adapter().get_proxy_url()
            proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
            headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
            try:
                get_upstox_limiter().acquire(timeout=1.5)
            except Exception:
                pass
            last_code = 0
            last_body: Dict[str, Any] = {}
            for host in ("https://api-hft.upstox.com", "https://api.upstox.com"):
                try:
                    resp = requests.delete(
                        f"{host}/v2/order/cancel",
                        params={"order_id": boid},
                        headers=headers,
                        proxies=proxies,
                        timeout=5.0,
                    )
                    last_code = resp.status_code
                    last_body = resp.json() if resp.content else {}
                    if resp.status_code == 200:
                        self._live_orders_cache = (0.0, {}, {})
                        return {"cancelled": True, "http_status": 200, "raw": last_body}
                    # Order already completed or cancelled on exchange
                    msg_low = str(last_body).lower()
                    if "already" in msg_low or "cancel" in msg_low or "complete" in msg_low:
                        return {"cancelled": True, "http_status": resp.status_code, "raw": last_body}
                    if resp.status_code < 500:
                        break
                except Exception as inner_e:
                    last_body = {"error": str(inner_e)[:160]}
            return {"cancelled": False, "http_status": last_code, "raw": last_body}
        except Exception as err:
            return {"cancelled": False, "http_status": 0, "error": str(err)[:160]}

    def _fetch_upstox_day_orders_map(self, force: bool = False) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        now_ep = time.time()
        if not force and (now_ep - self._live_orders_cache[0]) < 2.5:
            return self._live_orders_cache[1], self._live_orders_cache[2]
        by_id: Dict[str, Dict[str, Any]] = {}
        by_tag: Dict[str, Dict[str, Any]] = {}
        try:
            import requests
            from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
            from app.data.upstox_provider import get_upstox_config, get_upstox_limiter, get_upstox_token

            cfg = get_upstox_config()
            tok = (cfg.get("algo_token") or "").strip() or get_upstox_token()
            if not tok:
                return by_id, by_tag
            proxy_url = get_option_live_adapter().get_proxy_url()
            proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
            try:
                get_upstox_limiter().acquire(timeout=1.5)
            except Exception:
                pass
            resp = requests.get(
                "https://api.upstox.com/v2/order/retrieve-all",
                headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
                proxies=proxies,
                timeout=5.0,
            )
            if resp.status_code == 200:
                for item in (resp.json() or {}).get("data") or []:
                    oid = str(item.get("order_id") or "")
                    tg = str(item.get("tag") or "")
                    if oid:
                        by_id[oid] = item
                    if tg:
                        by_tag[tg] = item
                self._live_orders_cache = (now_ep, by_id, by_tag)
        except Exception:
            pass
        return by_id, by_tag

    @staticmethod
    def _fetch_l1_quotes(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
        clean_list = [t for t in dict.fromkeys(tickers) if t]
        if not clean_list:
            return {}
        try:
            from app.data.upstox_provider import fetch_upstox_bulk_quotes
            return fetch_upstox_bulk_quotes(clean_list, max_cache_age_sec=2.0) or {}
        except Exception:
            return {}

    @staticmethod
    def _evaluate_l1_postonly_guard(
        direction: str,
        limit_px: float,
        l1_quote: Optional[Dict[str, Any]],
        max_spread_bps: float = BOOK2_MAX_L1_SPREAD_BPS,
    ) -> Tuple[bool, str]:
        if not isinstance(l1_quote, dict):
            return False, "L1_QUOTE_MISSING"
        try:
            best_bid = float(l1_quote.get("best_bid")) if l1_quote.get("best_bid") is not None else None
            best_ask = float(l1_quote.get("best_ask")) if l1_quote.get("best_ask") is not None else None
        except (TypeError, ValueError):
            return False, "L1_DEPTH_INVALID"
        if (
            best_bid is None
            or best_ask is None
            or not (math.isfinite(best_bid) and math.isfinite(best_ask) and math.isfinite(float(limit_px)))
        ):
            return False, "L1_DEPTH_INVALID"
        if best_bid <= 0.0 or best_ask <= 0.0:
            return False, "L1_DEPTH_MISSING_OR_ZERO"
        if best_ask < best_bid:
            return False, f"L1_CROSSED_BOOK(bid={best_bid},ask={best_ask})"
        mid = 0.5 * (best_bid + best_ask)
        spread_bps = ((best_ask - best_bid) / mid) * 10000.0 if mid > 0.0 else 99999.0
        if spread_bps > max_spread_bps:
            return False, f"L1_SPREAD_TOO_WIDE({spread_bps:.1f}bps>{max_spread_bps:.1f}bps)"
        d = str(direction or "").upper().strip()
        if d not in ("BUY", "SELL"):
            return False, f"L1_INVALID_DIRECTION({d})"
        if d == "SELL" and not (0.0 < best_bid < limit_px):
            return False, f"L1_SELL_POSTONLY_VIOLATED(best_bid={best_bid}>=limit={limit_px})"
        if d == "BUY" and not (best_ask > limit_px > 0.0):
            return False, f"L1_BUY_POSTONLY_VIOLATED(best_ask={best_ask}<=limit={limit_px})"
        return True, f"L1_OK(bid={best_bid},ask={best_ask},spread={spread_bps:.1f}bps)"

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
        is_exit: bool = False,
        l1_quote: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Routes an idempotent cash-equity order to Upstox:
          - New entry orders (`not is_exit and not is_emergency`) require human arming (`not self.simulation_mode`),
            fresh live quote (for `MARKET` entries), live L1 post-only guard (for Book-2 `MIS` `LIMIT` entries),
            AND must pass the Option Sniffer Margin Firewall!
          - Protective/rebalance/manual/SOS exits (`is_exit or is_emergency`) on verified `LIVE_UPSTOX` positions
            are permitted even if `self.simulation_mode == True` (e.g. Day-2 disarmed stop-loss protection).
          - Converts raw `MARKET` orders with a valid reference price into collared marketable `LIMIT` orders
            (`±1.0%` normal / `±2.5%` emergency) to eliminate NSE illiquidity/band rejections.
        """
        if self.simulation_mode and not (is_emergency or is_exit):
            return {"routed": False, "state": "PAPER_SHADOW_ONLY", "broker_order_id": None}

        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        intent_id = f"cv3_{action.lower()}_{order_or_pos_id}"
        if not instrument_key or not instrument_key.startswith("NSE_EQ|"):
            return {"routed": False, "state": "MISSING_NSE_EQ_KEY", "broker_order_id": None, "error": f"Missing NSE_EQ key for {ticker}"}

        orig_order_type = str(order_type or "LIMIT").upper()
        eff_order_type = orig_order_type
        eff_px = round_nse_eq_tick(price) if float(price or 0.0) > 0 else 0.0
        if eff_order_type == "MARKET":
            live_q_px = None
            try:
                from app.data.market_provider import get_live_quote_with_meta
                qm = get_live_quote_with_meta(ticker)
                if (
                    qm
                    and isinstance(qm.get("price"), (int, float))
                    and float(qm["price"]) > 0.0
                    and qm.get("source") != "unavailable"
                    and qm.get("is_realtime", True)
                ):
                    live_q_px = round_nse_eq_tick(float(qm["price"]))
            except Exception:
                live_q_px = None

            if live_q_px is not None:
                eff_px = live_q_px
            elif not is_exit and not is_emergency:
                err_q = f"Live Upstox quote unavailable for {ticker}; refusing stale-price MARKET entry"
                with self._conn() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO consensus_v3_live_intents (
                            intent_id, order_or_pos_id, action, product_type, order_type, ticker,
                            instrument_key, shares, price, state, broker_order_id, rejection_reason,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'BLOCKED_NO_LIVE_QUOTE', NULL, ?, ?, ?)
                        """,
                        (intent_id, order_or_pos_id, action.upper(), product_type, eff_order_type, ticker, instrument_key, int(shares), float(eff_px or price), err_q[:240], now_s, now_s),
                    )
                    conn.commit()
                return {"routed": False, "state": "MISSING_LIVE_QUOTE", "broker_order_id": None, "avg_fill_price": None, "error": err_q}

        # Enforce Option Sniffer Margin Firewall on all new live entry orders using refreshed `eff_px`!
        if not is_exit and not is_emergency:
            px_for_fw = eff_px if eff_px > 0 else float(price or 0.0)
            est_cash = round(int(shares) * max(0.05, px_for_fw), 2) if product_type == "CNC" else round(int(shares) * max(0.05, px_for_fw) * 0.35, 2)
            fw = self._check_option_sniffer_margin_firewall(est_cash)
            if not fw.get("allowed", True):
                rej_fw = str(fw.get("reason") or "Option Sniffer Margin Firewall blocked entry")
                with self._conn() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO consensus_v3_live_intents (
                            intent_id, order_or_pos_id, action, product_type, order_type, ticker,
                            instrument_key, shares, price, state, broker_order_id, rejection_reason,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'BLOCKED_FIREWALL', NULL, ?, ?, ?)
                        """,
                        (intent_id, order_or_pos_id, action.upper(), product_type, eff_order_type, ticker, instrument_key, int(shares), float(px_for_fw), rej_fw[:240], now_s, now_s),
                    )
                    conn.commit()
                return {"routed": False, "state": "BLOCKED_FIREWALL", "broker_order_id": None, "error": rej_fw}

        if eff_order_type == "MARKET" and eff_px > 0.0:
            band = 0.025 if is_emergency else 0.010
            eff_px = round_nse_eq_tick(eff_px * ((1.0 + band) if action.upper() == "BUY" else (1.0 - band)))
            eff_order_type = "LIMIT"

        if product_type == "MIS" and orig_order_type == "LIMIT" and not is_exit and not is_emergency:
            q_l1 = l1_quote
            if q_l1 is None:
                q_map = self._fetch_l1_quotes([ticker])
                clean_t = ticker.replace(".NS", "").replace(".BO", "").strip().upper()
                q_l1 = q_map.get(ticker) or q_map.get(clean_t)
            l1_ok, l1_reason = self._evaluate_l1_postonly_guard(action, eff_px, q_l1)
            if not l1_ok:
                with self._conn() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO consensus_v3_live_intents (
                            intent_id, order_or_pos_id, action, product_type, order_type, ticker,
                            instrument_key, shares, price, state, broker_order_id, rejection_reason,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'BLOCKED_L1_GUARD', NULL, ?, ?, ?)
                        """,
                        (intent_id, order_or_pos_id, action.upper(), product_type, eff_order_type, ticker, instrument_key, int(shares), float(eff_px or price), l1_reason[:240], now_s, now_s),
                    )
                    conn.commit()
                return {"routed": False, "state": "BLOCKED_L1_GUARD", "broker_order_id": None, "avg_fill_price": None, "error": l1_reason}

        tag = self._sanitize_upstox_tag(order_or_pos_id, action)
        payload = {
            "quantity": int(shares),
            "product": "I" if product_type == "MIS" else "D",
            "validity": "DAY",
            "price": eff_px if eff_order_type == "LIMIT" else 0.0,
            "tag": tag,
            "instrument_token": instrument_key,
            "order_type": eff_order_type,
            "transaction_type": action.upper(),
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
        }

        state = "SUBMITTED"
        broker_oid = None
        avg_fill_px = None
        rej = None
        try:
            import requests
            from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
            from app.data.upstox_provider import get_upstox_config, get_upstox_limiter, get_upstox_token

            adapter = get_option_live_adapter()
            cfg = get_upstox_config()
            tok = (cfg.get("algo_token") or "").strip() or get_upstox_token()
            if not tok:
                raise RuntimeError("Missing Upstox bearer token")
            proxy_url = adapter.get_proxy_url()
            proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
            headers = {
                "Authorization": f"Bearer {tok}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            try:
                get_upstox_limiter().acquire(timeout=0.5 if is_emergency else 2.0)
            except Exception:
                pass

            http_resp = None
            last_exc = None
            for host in ("https://api-hft.upstox.com", "https://api.upstox.com"):
                try:
                    http_resp = requests.post(
                        f"{host}/v2/order/place",
                        headers=headers,
                        json=payload,
                        proxies=proxies,
                        timeout=7.5,
                    )
                    if http_resp.status_code < 500:
                        break
                except requests.Timeout as te:
                    last_exc = te
                    # Ambiguous timeout guard: check orderbook by tag BEFORE retrying so we never double-buy/sell!
                    _, by_tag = self._fetch_upstox_day_orders_map(force=True)
                    matched = by_tag.get(tag)
                    if matched and matched.get("order_id"):
                        broker_oid = str(matched["order_id"])
                        http_resp = None
                        break
                except Exception as he:
                    last_exc = he
                    continue

            if broker_oid is None:
                if http_resp is None:
                    raise RuntimeError(f"Upstox order transport failed: {last_exc}")
                resp = http_resp.json() if http_resp.content else {}
                broker_oid = str((resp or {}).get("data", {}).get("order_id") or "")
                if http_resp.status_code != 200 or not broker_oid:
                    state = "REJECTED"
                    rej = f"HTTP {http_resp.status_code}: {str(resp)[:200]}"

            # Immediate post-placement exchange rejection / fill-price verification (catches DDPI/TPIN or margin rejects!)
            if state == "SUBMITTED" and broker_oid:
                by_id, _ = self._fetch_upstox_day_orders_map(force=True)
                oinfo = by_id.get(broker_oid)
                if oinfo:
                    ost = str(oinfo.get("status") or "").lower()
                    if ost in ("rejected", "cancelled", "canceled") and int(float(oinfo.get("filled_quantity") or 0)) == 0:
                        state = "REJECTED"
                        rej = f"Exchange {ost.upper()}: {str(oinfo.get('status_message') or '')[:180]}"
                    elif float(oinfo.get("average_price") or 0.0) > 0.0:
                        avg_fill_px = float(oinfo["average_price"])
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
                    intent_id, order_or_pos_id, action.upper(), product_type, eff_order_type, ticker,
                    instrument_key, int(shares), float(eff_px or price), state, broker_oid, rej, now_s, now_s,
                ),
            )
            conn.commit()
        return {
            "routed": state == "SUBMITTED",
            "state": state,
            "broker_order_id": broker_oid,
            "avg_fill_price": avg_fill_px,
            "error": rej,
        }

    def reserve_book2_pending_limit_orders(
        self,
        approved_signals: List[Dict[str, Any]],
        now_dt: Optional[datetime] = None,
        allow_broker_probe: bool = True,
        force_shadow_session: bool = False,
        l1_quotes: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Places 15-minute Post-Only Limit Reservations in `consensus_v3_pending_orders`:
          - Immediately locks `shares * margin_per_share` in `pending_reserved_cash_inr`.
          - Enforces `invested_margin + pending_reserved_cash <= current_equity` (`0` wallet cash deficit!)
            and `total_b2_open_and_pending_notional <= 1.0x current_equity`.
          - Blocks new reservations at `>= 14:55 IST` and clamps `expires_at <= 14:55:00` (`< 15:10 IST` square-off).
          - When `force_shadow_session=True` (used while Live `BOOK1_SWING_CNC_ONLY` is armed), records Book-2
            shadow limit orders strictly in `shadow_session_id` (`PAPER_SHADOW`) so the 60-session gate keeps advancing!
        """
        ts = now_dt or datetime.now()
        today_s = ts.strftime("%Y-%m-%d")
        hm_now = ts.strftime("%H:%M")
        if hm_now >= "14:55":
            return []
        now_s = ts.strftime("%Y-%m-%d %H:%M:%S")
        cutoff_1455 = ts.replace(hour=14, minute=55, second=0, microsecond=0)
        exp_s = min(ts + timedelta(minutes=15), cutoff_1455).strftime("%Y-%m-%d %H:%M:%S")

        created_orders: List[Dict[str, Any]] = []
        with self._lock:
            target_sid = self.shadow_session_id if force_shadow_session else None
            acct = self.get_arena_accounting(now_dt=ts, session_override=target_sid)
            sid = acct["session_id"]

            if not force_shadow_session and acct["execution_mode"] == MODE_BOOK1_SWING_CNC:
                return []
            if acct["circuit_breaker"]["is_session_halted"]:
                return []
            if acct["circuit_breaker"]["fills_today"] >= 6:
                return []

            avail_b2_slots = acct["book2_slots"]["max"] - (acct["book2_slots"]["open"] + acct["book2_slots"]["pending"])
            if avail_b2_slots <= 0:
                return []

            free_cash = acct["free_unreserved_cash_inr"]
            rem_b2_notional = float(acct["book2_slots"]["remaining_notional_cap_inr"])
            slot_cap = acct["book2_slots"]["slot_cap_inr"]
            m_mode = acct.get("margin_mode", MARGIN_MODE_CASH_1X)
            mis_lev = float(acct.get("intraday_mis_leverage", 1.0))
            risk_pct = float(acct.get("max_risk_per_trade_pct", 0.50))
            risk_budget_inr = acct["current_equity_inr"] * (risk_pct / 100.0)
            daily_halt_budget_inr = acct["funded_capital_inr"] * DAILY_LOSS_HALT_FRACTION
            exec_env = "PAPER_SHADOW" if (self.simulation_mode or force_shadow_session) else "LIVE_UPSTOX"

            check_l1 = (exec_env == "LIVE_UPSTOX" and (allow_broker_probe or l1_quotes is not None)) or (l1_quotes is not None)
            if check_l1:
                l1_map = l1_quotes if l1_quotes is not None else self._fetch_l1_quotes([s["ticker"] for s in approved_signals])
            else:
                l1_map = {}

            with self._conn() as conn:
                already_today = {
                    r[0]
                    for r in conn.execute(
                        """
                        SELECT ticker FROM consensus_v3_pending_orders WHERE session_id = ? AND substr(created_at,1,10) = ?
                        UNION
                        SELECT ticker FROM consensus_v3_positions WHERE session_id = ? AND substr(entry_time,1,10) = ?
                        """,
                        (sid, today_s, sid, today_s),
                    ).fetchall()
                }

                for sig in approved_signals:
                    if avail_b2_slots <= 0:
                        break
                    ticker = sig["ticker"]
                    if ticker in already_today:
                        continue

                    lim_px = round_nse_eq_tick(float(sig["limit_entry_px"]))
                    if rem_b2_notional < lim_px:
                        continue

                    q_l1 = None
                    if check_l1:
                        clean_t = ticker.replace(".NS", "").replace(".BO", "").strip().upper()
                        q_l1 = l1_map.get(ticker) or l1_map.get(clean_t)
                        if exec_env == "LIVE_UPSTOX" or q_l1 is not None:
                            l1_ok, _ = self._evaluate_l1_postonly_guard(sig["direction"], lim_px, q_l1)
                            if not l1_ok:
                                continue

                    sl_px = float(sig["stop_loss_px"])
                    adr_pct = float((sig.get("telemetry") or {}).get("atr_pct") or 2.0)

                    margin_per_sh, eff_lev, margin_src = self.estimate_mis_margin_per_share(
                        instrument_key=sig.get("instrument_key", ""),
                        limit_px=lim_px,
                        direction=sig["direction"],
                        adr_pct=adr_pct,
                        margin_mode=m_mode,
                        mis_leverage_cap=mis_lev,
                        allow_broker_probe=allow_broker_probe and not force_shadow_session,
                    )

                    max_by_cash_margin = int(free_cash // max(0.05, margin_per_sh))
                    max_by_1x_notional = int(rem_b2_notional // max(0.05, lim_px))
                    max_by_slot_notional = compute_whole_shares(
                        allocatable_cash_inr=min(slot_cap, rem_b2_notional),
                        price_inr=lim_px,
                        max_slot_inr=slot_cap,
                        liquidity_cap_inr=float(sig.get("liq_1pct_bar_inr", slot_cap)),
                    )
                    shares = min(max_by_cash_margin, max_by_slot_notional, max_by_1x_notional)

                    if shares >= 1:
                        stop_dist_px = max(lim_px * 0.0035, abs(sl_px - lim_px))
                        sl_cap_budget = risk_budget_inr if mis_lev > 1.001 else daily_halt_budget_inr
                        max_by_stop_risk = int(sl_cap_budget // stop_dist_px)
                        shares = min(shares, max_by_stop_risk)

                    if shares < 1:
                        continue

                    reserved_inr = round(shares * margin_per_sh, 2)
                    notional_inr = round(shares * lim_px, 2)
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
                            l1_quote=q_l1,
                        )
                        if not route_res["routed"]:
                            continue
                        broker_oid = route_res["broker_order_id"]

                    conn.execute(
                        """
                        INSERT INTO consensus_v3_pending_orders (
                            order_id, session_id, book, product_type, ticker, symbol, instrument_key,
                            direction, shares, signal_close_px, limit_price, reserved_cash_inr,
                            exposure_notional_inr, effective_leverage, margin_source,
                            stop_loss_px, scheduled_exit_hm, status, execution_env, broker_order_id,
                            created_at, expires_at, details_json
                        ) VALUES (?, ?, ?, 'MIS', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_RESERVED', ?, ?, ?, ?, ?)
                        """,
                        (
                            oid, sid, sig["book"], ticker, sig["symbol"], sig.get("instrument_key", ""),
                            sig["direction"], shares, float(sig["signal_close_px"]), lim_px, reserved_inr,
                            notional_inr, eff_lev, margin_src,
                            sl_px, sig.get("scheduled_exit_hm", "15:10"),
                            exec_env, broker_oid, now_s, exp_s, json.dumps(sig),
                        ),
                    )
                    free_cash -= reserved_inr
                    rem_b2_notional -= notional_inr
                    avail_b2_slots -= 1
                    already_today.add(ticker)
                    created_orders.append({
                        "order_id": oid,
                        "session_id": sid,
                        "ticker": ticker,
                        "direction": sig["direction"],
                        "shares": shares,
                        "limit_price": lim_px,
                        "reserved_cash_inr": reserved_inr,
                        "exposure_notional_inr": notional_inr,
                        "effective_leverage": eff_lev,
                        "margin_source": margin_src,
                        "expires_at": exp_s,
                        "execution_env": exec_env,
                    })
                conn.commit()
        return created_orders

    def execute_book1_scheduled_rebalance(
        self,
        scored_snapshot: Dict[str, Any],
        arb_b1: Dict[str, Any],
        force: bool = False,
        now_dt: Optional[datetime] = None,
        live_quotes: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Executes Layer-6 Book 1 (`20d` Scheduled Cash `CNC` Swing Core) Rebalance + Mid-Cycle PMS Check:
          - Strictly `1.0x` Funded Cash (`0x` leverage, `product_type='CNC'`, whole shares `>= 1`).
          - Enforces Sector Concentration Cap (`max 1/sector` at `<=3` slots, `max 2/sector` at `>=4` slots)
            and Bounded Inverse-Volatility slot sizing (`[0.85x..1.15x]`).
          - Applies SEBI `80%` T+0 same-day `CNC` delivery sale-proceeds haircut on `LIVE_UPSTOX` replacement buys.
          - When not yet at `20` trading days, automatically checks the 4-Gate Mid-Cycle Top-Up Deployment Engine!
        """
        ts = now_dt or datetime.now()
        now_s = ts.strftime("%Y-%m-%d %H:%M:%S")
        today_s = ts.strftime("%Y-%m-%d")
        as_of_date = str(arb_b1.get("as_of_date") or scored_snapshot.get("as_of_date") or today_s)

        with self._lock:
            acct = self.get_arena_accounting(now_dt=ts)
            sid = acct["session_id"]
            if acct["execution_mode"] == MODE_BOOK2_INTRADAY_MIS:
                return {"status": "SKIPPED_MODE_BOOK2_ONLY", "opened": 0, "closed": 0, "retained": 0}
            if acct["circuit_breaker"]["is_session_halted"] and not force:
                return {"status": "SKIPPED_CIRCUIT_BREAKER", "opened": 0, "closed": 0, "retained": 0}

            b1_max = int(acct["book1_slots"]["max"])
            shield = arb_b1.get("macro_shield") or {}
            raw_shield_slots = int(shield.get("target_slots", 8))
            shield_ratio = max(0.0, min(1.0, raw_shield_slots / 8.0))
            target_slots = int(round(b1_max * shield_ratio))

            last_reb = acct.get("last_book1_rebalance_date")
            with self._conn() as conn:
                open_b1 = conn.execute(
                    "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND book = 'BOOK1_SWING_CNC_CORE' AND status IN ('OPEN', 'EXIT_RETRY_PENDING')",
                    (sid,),
                ).fetchall()

                tdays_elapsed = 999
                if last_reb:
                    tdays_elapsed = self._count_trading_days_since(conn, str(last_reb)[:10], today_s)

                shield_stepdown_needed = len(open_b1) > target_slots
                is_due = force or (last_reb is None) or (tdays_elapsed >= 20) or shield_stepdown_needed

            if not is_due:
                mid_res = self.deploy_midcycle_inflow_4gate(scored_snapshot, arb_b1, now_dt=ts, live_quotes=live_quotes)
                if mid_res.get("opened", 0) > 0 or mid_res.get("pyramided", 0) > 0:
                    return {
                        "status": "MIDCYCLE_TOPUP_DEPLOYED",
                        "trading_days_since_last": tdays_elapsed,
                        "next_due_in_trading_days": max(0, 20 - tdays_elapsed),
                        "target_slots": target_slots,
                        "opened": mid_res["opened"],
                        "pyramided": mid_res["pyramided"],
                        "closed": 0,
                        "retained": len(open_b1),
                        "midcycle_detail": mid_res,
                    }
                return {
                    "status": "NOT_DUE_YET",
                    "trading_days_since_last": tdays_elapsed,
                    "next_due_in_trading_days": max(0, 20 - tdays_elapsed),
                    "target_slots": target_slots,
                    "opened": 0,
                    "closed": 0,
                    "retained": len(open_b1),
                }

            with self._conn() as conn:
                open_b1 = conn.execute(
                    "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND book = 'BOOK1_SWING_CNC_CORE' AND status IN ('OPEN', 'EXIT_RETRY_PENDING')",
                    (sid,),
                ).fetchall()
                tickers_needed = [r["ticker"] for r in open_b1] + [
                    l["ticker"] for l in (arb_b1.get("approved_rebalance_leaders") or [])
                ]
                quotes = live_quotes if live_quotes is not None else self._fetch_bulk_quotes_safe(tickers_needed)
                cand_map = {c["ticker"]: c for c in (scored_snapshot.get("candidates") or [])}
                keep_candidates = []
                to_close = []

                for pos in open_b1:
                    sym = pos["ticker"]
                    epx = float(pos["entry_price"])
                    cinfo = cand_map.get(sym)
                    fb_px = float(cinfo["close"]) if cinfo else float(pos["current_price"] or epx)
                    curr_px_resolved, _ = self._resolve_book1_exec_price(
                        conn, sym, fb_px, today_s, now_s, quotes=quotes, require_live_quote=False,
                    )
                    curr_px = float(curr_px_resolved or fb_px)
                    raw_jump = abs(curr_px - float(pos["current_price"] or epx)) / max(1.0, float(pos["current_price"] or epx))

                    if raw_jump > 0.40:
                        to_close.append((pos, float(pos["current_price"] or epx), "CORP_ACTION_GUARD_EXIT"))
                    elif target_slots == 0:
                        to_close.append((pos, curr_px, "MACRO_BEAR_SHIELD_CASH_EXIT"))
                    elif cinfo is None or not cinfo.get("sma_alignment_ok", False) or float(cinfo.get("qlib_pct_rank", 0.0)) < 0.78:
                        to_close.append((pos, curr_px, "BOOK1_RANK_HYSTERESIS_EXIT"))
                    else:
                        keep_candidates.append((float(cinfo.get("qlib_pct_rank", 0.0)), pos, curr_px))

                keep_candidates.sort(key=lambda x: x[0], reverse=True)
                retained_list = keep_candidates[:target_slots]
                for _, extra_pos, extra_px in keep_candidates[target_slots:]:
                    to_close.append((extra_pos, extra_px, "MACRO_SHIELD_SLOT_STEPDOWN"))

                closed_records = []
                live_sold_proceeds_inr = 0.0
                for pos_row, xpx, reason in to_close:
                    cres = self._close_position_in_tx(conn, pos_row, xpx, reason, now_s, today_s, session_id=sid)
                    if cres.get("closed", True):
                        closed_records.append(cres)
                        if pos_row["execution_env"] == "LIVE_UPSTOX":
                            live_sold_proceeds_inr += float(cres["exit_px"]) * int(pos_row["shares"])

                st_now = conn.execute("SELECT * FROM consensus_v3_arena_state WHERE session_id = ?", (sid,)).fetchone()
                rem_open = conn.execute(
                    "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND status IN ('OPEN', 'EXIT_RETRY_PENDING')",
                    (sid,),
                ).fetchall()
                rem_pend = conn.execute(
                    "SELECT * FROM consensus_v3_pending_orders WHERE session_id = ? AND status = 'PENDING_RESERVED'",
                    (sid,),
                ).fetchall()

                used_margin = sum(
                    float(r["margin_used_inr"]) if ("margin_used_inr" in r.keys() and r["margin_used_inr"] is not None) else float(r["invested_notional_inr"])
                    for r in rem_open
                )
                pend_cash = sum(float(r["reserved_cash_inr"]) for r in rem_pend)
                rem_unreal = sum(float(r["unrealized_pnl_inr"]) for r in rem_open)
                eq_now = float(st_now["funded_capital_inr"]) + float(st_now["realized_pnl_inr"]) + rem_unreal

                mode_now = str(st_now["execution_mode"])
                res_pct_now = 0.0 if mode_now == MODE_BOOK1_SWING_CNC else float(
                    st_now["intraday_cash_reserve_pct"] if "intraday_cash_reserve_pct" in st_now.keys() else 20.0
                )
                intraday_floor_inr = eq_now * (res_pct_now / 100.0) if mode_now == MODE_DUAL_FUNDED_ARENA else 0.0
                sebi_t0_haircut_inr = (1.0 - SEBI_SAME_DAY_CNC_SALE_CREDIT_FRACTION) * live_sold_proceeds_inr
                free_cash = max(0.0, eq_now - used_margin - pend_cash - intraday_floor_inr - sebi_t0_haircut_inr)

                # New entries strictly blocked when simulation_mode == True AND sid == live_session_id
                can_open_entries = (not self.simulation_mode) if sid == self.live_session_id else True
                exec_env = "PAPER_SHADOW" if self.simulation_mode else "LIVE_UPSTOX"

                rem_b1_open_cnt = sum(1 for r in rem_open if r["book"] == "BOOK1_SWING_CNC_CORE")
                held_tickers = {r["ticker"] for r in rem_open if r["book"] == "BOOK1_SWING_CNC_CORE"}
                sector_counts: Dict[str, int] = {}
                for r in rem_open:
                    if r["book"] == "BOOK1_SWING_CNC_CORE":
                        sec = resolve_fno_sector(r["symbol"])
                        sector_counts[sec] = sector_counts.get(sec, 0) + 1
                max_per_sector = 1 if target_slots <= 3 else 2

                slots_to_open = max(0, target_slots - rem_b1_open_cnt) if can_open_entries else 0
                b1_total_budget = float(st_now["funded_capital_inr"]) * (1.0 - res_pct_now / 100.0)
                base_slot_cap_inr = b1_total_budget / max(1, b1_max)

                opened_records = []
                leaders_pool = list(arb_b1.get("approved_rebalance_leaders") or [])

                for leader in leaders_pool:
                    if slots_to_open <= 0 or free_cash < 50.0:
                        break
                    sym = leader["ticker"]
                    if sym in held_tickers:
                        continue
                    sec = str(leader.get("sector") or resolve_fno_sector(leader["symbol"]))
                    if sec != "DIVERSIFIED_FNO" and sector_counts.get(sec, 0) >= max_per_sector:
                        continue

                    vol60 = max(18.0, min(65.0, float(leader.get("vol60_ann_pct") or 30.0)))
                    vol_mult = max(0.85, min(1.15, 30.0 / vol60))
                    # Ensure remaining slots also get fair cash share when scaling inverse-vol
                    fair_rem_cap = free_cash / max(1, slots_to_open) if slots_to_open > 1 else free_cash
                    eff_slot_cap = min(free_cash, max(base_slot_cap_inr * 0.85, min(base_slot_cap_inr * vol_mult, fair_rem_cap * 1.12)))

                    epx, _ = self._resolve_book1_exec_price(
                        conn, sym, float(leader["close_px"]), today_s, now_s,
                        quotes=quotes, require_live_quote=(exec_env == "LIVE_UPSTOX"),
                    )
                    if epx is None or epx <= 0.0:
                        continue
                    sh = compute_whole_shares(
                        allocatable_cash_inr=free_cash,
                        price_inr=epx,
                        max_slot_inr=eff_slot_cap,
                        liquidity_cap_inr=base_slot_cap_inr * 10.0,
                    )
                    if sh < 1:
                        continue

                    notional = round(sh * epx, 2)
                    pid = f"pos_b1_{today_s.replace('-', '')}_{uuid.uuid4().hex[:8]}"
                    if exec_env == "LIVE_UPSTOX":
                        r_res = self._route_upstox_equity_order(
                            order_or_pos_id=pid,
                            action="BUY",
                            product_type="CNC",
                            order_type="MARKET",
                            ticker=sym,
                            instrument_key=leader.get("instrument_key", ""),
                            shares=sh,
                            price=epx,
                        )
                        if not r_res["routed"]:
                            continue
                        if r_res.get("avg_fill_price"):
                            epx = round_nse_eq_tick(float(r_res["avg_fill_price"]))
                            notional = round(sh * epx, 2)

                    # -14% Hard Disaster Stop + Active Intra-Cycle SMA50/SMA200/FinBERT Breakdown Sentinel
                    disaster_sl = round_nse_eq_tick(epx * 0.86)
                    conn.execute(
                        """
                        INSERT INTO consensus_v3_positions (
                            position_id, session_id, order_id, book, product_type, ticker, symbol,
                            instrument_key, direction, shares, entry_price, invested_notional_inr,
                            margin_used_inr, effective_leverage, margin_source,
                            stop_loss_px, scheduled_exit, status, execution_env, current_price,
                            unrealized_pnl_inr, entry_time, details_json
                        ) VALUES (?, ?, NULL, 'BOOK1_SWING_CNC_CORE', 'CNC', ?, ?, ?, 'BUY', ?, ?, ?, ?, 1.0, 'CASH_1X_EXACT', ?, '20D_SCHEDULED_REBALANCE', 'OPEN', ?, ?, 0.0, ?, ?)
                        """,
                        (
                            pid, sid, sym, leader["symbol"], leader.get("instrument_key", ""),
                            sh, epx, notional, notional, disaster_sl, exec_env, epx, now_s, json.dumps(leader),
                        ),
                    )
                    free_cash -= notional
                    slots_to_open -= 1
                    held_tickers.add(sym)
                    sector_counts[sec] = sector_counts.get(sec, 0) + 1
                    opened_records.append({
                        "position_id": pid,
                        "ticker": sym,
                        "sector": sec,
                        "shares": sh,
                        "entry_price": epx,
                        "notional_inr": notional,
                    })

                conn.execute(
                    "UPDATE consensus_v3_arena_state SET last_book1_rebalance_date = ?, updated_at = ? WHERE session_id = ?",
                    (today_s, now_s, sid),
                )
                conn.commit()

        return {
            "status": "REBALANCED",
            "session_id": sid,
            "as_of_date": as_of_date,
            "rebalance_date": today_s,
            "target_slots": target_slots,
            "sebi_same_day_cnc_haircut_inr": round(sebi_t0_haircut_inr, 2),
            "retained": len(retained_list),
            "closed": len(closed_records),
            "opened": len(opened_records),
            "opened_positions": opened_records,
            "closed_positions": closed_records,
        }

    def _check_book1_intracycle_breakdown(
        self,
        conn: sqlite3.Connection,
        sym: str,
        entry_date_s: str,
        curr_px: float,
        today_s: str,
        hm_now: str,
        allow_live_finbert: bool = False,
    ) -> Optional[str]:
        """
        Multi-condition institutional breakdown & FinBERT shock sentinel for bought Book-1 `CNC` holdings:
          1. Completed daily `Close < SMA200 * 0.995` (macro trend loss).
          2. Completed daily `Close < SMA50` AND `1d return <= -4.0%` AND `20d RVOL >= 2.0x` (institutional distribution).
          3. Live `FinBERT` severe regulatory/governance shock (`Neg >= 0.85` + regulatory keyword) confirmed by `<= -3.0%` price drop.
          Gracefully skips on synthetic `<200`-day test databases.
        """
        rows_1d = conn.execute(
            """
            SELECT substr(date,1,10) as d, open, high, low, close, volume
            FROM ohlcv
            WHERE ticker = ? AND timeframe = '1d' AND substr(date,1,10) <= ?
            ORDER BY date DESC LIMIT 210
            """,
            (sym, today_s),
        ).fetchall()
        by_d: Dict[str, Tuple[float, float, float, float, float]] = {}
        for r in rows_1d:
            if float(r[4] or 0) > 0:
                by_d[str(r[0])] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0.0))

        # Stitch completed post-2026-09-24 15m sessions (excluding incomplete active session < 15:30 IST)
        rows_15m = conn.execute(
            """
            SELECT substr(date,1,10) as d, open, high, low, close, volume
            FROM ohlcv
            WHERE ticker = ? AND timeframe = '15m' AND date >= '2026-09-24' AND substr(date,1,10) <= ?
            ORDER BY date ASC
            """,
            (sym, today_s),
        ).fetchall()
        g15: Dict[str, list] = {}
        for r in rows_15m:
            if float(r[4] or 0) > 0:
                g15.setdefault(str(r[0]), []).append((float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0.0)))
        for d_s, bls in g15.items():
            if d_s == today_s and hm_now < "15:30" and len(bls) < 25:
                continue
            if len(bls) >= (24 if d_s == today_s else 15):
                by_d[d_s] = (bls[0][0], max(x[1] for x in bls), min(x[2] for x in bls), bls[-1][3], sum(x[4] for x in bls))

        sdays = sorted(by_d.keys())
        if len(sdays) < 200:
            return None

        last_d = sdays[-1]
        closes_200 = [by_d[d][3] for d in sdays[-200:]]
        sma50 = sum(closes_200[-50:]) / 50.0
        sma200 = sum(closes_200) / 200.0
        last_c = closes_200[-1]
        prev_c = closes_200[-2]

        # Only trigger daily close breakdown on a completed daily bar strictly >= entry_date_s
        if last_d > entry_date_s:
            if last_c < sma200 * 0.995:
                return "INTRACYCLE_SMA200_TREND_BREAKDOWN"
            ret_1d = ((last_c / max(0.05, prev_c)) - 1.0) * 100.0
            turns_20 = [by_d[d][3] * by_d[d][4] for d in sdays[-21:-1]]
            avg_t20 = (sum(turns_20) / len(turns_20)) if turns_20 else 1.0
            rvol_20 = (last_c * by_d[last_d][4]) / max(1.0, avg_t20)
            if last_c < sma50 and ret_1d <= -4.0 and rvol_20 >= 2.0:
                return "INTRACYCLE_INSTITUTIONAL_DISTRIBUTION_EXIT"

        # Live FinBERT Regulatory/Fraud Shock Sentinel (gated by >=3% intraday drop so calm days incur 0ms overhead!)
        if allow_live_finbert and curr_px > 0 and last_c > 0:
            intraday_drop_pct = ((curr_px / last_c) - 1.0) * 100.0
            if intraday_drop_pct <= -3.0:
                try:
                    from app.analytics.nlp_engine import get_nlp_analyzer
                    nlp_res = get_nlp_analyzer().analyze_ticker_news(sym)
                    if nlp_res.get("is_severe_negative_shock"):
                        return "FINBERT_REGULATORY_SHOCK_EXIT"
                except Exception:
                    pass
        return None

    def reconcile_orders_and_positions(
        self,
        live_quotes: Optional[Dict[str, Dict[str, Any]]] = None,
        now_dt: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Causal 1-Minute Reconciler across Paper Shadow and Live Upstox ledgers:
          1. `PAPER_SHADOW` orders: strict post-signal `3 bps` limit trade-through or `15m` TIF expiry
             with forming-bar open post-only gap protection, `fills_today < 6` enforcement, and post-SL cash recheck.
          2. `LIVE_UPSTOX` orders: queries Upstox `/v2/order/retrieve-all` for true exchange fills/partials,
             and calls `/v2/order/cancel` upon `15m` TIF expiry (`14:55 IST` cutoff) before releasing cash!
          3. `OPEN` / `EXIT_RETRY_PENDING` positions: updates live MTM, enforces `15:10 IST` NSE `MIS` square-off,
             entry-candle + multi-bar structural stops, and Book-1 Intra-Cycle Breakdown + FinBERT Sentinel.
        """
        ts = now_dt or datetime.now()
        now_s = ts.strftime("%Y-%m-%d %H:%M:%S")
        hm_now = ts.strftime("%H:%M")
        today_s = ts.strftime("%Y-%m-%d")
        allow_live_finbert = now_dt is None
        slot_start_dt = ts.replace(minute=(ts.minute // 15) * 15, second=0, microsecond=0)
        slot_start_s = slot_start_dt.strftime("%Y-%m-%d %H:%M:%S")

        def _slot_floor_str(raw_ts: Optional[str]) -> str:
            try:
                parsed = datetime.strptime(str(raw_ts or "")[:19], "%Y-%m-%d %H:%M:%S")
                return parsed.replace(minute=(parsed.minute // 15) * 15, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return slot_start_s

        total_filled, total_expired, total_closed = 0, 0, 0
        with self._lock, self._conn() as conn:
            sessions_to_run = [self.shadow_session_id]
            if not self.simulation_mode or self._has_open_live_exposure(conn):
                if self.live_session_id not in sessions_to_run:
                    sessions_to_run.append(self.live_session_id)

            all_pend = conn.execute(
                f"SELECT * FROM consensus_v3_pending_orders WHERE session_id IN ({','.join('?' for _ in sessions_to_run)}) AND status = 'PENDING_RESERVED'",
                tuple(sessions_to_run),
            ).fetchall()
            all_open = conn.execute(
                f"SELECT * FROM consensus_v3_positions WHERE session_id IN ({','.join('?' for _ in sessions_to_run)}) AND status IN ('OPEN', 'EXIT_RETRY_PENDING')",
                tuple(sessions_to_run),
            ).fetchall()

            tickers_needed = list({r["ticker"] for r in all_pend} | {r["ticker"] for r in all_open})
            quotes = live_quotes
            if quotes is None and tickers_needed:
                try:
                    from app.data.market_provider import get_bulk_live_quotes_with_meta
                    quotes = get_bulk_live_quotes_with_meta(tickers_needed)
                except Exception:
                    quotes = {}
            quotes = quotes or {}

            has_live_pend = any(r["execution_env"] == "LIVE_UPSTOX" for r in all_pend)
            live_orders_by_id: Dict[str, Dict[str, Any]] = {}
            if has_live_pend:
                live_orders_by_id, _ = self._fetch_upstox_day_orders_map(force=True)

            completed_15m_cutoff = (ts - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")

            def _latest_px_and_bars(
                sym: str,
                after_ts: str,
                prod_type: str = "MIS",
                details_json: Optional[str] = None,
                is_pending: bool = False,
            ) -> Tuple[Optional[float], Optional[Tuple[Any, ...]], Optional[float], Optional[float]]:
                q = quotes.get(sym) or quotes.get(sym.replace(".NS", ""))
                ltp = float(q["price"]) if (q and isinstance(q.get("price"), (int, float)) and q["price"] > 0) else None

                djson: Dict[str, Any] = {}
                if details_json:
                    try:
                        parsed = json.loads(details_json)
                        if isinstance(parsed, dict):
                            djson = parsed
                    except Exception:
                        djson = {}

                bars: List[Tuple[Any, ...]] = []
                if is_pending:
                    sig_bar_dt = djson.get("bar_dt")
                    if sig_bar_dt:
                        bars = conn.execute(
                            "SELECT date, open, high, low, close FROM ohlcv WHERE ticker = ? AND timeframe = '15m' AND date > ? AND date <= ? ORDER BY date ASC",
                            (sym, str(sig_bar_dt)[:19], completed_15m_cutoff),
                        ).fetchall()
                    else:
                        min_bar_s = str(after_ts)[:19]
                        use_gte = False
                        try:
                            adt = datetime.strptime(min_bar_s, "%Y-%m-%d %H:%M:%S")
                            slot_dt = adt.replace(minute=(adt.minute // 15) * 15, second=0, microsecond=0)
                            if 0.0 <= (adt - slot_dt).total_seconds() <= 90.0:
                                min_bar_s = slot_dt.strftime("%Y-%m-%d %H:%M:%S")
                                use_gte = True
                        except Exception:
                            pass
                        op = ">=" if use_gte else ">"
                        bars = conn.execute(
                            f"SELECT date, open, high, low, close FROM ohlcv WHERE ticker = ? AND timeframe = '15m' AND date {op} ? AND date <= ? ORDER BY date ASC",
                            (sym, min_bar_s, completed_15m_cutoff),
                        ).fetchall()
                    if bars:
                        first_b = bars[0]
                        return ltp, first_b, float(first_b[2]), float(first_b[3])
                    return ltp, None, None, None

                # Open position evaluation: check ALL completed bars since fill/entry slot
                entry_bar_dt = djson.get("entry_bar_dt")
                entry_slot_dt = djson.get("entry_slot_dt")
                if entry_bar_dt:
                    op = ">"
                    min_bar_s = str(entry_bar_dt)[:19]
                elif entry_slot_dt:
                    op = ">="
                    min_bar_s = str(entry_slot_dt)[:19]
                else:
                    min_bar_s = str(after_ts)[:19]
                    use_gte = False
                    try:
                        adt = datetime.strptime(min_bar_s, "%Y-%m-%d %H:%M:%S")
                        slot_dt = adt.replace(minute=(adt.minute // 15) * 15, second=0, microsecond=0)
                        if prod_type == "MIS" or (0.0 <= (adt - slot_dt).total_seconds() <= 90.0):
                            min_bar_s = slot_dt.strftime("%Y-%m-%d %H:%M:%S")
                            use_gte = True
                    except Exception:
                        pass
                    op = ">=" if use_gte else ">"

                bars = conn.execute(
                    f"SELECT date, open, high, low, close FROM ohlcv WHERE ticker = ? AND timeframe = '15m' AND date {op} ? AND date <= ? ORDER BY date ASC",
                    (sym, min_bar_s, completed_15m_cutoff),
                ).fetchall()

                if bars:
                    latest_b = bars[-1]
                    max_high = max(float(b[2]) for b in bars)
                    min_low = min(float(b[3]) for b in bars)
                    return ltp, latest_b, max_high, min_low

                if prod_type == "CNC":
                    row_1d = conn.execute(
                        "SELECT date, open, high, low, close FROM ohlcv WHERE ticker = ? AND timeframe = '1d' AND substr(date,1,10) > ? AND substr(date,1,10) <= ? ORDER BY date DESC LIMIT 1",
                        (sym, str(after_ts)[:10], today_s),
                    ).fetchone()
                    if row_1d:
                        return ltp, row_1d, float(row_1d[2]), float(row_1d[3])

                return ltp, None, None, None

            def _resolve_forming_15m_open(sym: str) -> Optional[float]:
                q = quotes.get(sym) or quotes.get(sym.replace(".NS", ""))
                if isinstance(q, dict):
                    for k in ("bar_open", "forming_open", "15m_open"):
                        v = q.get(k)
                        if isinstance(v, (int, float)) and float(v) > 0.0:
                            return float(v)
                    if slot_start_dt.strftime("%H:%M") == "09:15":
                        v_open = q.get("open")
                        if isinstance(v_open, (int, float)) and float(v_open) > 0.0:
                            return float(v_open)
                row_1m = conn.execute(
                    "SELECT open FROM ohlcv WHERE ticker = ? AND timeframe = '1m' AND date >= ? AND date <= ? ORDER BY date ASC LIMIT 1",
                    (sym, slot_start_s, now_s),
                ).fetchone()
                if row_1m and float(row_1m[0] or 0.0) > 0.0:
                    return float(row_1m[0])
                try:
                    from app.data.upstox_stream_service import get_stream_service
                    f_op = get_stream_service().get_forming_15m_open(sym, as_of_dt=ts)
                    if f_op is not None and float(f_op) > 0.0:
                        return float(f_op)
                except Exception:
                    pass
                return None

            def _evaluate_open_positions(sid_val: str, pos_rows: List[sqlite3.Row]) -> int:
                closed_cnt = 0
                for pos in pos_rows:
                    pid = pos["position_id"]
                    sym = pos["ticker"]
                    sdir = pos["direction"]
                    sh = int(pos["shares"])
                    epx = float(pos["entry_price"])
                    sl = float(pos["stop_loss_px"])
                    prod = pos["product_type"]
                    ltp, brow, max_high, min_low = _latest_px_and_bars(
                        sym, pos["entry_time"], prod, pos["details_json"], is_pending=False,
                    )

                    curr_px = ltp
                    if curr_px is None and brow is not None:
                        curr_px = float(brow[4])
                    if curr_px is None:
                        curr_px = float(pos["current_price"])

                    exit_reason = None
                    exit_px = curr_px
                    if pos["status"] == "EXIT_RETRY_PENDING" and ("09:15" <= hm_now <= "15:29"):
                        exit_reason = "RETRY_PENDING_BROKER_EXIT"
                    elif prod == "MIS" and sym and str(pos["entry_time"])[:10] < today_s:
                        exit_reason = "OVERDUE_SESSION_SQUAREOFF"
                    elif prod == "MIS" and hm_now >= "15:10":
                        exit_reason = "MANDATORY_1510_MIS_SQUAREOFF"
                    elif sdir == "SELL" and ((max_high is not None and max_high >= sl) or curr_px >= sl):
                        exit_reason = "STRUCTURAL_STOP_HIT"
                        exit_px = max(sl, curr_px) if ltp is not None else sl
                    elif sdir == "BUY" and sl > 0 and ((min_low is not None and min_low <= sl) or curr_px <= sl):
                        exit_reason = "STRUCTURAL_STOP_HIT"
                        exit_px = min(sl, curr_px) if ltp is not None else sl
                    elif prod == "CNC" and pos["book"] == "BOOK1_SWING_CNC_CORE":
                        bd_reason = self._check_book1_intracycle_breakdown(
                            conn=conn,
                            sym=sym,
                            entry_date_s=str(pos["entry_time"])[:10],
                            curr_px=curr_px,
                            today_s=today_s,
                            hm_now=hm_now,
                            allow_live_finbert=allow_live_finbert,
                        )
                        if bd_reason:
                            exit_reason = bd_reason

                    if exit_reason and pos["status"] != "EXIT_RETRY_PENDING":
                        cres = self._close_position_in_tx(conn, pos, exit_px, exit_reason, now_s, today_s, session_id=sid_val)
                        if cres.get("closed", True):
                            closed_cnt += 1
                    else:
                        unreal = ((curr_px - epx) if sdir == "BUY" else (epx - curr_px)) * sh
                        conn.execute(
                            "UPDATE consensus_v3_positions SET current_price = ?, unrealized_pnl_inr = ? WHERE position_id = ?",
                            (round(curr_px, 2), round(unreal, 2), pid),
                        )
                return closed_cnt

            for sid in sessions_to_run:
                pend = [r for r in all_pend if r["session_id"] == sid]
                r_key = self._risk_date_key(sid, today_s)

                # 1. Evaluate pre-existing open positions FIRST so same-cycle SL losses & circuit breaker
                #    updates are reflected immediately before evaluating pending fills.
                pre_open_pos = [r for r in all_open if r["session_id"] == sid]
                if pre_open_pos:
                    total_closed += _evaluate_open_positions(sid, pre_open_pos)

                newly_opened_pids: List[str] = []
                for pr in pend:
                    oid = pr["order_id"]
                    sym = pr["ticker"]
                    lim = float(pr["limit_price"])
                    sdir = pr["direction"]

                    # ── BRANCH A: LIVE_UPSTOX REAL BROKER ORDER RECONCILIATION ──
                    if pr["execution_env"] == "LIVE_UPSTOX":
                        boid = str(pr["broker_order_id"] or "")
                        b_ord = live_orders_by_id.get(boid)
                        ost = str((b_ord or {}).get("status") or "").lower()
                        filled_qty = int(float((b_ord or {}).get("filled_quantity") or 0))
                        avg_px = round_nse_eq_tick(float((b_ord or {}).get("average_price") or lim))
                        is_expired_time = now_s >= str(pr["expires_at"]) or (pr["product_type"] == "MIS" and hm_now >= "14:55")

                        def _with_entry_slot_json(raw_json: Optional[str]) -> str:
                            dj: Dict[str, Any] = {}
                            if raw_json:
                                try:
                                    parsed = json.loads(raw_json)
                                    if isinstance(parsed, dict):
                                        dj = parsed
                                except Exception:
                                    dj = {}
                            dj["entry_slot_dt"] = _slot_floor_str(pr["created_at"])
                            dj["filled_via"] = "LIVE_UPSTOX"
                            return json.dumps(dj)

                        if ost == "complete" and filled_qty >= 1:
                            pid = f"pos_{oid[4:]}"
                            notional = round(filled_qty * avg_px, 2)
                            ratio_f = min(1.0, filled_qty / max(1, int(pr["shares"])))
                            m_used = round(float(pr["reserved_cash_inr"]) * ratio_f, 2)
                            eff_lev = float(pr["effective_leverage"] or 1.0)
                            m_src = str(pr["margin_source"] or "UPSTOX_API_VAR_ELM")
                            conn.execute(
                                "UPDATE consensus_v3_pending_orders SET status = 'FILLED', resolved_at = ?, resolution_note = 'Upstox order complete' WHERE order_id = ?",
                                (now_s, oid),
                            )
                            conn.execute(
                                """
                                INSERT INTO consensus_v3_positions (
                                    position_id, session_id, order_id, book, product_type, ticker, symbol,
                                    instrument_key, direction, shares, entry_price, invested_notional_inr,
                                    margin_used_inr, effective_leverage, margin_source,
                                    stop_loss_px, scheduled_exit, status, execution_env, current_price,
                                    unrealized_pnl_inr, entry_time, details_json
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 'LIVE_UPSTOX', ?, 0.0, ?, ?)
                                """,
                                (
                                    pid, sid, oid, pr["book"], pr["product_type"], sym, pr["symbol"],
                                    pr["instrument_key"], sdir, filled_qty, avg_px, notional,
                                    m_used, eff_lev, m_src, float(pr["stop_loss_px"]),
                                    pr["scheduled_exit_hm"] or "15:10", avg_px, now_s, _with_entry_slot_json(pr["details_json"]),
                                ),
                            )
                            conn.execute(
                                """
                                INSERT INTO consensus_v3_risk_ledger (session_date, realized_pnl_inr, fills_today, circuit_breaker_tripped, updated_at)
                                VALUES (?, 0.0, 1, 0, ?)
                                ON CONFLICT(session_date) DO UPDATE SET fills_today = fills_today + 1, updated_at = excluded.updated_at
                                """,
                                (r_key, now_s),
                            )
                            total_filled += 1
                            newly_opened_pids.append(pid)
                            continue

                        if ost in ("rejected", "cancelled", "canceled"):
                            if filled_qty >= 1:
                                pid = f"pos_{oid[4:]}"
                                notional = round(filled_qty * avg_px, 2)
                                ratio_f = min(1.0, filled_qty / max(1, int(pr["shares"])))
                                m_used = round(float(pr["reserved_cash_inr"]) * ratio_f, 2)
                                conn.execute(
                                    "UPDATE consensus_v3_pending_orders SET status = 'PARTIAL_FILLED_CANCELLED', resolved_at = ?, resolution_note = ? WHERE order_id = ?",
                                    (now_s, f"Partial {filled_qty} sh filled before {ost}", oid),
                                )
                                conn.execute(
                                    """
                                    INSERT INTO consensus_v3_positions (
                                        position_id, session_id, order_id, book, product_type, ticker, symbol,
                                        instrument_key, direction, shares, entry_price, invested_notional_inr,
                                        margin_used_inr, effective_leverage, margin_source,
                                        stop_loss_px, scheduled_exit, status, execution_env, current_price,
                                        unrealized_pnl_inr, entry_time, details_json
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 'LIVE_UPSTOX', ?, 0.0, ?, ?)
                                    """,
                                    (
                                        pid, sid, oid, pr["book"], pr["product_type"], sym, pr["symbol"],
                                        pr["instrument_key"], sdir, filled_qty, avg_px, notional,
                                        m_used, float(pr["effective_leverage"] or 1.0), str(pr["margin_source"] or "UPSTOX_API_VAR_ELM"),
                                        float(pr["stop_loss_px"]), pr["scheduled_exit_hm"] or "15:10", avg_px, now_s, _with_entry_slot_json(pr["details_json"]),
                                    ),
                                )
                                conn.execute(
                                    """
                                    INSERT INTO consensus_v3_risk_ledger (session_date, realized_pnl_inr, fills_today, circuit_breaker_tripped, updated_at)
                                    VALUES (?, 0.0, 1, 0, ?)
                                    ON CONFLICT(session_date) DO UPDATE SET fills_today = fills_today + 1, updated_at = excluded.updated_at
                                    """,
                                    (r_key, now_s),
                                )
                                total_filled += 1
                                newly_opened_pids.append(pid)
                            else:
                                msg_b = str((b_ord or {}).get("status_message") or ost)[:160]
                                conn.execute(
                                    "UPDATE consensus_v3_pending_orders SET status = 'CANCELLED_BROKER', resolved_at = ?, resolution_note = ? WHERE order_id = ?",
                                    (now_s, msg_b, oid),
                                )
                                total_expired += 1
                            continue

                        if is_expired_time:
                            c_res = self._cancel_upstox_order(boid)
                            if c_res.get("cancelled"):
                                # Re-check partial fill right after cancel
                                ref_map, _ = self._fetch_upstox_day_orders_map(force=True)
                                ref_ord = ref_map.get(boid) or b_ord or {}
                                f_qty = int(float(ref_ord.get("filled_quantity") or 0))
                                if f_qty >= 1:
                                    f_px = round_nse_eq_tick(float(ref_ord.get("average_price") or lim))
                                    pid = f"pos_{oid[4:]}"
                                    notional = round(f_qty * f_px, 2)
                                    ratio_f = min(1.0, f_qty / max(1, int(pr["shares"])))
                                    m_used = round(float(pr["reserved_cash_inr"]) * ratio_f, 2)
                                    conn.execute(
                                        "UPDATE consensus_v3_pending_orders SET status = 'PARTIAL_FILLED_CANCELLED', resolved_at = ?, resolution_note = '15m TIF cancelled after partial fill' WHERE order_id = ?",
                                        (now_s, oid),
                                    )
                                    conn.execute(
                                        """
                                        INSERT INTO consensus_v3_positions (
                                            position_id, session_id, order_id, book, product_type, ticker, symbol,
                                            instrument_key, direction, shares, entry_price, invested_notional_inr,
                                            margin_used_inr, effective_leverage, margin_source,
                                            stop_loss_px, scheduled_exit, status, execution_env, current_price,
                                            unrealized_pnl_inr, entry_time, details_json
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 'LIVE_UPSTOX', ?, 0.0, ?, ?)
                                        """,
                                        (
                                            pid, sid, oid, pr["book"], pr["product_type"], sym, pr["symbol"],
                                            pr["instrument_key"], sdir, f_qty, f_px, notional,
                                            m_used, float(pr["effective_leverage"] or 1.0), str(pr["margin_source"] or "UPSTOX_API_VAR_ELM"),
                                            float(pr["stop_loss_px"]), pr["scheduled_exit_hm"] or "15:10", f_px, now_s, _with_entry_slot_json(pr["details_json"]),
                                        ),
                                    )
                                    conn.execute(
                                        """
                                        INSERT INTO consensus_v3_risk_ledger (session_date, realized_pnl_inr, fills_today, circuit_breaker_tripped, updated_at)
                                        VALUES (?, 0.0, 1, 0, ?)
                                        ON CONFLICT(session_date) DO UPDATE SET fills_today = fills_today + 1, updated_at = excluded.updated_at
                                        """,
                                        (r_key, now_s),
                                    )
                                    total_filled += 1
                                    newly_opened_pids.append(pid)
                                else:
                                    conn.execute(
                                        "UPDATE consensus_v3_pending_orders SET status = 'EXPIRED_UNFILLED', resolved_at = ?, resolution_note = 'Upstox order cancelled at 15m TIF; cash released' WHERE order_id = ?",
                                        (now_s, oid),
                                    )
                                    total_expired += 1
                        continue

                    # ── BRANCH B: PAPER_SHADOW STRICT POST-ONLY OHLCV/QUOTE RECONCILIATION ──
                    is_expired_time = now_s >= str(pr["expires_at"]) or (pr["product_type"] == "MIS" and hm_now >= "14:55")
                    ltp, brow, _, _ = _latest_px_and_bars(
                        sym, pr["created_at"], pr["product_type"], pr["details_json"], is_pending=True,
                    )
                    is_filled = False
                    filled_via_brow = False
                    post_only_violated = False
                    brow_dt_s = None
                    if brow is not None:
                        brow_dt_s = str(brow[0])[:19]
                        brow_valid_for_order = True
                        try:
                            brow_end_dt = datetime.strptime(brow_dt_s, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=15)
                            exp_limit_dt = datetime.strptime(str(pr["expires_at"])[:19], "%Y-%m-%d %H:%M:%S")
                            if pr["product_type"] == "MIS":
                                exp_limit_dt = min(exp_limit_dt, exp_limit_dt.replace(hour=14, minute=55, second=0, microsecond=0))
                            if brow_end_dt > exp_limit_dt:
                                brow_valid_for_order = False
                        except Exception:
                            brow_valid_for_order = True

                        if brow_valid_for_order:
                            bo, bh, bl = float(brow[1]), float(brow[2]), float(brow[3])
                            if sdir == "SELL":
                                if bo >= lim:
                                    post_only_violated = True
                                elif bh >= lim * 1.0003:
                                    is_filled = True
                                    filled_via_brow = True
                            else:
                                if bo <= lim:
                                    post_only_violated = True
                                elif bl <= lim * 0.9997:
                                    is_filled = True
                                    filled_via_brow = True

                    if (not is_expired_time) and (not post_only_violated) and (not is_filled):
                        if brow is None or (brow_dt_s is not None and brow_dt_s < slot_start_s):
                            forming_open = _resolve_forming_15m_open(sym)
                            if forming_open is not None:
                                if sdir == "SELL" and forming_open >= lim:
                                    post_only_violated = True
                                elif sdir == "BUY" and forming_open <= lim:
                                    post_only_violated = True

                        if not post_only_violated and ltp is not None:
                            if sdir == "SELL" and ltp >= lim * 1.0003:
                                is_filled = True
                            elif sdir == "BUY" and ltp <= lim * 0.9997:
                                is_filled = True

                    if post_only_violated:
                        conn.execute(
                            "UPDATE consensus_v3_pending_orders SET status = 'CANCELLED_POSTONLY_GAP', resolved_at = ?, resolution_note = 'Next bar gapped past post-only limit' WHERE order_id = ?",
                            (now_s, oid),
                        )
                        total_expired += 1
                        continue

                    if is_filled:
                        # M2 Advisory 1: Strict daily fills_today < 6 and circuit-breaker check before filling
                        if pr["book"] == "BOOK2_INTRADAY_MIS_SNIFFER":
                            rl_row = conn.execute(
                                "SELECT fills_today, circuit_breaker_tripped FROM consensus_v3_risk_ledger WHERE session_date = ?",
                                (r_key,),
                            ).fetchone()
                            if rl_row and bool(rl_row["circuit_breaker_tripped"]):
                                conn.execute(
                                    "UPDATE consensus_v3_pending_orders SET status = 'CANCELLED_CIRCUIT_BREAKER', resolved_at = ?, resolution_note = 'Session daily-loss circuit breaker tripped' WHERE order_id = ?",
                                    (now_s, oid),
                                )
                                total_expired += 1
                                continue
                            if rl_row and int(rl_row["fills_today"] or 0) >= 6:
                                conn.execute(
                                    "UPDATE consensus_v3_pending_orders SET status = 'CANCELLED_DAILY_FILL_CAP', resolved_at = ?, resolution_note = 'Daily MIS fill cap (6) reached' WHERE order_id = ?",
                                    (now_s, oid),
                                )
                                total_expired += 1
                                continue

                            # M2 Advisory 2: Recompute live DB equity, free cash, and 1.0x MIS notional after any SL losses
                            st_cur = conn.execute(
                                "SELECT funded_capital_inr, realized_pnl_inr FROM consensus_v3_arena_state WHERE session_id = ?",
                                (sid,),
                            ).fetchone()
                            open_cur = conn.execute(
                                "SELECT book, margin_used_inr, invested_notional_inr, unrealized_pnl_inr FROM consensus_v3_positions WHERE session_id = ? AND status IN ('OPEN', 'EXIT_RETRY_PENDING')",
                                (sid,),
                            ).fetchall()
                            other_pend = conn.execute(
                                "SELECT book, reserved_cash_inr, exposure_notional_inr, shares, limit_price FROM consensus_v3_pending_orders WHERE session_id = ? AND status = 'PENDING_RESERVED' AND order_id != ?",
                                (sid, oid),
                            ).fetchall()
                            eq_cur = (
                                float(st_cur["funded_capital_inr"])
                                + float(st_cur["realized_pnl_inr"])
                                + sum(float(r["unrealized_pnl_inr"]) for r in open_cur)
                            )
                            other_margin = (
                                sum(
                                    float(r["margin_used_inr"] if r["margin_used_inr"] is not None else r["invested_notional_inr"])
                                    for r in open_cur
                                )
                                + sum(float(r["reserved_cash_inr"]) for r in other_pend)
                            )
                            avail_cash_for_pr = eq_cur - other_margin
                            b2_other_notional = (
                                sum(
                                    float(r["invested_notional_inr"])
                                    for r in open_cur
                                    if r["book"] == "BOOK2_INTRADAY_MIS_SNIFFER"
                                )
                                + sum(
                                    float(
                                        r["exposure_notional_inr"]
                                        if r["exposure_notional_inr"] is not None
                                        else float(r["shares"]) * float(r["limit_price"])
                                    )
                                    for r in other_pend
                                    if r["book"] == "BOOK2_INTRADAY_MIS_SNIFFER"
                                )
                            )
                            pr_notional = round(int(pr["shares"]) * lim, 2)
                            if avail_cash_for_pr + 1e-6 < float(pr["reserved_cash_inr"]) or (b2_other_notional + pr_notional) > (eq_cur + 1.0):
                                conn.execute(
                                    "UPDATE consensus_v3_pending_orders SET status = 'CANCELLED_POST_SL_CASH_SHORTFALL', resolved_at = ?, resolution_note = 'Post-SL equity/margin shortfall prevented fill' WHERE order_id = ?",
                                    (now_s, oid),
                                )
                                total_expired += 1
                                continue

                        pid = f"pos_{oid[4:]}"
                        sh = int(pr["shares"])
                        notional = round(sh * lim, 2)
                        pr_keys = pr.keys()
                        m_used = float(pr["reserved_cash_inr"])
                        eff_lev = float(pr["effective_leverage"]) if ("effective_leverage" in pr_keys and pr["effective_leverage"]) else 1.0
                        m_src = str(pr["margin_source"]) if ("margin_source" in pr_keys and pr["margin_source"]) else "CASH_1X_EXACT"

                        pos_details: Dict[str, Any] = {}
                        if pr["details_json"]:
                            try:
                                parsed_d = json.loads(pr["details_json"])
                                if isinstance(parsed_d, dict):
                                    pos_details = parsed_d
                            except Exception:
                                pos_details = {}
                        if filled_via_brow and brow_dt_s:
                            pos_details["entry_bar_dt"] = brow_dt_s
                            pos_details["filled_via"] = "BAR"
                        else:
                            pos_details["entry_slot_dt"] = slot_start_s
                            pos_details["filled_via"] = "LTP"

                        conn.execute(
                            "UPDATE consensus_v3_pending_orders SET status = 'FILLED', resolved_at = ?, resolution_note = 'Limit trade-through verified' WHERE order_id = ?",
                            (now_s, oid),
                        )
                        conn.execute(
                            """
                            INSERT INTO consensus_v3_positions (
                                position_id, session_id, order_id, book, product_type, ticker, symbol,
                                instrument_key, direction, shares, entry_price, invested_notional_inr,
                                margin_used_inr, effective_leverage, margin_source,
                                stop_loss_px, scheduled_exit, status, execution_env, current_price,
                                unrealized_pnl_inr, entry_time, details_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, 0.0, ?, ?)
                            """,
                            (
                                pid, sid, oid, pr["book"], pr["product_type"], sym, pr["symbol"],
                                pr["instrument_key"], sdir, sh, lim, notional,
                                m_used, eff_lev, m_src,
                                float(pr["stop_loss_px"]),
                                pr["scheduled_exit_hm"] or "15:10", pr["execution_env"], lim, now_s, json.dumps(pos_details),
                            ),
                        )
                        conn.execute(
                            """
                            INSERT INTO consensus_v3_risk_ledger (session_date, realized_pnl_inr, fills_today, circuit_breaker_tripped, updated_at)
                            VALUES (?, 0.0, 1, 0, ?)
                            ON CONFLICT(session_date) DO UPDATE SET fills_today = fills_today + 1, updated_at = excluded.updated_at
                            """,
                            (r_key, now_s),
                        )
                        total_filled += 1

                        # Bug 5: Check same-bar intrabar SL breach immediately when filled via completed bar `brow`!
                        same_bar_closed = False
                        if filled_via_brow and brow is not None:
                            sl_px = float(pr["stop_loss_px"])
                            bh_f, bl_f = float(brow[2]), float(brow[3])
                            same_bar_sl_hit = (sdir == "SELL" and bh_f >= sl_px) or (sdir == "BUY" and sl_px > 0 and bl_f <= sl_px)
                            if same_bar_sl_hit:
                                pos_row = conn.execute(
                                    "SELECT * FROM consensus_v3_positions WHERE position_id = ?",
                                    (pid,),
                                ).fetchone()
                                if pos_row:
                                    cres = self._close_position_in_tx(
                                        conn, pos_row, sl_px, "STRUCTURAL_STOP_HIT", now_s, today_s, session_id=sid,
                                    )
                                    if cres.get("closed", True):
                                        total_closed += 1
                                        same_bar_closed = True
                        if not same_bar_closed:
                            newly_opened_pids.append(pid)
                        continue

                    if now_s >= str(pr["expires_at"]) or (pr["product_type"] == "MIS" and hm_now >= "14:55"):
                        conn.execute(
                            "UPDATE consensus_v3_pending_orders SET status = 'EXPIRED_UNFILLED', resolved_at = ?, resolution_note = '15m TIF expired; reserved cash released' WHERE order_id = ?",
                            (now_s, oid),
                        )
                        total_expired += 1

                # Evaluate any newly opened positions (LTP fills / LIVE_UPSTOX / surviving BAR fills) on this tick
                if newly_opened_pids:
                    placeholders = ",".join("?" for _ in newly_opened_pids)
                    new_open_rows = conn.execute(
                        f"SELECT * FROM consensus_v3_positions WHERE position_id IN ({placeholders}) AND status IN ('OPEN', 'EXIT_RETRY_PENDING')",
                        tuple(newly_opened_pids),
                    ).fetchall()
                    if new_open_rows:
                        total_closed += _evaluate_open_positions(sid, new_open_rows)

                # Update Peak Equity, Unit NAV & Max Drawdown for this session
                st = conn.execute("SELECT * FROM consensus_v3_arena_state WHERE session_id = ?", (sid,)).fetchone()
                rem_unreal = conn.execute(
                    "SELECT COALESCE(SUM(unrealized_pnl_inr), 0.0) FROM consensus_v3_positions WHERE session_id = ? AND status IN ('OPEN', 'EXIT_RETRY_PENDING')",
                    (sid,),
                ).fetchone()[0]
                eq_now = float(st["funded_capital_inr"]) + float(st["realized_pnl_inr"]) + float(rem_unreal)
                nav_u = float(st["nav_units"]) if ("nav_units" in st.keys() and st["nav_units"]) else round(float(st["funded_capital_inr"]) / 100.0, 6)
                u_nav = round(eq_now / max(1e-6, nav_u), 4)
                pk = max(float(st["peak_equity_inr"]), eq_now)
                dd = max(float(st["max_drawdown_pct"]), ((pk - eq_now) / max(1.0, pk)) * 100.0)
                conn.execute(
                    "UPDATE consensus_v3_arena_state SET peak_equity_inr = ?, unit_nav = ?, max_drawdown_pct = ?, updated_at = ? WHERE session_id = ?",
                    (round(pk, 2), u_nav, round(dd, 2), now_s, sid),
                )
            conn.commit()

        return {"filled": total_filled, "expired": total_expired, "closed": total_closed}

    def _close_position_in_tx(
        self,
        conn: sqlite3.Connection,
        pos: sqlite3.Row,
        exit_px: float,
        exit_reason: str,
        now_s: str,
        today_s: str,
        is_emergency: bool = False,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        pid = pos["position_id"]
        sid = session_id or pos["session_id"]
        sdir = pos["direction"]
        sh = int(pos["shares"])
        epx = float(pos["entry_price"])
        prod = pos["product_type"]
        clean_xpx = round_nse_eq_tick(exit_px)

        if pos["execution_env"] == "LIVE_UPSTOX":
            opp = "BUY" if sdir == "SELL" else "SELL"
            route_res = self._route_upstox_equity_order(
                order_or_pos_id=f"{pid}_{int(time.time()) % 10000}",
                action=opp,
                product_type=prod,
                order_type="MARKET",
                ticker=pos["ticker"],
                instrument_key=pos["instrument_key"] or "",
                shares=sh,
                price=clean_xpx,
                is_emergency=is_emergency,
                is_exit=True,
            )
            if not route_res.get("routed"):
                err_txt = str(route_res.get("error") or route_res.get("state") or "BROKER_EXIT_REJECTED")[:160]
                unreal = ((clean_xpx - epx) if sdir == "BUY" else (epx - clean_xpx)) * sh
                conn.execute(
                    """
                    UPDATE consensus_v3_positions
                    SET status = 'EXIT_RETRY_PENDING', current_price = ?, unrealized_pnl_inr = ?,
                        exit_reason = ?
                    WHERE position_id = ?
                    """,
                    (clean_xpx, round(unreal, 2), f"EXIT_RETRY_PENDING ({exit_reason}): {err_txt}", pid),
                )
                return {
                    "position_id": pid,
                    "closed": False,
                    "status": "EXIT_RETRY_PENDING",
                    "error": err_txt,
                    "exit_px": clean_xpx,
                    "net_pnl_inr": 0.0,
                    "fees_inr": 0.0,
                }
            if route_res.get("avg_fill_price"):
                clean_xpx = round_nse_eq_tick(float(route_res["avg_fill_price"]))

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
            WHERE position_id = ? AND status IN ('OPEN', 'EXIT_RETRY_PENDING')
            """,
            (clean_xpx, clean_xpx, now_s, exit_reason, round(gross, 2), fees, net, pid),
        )
        conn.execute(
            "UPDATE consensus_v3_arena_state SET realized_pnl_inr = realized_pnl_inr + ?, updated_at = ? WHERE session_id = ?",
            (net, now_s, sid),
        )

        st = conn.execute("SELECT funded_capital_inr FROM consensus_v3_arena_state WHERE session_id = ?", (sid,)).fetchone()
        halt_floor = -round(float(st["funded_capital_inr"]) * DAILY_LOSS_HALT_FRACTION, 2)
        r_key = self._risk_date_key(sid, today_s)
        conn.execute(
            """
            INSERT INTO consensus_v3_risk_ledger (session_date, realized_pnl_inr, fills_today, circuit_breaker_tripped, updated_at)
            VALUES (?, ?, 0, 0, ?)
            ON CONFLICT(session_date) DO UPDATE SET
                realized_pnl_inr = realized_pnl_inr + excluded.realized_pnl_inr,
                updated_at = excluded.updated_at
            """,
            (r_key, net, now_s),
        )
        r_led = conn.execute("SELECT realized_pnl_inr FROM consensus_v3_risk_ledger WHERE session_date = ?", (r_key,)).fetchone()
        if r_led and float(r_led[0]) <= halt_floor:
            conn.execute(
                "UPDATE consensus_v3_risk_ledger SET circuit_breaker_tripped = 1, trip_reason = ? WHERE session_date = ?",
                (f"Daily realized loss ₹{float(r_led[0]):.0f} breached halt ₹{halt_floor:.0f}", r_key),
            )
        return {"position_id": pid, "closed": True, "status": "CLOSED", "exit_px": clean_xpx, "net_pnl_inr": net, "fees_inr": fees}

    def manual_exit_position(self, position_id: str, reason: str = "MANUAL_OPERATOR_EXIT") -> Dict[str, Any]:
        now_dt = datetime.now()
        now_s = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        today_s = now_dt.strftime("%Y-%m-%d")
        with self._lock, self._conn() as conn:
            pos = conn.execute(
                """
                SELECT * FROM consensus_v3_positions
                WHERE position_id = ? AND session_id IN (?, ?) AND status IN ('OPEN', 'EXIT_RETRY_PENDING')
                """,
                (position_id, self.shadow_session_id, self.live_session_id),
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
            res = self._close_position_in_tx(conn, pos, xpx, reason, now_s, today_s, session_id=pos["session_id"])
            conn.commit()
        if not res.get("closed", True):
            return {"status": "EXIT_RETRY_PENDING", "message": res.get("error"), **res}
        return {"status": "CLOSED", **res}

    def execute_sos_panic_exit(self) -> Dict[str, Any]:
        """
        Emergency 1-Click Flatten & Disarm:
          - Cancels all live Upstox resting orders via `/v2/order/cancel` before marking `CANCELLED_SOS`.
          - Flattens open positions in the active ledger (`is_emergency=True`) and disarms Live Bridge.
        """
        now_dt = datetime.now()
        now_s = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        today_s = now_dt.strftime("%Y-%m-%d")
        flattened = []
        retry_pending = []
        with self._lock:
            was_live = not self.simulation_mode
            self.disarm_live_trading()
            with self._conn() as conn:
                has_live = was_live or self._has_open_live_exposure(conn)
                target_sids = [self.live_session_id] if has_live else [self.shadow_session_id]

                pend_rows = conn.execute(
                    f"SELECT * FROM consensus_v3_pending_orders WHERE session_id IN ({','.join('?' for _ in target_sids)}) AND status = 'PENDING_RESERVED'",
                    tuple(target_sids),
                ).fetchall()
                for pr in pend_rows:
                    if pr["execution_env"] == "LIVE_UPSTOX" and pr["broker_order_id"]:
                        c_res = self._cancel_upstox_order(pr["broker_order_id"])
                        if not c_res.get("cancelled"):
                            continue
                    conn.execute(
                        """
                        UPDATE consensus_v3_pending_orders
                        SET status = 'CANCELLED_SOS', resolved_at = ?, resolution_note = 'SOS Panic Exit cancelled pending reservation'
                        WHERE order_id = ?
                        """,
                        (now_s, pr["order_id"]),
                    )

                open_rows = conn.execute(
                    f"SELECT * FROM consensus_v3_positions WHERE session_id IN ({','.join('?' for _ in target_sids)}) AND status IN ('OPEN', 'EXIT_RETRY_PENDING')",
                    tuple(target_sids),
                ).fetchall()
                for pos in open_rows:
                    xpx = float(pos["current_price"] or pos["entry_price"])
                    cres = self._close_position_in_tx(conn, pos, xpx, "SOS_PANIC_EXIT", now_s, today_s, is_emergency=True, session_id=pos["session_id"])
                    if cres.get("closed", True):
                        flattened.append(cres)
                    else:
                        retry_pending.append(cres)
                conn.commit()
        return {
            "status": "SOS_FLATTENED_AND_DISARMED" if not retry_pending else "SOS_PARTIAL_EXIT_RETRY_PENDING",
            "simulation_mode": True,
            "flattened_count": len(flattened),
            "exit_retry_pending_count": len(retry_pending),
            "flattened_positions": flattened,
            "retry_pending_positions": retry_pending,
        }

    def get_positions_and_reservations(self, limit: int = 100) -> Dict[str, Any]:
        acct = self.get_arena_accounting()
        sid = acct["session_id"]
        with self._conn() as conn:
            pend = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM consensus_v3_pending_orders WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                    (sid, limit),
                ).fetchall()
            ]
            open_p = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND status IN ('OPEN', 'EXIT_RETRY_PENDING') ORDER BY entry_time DESC",
                    (sid,),
                ).fetchall()
            ]
            closed_p = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND status = 'CLOSED' ORDER BY exit_time DESC LIMIT ?",
                    (sid, limit),
                ).fetchall()
            ]
            flows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM consensus_v3_capital_flows WHERE session_id = ? ORDER BY created_at DESC LIMIT 20",
                    (sid,),
                ).fetchall()
            ]
        for p in open_p:
            p["sector"] = resolve_fno_sector(p.get("symbol", ""))
        return {
            "accounting": acct,
            "pending_reserved_orders": [p for p in pend if p["status"] == "PENDING_RESERVED"],
            "recent_order_reservations": pend,
            "open_positions": open_p,
            "closed_positions": closed_p,
            "capital_flows": flows,
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

    def get_equity_curve_and_calendar(self) -> Dict[str, Any]:
        """
        Builds the strictly era-isolated Prospective V3 Equity Curve & Session P&L Calendar
        directly from `consensus_v3_arena_state` and `consensus_v3_positions` (zero V1/V2 replay contamination).
        """
        acct = self.get_arena_accounting()
        sid = acct["session_id"]
        funded = float(acct["funded_capital_inr"])
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._conn() as conn:
            st = conn.execute(
                "SELECT updated_at FROM consensus_v3_arena_state WHERE session_id = ?",
                (sid,),
            ).fetchone()
            closed_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND status = 'CLOSED' ORDER BY exit_time ASC, position_id ASC",
                    (sid,),
                ).fetchall()
            ]

        start_ts = str(st["updated_at"]) if st and st["updated_at"] else now_s
        curve: List[Dict[str, Any]] = [
            {
                "idx": 0,
                "label": "Funded Baseline",
                "timestamp": start_ts,
                "symbol": "START",
                "equity_inr": round(funded, 2),
                "cum_net_pnl_inr": 0.0,
                "trade_net_pnl_inr": 0.0,
                "drawdown_pct": 0.0,
            }
        ]

        cum_net = 0.0
        peak_eq = funded
        by_date: Dict[str, Dict[str, Any]] = {}

        for i, row in enumerate(closed_rows, start=1):
            net = float(row.get("net_pnl_inr") or 0.0)
            gross = float(row.get("gross_pnl_inr") or 0.0)
            fees = float(row.get("fees_inr") or 0.0)
            cum_net += net
            eq = funded + cum_net
            peak_eq = max(peak_eq, eq)
            dd = ((peak_eq - eq) / max(1.0, peak_eq)) * 100.0
            exit_ts = str(row.get("exit_time") or now_s)
            d_key = exit_ts[:10]

            curve.append(
                {
                    "idx": i,
                    "label": f"{d_key[5:]} {exit_ts[11:16]} ({row.get('symbol')})",
                    "timestamp": exit_ts,
                    "symbol": row.get("symbol"),
                    "book": row.get("book"),
                    "equity_inr": round(eq, 2),
                    "cum_net_pnl_inr": round(cum_net, 2),
                    "trade_net_pnl_inr": round(net, 2),
                    "drawdown_pct": round(dd, 2),
                }
            )

            bucket = by_date.setdefault(
                d_key,
                {
                    "date": d_key,
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "gross_pnl_inr": 0.0,
                    "fees_inr": 0.0,
                    "net_pnl_inr": 0.0,
                    "book1_net_inr": 0.0,
                    "book2_net_inr": 0.0,
                    "items": [],
                },
            )
            bucket["trades"] += 1
            if net > 0:
                bucket["wins"] += 1
            else:
                bucket["losses"] += 1
            bucket["gross_pnl_inr"] = round(bucket["gross_pnl_inr"] + gross, 2)
            bucket["fees_inr"] = round(bucket["fees_inr"] + fees, 2)
            bucket["net_pnl_inr"] = round(bucket["net_pnl_inr"] + net, 2)
            if row.get("book") == "BOOK1_SWING_CNC_CORE":
                bucket["book1_net_inr"] = round(bucket["book1_net_inr"] + net, 2)
            else:
                bucket["book2_net_inr"] = round(bucket["book2_net_inr"] + net, 2)
            bucket["items"].append(
                {
                    "position_id": row.get("position_id"),
                    "symbol": row.get("symbol"),
                    "book": row.get("book"),
                    "product_type": row.get("product_type"),
                    "direction": row.get("direction"),
                    "shares": row.get("shares"),
                    "entry_price": row.get("entry_price"),
                    "exit_price": row.get("exit_price"),
                    "fees_inr": round(fees, 2),
                    "net_pnl_inr": round(net, 2),
                    "exit_reason": row.get("exit_reason"),
                    "exit_time": exit_ts,
                }
            )

        live_eq = float(acct["current_equity_inr"])
        live_cum = float(acct["realized_pnl_inr"]) + float(acct["unrealized_pnl_inr"])
        peak_eq = max(peak_eq, live_eq)
        live_dd = ((peak_eq - live_eq) / max(1.0, peak_eq)) * 100.0
        curve.append(
            {
                "idx": len(curve),
                "label": "Live MTM",
                "timestamp": now_s,
                "symbol": "LIVE",
                "equity_inr": round(live_eq, 2),
                "cum_net_pnl_inr": round(live_cum, 2),
                "trade_net_pnl_inr": round(float(acct["unrealized_pnl_inr"]), 2),
                "drawdown_pct": round(live_dd, 2),
            }
        )

        return {
            "protocol_id": PROTOCOL_ID,
            "accounting": acct,
            "closed_trades_count": len(closed_rows),
            "equity_curve": curve,
            "daily_sessions": [by_date[k] for k in sorted(by_date.keys(), reverse=True)],
        }


def get_consensus_v3_bridge() -> ConsensusArenaExecutionBridge:
    global _ARENA_BRIDGE_INSTANCE
    with _ARENA_BRIDGE_LOCK:
        if _ARENA_BRIDGE_INSTANCE is None:
            _ARENA_BRIDGE_INSTANCE = ConsensusArenaExecutionBridge()
        return _ARENA_BRIDGE_INSTANCE
