"""Isolated Live Execution Engine for Option Sniffer Advance V2.

Safety Guarantees:
1. Always boots disarmed (`armed=False`); resets prior armed DB rows on startup.
2. Requires explicit human PIN confirmation (`hmac.compare_digest`) + confirmation flag.
3. Disarm stops new entries immediately while preserving position management, broker
   reconciliation, and manual emergency square-off of open positions.
4. Enforces Upstox deterministic intent IDs, `UNKNOWN_AMBIGUOUS` timeout halts,
   buy-wing-first spread execution (`short_qty <= confirmed_protected_qty`), and
   post-GTT-cancel order+position reconciliation before manual exit (prevents double-sell).
5. Uses canonical `app.analytics.common.options_accounting` for P&L and `₹0.05` tick rounding.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from app.analytics.common.options_accounting import (
    OptionLeg,
    compute_package_accounting,
    round_to_nse_tick,
)
from app.analytics.option_sniffer_v2_live.v2_live_ledger import (
    V2LiveLedgerStore,
    make_deterministic_intent_id,
    now_ist_iso,
)
from app.analytics.option_sniffer_v2_live.v2_upstox_broker_client import (
    UPSTOX_VERIFIED_API_FACTS,
    BrokerOrderRejectedError,
    BrokerTimeoutAmbiguousError,
    UpstoxV2BrokerClient,
    V2BrokerClientProtocol,
    sanitize_upstox_tag,
)

logger = logging.getLogger(__name__)

ALLOWED_LIVE_BOOKS = {"V2_BOOK_B_PAID_FLOW"}
OPEN_LIKE_PACKAGE_STATES = [
    "OPENING_BUY_WING",
    "OPENING_SHORT",
    "OPEN",
    "CLOSING",
    "UNWINDING_INCOMPLETE",
    "MANUAL_INTERVENTION_REQUIRED",
    "RECONCILE_MISMATCH",
]

_ENGINE_LOCK = threading.RLock()
_ENGINE_INSTANCE: Optional["V2LiveExecutionEngine"] = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _generate_4digit_pin() -> str:
    env_pin = (os.environ.get("OSV2_LIVE_PIN") or "").strip()
    if len(env_pin) == 4 and env_pin.isdigit():
        return env_pin
    return f"{1000 + secrets.randbelow(9000)}"


class V2LiveExecutionEngine:
    """Isolated V2 live order coordinator (paper engine and ledger untouched)."""

    def __init__(
        self,
        *,
        db_path: Optional[str] = None,
        broker_client: Optional[V2BrokerClientProtocol] = None,
        initial_pin: Optional[str] = None,
        log_pin_on_boot: bool = True,
    ):
        self._lock = threading.RLock()
        self.store = V2LiveLedgerStore(db_path)
        self.broker: V2BrokerClientProtocol = broker_client or UpstoxV2BrokerClient()
        self._has_custom_broker = broker_client is not None
        if initial_pin is not None:
            pin_s = str(initial_pin).strip()
            if not (len(pin_s) == 4 and pin_s.isdigit()):
                raise ValueError("Live PIN must be exactly 4 numeric digits.")
            self._session_pin = pin_s
        else:
            self._session_pin = _generate_4digit_pin()

        # Fail-safe: any pre-existing live DB boots disarmed without creating a cold DB
        if self.store.db_exists():
            self.store.force_boot_disarm_if_exists()
        if log_pin_on_boot:
            logger.warning(
                "[V2LiveExecutionEngine] Booted DISARMED (db=%s). Interactive human session PIN=%s",
                self.store.db_path,
                self._session_pin,
            )

    def _read_paper_baseline_summary(self) -> Dict[str, Any]:
        """Strictly read-only snapshot of V2 paper baseline metrics if V2 paper DB exists."""
        try:
            from app.analytics.option_sniffer_v2.v2_service import get_option_sniffer_v2_service

            svc = get_option_sniffer_v2_service()
            if not svc.store.exists():
                return {
                    "available": False,
                    "reason": "PAPER_DB_COLD_NOT_INITIALIZED",
                    "simulation_mode_baseline": True,
                }
            ov = svc.get_overview(include_collateral=False)
            fwd = ov.get("forward_paper_summary") or {}
            dd_corr = fwd.get("drawdown_audit_correction") or {}
            return {
                "available": True,
                "simulation_mode_baseline": True,
                "config_hash": ov.get("config_hash"),
                "closed_trades": fwd.get("closed_trades", 0),
                "open_packages": fwd.get("open_count", 0),
                "realized_net_pnl_inr": fwd.get("net_pnl_inr", 0.0),
                "observed_mark_max_dd_inr": fwd.get("observed_mark_dd_inr", dd_corr.get("observed_mark_max_dd_inr")),
                "stress_max_dd_inr": fwd.get("stress_dd_inr", dd_corr.get("stress_max_dd_inr")),
                "feed_health": (ov.get("feed_hygiene") or {}).get("feed_consumer"),
            }
        except Exception as exc:
            logger.debug("Read-only paper baseline snapshot unavailable: %s", exc)
            return {"available": False, "reason": str(exc), "simulation_mode_baseline": True}

    def get_status(self, *, include_paper_baseline: bool = True) -> Dict[str, Any]:
        """Cold-start safe read-only status (zero DB creation, PIN never exposed)."""
        arm_state = self.store.get_arm_state()
        faults = self.store.count_unresolved_faults()
        open_pkgs = self.store.list_packages(states=OPEN_LIKE_PACKAGE_STATES)
        closed_pkgs = self.store.list_packages(states=["CLOSED"], limit=50)
        today = time.strftime("%Y-%m-%d")
        daily_realized = round(
            sum(float(p.get("realized_net_inr") or 0.0) for p in closed_pkgs if p.get("session_date") == today),
            2,
        )
        return {
            "service": "OPTION_SNIFFER_ADVANCE_V2_LIVE",
            "isolated_db_path": self.store.db_path,
            "db_exists": self.store.db_exists(),
            "simulation_mode_baseline_preserved": True,
            "execution_mode": "LIVE_ARMED" if arm_state.get("armed") else "DISARMED_PAPER_BASELINE_ONLY",
            "armed": bool(arm_state.get("armed")),
            "pin_required": True,
            "arm_state": arm_state,
            "open_packages": open_pkgs,
            "open_package_count": len(open_pkgs),
            "closed_package_count": len(closed_pkgs),
            "daily_realized_net_inr": daily_realized,
            "unresolved_faults": faults,
            "paper_baseline_summary": self._read_paper_baseline_summary() if include_paper_baseline else None,
            "upstox_verified_api_facts": UPSTOX_VERIFIED_API_FACTS,
            "disclaimer": "An AI backtest or paper baseline does not prove future income. Broker orders execute only when armed by human PIN.",
        }

    def get_positions(self) -> Dict[str, Any]:
        return {
            "db_exists": self.store.db_exists(),
            "open_packages": self.store.list_packages(states=OPEN_LIKE_PACKAGE_STATES),
            "closed_packages": self.store.list_packages(states=["CLOSED"], limit=100),
            "recent_intents": self.store.list_intents(limit=100),
            "unresolved_faults": self.store.count_unresolved_faults(),
        }

    def get_audit_log(self, limit: int = 100) -> Dict[str, Any]:
        return {
            "db_exists": self.store.db_exists(),
            "entries": self.store.list_audit_log(limit=limit),
        }

    def arm_live(
        self,
        *,
        pin: str,
        human_confirmed: bool,
        selected_books: List[str],
        protocol_profile_id: str = "V2_BOOK_B_FROZEN_PAPER",
        max_lots_per_book: int = 1,
        max_daily_loss_inr: float = 2500.0,
        max_open_packages: int = 1,
        actor: str = "HUMAN_UI",
    ) -> Dict[str, Any]:
        with self._lock:
            if not human_confirmed:
                raise ValueError("Explicit human confirmation checkbox is required before arming live execution.")
            pin_clean = str(pin or "").strip()
            if not hmac.compare_digest(pin_clean, self._session_pin):
                if self.store.db_exists():
                    self.store.append_audit("ARM_REJECTED_BAD_PIN", actor, {"ts_iso": now_ist_iso()})
                raise PermissionError("Invalid 4-digit human security PIN.")

            books = [str(b).strip() for b in (selected_books or []) if str(b).strip()]
            if not books:
                raise ValueError("At least one book must be selected to arm live execution.")
            disallowed = [b for b in books if b not in ALLOWED_LIVE_BOOKS]
            if disallowed:
                raise ValueError(
                    f"Book(s) {disallowed} are blocked from live execution due to unverified spec conflicts."
                )
            if int(max_lots_per_book) < 1 or int(max_lots_per_book) > 10:
                raise ValueError("max_lots_per_book must be between 1 and 10.")
            if float(max_daily_loss_inr) <= 0:
                raise ValueError("max_daily_loss_inr must be positive.")
            if int(max_open_packages) < 1 or int(max_open_packages) > 5:
                raise ValueError("max_open_packages must be between 1 and 5.")

            faults = self.store.count_unresolved_faults()
            if faults["total_blocking_faults"] > 0:
                raise RuntimeError(
                    f"Cannot arm with {faults['total_blocking_faults']} unresolved fault(s); run reconciliation first."
                )

            funds_snapshot: Dict[str, Any] = {}
            try:
                funds_snapshot = self.broker.get_funds_and_margin()
            except Exception as exc:
                raise RuntimeError(f"Broker pre-flight funds check failed; refusing to arm: {exc}") from exc

            state = self.store.set_arm_state(
                armed=True,
                selected_books=books,
                protocol_profile_id=protocol_profile_id,
                max_lots_per_book=int(max_lots_per_book),
                max_daily_loss_inr=float(max_daily_loss_inr),
                max_open_packages=int(max_open_packages),
                reason="ARMED_BY_HUMAN_PIN",
                actor=actor,
            )
            self.store.append_audit(
                "PREFLIGHT_FUNDS_VERIFIED",
                actor,
                {"funds": funds_snapshot, "armed_for_date": state.get("armed_for_date")},
            )
            return self.get_status()

    def disarm_live(self, *, reason: str = "HUMAN_DISARMED", actor: str = "HUMAN_UI") -> Dict[str, Any]:
        with self._lock:
            cur = self.store.get_arm_state()
            self.store.set_arm_state(
                armed=False,
                selected_books=cur.get("selected_books") or ["V2_BOOK_B_PAID_FLOW"],
                protocol_profile_id=cur.get("protocol_profile_id") or "V2_BOOK_B_FROZEN_PAPER",
                max_lots_per_book=int(cur.get("max_lots_per_book") or 1),
                max_daily_loss_inr=float(cur.get("max_daily_loss_inr") or 2500.0),
                max_open_packages=int(cur.get("max_open_packages") or 1),
                reason=reason,
                actor=actor,
            )
            return self.get_status()

    def _compute_realized_pkg_accounting(self, pkg: Dict[str, Any]) -> Dict[str, Any]:
        legs = pkg.get("legs") or {}
        acc_legs: List[OptionLeg] = []
        buy_leg = legs.get("buy_leg") or {}
        if float(buy_leg.get("entry_price") or 0) > 0 and float(buy_leg.get("exit_price") or 0) > 0 and int(buy_leg.get("qty") or 0) > 0:
            acc_legs.append(
                OptionLeg(
                    option_type=str(buy_leg.get("option_type") or "CE"),
                    strike=float(buy_leg.get("strike") or 0.0),
                    qty=int(buy_leg["qty"]),
                    entry_price=float(buy_leg["entry_price"]),
                    exit_price=float(buy_leg["exit_price"]),
                    label=str(buy_leg.get("trading_symbol") or "BUY_WING"),
                )
            )
        short_leg = legs.get("short_leg") or {}
        if float(short_leg.get("entry_price") or 0) > 0 and float(short_leg.get("exit_price") or 0) > 0 and int(short_leg.get("qty") or 0) > 0:
            acc_legs.append(
                OptionLeg(
                    option_type=str(short_leg.get("option_type") or "CE"),
                    strike=float(short_leg.get("strike") or 0.0),
                    qty=-abs(int(short_leg["qty"])),
                    entry_price=float(short_leg["entry_price"]),
                    exit_price=float(short_leg["exit_price"]),
                    label=str(short_leg.get("trading_symbol") or "SHORT_LEG"),
                )
            )
        if not acc_legs:
            return {"gross_pnl": 0.0, "total_friction": 0.0, "net_pnl": 0.0}
        return compute_package_accounting(acc_legs)

    def _place_single_order_with_durability(
        self,
        *,
        signal_id: str,
        live_package_id: str,
        book_id: str,
        leg_role: str,
        action: str,
        session_date: str,
        instrument_key: str,
        trading_symbol: str,
        side: str,
        order_type: str,
        qty: int,
        limit_price: Optional[float] = None,
        market_protection: Optional[float] = None,
        is_emergency: bool = False,
    ) -> Dict[str, Any]:
        intent_id = make_deterministic_intent_id(signal_id, book_id, leg_role, action, session_date)
        tag = sanitize_upstox_tag(f"V2-{intent_id[-14:]}")
        now_ms = _now_ms()
        rec, created = self.store.create_or_get_intent(
            {
                "intent_id": intent_id,
                "signal_id": signal_id,
                "live_package_id": live_package_id,
                "book_id": book_id,
                "leg_role": leg_role,
                "action": action,
                "session_date": session_date,
                "instrument_key": instrument_key,
                "trading_symbol": trading_symbol,
                "side": side,
                "order_type": order_type,
                "product": "D",
                "requested_qty": int(qty),
                "limit_price": round_to_nse_tick(float(limit_price)) if limit_price else None,
                "market_protection": market_protection if order_type in ("MARKET", "SL-M") else None,
                "tag": tag,
                "state": "PENDING_SEND",
                "created_ts_ms": now_ms,
                "updated_ts_ms": now_ms,
            }
        )
        if not created:
            return {"idempotent_replay": True, "intent": rec, "status": rec["state"]}

        payload = {
            "quantity": int(qty),
            "product": "D",
            "validity": "DAY",
            "price": round_to_nse_tick(float(limit_price)) if limit_price else 0.0,
            "tag": tag,
            "instrument_token": instrument_key,
            "trading_symbol": trading_symbol,
            "order_type": order_type,
            "transaction_type": side,
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
        }
        if order_type in ("MARKET", "SL-M") and market_protection is not None:
            payload["market_protection"] = market_protection

        try:
            resp = self.broker.place_order(payload, is_emergency=is_emergency)
        except BrokerTimeoutAmbiguousError as exc:
            updated = self.store.update_intent(
                intent_id,
                state="UNKNOWN_AMBIGUOUS",
                updated_ts_ms=_now_ms(),
                error_detail=str(exc),
            )
            self.store.append_audit(
                "ORDER_TIMEOUT_AMBIGUOUS",
                "ENGINE",
                {"intent_id": intent_id, "tag": tag, "instrument_key": instrument_key, "error": str(exc)},
            )
            return {"idempotent_replay": False, "intent": updated, "status": "UNKNOWN_AMBIGUOUS", "error": str(exc)}
        except BrokerOrderRejectedError as exc:
            updated = self.store.update_intent(
                intent_id,
                state="REJECTED",
                updated_ts_ms=_now_ms(),
                error_detail=str(exc),
            )
            self.store.append_audit(
                "ORDER_REJECTED",
                "ENGINE",
                {"intent_id": intent_id, "tag": tag, "instrument_key": instrument_key, "error": str(exc)},
            )
            return {"idempotent_replay": False, "intent": updated, "status": "REJECTED", "error": str(exc)}

        data = resp.get("data") or {}
        oid = str(data.get("order_id") or (data.get("order_ids") or [""])[0] or "")
        order_obj = data.get("order") or {}
        filled_qty = int(order_obj.get("filled_quantity") or 0)
        avg_px = float(order_obj.get("average_price") or (limit_price or 0.0))
        if filled_qty <= 0 and oid:
            # Query order book if broker ack omitted inline order object
            for o in self.broker.get_orders():
                if str(o.get("order_id")) == oid:
                    filled_qty = int(o.get("filled_quantity") or 0)
                    avg_px = float(o.get("average_price") or avg_px)
                    break

        if filled_qty == int(qty):
            st = "FILLED"
        elif filled_qty > 0:
            st = "PARTIAL_FILL"
        else:
            st = "REJECTED"

        updated = self.store.update_intent(
            intent_id,
            state=st,
            updated_ts_ms=_now_ms(),
            filled_qty=filled_qty,
            avg_fill_price=avg_px,
            broker_order_id=oid or None,
            raw_response=resp,
        )
        return {"idempotent_replay": False, "intent": updated, "status": st}

    def _unwind_buy_wing(
        self,
        *,
        pkg: Dict[str, Any],
        qty_to_unwind: int,
        unwind_price: float,
        reason: str,
    ) -> Dict[str, Any]:
        buy_leg = pkg["legs"]["buy_leg"]
        res = self._place_single_order_with_durability(
            signal_id=pkg["signal_id"],
            live_package_id=pkg["live_package_id"],
            book_id=pkg["book_id"],
            leg_role="BUY_WING_UNWIND",
            action=f"UNWIND_{qty_to_unwind}",
            session_date=pkg["session_date"],
            instrument_key=buy_leg["instrument_key"],
            trading_symbol=buy_leg["trading_symbol"],
            side="SELL",
            order_type="MARKET",
            qty=int(qty_to_unwind),
            limit_price=unwind_price,
            market_protection=5.0,
            is_emergency=True,
        )
        intent = res["intent"]
        if res["status"] == "FILLED" and int(intent.get("filled_qty") or 0) == int(qty_to_unwind):
            return {"ok": True, "filled_qty": int(intent["filled_qty"]), "avg_price": float(intent["avg_fill_price"] or unwind_price), "intent": intent}
        return {"ok": False, "status": res["status"], "error": res.get("error") or intent.get("error_detail"), "intent": intent}

    def submit_entry_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            arm_state = self.store.get_arm_state()
            if not arm_state.get("armed"):
                return {"ok": False, "status": "DISARMED_NEW_ENTRIES_BLOCKED"}

            signal_id = str(signal.get("signal_id") or "").strip()
            book_id = str(signal.get("book_id") or "").strip()
            session_date = str(signal.get("session_date") or "").strip()
            structure = str(signal.get("structure") or "SINGLE_LONG").strip()
            if not signal_id or not book_id or not session_date:
                return {"ok": False, "status": "INVALID_SIGNAL_IDENTITY"}
            if book_id not in (arm_state.get("selected_books") or []) or book_id not in ALLOWED_LIVE_BOOKS:
                return {"ok": False, "status": "BOOK_NOT_ARMED_OR_BLOCKED"}

            if not bool(signal.get("quote_fresh_ok", True)):
                return {"ok": False, "status": "STALE_OR_MISSING_QUOTE"}

            # Idempotency check BEFORE fault or limit rejection on duplicate replays
            existing_pkg = self.store.get_package_by_signal(signal_id)
            buy_intent_id = make_deterministic_intent_id(signal_id, book_id, "BUY_WING", "ENTRY", session_date)
            existing_intent = self.store.get_intent(buy_intent_id)
            if existing_pkg is not None or existing_intent is not None:
                self.store.append_audit(
                    "DUPLICATE_SIGNAL_IGNORED",
                    "ENGINE",
                    {"signal_id": signal_id, "live_package_id": existing_pkg["live_package_id"] if existing_pkg else None},
                )
                return {
                    "ok": True,
                    "status": "DUPLICATE_SIGNAL_IDEMPOTENT",
                    "idempotent_replay": True,
                    "package": existing_pkg,
                    "intent": existing_intent,
                }

            faults = self.store.count_unresolved_faults()
            if faults["total_blocking_faults"] > 0:
                return {"ok": False, "status": "HALTED_UNRESOLVED_FAULT", "unresolved_faults": faults}

            open_pkgs = self.store.list_packages(states=OPEN_LIKE_PACKAGE_STATES)
            if len(open_pkgs) >= int(arm_state.get("max_open_packages") or 1):
                return {"ok": False, "status": "MAX_OPEN_PACKAGES_REACHED"}

            closed_today = [p for p in self.store.list_packages(states=["CLOSED"], limit=100) if p["session_date"] == session_date]
            daily_net = sum(float(p.get("realized_net_inr") or 0.0) for p in closed_today)
            if daily_net <= -abs(float(arm_state.get("max_daily_loss_inr") or 2500.0)):
                return {"ok": False, "status": "MAX_DAILY_LOSS_REACHED", "daily_net_inr": round(daily_net, 2)}

            buy_leg = dict(signal.get("buy_leg") or {})
            buy_qty = int(buy_leg.get("qty") or 0)
            lot_size = int(buy_leg.get("lot_size") or 65)
            max_qty = int(arm_state.get("max_lots_per_book") or 1) * lot_size
            if buy_qty <= 0 or float(buy_leg.get("limit_price") or 0) <= 0:
                return {"ok": False, "status": "INVALID_BUY_LEG"}
            if buy_qty > max_qty:
                return {"ok": False, "status": "LOT_LIMIT_EXCEEDED", "requested_qty": buy_qty, "max_qty": max_qty}

            live_package_id = f"osv2l_{signal_id[:16]}"
            pkg: Dict[str, Any] = {
                "live_package_id": live_package_id,
                "signal_id": signal_id,
                "session_date": session_date,
                "book_id": book_id,
                "protocol_profile_id": str(signal.get("protocol_profile_id") or arm_state.get("protocol_profile_id") or "V2_BOOK_B_FROZEN_PAPER"),
                "structure": structure,
                "state": "OPENING_BUY_WING",
                "entry_ts_ms": _now_ms(),
                "exit_ts_ms": None,
                "exit_reason": None,
                "legs": {
                    "buy_leg": {
                        "instrument_key": buy_leg["instrument_key"],
                        "trading_symbol": buy_leg["trading_symbol"],
                        "option_type": buy_leg.get("option_type", "CE"),
                        "strike": float(buy_leg.get("strike") or 0.0),
                        "lot_size": lot_size,
                        "requested_qty": buy_qty,
                        "qty": 0,
                        "limit_price": round_to_nse_tick(float(buy_leg["limit_price"])),
                        "entry_price": 0.0,
                        "exit_price": None,
                        "stop_trigger_price": buy_leg.get("stop_trigger_price"),
                        "stop_limit_price": buy_leg.get("stop_limit_price"),
                        "gtt_id": None,
                    }
                },
                "realized_gross_inr": 0.0,
                "realized_friction_inr": 0.0,
                "realized_net_inr": 0.0,
                "reconciled_at_iso": None,
                "unresolved_fault": None,
            }
            if structure == "DEFINED_RISK_SPREAD":
                short_leg = dict(signal.get("short_leg") or {})
                if not short_leg.get("instrument_key") or float(short_leg.get("limit_price") or 0) <= 0:
                    return {"ok": False, "status": "INVALID_SHORT_LEG"}
                pkg["legs"]["short_leg"] = {
                    "instrument_key": short_leg["instrument_key"],
                    "trading_symbol": short_leg["trading_symbol"],
                    "option_type": short_leg.get("option_type", "CE"),
                    "strike": float(short_leg.get("strike") or 0.0),
                    "lot_size": int(short_leg.get("lot_size") or lot_size),
                    "requested_qty": int(short_leg.get("qty") or buy_qty),
                    "qty": 0,
                    "limit_price": round_to_nse_tick(float(short_leg["limit_price"])),
                    "entry_price": 0.0,
                    "exit_price": None,
                }
            self.store.upsert_package(pkg)

            # STEP 1: ALWAYS execute BUY_WING first (zero naked shorts)
            buy_res = self._place_single_order_with_durability(
                signal_id=signal_id,
                live_package_id=live_package_id,
                book_id=book_id,
                leg_role="BUY_WING",
                action="ENTRY",
                session_date=session_date,
                instrument_key=buy_leg["instrument_key"],
                trading_symbol=buy_leg["trading_symbol"],
                side="BUY",
                order_type=str(buy_leg.get("order_type") or "LIMIT"),
                qty=buy_qty,
                limit_price=float(buy_leg["limit_price"]),
                market_protection=buy_leg.get("market_protection"),
            )
            if buy_res["status"] == "UNKNOWN_AMBIGUOUS":
                pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                pkg["unresolved_fault"] = "BUY_WING_TIMEOUT_AMBIGUOUS"
                self.store.upsert_package(pkg)
                self.disarm_live(reason="AUTO_DISARM_ORDER_TIMEOUT_AMBIGUOUS", actor="SAFETY_GUARD")
                return {"ok": False, "status": "UNKNOWN_AMBIGUOUS", "package": pkg, "intent": buy_res["intent"]}

            buy_filled_qty = int(buy_res["intent"].get("filled_qty") or 0)
            if buy_filled_qty <= 0 or buy_res["status"] == "REJECTED":
                pkg["state"] = "CLOSED"
                pkg["exit_ts_ms"] = _now_ms()
                pkg["exit_reason"] = "ENTRY_BUY_REJECTED"
                self.store.upsert_package(pkg)
                return {"ok": False, "status": "BUY_LEG_REJECTED", "package": pkg, "intent": buy_res["intent"]}

            buy_avg_px = float(buy_res["intent"].get("avg_fill_price") or buy_leg["limit_price"])
            pkg["legs"]["buy_leg"]["qty"] = buy_filled_qty
            pkg["legs"]["buy_leg"]["entry_price"] = buy_avg_px

            if structure == "SINGLE_LONG":
                # Optional protective V3 GTT stop-loss
                stop_trig = buy_leg.get("stop_trigger_price")
                if stop_trig is not None and float(stop_trig) > 0:
                    stop_trig_r = round_to_nse_tick(float(stop_trig))
                    stop_lim_r = round_to_nse_tick(float(buy_leg.get("stop_limit_price") or max(0.05, stop_trig_r * 0.99)))
                    try:
                        gtt_resp = self.broker.place_gtt_order(
                            {
                                "type": "SINGLE",
                                "quantity": buy_filled_qty,
                                "product": "D",
                                "instrument_token": buy_leg["instrument_key"],
                                "trading_symbol": buy_leg["trading_symbol"],
                                "transaction_type": "SELL",
                                "rules": [
                                    {
                                        "strategy": "SL",
                                        "trigger_type": "BELOW",
                                        "trigger_price": stop_trig_r,
                                        "limit_price": stop_lim_r,
                                    }
                                ],
                            }
                        )
                        gdata = gtt_resp.get("data") or {}
                        gid = str(gdata.get("gtt_order_id") or (gdata.get("gtt_order_ids") or [""])[0] or "")
                        pkg["legs"]["buy_leg"]["gtt_id"] = gid or None
                    except Exception as exc:
                        self.store.append_audit(
                            "GTT_PLACEMENT_WARNING",
                            "ENGINE",
                            {"live_package_id": live_package_id, "error": str(exc)},
                        )
                pkg["state"] = "OPEN"
                self.store.upsert_package(pkg)
                self.store.append_audit(
                    "LIVE_PACKAGE_OPENED",
                    "ENGINE",
                    {"live_package_id": live_package_id, "structure": structure, "qty": buy_filled_qty},
                )
                return {"ok": True, "status": "OPEN", "package": pkg, "buy_intent": buy_res["intent"]}

            # DEFINED_RISK_SPREAD: Cap short quantity strictly at confirmed_protected_qty
            short_info = pkg["legs"]["short_leg"]
            s_lot = int(short_info.get("lot_size") or lot_size)
            short_target_qty = (min(int(short_info["requested_qty"]), buy_filled_qty) // s_lot) * s_lot
            if short_target_qty <= 0:
                unwind_res = self._unwind_buy_wing(
                    pkg=pkg, qty_to_unwind=buy_filled_qty, unwind_price=buy_avg_px, reason="PARTIAL_BUY_BELOW_ONE_LOT"
                )
                return self._finalize_failed_short_entry(pkg, buy_avg_px, buy_filled_qty, unwind_res, "PARTIAL_BUY_BELOW_LOT")

            pkg["state"] = "OPENING_SHORT"
            self.store.upsert_package(pkg)

            short_res = self._place_single_order_with_durability(
                signal_id=signal_id,
                live_package_id=live_package_id,
                book_id=book_id,
                leg_role="SHORT_LEG",
                action="ENTRY",
                session_date=session_date,
                instrument_key=short_info["instrument_key"],
                trading_symbol=short_info["trading_symbol"],
                side="SELL",
                order_type=str(signal["short_leg"].get("order_type") or "LIMIT"),
                qty=short_target_qty,
                limit_price=float(short_info["limit_price"]),
                market_protection=signal["short_leg"].get("market_protection"),
            )
            if short_res["status"] == "UNKNOWN_AMBIGUOUS":
                pkg["state"] = "RECONCILE_MISMATCH"
                pkg["unresolved_fault"] = "SHORT_LEG_TIMEOUT_AMBIGUOUS"
                self.store.upsert_package(pkg)
                self.disarm_live(reason="AUTO_DISARM_SHORT_TIMEOUT_AMBIGUOUS", actor="SAFETY_GUARD")
                return {"ok": False, "status": "SHORT_TIMEOUT_AMBIGUOUS", "package": pkg, "short_intent": short_res["intent"]}

            short_filled_qty = int(short_res["intent"].get("filled_qty") or 0)
            if short_filled_qty <= 0 or short_res["status"] == "REJECTED":
                unwind_res = self._unwind_buy_wing(
                    pkg=pkg, qty_to_unwind=buy_filled_qty, unwind_price=buy_avg_px, reason="SHORT_LEG_REJECTED"
                )
                return self._finalize_failed_short_entry(pkg, buy_avg_px, buy_filled_qty, unwind_res, "SHORT_LEG_REJECTED_UNWOUND")

            short_avg_px = float(short_res["intent"].get("avg_fill_price") or short_info["limit_price"])
            pkg["legs"]["short_leg"]["qty"] = short_filled_qty
            pkg["legs"]["short_leg"]["entry_price"] = short_avg_px

            # Strictly verify confirmed_protected_qty == short_filled_qty; unwind any excess buy wing
            if buy_filled_qty > short_filled_qty:
                excess = buy_filled_qty - short_filled_qty
                unwind_excess = self._unwind_buy_wing(
                    pkg=pkg, qty_to_unwind=excess, unwind_price=buy_avg_px, reason="EXCESS_BUY_WING_UNWIND"
                )
                if not unwind_excess["ok"]:
                    pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                    pkg["unresolved_fault"] = "EXCESS_BUY_WING_UNWIND_FAILED"
                    self.store.upsert_package(pkg)
                    self.disarm_live(reason="AUTO_DISARM_EXCESS_UNWIND_FAIL", actor="SAFETY_GUARD")
                    return {"ok": False, "status": "MANUAL_INTERVENTION_REQUIRED", "package": pkg}
                pkg["legs"]["buy_leg"]["qty"] = short_filled_qty

            assert pkg["legs"]["short_leg"]["qty"] <= pkg["legs"]["buy_leg"]["qty"]
            pkg["state"] = "OPEN"
            self.store.upsert_package(pkg)
            self.store.append_audit(
                "LIVE_SPREAD_OPENED",
                "ENGINE",
                {
                    "live_package_id": live_package_id,
                    "buy_qty": pkg["legs"]["buy_leg"]["qty"],
                    "short_qty": pkg["legs"]["short_leg"]["qty"],
                },
            )
            return {"ok": True, "status": "OPEN", "package": pkg, "buy_intent": buy_res["intent"], "short_intent": short_res["intent"]}

    def _finalize_failed_short_entry(
        self,
        pkg: Dict[str, Any],
        buy_avg_px: float,
        buy_filled_qty: int,
        unwind_res: Dict[str, Any],
        exit_reason: str,
    ) -> Dict[str, Any]:
        if unwind_res["ok"]:
            pkg["legs"]["buy_leg"]["exit_price"] = unwind_res["avg_price"]
            acc = self._compute_realized_pkg_accounting(pkg)
            pkg["state"] = "CLOSED"
            pkg["exit_ts_ms"] = _now_ms()
            pkg["exit_reason"] = exit_reason
            pkg["realized_gross_inr"] = float(acc["gross_pnl"])
            pkg["realized_friction_inr"] = float(acc["total_friction"])
            pkg["realized_net_inr"] = float(acc["net_pnl"])
            self.store.upsert_package(pkg)
            self.store.append_audit(
                "BUY_WING_UNWOUND_SAFELY",
                "ENGINE",
                {"live_package_id": pkg["live_package_id"], "net_pnl_inr": acc["net_pnl"]},
            )
            return {"ok": False, "status": exit_reason, "package": pkg}
        pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
        pkg["unresolved_fault"] = f"BUY_WING_UNWIND_FAILED: {unwind_res.get('error')}"
        self.store.upsert_package(pkg)
        self.disarm_live(reason="AUTO_DISARM_BUY_WING_UNWIND_FAILED", actor="SAFETY_GUARD")
        self.store.append_audit(
            "CRITICAL_MANUAL_INTERVENTION_REQUIRED",
            "SAFETY_GUARD",
            {"live_package_id": pkg["live_package_id"], "error": unwind_res.get("error")},
        )
        return {"ok": False, "status": "MANUAL_INTERVENTION_REQUIRED", "package": pkg}

    def exit_live_package(
        self,
        *,
        live_package_id: str,
        reason: str = "MANUAL_HUMAN_SQUARE_OFF",
        exit_price: Optional[float] = None,
        short_exit_price: Optional[float] = None,
        actor: str = "HUMAN_UI",
    ) -> Dict[str, Any]:
        """Exit an open live package. Explicitly permitted even while DISARMED."""
        with self._lock:
            pkg = self.store.get_package(live_package_id)
            if not pkg:
                raise KeyError(f"Unknown live package: {live_package_id}")
            if pkg["state"] == "CLOSED":
                return {"ok": True, "status": "ALREADY_CLOSED", "package": pkg}

            pkg["state"] = "CLOSING"
            self.store.upsert_package(pkg)
            buy_leg = pkg["legs"].get("buy_leg") or {}
            short_leg = pkg["legs"].get("short_leg") or {}

            # 1. Cancel active protective GTT (if any) AND ALWAYS reconcile orders + positions before selling!
            gtt_id = buy_leg.get("gtt_id")
            if gtt_id:
                try:
                    self.broker.cancel_gtt_order(gtt_id, is_emergency=True)
                except Exception as exc:
                    self.store.append_audit(
                        "GTT_CANCEL_WARNING",
                        actor,
                        {"live_package_id": live_package_id, "gtt_id": gtt_id, "error": str(exc)},
                    )

                # Mandatory Upstox V3 GTT cancel race check: inspect BOTH orders and positions BEFORE placing exit sell
                broker_orders = self.broker.get_orders()
                broker_positions = self.broker.get_positions()
                pos_qty = 0
                for bp in broker_positions:
                    if str(bp.get("instrument_token")) == str(buy_leg["instrument_key"]):
                        pos_qty = int(bp.get("quantity") or 0)
                        break

                gtt_child_order = None
                for bo in broker_orders:
                    if (
                        str(bo.get("instrument_token")) == str(buy_leg["instrument_key"])
                        and str(bo.get("transaction_type")).upper() == "SELL"
                        and str(bo.get("status")).lower() == "complete"
                        and (str(bo.get("parent_gtt_id") or "") == str(gtt_id) or str(bo.get("tag") or "").startswith("GTT-"))
                    ):
                        gtt_child_order = bo
                        break

                if pos_qty <= 0 and gtt_child_order is not None and pkg["structure"] == "SINGLE_LONG":
                    trig_fill_px = float(gtt_child_order.get("average_price") or buy_leg["entry_price"])
                    pkg["legs"]["buy_leg"]["exit_price"] = trig_fill_px
                    pkg["legs"]["buy_leg"]["gtt_id"] = None
                    acc = self._compute_realized_pkg_accounting(pkg)
                    pkg["state"] = "CLOSED"
                    pkg["exit_ts_ms"] = _now_ms()
                    pkg["exit_reason"] = "CLOSED_BY_TRIGGERED_GTT_NO_DOUBLE_SELL"
                    pkg["realized_gross_inr"] = float(acc["gross_pnl"])
                    pkg["realized_friction_inr"] = float(acc["total_friction"])
                    pkg["realized_net_inr"] = float(acc["net_pnl"])
                    pkg["reconciled_at_iso"] = now_ist_iso()
                    pkg["unresolved_fault"] = None
                    self.store.upsert_package(pkg)
                    self.store.append_audit(
                        "GTT_RACE_PREVENTED_DOUBLE_SELL",
                        actor,
                        {
                            "live_package_id": live_package_id,
                            "gtt_id": gtt_id,
                            "child_order_id": gtt_child_order.get("order_id"),
                            "exit_price": trig_fill_px,
                            "net_pnl_inr": acc["net_pnl"],
                        },
                    )
                    return {"ok": True, "status": "CLOSED_BY_TRIGGERED_GTT_NO_DOUBLE_SELL", "package": pkg}

            # 2. For spreads, ALWAYS cover SHORT_LEG BEFORE selling BUY_WING (never expose a naked short)
            if short_leg and int(short_leg.get("qty") or 0) > 0 and short_leg.get("exit_price") is None:
                s_px = float(short_exit_price if short_exit_price is not None else short_leg["entry_price"])
                s_exit = self._place_single_order_with_durability(
                    signal_id=pkg["signal_id"],
                    live_package_id=live_package_id,
                    book_id=pkg["book_id"],
                    leg_role="SHORT_LEG",
                    action="EXIT",
                    session_date=pkg["session_date"],
                    instrument_key=short_leg["instrument_key"],
                    trading_symbol=short_leg["trading_symbol"],
                    side="BUY",
                    order_type="MARKET",
                    qty=int(short_leg["qty"]),
                    limit_price=s_px,
                    market_protection=5.0,
                    is_emergency=True,
                )
                if s_exit["status"] != "FILLED":
                    pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                    pkg["unresolved_fault"] = f"SHORT_EXIT_FAILED: {s_exit['status']}"
                    self.store.upsert_package(pkg)
                    return {"ok": False, "status": s_exit["status"], "package": pkg}
                pkg["legs"]["short_leg"]["exit_price"] = float(s_exit["intent"].get("avg_fill_price") or s_px)

            # 3. Close BUY_WING
            if buy_leg and int(buy_leg.get("qty") or 0) > 0 and buy_leg.get("exit_price") is None:
                b_px = float(exit_price if exit_price is not None else buy_leg["entry_price"])
                b_exit = self._place_single_order_with_durability(
                    signal_id=pkg["signal_id"],
                    live_package_id=live_package_id,
                    book_id=pkg["book_id"],
                    leg_role="BUY_WING",
                    action="EXIT",
                    session_date=pkg["session_date"],
                    instrument_key=buy_leg["instrument_key"],
                    trading_symbol=buy_leg["trading_symbol"],
                    side="SELL",
                    order_type="MARKET",
                    qty=int(buy_leg["qty"]),
                    limit_price=b_px,
                    market_protection=5.0,
                    is_emergency=True,
                )
                if b_exit["status"] != "FILLED":
                    pkg["state"] = "MANUAL_INTERVENTION_REQUIRED"
                    pkg["unresolved_fault"] = f"BUY_EXIT_FAILED: {b_exit['status']}"
                    self.store.upsert_package(pkg)
                    return {"ok": False, "status": b_exit["status"], "package": pkg}
                pkg["legs"]["buy_leg"]["exit_price"] = float(b_exit["intent"].get("avg_fill_price") or b_px)

            acc = self._compute_realized_pkg_accounting(pkg)
            pkg["state"] = "CLOSED"
            pkg["exit_ts_ms"] = _now_ms()
            pkg["exit_reason"] = reason
            pkg["realized_gross_inr"] = float(acc["gross_pnl"])
            pkg["realized_friction_inr"] = float(acc["total_friction"])
            pkg["realized_net_inr"] = float(acc["net_pnl"])
            pkg["reconciled_at_iso"] = now_ist_iso()
            pkg["unresolved_fault"] = None
            self.store.upsert_package(pkg)
            self.store.append_audit(
                "LIVE_PACKAGE_CLOSED",
                actor,
                {"live_package_id": live_package_id, "reason": reason, "net_pnl_inr": acc["net_pnl"]},
            )
            return {"ok": True, "status": "CLOSED", "package": pkg}

    def reconcile_with_broker(self, *, actor: str = "RECONCILER") -> Dict[str, Any]:
        """Reconciles pending/ambiguous intents and open packages against broker state."""
        with self._lock:
            broker_orders = self.broker.get_orders()
            broker_positions = self.broker.get_positions()
            active_gtts = self.broker.get_gtt_orders()

            orders_by_tag = {str(o.get("tag") or ""): o for o in broker_orders if o.get("tag")}
            orders_by_id = {str(o.get("order_id") or ""): o for o in broker_orders if o.get("order_id")}
            pos_by_key = {str(p.get("instrument_token") or ""): int(p.get("quantity") or 0) for p in broker_positions}

            resolved_intents: List[Dict[str, Any]] = []
            ambiguous_intents = self.store.list_intents(states=["UNKNOWN_AMBIGUOUS", "PENDING_SEND", "SENT"])
            for intent in ambiguous_intents:
                matched = orders_by_id.get(str(intent.get("broker_order_id") or "")) or orders_by_tag.get(str(intent["tag"]))
                pkg = self.store.get_package(intent["live_package_id"])
                if matched is not None:
                    m_status = str(matched.get("status") or "").lower()
                    fqty = int(matched.get("filled_quantity") or 0)
                    fpx = float(matched.get("average_price") or intent.get("limit_price") or 0.0)
                    new_state = "FILLED" if (m_status == "complete" and fqty >= int(intent["requested_qty"])) else ("PARTIAL_FILL" if fqty > 0 else "REJECTED")
                    upd = self.store.update_intent(
                        intent["intent_id"],
                        state=new_state,
                        updated_ts_ms=_now_ms(),
                        filled_qty=fqty,
                        avg_fill_price=fpx,
                        broker_order_id=str(matched.get("order_id") or ""),
                        error_detail=None,
                        raw_response=matched,
                    )
                    resolved_intents.append({"intent_id": intent["intent_id"], "resolution": f"MATCHED_{new_state}"})
                    if pkg and intent["leg_role"] == "BUY_WING" and intent["action"] == "ENTRY" and fqty > 0:
                        pkg["legs"]["buy_leg"]["qty"] = fqty
                        pkg["legs"]["buy_leg"]["entry_price"] = fpx
                        pkg["state"] = "OPEN"
                        pkg["reconciled_at_iso"] = now_ist_iso()
                        pkg["unresolved_fault"] = None
                        self.store.upsert_package(pkg)
                else:
                    # Confirmed absent in broker orders -> safe EXPIRED_NOT_FOUND
                    upd = self.store.update_intent(
                        intent["intent_id"],
                        state="EXPIRED_NOT_FOUND",
                        updated_ts_ms=_now_ms(),
                        error_detail="RECONCILED_NOT_FOUND_AT_BROKER",
                    )
                    resolved_intents.append({"intent_id": intent["intent_id"], "resolution": "EXPIRED_NOT_FOUND"})
                    if pkg and pkg["state"] in ("MANUAL_INTERVENTION_REQUIRED", "OPENING_BUY_WING") and intent["action"] == "ENTRY":
                        pkg["state"] = "CLOSED"
                        pkg["exit_ts_ms"] = _now_ms()
                        pkg["exit_reason"] = "RECONCILED_ENTRY_NOT_RECEIVED"
                        pkg["reconciled_at_iso"] = now_ist_iso()
                        pkg["unresolved_fault"] = None
                        self.store.upsert_package(pkg)

            # Reconcile open packages against actual broker net positions
            pkg_reconciliations: List[Dict[str, Any]] = []
            for pkg in self.store.list_packages(states=OPEN_LIKE_PACKAGE_STATES):
                buy_leg = pkg["legs"].get("buy_leg") or {}
                ikey = str(buy_leg.get("instrument_key") or "")
                actual_qty = pos_by_key.get(ikey, 0)
                if pkg["state"] == "OPEN" and pkg["structure"] == "SINGLE_LONG" and actual_qty == 0 and int(buy_leg.get("qty") or 0) > 0:
                    # Check if flattened by GTT or manual exit on broker
                    child_sell = None
                    for bo in broker_orders:
                        if str(bo.get("instrument_token")) == ikey and str(bo.get("transaction_type")).upper() == "SELL" and str(bo.get("status")).lower() == "complete":
                            child_sell = bo
                            break
                    if child_sell is not None:
                        pkg["legs"]["buy_leg"]["exit_price"] = float(child_sell.get("average_price") or buy_leg["entry_price"])
                        acc = self._compute_realized_pkg_accounting(pkg)
                        pkg["state"] = "CLOSED"
                        pkg["exit_ts_ms"] = _now_ms()
                        pkg["exit_reason"] = "RECONCILED_BROKER_EXIT_OR_GTT"
                        pkg["realized_gross_inr"] = float(acc["gross_pnl"])
                        pkg["realized_friction_inr"] = float(acc["total_friction"])
                        pkg["realized_net_inr"] = float(acc["net_pnl"])
                        pkg["reconciled_at_iso"] = now_ist_iso()
                        pkg["unresolved_fault"] = None
                        self.store.upsert_package(pkg)
                        pkg_reconciliations.append({"live_package_id": pkg["live_package_id"], "status": "CLOSED_FROM_BROKER_EXIT"})
                        continue
                pkg["reconciled_at_iso"] = now_ist_iso()
                self.store.upsert_package(pkg)
                pkg_reconciliations.append({"live_package_id": pkg["live_package_id"], "status": pkg["state"], "broker_pos_qty": actual_qty})

            faults = self.store.count_unresolved_faults()
            if self.store.db_exists():
                self.store.append_audit(
                    "RECONCILE_COMPLETED",
                    actor,
                    {
                        "resolved_intents": resolved_intents,
                        "package_reconciliations": pkg_reconciliations,
                        "active_gtts_count": len(active_gtts),
                        "blocking_faults_remaining": faults["total_blocking_faults"],
                    },
                )
            return {
                "ok": True,
                "resolved_intents": resolved_intents,
                "package_reconciliations": pkg_reconciliations,
                "active_gtts_count": len(active_gtts),
                "unresolved_faults": faults,
            }


def get_option_sniffer_v2_live_engine(
    *,
    db_path: Optional[str] = None,
    broker_client: Optional[V2BrokerClientProtocol] = None,
    initial_pin: Optional[str] = None,
) -> V2LiveExecutionEngine:
    global _ENGINE_INSTANCE
    with _ENGINE_LOCK:
        if _ENGINE_INSTANCE is None:
            _ENGINE_INSTANCE = V2LiveExecutionEngine(
                db_path=db_path,
                broker_client=broker_client,
                initial_pin=initial_pin,
            )
        return _ENGINE_INSTANCE


def reset_option_sniffer_v2_live_engine() -> None:
    global _ENGINE_INSTANCE
    with _ENGINE_LOCK:
        _ENGINE_INSTANCE = None
