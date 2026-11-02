"""
Unit tests for Rolling Empirical Probability Calibration.
Verifies that calibrator fits from canonical ml_trade_history and enforces monotonic safety.
"""

import unittest
import sqlite3
import tempfile
import os
import numpy as np
from unittest.mock import patch

from app.analytics.calibration import ProbabilityCalibrator


class TestRollingCalibration(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        self.temp_weights = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.weights_path = self.temp_weights.name
        self.temp_weights.close()

        self.patcher_weights = patch("app.analytics.calibration.WEIGHTS_PATH", self.weights_path)
        self.patcher_weights.start()

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ml_trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                ticker TEXT,
                confidence REAL,
                status TEXT,
                outcome TEXT,
                profit_pct REAL
            );
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.patcher_weights.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        if os.path.exists(self.weights_path):
            os.remove(self.weights_path)

    @patch("app.analytics.calibration.get_db_path")
    def test_fit_from_history_with_valid_correlation(self, mock_db_path):
        mock_db_path.return_value = self.db_path
        conn = sqlite3.connect(self.db_path)

        # Insert 40 trades with a realistic positive monotonic slope
        # High confidence (80%) -> Mostly wins
        # Low confidence (40%) -> Mostly losses
        for i in range(20):
            conn.execute("""
                INSERT INTO ml_trade_history (timestamp, ticker, confidence, status, outcome, profit_pct)
                VALUES (?, 'STOCK.NS', 80.0, 'CLOSED', 'TARGET MET', 2.5);
            """, (f"2026-09-10 10:{i:02d}:00",))
        for i in range(20):
            conn.execute("""
                INSERT INTO ml_trade_history (timestamp, ticker, confidence, status, outcome, profit_pct)
                VALUES (?, 'STOCK.NS', 40.0, 'CLOSED', 'SL HIT', -1.5);
            """, (f"2026-09-11 10:{i:02d}:00",))
        conn.commit()
        conn.close()

        cal = ProbabilityCalibrator()
        ok = cal.fit_from_history(max_trades=50)
        self.assertTrue(ok)
        self.assertTrue(cal.is_fitted)
        self.assertGreater(cal.coef, 0.01)

        # Test calibration output
        cal_pct, raw_pct, meta = cal.calibrate(80.0)
        self.assertEqual(meta["calibration_status"], "calibrated")
        self.assertGreater(cal_pct, 50.0)

    @patch("app.analytics.calibration.get_db_path")
    def test_defensive_disable_on_flat_or_inverted_data(self, mock_db_path):
        mock_db_path.return_value = self.db_path
        conn = sqlite3.connect(self.db_path)

        # Insert trades where confidence is inverted (high conf trades lose)
        for i in range(20):
            conn.execute("""
                INSERT INTO ml_trade_history (timestamp, ticker, confidence, status, outcome, profit_pct)
                VALUES (?, 'STOCK.NS', 90.0, 'CLOSED', 'SL HIT', -2.0);
            """, (f"2026-09-10 10:{i:02d}:00",))
        for i in range(20):
            conn.execute("""
                INSERT INTO ml_trade_history (timestamp, ticker, confidence, status, outcome, profit_pct)
                VALUES (?, 'STOCK.NS', 30.0, 'CLOSED', 'TARGET MET', 3.0);
            """, (f"2026-09-11 10:{i:02d}:00",))
        conn.commit()
        conn.close()

        cal = ProbabilityCalibrator()
        ok = cal.fit_from_history(max_trades=50)
        # Should reject non-monotonic slope
        self.assertFalse(ok)
        self.assertFalse(cal.is_fitted)


if __name__ == "__main__":
    unittest.main()
