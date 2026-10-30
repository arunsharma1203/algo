"""
PositionMonitorService: Authoritative Single-Source-of-Truth Position & Recommendation Monitor.

Consolidates trade tracking, LTP refresh, outcome evaluation (SL/TP hits), and defensive risk guards.
Eliminates duplicate polling across ml_history and autonomous_bot while strictly preserving:
1. Capital safety: recommendations with position_type = 'NOT_A_POSITION' consume 0.0% portfolio heat.
2. Honest outcome accounting: real candle High/Low/Close checks with slippage modeling.
"""

import json
import logging
import sqlite3
import time as time_module
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd

from app.data.historical_data_layer import get_db_path
from app.data.data_gateway import DataGateway
from app.analytics.kelly_sizer import get_portfolio_heat_status

logger = logging.getLogger(__name__)

_MONITOR_CACHE: Dict[str, Any] = {
    "data": None,
    "timestamp": 0.0,
    "db_path": None
}
_MONITOR_CACHE_TTL = 30.0  # 30-second cache TTL

class PositionMonitorService:
    """
    Authoritative Position & Trade Recommendation Monitor.
    Tracks all entries in ml_trade_history and manages active price updates and P&L.
    """

    @classmethod
    def evaluate_all(cls, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Evaluates all recorded ML trade history against subsequent market price action.
        Delegates to canonical evaluate_ml_history logic with shared DataGateway integration.
        Returns the complete list of evaluated trade records.
        """
        epoch_now = time_module.time()
        current_db = get_db_path()

        if not force_refresh and _MONITOR_CACHE["data"] is not None and _MONITOR_CACHE.get("db_path") == current_db:
            if (epoch_now - _MONITOR_CACHE["timestamp"]) < _MONITOR_CACHE_TTL:
                return _MONITOR_CACHE["data"]

        # Import canonical evaluate_ml_history implementation from ml_history
        from app.api.ml_history import evaluate_ml_history as _canonical_eval
        results = _canonical_eval(force_refresh=force_refresh)

        _MONITOR_CACHE["data"] = results
        _MONITOR_CACHE["timestamp"] = epoch_now
        _MONITOR_CACHE["db_path"] = current_db
        return results

    @classmethod
    def get_open_positions_summary(cls, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Returns an authoritative summary of all active open trades and recommendations.
        Separates virtual recommendations (NOT_A_POSITION) from real positions (PAPER_POSITION, LIVE_POSITION).
        """
        all_trades = cls.evaluate_all(force_refresh=force_refresh)
        open_trades = [t for t in all_trades if t.get("outcome") == "OPEN"]

        actual_positions = [
            t for t in open_trades 
            if t.get("position_type") in ("PAPER_POSITION", "LIVE_POSITION")
        ]
        virtual_recs = [
            t for t in open_trades 
            if t.get("position_type") not in ("PAPER_POSITION", "LIVE_POSITION")
        ]

        heat_status = get_portfolio_heat_status()

        total_unrealized_pnl = sum(t.get("profit_pct", 0.0) for t in open_trades)
        avg_unrealized_pnl = (total_unrealized_pnl / len(open_trades)) if open_trades else 0.0

        return {
            "timestamp": datetime.now().isoformat(),
            "total_open": len(open_trades),
            "actual_positions_count": len(actual_positions),
            "virtual_recommendations_count": len(virtual_recs),
            "portfolio_heat_pct": heat_status.get("current_heat_pct", 0.0),
            "portfolio_heat_status": heat_status.get("status", "NORMAL"),
            "max_heat_cap_pct": heat_status.get("max_heat_cap_pct", 6.0),
            "avg_unrealized_pnl_pct": round(avg_unrealized_pnl, 2),
            "actual_positions": actual_positions,
            "virtual_recommendations": virtual_recs
        }

    @classmethod
    def reconcile_trade(
        cls,
        trade_id: int,
        exit_price: float,
        outcome: str = "MANUAL_RECONCILED",
        exit_time: Optional[str] = None
    ) -> bool:
        """
        Explicitly closes and reconciles an open trade record in ml_trade_history.
        """
        db_path = get_db_path()
        conn = sqlite3.connect(db_path, timeout=15.0)
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("SELECT entry, direction FROM ml_trade_history WHERE id = ?", (trade_id,))
                row = cur.fetchone()
                if not row:
                    logger.warning(f"[PositionMonitor] Trade id {trade_id} not found for reconciliation.")
                    return False

                entry = float(row[0])
                direction = row[1]

                if direction == "BULLISH":
                    profit_pct = round(((exit_price - entry) / entry) * 100.0, 2)
                else:
                    profit_pct = round(((entry - exit_price) / entry) * 100.0, 2)

                t_exit = exit_time or datetime.now().isoformat()

                cur.execute("""
                    UPDATE ml_trade_history
                    SET status = 'CLOSED',
                        outcome = ?,
                        exit_price = ?,
                        exit_time = ?,
                        profit_pct = ?,
                        ideal_profit_pct = ?
                    WHERE id = ?
                """, (outcome, exit_price, t_exit, profit_pct, profit_pct, trade_id))

            # Invalidate cache
            _MONITOR_CACHE["data"] = None
            logger.info(f"[PositionMonitor] Reconciled trade {trade_id} with outcome {outcome} (PnL: {profit_pct}%)")
            return True
        except Exception as e:
            logger.error(f"[PositionMonitor] Failed to reconcile trade {trade_id}: {e}")
            return False
        finally:
            conn.close()

    @classmethod
    def run_defensive_sweep(cls, force_run: bool = False) -> Dict[str, Any]:
        """
        Executes multi-model risk audit for genuine active positions (PAPER_POSITION and LIVE_POSITION).
        Virtual recommendations (NOT_A_POSITION) are strictly skipped.
        """
        from app.analytics.autonomous_bot import active_trade_tracker
        active_trade_tracker(force_run=force_run)
        return cls.get_open_positions_summary(force_refresh=True)
