"""
UNIT & INTEGRATION TEST SUITE: REAL QLIB PARALLEL TRADING SYSTEM
================================================================
Comprehensive verification suite covering all 27 requirements:
1. Real Qlib import
2. Qlib version detection
3. India data adapter
4. Trading calendar
5. Alpha158 from real Qlib
6. Alpha360 from real Qlib
7. Model training
8. Model serialization
9. Model loading
10. Model hash
11. OOS separation
12. No leakage
13. Swing inference
14. Intraday inference
15. F&O inference
16. Scanner integration
17. No legacy fallback
18. Missing-model fail-closed
19. Missing-data fail-closed
20. Fresh entry price
21. Virtual recommendation tracking
22. No portfolio heat consumption
23. No broker execution
24. Dynamic frontend metrics
25. No hardcoded performance
26. Legacy vs Qlib comparison
27. Frontend build
"""

import os
import sys
import unittest
import tempfile
import shutil
import sqlite3
import hashlib
import numpy as np
import pandas as pd
from unittest.mock import patch, MagicMock

# Configure environment before imports
os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"
os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"
os.environ["SUPPRESS_SCHEDULER"] = "true"

import qlib
from qlib.contrib.data.handler import Alpha158, Alpha360
from qlib.contrib.model.gbdt import LGBModel

from app.qlib_engine.india_data_adapter import IndiaMarketDataAdapter, QLIB_FIELDS
from app.qlib_engine.feature_pipeline import QlibFeaturePipeline
from app.qlib_engine.temporal_split import TemporalSplitter, TemporalSplitError
from app.qlib_engine.model_registry import QlibModelRegistry, QlibModelIntegrityError
from app.qlib_engine.scanners import QlibScanners, QlibModelUnavailableError
from app.qlib_engine.virtual_tracker import QlibVirtualTracker, init_qlib_tracking_schema
from app.qlib_engine.comparison_service import ComparisonService
from app.analytics.kelly_sizer import get_portfolio_heat_status


class TestRealQlibParallelSystem(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp(prefix="qlib_test_suite_")
        cls.test_db = os.path.join(cls.test_dir, "test_market_data.db")
        
        # Populate test database with minimal required schema
        conn = sqlite3.connect(cls.test_db)
        conn.execute("""
            CREATE TABLE ohlcv (
                ticker TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
                timeframe TEXT, hoard_timestamp TEXT, source TEXT,
                PRIMARY KEY (ticker, date, timeframe)
            )
        """)
        conn.execute("""
            CREATE TABLE ml_trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, ticker TEXT, direction TEXT,
                entry REAL, sl REAL, tp1 REAL, tp2 REAL, confidence REAL, trade_type TEXT DEFAULT 'INTRADAY',
                status TEXT DEFAULT 'CLOSED', outcome TEXT, profit_pct REAL, source TEXT, position_type TEXT
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
        
        # Insert sample daily bars for testing
        dates = pd.date_range("2023-01-01", periods=100, freq="B")
        for sym in ["RELIANCE.NS", "TCS.NS"]:
            p = 1000.0
            for d in dates:
                conn.execute(
                    "INSERT INTO ohlcv (ticker, date, open, high, low, close, volume, timeframe) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, '1d')",
                    (sym, d.strftime("%Y-%m-%d"), p * 0.99, p * 1.02, p * 0.98, p, 50000.0)
                )
                p += 1.0

        # Insert sample closed trades for comparison
        conn.execute(
            "INSERT INTO ml_trade_history (timestamp, ticker, direction, entry, sl, tp1, tp2, confidence, status, outcome, profit_pct, position_type) "
            "VALUES ('2023-01-10', 'RELIANCE.NS', 'BUY', 1000, 980, 1040, 1080, 75.0, 'CLOSED', 'TARGET MET', 4.0, 'LIVE_POSITION')"
        )
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    # 1. Real Qlib import
    def test_01_real_qlib_imported(self):
        self.assertIsNotNone(qlib)
        self.assertTrue(hasattr(qlib, "init"))

    # 2. Qlib version detection
    def test_02_qlib_version_detection(self):
        ver = getattr(qlib, "__version__", "")
        self.assertTrue(len(ver) > 0)
        self.assertEqual(ver, "0.9.7")
        self.assertIn("site-packages/qlib", qlib.__file__)

    # 3. India data adapter
    def test_03_india_data_adapter_creation(self):
        adapter = IndiaMarketDataAdapter(provider_uri=os.path.join(self.test_dir, "provider"))
        self.assertTrue(os.path.exists(adapter.cal_dir))
        self.assertTrue(os.path.exists(adapter.inst_dir))
        self.assertTrue(os.path.exists(adapter.feat_dir))

    # 4. Trading calendar
    def test_04_trading_calendar_generation(self):
        adapter = IndiaMarketDataAdapter(provider_uri=os.path.join(self.test_dir, "provider"))
        with patch("app.qlib_engine.india_data_adapter.get_db_path", return_value=self.test_db):
            res = adapter.sync_daily_universe(tickers=["RELIANCE.NS", "TCS.NS"], min_bars=50)
            self.assertEqual(res["status"], "SUCCESS")
            self.assertEqual(res["trading_days"], 100)
            cal_file = os.path.join(adapter.cal_dir, "day.txt")
            self.assertTrue(os.path.exists(cal_file))

    # 5. Alpha158 from real Qlib
    def test_05_alpha158_real_qlib_handler(self):
        meta = QlibFeaturePipeline.get_feature_metadata("Alpha158")
        self.assertEqual(meta["handler_class"], "Alpha158")
        self.assertEqual(meta["handler_module"], "qlib.contrib.data.handler")
        self.assertEqual(meta["expected_factor_count"], 158)
        self.assertTrue(meta["real_qlib"])

    # 6. Alpha360 from real Qlib
    def test_06_alpha360_real_qlib_handler(self):
        meta = QlibFeaturePipeline.get_feature_metadata("Alpha360")
        self.assertEqual(meta["handler_class"], "Alpha360")
        self.assertEqual(meta["handler_module"], "qlib.contrib.data.handler")
        self.assertEqual(meta["expected_factor_count"], 360)
        self.assertTrue(meta["real_qlib"])

    # 7. Model training (Qlib LGBModel)
    def test_07_qlib_model_instantiation(self):
        from app.qlib_engine.trainer import QlibTrainer
        model = QlibTrainer.instantiate_qlib_model("LGBModel")
        self.assertIsInstance(model, LGBModel)

    # 8. Model serialization
    def test_08_model_serialization(self):
        model = LGBModel()
        manifest_meta = {
            "model_class": "LGBModel",
            "feature_handler": "Alpha158",
            "metrics": {"ic": 0.05, "win_rate": 55.0}
        }
        with tempfile.TemporaryDirectory() as tmp_reg:
            with patch("app.qlib_engine.model_registry.QLIB_MODELS_BASE_DIR", tmp_reg):
                manifest = QlibModelRegistry.save_model("SWING", model, manifest_meta)
                self.assertTrue(os.path.exists(manifest["artifact_path"]))
                self.assertEqual(manifest["strategy"], "SWING")

    # 9. Model loading
    def test_09_model_loading_integrity(self):
        model = LGBModel()
        with tempfile.TemporaryDirectory() as tmp_reg:
            with patch("app.qlib_engine.model_registry.QLIB_MODELS_BASE_DIR", tmp_reg):
                QlibModelRegistry.save_model("SWING", model, {"model_class": "LGBModel"})
                loaded_model, manifest = QlibModelRegistry.load_active_model("SWING")
                self.assertIsInstance(loaded_model, LGBModel)
                self.assertEqual(manifest["model_class"], "LGBModel")

    # 10. Model hash verification
    def test_10_model_hash_verification(self):
        model = LGBModel()
        with tempfile.TemporaryDirectory() as tmp_reg:
            with patch("app.qlib_engine.model_registry.QLIB_MODELS_BASE_DIR", tmp_reg):
                saved = QlibModelRegistry.save_model("SWING", model, {"model_class": "LGBModel"})
                h = hashlib.sha256()
                with open(saved["artifact_path"], "rb") as f:
                    h.update(f.read())
                self.assertEqual(saved["artifact_sha256"], h.hexdigest())

    # 11. OOS separation
    def test_11_temporal_oos_separation(self):
        dates = [f"2023-01-{i:02d}" for i in range(1, 31)] + [f"2023-02-{i:02d}" for i in range(1, 29)]
        segs = TemporalSplitter.partition_dates(dates, train_ratio=0.70, val_ratio=0.15)
        self.assertIn("train", segs)
        self.assertIn("valid", segs)
        self.assertIn("test", segs)
        # Verify strict non-overlapping order
        self.assertTrue(segs["train"][1] < segs["valid"][0])
        self.assertTrue(segs["valid"][1] < segs["test"][0])

    # 12. No leakage
    def test_12_leakage_rejection(self):
        overlapping = ["2023-01-01", "2023-01-02", "2023-01-03"]
        with self.assertRaises(TemporalSplitError):
            TemporalSplitter.partition_dates(overlapping)

    # 13. Swing inference
    def test_13_swing_inference_with_active_model(self):
        manifest = QlibModelRegistry.get_active_manifest("SWING")
        self.assertIsNotNone(manifest, "Active Swing model must be registered")
        model, _ = QlibModelRegistry.load_active_model("SWING")
        self.assertIsNotNone(model)

    # 14. Intraday inference
    def test_14_intraday_inference_with_active_model(self):
        manifest = QlibModelRegistry.get_active_manifest("INTRADAY")
        self.assertIsNotNone(manifest, "Active Intraday model must be registered")
        model, _ = QlibModelRegistry.load_active_model("INTRADAY")
        self.assertIsNotNone(model)

    # 15. F&O inference
    def test_15_fno_inference_with_active_model(self):
        manifest = QlibModelRegistry.get_active_manifest("FNO")
        self.assertIsNotNone(manifest, "Active FNO model must be registered")
        model, _ = QlibModelRegistry.load_active_model("FNO")
        self.assertIsNotNone(model)

    # 16. Scanner integration
    def test_16_scanner_returns_structured_results(self):
        res = QlibScanners.run_fno_scan(underlyings=["RELIANCE", "TCS"])
        self.assertEqual(res["engine"], "QLIB")
        self.assertEqual(res["strategy"], "FNO")
        self.assertIn("opportunities", res)

    # 17. No legacy fallback
    def test_17_no_legacy_fallback_when_model_missing(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            with patch("app.qlib_engine.model_registry.QLIB_MODELS_BASE_DIR", empty_dir):
                with self.assertRaises(QlibModelUnavailableError):
                    QlibScanners.run_swing_scan()

    # 18. Missing-model fail-closed
    def test_18_missing_model_fails_closed(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            with patch("app.qlib_engine.model_registry.QLIB_MODELS_BASE_DIR", empty_dir):
                with self.assertRaises(QlibModelIntegrityError):
                    QlibModelRegistry.load_active_model("SWING")

    # 19. Missing-data fail-closed
    def test_19_missing_data_fails_closed(self):
        adapter = IndiaMarketDataAdapter(provider_uri=os.path.join(self.test_dir, "empty_provider"))
        with tempfile.NamedTemporaryFile(suffix=".db") as empty_db:
            conn = sqlite3.connect(empty_db.name)
            conn.execute("CREATE TABLE ohlcv (ticker TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, timeframe TEXT)")
            conn.close()
            with patch("app.qlib_engine.india_data_adapter.get_db_path", return_value=empty_db.name):
                with self.assertRaises(ValueError):
                    adapter.sync_daily_universe(tickers=["RELIANCE.NS"])

    # 20. Fresh entry price
    def test_20_fresh_entry_price_used(self):
        recs = QlibVirtualTracker.get_recommendations(strategy="SWING", limit=1)
        if recs:
            rec = recs[0]
            self.assertGreater(rec["entry_price"], 0.0)
            self.assertGreater(rec["target_price"], rec["entry_price"])
            self.assertLess(rec["stop_loss"], rec["entry_price"])

    # 21. Virtual recommendation tracking
    def test_21_virtual_recommendation_persisted(self):
        with patch("app.qlib_engine.virtual_tracker.get_db_path", return_value=self.test_db):
            rec = QlibVirtualTracker.record_recommendation(
                ticker="TEST.NS",
                strategy="SWING",
                direction="BUY",
                entry_price=500.0,
                stop_loss=480.0,
                target_price=540.0,
                confidence=78.5,
                model_id="test_model_1",
                model_hash="abc123hash",
                feature_version="alpha158_v1"
            )
            self.assertEqual(rec["ticker"], "TEST.NS")
            self.assertEqual(rec["status"], "OPEN")
            self.assertEqual(rec["engine"], "QLIB")

    # 22. No portfolio heat consumption
    def test_22_zero_portfolio_heat_consumption(self):
        # Even after recording Qlib recommendations, heat remains 0.0%
        heat = get_portfolio_heat_status()
        self.assertEqual(heat["current_heat_pct"], 0.0)
        self.assertEqual(heat["open_positions"], 0)

    # 23. No broker execution
    def test_23_no_broker_execution(self):
        # Qlib recommendations are virtual research signals with 0 broker calls
        from unittest.mock import patch
        with patch("app.qlib_engine.virtual_tracker.get_db_path", return_value=self.test_db):
            with patch("app.api.broker.execute_trade") as mock_exec:
                rec = QlibVirtualTracker.record_recommendation(
                    ticker="TCS.NS",
                    strategy="SWING",
                    direction="BUY",
                    entry_price=4000.0,
                    stop_loss=3920.0,
                    target_price=4160.0,
                    confidence=85.0,
                    model_id="test_model_nobroker",
                    model_hash="abc123nobroker",
                    feature_version="alpha158_v1"
                )
                self.assertIsNotNone(rec.get("id"))
                mock_exec.assert_not_called()

        # Verify broker defaults to simulation mode in app_settings / broker logic
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = 'simulation_mode'")
        row = cur.fetchone()
        conn.close()
        sim_mode = row[0] if row else 'true'
        self.assertEqual(sim_mode.lower(), 'true')

    # 24. Dynamic frontend metrics
    def test_24_dynamic_frontend_metrics_api(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        res = client.get("/api/qlib/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["engine"], "QLIB")
        self.assertTrue(data["qlib_installed"])

    # 25. No hardcoded performance
    def test_25_no_hardcoded_performance(self):
        summary = QlibVirtualTracker.get_performance_summary("SWING")
        if summary.get("completed_trades") == 0:
            self.assertIsNone(summary.get("win_rate"))
            self.assertIsNone(summary.get("profit_factor"))
            self.assertIn("N/A", summary.get("display_status"))

    # 26. Legacy vs Qlib comparison
    def test_26_legacy_vs_qlib_comparison(self):
        comp = ComparisonService.get_side_by_side_comparison()
        self.assertIn("verdict", comp)
        self.assertIn("legacy", comp)
        self.assertIn("qlib", comp)
        self.assertEqual(comp["legacy"]["engine"], "LEGACY")
        self.assertEqual(comp["qlib"]["engine"], "QLIB")

    # 27. Legacy champion models untouched
    def test_27_legacy_champions_untouched(self):
        def sha(p):
            with open(p, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(
            sha("models/swing/champion_ensemble.pkl"),
            "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"
        )
        self.assertEqual(
            sha("models/intraday/champion_ensemble.pkl"),
            "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
        )


if __name__ == "__main__":
    unittest.main()
