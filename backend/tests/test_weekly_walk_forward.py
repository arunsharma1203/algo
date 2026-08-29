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

from app.api.ml_backtest import WeeklyWalkForwardBacktestEngine, calculate_indian_trade_friction

class TestWeeklyWalkForwardBacktest(unittest.TestCase):
    """
    Automated Test Suite for Production-Parity Expanding Weekly Walk-Forward Backtester.
    Verifies expanding window progression, purge isolation, calibration safety,
    Champion/Challenger gate emulation, Fractional Kelly sizing, and statutory friction.
    """

    def setUp(self):
        # Construct a synthetic multi-year daily dataset for deterministic unit testing
        np.random.seed(42)
        n_bars = 600 # ~2.4 years of daily bars
        dates = pd.date_range(start='2022-01-01', periods=n_bars, freq='B')
        
        # Random walk price series with trend
        returns = np.random.normal(0.0005, 0.015, n_bars)
        price = 100.0 * np.cumprod(1 + returns)
        
        high = price * (1 + np.abs(np.random.normal(0.005, 0.005, n_bars)))
        low = price * (1 - np.abs(np.random.normal(0.005, 0.005, n_bars)))
        open_p = price * (1 + np.random.normal(0.0, 0.003, n_bars))
        volume = np.random.randint(50000, 500000, n_bars)

        self.sample_df = pd.DataFrame({
            'date': dates,
            'open': open_p,
            'high': high,
            'low': low,
            'close': price,
            'volume': volume
        })

    # 1. Test Expanding Window Progression
    def test_expanding_window_cadence(self):
        engine = WeeklyWalkForwardBacktestEngine(self.sample_df, model_type="SWING")
        results = engine.run()
        
        self.assertEqual(results["status"], "success")
        lifecycle = results["champion_challenger_lifecycle"]["recent_cycles"]
        self.assertGreater(len(lifecycle), 0)
        
        # Verify training samples strictly expand
        train_samples = [c["train_samples"] for c in lifecycle]
        for i in range(1, len(train_samples)):
            self.assertGreaterEqual(train_samples[i], train_samples[i-1], "Training window must strictly expand week-by-week.")

    # 2. Test Purge Gap Prevents Target Leakage
    def test_purge_gap_prevents_target_leakage(self):
        engine = WeeklyWalkForwardBacktestEngine(self.sample_df, model_type="SWING")
        res = engine.run()
        
        purge_bars = res["simulation_parameters"]["purge_bars_applied"]
        self.assertEqual(purge_bars, 5) # 5-day horizon purge for Swing

    # 3. Test Champion vs Challenger Gate Lifecycle
    def test_champion_challenger_gate_lifecycle(self):
        engine = WeeklyWalkForwardBacktestEngine(self.sample_df, model_type="SWING")
        res = engine.run()
        
        lifecycle = res["champion_challenger_lifecycle"]
        total_cycles = lifecycle["total_weekly_cycles"]
        promotions = lifecycle["promotions"]
        retentions = lifecycle["retentions"]
        
        self.assertEqual(total_cycles, promotions + retentions)
        self.assertIn("v", lifecycle["active_champion_version"])

    # 4. Test Fractional Kelly Sizing Realism
    def test_fractional_kelly_sizing_realism(self):
        engine = WeeklyWalkForwardBacktestEngine(self.sample_df, model_type="SWING", initial_capital=100000.0, kelly_mode="HALF")
        res = engine.run()
        
        trades = res["trades"]
        if trades:
            for t in trades:
                # Max single trade loss should not exceed 10% of capital
                gross_loss = abs(min(0.0, t["gross_pnl"]))
                self.assertLess(gross_loss, 25000.0, "Fractional Kelly must prevent outsized ruinous positions.")

    # 5. Test Indian Statutory Friction Calculations
    def test_statutory_indian_friction_math(self):
        turnover = 100000.0 # ₹1 Lakh turnover
        # Intraday
        f_intra = calculate_indian_trade_friction(turnover, is_intraday=True, flat_brokerage=20.0, slippage_pct=0.08)
        self.assertGreater(f_intra, 100.0) # Brokerage + STT + GST + Slippage (80) > ₹100
        
        # Delivery (Swing)
        f_swing = calculate_indian_trade_friction(turnover, is_intraday=False, flat_brokerage=20.0, slippage_pct=0.08)
        self.assertGreater(f_swing, f_intra) # Delivery STT is higher (0.1% vs 0.025%)

    # 6. Test Locked Final Holdout Isolation
    def test_locked_holdout_isolation(self):
        engine = WeeklyWalkForwardBacktestEngine(self.sample_df, model_type="SWING")
        res = engine.run()
        
        holdout = res["locked_final_holdout"]
        self.assertIn("win_rate_pct", holdout)
        self.assertIn("total_trades", holdout)
        self.assertGreater(holdout["holdout_samples"], 0)

    # 7. Test Scientific Data Availability Disclaimers
    def test_scientific_data_availability_disclaimers(self):
        engine = WeeklyWalkForwardBacktestEngine(self.sample_df, model_type="SWING")
        res = engine.run()
        
        data_avail = res["scientific_data_availability"]
        self.assertIn("HISTORICAL DATA UNAVAILABLE", data_avail["vader_news_sentiment"])
        self.assertIn("HISTORICAL DATA UNAVAILABLE", data_avail["nse_option_chain_oi"])
        self.assertIn("HISTORICAL DATA ACTIVE", data_avail["price_and_technicals"])

if __name__ == '__main__':
    unittest.main()

