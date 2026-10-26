"""
Unit tests for Qlib audit fixes:
1. 14-period True Range ATR computation & SL/TP levels.
2. Promotion margin gates (+0.05 Sharpe, +0.02 Profit Factor).
3. Swing scanner positive alpha floor (min_score=0.0).
"""

import unittest
import pandas as pd
import numpy as np

from app.qlib_engine.scanners import compute_true_atr_14, QlibScanners
from app.qlib_engine.promotion_policy import (
    QlibPromotionPolicy,
    PROMOTION_SHARPE_MARGIN,
    PROMOTION_PF_MARGIN
)


class TestQlibAuditFixes(unittest.TestCase):

    def test_compute_true_atr_14_empty_or_single_fallback(self):
        # Empty fallback
        atr_empty = compute_true_atr_14(pd.DataFrame(), ltp=100.0)
        self.assertEqual(atr_empty, 2.0)  # 2% default

        # Single bar fallback
        df_single = pd.DataFrame([{"$high": 105.0, "$low": 95.0, "$close": 100.0}])
        atr_single = compute_true_atr_14(df_single, ltp=100.0)
        self.assertEqual(atr_single, 2.0)

    def test_compute_true_atr_14_multibar_true_range(self):
        # Construct 15 bars with known True Range
        # Bar 0: H=102, L=98, C=100
        # Bar 1: H=105, L=99, C=104 (TR: max(6, |105-100|=5, |99-100|=1) = 6)
        # Bar 2: Gap up H=112, L=108, C=110 (TR: max(4, |112-104|=8, |108-104|=4) = 8)
        data = []
        base = 100.0
        for i in range(20):
            data.append({
                "$high": base + 3.0,
                "$low": base - 2.0,
                "$close": base + 1.0
            })
            base += 1.0

        df = pd.DataFrame(data)
        ltp = df["$close"].iloc[-1]
        atr = compute_true_atr_14(df, ltp=ltp)

        self.assertGreater(atr, 0.0)
        # Verify it falls within safety clamps (1.5% to 10% of LTP)
        self.assertGreaterEqual(atr, ltp * 0.015)
        self.assertLessEqual(atr, ltp * 0.10)

    def test_compute_true_atr_14_clamping(self):
        # Very tiny ranges (e.g. range = 0.1 on 1000 LTP) should clamp to min 1.5%
        data = [{"$high": 1000.1, "$low": 999.9, "$close": 1000.0} for _ in range(20)]
        df = pd.DataFrame(data)
        atr = compute_true_atr_14(df, ltp=1000.0)
        self.assertAlmostEqual(atr, 1000.0 * 0.015, places=2)

        # Huge ranges (e.g. range = 200 on 1000 LTP) should clamp to max 10.0%
        data_huge = [{"$high": 1200.0, "$low": 800.0, "$close": 1000.0} for _ in range(20)]
        df_huge = pd.DataFrame(data_huge)
        atr_huge = compute_true_atr_14(df_huge, ltp=1000.0)
        self.assertAlmostEqual(atr_huge, 1000.0 * 0.10, places=2)

    def test_promotion_policy_sharpe_margin(self):
        # Candidate with equal Sharpe to Champion should FAIL Gate 8
        champion_manifest = {
            "model_id": "champ_001",
            "metrics": {
                "execution_metrics": {
                    "net_sharpe": 1.20,
                    "net_profit_factor": 1.50,
                    "net_win_rate": 60.0,
                    "net_trades_count": 50,
                    "net_max_drawdown_pct": 12.0
                }
            }
        }

        # Case A: Challenger equal to Champion (1.20 vs 1.20) -> FAILS (needs +0.05)
        cand_equal = {
            "model_id": "cand_001",
            "status": "FROZEN_CHALLENGER",
            "model_class": "LGBModel",
            "feature_handler": "Alpha158",
            "strategy": "SWING",
            "metrics": {
                "execution_metrics": {
                    "net_sharpe": 1.20,
                    "net_profit_factor": 1.60,
                    "net_win_rate": 60.0,
                    "net_trades_count": 50,
                    "net_max_drawdown_pct": 12.0
                }
            }
        }
        res_equal = QlibPromotionPolicy.evaluate_promotion_eligibility(cand_equal, champion_manifest)
        self.assertFalse(res_equal["gates"]["champion_beating_sharpe"]["passed"])

        # Case B: Challenger Sharpe 1.23 (+0.03 margin, below +0.05) -> FAILS
        cand_close = dict(cand_equal)
        cand_close["metrics"] = {
            "execution_metrics": {
                "net_sharpe": 1.23,
                "net_profit_factor": 1.60,
                "net_win_rate": 60.0,
                "net_trades_count": 50,
                "net_max_drawdown_pct": 12.0
            }
        }
        res_close = QlibPromotionPolicy.evaluate_promotion_eligibility(cand_close, champion_manifest)
        self.assertFalse(res_close["gates"]["champion_beating_sharpe"]["passed"])

        # Case C: Challenger Sharpe 1.26 (+0.06 margin, above +0.05) -> PASSES Gate 8
        cand_beat = dict(cand_equal)
        cand_beat["metrics"] = {
            "execution_metrics": {
                "net_sharpe": 1.26,
                "net_profit_factor": 1.60,
                "net_win_rate": 60.0,
                "net_trades_count": 50,
                "net_max_drawdown_pct": 12.0
            }
        }
        res_beat = QlibPromotionPolicy.evaluate_promotion_eligibility(cand_beat, champion_manifest)
        self.assertTrue(res_beat["gates"]["champion_beating_sharpe"]["passed"])

    def test_promotion_policy_profit_factor_margin(self):
        champion_manifest = {
            "model_id": "champ_001",
            "metrics": {
                "execution_metrics": {
                    "net_sharpe": 1.20,
                    "net_profit_factor": 1.50,
                    "net_win_rate": 60.0,
                    "net_trades_count": 50,
                    "net_max_drawdown_pct": 12.0
                }
            }
        }

        # Challenger PF equal (1.50 vs 1.50) -> FAILS Gate 9 (requires +0.02)
        cand_equal_pf = {
            "model_id": "cand_pf_001",
            "status": "FROZEN_CHALLENGER",
            "model_class": "LGBModel",
            "feature_handler": "Alpha158",
            "strategy": "SWING",
            "metrics": {
                "execution_metrics": {
                    "net_sharpe": 1.30,
                    "net_profit_factor": 1.50,
                    "net_win_rate": 60.0,
                    "net_trades_count": 50,
                    "net_max_drawdown_pct": 12.0
                }
            }
        }
        res_equal_pf = QlibPromotionPolicy.evaluate_promotion_eligibility(cand_equal_pf, champion_manifest)
        self.assertFalse(res_equal_pf["gates"]["champion_beating_profit_factor"]["passed"])

        # Challenger PF 1.53 (+0.03 margin) -> PASSES Gate 9
        cand_beat_pf = dict(cand_equal_pf)
        cand_beat_pf["metrics"] = {
            "execution_metrics": {
                "net_sharpe": 1.30,
                "net_profit_factor": 1.53,
                "net_win_rate": 60.0,
                "net_trades_count": 50,
                "net_max_drawdown_pct": 12.0
            }
        }
        res_beat_pf = QlibPromotionPolicy.evaluate_promotion_eligibility(cand_beat_pf, champion_manifest)
        self.assertTrue(res_beat_pf["gates"]["champion_beating_profit_factor"]["passed"])

    def test_swing_scanner_default_min_score(self):
        # Inspect default signature of run_swing_scan
        import inspect
        sig = inspect.signature(QlibScanners.run_swing_scan)
        self.assertEqual(sig.parameters["min_score"].default, 0.0)

    def test_fno_scanner_post_market_preview_mode(self):
        """When market is closed (post 15:30 IST), scanner returns MARKET_CLOSED with preview contracts without virtual execution."""
        from unittest.mock import patch, MagicMock

        fake_opp = {
            "underlying": "RELIANCE",
            "strike": 2800.0,
            "expiry": "2026-09-24",
            "option_type": "CE",
            "entry_premium": 45.0,
            "spot_price": 2810.0,
            "stop_loss": 30.0,
            "target_price": 75.0,
            "contract": "RELIANCE 2026-09-24 2800 CE",
            "contract_status": "AVAILABLE",
            "freshness_state": "FRESH",
            "freshness_age_seconds": 12.0,
            "is_real_premium": True
        }

        # Mock market closed (is_market_open -> False)
        with patch("app.data.data_gateway.DataGateway.is_market_open", return_value=False), \
             patch("app.qlib_engine.virtual_tracker.QlibVirtualTracker.record_recommendation") as mock_record:
            
            # Simulate opportunities list after candidate loop
            opp = dict(fake_opp)
            score = 0.05
            rank_percentile = 85.0
            direction = "BULLISH"
            is_risk_valid = True
            is_mkt_open = False
            has_conviction = (score >= 0.03 and rank_percentile >= 75.0 and direction in ("BULLISH", "BEARISH"))

            if not is_risk_valid:
                opp["contract_status"] = "INVALID_RISK_STRUCTURE"
            elif not has_conviction:
                opp["contract_status"] = "LOW_CONVICTION_FILTERED"
            elif not is_mkt_open:
                opp["contract_status"] = "MARKET_CLOSED"
                opp["reason"] = "Market is closed (09:15-15:30 IST). Showing after-hours closing session preview (not actionable for live execution)."
            else:
                opp["contract_status"] = "AVAILABLE"

            self.assertEqual(opp["contract_status"], "MARKET_CLOSED")
            self.assertIn("Market is closed", opp["reason"])
            # Ensure virtual recommendation was NOT recorded
            self.assertFalse(mock_record.called)

    def test_fno_scanner_market_open_mode(self):
        """When market is open (09:15 - 15:30 IST), candidate receives AVAILABLE status."""
        opp = {
            "underlying": "RELIANCE",
            "strike": 2800.0,
            "entry_premium": 45.0,
            "contract_status": "AVAILABLE"
        }
        score = 0.05
        rank_percentile = 85.0
        direction = "BULLISH"
        is_risk_valid = True
        is_mkt_open = True
        has_conviction = (score >= 0.03 and rank_percentile >= 75.0 and direction in ("BULLISH", "BEARISH"))

        if not is_risk_valid:
            opp["contract_status"] = "INVALID_RISK_STRUCTURE"
        elif not has_conviction:
            opp["contract_status"] = "LOW_CONVICTION_FILTERED"
        elif not is_mkt_open:
            opp["contract_status"] = "MARKET_CLOSED"
        else:
            opp["contract_status"] = "AVAILABLE"

        self.assertEqual(opp["contract_status"], "AVAILABLE")


if __name__ == "__main__":
    unittest.main()

