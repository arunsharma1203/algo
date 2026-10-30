import unittest
import os
import io
from datetime import datetime
import pytz
import sqlite3
import tempfile
from unittest.mock import patch, MagicMock
from pypdf import PdfWriter

from app.analytics.dashboard_telegram_scheduler import (
    DashboardTelegramScheduler,
    _ensure_deliveries_table,
    IST
)
from app.analytics.modularity_auditor import audit_subsystems, get_rating

class TestTelegramMorningScheduleAndSafeguards(unittest.TestCase):
    """
    Test suite verifying:
    1. 08:15 AM IST scheduling & timezone binding
    2. Time-aware morning data integrity verification (Safeguard 1)
    3. Deep pypdf binary validation (Safeguard 4)
    4. Duplicate suppression on server restarts
    5. Modularity audit scoring & resilience
    """

    def setUp(self):
        # Create temp sqlite database for delivery records
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        conn = sqlite3.connect(self.db_path)
        _ensure_deliveries_table(conn)
        conn.close()

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_ist_timezone_definition(self):
        """Verify scheduler uses canonical Asia/Kolkata timezone."""
        self.assertIsNotNone(IST)
        self.assertEqual(str(IST), "Asia/Kolkata")

    def test_time_aware_morning_verification(self):
        """
        Safeguard 1: Time-aware verification must pass for pre-market legitimate state.
        Live intraday ticks are in PRE_MARKET_STANDBY, not an error.
        """
        now_ist = datetime.now(IST)
        passed, msg, details = DashboardTelegramScheduler.verify_morning_data_integrity(now_ist)
        self.assertIn("checks", details)
        self.assertIn("pre_market_equity_ticks", details["checks"])
        self.assertEqual(details["checks"]["pre_market_equity_ticks"]["status"], "PRE_MARKET_STANDBY")
        self.assertTrue(passed)

    def test_deep_pdf_validation_valid_pdf(self):
        """
        Safeguard 4: Verify that valid multi-page PDF passes deep pypdf validation.
        """
        # Generate a real valid 2-page PDF with ReportLab or report generator
        from app.analytics.dashboard_report_pdf_generator import DashboardReportPDFGenerator
        # Generate minimal sample PDF
        sample_snapshot = {
            "snapshot_id": "test-snap-001",
            "as_of_time": "2026-09-05 08:15:00 IST",
            "market_breadth": {"nifty_500": {"evaluated_count": 500, "advances": 300, "declines": 180, "unchanged": 20, "advance_decline_ratio": 1.67}},
            "macro_regime": {"market_regime": "BULLISH_EXPANSION", "regime_score": 75},
            "institutional_flows": {"disclosure_date": "04-Sep-2026", "fii": {"net_crores": 1500.0}, "dii": {"net_crores": 800.0}, "formatted_net_total": "+2300.00 Cr"},
            "top_gainers": [{"ticker": "TCS.NS", "change_pct": 2.5}],
            "top_losers": [{"ticker": "INFY.NS", "change_pct": -1.2}],
            "swing_candidates": [{"ticker": "RELIANCE.NS", "setup_type": "BREAKOUT", "confidence_score": 85}],
            "watchlist_radar": [{"ticker": "MAZDOCK.NS", "radar_status": "ACCUMULATION", "composite_score": 78}],
            "system_audit": {"active_monitors": 0, "portfolio_heat_pct": 0.0, "environment": "SIMULATION"}
        }
        pdf_bytes = DashboardReportPDFGenerator.generate_pdf(sample_snapshot)
        self.assertGreaterEqual(len(pdf_bytes), 15000)

        is_valid, reason, details = DashboardTelegramScheduler.validate_pdf_binary(pdf_bytes, "2026-09-05")
        self.assertTrue(is_valid, f"Expected valid, got: {reason}")
        self.assertGreaterEqual(details["page_count"], 2)
        self.assertTrue(details.get("verified_title"))

    def test_deep_pdf_validation_too_small(self):
        """Safeguard 4: PDF below 15KB must fail validation."""
        tiny_pdf = b"%PDF-1.4 dummy content"
        is_valid, reason, details = DashboardTelegramScheduler.validate_pdf_binary(tiny_pdf, "2026-09-05")
        self.assertFalse(is_valid)
        self.assertIn("too small", reason)

    def test_deep_pdf_validation_invalid_header(self):
        """Safeguard 4: Binary without %PDF header must fail."""
        fake_binary = b"NOT_A_PDF_STREAM" * 1500
        is_valid, reason, details = DashboardTelegramScheduler.validate_pdf_binary(fake_binary, "2026-09-05")
        self.assertFalse(is_valid)
        self.assertIn("missing %PDF header", reason)

    def test_deep_pdf_validation_corrupt_stream(self):
        """Safeguard 4: Corrupt PDF stream must raise invalid PDF error."""
        corrupt = b"%PDF-1.4\n" + (b"\xff\xfe\x00\x01\x99" * 3500)
        is_valid, reason, details = DashboardTelegramScheduler.validate_pdf_binary(corrupt, "2026-09-05")
        self.assertFalse(is_valid)
        self.assertIn("corrupt", reason.lower())

    @patch("app.analytics.dashboard_telegram_scheduler.get_db_path")
    def test_duplicate_delivery_suppression(self, mock_get_db_path):
        """
        Verify that if report is already delivered today, pipeline suppresses duplicate.
        """
        mock_get_db_path.return_value = self.db_path
        
        now_ist = datetime.now(IST)
        today_str = now_ist.strftime("%Y-%m-%d")
        
        # Manually record a successful delivery in temp db
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO dashboard_report_deliveries 
            (report_date, snapshot_id, report_version, sent_at, telegram_message_id, status, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (today_str, "snap-001", "v1.0.0", "08:15:05 IST", "tg-12345", "DELIVERED", "Sent ok"))
        conn.commit()
        conn.close()
        
        # Execute delivery - should return duplicate_suppressed
        res = DashboardTelegramScheduler.send_daily_report(force=False)
        self.assertEqual(res.get("status"), "duplicate_suppressed")
        self.assertIn("already delivered today", res.get("message", ""))

    def test_modularity_auditor_scores(self):
        """
        Verify modularity auditor scoring rubric and outputs.
        """
        audit = audit_subsystems()
        self.assertIn("model_loading_and_lifecycle", audit)
        self.assertIn("telegram_notifications_and_reporting", audit)
        self.assertIn("data_access_and_providers", audit)

        # Model Loading must be GREEN (>= 80)
        self.assertGreaterEqual(audit["model_loading_and_lifecycle"]["total_score"], 80)
        self.assertEqual(audit["model_loading_and_lifecycle"]["rating"], "GREEN")

        # Telegram reporting must be GREEN (>= 80)
        self.assertGreaterEqual(audit["telegram_notifications_and_reporting"]["total_score"], 80)
        self.assertEqual(audit["telegram_notifications_and_reporting"]["rating"], "GREEN")

        # Data providers is diagnosed as RED (< 60) due to 16 direct callers
        self.assertLess(audit["data_access_and_providers"]["total_score"], 60)
        self.assertEqual(audit["data_access_and_providers"]["rating"], "RED")

    def test_main_scheduler_job_registered(self):
        """
        Verify that daily_dashboard_report_0815 is properly registered on the APScheduler
        with 08:15 IST schedule.
        """
        from app.main import scheduler
        job = scheduler.get_job('daily_dashboard_report_0815')
        self.assertIsNotNone(job, "Job daily_dashboard_report_0815 must be registered")
        trigger = job.trigger
        # Cron trigger attributes: fields for hour, minute, day_of_week
        self.assertEqual(str(job.trigger.timezone), "Asia/Kolkata")
        # Check hour and minute expressions
        hour_field = [f for f in trigger.fields if f.name == 'hour'][0]
        minute_field = [f for f in trigger.fields if f.name == 'minute'][0]
        self.assertIn("8", str(hour_field))
        self.assertIn("15", str(minute_field))

    @patch("requests.get")
    def test_market_search_sqlite_fallback(self, mock_requests_get):
        """
        Quick win verification: When external Yahoo Search fails, /api/market/search
        falls back gracefully to canonical local SQLite DB.
        """
        import asyncio
        import requests
        from app.api.market import search_tickers

        # Simulate Yahoo network outage
        mock_requests_get.side_effect = requests.exceptions.ConnectionError("Yahoo offline")

        # Run async search
        results = asyncio.run(search_tickers("RELIANCE"))
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0, "Should return results from local SQLite database")
        symbols = [r["symbol"] for r in results]
        self.assertIn("RELIANCE.NS", symbols)

if __name__ == "__main__":
    unittest.main()
