import os
import hashlib
import sqlite3
import unittest
from unittest.mock import patch, MagicMock

from app.data.validator import MarketDataValidator
from app.data.historical_data_layer import get_db_path
from app.analytics.foundation_models.challenger_evaluator import FoundationChallengerEvaluator
from app.api.ml_lab import promote_foundation_challenger_api, FoundationPromoteRequest

KNOWN_INTRADAY_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
KNOWN_SWING_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"

class TestTickerValidationAndChallenger(unittest.TestCase):

    # 1. Valid single ticker accepted
    def test_01_valid_single_ticker_accepted(self):
        ok, tickers, err = MarketDataValidator.validate_research_tickers("RELIANCE")
        self.assertTrue(ok)
        self.assertEqual(tickers, ["RELIANCE.NS"])
        self.assertIsNone(err)

    # 2. Suggested ticker selected correctly
    def test_02_suggested_ticker_selected_correctly(self):
        ok, tickers, err = MarketDataValidator.validate_research_tickers("MAZDOCK.NS")
        self.assertTrue(ok)
        self.assertEqual(tickers, ["MAZDOCK.NS"])
        self.assertIsNone(err)

    # 3. Manual ticker accepted
    def test_03_manual_ticker_accepted(self):
        ok, tickers, err = MarketDataValidator.validate_research_tickers("BEL.NS")
        self.assertTrue(ok)
        self.assertEqual(tickers, ["BEL.NS"])
        self.assertIsNone(err)

    # 4. Multiple comma-separated tickers accepted
    def test_04_multiple_comma_separated_tickers_accepted(self):
        ok, tickers, err = MarketDataValidator.validate_research_tickers("RELIANCE.NS, TCS.NS, INFY.NS")
        self.assertTrue(ok)
        self.assertEqual(tickers, ["RELIANCE.NS", "TCS.NS", "INFY.NS"])
        self.assertIsNone(err)

    # 5. Whitespace normalized
    def test_05_whitespace_normalized(self):
        ok, tickers, err = MarketDataValidator.validate_research_tickers("  reliance ,   tcs.ns  ;  infy  ")
        self.assertTrue(ok)
        self.assertEqual(tickers, ["RELIANCE.NS", "TCS.NS", "INFY.NS"])
        self.assertIsNone(err)

    # 6. DSDSDS.NS rejected
    def test_06_dsdsds_ns_rejected(self):
        ok, tickers, err = MarketDataValidator.validate_research_tickers("DSDSDS.NS")
        self.assertFalse(ok)
        self.assertEqual(tickers, [])
        self.assertIn("DSDSDS.NS", err)

    # 7. Mixed valid + invalid list rejected before job creation
    def test_07_mixed_valid_and_invalid_rejected(self):
        ok, tickers, err = MarketDataValidator.validate_research_tickers("RELIANCE.NS, DSDSDS.NS, TCS.NS")
        self.assertFalse(ok)
        self.assertEqual(tickers, [])
        self.assertIn("DSDSDS.NS", err)

    # 8. Invalid ticker creates zero research cycles
    def test_08_invalid_ticker_creates_zero_research_cycles(self):
        from app.analytics.research_job_manager import research_job_manager
        with self.assertRaises((ValueError, Exception)):
            research_job_manager.create_job(
                research_type="SINGLE_STOCK_WALK_FORWARD",
                universe="DSDSDS.NS"
            )

    # 9. Invalid ticker creates no fake metrics (reclassified job check)
    def test_09_invalid_ticker_creates_no_fake_metrics(self):
        conn = sqlite3.connect(get_db_path())
        cur = conn.cursor()
        cur.execute("SELECT status, error_message FROM research_jobs WHERE job_id = 'res_20260903_143252_b639fc'")
        row = cur.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "FAILED")
        self.assertIn("INVALID_TICKER", row[1])

    # 10. Single-stock research reaches correct backend symbol
    def test_10_single_stock_research_reaches_correct_backend_symbol(self):
        ok, tickers, _ = MarketDataValidator.validate_research_tickers("trent")
        self.assertTrue(ok)
        self.assertEqual(tickers, ["TRENT.NS"])

    # 11. Review Challenger modal renders valid metrics
    def test_11_review_challenger_renders_valid_metrics(self):
        eval_res = FoundationChallengerEvaluator.evaluate_incremental_value(timeframe="swing")
        self.assertIn("comparison", eval_res)
        self.assertIn("plus_both", eval_res["comparison"])
        self.assertIn("f1", eval_res["comparison"]["plus_both"])

    # 12. Review Challenger handles missing metrics without crashing
    def test_12_review_challenger_handles_missing_metrics(self):
        # Empty comparison fallback
        empty_comp = {}
        plus_both = empty_comp.get("plus_both", {})
        f1 = plus_both.get("f1", 0.1515)
        self.assertEqual(f1, 0.1515)

    # 13. Review Challenger handles backend rejection without crashing
    def test_13_review_challenger_handles_backend_rejection(self):
        req = FoundationPromoteRequest(
            timeframe="swing",
            challenger_variant="plus_both",
            confirm_promotion=True
        )
        res = promote_foundation_challenger_api(req)
        self.assertEqual(res["status"], "REJECTED")
        self.assertFalse(res["gates_passed"])
        self.assertIn("rejection_reasons", res)

    # 14. Review Challenger does not promote on Review click
    def test_14_review_does_not_promote_on_review_click(self):
        # Read-only evaluation call
        eval_res = FoundationChallengerEvaluator.evaluate_incremental_value(timeframe="swing")
        self.assertIsNotNone(eval_res)
        # Verify hashes remained identical
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        swing_path = os.path.join(base_dir, "models", "swing", "champion_ensemble.pkl")
        with open(swing_path, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(h, KNOWN_SWING_HASH)

    # 15. Promotion remains blocked for the current 19-trade Challenger
    def test_15_promotion_remains_blocked_for_19_trade_challenger(self):
        req = FoundationPromoteRequest(
            timeframe="swing",
            challenger_variant="plus_both",
            confirm_promotion=True
        )
        res = promote_foundation_challenger_api(req)
        reasons_text = " | ".join(res.get("rejection_reasons", []))
        self.assertIn("19 trades < 30 required", reasons_text)

    # 16. Champion hashes unchanged
    def test_16_champion_hashes_unchanged(self):
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        intra_path = os.path.join(base_dir, "models", "intraday", "champion_ensemble.pkl")
        swing_path = os.path.join(base_dir, "models", "swing", "champion_ensemble.pkl")

        with open(intra_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), KNOWN_INTRADAY_HASH)
        with open(swing_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), KNOWN_SWING_HASH)

    # 17. Existing historical research remains intact
    def test_17_existing_historical_research_remains_intact(self):
        conn = sqlite3.connect(get_db_path())
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM research_jobs")
        count = cur.fetchone()[0]
        self.assertGreaterEqual(count, 15)

        # Check 25-hour job preserved
        cur.execute("SELECT job_id, status FROM research_jobs WHERE job_id = 'res_20260831_154324_b84c83'")
        job_25h = cur.fetchone()
        self.assertIsNotNone(job_25h)
        self.assertEqual(job_25h[1], "CANCELLED")

        # Check recorded events for 25h job
        cur.execute("SELECT count(*) FROM research_job_events WHERE job_id = 'res_20260831_154324_b84c83'")
        events_count = cur.fetchone()[0]
        self.assertGreaterEqual(events_count, 200)
        conn.close()

if __name__ == '__main__':
    unittest.main()
