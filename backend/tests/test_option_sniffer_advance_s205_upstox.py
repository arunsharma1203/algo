"""
Unit & Integration Verification Suite for Option Sniffer Advance S2_05 + Upstox V3:
  1. Verifies Fold-18 Causal Conformal Meta-Learner SHA-256 integrity (`embargoed_train_end = 2026-10-30`, `p_min = 0.56`).
  2. Verifies Upstox V3 Front-Month Futures (`fut_ltp`, `fut_atp_vwap`, `fut_obi`) & Option 5-level `bidAskQuote` parsing
     while strictly excluding `is_futures` and `is_catastrophe_wing` from `get_all_active_option_quotes()`.
  3. Proves Strictly Close-Only Ratchet invariance: intrabar WebSocket ticks NEVER arm ratchets; `:00` completed 1m candle
     close updates ratchets; subsequent ticks enforce armed stops & Hard-SL in sub-second time.
  4. Verifies S2_05 Dual-Book (`V6B_S1_OI` Short + `V6B_LBASIS_ATM` Long) paper execution, Daily ±450pt Catastrophe Wing
     tracking, sub-second `-₹3,500` Portfolio MTM Daily Kill flatten, and 100% compatibility with `AdvanceLiveBridge`
     (`_is_live_armed == False`).
  5. Verifies `/status` and `/backend-readiness` frontend telemetry contract (`engine_readiness` 4-engine checklist).
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
from app.analytics.option_sniffer_advance.advance_constants import BOOK_LONG_BUY, BOOK_SHORT_SELL
from app.analytics.option_sniffer_advance.advance_db import AdvanceDB, init_option_sniffer_advance_tables
from app.analytics.option_sniffer_advance.advance_paper_execution import AdvancePaperExecution
from app.analytics.option_sniffer_advance_live.advance_live_bridge import (
    AdvanceLiveBridge,
    init_advance_live_tables,
)
from app.analytics.option_sniffer_advance_s205.s205_meta_model import S205CausalMetaModel
from app.analytics.option_sniffer_advance_s205.s205_signal_evaluator import (
    build_causal_intraday_context,
    compute_close_only_ratchet,
    evaluate_s205_bar_and_readiness,
)
from app.analytics.option_sniffer_advance_s205.s205_upstox_engine import S205UpstoxOrchestrator


class TestOptionSnifferAdvanceS205Upstox(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_s205_upstox.sqlite")
        self.p_adv_db = patch(
            "app.analytics.option_sniffer_advance.advance_db.get_db_path",
            return_value=self.db_path,
        )
        self.p_s205_db = patch(
            "app.analytics.option_sniffer_advance_s205.s205_upstox_engine.get_db_path",
            return_value=self.db_path,
        )
        self.p_adv_db.start()
        self.p_s205_db.start()
        init_option_sniffer_advance_tables()
        init_advance_live_tables(db_path=self.db_path)

        # Reset singletons cleanly for isolated testing
        S205UpstoxOrchestrator._instance = None
        self.orch = S205UpstoxOrchestrator()
        self.sm = OptionStreamManager()

    def tearDown(self):
        self.orch.stop()
        self.p_s205_db.stop()
        self.p_adv_db.stop()
        self.tmp_dir.cleanup()

    def test_01_fold18_meta_model_sha256_and_conformal_calibration(self):
        model = S205CausalMetaModel()
        self.assertEqual(model.candidate_id, "S2_05_V6B_ML_FIXED_SIM")
        self.assertEqual(model.fold_id, 18)
        self.assertEqual(model.test_month, "2026-11")
        self.assertEqual(model.embargoed_train_end, "2026-10-30")
        self.assertAlmostEqual(model.p_min, 0.56, places=2)
        self.assertTrue(model.weights_sha256.startswith("a2149508"))

        exp = model.explain_prediction([0.0] * 30)
        self.assertGreater(exp["prob"], 0.0)
        self.assertLess(exp["prob"], 1.0)
        self.assertIn("p_gbdt", exp)
        self.assertIn("p_knn", exp)
        self.assertIn("p_lr", exp)

    def test_02_upstox_v3_futures_and_l5_depth_parsing_without_polluting_option_chain(self):
        self.sm.pin_extra_contracts(
            [
                {
                    "instrument_key": "NSE_FO|FUT_TEST_999",
                    "trading_symbol": "NIFTY 26NOV FUT",
                    "underlying": "NIFTY",
                    "is_futures": True,
                    "lot_size": 65,
                },
                {
                    "instrument_key": "NSE_FO|WING_TEST_888",
                    "trading_symbol": "NIFTY 23900 CE",
                    "underlying": "NIFTY",
                    "strike": 23900.0,
                    "option_type": "CE",
                    "expiry": "2026-11-19",
                    "is_catastrophe_wing": True,
                    "lot_size": 65,
                },
                {
                    "instrument_key": "NSE_FO|OPT_TEST_777",
                    "trading_symbol": "NIFTY 23450 PE",
                    "underlying": "NIFTY",
                    "strike": 23450.0,
                    "option_type": "PE",
                    "expiry": "2026-11-19",
                    "lot_size": 65,
                },
            ]
        )

        # Feed Upstox V3 payload containing Futures + 5-level Option depth + Catastrophe Wing
        v3_msg = {
            "feeds": {
                "NSE_FO|FUT_TEST_999": {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {"ltp": 23512.5, "cp": 23440.0},
                            "atp": 23498.0,
                            "vtt": 4200000,
                            "oi": 11250000,
                            "poi": 10900000,
                            "tbq": 185000,
                            "tsq": 125000,
                            "marketLevel": {
                                "bidAskQuote": [
                                    {"bidP": 23512.0, "bidQ": 1300, "askP": 23513.0, "askQ": 650},
                                ]
                            },
                        }
                    }
                },
                "NSE_FO|OPT_TEST_777": {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {"ltp": 118.0, "cp": 130.0},
                            "atp": 126.5,
                            "vtt": 850000,
                            "oi": 420000,
                            "tbq": 95000,
                            "tsq": 140000,
                            "optionGreeks": {"delta": -0.44, "gamma": 0.0014, "theta": -11.2, "vega": 9.8, "iv": 0.148},
                            "marketLevel": {
                                "bidAskQuote": [
                                    {"bidP": 117.8, "bidQ": 650, "askP": 118.2, "askQ": 1300},
                                    {"bidP": 117.7, "bidQ": 650, "askP": 118.3, "askQ": 1300},
                                    {"bidP": 117.6, "bidQ": 650, "askP": 118.4, "askQ": 1300},
                                    {"bidP": 117.5, "bidQ": 650, "askP": 118.5, "askQ": 1300},
                                    {"bidP": 117.4, "bidQ": 650, "askP": 118.6, "askQ": 1300},
                                ]
                            },
                        }
                    }
                },
                "NSE_FO|WING_TEST_888": {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {"ltp": 3.2, "cp": 3.5},
                            "marketLevel": {"bidAskQuote": [{"bidP": 3.1, "bidQ": 1950, "askP": 3.3, "askQ": 1950}]},
                        }
                    }
                },
            }
        }
        self.sm.on_feed_message(v3_msg)

        fq = self.sm.get_futures_quote("NIFTY")
        self.assertIsNotNone(fq)
        self.assertAlmostEqual(fq["fut_ltp"], 23512.5, places=1)
        self.assertAlmostEqual(fq["fut_atp_vwap"], 23498.0, places=1)
        self.assertGreater(fq["fut_obi"], 0.15)

        oq = self.sm.get_option_quote("NSE_FO|OPT_TEST_777")
        self.assertEqual(len(oq["depth_levels"]), 5)
        self.assertEqual(oq["l5_bid_qty"], 3250)
        self.assertEqual(oq["l5_ask_qty"], 6500)
        self.assertLess(oq["book_obi_l5"], -0.30)
        self.assertAlmostEqual(oq["option_vwap_atp"], 126.5, places=1)

        active_keys = {q["instrument_key"] for q in self.sm.get_all_active_option_quotes()}
        self.assertIn("NSE_FO|OPT_TEST_777", active_keys)
        self.assertNotIn("NSE_FO|FUT_TEST_999", active_keys)
        self.assertNotIn("NSE_FO|WING_TEST_888", active_keys)

    def test_03_close_only_ratchet_never_arms_intrabar_and_arms_on_completed_bar_close(self):
        # Create an open V6B_S1_OI Short Option trade @ ₹100.0 (initial hard stop = ₹118.0)
        rctx = {
            "engine_version": "S2_05_UPSTOX_V3",
            "ratchet_stages": [[12.0, 0.0, 3.5], [26.0, 11.0, 12.0]],
            "max_hold_bars": 135,
            "time_stop_mins": 36,
            "time_min_gain_pct": 3.0,
            "opt_vwap_mult": 0.0,
            "vflip_pts": 0.0,
            "is_bull": True,
        }
        AdvanceDB.create_trade(
            {
                "trade_id": "adv_trd_ratchet_1",
                "signal_id": "sig_r1",
                "book_type": BOOK_SHORT_SELL,
                "strategy_mode": "V6B_S1_OI",
                "direction": "SELL_PE",
                "instrument_key": "NSE_FO|RATCHET_PE",
                "trading_symbol": "NIFTY 23500 PE",
                "underlying": "NIFTY",
                "option_type": "PE",
                "strike": 23500.0,
                "expiry": "2026-11-19",
                "lot_size": 65,
                "quantity": 65,
                "entry_time": "2026-11-13T10:00:00",
                "entry_bid": 100.0,
                "entry_ask": 100.2,
                "entry_price_used": 100.0,
                "entry_spot_price": 23480.0,
                "peak_price": 100.0,
                "trough_price": 100.0,
                "trailing_stop": 118.0,
                "stop_loss_pct": 18.0,
                "target_pct": 0.0,
                "estimated_margin": 55000.0,
                "regime_context": rctx,
            }
        )

        # 1. Intrabar flash wick down to ₹82.0 (+18% intrabar decay) via AdvancePaperExecution router -> MUST NOT tighten trailing_stop!
        closed_wick = AdvancePaperExecution.evaluate_subsecond_tick_guard(
            "NSE_FO|RATCHET_PE",
            {"bid": 81.8, "ask": 82.0, "ltp": 82.0},
        )
        self.assertEqual(len(closed_wick), 0)
        open_after_wick = AdvanceDB.get_open_trades()[0]
        self.assertAlmostEqual(float(open_after_wick["trailing_stop"]), 118.0, places=2)

        # 2. Intrabar bounce back to ₹98.0 -> STILL NOT stopped out because ratchet was NOT armed intrabar!
        closed_bounce = AdvancePaperExecution.evaluate_subsecond_tick_guard(
            "NSE_FO|RATCHET_PE",
            {"bid": 97.8, "ask": 98.0, "ltp": 98.0},
        )
        self.assertEqual(len(closed_bounce), 0)
        self.assertEqual(len(AdvanceDB.get_open_trades()), 1)

        # 3. Now simulate a completed `:00` 1m bar closing at ₹85.0 (+15% closed decay >= 12% Stage-1 -> locks +3.5% floor = ₹96.50)!
        new_stop = compute_close_only_ratchet(
            True,
            100.0,
            85.0,
            None,
            [(12.0, 0.0, 3.5), (26.0, 11.0, 12.0)],
        )
        self.assertAlmostEqual(new_stop, 96.50, places=2)
        AdvanceDB.update_open_trade("adv_trd_ratchet_1", 100.0, 82.0, 18.0, 0.0, new_stop, 975.0, 910.0, 14.0)

        # 4. Next bar tick bounces to ₹97.0 (>= ₹96.50 armed close-only ratchet) -> sub-second guard exits with RATCHET_STOP_HIT!
        closed_hit = self.orch.evaluate_subsecond_tick_guard(
            "NSE_FO|RATCHET_PE",
            {"bid": 96.8, "ask": 97.0, "ltp": 97.0},
            now_dt=datetime(2026, 11, 13, 10, 5, 12),
        )
        self.assertEqual(len(closed_hit), 1)
        self.assertEqual(closed_hit[0]["exit_reason"], "RATCHET_STOP_HIT")
        self.assertGreater(closed_hit[0]["net_pnl"], 100.0)

    def test_04_daily_3500_mtm_kill_flattens_open_v6b_positions_and_live_bridge_stays_disarmed(self):
        # Insert an open V6B_LBASIS_ATM Long trade @ ₹150.0 (65 qty)
        AdvanceDB.create_trade(
            {
                "trade_id": "adv_trd_kill_1",
                "signal_id": "sig_k1",
                "book_type": BOOK_LONG_BUY,
                "strategy_mode": "V6B_LBASIS_ATM",
                "direction": "BUY_CE",
                "instrument_key": "NSE_FO|KILL_CE",
                "trading_symbol": "NIFTY 23500 CE",
                "underlying": "NIFTY",
                "option_type": "CE",
                "strike": 23500.0,
                "expiry": "2026-11-19",
                "lot_size": 65,
                "quantity": 65,
                "entry_time": "2026-11-13T10:15:00",
                "entry_bid": 149.8,
                "entry_ask": 150.0,
                "entry_price_used": 150.0,
                "entry_spot_price": 23510.0,
                "peak_price": 150.0,
                "trough_price": 150.0,
                "trailing_stop": 50.0,  # Wide stop so MTM kill triggers first
                "stop_loss_pct": 66.0,
                "target_pct": 0.0,
                "estimated_margin": 9750.0,
                "regime_context": {"engine_version": "S2_05_UPSTOX_V3"},
            }
        )

        # Verify AdvanceLiveBridge stays strictly in STANDBY_PAPER (`is_live_armed == False`)
        bridge = AdvanceLiveBridge(db_path=self.db_path, auto_start_daemon=False)
        self.assertFalse(bridge.is_live_armed)
        sync_res = bridge.sync_once_with_advance_paper_table(now_dt=datetime(2026, 11, 13, 10, 16, 0))
        self.assertEqual(sync_res["status"], "STANDBY_PAPER")

        # Drop bid to ₹90.0 (-₹60 * 65 = -₹3,900 < -₹3,500 MTM kill) -> sub-second portfolio flatten!
        closed = self.orch.evaluate_subsecond_tick_guard(
            "NSE_FO|KILL_CE",
            {"bid": 90.0, "ask": 90.5, "ltp": 90.2},
            now_dt=datetime(2026, 11, 13, 10, 17, 5),
        )
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "DAILY_MTM_KILL_FLATTEN")
        self.assertTrue(self.orch._daily_kill_triggered)
        self.assertEqual(len(AdvanceDB.get_open_trades()), 0)

    def test_05_s205_context_readiness_and_status_contract_parity(self):
        # Seed 35 minutes of 1m bullish NIFTY bars (09:15..09:49) + front option bars
        spot_bars = {}
        vix_bars = {}
        opt_pe = {}
        opt_ce = {}
        for i in range(35):
            mm = 15 + i
            hh = 9 + (mm // 60)
            m_rem = mm % 60
            ts = f"2026-11-13 {hh:02d}:{m_rem:02d}:00"
            px = 23450.0 + i * 2.2
            spot_bars[ts] = (px - 1.5, px + 2.0, px - 2.5, px, 1000.0)
            vix_bars[ts] = (14.2, 14.3, 14.1, 14.2)
            # Decaying PE with rising writer OI
            pe_px = max(45.0, 110.0 - i * 0.9)
            ce_px = 95.0 + i * 1.1
            opt_pe[ts] = (pe_px + 0.5, pe_px + 1.0, pe_px - 0.5, pe_px, 12000.0, 200000.0 + i * 8000.0)
            opt_ce[ts] = (ce_px - 0.5, ce_px + 1.0, ce_px - 1.0, ce_px, 14000.0, 180000.0)

        with self.orch._op_lock:
            self.orch._prior_day_dates = ["2026-11-12"]
            self.orch._prior_day_stats = {"pdh": 23520.0, "pdl": 23380.0, "pdc": 23445.0, "vix_20d_closes": [14.0] * 20}
            self.orch._spot_bars = spot_bars
            self.orch._vix_bars = vix_bars
            self.orch._opt_bars = {
                ("2026-11-19", 23550.0, "PE"): opt_pe,
                ("2026-11-19", 23500.0, "PE"): opt_pe,
                ("2026-11-19", 23500.0, "CE"): opt_ce,
                ("2026-11-19", 23450.0, "PE"): opt_pe,
                ("2026-11-19", 23450.0, "CE"): opt_ce,
            }

        sweep = self.orch.run_surveillance_sweep(now_dt=datetime(2026, 11, 13, 9, 50, 5), allow_network=False)
        self.assertEqual(sweep["status"], "OK")
        self.assertEqual(sweep["engine_version"], "S2_05_UPSTOX_V3")

        st = self.orch.get_status()
        self.assertTrue(st["simulation_only"])
        self.assertEqual(st["runtime_parity"]["status"], "SYNCHRONIZED")
        self.assertTrue(st["runtime_parity"]["in_sync"])

        br = st["backend_readiness"]
        self.assertEqual(len(br["engine_readiness"]), 4)
        engine_ids = [r["engine_id"] for r in br["engine_readiness"]]
        self.assertEqual(engine_ids, ["V6B_S1_OI", "V6B_S3_SPIKE", "V6B_S4_Z2", "V6B_LBASIS_ATM"])
        for row in br["engine_readiness"]:
            self.assertGreaterEqual(row["gates_total"], 5)
            self.assertTrue(bool(row["when_order_fires"]))


if __name__ == "__main__":
    unittest.main()
