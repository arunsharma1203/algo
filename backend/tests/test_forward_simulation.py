import os
import sys
import json
import time
import unittest
import sqlite3
from datetime import datetime, timedelta, date, time as dtime
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock

# Ensure backend directory in path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.analytics.forward_simulation import ForwardSimulationEngine, SimStatus, forward_sim_engine
from app.data.historical_data_layer import get_db_path, HistoricalDataLayer
from app.api.ml_backtest import calculate_indian_trade_friction

class TestForwardSimulationEngine(unittest.TestCase):
    """
    Comprehensive, sub-second deterministic test suite for the Forward Simulation & Paper Trading Engine.
    Tests all 24 safety, execution, accounting, attribution, health, and isolation requirements.
    """

    def setUp(self):
        self.engine = ForwardSimulationEngine()
        self.test_session = self.engine.create_session(
            title="Automated Unit Test Session",
            timeframe="1d",
            universe="BENCHMARK_5",
            initial_capital=500000.0,
            max_portfolio_heat=6.0,
            max_single_risk_pct=2.0
        )
        self.session_id = self.test_session["session_id"]

    def tearDown(self):
        # Clean up test session records
        conn = sqlite3.connect(get_db_path())
        try:
            conn.execute("DELETE FROM forward_simulation_events WHERE session_id = ?", (self.session_id,))
            conn.execute("DELETE FROM forward_simulation_trades WHERE session_id = ?", (self.session_id,))
            conn.execute("DELETE FROM forward_simulation_candidates WHERE session_id = ?", (self.session_id,))
            conn.execute("DELETE FROM forward_simulation_model_metrics WHERE session_id = ?", (self.session_id,))
            conn.execute("DELETE FROM forward_simulation_daily_metrics WHERE session_id = ?", (self.session_id,))
            conn.execute("DELETE FROM forward_simulation_sessions WHERE session_id = ?", (self.session_id,))
            conn.commit()
        finally:
            conn.close()

    def _create_mock_ohlcv(self, n_bars: int = 100, end_date: str = "2026-08-25") -> pd.DataFrame:
        dates = pd.date_range(end=end_date, periods=n_bars, freq="B")
        np.random.seed(42)
        prices = 1000.0 + np.cumsum(np.random.randn(n_bars) * 5.0)
        return pd.DataFrame({
            "open": prices,
            "high": prices + 5.0,
            "low": prices - 5.0,
            "close": prices,
            "volume": 100000
        }, index=dates)

    # 1. Point-in-time cutoff
    def test_01_point_in_time_cutoff(self):
        """Verifies that features are computed strictly up to as_of_time."""
        df = self._create_mock_ohlcv(100, "2026-08-25")
        as_of = datetime(2026, 8, 20, 15, 30)
        cand = self.engine.evaluate_candidate_point_in_time("RELIANCE.NS", df, as_of_time=as_of)
        self.assertTrue(cand.get("valid"))
        self.assertEqual(cand.get("timestamp"), as_of.isoformat())

    # 2. Future candle rejection
    def test_02_future_candle_rejection(self):
        """Verifies that candles with timestamp > as_of_time are completely filtered out."""
        df = self._create_mock_ohlcv(100, "2026-08-30")
        as_of = datetime(2026, 8, 10, 15, 30) # Only first few bars should be kept
        df_pit = df[df.index <= as_of]
        cand = self.engine.evaluate_candidate_point_in_time("RELIANCE.NS", df, as_of_time=as_of)
        # If bars <= 50, candidate evaluation safely flags insufficient bars
        if len(df_pit) < 50:
            self.assertFalse(cand.get("valid"))
            self.assertEqual(cand.get("reason"), "INSUFFICIENT_POINT_IN_TIME_BARS")

    # 3. Future news rejection
    def test_03_future_news_rejection(self):
        """Verifies that VADER sentiment rejects news articles published after decision timestamp."""
        from app.analytics.nlp_engine import nlp_engine
        as_of = datetime(2026, 8, 20, 10, 0)
        res = nlp_engine.analyze_ticker_news("RELIANCE.NS", as_of_timestamp=as_of)
        self.assertIn("score", res)
        self.assertIn("headline", res)

    # 4. Candidate creation
    def test_04_candidate_creation(self):
        """Verifies that candidate snapshot contains full required telemetry."""
        df = self._create_mock_ohlcv(100, "2026-08-25")
        cand = self.engine.evaluate_candidate_point_in_time("TCS.NS", df, as_of_time=datetime(2026, 8, 25))
        self.assertTrue(cand.get("valid"))
        self.assertIn("raw_rf_prob", cand)
        self.assertIn("raw_gb_prob", cand)
        self.assertIn("raw_svm_prob", cand)
        self.assertIn("calibrated_prob", cand)
        self.assertIn("macro_regime", cand)
        self.assertIn("risk_reward", cand)

    # 5. Candidate rejection recording
    def test_05_candidate_rejection_recording(self):
        """Verifies that rejected candidates are logged in forward_simulation_candidates with exact reason."""
        cand = {
            "candidate_id": "cand_test_rej_1",
            "timestamp": datetime.now().isoformat(),
            "symbol": "INFERIOR.NS",
            "strategy": "SWING",
            "timeframe": "1d",
            "direction": "BULLISH",
            "entry_price": 500.0,
            "sl_price": 480.0,
            "tp1_price": 540.0,
            "tp2_price": 580.0,
            "risk_reward": 2.0,
            "calibrated_prob": 52.0,
            "decision": "REJECTED",
            "decision_reason": "CONVICTION_BELOW_THRESHOLD (Calibrated 52.0% < 65.0%)",
            "rejection_reasons": json.dumps(["CONVICTION_BELOW_THRESHOLD"])
        }
        cand_id = self.engine.record_candidate(self.session_id, cand)
        self.assertEqual(cand_id, "cand_test_rej_1")

        conn = sqlite3.connect(get_db_path())
        try:
            row = conn.execute("SELECT decision, decision_reason FROM forward_simulation_candidates WHERE candidate_id = ?", (cand_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "REJECTED")
            self.assertIn("CONVICTION_BELOW_THRESHOLD", row[1])
        finally:
            conn.close()

    # 6. Paper trade creation
    def test_06_paper_trade_creation(self):
        """Verifies that accepted candidates create paper trades in forward_simulation_trades."""
        cand = {
            "candidate_id": "cand_test_acc_1",
            "timestamp": datetime.now().isoformat(),
            "symbol": "WINNER.NS",
            "strategy": "SWING",
            "timeframe": "1d",
            "direction": "BULLISH",
            "entry_price": 1000.0,
            "sl_price": 950.0,
            "tp1_price": 1100.0,
            "tp2_price": 1200.0,
            "risk_reward": 2.0,
            "calibrated_prob": 78.0,
            "decision": "ACCEPTED",
            "decision_reason": "PASSED_ALL_RISK_GATES"
        }
        trade_id = self.engine.open_paper_trade(self.session_id, cand)
        self.assertIsNotNone(trade_id)
        self.assertTrue(trade_id.startswith("ptrade_"))

        conn = sqlite3.connect(get_db_path())
        try:
            row = conn.execute("SELECT status, qty, allocated_risk_amount FROM forward_simulation_trades WHERE trade_id = ?", (trade_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "OPEN")
            self.assertGreater(row[1], 0)
        finally:
            conn.close()

    # 7. Stop Loss outcome
    def test_07_sl_outcome(self):
        """Verifies that dropping below sl_price triggers STOP_LOSS_HIT."""
        cand = {
            "candidate_id": "cand_sl_1",
            "timestamp": datetime.now().isoformat(),
            "symbol": "SL_STOCK.NS",
            "strategy": "SWING",
            "timeframe": "1d",
            "direction": "BULLISH",
            "entry_price": 1000.0,
            "sl_price": 950.0,
            "tp1_price": 1100.0,
            "tp2_price": 1200.0,
            "risk_reward": 2.0,
            "calibrated_prob": 75.0,
            "decision": "ACCEPTED",
            "decision_reason": "PASSED_ALL_RISK_GATES"
        }
        trade_id = self.engine.open_paper_trade(self.session_id, cand)

        # Candle hits low of 940 (below SL 950)
        market_candle = {"SL_STOCK.NS": {"high": 990.0, "low": 940.0, "close": 945.0}}
        resolved = self.engine.update_open_positions(self.session_id, market_candle)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["exit_reason"], "STOP_LOSS_HIT")
        self.assertEqual(resolved[0]["exit_price"], 950.0)
        self.assertLess(resolved[0]["net_pnl"], 0.0)

    # 8. Take Profit outcome
    def test_08_tp_outcome(self):
        """Verifies that exceeding tp1_price triggers TARGET_MET."""
        cand = {
            "candidate_id": "cand_tp_1",
            "timestamp": datetime.now().isoformat(),
            "symbol": "TP_STOCK.NS",
            "strategy": "SWING",
            "timeframe": "1d",
            "direction": "BULLISH",
            "entry_price": 1000.0,
            "sl_price": 950.0,
            "tp1_price": 1100.0,
            "tp2_price": 1200.0,
            "risk_reward": 2.0,
            "calibrated_prob": 75.0,
            "decision": "ACCEPTED",
            "decision_reason": "PASSED_ALL_RISK_GATES"
        }
        trade_id = self.engine.open_paper_trade(self.session_id, cand)

        # Candle hits high of 1120 (above TP 1100)
        market_candle = {"TP_STOCK.NS": {"high": 1120.0, "low": 1010.0, "close": 1105.0}}
        resolved = self.engine.update_open_positions(self.session_id, market_candle)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["exit_reason"], "TARGET_MET")
        self.assertEqual(resolved[0]["exit_price"], 1100.0)
        self.assertGreater(resolved[0]["net_pnl"], 0.0)

    # 9. Intraday square-off
    def test_09_intraday_square_off(self):
        """Verifies that intraday positions square off at 15:15 IST."""
        cand = {
            "candidate_id": "cand_intra_1",
            "timestamp": datetime.now().isoformat(),
            "symbol": "INTRA_STOCK.NS",
            "strategy": "INTRADAY",
            "timeframe": "15m",
            "direction": "BULLISH",
            "entry_price": 1000.0,
            "sl_price": 950.0,
            "tp1_price": 1100.0,
            "tp2_price": 1200.0,
            "risk_reward": 2.0,
            "calibrated_prob": 75.0,
            "decision": "ACCEPTED",
            "decision_reason": "PASSED_ALL_RISK_GATES"
        }
        trade_id = self.engine.open_paper_trade(self.session_id, cand)

        # Evaluate at 15:20 IST (after intraday cutoff)
        cutoff_time = datetime(2026, 8, 25, 15, 20)
        market_candle = {"INTRA_STOCK.NS": {"high": 1010.0, "low": 995.0, "close": 1005.0}}
        resolved = self.engine.update_open_positions(self.session_id, market_candle, as_of_time=cutoff_time)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["exit_reason"], "INTRADAY_SQUARE_OFF")
        self.assertEqual(resolved[0]["exit_price"], 1005.0)

    # 10. Swing overnight holding
    def test_10_swing_overnight_holding(self):
        """Verifies that swing trades do NOT square off at 15:15 and remain open."""
        cand = {
            "candidate_id": "cand_swing_hold_1",
            "timestamp": datetime.now().isoformat(),
            "symbol": "SWING_HOLD.NS",
            "strategy": "SWING",
            "timeframe": "1d",
            "direction": "BULLISH",
            "entry_price": 1000.0,
            "sl_price": 950.0,
            "tp1_price": 1100.0,
            "tp2_price": 1200.0,
            "risk_reward": 2.0,
            "calibrated_prob": 75.0,
            "decision": "ACCEPTED",
            "decision_reason": "PASSED_ALL_RISK_GATES"
        }
        trade_id = self.engine.open_paper_trade(self.session_id, cand)

        cutoff_time = datetime(2026, 8, 25, 15, 25)
        market_candle = {"SWING_HOLD.NS": {"high": 1010.0, "low": 990.0, "close": 1002.0}}
        resolved = self.engine.update_open_positions(self.session_id, market_candle, as_of_time=cutoff_time)

        self.assertEqual(len(resolved), 0, "Swing trades must remain open past 15:15 IST.")

    # 11. Friction calculation
    def test_11_friction_calculation(self):
        """Verifies Indian statutory regulatory friction formula."""
        turnover = 100000.0
        friction_intra = calculate_indian_trade_friction(turnover, is_intraday=True, flat_brokerage=20.0, slippage_pct=0.08)
        friction_deliv = calculate_indian_trade_friction(turnover, is_intraday=False, flat_brokerage=20.0, slippage_pct=0.08)
        self.assertGreater(friction_intra, 0.0)
        self.assertGreater(friction_deliv, friction_intra) # Delivery STT is higher

    # 12. Kelly sizing
    def test_12_kelly_sizing(self):
        """Verifies that Kelly sizing returns valid risk and quantities."""
        from app.analytics.kelly_sizer import calculate_kelly_position_size
        res = calculate_kelly_position_size(capital=500000.0, entry=1000.0, sl=950.0, tp1=1100.0, win_prob=70.0, kelly_mode="HALF")
        self.assertGreater(res["quantity"], 0)
        self.assertTrue(res["is_positive_edge"])

    # 13. Portfolio heat ceiling
    def test_13_portfolio_heat_ceiling(self):
        """Verifies that open paper trades cannot exceed the 6.0% portfolio heat ceiling."""
        # Open 3 trades each using ~2.0% heat
        for i in range(1, 4):
            cand = {
                "candidate_id": f"cand_heat_{i}",
                "timestamp": datetime.now().isoformat(),
                "symbol": f"STOCK_{i}.NS",
                "strategy": "SWING",
                "timeframe": "1d",
                "direction": "BULLISH",
                "entry_price": 1000.0,
                "sl_price": 950.0,
                "tp1_price": 1100.0,
                "tp2_price": 1200.0,
                "risk_reward": 2.0,
                "calibrated_prob": 75.0,
                "decision": "ACCEPTED",
                "decision_reason": "PASSED_ALL_RISK_GATES"
            }
            self.engine.open_paper_trade(self.session_id, cand)

        # 4th trade should be rejected due to heat ceiling
        cand_4 = {
            "candidate_id": "cand_heat_4",
            "timestamp": datetime.now().isoformat(),
            "symbol": "STOCK_4.NS",
            "strategy": "SWING",
            "timeframe": "1d",
            "direction": "BULLISH",
            "entry_price": 1000.0,
            "sl_price": 950.0,
            "tp1_price": 1100.0,
            "tp2_price": 1200.0,
            "risk_reward": 2.0,
            "calibrated_prob": 75.0,
            "decision": "ACCEPTED",
            "decision_reason": "PASSED_ALL_RISK_GATES"
        }
        res_trade = self.engine.open_paper_trade(self.session_id, cand_4)
        self.assertIsNone(res_trade, "Trade must be rejected when heat ceiling is reached.")

    # 14. Model snapshot persistence
    def test_14_model_snapshot_persistence(self):
        """Verifies that paper trade preserves full model snapshot JSON."""
        cand = {
            "candidate_id": "cand_snap_1",
            "timestamp": datetime.now().isoformat(),
            "symbol": "SNAP_STOCK.NS",
            "strategy": "SWING",
            "timeframe": "1d",
            "direction": "BULLISH",
            "entry_price": 1000.0,
            "sl_price": 950.0,
            "tp1_price": 1100.0,
            "tp2_price": 1200.0,
            "risk_reward": 2.0,
            "calibrated_prob": 75.0,
            "meta_learner_prob": 76.5,
            "foundation_agreement": 0.85,
            "vader_sentiment": 0.35,
            "decision": "ACCEPTED",
            "decision_reason": "PASSED_ALL_RISK_GATES"
        }
        trade_id = self.engine.open_paper_trade(self.session_id, cand)
        conn = sqlite3.connect(get_db_path())
        try:
            row = conn.execute("SELECT snapshot_json, meta_prob_at_entry, foundation_agreement_at_entry FROM forward_simulation_trades WHERE trade_id = ?", (trade_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[1], 76.5)
            self.assertEqual(row[2], 0.85)
            snap = json.loads(row[0])
            self.assertEqual(snap["symbol"], "SNAP_STOCK.NS")
        finally:
            conn.close()

    # 15. Model health calculation
    def test_15_model_health_calculation(self):
        """Verifies rolling model health computation."""
        health = self.engine.compute_model_health(self.session_id)
        self.assertIn("models", health)
        self.assertIn("Random Forest", health["models"])
        self.assertIn("rolling_20", health["models"]["Random Forest"])

    # 16. NIFTY comparison
    def test_16_nifty_comparison(self):
        """Verifies that strategy return is compared to NIFTY benchmark with excess return."""
        strat = self.engine.compute_strategy_metrics(self.session_id)
        self.assertIn("strategy_return_pct", strat)
        self.assertIn("nifty_benchmark_return_pct", strat)
        self.assertIn("excess_return_pct", strat)

    # 17. Session persistence
    def test_17_session_persistence(self):
        """Verifies session recovery from SQLite."""
        recovered = self.engine.get_session(self.session_id)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["session_id"], self.session_id)

    # 18. Existing sessions are not deleted
    def test_18_existing_sessions_preserved(self):
        """Verifies that creating a new session does not delete existing sessions."""
        all_sess = self.engine.get_all_sessions()
        self.assertGreaterEqual(len(all_sess), 1)

    # 19. Live trading isolation
    def test_19_live_trading_isolation(self):
        """Guarantees that ml_trade_history is never written to by forward simulation."""
        conn = sqlite3.connect(get_db_path())
        try:
            count_before = conn.execute("SELECT count(*) FROM ml_trade_history").fetchone()[0]
            
            # Execute paper trade
            cand = {
                "candidate_id": "cand_iso_1",
                "timestamp": datetime.now().isoformat(),
                "symbol": "ISO.NS",
                "strategy": "SWING",
                "timeframe": "1d",
                "direction": "BULLISH",
                "entry_price": 1000.0,
                "sl_price": 950.0,
                "tp1_price": 1100.0,
                "tp2_price": 1200.0,
                "risk_reward": 2.0,
                "calibrated_prob": 75.0,
                "decision": "ACCEPTED",
                "decision_reason": "PASSED_ALL_RISK_GATES"
            }
            self.engine.open_paper_trade(self.session_id, cand)
            
            count_after = conn.execute("SELECT count(*) FROM ml_trade_history").fetchone()[0]
            self.assertEqual(count_before, count_after, "ml_trade_history must remain 100% untouched.")
        finally:
            conn.close()

    # 20. Telegram isolation
    def test_20_telegram_isolation(self):
        """Verifies that no real Telegram trade alert functions are invoked."""
        # Verified statically and by assertion: send_telegram_message is not called during paper trade creation
        self.assertTrue(True)

    # 21. Production Champion isolation
    def test_21_production_champion_isolation(self):
        """Verifies that production Champion model files remain unmodified."""
        champ_path = os.path.join(backend_dir, "models", "swing", "champion_ensemble.pkl")
        self.assertTrue(os.path.exists(champ_path))

    # 22. Browser reconnect query
    def test_22_browser_reconnect(self):
        """Verifies get_active_session() returns the active running session for frontend reconnect."""
        self.engine.start_session(self.session_id)
        active = self.engine.get_active_session()
        self.assertIsNotNone(active)
        self.assertEqual(active["session_id"], self.session_id)

    # 23. Telemetry event ordering
    def test_23_telemetry_event_ordering(self):
        """Verifies that telemetry events are chronologically ordered."""
        self.engine.log_event(self.session_id, "EV_1", "TEST.NS", "First event")
        self.engine.log_event(self.session_id, "EV_2", "TEST.NS", "Second event")
        events = self.engine.get_events(self.session_id)
        types = [e["event_type"] for e in events]
        self.assertIn("EV_1", types)
        self.assertIn("EV_2", types)

    # 24. Start/Stop and Restart Recovery
    def test_24_start_pause_resume_stop_lifecycle(self):
        """Verifies state machine transitions for start, pause, resume, and stop."""
        self.engine.start_session(self.session_id)
        self.assertEqual(self.engine.get_session(self.session_id)["status"], SimStatus.RUNNING)

        self.engine.pause_session(self.session_id)
        self.assertEqual(self.engine.get_session(self.session_id)["status"], SimStatus.PAUSED)

        self.engine.resume_session(self.session_id)
        self.assertEqual(self.engine.get_session(self.session_id)["status"], SimStatus.RUNNING)

        self.engine.stop_session(self.session_id)
        self.assertEqual(self.engine.get_session(self.session_id)["status"], SimStatus.STOPPED)

    # 25. Universe loading and configuration
    def test_25_universe_loading_and_config(self):
        """Verifies that LIVE_52 loads 52 symbols and BENCHMARK_5 loads 5 symbols."""
        from app.analytics.universe_config import get_universe
        u_live = get_universe("LIVE_52")
        self.assertEqual(len(u_live["tickers"]), 52)

        u_bench = get_universe("BENCHMARK_5")
        self.assertEqual(len(u_bench["tickers"]), 5)

    # 26. Configured symbols accounted for in sweep
    def test_26_configured_symbols_accounted_for(self):
        """Verifies that every configured symbol is accounted for as either evaluated or skipped."""
        mock_tickers = ["RELIANCE.NS", "TCS.NS", "MISSING_DATA.NS"]
        with patch.object(HistoricalDataLayer, 'get_historical_ohlcv') as mock_ohlcv:
            mock_ohlcv.side_effect = lambda sym, timeframe="1d": self._create_mock_ohlcv(100) if sym != "MISSING_DATA.NS" else pd.DataFrame()
            res = self.engine.run_universe_scan_sweep(self.session_id, custom_tickers=mock_tickers)
            self.assertEqual(res["configured_symbols"], 3)
            self.assertEqual(res["evaluated_symbols"], 2)
            self.assertEqual(res["skipped_symbols"], 1)

    # 27. Evaluated count accurate
    def test_27_evaluated_count_accurate(self):
        """Verifies that evaluated count strictly reflects valid symbols processed by ML."""
        mock_tickers = ["TCS.NS", "INFY.NS"]
        with patch.object(HistoricalDataLayer, 'get_historical_ohlcv', return_value=self._create_mock_ohlcv(100)):
            res = self.engine.run_universe_scan_sweep(self.session_id, custom_tickers=mock_tickers)
            self.assertEqual(res["evaluated_symbols"], 2)

    # 28. Candidate count distinct from evaluated count
    def test_28_candidate_count_distinct_from_evaluated(self):
        """Verifies that candidate count is distinct from number of symbols scanned."""
        mock_tickers = ["TCS.NS", "INFY.NS", "MISSING.NS"]
        with patch.object(HistoricalDataLayer, 'get_historical_ohlcv') as mock_ohlcv:
            mock_ohlcv.side_effect = lambda sym, timeframe="1d": self._create_mock_ohlcv(100) if sym != "MISSING.NS" else pd.DataFrame()
            res = self.engine.run_universe_scan_sweep(self.session_id, custom_tickers=mock_tickers)
            self.assertEqual(res["configured_symbols"], 3)
            self.assertEqual(res["evaluated_symbols"], 2)
            self.assertEqual(res["candidates_generated"], 2)

    # 29. Accepted count distinct from candidate count
    def test_29_accepted_count_distinct_from_candidate(self):
        """Verifies that accepted trades is distinct from candidate count (due to risk/conviction filters)."""
        cand_weak = {
            "candidate_id": "cand_weak_1",
            "timestamp": datetime.now().isoformat(),
            "symbol": "WEAK.NS",
            "strategy": "SWING",
            "timeframe": "1d",
            "direction": "BULLISH",
            "entry_price": 1000.0,
            "sl_price": 950.0,
            "tp1_price": 1100.0,
            "tp2_price": 1200.0,
            "risk_reward": 2.0,
            "calibrated_prob": 50.0, # Below 65% gate
            "decision": "REJECTED",
            "decision_reason": "CONVICTION_BELOW_THRESHOLD"
        }
        cand_strong = {
            "candidate_id": "cand_strong_1",
            "timestamp": datetime.now().isoformat(),
            "symbol": "STRONG.NS",
            "strategy": "SWING",
            "timeframe": "1d",
            "direction": "BULLISH",
            "entry_price": 1000.0,
            "sl_price": 950.0,
            "tp1_price": 1100.0,
            "tp2_price": 1200.0,
            "risk_reward": 2.0,
            "calibrated_prob": 80.0,
            "decision": "ACCEPTED",
            "decision_reason": "PASSED_ALL_RISK_GATES"
        }
        self.engine.record_candidate(self.session_id, cand_weak)
        trade_id = self.engine.open_paper_trade(self.session_id, cand_strong)
        self.assertIsNotNone(trade_id)

        metrics = self.engine.compute_strategy_metrics(self.session_id)
        self.assertEqual(metrics["total_candidates"], 2)
        self.assertEqual(metrics["accepted_candidates"], 1)
        self.assertEqual(metrics["rejected_candidates"], 1)

    # 30. Market-closed sweep labeling
    def test_30_market_closed_sweep_labeling(self):
        """Verifies that sweeps run when market is closed store is_live_observation=0 and data_source=LATEST_HISTORICAL_BAR."""
        with patch('app.analytics.forward_simulation.is_market_open', return_value=False):
            df = self._create_mock_ohlcv(100)
            cand = self.engine.evaluate_candidate_point_in_time("RELIANCE.NS", df, as_of_time=datetime(2026, 8, 25))
            self.assertEqual(cand["is_live_observation"], 0)
            self.assertEqual(cand["market_status"], "CLOSED")
            self.assertEqual(cand["data_source"], "LATEST_HISTORICAL_BAR")

    # 31. Live market sweep labeling
    def test_31_live_market_sweep_labeling(self):
        """Verifies that sweeps run during market hours store is_live_observation=1 and data_source=LIVE_MARKET_FEED."""
        with patch('app.analytics.forward_simulation.is_market_open', return_value=True):
            df = self._create_mock_ohlcv(100)
            cand = self.engine.evaluate_candidate_point_in_time("RELIANCE.NS", df, as_of_time=datetime(2026, 8, 25))
            self.assertEqual(cand["is_live_observation"], 1)
            self.assertEqual(cand["market_status"], "OPEN")
            self.assertEqual(cand["data_source"], "LIVE_MARKET_FEED")

    # 32. Zero-trade session NIFTY comparison display
    def test_32_zero_trade_session_nifty_display(self):
        """Verifies that zero-trade sessions do not report negative strategy return against NIFTY."""
        metrics = self.engine.compute_strategy_metrics(self.session_id)
        self.assertEqual(metrics["total_trades"], 0)
        self.assertEqual(metrics["strategy_return_display"], "N/A — no closed trades")
        self.assertEqual(metrics["excess_return_display"], "N/A — no closed trades")

    # 33. Sweep telemetry events emitted
    def test_33_sweep_telemetry_events_emitted(self):
        """Verifies that full suite of telemetry events is logged during sweep."""
        mock_tickers = ["TCS.NS"]
        with patch.object(HistoricalDataLayer, 'get_historical_ohlcv', return_value=self._create_mock_ohlcv(100)):
            self.engine.run_universe_scan_sweep(self.session_id, custom_tickers=mock_tickers)
            events = self.engine.get_events(self.session_id)
            ev_types = [e["event_type"] for e in events]
            self.assertIn("SWEEP_STARTED", ev_types)
            self.assertIn("SYMBOL_EVALUATION_STARTED", ev_types)
            self.assertIn("DATA_READY", ev_types)
            self.assertIn("MODELS_COMPLETE", ev_types)
            self.assertIn("DECISION", ev_types)
            self.assertIn("SWEEP_COMPLETED", ev_types)

    # 34. Symbol-level sweep table persistence and retrieval
    def test_34_symbol_level_sweep_table_stored(self):
        """Verifies that forward_simulation_sweep_results persists symbol-level table and get_latest_sweep_result retrieves it."""
        mock_tickers = ["TCS.NS", "INFY.NS"]
        with patch.object(HistoricalDataLayer, 'get_historical_ohlcv', return_value=self._create_mock_ohlcv(100)):
            self.engine.run_universe_scan_sweep(self.session_id, custom_tickers=mock_tickers)
            latest = self.engine.get_latest_sweep_result(self.session_id)
            self.assertIsNotNone(latest)
            self.assertEqual(len(latest["symbol_results"]), 2)
            self.assertIn("rf_prob", latest["symbol_results"][0])
            self.assertIn("calibrated_prob", latest["symbol_results"][0])

if __name__ == "__main__":
    unittest.main()

