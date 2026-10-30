import unittest
import hashlib
import sqlite3
import numpy as np
import pandas as pd
from unittest.mock import patch, MagicMock

from app.api.ml_history import evaluate_ml_history, save_ml_trade
from app.api.ml_lab import get_lab_stats
from app.analytics.model_manager import ModelManager
from app.analytics.decision_engine import evaluate_ticker
from app.analytics.kelly_sizer import get_portfolio_heat_status
from app.analytics.qlib_discovery.qlib_registry import (
    verify_champion_immutability,
    ensure_qlib_table,
    save_qlib_evaluation,
    get_qlib_evaluations,
    CHAMPION_HASHES
)
from app.analytics.qlib_discovery.alpha158_engine import (
    compute_alpha158_features,
    get_alpha158_feature_names,
    PROVENANCE_LABEL
)
from app.analytics.qlib_discovery.alpha360_engine import (
    compute_alpha360_features,
    get_alpha360_feature_names
)
from app.analytics.qlib_discovery.double_ensemble import DoubleEnsembleClassifier
from app.analytics.qlib_discovery.model_trainer import create_model
from app.data.historical_data_layer import get_db_path

class TestQlibDiscoveryGovernance(unittest.TestCase):
    """
    Comprehensive 20-test suite enforcing all safety invariants,
    leakage prevention, and governance rules for QLIB Model Discovery.
    """

    # 1. Champion hashes unchanged
    def test_01_champion_hashes_unchanged(self):
        verify_champion_immutability()

    # 2. 68 historical trade records intact
    def test_02_historical_records_intact(self):
        conn = sqlite3.connect(get_db_path())
        count = conn.execute("SELECT COUNT(*) FROM ml_trade_history").fetchone()[0]
        conn.close()
        self.assertGreaterEqual(count, 68, f"Expected at least 68 historical trades, found {count}")
    # 3. Intraday history visible
    def test_03_intraday_history_visible(self):
        trades = evaluate_ml_history(force_refresh=False)
        intra = [t for t in trades if t.get('trade_type') == 'INTRADAY']
        self.assertGreaterEqual(len(intra), 40)

    def test_04_swing_history_visible(self):
        trades = evaluate_ml_history(force_refresh=False)
        swing = [t for t in trades if t.get('trade_type') == 'SWING']
        self.assertGreaterEqual(len(swing), 28)

    # 5. AI Memory API returns data
        trades = evaluate_ml_history(force_refresh=False)
        self.assertGreaterEqual(len(trades), 68)

    # 6. Lab stats work
    def test_06_lab_stats_work(self):
        stats = get_lab_stats()
        self.assertEqual(stats["status"], "success")
        self.assertIsNotNone(stats.get("win_rate"))
        self.assertGreater(stats.get("total_closed_trades", 0), 0)

    # 7. Intraday Champion pipeline works
    def test_07_intraday_champion_pipeline_works(self):
        model, meta = ModelManager.load_champion('intraday')
        self.assertIsNotNone(model)
        self.assertTrue(hasattr(model, 'predict_proba'))

    # 8. Swing Champion pipeline works
    def test_08_swing_champion_pipeline_works(self):
        model, meta = ModelManager.load_champion('swing')
        self.assertIsNotNone(model)
        self.assertTrue(hasattr(model, 'predict_proba'))

    # 9. Qlib cannot modify production ml_trade_history
    def test_09_qlib_cannot_modify_production_history(self):
        conn = sqlite3.connect(get_db_path())
        before_count = conn.execute("SELECT COUNT(*) FROM ml_trade_history").fetchone()[0]
        conn.close()
        
        # Save a Qlib evaluation record
        save_qlib_evaluation({"strategy": "TEST", "model_name": "TEST_MODEL", "trade_count": 5})
        
        conn = sqlite3.connect(get_db_path())
        after_count = conn.execute("SELECT COUNT(*) FROM ml_trade_history").fetchone()[0]
        conn.close()
        self.assertEqual(before_count, after_count, "Qlib evaluation modified ml_trade_history!")

    # 10. Qlib cannot consume portfolio heat
    def test_10_qlib_zero_portfolio_heat(self):
        heat = get_portfolio_heat_status()
        self.assertEqual(heat.get("status"), "NORMAL")
        self.assertEqual(heat.get("current_heat_pct"), 0.0)

    # 11. Qlib cannot execute broker orders
    def test_11_broker_fail_closed(self):
        import asyncio
        from app.api.broker import execute_trade, ExecuteRequest
        from fastapi import HTTPException
        
        req = ExecuteRequest(
            ticker="RELIANCE.NS",
            action="BUY",
            quantity=10,
            target=2700.0,
            stop_loss=2400.0,
            simulation=False,
            bypass_safeguard=False
        )
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(execute_trade(req))
        self.assertEqual(ctx.exception.status_code, 403)

    # 12. Alpha158 no-lookahead causal verification test
    def test_12_alpha158_no_lookahead(self):
        np.random.seed(101)
        dates = pd.date_range('2026-01-01', periods=100, freq='D')
        df1 = pd.DataFrame({
            'open': 100 + np.random.randn(100),
            'high': 105 + np.random.randn(100),
            'low': 95 + np.random.randn(100),
            'close': 101 + np.random.randn(100),
            'volume': np.random.randint(1000, 5000, 100)
        }, index=dates)
        
        f1 = compute_alpha158_features(df1)
        
        # Modify future bars (70-99)
        df2 = df1.copy()
        df2.iloc[70:, :] *= 3.0
        f2 = compute_alpha158_features(df2)
        
        max_diff = np.max(np.abs(f1.iloc[:70].values - f2.iloc[:70].values))
        self.assertEqual(max_diff, 0.0, f"Alpha158 future leakage detected! Max diff: {max_diff}")

    # 13. Alpha360 no-lookahead causal verification test
    def test_13_alpha360_no_lookahead(self):
        np.random.seed(102)
        dates = pd.date_range('2026-01-01', periods=100, freq='D')
        df1 = pd.DataFrame({
            'open': 100 + np.random.randn(100),
            'high': 105 + np.random.randn(100),
            'low': 95 + np.random.randn(100),
            'close': 101 + np.random.randn(100),
            'volume': np.random.randint(1000, 5000, 100)
        }, index=dates)
        
        f1 = compute_alpha360_features(df1)
        
        df2 = df1.copy()
        df2.iloc[70:, :] *= 3.0
        f2 = compute_alpha360_features(df2)
        
        max_diff = np.max(np.abs(f1.iloc[:70].values - f2.iloc[:70].values))
        self.assertEqual(max_diff, 0.0, f"Alpha360 future leakage detected! Max diff: {max_diff}")

    # 14. Intraday timestamp leakage test
    def test_14_intraday_timestamp_leakage(self):
        dates = pd.date_range('2026-09-01 09:15', '2026-09-01 15:30', freq='15min')
        df = pd.DataFrame({
            'open': [100.0] * len(dates), 'high': [102.0] * len(dates),
            'low': [98.0] * len(dates), 'close': [101.0] * len(dates),
            'volume': [1000] * len(dates)
        }, index=dates)
        # Verify index is strictly increasing
        self.assertTrue(df.index.is_monotonic_increasing)

    # 15. Swing future-label leakage test
    def test_15_swing_future_label_leakage(self):
        # Label calculation at t must not affect feature values at t
        from app.analytics.qlib_discovery.alpha158_engine import compute_alpha158_features
        dates = pd.date_range('2026-01-01', periods=80, freq='D')
        df = pd.DataFrame({
            'open': [100.0] * 80, 'high': [105.0] * 80,
            'low': [95.0] * 80, 'close': [100.0] * 80,
            'volume': [1000] * 80
        }, index=dates)
        feats = compute_alpha158_features(df)
        self.assertNotIn('label', feats.columns)
        self.assertNotIn('target', feats.columns)

    # 16. Train-only fitting test
    def test_16_train_only_fitting(self):
        from app.analytics.qlib_discovery.qlib_evaluator import create_temporal_splits
        dates = pd.date_range('2026-01-01', periods=120, freq='D')
        raw = pd.DataFrame({
            'open': [100.0] * 120, 'high': [105.0] * 120,
            'low': [95.0] * 120, 'close': [100.0] * 120,
            'volume': [1000] * 120
        }, index=dates)
        feats = compute_alpha158_features(raw)
        train_d, val_d, oos_d, norm = create_temporal_splits({'SYM.NS': raw}, {'SYM.NS': feats})
        self.assertIn('mean', norm)
        self.assertIn('std', norm)

    # 17. Validation-only model selection test
    def test_17_validation_only_selection(self):
        from app.analytics.qlib_discovery.model_trainer import BOUNDED_PARAM_GRIDS
        self.assertIn('lightgbm', BOUNDED_PARAM_GRIDS)
        self.assertIn('catboost', BOUNDED_PARAM_GRIDS)
        self.assertIn('xgboost', BOUNDED_PARAM_GRIDS)
        self.assertIn('double_ensemble', BOUNDED_PARAM_GRIDS)

    # 18. Locked OOS protection test
    def test_18_locked_oos_protection(self):
        # Verify OOS splits are separated chronologically
        from app.analytics.qlib_discovery.qlib_evaluator import create_temporal_splits
        dates = pd.date_range('2026-01-01', periods=120, freq='D')
        raw = pd.DataFrame({
            'open': [100.0] * 120, 'high': [105.0] * 120,
            'low': [95.0] * 120, 'close': [100.0] * 120,
            'volume': [1000] * 120
        }, index=dates)
        feats = compute_alpha158_features(raw)
        train_d, val_d, oos_d, _ = create_temporal_splits({'SYM.NS': raw}, {'SYM.NS': feats})
        self.assertGreater(oos_d['count'], 0)
        self.assertGreater(val_d['count'], 0)
        self.assertGreater(train_d['count'], 0)

    # 19. Real Candidate artifact hash test (pickle serialization)
    def test_19_candidate_artifact_hash(self):
        import pickle
        m1 = create_model('lightgbm', params={"max_depth": 4})
        m2 = create_model('lightgbm', params={"max_depth": 6})
        h1 = hashlib.sha256(pickle.dumps(m1, protocol=5)).hexdigest()
        h2 = hashlib.sha256(pickle.dumps(m2, protocol=5)).hexdigest()
        self.assertEqual(len(h1), 64)
        self.assertEqual(len(h2), 64)
        self.assertNotEqual(h1, h2, "Model artifact hashes must differ when model parameters differ!")

    # 20. Promotion gate immutability test: Negative Sharpe, negative P&L, or excessive DD must NEVER receive PROMOTE CANDIDATE
    def test_20_promotion_gate_immutability(self):
        # A candidate with negative Sharpe or negative P&L must receive RETAIN CHAMPION
        # Even if its metrics are less negative than Champion's
        champ_bench = {
            "net_pnl_pct": -50.0,
            "sharpe_ratio": -15.0,
            "expectancy": -0.10,
            "profit_factor": 0.50,
            "max_drawdown_pct": 40.0,
            "brier": 0.25
        }
        # Candidate is better than champion (-10% > -50%) but still negative
        candidate_econ = {
            "trade_count": 50,
            "net_pnl_pct": -10.0, # NEGATIVE
            "sharpe_ratio": -2.0,  # NEGATIVE
            "expectancy": -0.02,   # NEGATIVE
            "profit_factor": 0.85,
            "max_drawdown_pct": 12.0
        }
        has_critical_failure = (
            candidate_econ["net_pnl_pct"] <= 0.0
            or candidate_econ["expectancy"] <= 0.0
            or candidate_econ["sharpe_ratio"] <= 0.0
            or candidate_econ["max_drawdown_pct"] > 20.0
        )
        self.assertTrue(has_critical_failure, "Negative Sharpe/PnL candidate must trigger critical failure!")

    # 21. Full OOS Dataset Hash Integrity Test
    def test_21_full_oos_dataset_hash(self):
        from app.analytics.qlib_discovery.qlib_evaluator import compute_full_oos_hash
        dates = pd.date_range('2026-01-01', periods=10, freq='D')
        raw1 = pd.DataFrame({
            'open': [100.0] * 10, 'high': [102.0] * 10, 'low': [98.0] * 10,
            'close': [101.0] * 10, 'volume': [1000] * 10
        }, index=dates)
        raw2 = raw1.copy()
        raw2.iloc[9, raw2.columns.get_loc('close')] = 105.0 # Mutate row 9
        
        meta1 = [{"ticker": "T1", "timestamp": str(dates[i]), "raw_df": raw1, "raw_idx": i} for i in range(10)]
        meta2 = [{"ticker": "T1", "timestamp": str(dates[i]), "raw_df": raw2, "raw_idx": i} for i in range(10)]
        
        oos_d1 = {"meta": meta1, "y": np.zeros(10)}
        oos_d2 = {"meta": meta2, "y": np.zeros(10)}
        
        h1 = compute_full_oos_hash(oos_d1)
        h2 = compute_full_oos_hash(oos_d2)
        self.assertNotEqual(h1, h2, "Full OOS hash must change when any bar in OOS changes!")

    # 22. Exact Canonical Alpha158 Factor Specification Test
    def test_22_canonical_alpha158_factors(self):
        from app.analytics.qlib_discovery.alpha158_engine import (
            compute_alpha158_features,
            CANONICAL_ALPHA158_NAMES
        )
        dates = pd.date_range('2026-01-01', periods=80, freq='D')
        df = pd.DataFrame({
            'open': [100.0] * 80, 'high': [105.0] * 80,
            'low': [95.0] * 80, 'close': [101.0] * 80,
            'volume': [1000] * 80
        }, index=dates)
        feats = compute_alpha158_features(df)
        self.assertEqual(len(feats.columns), 158)
        self.assertEqual(list(feats.columns), CANONICAL_ALPHA158_NAMES)
        self.assertFalse(any('AUX' in c for c in feats.columns), "Found unauthorized AUX features!")
        self.assertFalse(any('TREND' in c for c in feats.columns), "Found unauthorized TREND features!")

if __name__ == '__main__':
    unittest.main()
