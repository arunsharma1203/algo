import unittest
import asyncio
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np

from app.qlib_engine.scanners import QlibScanners
from app.analytics.consensus_scanner import ConsensusScanner


class TestQlibOptionBAndConfluence(unittest.IsolatedAsyncioTestCase):
    """
    Validates Option B (relative alpha conviction + positive alpha hurdle)
    and expanded consensus candidate pool depth to prevent pipeline starvation.
    """

    @patch("app.qlib_engine.scanners.predict_with_qlib_model")
    @patch("app.qlib_engine.scanners.QlibModelRegistry.load_active_model")
    @patch("app.qlib_engine.scanners._get_active_adapter")
    @patch("app.qlib_engine.scanners.D")
    @patch("app.analytics.macro_engine.get_macro_regime")
    def test_option_b_positive_alpha_qualifies(
        self, mock_macro, mock_d, mock_adapter, mock_model_reg, mock_predict
    ):
        """
        Verify that realistic alpha return predictions (e.g. +0.0041) qualify
        under Option B and are not blocked by arbitrary static floors.
        """
        # Setup mock adapter & freshness
        adapter = MagicMock()
        adapter.check_freshness.return_value = {
            "data_status": "FRESH",
            "authoritative_latest_timestamp": "2026-09-18 15:15:00",
            "qlib_provider_latest_timestamp": "2026-09-18 15:15:00",
            "data_lag_minutes": 0,
            "is_market_open": False
        }
        adapter.feat_dir = "/tmp/mock_feat"
        adapter.normalize_symbol.side_effect = lambda s: (s.replace(".NS", ""), ".NS")
        mock_adapter.return_value = adapter

        # Setup mock model
        mock_model_reg.return_value = (
            MagicMock(),
            {"model_id": "test_model_v1", "artifact_sha256": "abcdef123456", "feature_manifest": None}
        )

        # Setup mock calendar & features using valid 15m date_range
        valid_cal = pd.date_range("2026-09-01 09:15:00", periods=60, freq="15min")
        mock_d.calendar.return_value = valid_cal

        # Mock predictions: HEG (+0.0041), SUMICHEM (+0.0011), ZERO (0.0000)
        mock_predict.return_value = {
            "HEG": 0.0041,
            "SUMICHEM": 0.0011,
            "FLAT": 0.0000
        }

        # Mock Alpha158 handler fetch
        with patch("qlib.contrib.data.handler.Alpha158") as mock_handler_cls:
            handler_inst = MagicMock()
            idx = pd.MultiIndex.from_tuples([("2026-09-18 15:00:00", "HEG"), ("2026-09-18 15:00:00", "SUMICHEM"), ("2026-09-18 15:00:00", "FLAT")])
            handler_inst.fetch.return_value = pd.DataFrame(
                {"feature_1": [1.0, 2.0, 3.0]}, index=idx
            )
            mock_handler_cls.return_value = handler_inst

            # Mock D.features for price lookup (indexed by instrument, datetime)
            price_idx = pd.MultiIndex.from_tuples(
                [("HEG", "2026-09-18 15:00:00"), ("SUMICHEM", "2026-09-18 15:00:00"), ("FLAT", "2026-09-18 15:00:00")],
                names=["instrument", "datetime"]
            )
            mock_d.features.return_value = pd.DataFrame(
                {"$close": [2450.0, 480.0, 100.0]}, index=price_idx
            )

            # Macro regime: Bullish intraday
            mock_macro.return_value = {
                "intraday_regime_verdict": "INTRADAY_BULL_MOMENTUM",
                "nifty_trend_short": "BULLISH"
            }

            with patch("app.analytics.macro_engine.is_direction_permitted", return_value=(True, "Permitted")):
                res = QlibScanners.run_intraday_scan(
                    tickers=["HEG.NS", "SUMICHEM.NS", "FLAT.NS"],
                    top_k=5,
                    min_score=0.0
                )

        self.assertEqual(res["status"], "SUCCESS")
        recs = res.get("recommendations", [])
        rec_tickers = [r["ticker"] for r in recs]

        # HEG (+0.0041) and SUMICHEM (+0.0011) must qualify
        self.assertIn("HEG.NS", rec_tickers)
        self.assertIn("SUMICHEM.NS", rec_tickers)

        # FLAT (0.0000) must be rejected
        self.assertNotIn("FLAT.NS", rec_tickers)

        # HEG must be #1 due to higher absolute conviction
        self.assertEqual(recs[0]["ticker"], "HEG.NS")
        self.assertEqual(recs[0]["direction"], "BUY")
        self.assertGreater(recs[0]["confidence"], 50.0)

    @patch("app.qlib_engine.scanners.predict_with_qlib_model")
    @patch("app.qlib_engine.scanners.QlibModelRegistry.load_active_model")
    @patch("app.qlib_engine.scanners._get_active_adapter")
    @patch("app.qlib_engine.scanners.D")
    @patch("app.analytics.macro_engine.get_macro_regime")
    def test_option_b_custom_min_score_filter(
        self, mock_macro, mock_d, mock_adapter, mock_model_reg, mock_predict
    ):
        """
        Verify that passing a custom min_score hurdle (e.g. 0.002) filters
        lower-edge candidates while letting higher-edge candidates pass.
        """
        adapter = MagicMock()
        adapter.check_freshness.return_value = {
            "data_status": "FRESH",
            "authoritative_latest_timestamp": "2026-09-18 15:15:00",
            "qlib_provider_latest_timestamp": "2026-09-18 15:15:00",
            "data_lag_minutes": 0,
            "is_market_open": False
        }
        adapter.feat_dir = "/tmp/mock_feat"
        adapter.normalize_symbol.side_effect = lambda s: (s.replace(".NS", ""), ".NS")
        mock_adapter.return_value = adapter

        mock_model_reg.return_value = (
            MagicMock(),
            {"model_id": "test_model_v1", "artifact_sha256": "abcdef123456", "feature_manifest": None}
        )
        mock_d.calendar.return_value = pd.date_range("2026-09-01 09:15:00", periods=60, freq="15min")

        mock_predict.return_value = {
            "HEG": 0.0041,
            "SUMICHEM": 0.0011
        }

        with patch("qlib.contrib.data.handler.Alpha158") as mock_handler_cls:
            handler_inst = MagicMock()
            idx = pd.MultiIndex.from_tuples([("2026-09-18 15:00:00", "HEG"), ("2026-09-18 15:00:00", "SUMICHEM")])
            handler_inst.fetch.return_value = pd.DataFrame({"feat": [1.0, 2.0]}, index=idx)
            mock_handler_cls.return_value = handler_inst

            price_idx = pd.MultiIndex.from_tuples(
                [("HEG", "2026-09-18 15:00:00"), ("SUMICHEM", "2026-09-18 15:00:00")],
                names=["instrument", "datetime"]
            )
            mock_d.features.return_value = pd.DataFrame({"$close": [2450.0, 480.0]}, index=price_idx)

            mock_macro.return_value = {"intraday_regime_verdict": "INTRADAY_BULL_MOMENTUM"}

            with patch("app.analytics.macro_engine.is_direction_permitted", return_value=(True, "Permitted")):
                res = QlibScanners.run_intraday_scan(
                    tickers=["HEG.NS", "SUMICHEM.NS"],
                    top_k=5,
                    min_score=0.002  # Demands 20 bps edge
                )

        recs = res.get("recommendations", [])
        rec_tickers = [r["ticker"] for r in recs]

        # HEG (+0.0041 > 0.002) qualifies
        self.assertIn("HEG.NS", rec_tickers)
        # SUMICHEM (+0.0011 <= 0.002) is rejected by custom threshold
        self.assertNotIn("SUMICHEM.NS", rec_tickers)

    @patch("app.analytics.consensus_scanner.QlibScanners.run_intraday_scan")
    @patch("app.analytics.consensus_scanner.SmartScannerPipeline.run_sweep_batch")
    async def test_consensus_candidate_pool_depth_expansion(self, mock_legacy, mock_qlib):
        """
        Verify that Consensus sweeps query an expanded candidate pool (top_k=50)
        from Qlib to prevent pre-truncation starvation, while final results slice to top_k=5.
        """
        # Qlib produces 10 candidates
        qlib_opps = [
            {
                "ticker": f"STOCK_{i}.NS",
                "direction": "BUY",
                "entry_price": 100.0 + i,
                "stop_loss": 98.0 + i,
                "target_price": 104.0 + i,
                "score": 0.50 + (i * 0.01),
                "rank": i + 1
            }
            for i in range(10)
        ]
        mock_qlib.return_value = {"status": "SUCCESS", "opportunities": qlib_opps}

        # Legacy also produces matching candidates
        legacy_cands = [
            {
                "ticker": f"STOCK_{i}.NS",
                "direction": "BULLISH",
                "is_bullish": True,
                "entry": 100.0 + i,
                "sl": 98.0 + i,
                "tp1": 104.0 + i,
                "confidence": 65.0
            }
            for i in range(10)
        ]
        mock_legacy.return_value = {"status": "SUCCESS", "candidates": legacy_cands}

        res = await ConsensusScanner.run_consensus_scan(strategy="INTRADAY", universe="NIFTY_500", top_k=5)

        # 1. Verify Qlib was queried with expanded pool (confluence_pool_k = 50)
        mock_qlib.assert_called_once()
        _, kwargs = mock_qlib.call_args
        self.assertEqual(kwargs.get("top_k"), 50)

        # 2. Verify output is strictly sliced to top_k (5)
        self.assertEqual(len(res["unanimous_trades"]), 5)
        self.assertEqual(len(res["consensus_trades"]), 5)
        self.assertEqual(res["summary"]["unanimous_count"], 10)


if __name__ == "__main__":
    unittest.main()
