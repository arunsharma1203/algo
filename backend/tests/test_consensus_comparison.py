"""
Unit tests for Consensus Comparison & History API Endpoints.
Verifies GET /api/consensus/comparison and GET /api/consensus/history.
"""

import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.analytics.consensus_tracker import ConsensusTracker


class TestConsensusComparisonAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch.object(ConsensusTracker, "get_multi_engine_comparison")
    def test_get_multi_engine_comparison_success(self, mock_comp):
        mock_comp.return_value = {
            "status": "SUCCESS",
            "summary": {
                "consensus": {
                    "name": "Dual-Engine Consensus",
                    "badge": "Confluence (Grade-A)",
                    "total_trades": 12,
                    "win_rate": 75.0,
                    "wins": 9,
                    "losses": 3,
                    "total_pnl": 18.5,
                    "avg_pnl": 1.54,
                    "profit_factor": 3.2
                },
                "legacy": {
                    "name": "Legacy AI",
                    "badge": "Ensemble + FinBERT",
                    "total_trades": 45,
                    "win_rate": 57.8,
                    "wins": 26,
                    "losses": 19,
                    "total_pnl": 24.2,
                    "avg_pnl": 0.54,
                    "profit_factor": 1.45
                },
                "qlib": {
                    "name": "Qlib Alpha-158",
                    "badge": "LightGBM Alpha",
                    "total_trades": 38,
                    "win_rate": 63.2,
                    "wins": 24,
                    "losses": 14,
                    "total_pnl": 31.0,
                    "avg_pnl": 0.82,
                    "profit_factor": 1.88
                }
            },
            "timeline": [
                {"date": "2026-09-01", "consensus": 2.1, "legacy": 1.0, "qlib": 1.8},
                {"date": "2026-09-02", "consensus": 4.5, "legacy": 1.8, "qlib": 3.2}
            ],
            "recent_trades": []
        }

        res = self.client.get("/api/consensus/comparison?days=30")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "SUCCESS")
        self.assertIn("summary", data)
        self.assertIn("consensus", data["summary"])
        self.assertIn("legacy", data["summary"])
        self.assertIn("qlib", data["summary"])
        self.assertEqual(len(data["timeline"]), 2)
        mock_comp.assert_called_once_with(days=30)

    @patch.object(ConsensusTracker, "get_multi_engine_comparison")
    def test_get_multi_engine_comparison_with_highest_confluence_filter(self, mock_comp):
        mock_comp.return_value = {
            "status": "SUCCESS",
            "filter_mode": "HIGHEST_CONFLUENCE_ONLY",
            "summary": {
                "consensus": {"name": "Dual-Engine Consensus", "total_trades": 5, "win_rate": 80.0},
                "legacy": {"name": "Legacy AI", "total_trades": 20, "win_rate": 60.0},
                "qlib": {"name": "Qlib Alpha-158", "total_trades": 15, "win_rate": 66.7}
            },
            "timeline": []
        }
        res = self.client.get("/api/consensus/comparison?days=30&filter_mode=HIGHEST_CONFLUENCE_ONLY")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["filter_mode"], "HIGHEST_CONFLUENCE_ONLY")
        mock_comp.assert_called_once_with(days=30, filter_mode="HIGHEST_CONFLUENCE_ONLY")

    @patch.object(ConsensusTracker, "get_consensus_performance_stats")
    def test_get_consensus_history_success(self, mock_stats):
        mock_stats.return_value = {
            "total_trades": 5,
            "win_rate": 80.0,
            "target_hits": 4,
            "sl_hits": 1,
            "squared_off": 0,
            "avg_profit_pct": 2.1,
            "total_profit_pct": 10.5,
            "cumulative_series": [
                {"date": "2026-09-10", "time": "2026-09-10 10:15:00", "ticker": "RELIANCE.NS", "cum_pnl": 2.5}
            ],
            "trades": [
                {
                    "id": 1,
                    "timestamp": "2026-09-10 10:15:00",
                    "ticker": "RELIANCE.NS",
                    "direction": "BUY",
                    "entry_price": 2950.0,
                    "exit_price": 3020.0,
                    "outcome": "TARGET MET",
                    "profit_pct": 2.37,
                    "score": 0.84
                }
            ]
        }

        res = self.client.get("/api/consensus/history?days=30")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total_trades"], 5)
        self.assertEqual(data["win_rate"], 80.0)
        self.assertEqual(len(data["trades"]), 1)
        mock_stats.assert_called_once_with(days=30)

    @patch.object(ConsensusTracker, "get_consensus_performance_stats")
    def test_get_consensus_history_with_highest_confluence_filter(self, mock_stats):
        mock_stats.return_value = {
            "total_trades": 2,
            "filter_mode": "HIGHEST_CONFLUENCE_ONLY",
            "win_rate": 100.0,
            "target_hits": 2,
            "sl_hits": 0,
            "squared_off": 0,
            "avg_profit_pct": 3.0,
            "total_profit_pct": 6.0,
            "cumulative_series": [],
            "trades": []
        }
        res = self.client.get("/api/consensus/history?days=30&filter_mode=HIGHEST_CONFLUENCE_ONLY")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["filter_mode"], "HIGHEST_CONFLUENCE_ONLY")
        mock_stats.assert_called_once_with(days=30, filter_mode="HIGHEST_CONFLUENCE_ONLY")

    @patch.object(ConsensusTracker, "get_multi_engine_comparison", side_effect=RuntimeError("DB query timeout"))
    def test_get_multi_engine_comparison_error(self, mock_comp):
        res = self.client.get("/api/consensus/comparison")
        self.assertEqual(res.status_code, 500)
        self.assertIn("DB query timeout", res.json()["detail"])

    def test_get_consensus_weights_endpoint(self):
        res = self.client.get("/api/consensus/weights?strategy=INTRADAY")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["strategy"], "INTRADAY")
        self.assertIn("w_qlib", data["weights"])
        self.assertIn("w_legacy", data["weights"])
        self.assertAlmostEqual(data["weights"]["w_qlib"] + data["weights"]["w_legacy"], 1.0)
        self.assertIn("cusum_risk_gate", data)

    def test_trigger_consensus_weight_update_endpoint(self):
        res = self.client.post("/api/consensus/weights/update?strategy=INTRADAY&trailing_n=60")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("weights", data)
        self.assertIn("w_qlib", data["weights"])

    def test_get_consensus_learning_history_endpoint(self):
        res = self.client.get("/api/consensus/learning-history?strategy=INTRADAY&limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIsInstance(data, list)

    def test_get_consensus_shadow_candidates_endpoint(self):
        res = self.client.get("/api/consensus/shadow-candidates?strategy=INTRADAY&limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIsInstance(data, list)


if __name__ == "__main__":
    unittest.main()


