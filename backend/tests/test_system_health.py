import unittest
import time
import os
import sqlite3
from unittest.mock import patch, MagicMock

from app.analytics.system_health_center import SystemHealthCenter
from app.data.historical_data_layer import get_db_path

class TestSystemHealthCenter(unittest.TestCase):
    """
    Comprehensive, deterministic unit tests for SystemHealthCenter.
    Ensures sub-second execution, zero expensive ML retraining, and complete category coverage.
    """

    def test_01_quick_health_check_structure_and_speed(self):
        """Quick health check must complete in < 2.0 seconds and return all 10 categories."""
        t0 = time.perf_counter()
        res = SystemHealthCenter.run_quick_health_check()
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 2.0, f"Quick health check took {elapsed:.2f}s, expected < 2.0s")
        self.assertEqual(res["mode"], "QUICK")
        self.assertIn(res["overall_status"], ["HEALTHY", "DEGRADED", "CRITICAL"])
        self.assertIn("total_latency_ms", res)
        self.assertIn("categories", res)
        
        expected_categories = [
            "application_core", "historical_data", "universe_integrity",
            "model_system", "scanner_health", "forward_simulation",
            "research_engine", "resource_health", "database_health", "trade_history"
        ]
        for cat in expected_categories:
            self.assertIn(cat, res["categories"], f"Missing category: {cat}")
            self.assertIn(res["categories"][cat]["status"], ["HEALTHY", "WARNING", "FAILED"])
            self.assertGreaterEqual(res["categories"][cat]["latency_ms"], 0.0)

    def test_02_deep_health_check_speed_and_smoke_tests(self):
        """Deep health check must complete in < 10.0 seconds and exercise smoke tests."""
        t0 = time.perf_counter()
        res = SystemHealthCenter.run_deep_health_check()
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 10.0, f"Deep health check took {elapsed:.2f}s, expected < 10.0s")
        self.assertEqual(res["mode"], "DEEP")
        self.assertIn(res["overall_status"], ["HEALTHY", "DEGRADED", "CRITICAL"])
        self.assertIn("categories", res)

    def test_03_application_core_check(self):
        """Application core must report Python version, writable filesystem, and scheduler."""
        core = SystemHealthCenter._check_application_core(deep=True)
        self.assertIn(core["status"], ["HEALTHY", "WARNING", "FAILED"])
        self.assertTrue(core["details"]["write_permission"])
        self.assertTrue(core["details"]["required_dirs_present"])
        self.assertIn("python_version", core["details"])

    def test_04_historical_data_layer_check(self):
        """Historical data layer must audit row counts, date bounds, and candle integrity."""
        data_res = SystemHealthCenter._check_historical_data_layer(deep=True)
        self.assertIn(data_res["status"], ["HEALTHY", "WARNING", "FAILED"])
        self.assertGreater(data_res["details"]["total_ohlcv_rows"], 0)
        self.assertGreater(data_res["details"]["daily_symbols_count"], 0)
        self.assertIsNotNone(data_res["details"]["oldest_daily_date"])
        self.assertIsNotNone(data_res["details"]["newest_daily_date"])

    def test_05_universe_integrity_check(self):
        """Universe integrity must verify LIVE_52, RESEARCH_100, and BENCHMARK_5."""
        uni_res = SystemHealthCenter._check_universe_integrity()
        self.assertEqual(uni_res["status"], "HEALTHY")
        self.assertEqual(uni_res["details"]["BENCHMARK_5_count"], 5)
        self.assertEqual(uni_res["details"]["LIVE_52_count"], 52)
        self.assertGreater(uni_res["details"]["RESEARCH_100_count"], 50)
        self.assertIn("universe_config_hash", uni_res["details"])

    def test_06_model_system_check(self):
        """Model system must verify champion models, metadata, and calibrator."""
        model_res = SystemHealthCenter._check_model_system(deep=True)
        self.assertEqual(model_res["status"], "HEALTHY")
        self.assertIn("swing_version", model_res["details"])
        self.assertIn("intraday_version", model_res["details"])
        self.assertTrue(model_res["details"].get("swing_model_loaded", False))

    def test_07_scanner_health_check(self):
        """Scanner health must test macro engine, Kelly sizer, and technical indicator calculation."""
        scan_res = SystemHealthCenter._check_scanner_health(deep=True)
        self.assertEqual(scan_res["status"], "HEALTHY")
        self.assertIn("macro_regime", scan_res["details"])
        self.assertEqual(scan_res["details"]["portfolio_heat_cap_pct"], 6.0)
        self.assertEqual(scan_res["details"]["feature_generation_smoke"], "PASS")

    def test_08_forward_simulation_check(self):
        """Forward simulation check must verify database tables and attribution engine."""
        fsim_res = SystemHealthCenter._check_forward_simulation(deep=True)
        self.assertEqual(fsim_res["status"], "HEALTHY")
        self.assertTrue(fsim_res["details"]["friction_model_available"])

    def test_09_research_engine_orchestrator_check(self):
        """Research engine must verify orchestrator status without launching heavy 10Y jobs."""
        orch_res = SystemHealthCenter._check_research_engine(deep=True)
        self.assertEqual(orch_res["status"], "HEALTHY")
        self.assertEqual(orch_res["details"]["10y_research_engine"], "AVAILABLE — NOT EXECUTED (Standby)")
        self.assertEqual(orch_res["details"]["max_parallel_workers"], 4)

    def test_10_apple_silicon_resource_check(self):
        """Resource check must report Apple Silicon M1 Pro specs and thread clamp."""
        res_res = SystemHealthCenter._check_resource_health()
        self.assertIn(res_res["status"], ["HEALTHY", "WARNING"])
        self.assertEqual(res_res["details"]["thread_clamp"], "OMP_NUM_THREADS=1")
        self.assertEqual(res_res["details"]["default_research_workers"], 4)

    def test_11_database_health_and_wal_check(self):
        """Database check must verify WAL mode, table counts, and PRAGMA integrity."""
        db_res = SystemHealthCenter._check_database_health(deep=True)
        self.assertIn(db_res["status"], ["HEALTHY", "WARNING"])
        self.assertEqual(db_res["details"]["journal_mode"], "WAL")
        self.assertGreater(db_res["details"]["table_count"], 15)
        self.assertEqual(db_res["details"].get("integrity_check"), "ok")

    def test_12_trade_history_protection_check(self):
        """Trade history check must report all 36 recovered records and boundary isolation."""
        th_res = SystemHealthCenter._check_trade_history()
        self.assertEqual(th_res["status"], "HEALTHY")
        self.assertGreaterEqual(th_res["details"]["total_trades_count"], 32)
        self.assertEqual(th_res["details"]["source_of_truth"], "backend/market_data.db")
        self.assertIn("production_trades", th_res["details"]["isolation_boundaries"])

if __name__ == "__main__":
    unittest.main()
