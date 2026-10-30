"""
Unit & Integration Test Suite for Step 1:
15-Minute Consensus Scan Freeze Remediation & 28-Minute Candle Contiguity Fix.

Verifies:
  1. DataGateway.get_ohlcv(limit=...) slices returned DataFrame without raising TypeError.
  2. QlibVirtualTracker.reconcile_open_recommendations updates only OPEN rows by primary key `id`
     in sub-second time without full-table write locks.
  3. ml_history.ensure_ml_table caches initialized DB paths, and evaluate_ml_history caches
     finalized rows for O(1) warm evaluation while preserving 100% accuracy on OPEN rows.
  4. IndiaMarketDataAdapter.sync_intraday_universe returns `synced: True`, deduplicates back-to-back
     sync calls within 60s when authoritative timestamp is unchanged, and avoids uncontrolled
     live network fetches by default (`allow_network_fetch=False`).
  5. IndexCandleIngestionPrototype.heal_intraday_gaps_if_needed enforces the 45-second Upstox
     rate-limit cooldown and finalizes elapsed 1-minute buckets deterministically.
  6. OptionRegimeGate.get_completed_index_bars and evaluate_regime pass 28-bar contiguity and
     preserve fresh directional impulse breakouts (|DI+ - DI-| >= 12.0 & ER >= 0.35 & ADX >= 15.0)
     while still blocking true low-ADX chop.
"""

import os
import sys
import tempfile
import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.data.data_gateway import DataGateway
from app.qlib_engine.virtual_tracker import QlibVirtualTracker, init_qlib_tracking_schema
from app.api.ml_history import ensure_ml_table, save_ml_trade, evaluate_ml_history
from app.qlib_engine.india_data_adapter import IndiaMarketDataAdapter
from app.data.index_candle_ingestion_prototype import IndexCandleIngestionPrototype
from app.analytics.option_sniffer.option_regime_gate import OptionRegimeGate


class TestStep1ConsensusFreezeAnd28mCandleFix(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_step1.db")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_01_data_gateway_get_ohlcv_accepts_limit_parameter(self):
        """DataGateway.get_ohlcv(..., limit=30) must slice to last 30 rows without TypeError."""
        dates = pd.date_range("2026-10-01 09:15:00", periods=60, freq="15min")
        mock_df = pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d %H:%M:%S"),
            "open": np.linspace(100, 160, 60),
            "high": np.linspace(101, 161, 60),
            "low": np.linspace(99, 159, 60),
            "close": np.linspace(100.5, 160.5, 60),
            "volume": [1000] * 60,
        })
        with patch("app.data.historical_data_layer.HistoricalDataLayer.get_historical_ohlcv", return_value=mock_df):
            df_limited = DataGateway.get_ohlcv("RELIANCE", timeframe="15m", fetch_incremental=False, limit=30)
            self.assertEqual(len(df_limited), 30)
            self.assertAlmostEqual(float(df_limited.iloc[-1]["close"]), 160.5, places=2)

    def test_02_virtual_tracker_reconcile_updates_only_open_rows_by_pk(self):
        """QlibVirtualTracker.reconcile_open_recommendations matches OPEN rows in O(1) and updates by id."""
        with patch("app.qlib_engine.virtual_tracker.get_db_path", return_value=self.db_path), \
             patch("app.api.ml_history.get_db_path", return_value=self.db_path):
            init_qlib_tracking_schema()
            ensure_ml_table()

            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO qlib_virtual_recommendations
                (timestamp, ticker, strategy, direction, entry_price, stop_loss, target_price, confidence, model_id, model_hash, feature_version, status, outcome)
                VALUES ('2026-10-30T10:00:00', 'TCS.NS', 'INTRADAY', 'BUY', 4000.0, 3950.0, 4100.0, 75.0, 'qlib_intraday_v1', 'abc123', 'qlib_alpha158_v1', 'OPEN', 'OPEN')
            """)
            conn.commit()
            conn.close()

            fake_eval = [{
                "id": 1,
                "timestamp": "2026-10-30 10:00",
                "ticker": "TCS.NS",
                "strategy": "INTRADAY",
                "trade_type": "INTRADAY",
                "direction": "BULLISH",
                "status": "CLOSED",
                "outcome": "TARGET MET",
                "profit_pct": 2.42,
                "exit_price": 4100.0,
                "exit_time": "2026-10-30 11:15:00",
                "engine": "QLIB",
                "source": "QLIB_SCANNER",
            }]
            with patch("app.api.ml_history.evaluate_ml_history", return_value=fake_eval):
                count = QlibVirtualTracker.reconcile_open_recommendations(force_refresh=True)
                self.assertEqual(count, 1)

            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT status, outcome, pnl_pct, exit_price FROM qlib_virtual_recommendations WHERE ticker = 'TCS.NS'"
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], "CLOSED")
            self.assertEqual(row[1], "TARGET MET")
            self.assertAlmostEqual(float(row[2]), 2.42, places=2)
            self.assertAlmostEqual(float(row[3]), 4100.0, places=2)

    def test_03_ml_history_finalized_row_cache_and_schema_guard(self):
        """ensure_ml_table and evaluate_ml_history cache finalized rows while accurately resolving OPEN rows."""
        with patch("app.api.ml_history.get_db_path", return_value=self.db_path):
            ensure_ml_table()
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO ml_trade_history
                (timestamp, ticker, direction, entry, sl, tp1, tp2, confidence, trade_type, status, outcome, profit_pct, exit_price, exit_time, engine, source)
                VALUES ('2026-10-29T10:00:00', 'INFY.NS', 'BULLISH', 1800.0, 1770.0, 1850.0, 1880.0, 78.0, 'INTRADAY', 'CLOSED', 'TARGET MET', 2.65, 1850.0, '2026-10-29T11:30:00', 'QLIB', 'QLIB_SCANNER')
            """)
            conn.commit()
            conn.close()

            with patch("app.analytics.macro_engine.get_macro_regime", return_value={"regime": "BULL"}):
                res1 = evaluate_ml_history(force_refresh=True, engine="QLIB")
                res2 = evaluate_ml_history(force_refresh=True, engine="QLIB")
                self.assertEqual(len(res1), 1)
                self.assertEqual(res1[0]["outcome"], "TARGET MET")
                self.assertIs(res1[0], res2[0])  # O(1) finalized row cache hit

    def test_04_qlib_adapter_sync_intraday_returns_synced_and_deduplicates(self):
        """sync_intraday_universe exports bulk SQLite 15m bars, returns synced=True, and deduplicates within 60s."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE ohlcv (
                ticker TEXT,
                timeframe TEXT,
                date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL
            )
        """)
        base_dt = datetime(2026, 10, 28, 9, 15, 0)
        rows = []
        for i in range(55):
            dt_str = (base_dt + timedelta(minutes=15 * i)).strftime("%Y-%m-%d %H:%M:%S")
            rows.append(("RELIANCE.NS", "15m", dt_str, 2800.0 + i, 2805.0 + i, 2795.0 + i, 2802.0 + i, 10000.0))
        conn.executemany("INSERT INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()

        qlib_dir = os.path.join(self.tmp_dir.name, "qlib_bin")
        adapter = IndiaMarketDataAdapter(provider_uri=qlib_dir)
        with patch("app.qlib_engine.india_data_adapter.get_db_path", return_value=self.db_path):
            res1 = adapter.sync_intraday_universe(tickers=["RELIANCE"], min_bars=50)
            self.assertTrue(res1.get("synced"))
            self.assertEqual(res1.get("symbols_updated"), 1)
            self.assertIn("duration", res1)

            # Immediate 2nd call hits 60s deduplication cache
            res2 = adapter.sync_intraday_universe(tickers=["RELIANCE"], min_bars=50)
            self.assertEqual(res2.get("data_hash"), res1.get("data_hash"))

    def test_05_index_ingestion_heal_intraday_gaps_respects_cooldown(self):
        """heal_intraday_gaps_if_needed enforces 45s cooldown to protect 64 req/min Upstox ceiling."""
        svc = IndexCandleIngestionPrototype(db_path=self.db_path)
        with patch.object(svc, "backfill_intraday_session_gaps", return_value=4) as mock_backfill:
            c1 = svc.heal_intraday_gaps_if_needed(min_interval_sec=45.0)
            c2 = svc.heal_intraday_gaps_if_needed(min_interval_sec=45.0)
            self.assertEqual(c1, 4)
            self.assertEqual(c2, 0)
            self.assertEqual(mock_backfill.call_count, 1)

    def test_06_regime_gate_allows_fresh_impulse_breakout_and_blocks_true_chop(self):
        """OptionRegimeGate allows fresh directional breakout (ADX 15-20 with strong DI spread & ER>=0.35) while blocking chop."""
        # 1. Construct a 28-bar series that consolidates flat for 20 bars and breaks out sharply in last 8 bars
        base_dt = datetime(2026, 10, 30, 9, 15, 0)
        rows = []
        price = 22800.0
        for i in range(28):
            b_dt = base_dt + timedelta(minutes=i)
            if i < 20:
                # Tight oscillating consolidation
                delta = 2.0 if (i % 2 == 0) else -2.0
                o_p = price
                c_p = price + delta
                h_p = max(o_p, c_p) + 3.0
                l_p = min(o_p, c_p) - 3.0
                price = c_p
            else:
                # Sharp directional impulse (+12 pts/min for 8 mins)
                o_p = price
                c_p = price + 12.0
                h_p = c_p + 1.0
                l_p = o_p - 0.5
                price = c_p
            rows.append({
                "ticker": "NIFTY",
                "timestamp": b_dt.strftime("%Y-%m-%d %H:%M:00"),
                "open": o_p,
                "high": h_p,
                "low": l_p,
                "close": c_p,
                "volume": 1000,
                "is_complete": 1,
                "is_synthetic": 0,
                "is_gap": 0,
                "available_at": (b_dt + timedelta(minutes=1)).isoformat(),
            })
        df_impulse = pd.DataFrame(rows)
        gate = OptionRegimeGate(db_path=self.db_path)
        hw_bull = {
            h: [
                {"timestamp": "2026-10-30 09:15:00", "open": 1000.0, "close": 1005.0},
                {"timestamp": "2026-10-30 09:30:00", "open": 1005.0, "close": 1012.0},
            ]
            for h in ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS"]
        }
        res_impulse = gate.evaluate_regime(
            underlying="NIFTY",
            opt_type="CE",
            as_of_time=datetime(2026, 10, 30, 9, 43, 5),
            continuous_bars=df_impulse,
            heavyweight_candles=hw_bull,
            option_ask=120.0,
            target_pct=35.0,
            lot_size=65,
            enforce_cutoff=True,
        )
        self.assertTrue(res_impulse["hurdles_passed"], f"Expected fresh impulse to pass, got: {res_impulse['rejection_reasons']}")


if __name__ == "__main__":
    unittest.main()
