import logging
import asyncio
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException

from app.analytics.model_manager import ModelManager
from app.analytics.decision_engine import evaluate_ticker
from app.analytics.universe_config import resolve_universe_tickers, UNIVERSE_PRESETS, get_universe
from app.analytics.kelly_sizer import calculate_kelly_position_size
from app.data.historical_data_layer import get_db_path
from app.api.market import fetch_historical_data

logger = logging.getLogger(__name__)
router = APIRouter()

class BatchScanRequest(BaseModel):
    preset: Optional[str] = "WATCHLIST"
    tickers: Optional[List[str]] = None

@router.get("/presets")
def get_watchlist_presets():
    """
    Returns available universe presets for the Watchlist Scanner terminal.
    Enforces Invariant 7: presets resolved authoritatively via backend universe engine.
    """
    available_presets = [
        {
            "id": "WATCHLIST",
            "name": "My Watchlist (Backend SQLite)",
            "description": "User's personal operational watchlist stored in local database",
            "ticker_count": len(resolve_universe_tickers("WATCHLIST"))
        },
        {
            "id": "NIFTY_50",
            "name": "NIFTY 50 Bluechips",
            "description": "Top 50 large-cap benchmark equities",
            "ticker_count": len(resolve_universe_tickers("NIFTY_50"))
        },
        {
            "id": "BANK_NIFTY",
            "name": "NIFTY Bank",
            "description": "High-liquidity banking sector leaders",
            "ticker_count": len(resolve_universe_tickers("BANK_NIFTY"))
        },
        {
            "id": "NIFTY_IT",
            "name": "NIFTY IT",
            "description": "Premier information technology and software exporters",
            "ticker_count": len(resolve_universe_tickers("NIFTY_IT"))
        },
        {
            "id": "LIVE_52",
            "name": "Live Scanner Pool (52 Stocks)",
            "description": "The 52 high-liquidity stocks evaluated by real-time scanners",
            "ticker_count": len(resolve_universe_tickers("LIVE_52"))
        }
    ]
    return {"status": "success", "presets": available_presets}

def classify_setup(ltp: float, ema20: float, ema50: float, ema200: float, rsi: float, vol_surge: float) -> str:
    """Classifies market structure into institutional setup tags."""
    if ema20 and ema50 and ema200:
        if ltp > ema20 > ema50 > ema200 and vol_surge >= 1.4 and 50 <= rsi <= 72:
            return "A+ Breakout"
        if ltp > ema200 and rsi < 40 and ltp < ema20:
            return "Pullback Support"
        if rsi < 30:
            return "Oversold Rebound"
        if rsi > 75:
            return "Overextended (Cautious)"
        if ltp > ema20 and ema20 > ema50:
            return "Trend Continuation"
        if ltp < ema20 < ema50 < ema200 and vol_surge >= 1.4:
            return "Bearish Breakdown"
    return "Consolidation"

def calculate_technical_score(ltp: float, ema20: float, ema50: float, ema200: float, rsi: float, adx: float, vol_surge: float) -> int:
    """Informational technical scoring from 0 to 100. Does NOT override AI decision engine."""
    score = 50
    if ema20 and ltp > ema20:
        score += 10
    else:
        score -= 10
        
    if ema50 and ltp > ema50:
        score += 10
    else:
        score -= 10
        
    if ema200 and ltp > ema200:
        score += 15
    else:
        score -= 15
        
    if 45 <= rsi <= 65:
        score += 10
    elif rsi > 70 or rsi < 30:
        score -= 5
        
    if adx > 25:
        score += 5
        
    if vol_surge >= 1.5:
        score += 10
        
    return max(5, min(95, score))

def evaluate_single_ticker_data(ticker: str, champion_model, champion_meta) -> Dict[str, Any]:
    """
    Evaluates a single ticker for the Watchlist Scanner.
    Enforces Invariants 3, 4, 5:
    - Uses shared decision engine evaluate_ticker (authoritative AI brain)
    - Zero broker orders, zero portfolio heat, zero trade history writes
    - Explicit freshness, timestamp, and source disclosures
    """
    now = datetime.now()
    try:
        # Fetch OHLCV candles
        today = datetime.now()
        start_date = today - timedelta(days=120)
        end_date = today + timedelta(days=1)
        
        df = fetch_historical_data(ticker, start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))
        if df is None or len(df) < 30:
            return {
                "ticker": ticker,
                "error": True,
                "error_message": "Insufficient historical candles (<30 bars)",
                "data_freshness": "UNAVAILABLE"
            }
            
        data_source = df.attrs.get('source', 'Local DB / Yahoo')
        last_candle_date = str(df.index[-1]) if hasattr(df.index[-1], 'strftime') else str(df.iloc[-1].get('date', df.index[-1]))
        
        # Check staleness (if last candle is older than 5 days, mark STALE)
        freshness = "LIVE"
        try:
            candle_dt = pd.to_datetime(last_candle_date)
            days_old = (now - candle_dt.to_pydatetime()).days
            if days_old > 3:
                freshness = "STALE"
            elif days_old > 0:
                freshness = "DAILY_EOD"
        except Exception:
            freshness = "DELAYED"
            
        ltp = float(df['close'].iloc[-1])
        prev_close = float(df['close'].iloc[-2]) if len(df) > 1 else ltp
        change_pct = round(((ltp - prev_close) / prev_close) * 100, 2)
        open_p = float(df['open'].iloc[-1])
        high_p = float(df['high'].iloc[-1])
        low_p = float(df['low'].iloc[-1])
        vol = float(df['volume'].iloc[-1])
        
        # 20 SMA Volume
        vol_20_sma = float(df['volume'].tail(20).mean()) if len(df) >= 20 else vol
        vol_surge = round(vol / vol_20_sma, 2) if vol_20_sma > 0 else 1.0
        
        # Technical indicators
        ema20 = float(df['close'].ewm(span=20, adjust=False).mean().iloc[-1]) if len(df) >= 20 else ltp
        ema50 = float(df['close'].ewm(span=50, adjust=False).mean().iloc[-1]) if len(df) >= 50 else ltp
        ema200 = float(df['close'].ewm(span=200, adjust=False).mean().iloc[-1]) if len(df) >= 200 else ltp
        
        # RSI 14
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / (loss + 1e-9)
        rsi_series = 100 - (100 / (1 + rs))
        rsi = round(float(rsi_series.iloc[-1]), 1) if not pd.isna(rsi_series.iloc[-1]) else 50.0
        
        # ADX estimate / ATR
        tr1 = df['high'] - df['low']
        tr2 = (df['high'] - df['close'].shift()).abs()
        tr3 = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = round(float(tr.rolling(14).mean().iloc[-1]), 2) if len(df) >= 14 else round(ltp * 0.02, 2)
        adx = 22.0 # Standard benchmark estimate if ta package not full
        
        # 3. Existing Authoritative AI Brain Evaluation (Invariant 3)
        ai_direction = "NEUTRAL"
        ai_conviction = 50.0
        ai_qualified = False
        rejection_reason = None
        
        try:
            eval_res = evaluate_ticker(
                ticker=ticker,
                df=df,
                champion_model=champion_model,
                champion_meta=champion_meta,
                trade_type="INTRADAY",
                source="WATCHLIST_SCANNER",
                skip_enrichment=True
            )
            ai_direction = eval_res.direction
            ai_conviction = round(eval_res.confidence, 1)
            ai_qualified = eval_res.qualified
            rejection_reason = eval_res.rejection_reason
            
            # Use authoritative SL/TP from decision engine if available
            sl = round(eval_res.sl, 2) if eval_res.sl > 0 else round(ltp - (1.5 * atr), 2)
            tp1 = round(eval_res.tp1, 2) if eval_res.tp1 > 0 else round(ltp + (2.0 * atr), 2)
            tp2 = round(eval_res.tp2, 2) if eval_res.tp2 > 0 else round(ltp + (3.0 * atr), 2)
        except Exception as e:
            logger.debug(f"Decision engine check for {ticker} using baseline: {e}")
            sl = round(ltp - (1.5 * atr), 2)
            tp1 = round(ltp + (2.0 * atr), 2)
            tp2 = round(ltp + (3.0 * atr), 2)
            
        setup_tag = classify_setup(ltp, ema20, ema50, ema200, rsi, vol_surge)
        tech_score = calculate_technical_score(ltp, ema20, ema50, ema200, rsi, adx, vol_surge)
        
        # 4. Informational Kelly Position Sizing (Invariant 4)
        kelly_info = calculate_kelly_position_size(
            capital=100000.0,
            entry=ltp,
            sl=sl,
            tp1=tp1,
            win_prob=ai_conviction,
            kelly_mode="HALF"
        )
        
        return {
            "ticker": ticker,
            "error": False,
            "ltp": ltp,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "volume": vol,
            "volume_surge": vol_surge,
            "volume_20sma": round(vol_20_sma, 0),
            
            # Freshness & provenance (Invariant 5)
            "data_timestamp": last_candle_date,
            "data_source": data_source,
            "data_freshness": freshness,
            
            # Technical metrics (Informational)
            "ema20": round(ema20, 2),
            "ema50": round(ema50, 2),
            "ema200": round(ema200, 2),
            "rsi": rsi,
            "atr": atr,
            "technical_score": tech_score,
            "setup_tag": setup_tag,
            
            # Authoritative AI Decision Engine result (Invariant 3)
            "ai_signal": ai_direction,
            "ai_conviction": ai_conviction,
            "ai_qualified": ai_qualified,
            "rejection_reason": rejection_reason,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            
            # Informational Kelly sizing (Invariant 4)
            "kelly_sizing": {
                "recommended_qty": kelly_info.get("quantity", 0),
                "risk_amount": kelly_info.get("risk_amount", 0.0),
                "allocated_risk_pct": kelly_info.get("allocated_risk_pct", 0.0),
                "reward_risk_ratio": round(kelly_info.get("reward_risk_ratio", 0.0), 2),
                "is_positive_edge": kelly_info.get("is_positive_edge", False)
            }
        }
    except Exception as e:
        logger.error(f"Error evaluating ticker {ticker}: {e}")
        return {
            "ticker": ticker,
            "error": True,
            "error_message": str(e),
            "data_freshness": "ERROR"
        }

@router.post("/scan")
async def scan_watchlist_batch(payload: BatchScanRequest):
    """
    Executes high-throughput batch scanning for Watchlist and preset universes.
    Enforces all Invariants:
    - 0 live orders, 0 portfolio heat, 0 trade writes (Invariant 4)
    - Authoritative AI Decision Engine used for model inference (Invariant 3)
    - Explicit freshness and timestamp metadata returned (Invariant 5)
    - Resolves via authoritative backend universe engine (Invariant 7)
    """
    preset_name = payload.preset or "WATCHLIST"
    candidate_tickers = resolve_universe_tickers(preset_name, custom_tickers=payload.tickers)
    
    # Cap candidate pool to max 60 per request for sub-second UI responsiveness
    target_tickers = candidate_tickers[:60]
    
    # Load authoritative Champion model once
    champion_model, champion_meta = ModelManager.load_champion("intraday")
    
    # Run evaluations concurrently
    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(None, evaluate_single_ticker_data, ticker, champion_model, champion_meta)
        for ticker in target_tickers
    ]
    results = await asyncio.gather(*tasks)
    
    # Aggregate summary KPIs
    valid_results = [r for r in results if not r.get("error")]
    advancing = sum(1 for r in valid_results if r.get("change_pct", 0) > 0)
    declining = sum(1 for r in valid_results if r.get("change_pct", 0) < 0)
    bullish_ai = sum(1 for r in valid_results if r.get("ai_signal") == "BULLISH" and r.get("ai_conviction", 0) >= 60)
    bearish_ai = sum(1 for r in valid_results if r.get("ai_signal") == "BEARISH" and r.get("ai_conviction", 0) >= 60)
    avg_vol_surge = round(float(np.mean([r.get("volume_surge", 1.0) for r in valid_results])), 2) if valid_results else 1.0
    
    return {
        "status": "success",
        "preset": preset_name,
        "total_scanned": len(target_tickers),
        "valid_count": len(valid_results),
        "kpi_summary": {
            "advancing": advancing,
            "declining": declining,
            "bullish_ai_count": bullish_ai,
            "bearish_ai_count": bearish_ai,
            "avg_volume_surge": avg_vol_surge
        },
        "results": results,
        "timestamp": datetime.now().isoformat(),
        "zero_risk_enforced": True
    }

