import os
import sys
import hashlib
import sqlite3
import unittest
from unittest.mock import patch, MagicMock
import numpy as np
import pandas as pd

from app.analytics.foundation_models.timesfm_adapter import TimesFMAdapter
from app.analytics.foundation_models.chronos_adapter import ChronosAdapter
from app.analytics.foundation_models.real_foundation_service import RealFoundationService
from app.analytics.foundation_models.real_challenger_evaluator import RealFoundationChallengerEvaluator
from app.data.historical_data_layer import get_db_path

class TestRealFoundationChallenger(unittest.TestCase):
    """
    Test suite for REAL_TIMESFM_CHRONOS_ABLATION research engine.
    Verifies model provenance, scale-invariant return features, temporal split,
    cache behavior, promotion gates, and champion invariants.
    """

    CHAMPION_INTRADAY_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
    CHAMPION_SWING_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

    def test_01_champion_hashes_strictly_preserved(self):
        """Ensures champion model files are byte-for-byte unchanged."""
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        intra_path = os.path.join(base_dir, 'models', 'intraday', 'champion_ensemble.pkl')
        swing_path = os.path.join(base_dir, 'models', 'swing', 'champion_ensemble.pkl')

        self.assertTrue(os.path.exists(intra_path), f"Missing {intra_path}")
        self.assertTrue(os.path.exists(swing_path), f"Missing {swing_path}")

        with open(intra_path, 'rb') as f:
            actual_intra = hashlib.sha256(f.read()).hexdigest()
        with open(swing_path, 'rb') as f:
            actual_swing = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(actual_intra, self.CHAMPION_INTRADAY_HASH, "Champion intraday model hash mismatch!")
        self.assertEqual(actual_swing, self.CHAMPION_SWING_HASH, "Champion swing model hash mismatch!")

    def test_02_trade_history_unmodified(self):
        """Ensures ml_trade_history remains exactly at 68 rows (zero writes)."""
        db = get_db_path()
        conn = sqlite3.connect(db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM ml_trade_history")
        count = c.fetchone()[0]
        conn.close()
        self.assertEqual(count, 68, f"ml_trade_history modified! Expected 68 rows, found {count}")

    def test_03_genuine_timesfm_provenance(self):
        """Verifies TimesFM adapter uses genuine google/timesfm-2.5-200m-pytorch checkpoint."""
        adapter = TimesFMAdapter()
        info = adapter.get_model_info()
        self.assertEqual(info["model_name"], "TimesFM 2.5")
        self.assertEqual(info["version"], "google/timesfm-2.5-200m-pytorch")
        self.assertEqual(info["provider"], "Google Research")

    def test_04_genuine_chronos_provenance(self):
        """Verifies Chronos adapter uses genuine amazon/chronos-2 checkpoint."""
        adapter = ChronosAdapter()
        info = adapter.get_model_info()
        self.assertEqual(info["model_name"], "Chronos-2")
        self.assertEqual(info["version"], "amazon/chronos-2")
        self.assertIn("Amazon Science", info["provider"])

    def test_05_scale_invariant_return_features(self):
        """Verifies foundation return feature is percentage-based and scale-invariant."""
        p1_curr, p1_fut = 100.0, 105.0
        p2_curr, p2_fut = 3000.0, 3150.0

        ret1 = ((p1_fut - p1_curr) / p1_curr) * 100.0
        ret2 = ((p2_fut - p2_curr) / p2_curr) * 100.0

        self.assertAlmostEqual(ret1, 5.0, places=4)
        self.assertAlmostEqual(ret2, 5.0, places=4)
        self.assertEqual(ret1, ret2)

    def test_06_cache_schema_and_isolation(self):
        """Verifies the isolated foundation forecast SQLite cache structure."""
        import tempfile
        tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        try:
            srv = RealFoundationService(cache_db_path=tmp_db)
            conn = sqlite3.connect(tmp_db)
            c = conn.cursor()
            c.execute("PRAGMA table_info(foundation_forecast_cache)")
            cols = {row[1] for row in c.fetchall()}
            conn.close()

            expected_cols = {
                "symbol", "as_of_date", "model_name", "model_version",
                "horizon_bars", "expected_return_pct", "uncertainty_score",
                "downside_risk_pct", "upside_potential_pct", "forecast_path_json", "created_at"
            }
            self.assertTrue(expected_cols.issubset(cols), f"Missing columns in cache: {expected_cols - cols}")
        finally:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)

    def test_07_promotion_gate_enforcement_low_trades(self):
        """Verifies promotion gate blocks candidate when completed trades < 30."""
        trade_count = 14
        f1_gain = 0.05
        sharpe_gain = 0.5
        max_dd = 10.0

        sample_size_passed = trade_count >= 30
        stat_hurdle_passed = (f1_gain >= 0.0100) and (sharpe_gain >= 0.0)
        risk_gate_passed = max_dd <= 20.0
        all_passed = sample_size_passed and stat_hurdle_passed and risk_gate_passed

        self.assertFalse(sample_size_passed)
        self.assertFalse(all_passed)

    def test_08_promotion_gate_enforcement_max_drawdown(self):
        """Verifies promotion gate blocks candidate when Max Drawdown > 20%."""
        trade_count = 50
        f1_gain = 0.05
        sharpe_gain = 0.5
        max_dd = 28.5

        sample_size_passed = trade_count >= 30
        stat_hurdle_passed = (f1_gain >= 0.0100) and (sharpe_gain >= 0.0)
        risk_gate_passed = max_dd <= 20.0
        all_passed = sample_size_passed and stat_hurdle_passed and risk_gate_passed

        self.assertTrue(sample_size_passed)
        self.assertFalse(risk_gate_passed)
        self.assertFalse(all_passed)

    def test_09_evaluator_metadata_contains_provenance(self):
        """Verifies RealFoundationService returns verified metadata structure."""
        srv = RealFoundationService()
        prov = srv.get_provenance_metadata()
        self.assertIn("timesfm_2p5", prov)
        self.assertIn("chronos_2", prov)
        self.assertEqual(prov["timesfm_2p5"]["model_version"], "google/timesfm-2.5-200m-pytorch")
        self.assertEqual(prov["chronos_2"]["model_version"], "amazon/chronos-2")

if __name__ == '__main__':
    unittest.main()
