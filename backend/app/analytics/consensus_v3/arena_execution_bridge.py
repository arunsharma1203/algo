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

MIN_FUNDED_CAPITAL_INR = 10000.0
DEFAULT_FUNDED_CAPITAL_INR = 400000.0
DAILY_LOSS_HALT_FRACTION = 0.0125  # -1.25% daily realized loss halt (-₹5,000 on ₹4,00,000)


def compute_optimal_slot_counts(funded_capital_inr: float, mis_leverage_cap: float = 1.0) -> tuple:
    """
    Auto-scales Book 1 (1.0x CNC) and Book 2 (MIS) slot counts so smaller wallets (e.g. ₹25k–₹1L)
    maintain ~₹20k–₹50k buying power per slot for whole-share sizing on NSE F&O stocks.
    """
    cap = max(MIN_FUNDED_CAPITAL_INR, float(funded_capital_inr))
    lev = max(1.0, min(5.0, float(mis_leverage_cap)))
    b1_slots = max(1, min(8, int(cap // 25000.0))) if cap < 200000.0 else 8
    b2_bp = cap * lev
    b2_slots = max(1, min(4, int(b2_bp // 25000.0))) if b2_bp < 100000.0 else 4
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
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cv3_pending_status ON consensus_v3_pending_orders(session_id, status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cv3_pos_status ON consensus_v3_positions(session_id, status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cv3_signals_time ON consensus_v3_signals(evaluated_at DESC);")

            # Additive non-destructive schema migrations for MIS buying power & cash margin tracking
            additive_cols = [
                ("consensus_v3_arena_state", "intraday_mis_leverage", "REAL NOT NULL DEFAULT 1.0"),
                ("consensus_v3_arena_state", "margin_mode", "TEXT NOT NULL DEFAULT 'CASH_1X'"),
                ("consensus_v3_arena_state", "max_risk_per_trade_pct", "REAL NOT NULL DEFAULT 0.50"),
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
        self.session_id = session_id
        self._lock = threading.RLock()
        self._margin_cache: Dict[str, tuple] = {}
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
                b1_s, b2_s = compute_optimal_slot_counts(DEFAULT_FUNDED_CAPITAL_INR, 1.0)
                conn.execute(
                    """
                    INSERT INTO consensus_v3_arena_state (
                        session_id, funded_capital_inr, realized_pnl_inr, peak_equity_inr,
                        max_drawdown_pct, execution_mode, book1_max_slots, book2_max_slots,
                        intraday_mis_leverage, margin_mode, max_risk_per_trade_pct, updated_at
                    ) VALUES (?, ?, 0.0, ?, 0.0, ?, ?, ?, 1.0, ?, 0.50, ?)
                    """,
                    (
                        self.session_id,
                        DEFAULT_FUNDED_CAPITAL_INR,
                        DEFAULT_FUNDED_CAPITAL_INR,
                        MODE_DUAL_FUNDED_ARENA,
                        b1_s,
                        b2_s,
                        MARGIN_MODE_CASH_1X,
                        now_s,
                    ),
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
    ) -> Dict[str, Any]:
        cap = max(MIN_FUNDED_CAPITAL_INR, float(funded_capital_inr))
        mode = str(execution_mode or MODE_DUAL_FUNDED_ARENA).upper().strip()
        if mode not in VALID_ARENA_MODES:
            return {"status": "ERROR", "message": f"Invalid mode '{mode}'. Choose from {VALID_ARENA_MODES}."}

        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._conn() as conn:
            cur_st = conn.execute(
                "SELECT * FROM consensus_v3_arena_state WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
            cur_keys = cur_st.keys() if cur_st else []
            existing_mm = str(cur_st["margin_mode"]) if (cur_st and "margin_mode" in cur_keys and cur_st["margin_mode"]) else MARGIN_MODE_CASH_1X
            existing_lev = float(cur_st["intraday_mis_leverage"]) if (cur_st and "intraday_mis_leverage" in cur_keys and cur_st["intraday_mis_leverage"]) else 1.0
            existing_risk = float(cur_st["max_risk_per_trade_pct"]) if (cur_st and "max_risk_per_trade_pct" in cur_keys and cur_st["max_risk_per_trade_pct"]) else 0.50

            if margin_mode is not None:
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

            risk_pct = (
                max(0.10, min(2.00, float(max_risk_per_trade_pct)))
                if max_risk_per_trade_pct is not None
                else existing_risk
            )
            auto_b1, auto_b2 = compute_optimal_slot_counts(cap, lev)
            b1_slots = max(1, min(12, int(book1_max_slots))) if book1_max_slots is not None else auto_b1
            b2_slots = auto_b2

            if reset_ledger and self.simulation_mode:
                conn.execute("DELETE FROM consensus_v3_pending_orders WHERE session_id = ?", (self.session_id,))
                conn.execute("DELETE FROM consensus_v3_positions WHERE session_id = ?", (self.session_id,))
                conn.execute(
                    """
                    UPDATE consensus_v3_arena_state
                    SET funded_capital_inr = ?, realized_pnl_inr = 0.0, peak_equity_inr = ?,
                        max_drawdown_pct = 0.0, execution_mode = ?, book1_max_slots = ?, book2_max_slots = ?,
                        intraday_mis_leverage = ?, margin_mode = ?, max_risk_per_trade_pct = ?,
                        last_book1_rebalance_date = NULL, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (cap, cap, mode, b1_slots, b2_slots, lev, mm, risk_pct, now_s, self.session_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE consensus_v3_arena_state
                    SET funded_capital_inr = ?, peak_equity_inr = MAX(peak_equity_inr, ? + realized_pnl_inr),
                        execution_mode = ?, book1_max_slots = ?, book2_max_slots = ?,
                        intraday_mis_leverage = ?, margin_mode = ?, max_risk_per_trade_pct = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (cap, cap, mode, b1_slots, b2_slots, lev, mm, risk_pct, now_s, self.session_id),
                )
            conn.commit()
        return self.get_arena_accounting()

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
          - `CASH_1X` (`1.0x`): exact `100%` cash (`CASH_1X_EXACT`).
          - `> 1.0x`: queries Upstox `/v2/charges/margin` (`product="I"`) when live token is active (`UPSTOX_API_VAR_ELM`),
            otherwise applies conservative SEBI `VaR + ELM` floor (`max(20%, 14% + 3.5 * ADR%)`) explicitly tagged
            `UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN` per `.agents/rules/no-data-leakage.md` Rule 4, bounded by `mis_leverage_cap`.
        """
        px = max(0.05, float(limit_px))
        cap_lev = max(1.0, min(5.0, float(mis_leverage_cap)))
        if cap_lev <= 1.001 or margin_mode == MARGIN_MODE_CASH_1X:
            return round(px, 4), 1.0, "CASH_1X_EXACT"

        user_min_margin_frac = 1.0 / cap_lev
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
            st_keys = st.keys() if st else []
            funded = float(st["funded_capital_inr"])
            realized = float(st["realized_pnl_inr"])
            mode = str(st["execution_mode"])
            b1_max = int(st["book1_max_slots"])
            b2_max = int(st["book2_max_slots"])
            mis_lev = float(st["intraday_mis_leverage"]) if ("intraday_mis_leverage" in st_keys and st["intraday_mis_leverage"]) else 1.0
            m_mode = str(st["margin_mode"]) if ("margin_mode" in st_keys and st["margin_mode"]) else MARGIN_MODE_CASH_1X
            risk_pct = float(st["max_risk_per_trade_pct"]) if ("max_risk_per_trade_pct" in st_keys and st["max_risk_per_trade_pct"]) else 0.50

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
        free_unreserved_cash_inr = max(0.0, current_equity_inr - invested_margin_inr - pending_reserved_inr)
        intraday_buying_power_inr = round(free_unreserved_cash_inr * mis_lev, 2)

        b1_open = sum(1 for r in open_rows if r["book"] == "BOOK1_SWING_CNC_CORE")
        b2_open = sum(1 for r in open_rows if r["book"] == "BOOK2_INTRADAY_MIS_SNIFFER")
        b2_pend = sum(1 for r in pend_rows if r["book"] == "BOOK2_INTRADAY_MIS_SNIFFER")

        max_daily_loss_inr = -round(funded * DAILY_LOSS_HALT_FRACTION, 2)
        today_realized = float(risk_row["realized_pnl_inr"]) if risk_row else 0.0
        fills_today = int(risk_row["fills_today"]) if risk_row else 0
        cb_tripped = bool(risk_row["circuit_breaker_tripped"]) if risk_row else (today_realized <= max_daily_loss_inr)

        b2_total_buying_power = funded * mis_lev
        return {
            "session_id": self.session_id,
            "simulation_mode": self.simulation_mode,
            "is_live_armed": not self.simulation_mode,
            "execution_mode": mode,
            "margin_mode": m_mode,
            "intraday_mis_leverage": round(mis_lev, 2),
            "max_risk_per_trade_pct": round(risk_pct, 2),
            "funded_capital_inr": round(funded, 2),
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
            "book1_slots": {"open": b1_open, "max": b1_max, "slot_cap_inr": round(funded / max(1, b1_max), 2)},
            "book2_slots": {
                "open": b2_open,
                "pending": b2_pend,
                "max": b2_max,
                "slot_cap_inr": round(b2_total_buying_power / max(1, b2_max), 2),
                "cash_slot_cap_inr": round(funded / max(1, b2_max), 2),
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
            rd = adapter.fetch_account_readiness(force_refresh=False) or {}
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
            "margin_mode": acct["margin_mode"],
            "intraday_mis_leverage": acct["intraday_mis_leverage"],
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
            import requests
            from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
            from app.data.upstox_provider import get_upstox_config, get_upstox_limiter, get_upstox_token

            adapter = get_option_live_adapter()
            cfg = get_upstox_config()
            tok = (cfg.get("algo_token") or "").strip() or get_upstox_token()
            if not tok:
                raise RuntimeError("Missing Upstox bearer token")
            proxy_url = adapter.get_proxy_url()
            if not is_emergency:
                try:
                    get_upstox_limiter().acquire(timeout=2.0)
                except Exception:
                    pass
            http_resp = requests.post(
                "https://api.upstox.com/v2/order/place",
                headers={
                    "Authorization": f"Bearer {tok}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=payload,
                proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
                timeout=8.0,
            )
            resp = http_resp.json() if http_resp.content else {}
            broker_oid = str((resp or {}).get("data", {}).get("order_id") or "")
            if http_resp.status_code != 200 or not broker_oid:
                state = "REJECTED"
                rej = f"HTTP {http_resp.status_code}: {str(resp)[:200]}"
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
        allow_broker_probe: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Places 15-minute Post-Only Limit Reservations in `consensus_v3_pending_orders`:
          - Immediately locks `shares * margin_per_share` in `pending_reserved_cash_inr`.
          - Enforces `invested_margin + pending_reserved_cash <= current_equity` (`0` wallet cash deficit!).
          - When `intraday_mis_leverage > 1.0`, caps whole shares by BOTH broker/VaR+ELM margin AND
            dollar Stop-Loss risk (`max_risk_per_trade_pct` of `current_equity_inr`).
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
            m_mode = acct.get("margin_mode", MARGIN_MODE_CASH_1X)
            mis_lev = float(acct.get("intraday_mis_leverage", 1.0))
            risk_pct = float(acct.get("max_risk_per_trade_pct", 0.50))
            risk_budget_inr = acct["current_equity_inr"] * (risk_pct / 100.0)
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
                    sl_px = float(sig["stop_loss_px"])
                    adr_pct = float((sig.get("telemetry") or {}).get("atr_pct") or 2.0)

                    margin_per_sh, eff_lev, margin_src = self.estimate_mis_margin_per_share(
                        instrument_key=sig.get("instrument_key", ""),
                        limit_px=lim_px,
                        direction=sig["direction"],
                        adr_pct=adr_pct,
                        margin_mode=m_mode,
                        mis_leverage_cap=mis_lev,
                        allow_broker_probe=allow_broker_probe,
                    )

                    # Cash-margin share ceiling vs slot notional ceiling vs 1% bar liquidity ceiling
                    max_by_cash_margin = int(free_cash // max(0.05, margin_per_sh))
                    max_by_slot_notional = compute_whole_shares(
                        allocatable_cash_inr=slot_cap,
                        price_inr=lim_px,
                        max_slot_inr=slot_cap,
                        liquidity_cap_inr=float(sig.get("liq_1pct_bar_inr", slot_cap)),
                    )
                    shares = min(max_by_cash_margin, max_by_slot_notional)

                    # Enforce Stop-Loss Dollar Risk Ceiling whenever leveraged MIS buying power (> 1.0x) is active
                    if eff_lev > 1.001 and shares >= 1:
                        stop_dist_px = max(lim_px * 0.0035, abs(sl_px - lim_px))
                        max_by_stop_risk = int(risk_budget_inr // stop_dist_px)
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
                            oid, self.session_id, sig["book"], ticker, sig["symbol"], sig.get("instrument_key", ""),
                            sig["direction"], shares, float(sig["signal_close_px"]), lim_px, reserved_inr,
                            notional_inr, eff_lev, margin_src,
                            sl_px, sig.get("scheduled_exit_hm", "15:15"),
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
    ) -> Dict[str, Any]:
        """
        Executes Layer-6 Book 1 (`20d` Scheduled Cash `CNC` Swing Core) Rebalance:
          - Strictly `1.0x` Funded Cash (`0x` leverage, `product_type='CNC'`, whole shares `>= 1`).
          - Scales target slots dynamically by BOTH wallet `book1_max_slots` AND Macro Breadth Shield (`100% / 50% / 0%`).
          - Retains existing open Book-1 holdings whose `qlib_pct_rank >= 0.78` AND `sma_alignment_ok == True`
            (Rank-78% Hysteresis turnover control).
          - Exits decayed holdings (`qlib_pct_rank < 0.78`), corporate-action split anomalies (`|1d jump| > 40%`),
            or Macro Shield step-downs, deducting exact statutory NSE `CNC` STT + DP + Slippage.
          - Allocates remaining empty slots into top `approved_rebalance_leaders` out of `free_unreserved_cash_inr`.
        """
        ts = now_dt or datetime.now()
        now_s = ts.strftime("%Y-%m-%d %H:%M:%S")
        today_s = ts.strftime("%Y-%m-%d")
        as_of_date = str(arb_b1.get("as_of_date") or scored_snapshot.get("as_of_date") or today_s)

        with self._lock:
            acct = self.get_arena_accounting(now_dt=ts)
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
                    "SELECT * FROM consensus_v3_positions WHERE session_id = ? AND book = 'BOOK1_SWING_CNC_CORE' AND status = 'OPEN'",
                    (self.session_id,),
                ).fetchall()

                # Count completed trading sessions since last Book-1 rebalance
                tdays_elapsed = 999
                if last_reb:
                    tdays_elapsed = int(
                        conn.execute(
                            "SELECT COUNT(DISTINCT substr(date,1,10)) FROM ohlcv WHERE timeframe = '1d' AND substr(date,1,10) > ? AND substr(date,1,10) <= ?",
                            (str(last_reb)[:10], as_of_date),
                        ).fetchone()[0]
                    )

                shield_stepdown_needed = len(open_b1) > target_slots
                is_due = force or (last_reb is None) or (tdays_elapsed >= 20) or shield_stepdown_needed
                if not is_due:
                    return {
                        "status": "NOT_DUE_YET",
                        "trading_days_since_last": tdays_elapsed,
                        "next_due_in_trading_days": max(0, 20 - tdays_elapsed),
                        "target_slots": target_slots,
                        "opened": 0,
                        "closed": 0,
                        "retained": len(open_b1),
                    }

                cand_map = {c["ticker"]: c for c in (scored_snapshot.get("candidates") or [])}
                keep_candidates = []
                to_close = []

                for pos in open_b1:
                    sym = pos["ticker"]
                    epx = float(pos["entry_price"])
                    cinfo = cand_map.get(sym)
                    curr_px = float(cinfo["close"]) if cinfo else float(pos["current_price"] or epx)
                    raw_jump = abs(curr_px - float(pos["current_price"] or epx)) / max(1.0, float(pos["current_price"] or epx))

                    if raw_jump > 0.40:
                        # Corporate-Action / Split Guard
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
                for pos_row, xpx, reason in to_close:
                    closed_records.append(self._close_position_in_tx(conn, pos_row, xpx, reason, now_s, today_s))

                # Refresh accounting inside tx after exits release cash!
                st_now = conn.execute("SELECT funded_capital_inr, realized_pnl_inr FROM consensus_v3_arena_state WHERE session_id = ?", (self.session_id,)).fetchone()
                rem_open = conn.execute("SELECT * FROM consensus_v3_positions WHERE session_id = ? AND status = 'OPEN'", (self.session_id,)).fetchall()
                rem_pend = conn.execute("SELECT * FROM consensus_v3_pending_orders WHERE session_id = ? AND status = 'PENDING_RESERVED'", (self.session_id,)).fetchall()

                used_margin = sum(
                    float(r["margin_used_inr"]) if ("margin_used_inr" in r.keys() and r["margin_used_inr"] is not None) else float(r["invested_notional_inr"])
                    for r in rem_open
                )
                pend_cash = sum(float(r["reserved_cash_inr"]) for r in rem_pend)
                rem_unreal = sum(float(r["unrealized_pnl_inr"]) for r in rem_open)
                eq_now = float(st_now["funded_capital_inr"]) + float(st_now["realized_pnl_inr"]) + rem_unreal
                free_cash = max(0.0, eq_now - used_margin - pend_cash)

                held_tickers = {p["ticker"] for _, p, _ in retained_list}
                slots_to_open = max(0, target_slots - len(retained_list))
                slot_cap_inr = float(st_now["funded_capital_inr"]) / max(1, b1_max)
                exec_env = "PAPER_SHADOW" if self.simulation_mode else "LIVE_UPSTOX"

                opened_records = []
                for leader in (arb_b1.get("approved_rebalance_leaders") or []):
                    if slots_to_open <= 0:
                        break
                    sym = leader["ticker"]
                    if sym in held_tickers:
                        continue
                    epx = round_nse_eq_tick(float(leader["close_px"]))
                    sh = compute_whole_shares(
                        allocatable_cash_inr=free_cash,
                        price_inr=epx,
                        max_slot_inr=slot_cap_inr,
                        liquidity_cap_inr=slot_cap_inr * 10.0,
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

                    # Catastrophic 18% hard disaster stop in DB while primary exit is 20d Rank-78% Hysteresis
                    disaster_sl = round_nse_eq_tick(epx * 0.82)
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
                            pid, self.session_id, sym, leader["symbol"], leader.get("instrument_key", ""),
                            sh, epx, notional, notional, disaster_sl, exec_env, epx, now_s, json.dumps(leader),
                        ),
                    )
                    free_cash -= notional
                    slots_to_open -= 1
                    held_tickers.add(sym)
                    opened_records.append({"position_id": pid, "ticker": sym, "shares": sh, "entry_price": epx, "notional_inr": notional})

                conn.execute(
                    "UPDATE consensus_v3_arena_state SET last_book1_rebalance_date = ?, updated_at = ? WHERE session_id = ?",
                    (as_of_date, now_s, self.session_id),
                )
                conn.commit()

        return {
            "status": "REBALANCED",
            "as_of_date": as_of_date,
            "target_slots": target_slots,
            "retained": len(retained_list),
            "closed": len(closed_records),
            "opened": len(opened_records),
            "opened_positions": opened_records,
            "closed_positions": closed_records,
        }

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

            completed_15m_cutoff = (ts - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")

            def _latest_px_and_bar(sym: str, after_ts: str, prod_type: str = "MIS", details_json: Optional[str] = None):
                q = quotes.get(sym) or quotes.get(sym.replace(".NS", ""))
                ltp = float(q["price"]) if (q and isinstance(q.get("price"), (int, float)) and q["price"] > 0) else None

                sig_bar_dt = None
                if details_json:
                    try:
                        sig_bar_dt = (json.loads(details_json) or {}).get("bar_dt")
                    except Exception:
                        sig_bar_dt = None

                brow = None
                if sig_bar_dt:
                    # Strictly post-signal completed 15m bar (`bar_start > signal_bar_dt` AND `bar_start + 15m <= now_dt`)
                    brow = conn.execute(
                        "SELECT open, high, low, close FROM ohlcv WHERE ticker = ? AND timeframe = '15m' AND date > ? AND date <= ? ORDER BY date DESC LIMIT 1",
                        (sym, str(sig_bar_dt)[:19], completed_15m_cutoff),
                    ).fetchone()
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
                    brow = conn.execute(
                        f"SELECT open, high, low, close FROM ohlcv WHERE ticker = ? AND timeframe = '15m' AND date {op} ? AND date <= ? ORDER BY date DESC LIMIT 1",
                        (sym, min_bar_s, completed_15m_cutoff),
                    ).fetchone()

                if brow is None and prod_type == "CNC":
                    brow = conn.execute(
                        "SELECT open, high, low, close FROM ohlcv WHERE ticker = ? AND timeframe = '1d' AND substr(date,1,10) > ? AND substr(date,1,10) <= ? ORDER BY date DESC LIMIT 1",
                        (sym, str(after_ts)[:10], today_s),
                    ).fetchone()
                return ltp, brow

            # 1. Reconcile Pending Limit Reservations
            for pr in pend:
                oid = pr["order_id"]
                sym = pr["ticker"]
                lim = float(pr["limit_price"])
                sdir = pr["direction"]
                ltp, brow = _latest_px_and_bar(sym, pr["created_at"], pr["product_type"], pr["details_json"])

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
                    pr_keys = pr.keys()
                    m_used = float(pr["reserved_cash_inr"])
                    eff_lev = float(pr["effective_leverage"]) if ("effective_leverage" in pr_keys and pr["effective_leverage"]) else 1.0
                    m_src = str(pr["margin_source"]) if ("margin_source" in pr_keys and pr["margin_source"]) else "CASH_1X_EXACT"
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
                            pid, self.session_id, oid, pr["book"], pr["product_type"], sym, pr["symbol"],
                            pr["instrument_key"], sdir, sh, lim, notional,
                            m_used, eff_lev, m_src,
                            float(pr["stop_loss_px"]),
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
                ltp, brow = _latest_px_and_bar(sym, pos["entry_time"], prod)

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
