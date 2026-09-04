import os
import time
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Generator, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf
import ta

from app.analytics.universe_config import resolve_universe_tickers, LIVE_UNIVERSE
from app.api.ml_history import save_ml_training_data
from app.analytics.master_logger import MasterLogger

logger = logging.getLogger(__name__)

def format_sse(data: dict) -> str:
    """Formats payload as Server-Sent Event string."""
    return f"{json.dumps(data)}\n"

def process_single_ticker_hoard(ticker: str, data_source: str = 'yfinance', api_key: str = '') -> Tuple[bool, int, str]:
    """
    Fetches 15m intraday candle data for a single equity symbol, computes technical indicators,
    and persists to SQLite ml_training_data table.
    Returns (success: bool, row_count: int, message: str).
    """
    clean_ticker = ticker.strip().upper()
    if not clean_ticker.endswith(('.NS', '.BO')):
        clean_ticker = f"{clean_ticker}.NS"

    try:
        df = pd.DataFrame()

        if data_source == 'groww':
            import requests
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            symbol = clean_ticker.replace('.NS', '')
            res = requests.get(
                f"https://api.groww.in/v1/historical/candle/range?exchange=NSE&segment=CASH&trading_symbol={symbol}&interval_in_minutes=15",
                headers=headers, timeout=10.0
            )
            if res.status_code != 200:
                return False, 0, f"Groww API Error: {res.status_code}"
            data = res.json()
            candles = data.get('candles', data.get('data', []))
            if not candles:
                return False, 0, "No candles returned by Groww."
            if isinstance(candles[0], list):
                df = pd.DataFrame(candles, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
                if len(str(df['datetime'].iloc[0])) == 10:
                    df['datetime'] = pd.to_datetime(df['datetime'], unit='s')
                else:
                    df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
            else:
                df = pd.DataFrame(candles)

        elif data_source == 'upstox':
            from app.data.upstox_provider import fetch_upstox_candles
            df = fetch_upstox_candles(clean_ticker, interval='15m', period='60d')

        elif data_source == 'dhan':
            import requests
            headers = {"access-token": api_key, "client-id": "SWING_AI"}
            res = requests.post(
                "https://api.dhan.co/charts/intraday",
                json={"securityId": clean_ticker, "exchangeSegment": "NSE_EQ", "instrument": "EQUITY"},
                headers=headers, timeout=10.0
            )
            if res.status_code != 200:
                return False, 0, f"Dhan API Error: {res.status_code}"

        elif data_source == 'zerodha':
            import requests
            headers = {"X-Kite-Version": "3", "Authorization": f"token {api_key}"}
            res = requests.get(
                f"https://api.kite.trade/instruments/historical/123456/15minute",
                headers=headers, timeout=10.0
            )
            if res.status_code != 200:
                return False, 0, f"Zerodha API Error: {res.status_code}"

        else:
            # Default Yahoo Finance Provider
            df = yf.download(clean_ticker, period="60d", interval="15m", progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)

        if df is None or df.empty or len(df) < 10:
            return False, 0, f"No 15m data returned for {clean_ticker}"

        # Standardize columns
        df = df.rename(columns={
            'Open': 'open', 'High': 'high', 'Low': 'low', 
            'Close': 'close', 'Volume': 'volume'
        })
        df = df.reset_index()
        df = df.rename(columns={df.columns[0]: 'datetime'})

        # Technical Indicators
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        macd = ta.trend.MACD(df['close'])
        df['macd'] = macd.macd()
        df['macd_diff'] = macd.macd_diff()
        df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
        df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()

        # ML Target
        df['returns'] = df['close'].pct_change()
        df['target'] = (df['returns'].shift(-1) > 0).astype(int)

        ml_df = df.dropna().copy()
        if ml_df.empty:
            return False, 0, f"Cleaned feature dataframe empty for {clean_ticker}"

        ml_df['datetime'] = ml_df['datetime'].astype(str)
        ml_df.attrs['source'] = data_source

        save_ml_training_data(clean_ticker, ml_df)
        return True, len(ml_df), f"Saved {len(ml_df)} 15m bars for {clean_ticker}"

    except Exception as e:
        return False, 0, str(e)


def hoard_intraday_data(
    universe: str = 'NIFTY_500',
    custom_tickers: Optional[List[str]] = None,
    data_source: str = 'yfinance',
    api_key: str = '',
    max_workers: int = 8,
    batch_size: int = 25
) -> Dict[str, Any]:
    """
    Background batch job to fetch 15m intraday data for any configured universe,
    compute technical features, and store in SQLite ml_training_data table.
    """
    start_time = time.time()
    tickers = resolve_universe_tickers(universe, custom_tickers=custom_tickers)
    total_tickers = len(tickers)

    logger.info(f"Starting Data Hoarder for universe '{universe}' ({total_tickers} symbols, workers={max_workers})...")
    MasterLogger.log_event(
        "SCHEDULER", "HOARDER_STARTED",
        f"Data Hoarder started 15m candle sync for {total_tickers} symbols ({universe}, {data_source})",
        universe=universe,
        details={"universe": universe, "total_tickers": total_tickers, "source": data_source}
    )

    success_count = 0
    fail_count = 0
    results_detail = []

    # Process in chunks to bound memory and respect exchange rate limits
    for i in range(0, total_tickers, batch_size):
        chunk = tickers[i:i + batch_size]
        with ThreadPoolExecutor(max_workers=min(max_workers, len(chunk))) as executor:
            future_to_ticker = {
                executor.submit(process_single_ticker_hoard, t, data_source, api_key): t
                for t in chunk
            }
            for future in as_completed(future_to_ticker):
                t = future_to_ticker[future]
                try:
                    success, bars, msg = future.result()
                    if success:
                        success_count += 1
                        results_detail.append({"ticker": t, "status": "SUCCESS", "bars": bars, "msg": msg})
                    else:
                        fail_count += 1
                        results_detail.append({"ticker": t, "status": "FAILED", "bars": 0, "msg": msg})
                except Exception as e:
                    fail_count += 1
                    results_detail.append({"ticker": t, "status": "ERROR", "bars": 0, "msg": str(e)})

        # Brief rate limiting pause between batches
        time.sleep(0.5)

    elapsed = round(time.time() - start_time, 2)
    cov_pct = round((success_count / total_tickers * 100.0), 2) if total_tickers > 0 else 0.0

    MasterLogger.log_event(
        "SCHEDULER", "HOARDER_COMPLETED",
        f"Data Hoarder completed: {success_count}/{total_tickers} synced ({cov_pct}% coverage) in {elapsed}s",
        universe=universe,
        details={"universe": universe, "success": success_count, "failed": fail_count, "coverage_pct": cov_pct, "elapsed": elapsed}
    )

    return {
        "status": "COMPLETED",
        "universe": universe,
        "total_requested": total_tickers,
        "success_count": success_count,
        "fail_count": fail_count,
        "coverage_percent": cov_pct,
        "elapsed_seconds": elapsed,
        "results": results_detail
    }


def hoard_intraday_data_stream(
    universe: str = 'NIFTY_500',
    custom_tickers: Optional[List[str]] = None,
    data_source: str = 'yfinance',
    api_key: str = '',
    max_workers: int = 8,
    batch_size: int = 25
) -> Generator[str, None, None]:
    """
    Generator yielding Server-Sent Events (SSE) for streaming real-time Data Hoarder telemetry to UI.
    """
    start_time = time.time()
    tickers = resolve_universe_tickers(universe, custom_tickers=custom_tickers)
    total_tickers = len(tickers)

    yield format_sse({
        "type": "info",
        "universe": universe,
        "total": total_tickers,
        "message": f"Starting Data Hoarder sync for {total_tickers} stocks ({universe})..."
    })

    MasterLogger.log_event(
        "SCHEDULER", "HOARDER_STARTED",
        f"Streaming Data Hoarder started for {total_tickers} symbols ({universe})",
        universe=universe,
        details={"universe": universe, "total": total_tickers}
    )

    success_count = 0
    fail_count = 0
    processed_count = 0

    for i in range(0, total_tickers, batch_size):
        chunk = tickers[i:i + batch_size]
        with ThreadPoolExecutor(max_workers=min(max_workers, len(chunk))) as executor:
            future_to_ticker = {
                executor.submit(process_single_ticker_hoard, t, data_source, api_key): t
                for t in chunk
            }
            for future in as_completed(future_to_ticker):
                t = future_to_ticker[future]
                processed_count += 1
                try:
                    ok, count, msg = future.result()
                    if ok:
                        success_count += 1
                        yield format_sse({
                            "type": "progress",
                            "ticker": t,
                            "status": "SUCCESS",
                            "completed": processed_count,
                            "total": total_tickers,
                            "progress_pct": round(processed_count / total_tickers * 100, 1),
                            "message": f"[{processed_count}/{total_tickers}] {t} - {count} candles cached."
                        })
                    else:
                        fail_count += 1
                        yield format_sse({
                            "type": "warning",
                            "ticker": t,
                            "status": "FAILED",
                            "completed": processed_count,
                            "total": total_tickers,
                            "progress_pct": round(processed_count / total_tickers * 100, 1),
                            "message": f"[{processed_count}/{total_tickers}] {t} - {msg}"
                        })
                except Exception as e:
                    fail_count += 1
                    yield format_sse({
                        "type": "error",
                        "ticker": t,
                        "status": "ERROR",
                        "completed": processed_count,
                        "total": total_tickers,
                        "progress_pct": round(processed_count / total_tickers * 100, 1),
                        "message": f"[{processed_count}/{total_tickers}] {t} error: {str(e)}"
                    })

        if i + batch_size < total_tickers:
            time.sleep(0.3)

    elapsed = round(time.time() - start_time, 2)
    MasterLogger.log_event(
        "SCHEDULER", "HOARDER_COMPLETED",
        f"Streaming Hoarder finished for {universe} in {elapsed}s. Success: {success_count}, Failed: {fail_count}",
        universe=universe,
        details={"elapsed_seconds": elapsed, "success": success_count, "failed": fail_count}
    )

    yield format_sse({
        "type": "completed",
        "universe": universe,
        "total": total_tickers,
        "success": success_count,
        "failed": fail_count,
        "elapsed_seconds": elapsed,
        "message": f"Hoarding complete! {success_count}/{total_tickers} stocks successfully synchronized in {elapsed}s."
    })
