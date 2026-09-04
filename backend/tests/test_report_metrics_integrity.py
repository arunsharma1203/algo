import os
import json
import unittest
from datetime import datetime

class TestReportMetricsIntegrity(unittest.TestCase):
    """
    Automated forensic integrity test suite for quantitative research reporting.
    Validates that report metrics adapter, forensic analyzer, and PDF generator
    produce internally consistent, mathematically reconciled metrics with zero contradictions.
    """

    @classmethod
    def setUpClass(cls):
        from app.analytics.research_job_manager import ResearchJobManager
        from app.analytics.research_report_metrics_adapter import ResearchReportMetricsAdapter
        from app.analytics.research_forensic_analyzer import ResearchForensicAnalyzer
        from app.analytics.research_report_pdf_generator import ResearchReportPDFGenerator

        cls.manager = ResearchJobManager()
        cls.job_id = "res_20260903_231545_a6e85e"
        cls.raw_job = cls.manager.get_job(cls.job_id) or {
            "job_id": cls.job_id,
            "title": "10Y ALL_COLLECTED Universe Research",
            "research_type": "UNIVERSE_RESEARCH",
            "universe": "ALL_COLLECTED",
            "history_years": 10,
            "initial_capital": 500000.0,
            "status": "COMPLETED"
        }
        cls.raw_results = cls.manager.get_job_results(cls.job_id)

        # Adapt metrics
        cls.adapted_results = ResearchReportMetricsAdapter.adapt(cls.raw_job, cls.raw_results)
        cls.enriched_results = ResearchForensicAnalyzer.enrich_results(cls.raw_job, cls.adapted_results)
        cls.auth = cls.enriched_results.get("authoritative_metrics", {})
        cls.metrics = cls.enriched_results.get("metrics", {})
        cls.horizon = cls.enriched_results.get("horizon_forensics", {})
        cls.drawdown = cls.enriched_results.get("drawdown_forensics", {})
        cls.holdout = cls.enriched_results.get("holdout_deep_dive", {})
        cls.friction = cls.enriched_results.get("friction_breakdown", {})

    def test_total_portfolio_trades_positive_and_equals_wins_plus_losses(self):
        """Total portfolio trades > 0 and equals portfolio wins + losses."""
        tot_trades = self.auth.get("total_portfolio_trades")
        wins = self.auth.get("portfolio_winning_trades")
        losses = self.auth.get("portfolio_losing_trades")
        self.assertGreater(tot_trades, 0, "Portfolio trades must be > 0")
        self.assertEqual(wins + losses, tot_trades, "Wins + Losses must equal total portfolio trades")
        self.assertEqual(tot_trades, 255)
        self.assertEqual(wins, 124)
        self.assertEqual(losses, 131)

    def test_holdout_trades_and_wins_plus_losses(self):
        """Holdout trades == 34 and equals holdout wins (16) + losses (18)."""
        ho_trades = self.auth.get("locked_holdout_trades_count")
        ho_wins = self.auth.get("locked_holdout_winning_trades")
        ho_losses = self.auth.get("locked_holdout_losing_trades")
        self.assertEqual(ho_trades, 34, "Holdout trades must equal 34")
        self.assertEqual(ho_wins + ho_losses, ho_trades, "Holdout wins + losses must equal 34")
        self.assertEqual(ho_wins, 16)
        self.assertEqual(ho_losses, 18)

    def test_holdout_win_rate_mathematically_matches_ledger(self):
        """Holdout win rate is exactly 47.06% (16/34)."""
        ho_wr = self.auth.get("locked_holdout_win_rate_pct")
        expected_wr = round(16 / 34 * 100.0, 2)
        self.assertEqual(ho_wr, 47.06)
        self.assertEqual(ho_wr, expected_wr)

    def test_gross_pnl_minus_friction_reconciles_with_net_pnl(self):
        """Gross P&L - Friction & Slippage = Net P&L within rounding tolerance."""
        gross = self.auth.get("gross_trading_pnl")
        friction = self.auth.get("total_friction")
        net = self.auth.get("total_net_pnl")
        self.assertGreater(gross, 0.0, "Gross P&L cannot be 0.0")
        self.assertGreater(friction, 0.0, "Total friction cannot be 0.0")
        self.assertGreater(net, 0.0, "Net P&L cannot be 0.0")
        diff = abs((gross - friction) - net)
        self.assertLessEqual(diff, 0.10, f"Gross ({gross}) - Friction ({friction}) must equal Net ({net}) within 10 paise")

    def test_final_equity_reconciles_with_initial_capital_plus_net_pnl(self):
        """Final Equity = Initial Capital + Net P&L within rounding tolerance."""
        init_cap = self.auth.get("initial_capital")
        net = self.auth.get("total_net_pnl")
        final_eq = self.auth.get("final_equity")
        expected_final = init_cap + net
        diff = abs(final_eq - expected_final)
        self.assertLessEqual(diff, 0.10, f"Final equity {final_eq} must equal Initial {init_cap} + Net {net}")

    def test_actual_date_range_and_horizon_disclosure(self):
        """Actual evaluated date range is 2017-10-23 to 2026-09-03 (8.9Y)."""
        start = self.horizon.get("data_start")
        end = self.horizon.get("data_end")
        years = self.horizon.get("actual_years")
        self.assertEqual(start, "2017-10-23")
        self.assertEqual(end, "2026-09-03")
        self.assertEqual(years, 8.9)

    def test_trading_sessions_cannot_be_zero(self):
        """Trading sessions cannot be 0 for completed non-empty research (must be 2,196 bars)."""
        bars = self.horizon.get("total_trading_bars")
        self.assertIsNotNone(bars)
        self.assertGreater(bars, 0, "Trading sessions cannot be 0 bars")
        self.assertEqual(bars, 2196)

    def test_drawdown_metrics_have_explicit_definitions_and_no_zero_closed_dd(self):
        """Closed-trade DD is 21.74% (never 0.00%) and MTM DD is 58.75% with explicit definitions."""
        mtm_dd = self.drawdown.get("reported_max_drawdown_pct")
        closed_dd = self.drawdown.get("closed_trade_max_drawdown_pct")
        mtm_def = self.drawdown.get("reported_mtm_definition")
        closed_def = self.drawdown.get("closed_trade_definition")

        self.assertEqual(mtm_dd, 58.75)
        self.assertEqual(closed_dd, 21.74)
        self.assertNotEqual(closed_dd, 0.0)
        self.assertIsNotNone(mtm_def)
        self.assertIn("Mark-to-market", mtm_def)
        self.assertIsNotNone(closed_def)
        self.assertIn("closed-trade", closed_def.lower())

    def test_pdf_generation_succeeds_and_contains_valid_bytes(self):
        """PDF generation succeeds without raising and produces non-empty bytes."""
        from app.analytics.research_report_pdf_generator import ResearchReportPDFGenerator
        pdf_bytes = ResearchReportPDFGenerator.generate_pdf(self.raw_job, self.raw_results)
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertGreater(len(pdf_bytes), 10000, "PDF bytes must be substantial")
        # PDF magic bytes
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"), "Must be a valid PDF document")

    def test_distinguishes_raw_ticker_trades_vs_portfolio_trades(self):
        """Correctly separates 28,313 raw ticker trades from 255 portfolio trades."""
        raw_trades = self.auth.get("raw_ticker_trades_count")
        portfolio_trades = self.auth.get("total_portfolio_trades")
        self.assertEqual(raw_trades, 28313, "Raw ticker backtest trades must be 28,313")
        self.assertEqual(portfolio_trades, 255, "Portfolio walk-forward trades must be 255")
        self.assertNotEqual(raw_trades, portfolio_trades, "Must never substitute raw for portfolio trades")

if __name__ == "__main__":
    unittest.main()

