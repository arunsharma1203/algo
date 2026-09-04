import os
import sys
import unittest
import sqlite3
import hashlib
import tempfile
from unittest.mock import patch, MagicMock
from datetime import datetime

# Ensure backend root is on sys.path
backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

from app.analytics.dashboard_intelligence_service import (
    DashboardIntelligenceService, REPORT_VERSION
)
from app.analytics.dashboard_report_pdf_generator import DashboardReportPDFGenerator
from app.analytics.dashboard_telegram_scheduler import DashboardTelegramScheduler
from app.analytics.telegram_notifier import send_telegram_document
from app.analytics.master_logger import MasterLogger
from app.data.historical_data_layer import get_db_path


class TestDashboardReportSystem(unittest.TestCase):
    """
    Comprehensive test suite for the AI Trading Market Command Center,
    Daily Market Report PDF Generator, and Telegram Delivery Scheduler.
    """

    @classmethod
    def setUpClass(cls):
        # Record canonical model hashes for baseline comparison
        cls.expected_intraday_hash = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
        cls.expected_swing_hash = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

    # ─────────────────────────────────────────────────────────────────────
    # 1. MARKET STATUS & INTELLIGENCE TESTS
    # ─────────────────────────────────────────────────────────────────────
    def test_market_status_calculation(self):
        status = DashboardIntelligenceService.get_market_status()
        self.assertIn("status", status)
        self.assertIn("status_label", status)
        self.assertIn("is_open", status)
        self.assertIn("ist_time", status)
        self.assertIn("report_date", status)
        self.assertIn("freshness", status)
        self.assertEqual(len(status["report_date"]), 10)  # YYYY-MM-DD

    def test_indian_markets_aggregation(self):
        indices = DashboardIntelligenceService.get_indian_markets()
        self.assertIsInstance(indices, list)
        self.assertEqual(len(indices), 5)
        names = [idx["name"] for idx in indices]
        self.assertIn("NIFTY 50", names)
        self.assertIn("BANK NIFTY", names)
        self.assertIn("INDIA VIX", names)
        for idx in indices:
            self.assertIn("freshness", idx)
            self.assertIn("trend", idx)

    def test_global_cues_aggregation(self):
        cues = DashboardIntelligenceService.get_global_cues()
        self.assertIsInstance(cues, list)
        self.assertGreaterEqual(len(cues), 10)
        regions = {c["region"] for c in cues}
        self.assertIn("US", regions)
        self.assertIn("Asia", regions)
        self.assertIn("Commodities", regions)
        self.assertIn("FX", regions)

    def test_market_regime_integration(self):
        regime = DashboardIntelligenceService.get_market_regime_status()
        self.assertIn("composite_regime", regime)
        self.assertIn("nifty_trend_long", regime)
        self.assertIn("nifty_trend_short", regime)
        self.assertIn("vix_status", regime)
        self.assertIn(regime["composite_regime"], [
            "STRONG BULLISH", "CAUTIOUS BULLISH", "NEUTRAL", "CAUTIOUS BEARISH", "BEARISH"
        ])

    def test_market_breadth_calculation(self):
        breadth = DashboardIntelligenceService.get_market_breadth()
        self.assertIn("universe", breadth)
        self.assertIn("advances", breadth)
        self.assertIn("declines", breadth)
        self.assertIn("ad_ratio", breadth)
        self.assertIn("above_20_dma_pct", breadth)
        self.assertIn("above_50_dma_pct", breadth)
        self.assertIn("above_200_dma_pct", breadth)
        self.assertIn("highs_52w", breadth)
        self.assertIn("lows_52w", breadth)

    def test_sector_performance_ranking(self):
        sectors = DashboardIntelligenceService.get_sector_performance()
        self.assertIn("sectors", sectors)
        self.assertIn("leaders", sectors)
        self.assertIn("laggards", sectors)
        self.assertIsInstance(sectors["sectors"], list)
        self.assertGreater(len(sectors["sectors"]), 0)

    def test_volatility_risk_radar(self):
        dummy_indian = [{"name": "INDIA VIX", "ltp": 14.5}]
        dummy_global = [
            {"name": "US VIX", "value": 15.2},
            {"name": "Brent Crude", "value": 78.5},
            {"name": "USD / INR", "value": 83.9}
        ]
        radar = DashboardIntelligenceService.get_volatility_risk_radar(dummy_indian, dummy_global)
        self.assertIn("composite_risk", radar)
        self.assertIn(radar["composite_risk"], ["LOW", "MODERATE", "HIGH"])
        self.assertIn("contributing_factors", radar)

    def test_news_intelligence_structure(self):
        news = DashboardIntelligenceService.get_news_intelligence()
        self.assertIn("articles", news)
        self.assertIn("overall_sentiment", news)
        self.assertIn("bullish_count", news)
        self.assertIn("stocks_in_focus", news)
        self.assertIn("ai_interpretation", news)

    def test_ai_opportunities_isolation(self):
        ai_ops = DashboardIntelligenceService.get_ai_opportunities()
        self.assertIn("disclaimer", ai_ops)
        self.assertEqual(ai_ops["disclaimer"], "VIRTUAL AI RECOMMENDATIONS — NOT LIVE POSITIONS")
        self.assertIn("intraday", ai_ops)
        self.assertIn("swing", ai_ops)
        for op in ai_ops["intraday"].get("opportunities", []):
            self.assertEqual(op.get("classification"), "VIRTUAL RECOMMENDATION")
        for op in ai_ops["swing"].get("opportunities", []):
            self.assertEqual(op.get("classification"), "VIRTUAL RECOMMENDATION")

    def test_ai_market_summary_grounding(self):
        regime = {"composite_regime": "CAUTIOUS BULLISH"}
        breadth = {"pct_advancing": 62.0, "above_200_dma_pct": 58.0}
        sectors = {"leaders": [{"name": "NIFTY BANK"}], "laggards": [{"name": "NIFTY METAL"}]}
        risk_radar = {"composite_risk": "LOW"}
        news = {"overall_sentiment": "BULLISH"}

        summary = DashboardIntelligenceService.get_ai_market_summary(
            regime, breadth, sectors, risk_radar, news
        )
        self.assertIn("market_view", summary)
        self.assertEqual(summary["market_view"], "CAUTIOUS BULLISH")
        self.assertIn("supporting_factors", summary)
        self.assertIn("headwinds", summary)
        self.assertIn("ai_observation", summary)
        self.assertGreater(len(summary["supporting_factors"]), 0)

    def test_dashboard_snapshot_caching(self):
        snap1 = DashboardIntelligenceService.get_dashboard_snapshot(force_refresh=True)
        snap2 = DashboardIntelligenceService.get_dashboard_snapshot(force_refresh=False)
        self.assertEqual(snap1["snapshot_id"], snap2["snapshot_id"])
        self.assertEqual(snap1["report_version"], REPORT_VERSION)

    def test_partial_failure_isolation(self):
        # Simulate Yahoo failure
        with patch('yfinance.download', side_effect=Exception("Network Timeout")):
            indices = DashboardIntelligenceService.get_indian_markets()
            self.assertEqual(len(indices), 5)
            for idx in indices:
                self.assertEqual(idx["freshness"], "UNAVAILABLE")

    # ─────────────────────────────────────────────────────────────────────
    # 2. PDF REPORT GENERATOR TESTS
    # ─────────────────────────────────────────────────────────────────────
    def test_pdf_generation_validity(self):
        snapshot = DashboardIntelligenceService.get_dashboard_snapshot(force_refresh=False)
        pdf_bytes = DashboardReportPDFGenerator.generate_pdf(snapshot)
        
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"), "Generated file must start with valid PDF magic bytes.")
        self.assertGreater(len(pdf_bytes), 15000, "Publication PDF should be substantial (>15 KB).")

    def test_pdf_zero_research_contamination(self):
        with patch('app.analytics.research_orchestrator.research_orchestrator') as mock_orch:
            snapshot = DashboardIntelligenceService.get_dashboard_snapshot(force_refresh=False)
            pdf_bytes = DashboardReportPDFGenerator.generate_pdf(snapshot)
            self.assertFalse(mock_orch.start_orchestrator_daemon.called)
            self.assertFalse(mock_orch.register_scheduled_jobs.called)

    # ─────────────────────────────────────────────────────────────────────
    # 3. TELEGRAM DELIVERY & DEDUPLICATION TESTS
    # ─────────────────────────────────────────────────────────────────────
    @patch('requests.post')
    def test_send_telegram_document_mock(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        # Mock app_settings with test credentials
        with patch('sqlite3.connect') as mock_conn:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.side_effect = [("dummy_token",), ("dummy_chat_id",)]
            mock_conn.return_value.cursor.return_value = mock_cursor

            res = send_telegram_document(b"%PDF-dummy", "Test.pdf", caption="Test Caption")
            self.assertTrue(res)
            self.assertTrue(mock_post.called)

    def test_telegram_deduplication(self):
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Insert artificial delivered record in temp db or market_data.db
        conn = sqlite3.connect(get_db_path(), timeout=5.0)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_report_deliveries (
                report_date TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                report_version TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                telegram_message_id TEXT,
                status TEXT NOT NULL
            );
        """)
        conn.execute("""
            INSERT OR REPLACE INTO dashboard_report_deliveries 
            (report_date, snapshot_id, report_version, sent_at, telegram_message_id, status)
            VALUES (?, 'snap_test_dedup', 'DASHBOARD_REPORT_V1', ?, '12345', 'DELIVERED')
        """, (today, datetime.now().isoformat()))
        conn.commit()
        conn.close()

        # Calling without force should suppress
        res = DashboardTelegramScheduler.send_daily_report(force=False)
        self.assertEqual(res["status"], "duplicate_suppressed")
        self.assertEqual(res["report_date"], today)

    # ─────────────────────────────────────────────────────────────────────
    # 4. SYSTEM INVARIANTS (BEFORE / AFTER COMPARISON)
    # ─────────────────────────────────────────────────────────────────────
    def test_champion_hashes_strictly_unchanged(self):
        intraday_path = os.path.join(backend_path, 'models/intraday/champion_ensemble.pkl')
        swing_path = os.path.join(backend_path, 'models/swing/champion_ensemble.pkl')

        with open(intraday_path, 'rb') as f:
            intraday_hash = hashlib.sha256(f.read()).hexdigest()
        with open(swing_path, 'rb') as f:
            swing_hash = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(intraday_hash, self.expected_intraday_hash, "Champion Intraday hash must remain strictly unchanged!")
        self.assertEqual(swing_hash, self.expected_swing_hash, "Champion Swing hash must remain strictly unchanged!")

    def test_trade_history_untouched(self):
        conn = sqlite3.connect(get_db_path(), timeout=5.0)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ml_trade_history")
        total_trades = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ml_trade_history WHERE position_type IN ('PAPER_POSITION', 'LIVE_POSITION') AND status='OPEN'")
        live_pos = cur.fetchone()[0]
        conn.close()

        self.assertGreaterEqual(total_trades, 68)
        self.assertEqual(live_pos, 0, "No live/paper broker positions should exist.")

    def test_portfolio_heat_untouched(self):
        from app.analytics.kelly_sizer import get_portfolio_heat_status
        heat_status = get_portfolio_heat_status()
        self.assertEqual(heat_status.get("current_heat_pct"), 0.0)
        self.assertEqual(heat_status.get("status"), "NORMAL")


if __name__ == '__main__':
    unittest.main()
