from datetime import datetime
"""
LEGACY vs QLIB COMPARISON SERVICE
=================================
Performs dynamic, apples-to-apples performance comparisons between
the production Legacy Champion system and the new Microsoft Qlib system.

Invariants:
- Zero hardcoded comparison metrics; derived strictly from SQLite trade history.
- Honest handling of zero completed trades: displays 'INSUFFICIENT DATA', not 0.0% win rate.
- Strategy-specific isolation: SWING vs SWING, INTRADAY vs INTRADAY, FNO vs FNO.
- Transparent verdict: Never declares Qlib superior without concrete economic trading evidence.
"""

import sqlite3
import logging
import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, List
from app.data.historical_data_layer import get_db_path
from app.qlib_engine.model_registry import QlibModelRegistry

logger = logging.getLogger(__name__)


class ComparisonService:
    """
    Evaluates Legacy vs Qlib system metrics dynamically from tracked database records.
    """

    @classmethod
    def calculate_engine_metrics(cls, engine: str, strategy: Optional[str] = None) -> Dict[str, Any]:
        """Calculates performance for given engine (LEGACY or QLIB) and optional strategy from ml_trade_history."""
        db_file = get_db_path()
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        try:
            where_clauses = []
            params = []

            clean_eng = engine.upper().strip()
            if clean_eng == "LEGACY":
                where_clauses.append("(engine = 'LEGACY' OR engine IS NULL OR (engine != 'QLIB' AND source != 'QLIB_SCANNER'))")
            else:
                where_clauses.append("(engine = 'QLIB' OR source = 'QLIB_SCANNER')")

            if strategy:
                clean_strat = strategy.upper().strip()
                where_clauses.append("(strategy = ? OR trade_type = ?)")
                params.extend([clean_strat, clean_strat])

            where_str = " AND ".join(where_clauses)

            # Query all trades for this engine & strategy
            rows = conn.execute(f"SELECT * FROM ml_trade_history WHERE {where_str} ORDER BY id DESC", params).fetchall()

            total_recs = len(rows)
            closed_trades = [r for r in rows if r["status"] != "OPEN" and r["outcome"] not in (None, "", "OPEN")]
            open_trades = [r for r in rows if r["status"] == "OPEN" or r["outcome"] in (None, "", "OPEN")]

            # If no rows in ml_trade_history for QLIB, check fallback table
            if total_recs == 0 and clean_eng == "QLIB":
                q_fb = "SELECT * FROM qlib_virtual_recommendations WHERE 1=1"
                p_fb = []
                if strategy:
                    q_fb += " AND strategy = ?"
                    p_fb.append(strategy.upper())
                fb_rows = conn.execute(q_fb, p_fb).fetchall()
                total_recs = len(fb_rows)
                closed_trades = [r for r in fb_rows if r["status"] != "OPEN" and r["outcome"] not in (None, "", "OPEN")]
                open_trades = [r for r in fb_rows if r["status"] == "OPEN" or r["outcome"] in (None, "", "OPEN")]

            closed_count = len(closed_trades)

            if closed_count == 0:
                return {
                    "engine": clean_eng,
                    "strategy": strategy or "ALL",
                    "total_recommendations": total_recs,
                    "open_trades": len(open_trades),
                    "completed_trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "win_rate": None,
                    "profit_factor": None,
                    "net_pnl_pct": None,
                    "avg_trade_pnl_pct": None,
                    "expectancy_pct": None,
                    "max_drawdown_pct": None,
                    "sharpe": None,
                    "avg_holding_period": None,
                    "slippage_drag_total": 0.0,
                    "target_hit_rate": None,
                    "stop_hit_rate": None,
                    "expiration_rate": None,
                    "status": "INSUFFICIENT DATA",
                    "display_status": "INSUFFICIENT DATA — 0 completed trades"
                }

            pnls = []
            drags = []
            holding_hours = []

            for r in closed_trades:
                p = 0.0
                if "profit_pct" in r.keys() and r["profit_pct"] is not None:
                    p = r["profit_pct"]
                elif "pnl_pct" in r.keys() and r["pnl_pct"] is not None:
                    p = r["pnl_pct"]
                if p is not None:
                    p_val = float(p)
                    # Real-world invariant: Long cash equity positions cannot lose > -100%
                    strat_str = str(r.get("strategy") or "").upper()
                    if strat_str != "FNO":
                        p_val = max(-100.0, p_val)
                    pnls.append(p_val)
                if "slippage_drag" in r.keys() and r["slippage_drag"] is not None:

                    drags.append(float(r["slippage_drag"]))

                # Calculate holding time if timestamps exist
                try:
                    t_in = pd.to_datetime(r["timestamp"])
                    if r["exit_time"]:
                        t_out = pd.to_datetime(r["exit_time"])
                        hrs = max(0.0, (t_out - t_in).total_seconds() / 3600.0)
                        holding_hours.append(hrs)
                except Exception:
                    pass

            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            win_rate = round((len(wins) / closed_count) * 100.0, 2)

            target_hits = [r for r in closed_trades if r["outcome"] == "TARGET MET"]
            stop_hits = [r for r in closed_trades if r["outcome"] == "SL HIT"]
            expirations = [r for r in closed_trades if "SQUARE" in str(r["outcome"]).upper() or "EXPIR" in str(r["outcome"]).upper()]

            target_hit_rate = round((len(target_hits) / closed_count) * 100.0, 2)
            stop_hit_rate = round((len(stop_hits) / closed_count) * 100.0, 2)
            expiration_rate = round((len(expirations) / closed_count) * 100.0, 2)

            gross_win = sum(wins) if wins else 0.0
            gross_loss = abs(sum(losses)) if losses else 0.0
            pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_win > 0 else 1.0)

            avg_win = float(np.mean(wins)) if wins else 0.0
            avg_loss = abs(float(np.mean(losses))) if losses else 0.0
            win_prob = len(wins) / closed_count
            loss_prob = len(losses) / closed_count
            expectancy = round((win_prob * avg_win) - (loss_prob * avg_loss), 2)

            # Max drawdown
            cum_ret = np.cumsum(pnls)
            running_max = np.maximum.accumulate(cum_ret)
            dd = running_max - cum_ret
            max_dd = round(float(np.max(dd)) if len(dd) > 0 else 0.0, 2)

            # Sharpe (annualized, 252 days)
            pnl_arr = np.array(pnls)
            std_pnl = float(np.std(pnl_arr)) if len(pnl_arr) > 1 else 1.0
            mean_pnl = float(np.mean(pnl_arr))
            annual_factor = np.sqrt(252) if (strategy and strategy.upper() == "SWING") else np.sqrt(252 * 25)
            sharpe = round(float((mean_pnl / (std_pnl + 1e-8)) * annual_factor), 2) if len(pnls) >= 5 else None

            avg_hold_str = f"{round(float(np.mean(holding_hours)), 1)} hrs" if holding_hours else "N/A"

            return {
                "engine": clean_eng,
                "strategy": strategy or "ALL",
                "total_recommendations": total_recs,
                "open_trades": len(open_trades),
                "completed_trades": closed_count,
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": win_rate,
                "profit_factor": pf,
                "net_pnl_pct": round(float(sum(pnls)), 2),
                "cumulative_return_points": round(float(sum(pnls)), 2),
                "compounded_growth_pct": round(float(np.prod(1.0 + np.array(pnls) / 100.0) - 1.0) * 100.0, 2) if pnls else 0.0,
                "avg_trade_pnl_pct": round(mean_pnl, 2),
                "expectancy_pct": expectancy,
                "max_drawdown_pct": max_dd,
                "sharpe": sharpe,
                "avg_holding_period": avg_hold_str,
                "slippage_drag_total": round(float(sum(drags)), 2) if drags else 0.0,
                "target_hit_rate": target_hit_rate,
                "stop_hit_rate": stop_hit_rate,
                "expiration_rate": expiration_rate,
                "status": "ACTIVE_PRODUCTION" if clean_eng == "LEGACY" else "EXPERIMENTAL_PARALLEL",
                "display_status": f"{win_rate}% Win Rate ({closed_count} closed)"
            }
        finally:
            conn.close()

    @classmethod
    def get_legacy_metrics(cls) -> Dict[str, Any]:
        """Calculates closed-trade performance for the Legacy system."""
        return cls.calculate_engine_metrics("LEGACY")

    @classmethod
    def get_qlib_metrics(cls) -> Dict[str, Any]:
        """Calculates performance for the Qlib system."""
        q_metrics = cls.calculate_engine_metrics("QLIB")
        # Pull registered model OOS metrics for reference
        swing_manifest = QlibModelRegistry.get_active_manifest("SWING")
        oos_meta = swing_manifest.get("metrics", {}) if swing_manifest else {}
        q_metrics["oos_trades_count"] = oos_meta.get("trades_count", 0)
        q_metrics["oos_win_rate"] = oos_meta.get("win_rate")
        q_metrics["oos_profit_factor"] = oos_meta.get("profit_factor")
        q_metrics["oos_sharpe"] = oos_meta.get("sharpe")
        q_metrics["oos_max_drawdown_pct"] = oos_meta.get("max_drawdown_pct")
        q_metrics["oos_ic"] = oos_meta.get("ic")
        q_metrics["oos_rank_ic"] = oos_meta.get("rank_ic")
        return q_metrics

    @classmethod
    def get_side_by_side_comparison(cls) -> Dict[str, Any]:
        """Generates unified, strategy-by-strategy comparison scorecard."""
        overall_legacy = cls.get_legacy_metrics()
        overall_qlib = cls.get_qlib_metrics()

        # Strategy-specific comparisons
        swing_legacy = cls.calculate_engine_metrics("LEGACY", "SWING")
        swing_qlib = cls.calculate_engine_metrics("QLIB", "SWING")

        intraday_legacy = cls.calculate_engine_metrics("LEGACY", "INTRADAY")
        intraday_qlib = cls.calculate_engine_metrics("QLIB", "INTRADAY")

        fno_qlib = cls.calculate_engine_metrics("QLIB", "FNO")

        # Objectively assess verdict (requires at least 10 completed forward trades for statistical significance)
        qlib_completed = overall_qlib.get("completed_trades", 0)
        if qlib_completed < 10:
            verdict = f"INSUFFICIENT DATA ({qlib_completed}/10 forward trades completed) — Qlib virtual trading is actively accumulating forward market history."
            recommendation = "KEEP_LEGACY_CONTROL_GROUP"
        elif (overall_qlib.get("profit_factor") or 0) > (overall_legacy.get("profit_factor") or 1.0):
            verdict = "QLIB SUPERIOR — Qlib shows higher live profit factor and risk-adjusted return."
            recommendation = "CONSIDER_MIGRATION"
        else:
            verdict = "LEGACY SUPERIOR — Production Champion outperforms Qlib on live realized returns."
            recommendation = "KEEP_LEGACY_CONTROL_GROUP"

        return {
            "comparison_timestamp": datetime.now().isoformat(),
            "verdict": verdict,
            "system_decision": recommendation,
            "legacy": overall_legacy,
            "qlib": overall_qlib,
            "by_strategy": {
                "SWING": {
                    "legacy": swing_legacy,
                    "qlib": swing_qlib,
                    "comparable": True
                },
                "INTRADAY": {
                    "legacy": intraday_legacy,
                    "qlib": intraday_qlib,
                    "comparable": True
                },
                "FNO": {
                    "legacy": None,
                    "qlib": fno_qlib,
                    "note": "Legacy F&O equivalent does not exist; Qlib tracked independently."
                }
            },
            "notes": [
                "Legacy represents the active production Champion control group.",
                "Qlib is the experimental parallel system running real Microsoft Qlib models.",
                "Migration decision requires statistically significant live forward evidence.",
                "All performance calculations derived strictly from SQLite without synthetic metrics."
            ]
        }
