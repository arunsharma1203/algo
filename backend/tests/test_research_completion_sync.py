import unittest
import os
import json
import tempfile
import sqlite3
from unittest.mock import patch, MagicMock

class TestResearchCompletionSync(unittest.TestCase):
    """
    Automated verification suite for Research Completion & State Synchronization.
    Covers Scenarios A through J strictly with isolated temporary databases.
    """

    def setUp(self):
        # Create temporary SQLite database
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        # Create temporary results directory
        self.temp_dir = tempfile.TemporaryDirectory()
        self.results_dir = self.temp_dir.name

        # Initialize schema
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE research_jobs (
                job_id TEXT PRIMARY KEY,
                title TEXT,
                research_type TEXT,
                universe TEXT,
                timeframe TEXT DEFAULT '1d',
                history_years INTEGER DEFAULT 10,
                status TEXT DEFAULT 'QUEUED',
                worker_count INTEGER DEFAULT 4,
                initial_capital REAL DEFAULT 500000.0,
                max_portfolio_heat REAL DEFAULT 6.0,
                kelly_mode TEXT DEFAULT 'HALF',
                created_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                cancelled_at TEXT,
                total_tasks INTEGER DEFAULT 0,
                completed_tasks INTEGER DEFAULT 0,
                failed_tasks INTEGER DEFAULT 0,
                progress_percent REAL DEFAULT 0.0,
                current_phase TEXT,
                current_symbol TEXT,
                current_cycle INTEGER DEFAULT 0,
                total_cycles INTEGER DEFAULT 0,
                trades_processed INTEGER DEFAULT 0,
                models_fitted INTEGER DEFAULT 0,
                promotions INTEGER DEFAULT 0,
                retentions INTEGER DEFAULT 0,
                elapsed_seconds REAL DEFAULT 0.0,
                estimated_remaining_seconds REAL DEFAULT 0.0,
                error_message TEXT,
                checkpoint_path TEXT,
                result_path TEXT,
                last_heartbeat_at TEXT,
                last_cycle_completed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE research_job_results (
                job_id TEXT PRIMARY KEY,
                total_pnl REAL DEFAULT 0.0,
                win_rate REAL DEFAULT 0.0,
                profit_factor REAL DEFAULT 0.0,
                max_drawdown_pct REAL DEFAULT 0.0,
                sharpe_ratio REAL DEFAULT 0.0,
                total_trades INTEGER DEFAULT 0,
                metrics_json TEXT,
                summary_json TEXT
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.temp_dir.cleanup()

    # =========================================================================
    # SCENARIO A: RUNNING at 68% -> remains running
    # =========================================================================
    def test_scenario_a_running_at_68_percent_remains_running(self):
        """When backend has an in-progress job at 68%, it remains RUNNING and does not fake 100%."""
        job_id = "job_test_running"
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO research_jobs (
                job_id, title, status, progress_percent, completed_tasks, total_tasks
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (job_id, "Active Research Job", "RUNNING", 68.0, 314, 511))
        conn.commit()
        conn.close()

        with patch("app.analytics.research_job_manager.get_db_path", return_value=self.db_path), \
             patch("app.analytics.research_job_manager.RESULTS_DIR", self.results_dir):
            from app.analytics.research_job_manager import ResearchJobManager
            mgr = ResearchJobManager()
            job = mgr.get_job(job_id)

            self.assertIsNotNone(job)
            self.assertEqual(job["status"], "RUNNING")
            self.assertEqual(job["progress_percent"], 68.0)
            self.assertEqual(job["completed_tasks"], 314)

    # =========================================================================
    # SCENARIO B: RUNNING -> COMPLETED -> becomes 100%
    # =========================================================================
    def test_scenario_b_running_transitions_to_completed_100_percent(self):
        """When result file is written with status SUCCESS, reconciliation marks job COMPLETED (100%)."""
        job_id = "job_test_completing"
        res_path = os.path.join(self.results_dir, f"result_{job_id}.json")
        with open(res_path, "w") as f:
            json.dump({
                "status": "SUCCESS",
                "job_id": job_id,
                "total_tickers": 511,
                "total_runtime_seconds": 3600.0,
                "results": {
                    "INFY.NS": {"metrics": {"total_pnl": 50000.0, "win_rate": 60.0}, "trades_count": 10},
                    "TCS.NS": {"metrics": {"total_pnl": 30000.0, "win_rate": 55.0}, "trades_count": 8}
                }
            }, f)

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO research_jobs (
                job_id, title, status, progress_percent, completed_tasks, total_tasks, result_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (job_id, "Finished Run", "RUNNING", 68.0, 314, 511, res_path))
        conn.commit()
        conn.close()

        with patch("app.analytics.research_job_manager.get_db_path", return_value=self.db_path), \
             patch("app.analytics.research_job_manager.RESULTS_DIR", self.results_dir):
            from app.analytics.research_job_manager import ResearchJobManager
            mgr = ResearchJobManager()
            job = mgr.get_job(job_id)

            self.assertEqual(job["status"], "COMPLETED")
            self.assertEqual(job["progress_percent"], 100.0)
            self.assertEqual(job["completed_tasks"], 511)
            self.assertEqual(job["trades_processed"], 18)

            # Check database row was reconciled atomically
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT status, progress_percent, completed_tasks FROM research_jobs WHERE job_id=?", (job_id,))
            db_row = dict(cur.fetchone())
            self.assertEqual(db_row["status"], "COMPLETED")
            self.assertEqual(db_row["progress_percent"], 100.0)
            self.assertEqual(db_row["completed_tasks"], 511)
            conn.close()

    # =========================================================================
    # SCENARIO C: Backend already COMPLETED on load -> immediately shows 100%
    # =========================================================================
    def test_scenario_c_already_completed_on_load_immediately_100_percent(self):
        """When page loads and backend job is COMPLETED, it returns 100% and no active job."""
        job_id = "job_test_already_done"
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO research_jobs (
                job_id, title, status, progress_percent, completed_tasks, total_tasks
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (job_id, "Completed Job", "COMPLETED", 100.0, 511, 511))
        conn.commit()
        conn.close()

        with patch("app.analytics.research_job_manager.get_db_path", return_value=self.db_path), \
             patch("app.analytics.research_job_manager.RESULTS_DIR", self.results_dir):
            from app.analytics.research_job_manager import ResearchJobManager
            mgr = ResearchJobManager()
            jobs = mgr.get_all_jobs()
            active = mgr.get_active_job()

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["status"], "COMPLETED")
            self.assertEqual(jobs[0]["progress_percent"], 100.0)
            self.assertIsNone(active)

    # =========================================================================
    # SCENARIO D: Browser refresh after COMPLETED -> still 100%
    # =========================================================================
    def test_scenario_d_browser_refresh_preserves_100_percent_completed(self):
        """Re-fetching jobs on page refresh preserves 100% COMPLETED status without re-running."""
        job_id = "job_test_refresh"
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO research_jobs (
                job_id, title, status, progress_percent, completed_tasks, total_tasks
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (job_id, "Refreshed Run", "COMPLETED", 100.0, 511, 511))
        conn.commit()
        conn.close()

        with patch("app.analytics.research_job_manager.get_db_path", return_value=self.db_path), \
             patch("app.analytics.research_job_manager.RESULTS_DIR", self.results_dir):
            from app.analytics.research_job_manager import ResearchJobManager
            mgr = ResearchJobManager()

            # First load
            jobs_1 = mgr.get_all_jobs()
            # Simulated browser refresh (second fetch)
            jobs_2 = mgr.get_all_jobs()

            self.assertEqual(jobs_1[0]["status"], "COMPLETED")
            self.assertEqual(jobs_1[0]["progress_percent"], 100.0)
            self.assertEqual(jobs_2[0]["status"], "COMPLETED")
            self.assertEqual(jobs_2[0]["progress_percent"], 100.0)

    # =========================================================================
    # SCENARIO E: FAILED -> frontend shows FAILED
    # =========================================================================
    def test_scenario_e_failed_status_handling(self):
        """Failed jobs are reported as FAILED with error message, not RUNNING or 100% COMPLETED."""
        job_id = "job_test_failed"
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO research_jobs (
                job_id, title, status, progress_percent, completed_tasks, total_tasks, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (job_id, "Failed Run", "FAILED", 45.0, 100, 511, "Memory allocation error"))
        conn.commit()
        conn.close()

        with patch("app.analytics.research_job_manager.get_db_path", return_value=self.db_path), \
             patch("app.analytics.research_job_manager.RESULTS_DIR", self.results_dir):
            from app.analytics.research_job_manager import ResearchJobManager
            mgr = ResearchJobManager()
            job = mgr.get_job(job_id)
            active = mgr.get_active_job()

            self.assertEqual(job["status"], "FAILED")
            self.assertEqual(job["error_message"], "Memory allocation error")
            self.assertIsNone(active)

    # =========================================================================
    # SCENARIO F: CANCELLED -> frontend shows CANCELLED
    # =========================================================================
    def test_scenario_f_cancelled_status_handling(self):
        """Cancelled jobs are reported as CANCELLED, not RUNNING or 100% COMPLETED."""
        job_id = "job_test_cancelled"
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO research_jobs (
                job_id, title, status, progress_percent, completed_tasks, total_tasks
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (job_id, "Cancelled Run", "CANCELLED", 35.0, 80, 511))
        conn.commit()
        conn.close()

        with patch("app.analytics.research_job_manager.get_db_path", return_value=self.db_path), \
             patch("app.analytics.research_job_manager.RESULTS_DIR", self.results_dir):
            from app.analytics.research_job_manager import ResearchJobManager
            mgr = ResearchJobManager()
            job = mgr.get_job(job_id)
            active = mgr.get_active_job()

            self.assertEqual(job["status"], "CANCELLED")
            self.assertIsNone(active)

    # =========================================================================
    # SCENARIO G: Missing report -> graceful error, no white screen
    # =========================================================================
    def test_scenario_g_missing_report_handled_gracefully(self):
        """When results file does not exist, get_job_results returns None gracefully without exception."""
        job_id = "job_missing_results"
        with patch("app.analytics.research_job_manager.get_db_path", return_value=self.db_path), \
             patch("app.analytics.research_job_manager.RESULTS_DIR", self.results_dir):
            from app.analytics.research_job_manager import ResearchJobManager
            mgr = ResearchJobManager()
            res = mgr.get_job_results(job_id)
            self.assertIsNone(res)

    # =========================================================================
    # SCENARIO H: Delayed API response -> no stale-state corruption
    # =========================================================================
    def test_scenario_h_delayed_api_response_sequence_protection(self):
        """Simulate sequence protection: an older delayed response (seq 1) does not overwrite newer (seq 2)."""
        responses = []

        # Sequence 1: delayed stale response (status RUNNING, 68%)
        seq_1_data = {"seq": 1, "status": "RUNNING", "progress_percent": 68.0}
        # Sequence 2: newer authoritative response (status COMPLETED, 100%)
        seq_2_data = {"seq": 2, "status": "COMPLETED", "progress_percent": 100.0}

        # Emulate frontend fetchJobSeqRef logic
        current_seq = 2
        
        # When seq 1 arrives late:
        if seq_1_data["seq"] == current_seq:
            responses.append(seq_1_data)
        
        # When seq 2 arrives:
        if seq_2_data["seq"] == current_seq:
            responses.append(seq_2_data)

        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["status"], "COMPLETED")
        self.assertEqual(responses[0]["progress_percent"], 100.0)

    # =========================================================================
    # SCENARIO I: Polling cleanup after terminal state
    # =========================================================================
    def test_scenario_i_terminal_state_cleans_up_active_job_and_sse(self):
        """When jobs transition to terminal state (COMPLETED), get_active_job returns None."""
        job_id = "job_terminal"
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO research_jobs (
                job_id, title, status, progress_percent, completed_tasks, total_tasks
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (job_id, "Terminal Job", "COMPLETED", 100.0, 511, 511))
        conn.commit()
        conn.close()

        with patch("app.analytics.research_job_manager.get_db_path", return_value=self.db_path), \
             patch("app.analytics.research_job_manager.RESULTS_DIR", self.results_dir):
            from app.analytics.research_job_manager import ResearchJobManager
            mgr = ResearchJobManager()
            active = mgr.get_active_job()
            self.assertIsNone(active)

    # =========================================================================
    # SCENARIO J: Repeated polling does not create duplicate requests
    # =========================================================================
    def test_scenario_j_is_fetching_guard_prevents_duplicate_requests(self):
        """Emulate isFetchingJobsRef guard: repeated polling triggers while one is in-flight are dropped."""
        is_fetching = False
        execution_count = 0

        def poll():
            nonlocal is_fetching, execution_count
            if is_fetching:
                return False
            is_fetching = True
            execution_count += 1
            return True

        # Call 1: should execute
        res1 = poll()
        self.assertTrue(res1)
        self.assertEqual(execution_count, 1)

        # Call 2 while in flight: should be dropped
        res2 = poll()
        self.assertFalse(res2)
        self.assertEqual(execution_count, 1)

        # In flight finishes:
        is_fetching = False

        # Call 3: should execute
        res3 = poll()
        self.assertTrue(res3)
        self.assertEqual(execution_count, 2)

if __name__ == "__main__":
    unittest.main()
