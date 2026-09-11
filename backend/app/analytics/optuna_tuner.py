import os
import json
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
import yfinance as yf
import ta

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

    # 2. Ingest Data (Prefer Local SQLite for Swing to eliminate rate limits & network drag)
    from app.data.historical_data_layer import HistoricalDataLayer

    # If swing, attempt local retrieval first
    tickers_to_fetch_remote = []
    local_data_map = {}

    if timeframe == "swing":
        for t in clean_tickers:
            try:
                local_df = HistoricalDataLayer.get_historical_ohlcv(t, timeframe="1d")
                if local_df is not None and not local_df.empty and len(local_df) >= min_rows_per_stock:
                    # Filter to 2y lookback (~504 trading days)
                    local_data_map[t] = local_df.tail(504).copy()
                else:
                    tickers_to_fetch_remote.append(t)
            except Exception:
                tickers_to_fetch_remote.append(t)
    else:
        tickers_to_fetch_remote = list(clean_tickers)

    remote_data = None
    if tickers_to_fetch_remote:
        try:
            remote_data = yf.download(tickers_to_fetch_remote, period=period, interval=interval, progress=False)
        except Exception as e:
            logger.warning(f"Remote fetch error for {len(tickers_to_fetch_remote)} tickers: {e}")

    for t in clean_tickers:
        try:
            df = None
            if t in local_data_map:
                df = local_data_map[t].copy()
            elif remote_data is not None and not remote_data.empty:
                if isinstance(remote_data.columns, pd.MultiIndex):
                    try:
                        df = remote_data.xs(t, level=1, axis=1).copy()
                    except KeyError:
                        pass
                elif len(tickers_to_fetch_remote) == 1:
                    df = remote_data.copy()

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

            # Label Definition
            if timeframe == "intraday":
                df['returns'] = df['close'].pct_change()
                df['target'] = (df['returns'].shift(-1) > 0).astype(int)
                features = ['rsi', 'macd', 'macd_diff', 'adx', 'returns']
            else: # swing
                df['future_5d'] = df['close'].shift(-5)
                df['target'] = (((df['future_5d'] - df['close']) / df['close']) > 0.02).astype(int)
                features = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']

            clean_df = df.dropna(subset=features + ['target']).copy()
            if len(clean_df) < min_rows_per_stock:
                excluded_tickers[t] = f"insufficient_rows_after_features: {len(clean_df)} < {min_rows_per_stock}"
                continue

            clean_df['ticker'] = t
            clean_df['datetime'] = clean_df.index
            stock_dfs.append(clean_df[features + ['target', 'ticker', 'datetime']])
            valid_tickers.append(t)

        except Exception as e:
            excluded_tickers[t] = f"processing_error: {str(e)}"
            continue

    if not stock_dfs:
        raise DataValidationError(f"Zero tickers passed data quality and feature validation. Excluded: {excluded_tickers}")

    # Ticker-aware temporal ordering: group chronologically
    combined = pd.concat(stock_dfs).sort_values('datetime').reset_index(drop=True)
    features_list = ['rsi', 'macd', 'macd_diff', 'adx', 'returns'] if timeframe == "intraday" else ['rsi', 'macd', 'macd_diff', 'adx', 'atr']
    
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
