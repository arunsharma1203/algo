"""
QLIB VIRTUAL RECOMMENDATION TRACKER
===================================
Tracks generated Qlib recommendations honestly as virtual research signals.

Invariants:
- 100% physically isolated in `qlib_virtual_recommendations` table.
- 0 portfolio heat consumed (never touches `ml_trade_history` live positions).
- 0 broker execution (zero live broker orders).
- Strictly preserves provenance: model_id, model_hash, feature_version, engine='QLIB'.
"""

import os
import json
import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)


def init_qlib_tracking_schema():
    """Initializes dedicated Qlib virtual recommendation table."""
    db_file = get_db_path()
    conn = sqlite3.connect(db_file)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS qlib_virtual_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                strategy TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                target_price REAL NOT NULL,
                confidence REAL NOT NULL,
                status TEXT DEFAULT 'OPEN',
                outcome TEXT,
                pnl_pct REAL,
                exit_price REAL,
                exit_time TEXT,
                model_id TEXT NOT NULL,
                model_hash TEXT NOT NULL,
                feature_version TEXT NOT NULL,
                data_timestamp TEXT,
                fno_contract_info TEXT,
                engine TEXT DEFAULT 'QLIB'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_qlib_rec_strategy ON qlib_virtual_recommendations(strategy, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_qlib_rec_timestamp ON qlib_virtual_recommendations(timestamp)")
        conn.commit()
    finally:
        conn.close()


class QlibVirtualTracker:
    """
    Records and monitors Qlib virtual recommendations.
    """

    @classmethod
    def record_recommendation(
        cls,
        ticker: str,
        strategy: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        target_price: float,
        confidence: float = 75.0,
        model_id: str = "qlib_active",
        model_hash: str = "authoritative_hash",
        feature_version: str = "qlib_alpha158_v1",
        data_timestamp: Optional[str] = None,
        fno_contract_info: Optional[Dict[str, Any]] = None,
        raw_prediction: Optional[float] = None,
        recommendation_score: Optional[float] = None,
        rank_percentile: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Records a new virtual recommendation in authoritative ml_trade_history with engine='QLIB',
        ensuring common trade outcome tracking while keeping statistics separate.
        Also mirrors to qlib_virtual_recommendations table.
        """
        init_qlib_tracking_schema()
        db_file = get_db_path()
        conn = sqlite3.connect(db_file)
        now_iso = datetime.now().isoformat()

        fno_json = json.dumps(fno_contract_info) if fno_contract_info else None
        clean_strat = strategy.upper().strip()
        clean_dir = direction.upper().strip()
        is_bullish = clean_dir in ("BUY", "BULLISH", "LONG")

        # 1. Authoritative ml_trade_history persistence (position_type='NOT_A_POSITION', 0 heat)
        try:
            from app.api.ml_history import save_ml_trade
            explanation_payload = {
                "engine": "QLIB",
                "strategy": clean_strat,
                "model_id": model_id,
                "model_hash": model_hash,
                "feature_version": feature_version,
                "data_timestamp": data_timestamp,
                "fno_contract_info": fno_contract_info,
                "raw_prediction": raw_prediction,
                "recommendation_score": recommendation_score if recommendation_score is not None else raw_prediction,
                "rank_percentile": rank_percentile
            }
            save_ml_trade(
                ticker=ticker.upper().strip(),
                is_bullish=is_bullish,
                entry=round(float(entry_price), 2),
                sl=round(float(stop_loss), 2),
                tp1=round(float(target_price), 2),
                tp2=round(float(target_price) * 1.05, 2),
                confidence=round(float(confidence) * 100.0 if confidence <= 1.0 else float(confidence), 2),
                trade_type=clean_strat,
                explanation=explanation_payload,
                source="QLIB_SCANNER",
                position_type="NOT_A_POSITION",
                engine="QLIB",
                strategy=clean_strat,
                model_id=model_id,
                model_hash=model_hash,
                data_timestamp=data_timestamp or now_iso,
                instrument=ticker.upper().strip()
            )
        except Exception as e:
            logger.warning(f"[QlibVirtualTracker] ml_trade_history bridge warning: {e}")

        # 2. Mirror into qlib_virtual_recommendations
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO qlib_virtual_recommendations (
                    timestamp, ticker, strategy, direction,
                    entry_price, stop_loss, target_price, confidence,
                    status, model_id, model_hash, feature_version,
                    data_timestamp, fno_contract_info, engine
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, 'QLIB')
            """, (
                now_iso, ticker.upper().strip(), clean_strat, clean_dir,
                round(float(entry_price), 2), round(float(stop_loss), 2), round(float(target_price), 2),
                round(float(confidence), 4), model_id, model_hash, feature_version,
                data_timestamp or now_iso, fno_json
            ))
            conn.commit()
            rec_id = cursor.lastrowid

            return {
                "id": rec_id,
                "recommendation_id": f"qlib_rec_{rec_id}",
                "timestamp": now_iso,
                "ticker": ticker.upper().strip(),
                "strategy": clean_strat,
                "direction": clean_dir,
                "entry": round(float(entry_price), 2),
                "entry_price": round(float(entry_price), 2),
                "sl": round(float(stop_loss), 2),
                "stop_loss": round(float(stop_loss), 2),
                "tp1": round(float(target_price), 2),
                "target_price": round(float(target_price), 2),
                "confidence": round(float(confidence), 4),
                "raw_prediction": raw_prediction,
                "recommendation_score": recommendation_score if recommendation_score is not None else raw_prediction,
                "rank_percentile": rank_percentile,
                "data_timestamp": data_timestamp or now_iso,
                "status": "OPEN",
                "model_id": model_id,
                "model_hash": model_hash,
                "engine": "QLIB",
                "position_type": "NOT_A_POSITION"
            }
        finally:
            conn.close()

    @classmethod
    def reconcile_open_recommendations(cls, force_refresh: bool = False) -> int:
        """
        Reconciles open Qlib virtual recommendations against market prices using
        the authoritative common outcome tracking engine.
        Returns count of active Qlib recommendations monitored.
        """
        try:
            from app.api.ml_history import evaluate_ml_history
            trades = evaluate_ml_history(force_refresh=force_refresh, engine="QLIB")
            qlib_recs = [t for t in trades if t.get("engine") == "QLIB" or t.get("source") == "QLIB_SCANNER"]

            # Sync finalized outcomes back to qlib_virtual_recommendations table
            db_file = get_db_path()
            conn = sqlite3.connect(db_file)
            try:
                for q in qlib_recs:
                    if q.get("status") == "CLOSED" and q.get("outcome") not in (None, "", "OPEN"):
                        strat = q.get("strategy") or q.get("trade_type")
                        clean_dir = "BUY" if q.get("direction") in ("BUY", "BULLISH") else "SELL"
                        q_ts = q.get("timestamp") or ""
                        conn.execute("""
                            UPDATE qlib_virtual_recommendations
                            SET status = 'CLOSED', outcome = ?, pnl_pct = ?, exit_price = ?, exit_time = ?
                            WHERE (ticker = ? OR ticker = ?) 
                              AND strategy = ?
                              AND direction = ?
                              AND date(timestamp) = date(?)
                              AND (status = 'OPEN' OR outcome IS NULL OR outcome = 'OPEN')
                        """, (
                            q.get("outcome"), q.get("profit_pct", 0.0),
                            q.get("exit_price"), q.get("exit_time"),
                            q.get("ticker"), q.get("ticker", "").replace(".NS", ""),
                            strat, clean_dir, q_ts
                        ))
                conn.commit()
            finally:
                conn.close()

            return len(qlib_recs)
        except Exception as e:
            logger.error(f"[QlibVirtualTracker] Reconciliation error: {e}")
            return 0

    @classmethod
    def get_recommendations(
        cls,
        strategy: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Queries recorded Qlib virtual recommendations from authoritative ml_trade_history.
        Falls back to qlib_virtual_recommendations if needed.
        Supports filtering by strategy and status, plus offset pagination.
        """
        init_qlib_tracking_schema()
        db_file = get_db_path()
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        try:
            # Query authoritative ml_trade_history first
            q = "SELECT * FROM ml_trade_history WHERE (engine = 'QLIB' OR source = 'QLIB_SCANNER')"
            params = []
            if strategy and strategy.upper() != "ALL":
                q += " AND (strategy = ? OR trade_type = ?)"
                params.extend([strategy.upper(), strategy.upper()])
            if status and status.upper() != "ALL":
                if status.upper() == "OPEN":
                    q += " AND status = 'OPEN'"
                elif status.upper() == "CLOSED":
                    q += " AND status != 'OPEN'"
            q += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(q, params).fetchall()
            if not rows and offset == 0:
                # Check qlib_virtual_recommendations
                q2 = "SELECT * FROM qlib_virtual_recommendations WHERE 1=1"
                p2 = []
                if strategy and strategy.upper() != "ALL":
                    q2 += " AND strategy = ?"
                    p2.append(strategy.upper())
                if status and status.upper() != "ALL":
                    if status.upper() == "OPEN":
                        q2 += " AND status = 'OPEN'"
                    elif status.upper() == "CLOSED":
                        q2 += " AND status != 'OPEN'"
                q2 += " ORDER BY id DESC LIMIT ? OFFSET ?"
                p2.extend([limit, offset])
                rows = conn.execute(q2, p2).fetchall()

            res = []
            now_dt = datetime.now()
            for r in rows:
                d = dict(r)
                rec_id = d.get("id")
                strat = d.get("strategy") or d.get("trade_type") or "SWING"
                ticker_sym = d.get("ticker")
                direction = d.get("direction") or "BUY"
                is_bullish = direction in ("BUY", "BULLISH", "LONG")
                entry_val = d.get("entry") if d.get("entry") is not None else d.get("entry_price")
                sl_val = d.get("sl") if d.get("sl") is not None else d.get("stop_loss")
                tp_val = d.get("tp1") if d.get("tp1") is not None else d.get("target_price")
                prof_val = d.get("profit_pct") if d.get("profit_pct") is not None else d.get("pnl_pct")

                expl = {}
                fno_info = None
                if d.get("fno_contract_info"):
                    try:
                        fno_info = json.loads(d["fno_contract_info"]) if isinstance(d["fno_contract_info"], str) else d["fno_contract_info"]
                    except Exception:
                        pass
                elif d.get("explanation"):
                    try:
                        expl = json.loads(d["explanation"]) if isinstance(d["explanation"], str) else (d["explanation"] or {})
                        fno_info = expl.get("fno_contract_info")
                    except Exception:
                        expl = {}
                elif d.get("meta_json"):
                    try:
                        expl = json.loads(d["meta_json"]) if isinstance(d["meta_json"], str) else (d["meta_json"] or {})
                        fno_info = expl.get("fno_contract_info")
                    except Exception:
                        expl = {}

                raw_prediction = d.get("raw_prediction") or expl.get("raw_prediction") or expl.get("model_score")
                recommendation_score = d.get("recommendation_score") or expl.get("recommendation_score") or expl.get("model_score") or raw_prediction
                rank_percentile = d.get("rank_percentile") or expl.get("rank_percentile")
                data_timestamp = d.get("data_timestamp") or expl.get("data_timestamp") or d.get("timestamp")

                curr_price = d.get("current_price")
                if (curr_price is None or curr_price == entry_val) and ticker_sym:
                    try:
                        ohlc_row = conn.execute(
                            "SELECT close FROM ohlcv WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                            (ticker_sym,)
                        ).fetchone()
                        if ohlc_row and ohlc_row[0]:
                            curr_price = round(float(ohlc_row[0]), 2)
                    except Exception:
                        pass

                if curr_price is None:
                    curr_price = entry_val

                # Calculate unrealized P&L %
                unrealized_pnl_pct = prof_val
                if unrealized_pnl_pct is None and entry_val and curr_price:
                    if strat == "FNO":
                        # For long options, P&L is direct premium change
                        unrealized_pnl_pct = round(((curr_price - entry_val) / entry_val) * 100.0, 2)
                    elif is_bullish:
                        unrealized_pnl_pct = round(((curr_price - entry_val) / entry_val) * 100.0, 2)
                    else:
                        unrealized_pnl_pct = round(((entry_val - curr_price) / entry_val) * 100.0, 2)

                # Determine pnl_status badge
                outcome_val = d.get("outcome") if d.get("outcome") != "OPEN" else None
                if outcome_val in ("TARGET MET", "WIN"):
                    pnl_status = "WIN"
                elif outcome_val in ("SL HIT", "LOSS"):
                    pnl_status = "LOSS"
                elif outcome_val in ("SQUARED OFF (3:15 PM)", "EXPIRED", "SWING_HORIZON_REACHED", "CLOSED"):
                    pnl_status = "CLOSED"
                elif unrealized_pnl_pct is not None and unrealized_pnl_pct > 0:
                    pnl_status = "PROFIT"
                elif unrealized_pnl_pct is not None and unrealized_pnl_pct < 0:
                    pnl_status = "LOSS"
                else:
                    pnl_status = "FLAT" if d.get("status") == "OPEN" else "CLOSED"

                # Calculate trading days held and days remaining for Swing
                days_held = 0
                days_remaining = 5
                try:
                    from app.analytics.nse_holidays import count_nse_trading_days
                    entry_raw = d.get("timestamp") or d.get("data_timestamp") or ""
                    if entry_raw:
                        entry_date = datetime.fromisoformat(entry_raw[:10]).date()
                        days_held = count_nse_trading_days(entry_date, now_dt.date())
                        days_remaining = max(0, 5 - days_held)
                except Exception:
                    pass

                # F&O specific field extraction & risk validation
                underlying = ticker_sym
                spot_price = entry_val
                opt_action = "BUY " + ("CE" if is_bullish else "PE") if strat == "FNO" else None
                opt_type = ("CE" if is_bullish else "PE") if strat == "FNO" else None
                strike = None
                expiry = None
                lot_size = 0
                entry_premium = entry_val
                current_premium = curr_price
                risk_valid = True

                if fno_info and isinstance(fno_info, dict):
                    underlying = fno_info.get("underlying") or ticker_sym
                    spot_price = fno_info.get("spot_price") or entry_val
                    opt_action = fno_info.get("action") or opt_action
                    opt_type = fno_info.get("option_type") or opt_type
                    strike = fno_info.get("strike")
                    expiry = fno_info.get("expiry")
                    lot_size = int(fno_info.get("lot_size") or 0)
                    entry_premium = fno_info.get("entry_premium") or entry_val
                    current_premium = curr_price
                    risk_valid = fno_info.get("risk_structure_valid", True)
                elif strat == "FNO":
                    from app.qlib_engine.fno_universe import get_fno_lot_size
                    lot_size = get_fno_lot_size(ticker_sym)

                # Validate long option risk structure invariant: SL < Entry < Target
                if strat == "FNO" and sl_val and tp_val and entry_val:
                    if not (sl_val < entry_val < tp_val):
                        risk_valid = False

                # P&L in Rupees (where lot_size is available)
                pnl_rupees = None
                if strat == "FNO" and lot_size > 0 and entry_val and curr_price:
                    pnl_rupees = round((curr_price - entry_val) * lot_size, 2)

                res.append({
                    "id": rec_id,
                    "recommendation_id": f"qlib_rec_{rec_id}",
                    "engine": "QLIB",
                    "strategy": strat,
                    "ticker": ticker_sym,
                    "underlying": underlying,
                    "spot_price": spot_price,
                    "direction": direction,
                    "action": opt_action or direction,
                    "instrument": "OPTION" if strat == "FNO" else "EQUITY",
                    "option_type": opt_type,
                    "strike": strike,
                    "expiry": expiry,
                    "lot_size": lot_size,
                    "entry": entry_val,
                    "entry_price": entry_val,
                    "entry_premium": entry_premium,
                    "current_price": curr_price,
                    "current_premium": current_premium,
                    "sl": sl_val,
                    "stop_loss": sl_val,
                    "tp1": tp_val,
                    "target": tp_val,
                    "target_price": tp_val,
                    "confidence": d.get("confidence"),
                    "raw_prediction": raw_prediction,
                    "recommendation_score": recommendation_score,
                    "rank_percentile": rank_percentile,
                    "data_timestamp": data_timestamp,
                    "status": d.get("status", "OPEN"),
                    "outcome": outcome_val,
                    "profit_pct": prof_val,
                    "pnl_pct": prof_val,
                    "pnl_rupees": pnl_rupees,
                    "unrealized_pnl_pct": unrealized_pnl_pct,
                    "pnl_status": pnl_status,
                    "risk_structure_valid": risk_valid,
                    "contract_status": "AVAILABLE" if risk_valid else "INVALID_RISK_STRUCTURE",
                    "timestamp": d.get("timestamp"),
                    "entry_date": str(d.get("timestamp"))[:10],
                    "days_held": days_held,
                    "days_remaining": days_remaining,
                    "horizon_trading_days": 5,
                    "square_off_time": "15:15–15:30 IST" if strat == "INTRADAY" else None,
                    "exit_price": d.get("exit_price"),
                    "exit_time": d.get("exit_time"),
                    "model_id": d.get("model_id"),
                    "model_hash": d.get("model_hash"),
                    "fno_contract_info": fno_info,
                    "position_type": "NOT_A_POSITION"
                })
            return res
        finally:
            conn.close()

    @classmethod
    def get_recommendations_paginated(
        cls,
        strategy: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        limit: int = 20
    ) -> Dict[str, Any]:
        """
        Returns full server-side filtered and paginated response for Qlib Virtual Recommendations.
        Computes accurate live category counts across ALL, OPEN, CLOSED, SWING, INTRADAY, and FNO.
        """
        import math
        init_qlib_tracking_schema()

        # Debounced passive reconciliation for active recommendations
        try:
            cls.reconcile_open_recommendations(force_refresh=False)
        except Exception as e:
            logger.debug(f"[QlibVirtualTracker] Passive reconcile: {e}")

        db_file = get_db_path()
        conn = sqlite3.connect(db_file)
        try:
            base_from = "FROM ml_trade_history WHERE (engine = 'QLIB' OR source = 'QLIB_SCANNER')"

            # 1. Compute true counts across all categories
            count_all = conn.execute(f"SELECT COUNT(*) {base_from}").fetchone()[0]
            count_open = conn.execute(f"SELECT COUNT(*) {base_from} AND status = 'OPEN'").fetchone()[0]
            count_closed = conn.execute(f"SELECT COUNT(*) {base_from} AND status != 'OPEN'").fetchone()[0]
            count_swing = conn.execute(f"SELECT COUNT(*) {base_from} AND (strategy = 'SWING' OR trade_type = 'SWING')").fetchone()[0]
            count_intraday = conn.execute(f"SELECT COUNT(*) {base_from} AND (strategy = 'INTRADAY' OR trade_type = 'INTRADAY')").fetchone()[0]
            count_fno = conn.execute(f"SELECT COUNT(*) {base_from} AND (strategy = 'FNO' OR trade_type = 'FNO')").fetchone()[0]

            # 2. Compute filtered count for active query
            q_filt = f"SELECT COUNT(*) {base_from}"
            p_filt = []
            if strategy and strategy.upper() != "ALL":
                q_filt += " AND (strategy = ? OR trade_type = ?)"
                p_filt.extend([strategy.upper(), strategy.upper()])
            if status and status.upper() != "ALL":
                if status.upper() == "OPEN":
                    q_filt += " AND status = 'OPEN'"
                elif status.upper() == "CLOSED":
                    q_filt += " AND status != 'OPEN'"

            filtered_count = conn.execute(q_filt, p_filt).fetchone()[0]
            total_pages = max(1, math.ceil(filtered_count / max(1, limit)))
            safe_page = min(max(1, page), total_pages)
            offset = (safe_page - 1) * limit

            recs = cls.get_recommendations(
                strategy=strategy,
                status=status,
                limit=limit,
                offset=offset
            )

            return {
                "engine": "QLIB",
                "total_count": count_all,
                "filtered_count": filtered_count,
                "page": safe_page,
                "limit": limit,
                "total_pages": total_pages,
                "counts": {
                    "ALL": count_all,
                    "OPEN": count_open,
                    "CLOSED": count_closed,
                    "SWING": count_swing,
                    "INTRADAY": count_intraday,
                    "FNO": count_fno
                },
                "recommendations": recs,
                "performance": cls.get_performance_summary(strategy=strategy if strategy != 'ALL' else None)
            }
        finally:
            conn.close()

    @classmethod
    def get_performance_summary(cls, strategy: Optional[str] = None) -> Dict[str, Any]:
        """
        Computes performance metrics for Qlib recommendations from ml_trade_history.
        If no completed trades exist, returns honest N/A rather than 0% accuracy.
        """
        init_qlib_tracking_schema()
        db_file = get_db_path()
        conn = sqlite3.connect(db_file)
        try:
            base_q = "FROM ml_trade_history WHERE (engine = 'QLIB' OR source = 'QLIB_SCANNER')"
            params = []
            if strategy:
                base_q += " AND (strategy = ? OR trade_type = ?)"
                params.extend([strategy.upper(), strategy.upper()])

            total_recs = conn.execute(f"SELECT COUNT(*) {base_q}", params).fetchone()[0]
            if total_recs == 0:
                # Check qlib_virtual_recommendations
                base_q = "FROM qlib_virtual_recommendations WHERE 1=1"
                params = []
                if strategy:
                    base_q += " AND strategy = ?"
                    params.append(strategy.upper())
                total_recs = conn.execute(f"SELECT COUNT(*) {base_q}", params).fetchone()[0]

            open_recs = conn.execute(f"SELECT COUNT(*) {base_q} AND status = 'OPEN'", params).fetchone()[0]
            closed_recs = conn.execute(f"SELECT COUNT(*) {base_q} AND status != 'OPEN' AND outcome IS NOT NULL AND outcome != '' AND outcome != 'OPEN'", params).fetchone()[0]

            if closed_recs == 0:
                return {
                    "strategy": strategy or "ALL",
                    "total_recommendations": total_recs,
                    "open_trades": open_recs,
                    "completed_trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "win_rate": None,
                    "profit_factor": None,
                    "net_pnl_pct": None,
                    "avg_trade_pnl_pct": None,
                    "display_status": "N/A — INSUFFICIENT DATA (0 completed trades)"
                }

            row = conn.execute(f"""
                SELECT 
                    COUNT(CASE WHEN profit_pct > 0 THEN 1 END) as wins,
                    COUNT(CASE WHEN profit_pct < 0 THEN 1 END) as losses,
                    AVG(profit_pct) as avg_pnl,
                    SUM(profit_pct) as total_pnl,
                    SUM(CASE WHEN profit_pct > 0 THEN profit_pct ELSE 0 END) as gross_win,
                    ABS(SUM(CASE WHEN profit_pct < 0 THEN profit_pct ELSE 0 END)) as gross_loss
                {base_q} AND status != 'OPEN' AND outcome IS NOT NULL AND outcome != '' AND outcome != 'OPEN'
            """, params).fetchone()

            wins = row[0] or 0
            losses = row[1] or 0
            win_rate = round((wins / closed_recs) * 100.0, 2)
            gross_win = row[4] or 0.0
            gross_loss = row[5] or 0.0
            pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_win > 0 else 1.0)

            return {
                "strategy": strategy or "ALL",
                "total_recommendations": total_recs,
                "open_trades": open_recs,
                "completed_trades": closed_recs,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "profit_factor": pf,
                "net_pnl_pct": round(row[3] or 0.0, 2),
                "avg_trade_pnl_pct": round(row[2] or 0.0, 2),
                "display_status": "ACTIVE_EVALUATION"
            }
        finally:
            conn.close()

    @classmethod
    def get_recommendations_count(
        cls,
        strategy: Optional[str] = None,
        status: Optional[str] = None
    ) -> int:
        """
        Returns count of recommendations matching strategy and status.
        Queries authoritative ml_trade_history, falling back to qlib_virtual_recommendations.
        """
        init_qlib_tracking_schema()
        db_file = get_db_path()
        conn = sqlite3.connect(db_file)
        try:
            cnt = 0
            try:
                cols = [c[1] for c in conn.execute("PRAGMA table_info(ml_trade_history)").fetchall()]
                where_clauses = []
                params = []

                engine_parts = []
                if "engine" in cols:
                    engine_parts.append("engine = 'QLIB'")
                if "source" in cols:
                    engine_parts.append("source = 'QLIB_SCANNER'")
                if engine_parts:
                    where_clauses.append(f"({' OR '.join(engine_parts)})")

                if strategy and strategy.upper() != "ALL":
                    strat_parts = []
                    if "strategy" in cols:
                        strat_parts.append("strategy = ?")
                        params.append(strategy.upper())
                    if "trade_type" in cols:
                        strat_parts.append("trade_type = ?")
                        params.append(strategy.upper())
                    if strat_parts:
                        where_clauses.append(f"({' OR '.join(strat_parts)})")

                if status and status.upper() != "ALL" and "status" in cols:
                    if status.upper() == "OPEN":
                        where_clauses.append("status = 'OPEN'")
                    elif status.upper() == "CLOSED":
                        where_clauses.append("status != 'OPEN'")

                where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
                cnt = conn.execute(f"SELECT COUNT(*) FROM ml_trade_history {where_sql}", params).fetchone()[0]
            except Exception:
                cnt = 0

            if cnt == 0:
                base_q2 = "FROM qlib_virtual_recommendations WHERE 1=1"
                params2 = []
                if strategy and strategy.upper() != "ALL":
                    base_q2 += " AND strategy = ?"
                    params2.append(strategy.upper())
                if status and status.upper() != "ALL":
                    if status.upper() == "OPEN":
                        base_q2 += " AND status = 'OPEN'"
                    elif status.upper() == "CLOSED":
                        base_q2 += " AND status != 'OPEN'"
                try:
                    cnt = conn.execute(f"SELECT COUNT(*) {base_q2}", params2).fetchone()[0]
                except Exception:
                    cnt = 0
            return cnt
        finally:
            conn.close()

    @classmethod
    def get_recommendation_by_id(cls, rec_id: Any) -> Optional[Dict[str, Any]]:
        """
        Finds recommendation by ID from authoritative ml_trade_history
        or mirrored qlib_virtual_recommendations table.
        """
        if isinstance(rec_id, dict):
            id_val = rec_id.get("id")
        else:
            id_val = rec_id
        if id_val is None:
            return None

        recs = cls.get_recommendations(limit=2000)
        for r in recs:
            if str(r.get("id")) == str(id_val) or str(r.get("recommendation_id")) == str(id_val):
                return r

        init_qlib_tracking_schema()
        db_file = get_db_path()
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM qlib_virtual_recommendations WHERE id = ?", (id_val,)).fetchone()
            if row:
                d = dict(row)
                d["position_type"] = "NOT_A_POSITION"
                return d
            row2 = conn.execute("SELECT * FROM ml_trade_history WHERE id = ?", (id_val,)).fetchone()
            if row2:
                d = dict(row2)
                return d
            return None
        finally:
            conn.close()
