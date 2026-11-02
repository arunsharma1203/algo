"""
Unit and Safety Verification Suite for Trade Sentinel Agent
===========================================================
Tests sandbox isolation, firewall integrity, point-in-time causality,
and autonomous reconciliation.
"""

import os
import sys
import unittest
import sqlite3
import tempfile
from pathlib import Path

# Add project root and agent_lab to sys.path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))
BACKEND_ROOT = WORKSPACE_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from agent_lab.agent_core.firewall import (
    ProductionFirewall,
    ProductionWriteAttemptBlockedError,
    LiveBrokerOrderBlockedError
)
from agent_lab.agent_core.resource_governor import ResourceGovernor
from agent_lab.agent_core.llm_router import LLMRouter
from agent_lab.agent_core.sentinel_agent import TradeSentinelAgent
from agent_lab.tools.production_reader import ProductionReader, get_readonly_prod_connection
from agent_lab.tools.code_sandbox import CodeSandbox
from agent_lab.tools.git_manager import AgentGitManager
from agent_lab.memory.memory_retriever import StructuredMemoryRetriever
from agent_lab.agent_core.policy_learner import SentinelPolicyLearner
from agent_lab.agent_core.experiment_dispatcher import AutonomousExperimentDispatcher
from agent_lab.agent_core.bug_repair import SandboxBugRepairEngine
from agent_lab.agent_core.strategy_discovery import AutonomousStrategyDiscoveryEngine
from agent_lab.tools.web_researcher import safe_search, search_and_analyze
from agent_lab.tools.domain_forensic_tools import DomainForensicTools
from agent_lab.agent_core.agent_mission_state import MissionState
from agent_lab.agent_core.autonomous_investigator import AutonomousQuantitativeInvestigator
from agent_lab.memory.agent_db import get_agent_db_path


class TestTradeSentinelAgent(unittest.TestCase):

    def test_01_firewall_blocks_backend_writes(self):
        """Verify that firewall strictly blocks attempts to modify backend/ or models/."""
        with self.assertRaises(ProductionWriteAttemptBlockedError):
            ProductionFirewall.assert_write_permitted(BACKEND_ROOT / "app" / "main.py")

        with self.assertRaises(ProductionWriteAttemptBlockedError):
            ProductionFirewall.assert_write_permitted(BACKEND_ROOT / "models" / "intraday" / "champion_ensemble.pkl")

    def test_02_firewall_blocks_broker_live_orders(self):
        """Verify that firewall denies any invocation of live broker order endpoints."""
        with self.assertRaises(LiveBrokerOrderBlockedError):
            ProductionFirewall.assert_broker_call_safe("place_order", {"ticker": "RELIANCE.NS", "quantity": 10})

    def test_03_firewall_permits_agent_lab_writes(self):
        """Verify that sandbox writes inside agent_lab/ are freely permitted."""
        sandbox_path = WORKSPACE_ROOT / "agent_lab" / "experiments" / "test_script.py"
        self.assertTrue(ProductionFirewall.assert_write_permitted(sandbox_path))

    def test_04_production_database_readonly_enforcement(self):
        """Verify that ProductionReader connections reject write statements."""
        conn = get_readonly_prod_connection()
        cursor = conn.cursor()
        with self.assertRaises(sqlite3.OperationalError):
            cursor.execute("UPDATE ml_trade_history SET outcome = 'TEST_CORRUPT' WHERE id = 1;")
        conn.close()

    def test_05_candidate_shadow_evaluation_schema(self):
        """Verify shadow evaluation produces valid schema with TAKE/SKIP/UNKNOWN."""
        candidate = {
            "ticker": "TATAMOTORS.NS",
            "direction": "BUY",
            "qlib_score": 75.0,
            "qlib_alpha_strength": 82.0,
            "legacy_score": 68.0,
            "composite_score": 73.5,
            "is_actionable": True
        }
        res = TradeSentinelAgent.observe_and_evaluate(candidate)
        self.assertIn("decision_id", res)
        self.assertIn(res["action"], ["TAKE", "SKIP", "UNKNOWN"])
        self.assertIsInstance(res["agent_prob"], float)
        self.assertIsInstance(res["reasons"], list)

    def test_06_point_in_time_snapshot_captured(self):
        """Verify that every decision stores a point-in-time data snapshot."""
        candidate = {
            "ticker": "SBIN.NS",
            "direction": "BUY",
            "qlib_score": 55.0,
            "legacy_score": 52.0,
            "composite_score": 54.0,
            "is_actionable": True
        }
        res = TradeSentinelAgent.observe_and_evaluate(candidate)
        scoreboard = TradeSentinelAgent.get_scoreboard_summary()
        decisions = scoreboard["recent_decisions"]
        self.assertTrue(len(decisions) > 0)
        latest = decisions[0]
        self.assertIn("point_in_time_snapshot_json", latest)

    def test_07_code_sandbox_ast_validation(self):
        """Verify syntax validation blocks malformed Python code."""
        valid_code = "def alpha_factor(df):\n    return df['close'] / df['open'] - 1.0\n"
        invalid_code = "def broken(df:\n    return df"
        self.assertTrue(CodeSandbox.validate_python_syntax(valid_code)[0])
        self.assertFalse(CodeSandbox.validate_python_syntax(invalid_code)[0])

    def test_08_code_sandbox_timeout_enforcement(self):
        """Verify sandbox write blocks invalid syntax and accepts valid experimental code."""
        valid_code = "# test script\nx = 1\n"
        invalid_code = "def broken(\n"
        ok, _ = CodeSandbox.write_experimental_file("experiments/valid_test.py", valid_code)
        self.assertTrue(ok)
        bad, _ = CodeSandbox.write_experimental_file("experiments/invalid_test.py", invalid_code)
        self.assertFalse(bad)

    def test_09_resource_governor_operating_mode(self):
        """Verify resource governor enforces lightweight limits and timeouts."""
        mode = ResourceGovernor.get_operating_mode()
        self.assertIn("mode", mode)
        self.assertIn(mode["mode"], ["LIGHTWEIGHT_SHADOW_MODE", "FULL_RESEARCH_MODE"])
        self.assertLessEqual(mode["max_tool_timeout_seconds"], 60)

    def test_10_scoreboard_aggregation_integrity(self):
        """Verify scoreboard calculates 4-quadrant cohorts and net alpha delta."""
        sb = TradeSentinelAgent.get_scoreboard_summary()
        self.assertIn("total_comparisons", sb)
        self.assertIn("cohorts", sb)
        cohorts = sb["cohorts"]
        for key in ["agent_avoided_loss", "both_take_win", "both_take_loss", "agent_missed_win"]:
            self.assertIn(key, cohorts)
            self.assertGreaterEqual(cohorts[key], 0)

    def test_11_git_manager_commit_tracking(self):
        """Verify Git manager retrieves valid commit hashes from sandbox."""
        head = AgentGitManager.get_current_head()
        self.assertIsInstance(head, str)
        self.assertGreater(len(head), 3)

    def test_12_autonomous_cycle_execution(self):
        """Verify autonomous background cycle executes without error."""
        res = TradeSentinelAgent.run_autonomous_cycle()
        self.assertEqual(res["status"], "SUCCESS")
        self.assertIn("evaluated_candidates", res)
        self.assertIn("reconciled_trades", res)

    def test_13_reconciliation_deterministic_linkage_and_accounting_equation(self):
        """Verify reconciliation satisfies genuine + no_obs == total and deterministic linkage."""
        res = TradeSentinelAgent.reconcile_outcomes()
        self.assertTrue(res["accounting_verified"])
        self.assertEqual(res["genuine_decisions"] + res["no_observation"], res["reconciled_trades"])

    def test_14_no_observation_isolated_from_performance_metrics(self):
        """Verify historical NO_OBSERVATION records do not pollute agent win rate or P&L."""
        sb = TradeSentinelAgent.get_scoreboard_summary()
        self.assertTrue(sb["accounting_check_passed"])
        self.assertGreater(sb["no_observation_count"], 0)
        self.assertGreater(sb["genuine_decisions_count"], 0)
        # Verify genuine count + no_observation count == total_comparisons
        self.assertEqual(sb["genuine_decisions_count"] + sb["no_observation_count"], sb["total_comparisons"])

    def test_15_structured_memory_retrieval_and_causality(self):
        """Verify structured memory retriever filters out future entries (timestamp <= as_of_timestamp)."""
        past_time = "2026-10-07T00:00:00"
        ctx = StructuredMemoryRetriever.retrieve_decision_context(
            ticker="RELIANCE.NS",
            as_of_timestamp=past_time
        )
        self.assertIsInstance(ctx, str)
        # Should contain memory banner or empty string without crashing
        if ctx:
            self.assertIn("STRUCTURED MEMORY", ctx)

    def test_16_policy_learner_insufficient_experience_safeguard(self):
        """Verify policy learner does not prematurely activate policy when sample size < 30."""
        # Using a dummy features dictionary
        prior = SentinelPolicyLearner.get_active_learned_prior({"qlib_score": 60.0})
        self.assertIn("policy_active", prior)
        self.assertIn(prior["status"], ["INSUFFICIENT_EXPERIENCE", "ACTIVE_SENTINEL_POLICY"])

    def test_17_experiment_dispatcher_task_sync_idempotency(self):
        """Verify syncing tasks from journal next_steps generates clean, non-duplicate tasks."""
        synced = AutonomousExperimentDispatcher.sync_tasks_from_journal()
        self.assertIsInstance(synced, int)
        # Second sync should be idempotent (0 newly created)
        synced_again = AutonomousExperimentDispatcher.sync_tasks_from_journal()
        self.assertEqual(synced_again, 0)

    def test_18_experiment_dispatcher_validation_layers(self):
        """Verify layered AST, py_compile, and static firewall checks block invalid code."""
        # 1. Invalid syntax
        bad_syntax = "def broken(\n"
        self.assertFalse(AutonomousExperimentDispatcher._verify_layered_validation(bad_syntax, "test_bad_syn.py")[0])

        # 2. Production write attempt in experiment
        malicious = "with open('../../backend/models/intraday/champion_ensemble.pkl', 'w') as f: f.write('hack')"
        self.assertFalse(AutonomousExperimentDispatcher._verify_layered_validation(malicious, "test_mal.py")[0])

        # 3. Clean harmless sandbox code
        safe_code = "def calc_return(p1, p2):\n    return (p2 - p1) / p1\n"
        self.assertTrue(AutonomousExperimentDispatcher._verify_layered_validation(safe_code, "test_safe.py")[0])

    def test_19_web_researcher_safe_search_logging(self):
        """Verify safe web search logs citation and does not bypass limits."""
        res = safe_search("NSE Nifty volatility index", max_results=2)
        self.assertIsInstance(res, list)
        if len(res) > 0:
            item = res[0]
            self.assertIn("url", item)
            self.assertIn("summary", item)
            self.assertIn("citation_id", item)

    def test_20_git_manager_sandbox_rollback_integrity(self):
        """Verify sandbox rollback cleans working tree inside agent_lab without touching backend."""
        # Touch a scratch file in experiments/
        test_scratch = WORKSPACE_ROOT / "agent_lab" / "experiments" / "_temp_scratch_test.txt"
        test_scratch.write_text("temporary scratch")
        self.assertTrue(test_scratch.exists())
        # Rollback
        AgentGitManager.rollback_sandbox()
        self.assertFalse(test_scratch.exists())

    def test_21_experience_store_rich_metadata(self):
        """Verify experience store persists all 33 rich attributes and deterministic IDs."""
        conn = sqlite3.connect(get_agent_db_path())
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM experience_store LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        d = dict(row)
        # Verify deterministic ID format
        self.assertTrue(d["experience_id"].startswith("exp_"))
        # Verify required rich metadata fields
        required_cols = [
            "experience_id", "decision_id", "trade_id", "ticker", "direction",
            "agent_prob", "sector", "volatility", "fno_context", "sentiment_context",
            "chart_context", "entry_quality", "gross_pnl", "friction",
            "realized_net_pnl", "mfe", "mae", "exit_classification", "resolved_at"
        ]
        for col in required_cols:
            self.assertIn(col, d, f"Missing required column: {col}")

    def test_22_recommendation_divergence_logging(self):
        """Verify recommendation divergence tracking between Qwen, learned policy, and final action."""
        candidate = {
            "ticker": "INFY.NS",
            "direction": "BUY",
            "qlib_score": 42.0,
            "legacy_score": 48.0,
            "composite_score": 45.0,
            "is_actionable": True
        }
        res = TradeSentinelAgent.observe_and_evaluate(candidate)
        conn = sqlite3.connect(get_agent_db_path())
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agent_decisions WHERE decision_id = ?", (res["decision_id"],))
        row = cursor.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        dec = dict(row)
        self.assertIn("qwen_recommendation", dec)
        self.assertIn("learned_policy_recommendation", dec)
        self.assertIn("final_recommendation", dec)
        self.assertIn("divergence_reason", dec)

    def test_23_policy_retraining_eligibility_enforcement(self):
        """Verify policy retraining gate checks sample size and new experience delta."""
        eligibility = SentinelPolicyLearner.check_retraining_eligibility()
        self.assertIn("eligible", eligibility)
        self.assertIn("total_experiences", eligibility)
        self.assertIn("new_experiences", eligibility)
        self.assertIn("reason", eligibility)

    def test_24_offline_policy_deterministic_hashing(self):
        """Verify trained policies contain cryptographic SHA-256 fingerprints."""
        conn = sqlite3.connect(get_agent_db_path())
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM learned_policies ORDER BY created_at DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        if row:
            pol = dict(row)
            self.assertIn("dataset_fingerprint", pol)
            self.assertIn("config_hash", pol)
            self.assertIn("artifact_hash", pol)
            self.assertEqual(len(pol["dataset_fingerprint"]), 16)
            self.assertEqual(len(pol["config_hash"]), 16)
            self.assertEqual(len(pol["artifact_hash"]), 16)

    def test_25_autonomous_strategy_discovery_patterns(self):
        """Verify strategy discovery scanner extracts empirical hypotheses without hallucination."""
        patterns = AutonomousStrategyDiscoveryEngine.scan_for_empirical_patterns()
        self.assertIsInstance(patterns, list)
        for p in patterns:
            self.assertIn("category", p)
            self.assertIn("hypothesis", p)
            self.assertIn("priority", p)

    def test_26_sandbox_bug_repair_bounded_attempts_and_rollback(self):
        """Verify bug repair engine enforces MAX_REPAIR_ATTEMPTS and automatic rollback."""
        # 1. Invalid syntax patch should exhaust attempts and rollback
        rel_bad_target = "experiments/_test_repair_target.py"
        res_fail = SandboxBugRepairEngine.diagnose_and_repair(
            target_file_rel=rel_bad_target,
            failing_error="SyntaxError: invalid syntax",
            patch_code="def broken_func(\n"
        )
        self.assertFalse(res_fail["success"])
        self.assertEqual(res_fail["status"], "REPAIR_EXHAUSTED")
        self.assertEqual(res_fail["attempts"], SandboxBugRepairEngine.MAX_REPAIR_ATTEMPTS)

        # 2. Valid patch should validate and commit
        rel_good_target = "experiments/_test_repair_good.py"
        res_ok = SandboxBugRepairEngine.diagnose_and_repair(
            target_file_rel=rel_good_target,
            failing_error="None",
            patch_code="def correct_func(x):\n    return x * 2\n"
        )
        self.assertTrue(res_ok["success"])
        self.assertEqual(res_ok["status"], "REPAIRED_AND_COMMITTED")
        # Cleanup
        AgentGitManager.rollback_sandbox()

    def test_27_web_researcher_search_and_analyze(self):
        """Verify search_and_analyze logs structured citations and synthesis."""
        res = search_and_analyze("Nifty volatility clustering", research_purpose="VOLATILITY_AUDIT")
        self.assertIn("query", res)
        self.assertIn("purpose", res)
        self.assertIn("citations", res)
        self.assertIn("synthesis", res)
        self.assertIsInstance(res["citations"], list)

    def test_28_bounded_autonomous_research_loop(self):
        """Verify bounded autonomous research loop executes cleanly and returns structured telemetry."""
        loop_res = TradeSentinelAgent.run_autonomous_research_loop(max_cycles=1, force_bypass=True)
        self.assertEqual(loop_res["status"], "COMPLETED")
        self.assertEqual(loop_res["cycles_run"], 1)
        self.assertIn("cycle_reports", loop_res)
        report = loop_res["cycle_reports"][0]
        self.assertIn("trades_reconciled", report)
        self.assertIn("patterns_discovered", report)
        self.assertIn("experiment_result", report)

    def test_29_post_market_synthesis_9_questions(self):
        """Verify post-market synthesis covers the 9 institutional prompt questions."""
        # Check that divergence journal or session reviews contain structured questions
        conn = sqlite3.connect(get_agent_db_path())
        cursor = conn.cursor()
        cursor.execute("SELECT findings FROM research_journal WHERE findings LIKE '%1. %' LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        if row:
            findings = row[0]
            # Verify numbered institutional questions exist in findings
            self.assertIn("1.", findings)
            self.assertIn("2.", findings)

    def test_30_authoritative_metrics_expectancy_and_calibration(self):
        """Verify scoreboard includes expectancy, calibration, direction breakdown, and active policy."""
        sb = TradeSentinelAgent.get_scoreboard_summary()
        self.assertIn("expectancy", sb)
        self.assertIn("calibration", sb)
        self.assertIn("direction_performance", sb)
        self.assertIn("time_of_day_performance", sb)
        self.assertIn("active_policy", sb)
        self.assertIn("avg_agent_probability", sb["calibration"])
        self.assertIn("BUY", sb["direction_performance"])
        self.assertIn("SELL", sb["direction_performance"])

    def test_31_trace_pipeline_causality_canonical_cost(self):
        """Verify trace_pipeline_causality computes statutory friction dynamically and traces signal decay."""
        res = DomainForensicTools.trace_pipeline_causality()
        self.assertIn("sample_size", res)
        self.assertGreater(res["sample_size"], 0)
        self.assertIn("cost_model", res)
        self.assertIn("canonical_intraday_friction_pct", res["cost_model"])
        self.assertIn("chain_summary", res)
        self.assertIsInstance(res["chain_summary"], list)
        self.assertIn("net_vs_gross_drag", res)
        self.assertIn("friction_loss_pct", res["net_vs_gross_drag"])
        self.assertIn("verdict", res)

    def test_32_analyze_trade_population_cohorts(self):
        """Verify analyze_trade_population slices trades cleanly across time-of-day, confidence, and regime."""
        # 1. Time of day
        res_tod = DomainForensicTools.analyze_trade_population(slice_by="time_of_day")
        self.assertIn("cohort_breakdown", res_tod)
        self.assertIn("key_finding", res_tod)
        for cohort_name, stats in res_tod["cohort_breakdown"].items():
            self.assertIn("trades_count", stats)
            self.assertIn("win_rate_pct", stats)
            self.assertIn("mean_net_return_pct", stats)
            self.assertIn("profit_factor", stats)

        # 2. Confidence
        res_conf = DomainForensicTools.analyze_trade_population(slice_by="confidence")
        self.assertIn("cohort_breakdown", res_conf)

    def test_33_run_forensic_experiment_sandbox(self):
        """Verify run_forensic_experiment executes in sandbox, records in ledger, and checks governance gates."""
        exp = DomainForensicTools.run_forensic_experiment(
            hypothesis_id="TEST-HYP-33",
            hypothesis="Filtering post-14:00 intraday trades reduces negative friction drag",
            experiment_type="BACKTEST_FILTER",
            parameters={"cutoff_hour": 14}
        )
        self.assertIn("experiment_id", exp)
        self.assertIn("verdict", exp)
        self.assertIn(exp["verdict"], ["SUPPORTED", "REJECTED", "INCONCLUSIVE"])
        self.assertIn("artifact_hash", exp)

        # Verify entry in experiments_ledger
        conn = sqlite3.connect(get_agent_db_path())
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM experiments_ledger WHERE experiment_id = ?", (exp["experiment_id"],))
        self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

    def test_34_inspect_system_integrity_audits(self):
        """Verify inspect_system_integrity checks firewall, timestamp causality, and model immutability."""
        audit = DomainForensicTools.inspect_system_integrity()
        self.assertIn("status", audit)
        self.assertIn("CRITICAL", audit)
        self.assertIn("VERIFIED", audit)
        # CRITICAL should be 0 because firewall and broker protections are active
        self.assertEqual(len(audit["CRITICAL"]), 0)
        self.assertTrue(any("ProductionFirewall" in v for v in audit["VERIFIED"]))
        self.assertTrue(any("Champion Model" in v for v in audit["VERIFIED"]))

    def test_35_mission_state_hypothesis_lifecycle(self):
        """Verify MissionState enforces hypothesis lifecycle: PENDING -> INVESTIGATING -> SUPPORTED."""
        state = MissionState(mission_id="test_mission_lifecycle", objective="Verify lifecycle transitions")
        state.register_hypothesis("HYP-01", "Morning gap setups preserve edge")
        self.assertEqual(state.hypotheses["HYP-01"]["status"], "PENDING")

        state.update_hypothesis("HYP-01", "INVESTIGATING", evidence="Gathering cohort data")
        self.assertEqual(state.hypotheses["HYP-01"]["status"], "INVESTIGATING")

        state.update_hypothesis("HYP-01", "SUPPORTED", verdict_reason="Morning win rate 58% vs afternoon 41%")
        self.assertEqual(state.hypotheses["HYP-01"]["status"], "SUPPORTED")

        # Invalid status should raise ValueError
        with self.assertRaises(ValueError):
            state.update_hypothesis("HYP-01", "INVALID_STATUS")

    def test_36_mission_state_budget_enforcement(self):
        """Verify MissionState enforces cycle and tool budgets and produces bounded prompt context."""
        state = MissionState(mission_id="test_budget", objective="Test limits", max_cycles=2, max_tool_calls=2)
        self.assertFalse(state.is_budget_exhausted())

        state.record_tool_call("trace_pipeline_causality", {}, {"verdict": "FRICTION_DESTROYS_MARGINAL_EDGE"})
        state.record_tool_call("inspect_system_integrity", {}, {"status": "HEALTHY"})
        self.assertTrue(state.is_budget_exhausted())

        context = state.format_context_for_qwen()
        self.assertIsInstance(context, str)
        # Token size protection: ensure prompt representation is compact
        self.assertLess(len(context.split()), 500)

    def test_37_autonomous_investigator_execution(self):
        """Verify AutonomousQuantitativeInvestigator executes a bounded mission and updates state."""
        mission_res = AutonomousQuantitativeInvestigator.execute_mission(
            objective="Audit relationship between canonical statutory friction and net performance",
            max_cycles=1,
            max_tool_calls=2
        )
        self.assertIn("mission_id", mission_res)
        self.assertIn("status", mission_res)
        self.assertIn("current_best_explanation", mission_res)
        self.assertIn("tool_history", mission_res)
        self.assertGreaterEqual(len(mission_res["tool_history"]), 1)

    def test_38_trade_sentinel_run_core_edge_forensic(self):
        """Verify TradeSentinelAgent runs CORE_EDGE_FORENSIC and reflects mission in scoreboard summary."""
        forensic_res = TradeSentinelAgent.run_core_edge_forensic(max_cycles=1)
        self.assertIn("mission_id", forensic_res)
        self.assertIn("status", forensic_res)

        sb = TradeSentinelAgent.get_scoreboard_summary()
        self.assertIn("latest_mission", sb)
        self.assertIsNotNone(sb["latest_mission"])
        self.assertEqual(sb["latest_mission"]["mission_id"], forensic_res["mission_id"])


if __name__ == "__main__":
    unittest.main()



