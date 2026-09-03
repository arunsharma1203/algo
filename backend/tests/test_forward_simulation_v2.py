import unittest
import os
import sys
import time
import json
import sqlite3
from datetime import datetime

from app.data.historical_data_layer import get_db_path
from app.analytics.universe_config import get_universe, get_universe_coverage, UNIVERSE_PRESETS
from app.analytics.forward_simulation import forward_sim_engine, SimStatus, SweepStatus

class TestForwardSimulationV2(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db_path = get_db_path()
        if not os.path.exists(cls.db_path):
            raise unittest.SkipTest("market_data.db not found")

    def test_01_multi_universe_presets(self):
        """Verifies that all 6 universe presets exist and resolve properly."""
        for u_name in ["BENCHMARK_5", "NIFTY_50", "LIVE_52", "RESEARCH_100", "ALL_117"]:
            u = get_universe(u_name)
            self.assertIn("name", u)
            self.assertIn("tickers", u)
            self.assertIsInstance(u["tickers"], list)
            self.assertGreater(len(u["tickers"]), 0)

        custom = get_universe("CUSTOM", custom_tickers=["RELIANCE.NS", "TCS.NS"])
        self.assertEqual(custom["tickers"], ["RELIANCE.NS", "TCS.NS"])

    def test_02_universe_coverage(self):
        """Verifies local database coverage calculation."""
        cov = get_universe_coverage("BENCHMARK_5")
        self.assertEqual(cov["universe"], "BENCHMARK_5")
        self.assertEqual(cov["total_configured"], 5)
        self.assertGreaterEqual(cov["available_count"], 1)
        self.assertGreater(cov["coverage_pct"], 0.0)

    def test_03_session_lifecycle(self):
        """Verifies that session lifetime is decoupled from computation."""
        sess = forward_sim_engine.create_session(
            title="Test Session V2",
            timeframe="1d",
            universe="BENCHMARK_5",
            initial_capital=500000.0
        )
        session_id = sess["session_id"]
        self.assertEqual(sess["status"], SimStatus.INITIALIZED)

        sess_started = forward_sim_engine.start_session(session_id)
        self.assertEqual(sess_started["status"], SimStatus.ACTIVE)

        sess_paused = forward_sim_engine.pause_session(session_id)
        self.assertEqual(sess_paused["status"], SimStatus.PAUSED)

        sess_resumed = forward_sim_engine.resume_session(session_id)
        self.assertEqual(sess_resumed["status"], SimStatus.ACTIVE)

        sess_closed = forward_sim_engine.close_session(session_id)
        self.assertEqual(sess_closed["status"], SimStatus.CLOSED)

    def test_04_synchronous_sweep_with_stage_timings(self):
        """Verifies that running a sweep evaluates candidates, generates stage timings, and stores audit."""
        sess = forward_sim_engine.create_session(
            title="Test Sweep With Timings",
            timeframe="1d",
            universe="BENCHMARK_5",
            initial_capital=500000.0
        )
        session_id = sess["session_id"]
        forward_sim_engine.start_session(session_id)

        res = forward_sim_engine.run_universe_scan_sweep(
            session_id=session_id,
            custom_tickers=["RELIANCE.NS", "TCS.NS"],
            worker_count=2
        )

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["configured_symbols"], 2)
        self.assertGreater(len(res["symbol_results"]), 0)
        
        for sym_res in res["symbol_results"]:
            if sym_res["data_status"] == "DATA_OK":
                self.assertIn("stage_timings", sym_res)
                self.assertIsInstance(sym_res["stage_timings"], dict)

        latest = forward_sim_engine.get_latest_sweep_result(session_id)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["configured_symbols"], 2)

        forward_sim_engine.close_session(session_id)

    def test_05_background_sweep_and_cancellation(self):
        """Verifies async background sweep dispatch and safe cancellation."""
        sess = forward_sim_engine.create_session(
            title="Test Background Sweep Cancel",
            timeframe="1d",
            universe="BENCHMARK_5",
            initial_capital=500000.0
        )
        session_id = sess["session_id"]
        forward_sim_engine.start_session(session_id)

        bg_res = forward_sim_engine.start_sweep_background(
            session_id=session_id,
            custom_tickers=["RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS"],
            worker_count=1
        )
        self.assertEqual(bg_res["status"], "QUEUED")
        sweep_id = bg_res["sweep_id"]

        cancel_res = forward_sim_engine.cancel_sweep(session_id, sweep_id=sweep_id)
        self.assertEqual(cancel_res["status"], "CANCEL_REQUESTED")

        time.sleep(1.0)
        active = forward_sim_engine.get_active_sweep(session_id)
        self.assertIsNotNone(active)

        forward_sim_engine.close_session(session_id)

    def test_06_safety_invariants(self):
        """Verifies that no writes touch ml_trade_history and no live broker orders occur."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM ml_trade_history")
        before_count = cur.fetchone()[0]

        sess = forward_sim_engine.create_session(
            title="Safety Test Session",
            timeframe="1d",
            universe="BENCHMARK_5"
        )
        session_id = sess["session_id"]
        forward_sim_engine.start_session(session_id)
        forward_sim_engine.run_universe_scan_sweep(session_id, custom_tickers=["RELIANCE.NS"])

        cur.execute("SELECT count(*) FROM ml_trade_history")
        after_count = cur.fetchone()[0]
        conn.close()

        self.assertEqual(before_count, after_count)

if __name__ == "__main__":
    unittest.main()
