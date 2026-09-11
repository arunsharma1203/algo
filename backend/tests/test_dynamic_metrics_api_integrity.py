"""
TEST SUITE: DYNAMIC METRICS & API INTEGRITY (MILESTONE 4)
=========================================================
Validates that:
1. /research/challenger/{job_id}/readiness returns genuinely computed metrics from job results.
2. If a job has no results or is pending, the endpoint returns UNAVAILABLE, never hardcoded fallbacks (34, 1.60, 21.74).
3. Sample size gate and risk gate reflect real values.
4. Promotion eligibility reflects real forward OOS progress.
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.data.database import set_test_db_override
from app.api.data_lab import get_research_challenger_readiness

class TestDynamicMetricsApiIntegrity(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db.name
        self.temp_db.close()

        set_test_db_override(self.temp_db_path)

        import sqlite3
        conn = sqlite3.connect(self.temp_db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS research_shadow_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_job_id TEXT,
                ticker TEXT,
                entry_date TEXT,
                status TEXT
            );
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        set_test_db_override(None)
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    @patch("app.api.data_lab.research_job_manager")
    async def test_01_job_without_results_returns_unavailable(self, mock_rjm):
        """Jobs without results return UNAVAILABLE with None metrics, never hardcoded numbers."""
        mock_rjm.get_job.return_value = {
            "job_id": "job_empty",
            "universe": "BENCHMARK_5",
            "research_fingerprint": "fp_empty"
        }
        mock_rjm.get_job_results.return_value = None

        res = await get_research_challenger_readiness("job_empty")

        self.assertEqual(res["status"], "UNAVAILABLE")
        self.assertEqual(res["readiness_verdict"], "NO_JOB_RESULTS")
        self.assertIsNone(res["research_holdout_trades"])
        self.assertIsNone(res["holdout_profit_factor"])
        self.assertIsNone(res["closed_trade_max_dd_pct"])
        self.assertIn("INELIGIBLE", res["promotion_eligibility"])

    @patch("app.api.data_lab.research_job_manager")
    @patch("app.analytics.research_shadow_scorer.count_fresh_shadow_trades")
    async def test_02_job_with_real_results_returns_dynamic_metrics(self, mock_shadow, mock_rjm):
        """Jobs with custom results return their actual trades count and profit factor."""
        mock_rjm.get_job.return_value = {
            "job_id": "job_custom",
            "universe": "BENCHMARK_5",
            "research_fingerprint": "fp_custom"
        }
        # Provide job results with 50 holdout trades (30 wins @ +3000, 20 losses @ -1000 => PF = 4.5)
        custom_trades = [
            {"is_locked_holdout": True, "pnl": 3000.0, "entry_date": "2025-03-01", "exit_date": "2025-03-10"}
            for _ in range(30)
        ] + [
            {"is_locked_holdout": True, "pnl": -1000.0, "entry_date": "2025-03-11", "exit_date": "2025-03-20"}
            for _ in range(20)
        ]
        mock_rjm.get_job_results.return_value = {
            "trades": custom_trades,
            "authoritative_metrics": {
                "profit_factor": 4.5,
                "max_drawdown_pct": 12.5
            }
        }
        mock_shadow.return_value = 5  # 5 fresh shadow trades

        res = await get_research_challenger_readiness("job_custom")

        self.assertEqual(res["research_holdout_trades"], 50)
        self.assertEqual(res["holdout_profit_factor"], 4.5)
        self.assertEqual(res["fresh_oos_shadow_trades"], 5)
        self.assertIn("FAIL (5/30", res["sample_size_gate"])
        self.assertIn("NOT ELIGIBLE", res["promotion_eligibility"])

if __name__ == "__main__":
    unittest.main()
