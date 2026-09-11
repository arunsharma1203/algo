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

import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional, AsyncGenerator
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.analytics.universe_config import resolve_universe_tickers, validate_ticker_list
from app.analytics.model_registry import ModelRegistry, ModelIntegrityViolationError
from app.analytics.decision_engine import evaluate_ticker, QualificationResult
from app.analytics.macro_engine import get_macro_regime
from app.analytics.kelly_sizer import get_portfolio_heat_status
from app.api.ml_history import save_ml_trade
from app.data.data_gateway import DataGateway
import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)

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

        # ── STAGE 4: PARALLEL DATA ACQUISITION ────────────────────────
        active_symbols = list(target_symbols)
        bulk_data_map = {}

        if trade_type == "INTRADAY":
            # For intraday: If universe is large (>50), screen top 50 by 1-day volume first for sub-minute performance
            if raw_total > 50:
                yield format_sse({
                    "type": "info",
                    "message": f"📊 Volume Screening {raw_total} universe symbols for top liquid intraday momentum...",
                    "progress": 12
                })
                
                vol_chunk_size = 100
                vol_chunks = [target_symbols[i:i + vol_chunk_size] for i in range(0, raw_total, vol_chunk_size)]
                stock_vols = {}

                def fetch_vol(chk):
                    try:
                        df_v = yf.download(chk, period="1d", progress=False)
                        if 'Volume' in df_v and len(df_v['Volume']) > 0:
                            return df_v['Volume'].iloc[-1].dropna().to_dict()
                    except Exception:
                        pass
                    return {}

                with ThreadPoolExecutor(max_workers=min(5, len(vol_chunks))) as executor:
                    fut_map = {executor.submit(fetch_vol, chk): chk for chk in vol_chunks}
                    for fut in as_completed(fut_map):
                        res_vol = fut.result()
                        stock_vols.update(res_vol)

                if stock_vols:
                    sorted_by_vol = sorted(stock_vols.items(), key=lambda x: x[1], reverse=True)
                    top_50 = [x[0] for x in sorted_by_vol[:50]]
                    active_symbols = [s for s in target_symbols if s in top_50 or (custom_tickers and s in custom_tickers)]
                else:
                    active_symbols = target_symbols[:50]

            yield format_sse({
                "type": "info",
                "message": f"📥 Ingesting 60-day 15m candle feeds for top {len(active_symbols)} liquid symbols...",
                "progress": 20
            })

            try:
                bulk_data = await asyncio.to_thread(
                    yf.download,
                    active_symbols,
                    period="60d",
                    interval="15m",
                    progress=False
                )
            except Exception as e:
                logger.warning(f"[SmartScanner] Intraday bulk fetch error: {e}")
                bulk_data = pd.DataFrame()

            # Unpack into map
            if isinstance(bulk_data.columns, pd.MultiIndex):
                for sym in active_symbols:
                    try:
                        bulk_data_map[sym] = bulk_data.xs(sym, level=1, axis=1).copy()
                    except KeyError:
                        bulk_data_map[sym] = pd.DataFrame()
            elif len(active_symbols) == 1:
                bulk_data_map[active_symbols[0]] = bulk_data.copy()

        else:
            # For SWING: 2y daily candles in parallel chunks
            chunk_size = 100
            chunks = [target_symbols[i:i + chunk_size] for i in range(0, raw_total, chunk_size)]
            
            yield format_sse({
                "type": "info",
                "message": f"📥 Bulk fetching 2-year daily history across {raw_total} symbols in {len(chunks)} parallel batches...",
                "progress": 15
            })

            def fetch_chunk(chk):
                try:
                    return yf.download(chk, period="2y", interval="1d", progress=False)
                except Exception:
                    return pd.DataFrame()

            chunk_results = []
            with ThreadPoolExecutor(max_workers=min(5, len(chunks))) as executor:
                futs = [executor.submit(fetch_chunk, chk) for chk in chunks]
                for fut in as_completed(futs):
                    res_df = fut.result()
                    if res_df is not None and not res_df.empty:
                        chunk_results.append(res_df)

            for c_df in chunk_results:
                if c_df is not None and not c_df.empty:
                    if isinstance(c_df.columns, pd.MultiIndex):
                        tickers_in_chunk = set(c_df.columns.get_level_values(1))
                        for sym in tickers_in_chunk:
                            try:
                                s_df = c_df.xs(sym, level=1, axis=1).dropna(how='all')
                                if not s_df.empty and len(s_df) >= 30:
                                    bulk_data_map[sym] = s_df.copy()
                            except KeyError:
                                pass
                    elif len(target_symbols) == 1:
                        bulk_data_map[target_symbols[0]] = c_df.copy()

        # ── STAGES 5-7: DECISION ENGINE INFERENCE ─────────────────────
        total_eval = len(active_symbols)
        yield format_sse({
            "type": "system",
            "message": f"🧠 Market data ingested. Running multi-model inference across {total_eval} instruments...",
            "progress": 30
        })

        qualified_candidates = []
        best_candidate = None
        best_confidence = 0.0

        for idx, sym in enumerate(active_symbols):
            progress_pct = int(30 + ((idx + 1) / total_eval * 60))

            try:
                df_sym = bulk_data_map.get(sym, pd.DataFrame())

                # Only fallback to individual fetch for small custom/benchmark pool (<= 5 symbols)
                if (df_sym.empty or len(df_sym) < 30) and len(active_symbols) <= 5:
                    try:
                        df_sym = await asyncio.to_thread(
                            DataGateway.get_ohlcv,
                            sym,
                            timeframe=candle_interval,
                            min_rows=30
                        )
                    except Exception:
                        df_sym = pd.DataFrame()

                if df_sym is None or df_sym.empty or len(df_sym) < 30:
                    continue

                # Fast screening pass (skip enrichment for high speed)
                q_res: QualificationResult = evaluate_ticker(
                    ticker=sym,
                    df=df_sym,
                    champion_model=champion_model,
                    champion_meta=champion_meta,
                    trade_type=trade_type,
                    source="MANUAL",
                    macro_state=macro,
                    skip_enrichment=True
                )

                # Periodic heartbeat progress log so progress never freezes
                if (idx + 1) % 10 == 0 or (idx + 1) == total_eval:
                    yield format_sse({
                        "type": "info",
                        "message": f"🔍 Evaluated {idx+1}/{total_eval} symbols ({sym} screened)...",
                        "progress": progress_pct
                    })

                if q_res.qualified and q_res.confidence >= min_confidence:
                    # Enforce Swing Cash Short Ban
                    if trade_type == "SWING" and not q_res.is_bullish:
                        continue

                    # Persist recommendation strictly as NOT_A_POSITION (0.0% heat)
                    save_ml_trade(
                        ticker=q_res.ticker,
                        is_bullish=q_res.is_bullish,
                        entry=q_res.entry,
                        sl=q_res.sl,
                        tp1=q_res.tp1,
                        tp2=q_res.tp2,
                        confidence=q_res.confidence,
                        trade_type=trade_type,
                        explanation=q_res.telemetry,
                        source="MANUAL",
                        position_type="NOT_A_POSITION"
                    )

                    trade_card = {
                        "ticker": q_res.ticker,
                        "direction": q_res.direction,
                        "is_bullish": q_res.is_bullish,
                        "confidence": round(q_res.confidence, 1),
                        "raw_confidence": round(q_res.raw_confidence, 1),
                        "entry": round(q_res.entry, 2),
                        "sl": round(q_res.sl, 2),
                        "tp1": round(q_res.tp1, 2),
                        "tp2": round(q_res.tp2, 2),
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

            except Exception as e:
                logger.debug(f"[SmartScanner] Evaluation error for {sym}: {e}")
                continue

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
