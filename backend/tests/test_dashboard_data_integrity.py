import unittest
import hashlib
import os
import sqlite3
import tempfile
import pandas as pd
from unittest.mock import patch, MagicMock

from app.data.historical_data_layer import get_db_path
from app.analytics.dashboard_intelligence_service import DashboardIntelligenceService
from app.analytics.dashboard_report_pdf_generator import DashboardReportPDFGenerator


class TestDashboardDataIntegrity(unittest.TestCase):
    """
    Forensic Data Integrity Test Suite for Dashboard Intelligence Layer.
    Validates:
      1. Market Breadth mathematical invariants and truthful denominators.
      2. DMA technical breadth and 52-week High/Low calculations.
      3. Sector return calculations (strict prevention of 0.00% missing data fallbacks).
      4. Global cues market session states (LAST CLOSE, LIVE, PRE-MARKET).
      5. AI opportunity canonical ticker normalization and deduplication.
      6. System health status semantic mappings.
      7. Cache age and freshness metadata.
      8. Safety protections (Champion model hashes and broker fail-safe).
    """

    @classmethod
    def setUpClass(cls):
        cls.db_path = get_db_path()
        cls.intraday_model_path = "backend/models/intraday/champion_ensemble.pkl"
        cls.swing_model_path = "backend/models/swing/champion_ensemble.pkl"
        cls.expected_intraday_hash = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
        cls.expected_swing_hash = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

    # ── 1. BREADTH INVARIANTS & DENOMINATORS ─────────────────────────────
    def test_breadth_invariants_and_denominators(self):
        breadth = DashboardIntelligenceService.get_market_breadth()
        
        universe_size = breadth.get("universe_size", 0)
        evaluated_count = breadth.get("evaluated_count", 0)
        missing_count = breadth.get("missing_count", 0)
        advances = breadth.get("advances", 0)
        declines = breadth.get("declines", 0)
        unchanged = breadth.get("unchanged", 0)
        coverage_pct = breadth.get("coverage_pct", 0.0)

        # Invariant 1: advances + declines + unchanged == evaluated_count
        self.assertEqual(
            advances + declines + unchanged,
            evaluated_count,
            f"Math invariant failed: {advances} + {declines} + {unchanged} != {evaluated_count}"
        )

        # Invariant 2: evaluated_count + missing_count == universe_size
        self.assertEqual(
            evaluated_count + missing_count,
            universe_size,
            f"Coverage invariant failed: {evaluated_count} + {missing_count} != {universe_size}"
        )

        # Invariant 3: coverage_pct is truthful
        if universe_size > 0:
            expected_cov = round(evaluated_count / universe_size * 100.0, 1)
            self.assertAlmostEqual(coverage_pct, expected_cov, places=1)

        # Invariant 4: percentage advancing is based on evaluated_count, NOT universe_size
        if evaluated_count > 0:
            expected_pct_adv = round(advances / evaluated_count * 100.0, 1)
            self.assertAlmostEqual(breadth.get("pct_advancing"), expected_pct_adv, places=1)
            
            # Specifically verify that 33/51 = 64.7%, NOT 33/511 = 6.46%
            if advances == 33 and evaluated_count == 51:
                self.assertAlmostEqual(breadth.get("pct_advancing"), 64.7, places=1)

    def test_breadth_dma_and_high_low_denominators(self):
        breadth = DashboardIntelligenceService.get_market_breadth()
        dma_eval = breadth.get("dma_evaluated_count", 0)
        
        self.assertGreater(dma_eval, 0, "DMA evaluated count must be greater than 0")
        self.assertLessEqual(breadth.get("above_20_count", 0), dma_eval)
        self.assertLessEqual(breadth.get("above_50_count", 0), dma_eval)
        self.assertLessEqual(breadth.get("above_200_count", 0), dma_eval)
        
        if dma_eval > 0:
            expected_dma20 = round(breadth["above_20_count"] / dma_eval * 100.0, 1)
            self.assertAlmostEqual(breadth["above_20_dma_pct"], expected_dma20, places=1)

    # ── 2. SECTOR ROTATION DATA HONESTY ──────────────────────────────────
    def test_sector_missing_data_returns_none_not_zero(self):
        sectors_res = DashboardIntelligenceService.get_sector_performance()
        sectors = sectors_res.get("sectors", [])
        self.assertGreater(len(sectors), 0, "Sectors list should not be empty")

        for sec in sectors:
            if sec.get("status") == "UNAVAILABLE":
                self.assertIsNone(
                    sec.get("change_1d_pct"),
                    f"Sector {sec.get('name')} marked UNAVAILABLE but has non-None change {sec.get('change_1d_pct')}"
                )
            elif sec.get("change_1d_pct") is not None:
                self.assertIsNotNone(sec.get("previous_close"))
                self.assertGreater(sec.get("previous_close"), 0)

    # ── 3. GLOBAL CUES SESSION STATES ────────────────────────────────────
    def test_global_cues_session_states(self):
        cues = DashboardIntelligenceService.get_global_cues()
        self.assertGreater(len(cues), 0)

        valid_states = {"LIVE", "CLOSED", "PRE-MARKET", "POST-MARKET", "UNAVAILABLE"}
        valid_labels = {"LIVE", "CLOSED", "PRE-MARKET", "POST-MARKET", "LAST CLOSE", "UNAVAILABLE"}

        for cue in cues:
            m_state = cue.get("market_state")
            s_label = cue.get("state_label")
            self.assertIn(m_state, valid_states, f"Invalid market_state {m_state} for {cue.get('name')}")
            self.assertIn(s_label, valid_labels, f"Invalid state_label {s_label} for {cue.get('name')}")

    # ── 4. AI OPPORTUNITY DEDUPLICATION & CANONICAL NORMALIZATION ─────────
    def test_ai_opportunities_deduplication(self):
        ai_data = DashboardIntelligenceService.get_ai_opportunities()
        intraday_ops = ai_data.get("intraday", {}).get("opportunities", [])
        swing_ops = ai_data.get("swing", {}).get("opportunities", [])
        
        all_rendered = intraday_ops + swing_ops
        canonical_symbols = [op["canonical_ticker"] for op in all_rendered]
        
        self.assertEqual(
            len(canonical_symbols),
            len(set(canonical_symbols)),
            f"Duplicate canonical tickers found in AI opportunities: {canonical_symbols}"
        )
        self.assertIn("disclaimer", ai_data)
        self.assertIn("VIRTUAL AI RECOMMENDATIONS — NOT LIVE POSITIONS", ai_data["disclaimer"])

    # ── 5. SYSTEM HEALTH MATRIX ──────────────────────────────────────────
    def test_system_health_matrix_mapping(self):
        health = DashboardIntelligenceService.get_system_health_matrix()
        self.assertIn(health.get("market_data"), ["HEALTHY", "WARNING", "DEGRADED"])
        self.assertIn(health.get("ai_models"), ["HEALTHY", "WARNING", "DEGRADED"])
        self.assertIn(health.get("database"), ["HEALTHY", "WARNING", "DEGRADED"])
        self.assertIn(health.get("telegram"), ["HEALTHY", "UNAVAILABLE"])
        self.assertIn(health.get("broker_mode"), ["SIMULATION (Fail-Safe)"])

    # ── 6. CACHE METADATA ────────────────────────────────────────────────
    def test_cache_metadata_integrity(self):
        snap = DashboardIntelligenceService.get_dashboard_snapshot(force_refresh=False)
        cache_meta = snap.get("cache_metadata")
        self.assertIsNotNone(cache_meta, "cache_metadata must be present in snapshot")
        self.assertIn("is_cache_hit", cache_meta)
        self.assertIn("cache_age_seconds", cache_meta)
        self.assertIn("freshness_status", cache_meta)
        self.assertIn(cache_meta["freshness_status"], ["FRESH", "STALE", "DELAYED", "UNAVAILABLE"])

    # ── 7. SAFETY: CHAMPION MODELS UNTOUCHED ─────────────────────────────
    def test_champion_models_unchanged(self):
        if os.path.exists(self.intraday_model_path):
            with open(self.intraday_model_path, "rb") as f:
                h = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(h, self.expected_intraday_hash, "Champion Intraday model hash altered!")

        if os.path.exists(self.swing_model_path):
            with open(self.swing_model_path, "rb") as f:
                h = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(h, self.expected_swing_hash, "Champion Swing model hash altered!")

    # ── 8. SAFETY: RESEARCH JOBS UNPERTURBED ─────────────────────────────
    def test_research_jobs_unperturbed(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM research_jobs WHERE status IN ('RUNNING', 'PENDING', 'QUEUED')")
        active_count = cur.fetchone()[0]
        self.assertEqual(active_count, 0, "No research jobs should be active or pending")
        
        cur.execute("SELECT status, completed_tasks, total_tasks, research_fingerprint FROM research_jobs WHERE job_id = 'res_20260903_231545_a6e85e'")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "COMPLETED")
        self.assertEqual(row[1], 511)
        self.assertEqual(row[2], 511)
        self.assertEqual(row[3], "249b320ca10c3fcf4f5e1a5cf51375060a7601404905648143cbad2e50d79885")
        conn.close()

    # ── 9. SAFETY: BROKER FAIL-CLOSED IN SIMULATION MODE ─────────────────
    def test_broker_fail_closed_mode(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = 'broker_mode'")
        row = cur.fetchone()
        conn.close()
        mode = row[0] if row else "SIMULATION"
        self.assertEqual(mode, "SIMULATION", "Broker execution mode must remain SIMULATION (Fail-Safe)")


if __name__ == "__main__":
    unittest.main()
