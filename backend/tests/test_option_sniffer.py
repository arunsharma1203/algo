"""
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
    is_option_trading_window,
    is_option_entry_window,
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
            apply_slippage=True,
            trade_date="LEGACY"
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
        self.engine._DIRECTION_SL_COOLDOWNS.clear()
        self.engine._PORTFOLIO_LOSS_HISTORY.clear()
        OptionSignalEngine._INTROSPECTION_FROZEN_UNTIL = 0.0

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
        res = self.engine.evaluate_contract(self.contract, self.valid_quote, self.valid_underlying, enforce_regime_gate=False)

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
        res = self.engine.evaluate_contract(self.contract, self.valid_quote, self.valid_underlying, enforce_regime_gate=False)

        self.assertEqual(res["status"], STATE_WATCH)
        self.assertTrue(any("Insufficient net edge" in r for r in res["rejection_reasons"]))

    @patch("app.analytics.option_sniffer.option_signal_engine.send_telegram_message")
    def test_successful_active_transition_and_cooldown(self, mock_tg):
        """When all gates pass: transitions to ACTIVE, dispatches Telegram, and enters cooldown."""
        res1 = self.engine.evaluate_contract(self.contract, self.valid_quote, self.valid_underlying, enforce_regime_gate=False)

        self.assertEqual(res1["status"], STATE_ACTIVE)
        self.assertTrue(res1["hurdles_passed"])
        self.assertEqual(res1["direction"], DIRECTION_BUY_CE)
        mock_tg.assert_called_once()

        # Immediate re-evaluation must be gated by cooldown (stays in QUALIFIED)
        res2 = self.engine.evaluate_contract(self.contract, self.valid_quote, self.valid_underlying, enforce_regime_gate=False)
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


class TestOptionSessionCutoffAt1515(unittest.TestCase):
    """Verifies that Option Sniffer strictly respects the 15:15 IST intraday cutoff."""

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

    def test_trading_window_helper_boundaries(self):
        """Verifies is_option_trading_window for weekday times."""
        # Active session: Wednesday 11:00 AM IST
        wed_active = datetime(2026, 10, 7, 11, 0, 0)
        self.assertTrue(is_option_trading_window(wed_active))

        # Pre-market: Wednesday 09:14 AM IST
        wed_pre = datetime(2026, 10, 7, 9, 14, 59)
        self.assertFalse(is_option_trading_window(wed_pre))

        # Right at cutoff: Wednesday 15:15:00 IST
        wed_cutoff = datetime(2026, 10, 7, 15, 15, 0)
        self.assertFalse(is_option_trading_window(wed_cutoff))

        # Past cutoff: Wednesday 15:30:00 IST
        wed_past = datetime(2026, 10, 7, 15, 30, 0)
        self.assertFalse(is_option_trading_window(wed_past))

        # Post-market: Wednesday 16:00:00 IST
        wed_eod = datetime(2026, 10, 7, 16, 0, 0)
        self.assertFalse(is_option_trading_window(wed_eod))

        # Weekend: Saturday 11:00 AM IST
        sat_active_time = datetime(2026, 10, 10, 11, 0, 0)
        self.assertFalse(is_option_trading_window(sat_active_time))

    def test_open_trades_square_off_at_1515(self):
        """At 15:15:00 IST or later, all open paper trades square off with SESSION_END_SQUAREOFF."""
        signal = {
            "signal_id": "sig_sq_test",
            "instrument_key": "NSE_FO|999",
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

        # 1. At 15:14:59 IST: Trade remains OPEN
        time_before_cutoff = datetime(2026, 10, 7, 15, 14, 59)
        quote = {"instrument_key": "NSE_FO|999", "bid": 101.0, "ask": 102.0, "ltp": 101.5}
        res_before = OptionPaperExecution.update_open_trades(
            {quote["instrument_key"]: quote},
            as_of_time=time_before_cutoff
        )
        self.assertEqual(len(res_before), 0)
        self.assertEqual(len(OptionDB.get_open_paper_trades()), 1)

        # 2. At 15:15:00 IST: Trade must square off
        time_at_cutoff = datetime(2026, 10, 7, 15, 15, 0)
        res_cutoff = OptionPaperExecution.update_open_trades(
            {quote["instrument_key"]: quote},
            as_of_time=time_at_cutoff
        )
        self.assertEqual(len(res_cutoff), 1)
        self.assertEqual(res_cutoff[0]["status"], "CLOSED")
        self.assertEqual(res_cutoff[0]["exit_reason"], "SESSION_END_SQUAREOFF")
        self.assertEqual(len(OptionDB.get_open_paper_trades()), 0)

    def test_open_trades_square_off_post_market(self):
        """If trades were somehow open at 16:00 IST, they must square off with SESSION_END_SQUAREOFF."""
        signal = {
            "signal_id": "sig_pm_test",
            "instrument_key": "NSE_FO|998",
            "trading_symbol": "BANKNIFTY26OCT54000PE",
            "underlying": "BANKNIFTY",
            "strike": 54000.0,
            "expiry": "2026-10-29",
            "option_type": "PE",
            "lot_size": 15,
            "option_bid": 200.0,
            "option_ask": 200.0,
            "model_version": "v1.0",
            "signal_version": "v1.0",
            "data_freshness": "REAL_TIME",
        }
        OptionPaperExecution.open_trade(signal)

        quote = {"instrument_key": "NSE_FO|998", "bid": 202.0, "ask": 204.0, "ltp": 203.0}
        post_market_time = datetime(2026, 10, 7, 16, 0, 0)
        res = OptionPaperExecution.update_open_trades(
            {quote["instrument_key"]: quote},
            as_of_time=post_market_time
        )
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "CLOSED")
        self.assertEqual(res[0]["exit_reason"], "SESSION_END_SQUAREOFF")

    def test_evaluate_contract_halted_at_1515(self):
        """Signal engine must reject candidates at or after 15:15 IST when enforce_cutoff is True."""
        mock_model = MagicMock()
        mock_model.version = "test-v1.0"
        engine = OptionSignalEngine(model=mock_model)

        contract = {
            "instrument_key": "NSE_FO|997",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "ltp": 120.0, "bid": 119.5, "ask": 120.5, "spread_pct": 0.83,
            "oi": 50000, "volume": 2500, "quote_age_ms": 250, "quote_freshness": "REAL_TIME"
        }
        underlying = {"spot_ltp": 25020.0, "quote_age_ms": 150}

        time_at_cutoff = datetime(2026, 10, 7, 15, 15, 0)
        res = engine.evaluate_contract(
            contract, quote, underlying,
            as_of_time=time_at_cutoff,
            enforce_cutoff=True
        )
        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Trading session cutoff passed" in r for r in res["rejection_reasons"]))

    def test_open_trade_blocked_past_cutoff(self):
        """OptionPaperExecution.open_trade returns None when called past 15:15 with enforce_cutoff=True."""
        signal = {
            "signal_id": "sig_blk_test",
            "instrument_key": "NSE_FO|996",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
            "option_bid": 100.0,
            "option_ask": 100.0,
        }
        time_past_cutoff = datetime(2026, 10, 7, 15, 20, 0)
        trade_id = OptionPaperExecution.open_trade(
            signal,
            as_of_time=time_past_cutoff,
            enforce_cutoff=True
        )
        self.assertIsNone(trade_id)


class TestAdaptiveOptionsAndRegimeShield(unittest.TestCase):
    """
    Verifies Phase 1 & 2 Adaptive Options Upgrades:
      1. Golden Entry Window (09:30 to 12:45 IST) Curfew.
      2. Dynamic Regime Shield (Chop Defense, Bear CE Block, Bull PE Block).
      3. Upstream Dual-Engine Concurrence & Conflict Shield.
      4. Trailing Stop Breakeven Profit Floor (+2% lock-in after +15% MFE).
    """

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db.name
        self.temp_db.close()
        self.patcher = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.temp_db_path)
        self.patcher.start()
        init_option_sniffer_tables()

        self.mock_model = MagicMock()
        self.mock_model.version = "test-model"
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.60,
            "expected_net_return_pct": 5.0,
        }

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except Exception:
                pass

    def test_is_option_entry_window_timing(self):
        """Validates entry window helper for weekdays vs weekends and hour brackets."""
        # Wednesday (weekday)
        t_early = datetime(2026, 10, 7, 9, 20, 0)      # 09:20 -> False (too early)
        t_open = datetime(2026, 10, 7, 9, 30, 0)       # 09:30 -> True (window open)
        t_morning = datetime(2026, 10, 7, 11, 15, 0)    # 11:15 -> True
        t_midday = datetime(2026, 10, 7, 12, 30, 0)     # 12:30 -> True (midday window)
        t_curfew = datetime(2026, 10, 7, 13, 15, 0)     # 13:15 -> False (curfew reached)
        t_afternoon = datetime(2026, 10, 7, 14, 30, 0)  # 14:30 -> False (decay zone)
        t_weekend = datetime(2026, 10, 10, 10, 0, 0)    # Saturday -> False

        self.assertFalse(is_option_entry_window(t_early))
        self.assertTrue(is_option_entry_window(t_open))
        self.assertTrue(is_option_entry_window(t_morning))
        self.assertTrue(is_option_entry_window(t_midday))
        self.assertFalse(is_option_entry_window(t_curfew))
        self.assertFalse(is_option_entry_window(t_afternoon))
        self.assertFalse(is_option_entry_window(t_weekend))

    def test_entry_window_curfew_blocks_new_signals(self):
        """New signals evaluated after 13:15 IST are halted to prevent afternoon theta decay."""
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|991",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "bid": 100.0, "ask": 101.0, "ltp": 100.5, "oi": 50000,
            "volume": 20000, "spread_pct": 1.0, "quote_age_ms": 100,
        }
        underlying = {"spot_ltp": 25020.0, "quote_age_ms": 150}

        # Time at 13:20 IST (after 13:15 curfew)
        time_at_1320 = datetime(2026, 10, 7, 13, 20, 0)
        res = engine.evaluate_contract(
            contract, quote, underlying,
            as_of_time=time_at_1320,
            enforce_cutoff=True
        )
        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Outside golden option entry window" in r for r in res["rejection_reasons"]))

    def test_regime_shield_chop_blocks_all_options(self):
        """CHOP / SIDEWAYS market regime dynamically halts all option buying."""
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|992",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "bid": 100.0, "ask": 101.0, "ltp": 100.5, "oi": 50000,
            "volume": 20000, "spread_pct": 1.0, "quote_age_ms": 100,
        }
        underlying = {"spot_ltp": 25020.0, "quote_age_ms": 150}

        # Morning time 10:30 IST during CHOP regime
        morning_time = datetime(2026, 10, 7, 10, 30, 0)
        macro_chop = {"intraday_regime_verdict": "INTRADAY_NEUTRAL_CHOP"}

        res = engine.evaluate_contract(
            contract, quote, underlying,
            macro_context=macro_chop,
            as_of_time=morning_time,
            enforce_cutoff=True
        )
        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("CHOP / SIDEWAYS" in r for r in res["rejection_reasons"]))

    def test_regime_shield_bear_blocks_call_options(self):
        """BEAR regime dynamically blocks CALL (CE) options."""
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|993",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "bid": 100.0, "ask": 101.0, "ltp": 100.5, "oi": 50000,
            "volume": 20000, "spread_pct": 1.0, "quote_age_ms": 100,
        }
        underlying = {"spot_ltp": 25020.0, "quote_age_ms": 150}

        morning_time = datetime(2026, 10, 7, 10, 30, 0)
        macro_bear = {"intraday_regime_verdict": "INTRADAY_STRONG_BEAR"}

        res = engine.evaluate_contract(
            contract, quote, underlying,
            macro_context=macro_bear,
            as_of_time=morning_time,
            enforce_cutoff=True
        )
        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Bearish regime" in r for r in res["rejection_reasons"]))

    def test_regime_shield_bull_blocks_put_options(self):
        """BULL regime dynamically blocks PUT (PE) options."""
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_PE,
            "calibrated_prob": 0.60,
            "expected_net_return_pct": 5.0,
        }
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|994",
            "trading_symbol": "NIFTY26OCT25000PE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "PE",
            "lot_size": 65,
        }
        quote = {
            "bid": 100.0, "ask": 101.0, "ltp": 100.5, "oi": 50000,
            "volume": 20000, "spread_pct": 1.0, "quote_age_ms": 100,
        }
        underlying = {"spot_ltp": 25020.0, "quote_age_ms": 150}

        morning_time = datetime(2026, 10, 7, 10, 30, 0)
        macro_bull = {"intraday_regime_verdict": "INTRADAY_STRONG_BULL"}

        res = engine.evaluate_contract(
            contract, quote, underlying,
            macro_context=macro_bull,
            as_of_time=morning_time,
            enforce_cutoff=True
        )
        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Bullish regime" in r for r in res["rejection_reasons"]))

    def test_dual_engine_conflict_blocks_options(self):
        """Upstream AI engine conflict blocks option signals."""
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|995",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "bid": 100.0, "ask": 101.0, "ltp": 100.5, "oi": 50000,
            "volume": 20000, "spread_pct": 1.0, "quote_age_ms": 100,
        }
        underlying = {"spot_ltp": 25020.0, "quote_age_ms": 150}

        morning_time = datetime(2026, 10, 7, 10, 30, 0)
        upstream_conflicted = {"is_conflicted": True}

        res = engine.evaluate_contract(
            contract, quote, underlying,
            upstream_signals=upstream_conflicted,
            as_of_time=morning_time,
            enforce_cutoff=True
        )
        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("conflict detected" in r for r in res["rejection_reasons"]))

    def test_breakeven_ratchet_floor_protects_profits(self):
        """Trailing stop ratchet enforces +2% floor once MFE reaches +15%, locking in profit."""
        # 1. Create open trade with entry ask = 100.0
        signal = {
            "signal_id": "sig_ratchet_test",
            "instrument_key": "NSE_FO|997",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
            "option_bid": 99.0,
            "option_ask": 100.0,
        }
        trade_id = OptionPaperExecution.open_trade(signal)
        self.assertIsNotNone(trade_id)

        # 2. First update: Price surges to 116.0 (MFE reaches +16.0%, triggering ratchet)
        now = datetime(2026, 10, 16, 11, 0, 0)
        live_quotes_surge = {
            "NSE_FO|997": {"bid": 116.0, "ask": 117.0, "ltp": 116.0}
        }
        closed_surge = OptionPaperExecution.update_open_trades(live_quotes_surge, as_of_time=now)
        self.assertEqual(len(closed_surge), 0)  # Trade still open, peak updated to 116.0

        # 3. Second update: Price pulls back to 103.0 (which is <= 116 * 0.90 = 104.4, but above 102.0 floor)
        live_quotes_pullback = {
            "NSE_FO|997": {"bid": 103.0, "ask": 104.0, "ltp": 103.0}
        }
        closed_pullback = OptionPaperExecution.update_open_trades(live_quotes_pullback, as_of_time=now)
        self.assertEqual(len(closed_pullback), 1)
        self.assertEqual(closed_pullback[0]["exit_reason"], "TRAILING_STOP_HIT")
        self.assertGreater(closed_pullback[0]["net_pnl"], 0.0)  # Positive net P&L guaranteed

    def test_banknifty_bear_momentum_allows_pe_during_nifty_chop(self):
        """BANKNIFTY with strong intraday bear momentum allows PE even when NIFTY is in CHOP."""
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_PE,
            "calibrated_prob": 0.62,
            "expected_net_return_pct": 6.5,
        }
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|998",
            "trading_symbol": "BANKNIFTY26OCT55000PE",
            "underlying": "BANKNIFTY",
            "strike": 55000.0,
            "expiry": "2026-10-29",
            "option_type": "PE",
            "lot_size": 30,
        }
        quote = {
            "bid": 250.0, "ask": 252.0, "ltp": 251.0, "oi": 60000,
            "volume": 35000, "spread_pct": 0.8, "quote_age_ms": 100,
        }
        # BANKNIFTY down -0.78% from open (55000 vs 55430 open)
        underlying = {"spot_ltp": 55000.0, "open": 55430.0, "close": 55400.0, "quote_age_ms": 150}

        morning_time = datetime(2026, 10, 7, 10, 30, 0)
        macro_chop = {"intraday_regime_verdict": "INTRADAY_NEUTRAL_CHOP"}

        res = engine.evaluate_contract(
            contract, quote, underlying,
            macro_context=macro_chop,
            as_of_time=morning_time,
            enforce_cutoff=True,
            enforce_regime_gate=False
        )
        self.assertIn(res["status"], (STATE_QUALIFIED, STATE_ACTIVE))
        self.assertTrue(res["hurdles_passed"])
        self.assertEqual(res["direction"], DIRECTION_BUY_PE)

    def test_banknifty_bear_momentum_blocks_ce(self):
        """BANKNIFTY with strong intraday bear momentum blocks CE options."""
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|999",
            "trading_symbol": "BANKNIFTY26OCT55000CE",
            "underlying": "BANKNIFTY",
            "strike": 55000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 30,
        }
        quote = {
            "bid": 250.0, "ask": 252.0, "ltp": 251.0, "oi": 60000,
            "volume": 35000, "spread_pct": 0.8, "quote_age_ms": 100,
        }
        underlying = {"spot_ltp": 55000.0, "open": 55430.0, "close": 55400.0, "quote_age_ms": 150}

        morning_time = datetime(2026, 10, 7, 10, 30, 0)
        macro_chop = {"intraday_regime_verdict": "INTRADAY_NEUTRAL_CHOP"}

        res = engine.evaluate_contract(
            contract, quote, underlying,
            macro_context=macro_chop,
            as_of_time=morning_time,
            enforce_cutoff=True
        )
        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("intraday bear momentum" in r for r in res["rejection_reasons"]))

    def test_banknifty_flat_blocked_during_nifty_chop(self):
        """BANKNIFTY when flat is guarded by macro CHOP regime."""
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|1000",
            "trading_symbol": "BANKNIFTY26OCT55000PE",
            "underlying": "BANKNIFTY",
            "strike": 55000.0,
            "expiry": "2026-10-29",
            "option_type": "PE",
            "lot_size": 30,
        }
        quote = {
            "bid": 250.0, "ask": 252.0, "ltp": 251.0, "oi": 60000,
            "volume": 35000, "spread_pct": 0.8, "quote_age_ms": 100,
        }
        # Flat drift: spot 55000, open 55020 (drift = -0.04%)
        underlying = {"spot_ltp": 55000.0, "open": 55020.0, "close": 55010.0, "quote_age_ms": 150}

        morning_time = datetime(2026, 10, 7, 10, 30, 0)
        macro_chop = {"intraday_regime_verdict": "INTRADAY_NEUTRAL_CHOP"}

        res = engine.evaluate_contract(
            contract, quote, underlying,
            macro_context=macro_chop,
            as_of_time=morning_time,
            enforce_cutoff=True
        )
        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("CHOP / SIDEWAYS" in r for r in res["rejection_reasons"]))

    def test_gate_5d_heavyweight_divergence_blocks_ce(self):
        """Gate 5D: If all tracked heavyweights (RELIANCE & HDFCBANK) are below VWAP, CALL (CE) is blocked."""
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|99901",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "bid": 120.0, "ask": 120.5, "ltp": 120.2, "oi": 50000,
            "volume": 2500, "spread_pct": 0.4, "quote_age_ms": 100,
        }
        # Nifty spot is above VWAP
        underlying = {"spot_ltp": 25150.0, "open": 25000.0, "high": 25160.0, "low": 24980.0, "close": 25140.0, "vwap": 25050.0, "quote_age_ms": 100}

        # Heavyweights both slumping below VWAP
        hw_quotes = {
            "RELIANCE": {"spot_ltp": 2900.0, "vwap": 2950.0},
            "HDFCBANK": {"spot_ltp": 1600.0, "vwap": 1640.0},
        }

        res = engine.evaluate_contract(
            contract, quote, underlying,
            heavyweight_quotes=hw_quotes,
            as_of_time=datetime(2026, 10, 14, 11, 0, 0),
            enforce_cutoff=True
        )
        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Gate 5D" in r and "below VWAP" in r for r in res["rejection_reasons"]))

    def test_gate_5d_heavyweight_divergence_blocks_pe(self):
        """Gate 5D: If all tracked heavyweights (RELIANCE & HDFCBANK) are above VWAP, PUT (PE) is blocked."""
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|99902",
            "trading_symbol": "NIFTY26OCT25000PE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "PE",
            "lot_size": 65,
        }
        quote = {
            "bid": 120.0, "ask": 120.5, "ltp": 120.2, "oi": 50000,
            "volume": 2500, "spread_pct": 0.4, "quote_age_ms": 100,
        }
        # Nifty spot is below VWAP
        underlying = {"spot_ltp": 24850.0, "open": 25000.0, "high": 25020.0, "low": 24840.0, "close": 24860.0, "vwap": 24950.0, "quote_age_ms": 100}

        # Heavyweights both rallying above VWAP
        hw_quotes = {
            "RELIANCE": {"spot_ltp": 3000.0, "vwap": 2950.0},
            "HDFCBANK": {"spot_ltp": 1680.0, "vwap": 1640.0},
        }

        res = engine.evaluate_contract(
            contract, quote, underlying,
            heavyweight_quotes=hw_quotes,
            as_of_time=datetime(2026, 10, 14, 11, 0, 0),
            enforce_cutoff=True
        )
        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Gate 5D" in r and "above VWAP" in r for r in res["rejection_reasons"]))

    def test_gate_5d_heavyweight_concurrence_allows_trade(self):
        """Gate 5D: When at least one heavyweight confirms trend, trade passes Gate 5D."""
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|99901",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "bid": 120.0, "ask": 120.5, "ltp": 120.2, "oi": 50000,
            "volume": 2500, "spread_pct": 0.4, "quote_age_ms": 100,
        }
        underlying = {"spot_ltp": 25150.0, "open": 25000.0, "high": 25160.0, "low": 24980.0, "close": 25140.0, "vwap": 25050.0, "quote_age_ms": 100}

        # RELIANCE is above VWAP (bullish), HDFCBANK is below VWAP
        hw_quotes = {
            "RELIANCE": {"spot_ltp": 3000.0, "vwap": 2950.0},
            "HDFCBANK": {"spot_ltp": 1630.0, "vwap": 1640.0},
        }

        res = engine.evaluate_contract(
            contract, quote, underlying,
            heavyweight_quotes=hw_quotes,
            as_of_time=datetime(2026, 10, 14, 11, 0, 0),
            enforce_cutoff=True
        )
        # Should not be rejected by Gate 5D
        self.assertFalse(any("Gate 5D" in r for r in res.get("rejection_reasons", [])))

    def test_gate_5d_heavyweight_divergence_blocks_ce_even_in_crbi_breakout(self):
        """CRB-I Immunity: Heavyweight divergence must veto CE even if spot is breaking Day High."""
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|99901",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "bid": 120.0, "ask": 120.5, "ltp": 120.2, "oi": 50000,
            "volume": 2500, "spread_pct": 0.4, "quote_age_ms": 100,
        }
        # Spot is at Day High + 5 pts (Day High Breakout!)
        underlying = {
            "spot_ltp": 25200.0, "open": 25000.0, "high": 25195.0, "low": 24980.0,
            "close": 25200.0, "vwap": 25050.0, "quote_age_ms": 100
        }

        # Heavyweights both falling below VWAP (contrary supply overhead)
        hw_quotes = {
            "RELIANCE": {"spot_ltp": 2900.0, "vwap": 2950.0},
            "HDFCBANK": {"spot_ltp": 1600.0, "vwap": 1640.0},
        }

        res = engine.evaluate_contract(
            contract, quote, underlying,
            heavyweight_quotes=hw_quotes,
            as_of_time=datetime(2026, 10, 21, 9, 30, 1),
            enforce_cutoff=True
        )
        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Heavyweight Concurrence" in r and "below VWAP" in r for r in res["rejection_reasons"]))
        # Verify CRB-I is NOT active when heavyweights are contrary
        self.assertFalse(res.get("is_crbi_active", False))


class TestPersistentCircuitBreakerAndParity(unittest.TestCase):
    """
    Verifies institutional-grade safeguards:
      1. Persistent SQLite-backed 2-Loss Session Circuit Breaker.
      2. Max Daily Drawdown Lockout (-₹2,500 ceiling).
      3. Anti-Clustering Shield (Max 1 contract per underlying).
      4. Cross-Index Contagion Lockout (20-minute stabilization after SL).
      5. Runtime-to-Disk SHA-256 Parity Handshake.
    """

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db.name
        self.temp_db.close()

        self.db_patcher = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.temp_db_path)
        self.mock_get_db_path = self.db_patcher.start()

        init_option_sniffer_tables()

        self.mock_model = MagicMock(spec=OptionAlphaModel)
        self.mock_model.version = "v1.0-champion-test"
        self.mock_model.artifact_hash = "mock_hash_123"
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.65,
            "expected_net_return_pct": 8.5,
        }

        # Clear engine class-level state between tests
        OptionSignalEngine._COOLDOWNS.clear()
        OptionSignalEngine._DIRECTION_SL_COOLDOWNS.clear()
        OptionSignalEngine._PORTFOLIO_LOSS_HISTORY.clear()
        OptionSignalEngine._INTROSPECTION_FROZEN_UNTIL = 0.0

    def tearDown(self):
        self.db_patcher.stop()
        if os.path.exists(self.temp_db_path):
            os.unlink(self.temp_db_path)

    def _insert_closed_trade(self, trade_id, symbol, underlying, pnl, exit_time_str, exit_reason="STOP_LOSS"):
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("""
            INSERT INTO option_sniffer_paper_trades (
                trade_id, instrument_key, trading_symbol, underlying, option_type,
                strike, expiry, lot_size, quantity, entry_time, entry_ask,
                entry_price_used, exit_time, exit_price_used, status, gross_pnl, charges, net_pnl, exit_reason
            ) VALUES (?, ?, ?, ?, 'CE', 25000, '2026-10-29', 65, 65, '2026-10-16 09:30:00', 100.0, 100.0, ?, ?, 'CLOSED', ?, 50.0, ?, ?)
        """, (trade_id, f"NSE_FO|{trade_id}", symbol, underlying, exit_time_str, 80.0, pnl + 50.0, pnl, exit_reason))
        conn.commit()
        conn.close()

    def _insert_open_trade(self, trade_id, symbol, underlying, mfe_pct=0.0, net_pnl_pct=0.0):
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("""
            INSERT INTO option_sniffer_paper_trades (
                trade_id, instrument_key, trading_symbol, underlying, option_type,
                strike, expiry, lot_size, quantity, entry_time, entry_ask,
                entry_price_used, status, mfe_pct, net_pnl_pct
            ) VALUES (?, ?, ?, ?, 'CE', 25000, '2026-10-29', 65, 65, '2026-10-16 09:30:00', 100.0, 100.0, 'OPEN', ?, ?)
        """, (trade_id, f"NSE_FO|{trade_id}", symbol, underlying, mfe_pct, net_pnl_pct))
        conn.commit()
        conn.close()

    def test_daily_circuit_breaker_detects_two_consecutive_losses(self):
        """2 consecutive losses triggers dynamic introspection pause (Tier 1)."""
        today = datetime.now().strftime("%Y-%m-%d")
        self._insert_closed_trade("t1", "NIFTY26OCT25000CE", "NIFTY", -1000.0, f"{today} 09:45:00")
        self._insert_closed_trade("t2", "NIFTY26OCT25100CE", "NIFTY", -1100.0, f"{today} 10:00:00")

        eval_dt = datetime.strptime(f"{today} 10:15:00", "%Y-%m-%d %H:%M:%S")
        telem = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date=today, as_of_time=eval_dt)
        self.assertEqual(telem["consecutive_losses"], 2)
        self.assertTrue(telem["is_consecutive_limit_hit"])
        self.assertTrue(telem["is_pause_active"])
        self.assertFalse(telem["is_session_halted"])  # Hard halt reserved for -₹3,500 daily ceiling
        self.assertIn("Dynamic Introspection Pause Active", telem["halt_reason"])

    def test_daily_circuit_breaker_streak_broken_by_win(self):
        """A winning trade breaks the consecutive loss streak."""
        today = datetime.now().strftime("%Y-%m-%d")
        self._insert_closed_trade("t1", "NIFTY26OCT25000CE", "NIFTY", -1000.0, f"{today} 09:45:00")
        self._insert_closed_trade("t2", "NIFTY26OCT25100CE", "NIFTY", +1500.0, f"{today} 10:15:00")

        telem = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date=today)
        self.assertEqual(telem["consecutive_losses"], 0)
        self.assertFalse(telem["is_consecutive_limit_hit"])
        self.assertFalse(telem["is_pause_active"])
        self.assertFalse(telem["is_session_halted"])

    def test_daily_circuit_breaker_max_drawdown_lockout(self):
        """Cumulative loss exceeding ₹3,500 trips hard session lockout (Tier 2)."""
        today = datetime.now().strftime("%Y-%m-%d")
        self._insert_closed_trade("t1", "NIFTY26OCT25000CE", "NIFTY", -3800.0, f"{today} 09:45:00")

        telem = OptionDB.get_daily_circuit_breaker_telemetry(as_of_date=today)
        self.assertTrue(telem["is_max_loss_limit_hit"])
        self.assertTrue(telem["is_session_halted"])
        self.assertIn("Daily max loss ceiling reached", telem["halt_reason"])

    def test_circuit_breaker_blocks_in_signal_engine_gate_9b(self):
        """Gate 9B blocks during 30m dynamic introspection pause after 2 losses."""
        today = "2026-10-16"
        self._insert_closed_trade("t1", "NIFTY26OCT25000CE", "NIFTY", -1000.0, f"{today} 09:45:00")
        self._insert_closed_trade("t2", "NIFTY26OCT25100CE", "NIFTY", -1000.0, f"{today} 10:00:00")

        # Fresh engine instance simulating server restart
        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|99901",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "bid": 120.0, "ask": 120.5, "ltp": 120.2, "oi": 50000,
            "volume": 2500, "spread_pct": 0.4, "quote_age_ms": 100,
        }
        underlying = {"spot_ltp": 25150.0, "open": 25000.0, "high": 25160.0, "low": 24980.0, "close": 25140.0, "vwap": 25050.0, "quote_age_ms": 100}

        # Attempt to evaluate 15 minutes after 2nd loss (during 30m pause)
        eval_dt = datetime.strptime(f"{today} 10:15:00", "%Y-%m-%d %H:%M:%S")
        res = engine.evaluate_contract(contract, quote, underlying, as_of_time=eval_dt, enforce_cutoff=True, enforce_regime_gate=False)

        self.assertNotEqual(res["status"], STATE_ACTIVE)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Dynamic Introspection Pause Active" in r for r in res.get("rejection_reasons", [])))

    def test_anti_clustering_shield_blocks_duplicate_underlying_position(self):
        """Gate 9: Max 1 position per underlying prevents multi-strike cluster bleed."""
        self._insert_open_trade("open1", "NIFTY26OCT25000CE", "NIFTY")

        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|99902",
            "trading_symbol": "NIFTY26OCT25050CE",
            "underlying": "NIFTY",
            "strike": 25050.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "bid": 120.0, "ask": 120.5, "ltp": 120.2, "oi": 50000,
            "volume": 2500, "spread_pct": 0.4, "quote_age_ms": 100,
        }
        underlying = {"spot_ltp": 25150.0, "open": 25000.0, "high": 25160.0, "low": 24980.0, "close": 25140.0, "vwap": 25050.0, "quote_age_ms": 100}

        res = engine.evaluate_contract(
            contract, quote, underlying,
            as_of_time=datetime(2026, 10, 16, 10, 0, 0),
            enforce_cutoff=True,
            enforce_regime_gate=False
        )
        self.assertNotEqual(res["status"], STATE_ACTIVE)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Anti-Clustering Shield" in r and "Active NIFTY position exists" in r for r in res.get("rejection_reasons", [])))

    def test_cross_index_contagion_lockout(self):
        """Gate 9C: When NIFTY hits SL, BANKNIFTY is locked out for 20 minutes."""
        today = "2026-10-16"
        self._insert_closed_trade("t1", "NIFTY26OCT25000CE", "NIFTY", -800.0, f"{today} 10:00:00")

        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|99903",
            "trading_symbol": "BANKNIFTY26OCT52000CE",
            "underlying": "BANKNIFTY",
            "strike": 52000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 15,
        }
        quote = {
            "bid": 300.0, "ask": 301.0, "ltp": 300.5, "oi": 25000,
            "volume": 1200, "spread_pct": 0.3, "quote_age_ms": 100,
        }
        underlying = {"spot_ltp": 52100.0, "open": 51800.0, "high": 52150.0, "low": 51750.0, "close": 52090.0, "vwap": 51900.0, "quote_age_ms": 100}

        # Attempt to trade 5 minutes later at 10:05:00 (< 20 mins)
        eval_dt = datetime.strptime(f"{today} 10:05:00", "%Y-%m-%d %H:%M:%S")
        res = engine.evaluate_contract(contract, quote, underlying, as_of_time=eval_dt, enforce_cutoff=True, enforce_regime_gate=False)

        self.assertNotEqual(res["status"], STATE_ACTIVE)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Cross-Index Contagion Lockout" in r for r in res.get("rejection_reasons", [])))

    def test_runtime_parity_handshake(self):
        """OptionSnifferOrchestrator computes and verifies SHA-256 module parity."""
        from app.analytics.option_sniffer.option_sniffer_orchestrator import OptionSnifferOrchestrator
        parity = OptionSnifferOrchestrator.check_runtime_disk_parity()
        self.assertTrue(parity["in_sync"])
        self.assertEqual(parity["status"], "SYNCHRONIZED")
        self.assertEqual(len(parity["stale_files"]), 0)

    def test_smart_pyramiding_unlocks_second_contract_when_first_trade_is_winning(self):
        """Smart Pyramiding Gate: Unlocks 2nd contract when Contract #1 has MFE >= +15% and PnL >= +2%."""
        self._insert_open_trade("open1", "NIFTY26OCT25000CE", "NIFTY", mfe_pct=18.5, net_pnl_pct=8.2)

        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|99904",
            "trading_symbol": "NIFTY26OCT25050CE",
            "underlying": "NIFTY",
            "strike": 25050.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "bid": 120.0, "ask": 120.5, "ltp": 120.2, "oi": 50000,
            "volume": 2500, "spread_pct": 0.4, "quote_age_ms": 100,
        }
        underlying = {"spot_ltp": 25150.0, "open": 25000.0, "high": 25160.0, "low": 24980.0, "close": 25140.0, "vwap": 25050.0, "quote_age_ms": 100}

        res = engine.evaluate_contract(
            contract, quote, underlying,
            as_of_time=datetime(2026, 10, 16, 10, 0, 0),
            enforce_cutoff=True,
            enforce_regime_gate=False
        )
        self.assertEqual(res["status"], STATE_ACTIVE)
        self.assertTrue(res["hurdles_passed"])
        self.assertEqual(len(res.get("rejection_reasons", [])), 0)

    def test_hard_daily_circuit_breaker_halts_gate_9b_permanently(self):
        """Gate 9B halts session permanently when daily cumulative losses breach -₹3,500."""
        today = "2026-10-16"
        self._insert_closed_trade("t1", "NIFTY26OCT25000CE", "NIFTY", -3600.0, f"{today} 09:45:00")

        engine = OptionSignalEngine(model=self.mock_model)
        contract = {
            "instrument_key": "NSE_FO|99905",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        quote = {
            "bid": 120.0, "ask": 120.5, "ltp": 120.2, "oi": 50000,
            "volume": 2500, "spread_pct": 0.4, "quote_age_ms": 100,
        }
        underlying = {"spot_ltp": 25150.0, "open": 25000.0, "high": 25160.0, "low": 24980.0, "close": 25140.0, "vwap": 25050.0, "quote_age_ms": 100}

        eval_dt = datetime.strptime(f"{today} 12:00:00", "%Y-%m-%d %H:%M:%S")
        res = engine.evaluate_contract(contract, quote, underlying, as_of_time=eval_dt, enforce_cutoff=True, enforce_regime_gate=False)

        self.assertNotEqual(res["status"], STATE_ACTIVE)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Daily Session Circuit Breaker Tripped" in r for r in res.get("rejection_reasons", [])))


if __name__ == "__main__":
    unittest.main()
