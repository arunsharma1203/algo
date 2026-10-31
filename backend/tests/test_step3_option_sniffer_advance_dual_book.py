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


if __name__ == "__main__":
    unittest.main()
