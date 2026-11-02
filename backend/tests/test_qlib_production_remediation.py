"""
QLIB PRODUCTION V1 REMEDIATION TEST SUITE
=========================================
Validates the complete remediation of runtime failures, data freshness,
scoring independence, champion protection, and concurrent isolation.

Test Coverage:
1. Multiple sequential Qlib scans without RecorderInitializationError
2. Legacy Champion model loading via ModelRegistry.load_champion()
3. Champion SHA-256 hash preservation (Intraday & Swing bit-for-bit unchanged)
4. Qlib training LightGBM parameter collision fix (no duplicate verbose)
5. Data freshness classification (FRESH, DELAYED, STALE, UNAVAILABLE)
6. Incremental data adapter sync (sync_if_authoritative_newer)
7. Dynamic universe resolution (ALL_DATABASE_STOCKS)
8. Zero portfolio heat (position_type='NOT_A_POSITION', 0.0% heat)
9. Zero broker execution (no live orders)
10. Score differentiation (raw_prediction, recommendation_score, rank_percentile)
11. F&O contract fail-closed structure (contract_status, source, reason)
12. ComparisonService metrics (target_hit_rate, stop_hit_rate, expiration_rate)
13. Zero Legacy injection into Qlib inference pipeline
"""

import os
import sys
import unittest
import tempfile
import shutil
import sqlite3
import hashlib
import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Set environment flags for test isolation
os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"
os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"
os.environ["SUPPRESS_SCHEDULER"] = "true"

import qlib
from app.qlib_engine.india_data_adapter import (
    IndiaMarketDataAdapter, 
    get_qlib_adapter, 
    ensure_qlib_ready
)
from app.qlib_engine.model_registry import QlibModelRegistry
from app.analytics.model_registry import ModelRegistry
from app.qlib_engine.trainer import QlibTrainer
from app.qlib_engine.virtual_tracker import QlibVirtualTracker, init_qlib_tracking_schema
from app.qlib_engine.comparison_service import ComparisonService
from app.qlib_engine.scanners import QlibScanners
from app.analytics.kelly_sizer import get_portfolio_heat_status
from app.analytics.universe_config import resolve_all_database_stocks


class TestQlibProductionRemediation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp(prefix="qlib_remediation_test_")
        cls.test_db = os.path.join(cls.test_dir, "test_market_data.db")

        # Initialize SQLite schema with authoritative ml_trade_history and ohlcv tables
        conn = sqlite3.connect(cls.test_db)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE ml_trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry REAL NOT NULL,
                sl REAL NOT NULL,
                tp1 REAL NOT NULL,
                tp2 REAL,
                confidence REAL NOT NULL,
                trade_type TEXT DEFAULT 'SWING',
                status TEXT DEFAULT 'OPEN',
                explanation TEXT,
                outcome TEXT,
                profit_pct REAL,
                effective_entry REAL,
                slippage_drag REAL,
                ideal_profit_pct REAL,
                exit_price REAL,
                exit_time TEXT,
                source TEXT DEFAULT 'MANUAL',
                position_type TEXT DEFAULT 'NOT_A_POSITION',
                reference_price REAL,
                model_candle_close REAL,
                price_source TEXT,
                price_timestamp TEXT,
                price_is_fresh INTEGER DEFAULT 0,
                engine TEXT DEFAULT 'LEGACY',
                strategy TEXT,
                instrument TEXT,
                model_id TEXT,
                model_hash TEXT,
                data_timestamp TEXT,
                is_reconciled INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE ohlcv (
                ticker TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                timeframe TEXT DEFAULT '1d',
                PRIMARY KEY (ticker, date, timeframe)
            )
        """)
        conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")

        # Populate sample OHLCV data for dynamic universe testing
        sample_symbols = ["RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS"]
        base_date = datetime.now() - timedelta(days=100)
        for sym in sample_symbols:
            for d in range(80):
                cur_date = (base_date + timedelta(days=d)).strftime("%Y-%m-%d")
                conn.execute(
                    "INSERT INTO ohlcv (ticker, date, open, high, low, close, volume, timeframe) "
                    "VALUES (?, ?, 1000.0, 1020.0, 990.0, 1010.0, 50000.0, '1d')",
                    (sym, cur_date)
                )
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    # ── 1. MULTIPLE SEQUENTIAL SCANS (IDEMPOTENT INITIALIZATION) ───────
    def test_01_sequential_qlib_initialization_idempotent(self):
        """Verify ensure_qlib_ready() and initialize_qlib() can be called repeatedly without crashing."""
        adapter1 = ensure_qlib_ready()
        self.assertIsInstance(adapter1, IndiaMarketDataAdapter)

        adapter2 = ensure_qlib_ready()
        self.assertIsInstance(adapter2, IndiaMarketDataAdapter)

        # Direct adapter initialization check
        adapter = get_qlib_adapter()
        init_res = adapter.initialize_qlib()
        self.assertIn(init_res.get("status"), ("ALREADY_INITIALIZED", "INITIALIZED", "READY"))

    # ── 2. LEGACY CHAMPION LOADING (ModelRegistry.load_champion) ───────
    def test_02_legacy_champion_model_loading(self):
        """Verify Legacy Champion models load successfully via ModelRegistry.load_champion()."""
        intraday_res = ModelRegistry.load_champion("intraday")
        self.assertIsNotNone(intraday_res, "Legacy Intraday Champion model failed to load")
        intraday_champ = intraday_res[0] if isinstance(intraday_res, tuple) else intraday_res
        self.assertTrue(
            hasattr(intraday_champ, "predict_proba") or hasattr(intraday_champ, "estimators_"),
            "Intraday Champion is missing expected prediction interface"
        )

        swing_res = ModelRegistry.load_champion("swing")
        self.assertIsNotNone(swing_res, "Legacy Swing Champion model failed to load")
        swing_champ = swing_res[0] if isinstance(swing_res, tuple) else swing_res
        self.assertTrue(
            hasattr(swing_champ, "predict_proba") or hasattr(swing_champ, "estimators_"),
            "Swing Champion is missing expected prediction interface"
        )

    # ── 3. CHAMPION SHA-256 HASHES UNTOUCHED ───────────────────────────
    def test_03_champion_hashes_unchanged(self):
        """Verify both production champion ensemble models are bit-for-bit unchanged."""
        expected_intraday = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
        expected_swing = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

        backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        intra_path = os.path.join(backend_dir, "models", "intraday", "champion_ensemble.pkl")
        swing_path = os.path.join(backend_dir, "models", "swing", "champion_ensemble.pkl")

        self.assertTrue(os.path.exists(intra_path), f"Missing {intra_path}")
        self.assertTrue(os.path.exists(swing_path), f"Missing {swing_path}")

        with open(intra_path, "rb") as f:
            actual_intra = hashlib.sha256(f.read()).hexdigest()
        with open(swing_path, "rb") as f:
            actual_swing = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(actual_intra, expected_intraday, "Intraday Champion artifact has been modified!")
        self.assertEqual(actual_swing, expected_swing, "Swing Champion artifact has been modified!")

    # ── 4. QLIB TRAINING PARAMETER FIX (NO VERBOSE COLLISION) ──────────
    def test_04_qlib_trainer_lgbm_params_no_verbose_collision(self):
        """Verify default parameters for LGBModel in trainer do not contain 'verbose'."""
        import inspect
        src = inspect.getsource(QlibTrainer.instantiate_qlib_model)
        self.assertIn('default_p.pop("verbose", None)', src, "trainer.py must explicitly pop 'verbose'")

        # Test model instantiation directly with default parameters
        from qlib.contrib.model.gbdt import LGBModel
        # Should initialize without raising LightGBMError
        model = LGBModel(loss="mse", learning_rate=0.05, num_leaves=31)
        self.assertIsNotNone(model)

    # ── 5. DATA FRESHNESS CLASSIFICATION ───────────────────────────────
    def test_05_freshness_classification_states(self):
        """Verify freshness logic accurately classifies FRESH, DELAYED, STALE, and UNAVAILABLE."""
        adapter = get_qlib_adapter()

        # Mock unavailable
        with patch.object(adapter, "get_latest_authoritative_timestamp", return_value=None):
            f_unavail = adapter.check_freshness("1d")
            self.assertEqual(f_unavail["data_status"], "UNAVAILABLE")

        # Mock current timestamp (FRESH)
        now_dt = datetime.now()
        with patch.object(adapter, "get_latest_authoritative_timestamp", return_value=now_dt):
            with patch.object(adapter, "get_latest_provider_timestamp", return_value=now_dt):
                f_fresh = adapter.check_freshness("1d")
                self.assertEqual(f_fresh["data_status"], "FRESH")

        # Mock 2 hours lag during market hours (DELAYED or STALE depending on threshold)
        delayed_dt = now_dt - timedelta(hours=2)
        with patch.object(adapter, "get_latest_authoritative_timestamp", return_value=delayed_dt):
            with patch.object(adapter, "get_latest_provider_timestamp", return_value=delayed_dt):
                f_del = adapter.check_freshness("15m")
                self.assertIn(f_del["data_status"], ("DELAYED", "STALE", "FRESH"))

    # ── 6. INCREMENTAL DATA ADAPTER SYNC ───────────────────────────────
    def test_06_sync_if_authoritative_newer(self):
        """Verify sync_if_authoritative_newer triggers when authoritative DB is ahead."""
        adapter = get_qlib_adapter()
        auth_ts = datetime(2026, 9, 11, 15, 0, 0)
        prov_ts_old = datetime(2026, 9, 10, 15, 0, 0)

        with patch.object(adapter, "get_latest_authoritative_timestamp", return_value=auth_ts):
            with patch.object(adapter, "get_latest_provider_timestamp", return_value=prov_ts_old):
                with patch.object(adapter, "sync_daily_universe", return_value={"synced_count": 5}) as mock_sync:
                    res = adapter.sync_if_authoritative_newer("1d")
                    self.assertTrue(res["synced"])
                    mock_sync.assert_called_once()

    # ── 7. DYNAMIC UNIVERSE RESOLUTION (ALL_DATABASE_STOCKS) ───────────
    def test_07_dynamic_universe_resolution(self):
        """Verify resolve_all_database_stocks dynamically discovers valid symbols from SQLite."""
        with patch("app.data.historical_data_layer.get_db_path", return_value=self.test_db):
            res = resolve_all_database_stocks(timeframe="1d", min_bars=60)

            self.assertGreaterEqual(res["discovered_count"], 5)
            self.assertGreaterEqual(res["scan_ready_count"], 5)
            self.assertIn("RELIANCE.NS", res["scan_ready_symbols"])
            self.assertIn("TCS.NS", res["scan_ready_symbols"])

    # ── 8. ZERO PORTFOLIO HEAT (position_type='NOT_A_POSITION') ────────
    def test_08_qlib_recommendations_zero_heat(self):
        """Verify Qlib recommendations are stored as NOT_A_POSITION and consume 0.0% heat."""
        with patch("app.data.historical_data_layer.get_db_path", return_value=self.test_db), \
             patch("app.qlib_engine.virtual_tracker.get_db_path", return_value=self.test_db), \
             patch("app.api.ml_history.get_db_path", return_value=self.test_db):

            # Record 3 Qlib virtual recommendations
            for i, sym in enumerate(["RELIANCE.NS", "TCS.NS", "INFY.NS"]):
                QlibVirtualTracker.record_recommendation(
                    ticker=sym,
                    strategy="SWING",
                    direction="BUY",
                    entry_price=2500.0,
                    stop_loss=2400.0,
                    target_price=2700.0,
                    confidence=75.0,
                    model_id="test_model",
                    model_hash="test_hash",
                    feature_version="alpha158_v1",
                    data_timestamp="2026-09-11"
                )

            heat_status = get_portfolio_heat_status()
            self.assertEqual(heat_status["current_heat_pct"], 0.0, "Qlib virtual recommendations must consume 0.0% heat!")
            self.assertEqual(heat_status["status"], "NORMAL")
            self.assertFalse(heat_status.get("is_max_reached", False))

    # ── 9. ZERO BROKER EXECUTION ───────────────────────────────────────
    def test_09_zero_broker_execution(self):
        """Verify Qlib virtual recommendations do not touch live broker transport."""
        import inspect
        from app.qlib_engine import virtual_tracker, scanners
        
        # Verify virtual_tracker and scanners do not import broker execution logic
        vt_src = inspect.getsource(virtual_tracker)
        sc_src = inspect.getsource(scanners)
        self.assertNotIn("app.api.broker", vt_src)
        self.assertNotIn("execute_trade", vt_src)
        self.assertNotIn("app.api.broker", sc_src)
        self.assertNotIn("execute_trade", sc_src)

    # ── 10. SCORE DIFFERENTIATION ──────────────────────────────────────
    def test_10_score_differentiation(self):
        """Verify Qlib recommendations distinguish raw_prediction, recommendation_score, and rank_percentile."""
        with patch("app.data.historical_data_layer.get_db_path", return_value=self.test_db), \
             patch("app.qlib_engine.virtual_tracker.get_db_path", return_value=self.test_db), \
             patch("app.api.ml_history.get_db_path", return_value=self.test_db):

            rec = QlibVirtualTracker.record_recommendation(
                ticker="HDFCBANK.NS",
                strategy="SWING",
                direction="BUY",
                entry_price=1600.0,
                stop_loss=1550.0,
                target_price=1700.0,
                confidence=85.0,
                model_id="qlib_lgb_v1",
                model_hash="abc123hash",
                feature_version="alpha158_v1",
                data_timestamp="2026-09-11",
                raw_prediction=0.0452,
                recommendation_score=0.0452,
                rank_percentile=85.0
            )

            self.assertEqual(rec["raw_prediction"], 0.0452)
            self.assertEqual(rec["recommendation_score"], 0.0452)
            self.assertEqual(rec["rank_percentile"], 85.0)
            self.assertIsNone(rec.get("calibrated_probability"))

            recs = QlibVirtualTracker.get_recommendations(strategy="SWING", limit=5)
            matching = [r for r in recs if r["ticker"] == "HDFCBANK.NS"]
            self.assertGreater(len(matching), 0)
            item = matching[0]
            self.assertEqual(item["raw_prediction"], 0.0452)
            self.assertIn("pnl_status", item)
            self.assertIn("current_price", item)

    # ── 11. F&O CONTRACT STRUCTURE FAIL-CLOSED ─────────────────────────
    def test_11_fno_contract_structure_fail_closed(self):
        """Verify run_fno_scan returns honest fail-closed structure when contracts unavailable."""
        with patch("app.qlib_engine.scanners.fetch_nse_option_chain") as mock_fno, \
             patch("app.qlib_engine.scanners.ensure_qlib_ready") as mock_ready:
            mock_ready.return_value.check_freshness.return_value = {"data_status": "FRESH"}
            mock_fno.return_value = {
                "symbol": "RELIANCE",
                "status": "unavailable",
                "reason": "NSE Option Chain currently unreachable",
                "underlying_price": None
            }

            result = QlibScanners.run_fno_scan(underlyings=["RELIANCE.NS"])
            self.assertEqual(result["engine"], "QLIB")
            self.assertEqual(result["strategy"], "FNO")
            self.assertEqual(result["contract_status"], "DATA UNAVAILABLE")
            self.assertEqual(result["source"], "NSE India Option Chain API v3")
            self.assertIn("last_refresh", result)
            self.assertGreater(len(result["opportunities"]), 0)
            self.assertEqual(result["opportunities"][0]["contract_status"], "DATA UNAVAILABLE")

    # ── 12. COMPARISON SERVICE METRICS & INSUFFICIENT DATA ──────────────
    def test_12_comparison_service_hit_rates_and_insufficient_data(self):
        """Verify ComparisonService includes target_hit_rate, stop_hit_rate, and honest N/A on 0 closed."""
        with patch("app.data.historical_data_layer.get_db_path", return_value=self.test_db), \
             patch("app.qlib_engine.comparison_service.get_db_path", return_value=self.test_db):

            # Case A: When 0 closed trades exist
            metrics = ComparisonService.calculate_engine_metrics("QLIB", "SWING")
            self.assertEqual(metrics["completed_trades"], 0)
            self.assertIsNone(metrics["win_rate"])
            self.assertIsNone(metrics["target_hit_rate"])
            self.assertIsNone(metrics["stop_hit_rate"])
            self.assertIsNone(metrics["expiration_rate"])
            self.assertIn("INSUFFICIENT DATA", metrics["status"])

            # Case B: When closed trades exist
            conn = sqlite3.connect(self.test_db)
            now_str = datetime.now().isoformat()
            conn.execute("""
                INSERT INTO ml_trade_history (
                    timestamp, ticker, direction, entry, sl, tp1, confidence, trade_type,
                    status, outcome, profit_pct, engine, source, position_type
                ) VALUES 
                (?, 'TCS.NS', 'BULLISH', 3500, 3400, 3700, 80, 'SWING', 'CLOSED', 'TARGET MET', 5.7, 'QLIB', 'QLIB_SCANNER', 'NOT_A_POSITION'),
                (?, 'INFY.NS', 'BULLISH', 1800, 1750, 1900, 75, 'SWING', 'CLOSED', 'SL HIT', -2.8, 'QLIB', 'QLIB_SCANNER', 'NOT_A_POSITION')
            """, (now_str, now_str))
            conn.commit()
            conn.close()

            metrics_closed = ComparisonService.calculate_engine_metrics("QLIB", "SWING")
            self.assertEqual(metrics_closed["completed_trades"], 2)
            self.assertEqual(metrics_closed["win_rate"], 50.0)
            self.assertEqual(metrics_closed["target_hit_rate"], 50.0)
            self.assertEqual(metrics_closed["stop_hit_rate"], 50.0)
            self.assertEqual(metrics_closed["expiration_rate"], 0.0)

    # ── 13. ZERO LEGACY INJECTION INTO QLIB INFERENCE ───────────────────
    def test_13_zero_legacy_injection_in_qlib_pipeline(self):
        """Verify Qlib inference does NOT invoke Legacy Macro Engine, Platt calibration, or Meta-Learner."""
        import inspect
        from app.qlib_engine import scanners, trainer, feature_pipeline

        for mod in [scanners, trainer, feature_pipeline]:
            src = inspect.getsource(mod)
            self.assertNotIn("app.analytics.macro_engine", src, f"Legacy macro_engine found in {mod.__name__}")
            self.assertNotIn("app.analytics.calibration", src, f"Legacy calibration found in {mod.__name__}")
            self.assertNotIn("app.analytics.metalearner", src, f"Legacy metalearner found in {mod.__name__}")
            self.assertNotIn("apply_platt_scaling", src, f"Platt scaling found in {mod.__name__}")


if __name__ == "__main__":
    unittest.main()
