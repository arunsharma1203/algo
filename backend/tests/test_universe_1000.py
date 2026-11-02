import unittest
from app.analytics.universe_config import (
    resolve_nse_1000,
    resolve_universe_tickers,
    validate_ticker,
    get_universe,
    UNIVERSE_PRESETS
)

class TestNSE1000Universe(unittest.TestCase):

    def test_01_resolve_nse_1000_returns_1000_equities(self):
        """Verify resolve_nse_1000 returns exactly 1,000 tickers with .NS suffix."""
        universe = resolve_nse_1000()
        self.assertEqual(len(universe), 1000)
        self.assertEqual(len(set(universe)), 1000)
        for sym in universe:
            self.assertTrue(sym.endswith(".NS"), f"Symbol {sym} does not end with .NS")
            self.assertFalse(sym.startswith(("CACHE_", "TEMP_", "DUMMY_")), f"Dummy symbol detected: {sym}")

    def test_02_universe_preset_registration(self):
        """Verify NSE_1000 and NIFTY_1000 are registered in presets."""
        self.assertIn("NSE_1000", UNIVERSE_PRESETS)
        self.assertIn("NIFTY_1000", UNIVERSE_PRESETS)
        u_info = get_universe("NSE_1000")
        self.assertEqual(len(u_info["tickers"]), 1000)
        self.assertIn("1,000", u_info["name"])

    def test_03_resolve_universe_tickers_supports_nse_1000(self):
        """Verify resolve_universe_tickers('NSE_1000') resolves all 1000 tickers."""
        tickers = resolve_universe_tickers("NSE_1000")
        self.assertEqual(len(tickers), 1000)
        nifty_alias = resolve_universe_tickers("NIFTY_1000")
        self.assertEqual(len(nifty_alias), 1000)

    def test_04_validate_ticker_recognizes_nse_1000_constituents(self):
        """Verify validate_ticker recognizes constituents across the 1000 universe."""
        universe = resolve_nse_1000()
        for idx in [0, 250, 500, 750, 999]:
            sym = universe[idx]
            is_valid, clean_sym, err = validate_ticker(sym)
            self.assertTrue(is_valid, f"Expected {sym} to be valid, got: {err}")
            self.assertEqual(clean_sym, sym)

    def test_05_unknown_symbol_still_fails_closed(self):
        """Verify fake/unknown symbols outside the 1,000 universe fail closed."""
        is_valid, clean_sym, err = validate_ticker("NONEXISTENT_XYZ_123.NS")
        self.assertFalse(is_valid)
        self.assertIn("INVALID_TICKER", err)


if __name__ == '__main__':
    unittest.main()

