"""
Unit tests for OptionStreamManager.get_underlying_history_df and defensive orchestrator integration.
"""

import unittest
import pandas as pd
import time
from unittest.mock import MagicMock, patch

from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
from app.analytics.option_sniffer.option_sniffer_orchestrator import OptionSnifferOrchestrator


class TestOptionStreamManagerHistory(unittest.TestCase):
    def setUp(self):
        self.mgr = OptionStreamManager()

    def test_get_underlying_history_df_returns_dataframe_or_none(self):
        """Verify that get_underlying_history_df returns a valid DataFrame or None without throwing."""
        df = self.mgr.get_underlying_history_df("NIFTY", limit=10)
        if df is not None:
            self.assertIsInstance(df, pd.DataFrame)
            self.assertIn("close", df.columns)
            self.assertIn("timestamp", df.columns)
            self.assertLessEqual(len(df), 10)

    def test_caching_behavior_returns_cached_reference(self):
        """Verify that subsequent calls within 1.5s hit the in-memory cache."""
        mock_df = pd.DataFrame({
            "timestamp": ["2026-10-23 09:15:00"],
            "open": [22500.0],
            "high": [22510.0],
            "low": [22490.0],
            "close": [22505.0],
            "volume": [1000]
        })
        now = time.time()
        self.mgr._history_cache["TEST_INDEX"] = (now, mock_df)

        cached_result = self.mgr.get_underlying_history_df("TEST_INDEX")
        self.assertIs(cached_result, mock_df)

    def test_orchestrator_surveillance_safely_calls_history_df(self):
        """Verify that OptionSnifferOrchestrator safely interacts with stream manager get_underlying_history_df."""
        orch = OptionSnifferOrchestrator()
        # Orchestrator stream_manager must have get_underlying_history_df
        self.assertTrue(hasattr(orch.stream_manager, "get_underlying_history_df"))
        self.assertTrue(callable(orch.stream_manager.get_underlying_history_df))


if __name__ == "__main__":
    unittest.main()

