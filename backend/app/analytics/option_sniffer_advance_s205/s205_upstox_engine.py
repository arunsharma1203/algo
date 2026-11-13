"""
Option Sniffer Advance — S2_05 (`V6B + Causal Conformal Meta-Learner`) & Upstox V3 Live Orchestrator.

Key Institutional Safeguards:
  1. 100% Paper-Mode Default (`SIMULATION_ONLY = True`); `AdvanceLiveBridge` Arm/Disarm PIN,
     broker order placement, Exchange-Resting SL protection, and SOS panic exit remain 100% untouched.
  2. Zero bytes modified in `backend/app/analytics/option_sniffer_advance/*` or
     `backend/app/analytics/option_sniffer_advance_live/*`.
  3. Event-driven `:00` completed 1-minute candle-close signal evaluation (`<100ms` latency)
     with automatic `09:15..now` Upstox V3 1-minute intraday backfill on startup.
  4. Strict Close-Only Ratchets (updated ONLY at `:00` 1m bar close from bar close price),
     paired with sub-second Hard Stop-Loss and sub-second `-₹3,500` Portfolio MTM Daily Kill.
  5. Once-per-day `±450pt` OTM NIFTY Session Catastrophe Wing live quote subscription & tracking.
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.parse
import uuid
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.option_sniffer.option_constants import (
    DEFAULT_LOT_SIZES,
    MAX_SPREAD_PCT,
    calculate_option_trade_costs,
    to_ist_naive,
)
from app.analytics.option_sniffer.option_contract_discovery import OptionContractDiscovery
from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
from app.analytics.option_sniffer_advance.advance_chain_intelligence import AdvanceChainIntelligence
from app.analytics.option_sniffer_advance.advance_constants import (
    BOOK_LONG_BUY,
    BOOK_SHORT_SELL,
    SHORT_AUTO_HEDGE_WING_PTS_NIFTY,
    calculate_short_option_trade_costs,
)
from app.analytics.option_sniffer_advance.advance_db import (
    AdvanceDB,
    init_option_sniffer_advance_tables,
)
from app.analytics.option_sniffer_advance_s205.s205_meta_model import (
    S205CausalMetaModel,
    get_s205_meta_model,
)
from app.analytics.option_sniffer_advance_s205.s205_signal_evaluator import (
    build_causal_intraday_context,
    compute_close_only_ratchet,
    evaluate_s205_bar_and_readiness,
)
from app.data.historical_data_layer import get_db_path
from app.data.upstox_provider import UPSTOX_BASE_URL, get_upstox_token, upstox_request

logger = logging.getLogger(__name__)

S205_DAILY_MTM_KILL_INR: float = -3500.0
S205_MAX_TOTAL_OPEN: int = 2
S205_MAX_TOTAL_TRADES_DAY: int = 3
S205_BOOK_LIMITS: Dict[str, Tuple[int, int]] = {
    BOOK_LONG_BUY: (1, 1),
    BOOK_SHORT_SELL: (1, 2),
}
S205_COOLDOWN_SECONDS: int = 15 * 60  # 15 minutes (15 bars) post-exit cooldown
S205_CATASTROPHE_WING_OFFSET_PTS: float = 450.0

_HW_INSTRUMENT_KEYS: Dict[str, str] = {
    "RELIANCE": "NSE_EQ|INE002A01018",
    "HDFCBANK": "NSE_EQ|INE040A01034",
    "ICICIBANK": "NSE_EQ|INE090A01021",
}

_S205_MODULE_FILES: Tuple[str, ...] = (
    "__init__.py",
    "s205_meta_model.py",
    "s205_meta_weights.json",
    "s205_signal_evaluator.py",
    "s205_upstox_engine.py",
)


def _compute_s205_hashes() -> Dict[str, str]:
    base = os.path.dirname(os.path.abspath(__file__))
    out: Dict[str, str] = {}
    for fname in _S205_MODULE_FILES:
        fpath = os.path.join(base, fname)
        if os.path.exists(fpath):
            with open(fpath, "rb") as fh:
                out[fname] = hashlib.sha256(fh.read()).hexdigest()[:16]
    return out


_BOOT_S205_HASHES: Dict[str, str] = {}


def init_s205_tables() -> None:
    """Initializes core Advance tables + additive S2_05 L5 & Catastrophe Wing tables."""
    init_option_sniffer_advance_tables()
    try:
        conn = sqlite3.connect(get_db_path(), timeout=15.0)
        with conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_sniffer_advance_s205_l5_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    bar_ts TEXT NOT NULL,
                    spot_ltp REAL,
                    fut_ltp REAL,
                    fut_atp_vwap REAL,
                    real_basis_pts REAL,
                    fut_oi REAL,
                    india_vix REAL,
                    avg_spread_pct REAL,
                    avg_l5_obi REAL,
                    readiness_json TEXT
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_sniffer_advance_s205_catastrophe_wings (
                    session_date TEXT PRIMARY KEY,
                    expiry TEXT,
                    anchor_atm REAL,
                    ce_wing_key TEXT,
                    ce_wing_symbol TEXT,
                    ce_wing_strike REAL,
                    ce_wing_ask REAL,
                    pe_wing_key TEXT,
                    pe_wing_symbol TEXT,
                    pe_wing_strike REAL,
                    pe_wing_ask REAL,
                    status TEXT DEFAULT 'STANDBY',
                    activated_at TEXT,
                    activated_side TEXT,
                    virtual_cost_inr REAL DEFAULT 0.0,
                    updated_at TEXT
                );
                """
            )
        conn.close()
    except Exception as e:
        logger.error(f"[S205Engine] Table init error: {e}")


class S205UpstoxOrchestrator:
    """
    Singleton live Upstox V3 + `S2_05` (`V6B + Causal Meta-Learner`) orchestrator
    powering Option Sniffer Advance in 100% Paper Sandbox Mode (`SIMULATION_ONLY = True`).
    """

    SIMULATION_ONLY: bool = True

    _instance: Optional["S205UpstoxOrchestrator"] = None
    _init_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        init_s205_tables()
        self._op_lock = threading.RLock()
        self._bar_wake_event = threading.Event()
        self._meta_model: S205CausalMetaModel = get_s205_meta_model()
        self._is_running: bool = False
        self._worker_thread: Optional[threading.Thread] = None

        # Session rolling bar state (reset automatically on IST date rollover)
        self._session_date: str = ""
        self._spot_bars: Dict[str, Tuple[float, float, float, float, float]] = {}
        self._vix_bars: Dict[str, Tuple[float, float, float, float]] = {}
        self._fut_bars: Dict[str, Tuple[float, float, float, float, float, float]] = {}
        self._opt_bars: Dict[Tuple[str, float, str], Dict[str, Tuple[float, float, float, float, float, float]]] = {}
        self._hw_15m_bars: Dict[str, List[Tuple[str, float, float, float, float]]] = {}
        self._opt_prev_vtt: Dict[str, int] = {}
        self._prior_day_stats: Dict[str, Any] = {}

        # Per-trade close-only ratchet state: trade_id -> {best_close_pts, ratchet_stop, entry_bar_ts, spec}
        self._open_trade_runtime: Dict[str, Dict[str, Any]] = {}
        self._book_cooldown_until: Dict[str, float] = {BOOK_LONG_BUY: 0.0, BOOK_SHORT_SELL: 0.0}
        self._daily_kill_triggered: bool = False
        self._daily_kill_reason: str = ""

        # Backfill & sweep telemetry
        self._last_prior_day_fetch_date: str = ""
        self._last_intraday_backfill_ts: float = 0.0
        self._last_hw_fetch_ts: float = 0.0
        self._last_wing_pin_ts: float = 0.0
        self._last_evaluated_bar_ts: str = ""
        self._last_eval_latency_ms: float = 0.0
        self._sweep_count: int = 0
        self._last_sweep_time: Optional[str] = None
        self._last_chain_intel: Dict[str, Dict[str, Any]] = {}
        self._catastrophe_wing_state: Dict[str, Any] = {}
        self._latest_engine_readiness: List[Dict[str, Any]] = []
        self._latest_context_summary: Dict[str, Any] = {}

        self._install_tick_guard_router()
        try:
            OptionStreamManager().register_advance_feed_listener(self.on_live_feed_events)
        except Exception:
            pass

        self._initialized = True
        logger.info(
            f"[S205Engine] Initialized S2_05 Upstox V3 Orchestrator "
            f"(Fold={self._meta_model.fold_id}, p_min={self._meta_model.p_min}, sha256={self._meta_model.weights_sha256[:12]})."
        )

    def _install_tick_guard_router(self) -> None:
        """
        Wraps `AdvancePaperExecution.evaluate_subsecond_tick_guard` in memory (0 disk bytes touched)
        so `V6B_*` trades are strictly protected by S2_05's close-only ratchet + -₹3,500 MTM kill,
        while non-`V6B_*` legacy unit-test trades pass through to V2.0 untouched.
        """
        try:
            from app.analytics.option_sniffer_advance.advance_paper_execution import AdvancePaperExecution

            if getattr(AdvancePaperExecution, "_s205_guard_wrapped", False):
                return
            orig_fn = AdvancePaperExecution.evaluate_subsecond_tick_guard

            def _routed_guard(ikey: str, quote_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
                s205_closed: List[Dict[str, Any]] = []
                curr_orch = get_s205_orchestrator_if_loaded()
                if curr_orch is not None:
                    s205_closed = curr_orch.evaluate_subsecond_tick_guard(ikey, quote_payload) or []
                legacy_closed: List[Dict[str, Any]] = []
                try:
                    non_v6b = [
                        t for t in (AdvanceDB.get_open_trades() or [])
                        if not str(t.get("strategy_mode") or "").startswith("V6B_")
                    ]
                    if non_v6b:
                        legacy_closed = orig_fn(ikey, quote_payload) or []
                except Exception:
                    pass
                return s205_closed + legacy_closed

            AdvancePaperExecution.evaluate_subsecond_tick_guard = _routed_guard
            AdvancePaperExecution._s205_guard_wrapped = True
        except Exception as e:
            logger.debug(f"[S205Engine] Tick guard router note: {e}")

    def _reset_session_if_needed(self, now_dt: datetime) -> None:
        today_s = now_dt.strftime("%Y-%m-%d")
        if self._session_date != today_s:
            self._session_date = today_s
            self._spot_bars.clear()
            self._vix_bars.clear()
            self._fut_bars.clear()
            self._opt_bars.clear()
            self._hw_15m_bars.clear()
            self._opt_prev_vtt.clear()
            self._daily_kill_triggered = False
            self._daily_kill_reason = ""
            self._last_evaluated_bar_ts = ""
            self._book_cooldown_until = {BOOK_LONG_BUY: 0.0, BOOK_SHORT_SELL: 0.0}
            self._catastrophe_wing_state = {
                "session_date": today_s,
                "status": "STANDBY",
                "offset_pts": S205_CATASTROPHE_WING_OFFSET_PTS,
                "ce_wing_strike": 0.0,
                "ce_wing_ask": 0.0,
                "pe_wing_strike": 0.0,
                "pe_wing_ask": 0.0,
                "virtual_cost_inr": 0.0,
            }

    # ──────────────────────────────────────────────────────────────────────────
    # REAL-TIME UPSTOX V3 TICK -> 1M BAR AGGREGATOR & :00 WAKEUP
    # ──────────────────────────────────────────────────────────────────────────

    def on_live_feed_events(self, events: List[Tuple[str, str, Dict[str, Any]]]) -> None:
        """Ingests real-time WebSocket V3 index, futures, and option ticks into 1m session bars."""
        now_dt = to_ist_naive()
        t_cur = now_dt.time()
        if t_cur < dt_time(9, 15) or t_cur > dt_time(15, 30):
            return

        bar_ts = now_dt.strftime("%Y-%m-%d %H:%M:00")
        prev_min_dt = now_dt - timedelta(minutes=1)
        completed_ts = prev_min_dt.strftime("%Y-%m-%d %H:%M:00") if prev_min_dt.time() >= dt_time(9, 15) else ""
        should_wake = False

        with self._op_lock:
            self._reset_session_if_needed(now_dt)
            for kind, key_or_sym, payload in events:
                if kind == "INDEX":
                    px = float(payload.get("spot_ltp") or 0.0)
                    if px <= 0:
                        continue
                    if key_or_sym == "NIFTY":
                        cur = self._spot_bars.get(bar_ts)
                        self._spot_bars[bar_ts] = (px, px, px, px, 1.0) if cur is None else (cur[0], max(cur[1], px), min(cur[2], px), px, cur[4] + 1.0)
                    elif "VIX" in key_or_sym.upper():
                        cur_v = self._vix_bars.get(bar_ts)
                        self._vix_bars[bar_ts] = (px, px, px, px) if cur_v is None else (cur_v[0], max(cur_v[1], px), min(cur_v[2], px), px)
                elif kind == "FUTURES" and key_or_sym == "NIFTY":
                    fpx = float(payload.get("fut_ltp") or 0.0)
                    fvtt = float(payload.get("fut_volume_vtt") or 0.0)
                    foi = float(payload.get("fut_oi") or 0.0)
                    if fpx > 0:
                        cf = self._fut_bars.get(bar_ts)
                        self._fut_bars[bar_ts] = (fpx, fpx, fpx, fpx, fvtt, foi) if cf is None else (cf[0], max(cf[1], fpx), min(cf[2], fpx), fpx, fvtt, foi)
                elif kind == "OPTION":
                    if str(payload.get("underlying") or "").upper() != "NIFTY":
                        continue
                    exp_s = str(payload.get("expiry") or "")[:10]
                    stk = float(payload.get("strike") or 0.0)
                    ot = str(payload.get("option_type") or "").upper()
                    opx = float(payload.get("ltp") or 0.0)
                    vtt = int(payload.get("volume") or 0)
                    ooi = float(payload.get("oi") or 0.0)
                    if not exp_s or stk <= 0 or ot not in ("CE", "PE") or opx <= 0:
                        continue
                    prev_v = self._opt_prev_vtt.get(key_or_sym, vtt)
                    d_vol = max(0, vtt - prev_v)
                    self._opt_prev_vtt[key_or_sym] = vtt
                    eso = (exp_s, stk, ot)
                    bmap = self._opt_bars.setdefault(eso, {})
                    co = bmap.get(bar_ts)
                    bmap[bar_ts] = (
                        (opx, opx, opx, opx, max(1.0, float(d_vol)), ooi)
                        if co is None
                        else (co[0], max(co[1], opx), min(co[2], opx), opx, co[4] + float(d_vol), ooi if ooi > 0 else co[5])
                    )

            if completed_ts and completed_ts > self._last_evaluated_bar_ts and completed_ts in self._spot_bars:
                should_wake = True

        if should_wake:
            self._bar_wake_event.set()

    # ──────────────────────────────────────────────────────────────────────────
    # UPSTOX HISTORICAL DAILY & INTRADAY BACKFILL (ZERO COLD-START BLINDNESS)
    # ──────────────────────────────────────────────────────────────────────────

    def _upstox_get_candles(self, url: str) -> List[list]:
        token = get_upstox_token()
        if not token:
            return []
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token.strip()}"}
        try:
            res = upstox_request("GET", url, headers=headers, timeout=6.0)
            if res.status_code == 200:
                return list((res.json().get("data") or {}).get("candles") or [])
        except Exception as e:
            logger.debug(f"[S205Engine] Candle fetch note ({url[-45:]}): {e}")
        return []

    def _ensure_prior_day_and_intraday_backfill(self, now_dt: datetime, allow_network: bool = True) -> None:
        today_s = now_dt.strftime("%Y-%m-%d")
        with self._op_lock:
            self._reset_session_if_needed(now_dt)

        # 1. Seed NIFTY 1m bars from local SQLite `continuous_index_ohlcv` first (0 network cost!)
        try:
            conn = sqlite3.connect(get_db_path(), timeout=5.0)
            rows = conn.execute(
                """
                SELECT substr(replace(timestamp, 'T', ' '), 1, 19), open, high, low, close, volume
                FROM continuous_index_ohlcv
                WHERE ticker = 'NIFTY' AND substr(replace(timestamp, 'T', ' '), 1, 10) = ?
                ORDER BY timestamp ASC
                """,
                (today_s,),
            ).fetchall()
            conn.close()
            with self._op_lock:
                for ts_r, o_r, h_r, l_r, c_r, v_r in rows:
                    if "09:15:00" <= ts_r[11:19] <= "15:29:00" and ts_r not in self._spot_bars:
                        self._spot_bars[ts_r] = (float(o_r), float(h_r), float(l_r), float(c_r), float(v_r or 1.0))
        except Exception:
            pass

        if not allow_network or not get_upstox_token():
            return

        now_epoch = time.time()
        # 2. Prior-day daily OHLC + 20d IVP (once per session)
        if self._last_prior_day_fetch_date != today_s:
            self._last_prior_day_fetch_date = today_s
            from_d = (now_dt.date() - timedelta(days=40)).strftime("%Y-%m-%d")
            prev_d = (now_dt.date() - timedelta(days=1)).strftime("%Y-%m-%d")
            n_enc = urllib.parse.quote("NSE_INDEX|Nifty 50", safe="")
            v_enc = urllib.parse.quote("NSE_INDEX|India VIX", safe="")
            d_candles = self._upstox_get_candles(f"{UPSTOX_BASE_URL}/historical-candle/{n_enc}/day/{prev_d}/{from_d}")
            v_candles = self._upstox_get_candles(f"{UPSTOX_BASE_URL}/historical-candle/{v_enc}/day/{prev_d}/{from_d}")
            d_valid = sorted([c for c in d_candles if str(c[0])[:10] < today_s], key=lambda x: str(x[0]))
            v_valid = sorted([c for c in v_candles if str(c[0])[:10] < today_s], key=lambda x: str(x[0]))
            if d_valid:
                last_c = d_valid[-1]
                pd_h, pd_l, pd_c = float(last_c[2]), float(last_c[3]), float(last_c[4])
                pivot = (pd_h + pd_l + pd_c) / 3.0
                bc = (pd_h + pd_l) / 2.0
                tc = (pivot - bc) + pivot
                cpr_w = abs(tc - bc) / pd_c * 100.0 if pd_c > 0 else 0.25
                rngs = [float(x[2]) - float(x[3]) for x in d_valid[-7:]]
                pd_rng = pd_h - pd_l
                with self._op_lock:
                    self._prior_day_stats = {
                        "pd_h": pd_h,
                        "pd_l": pd_l,
                        "pd_c": pd_c,
                        "cpr_width_pct": round(cpr_w, 4),
                        "is_nr4": len(rngs) >= 4 and pd_rng <= min(rngs[-4:]) * 1.02,
                        "is_nr7": len(rngs) >= 7 and pd_rng <= min(rngs[-7:]) * 1.02,
                        "prior_vix_closes": [float(x[4]) for x in v_valid[-20:]],
                        "last_vix_close": float(v_valid[-1][4]) if v_valid else 14.0,
                    }

        # 3. Pin Front-Month Futures & Daily ±450pt Catastrophe Wings (at most once per 120s)
        if (now_epoch - self._last_wing_pin_ts) >= 120.0:
            self._last_wing_pin_ts = now_epoch
            try:
                fut_map = OptionContractDiscovery.discover_front_futures(allow_network=True) or {}
                extra_to_pin: List[Dict[str, Any]] = []
                for _, slots in fut_map.items():
                    if isinstance((slots or {}).get("front"), dict):
                        extra_to_pin.append(dict(slots["front"]))

                sm = OptionStreamManager()
                u_q = sm.get_underlying_quote("NIFTY") or {}
                spot_now = float(u_q.get("spot_ltp") or 0.0)
                exps = OptionContractDiscovery.get_available_expiries("NIFTY", force_refresh=False)
                if spot_now > 0 and exps:
                    exp_w = exps[0]
                    atm_w = OptionContractDiscovery.calculate_atm_strike("NIFTY", spot_now)
                    ce_w = OptionContractDiscovery.find_option_contract("NIFTY", exp_w, atm_w + S205_CATASTROPHE_WING_OFFSET_PTS, "CE")
                    pe_w = OptionContractDiscovery.find_option_contract("NIFTY", exp_w, atm_w - S205_CATASTROPHE_WING_OFFSET_PTS, "PE")
                    for w_c in (ce_w, pe_w):
                        if w_c:
                            w_c["is_catastrophe_wing"] = True
                            extra_to_pin.append(w_c)
                    with self._op_lock:
                        self._catastrophe_wing_state.update(
                            {
                                "expiry": exp_w,
                                "anchor_atm": atm_w,
                                "ce_wing_key": (ce_w or {}).get("instrument_key"),
                                "ce_wing_symbol": (ce_w or {}).get("trading_symbol"),
                                "ce_wing_strike": float((ce_w or {}).get("strike") or (atm_w + S205_CATASTROPHE_WING_OFFSET_PTS)),
                                "pe_wing_key": (pe_w or {}).get("instrument_key"),
                                "pe_wing_symbol": (pe_w or {}).get("trading_symbol"),
                                "pe_wing_strike": float((pe_w or {}).get("strike") or (atm_w - S205_CATASTROPHE_WING_OFFSET_PTS)),
                            }
                        )
                if extra_to_pin:
                    sm.pin_extra_contracts(extra_to_pin)
            except Exception as pe:
                logger.debug(f"[S205Engine] Pin futures/wings note: {pe}")

        if now_dt.time() < dt_time(9, 16):
            return

        # 4. Intraday 15m Heavyweights refresh (once every 5 minutes)
        if (now_epoch - self._last_hw_fetch_ts) >= 300.0:
            self._last_hw_fetch_ts = now_epoch
            for sym, ikey in _HW_INSTRUMENT_KEYS.items():
                enc = urllib.parse.quote(ikey, safe="")
                c15 = self._upstox_get_candles(f"{UPSTOX_BASE_URL}/historical-candle/intraday/{enc}/30minute" if False else f"https://api.upstox.com/v3/historical-candle/intraday/{enc}/minutes/15")
                rows15 = []
                for c in c15:
                    ts15 = str(c[0])[:19].replace("T", " ")
                    if ts15[:10] == today_s and "09:15:00" <= ts15[11:19] <= "15:15:00":
                        rows15.append((ts15, float(c[1]), float(c[2]), float(c[3]), float(c[4])))
                if rows15:
                    rows15.sort(key=lambda x: x[0])
                    with self._op_lock:
                        self._hw_15m_bars[sym] = rows15

        # 5. Intraday 1m Spot, VIX & active ATM±100 Option backfill if cold-started mid-session
        expected_bars = max(0, int((now_dt - now_dt.replace(hour=9, minute=15, second=0, microsecond=0)).total_seconds() // 60))
        need_spot_backfill = len(self._spot_bars) + 2 < min(expected_bars, 240)
        if need_spot_backfill and (now_epoch - self._last_intraday_backfill_ts) >= 60.0:
            self._last_intraday_backfill_ts = now_epoch
            n_enc = urllib.parse.quote("NSE_INDEX|Nifty 50", safe="")
            v_enc = urllib.parse.quote("NSE_INDEX|India VIX", safe="")
            s_cands = self._upstox_get_candles(f"{UPSTOX_BASE_URL}/historical-candle/intraday/{n_enc}/1minute")
            v_cands = self._upstox_get_candles(f"{UPSTOX_BASE_URL}/historical-candle/intraday/{v_enc}/1minute")
            with self._op_lock:
                for c in s_cands:
                    ts_m = str(c[0])[:19].replace("T", " ")
                    if ts_m[:10] == today_s and "09:15:00" <= ts_m[11:19] <= "15:29:00":
                        self._spot_bars[ts_m] = (float(c[1]), float(c[2]), float(c[3]), float(c[4]), max(1.0, float(c[5] or 1.0)))
                for c in v_cands:
                    ts_m = str(c[0])[:19].replace("T", " ")
                    if ts_m[:10] == today_s and "09:15:00" <= ts_m[11:19] <= "15:29:00":
                        self._vix_bars[ts_m] = (float(c[1]), float(c[2]), float(c[3]), float(c[4]))

            # Backfill core ATM +/- 2 strikes for NIFTY front expiry so Option_VWAP & 09:15 Spike Peak are exact
            sm = OptionStreamManager()
            active_qs = [q for q in sm.get_all_active_option_quotes() if str(q.get("underlying") or "").upper() == "NIFTY"]
            spot_ref = float((sm.get_underlying_quote("NIFTY") or {}).get("spot_ltp") or 0.0)
            if spot_ref > 0 and active_qs:
                atm_r = round(spot_ref / 50.0) * 50.0
                core_qs = [q for q in active_qs if abs(float(q.get("strike") or 0.0) - atm_r) <= 100.0][:12]
                for q in core_qs:
                    ik = str(q.get("instrument_key") or "")
                    if not ik:
                        continue
                    eso = (str(q.get("expiry") or "")[:10], float(q.get("strike") or 0.0), str(q.get("option_type") or "CE").upper())
                    if len(self._opt_bars.get(eso, {})) >= max(1, expected_bars - 3):
                        continue
                    o_cands = self._upstox_get_candles(f"{UPSTOX_BASE_URL}/historical-candle/intraday/{urllib.parse.quote(ik, safe='')}/1minute")
                    with self._op_lock:
                        bmap = self._opt_bars.setdefault(eso, {})
                        for c in o_cands:
                            ts_m = str(c[0])[:19].replace("T", " ")
                            if ts_m[:10] == today_s and "09:15:00" <= ts_m[11:19] <= "15:29:00":
                                bmap[ts_m] = (float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5] or 0.0), float(c[6] if len(c) > 6 else (q.get("oi") or 0.0)))

    # ──────────────────────────────────────────────────────────────────────────
    # SUB-SECOND HARD-SL & -₹3,500 PORTFOLIO MTM DAILY KILL GUARD
    # ──────────────────────────────────────────────────────────────────────────

    def _compute_today_session_pnl(self, today_s: str, open_trades: List[Dict[str, Any]]) -> Tuple[float, float]:
        realized = 0.0
        for ct in AdvanceDB.get_closed_trades(limit=100):
            if str(ct.get("entry_time") or "")[:10] == today_s:
                realized += float(ct.get("net_pnl") or 0.0)
        unrealized = 0.0
        for ot in open_trades:
            if str(ot.get("entry_time") or "")[:10] == today_s:
                unrealized += float(ot.get("net_pnl") or 0.0)
        return round(realized, 2), round(unrealized, 2)

    def _close_v6b_paper_trade(
        self,
        trade: Dict[str, Any],
        exit_bid: float,
        exit_ask: float,
        exit_reason: str,
        now_dt: datetime,
    ) -> Optional[Dict[str, Any]]:
        tid = str(trade["trade_id"])
        btype = str(trade.get("book_type") or BOOK_SHORT_SELL)
        qty = int(trade.get("quantity") or trade.get("lot_size") or 65)
        entry_bid = float(trade.get("entry_bid") or 0.0)
        entry_ask = float(trade.get("entry_ask") or 0.0)
        try:
            ent_dt = datetime.fromisoformat(str(trade["entry_time"]).replace("T", " ")[:19])
            dur_s = max(0, int((now_dt - ent_dt).total_seconds()))
        except Exception:
            dur_s = 0

        if btype == BOOK_SHORT_SELL:
            exit_px = exit_ask
            pk = max(float(trade.get("peak_price") or entry_bid), exit_ask)
            tr = min(float(trade.get("trough_price") or entry_bid), exit_ask)
            mfe = round(((entry_bid - tr) / max(0.05, entry_bid)) * 100.0, 2)
            mae = round(((entry_bid - pk) / max(0.05, entry_bid)) * 100.0, 2)
            res = calculate_short_option_trade_costs(
                sell_entry_bid=entry_bid,
                buy_exit_ask=exit_ask,
                quantity=qty,
                apply_slippage=True,
                trade_date=trade.get("entry_time"),
                entry_bid=entry_bid,
                entry_ask=entry_ask,
                exit_bid=exit_bid,
                exit_ask=exit_ask,
            )
            gross, chg, slip, net, net_pct = res["gross_pnl"], res["statutory_charges"], res["slippage"], res["net_pnl"], res["return_on_premium_pct"]
        else:
            exit_px = exit_bid
            pk = max(float(trade.get("peak_price") or entry_ask), exit_bid)
            tr = min(float(trade.get("trough_price") or entry_ask), exit_bid)
            mfe = round(((pk - entry_ask) / max(0.05, entry_ask)) * 100.0, 2)
            mae = round(((tr - entry_ask) / max(0.05, entry_ask)) * 100.0, 2)
            costs = calculate_option_trade_costs(
                buy_price=entry_ask,
                sell_price=exit_bid,
                quantity=qty,
                apply_slippage=True,
                trade_date=trade.get("entry_time"),
                buy_bid=entry_bid,
                buy_ask=entry_ask,
                sell_bid=exit_bid,
                sell_ask=exit_ask,
            )
            gross = round((exit_bid - entry_ask) * qty, 2)
            chg, slip = costs["statutory_charges"], costs["slippage"]
            net = round(gross - costs["total_friction"], 2)
            net_pct = round((net / max(1.0, entry_ask * qty)) * 100.0, 2)

        close_data = {
            "status": "CLOSED",
            "exit_time": now_dt.isoformat(),
            "exit_bid": exit_bid,
            "exit_ask": exit_ask,
            "exit_price_used": exit_px,
            "peak_price": pk,
            "trough_price": tr,
            "mfe_pct": mfe,
            "mae_pct": mae,
            "gross_pnl": gross,
            "charges": chg,
            "slippage": slip,
            "net_pnl": net,
            "net_pnl_pct": net_pct,
            "holding_duration_seconds": dur_s,
            "exit_reason": exit_reason,
        }
        if AdvanceDB.close_trade(tid, close_data):
            with self._op_lock:
                self._open_trade_runtime.pop(tid, None)
                self._book_cooldown_until[btype] = time.time() + S205_COOLDOWN_SECONDS
            AdvanceDB.log_event(
                "S205_EXIT",
                "INFO" if net >= 0 else "WARN",
                f"Closed {trade.get('strategy_mode')} {trade.get('trading_symbol')} ({exit_reason}) | Net ₹{net:+.2f}",
                close_data,
            )
            return close_data
        return None

    def evaluate_subsecond_tick_guard(
        self,
        ikey: str,
        quote_payload: Dict[str, Any],
        now_dt: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Sub-second WebSocket tick guard for open `V6B_*` positions:
          - Evaluates Hard Stop-Loss, previously armed Close-Only Ratchet Stop, Fixed TP (`S4_Z2`),
            `15:15 IST` EOD cutoff, and `-₹3,500` Portfolio MTM Daily Kill.
          - NEVER arms a new ratchet from intrabar highs/lows (preserves 100% close-only ratchet integrity).
        """
        closed_out: List[Dict[str, Any]] = []
        open_trades = [
            t for t in (AdvanceDB.get_open_trades() or [])
            if str(t.get("strategy_mode") or "").startswith("V6B_")
        ]
        if not open_trades:
            return closed_out

        curr_bid = float(quote_payload.get("bid") or quote_payload.get("ltp") or 0.0)
        curr_ask = float(quote_payload.get("ask") or quote_payload.get("ltp") or curr_bid)
        if curr_bid <= 0 or curr_ask <= 0:
            return closed_out

        now_dt = to_ist_naive(now_dt)
        today_s = now_dt.strftime("%Y-%m-%d")
        is_eod = now_dt.time() >= dt_time(15, 15)

        for trade in open_trades:
            if trade.get("instrument_key") != ikey:
                continue
            tid = str(trade["trade_id"])
            btype = str(trade.get("book_type") or BOOK_SHORT_SELL)
            qty = int(trade.get("quantity") or trade.get("lot_size") or 65)
            active_stop = float(trade.get("trailing_stop") or 0.0)
            tgt_pct = float(trade.get("target_pct") or 0.0)

            if btype == BOOK_SHORT_SELL:
                entry_px = float(trade["entry_bid"])
                hard_sl_px = round(entry_px * (1.0 + float(trade.get("stop_loss_pct") or 18.0) / 100.0), 2)
                eff_stop = active_stop if active_stop > 0 else hard_sl_px
                pk = max(float(trade.get("peak_price") or entry_px), curr_ask)
                tr = min(float(trade.get("trough_price") or entry_px), curr_ask)
                mfe = round(((entry_px - tr) / max(0.05, entry_px)) * 100.0, 2)
                mae = round(((entry_px - pk) / max(0.05, entry_px)) * 100.0, 2)

                if is_eod:
                    rsn = "EOD_SQUAREOFF_1515"
                elif curr_ask >= eff_stop:
                    rsn = "RATCHET_STOP_HIT" if eff_stop < hard_sl_px - 0.01 else "SHORT_HARD_SL_HIT"
                elif tgt_pct > 0 and curr_ask <= entry_px * (1.0 - tgt_pct / 100.0):
                    rsn = "SHORT_TAKE_PROFIT_HIT"
                else:
                    rsn = ""

                if rsn:
                    c_res = self._close_v6b_paper_trade(trade, curr_bid, curr_ask, rsn, now_dt)
                    if c_res:
                        closed_out.append(c_res)
                else:
                    est = calculate_short_option_trade_costs(sell_entry_bid=entry_px, buy_exit_ask=curr_ask, quantity=qty, trade_date=trade.get("entry_time"))
                    AdvanceDB.update_open_trade(tid, pk, tr, mfe, mae, eff_stop, est["gross_pnl"], est["net_pnl"], est["return_on_premium_pct"])
                    trade["net_pnl"] = est["net_pnl"]
            else:
                entry_px = float(trade["entry_ask"])
                hard_sl_px = round(entry_px * (1.0 - float(trade.get("stop_loss_pct") or 22.0) / 100.0), 2)
                eff_stop = active_stop if active_stop > 0 else hard_sl_px
                pk = max(float(trade.get("peak_price") or entry_px), curr_bid)
                tr = min(float(trade.get("trough_price") or entry_px), curr_bid)
                mfe = round(((pk - entry_px) / max(0.05, entry_px)) * 100.0, 2)
                mae = round(((tr - entry_px) / max(0.05, entry_px)) * 100.0, 2)

                if is_eod:
                    rsn = "EOD_SQUAREOFF_1515"
                elif curr_bid <= eff_stop:
                    rsn = "RATCHET_STOP_HIT" if eff_stop > hard_sl_px + 0.01 else "LONG_HARD_SL_HIT"
                else:
                    rsn = ""

                if rsn:
                    c_res = self._close_v6b_paper_trade(trade, curr_bid, curr_ask, rsn, now_dt)
                    if c_res:
                        closed_out.append(c_res)
                else:
                    costs = calculate_option_trade_costs(buy_price=entry_px, sell_price=curr_bid, quantity=qty, apply_slippage=True, trade_date=trade.get("entry_time"))
                    gross = round((curr_bid - entry_px) * qty, 2)
                    net = round(gross - costs["total_friction"], 2)
                    net_pct = round((net / max(1.0, entry_px * qty)) * 100.0, 2)
                    AdvanceDB.update_open_trade(tid, pk, tr, mfe, mae, eff_stop, gross, net, net_pct)
                    trade["net_pnl"] = net

        # Portfolio-wide sub-second -₹3,500 MTM Daily Kill check
        rem_open = [t for t in (AdvanceDB.get_open_trades() or []) if str(t.get("strategy_mode") or "").startswith("V6B_")]
        real_pnl, unreal_pnl = self._compute_today_session_pnl(today_s, rem_open)
        if (real_pnl + unreal_pnl) <= S205_DAILY_MTM_KILL_INR:
            with self._op_lock:
                self._daily_kill_triggered = True
                self._daily_kill_reason = f"Daily MTM Kill Breached (₹{real_pnl + unreal_pnl:,.2f} <= ₹{S205_DAILY_MTM_KILL_INR:,.0f})"
            sm = OptionStreamManager()
            for rem_t in rem_open:
                rq = sm.get_option_quote(str(rem_t.get("instrument_key") or "")) or quote_payload
                rb = float(rq.get("bid") or rq.get("ltp") or curr_bid)
                ra = float(rq.get("ask") or rq.get("ltp") or curr_ask)
                c_res = self._close_v6b_paper_trade(rem_t, rb, ra, "DAILY_MTM_KILL_FLATTEN", now_dt)
                if c_res:
                    closed_out.append(c_res)

        return closed_out

    # ──────────────────────────────────────────────────────────────────────────
    # COMPLETED 1M BAR (:00) EVALUATION, RATCHET UPDATE & PAPER EXECUTION
    # ──────────────────────────────────────────────────────────────────────────

    def _open_s205_paper_trade(
        self,
        cand: Dict[str, Any],
        quote: Dict[str, Any],
        spot_price: float,
        fut_quote: Optional[Dict[str, Any]],
        ctx: Dict[str, Any],
        bar_ts: str,
        now_dt: datetime,
    ) -> Optional[str]:
        btype = str(cand["book_type"])
        eso = cand["eso"]
        lot_sz = int(quote.get("lot_size") or DEFAULT_LOT_SIZES.get("NIFTY", 65))
        bid = float(quote.get("bid") or 0.0)
        ask = float(quote.get("ask") or 0.0)
        spread_pct = float(quote.get("spread_pct") or 999.0)
        if bid <= 0 or ask < bid or spread_pct > MAX_SPREAD_PCT:
            return None

        entry_px = bid if btype == BOOK_SHORT_SELL else ask
        raw_sl_pct = float(cand["sl_pct"])
        max_loss_inr = float(cand["max_loss_inr"])
        capped_sl_pct = round(min(raw_sl_pct, (max_loss_inr / max(1.0, entry_px * lot_sz)) * 100.0), 2)
        capped_sl_pct = max(6.0, capped_sl_pct)

        if btype == BOOK_SHORT_SELL:
            init_stop = round(entry_px * (1.0 + capped_sl_pct / 100.0), 2)
            direction = f"SELL_{eso[2]}"
            hedge_wing_strike = float(eso[1]) + (SHORT_AUTO_HEDGE_WING_PTS_NIFTY if eso[2] == "CE" else -SHORT_AUTO_HEDGE_WING_PTS_NIFTY)
            est_margin = 55000.0
        else:
            init_stop = round(entry_px * (1.0 - capped_sl_pct / 100.0), 2)
            direction = f"BUY_{eso[2]}"
            hedge_wing_strike = 0.0
            est_margin = round(entry_px * lot_sz, 2)

        sig_id = f"s205_sig_{uuid.uuid4().hex[:10]}"
        tid = f"adv_trd_{uuid.uuid4().hex[:10]}"
        ml_prob = float(cand.get("ml_prob") or self._meta_model.p_min)

        # Activate virtual once-per-day 450pt Catastrophe Wing telemetry on first Short of session
        with self._op_lock:
            if btype == BOOK_SHORT_SELL and self._catastrophe_wing_state.get("status") == "STANDBY":
                w_side = eso[2]
                w_key = self._catastrophe_wing_state.get("ce_wing_key" if w_side == "CE" else "pe_wing_key")
                wq = OptionStreamManager().get_option_quote(str(w_key or "")) if w_key else None
                w_ask = float((wq or {}).get("ask") or (wq or {}).get("ltp") or 2.5)
                self._catastrophe_wing_state.update(
                    {
                        "status": "ACTIVE_PROTECTING",
                        "activated_at": now_dt.isoformat(),
                        "activated_side": w_side,
                        "virtual_cost_inr": round(w_ask * lot_sz, 2),
                    }
                )

        regime_ctx = {
            "engine_version": "S2_05_UPSTOX_V3",
            "signal_bar_ts": bar_ts,
            "regime_tag": ctx.get("regime_tag"),
            "ml_prob": ml_prob,
            "ml_p_min": self._meta_model.p_min,
            "ml_components": cand.get("ml_explain"),
            "hedge_wing_strike": hedge_wing_strike,
            "hedge_wing_option_type": eso[2],
            "catastrophe_wing_offset_pts": S205_CATASTROPHE_WING_OFFSET_PTS,
            "fut_ltp": (fut_quote or {}).get("fut_ltp"),
            "fut_atp_vwap": (fut_quote or {}).get("fut_atp_vwap"),
            "l5_obi": quote.get("book_obi_l5", 0.0),
            "option_vwap_atp": quote.get("option_vwap_atp", 0.0),
            "ratchet_stages": cand["ratchet_stages"],
            "max_hold_bars": cand["max_hold_bars"],
            "time_stop_mins": cand["time_stop_mins"],
            "time_min_gain_pct": cand["time_min_gain_pct"],
            "opt_vwap_mult": cand["opt_vwap_mult"],
            "vflip_pts": cand["vflip_pts"],
            "is_bull": cand["is_bull"],
        }

        AdvanceDB.save_signal(
            {
                "signal_id": sig_id,
                "created_at": now_dt.isoformat(),
                "book_type": btype,
                "strategy_mode": cand["engine_id"],
                "direction": direction,
                "status": "TRIGGERED",
                "instrument_key": quote["instrument_key"],
                "trading_symbol": quote["trading_symbol"],
                "underlying": "NIFTY",
                "strike": float(eso[1]),
                "option_type": eso[2],
                "expiry": eso[0],
                "lot_size": lot_sz,
                "spot_price": spot_price,
                "option_bid": bid,
                "option_ask": ask,
                "spread_pct": spread_pct,
                "delta": quote.get("delta", 0.0),
                "gamma": quote.get("gamma", 0.0),
                "theta": quote.get("theta", 0.0),
                "vega": quote.get("vega", 0.0),
                "iv": quote.get("iv", 0.0),
                "oi": quote.get("oi", 0.0),
                "volume": quote.get("volume", 0.0),
                "adx_14": ctx["adx"][-1],
                "kaufman_er": ctx["er"][-1],
                "atr_ratio": ctx["atrr"][-1],
                "synthetic_basis_bps": round((ctx["syn_basis"][-1] / max(1.0, spot_price)) * 10000.0, 2),
                "calibrated_prob": ml_prob,
                "expected_net_return": round((ml_prob - 0.45) * 35.0, 2),
                "hurdles_passed": 1,
                "regime_telemetry": regime_ctx,
            }
        )

        saved = AdvanceDB.create_trade(
            {
                "trade_id": tid,
                "signal_id": sig_id,
                "book_type": btype,
                "strategy_mode": cand["engine_id"],
                "direction": direction,
                "instrument_key": quote["instrument_key"],
                "trading_symbol": quote["trading_symbol"],
                "underlying": "NIFTY",
                "option_type": eso[2],
                "strike": float(eso[1]),
                "expiry": eso[0],
                "lot_size": lot_sz,
                "quantity": lot_sz,
                "entry_time": now_dt.isoformat(),
                "entry_bid": bid,
                "entry_ask": ask,
                "entry_price_used": entry_px,
                "entry_spot_price": spot_price,
                "peak_price": entry_px,
                "trough_price": entry_px,
                "trailing_stop": init_stop,
                "stop_loss_pct": capped_sl_pct,
                "target_pct": float(cand["tp_pct"] or 0.0),
                "estimated_margin": est_margin,
                "regime_context": regime_ctx,
            }
        )
        if not saved:
            return None

        with self._op_lock:
            self._open_trade_runtime[tid] = {
                "best_close_pts": entry_px,
                "ratchet_stop": None,
                "hard_stop": init_stop,
                "entry_bar_ts": bar_ts,
                "spec": cand,
            }
        AdvanceDB.log_event(
            "S205_ENTRY",
            "INFO",
            f"Opened 1-Lot {cand['engine_id']} ({direction}) {quote['trading_symbol']} @ ₹{entry_px:.2f} (ML={ml_prob:.2f})",
            regime_ctx,
        )
        return tid

    def run_surveillance_sweep(self, now_dt: Optional[datetime] = None, allow_network: bool = True) -> Dict[str, Any]:
        """
        Executes a live surveillance & readiness pass:
          1. Refreshes Chain Intelligence for NIFTY & BANKNIFTY.
          2. Evaluates completed 1-minute bar `:00` close-only ratchets, bar-close invalidations,
             and new `S2_05` (`V6B`) 1-lot entries if a new 1m candle has closed.
          3. Updates real-time `engine_readiness` checklist for the frontend.
        """
        t0 = time.perf_counter()
        now_dt = to_ist_naive(now_dt)
        today_s = now_dt.strftime("%Y-%m-%d")
        self._ensure_prior_day_and_intraday_backfill(now_dt, allow_network=allow_network)

        sm = OptionStreamManager()
        all_quotes = sm.get_all_active_option_quotes()
        fut_nifty = sm.get_futures_quote("NIFTY")
        fut_bnf = sm.get_futures_quote("BANKNIFTY")

        # 1. Refresh NIFTY & BANKNIFTY Chain Intelligence (preserves 100% of existing frontend cards)
        for u_sym, fq in (("NIFTY", fut_nifty), ("BANKNIFTY", fut_bnf)):
            uq = sm.get_underlying_quote(u_sym) or {}
            u_opts = [q for q in all_quotes if str(q.get("underlying") or "").upper() == u_sym]
            snap = AdvanceChainIntelligence.compute_chain_intelligence(
                underlying=u_sym,
                option_quotes=u_opts if u_opts else None,
                underlying_quote=uq,
                as_of_time=now_dt,
            )
            snap["spot_ltp"] = snap.get("spot_price", 0.0)
            snap["net_gex_cr"] = snap.get("net_gex", 0.0)
            snap["rv_15m"] = snap.get("rv_15m_ann", 0.0)
            snap["pcr_vol"] = snap.get("pcr_volume", 1.0)
            snap["atr_ratio_5_20"] = snap.get("atr_ratio", 1.0)
            snap["recommended_strategy"] = snap.get("active_strategy_mode", "S2_05_DUAL_BOOK_SURVEILLANCE")
            snap["regime_reason"] = snap.get("regime_summary", "")
            snap["recommended_book"] = "DUAL_BOOK_READY"
            if fq:
                snap["futures_ltp"] = fq.get("fut_ltp")
                snap["futures_atp_vwap"] = fq.get("fut_atp_vwap")
                snap["futures_oi"] = fq.get("fut_oi")
                snap["futures_obi"] = fq.get("fut_obi")
            self._last_chain_intel[u_sym] = snap

        # 2. Build causal intraday context up to last completed 1m bar (`ts < current minute`)
        cur_min_ts = now_dt.strftime("%Y-%m-%d %H:%M:00")
        exps = OptionContractDiscovery.get_available_expiries("NIFTY", force_refresh=False) if allow_network else []
        if not exps:
            exps = sorted({str(q.get("expiry") or "")[:10] for q in all_quotes if str(q.get("underlying") or "").upper() == "NIFTY" and q.get("expiry")})
        front_exp = exps[0] if exps else ""
        next_exp = exps[1] if len(exps) > 1 else front_exp

        with self._op_lock:
            completed_spot = {k: v for k, v in self._spot_bars.items() if k < cur_min_ts}
            eval_spot = completed_spot if completed_spot else dict(self._spot_bars)
            vix_copy = dict(self._vix_bars)
            hw_copy = {k: list(v) for k, v in self._hw_15m_bars.items()}
            opt_copy = {eso: dict(bm) for eso, bm in self._opt_bars.items()}
            pd_copy = dict(self._prior_day_stats)

        # Also seed `opt_copy` at latest bar from live quotes if any strike had no tick in `self._opt_bars` yet
        quote_lookup: Dict[Tuple[str, float, str], Dict[str, Any]] = {}
        latest_bar_key = max(eval_spot.keys()) if eval_spot else cur_min_ts
        for q in all_quotes:
            if str(q.get("underlying") or "").upper() != "NIFTY":
                continue
            eso_q = (str(q.get("expiry") or "")[:10], float(q.get("strike") or 0.0), str(q.get("option_type") or "").upper())
            quote_lookup[eso_q] = q
            if latest_bar_key and float(q.get("ltp") or 0.0) > 0:
                bm = opt_copy.setdefault(eso_q, {})
                if latest_bar_key not in bm:
                    px_q = float(q["ltp"])
                    bm[latest_bar_key] = (px_q, px_q, px_q, px_q, max(1.0, float(q.get("volume") or 1.0)), float(q.get("oi") or 0.0))

        ctx = build_causal_intraday_context("NIFTY" if False else today_s, eval_spot, vix_copy, hw_copy, opt_copy, front_exp, next_exp, pd_copy)

        # Determine session risk & cooldown block status
        open_all = [t for t in (AdvanceDB.get_open_trades() or []) if str(t.get("strategy_mode") or "").startswith("V6B_")]
        closed_today = [t for t in (AdvanceDB.get_closed_trades(limit=100) or []) if str(t.get("entry_time") or "")[:10] == today_s and str(t.get("strategy_mode") or "").startswith("V6B_")]
        trades_today_cnt = len(open_all) + len(closed_today)
        real_pnl, unreal_pnl = self._compute_today_session_pnl(today_s, open_all)
        if (real_pnl + unreal_pnl) <= S205_DAILY_MTM_KILL_INR:
            self._daily_kill_triggered = True
            self._daily_kill_reason = f"Daily MTM Kill Active (₹{real_pnl + unreal_pnl:,.0f} <= -₹3,500)"

        block_reason: Optional[str] = None
        if self._daily_kill_triggered:
            block_reason = self._daily_kill_reason
        elif trades_today_cnt >= S205_MAX_TOTAL_TRADES_DAY:
            block_reason = f"Daily 3-trade session cap reached ({trades_today_cnt}/{S205_MAX_TOTAL_TRADES_DAY})."
        elif len(open_all) >= S205_MAX_TOTAL_OPEN:
            block_reason = f"Max concurrent open cap reached ({len(open_all)}/{S205_MAX_TOTAL_OPEN})."

        ist_hhmm = now_dt.hour * 100 + now_dt.minute
        bar_idx = (len(ctx["ts"]) - 1) if ctx else -1
        exec_cands, readiness_rows = evaluate_s205_bar_and_readiness(
            ctx, bar_idx, self._meta_model, quote_lookup, fut_nifty, lot_size=65, ist_hhmm=ist_hhmm, session_blocked_reason=block_reason
        )

        opened_ids: List[str] = []
        closed_on_bar: List[Dict[str, Any]] = []

        # 3. Execute ONLY once per newly completed 1m candle (`completed_spot` non-empty & new `bar_ts`)
        if ctx is not None and completed_spot and bar_idx >= 0:
            bar_ts = ctx["ts"][bar_idx]
            is_new_completed_bar = bar_ts > self._last_evaluated_bar_ts
            if is_new_completed_bar:
                self._last_evaluated_bar_ts = bar_ts

                # 3A. Manage open `V6B_*` positions at `:00` bar close (invalidation + close-only ratchet update!)
                for tr in list(open_all):
                    tid = str(tr["trade_id"])
                    eso_t = (str(tr["expiry"])[:10], float(tr["strike"]), str(tr["option_type"]).upper())
                    q_t = quote_lookup.get(eso_t) or sm.get_option_quote(str(tr["instrument_key"])) or {}
                    cbid = float(q_t.get("bid") or q_t.get("ltp") or tr.get("entry_bid") or 0.0)
                    cask = float(q_t.get("ask") or q_t.get("ltp") or tr.get("entry_ask") or 0.0)
                    ob_t = ctx["day_eso"].get(eso_t, {}).get(bar_ts)
                    close_pts = float(ob_t[3]) if ob_t else (cask if tr["book_type"] == BOOK_SHORT_SELL else cbid)

                    rctx = {}
                    try:
                        rctx = json.loads(str(tr.get("regime_context_json") or "{}") or "{}")
                    except Exception:
                        pass

                    try:
                        ent_dt = datetime.fromisoformat(str(tr["entry_time"]).replace("T", " ")[:19])
                        held_mins = max(0, int((now_dt - ent_dt).total_seconds() // 60))
                    except Exception:
                        held_mins = 0

                    is_cred = tr["book_type"] == BOOK_SHORT_SELL
                    ent_px = float(tr["entry_bid"] if is_cred else tr["entry_ask"])
                    fav_now = (((ent_px - close_pts) if is_cred else (close_pts - ent_px)) / max(0.05, ent_px)) * 100.0

                    inv_reason = ""
                    ts_m = int(rctx.get("time_stop_mins") or 0)
                    if ist_hhmm >= 1515:
                        inv_reason = "EOD_SQUAREOFF_1515"
                    elif held_mins >= int(rctx.get("max_hold_bars") or 120):
                        inv_reason = "MAX_HOLD_TIME_EXIT"
                    elif ts_m > 0 and held_mins >= ts_m and fav_now < float(rctx.get("time_min_gain_pct") or 3.0):
                        inv_reason = "STAGNATION_TIME_STOP"
                    elif is_cred and float(rctx.get("opt_vwap_mult") or 0.0) > 0 and held_mins >= 5:
                        ost_t = ctx["opt_stats"].get(eso_t)
                        if ost_t and ost_t[0][bar_idx] > 0 and close_pts >= ost_t[0][bar_idx] * float(rctx["opt_vwap_mult"]):
                            inv_reason = "OPT_VWAP_RECLAIM_EXIT"
                    elif float(rctx.get("vflip_pts") or 0.0) > 0 and held_mins >= 4 and bar_idx >= 1:
                        vf = float(rctx["vflip_pts"])
                        b_dir = bool(rctx.get("is_bull", True))
                        c0, c1 = ctx["c"][bar_idx], ctx["c"][bar_idx - 1]
                        t0_v, t1_v = ctx["tpb"][bar_idx], ctx["tpb"][bar_idx - 1]
                        if (b_dir and c0 < t0_v - vf and c1 < t1_v - vf) or ((not b_dir) and c0 > t0_v + vf and c1 > t1_v + vf):
                            inv_reason = "SPOT_VWAP_FLIP_EXIT"

                    if inv_reason and cbid > 0 and cask > 0:
                        c_info = self._close_v6b_paper_trade(tr, cbid, cask, inv_reason, now_dt)
                        if c_info:
                            closed_on_bar.append(c_info)
                        continue

                    # Strictly Close-Only Ratchet update!
                    rt_state = self._open_trade_runtime.setdefault(
                        tid,
                        {"best_close_pts": ent_px, "ratchet_stop": None, "hard_stop": float(tr.get("trailing_stop") or 0.0)},
                    )
                    if is_cred:
                        rt_state["best_close_pts"] = min(float(rt_state["best_close_pts"]), close_pts)
                    else:
                        rt_state["best_close_pts"] = max(float(rt_state["best_close_pts"]), close_pts)

                    stages = [tuple(s) for s in (rctx.get("ratchet_stages") or [])]
                    if stages:
                        new_r = compute_close_only_ratchet(is_cred, ent_px, float(rt_state["best_close_pts"]), rt_state.get("ratchet_stop"), stages)
                        rt_state["ratchet_stop"] = new_r
                        if new_r is not None:
                            cur_s = float(tr.get("trailing_stop") or new_r)
                            tight_s = min(cur_s, new_r) if is_cred else max(cur_s, new_r)
                            AdvanceDB.update_open_trade(
                                tid,
                                float(tr.get("peak_price") or ent_px),
                                float(tr.get("trough_price") or ent_px),
                                float(tr.get("mfe_pct") or 0.0),
                                float(tr.get("mae_pct") or 0.0),
                                tight_s,
                                float(tr.get("gross_pnl") or 0.0),
                                float(tr.get("net_pnl") or 0.0),
                                float(tr.get("net_pnl_pct") or 0.0),
                            )

                # 3B. Open new `:00` verified S2_05 entries (respecting book limits & 15m cooldown)
                if not block_reason and 945 <= ist_hhmm <= 1345:
                    now_ep = time.time()
                    open_now = [t for t in (AdvanceDB.get_open_trades() or []) if str(t.get("strategy_mode") or "").startswith("V6B_")]
                    for cand in exec_cands:
                        btype = cand["book_type"]
                        max_b_open, max_b_day = S205_BOOK_LIMITS.get(btype, (1, 1))
                        b_open_cnt = sum(1 for t in open_now if t.get("book_type") == btype)
                        b_day_cnt = b_open_cnt + sum(1 for t in closed_today if t.get("book_type") == btype)
                        if len(open_now) + len(opened_ids) >= S205_MAX_TOTAL_OPEN:
                            break
                        if trades_today_cnt + len(opened_ids) >= S205_MAX_TOTAL_TRADES_DAY:
                            break
                        if b_open_cnt >= max_b_open or b_day_cnt >= max_b_day:
                            continue
                        if now_ep < self._book_cooldown_until.get(btype, 0.0):
                            continue
                        q_cand = quote_lookup.get(cand["eso"])
                        if not q_cand or not q_cand.get("is_valid", True):
                            continue
                        new_tid = self._open_s205_paper_trade(cand, q_cand, ctx["c"][bar_idx], fut_nifty, ctx, bar_ts, now_dt)
                        if new_tid:
                            opened_ids.append(new_tid)

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        with self._op_lock:
            self._sweep_count += 1
            self._last_eval_latency_ms = elapsed_ms
            self._last_sweep_time = now_dt.isoformat()
            self._latest_engine_readiness = readiness_rows
            if ctx is not None and bar_idx >= 0:
                self._latest_context_summary = {
                    "bar_ts": ctx["ts"][bar_idx],
                    "bars_count": len(ctx["ts"]),
                    "regime_tag": ctx["regime_tag"],
                    "spot_close": round(ctx["c"][bar_idx], 2),
                    "spot_tpb_vwap": round(ctx["tpb"][bar_idx], 2),
                    "orb30_high": round(ctx["orb30_h"], 2),
                    "orb30_low": round(ctx["orb30_l"], 2),
                    "adx14": round(ctx["adx"][bar_idx], 2),
                    "er20": round(ctx["er"][bar_idx], 3),
                    "rsi14": round(ctx["rsi14"][bar_idx], 1),
                    "vwap_z": round(ctx["vwap_z"][bar_idx], 2),
                    "syn_basis_pts": round(ctx["syn_basis"][bar_idx], 2),
                    "oi_bull_diff": round(ctx["oi_bull_diff"][bar_idx], 3),
                    "heavyweight_15m": round(ctx["hw"][bar_idx], 2),
                    "ivp_20d": ctx["ivp_20d"],
                    "front_exp": ctx["front_exp"],
                    "front_dte": ctx["front_dte"],
                }

        return {
            "status": "OK",
            "engine_version": "S2_05_UPSTOX_V3",
            "evaluated_bar_ts": self._last_evaluated_bar_ts,
            "opened_trades": opened_ids,
            "closed_trades": len(closed_on_bar),
            "duration_ms": elapsed_ms,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # BACKGROUND WORKER LOOP & FRONTEND READINESS TELEMETRY
    # ──────────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._op_lock:
            if self._is_running and self._worker_thread and self._worker_thread.is_alive():
                return
            self._is_running = True
            self._worker_thread = threading.Thread(target=self._worker_loop, name="S205UpstoxWorker", daemon=True)
            self._worker_thread.start()
            logger.info("[S205Engine] Started event-driven :00 candle-close worker thread.")

    def stop(self) -> None:
        with self._op_lock:
            self._is_running = False
            self._bar_wake_event.set()

    def _worker_loop(self) -> None:
        while self._is_running:
            try:
                now_dt = to_ist_naive()
                if dt_time(9, 14) <= now_dt.time() <= dt_time(15, 20):
                    self.run_surveillance_sweep(now_dt=now_dt, allow_network=True)
                    wait_s = max(0.25, min(2.0, 60.05 - (now_dt.second + now_dt.microsecond / 1e6)))
                else:
                    if self._sweep_count == 0:
                        self.run_surveillance_sweep(now_dt=now_dt, allow_network=True)
                    wait_s = 15.0
                self._bar_wake_event.wait(timeout=wait_s)
                self._bar_wake_event.clear()
            except Exception as e:
                logger.debug(f"[S205Engine] Worker loop note: {e}")
                time.sleep(2.0)

    def get_backend_readiness(self, now_dt: Optional[datetime] = None) -> Dict[str, Any]:
        """Returns clean, zero-clutter real-time backend & order trigger readiness for the UI."""
        now_dt = to_ist_naive(now_dt)
        today_s = now_dt.strftime("%Y-%m-%d")
        hhmm = now_dt.hour * 100 + now_dt.minute
        sec_to_next_bar = max(0, 60 - now_dt.second) if 915 <= hhmm < 1515 else 0

        if not self._latest_engine_readiness:
            try:
                self.run_surveillance_sweep(now_dt=now_dt, allow_network=False)
            except Exception:
                pass

        if self._daily_kill_triggered:
            phase, phase_label = "HALTED_DAILY_KILL", "DAILY -₹3,500 MTM KILL ACTIVE (FLATTENED)"
        elif hhmm < 915:
            phase, phase_label = "PRE_MARKET", "PRE-MARKET STANDBY (NSE OPENS 09:15 IST)"
        elif hhmm < 945:
            phase, phase_label = "ORB30_WARMUP", "ORB-30 WARMUP (09:15–09:45 — ENTRIES OPEN AT 09:45)"
        elif hhmm <= 1245:
            phase, phase_label = "ACTIVE_DUAL_WINDOW", "ACTIVE ENTRY WINDOW (LONG + SHORT BOOKS ARMED)"
        elif hhmm <= 1345:
            phase, phase_label = "ACTIVE_SHORT_WINDOW", "ACTIVE SHORT WINDOW (LONG CLOSED 12:45; SHORT OPEN TO 13:45)"
        elif hhmm < 1515:
            phase, phase_label = "HARVEST_TRAIL_ONLY", "TRAIL & HARVEST ONLY (NO NEW ENTRIES; EOD AT 15:15)"
        else:
            phase, phase_label = "EOD_CLOSED", "SESSION CLOSED (15:15 EOD SQUARE-OFF COMPLETE)"

        sm = OptionStreamManager()
        uq = sm.get_underlying_quote("NIFTY") or {}
        vq = sm.get_underlying_quote("INDIA_VIX") or sm.get_underlying_quote("INDIAVIX") or {}
        fq = sm.get_futures_quote("NIFTY") or {}
        nifty_opts = [q for q in sm.get_all_active_option_quotes() if str(q.get("underlying") or "").upper() == "NIFTY"]

        spot_ltp = float(uq.get("spot_ltp") or self._latest_context_summary.get("spot_close") or 0.0)
        fut_ltp = float(fq.get("fut_ltp") or 0.0)
        fut_atp = float(fq.get("fut_atp_vwap") or 0.0)
        real_basis = round(fut_ltp - spot_ltp, 2) if (fut_ltp > 0 and spot_ltp > 0) else float(self._latest_context_summary.get("syn_basis_pts") or 0.0)
        avg_sp = round(sum(float(q.get("spread_pct") or 0.0) for q in nifty_opts) / len(nifty_opts), 2) if nifty_opts else 0.0
        avg_obi = round(sum(float(q.get("book_obi_l5") or 0.0) for q in nifty_opts) / len(nifty_opts), 3) if nifty_opts else 0.0

        open_t = AdvanceDB.get_open_trades() or []
        closed_today = [t for t in (AdvanceDB.get_closed_trades(limit=100) or []) if str(t.get("entry_time") or "")[:10] == today_s]
        real_pnl, unreal_pnl = self._compute_today_session_pnl(today_s, open_t)
        net_mtm = round(real_pnl + unreal_pnl, 2)

        now_ep = time.time()
        cd_rem = int(max(0.0, max(self._book_cooldown_until.values()) - now_ep)) if self._book_cooldown_until else 0

        # Refresh live wing ask prices for UI display
        wing_info = dict(self._catastrophe_wing_state)
        for wk, field_n in (("ce_wing_key", "ce_wing_ask"), ("pe_wing_key", "pe_wing_ask")):
            ikey_w = wing_info.get(wk)
            if ikey_w:
                wq = sm.get_option_quote(str(ikey_w))
                if wq and float(wq.get("ask") or wq.get("ltp") or 0.0) > 0:
                    wing_info[field_n] = float(wq.get("ask") or wq.get("ltp"))

        return {
            "engine_version": "S2_05_UPSTOX_V3",
            "simulation_only": True,
            "meta_model": {
                "candidate_id": self._meta_model.candidate_id,
                "fold_id": self._meta_model.fold_id,
                "test_month": self._meta_model.test_month,
                "embargoed_train_end": self._meta_model.embargoed_train_end,
                "p_min": self._meta_model.p_min,
                "chosen_params": self._meta_model.chosen_params,
                "weights_sha256": self._meta_model.weights_sha256[:16],
            },
            "timing": {
                "ist_now": now_dt.strftime("%H:%M:%S"),
                "session_phase": phase,
                "session_phase_label": phase_label,
                "seconds_to_next_bar_eval": sec_to_next_bar,
                "last_bar_evaluated_ts": self._last_evaluated_bar_ts or "—",
                "last_eval_latency_ms": self._last_eval_latency_ms,
                "bars_collected_today": len(self._spot_bars),
                "cooldown_remaining_sec": cd_rem,
            },
            "session_risk": {
                "trades_today": len(open_t) + len(closed_today),
                "max_trades_day": S205_MAX_TOTAL_TRADES_DAY,
                "open_positions": len(open_t),
                "max_open_positions": S205_MAX_TOTAL_OPEN,
                "long_slots_used": sum(1 for t in open_t + closed_today if t.get("book_type") == BOOK_LONG_BUY),
                "short_slots_used": sum(1 for t in open_t + closed_today if t.get("book_type") == BOOK_SHORT_SELL),
                "realized_pnl_today": real_pnl,
                "unrealized_mtm_today": unreal_pnl,
                "net_session_mtm_today": net_mtm,
                "daily_kill_limit_inr": S205_DAILY_MTM_KILL_INR,
                "distance_to_kill_inr": round(net_mtm - S205_DAILY_MTM_KILL_INR, 2),
                "daily_kill_triggered": self._daily_kill_triggered,
                "catastrophe_wing": wing_info,
            },
            "upstox_v3_feed": {
                "websocket_connected": bool(sm.get_health_status().get("websocket_connected")),
                "spot_ltp": spot_ltp,
                "spot_tpb_vwap": self._latest_context_summary.get("spot_tpb_vwap", 0.0),
                "fut_symbol": fq.get("trading_symbol") or "NIFTY FRONT FUT",
                "fut_ltp": fut_ltp,
                "fut_atp_vwap": fut_atp,
                "real_basis_pts": real_basis,
                "basis_source": "UPSTOX_V3_FUTURES" if fut_ltp > 0 else "PUT_CALL_PARITY",
                "fut_oi": fq.get("fut_oi", 0.0),
                "fut_oi_chg_pct": fq.get("fut_oi_chg_pct", 0.0),
                "fut_obi": fq.get("fut_obi", 0.0),
                "india_vix": float(vq.get("spot_ltp") or 0.0),
                "avg_opt_spread_pct": avg_sp,
                "avg_l5_obi": avg_obi,
                "active_nifty_contracts": len(nifty_opts),
                "indicators": self._latest_context_summary,
            },
            "engine_readiness": list(self._latest_engine_readiness),
        }

    @staticmethod
    def _flatten_pnl_summary(raw_pnl: Dict[str, Any]) -> Dict[str, Any]:
        comb = raw_pnl.get("combined", {})
        lb = raw_pnl.get("long_book", {})
        sb = raw_pnl.get("short_book", {})
        return {
            **raw_pnl,
            "total_trades": comb.get("total_closed_trades", 0),
            "wins": comb.get("wins", 0),
            "losses": comb.get("losses", 0),
            "win_rate": comb.get("win_rate_pct", 0.0),
            "profit_factor": comb.get("profit_factor", 0.0),
            "total_net_pnl": comb.get("total_net_pnl", 0.0),
            "long_book": {
                **lb,
                "trades": lb.get("total_closed_trades", 0),
                "win_rate": lb.get("win_rate_pct", 0.0),
                "net_pnl": lb.get("total_net_pnl", 0.0),
            },
            "short_book": {
                **sb,
                "trades": sb.get("total_closed_trades", 0),
                "win_rate": sb.get("win_rate_pct", 0.0),
                "net_pnl": sb.get("total_net_pnl", 0.0),
            },
        }

    def get_status(self) -> Dict[str, Any]:
        now_dt = to_ist_naive()
        if not self._last_chain_intel:
            try:
                self.run_surveillance_sweep(now_dt=now_dt, allow_network=False)
            except Exception:
                pass
        disk_h = _compute_s205_hashes()
        in_sync = disk_h == _BOOT_S205_HASHES
        pnl_summary = self._flatten_pnl_summary(AdvanceDB.get_dual_book_pnl_summary())
        return {
            "service": "OPTION_SNIFFER_ADVANCE",
            "version": "S2_05_UPSTOX_V3",
            "engine": "Option Sniffer Advance — S2_05 (V6B + Causal Conformal Meta-Learner + Upstox V3)",
            "engine_version": "S2_05_UPSTOX_V3",
            "simulation_only": True,
            "mode": "PAPER_SANDBOX_DUAL_BOOK",
            "is_running": self._is_running,
            "is_trading_window": dt_time(9, 15) <= now_dt.time() <= dt_time(15, 15) and now_dt.weekday() < 5,
            "cutoff_time": "15:15 IST",
            "total_sweeps": self._sweep_count,
            "sweep_count": self._sweep_count,
            "last_sweep_time": self._last_sweep_time,
            "last_sweep_duration_ms": self._last_eval_latency_ms,
            "runtime_parity": {
                "status": "SYNCHRONIZED" if in_sync else "DESYNCHRONIZED_STALE_RAM",
                "in_sync": in_sync,
                "stale_files": [k for k, v in _BOOT_S205_HASHES.items() if disk_h.get(k) != v],
                "boot_hashes": _BOOT_S205_HASHES,
                "disk_hashes": disk_h,
            },
            "stream_health": OptionStreamManager().get_health_status(),
            "chain_intelligence": self._last_chain_intel,
            "pnl_summary": pnl_summary,
            "circuit_breaker": AdvanceDB.get_circuit_breaker_telemetry(as_of_time=now_dt),
            "strategy_architecture": {
                "long_book": {
                    "book_type": BOOK_LONG_BUY,
                    "lots": 1,
                    "strategy": "V6B_LBASIS_ATM (ORB-30 + Basis >= +6pts + ADX14 >= 22 + Conformal Meta >= 0.56)",
                    "strike_zone": "ATM Next/Front NIFTY (09:45–12:45 IST, Max 1/day)",
                    "max_loss_cap": "-22% / ₹2,200 + Sub-Second -₹3,500 Portfolio MTM Kill",
                    "trailing_engine": "Strictly Close-Only 5-Stage Ratchet (+10% -> +2% Floor up to +65% -> +48% Floor)",
                },
                "short_book": {
                    "book_type": BOOK_SHORT_SELL,
                    "lots": 1,
                    "strategies": [
                        "V6B_S1_OI (Institutional OI Writer + OptVWAP <= 0.94 + Conformal Meta >= 0.56)",
                        "V6B_S3_SPIKE (09:15 Spike Exhaustion Trap + Opt <= 0.86*Peak + Conformal Meta >= 0.56)",
                        "V6B_S4_Z2 (2.1σ VWAP Exhaustion Mean-Reversion + Fixed +20% Decay TP)",
                    ],
                    "max_loss_cap": "+14%..+20% / ₹1,800..₹2,100 + Daily ±450pt Catastrophe Wing",
                    "trailing_engine": "Strictly Close-Only Decay Ratchet (Updated Exclusively on :00 Completed 1m Candle Close)",
                },
            },
            "backend_readiness": self.get_backend_readiness(now_dt=now_dt),
        }

    def get_chain_intelligence(self) -> Dict[str, Any]:
        if not self._last_chain_intel:
            self.run_surveillance_sweep(allow_network=False)
        return {"underlyings": self._last_chain_intel, "timestamp": to_ist_naive().isoformat()}

    def get_signals(self, limit: int = 50, book_type: Optional[str] = None) -> List[Dict[str, Any]]:
        return AdvanceDB.get_recent_signals(limit=limit, book_type=book_type)

    def get_trades(self) -> Dict[str, Any]:
        open_raw = AdvanceDB.get_open_trades() or []
        now_dt = to_ist_naive()
        sm = OptionStreamManager()
        decorated_open: List[Dict[str, Any]] = []

        for trade in open_raw:
            t = dict(trade)
            btype = str(t.get("book_type") or BOOK_LONG_BUY)
            ikey = t.get("instrument_key")
            quote = sm.get_option_quote(str(ikey)) if ikey else None
            entry_price = float(t.get("entry_price_used") or t.get("entry_ask") or t.get("entry_bid") or 0.0)
            quantity = int(t.get("quantity") or 65)

            if quote and float(quote.get("bid") or 0.0) > 0:
                current_bid = float(quote.get("bid"))
                current_ask = float(quote.get("ask") or current_bid)
                current_ltp = float(quote.get("ltp") or current_bid)
            else:
                current_bid = entry_price
                current_ask = entry_price
                current_ltp = entry_price

            try:
                entry_dt = datetime.fromisoformat(str(t["entry_time"]).replace("T", " ")[:19])
                duration_sec = max(0, int((now_dt - entry_dt).total_seconds()))
            except Exception:
                duration_sec = int(t.get("holding_duration_seconds") or 0)

            sl_pct = float(t.get("stop_loss_pct") or (18.0 if btype == BOOK_SHORT_SELL else 22.0))
            if btype == BOOK_LONG_BUY:
                peak_price = max(float(t.get("peak_price") or entry_price), current_bid)
                trough_price = min(float(t.get("trough_price") or entry_price), current_bid)
                mfe_pct = round(((peak_price - entry_price) / max(0.05, entry_price)) * 100.0, 2)
                mae_pct = round(((trough_price - entry_price) / max(0.05, entry_price)) * 100.0, 2)
                stop_price = round(entry_price * (1.0 - sl_pct / 100.0), 2)
                active_trail = float(t.get("trailing_stop") or 0.0)
                effective_stop = max(stop_price, active_trail) if active_trail > 0 else stop_price
                cost_info = calculate_option_trade_costs(
                    buy_price=entry_price, sell_price=current_bid, quantity=quantity, apply_slippage=True
                )
                unrealized_gross = round((current_bid - entry_price) * quantity, 2)
                unrealized_net = round(unrealized_gross - cost_info["total_friction"], 2)
                unrealized_pct = round(((current_bid - entry_price) / max(0.05, entry_price)) * 100.0, 2)
            else:
                peak_price = max(float(t.get("peak_price") or entry_price), current_ask)
                trough_price = min(float(t.get("trough_price") or entry_price), current_ask)
                mfe_pct = round(((entry_price - trough_price) / max(0.05, entry_price)) * 100.0, 2)
                mae_pct = round(((entry_price - peak_price) / max(0.05, entry_price)) * 100.0, 2)
                stop_price = round(entry_price * (1.0 + sl_pct / 100.0), 2)
                active_trail = float(t.get("trailing_stop") or 0.0)
                effective_stop = min(stop_price, active_trail) if active_trail > 0 else stop_price
                cost_info = calculate_short_option_trade_costs(
                    sell_entry_bid=entry_price, buy_exit_ask=current_ask, quantity=quantity
                )
                unrealized_gross = cost_info["gross_pnl"]
                unrealized_net = cost_info["net_pnl"]
                unrealized_pct = cost_info["return_on_premium_pct"]

            t.update(
                {
                    "entry_price": entry_price,
                    "strike_price": t.get("strike"),
                    "current_bid": current_bid,
                    "current_ask": current_ask,
                    "current_ltp": current_ltp,
                    "mfe_pct": mfe_pct,
                    "mae_pct": mae_pct,
                    "holding_duration_seconds": duration_sec,
                    "stop_loss_price": stop_price,
                    "effective_stop_price": effective_stop,
                    "unrealized_gross_pnl": unrealized_gross,
                    "unrealized_charges": round(cost_info["total_friction"], 2),
                    "unrealized_net_pnl": unrealized_net,
                    "unrealized_pnl_pct": unrealized_pct,
                }
            )
            decorated_open.append(t)

        closed_raw = AdvanceDB.get_closed_trades(limit=200) or []
        for c in closed_raw:
            c["entry_price"] = c.get("entry_price_used")
            c["exit_price"] = c.get("exit_price_used")
            c["strike_price"] = c.get("strike")

        return {
            "open_trades": decorated_open,
            "closed_trades": closed_raw,
            "pnl_summary": self._flatten_pnl_summary(AdvanceDB.get_dual_book_pnl_summary()),
            "circuit_breaker": AdvanceDB.get_circuit_breaker_telemetry(as_of_time=now_dt),
        }


_BOOT_S205_HASHES = _compute_s205_hashes()


def get_s205_orchestrator_if_loaded() -> Optional[S205UpstoxOrchestrator]:
    return S205UpstoxOrchestrator._instance


def get_s205_orchestrator() -> S205UpstoxOrchestrator:
    return S205UpstoxOrchestrator()
