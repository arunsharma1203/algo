"""
V2 Option Sniffer — Local Paper-Only Simulator & Atomic State Machine.

Guarantees:
  - Strict paper-only execution: ONLY local simulation backed by `options_accounting.py`.
    Zero broker client imports, zero order placement/modification/cancellation, zero live toggles.
  - Buy-First 2-Leg Spread Execution: confirms protective long hedge fill FIRST, supports
    partial fills rounded down to lot multiples (`confirmed_hedge_qty`), rejects Leg-2 if
    `short_fill - actual_hedge_fill < min_net_credit_pts` at the worst swept/limit level,
    and cleanly unwinds confirmed hedge leg (or holds `HEDGE_ONLY_EXPOSURE_OPEN` if unpriced).
  - Overnight & Cutoff Accounting: tracks entry-day MTM, overnight opening gap at `T0 09:15`,
    and expiry intraday P&L separately; persists unresolved exposure past `14:45` rather than
    manufacturing cutoff fills.
  - Synchronized Executable MTM: never drops unpriced risk to Rs 0 or creates phantom profit.
  - Crash & Restart Recovery: reloads exact open/pending/unresolved packages and cash/reserves.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.common.options_accounting import (
    OptionLeg,
    compute_package_accounting,
    round_to_nse_tick,
)
from app.analytics.option_sniffer_v2.v2_config import (
    validate_v2_paper_invariants,
)
from app.analytics.option_sniffer_v2.v2_data_contract import (
    check_cross_leg_skew,
    resolve_strict_quote_age_ms,
    sweep_observed_l5_book,
)
from app.analytics.option_sniffer_v2.v2_ledger_db import V2LedgerStore

OPEN_EXPOSURE_STATES = (
    "PENDING_LEG2_SHORT",
    "OPEN",
    "HEDGE_ONLY_EXPOSURE_OPEN",
    "UNRESOLVED_EXPOSURE_PAST_1445",
)


def make_stable_id(prefix: str, *parts: Any) -> str:
    """Generates a deterministic, content-addressed ID for signal/intent/fill/package/ledger."""
    raw = "|".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def compute_chronological_drawdown(
    marked_equity_series: List[float],
    starting_capital_inr: float = 200000.0,
) -> Dict[str, Any]:
    """
    Computes true chronological peak-to-trough maximum drawdown across all observed marked equity points.
    Reports rupee DD, % of preregistered starting capital, and % of running peak equity.
    """
    start_cap = max(1.0, float(starting_capital_inr))
    peak = start_cap
    max_dd = 0.0
    max_dd_pct_peak = 0.0
    for val in marked_equity_series:
        v = float(val)
        if v > peak:
            peak = v
        dd = round(peak - v, 2)
        if dd > max_dd:
            max_dd = dd
        pct_p = round(100.0 * dd / max(1.0, peak), 2)
        if pct_p > max_dd_pct_peak:
            max_dd_pct_peak = pct_p
    return {
        "running_peak_equity_inr": round(peak, 2),
        "max_dd_inr": round(max_dd, 2),
        "max_dd_pct_of_starting_capital": round(100.0 * max_dd / start_cap, 2),
        "max_dd_pct_of_peak_equity": round(max_dd_pct_peak, 2),
    }


def _append_timeline(pkg: Dict[str, Any], ts_ms: int, bar_ts: str, event: str, detail: str) -> List[Dict[str, Any]]:
    cur = list(pkg.get("events_timeline") or [])
    cur.append({"ts_ms": int(ts_ms), "bar_ts": str(bar_ts), "event": str(event), "detail": str(detail)})
    return cur


class V2PaperSimulator:
    """
    Deterministic, paper-only multi-book options execution & mark-to-market state machine.
    """

    SIMULATION_ONLY: bool = True
    LIVE_CAPABLE: bool = False

    def __init__(
        self,
        config: Dict[str, Any],
        config_hash: str,
        store: V2LedgerStore,
        run_mode: str = "FORWARD_PAPER",
        cost_multiplier: float = 1.0,
        extra_adverse_slippage_ticks: int = 0,
    ) -> None:
        validate_v2_paper_invariants(config)
        self._lock = threading.RLock()
        self.config = config
        self.config_hash = str(config_hash)
        self.store = store
        self.run_mode = str(run_mode)
        self.cost_multiplier = max(1.0, float(cost_multiplier))
        self.extra_adverse_slippage_ticks = max(0, int(extra_adverse_slippage_ticks))

        env = config["account_envelope"]
        self.starting_capital_inr = float(env["starting_capital_inr"])
        self.realized_equity_inr: float = self.starting_capital_inr
        self.reserved_capital_inr: float = 0.0
        self.running_peak_equity_inr: float = self.starting_capital_inr
        self.max_mtm_dd_inr: float = 0.0
        self.equity_seq_no: int = 0

        self.open_packages: Dict[str, Dict[str, Any]] = {}
        self.locked_until_after_bar: Dict[str, List[Tuple[str, float]]] = {}
        self.cooldown_until_ms: Dict[str, int] = {}
        self.seen_signal_ids: set[str] = set()
        self.daily_kill_triggered_by_date: Dict[str, str] = {}
        self.intraday_min_equity_by_date: Dict[str, float] = {}
        self.peak_reserved_by_date: Dict[str, float] = {}
        self.subscription_evicted_keys: set[str] = set()
        self._all_pkgs_by_id: Dict[str, Dict[str, Any]] = {}
        self.booking_fault: Optional[Dict[str, Any]] = None
        self.blocked_books_due_to_fault: set[str] = set()

        self.recover_state_from_db()

    def recover_state_from_db(self) -> Dict[str, Any]:
        """Restores exact equity, reserves, packages, drawdown peak/trough, kills, cooldowns, and bar locks."""
        with self._lock:
            self.open_packages.clear()
            self.seen_signal_ids.clear()
            self._all_pkgs_by_id.clear()
            self.daily_kill_triggered_by_date.clear()
            self.cooldown_until_ms.clear()
            self.locked_until_after_bar.clear()
            self.intraday_min_equity_by_date.clear()
            self.peak_reserved_by_date.clear()
            self.booking_fault = None
            self.blocked_books_due_to_fault.clear()

            entries = self.store.load_ledger_entries(self.run_mode, self.config_hash)
            if entries:
                last_e = entries[-1]
                self.realized_equity_inr = float(last_e["realized_equity_after_inr"])
                self.reserved_capital_inr = float(last_e["reserved_after_inr"])
            else:
                self.realized_equity_inr = self.starting_capital_inr
                self.reserved_capital_inr = 0.0

            all_pkgs = self.store.load_packages(self.run_mode, self.config_hash)
            sum_open_res = 0.0
            for pkg in all_pkgs:
                self._all_pkgs_by_id[pkg["package_id"]] = pkg
                self.seen_signal_ids.add(pkg["signal_id"])
                if pkg["state"] in OPEN_EXPOSURE_STATES:
                    self.open_packages[pkg["package_id"]] = pkg
                    sum_open_res += float(pkg.get("reserved_capital_inr") or 0.0)
            self.reserved_capital_inr = round(sum_open_res, 2)

            eq_curve = self.store.load_equity_curve(self.run_mode, self.config_hash)
            if eq_curve:
                last_pt = eq_curve[-1]
                self.equity_seq_no = int(last_pt["seq_no"])
                self.running_peak_equity_inr = float(last_pt["running_peak_equity_inr"])
                self.max_mtm_dd_inr = max(float(pt["drawdown_inr"]) for pt in eq_curve)
            else:
                self.equity_seq_no = 0
                self.running_peak_equity_inr = max(self.starting_capital_inr, self.realized_equity_inr)
                self.max_mtm_dd_inr = 0.0

            ckpts = self.store.load_runtime_checkpoints(self.run_mode, self.config_hash)
            for ck in ckpts:
                dt_c = ck["session_date"]
                if ck.get("daily_kill_reason"):
                    self.daily_kill_triggered_by_date[dt_c] = str(ck["daily_kill_reason"])
                for bid, ts_u in (ck.get("cooldown") or {}).items():
                    self.cooldown_until_ms[str(bid)] = max(self.cooldown_until_ms.get(str(bid), 0), int(ts_u))
                for b_k, items in (ck.get("bar_locks") or {}).items():
                    self.locked_until_after_bar[str(b_k)] = [(str(x[0]), float(x[1])) for x in items]
                self.intraday_min_equity_by_date[dt_c] = float(ck.get("intraday_min_mtm_equity_inr") or self.realized_equity_inr)
                self.peak_reserved_by_date[dt_c] = float(ck.get("peak_reserved_inr") or self.reserved_capital_inr)
                self.running_peak_equity_inr = max(self.running_peak_equity_inr, float(ck.get("running_peak_equity_inr") or self.starting_capital_inr))
                self.max_mtm_dd_inr = max(self.max_mtm_dd_inr, float(ck.get("max_mtm_dd_inr") or 0.0))
                if ck.get("booking_fault"):
                    self.booking_fault = ck["booking_fault"]
                    self.blocked_books_due_to_fault.add(str(ck["booking_fault"].get("book_id") or "PORTFOLIO"))

            return {
                "recovered_open_count": len(self.open_packages),
                "realized_equity_inr": self.realized_equity_inr,
                "reserved_capital_inr": self.reserved_capital_inr,
                "running_peak_equity_inr": self.running_peak_equity_inr,
                "max_mtm_dd_inr": self.max_mtm_dd_inr,
            }

    def _build_equity_point(
        self,
        session_date: str,
        ts_ms: int,
        event_kind: str,
        realized_eq: float,
        conservative_mtm: float,
    ) -> Dict[str, Any]:
        marked_eq = round(realized_eq + conservative_mtm, 2)
        new_peak = max(self.running_peak_equity_inr, marked_eq)
        cur_dd = round(max(0.0, new_peak - marked_eq), 2)
        self.running_peak_equity_inr = new_peak
        if cur_dd > self.max_mtm_dd_inr:
            self.max_mtm_dd_inr = cur_dd
        self.equity_seq_no += 1
        return {
            "run_mode": self.run_mode,
            "config_hash": self.config_hash,
            "seq_no": self.equity_seq_no,
            "session_date": session_date,
            "ts_ms": int(ts_ms),
            "event_kind": str(event_kind),
            "realized_equity_inr": round(realized_eq, 2),
            "conservative_mtm_inr": round(conservative_mtm, 2),
            "marked_equity_inr": marked_eq,
            "running_peak_equity_inr": round(self.running_peak_equity_inr, 2),
            "drawdown_inr": cur_dd,
        }

    def _commit_atomic_or_fault(
        self,
        book_id: str,
        op_name: str,
        decision_ts_ms: int,
        intents: List[Dict[str, Any]],
        fills: List[Dict[str, Any]],
        pkg_upsert: Optional[Dict[str, Any]],
        ledger_entry: Optional[Dict[str, Any]],
        equity_point: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Executes atomic SQLite commit; latches booking fault & blocks future entries on DB error."""
        try:
            ok = self.store.execute_atomic_transition(
                intents, fills, pkg_upsert, ledger_entry, equity_point=equity_point
            )
            if not ok:
                return False, "DUPLICATE_LEDGER_TRANSITION"
            return True, None
        except Exception as exc:
            self.booking_fault = {
                "fault_ts_ms": int(decision_ts_ms),
                "book_id": str(book_id),
                "operation": str(op_name),
                "error": f"{type(exc).__name__}: {exc}",
            }
            self.blocked_books_due_to_fault.add(str(book_id))
            self.blocked_books_due_to_fault.add("PORTFOLIO")
            return False, f"BOOKING_FAILURE_HALTED:{type(exc).__name__}"

    def reconcile_after_booking_fault(self) -> Dict[str, Any]:
        """Re-synchronizes memory from SQLite and clears booking fault only after clean DB reload."""
        with self._lock:
            rec = self.recover_state_from_db()
            self.booking_fault = None
            self.blocked_books_due_to_fault.clear()
            return {"ok": True, **rec}

    def is_booking_halted(self, book_id: Optional[str] = None) -> bool:
        with self._lock:
            if not self.booking_fault:
                return False
            if "PORTFOLIO" in self.blocked_books_due_to_fault:
                return True
            if book_id and book_id in self.blocked_books_due_to_fault:
                return True
            return True

    def current_open_conservative_mtm(self) -> float:
        tot = 0.0
        for p in self.open_packages.values():
            if p.get("is_unpriced_exposure"):
                tot += -abs(float(p.get("structural_max_loss_inr") or 0.0))
            else:
                tot += float(p.get("last_mark_net_pnl_inr") or 0.0)
        return round(tot, 2)

    def mark_key_evicted(self, instrument_key: str) -> None:
        with self._lock:
            self.subscription_evicted_keys.add(str(instrument_key))

    def _effective_slippage_ticks(self) -> int:
        base_ticks = int(self.config["quote_hygiene"]["adverse_latency_ticks"])
        return base_ticks + self.extra_adverse_slippage_ticks

    def _apply_cost_multiplier(self, acct: Dict[str, Any]) -> Dict[str, Any]:
        """Applies `cost_multiplier` (e.g. `2.0` for 2x charges stress) strictly preserving net = gross - friction."""
        if abs(self.cost_multiplier - 1.0) < 1e-9:
            return acct
        out = dict(acct)
        gross = float(acct["gross_pnl"])
        base_fric = float(acct["total_friction"])
        scaled_fric = round(base_fric * self.cost_multiplier, 2)
        out["brokerage"] = round(float(acct["brokerage"]) * self.cost_multiplier, 2)
        out["stt"] = round(float(acct["stt"]) * self.cost_multiplier, 2)
        out["statutory_charges"] = round(float(acct["statutory_charges"]) * self.cost_multiplier, 2)
        out["total_friction"] = scaled_fric
        out["net_pnl"] = round(gross - scaled_fric, 2)
        return out

    def active_slots_and_reserves(self, eval_bar_ts: str, session_date: str) -> Dict[str, Any]:
        """Strict `bar[t].open < bar[t].close` causality: released slots stay locked until strictly later bars."""
        with self._lock:
            stale = [b for b in self.locked_until_after_bar if b < eval_bar_ts]
            for b in stale:
                self.locked_until_after_bar.pop(b, None)

            open_by_book: Dict[str, int] = {}
            extra_locked_res = 0.0
            for pkg in self.open_packages.values():
                bid = pkg["book_id"]
                open_by_book[bid] = open_by_book.get(bid, 0) + 1

            for bar_k, items in self.locked_until_after_bar.items():
                if bar_k >= eval_bar_ts:
                    for bid, locked_amt in items:
                        open_by_book[bid] = open_by_book.get(bid, 0) + 1
                        extra_locked_res += float(locked_amt)

            closed_today = [
                p
                for p in self._all_pkgs_by_id.values()
                if p["session_date"] == session_date
            ]
            trades_today_by_book: Dict[str, int] = {}
            realized_today = 0.0
            for p in closed_today:
                bid = p["book_id"]
                trades_today_by_book[bid] = trades_today_by_book.get(bid, 0) + 1
                if p["state"] not in OPEN_EXPOSURE_STATES and (p.get("exit_bar_ts") or "")[:10] == session_date:
                    realized_today += float(p.get("net_pnl_inr") or 0.0)

            return {
                "open_total": sum(open_by_book.values()),
                "open_by_book": open_by_book,
                "trades_today_total": len(closed_today),
                "trades_today_by_book": trades_today_by_book,
                "effective_reserved_inr": round(self.reserved_capital_inr + extra_locked_res, 2),
                "realized_today_inr": round(realized_today, 2),
            }

    # ──────────────────────────────────────────────────────────────────────────
    # BOOK B: PAID 1-LOT LONG OPTION ENTRY
    # ──────────────────────────────────────────────────────────────────────────

    def open_book_b_paid_long(
        self,
        signal_id: str,
        candidate_variant: str,
        session_date: str,
        bar_ts: str,
        decision_ts_ms: int,
        contract: Dict[str, Any],
        quote: Dict[str, Any],
        spot_price: float,
        basis_meta: Dict[str, Any],
        used_single_otm_fallback: bool = False,
        allow_partial_lot: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            if self.is_booking_halted(self.config["books"]["V2_BOOK_B_PAID_FLOW"]["book_id"]):
                return {"ok": False, "reason": "BLOCKED_BY_BOOKING_FAULT", "fault": self.booking_fault}
            if signal_id in self.seen_signal_ids:
                return {"ok": False, "reason": "DUPLICATE_SIGNAL_SUPPRESSED"}

            hyg = self.config["quote_hygiene"]
            cfg_b = self.config["books"]["V2_BOOK_B_PAID_FLOW"]
            lot_sz = int(contract["lot_size"])
            target_qty = int(cfg_b["lot_count"]) * lot_sz

            sw = sweep_observed_l5_book(
                quote=quote,
                side="BUY",
                target_qty=target_qty,
                lot_size=lot_sz,
                adverse_ticks=self._effective_slippage_ticks(),
                max_age_ms=int(hyg["max_entry_quote_age_ms"]),
                now_ms=decision_ts_ms,
                max_spread_pct=float(hyg["max_primary_spread_pct"]),
                require_observed_l5=bool(hyg["require_observed_l5_book_for_entry"]),
                allow_partial_lot_multiples=allow_partial_lot,
            )
            if not sw.get("ok") and not (allow_partial_lot and int(sw.get("confirmed_lot_qty") or 0) > 0):
                return {"ok": False, "reason": f"BOOK_B_SWEEP_REJECTED:{sw.get('reason')}", "sweep": sw}

            filled_qty = int(sw.get("confirmed_lot_qty") or target_qty)
            entry_px = float(sw["fill_price"])
            outlay_inr = round(entry_px * filled_qty, 2)
            if outlay_inr > float(cfg_b["max_entry_outlay_inr"]):
                return {"ok": False, "reason": "EXCEEDS_CASH_OUTLAY_CAP", "outlay_inr": outlay_inr}

            env = self.config["account_envelope"]
            if self.reserved_capital_inr + outlay_inr > float(env["max_capital_reserved_inr"]):
                return {"ok": False, "reason": "MAX_RESERVED_CAPITAL_REACHED"}

            # Planned loss = min(20% of filled premium outlay, Rs 1,800) with adverse_buy stop tick so
            # planned gross loss <= budget (never exceeded merely by adverse rounding).
            pct_budget = outlay_inr * (float(cfg_b["planned_loss_max_pct_of_outlay"]) / 100.0)
            planned_loss_inr = round(min(pct_budget, float(cfg_b["planned_loss_max_inr"])), 2)
            raw_stop = max(0.05, entry_px - (planned_loss_inr / float(filled_qty)))
            hard_stop_px = round_to_nse_tick(raw_stop, mode="adverse_buy", allow_zero_settlement=False)
            planned_stop_loss_inr = round((entry_px - hard_stop_px) * filled_qty, 2)

            acct_preview = self._apply_cost_multiplier(
                compute_package_accounting(
                    legs=[OptionLeg(contract["option_type"], float(contract["strike"]), filled_qty, entry_px, entry_px, lot_size=lot_sz)],
                    trade_date=session_date,
                    reference_spot=spot_price,
                )
            )
            struct_loss_inr = float(acct_preview["risk"]["final_package_max_loss_after_friction_inr"])

            pkg_id = make_stable_id("osv2_pkg", signal_id, contract["instrument_key"], bar_ts)
            intent_id = make_stable_id("osv2_int", pkg_id, "LONG_LEG", "BUY")
            fill_id = make_stable_id("osv2_fil", intent_id, sw["ts_ms"])

            intent_row = {
                "intent_id": intent_id,
                "signal_id": signal_id,
                "package_id": pkg_id,
                "run_mode": self.run_mode,
                "config_hash": self.config_hash,
                "leg_role": "LONG_LEG",
                "instrument_key": contract["instrument_key"],
                "side": "BUY",
                "target_qty": target_qty,
                "limit_price": entry_px,
                "filled_qty": filled_qty,
                "state": "FILLED" if filled_qty == target_qty else "PARTIAL_FILLED",
                "reject_reason": None,
                "created_ts_ms": decision_ts_ms,
                "updated_ts_ms": decision_ts_ms,
            }
            fill_row = {
                "fill_id": fill_id,
                "intent_id": intent_id,
                "package_id": pkg_id,
                "run_mode": self.run_mode,
                "config_hash": self.config_hash,
                "instrument_key": contract["instrument_key"],
                "side": "BUY",
                "qty": filled_qty,
                "fill_price": entry_px,
                "raw_top_price": float(sw["top_price"]),
                "vwap_raw": float(sw["vwap_raw"]),
                "slippage_pts": float(sw["depth_slippage_pts"]),
                "fill_fidelity": sw["fill_fidelity"],
                "exchange_ts_ms": int(quote.get("exchange_ltt_ms") or sw["ts_ms"]),
                "receipt_ts_ms": int(quote.get("local_receipt_ts_ms") or sw["ts_ms"]),
                "decision_ts_ms": decision_ts_ms,
            }
            new_reserved = round(self.reserved_capital_inr + outlay_inr, 2)
            legs_state = {
                "mode": "SINGLE_PAID_LONG",
                "long_leg": {
                    "instrument_key": contract["instrument_key"],
                    "trading_symbol": contract.get("trading_symbol") or contract["instrument_key"],
                    "expiry": contract["expiry"],
                    "strike": float(contract["strike"]),
                    "option_type": contract["option_type"],
                    "lot_size": lot_sz,
                    "qty": filled_qty,
                    "entry_price": entry_px,
                    "entry_sweep": sw,
                },
                "best_close_pts": entry_px,
                "ratchet_stop": None,
                "hard_stop": hard_stop_px,
                "planned_stop_loss_inr": planned_stop_loss_inr,
                "is_bull": contract["option_type"] == "CE",
                "used_single_otm_fallback": bool(used_single_otm_fallback),
                "basis_meta": basis_meta,
            }
            pkg_row = {
                "package_id": pkg_id,
                "signal_id": signal_id,
                "run_mode": self.run_mode,
                "config_hash": self.config_hash,
                "book_id": cfg_b["book_id"],
                "candidate_variant": candidate_variant,
                "session_date": session_date,
                "expiry": contract["expiry"],
                "state": "OPEN",
                "fill_fidelity": sw["fill_fidelity"],
                "direction_label": f"BUY_{contract['option_type']}",
                "entry_bar_ts": bar_ts,
                "entry_ts_ms": decision_ts_ms,
                "leg2_confirmed_ts_ms": decision_ts_ms,
                "exit_bar_ts": None,
                "exit_ts_ms": None,
                "exit_reason": None,
                "legs_state": legs_state,
                "accounting": None,
                "reserved_capital_inr": outlay_inr,
                "structural_max_loss_inr": struct_loss_inr,
                "planned_loss_inr": planned_stop_loss_inr,
                "actual_gross_fill_loss_inr": 0.0,
                "slippage_and_gap_overshoot_inr": 0.0,
                "gross_pnl_inr": 0.0,
                "brokerage_inr": 0.0,
                "stt_inr": 0.0,
                "statutory_charges_inr": 0.0,
                "total_friction_inr": 0.0,
                "net_pnl_inr": 0.0,
                "entry_day_mtm_pnl_inr": 0.0,
                "overnight_gap_pnl_inr": 0.0,
                "expiry_intraday_pnl_inr": 0.0,
                "worst_gap_loss_inr": 0.0,
                "is_unpriced_exposure": 0,
                "unpriced_reason": None,
                "unpriced_since_ts_ms": None,
                "last_mark_net_pnl_inr": 0.0,
                "last_mark_ts_ms": decision_ts_ms,
                "last_reliable_mark_net_inr": 0.0,
                "last_reliable_mark_ts_ms": decision_ts_ms,
                "events_timeline": [
                    {
                        "ts_ms": int(decision_ts_ms),
                        "bar_ts": str(bar_ts),
                        "event": "OPEN_BOOK_B",
                        "detail": f"Bought {filled_qty} {contract.get('trading_symbol') or contract['instrument_key']} @ Rs {entry_px:.2f} (SL Rs {hard_stop_px:.2f})",
                    }
                ],
                "transition_seq": 1,
            }
            ledger_entry = {
                "entry_id": make_stable_id("osv2_le", pkg_id, 1, "OPEN_RESERVE"),
                "package_id": pkg_id,
                "transition_seq": 1,
                "run_mode": self.run_mode,
                "config_hash": self.config_hash,
                "ts_ms": decision_ts_ms,
                "session_date": session_date,
                "event_type": "OPEN_RESERVE",
                "cash_delta_inr": 0.0,
                "reserved_delta_inr": outlay_inr,
                "realized_equity_after_inr": self.realized_equity_inr,
                "reserved_after_inr": new_reserved,
            }
            committed, err_rsn = self._commit_atomic_or_fault(
                cfg_b["book_id"], "OPEN_BOOK_B", decision_ts_ms, [intent_row], [fill_row], pkg_row, ledger_entry
            )
            if not committed:
                return {"ok": False, "reason": err_rsn}

            self.reserved_capital_inr = new_reserved
            self.open_packages[pkg_id] = pkg_row
            self._all_pkgs_by_id[pkg_id] = pkg_row
            self.seen_signal_ids.add(signal_id)
            self.peak_reserved_by_date[session_date] = max(
                self.peak_reserved_by_date.get(session_date, 0.0), self.reserved_capital_inr
            )
            return {"ok": True, "package_id": pkg_id, "entry_price": entry_px, "filled_qty": filled_qty}

    # ──────────────────────────────────────────────────────────────────────────
    # BOOK A: BUY-FIRST LEG-1 HEDGE -> LEG-2 SHORT CREDIT SPREAD OR UNWIND
    # ──────────────────────────────────────────────────────────────────────────

    def open_book_a_leg1_hedge(
        self,
        signal_id: str,
        candidate_variant: str,
        session_date: str,
        bar_ts: str,
        decision_ts_ms: int,
        wing_contract: Dict[str, Any],
        short_contract: Dict[str, Any],
        wing_quote: Dict[str, Any],
        spot_price: float,
        vix_val: Optional[float],
        is_bull: bool,
        allow_partial_lot: bool = True,
    ) -> Dict[str, Any]:
        with self._lock:
            if self.is_booking_halted(self.config["books"]["V2_BOOK_A_PROPOSED_O3"]["book_id"]):
                return {"ok": False, "reason": "BLOCKED_BY_BOOKING_FAULT", "fault": self.booking_fault}
            if signal_id in self.seen_signal_ids:
                return {"ok": False, "reason": "DUPLICATE_SIGNAL_SUPPRESSED"}

            hyg = self.config["quote_hygiene"]
            cfg_a = self.config["books"]["V2_BOOK_A_PROPOSED_O3"]
            env = self.config["account_envelope"]
            lot_sz = int(wing_contract["lot_size"])
            if int(short_contract["lot_size"]) != lot_sz:
                return {"ok": False, "reason": "LOT_SIZE_MISMATCH_ACROSS_LEGS"}
            target_qty = int(cfg_a["lot_count"]) * lot_sz

            sw_w = sweep_observed_l5_book(
                quote=wing_quote,
                side="BUY",
                target_qty=target_qty,
                lot_size=lot_sz,
                adverse_ticks=self._effective_slippage_ticks(),
                max_age_ms=int(hyg["max_entry_quote_age_ms"]),
                now_ms=decision_ts_ms,
                max_spread_pct=float(hyg["max_wing_spread_pct"]),
                max_spread_pts=float(hyg["max_wing_spread_pts"]),
                require_observed_l5=bool(hyg["require_observed_l5_book_for_entry"]),
                allow_partial_lot_multiples=allow_partial_lot,
            )
            confirmed_qty = int(sw_w.get("confirmed_lot_qty") or 0)
            if confirmed_qty <= 0 or (not sw_w.get("ok") and not sw_w.get("partial_fill")):
                return {"ok": False, "reason": f"LEG1_HEDGE_REJECTED:{sw_w.get('reason')}", "sweep": sw_w}

            # If partial fill filled sub-lot contracts (< 1 lot) that cannot back a whole lot short, reject fail-closed
            wing_px = float(sw_w["fill_price"])
            margin_res = max(
                float(cfg_a["fallback_unverified_margin_reserve_inr"]) * (confirmed_qty / 65.0),
                float(cfg_a["spread_width_pts"]) * confirmed_qty,
            )
            if self.reserved_capital_inr + margin_res > float(env["max_capital_reserved_inr"]):
                return {"ok": False, "reason": "MAX_RESERVED_CAPITAL_REACHED"}

            pkg_id = make_stable_id("osv2_pkg", signal_id, wing_contract["instrument_key"], short_contract["instrument_key"], bar_ts)
            intent_id = make_stable_id("osv2_int", pkg_id, "WING_LEG", "BUY")
            fill_id = make_stable_id("osv2_fil", intent_id, sw_w["ts_ms"])

            intent_row = {
                "intent_id": intent_id,
                "signal_id": signal_id,
                "package_id": pkg_id,
                "run_mode": self.run_mode,
                "config_hash": self.config_hash,
                "leg_role": "WING_LEG",
                "instrument_key": wing_contract["instrument_key"],
                "side": "BUY",
                "target_qty": target_qty,
                "limit_price": wing_px,
                "filled_qty": confirmed_qty,
                "state": "FILLED" if confirmed_qty == target_qty else "PARTIAL_FILLED_LOT_CAPPED",
                "reject_reason": None if confirmed_qty == target_qty else "PARTIAL_FILL_ONLY_CONFIRMED_HEDGE_BACKS_SHORT",
                "created_ts_ms": decision_ts_ms,
                "updated_ts_ms": decision_ts_ms,
            }
            fill_row = {
                "fill_id": fill_id,
                "intent_id": intent_id,
                "package_id": pkg_id,
                "run_mode": self.run_mode,
                "config_hash": self.config_hash,
                "instrument_key": wing_contract["instrument_key"],
                "side": "BUY",
                "qty": confirmed_qty,
                "fill_price": wing_px,
                "raw_top_price": float(sw_w["top_price"]),
                "vwap_raw": float(sw_w["vwap_raw"]),
                "slippage_pts": float(sw_w["depth_slippage_pts"]),
                "fill_fidelity": sw_w["fill_fidelity"],
                "exchange_ts_ms": int(wing_quote.get("exchange_ltt_ms") or sw_w["ts_ms"]),
                "receipt_ts_ms": int(wing_quote.get("local_receipt_ts_ms") or sw_w["ts_ms"]),
                "decision_ts_ms": decision_ts_ms,
            }
            new_reserved = round(self.reserved_capital_inr + margin_res, 2)
            legs_state = {
                "mode": "DEFINED_RISK_CREDIT_SPREAD_100",
                "wing_leg": {
                    "instrument_key": wing_contract["instrument_key"],
                    "trading_symbol": wing_contract.get("trading_symbol") or wing_contract["instrument_key"],
                    "expiry": wing_contract["expiry"],
                    "strike": float(wing_contract["strike"]),
                    "option_type": wing_contract["option_type"],
                    "lot_size": lot_sz,
                    "qty": confirmed_qty,
                    "entry_price": wing_px,
                    "partial_unfilled_qty": max(0, target_qty - confirmed_qty),
                },
                "short_target": {
                    "instrument_key": short_contract["instrument_key"],
                    "trading_symbol": short_contract.get("trading_symbol") or short_contract["instrument_key"],
                    "expiry": short_contract["expiry"],
                    "strike": float(short_contract["strike"]),
                    "option_type": short_contract["option_type"],
                    "lot_size": lot_sz,
                    "qty": -confirmed_qty,
                },
                "leg1_ts_ms": decision_ts_ms,
                "deadline_ts_ms": decision_ts_ms + int(hyg["leg2_timeout_ms"]),
                "spot_at_entry": float(spot_price),
                "vix_at_entry": vix_val,
                "is_bull": bool(is_bull),
                "margin_status": "UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN",
            }
            pkg_row = {
                "package_id": pkg_id,
                "signal_id": signal_id,
                "run_mode": self.run_mode,
                "config_hash": self.config_hash,
                "book_id": cfg_a["book_id"],
                "candidate_variant": candidate_variant,
                "session_date": session_date,
                "expiry": wing_contract["expiry"],
                "state": "PENDING_LEG2_SHORT",
                "fill_fidelity": sw_w["fill_fidelity"],
                "direction_label": f"SPREAD_SELL_{wing_contract['option_type']}",
                "entry_bar_ts": bar_ts,
                "entry_ts_ms": decision_ts_ms,
                "leg2_confirmed_ts_ms": None,
                "exit_bar_ts": None,
                "exit_ts_ms": None,
                "exit_reason": None,
                "legs_state": legs_state,
                "accounting": None,
                "reserved_capital_inr": round(margin_res, 2),
                "structural_max_loss_inr": round(wing_px * confirmed_qty, 2),
                "planned_loss_inr": round(wing_px * confirmed_qty, 2),
                "actual_gross_fill_loss_inr": 0.0,
                "slippage_and_gap_overshoot_inr": 0.0,
                "gross_pnl_inr": 0.0,
                "brokerage_inr": 0.0,
                "stt_inr": 0.0,
                "statutory_charges_inr": 0.0,
                "total_friction_inr": 0.0,
                "net_pnl_inr": 0.0,
                "entry_day_mtm_pnl_inr": 0.0,
                "overnight_gap_pnl_inr": 0.0,
                "expiry_intraday_pnl_inr": 0.0,
                "worst_gap_loss_inr": 0.0,
                "is_unpriced_exposure": 0,
                "unpriced_reason": None,
                "unpriced_since_ts_ms": None,
                "last_mark_net_pnl_inr": 0.0,
                "last_mark_ts_ms": decision_ts_ms,
                "last_reliable_mark_net_inr": 0.0,
                "last_reliable_mark_ts_ms": decision_ts_ms,
                "events_timeline": [
                    {
                        "ts_ms": int(decision_ts_ms),
                        "bar_ts": str(bar_ts),
                        "event": "LEG1_HEDGE_BUY",
                        "detail": f"Confirmed wing {confirmed_qty} @ Rs {wing_px:.2f} (pending Leg-2 short)",
                    }
                ],
                "transition_seq": 1,
            }
            ledger_entry = {
                "entry_id": make_stable_id("osv2_le", pkg_id, 1, "LEG1_HEDGE_OPEN"),
                "package_id": pkg_id,
                "transition_seq": 1,
                "run_mode": self.run_mode,
                "config_hash": self.config_hash,
                "ts_ms": decision_ts_ms,
                "session_date": session_date,
                "event_type": "LEG1_HEDGE_OPEN",
                "cash_delta_inr": 0.0,
                "reserved_delta_inr": round(margin_res, 2),
                "realized_equity_after_inr": self.realized_equity_inr,
                "reserved_after_inr": new_reserved,
            }
            committed, err_rsn = self._commit_atomic_or_fault(
                cfg_a["book_id"], "LEG1_HEDGE_OPEN", decision_ts_ms, [intent_row], [fill_row], pkg_row, ledger_entry
            )
            if not committed:
                return {"ok": False, "reason": err_rsn}

            self.reserved_capital_inr = new_reserved
            self.open_packages[pkg_id] = pkg_row
            self._all_pkgs_by_id[pkg_id] = pkg_row
            self.seen_signal_ids.add(signal_id)
            self.peak_reserved_by_date[session_date] = max(
                self.peak_reserved_by_date.get(session_date, 0.0), self.reserved_capital_inr
            )
            return {
                "ok": True,
                "package_id": pkg_id,
                "confirmed_hedge_qty": confirmed_qty,
                "wing_fill_price": wing_px,
            }

    def complete_or_unwind_pending_leg2(
        self,
        package_id: str,
        short_quote: Optional[Dict[str, Any]],
        wing_quote: Optional[Dict[str, Any]],
        decision_ts_ms: int,
        session_date: str,
        bar_ts: str,
    ) -> Dict[str, Any]:
        """
        Attempts Leg-2 short sell bounded by `min_permitted_short_px = actual_wing_fill + min_credit_pts`.
        Only confirmed hedge quantity may back the short leg. On rejection or timeout, unwinds wing
        at observed L5 bid (or retains `HEDGE_ONLY_EXPOSURE_OPEN` if wing bid is missing/stale).
        """
        with self._lock:
            pkg = self.open_packages.get(package_id)
            if not pkg or pkg["state"] != "PENDING_LEG2_SHORT":
                return {"ok": False, "reason": "NOT_PENDING_LEG2"}

            hyg = self.config["quote_hygiene"]
            cfg_a = self.config["books"]["V2_BOOK_A_PROPOSED_O3"]
            ls = dict(pkg["legs_state"])
            wl = ls["wing_leg"]
            st = ls["short_target"]
            confirmed_qty = int(wl["qty"])
            lot_sz = int(wl["lot_size"])
            wing_fill_px = float(wl["entry_price"])
            min_short_limit_px = round_to_nse_tick(
                wing_fill_px + float(cfg_a["min_net_credit_pts"]),
                mode="adverse_buy",
                allow_zero_settlement=False,
            )

            timed_out = decision_ts_ms >= int(ls["deadline_ts_ms"])
            sw_s: Dict[str, Any] = {"ok": False, "reason": "LEG2_TIMEOUT"}
            skew_ok, skew_ms, skew_reason = check_cross_leg_skew(
                short_quote, wing_quote, max_skew_ms=int(hyg["max_cross_leg_skew_ms"]), require_depth_ts=True
            )
            if not timed_out:
                if not skew_ok and short_quote is not None and wing_quote is not None:
                    sw_s = {"ok": False, "reason": skew_reason, "skew_ms": skew_ms}
                else:
                    sw_s = sweep_observed_l5_book(
                        quote=short_quote,
                        side="SELL",
                        target_qty=confirmed_qty,
                        lot_size=lot_sz,
                        adverse_ticks=self._effective_slippage_ticks(),
                        max_age_ms=int(hyg["max_entry_quote_age_ms"]),
                        now_ms=decision_ts_ms,
                        max_spread_pct=float(hyg["max_primary_spread_pct"]),
                        require_observed_l5=bool(hyg["require_observed_l5_book_for_entry"]),
                        allow_partial_lot_multiples=False,
                        min_acceptable_sell_price=min_short_limit_px,
                    )

            if sw_s.get("ok"):
                short_px = float(sw_s["fill_price"])
                net_credit = round(short_px - wing_fill_px, 2)
                worst_w = round_to_nse_tick(float(cfg_a["spread_width_pts"]) + 100.0, mode="adverse_sell")
                worst_s = round_to_nse_tick(200.0 + float(cfg_a["spread_width_pts"]), mode="adverse_buy")
                prov_acc = self._apply_cost_multiplier(
                    compute_package_accounting(
                        legs=[
                            OptionLeg(wl["option_type"], float(wl["strike"]), confirmed_qty, wing_fill_px, worst_w, lot_size=lot_sz),
                            OptionLeg(st["option_type"], float(st["strike"]), -confirmed_qty, short_px, worst_s, lot_size=lot_sz),
                        ],
                        trade_date=session_date,
                        reference_spot=float(ls["spot_at_entry"]),
                    )
                )
                struct_loss = float(prov_acc["risk"]["final_package_max_loss_after_friction_inr"])
                if struct_loss <= float(cfg_a["max_structural_loss_inr"]) * (confirmed_qty / 65.0):
                    # Complete 2-leg Book A package
                    intent_id = make_stable_id("osv2_int", package_id, "SHORT_LEG", "SELL")
                    fill_id = make_stable_id("osv2_fil", intent_id, sw_s["ts_ms"])
                    seq = int(pkg["transition_seq"]) + 1
                    hard_stop_debit = round(
                        min(
                            net_credit * float(cfg_a["close_only_stop_debit_mult"]),
                            net_credit + float(cfg_a["max_structural_loss_inr"]) / float(confirmed_qty),
                        ),
                        2,
                    )
                    target_debit = round(
                        net_credit * (1.0 - float(cfg_a["credit_capture_target_pct"]) / 100.0),
                        2,
                    )
                    ls["short_leg"] = {
                        **st,
                        "entry_price": short_px,
                        "entry_sweep": sw_s,
                    }
                    ls["net_entry_credit_pts"] = net_credit
                    ls["best_close_credit_decay_pts"] = net_credit
                    ls["ratchet_max_debit_pts"] = None
                    ls["hard_stop_debit_pts"] = hard_stop_debit
                    ls["target_debit_pts"] = target_debit
                    ls["prev_eod_mtm_inr"] = 0.0

                    intent_row = {
                        "intent_id": intent_id,
                        "signal_id": pkg["signal_id"],
                        "package_id": package_id,
                        "run_mode": self.run_mode,
                        "config_hash": self.config_hash,
                        "leg_role": "SHORT_LEG",
                        "instrument_key": st["instrument_key"],
                        "side": "SELL",
                        "target_qty": confirmed_qty,
                        "limit_price": min_short_limit_px,
                        "filled_qty": confirmed_qty,
                        "state": "FILLED",
                        "reject_reason": None,
                        "created_ts_ms": decision_ts_ms,
                        "updated_ts_ms": decision_ts_ms,
                    }
                    fill_row = {
                        "fill_id": fill_id,
                        "intent_id": intent_id,
                        "package_id": package_id,
                        "run_mode": self.run_mode,
                        "config_hash": self.config_hash,
                        "instrument_key": st["instrument_key"],
                        "side": "SELL",
                        "qty": confirmed_qty,
                        "fill_price": short_px,
                        "raw_top_price": float(sw_s["top_price"]),
                        "vwap_raw": float(sw_s["vwap_raw"]),
                        "slippage_pts": float(sw_s["depth_slippage_pts"]),
                        "fill_fidelity": sw_s["fill_fidelity"],
                        "exchange_ts_ms": int((short_quote or {}).get("exchange_ltt_ms") or sw_s["ts_ms"]),
                        "receipt_ts_ms": int((short_quote or {}).get("local_receipt_ts_ms") or sw_s["ts_ms"]),
                        "decision_ts_ms": decision_ts_ms,
                    }
                    pkg_upd = {
                        **pkg,
                        "state": "OPEN",
                        "leg2_confirmed_ts_ms": decision_ts_ms,
                        "legs_state": ls,
                        "structural_max_loss_inr": struct_loss,
                        "planned_loss_inr": round((hard_stop_debit - net_credit) * confirmed_qty, 2),
                        "events_timeline": _append_timeline(
                            pkg, decision_ts_ms, bar_ts, "LEG2_SHORT_SELL", f"Sold {confirmed_qty} @ Rs {short_px:.2f}; credit={net_credit:.2f}pt"
                        ),
                        "transition_seq": seq,
                    }
                    le = {
                        "entry_id": make_stable_id("osv2_le", package_id, seq, "LEG2_SHORT_CONFIRMED"),
                        "package_id": package_id,
                        "transition_seq": seq,
                        "run_mode": self.run_mode,
                        "config_hash": self.config_hash,
                        "ts_ms": decision_ts_ms,
                        "session_date": session_date,
                        "event_type": "LEG2_SHORT_CONFIRMED",
                        "cash_delta_inr": 0.0,
                        "reserved_delta_inr": 0.0,
                        "realized_equity_after_inr": self.realized_equity_inr,
                        "reserved_after_inr": self.reserved_capital_inr,
                    }
                    committed, err_rsn = self._commit_atomic_or_fault(
                        pkg["book_id"], "LEG2_SHORT_CONFIRMED", decision_ts_ms, [intent_row], [fill_row], pkg_upd, le
                    )
                    if not committed:
                        return {"ok": False, "reason": err_rsn}
                    self.open_packages[package_id] = pkg_upd
                    self._all_pkgs_by_id[package_id] = pkg_upd
                    return {"ok": True, "state": "OPEN", "package_id": package_id, "net_credit_pts": net_credit}

                sw_s = {"ok": False, "reason": f"STRUCTURAL_LOSS_EXCEEDED:{struct_loss:.2f}"}

            # Reject or timeout -> unwind confirmed hedge wing!
            reject_reason = str(sw_s.get("reason") or "LEG2_REJECTED")
            sw_unwind = sweep_observed_l5_book(
                quote=wing_quote,
                side="SELL",
                target_qty=confirmed_qty,
                lot_size=lot_sz,
                adverse_ticks=self._effective_slippage_ticks(),
                max_age_ms=int(hyg["max_exit_quote_age_ms"]),
                now_ms=decision_ts_ms,
                max_spread_pct=None,
                require_observed_l5=False,
                allow_fresh_l1_only=True,
                allow_partial_lot_multiples=False,
            )
            seq = int(pkg["transition_seq"]) + 1
            if not sw_unwind.get("ok"):
                # Cannot manufacture an exit fill when wing quote is stale/missing -> hold Hedge-Only Open!
                pkg_hold = {
                    **pkg,
                    "state": "HEDGE_ONLY_EXPOSURE_OPEN",
                    "legs_state": ls,
                    "structural_max_loss_inr": round(wing_fill_px * confirmed_qty, 2),
                    "is_unpriced_exposure": 1,
                    "unpriced_reason": f"HEDGE_UNWIND_BLOCKED:{sw_unwind.get('reason')}",
                    "unpriced_since_ts_ms": pkg.get("unpriced_since_ts_ms") or decision_ts_ms,
                    "events_timeline": _append_timeline(
                        pkg, decision_ts_ms, bar_ts, "HEDGE_ONLY_EXPOSURE_OPEN", f"Unwind blocked ({sw_unwind.get('reason')})"
                    ),
                    "transition_seq": seq,
                }
                le_h = {
                    "entry_id": make_stable_id("osv2_le", package_id, seq, "HEDGE_ONLY_HOLD"),
                    "package_id": package_id,
                    "transition_seq": seq,
                    "run_mode": self.run_mode,
                    "config_hash": self.config_hash,
                    "ts_ms": decision_ts_ms,
                    "session_date": session_date,
                    "event_type": "HEDGE_ONLY_HOLD",
                    "cash_delta_inr": 0.0,
                    "reserved_delta_inr": 0.0,
                    "realized_equity_after_inr": self.realized_equity_inr,
                    "reserved_after_inr": self.reserved_capital_inr,
                }
                committed_h, err_h = self._commit_atomic_or_fault(
                    pkg["book_id"], "HEDGE_ONLY_HOLD", decision_ts_ms, [], [], pkg_hold, le_h
                )
                if not committed_h:
                    return {"ok": False, "reason": err_h}
                self.open_packages[package_id] = pkg_hold
                self._all_pkgs_by_id[package_id] = pkg_hold
                return {"ok": False, "state": "HEDGE_ONLY_EXPOSURE_OPEN", "package_id": package_id, "reason": reject_reason}

            unwind_px = float(sw_unwind["fill_price"])
            acc = self._apply_cost_multiplier(
                compute_package_accounting(
                    legs=[OptionLeg(wl["option_type"], float(wl["strike"]), confirmed_qty, wing_fill_px, unwind_px, lot_size=lot_sz, label="HEDGE_LEG_UNWIND")],
                    trade_date=session_date,
                    reference_spot=float(ls["spot_at_entry"]),
                )
            )
            net_pnl = float(acc["net_pnl"])
            res_rel = -float(pkg["reserved_capital_inr"])
            new_realized = round(self.realized_equity_inr + net_pnl, 2)
            new_reserved = round(max(0.0, self.reserved_capital_inr + res_rel), 2)

            unw_intent_id = make_stable_id("osv2_int", package_id, "WING_UNWIND", "SELL")
            unw_fill_id = make_stable_id("osv2_fil", unw_intent_id, sw_unwind["ts_ms"])
            intent_row = {
                "intent_id": unw_intent_id,
                "signal_id": pkg["signal_id"],
                "package_id": package_id,
                "run_mode": self.run_mode,
                "config_hash": self.config_hash,
                "leg_role": "WING_UNWIND",
                "instrument_key": wl["instrument_key"],
                "side": "SELL",
                "target_qty": confirmed_qty,
                "limit_price": unwind_px,
                "filled_qty": confirmed_qty,
                "state": "FILLED",
                "reject_reason": reject_reason,
                "created_ts_ms": decision_ts_ms,
                "updated_ts_ms": decision_ts_ms,
            }
            fill_row = {
                "fill_id": unw_fill_id,
                "intent_id": unw_intent_id,
                "package_id": package_id,
                "run_mode": self.run_mode,
                "config_hash": self.config_hash,
                "instrument_key": wl["instrument_key"],
                "side": "SELL",
                "qty": confirmed_qty,
                "fill_price": unwind_px,
                "raw_top_price": float(sw_unwind["top_price"]),
                "vwap_raw": float(sw_unwind["vwap_raw"]),
                "slippage_pts": float(sw_unwind["depth_slippage_pts"]),
                "fill_fidelity": sw_unwind["fill_fidelity"],
                "exchange_ts_ms": int((wing_quote or {}).get("exchange_ltt_ms") or sw_unwind["ts_ms"]),
                "receipt_ts_ms": int((wing_quote or {}).get("local_receipt_ts_ms") or sw_unwind["ts_ms"]),
                "decision_ts_ms": decision_ts_ms,
            }
            ls["wing_unwind_price"] = unwind_px
            pkg_closed = {
                **pkg,
                "state": "INCOMPLETE_LEG_UNWOUND",
                "exit_bar_ts": bar_ts,
                "exit_ts_ms": decision_ts_ms,
                "exit_reason": f"HEDGE_UNWIND:{reject_reason}",
                "legs_state": ls,
                "accounting": acc,
                "reserved_capital_inr": 0.0,
                "gross_pnl_inr": float(acc["gross_pnl"]),
                "brokerage_inr": float(acc["brokerage"]),
                "stt_inr": float(acc["stt"]),
                "statutory_charges_inr": float(acc["statutory_charges"]),
                "total_friction_inr": float(acc["total_friction"]),
                "net_pnl_inr": net_pnl,
                "entry_day_mtm_pnl_inr": net_pnl,
                "last_reliable_mark_net_inr": net_pnl,
                "last_reliable_mark_ts_ms": decision_ts_ms,
                "is_unpriced_exposure": 0,
                "unpriced_reason": None,
                "unpriced_since_ts_ms": None,
                "events_timeline": _append_timeline(
                    pkg, decision_ts_ms, bar_ts, "HEDGE_UNWOUND_CLOSE", f"Unwound wing @ Rs {unwind_px:.2f} ({reject_reason})"
                ),
                "transition_seq": seq,
            }
            other_open_mtm = sum(
                (-abs(float(p.get("structural_max_loss_inr") or 0.0)) if p.get("is_unpriced_exposure") else float(p.get("last_mark_net_pnl_inr") or 0.0))
                for pid_o, p in self.open_packages.items()
                if pid_o != package_id
            )
            eq_pt_unw = self._build_equity_point(session_date, decision_ts_ms, "HEDGE_UNWOUND_CLOSE", new_realized, other_open_mtm)
            le_c = {
                "entry_id": make_stable_id("osv2_le", package_id, seq, "HEDGE_UNWOUND_CLOSE"),
                "package_id": package_id,
                "transition_seq": seq,
                "run_mode": self.run_mode,
                "config_hash": self.config_hash,
                "ts_ms": decision_ts_ms,
                "session_date": session_date,
                "event_type": "HEDGE_UNWOUND_CLOSE",
                "cash_delta_inr": net_pnl,
                "reserved_delta_inr": res_rel,
                "realized_equity_after_inr": new_realized,
                "reserved_after_inr": new_reserved,
            }
            committed_u, err_u = self._commit_atomic_or_fault(
                pkg["book_id"], "HEDGE_UNWOUND_CLOSE", decision_ts_ms, [intent_row], [fill_row], pkg_closed, le_c, equity_point=eq_pt_unw
            )
            if not committed_u:
                return {"ok": False, "reason": err_u}
            self.realized_equity_inr = new_realized
            self.reserved_capital_inr = new_reserved
            self.open_packages.pop(package_id, None)
            self._all_pkgs_by_id[package_id] = pkg_closed
            self.locked_until_after_bar.setdefault(bar_ts, []).append((pkg["book_id"], float(pkg["reserved_capital_inr"])))
            self.cooldown_until_ms[pkg["book_id"]] = decision_ts_ms + int(self.config["account_envelope"]["cooldown_seconds_after_exit"]) * 1000
            return {
                "ok": False,
                "state": "INCOMPLETE_LEG_UNWOUND",
                "package_id": package_id,
                "reason": reject_reason,
                "net_pnl_inr": net_pnl,
            }

    # ──────────────────────────────────────────────────────────────────────────
    # EXIT, GAP TRACKING, CUTOFF PERSISTENCE & SYNCHRONIZED MTM MARKING
    # ──────────────────────────────────────────────────────────────────────────

    def close_package_market_or_expiry(
        self,
        package_id: str,
        session_date: str,
        bar_ts: str,
        decision_ts_ms: int,
        exit_reason: str,
        long_or_wing_quote: Optional[Dict[str, Any]],
        short_quote: Optional[Dict[str, Any]] = None,
        settlement_fsp_spot: Optional[float] = None,
        reference_spot: float = 0.0,
    ) -> Dict[str, Any]:
        """Closes single-leg Paid Flow or 2-leg Spread via L5 market sweep OR 15:30 cash settlement."""
        with self._lock:
            pkg = self.open_packages.get(package_id)
            if not pkg or pkg["state"] not in OPEN_EXPOSURE_STATES:
                return {"ok": False, "reason": "PACKAGE_NOT_OPEN"}

            hyg = self.config["quote_hygiene"]
            ls = dict(pkg["legs_state"])
            mode = ls["mode"]
            seq = int(pkg["transition_seq"]) + 1
            intents: List[Dict[str, Any]] = []
            fills: List[Dict[str, Any]] = []
            legs: List[OptionLeg] = []

            if mode == "SINGLE_PAID_LONG":
                ll = ls["long_leg"]
                qty = int(ll["qty"])
                lot_sz = int(ll["lot_size"])
                if settlement_fsp_spot is not None and settlement_fsp_spot > 0:
                    legs = [
                        OptionLeg(ll["option_type"], float(ll["strike"]), qty, float(ll["entry_price"]), exit_mode="CASH_SETTLED_EXPIRY", settlement_spot=float(settlement_fsp_spot), lot_size=lot_sz)
                    ]
                    exit_px_eff = max(0.0, (float(settlement_fsp_spot) - float(ll["strike"])) if ll["option_type"] == "CE" else (float(ll["strike"]) - float(settlement_fsp_spot)))
                else:
                    sw = sweep_observed_l5_book(
                        quote=long_or_wing_quote,
                        side="SELL",
                        target_qty=qty,
                        lot_size=lot_sz,
                        adverse_ticks=self._effective_slippage_ticks(),
                        max_age_ms=int(hyg["max_exit_quote_age_ms"]),
                        now_ms=decision_ts_ms,
                        max_spread_pct=None,
                        require_observed_l5=False,
                        allow_fresh_l1_only=True,
                    )
                    if not sw.get("ok"):
                        return self._mark_unresolved_or_unpriced(pkg, decision_ts_ms, str(sw.get("reason") or "EXIT_QUOTE_UNAVAILABLE"), bar_ts)
                    exit_px_eff = float(sw["fill_price"])
                    iid = make_stable_id("osv2_int", package_id, "LONG_EXIT", "SELL")
                    fid = make_stable_id("osv2_fil", iid, sw["ts_ms"])
                    intents.append({"intent_id": iid, "signal_id": pkg["signal_id"], "package_id": package_id, "run_mode": self.run_mode, "config_hash": self.config_hash, "leg_role": "LONG_EXIT", "instrument_key": ll["instrument_key"], "side": "SELL", "target_qty": qty, "limit_price": exit_px_eff, "filled_qty": qty, "state": "FILLED", "reject_reason": None, "created_ts_ms": decision_ts_ms, "updated_ts_ms": decision_ts_ms})
                    fills.append({"fill_id": fid, "intent_id": iid, "package_id": package_id, "run_mode": self.run_mode, "config_hash": self.config_hash, "instrument_key": ll["instrument_key"], "side": "SELL", "qty": qty, "fill_price": exit_px_eff, "raw_top_price": float(sw["top_price"]), "vwap_raw": float(sw["vwap_raw"]), "slippage_pts": float(sw["depth_slippage_pts"]), "fill_fidelity": sw["fill_fidelity"], "exchange_ts_ms": int((long_or_wing_quote or {}).get("exchange_ltt_ms") or sw["ts_ms"]), "receipt_ts_ms": int((long_or_wing_quote or {}).get("local_receipt_ts_ms") or sw["ts_ms"]), "decision_ts_ms": decision_ts_ms})
                    legs = [OptionLeg(ll["option_type"], float(ll["strike"]), qty, float(ll["entry_price"]), exit_px_eff, lot_size=lot_sz)]

                ll["exit_price"] = exit_px_eff
            else:
                # 2-Leg Spread or Hedge-Only Open
                wl = ls["wing_leg"]
                sl = ls.get("short_leg")
                w_qty = int(wl["qty"])
                lot_sz = int(wl["lot_size"])
                if settlement_fsp_spot is not None and settlement_fsp_spot > 0:
                    legs.append(OptionLeg(wl["option_type"], float(wl["strike"]), w_qty, float(wl["entry_price"]), exit_mode="CASH_SETTLED_EXPIRY", settlement_spot=float(settlement_fsp_spot), lot_size=lot_sz))
                    if sl:
                        legs.append(OptionLeg(sl["option_type"], float(sl["strike"]), -w_qty, float(sl["entry_price"]), exit_mode="CASH_SETTLED_EXPIRY", settlement_spot=float(settlement_fsp_spot), lot_size=lot_sz))
                else:
                    sw_w = sweep_observed_l5_book(
                        quote=long_or_wing_quote,
                        side="SELL",
                        target_qty=w_qty,
                        lot_size=lot_sz,
                        adverse_ticks=self._effective_slippage_ticks(),
                        max_age_ms=int(hyg["max_exit_quote_age_ms"]),
                        now_ms=decision_ts_ms,
                        max_spread_pct=None,
                        require_observed_l5=False,
                        allow_fresh_l1_only=True,
                    )
                    if not sw_w.get("ok"):
                        return self._mark_unresolved_or_unpriced(pkg, decision_ts_ms, f"WING_EXIT_{sw_w.get('reason')}", bar_ts)
                    if sl is not None:
                        sw_s = sweep_observed_l5_book(
                            quote=short_quote,
                            side="BUY",
                            target_qty=w_qty,
                            lot_size=lot_sz,
                            adverse_ticks=self._effective_slippage_ticks(),
                            max_age_ms=int(hyg["max_exit_quote_age_ms"]),
                            now_ms=decision_ts_ms,
                            max_spread_pct=None,
                            require_observed_l5=False,
                            allow_fresh_l1_only=True,
                        )
                        if not sw_s.get("ok"):
                            return self._mark_unresolved_or_unpriced(pkg, decision_ts_ms, f"SHORT_EXIT_{sw_s.get('reason')}", bar_ts)
                        skew_ok, _, skew_reason = check_cross_leg_skew(
                            long_or_wing_quote,
                            short_quote,
                            max_skew_ms=int(hyg["max_cross_leg_skew_ms"]),
                            consumed_ts_a=sw_w.get("consumed_ts_ms"),
                            consumed_ts_b=sw_s.get("consumed_ts_ms"),
                        )
                        if not skew_ok:
                            return self._mark_unresolved_or_unpriced(pkg, decision_ts_ms, skew_reason, bar_ts)
                        s_ex = float(sw_s["fill_price"])
                        iid_s = make_stable_id("osv2_int", package_id, "SHORT_EXIT", "BUY")
                        fid_s = make_stable_id("osv2_fil", iid_s, sw_s["ts_ms"])
                        intents.append({"intent_id": iid_s, "signal_id": pkg["signal_id"], "package_id": package_id, "run_mode": self.run_mode, "config_hash": self.config_hash, "leg_role": "SHORT_EXIT", "instrument_key": sl["instrument_key"], "side": "BUY", "target_qty": w_qty, "limit_price": s_ex, "filled_qty": w_qty, "state": "FILLED", "reject_reason": None, "created_ts_ms": decision_ts_ms, "updated_ts_ms": decision_ts_ms})
                        fills.append({"fill_id": fid_s, "intent_id": iid_s, "package_id": package_id, "run_mode": self.run_mode, "config_hash": self.config_hash, "instrument_key": sl["instrument_key"], "side": "BUY", "qty": w_qty, "fill_price": s_ex, "raw_top_price": float(sw_s["top_price"]), "vwap_raw": float(sw_s["vwap_raw"]), "slippage_pts": float(sw_s["depth_slippage_pts"]), "fill_fidelity": sw_s["fill_fidelity"], "exchange_ts_ms": int((short_quote or {}).get("exchange_ltt_ms") or sw_s["ts_ms"]), "receipt_ts_ms": int((short_quote or {}).get("local_receipt_ts_ms") or sw_s["ts_ms"]), "decision_ts_ms": decision_ts_ms})
                        sl["exit_price"] = s_ex
                        legs.append(OptionLeg(sl["option_type"], float(sl["strike"]), -w_qty, float(sl["entry_price"]), s_ex, lot_size=lot_sz))

                    w_ex = float(sw_w["fill_price"])
                    iid_w = make_stable_id("osv2_int", package_id, "WING_EXIT", "SELL")
                    fid_w = make_stable_id("osv2_fil", iid_w, sw_w["ts_ms"])
                    intents.append({"intent_id": iid_w, "signal_id": pkg["signal_id"], "package_id": package_id, "run_mode": self.run_mode, "config_hash": self.config_hash, "leg_role": "WING_EXIT", "instrument_key": wl["instrument_key"], "side": "SELL", "target_qty": w_qty, "limit_price": w_ex, "filled_qty": w_qty, "state": "FILLED", "reject_reason": None, "created_ts_ms": decision_ts_ms, "updated_ts_ms": decision_ts_ms})
                    fills.append({"fill_id": fid_w, "intent_id": iid_w, "package_id": package_id, "run_mode": self.run_mode, "config_hash": self.config_hash, "instrument_key": wl["instrument_key"], "side": "SELL", "qty": w_qty, "fill_price": w_ex, "raw_top_price": float(sw_w["top_price"]), "vwap_raw": float(sw_w["vwap_raw"]), "slippage_pts": float(sw_w["depth_slippage_pts"]), "fill_fidelity": sw_w["fill_fidelity"], "exchange_ts_ms": int((long_or_wing_quote or {}).get("exchange_ltt_ms") or sw_w["ts_ms"]), "receipt_ts_ms": int((long_or_wing_quote or {}).get("local_receipt_ts_ms") or sw_w["ts_ms"]), "decision_ts_ms": decision_ts_ms})
                    wl["exit_price"] = w_ex
                    legs.insert(0, OptionLeg(wl["option_type"], float(wl["strike"]), w_qty, float(wl["entry_price"]), w_ex, lot_size=lot_sz))

            acc = self._apply_cost_multiplier(
                compute_package_accounting(legs=legs, trade_date=session_date, reference_spot=reference_spot or None)
            )
            gross = float(acc["gross_pnl"])
            net = float(acc["net_pnl"])
            planned_loss = float(pkg.get("planned_loss_inr") or 0.0)
            actual_gross_loss = round(max(0.0, -gross), 2)
            overshoot = round(max(0.0, actual_gross_loss - planned_loss), 2) if actual_gross_loss > 0 else 0.0

            # Attribute entry_day MTM, overnight gap, and expiry-day intraday P&L cleanly
            entry_day_mtm = float(pkg.get("entry_day_mtm_pnl_inr") or 0.0)
            overnight_gap = float(pkg.get("overnight_gap_pnl_inr") or 0.0)
            if session_date == pkg["session_date"]:
                entry_day_mtm = net
                expiry_intraday = 0.0
            else:
                expiry_intraday = round(net - (entry_day_mtm + overnight_gap), 2)

            res_rel = -float(pkg["reserved_capital_inr"])
            new_realized = round(self.realized_equity_inr + net, 2)
            new_reserved = round(max(0.0, self.reserved_capital_inr + res_rel), 2)

            pkg_closed = {
                **pkg,
                "state": "CLOSED",
                "exit_bar_ts": bar_ts,
                "exit_ts_ms": decision_ts_ms,
                "exit_reason": exit_reason,
                "legs_state": ls,
                "accounting": acc,
                "reserved_capital_inr": 0.0,
                "actual_gross_fill_loss_inr": actual_gross_loss,
                "slippage_and_gap_overshoot_inr": overshoot,
                "gross_pnl_inr": gross,
                "brokerage_inr": float(acc["brokerage"]),
                "stt_inr": float(acc["stt"]),
                "statutory_charges_inr": float(acc["statutory_charges"]),
                "total_friction_inr": float(acc["total_friction"]),
                "net_pnl_inr": net,
                "entry_day_mtm_pnl_inr": entry_day_mtm,
                "overnight_gap_pnl_inr": overnight_gap,
                "expiry_intraday_pnl_inr": expiry_intraday,
                "is_unpriced_exposure": 0,
                "unpriced_reason": None,
                "unpriced_since_ts_ms": None,
                "last_mark_net_pnl_inr": net,
                "last_mark_ts_ms": decision_ts_ms,
                "last_reliable_mark_net_inr": net,
                "last_reliable_mark_ts_ms": decision_ts_ms,
                "events_timeline": _append_timeline(
                    pkg, decision_ts_ms, bar_ts, f"EXIT_{exit_reason}", f"Gross {gross:+.2f} - Fric {float(acc['total_friction']):.2f} = Net {net:+.2f}"
                ),
                "transition_seq": seq,
            }
            other_open_mtm = sum(
                (-abs(float(p.get("structural_max_loss_inr") or 0.0)) if p.get("is_unpriced_exposure") else float(p.get("last_mark_net_pnl_inr") or 0.0))
                for pid_o, p in self.open_packages.items()
                if pid_o != package_id
            )
            eq_pt_c = self._build_equity_point(session_date, decision_ts_ms, f"CLOSE_{exit_reason}", new_realized, other_open_mtm)
            le = {
                "entry_id": make_stable_id("osv2_le", package_id, seq, "CLOSE_PACKAGE"),
                "package_id": package_id,
                "transition_seq": seq,
                "run_mode": self.run_mode,
                "config_hash": self.config_hash,
                "ts_ms": decision_ts_ms,
                "session_date": session_date,
                "event_type": "CLOSE_PACKAGE",
                "cash_delta_inr": net,
                "reserved_delta_inr": res_rel,
                "realized_equity_after_inr": new_realized,
                "reserved_after_inr": new_reserved,
            }
            committed_c, err_c = self._commit_atomic_or_fault(
                pkg["book_id"], f"CLOSE_{exit_reason}", decision_ts_ms, intents, fills, pkg_closed, le, equity_point=eq_pt_c
            )
            if not committed_c:
                return {"ok": False, "reason": err_c}
            self.realized_equity_inr = new_realized
            self.reserved_capital_inr = new_reserved
            self.open_packages.pop(package_id, None)
            self._all_pkgs_by_id[package_id] = pkg_closed
            self.locked_until_after_bar.setdefault(bar_ts, []).append((pkg["book_id"], float(pkg["reserved_capital_inr"])))
            self.cooldown_until_ms[pkg["book_id"]] = decision_ts_ms + int(self.config["account_envelope"]["cooldown_seconds_after_exit"]) * 1000
            return {
                "ok": True,
                "state": "CLOSED",
                "package_id": package_id,
                "exit_reason": exit_reason,
                "net_pnl_inr": net,
                "actual_gross_fill_loss_inr": actual_gross_loss,
                "slippage_and_gap_overshoot_inr": overshoot,
            }

    def _mark_unresolved_or_unpriced(
        self,
        pkg: Dict[str, Any],
        decision_ts_ms: int,
        reason: str,
        bar_ts: str,
    ) -> Dict[str, Any]:
        """Never manufactures an exit or profit when closing quotes fail: persists unresolved state & last reliable mark."""
        new_state = pkg["state"]
        if (pkg.get("expiry") or "") <= bar_ts[:10] and bar_ts[11:16] >= "14:45":
            new_state = "UNRESOLVED_EXPOSURE_PAST_1445"
        seq = int(pkg["transition_seq"]) + 1
        last_rel_net = pkg.get("last_reliable_mark_net_inr")
        if last_rel_net is None:
            last_rel_net = pkg.get("last_mark_net_pnl_inr")
        last_rel_ts = pkg.get("last_reliable_mark_ts_ms") or pkg.get("last_mark_ts_ms") or pkg.get("entry_ts_ms")
        upd = {
            **pkg,
            "state": new_state,
            "is_unpriced_exposure": 1,
            "unpriced_reason": reason,
            "unpriced_since_ts_ms": pkg.get("unpriced_since_ts_ms") or decision_ts_ms,
            "last_reliable_mark_net_inr": last_rel_net,
            "last_reliable_mark_ts_ms": last_rel_ts,
            "events_timeline": _append_timeline(
                pkg, decision_ts_ms, bar_ts, new_state, f"Exit/valuation retained open: {reason}"
            ),
            "transition_seq": seq,
        }
        committed_m, err_m = self._commit_atomic_or_fault(
            pkg["book_id"], f"UNRESOLVED_{reason}", decision_ts_ms, [], [], upd, None
        )
        if not committed_m:
            return {"ok": False, "state": pkg["state"], "package_id": pkg["package_id"], "reason": err_m}
        self.open_packages[pkg["package_id"]] = upd
        self._all_pkgs_by_id[pkg["package_id"]] = upd
        return {"ok": False, "state": new_state, "package_id": pkg["package_id"], "reason": reason}

    def mark_open_packages(
        self,
        quote_by_ikey: Dict[str, Dict[str, Any]],
        session_date: str,
        bar_ts: str,
        now_ms: int,
        is_opening_bar: bool = False,
        is_eod_bar: bool = False,
    ) -> Dict[str, Any]:
        """
        Marks all open packages strictly from fresh synchronized executable liquidity.
        Retains open exposure + last reliable mark/age on missing/stale/skewed/carried/evicted quotes
        and charges structural max loss conservatively to chronological drawdown.
        """
        with self._lock:
            hyg = self.config["quote_hygiene"]
            priced_mtm = 0.0
            conservative_mtm = 0.0
            unpriced_details: List[Dict[str, Any]] = []

            for pid, pkg in list(self.open_packages.items()):
                ls = dict(pkg["legs_state"])
                mode = ls["mode"]
                unpriced_reason: Optional[str] = None
                net_mark: Optional[float] = None

                if pkg["state"] == "PENDING_LEG2_SHORT":
                    wl = ls["wing_leg"]
                    wq = quote_by_ikey.get(wl["instrument_key"])
                    if wl["instrument_key"] in self.subscription_evicted_keys:
                        unpriced_reason = "LEG_SUBSCRIPTION_EVICTED"
                    else:
                        sw = sweep_observed_l5_book(
                            wq, "SELL", int(wl["qty"]), int(wl["lot_size"]),
                            self._effective_slippage_ticks(), int(hyg["max_exit_quote_age_ms"]),
                            now_ms, max_spread_pct=None, require_observed_l5=False, allow_fresh_l1_only=True,
                        )
                        if not sw.get("ok"):
                            unpriced_reason = str(sw.get("reason"))
                        else:
                            acc = self._apply_cost_multiplier(compute_package_accounting([OptionLeg(wl["option_type"], float(wl["strike"]), int(wl["qty"]), float(wl["entry_price"]), float(sw["fill_price"]), lot_size=int(wl["lot_size"]))], trade_date=session_date))
                            net_mark = float(acc["net_pnl"])
                elif mode == "SINGLE_PAID_LONG":
                    ll = ls["long_leg"]
                    if ll["instrument_key"] in self.subscription_evicted_keys:
                        unpriced_reason = "LEG_SUBSCRIPTION_EVICTED"
                    else:
                        lq = quote_by_ikey.get(ll["instrument_key"])
                        sw = sweep_observed_l5_book(
                            lq, "SELL", int(ll["qty"]), int(ll["lot_size"]),
                            self._effective_slippage_ticks(), int(hyg["max_exit_quote_age_ms"]),
                            now_ms, max_spread_pct=None, require_observed_l5=False, allow_fresh_l1_only=True,
                        )
                        if not sw.get("ok"):
                            unpriced_reason = str(sw.get("reason"))
                        else:
                            acc = self._apply_cost_multiplier(compute_package_accounting([OptionLeg(ll["option_type"], float(ll["strike"]), int(ll["qty"]), float(ll["entry_price"]), float(sw["fill_price"]), lot_size=int(ll["lot_size"]))], trade_date=session_date))
                            net_mark = float(acc["net_pnl"])
                else:
                    wl = ls["wing_leg"]
                    sl = ls.get("short_leg")
                    if wl["instrument_key"] in self.subscription_evicted_keys or (sl and sl["instrument_key"] in self.subscription_evicted_keys):
                        unpriced_reason = "LEG_SUBSCRIPTION_EVICTED"
                    elif sl is None:
                        wq = quote_by_ikey.get(wl["instrument_key"])
                        sw_w = sweep_observed_l5_book(
                            wq, "SELL", int(wl["qty"]), int(wl["lot_size"]),
                            self._effective_slippage_ticks(), int(hyg["max_exit_quote_age_ms"]),
                            now_ms, max_spread_pct=None, require_observed_l5=False, allow_fresh_l1_only=True,
                        )
                        if not sw_w.get("ok"):
                            unpriced_reason = str(sw_w.get("reason"))
                        else:
                            acc = self._apply_cost_multiplier(compute_package_accounting([OptionLeg(wl["option_type"], float(wl["strike"]), int(wl["qty"]), float(wl["entry_price"]), float(sw_w["fill_price"]), lot_size=int(wl["lot_size"]))], trade_date=session_date))
                            net_mark = float(acc["net_pnl"])
                    else:
                        wq = quote_by_ikey.get(wl["instrument_key"])
                        sq = quote_by_ikey.get(sl["instrument_key"])
                        sw_w = sweep_observed_l5_book(
                            wq, "SELL", int(wl["qty"]), int(wl["lot_size"]),
                            self._effective_slippage_ticks(), int(hyg["max_exit_quote_age_ms"]),
                            now_ms, max_spread_pct=None, require_observed_l5=False, allow_fresh_l1_only=True,
                        )
                        sw_s = sweep_observed_l5_book(
                            sq, "BUY", abs(int(sl["qty"])), int(sl["lot_size"]),
                            self._effective_slippage_ticks(), int(hyg["max_exit_quote_age_ms"]),
                            now_ms, max_spread_pct=None, require_observed_l5=False, allow_fresh_l1_only=True,
                        )
                        if not sw_w.get("ok"):
                            unpriced_reason = f"WING_{sw_w.get('reason')}"
                        elif not sw_s.get("ok"):
                            unpriced_reason = f"SHORT_{sw_s.get('reason')}"
                        else:
                            skew_ok, _, skew_rsn = check_cross_leg_skew(
                                wq, sq,
                                max_skew_ms=int(hyg["max_cross_leg_skew_ms"]),
                                consumed_ts_a=sw_w.get("consumed_ts_ms"),
                                consumed_ts_b=sw_s.get("consumed_ts_ms"),
                            )
                            if not skew_ok:
                                unpriced_reason = skew_rsn
                            else:
                                acc = self._apply_cost_multiplier(
                                    compute_package_accounting(
                                        [
                                            OptionLeg(wl["option_type"], float(wl["strike"]), int(wl["qty"]), float(wl["entry_price"]), float(sw_w["fill_price"]), lot_size=int(wl["lot_size"])),
                                            OptionLeg(sl["option_type"], float(sl["strike"]), int(sl["qty"]), float(sl["entry_price"]), float(sw_s["fill_price"]), lot_size=int(sl["lot_size"])),
                                        ],
                                        trade_date=session_date,
                                    )
                                )
                                net_mark = float(acc["net_pnl"])

                if net_mark is not None:
                    priced_mtm += net_mark
                    conservative_mtm += net_mark
                    entry_day_mtm = float(pkg.get("entry_day_mtm_pnl_inr") or 0.0)
                    overnight_gap = float(pkg.get("overnight_gap_pnl_inr") or 0.0)
                    worst_gap = float(pkg.get("worst_gap_loss_inr") or 0.0)
                    if pkg["session_date"] < session_date and is_opening_bar:
                        gap_d = round(net_mark - entry_day_mtm, 2)
                        overnight_gap = gap_d
                        if gap_d < worst_gap:
                            worst_gap = gap_d
                    elif pkg["session_date"] == session_date and is_eod_bar:
                        entry_day_mtm = round(net_mark, 2)

                    upd = {
                        **pkg,
                        "entry_day_mtm_pnl_inr": entry_day_mtm,
                        "overnight_gap_pnl_inr": overnight_gap,
                        "worst_gap_loss_inr": worst_gap,
                        "is_unpriced_exposure": 0,
                        "unpriced_reason": None,
                        "unpriced_since_ts_ms": None,
                        "last_mark_net_pnl_inr": round(net_mark, 2),
                        "last_mark_ts_ms": now_ms,
                        "last_reliable_mark_net_inr": round(net_mark, 2),
                        "last_reliable_mark_ts_ms": now_ms,
                    }
                    self.open_packages[pid] = upd
                    self._all_pkgs_by_id[pid] = upd
                    if self.run_mode == "FORWARD_PAPER" or is_opening_bar or is_eod_bar or int(pkg.get("is_unpriced_exposure") or 0) != 0:
                        self._commit_atomic_or_fault(pkg["book_id"], "MARK_OPEN", now_ms, [], [], upd, None)
                else:
                    since_ms = pkg.get("unpriced_since_ts_ms") or now_ms
                    age_unpriced = max(0, now_ms - int(since_ms))
                    worst_loss = -abs(float(pkg.get("structural_max_loss_inr") or 0.0))
                    conservative_mtm += worst_loss
                    last_rel_net = pkg.get("last_reliable_mark_net_inr")
                    if last_rel_net is None:
                        last_rel_net = pkg.get("last_mark_net_pnl_inr")
                    last_rel_ts = pkg.get("last_reliable_mark_ts_ms") or pkg.get("last_mark_ts_ms") or pkg.get("entry_ts_ms")
                    last_rel_age = max(0, now_ms - int(last_rel_ts)) if last_rel_ts else age_unpriced
                    upd = {
                        **pkg,
                        "is_unpriced_exposure": 1,
                        "unpriced_reason": unpriced_reason or "UNPRICED_UNKNOWN",
                        "unpriced_since_ts_ms": since_ms,
                        "last_reliable_mark_net_inr": last_rel_net,
                        "last_reliable_mark_ts_ms": last_rel_ts,
                    }
                    self.open_packages[pid] = upd
                    self._all_pkgs_by_id[pid] = upd
                    if (
                        self.run_mode == "FORWARD_PAPER"
                        or is_opening_bar
                        or is_eod_bar
                        or int(pkg.get("is_unpriced_exposure") or 0) != 1
                        or pkg.get("unpriced_reason") != upd["unpriced_reason"]
                    ):
                        self._commit_atomic_or_fault(pkg["book_id"], "MARK_UNPRICED", now_ms, [], [], upd, None)
                    unpriced_details.append(
                        {
                            "package_id": pid,
                            "book_id": pkg["book_id"],
                            "reason": upd["unpriced_reason"],
                            "unpriced_age_ms": age_unpriced,
                            "last_reliable_mark_net_inr": last_rel_net,
                            "last_reliable_mark_ts_ms": last_rel_ts,
                            "last_reliable_mark_age_ms": last_rel_age,
                            "worst_case_risk_inr": worst_loss,
                        }
                    )

            eq_pt = self._build_equity_point(session_date, now_ms, "BAR_MTM", self.realized_equity_inr, conservative_mtm)
            if self.run_mode == "FORWARD_PAPER" or self.open_packages:
                try:
                    self.store.append_equity_point(eq_pt)
                except Exception as exc_eq:
                    self.booking_fault = {
                        "fault_ts_ms": int(now_ms),
                        "book_id": "PORTFOLIO",
                        "operation": "APPEND_EQUITY_POINT",
                        "error": f"{type(exc_eq).__name__}: {exc_eq}",
                    }
                    self.blocked_books_due_to_fault.add("PORTFOLIO")
            cur_mtm_eq = float(eq_pt["marked_equity_inr"])
            prev_min = self.intraday_min_equity_by_date.get(session_date, cur_mtm_eq)
            self.intraday_min_equity_by_date[session_date] = min(prev_min, cur_mtm_eq)
            return {
                "realized_equity_inr": self.realized_equity_inr,
                "priced_unrealized_mtm_inr": round(priced_mtm, 2),
                "conservative_unrealized_mtm_inr": round(conservative_mtm, 2),
                "conservative_mtm_equity_inr": cur_mtm_eq,
                "running_peak_equity_inr": self.running_peak_equity_inr,
                "max_mtm_dd_inr": self.max_mtm_dd_inr,
                "unpriced_count": len(unpriced_details),
                "unpriced_details": unpriced_details,
                "open_count": len(self.open_packages),
                "reserved_capital_inr": self.reserved_capital_inr,
            }
