"""
OOS CANDIDATE VAULT BACKLOG PROCESSOR
======================================
Batch-evaluates all candidates stuck in OOS_PENDING against real forward market bars.
Transitions qualified candidates to OOS_PASSED and rejected candidates to OOS_FAILED.
"""

import time
import sqlite3
import logging
from app.data.historical_data_layer import get_db_path
from app.analytics.research_orchestrator.research_orchestrator import ResearchOrchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def main():
    conn = sqlite3.connect(get_db_path(), timeout=30.0)
    c = conn.cursor()
    c.execute("SELECT candidate_id FROM research_candidate_vault WHERE status = 'OOS_PENDING' ORDER BY created_at ASC")
    pending_ids = [r[0] for r in c.fetchall()]
    conn.close()

    total = len(pending_ids)
    logger.info(f"Starting OOS evaluation batch for {total} pending candidates...")

    passed = 0
    failed = 0
    errors = 0
    t0 = time.time()

    for i, cid in enumerate(pending_ids, 1):
        try:
            res = ResearchOrchestrator.evaluate_oos_candidate_live(cid)
            status = res.get("status") if isinstance(res, dict) else "UNKNOWN"
            if status == "OOS_PASSED":
                passed += 1
                logger.info(f"[{i}/{total}] Candidate {cid} PASSED OOS: {res}")
            elif status == "OOS_FAILED":
                failed += 1
            else:
                failed += 1
        except Exception as e:
            errors += 1
            logger.debug(f"[{i}/{total}] Evaluation error for {cid}: {e}")

        if i % 50 == 0 or i == total:
            elapsed = round(time.time() - t0, 1)
            logger.info(f"Progress: [{i}/{total}] ({round(i/total*100, 1)}%) | Passed: {passed}, Failed: {failed}, Errors: {errors} | Elapsed: {elapsed}s")

    elapsed = round(time.time() - t0, 1)
    logger.info(f"Batch completed in {elapsed}s. Final: Passed={passed}, Failed={failed}, Errors={errors}")

if __name__ == "__main__":
    main()

