"""
Dedicated routes for Option Sniffer surveillance dashboard:
  - /api/option-sniffer/status
  - /api/option-sniffer/candidates
  - /api/option-sniffer/signals
  - /api/option-sniffer/paper-trades
  - /api/option-sniffer/pnl
  - /api/option-sniffer/calibration
  - /api/option-sniffer/scan
"""

import logging
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Query

from app.analytics.option_sniffer.option_sniffer_orchestrator import get_option_sniffer_orchestrator
from app.analytics.option_sniffer.option_db import OptionDB

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/option-sniffer", tags=["Option Sniffer"])


@router.get("/status")
def get_sniffer_status() -> Dict[str, Any]:
    """Provides operational status of Option Sniffer feed, model, and active trades."""
    try:
        orch = get_option_sniffer_orchestrator()
        if not orch._is_running:
            orch.start()
        return orch.get_status()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/candidates")
def get_sniffer_candidates() -> List[Dict[str, Any]]:
    """Returns active monitored candidate option contracts with live order book quotes."""
    try:
        orch = get_option_sniffer_orchestrator()
        return orch.get_candidates()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get candidates: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals")
def get_sniffer_signals(
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = Query(None)
) -> List[Dict[str, Any]]:
    """Returns historical and active signals from the state machine."""
    try:
        return OptionDB.get_recent_signals(limit=limit, status=status)
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get signals: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/paper-trades")
def get_sniffer_paper_trades() -> Dict[str, Any]:
    """Returns active open paper positions and closed trade performance."""
    try:
        orch = get_option_sniffer_orchestrator()
        return orch.get_paper_trades()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get paper trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pnl")
def get_sniffer_pnl() -> Dict[str, Any]:
    """Returns cumulative and today's realized/unrealized paper P&L metrics."""
    try:
        return OptionDB.get_pnl_summary()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get PnL: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/calibration")
def get_sniffer_calibration() -> Dict[str, Any]:
    """Returns empirical calibration curves, Brier score, and feature ablation results."""
    try:
        orch = get_option_sniffer_orchestrator()
        return orch.get_calibration_stats()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get calibration stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/scan")
def trigger_sniffer_scan() -> Dict[str, Any]:
    """Triggers an immediate evaluation sweep across all active option contracts."""
    try:
        orch = get_option_sniffer_orchestrator()
        results = orch.run_surveillance_sweep()
        return {"status": "SUCCESS", "results": results}
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to run scan sweep: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------------
# PHASE 1: READ-ONLY LIVE TERMINAL & ADAPTER TELEMETRY
# (Zero live order placement capability)
# -------------------------------------------------------------------------

@router.get("/live/readiness")
def get_live_account_readiness(force_refresh: bool = False) -> Dict[str, Any]:
    """
    Returns Upstox account readiness telemetry via the Mumbai static IPv6 proxy:
    F&O segment status, wallet available margin, and static IP whitelist verification.
    """
    try:
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        adapter = get_option_live_adapter()
        return adapter.fetch_account_readiness(force_refresh=force_refresh)
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to fetch live readiness: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/live/allocation")
def get_live_allocation_simulation(
    custom_capital: Optional[float] = Query(None, ge=0.0),
    today_only: bool = Query(True)
) -> Dict[str, Any]:
    """
    Simulates dynamic knapsack capital allocation based on available wallet balance
    or custom simulated capital, deducting 10% safety buffer for statutory charges and slippage.
    """
    try:
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        adapter = get_option_live_adapter()
        return adapter.simulate_dynamic_capital_allocation(custom_capital=custom_capital, today_only=today_only)
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to run allocation simulation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/live/paper-mirror")
def get_live_paper_mirror() -> Dict[str, Any]:
    """
    Returns read-only snapshot of active paper positions queried via SQLite query_only=ON.
    """
    try:
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        adapter = get_option_live_adapter()
        return adapter.get_read_only_paper_trades()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to query read-only paper mirror: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------------
# PHASE 2: ORDER INTENT SIMULATION & FAILURE RECOVERY ENDPOINTS
# -------------------------------------------------------------------------

@router.post("/live/simulate-order")
def simulate_order_intent(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validates risk limits, quote freshness, and slippage, creates an idempotent
    order intent UUID, and simulates execution under various exchange scenarios
    (CLEAN_FILL, PARTIAL_FILL, RMS_REJECTION, TIMEOUT_AMBIGUOUS).
    """
    try:
        from app.analytics.option_sniffer.option_live_order_intent import get_order_intent_engine
        engine = get_order_intent_engine()

        signal_payload = payload.get("signal", {})
        live_quote = payload.get("quote", {"ask": 100.0, "bid": 99.5, "quote_age_ms": 200})
        deployable_capital = float(payload.get("deployable_capital", 15000.0))
        scenario = payload.get("scenario", "CLEAN_FILL")

        enforce_regime = payload.get("enforce_regime_gate", True)
        ok, reason, intent = engine.create_and_validate_intent(
            signal_payload=signal_payload,
            live_quote=live_quote,
            deployable_capital=deployable_capital,
            enforce_regime_gate=enforce_regime
        )

        if not ok:
            return {"status": "BLOCKED", "reason": reason, "intent": None}

        sim_result = engine.simulate_order_execution(
            order_intent_id=intent["order_intent_id"],
            scenario=scenario
        )

        return {
            "status": "SUCCESS",
            "validation": reason,
            "order_intent": intent,
            "execution_result": sim_result
        }
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to simulate order: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/live/simulate-sos")
def simulate_sos_exit(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Triggers emergency SOS square-off state machine on an order intent:
    locks exit mutex, simulates marketable exit, confirms broker qty == 0.
    """
    try:
        from app.analytics.option_sniffer.option_live_order_intent import get_order_intent_engine
        engine = get_order_intent_engine()

        order_intent_id = payload.get("order_intent_id", "")
        current_bid = float(payload.get("current_bid_price", 100.0))

        if not order_intent_id:
            raise HTTPException(status_code=400, detail="Missing order_intent_id")

        return engine.trigger_sos_panic_exit(order_intent_id, current_bid_price=current_bid)
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to trigger SOS panic exit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/live/order-intents")
def get_live_order_intents(limit: int = Query(20, ge=1, le=100)) -> List[Dict[str, Any]]:
    """Returns historical and active order intents from the isolated order intent ledger, enriched with live MTM."""
    try:
        import sqlite3
        from app.data.historical_data_layer import get_db_path
        conn = sqlite3.connect(get_db_path(), timeout=5.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action,
                   lot_size, target_quantity, filled_quantity, limit_price, average_fill_price,
                   state, broker_order_id, stop_loss_trigger, stop_loss_limit, broker_sl_order_id,
                   exit_state, exit_fill_price, gross_pnl, statutory_charges, realized_pnl, rejection_reason, created_at, updated_at
            FROM option_live_order_intents
            ORDER BY id DESC LIMIT ?;
        """, (limit,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        try:
            from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
            stream_mgr = OptionStreamManager()
            for r in rows:
                if r.get("state") == "FILLED" and r.get("exit_state") != "CONFIRMED_FLAT":
                    ikey = r.get("instrument_key")
                    quote = stream_mgr.get_option_quote(ikey) if ikey else None
                    if quote:
                        current_bid = float(quote.get("bid") or quote.get("ltp") or 0.0)
                        entry_fill = float(r.get("average_fill_price") or r.get("limit_price") or 0.0)
                        qty = int(r.get("filled_quantity") or r.get("target_quantity") or 0)
                        r["current_bid"] = round(current_bid, 2)
                        if current_bid > 0 and entry_fill > 0 and qty > 0:
                            gross_mtm = (current_bid - entry_fill) * qty
                            r["unrealized_pnl"] = round(gross_mtm - 55.0, 2)
                            r["unrealized_pnl_pct"] = round(((current_bid - entry_fill) / entry_fill) * 100.0, 2)
        except Exception:
            pass

        return rows
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to fetch order intents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------------
# PHASE 3: 1-DAY EPHEMERAL ARMING, PIN VERIFICATION & SOS PANIC EXIT
# -------------------------------------------------------------------------

@router.get("/live/status")
def get_live_operational_status() -> Dict[str, Any]:
    """
    Returns live operational status: 1-day arming flag, session cutoff,
    dynamic 4-digit confirmation PIN, pre-flight readiness checklist,
    and active live positions.
    """
    try:
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        adapter = get_option_live_adapter()
        return adapter.get_live_status()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get live status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/live/pnl-summary")
def get_live_broker_pnl_summary() -> Dict[str, Any]:
    """Returns today's real broker P&L summary (realized, unrealized MTM, net after fees)."""
    try:
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        adapter = get_option_live_adapter()
        return adapter.get_live_pnl_summary()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to get live PnL summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/live/arm")
def arm_real_trading_today(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Arms real trading on Upstox for today after validating the 4-digit security PIN
    and passing all pre-flight checks (F&O segment, wallet margin, static IP).
    Fails closed back to Paper Mode if ANY check fails.
    """
    try:
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        adapter = get_option_live_adapter()
        pin = str(payload.get("pin", ""))
        ok, msg, readiness = adapter.arm_live_trading(pin)
        return {
            "status": "ARMED" if ok else "BLOCKED",
            "is_live_armed": ok,
            "message": msg,
            "readiness": readiness
        }
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to arm live trading: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/live/disarm")
def disarm_real_trading() -> Dict[str, Any]:
    """
    Disarms real trading and reverts back to Paper Mode immediately.
    """
    try:
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        adapter = get_option_live_adapter()
        ok, msg = adapter.disarm_live_trading()
        return {
            "status": "DISARMED",
            "is_live_armed": False,
            "message": msg
        }
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to disarm live trading: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/live/sos-panic-exit")
def execute_sos_panic_exit_all() -> Dict[str, Any]:
    """
    Emergency SOS Button: Cancels all pending orders, flattens all open positions
    at market, disarms live trading, and confirms broker quantity == 0.
    """
    try:
        from app.analytics.option_sniffer.option_live_adapter import get_option_live_adapter
        adapter = get_option_live_adapter()
        return adapter.execute_sos_panic_exit()
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to execute SOS panic exit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/index-charts")
def get_sniffer_index_charts() -> Dict[str, Any]:
    """
    Returns authoritative regular-session (09:15–15:30 IST) 1m and 5m OHLCV + Session VWAP
    series, Opening Range (09:15–09:30) High/Low, and Day High/Low for NIFTY and BANKNIFTY
    from continuous_index_ohlcv, along with market-hours status.
    """
    try:
        from datetime import datetime, time as dtime
        import pytz

        now_ist = datetime.now(pytz.timezone("Asia/Kolkata")).replace(tzinfo=None)
        is_market_open = bool(
            now_ist.weekday() < 5 and dtime(9, 15) <= now_ist.time() <= dtime(15, 30)
        )

        conn = OptionDB.get_connection()
        cur = conn.cursor()
        underlyings_payload: Dict[str, Any] = {}

        for ticker in ("NIFTY", "BANKNIFTY"):
            cur.execute(
                """
                SELECT substr(replace(timestamp, 'T', ' '), 1, 10) AS sdate
                FROM continuous_index_ohlcv
                WHERE ticker = ?
                  AND substr(replace(timestamp, 'T', ' '), 12, 8) BETWEEN '09:15:00' AND '15:30:00'
                  AND COALESCE(is_complete, 1) = 1
                  AND COALESCE(is_synthetic, 0) = 0
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (ticker,),
            )
            row = cur.fetchone()
            session_date = str(row[0]) if row and row[0] else now_ist.strftime("%Y-%m-%d")

            cur.execute(
                """
                SELECT substr(replace(timestamp, 'T', ' '), 1, 19) AS ts,
                       open, high, low, close, COALESCE(volume, 1) AS volume
                FROM continuous_index_ohlcv
                WHERE ticker = ?
                  AND substr(replace(timestamp, 'T', ' '), 1, 10) = ?
                  AND substr(replace(timestamp, 'T', ' '), 12, 8) BETWEEN '09:15:00' AND '15:30:00'
                  AND COALESCE(is_complete, 1) = 1
                  AND COALESCE(is_synthetic, 0) = 0
                ORDER BY ts ASC
                """,
                (ticker, session_date),
            )
            raw_rows = cur.fetchall()

            bars_1m: List[Dict[str, Any]] = []
            bars_5m: List[Dict[str, Any]] = []
            cum_pv = 0.0
            cum_v = 0.0
            orb_high = 0.0
            orb_low = float("inf")
            day_high = 0.0
            day_low = float("inf")

            bucket_map: Dict[str, Dict[str, Any]] = {}
            bucket_order: List[str] = []

            for r in raw_rows:
                ts_str = str(r[0])
                o = float(r[1] or r[4] or 0.0)
                h = float(r[2] or o)
                l = float(r[3] or o)
                c = float(r[4] or o)
                v = max(1.0, float(r[5] or 1.0))
                if c <= 0:
                    continue

                tp = (h + l + c) / 3.0
                cum_pv += tp * v
                cum_v += v
                vwap_val = round(cum_pv / cum_v, 2) if cum_v > 0 else round(c, 2)

                hhmm = ts_str[11:16] if len(ts_str) >= 16 else ts_str
                if "09:15" <= hhmm <= "09:29":
                    if h > orb_high:
                        orb_high = h
                    if l < orb_low:
                        orb_low = l

                if h > day_high:
                    day_high = h
                if l < day_low:
                    day_low = l

                bars_1m.append({
                    "time": ts_str,
                    "open": round(o, 2),
                    "high": round(h, 2),
                    "low": round(l, 2),
                    "close": round(c, 2),
                    "volume": round(v, 2),
                    "vwap": vwap_val,
                })

                # 5-minute bucket aggregation (e.g. 09:15..09:19 -> 09:15)
                try:
                    hr = int(ts_str[11:13])
                    mn = (int(ts_str[14:16]) // 5) * 5
                    b_ts = f"{ts_str[:10]} {hr:02d}:{mn:02d}:00"
                except Exception:
                    b_ts = ts_str

                if b_ts not in bucket_map:
                    bucket_order.append(b_ts)
                    bucket_map[b_ts] = {
                        "time": b_ts,
                        "open": round(o, 2),
                        "high": round(h, 2),
                        "low": round(l, 2),
                        "close": round(c, 2),
                        "volume": round(v, 2),
                        "vwap": vwap_val,
                    }
                else:
                    b_obj = bucket_map[b_ts]
                    b_obj["high"] = round(max(b_obj["high"], h), 2)
                    b_obj["low"] = round(min(b_obj["low"], l), 2)
                    b_obj["close"] = round(c, 2)
                    b_obj["volume"] = round(b_obj["volume"] + v, 2)
                    b_obj["vwap"] = vwap_val

            bars_5m = [bucket_map[k] for k in bucket_order]
            day_open = bars_1m[0]["open"] if bars_1m else 0.0
            day_close = bars_1m[-1]["close"] if bars_1m else 0.0
            session_vwap = bars_1m[-1]["vwap"] if bars_1m else 0.0

            underlyings_payload[ticker] = {
                "session_date": session_date,
                "day_open": round(day_open, 2),
                "day_high": round(day_high if day_high > 0 else day_close, 2),
                "day_low": round(day_low if day_low < float("inf") else day_close, 2),
                "day_close": round(day_close, 2),
                "orb_high": round(orb_high if orb_high > 0 else day_high, 2),
                "orb_low": round(orb_low if orb_low < float("inf") else day_low, 2),
                "session_vwap": round(session_vwap, 2),
                "bars_1m": bars_1m,
                "bars_5m": bars_5m,
            }

        conn.close()
        return {
            "is_market_open": is_market_open,
            "server_time_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
            "underlyings": underlyings_payload,
        }
    except Exception as e:
        logger.error(f"[OptionSnifferAPI] Failed to build index charts: {e}")
        raise HTTPException(status_code=500, detail=str(e))




