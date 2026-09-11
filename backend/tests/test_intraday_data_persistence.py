"""
TEST SUITE: MILESTONE 6 — INTRADAY DATA PERSISTENCE
===================================================
Verifies canonical DataGateway 15m candle persistence, deduplication,
incremental updates, complete non-interference with historical daily 1d data,
and transparent cache reads.
"""

import unittest
import tempfile
import sqlite3
import os
import shutil
import pandas as pd
from datetime import datetime, timedelta

from app.data.database import set_test_db_override
from app.data.historical_data_layer import HistoricalDataLayer
from app.data.data_gateway import DataGateway


class TestIntradayDataPersistence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_market_data.db")
        set_test_db_override(self.db_path)
        HistoricalDataLayer.init_schema()

    def tearDown(self):
        set_test_db_override(None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _generate_sample_15m_df(self, count=10, start_dt=None):
        if start_dt is None:
            start_dt = datetime(2026, 9, 11, 9, 15)
        dates = [start_dt + timedelta(minutes=15 * i) for i in range(count)]
        data = {
            "open": [100.0 + i for i in range(count)],
            "high": [105.0 + i for i in range(count)],
            "low": [98.0 + i for i in range(count)],
            "close": [102.0 + i for i in range(count)],
            "volume": [1000 * (i + 1) for i in range(count)]
        }
        df = pd.DataFrame(data, index=dates)
        df.index.name = "datetime"
        return df

    def test_persist_intraday_candles_schema_and_columns(self):
        """Verifies that 15m candles are saved with ticker, date, OHLCV, timeframe, source, and hoard_timestamp."""
        df = self._generate_sample_15m_df(5)
        saved = DataGateway.persist_intraday_candles("INFY.NS", df, timeframe="15m", source="yfinance")
        self.assertEqual(saved, 5)

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        rows = c.execute("""
            SELECT ticker, date, open, high, low, close, volume, timeframe, source, hoard_timestamp
            FROM ohlcv WHERE ticker = 'INFY.NS' AND timeframe = '15m'
            ORDER BY date ASC
        """).fetchall()
        conn.close()

        self.assertEqual(len(rows), 5)
        first_row = rows[0]
        self.assertEqual(first_row[0], "INFY.NS")
        self.assertEqual(first_row[1], "2026-09-11 09:15:00")
        self.assertEqual(first_row[2], 100.0)
        self.assertEqual(first_row[7], "15m")
        self.assertEqual(first_row[8], "yfinance")
        self.assertTrue(bool(first_row[9]), "hoard_timestamp must be recorded")

    def test_persist_intraday_candles_deduplication(self):
        """Persisting the exact same 15m candles twice must update/replace without duplicating rows."""
        df = self._generate_sample_15m_df(5)
        DataGateway.persist_intraday_candles("TCS.NS", df, timeframe="15m")
        # Persist same candles again
        saved_again = DataGateway.persist_intraday_candles("TCS.NS", df, timeframe="15m")
        self.assertEqual(saved_again, 5)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM ohlcv WHERE ticker = 'TCS.NS' AND timeframe = '15m'").fetchone()[0]
        conn.close()

        self.assertEqual(count, 5, "Duplicate candles must be deduplicated")

    def test_incremental_update_adds_new_candles(self):
        """Adding initial batch then newer candles incrementally increases row count."""
        start_time = datetime(2026, 9, 11, 9, 15)
        batch1 = self._generate_sample_15m_df(count=4, start_dt=start_time)
        DataGateway.persist_intraday_candles("RELIANCE.NS", batch1, timeframe="15m")

        # Second batch with 2 overlapping and 3 new
        overlap_time = start_time + timedelta(minutes=30)
        batch2 = self._generate_sample_15m_df(count=5, start_dt=overlap_time)
        DataGateway.persist_intraday_candles("RELIANCE.NS", batch2, timeframe="15m")

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM ohlcv WHERE ticker = 'RELIANCE.NS' AND timeframe = '15m'").fetchone()[0]
        conn.close()

        # 4 initial (0, 15, 30, 45) + 3 new (60, 75, 90) = 7 distinct candles
        self.assertEqual(count, 7)

    def test_daily_ohlcv_unaltered_by_intraday_persistence(self):
        """Persisting 15m intraday data must NEVER modify or overwrite 1d daily candles."""
        # Insert mock daily candles
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO ohlcv (ticker, date, open, high, low, close, volume, timeframe, source, hoard_timestamp)
            VALUES ('HDFCBANK.NS', '2026-09-10', 1600.0, 1620.0, 1590.0, 1610.0, 50000, '1d', 'historical', '2026-09-10T18:00:00')
        """)
        conn.commit()
        conn.close()

        # Persist 15m intraday data for same ticker
        df_15m = self._generate_sample_15m_df(count=5, start_dt=datetime(2026, 9, 11, 9, 15))
        DataGateway.persist_intraday_candles("HDFCBANK.NS", df_15m, timeframe="15m")

        # Verify daily candle is intact and unchanged
        conn = sqlite3.connect(self.db_path)
        daily_rows = conn.execute("SELECT date, close, timeframe FROM ohlcv WHERE ticker = 'HDFCBANK.NS' AND timeframe = '1d'").fetchall()
        intraday_count = conn.execute("SELECT COUNT(*) FROM ohlcv WHERE ticker = 'HDFCBANK.NS' AND timeframe = '15m'").fetchone()[0]
        conn.close()

        self.assertEqual(len(daily_rows), 1)
        self.assertEqual(daily_rows[0][0], "2026-09-10")
        self.assertEqual(daily_rows[0][1], 1610.0)
        self.assertEqual(intraday_count, 5)

    def test_datagateway_get_ohlcv_15m_reads_cache_transparently(self):
        """DataGateway.get_ohlcv retrieves 15m candles from SQLite cache."""
        df_15m = self._generate_sample_15m_df(count=8)
        DataGateway.persist_intraday_candles("SBIN.NS", df_15m, timeframe="15m")

        # Retrieve via DataGateway with fetch_incremental=False to isolate cache
        retrieved_df = DataGateway.get_ohlcv("SBIN.NS", timeframe="15m", use_cache=True, fetch_incremental=False, min_rows=5)
        self.assertFalse(retrieved_df.empty)
        self.assertEqual(len(retrieved_df), 8)
        self.assertIn("close", retrieved_df.columns)
        self.assertIn("volume", retrieved_df.columns)


if __name__ == "__main__":
    unittest.main()

