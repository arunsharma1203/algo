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

            # Verify V3 era-isolated equity curve & calendar reflects the closed trade and resets cleanly to 0 on reset_ledger=True
            eq_payload = bridge.get_equity_curve_and_calendar()
            self.assertEqual(eq_payload["closed_trades_count"], 1)
            self.assertEqual(len(eq_payload["daily_sessions"]), 1)
            self.assertAlmostEqual(eq_payload["equity_curve"][-1]["cum_net_pnl_inr"], closed_tr["net_pnl_inr"], places=2)

            bridge.configure_arena_capital(100000.0, MODE_DUAL_FUNDED_ARENA, reset_ledger=True)
            eq_reset = bridge.get_equity_curve_and_calendar()
            self.assertEqual(eq_reset["closed_trades_count"], 0)
            self.assertEqual(eq_reset["accounting"]["free_unreserved_cash_inr"], 100000.0)
            self.assertEqual(eq_reset["equity_curve"][-1]["equity_inr"], 100000.0)
            self.assertEqual(eq_reset["equity_curve"][-1]["cum_net_pnl_inr"], 0.0)

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
                # Also verify live path (`as_of_ts=None`) executes cleanly without missing imports
                live_snap = BaselineEngine.build_book2_15m_snapshot(c1, as_of_ts=None)
                self.assertIn("market_context", live_snap)
            finally:
                c1.close()
                c2.close()

    def test_custom_wallet_capital_mis_buying_power_and_book1_swing_rebalance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "wallet_b1.db")
            _seed_synthetic_fno_db(db_p)
            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="wallet_test")

            # 1. Reconfigure from ₹4,00,000 down to ₹50,000 with 3.0x MIS buying power
            acct = bridge.configure_arena_capital(
                funded_capital_inr=50000.0,
                execution_mode="DUAL_FUNDED_CASH_ARENA",
                margin_mode="MIS_3X",
                max_risk_per_trade_pct=0.50,
                reset_ledger=True,
            )
            self.assertEqual(acct["funded_capital_inr"], 50000.0)
            self.assertEqual(acct["intraday_mis_leverage"], 3.0)
            self.assertEqual(acct["book1_slots"]["max"], 2)
            self.assertEqual(acct["intraday_mis_buying_power_inr"], 150000.0)

            # 2. Book-2 order with MIS_3X: offline margin must tag UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN & cap SL risk <= ₹250 (0.50% of ₹50k)
            sigs = [
                {
                    "book": "BOOK2_INTRADAY_MIS_SNIFFER",
                    "ticker": "FNO01.NS",
                    "symbol": "FNO01",
                    "instrument_key": "NSE_EQ|INE000001001",
                    "direction": "SELL",
                    "signal_close_px": 500.0,
                    "limit_entry_px": 500.0,
                    "stop_loss_px": 510.0,  # ₹10/share stop distance -> ₹250 risk budget caps at 25 shares!
                    "scheduled_exit_hm": "15:15",
                    "liq_1pct_bar_inr": 500000.0,
                    "telemetry": {"atr_pct": 2.0},
                }
            ]
            created = bridge.reserve_book2_pending_limit_orders(
                sigs, now_dt=datetime(2026, 10, 19, 10, 0, 0), allow_broker_probe=False
            )
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0]["shares"], 25)
            self.assertEqual(created[0]["margin_source"], "UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN")
            self.assertLess(created[0]["reserved_cash_inr"], created[0]["exposure_notional_inr"])
            acct_after = bridge.get_arena_accounting(now_dt=datetime(2026, 10, 19, 10, 1, 0))
            self.assertTrue(acct_after["cash_conservation_invariant_ok"])

            # 3. Book-1 20d Scheduled CNC Swing Rebalance: open 2 whole-share CNC slots, then step down to 0 in Bear Shield
            snap_bull = {
                "as_of_date": "2026-10-19",
                "candidates": [
                    {"ticker": "FNO02.NS", "close": 1000.0, "sma_alignment_ok": True, "qlib_pct_rank": 0.95},
                    {"ticker": "FNO03.NS", "close": 1250.0, "sma_alignment_ok": True, "qlib_pct_rank": 0.91},
                ],
            }
            arb_bull = {
                "as_of_date": "2026-10-19",
                "macro_shield": {"regime": "BULL_FULL_DEPLOYMENT", "target_slots": 8},
                "approved_rebalance_leaders": [
                    {"ticker": "FNO02.NS", "symbol": "FNO02", "instrument_key": "NSE_EQ|INE000001002", "close_px": 1000.0},
                    {"ticker": "FNO03.NS", "symbol": "FNO03", "instrument_key": "NSE_EQ|INE000001003", "close_px": 1250.0},
                ],
            }
            rb1 = bridge.execute_book1_scheduled_rebalance(snap_bull, arb_bull, force=True, now_dt=datetime(2026, 10, 19, 10, 5, 0))
            self.assertEqual(rb1["status"], "REBALANCED")
            self.assertEqual(rb1["opened"], 2)
            self.assertTrue(bridge.get_arena_accounting(now_dt=datetime(2026, 10, 19, 10, 6, 0))["cash_conservation_invariant_ok"])

            # Macro Bear Shield triggers immediate protective step-down to 0 CNC slots even before 20 days!
            arb_bear = {
                "as_of_date": "2026-10-20",
                "macro_shield": {"regime": "BEAR_CASH_SHIELD", "target_slots": 0},
                "approved_rebalance_leaders": [],
            }
            rb2 = bridge.execute_book1_scheduled_rebalance(snap_bull, arb_bear, force=False, now_dt=datetime(2026, 10, 20, 9, 20, 0))
            self.assertEqual(rb2["status"], "REBALANCED")
            self.assertEqual(rb2["closed"], 2)

    def test_realtime_slot_floor_wick_fill_and_uncompleted_bar_causality(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "rt_slot.db")
            conn = sqlite3.connect(db_p)
            conn.execute("CREATE TABLE fno_universe_cache (symbol TEXT PRIMARY KEY);")
            conn.execute("CREATE TABLE ohlcv (ticker TEXT, date TEXT, timeframe TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL);")
            conn.execute("INSERT INTO fno_universe_cache VALUES ('WICK01'), ('GAP01');")
            # Pre-seed the 11:00:00 (11:00–11:15) 15m bar:
            # WICK01 opens at 498 (< 500 limit, post-only valid) and wicks to 501.0 (>= 500 * 1.0003)
            # GAP01 opens at 502 (>= 500 limit -> post-only gap violation!)
            conn.execute("INSERT INTO ohlcv VALUES ('WICK01.NS', '2026-10-19 11:00:00', '15m', 498.0, 501.0, 495.0, 497.0, 200000);")
            conn.execute("INSERT INTO ohlcv VALUES ('GAP01.NS',  '2026-10-19 11:00:00', '15m', 502.0, 504.0, 496.0, 498.0, 200000);")
            conn.commit()
            conn.close()

            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="rt_slot_test")
            bridge.configure_arena_capital(400000.0, "BOOK2_INTRADAY_MIS_SNIFFER", reset_ledger=True)

            sigs = [
                {
                    "book": "BOOK2_INTRADAY_MIS_SNIFFER",
                    "ticker": "WICK01.NS",
                    "symbol": "WICK01",
                    "instrument_key": "NSE_EQ|INE000009001",
                    "bar_dt": "2026-10-19 10:45:00",
                    "direction": "SELL",
                    "signal_close_px": 498.0,
                    "limit_entry_px": 500.0,
                    "stop_loss_px": 508.0,
                    "scheduled_exit_hm": "15:15",
                    "liq_1pct_bar_inr": 500000.0,
                },
                {
                    "book": "BOOK2_INTRADAY_MIS_SNIFFER",
                    "ticker": "GAP01.NS",
                    "symbol": "GAP01",
                    "instrument_key": "NSE_EQ|INE000009002",
                    "bar_dt": "2026-10-19 10:45:00",
                    "direction": "SELL",
                    "signal_close_px": 498.0,
                    "limit_entry_px": 500.0,
                    "stop_loss_px": 508.0,
                    "scheduled_exit_hm": "15:15",
                    "liq_1pct_bar_inr": 500000.0,
                },
            ]
            # Created at 11:00:02 (2s after 10:45–11:00 signal bar closed)
            created = bridge.reserve_book2_pending_limit_orders(sigs, now_dt=datetime(2026, 10, 19, 11, 0, 2), allow_broker_probe=False)
            self.assertEqual(len(created), 2)

            # 1. At 11:05:00, the 11:00–11:15 bar is NOT completed yet -> MUST NOT leak pre-seeded 11:00 bar!
            rec_early = bridge.reconcile_orders_and_positions(live_quotes={}, now_dt=datetime(2026, 10, 19, 11, 5, 0))
            self.assertEqual(rec_early["filled"], 0)
            self.assertEqual(rec_early["expired"], 0)

            # 2. At 11:15:01 (bar 11:00–11:15 completed, 1s before 11:15:02 TIF), WICK01 fills and GAP01 cancels post-only!
            rec_done = bridge.reconcile_orders_and_positions(live_quotes={}, now_dt=datetime(2026, 10, 19, 11, 15, 1))
            self.assertEqual(rec_done["filled"], 1)
            self.assertEqual(rec_done["expired"], 1)


if __name__ == "__main__":
    unittest.main()
