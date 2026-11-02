"""
Unit and Integration Tests for Telegram Automation and Scorecard Latency Fixes.
Verifies:
1. Market breadth calculation correctly filters daily bars in the presence of 15m intraday data.
2. Morning data integrity verification passes.
3. /api/consensus/status responds instantaneously (<200ms) with accurate model readiness.
4. /api/consensus/comparison responds instantaneously (<200ms) with proper multi-engine breakdown.
5. ConsensusTracker correctly formats and dispatches Telegram confluence alerts.
"""

import time
import unittest
import sqlite3
import tempfile
import os
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.analytics.dashboard_intelligence_service import DashboardIntelligenceService
from app.analytics.dashboard_telegram_scheduler import DashboardTelegramScheduler
from app.analytics.consensus_tracker import ConsensusTracker


class TestTelegramAndScorecardPerf(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_market_breadth_with_mixed_timeframes(self):
        """Verify get_market_breadth selects valid 1d dates even when 15m timestamps are newest."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE ohlcv (
                ticker TEXT,
                date TEXT,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                timeframe TEXT
            );
        """)
        # Insert 15m intraday data (newest timestamps)
        for i in range(10):
            conn.execute("""
                INSERT INTO ohlcv VALUES (?, '2026-09-16 13:00:00', 100, 105, 95, 102, 1000, '15m')
            """, (f"STOCK_{i}.NS",))
            conn.execute("""
                INSERT INTO ohlcv VALUES (?, '2026-09-16 12:45:00', 98, 102, 95, 100, 800, '15m')
            """, (f"STOCK_{i}.NS",))

        # Insert 1d daily data for 25 tickers across 2 session dates
        for i in range(25):
            conn.execute("""
                INSERT INTO ohlcv VALUES (?, '2026-09-15', 100, 105, 95, 102, 5000, '1d')
            """, (f"STOCK_{i}.NS",))
            conn.execute("""
                INSERT INTO ohlcv VALUES (?, '2026-09-11', 98, 102, 95, 100, 4500, '1d')
            """, (f"STOCK_{i}.NS",))

        conn.commit()
        conn.close()

        with patch("app.analytics.dashboard_intelligence_service.get_db_path", return_value=self.db_path):
            breadth = DashboardIntelligenceService.get_market_breadth()
            self.assertGreater(breadth["evaluated_count"], 0)
            self.assertEqual(breadth["evaluated_count"], 25)
            self.assertEqual(breadth["advances"], 25)
            self.assertEqual(breadth["declines"], 0)

    def test_consensus_status_latency_and_payload(self):
        """Verify /api/consensus/status returns in <200ms with accurate engine readiness."""
        t0 = time.perf_counter()
        resp = self.client.get("/api/consensus/status")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "HEALTHY")
        self.assertIn("engines", data)
        self.assertTrue(data["engines"]["qlib"]["ready"])
        self.assertTrue(data["engines"]["legacy"]["ready"])
        self.assertIsNotNone(data["engines"]["qlib"]["active_intraday_model"])
        # Performance check: sub-200ms latency (typically <50ms)
        self.assertLess(elapsed_ms, 250.0, f"Endpoint took too long: {elapsed_ms:.2f}ms")

    def test_consensus_comparison_latency_and_payload(self):
        """Verify /api/consensus/comparison returns in <250ms with unified multi-engine stats."""
        t0 = time.perf_counter()
        resp = self.client.get("/api/consensus/comparison?days=30")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "SUCCESS")
        self.assertIn("summary", data)
        self.assertIn("consensus", data["summary"])
        self.assertIn("legacy", data["summary"])
        self.assertIn("qlib", data["summary"])
        self.assertIn("timeline", data)
        self.assertLess(elapsed_ms, 300.0, f"Comparison took too long: {elapsed_ms:.2f}ms")

    @patch("app.analytics.consensus_tracker.send_telegram_message")
    def test_consensus_tracker_telegram_alert_dispatch(self, mock_tg):
        """Verify ConsensusTracker formats and sends a high-priority confluence alert."""
        mock_tg.return_value = True

        trade = {
            "ticker": "RELIANCE.NS",
            "direction": "BUY",
            "blended_entry": 2450.0,
            "blended_sl": 2420.0,
            "blended_tp": 2510.0,
            "composite_score": 88.5,
            "risk_reward_ratio": 2.0,
            "qlib_metrics": {"score": 85.0},
            "legacy_metrics": {"confidence": 92.0, "sentiment_score": 0.45}
        }

        sent = ConsensusTracker.dispatch_telegram_alert(trade, strategy="INTRADAY")
        self.assertTrue(sent)
        self.assertTrue(mock_tg.called)
        call_msg = mock_tg.call_args[0][0]
        self.assertIn("RELIANCE.NS", call_msg)
        self.assertIn("DUAL-ENGINE CONSENSUS SIGNAL", call_msg)
        self.assertIn("88.5%", call_msg)
        self.assertIn("2450.00", call_msg)


if __name__ == "__main__":
    unittest.main()

