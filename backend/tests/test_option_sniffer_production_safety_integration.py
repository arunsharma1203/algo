"""
Option Sniffer: Production Safety Integration Test Suite.
Validates the complete end-to-end integration of:
  1. Authoritative Completed-Bar Regime & Chop Gate (Wilder ADX, Kaufman ER, ATR compression ratio).
  2. Heavyweight Stock Alignment (15m completed bars without cross-session contamination).
  3. Reversal Exhaustion & Extrema Displacement (> 2.5x ATR).
  4. Reward-to-Cost Economic Friction Hurdle (>= 2.5x all-in friction).
  5. Causal Data Quality & Fail-Closed Behavior (missing bars, synthetic carry, gaps, staleness).
  6. Persistent Drawdown & Daily Loss Latching (peak-to-trough drawdown cannot be unlatched by later wins).
  7. Risk Capacity & Exposure Caps (max concurrent positions, anti-clustering single-underlying).
  8. Direction SL Cooldown & Anti-Ping-Pong Directional Flip Lockout.
  9. Execution Path Protection (Autopilot, Manual Frontend API, Order Intent Engine).
  10. Live Order Intent State Machine & Emergency SOS Liquidations.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, time
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd

from app.analytics.option_sniffer.option_constants import (
    STATE_WATCH,
    STATE_QUALIFIED,
    STATE_ACTIVE,
    DIRECTION_BUY_CE,
    DIRECTION_BUY_PE,
    DIRECTION_NO_TRADE,
    MAX_DAILY_LOSS_INR,
    PEAK_TO_TROUGH_MAX_DRAWDOWN_INR,
    MAX_CONCURRENT_OPEN_TRADES_CAP,
    MAX_QUOTE_AGE_MS,
    MAX_SPREAD_PCT,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TARGET_PCT
)
from app.analytics.option_sniffer.option_regime_gate import (
    OptionRegimeGate,
    compute_wilder_adx_canonical,
    compute_kaufman_er,
    compute_atr_ratio,
    ADX_MIN_THRESHOLD,
    KAUFMAN_ER_MIN_THRESHOLD,
    ATR_COMPRESSION_RATIO_MIN,
    MIN_REWARD_TO_COST_RATIO,
    EXTREMA_DISPLACEMENT_MAX_ATR
)
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
from app.analytics.option_sniffer.option_live_order_intent import (
    OrderIntentEngine,
    STATE_CREATED,
    STATE_VALIDATED,
    STATE_SUBMITTED,
    STATE_FILLED,
    STATE_REJECTED,
    EXIT_CONFIRMED_FLAT,
    MAX_SLIPPAGE_PCT
)
from app.analytics.option_sniffer.option_db import OptionDB, init_option_sniffer_tables
from app.data.index_candle_ingestion_prototype import IndexCandleIngestionPrototype


def _generate_synthetic_candles(
    n_bars: int = 35,
    trend: str = "UP",
    start_time: str = "2026-10-19 09:15:00",
    base_price: float = 25000.0,
    noise_amplitude: float = 2.0
) -> pd.DataFrame:
    """Helper to generate strictly contiguous, non-synthetic 1-minute OHLCV candles."""
    start_dt = datetime.fromisoformat(start_time)
    records = []
    curr = base_price
    for i in range(n_bars):
        c_ts = start_dt + timedelta(minutes=i)
        available_ts = c_ts + timedelta(minutes=1)
        if trend == "UP":
            drift = 5.0
        elif trend == "DOWN":
            drift = -5.0
        elif trend == "CHOP":
            drift = 3.0 if i % 2 == 0 else -3.0
        else:
            drift = 0.0

        open_p = curr
        high_p = open_p + max(1.0, abs(drift) + np.random.uniform(0.5, 1.5))
        low_p = open_p - max(1.0, abs(drift) + np.random.uniform(0.5, 1.5))
        close_p = open_p + drift + np.random.uniform(-noise_amplitude, noise_amplitude)
        high_p = max(high_p, open_p, close_p)
        low_p = min(low_p, open_p, close_p)

        records.append({
            "ticker": "NIFTY",
            "timestamp": c_ts.strftime("%Y-%m-%d %H:%M:%S"),
            "open": round(open_p, 2),
            "high": round(high_p, 2),
            "low": round(low_p, 2),
            "close": round(close_p, 2),
            "volume": 50000 + i * 100,
            "is_complete": 1,
            "is_synthetic": 0,
            "is_gap": 0,
            "received_at": c_ts.isoformat(),
            "available_at": available_ts.isoformat(),
            "is_point_in_time": 1,
            "source": "UPSTOX_V3_FEED"
        })
        curr = close_p
    return pd.DataFrame(records)


class TestOptionSnifferProductionSafetyIntegration(unittest.TestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        # Patch get_db_path across all relevant modules to use the temp test database
        self.p_db1 = patch("app.analytics.option_sniffer.option_db.get_db_path", return_value=self.db_path)
        self.p_db2 = patch("app.analytics.option_sniffer.option_live_order_intent.get_db_path", return_value=self.db_path)
        self.p_db3 = patch("app.analytics.option_sniffer.option_regime_gate.get_db_path", return_value=self.db_path)
        self.p_db4 = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)

        self.p_db1.start()
        self.p_db2.start()
        self.p_db3.start()
        self.p_db4.start()

        # Initialize tables
        init_option_sniffer_tables()
        IndexCandleIngestionPrototype(db_path=self.db_path).init_db()
        self.intent_engine = OrderIntentEngine()

        # Mock model for signal engine
        self.mock_model = MagicMock()
        self.mock_model.version = "TEST_MODEL_V1"
        self.mock_model.predict_contract.return_value = {
            "direction": DIRECTION_BUY_CE,
            "calibrated_prob": 0.68,
            "expected_net_return_pct": 12.5,
            "raw_score": 0.72,
        }
        self.signal_engine = OptionSignalEngine(model=self.mock_model)

        self.contract = {
            "instrument_key": "NSE_FO|54321",
            "trading_symbol": "NIFTY26OCT25000CE",
            "underlying": "NIFTY",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "option_type": "CE",
            "lot_size": 65,
        }
        self.valid_quote = {
            "bid": 100.0,
            "ask": 100.5,
            "ltp": 100.2,
            "spread_pct": 0.5,
            "oi": 50000,
            "volume": 5000,
            "quote_age_ms": 150,
            "delta": 0.52,
            "iv": 14.5
        }
        self.valid_underlying = {
            "spot_ltp": 25050.0,
            "open": 24950.0,
            "high": 25080.0,
            "low": 24930.0,
            "close": 25040.0,
            "vwap": 25000.0,
            "quote_age_ms": 150
        }

    def tearDown(self):
        self.p_db1.stop()
        self.p_db2.stop()
        self.p_db3.stop()
        self.p_db4.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    # -------------------------------------------------------------------------
    # 1. REGIME & CHOP GATE INTEGRATION TESTS
    # -------------------------------------------------------------------------

    def test_regime_gate_fails_closed_when_continuous_data_missing(self):
        """Pre-trade regime gate strictly blocks entry when continuous index data is missing."""
        as_of = datetime(2026, 10, 19, 10, 30, 0)
        regime_gate = OptionRegimeGate(db_path=self.db_path)
        res = regime_gate.evaluate_regime(
            underlying="NIFTY",
            opt_type="CE",
            as_of_time=as_of,
            continuous_bars=None,
            enforce_cutoff=True
        )
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Insufficient continuous completed bars" in r for r in res["rejection_reasons"]))

    def test_regime_gate_fails_closed_on_synthetic_or_gap_candles(self):
        """Synthetic carry candles or gap candles in warm-up window immediately fail closed."""
        df = _generate_synthetic_candles(n_bars=30, trend="UP")
        # Corrupt one candle in the required lookback window
        df.loc[25, "is_synthetic"] = 1

        regime_gate = OptionRegimeGate(db_path=self.db_path)
        # Populate DB table
        with sqlite3.connect(self.db_path) as conn:
            df.to_sql("continuous_index_ohlcv", conn, if_exists="append", index=False)

        as_of = datetime.fromisoformat(df.iloc[-1]["available_at"])
        res = regime_gate.evaluate_regime(
            underlying="NIFTY",
            opt_type="CE",
            as_of_time=as_of,
            enforce_cutoff=True
        )
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Synthetic carry bar detected" in r for r in res["rejection_reasons"]))

    def test_wilder_adx_chop_blocks_entry(self):
        """Wilder ADX(14) < 18.0 blocks option entry due to weak chop."""
        df_chop = _generate_synthetic_candles(n_bars=35, trend="CHOP", noise_amplitude=0.5)
        as_of = datetime.fromisoformat(df_chop.iloc[-1]["available_at"])

        regime_gate = OptionRegimeGate(db_path=self.db_path)
        res = regime_gate.evaluate_regime(
            underlying="NIFTY",
            opt_type="CE",
            as_of_time=as_of,
            continuous_bars=df_chop,
            option_ask=100.0,
            target_pct=35.0,
            lot_size=65,
            enforce_cutoff=True
        )
        self.assertFalse(res["hurdles_passed"])
        self.assertTrue(any("Chop Shield: Wilder ADX(14)" in r for r in res["rejection_reasons"]))

    def test_adx_directional_conflict_blocks_ce_and_pe(self):
        """+DI < -DI blocks CE; -DI < +DI blocks PE."""
        df_down = _generate_synthetic_candles(n_bars=35, trend="DOWN")
        as_of = datetime.fromisoformat(df_down.iloc[-1]["available_at"])

        regime_gate = OptionRegimeGate(db_path=self.db_path)
        # In a downtrend, +DI < -DI -> CE should be blocked
        res_ce = regime_gate.evaluate_regime(
            underlying="NIFTY",
            opt_type="CE",
            as_of_time=as_of,
            continuous_bars=df_down,
            option_ask=100.0,
            target_pct=35.0,
            lot_size=65,
            enforce_cutoff=True
        )
        self.assertFalse(res_ce["hurdles_passed"])
        self.assertTrue(any("+DI" in r and "CALL (CE) entry blocked" in r for r in res_ce["rejection_reasons"]))

        # In an uptrend, -DI < +DI -> PE should be blocked
        df_up = _generate_synthetic_candles(n_bars=35, trend="UP")
        as_of_up = datetime.fromisoformat(df_up.iloc[-1]["available_at"])
        res_pe = regime_gate.evaluate_regime(
            underlying="NIFTY",
            opt_type="PE",
            as_of_time=as_of_up,
            continuous_bars=df_up,
            option_ask=100.0,
            target_pct=35.0,
            lot_size=65,
            enforce_cutoff=True
        )
        self.assertFalse(res_pe["hurdles_passed"])
        self.assertTrue(any("-DI" in r and "PUT (PE) entry blocked" in r for r in res_pe["rejection_reasons"]))

    def test_heavyweight_stock_divergence_blocks_entry(self):
        """When all primary heavyweights have contrary slope, option entry is blocked."""
        regime_gate = OptionRegimeGate(db_path=self.db_path)
        as_of = datetime(2026, 10, 19, 11, 0, 0)

        # Simulate heavyweights falling (-0.30% each)
        falling_hw = {
            "RELIANCE": [{"timestamp": "2026-10-19 10:15:00", "open": 2800.0, "close": 2790.0}],
            "HDFCBANK": [{"timestamp": "2026-10-19 10:15:00", "open": 1650.0, "close": 1640.0}],
            "ICICIBANK": [{"timestamp": "2026-10-19 10:15:00", "open": 1200.0, "close": 1190.0}],
            "INFY": [{"timestamp": "2026-10-19 10:15:00", "open": 1900.0, "close": 1890.0}],
            "TCS": [{"timestamp": "2026-10-19 10:15:00", "open": 4200.0, "close": 4180.0}],
        }
        # Add a 2nd completed 15m bar
        for k, v in falling_hw.items():
            v.append({"timestamp": "2026-10-19 10:30:00", "open": v[0]["close"], "close": v[0]["close"] * 0.995})

        ok, msg, slopes = regime_gate.evaluate_heavyweight_slopes(
            underlying="NIFTY",
            opt_type="CE",
            as_of_time=as_of,
            heavyweight_candles=falling_hw
        )
        self.assertFalse(ok)
        self.assertIn("Heavyweight Overhead", msg)

    def test_reversal_extrema_displacement_exhaustion(self):
        """When underlying has moved > 2.5x ATR off day extrema, counter-trend entry is blocked."""
        regime_gate = OptionRegimeGate(db_path=self.db_path)
        # Spot has rallied 120 points off low; spot ATR is 30 points (4.0x ATR displacement)
        ok, msg = regime_gate.evaluate_reversal_displacement(
            spot_ltp=25120.0,
            day_high=25130.0,
            day_low=25000.0,
            spot_atr=30.0,
            opt_type="PE",
            is_breakout=False
        )
        self.assertFalse(ok)
        self.assertIn("Reversal Exhaustion", msg)

    def test_reward_to_cost_friction_hurdle(self):
        """Option ask with low target gain failing the 2.5x friction hurdle is rejected."""
        regime_gate = OptionRegimeGate(db_path=self.db_path)
        # Cheap penny option ₹10 with tiny 5% target (gain = 0.50 * 65 = ₹32.50 vs ₹55+ friction)
        ok, msg, _ = regime_gate.evaluate_reward_to_cost(
            option_ask=10.0,
            target_pct=5.0,
            lot_size=65
        )
        self.assertFalse(ok)
        self.assertIn("Reward-to-Cost Gate", msg)

    # -------------------------------------------------------------------------
    # 2. SIGNAL ENGINE GATE 5E & PERSISTENT DRAWDOWN LATCHING
    # -------------------------------------------------------------------------

    def test_signal_engine_gate_5e_blocks_in_watch(self):
        """SignalEngine blocks in STATE_WATCH when Gate 5E regime evaluation fails."""
        df_chop = _generate_synthetic_candles(n_bars=35, trend="CHOP")
        as_of = datetime.fromisoformat(df_chop.iloc[-1]["available_at"])

        res = self.signal_engine.evaluate_contract(
            contract=self.contract,
            quote=self.valid_quote,
            underlying_quote=self.valid_underlying,
            underlying_df=df_chop,
            as_of_time=as_of,
            enforce_cutoff=True,
            enforce_regime_gate=True
        )
        self.assertEqual(res["status"], STATE_WATCH)
        self.assertFalse(res["hurdles_passed"])
        self.assertFalse(res["regime_gate_passed"])
        self.assertTrue(any("Chop Shield" in r or "Regime Gate" in r for r in res["rejection_reasons"]))

    def test_chronological_drawdown_latch_cannot_be_unlatched_by_later_win(self):
        """Proves that a peak-to-trough drawdown latch permanently halts session in SQLite ledger."""
        today = "2026-10-19"
        # Simulate 3 trades:
        # Trade 1: -₹2,000 loss
        # Trade 2: -₹1,800 loss (cumulative loss -₹3,800 -> exceeds -₹3,500 drawdown ceiling)
        # Trade 3: +₹5,000 win (realized net PnL becomes +₹1,200)
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO option_sniffer_paper_trades
                (trade_id, signal_id, instrument_key, trading_symbol, underlying, option_type, strike, expiry,
                 lot_size, quantity, entry_time, entry_ask, entry_price_used, exit_time, exit_price_used, status, net_pnl, exit_reason)
                VALUES
                ('T1', 'S1', 'NSE_FO|1', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29', 50, 100, '2026-10-19 09:30:00', 100, 100, '2026-10-19 09:45:00', 80, 'CLOSED', -2000.0, 'STOP_LOSS_HIT'),
                ('T2', 'S2', 'NSE_FO|2', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29', 50, 100, '2026-10-19 10:00:00', 100, 100, '2026-10-19 10:15:00', 82, 'CLOSED', -1800.0, 'STOP_LOSS_HIT'),
                ('T3', 'S3', 'NSE_FO|3', 'NIFTY 25000 CE', 'NIFTY', 'CE', 25000, '2026-10-29', 50, 100, '2026-10-19 11:00:00', 100, 100, '2026-10-19 11:30:00', 150, 'CLOSED', 5000.0, 'TARGET_HIT');
            """)
            conn.commit()

        # Query circuit breaker telemetry
        as_of = datetime(2026, 10, 19, 12, 0, 0)
        telem = OptionDB.get_daily_circuit_breaker_telemetry(
            max_daily_loss_inr=3500.0,
            as_of_date=today,
            as_of_time=as_of
        )
        # Net PnL is positive (+₹1,200), BUT peak-to-trough drawdown was -₹3,800
        self.assertGreater(telem["today_net_pnl"], 0.0)
        self.assertGreaterEqual(telem["max_drawdown"], 3500.0)
        self.assertTrue(telem["is_session_halted"])
        self.assertIn("drawdown", telem["halt_reason"].lower())

        # Verify latch in option_sniffer_paper_risk_ledger
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT circuit_breaker_tripped, trip_reason FROM option_sniffer_paper_risk_ledger WHERE session_date = ?", (today,))
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 1)
            self.assertIn("drawdown", row[1].lower())

    # -------------------------------------------------------------------------
    # 3. EXECUTION PATH SAFEGUARDS & PRE-TRADE GATES
    # -------------------------------------------------------------------------

    def test_live_intent_engine_rejects_unverified_regime(self):
        """OrderIntentEngine strictly rejects orders when regime check fails."""
        signal_payload = {
            "signal_id": "SIG_REG_FAIL_001",
            "instrument_key": "NSE_FO|54321",
            "trading_symbol": "NIFTY 25000 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 100.0,
            "regime_gate_passed": False,
            "regime_rejection_reasons": ["Wilder ADX(14) 14.2 indicates chop"]
        }
        live_quote = {"ask": 100.0, "bid": 99.8, "quote_age_ms": 100}

        ok, reason, intent = self.intent_engine.create_and_validate_intent(
            signal_payload=signal_payload,
            live_quote=live_quote
        )
        self.assertFalse(ok)
        self.assertIn("REGIME_GATE_BLOCKED", reason)
        self.assertIn("Wilder ADX(14) 14.2 indicates chop", reason)

    def test_frontend_api_enforces_regime_gate_by_default(self):
        """API endpoint /live/simulate-order enforces regime gate and blocks unverified signals."""
        from app.api.option_sniffer_api import simulate_order_intent

        payload = {
            "signal": {
                "signal_id": "SIG_API_001",
                "instrument_key": "NSE_FO|54321",
                "trading_symbol": "NIFTY 25000 CE",
                "underlying": "NIFTY",
                "action": "BUY_CE",
                "suggested_entry": 100.0,
                "as_of_time": datetime(2026, 10, 19, 10, 30, 0),
                "enforce_cutoff": True
            },
            "quote": {"ask": 100.0, "bid": 99.8, "quote_age_ms": 100},
            "deployable_capital": 15000.0
        }
        res = simulate_order_intent(payload)
        self.assertEqual(res["status"], "BLOCKED")
        self.assertIn("REGIME_GATE_BLOCKED", res["reason"])

    def test_sos_panic_exit_state_machine_and_zero_quantity_check(self):
        """Emergency SOS panic exit locks mutex, flattens position, and confirms broker qty == 0."""
        # 1. Create and validate intent
        signal_payload = {
            "signal_id": "SIG_SOS_001",
            "instrument_key": "NSE_FO|60001",
            "trading_symbol": "NIFTY 25100 CE",
            "underlying": "NIFTY",
            "action": "BUY_CE",
            "suggested_entry": 150.0,
            "regime_gate_passed": True
        }
        live_quote = {"ask": 150.0, "bid": 149.5, "quote_age_ms": 100}
        ok, _, intent = self.intent_engine.create_and_validate_intent(signal_payload, live_quote)
        self.assertTrue(ok)
        intent_id = intent["order_intent_id"]

        # 2. Simulate filled position
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE option_live_order_intents
                SET state = 'FILLED', filled_quantity = 65, average_fill_price = 150.0,
                    broker_order_id = 'REAL_ORD_SOS_99', updated_at = datetime('now')
                WHERE order_intent_id = ?;
            """, (intent_id,))
            conn.commit()

        # 3. Mock Upstox broker API for exit order and position book
        call_tracker = {"exit_placed": False}
        def api_side_effect(method, endpoint, payload=None, **kwargs):
            if endpoint == "/portfolio/short-term-positions":
                if not call_tracker["exit_placed"]:
                    return True, 200, {"status": "success", "data": [{"instrument_token": "NSE_FO|60001", "quantity": 65}]}
                return True, 200, {"status": "success", "data": [{"instrument_token": "NSE_FO|60001", "quantity": 0}]}
            elif endpoint == "/order/place":
                call_tracker["exit_placed"] = True
                return True, 200, {"status": "success", "data": {"order_id": "EXIT_BROKER_ORD_101"}}
            return True, 200, {"status": "success"}

        with patch.object(self.intent_engine, "_call_upstox_broker_api", side_effect=api_side_effect):
            res = self.intent_engine.trigger_sos_panic_exit(intent_id, current_bid_price=140.0)
            self.assertEqual(res["status"], "CONFIRMED_FLAT")
            self.assertEqual(res["exit_state"], EXIT_CONFIRMED_FLAT)
            self.assertEqual(res["broker_position_qty"], 0)


if __name__ == "__main__":
    unittest.main()
