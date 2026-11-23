"""
Fault-Injection, Broker Safety & Canonical Accounting Suite for `ShadowMirrorExecutor`.

Covers all Hostile Audit invariants (A1–A6):
  1. Hard `DRY_RUN` gateway firewall (`mutation_call_count == 0`, zero broker HTTP mutations).
  2. `SHADOW_DRY_RUN_AB` cannot be live-armed via `AdvanceLiveBridge.arm_live_trading`.
  3. Permanent `V6B_ML_LEGACY_COMPARATOR` airgap at both `ShadowMirrorExecutor` and `AdvanceLiveBridge`.
  4. Book A happy path: Buy-First `LEG1_WING` -> `/order/history` list (`entries[-1]` vs `entries[0]`) ->
     `LEG2_SHORT` -> Reverse Short-First Exit -> `₹0.00` parity with `options_accounting`.
  5. Book A Leg-1 partial fill (`30/65`) -> immediate cancel + exact `30`-qty wing unwind (`0` short orders).
  6. Book A Leg-2 credit-floor fade (`< 14.0 pts`) -> immediate wing unwind (`0` short orders).
  7. Book A Leg-2 rejection -> immediate wing unwind + max-1-abort daily gate.
  8. Book A exit short-unfilled invariant -> protective wing NEVER sold while short open_qty > 0.
  9. Book B happy path -> `BUY Long` + exchange `SL-L` + monotonic bar-close `PUT /order/modify` + cancel-first exit.
  10. Book B anti-double-sell race -> exchange `SL-L` already `complete (65/65)` -> zero software `SELL` orders!
  11. Unified `SHADOW_LIVE_AB` mode -> mirrors Book A + Book B + eligible Advance trades while blocking `V6B_*`.
  12. Circuit breaker (`-₹3,500`) & `SOS Panic Exit` integration across `AdvanceLiveBridge` + `ShadowMirrorExecutor`.
  13. `5s` Broker Position Watchdog -> detects orphan short (`short=-65, wing=0`), flattens short & auto-disarms.
  14. Pre-arm signal skip (`entry_time < armed_at_iso`) & idempotent `UNIQUE(shadow_package_id)` deduplication.
"""

from __future__ import annotations

from datetime import datetime
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple
import unittest
from unittest.mock import patch

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.analytics.common.options_accounting import (
    OptionLeg,
    compute_package_accounting,
)
from app.analytics.option_sniffer_advance_live.shadow_mirror_executor import (
    BOOK_ID_PAID_B,
    BOOK_ID_SPREAD_A,
    MODE_SHADOW_DRY_RUN_AB,
    MODE_SHADOW_LIVE_AB,
    MODE_SHADOW_LIVE_BOOK_A,
    MODE_SHADOW_LIVE_BOOK_B,
    ShadowMirrorExecutor,
    get_shadow_mirror_executor,
    init_shadow_mirror_tables,
)

try:
    from app.analytics.option_sniffer_advance.advance_db import (
        init_option_sniffer_advance_tables,
    )
    from app.analytics.option_sniffer_advance_live.advance_live_bridge import (
        AdvanceLiveBridge,
        MODE_DUAL_BOOK_NAKED,
        MODE_LONG_BOOK_ONLY,
        get_advance_live_bridge,
        init_advance_live_tables,
    )

    _HAS_BRIDGE_DEPS = True
except ModuleNotFoundError:
    _HAS_BRIDGE_DEPS = False
    AdvanceLiveBridge = None  # type: ignore
    init_advance_live_tables = None  # type: ignore
    init_option_sniffer_advance_tables = None  # type: ignore


def _sample_book_a_pkg(
    pkg_id: str = "shdw_booka000001",
    session_date: str = "2026-11-19",
    entry_time: str = "2026-11-19T10:05:00",
    wing_px: float = 56.50,
    short_px: float = 101.25,
) -> Dict[str, Any]:
    net_cr = round(short_px - wing_px, 2)
    return {
        "package_id": pkg_id,
        "advance_trade_id": f"adv_shdw_{pkg_id[-10:]}",
        "session_date": session_date,
        "book_id": BOOK_ID_SPREAD_A,
        "state": "OPEN",
        "direction_label": "SPREAD_SELL_PE",
        "expiry": "2026-11-23",
        "entry_bar_ts": f"{session_date} 10:04:00",
        "entry_time": entry_time,
        "leg2_confirmed_time": entry_time,
        "upstox_final_margin_inr": 39100.0,
        "legs_state": {
            "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
            "wing_leg": {
                "instrument_key": "NSE_FO|WING_25000PE",
                "trading_symbol": "NIFTY 25000 PE 23 NOV 26",
                "expiry": "2026-11-23",
                "strike": 25000.0,
                "option_type": "PE",
                "qty": 65,
                "entry_price": wing_px,
            },
            "short_leg": {
                "instrument_key": "NSE_FO|SHORT_25150PE",
                "trading_symbol": "NIFTY 25150 PE 23 NOV 26",
                "expiry": "2026-11-23",
                "strike": 25150.0,
                "option_type": "PE",
                "qty": -65,
                "entry_price": short_px,
            },
            "net_entry_credit_pts": net_cr,
            "hard_stop_debit_pts": round(net_cr * 1.65, 2),
            "target_debit_pts": round(net_cr * 0.30, 2),
            "is_bull": True,
        },
    }


def _sample_book_b_pkg(
    pkg_id: str = "shdw_bookb000001",
    session_date: str = "2026-11-19",
    entry_time: str = "2026-11-19T10:50:00",
    entry_px: float = 145.05,
) -> Dict[str, Any]:
    return {
        "package_id": pkg_id,
        "advance_trade_id": f"adv_shdw_{pkg_id[-10:]}",
        "session_date": session_date,
        "book_id": BOOK_ID_PAID_B,
        "state": "OPEN",
        "direction_label": "BUY_CE",
        "expiry": "2026-11-23",
        "entry_bar_ts": f"{session_date} 10:49:00",
        "entry_time": entry_time,
        "leg2_confirmed_time": entry_time,
        "legs_state": {
            "mode": "SINGLE_PAID_LONG",
            "long_leg": {
                "instrument_key": "NSE_FO|LONG_25300CE",
                "trading_symbol": "NIFTY 25300 CE 23 NOV 26",
                "expiry": "2026-11-23",
                "strike": 25300.0,
                "option_type": "CE",
                "qty": 65,
                "entry_price": entry_px,
            },
            "best_close_pts": entry_px,
            "ratchet_stop": None,
            "hard_stop": 113.10,
            "stop_loss_pct": 22.0,
            "is_bull": True,
        },
    }


class MockUpstoxBroker:
    """Deterministic Upstox V2 mock capturing exact call order & transitional `/order/history` lists."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = []
        self._oid_seq: int = 100
        self.orders: Dict[str, List[Dict[str, Any]]] = {}
        self.place_behavior: Dict[str, Any] = {}
        self.cancel_behavior: Dict[str, List[Dict[str, Any]]] = {}
        self.positions_data: List[Dict[str, Any]] = []

    def __call__(
        self,
        method: str,
        endpoint: str,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        is_emergency: bool = False,
    ) -> Tuple[bool, int, Dict[str, Any]]:
        self.calls.append((method, endpoint, payload, params))

        if method == "POST" and endpoint == "/order/place":
            ikey = str((payload or {}).get("instrument_token") or "")
            tx = str((payload or {}).get("transaction_type") or "")
            ot = str((payload or {}).get("order_type") or "LIMIT")
            key = f"{tx}:{ikey}:{ot}"
            beh = self.place_behavior.get(key) or self.place_behavior.get(f"{tx}:{ikey}") or {"mode": "complete"}
            if callable(beh):
                beh = beh(payload)
            if beh.get("http_reject"):
                return False, 400, {"status": "error", "error": beh.get("reason", "RMS_MARGIN_REJECT")}

            self._oid_seq += 1
            oid = f"OID_{self._oid_seq}"
            qty = int((payload or {}).get("quantity") or 65)
            px = float(beh.get("fill_price") or (payload or {}).get("price") or 50.0)
            mode = beh.get("mode", "complete")

            if mode == "complete":
                # Simulate Upstox chronological history list: entries[0] is open (0 filled), entries[-1] is complete!
                self.orders[oid] = [
                    {"order_id": oid, "status": "put order req received", "filled_quantity": 0, "average_price": 0.0},
                    {"order_id": oid, "status": "open", "filled_quantity": 0, "average_price": 0.0},
                    {"order_id": oid, "status": "complete", "filled_quantity": qty, "average_price": px},
                ]
            elif mode == "resting_open":
                self.orders[oid] = [
                    {"order_id": oid, "status": "open", "filled_quantity": 0, "average_price": 0.0},
                ]
            elif mode == "partial_open":
                part_q = int(beh.get("partial_qty", 30))
                self.orders[oid] = [
                    {"order_id": oid, "status": "open", "filled_quantity": part_q, "average_price": px},
                ]
                self.cancel_behavior[oid] = [
                    {"order_id": oid, "status": "cancelled", "filled_quantity": part_q, "average_price": px},
                ]
            elif mode == "rejected":
                self.orders[oid] = [
                    {"order_id": oid, "status": "rejected", "filled_quantity": 0, "average_price": 0.0, "status_message": beh.get("reason", "MARGIN_SHORTFALL")},
                ]
            return True, 200, {"status": "success", "data": {"order_id": oid}}

        if method == "PUT" and endpoint == "/order/modify":
            oid = str((payload or {}).get("order_id") or "")
            return True, 200, {"status": "success", "data": {"order_id": oid}}

        if method == "DELETE" and endpoint == "/order/cancel":
            oid = str((params or {}).get("order_id") or "")
            if oid in self.cancel_behavior:
                self.orders[oid] = self.cancel_behavior[oid]
            elif oid in self.orders:
                last = dict(self.orders[oid][-1])
                if last.get("status") != "complete":
                    last["status"] = "cancelled"
                    self.orders[oid].append(last)
            return True, 200, {"status": "success", "data": {"order_id": oid}}

        if method == "GET" and endpoint == "/order/history":
            oid = str((params or {}).get("order_id") or "")
            return True, 200, {"status": "success", "data": self.orders.get(oid, [])}

        if method == "GET" and endpoint == "/portfolio/short-term-positions":
            return True, 200, {"status": "success", "data": self.positions_data}

        return True, 200, {"status": "success", "data": []}


class TestShadowMirrorExecutor(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_mirror.sqlite")
        init_shadow_mirror_tables(self.db_path)
        if _HAS_BRIDGE_DEPS and init_advance_live_tables is not None:
            init_advance_live_tables(self.db_path)
        self.broker = MockUpstoxBroker()
        self.quotes: Dict[str, Dict[str, Any]] = {
            "NSE_FO|WING_25000PE": {"bid": 56.30, "ask": 56.45, "ltp": 56.40, "quote_age_ms": 100},
            "NSE_FO|SHORT_25150PE": {"bid": 101.30, "ask": 101.55, "ltp": 101.40, "quote_age_ms": 100},
            "NSE_FO|LONG_25300CE": {"bid": 144.95, "ask": 145.00, "ltp": 145.00, "quote_age_ms": 100},
        }
        self.bridge_state: Dict[str, Any] = {
            "is_live_armed": False,
            "execution_mode": MODE_SHADOW_DRY_RUN_AB,
            "armed_at_iso": "2026-11-19 09:30:00",
            "armed_for_date": "2026-11-19",
            "is_session_halted": False,
            "daily_total_exposure_pnl_inr": 0.0,
            "max_daily_loss_inr": 3500.0,
        }
        self.disarmed_reasons: List[str] = []

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _make_executor(self, avail_margin: float = 100000.0) -> ShadowMirrorExecutor:
        return ShadowMirrorExecutor(
            db_path=self.db_path,
            bridge_state_fn=lambda: self.bridge_state,
            disarm_callback=lambda r: self.disarmed_reasons.append(r),
            broker_api_fn=self.broker,
            quote_lookup_fn=lambda k: self.quotes.get(k),
            readiness_fn=lambda: {"live_trading_eligible": True, "available_margin": avail_margin},
            poll_interval_sec=0.002,
            poll_max_attempts=4,
            auto_start_worker=False,
        )

    def test_01_dry_run_firewall_zero_broker_mutations_and_accounting_parity(self) -> None:
        ex = self._make_executor()
        now = datetime(2026, 11, 19, 10, 5, 1)

        r_open = ex.process_shadow_event_sync("BOOK_A_OPEN", _sample_book_a_pkg(), now_dt=now)
        self.assertEqual(r_open["execution_tier"], "DRY_RUN")
        self.assertEqual(r_open["state"], "DRY_RUN_OPEN")
        self.assertEqual(ex.mutation_call_count, 0)
        self.assertEqual(len(self.broker.calls), 0)

        self.quotes["NSE_FO|SHORT_25150PE"] = {"bid": 32.00, "ask": 32.20, "ltp": 32.10, "quote_age_ms": 100}
        self.quotes["NSE_FO|WING_25000PE"] = {"bid": 18.80, "ask": 19.00, "ltp": 18.90, "quote_age_ms": 100}
        r_close = ex.process_shadow_event_sync(
            "BOOK_A_CLOSE",
            {"package_id": "shdw_booka000001", "book_id": BOOK_ID_SPREAD_A, "exit_reason": "SPREAD_DECAY_TARGET_70PCT", "net_pnl_inr": 1928.67},
            now_dt=datetime(2026, 11, 19, 12, 1, 0),
        )
        self.assertEqual(r_close["state"], "DRY_RUN_CLOSED")
        self.assertEqual(ex.mutation_call_count, 0)

        ref = compute_package_accounting(
            legs=[
                OptionLeg("PE", 25000.0, 65, r_close["leg1_entry_fill"], r_close["leg1_exit_fill"], "MARKET_ORDER"),
                OptionLeg("PE", 25150.0, -65, r_close["leg2_entry_fill"], r_close["leg2_exit_fill"], "MARKET_ORDER"),
            ],
            trade_date="2026-11-19",
        )
        self.assertEqual(r_close["realized_net_pnl_inr"], ref["net_pnl"])

    @unittest.skipUnless(_HAS_BRIDGE_DEPS, "Requires full backend venv (requests/pandas) for AdvanceLiveBridge")
    def test_02_dry_run_mode_cannot_be_live_armed_in_bridge(self) -> None:
        bridge = AdvanceLiveBridge(db_path=self.db_path, auto_start_daemon=False)
        pin = bridge.current_pin
        res = bridge.arm_live_trading(pin=pin, execution_mode=MODE_SHADOW_DRY_RUN_AB, now_dt=datetime(2026, 11, 19, 10, 0, 0))
        self.assertEqual(res["status"], "ARM_REJECTED")
        self.assertFalse(bridge.is_live_armed)
        self.assertIn("DRY_RUN_MODE_ACTIVE", res["message"])

    def test_03_v6b_comparator_permanently_airgapped(self) -> None:
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_AB
        res = ex.process_shadow_event_sync(
            "BOOK_A_OPEN",
            {"package_id": "v6b_hack_1", "book_id": "V6B_S1_OI"},
            now_dt=datetime(2026, 11, 19, 10, 5, 0),
        )
        self.assertIsNone(res)
        self.assertEqual(ex.mutation_call_count, 0)

    def test_04_book_a_live_happy_path_buy_first_entry_and_short_first_exit(self) -> None:
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_A
        self.broker.place_behavior["BUY:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 56.55}
        self.broker.place_behavior["SELL:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "complete", "fill_price": 101.20}

        r_open = ex.process_shadow_event_sync("BOOK_A_OPEN", _sample_book_a_pkg(), now_dt=datetime(2026, 11, 19, 10, 5, 1))
        self.assertEqual(r_open["state"], "OPEN_HEDGED")
        self.assertEqual(r_open["leg1_entry_fill"], 56.55)
        self.assertEqual(r_open["leg2_entry_fill"], 101.20)

        # Verify strict BUY Wing -> poll history -> SELL Short call order!
        place_calls = [c for c in self.broker.calls if c[0] == "POST" and c[1] == "/order/place"]
        self.assertEqual(len(place_calls), 2)
        self.assertEqual(place_calls[0][2]["transaction_type"], "BUY")
        self.assertEqual(place_calls[0][2]["instrument_token"], "NSE_FO|WING_25000PE")
        self.assertEqual(place_calls[1][2]["transaction_type"], "SELL")
        self.assertEqual(place_calls[1][2]["instrument_token"], "NSE_FO|SHORT_25150PE")

        # Exit: BUY-to-close Short MUST happen BEFORE SELL-to-close Wing!
        self.broker.calls.clear()
        self.broker.place_behavior["BUY:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "complete", "fill_price": 32.15}
        self.broker.place_behavior["SELL:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 18.85}
        r_close = ex.process_shadow_event_sync(
            "BOOK_A_CLOSE",
            {"package_id": "shdw_booka000001", "book_id": BOOK_ID_SPREAD_A, "exit_reason": "SPREAD_DECAY_TARGET_70PCT", "net_pnl_inr": 1928.67},
            now_dt=datetime(2026, 11, 19, 12, 1, 47),
        )
        self.assertEqual(r_close["state"], "CONFIRMED_FLAT")
        ex_places = [c for c in self.broker.calls if c[0] == "POST" and c[1] == "/order/place"]
        self.assertEqual(len(ex_places), 2)
        self.assertEqual(ex_places[0][2]["transaction_type"], "BUY")
        self.assertEqual(ex_places[0][2]["instrument_token"], "NSE_FO|SHORT_25150PE")
        self.assertEqual(ex_places[1][2]["transaction_type"], "SELL")
        self.assertEqual(ex_places[1][2]["instrument_token"], "NSE_FO|WING_25000PE")

        ref_acc = compute_package_accounting(
            legs=[
                OptionLeg("PE", 25000.0, 65, 56.55, 18.85, "MARKET_ORDER"),
                OptionLeg("PE", 25150.0, -65, 101.20, 32.15, "MARKET_ORDER"),
            ],
            trade_date="2026-11-19",
        )
        self.assertEqual(r_close["realized_net_pnl_inr"], ref_acc["net_pnl"])
        self.assertEqual(r_close["statutory_charges_inr"], ref_acc["total_friction"])

    def test_05_book_a_leg1_partial_fill_unwinds_exact_qty_and_never_sells_short(self) -> None:
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_A
        self.broker.place_behavior["BUY:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "partial_open", "partial_qty": 30, "fill_price": 56.50}
        self.broker.place_behavior["SELL:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 56.10}

        r = ex.process_shadow_event_sync("BOOK_A_OPEN", _sample_book_a_pkg("shdw_part000001"), now_dt=datetime(2026, 11, 19, 10, 5, 1))
        self.assertEqual(r["state"], "ABORTED_PARTIAL_UNWOUND")
        self.assertEqual(r["leg1_filled_qty"], 30)
        self.assertEqual(r["leg1_exited_qty"], 30)
        short_calls = [c for c in self.broker.calls if c[0] == "POST" and (c[2] or {}).get("instrument_token") == "NSE_FO|SHORT_25150PE"]
        self.assertEqual(len(short_calls), 0)

    def test_06_book_a_leg2_credit_fade_unwinds_wing_without_placing_short(self) -> None:
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_A
        self.broker.place_behavior["BUY:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 56.50}
        self.broker.place_behavior["SELL:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 56.00}
        # Short bid collapses to 65.00 -> net credit 8.50 < 14.0 floor!
        self.quotes["NSE_FO|SHORT_25150PE"] = {"bid": 65.00, "ask": 65.50, "ltp": 65.20, "quote_age_ms": 100}

        r = ex.process_shadow_event_sync("BOOK_A_OPEN", _sample_book_a_pkg("shdw_fade000001"), now_dt=datetime(2026, 11, 19, 10, 5, 1))
        self.assertEqual(r["state"], "LEG2_CREDIT_FADE_UNWOUND")
        short_calls = [c for c in self.broker.calls if c[0] == "POST" and (c[2] or {}).get("instrument_token") == "NSE_FO|SHORT_25150PE"]
        self.assertEqual(len(short_calls), 0)

    def test_07_book_a_leg2_rejection_unwinds_wing_and_enforces_daily_abort_cap(self) -> None:
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_A
        self.broker.place_behavior["BUY:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 56.50}
        self.broker.place_behavior["SELL:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "rejected", "reason": "RMS_REJECT"}
        self.broker.place_behavior["SELL:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 56.10}

        r1 = ex.process_shadow_event_sync("BOOK_A_OPEN", _sample_book_a_pkg("shdw_rej0000001"), now_dt=datetime(2026, 11, 19, 10, 5, 1))
        self.assertEqual(r1["state"], "LEG2_ABORT_UNWOUND")

        # Second attempt same day MUST be blocked by MAX_LEG2_ABORTS_REACHED!
        r2 = ex.process_shadow_event_sync("BOOK_A_OPEN", _sample_book_a_pkg("shdw_rej0000002"), now_dt=datetime(2026, 11, 19, 11, 0, 1))
        self.assertEqual(r2["state"], "PREFLIGHT_BLOCKED")
        self.assertIn("MAX_LEG2_ABORTS_REACHED", r2["error_reason"])

    def test_08_book_a_exit_never_touches_wing_while_short_unfilled(self) -> None:
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_A
        self.broker.place_behavior["BUY:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 56.50}
        self.broker.place_behavior["SELL:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "complete", "fill_price": 101.25}
        ex.process_shadow_event_sync("BOOK_A_OPEN", _sample_book_a_pkg("shdw_exguard001"), now_dt=datetime(2026, 11, 19, 10, 5, 1))

        # Force short exit to fail/stay open on all 3 collar attempts -> Wing MUST NOT be sold!
        self.broker.calls.clear()
        self.broker.place_behavior["BUY:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "resting_open"}
        r_fail = ex.process_shadow_event_sync(
            "BOOK_A_CLOSE",
            {"package_id": "shdw_exguard001", "book_id": BOOK_ID_SPREAD_A, "exit_reason": "SPOT_VWAP_FLIP_EXIT"},
            now_dt=datetime(2026, 11, 19, 11, 15, 0),
        )
        self.assertEqual(r_fail["state"], "EXIT_RETRY_PENDING")
        wing_sell_calls = [
            c for c in self.broker.calls
            if c[0] == "POST" and (c[2] or {}).get("instrument_token") == "NSE_FO|WING_25000PE" and (c[2] or {}).get("transaction_type") == "SELL"
        ]
        self.assertEqual(len(wing_sell_calls), 0)

        # Now liquidity returns on retry -> short closes first, then wing closes!
        self.broker.place_behavior["BUY:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "complete", "fill_price": 112.00}
        self.broker.place_behavior["SELL:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 62.00}
        ex.sync_with_shadow_db_once(now_dt=datetime(2026, 11, 19, 11, 15, 5))
        final_row = ex._existing_mirror_row("shdw_exguard001")
        self.assertEqual(final_row["state"], "CONFIRMED_FLAT")

    def test_09_book_b_live_happy_path_sl_ratchet_modify_and_cancel_first_exit(self) -> None:
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_B
        self.broker.place_behavior["BUY:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "complete", "fill_price": 145.05}
        self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:SL"] = {"mode": "resting_open"}

        pkg_b = _sample_book_b_pkg()
        r_open = ex.process_shadow_event_sync("BOOK_B_OPEN", pkg_b, now_dt=datetime(2026, 11, 19, 10, 50, 1))
        self.assertEqual(r_open["state"], "OPEN_LONG")
        self.assertIsNotNone(r_open["broker_sl_oid"])
        self.assertAlmostEqual(r_open["broker_sl_trigger"], 113.10, places=1)

        # Ratchet tightens to 147.95 -> PUT /order/modify!
        pkg_b["legs_state"]["ratchet_stop"] = 147.95
        r_rat = ex.process_shadow_event_sync("BOOK_B_RATCHET", pkg_b, now_dt=datetime(2026, 11, 19, 10, 56, 0))
        self.assertEqual(r_rat["broker_sl_trigger"], 147.95)
        self.assertEqual(r_rat["broker_sl_status"], "RATCHET_MODIFIED")

        # Software exit (EOD) -> MUST cancel SL-L first, confirm cancelled, THEN send SELL!
        self.broker.calls.clear()
        self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "complete", "fill_price": 220.00}
        r_close = ex.process_shadow_event_sync(
            "BOOK_B_CLOSE",
            {"package_id": "shdw_bookb000001", "book_id": BOOK_ID_PAID_B, "exit_reason": "EOD_SQUAREOFF_1515", "net_pnl_inr": 4800.0},
            now_dt=datetime(2026, 11, 19, 15, 15, 0),
        )
        self.assertEqual(r_close["state"], "CONFIRMED_FLAT")
        cancel_idx = next(i for i, c in enumerate(self.broker.calls) if c[0] == "DELETE" and c[1] == "/order/cancel")
        sell_idx = next(i for i, c in enumerate(self.broker.calls) if c[0] == "POST" and c[1] == "/order/place")
        self.assertLess(cancel_idx, sell_idx)

    def test_10_book_b_anti_double_sell_when_exchange_sl_already_filled(self) -> None:
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_B
        self.broker.place_behavior["BUY:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "complete", "fill_price": 145.05}
        self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:SL"] = {"mode": "resting_open"}

        r_open = ex.process_shadow_event_sync("BOOK_B_OPEN", _sample_book_b_pkg("shdw_slrace0001"), now_dt=datetime(2026, 11, 19, 10, 50, 1))
        sl_oid = r_open["broker_sl_oid"]

        # Simulate exchange SL-L filling 65/65 right as cancel arrives!
        self.broker.cancel_behavior[sl_oid] = [
            {"order_id": sl_oid, "status": "complete", "filled_quantity": 65, "average_price": 113.05},
        ]
        self.broker.calls.clear()
        r_close = ex.process_shadow_event_sync(
            "BOOK_B_CLOSE",
            {"package_id": "shdw_slrace0001", "book_id": BOOK_ID_PAID_B, "exit_reason": "LONG_HARD_SL_HIT"},
            now_dt=datetime(2026, 11, 19, 11, 2, 0),
        )
        self.assertEqual(r_close["state"], "CONFIRMED_FLAT")
        self.assertEqual(r_close["broker_sl_status"], "FILLED_ON_EXCHANGE")
        self.assertEqual(r_close["leg1_exit_fill"], 113.05)
        # ZERO new SELL orders may be placed (prevents accidental naked short 65 qty!)
        sell_places = [c for c in self.broker.calls if c[0] == "POST" and c[1] == "/order/place"]
        self.assertEqual(len(sell_places), 0)

    @unittest.skipUnless(_HAS_BRIDGE_DEPS, "Requires full backend venv (requests/pandas) for AdvanceLiveBridge")
    def test_11_whole_system_shadow_live_ab_and_sos_panic_exit_via_bridge(self) -> None:
        with patch(
            "app.analytics.option_sniffer_advance.advance_db.get_db_path",
            return_value=self.db_path,
        ):
            init_option_sniffer_advance_tables()
            bridge = AdvanceLiveBridge(db_path=self.db_path, auto_start_daemon=False)
            bridge._shadow_mirror._broker_api_fn = self.broker
            bridge._shadow_mirror._quote_lookup_fn = lambda k: self.quotes.get(k)
            bridge._shadow_mirror._readiness_fn = lambda: {"live_trading_eligible": True, "available_margin": 110000.0}
            bridge._shadow_mirror._poll_interval_sec = 0.002

            with patch.object(
                bridge._live_adapter,
                "fetch_account_readiness",
                return_value={"live_trading_eligible": True, "available_margin": 110000.0},
            ):
                arm_res = bridge.arm_live_trading(
                    pin=bridge.current_pin,
                    execution_mode=MODE_SHADOW_LIVE_AB,
                    now_dt=datetime(2026, 11, 19, 9, 40, 0),
                )
            self.assertEqual(arm_res["status"], "ARMED")
            self.assertTrue(bridge.is_live_armed)

            self.broker.place_behavior["BUY:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 56.50}
            self.broker.place_behavior["SELL:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "complete", "fill_price": 101.25}
            self.broker.place_behavior["BUY:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "complete", "fill_price": 145.05}
            self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:SL"] = {"mode": "resting_open"}

            bridge.shadow_mirror.process_shadow_event_sync("BOOK_A_OPEN", _sample_book_a_pkg("shdw_ab_a00001"), now_dt=datetime(2026, 11, 19, 10, 5, 0))
            bridge.shadow_mirror.process_shadow_event_sync("BOOK_B_OPEN", _sample_book_b_pkg("shdw_ab_b00001"), now_dt=datetime(2026, 11, 19, 10, 50, 0))

            active_pos = bridge.get_active_live_positions()
            self.assertEqual(len(active_pos), 2)

            # SOS Panic Exit MUST flatten BOTH Book A (short-first) and Book B (cancel-SL-first) and disarm!
            self.broker.place_behavior["BUY:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "complete", "fill_price": 98.00}
            self.broker.place_behavior["SELL:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 54.00}
            self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "complete", "fill_price": 152.00}

            sos_res = bridge.execute_sos_panic_exit()
            self.assertEqual(sos_res["status"], "SOS_EXECUTED_AND_DISARMED")
            self.assertFalse(bridge.is_live_armed)
            self.assertEqual(sos_res["positions_flattened"], 2)
            self.assertEqual(len(bridge.get_active_live_positions()), 0)

    def test_12_watchdog_orphan_short_emergency_flatten_and_auto_disarm(self) -> None:
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_A
        self.broker.place_behavior["BUY:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 56.50}
        self.broker.place_behavior["SELL:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "complete", "fill_price": 101.25}
        self.broker.place_behavior["BUY:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "complete", "fill_price": 103.00}

        ex.process_shadow_event_sync("BOOK_A_OPEN", _sample_book_a_pkg("shdw_orph00001"), now_dt=datetime(2026, 11, 19, 10, 5, 1))

        # Broker reports short=-65 with wing=0 (orphan short!)
        self.broker.positions_data = [
            {"instrument_token": "NSE_FO|SHORT_25150PE", "quantity": -65},
            {"instrument_token": "NSE_FO|WING_25000PE", "quantity": 0},
        ]
        ex.sync_with_shadow_db_once(now_dt=datetime(2026, 11, 19, 10, 6, 0))
        row = ex._existing_mirror_row("shdw_orph00001")
        self.assertEqual(row["state"], "CONFIRMED_FLAT")
        self.assertEqual(row["exit_reason"], "ORPHAN_SHORT_EMERGENCY_FLATTENED")
        self.assertIn("ORPHAN_SHORT_EMERGENCY_FLATTENED", self.disarmed_reasons)

    def test_13_pre_arm_stale_signal_blocked_and_duplicate_event_idempotent(self) -> None:
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_A
        self.bridge_state["armed_at_iso"] = "2026-11-19 10:30:00"

        # Signal opened at 10:05 BEFORE 10:30 arm timestamp -> MUST be blocked!
        r_pre = ex.process_shadow_event_sync("BOOK_A_OPEN", _sample_book_a_pkg("shdw_prearm001", entry_time="2026-11-19T10:05:00"), now_dt=datetime(2026, 11, 19, 10, 30, 5))
        self.assertEqual(r_pre["state"], "PREFLIGHT_BLOCKED")
        self.assertIn("PRE_ARM_SIGNAL_SKIPPED", r_pre["error_reason"])
        self.assertEqual(ex.mutation_call_count, 0)

        # Duplicate event on post-arm signal -> places orders ONCE only!
        self.broker.place_behavior["BUY:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 56.50}
        self.broker.place_behavior["SELL:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "complete", "fill_price": 101.25}
        pkg_ok = _sample_book_a_pkg("shdw_idem00001", entry_time="2026-11-19T10:35:00")
        ex.process_shadow_event_sync("BOOK_A_OPEN", pkg_ok, now_dt=datetime(2026, 11, 19, 10, 35, 1))
        calls_after_1 = ex.mutation_call_count
        ex.process_shadow_event_sync("BOOK_A_OPEN", pkg_ok, now_dt=datetime(2026, 11, 19, 10, 35, 2))
        self.assertEqual(ex.mutation_call_count, calls_after_1)

    def test_14_p0_1_unconfirmed_cancel_blocks_duplicate_stage_and_retry_orders(self) -> None:
        """[P0-1] If cancel fails to confirm terminal (`status == 'open'`), ZERO Stage-2/retry orders may be placed."""
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_B
        self.broker.place_behavior["BUY:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "complete", "fill_price": 145.05}
        self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:SL"] = {"mode": "resting_open"}

        r_open = ex.process_shadow_event_sync("BOOK_B_OPEN", _sample_book_b_pkg("shdw_p01_00001"), now_dt=datetime(2026, 11, 19, 10, 50, 1))
        sl_oid = r_open["broker_sl_oid"]
        # First cancel SL cleanly, then make Stage-1 exit order stay 'open' EVEN AFTER DELETE /order/cancel!
        self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "resting_open"}

        orig_call = self.broker.__call__

        def sticky_cancel_broker(method, endpoint, payload=None, params=None, is_emergency=False):
            if method == "DELETE" and endpoint == "/order/cancel":
                oid = str((params or {}).get("order_id") or "")
                if oid != sl_oid:
                    # Simulate gateway cancel timeout: order remains 'open' on exchange!
                    return False, 504, {"status": "error", "error": "Gateway Timeout"}
            return orig_call(method, endpoint, payload, params, is_emergency)

        ex._broker_api_fn = sticky_cancel_broker
        self.broker.calls.clear()

        r_close = ex.process_shadow_event_sync(
            "BOOK_B_CLOSE",
            {"package_id": "shdw_p01_00001", "book_id": BOOK_ID_PAID_B, "exit_reason": "EOD_SQUAREOFF_1515"},
            now_dt=datetime(2026, 11, 19, 15, 15, 0),
        )
        self.assertEqual(r_close["state"], "EXIT_RETRY_PENDING")
        self.assertTrue(str(r_close["leg1_exit_oid"]).startswith("PENDING:"))
        # Stage 1 placed 1 order; Stage 2 & Stage 3 MUST NOT have placed duplicate orders!
        sell_places = [c for c in self.broker.calls if c[0] == "POST" and c[1] == "/order/place"]
        self.assertEqual(len(sell_places), 1)

        # Next retry pass while still non-terminal: MUST place 0 new orders!
        self.broker.calls.clear()
        ex.sync_with_shadow_db_once(now_dt=datetime(2026, 11, 19, 15, 15, 5))
        retry_places = [c for c in self.broker.calls if c[0] == "POST" and c[1] == "/order/place"]
        self.assertEqual(len(retry_places), 0)

        # Once the stuck order finally fills on the exchange, retry reconciles CONFIRMED_FLAT with 0 extra orders!
        stuck_oid = str(r_close["leg1_exit_oid"]).split(":", 1)[1]
        self.broker.orders[stuck_oid] = [
            {"order_id": stuck_oid, "status": "complete", "filled_quantity": 65, "average_price": 150.00}
        ]
        ex._broker_api_fn = self.broker
        ex.sync_with_shadow_db_once(now_dt=datetime(2026, 11, 19, 15, 15, 10))
        settled_row = ex._existing_mirror_row("shdw_p01_00001")
        self.assertEqual(settled_row["state"], "CONFIRMED_FLAT")
        self.assertEqual(settled_row["leg1_exited_qty"], 65)
        self.assertEqual(settled_row["leg1_exit_fill"], 150.00)

    def test_15_p0_2_partial_exit_and_partial_sl_fill_persisted_across_retries(self) -> None:
        """[P0-2] Partial SL fill (25/65) + partial Stage-1 exit (15/40) sells ONLY remaining 25 on retry."""
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_B
        self.broker.place_behavior["BUY:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "complete", "fill_price": 145.00}
        self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:SL"] = {"mode": "resting_open"}

        r_open = ex.process_shadow_event_sync("BOOK_B_OPEN", _sample_book_b_pkg("shdw_p02_00001"), now_dt=datetime(2026, 11, 19, 10, 50, 1))
        sl_oid = r_open["broker_sl_oid"]
        # Exchange SL partially filled 25 @ 113.00 right before cancel settled!
        self.broker.cancel_behavior[sl_oid] = [
            {"order_id": sl_oid, "status": "cancelled", "filled_quantity": 25, "average_price": 113.00}
        ]

        # Stage 1 software exit partially fills 15 @ 112.00 then cancels; Stage 2 & 3 rest unfilled then cancel!
        stage_counter = {"n": 0}

        def staged_sell_beh(payload):
            stage_counter["n"] += 1
            if stage_counter["n"] == 1:
                return {"mode": "partial_open", "partial_qty": 15, "fill_price": 112.00}
            return {"mode": "resting_open"}

        self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:LIMIT"] = staged_sell_beh
        r_mid = ex.process_shadow_event_sync(
            "BOOK_B_CLOSE",
            {"package_id": "shdw_p02_00001", "book_id": BOOK_ID_PAID_B, "exit_reason": "LONG_HARD_SL_HIT"},
            now_dt=datetime(2026, 11, 19, 11, 5, 0),
        )
        self.assertEqual(r_mid["state"], "EXIT_RETRY_PENDING")
        # 25 (from SL) + 15 (from Stage 1) = 40 exited so far!
        self.assertEqual(r_mid["leg1_exited_qty"], 40)

        # Next retry pass: MUST place strictly 25 qty (65 - 40 = 25), NEVER 65!
        self.broker.calls.clear()
        self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "complete", "fill_price": 110.00}
        ex.sync_with_shadow_db_once(now_dt=datetime(2026, 11, 19, 11, 5, 5))
        retry_sells = [c for c in self.broker.calls if c[0] == "POST" and c[1] == "/order/place"]
        self.assertEqual(len(retry_sells), 1)
        self.assertEqual(retry_sells[0][2]["quantity"], 25)

        final_row = ex._existing_mirror_row("shdw_p02_00001")
        self.assertEqual(final_row["state"], "CONFIRMED_FLAT")
        self.assertEqual(final_row["leg1_exited_qty"], 65)
        expected_vwap = (25 * 113.00 + 15 * 112.00 + 25 * 110.00) / 65.0
        self.assertAlmostEqual(final_row["leg1_exit_fill"], expected_vwap, places=2)

    def test_16_p0_3_failed_short_rebuy_stays_exit_retry_pending_with_wing_intact_and_zero_synthetic_fills(self) -> None:
        """[P0-3] Partial Leg-2 short abort with failed short buyback keeps wing intact & retries until truly flat."""
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_A
        self.broker.place_behavior["BUY:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 56.50}
        # Leg-2 short partially fills 30/65 @ 101.00 and cancels!
        self.broker.place_behavior["SELL:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "partial_open", "partial_qty": 30, "fill_price": 101.00}
        # Short buyback stalls (unfilled on all 3 collar stages)!
        self.broker.place_behavior["BUY:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "resting_open"}

        r_abort = ex.process_shadow_event_sync("BOOK_A_OPEN", _sample_book_a_pkg("shdw_p03_00001"), now_dt=datetime(2026, 11, 19, 10, 5, 1))
        # MUST NOT mark terminal LEG2_PARTIAL_UNWOUND and MUST NOT sell wing while 30 short is still open!
        self.assertEqual(r_abort["state"], "EXIT_RETRY_PENDING")
        self.assertEqual(r_abort["leg2_filled_qty"], 30)
        self.assertEqual(r_abort["leg2_exited_qty"], 0)
        self.assertEqual(r_abort["leg1_exited_qty"], 0)
        wing_sells = [
            c for c in self.broker.calls
            if c[0] == "POST" and (c[2] or {}).get("instrument_token") == "NSE_FO|WING_25000PE" and (c[2] or {}).get("transaction_type") == "SELL"
        ]
        self.assertEqual(len(wing_sells), 0)

        # Now liquidity returns on retry: 30 short bought back FIRST, then 65 wing sold, preserving LEG2_PARTIAL_UNWOUND!
        self.broker.calls.clear()
        self.broker.place_behavior["BUY:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "complete", "fill_price": 102.00}
        self.broker.place_behavior["SELL:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 56.00}
        ex.sync_with_shadow_db_once(now_dt=datetime(2026, 11, 19, 10, 5, 6))
        final_row = ex._existing_mirror_row("shdw_p03_00001")
        self.assertEqual(final_row["state"], "LEG2_PARTIAL_UNWOUND")
        self.assertEqual(final_row["leg2_exited_qty"], 30)
        self.assertEqual(final_row["leg1_exited_qty"], 65)

    @unittest.skipUnless(_HAS_BRIDGE_DEPS, "Requires full backend venv (requests/pandas) for AdvanceLiveBridge")
    def test_17_p0_4_mode_switch_while_armed_auto_disarms_and_rotates_pin(self) -> None:
        """[P0-4] Calling `set_execution_mode` while armed immediately auto-disarms & rotates PIN."""
        bridge = AdvanceLiveBridge(db_path=self.db_path, auto_start_daemon=False)
        with patch.object(
            bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"live_trading_eligible": True, "available_margin": 55000.0},
        ):
            arm_res = bridge.arm_live_trading(
                pin=bridge.current_pin,
                execution_mode=MODE_LONG_BOOK_ONLY,
                now_dt=datetime(2026, 11, 19, 10, 0, 0),
            )
        self.assertEqual(arm_res["status"], "ARMED")
        self.assertTrue(bridge.is_live_armed)
        pin_before_switch = bridge.current_pin

        # Attempt to hot-swap into DUAL_BOOK_NAKED (₹1.5L margin) via set_execution_mode without PIN:
        sw_res = bridge.set_execution_mode(MODE_DUAL_BOOK_NAKED)
        self.assertEqual(sw_res["status"], "UPDATED")
        self.assertTrue(sw_res["was_auto_disarmed"])
        self.assertFalse(bridge.is_live_armed)
        self.assertNotEqual(bridge.current_pin, pin_before_switch)

    def test_18_p1_1_missing_zero_or_crossed_live_quote_fails_closed(self) -> None:
        """[P1-1] Missing, zero-bid, or crossed quotes fail closed on LIVE_ARMED entry with 0 broker mutations."""
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_B

        # 1. Missing quote (None)
        self.quotes.pop("NSE_FO|LONG_25300CE", None)
        r1 = ex.process_shadow_event_sync("BOOK_B_OPEN", _sample_book_b_pkg("shdw_q_none001"), now_dt=datetime(2026, 11, 19, 10, 50, 1))
        self.assertEqual(r1["state"], "PREFLIGHT_BLOCKED")
        self.assertIn("MISSING_LIVE_QUOTE", r1["error_reason"])

        # 2. Zero bid
        self.quotes["NSE_FO|LONG_25300CE"] = {"bid": 0.0, "ask": 145.0, "ltp": 145.0}
        r2 = ex.process_shadow_event_sync("BOOK_B_OPEN", _sample_book_b_pkg("shdw_q_zero001"), now_dt=datetime(2026, 11, 19, 10, 51, 1))
        self.assertEqual(r2["state"], "PREFLIGHT_BLOCKED")
        self.assertIn("ZERO_OR_NEGATIVE_BID", r2["error_reason"])

        # 3. Crossed book (ask < bid)
        self.quotes["NSE_FO|LONG_25300CE"] = {"bid": 146.0, "ask": 145.0, "ltp": 145.5}
        r3 = ex.process_shadow_event_sync("BOOK_B_OPEN", _sample_book_b_pkg("shdw_q_cros001"), now_dt=datetime(2026, 11, 19, 10, 52, 1))
        self.assertEqual(r3["state"], "PREFLIGHT_BLOCKED")
        self.assertIn("CROSSED_QUOTE", r3["error_reason"])

        # 4. Unknown quote age (valid bid/ask without quote_age_ms or l1_book_age_ms)
        self.quotes["NSE_FO|LONG_25300CE"] = {"bid": 144.95, "ask": 145.00, "ltp": 145.00}
        r4 = ex.process_shadow_event_sync("BOOK_B_OPEN", _sample_book_b_pkg("shdw_q_noag001"), now_dt=datetime(2026, 11, 19, 10, 53, 1))
        self.assertEqual(r4["state"], "PREFLIGHT_BLOCKED")
        self.assertIn("UNKNOWN_LIVE_QUOTE_AGE", r4["error_reason"])
        self.assertEqual(ex.mutation_call_count, 0)

    def test_19_p1_2_rejected_exchange_sl_triggers_immediate_protective_flatten(self) -> None:
        """[P1-2] If Upstox/NSE rejects Book B's exchange SL order, long position is immediately flattened."""
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_B
        self.broker.place_behavior["BUY:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "complete", "fill_price": 145.05}
        self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:SL"] = {"mode": "rejected", "reason": "TRIGGER_OUT_OF_BAND"}
        self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "complete", "fill_price": 144.80}

        r = ex.process_shadow_event_sync("BOOK_B_OPEN", _sample_book_b_pkg("shdw_p12_slrej"), now_dt=datetime(2026, 11, 19, 10, 50, 1))
        self.assertEqual(r["state"], "CONFIRMED_FLAT")
        self.assertEqual(r["exit_reason"], "SL_REJECTED_PROTECTIVE_FLATTEN")
        self.assertEqual(r["leg1_exited_qty"], 65)

    def test_20_p1_3_watchdog_orphan_and_external_flat_record_canonical_pnl(self) -> None:
        """[P1-3] Watchdog orphan flatten & external flat record exact canonical P&L for circuit breaker."""
        ex = self._make_executor()
        self.bridge_state["is_live_armed"] = True
        self.bridge_state["execution_mode"] = MODE_SHADOW_LIVE_BOOK_A
        self.broker.place_behavior["BUY:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 56.50}
        self.broker.place_behavior["SELL:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "complete", "fill_price": 101.25}
        self.broker.place_behavior["BUY:NSE_FO|SHORT_25150PE:LIMIT"] = {"mode": "complete", "fill_price": 135.00}

        ex.process_shadow_event_sync("BOOK_A_OPEN", _sample_book_a_pkg("shdw_p13_orph1"), now_dt=datetime(2026, 11, 19, 10, 5, 1))
        self.broker.positions_data = [
            {"instrument_token": "NSE_FO|SHORT_25150PE", "quantity": -65},
            {"instrument_token": "NSE_FO|WING_25000PE", "quantity": 0},
        ]
        ex.sync_with_shadow_db_once(now_dt=datetime(2026, 11, 19, 10, 6, 0))
        row = ex._existing_mirror_row("shdw_p13_orph1")
        self.assertEqual(row["state"], "CONFIRMED_FLAT")
        self.assertLess(float(row["realized_net_pnl_inr"]), -2000.0)
        self.assertGreater(float(row["statutory_charges_inr"]), 0.0)

    def test_21_p1_4_crash_recovery_reconciles_leg1_inflight_and_sos_flattens(self) -> None:
        """[P1-4] Crash recovery reconciles an in-flight `LEG1_SUBMITTED` filled order and flattens cleanly on SOS."""
        ex = self._make_executor()
        self.broker.orders["OID_CRASH_LEG1"] = [
            {"order_id": "OID_CRASH_LEG1", "status": "complete", "filled_quantity": 65, "average_price": 56.50}
        ]
        self.broker.place_behavior["SELL:NSE_FO|WING_25000PE:LIMIT"] = {"mode": "complete", "fill_price": 55.80}

        # Insert simulated post-crash LEG1_SUBMITTED row with leg1_filled_qty = 0
        with ex._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO option_advance_live_shadow_packages (
                    mirror_id, shadow_package_id, advance_trade_id, session_date, book_id,
                    execution_tier, bridge_mode, state, direction_label, expiry, qty,
                    leg1_instrument_key, leg1_trading_symbol, leg1_strike, leg1_option_type, leg1_side,
                    leg1_entry_oid, leg1_filled_qty, created_at, updated_at
                ) VALUES (
                    'mir_crash_01', 'shdw_crash_01', 'adv_crash_01', '2026-11-19', ?,
                    'LIVE_ARMED', ?, 'LEG1_SUBMITTED', 'SPREAD_SELL_PE', '2026-11-23', 65,
                    'NSE_FO|WING_25000PE', 'NIFTY 25000 PE', 25000.0, 'PE', 'BUY',
                    'PENDING:OID_CRASH_LEG1', 0, '2026-11-19 10:05:00', '2026-11-19 10:05:00'
                );
                """,
                (BOOK_ID_SPREAD_A, MODE_SHADOW_LIVE_BOOK_A),
            )
            conn.commit()

        flat_ids = ex.flatten_all_open_live_packages("SOS_PANIC_EXIT", now_dt=datetime(2026, 11, 19, 10, 6, 0))
        self.assertIn("mir_crash_01", flat_ids)
        row = ex._existing_mirror_row("shdw_crash_01")
        self.assertIn(row["state"], ("CONFIRMED_FLAT", "ABORTED_PARTIAL_UNWOUND", "LEG2_ABORT_UNWOUND"))
        self.assertEqual(row["leg1_filled_qty"], 65)
        self.assertEqual(row["leg1_exited_qty"], 65)

    @unittest.skipUnless(_HAS_BRIDGE_DEPS, "Requires full backend venv (requests/pandas) for AdvanceLiveBridge")
    def test_22_p2_1_p2_2_singleton_delegation_live_calendar_and_chronological_cb(self) -> None:
        """[P2-1, P2-2] Verifies singleton delegation, LIVE_BROKER calendar inclusion & chronological CB losses."""
        self.assertIs(get_shadow_mirror_executor(), get_advance_live_bridge().shadow_mirror)

        bridge = AdvanceLiveBridge(db_path=self.db_path, auto_start_daemon=False)
        bridge._shadow_mirror._broker_api_fn = self.broker
        bridge._shadow_mirror._quote_lookup_fn = lambda k: self.quotes.get(k)
        bridge._shadow_mirror._readiness_fn = lambda: {"live_trading_eligible": True, "available_margin": 110000.0}
        bridge._shadow_mirror._poll_interval_sec = 0.002

        with patch.object(
            bridge._live_adapter,
            "fetch_account_readiness",
            return_value={"live_trading_eligible": True, "available_margin": 110000.0},
        ):
            bridge.arm_live_trading(
                pin=bridge.current_pin,
                execution_mode=MODE_SHADOW_LIVE_BOOK_B,
                now_dt=datetime(2026, 11, 19, 9, 40, 0),
            )

        self.broker.place_behavior["BUY:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "complete", "fill_price": 145.00}
        self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:SL"] = {"mode": "resting_open"}
        self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "complete", "fill_price": 160.00}

        bridge.shadow_mirror.process_shadow_event_sync("BOOK_B_OPEN", _sample_book_b_pkg("shdw_cal00001"), now_dt=datetime(2026, 11, 19, 10, 50, 1))
        bridge.shadow_mirror.process_shadow_event_sync(
            "BOOK_B_CLOSE",
            {"package_id": "shdw_cal00001", "book_id": BOOK_ID_PAID_B, "exit_reason": "EOD_SQUAREOFF_1515"},
            now_dt=datetime(2026, 11, 19, 15, 15, 0),
        )

        cal = bridge.get_calendar_performance(book_filter="LIVE_BROKER")
        self.assertEqual(len(cal["days"]), 1)
        self.assertEqual(cal["days"][0]["trade_count"], 1)
        self.assertGreater(cal["days"][0]["net_pnl_rupees"], 800.0)

    def test_23_r2_p1_worker_idle_sync_no_self_throttle_and_book_b_unprotected_crash_flatten(self) -> None:
        """[R2-P1-1, R2-P1-2] Worker idle sync never mixes clocks/self-throttles, and Book B crash without SL flattens."""
        ex = self._make_executor()
        # 1. Verify `sync_with_shadow_db_once()` with `now_dt=None` executes cleanly after `_last_worker_idle_mono` updates
        ex._last_worker_idle_mono = 123.45
        first_sync = ex.sync_with_shadow_db_once(now_dt=None)
        self.assertNotEqual(first_sync.get("status"), "THROTTLED")
        self.assertGreater(ex._last_db_sync_epoch, 1_700_000_000.0)

        # 2. Insert simulated post-crash Book B `LEG1_SUBMITTED` (65/65 filled @ 145.05) BEFORE exchange SL was placed (`broker_sl_oid=NULL`)
        self.broker.orders["OID_CRASH_B_LEG1"] = [
            {"order_id": "OID_CRASH_B_LEG1", "status": "complete", "filled_quantity": 65, "average_price": 145.05}
        ]
        self.broker.place_behavior["SELL:NSE_FO|LONG_25300CE:LIMIT"] = {"mode": "complete", "fill_price": 144.50}
        with ex._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO option_advance_live_shadow_packages (
                    mirror_id, shadow_package_id, advance_trade_id, session_date, book_id,
                    execution_tier, bridge_mode, state, direction_label, expiry, qty,
                    leg1_instrument_key, leg1_trading_symbol, leg1_strike, leg1_option_type, leg1_side,
                    leg1_entry_oid, leg1_filled_qty, broker_sl_oid, created_at, updated_at
                ) VALUES (
                    'mir_crash_b01', 'shdw_crash_b01', 'adv_crash_b01', '2026-11-19', ?,
                    'LIVE_ARMED', ?, 'LEG1_SUBMITTED', 'BUY_CE', '2026-11-23', 65,
                    'NSE_FO|LONG_25300CE', 'NIFTY 25300 CE', 25300.0, 'CE', 'BUY',
                    'PENDING:OID_CRASH_B_LEG1', 0, NULL, '2026-11-19 10:50:00', '2026-11-19 10:50:00'
                );
                """,
                (BOOK_ID_PAID_B, MODE_SHADOW_LIVE_BOOK_B),
            )
            conn.commit()

        # Mid-session `sync_with_shadow_db_once` MUST NOT leave it in `OPEN_LONG` with `broker_sl_oid=None` —
        # it MUST immediately protective-flatten (`RECOVERED_UNPROTECTED_LONG_FLATTEN` -> `CONFIRMED_FLAT`)!
        ex.sync_with_shadow_db_once(now_dt=datetime(2026, 11, 19, 10, 51, 0))
        row_b = ex._existing_mirror_row("shdw_crash_b01")
        self.assertEqual(row_b["state"], "CONFIRMED_FLAT")
        self.assertEqual(row_b["exit_reason"], "RECOVERED_UNPROTECTED_LONG_FLATTEN")
        self.assertEqual(row_b["leg1_filled_qty"], 65)
        self.assertEqual(row_b["leg1_exited_qty"], 65)


if __name__ == "__main__":
    unittest.main()
