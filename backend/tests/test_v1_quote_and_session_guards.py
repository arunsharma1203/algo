"""Regression tests for the V1 quote writer, WS hard-stop dispatch and holiday sweep."""
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
from app.analytics.option_sniffer_advance_s205 import s205_upstox_engine as engine
from app.analytics.option_sniffer_advance_s205.s205_shadow_collector import (
    S205ShadowCollector, sweep_l5_book_for_qty,
)


class TestV1QuoteAndSessionGuards(unittest.TestCase):
    def setUp(self):
        self.key = "NSE_FO|10001"
        self.sm = object.__new__(OptionStreamManager)
        self.sm._lock = threading.RLock()
        self.sm._key_to_contract = {self.key: {"underlying": "NIFTY", "option_type": "CE"}}
        self.sm._option_quotes = {}
        self.sm._underlying_quotes = {}
        self.sm._total_ticks_received = 0

    def feed(self, bid, ask, qty=65):
        self.sm.on_feed_message({self.key: {"fullFeed": {"marketFF": {
            "ltpc": {"ltp": 100},
            "marketLevel": {"bidAskQuote": [{"bidP": bid, "bidQ": qty, "askP": ask, "askQ": qty}]},
        }}}})

    def poll(self, timestamp, with_depth=True):
        payload = {"instrument_token": self.key, "last_price": 101, "timestamp": timestamp}
        if with_depth:
            payload["depth"] = {"buy": [{"price": 100, "quantity": 65}],
                                "sell": [{"price": 101, "quantity": 65}]}
        response = Mock(status_code=200)
        response.json.return_value = {"data": {self.key: payload}}
        with patch("app.data.upstox_provider.get_upstox_token", return_value="test"), \
             patch("app.data.upstox_provider.upstox_request", return_value=response), \
             patch("app.analytics.option_sniffer.option_stream_manager.get_stream_service"):
            self.sm.refresh_quotes_via_rest(force=True)
        return self.sm.get_option_quote(self.key)

    def test_poll_keeps_ws_snapshot_and_age_together(self):
        with patch.object(self.sm, "_evaluate_tick_protective_guards"):
            self.feed(98, 99)
        original = self.sm.get_option_quote(self.key)
        stamp = original["l1_book_timestamp_ms"]
        for ts, depth in [(None, False), (stamp - 500, True), (stamp, True)]:
            q = self.poll(ts, depth)
            self.assertEqual((q["bid"], q["book_provenance"], q["l1_book_timestamp_ms"]),
                             (98, "OBSERVED_V3_L5", stamp))
            self.assertEqual(q["depth_levels"][0]["bid_p"], 98)

    def test_rest_depth_requires_both_sides_and_ages_from_upstream(self):
        stamp = int(time.time() * 1000) - 300
        iso = datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()
        q = self.poll(iso)
        self.assertEqual(q["book_provenance"], "REST_BATCH_DEPTH")
        self.assertLessEqual(abs(q["l1_book_timestamp_ms"] - stamp), 1)
        self.assertTrue(sweep_l5_book_for_qty(q, "BUY", require_observed=False)["ok"])
        self.assertFalse(sweep_l5_book_for_qty(q, "BUY")["ok"])
        self.assertFalse(sweep_l5_book_for_qty(q, "BUY", max_age_ms=100, require_observed=False)["ok"])
        for ts, depth in [(None, True), (stamp, False), (stamp + 60000, True)]:
            self.sm._option_quotes.clear()
            q = self.poll(ts, depth)
            self.assertFalse(sweep_l5_book_for_qty(q, "BUY", require_observed=False)["ok"])

    def test_wide_tick_hard_stop_only_through_stream_and_collector(self):
        sc = object.__new__(S205ShadowCollector)
        sc._lock = threading.RLock()
        sc._pending_leg2 = {"pending": {}}  # must never be consumed by a wide tick
        sc._last_valid_pkg_mtm_inr = {}
        long_leg = {"instrument_key": self.key, "strike": 25000, "option_type": "CE",
                    "qty": 65, "entry_price": 100}
        pkg = {"package_id": "test", "session_date": "2026-11-25", "legs_state": {
            "mode": "SINGLE_PAID_LONG", "long_leg": long_leg, "hard_stop": 80, "ratchet_stop": 98,
        }}
        sc._open_packages = {"test": pkg}
        sc._close_single_long_package = Mock(return_value={"closed": True})
        sc._close_spread_package = Mock(return_value={"closed": True})
        orch = object.__new__(engine.S205UpstoxOrchestrator)
        orch._shadow_collector = sc
        with patch.object(engine, "get_s205_orchestrator_if_loaded", return_value=orch), \
             patch.object(engine, "OptionStreamManager", return_value=self.sm), \
             patch.object(engine, "to_ist_naive", return_value=datetime(2026, 11, 25, 10, 30)):
            self.feed(95, 101)  # Would breach only the armed ratchet.
            self.feed(65, 101, qty=0)
            self.feed(80, 79)
            sc._close_single_long_package.assert_not_called()
            self.feed(75, 101)
            self.assertFalse(self.sm.get_option_quote(self.key)["is_valid"])
            self.assertEqual(sc._close_single_long_package.call_args.args[3], "LONG_HARD_SL_HIT")
            wing_key = "NSE_FO|10002"
            wing_quote = dict(self.sm.get_option_quote(self.key), instrument_key=wing_key)
            self.sm._option_quotes[wing_key] = wing_quote
            pkg["legs_state"] = {
                "mode": "DEFINED_RISK_CREDIT_SPREAD_150",
                "short_leg": dict(long_leg, qty=-65),
                "wing_leg": dict(long_leg, instrument_key=wing_key, strike=25150),
                "hard_stop_debit_pts": 50, "target_debit_pts": 30, "ratchet_max_debit_pts": 15,
            }
            self.feed(96, 102)  # Would hit a target or ratchet in normal dispatch.
            sc._close_spread_package.assert_not_called()
            self.feed(115, 140)
            self.assertEqual(sc._close_spread_package.call_args.args[5], "SPREAD_HARD_SL_HIT")

    def test_closed_session_short_circuits_every_ingress_before_state_or_io(self):
        orch = object.__new__(engine.S205UpstoxOrchestrator)
        for day in (datetime(2026, 11, 24, 9, 33), datetime(2026, 11, 22, 9, 33)):
            with patch.object(engine, "to_ist_naive", return_value=day):
                orch.on_live_feed_events([("INDEX", "NIFTY", {"spot_ltp": 25000})])
                self.assertEqual(orch.evaluate_subsecond_tick_guard(self.key, {}), [])
                self.assertEqual(orch.run_surveillance_sweep()["status"], "MARKET_CLOSED")
        self.assertTrue(engine._is_regular_session(datetime(2026, 11, 25).date()))
        self.assertTrue(engine._is_regular_session(datetime(2026, 11, 9).date()))
        self.assertFalse(engine._is_regular_session(datetime(2026, 11, 10).date()))
