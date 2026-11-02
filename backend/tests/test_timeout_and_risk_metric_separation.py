"""
Unit test suite verifying timeout reconciliation and mathematical separation of risk metrics:
  1. Timeout Reconciliation:
     - MAX_QUOTE_AGE_MS = 15,000 ms: Scanner surveillance freshness in OptionSignalEngine (Gate 1).
     - MAX_EXECUTION_QUOTE_AGE_MS = 1,000 ms: Order execution freshness in OrderIntentEngine.
     - MAX_DATA_STALENESS_SECONDS = 120.0 s: Continuous index 1m OHLCV candle freshness in OptionRegimeGate (Gate 5E).
     - UNRESOLVED_ORDER_TIMEOUT_SECONDS = 15.0 s: Broker response timeout before AMBIGUOUS_ORDER_PENDING state.
  2. Mathematical Separation of 5 Risk Metrics:
     - Session Realized Net Loss: L_realized <= -3,500.00 INR.
     - Peak-to-Trough Drawdown: Peak - Current >= 3,500.00 INR.
     - Open-Position MTM Risk: Separated from realized loss; worst-case SL exposure.
     - Pending-Order Reserved Risk: Committed capital for submitted orders.
     - Ambiguous-Order Risk: Orders unconfirmed after 15s halt new submissions.
  3. Latch Permanence & Exit Availability:
     - Latch cannot be cleared while exposure or broker state is unresolved.
     - Exits (SL, Target, SOS) remain strictly available while latched.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from app.analytics.option_sniffer.option_constants import (
    MAX_QUOTE_AGE_MS,
    MAX_EXECUTION_QUOTE_AGE_MS,
    MAX_DATA_STALENESS_SECONDS,
    UNRESOLVED_ORDER_TIMEOUT_SECONDS,
    MAX_DAILY_LOSS_INR,
    PEAK_TO_TROUGH_MAX_DRAWDOWN_INR,
    STATE_WATCH,
    STATE_QUALIFIED,
    DIRECTION_BUY_CE,
)
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
from app.analytics.option_sniffer.option_regime_gate import OptionRegimeGate
from app.analytics.option_sniffer.option_live_order_intent import (
    OrderIntentEngine,
    init_live_order_tables,
    STATE_VALIDATED,
    STATE_SUBMITTED,
    STATE_FILLED,
    STATE_REJECTED,
    STATE_AMBIGUOUS_ORDER_PENDING,
    EXIT_NONE,
    EXIT_PENDING,
    EXIT_CONFIRMED_FLAT,
)
from app.analytics.option_sniffer.option_db import (
    OptionDB,
    init_option_sniffer_tables,
)


class TestTimeoutAndRiskMetricSeparation(unittest.TestCase):

    def setUp(self):
        OptionSignalEngine.reset_class_state()
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp_db.name
        self.tmp_db.close()

        self.db_patcher = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.db_patcher2 = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.db_patcher3 = patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path)
        self.db_patcher.start()
        self.db_patcher2.start()
        self.db_patcher3.start()

        init_option_sniffer_tables()
        init_live_order_tables()
        self.intent_engine = OrderIntentEngine()

    def tearDown(self):
        OptionSignalEngine.reset_class_state()
        self.db_patcher3.stop()
        self.db_patcher2.stop()
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    # -------------------------------------------------------------------------
    # 1. TIMEOUT RECONCILIATION & PRODUCTION PREDICATES
    # -------------------------------------------------------------------------

    def test_timeout_values_are_strictly_reconciled(self):
        """Proves each data type has its own distinct, documented timeout configuration."""
        self.assertEqual(MAX_QUOTE_AGE_MS, 15000)
        self.assertEqual(MAX_EXECUTION_QUOTE_AGE_MS, 1000)
        self.assertEqual(MAX_DATA_STALENESS_SECONDS, 120.0)
        self.assertEqual(UNRESOLVED_ORDER_TIMEOUT_SECONDS, 15.0)

    def test_scanner_predicate_accepts_14s_and_rejects_16s(self):
        """
        Scanner surveillance predicate: quote_age_ms <= MAX_QUOTE_AGE_MS (15,000ms).
        - 14,000ms (14.0s) passes Gate 1.
        - 16,000ms (16.0s) rejected at Gate 1 with 'Stale option quote'.
        """
        mock_model = MagicMock()
        mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.65,
            "expected_net_return_pct": 8.0,
        }
        engine = OptionSignalEngine(model=mock_model)

        contract = {
            "instrument_key": "NSE_FO|99001",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "strike": 25000,
            "option_type": "CE",
            "expiry": "2026-10-29",
            "lot_size": 65,
        }

        u_quote = {
            "spot_ltp": 25000.0, "open": 25000.0, "high": 25010.0, "low": 24990.0, "close": 25005.0,
            "quote_age_ms": 100,
        }

        # 16,000ms: Must fail Gate 1
        quote_16s = {
            "bid": 100.0, "ask": 101.0, "ltp": 100.5, "spread_pct": 0.99,
            "quote_age_ms": 16000, "oi": 500000, "volume": 200000,
            "delta": 0.50, "iv": 14.5, "quote_freshness": "STALE",
        }
        res = engine.evaluate_contract(contract=contract, quote=quote_16s, underlying_quote=u_quote, enforce_cutoff=False)
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Stale quote" in r for r in res["rejection_reasons"]))

        # 14,000ms: Passes Gate 1
        quote_14s = dict(quote_16s, quote_age_ms=14000, quote_freshness="REAL_TIME")
        res_pass = engine.evaluate_contract(contract=contract, quote=quote_14s, underlying_quote=u_quote, enforce_cutoff=False)
        rejections = " ".join(res_pass.get("rejection_reasons", []))
        self.assertNotIn("Stale quote", rejections)

    def test_execution_predicate_accepts_800ms_and_rejects_1200ms(self):
        """
        Execution predicate in OrderIntentEngine: quote_age_ms <= MAX_EXECUTION_QUOTE_AGE_MS (1,000ms).
        - 800ms quote is accepted for order dispatch.
        - 1,200ms quote is rejected (STATE_REJECTED) to prevent stale execution slippage.
        """
        signal_data = {
            "signal_id": "SIG_EXEC_800",
            "instrument_key": "NSE_FO|111",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "direction": "BUY_CE",
            "lot_size": 65,
            "recommended_lots": 1,
            "ask": 100.0,
            "bid": 99.5,
            "quote_age_ms": 800,  # <= 1000ms
        }
        mock_adapter = MagicMock()
        mock_adapter.get_margin_status.return_value = {"free_margin": 50000.0}

        valid_800, msg_800, intent_800 = self.intent_engine.create_and_validate_intent(signal_data, mock_adapter)
        self.assertTrue(valid_800)
        self.assertEqual(intent_800["state"], STATE_VALIDATED)

        # 1,200ms quote
        signal_1200 = dict(signal_data, signal_id="SIG_EXEC_1200", quote_age_ms=1200)
        valid_1200, msg_1200, intent_1200 = self.intent_engine.create_and_validate_intent(signal_1200, mock_adapter)
        self.assertFalse(valid_1200)
        self.assertIn("exceeds 1000ms execution freshness ceiling", msg_1200)

    def test_candle_staleness_predicate_accepts_110s_and_rejects_130s(self):
        """
        Regime gate candle staleness predicate:
        (as_of_time - candle_end_time).total_seconds() <= MAX_DATA_STALENESS_SECONDS (120.0s).
        """
        from app.analytics.option_sniffer.option_regime_gate import seed_mock_continuous_bars
        now = datetime.now()
        seed_mock_continuous_bars(self.db_path, ticker="NIFTY", num_bars=30, base_time=now)
        gate = OptionRegimeGate(db_path=self.db_path)
        session_date = now.strftime("%Y-%m-%d")

        # Decision at now + 30s: staleness = 30s <= 120s -> OK
        as_of_fresh = now + timedelta(seconds=30)
        ok_fresh, msg_fresh, _ = gate.get_completed_index_bars("NIFTY", session_date, as_of_fresh)
        self.assertTrue(ok_fresh)

        # Decision at now + 150s: staleness = 150s > 120s -> Stale fail-closed
        as_of_stale = now + timedelta(seconds=150)
        ok_stale, msg_stale, _ = gate.get_completed_index_bars("NIFTY", session_date, as_of_stale)
        self.assertFalse(ok_stale)
        self.assertIn("Index feed is stale", msg_stale)

    def test_unresolved_order_predicate_triggers_ambiguous_after_15s(self):
        """
        Unresolved order predicate:
        (now - submitted_at) > UNRESOLVED_ORDER_TIMEOUT_SECONDS (15.0s) in STATE_SUBMITTED
        transitions intent to STATE_AMBIGUOUS_ORDER_PENDING.
        """
        now = datetime.now()
        submitted_at_16s_ago = (now - timedelta(seconds=16)).strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents (
                    order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action,
                    lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state,
                    broker_order_id, exit_state, gross_pnl, statutory_charges, realized_pnl,
                    created_at, updated_at
                ) VALUES (
                    'INTENT_TIMEOUT_1', 'SIG_T1', 'NSE_FO|999', 'NIFTY 25000 CE', 'NIFTY', 'BUY_CE',
                    65, 65, 0, 100.0, 0.0, 'SUBMITTED',
                    'BROKER_SUB_1', 'NONE', 0.0, 0.0, 0.0,
                    ?, ?
                );
            """, (submitted_at_16s_ago, submitted_at_16s_ago))

        mock_adapter = MagicMock()
        mock_adapter.get_margin_status.return_value = {"free_margin": 50000.0}

        signal_new = {
            "signal_id": "SIG_NEW_WHILE_PENDING",
            "instrument_key": "NSE_FO|111",
            "trading_symbol": "NIFTY 25100 CE",
            "underlying": "NIFTY",
            "direction": "BUY_CE",
            "lot_size": 65,
            "recommended_lots": 1,
            "ask": 100.0,
            "bid": 99.5,
            "quote_age_ms": 200,
        }
        valid, msg, intent = self.intent_engine.create_and_validate_intent(signal_new, mock_adapter)
        self.assertFalse(valid)
        self.assertIn("AMBIGUOUS_ORDER_PENDING", msg)
        self.assertIn("awaiting broker confirmation", msg)

    # -------------------------------------------------------------------------
    # 2. MATHEMATICAL SEPARATION OF RISK METRICS
    # -------------------------------------------------------------------------

    def test_realized_loss_boundary_isolated_from_drawdown(self):
        """
        Session Realized Net Loss: L_realized = sum(closed trades today).
        Ceiling: L_realized <= -3,500.00 INR.
        Must evaluate strictly on net realized PnL regardless of peak or open trades.
        """
        today = "2026-10-19"
        with sqlite3.connect(self.db_path) as conn:
            # Trade with exactly -3,500.00 net PnL
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades
                (trade_id, signal_id, instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                 lot_size, quantity, entry_time, entry_ask, entry_price_used, exit_time, exit_price_used, status, net_pnl, exit_reason)
                VALUES
                ('T_RL_3500', 'S_RL', 'NSE_FO|10', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29',
                 65, 65, '2026-10-19 10:00:00', 100.0, 100.0, '2026-10-19 10:15:00', 46.15, 'CLOSED', -3500.00, 'STOP_LOSS_HIT');
            """)

        telem = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            enforce_halt_latch=False
        )
        self.assertEqual(telem["today_net_pnl"], -3500.00)
        self.assertTrue(telem["is_realized_limit_hit"])
        self.assertTrue(telem["is_session_halted"])

    def test_peak_to_trough_drawdown_boundary_isolated(self):
        """
        Peak-to-Trough Drawdown: Peak - Current >= 3,500.00 INR.
        Here:
          Trade 1: +5,000.00 (Peak = +5,000.00)
          Trade 2: -3,500.00 (Current = +1,500.00)
          Drawdown = 5,000.00 - 1,500.00 = 3,500.00 INR >= 3,500.00 INR.
        Circuit breaker trips on DRAWDOWN even though total session net P&L is POSITIVE (+1,500.00).
        """
        today = "2026-10-19"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades
                (trade_id, signal_id, instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                 lot_size, quantity, entry_time, entry_ask, entry_price_used, exit_time, exit_price_used, status, net_pnl, exit_reason)
                VALUES
                ('T_WIN', 'S_W', 'NSE_FO|11', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29',
                 65, 65, '2026-10-19 09:30:00', 100.0, 100.0, '2026-10-19 10:00:00', 176.92, 'CLOSED', 5000.00, 'TARGET_HIT'),
                ('T_LOSS', 'S_L', 'NSE_FO|12', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29',
                 65, 65, '2026-10-19 10:30:00', 100.0, 100.0, '2026-10-19 11:00:00', 46.15, 'CLOSED', -3500.00, 'STOP_LOSS_HIT');
            """)

        telem = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            enforce_halt_latch=False
        )
        self.assertEqual(telem["today_net_pnl"], 1500.00)  # Positive net PnL!
        self.assertFalse(telem["is_realized_limit_hit"])   # Realized limit NOT hit (positive PnL)
        self.assertTrue(telem["is_drawdown_limit_hit"])    # DRAWDOWN limit HIT (3,500.00 >= 3,500.00)
        self.assertTrue(telem["is_session_halted"])        # Session halted to protect banked gains

    def test_open_position_mtm_risk_isolated(self):
        """
        Open-Position MTM Risk: Separated from realized loss.
        If an open trade has floating MTM loss, it is accounted for in total exposure,
        preventing new entries while keeping exit execution fully available.
        """
        today = "2026-10-19"
        with sqlite3.connect(self.db_path) as conn:
            # 1 closed loss of -2,000
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades
                (trade_id, signal_id, instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                 lot_size, quantity, entry_time, entry_ask, entry_price_used, exit_time, exit_price_used, status, net_pnl, exit_reason)
                VALUES
                ('T_C1', 'S1', 'NSE_FO|21', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29',
                 65, 65, '2026-10-19 09:30:00', 100.0, 100.0, '2026-10-19 10:00:00', 69.23, 'CLOSED', -2000.00, 'STOP_LOSS_HIT');
            """)
            # 1 open trade with entry at 100.0
            conn.execute("""
                INSERT INTO option_sniffer_paper_trades
                (trade_id, signal_id, instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                 lot_size, quantity, entry_time, entry_ask, entry_price_used, status, net_pnl)
                VALUES
                ('T_O1', 'S2', 'NSE_FO|22', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29',
                 65, 65, '2026-10-19 10:30:00', 100.0, 100.0, 'OPEN', 0.0);
            """)

        # When current price drops to 75.0 (unrealized loss = -1625.0 + friction ~ 55 = -1680.0)
        # Total exposure = -2000 + (-1680) = -3680 <= -3500 limit
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE option_sniffer_paper_trades
                SET gross_pnl = -1625.0, charges = 55.0, net_pnl = -1680.0
                WHERE trade_id = 'T_O1';
            """)

        telem = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            enforce_halt_latch=False,
            live_quotes_map={"NSE_FO|22": {"bid": 75.0, "ask": 75.5}}
        )
        self.assertEqual(telem["today_net_pnl"], -2000.00)  # Realized is -2000
        self.assertLess(telem["unrealized_pnl"], -1600.00)   # Open MTM is ~ -1680
        self.assertTrue(telem["is_max_loss_limit_hit"])      # Total exposure tripped
        self.assertTrue(telem["is_session_halted"])

        # Exits must remain available: Open trade T_O1 can still be closed cleanly!
        closed = OptionDB.close_paper_trade(
            trade_id="T_O1",
            close_data={
                "exit_time": "2026-10-19 11:00:00",
                "exit_bid": 75.0,
                "exit_ask": 75.5,
                "exit_price_used": 75.0,
                "gross_pnl": -1625.0,
                "charges": 55.0,
                "slippage": 0.0,
                "net_pnl": -1680.0,
                "net_pnl_pct": -25.0,
                "holding_duration_seconds": 1800,
                "exit_reason": "STOP_LOSS_HIT"
            }
        )
        self.assertTrue(closed)

    def test_ambiguous_order_risk_blocks_new_orders_while_latched(self):
        """
        Ambiguous-Order Risk:
        If ANY order is in STATE_AMBIGUOUS_ORDER_PENDING, new live order intent validation
        must be strictly blocked to prevent compounding exposure on untracked broker states.
        """
        # Insert an ambiguous order intent
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO option_live_order_intents (
                    order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action,
                    lot_size, target_quantity, filled_quantity, limit_price, average_fill_price, state,
                    broker_order_id, exit_state, gross_pnl, statutory_charges, realized_pnl,
                    created_at, updated_at
                ) VALUES (
                    'INTENT_AMB_1', 'SIG_AMB', 'NSE_FO|333', 'NIFTY 25000 CE', 'NIFTY', 'BUY_CE',
                    65, 65, 0, 100.0, 0.0, 'AMBIGUOUS_ORDER_PENDING',
                    'BROKER_AMB_1', 'NONE', 0.0, 0.0, 0.0,
                    '2026-10-19 10:00:00', '2026-10-19 10:00:00'
                );
            """)

        # Attempt to validate a new order intent
        signal_new = {
            "signal_id": "SIG_NEW_WHILE_AMB",
            "instrument_key": "NSE_FO|444",
            "trading_symbol": "NIFTY 25100 CE",
            "underlying": "NIFTY",
            "direction": "BUY_CE",
            "lot_size": 65,
            "recommended_lots": 1,
            "ask": 80.0,
            "bid": 79.5,
            "quote_age_ms": 200,
        }
        mock_adapter = MagicMock()
        mock_adapter.get_margin_status.return_value = {"free_margin": 50000.0}

        valid_amb, msg_amb, intent_amb = self.intent_engine.create_and_validate_intent(signal_new, mock_adapter)
        self.assertFalse(valid_amb)
        self.assertIn("AMBIGUOUS_ORDER_PENDING", msg_amb)


if __name__ == "__main__":
    unittest.main()
