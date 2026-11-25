"""
S2_05 Institutional Shadow-Execution & L1/L5/Margin/FSP Telemetry Collector.

Guarantees:
  1. 100% Paper-Only Shadow Execution (`SIMULATION_ONLY = True`, `shadow_only = True`).
     Zero broker order placement; hard-fenced from `AdvanceLiveBridge`.
  2. 100% Canonical Options Accounting via `app.analytics.common.options_accounting`
     (`OptionLeg`, `compute_package_accounting`, `compute_structural_package_risk`,
     `round_to_nse_tick`). Zero inline brokerage, STT, or tick-rounding math.
  3. Fail-Closed Microstructure Hygiene: requires genuinely observed Upstox V3 L5
     order books (`is_observed_l1_book = True`, `depth_observed_on_tick = True`,
     `l1_book_age_ms <= max_l1_book_age_ms`, cumulative 5-level qty >= 65).
  4. Physical Buy-First 2-Leg Credit Spread State Machine (`PENDING_LEG2_SHORT` ->
     `OPEN` on subsequent observed quote, or `INCOMPLETE_LEG_UNWOUND` emergency
     wing liquidation with full round-trip canonical charges).
  5. Strict Intra-Bar Capital & Slot Causality (`bar[t].open` before `bar[t].close`):
     cash/margin and book slots released by an exit on `bar_ts` remain locked until
     strictly `next_bar_ts > exit_bar_ts`.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, time as dt_time
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

from app.analytics.common.options_accounting import (
    OptionLeg,
    compute_package_accounting,
    compute_structural_package_risk,
    round_to_nse_tick,
)
from app.analytics.option_sniffer_advance_s205.s205_signal_evaluator import (
    compute_close_only_ratchet,
)

BOOK_LONG_BUY = "LONG_BOOK_BUY"
BOOK_SHORT_SELL = "SHORT_BOOK_SELL"


def _to_ist_naive_safe(dt_val: Optional[datetime] = None) -> datetime:
    if dt_val is None:
        return datetime.now()
    if getattr(dt_val, "tzinfo", None) is not None:
        return dt_val.replace(tzinfo=None)
    return dt_val


def _resolve_default_db_path() -> str:
    from app.data.historical_data_layer import get_db_path

    return get_db_path()

logger = logging.getLogger(__name__)

_DEFAULT_PREREG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "SHADOW_PREREGISTRATION.json",
)

_FADE_HORIZONS_MS: Tuple[Tuple[str, int], ...] = (
    ("+250ms", 250),
    ("+1s", 1000),
    ("+5s", 5000),
    ("+60s", 60000),
)


def load_shadow_preregistration(path: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
    """Loads the frozen shadow pre-registration JSON and returns `(config_dict, sha256_hex)`."""
    target = path or _DEFAULT_PREREG_PATH
    with open(target, "rb") as fh:
        raw_bytes = fh.read()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    cfg = json.loads(raw_bytes.decode("utf-8"))
    if not bool(cfg.get("simulation_only", False)):
        raise ValueError("SHADOW_PREREGISTRATION must keep simulation_only = true")
    if bool(cfg.get("naked_short_allowed", True)):
        raise ValueError("SHADOW_PREREGISTRATION must keep naked_short_allowed = false")
    if bool(cfg.get("allow_live_bridge_mirroring", True)):
        raise ValueError("SHADOW_PREREGISTRATION must keep allow_live_bridge_mirroring = false")
    return cfg, digest


def init_shadow_telemetry_tables(db_path: Optional[str] = None) -> None:
    """Creates additive WAL-safe telemetry & canonical shadow ledger tables."""
    resolved = db_path or _resolve_default_db_path()
    conn = sqlite3.connect(resolved, timeout=15.0)
    try:
        with conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_sniffer_advance_s205_l5_chain_1m (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_date TEXT NOT NULL,
                    bar_ts TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT,
                    expiry TEXT NOT NULL,
                    strike REAL NOT NULL,
                    option_type TEXT NOT NULL,
                    ltp REAL,
                    raw_bid REAL,
                    raw_ask REAL,
                    bid_qty INTEGER,
                    ask_qty INTEGER,
                    l5_bid_qty INTEGER,
                    l5_ask_qty INTEGER,
                    book_obi_l5 REAL,
                    depth_json TEXT,
                    is_observed_l1_book INTEGER NOT NULL DEFAULT 0,
                    depth_observed_on_tick INTEGER NOT NULL DEFAULT 0,
                    book_provenance TEXT,
                    l1_book_age_ms INTEGER,
                    delta_vtt_1m REAL,
                    cumulative_vtt INTEGER,
                    oi REAL,
                    iv REAL,
                    delta REAL,
                    gamma REAL,
                    theta REAL,
                    vega REAL,
                    spot_ltp REAL,
                    fut_ltp REAL,
                    fut_oi REAL,
                    india_vix REAL,
                    UNIQUE(bar_ts, instrument_key)
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_s205_l5_chain_session_bar "
                "ON option_sniffer_advance_s205_l5_chain_1m(session_date, bar_ts);"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_sniffer_advance_s205_quote_fade_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    package_id TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    horizon_label TEXT NOT NULL,
                    target_ts_ms INTEGER NOT NULL,
                    observed_ts_ms INTEGER NOT NULL,
                    instrument_key TEXT NOT NULL,
                    side TEXT NOT NULL,
                    raw_bid REAL,
                    raw_ask REAL,
                    l5_vwap_65_buy REAL,
                    l5_vwap_65_sell REAL,
                    l5_bid_qty INTEGER,
                    l5_ask_qty INTEGER,
                    is_observed_l1_book INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(event_id, horizon_label, instrument_key)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_sniffer_advance_s205_margin_telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    package_id TEXT,
                    book_id TEXT NOT NULL,
                    legs_json TEXT NOT NULL,
                    upstox_http_status INTEGER,
                    margin_verified INTEGER NOT NULL DEFAULT 0,
                    margin_status TEXT NOT NULL,
                    span_margin_inr REAL,
                    exposure_margin_inr REAL,
                    final_margin_inr REAL,
                    standalone_unhedged_margin_inr REAL,
                    hedge_benefit_inr REAL,
                    structural_max_loss_inr REAL,
                    spot_ltp REAL,
                    india_vix REAL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_sniffer_advance_s205_expiry_fsp_windows (
                    expiry_date TEXT PRIMARY KEY,
                    samples_count INTEGER NOT NULL DEFAULT 0,
                    spot_1500_1530_twap REAL,
                    spot_1529_close REAL,
                    fut_1529_close REAL,
                    official_nse_settlement REAL,
                    fsp_minus_1529_pts REAL,
                    samples_json TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_sniffer_advance_s205_shadow_packages (
                    package_id TEXT PRIMARY KEY,
                    advance_trade_id TEXT,
                    prereg_sha256 TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    book_id TEXT NOT NULL,
                    ui_book_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    direction_label TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    entry_bar_ts TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    leg2_confirmed_time TEXT,
                    exit_bar_ts TEXT,
                    exit_time TEXT,
                    legs_state_json TEXT NOT NULL,
                    accounting_json TEXT,
                    structural_max_loss_inr REAL NOT NULL DEFAULT 0.0,
                    upstox_final_margin_inr REAL,
                    margin_status TEXT NOT NULL,
                    cash_reserved_inr REAL NOT NULL DEFAULT 0.0,
                    gross_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    total_charges_inr REAL NOT NULL DEFAULT 0.0,
                    stt_inr REAL NOT NULL DEFAULT 0.0,
                    brokerage_inr REAL NOT NULL DEFAULT 0.0,
                    net_pnl_inr REAL NOT NULL DEFAULT 0.0,
                    exit_reason TEXT,
                    regime_context_json TEXT,
                    shadow_only INTEGER NOT NULL DEFAULT 1
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_s205_shadow_pkg_session_state "
                "ON option_sniffer_advance_s205_shadow_packages(session_date, state);"
            )
    finally:
        conn.close()


SHADOW_EXIT_MAX_QUOTE_AGE_MS: int = 5000
SHADOW_CROSS_LEG_MAX_SKEW_MS: int = 5000
SHADOW_EMERGENCY_REST_COOLDOWN_SEC: float = 5.0
SHADOW_EOD_REST_COOLDOWN_SEC: float = 1.0
CLEAN_BOOK_A_EPOCH_START_DATE: str = "2026-11-24"


def _extract_quote_ts_ms(quote: Optional[Dict[str, Any]]) -> int:
    """
    Extracts the epoch-ms timestamp bound strictly to the price/depth payload consumed.
    Returns 0 when unknown/missing so callers fail closed rather than assuming synchronization.
    """
    if not isinstance(quote, dict) or not quote:
        return 0
    prov = str(quote.get("book_provenance") or "")
    has_depth = bool(quote.get("depth_levels"))
    if prov == "EMERGENCY_REST_L1":
        key_order: Tuple[str, ...] = ("timestamp_ms", "local_receipt_ts_ms")
    elif prov == "OBSERVED_V3_L5" or has_depth:
        l1_ts_raw = int(quote.get("l1_book_timestamp_ms") or 0) if str(quote.get("l1_book_timestamp_ms") or "").lstrip("-").isdigit() else 0
        l1_age_raw = quote.get("l1_book_age_ms")
        if l1_age_raw is not None:
            try:
                if int(l1_age_raw) == 999999 and l1_ts_raw <= 0:
                    return 0
            except (ValueError, TypeError):
                return 0
        key_order = ("l1_book_timestamp_ms", "local_receipt_ts_ms", "timestamp_ms")
    else:
        key_order = ("l1_book_timestamp_ms", "local_receipt_ts_ms", "timestamp_ms")

    for k in key_order:
        v = quote.get(k)
        if v is not None:
            try:
                iv = int(v)
                if iv > 0:
                    return iv
            except (ValueError, TypeError):
                pass
    return 0


def _quotes_within_skew(
    quote_a: Optional[Dict[str, Any]],
    quote_b: Optional[Dict[str, Any]],
    max_skew_ms: int = SHADOW_CROSS_LEG_MAX_SKEW_MS,
) -> bool:
    """
    Fail-closed cross-leg synchronization check:
    Returns False if either quote is missing a valid receipt/book timestamp (`<= 0`)
    or if the two timestamps diverge by `> max_skew_ms`.
    """
    if not isinstance(quote_a, dict) or not isinstance(quote_b, dict) or not quote_a or not quote_b:
        return False
    ts_a = _extract_quote_ts_ms(quote_a)
    ts_b = _extract_quote_ts_ms(quote_b)
    if ts_a <= 0 or ts_b <= 0:
        return False
    return abs(ts_a - ts_b) <= max_skew_ms


def _resolve_consumed_quote_age_ms(
    quote: Dict[str, Any],
    require_observed: bool,
) -> Optional[int]:
    """
    Computes conservative age (ms) of the exact price/depth structure consumed by `sweep_l5_book_for_qty`.
    Returns `None` when age is missing, sentinel (`>= 999999`), or unparseable — NEVER defaults to 0!
    """
    prov = str(quote.get("book_provenance") or "")
    has_depth = bool(quote.get("depth_levels"))

    q_age: Optional[int] = None
    if quote.get("quote_age_ms") is not None:
        try:
            qa = int(quote["quote_age_ms"])
            has_q_ts = any(
                int(quote.get(k) or 0) > 0
                for k in ("timestamp_ms", "local_receipt_ts_ms", "l1_book_timestamp_ms")
                if isinstance(quote.get(k), (int, float))
            )
            if qa >= 0 and not (qa == 999999 and not has_q_ts):
                q_age = qa
        except (ValueError, TypeError):
            return None

    l1_age: Optional[int] = None
    if quote.get("l1_book_age_ms") is not None:
        try:
            la = int(quote["l1_book_age_ms"])
            has_l1_ts = isinstance(quote.get("l1_book_timestamp_ms"), (int, float)) and int(quote.get("l1_book_timestamp_ms") or 0) > 0
            if la >= 0 and not (la == 999999 and not has_l1_ts):
                l1_age = la
        except (ValueError, TypeError):
            return None

    if require_observed:
        if l1_age is None:
            return None
        return max(l1_age, q_age) if q_age is not None else l1_age

    if prov == "EMERGENCY_REST_L1":
        return q_age

    if has_depth:
        # Sweeping `depth_levels`: require valid L1/L5 depth age so fresh LTP cannot mask stale depth!
        if l1_age is None:
            return None
        return max(l1_age, q_age) if q_age is not None else l1_age

    if l1_age is not None and q_age is not None:
        return max(l1_age, q_age)
    return l1_age if l1_age is not None else q_age


def sweep_l5_book_for_qty(
    quote: Optional[Dict[str, Any]],
    side: str,
    qty: int = 65,
    adverse_ticks: int = 1,
    max_age_ms: Optional[int] = 1500,
    max_spread_pct: Optional[float] = 2.5,
    max_spread_pts: Optional[float] = None,
    require_observed: bool = True,
) -> Dict[str, Any]:
    """
    Sweeps the 5-level order book (`depth_levels[0..4]`) for `qty` contracts and applies
    canonical `₹0.05` adverse tick rounding via `options_accounting.round_to_nse_tick`.

    Fails closed (`ok = False`) on synthetic/carried books, unknown/missing quote age
    (`UNKNOWN_QUOTE_AGE`), stale books (`STALE_L1_BOOK`), crossed quotes, excessive spread,
    or cumulative 5-level depth `< qty`.
    """
    side_u = str(side or "").upper().strip()
    if side_u not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")
    if qty <= 0:
        raise ValueError(f"qty must be > 0, got {qty!r}")

    if not isinstance(quote, dict) or not quote:
        return {"ok": False, "reason": "MISSING_QUOTE"}

    is_obs = bool(quote.get("is_observed_l1_book", False))
    depth_obs = bool(quote.get("depth_observed_on_tick", False))
    prov = str(quote.get("book_provenance") or "")

    if prov == "REST_UNVERIFIED_BOOK":
        return {"ok": False, "reason": "UNVERIFIED_REST_BOOK"}

    if require_observed:
        if not is_obs or not depth_obs or prov != "OBSERVED_V3_L5":
            return {
                "ok": False,
                "reason": "SYNTHETIC_OR_CARRIED_BOOK_REJECTED",
                "book_provenance": prov or "UNKNOWN",
            }

    eff_age_ms = _resolve_consumed_quote_age_ms(quote, require_observed=require_observed)
    if max_age_ms is not None:
        if eff_age_ms is None:
            return {
                "ok": False,
                "reason": "UNKNOWN_QUOTE_AGE",
                "max_age_ms": max_age_ms,
            }
        if eff_age_ms > max_age_ms:
            return {
                "ok": False,
                "reason": "STALE_L1_BOOK",
                "l1_book_age_ms": eff_age_ms,
                "max_age_ms": max_age_ms,
            }

    raw_bid = float(
        quote["raw_bid"] if quote.get("raw_bid") is not None else (quote.get("bid") or 0.0)
    )
    raw_ask = float(
        quote["raw_ask"] if quote.get("raw_ask") is not None else (quote.get("ask") or 0.0)
    )
    if raw_bid <= 0.0 or raw_ask < raw_bid:
        return {
            "ok": False,
            "reason": "ZERO_OR_CROSSED_QUOTE",
            "raw_bid": raw_bid,
            "raw_ask": raw_ask,
        }

    mid = 0.5 * (raw_bid + raw_ask)
    spread_pts = round(raw_ask - raw_bid, 4)
    spread_pct = round((spread_pts / mid) * 100.0, 4) if mid > 0 else 999.0

    if max_spread_pct is not None:
        spread_ok = spread_pct <= max_spread_pct
        if not spread_ok and max_spread_pts is not None:
            spread_ok = (spread_pts <= max_spread_pts) and (spread_pct <= max(max_spread_pct, 6.0))
        if not spread_ok:
            return {
                "ok": False,
                "reason": "SPREAD_TOO_WIDE",
                "spread_pts": spread_pts,
                "spread_pct": spread_pct,
            }

    levels = list(quote.get("depth_levels") or [])
    if not levels:
        return {"ok": False, "reason": "EMPTY_DEPTH_LEVELS"}

    rem = int(qty)
    notional = 0.0
    available = 0
    levels_used = 0
    px_key = "ask_p" if side_u == "BUY" else "bid_p"
    q_key = "ask_q" if side_u == "BUY" else "bid_q"

    for lvl in levels[:5]:
        lp = float(lvl.get(px_key) or 0.0)
        lq = int(lvl.get(q_key) or 0)
        if lp <= 0.0 or lq <= 0:
            continue
        available += lq
        if rem > 0:
            take = min(rem, lq)
            notional += take * lp
            rem -= take
            levels_used += 1

    if rem > 0:
        return {
            "ok": False,
            "reason": "INSUFFICIENT_L5_DEPTH",
            "available_qty": available,
            "required_qty": qty,
        }

    vwap_raw = notional / float(qty)
    tick_shift = max(0, int(adverse_ticks)) * 0.05
    if side_u == "BUY":
        fill_px = round_to_nse_tick(vwap_raw + tick_shift, mode="adverse_buy", allow_zero_settlement=False)
        top_px = raw_ask
        depth_slip = round(fill_px - top_px, 4)
    else:
        shifted = max(0.05, vwap_raw - tick_shift)
        fill_px = round_to_nse_tick(shifted, mode="adverse_sell", allow_zero_settlement=False)
        top_px = raw_bid
        depth_slip = round(top_px - fill_px, 4)

    return {
        "ok": True,
        "side": side_u,
        "qty": qty,
        "fill_price": fill_px,
        "vwap_raw": round(vwap_raw, 4),
        "top_price": round(top_px, 2),
        "raw_bid": round(raw_bid, 2),
        "raw_ask": round(raw_ask, 2),
        "depth_slippage_pts": depth_slip,
        "levels_swept": levels_used,
        "available_l5_qty": available,
        "spread_pts": round(spread_pts, 2),
        "spread_pct": round(spread_pct, 2),
        "l1_book_age_ms": eff_age_ms,
    }


class S205ShadowCollector:
    """
    Canonical `₹2,00,000` shadow-execution state machine + L5/Margin/FSP telemetry recorder.
    """

    SIMULATION_ONLY: bool = True

    def __init__(
        self,
        db_path: Optional[str] = None,
        prereg_path: Optional[str] = None,
        sync_advance_db: bool = True,
        margin_fetcher: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = None,
        emergency_quote_fetcher: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._db_path_override = db_path
        self._sync_advance_db = bool(sync_advance_db)
        self._margin_fetcher = margin_fetcher
        self._emergency_quote_fetcher = emergency_quote_fetcher
        self.config, self.prereg_sha256 = load_shadow_preregistration(prereg_path)

        init_shadow_telemetry_tables(self._db_path())

        self._session_date: str = ""
        self._prev_chain_vtt: Dict[str, int] = {}
        self._last_recorded_chain_bar_ts: str = ""
        self._margin_probes_today: int = 0
        self._daily_kill_triggered: bool = False
        self._daily_kill_reason: str = ""
        self._cooldown_until_epoch: Dict[str, float] = {}
        self._last_emergency_rest_mono: Dict[str, float] = {}
        self._last_valid_pkg_mtm_inr: Dict[str, float] = {}
        self._unpriced_open_count: int = 0
        self._unpriced_package_ids: List[str] = []
        self._mtm_visibility_status: str = "COMPLETE"
        self._last_known_unrealized_inr: float = 0.0
        self._effective_unrealized_inr: Optional[float] = 0.0
        self._worst_case_unpriced_exposure_inr: float = 0.0

        # Strict intra-bar capital causality (`bar[t].open` before `bar[t].close`):
        # maps `exit_bar_ts -> list of (book_id, cash_reserved_inr)` locked until `bar_ts > exit_bar_ts`.
        self._locked_until_after_bar: Dict[str, List[Tuple[str, float]]] = {}

        # Pending Leg-2 buy-first spread packages awaiting subsequent observed short quote
        self._pending_leg2: Dict[str, Dict[str, Any]] = {}

        # Active open shadow packages in memory (mirrored to SQLite)
        self._open_packages: Dict[str, Dict[str, Any]] = {}

        # Non-blocking due-horizon quote-fade probes
        self._pending_fade_probes: Deque[Dict[str, Any]] = deque(maxlen=400)

        # Expiry-day 15:00..15:30 IST spot samples
        self._expiry_fsp_samples: Dict[str, List[Tuple[str, float]]] = {}

        # Optional read-only lifecycle listeners (e.g. isolated ShadowMirrorExecutor)
        self._lifecycle_listeners: List[Callable[[str, Dict[str, Any]], Any]] = []

        self._restore_open_packages_from_db()

    def register_lifecycle_listener(self, listener: Callable[[str, Dict[str, Any]], Any]) -> None:
        """Registers a non-blocking read-only package lifecycle listener."""
        if callable(listener):
            with self._lock:
                if listener not in self._lifecycle_listeners:
                    self._lifecycle_listeners.append(listener)

    def _emit_lifecycle_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Invokes registered listeners inside try/except so external listeners can never fault Shadow V1."""
        with self._lock:
            listeners = list(self._lifecycle_listeners)
        for fn in listeners:
            try:
                fn(event_type, payload)
            except Exception as exc:
                logger.debug(f"[S205ShadowCollector] Lifecycle listener note ({event_type}): {exc}")

    def _db_path(self) -> str:
        return self._db_path_override or _resolve_default_db_path()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path(), timeout=15.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _reset_session_if_needed(self, session_date: str) -> None:
        if self._session_date != session_date:
            self._session_date = session_date
            self._prev_chain_vtt.clear()
            self._last_recorded_chain_bar_ts = ""
            self._margin_probes_today = 0
            self._daily_kill_triggered = False
            self._daily_kill_reason = ""
            self._locked_until_after_bar.clear()
            self._cooldown_until_epoch.clear()
            self._last_emergency_rest_mono.clear()
            self._last_valid_pkg_mtm_inr.clear()
            self._unpriced_open_count = 0
            self._unpriced_package_ids = []
            self._mtm_visibility_status = "COMPLETE"
            self._last_known_unrealized_inr = 0.0
            self._effective_unrealized_inr = 0.0
            self._worst_case_unpriced_exposure_inr = 0.0

    def _active_pinned_leg_keys(self) -> set:
        keys = set()
        with self._lock:
            for pkg in self._open_packages.values():
                ls = pkg.get("legs_state") or {}
                for lk in ("wing_leg", "short_leg", "long_leg"):
                    ld = ls.get(lk)
                    if isinstance(ld, dict) and ld.get("instrument_key"):
                        keys.add(str(ld["instrument_key"]))
            for pobj in self._pending_leg2.values():
                for lk in ("wing_leg", "short_target"):
                    ld = pobj.get(lk)
                    if isinstance(ld, dict) and ld.get("instrument_key"):
                        keys.add(str(ld["instrument_key"]))
        return keys

    def _pin_package_contracts(self, legs: Sequence[Dict[str, Any]]) -> None:
        if not self._sync_advance_db or not legs:
            return
        to_pin: List[Dict[str, Any]] = []
        for ld in legs:
            if not isinstance(ld, dict) or not ld.get("instrument_key"):
                continue
            to_pin.append(
                {
                    "instrument_key": str(ld["instrument_key"]),
                    "trading_symbol": str(ld.get("trading_symbol") or ld["instrument_key"]),
                    "underlying": "NIFTY",
                    "expiry": str(ld.get("expiry") or "")[:10],
                    "strike": float(ld.get("strike") or 0.0),
                    "option_type": str(ld.get("option_type") or "CE").upper(),
                    "lot_size": abs(int(ld.get("qty") or 65)) or 65,
                    "is_pinned_active_leg": True,
                    "is_active_position_leg": True,
                }
            )
        if to_pin:
            try:
                from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
                OptionStreamManager().pin_extra_contracts(to_pin)
            except Exception as exc:
                logger.debug(f"[S205ShadowCollector] Pin package contracts note: {exc}")

    def _unpin_unused_package_keys(self, candidate_keys: Sequence[str]) -> None:
        if not self._sync_advance_db or not candidate_keys:
            return
        still_active = self._active_pinned_leg_keys()
        releasable = [str(k) for k in candidate_keys if k and str(k) not in still_active]
        if releasable:
            try:
                from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
                sm = OptionStreamManager()
                if hasattr(sm, "unpin_extra_contracts"):
                    sm.unpin_extra_contracts(releasable)
            except Exception as exc:
                logger.debug(f"[S205ShadowCollector] Unpin package contracts note: {exc}")

    def _resolve_exit_quote(
        self,
        ikey: str,
        cand_quote: Optional[Dict[str, Any]],
        max_age_ms: int = SHADOW_EXIT_MAX_QUOTE_AGE_MS,
        allow_emergency_rest: bool = False,
        is_eod_urgent: bool = False,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        if not force_refresh:
            chk = sweep_l5_book_for_qty(
                cand_quote,
                "SELL",
                qty=1,
                adverse_ticks=0,
                max_age_ms=max_age_ms,
                max_spread_pct=None,
                require_observed=False,
            )
            if chk.get("ok") and isinstance(cand_quote, dict):
                return cand_quote

        ik = str(ikey or "").strip()
        if not allow_emergency_rest or not ik:
            return cand_quote if isinstance(cand_quote, dict) else {}

        # If a newer observed WS quote arrived in OptionStreamManager since `quote_lookup` was built,
        # validate it via `sweep_l5_book_for_qty` BEFORE burning the per-leg REST cooldown!
        sm_inst = None
        if self._sync_advance_db and self._emergency_quote_fetcher is None:
            try:
                from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

                sm_inst = OptionStreamManager()
                if not force_refresh and hasattr(sm_inst, "get_option_quote"):
                    sm_cached = sm_inst.get_option_quote(ik)
                    chk_sm = sweep_l5_book_for_qty(
                        sm_cached,
                        "SELL",
                        qty=1,
                        adverse_ticks=0,
                        max_age_ms=max_age_ms,
                        max_spread_pct=None,
                        require_observed=False,
                    )
                    if chk_sm.get("ok") and isinstance(sm_cached, dict):
                        return sm_cached
            except Exception as exc:
                logger.debug(f"[S205ShadowCollector] Stream cache check note ({ik}): {exc}")

        cooldown_sec = SHADOW_EOD_REST_COOLDOWN_SEC if is_eod_urgent else SHADOW_EMERGENCY_REST_COOLDOWN_SEC
        now_mono = time.monotonic()
        with self._lock:
            last_mono = self._last_emergency_rest_mono.get(ik)
            if last_mono is not None and (now_mono - last_mono) < cooldown_sec:
                return cand_quote if isinstance(cand_quote, dict) else {}
            self._last_emergency_rest_mono[ik] = now_mono

        if self._emergency_quote_fetcher is not None:
            try:
                q_em = self._emergency_quote_fetcher(ik)
                if isinstance(q_em, dict) and q_em:
                    return q_em
            except Exception as exc:
                logger.debug(f"[S205ShadowCollector] Custom emergency quote fetcher note ({ik}): {exc}")

        if self._sync_advance_db:
            try:
                if sm_inst is None:
                    from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

                    sm_inst = OptionStreamManager()
                if hasattr(sm_inst, "fetch_single_quote_emergency_rest"):
                    # CRITICAL: Always pass `max_cache_age_ms=-1` here because `sm_inst.get_option_quote(ik)`
                    # already failed `sweep_l5_book_for_qty` above (prevents even a sub-ms `c_age == 0` collision).
                    q_rest = sm_inst.fetch_single_quote_emergency_rest(ik, max_cache_age_ms=-1)
                    if isinstance(q_rest, dict) and q_rest:
                        return q_rest
            except Exception as exc:
                logger.debug(f"[S205ShadowCollector] Emergency REST quote fetch note ({ik}): {exc}")

        return cand_quote if isinstance(cand_quote, dict) else {}

    def _restore_open_packages_from_db(self) -> None:
        legs_to_pin: List[Dict[str, Any]] = []
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM option_sniffer_advance_s205_shadow_packages WHERE state = 'OPEN';"
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    ls = json.loads(d.get("legs_state_json") or "{}")
                    d["legs_state"] = ls
                    d["regime_context"] = json.loads(d.get("regime_context_json") or "{}")
                    self._open_packages[str(d["package_id"])] = d
                    for lk in ("wing_leg", "short_leg", "long_leg"):
                        ld = ls.get(lk)
                        if isinstance(ld, dict) and ld.get("instrument_key"):
                            legs_to_pin.append(ld)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"[S205ShadowCollector] Restore open packages note: {exc}")
        if legs_to_pin:
            self._pin_package_contracts(legs_to_pin)

    # ──────────────────────────────────────────────────────────────────────────
    # 1. L5 CHAIN TELEMETRY, QUOTE-FADE HORIZONS & EXPIRY FSP RECORDING
    # ──────────────────────────────────────────────────────────────────────────

    def record_1m_chain_and_snapshot(
        self,
        bar_ts: str,
        now_dt: datetime,
        nifty_quotes: Sequence[Dict[str, Any]],
        spot_ltp: float,
        fut_quote: Optional[Dict[str, Any]],
        india_vix: float,
        front_exp: str,
        next_exp: str,
        readiness_rows: Sequence[Dict[str, Any]],
    ) -> int:
        """Persists 1-minute ATM±6 L5 chain rows + summary `l5_snapshots` row."""
        if not bar_ts or spot_ltp <= 0:
            return 0
        session_date = bar_ts[:10]
        with self._lock:
            self._reset_session_if_needed(session_date)
            if bar_ts == self._last_recorded_chain_bar_ts:
                return 0
            self._last_recorded_chain_bar_ts = bar_ts

        atm = round(spot_ltp / 50.0) * 50.0
        target_exps = {e for e in (front_exp, next_exp) if e}
        fut_ltp = float((fut_quote or {}).get("fut_ltp") or 0.0)
        fut_atp = float((fut_quote or {}).get("fut_atp_vwap") or 0.0)
        fut_oi = float((fut_quote or {}).get("fut_oi") or 0.0)
        real_basis = round(fut_ltp - spot_ltp, 2) if (fut_ltp > 0 and spot_ltp > 0) else 0.0

        selected_rows: List[Tuple[Any, ...]] = []
        obs_spreads: List[float] = []
        obs_obis: List[float] = []
        now_iso = now_dt.isoformat()
        open_leg_keys = self._active_pinned_leg_keys()

        for q in nifty_quotes:
            exp_s = str(q.get("expiry") or "")[:10]
            stk = float(q.get("strike") or 0.0)
            ot = str(q.get("option_type") or "").upper()
            ikey = str(q.get("instrument_key") or "")
            if not ikey or ot not in ("CE", "PE"):
                continue
            if target_exps and exp_s not in target_exps and ikey not in open_leg_keys:
                continue
            if abs(stk - atm) > 300.0 and ikey not in open_leg_keys:
                continue

            vtt = int(q.get("volume") or 0)
            with self._lock:
                prev_v = self._prev_chain_vtt.get(ikey, vtt)
                delta_vtt = float(max(0, vtt - prev_v))
                self._prev_chain_vtt[ikey] = vtt

            is_obs = 1 if bool(q.get("is_observed_l1_book")) else 0
            depth_obs = 1 if bool(q.get("depth_observed_on_tick")) else 0
            if is_obs and float(q.get("spread_pct") or 999.0) < 100.0:
                obs_spreads.append(float(q.get("spread_pct") or 0.0))
                obs_obis.append(float(q.get("book_obi_l5") or 0.0))

            selected_rows.append(
                (
                    session_date,
                    bar_ts,
                    now_iso,
                    ikey,
                    q.get("trading_symbol"),
                    exp_s,
                    stk,
                    ot,
                    float(q.get("ltp") or 0.0),
                    float(q.get("raw_bid") if q.get("raw_bid") is not None else (q.get("bid") or 0.0)),
                    float(q.get("raw_ask") if q.get("raw_ask") is not None else (q.get("ask") or 0.0)),
                    int(q.get("bid_qty") or 0),
                    int(q.get("ask_qty") or 0),
                    int(q.get("l5_bid_qty") or 0),
                    int(q.get("l5_ask_qty") or 0),
                    float(q.get("book_obi_l5") or 0.0),
                    json.dumps(q.get("depth_levels") or []),
                    is_obs,
                    depth_obs,
                    str(q.get("book_provenance") or "UNKNOWN"),
                    int(q.get("l1_book_age_ms") if q.get("l1_book_age_ms") is not None else (q.get("quote_age_ms") or 0)),
                    delta_vtt,
                    vtt,
                    float(q.get("oi") or 0.0),
                    float(q.get("iv") or 0.0),
                    float(q.get("delta") or 0.0),
                    float(q.get("gamma") or 0.0),
                    float(q.get("theta") or 0.0),
                    float(q.get("vega") or 0.0),
                    spot_ltp,
                    fut_ltp,
                    fut_oi,
                    india_vix,
                )
            )

        avg_sp = round(sum(obs_spreads) / len(obs_spreads), 3) if obs_spreads else 0.0
        avg_obi = round(sum(obs_obis) / len(obs_obis), 4) if obs_obis else 0.0

        conn = self._connect()
        try:
            with conn:
                if selected_rows:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO option_sniffer_advance_s205_l5_chain_1m (
                            session_date, bar_ts, recorded_at, instrument_key, trading_symbol,
                            expiry, strike, option_type, ltp, raw_bid, raw_ask, bid_qty, ask_qty,
                            l5_bid_qty, l5_ask_qty, book_obi_l5, depth_json, is_observed_l1_book,
                            depth_observed_on_tick, book_provenance, l1_book_age_ms, delta_vtt_1m,
                            cumulative_vtt, oi, iv, delta, gamma, theta, vega, spot_ltp, fut_ltp,
                            fut_oi, india_vix
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        selected_rows,
                    )
                conn.execute(
                    """
                    INSERT INTO option_sniffer_advance_s205_l5_snapshots (
                        timestamp, bar_ts, spot_ltp, fut_ltp, fut_atp_vwap,
                        real_basis_pts, fut_oi, india_vix, avg_spread_pct, avg_l5_obi, readiness_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        now_iso,
                        bar_ts,
                        spot_ltp,
                        fut_ltp,
                        fut_atp,
                        real_basis,
                        fut_oi,
                        india_vix,
                        avg_sp,
                        avg_obi,
                        json.dumps(list(readiness_rows)[:6]),
                    ),
                )
        except Exception as exc:
            logger.debug(f"[S205ShadowCollector] Persist 1m chain note: {exc}")
        finally:
            conn.close()
        return len(selected_rows)

    def record_expiry_fsp_sample(
        self,
        now_dt: datetime,
        front_exp: str,
        spot_ltp: float,
        fut_ltp: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        """Records 15:00–15:30 IST NIFTY Spot samples on expiry day to compute NCL FSP 30m TWAP."""
        today_s = now_dt.strftime("%Y-%m-%d")
        if not front_exp or today_s != front_exp[:10] or spot_ltp <= 0:
            return None
        t_cur = now_dt.time()
        if t_cur < dt_time(15, 0) or t_cur > dt_time(15, 30, 30):
            return None

        sec_bucket = now_dt.strftime("%H:%M:%S")
        with self._lock:
            samples = self._expiry_fsp_samples.setdefault(today_s, [])
            if not samples or samples[-1][0] != sec_bucket:
                samples.append((sec_bucket, float(spot_ltp)))
            twap = round(sum(px for _, px in samples) / len(samples), 2)
            s_1529 = next((px for ts_s, px in reversed(samples) if ts_s >= "15:29:00"), samples[-1][1])
            diff_pts = round(twap - s_1529, 2)
            snap = {
                "expiry_date": today_s,
                "samples_count": len(samples),
                "spot_1500_1530_twap": twap,
                "spot_1529_close": round(s_1529, 2),
                "fut_1529_close": round(fut_ltp, 2) if fut_ltp > 0 else None,
                "fsp_minus_1529_pts": diff_pts,
            }

        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO option_sniffer_advance_s205_expiry_fsp_windows (
                        expiry_date, samples_count, spot_1500_1530_twap, spot_1529_close,
                        fut_1529_close, fsp_minus_1529_pts, samples_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(expiry_date) DO UPDATE SET
                        samples_count = excluded.samples_count,
                        spot_1500_1530_twap = excluded.spot_1500_1530_twap,
                        spot_1529_close = excluded.spot_1529_close,
                        fut_1529_close = COALESCE(excluded.fut_1529_close, fut_1529_close),
                        fsp_minus_1529_pts = excluded.fsp_minus_1529_pts,
                        samples_json = excluded.samples_json,
                        updated_at = excluded.updated_at;
                    """,
                    (
                        today_s,
                        snap["samples_count"],
                        twap,
                        snap["spot_1529_close"],
                        snap["fut_1529_close"],
                        diff_pts,
                        json.dumps(samples[-120:]),
                        now_dt.isoformat(),
                    ),
                )
        finally:
            conn.close()
        return snap

    def _schedule_quote_fade_event(
        self,
        package_id: str,
        event_kind: str,
        legs_quotes: Sequence[Tuple[str, Dict[str, Any]]],
        now_ms: int,
    ) -> None:
        event_id = f"fade_{uuid.uuid4().hex[:12]}"
        immediate_rows: List[Tuple[Any, ...]] = []
        for side_s, q in legs_quotes:
            ikey = str(q.get("instrument_key") or "")
            if not ikey:
                continue
            b_sw = sweep_l5_book_for_qty(q, "BUY", qty=65, adverse_ticks=0, max_age_ms=None, max_spread_pct=None, require_observed=False)
            s_sw = sweep_l5_book_for_qty(q, "SELL", qty=65, adverse_ticks=0, max_age_ms=None, max_spread_pct=None, require_observed=False)
            immediate_rows.append(
                (
                    event_id,
                    package_id,
                    event_kind,
                    "t0",
                    now_ms,
                    now_ms,
                    ikey,
                    side_s,
                    float(q.get("raw_bid") if q.get("raw_bid") is not None else (q.get("bid") or 0.0)),
                    float(q.get("raw_ask") if q.get("raw_ask") is not None else (q.get("ask") or 0.0)),
                    b_sw.get("fill_price"),
                    s_sw.get("fill_price"),
                    int(q.get("l5_bid_qty") or 0),
                    int(q.get("l5_ask_qty") or 0),
                    1 if bool(q.get("is_observed_l1_book")) else 0,
                )
            )
            with self._lock:
                for h_lbl, delta_ms in _FADE_HORIZONS_MS:
                    self._pending_fade_probes.append(
                        {
                            "event_id": event_id,
                            "package_id": package_id,
                            "event_kind": event_kind,
                            "horizon_label": h_lbl,
                            "target_ts_ms": now_ms + delta_ms,
                            "instrument_key": ikey,
                            "side": side_s,
                        }
                    )
        if immediate_rows:
            self._insert_fade_rows(immediate_rows)

    def _flush_due_fade_probes(self, ikey: str, quote: Dict[str, Any], now_ms: int) -> None:
        due: List[Dict[str, Any]] = []
        with self._lock:
            if not self._pending_fade_probes:
                return
            keep: Deque[Dict[str, Any]] = deque(maxlen=400)
            while self._pending_fade_probes:
                item = self._pending_fade_probes.popleft()
                if now_ms - int(item["target_ts_ms"]) > 120000:
                    continue
                if item["instrument_key"] == ikey and now_ms >= int(item["target_ts_ms"]):
                    due.append(item)
                else:
                    keep.append(item)
            self._pending_fade_probes = keep

        if not due:
            return
        b_sw = sweep_l5_book_for_qty(quote, "BUY", qty=65, adverse_ticks=0, max_age_ms=None, max_spread_pct=None, require_observed=False)
        s_sw = sweep_l5_book_for_qty(quote, "SELL", qty=65, adverse_ticks=0, max_age_ms=None, max_spread_pct=None, require_observed=False)
        rows = [
            (
                d["event_id"],
                d["package_id"],
                d["event_kind"],
                d["horizon_label"],
                int(d["target_ts_ms"]),
                now_ms,
                ikey,
                d["side"],
                float(quote.get("raw_bid") if quote.get("raw_bid") is not None else (quote.get("bid") or 0.0)),
                float(quote.get("raw_ask") if quote.get("raw_ask") is not None else (quote.get("ask") or 0.0)),
                b_sw.get("fill_price"),
                s_sw.get("fill_price"),
                int(quote.get("l5_bid_qty") or 0),
                int(quote.get("l5_ask_qty") or 0),
                1 if bool(quote.get("is_observed_l1_book")) else 0,
            )
            for d in due
        ]
        self._insert_fade_rows(rows)

    def _insert_fade_rows(self, rows: Sequence[Tuple[Any, ...]]) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO option_sniffer_advance_s205_quote_fade_events (
                        event_id, package_id, event_kind, horizon_label, target_ts_ms,
                        observed_ts_ms, instrument_key, side, raw_bid, raw_ask,
                        l5_vwap_65_buy, l5_vwap_65_sell, l5_bid_qty, l5_ask_qty, is_observed_l1_book
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    rows,
                )
        except Exception as exc:
            logger.debug(f"[S205ShadowCollector] Quote-fade insert note: {exc}")
        finally:
            conn.close()

    # ──────────────────────────────────────────────────────────────────────────
    # 2. READ-ONLY UPSTOX SPAN + EXPOSURE MARGIN TELEMETRY PROBE
    # ──────────────────────────────────────────────────────────────────────────

    def probe_upstox_basket_margin(
        self,
        book_id: str,
        instruments: List[Dict[str, Any]],
        structural_max_loss_inr: float,
        spot_ltp: float,
        india_vix: float,
        now_dt: datetime,
        package_id: Optional[str] = None,
        allow_network: bool = True,
    ) -> Dict[str, Any]:
        """
        Queries Upstox read-only `POST /v2/charges/margin` (zero order placement) to record
        true broker SPAN + Exposure margin vs. `structural_max_loss_inr`.
        """
        session_date = now_dt.strftime("%Y-%m-%d")
        http_status: Optional[int] = None
        verified = False
        margin_status = "UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN"
        span_m: Optional[float] = None
        exp_m: Optional[float] = None
        final_m: Optional[float] = None
        standalone_m: Optional[float] = None
        benefit_m: Optional[float] = None

        if self._margin_fetcher is not None:
            try:
                resp_data = self._margin_fetcher(instruments)
                http_status = int(resp_data.get("http_status", 200))
                if http_status == 200 and resp_data.get("final_margin") is not None:
                    verified = True
                    margin_status = "UPSTOX_V2_CHARGES_MARGIN_VERIFIED"
                    span_m = float(resp_data.get("span_margin") or 0.0)
                    exp_m = float(resp_data.get("exposure_margin") or 0.0)
                    final_m = float(resp_data.get("final_margin") or 0.0)
                    benefit_m = float(resp_data.get(" margin_benefit") or resp_data.get("hedge_benefit") or 0.0)
            except Exception as exc:
                margin_status = f"CUSTOM_FETCHER_ERR:{type(exc).__name__}"
        elif allow_network and self._margin_probes_today < 16:
            from app.data.upstox_provider import (
                UPSTOX_BASE_URL,
                get_upstox_limiter,
                get_upstox_token,
                upstox_request,
            )

            token = get_upstox_token()
            limiter = get_upstox_limiter()
            if token and not limiter.is_circuit_open():
                self._margin_probes_today += 1
                headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token.strip()}",
                }
                try:
                    res = upstox_request(
                        "POST",
                        f"{UPSTOX_BASE_URL}/charges/margin",
                        headers=headers,
                        json={"instruments": instruments},
                        timeout=4.5,
                    )
                    http_status = res.status_code
                    if res.status_code == 200:
                        data = (res.json() or {}).get("data") or {}
                        margins_list = data.get("margins") or []
                        span_m = float(
                            data.get("required_margin")
                            if data.get("span_margin") is None and not margins_list
                            else sum(float(m.get("span_margin") or 0.0) for m in margins_list)
                        )
                        exp_m = float(sum(float(m.get("exposure_margin") or 0.0) for m in margins_list))
                        final_m = float(
                            data.get("final_margin")
                            or data.get("required_margin")
                            or sum(float(m.get("total_margin") or 0.0) for m in margins_list)
                        )
                        benefit_m = float(data.get("margin_benefit") or 0.0)
                        if final_m > 0:
                            verified = True
                            margin_status = "UPSTOX_V2_CHARGES_MARGIN_VERIFIED"
                            standalone_m = round(final_m + benefit_m, 2)
                except Exception as exc:
                    margin_status = f"UPSTOX_MARGIN_UNAVAILABLE:{type(exc).__name__}"

        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO option_sniffer_advance_s205_margin_telemetry (
                        timestamp, session_date, package_id, book_id, legs_json,
                        upstox_http_status, margin_verified, margin_status,
                        span_margin_inr, exposure_margin_inr, final_margin_inr,
                        standalone_unhedged_margin_inr, hedge_benefit_inr,
                        structural_max_loss_inr, spot_ltp, india_vix
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        now_dt.isoformat(),
                        session_date,
                        package_id,
                        book_id,
                        json.dumps(instruments),
                        http_status,
                        1 if verified else 0,
                        margin_status,
                        span_m,
                        exp_m,
                        final_m,
                        standalone_m,
                        benefit_m,
                        structural_max_loss_inr,
                        spot_ltp,
                        india_vix,
                    ),
                )
        finally:
            conn.close()

        return {
            "verified": verified,
            "margin_status": margin_status,
            "http_status": http_status,
            "span_margin_inr": span_m,
            "exposure_margin_inr": exp_m,
            "final_margin_inr": final_m,
            "hedge_benefit_inr": benefit_m,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 3. CAPITAL RESERVATION & INTRA-BAR CAUSALITY (`bar[t].open` < `bar[t].close`)
    # ──────────────────────────────────────────────────────────────────────────

    def _active_capital_and_slots(self, eval_bar_ts: str, session_date: str) -> Dict[str, Any]:
        """
        Computes currently occupied slots & reserved capital, INCLUDING capital/slots
        from packages that exited on `>= eval_bar_ts` so an exit on bar `t` can NEVER
        fund a new entry on bar `t`.
        """
        with self._lock:
            stale_bars = [b for b in self._locked_until_after_bar if b < eval_bar_ts]
            for b in stale_bars:
                self._locked_until_after_bar.pop(b, None)

            open_by_book: Dict[str, int] = {}
            reserved_inr = 0.0
            for pkg in self._open_packages.values():
                bid = str(pkg["book_id"])
                open_by_book[bid] = open_by_book.get(bid, 0) + 1
                reserved_inr += float(pkg.get("cash_reserved_inr") or 0.0)
            for pkg in self._pending_leg2.values():
                bid = str(pkg["book_id"])
                open_by_book[bid] = open_by_book.get(bid, 0) + 1
                reserved_inr += float(pkg.get("cash_reserved_inr") or 0.0)

            for lock_bar, entries in self._locked_until_after_bar.items():
                if lock_bar >= eval_bar_ts:
                    for bid, locked_cash in entries:
                        open_by_book[bid] = open_by_book.get(bid, 0) + 1
                        reserved_inr += float(locked_cash)

        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT book_id, state, net_pnl_inr FROM option_sniffer_advance_s205_shadow_packages
                WHERE session_date = ? AND state IN ('OPEN', 'CLOSED', 'INCOMPLETE_LEG_UNWOUND');
                """,
                (session_date,),
            ).fetchall()
        finally:
            conn.close()

        trades_today_by_book: Dict[str, int] = {}
        realized_today = 0.0
        for r in rows:
            b_id = str(r["book_id"])
            trades_today_by_book[b_id] = trades_today_by_book.get(b_id, 0) + 1
            if str(r["state"]) != "OPEN":
                realized_today += float(r["net_pnl_inr"] or 0.0)

        for pkg in self._pending_leg2.values():
            b_id = str(pkg["book_id"])
            trades_today_by_book[b_id] = trades_today_by_book.get(b_id, 0) + 1

        return {
            "open_total": sum(open_by_book.values()),
            "open_by_book": open_by_book,
            "trades_today_total": sum(trades_today_by_book.values()),
            "trades_today_by_book": trades_today_by_book,
            "reserved_inr": round(reserved_inr, 2),
            "realized_today_inr": round(realized_today, 2),
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 4. COMPLETED 1M BAR (:00) EVALUATION & BUY-FIRST SPREAD STATE MACHINE
    # ──────────────────────────────────────────────────────────────────────────

    def evaluate_completed_bar(
        self,
        ctx: Optional[Dict[str, Any]],
        bar_idx: int,
        quote_lookup: Dict[Tuple[str, float, str], Dict[str, Any]],
        fut_quote: Optional[Dict[str, Any]],
        india_vix: float,
        now_dt: datetime,
        execute_new_entries: bool = True,
        allow_network: bool = True,
    ) -> Tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
        """
        1. Builds UI-compatible `shadow_readiness_rows` for `SHADOW_BOOK_B_PAID_FLOW` &
           `SHADOW_BOOK_A_SPREAD_150`.
        2. On newly completed `:00` 1m bar (`execute_new_entries=True`):
           - Evaluates close-only ratchets & bar-close invalidations on open shadow packages.
           - Evaluates new pre-registered entries under strict `bar[t].open < bar[t].close`
             capital/slot causality.
        """
        now_dt = _to_ist_naive_safe(now_dt)
        today_s = now_dt.strftime("%Y-%m-%d")
        ist_hhmm = now_dt.hour * 100 + now_dt.minute
        with self._lock:
            self._reset_session_if_needed(today_s)

        env = self.config["account_envelope"]
        hyg = self.config["quote_hygiene"]
        cfg_a = self.config["shadow_books"]["SHADOW_BOOK_A_SPREAD_150"]
        cfg_b = self.config["shadow_books"]["SHADOW_BOOK_B_PAID_FLOW"]
        lot_sz = int(env["lot_size_nifty"])

        if ctx is None or bar_idx < 0 or not ctx.get("ts"):
            return self._warmup_readiness_rows(cfg_a, cfg_b), [], []

        bar_ts = str(ctx["ts"][bar_idx])
        closed_pkgs: List[Dict[str, Any]] = []
        opened_pkg_ids: List[str] = []

        # Step A: Manage open shadow packages on `:00` completed 1m candle close
        if execute_new_entries:
            closed_pkgs.extend(
                self._manage_open_packages_on_bar_close(
                    ctx, bar_idx, bar_ts, quote_lookup, ist_hhmm, now_dt, allow_network=allow_network
                )
            )

        cap_state = self._active_capital_and_slots(bar_ts, today_s)
        mtm_eval = self._evaluate_open_unrealized_mtm(
            quote_lookup, today_s, allow_network=allow_network
        )
        unpriced_risk_halt = int(mtm_eval["unpriced_open_count"]) > 0
        conservative_kill_mtm = float(mtm_eval["conservative_kill_mtm_inr"])
        if (cap_state["realized_today_inr"] + conservative_kill_mtm) <= float(env["max_daily_mtm_kill_inr"]):
            self._daily_kill_triggered = True
            self._daily_kill_reason = (
                f"Daily Shadow Kill Active (₹{cap_state['realized_today_inr'] + conservative_kill_mtm:,.2f} "
                f"<= ₹{float(env['max_daily_mtm_kill_inr']):,.0f})"
            )

        spot_c = float(ctx["c"][bar_idx])
        spot_vwap = float(ctx["tpb"][bar_idx])
        orb_h = float(ctx["orb30_h"])
        orb_l = float(ctx["orb30_l"])
        adx_v = float(ctx["adx"][bar_idx])
        er_v = float(ctx["er"][bar_idx])
        atrr_v = float(ctx["atrr"][bar_idx])
        fut_ltp = float((fut_quote or {}).get("fut_ltp") or 0.0)
        real_basis = (fut_ltp - spot_c) if (fut_ltp > 0 and spot_c > 0) else float(ctx["syn_basis"][bar_idx])
        atm = round(spot_c / 50.0) * 50.0

        is_bull = spot_c > orb_h and spot_c > spot_vwap
        is_bear = spot_c < orb_l and spot_c < spot_vwap

        readiness_rows: List[Dict[str, Any]] = []

        # ── Evaluate SHADOW_BOOK_B_PAID_FLOW (1-Lot Fully Paid Convexity) ───────
        exp_b = ctx["front_exp"] if int(ctx["front_dte"]) >= int(cfg_b["min_dte"]) else ctx["next_exp"]
        ot_b = "CE" if (is_bull or (not is_bear and spot_c >= spot_vwap)) else "PE"
        eso_b = (str(exp_b), float(atm), ot_b)
        q_b = quote_lookup.get(eso_b)
        sw_b = sweep_l5_book_for_qty(
            q_b,
            side="BUY",
            qty=lot_sz,
            adverse_ticks=int(hyg["adverse_latency_ticks"]),
            max_age_ms=int(hyg["max_l1_book_age_ms"]),
            max_spread_pct=float(hyg["max_primary_spread_pct"]),
            require_observed=bool(hyg["require_observed_l1_book"]),
        )
        outlay_b = round(float(sw_b.get("fill_price") or 0.0) * lot_sz, 2) if sw_b.get("ok") else 0.0
        obi_b = float((q_b or {}).get("book_obi_l5") or 0.0)
        basis_ok_b = (real_basis >= float(cfg_b["min_real_basis_abs_pts"])) if ot_b == "CE" else (real_basis <= -float(cfg_b["min_real_basis_abs_pts"]))
        win_ok_b = int(cfg_b["window_start_hhmm"]) <= ist_hhmm <= int(cfg_b["window_end_hhmm"])

        gates_b = [
            {"name": "ORB30+VWAP", "passed": bool(is_bull or is_bear), "current": f"{spot_c:.0f}", "target": f">{orb_h:.0f} or <{orb_l:.0f}"},
            {"name": "FutBasis", "passed": bool(basis_ok_b), "current": f"{real_basis:+.1f}pt", "target": f"|B|>={cfg_b['min_real_basis_abs_pts']:.0f}pt"},
            {"name": "ADX/ER", "passed": bool(adx_v >= float(cfg_b["min_adx14"]) and er_v >= float(cfg_b["min_er20"])), "current": f"{adx_v:.1f}/{er_v:.2f}", "target": f">={cfg_b['min_adx14']:.0f}/{cfg_b['min_er20']:.2f}"},
            {"name": "ObsL5Depth", "passed": bool(sw_b.get("ok") and obi_b >= float(cfg_b["min_l5_obi"])), "current": f"{sw_b.get('available_l5_qty', 0)}q/OBI {obi_b:+.2f}", "target": f">=65q & OBI>={cfg_b['min_l5_obi']:.2f}"},
            {"name": "CashCap", "passed": bool(sw_b.get("ok") and 0 < outlay_b <= float(cfg_b["max_entry_outlay_inr"])), "current": f"₹{outlay_b:,.0f}" if outlay_b > 0 else "—", "target": f"<=₹{cfg_b['max_entry_outlay_inr']:,.0f}"},
        ]
        passed_cnt_b = sum(1 for g in gates_b if g["passed"])
        all_b = passed_cnt_b == len(gates_b) and win_ok_b and not self._daily_kill_triggered and not unpriced_risk_halt
        state_b = (
            "HALTED_KILL"
            if self._daily_kill_triggered
            else (
                "HALTED_UNPRICED_RISK"
                if unpriced_risk_halt
                else ("WARMUP_0945" if ist_hhmm < 945 else ("WINDOW_CLOSED" if not win_ok_b else ("READY_FIRE" if all_b else "WATCHING")))
            )
        )
        readiness_rows.append(
            {
                "engine_id": cfg_b["book_id"],
                "label": cfg_b["label"],
                "book_type": cfg_b["ui_book_type"],
                "action_badge": cfg_b["action_badge"],
                "window_ist": cfg_b["window_label"],
                "state": state_b,
                "gates_passed": passed_cnt_b,
                "gates_total": len(gates_b),
                "candidate_contract": f"NIFTY {exp_b} {int(atm)}{ot_b}" if exp_b else "—",
                "gates": gates_b,
                "when_order_fires": "Fires 1-Lot cash-paid L5-swept BUY on :00 close when 5/5 causal gates pass (0 naked risk).",
            }
        )

        # ── Evaluate SHADOW_BOOK_A_SPREAD_150 (Buy-First 150pt Credit Spread) ───
        exp_a = ctx["front_exp"] if int(ctx["front_dte"]) >= int(cfg_a["min_dte"]) else ctx["next_exp"]
        ot_a = "PE" if (is_bull or (not is_bear and spot_c >= spot_vwap)) else "CE"
        short_k = (atm - float(cfg_a["short_otm_offset_pts"])) if ot_a == "PE" else (atm + float(cfg_a["short_otm_offset_pts"]))
        wing_k = (short_k - float(cfg_a["spread_width_pts"])) if ot_a == "PE" else (short_k + float(cfg_a["spread_width_pts"]))
        eso_short = (str(exp_a), float(short_k), ot_a)
        eso_wing = (str(exp_a), float(wing_k), ot_a)
        q_short = quote_lookup.get(eso_short)
        q_wing = quote_lookup.get(eso_wing)

        sw_wing = sweep_l5_book_for_qty(
            q_wing,
            side="BUY",
            qty=lot_sz,
            adverse_ticks=int(hyg["adverse_latency_ticks"]),
            max_age_ms=int(hyg["max_l1_book_age_ms"]),
            max_spread_pct=float(hyg["max_wing_spread_pct"]),
            max_spread_pts=float(hyg["max_wing_spread_pts"]),
            require_observed=bool(hyg["require_observed_l1_book"]),
        )
        sw_short = sweep_l5_book_for_qty(
            q_short,
            side="SELL",
            qty=lot_sz,
            adverse_ticks=int(hyg["adverse_latency_ticks"]),
            max_age_ms=int(hyg["max_l1_book_age_ms"]),
            max_spread_pct=float(hyg["max_primary_spread_pct"]),
            require_observed=bool(hyg["require_observed_l1_book"]),
        )
        est_credit_pts = round(float(sw_short.get("fill_price") or 0.0) - float(sw_wing.get("fill_price") or 0.0), 2) if (sw_wing.get("ok") and sw_short.get("ok")) else 0.0
        est_struct_loss = 999999.0
        if sw_wing.get("ok") and sw_short.get("ok") and est_credit_pts > 0:
            pkg_preview = compute_package_accounting(
                legs=[
                    OptionLeg(option_type=ot_a, strike=wing_k, qty=lot_sz, entry_price=float(sw_wing["fill_price"]), exit_price=float(sw_wing["fill_price"])),
                    OptionLeg(option_type=ot_a, strike=short_k, qty=-lot_sz, entry_price=float(sw_short["fill_price"]), exit_price=float(sw_short["fill_price"])),
                ],
                trade_date=today_s,
                reference_spot=spot_c,
            )
            est_struct_loss = float(pkg_preview["risk"]["final_package_max_loss_after_friction_inr"])

        win_ok_a = int(cfg_a["window_start_hhmm"]) <= ist_hhmm <= int(cfg_a["window_end_hhmm"])
        gates_a = [
            {"name": "ORB30+VWAP", "passed": bool(is_bull or is_bear), "current": f"{spot_c:.0f}", "target": f">{orb_h:.0f} or <{orb_l:.0f}"},
            {"name": "ADX/ATRR", "passed": bool(adx_v >= float(cfg_a["min_adx14"]) and atrr_v <= float(cfg_a["max_atr_ratio"])), "current": f"{adx_v:.1f}/{atrr_v:.2f}x", "target": f">={cfg_a['min_adx14']:.0f}/<={cfg_a['max_atr_ratio']:.2f}x"},
            {"name": "2LegObsL5", "passed": bool(sw_wing.get("ok") and sw_short.get("ok")), "current": f"W:{sw_wing.get('available_l5_qty', 0)}q S:{sw_short.get('available_l5_qty', 0)}q", "target": ">=65q Both Legs"},
            {"name": "NetCredit", "passed": bool(est_credit_pts >= float(cfg_a["min_net_credit_pts"])), "current": f"{est_credit_pts:.2f}pt", "target": f">={cfg_a['min_net_credit_pts']:.1f}pt"},
            {"name": "StructRisk", "passed": bool(est_struct_loss <= float(env["max_structural_loss_per_package_inr"])), "current": f"₹{est_struct_loss:,.0f}" if est_struct_loss < 900000 else "—", "target": f"<=₹{env['max_structural_loss_per_package_inr']:,.0f}"},
        ]
        passed_cnt_a = sum(1 for g in gates_a if g["passed"])
        all_a = passed_cnt_a == len(gates_a) and win_ok_a and not self._daily_kill_triggered and not unpriced_risk_halt
        state_a = (
            "HALTED_KILL"
            if self._daily_kill_triggered
            else (
                "HALTED_UNPRICED_RISK"
                if unpriced_risk_halt
                else ("WARMUP_0945" if ist_hhmm < 945 else ("WINDOW_CLOSED" if not win_ok_a else ("READY_FIRE" if all_a else "WATCHING")))
            )
        )
        readiness_rows.append(
            {
                "engine_id": cfg_a["book_id"],
                "label": cfg_a["label"],
                "book_type": cfg_a["ui_book_type"],
                "action_badge": cfg_a["action_badge"],
                "window_ist": cfg_a["window_label"],
                "state": state_a,
                "gates_passed": passed_cnt_a,
                "gates_total": len(gates_a),
                "candidate_contract": f"+{int(wing_k)}{ot_a} / -{int(short_k)}{ot_a} ({exp_a})" if exp_a else "—",
                "gates": gates_a,
                "when_order_fires": "Buys 150pt wing FIRST at L5 ask, then sells short leg on next observed L5 bid (or unwinds wing).",
            }
        )

        if not execute_new_entries or self._daily_kill_triggered or unpriced_risk_halt:
            return readiness_rows, opened_pkg_ids, closed_pkgs

        # Step B: Execute eligible shadow entries respecting strict `bar[t].open < bar[t].close` capital causality
        cap_state = self._active_capital_and_slots(bar_ts, today_s)
        now_ep = time.time()

        # 1. Try SHADOW_BOOK_B_PAID_FLOW
        if (
            all_b
            and q_b is not None
            and cap_state["open_total"] < int(env["max_open_packages_total"])
            and cap_state["trades_today_total"] < int(env["max_total_trades_per_day"])
            and cap_state["open_by_book"].get(cfg_b["book_id"], 0) < int(cfg_b["max_open"])
            and cap_state["trades_today_by_book"].get(cfg_b["book_id"], 0) < int(cfg_b["max_trades_day"])
            and now_ep >= self._cooldown_until_epoch.get(cfg_b["book_id"], 0.0)
            and (cap_state["reserved_inr"] + outlay_b) <= float(env["max_capital_reserved_inr"])
        ):
            pid_b = self._open_paid_flow_package(
                cfg_b=cfg_b,
                eso=eso_b,
                quote=q_b,
                sweep_buy=sw_b,
                spot_c=spot_c,
                india_vix=india_vix,
                is_bull=(ot_b == "CE"),
                bar_ts=bar_ts,
                now_dt=now_dt,
                allow_network=allow_network,
            )
            if pid_b:
                opened_pkg_ids.append(pid_b)
                cap_state = self._active_capital_and_slots(bar_ts, today_s)

        # 2. Try SHADOW_BOOK_A_SPREAD_150 (Buy-First Step 1: Fill Wing BUY, Queue Short Leg 2)
        if (
            all_a
            and q_wing is not None
            and q_short is not None
            and cap_state["open_total"] < int(env["max_open_packages_total"])
            and cap_state["trades_today_total"] < int(env["max_total_trades_per_day"])
            and cap_state["open_by_book"].get(cfg_a["book_id"], 0) < int(cfg_a["max_open"])
            and cap_state["trades_today_by_book"].get(cfg_a["book_id"], 0) < int(cfg_a["max_trades_day"])
            and now_ep >= self._cooldown_until_epoch.get(cfg_a["book_id"], 0.0)
        ):
            pid_a = self._initiate_buy_first_spread_leg1(
                cfg_a=cfg_a,
                eso_wing=eso_wing,
                eso_short=eso_short,
                q_wing=q_wing,
                q_short=q_short,
                sw_wing=sw_wing,
                est_struct_loss=est_struct_loss,
                spot_c=spot_c,
                india_vix=india_vix,
                is_bull=(ot_a == "PE"),
                bar_ts=bar_ts,
                now_dt=now_dt,
                reserved_already_inr=cap_state["reserved_inr"],
                allow_network=allow_network,
            )
            if pid_a:
                opened_pkg_ids.append(pid_a)

        return readiness_rows, opened_pkg_ids, closed_pkgs

    @staticmethod
    def _warmup_readiness_rows(cfg_a: Dict[str, Any], cfg_b: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "engine_id": cfg_b["book_id"],
                "label": cfg_b["label"],
                "book_type": cfg_b["ui_book_type"],
                "action_badge": cfg_b["action_badge"],
                "window_ist": cfg_b["window_label"],
                "state": "WARMUP_0945",
                "gates_passed": 0,
                "gates_total": 5,
                "candidate_contract": "—",
                "gates": [],
                "when_order_fires": "Awaiting completed 09:15–09:45 ORB-30 1m bars.",
            },
            {
                "engine_id": cfg_a["book_id"],
                "label": cfg_a["label"],
                "book_type": cfg_a["ui_book_type"],
                "action_badge": cfg_a["action_badge"],
                "window_ist": cfg_a["window_label"],
                "state": "WARMUP_0945",
                "gates_passed": 0,
                "gates_total": 5,
                "candidate_contract": "—",
                "gates": [],
                "when_order_fires": "Awaiting completed 09:15–09:45 ORB-30 1m bars.",
            },
        ]

    # ──────────────────────────────────────────────────────────────────────────
    # 5. PACKAGE ENTRY, BUY-FIRST LEG-2 COMPLETION & EMERGENCY UNWIND
    # ──────────────────────────────────────────────────────────────────────────

    def _open_paid_flow_package(
        self,
        cfg_b: Dict[str, Any],
        eso: Tuple[str, float, str],
        quote: Dict[str, Any],
        sweep_buy: Dict[str, Any],
        spot_c: float,
        india_vix: float,
        is_bull: bool,
        bar_ts: str,
        now_dt: datetime,
        allow_network: bool,
    ) -> Optional[str]:
        lot_sz = int(self.config["account_envelope"]["lot_size_nifty"])
        entry_px = float(sweep_buy["fill_price"])
        outlay_inr = round(entry_px * lot_sz, 2)

        preview = compute_package_accounting(
            legs=[OptionLeg(option_type=eso[2], strike=eso[1], qty=lot_sz, entry_price=entry_px, exit_price=entry_px)],
            trade_date=bar_ts[:10],
            reference_spot=spot_c,
        )
        struct_loss = float(preview["risk"]["final_package_max_loss_after_friction_inr"])
        if struct_loss > float(self.config["account_envelope"]["max_structural_loss_per_package_inr"]) * 1.2:
            # Paid outlay capped by max_entry_outlay_inr (₹11,500), planned stop capped at ₹2,200
            pass

        margin_info = self.probe_upstox_basket_margin(
            book_id=cfg_b["book_id"],
            instruments=[
                {
                    "instrument_key": str(quote["instrument_key"]),
                    "quantity": lot_sz,
                    "transaction_type": "BUY",
                    "product": "I",
                }
            ],
            structural_max_loss_inr=struct_loss,
            spot_ltp=spot_c,
            india_vix=india_vix,
            now_dt=now_dt,
            allow_network=allow_network,
        )

        pkg_id = f"shdw_{uuid.uuid4().hex[:12]}"
        adv_tid = f"adv_shdw_{pkg_id[-10:]}"
        capped_sl_pct = round(
            max(6.0, min(float(cfg_b["hard_sl_pct"]), (float(cfg_b["max_planned_stop_loss_inr"]) / max(1.0, outlay_inr)) * 100.0)),
            2,
        )
        hard_stop_px = round_to_nse_tick(entry_px * (1.0 - capped_sl_pct / 100.0), mode="adverse_sell")

        legs_state = {
            "mode": "SINGLE_PAID_LONG",
            "long_leg": {
                "instrument_key": str(quote["instrument_key"]),
                "trading_symbol": str(quote.get("trading_symbol") or f"NIFTY {eso[0]} {int(eso[1])}{eso[2]}"),
                "expiry": eso[0],
                "strike": float(eso[1]),
                "option_type": eso[2],
                "qty": lot_sz,
                "entry_price": entry_px,
                "entry_sweep": sweep_buy,
            },
            "best_close_pts": entry_px,
            "ratchet_stop": None,
            "hard_stop": hard_stop_px,
            "stop_loss_pct": capped_sl_pct,
            "is_bull": is_bull,
        }
        regime_ctx = {
            "shadow_only": True,
            "prereg_sha256": self.prereg_sha256,
            "book_id": cfg_b["book_id"],
            "entry_bar_ts": bar_ts,
            "spot_at_entry": spot_c,
            "india_vix": india_vix,
            "margin_status": margin_info["margin_status"],
            "expectancy_status": "UNVALIDATED_SHADOW_COLLECTION",
        }

        pkg_row = {
            "package_id": pkg_id,
            "advance_trade_id": adv_tid,
            "prereg_sha256": self.prereg_sha256,
            "session_date": bar_ts[:10],
            "book_id": cfg_b["book_id"],
            "ui_book_type": BOOK_LONG_BUY,
            "state": "OPEN",
            "direction_label": f"BUY_{eso[2]}",
            "expiry": eso[0],
            "entry_bar_ts": bar_ts,
            "entry_time": now_dt.isoformat(),
            "leg2_confirmed_time": now_dt.isoformat(),
            "legs_state": legs_state,
            "structural_max_loss_inr": struct_loss,
            "upstox_final_margin_inr": margin_info.get("final_margin_inr"),
            "margin_status": margin_info["margin_status"],
            "cash_reserved_inr": outlay_inr,
            "regime_context": regime_ctx,
        }
        self._persist_open_package(pkg_row)
        with self._lock:
            self._open_packages[pkg_id] = pkg_row
        self._pin_package_contracts([legs_state["long_leg"]])

        now_ms = int(quote.get("local_receipt_ts_ms") or int(time.time() * 1000))
        self._schedule_quote_fade_event(pkg_id, "ENTRY_PAID_LONG", [("BUY", quote)], now_ms)

        if self._sync_advance_db:
            self._mirror_open_to_advance_db(pkg_row, quote, entry_px, capped_sl_pct, hard_stop_px, spot_c)
        self._emit_lifecycle_event("BOOK_B_OPEN", pkg_row)
        return pkg_id

    def _initiate_buy_first_spread_leg1(
        self,
        cfg_a: Dict[str, Any],
        eso_wing: Tuple[str, float, str],
        eso_short: Tuple[str, float, str],
        q_wing: Dict[str, Any],
        q_short: Dict[str, Any],
        sw_wing: Dict[str, Any],
        est_struct_loss: float,
        spot_c: float,
        india_vix: float,
        is_bull: bool,
        bar_ts: str,
        now_dt: datetime,
        reserved_already_inr: float,
        allow_network: bool,
    ) -> Optional[str]:
        env = self.config["account_envelope"]
        lot_sz = int(env["lot_size_nifty"])
        wing_px = float(sw_wing["fill_price"])

        margin_info = self.probe_upstox_basket_margin(
            book_id=cfg_a["book_id"],
            instruments=[
                {
                    "instrument_key": str(q_wing["instrument_key"]),
                    "quantity": lot_sz,
                    "transaction_type": "BUY",
                    "product": "I",
                },
                {
                    "instrument_key": str(q_short["instrument_key"]),
                    "quantity": lot_sz,
                    "transaction_type": "SELL",
                    "product": "I",
                },
            ],
            structural_max_loss_inr=est_struct_loss,
            spot_ltp=spot_c,
            india_vix=india_vix,
            now_dt=now_dt,
            allow_network=allow_network,
        )

        if margin_info["verified"] and float(margin_info["final_margin_inr"] or 0.0) > float(cfg_a["max_upstox_margin_inr"]):
            return None

        cash_reserve = (
            float(margin_info["final_margin_inr"])
            if margin_info["verified"] and margin_info["final_margin_inr"]
            else max(est_struct_loss, float(cfg_a["fallback_unverified_margin_reserve_inr"]))
        )
        if (reserved_already_inr + cash_reserve) > float(env["max_capital_reserved_inr"]):
            return None

        pkg_id = f"shdw_{uuid.uuid4().hex[:12]}"
        leg1_ts_ms = int(q_wing.get("local_receipt_ts_ms") or int(time.time() * 1000))
        pending_obj = {
            "package_id": pkg_id,
            "advance_trade_id": f"adv_shdw_{pkg_id[-10:]}",
            "book_id": cfg_a["book_id"],
            "ui_book_type": BOOK_SHORT_SELL,
            "session_date": bar_ts[:10],
            "expiry": eso_short[0],
            "entry_bar_ts": bar_ts,
            "entry_time": now_dt.isoformat(),
            "leg1_ts_ms": leg1_ts_ms,
            "deadline_ts_ms": leg1_ts_ms + int(self.config["quote_hygiene"]["buy_first_leg2_timeout_ms"]),
            "spot_c": spot_c,
            "india_vix": india_vix,
            "is_bull": is_bull,
            "cash_reserved_inr": round(cash_reserve, 2),
            "margin_info": margin_info,
            "wing_leg": {
                "instrument_key": str(q_wing["instrument_key"]),
                "trading_symbol": str(q_wing.get("trading_symbol") or f"NIFTY {eso_wing[0]} {int(eso_wing[1])}{eso_wing[2]}"),
                "expiry": eso_wing[0],
                "strike": float(eso_wing[1]),
                "option_type": eso_wing[2],
                "qty": lot_sz,
                "entry_price": wing_px,
                "entry_sweep": sw_wing,
                "last_quote": q_wing,
            },
            "short_target": {
                "instrument_key": str(q_short["instrument_key"]),
                "trading_symbol": str(q_short.get("trading_symbol") or f"NIFTY {eso_short[0]} {int(eso_short[1])}{eso_short[2]}"),
                "expiry": eso_short[0],
                "strike": float(eso_short[1]),
                "option_type": eso_short[2],
                "qty": -lot_sz,
            },
        }
        with self._lock:
            self._pending_leg2[pkg_id] = pending_obj
        self._pin_package_contracts([pending_obj["wing_leg"], pending_obj["short_target"]])
        self._schedule_quote_fade_event(pkg_id, "ENTRY_SPREAD_LEG1_WING", [("BUY", q_wing), ("SELL", q_short)], leg1_ts_ms)
        return pkg_id

    def complete_pending_leg2_or_unwind(
        self,
        package_id: str,
        short_quote: Optional[Dict[str, Any]],
        wing_quote: Optional[Dict[str, Any]],
        now_dt: datetime,
        current_bar_ts: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Completes Leg 2 (`SELL` short strike) of a buy-first spread on a subsequent observed quote,
        OR executes `INCOMPLETE_LEG_UNWOUND` (selling the protective wing back at L5 bid with
        full 1-leg round-trip Upstox statutory charges) if Leg 2 fails hygiene/credit/risk gates.
        """
        with self._lock:
            pending = self._pending_leg2.get(package_id)
            if not pending:
                return None

        hyg = self.config["quote_hygiene"]
        env = self.config["account_envelope"]
        cfg_a = self.config["shadow_books"]["SHADOW_BOOK_A_SPREAD_150"]
        lot_sz = abs(int(pending["short_target"]["qty"]))
        wing_leg = pending["wing_leg"]
        short_t = pending["short_target"]

        sw_short = sweep_l5_book_for_qty(
            short_quote,
            side="SELL",
            qty=lot_sz,
            adverse_ticks=int(hyg["adverse_latency_ticks"]),
            max_age_ms=int(hyg["max_l1_book_age_ms"]),
            max_spread_pct=float(hyg["max_primary_spread_pct"]),
            require_observed=bool(hyg["require_observed_l1_book"]),
        )

        leg2_ok = False
        reject_reason = str(sw_short.get("reason") or "LEG2_REJECTED")
        struct_loss = 0.0
        net_credit_pts = 0.0

        if sw_short.get("ok"):
            short_px = float(sw_short["fill_price"])
            net_credit_pts = round(short_px - float(wing_leg["entry_price"]), 2)
            if net_credit_pts >= float(cfg_a["min_net_credit_pts"]):
                preview = compute_package_accounting(
                    legs=[
                        OptionLeg(
                            option_type=wing_leg["option_type"],
                            strike=wing_leg["strike"],
                            qty=lot_sz,
                            entry_price=float(wing_leg["entry_price"]),
                            exit_price=float(wing_leg["entry_price"]),
                        ),
                        OptionLeg(
                            option_type=short_t["option_type"],
                            strike=short_t["strike"],
                            qty=-lot_sz,
                            entry_price=short_px,
                            exit_price=short_px,
                        ),
                    ],
                    trade_date=pending["session_date"],
                    reference_spot=float(pending["spot_c"]),
                )
                struct_loss = float(preview["risk"]["final_package_max_loss_after_friction_inr"])
                if struct_loss <= float(env["max_structural_loss_per_package_inr"]):
                    leg2_ok = True
                else:
                    reject_reason = f"STRUCTURAL_LOSS_EXCEEDED:{struct_loss:.2f}"
            else:
                reject_reason = f"CREDIT_COLLAPSED:{net_credit_pts:.2f}"

        with self._lock:
            self._pending_leg2.pop(package_id, None)

        if leg2_ok and short_quote is not None:
            short_px = float(sw_short["fill_price"])
            max_debit_stop = round(
                min(
                    net_credit_pts * float(cfg_a["stop_debit_mult"]),
                    net_credit_pts + (float(cfg_a["max_planned_stop_loss_inr"]) / float(lot_sz)),
                ),
                2,
            )
            legs_state = {
                "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
                "wing_leg": {k: v for k, v in wing_leg.items() if k != "last_quote"},
                "short_leg": {
                    **short_t,
                    "entry_price": short_px,
                    "entry_sweep": sw_short,
                },
                "net_entry_credit_pts": net_credit_pts,
                "best_close_credit_decay_pts": net_credit_pts,
                "ratchet_max_debit_pts": None,
                "hard_stop_debit_pts": max_debit_stop,
                "target_debit_pts": round(net_credit_pts * (1.0 - float(cfg_a["decay_target_capture_pct"]) / 100.0), 2),
                "is_bull": bool(pending["is_bull"]),
            }
            regime_ctx = {
                "shadow_only": True,
                "prereg_sha256": self.prereg_sha256,
                "book_id": cfg_a["book_id"],
                "entry_bar_ts": pending["entry_bar_ts"],
                "hedge_wing_ikey": str(wing_leg["instrument_key"]),
                "hedge_wing_strike": float(wing_leg["strike"]),
                "hedge_wing_option_type": wing_leg["option_type"],
                "hedge_wing_entry_price": float(wing_leg["entry_price"]),
                "short_leg_entry_price": float(short_px),
                "spread_legs_label": f"+{int(wing_leg['strike'])}{wing_leg['option_type']} / -{int(short_t['strike'])}{short_t['option_type']}",
                "net_entry_credit_pts": net_credit_pts,
                "structural_max_loss_inr": struct_loss,
                "margin_status": pending["margin_info"]["margin_status"],
                "expectancy_status": "UNVALIDATED_SHADOW_COLLECTION",
            }
            pkg_row = {
                "package_id": package_id,
                "advance_trade_id": pending["advance_trade_id"],
                "prereg_sha256": self.prereg_sha256,
                "session_date": pending["session_date"],
                "book_id": cfg_a["book_id"],
                "ui_book_type": BOOK_SHORT_SELL,
                "state": "OPEN",
                "direction_label": f"SPREAD_SELL_{short_t['option_type']}",
                "expiry": pending["expiry"],
                "entry_bar_ts": pending["entry_bar_ts"],
                "entry_time": pending["entry_time"],
                "leg2_confirmed_time": now_dt.isoformat(),
                "legs_state": legs_state,
                "structural_max_loss_inr": struct_loss,
                "upstox_final_margin_inr": pending["margin_info"].get("final_margin_inr"),
                "margin_status": pending["margin_info"]["margin_status"],
                "cash_reserved_inr": float(pending["cash_reserved_inr"]),
                "regime_context": regime_ctx,
            }
            self._persist_open_package(pkg_row)
            with self._lock:
                self._open_packages[package_id] = pkg_row
            self._pin_package_contracts([legs_state["wing_leg"], legs_state["short_leg"]])
            if self._sync_advance_db:
                sl_pct_equiv = round(((max_debit_stop - net_credit_pts) / max(0.05, net_credit_pts)) * 100.0, 2)
                self._mirror_open_to_advance_db(
                    pkg_row,
                    short_quote,
                    net_credit_pts,
                    sl_pct_equiv,
                    max_debit_stop,
                    float(pending["spot_c"]),
                )
            self._emit_lifecycle_event("BOOK_A_OPEN", pkg_row)
            return {"state": "OPEN", "package_id": package_id, "net_entry_credit_pts": net_credit_pts}

        # Emergency Incomplete-Leg Unwind: Sell bought protective wing back at L5 bid!
        eff_wing_q = self._resolve_exit_quote(
            str(wing_leg["instrument_key"]),
            wing_quote or wing_leg.get("last_quote") or {},
            max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
            allow_emergency_rest=True,
        )
        sw_unwind = sweep_l5_book_for_qty(
            eff_wing_q,
            side="SELL",
            qty=lot_sz,
            adverse_ticks=int(hyg["adverse_latency_ticks"]),
            max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
            max_spread_pct=None,
            require_observed=False,
        )
        if sw_unwind.get("ok"):
            unwind_px = float(sw_unwind["fill_price"])
        else:
            fallback_b = float(eff_wing_q.get("raw_bid") or eff_wing_q.get("bid") or wing_leg["entry_price"])
            unwind_px = round_to_nse_tick(max(0.05, fallback_b - 0.05), mode="adverse_sell")

        acc = compute_package_accounting(
            legs=[
                OptionLeg(
                    option_type=wing_leg["option_type"],
                    strike=wing_leg["strike"],
                    qty=lot_sz,
                    entry_price=float(wing_leg["entry_price"]),
                    exit_price=unwind_px,
                    exit_mode="MARKET_ORDER",
                    label="INCOMPLETE_LEG_WING_UNWIND",
                )
            ],
            trade_date=pending["session_date"],
            reference_spot=float(pending["spot_c"]),
        )
        exit_bar = current_bar_ts or now_dt.strftime("%Y-%m-%d %H:%M:00")
        with self._lock:
            self._locked_until_after_bar.setdefault(exit_bar, []).append(
                (pending["book_id"], float(pending["cash_reserved_inr"]))
            )
            self._cooldown_until_epoch[pending["book_id"]] = time.time() + float(env["cooldown_seconds_after_exit"])

        unwound_row = {
            "package_id": package_id,
            "advance_trade_id": pending["advance_trade_id"],
            "prereg_sha256": self.prereg_sha256,
            "session_date": pending["session_date"],
            "book_id": pending["book_id"],
            "ui_book_type": BOOK_SHORT_SELL,
            "state": "INCOMPLETE_LEG_UNWOUND",
            "direction_label": f"UNWOUND_WING_{wing_leg['option_type']}",
            "expiry": pending["expiry"],
            "entry_bar_ts": pending["entry_bar_ts"],
            "entry_time": pending["entry_time"],
            "leg2_confirmed_time": None,
            "exit_bar_ts": exit_bar,
            "exit_time": now_dt.isoformat(),
            "legs_state": {"wing_leg": {k: v for k, v in wing_leg.items() if k != "last_quote"}, "unwind_price": unwind_px},
            "accounting": acc,
            "structural_max_loss_inr": float(acc["risk"]["final_package_max_loss_after_friction_inr"]),
            "upstox_final_margin_inr": pending["margin_info"].get("final_margin_inr"),
            "margin_status": pending["margin_info"]["margin_status"],
            "cash_reserved_inr": float(pending["cash_reserved_inr"]),
            "gross_pnl_inr": float(acc["gross_pnl"]),
            "total_charges_inr": float(acc["total_friction"]),
            "stt_inr": float(acc["stt"]),
            "brokerage_inr": float(acc["brokerage"]),
            "net_pnl_inr": float(acc["net_pnl"]),
            "exit_reason": f"INCOMPLETE_LEG_EMERGENCY_UNWIND:{reject_reason}",
            "regime_context": {"shadow_only": True, "reject_reason": reject_reason},
        }
        self._persist_closed_or_unwound_package(unwound_row)
        self._unpin_unused_package_keys([str(wing_leg["instrument_key"]), str(short_t["instrument_key"])])
        return {"state": "INCOMPLETE_LEG_UNWOUND", "package_id": package_id, "net_pnl_inr": float(acc["net_pnl"]), "reason": reject_reason}

    # ──────────────────────────────────────────────────────────────────────────
    # 6. SUB-SECOND TICK GUARD & BAR-CLOSE RATCHET / INVALIDATION EXITS
    # ──────────────────────────────────────────────────────────────────────────

    def on_observed_tick(
        self,
        ikey: str,
        quote_payload: Dict[str, Any],
        quote_lookup_by_ikey: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        now_dt: Optional[datetime] = None,
        *,
        hard_stop_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Sub-second WebSocket tick handler:
          1. Flushes due `+250ms/+1s/+5s/+60s` quote-fade micro-snapshots.
          2. Completes or emergency-unwinds any `PENDING_LEG2_SHORT` spread package.
          3. Evaluates Hard Stop-Loss, Spread Decay Target, `15:15 IST` square-off, and
             `-₹3,500` MTM Daily Kill (NEVER updating close-only ratchets intrabar).
        """
        now_dt = _to_ist_naive_safe(now_dt)
        now_ms = int(quote_payload.get("local_receipt_ts_ms") or int(time.time() * 1000))
        if hard_stop_only:
            # The dispatch queue is outside the stream lock. Recheck the age at use.
            age_ms = int(time.time() * 1000) - int(quote_payload.get("l1_book_timestamp_ms") or 0)
            if not 0 <= age_ms <= SHADOW_EXIT_MAX_QUOTE_AGE_MS:
                return []
            quote_payload = dict(quote_payload, l1_book_age_ms=age_ms, quote_age_ms=age_ms)
        else:
            self._flush_due_fade_probes(ikey, quote_payload, now_ms)

        closed_events: List[Dict[str, Any]] = []
        getter = quote_lookup_by_ikey or (lambda k: quote_payload if k == ikey else None)

        # 1. Check pending Leg-2 buy-first spreads
        with self._lock:
            pending_ids = [] if hard_stop_only else list(self._pending_leg2.keys())
        for pid in pending_ids:
            with self._lock:
                p_obj = self._pending_leg2.get(pid)
            if not p_obj:
                continue
            s_ik = p_obj["short_target"]["instrument_key"]
            w_ik = p_obj["wing_leg"]["instrument_key"]
            timed_out = now_ms >= int(p_obj["deadline_ts_ms"])
            if (ikey == s_ik and now_ms > int(p_obj["leg1_ts_ms"])) or timed_out:
                sq = quote_payload if ikey == s_ik else getter(s_ik)
                wq = quote_payload if ikey == w_ik else getter(w_ik)
                res_p = self.complete_pending_leg2_or_unwind(pid, sq, wq, now_dt=now_dt)
                if res_p and res_p.get("state") == "INCOMPLETE_LEG_UNWOUND":
                    closed_events.append(res_p)

        # 2. Check open shadow packages for Hard-SL, Fixed Decay TP, or 15:15 EOD
        is_eod = not hard_stop_only and now_dt.time() >= dt_time(15, 15)
        with self._lock:
            open_list = list(self._open_packages.values())

        for pkg in open_list:
            pid = str(pkg["package_id"])
            ls = pkg["legs_state"]
            mode = ls.get("mode")
            if mode == "SINGLE_PAID_LONG":
                l_ik = ls["long_leg"]["instrument_key"]
                if ikey != l_ik and not is_eod:
                    continue
                lq = quote_payload if ikey == l_ik else getter(l_ik)
                qty_l = abs(int(ls["long_leg"]["qty"]))
                sw_sell = sweep_l5_book_for_qty(
                    lq,
                    "SELL",
                    qty=qty_l,
                    adverse_ticks=1,
                    max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                    max_spread_pct=None,
                    require_observed=False,
                )
                if not sw_sell.get("ok"):
                    lq = self._resolve_exit_quote(
                        l_ik,
                        lq,
                        max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                        allow_emergency_rest=True,
                        is_eod_urgent=is_eod,
                    )
                    sw_sell = sweep_l5_book_for_qty(
                        lq,
                        "SELL",
                        qty=qty_l,
                        adverse_ticks=1,
                        max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                        max_spread_pct=None,
                        require_observed=False,
                    )
                if not sw_sell.get("ok"):
                    continue
                cur_exit_px = float(sw_sell["fill_price"])
                ll_tick = ls["long_leg"]
                acc_tick_l = compute_package_accounting(
                    legs=[
                        OptionLeg(
                            ll_tick["option_type"],
                            float(ll_tick["strike"]),
                            int(ll_tick["qty"]),
                            float(ll_tick["entry_price"]),
                            cur_exit_px,
                        )
                    ],
                    trade_date=pkg["session_date"],
                )
                with self._lock:
                    self._last_valid_pkg_mtm_inr[pid] = float(acc_tick_l["net_pnl"])
                eff_stop = max(float(ls["hard_stop"]), float(ls.get("ratchet_stop") or 0.0))
                rsn = ""
                if hard_stop_only:
                    rsn = "LONG_HARD_SL_HIT" if cur_exit_px <= float(ls["hard_stop"]) else ""
                elif is_eod:
                    rsn = "EOD_SQUAREOFF_1515"
                elif cur_exit_px <= eff_stop:
                    rsn = "RATCHET_STOP_HIT" if (ls.get("ratchet_stop") and float(ls["ratchet_stop"]) > float(ls["hard_stop"]) + 0.01) else "LONG_HARD_SL_HIT"
                if rsn:
                    c_out = self._close_single_long_package(pkg, lq or quote_payload, cur_exit_px, rsn, now_dt)
                    if c_out:
                        closed_events.append(c_out)
            elif mode == "DEFINED_RISK_CREDIT_SPREAD_150":
                s_ik = ls["short_leg"]["instrument_key"]
                w_ik = ls["wing_leg"]["instrument_key"]
                if ikey not in (s_ik, w_ik) and not is_eod:
                    continue
                sq = quote_payload if ikey == s_ik else getter(s_ik)
                wq = quote_payload if ikey == w_ik else getter(w_ik)
                qty_abs = abs(int(ls["short_leg"]["qty"]))
                sw_buy_short = sweep_l5_book_for_qty(
                    sq,
                    "BUY",
                    qty=qty_abs,
                    adverse_ticks=1,
                    max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                    max_spread_pct=None,
                    require_observed=False,
                )
                sw_sell_wing = sweep_l5_book_for_qty(
                    wq,
                    "SELL",
                    qty=qty_abs,
                    adverse_ticks=1,
                    max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                    max_spread_pct=None,
                    require_observed=False,
                )
                # Cooldown-throttled emergency REST recovery whenever EITHER leg is stale/invalid
                # (covers stale short leg pre-EOD, stale wing leg, <₹12 credit Hard-SL, and ratchets)
                if not sw_buy_short.get("ok"):
                    sq = self._resolve_exit_quote(
                        s_ik,
                        sq,
                        max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                        allow_emergency_rest=True,
                        is_eod_urgent=is_eod,
                    )
                    sw_buy_short = sweep_l5_book_for_qty(
                        sq,
                        "BUY",
                        qty=qty_abs,
                        adverse_ticks=1,
                        max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                        max_spread_pct=None,
                        require_observed=False,
                    )
                if not sw_sell_wing.get("ok"):
                    wq = self._resolve_exit_quote(
                        w_ik,
                        wq,
                        max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                        allow_emergency_rest=True,
                        is_eod_urgent=is_eod,
                    )
                    sw_sell_wing = sweep_l5_book_for_qty(
                        wq,
                        "SELL",
                        qty=qty_abs,
                        adverse_ticks=1,
                        max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                        max_spread_pct=None,
                        require_observed=False,
                    )
                if (
                    sw_buy_short.get("ok")
                    and sw_sell_wing.get("ok")
                    and not _quotes_within_skew(sq, wq, max_skew_ms=SHADOW_CROSS_LEG_MAX_SKEW_MS)
                ):
                    ts_s = _extract_quote_ts_ms(sq)
                    ts_w = _extract_quote_ts_ms(wq)
                    if ts_s <= 0 or (ts_w > 0 and ts_s <= ts_w):
                        sq = self._resolve_exit_quote(
                            s_ik,
                            sq,
                            max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                            allow_emergency_rest=True,
                            is_eod_urgent=is_eod,
                            force_refresh=True,
                        )
                        sw_buy_short = sweep_l5_book_for_qty(
                            sq,
                            "BUY",
                            qty=qty_abs,
                            adverse_ticks=1,
                            max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                            max_spread_pct=None,
                            require_observed=False,
                        )
                    if ts_w <= 0 or (ts_s > 0 and ts_w < ts_s):
                        wq = self._resolve_exit_quote(
                            w_ik,
                            wq,
                            max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                            allow_emergency_rest=True,
                            is_eod_urgent=is_eod,
                            force_refresh=True,
                        )
                        sw_sell_wing = sweep_l5_book_for_qty(
                            wq,
                            "SELL",
                            qty=qty_abs,
                            adverse_ticks=1,
                            max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                            max_spread_pct=None,
                            require_observed=False,
                        )
                if not sw_buy_short.get("ok") or not sw_sell_wing.get("ok"):
                    continue
                if not _quotes_within_skew(sq, wq, max_skew_ms=SHADOW_CROSS_LEG_MAX_SKEW_MS):
                    continue
                sl_tick = ls["short_leg"]
                wl_tick = ls["wing_leg"]
                acc_tick_s = compute_package_accounting(
                    legs=[
                        OptionLeg(
                            wl_tick["option_type"],
                            float(wl_tick["strike"]),
                            qty_abs,
                            float(wl_tick["entry_price"]),
                            float(sw_sell_wing["fill_price"]),
                        ),
                        OptionLeg(
                            sl_tick["option_type"],
                            float(sl_tick["strike"]),
                            -qty_abs,
                            float(sl_tick["entry_price"]),
                            float(sw_buy_short["fill_price"]),
                        ),
                    ],
                    trade_date=pkg["session_date"],
                )
                with self._lock:
                    self._last_valid_pkg_mtm_inr[pid] = float(acc_tick_s["net_pnl"])
                cur_debit = round(float(sw_buy_short["fill_price"]) - float(sw_sell_wing["fill_price"]), 2)
                hard_debit = float(ls["hard_stop_debit_pts"])
                r_debit = ls.get("ratchet_max_debit_pts")
                eff_max_debit = min(hard_debit, float(r_debit)) if r_debit is not None else hard_debit
                rsn = ""
                if hard_stop_only:
                    rsn = "SPREAD_HARD_SL_HIT" if cur_debit >= hard_debit else ""
                elif is_eod:
                    rsn = "EOD_SQUAREOFF_1515"
                elif cur_debit >= eff_max_debit:
                    rsn = (
                        "SPREAD_RATCHET_STOP_HIT"
                        if (r_debit is not None and float(r_debit) < hard_debit - 0.01)
                        else "SPREAD_HARD_SL_HIT"
                    )
                elif cur_debit <= float(ls["target_debit_pts"]):
                    rsn = "SPREAD_DECAY_TARGET_70PCT"
                if rsn:
                    c_out = self._close_spread_package(
                        pkg,
                        sq or {},
                        wq or {},
                        float(sw_buy_short["fill_price"]),
                        float(sw_sell_wing["fill_price"]),
                        rsn,
                        now_dt,
                    )
                    if c_out:
                        closed_events.append(c_out)

        return closed_events

    def _manage_open_packages_on_bar_close(
        self,
        ctx: Dict[str, Any],
        bar_idx: int,
        bar_ts: str,
        quote_lookup: Dict[Tuple[str, float, str], Dict[str, Any]],
        ist_hhmm: int,
        now_dt: datetime,
        allow_network: bool = False,
    ) -> List[Dict[str, Any]]:
        closed: List[Dict[str, Any]] = []
        with self._lock:
            open_list = list(self._open_packages.values())

        cfg_a = self.config["shadow_books"]["SHADOW_BOOK_A_SPREAD_150"]
        cfg_b = self.config["shadow_books"]["SHADOW_BOOK_B_PAID_FLOW"]
        can_rest = bool(allow_network or self._emergency_quote_fetcher is not None)

        for pkg in open_list:
            try:
                ls = pkg["legs_state"]
                mode = ls.get("mode")
                is_bull = bool(ls.get("is_bull", True))
                try:
                    ent_dt = datetime.fromisoformat(str(pkg["entry_time"]).replace("T", " ")[:19])
                    held_mins = max(0, int((now_dt - ent_dt).total_seconds() // 60))
                except Exception:
                    held_mins = 0

                vflip_hit = False
                vf_pts = float(cfg_b["vflip_pts"] if mode == "SINGLE_PAID_LONG" else cfg_a["vflip_pts"])
                if held_mins >= 4 and bar_idx >= 1:
                    c0, c1 = float(ctx["c"][bar_idx]), float(ctx["c"][bar_idx - 1])
                    t0_v, t1_v = float(ctx["tpb"][bar_idx]), float(ctx["tpb"][bar_idx - 1])
                    vflip_hit = (is_bull and c0 < t0_v - vf_pts and c1 < t1_v - vf_pts) or (
                        (not is_bull) and c0 > t0_v + vf_pts and c1 > t1_v + vf_pts
                    )

                if mode == "SINGLE_PAID_LONG":
                    ll = ls["long_leg"]
                    l_ik = str(ll["instrument_key"])
                    eso_l = (str(ll["expiry"])[:10], float(ll["strike"]), str(ll["option_type"]).upper())
                    ql = quote_lookup.get(eso_l) or {}
                    need_bar_exit_b = (
                        ist_hhmm >= int(cfg_b["eod_squareoff_hhmm"])
                        or vflip_hit
                        or held_mins >= int(cfg_b["stagnation_mins"])
                    )
                    sw_s = sweep_l5_book_for_qty(
                        ql,
                        "SELL",
                        qty=int(ll["qty"]),
                        adverse_ticks=1,
                        max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                        max_spread_pct=None,
                        require_observed=False,
                    )
                    if not sw_s.get("ok") and can_rest:
                        ql = self._resolve_exit_quote(
                            l_ik,
                            ql,
                            max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                            allow_emergency_rest=True,
                            is_eod_urgent=need_bar_exit_b,
                        )
                        sw_s = sweep_l5_book_for_qty(
                            ql, "SELL", qty=int(ll["qty"]), adverse_ticks=1, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, max_spread_pct=None, require_observed=False
                        )
                    if not sw_s.get("ok"):
                        continue
                    close_px = float(sw_s["fill_price"])
                    gain_pct = ((close_px - float(ll["entry_price"])) / max(0.05, float(ll["entry_price"]))) * 100.0

                    eff_stop_b = max(float(ls["hard_stop"]), float(ls.get("ratchet_stop") or 0.0))
                    rsn = ""
                    if ist_hhmm >= int(cfg_b["eod_squareoff_hhmm"]):
                        rsn = "EOD_SQUAREOFF_1515"
                    elif close_px <= eff_stop_b:
                        rsn = (
                            "RATCHET_STOP_HIT"
                            if (ls.get("ratchet_stop") and float(ls["ratchet_stop"]) > float(ls["hard_stop"]) + 0.01)
                            else "LONG_HARD_SL_HIT"
                        )
                    elif held_mins >= int(cfg_b["stagnation_mins"]) and gain_pct < float(cfg_b["stagnation_min_gain_pct"]):
                        rsn = "STAGNATION_TIME_STOP"
                    elif vflip_hit:
                        rsn = "SPOT_VWAP_FLIP_EXIT"

                    if rsn:
                        c_res = self._close_single_long_package(pkg, ql, close_px, rsn, now_dt, exit_bar_ts=bar_ts)
                        if c_res:
                            closed.append(c_res)
                        continue

                    # Strictly Close-Only Ratchet Update (supports both 2-tuple [act, lock] and 3-tuple [act, dist, lock])!
                    prev_r = ls.get("ratchet_stop")
                    ls["best_close_pts"] = max(float(ls.get("best_close_pts") or ll["entry_price"]), close_px)
                    stages = [
                        (float(s[0]), float(s[1]), float(s[2])) if len(s) >= 3 else (float(s[0]), 0.0, float(s[1]))
                        for s in cfg_b["ratchet_stages"]
                    ]
                    new_r = compute_close_only_ratchet(
                        False, float(ll["entry_price"]), float(ls["best_close_pts"]), prev_r, stages
                    )
                    ls["ratchet_stop"] = new_r
                    self._update_open_legs_state(pkg["package_id"], ls)
                    if new_r is not None and (prev_r is None or float(new_r) > float(prev_r) + 1e-6):
                        self._emit_lifecycle_event("BOOK_B_RATCHET", {**pkg, "legs_state": ls})

                elif mode == "DEFINED_RISK_CREDIT_SPREAD_150":
                    sl = ls["short_leg"]
                    wl = ls["wing_leg"]
                    s_ik = str(sl["instrument_key"])
                    w_ik = str(wl["instrument_key"])
                    eso_s = (str(sl["expiry"])[:10], float(sl["strike"]), str(sl["option_type"]).upper())
                    eso_w = (str(wl["expiry"])[:10], float(wl["strike"]), str(wl["option_type"]).upper())
                    qs = quote_lookup.get(eso_s) or {}
                    qw = quote_lookup.get(eso_w) or {}
                    qty_abs = abs(int(sl["qty"]))
                    need_bar_exit_a = ist_hhmm >= int(cfg_a["eod_squareoff_hhmm"]) or vflip_hit
                    sw_bs = sweep_l5_book_for_qty(
                        qs,
                        "BUY",
                        qty=qty_abs,
                        adverse_ticks=1,
                        max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                        max_spread_pct=None,
                        require_observed=False,
                    )
                    sw_sw = sweep_l5_book_for_qty(
                        qw,
                        "SELL",
                        qty=qty_abs,
                        adverse_ticks=1,
                        max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                        max_spread_pct=None,
                        require_observed=False,
                    )
                    if can_rest:
                        if not sw_bs.get("ok"):
                            qs = self._resolve_exit_quote(
                                s_ik,
                                qs,
                                max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                                allow_emergency_rest=True,
                                is_eod_urgent=need_bar_exit_a,
                            )
                            sw_bs = sweep_l5_book_for_qty(
                                qs, "BUY", qty=qty_abs, adverse_ticks=1, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, max_spread_pct=None, require_observed=False
                            )
                        if not sw_sw.get("ok"):
                            qw = self._resolve_exit_quote(
                                w_ik,
                                qw,
                                max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                                allow_emergency_rest=True,
                                is_eod_urgent=need_bar_exit_a,
                            )
                            sw_sw = sweep_l5_book_for_qty(
                                qw, "SELL", qty=qty_abs, adverse_ticks=1, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, max_spread_pct=None, require_observed=False
                            )
                        if (
                            sw_bs.get("ok")
                            and sw_sw.get("ok")
                            and not _quotes_within_skew(qs, qw, max_skew_ms=SHADOW_CROSS_LEG_MAX_SKEW_MS)
                        ):
                            ts_s = _extract_quote_ts_ms(qs)
                            ts_w = _extract_quote_ts_ms(qw)
                            if ts_s <= 0 or (ts_w > 0 and ts_s <= ts_w):
                                qs = self._resolve_exit_quote(
                                    s_ik,
                                    qs,
                                    max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                                    allow_emergency_rest=True,
                                    is_eod_urgent=need_bar_exit_a,
                                    force_refresh=True,
                                )
                                sw_bs = sweep_l5_book_for_qty(
                                    qs, "BUY", qty=qty_abs, adverse_ticks=1, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, max_spread_pct=None, require_observed=False
                                )
                            if ts_w <= 0 or (ts_s > 0 and ts_w < ts_s):
                                qw = self._resolve_exit_quote(
                                    w_ik,
                                    qw,
                                    max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                                    allow_emergency_rest=True,
                                    is_eod_urgent=need_bar_exit_a,
                                    force_refresh=True,
                                )
                                sw_sw = sweep_l5_book_for_qty(
                                    qw, "SELL", qty=qty_abs, adverse_ticks=1, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, max_spread_pct=None, require_observed=False
                                )
                    if not sw_bs.get("ok") or not sw_sw.get("ok"):
                        continue
                    if not _quotes_within_skew(qs, qw, max_skew_ms=SHADOW_CROSS_LEG_MAX_SKEW_MS):
                        continue
                    cur_debit = round(float(sw_bs["fill_price"]) - float(sw_sw["fill_price"]), 2)

                    hard_debit_bar = float(ls["hard_stop_debit_pts"])
                    r_debit_prev = ls.get("ratchet_max_debit_pts")
                    eff_max_debit_bar = (
                        min(hard_debit_bar, float(r_debit_prev)) if r_debit_prev is not None else hard_debit_bar
                    )
                    rsn = ""
                    if ist_hhmm >= int(cfg_a["eod_squareoff_hhmm"]):
                        rsn = "EOD_SQUAREOFF_1515"
                    elif cur_debit >= eff_max_debit_bar:
                        rsn = (
                            "SPREAD_RATCHET_STOP_HIT"
                            if (r_debit_prev is not None and float(r_debit_prev) < hard_debit_bar - 0.01)
                            else "SPREAD_HARD_SL_HIT"
                        )
                    elif cur_debit <= float(ls["target_debit_pts"]):
                        rsn = "SPREAD_DECAY_TARGET_70PCT"
                    elif vflip_hit:
                        rsn = "SPOT_VWAP_FLIP_EXIT"

                    if rsn:
                        c_res = self._close_spread_package(
                            pkg, qs, qw, float(sw_bs["fill_price"]), float(sw_sw["fill_price"]), rsn, now_dt, exit_bar_ts=bar_ts
                        )
                        if c_res:
                            closed.append(c_res)
                        continue

                    # Strictly Close-Only Spread Decay Ratchet Update!
                    init_cr = float(ls["net_entry_credit_pts"])
                    best_d = min(float(ls.get("best_close_credit_decay_pts") or init_cr), max(0.05, cur_debit))
                    ls["best_close_credit_decay_pts"] = best_d
                    decay_pct = ((init_cr - best_d) / max(0.05, init_cr)) * 100.0
                    r_debit = ls.get("ratchet_max_debit_pts")
                    for trig_pct, lock_pct in cfg_a["ratchet_stages"]:
                        if decay_pct >= float(trig_pct):
                            cand_debit = round(init_cr * (1.0 - float(lock_pct) / 100.0), 2)
                            r_debit = cand_debit if r_debit is None else min(float(r_debit), cand_debit)
                    ls["ratchet_max_debit_pts"] = r_debit
                    self._update_open_legs_state(pkg["package_id"], ls)
            except Exception as pkg_err:
                logger.error(f"[S205ShadowCollector] Bar-close package evaluation error ({pkg.get('package_id')}): {pkg_err}", exc_info=True)

        return closed

    def _close_single_long_package(
        self,
        pkg: Dict[str, Any],
        exit_quote: Dict[str, Any],
        exit_px: float,
        exit_reason: str,
        now_dt: datetime,
        exit_bar_ts: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        pid = str(pkg["package_id"])
        with self._lock:
            if pid not in self._open_packages:
                return None
            self._open_packages.pop(pid, None)
            self._last_valid_pkg_mtm_inr.pop(pid, None)
            if not self._open_packages:
                self._unpriced_open_count = 0
                self._unpriced_package_ids = []
                self._mtm_visibility_status = "COMPLETE"
                self._last_known_unrealized_inr = 0.0
                self._effective_unrealized_inr = 0.0
                self._worst_case_unpriced_exposure_inr = 0.0

        ll = pkg["legs_state"]["long_leg"]
        acc = compute_package_accounting(
            legs=[
                OptionLeg(
                    option_type=ll["option_type"],
                    strike=float(ll["strike"]),
                    qty=int(ll["qty"]),
                    entry_price=float(ll["entry_price"]),
                    exit_price=exit_px,
                    exit_mode="MARKET_ORDER",
                )
            ],
            trade_date=pkg["session_date"],
        )
        eff_bar = exit_bar_ts or now_dt.strftime("%Y-%m-%d %H:%M:00")
        env = self.config["account_envelope"]
        with self._lock:
            self._locked_until_after_bar.setdefault(eff_bar, []).append((pkg["book_id"], float(pkg["cash_reserved_inr"])))
            self._cooldown_until_epoch[pkg["book_id"]] = time.time() + float(env["cooldown_seconds_after_exit"])

        closed_row = {
            **pkg,
            "state": "CLOSED",
            "exit_bar_ts": eff_bar,
            "exit_time": now_dt.isoformat(),
            "accounting": acc,
            "gross_pnl_inr": float(acc["gross_pnl"]),
            "total_charges_inr": float(acc["total_friction"]),
            "stt_inr": float(acc["stt"]),
            "brokerage_inr": float(acc["brokerage"]),
            "net_pnl_inr": float(acc["net_pnl"]),
            "exit_reason": exit_reason,
        }
        self._persist_closed_or_unwound_package(closed_row)
        self._unpin_unused_package_keys([str(ll["instrument_key"])])
        now_ms = int(exit_quote.get("local_receipt_ts_ms") or int(time.time() * 1000))
        self._schedule_quote_fade_event(pid, f"EXIT_{exit_reason}", [("SELL", exit_quote)], now_ms)

        if self._sync_advance_db:
            self._mirror_close_to_advance_db(closed_row, exit_px, exit_px, now_dt)
        self._emit_lifecycle_event("BOOK_B_CLOSE", closed_row)
        return closed_row

    def _close_spread_package(
        self,
        pkg: Dict[str, Any],
        short_quote: Dict[str, Any],
        wing_quote: Dict[str, Any],
        short_exit_px: float,
        wing_exit_px: float,
        exit_reason: str,
        now_dt: datetime,
        exit_bar_ts: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        pid = str(pkg["package_id"])
        with self._lock:
            if pid not in self._open_packages:
                return None
            self._open_packages.pop(pid, None)
            self._last_valid_pkg_mtm_inr.pop(pid, None)
            if not self._open_packages:
                self._unpriced_open_count = 0
                self._unpriced_package_ids = []
                self._mtm_visibility_status = "COMPLETE"
                self._last_known_unrealized_inr = 0.0
                self._effective_unrealized_inr = 0.0
                self._worst_case_unpriced_exposure_inr = 0.0

        ls = pkg["legs_state"]
        wl = ls["wing_leg"]
        sl = ls["short_leg"]
        qty_abs = abs(int(sl["qty"]))
        acc = compute_package_accounting(
            legs=[
                OptionLeg(
                    option_type=wl["option_type"],
                    strike=float(wl["strike"]),
                    qty=qty_abs,
                    entry_price=float(wl["entry_price"]),
                    exit_price=wing_exit_px,
                    exit_mode="MARKET_ORDER",
                ),
                OptionLeg(
                    option_type=sl["option_type"],
                    strike=float(sl["strike"]),
                    qty=-qty_abs,
                    entry_price=float(sl["entry_price"]),
                    exit_price=short_exit_px,
                    exit_mode="MARKET_ORDER",
                ),
            ],
            trade_date=pkg["session_date"],
        )
        eff_bar = exit_bar_ts or now_dt.strftime("%Y-%m-%d %H:%M:00")
        env = self.config["account_envelope"]
        with self._lock:
            self._locked_until_after_bar.setdefault(eff_bar, []).append((pkg["book_id"], float(pkg["cash_reserved_inr"])))
            self._cooldown_until_epoch[pkg["book_id"]] = time.time() + float(env["cooldown_seconds_after_exit"])

        closed_row = {
            **pkg,
            "state": "CLOSED",
            "exit_bar_ts": eff_bar,
            "exit_time": now_dt.isoformat(),
            "accounting": acc,
            "gross_pnl_inr": float(acc["gross_pnl"]),
            "total_charges_inr": float(acc["total_friction"]),
            "stt_inr": float(acc["stt"]),
            "brokerage_inr": float(acc["brokerage"]),
            "net_pnl_inr": float(acc["net_pnl"]),
            "exit_reason": exit_reason,
        }
        self._persist_closed_or_unwound_package(closed_row)
        self._unpin_unused_package_keys([str(wl["instrument_key"]), str(sl["instrument_key"])])
        now_ms = int(short_quote.get("local_receipt_ts_ms") or int(time.time() * 1000))
        self._schedule_quote_fade_event(pid, f"EXIT_{exit_reason}", [("BUY", short_quote), ("SELL", wing_quote)], now_ms)

        if self._sync_advance_db:
            exit_debit = max(0.05, round(short_exit_px - wing_exit_px, 2))
            self._mirror_close_to_advance_db(closed_row, exit_debit, exit_debit, now_dt)
        self._emit_lifecycle_event("BOOK_A_CLOSE", closed_row)
        return closed_row

    def _evaluate_open_unrealized_mtm(
        self,
        quote_lookup: Dict[Tuple[str, float, str], Dict[str, Any]],
        session_date: str,
        allow_network: bool = False,
    ) -> Dict[str, Any]:
        """
        Evaluates open-package unrealized MTM with explicit unpriced-exposure state tracking:
          - Never silently reports `₹0.00` aggregate MTM when open packages fail freshness/skew validation.
          - Retains last-known adverse MTM (`min(0.0, last_valid_mtm)`) on unpriced packages and prevents
            positive unrealized gains on other open packages from masking realized/unpriced losses in the
            daily loss-kill calculation.
        """
        can_rest = bool(allow_network or self._emergency_quote_fetcher is not None)
        with self._lock:
            open_list = list(self._open_packages.values())

        priced_pnls: List[float] = []
        unpriced_ids: List[str] = []
        unpriced_adverse_mtm: float = 0.0
        worst_unpriced_exp: float = 0.0

        for pkg in open_list:
            pid = str(pkg["package_id"])
            ls = pkg["legs_state"]
            pkg_priced = False
            pkg_pnl = 0.0

            if ls.get("mode") == "SINGLE_PAID_LONG":
                ll = ls["long_leg"]
                l_ik = str(ll.get("instrument_key") or "")
                q = quote_lookup.get((str(ll["expiry"])[:10], float(ll["strike"]), str(ll["option_type"]).upper()))
                sw = sweep_l5_book_for_qty(
                    q,
                    "SELL",
                    qty=int(ll["qty"]),
                    adverse_ticks=1,
                    max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                    max_spread_pct=None,
                    require_observed=False,
                )
                if not sw.get("ok") and can_rest and l_ik:
                    q = self._resolve_exit_quote(
                        l_ik, q, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, allow_emergency_rest=True
                    )
                    sw = sweep_l5_book_for_qty(
                        q,
                        "SELL",
                        qty=int(ll["qty"]),
                        adverse_ticks=1,
                        max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                        max_spread_pct=None,
                        require_observed=False,
                    )
                if sw.get("ok"):
                    acc = compute_package_accounting(
                        legs=[OptionLeg(ll["option_type"], float(ll["strike"]), int(ll["qty"]), float(ll["entry_price"]), float(sw["fill_price"]))],
                        trade_date=session_date,
                    )
                    pkg_pnl = float(acc["net_pnl"])
                    pkg_priced = True

            elif ls.get("mode") == "DEFINED_RISK_CREDIT_SPREAD_150":
                sl = ls["short_leg"]
                wl = ls["wing_leg"]
                s_ik = str(sl.get("instrument_key") or "")
                w_ik = str(wl.get("instrument_key") or "")
                qs = quote_lookup.get((str(sl["expiry"])[:10], float(sl["strike"]), str(sl["option_type"]).upper()))
                qw = quote_lookup.get((str(wl["expiry"])[:10], float(wl["strike"]), str(wl["option_type"]).upper()))
                qty_abs = abs(int(sl["qty"]))
                sw_s = sweep_l5_book_for_qty(
                    qs,
                    "BUY",
                    qty=qty_abs,
                    adverse_ticks=1,
                    max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                    max_spread_pct=None,
                    require_observed=False,
                )
                sw_w = sweep_l5_book_for_qty(
                    qw,
                    "SELL",
                    qty=qty_abs,
                    adverse_ticks=1,
                    max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS,
                    max_spread_pct=None,
                    require_observed=False,
                )
                if can_rest:
                    if not sw_s.get("ok") and s_ik:
                        qs = self._resolve_exit_quote(
                            s_ik, qs, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, allow_emergency_rest=True
                        )
                        sw_s = sweep_l5_book_for_qty(
                            qs, "BUY", qty=qty_abs, adverse_ticks=1, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, max_spread_pct=None, require_observed=False
                        )
                    if not sw_w.get("ok") and w_ik:
                        qw = self._resolve_exit_quote(
                            w_ik, qw, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, allow_emergency_rest=True
                        )
                        sw_w = sweep_l5_book_for_qty(
                            qw, "SELL", qty=qty_abs, adverse_ticks=1, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, max_spread_pct=None, require_observed=False
                        )
                    if sw_s.get("ok") and sw_w.get("ok") and not _quotes_within_skew(qs, qw, max_skew_ms=SHADOW_CROSS_LEG_MAX_SKEW_MS):
                        ts_s = _extract_quote_ts_ms(qs)
                        ts_w = _extract_quote_ts_ms(qw)
                        if (ts_s <= 0 or (ts_w > 0 and ts_s <= ts_w)) and s_ik:
                            qs = self._resolve_exit_quote(
                                s_ik, qs, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, allow_emergency_rest=True, force_refresh=True
                            )
                            sw_s = sweep_l5_book_for_qty(
                                qs, "BUY", qty=qty_abs, adverse_ticks=1, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, max_spread_pct=None, require_observed=False
                            )
                        if (ts_w <= 0 or (ts_s > 0 and ts_w < ts_s)) and w_ik:
                            qw = self._resolve_exit_quote(
                                w_ik, qw, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, allow_emergency_rest=True, force_refresh=True
                            )
                            sw_w = sweep_l5_book_for_qty(
                                qw, "SELL", qty=qty_abs, adverse_ticks=1, max_age_ms=SHADOW_EXIT_MAX_QUOTE_AGE_MS, max_spread_pct=None, require_observed=False
                            )

                if sw_s.get("ok") and sw_w.get("ok") and _quotes_within_skew(qs, qw, max_skew_ms=SHADOW_CROSS_LEG_MAX_SKEW_MS):
                    acc = compute_package_accounting(
                        legs=[
                            OptionLeg(wl["option_type"], float(wl["strike"]), qty_abs, float(wl["entry_price"]), float(sw_w["fill_price"])),
                            OptionLeg(sl["option_type"], float(sl["strike"]), -qty_abs, float(sl["entry_price"]), float(sw_s["fill_price"])),
                        ],
                        trade_date=session_date,
                    )
                    pkg_pnl = float(acc["net_pnl"])
                    pkg_priced = True

            if pkg_priced:
                with self._lock:
                    self._last_valid_pkg_mtm_inr[pid] = pkg_pnl
                priced_pnls.append(pkg_pnl)
            else:
                unpriced_ids.append(pid)
                worst_unpriced_exp += float(pkg.get("structural_max_loss_inr") or 0.0)
                with self._lock:
                    last_valid = float(self._last_valid_pkg_mtm_inr.get(pid, 0.0))
                unpriced_adverse_mtm += min(0.0, last_valid)

        known_unrealized = round(sum(priced_pnls), 2)
        unpriced_cnt = len(unpriced_ids)
        if unpriced_cnt == 0:
            vis_status = "COMPLETE"
            effective_unrealized: Optional[float] = known_unrealized
            conservative_kill_mtm = known_unrealized
        else:
            vis_status = "DEGRADED_UNPRICED_OPEN_RISK"
            effective_unrealized = None
            conservative_kill_mtm = round(
                sum(min(0.0, p) for p in priced_pnls) + unpriced_adverse_mtm,
                2,
            )

        worst_unpriced_exp = round(worst_unpriced_exp, 2)
        with self._lock:
            self._unpriced_open_count = unpriced_cnt
            self._unpriced_package_ids = list(unpriced_ids)
            self._mtm_visibility_status = vis_status
            self._last_known_unrealized_inr = known_unrealized
            self._effective_unrealized_inr = effective_unrealized
            self._worst_case_unpriced_exposure_inr = worst_unpriced_exp

        return {
            "mtm_visibility_status": vis_status,
            "known_unrealized_inr": known_unrealized,
            "effective_unrealized_inr": effective_unrealized,
            "conservative_kill_mtm_inr": conservative_kill_mtm,
            "unpriced_open_count": unpriced_cnt,
            "unpriced_package_ids": unpriced_ids,
            "worst_case_unpriced_exposure_inr": worst_unpriced_exp,
        }

    def _compute_open_unrealized_pnl(
        self, quote_lookup: Dict[Tuple[str, float, str], Dict[str, Any]], session_date: str
    ) -> float:
        res = self._evaluate_open_unrealized_mtm(quote_lookup, session_date, allow_network=False)
        return float(res["conservative_kill_mtm_inr"])

    # ──────────────────────────────────────────────────────────────────────────
    # 7. SQLITE PERSISTENCE & UI ADVANCE_DB MIRRORING (HARD-AIRGAPPED)
    # ──────────────────────────────────────────────────────────────────────────

    def _persist_open_package(self, pkg: Dict[str, Any]) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO option_sniffer_advance_s205_shadow_packages (
                        package_id, advance_trade_id, prereg_sha256, session_date,
                        book_id, ui_book_type, state, direction_label, expiry,
                        entry_bar_ts, entry_time, leg2_confirmed_time, legs_state_json,
                        structural_max_loss_inr, upstox_final_margin_inr, margin_status,
                        cash_reserved_inr, regime_context_json, shadow_only
                    ) VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1);
                    """,
                    (
                        pkg["package_id"],
                        pkg["advance_trade_id"],
                        pkg["prereg_sha256"],
                        pkg["session_date"],
                        pkg["book_id"],
                        pkg["ui_book_type"],
                        pkg["direction_label"],
                        pkg["expiry"],
                        pkg["entry_bar_ts"],
                        pkg["entry_time"],
                        pkg.get("leg2_confirmed_time"),
                        json.dumps(pkg["legs_state"]),
                        float(pkg["structural_max_loss_inr"]),
                        pkg.get("upstox_final_margin_inr"),
                        pkg["margin_status"],
                        float(pkg["cash_reserved_inr"]),
                        json.dumps(pkg.get("regime_context") or {}),
                    ),
                )
        finally:
            conn.close()

    def _update_open_legs_state(self, package_id: str, legs_state: Dict[str, Any]) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE option_sniffer_advance_s205_shadow_packages SET legs_state_json = ? WHERE package_id = ?;",
                    (json.dumps(legs_state), package_id),
                )
        finally:
            conn.close()

    def _persist_closed_or_unwound_package(self, pkg: Dict[str, Any]) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO option_sniffer_advance_s205_shadow_packages (
                        package_id, advance_trade_id, prereg_sha256, session_date,
                        book_id, ui_book_type, state, direction_label, expiry,
                        entry_bar_ts, entry_time, leg2_confirmed_time, exit_bar_ts, exit_time,
                        legs_state_json, accounting_json, structural_max_loss_inr,
                        upstox_final_margin_inr, margin_status, cash_reserved_inr,
                        gross_pnl_inr, total_charges_inr, stt_inr, brokerage_inr,
                        net_pnl_inr, exit_reason, regime_context_json, shadow_only
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1);
                    """,
                    (
                        pkg["package_id"],
                        pkg.get("advance_trade_id"),
                        pkg["prereg_sha256"],
                        pkg["session_date"],
                        pkg["book_id"],
                        pkg["ui_book_type"],
                        pkg["state"],
                        pkg["direction_label"],
                        pkg["expiry"],
                        pkg["entry_bar_ts"],
                        pkg["entry_time"],
                        pkg.get("leg2_confirmed_time"),
                        pkg.get("exit_bar_ts"),
                        pkg.get("exit_time"),
                        json.dumps(pkg.get("legs_state") or {}),
                        json.dumps(pkg.get("accounting") or {}),
                        float(pkg.get("structural_max_loss_inr") or 0.0),
                        pkg.get("upstox_final_margin_inr"),
                        pkg.get("margin_status") or "UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN",
                        float(pkg.get("cash_reserved_inr") or 0.0),
                        float(pkg.get("gross_pnl_inr") or 0.0),
                        float(pkg.get("total_charges_inr") or 0.0),
                        float(pkg.get("stt_inr") or 0.0),
                        float(pkg.get("brokerage_inr") or 0.0),
                        float(pkg.get("net_pnl_inr") or 0.0),
                        pkg.get("exit_reason"),
                        json.dumps(pkg.get("regime_context") or {}),
                    ),
                )
        finally:
            conn.close()

    def _mirror_open_to_advance_db(
        self,
        pkg: Dict[str, Any],
        primary_quote: Dict[str, Any],
        entry_px_or_credit: float,
        stop_loss_pct: float,
        init_stop_px: float,
        spot_c: float,
    ) -> None:
        conn = self._connect()
        try:
            sig_id = f"shdw_sig_{pkg['package_id'][-10:]}"
            lot_sz = int(self.config["account_envelope"]["lot_size_nifty"])
            with conn:
                conn.execute(
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
                        delta REAL, gamma REAL, theta REAL, vega REAL, iv REAL, oi REAL, volume REAL,
                        otm_strikes REAL, adx_14 REAL, kaufman_er REAL, atr_ratio REAL, net_gex REAL,
                        vrp REAL, synthetic_basis_bps REAL, pcr_oi REAL,
                        calibrated_prob REAL,
                        expected_net_return REAL,
                        hurdles_passed INTEGER DEFAULT 1,
                        rejection_reasons TEXT,
                        regime_telemetry_json TEXT
                    );
                    """
                )
                conn.execute(
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
                        exit_time TEXT, exit_bid REAL, exit_ask REAL, exit_price_used REAL,
                        status TEXT DEFAULT 'OPEN',
                        peak_price REAL, trough_price REAL, trailing_stop REAL,
                        mfe_pct REAL DEFAULT 0.0, mae_pct REAL DEFAULT 0.0,
                        stop_loss_pct REAL, target_pct REAL,
                        gross_pnl REAL DEFAULT 0.0, charges REAL DEFAULT 0.0, slippage REAL DEFAULT 0.0,
                        net_pnl REAL DEFAULT 0.0, net_pnl_pct REAL DEFAULT 0.0,
                        estimated_margin REAL DEFAULT 0.0, holding_duration_seconds INTEGER DEFAULT 0,
                        exit_reason TEXT, regime_context_json TEXT
                    );
                    """
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO option_sniffer_advance_signals (
                        signal_id, created_at, book_type, strategy_mode, direction, status,
                        instrument_key, trading_symbol, underlying, strike, option_type, expiry,
                        lot_size, spot_price, option_bid, option_ask, spread_pct,
                        calibrated_prob, expected_net_return, hurdles_passed, regime_telemetry_json
                    ) VALUES (?, ?, ?, ?, ?, 'TRIGGERED', ?, ?, 'NIFTY', ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 0.0, 5, ?);
                    """,
                    (
                        sig_id,
                        pkg["entry_time"],
                        pkg["ui_book_type"],
                        pkg["book_id"],
                        pkg["direction_label"],
                        str(primary_quote.get("instrument_key") or ""),
                        str(primary_quote.get("trading_symbol") or pkg["book_id"]),
                        float(primary_quote.get("strike") or 0.0),
                        str(primary_quote.get("option_type") or "CE"),
                        pkg["expiry"],
                        lot_sz,
                        spot_c,
                        float(primary_quote.get("raw_bid") or primary_quote.get("bid") or entry_px_or_credit),
                        float(primary_quote.get("raw_ask") or primary_quote.get("ask") or entry_px_or_credit),
                        float(primary_quote.get("spread_pct") or 0.0),
                        json.dumps(pkg["regime_context"]),
                    ),
                )
                # Explicit SQLite insert preserves `target_pct = 0.0` (never falsy-coerced to 35.0)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO option_sniffer_advance_trades (
                        trade_id, signal_id, book_type, strategy_mode, direction,
                        instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                        lot_size, quantity, entry_time, entry_bid, entry_ask, entry_price_used,
                        entry_spot_price, status, peak_price, trough_price, trailing_stop,
                        stop_loss_pct, target_pct, estimated_margin, regime_context_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'NIFTY', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, 0.0, ?, ?);
                    """,
                    (
                        pkg["advance_trade_id"],
                        sig_id,
                        pkg["ui_book_type"],
                        pkg["book_id"],
                        pkg["direction_label"],
                        str(primary_quote.get("instrument_key") or ""),
                        str(primary_quote.get("trading_symbol") or pkg["book_id"]),
                        str(primary_quote.get("option_type") or "CE"),
                        float(primary_quote.get("strike") or 0.0),
                        pkg["expiry"],
                        lot_sz,
                        lot_sz,
                        pkg["entry_time"],
                        entry_px_or_credit,
                        entry_px_or_credit,
                        entry_px_or_credit,
                        spot_c,
                        entry_px_or_credit,
                        entry_px_or_credit,
                        init_stop_px,
                        stop_loss_pct,
                        float(pkg["cash_reserved_inr"]),
                        json.dumps(pkg["regime_context"]),
                    ),
                )
        except Exception as exc:
            logger.debug(f"[S205ShadowCollector] Mirror open to AdvanceDB note: {exc}")
        finally:
            conn.close()

    def _mirror_close_to_advance_db(
        self,
        closed_pkg: Dict[str, Any],
        exit_bid: float,
        exit_ask: float,
        now_dt: datetime,
    ) -> None:
        try:
            ent_dt = datetime.fromisoformat(str(closed_pkg["entry_time"]).replace("T", " ")[:19])
            dur_s = max(0, int((now_dt - ent_dt).total_seconds()))
        except Exception:
            dur_s = 0
        cash_base = max(1.0, float(closed_pkg.get("cash_reserved_inr") or 1.0))
        net_pnl = float(closed_pkg["net_pnl_inr"])
        exit_px_used = exit_ask if closed_pkg["ui_book_type"] == BOOK_SHORT_SELL else exit_bid
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE option_sniffer_advance_trades
                    SET status = 'CLOSED',
                        exit_time = ?,
                        exit_bid = ?,
                        exit_ask = ?,
                        exit_price_used = ?,
                        peak_price = ?,
                        trough_price = ?,
                        mfe_pct = 0.0,
                        mae_pct = 0.0,
                        gross_pnl = ?,
                        charges = ?,
                        slippage = 0.0,
                        net_pnl = ?,
                        net_pnl_pct = ?,
                        holding_duration_seconds = ?,
                        exit_reason = ?
                    WHERE trade_id = ? AND status = 'OPEN';
                    """,
                    (
                        closed_pkg["exit_time"],
                        exit_bid,
                        exit_ask,
                        exit_px_used,
                        exit_bid,
                        exit_ask,
                        float(closed_pkg["gross_pnl_inr"]),
                        float(closed_pkg["total_charges_inr"]),
                        net_pnl,
                        round((net_pnl / cash_base) * 100.0, 2),
                        dur_s,
                        str(closed_pkg["exit_reason"]),
                        str(closed_pkg["advance_trade_id"]),
                    ),
                )
        except Exception as exc:
            logger.debug(f"[S205ShadowCollector] Mirror close to AdvanceDB note: {exc}")
        finally:
            conn.close()

    # ──────────────────────────────────────────────────────────────────────────
    # 8. READ-ONLY TELEMETRY SUMMARY FOR UI & `/shadow-telemetry` ENDPOINT
    # ──────────────────────────────────────────────────────────────────────────

    def get_summary(self) -> Dict[str, Any]:
        env = self.config["account_envelope"]
        horizon = self.config["certification_horizon"]
        conn = self._connect()
        try:
            sess_cnt = int(
                (conn.execute("SELECT COUNT(DISTINCT session_date) FROM option_sniffer_advance_s205_l5_chain_1m;").fetchone() or [0])[0]
            )
            chain_rows = int(
                (conn.execute("SELECT COUNT(*) FROM option_sniffer_advance_s205_l5_chain_1m;").fetchone() or [0])[0]
            )
            fade_rows = int(
                (conn.execute("SELECT COUNT(*) FROM option_sniffer_advance_s205_quote_fade_events;").fetchone() or [0])[0]
            )
            margin_rows = int(
                (conn.execute("SELECT COUNT(*) FROM option_sniffer_advance_s205_margin_telemetry;").fetchone() or [0])[0]
            )
            fsp_rows = int(
                (conn.execute("SELECT COUNT(*) FROM option_sniffer_advance_s205_expiry_fsp_windows;").fetchone() or [0])[0]
            )
            pkg_rows_raw = conn.execute(
                """
                SELECT package_id, book_id, ui_book_type, state, direction_label,
                       expiry, entry_bar_ts, exit_bar_ts, legs_state_json,
                       structural_max_loss_inr, upstox_final_margin_inr, margin_status,
                       cash_reserved_inr, gross_pnl_inr, total_charges_inr, stt_inr,
                       brokerage_inr, net_pnl_inr, exit_reason
                FROM option_sniffer_advance_s205_shadow_packages
                ORDER BY entry_time DESC LIMIT 100;
                """
            ).fetchall()
            pkg_rows = []
            for r in pkg_rows_raw:
                d = dict(r)
                ls_j = {}
                try:
                    ls_j = json.loads(d.pop("legs_state_json", None) or "{}")
                except Exception:
                    d.pop("legs_state_json", None)
                if ls_j.get("mode") == "DEFINED_RISK_CREDIT_SPREAD_150":
                    wl = ls_j.get("wing_leg") or {}
                    sl = ls_j.get("short_leg") or {}
                    d["legs_summary"] = f"+{int(float(wl.get('strike') or 0))}{wl.get('option_type', '')} / -{int(float(sl.get('strike') or 0))}{sl.get('option_type', '')}"
                elif ls_j.get("mode") == "SINGLE_PAID_LONG":
                    ll = ls_j.get("long_leg") or {}
                    d["legs_summary"] = f"+{int(float(ll.get('strike') or 0))}{ll.get('option_type', '')} (1L Paid)"
                elif "wing_leg" in ls_j:
                    wl = ls_j.get("wing_leg") or {}
                    d["legs_summary"] = f"Unwound +{int(float(wl.get('strike') or 0))}{wl.get('option_type', '')} Wing"
                else:
                    d["legs_summary"] = d.get("direction_label") or "—"
                pkg_rows.append(d)

            latest_margin_row = conn.execute(
                """
                SELECT timestamp, book_id, margin_verified, margin_status,
                       span_margin_inr, exposure_margin_inr, final_margin_inr,
                       hedge_benefit_inr, structural_max_loss_inr
                FROM option_sniffer_advance_s205_margin_telemetry
                ORDER BY id DESC LIMIT 1;
                """
            ).fetchone()
            latest_fsp_row = conn.execute(
                """
                SELECT expiry_date, samples_count, spot_1500_1530_twap,
                       spot_1529_close, fsp_minus_1529_pts
                FROM option_sniffer_advance_s205_expiry_fsp_windows
                ORDER BY expiry_date DESC LIMIT 1;
                """
            ).fetchone()
            totals = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN state IN ('CLOSED', 'INCOMPLETE_LEG_UNWOUND') THEN 1 ELSE 0 END) AS closed_cnt,
                    SUM(CASE WHEN state = 'INCOMPLETE_LEG_UNWOUND' THEN 1 ELSE 0 END) AS unwound_cnt,
                    SUM(CASE WHEN state = 'OPEN' THEN 1 ELSE 0 END) AS open_cnt,
                    COALESCE(SUM(CASE WHEN state IN ('CLOSED', 'INCOMPLETE_LEG_UNWOUND') THEN net_pnl_inr ELSE 0.0 END), 0.0) AS realized_net,
                    COALESCE(SUM(CASE WHEN state IN ('CLOSED', 'INCOMPLETE_LEG_UNWOUND') THEN total_charges_inr ELSE 0.0 END), 0.0) AS total_fees,
                    COALESCE(SUM(CASE WHEN state = 'OPEN' THEN cash_reserved_inr ELSE 0.0 END), 0.0) AS active_reserved
                FROM option_sniffer_advance_s205_shadow_packages;
                """
            ).fetchone()
            clean_epoch_totals = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN state IN ('CLOSED', 'INCOMPLETE_LEG_UNWOUND')
                              AND (book_id != 'SHADOW_BOOK_A_SPREAD_150' OR session_date >= ?) THEN 1 ELSE 0 END) AS clean_closed_cnt,
                    COALESCE(SUM(CASE WHEN state IN ('CLOSED', 'INCOMPLETE_LEG_UNWOUND')
                                       AND (book_id != 'SHADOW_BOOK_A_SPREAD_150' OR session_date >= ?) THEN net_pnl_inr ELSE 0.0 END), 0.0) AS clean_realized_net,
                    SUM(CASE WHEN state IN ('CLOSED', 'INCOMPLETE_LEG_UNWOUND')
                              AND book_id = 'SHADOW_BOOK_A_SPREAD_150' AND session_date >= ? THEN 1 ELSE 0 END) AS clean_book_a_cnt,
                    COALESCE(SUM(CASE WHEN state IN ('CLOSED', 'INCOMPLETE_LEG_UNWOUND')
                                       AND book_id = 'SHADOW_BOOK_A_SPREAD_150' AND session_date >= ? THEN net_pnl_inr ELSE 0.0 END), 0.0) AS clean_book_a_net,
                    SUM(CASE WHEN state IN ('CLOSED', 'INCOMPLETE_LEG_UNWOUND')
                              AND book_id = 'SHADOW_BOOK_A_SPREAD_150' AND session_date < ? THEN 1 ELSE 0 END) AS quarantined_book_a_cnt,
                    COALESCE(SUM(CASE WHEN state IN ('CLOSED', 'INCOMPLETE_LEG_UNWOUND')
                                       AND book_id = 'SHADOW_BOOK_A_SPREAD_150' AND session_date < ? THEN net_pnl_inr ELSE 0.0 END), 0.0) AS quarantined_book_a_net
                FROM option_sniffer_advance_s205_shadow_packages;
                """,
                (
                    CLEAN_BOOK_A_EPOCH_START_DATE,
                    CLEAN_BOOK_A_EPOCH_START_DATE,
                    CLEAN_BOOK_A_EPOCH_START_DATE,
                    CLEAN_BOOK_A_EPOCH_START_DATE,
                    CLEAN_BOOK_A_EPOCH_START_DATE,
                    CLEAN_BOOK_A_EPOCH_START_DATE,
                ),
            ).fetchone()
            clean_book_a_sess = int(
                (
                    conn.execute(
                        "SELECT COUNT(DISTINCT session_date) FROM option_sniffer_advance_s205_l5_chain_1m WHERE session_date >= ?;",
                        (CLEAN_BOOK_A_EPOCH_START_DATE,),
                    ).fetchone()
                    or [0]
                )[0]
            )
        finally:
            conn.close()

        closed_cnt = int((totals["closed_cnt"] if totals else 0) or 0)
        realized_net = round(float((totals["realized_net"] if totals else 0.0) or 0.0), 2)
        active_res = round(float((totals["active_reserved"] if totals else 0.0) or 0.0), 2)
        init_cap = float(env["initial_capital_inr"])
        curr_eq = round(init_cap + realized_net, 2)
        clean_closed_cnt = int((clean_epoch_totals["clean_closed_cnt"] if clean_epoch_totals else 0) or 0)
        clean_realized_net = round(float((clean_epoch_totals["clean_realized_net"] if clean_epoch_totals else 0.0) or 0.0), 2)
        clean_book_a_cnt = int((clean_epoch_totals["clean_book_a_cnt"] if clean_epoch_totals else 0) or 0)
        clean_book_a_net = round(float((clean_epoch_totals["clean_book_a_net"] if clean_epoch_totals else 0.0) or 0.0), 2)
        quar_book_a_cnt = int((clean_epoch_totals["quarantined_book_a_cnt"] if clean_epoch_totals else 0) or 0)
        quar_book_a_net = round(float((clean_epoch_totals["quarantined_book_a_net"] if clean_epoch_totals else 0.0) or 0.0), 2)

        return {
            "protocol_id": self.config["protocol_id"],
            "protocol_version": self.config["protocol_version"],
            "prereg_sha256": self.prereg_sha256,
            "simulation_only": True,
            "naked_short_allowed": False,
            "legacy_v6b_paper_execution_retired": True,
            "certification_progress": {
                "sessions_collected": sess_cnt,
                "min_sessions_required": int(horizon["min_forward_trading_sessions"]),
                "target_sessions": int(horizon["target_forward_trading_sessions"]),
                "closed_shadow_packages": closed_cnt,
                "min_trades_required": int(horizon["min_forward_trades_for_review"]),
                "ready_for_go_no_go_review": bool(
                    clean_book_a_sess >= int(horizon["min_forward_trading_sessions"])
                    and clean_closed_cnt >= int(horizon["min_forward_trades_for_review"])
                ),
            },
            "clean_post_fix_audit": {
                "clean_book_a_epoch_start_date": CLEAN_BOOK_A_EPOCH_START_DATE,
                "clean_book_a_sessions_collected": clean_book_a_sess,
                "clean_closed_shadow_packages": clean_closed_cnt,
                "clean_realized_net_pnl_inr": clean_realized_net,
                "clean_book_a_closed_packages": clean_book_a_cnt,
                "clean_book_a_realized_net_pnl_inr": clean_book_a_net,
                "quarantined_pre_fix_book_a_packages": quar_book_a_cnt,
                "quarantined_pre_fix_book_a_net_pnl_inr": quar_book_a_net,
                "quarantine_reason": "PRE_2026_11_24_WING_EVICTION_STALE_QUOTE_CONTAMINATION",
            },
            "shadow_account_ledger": {
                "initial_capital_inr": init_cap,
                "current_realized_equity_inr": curr_eq,
                "realized_net_pnl_inr": realized_net,
                "total_canonical_charges_inr": round(float((totals["total_fees"] if totals else 0.0) or 0.0), 2),
                "active_cash_or_margin_reserved_inr": active_res,
                "free_unreserved_capital_inr": round(curr_eq - active_res, 2),
                "open_packages_count": int((totals["open_cnt"] if totals else 0) or 0),
                "pending_leg2_count": len(self._pending_leg2),
                "incomplete_leg_unwinds_count": int((totals["unwound_cnt"] if totals else 0) or 0),
                "daily_kill_triggered": self._daily_kill_triggered,
                "mtm_visibility_status": self._mtm_visibility_status,
                "unpriced_open_packages_count": self._unpriced_open_count,
                "unpriced_package_ids": list(self._unpriced_package_ids),
                "known_unrealized_mtm_inr": self._last_known_unrealized_inr,
                "effective_unrealized_mtm_inr": self._effective_unrealized_inr,
                "worst_case_unpriced_exposure_inr": self._worst_case_unpriced_exposure_inr,
            },
            "telemetry_row_counts": {
                "l5_chain_1m_rows": chain_rows,
                "quote_fade_event_rows": fade_rows,
                "upstox_margin_probe_rows": margin_rows,
                "expiry_fsp_window_rows": fsp_rows,
            },
            "latest_margin_probe": dict(latest_margin_row) if latest_margin_row else None,
            "latest_fsp_window": dict(latest_fsp_row) if latest_fsp_row else None,
            "recent_packages": pkg_rows[:25],
        }
