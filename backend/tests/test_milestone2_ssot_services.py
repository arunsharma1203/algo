"""
Test suite for Milestone 2: Single Sources of Truth (SSOT) Backend Services.
Tests:
1. Ticker Gateway (Canonical normalization, index preservation, delegation parity)
2. DataGateway (OHLCV retrieval, quote fetching, batching, validation)
3. ModelRegistry (Cryptographic integrity, fail-closed enforcement, metadata)
4. PositionMonitorService (Trade evaluation, zero heat for virtual setups, summary)
5. Non-negotiable Production Safety Invariants
"""

import unittest
import os
import sqlite3
import hashlib
import tempfile
import pandas as pd
from unittest.mock import patch, MagicMock

from app.analytics.universe_config import normalize_ticker, validate_ticker
from app.data.validator import MarketDataValidator
from app.data.data_gateway import DataGateway
from app.analytics.model_registry import (
    ModelRegistry,
    ModelIntegrityViolationError,
    CHAMPION_HASHES
)
from app.analytics.position_monitor import PositionMonitorService
from app.analytics.kelly_sizer import get_portfolio_heat_status
from app.data.historical_data_layer import get_db_path

EXPECTED_INTRADAY_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
EXPECTED_SWING_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

class TestMilestone2SSOTServices(unittest.TestCase):

    # ─────────────────────────────────────────────────────────────
    # 1. TICKER GATEWAY TESTS
    # ─────────────────────────────────────────────────────────────

    def test_01_ticker_normalization_standard(self):
        self.assertEqual(normalize_ticker("reliance"), "RELIANCE.NS")
        self.assertEqual(normalize_ticker("TCS"), "TCS.NS")
        self.assertEqual(normalize_ticker("HDFCBANK.NS"), "HDFCBANK.NS")

    def test_02_ticker_normalization_whitespace_and_casing(self):
        self.assertEqual(normalize_ticker("  infy  "), "INFY.NS")
        self.assertEqual(normalize_ticker("icicibank.ns"), "ICICIBANK.NS")

    def test_03_ticker_normalization_preserves_indices(self):
        self.assertEqual(normalize_ticker("^NSEI"), "^NSEI")
        self.assertEqual(normalize_ticker("^INDIAVIX"), "^INDIAVIX")
        self.assertEqual(normalize_ticker(" ^nsei "), "^NSEI")

    def test_04_ticker_normalization_preserves_bse(self):
        self.assertEqual(normalize_ticker("TCS.BO"), "TCS.BO")
        self.assertEqual(normalize_ticker("reliance.bo"), "RELIANCE.BO")

    def test_05_ticker_normalization_empty_inputs(self):
        self.assertEqual(normalize_ticker(""), "")
        self.assertEqual(normalize_ticker(None), "")
        self.assertEqual(normalize_ticker("   "), "")

    def test_06_ticker_normalization_delegation_parity(self):
        symbols = ["RELIANCE", "tcs", "^NSEI", "SBIN.BO", "infy.ns"]
        for sym in symbols:
            self.assertEqual(
                MarketDataValidator.normalize_ticker(sym),
                normalize_ticker(sym)
            )
            self.assertEqual(
                DataGateway.normalize_ticker(sym),
                normalize_ticker(sym)
            )

    # ─────────────────────────────────────────────────────────────
    # 2. DATA GATEWAY TESTS
    # ─────────────────────────────────────────────────────────────

    def test_07_datagateway_is_market_open_force_override(self):
        self.assertTrue(DataGateway.is_market_open(force_override=True))

    def test_08_datagateway_get_live_quote_structure(self):
        # Mock fetch to ensure deterministic offline test
        with patch("app.data.data_gateway.get_live_quote_with_meta") as mock_meta:
            mock_meta.return_value = {
                "price": 2500.0,
                "source": "mock",
                "timestamp": "2026-09-07T12:00:00",
                "is_fresh": True
            }
            quote = DataGateway.get_live_quote("RELIANCE")
            self.assertEqual(quote["ticker"], "RELIANCE.NS")
            self.assertEqual(quote["price"], 2500.0)
            self.assertEqual(quote["source"], "mock")
            self.assertTrue(quote["is_fresh"])

    def test_09_datagateway_get_batch_quotes_error_isolation(self):
        with patch("app.data.data_gateway.get_live_quote_with_meta") as mock_meta:
            def side_effect(ticker):
                if "BAD" in ticker:
                    raise RuntimeError("Failed fetch")
                return {"price": 100.0, "source": "mock"}

            mock_meta.side_effect = side_effect
            batch = DataGateway.get_batch_quotes(["TCS", "BADSTOCK", "INFY"])
            self.assertIn("TCS.NS", batch)
            self.assertIn("BADSTOCK.NS", batch)
            self.assertIn("INFY.NS", batch)
            self.assertEqual(batch["TCS.NS"]["price"], 100.0)
            self.assertIsNone(batch["BADSTOCK.NS"]["price"])

    def test_10_datagateway_get_ohlcv_column_normalization(self):
        dummy_df = pd.DataFrame({
            "Open": [100.0, 101.0],
            "High": [105.0, 106.0],
            "Low": [99.0, 100.0],
            "Close": [104.0, 105.0],
            "Volume": [1000, 1200],
            "Date": ["2026-09-01", "2026-09-02"]
        })
        with patch("app.data.historical_data_layer.HistoricalDataLayer.get_historical_ohlcv") as mock_hist:
            mock_hist.return_value = dummy_df
            res = DataGateway.get_ohlcv("RELIANCE.NS", timeframe="1d", min_rows=2)
            self.assertFalse(res.empty)
            for col in ["open", "high", "low", "close", "volume"]:
                self.assertIn(col, res.columns)

    # ─────────────────────────────────────────────────────────────
    # 3. MODEL REGISTRY TESTS
    # ─────────────────────────────────────────────────────────────

    def test_11_model_registry_intraday_hash_exact(self):
        is_valid, current_sha, expected_sha = ModelRegistry.verify_champion_hash("intraday")
        self.assertTrue(is_valid)
        self.assertEqual(current_sha, EXPECTED_INTRADAY_HASH)
        self.assertEqual(expected_sha, EXPECTED_INTRADAY_HASH)

    def test_12_model_registry_swing_hash_exact(self):
        is_valid, current_sha, expected_sha = ModelRegistry.verify_champion_hash("swing")
        self.assertTrue(is_valid)
        self.assertEqual(current_sha, EXPECTED_SWING_HASH)
        self.assertEqual(expected_sha, EXPECTED_SWING_HASH)

    def test_13_model_registry_load_champion_intraday_succeeds(self):
        model, meta = ModelRegistry.load_champion("intraday", enforce_hash=True)
        self.assertIsNotNone(model)
        self.assertIsInstance(meta, dict)
        self.assertEqual(meta.get("version"), "v1.0-champion")

    def test_14_model_registry_load_champion_swing_succeeds(self):
        model, meta = ModelRegistry.load_champion("swing", enforce_hash=True)
        self.assertIsNotNone(model)
        self.assertIsInstance(meta, dict)
        self.assertEqual(meta.get("version"), "v1.0-champion")

    def test_15_model_registry_fail_closed_on_tampering(self):
        with patch.object(ModelRegistry, "verify_champion_hash", return_value=(False, "fake_sha_abc", EXPECTED_INTRADAY_HASH)):
            with self.assertRaises(ModelIntegrityViolationError):
                ModelRegistry.load_champion("intraday", enforce_hash=True)

    def test_16_model_registry_status_telemetry(self):
        status = ModelRegistry.get_champion_status("intraday")
        self.assertEqual(status["timeframe"], "intraday")
        self.assertEqual(status["status"], "ACTIVE_VERIFIED")
        self.assertTrue(status["hash_verified"])
        self.assertGreater(status["file_size_bytes"], 0)

    def test_17_model_registry_verify_all_champions(self):
        res = ModelRegistry.verify_all_champions()
        self.assertTrue(res["all_champions_intact"])
        self.assertTrue(res["intraday"]["hash_verified"])
        self.assertTrue(res["swing"]["hash_verified"])

    # ─────────────────────────────────────────────────────────────
    # 4. POSITION MONITOR SERVICE TESTS
    # ─────────────────────────────────────────────────────────────

    def test_18_position_monitor_summary_keys(self):
        summary = PositionMonitorService.get_open_positions_summary()
        self.assertIn("total_open", summary)
        self.assertIn("actual_positions_count", summary)
        self.assertIn("virtual_recommendations_count", summary)
        self.assertIn("portfolio_heat_pct", summary)
        self.assertIn("max_heat_cap_pct", summary)

    def test_19_position_monitor_virtual_trades_consume_zero_heat(self):
        summary = PositionMonitorService.get_open_positions_summary()
        # In baseline, there are 0 real positions and 5 virtual setups
        self.assertEqual(summary["actual_positions_count"], 0)
        self.assertEqual(summary["portfolio_heat_pct"], 0.0)

    def test_20_position_monitor_reconcile_trade_isolated(self):
        # Create an isolated temporary SQLite DB with one open trade
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            temp_db = f.name

        try:
            conn = sqlite3.connect(temp_db)
            conn.execute("""
                CREATE TABLE ml_trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    ticker TEXT,
                    direction TEXT,
                    entry REAL,
                    sl REAL,
                    tp1 REAL,
                    confidence REAL,
                    status TEXT,
                    outcome TEXT,
                    exit_price REAL,
                    exit_time TEXT,
                    profit_pct REAL,
                    ideal_profit_pct REAL
                )
            """)
            conn.execute("""
                INSERT INTO ml_trade_history (timestamp, ticker, direction, entry, sl, tp1, confidence, status, outcome)
                VALUES ('2026-09-01T10:00:00', 'RELIANCE.NS', 'BULLISH', 2500.0, 2450.0, 2600.0, 75.0, 'OPEN', 'OPEN')
            """)
            conn.commit()
            conn.close()

            with patch("app.analytics.position_monitor.get_db_path", return_value=temp_db):
                ok = PositionMonitorService.reconcile_trade(
                    trade_id=1,
                    exit_price=2600.0,
                    outcome="TARGET 1 MET"
                )
                self.assertTrue(ok)

                # Verify outcome in DB
                conn = sqlite3.connect(temp_db)
                row = conn.execute("SELECT status, outcome, exit_price, profit_pct FROM ml_trade_history WHERE id = 1").fetchone()
                conn.close()

                self.assertEqual(row[0], "CLOSED")
                self.assertEqual(row[1], "TARGET 1 MET")
                self.assertEqual(row[2], 2600.0)
                self.assertEqual(row[3], 4.0)  # (2600 - 2500) / 2500 * 100 = 4.0%
        finally:
            if os.path.exists(temp_db):
                os.unlink(temp_db)

    # ─────────────────────────────────────────────────────────────
    # 5. PRODUCTION SAFETY INVARIANTS
    # ─────────────────────────────────────────────────────────────

    def test_21_canonical_database_row_counts_unaltered(self):
        canonical_db = get_db_path()
        conn = sqlite3.connect(canonical_db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM ml_trade_history")
        rows = c.fetchone()[0]
        conn.close()
        # Verify history is preserved (>= 73 rows)
        self.assertGreaterEqual(rows, 73)

    def test_22_simulation_mode_is_active(self):
        canonical_db = get_db_path()
        conn = sqlite3.connect(canonical_db)
        c = conn.cursor()
        c.execute("SELECT value FROM app_settings WHERE key = 'simulation_mode'")
        row = c.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "true")

    def test_23_portfolio_heat_is_zero(self):
        heat_status = get_portfolio_heat_status()
        self.assertEqual(heat_status.get("current_heat_pct"), 0.0)
        self.assertEqual(heat_status.get("actual_positions"), 0)

    def test_24_champion_models_remain_byte_exact(self):
        intra_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "models", "intraday", "champion_ensemble.pkl"))
        swing_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "models", "swing", "champion_ensemble.pkl"))

        with open(intra_path, "rb") as f:
            intra_sha = hashlib.sha256(f.read()).hexdigest()
        with open(swing_path, "rb") as f:
            swing_sha = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(intra_sha, EXPECTED_INTRADAY_HASH)
        self.assertEqual(swing_sha, EXPECTED_SWING_HASH)


if __name__ == "__main__":
    unittest.main()
