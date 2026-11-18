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

    def test_boot_disarmed_pending_cash_reservation_postonly_and_1510_squareoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "test_arena.db")
            _seed_synthetic_fno_db(db_p)

            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="unit_test_arena")
            # AGENTS.md Rule 2: Must boot strictly DISARMED (`simulation_mode = True`)
            self.assertTrue(bridge.simulation_mode)
            acct0 = bridge.configure_arena_capital(400000.0, MODE_DUAL_FUNDED_ARENA, reset_ledger=True)
            self.assertEqual(acct0["funded_capital_inr"], 400000.0)
            self.assertEqual(acct0["free_unreserved_cash_inr"], 400000.0)

            # Submit 6 simultaneous ₹1,00,000 signals -> 1.0x MIS notional cap MUST block orders #5 and #6 (`₹4,00,000` max),
            # while reserving SEBI VaR+ELM (`21%` at ADR=2.0% -> `₹21,000`/order = `₹84,000`) in unpledged cash!
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
                    "scheduled_exit_hm": "15:10",
                    "liq_1pct_bar_inr": 500000.0,
                }
                for i in range(1, 7)
            ]
            created = bridge.reserve_book2_pending_limit_orders(sigs, now_dt=t0)
            self.assertEqual(len(created), 4)
            for ord_rec in created:
                self.assertIsInstance(ord_rec["shares"], int)
                self.assertEqual(ord_rec["shares"], 100)
                self.assertEqual(ord_rec["reserved_cash_inr"], 21000.0)
                self.assertEqual(ord_rec["exposure_notional_inr"], 100000.0)

            acct_res = bridge.get_arena_accounting(now_dt=t0)
            self.assertEqual(acct_res["pending_reserved_cash_inr"], 84000.0)
            self.assertEqual(acct_res["pending_notional_inr"], 400000.0)
            self.assertEqual(acct_res["free_unreserved_cash_inr"], 316000.0)
            self.assertTrue(acct_res["book2_slots"]["mis_notional_within_1x_equity_ok"])
            self.assertTrue(acct_res["cash_conservation_invariant_ok"])

            # Reconcile at 10:20: FNO01 trades through 1000.0 (`1000.50 >= 1000.30`), others stay below 1000.0
            rec1 = bridge.reconcile_orders_and_positions(
                live_quotes={"FNO01.NS": {"price": 1000.50}, "FNO02.NS": {"price": 998.0}},
                now_dt=datetime(2026, 10, 19, 10, 20, 0),
            )
            self.assertEqual(rec1["filled"], 1)

            # At 10:31 (past 15m TIF), remaining 3 unfilled orders MUST expire and release reserved cash!
            rec2 = bridge.reconcile_orders_and_positions(
                live_quotes={"FNO01.NS": {"price": 992.0}},
                now_dt=datetime(2026, 10, 19, 10, 31, 0),
            )
            self.assertEqual(rec2["expired"], 3)
            acct_after_exp = bridge.get_arena_accounting(now_dt=datetime(2026, 10, 19, 10, 31, 0))
            self.assertEqual(acct_after_exp["pending_reserved_cash_inr"], 0.0)
            self.assertEqual(acct_after_exp["invested_notional_inr"], 100000.0)
            self.assertGreater(acct_after_exp["free_unreserved_cash_inr"], 300000.0)

            # At 15:09:50 IST (< 15:10), MIS position stays OPEN; at 15:10:05 IST, mandatory square-off MUST close it!
            rec_pre = bridge.reconcile_orders_and_positions(
                live_quotes={"FNO01.NS": {"price": 989.0}},
                now_dt=datetime(2026, 10, 19, 15, 9, 50),
            )
            self.assertEqual(rec_pre["closed"], 0)

            rec3 = bridge.reconcile_orders_and_positions(
                live_quotes={"FNO01.NS": {"price": 988.0}},
                now_dt=datetime(2026, 10, 19, 15, 10, 5),
            )
            self.assertEqual(rec3["closed"], 1)
            pos_bundle = bridge.get_positions_and_reservations()
            self.assertEqual(len(pos_bundle["open_positions"]), 0)
            self.assertEqual(len(pos_bundle["closed_positions"]), 1)
            closed_tr = pos_bundle["closed_positions"][0]
            self.assertEqual(closed_tr["exit_reason"], "MANDATORY_1510_MIS_SQUAREOFF")
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
                    "scheduled_exit_hm": "15:10",
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
                    "scheduled_exit_hm": "15:10",
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
                    "scheduled_exit_hm": "15:10",
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

    def test_monday_50k_cnc_zero_leverage_lock_slot_scaling_and_sector_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "cnc_lock.db")
            _seed_synthetic_fno_db(db_p)
            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="cnc_lock_test")

            # Attempt to pass MIS_3X / 5x leverage into BOOK1_SWING_CNC_ONLY -> MUST hard-lock to CASH_1X (1.0x)!
            acct = bridge.configure_arena_capital(
                funded_capital_inr=50000.0,
                execution_mode="BOOK1_SWING_CNC_ONLY",
                margin_mode="MIS_3X",
                intraday_mis_leverage=5.0,
                reset_ledger=True,
            )
            self.assertEqual(acct["execution_mode"], "BOOK1_SWING_CNC_ONLY")
            self.assertEqual(acct["margin_mode"], "CASH_1X")
            self.assertEqual(acct["intraday_mis_leverage"], 1.0)
            self.assertTrue(acct["cnc_leverage_locked_1x"])
            self.assertEqual(acct["book1_slots"]["max"], 3)

            # Verify 3 -> 8 dynamic slot ladder as capital grows
            from app.analytics.consensus_v3.arena_execution_bridge import compute_optimal_slot_counts
            self.assertEqual(compute_optimal_slot_counts(50000.0, 1.0, "BOOK1_SWING_CNC_ONLY")[0], 3)
            self.assertEqual(compute_optimal_slot_counts(75000.0, 1.0, "BOOK1_SWING_CNC_ONLY")[0], 4)
            self.assertEqual(compute_optimal_slot_counts(100000.0, 1.0, "BOOK1_SWING_CNC_ONLY")[0], 5)
            self.assertEqual(compute_optimal_slot_counts(125000.0, 1.0, "BOOK1_SWING_CNC_ONLY")[0], 6)
            self.assertEqual(compute_optimal_slot_counts(160000.0, 1.0, "BOOK1_SWING_CNC_ONLY")[0], 8)

            # Sector Concentration Cap: at 3 slots, max 1 stock per sector!
            snap = {"as_of_date": "2026-10-19", "candidates": []}
            arb = {
                "as_of_date": "2026-10-19",
                "macro_shield": {"regime": "BULL_FULL_DEPLOYMENT", "target_slots": 8},
                "approved_rebalance_leaders": [
                    {"ticker": "LAURUSLABS.NS", "symbol": "LAURUSLABS", "sector": "PHARMA", "instrument_key": "NSE_EQ|INE947Q01028", "close_px": 950.0, "vol60_ann_pct": 30.0},
                    {"ticker": "AUROPHARMA.NS", "symbol": "AUROPHARMA", "sector": "PHARMA", "instrument_key": "NSE_EQ|INE406A01037", "close_px": 1200.0, "vol60_ann_pct": 28.0},
                    {"ticker": "MCX.NS", "symbol": "MCX", "sector": "EXCHANGES_FINTECH", "instrument_key": "NSE_EQ|INE745G01035", "close_px": 2500.0, "vol60_ann_pct": 32.0},
                    {"ticker": "MOTHERSON.NS", "symbol": "MOTHERSON", "sector": "AUTO", "instrument_key": "NSE_EQ|INE775A01035", "close_px": 180.0, "vol60_ann_pct": 29.0},
                ],
            }
            rb = bridge.execute_book1_scheduled_rebalance(snap, arb, force=True, now_dt=datetime(2026, 10, 19, 10, 0, 0))
            self.assertEqual(rb["opened"], 3)
            opened_syms = [p["ticker"] for p in rb["opened_positions"]]
            self.assertEqual(opened_syms, ["LAURUSLABS.NS", "MCX.NS", "MOTHERSON.NS"])

    def test_unit_nav_midcycle_topup_4gate_pms_zero_loser_averaging(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "topup_pms.db")
            _seed_synthetic_fno_db(db_p)
            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="topup_test")
            bridge.configure_arena_capital(50000.0, "BOOK1_SWING_CNC_ONLY", reset_ledger=True)

            snap = {
                "as_of_date": "2026-10-19",
                "candidates": [
                    {"ticker": "LAURUSLABS.NS", "symbol": "LAURUSLABS", "close": 1100.0, "sma_alignment_ok": True, "qlib_pct_rank": 0.94},
                    {"ticker": "MCX.NS", "symbol": "MCX", "close": 1900.0, "sma_alignment_ok": True, "qlib_pct_rank": 0.92},
                    {"ticker": "MOTHERSON.NS", "symbol": "MOTHERSON", "close": 200.0, "sma_alignment_ok": True, "qlib_pct_rank": 0.90},
                    {"ticker": "BHEL.NS", "symbol": "BHEL", "close": 250.0, "sma_alignment_ok": True, "qlib_pct_rank": 0.89},
                ],
            }
            arb = {
                "as_of_date": "2026-10-19",
                "macro_shield": {"regime": "BULL_FULL_DEPLOYMENT", "target_slots": 8},
                "approved_rebalance_leaders": [
                    {"ticker": "LAURUSLABS.NS", "symbol": "LAURUSLABS", "sector": "PHARMA", "instrument_key": "NSE_EQ|INE947Q01028", "close_px": 1000.0, "vol60_ann_pct": 30.0},
                    {"ticker": "MCX.NS", "symbol": "MCX", "sector": "EXCHANGES_FINTECH", "instrument_key": "NSE_EQ|INE745G01035", "close_px": 2000.0, "vol60_ann_pct": 30.0},
                    {"ticker": "MOTHERSON.NS", "symbol": "MOTHERSON", "sector": "AUTO", "instrument_key": "NSE_EQ|INE775A01035", "close_px": 200.0, "vol60_ann_pct": 30.0},
                    {"ticker": "BHEL.NS", "symbol": "BHEL", "sector": "CAPITAL_GOODS", "instrument_key": "NSE_EQ|INE257A01026", "close_px": 250.0, "vol60_ann_pct": 30.0},
                ],
            }
            rb0 = bridge.execute_book1_scheduled_rebalance(snap, arb, force=True, now_dt=datetime(2026, 10, 19, 10, 0, 0))
            self.assertEqual(rb0["opened"], 3)

            # Simulate +10% gain on LAURUSLABS (1000 -> 1100) and -5% dip on MCX (2000 -> 1900)
            bridge.reconcile_orders_and_positions(
                live_quotes={"LAURUSLABS.NS": {"price": 1100.0}, "MCX.NS": {"price": 1900.0}, "MOTHERSON.NS": {"price": 200.0}},
                now_dt=datetime(2026, 10, 20, 11, 0, 0),
            )
            acct_pre = bridge.get_arena_accounting()
            pre_unav = acct_pre["unit_nav"]
            pre_twr = acct_pre["time_weighted_return_pct"]
            self.assertGreater(pre_unav, 100.0)

            # Mid-cycle +₹25,000 top-up MUST preserve Unit NAV & TWR% while scaling Book-1 slots 3 -> 4!
            tp = bridge.top_up_arena_capital(25000.0, deploy_mode="AUTO_4GATE_PMS")
            self.assertEqual(tp["status"], "TOPPED_UP")
            self.assertAlmostEqual(tp["accounting"]["unit_nav"], pre_unav, places=2)
            self.assertAlmostEqual(tp["accounting"]["time_weighted_return_pct"], pre_twr, places=2)
            self.assertEqual(tp["accounting"]["book1_slots"]["max"], 4)

            # 4-Gate deployment fills the 4th slot with BHEL without resetting last_book1_rebalance_date!
            dep = bridge.deploy_midcycle_inflow_4gate(snap, arb, now_dt=datetime(2026, 10, 20, 11, 5, 0))
            self.assertEqual(dep["status"], "DEPLOYED_MIDCYCLE_4GATE")
            self.assertEqual(dep["opened"], 1)
            self.assertEqual(dep["opened_positions"][0]["ticker"], "BHEL.NS")
            self.assertEqual(bridge.get_arena_accounting()["last_book1_rebalance_date"], "2026-10-19")

            # Add another ₹15,000 while all 4 slots are full (cap=₹90k, still 4 slots): MCX is losing (1900 < 2000) -> MUST NOT average down!
            bridge.top_up_arena_capital(15000.0, deploy_mode="AUTO_4GATE_PMS")
            snap_only_loser_room = {
                "as_of_date": "2026-10-20",
                "candidates": [
                    {"ticker": "MCX.NS", "symbol": "MCX", "close": 1900.0, "sma_alignment_ok": True, "qlib_pct_rank": 0.95},
                ],
            }
            dep_no_loser = bridge.deploy_midcycle_inflow_4gate(snap_only_loser_room, {"as_of_date": "2026-10-20", "macro_shield": {"target_slots": 8}, "approved_rebalance_leaders": []})
            self.assertEqual(dep_no_loser["pyramided"], 0)

    def test_live_upstox_hardening_cancel_retrieve_exit_retry_sniffer_firewall_and_day2_guard(self):
        from unittest.mock import patch
        from app.analytics.consensus_v3.math_and_costs import RegularizedBoostedStumpPAV

        # 1. Verify PAV tie-aggregation eliminates fake 0.0 / 1.0 boundary spikes on tied scores
        clf = RegularizedBoostedStumpPAV(n_trees=2, lr=0.1, min_leaf=5)
        X_tie = [[1.0, 2.0]] * 25 + [[5.0, 6.0]] * 25
        y_tie = ([0, 1, 0, 1, 0] * 5) + ([1, 1, 0, 1, 1] * 5)
        clf.fit(X_tie, y_tie)
        p_lo = clf.predict_proba([1.0, 2.0])
        p_hi = clf.predict_proba([5.0, 6.0])
        self.assertGreater(p_lo, 0.15)
        self.assertLess(p_hi, 0.90)

        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "live_mock.db")
            _seed_synthetic_fno_db(db_p)
            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="live_suite")
            bridge.configure_arena_capital(
                funded_capital_inr=50000.0,
                execution_mode="DUAL_FUNDED_CASH_ARENA",
                option_sniffer_reserve_inr=90000.0,
                reset_ledger=True,
            )

            # Tag sanitization <= 20 chars & unique on trailing UUID
            t1 = bridge._sanitize_upstox_tag("ord_b2_20261123_aaaa1111", "BUY")
            t2 = bridge._sanitize_upstox_tag("ord_b2_20261123_bbbb2222", "BUY")
            self.assertLessEqual(len(t1), 20)
            self.assertNotEqual(t1, t2)

            # Option Sniffer Margin Firewall: if Upstox has ₹1,10,000 and ₹90,000 is fenced for Option Sniffer,
            # a ₹25,000 equity order (leaving ₹85,000 < ₹90,000) MUST be blocked!
            with patch.object(bridge, "_fetch_upstox_readiness", return_value={"token_valid": True, "available_margin": 110000.0}):
                fw_block = bridge._check_option_sniffer_margin_firewall(25000.0)
                self.assertFalse(fw_block["allowed"])
                fw_ok = bridge._check_option_sniffer_margin_firewall(15000.0)
                self.assertTrue(fw_ok["allowed"])

            # Mock live readiness with ₹1,50,000 wallet (>= ₹50k equity + ₹90k Sniffer fence = ₹1,40,000)
            with patch.object(bridge, "_fetch_upstox_readiness", return_value={"token_valid": True, "available_margin": 150000.0, "is_read_only_token": False, "static_ip_matched": True}):
                st0 = bridge.get_live_status()
                self.assertTrue(st0["readiness"]["live_trading_eligible"])
                arm_res = bridge.arm_live_trading(pin=bridge.current_pin)
                self.assertEqual(arm_res["status"], "ARMED")
                self.assertEqual(bridge.session_id, bridge.live_session_id)

                # Reset ledger MUST be hard-blocked while armed!
                rst_err = bridge.configure_arena_capital(50000.0, reset_ledger=True)
                self.assertEqual(rst_err["status"], "ERROR")
                self.assertEqual(rst_err["code"], "LIVE_POSITIONS_ACTIVE")

                # Place mocked LIVE_UPSTOX Book-2 order -> verify it does NOT fill from OHLCV bars, and cancels via /order/cancel at TIF!
                with patch.object(bridge, "_route_upstox_equity_order", return_value={"routed": True, "state": "SUBMITTED", "broker_order_id": "UPX_ORD_1", "avg_fill_price": None}):
                    sigs = [{
                        "book": "BOOK2_INTRADAY_MIS_SNIFFER",
                        "ticker": "FNO01.NS",
                        "symbol": "FNO01",
                        "instrument_key": "NSE_EQ|INE000001001",
                        "direction": "SELL",
                        "signal_close_px": 498.0,
                        "limit_entry_px": 500.0,
                        "stop_loss_px": 510.0,
                        "scheduled_exit_hm": "15:10",
                        "liq_1pct_bar_inr": 500000.0,
                    }]
                    ords = bridge.reserve_book2_pending_limit_orders(sigs, now_dt=datetime(2026, 10, 19, 10, 0, 0), allow_broker_probe=False)
                    self.assertEqual(len(ords), 1)
                    self.assertEqual(ords[0]["execution_env"], "LIVE_UPSTOX")

                # Even if quote trades through 505.0, LIVE_UPSTOX order stays PENDING while broker status is 'open'!
                with patch.object(bridge, "_fetch_upstox_day_orders_map", return_value=({"UPX_ORD_1": {"status": "open", "filled_quantity": 0}}, {})):
                    r_open = bridge.reconcile_orders_and_positions(live_quotes={"FNO01.NS": {"price": 505.0}}, now_dt=datetime(2026, 10, 19, 10, 5, 0))
                    self.assertEqual(r_open["filled"], 0)

                # At 10:16 (past 15m TIF), reconciler calls _cancel_upstox_order before releasing cash!
                with patch.object(bridge, "_fetch_upstox_day_orders_map", return_value=({"UPX_ORD_1": {"status": "open", "filled_quantity": 0}}, {})), \
                     patch.object(bridge, "_cancel_upstox_order", return_value={"cancelled": True, "http_status": 200}) as mock_cancel:
                    r_exp = bridge.reconcile_orders_and_positions(live_quotes={}, now_dt=datetime(2026, 10, 19, 10, 16, 0))
                    self.assertEqual(r_exp["expired"], 1)
                    mock_cancel.assert_called_once_with("UPX_ORD_1")

                # Open a LIVE_UPSTOX CNC position (₹18,000), then disarm (simulating Day-2 overnight auto-disarm)
                with patch.object(bridge, "_route_upstox_equity_order", return_value={"routed": True, "state": "SUBMITTED", "broker_order_id": "UPX_BUY_1", "avg_fill_price": 900.0}), \
                     patch("app.data.market_provider.get_bulk_live_quotes_with_meta", return_value={"LAURUSLABS.NS": {"price": 900.0, "source": "upstox", "is_realtime": True}}):
                    rb_live = bridge.execute_book1_scheduled_rebalance(
                        {"as_of_date": "2026-10-19", "candidates": []},
                        {
                            "as_of_date": "2026-10-19",
                            "macro_shield": {"regime": "BULL_FULL_DEPLOYMENT", "target_slots": 8},
                            "approved_rebalance_leaders": [
                                {"ticker": "LAURUSLABS.NS", "symbol": "LAURUSLABS", "sector": "PHARMA", "instrument_key": "NSE_EQ|INE947Q01028", "close_px": 900.0, "vol60_ann_pct": 30.0},
                            ],
                        },
                        force=True,
                        now_dt=datetime(2026, 10, 19, 10, 20, 0),
                    )
                    self.assertEqual(rb_live["opened"], 1)
                    live_pid = rb_live["opened_positions"][0]["position_id"]

                bridge.disarm_live_trading()
                self.assertTrue(bridge.simulation_mode)
                # Active session MUST still track live_session_id & block Reset Shadow while LIVE_UPSTOX position is open!
                self.assertEqual(bridge.session_id, bridge.live_session_id)
                self.assertEqual(bridge.configure_arena_capital(50000.0, reset_ledger=True)["code"], "LIVE_POSITIONS_ACTIVE")

            # Day-2 cash check: ₹18,000 is already invested in CNC shares, so ₹1,25,000 free broker cash (>= ₹32k rem + ₹90k fence = ₹1,22,000) MUST be eligible!
            with patch.object(bridge, "_fetch_upstox_readiness", return_value={"token_valid": True, "available_margin": 125000.0, "is_read_only_token": False, "static_ip_matched": True}):
                st_day2 = bridge.get_live_status()
                self.assertTrue(st_day2["mode_margin_ok"])

            # If broker rejects SELL exit (e.g., DDPI/TPIN missing), position MUST become EXIT_RETRY_PENDING (NEVER silently CLOSED)!
            with patch.object(bridge, "_route_upstox_equity_order", return_value={"routed": False, "state": "REJECTED", "error": "EDIS_TPIN_REQUIRED"}):
                ex_fail = bridge.manual_exit_position(live_pid, reason="TEST_EXIT")
                self.assertEqual(ex_fail["status"], "EXIT_RETRY_PENDING")
                open_after_fail = bridge.get_positions_and_reservations()["open_positions"]
                self.assertEqual(len(open_after_fail), 1)
                self.assertEqual(open_after_fail[0]["status"], "EXIT_RETRY_PENDING")

            # Retry exit once DDPI/TPIN is authorized -> routes cleanly EVEN WHILE DISARMED (`is_exit=True`) and closes!
            with patch.object(bridge, "_route_upstox_equity_order", return_value={"routed": True, "state": "SUBMITTED", "broker_order_id": "UPX_SELL_1", "avg_fill_price": 945.0}):
                ex_ok = bridge.manual_exit_position(live_pid, reason="TEST_EXIT_RETRY")
                self.assertEqual(ex_ok["status"], "CLOSED")
                self.assertGreater(ex_ok["net_pnl_inr"], 700.0)

    def test_intracycle_institutional_breakdown_and_finbert_shock_sentinel(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "breakdown.db")
            conn = sqlite3.connect(db_p)
            conn.execute("CREATE TABLE fno_universe_cache (symbol TEXT PRIMARY KEY);")
            conn.execute("CREATE TABLE ohlcv (ticker TEXT, date TEXT, timeframe TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL);")
            conn.execute("INSERT INTO fno_universe_cache VALUES ('LAURUSLABS');")

            # Seed 205 calm daily bars rising to 1000.0 (SMA50 ~ 975, SMA200 ~ 900, avg turnover = 1000 * 100k = 10Cr)
            base_d = datetime(2026, 1, 1)
            rows = []
            for i in range(205):
                d_s = (base_d + timedelta(days=i)).strftime("%Y-%m-%d")
                px = 800.0 + i * 1.0
                rows.append(("LAURUSLABS.NS", d_s, "1d", px, px * 1.01, px * 0.99, px, 100000.0))
            entry_d = (base_d + timedelta(days=204)).strftime("%Y-%m-%d")
            next_d = (base_d + timedelta(days=205)).strftime("%Y-%m-%d")
            conn.executemany("INSERT INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
            conn.close()

            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="bd_test")
            bridge.configure_arena_capital(50000.0, "BOOK1_SWING_CNC_ONLY", reset_ledger=True)
            bridge.execute_book1_scheduled_rebalance(
                {"as_of_date": entry_d, "candidates": []},
                {
                    "as_of_date": entry_d,
                    "macro_shield": {"regime": "BULL_FULL_DEPLOYMENT", "target_slots": 8},
                    "approved_rebalance_leaders": [
                        {"ticker": "LAURUSLABS.NS", "symbol": "LAURUSLABS", "sector": "PHARMA", "instrument_key": "NSE_EQ|INE947Q01028", "close_px": 1004.0, "vol60_ann_pct": 30.0},
                    ],
                },
                force=True,
                now_dt=datetime.strptime(f"{entry_d} 15:00:00", "%Y-%m-%d %H:%M:%S"),
            )

            # 1. Normal -2.5% dip on average volume (above SMA50) MUST NOT shake out the position!
            conn = sqlite3.connect(db_p)
            conn.execute("INSERT INTO ohlcv VALUES ('LAURUSLABS.NS', ?, '1d', 1000.0, 1002.0, 978.0, 980.0, 100000.0)", (next_d,))
            conn.commit()
            conn.close()
            r_calm = bridge.reconcile_orders_and_positions(
                live_quotes={"LAURUSLABS.NS": {"price": 980.0}},
                now_dt=datetime.strptime(f"{next_d} 15:35:00", "%Y-%m-%d %H:%M:%S"),
            )
            self.assertEqual(r_calm["closed"], 0)

            # 2. High-volume institutional distribution breakdown (< SMA50, ret_1d <= -4%, RVOL >= 2.0x) MUST exit!
            d_bd = (base_d + timedelta(days=206)).strftime("%Y-%m-%d")
            conn = sqlite3.connect(db_p)
            conn.execute("INSERT INTO ohlcv VALUES ('LAURUSLABS.NS', ?, '1d', 975.0, 976.0, 925.0, 930.0, 320000.0)", (d_bd,))
            conn.commit()
            conn.close()
            r_bd = bridge.reconcile_orders_and_positions(
                live_quotes={"LAURUSLABS.NS": {"price": 930.0}},
                now_dt=datetime.strptime(f"{d_bd} 15:35:00", "%Y-%m-%d %H:%M:%S"),
            )
            self.assertEqual(r_bd["closed"], 1)
            closed_pos = bridge.get_positions_and_reservations()["closed_positions"][0]
            self.assertEqual(closed_pos["exit_reason"], "INTRACYCLE_INSTITUTIONAL_DISTRIBUTION_EXIT")

    def test_finbert_analyzer_export_shock_veto_and_intracycle_shock_exit(self):
        from unittest.mock import patch
        from app.analytics.nlp_engine import get_nlp_analyzer, nlp_engine

        # 1. Unmocked singleton export check (prevents silent ImportError regression!)
        self.assertIs(get_nlp_analyzer(), nlp_engine)

        # 2. Verify ConsensusArbiter vetoes severe FinBERT shock & formats clean NSE filings badge
        snap = {
            "as_of_date": "2026-11-17",
            "macro_shield": {"regime": "BULL_FULL_DEPLOYMENT", "target_slots": 3},
            "candidates": [
                {
                    "ticker": "SHOCKCO.NS", "symbol": "SHOCKCO", "sector": "PHARMA",
                    "instrument_key": "NSE_EQ|INE000001111", "as_of_date": "2026-11-17",
                    "close": 1000.0, "sma_alignment_ok": True, "qlib_pct_rank": 0.96,
                    "qlib_expected_gross_bps": 120.0, "rsi": 58.0, "adx": 28.0,
                    "atr_pct": 2.1, "mom_12_1_pct": 42.0, "near_52w_ratio": 0.95,
                },
                {
                    "ticker": "CLEANCO.NS", "symbol": "CLEANCO", "sector": "AUTO",
                    "instrument_key": "NSE_EQ|INE000002222", "as_of_date": "2026-11-17",
                    "close": 500.0, "sma_alignment_ok": True, "qlib_pct_rank": 0.92,
                    "qlib_expected_gross_bps": 95.0, "rsi": 60.0, "adx": 30.0,
                    "atr_pct": 1.9, "mom_12_1_pct": 38.0, "near_52w_ratio": 0.94,
                },
            ],
        }

        def _fake_analyze(sym):
            if "SHOCKCO" in sym:
                return {"score": -91.0, "articles_analyzed": 3, "max_neg_prob": 0.92, "is_severe_negative_shock": True}
            return {"score": 4.5, "articles_analyzed": 8, "max_neg_prob": 0.04, "is_severe_negative_shock": False}

        with patch.object(nlp_engine, "analyze_ticker_news", side_effect=_fake_analyze):
            arb = ConsensusArbiter.arbitrate_book1_20d_rebalance(snap, slot_cap_inr=20000.0, check_live_finbert=True)
            self.assertEqual(len(arb["approved_rebalance_leaders"]), 1)
            self.assertEqual(arb["approved_rebalance_leaders"][0]["symbol"], "CLEANCO")
            self.assertEqual(arb["approved_rebalance_leaders"][0]["telemetry"]["finbert_badge"], "NLP:FILINGS_OK(8)")
            self.assertEqual(arb["top_abstained_sample"][0]["abstain_reason"], "FINBERT_SEVERE_REGULATORY_SHOCK_VETO")

            # 3. Verify intra-cycle >=3% intraday drop + severe FinBERT shock triggers FINBERT_REGULATORY_SHOCK_EXIT!
            with tempfile.TemporaryDirectory() as tmp:
                db_p = os.path.join(tmp, "finbert_exit.db")
                conn = sqlite3.connect(db_p)
                conn.execute("CREATE TABLE ohlcv (ticker TEXT, date TEXT, timeframe TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL);")
                base_d = datetime(2026, 1, 1)
                rows = [
                    ("SHOCKCO.NS", (base_d + timedelta(days=i)).strftime("%Y-%m-%d"), "1d", 1000.0, 1005.0, 995.0, 1000.0, 100000.0)
                    for i in range(205)
                ]
                conn.executemany("INSERT INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
                conn.commit()
                bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="fb_exit")
                reason = bridge._check_book1_intracycle_breakdown(
                    conn=conn,
                    sym="SHOCKCO.NS",
                    entry_date_s=(base_d + timedelta(days=204)).strftime("%Y-%m-%d"),
                    curr_px=962.0,  # -3.8% intraday drop vs 1000.0 prior close
                    today_s=(base_d + timedelta(days=204)).strftime("%Y-%m-%d"),
                    hm_now="11:15",
                    allow_live_finbert=True,
                )
                self.assertEqual(reason, "FINBERT_REGULATORY_SHOCK_EXIT")
                conn.close()

        # 4. Archival staleness immunity: 2016 NSE filings (>7d) MUST be discarded & never trigger shock flags,
        #    while fresh <=72h regulatory shocks MUST trigger!
        now_dt = datetime.now()
        stale_2016_ts = datetime(2016, 6, 30, 12, 0, 0).timestamp()
        old_5d_ts = (now_dt - timedelta(days=5.2)).timestamp()
        fresh_4h_ts = (now_dt - timedelta(hours=4)).timestamp()

        with patch("app.analytics.nlp_engine._fetch_live_exchange_and_rss_news", return_value=[
            {"title": "MCX: SEBI ban fraud default warning letter", "summary": "Archival 2016 row", "providerPublishTime": stale_2016_ts, "source": "NSE_CORPORATE_FILING"},
        ]), patch.object(nlp_engine.analyzer, "polarity_scores", return_value={"neg": 0.96, "neu": 0.02, "pos": 0.02, "compound": -0.94}):
            res_stale = nlp_engine.analyze_ticker_news("MCX.NS", as_of_timestamp=now_dt)
            self.assertEqual(res_stale["articles_analyzed"], 0)
            self.assertFalse(res_stale["is_severe_negative_shock"])

        # 5.2-day-old item is within 7d sentiment window, but >72h shock cutoff -> must NOT trigger emergency shock exit!
        with patch("app.analytics.nlp_engine._fetch_live_exchange_and_rss_news", return_value=[
            {"title": "MCX: SEBI order warning letter", "summary": "5d ago", "providerPublishTime": old_5d_ts, "source": "NSE_CORPORATE_FILING"},
        ]), patch.object(nlp_engine.analyzer, "polarity_scores", return_value={"neg": 0.92, "neu": 0.05, "pos": 0.03, "compound": -0.89}):
            res_5d = nlp_engine.analyze_ticker_news("MCX.NS", as_of_timestamp=now_dt)
            self.assertEqual(res_5d["articles_analyzed"], 1)
            self.assertFalse(res_5d["is_severe_negative_shock"])

        # Fresh 4-hour-old item (<72h) WITH severe neg >= 0.85 + shock keyword -> MUST trigger is_severe_negative_shock!
        with patch("app.analytics.nlp_engine._fetch_live_exchange_and_rss_news", return_value=[
            {"title": "MCX: SEBI order forensic audit", "summary": "4h ago", "providerPublishTime": fresh_4h_ts, "source": "NSE_CORPORATE_FILING"},
        ]), patch.object(nlp_engine.analyzer, "polarity_scores", return_value={"neg": 0.93, "neu": 0.05, "pos": 0.02, "compound": -0.91}):
            res_fresh = nlp_engine.analyze_ticker_news("MCX.NS", as_of_timestamp=now_dt)
            self.assertEqual(res_fresh["articles_analyzed"], 1)
            self.assertTrue(res_fresh["is_severe_negative_shock"])

    def test_bug1_live_quote_and_0915_open_fill_plus_intraday_b1_cache(self):
        from unittest.mock import patch
        from app.analytics.consensus_v3.orchestrator import ConsensusV3Orchestrator

        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "bug1.db")
            conn = sqlite3.connect(db_p)
            conn.execute("CREATE TABLE fno_universe_cache (symbol TEXT PRIMARY KEY);")
            conn.execute("CREATE TABLE ohlcv (ticker TEXT, date TEXT, timeframe TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL);")

            # Seed 85 F&O stocks with 265 daily bars ending 2026-10-16 (T-1) + 25 15m bars on 2026-10-19 (today)
            tickers = [f"B1T{i:02d}.NS" for i in range(1, 86)]
            conn.executemany("INSERT INTO fno_universe_cache VALUES (?)", [(t.replace(".NS", ""),) for t in tickers])
            base_d = datetime(2025, 11, 1)
            rows_1d = []
            for d_off in range(265):
                d_s = (base_d + timedelta(days=d_off)).strftime("%Y-%m-%d")
                if d_s >= "2026-09-24":
                    break
                px = 100.0 + d_off * 0.5
                for t in tickers:
                    rows_1d.append((t, d_s, "1d", px, px * 1.01, px * 0.99, px, 200000.0))
            # Also seed 15m bars for 2026-09-24 .. 2026-10-16 (completed) + 25 forming bars on 2026-10-19
            rows_15m = []
            hms_25 = [
                f"{9 + (15 + 15 * k) // 60:02d}:{(15 + 15 * k) % 60:02d}:00"
                for k in range(25)
            ]
            for d_off in range(25):
                d_s = (datetime(2026, 9, 24) + timedelta(days=d_off)).strftime("%Y-%m-%d")
                if d_s > "2026-10-16":
                    break
                for t in tickers:
                    for hm in hms_25:
                        rows_15m.append((t, f"{d_s} {hm}", "15m", 300.0, 303.0, 298.0, 300.0, 15000.0))
            # Today (2026-10-19) has 25 bars with 09:15 open = 315.0 (gapped +5% over T-1 close = 300.0!)
            for t in tickers:
                for idx_h, hm in enumerate(hms_25):
                    op = 315.0 if idx_h == 0 else 318.0
                    rows_15m.append((t, f"2026-10-19 {hm}", "15m", op, op + 2.0, op - 1.0, op + 1.0, 15000.0))
            conn.executemany("INSERT INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows_1d + rows_15m)
            conn.commit()

            # 1. Before 15:30 IST on 2026-10-19, even with 25 bars present, eval_date MUST remain strictly T-1 (2026-10-16)!
            class _FakePreCloseDt(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime(2026, 10, 19, 15, 20, 0)

            with patch("app.analytics.consensus_v3.baseline_engine.datetime", _FakePreCloseDt):
                snap_pre = BaselineEngine.build_book1_20d_snapshot(conn, as_of_date="2026-10-19")
                self.assertEqual(snap_pre["as_of_date"], "2026-10-16")
            conn.close()

            # 2. Book-1 fill price resolution: live quote > 09:15 open (315.0) > T-1 close fallback (300.0)
            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="bug1_exec")
            bridge.configure_arena_capital(100000.0, "BOOK1_SWING_CNC_ONLY", reset_ledger=True)
            snap_mock = {
                "as_of_date": "2026-10-16",
                "candidates": [{"ticker": "B1T01.NS", "symbol": "B1T01", "close": 300.0, "sma_alignment_ok": True, "qlib_pct_rank": 0.96}],
            }
            arb_mock = {
                "as_of_date": "2026-10-16",
                "macro_shield": {"regime": "BULL_FULL_DEPLOYMENT", "target_slots": 8},
                "approved_rebalance_leaders": [
                    {"ticker": "B1T01.NS", "symbol": "B1T01", "sector": "AUTO", "instrument_key": "NSE_EQ|INE000001111", "close_px": 300.0, "vol60_ann_pct": 30.0},
                    {"ticker": "B1T02.NS", "symbol": "B1T02", "sector": "PHARMA", "instrument_key": "NSE_EQ|INE000001112", "close_px": 300.0, "vol60_ann_pct": 30.0},
                ],
            }
            # B1T01 gets live quote 322.0; B1T02 has no live quote -> fills at today's 09:15 open (315.0), NEVER 300.0!
            rb = bridge.execute_book1_scheduled_rebalance(
                snap_mock,
                arb_mock,
                force=True,
                now_dt=datetime(2026, 10, 19, 10, 0, 0),
                live_quotes={"B1T01.NS": {"price": 322.0, "source": "upstox", "is_realtime": True}},
            )
            by_t = {p["ticker"]: p["entry_price"] for p in rb["opened_positions"]}
            self.assertEqual(by_t["B1T01.NS"], 322.0)
            self.assertEqual(by_t["B1T02.NS"], 315.0)

            # Mid-cycle 4-gate fill also uses live quote / 09:15 open
            bridge.top_up_arena_capital(25000.0, deploy_mode="AUTO_4GATE_PMS")
            arb_mid = {
                "as_of_date": "2026-10-16",
                "macro_shield": {"regime": "BULL_FULL_DEPLOYMENT", "target_slots": 8},
                "approved_rebalance_leaders": [
                    {"ticker": "B1T03.NS", "symbol": "B1T03", "sector": "IT", "instrument_key": "NSE_EQ|INE000001113", "close_px": 300.0, "vol60_ann_pct": 30.0},
                ],
            }
            dep = bridge.deploy_midcycle_inflow_4gate(
                snap_mock, arb_mid, now_dt=datetime(2026, 10, 19, 10, 15, 0), live_quotes={}
            )
            self.assertEqual(dep["opened"], 1)
            self.assertEqual(dep["opened_positions"][0]["entry_price"], 315.0)

            # 3. LIVE_UPSTOX fail-closed on missing live quote (Book 1 rebalance + _route_upstox_equity_order MARKET entry)
            with patch.object(bridge, "_fetch_upstox_readiness", return_value={"token_valid": True, "available_margin": 500000.0, "is_read_only_token": False, "static_ip_matched": True}):
                bridge.arm_live_trading(pin=bridge.current_pin)
                rb_fail = bridge.execute_book1_scheduled_rebalance(
                    snap_mock, arb_mock, force=True, now_dt=datetime(2026, 10, 19, 10, 20, 0), live_quotes={}
                )
                self.assertEqual(rb_fail["opened"], 0)
                with patch("app.data.market_provider.get_live_quote_with_meta", return_value={"price": None, "source": "unavailable", "is_realtime": False}):
                    r_blk = bridge._route_upstox_equity_order(
                        order_or_pos_id="ord_mkt_missing",
                        action="BUY",
                        product_type="CNC",
                        order_type="MARKET",
                        ticker="B1T01.NS",
                        instrument_key="NSE_EQ|INE000001111",
                        shares=10,
                        price=300.0,
                    )
                    self.assertFalse(r_blk["routed"])
                    self.assertEqual(r_blk["state"], "MISSING_LIVE_QUOTE")
                bridge.disarm_live_trading()

            # 4. Orchestrator Book 1 intraday cache avoids thrash when snap["as_of_date"] == T-1 and isolates as_of_ts
            m_dir = os.path.join(tmp, "models_b1")
            orch = ConsensusV3Orchestrator(db_path=db_p, model_dir=m_dir)
            b1_call_cnt = {"n": 0}
            fake_snap_b1 = {"as_of_date": "2026-10-16", "candidates": [], "macro_shield": {"regime": "BULL_FULL_DEPLOYMENT", "target_slots": 8}}
            fake_snap_b2 = {"as_of_ts": "2026-10-19 10:15:00", "session_date": "2026-10-19", "bar_hm": "10:15", "is_sniffer_window": True, "items": [], "market_context": {}}

            def _count_b1(ro_conn, as_of_date=None):
                b1_call_cnt["n"] += 1
                return dict(fake_snap_b1)

            with patch.object(BaselineEngine, "build_book1_20d_snapshot", side_effect=_count_b1), \
                 patch.object(BaselineEngine, "build_book2_15m_snapshot", return_value=fake_snap_b2), \
                 patch.object(orch.qlib_forecaster, "ensure_models_ready", return_value={"book1": {"artifact_sha256": "a"}, "book2": {"artifact_sha256": "b"}}), \
                 patch.object(orch.legacy_estimator, "ensure_ready", return_value={"artifact_sha256": "c", "qlib_inputs_used": 0}), \
                 patch.object(orch.qlib_forecaster, "score_book1_snapshot"), \
                 patch.object(orch.qlib_forecaster, "score_book2_snapshot"), \
                 patch.object(ConsensusArbiter, "arbitrate_book2_15m_sniffer", return_value={"approved_signals": [], "abstained_signals": [], "market_context": {}}), \
                 patch.object(ConsensusArbiter, "arbitrate_book1_20d_rebalance", return_value={"as_of_date": "2026-10-16", "macro_shield": {"target_slots": 8}, "approved_rebalance_leaders": []}):
                orch.run_15m_sniffer_sweep(as_of_ts=None, reserve_arena_orders=False)
                orch.run_15m_sniffer_sweep(as_of_ts=None, reserve_arena_orders=False)
                self.assertEqual(b1_call_cnt["n"], 1)  # Cached despite as_of_date ("2026-10-16") != today ("2026-11-18")!
                # Backtest call (`as_of_ts is not None`) runs separately and does NOT clobber `_last_book1_summary`
                bt_res = orch.run_15m_sniffer_sweep(as_of_ts="2026-10-16 10:15:00", reserve_arena_orders=False)
                self.assertEqual(b1_call_cnt["n"], 2)
                self.assertEqual(bt_res["book1_20d_rebalance"]["as_of_date"], "2026-10-16")
                orch.run_15m_sniffer_sweep(as_of_ts=None, reserve_arena_orders=False)
                self.assertEqual(b1_call_cnt["n"], 2)  # Live cache still intact!

    def test_bug2_rebalance_clock_counts_1d_plus_15m_trading_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "bug2_clock.db")
            conn = sqlite3.connect(db_p)
            conn.execute("CREATE TABLE fno_universe_cache (symbol TEXT PRIMARY KEY);")
            conn.execute("CREATE TABLE ohlcv (ticker TEXT, date TEXT, timeframe TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL);")
            conn.execute("INSERT INTO fno_universe_cache VALUES ('LAURUSLABS');")

            # Seed 1d bars frozen at 2026-09-24 + 20 trading sessions of 15m bars from 2026-09-25 .. 2026-10-14
            rows = [("LAURUSLABS.NS", "2026-09-24", "1d", 900.0, 910.0, 895.0, 905.0, 100000.0)]
            sess_days = [(datetime(2026, 9, 25) + timedelta(days=k)).strftime("%Y-%m-%d") for k in range(20)]
            for d_s in sess_days:
                rows.append(("LAURUSLABS.NS", f"{d_s} 09:15:00", "15m", 910.0, 915.0, 905.0, 912.0, 20000.0))
            conn.executemany("INSERT INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
            conn.close()

            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="bug2_test")
            bridge.configure_arena_capital(100000.0, "BOOK1_SWING_CNC_ONLY", reset_ledger=True)
            with bridge._conn() as c2:
                c2.execute("UPDATE consensus_v3_arena_state SET last_book1_rebalance_date = '2026-09-24' WHERE session_id = ?", (bridge.shadow_session_id,))
                c2.commit()

            snap = {"as_of_date": sess_days[16], "candidates": []}
            arb = {
                "as_of_date": sess_days[16],
                "macro_shield": {"regime": "BULL_FULL_DEPLOYMENT", "target_slots": 8},
                "approved_rebalance_leaders": [
                    {"ticker": "LAURUSLABS.NS", "symbol": "LAURUSLABS", "sector": "PHARMA", "instrument_key": "NSE_EQ|INE947Q01028", "close_px": 910.0, "vol60_ann_pct": 30.0},
                ],
            }

            # At day 17 (`20 - 17 = 3 < 4`): Gate 3 holds mid-cycle deployment!
            dep_hold = bridge.deploy_midcycle_inflow_4gate(
                snap, arb, now_dt=datetime.strptime(f"{sess_days[16]} 11:00:00", "%Y-%m-%d %H:%M:%S")
            )
            self.assertEqual(dep_hold["status"], "GATE3_NEAR_SCHEDULED_REBALANCE_HOLD")
            self.assertEqual(dep_hold["days_remaining_in_cycle"], 3)

            # At day 19 (`tdays_elapsed == 19 < 20`): scheduled rebalance is NOT_DUE_YET
            rb_19 = bridge.execute_book1_scheduled_rebalance(
                snap, arb, force=False, now_dt=datetime.strptime(f"{sess_days[18]} 11:00:00", "%Y-%m-%d %H:%M:%S")
            )
            self.assertEqual(rb_19["status"], "NOT_DUE_YET")
            self.assertEqual(rb_19["trading_days_since_last"], 19)

            # At day 20 (`tdays_elapsed == 20`): scheduled rebalance triggers and stores today_s (not T-1)
            rb_20 = bridge.execute_book1_scheduled_rebalance(
                snap, arb, force=False, now_dt=datetime.strptime(f"{sess_days[19]} 11:00:00", "%Y-%m-%d %H:%M:%S")
            )
            self.assertEqual(rb_20["status"], "REBALANCED")
            self.assertEqual(rb_20["rebalance_date"], sess_days[19])
            self.assertEqual(bridge.get_arena_accounting()["last_book1_rebalance_date"], sess_days[19])

    def test_bug3_1510_mis_squareoff_cutoff_and_expiration(self):
        from unittest.mock import patch
        from app.analytics.consensus_v3.consensus_arbiter import ExitController
        from app.analytics.consensus_v3.orchestrator import ConsensusV3Orchestrator

        plan = ExitController.build_book2_sniffer_exit_plan(
            {"atr_px": 5.0, "session_high": 505.0, "session_low": 495.0},
            direction="SELL",
            limit_px=500.0,
        )
        self.assertEqual(plan["scheduled_squareoff_hm"], "15:10")
        self.assertEqual(plan["exit_rule_version"], "EXPLICIT_STRUCTURAL_SL_PLUS_1510_SQUAREOFF_V2")

        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "bug3.db")
            _seed_synthetic_fno_db(db_p)
            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="bug3_test")
            bridge.configure_arena_capital(100000.0, MODE_DUAL_FUNDED_ARENA, reset_ledger=True)

            sig = [{
                "book": "BOOK2_INTRADAY_MIS_SNIFFER",
                "ticker": "FNO01.NS",
                "symbol": "FNO01",
                "instrument_key": "NSE_EQ|INE000000001",
                "direction": "SELL",
                "signal_close_px": 995.0,
                "limit_entry_px": 1000.0,
                "stop_loss_px": 1010.0,
                "scheduled_exit_hm": "15:10",
                "liq_1pct_bar_inr": 500000.0,
            }]
            # At 14:48:00, expires_at MUST be clamped to 14:55:00 (< 15:10:00)
            ords_1448 = bridge.reserve_book2_pending_limit_orders(sig, now_dt=datetime(2026, 10, 19, 14, 48, 0), allow_broker_probe=False)
            self.assertEqual(len(ords_1448), 1)
            self.assertEqual(ords_1448[0]["expires_at"], "2026-10-19 14:55:00")

            # At >= 14:55:00, new Book-2 reservations MUST be blocked
            ords_1455 = bridge.reserve_book2_pending_limit_orders(sig, now_dt=datetime(2026, 10, 19, 14, 55, 0), allow_broker_probe=False)
            self.assertEqual(len(ords_1455), 0)

            # Fill FNO01 at 14:50, verify OPEN at 15:09:59, and MANDATORY_1510_MIS_SQUAREOFF at 15:10:00
            r_fill = bridge.reconcile_orders_and_positions(live_quotes={"FNO01.NS": {"price": 1000.50}}, now_dt=datetime(2026, 10, 19, 14, 50, 0))
            self.assertEqual(r_fill["filled"], 1)
            r_pre = bridge.reconcile_orders_and_positions(live_quotes={"FNO01.NS": {"price": 998.0}}, now_dt=datetime(2026, 10, 19, 15, 9, 59))
            self.assertEqual(r_pre["closed"], 0)
            r_sq = bridge.reconcile_orders_and_positions(live_quotes={"FNO01.NS": {"price": 998.0}}, now_dt=datetime(2026, 10, 19, 15, 10, 0))
            self.assertEqual(r_sq["closed"], 1)
            self.assertEqual(bridge.get_positions_and_reservations()["closed_positions"][0]["exit_reason"], "MANDATORY_1510_MIS_SQUAREOFF")

            # Orchestrator workspace status phase label uses 15:10 IST
            orch = ConsensusV3Orchestrator(db_path=db_p, model_dir=os.path.join(tmp, "m_b3"))
            orch._last_sweep_summary = {"book2_intraday_sniffer": {}, "model_provenance": {}}
            with patch("app.analytics.consensus_v3.orchestrator.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 10, 19, 15, 10, 0)
                st = orch.get_workspace_status()
                self.assertEqual(st["session_phase_label"], "15:10 IST • MANDATORY MIS SQUAREOFF")

    def test_bug4_l1_depth_guard_and_forming_bar_postonly_gap(self):
        from unittest.mock import MagicMock, patch
        from app.data import upstox_provider

        # 1. Verify fetch_upstox_bulk_quotes parses depth + crossed book + preserves legacy keys
        upstox_provider._quotes_cache.clear()
        fake_payload = {
            "data": {
                "NSE_EQ:TATASTEEL": {
                    "last_price": 150.0,
                    "average_price": 149.8,
                    "ohlc": {"open": 149.0, "high": 151.0, "low": 148.5, "close": 149.5},
                    "net_change": 0.5,
                    "volume": 120000,
                    "depth": {
                        "buy": [{"price": 149.95, "quantity": 500, "orders": 2}],
                        "sell": [{"price": 150.05, "quantity": 400, "orders": 1}],
                    },
                },
                "NSE_EQ:INFY": {
                    "last_price": 1500.0,
                    "ohlc": {"open": 1490.0, "high": 1505.0, "low": 1488.0, "close": 1495.0},
                    "depth": {"buy": [], "sell": []},
                },
                "NSE_EQ:TCS": {
                    "last_price": 4000.0,
                    "ohlc": {"open": 3990.0, "high": 4010.0, "low": 3985.0, "close": 3995.0},
                    "depth": {
                        "buy": [{"price": 4002.0, "quantity": 10}],
                        "sell": [{"price": 4000.0, "quantity": 10}],
                    },
                },
            }
        }
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = fake_payload
        with patch("app.data.upstox_provider.get_upstox_token", return_value="tok_" + "x" * 24), \
             patch("app.data.upstox_provider._GLOBAL_UPSTOX_LIMITER.is_circuit_open", return_value=False), \
             patch("app.data.upstox_provider.get_instrument_key", side_effect=lambda s: f"NSE_EQ|{s}"), \
             patch("app.data.upstox_provider.upstox_request", return_value=mock_resp):
            q_out = upstox_provider.fetch_upstox_bulk_quotes(["TATASTEEL.NS", "INFY.NS", "TCS.NS"], max_cache_age_sec=0.0)
            ts_q = q_out["TATASTEEL.NS"]
            self.assertEqual(ts_q["ltp"], 150.0)
            self.assertEqual(ts_q["best_bid"], 149.95)
            self.assertEqual(ts_q["best_ask"], 150.05)
            self.assertEqual(ts_q["best_bid_qty"], 500)
            self.assertEqual(ts_q["best_ask_qty"], 400)
            self.assertAlmostEqual(ts_q["spread_bps"], 6.67, places=2)
            self.assertFalse(ts_q["is_crossed_book"])
            self.assertTrue(ts_q["has_l1_depth"])
            self.assertFalse(q_out["INFY.NS"]["has_l1_depth"])
            self.assertTrue(q_out["TCS.NS"]["is_crossed_book"])
            self.assertFalse(q_out["TCS.NS"]["has_l1_depth"])

        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "bug4.db")
            _seed_synthetic_fno_db(db_p)
            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="bug4_test")
            bridge.configure_arena_capital(100000.0, MODE_DUAL_FUNDED_ARENA, reset_ledger=True)

            sig = [{
                "book": "BOOK2_INTRADAY_MIS_SNIFFER",
                "ticker": "FNO01.NS",
                "symbol": "FNO01",
                "instrument_key": "NSE_EQ|INE000000001",
                "direction": "SELL",
                "signal_close_px": 498.0,
                "limit_entry_px": 500.0,
                "stop_loss_px": 508.0,
                "scheduled_exit_hm": "15:10",
                "liq_1pct_bar_inr": 500000.0,
            }]

            # 2. LIVE_UPSTOX armed L1 pre-order guard blocks missing, crossed, wide (>15 bps), and touching bids
            with patch.object(bridge, "_fetch_upstox_readiness", return_value={"token_valid": True, "available_margin": 500000.0, "is_read_only_token": False, "static_ip_matched": True}):
                bridge.arm_live_trading(pin=bridge.current_pin)
                bad_l1_cases = [
                    {},  # missing
                    {"FNO01.NS": {"best_bid": 500.0, "best_ask": 500.1}},  # best_bid >= limit_px
                    {"FNO01.NS": {"best_bid": 498.0, "best_ask": 499.0}},  # spread 20 bps > 15 bps
                    {"FNO01.NS": {"best_bid": 499.5, "best_ask": 499.0}},  # crossed book
                    {"FNO01.NS": {"best_bid": 0.0, "best_ask": 499.5}},    # zero bid
                ]
                for bad_l1 in bad_l1_cases:
                    with patch.object(bridge, "_fetch_l1_quotes", return_value=bad_l1), \
                         patch.object(bridge, "_route_upstox_equity_order") as m_route:
                        res_bad = bridge.reserve_book2_pending_limit_orders(sig, now_dt=datetime(2026, 10, 19, 10, 15, 5), allow_broker_probe=True)
                        self.assertEqual(len(res_bad), 0)
                        m_route.assert_not_called()

                # Direct _route_upstox_equity_order also fails closed with BLOCKED_L1_GUARD
                r_direct_blk = bridge._route_upstox_equity_order(
                    order_or_pos_id="ord_l1_blk",
                    action="SELL",
                    product_type="MIS",
                    order_type="LIMIT",
                    ticker="FNO01.NS",
                    instrument_key="NSE_EQ|INE000000001",
                    shares=10,
                    price=500.0,
                    l1_quote={"best_bid": 500.05, "best_ask": 500.15},
                )
                self.assertFalse(r_direct_blk["routed"])
                self.assertEqual(r_direct_blk["state"], "BLOCKED_L1_GUARD")

                # Valid L1 (0 < best_bid=499.5 < 500.0, spread=5 bps <= 15 bps) routes cleanly!
                with patch.object(bridge, "_fetch_l1_quotes", return_value={"FNO01.NS": {"best_bid": 499.50, "best_ask": 499.75}}), \
                     patch.object(bridge, "_route_upstox_equity_order", return_value={"routed": True, "state": "SUBMITTED", "broker_order_id": "UPX_L1_OK", "avg_fill_price": None}):
                    res_ok = bridge.reserve_book2_pending_limit_orders(sig, now_dt=datetime(2026, 10, 19, 10, 15, 5), allow_broker_probe=True)
                    self.assertEqual(len(res_ok), 1)
                bridge.disarm_live_trading()

            # 3. PAPER_SHADOW forming-bar open gap guard (`brow is None` and across slot boundary) + NaN L1 & post-expiry LTP guard
            bridge_gap = ConsensusArenaExecutionBridge(db_path=db_p, session_id="bug4_gap_test")
            bridge_gap.configure_arena_capital(100000.0, MODE_DUAL_FUNDED_ARENA, reset_ledger=True)
            ords_gap = bridge_gap.reserve_book2_pending_limit_orders(sig, now_dt=datetime(2026, 10, 19, 10, 15, 5), allow_broker_probe=False)
            self.assertEqual(len(ords_gap), 1)
            rec_gap = bridge_gap.reconcile_orders_and_positions(
                live_quotes={"FNO01.NS": {"price": 501.0, "bar_open": 500.25}},
                now_dt=datetime(2026, 10, 19, 10, 18, 0),
            )
            self.assertEqual(rec_gap["filled"], 0)
            self.assertEqual(rec_gap["expired"], 1)
            self.assertEqual(bridge_gap.get_positions_and_reservations()["recent_order_reservations"][0]["status"], "CANCELLED_POSTONLY_GAP")

            # Boundary forming-bar open gap guard (`10:15` completed brow did not fill, new `10:30` bar gaps >= limit)
            with sqlite3.connect(db_p) as c_bg:
                c_bg.execute("INSERT INTO ohlcv VALUES ('BGAP.NS', '2026-10-19 10:15:00', '15m', 498.0, 499.5, 497.0, 498.5, 50000.0);")
                c_bg.commit()
            ords_bg = bridge_gap.reserve_book2_pending_limit_orders(
                [{
                    "book": "BOOK2_INTRADAY_MIS_SNIFFER", "ticker": "BGAP.NS", "symbol": "BGAP",
                    "instrument_key": "NSE_EQ|INE000000099", "bar_dt": "2026-10-19 10:00:00",
                    "direction": "SELL", "signal_close_px": 498.0, "limit_entry_px": 500.0, "stop_loss_px": 508.0,
                    "scheduled_exit_hm": "15:10", "liq_1pct_bar_inr": 500000.0,
                }],
                now_dt=datetime(2026, 10, 19, 10, 15, 5),
                allow_broker_probe=False,
            )
            self.assertEqual(len(ords_bg), 1)
            rec_bg = bridge_gap.reconcile_orders_and_positions(
                live_quotes={"BGAP.NS": {"price": 501.0, "bar_open": 500.5}},
                now_dt=datetime(2026, 10, 19, 10, 30, 2),
            )
            self.assertEqual(rec_bg["filled"], 0)
            self.assertEqual(rec_bg["expired"], 1)
            self.assertEqual(bridge_gap.get_positions_and_reservations()["recent_order_reservations"][0]["status"], "CANCELLED_POSTONLY_GAP")

            # NaN L1 rejection + Day-2 disarmed mid-cycle deployment guard + post-14:55 LTP expiry guard
            ok_nan, reason_nan = bridge_gap._evaluate_l1_postonly_guard("SELL", 500.0, {"best_bid": float("nan"), "best_ask": 500.1})
            self.assertFalse(ok_nan)
            self.assertEqual(reason_nan, "L1_DEPTH_INVALID")

            ords_late = bridge_gap.reserve_book2_pending_limit_orders(
                [{
                    "book": "BOOK2_INTRADAY_MIS_SNIFFER", "ticker": "FNO02.NS", "symbol": "FNO02",
                    "instrument_key": "NSE_EQ|INE000000002", "direction": "SELL",
                    "signal_close_px": 498.0, "limit_entry_px": 500.0, "stop_loss_px": 508.0,
                    "scheduled_exit_hm": "15:10", "liq_1pct_bar_inr": 500000.0,
                }],
                now_dt=datetime(2026, 10, 19, 14, 48, 0),
                allow_broker_probe=False,
            )
            self.assertEqual(len(ords_late), 1)
            rec_late = bridge_gap.reconcile_orders_and_positions(
                live_quotes={"FNO02.NS": {"price": 501.0}},
                now_dt=datetime(2026, 10, 19, 14, 56, 0),
            )
            self.assertEqual(rec_late["filled"], 0)
            self.assertEqual(rec_late["expired"], 1)
            self.assertEqual(bridge_gap.get_positions_and_reservations()["recent_order_reservations"][0]["status"], "EXPIRED_UNFILLED")

            # Day-2 disarmed live session must NOT accept mid-cycle PAPER_SHADOW entries into live_session_id
            mid_live_skip = bridge.deploy_midcycle_inflow_4gate(
                {"as_of_date": "2026-10-19", "candidates": []},
                {"as_of_date": "2026-10-19", "macro_shield": {"regime": "BULL_FULL_DEPLOYMENT", "target_slots": 8}, "approved_rebalance_leaders": []},
                now_dt=datetime(2026, 10, 20, 10, 0, 0),
            )
            self.assertEqual(mid_live_skip["status"], "SKIPPED_DISARMED_LIVE_SESSION")
            self.assertEqual(mid_live_skip["opened"], 0)

    def test_bug5_entry_candle_intrabar_sl_and_slot_aligned_ltp_sl(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "bug5.db")
            conn = sqlite3.connect(db_p)
            conn.execute("CREATE TABLE fno_universe_cache (symbol TEXT PRIMARY KEY);")
            conn.execute("CREATE TABLE ohlcv (ticker TEXT, date TEXT, timeframe TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL);")
            conn.execute("INSERT INTO fno_universe_cache VALUES ('SLSELL'), ('SLBUY'), ('MIDSL'), ('PREIMM');")
            # SLSELL: 11:00 bar opens 498 (<500), hits 502 (fills 500) AND hits 510 (>= sl=508), closes 499!
            conn.execute("INSERT INTO ohlcv VALUES ('SLSELL.NS', '2026-10-19 11:00:00', '15m', 498.0, 510.0, 497.0, 499.0, 100000);")
            # SLBUY: 11:00 bar opens 502 (>500), dips to 490 (<= sl=492, fills 500 and stops out!), closes 501!
            conn.execute("INSERT INTO ohlcv VALUES ('SLBUY.NS',  '2026-10-19 11:00:00', '15m', 502.0, 503.0, 490.0, 501.0, 100000);")
            # MIDSL: 10:15 bar completes at 10:30 with high=512 >= sl=508 (close=497 < sl)
            conn.execute("INSERT INTO ohlcv VALUES ('MIDSL.NS',  '2026-10-19 10:15:00', '15m', 498.0, 512.0, 496.0, 497.0, 100000);")
            # PREIMM: pre-entry 10:45 bar wicks 515 >= sl=508 (must NOT stop out position entered at 11:00:05!)
            conn.execute("INSERT INTO ohlcv VALUES ('PREIMM.NS', '2026-10-19 10:45:00', '15m', 498.0, 515.0, 496.0, 498.0, 100000);")
            conn.commit()
            conn.close()

            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="bug5_test")
            bridge.configure_arena_capital(400000.0, "BOOK2_INTRADAY_MIS_SNIFFER", reset_ledger=True)

            # 1. Single-bar fill + intrabar SL for BOTH SELL and BUY
            sigs_same = [
                {
                    "book": "BOOK2_INTRADAY_MIS_SNIFFER", "ticker": "SLSELL.NS", "symbol": "SLSELL",
                    "instrument_key": "NSE_EQ|INE000005001", "bar_dt": "2026-10-19 10:45:00",
                    "direction": "SELL", "signal_close_px": 498.0, "limit_entry_px": 500.0,
                    "stop_loss_px": 508.0, "scheduled_exit_hm": "15:10", "liq_1pct_bar_inr": 500000.0,
                },
                {
                    "book": "BOOK2_INTRADAY_MIS_SNIFFER", "ticker": "SLBUY.NS", "symbol": "SLBUY",
                    "instrument_key": "NSE_EQ|INE000005002", "bar_dt": "2026-10-19 10:45:00",
                    "direction": "BUY", "signal_close_px": 502.0, "limit_entry_px": 500.0,
                    "stop_loss_px": 492.0, "scheduled_exit_hm": "15:10", "liq_1pct_bar_inr": 500000.0,
                },
            ]
            bridge.reserve_book2_pending_limit_orders(sigs_same, now_dt=datetime(2026, 10, 19, 11, 0, 2), allow_broker_probe=False)
            rec_same = bridge.reconcile_orders_and_positions(live_quotes={}, now_dt=datetime(2026, 10, 19, 11, 15, 1))
            self.assertEqual(rec_same["filled"], 2)
            self.assertEqual(rec_same["closed"], 2)
            closed_b5 = bridge.get_positions_and_reservations()["closed_positions"]
            self.assertTrue(all(p["exit_reason"] == "STRUCTURAL_STOP_HIT" for p in closed_b5))

            # 2. Mid-bar LTP fill at 10:18:00 (>90s past 10:15:00) MUST still catch completed 10:15:00 bar SL wick at 10:30:05!
            bridge.configure_arena_capital(400000.0, "BOOK2_INTRADAY_MIS_SNIFFER", reset_ledger=True)
            bridge.reserve_book2_pending_limit_orders([{
                "book": "BOOK2_INTRADAY_MIS_SNIFFER", "ticker": "MIDSL.NS", "symbol": "MIDSL",
                "instrument_key": "NSE_EQ|INE000005003", "bar_dt": "2026-10-19 10:00:00",
                "direction": "SELL", "signal_close_px": 498.0, "limit_entry_px": 500.0,
                "stop_loss_px": 508.0, "scheduled_exit_hm": "15:10", "liq_1pct_bar_inr": 500000.0,
            }], now_dt=datetime(2026, 10, 19, 10, 15, 5), allow_broker_probe=False)
            r_mid_fill = bridge.reconcile_orders_and_positions(
                live_quotes={"MIDSL.NS": {"price": 500.50}},
                now_dt=datetime(2026, 10, 19, 10, 18, 0),
            )
            self.assertEqual(r_mid_fill["filled"], 1)
            self.assertEqual(r_mid_fill["closed"], 0)
            r_mid_sl = bridge.reconcile_orders_and_positions(
                live_quotes={"MIDSL.NS": {"price": 497.0}},
                now_dt=datetime(2026, 10, 19, 10, 30, 5),
            )
            self.assertEqual(r_mid_sl["closed"], 1)
            self.assertEqual(bridge.get_positions_and_reservations()["closed_positions"][0]["exit_reason"], "STRUCTURAL_STOP_HIT")

            # 3. Pre-entry bar immunity: 10:45:00 wick must NOT stop out position filled at 11:00:05
            bridge.reserve_book2_pending_limit_orders([{
                "book": "BOOK2_INTRADAY_MIS_SNIFFER", "ticker": "PREIMM.NS", "symbol": "PREIMM",
                "instrument_key": "NSE_EQ|INE000005004", "bar_dt": "2026-10-19 10:45:00",
                "direction": "SELL", "signal_close_px": 498.0, "limit_entry_px": 500.0,
                "stop_loss_px": 508.0, "scheduled_exit_hm": "15:10", "liq_1pct_bar_inr": 500000.0,
            }], now_dt=datetime(2026, 10, 19, 11, 0, 2), allow_broker_probe=False)
            r_imm = bridge.reconcile_orders_and_positions(
                live_quotes={"PREIMM.NS": {"price": 500.50}},
                now_dt=datetime(2026, 10, 19, 11, 0, 5),
            )
            self.assertEqual(r_imm["filled"], 1)
            self.assertEqual(r_imm["closed"], 0)

    def test_bug6_45k_slot_sizing_unpledged_var_elm_1x_cap_fills_and_post_sl_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = os.path.join(tmp, "bug6.db")
            _seed_synthetic_fno_db(db_p)
            bridge = ConsensusArenaExecutionBridge(db_path=db_p, session_id="bug6_test")

            # 1. Verify concentrated >=₹45k Book-2 slot ladder across ₹50k / ₹90k / ₹1L
            a50 = bridge.configure_arena_capital(50000.0, MODE_DUAL_FUNDED_ARENA, reset_ledger=True)
            self.assertEqual(a50["book2_slots"]["max"], 1)
            self.assertEqual(a50["book2_slots"]["slot_cap_inr"], 48000.0)

            a90 = bridge.configure_arena_capital(90000.0, MODE_DUAL_FUNDED_ARENA, reset_ledger=True)
            self.assertEqual(a90["book2_slots"]["max"], 2)
            self.assertEqual(a90["book2_slots"]["slot_cap_inr"], 45000.0)

            a100 = bridge.configure_arena_capital(100000.0, MODE_DUAL_FUNDED_ARENA, reset_ledger=True)
            self.assertEqual(a100["book2_slots"]["max"], 2)
            self.assertEqual(a100["book2_slots"]["slot_cap_inr"], 47500.0)

            # 2. On ₹1,00,000 DUAL_FUNDED_CASH_ARENA with ₹80,000 Book-1 CNC invested and ₹20,000 unpledged cash:
            #    2 Book-2 ₹47,500 1x-notional MIS orders reserve ~21% VaR+ELM (₹19,950 <= ₹20,000)!
            bridge.execute_book1_scheduled_rebalance(
                {"as_of_date": "2026-10-19", "candidates": []},
                {
                    "as_of_date": "2026-10-19",
                    "macro_shield": {"regime": "BULL_FULL_DEPLOYMENT", "target_slots": 8},
                    "approved_rebalance_leaders": [
                        {"ticker": "B1A.NS", "symbol": "B1A", "sector": "AUTO", "instrument_key": "NSE_EQ|INE1", "close_px": 1000.0, "vol60_ann_pct": 30.0},
                        {"ticker": "B1B.NS", "symbol": "B1B", "sector": "PHARMA", "instrument_key": "NSE_EQ|INE2", "close_px": 1000.0, "vol60_ann_pct": 30.0},
                        {"ticker": "B1C.NS", "symbol": "B1C", "sector": "IT", "instrument_key": "NSE_EQ|INE3", "close_px": 1000.0, "vol60_ann_pct": 30.0},
                        {"ticker": "B1D.NS", "symbol": "B1D", "sector": "FMCG", "instrument_key": "NSE_EQ|INE4", "close_px": 1000.0, "vol60_ann_pct": 30.0},
                    ],
                },
                force=True,
                now_dt=datetime(2026, 10, 19, 9, 30, 0),
            )
            sigs_b2 = [
                {
                    "book": "BOOK2_INTRADAY_MIS_SNIFFER", "ticker": f"FNO0{i}.NS", "symbol": f"FNO0{i}",
                    "instrument_key": f"NSE_EQ|INE00000000{i}", "direction": "SELL",
                    "signal_close_px": 995.0, "limit_entry_px": 1000.0, "stop_loss_px": 1015.0,
                    "scheduled_exit_hm": "15:10", "liq_1pct_bar_inr": 500000.0, "telemetry": {"atr_pct": 2.0},
                }
                for i in range(1, 4)
            ]
            ords_100k = bridge.reserve_book2_pending_limit_orders(
                sigs_b2, now_dt=datetime(2026, 10, 19, 10, 15, 5), allow_broker_probe=False
            )
            self.assertEqual(len(ords_100k), 2)
            for o in ords_100k:
                self.assertEqual(o["exposure_notional_inr"], 47000.0)
                self.assertEqual(o["margin_source"], "UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN")
                self.assertLessEqual(o["reserved_cash_inr"], 10000.0)
            acct_100k = bridge.get_arena_accounting(now_dt=datetime(2026, 10, 19, 10, 15, 5))
            self.assertTrue(acct_100k["cash_conservation_invariant_ok"])
            self.assertTrue(acct_100k["book2_slots"]["mis_notional_within_1x_equity_ok"])
            self.assertLessEqual(acct_100k["book2_slots"]["open_and_pending_notional_inr"], 100000.0)

            # Verify MIS_3X on ₹1,00,000 STILL enforces total MIS notional <= 1.0x equity
            bridge.configure_arena_capital(100000.0, MODE_DUAL_FUNDED_ARENA, margin_mode="MIS_3X", reset_ledger=True)
            ords_3x = bridge.reserve_book2_pending_limit_orders(
                sigs_b2, now_dt=datetime(2026, 10, 19, 10, 15, 5), allow_broker_probe=False
            )
            self.assertEqual(len(ords_3x), 2)
            self.assertLessEqual(sum(o["exposure_notional_inr"] for o in ords_3x), 100000.0)

            # 3. M2 Advisory: Tight fills_today < 6 cap in reconcile_orders_and_positions (5 -> max 1 fill, NEVER 7!)
            bridge.configure_arena_capital(100000.0, MODE_DUAL_FUNDED_ARENA, reset_ledger=True)
            ords_cap = bridge.reserve_book2_pending_limit_orders(
                sigs_b2[:2], now_dt=datetime(2026, 10, 19, 10, 15, 5), allow_broker_probe=False
            )
            self.assertEqual(len(ords_cap), 2)
            with bridge._conn() as c_cap:
                c_cap.execute(
                    "INSERT OR REPLACE INTO consensus_v3_risk_ledger (session_date, realized_pnl_inr, fills_today, circuit_breaker_tripped, updated_at) VALUES ('2026-10-19', 0.0, 5, 0, '2026-10-19 10:15:00')"
                )
                c_cap.commit()
            rec_cap = bridge.reconcile_orders_and_positions(
                live_quotes={"FNO01.NS": {"price": 1001.0}, "FNO02.NS": {"price": 1001.0}},
                now_dt=datetime(2026, 10, 19, 10, 20, 0),
            )
            self.assertEqual(rec_cap["filled"], 1)
            self.assertEqual(rec_cap["expired"], 1)
            self.assertEqual(bridge.get_arena_accounting(now_dt=datetime(2026, 10, 19, 10, 20, 0))["circuit_breaker"]["fills_today"], 6)

            # 4. M2 Advisory: Post-SL gap loss reduces equity so queued PENDING_RESERVED order MUST NOT fill into cash breach!
            bridge.configure_arena_capital(100000.0, MODE_DUAL_FUNDED_ARENA, reset_ledger=True)
            ords_sl = bridge.reserve_book2_pending_limit_orders(
                [
                    {
                        "book": "BOOK2_INTRADAY_MIS_SNIFFER", "ticker": "FNO01.NS", "symbol": "FNO01",
                        "instrument_key": "NSE_EQ|INE000000001", "direction": "SELL",
                        "signal_close_px": 995.0, "limit_entry_px": 1000.0, "stop_loss_px": 1015.0,
                        "scheduled_exit_hm": "15:10", "liq_1pct_bar_inr": 500000.0, "telemetry": {"atr_pct": 2.0},
                    }
                ],
                now_dt=datetime(2026, 10, 19, 10, 15, 5),
                allow_broker_probe=False,
            )
            self.assertEqual(len(ords_sl), 1)
            bridge.reconcile_orders_and_positions(
                live_quotes={"FNO01.NS": {"price": 1000.5}},
                now_dt=datetime(2026, 10, 19, 10, 16, 0),
            )
            # Queue a 2nd ₹47k order while FNO01 is open
            ords_sl2 = bridge.reserve_book2_pending_limit_orders(
                [
                    {
                        "book": "BOOK2_INTRADAY_MIS_SNIFFER", "ticker": "FNO02.NS", "symbol": "FNO02",
                        "instrument_key": "NSE_EQ|INE000000002", "direction": "SELL",
                        "signal_close_px": 995.0, "limit_entry_px": 1000.0, "stop_loss_px": 1015.0,
                        "scheduled_exit_hm": "15:10", "liq_1pct_bar_inr": 500000.0, "telemetry": {"atr_pct": 2.0},
                    }
                ],
                now_dt=datetime(2026, 10, 19, 10, 17, 0),
                allow_broker_probe=False,
            )
            self.assertEqual(len(ords_sl2), 1)
            # Simulate catastrophic gap up on FNO01 (1000 -> 2850, ~-₹87k loss) in the SAME reconcile tick where FNO02 trades through 1000.5!
            rec_post_sl = bridge.reconcile_orders_and_positions(
                live_quotes={"FNO01.NS": {"price": 2850.0}, "FNO02.NS": {"price": 1000.5}},
                now_dt=datetime(2026, 10, 19, 10, 20, 0),
            )
            self.assertEqual(rec_post_sl["closed"], 1)
            self.assertEqual(rec_post_sl["filled"], 0)
            self.assertEqual(rec_post_sl["expired"], 1)
            acct_post_sl = bridge.get_arena_accounting(now_dt=datetime(2026, 10, 19, 10, 20, 0))
            self.assertTrue(acct_post_sl["cash_conservation_invariant_ok"])


if __name__ == "__main__":
    unittest.main()
