"""
Hostile Audit Hardening Suite (`test_audit_6_findings_hardening.py`):
Proves end-to-end resolution of all 6 institutional audit findings across W1, W2, and W3:

  1. Finding #1 (Live Exit Collar Fallback):
     - Emergency REST failure (`504` / timeout / empty book) NEVER returns a stale quote or prices
       Stage 1/2/3 LIMIT exit orders from stale/fallback data (`0` wing orders placed while REST fails).
     - Escalates cleanly to `disarm_callback("UNRECOVERABLE_STALE_EXIT_QUOTE")` on the 3rd consecutive
       failure (outside `_pkg_locks`) while keeping `EXIT_RETRY_PENDING` alive so recovery still exits flat.
  2. Finding #2 (Frontend & Bridge Stale MTM Surfacing):
     - `ShadowMirrorExecutor` flags `quote_stale=True` on both `>10s` age and `None`/unknown age,
       `AdvanceLiveBridge` exposes `has_stale_positions=True` + conservative circuit-breaker MTM,
       new live entries block via `STALE_OPEN_POSITION_MTM_BLOCK`, and `PositionsTab.jsx` / `OverviewTab.jsx`
       consume `quote_stale`, `wing_quote_age_ms`, `short_quote_age_ms`, `has_stale_positions`, and `readiness_stale`.
  3. Finding #3A & #3B (Unknown Data Rejected Fail-Closed):
     - Missing/unknown ages return `UNKNOWN_QUOTE_AGE` (never default to `0`), fresh `quote_age_ms`
       cannot mask stale `l1_book_age_ms` on L5 depth sweeps (`STALE_L1_BOOK`), and `_quotes_within_skew`
       returns `False` whenever either timestamp is missing/`0`/sentinel.
  4. Finding #4 (Unpriced Open Risk Never Understates Daily Loss Kill):
     - `_evaluate_open_unrealized_mtm` sets `DEGRADED_UNPRICED_OPEN_RISK` (`effective_unrealized_inr=None`),
       retains last-known adverse MTM (`min(0, last_valid_mtm)`) for daily kill while preventing green
       open packages from masking realized/unpriced losses, and halts new entries (`HALTED_UNPRICED_RISK`).
  5. Finding #5A & #5B (Complete Intrabar Protective-Stop Recovery):
     - Stale short leg is recovered via emergency REST *before* 15:15 EOD when wing ticks, and both
       ratchet-stop breaches (below entry price) and `<₹12` small-credit Hard-SL breaches recover stale wings.
  6. Finding #6 (Rate-Limited Readiness Cache Stale Flag & 120s Expiry):
     - Rate-limit fallback marks `readiness_stale=True` + `readiness_cache_age_sec` at 45s without mutating
       cache in place, and forces `live_trading_eligible=False` with explicit blocker once age exceeds `120s`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
import os
import sys
import tempfile
import time
import types
from typing import Any, Dict, List, Optional, Tuple
import unittest
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO_ROOT = os.path.abspath(os.path.join(BACKEND_DIR, ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.analytics.common.options_accounting import (
    OptionLeg,
    compute_package_accounting,
)
from app.data.database import set_test_db_override


def _ensure_sandbox_stubs() -> None:
    if "numpy" not in sys.modules:
        try:
            import numpy  # noqa: F401
        except ModuleNotFoundError:
            _np = types.ModuleType("numpy")
            _np.ndarray = object  # type: ignore[attr-defined]
            _np.float64 = float  # type: ignore[attr-defined]
            sys.modules["numpy"] = _np

    if "pandas" not in sys.modules:
        try:
            import pandas  # noqa: F401
        except ModuleNotFoundError:
            _pd = types.ModuleType("pandas")
            _pd.DataFrame = object  # type: ignore[attr-defined]
            _pd.Series = object  # type: ignore[attr-defined]
            sys.modules["pandas"] = _pd

    if "requests" not in sys.modules:
        try:
            import requests  # noqa: F401
        except ModuleNotFoundError:
            _req = types.ModuleType("requests")
            _req.Response = object  # type: ignore[attr-defined]
            _req.get = MagicMock()  # type: ignore[attr-defined]
            _req.post = MagicMock()  # type: ignore[attr-defined]
            _req.request = MagicMock()  # type: ignore[attr-defined]
            sys.modules["requests"] = _req

    if "pytz" not in sys.modules:
        try:
            import pytz  # noqa: F401
        except ModuleNotFoundError:
            from zoneinfo import ZoneInfo

            class _ZoneInfoPytzAdapter(ZoneInfo):
                def localize(self, dt: datetime, is_dst: Any = None) -> datetime:
                    if dt.tzinfo is not None:
                        return dt.astimezone(self)
                    return dt.replace(tzinfo=self)

                def normalize(self, dt: datetime) -> datetime:
                    return dt.astimezone(self)

            _pytz = types.ModuleType("pytz")
            _pytz.timezone = lambda name: _ZoneInfoPytzAdapter(name)  # type: ignore[attr-defined]
            _pytz.utc = _ZoneInfoPytzAdapter("UTC")  # type: ignore[attr-defined]
            _pytz.UTC = _ZoneInfoPytzAdapter("UTC")  # type: ignore[attr-defined]
            sys.modules["pytz"] = _pytz

    if "app.data.historical_data_layer" not in sys.modules:
        try:
            import app.data.historical_data_layer  # noqa: F401
        except ModuleNotFoundError:
            from app.data.database import get_db_path as _db_fn

            _hdl = types.ModuleType("app.data.historical_data_layer")
            _hdl.get_db_path = _db_fn  # type: ignore[attr-defined]
            sys.modules["app.data.historical_data_layer"] = _hdl

    if "app.data.upstox_stream_service" not in sys.modules:
        try:
            import app.data.upstox_stream_service  # noqa: F401
        except ModuleNotFoundError:
            _uss = types.ModuleType("app.data.upstox_stream_service")

            class _FakeStreamSvc:
                _is_connected = True
                _lock = __import__("threading").RLock()
                _key_to_symbol: Dict[str, str] = {}
                _additional_keys: set = set()
                _subscribed_ws_keys: set = set()
                _feed_listeners: List[Any] = []

            inst = _FakeStreamSvc()
            _uss.get_stream_service = lambda: inst  # type: ignore[attr-defined]
            sys.modules["app.data.upstox_stream_service"] = _uss


_ensure_sandbox_stubs()

try:
    from app.analytics.option_sniffer_advance_s205.s205_shadow_collector import (
        S205ShadowCollector,
        _quotes_within_skew,
        sweep_l5_book_for_qty,
    )
    from app.analytics.option_sniffer_advance_s205.s205_signal_evaluator import (
        build_causal_intraday_context,
    )
except ModuleNotFoundError:
    _PKG_NAME = "app.analytics.option_sniffer_advance_s205"
    _PKG_DIR = os.path.join(BACKEND_DIR, "app", "analytics", "option_sniffer_advance_s205")
    if _PKG_NAME not in sys.modules:
        _pkg = types.ModuleType(_PKG_NAME)
        _pkg.__path__ = [_PKG_DIR]
        sys.modules[_PKG_NAME] = _pkg

    def _load_s205_submod(sub_name: str):
        fq = f"{_PKG_NAME}.{sub_name}"
        if fq in sys.modules:
            return sys.modules[fq]
        sp = importlib.util.spec_from_file_location(fq, os.path.join(_PKG_DIR, f"{sub_name}.py"))
        mod = importlib.util.module_from_spec(sp)  # type: ignore[arg-type]
        sys.modules[fq] = mod
        sp.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    _load_s205_submod("s205_meta_model")
    _cf_mod = _load_s205_submod("s205_signal_evaluator")
    _sc_mod = _load_s205_submod("s205_shadow_collector")
    build_causal_intraday_context = _cf_mod.build_causal_intraday_context
    S205ShadowCollector = _sc_mod.S205ShadowCollector
    _quotes_within_skew = _sc_mod._quotes_within_skew
    sweep_l5_book_for_qty = _sc_mod.sweep_l5_book_for_qty

from app.analytics.option_sniffer.option_live_adapter import (
    PRIMARY_WHITELISTED_IPV6,
    OptionLiveAdapter,
)
from app.analytics.option_sniffer_advance_live.shadow_mirror_executor import (
    BOOK_ID_PAID_B,
    BOOK_ID_SPREAD_A,
    MODE_SHADOW_LIVE_BOOK_A,
    ShadowMirrorExecutor,
    compute_smart_limit_collar,
    init_shadow_mirror_tables,
)
from app.data.upstox_provider import get_upstox_limiter


def _make_l5_quote(
    ikey: str,
    expiry: str,
    strike: float,
    opt_type: str,
    bid: float,
    ask: float,
    age_ms: int = 80,
    ts_ms: int = 1763881860000,
    observed: bool = True,
    obi: float = 0.22,
) -> Dict[str, Any]:
    levels = [
        {
            "bid_p": round(max(0.05, bid - i * 0.05), 2),
            "bid_q": 65,
            "ask_p": round(ask + i * 0.05, 2),
            "ask_q": 65,
        }
        for i in range(5)
    ]
    mid = round(0.5 * (bid + ask), 2)
    return {
        "instrument_key": ikey,
        "trading_symbol": f"NIFTY {expiry} {int(strike)}{opt_type}",
        "underlying": "NIFTY",
        "expiry": expiry,
        "strike": float(strike),
        "option_type": opt_type,
        "lot_size": 65,
        "ltp": mid,
        "bid": bid,
        "ask": ask,
        "raw_bid": bid,
        "raw_ask": ask,
        "bid_qty": 65,
        "ask_qty": 65,
        "l5_bid_qty": 325,
        "l5_ask_qty": 325,
        "book_obi_l5": obi,
        "depth_levels": levels,
        "is_observed_l1_book": observed,
        "depth_observed_on_tick": observed,
        "book_provenance": "OBSERVED_V3_L5" if observed else "EMERGENCY_REST_L1",
        "l1_book_timestamp_ms": ts_ms - age_ms if observed else 0,
        "l1_book_age_ms": age_ms if observed else 999999,
        "quote_age_ms": age_ms,
        "local_receipt_ts_ms": ts_ms,
        "timestamp_ms": ts_ms,
        "spread": round(ask - bid, 2),
        "spread_pct": round(((ask - bid) / mid) * 100.0, 2) if mid > 0 else 0.0,
        "volume": 20000,
        "oi": 500000.0,
        "iv": 15.2,
        "delta": 0.45 if opt_type == "CE" else -0.35,
        "is_valid": True,
    }


def _build_trending_session(day: str = "2026-11-24", exp: str = "2026-12-01", num_bars: int = 45):
    base_dt = datetime.fromisoformat(f"{day} 09:15:00")
    spot_bars: Dict[str, Tuple[float, float, float, float, float]] = {}
    vix_bars: Dict[str, Tuple[float, float, float, float]] = {}
    opt_bars: Dict[Tuple[str, float, str], Dict[str, Tuple[float, float, float, float, float, float]]] = {}
    for i in range(num_bars):
        ts = (base_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
        drift = 1.5 * i if i < 30 else 45.0 + 4.5 * (i - 29)
        o = 24000.0 + drift
        spot_bars[ts] = (o, o + 7.0, o - 3.0, o + 5.0, 1200.0 + i * 10.0)
        vix_bars[ts] = (14.2, 14.3, 14.1, 14.2)
        atm_k = round((o + 5.0) / 50.0) * 50.0
        for stk in (atm_k, atm_k - 100.0, atm_k - 250.0):
            for ot in ("CE", "PE"):
                opt_bars.setdefault((exp, float(stk), ot), {})[ts] = (110.0, 114.0, 108.0, 112.0, 500.0, 300000.0)
    return day, spot_bars, vix_bars, opt_bars


class TestAudit6FindingsHardening(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "audit6_test.db")
        set_test_db_override(self.db_path)
        init_shadow_mirror_tables(self.db_path)

    def tearDown(self) -> None:
        set_test_db_override(None)
        self._tmp.cleanup()

    def test_01_finding1_rest_failure_never_submits_stale_exit_collar_and_escalates_on_3rd_strike(self) -> None:
        placed_orders: List[Dict[str, Any]] = []
        disarm_events: List[str] = []
        rest_healthy = [False]
        oid_seq = [200]
        fills_by_oid: Dict[str, Tuple[int, float]] = {}
        bridge_armed = [True]

        def fake_broker_api(
            method: str,
            endpoint: str,
            payload: Optional[Dict[str, Any]] = None,
            params: Optional[Dict[str, Any]] = None,
            is_emergency: bool = False,
        ) -> Tuple[bool, int, Dict[str, Any]]:
            if method == "GET" and endpoint == "/market-quote/quotes":
                if not rest_healthy[0]:
                    return False, 504, {"error": "UPSTOX_GATEWAY_TIMEOUT"}
                return True, 200, {
                    "status": "success",
                    "data": {
                        "NSE_FO:47839": {
                            "instrument_token": "NSE_FO|47839",
                            "last_price": 118.20,
                            "depth": {
                                "buy": [{"price": 118.00, "quantity": 195}],
                                "sell": [{"price": 118.40, "quantity": 195}],
                            },
                        }
                    },
                }
            if method == "POST" and endpoint == "/order/place":
                oid_seq[0] += 1
                oid = f"OID_{oid_seq[0]}"
                pl = dict(payload or {})
                placed_orders.append(pl)
                fills_by_oid[oid] = (int(pl["quantity"]), float(pl["price"]))
                return True, 200, {"status": "success", "data": {"order_id": oid}}
            if method == "GET" and endpoint == "/order/history":
                oid = str((params or {}).get("order_id") or "")
                fq, fp = fills_by_oid.get(oid, (65, 100.0))
                return True, 200, {
                    "status": "success",
                    "data": [{"status": "complete", "filled_quantity": fq, "average_price": fp}],
                }
            if method == "GET" and endpoint == "/portfolio/short-term-positions":
                return True, 200, {
                    "status": "success",
                    "data": [{"instrument_token": "NSE_FO|47839", "quantity": 65, "buy_price": 155.00}],
                }
            return True, 200, {"status": "success", "data": []}

        quote_state: Dict[str, Dict[str, Any]] = {
            "NSE_FO|47839": {"bid": 154.80, "ask": 155.00, "ltp": 154.90, "quote_age_ms": 80},
            "NSE_FO|47821": {"bid": 209.70, "ask": 210.10, "ltp": 209.90, "quote_age_ms": 80},
        }

        def on_disarm(rsn: str) -> None:
            disarm_events.append(rsn)
            bridge_armed[0] = False

        ex = ShadowMirrorExecutor(
            db_path=self.db_path,
            bridge_state_fn=lambda *_: {
                "is_live_armed": bridge_armed[0],
                "execution_mode": MODE_SHADOW_LIVE_BOOK_A,
                "armed_for_date": "2026-11-24",
                "armed_at_iso": "2026-11-24 09:45:00",
                "is_session_halted": False,
                "has_stale_positions": False,
                "daily_total_exposure_pnl_inr": 0.0,
                "max_daily_loss_inr": 3500.0,
            },
            disarm_callback=on_disarm,
            broker_api_fn=fake_broker_api,
            quote_lookup_fn=lambda k: quote_state.get(k),
            readiness_fn=lambda: {"live_trading_eligible": True, "available_margin": 85448.27},
            poll_interval_sec=0.001,
            auto_start_worker=False,
        )

        open_snap = {
            "package_id": "shdw_f1_collar01",
            "advance_trade_id": "adv_shdw_f1_01",
            "session_date": "2026-11-24",
            "book_id": BOOK_ID_SPREAD_A,
            "state": "OPEN",
            "direction_label": "SPREAD_SELL_CE",
            "expiry": "2026-12-01",
            "entry_bar_ts": "2026-11-24 11:02:00",
            "entry_time": "2026-11-24T11:03:01",
            "upstox_final_margin_inr": 39676.26,
            "legs_state": {
                "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
                "wing_leg": {"instrument_key": "NSE_FO|47839", "trading_symbol": "W", "expiry": "2026-12-01", "strike": 25800.0, "option_type": "CE", "qty": 65, "entry_price": 155.00},
                "short_leg": {"instrument_key": "NSE_FO|47821", "trading_symbol": "S", "expiry": "2026-12-01", "strike": 25650.0, "option_type": "CE", "qty": -65, "entry_price": 209.70},
                "net_entry_credit_pts": 54.70,
                "hard_stop_debit_pts": 90.26,
                "target_debit_pts": 16.41,
                "is_bull": False,
            },
        }
        r_open = ex.process_shadow_event_sync("BOOK_A_OPEN", open_snap, now_dt=datetime(2026, 11, 24, 11, 3, 2))
        self.assertEqual(r_open["state"], "OPEN_HEDGED")
        placed_orders.clear()

        # Wing freezes at ₹147.65 (90s stale) AND emergency REST fails with 504!
        quote_state["NSE_FO|47839"] = {"bid": 147.65, "ask": 148.00, "ltp": 147.80, "quote_age_ms": 90000}
        quote_state["NSE_FO|47821"] = {"bid": 161.90, "ask": 162.25, "ltp": 162.10, "quote_age_ms": 80}

        close_snap = {**open_snap, "state": "CLOSED", "exit_reason": "EOD_SQUAREOFF_1515", "net_pnl_inr": 900.0}
        # Strike 1: Short leg buys back cleanly; wing leg MUST NOT submit a stale LIMIT order!
        r_c1 = ex.process_shadow_event_sync("BOOK_A_CLOSE", close_snap, now_dt=datetime(2026, 11, 24, 15, 15, 1))
        self.assertEqual(r_c1["state"], "EXIT_RETRY_PENDING")
        self.assertIn("STALE_EXIT_QUOTE_UNRECOVERABLE", str(r_c1.get("error_reason") or ""))
        wing_orders = [o for o in placed_orders if o["instrument_token"] == "NSE_FO|47839"]
        self.assertEqual(len(wing_orders), 0, "Zero wing exit orders may be priced from stale quote on REST failure!")
        self.assertEqual(len(disarm_events), 0)

        # Strike 2 & Strike 3: On 3rd consecutive failure, escalates auto-disarm!
        ex.sync_with_shadow_db_once(now_dt=datetime(2026, 11, 24, 15, 15, 3))
        self.assertEqual(len(disarm_events), 0)
        ex.sync_with_shadow_db_once(now_dt=datetime(2026, 11, 24, 15, 15, 5))
        self.assertEqual(disarm_events, ["UNRECOVERABLE_STALE_EXIT_QUOTE"])
        self.assertFalse(bridge_armed[0])
        self.assertEqual(len([o for o in placed_orders if o["instrument_token"] == "NSE_FO|47839"]), 0)

        # Recovery: REST comes back online (₹118.00) -> even though disarmed for new entries, open wing exits cleanly!
        rest_healthy[0] = True
        ex.sync_with_shadow_db_once(now_dt=datetime(2026, 11, 24, 15, 15, 8))
        row_final = ex._existing_mirror_row("shdw_f1_collar01")
        self.assertEqual(row_final["state"], "CONFIRMED_FLAT")
        wing_orders_after = [o for o in placed_orders if o["instrument_token"] == "NSE_FO|47839"]
        self.assertEqual(len(wing_orders_after), 1)
        self.assertEqual(wing_orders_after[0]["price"], compute_smart_limit_collar(118.00, "SELL", stage=1))

    def test_02_finding2_stale_live_position_flags_bridge_cb_and_jsx_source_consumption(self) -> None:
        from app.analytics.option_sniffer_advance_live.advance_live_bridge import (
            AdvanceLiveBridge,
            init_advance_live_tables,
        )

        init_advance_live_tables(self.db_path)
        bridge = AdvanceLiveBridge(db_path=self.db_path, auto_start_daemon=False)

        # Simulate 2 active positions: Position 1 is stale with phantom +₹2,500; Position 2 is fresh at -₹2,200
        # Realized today is -₹1,500 -> Conservative exposure MUST be -₹1,500 + -₹2,200 = -₹3,700 (trips -₹3,500 CB!)
        today = datetime.now().strftime("%Y-%m-%d")
        active_mock = [
            {
                "order_intent_id": "mir_stale_pos",
                "created_at": f"{today} 10:05:00",
                "unrealized_pnl": 2500.0,
                "quote_stale": True,
                "wing_quote_age_ms": 85000,
                "short_quote_age_ms": 90,
            },
            {
                "order_intent_id": "mir_fresh_loss",
                "created_at": f"{today} 10:20:00",
                "unrealized_pnl": -2200.0,
                "quote_stale": False,
                "wing_quote_age_ms": 110,
                "short_quote_age_ms": 95,
            },
        ]
        summary = bridge.get_live_pnl_summary(active_positions=active_mock)
        self.assertTrue(summary["has_stale_positions"])
        self.assertEqual(summary["stale_positions_count"], 1)
        self.assertEqual(summary["mtm_visibility_status"], "STALE_QUOTE_DEGRADED")
        self.assertEqual(summary["clean_unrealized_net_pnl"], -2200.0)

        with patch.object(bridge._shadow_mirror, "get_today_live_closed_events", return_value=[(f"{today} 09:58:00", -1500.0, 0.0, -1500.0)]):
            cb = bridge.get_circuit_breaker_status(today, active_positions=active_mock)
        self.assertTrue(cb["has_stale_positions"])
        self.assertEqual(cb["total_exposure_pnl_inr"], -3700.0)
        self.assertTrue(cb["is_session_halted"], "Phantom positive stale MTM must NOT mask realized + fresh losses!")

        # Verify `STALE_OPEN_POSITION_MTM_BLOCK` blocks new LIVE_ARMED entries
        ex = ShadowMirrorExecutor(
            db_path=self.db_path,
            bridge_state_fn=lambda *_: {
                "is_live_armed": True,
                "execution_mode": MODE_SHADOW_LIVE_BOOK_A,
                "armed_for_date": "2026-11-24",
                "armed_at_iso": "2026-11-24 09:45:00",
                "is_session_halted": False,
                "has_stale_positions": True,
                "daily_total_exposure_pnl_inr": 0.0,
                "max_daily_loss_inr": 3500.0,
            },
            quote_lookup_fn=lambda k: {"bid": 50.0, "ask": 50.5, "ltp": 50.2, "quote_age_ms": 90},
            readiness_fn=lambda: {"live_trading_eligible": True, "available_margin": 90000.0},
            auto_start_worker=False,
        )
        res_pre = ex.process_shadow_event_sync(
            "BOOK_A_OPEN",
            {
                "package_id": "shdw_f2_block01",
                "advance_trade_id": "adv_f2_block01",
                "session_date": "2026-11-24",
                "book_id": BOOK_ID_SPREAD_A,
                "state": "OPEN",
                "direction_label": "SPREAD_SELL_CE",
                "expiry": "2026-12-01",
                "entry_bar_ts": "2026-11-24 11:00:00",
                "entry_time": "2026-11-24T11:00:01",
                "upstox_final_margin_inr": 39500.0,
                "legs_state": {
                    "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
                    "wing_leg": {"instrument_key": "NSE_FO|W", "trading_symbol": "W", "expiry": "2026-12-01", "strike": 25800.0, "option_type": "CE", "qty": 65, "entry_price": 50.5},
                    "short_leg": {"instrument_key": "NSE_FO|S", "trading_symbol": "S", "expiry": "2026-12-01", "strike": 25650.0, "option_type": "CE", "qty": -65, "entry_price": 100.0},
                    "net_entry_credit_pts": 49.5,
                    "hard_stop_debit_pts": 81.68,
                    "target_debit_pts": 14.85,
                    "is_bull": False,
                },
            },
            now_dt=datetime(2026, 11, 24, 11, 0, 2),
        )
        self.assertEqual(res_pre["state"], "PREFLIGHT_BLOCKED")
        self.assertEqual(res_pre["error_reason"], "STALE_OPEN_POSITION_MTM_BLOCK")
        self.assertEqual(ex.mutation_call_count, 0)

        # Verify frontend JSX files wire all stale MTM & readiness cache fields
        pos_jsx_path = os.path.join(REPO_ROOT, "frontend", "src", "pages", "optionSnifferAdvance", "PositionsTab.jsx")
        ov_jsx_path = os.path.join(REPO_ROOT, "frontend", "src", "pages", "optionSnifferAdvance", "OverviewTab.jsx")
        with open(pos_jsx_path, "r", encoding="utf-8") as fh:
            pos_src = fh.read()
        with open(ov_jsx_path, "r", encoding="utf-8") as fh:
            ov_src = fh.read()
        self.assertIn("quote_stale", pos_src)
        self.assertIn("wing_quote_age_ms", pos_src)
        self.assertIn("short_quote_age_ms", pos_src)
        self.assertIn("STALE QUOTE", pos_src)
        self.assertIn("has_stale_positions", ov_src)
        self.assertIn("STALE MTM", ov_src)
        self.assertIn("readiness_stale", ov_src)

    def test_03a_finding3_unknown_age_and_fresh_ltp_masking_stale_depth_rejected(self) -> None:
        # 1. Valid book prices/depth with missing ages -> UNKNOWN_QUOTE_AGE (never defaults to 0!)
        q_unknown = _make_l5_quote("NSE_FO|1", "2026-12-01", 25000.0, "PE", 50.0, 50.5)
        q_unknown.pop("quote_age_ms", None)
        q_unknown.pop("l1_book_age_ms", None)
        r_unk = sweep_l5_book_for_qty(q_unknown, "SELL", qty=65, max_age_ms=5000, max_spread_pct=None, require_observed=False)
        self.assertFalse(r_unk["ok"])
        self.assertEqual(r_unk["reason"], "UNKNOWN_QUOTE_AGE")

        # 2. Fresh LTP (`quote_age_ms=40`) + stale L5 depth (`l1_book_age_ms=14000`) -> STALE_L1_BOOK!
        q_masked = _make_l5_quote("NSE_FO|1", "2026-12-01", 25000.0, "PE", 50.0, 50.5, age_ms=14000)
        q_masked["quote_age_ms"] = 40
        r_mask = sweep_l5_book_for_qty(q_masked, "SELL", qty=65, max_age_ms=5000, max_spread_pct=None, require_observed=False)
        self.assertFalse(r_mask["ok"])
        self.assertEqual(r_mask["reason"], "STALE_L1_BOOK")
        self.assertEqual(r_mask["l1_book_age_ms"], 14000)

        # 3. Unbacked 999999 sentinel (`l1_book_timestamp_ms=0`) on depth book -> UNKNOWN_QUOTE_AGE!
        q_sent = _make_l5_quote("NSE_FO|1", "2026-12-01", 25000.0, "PE", 50.0, 50.5)
        q_sent["l1_book_age_ms"] = 999999
        q_sent["l1_book_timestamp_ms"] = 0
        r_sent = sweep_l5_book_for_qty(q_sent, "SELL", qty=65, max_age_ms=5000, max_spread_pct=None, require_observed=False)
        self.assertFalse(r_sent["ok"])
        self.assertEqual(r_sent["reason"], "UNKNOWN_QUOTE_AGE")

    def test_03b_finding3_cross_leg_skew_fails_closed_on_missing_or_zero_timestamps(self) -> None:
        t_now = 1763960000000
        q_valid = _make_l5_quote("NSE_FO|A", "2026-12-01", 25000.0, "PE", 40.0, 40.5, age_ms=80, ts_ms=t_now)
        q_no_ts = {
            "bid": 20.0,
            "ask": 20.4,
            "l1_book_timestamp_ms": 0,
            "local_receipt_ts_ms": 0,
            "timestamp_ms": 0,
        }
        self.assertFalse(_quotes_within_skew(q_valid, q_no_ts))
        self.assertFalse(_quotes_within_skew(q_no_ts, q_no_ts))
        self.assertFalse(_quotes_within_skew(q_valid, {}))

        q_valid_b = _make_l5_quote("NSE_FO|B", "2026-12-01", 24850.0, "PE", 18.0, 18.4, age_ms=120, ts_ms=t_now - 800)
        self.assertTrue(_quotes_within_skew(q_valid, q_valid_b))

    def test_04_finding4_unpriced_open_mtm_retains_adverse_exposure_for_kill_and_halts_new_entries(self) -> None:
        sc = S205ShadowCollector(db_path=self.db_path, sync_advance_db=False)
        day, spot_b, vix_b, opt_b = _build_trending_session("2026-11-24", "2026-12-01", 45)
        ctx = build_causal_intraday_context(day, spot_b, vix_b, {}, opt_b, "2026-12-01", "2026-12-08", {})
        b_idx = len(ctx["ts"]) - 1
        atm = round(ctx["c"][b_idx] / 50.0) * 50.0

        # Open 1: Book B paid long currently UP +₹1,100
        pkg_green = {
            "package_id": "shdw_green_b",
            "advance_trade_id": "adv_green_b",
            "prereg_sha256": sc.prereg_sha256,
            "session_date": day,
            "book_id": "SHADOW_BOOK_B_PAID_FLOW",
            "ui_book_type": "LONG_BOOK_BUY",
            "state": "OPEN",
            "direction_label": "BUY_CE",
            "expiry": "2026-12-01",
            "entry_bar_ts": f"{day} 09:50:00",
            "entry_time": f"{day}T09:50:01",
            "leg2_confirmed_time": f"{day}T09:50:01",
            "legs_state": {
                "mode": "SINGLE_PAID_LONG",
                "long_leg": {"instrument_key": "NSE_FO|CE_GREEN", "trading_symbol": "CE", "expiry": "2026-12-01", "strike": atm, "option_type": "CE", "qty": 65, "entry_price": 100.0},
                "hard_stop": 78.0,
                "ratchet_stop": None,
                "is_bull": True,
            },
            "structural_max_loss_inr": 6500.0,
            "upstox_final_margin_inr": 6500.0,
            "margin_status": "CASH_PAID_LONG_OUTLAY",
            "cash_reserved_inr": 6500.0,
            "regime_context": {"shadow_only": True},
        }
        # Open 2: Book A spread that was previously at -₹1,200 MTM before its wing went stale
        pkg_stale_a = {
            "package_id": "shdw_stale_a",
            "advance_trade_id": "adv_stale_a",
            "prereg_sha256": sc.prereg_sha256,
            "session_date": day,
            "book_id": "SHADOW_BOOK_A_SPREAD_150",
            "ui_book_type": "SHORT_BOOK_SELL",
            "state": "OPEN",
            "direction_label": "SPREAD_SELL_PE",
            "expiry": "2026-12-01",
            "entry_bar_ts": f"{day} 09:52:00",
            "entry_time": f"{day}T09:52:01",
            "leg2_confirmed_time": f"{day}T09:52:02",
            "legs_state": {
                "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
                "wing_leg": {"instrument_key": "NSE_FO|PE_W", "trading_symbol": "W", "expiry": "2026-12-01", "strike": atm - 250.0, "option_type": "PE", "qty": 65, "entry_price": 12.0},
                "short_leg": {"instrument_key": "NSE_FO|PE_S", "trading_symbol": "S", "expiry": "2026-12-01", "strike": atm - 100.0, "option_type": "PE", "qty": -65, "entry_price": 32.0},
                "net_entry_credit_pts": 20.0,
                "hard_stop_debit_pts": 33.0,
                "target_debit_pts": 6.0,
                "is_bull": True,
            },
            "structural_max_loss_inr": 8580.0,
            "upstox_final_margin_inr": 39500.0,
            "margin_status": "UPSTOX_V2_CHARGES_MARGIN_VERIFIED",
            "cash_reserved_inr": 39500.0,
            "regime_context": {"shadow_only": True},
        }
        sc._persist_open_package(pkg_green)
        sc._persist_open_package(pkg_stale_a)
        sc._open_packages["shdw_green_b"] = pkg_green
        sc._open_packages["shdw_stale_a"] = pkg_stale_a

        # First evaluate when BOTH packages are validly priced: pkg_green ~ +₹1,100, pkg_stale_a ~ -₹1,250
        t0 = 1763960000000
        q_ce_up = _make_l5_quote("NSE_FO|CE_GREEN", "2026-12-01", atm, "CE", 118.0, 118.5, age_ms=80, ts_ms=t0)
        q_pe_s_adv = _make_l5_quote("NSE_FO|PE_S", "2026-12-01", atm - 100.0, "PE", 55.0, 55.4, age_ms=80, ts_ms=t0)
        q_pe_w_adv = _make_l5_quote("NSE_FO|PE_W", "2026-12-01", atm - 250.0, "PE", 17.6, 18.0, age_ms=80, ts_ms=t0)
        mtm_ok = sc._evaluate_open_unrealized_mtm(
            {
                ("2026-12-01", atm, "CE"): q_ce_up,
                ("2026-12-01", atm - 100.0, "PE"): q_pe_s_adv,
                ("2026-12-01", atm - 250.0, "PE"): q_pe_w_adv,
            },
            day,
        )
        self.assertEqual(mtm_ok["mtm_visibility_status"], "COMPLETE")
        self.assertEqual(mtm_ok["unpriced_open_count"], 0)
        last_adv_a = sc._last_valid_pkg_mtm_inr["shdw_stale_a"]
        self.assertLess(last_adv_a, -1000.0)

        # Now wing quote on pkg_stale_a goes STALE (age_ms=80000)!
        q_pe_w_stale = _make_l5_quote("NSE_FO|PE_W", "2026-12-01", atm - 250.0, "PE", 17.6, 18.0, age_ms=80000, ts_ms=t0 - 80000)
        qlookup_degraded = {
            ("2026-12-01", atm, "CE"): q_ce_up,
            ("2026-12-01", atm - 100.0, "PE"): q_pe_s_adv,
            ("2026-12-01", atm - 250.0, "PE"): q_pe_w_stale,
        }
        mtm_deg = sc._evaluate_open_unrealized_mtm(qlookup_degraded, day)
        self.assertEqual(mtm_deg["mtm_visibility_status"], "DEGRADED_UNPRICED_OPEN_RISK")
        self.assertEqual(mtm_deg["unpriced_open_count"], 1)
        self.assertIsNone(mtm_deg["effective_unrealized_inr"])
        # Green +₹1,100 MUST NOT offset unpriced adverse MTM (`last_adv_a`)!
        self.assertAlmostEqual(mtm_deg["conservative_kill_mtm_inr"], round(last_adv_a, 2), places=2)

        # Even before daily kill trips, unpriced risk MUST set HALTED_UNPRICED_RISK & block new entries!
        sc._session_date = day
        rows_halt, opened_halt, _ = sc.evaluate_completed_bar(
            ctx=ctx,
            bar_idx=b_idx,
            quote_lookup=qlookup_degraded,
            fut_quote={"fut_ltp": ctx["c"][b_idx] + 9.0},
            india_vix=14.2,
            now_dt=datetime(2026, 11, 24, 10, 0, 0),
            execute_new_entries=True,
            allow_network=False,
        )
        self.assertEqual(len(opened_halt), 0)
        self.assertTrue(all(r["state"] == "HALTED_UNPRICED_RISK" for r in rows_halt))
        led = sc.get_summary()["shadow_account_ledger"]
        self.assertEqual(led["mtm_visibility_status"], "DEGRADED_UNPRICED_OPEN_RISK")
        self.assertIsNone(led["effective_unrealized_mtm_inr"])

        # Add a -₹2,500 realized closed package today -> realized (-₹2,500) + conservative MTM (last_adv_a < -₹1,000) <= -₹3,500 -> TRIPS DAILY KILL!
        sc._persist_closed_or_unwound_package(
            {
                **pkg_green,
                "package_id": "shdw_closed_loss",
                "advance_trade_id": "adv_closed_loss",
                "state": "CLOSED",
                "exit_bar_ts": f"{day} 09:48:00",
                "exit_time": f"{day}T09:48:30",
                "accounting": {},
                "gross_pnl_inr": -2420.0,
                "total_charges_inr": 80.0,
                "stt_inr": 10.0,
                "brokerage_inr": 40.0,
                "net_pnl_inr": -2500.0,
                "exit_reason": "LONG_HARD_SL_HIT",
            }
        )
        sc.evaluate_completed_bar(
            ctx=ctx,
            bar_idx=b_idx,
            quote_lookup=qlookup_degraded,
            fut_quote={"fut_ltp": ctx["c"][b_idx] + 9.0},
            india_vix=14.2,
            now_dt=datetime(2026, 11, 24, 10, 1, 0),
            execute_new_entries=True,
            allow_network=False,
        )
        self.assertTrue(sc._daily_kill_triggered, "Unpriced open risk must retain adverse MTM and trip daily loss kill!")

    def test_05a_finding5_stale_short_leg_recovered_intrabar_before_eod_for_hard_sl(self) -> None:
        t_1115 = 1763963100000
        q_short_rest = _make_l5_quote(
            "NSE_FO|47821", "2026-12-01", 25650.0, "CE", 261.50, 262.00, age_ms=20, ts_ms=t_1115, observed=False
        )
        rest_fetched: List[str] = []

        sc = S205ShadowCollector(
            db_path=self.db_path,
            sync_advance_db=False,
            emergency_quote_fetcher=lambda ik: (rest_fetched.append(ik) or (q_short_rest if ik == "NSE_FO|47821" else None)),
        )
        pkg = {
            "package_id": "shdw_staleshort01",
            "advance_trade_id": "adv_staleshort01",
            "prereg_sha256": sc.prereg_sha256,
            "session_date": "2026-11-24",
            "book_id": "SHADOW_BOOK_A_SPREAD_150",
            "ui_book_type": "SHORT_BOOK_SELL",
            "state": "OPEN",
            "direction_label": "SPREAD_SELL_CE",
            "expiry": "2026-12-01",
            "entry_bar_ts": "2026-11-24 10:10:00",
            "entry_time": "2026-11-24T10:11:00",
            "leg2_confirmed_time": "2026-11-24T10:11:01",
            "legs_state": {
                "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
                "wing_leg": {"instrument_key": "NSE_FO|47839", "trading_symbol": "W", "expiry": "2026-12-01", "strike": 25800.0, "option_type": "CE", "qty": 65, "entry_price": 159.25},
                "short_leg": {"instrument_key": "NSE_FO|47821", "trading_symbol": "S", "expiry": "2026-12-01", "strike": 25650.0, "option_type": "CE", "qty": -65, "entry_price": 215.70},
                "net_entry_credit_pts": 56.45,
                "best_close_credit_decay_pts": 56.45,
                "ratchet_max_debit_pts": None,
                "hard_stop_debit_pts": 93.14,
                "target_debit_pts": 16.94,
                "is_bull": False,
            },
            "structural_max_loss_inr": 6232.92,
            "upstox_final_margin_inr": 39676.26,
            "margin_status": "UPSTOX_V2_CHARGES_MARGIN_VERIFIED",
            "cash_reserved_inr": 39676.26,
            "regime_context": {"shadow_only": True},
        }
        sc._persist_open_package(pkg)
        sc._open_packages[pkg["package_id"]] = pkg

        # Wing ticks fresh at 11:15 IST (`bid=165.00`), while short leg cached quote is STALE (45s old)!
        q_wing_fresh = _make_l5_quote("NSE_FO|47839", "2026-12-01", 25800.0, "CE", 165.00, 165.40, age_ms=70, ts_ms=t_1115)
        q_short_stale = _make_l5_quote("NSE_FO|47821", "2026-12-01", 25650.0, "CE", 218.00, 218.40, age_ms=45000, ts_ms=t_1115 - 45000)

        closed = sc.on_observed_tick(
            "NSE_FO|47839",
            q_wing_fresh,
            quote_lookup_by_ikey=lambda k: q_short_stale if k == "NSE_FO|47821" else q_wing_fresh,
            now_dt=datetime(2026, 11, 24, 11, 15, 5),
        )
        self.assertEqual(len(closed), 1)
        self.assertIn("NSE_FO|47821", rest_fetched)
        self.assertEqual(closed[0]["exit_reason"], "SPREAD_HARD_SL_HIT")

    def test_05b_finding5_ratchet_stop_and_sub12pt_hard_sl_recover_stale_wing_intrabar(self) -> None:
        t_1310 = 1763970000000
        rest_wing_map: Dict[str, Dict[str, Any]] = {
            "NSE_FO|W_RATCHET": _make_l5_quote("NSE_FO|W_RATCHET", "2026-12-01", 25800.0, "CE", 92.00, 92.30, age_ms=25, ts_ms=t_1310, observed=False),
            "NSE_FO|W_SMALL": _make_l5_quote("NSE_FO|W_SMALL", "2026-12-01", 25800.0, "CE", 14.00, 14.30, age_ms=25, ts_ms=t_1310, observed=False),
        }
        sc = S205ShadowCollector(
            db_path=self.db_path,
            sync_advance_db=False,
            emergency_quote_fetcher=lambda ik: rest_wing_map.get(ik),
        )

        # Case 1: Ratchet stop breach while short leg is 20 pts BELOW entry_price (`130.00 < 150.00`)
        pkg_r = {
            "package_id": "shdw_ratchet_rec01",
            "advance_trade_id": "adv_ratchet_rec01",
            "prereg_sha256": sc.prereg_sha256,
            "session_date": "2026-11-24",
            "book_id": "SHADOW_BOOK_A_SPREAD_150",
            "ui_book_type": "SHORT_BOOK_SELL",
            "state": "OPEN",
            "direction_label": "SPREAD_SELL_CE",
            "expiry": "2026-12-01",
            "entry_bar_ts": "2026-11-24 10:15:00",
            "entry_time": "2026-11-24T10:16:00",
            "leg2_confirmed_time": "2026-11-24T10:16:01",
            "legs_state": {
                "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
                "wing_leg": {"instrument_key": "NSE_FO|W_RATCHET", "trading_symbol": "WR", "expiry": "2026-12-01", "strike": 25800.0, "option_type": "CE", "qty": 65, "entry_price": 100.00},
                "short_leg": {"instrument_key": "NSE_FO|S_RATCHET", "trading_symbol": "SR", "expiry": "2026-12-01", "strike": 25650.0, "option_type": "CE", "qty": -65, "entry_price": 150.00},
                "net_entry_credit_pts": 50.00,
                "best_close_credit_decay_pts": 22.00,
                "ratchet_max_debit_pts": 35.00,
                "hard_stop_debit_pts": 82.50,
                "target_debit_pts": 15.00,
                "is_bull": False,
            },
            "structural_max_loss_inr": 6580.0,
            "upstox_final_margin_inr": 39500.0,
            "margin_status": "UPSTOX_V2_CHARGES_MARGIN_VERIFIED",
            "cash_reserved_inr": 39500.0,
            "regime_context": {"shadow_only": True},
        }
        sc._persist_open_package(pkg_r)
        sc._open_packages["shdw_ratchet_rec01"] = pkg_r

        q_s_ratchet = _make_l5_quote("NSE_FO|S_RATCHET", "2026-12-01", 25650.0, "CE", 129.70, 130.00, age_ms=60, ts_ms=t_1310)
        q_w_stale1 = _make_l5_quote("NSE_FO|W_RATCHET", "2026-12-01", 25800.0, "CE", 96.00, 96.40, age_ms=35000, ts_ms=t_1310 - 35000)
        closed_r = sc.on_observed_tick(
            "NSE_FO|S_RATCHET",
            q_s_ratchet,
            quote_lookup_by_ikey=lambda k: q_w_stale1 if k == "NSE_FO|W_RATCHET" else q_s_ratchet,
            now_dt=datetime(2026, 11, 24, 13, 10, 2),
        )
        self.assertEqual(len(closed_r), 1)
        self.assertEqual(closed_r[0]["exit_reason"], "SPREAD_RATCHET_STOP_HIT")

        # Case 2: Small-credit spread (`net_entry_credit_pts = 14.50`, Hard-SL = `23.93`) where short rises +9.50 (< +12.0!)
        pkg_sm = {
            **pkg_r,
            "package_id": "shdw_smallcr_01",
            "advance_trade_id": "adv_smallcr_01",
            "legs_state": {
                "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
                "wing_leg": {"instrument_key": "NSE_FO|W_SMALL", "trading_symbol": "WS", "expiry": "2026-12-01", "strike": 25800.0, "option_type": "CE", "qty": 65, "entry_price": 15.50},
                "short_leg": {"instrument_key": "NSE_FO|S_SMALL", "trading_symbol": "SS", "expiry": "2026-12-01", "strike": 25650.0, "option_type": "CE", "qty": -65, "entry_price": 30.00},
                "net_entry_credit_pts": 14.50,
                "best_close_credit_decay_pts": 14.50,
                "ratchet_max_debit_pts": None,
                "hard_stop_debit_pts": 23.93,
                "target_debit_pts": 4.35,
                "is_bull": False,
            },
        }
        sc._persist_open_package(pkg_sm)
        sc._open_packages["shdw_smallcr_01"] = pkg_sm

        q_s_small = _make_l5_quote("NSE_FO|S_SMALL", "2026-12-01", 25650.0, "CE", 39.20, 39.50, age_ms=60, ts_ms=t_1310)
        q_w_stale2 = _make_l5_quote("NSE_FO|W_SMALL", "2026-12-01", 25800.0, "CE", 15.80, 16.10, age_ms=35000, ts_ms=t_1310 - 35000)
        closed_sm = sc.on_observed_tick(
            "NSE_FO|S_SMALL",
            q_s_small,
            quote_lookup_by_ikey=lambda k: q_w_stale2 if k == "NSE_FO|W_SMALL" else q_s_small,
            now_dt=datetime(2026, 11, 24, 13, 10, 4),
        )
        self.assertEqual(len(closed_sm), 1)
        self.assertEqual(closed_sm[0]["exit_reason"], "SPREAD_HARD_SL_HIT")

    def test_06_finding6_rate_limited_readiness_cache_flags_stale_and_expires_eligibility_past_120s(self) -> None:
        adapter = OptionLiveAdapter()
        limiter = get_upstox_limiter()
        with adapter._cache_lock:
            adapter._cached_readiness = None
            adapter._last_readiness_fetch = 0.0
            adapter._cached_profile_ip = None
            adapter._last_profile_ip_fetch = 0.0

        def fake_get(url: str, **kwargs: Any) -> Any:
            resp = MagicMock()
            resp.status_code = 200
            if "/user/profile" in url:
                resp.json.return_value = {"data": {"user_id": "U1", "user_name": "Quant", "exchanges": ["NSE", "NFO"]}}
            elif "/user/ip" in url:
                resp.json.return_value = {"data": {"primary_ip": PRIMARY_WHITELISTED_IPV6, "secondary_ip": ""}}
            elif "/user/get-funds-and-margin" in url:
                resp.json.return_value = {"data": {"equity": {"available_margin": 85448.27, "used_margin": 0.0, "payin_amount": 0.0}}}
            return resp

        with patch("app.data.upstox_provider.get_upstox_config", return_value={"algo_token": "VALID_ALGO_TOK"}), patch(
            "app.analytics.option_sniffer.option_live_adapter.requests.get", side_effect=fake_get
        ):
            r_fresh = adapter.fetch_account_readiness(force_refresh=True)
            self.assertTrue(r_fresh["live_trading_eligible"])
            self.assertFalse(r_fresh["readiness_stale"])

            # Simulate rate-limit rejection at age=45s (<=120s): flagged stale, eligible preserved, cache unmutated!
            with adapter._cache_lock:
                adapter._last_readiness_fetch = time.time() - 45.0
            with patch.object(limiter, "acquire", side_effect=RuntimeError("UpstoxRateLimitExceeded")):
                r_45 = adapter.fetch_account_readiness(force_refresh=False)
                self.assertTrue(r_45["readiness_stale"])
                self.assertEqual(r_45["stale_reason"], "RATE_LIMIT_FALLBACK")
                self.assertGreaterEqual(r_45["readiness_cache_age_sec"], 44.5)
                self.assertTrue(r_45["live_trading_eligible"])
                self.assertEqual(len(adapter._cached_readiness["blockers"]), 0)

                # Simulate rate-limit rejection at age=150s (>120s): live_trading_eligible MUST flip False!
                with adapter._cache_lock:
                    adapter._last_readiness_fetch = time.time() - 150.0
                r_150 = adapter.fetch_account_readiness(force_refresh=False)
                self.assertTrue(r_150["readiness_stale"])
                self.assertFalse(r_150["live_trading_eligible"])
                self.assertTrue(any("expired" in b.lower() for b in r_150["blockers"]))
                # Verify underlying cached dict was NOT mutated in place!
                self.assertTrue(adapter._cached_readiness["live_trading_eligible"])

    def test_07_gatekeeper_ws_stall_rest_shortcircuit_seeded_baseline_and_partial_blockers(self) -> None:
        from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

        # 1. Defect #1: During WS stall, `refresh_quotes_via_rest` leaves a cached quote with
        #    `quote_age_ms=1200 < 5000`, `depth_levels` present, NO `book_provenance`, and `l1_book_age_ms=999999`.
        #    `_resolve_exit_quote` with `sync_advance_db=True` MUST pass `max_cache_age_ms=0` to
        #    `fetch_single_quote_emergency_rest` instead of short-circuiting on `quote_age_ms < 5000`!
        sm = OptionStreamManager()
        ik_stall = "NSE_FO|STALL_WING"
        now_ms = int(time.time() * 1000)
        ws_stall_batch_quote = {
            "instrument_key": ik_stall,
            "bid": 120.00,
            "ask": 120.40,
            "raw_bid": 120.00,
            "raw_ask": 120.40,
            "ltp": 120.20,
            "depth_levels": [{"bid_p": 120.00, "bid_q": 130, "ask_p": 120.40, "ask_q": 130}],
            "timestamp_ms": now_ms - 1200,
            "quote_age_ms": 1200,
            "l1_book_timestamp_ms": 0,
            "l1_book_age_ms": 999999,
        }
        rest_called_max_age: List[int] = []
        fresh_em_rest = _make_l5_quote(
            ik_stall, "2026-12-01", 25800.0, "CE", 118.50, 118.90, age_ms=0, ts_ms=now_ms, observed=False
        )

        def spy_em_rest(ikey: str, max_cache_age_ms: int = 5000) -> Optional[Dict[str, Any]]:
            rest_called_max_age.append(max_cache_age_ms)
            if max_cache_age_ms >= 0:
                # Reproduces real `OptionStreamManager.fetch_single_quote_emergency_rest` short-circuit if >= 0 on c_age==0!
                return ws_stall_batch_quote
            return fresh_em_rest

        sc_live = S205ShadowCollector(db_path=self.db_path, sync_advance_db=True)
        with patch.object(sm, "get_option_quote", return_value=ws_stall_batch_quote), patch.object(
            sm, "fetch_single_quote_emergency_rest", side_effect=spy_em_rest
        ):
            recovered = sc_live._resolve_exit_quote(
                ik_stall, ws_stall_batch_quote, max_age_ms=5000, allow_emergency_rest=True
            )
            sw_rec = sweep_l5_book_for_qty(
                recovered, "SELL", qty=65, max_age_ms=5000, max_spread_pct=None, require_observed=False
            )
        self.assertEqual(rest_called_max_age, [-1])
        self.assertTrue(sw_rec["ok"], f"Recovered emergency REST quote must pass sweep, got {sw_rec}")

        # 2. Defect #3: `SEEDED_BASELINE`, `SYNTHETIC_LTP_FALLBACK`, unbacked `999999`, and `max_age_ms=0`
        ok_seed, _, _, rsn_seed = ShadowMirrorExecutor._extract_strict_live_bid_ask(
            {"bid": 99.5, "ask": 100.5, "ltp": 100.0, "quote_age_ms": 0, "quote_freshness": "SEEDED_BASELINE"},
            max_age_ms=5000,
        )
        self.assertFalse(ok_seed)
        self.assertEqual(rsn_seed, "SEEDED_BASELINE_REJECTED")

        ok_syn, _, _, rsn_syn = ShadowMirrorExecutor._extract_strict_live_bid_ask(
            {"bid": 99.5, "ask": 100.5, "ltp": 100.0, "quote_age_ms": 10, "book_provenance": "SYNTHETIC_LTP_FALLBACK"},
            max_age_ms=5000,
        )
        self.assertFalse(ok_syn)
        self.assertEqual(rsn_syn, "SYNTHETIC_LTP_FALLBACK_REJECTED")

        ok_unbacked, _, _, rsn_unbacked = ShadowMirrorExecutor._extract_strict_live_bid_ask(
            {"bid": 99.5, "ask": 100.5, "ltp": 100.0, "quote_age_ms": 50, "timestamp_ms": now_ms, "l1_book_age_ms": 999999, "l1_book_timestamp_ms": 0},
            max_age_ms=5000,
        )
        self.assertFalse(ok_unbacked)
        self.assertEqual(rsn_unbacked, "UNKNOWN_LIVE_QUOTE_AGE")

        ok_zero_max, _, _, rsn_zero_max = ShadowMirrorExecutor._extract_strict_live_bid_ask(
            {"bid": 99.5, "ask": 100.5, "ltp": 100.0, "quote_age_ms": 25},
            max_age_ms=0,
        )
        self.assertFalse(ok_zero_max)
        self.assertIn("STALE_LIVE_QUOTE", rsn_zero_max)

        # 3. Defect #5: Partial-probe 401 blocker BEFORE rate-limit fallback MUST flip `live_trading_eligible=False`!
        adapter = OptionLiveAdapter()
        limiter = get_upstox_limiter()
        with adapter._cache_lock:
            adapter._cached_readiness = {
                "token_valid": True,
                "nfo_enabled": True,
                "static_ip_matched": True,
                "available_margin": 85000.0,
                "live_trading_eligible": True,
                "blockers": [],
            }
            adapter._last_readiness_fetch = time.time() - 40.0
            adapter._cached_profile_ip = None
            adapter._last_profile_ip_fetch = 0.0

        acquire_calls = [0]

        def acquire_once_then_fail(**kwargs: Any) -> None:
            acquire_calls[0] += 1
            if acquire_calls[0] >= 2:
                raise RuntimeError("UpstoxRateLimitExceeded")

        resp_401 = MagicMock()
        resp_401.status_code = 401
        with patch("app.data.upstox_provider.get_upstox_config", return_value={"algo_token": "EXPIRED_TOK"}), patch.object(
            limiter, "acquire", side_effect=acquire_once_then_fail
        ), patch("app.analytics.option_sniffer.option_live_adapter.requests.get", return_value=resp_401):
            r_partial = adapter.fetch_account_readiness(force_refresh=False)
        self.assertTrue(r_partial["readiness_stale"])
        self.assertFalse(r_partial["live_trading_eligible"])
        self.assertTrue(any("401" in b for b in r_partial["blockers"]))

        # 4. Defect #4 & #6: Verify OverviewTab wires UNPRICED MTM / HALTED (UNPRICED) & PositionsTab single-leg Age
        ov_jsx_path = os.path.join(REPO_ROOT, "frontend", "src", "pages", "optionSnifferAdvance", "OverviewTab.jsx")
        pos_jsx_path = os.path.join(REPO_ROOT, "frontend", "src", "pages", "optionSnifferAdvance", "PositionsTab.jsx")
        with open(ov_jsx_path, "r", encoding="utf-8") as fh:
            ov_src = fh.read()
        with open(pos_jsx_path, "r", encoding="utf-8") as fh:
            pos_src = fh.read()
        self.assertIn("UNPRICED MTM", ov_src)
        self.assertIn("HALTED (UNPRICED)", ov_src)
        self.assertIn("Age:${formatLegAgeCompact", pos_src)


if __name__ == "__main__":
    unittest.main()
