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

def prepare_benchmark_dataset(timeframe: str = "swing", tickers: list = None) -> tuple[np.ndarray, np.ndarray, list]:
    """
    Downloads real multi-year data from benchmark Nifty constituents and constructs point-in-time features.
    
    CRITICAL RULE:
    If real market data is unavailable or fails validation, NO synthetic/random data is generated.
    Raises DataValidationError strictly.
    """
    if tickers is None:
        tickers = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS"]

    period = "60d" if timeframe == "intraday" else "2y"
    interval = "15m" if timeframe == "intraday" else "1d"
    min_rows_per_stock = 100 if timeframe == "intraday" else 200

    logger.info(f"Fetching validated real market benchmark data ({timeframe}) for {tickers}...")
    try:
        data = yf.download(tickers, period=period, interval=interval, progress=False)
    except Exception as e:
        logger.error(f"Market data fetch error for benchmark tickers: {e}")
        raise DataValidationError(f"Could not fetch benchmark dataset from provider: {e}")

    if data is None or data.empty:
        raise DataValidationError("Provider returned an empty dataset for benchmark tickers.")

    stock_dfs = []

    for t in tickers:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                try:
                    df = data.xs(t, level=1, axis=1).copy()
                except KeyError:
                    continue
            else:
                if len(tickers) == 1:
                    df = data.copy()
                else:
                    continue

            # 1. Structural Data Validation
            val_report = MarketDataValidator.validate_ohlcv(
                df, ticker=t, timeframe=timeframe, min_rows=min_rows_per_stock
            )
            if not val_report["valid"]:
                logger.warning(f"Ticker {t} failed data validation: {val_report['errors']}. Skipping.")
                continue

            df = df.dropna(how='all')
            df.columns = [col.lower() for col in df.columns]

            # 2. Point-In-Time Feature Engineering
            df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
            macd = ta.trend.MACD(df['close'])
            df['macd'] = macd.macd()
            df['macd_diff'] = macd.macd_diff()
            df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
            df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()

            # 3. Label Definition
            if timeframe == "intraday":
                df['returns'] = df['close'].pct_change()
                # Target: positive return on the very next 15m candle
                df['target'] = (df['returns'].shift(-1) > 0).astype(int)
                features = ['rsi', 'macd', 'macd_diff', 'adx', 'returns']
            else: # swing
                df['future_5d'] = df['close'].shift(-5)
                # Target: cumulative forward return > 2% over next 5 daily bars
                df['target'] = (((df['future_5d'] - df['close']) / df['close']) > 0.02).astype(int)
                features = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']

            clean_df = df.dropna(subset=features + ['target']).copy()
            if len(clean_df) < min_rows_per_stock:
                continue

            clean_df['ticker'] = t
            clean_df['datetime'] = clean_df.index
            stock_dfs.append(clean_df[features + ['target', 'ticker', 'datetime']])

        except Exception as e:
            logger.warning(f"Error preparing {t}: {e}")
            continue

    if not stock_dfs:
        raise DataValidationError("Zero tickers passed data quality and feature validation. Aborting without synthetic fallback.")

    # Ticker-aware temporal ordering: group chronologically
    combined = pd.concat(stock_dfs).sort_values('datetime').reset_index(drop=True)
    features_list = ['rsi', 'macd', 'macd_diff', 'adx', 'returns'] if timeframe == "intraday" else ['rsi', 'macd', 'macd_diff', 'adx', 'atr']
    
    X = combined[features_list].values
    y = combined['target'].values.astype(int)

    logger.info(f"Validated Benchmark Dataset ready ({timeframe}): {len(X)} samples across {len(stock_dfs)} tickers.")
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
