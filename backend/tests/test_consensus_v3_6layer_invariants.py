import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.analytics.consensus_v3.arena_execution_bridge import (  # noqa: E402
    MODE_DUAL_FUNDED_ARENA,
    ConsensusArenaExecutionBridge,
)
from app.analytics.consensus_v3.baseline_engine import BaselineEngine  # noqa: E402
from app.analytics.consensus_v3.consensus_arbiter import ConsensusArbiter  # noqa: E402
from app.analytics.consensus_v3.legacy_payoff_estimator import LegacyPayoffEstimator  # noqa: E402
from app.analytics.consensus_v3.math_and_costs import (  # noqa: E402
    compute_nse_cnc_charges,
    compute_nse_mis_charges,
    compute_whole_shares,
    round_nse_eq_tick,
)
from app.analytics.consensus_v3.qlib_horizon_forecaster import QlibHorizonForecaster  # noqa: E402


def _seed_synthetic_fno_db(db_path: str, corrupt_after: str = None) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE fno_universe_cache (symbol TEXT PRIMARY KEY);")
    cur.execute("CREATE TABLE ohlcv (ticker TEXT, date TEXT, timeframe TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL);")

    tickers = [f"FNO{i:02d}.NS" for i in range(1, 42)]
    for t in tickers:
        cur.execute("INSERT INTO fno_universe_cache (symbol) VALUES (?)", (t.replace(".NS", ""),))

    base_dt = datetime(2026, 10, 1)
    day_list = [(base_dt + timedelta(days=k)).strftime("%Y-%m-%d") for k in range(22)]
    hms = ["09:15", "09:30", "09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30", "12:00", "12:30", "13:00", "14:00", "14:45", "15:00", "15:15"]

    rows = []
    for idx_t, sym in enumerate(tickers):
        px = 500.0 + idx_t * 15.0
        for di, d_s in enumerate(day_list):
            mult = 4.2 if (corrupt_after and d_s > corrupt_after) else 1.0
            for bi, hm in enumerate(hms):
                # On day 18 at 10:15, make FNO01..FNO06 sweep PDH and close bearish!
                bo = (px + (bi * 0.4) + (di % 3) * 1.2) * mult
                bh = bo * 1.012
                bl = bo * 0.992
                bc = bo * 1.001
                bv = 180000.0
                if di == 18 and hm == "10:15" and idx_t < 6:
                    bh = bo * 1.032  # sweeps PDH
                    bl = bo * 0.994
                    bc = bo * 0.995  # closes near low (< PDH, CLV <= 0.30)
                    bv = 850000.0    # RVOL >= 2.0
                rows.append((sym, f"{d_s} {hm}:00", "15m", bo, bh, bl, bc, bv))
    cur.executemany("INSERT INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


class TestConsensusV3SixLayerInvariants(unittest.TestCase):
    def test_tick_rounding_whole_shares_and_exact_fees(self):
        self.assertEqual(round_nse_eq_tick(100.03), 100.05)
        self.assertEqual(round_nse_eq_tick(100.01), 100.00)
        self.assertEqual(compute_whole_shares(99999.0, 1000.0, 100000.0, 500000.0), 99)
        self.assertEqual(compute_whole_shares(500.0, 1000.0, 100000.0, 500000.0), 0)

        mis = compute_nse_mis_charges(1000.0, 990.0, 100, "SELL", is_fno_eligible=True, passive_entry=True)
        cnc = compute_nse_cnc_charges(1000.0, 1050.0, 50, is_fno_eligible=True)
        self.assertGreater(mis["total_cost_inr"], 100.0)
        self.assertGreater(cnc["total_cost_inr"], 200.0)  # includes 20 bps STT + DP debit + slippage

    def test_boot_disarmed_pending_cash_reservation_postonly_and_1515_squareoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "test_arena.db")
            _seed_synthetic_fno_db(db_p)

            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="unit_test_arena")
            # AGENTS.md Rule 2: Must boot strictly DISARMED (`simulation_mode = True`)
            self.assertTrue(bridge.simulation_mode)
            acct0 = bridge.configure_arena_capital(400000.0, MODE_DUAL_FUNDED_ARENA, reset_ledger=True)
            self.assertEqual(acct0["funded_capital_inr"], 400000.0)
            self.assertEqual(acct0["free_unreserved_cash_inr"], 400000.0)

            # Submit 6 simultaneous ₹1,00,000 signals -> Pending-Order Cash Reservation MUST cap at 4 orders (₹4,00,000)!
            t0 = datetime(2026, 10, 19, 10, 15, 5)
            sigs = [
                {
                    "book": "BOOK2_INTRADAY_MIS_SNIFFER",
                    "ticker": f"FNO0{i}.NS",
                    "symbol": f"FNO0{i}",
                    "instrument_key": f"NSE_EQ|INE00000000{i}",
                    "direction": "SELL",
                    "signal_close_px": 995.0,
                    "limit_entry_px": 1000.0,
                    "stop_loss_px": 1018.0,
                    "scheduled_exit_hm": "15:15",
                    "liq_1pct_bar_inr": 500000.0,
                }
                for i in range(1, 7)
            ]
            created = bridge.reserve_book2_pending_limit_orders(sigs, now_dt=t0)
            self.assertEqual(len(created), 4)
            for ord_rec in created:
                self.assertIsInstance(ord_rec["shares"], int)
                self.assertEqual(ord_rec["shares"], 100)
                self.assertEqual(ord_rec["reserved_cash_inr"], 100000.0)

            acct_res = bridge.get_arena_accounting(now_dt=t0)
            self.assertEqual(acct_res["pending_reserved_cash_inr"], 400000.0)
            self.assertEqual(acct_res["free_unreserved_cash_inr"], 0.0)
            self.assertTrue(acct_res["cash_conservation_invariant_ok"])

            # Reconcile at 10:20: FNO01 trades through 1000.0 (`1000.50 >= 1000.30`), others stay below 1000.0
            rec1 = bridge.reconcile_orders_and_positions(
                live_quotes={"FNO01.NS": {"price": 1000.50}, "FNO02.NS": {"price": 998.0}},
                now_dt=datetime(2026, 10, 19, 10, 20, 0),
            )
            self.assertEqual(rec1["filled"], 1)

            # At 10:31 (past 15m TIF), remaining 3 unfilled orders MUST expire and release ₹3,00,000 cash!
            rec2 = bridge.reconcile_orders_and_positions(
                live_quotes={"FNO01.NS": {"price": 992.0}},
                now_dt=datetime(2026, 10, 19, 10, 31, 0),
            )
            self.assertEqual(rec2["expired"], 3)
            acct_after_exp = bridge.get_arena_accounting(now_dt=datetime(2026, 10, 19, 10, 31, 0))
            self.assertEqual(acct_after_exp["pending_reserved_cash_inr"], 0.0)
            self.assertEqual(acct_after_exp["invested_notional_inr"], 100000.0)
            self.assertGreater(acct_after_exp["free_unreserved_cash_inr"], 300000.0)

            # At 15:15 IST, mandatory NSE MIS square-off MUST close FNO01.NS and deduct exact fees!
            rec3 = bridge.reconcile_orders_and_positions(
                live_quotes={"FNO01.NS": {"price": 988.0}},
                now_dt=datetime(2026, 10, 19, 15, 15, 5),
            )
            self.assertEqual(rec3["closed"], 1)
            pos_bundle = bridge.get_positions_and_reservations()
            self.assertEqual(len(pos_bundle["open_positions"]), 0)
            self.assertEqual(len(pos_bundle["closed_positions"]), 1)
            closed_tr = pos_bundle["closed_positions"][0]
            self.assertEqual(closed_tr["exit_reason"], "MANDATORY_1515_MIS_SQUAREOFF")
            self.assertGreater(closed_tr["fees_inr"], 0.0)
            self.assertGreater(closed_tr["net_pnl_inr"], 900.0)

    def test_future_perturbation_immunity_and_qlib_legacy_independence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_clean = os.path.join(tmp, "clean.db")
            db_corrupt = os.path.join(tmp, "corrupt.db")
            models_clean = os.path.join(tmp, "m_clean")
            models_corrupt = os.path.join(tmp, "m_corrupt")

            cutoff = "2026-10-19"
            _seed_synthetic_fno_db(db_clean, corrupt_after=None)
            _seed_synthetic_fno_db(db_corrupt, corrupt_after=cutoff)

            c1 = sqlite3.connect(db_clean)
            c2 = sqlite3.connect(db_corrupt)
            try:
                qf1 = QlibHorizonForecaster(model_dir=models_clean)
                qf2 = QlibHorizonForecaster(model_dir=models_corrupt)
                lf1 = LegacyPayoffEstimator(model_dir=models_clean)
                lf2 = LegacyPayoffEstimator(model_dir=models_corrupt)

                q_art1 = qf1.fit_book2_15m_causal(c1, max_train_date="2026-10-18")
                q_art2 = qf2.fit_book2_15m_causal(c2, max_train_date="2026-10-18")
                l_art1 = lf1.fit_book2_independent_causal(c1, max_train_date="2026-10-18")
                l_art2 = lf2.fit_book2_independent_causal(c2, max_train_date="2026-10-18")

                # Architectural Independence Invariant
                self.assertEqual(l_art1["qlib_inputs_used"], 0)
                self.assertEqual(q_art1["artifact_sha256"], q_art2["artifact_sha256"])
                self.assertEqual(l_art1["artifact_sha256"], l_art2["artifact_sha256"])

                # Snapshot & Arbitration on 2026-10-19 10:15:00 MUST be bit-for-bit identical despite 4.2x future corruption!
                s1 = BaselineEngine.build_book2_15m_snapshot(c1, as_of_ts="2026-10-19 10:15:00")
                s2 = BaselineEngine.build_book2_15m_snapshot(c2, as_of_ts="2026-10-19 10:15:00")
                qf1.score_book2_snapshot(s1, artifact=q_art1)
                qf2.score_book2_snapshot(s2, artifact=q_art2)
                a1 = ConsensusArbiter.arbitrate_book2_15m_sniffer(s1, lf1, l_art1)
                a2 = ConsensusArbiter.arbitrate_book2_15m_sniffer(s2, lf2, l_art2)

                self.assertEqual(a1["approved_signals"], a2["approved_signals"])
                self.assertEqual(a1["abstained_signals"], a2["abstained_signals"])
            finally:
                c1.close()
                c2.close()


if __name__ == "__main__":
    unittest.main()
