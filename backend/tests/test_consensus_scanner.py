import unittest
import asyncio
from unittest.mock import patch, MagicMock
from app.analytics.consensus_scanner import ConsensusScanner, normalize_symbol, normalize_direction


class TestConsensusScanner(unittest.IsolatedAsyncioTestCase):

    def test_01_symbol_normalization(self):
        self.assertEqual(normalize_symbol("RELIANCE.NS"), "RELIANCE")
        self.assertEqual(normalize_symbol("tcs.bo"), "TCS")
        self.assertEqual(normalize_symbol(" INFY "), "INFY")
        self.assertEqual(normalize_symbol(""), "")

    def test_02_direction_normalization(self):
        self.assertEqual(normalize_direction("BUY"), "BUY")
        self.assertEqual(normalize_direction("BULLISH"), "BUY")
        self.assertEqual(normalize_direction("LONG"), "BUY")
        self.assertEqual(normalize_direction("SELL"), "SELL")
        self.assertEqual(normalize_direction("BEARISH"), "SELL")
        self.assertEqual(normalize_direction("SHORT"), "SELL")
        self.assertEqual(normalize_direction("", is_bullish=True), "BUY")
        self.assertEqual(normalize_direction("", is_bullish=False), "SELL")

    @patch("app.analytics.consensus_scanner.QlibScanners.run_intraday_scan")
    @patch("app.analytics.consensus_scanner.SmartScannerPipeline.run_sweep_batch")
    async def test_03_unanimous_buy_confluence(self, mock_legacy, mock_qlib):
        # Qlib produces BUY for RELIANCE
        mock_qlib.return_value = {
            "status": "SUCCESS",
            "opportunities": [
                {
                    "ticker": "RELIANCE.NS",
                    "direction": "BUY",
                    "entry_price": 2500.0,
                    "stop_loss": 2450.0,
                    "target_price": 2600.0,
                    "score": 0.85,
                    "rank": 1
                }
            ]
        }

        # Legacy produces BUY for RELIANCE
        mock_legacy.return_value = {
            "status": "SUCCESS",
            "candidates": [
                {
                    "ticker": "RELIANCE.NS",
                    "direction": "BULLISH",
                    "is_bullish": True,
                    "entry": 2502.0,
                    "sl": 2460.0,
                    "tp1": 2580.0,
                    "tp2": 2620.0,
                    "confidence": 80.0
                }
            ]
        }

        res = await ConsensusScanner.run_consensus_scan(strategy="INTRADAY", universe="NIFTY_50", top_k=5)

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["summary"]["unanimous_count"], 1)
        self.assertEqual(res["summary"]["conflict_count"], 0)

        trade = res["unanimous_trades"][0]
        self.assertEqual(trade["symbol"], "RELIANCE")
        self.assertEqual(trade["direction"], "BUY")
        self.assertEqual(trade["confluence_status"], "UNANIMOUS")
        # Blended entry: average of 2500.0 and 2502.0 = 2501.0
        self.assertEqual(trade["blended_entry"], 2501.0)
        # Percentage-blended SL projected from blended_entry: 2455.0
        self.assertEqual(trade["blended_sl"], 2455.0)
        # Percentage-blended TP projected from blended_entry: 2590.0
        self.assertEqual(trade["blended_tp"], 2590.0)
        # Risk-to-reward ratio meets institutional standard (>= 1.25)
        self.assertGreaterEqual(trade["risk_reward_ratio"], 1.25)
        self.assertEqual(trade["risk_reward_ratio"], 1.93)
        # Composite score: avg(85.0, 80.0) = 82.5
        self.assertEqual(trade["composite_score"], 82.5)
        # Verify consensus_trades alias exists for daemon compatibility
        self.assertIn("consensus_trades", res)
        self.assertEqual(res["consensus_trades"], res["unanimous_trades"])

    @patch("app.analytics.consensus_scanner.QlibScanners.run_intraday_scan")
    @patch("app.analytics.consensus_scanner.SmartScannerPipeline.run_sweep_batch")
    async def test_04_directional_conflict_warning(self, mock_legacy, mock_qlib):
        # Qlib produces SELL for TATASTEEL
        mock_qlib.return_value = {
            "status": "SUCCESS",
            "opportunities": [
                {
                    "ticker": "TATASTEEL.NS",
                    "direction": "SELL",
                    "entry_price": 140.0,
                    "stop_loss": 145.0,
                    "target_price": 130.0,
                    "score": -0.75,
                    "rank": 1
                }
            ]
        }

        # Legacy produces BUY for TATASTEEL
        mock_legacy.return_value = {
            "status": "SUCCESS",
            "candidates": [
                {
                    "ticker": "TATASTEEL.NS",
                    "direction": "BULLISH",
                    "is_bullish": True,
                    "entry": 140.0,
                    "sl": 135.0,
                    "tp1": 150.0,
                    "confidence": 72.0
                }
            ]
        }

        res = await ConsensusScanner.run_consensus_scan(strategy="INTRADAY", universe="NIFTY_50", top_k=5)

        self.assertEqual(res["summary"]["unanimous_count"], 0)
        self.assertEqual(res["summary"]["conflict_count"], 1)

        conflict = res["conflicting_trades"][0]
        self.assertEqual(conflict["symbol"], "TATASTEEL")
        self.assertEqual(conflict["confluence_status"], "CONFLICT")
        self.assertEqual(conflict["qlib_direction"], "SELL")
        self.assertEqual(conflict["legacy_direction"], "BUY")
        self.assertIn("Directional conflict", conflict["warning"])

    @patch("app.analytics.consensus_scanner.QlibScanners.run_swing_scan")
    @patch("app.analytics.consensus_scanner.SmartScannerPipeline.run_sweep_batch")
    async def test_05_solo_picks_partitioning(self, mock_legacy, mock_qlib):
        mock_qlib.return_value = {
            "status": "SUCCESS",
            "opportunities": [
                {
                    "ticker": "INFY.NS",
                    "direction": "BUY",
                    "entry_price": 1800.0,
                    "stop_loss": 1750.0,
                    "target_price": 1900.0,
                    "score": 0.65,
                    "rank": 1
                }
            ]
        }

        mock_legacy.return_value = {
            "status": "SUCCESS",
            "candidates": [
                {
                    "ticker": "WIPRO.NS",
                    "direction": "BULLISH",
                    "is_bullish": True,
                    "entry": 450.0,
                    "sl": 435.0,
                    "tp1": 480.0,
                    "confidence": 68.0
                }
            ]
        }

        res = await ConsensusScanner.run_consensus_scan(strategy="SWING", universe="NIFTY_500", top_k=5)

        self.assertEqual(res["summary"]["unanimous_count"], 0)
        self.assertEqual(res["summary"]["conflict_count"], 0)
        self.assertEqual(res["summary"]["qlib_solo_count"], 1)
        self.assertEqual(res["summary"]["legacy_solo_count"], 1)
        self.assertEqual(res["qlib_solo"][0]["symbol"], "INFY")
        self.assertEqual(res["legacy_solo"][0]["symbol"], "WIPRO")

    @patch("app.analytics.consensus_scanner.QlibScanners.run_intraday_scan")
    @patch("app.analytics.consensus_scanner.SmartScannerPipeline.run_sweep_batch")
    async def test_06_defensive_engine_failure_handling(self, mock_legacy, mock_qlib):
        # Qlib throws an unhandled exception
        mock_qlib.side_effect = RuntimeError("Qlib data feed connection lost")

        # Legacy succeeds
        mock_legacy.return_value = {
            "status": "SUCCESS",
            "candidates": [
                {
                    "ticker": "HDFCBANK.NS",
                    "direction": "BULLISH",
                    "is_bullish": True,
                    "entry": 1600.0,
                    "sl": 1560.0,
                    "tp1": 1680.0,
                    "confidence": 75.0
                }
            ]
        }

        res = await ConsensusScanner.run_consensus_scan(strategy="INTRADAY", universe="NIFTY_50", top_k=5)

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["engines"]["qlib"]["status"], "ERROR")
        self.assertIn("connection lost", res["engines"]["qlib"]["error"])
        self.assertEqual(res["engines"]["legacy"]["status"], "SUCCESS")
        self.assertEqual(len(res["legacy_solo"]), 1)

    def test_07_active_scan_and_latest_scan_persistence(self):
        # 1. Idle state check
        active = ConsensusScanner.get_active_scan()
        self.assertFalse(active["active"])

        # 2. Latest scan persistence check
        test_payload = {
            "scan_id": "consensus_test_123",
            "strategy": "INTRADAY",
            "universe": "TEST_UNIVERSE",
            "status": "SUCCESS",
            "elapsed_seconds": 1.25,
            "completed_at": "2026-09-17T12:00:00",
            "summary": {"unanimous_count": 0, "conflict_count": 0},
            "unanimous_trades": [],
            "conflicting_trades": [],
            "qlib_solo": [],
            "legacy_solo": []
        }
        ConsensusScanner._save_latest_scan("INTRADAY", test_payload)

        # Retrieve via get_latest_scan
        retrieved = ConsensusScanner.get_latest_scan("INTRADAY")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["scan_id"], "consensus_test_123")
        self.assertEqual(retrieved["universe"], "TEST_UNIVERSE")

    def test_08_stop_active_scan(self):
        """Verify ConsensusScanner.stop_active_scan resets active scan and sets cancellation flag."""
        ConsensusScanner._ACTIVE_SCAN = {
            "scan_id": "test_scan_to_stop",
            "strategy": "INTRADAY",
            "universe": "TEST_UNIVERSE",
            "start_time": 100.0,
            "started_at": "2026-09-17T12:00:00",
            "stage": "RUNNING",
            "stage_name": "Running",
            "progress": 50,
            "universe_count": 100,
            "message": "Testing stop"
        }
        self.assertTrue(ConsensusScanner.get_active_scan()["active"])

        res = ConsensusScanner.stop_active_scan()
        self.assertEqual(res["status"], "STOPPED")
        self.assertTrue(ConsensusScanner.is_stop_requested())
        self.assertFalse(ConsensusScanner.get_active_scan()["active"])

    @patch("app.analytics.consensus_scanner.QlibScanners.run_intraday_scan")
    @patch("app.analytics.consensus_scanner.SmartScannerPipeline.run_sweep_batch")
    async def test_09_disparate_entry_rr_collapse_prevention(self, mock_legacy, mock_qlib):
        """
        Verify that when Qlib and Legacy evaluate a stock with disparate entry anchors
        (e.g., Qlib=3477.20, Legacy=3580.30 like SUPREMEIND.NS), percentage-based blending
        prevents R:R collapse and guarantees risk_reward_ratio >= 1.25 (never 0.05).
        """
        mock_qlib.return_value = {
            "status": "SUCCESS",
            "opportunities": [
                {
                    "ticker": "SUPREMEIND.NS",
                    "direction": "BUY",
                    "entry_price": 3477.20,
                    "stop_loss": 3449.38,
                    "target_price": 3532.84,
                    "score": 0.501,
                    "rank": 1
                }
            ]
        }

        mock_legacy.return_value = {
            "status": "SUCCESS",
            "candidates": [
                {
                    "ticker": "SUPREMEIND.NS",
                    "direction": "BULLISH",
                    "is_bullish": True,
                    "entry": 3580.30,
                    "sl": 3545.67,
                    "tp1": 3614.93,
                    "confidence": 50.8
                }
            ]
        }

        res = await ConsensusScanner.run_consensus_scan(strategy="INTRADAY", universe="NIFTY_50", top_k=5)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(len(res["unanimous_trades"]), 1)

        trade = res["unanimous_trades"][0]
        self.assertEqual(trade["symbol"], "SUPREMEIND")
        self.assertEqual(trade["blended_entry"], 3528.75)
        self.assertEqual(trade["blended_sl"], 3497.57)
        self.assertEqual(trade["blended_tp"], 3574.05)

        # Critical verification: R:R must be >= 1.25 and NOT the broken 0.05
        self.assertGreaterEqual(trade["risk_reward_ratio"], 1.25)
        self.assertEqual(trade["risk_reward_ratio"], 1.45)
        self.assertNotEqual(trade["risk_reward_ratio"], 0.05)

    @patch("app.analytics.consensus_scanner.QlibScanners.run_intraday_scan")
    @patch("app.analytics.consensus_scanner.SmartScannerPipeline.run_sweep_batch")
    @patch("app.analytics.macro_engine.get_macro_regime")
    async def test_10_nifty_macro_guard_blocks_countertrend_short(self, mock_macro, mock_legacy, mock_qlib):
        """Verify NIFTY 50 Intraday Macro Guard blocks counter-trend shorts during Bull Momentum."""
        mock_macro.return_value = {
            "intraday_regime_verdict": "INTRADAY_BULL_MOMENTUM",
            "intraday_change_pct": 0.45,
            "regime_verdict": "BULL_EXPANSION"
        }
        mock_qlib.return_value = {
            "status": "SUCCESS",
            "opportunities": [{
                "ticker": "TCS.NS",
                "direction": "SELL",
                "entry_price": 3500.0,
                "stop_loss": 3535.0,
                "target_price": 3430.0,
                "score": 0.508
            }]
        }
        mock_legacy.return_value = {
            "status": "SUCCESS",
            "candidates": [{
                "ticker": "TCS.NS",
                "direction": "BEARISH",
                "is_bullish": False,
                "entry": 3500.0,
                "sl": 3535.0,
                "tp1": 3430.0,
                "confidence": 50.8
            }]
        }

        res = await ConsensusScanner.run_consensus_scan(strategy="INTRADAY", universe="NIFTY_50", top_k=5)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(len(res["unanimous_trades"]), 1)
        trade = res["unanimous_trades"][0]

        # Trade must be flagged as blocked by Macro Guard
        self.assertFalse(trade["macro_aligned"])
        self.assertIn("INTRADAY_BULL_MOMENTUM", trade["macro_reason"])
        self.assertFalse(trade["is_actionable"])
        self.assertIn("BLOCKED", trade["precision_status"])

    def test_11_timed_anchor_1330_runway_gate(self):
        """Verify 13:30 Afternoon Breakout Anchor strictly enforces runway & momentum."""
        from datetime import datetime
        t_1335 = datetime(2026, 9, 25, 13, 35, 0)

        # Counter-trend short (above VWAP or positive session) at 13:30 must FAIL
        res_bad_short = ConsensusScanner.evaluate_timed_anchor_filter(
            ticker="FAIL_SHORT.NS",
            direction="SELL",
            current_price=101.0,
            live_vwap=100.0,
            live_open=99.0,
            eval_time=t_1335
        )
        self.assertFalse(res_bad_short["passed"])
        self.assertEqual(res_bad_short["quality_boost"], -50.0)

        # Legitimate breakdown short (below VWAP & negative session) at 13:30 must PASS
        res_good_short = ConsensusScanner.evaluate_timed_anchor_filter(
            ticker="GOOD_SHORT.NS",
            direction="SELL",
            current_price=99.0,
            live_vwap=100.0,
            live_open=100.5,
            eval_time=t_1335
        )
        self.assertTrue(res_good_short["passed"])
        self.assertGreater(res_good_short["quality_boost"], 0.0)


if __name__ == '__main__':
    unittest.main()

