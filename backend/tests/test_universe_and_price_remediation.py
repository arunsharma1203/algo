"""
TEST SUITE: UNIVERSE UPGRADE, TELEMETRY & PRICE REMEDIATION
===========================================================
Validates:
1. Dynamic NIFTY_500_ESTABLISHED universe resolution (414 eligible, 97 rejected).
2. Hardware & dataset benchmarking across 3 tiers (35, FNO, 414).
3. Live training telemetry & honest ETA state machine.
4. Fail-closed live market pricing in Decision Engine (zero candle-close fabrication).
5. Database-level engine boundary separation (Legacy vs Qlib).
6. Automated 15:15 IST intraday square-off and startup reconciliation.
7. Bit-for-bit Legacy Champion model hash invariants.
"""

import os
import sys
import unittest
import sqlite3
import tempfile
import hashlib
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Ensure backend path is in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.analytics.universe_config import (
    resolve_nifty_500_established,
    resolve_universe_tickers,
    UNIVERSE_PRESETS
)
from app.qlib_engine.benchmark import QlibBenchmarkSuite
from app.qlib_engine.training_telemetry import (
    TrainingTelemetryManager,
    TRAINING_STAGES
)
from app.analytics.decision_engine import evaluate_ticker, QualificationResult
from app.api.ml_history import save_ml_trade, evaluate_ml_history, _EVAL_CACHE
from app.analytics.intraday_squareoff import (
    execute_intraday_squareoff,
    reconcile_overdue_intraday_positions
)
from app.data.market_provider import get_live_quote_with_meta


class TestQlibUniverseResolution(unittest.TestCase):
    """Verifies dynamic NIFTY_500_ESTABLISHED discovery on canonical SQLite ohlcv."""

    def setUp(self):
        from app.analytics.universe_config import invalidate_universe_cache
        invalidate_universe_cache()

    def test_nifty_500_established_counts(self):
        """Discovers 414 eligible stocks (>=1200 bars, fresh, positive) and audits 97 rejections."""
        result = resolve_nifty_500_established(min_bars=1200, freshness_cutoff_days=15)
        self.assertEqual(len(result["symbols"]), 414)
        self.assertEqual(len(result["rejections"]), 97)
        self.assertEqual(result["eligible_count"], 414)
        self.assertEqual(result["rejected_count"], 97)

        # Verify established bluechips are included
        for bluechip in ["RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS"]:
            self.assertIn(bluechip, result["symbols"])

        # Verify recent IPOs or short-history stocks are rejected
        rejection_map = result["rejections"]
        for recent_stock in ["JIOFIN.NS", "IREDA.NS", "TATATECH.NS"]:
            self.assertIn(recent_stock, rejection_map)
            self.assertIn("INSUFFICIENT_BARS", rejection_map[recent_stock])

    def test_universe_preset_registration(self):
        """Verifies NIFTY_500_ESTABLISHED is registered in presets and resolve_universe_tickers."""
        self.assertIn("NIFTY_500_ESTABLISHED", UNIVERSE_PRESETS)
        preset = UNIVERSE_PRESETS["NIFTY_500_ESTABLISHED"]
        self.assertEqual(preset["min_bars"], 1200)

        resolved = resolve_universe_tickers("NIFTY_500_ESTABLISHED")
        self.assertEqual(len(resolved), 414)
        self.assertIn("RELIANCE.NS", resolved)


class TestQlibBenchmarksAndTelemetry(unittest.TestCase):
    """Verifies empirical hardware benchmarks and live telemetry state machine."""

    def test_benchmark_suite_tiers(self):
        """Verifies benchmark suite covers all 3 tiers with measured metrics."""
        data = QlibBenchmarkSuite.get_latest_benchmarks()
        self.assertIn("tiers", data)
        tiers = data["tiers"]
        self.assertIn("BOOTSTRAP_35", tiers)
        self.assertIn("FNO", tiers)
        self.assertIn("NIFTY_500_ESTABLISHED", tiers)

        # Verify Tier 3 metrics
        tier3 = tiers["NIFTY_500_ESTABLISHED"]
        self.assertEqual(tier3["instruments_count"], 414)
        self.assertEqual(tier3["features_count"], 158)
        self.assertGreater(tier3["feature_rows"]["total"], 1000000)
        self.assertGreater(tier3["timings_seconds"]["feature_generation"], 100.0)
        self.assertGreater(tier3["memory_mb"]["peak"], 2000.0)

    def test_telemetry_state_machine(self):
        """Verifies 10 discrete stages, progress monotonically increasing, and ETA calculation."""
        tm = TrainingTelemetryManager()
        tm.start_job(
            job_id="TEST-JOB-001",
            strategy="SWING",
            universe_name="NIFTY_500_ESTABLISHED",
            instruments_count=414
        )

        state = tm.get_latest()
        self.assertEqual(state["status"], "RUNNING")
        self.assertEqual(state["job_id"], "TEST-JOB-001")

        # Step through stages
        prev_progress = -1
        for stage in TRAINING_STAGES:
            tm.update_stage(stage)
            cur_state = tm.get_latest()
            self.assertEqual(cur_state["stage"], stage)
            self.assertGreaterEqual(cur_state["progress_pct"], prev_progress)
            prev_progress = cur_state["progress_pct"]

        # Verify complete
        tm.complete_job(result={"model_id": "TEST_QLIB_001"})
        final_state = tm.get_latest()
        self.assertEqual(final_state["status"], "COMPLETED")
        self.assertEqual(final_state["progress_pct"], 100.0)
        self.assertEqual(final_state["estimated_remaining_seconds"], 0.0)


class TestPriceRemediationAndFailClosed(unittest.TestCase):
    """Verifies Part B live price capture, metadata preservation, and fail-closed behavior."""

    def test_live_quote_with_metadata(self):
        """Verifies get_live_quote_with_meta returns price, age_ms, and quote_freshness."""
        meta = get_live_quote_with_meta("RELIANCE.NS")
        self.assertIsNotNone(meta)
        self.assertIn("price", meta)
        self.assertIn("source", meta)
        self.assertIn("quote_freshness", meta)
        self.assertIn("age_ms", meta)
        self.assertIn(meta["source"].lower(), ("upstox", "yahoo", "yfinance", "stale_cache", "cache"))
        self.assertGreater(meta["price"], 0.0)

    def _create_mock_df_and_model(self):
        import pandas as pd
        import numpy as np
        dates = pd.date_range(end=datetime.now(), periods=120, freq='15min')
        df = pd.DataFrame({
            'open': np.linspace(2800, 2850, 120),
            'high': np.linspace(2810, 2860, 120),
            'low': np.linspace(2790, 2840, 120),
            'close': np.linspace(2805, 2855, 120),
            'volume': [50000.0] * 120
        }, index=dates)

        mock_model = MagicMock()
        mock_model.predict_proba.return_value = np.array([[0.1, 0.9]])
        mock_est = MagicMock()
        mock_est.predict_proba.return_value = np.array([[0.1, 0.9]])
        mock_model.estimators_ = [mock_est, mock_est, mock_est]
        champion_meta = {"version": "v1.0-test"}
        return df, mock_model, champion_meta

    def test_evaluate_ticker_fails_closed_when_live_quote_missing(self):
        """evaluate_ticker fails closed when live quote is unavailable/stale."""
        df, model, meta = self._create_mock_df_and_model()

        # Mock market_provider to return missing quote
        with patch("app.data.market_provider.get_live_quote_with_meta", return_value=None):
            result = evaluate_ticker(
                ticker="RELIANCE.NS",
                df=df,
                champion_model=model,
                champion_meta=meta,
                trade_type="INTRADAY",
                source="MANUAL",
                skip_enrichment=True,
                is_simulation=False,
                allow_candle_close=False
            )
            self.assertFalse(result.qualified)
            self.assertEqual(result.rejection_reason, "LIVE_PRICE_UNAVAILABLE_OR_STALE")

    def test_evaluate_ticker_uses_live_quote_when_available(self):
        """evaluate_ticker anchors entry_price to live quote when available."""
        df, model, meta = self._create_mock_df_and_model()

        mock_quote = {
            "price": 2850.50,
            "source_name": "Upstox Live Feed",
            "source": "UPSTOX",
            "timestamp": "2026-09-11T10:00:00+05:30",
            "age_ms": 45,
            "quote_freshness": "REAL_TIME",
            "is_realtime": True
        }

        with patch("app.data.market_provider.get_live_quote_with_meta", return_value=mock_quote):
            result = evaluate_ticker(
                ticker="RELIANCE.NS",
                df=df,
                champion_model=model,
                champion_meta=meta,
                trade_type="INTRADAY",
                source="MANUAL",
                skip_enrichment=True,
                is_simulation=False,
                allow_candle_close=False
            )
            self.assertEqual(result.entry, 2850.50)
            self.assertEqual(result.quote_freshness, "REAL_TIME")
            self.assertTrue(result.price_is_fresh)


class TestEngineSeparationAndSquareOff(unittest.TestCase):
    """Verifies database-level engine separation and automated 15:15 square-off."""

    def setUp(self):
        """Create an isolated temporary SQLite database."""
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.conn = sqlite3.connect(self.db_path)

        # Create schema matching production ml_trade_history
        self.conn.execute("""
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
                tightened_sl REAL,
                ai_guard_action TEXT,
                risk_level TEXT DEFAULT 'NORMAL',
                risk_reasons TEXT,
                risk_updated_at TEXT,
                current_price REAL,
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
                entry_price_source TEXT,
                entry_price_timestamp TEXT,
                entry_price_age_ms INTEGER,
                quote_freshness TEXT
            );
        """)
        self.conn.execute("""
            CREATE TABLE ohlcv (
                ticker TEXT,
                date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume INTEGER,
                timeframe TEXT,
                PRIMARY KEY (ticker, date, timeframe)
            );
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_save_ml_trade_persists_price_metadata_and_engine(self):
        """save_ml_trade persists live entry metadata and enforces engine column."""
        with patch("app.api.ml_history.get_db_path", return_value=self.db_path):
            saved = save_ml_trade(
                ticker="INFY.NS",
                is_bullish=True,
                entry=1850.25,
                sl=1830.0,
                tp1=1880.0,
                tp2=1910.0,
                confidence=72.5,
                trade_type="INTRADAY",
                engine="LEGACY",
                entry_price_source="UPSTOX",
                entry_price_timestamp="2026-09-11T11:00:00+05:30",
                entry_price_age_ms=85,
                quote_freshness="REAL_TIME"
            )
            self.assertTrue(saved)

            row = self.conn.execute("SELECT * FROM ml_trade_history WHERE ticker = 'INFY.NS'").fetchone()
            self.assertIsNotNone(row)
            # Verify columns via cursor description
            cols = [d[0] for d in self.conn.execute("SELECT * FROM ml_trade_history").description]
            row_dict = dict(zip(cols, row))
            self.assertEqual(row_dict["entry"], 1850.25)
            self.assertEqual(row_dict["engine"], "LEGACY")
            self.assertEqual(row_dict["entry_price_source"], "UPSTOX")
            self.assertEqual(row_dict["entry_price_age_ms"], 85)
            self.assertEqual(row_dict["quote_freshness"], "REAL_TIME")

    def test_evaluate_ml_history_engine_separation(self):
        """evaluate_ml_history with engine='LEGACY' excludes QLIB virtual trades."""
        # Insert 1 Legacy trade and 1 Qlib trade
        self.conn.execute("""
            INSERT INTO ml_trade_history (
                ticker, direction, entry, sl, tp1, tp2, confidence, trade_type, status, engine, position_type, timestamp
            ) VALUES 
            ('TCS.NS', 'BULLISH', 4200.0, 4150.0, 4280.0, 4350.0, 75.0, 'SWING', 'OPEN', 'LEGACY', 'NOT_A_POSITION', '2026-09-11T10:00:00+05:30'),
            ('INFY.NS', 'BULLISH', 1850.0, 1820.0, 1890.0, 1930.0, 82.0, 'SWING', 'OPEN', 'QLIB', 'NOT_A_POSITION', '2026-09-11T10:00:00+05:30')
        """)
        self.conn.commit()

        with patch("app.api.ml_history.get_db_path", return_value=self.db_path):
            legacy_res = evaluate_ml_history(force_refresh=True, engine="LEGACY")
            legacy_tickers = [t["ticker"] for t in legacy_res]
            self.assertIn("TCS.NS", legacy_tickers)
            self.assertNotIn("INFY.NS", legacy_tickers)

            qlib_res = evaluate_ml_history(force_refresh=True, engine="QLIB")
            qlib_tickers = [t["ticker"] for t in qlib_res]
            self.assertIn("INFY.NS", qlib_tickers)
            self.assertNotIn("TCS.NS", qlib_tickers)

    def test_execute_intraday_squareoff_rules(self):
        """execute_intraday_squareoff closes OPEN Legacy intraday trades and leaves Swing/Qlib untouched."""
        # Insert 1 open legacy intraday trade, 1 open legacy swing trade, 1 open qlib intraday trade
        self.conn.execute("""
            INSERT INTO ml_trade_history (
                id, ticker, direction, entry, sl, tp1, tp2, trade_type, status, outcome, engine, timestamp
            ) VALUES 
            (1, 'RELIANCE.NS', 'BULLISH', 2900.0, 2870.0, 2940.0, 2980.0, 'INTRADAY', 'OPEN', 'OPEN', 'LEGACY', '2026-09-11T10:00:00+05:30'),
            (2, 'TCS.NS', 'BULLISH', 4200.0, 4150.0, 4280.0, 4350.0, 'SWING', 'OPEN', 'OPEN', 'LEGACY', '2026-09-11T10:00:00+05:30'),
            (3, 'INFY.NS', 'BULLISH', 1850.0, 1820.0, 1890.0, 1930.0, 'INTRADAY', 'OPEN', 'OPEN', 'QLIB', '2026-09-11T10:00:00+05:30')
        """)
        self.conn.commit()

        # Mock market live quote for RELIANCE.NS at 2920.0 (+0.69%)
        mock_quote = {"price": 2920.0, "source": "UPSTOX"}
        with patch("app.analytics.intraday_squareoff.get_live_quote_with_meta", return_value=mock_quote):
            result = execute_intraday_squareoff(db_path=self.db_path, force=True)
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(result["squared_off_count"], 1)

            # Trade 1 should be squared off
            row1 = self.conn.execute("SELECT status, outcome, exit_price, profit_pct FROM ml_trade_history WHERE id = 1").fetchone()
            self.assertEqual(row1[0], "CLOSED")
            self.assertEqual(row1[1], "SQUARED OFF (3:15 PM)")
            self.assertEqual(row1[2], 2920.0)
            self.assertEqual(row1[3], 0.69)

            # Trade 2 (Swing) should still be OPEN
            row2 = self.conn.execute("SELECT status, outcome FROM ml_trade_history WHERE id = 2").fetchone()
            self.assertEqual(row2[0], "OPEN")
            self.assertEqual(row2[1], "OPEN")

            # Trade 3 (Qlib) should still be OPEN
            row3 = self.conn.execute("SELECT status, outcome FROM ml_trade_history WHERE id = 3").fetchone()
            self.assertEqual(row3[0], "OPEN")
            self.assertEqual(row3[1], "OPEN")

    def test_reconcile_overdue_intraday_positions(self):
        """reconcile_overdue_intraday_positions sweeps overdue trades from prior calendar days."""
        # Insert trade from yesterday left OPEN
        self.conn.execute("""
            INSERT INTO ml_trade_history (
                id, ticker, direction, entry, sl, tp1, tp2, trade_type, status, outcome, engine, timestamp
            ) VALUES 
            (10, 'HDFCBANK.NS', 'BULLISH', 1600.0, 1580.0, 1630.0, 1660.0, 'INTRADAY', 'OPEN', 'OPEN', 'LEGACY', '2026-09-08T10:00:00+05:30')
        """)
        # Insert yesterday's close in ohlcv
        self.conn.execute("""
            INSERT INTO ohlcv (ticker, date, open, high, low, close, volume, timeframe)
            VALUES ('HDFCBANK.NS', '2026-09-08', 1600.0, 1625.0, 1595.0, 1620.0, 100000, '1d')
        """)
        self.conn.commit()

        rec_res = reconcile_overdue_intraday_positions(db_path=self.db_path)
        self.assertEqual(rec_res["status"], "RECONCILED")
        self.assertEqual(rec_res["reconciled_count"], 1)

        row = self.conn.execute("SELECT status, outcome, exit_price, profit_pct FROM ml_trade_history WHERE id = 10").fetchone()
        self.assertEqual(row[0], "CLOSED")
        self.assertEqual(row[1], "SQUARED OFF (RECONCILED)")
        self.assertEqual(row[2], 1620.0)
        self.assertEqual(row[3], 1.25)


class TestChampionModelInvariants(unittest.TestCase):
    """Guarantees bit-for-bit unchanged hashes for Legacy Champion models."""

    EXPECTED_INTRADAY_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
    EXPECTED_SWING_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

    def test_legacy_champion_hashes(self):
        """Verifies both Legacy Champion models match exact SHA-256 hashes."""
        intraday_path = os.path.join(BASE_DIR, "models", "intraday", "champion_ensemble.pkl")
        swing_path = os.path.join(BASE_DIR, "models", "swing", "champion_ensemble.pkl")

        self.assertTrue(os.path.exists(intraday_path), f"Missing {intraday_path}")
        self.assertTrue(os.path.exists(swing_path), f"Missing {swing_path}")

        with open(intraday_path, "rb") as f:
            intraday_sha = hashlib.sha256(f.read()).hexdigest()
        with open(swing_path, "rb") as f:
            swing_sha = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(intraday_sha, self.EXPECTED_INTRADAY_HASH, "INTRADAY CHAMPION HASH MUTATED!")
        self.assertEqual(swing_sha, self.EXPECTED_SWING_HASH, "SWING CHAMPION HASH MUTATED!")


if __name__ == "__main__":
    unittest.main()
