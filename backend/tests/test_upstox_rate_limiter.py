import unittest
import time
import threading
from unittest.mock import patch, MagicMock
from datetime import datetime

from app.data.upstox_provider import (
    UpstoxRateLimiter,
    UpstoxRateLimitExceeded,
    get_upstox_limiter,
    upstox_request,
    fetch_upstox_bulk_ltp,
    fetch_upstox_ltp,
    _ltp_cache
)
import app.data.upstox_provider as upstox_mod

class TestUpstoxRateLimiter(unittest.TestCase):
    def setUp(self):
        # Fresh isolated rate limiter for each test
        self.limiter = UpstoxRateLimiter()
        self.limiter._circuit_open_until = 0.0

    def tearDown(self):
        get_upstox_limiter().reset_circuit_breaker()

    def test_sliding_window_eviction(self):
        """Old timestamps outside 30 minutes are purged automatically."""
        now = time.time()
        # Insert artificial timestamps 35 minutes ago
        self.limiter._history.extend([now - 2100, now - 2000, now - 1900])
        self.assertEqual(len(self.limiter._history), 3)

        # Acquire a new slot
        self.limiter.acquire()

        # The 3 expired timestamps should have been purged, leaving only the new one
        self.assertEqual(len(self.limiter._history), 1)

    def test_second_rate_limiting_pacing(self):
        """When 1-second limit is hit, limiter sleeps and paces requests smoothly."""
        now = time.time()
        # Fill the 1-second bucket with 8 requests (the safety cap)
        for _ in range(self.limiter.MAX_PER_SECOND):
            self.limiter._history.append(now - 0.2)

        start = time.time()
        # Next request should be paced
        self.limiter.acquire(timeout=2.0)
        elapsed = time.time() - start

        # Must have slept until the 1-second window cleared
        self.assertGreaterEqual(elapsed, 0.7)
        self.assertGreaterEqual(self.limiter._throttled_count, 1)

    def test_inter_request_pacing_delay(self):
        """Sequential requests must be paced with at least MIN_INTERVAL_SEC delay."""
        limiter = UpstoxRateLimiter()
        limiter._circuit_open_until = 0.0
        t0 = time.time()
        limiter.acquire(timeout=2.0)
        t1 = time.time()
        limiter.acquire(timeout=2.0)
        t2 = time.time()
        self.assertGreaterEqual(t2 - t1, limiter.MIN_INTERVAL_SEC - 0.02)

    def test_circuit_breaker_fast_reject_on_429(self):
        """When circuit breaker trips, acquire() fast-rejects without network calls."""
        limiter = UpstoxRateLimiter()
        limiter.trip_circuit_breaker(duration=60.0, reason="HTTP 429 received in test")
        self.assertTrue(limiter.is_circuit_open())
        self.assertGreater(limiter.get_circuit_remaining_seconds(), 50)

        with self.assertRaises(UpstoxRateLimitExceeded) as ctx:
            limiter.acquire(timeout=1.0)
        self.assertIn("Circuit Breaker is OPEN", str(ctx.exception))

    def test_30min_hard_ceiling_raises_exception(self):
        """When 30-minute limit is reached (1,600), raises UpstoxRateLimitExceeded immediately."""
        now = time.time()
        # Fill the 30-minute bucket to MAX_PER_30MIN
        self.limiter._history.extend([now - 100] * self.limiter.MAX_PER_30MIN)

        with self.assertRaises(UpstoxRateLimitExceeded) as ctx:
            self.limiter.acquire(timeout=5.0)

        self.assertIn("30-minute API limit reached", str(ctx.exception))
        self.assertEqual(self.limiter._rejected_count, 1)

    def test_timeout_abort_when_wait_too_long(self):
        """If required sleep exceeds specified timeout, limiter aborts with UpstoxRateLimitExceeded."""
        now = time.time()
        # Fill 1-minute bucket to MAX_PER_MINUTE
        self.limiter._history.extend([now - 10] * self.limiter.MAX_PER_MINUTE)

        # Asking with short 0.1s timeout when ~50s sleep is required
        with self.assertRaises(UpstoxRateLimitExceeded) as ctx:
            self.limiter.acquire(timeout=0.1)

        self.assertIn("timeout", str(ctx.exception).lower())

    def test_telemetry_stats_reporting(self):
        """get_stats accurately reports sliding-window counts and thresholds."""
        stats = self.limiter.get_stats()
        self.assertEqual(stats["status"], "NORMAL")
        self.assertEqual(stats["max_1s"], self.limiter.MAX_PER_SECOND)
        self.assertEqual(stats["max_1m"], self.limiter.MAX_PER_MINUTE)
        self.assertEqual(stats["max_30m"], self.limiter.MAX_PER_30MIN)

        now = time.time()
        # Add some events
        self.limiter._history.extend([now - 0.5, now - 0.2])
        stats = self.limiter.get_stats()
        self.assertEqual(stats["current_1s"], 2)
        self.assertEqual(stats["current_1m"], 2)
        self.assertEqual(stats["current_30m"], 2)

    def test_thread_safety_under_concurrency(self):
        """Concurrent threads acquire slots safely without race conditions."""
        limiter = UpstoxRateLimiter()
        limiter._circuit_open_until = 0.0
        acquired = []

        def worker():
            for _ in range(3):
                limiter.acquire(timeout=5.0)
                acquired.append(1)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(acquired), 9)
        self.assertEqual(len(limiter._history), 9)

    @patch("app.data.upstox_provider.requests.get")
    @patch("app.data.upstox_provider.notify_upstox_auth_alert")
    def test_upstox_request_triggers_429_alert(self, mock_alert, mock_get):
        """HTTP 429 response trips circuit breaker and raises UpstoxRateLimitExceeded without false auth alerts."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_get.return_value = mock_resp

        # Ensure circuit breaker starts clear
        get_upstox_limiter()._circuit_open_until = 0.0

        with self.assertRaises(UpstoxRateLimitExceeded):
            upstox_request("GET", "https://api.upstox.com/v2/market-quote/ltp")
        # Critical fix: 429 must NOT spam user telling them to revoke/regenerate token!
        self.assertFalse(mock_alert.called)
        self.assertTrue(get_upstox_limiter().is_circuit_open())

    @patch("app.data.upstox_provider.get_upstox_token", return_value="valid_mock_token_1234567890")
    @patch("app.data.upstox_provider.requests.get")
    def test_bulk_ltp_caching_and_batching(self, mock_get, mock_token):
        """fetch_upstox_bulk_ltp uses 2s cache and returns quotes efficiently."""
        _ltp_cache.clear()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "success",
            "data": {
                "NSE_EQ:RELIANCE": {"last_price": 2950.0, "instrument_token": "NSE_EQ|INE002A01018"},
                "NSE_EQ:TCS": {"last_price": 4200.0, "instrument_token": "NSE_EQ|INE467B01029"}
            }
        }
        mock_get.return_value = mock_resp

        # First call hits network
        quotes = fetch_upstox_bulk_ltp(["RELIANCE", "TCS"])
        self.assertEqual(quotes.get("RELIANCE"), 2950.0)
        self.assertEqual(quotes.get("TCS"), 4200.0)
        self.assertEqual(mock_get.call_count, 1)

        # Second immediate call within 2 seconds MUST hit in-memory cache with 0 network calls!
        cached_quotes = fetch_upstox_bulk_ltp(["RELIANCE", "TCS"])
        self.assertEqual(cached_quotes.get("RELIANCE"), 2950.0)
        self.assertEqual(mock_get.call_count, 1)  # Still 1, no new network call!

        # Single fetch_upstox_ltp also uses the cache
        single_price = fetch_upstox_ltp("RELIANCE")
        self.assertEqual(single_price, 2950.0)
        self.assertEqual(mock_get.call_count, 1)  # Still 1!

    @patch("app.data.upstox_provider.get_upstox_token", return_value="valid_mock_token_1234567890")
    @patch("app.data.upstox_provider.fetch_upstox_candles")
    @patch("app.data.market_provider.get_active_data_source", return_value="upstox")
    def test_market_provider_clean_failover_on_rate_limit(self, mock_src, mock_upstox_candles, mock_token):
        """When Upstox rate limit is exceeded, market_provider safely fails closed without crashing."""
        from app.data.market_provider import fetch_candles

        # Upstox raises rate limit exceeded
        mock_upstox_candles.side_effect = UpstoxRateLimitExceeded("30-minute API limit reached")

        df = fetch_candles("INFY", interval="15m", period="5d")
        self.assertTrue(df.empty)

    @patch("app.data.upstox_provider.requests.get")
    def test_circuit_open_bypasses_bulk_ltp_and_single_ltp(self, mock_get):
        """When circuit breaker is open, bulk and single LTP functions return empty/None with 0 network calls."""
        limiter = get_upstox_limiter()
        limiter.trip_circuit_breaker(duration=120.0, reason="Testing circuit bypass")
        self.assertTrue(limiter.is_circuit_open())

        # Calling bulk LTP must return {} immediately without hitting requests.get
        bulk_res = fetch_upstox_bulk_ltp(["RELIANCE", "TCS"])
        self.assertEqual(bulk_res, {})
        self.assertEqual(mock_get.call_count, 0)

        # Calling single LTP must return None immediately without hitting requests.get
        single_res = fetch_upstox_ltp("RELIANCE")
        self.assertIsNone(single_res)
        self.assertEqual(mock_get.call_count, 0)

        # Clear circuit breaker
        limiter._circuit_open_until = 0.0

    @patch("app.data.market_provider.get_active_data_source", return_value="upstox")
    @patch("app.data.market_provider.fetch_upstox_bulk_ltp")
    @patch("app.data.market_provider.fetch_upstox_ltp")
    def test_bulk_quotes_fails_closed_for_missing_symbol(self, mock_fetch_ltp, mock_bulk, mock_src):
        """Missing symbols from Upstox bulk call must fail closed as UNAVAILABLE without Yahoo fallback."""
        from app.data.market_provider import get_bulk_live_quotes_with_meta

        get_upstox_limiter()._circuit_open_until = 0.0
        # Upstox bulk call only returns RELIANCE, leaving INFY missing
        mock_bulk.return_value = {"RELIANCE": 2950.0}

        results = get_bulk_live_quotes_with_meta(["RELIANCE", "INFY"])

        # Both symbols should be present in results
        self.assertIn("RELIANCE", results)
        self.assertEqual(results["RELIANCE"]["price"], 2950.0)
        self.assertEqual(results["RELIANCE"]["source"], "upstox")
        self.assertEqual(results["RELIANCE"]["quote_freshness"], "REAL_TIME")

        self.assertIn("INFY", results)
        self.assertIsNone(results["INFY"]["price"])
        self.assertEqual(results["INFY"]["source"], "unavailable")
        self.assertEqual(results["INFY"]["quote_freshness"], "UNAVAILABLE")

        # Crucial check: fetch_upstox_ltp MUST NOT be called individually for missing tickers
        self.assertEqual(mock_fetch_ltp.call_count, 0)

    @patch("app.data.upstox_provider.requests.get")
    @patch("app.data.upstox_provider.notify_upstox_auth_alert")
    def test_retry_after_header_adjusts_circuit_breaker(self, mock_alert, mock_get):
        """HTTP 429 with Retry-After header trips circuit breaker for at least that duration."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "360"}
        mock_resp.text = "Too Many Requests"
        mock_get.return_value = mock_resp

        limiter = get_upstox_limiter()
        limiter._circuit_open_until = 0.0

        with self.assertRaises(UpstoxRateLimitExceeded):
            upstox_request("GET", "https://api.upstox.com/v2/market-quote/ltp")

        self.assertTrue(limiter.is_circuit_open())
        # Circuit remaining should be at least 300s
        self.assertGreaterEqual(limiter.get_circuit_remaining_seconds(), 300.0)

        # Clear circuit breaker
        limiter._circuit_open_until = 0.0

if __name__ == "__main__":
    unittest.main()


