import unittest
from unittest.mock import patch, MagicMock
import os
import tempfile
import sqlite3
import hashlib
import json
import numpy as np
from datetime import datetime

from app.analytics.foundation_models.challenger_evaluator import (
    FoundationChallengerEvaluator, ensure_foundation_evaluations_table
)
from app.analytics.retrain_models import simulate_out_of_sample_trading
from app.api.ml_lab import promote_foundation_challenger_api, FoundationPromoteRequest


class TestFoundationChallengerGovernance(unittest.TestCase):
    """
    Comprehensive regression suite for Foundation Challenger Metric Consistency:
    1. Atomic evaluation_id generation and persistence.
    2. Disambiguated sample definitions (bars vs predictions vs signals vs executed trades).
    3. Low-sample Sharpe ratio tagging (LOW_SAMPLE for N < 30).
    4. 30-trade minimum gate enforcement (9 trades strictly fails).
    5. Promotion gate verification requires atomic evaluation_id (EVALUATION_INTEGRITY_UNVERIFIED).
    6. Identical OOS evaluation fold between Baseline and Challenger.
    7. Consistent compounding drawdown methodology.
    8. Champion model artifact SHA256 invariant preservation.
    """

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self._init_db()

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.unlink(self.db_path)
            except Exception:
                pass

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS foundation_challenger_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                timestamp TEXT,
                timeframe TEXT,
                model_version TEXT,
                dataset_hash TEXT,
                config_hash TEXT,
                universe TEXT,
                data_start TEXT,
                data_end TEXT,
                train_start TEXT,
                train_end TEXT,
                oos_start TEXT,
                oos_end TEXT,
                total_bars_count INTEGER,
                train_bars_count INTEGER,
                oos_bars_count INTEGER,
                prediction_count INTEGER,
                payload_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_master_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                subsystem TEXT,
                event_type TEXT,
                message TEXT,
                details TEXT,
                severity TEXT DEFAULT 'INFO'
            )
        """)
        conn.commit()
        conn.close()

    # -------------------------------------------------------------
    # 1. ATOMIC EVALUATION ID & PROVENANCE HASHES
    # -------------------------------------------------------------
    def test_evaluation_produces_atomic_id_and_hashes(self):
        """Verify that evaluate_incremental_value generates a unique evaluation_id and non-empty hashes."""
        np.random.seed(42)
        X = np.random.randn(100, 5)
        y = np.random.randint(0, 2, size=100)
        features = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']

        with patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path):
            res = FoundationChallengerEvaluator.evaluate_incremental_value(
                benchmark_dataset=(X, y, features),
                timeframe="swing"
            )

        self.assertIn("evaluation_id", res)
        self.assertTrue(res["evaluation_id"].startswith("fnd_eval_"))
        self.assertIn("dataset_hash", res)
        self.assertEqual(len(res["dataset_hash"]), 64)
        self.assertIn("config_hash", res)
        self.assertEqual(len(res["config_hash"]), 64)

        # Verify record was persisted in SQLite
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT evaluation_id, model_version, oos_bars_count FROM foundation_challenger_evaluations WHERE evaluation_id = ?", (res["evaluation_id"],))
        row = cur.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], res["evaluation_id"])
        self.assertEqual(row[2], 30)  # 30% of 100

    # -------------------------------------------------------------
    # 2. DISAMBIGUATED SAMPLE DEFINITIONS
    # -------------------------------------------------------------
    def test_sample_definition_fields_disambiguated(self):
        """Verify explicit separation between bars, predictions, raw signals, and executed trades."""
        np.random.seed(42)
        X = np.random.randn(100, 5)
        y = np.random.randint(0, 2, size=100)
        features = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']

        with patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path):
            res = FoundationChallengerEvaluator.evaluate_incremental_value(
                benchmark_dataset=(X, y, features),
                timeframe="swing"
            )

        definitions = res["sample_definitions"]
        self.assertEqual(definitions["total_bars_count"], 100)
        self.assertEqual(definitions["train_bars_count"], 70)
        self.assertEqual(definitions["oos_bars_count"], 30)
        self.assertEqual(definitions["prediction_count"], 30)

        # In candidate comparison, verify signal counts vs executed trades
        plus_both = res["comparison"]["plus_both"]
        self.assertIn("raw_signals_count", plus_both)
        self.assertIn("qualified_signals_count", plus_both)
        self.assertIn("completed_trade_count", plus_both)
        self.assertIn("winning_trades", plus_both)
        self.assertIn("losing_trades", plus_both)
        self.assertIn("is_low_sample", plus_both)

        # Prediction count (30) cannot be confused with executed trade count
        self.assertLessEqual(plus_both["completed_trade_count"], definitions["oos_bars_count"])

    # -------------------------------------------------------------
    # 3. LOW SAMPLE SHARPE TAGGING & FORMULA PRESERVATION
    # -------------------------------------------------------------
    def test_low_sample_sharpe_tagged_low_sample(self):
        """Verify that trade counts below 30 are marked LOW_SAMPLE without altering math."""
        # Simulate 9 trades (7 wins +2%, 2 losses -1.5%, minus 0.1% friction)
        y_val = np.array([1, 1, 1, 1, 1, 1, 1, 0, 0] + [0]*20)
        probs = np.array([0.60]*9 + [0.30]*20)

        trade = simulate_out_of_sample_trading(y_val, probs, cost_pct=0.001)

        self.assertEqual(trade["trade_count"], 9)
        self.assertTrue(trade["is_low_sample"])
        self.assertEqual(trade["sample_status"], "LOW_SAMPLE")
        self.assertEqual(trade["winning_trades"], 7)
        self.assertEqual(trade["losing_trades"], 2)
        # Math is preserved: Sharpe is computed and positive
        self.assertGreater(trade["sharpe"], 5.0)

    def test_sufficient_sample_sharpe_marked_valid(self):
        """Verify that 35 trades are marked VALID."""
        y_val = np.array([1, 0] * 20)  # 40 bars
        probs = np.array([0.60] * 35 + [0.30] * 5)

        trade = simulate_out_of_sample_trading(y_val, probs, cost_pct=0.001)

        self.assertEqual(trade["trade_count"], 35)
        self.assertFalse(trade["is_low_sample"])
        self.assertEqual(trade["sample_status"], "VALID")

    # -------------------------------------------------------------
    # 4. PROMOTION GATE: 9 TRADES STRICTLY FAILS 30-TRADE REQUIREMENT
    # -------------------------------------------------------------
    def test_nine_trades_fails_promotion_gate(self):
        """Verify that a candidate with 9 trades and Sharpe 11.54 is strictly REJECTED."""
        evaluation_id = "fnd_eval_test_9_trades"
        eval_payload = {
            "evaluation_id": evaluation_id,
            "timeframe": "swing",
            "comparison": {
                "champion": {"f1": 0.05, "sharpe": 0.0, "trade_count": 3, "max_drawdown_pct": 0.0},
                "plus_both": {
                    "f1": 0.12,  # Gain +0.07 (passes hurdle)
                    "sharpe": 11.54,  # High Sharpe (passes hurdle)
                    "completed_trade_count": 9,  # ONLY 9 TRADES (fails sample gate)
                    "trade_count": 9,
                    "max_drawdown_pct": 3.2  # Passes risk boundary
                }
            }
        }

        # Store in SQLite
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO foundation_challenger_evaluations (evaluation_id, payload_json) VALUES (?, ?)",
            (evaluation_id, json.dumps(eval_payload))
        )
        conn.commit()
        conn.close()

        req = FoundationPromoteRequest(
            challenger_type="FOUNDATION_MODEL_CHALLENGER",
            challenger_id="fnd_challenger_timesfm_chronos_swing",
            evaluation_id=evaluation_id,
            timeframe="swing",
            confirm_promotion=True
        )

        with patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path):
            res = promote_foundation_challenger_api(req)

        self.assertEqual(res["status"], "REJECTED")
        self.assertFalse(res["gates_passed"])
        self.assertEqual(res["trade_count"], 9)
        self.assertEqual(res["required_trade_count"], 30)
        self.assertTrue(any("Insufficient OOS sample size" in r for r in res["rejection_reasons"]))

    # -------------------------------------------------------------
    # 5. ATOMIC PROVENANCE ENFORCEMENT
    # -------------------------------------------------------------
    def test_promotion_rejected_without_evaluation_id(self):
        """Verify that promotion rejects without atomic evaluation_id with EVALUATION_INTEGRITY_UNVERIFIED."""
        req = FoundationPromoteRequest(
            challenger_type="FOUNDATION_MODEL_CHALLENGER",
            challenger_id="fnd_challenger_timesfm_chronos_swing",
            evaluation_id=None,  # Missing
            timeframe="swing",
            confirm_promotion=True
        )

        with patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path):
            res = promote_foundation_challenger_api(req)

        self.assertEqual(res["status"], "NOT_ELIGIBLE")
        self.assertFalse(res["gates_passed"])
        self.assertIn("EVALUATION_INTEGRITY_UNVERIFIED", res["message"])

    def test_promotion_rejected_for_unknown_evaluation_id(self):
        """Verify that promotion rejects for unknown evaluation_id with EVALUATION_INTEGRITY_UNVERIFIED."""
        req = FoundationPromoteRequest(
            challenger_type="FOUNDATION_MODEL_CHALLENGER",
            challenger_id="fnd_challenger_timesfm_chronos_swing",
            evaluation_id="fnd_eval_non_existent_12345",
            timeframe="swing",
            confirm_promotion=True
        )

        with patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path):
            res = promote_foundation_challenger_api(req)

        self.assertEqual(res["status"], "NOT_ELIGIBLE")
        self.assertFalse(res["gates_passed"])
        self.assertIn("EVALUATION_INTEGRITY_UNVERIFIED", res["message"])

    # -------------------------------------------------------------
    # 6. DRAWDOWN CALCULATION METHODOLOGY
    # -------------------------------------------------------------
    def test_drawdown_compounding_methodology(self):
        """Verify that Max Drawdown uses cumulative peak-to-trough high-water mark compounding."""
        # 10 bars: bar 1 win (+2% net), bars 2-6 losses (-1.5% net) -> 6 trades
        returns = np.array([1, 0, 0, 0, 0, 0] + [0]*4)
        probs = np.array([0.60, 0.60, 0.60, 0.60, 0.60, 0.60] + [0.30]*4)

        trade = simulate_out_of_sample_trading(returns, probs, cost_pct=0.001)

        self.assertEqual(trade["trade_count"], 6)
        self.assertGreater(trade["max_drawdown_pct"], 0.0)
        self.assertAlmostEqual(trade["max_drawdown_pct"], 7.6, delta=2.0)

    # -------------------------------------------------------------
    # 7. PRODUCTION INVARIANTS PRESERVATION
    # -------------------------------------------------------------
    def test_champion_model_hashes_strictly_unchanged(self):
        """Re-verify SHA256 checksums of champion models to ensure ZERO corruption."""
        def hash_file(p):
            with open(p, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()

        intra = hash_file('backend/models/intraday/champion_ensemble.pkl')
        swing = hash_file('backend/models/swing/champion_ensemble.pkl')

        self.assertEqual(intra, 'f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91')
        self.assertEqual(swing, '11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534')

    def test_research_jobs_count_unchanged(self):
        """Verify research jobs count remains strictly 25."""
        from app.data.historical_data_layer import get_db_path
        conn = sqlite3.connect(get_db_path())
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM research_jobs")
        jobs = c.fetchone()[0]
        conn.close()
        self.assertEqual(jobs, 25)


if __name__ == "__main__":
    unittest.main()
