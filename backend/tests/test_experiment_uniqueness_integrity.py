"""
TEST SUITE: MILESTONE 5 — EXPERIMENT UNIQUENESS & RESEARCH INTEGRITY
===================================================================
Verifies deterministic configuration hashing across all material parameters,
database-level unique index enforcement on (mission_id, config_hash),
lifecycle preservation (no destructive overwrite of created_at),
and unique budget consumption.
"""

import unittest
import tempfile
import sqlite3
import os
import shutil
import json

from app.data.database import set_test_db_override
from app.analytics.research_orchestrator.experiment_generator import ExperimentGenerator
from app.analytics.research_orchestrator.research_memory import ResearchMemory
from app.analytics.research_orchestrator.research_budget import ResearchBudgetManager


class TestExperimentUniquenessIntegrity(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_market_data.db")
        set_test_db_override(self.db_path)
        ResearchMemory.ensure_tables()

    def tearDown(self):
        set_test_db_override(None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_canonicalize_config_order_independence(self):
        """Keys provided in different order or with float precision quirks must produce identical hashes."""
        cfg_a = {
            "model_family": "LightGBM",
            "feature_family": "Alpha158",
            "horizon_days": 10,
            "hyperparameters": {"learning_rate": 0.05, "max_depth": 6},
            "data_boundaries": {"train_start": "2020-01-01", "train_end": "2023-12-31"},
            "cost_tiers": [0.002, 0.001, 0.003],
            "seed": 42
        }
        cfg_b = {
            "seed": 42,
            "cost_tiers": [0.001, 0.002, 0.003],
            "data_boundaries": {"train_end": "2023-12-31", "train_start": "2020-01-01"},
            "hyperparameters": {"max_depth": 6, "learning_rate": 0.0500000},
            "horizon_days": 10,
            "feature_family": "Alpha158",
            "model_family": "LightGBM"
        }
        hash_a = ExperimentGenerator.compute_config_hash(cfg_a)
        hash_b = ExperimentGenerator.compute_config_hash(cfg_b)
        self.assertEqual(hash_a, hash_b, "Config hashes must be identical regardless of key order")

    def test_canonicalize_config_captures_all_material_params(self):
        """Changing any material parameter must produce a distinct config_hash."""
        base_cfg = {
            "model_family": "LightGBM",
            "feature_family": "Alpha158",
            "horizon_days": 10,
            "universe": "LIVE_52",
            "strategy_type": "SWING",
            "timeframe": "1d",
            "portfolio_family": "HYSTERESIS_TOP5_15",
            "entry_top_k": 5,
            "exit_top_k": 15,
            "holding_period": 10,
            "rebalance_frequency": 1,
            "slippage_bps": 5.0,
            "hyperparameters": {"n_estimators": 100},
            "dataset_hash": "dset_abc123",
            "position_sizing": "EQUAL_WEIGHT",
            "secondary_constraints": {"max_drawdown_pct": 20.0},
            "seed": 42,
            "code_version": "v1.0"
        }
        base_hash = ExperimentGenerator.compute_config_hash(base_cfg)

        variations = [
            ("model_family", "DoubleEnsemble"),
            ("feature_family", "Alpha360"),
            ("horizon_days", 15),
            ("universe", "NIFTY50"),
            ("strategy_type", "INTRADAY"),
            ("timeframe", "15m"),
            ("portfolio_family", "TOP10"),
            ("entry_top_k", 10),
            ("exit_top_k", 20),
            ("holding_period", 5),
            ("rebalance_frequency", 5),
            ("slippage_bps", 10.0),
            ("hyperparameters", {"n_estimators": 200}),
            ("dataset_hash", "dset_diff999"),
            ("position_sizing", "VOLATILITY_PARITY"),
            ("secondary_constraints", {"max_drawdown_pct": 15.0}),
            ("seed", 999),
            ("code_version", "v2.0")
        ]

        for param_key, new_val in variations:
            mod_cfg = dict(base_cfg)
            mod_cfg[param_key] = new_val
            mod_hash = ExperimentGenerator.compute_config_hash(mod_cfg)
            self.assertNotEqual(base_hash, mod_hash, f"Modifying {param_key} must produce a different config_hash")

    def test_enqueue_experiment_deduplication(self):
        """Enqueueing duplicate config must return False and not create a duplicate row."""
        exp = {
            "experiment_id": "exp_test_001",
            "mission_id": "mission_m5",
            "hypothesis": "Test hypothesis",
            "config": {
                "model_family": "LightGBM",
                "feature_family": "Alpha158",
                "horizon_days": 10
            }
        }
        first = ResearchMemory.enqueue_experiment(exp)
        self.assertTrue(first, "First enqueue should succeed")

        exp2 = dict(exp)
        exp2["experiment_id"] = "exp_test_002"  # Different ID, same config
        second = ResearchMemory.enqueue_experiment(exp2)
        self.assertFalse(second, "Second enqueue with identical config must return False")

        # Verify only 1 row exists
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM research_experiments_queue WHERE mission_id = 'mission_m5'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_database_uniqueness_constraint_enforced(self):
        """Database partial unique index must raise IntegrityError on duplicate (mission_id, config_hash)."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO research_experiments_queue (
                experiment_id, mission_id, config_json, config_hash, created_at, updated_at
            ) VALUES ('exp_raw_1', 'mission_db_test', '{}', 'hash_unique_123', '2026-09-11', '2026-09-11');
        """)
        conn.commit()

        # Second insert with same mission and config_hash must violate unique constraint
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("""
                INSERT INTO research_experiments_queue (
                    experiment_id, mission_id, config_json, config_hash, created_at, updated_at
                ) VALUES ('exp_raw_2', 'mission_db_test', '{}', 'hash_unique_123', '2026-09-11', '2026-09-11');
            """)
            conn.commit()
        conn.close()

    def test_ledger_database_uniqueness_constraint_enforced(self):
        """Database partial unique index on ledger must raise IntegrityError on duplicate (mission_id, config_hash)."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO research_experiments_ledger (
                experiment_id, mission_id, config_hash, status, created_at
            ) VALUES ('exp_led_1', 'mission_db_test', 'hash_led_999', 'COMPLETED', '2026-09-11');
        """)
        conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("""
                INSERT INTO research_experiments_ledger (
                    experiment_id, mission_id, config_hash, status, created_at
                ) VALUES ('exp_led_2', 'mission_db_test', 'hash_led_999', 'COMPLETED', '2026-09-11');
            """)
            conn.commit()
        conn.close()

    def test_record_experiment_result_no_lifecycle_reset(self):
        """Recording results for an existing experiment updates mutable metrics without resetting created_at."""
        exp = {
            "experiment_id": "exp_test_life",
            "mission_id": "mission_m5",
            "hypothesis": "Initial hypothesis",
            "config": {"model_family": "LightGBM", "horizon_days": 10}
        }
        ResearchMemory.enqueue_experiment(exp)

        # Initial ledger entry
        initial_record = {
            "experiment_id": "exp_test_life",
            "mission_id": "mission_m5",
            "config_hash": "cfg_life_001",
            "status": "RUNNING",
            "metrics": {"sharpe": 1.0}
        }
        ResearchMemory.record_experiment_result(initial_record)

        conn = sqlite3.connect(self.db_path)
        first_created_at = conn.execute("SELECT created_at FROM research_experiments_ledger WHERE experiment_id = 'exp_test_life'").fetchone()[0]
        conn.close()

        # Update experiment with completion metrics
        completed_record = {
            "experiment_id": "exp_test_life",
            "mission_id": "mission_m5",
            "config_hash": "cfg_life_001",
            "status": "COMPLETED",
            "metrics": {"sharpe": 1.75},
            "quality_class": "CHALLENGER_VIABLE"
        }
        ResearchMemory.record_experiment_result(completed_record)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT created_at, status, quality_class FROM research_experiments_ledger WHERE experiment_id = 'exp_test_life'").fetchone()
        conn.close()

        self.assertEqual(row[0], first_created_at, "created_at must NOT be reset on update")
        self.assertEqual(row[1], "COMPLETED")
        self.assertEqual(row[2], "CHALLENGER_VIABLE")

    def test_budget_counts_unique_experiments(self):
        """Research budget must count distinct config_hashes, preventing duplicates from exhausting budget."""
        # Insert 2 records with distinct config hashes
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO research_experiments_ledger (experiment_id, mission_id, config_hash, status, created_at)
            VALUES ('exp_b1', 'mission_budget_m5', 'hash_aaa', 'COMPLETED', '2026-09-11'),
                   ('exp_b2', 'mission_budget_m5', 'hash_bbb', 'COMPLETED', '2026-09-11');
        """)
        conn.commit()
        conn.close()

        counts = ResearchMemory.get_mission_counts("mission_budget_m5")
        self.assertEqual(counts["unique_completed"], 2)


if __name__ == "__main__":
    unittest.main()

