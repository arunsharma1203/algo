"""
TEST TICKER VALIDATION FAIL-CLOSED ENGINE & FORENSICS
=====================================================
Comprehensive regression test suite verifying:
1. validate_ticker fails closed on invalid tickers (DSDSDS.NS, fake names, empty, None).
2. validate_ticker succeeds on authoritative Indian universe (RELIANCE.NS, TCS.NS, MAZDOCK.NS, etc.).
3. validate_ticker_list strictly rejects lists containing ANY invalid ticker.
4. resolve_universe_tickers fails closed without fallback on unknown universe or invalid tickers.
5. MarketDataValidator.validate_research_tickers records rejection in MasterLogger and fails closed.
6. POST /api/research/jobs fails closed with HTTP 400 INVALID_TICKER.
7. GET /api/market/search returns [] for unknown tickers and real Indian equities for prefix queries.
8. Read-only forensic verification of historical DSDSDS.NS audit records.
"""

import unittest
import os
import tempfile
import sqlite3
from fastapi.testclient import TestClient

from app.main import app
from app.data.database import set_test_db_override, get_canonical_db_path
from app.data.historical_data_layer import HistoricalDataLayer
from app.analytics.universe_config import (
    validate_ticker,
    validate_ticker_list,
    resolve_universe_tickers
)
from app.data.validator import MarketDataValidator
from app.analytics.master_logger import MasterLogger


class TestTickerValidationFailClosed(unittest.TestCase):
    """Rigorous tests validating authoritative fail-closed ticker validation."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.test_db_path = os.path.join(cls.temp_dir.name, "test_market_data.db")
        set_test_db_override(cls.test_db_path)

        HistoricalDataLayer.init_schema()
        MasterLogger._table_initialized = False

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        set_test_db_override(None)
        cls.temp_dir.cleanup()

    # ─────────────────────────────────────────────────────────────────────────
    # 1. Authoritative validate_ticker tests
    # ─────────────────────────────────────────────────────────────────────────
    def test_01_valid_tickers_pass(self):
        """Authoritative Indian equities must pass validation and standardize to .NS."""
        for sym in ["RELIANCE.NS", "TCS.NS", "INFY.NS", "MAZDOCK.NS", "HDFCBANK.NS"]:
            with self.subTest(sym=sym):
                ok, canon, err = validate_ticker(sym)
                self.assertTrue(ok)
                self.assertEqual(canon, sym)
                self.assertIsNone(err)

    def test_02_symbol_without_suffix_standardizes_to_ns(self):
        """Symbols without suffix (e.g. RELIANCE, GRSE) standardize automatically to .NS."""
        ok, canon, err = validate_ticker("RELIANCE")
        self.assertTrue(ok)
        self.assertEqual(canon, "RELIANCE.NS")

        ok, canon, err = validate_ticker("reliance")
        self.assertTrue(ok)
        self.assertEqual(canon, "RELIANCE.NS")

        ok, canon, err = validate_ticker("GRSE")
        self.assertTrue(ok)
        self.assertEqual(canon, "GRSE.NS")

    def test_03_invalid_tickers_fail_closed(self):
        """Fake, invalid, or hallucinated tickers must return (False, "", err) with INVALID_TICKER."""
        invalid_cases = [
            "DSDSDS.NS",
            "DSDSDS",
            "dsdsds",
            "SDFSDF.NS",
            "INVALID_TICKER_123.NS",
            "XYZ12345.NS",
            "TESTSTOCK_FAKE.NS"
        ]
        for bad_ticker in invalid_cases:
            with self.subTest(ticker=bad_ticker):
                ok, canon, err = validate_ticker(bad_ticker)
                self.assertFalse(ok)
                self.assertEqual(canon, "")
                self.assertIsNotNone(err)
                self.assertIn("INVALID_TICKER", err)

    def test_04_empty_or_none_tickers_fail_closed(self):
        """Empty string, whitespace, or None must fail closed."""
        for bad in [None, "", "   ", "	"]:
            with self.subTest(bad=bad):
                ok, canon, err = validate_ticker(bad)
                self.assertFalse(ok)
                self.assertEqual(canon, "")
                self.assertIsNotNone(err)

    # ─────────────────────────────────────────────────────────────────────────
    # 2. validate_ticker_list tests
    # ─────────────────────────────────────────────────────────────────────────
    def test_05_valid_ticker_list_succeeds(self):
        """A list of valid tickers returns deduplicated and standardized list."""
        input_list = ["RELIANCE", "TCS.NS", "reliance.ns", "INFY"]
        ok, clean, err = validate_ticker_list(input_list)
        self.assertTrue(ok)
        self.assertEqual(clean, ["RELIANCE.NS", "TCS.NS", "INFY.NS"])
        self.assertIsNone(err)

    def test_06_ticker_list_with_any_invalid_fails_closed(self):
        """A list containing even one invalid ticker must fail closed completely."""
        mixed_list = ["RELIANCE.NS", "TCS.NS", "DSDSDS.NS", "INFY.NS"]
        ok, clean, err = validate_ticker_list(mixed_list)
        self.assertFalse(ok)
        self.assertEqual(clean, [])
        self.assertIn("INVALID_TICKER", err)
        self.assertIn("DSDSDS.NS", err)

    def test_07_empty_ticker_list_fails_closed(self):
        """Empty ticker list must return False with INVALID_TICKER."""
        ok, clean, err = validate_ticker_list([])
        self.assertFalse(ok)
        self.assertEqual(clean, [])
        self.assertIn("INVALID_TICKER", err)

    # ─────────────────────────────────────────────────────────────────────────
    # 3. resolve_universe_tickers tests
    # ─────────────────────────────────────────────────────────────────────────
    def test_08_resolve_known_universe_presets(self):
        """Known universe presets (NIFTY_50, LIVE_52) resolve cleanly."""
        n50 = resolve_universe_tickers("NIFTY_50")
        self.assertGreaterEqual(len(n50), 50)
        self.assertIn("RELIANCE.NS", n50)

        l52 = resolve_universe_tickers("LIVE_52")
        self.assertEqual(len(l52), 52)
        self.assertIn("RELIANCE.NS", l52)

    def test_09_resolve_custom_universe_with_invalid_ticker_fails_closed(self):
        """Custom universe with invalid ticker must raise ValueError and NOT fallback to RELIANCE."""
        with self.assertRaises(ValueError) as ctx:
            resolve_universe_tickers("CUSTOM", custom_tickers=["DSDSDS.NS"])
        self.assertIn("INVALID_TICKER", str(ctx.exception))

    def test_10_resolve_unknown_universe_fails_closed(self):
        """Unknown universe name must raise ValueError and NOT fallback to RELIANCE."""
        with self.assertRaises(ValueError) as ctx:
            resolve_universe_tickers("UNKNOWN_UNIVERSE_XYZ")
        self.assertIn("INVALID_TICKER", str(ctx.exception))

    # ─────────────────────────────────────────────────────────────────────────
    # 4. validate_research_tickers security audit logging
    # ─────────────────────────────────────────────────────────────────────────
    def test_11_validate_research_tickers_logs_security_event_on_invalid(self):
        """Invalid tickers log a DATA_GATE event in app_master_events."""
        ok, clean, err = MarketDataValidator.validate_research_tickers(["DSDSDS.NS"])
        self.assertFalse(ok)
        self.assertEqual(clean, [])
        self.assertIn("INVALID_TICKER", err)

        conn = sqlite3.connect(self.test_db_path)
        cur = conn.execute(
            "SELECT event_type, severity, message FROM app_master_events "
            "WHERE category = 'DATA_GATE' AND event_type = 'REJECTED_INVALID_TICKER'"
        )
        row = cur.fetchone()
        conn.close()

        self.assertIsNotNone(row, "Security event must be logged to app_master_events")
        self.assertEqual(row[0], "REJECTED_INVALID_TICKER")
        self.assertEqual(row[1], "WARNING")
        self.assertIn("DSDSDS.NS", row[2])

    # ─────────────────────────────────────────────────────────────────────────
    # 5. API endpoint validation
    # ─────────────────────────────────────────────────────────────────────────
    def test_12_api_post_research_job_invalid_ticker_returns_400(self):
        """POST /api/research/jobs fails closed with HTTP 400 when invalid ticker supplied."""
        payload = {
            "title": "Malicious Ticker Test",
            "universe": "CUSTOM",
            "custom_tickers": ["DSDSDS.NS"],
            "model_family": "LightGBM",
            "timeframe": "1d"
        }
        resp = self.client.post("/api/data-lab/research/jobs", json=payload)
        self.assertEqual(resp.status_code, 400)
        detail = resp.json().get("detail", "")
        self.assertIn("INVALID_TICKER", detail)

    def test_13_api_market_search_empty_on_fake_symbol(self):
        """GET /api/market/search returns [] for non-existent symbols like DSDSDS."""
        resp = self.client.get("/api/market/search?query=DSDSDS")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_14_api_market_search_returns_matches_for_valid_prefix(self):
        """GET /api/market/search returns real Indian equities for valid prefixes like REL or GRSE."""
        resp = self.client.get("/api/market/search?query=REL")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        symbols = [item["symbol"] for item in data]
        self.assertIn("RELIANCE.NS", symbols)

    # ─────────────────────────────────────────────────────────────────────────
    # 6. Read-only forensic verification of canonical production records
    # ─────────────────────────────────────────────────────────────────────────
    def test_15_production_forensic_dsdsds_audit(self):
        """
        Verifies that in canonical production database:
        - 0 rows of DSDSDS.NS exist in ohlcv (no synthetic data written)
        - 0 rows of DSDSDS.NS exist in research_candidate_vault
        - 0 rows of DSDSDS.NS exist in research_experiments_ledger
        - Historical audit records are safely preserved without deletion
        """
        canonical_path = get_canonical_db_path()
        conn = sqlite3.connect(canonical_path)

        # 1. Zero rows in ohlcv
        cur = conn.execute("SELECT COUNT(*) FROM ohlcv WHERE ticker = 'DSDSDS.NS'")
        ohlcv_count = cur.fetchone()[0]
        self.assertEqual(ohlcv_count, 0, "Production ohlcv must contain 0 rows for DSDSDS.NS")

        # 2. Zero rows in candidate vault
        cur = conn.execute("SELECT COUNT(*) FROM research_candidate_vault WHERE discovery_universe LIKE '%DSDSDS%'")
        vault_count = cur.fetchone()[0]
        self.assertEqual(vault_count, 0, "Candidate vault must contain 0 rows for DSDSDS.NS")

        # 3. Zero rows in experiment ledger
        cur = conn.execute("SELECT COUNT(*) FROM research_experiments_ledger WHERE changes_json LIKE '%DSDSDS%'")
        ledger_count = cur.fetchone()[0]
        self.assertEqual(ledger_count, 0, "Experiment ledger must contain 0 rows for DSDSDS.NS")

        # 4. Historical audit records preserved in research_jobs
        cur = conn.execute("SELECT COUNT(*) FROM research_jobs WHERE universe LIKE '%DSDSDS%' OR title LIKE '%DSDSDS%'")
        jobs_count = cur.fetchone()[0]
        self.assertEqual(jobs_count, 3, "Historical research_jobs records must be preserved for forensics")

        conn.close()


if __name__ == "__main__":
    unittest.main()
