"""
Integration test proving the live candle ingestion service is connected end-to-end to the production regime gate:
  1. Traces the runtime path:
     Upstox V3 WebSocket feed message
       -> UpstoxStreamService._handle_feed_message
       -> IndexCandleIngestionPrototype.process_feed_message({"feeds": feeds})
       -> Local 1-minute aggregation & bucket finalization
       -> SQLite continuous_index_ohlcv table (with causal provenance: available_at, is_point_in_time=1, is_complete=1)
       -> OptionRegimeGate.get_completed_index_bars()
       -> OptionRegimeGate.evaluate_regime()
       -> OptionSignalEngine Gate 5E decision!
  2. Proves that WebSocket feed data directly drives the production regime gate decision.
  3. Proves that if WebSocket feed stalls for > 120s, OptionRegimeGate fails closed with 'Index feed is stale'.
  4. Proves provisional / in-flight buckets (is_complete = 0) cannot bypass completed-bar requirements.
"""

import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from app.analytics.option_sniffer.option_constants import (
    DIRECTION_BUY_CE,
    STATE_QUALIFIED,
    STATE_WATCH,
    MAX_DATA_STALENESS_SECONDS,
)
from app.data.index_candle_ingestion_prototype import (
    IndexCandleIngestionPrototype,
    INDEX_KEYS,
)
from app.analytics.option_sniffer.option_regime_gate import (
    OptionRegimeGate,
    MIN_WARMUP_BARS,
)
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine


class TestLiveCandleWebSocketToRegimeGateIntegration(unittest.TestCase):

    def setUp(self):
        OptionSignalEngine.reset_class_state()
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp_db.name
        self.tmp_db.close()

        self.db_patcher = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.db_patcher2 = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.db_patcher.start()
        self.db_patcher2.start()

        # Initialize ingestion service with isolated DB
        self.ingestion_service = IndexCandleIngestionPrototype(db_path=self.db_path)
        self.regime_gate = OptionRegimeGate(db_path=self.db_path)

        # Wire UpstoxStreamService feed listener to our isolated ingestion service
        from app.data.upstox_stream_service import get_stream_service
        self.stream_service = get_stream_service()
        self.feed_listener = lambda feeds: self.ingestion_service.process_feed_message({"feeds": feeds})
        self.stream_service.register_feed_listener(self.feed_listener)

    def tearDown(self):
        OptionSignalEngine.reset_class_state()
        if self.feed_listener in self.stream_service._feed_listeners:
            self.stream_service._feed_listeners.remove(self.feed_listener)

        self.db_patcher2.stop()
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def _generate_mock_v3_feed(self, ticker_key: str, ltp: float, timestamp_sec: float) -> dict:
        """Constructs an Upstox V3 protobuf-decoded feed dict for an index."""
        ts_ms = int(timestamp_sec * 1000)
        return {
            ticker_key: {
                "fullFeed": {
                    "indexFF": {
                        "ltpc": {
                            "ltp": ltp,
                            "ltt": str(ts_ms),
                            "cp": ltp - 10.0
                        },
                        "marketOHLC": {
                            "ohlc": [
                                {
                                    "interval": "I1",
                                    "open": ltp - 2.0,
                                    "high": ltp + 3.0,
                                    "low": ltp - 3.0,
                                    "close": ltp,
                                    "vol": 1000,
                                    "ts": str(ts_ms)
                                }
                            ]
                        }
                    }
                }
            }
        }

    def test_full_pipeline_from_websocket_to_regime_gate_decision(self):
        """
        End-to-End integration test:
          1. 29 minutes of mock WebSocket feed ticks are dispatched via UpstoxStreamService.
          2. IndexCandleIngestionPrototype ingests, aggregates, and finalizes 28 completed 1m candles in SQLite.
          3. OptionRegimeGate queries continuous_index_ohlcv and computes canonical Wilder ADX(14).
          4. Gate decision becomes eligible (hurdles_passed=True).
        """
        now = datetime.now()
        base_dt = (now - timedelta(minutes=30)).replace(second=0, microsecond=0)
        session_date = base_dt.strftime("%Y-%m-%d")

        # Simulate 29 completed minutes of ticks arriving over WebSocket
        for minute_idx in range(29):
            min_dt = base_dt + timedelta(minutes=minute_idx)
            for sec in [5, 30, 55]:
                tick_dt = min_dt + timedelta(seconds=sec)
                price = 25000.0 + (minute_idx * 5.0) + (sec * 0.05)
                feed = self._generate_mock_v3_feed("NSE_INDEX|Nifty 50", price, tick_dt.timestamp())
                self.stream_service._handle_feed_message({"feeds": feed})

        # Send one tick at minute 29 second 5 to finalize minute 28
        final_tick_dt = base_dt + timedelta(minutes=29, seconds=5)
        feed_final = self._generate_mock_v3_feed("NSE_INDEX|Nifty 50", 25150.0, final_tick_dt.timestamp())
        self.stream_service._handle_feed_message({"feeds": feed_final})

        # Verify SQLite has at least 28 completed candles
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT COUNT(*) FROM continuous_index_ohlcv
                WHERE ticker = 'NIFTY' AND is_complete = 1
            """).fetchone()
            completed_count = rows[0]

        self.assertGreaterEqual(completed_count, 28, f"Expected >= 28 completed candles, got {completed_count}")

        # Now evaluate OptionRegimeGate at minute 29 second 10 (fresh within 15 seconds)
        decision_time = final_tick_dt + timedelta(seconds=10)
        regime_result = self.regime_gate.evaluate_regime(
            underlying="NIFTY",
            opt_type="CE",
            as_of_time=decision_time,
            spot_ltp=25150.0,
            day_high=25155.0,
            day_low=24995.0,
            option_ask=120.0,
            target_pct=15.0,
            lot_size=65,
            enforce_cutoff=False
        )

        # Gate must pass and provide complete telemetry calculated from the DB bars
        self.assertTrue(regime_result["hurdles_passed"], f"Gate failed: {regime_result['rejection_reasons']}")
        self.assertIsNotNone(regime_result["telemetry"]["adx_14"])
        self.assertGreater(regime_result["telemetry"]["adx_14"], 20.0)
        self.assertGreater(regime_result["telemetry"]["kaufman_er"], 0.30)

    def test_pipeline_fails_closed_when_feed_stalls(self):
        """
        Verifies that if the live WebSocket feed stalls for > 120 seconds,
        OptionRegimeGate detects staleness from the DB and fails closed.
        """
        now = datetime.now()
        base_dt = (now - timedelta(minutes=35)).replace(second=0, microsecond=0)
        session_date = base_dt.strftime("%Y-%m-%d")

        # Ingest 29 minutes of data ending at now - 6 minutes
        for minute_idx in range(29):
            min_dt = base_dt + timedelta(minutes=minute_idx)
            for sec in [10, 45]:
                tick_dt = min_dt + timedelta(seconds=sec)
                price = 25000.0 + (minute_idx * 5.0)
                feed = self._generate_mock_v3_feed("NSE_INDEX|Nifty 50", price, tick_dt.timestamp())
                self.stream_service._handle_feed_message({"feeds": feed})

        final_tick_dt = base_dt + timedelta(minutes=29, seconds=5)
        feed_final = self._generate_mock_v3_feed("NSE_INDEX|Nifty 50", 25150.0, final_tick_dt.timestamp())
        self.stream_service._handle_feed_message({"feeds": feed_final})

        # Decision time is now (which is ~6 minutes after candle 28 completed > 120s staleness timeout)
        stale_decision_time = now
        regime_result = self.regime_gate.evaluate_regime(
            underlying="NIFTY",
            opt_type="CE",
            as_of_time=stale_decision_time,
            spot_ltp=25150.0,
            day_high=25155.0,
            day_low=24995.0,
            enforce_cutoff=False
        )

        self.assertFalse(regime_result["hurdles_passed"])
        rejections = " ".join(regime_result["rejection_reasons"])
        self.assertIn("Index feed is stale", rejections)

    def test_provisional_candle_is_excluded_from_warmup(self):
        """
        Verifies that an open, in-flight minute bucket in memory (is_complete = 0)
        is not treated as a completed candle by OptionRegimeGate.
        """
        now = datetime.now()
        base_dt = (now - timedelta(minutes=16)).replace(second=0, microsecond=0)
        session_date = base_dt.strftime("%Y-%m-%d")

        # Ingest only 15 minutes of data
        for minute_idx in range(15):
            min_dt = base_dt + timedelta(minutes=minute_idx)
            tick_dt = min_dt + timedelta(seconds=15)
            feed = self._generate_mock_v3_feed("NSE_INDEX|Nifty 50", 25000.0 + minute_idx * 2.0, tick_dt.timestamp())
            self.stream_service._handle_feed_message({"feeds": feed})

        # Finalize up to minute 14
        final_tick_dt = base_dt + timedelta(minutes=15, seconds=5)
        feed_final = self._generate_mock_v3_feed("NSE_INDEX|Nifty 50", 25030.0, final_tick_dt.timestamp())
        self.stream_service._handle_feed_message({"feeds": feed_final})

        # Gate at decision time should fail closed because only 14 completed bars exist
        decision_time = final_tick_dt + timedelta(seconds=5)
        ok, msg, df = self.regime_gate.get_completed_index_bars("NIFTY", session_date, decision_time)

        self.assertFalse(ok)
        self.assertIn("Insufficient continuous completed bars (15 < 28 required)", msg)


if __name__ == "__main__":
    unittest.main()
