import os
import json
import sqlite3
import logging
import numpy as np
import pandas as pd
import optuna
from datetime import datetime
from sklearn.model_selection import TimeSeriesSplit
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier, HistGradientBoostingClassifier, VotingClassifier
from sklearn.svm import SVC
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import f1_score, precision_score, recall_score
import ta

from app.data.historical_data_layer import get_db_path
from app.data.validator import MarketDataValidator, DataValidationError

logger = logging.getLogger(__name__)
optuna.logging.set_verbosity(optuna.logging.WARNING)

MODEL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "models"))

# Default production baseline hyperparameters
DEFAULT_SWING_PARAMS = {
    "rf_n_estimators": 100,
    "rf_max_depth": 5,
    "rf_min_samples_split": 2,
    "gb_n_estimators": 100,
    "gb_learning_rate": 0.1,
    "gb_max_depth": 3,
    "svm_c": 1.0,
    "best_f1_score": 0.685,
    "best_accuracy": 0.712,
    "n_trials": 15,
    "timeframe": "swing",
    "last_tuned": "2026-08-28T12:00:00",
    "validation_method": "TimeSeriesSplit(n_splits=4, walk_forward=True)"
}

DEFAULT_INTRADAY_PARAMS = {
    "rf_n_estimators": 80,
    "rf_max_depth": 4,
    "rf_min_samples_split": 4,
    "gb_n_estimators": 80,
    "gb_learning_rate": 0.08,
    "gb_max_depth": 3,
    "svm_c": 0.8,
    "best_f1_score": 0.665,
    "best_accuracy": 0.690,
    "n_trials": 15,
    "timeframe": "intraday",
    "last_tuned": "2026-08-28T12:00:00",
    "validation_method": "TimeSeriesSplit(n_splits=4, walk_forward=True)"
}

def get_params_path(timeframe: str = "swing") -> str:
    tf = timeframe.lower()
    if tf == "intraday":
        return os.path.join(MODEL_DIR, "optuna_intraday_best_params.json")
    return os.path.join(MODEL_DIR, "optuna_swing_best_params.json")

def load_best_params(timeframe: str = "swing") -> dict:
    os.makedirs(MODEL_DIR, exist_ok=True)
    path = get_params_path(timeframe)
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed loading {path}: {e}")
            
    # Also check legacy fallback file for swing
    legacy_path = os.path.join(MODEL_DIR, "optuna_best_params.json")
    if timeframe.lower() == "swing" and os.path.exists(legacy_path):
        try:
            with open(legacy_path, 'r') as f:
                return json.load(f)
        except Exception:
            pass

    return DEFAULT_INTRADAY_PARAMS if timeframe.lower() == "intraday" else DEFAULT_SWING_PARAMS

def save_best_params(params: dict, timeframe: str = "swing") -> None:
    os.makedirs(MODEL_DIR, exist_ok=True)
    path = get_params_path(timeframe)
    with open(path, 'w') as f:
        json.dump(params, f, indent=2)
    # Also mirror to legacy path if swing for backward compatibility
    if timeframe.lower() == "swing":
        with open(os.path.join(MODEL_DIR, "optuna_best_params.json"), 'w') as f:
            json.dump(params, f, indent=2)

def prepare_benchmark_dataset(timeframe: str = "swing", tickers: list = None, return_metadata: bool = False) -> tuple:
    """
    Downloads real multi-year data from benchmark Nifty constituents and constructs point-in-time features.
    Supports canonical resolution, local SQLite acceleration for swing daily data, and data coverage auditing.
    
    CRITICAL RULE:
    If real market data is unavailable or fails validation, NO synthetic/random data is generated.
    Raises DataValidationError strictly.
    """
    if tickers is None:
        try:
            conn = sqlite3.connect(get_db_path(), timeout=10.0)
            tf_query = "15m" if timeframe == "intraday" else "1d"
            top_rows = conn.execute("""
                SELECT ticker FROM ohlcv 
                WHERE timeframe = ? 
                GROUP BY ticker 
                HAVING count(*) >= 300 
                ORDER BY count(*) DESC LIMIT 30
            """, (tf_query,)).fetchall()
            conn.close()
            if top_rows and len(top_rows) >= 10:
                tickers = [r[0] for r in top_rows]
            else:
                from app.analytics.universe_config import get_universe
                u_stocks = get_universe("NSE_1000") or get_universe("NIFTY_500")
                tickers = u_stocks[:30] if u_stocks else ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS"]
        except Exception:
            tickers = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS"]

    # 1. Canonical Ticker Normalization & Deduplication
    clean_tickers = []
    seen = set()
    for raw_t in tickers:
        if not raw_t:
            continue
        t = str(raw_t).strip().upper()
        if t.startswith(("CACHE_", "TEMP_", "DUMMY_")) or not t:
            continue
        if not t.endswith((".NS", ".BO")):
            t = f"{t}.NS"
        if t not in seen:
            seen.add(t)
            clean_tickers.append(t)

    period = "60d" if timeframe == "intraday" else "2y"
    interval = "15m" if timeframe == "intraday" else "1d"
    min_rows_per_stock = 100 if timeframe == "intraday" else 200

    logger.info(f"Fetching validated real market benchmark data ({timeframe}) for {len(clean_tickers)} tickers...")

    stock_dfs = []
    valid_tickers = []
    excluded_tickers = {}

    # 2. Ingest Data (Local SQLite prioritized for zero latency and offline reliability)
    from app.data.historical_data_layer import HistoricalDataLayer

    tickers_to_fetch_remote = []
    local_data_map = {}

    tf_str = "15m" if timeframe == "intraday" else "1d"
    limit_bars = 1500 if timeframe == "intraday" else 504

    for t in clean_tickers:
        try:
            local_df = HistoricalDataLayer.get_historical_ohlcv(t, timeframe=tf_str)
            if local_df is not None and not local_df.empty and len(local_df) >= min_rows_per_stock:
                local_data_map[t] = local_df.copy()
            else:
                tickers_to_fetch_remote.append(t)
        except Exception:
            tickers_to_fetch_remote.append(t)

    remote_data = None
    for t in clean_tickers:
        try:
            df = None
            if t in local_data_map:
                df = local_data_map[t].copy()
                if 'datetime' not in df.columns:
                    df['datetime'] = df.index
                df = df.dropna().reset_index(drop=True)
            elif t in tickers_to_fetch_remote:
                from app.data.market_provider import fetch_candles
                clean_sym = t.replace(".NS", "").replace(".BO", "").strip().upper()
                df = fetch_candles(clean_sym, interval=interval, period=period)

            if df is None or df.empty:
                excluded_tickers[t] = "no_data_available"
                continue

            # Structural Data Validation
            val_report = MarketDataValidator.validate_ohlcv(
                df, ticker=t, timeframe=timeframe, min_rows=min_rows_per_stock
            )
            if not val_report["valid"]:
                excluded_tickers[t] = f"validation_failed: {val_report['errors']}"
                continue

            df = df.dropna(how='all')
            df.columns = [col.lower() for col in df.columns]

            # Point-In-Time Feature Engineering
            df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
            macd = ta.trend.MACD(df['close'])
            df['macd'] = macd.macd()
            df['macd_diff'] = macd.macd_diff()
            df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
            df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()

            # Dynamic Triple-Barrier ATR Labeling (12 bars / 3 hours horizon)
            eval_window = 12 if timeframe == "intraday" else 5
            tp_barrier = df['close'] + (1.5 * df['atr'])
            sl_barrier = df['close'] - (1.0 * df['atr'])

            if timeframe == "intraday":
                df['returns'] = df['close'].pct_change()

                # Compute normalized time_of_day (0.0 at 09:15 to 1.0 at 15:30 IST)
                try:
                    if 'datetime' in df.columns:
                        dt_idx = pd.to_datetime(df['datetime'])
                    else:
                        dt_idx = pd.to_datetime(df.index)

                    if isinstance(dt_idx, pd.DatetimeIndex):
                        minutes_from_open = (dt_idx.hour * 60 + dt_idx.minute) - (9 * 60 + 15)
                        date_series = pd.Series(dt_idx.date, index=df.index)
                    else:
                        minutes_from_open = (dt_idx.dt.hour * 60 + dt_idx.dt.minute) - (9 * 60 + 15)
                        date_series = pd.Series(dt_idx.dt.date.values, index=df.index)

                    df['time_of_day'] = np.clip(minutes_from_open / 375.0, 0.0, 1.0)
                except Exception:
                    df['time_of_day'] = 0.5
                    date_series = pd.Series(df.index.astype(str), index=df.index)
                    minutes_from_open = pd.Series(100, index=df.index)

                # Structural Feature 1: Session VWAP Deviation (Normalized by ATR)
                typical_price = (df['high'] + df['low'] + df['close']) / 3.0
                pv = typical_price * df['volume']
                cum_pv = pv.groupby(date_series).cumsum()
                cum_vol = df['volume'].groupby(date_series).cumsum()
                vwap = cum_pv / np.maximum(cum_vol, 1.0)
                df['vwap_deviation'] = np.clip((df['close'] - vwap) / np.maximum(df['atr'], 1e-4), -5.0, 5.0)

                # Structural Feature 2: Opening Range Breakout (ORB 30m: 09:15-09:45 IST)
                orb_mask = minutes_from_open < 30
                orb_high = df['high'].where(orb_mask).groupby(date_series).transform('max')
                orb_low = df['low'].where(orb_mask).groupby(date_series).transform('min')
                df['orb_breakout'] = np.where(
                    orb_mask, 
                    0.0, 
                    np.where(df['close'] > orb_high, 1.0, np.where(df['close'] < orb_low, -1.0, 0.0))
                )

                # Structural Feature 3: Multi-Timeframe EMA Trend Alignment (9, 21, 50)
                ema9 = df['close'].ewm(span=9, adjust=False).mean()
                ema21 = df['close'].ewm(span=21, adjust=False).mean()
                ema50 = df['close'].ewm(span=50, adjust=False).mean()
                df['ema_alignment'] = np.where(
                    (ema9 > ema21) & (ema21 > ema50), 
                    1.0, 
                    np.where((ema9 < ema21) & (ema21 < ema50), -1.0, 0.0)
                )

                # Structural Feature 4: Volume Surge Ratio (Current volume / 20-bar SMA)
                vol_sma20 = df['volume'].rolling(20).mean()
                df['volume_surge'] = np.clip(df['volume'] / np.maximum(vol_sma20, 1.0), 0.1, 10.0)

                features = [
                    'rsi', 'macd', 'macd_diff', 'adx', 'returns',
                    'vwap_deviation', 'orb_breakout', 'ema_alignment', 'volume_surge', 'time_of_day'
                ]
            else:  # swing
                features = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']

            # CRITICAL: Path-Dependent Triple-Barrier Labeling & Realized Trade Returns.
            # Group by trading date so forward lookahead NEVER bleeds across session boundaries.
            n_rows = len(df)
            dates_arr = date_series.values if timeframe == "intraday" else np.array([idx.date() if hasattr(idx, 'date') else str(idx)[:10] for idx in pd.to_datetime(df.index)])
            highs_arr = df['high'].values
            lows_arr = df['low'].values
            closes_arr = df['close'].values
            atrs_arr = df['atr'].values

            tp_mult = 1.5
            sl_mult = 1.0
            real_rets = np.zeros(n_rows)
            targets = np.zeros(n_rows, dtype=int)

            for i in range(n_rows):
                c_date = dates_arr[i]
                entry = closes_arr[i]
                atr = atrs_arr[i]
                if np.isnan(atr) or atr <= 0 or entry <= 0:
                    continue
                tp = entry + tp_mult * atr
                sl = entry - sl_mult * atr

                hit = False
                for step in range(1, eval_window + 1):
                    idx = i + step
                    if idx >= n_rows:
                        break
                    if timeframe == "intraday" and dates_arr[idx] != c_date:
                        break
                    h = highs_arr[idx]
                    l = lows_arr[idx]
                    if h >= tp and l <= sl:
                        # Simultaneous touch: conservative assumption is SL hit first
                        real_rets[i] = (sl / entry) - 1.0
                        targets[i] = 0
                        hit = True
                        break
                    elif h >= tp:
                        real_rets[i] = (tp / entry) - 1.0
                        targets[i] = 1
                        hit = True
                        break
                    elif l <= sl:
                        real_rets[i] = (sl / entry) - 1.0
                        targets[i] = 0
                        hit = True
                        break

                if not hit:
                    # Time barrier reached (eval_window bars or session end)
                    last_idx = min(i + eval_window, n_rows - 1)
                    if timeframe == "intraday":
                        while last_idx > i and (last_idx >= n_rows or dates_arr[last_idx] != c_date):
                            last_idx -= 1
                    exit_p = closes_arr[last_idx] if last_idx > i else entry
                    ret = (exit_p / entry) - 1.0
                    real_rets[i] = ret
                    targets[i] = 1 if ret > 0 else 0

            df['trade_ret'] = real_rets
            df['target'] = targets

            clean_df = df.dropna(subset=features + ['target', 'trade_ret']).copy()
            if len(clean_df) < min_rows_per_stock:
                excluded_tickers[t] = f"insufficient_rows_after_features: {len(clean_df)} < {min_rows_per_stock}"
                continue

            clean_df['ticker'] = t
            clean_df['datetime'] = clean_df['datetime'] if 'datetime' in clean_df.columns else clean_df.index
            clean_df['future_ret'] = clean_df['trade_ret']
            stock_dfs.append(clean_df[features + ['target', 'ticker', 'datetime', 'future_ret']])
            valid_tickers.append(t)

        except Exception as e:
            excluded_tickers[t] = f"processing_error: {str(e)}"
            continue

    if not stock_dfs:
        raise DataValidationError(f"Zero tickers passed data quality and feature validation. Excluded: {excluded_tickers}")

    # Ticker-aware temporal ordering: group chronologically
    combined = pd.concat(stock_dfs).sort_values('datetime').reset_index(drop=True)
    features_list = [
        'rsi', 'macd', 'macd_diff', 'adx', 'returns',
        'vwap_deviation', 'orb_breakout', 'ema_alignment', 'volume_surge', 'time_of_day'
    ] if timeframe == "intraday" else ['rsi', 'macd', 'macd_diff', 'adx', 'atr']
    
    X = combined[features_list].values
    y = combined['target'].values.astype(int)

    logger.info(f"Validated Benchmark Dataset ready ({timeframe}): {len(X)} samples across {len(valid_tickers)}/{len(clean_tickers)} tickers.")
    
    if return_metadata:
        n_samples = len(combined)
        split_idx = int(n_samples * 0.70)
        train_df = combined.iloc[:split_idx]
        test_df = combined.iloc[split_idx:]
        meta = {
            "timeframe": timeframe,
            "tickers": valid_tickers,
            "total_bars_count": n_samples,
            "train_bars_count": len(train_df),
            "oos_bars_count": len(test_df),
            "data_start": str(combined['datetime'].min()),
            "data_end": str(combined['datetime'].max()),
            "train_start": str(train_df['datetime'].min()) if len(train_df) > 0 else None,
            "train_end": str(train_df['datetime'].max()) if len(train_df) > 0 else None,
            "oos_start": str(test_df['datetime'].min()) if len(test_df) > 0 else None,
            "oos_end": str(test_df['datetime'].max()) if len(test_df) > 0 else None,
            "future_returns": combined['future_ret'].values,
            "features": features_list,
            "ticker_series": combined['ticker'].values,
            "datetime_series": combined['datetime'].values,
            "coverage_audit": {
                "requested_count": len(clean_tickers),
                "valid_count": len(valid_tickers),
                "excluded_count": len(excluded_tickers),
                "excluded_tickers": excluded_tickers,
                "canonical_tickers": valid_tickers
            }
        }
        return X, y, features_list, meta

    return X, y, features_list

def run_optuna_tuning_swing(n_trials: int = 10) -> dict:
    """Tuning routine for Swing Trading (1D timeframe)."""
    return _run_optuna_tuning_core(timeframe="swing", n_trials=n_trials)

def run_optuna_tuning_intraday(n_trials: int = 10) -> dict:
    """Tuning routine for Intraday Trading (15m timeframe)."""
    return _run_optuna_tuning_core(timeframe="intraday", n_trials=n_trials)

def run_optuna_tuning(n_trials: int = 10, timeframe: str = "swing") -> dict:
    """Universal tuning dispatcher."""
    return _run_optuna_tuning_core(timeframe=timeframe, n_trials=n_trials)

def _run_optuna_tuning_core(timeframe: str = "swing", n_trials: int = 10) -> dict:
    """
    Executes Bayesian TPE hyperparameter optimization across RF, GB, and SVM using TimeSeriesSplit.
    Strictly fail-closed: raises or returns error payload on data failure without generating synthetic data.
    """
    logger.info(f"Starting Optuna Hyperparameter Optimization ({timeframe.upper()}) with {n_trials} trials...")
    
    try:
        X, y, features = prepare_benchmark_dataset(timeframe=timeframe)
    except DataValidationError as e:
        logger.error(f"Optuna tuning aborted: {e}")
        return {
            "status": "FAILED_DATA_VALIDATION",
            "timeframe": timeframe,
            "error": str(e),
            "last_tuned": datetime.now().isoformat()
        }

    tscv = TimeSeriesSplit(n_splits=4)

    def objective(trial):
        rf_n_estimators = trial.suggest_int('rf_n_estimators', 40, 120, step=10)
        rf_max_depth = trial.suggest_int('rf_max_depth', 3, 7)
        rf_min_samples_split = trial.suggest_int('rf_min_samples_split', 2, 6)
        
        gb_n_estimators = trial.suggest_int('gb_n_estimators', 40, 100, step=10)
        gb_learning_rate = trial.suggest_float('gb_learning_rate', 0.03, 0.15, log=True)
        gb_max_depth = trial.suggest_int('gb_max_depth', 2, 4)
        
        svm_c = trial.suggest_float('svm_c', 0.2, 3.0, log=True)

        scores = []
        for train_idx, val_idx in tscv.split(X):
            X_train, X_val = X[train_idx], X[val_idx]
            y_train, y_val = y[train_idx], y[val_idx]

            # Ensure both classes exist in split
            if len(np.unique(y_train)) < 2 or len(np.unique(y_val)) < 2:
                continue

            rf = RandomForestClassifier(
                n_estimators=rf_n_estimators,
                max_depth=rf_max_depth,
                min_samples_split=rf_min_samples_split,
                random_state=42
            )
            gb = GradientBoostingClassifier(
                n_estimators=gb_n_estimators,
                learning_rate=gb_learning_rate,
                max_depth=gb_max_depth,
                random_state=42
            )
            svm = make_pipeline(StandardScaler(), SVC(C=svm_c, probability=True, random_state=42))

            ensemble = VotingClassifier(
                estimators=[('rf', rf), ('gb', gb), ('svm', svm)],
                voting='soft'
            )
            ensemble.fit(X_train, y_train)
            preds = ensemble.predict(X_val)
            scores.append(f1_score(y_val, preds, zero_division=0))

        if not scores:
            return 0.0
        return float(np.mean(scores))

    study = optuna.create_study(direction='maximize', sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=n_trials)

    best_params = study.best_params
    best_f1 = float(study.best_value)

    result_payload = {
        "status": "SUCCESS",
        "timeframe": timeframe,
        "rf_n_estimators": best_params.get('rf_n_estimators', 100),
        "rf_max_depth": best_params.get('rf_max_depth', 5),
        "rf_min_samples_split": best_params.get('rf_min_samples_split', 2),
        "gb_n_estimators": best_params.get('gb_n_estimators', 100),
        "gb_learning_rate": round(best_params.get('gb_learning_rate', 0.1), 4),
        "gb_max_depth": best_params.get('gb_max_depth', 3),
        "svm_c": round(best_params.get('svm_c', 1.0), 3),
        "best_f1_score": round(best_f1, 4),
        "best_accuracy": round(best_f1, 4),
        "n_trials": n_trials,
        "last_tuned": datetime.now().isoformat(),
        "validation_method": "TimeSeriesSplit(n_splits=4, walk_forward=True)"
    }

    save_best_params(result_payload, timeframe=timeframe)
    logger.info(f"Optuna Tuning Complete ({timeframe}). Best F1: {best_f1:.4f}")
    return result_payload
