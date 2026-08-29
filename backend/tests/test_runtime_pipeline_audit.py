import os
import sys
import unittest
import numpy as np
import pandas as pd
from datetime import datetime, time
from unittest.mock import patch, MagicMock

# Add backend directory to sys.path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.analytics.autonomous_bot import is_market_open, calculate_tightened_stop_loss
from app.analytics.model_manager import ModelManager
from app.analytics.foundation_models.timesfm_adapter import TimesFMAdapter
from app.analytics.foundation_models.chronos_adapter import ChronosAdapter
from app.analytics.telegram_notifier import send_telegram_message, get_db_path
from app.tasks.autopilot_scanner import is_autopilot_enabled

class TestRuntimePipelineAudit(unittest.TestCase):
    """
    Automated Test Suite for Runtime Pipeline Audit & Hardening.
    Verifies model loading, timezone safety, market-hours handling,
    intraday square-off guards, and Telegram dispatching.
    """

    # 1. Market closed on Saturday
    def test_market_closed_on_saturday(self):
        with patch('app.analytics.autonomous_bot.datetime') as mock_dt:
            # 2026-08-29 is a Saturday (weekday = 5)
            sat_dt = datetime(2026, 8, 29, 12, 0)
            mock_dt.now.return_value = sat_dt
            
            self.assertFalse(is_market_open())
            self.assertTrue(is_market_open(force_override=True))

    # 2. Market hours range Mon-Fri (09:15 to 15:30)
    def test_market_open_hours_range(self):
        with patch('app.analytics.autonomous_bot.datetime') as mock_dt:
            # 2026-08-31 is Monday at 10:30 IST (Open)
            mon_open = datetime(2026, 8, 31, 10, 30)
            mock_dt.now.return_value = mon_open
            self.assertTrue(is_market_open())

            # Monday at 16:00 IST (Closed)
            mon_closed = datetime(2026, 8, 31, 16, 0)
            mock_dt.now.return_value = mon_closed
            self.assertFalse(is_market_open())

    # 3. Champion model is always loaded and fitted (never None)
    def test_champion_model_always_loaded_and_fitted(self):
        m_intra, meta_intra = ModelManager.load_champion("intraday")
        self.assertIsNotNone(m_intra)
        self.assertTrue(hasattr(m_intra, "predict_proba"))
        
        m_swing, meta_swing = ModelManager.load_champion("swing")
        self.assertIsNotNone(m_swing)
        self.assertTrue(hasattr(m_swing, "predict_proba"))

    # 4. TimesFM timezone-aware index does not raise TypeError
    def test_timesfm_timezone_naive_conversion_no_crash(self):
        tfm = TimesFMAdapter()
        # Create timezone-aware pandas index (Asia/Kolkata)
        dates = pd.date_range(end='2026-08-29 15:30', periods=50, freq='15min', tz='Asia/Kolkata')
        df = pd.DataFrame({
            'open': np.linspace(100, 110, 50),
            'high': np.linspace(101, 111, 50),
            'low': np.linspace(99, 109, 50),
            'close': np.linspace(100.5, 110.5, 50),
            'volume': np.ones(50) * 1000
        }, index=dates)

        # Should execute point-in-time guard without TypeError
        as_of = datetime(2026, 8, 29, 16, 0)
        res = tfm.forecast("NTPC.NS", df, horizon_bars=1, timeframe="15m", as_of_time=as_of)
        self.assertIn(res.status, ["success", "unavailable", "insufficient_data"])

    # 5. Chronos timezone-aware index does not raise TypeError
    def test_chronos_timezone_naive_conversion_no_crash(self):
        chr_model = ChronosAdapter()
        dates = pd.date_range(end='2026-08-29 15:30', periods=50, freq='15min', tz='Asia/Kolkata')
        df = pd.DataFrame({
            'open': np.linspace(100, 110, 50),
            'high': np.linspace(101, 111, 50),
            'low': np.linspace(99, 109, 50),
            'close': np.linspace(100.5, 110.5, 50),
            'volume': np.ones(50) * 1000
        }, index=dates)

        as_of = datetime(2026, 8, 29, 16, 0)
        res = chr_model.forecast("NTPC.NS", df, horizon_bars=1, timeframe="15m", as_of_time=as_of)
        self.assertIn(res.status, ["success", "unavailable", "insufficient_data"])

    # 6. Stop loss tightening mathematics
    def test_stop_loss_tightening_math(self):
        # Bullish in profit (Entry 100, SL 95, Current 105 -> Breakeven at 100)
        tight_sl, cut_pct, mode = calculate_tightened_stop_loss("BULLISH", entry=100.0, sl=95.0, current_price=105.0)
        self.assertEqual(tight_sl, 100.0)
        self.assertEqual(cut_pct, 100.0)

        # Bullish in drawdown (Entry 100, SL 90, Current 95 -> Half risk cut to 95)
        tight_sl2, cut_pct2, mode2 = calculate_tightened_stop_loss("BULLISH", entry=100.0, sl=90.0, current_price=95.0)
        self.assertEqual(tight_sl2, 95.0)
        self.assertEqual(cut_pct2, 50.0)

    # 7. Telegram notifier handles missing / invalid credentials gracefully
    @patch('app.analytics.telegram_notifier.requests.post')
    def test_telegram_failure_tolerance(self, mock_post):
        mock_post.side_effect = Exception("Telegram API timeout")
        sent = send_telegram_message("Test message")
        self.assertFalse(sent)

    # 8. Autopilot enabled status reader
    def test_autopilot_enabled_status(self):
        enabled = is_autopilot_enabled()
        self.assertIsInstance(enabled, bool)

if __name__ == '__main__':
    unittest.main()
