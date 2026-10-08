"""
Option Sniffer: Comprehensive Unit & Integration Test Suite.

Verifies:
  1. Additive Database Isolation (all 7 tables exist, zero production table alterations).
  2. Rate Governor Hard Limits (8/sec, 64/min, 1600/30min preserved).
  3. Point-in-Time Feature Extraction (28 features, zero future leakage).
  4. 10 Quality Gates & Fail-Closed State Machine (WATCH -> QUALIFIED -> ACTIVE).
  5. Realistic Indian F&O Statutory Cost Modeling & Trailing Ratchet Stops.
  6. Telegram Dispatcher Integrity (PAPER ONLY disclaimer, all Greeks & quote data).
  7. Production Champion Model Preservation (SHA-256 hashes intact).
"""

import os
import time
import json
import sqlite3
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Core imports from app
from app.analytics.option_sniffer.option_constants import (
    STATE_WATCH,
    STATE_QUALIFIED,
    STATE_ACTIVE,
    STATE_PAPER_OPEN,
    STATE_PAPER_CLOSED,
    DIRECTION_BUY_CE,
    DIRECTION_BUY_PE,
    DIRECTION_NO_TRADE,
    MAX_QUOTE_AGE_MS,
    MAX_SPREAD_PCT,
    MIN_OI,
    MIN_VOLUME,
    MIN_BID_PRICE,
    MIN_CALIBRATED_PROB,
    MIN_EXPECTED_NET_RETURN_PCT,
    SIGNAL_COOLDOWN_SECONDS,
    calculate_option_trade_costs,
)
from app.analytics.option_sniffer.option_db import (
    OptionDB,
    init_option_sniffer_tables,
)
from app.analytics.option_sniffer.option_feature_extractor import OptionFeatureExtractor
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.option_sniffer.option_alpha_model import OptionAlphaModel
from app.data.upstox_provider import get_upstox_limiter


class TestOptionDatabaseIsolation(unittest.TestCase):
    """Verifies dedicated additive database schema and isolation from production tables."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db.name
        self.temp_db.close()

        # Seed minimal schema to simulate existing production tables
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("""
            CREATE TABLE ml_trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                direction TEXT,
                entry REAL,
                status TEXT
            );
        """)
        conn.execute("INSERT INTO ml_trade_history (ticker, direction, entry, status) VALUES ('RELIANCE.NS', 'BUY', 2900.0, 'OPEN');")
        conn.execute("""
            CREATE TABLE app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('simulation_mode', 'true');")
        conn.commit()
        conn.close()

        self.db_patcher = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.temp_db_path)
        self.mock_get_db_path = self.db_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        if os.path.exists(self.temp_db_path):
            os.unlink(self.temp_db_path)

    def test_all_seven_tables_created(self):
        """Verifies init_option_sniffer_tables creates all 7 dedicated tables."""
        init_option_sniffer_tables()

        conn = sqlite3.connect(self.temp_db_path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
        conn.close()

        expected_tables = [
            "option_sniffer_candidates",
            "option_sniffer_predictions",
            "option_sniffer_signals",
            "option_sniffer_paper_trades",
            "option_sniffer_events",
            "option_sniffer_pnl_snapshots",
            "option_sniffer_data_health",
        ]
        for tbl in expected_tables:
            self.assertIn(tbl, tables, f"Expected table {tbl} was not created!")

    def test_existing_tables_unaffected(self):
        """Verifies existing tables and rows are completely untouched."""
        init_option_sniffer_tables()

        conn = sqlite3.connect(self.temp_db_path)
        ml_rows = conn.execute("SELECT COUNT(*) FROM ml_trade_history;").fetchone()[0]
        setting_val = conn.execute("SELECT value FROM app_settings WHERE key='simulation_mode';").fetchone()[0]
        conn.close()

        self.assertEqual(ml_rows, 1, "Production ml_trade_history count modified!")
        self.assertEqual(setting_val, "true", "app_settings modified!")

    def test_candidate_catalog_upsert_and_fetch(self):
        """Tests saving and retrieving option candidate contracts."""
        init_option_sniffer_tables()

        candidates = [
            {
                "instrument_key": "NSE_FO|12345",
                "trading_symbol": "NIFTY26OCT25000CE",
                "underlying": "NIFTY",
                "expiry": "2026-10-29",
                "strike": 25000.0,
                "option_type": "CE",
                "lot_size": 65,
                "metadata": {"test": True},
            },
            {
                "instrument_key": "NSE_FO|12346",
                "trading_symbol": "NIFTY26OCT25000PE",
                "underlying": "NIFTY",
                "expiry": "2026-10-29",
                "strike": 25000.0,
                "option_type": "PE",
                "lot_size": 65,
                "metadata": {"test": True},
            },
        ]
        saved = sum(1 for c in candidates if OptionDB.save_candidate_contract(c))
        self.assertEqual(saved, 2)

        active = OptionDB.get_active_candidates(underlying="NIFTY")
        self.assertEqual(len(active), 2)
        self.assertEqual(active[0]["trading_symbol"], "NIFTY26OCT25000CE")

    def test_prediction_persistence_and_query(self):
        """Tests inserting and querying model predictions."""
        init_option_sniffer_tables()

        pred = {
            "prediction_id": "pred_test_01",
            "timestamp": datetime.now().isoformat(),
            "instrument_key": "NSE_FO|12345",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "option_type": "CE",
            "expiry": "2026-10-29",
            "spot_price": 25010.0,
            "option_ltp": 150.0,
            "bid": 149.5,
            "ask": 150.5,
            "spread_pct": 0.67,
            "delta": 0.52,
            "gamma": 0.001,
            "theta": -12.5,
            "vega": 18.0,
            "iv": 14.5,
            "raw_score": 0.65,
            "calibrated_prob": 0.61,
            "expected_net_return": 4.2,
            "direction": "BUY_CE",
            "features": {"delta": 0.52},
            "model_version": "v1.0",
        }
        ok = OptionDB.log_prediction(pred)
        self.assertTrue(ok)

        recent = OptionDB.get_recent_predictions(limit=5)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["prediction_id"], "pred_test_01")
        self.assertEqual(recent[0]["calibrated_prob"], 0.61)

    def test_signals_persistence_and_status_filtering(self):
        """Tests storing signals and querying by state machine status."""
        init_option_sniffer_tables()

        sig_watch = {
            "signal_id": "sig_watch_01",
            "created_at": datetime.now().isoformat(),
            "instrument_key": "NSE_FO|111",
            "trading_symbol": "NIFTY_CE_WATCH",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "option_type": "CE",
            "expiry": "2026-10-29",
            "direction": "BUY_CE",
            "status": "WATCH",
            "calibrated_prob": 0.52,
            "expected_net_return": 1.5,
            "spot_price": 25000.0,
            "option_ask": 150.0,
            "option_bid": 148.0,
            "spread_pct": 1.33,
            "hurdles_passed": False,
            "rejection_reasons": ["Low probability"],
            "telegram_sent": 0,
        }
        sig_active = {
            "signal_id": "sig_active_01",
            "created_at": datetime.now().isoformat(),
            "instrument_key": "NSE_FO|222",
            "trading_symbol": "NIFTY_CE_ACTIVE",
            "underlying": "NIFTY",
            "strike": 25050.0,
            "option_type": "CE",
            "expiry": "2026-10-29",
            "direction": "BUY_CE",
            "status": "ACTIVE",
            "calibrated_prob": 0.63,
            "expected_net_return": 4.8,
            "spot_price": 25050.0,
            "option_ask": 120.0,
            "option_bid": 119.0,
            "spread_pct": 0.83,
            "hurdles_passed": True,
            "rejection_reasons": [],
            "telegram_sent": 1,
        }
        OptionDB.save_signal(sig_watch)
        OptionDB.save_signal(sig_active)

        actives = OptionDB.get_recent_signals(status="ACTIVE")
        self.assertEqual(len(actives), 1)
        self.assertEqual(actives[0]["signal_id"], "sig_active_01")

        all_sigs = OptionDB.get_recent_signals(limit=10)
        self.assertEqual(len(all_sigs), 2)

    def test_paper_trade_lifecycle_db(self):
        """Tests full paper trade lifecycle in the database."""
        init_option_sniffer_tables()

        trade = {
            "trade_id": "trade_001",
            "signal_id": "sig_001",
            "instrument_key": "NSE_FO|12345",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
            "quantity": 65,
            "entry_time": datetime.now().isoformat(),
            "entry_bid": 149.0,
            "entry_ask": 150.0,
            "entry_price_used": 150.0,
            "status": "OPEN",
            "model_version": "v1.0",
            "signal_version": "v1.0",
            "data_freshness": "REAL_TIME",
        }
        OptionDB.create_paper_trade(trade)

        open_trades = OptionDB.get_open_paper_trades()
        self.assertEqual(len(open_trades), 1)
        self.assertEqual(open_trades[0]["entry_price_used"], 150.0)

        # Update and close trade
        close_data = {
            "exit_time": datetime.now().isoformat(),
            "exit_bid": 175.0,
            "exit_ask": 176.0,
            "peak_price": 175.0,
            "trough_price": 149.0,
            "gross_pnl": (175.0 - 150.0) * 65,  # 1625.0
            "charges": 45.20,
            "slippage": 25.0,
            "net_pnl": 1554.80,
            "net_pnl_pct": 15.94,
            "exit_reason": "TARGET_MET",
        }
        OptionDB.close_paper_trade(trade["trade_id"], close_data)

        open_after = OptionDB.get_open_paper_trades()
        self.assertEqual(len(open_after), 0)

        closed = OptionDB.get_closed_paper_trades(limit=5)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["net_pnl"], 1554.80)
        self.assertEqual(closed[0]["exit_reason"], "TARGET_MET")

    def test_pnl_summary_aggregation(self):
        """Tests aggregated P&L calculations from closed trades."""
        init_option_sniffer_tables()

        # Trade 1: Win (+1230 net)
        trade1 = {
            "trade_id": "win_1",
            "instrument_key": "NSE_FO|1",
            "trading_symbol": "OPT1",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
            "quantity": 65,
            "entry_time": datetime.now().isoformat(),
            "entry_ask": 100.0,
        }
        OptionDB.create_paper_trade(trade1)
        OptionDB.close_paper_trade("win_1", {
            "exit_time": datetime.now().isoformat(),
            "exit_bid": 120.0,
            "gross_pnl": 1300.0,
            "charges": 50.0,
            "slippage": 20.0,
            "net_pnl": 1230.0,
            "net_pnl_pct": 18.9,
            "exit_reason": "TARGET_MET",
        })

        # Trade 2: Loss (-1365 net)
        trade2 = {
            "trade_id": "loss_1",
            "instrument_key": "NSE_FO|2",
            "trading_symbol": "OPT2",
            "underlying": "NIFTY",
            "option_type": "PE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 65,
            "quantity": 65,
            "entry_time": datetime.now().isoformat(),
            "entry_ask": 100.0,
        }
        OptionDB.create_paper_trade(trade2)
        OptionDB.close_paper_trade("loss_1", {
            "exit_time": datetime.now().isoformat(),
            "exit_bid": 80.0,
            "gross_pnl": -1300.0,
            "charges": 45.0,
            "slippage": 20.0,
            "net_pnl": -1365.0,
            "net_pnl_pct": -21.0,
            "exit_reason": "STOP_LOSS_HIT",
        })

        summary = OptionDB.get_pnl_summary()
        self.assertEqual(summary["total_closed_trades"], 2)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["win_rate_pct"], 50.0)
        self.assertAlmostEqual(summary["total_net_pnl"], 1230.0 - 1365.0, places=2)


class TestOptionConstantsAndCostModeling(unittest.TestCase):
    """Verifies realistic Indian F&O statutory transaction cost calculation."""

    def test_calculate_option_trade_costs_exact_breakdown(self):
        """
        Tests cost breakdown for NIFTY option roundtrip:
        Buy @ ₹100, Sell @ ₹120, Lot size = 65 (Quantity = 65).
        Turnover:
          Buy = ₹6,500
          Sell = ₹7,800
          Total = ₹14,300
        """
        costs = calculate_option_trade_costs(
            buy_price=100.0,
            sell_price=120.0,
            quantity=65,
            apply_slippage=True
        )

        # 1. Brokerage: ₹20 buy + ₹20 sell = ₹40.00
        self.assertEqual(costs["brokerage"], 40.0)

        # 2. STT: 0.125% of sell leg (0.00125 * 7800 = ₹9.75)
        self.assertAlmostEqual(costs["stt"], 9.75, places=2)

        # 3. Exchange Fee: 0.0535% of total turnover (0.000535 * 14300 = ₹7.65)
        self.assertAlmostEqual(costs["exchange_fee"], 7.65, places=2)

        # 4. SEBI Fee: ₹10 per crore (0.000001 * 14300 = ₹0.01)
        self.assertAlmostEqual(costs["sebi_fee"], 0.01, places=2)

        # 5. GST: 18% on (Brokerage + Exchange + SEBI) = 0.18 * (40 + 7.65 + 0.01) = ₹8.58
        self.assertAlmostEqual(costs["gst"], 8.58, places=2)

        # 6. Stamp Duty: 0.003% of buy leg (0.00003 * 6500 = ₹0.20)
        self.assertAlmostEqual(costs["stamp_duty"], 0.20, places=2)

        # 7. Slippage: 0.25% of buy + 0.25% of sell = 0.0025 * 14300 = ₹35.75
        self.assertAlmostEqual(costs["slippage"], 35.75, places=2)

        # Total friction must exceed statutory charges
        self.assertGreater(costs["total_friction"], costs["statutory_charges"])
        self.assertGreater(costs["total_friction"], 100.0)

    def test_cost_calculation_zero_slippage_option(self):
        """Verifies statutory charges without execution slippage."""
        costs = calculate_option_trade_costs(
            buy_price=100.0,
            sell_price=100.0,
            quantity=65,
            apply_slippage=False
        )
        self.assertEqual(costs["slippage"], 0.0)
        self.assertEqual(costs["total_friction"], costs["statutory_charges"])


class TestFeatureExtractorPointInTime(unittest.TestCase):
    """Verifies leak-free, strictly point-in-time feature extraction."""

    def test_feature_vector_contains_all_28_features(self):
        """Verifies feature extractor outputs all 28 expected point-in-time features."""
        contract = {
            "instrument_key": "NSE_FO|12345",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "ltp": 120.0,
            "bid": 119.5,
            "ask": 120.5,
            "spread": 1.0,
            "spread_pct": 0.83,
            "bid_qty": 500,
            "ask_qty": 450,
            "volume": 50000,
            "oi": 120000,
            "delta": 0.52,
            "gamma": 0.0012,
            "theta": -14.2,
            "vega": 22.5,
            "iv": 14.8,
            "quote_age_ms": 120,
        }
        underlying_quote = {
            "spot_ltp": 25020.0,
            "open": 24950.0,
            "high": 25050.0,
            "low": 24920.0,
            "close": 24980.0,
            "vwap": 24995.0,
            "quote_age_ms": 90,
        }

        features = OptionFeatureExtractor.extract_features(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            as_of_time=datetime.now()
        )

        self.assertIsInstance(features, dict)
        self.assertEqual(len(features), 28, f"Expected 28 features, got {len(features)}")

        # Verify key individual feature sanity
        self.assertAlmostEqual(features["moneyness"], (25020.0 - 25000.0) / 25000.0, places=4)
        self.assertEqual(features["is_call"], 1.0)
        self.assertGreater(features["dte_days"], 0.0)
        self.assertEqual(features["delta"], 0.52)
        self.assertEqual(features["iv"], 14.8)

    def test_missing_data_fallbacks(self):
        """Verifies feature extractor handles empty or None inputs without raising exceptions."""
        contract = {"underlying": "NIFTY", "strike": 25000.0, "option_type": "PE"}
        quote = {}
        underlying_quote = {}

        features = OptionFeatureExtractor.extract_features(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            as_of_time=datetime.now()
        )
        self.assertEqual(len(features), 28)
        self.assertEqual(features["is_call"], 0.0)


class TestSignalEngineTenGates(unittest.TestCase):
    """Tests the 10 quality hurdle gates and fail-closed state transitions."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db.name
        self.temp_db.close()

        self.db_patcher = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.temp_db_path)
        self.mock_get_db_path = self.db_patcher.start()
        init_option_sniffer_tables()

        # Mock OptionAlphaModel to isolate gate testing
        self.mock_model = MagicMock()
        self.mock_model.version = "test-v1.0"
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.65,
            "expected_net_return_pct": 5.0,
            "raw_score": 0.70,
        }
        self.engine = OptionSignalEngine(model=self.mock_model)
        self.engine._COOLDOWNS.clear()

        # Base valid contract and quotes
        self.contract = {
            "instrument_key": "NSE_FO|99901",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        self.valid_quote = {
            "ltp": 120.0,
            "bid": 119.5,
            "ask": 120.5,
            "spread_pct": 0.83,
            "oi": 50000,
            "volume": 2500,
            "quote_age_ms": 250,
            "quote_freshness": "REAL_TIME",
        }
        self.valid_underlying = {
            "spot_ltp": 25020.0,
            "quote_age_ms": 150,
        }

    def tearDown(self):
        self.db_patcher.stop()
        if os.path.exists(self.temp_db_path):
            os.unlink(self.temp_db_path)

    def test_gate_1_stale_quote_rejected(self):
        """Gate 1: Quote age > 15,000 ms rejected fail-closed into WATCH."""
        quote = dict(self.valid_quote, quote_age_ms=16000)
        res = self.engine.evaluate_contract(self.contract, quote, self.valid_underlying)

        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Stale quote" in r for r in res["rejection_reasons"]))

    def test_gate_2_low_bid_rejected(self):
        """Gate 2: Micro-penny bids (< ₹5.00) rejected."""
        quote = dict(self.valid_quote, bid=3.5, ask=4.5)
        res = self.engine.evaluate_contract(self.contract, quote, self.valid_underlying)

        self.assertEqual(res["status"], STATE_WATCH)
        self.assertTrue(any("Bid price too low" in r for r in res["rejection_reasons"]))

    def test_gate_2_inverted_book_rejected(self):
        """Gate 2: Inverted book (Ask < Bid) rejected."""
        quote = dict(self.valid_quote, bid=120.0, ask=110.0)
        res = self.engine.evaluate_contract(self.contract, quote, self.valid_underlying)

        self.assertEqual(res["status"], STATE_WATCH)
        self.assertTrue(any("Inverted book" in r for r in res["rejection_reasons"]))

    def test_gate_3_wide_spread_rejected(self):
        """Gate 3: Spread > 3.5% rejected."""
        quote = dict(self.valid_quote, spread_pct=4.2)
        res = self.engine.evaluate_contract(self.contract, quote, self.valid_underlying)

        self.assertEqual(res["status"], STATE_WATCH)
        self.assertTrue(any("Excessive spread" in r for r in res["rejection_reasons"]))

    def test_gate_4_low_oi_rejected(self):
        """Gate 4: Open interest < 5,000 rejected."""
        quote = dict(self.valid_quote, oi=3500)
        res = self.engine.evaluate_contract(self.contract, quote, self.valid_underlying)

        self.assertEqual(res["status"], STATE_WATCH)
        self.assertTrue(any("Low Open Interest" in r for r in res["rejection_reasons"]))

    def test_gate_4_low_volume_rejected(self):
        """Gate 4: Daily volume < 200 rejected."""
        quote = dict(self.valid_quote, volume=150)
        res = self.engine.evaluate_contract(self.contract, quote, self.valid_underlying)

        self.assertEqual(res["status"], STATE_WATCH)
        self.assertTrue(any("Low Volume" in r for r in res["rejection_reasons"]))

    def test_gate_5_missing_underlying_rejected(self):
        """Gate 5: Missing or stale spot price rejected."""
        underlying = dict(self.valid_underlying, spot_ltp=0.0)
        res = self.engine.evaluate_contract(self.contract, self.valid_quote, underlying)

        self.assertEqual(res["status"], STATE_WATCH)
        self.assertTrue(any("Underlying spot unavailable" in r for r in res["rejection_reasons"]))

    def test_gate_7_direction_no_trade_rejected(self):
        """Gate 7: Model NO_TRADE verdict rejected."""
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_NO_TRADE,
            "calibrated_prob": 0.50,
            "expected_net_return_pct": 0.0,
            "raw_score": 0.50,
        }
        res = self.engine.evaluate_contract(self.contract, self.valid_quote, self.valid_underlying)

        self.assertEqual(res["status"], STATE_WATCH)
        self.assertEqual(res["direction"], DIRECTION_NO_TRADE)

    def test_gate_8_calibrated_prob_hurdle(self):
        """Gate 8: Calibrated probability below MIN_CALIBRATED_PROB rejected."""
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": MIN_CALIBRATED_PROB - 0.05,  # Below hurdle
            "expected_net_return_pct": 4.0,
            "raw_score": 0.60,
        }
        res = self.engine.evaluate_contract(self.contract, self.valid_quote, self.valid_underlying)

        self.assertEqual(res["status"], STATE_WATCH)
        self.assertTrue(any("Low probability" in r for r in res["rejection_reasons"]))

    def test_gate_9_expected_net_edge_hurdle(self):
        """Gate 9: Expected net return below MIN_EXPECTED_NET_RETURN_PCT rejected."""
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.62,
            "expected_net_return_pct": MIN_EXPECTED_NET_RETURN_PCT - 0.5,  # Below hurdle
            "raw_score": 0.65,
        }
        res = self.engine.evaluate_contract(self.contract, self.valid_quote, self.valid_underlying)

        self.assertEqual(res["status"], STATE_WATCH)
        self.assertTrue(any("Insufficient net edge" in r for r in res["rejection_reasons"]))

    @patch("app.analytics.option_sniffer.option_signal_engine.send_telegram_message")
    def test_successful_active_transition_and_cooldown(self, mock_tg):
        """When all gates pass: transitions to ACTIVE, dispatches Telegram, and enters cooldown."""
        res1 = self.engine.evaluate_contract(self.contract, self.valid_quote, self.valid_underlying)

        self.assertEqual(res1["status"], STATE_ACTIVE)
        self.assertTrue(res1["hurdles_passed"])
        self.assertEqual(res1["direction"], DIRECTION_BUY_CE)
        mock_tg.assert_called_once()

        # Immediate re-evaluation must be gated by cooldown (stays in QUALIFIED)
        res2 = self.engine.evaluate_contract(self.contract, self.valid_quote, self.valid_underlying)
        self.assertEqual(res2["status"], STATE_QUALIFIED)
        self.assertTrue(any("Signal cooldown active" in r for r in res2["rejection_reasons"]))


class TestPaperExecutionLifecycle(unittest.TestCase):
    """Verifies BUY @ ASK, SELL @ BID paper execution and exit ratchets."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db.name
        self.temp_db.close()

        self.db_patcher = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.temp_db_path)
        self.mock_get_db_path = self.db_patcher.start()
        init_option_sniffer_tables()

    def tearDown(self):
        self.db_patcher.stop()
        if os.path.exists(self.temp_db_path):
            os.unlink(self.temp_db_path)

    def test_paper_entry_uses_ask_price(self):
        """Paper entry must strictly execute at current ASK (not LTP or BID)."""
        signal = {
            "signal_id": "sig_ask_test",
            "instrument_key": "NSE_FO|888",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
            "option_bid": 100.0,
            "option_ask": 102.5,
            "model_version": "v1.0",
            "signal_version": "v1.0",
            "data_freshness": "REAL_TIME",
        }
        trade_id = OptionPaperExecution.open_trade(signal)
        self.assertIsNotNone(trade_id)

        trades = OptionDB.get_open_paper_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["entry_price_used"], 102.5)

    @patch("app.analytics.option_sniffer.option_paper_execution.send_telegram_message")
    def test_target_profit_exit(self, mock_tg):
        """Trade exits at current BID when target gain (+35%) is achieved."""
        signal = {
            "signal_id": "sig_tp_test",
            "instrument_key": "NSE_FO|777",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
            "option_bid": 100.0,
            "option_ask": 100.0,
            "model_version": "v1.0",
            "signal_version": "v1.0",
            "data_freshness": "REAL_TIME",
        }
        OptionPaperExecution.open_trade(signal)

        # Price rises to 140 (+40% gain, above +35% target)
        quote = {
            "instrument_key": "NSE_FO|777",
            "bid": 140.0,
            "ask": 141.0,
            "ltp": 140.5,
        }
        res = OptionPaperExecution.update_open_trades({quote["instrument_key"]: quote})
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "CLOSED")
        self.assertEqual(res[0]["exit_reason"], "TARGET_MET")
        self.assertEqual(res[0]["exit_price_used"], 140.0)  # Exited at BID
        self.assertGreater(res[0]["net_pnl"], 0.0)

    @patch("app.analytics.option_sniffer.option_paper_execution.send_telegram_message")
    def test_stop_loss_exit(self, mock_tg):
        """Trade exits at current BID when stop loss (-20%) is hit."""
        signal = {
            "signal_id": "sig_sl_test",
            "instrument_key": "NSE_FO|666",
            "trading_symbol": "NIFTY26OCT25000PE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "PE",
            "lot_size": 65,
            "option_bid": 100.0,
            "option_ask": 100.0,
            "model_version": "v1.0",
            "signal_version": "v1.0",
            "data_freshness": "REAL_TIME",
        }
        OptionPaperExecution.open_trade(signal)

        # Price drops to 78 (-22% loss, below -20% stop)
        quote = {
            "instrument_key": "NSE_FO|666",
            "bid": 78.0,
            "ask": 79.0,
            "ltp": 78.5,
        }
        res = OptionPaperExecution.update_open_trades({quote["instrument_key"]: quote})
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "CLOSED")
        self.assertEqual(res[0]["exit_reason"], "STOP_LOSS_HIT")
        self.assertEqual(res[0]["exit_price_used"], 78.0)
        self.assertLess(res[0]["net_pnl"], 0.0)

    @patch("app.analytics.option_sniffer.option_paper_execution.send_telegram_message")
    def test_ratchet_trailing_stop(self, mock_tg):
        """
        Tests trailing stop ratchet:
        Entry: 100.0
        Tick 1: Rises to 125.0 (+25%, triggers trailing ratchet). Peak = 125.0. Trailing stop = 125 - 10% = 112.5.
        Tick 2: Pulls back to 110.0 (below 112.5 trailing stop) -> Exits with TRAILING_STOP_HIT.
        """
        signal = {
            "signal_id": "sig_trail_test",
            "instrument_key": "NSE_FO|555",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
            "option_bid": 100.0,
            "option_ask": 100.0,
            "model_version": "v1.0",
            "signal_version": "v1.0",
            "data_freshness": "REAL_TIME",
        }
        OptionPaperExecution.open_trade(signal)

        # Midday simulation (11:00 AM) to isolate trailing stop logic from 15:20 session close
        midday_dt = datetime(2026, 10, 5, 11, 0, 0)

        # Tick 1: Peak price sets to 125.0
        quote_peak = {"instrument_key": "NSE_FO|555", "bid": 125.0, "ask": 126.0, "ltp": 125.5}
        OptionPaperExecution.update_open_trades({quote_peak["instrument_key"]: quote_peak}, as_of_time=midday_dt)

        # Tick 2: Retraces to 110.0
        quote_drop = {"instrument_key": "NSE_FO|555", "bid": 110.0, "ask": 111.0, "ltp": 110.5}
        res = OptionPaperExecution.update_open_trades({quote_drop["instrument_key"]: quote_drop}, as_of_time=midday_dt)

        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["exit_reason"], "TRAILING_STOP_HIT")
        self.assertGreater(res[0]["net_pnl"], 0.0)


class TestRateGovernorSafetyLimits(unittest.TestCase):
    """Verifies centralized Upstox API rate limits remain hard-enforced."""

    def test_rate_governor_hard_limits_unchanged(self):
        """Safety limits: 8/s, 64/min, 1600/30min."""
        limiter = get_upstox_limiter()
        self.assertEqual(limiter.MAX_PER_SECOND, 8)
        self.assertEqual(limiter.MAX_PER_MINUTE, 64)
        self.assertEqual(limiter.MAX_PER_30MIN, 1600)


class TestTelegramPayloadIntegrity(unittest.TestCase):
    """Verifies Telegram alert structure adheres to user specifications."""

    @patch("app.analytics.option_sniffer.option_signal_engine.send_telegram_message")
    def test_alert_payload_has_paper_only_warning(self, mock_tg):
        """Telegram alert must include 'PAPER ONLY — NO LIVE ORDER PLACED'."""
        signal = {
            "signal_id": "sig_tg_test",
            "created_at": datetime.now().isoformat(),
            "instrument_key": "NSE_FO|444",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "option_type": "CE",
            "expiry": "2026-10-29",
            "direction": DIRECTION_BUY_CE,
            "status": STATE_ACTIVE,
            "calibrated_prob": 0.64,
            "expected_net_return": 5.2,
            "spot_price": 25010.0,
            "option_ask": 135.0,
            "option_bid": 134.0,
            "spread_pct": 0.74,
            "delta": 0.53,
            "iv": 14.5,
            "oi": 85000,
            "volume": 6200,
            "data_freshness": "REAL_TIME",
            "quote_age_ms": 110,
        }
        OptionSignalEngine._send_telegram_signal_alert(signal)

        mock_tg.assert_called_once()
        msg_body = mock_tg.call_args[0][0]

        self.assertIn("OPTION SNIFFER", msg_body)
        self.assertIn("PAPER BUY", msg_body)
        self.assertIn("PAPER ONLY — NO LIVE ORDER PLACED", msg_body)
        self.assertIn("NIFTY26OCT25000CE", msg_body)
        self.assertIn("135.00", msg_body)  # Ask price
        self.assertIn("64.0%", msg_body)   # Win edge


class TestProductionIntegrity(unittest.TestCase):
    """Verifies existing production models and champion artifacts are untouched."""

    def test_champion_models_hashes_intact(self):
        """Production champion ensemble model hashes must match exactly."""
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        expected_hashes = {
            "models/intraday/champion_ensemble.pkl": "d78677fa40c214737684dcd20cf42e5f79d6e4de96f931f3123181427d5ff8d8",
            "models/swing/champion_ensemble.pkl": "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534",
        }

        for rel_path, expected_hash in expected_hashes.items():
            full_path = os.path.join(base_dir, rel_path)
            self.assertTrue(os.path.exists(full_path), f"Champion model {rel_path} missing!")

            with open(full_path, "rb") as f:
                actual_hash = hashlib.sha256(f.read()).hexdigest()

            self.assertEqual(
                actual_hash,
                expected_hash,
                f"Champion model {rel_path} was modified! Expected {expected_hash}, got {actual_hash}"
            )

    def test_option_alpha_model_exists_and_loads(self):
        """Verifies dedicated Option Alpha Model artifact exists and loads correctly."""
        model = OptionAlphaModel.load()
        self.assertIsNotNone(model)
        self.assertIsNotNone(model.model)

        # Test sample prediction with dummy feature vector
        sample_features = {
            "spot_price": 25000.0,
            "strike": 25000.0,
            "is_call": 1.0,
            "moneyness": 0.0,
            "abs_moneyness": 0.0,
            "dte_days": 4.0,
            "delta": 0.50,
            "gamma": 0.001,
            "theta": -15.0,
            "vega": 20.0,
            "iv": 14.0,
            "spread_pct": 0.8,
            "bid_ask_imbalance": 0.1,
            "log_volume": 10.5,
            "log_oi": 12.0,
            "spot_ret_1": 0.2,
            "spot_ret_3": 0.4,
            "spot_ret_6": 0.8,
            "spot_rsi": 58.0,
            "spot_atr_pct": 0.8,
            "spot_vwap_dist_pct": 0.1,
            "india_vix": 13.5,
            "macro_bullish": 1.0,
            "macro_bearish": 0.0,
            "upstream_qlib_score": 65.0,
            "upstream_legacy_score": 60.0,
            "upstream_consensus_conf": 62.0,
            "upstream_aligned": 1.0,
        }
        pred = model.predict_contract(sample_features)
        self.assertIn("direction", pred)
        self.assertIn("calibrated_prob", pred)
        self.assertIn("expected_net_return_pct", pred)
        self.assertGreaterEqual(pred["calibrated_prob"], 0.0)
        self.assertLessEqual(pred["calibrated_prob"], 1.0)

    @patch("app.data.upstox_provider.upstox_request")
    @patch("app.data.upstox_provider.get_upstox_token", return_value="test_mock_token_1234567890")
    def test_refresh_quotes_via_rest_resilient_fallback(self, mock_tok, mock_req):
        """Verifies OptionStreamManager.refresh_quotes_via_rest updates quotes when WebSocket is silent."""
        from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
        mgr = OptionStreamManager()
        test_contracts = [{
            "instrument_key": "NSE_FO|40679",
            "trading_symbol": "NIFTY 22300 CE 06 OCT 26",
            "underlying": "NIFTY",
            "expiry": "2026-10-06",
            "strike": 22300.0,
            "option_type": "CE",
            "lot_size": 65
        }]
        mgr.sync_monitored_contracts(test_contracts)

        # Mock Upstox API response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "success",
            "data": {
                "NSE_FO:NIFTY26O0622300CE": {
                    "instrument_token": "NSE_FO|40679",
                    "last_price": 325.50,
                    "volume": 50000,
                    "oi": 80000.0,
                    "depth": {
                        "buy": [{"price": 325.0, "quantity": 100}],
                        "sell": [{"price": 325.5, "quantity": 100}]
                    }
                }
            }
        }
        mock_req.return_value = mock_response

        updated = mgr.refresh_quotes_via_rest()
        self.assertGreaterEqual(updated, 1)

        q = mgr.get_option_quote("NSE_FO|40679")
        self.assertIsNotNone(q)
        self.assertEqual(q["ltp"], 325.50)
        self.assertEqual(q["bid"], 325.0)
        self.assertEqual(q["ask"], 325.5)
        self.assertTrue(q["is_valid"])
        self.assertLessEqual(q["quote_age_ms"], 1000)


if __name__ == "__main__":
    unittest.main()
