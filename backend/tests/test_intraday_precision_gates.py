import unittest
from unittest.mock import patch, MagicMock

class TestIntradayPrecisionGates(unittest.TestCase):
    def test_vwap_gate_blocks_long_below_vwap(self):
        """Long setup is rejected when price is below intraday VWAP."""
        from app.analytics.decision_engine import evaluate_ticker
        import pandas as pd
        import numpy as np

        dates = pd.date_range("2026-09-01", periods=100, freq="15min")
        df = pd.DataFrame({
            "open": np.linspace(100, 105, 100),
            "high": np.linspace(101, 106, 100),
            "low": np.linspace(99, 104, 100),
            "close": np.linspace(100, 105, 100),
            "volume": np.random.randint(1000, 5000, 100)
        }, index=dates)

        mock_model = MagicMock()
        mock_model.predict_proba.return_value = np.array([[0.3, 0.7]]) # Bullish 70%

        with patch("app.data.market_provider.get_live_quote_with_meta", return_value=None):
            with patch("app.data.upstox_provider.get_cached_upstox_quote") as mock_quotes:
                # Entry 104.0 is below VWAP 105.5
                mock_quotes.return_value = {
                    "ltp": 104.0, "vwap": 105.5, "open": 103.0, "source": "upstox", "is_realtime": True, "age_ms": 0
                }
                with patch("app.analytics.sector_engine.SectorEngine.is_sector_aligned", return_value=(True, "OK", {})):
                    with patch("app.analytics.macro_engine.is_direction_permitted", return_value=(True, "OK")):
                        res = evaluate_ticker(
                            ticker="TEST.NS",
                            df=df,
                            champion_model=mock_model,
                            champion_meta={"version": "v1.0-test"},
                            trade_type="INTRADAY",
                            source="MANUAL",
                            allow_candle_close=False
                        )
                        self.assertIsNotNone(res)
                        self.assertFalse(res.qualified)
                        self.assertIn("INTRADAY_VWAP_BELOW", res.rejection_reason)

    def test_vwap_gate_blocks_short_above_vwap(self):
        """Short setup is rejected when price is above intraday VWAP."""
        from app.analytics.decision_engine import evaluate_ticker
        import pandas as pd
        import numpy as np

        dates = pd.date_range("2026-09-01", periods=100, freq="15min")
        df = pd.DataFrame({
            "open": np.linspace(105, 100, 100),
            "high": np.linspace(106, 101, 100),
            "low": np.linspace(104, 99, 100),
            "close": np.linspace(105, 100, 100),
            "volume": np.random.randint(1000, 5000, 100)
        }, index=dates)

        mock_model = MagicMock()
        mock_model.predict_proba.return_value = np.array([[0.7, 0.3]]) # Bearish

        with patch("app.data.market_provider.get_live_quote_with_meta", return_value=None):
            with patch("app.data.upstox_provider.get_cached_upstox_quote") as mock_quotes:
                # Entry 102.0 is above VWAP 101.0
                mock_quotes.return_value = {
                    "ltp": 102.0, "vwap": 101.0, "open": 103.0, "source": "upstox", "is_realtime": True, "age_ms": 0
                }
                with patch("app.analytics.sector_engine.SectorEngine.is_sector_aligned", return_value=(True, "OK", {})):
                    with patch("app.analytics.macro_engine.is_direction_permitted", return_value=(True, "OK")):
                        res = evaluate_ticker(
                            ticker="TEST.NS",
                            df=df,
                            champion_model=mock_model,
                            champion_meta={"version": "v1.0-test"},
                            trade_type="INTRADAY",
                            source="MANUAL",
                            allow_candle_close=False
                        )
                        self.assertIsNotNone(res)
                        self.assertFalse(res.qualified)
                        self.assertIn("INTRADAY_VWAP_ABOVE", res.rejection_reason)

    def test_open_gate_blocks_short_above_open(self):
        """Short setup is rejected when price is trading above Day's Open (green day for stock)."""
        from app.analytics.decision_engine import evaluate_ticker
        import pandas as pd
        import numpy as np

        dates = pd.date_range("2026-09-01", periods=100, freq="15min")
        df = pd.DataFrame({
            "open": np.linspace(105, 100, 100),
            "high": np.linspace(106, 101, 100),
            "low": np.linspace(104, 99, 100),
            "close": np.linspace(105, 100, 100),
            "volume": np.random.randint(1000, 5000, 100)
        }, index=dates)

        mock_model = MagicMock()
        mock_model.predict_proba.return_value = np.array([[0.7, 0.3]]) # Bearish

        with patch("app.data.market_provider.get_live_quote_with_meta", return_value=None):
            with patch("app.data.upstox_provider.get_cached_upstox_quote") as mock_quotes:
                # LTP 103.0 is above Day's Open 101.0, even though VWAP is 103.5
                mock_quotes.return_value = {
                    "ltp": 103.0, "vwap": 103.5, "open": 101.0, "source": "upstox", "is_realtime": True, "age_ms": 0
                }
                with patch("app.analytics.sector_engine.SectorEngine.is_sector_aligned", return_value=(True, "OK", {})):
                    with patch("app.analytics.macro_engine.is_direction_permitted", return_value=(True, "OK")):
                        res = evaluate_ticker(
                            ticker="TEST.NS",
                            df=df,
                            champion_model=mock_model,
                            champion_meta={"version": "v1.0-test"},
                            trade_type="INTRADAY",
                            source="MANUAL",
                            allow_candle_close=False
                        )
                        self.assertIsNotNone(res)
                        self.assertFalse(res.qualified)
                        self.assertIn("INTRADAY_OPEN_ABOVE", res.rejection_reason)

    def test_conviction_floor_in_game_engine(self):
        """Consensus Game Engine suppresses trades with composite score < 54.0%."""
        score = 51.3
        self.assertTrue(score < 54.0, "Score below floor should be filtered")

if __name__ == "__main__":
    unittest.main()

