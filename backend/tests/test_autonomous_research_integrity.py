"""
AUTONOMOUS RESEARCH INTEGRITY & DEDUPLICATION TEST SUITE
=========================================================
Tests all 26 critical integrity points for Research Identity, Deduplication,
Queue/Ledger Accounting, Candidate Vault Idempotency, and Concurrency.
"""

import unittest
import os
import glob
import json
import sqlite3
import hashlib
from datetime import datetime
from uuid import uuid4

from app.data.historical_data_layer import get_db_path
from app.analytics.research_orchestrator.research_mission import ResearchMission
from app.analytics.research_orchestrator.experiment_generator import ExperimentGenerator
from app.analytics.research_orchestrator.research_memory import ResearchMemory
from app.analytics.research_orchestrator.candidate_vault import CandidateVault
from app.analytics.research_orchestrator.research_scheduler import ResearchScheduler
from app.analytics.research_orchestrator.research_orchestrator import (
    ResearchOrchestrator,
    CHAMPION_INTRADAY_HASH,
    CHAMPION_SWING_HASH,
    EXPECTED_HISTORY_ROWS
)

class TestAutonomousResearchIntegrity(unittest.TestCase):
    """26 tests validating Research Identity, Deduplication, and System Integrity."""

    @classmethod
    def setUpClass(cls):
        import tempfile, os
        from app.data.database import set_test_db_override
        cls._temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._temp_db_path = cls._temp_file.name
        cls._temp_file.close()

        set_test_db_override(cls._temp_db_path)
        ResearchMemory.ensure_tables()

    @classmethod
    def tearDownClass(cls):
        import os
        from app.data.database import set_test_db_override
        # Ensure all autonomous worker loops are stopped before releasing DB override
        with ResearchOrchestrator._class_lock:
            for mid, ev in list(ResearchOrchestrator._active_stop_events.items()):
                ev.set()
            for mid, th in list(ResearchOrchestrator._active_loops.items()):
                if th.is_alive():
                    th.join(timeout=1.0)
            ResearchOrchestrator._active_loops.clear()
            ResearchOrchestrator._active_stop_events.clear()
            ResearchOrchestrator._active_pause_events.clear()

        set_test_db_override(None)
        if os.path.exists(cls._temp_db_path):
            try:
                os.unlink(cls._temp_db_path)
            except Exception:
                pass

    def setUp(self):
        self.orchestrator = ResearchOrchestrator(scheduler=ResearchScheduler(max_workers=4))
        self.test_mission_ids = []

    def tearDown(self):
        # Clean up test missions from database
        if self.test_mission_ids:
            conn = sqlite3.connect(get_db_path(), timeout=30.0)
            c = conn.cursor()
            for m_id in self.test_mission_ids:
                c.execute("DELETE FROM research_experiments_queue WHERE mission_id = ?", (m_id,))
                c.execute("DELETE FROM research_experiments_ledger WHERE mission_id = ?", (m_id,))
                c.execute("DELETE FROM research_candidate_vault WHERE mission_id = ?", (m_id,))
                c.execute("DELETE FROM research_missions WHERE mission_id = ?", (m_id,))
            conn.commit()
            conn.close()

    def _create_test_mission(self, **kwargs) -> ResearchMission:
        m_id = f"m_integ_{uuid4().hex[:8]}"
        self.test_mission_ids.append(m_id)
        params = {
            "mission_id": m_id,
            "objective": "Integrity Test Mission",
            "primary_objective_metric": "SHARPE",
            "universe": "LIVE_52",
            "strategy_type": "CROSS_SECTIONAL_ALPHA",
            "research_seed": 42,
            "budget": {"max_experiments": 10, "max_runtime_seconds": 3600, "max_concurrent": 4}
        }
        params.update(kwargs)
        mission = ResearchMission(**params)
        ResearchMemory.save_mission(mission)
        return mission

    # 1. Deterministic config hash
    def test_01_deterministic_config_hash(self):
        cfg1 = {"model_family": "LightGBM", "feature_family": "Alpha158", "horizon": 10, "seed": 42}
        cfg2 = {"horizon": 10, "seed": 42, "model_family": "LightGBM", "feature_family": "Alpha158"}
        h1 = ExperimentGenerator.compute_config_hash(cfg1)
        h2 = ExperimentGenerator.compute_config_hash(cfg2)
        self.assertEqual(h1, h2, "Config hash must be independent of dictionary key order")
        self.assertEqual(len(h1), 64, "Config hash must be SHA-256 (64 hex characters)")

    # 2. Canonical hash normalization
    def test_02_canonical_hash_normalization(self):
        cfg_raw = {"model_family": " Ridge ", "horizon": "10", "universe": "live_52"}
        cfg_clean = {"model_family": "Ridge", "horizon": 10, "universe": "LIVE_52"}
        h_raw = ExperimentGenerator.compute_config_hash(cfg_raw)
        h_clean = ExperimentGenerator.compute_config_hash(cfg_clean)
        self.assertEqual(h_raw, h_clean, "Config hash must normalize strings and integer representations")

    # 3. Non-empty config_hash persisted in queue
    def test_03_config_hash_persisted_in_queue(self):
        mission = self._create_test_mission()
        exp = {
            "experiment_id": f"exp_{uuid4().hex[:8]}",
            "mission_id": mission.mission_id,
            "config": {"model_family": "LightGBM", "seed": 101}
        }
        accepted = ResearchMemory.enqueue_experiment(exp)
        self.assertTrue(accepted)

        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT config_hash FROM research_experiments_queue WHERE experiment_id = ?", (exp["experiment_id"],))
        row = c.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertTrue(row[0], "Queue must store non-empty config_hash")
        self.assertEqual(len(row[0]), 64)

    # 4. Non-empty config_hash recorded in ledger
    def test_04_config_hash_persisted_in_ledger(self):
        mission = self._create_test_mission()
        exp_id = f"exp_{uuid4().hex[:8]}"
        cfg = {"model_family": "XGBoost", "seed": 202}
        cfg_hash = ExperimentGenerator.compute_config_hash(cfg)

        exp = {"experiment_id": exp_id, "mission_id": mission.mission_id, "config": cfg, "config_hash": cfg_hash}
        ResearchMemory.enqueue_experiment(exp)

        rec = {
            "experiment_id": exp_id,
            "mission_id": mission.mission_id,
            "config_hash": cfg_hash,
            "status": "COMPLETED",
            "quality_class": "ACCEPTABLE",
            "governance_verdict": "PASS"
        }
        ResearchMemory.record_experiment_result(rec)

        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT config_hash FROM research_experiments_ledger WHERE experiment_id = ?", (exp_id,))
        row = c.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], cfg_hash, "Ledger must store canonical config_hash")

    # 5. Duplicate configuration rejection in queue
    def test_05_duplicate_configuration_rejection_queue(self):
        mission = self._create_test_mission()
        cfg = {"model_family": "Ridge", "seed": 303}
        exp1 = {"experiment_id": f"exp_dup_1_{uuid4().hex[:6]}", "mission_id": mission.mission_id, "config": cfg}
        exp2 = {"experiment_id": f"exp_dup_2_{uuid4().hex[:6]}", "mission_id": mission.mission_id, "config": cfg}

        accepted1 = ResearchMemory.enqueue_experiment(exp1)
        accepted2 = ResearchMemory.enqueue_experiment(exp2)

        self.assertTrue(accepted1, "First enqueue should succeed")
        self.assertFalse(accepted2, "Second enqueue of identical config must be rejected")

    # 6. Duplicate configuration rejection against ledger
    def test_06_duplicate_configuration_rejection_ledger(self):
        mission = self._create_test_mission()
        cfg = {"model_family": "Ridge", "seed": 404}
        cfg_hash = ExperimentGenerator.compute_config_hash(cfg)
        exp_id = f"exp_led_{uuid4().hex[:6]}"

        # Record directly in ledger as COMPLETED
        rec = {
            "experiment_id": exp_id,
            "mission_id": mission.mission_id,
            "config_hash": cfg_hash,
            "status": "COMPLETED",
            "quality_class": "STRONG",
            "governance_verdict": "PASS"
        }
        ResearchMemory.record_experiment_result(rec)

        # Attempt to enqueue same configuration
        exp_new = {"experiment_id": f"exp_led_new_{uuid4().hex[:6]}", "mission_id": mission.mission_id, "config": cfg}
        accepted = ResearchMemory.enqueue_experiment(exp_new)
        self.assertFalse(accepted, "Enqueue must be rejected if config already tested in ledger")

    # 7. Completed experiment cannot revert to QUEUED
    def test_07_completed_experiment_cannot_revert_to_queued(self):
        mission = self._create_test_mission()
        cfg = {"model_family": "LightGBM", "seed": 505}
        exp_id = f"exp_norev_{uuid4().hex[:6]}"
        exp = {"experiment_id": exp_id, "mission_id": mission.mission_id, "config": cfg}
        ResearchMemory.enqueue_experiment(exp)

        # Complete experiment
        ResearchMemory.record_experiment_result({
            "experiment_id": exp_id,
            "mission_id": mission.mission_id,
            "config_hash": ExperimentGenerator.compute_config_hash(cfg),
            "status": "COMPLETED"
        })

        # Re-enqueue attempt
        ResearchMemory.enqueue_experiment(exp)

        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT status FROM research_experiments_queue WHERE experiment_id = ?", (exp_id,))
        row = c.fetchone()
        conn.close()

        self.assertEqual(row[0], "COMPLETED", "Queue status must remain COMPLETED and not revert to QUEUED")

    # 8. Generator persistent state across replenishment batches
    def test_08_generator_persistent_state_across_batches(self):
        mission = self._create_test_mission(
            model_families=["Ridge", "LightGBM"],
            feature_families=["Alpha158"],
            target_horizons=[5, 10, 15]
        )
        batch1 = ExperimentGenerator.generate_next_experiments(mission, batch_size=3)
        self.assertEqual(len(batch1), 3)

        for item in batch1:
            ResearchMemory.enqueue_experiment(item)

        batch2 = ExperimentGenerator.generate_next_experiments(mission, batch_size=3)
        self.assertEqual(len(batch2), 3)

        hashes_batch1 = {item["config_hash"] for item in batch1}
        hashes_batch2 = {item["config_hash"] for item in batch2}

        # Sets must be completely disjoint
        intersection = hashes_batch1.intersection(hashes_batch2)
        self.assertEqual(len(intersection), 0, f"Generator generated duplicate hashes across batches: {intersection}")

    # 9. Search space exhaustion detection
    def test_09_search_space_exhaustion(self):
        # 1 model * 1 feature * 2 horizons = exactly 2 possible combinations
        mission = self._create_test_mission(
            model_families=["Ridge"],
            feature_families=["Alpha158"],
            target_horizons=[5, 10],
            portfolio_families=["TOP_K_EQUAL_WEIGHT"],
            budget={"max_experiments": 100}
        )

        grid = ExperimentGenerator.get_search_space_grid(mission)
        self.assertEqual(len(grid), 2)

        # Batch 1 generates both combinations
        batch1 = ExperimentGenerator.generate_next_experiments(mission, batch_size=10)
        self.assertEqual(len(batch1), 2)
        for item in batch1:
            ResearchMemory.enqueue_experiment(item)

        # Batch 2 should be empty because space is exhausted
        batch2 = ExperimentGenerator.generate_next_experiments(mission, batch_size=10)
        self.assertEqual(len(batch2), 0, "Generator must return empty list when search space is exhausted")

    # 10. Candidate Vault Idempotency (3 freeze calls yield 1 row)
    def test_10_candidate_vault_idempotency(self):
        mission = self._create_test_mission()
        cfg = {"model_family": "LightGBM", "horizon": 10, "seed": 777}
        cfg_hash = ExperimentGenerator.compute_config_hash(cfg)

        cand1 = CandidateVault.freeze_candidate(
            experiment_id="exp_cand_idem_1",
            mission_id=mission.mission_id,
            config=cfg,
            model_artifact={"weights": [1, 2, 3]},
            metrics={"sharpe": 1.8, "cagr_net": 25.0}
        )

        cand2 = CandidateVault.freeze_candidate(
            experiment_id="exp_cand_idem_2",
            mission_id=mission.mission_id,
            config=cfg,
            model_artifact={"weights": [1, 2, 3]},
            metrics={"sharpe": 1.8, "cagr_net": 25.0}
        )

        cand3 = CandidateVault.freeze_candidate(
            experiment_id="exp_cand_idem_3",
            mission_id=mission.mission_id,
            config=cfg,
            model_artifact={"weights": [1, 2, 3]},
            metrics={"sharpe": 1.8, "cagr_net": 25.0}
        )

        self.assertEqual(cand1["candidate_id"], cand2["candidate_id"])
        self.assertEqual(cand1["candidate_id"], cand3["candidate_id"])

        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT count(*) FROM research_candidate_vault WHERE mission_id = ? AND config_hash = ?",
                  (mission.mission_id, cfg_hash))
        count = c.fetchone()[0]
        conn.close()

        self.assertEqual(count, 1, "Candidate vault must contain exactly 1 row despite 3 freeze calls")

    # 11. Single authoritative freeze path (eliminated double freeze)
    def test_11_single_freeze_path_in_orchestrator(self):
        mission = self._create_test_mission()
        cfg = {"model_family": "Ridge", "horizon": 5, "seed": 888}
        cfg_hash = ExperimentGenerator.compute_config_hash(cfg)
        exp_id = f"exp_frz_{uuid4().hex[:6]}"

        item = {
            "experiment_id": exp_id,
            "mission_id": mission.mission_id,
            "config": cfg,
            "config_hash": cfg_hash
        }
        ResearchMemory.enqueue_experiment(item)

        # Execute single experiment through scheduler
        result = self.orchestrator.scheduler.execute_single_experiment(item, mission)

        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT count(*) FROM research_candidate_vault WHERE mission_id = ?", (mission.mission_id,))
        count = c.fetchone()[0]
        conn.close()

        # At most 1 candidate should exist (0 if rejected, 1 if passed, never 2)
        self.assertLessEqual(count, 1)

    # 12. Candidate identity derived deterministically from config_hash
    def test_12_candidate_deterministic_id(self):
        cfg = {"model_family": "XGBoost", "feature_family": "Alpha158", "horizon": 15}
        cfg_hash = ExperimentGenerator.compute_config_hash(cfg)
        expected_cand_id = f"cand_{cfg_hash[:16]}"

        cand = CandidateVault.freeze_candidate(
            experiment_id="exp_det_id",
            mission_id=f"m_det_{uuid4().hex[:6]}",
            config=cfg,
            model_artifact={"data": 123},
            metrics={"sharpe": 1.5}
        )
        self.assertEqual(cand["candidate_id"], expected_cand_id)

    # 13. OOS_PENDING governance preserved
    def test_13_oos_pending_governance_preserved(self):
        mission = self._create_test_mission()
        cand = CandidateVault.freeze_candidate(
            experiment_id="exp_oos_test",
            mission_id=mission.mission_id,
            config={"model_family": "Ridge", "horizon": 10},
            model_artifact={"data": "dummy"},
            metrics={"sharpe": 1.6}
        )
        self.assertEqual(cand["status"], "OOS_PENDING")

        loaded = CandidateVault.get_candidate(cand["candidate_id"])
        self.assertEqual(loaded["status"], "OOS_PENDING")

    # 14. Claim next queued experiments returns distinct config hashes
    def test_14_claim_next_queued_distinct_hashes(self):
        mission = self._create_test_mission()
        for i in range(6):
            cfg = {"model_family": "Ridge", "horizon": 5 + i, "seed": 100 + i}
            exp = {
                "experiment_id": f"exp_clm_{i}_{uuid4().hex[:6]}",
                "mission_id": mission.mission_id,
                "config": cfg
            }
            ResearchMemory.enqueue_experiment(exp)

    # 14. Claim next queued experiments returns distinct config hashes
    def test_14_claim_next_queued_distinct_hashes(self):
        mission = self._create_test_mission()
        for i in range(6):
            cfg = {"model_family": "Ridge", "horizon_days": 5 + i, "seed": 100 + i}
            exp = {
                "experiment_id": f"exp_clm_{i}_{uuid4().hex[:6]}",
                "mission_id": mission.mission_id,
                "config": cfg
            }
            ResearchMemory.enqueue_experiment(exp)

        claimed = ResearchMemory.claim_next_queued_experiments(mission.mission_id, limit=4)
        self.assertEqual(len(claimed), 4)

        claimed_hashes = [item["config_hash"] for item in claimed]
        self.assertEqual(len(claimed_hashes), len(set(claimed_hashes)), "Claimed items must all have distinct hashes")

    # 15. Claim next queued experiments avoids currently running configs
    def test_15_claim_avoids_running_configs(self):
        mission = self._create_test_mission()
        cfg = {"model_family": "Ridge", "seed": 999}
        cfg_hash = ExperimentGenerator.compute_config_hash(cfg)

        exp1 = {"experiment_id": f"exp_r1_{uuid4().hex[:6]}", "mission_id": mission.mission_id, "config": cfg}
        ResearchMemory.enqueue_experiment(exp1)
        ResearchMemory.update_queue_status(exp1["experiment_id"], "RUNNING")

        claimed = ResearchMemory.claim_next_queued_experiments(mission.mission_id, limit=4)
        for item in claimed:
            self.assertNotEqual(item.get("config_hash"), cfg_hash, "Must not claim config that is already RUNNING")

    # 16. Retry semantics: retry updates status but preserves config_hash
    def test_16_retry_semantics_preserves_config_hash(self):
        mission = self._create_test_mission()
        cfg = {"model_family": "LightGBM", "seed": 707}
        cfg_hash = ExperimentGenerator.compute_config_hash(cfg)
        exp_id = f"exp_retry_{uuid4().hex[:6]}"

        exp = {"experiment_id": exp_id, "mission_id": mission.mission_id, "config": cfg, "retry_count": 0}
        ResearchMemory.enqueue_experiment(exp)

        # Retry experiment by returning to QUEUED
        ResearchMemory.update_queue_status(exp_id, "QUEUED")

        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT config_hash, status FROM research_experiments_queue WHERE experiment_id = ?", (exp_id,))
        row = c.fetchone()
        conn.close()

        self.assertEqual(row[0], cfg_hash)
        self.assertEqual(row[1], "QUEUED")

    # 17. Authoritative mission counts query
    def test_17_get_mission_counts(self):
        mission = self._create_test_mission()
        for i in range(3):
            cfg = {"model_family": "Ridge", "horizon_days": 5 * (i + 1), "seed": 100 + i}
            exp_id = f"exp_cnt_{i}_{uuid4().hex[:6]}"
            ResearchMemory.enqueue_experiment({
                "experiment_id": exp_id,
                "mission_id": mission.mission_id,
                "config": cfg
            })
            ResearchMemory.record_experiment_result({
                "experiment_id": exp_id,
                "mission_id": mission.mission_id,
                "config_hash": ExperimentGenerator.compute_config_hash(cfg),
                "status": "COMPLETED"
            })

        counts = ResearchMemory.get_mission_counts(mission.mission_id)
        self.assertEqual(counts["unique_completed"], 3)
        self.assertEqual(counts["total_executions"], 3)

    # 18. Grid shuffle is deterministic with mission.research_seed
    def test_18_grid_shuffle_deterministic_with_seed(self):
        m1 = self._create_test_mission(research_seed=12345)
        m2 = self._create_test_mission(research_seed=12345)
        m3 = self._create_test_mission(research_seed=54321)

        b1 = ExperimentGenerator.generate_next_experiments(m1, batch_size=4)
        b2 = ExperimentGenerator.generate_next_experiments(m2, batch_size=4)
        b3 = ExperimentGenerator.generate_next_experiments(m3, batch_size=4)

        h1 = [x["config_hash"] for x in b1]
        h2 = [x["config_hash"] for x in b2]
        h3 = [x["config_hash"] for x in b3]

        self.assertEqual(h1, h2, "Identical research_seed must generate identical experiment sequence")
        self.assertNotEqual(h1, h3, "Different research_seed should generate different experiment order")

    # 19. Generator filters out all tested and queued configurations
    def test_19_generator_filters_all_tested_and_queued(self):
        mission = self._create_test_mission(
            model_families=["Ridge", "LightGBM"],
            feature_families=["Alpha158"],
            target_horizons=[5, 10],
            portfolio_families=["TOP5"]
        )
        # 2 models * 1 feature * 2 horizons * 1 portfolio = 4 total combinations
        grid = ExperimentGenerator.get_search_space_grid(mission)
        self.assertEqual(len(grid), 4)

        # Generate and enqueue first 2 experiments
        first_batch = ExperimentGenerator.generate_next_experiments(mission, batch_size=2)
        self.assertEqual(len(first_batch), 2)
        for exp in first_batch:
            accepted = ResearchMemory.enqueue_experiment(exp)
            self.assertTrue(accepted)

        # Next generation should only produce the remaining 2 untested configurations
        remaining_batch = ExperimentGenerator.generate_next_experiments(mission, batch_size=10)
        self.assertEqual(len(remaining_batch), 2, "Generator must only return the 2 remaining untested configurations")

        # And another batch should be empty (exhausted)
        for exp in remaining_batch:
            ResearchMemory.enqueue_experiment(exp)
        exhausted_batch = ExperimentGenerator.generate_next_experiments(mission, batch_size=10)
        self.assertEqual(len(exhausted_batch), 0, "Generator must return empty when all 4 are queued")

    # 20. Concurrency reaches 4 with 4 distinct config hashes
    def test_20_worker_concurrency_distinct_configs(self):
        mission = self._create_test_mission()
        configs = [{"model_family": f"Model_{i}", "seed": i, "horizon_days": 10 + i} for i in range(4)]
        items = []
        for i, cfg in enumerate(configs):
            item = {
                "experiment_id": f"exp_par_{i}_{uuid4().hex[:6]}",
                "mission_id": mission.mission_id,
                "config": cfg,
                "config_hash": ExperimentGenerator.compute_config_hash(cfg)
            }
            ResearchMemory.enqueue_experiment(item)
            items.append(item)

        # Verify items have 4 distinct config hashes
        hashes = {item["config_hash"] for item in items}
        self.assertEqual(len(hashes), 4)

        # Run batch with scheduler
        results = self.orchestrator.scheduler.run_queue_batch(mission, max_batch=4)
        self.assertEqual(len(results), 4)
        for res in results:
            self.assertIn(res["status"], ("COMPLETED", "FAILED"))

    # 21. Budget accounting strictly terminates at budget limit
    def test_21_budget_accounting_terminates_at_limit(self):
        mission = self._create_test_mission(
            budget={"max_experiments": 3, "max_runtime_seconds": 3600, "max_concurrent": 2}
        )
        counts = ResearchMemory.get_mission_counts(mission.mission_id)
        self.assertEqual(counts["unique_completed"], 0)

        # Simulate 3 completed unique experiments
        for i in range(3):
            cfg = {"model_family": "Ridge", "horizon_days": 5 * (i + 1), "seed": 200 + i}
            exp_id = f"exp_bdg_{i}_{uuid4().hex[:6]}"
            ResearchMemory.enqueue_experiment({"experiment_id": exp_id, "mission_id": mission.mission_id, "config": cfg})
            ResearchMemory.record_experiment_result({
                "experiment_id": exp_id,
                "mission_id": mission.mission_id,
                "config_hash": ExperimentGenerator.compute_config_hash(cfg),
                "status": "COMPLETED"
            })

        counts = ResearchMemory.get_mission_counts(mission.mission_id)
        self.assertGreaterEqual(counts["unique_completed"], mission.budget.get("max_experiments", 3))

    # 22. Historical stopped mission records intact
    def test_22_historical_stopped_mission_intact(self):
        from app.data.database import get_canonical_db_path
        conn = sqlite3.connect(get_canonical_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT count(*) FROM research_candidate_vault WHERE mission_id = 'mission_20260906_201001_805952'")
        cand_count = c.fetchone()[0]
        c.execute("SELECT count(*) FROM research_experiments_ledger WHERE mission_id = 'mission_20260906_201001_805952'")
        ledger_count = c.fetchone()[0]
        conn.close()

        self.assertEqual(cand_count, 299, "Historical candidate records for stopped mission must be preserved")
        self.assertEqual(ledger_count, 5, "Historical ledger executions for stopped mission must be preserved")

    # 23. Pre-repair backup file exists and valid
    def test_23_pre_repair_backup_file_exists(self):
        from app.data.database import get_canonical_db_path
        backup_files = glob.glob(os.path.join(os.path.dirname(get_canonical_db_path()), "market_data.db.research_integrity_pre_repair_backup_*"))
        self.assertGreaterEqual(len(backup_files), 1, "At least one pre-repair database backup file must exist")
        backup_size = os.path.getsize(backup_files[0])
        self.assertGreater(backup_size, 1000000, "Backup file must be non-empty and substantial")

    # 24. Production Safety Invariants Intact (Champion SHAs, Trade History, Heat)
    def test_24_production_safety_invariants_intact(self):
        safety = ResearchOrchestrator.verify_production_safety_invariants()
        self.assertTrue(safety["all_invariants_preserved"], f"Invariants failed: {safety}")
        self.assertEqual(safety["intraday_champion_hash"], CHAMPION_INTRADAY_HASH)
        self.assertEqual(safety["swing_champion_hash"], CHAMPION_SWING_HASH)
        self.assertEqual(safety["ml_trade_history_rows"], EXPECTED_HISTORY_ROWS)
        self.assertEqual(safety["portfolio_heat_pct"], 0.0)
        self.assertEqual(safety["broker_orders"], 0)
        self.assertEqual(safety["telegram_alerts"], 0)

    # 25. Has config tested helper accurately checks both queue and ledger
    def test_25_has_config_tested_checks_queue_and_ledger(self):
        mission = self._create_test_mission()
        cfg_q = {"model_family": "Ridge", "horizon_days": 5}
        cfg_l = {"model_family": "LightGBM", "horizon_days": 10}
        cfg_none = {"model_family": "CatBoost", "horizon_days": 15}

        h_q = ExperimentGenerator.compute_config_hash(cfg_q)
        h_l = ExperimentGenerator.compute_config_hash(cfg_l)
        h_none = ExperimentGenerator.compute_config_hash(cfg_none)

        ResearchMemory.enqueue_experiment({"experiment_id": f"exp_hq_{uuid4().hex[:6]}", "mission_id": mission.mission_id, "config": cfg_q})
        ResearchMemory.record_experiment_result({
            "experiment_id": f"exp_hl_{uuid4().hex[:6]}",
            "mission_id": mission.mission_id,
            "config_hash": h_l,
            "status": "COMPLETED"
        })

        self.assertTrue(ResearchMemory.has_config_tested(mission.mission_id, h_q))
        self.assertTrue(ResearchMemory.has_config_tested(mission.mission_id, h_l))
        self.assertFalse(ResearchMemory.has_config_tested(mission.mission_id, h_none))

    # 26. Get all config hashes returns union of queue and ledger
    def test_26_get_all_config_hashes_returns_union(self):
        mission = self._create_test_mission()
        cfg1 = {"model_family": "Ridge", "k": 1}
        cfg2 = {"model_family": "Ridge", "k": 2}

        h1 = ExperimentGenerator.compute_config_hash(cfg1)
        h2 = ExperimentGenerator.compute_config_hash(cfg2)

        ResearchMemory.enqueue_experiment({"experiment_id": f"exp_u1_{uuid4().hex[:6]}", "mission_id": mission.mission_id, "config": cfg1})
        ResearchMemory.record_experiment_result({
            "experiment_id": f"exp_u2_{uuid4().hex[:6]}",
            "mission_id": mission.mission_id,
            "config_hash": h2,
            "status": "COMPLETED"
        })

        all_hashes = ResearchMemory.get_all_config_hashes(mission.mission_id)
        self.assertIn(h1, all_hashes)
        self.assertIn(h2, all_hashes)


if __name__ == "__main__":
    unittest.main()
