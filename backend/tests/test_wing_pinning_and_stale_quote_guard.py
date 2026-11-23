"""
Regression & Isolation Suite for Track A (P0):
  1. ATM-shift survival of Book A / Live Mirror protective wing in `OptionStreamManager` + 1m L5 retention.
  2. Exact replay of 2026-11-23 `12:41 -> 14:06` stale-wing bug (`shdw_a699620d128d`): proves zero phantom
     `SPREAD_DECAY_TARGET_70PCT` triggers under both `max_age_ms` and cross-leg timestamp skew guards.
  3. Protective exit (`EOD_SQUAREOFF_1515` & `SPOT_VWAP_FLIP_EXIT`) emergency REST quote recovery with
     exact `options_accounting` parity.
  4. `ShadowMirrorExecutor` live exit limit-collar re-anchoring via `is_emergency=True` `/market-quote/quotes`
     when cached wing quote is stale (`₹147.65` stale vs `₹118.00` live).
  5. Honest `quote_stale` / `wing_quote_age_ms` MTM telemetry & stale-quote entry fail-closed firewall.
  6. `SHADOW_PREREGISTRATION.json` SHA-256 immutability & non-destructive post-fix clean OOS epoch quarantine.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
from typing import Any, Dict, List, Optional, Tuple
import unittest

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.analytics.common.options_accounting import (
    OptionLeg,
    compute_package_accounting,
)
from app.data.database import set_test_db_override
from app.analytics.option_sniffer_advance_live.shadow_mirror_executor import (
    BOOK_ID_SPREAD_A,
    MODE_SHADOW_LIVE_BOOK_A,
    ShadowMirrorExecutor,
    compute_smart_limit_collar,
)

try:
    from app.analytics.option_sniffer_advance_s205.s205_shadow_collector import (
        CLEAN_BOOK_A_EPOCH_START_DATE,
        S205ShadowCollector,
        load_shadow_preregistration,
        sweep_l5_book_for_qty,
    )
    from app.analytics.option_sniffer_advance_s205.s205_signal_evaluator import (
        build_causal_intraday_context,
    )
except ModuleNotFoundError:
    _pkg_name = "app.analytics.option_sniffer_advance_s205"
    _pkg_dir = os.path.join(BACKEND_DIR, "app", "analytics", "option_sniffer_advance_s205")
    if _pkg_name not in sys.modules:
        _pkg_stub = types.ModuleType(_pkg_name)
        _pkg_stub.__path__ = [_pkg_dir]
        sys.modules[_pkg_name] = _pkg_stub

    def _load_submod(mod_short: str):
        full = f"{_pkg_name}.{mod_short}"
        if full in sys.modules:
            return sys.modules[full]
        spec = importlib.util.spec_from_file_location(full, os.path.join(_pkg_dir, f"{mod_short}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
        return mod

    _load_submod("s205_meta_model")
    _eval_mod = _load_submod("s205_signal_evaluator")
    _shdw_mod = _load_submod("s205_shadow_collector")
    build_causal_intraday_context = _eval_mod.build_causal_intraday_context
    S205ShadowCollector = _shdw_mod.S205ShadowCollector
    load_shadow_preregistration = _shdw_mod.load_shadow_preregistration
    sweep_l5_book_for_qty = _shdw_mod.sweep_l5_book_for_qty
    CLEAN_BOOK_A_EPOCH_START_DATE = _shdw_mod.CLEAN_BOOK_A_EPOCH_START_DATE


def _make_l5_quote(
    ikey: str,
    expiry: str,
    strike: float,
    opt_type: str,
    bid: float,
    ask: float,
    age_ms: int = 120,
    ts_ms: int = 1763880000000,
    observed: bool = True,
) -> Dict[str, Any]:
    levels = [
        {
            "bid_p": round(max(0.05, bid - i * 0.05), 2),
            "bid_q": 65,
            "ask_p": round(ask + i * 0.05),
            "ask_q": 65,
        }
        for i in range(5)
    ]
    # Keep exact top level prices
    levels[0]["bid_p"] = round(bid, 2)
    levels[0]["ask_p"] = round(ask, 2)
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
        "book_obi_l5": 0.0,
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
        "volume": 15000,
        "oi": 500000.0,
        "iv": 16.8,
        "delta": 0.35 if opt_type == "CE" else -0.35,
        "is_valid": True,
    }


def _build_bearish_session(day: str = "2026-11-23", exp: str = "2026-12-01", num_bars: int = 50):
    base_dt = datetime(2026, 11, 23, 9, 15, 0)
    spot_bars: Dict[str, Tuple[float, float, float, float, float]] = {}
    vix_bars: Dict[str, Tuple[float, float, float, float]] = {}
    for i in range(num_bars):
        ts = (base_dt + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
        drift = -1.2 * i if i < 30 else -36.0 - 4.5 * (i - 29)
        o = 25600.0 + drift
        h = o + 3.0
        l = o - 8.0
        c = o - 5.0
        spot_bars[ts] = (o, h, l, c, 1500.0 + i * 10.0)
        vix_bars[ts] = (17.8, 18.0, 17.7, 17.9)
    return build_causal_intraday_context(day, spot_bars, vix_bars, {}, {}, exp, "2026-12-08", {})


class TestWingPinningAndStaleQuoteGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "wing_guard_test.db")
        set_test_db_override(self.db_path)

    def tearDown(self) -> None:
        set_test_db_override(None)
        self._tmp.cleanup()

    def test_01_atm_shift_preserves_open_spread_wing_and_1m_l5_telemetry(self) -> None:
        if "app.data.upstox_stream_service" not in sys.modules:
            try:
                import app.data.upstox_stream_service  # noqa: F401
            except ModuleNotFoundError:
                _uss_stub = types.ModuleType("app.data.upstox_stream_service")

                class _DummyStreamer:
                    _is_connected = False
                    _lock = __import__("threading").RLock()
                    _key_to_symbol: Dict[str, str] = {}
                    _additional_keys: set = set()
                    _subscribed_ws_keys: set = set()
                    _feed_listeners: List[Any] = []

                _uss_inst = _DummyStreamer()
                _uss_stub.get_stream_service = lambda: _uss_inst  # type: ignore[attr-defined]
                sys.modules["app.data.upstox_stream_service"] = _uss_stub

        if "app.data.historical_data_layer" not in sys.modules:
            try:
                import app.data.historical_data_layer  # noqa: F401
            except ModuleNotFoundError:
                from app.data.database import get_db_path as _db_path_fn

                _hdl_stub = types.ModuleType("app.data.historical_data_layer")
                _hdl_stub.get_db_path = _db_path_fn  # type: ignore[attr-defined]
                sys.modules["app.data.historical_data_layer"] = _hdl_stub

        from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

        sm = OptionStreamManager()
        with sm._lock:
            saved_k2c = dict(sm._key_to_contract)
            saved_quotes = dict(sm._option_quotes)

        try:
            sc = S205ShadowCollector(db_path=self.db_path, sync_advance_db=False)
            # Persist an OPEN Book A spread (+25800 CE / -25650 CE) in SQLite
            pkg_row = {
                "package_id": "shdw_atmshift01",
                "advance_trade_id": "adv_shdw_atmshift01",
                "prereg_sha256": sc.prereg_sha256,
                "session_date": "2026-11-23",
                "book_id": "SHADOW_BOOK_A_SPREAD_150",
                "ui_book_type": "SHORT_BOOK_SELL",
                "state": "OPEN",
                "direction_label": "SPREAD_SELL_CE",
                "expiry": "2026-12-01",
                "entry_bar_ts": "2026-11-23 11:02:00",
                "entry_time": "2026-11-23T11:03:00",
                "leg2_confirmed_time": "2026-11-23T11:03:01",
                "legs_state": {
                    "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
                    "wing_leg": {
                        "instrument_key": "NSE_FO|47839",
                        "trading_symbol": "NIFTY 25800 CE 01 DEC 26",
                        "expiry": "2026-12-01",
                        "strike": 25800.0,
                        "option_type": "CE",
                        "qty": 65,
                        "entry_price": 159.25,
                    },
                    "short_leg": {
                        "instrument_key": "NSE_FO|47821",
                        "trading_symbol": "NIFTY 25650 CE 01 DEC 26",
                        "expiry": "2026-12-01",
                        "strike": 25650.0,
                        "option_type": "CE",
                        "qty": -65,
                        "entry_price": 215.70,
                    },
                    "net_entry_credit_pts": 56.45,
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
            sc._persist_open_package(pkg_row)
            sc._open_packages["shdw_atmshift01"] = pkg_row

            # Shift ATM down to 25350 -> candidate window is [25100..25600] (25800 CE is +450 pts away!)
            shifted_universe = [
                {
                    "instrument_key": f"NSE_FO|K_{stk}",
                    "trading_symbol": f"NIFTY {stk} CE 01 DEC 26",
                    "underlying": "NIFTY",
                    "strike": float(stk),
                    "option_type": "CE",
                    "expiry": "2026-12-01",
                    "lot_size": 65,
                }
                for stk in range(25100, 25650, 50)
            ]
            sm.sync_monitored_contracts(shifted_universe)

            with sm._lock:
                self.assertIn("NSE_FO|47839", sm._key_to_contract)
                self.assertIn("NSE_FO|47821", sm._key_to_contract)
                self.assertTrue(sm._key_to_contract["NSE_FO|47839"].get("is_active_position_leg"))

            # Verify 1m L5 chain logger retains 25800 CE even when Spot=25350 (|25800 - 25350| = 450 > 300)
            conn = sqlite3.connect(self.db_path)
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
            conn.commit()
            conn.close()

            q_wing_far = _make_l5_quote("NSE_FO|47839", "2026-12-01", 25800.0, "CE", 118.0, 118.4)
            q_other_far = _make_l5_quote("NSE_FO|OTHER_25850", "2026-12-01", 25850.0, "CE", 99.0, 99.5)
            written = sc.record_1m_chain_and_snapshot(
                bar_ts="2026-11-23 14:06:00",
                now_dt=datetime(2026, 11, 23, 14, 7, 0),
                nifty_quotes=[q_wing_far, q_other_far],
                spot_ltp=25350.0,
                fut_quote={"fut_ltp": 25356.0},
                india_vix=17.9,
                front_exp="2026-11-23",
                next_exp="2026-12-01",
                readiness_rows=[],
            )
            # 25800 CE (open wing) MUST be retained; 25850 CE (not open, 500pt away) MUST be filtered out!
            self.assertEqual(written, 1)
        finally:
            with sm._lock:
                sm._key_to_contract = saved_k2c
                sm._option_quotes = saved_quotes

    def test_02_replay_2026_11_23_stale_wing_never_triggers_false_70pct_target(self) -> None:
        sc = S205ShadowCollector(db_path=self.db_path, sync_advance_db=False)
        t_1241 = 1763881860000
        t_1406 = t_1241 + (85 * 60 * 1000)

        pkg = {
            "package_id": "shdw_a699620d128d",
            "advance_trade_id": "adv_shdw_99620d128d",
            "prereg_sha256": sc.prereg_sha256,
            "session_date": "2026-11-23",
            "book_id": "SHADOW_BOOK_A_SPREAD_150",
            "ui_book_type": "SHORT_BOOK_SELL",
            "state": "OPEN",
            "direction_label": "SPREAD_SELL_CE",
            "expiry": "2026-12-01",
            "entry_bar_ts": "2026-11-23 11:02:00",
            "entry_time": "2026-11-23T11:03:00",
            "leg2_confirmed_time": "2026-11-23T11:03:01",
            "legs_state": {
                "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
                "wing_leg": {
                    "instrument_key": "NSE_FO|47839",
                    "trading_symbol": "NIFTY 25800 CE 01 DEC 26",
                    "expiry": "2026-12-01",
                    "strike": 25800.0,
                    "option_type": "CE",
                    "qty": 65,
                    "entry_price": 159.25,
                },
                "short_leg": {
                    "instrument_key": "NSE_FO|47821",
                    "trading_symbol": "NIFTY 25650 CE 01 DEC 26",
                    "expiry": "2026-12-01",
                    "strike": 25650.0,
                    "option_type": "CE",
                    "qty": -65,
                    "entry_price": 215.70,
                },
                "net_entry_credit_pts": 56.45,
                "best_close_credit_decay_pts": 53.90,
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

        # Case A: Frozen 12:41 wing quote (age=5,100,000 ms, bid=147.65) + live 14:06 short quote (ask=162.25)
        q_wing_frozen = _make_l5_quote("NSE_FO|47839", "2026-12-01", 25800.0, "CE", 147.65, 148.00, age_ms=5100000, ts_ms=t_1241)
        q_short_1406 = _make_l5_quote("NSE_FO|47821", "2026-12-01", 25650.0, "CE", 161.90, 162.25, age_ms=110, ts_ms=t_1406)

        # Direct sweep with require_observed=False MUST now reject stale quote (`max_age_ms=5000`)
        sw_stale = sweep_l5_book_for_qty(q_wing_frozen, "SELL", qty=65, max_age_ms=5000, max_spread_pct=None, require_observed=False)
        self.assertFalse(sw_stale["ok"])
        self.assertEqual(sw_stale["reason"], "STALE_L1_BOOK")

        closed = sc.on_observed_tick(
            "NSE_FO|47821",
            q_short_1406,
            quote_lookup_by_ikey=lambda k: q_wing_frozen if k == "NSE_FO|47839" else q_short_1406,
            now_dt=datetime(2026, 11, 23, 14, 6, 54),
        )
        self.assertEqual(len(closed), 0, "Stale wing quote must NEVER trigger SPREAD_DECAY_TARGET_70PCT!")
        self.assertIn("shdw_a699620d128d", sc._open_packages)

        # Case B: Even if age_ms were masked to 0, cross-leg timestamp skew (85 mins > 5000ms) MUST still block TP!
        q_wing_masked_age = dict(q_wing_frozen, quote_age_ms=0, l1_book_age_ms=0)
        closed_skew = sc.on_observed_tick(
            "NSE_FO|47821",
            q_short_1406,
            quote_lookup_by_ikey=lambda k: q_wing_masked_age if k == "NSE_FO|47839" else q_short_1406,
            now_dt=datetime(2026, 11, 23, 14, 6, 54),
        )
        self.assertEqual(len(closed_skew), 0, "Cross-leg timestamp skew > 5s must block profit target!")

    def test_03_protective_eod_exit_recovers_stale_wing_via_emergency_rest(self) -> None:
        t_1515 = 1763891100000
        q_wing_rest = _make_l5_quote(
            "NSE_FO|47839", "2026-12-01", 25800.0, "CE", 118.00, 118.40, age_ms=25, ts_ms=t_1515, observed=False
        )
        rest_calls: List[str] = []

        def mock_em_fetch(ik: str) -> Optional[Dict[str, Any]]:
            rest_calls.append(ik)
            return q_wing_rest if ik == "NSE_FO|47839" else None

        sc = S205ShadowCollector(db_path=self.db_path, sync_advance_db=False, emergency_quote_fetcher=mock_em_fetch)
        pkg = {
            "package_id": "shdw_eodrecover01",
            "advance_trade_id": "adv_shdw_eodrec01",
            "prereg_sha256": sc.prereg_sha256,
            "session_date": "2026-11-23",
            "book_id": "SHADOW_BOOK_A_SPREAD_150",
            "ui_book_type": "SHORT_BOOK_SELL",
            "state": "OPEN",
            "direction_label": "SPREAD_SELL_CE",
            "expiry": "2026-12-01",
            "entry_bar_ts": "2026-11-23 11:02:00",
            "entry_time": "2026-11-23T11:03:00",
            "leg2_confirmed_time": "2026-11-23T11:03:01",
            "legs_state": {
                "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
                "wing_leg": {
                    "instrument_key": "NSE_FO|47839",
                    "trading_symbol": "NIFTY 25800 CE 01 DEC 26",
                    "expiry": "2026-12-01",
                    "strike": 25800.0,
                    "option_type": "CE",
                    "qty": 65,
                    "entry_price": 159.25,
                },
                "short_leg": {
                    "instrument_key": "NSE_FO|47821",
                    "trading_symbol": "NIFTY 25650 CE 01 DEC 26",
                    "expiry": "2026-12-01",
                    "strike": 25650.0,
                    "option_type": "CE",
                    "qty": -65,
                    "entry_price": 215.70,
                },
                "net_entry_credit_pts": 56.45,
                "best_close_credit_decay_pts": 53.90,
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

        q_wing_stale = _make_l5_quote("NSE_FO|47839", "2026-12-01", 25800.0, "CE", 147.65, 148.00, age_ms=5100000, ts_ms=t_1515 - 5100000)
        q_short_eod = _make_l5_quote("NSE_FO|47821", "2026-12-01", 25650.0, "CE", 162.00, 162.25, age_ms=90, ts_ms=t_1515)

        closed = sc.on_observed_tick(
            "NSE_FO|47821",
            q_short_eod,
            quote_lookup_by_ikey=lambda k: q_wing_stale if k == "NSE_FO|47839" else q_short_eod,
            now_dt=datetime(2026, 11, 23, 15, 15, 1),
        )
        self.assertEqual(len(closed), 1)
        self.assertIn("NSE_FO|47839", rest_calls)
        self.assertEqual(closed[0]["exit_reason"], "EOD_SQUAREOFF_1515")

        # Must price wing exit off live ₹118.00 (-1 adverse tick = ₹117.95), NEVER stale ₹147.65!
        expected_acc = compute_package_accounting(
            legs=[
                OptionLeg("CE", 25800.0, 65, 159.25, 117.95, "MARKET_ORDER"),
                OptionLeg("CE", 25650.0, -65, 215.70, 162.30, "MARKET_ORDER"),
            ],
            trade_date="2026-11-23",
        )
        self.assertEqual(closed[0]["net_pnl_inr"], expected_acc["net_pnl"])

    def test_04_shadow_mirror_live_exit_reanchors_stale_wing_collar_via_emergency_rest(self) -> None:
        placed_orders: List[Dict[str, Any]] = []
        emergency_quote_queries: List[Tuple[str, bool]] = []
        oid_seq = [100]
        fills_by_oid: Dict[str, Tuple[int, float]] = {}

        def fake_broker_api(
            method: str,
            endpoint: str,
            payload: Optional[Dict[str, Any]] = None,
            params: Optional[Dict[str, Any]] = None,
            is_emergency: bool = False,
        ) -> Tuple[bool, int, Dict[str, Any]]:
            if method == "GET" and endpoint == "/market-quote/quotes":
                ik = str((params or {}).get("instrument_key") or "")
                emergency_quote_queries.append((ik, bool(is_emergency)))
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
            return True, 200, {"status": "success", "data": []}

        # Initial fresh quotes on entry; then wing quote freezes at 147.65 (quote_age_ms=90000) before exit!
        quote_state: Dict[str, Dict[str, Any]] = {
            "NSE_FO|47839": {"bid": 154.80, "ask": 155.00, "ltp": 154.90, "quote_age_ms": 80},
            "NSE_FO|47821": {"bid": 209.70, "ask": 210.10, "ltp": 209.90, "quote_age_ms": 80},
        }

        ex = ShadowMirrorExecutor(
            db_path=self.db_path,
            bridge_state_fn=lambda *_: {
                "is_live_armed": True,
                "execution_mode": MODE_SHADOW_LIVE_BOOK_A,
                "armed_for_date": "2026-11-23",
                "armed_at_iso": "2026-11-23 09:45:00",
                "is_session_halted": False,
                "daily_total_exposure_pnl_inr": 0.0,
                "max_daily_loss_inr": 3500.0,
            },
            broker_api_fn=fake_broker_api,
            quote_lookup_fn=lambda k: quote_state.get(k),
            readiness_fn=lambda: {"live_trading_eligible": True, "available_margin": 85448.27},
            poll_interval_sec=0.001,
            auto_start_worker=False,
        )

        open_snap = {
            "package_id": "shdw_livewing01",
            "advance_trade_id": "adv_shdw_livewing01",
            "session_date": "2026-11-23",
            "book_id": BOOK_ID_SPREAD_A,
            "state": "OPEN",
            "direction_label": "SPREAD_SELL_CE",
            "expiry": "2026-12-01",
            "entry_bar_ts": "2026-11-23 11:02:00",
            "entry_time": "2026-11-23T11:03:01",
            "upstox_final_margin_inr": 39676.26,
            "legs_state": {
                "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
                "wing_leg": {
                    "instrument_key": "NSE_FO|47839",
                    "trading_symbol": "NIFTY 25800 CE 01 DEC 26",
                    "expiry": "2026-12-01",
                    "strike": 25800.0,
                    "option_type": "CE",
                    "qty": 65,
                    "entry_price": 155.00,
                },
                "short_leg": {
                    "instrument_key": "NSE_FO|47821",
                    "trading_symbol": "NIFTY 25650 CE 01 DEC 26",
                    "expiry": "2026-12-01",
                    "strike": 25650.0,
                    "option_type": "CE",
                    "qty": -65,
                    "entry_price": 209.70,
                },
                "net_entry_credit_pts": 54.70,
                "hard_stop_debit_pts": 90.26,
                "target_debit_pts": 16.41,
                "is_bull": False,
            },
        }
        res_open = ex.process_shadow_event_sync(
            "BOOK_A_OPEN", open_snap, now_dt=datetime(2026, 11, 23, 11, 3, 2)
        )
        self.assertEqual(res_open["state"], "OPEN_HEDGED")

        # Simulate stale cached wing quote (90s old at ₹147.65) while short quote is fresh (80ms at ₹162.25)
        quote_state["NSE_FO|47839"] = {"bid": 147.65, "ask": 148.00, "ltp": 147.80, "quote_age_ms": 90000}
        quote_state["NSE_FO|47821"] = {"bid": 161.90, "ask": 162.25, "ltp": 162.10, "quote_age_ms": 80}

        # Verify MTM accessor flags quote_stale=True with exact leg ages
        active_pos = ex.get_active_live_positions_for_bridge()
        self.assertEqual(len(active_pos), 1)
        self.assertTrue(active_pos[0]["quote_stale"])
        self.assertEqual(active_pos[0]["wing_quote_age_ms"], 90000)
        self.assertEqual(active_pos[0]["short_quote_age_ms"], 80)

        # Now close: Step 1 buys short (NSE_FO|47821) FIRST, Step 2 fetches emergency REST quote (₹118.00) & sells wing!
        close_snap = {**open_snap, "state": "CLOSED", "exit_reason": "EOD_SQUAREOFF_1515", "net_pnl_inr": 900.0}
        res_close = ex.process_shadow_event_sync(
            "BOOK_A_CLOSE", close_snap, now_dt=datetime(2026, 11, 23, 15, 15, 1)
        )
        self.assertEqual(res_close["state"], "CONFIRMED_FLAT")
        self.assertEqual(emergency_quote_queries, [("NSE_FO|47839", True)])
        self.assertEqual(placed_orders[-2]["instrument_token"], "NSE_FO|47821")
        self.assertEqual(placed_orders[-2]["transaction_type"], "BUY")
        self.assertEqual(placed_orders[-1]["instrument_token"], "NSE_FO|47839")
        self.assertEqual(placed_orders[-1]["transaction_type"], "SELL")
        expected_wing_lim = compute_smart_limit_collar(118.00, "SELL", stage=1)
        self.assertEqual(placed_orders[-1]["price"], expected_wing_lim)
        self.assertLess(placed_orders[-1]["price"], 120.0)  # Never anchored near stale ₹147.65!

    def test_05_stale_quote_blocks_live_mirror_entry_fail_closed(self) -> None:
        mutations: List[Any] = []
        ex = ShadowMirrorExecutor(
            db_path=self.db_path,
            bridge_state_fn=lambda *_: {
                "is_live_armed": True,
                "execution_mode": MODE_SHADOW_LIVE_BOOK_A,
                "armed_for_date": "2026-11-23",
                "armed_at_iso": "2026-11-23 09:45:00",
                "is_session_halted": False,
                "daily_total_exposure_pnl_inr": 0.0,
                "max_daily_loss_inr": 3500.0,
            },
            broker_api_fn=lambda m, e, **kw: (mutations.append((m, e)) or (True, 200, {})),
            quote_lookup_fn=lambda k: {"bid": 150.0, "ask": 150.5, "ltp": 150.2, "quote_age_ms": 12000},
            readiness_fn=lambda: {"live_trading_eligible": True, "available_margin": 85448.27},
            auto_start_worker=False,
        )
        snap = {
            "package_id": "shdw_staleentry01",
            "advance_trade_id": "adv_shdw_staleentry01",
            "session_date": "2026-11-23",
            "book_id": BOOK_ID_SPREAD_A,
            "state": "OPEN",
            "direction_label": "SPREAD_SELL_CE",
            "expiry": "2026-12-01",
            "entry_bar_ts": "2026-11-23 11:02:00",
            "entry_time": "2026-11-23T11:03:01",
            "upstox_final_margin_inr": 39676.26,
            "legs_state": {
                "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
                "wing_leg": {"instrument_key": "NSE_FO|47839", "trading_symbol": "W", "expiry": "2026-12-01", "strike": 25800.0, "option_type": "CE", "qty": 65, "entry_price": 150.5},
                "short_leg": {"instrument_key": "NSE_FO|47821", "trading_symbol": "S", "expiry": "2026-12-01", "strike": 25650.0, "option_type": "CE", "qty": -65, "entry_price": 205.0},
                "net_entry_credit_pts": 54.5,
                "hard_stop_debit_pts": 89.9,
                "target_debit_pts": 16.35,
                "is_bull": False,
            },
        }
        res = ex.process_shadow_event_sync(
            "BOOK_A_OPEN", snap, now_dt=datetime(2026, 11, 23, 11, 3, 2)
        )
        self.assertIn(res["state"], ("PREFLIGHT_BLOCKED", "ABORTED_CLEAN"))
        self.assertIn("STALE_LIVE_QUOTE", str(res.get("error_reason") or ""))
        self.assertEqual(ex.mutation_call_count, 0)
        self.assertEqual(len(mutations), 0)

    def test_06_preregistration_hash_lock_and_clean_post_fix_epoch_quarantine(self) -> None:
        _, sha = load_shadow_preregistration()
        self.assertEqual(sha, "c11b7d5f41223f8c9425fe8d1e680151a0235032aab4dc78a4d39e417f81b56e")
        self.assertEqual(CLEAN_BOOK_A_EPOCH_START_DATE, "2026-11-24")

        sc = S205ShadowCollector(db_path=self.db_path, sync_advance_db=False)
        base_row = {
            "prereg_sha256": sha,
            "ui_book_type": "SHORT_BOOK_SELL",
            "state": "CLOSED",
            "direction_label": "SPREAD_SELL_PE",
            "expiry": "2026-11-23",
            "entry_bar_ts": "2026-11-20 09:45:00",
            "entry_time": "2026-11-20T09:46:00",
            "leg2_confirmed_time": "2026-11-20T09:46:01",
            "exit_bar_ts": "2026-11-20 11:53:00",
            "exit_time": "2026-11-20T11:53:58",
            "legs_state": {"mode": "DEFINED_RISK_CREDIT_SPREAD_150"},
            "accounting": {},
            "structural_max_loss_inr": 6000.0,
            "upstox_final_margin_inr": 39000.0,
            "margin_status": "UPSTOX_V2_CHARGES_MARGIN_VERIFIED",
            "cash_reserved_inr": 39000.0,
            "gross_pnl_inr": 2164.50,
            "total_charges_inr": 115.45,
            "stt_inr": 18.0,
            "brokerage_inr": 80.0,
            "net_pnl_inr": 2049.05,
            "exit_reason": "SPREAD_DECAY_TARGET_70PCT",
            "regime_context": {"shadow_only": True},
        }
        # 1. Pre-fix Book A row (2026-11-20 -> quarantined)
        sc._persist_closed_or_unwound_package({**base_row, "package_id": "shdw_pre_a", "advance_trade_id": "adv_1", "session_date": "2026-11-20", "book_id": "SHADOW_BOOK_A_SPREAD_150", "net_pnl_inr": 2049.05})
        # 2. Pre-fix Book B row (2026-11-20 -> clean, single-leg unaffected)
        sc._persist_closed_or_unwound_package({**base_row, "package_id": "shdw_pre_b", "advance_trade_id": "adv_2", "session_date": "2026-11-20", "book_id": "SHADOW_BOOK_B_PAID_FLOW", "ui_book_type": "LONG_BOOK_BUY", "net_pnl_inr": 715.93})
        # 3. Post-fix Book A row (2026-11-24 -> clean)
        sc._persist_closed_or_unwound_package({**base_row, "package_id": "shdw_post_a", "advance_trade_id": "adv_3", "session_date": "2026-11-24", "book_id": "SHADOW_BOOK_A_SPREAD_150", "net_pnl_inr": 850.00})

        summary = sc.get_summary()
        # Lifetime rows preserved immutably (3 total)
        self.assertEqual(summary["certification_progress"]["closed_shadow_packages"], 3)
        self.assertFalse(summary["certification_progress"]["ready_for_go_no_go_review"])
        audit = summary["clean_post_fix_audit"]
        self.assertEqual(audit["quarantined_pre_fix_book_a_packages"], 1)
        self.assertAlmostEqual(audit["quarantined_pre_fix_book_a_net_pnl_inr"], 2049.05, places=2)
        self.assertEqual(audit["clean_closed_shadow_packages"], 2)
        self.assertEqual(audit["clean_book_a_closed_packages"], 1)
        self.assertAlmostEqual(audit["clean_book_a_realized_net_pnl_inr"], 850.00, places=2)
        self.assertAlmostEqual(audit["clean_realized_net_pnl_inr"], 1565.93, places=2)


if __name__ == "__main__":
    unittest.main()
