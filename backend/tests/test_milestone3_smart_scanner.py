"""
Test suite for Milestone 3: Unified Smart Scanner Backend & UI.
Tests:
1. SmartScannerPipeline execution (Intraday vs Swing)
2. Fail-closed validation (invalid timeframe, unknown universe)
3. Cash equity swing short ban enforcement
4. Zero heat guarantee (position_type='NOT_A_POSITION')
5. FastAPI endpoints (/api/smart-scanner/sweep, /universes, /status)
6. Non-negotiable Production Safety Invariants
"""

import unittest
import asyncio
import json
import os
import hashlib
import sqlite3
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.analytics.smart_scanner import SmartScannerPipeline, format_sse
from app.analytics.decision_engine import QualificationResult
from app.analytics.model_registry import ModelRegistry, CHAMPION_HASHES
from app.analytics.kelly_sizer import get_portfolio_heat_status
from app.data.historical_data_layer import get_db_path

EXPECTED_INTRADAY_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
EXPECTED_SWING_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

class TestMilestone3SmartScanner(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)

    # ─────────────────────────────────────────────────────────────
    # 1. SMART SCANNER PIPELINE TESTS
    # ─────────────────────────────────────────────────────────────

    def test_01_format_sse_structure(self):
        payload = {"type": "info", "message": "Test message", "progress": 50}
        formatted = format_sse(payload)
        self.assertTrue(formatted.endswith("\n"))
        parsed = json.loads(formatted.strip())
        self.assertEqual(parsed["type"], "info")
        self.assertEqual(parsed["progress"], 50)

    def test_02_invalid_timeframe_fails_closed(self):
        async def run():
            events = []
            async for line in SmartScannerPipeline.run_sweep(timeframe="crypto_invalid", universe="NIFTY_50"):
                events.append(json.loads(line.strip()))
            return events

        events = asyncio.run(run())
        self.assertTrue(any(e.get("type") == "error" for e in events))
        error_msg = [e.get("message") for e in events if e.get("type") == "error"][0]
        self.assertIn("Invalid timeframe", error_msg)

    def test_03_unknown_universe_fails_closed(self):
        async def run():
            events = []
            async for line in SmartScannerPipeline.run_sweep(timeframe="intraday", universe="NON_EXISTENT_123"):
                events.append(json.loads(line.strip()))
            return events

        events = asyncio.run(run())
        self.assertTrue(any(e.get("type") == "error" for e in events))
        error_msg = [e.get("message") for e in events if e.get("type") == "error"][0]
        self.assertIn("Universe resolution failed", error_msg)

    def test_04_intraday_sweep_emits_valid_sse_stages(self):
        # Run sweep on Benchmark 5 mock to test deterministic flow
        mock_q = QualificationResult(
            ticker="RELIANCE.NS",
            qualified=True,
            direction="BULLISH",
            is_bullish=True,
            confidence=78.5,
            raw_confidence=75.0,
            entry=2500.0,
            sl=2450.0,
            tp1=2580.0,
            tp2=2640.0,
            base_probs=(0.8, 0.75, 0.82),
            rejection_reason=None
        )

        async def run():
            events = []
            with patch("app.analytics.smart_scanner.evaluate_ticker", return_value=mock_q), \
                 patch("app.analytics.smart_scanner.save_ml_trade", return_value=True), \
                 patch("app.data.data_gateway.DataGateway.get_ohlcv", return_value=MagicMock(empty=False, __len__=lambda s: 50)):
                async for line in SmartScannerPipeline.run_sweep(
                    timeframe="intraday",
                    universe="BENCHMARK_5",
                    min_confidence=60.0
                ):
                    events.append(json.loads(line.strip()))
            return events

        events = asyncio.run(run())
        event_types = [e.get("type") for e in events]
        self.assertIn("system", event_types)
        self.assertIn("info", event_types)
        self.assertIn("result", event_types)

    def test_05_swing_cash_short_ban_enforced(self):
        # A BEARISH qualification on SWING must NOT be persisted or emitted as a candidate
        mock_bearish = QualificationResult(
            ticker="TCS.NS",
            qualified=True,
            direction="BEARISH",
            is_bullish=False,
            confidence=85.0,
            raw_confidence=82.0,
            entry=3500.0,
            sl=3600.0,
            tp1=3400.0,
            tp2=3300.0,
            base_probs=(0.85, 0.82, 0.88),
            rejection_reason=None
        )

        async def run():
            events = []
            with patch("app.analytics.smart_scanner.evaluate_ticker", return_value=mock_bearish), \
                 patch("app.analytics.smart_scanner.save_ml_trade") as mock_save, \
                 patch("app.data.data_gateway.DataGateway.get_ohlcv", return_value=MagicMock(empty=False, __len__=lambda s: 50)):
                async for line in SmartScannerPipeline.run_sweep(
                    timeframe="swing",
                    universe="BENCHMARK_5"
                ):
                    events.append(json.loads(line.strip()))
            return events, mock_save

        events, mock_save = asyncio.run(run())
        # mock_save must NOT have been called because cash shorts in swing are banned
        mock_save.assert_not_called()
        candidate_events = [e for e in events if e.get("type") == "candidate"]
        self.assertEqual(len(candidate_events), 0)

    # ─────────────────────────────────────────────────────────────
    # 2. FASTAPI ROUTER TESTS
    # ─────────────────────────────────────────────────────────────

    def test_06_universes_endpoint_returns_presets(self):
        res = self.client.get("/api/smart-scanner/universes")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("presets", data)
        preset_ids = [p["id"] for p in data["presets"]]
        self.assertIn("NIFTY_500", preset_ids)
        self.assertIn("NIFTY_50", preset_ids)
        self.assertIn("BANK_NIFTY", preset_ids)
        self.assertIn("BENCHMARK_5", preset_ids)

    def test_07_status_endpoint_returns_governance_data(self):
        res = self.client.get("/api/smart-scanner/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("champions", data)
        self.assertTrue(data["champions"]["all_champions_intact"])
        self.assertIn("macro", data)
        self.assertIn("positions", data)
        self.assertEqual(data["positions"]["portfolio_heat_pct"], 0.0)

    def test_08_sweep_endpoint_streams_events(self):
        with patch("app.analytics.smart_scanner.SmartScannerPipeline.run_sweep") as mock_run:
            async def dummy_gen(**kwargs):
                yield format_sse({"type": "system", "message": "Sweep start", "progress": 10})
                yield format_sse({"type": "result", "data": None, "progress": 100})
            mock_run.side_effect = dummy_gen

            res = self.client.get("/api/smart-scanner/sweep?timeframe=intraday&universe=BENCHMARK_5")
            self.assertEqual(res.status_code, 200)
            self.assertIn("text/event-stream", res.headers["content-type"])
            content = res.text
            self.assertIn("Sweep start", content)
            self.assertIn("result", content)

    # ─────────────────────────────────────────────────────────────
    # 3. PRODUCTION SAFETY INVARIANTS
    # ─────────────────────────────────────────────────────────────

    def test_09_champion_hashes_remain_byte_exact(self):
        intra_valid, intra_sha, _ = ModelRegistry.verify_champion_hash("intraday")
        swing_valid, swing_sha, _ = ModelRegistry.verify_champion_hash("swing")

        self.assertTrue(intra_valid)
        self.assertEqual(intra_sha, EXPECTED_INTRADAY_HASH)

        self.assertTrue(swing_valid)
        self.assertEqual(swing_sha, EXPECTED_SWING_HASH)

    def test_10_portfolio_heat_is_zero(self):
        heat_status = get_portfolio_heat_status()
        self.assertEqual(heat_status.get("current_heat_pct"), 0.0)
        self.assertEqual(heat_status.get("actual_positions"), 0)

    def test_11_database_row_counts_preserved(self):
        canonical_db = get_db_path()
        conn = sqlite3.connect(canonical_db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM ml_trade_history")
        rows = c.fetchone()[0]
        conn.close()
        self.assertGreaterEqual(rows, 74)


if __name__ == "__main__":
    unittest.main()
