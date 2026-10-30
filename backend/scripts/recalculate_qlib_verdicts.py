"""
RECALCULATE ALL HISTORICAL QLIB GOVERNANCE VERDICTS
===================================================
Governance Rules:
- Preserves all historical evaluation records (0 rows deleted).
- Restores immutable criteria:
  - completed trades >= 30
  - Net P&L > 0.0
  - Expectancy > 0.0
  - Sharpe > 0.0
  - Max Drawdown <= 20.0%
- A candidate with negative Sharpe, negative expectancy, negative P&L, or excessive DD (>20%)
  MUST NEVER receive PROMOTE CANDIDATE.
- Updates gate_verdict to exactly one of:
  - PROMOTE CANDIDATE
  - RETAIN CHAMPION
  - INSUFFICIENT EVIDENCE
  (or BENCHMARK for the benchmark baseline)
- Records full audit trail in metrics_json.
"""

import sys
import os
import json
import sqlite3
from datetime import datetime

sys.path.insert(0, os.path.abspath("backend"))
from app.data.historical_data_layer import get_db_path

def recalculate_verdicts():
    conn = sqlite3.connect(get_db_path(), timeout=15.0)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    rows = cursor.execute("SELECT * FROM qlib_model_evaluations ORDER BY timestamp ASC").fetchall()
    print(f"Total historical evaluation records: {len(rows)}")
    
    updated_count = 0
    
    for r in rows:
        eval_id = r["evaluation_id"]
        old_verdict = r["gate_verdict"]
        strategy = r["strategy"]
        model_name = r["model_name"]
        trade_count = r["trade_count"] or 0
        win_rate = r["win_rate"] or 0.0
        net_pnl = r["net_pnl_pct"] or 0.0
        expectancy = r["expectancy"] or 0.0
        sharpe = r["sharpe_ratio"] or 0.0
        max_dd = r["max_drawdown_pct"] or 0.0
        
        try:
            metrics = json.loads(r["metrics_json"] or "{}")
        except Exception:
            metrics = {}

        if old_verdict == "BENCHMARK" or model_name == "Champion Baseline":
            corrected_verdict = "BENCHMARK"
        elif trade_count < 30:
            corrected_verdict = "INSUFFICIENT EVIDENCE"
        elif net_pnl <= 0.0 or expectancy <= 0.0 or sharpe <= 0.0 or max_dd > 20.0:
            corrected_verdict = "RETAIN CHAMPION"
        else:
            # Check relative hurdles
            champ_pnl = 110.56 if strategy == "SWING" else -59.68
            champ_sharpe = 6.73 if strategy == "SWING" else -18.37
            if net_pnl > champ_pnl and sharpe >= champ_sharpe and max_dd <= 20.0:
                corrected_verdict = "PROMOTE CANDIDATE"
            else:
                corrected_verdict = "RETAIN CHAMPION"

        if corrected_verdict != old_verdict:
            audit = metrics.get("audit_history", [])
            audit.append({
                "action": "GOVERNANCE_RECALCULATION",
                "timestamp": datetime.now().isoformat(),
                "previous_verdict": old_verdict,
                "recalculated_verdict": corrected_verdict,
                "reason": "Failed immutable criteria (Sharpe > 0, Net P&L > 0, Expectancy > 0, Max DD <= 20%)"
            })
            metrics["audit_history"] = audit
            metrics["superseded_previous_verdict"] = old_verdict
            
            cursor.execute("""
                UPDATE qlib_model_evaluations
                SET gate_verdict = ?, metrics_json = ?
                WHERE evaluation_id = ?
            """, (corrected_verdict, json.dumps(metrics), eval_id))
            
            updated_count += 1
            print(f"🔄 Corrected {eval_id} ({strategy} {model_name}): '{old_verdict}' -> '{corrected_verdict}' [Net P&L: {net_pnl}%, Sharpe: {sharpe}, DD: {max_dd}%]")
        else:
            print(f"✓ Verified {eval_id} ({strategy} {model_name}): remains '{corrected_verdict}'")

    conn.commit()
    conn.close()
    print(f"\n✅ Completed: {updated_count} records corrected out of {len(rows)} total records.")

if __name__ == "__main__":
    recalculate_verdicts()
