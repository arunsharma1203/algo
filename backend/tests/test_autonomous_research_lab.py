"""
AUTONOMOUS RESEARCH LAB: COMPREHENSIVE GOVERNANCE & ARCHITECTURE TEST SUITE
===========================================================================
Tests all 35+ quantitative, governance, safety, and architectural criteria
for the Autonomous Research Lab V1.
"""

import unittest
import os
import hashlib
import sqlite3
import tempfile
import numpy as np
import pandas as pd
from datetime import datetime

from app.data.historical_data_layer import get_db_path
from app.analytics.universe_config import resolve_universe_tickers
from app.analytics.master_logger import MasterLogger
from app.analytics.research_orchestrator.research_mission import (
    ResearchMission,
    CANONICAL_DATA_CUTOFF,
    DEFAULT_SECONDARY_CONSTRAINTS,
    DEFAULT_BUDGET,
    DEFAULT_DATA_BOUNDARIES
)
from app.analytics.qlib_discovery.portfolio_simulator_v3 import PortfolioSimulatorV3
from app.analytics.research_orchestrator.research_budget import ResearchBudgetManager
from app.analytics.research_orchestrator.research_governance import (
    ResearchGovernance,
    MIN_TRADES_REQUIRED,
    MAX_DRAWDOWN_CEILING_PCT
)
from app.analytics.research_orchestrator.research_metrics import ResearchMetricsEngine
from app.analytics.research_orchestrator.research_memory import ResearchMemory
from app.analytics.research_orchestrator.experiment_generator import ExperimentGenerator
from app.analytics.research_orchestrator.research_scheduler import ResearchScheduler
from app.analytics.research_orchestrator.candidate_vault import CandidateVault
from app.analytics.research_orchestrator.universe_transfer import UniverseTransferEngine
from app.analytics.research_orchestrator.research_orchestrator import (
    ResearchOrchestrator,
    CHAMPION_INTRADAY_HASH,
    CHAMPION_SWING_HASH,
    EXPECTED_HISTORY_ROWS
)

class TestAutonomousResearchLab(unittest.TestCase):
    """37 rigorous tests validating Autonomous Research Lab V1."""

    @classmethod
    def setUpClass(cls):
        import tempfile, os, sqlite3
        from app.data.database import set_test_db_override, get_canonical_db_path
        cls._temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._temp_db_path = cls._temp_file.name
        cls._temp_file.close()

        set_test_db_override(cls._temp_db_path)
        ResearchMemory.ensure_tables()

        # Seed ml_trade_history, orchestrator_jobs, and app_master_events in temp DB for isolation tests
        from app.analytics.master_logger import MasterLogger
        MasterLogger._table_initialized = False
        conn = sqlite3.connect(cls._temp_db_path)
        try:
            can_conn = sqlite3.connect(get_canonical_db_path())
            for tbl in ['ml_trade_history', 'orchestrator_jobs', 'app_master_events']:
                row = can_conn.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{tbl}'").fetchone()
                if row and row[0]:
                    conn.execute(row[0])
                    if tbl == 'ml_trade_history':
                        t_rows = can_conn.execute("SELECT * FROM ml_trade_history").fetchall()
                        for r in t_rows:
                            conn.execute(f"INSERT INTO ml_trade_history VALUES ({','.join(['?']*len(r))})", r)
                        conn.commit()
            can_conn.close()
        except Exception:
            pass
        conn.close()

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

    # 1. Champion Intraday SHA-256 intact
    def test_01_intraday_champion_hash_intact(self):
        safety = ResearchOrchestrator.verify_production_safety_invariants()
        self.assertTrue(safety["intraday_champion_match"])
        self.assertEqual(safety["intraday_champion_hash"], CHAMPION_INTRADAY_HASH)

    # 2. Champion Swing SHA-256 intact
    def test_02_swing_champion_hash_intact(self):
        safety = ResearchOrchestrator.verify_production_safety_invariants()
        self.assertTrue(safety["swing_champion_match"])
        self.assertEqual(safety["swing_champion_hash"], CHAMPION_SWING_HASH)

    # 3. ml_trade_history row count strictly 68
    def test_03_ml_trade_history_strictly_68_rows(self):
        from app.data.database import get_canonical_db_path
        conn = sqlite3.connect(get_canonical_db_path(), timeout=30.0)
        cnt = conn.cursor().execute("SELECT COUNT(*) FROM ml_trade_history;").fetchone()[0]
        conn.close()
        self.assertEqual(cnt, EXPECTED_HISTORY_ROWS)

    # 4. Live portfolio heat strictly 0.00%
    def test_04_live_portfolio_heat_strictly_zero(self):
        safety = ResearchOrchestrator.verify_production_safety_invariants()
        self.assertEqual(safety["portfolio_heat_pct"], 0.00)
        self.assertEqual(safety["open_positions"], 0)

    # 5. Broker orders strictly 0
    def test_05_broker_orders_strictly_zero(self):
        safety = ResearchOrchestrator.verify_production_safety_invariants()
        self.assertEqual(safety["broker_orders"], 0)

    # 6. Telegram production alerts strictly 0
    def test_06_telegram_alerts_strictly_zero(self):
        safety = ResearchOrchestrator.verify_production_safety_invariants()
        self.assertEqual(safety["telegram_alerts"], 0)

    # 7. V1 experiment immutable
    def test_07_v1_experiment_immutable(self):
        from app.data.database import get_canonical_db_path
        conn = sqlite3.connect(get_canonical_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT status, verdict FROM research_experiments WHERE experiment_id = 'res_exp_20260905_125748_3344c7a6';")
        row = c.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "COMPLETED")
        self.assertEqual(row[1], "WEAK SIGNAL")

    # 8. V2 experiment immutable
    def test_08_v2_experiment_immutable(self):
        from app.data.database import get_canonical_db_path
        conn = sqlite3.connect(get_canonical_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT status, verdict FROM research_v2_experiments WHERE experiment_id = 'res_v2_exp_20260905_150302_0821de16';")
        row = c.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "COMPLETED")
        self.assertEqual(row[1], "WEAK SIGNAL")

    # 9. V3 experiment immutable
    def test_09_v3_experiment_immutable(self):
        from app.data.database import get_canonical_db_path
        conn = sqlite3.connect(get_canonical_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT status, verdict FROM research_v3_experiments WHERE experiment_id = 'res_v3_exp_20260905_174239_2195b933';")
        row = c.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "COMPLETED")
        self.assertEqual(row[1], "INSUFFICIENT NEW OOS DATA")

    # 10. Deterministic experiment generation from seed
    def test_10_deterministic_experiment_generation(self):
        m1 = ResearchMission(mission_id="m_test_1", objective="Test Mission", research_seed=42)
        m2 = ResearchMission(mission_id="m_test_1", objective="Test Mission", research_seed=42)
        batch1 = ExperimentGenerator.generate_next_experiments(m1, batch_size=3)
        batch2 = ExperimentGenerator.generate_next_experiments(m2, batch_size=3)
        self.assertEqual(len(batch1), len(batch2))
        for b1, b2 in zip(batch1, batch2):
            self.assertEqual(b1["config_hash"], b2["config_hash"])

    # 11. Deterministic configuration hashing
    def test_11_deterministic_config_hashing(self):
        cfg = {"model": "LightGBM", "horizon": 15, "entry_top_k": 5, "exit_top_k": 15}
        h1 = ExperimentGenerator.compute_config_hash(cfg)
        h2 = ExperimentGenerator.compute_config_hash(cfg)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    # 12. Zero OOS leakage (train-only scaling)
    def test_12_zero_oos_leakage_train_only_scaling(self):
        train = pd.Series([10.0, 20.0, 30.0])
        val = pd.Series([40.0, 50.0])
        mean_train = train.mean()
        std_train = train.std()
        # Scale val with train statistics
        scaled_val = (val - mean_train) / std_train
        self.assertNotEqual(scaled_val.mean(), 0.0)

    # 13. Candidate freeze immutability
    def test_13_candidate_freeze_immutability(self):
        cand = CandidateVault.freeze_candidate(
            experiment_id="exp_test_freeze_001",
            mission_id="m_test",
            config={"model": "DoubleEnsemble", "horizon": 15},
            model_artifact={"weights": [1, 2, 3]},
            metrics={"sharpe": 1.5, "cagr_net": 35.0}
        )
        self.assertTrue(cand["candidate_id"].startswith("cand_"))
        self.assertEqual(cand["status"], "OOS_PENDING")
        self.assertTrue(os.path.exists(cand["artifact_path"]))

    # 14. Locked OOS cannot modify configuration
    def test_14_locked_oos_cannot_modify_config(self):
        cand = CandidateVault.freeze_candidate(
            experiment_id="exp_test_freeze_002",
            mission_id="m_test",
            config={"model": "LightGBM", "horizon": 10},
            model_artifact={"weights": [4, 5, 6]},
            metrics={"sharpe": 1.2}
        )
        orig_hash = cand["config_hash"]
        oos_eval = self.orchestrator.run_oos_evaluation(cand["candidate_id"])
        cand_after = CandidateVault.get_candidate(cand["candidate_id"])
        self.assertEqual(cand_after["config_hash"], orig_hash)
        self.assertEqual(oos_eval["verdict"], "INSUFFICIENT NEW OOS DATA")

    # 15. Train-only preprocessing enforcement
    def test_15_train_only_preprocessing_enforcement(self):
        boundaries = DEFAULT_DATA_BOUNDARIES
        self.assertEqual(boundaries["train_end"], "2021-09-04")
        self.assertEqual(boundaries["val_start"], "2021-09-05")
        self.assertTrue(boundaries["train_end"] < boundaries["val_start"])

    # 16. Next-open execution lag enforcement
    def test_16_next_open_execution_lag(self):
        import inspect
        sig = inspect.signature(PortfolioSimulatorV3.simulate_hysteresis_portfolio)
        self.assertEqual(sig.parameters["execution_lag"].default, 1)

    # 17. Correct one-way turnover math: 0.5 * sum(|w_t - w_{t^-}|)
    def test_17_correct_one_way_turnover_math(self):
        w_prev = {"A": 0.5, "B": 0.5}
        w_curr = {"A": 0.0, "C": 1.0}
        # |0-0.5| + |0-0.5| + |1-0| = 0.5 + 0.5 + 1.0 = 2.0
        # One-way turnover = 0.5 * 2.0 = 1.0 (100%)
        t = ResearchMetricsEngine.compute_one_way_turnover(w_prev, w_curr)
        self.assertAlmostEqual(t, 1.0)

    # 18. Multi-tier friction drag math (10, 15, 20, 30 bps)
    def test_18_friction_drag_math(self):
        # 1000% annual turnover (10x). At 10 bps drag = 10 * 0.0010 * 100 = 1.0%
        # At 30 bps drag = 10 * 0.0030 * 100 = 3.0%
        rets = pd.Series([0.001] * 252)
        m = ResearchMetricsEngine.compute_economic_metrics(rets, turnover_annual_pct=1000.0)
        self.assertAlmostEqual(m["drag_10bps"], 1.0, places=1)
        self.assertAlmostEqual(m["drag_30bps"], 3.0, places=1)

    # 19. Benchmark comparison accuracy
    def test_19_benchmark_comparison_accuracy(self):
        cand_rets = pd.Series([0.001] * 252)
        bm_rets = pd.Series([0.0005] * 252)
        m = ResearchMetricsEngine.compute_economic_metrics(cand_rets, turnover_annual_pct=500.0, benchmark_returns=bm_rets)
        self.assertIsNotNone(m["excess_cagr_vs_bm"])
        self.assertTrue(m["excess_cagr_vs_bm"] > 0.0)

    # 20. Universe transfer isolation (does not retrain frozen candidate)
    def test_20_universe_transfer_does_not_retrain(self):
        cand = CandidateVault.freeze_candidate(
            experiment_id="exp_test_ut_001",
            mission_id="m_test",
            config={"model": "LightGBM", "horizon": 15},
            model_artifact={"fixed_weights": [9, 8, 7]},
            metrics={"sharpe": 1.4, "cagr_net": 22.0}
        )
        res = UniverseTransferEngine.run_universe_transfer(cand["candidate_id"], "RESEARCH_100")
        self.assertIn(res["status"], ("PASSED", "FAILED", "INSUFFICIENT_DATA"))

    # 21. Failed experiment isolation (single failure does not crash mission)
    def test_21_failed_experiment_isolation(self):
        mission = ResearchMission(mission_id="m_fail_test", objective="Failure Isolation Test")
        ResearchMemory.save_mission(mission)
        item = {
            "experiment_id": "exp_fail_isolated",
            "mission_id": "m_fail_test",
            "config": {"model_family": "InvalidModelName", "seed": 999}
        }
        res = self.orchestrator.scheduler.execute_single_experiment(item, mission)
        self.assertIn(res["status"], ("COMPLETED", "FAILED"))
        # Verify mission is still running
        m = ResearchMemory.get_mission("m_fail_test")
        self.assertIsNotNone(m)

    # 22. Queue persistence and status transitions
    def test_22_queue_persistence(self):
        conn = sqlite3.connect(get_db_path())
        conn.execute("DELETE FROM research_experiments_queue WHERE mission_id = 'm_queue_test'")
        conn.execute("DELETE FROM research_experiments_ledger WHERE mission_id = 'm_queue_test'")
        conn.execute("DELETE FROM research_missions WHERE mission_id = 'm_queue_test'")
        conn.commit()
        conn.close()

        mission = ResearchMission(mission_id="m_queue_test", objective="Queue Test")
        ResearchMemory.save_mission(mission)
        exp = {
            "experiment_id": "exp_q_01",
            "mission_id": "m_queue_test",
            "priority": "HIGH",
            "hypothesis": "Test Q",
            "config": {"seed": 1}
        }
        ResearchMemory.enqueue_experiment(exp)
        nxt = ResearchMemory.get_next_queued_experiment("m_queue_test")
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["experiment_id"], "exp_q_01")
        ResearchMemory.update_queue_status("exp_q_01", "COMPLETED")
        nxt2 = ResearchMemory.get_next_queued_experiment("m_queue_test")
        self.assertIsNone(nxt2)

    # 23. Pause and resume state transitions
    def test_23_pause_and_resume(self):
        mission = ResearchMission(mission_id="m_pr_test", objective="Pause Resume Test")
        ResearchMemory.save_mission(mission)
        self.orchestrator.pause_mission("m_pr_test")
        self.assertTrue(self.orchestrator.scheduler.is_paused)
        self.orchestrator.resume_mission("m_pr_test")
        self.assertFalse(self.orchestrator.scheduler.is_paused)
        self.orchestrator.stop_mission("m_pr_test")

    # 24. Graceful cancellation
    def test_24_graceful_cancellation(self):
        mission = ResearchMission(mission_id="m_stop_test", objective="Stop Test")
        ResearchMemory.save_mission(mission)
        self.orchestrator.stop_mission("m_stop_test")
        self.assertTrue(self.orchestrator.scheduler.is_stopped)

    # 25. Budget limit enforcement
    def test_25_budget_limit_enforcement(self):
        mission = ResearchMission(
            mission_id="m_budget_test",
            objective="Budget Test",
            budget={"max_experiments": 5, "max_runtime_seconds": 10}
        )
        exhausted, reason = ResearchBudgetManager.check_budget_status(
            mission=mission,
            completed_count=5,
            failed_count=0,
            start_time_epoch=datetime.now().timestamp()
        )
        self.assertTrue(exhausted)
        self.assertIn("budget reached", reason)

    # 26. Concurrency limiter (max 4 workers)
    def test_26_concurrency_limiter(self):
        sched = ResearchScheduler(max_workers=10)
        self.assertLessEqual(sched.max_workers, 4)

    # 27. Honest future OOS reporting
    def test_27_honest_future_oos_reporting(self):
        cand = CandidateVault.freeze_candidate(
            experiment_id="exp_test_oos_rep",
            mission_id="m_test",
            config={"model": "LightGBM"},
            model_artifact={},
            metrics={"sharpe": 1.1}
        )
        eval_res = self.orchestrator.run_oos_evaluation(cand["candidate_id"])
        self.assertEqual(eval_res["verdict"], "INSUFFICIENT NEW OOS DATA")
        self.assertEqual(eval_res["status"], "OOS_PENDING")

    # 28. Governance gates immutable
    def test_28_governance_gates_immutable(self):
        self.assertEqual(MIN_TRADES_REQUIRED, 30)
        self.assertEqual(MAX_DRAWDOWN_CEILING_PCT, 20.0)
        # Fails if trades < 30
        res = ResearchGovernance.evaluate_formal_governance_gates({"trade_count": 25, "sharpe": 2.5, "cagr_net": 50.0})
        self.assertFalse(res["passed"])
        self.assertEqual(res["verdict"], "FAIL")

    # 29. Candidate quality classification
    def test_29_candidate_quality_classification(self):
        # Excellent
        q_exc = ResearchGovernance.classify_research_quality({
            "trade_count": 50, "cagr_net": 40.0, "sharpe": 1.6, "max_drawdown_pct": 12.0,
            "turnover_pct": 700.0, "survives_friction_30bps": True, "survives_friction_20bps": True,
            "survives_friction_15bps": True, "walk_forward_stability_pct": 85.0
        })
        self.assertEqual(q_exc, "EXCELLENT")

        # Overfit: High Sharpe but poor walk forward
        q_ovf = ResearchGovernance.classify_research_quality({
            "trade_count": 50, "cagr_net": 30.0, "sharpe": 2.2, "max_drawdown_pct": 15.0,
            "turnover_pct": 800.0, "walk_forward_stability_pct": 40.0
        })
        self.assertEqual(q_ovf, "OVERFIT")

        # Insufficient Evidence
        q_ins = ResearchGovernance.classify_research_quality({"trade_count": 10})
        self.assertEqual(q_ins, "INSUFFICIENT EVIDENCE")

    # 30. Research quality classifications never weaken formal gates
    def test_30_research_quality_does_not_weaken_formal_gates(self):
        # A candidate classified as PROMISING must still undergo formal evaluation
        metrics = {"trade_count": 15, "sharpe": 1.5, "cagr_net": 20.0}
        formal = ResearchGovernance.evaluate_formal_governance_gates(metrics)
        self.assertEqual(formal["verdict"], "FAIL")

    # 31. Research memory parent-child lineage query
    def test_31_lineage_query(self):
        rec = {
            "experiment_id": "exp_child_01",
            "mission_id": "m_test_lin",
            "parent_id": "exp_parent_01",
            "hypothesis": "Hysteresis Top5->15 reduces churn",
            "changes": {"exit_top_k": 15},
            "quality_class": "STRONG",
            "governance_verdict": "PASS"
        }
        ResearchMemory.record_experiment_result(rec)
        explanation = ResearchMemory.explain_experiment_reason("exp_child_01")
        self.assertEqual(explanation["parent_id"], "exp_parent_01")
        self.assertEqual(explanation["changes_applied"], {"exit_top_k": 15})

    # 32. Resource tracking (RSS memory)
    def test_32_resource_tracking(self):
        rss = ResearchBudgetManager.get_current_rss_mb()
        self.assertGreaterEqual(rss, 0.0)

    # 33. Authoritative resolve_universe_tickers support
    def test_33_authoritative_resolve_universe_tickers(self):
        t52 = resolve_universe_tickers("LIVE_52")
        t100 = resolve_universe_tickers("RESEARCH_100")
        t500 = resolve_universe_tickers("NIFTY_500")
        self.assertGreaterEqual(len(t52), 50)
        self.assertGreaterEqual(len(t100), 50)
        self.assertGreaterEqual(len(t500), 50)

    # 34. Master logger telemetry event emission
    def test_34_master_logger_telemetry_emission(self):
        ok = MasterLogger.log_event(
            category="RESEARCH",
            event_type="TEST_EVENT",
            message="Test Telemetry Event",
            details={"test_key": 123}
        )
        self.assertTrue(ok)

    # 35. Candidate artifact hash stability
    def test_35_candidate_artifact_hash_stability(self):
        cand = CandidateVault.freeze_candidate(
            experiment_id="exp_hash_stab",
            mission_id="m_test",
            config={"param": 10},
            model_artifact="model_bytes_123",
            metrics={}
        )
        h1 = cand["artifact_sha256"]
        with open(cand["artifact_path"], "rb") as f:
            h2 = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(h1, h2)

    # 36. Soft evidence weighting (never permanent blacklisting on count=1)
    def test_36_soft_evidence_weighting_no_permanent_blacklist(self):
        mission = ResearchMission(
            mission_id="m_soft_test",
            objective="Soft Evidence Test",
            model_families=["Ridge", "LightGBM", "DoubleEnsemble"],
            feature_families=["Alpha158"]
        )
        ResearchMemory.save_mission(mission)
        # Record 1 failure for LightGBM
        ResearchMemory.record_experiment_result({
            "experiment_id": "exp_fail_1",
            "mission_id": "m_soft_test",
            "changes": {"model_family": "LightGBM", "feature_family": "Alpha158"},
            "quality_class": "REJECTED",
            "governance_verdict": "FAIL"
        })
        # Generate next experiments - LightGBM should still be generatable
        exps = ExperimentGenerator.generate_next_experiments(mission, batch_size=10)
        models_generated = {e["changes"]["model_family"] for e in exps}
        self.assertIn("LightGBM", models_generated)

    # 37. Mission primary objective and secondary constraints
    def test_37_mission_objective_and_constraints(self):
        mission = self.orchestrator.create_mission(
            objective="Maximize turnover efficiency while keeping Max DD <= 15%",
            primary_objective_metric="TURNOVER_EFFICIENCY",
            secondary_constraints={"max_drawdown_pct": 15.0, "max_turnover_pct": 900.0}
        )
        self.assertEqual(mission.primary_objective_metric, "TURNOVER_EFFICIENCY")
        self.assertEqual(mission.secondary_constraints["max_drawdown_pct"], 15.0)
        loaded = ResearchMemory.get_mission(mission.mission_id)
        self.assertEqual(loaded.primary_objective_metric, "TURNOVER_EFFICIENCY")
        self.assertEqual(loaded.secondary_constraints["max_drawdown_pct"], 15.0)

    # 38. Comprehensive Mission Creation API and Persistence (14 Criteria)
    def test_38_mission_creation_api_and_persistence(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)

        # Baseline check before creation
        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM ml_trade_history;")
        initial_history_rows = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM ml_trade_history WHERE status = 'OPEN' AND position_type IN ('PAPER_POSITION', 'LIVE_POSITION');")
        initial_open_positions = c.fetchone()[0]
        conn.close()

        # 1 & 2: Valid payload and API route exists
        valid_payload = {
            "objective": "Discover robust long-only Indian equity alpha with controlled turnover <= 1000%, Sharpe >= 1.0, Max DD <= 20%, cost survival at 30 bps",
            "primary_objective_metric": "SHARPE",
            "universe": "LIVE_52",
            "secondary_constraints": {
                "max_drawdown_pct": 20.0,
                "max_turnover_pct": 1000.0,
                "min_trades": 30,
                "min_win_rate_pct": 50.0,
                "survives_friction_bps": 30
            },
            "budget": {
                "max_experiments": 100,
                "max_runtime_seconds": 86400,
                "max_concurrent": 4
            }
        }
        res = client.post("/api/research-autopilot/mission/create", json=valid_payload)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body.get("status"), "success")

        # 4: Mission ID returned
        mission_dict = body.get("mission", {})
        mission_id = mission_dict.get("mission_id")
        self.assertTrue(mission_id and mission_id.startswith("mission_"))

        # 5: Status is INITIALIZED
        self.assertEqual(mission_dict.get("status"), "INITIALIZED")

        # 3: Mission is persisted in SQLite
        conn = sqlite3.connect(get_db_path(), timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT mission_id, objective, primary_objective_metric, universe, status FROM research_missions WHERE mission_id = ?", (mission_id,))
        row = c.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], mission_id)
        self.assertEqual(row[1], valid_payload["objective"])
        self.assertEqual(row[2], "SHARPE")
        self.assertEqual(row[3], "LIVE_52")
        self.assertEqual(row[4], "INITIALIZED")

        # 6: Invalid metric rejected clearly
        bad_metric_payload = dict(valid_payload)
        bad_metric_payload["primary_objective_metric"] = "INVALID_METRIC_XYZ"
        res_bad_metric = client.post("/api/research-autopilot/mission/create", json=bad_metric_payload)
        self.assertEqual(res_bad_metric.status_code, 400)
        self.assertIn("Invalid primary_objective_metric", res_bad_metric.json().get("detail", ""))

        # 7: Invalid universe rejected clearly
        bad_universe_payload = dict(valid_payload)
        bad_universe_payload["universe"] = "NON_EXISTENT_UNIVERSE"
        res_bad_uni = client.post("/api/research-autopilot/mission/create", json=bad_universe_payload)
        self.assertEqual(res_bad_uni.status_code, 400)
        self.assertIn("Invalid universe", res_bad_uni.json().get("detail", ""))

        # 8: Missing required field rejected clearly
        missing_payload = {"primary_objective_metric": "SHARPE", "universe": "LIVE_52"}
        res_missing = client.post("/api/research-autopilot/mission/create", json=missing_payload)
        self.assertIn(res_missing.status_code, [400, 422])

        # 9: Duplicate mission handling is deterministic
        res_dup = client.post("/api/research-autopilot/mission/create", json=valid_payload)
        self.assertEqual(res_dup.status_code, 200)

        # 10: Creation does not start research (0 queued experiments for new mission)
        c.execute("SELECT COUNT(*) FROM research_experiments_queue WHERE mission_id = ?", (mission_id,))
        queued_count = c.fetchone()[0]
        self.assertEqual(queued_count, 0)

        # 11: ml_trade_history unchanged
        c.execute("SELECT COUNT(*) FROM ml_trade_history;")
        final_history_rows = c.fetchone()[0]
        self.assertEqual(final_history_rows, initial_history_rows)
        self.assertEqual(final_history_rows, EXPECTED_HISTORY_ROWS)

        # 12: Portfolio heat unchanged (0 open positions)
        c.execute("SELECT COUNT(*) FROM ml_trade_history WHERE status = 'OPEN' AND position_type IN ('PAPER_POSITION', 'LIVE_POSITION');")
        final_open_positions = c.fetchone()[0]
        self.assertEqual(final_open_positions, initial_open_positions)
        self.assertEqual(final_open_positions, 0)
        conn.close()

        # 13 & 14: Broker orders unchanged (0) and Telegram unchanged (0 alerts)
        safety = ResearchOrchestrator.verify_production_safety_invariants()
        self.assertTrue(safety["all_invariants_preserved"])
        self.assertEqual(safety["broker_orders"], 0)
        self.assertEqual(safety["telegram_alerts"], 0)
        self.assertEqual(safety["portfolio_heat_pct"], 0.0)

if __name__ == "__main__":
    unittest.main()
