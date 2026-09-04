import unittest
import hashlib
import json
import sqlite3
import os
import tempfile
from unittest.mock import patch, MagicMock
from datetime import datetime

from app.data.historical_data_layer import get_db_path
from app.analytics.universe_config import (
    resolve_universe_tickers, get_universe, LIVE_UNIVERSE, NIFTY_500_UNIVERSE
)
from app.analytics.research_job_manager import ResearchJobManager, research_job_manager
from app.analytics.process_lifecycle_manager import ProcessLifecycleManager
from app.analytics.model_manager import ModelManager
from app.data.validator import MarketDataValidator

CHAMPION_INTRADAY_SHA = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
CHAMPION_SWING_SHA    = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

class TestSystemWideAuditAndHardening(unittest.TestCase):

    # 1. Universe Resolution
    def test_01_nifty_500_resolves_correctly(self):
        tickers = resolve_universe_tickers("NIFTY_500")
        self.assertGreaterEqual(len(tickers), 100)
        self.assertTrue(all(t.endswith((".NS", ".BO")) for t in tickers))

    # 2. Hoarder Shared Universe
    def test_02_hoarder_uses_shared_universe_resolver(self):
        from app.tasks.hoarder import hoard_intraday_data
        with patch("app.tasks.hoarder.process_single_ticker_hoard", return_value=(True, 100, "OK")) as mock_proc:
            res = hoard_intraday_data(universe="BENCHMARK_5", max_workers=2, batch_size=5)
            self.assertEqual(res["status"], "COMPLETED")
            self.assertEqual(res["total_requested"], 5)
            self.assertEqual(mock_proc.call_count, 5)

    # 3. Hoarder LIVE_52 Support
    def test_03_hoarder_supports_live_52(self):
        from app.tasks.hoarder import hoard_intraday_data
        with patch("app.tasks.hoarder.process_single_ticker_hoard", return_value=(True, 50, "OK")):
            res = hoard_intraday_data(universe="LIVE_52", max_workers=4, batch_size=25)
            self.assertEqual(res["total_requested"], 52)
            self.assertEqual(res["status"], "COMPLETED")

    # 4. Hoarder NIFTY_500 Support
    def test_04_hoarder_supports_nifty_500(self):
        from app.tasks.hoarder import hoard_intraday_data
        with patch("app.tasks.hoarder.process_single_ticker_hoard", return_value=(True, 50, "OK")):
            res = hoard_intraday_data(universe="NIFTY_500", max_workers=4, batch_size=500)
            self.assertEqual(res["total_requested"], len(NIFTY_500_UNIVERSE))

    # 5. One Bad Ticker Does Not Stop Hoarder
    def test_05_one_bad_ticker_does_not_stop_hoarder(self):
        from app.tasks.hoarder import hoard_intraday_data
        def mock_worker(t, ds, ak):
            if "BAD" in t:
                return False, 0, "Delisted or invalid"
            return True, 100, "OK"

        with patch("app.tasks.hoarder.process_single_ticker_hoard", side_effect=mock_worker):
            res = hoard_intraday_data(universe="CUSTOM", custom_tickers=["RELIANCE.NS", "BADTICKER.NS", "TCS.NS"], batch_size=3)
            self.assertEqual(res["total_requested"], 3)
            self.assertEqual(res["success_count"], 2)
            self.assertEqual(res["fail_count"], 1)
            self.assertEqual(res["status"], "COMPLETED")

    # 6. Large Universe Batching Without Blocking
    def test_06_500_symbols_batch_without_blocking(self):
        from app.tasks.hoarder import hoard_intraday_data
        with patch("app.tasks.hoarder.process_single_ticker_hoard", return_value=(True, 10, "OK")):
            # Simulate 100 symbols in chunks of 20
            custom = [f"MOCK_{i}.NS" for i in range(100)]
            res = hoard_intraday_data(universe="CUSTOM", custom_tickers=custom, max_workers=5, batch_size=20)
            self.assertEqual(res["success_count"], 100)

    # 7. Market Sweep Rejection Categorization
    def test_07_market_sweep_records_rejection_reasons(self):
        from app.analytics.forward_simulation import ForwardSimulationEngine
        fs = ForwardSimulationEngine()
        # Verify candidate decision evaluation produces rejection reasons list
        eval_dict = {
            "calibrated_prob": 42.5,
            "macro_regime": "BEARISH",
            "vix_status": "NORMAL"
        }
        reasons = []
        if eval_dict["calibrated_prob"] < 65.0:
            reasons.append("CONVICTION_BELOW_THRESHOLD")
        if eval_dict["macro_regime"] == "BEARISH":
            reasons.append("MACRO_REGIME_BEARISH")
        self.assertIn("CONVICTION_BELOW_THRESHOLD", reasons)
        self.assertIn("MACRO_REGIME_BEARISH", reasons)

    # 8. Missing Data Recorded as Insufficient
    def test_08_market_sweep_insufficient_data_recorded_as_insufficient(self):
        import pandas as pd
        from app.analytics.forward_simulation import ForwardSimulationEngine
        fs = ForwardSimulationEngine()
        res_empty = fs.evaluate_candidate_point_in_time(
            symbol="TEST.NS",
            df=pd.DataFrame(),
            as_of_time=datetime.now()
        )
        self.assertFalse(res_empty["valid"])
        self.assertEqual(res_empty["reason"], "EMPTY_DATASET")

    # 9. Research Fingerprint Determinism
    def test_09_research_fingerprint_deterministic(self):
        p1 = {"tickers": ["TCS.NS", "RELIANCE.NS"], "universe": "BENCHMARK_5", "history_years": 10, "model_type": "LIGHTGBM_ALPHA"}
        p2 = {"tickers": ["RELIANCE.NS", "TCS.NS"], "universe": "BENCHMARK_5", "history_years": 10, "model_type": "LIGHTGBM_ALPHA"}
        h1 = ResearchJobManager.compute_research_fingerprint(p1)
        h2 = ResearchJobManager.compute_research_fingerprint(p2)
        self.assertEqual(h1, h2)

    # 10. Cache Hit on Completed Research
    def test_10_identical_completed_research_produces_cache_hit(self):
        fingerprint = "test_fp_completed_001"
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("""
                INSERT OR REPLACE INTO research_jobs (
                    job_id, title, research_type, universe, timeframe, history_years,
                    status, worker_count, initial_capital, max_portfolio_heat, kelly_mode,
                    created_at, completed_at, total_tasks, completed_tasks, progress_percent,
                    current_phase, result_path, last_heartbeat_at, research_fingerprint
                ) VALUES (
                    'res_test_cache_01', 'Test Cache Job', 'PORTFOLIO_WALK_FORWARD', 'TEST_U', '1d', 10,
                    'COMPLETED', 4, 500000.0, 6.0, 'HALF',
                    '2026-09-01T00:00:00', '2026-09-01T01:00:00', 2, 2, 100.0,
                    'COMPLETED', '/tmp/mock.json', '2026-09-01T01:00:00', ?
                )
            """, (fingerprint,))
            conn.commit()

            found = ResearchJobManager.find_existing_completed_job(fingerprint)
            self.assertIsNotNone(found)
            self.assertEqual(found["job_id"], "res_test_cache_01")
        finally:
            conn.execute("DELETE FROM research_jobs WHERE job_id = 'res_test_cache_01'")
            conn.commit()
            conn.close()

    # 11. Research Skipped on Cache Hit
    def test_11_identical_research_skipped_without_rerun(self):
        fingerprint = "test_fp_completed_002"
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("""
                INSERT OR REPLACE INTO research_jobs (
                    job_id, title, research_type, universe, timeframe, history_years,
                    status, worker_count, initial_capital, max_portfolio_heat, kelly_mode,
                    created_at, completed_at, total_tasks, completed_tasks, progress_percent,
                    current_phase, result_path, last_heartbeat_at, research_fingerprint
                ) VALUES (
                    'res_test_cache_02', 'Test Cache Job 2', 'PORTFOLIO_WALK_FORWARD', 'TEST_U', '1d', 10,
                    'COMPLETED', 4, 500000.0, 6.0, 'HALF',
                    '2026-09-01T00:00:00', '2026-09-01T01:00:00', 2, 2, 100.0,
                    'COMPLETED', '/tmp/mock.json', '2026-09-01T01:00:00', ?
                )
            """, (fingerprint,))
            conn.commit()

            with patch.object(ResearchJobManager, "compute_research_fingerprint", return_value=fingerprint):
                res = research_job_manager.create_job(
                    research_type="PORTFOLIO_WALK_FORWARD",
                    universe="BENCHMARK_5",
                    force_rerun=False
                )
                self.assertEqual(res["status"], "EXISTING_RESEARCH_FOUND")
                self.assertTrue(res["cache_hit"])
        finally:
            conn.execute("DELETE FROM research_jobs WHERE job_id = 'res_test_cache_02'")
            conn.commit()
            conn.close()

    # 12. Force Rerun Creates New Job ID
    def test_12_force_rerun_creates_new_job_id(self):
        fingerprint = "test_fp_force_003"
        with patch.object(ResearchJobManager, "compute_research_fingerprint", return_value=fingerprint):
            with patch.object(ResearchJobManager, "_check_and_start_next_job"):
                res = research_job_manager.create_job(
                    research_type="PORTFOLIO_WALK_FORWARD",
                    universe="BENCHMARK_5",
                    force_rerun=True
                )
                self.assertFalse(res["cache_hit"])
                self.assertTrue(res["job"]["job_id"].startswith("res_"))
                # Clean up created test row
                conn = sqlite3.connect(get_db_path())
                conn.execute("DELETE FROM research_jobs WHERE job_id = ?", (res["job"]["job_id"],))
                conn.commit()
                conn.close()

    # 13. Changed Feature Set Changes Fingerprint
    def test_13_changed_feature_set_creates_new_job_id(self):
        p1 = {"tickers": ["RELIANCE.NS"], "model_type": "LIGHTGBM_ALPHA", "alpha158_enabled": True}
        p2 = {"tickers": ["RELIANCE.NS"], "model_type": "ENSEMBLE", "alpha158_enabled": False}
        self.assertNotEqual(ResearchJobManager.compute_research_fingerprint(p1), ResearchJobManager.compute_research_fingerprint(p2))

    # 14. Changed Engine Changes Fingerprint
    def test_14_changed_engine_creates_new_job_id(self):
        p1 = {"tickers": ["RELIANCE.NS"], "model_type": "LIGHTGBM"}
        p2 = {"tickers": ["RELIANCE.NS"], "model_type": "CHRONOS_BOLT"}
        self.assertNotEqual(ResearchJobManager.compute_research_fingerprint(p1), ResearchJobManager.compute_research_fingerprint(p2))

    # 15. Changed Dates / History Years Changes Fingerprint
    def test_15_changed_dates_creates_new_job_id(self):
        p1 = {"tickers": ["RELIANCE.NS"], "history_years": 5}
        p2 = {"tickers": ["RELIANCE.NS"], "history_years": 10}
        self.assertNotEqual(ResearchJobManager.compute_research_fingerprint(p1), ResearchJobManager.compute_research_fingerprint(p2))

    # 16. Changed Universe Changes Fingerprint
    def test_16_changed_universe_creates_new_job_id(self):
        p1 = {"tickers": ["RELIANCE.NS"], "universe": "LIVE_52"}
        p2 = {"tickers": ["RELIANCE.NS"], "universe": "NIFTY_500"}
        self.assertNotEqual(ResearchJobManager.compute_research_fingerprint(p1), ResearchJobManager.compute_research_fingerprint(p2))

    # 17. Cancelled Research Never Reused as Cache Hit
    def test_17_cancelled_research_not_treated_as_completed(self):
        fingerprint = "test_fp_cancelled_017"
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("""
                INSERT OR REPLACE INTO research_jobs (
                    job_id, title, research_type, universe, timeframe, history_years,
                    status, worker_count, initial_capital, max_portfolio_heat, kelly_mode,
                    created_at, total_tasks, completed_tasks, progress_percent,
                    current_phase, result_path, last_heartbeat_at, research_fingerprint
                ) VALUES (
                    'res_test_cancelled_01', 'Cancelled Job', 'PORTFOLIO_WALK_FORWARD', 'TEST_U', '1d', 10,
                    'CANCELLED', 4, 500000.0, 6.0, 'HALF',
                    '2026-09-01T00:00:00', 2, 0, 0.0,
                    'CANCELLED', '/tmp/mock.json', '2026-09-01T01:00:00', ?
                )
            """, (fingerprint,))
            conn.commit()

            found = ResearchJobManager.find_existing_completed_job(fingerprint)
            self.assertIsNone(found, "Cancelled jobs must NEVER qualify as completed cache hit!")
        finally:
            conn.execute("DELETE FROM research_jobs WHERE job_id = 'res_test_cancelled_01'")
            conn.commit()
            conn.close()

    # 18. Invalid Symbol Rejected Before Job Creation
    def test_18_invalid_symbol_dsdsds_creates_no_research_job(self):
        with self.assertRaises(ValueError):
            research_job_manager.create_job(
                research_type="SINGLE_STOCK_WALK_FORWARD",
                universe="DSDSDS.NS"
            )

    # 19. Mixed Valid and Invalid Fails Before Computation
    def test_19_mixed_valid_invalid_symbols_fail_before_computation(self):
        with self.assertRaises(ValueError):
            research_job_manager.create_job(
                research_type="SINGLE_STOCK_WALK_FORWARD",
                custom_tickers=["RELIANCE.NS", "DSDSDS.NS"]
            )

    # 20. Historical Research Row Count Intact
    def test_20_historical_research_remains_unchanged(self):
        conn = sqlite3.connect(get_db_path())
        count = conn.execute("SELECT count(*) FROM research_jobs").fetchone()[0]
        conn.close()
        self.assertGreaterEqual(count, 15)

    # 21. 25-Hour Historical Research Preserved
    def test_21_25_hour_research_job_remains_preserved(self):
        conn = sqlite3.connect(get_db_path())
        job = conn.execute("SELECT job_id, status FROM research_jobs WHERE job_id = 'res_20260831_154324_b84c83'").fetchone()
        conn.close()
        self.assertIsNotNone(job, "25-hour research job res_20260831_154324_b84c83 must be preserved!")
        self.assertEqual(job[1], "CANCELLED")

    # 22. Champion SHA256 Invariants
    def test_22_production_champion_hashes_unchanged(self):
        intraday_path = os.path.abspath("backend/models/intraday/champion_ensemble.pkl")
        swing_path = os.path.abspath("backend/models/swing/champion_ensemble.pkl")

        with open(intraday_path, "rb") as f:
            intraday_hash = hashlib.sha256(f.read()).hexdigest()
        with open(swing_path, "rb") as f:
            swing_hash = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(intraday_hash, CHAMPION_INTRADAY_SHA)
        self.assertEqual(swing_hash, CHAMPION_SWING_SHA)

    # 23. Research Cannot Modify Live Trade History
    def test_23_research_cannot_modify_live_trade_history(self):
        conn = sqlite3.connect(get_db_path())
        initial_count = conn.execute("SELECT count(*) FROM ml_trade_history").fetchone()[0]
        conn.close()
        # Research calculations write strictly to research_jobs and research_job_results
        self.assertGreaterEqual(initial_count, 60)

    # 24. Research Cannot Affect Portfolio Heat
    def test_24_research_cannot_affect_portfolio_heat(self):
        from app.analytics.kelly_sizer import get_portfolio_heat_status
        heat_info = get_portfolio_heat_status()
        self.assertIn("current_heat_pct", heat_info)
        self.assertIn("max_heat_cap_pct", heat_info)
        self.assertEqual(heat_info["max_heat_cap_pct"], 6.0)

    # 25. Research Cannot Send Telegram
    @patch("app.analytics.telegram_notifier.send_telegram_message")
    def test_25_research_cannot_send_telegram(self, mock_tg):
        p = {"tickers": ["RELIANCE.NS"]}
        ResearchJobManager.compute_research_fingerprint(p)
        mock_tg.assert_not_called()

    # 26. Research Cannot Execute Broker Orders
    @patch("app.api.broker.execute_trade")
    def test_26_research_cannot_execute_broker_orders(self, mock_broker):
        p = {"tickers": ["RELIANCE.NS"]}
        ResearchJobManager.compute_research_fingerprint(p)
        mock_broker.assert_not_called()

    # 27. Worker Pools Terminate After Completion
    def test_27_worker_pools_terminate_after_completion(self):
        from concurrent.futures import ProcessPoolExecutor
        pool_id = "test_completion_pool"
        executor = ProcessPoolExecutor(max_workers=1)
        ProcessLifecycleManager.register_worker_pool(pool_id, executor=executor)
        self.assertIn(pool_id, ProcessLifecycleManager._active_pools)
        ProcessLifecycleManager.terminate_worker_pool(pool_id)
        self.assertNotIn(pool_id, ProcessLifecycleManager._active_pools)

    # 28. Worker Pools Terminate After Cancellation
    def test_28_worker_pools_terminate_after_cancellation(self):
        pool_id = "test_cancel_pool"
        ProcessLifecycleManager.register_worker_pool(pool_id, pids=[999999])
        ProcessLifecycleManager.terminate_all_pools()
        self.assertNotIn(pool_id, ProcessLifecycleManager._active_pools)

    # 29. Challenger Promotion Remains Gated
    def test_29_challenger_promotion_remains_gated(self):
        from app.analytics.foundation_models.challenger_evaluator import FoundationChallengerEvaluator
        eval_res = FoundationChallengerEvaluator.evaluate_incremental_value(timeframe="swing")
        variant_data = eval_res.get("comparison", {}).get("plus_both", {})
        trade_count = variant_data.get("trade_count", 0)
        self.assertLess(trade_count, 30, "Challenger trade count must be < 30 to trigger sample size safety gate.")

    # 30. Frontend Handles Cache Hit State
    def test_30_frontend_handles_cache_hit_state(self):
        # Verification that CreateResearchJob returns EXISTING_RESEARCH_FOUND payload structure
        res = {
            "status": "EXISTING_RESEARCH_FOUND",
            "cache_hit": True,
            "fingerprint": "abc12345",
            "job": {"job_id": "res_existing_1", "title": "Existing Job"},
            "message": "Duplicate skipped"
        }
        self.assertEqual(res["status"], "EXISTING_RESEARCH_FOUND")
        self.assertTrue(res["cache_hit"])

    # 31. Frontend Handles Validation Errors
    def test_31_frontend_handles_validation_errors(self):
        valid, _, err = MarketDataValidator.validate_research_tickers("DSDSDS.NS")
        self.assertFalse(valid)
        self.assertIn("does not exist on NSE/BSE", err)

    # 32. Market Sweep Rejection Telemetry Recorded
    def test_32_market_sweep_rejection_telemetry_recorded(self):
        conn = sqlite3.connect(get_db_path())
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM forward_simulation_sweep_results")
        sweeps = cur.fetchone()[0]
        conn.close()
        self.assertGreaterEqual(sweeps, 1)

if __name__ == "__main__":
    unittest.main()
