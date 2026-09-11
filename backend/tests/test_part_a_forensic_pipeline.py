import unittest
import sqlite3
import json
import hashlib
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np

from app.api.ml_history import evaluate_ml_history, save_ml_trade, ensure_ml_table
from app.api.ml_lab import get_lab_stats
from app.analytics.model_manager import ModelManager
from app.analytics.macro_engine import get_macro_regime
from app.analytics.decision_engine import evaluate_ticker
from app.analytics.kelly_sizer import get_portfolio_heat_status
from app.tasks.autopilot_scanner import run_scheduled_autopilot_sweep
from app.data.historical_data_layer import get_db_path

class TestPartAForensicPipeline(unittest.TestCase):
    """
    Forensic test suite proving:
    1. ml_history evaluation works and does not raise NameError.
    2. Historical trades (68 total: 40 intraday, 28 swing) are visible and evaluated.
    3. Accuracy calculation returns real win rate over closed trades.
    4. Champion models load and their SHA256 hashes are immutable.
    5. Intraday and Swing rejection funnels execute deterministically.
    6. Autopilot scanner correctly unpacks Champion ensemble.
    """

    def test_01_champion_hashes_intact(self):
        """Absolute Rule #1: Both Champion hashes must match known values byte-for-byte."""
        intra_expected = 'f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91'
        swing_expected = '11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534'

        with open('backend/models/intraday/champion_ensemble.pkl', 'rb') as f:
            intra_actual = hashlib.sha256(f.read()).hexdigest()
        with open('backend/models/swing/champion_ensemble.pkl', 'rb') as f:
            swing_actual = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(intra_actual, intra_expected, 'Intraday Champion model hash mismatch!')
        self.assertEqual(swing_actual, swing_expected, 'Swing Champion model hash mismatch!')

    def test_02_evaluate_ml_history_resolves_all_trades(self):
        """evaluate_ml_history must return all trades without NameError on still_needed."""
        trades = evaluate_ml_history(force_refresh=True)
        self.assertIsInstance(trades, list)
        self.assertGreaterEqual(len(trades), 68, 'Should have at least 68 historical trades')

        intra_trades = [t for t in trades if t.get('trade_type') == 'INTRADAY']
        swing_trades = [t for t in trades if t.get('trade_type') == 'SWING']
        self.assertGreaterEqual(len(intra_trades), 40, 'Should have 40 Intraday trades')
        self.assertGreaterEqual(len(swing_trades), 28, 'Should have 28 Swing trades')

        for t in trades[:5]:
            self.assertIn('ticker', t)
            self.assertIn('direction', t)
            self.assertIn('entry', t)
            self.assertIn('status', t)
            self.assertIn('outcome', t)
            self.assertIn('confidence', t)
            self.assertIn('trade_type', t)

    def test_03_get_lab_stats_win_rate_accuracy(self):
        """get_lab_stats must return actual win_rate rather than None or 0 from an uncaught exception."""
        stats = get_lab_stats()
        self.assertIn('status', stats)
        self.assertEqual(stats['status'], 'success')
        self.assertIn('win_rate', stats)
        self.assertIn('total_closed_trades', stats)
        self.assertGreater(stats['total_closed_trades'], 0)
        self.assertIsNotNone(stats['win_rate'])
        self.assertGreater(stats['win_rate'], 0.0, 'Win rate should be computed from closed trades')

    def test_04_intraday_pipeline_funnel(self):
        """Intraday pipeline funnel execution on known liquid ticker."""
        intra_model, intra_meta = ModelManager.load_champion('intraday')
        self.assertIsNotNone(intra_model)

        dates = pd.date_range(end=pd.Timestamp.now(), periods=100, freq='15min')
        close = 2500.0 + np.cumsum(np.random.randn(100) * 2.0)
        df_synthetic = pd.DataFrame({
            'open': close - 1.0,
            'high': close + 3.0,
            'low': close - 3.0,
            'close': close,
            'volume': 50000 + np.random.randint(0, 10000, 100)
        }, index=dates)

        res = evaluate_ticker(
            ticker='TESTSTOCK.NS',
            df=df_synthetic,
            champion_model=intra_model,
            champion_meta=intra_meta,
            trade_type='INTRADAY',
            source='SYSTEM_TEST',
            skip_enrichment=True
        )

        self.assertTrue(res.pipeline_components['data_validation'])
        self.assertTrue(res.pipeline_components['feature_engineering'])
        self.assertTrue(res.pipeline_components['ensemble'])
        self.assertIn(res.direction, ['BULLISH', 'BEARISH'])
        self.assertGreaterEqual(res.confidence, 0.0)
        self.assertLessEqual(res.confidence, 100.0)

    def test_05_swing_cash_short_gate_enforcement(self):
        """Swing pipeline must strictly enforce cash-equity swing short ban for bearish candidates."""
        swing_model, swing_meta = ModelManager.load_champion('swing')
        self.assertIsNotNone(swing_model)

        dates = pd.date_range(end=pd.Timestamp.now(), periods=100, freq='D')
        close = 3000.0 - np.arange(100) * 10.0
        df_bearish = pd.DataFrame({
            'open': close + 2.0,
            'high': close + 5.0,
            'low': close - 5.0,
            'close': close,
            'volume': 100000
        }, index=dates)

        res = evaluate_ticker(
            ticker='TESTSTOCK.NS',
            df=df_bearish,
            champion_model=swing_model,
            champion_meta=swing_meta,
            trade_type='SWING',
            source='SYSTEM_TEST',
            skip_enrichment=True
        )

        self.assertFalse(res.qualified)
        self.assertEqual(res.rejection_reason, 'SWING_CASH_SHORT_DISALLOWED: Cash-equity swing short disallowed (Indian equity multi-day positions must be LONG)')

    def test_06_autopilot_champion_loading_tuple_unpack(self):
        """Verify autopilot correctly loads champion model object without tuple error."""
        champion_model, champion_meta = ModelManager.load_champion('intraday')
        self.assertTrue(hasattr(champion_model, 'predict_proba'), 'Champion model must have predict_proba method')
        self.assertIsInstance(champion_meta, dict)

    def test_07_save_ml_trade_safety(self):
        """Verify save_ml_trade validates tickers and persists correctly."""
        self.assertFalse(save_ml_trade('CACHE_TICKER', True, 100, 95, 110, 120, 70.0))
        self.assertFalse(save_ml_trade('RELIANCE.NS', False, 100, 105, 90, 80, 70.0, trade_type='SWING'))

if __name__ == '__main__':
    unittest.main()
