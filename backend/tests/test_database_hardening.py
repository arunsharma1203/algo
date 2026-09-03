import unittest
import os
import time
import sqlite3
import tempfile
from unittest.mock import patch

from app.data.database import get_db_path, get_readonly_connection, is_canonical_path, get_connection
from app.analytics.system_health_center import SystemHealthCenter

class TestDatabaseArchitectureHardening(unittest.TestCase):
    """
    Fast, deterministic regression test suite (<5s) verifying Database Architecture Hardening,
    Path Disambiguation, Rogue Database Detection, and Read-Only Isolation Boundaries.
    """

    def test_01_canonical_path_resolution(self):
        """Resolver must return absolute path ending in backend/market_data.db."""
        path = get_db_path()
        self.assertTrue(os.path.isabs(path), f"Path {path} must be absolute.")
        self.assertTrue(path.endswith(os.path.join("backend", "market_data.db")), f"Path {path} does not point to backend/market_data.db")
        self.assertTrue(is_canonical_path(path))

    def test_02_cwd_independence_from_root_and_backend(self):
        """Resolver must return the exact same canonical path regardless of process CWD."""
        orig_cwd = os.getcwd()
        backend_dir = os.path.dirname(get_db_path())
        root_dir = os.path.abspath(os.path.join(backend_dir, ".."))

        try:
            # Change CWD to project root
            os.chdir(root_dir)
            path_from_root = get_db_path()

            # Change CWD to backend dir
            os.chdir(backend_dir)
            path_from_backend = get_db_path()

            # Change CWD to temporary directory
            with tempfile.TemporaryDirectory() as tmp_dir:
                os.chdir(tmp_dir)
                path_from_tmp = get_db_path()

            self.assertEqual(path_from_root, path_from_backend)
            self.assertEqual(path_from_root, path_from_tmp)
            self.assertTrue(path_from_root.endswith(os.path.join("backend", "market_data.db")))
        finally:
            os.chdir(orig_cwd)

    def test_03_no_fallback_to_root_database(self):
        """get_db_path must never return root market_data.db."""
        canonical = get_db_path()
        backend_dir = os.path.dirname(canonical)
        root_db = os.path.abspath(os.path.join(backend_dir, "..", "market_data.db"))
        self.assertNotEqual(canonical, root_db)
        self.assertFalse(is_canonical_path(root_db))

    def test_04_module_path_consistency_audit(self):
        """All critical backend modules must resolve the exact same canonical path."""
        audit = SystemHealthCenter._audit_module_path_consistency()
        self.assertTrue(audit["all_consistent"], f"Inconsistent modules: {audit.get('inconsistent_modules')}")
        self.assertEqual(len(audit["inconsistent_modules"]), 0)

    def test_05_rogue_database_detection(self):
        """Health check must detect root market_data.db and classify it as ROGUE_LEGACY_DATABASE."""
        rogue_report = SystemHealthCenter._detect_rogue_databases()
        self.assertIn("total_databases_found", rogue_report)
        self.assertIn("databases", rogue_report)

        # Check that canonical is identified
        canonical_items = [d for d in rogue_report["databases"] if d["classification"] == "CANONICAL_PRIMARY"]
        self.assertEqual(len(canonical_items), 1)
        self.assertTrue(canonical_items[0]["path"].endswith("backend/market_data.db"))

        # Check that rogue root database is identified if present
        rogue_items = [d for d in rogue_report["databases"] if d["classification"] == "ROGUE_LEGACY_DATABASE"]
        if rogue_items:
            for rog in rogue_items:
                self.assertIn("market_data.db", rog["filename"])
                self.assertEqual(rog["production_usage"], "IGNORED BY PRODUCTION RESOLVER")

    def test_06_backup_archives_audit(self):
        """Health check must inspect existing backups without modifying them."""
        backup_report = SystemHealthCenter._audit_backups()
        self.assertGreaterEqual(backup_report["backup_count"], 1)
        for b in backup_report["backups"]:
            self.assertTrue(b["readable"])
            self.assertIn(b["integrity"], ["ok", "UNKNOWN"])
            self.assertGreater(b["size_mb"], 0.0)

    def test_07_sqlite_readonly_connection(self):
        """get_readonly_connection must prevent any write or schema modification."""
        ro_conn = get_readonly_connection()
        cur = ro_conn.cursor()
        
        # Test reads succeed
        cur.execute("SELECT count(*) FROM ohlcv")
        cnt = cur.fetchone()[0]
        self.assertGreater(cnt, 0)

        # Test write fails immediately with readonly error
        with self.assertRaises(sqlite3.OperationalError):
            cur.execute("CREATE TABLE IF NOT EXISTS test_readonly_fail (id INTEGER PRIMARY KEY)")
        
        ro_conn.close()

    def test_08_database_health_latency_benchmark(self):
        """Database health check must execute in < 1.5 seconds."""
        t0 = time.perf_counter()
        db_res = SystemHealthCenter._check_database_health(deep=False)
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 1.5, f"Database health check took {elapsed:.2f}s, expected < 1.5s")
        self.assertIn(db_res["status"], ["HEALTHY", "WARNING"])
        self.assertIn("db_open_latency_ms", db_res["details"])
        self.assertIn("simple_query_latency_ms", db_res["details"])
        self.assertLess(db_res["details"]["db_open_latency_ms"], 50.0)

    def test_09_wal_mode_and_clean_health(self):
        """Canonical database must report WAL journal mode."""
        db_res = SystemHealthCenter._check_database_health(deep=False)
        self.assertEqual(db_res["details"]["journal_mode"], "WAL")
        self.assertIn(db_res["details"]["wal_health"], ["CHECKPOINTED / CLEAN", "ACTIVE_WAL"])

    def test_10_database_baselines_present(self):
        """Canonical database must contain required production tables and data baselines."""
        db_res = SystemHealthCenter._check_database_health(deep=False)
        details = db_res["details"]
        
        self.assertGreaterEqual(details["table_count"], 20)
        self.assertGreater(details["ohlcv_daily_rows"], 100000)
        self.assertGreaterEqual(details["ohlcv_daily_symbols"], 50)
        self.assertTrue(details["coverage_10y_verified"])
        self.assertGreater(details["table_row_counts"].get("ml_training_data", 0), 10000)
        self.assertGreater(details["table_row_counts"].get("ml_trade_history", 0), 30)

    def test_11_no_production_writes_during_health_check(self):
        """Database mtime must remain unmodified during read-only health checks."""
        canonical = get_db_path()
        mtime_before = os.path.getmtime(canonical)
        
        # Run health check
        SystemHealthCenter.run_quick_health_check()
        
        mtime_after = os.path.getmtime(canonical)
        self.assertEqual(mtime_before, mtime_after)

    def test_12_test_environment_isolation(self):
        """Temporary in-memory databases used for testing must be completely decoupled from production."""
        mem_conn = sqlite3.connect(":memory:")
        mem_conn.execute("CREATE TABLE mock_test (id INTEGER, val TEXT)")
        mem_conn.execute("INSERT INTO mock_test VALUES (1, 'mock')")
        mem_conn.commit()

        cur = mem_conn.cursor()
        cur.execute("SELECT count(*) FROM mock_test")
        self.assertEqual(cur.fetchone()[0], 1)
        mem_conn.close()

        # Canonical database remains completely unaffected
        prod_conn = get_readonly_connection()
        p_cur = prod_conn.cursor()
        p_cur.execute("SELECT count(*) FROM sqlite_master WHERE name='mock_test'")
        self.assertEqual(p_cur.fetchone()[0], 0)
        prod_conn.close()

if __name__ == "__main__":
    unittest.main()
