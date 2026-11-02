import unittest
from unittest.mock import patch, MagicMock

from app.analytics.sector_engine import SectorEngine, INDUSTRY_TO_SECTOR_MAP, EXPLICIT_SECTOR_OVERRIDES

class TestSectorEngine(unittest.TestCase):
    def test_stock_to_sector_resolution_explicit(self):
        """Heavyweights with explicit overrides resolve to their authoritative sectors."""
        self.assertEqual(SectorEngine.get_stock_sector("ITC"), "NIFTY FMCG")
        self.assertEqual(SectorEngine.get_stock_sector("ITC.NS"), "NIFTY FMCG")
        self.assertEqual(SectorEngine.get_stock_sector("MARUTI"), "NIFTY AUTO")
        self.assertEqual(SectorEngine.get_stock_sector("HDFCBANK"), "NIFTY BANK")
        self.assertEqual(SectorEngine.get_stock_sector("RBLBANK"), "NIFTY BANK")
        self.assertEqual(SectorEngine.get_stock_sector("TCS"), "NIFTY IT")
        self.assertEqual(SectorEngine.get_stock_sector("TATASTEEL"), "NIFTY METAL")
        self.assertEqual(SectorEngine.get_stock_sector("SUNPHARMA"), "NIFTY PHARMA")
        self.assertEqual(SectorEngine.get_stock_sector("RELIANCE"), "NIFTY ENERGY")

    def test_stock_to_sector_resolution_mapping(self):
        """Stocks mapped via official NSE industry resolve accurately."""
        self.assertEqual(SectorEngine.get_stock_sector("EMAMILTD"), "NIFTY FMCG")

    def test_sector_counter_trend_blocks_short(self):
        """Short trade is blocked when parent sector is surging."""
        mock_leaderboard = {
            "sectors": {
                "NIFTY FMCG": {"sector": "NIFTY FMCG", "pct_from_open": 0.85, "trend": "BULLISH"},
                "NIFTY BANK": {"sector": "NIFTY BANK", "pct_from_open": 0.50, "trend": "BULLISH"}
            },
            "leaders": ["NIFTY FMCG", "NIFTY BANK"],
            "laggards": []
        }
        is_ok, reason, telem = SectorEngine.is_sector_aligned("ITC", "SELL", leaderboard=mock_leaderboard)
        self.assertFalse(is_ok)
        self.assertIn("SECTOR_COUNTER_TREND", reason)
        self.assertIn("surging", reason)
        self.assertEqual(telem.get("sector"), "NIFTY FMCG")
        self.assertEqual(telem.get("sector_pct_from_open"), 0.85)

    def test_sector_counter_trend_blocks_long(self):
        """Long trade is blocked when parent sector is collapsing."""
        mock_leaderboard = {
            "sectors": {
                "NIFTY IT": {"sector": "NIFTY IT", "pct_from_open": -0.75, "trend": "BEARISH"}
            },
            "leaders": [],
            "laggards": ["NIFTY IT"]
        }
        is_ok, reason, telem = SectorEngine.is_sector_aligned("TCS", "BUY", leaderboard=mock_leaderboard)
        self.assertFalse(is_ok)
        self.assertIn("SECTOR_COUNTER_TREND", reason)
        self.assertIn("falling", reason)
        self.assertEqual(telem.get("sector"), "NIFTY IT")

    def test_sector_trend_allows_aligned_trades(self):
        """Long trade in surging sector and Short trade in falling sector are permitted."""
        mock_leaderboard = {
            "sectors": {
                "NIFTY FMCG": {"sector": "NIFTY FMCG", "pct_from_open": 0.85, "trend": "BULLISH"},
                "NIFTY IT": {"sector": "NIFTY IT", "pct_from_open": -0.60, "trend": "BEARISH"}
            },
            "leaders": ["NIFTY FMCG"],
            "laggards": ["NIFTY IT"]
        }
        # Long FMCG -> Allowed
        ok_long, _, _ = SectorEngine.is_sector_aligned("ITC", "BUY", leaderboard=mock_leaderboard)
        self.assertTrue(ok_long)

        # Short IT -> Allowed
        ok_short, _, _ = SectorEngine.is_sector_aligned("TCS", "SELL", leaderboard=mock_leaderboard)
        self.assertTrue(ok_short)

    def test_unclassified_symbol_passes_safely(self):
        """Unknown or unclassified symbol fails open safely without crashing."""
        is_ok, reason, telem = SectorEngine.is_sector_aligned("NONEXISTENT_TICKER", "BUY")
        self.assertTrue(is_ok)
        self.assertIn("Passed", reason)

if __name__ == "__main__":
    unittest.main()

