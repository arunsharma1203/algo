import os
import sys
import unittest
import time
from typing import Dict, Any

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.data.historical_data_layer import HistoricalDataLayer
from app.analytics.parallel_engine import ParallelWalkForwardOrchestrator, ResearchConfig

class TestParallelBenchmark(unittest.TestCase):
    """
    Automated Benchmark & Verification Suite for Apple Silicon (M1 Pro)
    Parallel Walk-Forward Execution Engine.
    """

    def setUp(self):
        HistoricalDataLayer.init_schema()

    def test_system_resource_profile(self):
        """Verifies Apple Silicon hardware topology detection."""
        prof = HistoricalDataLayer.get_system_resource_profile()
        self.assertIn("performance_cores", prof)
        self.assertIn("recommended_workers", prof)
        self.assertGreaterEqual(prof["total_logical_cpus"], 4)
        self.assertEqual(prof["recommended_ml_threads"], 1)

    def test_feature_caching(self):
        """Verifies point-in-time feature caching returns consistent DataFrames."""
        df1 = HistoricalDataLayer.get_cached_features("RELIANCE.NS", timeframe="1d")
        df2 = HistoricalDataLayer.get_cached_features("RELIANCE.NS", timeframe="1d")
        self.assertFalse(df1.empty)
        self.assertEqual(len(df1), len(df2))
        self.assertIn("rsi", df1.columns)
        self.assertIn("macd_diff", df1.columns)

    def test_checkpoint_lifecycle(self):
        """Tests checkpoint saving, reading, and clearing."""
        config = ResearchConfig(checkpoint_dir="backend/checkpoints_test", enable_checkpointing=True)
        orchestrator = ParallelWalkForwardOrchestrator(config)
        job_id = "test_chk_001"

        dummy_data = {"RELIANCE.NS": {"status": "SUCCESS", "pnl": 1000.0}}
        orchestrator.save_checkpoint(job_id, dummy_data)

        loaded = orchestrator.load_checkpoint(job_id)
        self.assertEqual(loaded.get("RELIANCE.NS", {}).get("status"), "SUCCESS")

        orchestrator.clear_checkpoint(job_id)
        self.assertEqual(orchestrator.load_checkpoint(job_id), {})

    def test_parallel_vs_sequential_result_equivalence(self):
        """
        Executes 1 worker (sequential) vs 2 workers (parallel) on local data.
        Asserts 100% mathematical equivalence of metrics and trade counts.
        """
        tickers = ["RELIANCE.NS", "TCS.NS"]

        # 1. Sequential Run (1 Worker)
        seq_config = ResearchConfig(max_workers=1, enable_checkpointing=False)
        seq_orchestrator = ParallelWalkForwardOrchestrator(seq_config)
        seq_res = seq_orchestrator.run_universe_walk_forward(tickers, job_id="test_seq")

        # 2. Parallel Run (2 Workers)
        par_config = ResearchConfig(max_workers=2, enable_checkpointing=False)
        par_orchestrator = ParallelWalkForwardOrchestrator(par_config)
        par_res = par_orchestrator.run_universe_walk_forward(tickers, job_id="test_par")

        self.assertEqual(seq_res["status"], "SUCCESS")
        self.assertEqual(par_res["status"], "SUCCESS")

        # Assert equivalence across all tickers
        for t in tickers:
            seq_t = seq_res["results"].get(t, {})
            par_t = par_res["results"].get(t, {})

            self.assertEqual(seq_t.get("status"), par_t.get("status"))
            self.assertEqual(seq_t.get("trades_count"), par_t.get("trades_count"))
            self.assertEqual(
                seq_t.get("metrics", {}).get("total_pnl"),
                par_t.get("metrics", {}).get("total_pnl")
            )
            self.assertEqual(
                seq_t.get("metrics", {}).get("win_rate"),
                par_t.get("metrics", {}).get("win_rate")
            )

if __name__ == '__main__':
    unittest.main()

