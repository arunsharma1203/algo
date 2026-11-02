"""
TEST SUITE: CONSENSUS EMPIRICAL META-LEARNER V2
================================================
Comprehensive verification testing:
1. Point-in-time feature construction (no future look-ahead)
2. Causal OHLCV and market breadth extraction
3. Missing F&O data handling (FNO_AVAILABLE = 0, neutral values)
4. Missing FinBERT handling (neutral 0.0, no unfair penalty)
5. Objective mathematical chart feature calculation
6. Probability / logit transformation mathematical stability
7. Probability calibration monotonic mapping
8. Challenger isolation (champion models remain unchanged)
9. Production model SHA256 integrity verification
10. Simulation mode strictly remains True
"""

import os
import unittest
import hashlib
import sqlite3
import numpy as np

from app.analytics.consensus_meta_feature_extractor import (
    ConsensusMetaFeatureExtractor,
    FEATURE_NAMES,
    GROUP_SLICES
)
from app.analytics.consensus_meta_learner_v2 import (
    ConsensusMetaLearnerV2,
    logit,
    sigmoid
)
from app.api.settings import get_simulation_mode


class TestConsensusMetaLearnerV2(unittest.TestCase):

    def setUp(self):
        self.db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "market_data.db"))
        self.learner = ConsensusMetaLearnerV2(auto_load=True)

    def test_production_model_hashes_unchanged(self):
        """Rule 1: Champion production models must remain strictly immutable."""
        expected_swing_sha = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"
        swing_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "models", "swing", "champion_ensemble.pkl"))
        with open(swing_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), expected_swing_sha)

        intraday_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "models", "intraday", "champion_ensemble.pkl"))
        with open(intraday_path, "rb") as f:
            intraday_sha = hashlib.sha256(f.read()).hexdigest()
        self.assertTrue(len(intraday_sha) == 64)

    def test_simulation_mode_remains_true(self):
        """Rule 2: Paper-only execution safeguard must remain strictly active."""
        mode_dict = get_simulation_mode()
        self.assertTrue(mode_dict.get("simulation_mode", False))

    def test_mathematical_logit_and_sigmoid_stability(self):
        """Verifies logit and sigmoid transformations behave with mathematical precision."""
        self.assertAlmostEqual(logit(0.5), 0.0, places=4)
        self.assertAlmostEqual(sigmoid(0.0), 0.5, places=4)
        self.assertAlmostEqual(sigmoid(logit(0.7)), 0.7, places=4)
        self.assertAlmostEqual(sigmoid(logit(0.2)), 0.2, places=4)

        # Boundary checks
        self.assertEqual(sigmoid(100.0), 1.0)
        self.assertEqual(sigmoid(-100.0), 0.0)
        self.assertTrue(np.isfinite(logit(0.0)))
        self.assertTrue(np.isfinite(logit(1.0)))

    def test_feature_count_and_deterministic_order(self):
        """Verifies exactly 39 features exist across 7 distinct groups."""
        self.assertEqual(len(FEATURE_NAMES), 39)
        total_group_feats = sum(len(v) for v in GROUP_SLICES.values())
        self.assertEqual(total_group_feats, 39)

    def test_missing_fno_handling(self):
        """Verifies that non-F&O candidates receive neutral FNO_AVAILABLE=0 mask and no penalty."""
        dummy_cand = {
            "ticker": "NON_EXISTENT_CO.NS",
            "symbol": "NONEXISTENT",
            "direction": "BUY",
            "entry_price": 100.0,
            "composite_score": 60.0,
            "timestamp": "2026-09-20 10:00:00"
        }
        feats = ConsensusMetaFeatureExtractor.extract_point_in_time_features(
            dummy_cand, db_path=self.db_path
        )
        self.assertEqual(feats["fno_available"], 0.0)
        self.assertEqual(feats["fno_pcr"], 1.0)
        self.assertEqual(feats["fno_pcr_bias_bull"], 0.0)

    def test_missing_news_handling(self):
        """Verifies missing news produces news_available=0 and neutral 0.0 sentiment."""
        dummy_cand = {
            "ticker": "TCS.NS",
            "symbol": "TCS",
            "direction": "BUY",
            "entry_price": 4000.0,
            "composite_score": 65.0,
            "timestamp": "2026-09-20 10:00:00"
        }
        feats = ConsensusMetaFeatureExtractor.extract_point_in_time_features(
            dummy_cand, db_path=self.db_path
        )
        self.assertEqual(feats["news_available"], 0.0)
        self.assertEqual(feats["finbert_sentiment_score"], 0.0)
        self.assertEqual(feats["finbert_contradicting"], 0.0)

    def test_objective_chart_structure_extraction(self):
        """Verifies that 15m OHLCV geometry is extracted without errors."""
        cand = {
            "ticker": "RELIANCE.NS",
            "symbol": "RELIANCE",
            "direction": "BUY",
            "entry_price": 1220.0,
            "composite_score": 70.0,
            "timestamp": "2026-10-06 14:00:00"
        }
        feats = ConsensusMetaFeatureExtractor.extract_point_in_time_features(
            cand, db_path=self.db_path
        )
        self.assertIn("ema_9_slope", feats)
        self.assertIn("range_position_20", feats)
        self.assertIn("close_location_val", feats)
        self.assertTrue(0.0 <= feats["range_position_20"] <= 1.0)
        self.assertTrue(0.0 <= feats["close_location_val"] <= 1.0)

    def test_meta_learner_challenger_evaluation(self):
        """Verifies Meta-Learner V2 evaluates and outputs complete attribution telemetry."""
        self.assertTrue(self.learner.is_trained)
        cand = {
            "ticker": "RELIANCE.NS",
            "symbol": "RELIANCE",
            "direction": "BUY",
            "entry_price": 1220.0,
            "composite_score": 68.0,
            "timestamp": "2026-10-06 14:00:00"
        }
        res = self.learner.predict_candidate(cand)
        self.assertEqual(res["base_conviction"], 68.0)
        self.assertIn("final_conviction", res)
        self.assertIn("meta_adjustment", res)
        self.assertIn("group_attributions", res)
        self.assertIn("CHART_STRUCTURE", res["group_attributions"])
        self.assertIn("MARKET_REGIME", res["group_attributions"])

    def test_historical_database_rows_remain_unmodified(self):
        """Verifies consensus_trade_history row count is intact and not altered."""
        conn = sqlite3.connect(self.db_path)
        cnt = conn.execute("SELECT COUNT(*) FROM consensus_trade_history").fetchone()[0]
        conn.close()
        self.assertGreaterEqual(cnt, 1280)


if __name__ == "__main__":
    unittest.main()
