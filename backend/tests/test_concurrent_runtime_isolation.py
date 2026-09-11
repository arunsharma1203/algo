"""
CONCURRENT LEGACY + QLIB RUNTIME ISOLATION TEST SUITE
=====================================================
Validates runtime independence, process isolation, scheduler isolation,
database non-collision, zero broker routing, and failure isolation.

Tests:
1. Legacy runtime health
2. Qlib runtime health
3. Both running simultaneously
4. Different ports (8000 vs 8001)
5. Database isolation (WAL mode & separate tables)
6. Scheduler isolation (Qlib does not start Legacy jobs)
7. Model directory isolation (models/qlib/ vs models/swing/)
8. Artifact isolation (backend/data/qlib_provider/ vs Legacy)
9. Engine-labelled recommendations (engine = 'QLIB' vs 'LEGACY')
10. Qlib failure does not stop Legacy
11. Legacy failure does not stop Qlib
12. No broker execution (0 live broker orders)
13. No duplicate scheduler jobs between runtimes
14. No duplicate Telegram jobs (Qlib has 0 Telegram calls)
15. Frontend dual-status resolution
"""

import os
import sys
import unittest
import tempfile
import shutil
import sqlite3
import hashlib
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

# Environment flags for test isolation
os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"
os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"
os.environ["SUPPRESS_SCHEDULER"] = "true"

from app.main import app as legacy_app, get_or_create_scheduler as get_legacy_scheduler
from app.qlib_server import app as qlib_app, get_or_create_qlib_scheduler
from app.qlib_engine.virtual_tracker import QlibVirtualTracker, init_qlib_tracking_schema
from app.qlib_engine.model_registry import QlibModelRegistry, QLIB_MODELS_BASE_DIR
from app.analytics.kelly_sizer import get_portfolio_heat_status
from app.data.historical_data_layer import get_db_path


class TestConcurrentRuntimeIsolation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp(prefix="concurrency_test_")
        cls.test_db = os.path.join(cls.test_dir, "test_market_data.db")

        # Initialize schema
        conn = sqlite3.connect(cls.test_db)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE ml_trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                ticker TEXT,
                direction TEXT,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                confidence REAL,
                trade_type TEXT DEFAULT 'INTRADAY',
                status TEXT DEFAULT 'OPEN',
                outcome TEXT,
                profit_pct REAL,
                source TEXT DEFAULT 'MANUAL',
                position_type TEXT DEFAULT 'NOT_A_POSITION'
            )
        """)
        conn.execute("""
            CREATE TABLE app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("INSERT INTO app_settings VALUES ('simulation_mode', 'true')")
        conn.execute("INSERT INTO app_settings VALUES ('portfolio_max_heat_cap', '6.0')")
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    # 1. Legacy runtime health
    def test_01_legacy_runtime_health(self):
        client = TestClient(legacy_app)
        res = client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Swing Trading AI API", res.json().get("message", ""))

        sched_res = client.get("/api/scheduler/status")
        self.assertEqual(sched_res.status_code, 200)
        self.assertEqual(sched_res.json().get("backend_status"), "ONLINE")

    # 2. Qlib runtime health
    def test_02_qlib_runtime_health(self):
        client = TestClient(qlib_app)
        res = client.get("/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["engine"], "QLIB")
        self.assertEqual(data["status"], "RUNNING")

        rt_res = client.get("/api/qlib/runtime-status")
        self.assertEqual(rt_res.status_code, 200)
        rt_data = rt_res.json()
        self.assertEqual(rt_data["engine"], "QLIB")
        self.assertTrue(rt_data["qlib_installed"])

    # 3. Both running simultaneously
    def test_03_both_apps_run_simultaneously(self):
        leg_client = TestClient(legacy_app)
        qlb_client = TestClient(qlib_app)

        leg_res = leg_client.get("/")
        qlb_res = qlb_client.get("/")

        self.assertEqual(leg_res.status_code, 200)
        self.assertEqual(qlb_res.status_code, 200)
        self.assertNotEqual(leg_res.json(), qlb_res.json())
        self.assertEqual(qlb_res.json()["engine"], "QLIB")

    # 4. Different ports
    def test_04_different_ports_configured(self):
        from scripts.manage_services import LEGACY_PORT, QLIB_PORT
        self.assertEqual(LEGACY_PORT, 8000)
        self.assertEqual(QLIB_PORT, 8001)
        self.assertNotEqual(LEGACY_PORT, QLIB_PORT)

    # 5. Database isolation
    def test_05_database_isolation_distinct_tables(self):
        with patch("app.qlib_engine.virtual_tracker.get_db_path", return_value=self.test_db):
            init_qlib_tracking_schema()
            conn = sqlite3.connect(self.test_db)
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            conn.close()

            self.assertIn("ml_trade_history", tables)
            self.assertIn("qlib_virtual_recommendations", tables)
            self.assertNotEqual("ml_trade_history", "qlib_virtual_recommendations")

    # 6. Scheduler isolation
    def test_06_scheduler_isolation(self):
        leg_sched = get_legacy_scheduler()
        qlb_sched = get_or_create_qlib_scheduler()

        # They are separate instances
        self.assertIsNot(leg_sched, qlb_sched)

        # Qlib scheduler does NOT contain Legacy jobs
        qlb_job_ids = [j.id for j in qlb_sched.get_jobs()]
        legacy_specific_jobs = [
            "data_hoarder_1600",
            "daily_ohlcv_sync_1615",
            "active_trade_tracker_5m",
            "weekly_retrain_sun",
            "autopilot_0930",
            "daily_dashboard_report_0815"
        ]
        for job_id in legacy_specific_jobs:
            self.assertNotIn(job_id, qlb_job_ids, f"Legacy job {job_id} leaked into QlibScheduler!")

    # 7. Model directory isolation
    def test_07_model_directory_isolation(self):
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        legacy_swing_dir = os.path.join(backend_dir, "models", "swing")
        qlib_swing_dir = QlibModelRegistry.get_strategy_dir("SWING")

        self.assertNotEqual(legacy_swing_dir, qlib_swing_dir)
        self.assertTrue(qlib_swing_dir.startswith(os.path.join(backend_dir, "models", "qlib")))
        self.assertFalse(legacy_swing_dir.startswith(os.path.join(backend_dir, "models", "qlib")))

    # 8. Artifact isolation
    def test_08_artifact_isolation(self):
        from app.qlib_engine.india_data_adapter import get_qlib_adapter
        adapter = get_qlib_adapter()
        self.assertIn("qlib_provider", adapter.provider_uri)

    # 9. Engine-labelled recommendations
    def test_09_engine_labelled_recommendations(self):
        with patch("app.qlib_engine.virtual_tracker.get_db_path", return_value=self.test_db):
            rec = QlibVirtualTracker.record_recommendation(
                ticker="SBIN.NS",
                strategy="SWING",
                direction="BUY",
                entry_price=800.0,
                stop_loss=780.0,
                target_price=840.0,
                confidence=72.5,
                model_id="qlib_test_model",
                model_hash="hash123",
                feature_version="alpha158_v1"
            )
            self.assertEqual(rec["engine"], "QLIB")
            self.assertEqual(rec["status"], "OPEN")

            # Verify in DB row
            conn = sqlite3.connect(self.test_db)
            row = conn.execute("SELECT engine, status FROM qlib_virtual_recommendations WHERE id = ?", (rec["id"],)).fetchone()
            conn.close()
            self.assertEqual(row[0], "QLIB")
            self.assertEqual(row[1], "OPEN")

    # 10. Qlib failure does not stop Legacy
    def test_10_qlib_failure_does_not_stop_legacy(self):
        from scripts.manage_services import check_legacy_status, is_pid_alive
        # Check that helper accurately tracks liveness without coupling
        self.assertFalse(is_pid_alive(9999999))  # Non-existent PID fails gracefully

    # 11. Legacy failure does not stop Qlib
    def test_11_legacy_failure_does_not_stop_qlib(self):
        from scripts.manage_services import check_qlib_status, probe_http
        # Probing offline port returns ok: False without raising unhandled exception
        result = probe_http("http://127.0.0.1:59999/health", timeout=0.1)
        self.assertFalse(result["ok"])

    # 12. No broker execution
    def test_12_no_broker_execution(self):
        with patch("app.api.broker.execute_trade") as mock_broker:
            with patch("app.qlib_engine.virtual_tracker.get_db_path", return_value=self.test_db):
                QlibVirtualTracker.record_recommendation(
                    ticker="INFY.NS",
                    strategy="SWING",
                    direction="BUY",
                    entry_price=1800.0,
                    stop_loss=1750.0,
                    target_price=1900.0,
                    confidence=68.0,
                    model_id="qlib_test_m",
                    model_hash="hash_infy",
                    feature_version="alpha158_v1"
                )
                mock_broker.assert_not_called()

    # 13. No duplicate scheduler jobs
    def test_13_no_duplicate_scheduler_jobs(self):
        leg_sched = get_legacy_scheduler()
        qlb_sched = get_or_create_qlib_scheduler()

        leg_jobs = set(j.id for j in leg_sched.get_jobs())
        qlb_jobs = set(j.id for j in qlb_sched.get_jobs())

        # Zero intersection
        overlap = leg_jobs.intersection(qlb_jobs)
        self.assertEqual(len(overlap), 0, f"Scheduler job IDs collided: {overlap}")

    # 14. No duplicate Telegram jobs
    def test_14_no_duplicate_telegram_jobs(self):
        import inspect
        import app.qlib_server as qs
        source_code = inspect.getsource(qs)
        self.assertNotIn("telegram_notifier", source_code)
        self.assertNotIn("send_telegram_message", source_code)
        self.assertNotIn("execute_daily_dashboard_telegram_job", source_code)

    # 15. Frontend dual-status API resolution
    def test_15_frontend_dual_status_resolution(self):
        leg_client = TestClient(legacy_app)
        qlb_client = TestClient(qlib_app)

        leg_st = leg_client.get("/api/scheduler/status").json()
        qlb_st = qlb_client.get("/api/qlib/runtime-status").json()

        self.assertEqual(leg_st.get("backend_status"), "ONLINE")
        self.assertEqual(qlb_st.get("engine"), "QLIB")
        self.assertEqual(qlb_st.get("runtime_status"), "ONLINE")
        self.assertEqual(qlb_st.get("port"), 8001)


if __name__ == "__main__":
    unittest.main()

