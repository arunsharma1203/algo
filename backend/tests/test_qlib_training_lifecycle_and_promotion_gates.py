"""
UNIT & INTEGRATION TESTS: QLIB TRAINING LIFECYCLE & AUTHORITATIVE PROMOTION GATES
=================================================================================
Verifies:
1. TEST 1: Job persisted immediately on start (SQLite row exists with status RUNNING).
2. TEST 2: Reconnection / Refresh restores exact same job state without reset to 0%.
3. TEST 3: Backend training continues independently when client disconnected.
4. TEST 4: Training completion stores candidate model in history and marks status COMPLETED.
5. TEST 5: Candidate with negative Sharpe blocked from promotion by policy (Gate 4 failure).
6. TEST 6: Candidate with worse Sharpe than champion blocked from promotion (Gate 8 failure).
7. TEST 7: Candidate with excessive drawdown blocked from promotion (Gate 7 failure).
8. TEST 8: Candidate with < 30 OOS trades blocked from promotion (Gate 6 failure).
9. TEST 9: Candidate with invalid artifact hash blocked (Gate 11 cryptographic failure).
10. TEST 10: Valid candidate beating champion passes all gates (is_eligible = True).
11. TEST 11: Backend promote endpoint re-checks all 12 gates independently (fails closed).
12. TEST 12: Race condition: candidate promoted while another was being evaluated rejects second candidate if active model changed.
13. TEST 13: Promotion updates active model pointer atomically and archives previous champion.
14. TEST 14: Regression: Legacy Champion models remain bit-for-bit identical.
15. TEST 15: Regression: Existing Portfolio Research Challenger OOS state remains untouched.
"""

import os
import sys
import unittest
import tempfile
import shutil
import sqlite3
import hashlib
import json
import time
from unittest.mock import patch, MagicMock

from app.qlib_engine.training_job_tracker import TrainingJobTracker
from app.qlib_engine.training_telemetry import TrainingTelemetry
from app.qlib_engine.promotion_policy import QlibPromotionPolicy
from app.qlib_engine.model_registry import QlibModelRegistry

INTRADAY_CHAMPION_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
SWING_CHAMPION_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"


class TestQlibTrainingLifecycleAndPromotionGates(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="qlib_test_")
        self.temp_db = os.path.join(self.temp_dir, "test_market.db")
        self.tracker = TrainingJobTracker(db_path=self.temp_db)
        self.registry = QlibModelRegistry(base_dir=os.path.join(self.temp_dir, "models"))

    def tearDown(self):
        QlibModelRegistry._base_dir_override = None
        TrainingJobTracker._db_path_override = None
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _create_mock_artifact(self, content=b"mock_model_weights_test_123"):
        file_path = os.path.join(self.temp_dir, "mock_model.pkl")
        with open(file_path, "wb") as f:
            f.write(content)
        sha = hashlib.sha256(content).hexdigest()
        return file_path, sha

    # -------------------------------------------------------------------------
    # TEST 1: Job persisted immediately on start
    # -------------------------------------------------------------------------
    def test_01_job_persisted_immediately_on_start(self):
        job_id = "test_job_persist_01"
        self.tracker.create_job(
            job_id=job_id,
            strategy="SWING",
            universe="BOOTSTRAP_35",
            model_family="LGBModel",
            feature_family="Alpha158"
        )

        with sqlite3.connect(self.temp_db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM qlib_training_jobs WHERE job_id = ?", (job_id,)).fetchone()

        self.assertIsNotNone(row, "Job row must exist immediately in SQLite upon creation")
        self.assertEqual(row["job_id"], job_id)
        self.assertEqual(row["status"], "RUNNING")
        self.assertEqual(row["stage"], "INITIALIZING")
        self.assertLessEqual(row["progress_pct"], 5.0)
        self.assertEqual(row["universe"], "BOOTSTRAP_35")
        self.assertEqual(row["model_family"], "LGBModel")
        self.assertIsNotNone(row["started_at"])

    # -------------------------------------------------------------------------
    # TEST 2: Reconnection / Refresh restores exact same job state
    # -------------------------------------------------------------------------
    def test_02_reconnection_restores_same_job_state(self):
        job_id = "test_job_refresh_02"
        self.tracker.create_job(
            job_id=job_id,
            strategy="SWING",
            universe="NIFTY_500_ESTABLISHED",
            model_family="DEnsembleModel",
            feature_family="Alpha158"
        )
        self.tracker.update_progress(
            job_id=job_id,
            stage="MODEL_TRAINING",
            progress_pct=65,
            elapsed_seconds=180,
            estimated_remaining_seconds=95,
            estimated_total_seconds=275,
            features_count=158,
            rows_processed=124000
        )

        # Simulate browser refresh / new request hydrating from DB
        active_job = self.tracker.get_active_job()
        self.assertIsNotNone(active_job, "Should return active job on reload")
        self.assertEqual(active_job["job_id"], job_id)
        self.assertEqual(active_job["status"], "RUNNING")
        self.assertEqual(active_job["stage"], "MODEL_TRAINING")
        self.assertEqual(active_job["progress_pct"], 65)
        self.assertEqual(active_job["elapsed_seconds"], 180)
        self.assertEqual(active_job["estimated_remaining_seconds"], 95)
        self.assertEqual(active_job["rows_processed"], 124000)

        # Telemetry manager initialization rehydration test
        with patch.object(TrainingJobTracker, "get_active_job", return_value=active_job):
            telemetry = TrainingTelemetry()
            telemetry._hydrate_from_db()
            snap = telemetry.get_latest()
            self.assertEqual(snap["job_id"], job_id)
            self.assertEqual(snap["progress_pct"], 65)
            self.assertEqual(snap["status"], "RUNNING")
            self.assertEqual(snap["stage"], "MODEL_TRAINING")

    # -------------------------------------------------------------------------
    # TEST 3: Backend training continues independently when client disconnected
    # -------------------------------------------------------------------------
    def test_03_backend_training_continues_independently(self):
        job_id = "test_job_daemon_03"
        with patch.object(TrainingJobTracker, "create_job") as mock_create, \
             patch.object(TrainingJobTracker, "update_progress") as mock_update:
            telemetry = TrainingTelemetry()
            telemetry.start_job(job_id=job_id, strategy="SWING", universe_name="BOOTSTRAP_35")

            # Client subscribes then disconnects
            sub_queue = telemetry.subscribe()
            initial_msg = json.loads(sub_queue.get_nowait())
            self.assertEqual(initial_msg["job_id"], job_id)
            telemetry.unsubscribe(sub_queue)

            # Backend worker continues updating progress with 0 subscribers
            telemetry.update_stage(stage_name="VALIDATING_MODEL", progress_pct=85.0)

            # Confirm tracker update was invoked independently
            mock_update.assert_called()
            snap = telemetry.get_latest()
            self.assertEqual(snap["stage"], "VALIDATING_MODEL")
            self.assertEqual(snap["progress_pct"], 85.0)

    # -------------------------------------------------------------------------
    # TEST 4: Training completion stores candidate model in history
    # -------------------------------------------------------------------------
    def test_04_training_completion_stores_candidate_in_history(self):
        job_id = "test_job_complete_04"
        candidate_id = "qlib_challenger_swing_20260911_test"
        self.tracker.create_job(job_id=job_id, strategy="SWING")
        self.tracker.complete_job(
            job_id=job_id,
            candidate_model_id=candidate_id,
            metrics={"sharpe": 0.85, "profit_factor": 1.25, "trades": 120}
        )

        # Active job must now be None
        active = self.tracker.get_active_job()
        self.assertIsNone(active, "Active job should be None once completed")

        # History must contain completed job with metrics
        history = self.tracker.list_jobs(limit=10)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["job_id"], job_id)
        self.assertEqual(history[0]["status"], "COMPLETED")
        self.assertEqual(history[0]["candidate_model_id"], candidate_id)
        self.assertEqual(history[0]["progress_pct"], 100)
        self.assertIn("sharpe", history[0]["metrics"])
        self.assertEqual(history[0]["metrics"]["sharpe"], 0.85)

    # -------------------------------------------------------------------------
    # TEST 5: Candidate with negative Sharpe blocked from promotion
    # -------------------------------------------------------------------------
    def test_05_candidate_with_negative_sharpe_blocked(self):
        art_path, art_sha = self._create_mock_artifact()
        candidate = {
            "model_id": "test_neg_sharpe",
            "strategy": "SWING",
            "status": "FROZEN_CHALLENGER",
            "model_path": art_path,
            "artifact_sha256": art_sha,
            "training_completed": True,
            "validation_successful": True,
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "features_count": 158,
            "metrics": {
                "sharpe": -0.45,
                "profit_factor": 0.82,
                "trades": 50,
                "max_drawdown_pct": 15.0,
                "win_rate": 42.0
            }
        }

        res = QlibPromotionPolicy.evaluate_promotion_eligibility(
            candidate_manifest=candidate,
            champion_manifest=None
        )

        self.assertFalse(res.is_eligible, "Candidate with negative Sharpe MUST NEVER be eligible")
        self.assertIn("positive_sharpe", res.failed_gates)
        self.assertTrue(any("negative" in r.lower() or "sharpe" in r.lower() for r in res.failed_reasons))

    # -------------------------------------------------------------------------
    # TEST 6: Candidate with worse Sharpe than champion blocked from promotion
    # -------------------------------------------------------------------------
    def test_06_candidate_with_worse_sharpe_than_champion_blocked(self):
        art_path, art_sha = self._create_mock_artifact()
        candidate = {
            "model_id": "test_worse_sharpe",
            "strategy": "SWING",
            "status": "FROZEN_CHALLENGER",
            "model_path": art_path,
            "artifact_sha256": art_sha,
            "training_completed": True,
            "validation_successful": True,
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "features_count": 158,
            "metrics": {
                "sharpe": 0.65,  # Positive, but worse than champion's 1.20
                "profit_factor": 1.15,
                "trades": 60,
                "max_drawdown_pct": 12.0,
                "win_rate": 51.0
            }
        }
        champion = {
            "model_id": "current_active_champ",
            "strategy": "SWING",
            "status": "ACTIVE",
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "metrics": {
                "sharpe": 1.20,
                "profit_factor": 1.40,
                "trades": 100,
                "max_drawdown_pct": 10.0,
                "win_rate": 55.0
            }
        }

        res = QlibPromotionPolicy.evaluate_promotion_eligibility(
            candidate_manifest=candidate,
            champion_manifest=champion
        )

        self.assertFalse(res.is_eligible, "Candidate with worse Sharpe than champion must be blocked")
        self.assertIn("champion_beating_sharpe", res.failed_gates)

    # -------------------------------------------------------------------------
    # TEST 7: Drawdown Risk Ceiling & True Peak-to-Trough Calculation Tests
    # -------------------------------------------------------------------------
    def test_07a_candidate_with_nan_drawdown_blocked(self):
        art_path, art_sha = self._create_mock_artifact()
        candidate = {
            "model_id": "test_nan_dd",
            "strategy": "SWING",
            "status": "FROZEN_CHALLENGER",
            "model_path": art_path,
            "artifact_sha256": art_sha,
            "training_completed": True,
            "validation_successful": True,
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "features_count": 158,
            "metrics": {
                "sharpe": 1.50,
                "profit_factor": 1.80,
                "trades": 80,
                "max_drawdown_pct": float("nan"),
                "win_rate": 55.0
            }
        }
        res = QlibPromotionPolicy.evaluate_promotion_eligibility(
            candidate_manifest=candidate,
            champion_manifest=None
        )
        self.assertFalse(res.is_eligible, "Candidate with NaN drawdown must be blocked")
        self.assertIn("max_drawdown", res.failed_gates)

    def test_07b_candidate_with_excessive_drawdown_above_20pct_blocked(self):
        art_path, art_sha = self._create_mock_artifact()
        # Drawdown of 25.0% exceeds the 20.0% capital preservation ceiling
        candidate = {
            "model_id": "test_excessive_dd",
            "strategy": "SWING",
            "status": "FROZEN_CHALLENGER",
            "model_path": art_path,
            "artifact_sha256": art_sha,
            "training_completed": True,
            "validation_successful": True,
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "features_count": 158,
            "metrics": {
                "sharpe": 1.50,
                "profit_factor": 1.80,
                "trades": 80,
                "max_drawdown_pct": 25.0,  # > 20.0% ceiling
                "win_rate": 55.0
            }
        }
        res = QlibPromotionPolicy.evaluate_promotion_eligibility(
            candidate_manifest=candidate,
            champion_manifest=None
        )
        self.assertFalse(res.is_eligible, "Candidate with drawdown > 20.0% must be blocked by Gate 7")
        self.assertIn("max_drawdown", res.failed_gates)
        self.assertTrue(any("exceeds platform maximum risk ceiling" in r for r in res.failed_reasons))

    def test_07c_candidate_with_extreme_old_arithmetic_drawdown_blocked(self):
        art_path, art_sha = self._create_mock_artifact()
        # Old buggy arithmetic values like 3,681.53% must definitely fail Gate 7
        candidate = {
            "model_id": "test_extreme_arithmetic_dd",
            "strategy": "SWING",
            "status": "FROZEN_CHALLENGER",
            "model_path": art_path,
            "artifact_sha256": art_sha,
            "training_completed": True,
            "validation_successful": True,
            "model_family": "DEnsembleModel",
            "feature_family": "Alpha360",
            "features_count": 360,
            "metrics": {
                "sharpe": 0.23,
                "profit_factor": 1.10,
                "trades": 1861,
                "max_drawdown_pct": 3681.53,  # Absurd arithmetic value
                "win_rate": 46.8
            }
        }
        res = QlibPromotionPolicy.evaluate_promotion_eligibility(
            candidate_manifest=candidate,
            champion_manifest=None
        )
        self.assertFalse(res.is_eligible, "Candidate with 3681.53% drawdown must fail Gate 7")
        self.assertIn("max_drawdown", res.failed_gates)

    def test_07d_candidate_with_negative_drawdown_blocked(self):
        art_path, art_sha = self._create_mock_artifact()
        candidate = {
            "model_id": "test_negative_dd",
            "strategy": "SWING",
            "status": "FROZEN_CHALLENGER",
            "model_path": art_path,
            "artifact_sha256": art_sha,
            "training_completed": True,
            "validation_successful": True,
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "features_count": 158,
            "metrics": {
                "sharpe": 1.50,
                "profit_factor": 1.80,
                "trades": 80,
                "max_drawdown_pct": -5.0,  # Negative drawdown is invalid
                "win_rate": 55.0
            }
        }
        res = QlibPromotionPolicy.evaluate_promotion_eligibility(
            candidate_manifest=candidate,
            champion_manifest=None
        )
        self.assertFalse(res.is_eligible, "Candidate with negative drawdown must fail Gate 7")
        self.assertIn("max_drawdown", res.failed_gates)

    def test_07e_candidate_with_valid_drawdown_passes_gate(self):
        art_path, art_sha = self._create_mock_artifact()
        candidate = {
            "model_id": "test_valid_dd",
            "strategy": "SWING",
            "status": "FROZEN_CHALLENGER",
            "model_path": art_path,
            "artifact_sha256": art_sha,
            "training_completed": True,
            "validation_successful": True,
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "features_count": 158,
            "metrics": {
                "sharpe": 1.50,
                "profit_factor": 1.80,
                "trades": 80,
                "max_drawdown_pct": 14.50,  # Valid drawdown <= 20.0%
                "win_rate": 55.0
            }
        }
        res = QlibPromotionPolicy.evaluate_promotion_eligibility(
            candidate_manifest=candidate,
            champion_manifest=None
        )
        self.assertTrue(res.gates["max_drawdown"]["passed"], "Drawdown of 14.5% must pass Gate 7")
        self.assertNotIn("max_drawdown", res.failed_gates)

    def test_07f_true_peak_to_trough_drawdown_calculation_bounded(self):
        from app.qlib_engine.trainer import QlibTrainer
        import pandas as pd
        import numpy as np

        dates = pd.date_range("2024-01-01", periods=100, freq="D")
        tickers = ["TICKER_1", "TICKER_2", "TICKER_3", "TICKER_4", "TICKER_5"]
        idx = pd.MultiIndex.from_product([dates, tickers], names=["datetime", "instrument"])
        
        # Suppose every single trade loses 2% (normalized label = -0.02)
        # Across 100 days * 5 stocks = 500 trades
        # Under the old buggy np.cumsum, 500 * -2.25% = -1,125% arithmetic drop!
        test_df = pd.DataFrame({"label": -0.02}, index=idx)
        preds = pd.Series(1.0, index=idx)
        mock_dataset = MagicMock()
        mock_dataset.prepare.return_value = test_df

        metrics = QlibTrainer.compute_oos_metrics(preds, mock_dataset, top_k=5, holding_period=5)
        
        self.assertIsNotNone(metrics["max_drawdown_pct"])
        self.assertGreaterEqual(metrics["max_drawdown_pct"], 0.0)
        self.assertLessEqual(metrics["max_drawdown_pct"], 100.0, "True drawdown must be mathematically bounded in [0%, 100%]")
        # Prove arithmetic cumsum drop (> 1,000%) does NOT occur
        self.assertLess(metrics["max_drawdown_pct"], 95.0, "Equity curve compound drawdown cannot equal arithmetic cumulative sum")
        self.assertIn("equity_curve", metrics)
        self.assertIn("peak_equity", metrics)
        self.assertIn("trough_equity", metrics)
        self.assertIn("drawdown_period", metrics)
        self.assertEqual(metrics["peak_equity"], 100.0)
        self.assertLess(metrics["trough_equity"], 100.0)
        self.assertGreater(len(metrics["equity_curve"]), 1)

    # -------------------------------------------------------------------------
    # TEST 8: Candidate with < 30 OOS trades blocked from promotion
    # -------------------------------------------------------------------------
    def test_08_candidate_below_30_trades_blocked(self):
        art_path, art_sha = self._create_mock_artifact()
        candidate = {
            "model_id": "test_under_30_trades",
            "strategy": "SWING",
            "status": "FROZEN_CHALLENGER",
            "model_path": art_path,
            "artifact_sha256": art_sha,
            "training_completed": True,
            "validation_successful": True,
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "features_count": 158,
            "metrics": {
                "sharpe": 2.10,
                "profit_factor": 2.50,
                "trades": 18,  # Only 18 trades, under the 30 trade minimum
                "max_drawdown_pct": 5.0,
                "win_rate": 60.0
            }
        }

        res = QlibPromotionPolicy.evaluate_promotion_eligibility(
            candidate_manifest=candidate,
            champion_manifest=None
        )

        self.assertFalse(res.is_eligible, "Candidate with < 30 OOS trades must be blocked")
        self.assertIn("min_trades_30", res.failed_gates)

    # -------------------------------------------------------------------------
    # TEST 9: Candidate with invalid artifact hash blocked
    # -------------------------------------------------------------------------
    def test_09_candidate_with_tampered_hash_blocked(self):
        art_path, _ = self._create_mock_artifact()
        candidate = {
            "model_id": "test_tampered_artifact",
            "strategy": "SWING",
            "status": "FROZEN_CHALLENGER",
            "model_path": art_path,
            "artifact_sha256": "0000000000000000000000000000000000000000000000000000000000000000",  # False hash
            "training_completed": True,
            "validation_successful": True,
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "features_count": 158,
            "metrics": {
                "sharpe": 1.50,
                "profit_factor": 1.60,
                "trades": 100,
                "max_drawdown_pct": 8.0,
                "win_rate": 55.0
            }
        }

        res = QlibPromotionPolicy.evaluate_promotion_eligibility(
            candidate_manifest=candidate,
            champion_manifest=None
        )

        self.assertFalse(res.is_eligible, "Candidate with mismatched artifact SHA-256 must fail gate 11")
        self.assertIn("cryptographic_integrity", res.failed_gates)

    # -------------------------------------------------------------------------
    # TEST 10: Valid candidate beating champion passes all gates
    # -------------------------------------------------------------------------
    def test_10_valid_candidate_beating_champion_eligible(self):
        art_path, art_sha = self._create_mock_artifact()
        candidate = {
            "model_id": "test_valid_champ_beater",
            "strategy": "SWING",
            "status": "FROZEN_CHALLENGER",
            "model_path": art_path,
            "artifact_sha256": art_sha,
            "training_completed": True,
            "validation_successful": True,
            "model_family": "DEnsembleModel",
            "feature_family": "Alpha158",
            "features_count": 158,
            "metrics": {
                "sharpe": 1.45,
                "profit_factor": 1.55,
                "trades": 250,
                "max_drawdown_pct": 7.5,
                "win_rate": 58.0
            }
        }
        champion = {
            "model_id": "old_champ_model",
            "strategy": "SWING",
            "status": "ACTIVE",
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "metrics": {
                "sharpe": 0.85,
                "profit_factor": 1.15,
                "trades": 180,
                "max_drawdown_pct": 12.0,
                "win_rate": 51.0
            }
        }

        res = QlibPromotionPolicy.evaluate_promotion_eligibility(
            candidate_manifest=candidate,
            champion_manifest=champion
        )

        self.assertTrue(res.is_eligible, f"Candidate beating champion must pass all gates. Failed: {res.failed_reasons}")
        self.assertEqual(len(res.failed_gates), 0)
        self.assertGreaterEqual(len(res.passed_gates), 12)

    # -------------------------------------------------------------------------
    # TEST 11: Backend promote endpoint re-checks all 12 gates independently
    # -------------------------------------------------------------------------
    def test_11_backend_promote_rechecks_gates_independently(self):
        ch_dir = self.registry.get_challengers_dir("SWING")
        m_dir = os.path.join(ch_dir, "reg_test_negative_sharpe")
        os.makedirs(m_dir, exist_ok=True)
        art_path = os.path.join(m_dir, "model.pkl")
        with open(art_path, "wb") as f:
            f.write(b"mock_weights")
        art_sha = hashlib.sha256(b"mock_weights").hexdigest()

        challenger_manifest = {
            "model_id": "reg_test_negative_sharpe",
            "strategy": "SWING",
            "created_at": "2026-09-11T20:00:00",
            "status": "FROZEN_CHALLENGER",
            "model_path": art_path,
            "artifact_sha256": art_sha,
            "training_completed": True,
            "validation_successful": True,
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "features_count": 158,
            "metrics": {
                "sharpe": -0.88,
                "profit_factor": 0.70,
                "win_rate": 45.0,
                "trades": 45,
                "max_drawdown_pct": 22.0
            }
        }
        with open(os.path.join(m_dir, "manifest.json"), "w") as f:
            json.dump(challenger_manifest, f)

        # Attempt promote: backend registry must independently reject with ValueError
        with self.assertRaises(ValueError) as ctx:
            self.registry.promote_challenger(
                strategy="SWING",
                model_id="reg_test_negative_sharpe",
                manual_approval=True
            )
        self.assertIn("Safety Gates", str(ctx.exception))

    # -------------------------------------------------------------------------
    # TEST 12: Race condition: rejection if active model changed during evaluation
    # -------------------------------------------------------------------------
    def test_12_race_condition_rejection_if_active_model_changed(self):
        ch_dir = self.registry.get_challengers_dir("SWING")
        strat_dir = self.registry.get_strategy_dir("SWING")

        art_champ = os.path.join(strat_dir, "active_model.pkl")
        with open(art_champ, "wb") as f:
            f.write(b"weights_A")
        sha_champ = hashlib.sha256(b"weights_A").hexdigest()

        # Initial baseline champion in registry
        initial_champ = {
            "model_id": "initial_champ",
            "strategy": "SWING",
            "status": "ACTIVE",
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "model_path": art_champ,
            "artifact_sha256": sha_champ,
            "metrics": {"sharpe": 0.50, "profit_factor": 1.05, "win_rate": 48.0, "trades": 50, "max_drawdown_pct": 10.0}
        }
        with open(os.path.join(strat_dir, "active_manifest.json"), "w") as f:
            json.dump(initial_champ, f)

        # Candidate B had Sharpe 0.80 (better than initial champ 0.50)
        m_dir_b = os.path.join(ch_dir, "cand_b")
        os.makedirs(m_dir_b, exist_ok=True)
        art_b = os.path.join(m_dir_b, "model.pkl")
        with open(art_b, "wb") as f:
            f.write(b"weights_B")
        sha_b = hashlib.sha256(b"weights_B").hexdigest()

        cand_b = {
            "model_id": "cand_b",
            "strategy": "SWING",
            "status": "FROZEN_CHALLENGER",
            "model_path": art_b,
            "artifact_sha256": sha_b,
            "training_completed": True,
            "validation_successful": True,
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "features_count": 158,
            "metrics": {"sharpe": 0.80, "profit_factor": 1.15, "win_rate": 50.0, "trades": 75, "max_drawdown_pct": 9.0}
        }
        with open(os.path.join(m_dir_b, "manifest.json"), "w") as f:
            json.dump(cand_b, f)

        # While B was waiting, Candidate A got promoted and set champion Sharpe to 1.30
        new_champ = {
            "model_id": "super_champ_A",
            "strategy": "SWING",
            "status": "ACTIVE",
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "model_path": art_champ,
            "artifact_sha256": sha_champ,
            "metrics": {"sharpe": 1.30, "profit_factor": 1.45, "win_rate": 55.0, "trades": 150, "max_drawdown_pct": 8.0}
        }
        with open(os.path.join(strat_dir, "active_manifest.json"), "w") as f:
            json.dump(new_champ, f)

        # Now when B tries to promote, it must be rejected because it lags the NEW champion!
        with self.assertRaises(ValueError) as ctx:
            self.registry.promote_challenger(
                strategy="SWING",
                model_id="cand_b",
                manual_approval=True
            )
        self.assertIn("Safety Gates", str(ctx.exception))

    # -------------------------------------------------------------------------
    # TEST 13: Promotion updates active model pointer atomically and archives
    # -------------------------------------------------------------------------
    def test_13_promotion_atomic_update_and_archival(self):
        ch_dir = self.registry.get_challengers_dir("SWING")
        strat_dir = self.registry.get_strategy_dir("SWING")

        old_art = os.path.join(strat_dir, "active_model.pkl")
        with open(old_art, "wb") as f:
            f.write(b"initial_champ_weights")
        old_sha = hashlib.sha256(b"initial_champ_weights").hexdigest()

        old_champ = {
            "model_id": "old_champ_model",
            "strategy": "SWING",
            "status": "ACTIVE",
            "model_family": "LGBModel",
            "feature_family": "Alpha158",
            "model_path": old_art,
            "artifact_sha256": old_sha,
            "metrics": {"sharpe": 0.40, "profit_factor": 1.05, "win_rate": 45.0, "trades": 40, "max_drawdown_pct": 14.0}
        }
        with open(os.path.join(strat_dir, "active_manifest.json"), "w") as f:
            json.dump(old_champ, f)

        # Eligible challenger
        m_dir_cand = os.path.join(ch_dir, "new_winner")
        os.makedirs(m_dir_cand, exist_ok=True)
        cand_art = os.path.join(m_dir_cand, "model.pkl")
        with open(cand_art, "wb") as f:
            f.write(b"new_winner_weights_content_123")
        cand_sha = hashlib.sha256(b"new_winner_weights_content_123").hexdigest()

        cand_manifest = {
            "model_id": "new_winner",
            "strategy": "SWING",
            "status": "FROZEN_CHALLENGER",
            "created_at": "2026-09-11T20:30:00",
            "model_path": cand_art,
            "artifact_sha256": cand_sha,
            "training_completed": True,
            "validation_successful": True,
            "model_family": "DEnsembleModel",
            "feature_family": "Alpha158",
            "features_count": 158,
            "metrics": {"sharpe": 1.15, "profit_factor": 1.35, "win_rate": 52.0, "trades": 120, "max_drawdown_pct": 9.0}
        }
        with open(os.path.join(m_dir_cand, "manifest.json"), "w") as f:
            json.dump(cand_manifest, f)

        # Execute promotion
        res = self.registry.promote_challenger(
            strategy="SWING",
            model_id="new_winner",
            manual_approval=True
        )

        self.assertEqual(res["status"], "PROMOTED")
        self.assertEqual(res["model_id"], "new_winner")

        # Active model file must now contain the new candidate's weights
        with open(old_art, "rb") as f:
            new_weights = f.read()
        self.assertEqual(new_weights, b"new_winner_weights_content_123")

        # Previous champion must be archived
        archived_dir = self.registry.get_archived_dir("SWING")
        archived_files = os.listdir(archived_dir)
        self.assertTrue(any("old_champ_model" in f for f in archived_files), "Previous champion must be in archived directory")

    # -------------------------------------------------------------------------
    # TEST 14: Regression: Legacy Champion models remain bit-for-bit identical
    # -------------------------------------------------------------------------
    def test_14_legacy_champion_hashes_unchanged(self):
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        intraday_path = os.path.join(backend_dir, "models", "intraday", "champion_ensemble.pkl")
        swing_path = os.path.join(backend_dir, "models", "swing", "champion_ensemble.pkl")

        self.assertTrue(os.path.exists(intraday_path), f"Intraday champion must exist at {intraday_path}")
        self.assertTrue(os.path.exists(swing_path), f"Swing champion must exist at {swing_path}")

        with open(intraday_path, "rb") as f:
            actual_intraday = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(
            actual_intraday,
            INTRADAY_CHAMPION_HASH,
            f"Intraday legacy champion has been modified! Expected {INTRADAY_CHAMPION_HASH}, got {actual_intraday}"
        )

        with open(swing_path, "rb") as f:
            actual_swing = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(
            actual_swing,
            SWING_CHAMPION_HASH,
            f"Swing legacy champion has been modified! Expected {SWING_CHAMPION_HASH}, got {actual_swing}"
        )

    # -------------------------------------------------------------------------
    # TEST 15: Regression: Existing Portfolio Research Challenger OOS state untouched
    # -------------------------------------------------------------------------
    def test_15_portfolio_research_challenger_oos_untouched(self):
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        db_path = os.path.join(backend_dir, "market_data.db")
        if not os.path.exists(db_path):
            self.skipTest("market_data.db does not exist in backend directory")

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "research_candidate_vault" not in tables:
                self.skipTest("research_candidate_vault table not present")

            rows = conn.execute("SELECT status, COUNT(*) as cnt FROM research_candidate_vault GROUP BY status").fetchall()
            status_map = {r["status"]: r["cnt"] for r in rows}

        # Check that research candidate vault has records and valid states
        self.assertGreater(sum(status_map.values()), 0, "Research candidate vault should have existing records")
        # Ensure no accidental status overwrite to invalid values
        for status in status_map.keys():
            self.assertIn(status, ["OOS_PENDING", "OOS_PASSED", "OOS_FAILED", "REJECTED", "ACTIVE_CHALLENGER", "ARCHIVED"])


if __name__ == "__main__":
    unittest.main()
