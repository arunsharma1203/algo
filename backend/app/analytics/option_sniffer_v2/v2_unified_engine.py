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
import os
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
from app.analytics.option_sniffer_v2.v2_book_a_engine import (
    evaluate_book_a_entry_signal,
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
)
from app.analytics.option_sniffer_v2.v2_data_contract import (
    check_spot_futures_sync,
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


def compute_causal_bar_indicators(completed_bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Shared single indicator builder for Forward & Replay from finalized session 1m bars.
    Fails closed (`None`) during indicator warmup rather than substituting synthetic proxies.
    """
    n = len(completed_bars)
    if n == 0:
        return {
            "bars_count": 0,
            "spot_close": 0.0,
            "tpb_vwap": None,
            "adx14": None,
            "er20": None,
            "orb30_h": None,
            "orb30_l": None,
            "orb30_ready": False,
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
        "orb30_h": orb_h,
        "orb30_l": orb_l,
        "orb30_ready": orb_ready,
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
        persist_bars_and_checkpoints: bool = True,
        on_package_opened_cb: Optional[Callable[[str, List[Dict[str, Any]]], None]] = None,
        on_package_closed_cb: Optional[Callable[[str], None]] = None,
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
        self.persist_bars_and_checkpoints = bool(persist_bars_and_checkpoints)
        self.on_package_opened_cb = on_package_opened_cb
        self.on_package_closed_cb = on_package_closed_cb

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

        # Intraday quotes & references (strict NIFTY identity only)
        self.open_basis_ref: Optional[Dict[str, Any]] = None
        self.latest_spot_q: Optional[Dict[str, Any]] = None
        self.latest_fut_q: Optional[Dict[str, Any]] = None
        self.latest_vix_q: Optional[Dict[str, Any]] = None
        self.latest_opt_quotes: Dict[str, Dict[str, Any]] = {}
        self.opt_contracts: Dict[Tuple[str, float, str], Dict[str, Any]] = {}
        self.fsp_by_expiry: Dict[str, float] = {}
        self.prior_daily_vix: List[float] = []
        self.prior_daily_spots: List[float] = []
        self.session_decision_trace: List[Dict[str, Any]] = []
        self.latest_decision_by_book: Dict[str, Dict[str, Any]] = {}
        self.last_indicators: Dict[str, Any] = compute_causal_bar_indicators([])

        if self.persist_bars_and_checkpoints:
            self.recover_from_db()

    def recover_from_db(self) -> None:
        """Reconstructs completed bars, indicator warmup, open basis ref, and checkpointed risk state."""
        ckpts = self.store.load_runtime_checkpoints(self.run_mode, self.cfg_hash)
        if not ckpts:
            return
        last_ck = ckpts[-1]
        dt = last_ck["session_date"]
        self.session_date = dt
        self.open_basis_ref = last_ck.get("open_basis_ref")
        self.last_usable_event_ts_ms = int(last_ck.get("last_event_ts_ms") or 0)
        self.last_finalized_bar_open_ts = last_ck.get("last_bar_open_ts")
        self.session_started_from_open = bool(last_ck.get("session_started_from_open"))
        self.sequence_gap_count = int(last_ck.get("sequence_gap_count") or 0)
        self.overflow_drop_count = int(last_ck.get("overflow_drop_count") or 0)
        bars = self.store.load_completed_bars(self.run_mode, self.cfg_hash, session_date=dt)
        self.completed_bars = [dict(b) for b in bars]
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
                self.prior_daily_spots.append(float(self.completed_bars[-1]["spot_c"]))
                v_last = next((b["vix_c"] for b in reversed(self.completed_bars) if b.get("vix_c")), None)
                if v_last:
                    self.prior_daily_vix.append(float(v_last))

        self.session_date = new_session_date
        open_mtm = self.sim.current_open_conservative_mtm()
        self.sod_equity_inr = round(self.sim.realized_equity_inr + open_mtm, 2)
        self.sim.intraday_min_equity_by_date[new_session_date] = self.sod_equity_inr
        self.sim.peak_reserved_by_date[new_session_date] = self.sim.reserved_capital_inr
        self.completed_bars = []
        self.building_bar = None
        self.last_finalized_bar_open_ts = None
        self.session_started_from_open = (bar_open_hhmm == 915)
        self.sequence_gap_count = 0
        self.overflow_drop_count = 0
        self.post_session_ignored_count = 0
        self.open_basis_ref = None
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

        # Detect minute discontinuity vs previous finalized bar in same session
        if self.last_finalized_bar_open_ts and self.last_finalized_bar_open_ts[:10] == dt:
            prev_dt = datetime.strptime(self.last_finalized_bar_open_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST_TZ)
            prev_ms = int(prev_dt.timestamp() * 1000)
            if bar_open_ms - prev_ms > 60_000:
                self.sequence_gap_count += int((bar_open_ms - prev_ms) // 60_000) - 1

        self.last_finalized_bar_open_ts = bar_open_ts
        decision_ts_ms = max(bar_close_ms, int(b["max_avail_ts_ms"]))
        dec_hhmm = ist_hhmm_from_ms(decision_ts_ms)

        if not allow_post_session and dec_hhmm > 1530:
            # Post-session EOD batch flush (e.g. 19:41 IST) cannot trigger retroactive trading or cutoffs
            self.sequence_gap_count += 1
            self._persist_session_snapshot_locked(dt)
            return {"status": "POST_SESSION_FLUSH_IGNORED", "bar_open_ts": bar_open_ts, "decision_ts_ms": decision_ts_ms}

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
        bar_open_hhmm = int(b["bar_open_hhmm"])
        eval_hhmm = ist_hhmm_from_ms(decision_ts_ms)
        if eval_hhmm < 915 or eval_hhmm > 1530:
            return {"status": "OUTSIDE_REGULAR_SESSION", "bar_open_ts": bar_open_ts}

        # Open basis reference window strictly on bars opening during 09:15..09:29 (decisions <= 09:30)
        if self.open_basis_ref is None and 915 <= bar_open_hhmm <= 929 and self.latest_spot_q and self.latest_fut_q:
            chk = check_spot_futures_sync(
                self.latest_spot_q,
                self.latest_fut_q,
                max_skew_ms=int(self.cfg["quote_hygiene"]["max_spot_fut_sync_skew_ms"]),
                max_age_ms=int(self.cfg["quote_hygiene"]["max_entry_quote_age_ms"]),
                now_ms=decision_ts_ms,
            )
            if chk.get("ok"):
                self.open_basis_ref = chk
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
        if (slots["realized_today_inr"] + mtm_info["conservative_unrealized_mtm_inr"]) <= kill_lim:
            self.sim.daily_kill_triggered_by_date[dt] = f"DAILY_KILL_MTM_LE_{kill_lim}"

        # 2. Manage open packages
        for pid, pkg in list(self.sim.open_packages.items()):
            res_close = None
            if pkg["book_id"] == "V2_BOOK_B_PAID_FLOW":
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
                    stagnation_mins_override=self.book_b_params.get("stagnation_mins_override"),
                    stagnation_min_gain_override=self.book_b_params.get("stagnation_min_gain_override"),
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

        # 3. Evaluate new entries strictly after exits (`active_slots_and_reserves` locks same-bar exit slots)
        slots = self.sim.active_slots_and_reserves(bar_open_ts, dt)
        env = self.cfg["account_envelope"]
        cfg_b = self.cfg["books"]["V2_BOOK_B_PAID_FLOW"]
        cfg_a = self.cfg["books"]["V2_BOOK_A_PROPOSED_O3"]
        entered_b = False
        sig_b_record: Optional[Dict[str, Any]] = None

        blocks = list(uncertainty_blocks or [])
        if self.sim.is_booking_halted():
            blocks.append("BOOKING_FAILURE_HALTED")
        if dt in self.sim.daily_kill_triggered_by_date:
            blocks.append(self.sim.daily_kill_triggered_by_date[dt])
        if mtm_info["unpriced_count"] > 0:
            blocks.append("UNPRICED_OPEN_EXPOSURE")

        if (
            not blocks
            and slots["open_total"] < int(env["max_open_packages_total"])
            and slots["trades_today_total"] < int(env["max_total_trades_per_day"])
            and slots["open_by_book"].get(cfg_b["book_id"], 0) < int(cfg_b["max_open"])
            and slots["trades_today_by_book"].get(cfg_b["book_id"], 0) < int(cfg_b["max_trades_day"])
            and decision_ts_ms >= self.sim.cooldown_until_ms.get(cfg_b["book_id"], 0)
        ):
            quotes_lk = {
                k: self.latest_opt_quotes[c["instrument_key"]]
                for k, c in self.opt_contracts.items()
                if c["instrument_key"] in self.latest_opt_quotes
            }
            expiries = sorted({k[0] for k in self.opt_contracts.keys()})
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
        else:
            blk_reason = "; ".join(blocks) if blocks else f"SLOTS_OR_COOLDOWN ({slots['trades_today_by_book'].get(cfg_b['book_id'], 0)} trades)"
            self.latest_decision_by_book[cfg_b["book_id"]] = {
                "bar_ts": bar_open_ts,
                "decision_ts_ms": decision_ts_ms,
                "passed": False,
                "state": "BLOCKED",
                "reject_reason": blk_reason,
                "gates": [],
                "indicators": dict(ind),
            }

        # Book A check (strictly disabled by default unless opt-in in research replay)
        if not cfg_a.get("autonomous_entries_enabled", False) and not self.enable_book_a_opt_in:
            self.latest_decision_by_book[cfg_a["book_id"]] = {
                "bar_ts": bar_open_ts,
                "decision_ts_ms": decision_ts_ms,
                "passed": False,
                "state": "DISABLED_UNVERIFIED",
                "reject_reason": "BLOCKED_UNVERIFIED_O3_SPEC_CONFLICT",
                "gates": [],
            }
        elif (
            not blocks
            and slots["open_by_book"].get(cfg_a["book_id"], 0) < int(cfg_a["max_open"])
            and decision_ts_ms >= self.sim.cooldown_until_ms.get(cfg_a["book_id"], 0)
        ):
            quotes_lk = {
                k: self.latest_opt_quotes[c["instrument_key"]]
                for k, c in self.opt_contracts.items()
                if c["instrument_key"] in self.latest_opt_quotes
            }
            expiries = sorted({k[0] for k in self.opt_contracts.keys()})
            vix_now = float((self.latest_vix_q or {}).get("spot_ltp") or (b.get("vix_c") or 0.0)) or None
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
                if l1.get("ok"):
                    self.sim.complete_or_unwind_pending_leg2(
                        package_id=l1["package_id"],
                        short_quote=sig_a["short_quote"],
                        wing_quote=sig_a["wing_quote"],
                        decision_ts_ms=decision_ts_ms + 250,
                        session_date=dt,
                        bar_ts=bar_open_ts,
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
                and ist_hhmm_from_ms(int(self.building_bar["max_avail_ts_ms"])) <= 1530
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
