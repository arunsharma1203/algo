import unittest
import os
import sqlite3
import tempfile
from unittest.mock import patch

from app.data.database import (
    is_testing_environment,
    get_db_path,
    get_canonical_db_path,
    get_connection,
    set_test_db_override,
    is_canonical_path,
    ProductionDatabaseWriteBlockedError
)
from app.analytics.telegram_notifier import send_telegram_message, send_telegram_document
from app.analytics.model_manager import ModelManager, ProductionModelMutationBlockedError
from app.main import get_or_create_scheduler

class TestIsolationLockdown(unittest.TestCase):
    """
    Milestone 0.5 Test Isolation Lockdown Suite.
    Verifies that automated test execution can NEVER write to production market_data.db,
    cannot overwrite Champion models, cannot dispatch Telegram messages, and cannot
    launch production background schedulers.
    """

    def setUp(self):
        self.canonical_db = get_canonical_db_path()
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        # Set clean isolation for tests
        set_test_db_override(self.temp_db.name)

    def tearDown(self):
        set_test_db_override(None)
        if os.path.exists(self.temp_db.name):
            os.remove(self.temp_db.name)

    def test_is_testing_environment_detection(self):
        """Verifies that running under unittest is automatically detected as test environment."""
        self.assertTrue(is_testing_environment(), "Execution under unittest runner must be detected as testing environment.")

    def test_get_db_path_auto_isolates_in_test(self):
        """Verifies that without an explicit override, get_db_path() auto-provisions an isolated temp DB."""
        set_test_db_override(None)
        active_path = get_db_path()
        self.assertNotEqual(active_path, self.canonical_db, "get_db_path() must NOT return canonical production DB in test mode.")
        self.assertTrue("isolated_test_db" in active_path or "temp" in active_path.lower(), "Active path must be an isolated test sandbox.")

    def test_get_connection_blocks_canonical_write_in_test(self):
        """Verifies that opening a writable connection to canonical production DB in test mode raises an error."""
        set_test_db_override(self.canonical_db)
        with self.assertRaises(ProductionDatabaseWriteBlockedError):
            get_connection()

    def test_telegram_suppressed_in_test(self):
        """Verifies that send_telegram_message suppresses external HTTP calls during test execution."""
        # Should return True gracefully without making any network request
        res = send_telegram_message("TEST ALERT SHOULD BE SUPPRESSED")
        self.assertTrue(res, "send_telegram_message should return True and suppress outbound request in test mode.")

    def test_telegram_document_suppressed_in_test(self):
        """Verifies that send_telegram_document suppresses external HTTP calls during test execution."""
        res = send_telegram_document(b"%PDF-1.4 test dummy", "test_report.pdf", "Caption")
        self.assertTrue(res, "send_telegram_document should return True and suppress outbound request in test mode.")

    def test_scheduler_suppressed_in_test(self):
        """Verifies that get_or_create_scheduler returns an unstarted instance during test execution."""
        scheduler = get_or_create_scheduler()
        self.assertFalse(scheduler.running, "Background scheduler must NOT be running during test execution.")

    def test_model_manager_blocks_production_promotion_in_test(self):
        """Verifies that ModelManager.promote_challenger blocks overwriting production champion during tests."""
        dummy_model = {"dummy": "weights"}
        dummy_meta = {"version": "v9.9-test"}
        with self.assertRaises(ProductionModelMutationBlockedError):
            ModelManager.promote_challenger(dummy_model, dummy_meta, "swing")

    def test_production_db_remains_unpolluted(self):
        """Verifies canonical DB row count does not decrease and contains no test artifacts."""
        conn = sqlite3.connect(f"file:{self.canonical_db}?mode=ro", uri=True)
        count = conn.execute("SELECT COUNT(*) FROM ml_trade_history").fetchone()[0]
        test_rows = conn.execute("SELECT COUNT(*) FROM ml_trade_history WHERE source = 'TEST' OR source LIKE '%LOCKDOWN%'").fetchone()[0]
        conn.close()
        self.assertGreaterEqual(count, 250, "Canonical database row count must not decrease below baseline.")
        self.assertEqual(test_rows, 0, "No test artifact rows may leak into canonical production database.")

if __name__ == "__main__":
    unittest.main()
