import os
import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
from app.tasks.intraday_market_daemon import run_15m_market_tick_pipeline


class TestIntradayMarketDaemon(unittest.TestCase):

    @patch("app.data.data_gateway.DataGateway.is_market_open", return_value=False)
    def test_01_market_closed_returns_immediately_zero_activity(self, mock_is_open):
        """When market is closed, tick returns immediately with 0 work done."""
        res = run_15m_market_tick_pipeline(force_run=False)

        self.assertEqual(res["status"], "MARKET_CLOSED")
        self.assertFalse(res["is_market_open"])
        self.assertIn("sleeping", res["message"].lower())

    @patch("app.data.data_gateway.DataGateway.is_market_open", return_value=True)
    @patch("app.analytics.universe_config.resolve_universe_tickers", return_value=["RELIANCE", "TCS"])
    @patch("app.tasks.intraday_market_daemon.resolve_fno_universe", return_value={"symbols": ["RELIANCE", "TCS"]})
    @patch("app.data.data_gateway.DataGateway.get_ohlcv")
    @patch("app.data.data_gateway.DataGateway.persist_intraday_candles", return_value=1)
    @patch("app.tasks.intraday_market_daemon.get_qlib_adapter")
    @patch("app.tasks.intraday_market_daemon.QlibVirtualTracker.reconcile_open_recommendations", return_value=2)
    def test_02_market_open_triggers_hoarding_and_reconciliation(
        self,
        mock_reconcile,
        mock_qlib_adapter,
        mock_persist,
        mock_get_ohlcv,
        mock_fno_uni,
        mock_uni_tickers,
        mock_is_open
    ):
        """When market is open, hoards 15m candles, syncs Qlib binary cache, and reconciles trades."""
        mock_df = pd.DataFrame({
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1000, 2000]
        })
        mock_get_ohlcv.return_value = mock_df

        mock_adapter_inst = MagicMock()
        mock_adapter_inst.sync_intraday_universe.return_value = {"synced": True, "symbols_updated": 2}
        mock_qlib_adapter.return_value = mock_adapter_inst

        res = run_15m_market_tick_pipeline(force_run=True)

        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(res["is_market_open"])
        self.assertEqual(res["symbols_scanned"], 2)
        self.assertEqual(res["symbols_hoarded"], 2)
        self.assertTrue(res["qlib_synced"])
        self.assertEqual(res["reconciled_trades"], 2)
        mock_persist.assert_called()
        mock_adapter_inst.sync_intraday_universe.assert_called_once()

    @patch("app.data.data_gateway.DataGateway.is_market_open", return_value=True)
    @patch("app.analytics.universe_config.resolve_universe_tickers", return_value=["ERROR_TICKER"])
    @patch("app.tasks.intraday_market_daemon.resolve_fno_universe", return_value={"symbols": ["ERROR_TICKER"]})
    @patch("app.data.data_gateway.DataGateway.get_ohlcv", side_effect=RuntimeError("Data gateway timeout"))
    @patch("app.tasks.intraday_market_daemon.get_qlib_adapter", side_effect=RuntimeError("Binary sync error"))
    def test_03_defensive_error_resilience(self, mock_adapter, mock_get_ohlcv, mock_fno_uni, mock_uni_tickers, mock_is_open):
        """Pipeline must handle network/gateway exceptions gracefully without crashing."""
        res = run_15m_market_tick_pipeline(force_run=True)

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["symbols_hoarded"], 0)
        self.assertFalse(res["qlib_synced"])

    @patch("app.data.database.is_testing_environment", return_value=False)
    def test_04_scheduler_job_registration(self, mock_test_env):
        """Verify the 15m daemon job is registered in the FastAPI app scheduler."""
        import app.main as main_mod
        old_inst = main_mod._SCHEDULER_INSTANCE
        old_suppress = os.environ.get("SUPPRESS_SCHEDULER")
        try:
            if "SUPPRESS_SCHEDULER" in os.environ:
                del os.environ["SUPPRESS_SCHEDULER"]
            main_mod._SCHEDULER_INSTANCE = None
            sched = main_mod.get_or_create_scheduler()
            job_ids = [j.id for j in sched.get_jobs()]
            self.assertIn("market_hours_15m_hoard_oos", job_ids)
        finally:
            main_mod._SCHEDULER_INSTANCE = old_inst
            if old_suppress is not None:
                os.environ["SUPPRESS_SCHEDULER"] = old_suppress


if __name__ == '__main__':
    unittest.main()
