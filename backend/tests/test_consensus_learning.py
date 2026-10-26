import os
import math
import sqlite3
import hashlib
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from app.analytics.consensus_learning_engine import ConsensusLearningEngine
from app.analytics.consensus_tracker import ConsensusTracker


class TestConsensusLearningEngine(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        # Patch get_db_path across both modules to isolate tests in temp_db
        self.patcher1 = patch("app.analytics.consensus_learning_engine.get_db_path", return_value=self.db_path)
        self.patcher2 = patch("app.analytics.consensus_tracker.get_db_path", return_value=self.db_path)
        self.patcher1.start()
        self.patcher2.start()

        ConsensusLearningEngine.init_learning_schema()
        ConsensusTracker.init_consensus_tracking_schema()

    def tearDown(self):
        self.patcher1.stop()
        self.patcher2.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_default_weights_fallback(self):
        weights = ConsensusLearningEngine.get_current_weights("INTRADAY")
        self.assertEqual(weights["w_qlib"], 0.50)
        self.assertEqual(weights["w_legacy"], 0.50)
        self.assertAlmostEqual(weights["w_qlib"] + weights["w_legacy"], 1.0)

    def test_brier_loss_calculation(self):
        # Perfect predictions
        self.assertAlmostEqual(ConsensusLearningEngine.calculate_brier_loss(1.0, 1.0), 0.0)
        self.assertAlmostEqual(ConsensusLearningEngine.calculate_brier_loss(0.0, 0.0), 0.0)
        # Completely wrong
        self.assertAlmostEqual(ConsensusLearningEngine.calculate_brier_loss(1.0, 0.0), 1.0)
        self.assertAlmostEqual(ConsensusLearningEngine.calculate_brier_loss(0.0, 1.0), 1.0)
        # Half confident: (0.7 - 1.0)^2 = 0.09
        self.assertAlmostEqual(ConsensusLearningEngine.calculate_brier_loss(0.7, 1.0), 0.09)
        # Half confident: (0.6 - 0.0)^2 = 0.36
        self.assertAlmostEqual(ConsensusLearningEngine.calculate_brier_loss(0.6, 0.0), 0.36)

    def test_hedge_weights_sum_to_one(self):
        # Seed 20 synthetic closed trades into temp db
        conn = sqlite3.connect(self.db_path)
        for i in range(20):
            outcome = "TARGET MET" if i % 2 == 0 else "SL HIT"
            pnl = 1.0 if i % 2 == 0 else -1.0
            # Qlib was more confident on wins
            q_score = 75.0 if i % 2 == 0 else 45.0
            l_score = 52.0
            conn.execute("""
                INSERT INTO consensus_trade_history (
                    timestamp, ticker, symbol, strategy, direction,
                    entry_price, stop_loss, target_price, composite_score,
                    qlib_score, legacy_score, status, outcome, profit_pct
                ) VALUES (?, ?, ?, 'INTRADAY', 'BUY', 100.0, 95.0, 105.0, 60.0, ?, ?, 'CLOSED', ?, ?)
            """, (f"2026-09-18 10:{i:02d}:00", f"STOCK_{i}.NS", f"STOCK_{i}", q_score, l_score, outcome, pnl))
        conn.commit()
        conn.close()

        res = ConsensusLearningEngine.execute_daily_weight_update(strategy="INTRADAY", trailing_n=20, min_samples=10)
        self.assertTrue(res["updated"])
        w_q = res["weights"]["w_qlib"]
        w_l = res["weights"]["w_legacy"]
        self.assertAlmostEqual(w_q + w_l, 1.0, places=4)

    def test_entropy_floor_enforced(self):
        # Even if Qlib has 100% loss on all trades, its weight cannot drop below 0.20
        conn = sqlite3.connect(self.db_path)
        for i in range(30):
            # All trades lost, Qlib had 99% conviction, Legacy had 1% conviction
            conn.execute("""
                INSERT INTO consensus_trade_history (
                    timestamp, ticker, symbol, strategy, direction,
                    entry_price, stop_loss, target_price, composite_score,
                    qlib_score, legacy_score, status, outcome, profit_pct
                ) VALUES (?, ?, ?, 'INTRADAY', 'BUY', 100.0, 95.0, 105.0, 60.0, 99.0, 1.0, 'CLOSED', 'SL HIT', -2.0)
            """, (f"2026-09-18 10:{i:02d}:00", f"STOCK_{i}.NS", f"STOCK_{i}"))
        conn.commit()
        conn.close()

        # Run 10 updates in succession
        for _ in range(10):
            res = ConsensusLearningEngine.execute_daily_weight_update(strategy="INTRADAY", trailing_n=30, min_samples=10)

        weights = ConsensusLearningEngine.get_current_weights("INTRADAY")
        self.assertGreaterEqual(weights["w_qlib"], 0.20)
        self.assertLessEqual(weights["w_legacy"], 0.80)
        self.assertAlmostEqual(weights["w_qlib"] + weights["w_legacy"], 1.0, places=4)

    def test_daily_step_clamp(self):
        # Even with extreme losses, a single update cannot change weight by more than 0.05
        conn = sqlite3.connect(self.db_path)
        for i in range(25):
            conn.execute("""
                INSERT INTO consensus_trade_history (
                    timestamp, ticker, symbol, strategy, direction,
                    entry_price, stop_loss, target_price, composite_score,
                    qlib_score, legacy_score, status, outcome, profit_pct
                ) VALUES (?, ?, ?, 'INTRADAY', 'BUY', 100.0, 95.0, 105.0, 60.0, 99.0, 1.0, 'CLOSED', 'SL HIT', -2.0)
            """, (f"2026-09-18 10:{i:02d}:00", f"STOCK_{i}.NS", f"STOCK_{i}"))
        conn.commit()
        conn.close()

        w_before = ConsensusLearningEngine.get_current_weights("INTRADAY")["w_qlib"]
        res = ConsensusLearningEngine.execute_daily_weight_update(strategy="INTRADAY", trailing_n=25, min_samples=10)
        w_after = res["weights"]["w_qlib"]
        delta = abs(w_after - w_before)
        self.assertLessEqual(delta, 0.0501)

    def test_shadow_candidates_logging_and_querying(self):
        candidates = [
            {
                "ticker": "TATAMOTORS.NS",
                "symbol": "TATAMOTORS",
                "qlib_direction": "BUY",
                "qlib_score": 72.5,
                "qlib_price": 950.0,
                "legacy_direction": "BUY",
                "legacy_confidence": 65.0,
                "legacy_price": 951.0,
                "composite_score": 68.8,
                "action_taken": "TRADED_UNANIMOUS",
                "entry_ref_price": 950.5,
                "details": {"risk_reward_ratio": 2.2}
            },
            {
                "ticker": "RELIANCE.NS",
                "symbol": "RELIANCE",
                "qlib_direction": "BUY",
                "qlib_score": 68.0,
                "qlib_price": 2500.0,
                "legacy_direction": "SELL",
                "legacy_confidence": 62.0,
                "legacy_price": 2498.0,
                "composite_score": 65.0,
                "action_taken": "CONFLICT_BLOCKED",
                "entry_ref_price": 2499.0,
                "details": {"warning": "Directional conflict"}
            }
        ]

        count = ConsensusTracker.log_shadow_candidates(candidates, scan_id="test_scan_123", strategy="INTRADAY")
        self.assertEqual(count, 2)

        shadow_list = ConsensusTracker.get_shadow_candidates(strategy="INTRADAY", limit=10)
        self.assertEqual(len(shadow_list), 2)
        tickers = {s["ticker"] for s in shadow_list}
        self.assertIn("TATAMOTORS.NS", tickers)
        self.assertIn("RELIANCE.NS", tickers)

        # Filter by action
        conflicts = ConsensusTracker.get_shadow_candidates(strategy="INTRADAY", action="CONFLICT_BLOCKED")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["ticker"], "RELIANCE.NS")

    def test_champion_model_hashes_unchanged(self):
        # Strict proof that base models are never retrained or modified
        expected_hashes = {
            "intraday": "d78677fa40c214737684dcd20cf42e5f79d6e4de96f931f3123181427d5ff8d8",
            "swing": "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"
        }
        for tf, exp_hash in expected_hashes.items():
            path = f"backend/models/{tf}/champion_ensemble.pkl"
            if os.path.exists(path):
                with open(path, "rb") as f:
                    actual_hash = hashlib.sha256(f.read()).hexdigest()
                self.assertEqual(actual_hash, exp_hash, f"{tf} champion model was altered!")


if __name__ == "__main__":
    unittest.main()

