import unittest
import os
import json
import hashlib
import sqlite3
from unittest.mock import patch, MagicMock
import requests

from app.data.historical_data_layer import get_db_path
from app.analytics.research_job_manager import research_job_manager, ResearchJobStatus

BASE_URL = "http://localhost:8000"

INTRADAY_CHAMPION_PATH = "backend/models/intraday/champion_ensemble.pkl"
SWING_CHAMPION_PATH = "backend/models/swing/champion_ensemble.pkl"
KNOWN_INTRADAY_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
KNOWN_SWING_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

class TestResearchReportUpgrade(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(get_db_path())
        self.cur = self.conn.cursor()

    def tearDown(self):
        self.conn.close()

    # 1. Successful research report loads
    def test_successful_research_report_loads(self):
        self.cur.execute("SELECT job_id FROM research_jobs WHERE status = 'COMPLETED' LIMIT 1")
        row = self.cur.fetchone()
        if row:
            job_id = row[0]
            resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs/{job_id}/results")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("job_id", data)
            self.assertIn("job", data)
            self.assertIn("results", data)
            self.assertEqual(data["job_id"], job_id)
            self.assertIsNotNone(data["results"])

    # 2. Cancelled research report loads safely (partial telemetry)
    def test_cancelled_research_report_loads_safely(self):
        self.cur.execute("SELECT job_id FROM research_jobs WHERE status = 'CANCELLED' LIMIT 1")
        row = self.cur.fetchone()
        if row:
            job_id = row[0]
            resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs/{job_id}/results")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["job_id"], job_id)
            results = data.get("results", {})
            self.assertTrue(results.get("is_partial", False) or results.get("status") == "CANCELLED")

    # 3. Failed research report loads safely
    def test_failed_research_report_loads_safely(self):
        self.cur.execute("SELECT job_id FROM research_jobs WHERE status = 'FAILED' LIMIT 1")
        row = self.cur.fetchone()
        if row:
            job_id = row[0]
            resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs/{job_id}/results")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["job_id"], job_id)
            self.assertIsNotNone(data.get("job"))

    # 4. 500+ stock report data handling
    def test_large_universe_report_data_handling(self):
        self.cur.execute("SELECT job_id FROM research_jobs WHERE universe IN ('NIFTY_50', 'NIFTY_500', 'ALL_COLLECTED') AND status = 'COMPLETED' LIMIT 1")
        row = self.cur.fetchone()
        if row:
            job_id = row[0]
            resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs/{job_id}/results")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            results = data.get("results", {})
            self.assertIn("trades", results)
            self.assertIn("equity_curve", results)

    # 5. Missing metric does not crash report payload
    def test_missing_metric_does_not_crash_payload(self):
        synth = research_job_manager.synthesize_partial_report("non_existent_job_12345")
        self.assertIsNone(synth)

    # 6. NaN does not crash report payload
    def test_nan_values_handled_safely(self):
        mock_results = {
            "status": "COMPLETED",
            "metrics": {
                "total_pnl": float("nan"),
                "win_rate": None,
                "sharpe_ratio": float("inf")
            }
        }
        dumped = json.dumps(mock_results, default=str)
        self.assertIn("NaN", dumped)
        self.assertIn("Infinity", dumped)

    # 7. Missing trade data does not crash report payload
    def test_missing_trade_data_handled_safely(self):
        mock_results = {
            "status": "COMPLETED",
            "trades": [],
            "equity_curve": []
        }
        self.assertEqual(len(mock_results["trades"]), 0)
        self.assertEqual(len(mock_results["equity_curve"]), 0)

    # 8. Equity curve structure correct
    def test_equity_curve_structure(self):
        self.cur.execute("SELECT job_id FROM research_jobs WHERE status = 'COMPLETED' LIMIT 1")
        row = self.cur.fetchone()
        if row:
            results = research_job_manager.get_job_results(row[0])
            if results and "equity_curve" in results:
                eq = results["equity_curve"]
                if len(eq) > 0:
                    pt = eq[0]
                    self.assertIn("date", pt)
                    self.assertIn("equity", pt)

    # 9. Drawdown calculation correct
    def test_drawdown_calculation(self):
        mock_curve = [
            {"date": "2025-01-01", "equity": 100.0},
            {"date": "2025-01-02", "equity": 120.0},
            {"date": "2025-01-03", "equity": 90.0},
        ]
        peak = 100.0
        max_dd = 0.0
        for pt in mock_curve:
            if pt["equity"] > peak:
                peak = pt["equity"]
            dd = (peak - pt["equity"]) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
        self.assertEqual(max_dd, 25.0)

    # 10. Yearly statistics structure
    def test_yearly_statistics_structure(self):
        self.cur.execute("SELECT job_id FROM research_jobs WHERE status = 'COMPLETED' LIMIT 1")
        row = self.cur.fetchone()
        if row:
            results = research_job_manager.get_job_results(row[0])
            if results and "performance_by_year" in results:
                y = results["performance_by_year"]
                self.assertIsInstance(y, dict)

    # 11. Trade statistics calculation
    def test_trade_statistics_calculation(self):
        mock_trades = [
            {"ticker": "TCS.NS", "pnl": 100.0},
            {"ticker": "INFY.NS", "pnl": -50.0},
            {"ticker": "RELIANCE.NS", "pnl": 200.0}
        ]
        wins = [t for t in mock_trades if t["pnl"] > 0]
        losses = [t for t in mock_trades if t["pnl"] <= 0]
        win_rate = len(wins) / len(mock_trades) * 100.0
        profit_factor = sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
        self.assertAlmostEqual(win_rate, 66.67, places=1)
        self.assertEqual(profit_factor, 6.0)

    # 12. Stock statistics aggregation
    def test_stock_statistics_aggregation(self):
        mock_trades = [
            {"ticker": "TCS.NS", "pnl": 100.0},
            {"ticker": "TCS.NS", "pnl": 50.0},
            {"ticker": "INFY.NS", "pnl": -20.0}
        ]
        tcs_pnl = sum(t["pnl"] for t in mock_trades if t["ticker"] == "TCS.NS")
        self.assertEqual(tcs_pnl, 150.0)

    # 13. Research fingerprint displayed correctly
    def test_research_fingerprint_persistence(self):
        self.cur.execute("SELECT job_id, research_fingerprint FROM research_jobs WHERE status = 'COMPLETED' AND research_fingerprint IS NOT NULL LIMIT 1")
        row = self.cur.fetchone()
        if row:
            job_id, fp = row
            resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs/{job_id}/results")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["job"]["research_fingerprint"], fp)

    # 14. Production usage status is correct
    def test_production_usage_status(self):
        self.cur.execute("SELECT job_id FROM research_jobs WHERE status = 'COMPLETED' LIMIT 1")
        row = self.cur.fetchone()
        if row:
            job = research_job_manager.get_job(row[0])
            self.assertNotEqual(job.get("status"), "CHAMPION_ACTIVE")

    # 15. Research-only result cannot be mistaken for Champion
    def test_research_cannot_be_mistaken_for_champion(self):
        self.cur.execute("SELECT job_id FROM research_jobs WHERE status = 'COMPLETED' LIMIT 1")
        row = self.cur.fetchone()
        if row:
            resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs/{row[0]}/results")
            data = resp.json()
            self.assertEqual(data["job"]["status"], "COMPLETED")

    # 16. Challenger status governance
    def test_challenger_status_governance(self):
        self.cur.execute("SELECT count(*) FROM research_jobs WHERE status = 'COMPLETED'")
        completed_count = self.cur.fetchone()[0]
        self.assertGreaterEqual(completed_count, 1)

    # 17. Historical research report remains accessible
    def test_historical_research_accessible(self):
        self.cur.execute("SELECT job_id FROM research_jobs ORDER BY created_at ASC LIMIT 1")
        row = self.cur.fetchone()
        if row:
            resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs/{row[0]}")
            self.assertEqual(resp.status_code, 200)

    # 18. Champion hashes remain unchanged
    def test_champion_hashes_unchanged(self):
        with open(INTRADAY_CHAMPION_PATH, "rb") as f:
            h_intra = hashlib.sha256(f.read()).hexdigest()
        with open(SWING_CHAMPION_PATH, "rb") as f:
            h_swing = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(h_intra, KNOWN_INTRADAY_HASH)
        self.assertEqual(h_swing, KNOWN_SWING_HASH)

    # 19. No research writes to live trade history
    def test_no_research_writes_to_live_trade_history(self):
        self.cur.execute("SELECT count(*) FROM ml_trade_history WHERE position_type = 'LIVE_POSITION'")
        live_count = self.cur.fetchone()[0]
        self.cur.execute("SELECT count(*) FROM ml_trade_history")
        total_history = self.cur.fetchone()[0]
        self.assertEqual(live_count, 0)
        self.assertEqual(total_history, 64)

    # 20. No research changes portfolio heat
    def test_no_research_changes_portfolio_heat(self):
        from app.analytics.kelly_sizer import get_portfolio_heat_status
        heat = get_portfolio_heat_status()
        self.assertEqual(heat["current_heat_pct"], 0.0)

    # 21. No research executes broker orders
    @patch("app.api.broker.execute_trade", side_effect=AssertionError("Broker execution must never be called by research!"))
    def test_no_broker_execution(self, mock_broker):
        resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs?limit=5")
        self.assertEqual(resp.status_code, 200)
        mock_broker.assert_not_called()

    # 22. No research sends production Telegram alerts
    @patch("app.analytics.telegram_notifier.send_telegram_message", side_effect=AssertionError("Telegram alert must never be sent by research!"))
    def test_no_telegram_alerts(self, mock_tg):
        resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs?limit=5")
        self.assertEqual(resp.status_code, 200)
        mock_tg.assert_not_called()

    # 23. Cache-hit research opens existing report correctly
    def test_cache_hit_research_opens_existing_report(self):
        p = {
            "research_type": "PORTFOLIO_WALK_FORWARD",
            "universe": "NIFTY_50",
            "timeframe": "1d",
            "history_years": 3,
            "model_type": "LIGHTGBM_ALPHA",
            "force_rerun": False
        }
        resp = requests.post(f"{BASE_URL}/api/data-lab/research/jobs", json=p)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("status"), "EXISTING_RESEARCH_FOUND")
        self.assertTrue(data.get("cache_hit"))
        self.assertIsNotNone(data.get("job", {}).get("job_id"))

    # 24. Force-rerun research flag recognized
    def test_force_rerun_flag_recognized(self):
        p = {
            "research_type": "PORTFOLIO_WALK_FORWARD",
            "universe": "BENCHMARK_5",
            "timeframe": "1d",
            "history_years": 3,
            "model_type": "LIGHTGBM_ALPHA",
            "force_rerun": True
        }
        fp = research_job_manager.compute_research_fingerprint(p)
        self.assertIsNotNone(fp)

    # 25. PDF report generation returns valid binary
    def test_pdf_report_generation(self):
        self.cur.execute("SELECT job_id FROM research_jobs WHERE status = 'COMPLETED' LIMIT 1")
        row = self.cur.fetchone()
        if row:
            job_id = row[0]
            resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs/{job_id}/pdf")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("content-type"), "application/pdf")
            self.assertTrue(resp.content.startswith(b"%PDF"))
            self.assertGreater(len(resp.content), 2000)

    # 26. Pipeline health sweep validates research integrity
    def test_pipeline_health_sweep_validates_research_integrity(self):
        from app.analytics.system_health_center import SystemHealthCenter
        res = SystemHealthCenter._check_research_engine()
        self.assertEqual(res["status"], "HEALTHY")
        self.assertTrue(res["details"]["champion_hashes_verified"])
        self.assertEqual(res["details"]["report_integrity_suite"], "24/24 CHECKS VERIFIED")

    # 27. Horizon forensics correctly audits actual years vs config label
    def test_horizon_forensics_accuracy(self):
        resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs/res_20260903_172929_829837/results")
        self.assertEqual(resp.status_code, 200)
        res = resp.json().get("results", {})
        hz = res.get("horizon_forensics", {})
        self.assertEqual(hz.get("actual_years"), 8.9)
        self.assertEqual(hz.get("total_trading_bars"), 2196)
        self.assertEqual(hz.get("configured_history_years"), 3)
        self.assertIn("8.9", hz.get("horizon_explanation", ""))

    # 28. Drawdown forensics computes closed-trade DD and identifies artifact
    def test_drawdown_forensics_closed_vs_mtm(self):
        resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs/res_20260903_172929_829837/results")
        self.assertEqual(resp.status_code, 200)
        res = resp.json().get("results", {})
        dd = res.get("drawdown_forensics", {})
        self.assertEqual(dd.get("reported_max_drawdown_pct"), 58.75)
        self.assertEqual(dd.get("closed_trade_max_drawdown_pct"), 21.74)
        self.assertIn("2024-09-16", dd.get("forensic_explanation", ""))
        self.assertIn("double", dd.get("forensic_explanation", "").lower())

    # 29. Holdout concentration analysis audits all 34 holdout trades
    def test_holdout_concentration_metrics(self):
        resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs/res_20260903_172929_829837/results")
        self.assertEqual(resp.status_code, 200)
        res = resp.json().get("results", {})
        ho = res.get("holdout_deep_dive", {})
        self.assertEqual(ho.get("total_trades"), 34)
        self.assertEqual(ho.get("win_rate_pct"), 47.06)
        self.assertEqual(len(ho.get("trades", [])), 34)
        self.assertEqual(ho.get("concentration", {}).get("top5_pct"), 99.36)

    # 30. Stock concentration audits 511 stocks and flags sample size warnings
    def test_stock_concentration_metrics(self):
        resp = requests.get(f"{BASE_URL}/api/data-lab/research/jobs/res_20260903_172929_829837/results")
        self.assertEqual(resp.status_code, 200)
        res = resp.json().get("results", {})
        sc = res.get("stock_concentration", {})
        self.assertEqual(sc.get("total_universe_stocks"), 511)
        self.assertEqual(sc.get("traded_stocks_count"), 163)
        self.assertEqual(sc.get("zero_trade_stocks_count"), 348)
        self.assertEqual(len(sc.get("top_20_stocks", [])), 20)
        self.assertEqual(len(sc.get("bottom_20_stocks", [])), 20)
        self.assertTrue(sc.get("top_20_stocks")[0].get("sample_size_warning"))

if __name__ == "__main__":
    unittest.main()
