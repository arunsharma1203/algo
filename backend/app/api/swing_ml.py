import json
import time
from datetime import datetime
import pandas as pd
import yfinance as yf
import ta
import numpy as np
import logging
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.intraday_ml import INDIAN_STOCK_UNIVERSE
from app.analytics.model_manager import ModelManager
from app.data.validator import MarketDataValidator
from app.analytics.macro_engine import get_macro_regime
from app.analytics.nlp_engine import nlp_engine
from app.analytics.meta_learner import meta_learner
from app.analytics.calibration import calibrator
from app.analytics.foundation_models.manager import foundation_model_manager
from app.analytics.telegram_notifier import send_telegram_message

logger = logging.getLogger(__name__)
router = APIRouter()

def format_sse(data: dict) -> str:
    return f"{json.dumps(data, default=str)}\n"

def get_or_init_swing_champion():
    """Loads the persisted Swing Champion model artifact."""
    model, meta = ModelManager.load_champion("swing")
    return model, meta

def run_swing_scan(custom_tickers: list = None, universe_preset: str = "NIFTY_500"):
    try:
        start_time = datetime.now()
        clean_custom = [t.strip().upper() for t in (custom_tickers or []) if t and t.strip()]
        if universe_preset and universe_preset.upper() == "ALL_COLLECTED":
            from app.analytics.universe_config import get_universe
            u_info = get_universe("ALL_COLLECTED", custom_tickers=clean_custom)
            universe = list(u_info.get("tickers", []))
        elif clean_custom and (not universe_preset or universe_preset.upper() in ("CUSTOM", "WATCHLIST")):
            universe = clean_custom
        else:
            from app.analytics.universe_config import get_universe
            u_info = get_universe(universe_preset or "NIFTY_500")
            universe = list(u_info.get("tickers", []))
            if not universe:
                universe = INDIAN_STOCK_UNIVERSE[:50].copy()
        
        yield format_sse({"type": "system", "message": f"[{start_time.strftime('%H:%M:%S')}] Initializing Swing Trade ML Pipeline for {len(universe)} symbols ({universe_preset or 'CUSTOM'})..."})
        yield format_sse({"type": "info", "message": "Analyzing Macro Market Regime (NIFTY 50 & INDIA VIX)...", "progress": 3})
        
        macro = get_macro_regime()
        regime = macro['nifty_trend_long']
        vix_status = macro['vix_status']
        vix_val = macro['vix_close']
        
        if macro['nifty_close'] > 0:
            if regime == "BULLISH":
                yield format_sse({"type": "info", "message": f"📈 Macro Regime is BULLISH (NIFTY {macro['nifty_close']:.2f} > 200 SMA {macro['sma_200']:.2f})."})
            else:
                yield format_sse({"type": "info", "message": f"📉 Macro Regime is BEARISH (NIFTY {macro['nifty_close']:.2f} < 200 SMA {macro['sma_200']:.2f}). -20 Penalty to Longs."})
                
            if vix_status == "HIGH":
                yield format_sse({"type": "info", "message": f"⚠️ VIX Spike Detected! (INDIA VIX = {vix_val:.2f}). High volatility penalty applied."})
            else:
                yield format_sse({"type": "info", "message": f"📊 VIX is NORMAL ({vix_val:.2f}). Safe environment for Swing Trades."})

        # Load Persisted Swing Champion Model Artifact
        yield format_sse({"type": "info", "message": "Loading verified Production Swing Champion Model...", "progress": 5})
        try:
            champion_model, champion_meta = get_or_init_swing_champion()
            champ_v = champion_meta.get("version", "v1.0-champion")
            f1_val = champion_meta.get('champion_f1', 0.695)
            yield format_sse({"type": "info", "message": f"⚡ Loaded Swing Champion {champ_v} (Validation F1: {f1_val:.4f})", "progress": 7})
        except Exception as e:
            yield format_sse({"type": "error", "message": f"Failed to load Swing Champion model: {e}"})
            return

        chunk_size = 100
        chunks = [universe[i:i + chunk_size] for i in range(0, len(universe), chunk_size)]
        yield format_sse({"type": "system", "message": f"Bulk fetching 2 years of daily data for {len(universe)} symbols in {len(chunks)} parallel batches...", "progress": 10})

        from concurrent.futures import ThreadPoolExecutor
        chunk_dfs = {}
        try:
            with ThreadPoolExecutor(max_workers=min(5, len(chunks))) as executor:
                future_to_idx = {executor.submit(yf.download, chk, period="2y", interval="1d", progress=False): idx for idx, chk in enumerate(chunks)}
                for fut in future_to_idx:
                    c_idx = future_to_idx[fut]
                    chunk_dfs[c_idx] = fut.result()
        except Exception as e:
            yield format_sse({"type": "error", "message": f"Bulk fetch failed: {str(e)}"})
            return
            
        yield format_sse({"type": "system", "message": "Market data fetched. Commencing deep out-of-sample inference...", "progress": 15})
        
        best_conviction = None
        best_score = 0
        scan_history = []
        total = len(universe)
        
        for idx, ticker in enumerate(universe):
            try:
                c_idx = idx // chunk_size
                bulk_data = chunk_dfs.get(c_idx)
                if bulk_data is None:
                    continue

                if isinstance(bulk_data.columns, pd.MultiIndex):
                    try:
                        df = bulk_data.xs(ticker, level=1, axis=1).copy()
                    except KeyError:
                        continue
                else:
                    if len(chunks[c_idx]) == 1:
                        df = bulk_data.copy()
                    else:
                        continue
                
                # Use SHARED DECISION ENGINE for screening (skip enrichment for speed)
                from app.analytics.decision_engine import evaluate_ticker
                screen_result = evaluate_ticker(
                    ticker=ticker,
                    df=df,
                    champion_model=champion_model,
                    champion_meta=champion_meta,
                    trade_type="SWING",
                    source="MANUAL",
                    macro_state=macro,
                    skip_enrichment=True,
                )

                if not screen_result.qualified:
                    continue

                progress_pct = int(15 + ((idx + 1) / total * 75))
                yield format_sse({
                    "type": "info",
                    "message": f"[{idx+1}/{total}] {ticker} (₹{screen_result.entry:.2f}) -> RF:{screen_result.base_probs[0]:.1f}% GB:{screen_result.base_probs[1]:.1f}% SVM:{screen_result.base_probs[2]:.1f}% | Ensemble: {screen_result.raw_confidence:.1f}%",
                    "progress": progress_pct
                })
                    
                scan_history.append({
                    "ticker": ticker,
                    "score": round(screen_result.score, 1),
                    "action": "BUY" if screen_result.score > 70 else "HOLD",
                    "prob": round(screen_result.raw_confidence, 1),
                    "price": round(screen_result.entry, 2)
                })
                
                # Apply macro penalty for ranking
                effective_score = screen_result.score
                if regime == "BEARISH":
                    effective_score -= 15
                    
                if effective_score > best_score:
                    best_score = effective_score
                    best_conviction = {
                        "ticker": ticker,
                        "action": "BUY",
                        "is_bullish": screen_result.is_bullish,
                        "score": screen_result.score,
                        "prob": screen_result.raw_confidence,
                        "base_probs": screen_result.base_probs,
                        "entry": screen_result.entry,
                        "sl": screen_result.sl,
                        "tp1": screen_result.tp1,
                        "tp2": screen_result.tp2,
                        "atr_pct": screen_result.atr_pct,
                        "volume_ratio": screen_result.volume_ratio,
                        "rsi": screen_result.rsi,
                        "macd_diff": screen_result.macd_diff,
                        "adx": screen_result.adx,
                        "raw_df": df,
                        "timestamp": datetime.now().isoformat()
                    }
                    yield format_sse({"type": "info", "message": f"🔥 New high conviction swing setup found: {ticker} (Score: {screen_result.score:.1f})"})
                
            except Exception as e:
                logger.warning(f"Error screening swing candidate {ticker}: {e}")
                continue
        
        yield format_sse({"type": "system", "message": "Swing Market Sweep Complete. Finalizing arbitration...", "progress": 90})
        
        if best_conviction:
            # ── Full enrichment on BEST TRADE using SHARED DECISION ENGINE ──
            yield format_sse({"type": "info", "message": f"Running full AI pipeline on best candidate {best_conviction['ticker']}...", "progress": 92})
            
            from app.analytics.decision_engine import evaluate_ticker
            final_result = evaluate_ticker(
                ticker=best_conviction['ticker'],
                df=best_conviction['raw_df'],
                champion_model=champion_model,
                champion_meta=champion_meta,
                trade_type="SWING",
                source="MANUAL",
                macro_state=macro,
                skip_enrichment=False,  # Full pipeline
            )

            # Report enrichment results
            pc = final_result.pipeline_components
            if pc.get('nlp_vader') is True:
                yield format_sse({"type": "info", "message": f"📰 VADER Sentiment: {final_result.nlp_sentiment:+}", "progress": 93})
            if pc.get('timesfm') is True or pc.get('chronos') is True:
                yield format_sse({"type": "info", "message": f"🔮 Foundation Models (TimesFM: {pc.get('timesfm')}, Chronos: {pc.get('chronos')})", "progress": 95})
            if pc.get('meta_learner') is True:
                yield format_sse({"type": "info", "message": f"🤖 {final_result.meta_learner_msg}", "progress": 97})
            if pc.get('calibration') is True:
                yield format_sse({"type": "info", "message": f"📊 Final Calibrated Win Rate: {final_result.confidence:.1f}%", "progress": 99})

            # Merge into best_conviction for frontend compatibility
            best_conviction['score'] = final_result.confidence
            best_conviction['is_bullish'] = final_result.is_bullish
            best_conviction['direction'] = "BULLISH"
            best_conviction['meta_learner_msg'] = final_result.meta_learner_msg
            best_conviction['telemetry'] = final_result.telemetry
            best_conviction['calibration'] = final_result.calibration
            best_conviction['nlp_sentiment'] = final_result.nlp_sentiment
            best_conviction['nlp_headline'] = final_result.nlp_headline
            best_conviction['entry'] = final_result.entry
            best_conviction['reference_price'] = final_result.reference_price
            best_conviction['model_candle_close'] = final_result.model_candle_close
            best_conviction['price_source'] = final_result.price_source
            best_conviction['price_timestamp'] = final_result.price_timestamp
            best_conviction['price_is_fresh'] = final_result.price_is_fresh
            best_conviction['sl'] = final_result.sl
            best_conviction['tp1'] = final_result.tp1
            best_conviction['tp2'] = final_result.tp2
            best_conviction['pipeline_components'] = final_result.pipeline_components
            if final_result.timesfm_forecast:
                best_conviction['timesfm_forecast'] = final_result.timesfm_forecast
            if final_result.chronos_forecast:
                best_conviction['chronos_forecast'] = final_result.chronos_forecast

            best_conviction.pop('raw_df', None)

            # Save as NOT_A_POSITION recommendation ONLY if genuinely qualified and bullish
            from app.api.ml_history import save_ml_trade

            was_saved = False
            if final_result.qualified and final_result.is_bullish:
                was_saved = save_ml_trade(
                    ticker=best_conviction['ticker'],
                    is_bullish=True,
                    entry=best_conviction['entry'],
                    sl=best_conviction['sl'],
                    tp1=best_conviction['tp1'],
                    tp2=best_conviction['tp2'],
                    confidence=best_conviction['score'],
                    trade_type="SWING",
                    explanation=final_result.explanation,
                    source='MANUAL',
                    position_type='NOT_A_POSITION'
                )
            if was_saved:
                yield format_sse({"type": "info", "message": f"💾 Saved new Swing recommendation for {best_conviction['ticker']} to Trade History"})
                try:
                    from app.analytics.master_logger import MasterLogger
                    MasterLogger.log_event("SCAN_MANUAL", "PERSISTED", f"Saved Swing recommendation for {best_conviction['ticker']}", ticker=best_conviction['ticker'], details=best_conviction)
                except Exception:
                    pass
            else:
                yield format_sse({"type": "info", "message": f"ℹ️ Identical recommendation for {best_conviction['ticker']} already logged today (deduplicated - record preserved)"})
                try:
                    from app.analytics.master_logger import MasterLogger
                    MasterLogger.log_event("SCAN_MANUAL", "DEDUPLICATED", f"Swing recommendation for {best_conviction['ticker']} deduplicated", ticker=best_conviction['ticker'])
                except Exception:
                    pass

            # Telegram — only if saved (not deduped) and above threshold
            if was_saved and best_conviction['score'] >= 60.0:
                dir_text = "BULLISH BUY" if best_conviction.get('is_bullish', True) else "BEARISH SHORT"
                tg_msg = (
                    f"🔥 <b>SWING TRADE SETUP DISCOVERED: {best_conviction['ticker']}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📈 <b>Action:</b> BUY (Swing 5-Day Horizon)\n"
                    f"🧠 <b>Calibrated Win Rate:</b> {best_conviction['score']:.1f}%\n"
                    f"💵 <b>Entry:</b> ₹{best_conviction['entry']:.2f}\n"
                    f"🛑 <b>Stop Loss:</b> ₹{best_conviction['sl']:.2f}\n"
                    f"🎯 <b>Target 1:</b> ₹{best_conviction['tp1']:.2f}\n"
                    f"🎯 <b>Target 2:</b> ₹{best_conviction['tp2']:.2f}\n"
                    f"🌐 <b>Macro Bias:</b> {regime}\n"
                    f"📡 <b>Source:</b> MANUAL SCAN\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<i>{best_conviction.get('meta_learner_msg', 'AI Multi-Model Consensus')}</i>"
                )
                try:
                    tg_sent = send_telegram_message(tg_msg)
                    if tg_sent:
                        yield format_sse({"type": "info", "message": f"📱 Telegram Push Alert Dispatched for {best_conviction['ticker']}"})
                        try:
                            from app.analytics.master_logger import MasterLogger
                            MasterLogger.log_event("TELEGRAM", "DISPATCHED", f"Telegram alert sent for Swing {best_conviction['ticker']}", ticker=best_conviction['ticker'])
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(f"Telegram alert warning: {e}")
            else:
                if not was_saved:
                    reason = "Trade call already broadcasted today (duplicate suppressed)"
                else:
                    reason = f"Calibrated score ({best_conviction['score']:.1f}%) below 60.0% alert gate"
                yield format_sse({"type": "info", "message": f"ℹ️ Telegram alert suppressed: {reason}"})
                try:
                    from app.analytics.master_logger import MasterLogger
                    MasterLogger.log_event("TELEGRAM", "SUPPRESSED", f"Telegram alert suppressed for Swing {best_conviction['ticker']}: {reason}", ticker=best_conviction['ticker'])
                except Exception:
                    pass

            best_conviction['telegram_sent'] = bool(was_saved and best_conviction['score'] >= 60.0)
            best_conviction['was_saved'] = was_saved
            scan_history = sorted(scan_history, key=lambda x: x['score'], reverse=True)
            best_conviction['history'] = scan_history
            
            elapsed = (datetime.now() - start_time).total_seconds()
            try:
                from app.analytics.master_logger import MasterLogger
                MasterLogger.log_event("SCAN_MANUAL", "COMPLETED", f"Swing scan completed in {elapsed:.2f}s", ticker=best_conviction.get('ticker'), details={"elapsed": elapsed, "top_ticker": best_conviction.get('ticker')})
            except Exception:
                pass

            yield format_sse({"type": "system", "message": f"Scan Complete in {elapsed:.2f}s! Trade parameters loaded into execution module.", "progress": 100})
            yield format_sse({"type": "result", "data": best_conviction})
        else:
            try:
                from app.analytics.master_logger import MasterLogger
                MasterLogger.log_event("SCAN_MANUAL", "NO_CANDIDATES", f"Swing scan finished: No bullish candidates passed the cash-equity swing safety gate across {len(universe)} stocks", severity="WARNING")
            except Exception:
                pass
            yield format_sse({"type": "error", "message": f"No high probability swing trade setups found meeting safety gates (All {len(universe)} symbols evaluated; candidates with bearish outlook disqualified by cash-equity LONG-only gate).", "progress": 100})
            
    except Exception as e:
        logger.error(f"Swing Scanner Error: {e}")
        yield format_sse({"type": "error", "message": f"Fatal Swing Scanner Error: {str(e)}", "progress": 100})

@router.get("/swing-scan")
def trigger_swing_scan(custom_tickers: str = None, universe: str = "NIFTY_500"):
    tickers = custom_tickers.split(',') if custom_tickers else None
    return StreamingResponse(run_swing_scan(tickers, universe_preset=universe), media_type="text/event-stream")
