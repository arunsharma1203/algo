import os
import sys
import unittest
import time
from typing import Dict, Any, List

# Ensure backend directory is in sys.path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.data.historical_data_layer import HistoricalDataLayer
from app.analytics.portfolio_walk_forward import MultiStockPortfolioWalkForwardEngine
from sklearn.linear_model import LogisticRegression

class DeterministicModelFactory:
    """Fast deterministic model factory for lightweight testing."""
    def __call__(self):
        return LogisticRegression(C=1.0, random_state=42, max_iter=200)

class TestPortfolioWalkForwardFix(unittest.TestCase):
    """
    Regression Test Suite for Multi-Stock Portfolio Walk-Forward Execution Engine.
    Verifies that the complete portfolio simulation, position management, candidate discovery,
    equity curve generation, and telemetry execute inside EVERY rebalance cycle.
    """

    def setUp(self):
        HistoricalDataLayer.init_schema()
        self.test_tickers = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS"]

    def test_portfolio_simulation_executes_inside_every_rebalance_cycle(self):
        """
        Verifies that:
        1. total_cycles > 1
        2. actual_simulation_cycles == total_cycles
        3. equity_curve has points spanning all simulation bars
        4. CYCLE_COMPLETED telemetry is emitted for every cycle
        5. Trades and accounting execute across multiple distinct cycles
        """
        telemetry_events = []

        def callback(event: Dict[str, Any]):
            telemetry_events.append(event)

        engine = MultiStockPortfolioWalkForwardEngine(
            tickers=self.test_tickers,
            initial_capital=500000.0,
            max_portfolio_heat=6.0,
            kelly_mode="HALF",
            universe_name="BENCHMARK_5",
            progress_callback=callback,
            worker_count=1,
            model_factory=DeterministicModelFactory()
        )

        results = engine.run()

        self.assertEqual(results["status"], "success")
        lifecycle = results["champion_challenger_lifecycle"]
        total_weekly_cycles = lifecycle["total_weekly_cycles"]

        # 1. Verify multiple cycles executed
        self.assertGreater(total_weekly_cycles, 5, "Simulation must execute across multiple weekly cycles.")

        # 2. Verify equity curve contains daily progression across all bars
        equity_curve = results["equity_curve"]
        self.assertGreater(len(equity_curve), total_weekly_cycles, "Equity curve must record points for every trading bar.")

        # 3. Verify telemetry events
        cycle_completed_events = [e for e in telemetry_events if e.get("event_type") == "CYCLE_COMPLETED"]
        self.assertEqual(len(cycle_completed_events), total_weekly_cycles, "CYCLE_COMPLETED must be emitted once per weekly cycle.")

        # 4. Verify progress percentages strictly advance
        progresses = [e.get("progress_percent", 0) for e in cycle_completed_events]
        for i in range(1, len(progresses)):
            self.assertGreaterEqual(progresses[i], progresses[i-1], "Progress must monotonically increase.")

        # 5. Verify trades and metrics
        trades = results["trades"]
        metrics = results["metrics"]
        self.assertIn("total_trades", metrics)
        self.assertIn("win_rate", metrics)
        self.assertIn("profit_factor", metrics)
        self.assertIn("max_drawdown", metrics)
        self.assertIn("sharpe_ratio", metrics)

        # 6. Verify trade dates span multiple distinct periods if trades occurred
        if len(trades) > 1:
            entry_dates = set(t["entry_date"] for t in trades)
            self.assertGreater(len(entry_dates), 1, "Trades must be generated across multiple distinct rebalance dates.")

    def test_parallel_vs_sequential_equivalence(self):
        """
        Executes worker_count=1 (sequential) vs worker_count=4 (parallel) on identical inputs.
        Verifies numerical and structural equivalence of trades, metrics, and lifecycle.
        """
        tickers = ["RELIANCE.NS", "TCS.NS"]

        # Run Sequential
        engine_seq = MultiStockPortfolioWalkForwardEngine(
            tickers=tickers,
            initial_capital=500000.0,
            worker_count=1,
            model_factory=DeterministicModelFactory()
        )
        res_seq = engine_seq.run()

        # Run Parallel (4 workers)
        engine_par = MultiStockPortfolioWalkForwardEngine(
            tickers=tickers,
            initial_capital=500000.0,
            worker_count=4,
            model_factory=DeterministicModelFactory()
        )
        res_par = engine_par.run()

        # Verify exact equivalence
        self.assertEqual(res_seq["metrics"]["total_trades"], res_par["metrics"]["total_trades"])
        self.assertEqual(res_seq["metrics"]["win_rate"], res_par["metrics"]["win_rate"])
        self.assertEqual(res_seq["metrics"]["total_pnl"], res_par["metrics"]["total_pnl"])
        self.assertEqual(res_seq["metrics"]["profit_factor"], res_par["metrics"]["profit_factor"])
        self.assertEqual(len(res_seq["equity_curve"]), len(res_par["equity_curve"]))
        self.assertEqual(
            res_seq["champion_challenger_lifecycle"]["total_weekly_cycles"],
            res_par["champion_challenger_lifecycle"]["total_weekly_cycles"]
        )

if __name__ == "__main__":
    unittest.main()
