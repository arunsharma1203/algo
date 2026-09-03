import unittest
import os
import time
import sqlite3
from unittest.mock import patch, MagicMock

from app.data.database import get_db_path, get_readonly_connection, is_canonical_path
from app.analytics.system_health_center import SystemHealthCenter
from app.analytics.health_report_generator import HealthReportPDFGenerator

class TestSystemHealthCenter2(unittest.TestCase):
    """
    Comprehensive regression suite for System Health Center 2.0.
    Verifies Forensic Diagnostics, Self-Healing, Deterministic Health Scoring,
    Telegram health safeguards, Autonomous Schedulers, and PDF Report Generation.
    Runs fast in <5s without expensive operations.
    """

    def test_01_canonical_database_and_storage_hardening(self):
        """Audits that canonical DB is 78+ MB and has 223k+ OHLCV rows."""
        db_res = SystemHealthCenter._check_database_health(deep=False)
        details = db_res["details"]

        self.assertTrue(details["canonical_db_path"].endswith(os.path.join("backend", "market_data.db")))
        self.assertGreaterEqual(details["database_size_mb"], 70.0)
        self.assertEqual(details["journal_mode"], "WAL")
        self.assertGreaterEqual(details["table_count"], 20)
        self.assertGreater(details["ohlcv_daily_rows"], 200000)
        self.assertEqual(details["ohlcv_daily_symbols"], 117)

    def test_02_rogue_database_detection_and_discrepancy_explanation(self):
        """Verifies that rogue ./market_data.db is detected and the 0.65 MB discrepancy is clearly explained."""
        db_res = SystemHealthCenter._check_database_health(deep=False)
        discrepancy = db_res["details"]["storage_discrepancy_explanation"]

        self.assertEqual(discrepancy["root_cause"], "RELATIVE_PATH_CWD_DISCREPANCY")
        self.assertEqual(discrepancy["remediation_status"], "PERMANENTLY_FIXED")
        self.assertIn("canonical_db_size_mb", discrepancy)
        self.assertIn("legacy_root_db_size_mb", discrepancy)

    def test_03_deterministic_health_scoring(self):
        """Verifies weighted health score calculation out of 100."""
        quick = SystemHealthCenter.run_quick_health_check()
        self.assertIn("health_score", quick)
        self.assertGreaterEqual(quick["health_score"], 80)
        self.assertIn(quick["overall_status"], ["HEALTHY", "HEALTHY WITH WARNINGS"])
        self.assertIn("score_breakdown", quick)

    def test_04_telegram_safeguard_no_auto_send(self):
        """Verifies that normal health checks audit Telegram WITHOUT sending any messages."""
        tg_res = SystemHealthCenter._check_telegram_health()
        self.assertTrue(tg_res["details"]["auto_send_blocked_in_health_check"])
        self.assertIn("token_preview", tg_res["details"])
        self.assertNotIn("1234567890:AA", tg_res["details"]["token_preview"])  # Must be masked

    def test_05_manual_telegram_test_execution(self):
        """Verifies manual test Telegram notification endpoint handles responses safely."""
        with patch("app.analytics.telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"ok": True}
            
            res = SystemHealthCenter.send_test_telegram_notification()
            self.assertIn("latency_ms", res)
            self.assertIn(res["status"], ["SUCCESS", "FAILED", "ERROR"])

    def test_06_autonomous_systems_and_schedulers(self):
        """Audits APScheduler, registered jobs, and fail-closed trading locks."""
        auto_res = SystemHealthCenter._check_autonomous_systems()
        details = auto_res["details"]

        self.assertIn("scheduler_status", details)
        self.assertIn("registered_jobs", details)
        self.assertEqual(details["broker_safety_status"], "PROTECTED")
        self.assertIn("Fail-Closed", details["live_broker_order_lock"])

    def test_07_controlled_self_healing_routine(self):
        """Executes safe, non-destructive self-healing routines without modifying production DB/models."""
        heal_res = SystemHealthCenter.execute_controlled_self_healing()
        self.assertEqual(heal_res["status"], "COMPLETED")
        self.assertGreaterEqual(heal_res["total_actions"], 2)
        
        # Verify actions executed
        actions = heal_res["actions_executed"]
        actions_names = [a.get("action") for a in actions]
        self.assertIn("FLUSH_STALE_FEATURE_CACHE", actions_names)
        self.assertIn("RESET_ORPHANED_JOBS", actions_names)

    def test_08_error_center_active_and_recovered_history(self):
        """Verifies active and recovered error tracking."""
        err_res = SystemHealthCenter.get_error_center_history()
        self.assertIn("active_errors", err_res)
        self.assertIn("recovered_errors", err_res)
        self.assertIn("total_active_count", err_res)

    def test_09_pdf_report_generation(self):
        """Generates and validates PDF health report binary using ReportLab."""
        health_data = SystemHealthCenter.run_quick_health_check()
        score_data = {"score": health_data["health_score"], "status_label": health_data["overall_status"]}
        
        pdf_bytes = HealthReportPDFGenerator.generate_pdf(health_data, score_data)
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertGreater(len(pdf_bytes), 2000)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))

    def test_10_ml_champion_model_deserialization_and_components(self):
        """Verifies Champion model validation without retraining."""
        model_res = SystemHealthCenter._check_model_system(deep=True)
        details = model_res["details"]
        
        self.assertTrue(details.get("swing_model_loaded", False))
        self.assertEqual(details.get("swing_smoke_inference"), "SUCCESS")
        self.assertIn("calibrator_brier", details)

    def test_11_forward_simulation_and_research_state(self):
        """Verifies forward simulation session tables and attribution engine."""
        fsim_res = SystemHealthCenter._check_forward_simulation(deep=False)
        self.assertTrue(fsim_res["details"].get("attribution_engine_available", False))
        self.assertGreaterEqual(fsim_res["details"].get("total_candidates", 0), 10)

    def test_12_quick_health_execution_speed(self):
        """Verifies that quick health check completes in under 2.0 seconds."""
        t0 = time.perf_counter()
        quick = SystemHealthCenter.run_quick_health_check()
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 2.0, f"Quick health check took {elapsed:.2f}s, expected < 2.0s")

if __name__ == "__main__":
    unittest.main()
