"""
V2 Option Sniffer — Unified Event-Time Bar, Indicator, Risk Checkpoint & Decision Engine.

Guarantees identical deterministic behavior across BOTH `FORWARD_PAPER` and `REPLAY_V2_1_CAUSAL`:
  - Uses ONE normalized event model (`availability_ts_ms`, `exchange_ltt_ms`, `receipt_ts_ms`).
  - Finalizes each `[t, t+60s)` completed bar BEFORE applying any tick belonging to `t+60s` or later.
  - Enforces exact NIFTY underlying & contract identity on Spot, Futures, and Option lookup.
  - Computes causal Wilder `ADX14` (`>=28` bars), Kaufman `ER20` (`>=21` bars), `TPB VWAP` (`>=3` bars),
    and gap-free `ORB30` using the repository's canonical functions from `s205_meta_model.py`.
  - Persists completed bars, runtime risk checkpoints, equity curve points, and daily session states
    so crash/restart recovery reproduces interrupted execution state cleanly without fresh risk budgets.
"""

from __future__ import annotations

from datetime import datetime
import importlib.util
import math
import os
import statistics
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# Load pure indicator functions from `s205_meta_model.py` without evaluating
# `option_sniffer_advance_s205/__init__.py` (which pulls live Upstox engine dependencies).
_S205_META_MODEL_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "option_sniffer_advance_s205",
        "s205_meta_model.py",
    )
)
_s205_mm_spec = importlib.util.spec_from_file_location("_osv2_ref_s205_meta_model", _S205_META_MODEL_PATH)
assert _s205_mm_spec is not None and _s205_mm_spec.loader is not None
_s205_mm_mod = importlib.util.module_from_spec(_s205_mm_spec)
_s205_mm_spec.loader.exec_module(_s205_mm_mod)
compute_adx_series_causal = _s205_mm_mod.compute_adx_series_causal
compute_er_series_causal = _s205_mm_mod.compute_er_series_causal
compute_atrr_series_causal = _s205_mm_mod.compute_atrr_series_causal
from app.analytics.option_sniffer_v2.v2_book_a_engine import (
    V2_BOOK_A_INTRADAY_CREDIT_SPEC,
    evaluate_book_a_entry_signal,
    evaluate_book_a_intraday_credit_signal,
    manage_open_book_a_intraday_on_bar,
    manage_open_book_a_on_bar,
)
from app.analytics.option_sniffer_v2.v2_book_b_engine import (
    evaluate_book_b_entry_signal,
    manage_open_book_b_on_bar,
)
from app.analytics.option_sniffer_v2.v2_config import (
    IST_TZ,
    bar_open_label_from_ms,
    ist_hhmm_from_ms,
    parse_ist_timestamp_ms,
    trading_sessions_between,
)
from app.analytics.option_sniffer_v2.v2_data_contract import (
    check_cross_leg_skew,
    check_spot_futures_sync,
    extract_observed_timestamp_ms,
    resolve_strict_quote_age_ms,
    validate_nifty_futures_identity,
    validate_nifty_option_identity,
    validate_nifty_spot_identity,
)
from app.analytics.option_sniffer_v2.v2_ledger_db import V2LedgerStore
from app.analytics.option_sniffer_v2.v2_paper_simulator import (
    OPEN_EXPOSURE_STATES,
    V2PaperSimulator,
    make_stable_id,
)
from app.analytics.option_sniffer_v2.v2_regime_arbitrator import (
    check_candidate_arbitration_gate,
    evaluate_v2_regime_arbitration,
)
from app.analytics.option_sniffer_v2.v2_vrp_and_breach_model import (
    compute_passive_aiml_breach_telemetry,
    compute_vrp_metrics,
    load_prior_nifty_daily_history_readonly,
)


def _normalize_paper_mode(mode: Optional[str]) -> str:
    val = str(mode or "AUTO").upper().strip()
    if val in ("AUTO", "BOOK_A_ONLY", "BOOK_B_ONLY"):
        return val
    return "AUTO"


def compute_causal_bar_indicators(completed_bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Shared single indicator builder for Forward & Replay from finalized session 1m bars.
    Fails closed (`None`) during indicator warmup rather than substituting synthetic proxies.
    """
    completed_bars = [
        b
        for b in completed_bars
        if "09:15" <= str(b.get("bar_open_ts") or "")[11:16] <= "15:29"
    ]
    n = len(completed_bars)
    if n == 0:
        return {
            "bars_count": 0,
            "spot_close": 0.0,
            "tpb_vwap": None,
            "adx14": None,
            "er20": None,
            "atrr_5_20": None,
            "orb30_h": None,
            "orb30_l": None,
            "orb30_ready": False,
            "r2_sum_today": 0.0,
            "session_high": None,
            "session_low": None,
        }

    h_list = [float(b["spot_h"]) for b in completed_bars]
    l_list = [float(b["spot_l"]) for b in completed_bars]
    c_list = [float(b["spot_c"]) for b in completed_bars]

    tpb_vwap: Optional[float] = None
    if n >= 3:
        tpb_sum = sum((h_list[i] + l_list[i] + c_list[i]) / 3.0 for i in range(n))
        tpb_vwap = round(tpb_sum / float(n), 4)

    adx14: Optional[float] = None
    if n >= 28:
        adx_arr, _, _ = compute_adx_series_causal(h_list, l_list, c_list, 14)
        adx14 = float(adx_arr[-1])

    er20: Optional[float] = None
    if n >= 21:
        er_arr = compute_er_series_causal(c_list, 20)
        er20 = float(er_arr[-1])

    atrr_5_20: Optional[float] = None
    if n >= 21:
        atrr_arr = compute_atrr_series_causal(h_list, l_list, c_list, 5, 20)
        atrr_5_20 = float(atrr_arr[-1])

    r2_sum_today = 0.0
    for i in range(1, n):
        if c_list[i - 1] > 0.0 and c_list[i] > 0.0:
            lr = math.log(c_list[i] / c_list[i - 1])
            r2_sum_today += lr * lr

    orb_bars = [b for b in completed_bars if "09:15" <= b["bar_open_ts"][11:16] <= "09:44"]
    last_hm = completed_bars[-1]["bar_open_ts"][11:16]
    # ORB30 requires bars from session start covering up to 09:44 (i.e. decision >= 09:45) without gaps
    has_0915 = any(b["bar_open_ts"][11:16] == "09:15" for b in orb_bars)
    orb_ready = bool(last_hm >= "09:44" and has_0915 and len(orb_bars) >= 28)
    orb_h = max(float(b["spot_h"]) for b in orb_bars) if orb_ready else None
    orb_l = min(float(b["spot_l"]) for b in orb_bars) if orb_ready else None

    return {
        "bars_count": n,
        "spot_close": c_list[-1],
        "tpb_vwap": tpb_vwap,
        "adx14": adx14,
        "er20": er20,
        "atrr_5_20": atrr_5_20,
        "orb30_h": orb_h,
        "orb30_l": orb_l,
        "orb30_ready": orb_ready,
        "r2_sum_today": r2_sum_today,
        "session_high": max(h_list),
        "session_low": min(l_list),
    }


class V2UnifiedEventEngine:
    """
    Single deterministic event-time state & decision engine used identically by
    both live/forward collection and causal replay.
    """

    def __init__(
        self,
        cfg: Dict[str, Any],
        cfg_hash: str,
        store: V2LedgerStore,
        simulator: V2PaperSimulator,
        run_mode: str = "FORWARD_PAPER",
        book_b_params: Optional[Dict[str, Any]] = None,
        enable_book_a_opt_in: bool = False,
        enable_v2_4_dual_book: bool = False,
        paper_execution_mode: str = "AUTO",
        market_db_path: Optional[str] = None,
        persist_bars_and_checkpoints: bool = True,
        on_package_opened_cb: Optional[Callable[[str, List[Dict[str, Any]]], None]] = None,
        on_package_closed_cb: Optional[Callable[[str], None]] = None,
        on_skew_sample_cb: Optional[Callable[[str, Optional[int]], None]] = None,
        on_lifecycle_event_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.cfg = cfg
        self.cfg_hash = str(cfg_hash)
        self.store = store
        self.sim = simulator
        self.run_mode = str(run_mode)
        self.book_b_params = dict(
            book_b_params
            or {
                "variant_code": "V2_FROZEN_COMBINED",
                "use_basis_delta": True,
                "use_weekly_syn_basis": False,
                "window_start": None,
                "window_end": None,
                "allow_otm_fallback": None,
                "min_er_override": None,
            }
        )
        self.enable_book_a_opt_in = bool(enable_book_a_opt_in)
        self.enable_v2_4_dual_book = bool(enable_v2_4_dual_book)
        self.paper_execution_mode = _normalize_paper_mode(paper_execution_mode)
        self.market_db_path = market_db_path
        self.persist_bars_and_checkpoints = bool(persist_bars_and_checkpoints)
        self.on_package_opened_cb = on_package_opened_cb
        self.on_package_closed_cb = on_package_closed_cb
        self.on_skew_sample_cb = on_skew_sample_cb
        self.on_lifecycle_event_cb = on_lifecycle_event_cb

        # Session & bar state
        self.session_date: Optional[str] = None
        self.sod_equity_inr: float = self.sim.starting_capital_inr
        self.completed_bars: List[Dict[str, Any]] = []
        self.building_bar: Optional[Dict[str, Any]] = None
        self.last_finalized_bar_open_ts: Optional[str] = None
        self.session_started_from_open: bool = False
        self.sequence_gap_count: int = 0
        self.overflow_drop_count: int = 0
        self.late_event_count: int = 0
        self.post_session_ignored_count: int = 0
        self.total_post_session_ignored_count: int = 0
        self.foreign_instrument_rejected_count: int = 0
        self.unverifiable_basis_events: int = 0
        self.last_usable_event_ts_ms: int = 0
        self.last_decision_ts_ms: int = 0
        self.open_basis_fail_counts: Dict[str, int] = {}
        self.stress_kill_warnings_by_date: Dict[str, str] = {}
        self._last_persisted_opp_sig: Dict[str, Tuple[Any, ...]] = {}

        # Intraday quotes & references (strict NIFTY identity only)
        self.open_basis_ref: Optional[Dict[str, Any]] = None
        self.open_basis_samples: List[Dict[str, Any]] = []
        self.latest_spot_q: Optional[Dict[str, Any]] = None
        self.latest_fut_q: Optional[Dict[str, Any]] = None
        self.latest_vix_q: Optional[Dict[str, Any]] = None
        self.latest_opt_quotes: Dict[str, Dict[str, Any]] = {}
        self.opt_contracts: Dict[Tuple[str, float, str], Dict[str, Any]] = {}
        self.fsp_by_expiry: Dict[str, float] = {}
        self.prior_daily_vix: List[float] = []
        self.prior_daily_spots: List[float] = []
        self.prior_daily_rvs: List[float] = []
        self._prior_history_date: Optional[str] = None
        self.prior_nifty_history: Dict[str, Any] = {}
        self.latest_vrp_telemetry: Optional[Dict[str, Any]] = None
        self.latest_aiml_telemetry: Optional[Dict[str, Any]] = None
        self.session_decision_trace: List[Dict[str, Any]] = []
        self.latest_decision_by_book: Dict[str, Dict[str, Any]] = {}
        self.latest_regime_arbitration: Optional[Dict[str, Any]] = None
        self.last_indicators: Dict[str, Any] = compute_causal_bar_indicators([])

        if self.persist_bars_and_checkpoints:
            self.recover_from_db()

    def set_paper_execution_mode(self, mode: str) -> str:
        self.paper_execution_mode = _normalize_paper_mode(mode)
        return self.paper_execution_mode

    def _ensure_prior_history(self, session_date: str) -> Dict[str, Any]:
        if self._prior_history_date == session_date and self.prior_nifty_history:
            return self.prior_nifty_history
        hist = load_prior_nifty_daily_history_readonly(
            self.store, session_date, market_db_path=self.market_db_path
        )
        # Overlay any in-memory completed sessions carried across multi-day unpersisted replay
        if self.prior_daily_rvs and len(hist.get("daily_rv_ann") or []) < len(self.prior_daily_rvs):
            rvs = list(self.prior_daily_rvs)
            spots = list(self.prior_daily_spots)
            vixes = list(self.prior_daily_vix)
            hist["daily_rv_ann"] = rvs
            hist["prior_days_count"] = len(rvs)
            hist["warmup_ready"] = len(rvs) >= 20
            hist["rv1"] = round(rvs[-1], 4) if len(rvs) >= 1 else None
            hist["rv5"] = round(sum(rvs[-5:]) / 5.0, 4) if len(rvs) >= 5 else None
            hist["rv20"] = round(sum(rvs[-20:]) / 20.0, 4) if len(rvs) >= 20 else None
            if len(spots) >= 20:
                hist["sma20"] = round(sum(spots[-20:]) / 20.0, 4)
            if len(vixes) >= 10:
                hist["vix_sma10"] = round(sum(vixes[-10:]) / 10.0, 4)
        elif self.prior_daily_vix and hist.get("vix_sma10") is None and len(self.prior_daily_vix) >= 10:
            hist["vix_sma10"] = round(sum(self.prior_daily_vix[-10:]) / 10.0, 4)
        self._prior_history_date = session_date
        self.prior_nifty_history = hist
        return hist

    def _emit_lifecycle_event(self, event_payload: Dict[str, Any]) -> None:
        if self.on_lifecycle_event_cb is not None:
            self.on_lifecycle_event_cb(event_payload)

    def recover_from_db(self) -> None:
        """Reconstructs completed bars, indicator warmup, open basis ref, and checkpointed risk state."""
        ckpts = self.store.load_runtime_checkpoints(self.run_mode, self.cfg_hash)
        if not ckpts:
            return
        last_ck = ckpts[-1]
        dt = last_ck["session_date"]
        self.session_date = dt
        self.open_basis_ref = last_ck.get("open_basis_ref")
        self.open_basis_samples = []
        if isinstance(self.open_basis_ref, dict) and self.open_basis_ref.get("ok"):
            pts = self.open_basis_ref.get("sample_basis_pts")
            if isinstance(pts, list) and pts:
                for p_val in pts:
                    self.open_basis_samples.append({**self.open_basis_ref, "basis_pts": float(p_val)})
            elif self.open_basis_ref.get("basis_pts") is not None:
                self.open_basis_samples.append(dict(self.open_basis_ref))
        self.last_usable_event_ts_ms = int(last_ck.get("last_event_ts_ms") or 0)
        self.last_finalized_bar_open_ts = last_ck.get("last_bar_open_ts")
        self.session_started_from_open = bool(last_ck.get("session_started_from_open"))
        self.sequence_gap_count = int(last_ck.get("sequence_gap_count") or 0)
        self.overflow_drop_count = int(last_ck.get("overflow_drop_count") or 0)
        self.open_basis_fail_counts = dict(last_ck.get("open_basis_fail_summary") or {})
        bars = self.store.load_completed_bars(self.run_mode, self.cfg_hash, session_date=dt)
        self.completed_bars = [
            dict(b)
            for b in bars
            if "09:15" <= str(b.get("bar_open_ts") or "")[11:16] <= "15:29"
        ]
        if self.completed_bars:
            self.last_finalized_bar_open_ts = self.completed_bars[-1]["bar_open_ts"]
        elif self.last_finalized_bar_open_ts and not (
            "09:15" <= str(self.last_finalized_bar_open_ts)[11:16] <= "15:29"
        ):
            self.last_finalized_bar_open_ts = None
            self.sequence_gap_count = 0
        self.last_indicators = compute_causal_bar_indicators(self.completed_bars)

        snaps = self.store.load_daily_snapshots(self.run_mode, self.cfg_hash)
        for s in snaps:
            if s["session_date"] == dt:
                self.sod_equity_inr = float(s["sod_equity_inr"])
                break

    def _roll_session_if_needed(self, new_session_date: str, bar_open_hhmm: int) -> None:
        if self.session_date == new_session_date:
            return
        if self.session_date is not None:
            # Finalize prior session state before clearing
            if self.building_bar is not None:
                self._finalize_building_bar_and_decide(self.building_bar["bar_close_ts_ms"], allow_post_session=False)
            self._persist_session_snapshot_locked(self.session_date)
            if self.completed_bars:
                spot_last = float(self.completed_bars[-1]["spot_c"])
                self.prior_daily_spots.append(spot_last)
                v_last = next((b["vix_c"] for b in reversed(self.completed_bars) if b.get("vix_c")), None)
                if v_last:
                    self.prior_daily_vix.append(float(v_last))
                if len(self.completed_bars) >= 120:
                    prev_ind = compute_causal_bar_indicators(self.completed_bars)
                    n_ret = max(1, len(self.completed_bars) - 1)
                    rv_d = round(
                        math.sqrt(252.0 * (374.0 / float(n_ret)) * max(0.0, float(prev_ind.get("r2_sum_today") or 0.0))) * 100.0,
                        4,
                    )
                    self.prior_daily_rvs.append(rv_d)
                    if self.persist_bars_and_checkpoints:
                        self.store.upsert_session_summary(
                            session_date=self.session_date,
                            rv_ann_pct=rv_d,
                            spot_close=spot_last,
                            vix_close=float(v_last) if v_last else None,
                            bars_count=len(self.completed_bars),
                            updated_ts_ms=self.last_usable_event_ts_ms,
                        )

        self.session_date = new_session_date
        self._prior_history_date = None
        self._ensure_prior_history(new_session_date)
        self.latest_vrp_telemetry = None
        self.latest_aiml_telemetry = None
        open_mtm = self.sim.current_open_conservative_mtm()
        self.sod_equity_inr = round(self.sim.realized_equity_inr + open_mtm, 2)
        self.sim.intraday_min_equity_by_date[new_session_date] = self.sod_equity_inr
        self.sim.stress_intraday_min_equity_by_date[new_session_date] = self.sod_equity_inr
        self.sim.observed_intraday_min_equity_by_date[new_session_date] = round(self.sim.realized_equity_inr, 2)
        self.sim.peak_reserved_by_date[new_session_date] = self.sim.reserved_capital_inr
        self.completed_bars = []
        self.building_bar = None
        self.last_finalized_bar_open_ts = None
        self.session_started_from_open = (bar_open_hhmm == 915)
        self.sequence_gap_count = 0
        self.overflow_drop_count = 0
        self.post_session_ignored_count = 0
        self.open_basis_ref = None
        self.open_basis_samples = []
        self.open_basis_fail_counts = {}
        self._last_persisted_opp_sig = {}
        self.latest_spot_q = None
        self.latest_fut_q = None
        self.latest_vix_q = None
        self.latest_opt_quotes.clear()
        self.opt_contracts.clear()
        self.last_indicators = compute_causal_bar_indicators([])

    def _compute_session_state(self, dt: str, unresolved_count: int) -> str:
        if unresolved_count > 0 or self.sim.is_booking_halted():
            return "UNRESOLVED_EXPOSURE"
        if self.sequence_gap_count > 0 or self.overflow_drop_count > 0:
            return "DATA_GAP"
        last_hhmm = 0
        if self.completed_bars:
            hm = self.completed_bars[-1]["bar_open_ts"][11:16]
            last_hhmm = int(hm[:2]) * 100 + int(hm[3:5])
        if self.session_started_from_open and len(self.completed_bars) >= 350 and last_hhmm >= 1515:
            return "COMPLETE_CLEAN"
        return "PARTIAL_SESSION"

    def _persist_session_snapshot_locked(self, dt: str) -> None:
        if not self.persist_bars_and_checkpoints:
            return
        all_pkgs = list(self.sim._all_pkgs_by_id.values())
        closed_dt = [p for p in all_pkgs if p["state"] not in OPEN_EXPOSURE_STATES and (p.get("exit_bar_ts") or "")[:10] == dt]
        open_pkgs = list(self.sim.open_packages.values())
        unresolved_cnt = sum(
            1
            for p in open_pkgs
            if p.get("is_unpriced_exposure") or p["state"] in ("HEDGE_ONLY_EXPOSURE_OPEN", "UNRESOLVED_EXPOSURE_PAST_1445")
        )
        open_mtm = self.sim.current_open_conservative_mtm()
        eod_mtm = round(self.sim.realized_equity_inr + open_mtm, 2)
        day_net = round(sum(float(p["net_pnl_inr"]) for p in closed_dt), 2)
        start_cap = max(1.0, self.sim.starting_capital_inr)
        peak_eq = max(start_cap, self.sim.running_peak_equity_inr)
        max_dd = round(self.sim.max_mtm_dd_inr, 2)
        s_state = self._compute_session_state(dt, unresolved_cnt)

        self.store.upsert_daily_snapshot(
            {
                "run_mode": self.run_mode,
                "config_hash": self.cfg_hash,
                "session_date": dt,
                "sod_equity_inr": self.sod_equity_inr,
                "eod_realized_equity_inr": self.sim.realized_equity_inr,
                "eod_mtm_equity_inr": eod_mtm,
                "intraday_min_mtm_equity_inr": self.sim.intraday_min_equity_by_date.get(dt, eod_mtm),
                "peak_reserved_capital_inr": self.sim.peak_reserved_by_date.get(dt, self.sim.reserved_capital_inr),
                "closed_trades": len(closed_dt),
                "open_overnight_packages": len(open_pkgs),
                "unresolved_exposure_count": unresolved_cnt,
                "net_pnl_inr": day_net,
                "session_state": s_state,
                "running_peak_equity_inr": peak_eq,
                "max_mtm_dd_inr": max_dd,
                "max_mtm_dd_pct_start": round(100.0 * max_dd / start_cap, 2),
                "max_mtm_dd_pct_peak": round(100.0 * max_dd / peak_eq, 2),
                "gap_count": self.sequence_gap_count,
                "overflow_count": self.overflow_drop_count,
                "observed_max_mtm_dd_inr": round(self.sim.max_observed_mtm_dd_inr, 2),
                "stress_max_mtm_dd_inr": round(self.sim.max_stress_mtm_dd_inr, 2),
                "observed_intraday_min_equity_inr": self.sim.observed_intraday_min_equity_by_date.get(dt),
                "stress_intraday_min_equity_inr": self.sim.stress_intraday_min_equity_by_date.get(dt, eod_mtm),
                "open_basis_fail_summary": dict(self.open_basis_fail_counts),
            }
        )

        self.store.save_runtime_checkpoint(
            {
                "run_mode": self.run_mode,
                "config_hash": self.cfg_hash,
                "session_date": dt,
                "updated_ts_ms": self.last_usable_event_ts_ms,
                "daily_kill_reason": self.sim.daily_kill_triggered_by_date.get(dt),
                "cooldown": self.sim.cooldown_until_ms,
                "bar_locks": self.sim.locked_until_after_bar,
                "open_basis_ref": self.open_basis_ref,
                "last_event_ts_ms": self.last_usable_event_ts_ms,
                "last_bar_open_ts": self.last_finalized_bar_open_ts,
                "session_started_from_open": self.session_started_from_open,
                "sequence_gap_count": self.sequence_gap_count,
                "overflow_drop_count": self.overflow_drop_count,
                "booking_fault": self.sim.booking_fault,
                "running_peak_equity_inr": peak_eq,
                "max_mtm_dd_inr": max_dd,
                "intraday_min_mtm_equity_inr": self.sim.intraday_min_equity_by_date.get(dt, eod_mtm),
                "peak_reserved_inr": self.sim.peak_reserved_by_date.get(dt, self.sim.reserved_capital_inr),
                "observed_running_peak_inr": round(self.sim.observed_running_peak_equity_inr, 2),
                "observed_max_mtm_dd_inr": round(self.sim.max_observed_mtm_dd_inr, 2),
                "stress_max_mtm_dd_inr": round(self.sim.max_stress_mtm_dd_inr, 2),
                "open_basis_fail_summary": dict(self.open_basis_fail_counts),
            }
        )

    @staticmethod
    def _classify_execution_availability(
        book_id: str,
        sig_or_block: Dict[str, Any],
        passed: bool,
        decision_status: str,
    ) -> str:
        if decision_status in ("BLOCKED", "DISABLED"):
            rsn_u = str(sig_or_block.get("reject_reason") or "").upper()
            if "UNVERIFIABLE" in rsn_u or "BASIS" in rsn_u or "SKEW" in rsn_u:
                return "UNEXECUTABLE_UNVERIFIABLE_BASIS"
            return "NOT_EVALUATED"
        if passed:
            return "EXECUTABLE_L5"
        rsn = str(sig_or_block.get("reject_reason") or "")
        for g in sig_or_block.get("gates") or []:
            if not g.get("passed") and g.get("name") == "ObsL5Depth":
                cur = str(g.get("current") or "").upper()
                if "STALE" in cur or "AGE" in cur:
                    return "UNEXECUTABLE_STALE"
                if "SPREAD" in cur:
                    return "UNEXECUTABLE_SPREAD_WIDE"
                if "MISSING" in cur or "NO_QUOTE" in cur or "UNOBSERVED" in cur or "NONE" in cur:
                    return "UNEXECUTABLE_MISSING_QUOTE"
        ru = rsn.upper()
        if "STALE" in ru:
            return "UNEXECUTABLE_STALE"
        if "SPREAD" in ru:
            return "UNEXECUTABLE_SPREAD_WIDE"
        if "MISSING" in ru or "ORB30_OR_VWAP_INCOMPLETE" in ru:
            return "UNEXECUTABLE_MISSING_QUOTE"
        if "BasisGate:" in rsn and any(tok in ru for tok in ("MISSING", "SKEW", "STALE", "UNVERIFIABLE", "ROLLED")):
            return "UNEXECUTABLE_UNVERIFIABLE_BASIS"
        # Check if quote payload was observed even though another gate failed
        q = sig_or_block.get("quote") or sig_or_block.get("short_quote")
        if isinstance(q, dict) and float(q.get("raw_bid") or 0.0) > 0.0 and float(q.get("raw_ask") or 0.0) >= float(q.get("raw_bid") or 0.0):
            if q.get("book_provenance") == "OBSERVED_V3_L5" and q.get("depth_observed_on_tick"):
                return "EXECUTABLE_L5"
            if q.get("is_observed_l1_book"):
                return "EXECUTABLE_L1_ONLY"
        return "UNEXECUTABLE_MISSING_QUOTE" if book_id == "V2_BOOK_B_PAID_FLOW" else "NOT_EVALUATED"

    def _record_opportunity_with_dedup(
        self,
        session_date: str,
        bar_ts: str,
        eval_hhmm: int,
        decision_ts_ms: int,
        book_id: str,
        candidate_variant: str,
        direction: Optional[str],
        decision_status: str,
        primary_reject_reason: Optional[str],
        gates: List[Dict[str, Any]],
        ind: Dict[str, Any],
        extra_inputs: Dict[str, Any],
        contract: Optional[Dict[str, Any]],
        quote: Optional[Dict[str, Any]],
        sig_obj: Dict[str, Any],
    ) -> None:
        if not self.persist_bars_and_checkpoints or not (915 <= eval_hhmm <= 1515):
            return
        failed_gates = [g for g in (gates or []) if not g.get("passed")]
        failed_names = tuple(str(g.get("name")) for g in failed_gates)
        exec_avail = self._classify_execution_availability(
            book_id, sig_obj, decision_status == "ACCEPTED", decision_status
        )
        # Flag true ORB30+VWAP breakout bars for Book B
        is_brk = 0
        if book_id == "V2_BOOK_B_PAID_FLOW":
            sc = float(ind.get("spot_close") or 0.0)
            oh = ind.get("orb30_h")
            ol = ind.get("orb30_l")
            vw = ind.get("tpb_vwap")
            if oh and ol and vw and ((sc > oh and sc > vw) or (sc < ol and sc < vw)):
                is_brk = 1
        elif book_id != "V2_BOOK_B_PAID_FLOW" and decision_status in ("ACCEPTED", "REJECTED"):
            is_brk = 1

        sig_key = (
            str(direction or ""),
            decision_status,
            failed_names,
            exec_avail,
            is_brk,
            str(primary_reject_reason or "") if decision_status == "BLOCKED" else "",
        )
        prev_sig = self._last_persisted_opp_sig.get(book_id)
        if decision_status != "ACCEPTED" and sig_key == prev_sig:
            return
        self._last_persisted_opp_sig[book_id] = sig_key

        spot_ts = extract_observed_timestamp_ms(self.latest_spot_q, require_depth_ts=False) or None
        fut_ts = extract_observed_timestamp_ms(self.latest_fut_q, require_depth_ts=False) or None
        opt_depth_ts = extract_observed_timestamp_ms(quote, require_depth_ts=True) or None if quote else None
        opt_l1_ts = extract_observed_timestamp_ms(quote, require_depth_ts=False) or None if quote else None
        sf_skew = abs(int(spot_ts) - int(fut_ts)) if (spot_ts and fut_ts) else None

        gate_inputs = {
            "spot_close": ind.get("spot_close"),
            "tpb_vwap": ind.get("tpb_vwap"),
            "orb30_h": ind.get("orb30_h"),
            "orb30_l": ind.get("orb30_l"),
            "adx14": ind.get("adx14"),
            "er20": ind.get("er20"),
            "vix_ltp": (self.latest_vix_q or {}).get("spot_ltp"),
            **(extra_inputs or {}),
        }
        contract_id = (
            {
                "instrument_key": contract.get("instrument_key"),
                "trading_symbol": contract.get("trading_symbol"),
                "expiry": contract.get("expiry"),
                "strike": contract.get("strike"),
                "option_type": contract.get("option_type"),
                "lot_size": contract.get("lot_size"),
            }
            if isinstance(contract, dict) and contract.get("instrument_key")
            else None
        )
        quote_ts = {
            "spot_ts_ms": spot_ts,
            "fut_ts_ms": fut_ts,
            "spot_fut_skew_ms": sf_skew,
            "opt_depth_ts_ms": opt_depth_ts,
            "opt_l1_ts_ms": opt_l1_ts,
            "opt_age_ms": quote.get("l1_book_age_ms") if isinstance(quote, dict) else None,
            "opt_provenance": quote.get("book_provenance") if isinstance(quote, dict) else None,
        }
        opp_id = make_stable_id("osv2_opp", self.run_mode, self.cfg_hash[:12], book_id, bar_ts, decision_status)
        self.store.record_opportunity_idempotent(
            {
                "opportunity_id": opp_id,
                "run_mode": self.run_mode,
                "config_hash": self.cfg_hash,
                "protocol_version": str(self.cfg.get("engine_version") or "2.2.0-causal"),
                "session_date": session_date,
                "bar_ts": bar_ts,
                "decision_ts_ms": decision_ts_ms,
                "book_id": book_id,
                "candidate_variant": candidate_variant,
                "direction": direction,
                "decision_status": decision_status,
                "primary_reject_reason": primary_reject_reason,
                "failed_gates": failed_gates,
                "gate_inputs": gate_inputs,
                "contract_identity": contract_id,
                "quote_timestamps": quote_ts,
                "execution_availability": exec_avail,
                "is_breakout_bar": is_brk,
            }
        )

    def _finalize_building_bar_and_decide(
        self,
        trigger_ts_ms: int,
        allow_post_session: bool = False,
        uncertainty_blocks: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Finalizes the completed `[bar_open, bar_close)` bar using ONLY ticks belonging to that bar
        and evaluates strictly causal signals/exits/MTM BEFORE the next-bar tick touches caches.
        """
        b = self.building_bar
        self.building_bar = None
        if b is None or b.get("spot_c", 0.0) <= 0.0:
            return None

        dt = b["session_date"]
        bar_open_ts = b["bar_open_ts"]
        bar_open_ms = int(b["bar_open_ms"])
        bar_close_ms = int(b["bar_close_ms"])
        bar_open_hhmm = int(b["bar_open_hhmm"])
        decision_ts_ms = max(bar_close_ms, int(b["max_avail_ts_ms"]))
        dec_hhmm = ist_hhmm_from_ms(decision_ts_ms)

        if bar_open_hhmm < 915 or dec_hhmm < 915:
            return {
                "status": "PRE_SESSION_IGNORED",
                "bar_open_ts": bar_open_ts,
                "decision_ts_ms": decision_ts_ms,
            }

        if not allow_post_session and dec_hhmm > 1530:
            # Post-session EOD batch flush (e.g. 19:41 IST) cannot trigger retroactive trading or cutoffs
            self.sequence_gap_count += 1
            self._persist_session_snapshot_locked(dt)
            return {"status": "POST_SESSION_FLUSH_IGNORED", "bar_open_ts": bar_open_ts, "decision_ts_ms": decision_ts_ms}

        # Detect minute discontinuity vs previous finalized bar in same session
        if self.last_finalized_bar_open_ts and self.last_finalized_bar_open_ts[:10] == dt:
            prev_dt = datetime.strptime(self.last_finalized_bar_open_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST_TZ)
            prev_ms = int(prev_dt.timestamp() * 1000)
            if bar_open_ms - prev_ms > 60_000:
                self.sequence_gap_count += int((bar_open_ms - prev_ms) // 60_000) - 1

        self.last_finalized_bar_open_ts = bar_open_ts

        row_bar = {
            "run_mode": self.run_mode,
            "config_hash": self.cfg_hash,
            "bar_open_ts": bar_open_ts,
            "session_date": dt,
            "spot_o": b["spot_o"],
            "spot_h": b["spot_h"],
            "spot_l": b["spot_l"],
            "spot_c": b["spot_c"],
            "vix_c": b.get("vix_c"),
            "bar_close_ts_ms": bar_close_ms,
            "decision_ts_ms": decision_ts_ms,
        }
        self.completed_bars.append(row_bar)
        if self.persist_bars_and_checkpoints:
            self.store.save_completed_bar(row_bar)

        ind = compute_causal_bar_indicators(self.completed_bars)
        self.last_indicators = ind
        self.last_decision_ts_ms = decision_ts_ms
        eval_hhmm = dec_hhmm
        if eval_hhmm < 915 or eval_hhmm > 1530:
            return {"status": "OUTSIDE_REGULAR_SESSION", "bar_open_ts": bar_open_ts}

        # Open basis reference window across 09:15..09:29 using causal median of synchronized
        # Spot/Futures observations (immune to 09:15 opening auction noise & mid-session roll).
        if (
            915 <= bar_open_hhmm <= 929
            and not (isinstance(self.open_basis_ref, dict) and self.open_basis_ref.get("ok") is False)
        ):
            if not self.latest_spot_q or not self.latest_fut_q:
                missing_tag = "MISSING_SPOT_AND_FUT" if (not self.latest_spot_q and not self.latest_fut_q) else ("MISSING_FUT" if not self.latest_fut_q else "MISSING_SPOT")
                self.open_basis_fail_counts[missing_tag] = self.open_basis_fail_counts.get(missing_tag, 0) + 1
            else:
                expected_open_contract = self.open_basis_samples[0] if self.open_basis_samples else None
                chk = check_spot_futures_sync(
                    self.latest_spot_q,
                    self.latest_fut_q,
                    max_skew_ms=int(self.cfg["quote_hygiene"]["max_spot_fut_sync_skew_ms"]),
                    max_age_ms=int(self.cfg["quote_hygiene"]["max_entry_quote_age_ms"]),
                    now_ms=decision_ts_ms,
                    expected_fut_contract=expected_open_contract,
                )
                if chk.get("skew_ms") is not None and self.on_skew_sample_cb:
                    self.on_skew_sample_cb("spot_fut", int(chk["skew_ms"]))
                if chk.get("ok"):
                    self.open_basis_samples.append(chk)
                    pts_list = [float(s["basis_pts"]) for s in self.open_basis_samples]
                    med_pts = round(float(statistics.median(pts_list)), 4)
                    self.open_basis_ref = {
                        **chk,
                        "basis_pts": med_pts,
                        "sample_count": len(pts_list),
                        "sample_basis_pts": pts_list,
                        "aggregation": "MEDIAN_0915_0929",
                    }
                else:
                    fail_rsn = str(chk.get("reason") or "BASIS_SYNC_FAILED")
                    fail_key = "SPOT_FUT_SKEW" if fail_rsn.startswith("SPOT_FUT_SKEW_") else fail_rsn
                    self.open_basis_fail_counts[fail_key] = self.open_basis_fail_counts.get(fail_key, 0) + 1
                    if fail_rsn == "FUTURES_CONTRACT_ROLLED_MID_SESSION":
                        self.open_basis_ref = {
                            "ok": False,
                            "verifiable": True,
                            "reason": "FUTURES_CONTRACT_ROLLED_MID_SESSION",
                        }
                    elif chk.get("verifiable") is False:
                        self.unverifiable_basis_events += 1

        # 1. Mark existing exposure first (strict bar[t].close MTM before evaluating exits / entries)
        mtm_info = self.sim.mark_open_packages(
            quote_by_ikey=self.latest_opt_quotes,
            session_date=dt,
            bar_ts=bar_open_ts,
            now_ms=decision_ts_ms,
            is_opening_bar=(bar_open_hhmm == 915),
            is_eod_bar=(eval_hhmm >= 1515),
        )
        kill_lim = float(self.cfg["account_envelope"]["max_daily_mtm_kill_inr"])
        slots = self.sim.active_slots_and_reserves(bar_open_ts, dt)
        obs_mtm_val = mtm_info.get("fresh_executable_mtm_inr")
        stress_mtm_val = float(mtm_info["stress_unrealized_mtm_inr"])
        if obs_mtm_val is not None and (slots["realized_today_inr"] + float(obs_mtm_val)) <= kill_lim:
            self.sim.daily_kill_triggered_by_date[dt] = f"DAILY_KILL_OBSERVED_MTM_LE_{kill_lim}"
        elif obs_mtm_val is None and (slots["realized_today_inr"] + stress_mtm_val) <= kill_lim:
            self.stress_kill_warnings_by_date[dt] = f"STRESS_ESTIMATE_LE_{kill_lim}"

        # 2. Manage open packages
        for pid, pkg in list(self.sim.open_packages.items()):
            prev_ratchet = (pkg.get("legs_state") or {}).get("ratchet_stop")
            prev_ratchet_debit = (pkg.get("legs_state") or {}).get("ratchet_max_debit_pts")
            res_close = None
            if pkg["book_id"] == "V2_BOOK_B_PAID_FLOW":
                stag_mins = (
                    self.book_b_params.get("stagnation_mins_override")
                    if self.book_b_params.get("stagnation_mins_override") is not None
                    else (45 if self.enable_v2_4_dual_book else None)
                )
                stag_gain = (
                    self.book_b_params.get("stagnation_min_gain_override")
                    if self.book_b_params.get("stagnation_min_gain_override") is not None
                    else (10.0 if self.enable_v2_4_dual_book else None)
                )
                res_close = manage_open_book_b_on_bar(
                    sim=self.sim,
                    pkg=pkg,
                    session_date=dt,
                    bar_ts=bar_open_ts,
                    ist_hhmm=eval_hhmm,
                    decision_ts_ms=decision_ts_ms,
                    spot_close=ind["spot_close"],
                    spot_vwap=ind["tpb_vwap"] or ind["spot_close"],
                    quote_by_ikey=self.latest_opt_quotes,
                    stop_pct_override=self.book_b_params.get("stop_pct_override"),
                    stop_max_inr_override=self.book_b_params.get("stop_max_inr_override"),
                    stagnation_mins_override=stag_mins,
                    stagnation_min_gain_override=stag_gain,
                )
            elif pkg["book_id"] == "V2_BOOK_A_INTRADAY_CREDIT":
                res_close = manage_open_book_a_intraday_on_bar(
                    sim=self.sim,
                    pkg=pkg,
                    session_date=dt,
                    bar_ts=bar_open_ts,
                    ist_hhmm=eval_hhmm,
                    decision_ts_ms=decision_ts_ms,
                    spot_close=ind["spot_close"],
                    spot_vwap=ind["tpb_vwap"] or ind["spot_close"],
                    quotes_by_ikey=self.latest_opt_quotes,
                    is_eod_bar=(eval_hhmm >= 1525),
                    eod_squareoff_hhmm=int(V2_BOOK_A_INTRADAY_CREDIT_SPEC["eod_squareoff_ist_hhmm"]),
                )
            else:
                res_close = manage_open_book_a_on_bar(
                    sim=self.sim,
                    pkg=pkg,
                    session_date=dt,
                    bar_ts=bar_open_ts,
                    ist_hhmm=eval_hhmm,
                    decision_ts_ms=decision_ts_ms,
                    spot_close=ind["spot_close"],
                    quote_by_ikey=self.latest_opt_quotes,
                    fsp_twap_spot=self.fsp_by_expiry.get(dt),
                )
            if isinstance(res_close, dict) and res_close.get("state") in ("CLOSED", "INCOMPLETE_LEG_UNWOUND"):
                if self.on_package_closed_cb:
                    self.on_package_closed_cb(pid)
                closed_snap = dict(self.sim._all_pkgs_by_id.get(pid) or pkg)
                ls_snap = closed_snap.get("legs_state") or {}
                q_snap: Dict[str, Dict[str, Any]] = {}
                for lk_name in ("long_leg", "wing_leg", "short_leg"):
                    lk_obj = ls_snap.get(lk_name) or {}
                    ikey_l = str(lk_obj.get("instrument_key") or "")
                    if ikey_l and ikey_l in self.latest_opt_quotes:
                        q_snap[ikey_l] = dict(self.latest_opt_quotes[ikey_l])
                self._emit_lifecycle_event(
                    {
                        "event_type": "PACKAGE_CLOSED",
                        "run_mode": self.run_mode,
                        "package_id": pid,
                        "signal_id": closed_snap.get("signal_id"),
                        "session_date": dt,
                        "bar_ts": bar_open_ts,
                        "decision_ts_ms": decision_ts_ms,
                        "book_id": closed_snap.get("book_id"),
                        "exit_reason": res_close.get("exit_reason") or closed_snap.get("exit_reason") or "STRATEGY_EXIT",
                        "package_snapshot": closed_snap,
                        "quote_by_ikey": q_snap,
                    }
                )
            elif pid in self.sim.open_packages and pkg["book_id"] == "V2_BOOK_B_PAID_FLOW":
                upd_pkg = self.sim.open_packages[pid]
                ls_upd = upd_pkg.get("legs_state") or {}
                new_ratchet = ls_upd.get("ratchet_stop")
                if new_ratchet is not None and (prev_ratchet is None or float(new_ratchet) > float(prev_ratchet) + 1e-6):
                    self._emit_lifecycle_event(
                        {
                            "event_type": "RATCHET_UPDATED",
                            "run_mode": self.run_mode,
                            "package_id": pid,
                            "signal_id": upd_pkg.get("signal_id"),
                            "session_date": dt,
                            "bar_ts": bar_open_ts,
                            "decision_ts_ms": decision_ts_ms,
                            "book_id": upd_pkg.get("book_id"),
                            "previous_ratchet_stop": prev_ratchet,
                            "new_ratchet_stop": float(new_ratchet),
                            "hard_stop": ls_upd.get("hard_stop"),
                        }
                    )
            elif pid in self.sim.open_packages and pkg["book_id"] == "V2_BOOK_A_INTRADAY_CREDIT":
                upd_pkg = self.sim.open_packages[pid]
                ls_upd = upd_pkg.get("legs_state") or {}
                new_ratchet_deb = ls_upd.get("ratchet_max_debit_pts")
                if new_ratchet_deb is not None and (
                    prev_ratchet_debit is None or float(new_ratchet_deb) < float(prev_ratchet_debit) - 1e-6
                ):
                    self._emit_lifecycle_event(
                        {
                            "event_type": "RATCHET_UPDATED",
                            "run_mode": self.run_mode,
                            "package_id": pid,
                            "signal_id": upd_pkg.get("signal_id"),
                            "session_date": dt,
                            "bar_ts": bar_open_ts,
                            "decision_ts_ms": decision_ts_ms,
                            "book_id": upd_pkg.get("book_id"),
                            "previous_ratchet_stop": prev_ratchet_debit,
                            "new_ratchet_stop": float(new_ratchet_deb),
                            "hard_stop": ls_upd.get("hard_stop_debit_pts"),
                        }
                    )

        # 3. Evaluate new entries strictly after exits (`active_slots_and_reserves` locks same-bar exit slots)
        slots = self.sim.active_slots_and_reserves(bar_open_ts, dt)
        env = self.cfg["account_envelope"]
        cfg_b = self.cfg["books"]["V2_BOOK_B_PAID_FLOW"]
        cfg_a = self.cfg["books"]["V2_BOOK_A_PROPOSED_O3"]
        hyg = self.cfg["quote_hygiene"]
        entered_b = False
        sig_b_record: Optional[Dict[str, Any]] = None
        expiries_all = sorted({k[0] for k in self.opt_contracts.keys()})
        vix_now_val = float((self.latest_vix_q or {}).get("spot_ltp") or (b.get("vix_c") or 0.0)) or None
        book_a_active = bool(cfg_a.get("autonomous_entries_enabled", False) or self.enable_book_a_opt_in)

        prior_hist = self._ensure_prior_history(dt)
        quotes_lk = {
            k: self.latest_opt_quotes[c["instrument_key"]]
            for k, c in self.opt_contracts.items()
            if c["instrument_key"] in self.latest_opt_quotes
        }

        if self.enable_v2_4_dual_book:
            book_b_mode_allowed = self.paper_execution_mode in ("AUTO", "BOOK_B_ONLY")
            book_a_intraday_allowed = self.paper_execution_mode in ("AUTO", "BOOK_A_ONLY")
            vrp_exp = next(
                (
                    e
                    for e in expiries_all
                    if e >= dt and int(cfg_b["min_trading_dte"]) <= trading_sessions_between(dt, e) <= int(cfg_b.get("max_trading_dte", 12))
                ),
                None,
            ) or next((e for e in expiries_all if e >= dt), None)
            vrp_tdte = trading_sessions_between(dt, vrp_exp) if vrp_exp else 0
            atm_vrp = round(float(ind["spot_close"]) / 50.0) * 50.0 if ind["spot_close"] > 0 else None
            ce_q_vrp = quotes_lk.get((vrp_exp or "", atm_vrp, "CE")) if (vrp_exp and atm_vrp) else None
            pe_q_vrp = quotes_lk.get((vrp_exp or "", atm_vrp, "PE")) if (vrp_exp and atm_vrp) else None
            ce_mid_vrp: Optional[float] = None
            pe_mid_vrp: Optional[float] = None
            if ce_q_vrp and pe_q_vrp:
                skew_ok_v, _, _ = check_cross_leg_skew(
                    ce_q_vrp, pe_q_vrp, max_skew_ms=int(hyg["max_cross_leg_skew_ms"]), require_depth_ts=False
                )
                max_age_v = int(hyg["max_entry_quote_age_ms"])
                ce_age = resolve_strict_quote_age_ms(ce_q_vrp, now_ms=decision_ts_ms, require_observed_l5=False)
                pe_age = resolve_strict_quote_age_ms(pe_q_vrp, now_ms=decision_ts_ms, require_observed_l5=False)
                ce_ts_ok = ce_age is not None and 0 <= ce_age <= max_age_v
                pe_ts_ok = pe_age is not None and 0 <= pe_age <= max_age_v
                cb, ca = float(ce_q_vrp.get("raw_bid") or 0.0), float(ce_q_vrp.get("raw_ask") or 0.0)
                pb, pa = float(pe_q_vrp.get("raw_bid") or 0.0), float(pe_q_vrp.get("raw_ask") or 0.0)
                if skew_ok_v and ce_ts_ok and pe_ts_ok and cb > 0.0 and ca >= cb and pb > 0.0 and pa >= pb:
                    ce_mid_vrp = 0.5 * (cb + ca)
                    pe_mid_vrp = 0.5 * (pb + pa)
            self.latest_vrp_telemetry = compute_vrp_metrics(
                atm_ce_mid=ce_mid_vrp,
                atm_pe_mid=pe_mid_vrp,
                atm_strike=atm_vrp,
                spot_close=ind["spot_close"],
                ist_hhmm=eval_hhmm,
                trading_dte=vrp_tdte,
                r2_sum_today=ind.get("r2_sum_today"),
                elapsed_bars_today=ind["bars_count"],
                prior_history=prior_hist,
            )
            dir_bull = bool(ind["tpb_vwap"] and ind["spot_close"] >= ind["tpb_vwap"])
            self.latest_aiml_telemetry = compute_passive_aiml_breach_telemetry(
                spot_close=ind["spot_close"],
                vwap=ind["tpb_vwap"],
                session_high=ind.get("session_high"),
                session_low=ind.get("session_low"),
                adx14=ind.get("adx14"),
                er20=ind.get("er20"),
                atrr_5_20=ind.get("atrr_5_20"),
                live_vix=vix_now_val,
                direction_bullish=dir_bull,
                barrier_pts=50.0,
                ist_hhmm=eval_hhmm,
                r2_sum_today=ind.get("r2_sum_today"),
                elapsed_bars_today=ind["bars_count"],
                prior_history=prior_hist,
            )
        else:
            book_b_mode_allowed = bool(cfg_b.get("autonomous_entries_enabled", True))
            book_a_intraday_allowed = False

        self.latest_regime_arbitration = evaluate_v2_regime_arbitration(
            session_date=dt,
            ist_hhmm=eval_hhmm,
            expiries=expiries_all,
            spot_close=ind["spot_close"],
            spot_vwap=ind["tpb_vwap"],
            orb30_h=ind["orb30_h"],
            orb30_l=ind["orb30_l"],
            orb30_ready=bool(ind["orb30_ready"]),
            adx14=ind["adx14"],
            er20=ind["er20"],
            vix_now=vix_now_val,
            prior_vix_closes=self.prior_daily_vix,
            open_packages=list(self.sim.open_packages.values()),
            max_open_packages_total=int(env["max_open_packages_total"]),
            book_a_enabled=(book_a_active or book_a_intraday_allowed),
            book_b_enabled=book_b_mode_allowed,
        )

        blocks = list(uncertainty_blocks or [])
        if self.sim.is_booking_halted():
            blocks.append("BOOKING_FAILURE_HALTED")
        if dt in self.sim.daily_kill_triggered_by_date:
            blocks.append(self.sim.daily_kill_triggered_by_date[dt])
        if mtm_info["unpriced_count"] > 0:
            blocks.append("UNPRICED_OPEN_EXPOSURE")

        if not book_b_mode_allowed:
            paused_obj_b = {
                "bar_ts": bar_open_ts,
                "decision_ts_ms": decision_ts_ms,
                "passed": False,
                "state": "PAUSED_BY_PAPER_MODE",
                "reject_reason": f"PAUSED_BY_PAPER_MODE:{self.paper_execution_mode}",
                "gates": [],
                "indicators": dict(ind),
            }
            self.latest_decision_by_book[cfg_b["book_id"]] = paused_obj_b
        elif (
            not blocks
            and slots["open_total"] < int(env["max_open_packages_total"])
            and slots["trades_today_total"] < int(env["max_total_trades_per_day"])
            and slots["open_by_book"].get(cfg_b["book_id"], 0) < int(cfg_b["max_open"])
            and slots["trades_today_by_book"].get(cfg_b["book_id"], 0) < int(cfg_b["max_trades_day"])
            and decision_ts_ms >= self.sim.cooldown_until_ms.get(cfg_b["book_id"], 0)
        ):
            expiries = expiries_all
            vrp_t = self.latest_vrp_telemetry or {}
            sig_b = evaluate_book_b_entry_signal(
                cfg=self.cfg,
                session_date=dt,
                bar_ts=bar_open_ts,
                ist_hhmm=eval_hhmm,
                decision_ts_ms=decision_ts_ms,
                spot_close=ind["spot_close"],
                spot_vwap=ind["tpb_vwap"],
                orb30_h=ind["orb30_h"],
                orb30_l=ind["orb30_l"],
                adx14=ind["adx14"],
                er20=ind["er20"],
                spot_quote=self.latest_spot_q,
                fut_quote=self.latest_fut_q,
                open_basis_ref=self.open_basis_ref,
                expiries=expiries,
                contracts_lookup=self.opt_contracts,
                quote_lookup=quotes_lk,
                variant_code=self.book_b_params["variant_code"],
                use_basis_delta=bool(self.book_b_params.get("use_basis_delta", True)),
                use_weekly_syn_basis=bool(self.book_b_params.get("use_weekly_syn_basis", False)),
                window_start_override=self.book_b_params.get("window_start"),
                window_end_override=self.book_b_params.get("window_end"),
                allow_single_otm_fallback=self.book_b_params.get("allow_otm_fallback"),
                min_er_override=self.book_b_params.get("min_er_override"),
                min_adx_override=self.book_b_params.get("min_adx_override"),
                min_obi_override=self.book_b_params.get("min_obi_override"),
                vrp_ratio=vrp_t.get("vrp_ratio"),
                atm_iv_pct=vrp_t.get("atm_iv_pct"),
                har_rv_pct=vrp_t.get("har_rv_pct"),
                enforce_vrp_gate=bool(self.enable_v2_4_dual_book and self.book_b_params.get("enforce_vrp_gate", True)),
                max_vrp_ratio=float(self.book_b_params.get("max_vrp_ratio", 1.15)),
                vrp_status=vrp_t.get("status"),
            )
            sig_b_record = {
                "bar_ts": bar_open_ts,
                "decision_ts_ms": decision_ts_ms,
                "passed": bool(sig_b["passed"]),
                "state": sig_b.get("state"),
                "reject_reason": sig_b.get("reject_reason"),
                "gates": sig_b.get("gates", []),
                "indicators": dict(ind),
            }
            self.latest_decision_by_book[cfg_b["book_id"]] = sig_b_record

            if sig_b["passed"] and sig_b.get("contract") and sig_b.get("quote"):
                arb_ok_b, arb_rsn_b = check_candidate_arbitration_gate(
                    self.latest_regime_arbitration,
                    book_id=cfg_b["book_id"],
                    is_bull=(sig_b["option_type"] == "CE"),
                    open_total=slots["open_total"],
                    max_open_packages_total=int(env["max_open_packages_total"]),
                    book_a_enabled=book_a_active,
                    book_b_enabled=bool(cfg_b.get("autonomous_entries_enabled", True)),
                )
                if not arb_ok_b:
                    self._record_opportunity_with_dedup(
                        session_date=dt,
                        bar_ts=bar_open_ts,
                        eval_hhmm=eval_hhmm,
                        decision_ts_ms=decision_ts_ms,
                        book_id=cfg_b["book_id"],
                        candidate_variant=self.book_b_params["variant_code"],
                        direction=sig_b.get("option_type"),
                        decision_status="BLOCKED",
                        primary_reject_reason=str(arb_rsn_b),
                        gates=sig_b.get("gates", []),
                        ind=ind,
                        extra_inputs=sig_b.get("basis_meta") or {},
                        contract=sig_b.get("contract"),
                        quote=sig_b.get("quote"),
                        sig_obj=sig_b,
                    )
                else:
                    sid = make_stable_id(
                        "osv2_sig", self.run_mode, self.book_b_params["variant_code"], bar_open_ts, sig_b["contract"]["instrument_key"]
                    )
                    self.store.record_signal_idempotent(
                        {
                            "signal_id": sid,
                            "run_mode": self.run_mode,
                            "config_hash": self.cfg_hash,
                            "session_date": dt,
                            "bar_ts": bar_open_ts,
                            "decision_ts_ms": decision_ts_ms,
                            "book_id": cfg_b["book_id"],
                            "candidate_variant": self.book_b_params["variant_code"],
                            "direction": sig_b["option_type"],
                            "status": "TRIGGERED",
                            "reject_reason": None,
                            "gates": sig_b["gates"],
                            "provenance": sig_b["basis_meta"],
                        }
                    )
                    op = self.sim.open_book_b_paid_long(
                        signal_id=sid,
                        candidate_variant=self.book_b_params["variant_code"],
                        session_date=dt,
                        bar_ts=bar_open_ts,
                        decision_ts_ms=decision_ts_ms,
                        contract=sig_b["contract"],
                        quote=sig_b["quote"],
                        spot_price=ind["spot_close"],
                        basis_meta=sig_b["basis_meta"],
                        used_single_otm_fallback=sig_b["used_single_otm_fallback"],
                    )
                    if op.get("ok"):
                        entered_b = True
                        if self.on_package_opened_cb:
                            self.on_package_opened_cb(op["package_id"], [sig_b["contract"]])
                        open_snap = dict(self.sim.open_packages.get(op["package_id"]) or {})
                        self._emit_lifecycle_event(
                            {
                                "event_type": "PACKAGE_OPENED",
                                "run_mode": self.run_mode,
                                "package_id": op["package_id"],
                                "signal_id": sid,
                                "session_date": dt,
                                "bar_ts": bar_open_ts,
                                "decision_ts_ms": decision_ts_ms,
                                "book_id": cfg_b["book_id"],
                                "structure": "SINGLE_LONG",
                                "package_snapshot": open_snap,
                                "quote_snapshot": dict(sig_b["quote"]),
                            }
                        )
                    b_extra = {
                        **(sig_b.get("basis_meta") or {}),
                        **(sig_b.get("vrp_meta") or {}),
                        "aiml_p_breach_passive": (self.latest_aiml_telemetry or {}).get("p_breach"),
                    }
                    self._record_opportunity_with_dedup(
                        session_date=dt,
                        bar_ts=bar_open_ts,
                        eval_hhmm=eval_hhmm,
                        decision_ts_ms=decision_ts_ms,
                        book_id=cfg_b["book_id"],
                        candidate_variant=self.book_b_params["variant_code"],
                        direction=sig_b.get("option_type"),
                        decision_status="ACCEPTED" if op.get("ok") else "BLOCKED",
                        primary_reject_reason=None if op.get("ok") else str(op.get("reason") or "OPEN_FAILED"),
                        gates=sig_b.get("gates", []),
                        ind=ind,
                        extra_inputs=b_extra,
                        contract=sig_b.get("contract"),
                        quote=sig_b.get("quote"),
                        sig_obj=sig_b,
                    )
            else:
                b_extra = {
                    **(sig_b.get("basis_meta") or {}),
                    **(sig_b.get("vrp_meta") or {}),
                    "aiml_p_breach_passive": (self.latest_aiml_telemetry or {}).get("p_breach"),
                }
                self._record_opportunity_with_dedup(
                    session_date=dt,
                    bar_ts=bar_open_ts,
                    eval_hhmm=eval_hhmm,
                    decision_ts_ms=decision_ts_ms,
                    book_id=cfg_b["book_id"],
                    candidate_variant=self.book_b_params["variant_code"],
                    direction=sig_b.get("option_type"),
                    decision_status="REJECTED",
                    primary_reject_reason=sig_b.get("reject_reason"),
                    gates=sig_b.get("gates", []),
                    ind=ind,
                    extra_inputs=b_extra,
                    contract=sig_b.get("contract"),
                    quote=sig_b.get("quote"),
                    sig_obj=sig_b,
                )
        else:
            blk_reason = "; ".join(blocks) if blocks else f"SLOTS_OR_COOLDOWN ({slots['trades_today_by_book'].get(cfg_b['book_id'], 0)} trades)"
            blk_obj = {
                "bar_ts": bar_open_ts,
                "decision_ts_ms": decision_ts_ms,
                "passed": False,
                "state": "BLOCKED",
                "reject_reason": blk_reason,
                "gates": [],
                "indicators": dict(ind),
            }
            self.latest_decision_by_book[cfg_b["book_id"]] = blk_obj
            self._record_opportunity_with_dedup(
                session_date=dt,
                bar_ts=bar_open_ts,
                eval_hhmm=eval_hhmm,
                decision_ts_ms=decision_ts_ms,
                book_id=cfg_b["book_id"],
                candidate_variant=self.book_b_params["variant_code"],
                direction=None,
                decision_status="BLOCKED",
                primary_reject_reason=blk_reason,
                gates=[],
                ind=ind,
                extra_inputs={"open_basis_pts": (self.open_basis_ref or {}).get("basis_pts")},
                contract=None,
                quote=None,
                sig_obj=blk_obj,
            )

        # Refresh slot & regime arbitrator state after potential Book B entry before evaluating Book A
        if entered_b:
            slots = self.sim.active_slots_and_reserves(bar_open_ts, dt)
            self.latest_regime_arbitration = evaluate_v2_regime_arbitration(
                session_date=dt,
                ist_hhmm=eval_hhmm,
                expiries=expiries_all,
                spot_close=ind["spot_close"],
                spot_vwap=ind["tpb_vwap"],
                orb30_h=ind["orb30_h"],
                orb30_l=ind["orb30_l"],
                orb30_ready=bool(ind["orb30_ready"]),
                adx14=ind["adx14"],
                er20=ind["er20"],
                vix_now=vix_now_val,
                prior_vix_closes=self.prior_daily_vix,
                open_packages=list(self.sim.open_packages.values()),
                max_open_packages_total=int(env["max_open_packages_total"]),
                book_a_enabled=(book_a_active or book_a_intraday_allowed),
                book_b_enabled=book_b_mode_allowed,
            )

        # Book A check (strictly disabled by default unless opt-in in research replay)
        if not book_a_active:
            dis_obj = {
                "bar_ts": bar_open_ts,
                "decision_ts_ms": decision_ts_ms,
                "passed": False,
                "state": "DISABLED_UNVERIFIED",
                "reject_reason": "BLOCKED_UNVERIFIED_O3_SPEC_CONFLICT",
                "gates": [],
            }
            self.latest_decision_by_book[cfg_a["book_id"]] = dis_obj
            self._record_opportunity_with_dedup(
                session_date=dt,
                bar_ts=bar_open_ts,
                eval_hhmm=eval_hhmm,
                decision_ts_ms=decision_ts_ms,
                book_id=cfg_a["book_id"],
                candidate_variant=cfg_a["verification_status"],
                direction=None,
                decision_status="DISABLED",
                primary_reject_reason="BLOCKED_UNVERIFIED_O3_SPEC_CONFLICT",
                gates=[],
                ind=ind,
                extra_inputs={},
                contract=None,
                quote=None,
                sig_obj=dis_obj,
            )
        elif (
            not blocks
            and slots["open_total"] < int(env["max_open_packages_total"])
            and slots["trades_today_total"] < int(env["max_total_trades_per_day"])
            and slots["open_by_book"].get(cfg_a["book_id"], 0) < int(cfg_a["max_open"])
            and slots["trades_today_by_book"].get(cfg_a["book_id"], 0) < int(cfg_a.get("max_trades_day", cfg_a.get("max_trades_per_expiry", 1)))
            and decision_ts_ms >= self.sim.cooldown_until_ms.get(cfg_a["book_id"], 0)
        ):
            quotes_lk = {
                k: self.latest_opt_quotes[c["instrument_key"]]
                for k, c in self.opt_contracts.items()
                if c["instrument_key"] in self.latest_opt_quotes
            }
            expiries = expiries_all
            vix_now = vix_now_val
            sig_a = evaluate_book_a_entry_signal(
                cfg=self.cfg,
                session_date=dt,
                bar_ts=bar_open_ts,
                ist_hhmm=eval_hhmm,
                decision_ts_ms=decision_ts_ms,
                spot_close=ind["spot_close"],
                spot_vwap=ind["tpb_vwap"] or ind["spot_close"],
                vix_now=vix_now,
                prior_vix_closes=self.prior_daily_vix,
                prior_spot_closes=self.prior_daily_spots,
                expiries=expiries,
                contracts_lookup=self.opt_contracts,
                quote_lookup=quotes_lk,
                allow_unverified_in_replay=self.enable_book_a_opt_in,
            )
            self.latest_decision_by_book[cfg_a["book_id"]] = {
                "bar_ts": bar_open_ts,
                "decision_ts_ms": decision_ts_ms,
                "passed": bool(sig_a["passed"]),
                "state": sig_a.get("state"),
                "reject_reason": sig_a.get("reject_reason"),
                "gates": sig_a.get("gates", []),
            }
            if sig_a["passed"]:
                cand_dte_a = (self.latest_regime_arbitration.get("dte_by_expiry") or {}).get(str(sig_a.get("expiry") or ""))
                arb_ok_a, arb_rsn_a = check_candidate_arbitration_gate(
                    self.latest_regime_arbitration,
                    book_id=cfg_a["book_id"],
                    is_bull=bool(sig_a["is_bull"]),
                    open_total=slots["open_total"],
                    max_open_packages_total=int(env["max_open_packages_total"]),
                    book_a_enabled=book_a_active,
                    book_b_enabled=bool(cfg_b.get("autonomous_entries_enabled", True)),
                    candidate_dte=cand_dte_a,
                )
                if not arb_ok_a:
                    self._record_opportunity_with_dedup(
                        session_date=dt,
                        bar_ts=bar_open_ts,
                        eval_hhmm=eval_hhmm,
                        decision_ts_ms=decision_ts_ms,
                        book_id=cfg_a["book_id"],
                        candidate_variant=sig_a["candidate_variant"],
                        direction=sig_a.get("option_type"),
                        decision_status="BLOCKED",
                        primary_reject_reason=str(arb_rsn_a),
                        gates=sig_a.get("gates", []),
                        ind=ind,
                        extra_inputs={"est_credit_pts": sig_a.get("est_credit_pts"), "skew_ms": sig_a.get("skew_ms")},
                        contract=sig_a.get("short_contract"),
                        quote=sig_a.get("short_quote"),
                        sig_obj=sig_a,
                    )
                else:
                    sid_a = make_stable_id(
                        "osv2_sig", self.run_mode, sig_a["candidate_variant"], bar_open_ts, sig_a["short_contract"]["instrument_key"]
                    )
                    l1 = self.sim.open_book_a_leg1_hedge(
                        signal_id=sid_a,
                        candidate_variant=sig_a["candidate_variant"],
                        session_date=dt,
                        bar_ts=bar_open_ts,
                        decision_ts_ms=decision_ts_ms,
                        wing_contract=sig_a["wing_contract"],
                        short_contract=sig_a["short_contract"],
                        wing_quote=sig_a["wing_quote"],
                        spot_price=ind["spot_close"],
                        vix_val=vix_now,
                        is_bull=sig_a["is_bull"],
                    )
                    l2 = None
                    if l1.get("ok"):
                        l2 = self.sim.complete_or_unwind_pending_leg2(
                            package_id=l1["package_id"],
                            short_quote=sig_a["short_quote"],
                            wing_quote=sig_a["wing_quote"],
                            decision_ts_ms=decision_ts_ms + 250,
                            session_date=dt,
                            bar_ts=bar_open_ts,
                        )
                        if isinstance(l2, dict) and l2.get("state") == "OPEN":
                            if self.on_package_opened_cb:
                                self.on_package_opened_cb(
                                    l1["package_id"], [sig_a["wing_contract"], sig_a["short_contract"]]
                                )
                            self._emit_lifecycle_event(
                                {
                                    "event_type": "PACKAGE_OPENED",
                                    "run_mode": self.run_mode,
                                    "package_id": l1["package_id"],
                                    "signal_id": sid_a,
                                    "session_date": dt,
                                    "bar_ts": bar_open_ts,
                                    "decision_ts_ms": decision_ts_ms,
                                    "book_id": cfg_a["book_id"],
                                    "structure": "DEFINED_RISK_SPREAD",
                                    "package_snapshot": dict(self.sim.open_packages.get(l1["package_id"]) or {}),
                                    "quote_snapshot": dict(sig_a["short_quote"]),
                                    "wing_quote_snapshot": dict(sig_a["wing_quote"]),
                                }
                            )
                    self._record_opportunity_with_dedup(
                        session_date=dt,
                        bar_ts=bar_open_ts,
                        eval_hhmm=eval_hhmm,
                        decision_ts_ms=decision_ts_ms,
                        book_id=cfg_a["book_id"],
                        candidate_variant=sig_a["candidate_variant"],
                        direction=sig_a.get("option_type"),
                        decision_status="ACCEPTED" if (isinstance(l2, dict) and l2.get("state") == "OPEN") else "BLOCKED",
                        primary_reject_reason=None if (isinstance(l2, dict) and l2.get("state") == "OPEN") else str((l2 or l1).get("reason") or "LEG2_NOT_OPEN"),
                        gates=sig_a.get("gates", []),
                        ind=ind,
                        extra_inputs={"est_credit_pts": sig_a.get("est_credit_pts"), "skew_ms": sig_a.get("skew_ms")},
                        contract=sig_a.get("short_contract"),
                        quote=sig_a.get("short_quote"),
                        sig_obj=sig_a,
                    )
            else:
                self._record_opportunity_with_dedup(
                    session_date=dt,
                    bar_ts=bar_open_ts,
                    eval_hhmm=eval_hhmm,
                    decision_ts_ms=decision_ts_ms,
                    book_id=cfg_a["book_id"],
                    candidate_variant=sig_a["candidate_variant"],
                    direction=sig_a.get("option_type"),
                    decision_status="REJECTED",
                    primary_reject_reason=sig_a.get("reject_reason"),
                    gates=sig_a.get("gates", []),
                    ind=ind,
                    extra_inputs={"est_credit_pts": sig_a.get("est_credit_pts"), "skew_ms": sig_a.get("skew_ms")},
                    contract=sig_a.get("short_contract"),
                    quote=sig_a.get("short_quote"),
                    sig_obj=sig_a,
                )

        # Book A v2.4 Intraday Defined-Risk Credit Spread (Friday cal_dte in {3,4}, 11:30-13:15, 50/100w, NO_VFLIP)
        if self.enable_v2_4_dual_book:
            book_ai_id = V2_BOOK_A_INTRADAY_CREDIT_SPEC["book_id"]
            if not book_a_intraday_allowed:
                self.latest_decision_by_book[book_ai_id] = {
                    "bar_ts": bar_open_ts,
                    "decision_ts_ms": decision_ts_ms,
                    "passed": False,
                    "state": "PAUSED_BY_PAPER_MODE",
                    "reject_reason": f"PAUSED_BY_PAPER_MODE:{self.paper_execution_mode}",
                    "gates": [],
                }
            elif (
                not blocks
                and slots["open_total"] < int(env["max_open_packages_total"])
                and slots["trades_today_total"] < int(env["max_total_trades_per_day"])
                and slots["open_by_book"].get(book_ai_id, 0) < 1
                and slots["trades_today_by_book"].get(book_ai_id, 0) < 1
                and decision_ts_ms >= self.sim.cooldown_until_ms.get(book_ai_id, 0)
            ):
                sig_ai = evaluate_book_a_intraday_credit_signal(
                    cfg=self.cfg,
                    session_date=dt,
                    bar_ts=bar_open_ts,
                    ist_hhmm=eval_hhmm,
                    decision_ts_ms=decision_ts_ms,
                    spot_close=ind["spot_close"],
                    spot_vwap=ind["tpb_vwap"],
                    orb30_h=ind["orb30_h"],
                    orb30_l=ind["orb30_l"],
                    expiries=expiries_all,
                    contracts_lookup=self.opt_contracts,
                    quote_lookup=quotes_lk,
                )
                ai_extra = {
                    "est_credit_pts": sig_ai.get("est_credit_pts"),
                    "cal_dte": sig_ai.get("cal_dte"),
                    "vrp_ratio": (self.latest_vrp_telemetry or {}).get("vrp_ratio"),
                    "aiml_p_breach_passive": (self.latest_aiml_telemetry or {}).get("p_breach"),
                }
                self.latest_decision_by_book[book_ai_id] = {
                    "bar_ts": bar_open_ts,
                    "decision_ts_ms": decision_ts_ms,
                    "passed": bool(sig_ai["passed"]),
                    "state": sig_ai.get("state"),
                    "reject_reason": sig_ai.get("reject_reason"),
                    "gates": sig_ai.get("gates", []),
                    "vrp_ratio": ai_extra["vrp_ratio"],
                    "aiml_p_breach_passive": ai_extra["aiml_p_breach_passive"],
                }
                if sig_ai["passed"] and sig_ai.get("wing_contract") and sig_ai.get("short_contract"):
                    arb_ok_ai, arb_rsn_ai = check_candidate_arbitration_gate(
                        self.latest_regime_arbitration,
                        book_id=book_ai_id,
                        is_bull=bool(sig_ai["is_bull"]),
                        open_total=slots["open_total"],
                        max_open_packages_total=int(env["max_open_packages_total"]),
                        book_a_enabled=True,
                        book_b_enabled=book_b_mode_allowed,
                        candidate_dte=sig_ai.get("trading_dte"),
                        enforce_intraday_dte4_on_book_a=True,
                    )
                    if not arb_ok_ai:
                        self._record_opportunity_with_dedup(
                            session_date=dt,
                            bar_ts=bar_open_ts,
                            eval_hhmm=eval_hhmm,
                            decision_ts_ms=decision_ts_ms,
                            book_id=book_ai_id,
                            candidate_variant=sig_ai["candidate_variant"],
                            direction=sig_ai.get("option_type"),
                            decision_status="BLOCKED",
                            primary_reject_reason=str(arb_rsn_ai),
                            gates=sig_ai.get("gates", []),
                            ind=ind,
                            extra_inputs=ai_extra,
                            contract=sig_ai.get("short_contract"),
                            quote=sig_ai.get("short_quote"),
                            sig_obj=sig_ai,
                        )
                    else:
                        sid_ai = make_stable_id(
                            "osv2_sig",
                            self.run_mode,
                            sig_ai["candidate_variant"],
                            bar_open_ts,
                            sig_ai["short_contract"]["instrument_key"],
                        )
                        l1_ai = self.sim.open_book_a_leg1_hedge(
                            signal_id=sid_ai,
                            candidate_variant=sig_ai["candidate_variant"],
                            session_date=dt,
                            bar_ts=bar_open_ts,
                            decision_ts_ms=decision_ts_ms,
                            wing_contract=sig_ai["wing_contract"],
                            short_contract=sig_ai["short_contract"],
                            wing_quote=sig_ai["wing_quote"],
                            spot_price=ind["spot_close"],
                            vix_val=vix_now_val,
                            is_bull=bool(sig_ai["is_bull"]),
                            book_id_override=book_ai_id,
                        )
                        l2_ai = None
                        if l1_ai.get("ok"):
                            l2_ai = self.sim.complete_or_unwind_pending_leg2(
                                package_id=l1_ai["package_id"],
                                short_quote=sig_ai["short_quote"],
                                wing_quote=sig_ai["wing_quote"],
                                decision_ts_ms=decision_ts_ms + 250,
                                session_date=dt,
                                bar_ts=bar_open_ts,
                            )
                            if isinstance(l2_ai, dict) and l2_ai.get("state") == "OPEN":
                                if self.on_package_opened_cb:
                                    self.on_package_opened_cb(
                                        l1_ai["package_id"], [sig_ai["wing_contract"], sig_ai["short_contract"]]
                                    )
                                self._emit_lifecycle_event(
                                    {
                                        "event_type": "PACKAGE_OPENED",
                                        "run_mode": self.run_mode,
                                        "package_id": l1_ai["package_id"],
                                        "signal_id": sid_ai,
                                        "session_date": dt,
                                        "bar_ts": bar_open_ts,
                                        "decision_ts_ms": decision_ts_ms,
                                        "book_id": book_ai_id,
                                        "min_live_credit_pts": float(V2_BOOK_A_INTRADAY_CREDIT_SPEC["min_live_credit_pts"]),
                                        "structure": "DEFINED_RISK_SPREAD",
                                        "package_snapshot": dict(self.sim.open_packages.get(l1_ai["package_id"]) or {}),
                                        "quote_snapshot": dict(sig_ai["short_quote"]),
                                        "wing_quote_snapshot": dict(sig_ai["wing_quote"]),
                                    }
                                )
                        self._record_opportunity_with_dedup(
                            session_date=dt,
                            bar_ts=bar_open_ts,
                            eval_hhmm=eval_hhmm,
                            decision_ts_ms=decision_ts_ms,
                            book_id=book_ai_id,
                            candidate_variant=sig_ai["candidate_variant"],
                            direction=sig_ai.get("option_type"),
                            decision_status="ACCEPTED" if (isinstance(l2_ai, dict) and l2_ai.get("state") == "OPEN") else "BLOCKED",
                            primary_reject_reason=None if (isinstance(l2_ai, dict) and l2_ai.get("state") == "OPEN") else str((l2_ai or l1_ai).get("reason") or "LEG2_NOT_OPEN"),
                            gates=sig_ai.get("gates", []),
                            ind=ind,
                            extra_inputs=ai_extra,
                            contract=sig_ai.get("short_contract"),
                            quote=sig_ai.get("short_quote"),
                            sig_obj=sig_ai,
                        )
                else:
                    self._record_opportunity_with_dedup(
                        session_date=dt,
                        bar_ts=bar_open_ts,
                        eval_hhmm=eval_hhmm,
                        decision_ts_ms=decision_ts_ms,
                        book_id=book_ai_id,
                        candidate_variant=sig_ai["candidate_variant"],
                        direction=sig_ai.get("option_type"),
                        decision_status="REJECTED",
                        primary_reject_reason=sig_ai.get("reject_reason"),
                        gates=sig_ai.get("gates", []),
                        ind=ind,
                        extra_inputs=ai_extra,
                        contract=sig_ai.get("short_contract"),
                        quote=sig_ai.get("short_quote"),
                        sig_obj=sig_ai,
                    )

        trace_entry = {
            "bar_open_ts": bar_open_ts,
            "decision_ts_ms": decision_ts_ms,
            "indicators": ind,
            "entered_book_b": entered_b,
            "book_b_decision": sig_b_record,
        }
        self.session_decision_trace.append(trace_entry)
        self._persist_session_snapshot_locked(dt)
        return trace_entry

    def ingest_event(
        self,
        kind: str,
        symbol_or_key: str,
        payload: Dict[str, Any],
        availability_ts_ms: Optional[int] = None,
        uncertainty_blocks: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Consumes a single normalized timed event strictly chronologically.
        If `availability_ts_ms` crosses into a later bar `[t+60s, t+120s)`, finalizes
        the prior bar and runs all completed-bar decisions BEFORE applying this tick to caches.
        """
        if not isinstance(payload, dict):
            return None

        avail_ms = int(
            availability_ts_ms
            if availability_ts_ms is not None
            else (
                payload.get("availability_ts_ms")
                or payload.get("local_receipt_ts_ms")
                or payload.get("timestamp_ms")
                or 0
            )
        )
        if avail_ms <= 0:
            return None

        # Reject post-session flushes (e.g., 19:41-23:23 IST) without contaminating regular session
        avail_hhmm = ist_hhmm_from_ms(avail_ms)
        if avail_hhmm > 1530:
            if (
                self.building_bar is not None
                and avail_ms >= int(self.building_bar["bar_close_ms"])
                and 915 <= ist_hhmm_from_ms(int(self.building_bar["max_avail_ts_ms"])) <= 1530
            ):
                finalized_result = self._finalize_building_bar_and_decide(
                    trigger_ts_ms=avail_ms,
                    allow_post_session=False,
                    uncertainty_blocks=uncertainty_blocks,
                )
            else:
                finalized_result = None
            self.post_session_ignored_count += 1
            self.total_post_session_ignored_count += 1
            return finalized_result or {
                "status": "POST_SESSION_FLUSH_IGNORED",
                "availability_ts_ms": avail_ms,
            }

        if avail_hhmm < 915:
            if (
                self.building_bar is not None
                and avail_ms >= int(self.building_bar["bar_close_ms"])
                and 915 <= ist_hhmm_from_ms(int(self.building_bar["max_avail_ts_ms"])) <= 1530
            ):
                finalized_result = self._finalize_building_bar_and_decide(
                    trigger_ts_ms=avail_ms,
                    allow_post_session=False,
                    uncertainty_blocks=uncertainty_blocks,
                )
            else:
                finalized_result = None
            return finalized_result or {
                "status": "PRE_SESSION_IGNORED",
                "availability_ts_ms": avail_ms,
            }

        # Synthetic bar-close boundary check event (used at discrete archive timestamps)
        kind_u = str(kind).upper().strip()
        if kind_u == "BAR_CLOSE_CHECK":
            if self.building_bar is not None and avail_ms >= int(self.building_bar["bar_close_ms"]):
                return self._finalize_building_bar_and_decide(
                    trigger_ts_ms=avail_ms,
                    allow_post_session=False,
                    uncertainty_blocks=uncertainty_blocks,
                )
            return None

        # Determine bar bucket [bar_open, bar_close)
        if payload.get("source_bar_open_ms") is not None:
            bar_open_ms = int(payload["source_bar_open_ms"])
            session_d, bar_open_ts, _, bar_close_ms, bar_open_hhmm = bar_open_label_from_ms(bar_open_ms)
        else:
            session_d, bar_open_ts, bar_open_ms, bar_close_ms, bar_open_hhmm = bar_open_label_from_ms(avail_ms)

        # Roll session or finalize previous bar BEFORE applying incoming new-minute tick!
        finalized_result: Optional[Dict[str, Any]] = None
        if bar_open_hhmm < 915 or bar_open_hhmm > 1529:
            if (
                self.building_bar is not None
                and (
                    (self.session_date is not None and session_d > self.session_date)
                    or bar_open_ts > self.building_bar["bar_open_ts"]
                )
                and 915 <= ist_hhmm_from_ms(int(self.building_bar["max_avail_ts_ms"])) <= 1530
            ):
                finalized_result = self._finalize_building_bar_and_decide(
                    trigger_ts_ms=avail_ms,
                    allow_post_session=False,
                    uncertainty_blocks=uncertainty_blocks,
                )
            return finalized_result or {
                "status": "OUTSIDE_REGULAR_BAR_WINDOW",
                "bar_open_ts": bar_open_ts,
            }

        if self.session_date is not None and session_d > self.session_date:
            self._roll_session_if_needed(session_d, bar_open_hhmm)
        elif self.session_date is None:
            self._roll_session_if_needed(session_d, bar_open_hhmm)
        elif self.building_bar is not None and bar_open_ts > self.building_bar["bar_open_ts"]:
            finalized_result = self._finalize_building_bar_and_decide(
                trigger_ts_ms=avail_ms,
                allow_post_session=False,
                uncertainty_blocks=uncertainty_blocks,
            )

        # Reject late ticks targeting an already-finalized earlier bar cleanly
        if self.last_finalized_bar_open_ts and bar_open_ts <= self.last_finalized_bar_open_ts:
            self.late_event_count += 1
            if self.session_date:
                self._persist_session_snapshot_locked(self.session_date)
            return finalized_result

        self.last_usable_event_ts_ms = max(self.last_usable_event_ts_ms, avail_ms)

        if kind_u == "INDEX":
            if "VIX" in str(symbol_or_key).upper() or "Vix" in str(payload.get("instrument_key") or ""):
                v_px = float(payload.get("spot_ltp") or payload.get("close") or 0.0)
                if v_px > 0:
                    self.latest_vix_q = {**payload, "availability_ts_ms": avail_ms}
                    if self.building_bar is not None:
                        self.building_bar["vix_c"] = v_px
                        self.building_bar["max_avail_ts_ms"] = max(self.building_bar["max_avail_ts_ms"], avail_ms)
            elif validate_nifty_spot_identity(payload)[0]:
                s_px = float(payload.get("spot_ltp") or payload.get("close") or 0.0)
                if s_px > 0:
                    self.latest_spot_q = {**payload, "availability_ts_ms": avail_ms}
                    if self.building_bar is None:
                        self.building_bar = {
                            "session_date": session_d,
                            "bar_open_ts": bar_open_ts,
                            "bar_open_ms": bar_open_ms,
                            "bar_close_ms": bar_close_ms,
                            "bar_close_ts_ms": bar_close_ms,
                            "bar_open_hhmm": bar_open_hhmm,
                            "spot_o": s_px,
                            "spot_h": s_px,
                            "spot_l": s_px,
                            "spot_c": s_px,
                            "vix_c": float((self.latest_vix_q or {}).get("spot_ltp") or 0.0) or None,
                            "max_avail_ts_ms": avail_ms,
                        }
                    else:
                        b = self.building_bar
                        b["spot_h"] = max(b["spot_h"], s_px)
                        b["spot_l"] = min(b["spot_l"], s_px)
                        b["spot_c"] = s_px
                        b["max_avail_ts_ms"] = max(b["max_avail_ts_ms"], avail_ms)
            else:
                self.foreign_instrument_rejected_count += 1

        elif kind_u == "FUTURES":
            id_ok, id_reason = validate_nifty_futures_identity(payload)
            if not id_ok:
                rs = str(id_reason)
                if "UNVERIFIABLE_" in rs or "MISSING_FUT" in rs:
                    self.unverifiable_basis_events += 1
                else:
                    self.foreign_instrument_rejected_count += 1
            else:
                if payload.get("source_ts_proven") is False:
                    self.unverifiable_basis_events += 1
                self.latest_fut_q = {**payload, "availability_ts_ms": avail_ms}
                if self.building_bar is not None:
                    self.building_bar["max_avail_ts_ms"] = max(self.building_bar["max_avail_ts_ms"], avail_ms)

        elif kind_u == "OPTION":
            opt_ok, _ = validate_nifty_option_identity(payload)
            if not opt_ok:
                self.foreign_instrument_rejected_count += 1
            else:
                ikey = str(payload.get("instrument_key") or symbol_or_key or "").strip()
                self.latest_opt_quotes[ikey] = {**payload, "availability_ts_ms": avail_ms}
                exp = str(payload.get("expiry") or "").strip()[:10]
                stk = payload.get("strike")
                ot = str(payload.get("option_type") or "").upper().strip()
                if exp and stk is not None and ot in ("CE", "PE"):
                    self.opt_contracts[(exp, float(stk), ot)] = {
                        "instrument_key": ikey,
                        "trading_symbol": payload.get("trading_symbol") or ikey,
                        "underlying": "NIFTY",
                        "expiry": exp,
                        "strike": float(stk),
                        "option_type": ot,
                        "lot_size": int(payload.get("lot_size") or self.cfg["account_envelope"]["lot_size_default_nifty"]),
                    }
                if self.building_bar is not None:
                    self.building_bar["max_avail_ts_ms"] = max(self.building_bar["max_avail_ts_ms"], avail_ms)

        return finalized_result

    def backfill_missing_completed_bars(
        self,
        session_date: str,
        spot_candles_1m: List[Tuple[Any, float, float, float, float]],
        vix_candles_1m: Optional[List[Tuple[Any, float, float, float, float]]] = None,
        now_ms: int = 0,
    ) -> Dict[str, Any]:
        """
        Strictly causal 1-minute Spot & VIX backfill for late startups or WS reconnect gaps.
        Only inserts completed candles whose `bar_close_ms <= now_ms` and precedes `building_bar`.
        """
        if now_ms <= 0:
            return {"inserted_count": 0, "reason": "INVALID_NOW_MS"}

        if self.session_date is not None and self.session_date != session_date:
            return {"inserted_count": 0, "reason": "SESSION_MISMATCH"}

        existing_ts = {b["bar_open_ts"] for b in self.completed_bars}
        building_open = self.building_bar["bar_open_ts"] if self.building_bar is not None else None
        candidates: List[Tuple[str, float, float, float, float, int, int]] = []
        for sc in spot_candles_1m or []:
            try:
                s_ms = parse_ist_timestamp_ms(str(sc[0]))
                s_dt, ts_norm, _, bar_close_ms, s_hhmm = bar_open_label_from_ms(s_ms)
                o_px = float(sc[1])
                h_px = float(sc[2])
                l_px = float(sc[3])
                c_px = float(sc[4])
            except Exception:
                continue
            if s_dt != session_date or not ("09:15:00" <= ts_norm[11:19] <= "15:29:00"):
                continue
            if bar_close_ms > int(now_ms):
                continue
            if building_open is not None and ts_norm >= building_open:
                continue
            if ts_norm in existing_ts or o_px <= 0.0 or h_px <= 0.0 or l_px <= 0.0 or c_px <= 0.0:
                continue
            candidates.append((ts_norm, o_px, h_px, l_px, c_px, bar_close_ms, s_hhmm))

        if self.session_date is None:
            if not candidates:
                return {
                    "inserted_count": 0,
                    "bars_total": 0,
                    "orb30_ready": False,
                    "sequence_gap_count": 0,
                }
            earliest_hhmm = min(c[6] for c in candidates)
            self._roll_session_if_needed(session_date, earliest_hhmm)

        vix_close_by_ts: Dict[str, float] = {}
        for vc in vix_candles_1m or []:
            try:
                v_ms = parse_ist_timestamp_ms(str(vc[0]))
                v_dt, v_ts, _, v_close_ms, _ = bar_open_label_from_ms(v_ms)
                v_close = float(vc[4])
            except Exception:
                continue
            if (
                v_dt == session_date
                and "09:15:00" <= v_ts[11:19] <= "15:29:00"
                and v_close_ms <= int(now_ms)
                and v_close > 0.0
            ):
                vix_close_by_ts[v_ts] = v_close

        inserted = 0
        for ts_norm, o_px, h_px, l_px, c_px, bar_close_ms, _ in candidates:
            if ts_norm in existing_ts:
                continue

            row_bar = {
                "run_mode": self.run_mode,
                "config_hash": self.cfg_hash,
                "bar_open_ts": ts_norm,
                "session_date": session_date,
                "spot_o": o_px,
                "spot_h": max(o_px, h_px, l_px, c_px),
                "spot_l": min(o_px, h_px, l_px, c_px),
                "spot_c": c_px,
                "vix_c": vix_close_by_ts.get(ts_norm),
                "bar_close_ts_ms": bar_close_ms,
                "decision_ts_ms": bar_close_ms,
            }
            self.completed_bars.append(row_bar)
            existing_ts.add(ts_norm)
            if self.persist_bars_and_checkpoints:
                self.store.save_completed_bar(row_bar)
            inserted += 1

        if inserted > 0:
            self.completed_bars.sort(key=lambda b: b["bar_open_ts"])
            self.session_started_from_open = any(
                b["bar_open_ts"][11:16] == "09:15" for b in self.completed_bars
            ) or (self.building_bar is not None and int(self.building_bar["bar_open_hhmm"]) == 915)

            gap_cnt = 0
            prev_ms: Optional[int] = None
            for cb in self.completed_bars:
                cur_ms = int(cb["bar_close_ts_ms"]) - 60_000
                if prev_ms is not None and cur_ms - prev_ms > 60_000:
                    gap_cnt += int((cur_ms - prev_ms) // 60_000) - 1
                prev_ms = cur_ms
            self.sequence_gap_count = gap_cnt

            if self.completed_bars:
                self.last_finalized_bar_open_ts = self.completed_bars[-1]["bar_open_ts"]
            self.last_indicators = compute_causal_bar_indicators(self.completed_bars)
            self._persist_session_snapshot_locked(session_date)

        return {
            "inserted_count": inserted,
            "bars_total": len(self.completed_bars),
            "orb30_ready": bool(self.last_indicators.get("orb30_ready")),
            "sequence_gap_count": self.sequence_gap_count,
        }

    def flush(self, uncertainty_blocks: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """Explicit end-of-stream flush finalizing the final open building bar cleanly."""
        res = None
        if self.building_bar is not None:
            res = self._finalize_building_bar_and_decide(
                trigger_ts_ms=max(int(self.building_bar["bar_close_ms"]), int(self.building_bar["max_avail_ts_ms"])),
                allow_post_session=False,
                uncertainty_blocks=uncertainty_blocks,
            )
        if self.session_date is not None:
            self._persist_session_snapshot_locked(self.session_date)
        return res
