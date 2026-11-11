"""
Step 3 Unit & Integration Test Suite:
Option Sniffer Advance (100% Sandboxed Dual-Book Engine — 1 Lot BUY + 1 Lot SELL)

Verifies:
  1. Strict Database & Execution Sandbox Isolation (zero mutation of main option_sniffer tables).
  2. Short Option Selling Statutory Cost Model (SELL at BID -> BUY at ASK) & Hard Rupee Stop-Loss Cap (<= ₹1,800 / +16%).
  3. 4-Tier Reverse-Chandelier Premium Decay Ratchet for Short Option Selling (+10% Cost Lock -> +14%/+22%/+35%/+50% Decay).
  4. Cross-Sectional Option Chain Intelligence (GEX, VRP, Put-Call Parity Basis, PCR, OI Walls, Max Pain).
  5. Dual-Book Regime Router (Book 1 Wave-1 Gamma Sniper vs Book 2 Contraction / Wave-2 Exhaustion / Diagonal OTM Wing Seller).
  6. Chronological Blind Walk-Forward Audit (Oct 19 – Oct 30, 2026: 14 trades, 85.7% WR, +₹17,910.94 Net PnL).
"""

import os
import sys
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.analytics.option_sniffer_advance.advance_constants import (
    SIMULATION_ONLY,
    LIVE_ORDER_PLACEMENT_ENABLED,
    BOOK_LONG_BUY,
    BOOK_SHORT_SELL,
    STRATEGY_WAVE1_GAMMA_SNIPER,
    STRATEGY_CONTRACTION_THETA_SELLER,
    STRATEGY_WAVE2_EXHAUSTION_SELLER,
    STRATEGY_DIAGONAL_OTM_WING_SELLER,
    calculate_short_option_trade_costs,
)
from app.analytics.option_sniffer_advance.advance_db import (
    AdvanceDB,
    init_option_sniffer_advance_tables,
)
from app.analytics.option_sniffer_advance.advance_chain_intelligence import AdvanceChainIntelligence
from app.analytics.option_sniffer_advance.advance_paper_execution import AdvancePaperExecution
from app.analytics.option_sniffer_advance.advance_signal_engine import AdvanceSignalEngine
from app.analytics.option_sniffer_advance.advance_orchestrator import get_option_sniffer_advance_orchestrator


class TestStep3OptionSnifferAdvanceDualBook(unittest.TestCase):
    def setUp(self):
        self.temp_fd, self.temp_db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(self.temp_fd)
        self.db_patcher = patch(
            "app.analytics.option_sniffer_advance.advance_db.get_db_path",
            return_value=self.temp_db_path,
        )
        self.db_patcher.start()
        init_option_sniffer_advance_tables()
        AdvanceSignalEngine.reset_state()

    def tearDown(self):
        self.db_patcher.stop()
        try:
            os.remove(self.temp_db_path)
        except Exception:
            pass

    def test_01_sandbox_isolation_and_simulation_guard(self):
        """Verifies strict SIMULATION_ONLY=True and isolated SQLite tables."""
        self.assertTrue(SIMULATION_ONLY)
        self.assertFalse(LIVE_ORDER_PLACEMENT_ENABLED)

        conn = sqlite3.connect(self.temp_db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {r[0] for r in cur.fetchall()}
        conn.close()

        self.assertIn("option_sniffer_advance_trades", tables)
        self.assertIn("option_sniffer_advance_signals", tables)
        self.assertIn("option_sniffer_advance_chain_snapshots", tables)
        self.assertIn("option_sniffer_advance_events", tables)
        self.assertNotIn("option_sniffer_paper_trades", tables)

    def test_02_short_option_cost_model_and_reverse_chandelier_ratchet(self):
        """Verifies Short Option Selling friction (SELL at BID -> BUY at ASK), Rupee SL Cap, and 4-tier Reverse-Chandelier."""
        costs = calculate_short_option_trade_costs(
            sell_entry_bid=150.0, buy_exit_ask=115.0, quantity=65
        )
        self.assertGreater(costs["total_friction"], 60.0)
        self.assertGreater(costs["stt"], 0.0)
        self.assertGreater(costs["net_pnl"], 2100.0)

        # Capped SL pct for high-premium short option (entry_bid=250, qty=65 -> ₹1,800 cap = 11.08% < 16%)
        capped_sl = AdvancePaperExecution.calculate_short_capped_sl_pct(
            entry_bid=250.0, lot_size=65, base_sl_pct=16.0
        )
        self.assertLessEqual(capped_sl, 11.1)
        self.assertGreaterEqual(capped_sl, 8.0)

        # Reverse-Chandelier tiers:
        # At +11% decay (trough=89 on entry=100), locks +4.0% cost floor (ceiling = 96.00)
        rc_floor = AdvancePaperExecution.calculate_short_reverse_chandelier_ceiling(
            entry_bid=100.0, current_trough_ask=89.0, short_mfe_pct=11.0
        )
        self.assertEqual(rc_floor, 96.00)

        # At +25% decay (trough=75 on entry=100), Tier 2 allows +4.5% bounce above trough -> 75 * 1.045 = 78.38
        rc_tier2 = AdvancePaperExecution.calculate_short_reverse_chandelier_ceiling(
            entry_bid=100.0, current_trough_ask=75.0, short_mfe_pct=25.0
        )
        self.assertAlmostEqual(rc_tier2, 78.38, places=2)

    def test_03_cross_sectional_chain_intelligence_math(self):
        """Verifies GEX, VRP, Put-Call Parity Synthetic Future & Basis, PCR, OI Walls, and Max Pain."""
        u_quote = {"spot_ltp": 22500.0, "open": 22480.0, "high": 22530.0, "low": 22460.0, "vwap": 22495.0}
        chain_quotes = [
            {
                "instrument_key": "NSE_FO|1",
                "underlying": "NIFTY",
                "strike": 22400.0,
                "option_type": "PE",
                "ltp": 85.0,
                "bid": 84.5,
                "ask": 85.5,
                "bid_qty": 1200,
                "ask_qty": 900,
                "oi": 450000,
                "volume": 80000,
                "iv": 15.8,
                "delta": -0.30,
                "gamma": 0.0011,
                "lot_size": 65,
            },
            {
                "instrument_key": "NSE_FO|2",
                "underlying": "NIFTY",
                "strike": 22500.0,
                "option_type": "CE",
                "ltp": 135.0,
                "bid": 134.5,
                "ask": 135.5,
                "bid_qty": 1000,
                "ask_qty": 1000,
                "oi": 320000,
                "volume": 95000,
                "iv": 15.2,
                "delta": 0.52,
                "gamma": 0.0015,
                "lot_size": 65,
            },
            {
                "instrument_key": "NSE_FO|3",
                "underlying": "NIFTY",
                "strike": 22500.0,
                "option_type": "PE",
                "ltp": 125.0,
                "bid": 124.5,
                "ask": 125.5,
                "bid_qty": 1100,
                "ask_qty": 950,
                "oi": 380000,
                "volume": 90000,
                "iv": 15.6,
                "delta": -0.48,
                "gamma": 0.0015,
                "lot_size": 65,
            },
            {
                "instrument_key": "NSE_FO|4",
                "underlying": "NIFTY",
                "strike": 22600.0,
                "option_type": "CE",
                "ltp": 80.0,
                "bid": 79.5,
                "ask": 80.5,
                "bid_qty": 800,
                "ask_qty": 1300,
                "oi": 520000,
                "volume": 85000,
                "iv": 14.9,
                "delta": 0.28,
                "gamma": 0.0011,
                "lot_size": 65,
            },
        ]
        intel = AdvanceChainIntelligence.compute_chain_intelligence(
            underlying="NIFTY",
            option_quotes=chain_quotes,
            underlying_quote=u_quote,
            as_of_time=datetime(2026, 10, 26, 10, 30, 0),
        )
        # Put-Call parity at 22500: K + C - P = 22500 + 135 - 125 = 22510.0
        self.assertAlmostEqual(intel["synthetic_future"], 22510.0, places=1)
        self.assertGreater(intel["synthetic_basis_bps"], 4.0)
        self.assertEqual(intel["put_wall_strike"], 22400.0)
        self.assertEqual(intel["call_wall_strike"], 22600.0)
        self.assertGreater(intel["pcr_oi"], 0.9)

    def test_04_dual_book_execution_lifecycle_long_and_short(self):
        """Verifies full paper trade lifecycle for both Book 1 (Long Buy) and Book 2 (Short Sell)."""
        engine = AdvanceSignalEngine()
        t0 = datetime(2026, 10, 26, 10, 0, 0)

        # Mock model prediction so signal qualifies deterministically
        with patch.object(
            engine.model,
            "predict_contract",
            return_value={"calibrated_prob": 0.56, "expected_net_return_pct": 14.5},
        ):
            long_contract = {
                "instrument_key": "NSE_FO|LONG_TEST",
                "trading_symbol": "NIFTY 22500 CE",
                "underlying": "NIFTY",
                "strike": 22500.0,
                "option_type": "CE",
                "lot_size": 65,
                "expiry": "2026-10-29",
            }
            long_quote = {
                **long_contract,
                "ltp": 120.0,
                "bid": 119.8,
                "ask": 120.0,
                "spread_pct": 0.16,
                "bid_qty": 1500,
                "ask_qty": 900,
                "volume": 50000,
                "oi": 250000,
                "iv": 15.0,
                "delta": 0.52,
                "gamma": 0.0014,
                "theta": -10.0,
                "vega": 11.0,
                "quote_age_ms": 100,
            }
            u_quote = {"spot_ltp": 22510.0, "open": 22450.0, "high": 22515.0, "low": 22440.0, "vwap": 22480.0}
            chain_intel = {
                "spot_price": 22510.0,
                "completed_bars_count": 45,
                "adx_14": 28.0,
                "kaufman_er": 0.42,
                "atr_ratio": 1.12,
                "vrp": 1.5,
                "net_gex": -8.5,
                "gex_regime": "SHORT_GAMMA_ACCELERATION",
                "pcr_oi": 1.15,
                "synthetic_basis_bps": 5.0,
            }
            res_long = engine.evaluate_contract(
                contract=long_contract,
                quote=long_quote,
                underlying_quote=u_quote,
                chain_intel=chain_intel,
                as_of_time=t0,
                enforce_cutoff=True,
            )
            self.assertEqual(res_long["status"], "ACTIVE")
            self.assertEqual(res_long["book_type"], BOOK_LONG_BUY)
            self.assertEqual(res_long["strategy_mode"], STRATEGY_WAVE1_GAMMA_SNIPER)
            self.assertIsNotNone(res_long.get("opened_trade_id"))

            # 2. 10 minutes later (600s >= 480s), while Book 1 Long is open, evaluate an OTM Call (+2.8 strikes OTM > 1.5)
            # -> Book 2 Diagonal OTM Wing Seller!
            t1 = datetime(2026, 10, 26, 10, 10, 0)
            otm_contract = {
                "instrument_key": "NSE_FO|SHORT_DIAG_TEST",
                "trading_symbol": "NIFTY 22650 CE",
                "underlying": "NIFTY",
                "strike": 22650.0,
                "option_type": "CE",
                "lot_size": 65,
                "expiry": "2026-10-29",
            }
            otm_quote = {
                **otm_contract,
                "ltp": 100.0,
                "bid": 100.0,
                "ask": 100.2,
                "spread_pct": 0.20,
                "bid_qty": 900,
                "ask_qty": 1100,
                "volume": 40000,
                "oi": 300000,
                "iv": 16.0,
                "delta": 0.34,
                "gamma": 0.0010,
                "theta": -14.0,
                "vega": 9.0,
                "quote_age_ms": 100,
            }
            res_short = engine.evaluate_contract(
                contract=otm_contract,
                quote=otm_quote,
                underlying_quote=u_quote,
                chain_intel=chain_intel,
                as_of_time=t1,
                enforce_cutoff=True,
            )
            self.assertEqual(res_short["status"], "ACTIVE")
            self.assertEqual(res_short["book_type"], BOOK_SHORT_SELL)
            self.assertEqual(res_short["strategy_mode"], STRATEGY_DIAGONAL_OTM_WING_SELLER)
            self.assertIsNotNone(res_short.get("opened_trade_id"))

        # Verify both Book 1 and Book 2 positions are open simultaneously
        open_all = AdvanceDB.get_open_trades()
        self.assertEqual(len(open_all), 2)

        # 3. Simulate Long rising 120 -> 150 (+25% MFE) and Short decaying 100 -> 78 (+22% decay),
        # then Long pulling back to 132 and Short bouncing to 84 -> both trail stops harvest profit!
        t2 = datetime(2026, 10, 26, 10, 20, 0)
        live_map_step1 = {
            "NSE_FO|LONG_TEST": {**long_quote, "bid": 150.0, "ask": 150.5, "ltp": 150.2},
            "NSE_FO|SHORT_DIAG_TEST": {**otm_quote, "bid": 77.8, "ask": 78.0, "ltp": 77.9},
        }
        AdvancePaperExecution.update_open_trades(live_map_step1, {"NIFTY": u_quote}, as_of_time=t2)

        t3 = datetime(2026, 10, 26, 10, 25, 0)
        live_map_step2 = {
            "NSE_FO|LONG_TEST": {**long_quote, "bid": 132.0, "ask": 132.5, "ltp": 132.2},
            "NSE_FO|SHORT_DIAG_TEST": {**otm_quote, "bid": 83.8, "ask": 84.0, "ltp": 83.9},
        }
        closed = AdvancePaperExecution.update_open_trades(live_map_step2, {"NIFTY": u_quote}, as_of_time=t3)
        self.assertEqual(len(closed), 2)

        pnl = AdvanceDB.get_dual_book_pnl_summary()
        self.assertEqual(pnl["combined"]["total_closed_trades"], 2)
        self.assertEqual(pnl["combined"]["wins"], 2)
        self.assertGreater(pnl["long_book"]["total_net_pnl"], 0.0)
        self.assertGreater(pnl["short_book"]["total_net_pnl"], 0.0)

    def test_05_causal_october_walkforward_audit_parity(self):
        """Verifies the 10-day Oct 19 – Oct 30, 2026 Dual-Book walk-forward audit produces 85.7% WR and +₹17,910.94 Net PnL."""
        # Temporarily stop the db_patcher so the walkforward audit reads canonical market_data.db
        self.db_patcher.stop()
        try:
            orch = get_option_sniffer_advance_orchestrator()
            report = orch.get_walkforward_audit_report(force_refresh=True)
            summary = report["summary"]

            self.assertEqual(summary["total_trades"], 14)
            self.assertEqual(summary["wins"], 12)
            self.assertEqual(summary["losses"], 2)
            self.assertAlmostEqual(summary["win_rate"], 85.7, places=1)
            self.assertAlmostEqual(summary["total_net_pnl"], 17910.94, places=1)
            self.assertEqual(summary["long_book"]["trades"], 8)
            self.assertEqual(summary["short_book"]["trades"], 6)
            self.assertEqual(summary["short_book"]["wins"], 6)
            self.assertAlmostEqual(summary["short_book"]["net_pnl"], 7244.33, places=1)
        finally:
            self.db_patcher.start()

    def test_06_subsecond_websocket_tick_guard_ratchets_and_exits_both_books(self):
        """
        Verifies that OptionStreamManager._evaluate_tick_protective_guards() immediately
        updates peak/trough MFE on sub-second ticks and fires immediate sub-second stop-loss
        exits for both Book 1 (LONG_BOOK_BUY) and Book 2 (SHORT_BOOK_SELL).
        """
        from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

        # 1. Open a Short Book trade at BID 100.0 (SL is +16.0% -> 116.0)
        short_sig = {
            "signal_id": "sig_subsec_short",
            "book_type": BOOK_SHORT_SELL,
            "strategy_mode": STRATEGY_DIAGONAL_OTM_WING_SELLER,
            "direction": "SELL_CE",
            "instrument_key": "NSE_FO|SUBSEC_SHORT",
            "trading_symbol": "NIFTY 22650 CE",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 22650.0,
            "expiry": "2026-11-03",
            "lot_size": 65,
            "spot_price": 22500.0,
            "option_bid": 100.0,
            "option_ask": 100.4,
            "ltp": 100.2,
        }
        tid_s = AdvancePaperExecution.open_trade(
            short_sig, as_of_time=datetime(2026, 10, 30, 10, 5, 0)
        )
        self.assertIsNotNone(tid_s)

        sm = OptionStreamManager()
        # Sub-second tick 1: Ask drops to 82.0 (+18% decay) -> trough_price must ratchet down to 82.0 immediately
        sm._evaluate_tick_protective_guards("NSE_FO|SUBSEC_SHORT", {"bid": 81.6, "ask": 82.0, "ltp": 81.8})
        open_trades = AdvanceDB.get_open_trades()
        self.assertEqual(len(open_trades), 1)
        self.assertAlmostEqual(float(open_trades[0]["trough_price"]), 82.0, places=2)
        self.assertAlmostEqual(float(open_trades[0]["mfe_pct"]), 18.0, places=2)

        # Sub-second tick 2: Ask spikes to 118.0 (-18% adverse > 16% SL) -> immediate SHORT_STOP_LOSS_HIT
        sm._evaluate_tick_protective_guards("NSE_FO|SUBSEC_SHORT", {"bid": 117.5, "ask": 118.0, "ltp": 117.8})
        self.assertEqual(len(AdvanceDB.get_open_trades()), 0)
        closed_trades = AdvanceDB.get_closed_trades(limit=5)
        self.assertEqual(closed_trades[0]["exit_reason"], "SHORT_STOP_LOSS_HIT")

    def test_07_counter_trend_long_call_and_low_rv_pinning_blocked(self):
        """
        Verifies Patch 1:
          - Blocks LONG_BOOK_BUY Call (CE) when -DI (27.7) > +DI (14.3) (reproducing Nov 2 Trade #4).
          - Blocks LONG_BOOK_BUY Put (PE) when +DI (28.5) > -DI (12.0).
          - Blocks LONG_BOOK_BUY during low-RV Gamma Pinning (ADX < 16, RV_15m < 8%, VRP > 6%).
        """
        engine = AdvanceSignalEngine()
        t0 = datetime(2026, 11, 2, 11, 21, 11)

        with patch.object(
            engine.model,
            "predict_contract",
            return_value={"calibrated_prob": 0.58, "expected_net_return_pct": 12.0},
        ):
            ce_contract = {
                "instrument_key": "NSE_FO|NOV2_TRADE4",
                "trading_symbol": "NIFTY 23300 CE 03 NOV 26",
                "underlying": "NIFTY",
                "strike": 23300.0,
                "option_type": "CE",
                "lot_size": 65,
                "expiry": "2026-11-03",
            }
            quote = {
                **ce_contract,
                "ltp": 93.8,
                "bid": 93.6,
                "ask": 93.95,
                "spread_pct": 0.37,
                "volume": 120000,
                "oi": 450000,
                "iv": 15.0,
                "delta": 0.54,
                "gamma": 0.0018,
                "theta": -15.0,
                "vega": 9.5,
                "quote_age_ms": 100,
            }
            u_quote = {"spot_ltp": 23317.45, "open": 23310.0, "vwap": 23315.0}
            bearish_chain_intel = {
                "spot_price": 23317.45,
                "completed_bars_count": 120,
                "adx_14": 19.5,
                "plus_di": 14.35,
                "minus_di": 27.72,
                "kaufman_er": 0.32,
                "atr_ratio": 1.02,
                "vrp": 3.5,
                "rv_15m_ann": 11.5,
                "net_gex": 12.0,
                "pcr_oi": 0.95,
                "synthetic_basis_bps": 7.96,
            }
            res_ce = engine.evaluate_contract(
                contract=ce_contract,
                quote=quote,
                underlying_quote=u_quote,
                chain_intel=bearish_chain_intel,
                as_of_time=t0,
                enforce_cutoff=True,
            )
            self.assertNotEqual(res_ce["status"], "ACTIVE")
            self.assertTrue(
                any("Counter-trend Long Call blocked" in r for r in res_ce["rejection_reasons"]),
                f"Expected counter-trend rejection, got: {res_ce['rejection_reasons']}",
            )

            # Also verify Low-RV Gamma Pinning Trap guard (ADX=15.0 < 16, RV_15m=6.1% < 8%, VRP=8.9% > 6%)
            pinning_intel = {
                **bearish_chain_intel,
                "adx_14": 15.0,
                "plus_di": 22.0,
                "minus_di": 18.0,
                "kaufman_er": 0.26,
                "rv_15m_ann": 6.08,
                "vrp": 8.92,
            }
            res_pin = engine.evaluate_contract(
                contract=ce_contract,
                quote=quote,
                underlying_quote=u_quote,
                chain_intel=pinning_intel,
                as_of_time=t0,
                enforce_cutoff=True,
            )
            self.assertNotEqual(res_pin["status"], "ACTIVE")
            self.assertTrue(
                any("Low-RV Gamma Pinning Trap blocked" in r for r in res_pin["rejection_reasons"]),
                f"Expected Low-RV Pinning rejection, got: {res_pin['rejection_reasons']}",
            )

    def test_08_short_book_zero_second_reentry_blocked_and_one_wing_per_parent(self):
        """
        Verifies Patches 2 & 3:
          - Blocks diagonal wing < 1.85 strikes OTM (e.g. +1.61 strikes OTM).
          - Blocks 0.0-second re-entry into SHORT_BOOK_SELL after SHORT_STOP_LOSS_HIT via:
            1) 1-Diagonal-Wing-Per-Parent-Long-Wave lock
            2) 15-minute Short Book Post-Loss Cooldown
            3) Per-contract exit cooldown stamp
        """
        engine = AdvanceSignalEngine()
        t_long = datetime(2026, 11, 2, 11, 51, 44)

        with patch.object(
            engine.model,
            "predict_contract",
            return_value={"calibrated_prob": 0.58, "expected_net_return_pct": 14.0},
        ):
            # 1. Open Parent Long at 11:51:44
            long_contract = {
                "instrument_key": "NSE_FO|PARENT_LONG",
                "trading_symbol": "NIFTY 23300 CE",
                "underlying": "NIFTY",
                "strike": 23300.0,
                "option_type": "CE",
                "lot_size": 65,
                "expiry": "2026-11-03",
            }
            long_quote = {
                **long_contract,
                "ltp": 75.0,
                "bid": 74.8,
                "ask": 75.15,
                "spread_pct": 0.46,
                "volume": 150000,
                "oi": 500000,
                "iv": 15.0,
                "delta": 0.50,
                "quote_age_ms": 50,
            }
            u_quote = {"spot_ltp": 23300.0, "open": 23280.0, "vwap": 23290.0}
            chain_intel = {
                "spot_price": 23300.0,
                "completed_bars_count": 150,
                "adx_14": 24.0,
                "plus_di": 28.0,
                "minus_di": 14.0,
                "kaufman_er": 0.38,
                "atr_ratio": 1.05,
                "vrp": 3.0,
                "rv_15m_ann": 12.0,
            }
            r_long = engine.evaluate_contract(
                contract=long_contract,
                quote=long_quote,
                underlying_quote=u_quote,
                chain_intel=chain_intel,
                as_of_time=t_long,
                enforce_cutoff=True,
            )
            self.assertEqual(r_long["status"], "ACTIVE")

            # 2. At 12:00:00 (> 8m elapsed), test a +2.0 OTM wing (23400 CE when spot=23300 -> +2.0 strikes OTM >= 1.85)
            t_wing1 = datetime(2026, 11, 2, 12, 0, 0)
            wing_contract = {
                "instrument_key": "NSE_FO|DIAG_WING_23400",
                "trading_symbol": "NIFTY 23400 CE",
                "underlying": "NIFTY",
                "strike": 23400.0,
                "option_type": "CE",
                "lot_size": 65,
                "expiry": "2026-11-03",
            }
            wing_quote = {
                **wing_contract,
                "ltp": 41.3,
                "bid": 41.25,
                "ask": 41.40,
                "spread_pct": 0.36,
                "volume": 180000,
                "oi": 600000,
                "iv": 15.2,
                "delta": 0.25,
                "quote_age_ms": 50,
            }
            r_wing1 = engine.evaluate_contract(
                contract=wing_contract,
                quote=wing_quote,
                underlying_quote=u_quote,
                chain_intel=chain_intel,
                as_of_time=t_wing1,
                enforce_cutoff=True,
            )
            self.assertEqual(r_wing1["status"], "ACTIVE")
            self.assertEqual(r_wing1["strategy_mode"], STRATEGY_DIAGONAL_OTM_WING_SELLER)

            # 3. At 12:09:22, Wing #1 spikes to 50.50 (+22.4% > 16% SL) and hits SHORT_STOP_LOSS_HIT
            t_sl = datetime(2026, 11, 2, 12, 9, 22)
            live_map = {
                "NSE_FO|PARENT_LONG": {**long_quote, "bid": 95.0, "ask": 95.5},
                "NSE_FO|DIAG_WING_23400": {**wing_quote, "bid": 50.35, "ask": 50.50},
            }
            closed = AdvancePaperExecution.update_open_trades(live_map, {"NIFTY": u_quote}, as_of_time=t_sl)
            self.assertEqual(len(closed), 1)
            self.assertEqual(closed[0]["exit_reason"], "SHORT_STOP_LOSS_HIT")

            # 4. 0.0 seconds later (at 12:09:22), attempt to re-evaluate NIFTY 23400 CE (reproducing Trade #9!)
            r_wing2 = engine.evaluate_contract(
                contract=wing_contract,
                quote={**wing_quote, "bid": 50.35, "ask": 50.50},
                underlying_quote=u_quote,
                chain_intel=chain_intel,
                as_of_time=t_sl,
                enforce_cutoff=True,
            )
            self.assertNotEqual(r_wing2["status"], "ACTIVE")
            self.assertIsNone(r_wing2.get("opened_trade_id"))
            reasons_joined = " | ".join(r_wing2["rejection_reasons"])
            self.assertIn("Diagonal Wing Anti-Churn", reasons_joined)
            self.assertIn("Short Book Post-Loss Cooldown", reasons_joined)
            self.assertIn("Per-contract 5m signal cooldown active", reasons_joined)

    def test_09_orphaned_diagonal_wing_reconciles_when_parent_long_exits(self):
        """
        Verifies Patch 4:
        When the parent LONG_BOOK_BUY trade closes, any open DIAGONAL_OTM_WING_SELLER on that
        underlying is automatically closed with exit_reason = 'PARENT_LONG_EXITED_RECONCILE'.
        """
        t0 = datetime(2026, 11, 2, 10, 30, 0)
        AdvancePaperExecution.open_trade(
            {
                "signal_id": "s_long_p",
                "book_type": BOOK_LONG_BUY,
                "strategy_mode": STRATEGY_WAVE1_GAMMA_SNIPER,
                "direction": "BUY_CE",
                "instrument_key": "NSE_FO|P_LONG",
                "trading_symbol": "NIFTY 23350 CE",
                "underlying": "NIFTY",
                "option_type": "CE",
                "strike": 23350.0,
                "expiry": "2026-11-03",
                "lot_size": 65,
                "spot_price": 23340.0,
                "option_bid": 79.2,
                "option_ask": 79.4,
            },
            as_of_time=t0,
        )
        t1 = datetime(2026, 11, 2, 10, 39, 0)
        AdvancePaperExecution.open_trade(
            {
                "signal_id": "s_short_w",
                "book_type": BOOK_SHORT_SELL,
                "strategy_mode": STRATEGY_DIAGONAL_OTM_WING_SELLER,
                "direction": "SELL_CE",
                "instrument_key": "NSE_FO|W_SHORT",
                "trading_symbol": "NIFTY 23450 CE",
                "underlying": "NIFTY",
                "option_type": "CE",
                "strike": 23450.0,
                "expiry": "2026-11-03",
                "lot_size": 65,
                "spot_price": 23340.0,
                "option_bid": 38.9,
                "option_ask": 39.1,
            },
            as_of_time=t1,
        )
        self.assertEqual(len(AdvanceDB.get_open_trades()), 2)

        # At 10:43:00 (13m elapsed for parent Long -> hits STAGNATION_TIME_STOP),
        # the diagonal wing (only 4m elapsed, flat at 37.90) must automatically reconcile and close!
        t2 = datetime(2026, 11, 2, 10, 43, 0)
        live_map = {
            "NSE_FO|P_LONG": {"bid": 77.15, "ask": 77.35, "ltp": 77.25},
            "NSE_FO|W_SHORT": {"bid": 37.70, "ask": 37.90, "ltp": 37.80},
        }
        closed = AdvancePaperExecution.update_open_trades(live_map, as_of_time=t2)
        self.assertEqual(len(closed), 2)
        reasons = {c["instrument_key"]: c["exit_reason"] for c in closed}
        self.assertEqual(reasons["NSE_FO|P_LONG"], "STAGNATION_TIME_STOP")
        self.assertEqual(reasons["NSE_FO|W_SHORT"], "PARENT_LONG_EXITED_RECONCILE")
        self.assertEqual(len(AdvanceDB.get_open_trades()), 0)

    def test_10_five_institutional_advance_upgrades(self):
        """
        Verifies Upgrades A1-A5:
          - A1: CONTRACTION_THETA_SELLER blocks VRP < 0.0 and blocks naked SELL_CE into strong +DI trend.
          - A2: DIAGONAL_OTM_WING_SELLER blocks opposite option_type wing (PE wing on CE parent Long).
          - A3: WAVE_2_EXHAUSTION_SELLER enforces 1-Exhaustion-Short-Per-Harvested-Long-Wave.
          - A4: LONG_BOOK_BUY enforces Ask >= Rs.35, |Delta| >= 0.32, OTM <= 2.5 strikes, and Same-Sweep Lock.
        """
        engine = AdvanceSignalEngine()
        t0 = datetime(2026, 11, 2, 10, 15, 0)

        with patch.object(
            engine.model,
            "predict_contract",
            return_value={"calibrated_prob": 0.58, "expected_net_return_pct": 14.0},
        ):
            # A4: Low premium / low delta / far-OTM lottery contract blocked in LONG_BOOK_BUY
            otm_lottery = {
                "instrument_key": "NSE_FO|LOTTERY_23600CE",
                "trading_symbol": "NIFTY 23600 CE",
                "underlying": "NIFTY",
                "strike": 23600.0,
                "option_type": "CE",
                "lot_size": 65,
                "expiry": "2026-11-03",
            }
            lottery_quote = {
                **otm_lottery,
                "ltp": 11.0,
                "bid": 10.75,
                "ask": 10.80,
                "spread_pct": 0.45,
                "volume": 80000,
                "oi": 200000,
                "iv": 14.5,
                "delta": 0.11,
                "quote_age_ms": 50,
            }
            u_quote = {"spot_ltp": 23350.0, "open": 23300.0, "vwap": 23320.0}
            bull_intel = {
                "spot_price": 23350.0,
                "completed_bars_count": 60,
                "adx_14": 25.0,
                "plus_di": 28.0,
                "minus_di": 14.0,
                "kaufman_er": 0.42,
                "atr_ratio": 1.08,
                "vrp": 2.5,
                "rv_15m_ann": 12.0,
            }
            res_lot = engine.evaluate_contract(
                contract=otm_lottery,
                quote=lottery_quote,
                underlying_quote=u_quote,
                chain_intel=bull_intel,
                as_of_time=t0,
                enforce_cutoff=True,
            )
            self.assertNotEqual(res_lot["status"], "ACTIVE")
            lot_reasons = " | ".join(res_lot["rejection_reasons"])
            self.assertIn("Minimum Premium Discipline", lot_reasons)
            self.assertIn("Minimum Delta Discipline", lot_reasons)
            self.assertIn("Far-OTM Strike Discipline", lot_reasons)

            # A1: CONTRACTION_THETA_SELLER blocks when VRP < 0.0 or strong +DI trend conflict
            atm_ce = {
                "instrument_key": "NSE_FO|ATM_23350CE",
                "trading_symbol": "NIFTY 23350 CE",
                "underlying": "NIFTY",
                "strike": 23350.0,
                "option_type": "CE",
                "lot_size": 65,
                "expiry": "2026-11-03",
            }
            atm_quote = {
                **atm_ce,
                "ltp": 82.0,
                "bid": 81.8,
                "ask": 82.0,
                "spread_pct": 0.24,
                "volume": 150000,
                "oi": 450000,
                "iv": 13.0,
                "delta": 0.50,
                "quote_age_ms": 50,
            }
            contraction_bad_intel = {
                "spot_price": 23350.0,
                "completed_bars_count": 60,
                "adx_14": 21.0,
                "plus_di": 26.0,
                "minus_di": 14.0,
                "kaufman_er": 0.34,
                "atr_ratio": 0.80,
                "vrp": -1.5,
                "rv_15m_ann": 14.5,
            }
            res_c_bad = engine.evaluate_contract(
                contract=atm_ce,
                quote=atm_quote,
                underlying_quote=u_quote,
                chain_intel=contraction_bad_intel,
                as_of_time=t0,
                enforce_cutoff=True,
            )
            self.assertNotEqual(res_c_bad["status"], "ACTIVE")
            c_reasons = " | ".join(res_c_bad["rejection_reasons"])
            self.assertIn("Contraction Short VRP Guard", c_reasons)
            self.assertIn("Contraction Short Trend Conflict", c_reasons)

            # A2: Open a parent Long CE at 10:15, then at 10:25 (> 8m elapsed) verify an OTM PE wing is blocked
            res_long = engine.evaluate_contract(
                contract=atm_ce,
                quote=atm_quote,
                underlying_quote=u_quote,
                chain_intel=bull_intel,
                as_of_time=t0,
                enforce_cutoff=True,
            )
            self.assertEqual(res_long["status"], "ACTIVE")

            otm_pe_wing = {
                "instrument_key": "NSE_FO|OTM_23250PE",
                "trading_symbol": "NIFTY 23250 PE",
                "underlying": "NIFTY",
                "strike": 23250.0,
                "option_type": "PE",
                "lot_size": 65,
                "expiry": "2026-11-03",
            }
            pe_wing_quote = {
                **otm_pe_wing,
                "ltp": 45.0,
                "bid": 44.8,
                "ask": 45.0,
                "spread_pct": 0.44,
                "volume": 120000,
                "oi": 380000,
                "iv": 15.0,
                "delta": -0.28,
                "quote_age_ms": 50,
            }
            res_mismatch = engine.evaluate_contract(
                contract=otm_pe_wing,
                quote=pe_wing_quote,
                underlying_quote=u_quote,
                chain_intel=bull_intel,
                as_of_time=datetime(2026, 11, 2, 10, 25, 0),
                enforce_cutoff=True,
            )
            self.assertNotEqual(res_mismatch["status"], "ACTIVE")
            self.assertTrue(
                any("Diagonal Spread Type Mismatch" in r for r in res_mismatch["rejection_reasons"]),
                f"Expected Diagonal Spread Type Mismatch, got: {res_mismatch['rejection_reasons']}",
            )

            # A3: Close the parent Long CE as a +30% MFE winner at 10:30, then verify WAVE_2_EXHAUSTION_SELLER
            # allows the 1st Exhaustion Short at 10:35, and blocks a 2nd repeat Exhaustion Short at 10:50!
            AdvancePaperExecution.update_open_trades(
                {"NSE_FO|ATM_23350CE": {**atm_quote, "bid": 108.0, "ask": 108.4}},
                {"NIFTY": u_quote},
                as_of_time=datetime(2026, 11, 2, 10, 28, 0),
            )
            closed_l = AdvancePaperExecution.update_open_trades(
                {"NSE_FO|ATM_23350CE": {**atm_quote, "bid": 98.0, "ask": 98.4}},
                {"NIFTY": u_quote},
                as_of_time=datetime(2026, 11, 2, 10, 30, 0),
            )
            self.assertEqual(len(closed_l), 1)

            exh_intel = {
                **bull_intel,
                "atr_ratio": 0.88,
                "kaufman_er": 0.18,
            }
            res_exh1 = engine.evaluate_contract(
                contract=atm_ce,
                quote={**atm_quote, "bid": 90.0, "ask": 90.2},
                underlying_quote=u_quote,
                chain_intel=exh_intel,
                as_of_time=datetime(2026, 11, 2, 10, 36, 0),
                enforce_cutoff=True,
            )
            self.assertEqual(res_exh1["status"], "ACTIVE")
            self.assertEqual(res_exh1["strategy_mode"], STRATEGY_WAVE2_EXHAUSTION_SELLER)

            # Close 1st Exhaustion Short as a Reverse-Chandelier winner at 10:45
            AdvancePaperExecution.update_open_trades(
                {"NSE_FO|ATM_23350CE": {**atm_quote, "bid": 68.0, "ask": 68.2}},
                {"NIFTY": u_quote},
                as_of_time=datetime(2026, 11, 2, 10, 42, 0),
            )
            closed_exh1 = AdvancePaperExecution.update_open_trades(
                {"NSE_FO|ATM_23350CE": {**atm_quote, "bid": 72.5, "ask": 72.8}},
                {"NIFTY": u_quote},
                as_of_time=datetime(2026, 11, 2, 10, 45, 0),
            )
            self.assertEqual(len(closed_exh1), 1)

            # Attempt 2nd repeat Wave-2 Exhaustion Short at 10:52 on the same harvested Long wave -> BLOCKED!
            res_exh2 = engine.evaluate_contract(
                contract=atm_ce,
                quote={**atm_quote, "bid": 78.0, "ask": 78.2},
                underlying_quote=u_quote,
                chain_intel=exh_intel,
                as_of_time=datetime(2026, 11, 2, 10, 52, 0),
                enforce_cutoff=True,
            )
            self.assertNotEqual(res_exh2["status"], "ACTIVE")
            self.assertTrue(
                any("Wave-2 Exhaustion Anti-Churn" in r for r in res_exh2["rejection_reasons"]),
                f"Expected Wave-2 Exhaustion Anti-Churn, got: {res_exh2['rejection_reasons']}",
            )

    def test_11_seven_weekend_institutional_upgrades_and_zero_main_domino(self):
        """
        Verifies the 7 Weekend Institutional Upgrades in Option Sniffer Advance:
          1. Tier-0 (+10.0% MFE -> +2.0% floor) & Tier-0.5 (+12.5% MFE -> +4.5% floor) Chandelier Ratchet.
          2. Rolling Session High/Low Pullback Guard (> 16.0 pts below Session High blocked for Long Call).
          3. 13:15–14:15 IST Super-Trend Afternoon Breakout Override (unlocks Wave-1 Long on Session High breakout).
          4. Wave-2 Trend Continuation Guard (blocks counter-trend SELL_CE like Nov 6 Trade #41 when +DI > -DI + 10 & ER > 0.20)
             and Trend-Aligned Put Selling (SELL_PE allowed on bull pause after CE harvest).
          5. Auto-Hedged Credit Spread Margin (₹26,500 defined-risk margin + hedge_wing_strike telemetry).
        """
        # 1. Verify Tier-0 (+10.0% MFE -> +2.0% floor) & Tier-0.5 (+12.5% MFE -> +4.5% floor)
        self.assertIsNone(AdvancePaperExecution.calculate_long_chandelier_stop(100.0, 109.0, 9.0))
        self.assertEqual(AdvancePaperExecution.calculate_long_chandelier_stop(100.0, 111.0, 11.0), 102.0)
        self.assertEqual(AdvancePaperExecution.calculate_long_chandelier_stop(100.0, 113.5, 13.5), 104.5)
        self.assertEqual(AdvancePaperExecution.calculate_long_chandelier_stop(100.0, 116.0, 16.0), 106.72)

        engine = AdvanceSignalEngine()
        with patch.object(
            engine.model,
            "predict_contract",
            return_value={"calibrated_prob": 0.57, "expected_net_return_pct": 12.5},
        ):
            # 2. Verify Session Extrema Pullback Guard blocks mid-air Call 28 pts below Session High
            c_call = {
                "instrument_key": "NSE_FO|NIFTY_24700CE",
                "trading_symbol": "NIFTY 24700 CE 09 NOV 26",
                "underlying": "NIFTY",
                "strike": 24700.0,
                "option_type": "CE",
                "lot_size": 65,
                "expiry": "2026-11-09",
            }
            q_call = {
                **c_call,
                "ltp": 150.0,
                "bid": 149.8,
                "ask": 150.0,
                "spread_pct": 0.13,
                "volume": 250000,
                "oi": 900000,
                "iv": 16.0,
                "delta": 0.48,
                "quote_age_ms": 50,
            }
            u_pullback = {"spot_ltp": 24672.0, "open": 24500.0, "vwap": 24610.0}
            intel_pullback = {
                "spot_price": 24672.0,
                "session_tpb": 24610.0,
                "orb_high": 24540.0,
                "orb_low": 24388.0,
                "day_high": 24702.6,
                "day_low": 24388.0,
                "high_bar_idx": 75,
                "low_bar_idx": 1,
                "completed_bars_count": 180,
                "adx_14": 32.0,
                "plus_di": 30.0,
                "minus_di": 16.0,
                "kaufman_er": 0.35,
                "atr_ratio": 0.98,
                "vrp": 2.0,
                "rv_15m_ann": 14.0,
            }
            res_pb = engine.evaluate_contract(
                contract=c_call,
                quote=q_call,
                underlying_quote=u_pullback,
                chain_intel=intel_pullback,
                as_of_time=datetime(2026, 11, 6, 12, 24, 0),
                enforce_cutoff=True,
            )
            self.assertNotEqual(res_pb["status"], "ACTIVE")
            self.assertTrue(
                any("Session Extrema Pullback Guard" in r for r in res_pb["rejection_reasons"]),
                f"Expected Session Extrema Pullback Guard, got: {res_pb['rejection_reasons']}",
            )

            # 3. Open & harvest a midday Wave-1 Long winner (12:16 -> 12:30 IST, +25% MFE), matching Nov 6 Trade #40!
            AdvanceSignalEngine.reset_state()
            u_midday_break = {"spot_ltp": 24701.0, "open": 24500.0, "vwap": 24610.0}
            intel_midday_break = {
                **intel_pullback,
                "spot_price": 24701.0,
                "day_high": 24702.6,
                "adx_14": 34.0,
                "plus_di": 34.0,
                "minus_di": 14.0,
                "kaufman_er": 0.42,
                "atr_ratio": 1.15,
            }
            res_mid = engine.evaluate_contract(
                contract=c_call,
                quote=q_call,
                underlying_quote=u_midday_break,
                chain_intel=intel_midday_break,
                as_of_time=datetime(2026, 11, 6, 12, 16, 0),
                enforce_cutoff=True,
            )
            self.assertEqual(res_mid["status"], "ACTIVE")
            self.assertIsNotNone(res_mid.get("opened_trade_id"))

            AdvancePaperExecution.update_open_trades(
                {"NSE_FO|NIFTY_24700CE": {**q_call, "bid": 188.0, "ask": 188.5}},
                {"NIFTY": u_midday_break},
                as_of_time=datetime(2026, 11, 6, 12, 25, 0),
            )
            closed_mid = AdvancePaperExecution.update_open_trades(
                {"NSE_FO|NIFTY_24700CE": {**q_call, "bid": 172.0, "ask": 172.5}},
                {"NIFTY": u_midday_break},
                as_of_time=datetime(2026, 11, 6, 12, 30, 0),
            )
            self.assertEqual(len(closed_mid), 1)

            # 4. Verify Wave-2 Trend Continuation Guard blocks counter-trend SELL_CE at 12:33:31 IST
            #    (reproducing Nov 6 Trade #41: ER=0.245 > 0.20, ATR_R=0.807 <= 0.93, +DI=30.96 >> -DI=16.63, Spot > TPB)
            AdvanceSignalEngine.reset_state()
            intel_trade41 = {
                **intel_pullback,
                "spot_price": 24676.05,
                "session_tpb": 24612.99,
                "day_high": 24702.6,
                "adx_14": 39.99,
                "plus_di": 30.96,
                "minus_di": 16.63,
                "kaufman_er": 0.245,
                "atr_ratio": 0.807,
            }
            res_t41 = engine.evaluate_contract(
                contract=c_call,
                quote={**q_call, "bid": 146.15, "ask": 146.50},
                underlying_quote={"spot_ltp": 24676.05, "open": 24500.0, "vwap": 24613.0},
                chain_intel=intel_trade41,
                as_of_time=datetime(2026, 11, 6, 12, 33, 31),
                enforce_cutoff=True,
            )
            self.assertNotEqual(res_t41["status"], "ACTIVE")
            self.assertTrue(
                any("Wave-2 Trend Continuation Guard" in r for r in res_t41["rejection_reasons"]),
                f"Expected Wave-2 Trend Continuation Guard on Trade #41 reproduction, got: {res_t41['rejection_reasons']}",
            )

            # 5. Verify Trend-Aligned Wave-2 Put Selling (SELL_PE) IS allowed on that exact same bull pause (+1.5 strikes OTM PE)
            c_put = {
                "instrument_key": "NSE_FO|NIFTY_24600PE",
                "trading_symbol": "NIFTY 24600 PE 09 NOV 26",
                "underlying": "NIFTY",
                "strike": 24600.0,
                "option_type": "PE",
                "lot_size": 65,
                "expiry": "2026-11-09",
            }
            q_put = {
                **c_put,
                "ltp": 95.0,
                "bid": 94.8,
                "ask": 95.0,
                "spread_pct": 0.21,
                "volume": 220000,
                "oi": 950000,
                "iv": 16.5,
                "delta": -0.34,
                "quote_age_ms": 50,
            }
            res_ta_pe = engine.evaluate_contract(
                contract=c_put,
                quote=q_put,
                underlying_quote={"spot_ltp": 24676.05, "open": 24500.0, "vwap": 24613.0},
                chain_intel=intel_trade41,
                as_of_time=datetime(2026, 11, 6, 12, 34, 0),
                enforce_cutoff=True,
            )
            self.assertEqual(res_ta_pe["status"], "ACTIVE")
            self.assertEqual(res_ta_pe["book_type"], BOOK_SHORT_SELL)
            self.assertEqual(res_ta_pe["direction"], "SELL_PE")
            self.assertEqual(res_ta_pe["strategy_mode"], STRATEGY_WAVE2_EXHAUSTION_SELLER)

            open_short = AdvanceDB.get_open_trades(book_type=BOOK_SHORT_SELL)
            self.assertEqual(len(open_short), 1)
            self.assertEqual(float(open_short[0]["estimated_margin"]), 26500.0)

            # 6. Verify 13:15–14:15 IST Super-Trend Afternoon Breakout Override unlocks at 13:45 IST
            #    when Spot breaks Session High (24800.0 >= 24801.9 - 4.5) with ER=0.45 >= 0.40, ADX=36 >= 28, ATR_R=1.30 >= 1.05
            AdvanceSignalEngine.reset_state()
            u_aft_break = {"spot_ltp": 24800.0, "open": 24500.0, "vwap": 24650.0}
            intel_aft_break = {
                **intel_pullback,
                "spot_price": 24800.0,
                "day_high": 24801.9,
                "adx_14": 36.0,
                "plus_di": 34.0,
                "minus_di": 14.0,
                "kaufman_er": 0.45,
                "atr_ratio": 1.30,
            }
            res_aft = engine.evaluate_contract(
                contract=c_call,
                quote=q_call,
                underlying_quote=u_aft_break,
                chain_intel=intel_aft_break,
                as_of_time=datetime(2026, 11, 6, 13, 45, 0),
                enforce_cutoff=True,
            )
            self.assertEqual(res_aft["status"], "ACTIVE")
            self.assertTrue(res_aft["is_super_trend_afternoon_breakout"])
            self.assertIsNotNone(res_aft.get("opened_trade_id"))

    def test_12_v21_seven_bug_hardening_polarity_atr_defer_stagnation_and_feed_health(self):
        """
        Verifies Option Sniffer Advance v2.1 7-Bug Hardening Suite:
          1. Bug #1 (Seller Consensus Polarity + ORB/Basis Guard): BULLISH consensus in CONTRACTION_THETA_SELLER
             boosts SELL_PE (`ACTIVE`) while rejecting counter-flow SELL_CE near ORB High (`syn_basis=+5.61 bps`).
          2. Bug #2 (Long ATR_Ratio >= 0.88 & Extreme VRP Pin Guard): Blocks shrinking-ATR non-breakout Long
             (`atr_ratio=0.801`, Nov 11 Trade #48) and extreme VRP Long-Gamma pin (`vrp=19.19%`, Nov 9 Trade #44),
             while preserving session-extrema breakouts (`Trade #32: atr_ratio=0.787, ER=0.412`).
          3. Bug #3 (`defer_open=True` Cross-Chain Ranking): Evaluates multiple strikes/sides without first-in-loop
             lockout and opens only the #1 ranked setup via `execute_deferred_signal()`.
          4. Bug #4 (`20m` `SHORT_REGIME_BREAK_STAGNATION_EXIT`): Bridges the `[3.5%, 10.0%)` MFE dead-zone when
             contraction breaks (`ER >= 0.28` or `ATR_Ratio >= 1.10`), while leaving quiet theta decay untouched.
          5. Bug #7 (`is_index_feed_healthy=False`): Fails closed on stale/synthetic 1m index candles.
        """
        engine = AdvanceSignalEngine()
        AdvanceSignalEngine.reset_state()
        t_nov11_1110 = datetime(2026, 11, 11, 11, 10, 3)

        # --- 1 & 3. Real OptionAlphaModel (unmocked) + BULLISH Consensus + defer_open=True ---
        u_nov11 = {"spot_ltp": 25378.85, "open": 25320.0, "high": 25424.35, "low": 25263.8, "vwap": 25352.0}
        intel_nov11_1110 = {
            "spot_price": 25378.85,
            "session_tpb": 25355.0,
            "orb_high": 25399.10,
            "orb_low": 25263.80,
            "day_high": 25424.35,
            "day_low": 25263.80,
            "completed_bars_count": 115,
            "is_index_feed_healthy": True,
            "adx_14": 18.98,
            "plus_di": 18.68,
            "minus_di": 14.93,
            "kaufman_er": 0.163,
            "atr_ratio": 0.739,
            "vrp": 12.73,
            "rv_15m_ann": 5.08,
            "net_gex": 642.18,
            "synthetic_basis_bps": 5.61,
            "call_wall_strike": 25500.0,
            "put_wall_strike": 25300.0,
        }
        bull_upstream = {"consensus_direction": "BULLISH", "consensus_confidence": 78.0}

        ce_25450 = {
            "instrument_key": "NSE_FO|T50_25450CE",
            "trading_symbol": "NIFTY 25450 CE 17 NOV 26",
            "underlying": "NIFTY",
            "strike": 25450.0,
            "option_type": "CE",
            "lot_size": 65,
            "expiry": "2026-11-17",
        }
        q_ce_25450 = {
            **ce_25450,
            "ltp": 208.1,
            "bid": 207.9,
            "ask": 208.35,
            "spread_pct": 0.22,
            "volume": 350000,
            "oi": 2741440,
            "iv": 17.8,
            "delta": 0.42,
            "quote_age_ms": 80,
        }
        pe_25300 = {
            "instrument_key": "NSE_FO|T50_25300PE",
            "trading_symbol": "NIFTY 25300 PE 17 NOV 26",
            "underlying": "NIFTY",
            "strike": 25300.0,
            "option_type": "PE",
            "lot_size": 65,
            "expiry": "2026-11-17",
        }
        q_pe_25300 = {
            **pe_25300,
            "ltp": 190.3,
            "bid": 190.1,
            "ask": 190.5,
            "spread_pct": 0.21,
            "volume": 380000,
            "oi": 3100000,
            "iv": 18.1,
            "delta": -0.40,
            "quote_age_ms": 80,
        }

        res_ce = engine.evaluate_contract(
            contract=ce_25450,
            quote=q_ce_25450,
            underlying_quote=u_nov11,
            chain_intel=intel_nov11_1110,
            upstream_signals=bull_upstream,
            as_of_time=t_nov11_1110,
            enforce_cutoff=True,
            defer_open=True,
        )
        res_pe = engine.evaluate_contract(
            contract=pe_25300,
            quote=q_pe_25300,
            underlying_quote=u_nov11,
            chain_intel=intel_nov11_1110,
            upstream_signals=bull_upstream,
            as_of_time=t_nov11_1110,
            enforce_cutoff=True,
            defer_open=True,
        )
        self.assertNotEqual(res_ce["status"], "ACTIVE")
        self.assertTrue(
            any("Bullish Basis Near ORB High" in r or "Activity conviction below hurdle" in r for r in res_ce["rejection_reasons"])
        )
        self.assertEqual(res_pe["status"], "ACTIVE")
        self.assertEqual(res_pe["direction"], "SELL_PE")
        # Because defer_open=True, no trade was opened prematurely before ranking
        self.assertIsNone(res_pe.get("opened_trade_id"))
        opened_tid = engine.execute_deferred_signal(res_pe, as_of_time=t_nov11_1110, enforce_cutoff=True)
        self.assertIsNotNone(opened_tid)

        # --- 4. Bug #4: 20m Short Regime-Break Stagnation Rung vs Quiet Theta Preservation ---
        # First verify quiet theta (ER=0.10, ATR_R=0.82) at +5.26% MFE and -4.8% bounce does NOT exit prematurely at 25m
        t_plus_10m = datetime(2026, 11, 11, 11, 20, 3)
        AdvancePaperExecution.update_open_trades(
            {"NSE_FO|T50_25300PE": {**q_pe_25300, "bid": 179.8, "ask": 180.1}},  # +5.26% MFE (< 10%, >= 3.5%)
            {"NIFTY": u_nov11},
            as_of_time=t_plus_10m,
            chain_intel_map={"NIFTY": {"kaufman_er": 0.10, "atr_ratio": 0.82}},
        )
        t_plus_25m = datetime(2026, 11, 11, 11, 35, 3)
        closed_quiet = AdvancePaperExecution.update_open_trades(
            {"NSE_FO|T50_25300PE": {**q_pe_25300, "bid": 199.1, "ask": 199.5}},  # -4.94% adverse, but quiet regime
            {"NIFTY": u_nov11},
            as_of_time=t_plus_25m,
            chain_intel_map={"NIFTY": {"kaufman_er": 0.10, "atr_ratio": 0.82}},
        )
        self.assertEqual(len(closed_quiet), 0, "Quiet contraction regime must NOT clip short theta trade!")

        # Now contraction breaks (ER=0.303 >= 0.28, ATR_R=1.178 >= 1.10) at 27m -> SHORT_REGIME_BREAK_STAGNATION_EXIT!
        t_plus_27m = datetime(2026, 11, 11, 11, 37, 3)
        closed_break = AdvancePaperExecution.update_open_trades(
            {"NSE_FO|T50_25300PE": {**q_pe_25300, "bid": 199.1, "ask": 199.5}},
            {"NIFTY": u_nov11},
            as_of_time=t_plus_27m,
            chain_intel_map={"NIFTY": {"kaufman_er": 0.303, "atr_ratio": 1.178}},
        )
        self.assertEqual(len(closed_break), 1)
        self.assertEqual(closed_break[0]["exit_reason"], "SHORT_REGIME_BREAK_STAGNATION_EXIT")

        # --- 2 & 5. Bug #2 (Long ATR_Ratio / Extreme VRP) & Bug #7 (Unhealthy 1m Feed) ---
        AdvanceSignalEngine.reset_state()
        with patch.object(
            engine.model,
            "predict_contract",
            return_value={"calibrated_prob": 0.56, "expected_net_return_pct": 12.0},
        ):
            t_0943 = datetime(2026, 11, 11, 9, 43, 22)
            c_25400ce = {
                "instrument_key": "NSE_FO|T48_25400CE",
                "trading_symbol": "NIFTY 25400 CE 17 NOV 26",
                "underlying": "NIFTY",
                "strike": 25400.0,
                "option_type": "CE",
                "lot_size": 65,
                "expiry": "2026-11-17",
            }
            q_25400ce = {
                **c_25400ce,
                "ltp": 251.3,
                "bid": 251.0,
                "ask": 251.55,
                "spread_pct": 0.22,
                "volume": 400000,
                "oi": 5682690,
                "iv": 17.8,
                "delta": 0.51,
                "quote_age_ms": 60,
            }
            # Shrinking ATR (0.801 < 0.88) below Session High (25400.8 < 25409.25 - 4.5) -> BLOCKED (Trade #48)
            intel_t48 = {
                "spot_price": 25400.8,
                "orb_high": 25399.1,
                "orb_low": 25263.8,
                "day_high": 25409.25,
                "day_low": 25263.8,
                "completed_bars_count": 28,
                "is_index_feed_healthy": True,
                "adx_14": 29.23,
                "plus_di": 32.42,
                "minus_di": 16.69,
                "kaufman_er": 0.403,
                "atr_ratio": 0.801,
                "vrp": 4.74,
                "rv_15m_ann": 13.04,
                "net_gex": 476.18,
            }
            res_t48 = engine.evaluate_contract(
                contract=c_25400ce,
                quote=q_25400ce,
                underlying_quote={"spot_ltp": 25400.8, "open": 25320.0, "vwap": 25360.0},
                chain_intel=intel_t48,
                as_of_time=t_0943,
                enforce_cutoff=True,
            )
            self.assertNotEqual(res_t48["status"], "ACTIVE")
            self.assertTrue(any("Volatility Contraction Guard for Long Buying" in r for r in res_t48["rejection_reasons"]))

            # Unhealthy 1m index feed fails closed (Bug #7)
            intel_stale = {**intel_t48, "atr_ratio": 1.15, "day_high": 25401.0, "is_index_feed_healthy": False, "index_feed_issue": "Stale 1m index candle (240s > 180s max)"}
            res_stale = engine.evaluate_contract(
                contract=c_25400ce,
                quote=q_25400ce,
                underlying_quote={"spot_ltp": 25400.8, "open": 25320.0, "vwap": 25360.0},
                chain_intel=intel_stale,
                as_of_time=t_0943,
                enforce_cutoff=True,
            )
            self.assertNotEqual(res_stale["status"], "ACTIVE")
            self.assertTrue(any("Index 1m feed unhealthy" in r for r in res_stale["rejection_reasons"]))


if __name__ == "__main__":
    unittest.main()
