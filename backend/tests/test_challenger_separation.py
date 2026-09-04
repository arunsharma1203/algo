import unittest
import requests
import json
import os
import hashlib
import sqlite3

BASE_URL = "http://localhost:8000"
DB_PATH = "backend/market_data.db"
INTRADAY_CHAMPION_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
SWING_CHAMPION_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"
RESEARCH_JOB_ID = "res_20260903_172929_829837"
RESEARCH_FINGERPRINT = "6313f1af52b6db3bf931839affc6f277da3371f81719f041efe3b6d62ee5aa95"

class TestChallengerSeparation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Verify server is up
        r = requests.get(f"{BASE_URL}/api/settings/datasource", timeout=5)
        assert r.status_code == 200, "Backend server not responding"

    # 1. Foundation Challenger promotion still evaluates Foundation Challenger
    def test_foundation_challenger_promotion_evaluates_foundation(self):
        payload = {
            "challenger_type": "FOUNDATION_MODEL_CHALLENGER",
            "challenger_id": "fnd_challenger_timesfm_chronos_swing",
            "timeframe": "swing",
            "challenger_variant": "plus_both",
            "confirm_promotion": True
        }
        resp = requests.post(f"{BASE_URL}/api/ml/foundation/promote", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("challenger_type"), "FOUNDATION_MODEL_CHALLENGER")
        self.assertEqual(data.get("model_type"), "VOTING_ENSEMBLE_PLUS_FOUNDATION")
        self.assertEqual(data.get("universe"), "BENCHMARK_5")

    # 2. Foundation Challenger with 19 trades fails sample gate
    def test_foundation_challenger_19_trades_fails_sample_gate(self):
        payload = {
            "challenger_type": "FOUNDATION_MODEL_CHALLENGER",
            "challenger_id": "fnd_challenger_timesfm_chronos_swing",
            "timeframe": "swing",
            "challenger_variant": "plus_both",
            "confirm_promotion": True
        }
        resp = requests.post(f"{BASE_URL}/api/ml/foundation/promote", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("status"), "REJECTED")
        self.assertFalse(data.get("gates_passed"))
        self.assertEqual(data.get("trade_count"), 19)
        self.assertEqual(data.get("required_trade_count"), 30)
        self.assertTrue(any("Insufficient OOS sample size" in r for r in data.get("rejection_reasons", [])))

    # 3. Foundation Challenger cannot accept Portfolio Research job ID
    def test_foundation_challenger_rejects_portfolio_research_job_id(self):
        payload = {
            "challenger_type": "FOUNDATION_MODEL_CHALLENGER",
            "challenger_id": RESEARCH_JOB_ID, # Cross-system contamination attempt
            "timeframe": "swing",
            "challenger_variant": "plus_both",
            "confirm_promotion": True
        }
        resp = requests.post(f"{BASE_URL}/api/ml/foundation/promote", json=payload)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("Cross-system routing violation", resp.json().get("detail", ""))

    # 4. Portfolio Research Challenger cannot accept Foundation Challenger ID
    def test_portfolio_research_challenger_rejects_foundation_id(self):
        payload = {
            "challenger_type": "PORTFOLIO_RESEARCH_CHALLENGER",
            "challenger_id": "fnd_challenger_timesfm_chronos",
            "source_research_job_id": RESEARCH_JOB_ID,
            "challenger_oos_start": "2026-09-04"
        }
        resp = requests.post(f"{BASE_URL}/api/data-lab/research/challenger/oos-test", json=payload)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("Cross-system routing violation", resp.json().get("detail", ""))

    # 5. Portfolio Research Challenger correctly references its source research job
    def test_portfolio_research_challenger_references_source_job(self):
        payload = {
            "challenger_type": "PORTFOLIO_RESEARCH_CHALLENGER",
            "challenger_id": f"prc_{RESEARCH_JOB_ID}",
            "source_research_job_id": RESEARCH_JOB_ID,
            "challenger_oos_start": "2026-09-04"
        }
        resp = requests.post(f"{BASE_URL}/api/data-lab/research/challenger/oos-test", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("source_research_job_id"), RESEARCH_JOB_ID)
        self.assertEqual(data.get("challenger_type"), "PORTFOLIO_RESEARCH_CHALLENGER")
        self.assertEqual(data.get("fingerprint"), RESEARCH_FINGERPRINT)

    # 6. Research job 34-trade locked holdout is NOT automatically counted as fresh Challenger OOS
    def test_research_34_trade_holdout_not_counted_as_fresh_oos(self):
        resp = requests.get(f"{BASE_URL}/api/data-lab/research/challenger/{RESEARCH_JOB_ID}/readiness")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("research_holdout_trades"), 34)
        self.assertEqual(data.get("fresh_oos_shadow_trades"), 0)
        self.assertIn("0/30", data.get("sample_size_gate", ""))

    # 7. Challenger OOS cannot begin before/equal to the research holdout end date (2026-09-03)
    def test_challenger_oos_cannot_start_before_holdout_end(self):
        payload = {
            "challenger_type": "PORTFOLIO_RESEARCH_CHALLENGER",
            "challenger_id": f"prc_{RESEARCH_JOB_ID}",
            "source_research_job_id": RESEARCH_JOB_ID,
            "challenger_oos_start": "2025-06-01", # Attempting to recycle research holdout
            "allow_historical_replay": False
        }
        resp = requests.post(f"{BASE_URL}/api/data-lab/research/challenger/oos-test", json=payload)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Temporal OOS Isolation Violation", resp.json().get("detail", ""))

    # 8. Frozen Challenger fingerprint remains unchanged during OOS
    def test_frozen_challenger_fingerprint_unchanged(self):
        resp = requests.get(f"{BASE_URL}/api/data-lab/research/challenger/{RESEARCH_JOB_ID}/readiness")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("fingerprint"), RESEARCH_FINGERPRINT)

    # 9. OOS evaluation cannot modify Champion hash
    def test_oos_evaluation_cannot_modify_champion_hash(self):
        # Trigger an OOS evaluation request
        payload = {
            "challenger_type": "PORTFOLIO_RESEARCH_CHALLENGER",
            "challenger_id": f"prc_{RESEARCH_JOB_ID}",
            "source_research_job_id": RESEARCH_JOB_ID,
            "challenger_oos_start": "2026-09-04"
        }
        requests.post(f"{BASE_URL}/api/data-lab/research/challenger/oos-test", json=payload)

        # Verify champion hashes
        with open("backend/models/intraday/champion_ensemble.pkl", "rb") as f:
            h_intra = hashlib.sha256(f.read()).hexdigest()
        with open("backend/models/swing/champion_ensemble.pkl", "rb") as f:
            h_swing = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(h_intra, INTRADAY_CHAMPION_HASH)
        self.assertEqual(h_swing, SWING_CHAMPION_HASH)

    # 10. OOS evaluation cannot create production positions
    def test_oos_evaluation_cannot_create_production_positions(self):
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM ml_trade_history WHERE position_type IN ('LIVE_POSITION', 'PAPER_POSITION')")
        live_count = cur.fetchone()[0]
        conn.close()
        self.assertEqual(live_count, 0)

    # 11. OOS evaluation consumes 0 portfolio heat
    def test_oos_evaluation_consumes_zero_portfolio_heat(self):
        from app.analytics.kelly_sizer import get_portfolio_heat_status
        heat_res = get_portfolio_heat_status()
        self.assertEqual(heat_res.get("current_heat_pct"), 0.0)
        self.assertEqual(heat_res.get("actual_positions"), 0)

    # 12. Broker execution cannot occur
    def test_broker_execution_cannot_occur(self):
        resp = requests.get(f"{BASE_URL}/api/settings/simulation")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("simulation_mode", True))

    # 13. Foundation and Portfolio Research Challenger metrics remain separate
    def test_foundation_and_portfolio_metrics_remain_separate(self):
        r_fnd = requests.post(f"{BASE_URL}/api/ml/foundation/evaluate?timeframe=swing")
        self.assertEqual(r_fnd.status_code, 200)
        data_fnd = r_fnd.json().get("data", {})

        r_res = requests.get(f"{BASE_URL}/api/data-lab/research/challenger/{RESEARCH_JOB_ID}/readiness")
        self.assertEqual(r_res.status_code, 200)
        data_res = r_res.json()

        self.assertEqual(data_fnd.get("challenger_type"), "FOUNDATION_MODEL_CHALLENGER")
        self.assertEqual(data_res.get("challenger_type"), "PORTFOLIO_RESEARCH_CHALLENGER")
        self.assertNotEqual(data_fnd.get("universe"), data_res.get("universe"))

    # 14. Promotion gates use the correct Challenger type
    def test_promotion_gates_use_correct_challenger_type(self):
        p_res = {
            "challenger_type": "PORTFOLIO_RESEARCH_CHALLENGER",
            "challenger_id": f"prc_{RESEARCH_JOB_ID}",
            "source_research_job_id": RESEARCH_JOB_ID,
            "confirm_promotion": True
        }
        r_res = requests.post(f"{BASE_URL}/api/data-lab/research/challenger/promote", json=p_res)
        self.assertEqual(r_res.status_code, 200)
        data = r_res.json()
        self.assertEqual(data.get("challenger_type"), "PORTFOLIO_RESEARCH_CHALLENGER")
        self.assertEqual(data.get("status"), "REJECTED")

    # 15. Missing challenger_type is rejected
    def test_missing_challenger_type_rejected(self):
        p = {"confirm_promotion": True}
        r = requests.post(f"{BASE_URL}/api/ml/foundation/promote", json=p)
        self.assertEqual(r.status_code, 422) # FastAPI validation error for missing field if required

    # 16. Wrong challenger_id/type combination is rejected
    def test_wrong_challenger_id_type_combination_rejected(self):
        p = {
            "challenger_type": "PORTFOLIO_RESEARCH_CHALLENGER", # Wrong type for foundation endpoint
            "challenger_id": "fnd_challenger_timesfm_chronos",
            "confirm_promotion": True
        }
        r = requests.post(f"{BASE_URL}/api/ml/foundation/promote", json=p)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Challenger type mismatch", r.json().get("detail", ""))

    # 17. Existing research job remains intact
    def test_existing_research_job_intact(self):
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT job_id, status, universe FROM research_jobs WHERE job_id = ?", (RESEARCH_JOB_ID,))
        row = cur.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "COMPLETED")
        self.assertEqual(row[2], "ALL_COLLECTED")

    # 18. Existing research result JSON remains intact
    def test_existing_research_result_json_intact(self):
        json_path = f"backend/results/research/result_{RESEARCH_JOB_ID}.json"
        self.assertTrue(os.path.exists(json_path))
        with open(json_path, "r") as f:
            res_data = json.load(f)
        self.assertEqual(len(res_data.get("trades", [])), 255)
        self.assertEqual(len(res_data.get("equity_curve", [])), 2196)

    # 19. Existing Champion hashes remain identical
    def test_existing_champion_hashes_remain_identical(self):
        with open("backend/models/intraday/champion_ensemble.pkl", "rb") as f:
            h_intra = hashlib.sha256(f.read()).hexdigest()
        with open("backend/models/swing/champion_ensemble.pkl", "rb") as f:
            h_swing = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(h_intra, INTRADAY_CHAMPION_HASH)
        self.assertEqual(h_swing, SWING_CHAMPION_HASH)

    # 20. Existing ml_trade_history remains unchanged by research OOS testing
    def test_existing_ml_trade_history_unchanged(self):
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM ml_trade_history")
        count = cur.fetchone()[0]
        conn.close()
        self.assertEqual(count, 64)

if __name__ == "__main__":
    unittest.main()
