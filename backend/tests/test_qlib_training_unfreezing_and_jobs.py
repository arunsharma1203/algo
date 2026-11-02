import unittest
import time
from unittest.mock import patch, MagicMock
from app.qlib_engine.training_job_tracker import TrainingJobTracker
from app.qlib_engine.training_telemetry import TrainingTelemetryManager
from app.qlib_engine.best_model_finder import BestModelFinder


class TestQlibTrainingUnfreezingAndJobs(unittest.TestCase):

    def setUp(self):
        self.tracker = TrainingJobTracker()
        self.telemetry = TrainingTelemetryManager()
        self.telemetry.reset()

    def tearDown(self):
        self.tracker.cancel_all_active_jobs(reason="Test tearDown")
        self.telemetry.reset()

    def test_cancel_all_active_jobs_and_reaping(self):
        # Create a test running job
        test_job_id = f"test_job_{int(time.time())}"
        self.tracker.create_job(
            job_id=test_job_id,
            strategy="SWING",
            universe="NIFTY_500_ESTABLISHED",
            feature_family="Alpha158",
            model_family="LGBModel"
        )
        active = self.tracker.get_active_job()
        self.assertIsNotNone(active)
        self.assertEqual(active["job_id"], test_job_id)

        # Cancel all active jobs
        cancelled = self.tracker.cancel_all_active_jobs(reason="Test cancel")
        self.assertGreaterEqual(cancelled, 1)

        # Now active job must be None
        active_after = self.tracker.get_active_job()
        self.assertIsNone(active_after)

    def test_telemetry_reset_returns_idle(self):
        state = self.telemetry.reset()
        self.assertEqual(state["status"], "IDLE")
        self.assertEqual(state["progress_pct"], 0.0)
        self.assertIsNone(state["job_id"])

    def test_record_error_alias_fails_job(self):
        self.telemetry.start_job(
            job_id="test_rec_error",
            strategy="SWING",
            universe_name="TEST_UNI"
        )
        res = self.telemetry.record_error("Testing record_error alias")
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("Testing record_error alias", res["error"])

    def test_start_mission_async_returns_job_id_and_starts_telemetry(self):
        with patch.object(BestModelFinder, 'run_research_mission') as mock_run:
            res = BestModelFinder.start_mission_async(
                universe="BOOTSTRAP_35",
                friction_bps=25.0,
                strategy="SWING"
            )
            self.assertIn("job_id", res)
            self.assertIn("mission_id", res)
            self.assertEqual(res["job_id"], res["mission_id"])

            tel = self.telemetry.get_latest()
            self.assertEqual(tel["job_id"], res["job_id"])
            self.assertEqual(tel["status"], "RUNNING")

            # Clean up
            self.telemetry.reset()
            self.tracker.cancel_all_active_jobs(reason="Test done")


if __name__ == "__main__":
    unittest.main()

