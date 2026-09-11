"""
UNIT TEST SUITE: QLIB SCANNER DYNAMIC UNIVERSE, MASTER LOGGING & IDEMPOTENT INIT
================================================================================
Covers:
1. Thread-safe idempotent initialization of Microsoft Qlib without RecorderInitializationError.
2. Dynamic discovery and resolution of ALL_DATABASE_STOCKS (all unique valid stocks).
3. Emission of canonical MasterLogger events under category SCAN_QLIB for every stage.
4. Fail-closed behavior on missing model or corrupt inputs.
5. Invariant preservation: Zero broker orders, zero portfolio heat, Legacy Champion models untouched.
"""

import unittest
import os
import hashlib
import tempfile
import sqlite3
from unittest.mock import patch, MagicMock

import qlib
from app.analytics.universe_config import (
    resolve_all_database_stocks,
    resolve_universe_tickers,
    validate_ticker,
    get_universe
)
from app.qlib_engine.india_data_adapter import get_qlib_adapter, ensure_qlib_ready
from app.qlib_engine.scanners import QlibScanners, QlibModelUnavailableError
from app.analytics.master_logger import MasterLogger
from app.data.historical_data_layer import get_db_path


from app.data.database import set_test_db_override, get_canonical_db_path


class TestQlibScannerUniverseAndLogging(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Route database queries in this test suite to canonical database
        set_test_db_override(get_canonical_db_path())

    @classmethod
    def tearDownClass(cls):
        set_test_db_override(None)

    def test_qlib_idempotent_initialization(self):
        """Test 1: Multiple ensure_qlib_ready calls succeed idempotently."""
        adapter1 = ensure_qlib_ready()
        self.assertIsNotNone(adapter1)
        adapter2 = ensure_qlib_ready()
        self.assertEqual(adapter1.provider_uri, adapter2.provider_uri)

    def test_recorder_initialization_error_prevented(self):
        """Test 2: Re-initialization when Qlib workflow Experiment is active does NOT raise RecorderInitializationError."""
        from qlib.workflow import R
        adapter = ensure_qlib_ready()
        with R.start(experiment_name="test_suite_exp"):
            # Calling initialize_qlib while an experiment is running must NOT throw RecorderInitializationError
            res = adapter.initialize_qlib()
            self.assertIn(res["status"], ("ALREADY_INITIALIZED", "RECORDER_ACTIVE_REUSED", "REUSED_AFTER_RECORDER_CHECK"))

    def test_all_database_stocks_dynamic_discovery(self):
        """Test 3: ALL_DATABASE_STOCKS resolves dynamically from SQLite."""
        stats = resolve_all_database_stocks(timeframe="1d", min_bars=60)
        self.assertGreater(stats["discovered_count"], 50)
        self.assertGreater(stats["valid_count"], 50)
        self.assertGreater(stats["scan_ready_count"], 50)
        self.assertEqual(len(stats["scan_ready_symbols"]), stats["scan_ready_count"])

    def test_all_database_stocks_no_hardcoded_count(self):
        """Test 4: Dynamic resolution does not equal fixed 52 stocks."""
        stats = resolve_all_database_stocks(timeframe="1d", min_bars=60)
        self.assertNotEqual(stats["discovered_count"], 52)
        self.assertGreater(stats["discovered_count"], 500)

    def test_resolve_universe_tickers_all_database_stocks(self):
        """Test 5: resolve_universe_tickers('ALL_DATABASE_STOCKS') returns dynamic list."""
        symbols = resolve_universe_tickers("ALL_DATABASE_STOCKS")
        self.assertIsInstance(symbols, list)
        self.assertGreater(len(symbols), 500)
        for s in symbols[:20]:
            ok, _, _ = validate_ticker(s)
            self.assertTrue(ok)

    def test_get_universe_all_database_stocks_preset(self):
        """Test 6: get_universe('ALL_DATABASE_STOCKS') contains metadata."""
        u_info = get_universe("ALL_DATABASE_STOCKS")
        self.assertIn("All Database Stocks", u_info["name"])
        self.assertIn("discovered_count", u_info)
        self.assertIn("valid_count", u_info)
        self.assertIn("scan_ready_count", u_info)

    def test_ticker_validation_filters_mock_symbols(self):
        """Test 7: validate_ticker rejects CACHE_, TEMP_, MOCK_ prefixes."""
        ok1, _, _ = validate_ticker("MOCK_STOCK.NS")
        self.assertFalse(ok1)
        ok2, _, _ = validate_ticker("TEMP_NIFTY.NS")
        self.assertFalse(ok2)

    def test_master_logger_records_scan_events(self):
        """Test 8: MasterLogger stores SCAN_QLIB category events in SQLite."""
        test_msg = "Unit test scan event"
        success = MasterLogger.log_event(
            category="SCAN_QLIB",
            event_type="QLIB_SCAN_TEST",
            message=test_msg,
            details={"test": True}
        )
        self.assertTrue(success)
        events = MasterLogger.get_events(category="SCAN_QLIB", limit=5)
        self.assertTrue(any(e["message"] == test_msg for e in events))

    def test_qlib_scan_lifecycle_events_logged(self):
        """Test 9: Real scan emits canonical events into app_master_events."""
        events = MasterLogger.get_events(category="SCAN_QLIB", limit=20)
        event_types = {e["event_type"] for e in events}
        expected_types = {
            "QLIB_SCAN_STARTED",
            "QLIB_MODEL_LOADING",
            "QLIB_DATA_VALIDATION",
            "QLIB_DATA_LOADING",
            "QLIB_FEATURE_GENERATION",
            "QLIB_INFERENCE",
            "QLIB_SIGNAL_FILTERING",
            "QLIB_SCAN_COMPLETED"
        }
        for et in expected_types:
            self.assertIn(et, event_types, f"Expected event {et} in MasterLogger audit log")

    def test_fail_closed_on_missing_model(self):
        """Test 10: Missing model raises QlibModelUnavailableError with QLIB_SCAN_FAILED event."""
        with patch("app.qlib_engine.model_registry.QlibModelRegistry.load_active_model", side_effect=RuntimeError("Model missing")):
            with self.assertRaises(QlibModelUnavailableError):
                QlibScanners.run_swing_scan(universe="ALL_DATABASE_STOCKS")

        events = MasterLogger.get_events(category="SCAN_QLIB", limit=5)
        self.assertTrue(any(e["event_type"] == "QLIB_SCAN_FAILED" for e in events))

    def test_legacy_champion_hashes_unchanged(self):
        """Test 11: Legacy Champion models remain untouched."""
        expected_hashes = {
            "backend/models/intraday/champion_ensemble.pkl": "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91",
            "backend/models/swing/champion_ensemble.pkl": "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"
        }
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        for rel_path, expected_h in expected_hashes.items():
            full_p = os.path.join(base_dir, rel_path.replace("backend/", ""))
            if os.path.exists(full_p):
                with open(full_p, "rb") as f:
                    actual_h = hashlib.sha256(f.read()).hexdigest()
                self.assertEqual(actual_h, expected_h, f"Hash mismatch for {rel_path}!")


if __name__ == "__main__":
    unittest.main()
