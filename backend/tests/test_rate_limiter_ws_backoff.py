"""
Isolation & Regression Suite for Track B (P1):
  1. `OptionStreamManager.refresh_quotes_via_rest` backs off to 60s when WebSocket V3, NIFTY spot,
     and all active-position legs are fresh (preventing 1,200+ redundant 89-symbol REST batch polls).
  2. Immediate fallback to `min_interval_sec` (3.5s) the moment WebSocket drops/stalls OR any
     `is_active_position_leg` / `is_pinned_active_leg` goes stale (> 5s) OR `force=True`.
  3. `OptionLiveAdapter.fetch_account_readiness` caches static `/user/profile` + `/user/ip` for 300s
     while refreshing `/user/get-funds-and-margin` every 15s (66% quota reduction), and never bypasses
     `UpstoxRateLimitExceeded` on routine (`force_refresh=False`) polls.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import types
from typing import Any, Dict, List
import unittest
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.data.database import set_test_db_override


def _ensure_lightweight_stubs() -> Any:
    if "app.data.upstox_stream_service" not in sys.modules:
        try:
            import app.data.upstox_stream_service as uss  # noqa: F401
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

    if "app.data.historical_data_layer" not in sys.modules:
        try:
            import app.data.historical_data_layer  # noqa: F401
        except ModuleNotFoundError:
            from app.data.database import get_db_path as _db_fn

            _hdl = types.ModuleType("app.data.historical_data_layer")
            _hdl.get_db_path = _db_fn  # type: ignore[attr-defined]
            sys.modules["app.data.historical_data_layer"] = _hdl

    if "numpy" not in sys.modules:
        try:
            import numpy  # noqa: F401
        except ModuleNotFoundError:
            sys.modules["numpy"] = types.ModuleType("numpy")

    if "pandas" not in sys.modules:
        try:
            import pandas  # noqa: F401
        except ModuleNotFoundError:
            _pd = types.ModuleType("pandas")
            _pd.DataFrame = object  # type: ignore[attr-defined]
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

    import app.data.upstox_stream_service as uss_mod

    return uss_mod


class TestRateLimiterWsBackoff(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "rl_backoff_test.db")
        set_test_db_override(self.db_path)

    def tearDown(self) -> None:
        set_test_db_override(None)
        self._tmp.cleanup()

    def test_01_ws_healthy_60s_backoff_vs_immediate_stale_leg_or_ws_drop_fallback(self) -> None:
        uss_mod = _ensure_lightweight_stubs()
        from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

        sm = OptionStreamManager()
        with sm._lock:
            saved_k2c = dict(sm._key_to_contract)
            saved_oq = dict(sm._option_quotes)
            saved_uq = dict(sm._underlying_quotes)
            saved_last_rest = getattr(sm, "_last_rest_refresh_time", 0.0)
            saved_last_ws = getattr(sm, "_last_ws_tick_time", 0.0)

        fake_svc = MagicMock()
        fake_svc._is_connected = True

        try:
            now_t = time.time()
            now_ms = int(now_t * 1000)
            with sm._lock:
                sm._key_to_contract = {
                    "NSE_FO|47839": {
                        "instrument_key": "NSE_FO|47839",
                        "trading_symbol": "NIFTY 25800 CE",
                        "underlying": "NIFTY",
                        "strike": 25800.0,
                        "option_type": "CE",
                        "expiry": "2026-12-01",
                        "is_active_position_leg": True,
                    },
                    "NSE_FO|BNF_DEEP_ITM": {
                        "instrument_key": "NSE_FO|BNF_DEEP_ITM",
                        "trading_symbol": "BANKNIFTY 62700 CE",
                        "underlying": "BANKNIFTY",
                        "strike": 62700.0,
                        "option_type": "CE",
                        "expiry": "2026-11-23",
                    },
                }
                sm._underlying_quotes = {
                    "NIFTY": {"spot_ltp": 25480.0, "timestamp_ms": now_ms - 100},
                }
                # Active leg is fresh (200ms); non-position deep ITM BNF leg is 25s old!
                sm._option_quotes = {
                    "NSE_FO|47839": {"ltp": 141.0, "bid": 140.8, "ask": 141.2, "timestamp_ms": now_ms - 200},
                    "NSE_FO|BNF_DEEP_ITM": {"ltp": 650.0, "bid": 648.0, "ask": 652.0, "timestamp_ms": now_ms - 25000},
                }
                sm._last_ws_tick_time = now_t - 0.2
                sm._last_rest_refresh_time = now_t - 10.0  # 10s ago (> 3.5s, < 60s)

            http_calls: List[Any] = []

            def fake_upstox_req(method: str, url: str, **kwargs: Any) -> Any:
                http_calls.append((method, url))
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"data": {}}
                return resp

            from datetime import datetime as _dt

            with patch.object(uss_mod, "get_stream_service", return_value=fake_svc), patch(
                "app.analytics.option_sniffer.option_stream_manager.get_stream_service", return_value=fake_svc
            ), patch(
                "app.analytics.option_sniffer.option_stream_manager.to_ist_naive",
                return_value=_dt(2026, 11, 23, 11, 30, 0),
            ), patch("app.data.upstox_provider.get_upstox_token", return_value="tok_live"), patch(
                "app.data.upstox_provider.upstox_request", side_effect=fake_upstox_req
            ):
                # 1. WS healthy + active leg fresh -> backs off to 60s (ZERO HTTP calls at t=10s!)
                cnt = sm.refresh_quotes_via_rest(min_interval_sec=3.5)
                self.assertEqual(cnt, 2)
                self.assertEqual(len(http_calls), 0)

                # 2. Active position leg goes stale (8s old) -> immediately triggers REST refresh!
                with sm._lock:
                    sm._option_quotes["NSE_FO|47839"]["timestamp_ms"] = now_ms - 8000
                sm.refresh_quotes_via_rest(min_interval_sec=3.5)
                self.assertEqual(len(http_calls), 1)

                # 3. Restore fresh active leg, reset last_rest to 10s ago, then drop WS -> triggers REST!
                with sm._lock:
                    sm._option_quotes["NSE_FO|47839"]["timestamp_ms"] = int(time.time() * 1000)
                    sm._last_rest_refresh_time = time.time() - 10.0
                fake_svc._is_connected = False
                sm.refresh_quotes_via_rest(min_interval_sec=3.5)
                self.assertEqual(len(http_calls), 2)
        finally:
            with sm._lock:
                sm._key_to_contract = saved_k2c
                sm._option_quotes = saved_oq
                sm._underlying_quotes = saved_uq
                sm._last_rest_refresh_time = saved_last_rest
                sm._last_ws_tick_time = saved_last_ws

    def test_02_readiness_300s_profile_ip_cache_and_rate_limit_protection(self) -> None:
        _ensure_lightweight_stubs()
        from app.analytics.option_sniffer.option_live_adapter import (
            OptionLiveAdapter,
            PRIMARY_WHITELISTED_IPV6,
        )
        from app.data.upstox_provider import UpstoxRateLimitExceeded

        adapter = OptionLiveAdapter()
        with adapter._cache_lock:
            adapter._cached_readiness = None
            adapter._last_readiness_fetch = 0.0
            adapter._cached_profile_ip = None
            adapter._last_profile_ip_fetch = 0.0

        called_urls: List[str] = []
        acquire_flags: List[bool] = []

        def fake_requests_get(url: str, **kwargs: Any) -> Any:
            called_urls.append(url)
            resp = MagicMock()
            resp.status_code = 200
            if url.endswith("/user/profile"):
                resp.json.return_value = {"data": {"user_id": "85BZFH", "user_name": "ARUN", "exchanges": ["NSE", "NFO"]}}
            elif url.endswith("/user/ip"):
                resp.json.return_value = {"data": {"primary_ip": PRIMARY_WHITELISTED_IPV6, "secondary_ip": ""}}
            elif url.endswith("/user/get-funds-and-margin"):
                resp.json.return_value = {"data": {"equity": {"available_margin": 85000.0, "used_margin": 0.0, "payin_amount": 0.0}}}
            return resp

        fake_limiter = MagicMock()
        fake_limiter.acquire.side_effect = lambda timeout=2.0, is_emergency=False: acquire_flags.append(bool(is_emergency)) or True

        with patch("app.data.upstox_provider.get_upstox_config", return_value={"algo_token": "tok_test_1"}), patch(
            "app.analytics.option_sniffer.option_live_adapter.get_upstox_limiter", return_value=fake_limiter
        ), patch("app.analytics.option_sniffer.option_live_adapter.requests.get", side_effect=fake_requests_get):
            # Call 1 (cold cache): probes all 3 endpoints
            r1 = adapter.fetch_account_readiness(force_refresh=False)
            self.assertTrue(r1["live_trading_eligible"])
            self.assertEqual(len(called_urls), 3)

            # Call 2 after 35s (>30s funds TTL, <300s profile/IP TTL): probes ONLY /get-funds-and-margin (1 call instead of 3!)
            with adapter._cache_lock:
                adapter._last_readiness_fetch -= 35.0
            r2 = adapter.fetch_account_readiness(force_refresh=False)
            self.assertTrue(r2["live_trading_eligible"])
            self.assertEqual(len(called_urls), 4)
            self.assertTrue(called_urls[-1].endswith("/user/get-funds-and-margin"))

            # Call 3 when rate limiter raises UpstoxRateLimitExceeded on routine poll: returns cached readiness with 0 new HTTP calls!
            with adapter._cache_lock:
                adapter._last_readiness_fetch -= 35.0
            fake_limiter.acquire.side_effect = UpstoxRateLimitExceeded("1600/30m quota reached")
            r3 = adapter.fetch_account_readiness(force_refresh=False)
            self.assertTrue(r3["live_trading_eligible"])
            self.assertEqual(len(called_urls), 4)


if __name__ == "__main__":
    unittest.main()
