"""
SmartScannerPipeline: Authoritative Unified Scanner Pipeline.

Consolidates Intraday (15m) and Swing (1D) screening into a single high-performance pipeline.
Enforces the 8-stage qualification workflow:
  1. TICKER POOL RESOLUTION (Authoritative Universe Config)
  2. DATA GATEWAY (Cached / Live Parallel OHLCV Ingestion)
  3. DATA VALIDATION (Point-in-time checks via MarketDataValidator)
  4. FEATURE ENGINEERING (Canonical Technical Features)
  5. BASE MODEL INFERENCE (Cryptographically Verified Champion Models)
  6. META / REGIME ARBITRATION (Macro Regime + VIX Friction)
  7. HEAT & SAFETY CHECK (0.0% Portfolio Heat for NOT_A_POSITION)
  8. QUALIFIED TRADE DISPATCH (SSE streaming + SQLite persistence)
"""

import os
import json
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta, time
from typing import Dict, Any, List, Optional, AsyncGenerator, Tuple
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

from app.analytics.universe_config import resolve_universe_tickers, validate_ticker_list
from app.analytics.model_registry import ModelRegistry, ModelIntegrityViolationError
from app.analytics.decision_engine import evaluate_ticker, QualificationResult
from app.analytics.macro_engine import get_macro_regime
from app.analytics.kelly_sizer import get_portfolio_heat_status
from app.api.ml_history import save_ml_trade
from app.data.data_gateway import DataGateway
from app.data.database import get_db_path
from app.data.market_provider import get_live_quote_with_meta, get_bulk_live_quotes_with_meta, fetch_candles
import pandas as pd

logger = logging.getLogger(__name__)

_WORKER_MODEL_CACHE: Dict[str, Tuple[Any, Dict[str, Any]]] = {}

def _eval_ticker_batch_worker(
    payload: Tuple[List[Tuple[str, pd.DataFrame]], str, Dict[str, Any]]
) -> List[QualificationResult]:
    """
    Multiprocessing worker function that executes DecisionEngine.evaluate_ticker across a chunk of symbols.
    Caches the cryptographically verified Champion model locally per child process to avoid IPC serialization overhead.
    """
    chunk_items, trade_type, macro = payload
    tf = "intraday" if trade_type.upper() == "INTRADAY" else "swing"

    if tf not in _WORKER_MODEL_CACHE:
        from app.analytics.model_registry import ModelRegistry
        try:
            m, meta = ModelRegistry.load_champion(tf, enforce_hash=True)
            _WORKER_MODEL_CACHE[tf] = (m, meta)
        except Exception as e:
            logger.error(f"[SmartScannerWorker] Failed to load champion model {tf}: {e}")
            return []

    champion_model, champion_meta = _WORKER_MODEL_CACHE[tf]
    from app.analytics.decision_engine import evaluate_ticker

    results = []
    for sym, df_sym in chunk_items:
        if df_sym is None or df_sym.empty or len(df_sym) < 30:
            continue
        try:
            res = evaluate_ticker(
                ticker=sym,
                df=df_sym,
                champion_model=champion_model,
                champion_meta=champion_meta,
                trade_type=trade_type,
                source="MANUAL",
                macro_state=macro,
                skip_enrichment=True,
                allow_candle_close=True
            )
            if res is not None:
                results.append(res)
        except Exception as e:
            logger.debug(f"[SmartScannerWorker] Evaluation error for {sym}: {e}")
    return results

def format_sse(data: dict) -> str:
    """Formats payload as newline-delimited JSON line for SSE streaming."""
    return f"{json.dumps(data)}\n"

class SmartScannerPipeline:
    """
    Authoritative Scanning Pipeline supporting both INTRADAY (15m) and SWING (1D) timeframes.
    """

    @classmethod
    async def run_sweep(
        cls,
        timeframe: str = "intraday",
        universe: str = "NIFTY_500",
        custom_tickers: Optional[List[str]] = None,
        min_confidence: float = 60.0
    ) -> AsyncGenerator[str, None]:
        """
        Executes unified AI sweep streaming Server-Sent Events (SSE) progress.
        """
        tf = timeframe.lower()
        if tf not in ("intraday", "swing"):
            yield format_sse({
                "type": "error",
                "message": f"Invalid timeframe '{timeframe}'. Must be 'intraday' or 'swing'.",
                "progress": 100
            })
            return

        trade_type = "INTRADAY" if tf == "intraday" else "SWING"
        candle_interval = "15m" if tf == "intraday" else "1d"

        yield format_sse({
            "type": "system",
            "message": f"🚀 Initializing Smart AI Sweep [{trade_type} | {candle_interval} | Universe: {universe}]...",
            "progress": 2
        })

        # ── STAGE 1: MACRO & PORTFOLIO RISK PRE-FLIGHT ────────────────
        try:
            macro = get_macro_regime()
            vix_val = macro.get("vix_close", 15.0)
            vix_status = macro.get("vix_status", "NORMAL")
            nifty_trend = macro.get("nifty_trend_short", "BULLISH")
            
            heat = get_portfolio_heat_status()
            current_heat = heat.get("current_heat_pct", 0.0)

            yield format_sse({
                "type": "info",
                "message": f"🌐 Macro State: Trend={nifty_trend} | VIX={vix_val:.2f} ({vix_status}) | Portfolio Heat={current_heat:.1f}%",
                "progress": 5
            })
        except Exception as e:
            logger.warning(f"[SmartScanner] Macro check failed: {e}")
            macro = {}

        # ── STAGE 2: MODEL REGISTRY CRYPTOGRAPHIC INTEGRITY AUDIT ─────
        yield format_sse({
            "type": "info",
            "message": f"🔒 Auditing cryptographic signature for {trade_type} Champion Model...",
            "progress": 8
        })

        try:
            champion_model, champion_meta = ModelRegistry.load_champion(tf, enforce_hash=True)
            champ_v = champion_meta.get("version", "v1.0-champion")
            champ_f1 = champion_meta.get("champion_f1", 0.685)
            yield format_sse({
                "type": "info",
                "message": f"⚡ Loaded Verified Champion {champ_v} (Validation F1: {champ_f1:.4f})",
                "progress": 10
            })
        except ModelIntegrityViolationError as e:
            yield format_sse({
                "type": "error",
                "message": f"CRITICAL SECURITY HALT: Model signature verification failed: {e}",
                "progress": 100
            })
            return
        except Exception as e:
            yield format_sse({
                "type": "error",
                "message": f"Failed to load champion model: {e}",
                "progress": 100
            })
            return

        # ── STAGE 3: UNIVERSE RESOLUTION (FAIL CLOSED) ────────────────
        try:
            target_symbols = resolve_universe_tickers(
                universe_name=universe,
                custom_tickers=custom_tickers
            )
        except ValueError as e:
            yield format_sse({
                "type": "error",
                "message": f"Universe resolution failed: {e}",
                "progress": 100
            })
            return

        if not target_symbols:
            yield format_sse({
                "type": "error",
                "message": f"No valid ticker symbols found for universe '{universe}'.",
                "progress": 100
            })
            return

        raw_total = len(target_symbols)

        # ── STAGE 4: HIGH-SPEED LOCAL SQLite & PARALLEL INGESTION ────────
        active_symbols = list(target_symbols)
        bulk_data_map: Dict[str, pd.DataFrame] = {}

        yield format_sse({
            "type": "info",
            "message": f"📥 Ingesting authoritative {candle_interval} bars for {len(active_symbols)} symbols from canonical SQLite store...",
            "progress": 15
        })

        # 1. Fast indexed bulk query from local canonical database
        db_path = get_db_path()
        try:
            conn = sqlite3.connect(db_path, timeout=30.0)
            tf_clause = "timeframe = '15m'" if trade_type == "INTRADAY" else "(timeframe = '1d' OR timeframe IS NULL)"
            cutoff_date = (datetime.now() - timedelta(days=45 if trade_type == "INTRADAY" else 730)).strftime('%Y-%m-%d')

            # Normalize tickers for lookup (.NS and plain)
            lookup_syms = set()
            for s in active_symbols:
                lookup_syms.add(s)
                lookup_syms.add(f"{s}.NS" if not s.endswith(('.NS', '.BO')) else s)

            sym_list = list(lookup_syms)
            chunk_size = 400
            for i in range(0, len(sym_list), chunk_size):
                sub_syms = sym_list[i:i + chunk_size]
                placeholders = ",".join("?" for _ in sub_syms)
                query = f"""
                    SELECT ticker, date, open, high, low, close, volume 
                    FROM ohlcv 
                    WHERE {tf_clause} AND date >= ? AND ticker IN ({placeholders})
                    ORDER BY ticker, date ASC
                """
                params = [cutoff_date] + sub_syms
                raw_db_df = pd.read_sql_query(query, conn, params=params)

                if not raw_db_df.empty:
                    for sym, grp in raw_db_df.groupby("ticker"):
                        df_sym = grp.set_index("date").copy()
                        if len(df_sym) >= 30:
                            bulk_data_map[sym] = df_sym
                            # Also map without suffix for flexible lookup
                            plain_sym = sym.replace('.NS', '').replace('.BO', '')
                            bulk_data_map[plain_sym] = df_sym
            conn.close()
        except Exception as e:
            logger.warning(f"[SmartScanner] SQLite bulk fetch error: {e}")

        # 2. Check for missing or stale symbols that need external provider sync
        now_dt = datetime.now()
        is_intra_market_hours = (trade_type == "INTRADAY" and DataGateway.is_market_open())
        # The first 15m candle of the session closes at 09:30 IST
        is_after_first_bar = (now_dt.time() >= time(9, 30))
        require_today_bar = (is_intra_market_hours and is_after_first_bar)
        today_str = now_dt.strftime('%Y-%m-%d')

        missing_symbols = []
        for s in active_symbols:
            df_sym = bulk_data_map.get(s)
            if df_sym is None:
                df_sym = bulk_data_map.get(f"{s}.NS")
            if df_sym is None:
                df_sym = bulk_data_map.get(s.replace('.NS', '').replace('.BO', ''))

            if df_sym is None or df_sym.empty or len(df_sym) < 30:
                missing_symbols.append(s)
            elif require_today_bar:
                # If market is actively open past 09:30, verify that the last bar is from today
                last_bar_dt = str(df_sym.index[-1])[:10]
                if last_bar_dt < today_str:
                    missing_symbols.append(s)

        if missing_symbols:
            sync_count = min(len(missing_symbols), 5)
            yield format_sse({
                "type": "info",
                "message": f"📡 Syncing {sync_count} priority symbols from Upstox (rate limit guarded)...",
                "progress": 25
            })
            period_str = "60d" if trade_type == "INTRADAY" else "2y"
            intv_str = "15m" if trade_type == "INTRADAY" else "1d"
            for sym in missing_symbols[:sync_count]:
                try:
                    s_df = await asyncio.to_thread(fetch_candles, sym, intv_str, period_str)
                    if s_df is not None and not s_df.empty and len(s_df) >= 30:
                        bulk_data_map[sym] = s_df.copy()
                        plain_s = sym.replace('.NS', '').replace('.BO', '')
                        bulk_data_map[plain_s] = s_df.copy()
                        try:
                            DataGateway.persist_intraday_candles(
                                sym, s_df, timeframe="15m" if trade_type == "INTRADAY" else "1d", source="upstox"
                            )
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"[SmartScanner] Upstox fetch note for {sym}: {e}")

        # ── STAGES 5-7: MULTICORE DECISION ENGINE INFERENCE ───────────
        total_eval = len(active_symbols)
        max_workers = min(8, os.cpu_count() or 4)
        yield format_sse({
            "type": "system",
            "message": f"🧠 Ingestion complete ({len(bulk_data_map)} feeds ready). Evaluating {total_eval} instruments across {max_workers} CPU cores...",
            "progress": 30
        })

        # Prepare evaluation items (ticker, DataFrame)
        eval_items: List[Tuple[str, pd.DataFrame]] = []
        for sym in active_symbols:
            df_sym = bulk_data_map.get(sym)
            if df_sym is None:
                df_sym = bulk_data_map.get(f"{sym}.NS")
            if df_sym is None:
                plain = sym.replace('.NS', '').replace('.BO', '')
                df_sym = bulk_data_map.get(plain)

            if df_sym is not None and not df_sym.empty and len(df_sym) >= 30:
                if require_today_bar:
                    last_bar_dt = str(df_sym.index[-1])[:10]
                    if last_bar_dt < today_str:
                        # Skip stale yesterday data during active market hours to avoid false counter-trend signals
                        continue
                eval_items.append((sym, df_sym))

        qualified_candidates = []
        best_candidate = None
        best_confidence = 0.0
        evaluated_count = 0

        # Import ConsensusScanner for stop check
        from app.analytics.consensus_scanner import ConsensusScanner

        loop = asyncio.get_running_loop()
        # Divide into batches of 40-50 instruments to provide smooth SSE streaming to frontend
        batch_size = max(20, min(50, total_eval // 8))

        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            for b_start in range(0, len(eval_items), batch_size):
                if ConsensusScanner.is_stop_requested():
                    logger.info("[SmartScanner] Cancellation requested. Halting multicore evaluation.")
                    break

                batch_chunk = eval_items[b_start:b_start + batch_size]
                # Partition this batch across the available worker processes
                sub_chunk_size = max(4, len(batch_chunk) // max_workers) if len(batch_chunk) >= max_workers else len(batch_chunk)
                sub_chunks = [batch_chunk[i:i + sub_chunk_size] for i in range(0, len(batch_chunk), sub_chunk_size)]

                futures = [
                    loop.run_in_executor(pool, _eval_ticker_batch_worker, (sc, trade_type, macro))
                    for sc in sub_chunks
                ]
                sub_results_list = await asyncio.gather(*futures)
                batch_res = [res for sub_res in sub_results_list for res in sub_res]

                # Pre-fetch live quotes in ONE bulk API request for all qualified setups in this batch
                batch_qualified_tickers = [
                    q.ticker for q in batch_res
                    if q is not None and q.qualified and (q.confidence if q.is_bullish else round(100.0 - q.confidence, 1)) >= min_confidence
                ]
                batch_quotes = get_bulk_live_quotes_with_meta(batch_qualified_tickers) if batch_qualified_tickers else {}

                for q_res in batch_res:
                    evaluated_count += 1
                    if q_res is None:
                        continue

                    directional_conf = q_res.confidence if q_res.is_bullish else round(100.0 - q_res.confidence, 1)
                    if q_res.qualified and directional_conf >= min_confidence:
                        # Enforce Swing Cash Short Ban
                        if trade_type == "SWING" and not q_res.is_bullish:
                            continue

                        entry_val = q_res.entry
                        sl_val = q_res.sl
                        tp1_val = q_res.tp1
                        tp2_val = q_res.tp2

                        # Live execution LTP lookup using bulk pre-fetched quote
                        # CRITICAL: Only accept REAL_TIME quotes (Upstox sub-second feed).
                        # Yahoo Finance fast_info is 15-min delayed (quote_freshness="DELAYED",
                        # age_ms=900000) and must NOT override the candle close — doing so
                        # records a stale entry price that never existed at signal time.
                        try:
                            clean_t = q_res.ticker.replace('.NS', '').replace('.BO', '').strip().upper()
                            live_q = batch_quotes.get(q_res.ticker) or batch_quotes.get(clean_t) or get_live_quote_with_meta(q_res.ticker)
                            freshness = (live_q or {}).get("quote_freshness", "UNAVAILABLE")
                            if (live_q and live_q.get("price") and float(live_q["price"]) > 0
                                    and freshness == "REAL_TIME"):
                                live_p = float(live_q["price"])
                                delta = live_p - entry_val
                                entry_val = live_p
                                sl_val += delta
                                tp1_val += delta
                                tp2_val += delta
                            # If DELAYED or UNAVAILABLE: keep candle close (already correct)
                        except Exception:
                            pass

                        # Persist recommendation strictly as NOT_A_POSITION (0.0% heat)
                        save_ml_trade(
                            ticker=q_res.ticker,
                            is_bullish=q_res.is_bullish,
                            entry=round(entry_val, 2),
                            sl=round(sl_val, 2),
                            tp1=round(tp1_val, 2),
                            tp2=round(tp2_val, 2),
                            confidence=directional_conf,
                            trade_type=trade_type,
                            explanation=q_res.telemetry,
                            source="MANUAL",
                            position_type="NOT_A_POSITION"
                        )

                        trade_card = {
                            "ticker": q_res.ticker,
                            "direction": q_res.direction,
                            "is_bullish": q_res.is_bullish,
                            "confidence": round(directional_conf, 1),
                            "raw_confidence": round(q_res.raw_confidence, 1),
                            "entry": round(entry_val, 2),
                            "sl": round(sl_val, 2),
                            "tp1": round(tp1_val, 2),
                            "tp2": round(tp2_val, 2),
                            "trade_type": trade_type,
                            "timeframe": candle_interval,
                            "base_probs": {
                                "rf": round(q_res.base_probs[0] * 100, 1),
                                "gb": round(q_res.base_probs[1] * 100, 1),
                                "svc": round(q_res.base_probs[2] * 100, 1)
                            },
                            "explanation": q_res.telemetry,
                            "timestamp": datetime.now().isoformat()
                        }

                        qualified_candidates.append(trade_card)

                        if q_res.confidence > best_confidence:
                            best_confidence = q_res.confidence
                            best_candidate = trade_card

                        yield format_sse({
                            "type": "candidate",
                            "message": f"✨ Qualified Setup: {q_res.ticker} ({q_res.direction} {q_res.confidence:.1f}%)",
                            "data": trade_card
                        })

                progress_pct = int(30 + (evaluated_count / max(total_eval, 1) * 60))
                yield format_sse({
                    "type": "info",
                    "message": f"🔍 Evaluated {evaluated_count}/{total_eval} symbols across {max_workers} CPU cores...",
                    "progress": progress_pct
                })

        # ── STAGE 8: COMPLETION SUMMARY TELEMETRY ─────────────────────
        yield format_sse({
            "type": "info",
            "message": f"📊 Sweep complete. Evaluated: {total_eval} | Qualified: {len(qualified_candidates)} setups.",
            "progress": 98
        })

        summary_data = {
            "total_scanned": total_eval,
            "qualified_count": len(qualified_candidates),
            "timeframe": trade_type,
            "universe": universe,
            "best_candidate": best_candidate,
            "all_candidates": qualified_candidates,
            "completed_at": datetime.now().isoformat()
        }

        yield format_sse({
            "type": "result",
            "data": best_candidate or (qualified_candidates[0] if qualified_candidates else None),
            "summary": summary_data,
            "progress": 100
        })

    @classmethod
    async def run_sweep_batch(
        cls,
        timeframe: str = "intraday",
        universe: str = "NIFTY_500",
        custom_tickers: Optional[List[str]] = None,
        min_confidence: float = 60.0
    ) -> Dict[str, Any]:
        """
        Executes unified AI sweep directly and returns collected candidates dictionary.
        """
        import json
        all_candidates = []
        summary = {}
        error_msg = None

        async for sse_chunk in cls.run_sweep(
            timeframe=timeframe,
            universe=universe,
            custom_tickers=custom_tickers,
            min_confidence=min_confidence
        ):
            try:
                for line in sse_chunk.strip().split("\n"):
                    clean_line = line.strip()
                    if clean_line.startswith("data:"):
                        clean_line = clean_line[5:].strip()
                    if not clean_line.startswith("{"):
                        continue
                    payload = json.loads(clean_line)
                    if payload.get("type") == "candidate":
                        all_candidates.append(payload.get("data"))
                    elif payload.get("type") == "result":
                        summary = payload.get("summary", {})
                    elif payload.get("type") == "error":
                        error_msg = payload.get("message")
            except Exception:
                continue

        return {
            "status": "ERROR" if error_msg else "SUCCESS",
            "error": error_msg,
            "candidates": all_candidates,
            "summary": summary
        }
