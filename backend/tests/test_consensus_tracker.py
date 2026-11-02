import os
import unittest
import sqlite3
import tempfile
from unittest.mock import patch, MagicMock

from app.analytics.consensus_tracker import ConsensusTracker


class TestConsensusTracker(unittest.TestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        # Patch get_db_path across modules
        self.patcher_db1 = patch("app.analytics.consensus_tracker.get_db_path", return_value=self.db_path)
        self.patcher_db2 = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.patcher_live = patch("app.data.market_provider.get_bulk_live_quotes_with_meta", return_value={})
        self.patcher_db1.start()
        self.patcher_db2.start()
        self.patcher_live.start()

        # Create schemas in temp DB
        conn = sqlite3.connect(self.db_path)
        from app.api.ml_history import ensure_ml_table
        ensure_ml_table()
        ConsensusTracker.init_consensus_tracking_schema(conn)

        # Create minimal ohlcv table for reconciliation tests
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ohlcv (
                ticker TEXT,
                timeframe TEXT,
                date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL
            );
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.patcher_db1.stop()
        self.patcher_db2.stop()
        self.patcher_live.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_01_schema_initialization(self):
        """Verify consensus_trade_history schema is initialized."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='consensus_trade_history'")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        conn.close()

        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        trade_data = {
            "ticker": "RELIANCE.NS",
            "symbol": "RELIANCE",
            "direction": "BUY",
            "blended_entry": 1250.0,
            "blended_sl": 1240.0,
            "blended_tp": 1270.0,
            "composite_score": 88.5,
            "risk_reward_ratio": 2.0,
            "qlib_metrics": {"score": 0.025},
            "legacy_metrics": {"confidence": 85.0, "sentiment_score": 0.65},
            "timestamp": f"{today} 10:00:00"
        }

        # 1. First save -> Success
        saved, reason = ConsensusTracker.save_consensus_trade(trade_data, strategy="INTRADAY")
        self.assertTrue(saved)
        self.assertEqual(reason, "SAVED")

        # Verify persisted row
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT ticker, direction, entry_price, status, composite_score FROM consensus_trade_history WHERE ticker='RELIANCE.NS'")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "RELIANCE.NS")
        self.assertEqual(row[1], "BUY")
        self.assertEqual(row[2], 1250.0)
        self.assertEqual(row[3], "OPEN")
        self.assertEqual(row[4], 88.5)
        conn.close()

        # 2. Duplicate save in same session -> Blocked
        saved2, reason2 = ConsensusTracker.save_consensus_trade(trade_data, strategy="INTRADAY")
        self.assertFalse(saved2)
        self.assertIn("DUPLICATE_SESSION_TRADE", reason2)

    @patch("app.analytics.consensus_tracker.send_telegram_message")
    def test_03_telegram_alert_dispatch(self, mock_send_tg):
        """Verify Telegram alert dispatch formatting."""
        trade_data = {
            "ticker": "TCS.NS",
            "direction": "BUY",
            "blended_entry": 2210.0,
            "blended_sl": 2190.0,
            "blended_tp": 2250.0,
            "composite_score": 92.0,
            "risk_reward_ratio": 2.0,
            "qlib_metrics": {"score": 0.035},
            "legacy_metrics": {"confidence": 88.0, "sentiment_score": 0.75}
        }

        sent = ConsensusTracker.dispatch_telegram_alert(trade_data, strategy="INTRADAY")
        self.assertTrue(sent)
        mock_send_tg.assert_called_once()
        msg = mock_send_tg.call_args[0][0]
        self.assertIn("TCS.NS", msg)
        self.assertIn("DUAL-ENGINE CONSENSUS", msg)
        self.assertIn("92.0%", msg)
        self.assertIn("₹2210.00", msg)

    def test_04_reconciliation_target_met(self):
        """Verify trade closes with TARGET MET when high reaches target."""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        trade_data = {
            "ticker": "INFY.NS",
            "direction": "BUY",
            "blended_entry": 1000.0,
            "blended_sl": 980.0,
            "blended_tp": 1040.0,
            "composite_score": 80.0,
            "timestamp": f"{today} 11:00:00"
        }
        ConsensusTracker.save_consensus_trade(trade_data, strategy="INTRADAY")

        # Insert a candle that crosses the target price (High = 1050 > 1040)
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES ('INFY.NS', '15m', '{today} 11:30:00', 1010, 1050, 1005, 1045, 50000);
        """)
        conn.commit()
        conn.close()

        reconciled = ConsensusTracker.reconcile_open_consensus_trades()
        self.assertEqual(reconciled, 1)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT status, outcome, profit_pct, exit_price FROM consensus_trade_history WHERE ticker='INFY.NS'")
        row = cur.fetchone()
        self.assertEqual(row[0], "CLOSED")
        self.assertEqual(row[1], "TARGET MET")
        self.assertEqual(row[2], 4.0) # (1040 - 1000) / 1000 * 100%
        self.assertEqual(row[3], 1040.0)
        conn.close()

    def test_05_reconciliation_sl_hit(self):
        """Verify trade closes with SL HIT when low breaches stop loss."""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        trade_data = {
            "ticker": "SBIN.NS",
            "direction": "BUY",
            "blended_entry": 800.0,
            "blended_sl": 780.0,
            "blended_tp": 840.0,
            "composite_score": 75.0,
            "timestamp": f"{today} 11:00:00"
        }
        ConsensusTracker.save_consensus_trade(trade_data, strategy="INTRADAY")

        # Insert a candle that breaches the stop loss (Low = 770 < 780)
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES ('SBIN.NS', '15m', '{today} 11:30:00', 790, 795, 770, 775, 40000);
        """)
        conn.commit()
        conn.close()

        reconciled = ConsensusTracker.reconcile_open_consensus_trades()
        self.assertEqual(reconciled, 1)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT status, outcome, profit_pct, exit_price FROM consensus_trade_history WHERE ticker='SBIN.NS'")
        row = cur.fetchone()
        self.assertEqual(row[0], "CLOSED")
        self.assertEqual(row[1], "SL HIT")
        self.assertEqual(row[2], -2.5) # (780 - 800) / 800 * 100%
        self.assertEqual(row[3], 780.0)
        conn.close()

    def test_06_performance_stats_aggregation(self):
        """Verify win rate and cumulative series calculation."""
        # Insert 1 win and 1 loss
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO consensus_trade_history (
                timestamp, ticker, symbol, strategy, direction,
                entry_price, stop_loss, target_price, composite_score,
                status, outcome, profit_pct, exit_price, exit_time
            ) VALUES 
            ('2026-09-15 10:00:00', 'TCS.NS', 'TCS', 'INTRADAY', 'BUY', 2000, 1960, 2080, 90, 'CLOSED', 'TARGET MET', 4.0, 2080, '2026-09-15 11:00:00'),
            ('2026-09-15 12:00:00', 'INFY.NS', 'INFY', 'INTRADAY', 'BUY', 1500, 1470, 1560, 85, 'CLOSED', 'SL HIT', -2.0, 1470, '2026-09-15 13:00:00');
        """)
        conn.commit()
        conn.close()

        stats = ConsensusTracker.get_consensus_performance_stats(days=30)
        self.assertEqual(stats["total_trades"], 2)
        self.assertEqual(stats["win_rate"], 50.0)
        self.assertEqual(stats["target_hits"], 1)
        self.assertEqual(stats["sl_hits"], 1)
        self.assertEqual(stats["total_profit_pct"], 2.0)
        self.assertEqual(len(stats["cumulative_series"]), 2)
        self.assertEqual(stats["cumulative_series"][-1]["cum_pnl"], 2.0)

    @patch("app.analytics.consensus_tracker.send_telegram_message")
    def test_07_dispatch_telegram_alert_with_finbert(self, mock_telegram):
        mock_telegram.return_value = True
        trade = {
            "ticker": "ICICIBANK.NS",
            "symbol": "ICICIBANK",
            "direction": "SELL",
            "blended_entry": 1352.2,
            "blended_sl": 1358.38,
            "blended_tp": 1351.58,
            "composite_score": 53.8,
            "risk_reward_ratio": 2.0,
            "qlib_metrics": {"score": 57.0, "raw_prediction": -0.0319},
            "legacy_metrics": {
                "confidence": 50.6,
                "explanation": {"sentiment_score": -0.35}
            }
        }
        res = ConsensusTracker.dispatch_telegram_alert(trade, strategy="INTRADAY")
        self.assertTrue(res)
        mock_telegram.assert_called_once()
        msg = mock_telegram.call_args[0][0]
        self.assertIn("ICICIBANK.NS", msg)
        self.assertIn("DUAL-ENGINE CONSENSUS SIGNAL", msg)
        self.assertIn("FinBERT Sentiment:", msg)
        self.assertIn("-0.35 (BEARISH)", msg)

    def test_08_reconciliation_with_live_quote_merge(self):
        """Verify trade closes with TARGET MET when live LTP exceeds target even if candle high is below target."""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        trade_data = {
            "ticker": "SUNTV.NS",
            "direction": "BUY",
            "blended_entry": 487.9,
            "blended_sl": 483.83,
            "blended_tp": 493.73,
            "composite_score": 50.5,
            "timestamp": f"{today} 09:15:00"
        }
        ConsensusTracker.save_consensus_trade(trade_data, strategy="INTRADAY")

        # Insert a 15m candle where high = 490 (below target 493.73) causal to trade entry
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"UPDATE consensus_trade_history SET timestamp = '{today} 09:15:00' WHERE ticker='SUNTV.NS';")
        conn.execute(f"""
            INSERT INTO ohlcv (ticker, timeframe, date, open, high, low, close, volume)
            VALUES ('SUNTV.NS', '15m', '{today} 09:30:00', 488, 490, 487, 489, 20000);
        """)
        conn.commit()
        conn.close()

        # Mock bulk live quote returning LTP 497.95 (blown past target 493.73)
        with patch("app.data.market_provider.get_bulk_live_quotes_with_meta", return_value={
            "SUNTV.NS": {"ticker": "SUNTV.NS", "price": 497.95, "is_realtime": True}
        }):
            sim_now = datetime.strptime(f"{today} 09:45:00", "%Y-%m-%d %H:%M:%S")
            reconciled = ConsensusTracker.reconcile_open_consensus_trades(now=sim_now)
            self.assertEqual(reconciled, 1)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT status, outcome, exit_price FROM consensus_trade_history WHERE ticker='SUNTV.NS'")
        row = cur.fetchone()
        self.assertEqual(row[0], "CLOSED")
        self.assertEqual(row[1], "TARGET MET")
        self.assertEqual(row[2], 493.73)
        conn.close()

    def test_09_reconciliation_throttle(self):
        """Verify min_interval_sec throttle prevents rapid redundant queries."""
        ConsensusTracker._last_reconcile_time = 0.0
        with patch("app.data.market_provider.get_bulk_live_quotes_with_meta") as mock_bulk:
            mock_bulk.return_value = {}
            # First call
            ConsensusTracker.reconcile_open_consensus_trades(min_interval_sec=10.0)
            # Immediate second call with min_interval_sec=10.0 should throttle and return 0
            second_res = ConsensusTracker.reconcile_open_consensus_trades(min_interval_sec=10.0)
            self.assertEqual(second_res, 0)


if __name__ == '__main__':
    unittest.main()


