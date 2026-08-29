import os
import sys
import unittest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# Add backend directory to sys.path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.data.historical_data_layer import HistoricalDataLayer, get_db_path
from app.analytics.universe_config import get_universe, UNIVERSE_PRESETS, LIVE_UNIVERSE, BENCHMARK_5_UNIVERSE
from app.analytics.portfolio_walk_forward import MultiStockPortfolioWalkForwardEngine

class TestHistoricalDataLayer(unittest.TestCase):
    """
    Automated Test Suite for Centralized Historical Data Layer,
    Universe Architecture, Data Quality, and Multi-Stock Portfolio Walk-Forward.
    """

    def setUp(self):
        HistoricalDataLayer.init_schema()

    def test_schema_indexes_exist(self):
        """Verifies performance indexes and table definitions exist in SQLite."""
        import sqlite3
        conn = sqlite3.connect(get_db_path())
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = [row[0] for row in cur.fetchall()]
        conn.close()

        self.assertIn("idx_ohlcv_lookup", indexes)
        self.assertIn("idx_ml_train_lookup", indexes)

    def test_universe_config_isolation(self):
        """Verifies LIVE universe is preserved and survivorship bias disclosures are attached."""
        self.assertEqual(len(LIVE_UNIVERSE), 52)
        
        bench_info = get_universe("BENCHMARK_5")
        self.assertEqual(len(bench_info["tickers"]), 5)
        self.assertIn("survivorship_bias", bench_info)

        live_info = get_universe("LIVE_52")
        self.assertEqual(len(live_info["tickers"]), 52)
        self.assertIn("MODERATE", live_info["survivorship_bias"])

    def test_historical_coverage_report(self):
        """Tests coverage calculation and data quality status across sample universe."""
        report = HistoricalDataLayer.get_historical_coverage_report(["RELIANCE.NS", "TCS.NS"])
        self.assertEqual(report["universe_size"], 2)
        self.assertIn("coverage_pct_10y", report)
        self.assertIn("total_daily_bars_stored", report)
        self.assertEqual(len(report["tickers_detail"]), 2)

    def test_intraday_accumulated_report(self):
        """Verifies accumulated 15m observations statistics."""
        rep = HistoricalDataLayer.get_intraday_accumulated_report()
        self.assertIn("total_15m_candles", rep)
        self.assertIn("distinct_tickers", rep)
        self.assertIn("source_mode", rep)

    def test_portfolio_walk_forward_execution(self):
        """Tests Multi-Stock Portfolio Walk-Forward Engine on Benchmark subset."""
        engine = MultiStockPortfolioWalkForwardEngine(
            tickers=["RELIANCE.NS", "TCS.NS"],
            initial_capital=200000.0,
            max_portfolio_heat=6.0,
            max_single_risk_pct=2.0,
            kelly_mode="HALF",
            universe_name="BENCHMARK_5"
        )
        results = engine.run()
        
        self.assertEqual(results["status"], "success")
        self.assertEqual(results["simulation_mode"], "MULTI_STOCK_PORTFOLIO_WALK_FORWARD")
        self.assertIn("metrics", results)
        self.assertIn("equity_curve", results)
        self.assertIn("survivorship_bias_disclosure", results)
        self.assertGreater(len(results["equity_curve"]), 0)

if __name__ == '__main__':
    unittest.main()

