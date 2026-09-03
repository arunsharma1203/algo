import unittest
import os
import hashlib
import json
import sqlite3
import pandas as pd
from unittest.mock import patch, MagicMock
from app.data.historical_data_layer import get_db_path
from app.analytics.master_logger import MasterLogger
from app.analytics.model_manager import ModelManager
from app.analytics.decision_engine import evaluate_ticker
from app.api.ml_history import save_ml_trade, _EVAL_CACHE, evaluate_ml_history
from app.analytics.research_job_manager import research_job_manager

class TestMasterSystemAuditHardening(unittest.TestCase):

    def test_champion_model_hashes_strictly_unchanged(self):
        """Ensures production Champion models are strictly preserved and never mutated."""
        intraday_path = "backend/models/intraday/champion_ensemble.pkl"
        swing_path = "backend/models/swing/champion_ensemble.pkl"

        self.assertTrue(os.path.exists(intraday_path), "Intraday Champion model missing")
        self.assertTrue(os.path.exists(swing_path), "Swing Champion model missing")

        with open(intraday_path, "rb") as f:
            intraday_hash = hashlib.sha256(f.read()).hexdigest()
        with open(swing_path, "rb") as f:
            swing_hash = hashlib.sha256(f.read()).hexdigest()

        expected_intraday = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
        expected_swing = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

        self.assertEqual(intraday_hash, expected_intraday, "Intraday model hash mutated!")
        self.assertEqual(swing_hash, expected_swing, "Swing model hash mutated!")

    def test_master_logger_records_and_queries_events(self):
        """Verifies that MasterLogger records events and retrieves them with category/severity filters."""
        test_ticker = "TEST_TICKER.NS"
        success = MasterLogger.log_event(
            category="SCAN_MANUAL",
            event_type="TEST_EVENT",
            message="Test master log event execution",
            ticker=test_ticker,
            universe="NIFTY_500",
            details={"test_metric": 42},
            severity="INFO"
        )
        self.assertTrue(success, "MasterLogger.log_event failed")

        events = MasterLogger.get_events(category="SCAN_MANUAL", ticker=test_ticker, limit=10)
        self.assertGreaterEqual(len(events), 1)
        latest = events[0]
        self.assertEqual(latest["category"], "SCAN_MANUAL")
        self.assertEqual(latest["event_type"], "TEST_EVENT")
        self.assertEqual(latest["ticker"], test_ticker)
        self.assertEqual(latest["details"].get("test_metric"), 42)

    def test_column_tuple_normalization_in_decision_engine(self):
        """Verifies that evaluate_ticker handles MultiIndex or tuple columns without crashing."""
        model, meta = ModelManager.load_champion("swing")
        dates = pd.date_range("2024-01-01", periods=100, freq="D")
        
        # Create DataFrame with tuple columns (MultiIndex simulation)
        df_multi = pd.DataFrame({
            ("Open", "TEST.NS"): [100.0 + i for i in range(100)],
            ("High", "TEST.NS"): [105.0 + i for i in range(100)],
            ("Low", "TEST.NS"): [95.0 + i for i in range(100)],
            ("Close", "TEST.NS"): [102.0 + i for i in range(100)],
            ("Volume", "TEST.NS"): [10000.0 + (i * 100) for i in range(100)]
        }, index=dates)

        # Should not raise AttributeError: 'tuple' object has no attribute 'lower'
        res = evaluate_ticker(
            ticker="TEST.NS",
            df=df_multi,
            champion_model=model,
            champion_meta=meta,
            trade_type="SWING",
            skip_enrichment=True
        )
        self.assertIsNotNone(res)
        self.assertTrue(res.pipeline_components.get("feature_engineering"), "Feature engineering failed on tuple columns")

    def test_eval_cache_invalidation_on_save_trade(self):
        """Verifies that _EVAL_CACHE is invalidated when save_ml_trade writes a new record into isolated temp DB."""
        import tempfile
        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp_db.close()

        try:
            with patch("app.api.ml_history.get_db_path", return_value=temp_db.name), \
                 patch("app.data.historical_data_layer.get_db_path", return_value=temp_db.name):
                
                # Populate cache
                _EVAL_CACHE["data"] = [{"id": 999, "ticker": "STALE.NS"}]
                _EVAL_CACHE["timestamp"] = 9999999999.0

                saved = save_ml_trade(
                    ticker="INFY.NS",
                    is_bullish=True,
                    entry=1500.0,
                    sl=1450.0,
                    tp1=1550.0,
                    tp2=1600.0,
                    confidence=65.0,
                    trade_type="INTRADAY",
                    source="MANUAL",
                    position_type="NOT_A_POSITION"
                )
                self.assertTrue(saved)
                # Cache must be wiped
                self.assertIsNone(_EVAL_CACHE["data"])
                self.assertEqual(_EVAL_CACHE["timestamp"], 0)
        finally:
            if os.path.exists(temp_db.name):
                os.unlink(temp_db.name)

    def test_save_ml_trade_rejects_cache_symbols(self):
        """Verifies that save_ml_trade strictly blocks fake/cache symbols from entering ml_trade_history."""
        import tempfile
        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp_db.close()

        try:
            with patch("app.api.ml_history.get_db_path", return_value=temp_db.name), \
                 patch("app.data.historical_data_layer.get_db_path", return_value=temp_db.name):
                
                # Should reject CACHE_xxx.NS
                saved_cache = save_ml_trade(
                    ticker="CACHE_1788418524070.NS",
                    is_bullish=True,
                    entry=500.0,
                    sl=480.0,
                    tp1=520.0,
                    tp2=540.0,
                    confidence=65.0,
                    trade_type="INTRADAY"
                )
                self.assertFalse(saved_cache, "Failed to block CACHE_* fake ticker!")

                # Should reject TEMP_xxx
                saved_temp = save_ml_trade(
                    ticker="TEMP_MOCK.NS",
                    is_bullish=True,
                    entry=500.0,
                    sl=480.0,
                    tp1=520.0,
                    tp2=540.0,
                    confidence=65.0
                )
                self.assertFalse(saved_temp, "Failed to block TEMP_* fake ticker!")
        finally:
            if os.path.exists(temp_db.name):
                os.unlink(temp_db.name)

    def test_recovered_25h_research_job_report(self):
        """Verifies that the 25.29h 5Y LIVE_52 research job results are recovered and retrievable."""
        job_id = "res_20260831_154324_b84c83"
        res = research_job_manager.get_job_results(job_id)
        self.assertIsNotNone(res, "Could not retrieve results for 5Y LIVE_52 research job")
        
        summary = res.get("summary", {})
        metrics = res.get("metrics", {})
        
        self.assertEqual(summary.get("job_id"), job_id)
        self.assertEqual(summary.get("completed_tasks"), 41)
        self.assertEqual(summary.get("total_tasks"), 93)
        self.assertGreaterEqual(summary.get("duration_hours", 0), 24.0)
        self.assertEqual(metrics.get("models_fitted"), 615)
        self.assertEqual(metrics.get("rebalance_date_reached"), "2025-08-05")

    def test_gated_challenger_promotion_endpoint(self):
        """Verifies the gated Challenger promotion route enforces confirmation and hurdle checks."""
        from app.api.ml_lab import promote_foundation_challenger_api, FoundationPromoteRequest

        # Attempt without confirmation -> must reject
        req_unconfirmed = FoundationPromoteRequest(timeframe="swing", confirm_promotion=False)
        res_unconfirmed = promote_foundation_challenger_api(req_unconfirmed)
        self.assertEqual(res_unconfirmed.get("status"), "APPROVAL_REQUIRED")
        self.assertFalse(res_unconfirmed.get("gates_passed"))

        # Attempt with confirmation -> validates statistical hurdles
        req_confirmed = FoundationPromoteRequest(timeframe="swing", confirm_promotion=True)
        res_confirmed = promote_foundation_challenger_api(req_confirmed)
        self.assertIn(res_confirmed.get("status"), ["PROMOTION_VALIDATED", "REJECTED"])

    def test_broker_remains_fail_closed(self):
        """Verifies that broker execution remains safely in simulation fail-closed mode."""
        import asyncio
        from app.api.broker import execute_trade, ExecuteRequest
        req = ExecuteRequest(
            ticker="RELIANCE.NS",
            action="BUY",
            quantity=10,
            target=3100.0,
            stop_loss=2800.0,
            order_type="LIMIT",
            simulation=True
        )
        res = asyncio.run(execute_trade(req))
        self.assertTrue(res.get("simulation", True))
        self.assertEqual(res.get("status"), "success")
        self.assertIn("simulat", res.get("message", "").lower())

    def test_synthetic_pipeline_diagnostic_all_stages_pass(self):
        """Verifies that the synthetic pipeline diagnostic evaluates all 11 stages without corrupting state."""
        from app.analytics.synthetic_pipeline_tester import SyntheticPipelineTester
        res = SyntheticPipelineTester.run_diagnostic()
        self.assertEqual(res.get("status"), "PASS")
        self.assertTrue(res.get("overall_pass"))
        self.assertEqual(res.get("passed_stages"), 11)
        self.assertEqual(res.get("total_stages"), 11)

if __name__ == "__main__":
    unittest.main()
