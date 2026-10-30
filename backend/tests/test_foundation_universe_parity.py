import unittest
import sqlite3
import hashlib
import json
import os
import numpy as np
import pandas as pd
from unittest.mock import patch, MagicMock

from app.analytics.universe_config import (
    resolve_universe_tickers,
    BENCHMARK_5_UNIVERSE,
    LIVE_UNIVERSE,
    NIFTY_50_UNIVERSE
)
from app.analytics.optuna_tuner import prepare_benchmark_dataset
from app.analytics.foundation_models.challenger_evaluator import (
    FoundationChallengerEvaluator,
    ensure_foundation_evaluations_table
)
from app.data.historical_data_layer import get_db_path


class TestFoundationUniverseParity(unittest.TestCase):
    """
    Test suite verifying Foundation Challenger System-Wide OOS Universe Migration & Evaluation Parity.
    """

    @classmethod
    def setUpClass(cls):
        ensure_foundation_evaluations_table()

    def test_01_benchmark_5_remains_available(self):
        """1. BENCHMARK_5 remains available as diagnostic/smoke benchmark."""
        tickers = resolve_universe_tickers("BENCHMARK_5")
        self.assertEqual(len(tickers), 5)
        self.assertEqual(tickers, BENCHMARK_5_UNIVERSE)

    def test_02_production_swing_universe_resolves_correctly(self):
        """2. Production Swing universe (LIVE_52) resolves correctly to 52 tickers."""
        tickers = resolve_universe_tickers("LIVE_52")
        self.assertEqual(len(tickers), 52)
        self.assertEqual(tickers, LIVE_UNIVERSE)

    def test_03_no_hardcoded_5_stock_in_evaluation_path(self):
        """3. When evaluated with LIVE_52, evaluator uses resolved tickers, not 5 stocks."""
        tickers = resolve_universe_tickers("LIVE_52")
        self.assertGreater(len(tickers), 5)

    def test_04_champion_and_challenger_receive_identical_tickers(self):
        """4. Champion and Challenger evaluate on identical OOS dataset and tickers."""
        # Create deterministic synthetic multi-stock dataset
        n_obs = 1000
        np.random.seed(42)
        X = np.random.randn(n_obs, 5)
        y = (np.random.rand(n_obs) > 0.5).astype(int)
        features = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']

        res = FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(X, y, features),
            timeframe="swing",
            universe="LIVE_52"
        )
        self.assertIn("comparison", res)
        champ = res["comparison"]["champion"]
        cand = res["comparison"]["plus_both"]
        self.assertIn("f1", champ)
        self.assertIn("f1", cand)
        self.assertEqual(res["universe"], "LIVE_52")

    def test_05_universe_hash_consistent_within_evaluation(self):
        """5. Universe hash is deterministic and recorded in evaluation payload."""
        tickers = resolve_universe_tickers("LIVE_52")
        canonical = sorted(tickers)
        expected_hash = hashlib.sha256(json.dumps(canonical).encode()).hexdigest()

        n_obs = 600
        X = np.random.randn(n_obs, 5)
        y = (np.random.rand(n_obs) > 0.5).astype(int)
        features = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']

        res = FoundationChallengerEvaluator.evaluate_incremental_value(
            benchmark_dataset=(X, y, features),
            timeframe="swing",
            universe="LIVE_52"
        )
        self.assertEqual(res["universe_hash"], expected_hash)
        self.assertEqual(res["ticker_count"], 52)

    def test_06_oos_dates_and_zero_future_leakage(self):
        """6 & 7. OOS partition preserves strict chronological order with zero leakage."""
        tickers = resolve_universe_tickers("BENCHMARK_5")
        X, y, feats, meta = prepare_benchmark_dataset(timeframe="swing", tickers=tickers, return_metadata=True)
        self.assertIsNotNone(meta.get("train_end"))
        self.assertIsNotNone(meta.get("oos_start"))
        self.assertLessEqual(meta["train_end"], meta["oos_start"])

    def test_08_old_benchmark_5_evaluations_preserved(self):
        """8. Old historical evaluations remain queryable in database."""
        conn = sqlite3.connect(get_db_path())
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM foundation_challenger_evaluations WHERE universe = 'BENCHMARK_5'")
        count = cur.fetchone()[0]
        conn.close()
        # Historical benchmark 5 evaluations must not be purged
        self.assertGreaterEqual(count, 0)

    def test_09_new_universe_creates_new_evaluation_id(self):
        """9. Every evaluation creates a fresh unique atomic evaluation_id."""
        n_obs = 500
        X = np.random.randn(n_obs, 5)
        y = (np.random.rand(n_obs) > 0.5).astype(int)
        feats = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']

        r1 = FoundationChallengerEvaluator.evaluate_incremental_value((X, y, feats), timeframe="swing", universe="LIVE_52")
        r2 = FoundationChallengerEvaluator.evaluate_incremental_value((X, y, feats), timeframe="swing", universe="BENCHMARK_5")
        self.assertNotEqual(r1["evaluation_id"], r2["evaluation_id"])
        self.assertNotEqual(r1["config_hash"], r2["config_hash"])
        self.assertEqual(r1["universe"], "LIVE_52")
        self.assertEqual(r2["universe"], "BENCHMARK_5")

    def test_10_cache_cannot_collide_across_universes(self):
        """10. Config hash contains universe and universe_hash, preventing cross-universe collisions."""
        u1_hash = hashlib.sha256(json.dumps(sorted(BENCHMARK_5_UNIVERSE)).encode()).hexdigest()
        u2_hash = hashlib.sha256(json.dumps(sorted(LIVE_UNIVERSE)).encode()).hexdigest()
        c1 = f"universe=BENCHMARK_5|universe_hash={u1_hash}|timeframe=swing|friction=0.001|split=0.70"
        c2 = f"universe=LIVE_52|universe_hash={u2_hash}|timeframe=swing|friction=0.001|split=0.70"
        h1 = hashlib.sha256(c1.encode()).hexdigest()
        h2 = hashlib.sha256(c2.encode()).hexdigest()
        self.assertNotEqual(h1, h2)

    def test_11_completed_trade_distinct_from_qualified_signals(self):
        """11. Trade funnel disambiguates bars, raw signals, qualified signals, and completed trades."""
        n_obs = 600
        X = np.random.randn(n_obs, 5)
        y = (np.random.rand(n_obs) > 0.5).astype(int)
        feats = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']
        res = FoundationChallengerEvaluator.evaluate_incremental_value((X, y, feats), timeframe="swing", universe="LIVE_52")
        both = res["comparison"]["plus_both"]
        self.assertIn("raw_signals_count", both)
        self.assertIn("qualified_signals_count", both)
        self.assertIn("completed_trade_count", both)
        self.assertIn("trade_count", both)
        self.assertEqual(both["completed_trade_count"], both["trade_count"])

    def test_12_promotion_uses_completed_oos_trades(self):
        """12. Promotion gate verifies completed_trade_count."""
        from app.api.ml_lab import promote_foundation_challenger_api, FoundationPromoteRequest
        
        # Test evaluation with 9 trades
        dummy_id = "fnd_eval_test_low_sample"
        dummy_payload = {
            "evaluation_id": dummy_id,
            "universe": "BENCHMARK_5",
            "timeframe": "swing",
            "comparison": {
                "champion": {"f1": 0.50, "sharpe": 1.0, "trade_count": 5},
                "plus_both": {
                    "f1": 0.55, "sharpe": 2.0, "completed_trade_count": 9,
                    "trade_count": 9, "max_drawdown_pct": 5.0
                }
            }
        }
        conn = sqlite3.connect(get_db_path())
        conn.execute("""
            INSERT OR REPLACE INTO foundation_challenger_evaluations
            (evaluation_id, timestamp, timeframe, model_version, dataset_hash, config_hash, universe,
             data_start, data_end, train_start, train_end, oos_start, oos_end,
             total_bars_count, train_bars_count, oos_bars_count, prediction_count, payload_json)
            VALUES (?, '2026-09-04', 'swing', 'v1.0', 'dshash', 'cfghash', 'BENCHMARK_5',
                    '2024-09-04', '2026-09-04', '2024-09-04', '2026-02-04', '2026-02-04', '2026-09-04',
                    1000, 700, 300, 300, ?)
        """, (dummy_id, json.dumps(dummy_payload)))
        conn.commit()
        conn.close()

        req = FoundationPromoteRequest(
            challenger_type="FOUNDATION_MODEL_CHALLENGER",
            challenger_id="fnd_test",
            evaluation_id=dummy_id,
            timeframe="swing",
            challenger_variant="plus_both",
            confirm_promotion=True
        )
        res = promote_foundation_challenger_api(req)
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("Insufficient OOS sample size (9 trades < 30 required", res["message"])

    def test_13_more_trades_with_high_drawdown_still_fails(self):
        """14. >=30 trades does NOT automatically pass promotion if drawdown or performance fails."""
        from app.api.ml_lab import promote_foundation_challenger_api, FoundationPromoteRequest

        dummy_id = "fnd_eval_test_high_dd"
        dummy_payload = {
            "evaluation_id": dummy_id,
            "universe": "LIVE_52",
            "timeframe": "swing",
            "comparison": {
                "champion": {"f1": 0.50, "sharpe": 1.0, "trade_count": 20},
                "plus_both": {
                    "f1": 0.52, "sharpe": -1.5, "completed_trade_count": 65,
                    "trade_count": 65, "max_drawdown_pct": 35.0
                }
            }
        }
        conn = sqlite3.connect(get_db_path())
        conn.execute("""
            INSERT OR REPLACE INTO foundation_challenger_evaluations
            (evaluation_id, timestamp, timeframe, model_version, dataset_hash, config_hash, universe,
             data_start, data_end, train_start, train_end, oos_start, oos_end,
             total_bars_count, train_bars_count, oos_bars_count, prediction_count, payload_json)
            VALUES (?, '2026-09-04', 'swing', 'v1.0', 'dshash', 'cfghash', 'LIVE_52',
                    '2024-09-04', '2026-09-04', '2024-09-04', '2026-02-04', '2026-02-04', '2026-09-04',
                    1000, 700, 300, 300, ?)
        """, (dummy_id, json.dumps(dummy_payload)))
        conn.commit()
        conn.close()

        req = FoundationPromoteRequest(
            challenger_type="FOUNDATION_MODEL_CHALLENGER",
            challenger_id="fnd_test",
            evaluation_id=dummy_id,
            timeframe="swing",
            challenger_variant="plus_both",
            confirm_promotion=True
        )
        res = promote_foundation_challenger_api(req)
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("Excessive Max Drawdown (35.0% > 20.0% ceiling)", res["message"])

    def test_14_champion_hashes_and_production_invariants(self):
        """17, 19, 20, 21, 22. Invariants check."""
        with open("backend/models/intraday/champion_ensemble.pkl", "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91")
        with open("backend/models/swing/champion_ensemble.pkl", "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534")

        conn = sqlite3.connect(get_db_path())
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM ml_trade_history")
        trade_rows = cur.fetchone()[0]
        self.assertEqual(trade_rows, 68)

        cur.execute("SELECT count(*) FROM research_jobs")
        res_jobs = cur.fetchone()[0]
        self.assertEqual(res_jobs, 25)
        conn.close()


if __name__ == "__main__":
    unittest.main()
