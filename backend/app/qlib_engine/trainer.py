"""
REAL QLIB TRAINING PIPELINE
===========================
Executes genuine quantitative model training using Microsoft Qlib's
`DatasetH` and model estimators (`LGBModel`, `DEnsembleModel`, `CatBoostModel`).

Strict Rules:
- Zero hardcoded predictions, win rates, profit factors, or scores.
- Strict chronological temporal splitting: TRAIN -> VALIDATION -> LOCKED OOS.
- Model selection and early stopping use VALIDATION only.
- Locked OOS evaluated strictly ONCE.
- Model artifacts cryptographically hashed and registered.
"""

import os
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from scipy.stats import spearmanr

# Configure MLflow filesystem before Qlib imports
os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"
os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"

import qlib
from qlib.data import D
from qlib.data.dataset import DatasetH
from qlib.contrib.data.handler import Alpha158, Alpha360
from qlib.contrib.model.gbdt import LGBModel

from app.qlib_engine.india_data_adapter import ensure_qlib_ready
from app.qlib_engine.temporal_split import TemporalSplitter
from app.qlib_engine.model_registry import QlibModelRegistry

logger = logging.getLogger(__name__)


class QlibTrainer:
    """
    Executes training and locked OOS evaluation using Microsoft Qlib.
    """

    @classmethod
    def instantiate_qlib_model(
        cls,
        model_family: str = "LGBModel",
        params: Optional[Dict[str, Any]] = None
    ):
        """Instantiates real Microsoft Qlib model class."""
        mf = model_family.upper()
        p = params or {}

        if mf in ("LGBMODEL", "LIGHTGBM", "LGB"):
            default_p = {
                "loss": "mse",
                "colsample_bytree": 0.85,
                "subsample": 0.85,
                "learning_rate": 0.05,
                "max_depth": 5,
                "num_leaves": 25,
                "n_estimators": 80,
                "verbose": -1
            }
            default_p.update(p)
            return LGBModel(**default_p)

        elif mf in ("DENSEMBLEMODEL", "DOUBLEENSEMBLE", "DENSEMBLE"):
            from qlib.contrib.model.double_ensemble import DEnsembleModel
            default_p = {
                "loss": "mse",
                "base_estimator": "gbdt",
                "num_models": 4,
                "sub_features": 0.70,
                "decay": 0.8,
                "max_depth": 4,
                "num_leaves": 15,
                "learning_rate": 0.05,
                "n_estimators": 50,
                "verbose": -1
            }
            default_p.update(p)
            return DEnsembleModel(**default_p)

        elif mf in ("CATBOOSTMODEL", "CATBOOST"):
            from qlib.contrib.model.catboost_model import CatBoostModel
            default_p = {
                "loss_function": "RMSE",
                "iterations": 80,
                "learning_rate": 0.05,
                "depth": 5,
                "verbose": False
            }
            default_p.update(p)
            return CatBoostModel(**default_p)

        else:
            raise ValueError(f"Unsupported Qlib model family: {model_family}. Use LGBModel, DEnsembleModel, or CatBoostModel.")

    @classmethod
    def compute_oos_metrics(
        cls,
        predictions: pd.Series,
        dataset: DatasetH,
        top_k: int = 5,
        holding_period: int = 5
    ) -> Dict[str, Any]:
        """
        Computes mathematically rigorous out-of-sample quantitative metrics:
        - Information Coefficient (IC) & Rank IC
        - Long-only simulated trades based on Top-K predicted scores
        - Win Rate, Profit Factor, Annualized Return, Max Drawdown
        """
        if predictions is None or predictions.empty:
            return {"ic": 0.0, "rank_ic": 0.0, "trades_count": 0, "win_rate": None, "profit_factor": None}

        # Get test labels from dataset
        test_df = dataset.prepare(segments="test", col_set=["label"])
        if test_df.empty:
            return {"ic": 0.0, "rank_ic": 0.0, "trades_count": 0, "win_rate": None, "profit_factor": None}

        label_col = test_df.columns[0]
        aligned = pd.DataFrame({"pred": predictions, "label": test_df[label_col]}).dropna()

        if len(aligned) < 5:
            return {"ic": 0.0, "rank_ic": 0.0, "trades_count": 0, "win_rate": None, "profit_factor": None}

        # IC & Rank IC
        ic = float(np.corrcoef(aligned["pred"], aligned["label"])[0, 1]) if len(aligned) > 2 else 0.0
        if np.isnan(ic):
            ic = 0.0
        rank_ic, _ = spearmanr(aligned["pred"], aligned["label"])
        if np.isnan(rank_ic):
            rank_ic = 0.0

        # Simulate top-k long trades across test dates
        dates = aligned.index.get_level_values(0).unique().sort_values()
        trade_pnls = []

        for d in dates:
            day_slice = aligned.loc[d]
            if len(day_slice) == 0:
                continue
            # Select top-k highest predicted scores
            top_stocks = day_slice.sort_values(by="pred", ascending=False).head(top_k)
            # The label represents the forward return
            for _, row in top_stocks.iterrows():
                # Convert normalized return back to percentage
                trade_pnls.append(float(row["label"]) * 100.0)

        completed_trades = len(trade_pnls)
        if completed_trades == 0:
            return {
                "ic": round(ic, 4),
                "rank_ic": round(float(rank_ic), 4),
                "trades_count": 0,
                "win_rate": None,
                "profit_factor": None,
                "sharpe": None,
                "max_drawdown_pct": None
            }

        wins = [p for p in trade_pnls if p > 0]
        losses = [p for p in trade_pnls if p < 0]
        win_rate = round((len(wins) / completed_trades) * 100.0, 2)

        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 0.0

        if gross_loss > 0:
            profit_factor = round(gross_profit / gross_loss, 2)
        elif gross_profit > 0:
            profit_factor = 99.0
        else:
            profit_factor = 1.0

        # Sharpe & Drawdown
        pnl_arr = np.array(trade_pnls)
        mean_pnl = float(np.mean(pnl_arr))
        std_pnl = float(np.std(pnl_arr)) if len(pnl_arr) > 1 else 1.0
        sharpe = round((mean_pnl / (std_pnl + 1e-8)) * np.sqrt(252 / holding_period), 2)

        # Max drawdown
        cum_ret = np.cumsum(pnl_arr)
        running_max = np.maximum.accumulate(cum_ret)
        dd = running_max - cum_ret
        max_dd = round(float(np.max(dd)) if len(dd) > 0 else 0.0, 2)

        return {
            "ic": round(ic, 4),
            "rank_ic": round(float(rank_ic), 4),
            "trades_count": completed_trades,
            "wins_count": len(wins),
            "losses_count": len(losses),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "sharpe": round(float(sharpe), 2),
            "max_drawdown_pct": max_dd,
            "mean_trade_pnl_pct": round(mean_pnl, 2)
        }

    @classmethod
    def train_model(
        cls,
        strategy: str = "SWING",
        instruments: Optional[List[str]] = None,
        feature_family: str = "Alpha158",
        model_family: str = "LGBModel",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        freq: str = "day"
    ) -> Dict[str, Any]:
        """
        Executes end-to-end real Qlib model training:
        1. Ensures Qlib is initialized with Indian market data.
        2. Computes chronological temporal splits (70% train, 15% val, 15% locked OOS).
        3. Builds real Qlib DataHandler (Alpha158 or Alpha360).
        4. Builds real Qlib DatasetH with non-overlapping segments.
        5. Fits official Qlib model on TRAIN with early stopping on VALIDATION.
        6. Predicts on LOCKED OOS and calculates real performance metrics.
        7. Saves and hashes the model artifact in QlibModelRegistry.
        """
        adapter = ensure_qlib_ready()
        strat = strategy.upper().strip()

        # Resolve instruments
        if not instruments:
            from app.analytics.universe_config import resolve_universe_tickers
            raw_universe = resolve_universe_tickers("LIVE_52")
            clean_insts = [adapter.normalize_symbol(t)[0] for t in raw_universe]
            # Use top liquid symbols for fast, robust training
            clean_insts = clean_insts[:20]
        else:
            clean_insts = [adapter.normalize_symbol(t)[0] for t in instruments]

        # Get calendar dates
        cal = D.calendar(freq=freq)
        cal_strs = [pd.to_datetime(d).strftime("%Y-%m-%d" if freq == "day" else "%Y-%m-%d %H:%M:%S") for d in cal]

        if start_date and end_date:
            cal_strs = [d for d in cal_strs if start_date <= d <= end_date]

        # Temporal split (70% train, 15% val, 15% OOS)
        segments = TemporalSplitter.partition_dates(cal_strs, train_ratio=0.70, val_ratio=0.15)
        train_start, train_end = segments["train"]
        val_start, val_end = segments["valid"]
        oos_start, oos_end = segments["test"]

        logger.info(
            f"[QlibTrainer] Starting {strat} training with {len(clean_insts)} instruments. "
            f"TRAIN: [{train_start} to {train_end}] | "
            f"VAL: [{val_start} to {val_end}] | "
            f"OOS: [{oos_start} to {oos_end}]"
        )

        # Build Qlib DataHandler
        fam = feature_family.upper()
        if fam == "ALPHA158":
            dh = Alpha158(
                instruments=clean_insts,
                start_time=train_start,
                end_time=oos_end,
                freq=freq,
                fit_start_time=train_start,
                fit_end_time=train_end
            )
        elif fam == "ALPHA360":
            dh = Alpha360(
                instruments=clean_insts,
                start_time=train_start,
                end_time=oos_end,
                freq=freq,
                fit_start_time=train_start,
                fit_end_time=train_end
            )
        else:
            raise ValueError(f"Unknown feature family: {feature_family}")

        # Build Qlib DatasetH
        dataset = DatasetH(handler=dh, segments=segments)

        # Instantiate real Qlib model
        model = cls.instantiate_qlib_model(model_family)

        # Train model
        logger.info(f"[QlibTrainer] Fitting {type(model).__name__} on Qlib DatasetH...")
        model.fit(dataset)

        # Predict on locked OOS
        logger.info(f"[QlibTrainer] Running locked OOS forward evaluation...")
        predictions = model.predict(dataset, segment="test")

        # Compute real OOS metrics
        oos_metrics = cls.compute_oos_metrics(predictions, dataset)
        logger.info(
            f"[QlibTrainer] OOS Evaluation complete: IC={oos_metrics.get('ic')}, "
            f"Sharpe={oos_metrics.get('sharpe')}, Trades={oos_metrics.get('trades_count')}, "
            f"WinRate={oos_metrics.get('win_rate')}%"
        )

        # Save to QlibModelRegistry
        manifest_meta = {
            "model_class": type(model).__name__,
            "feature_handler": feature_family,
            "feature_version": f"qlib_{feature_family.lower()}_v1",
            "universe": "LIVE_52",
            "data_hash": getattr(adapter, "data_hash", "authoritative_nse_daily"),
            "train_start": train_start,
            "train_end": train_end,
            "valid_start": val_start,
            "valid_end": val_end,
            "oos_start": oos_start,
            "oos_end": oos_end,
            "metrics": oos_metrics
        }

        saved_manifest = QlibModelRegistry.save_model(strat, model, manifest_meta)

        return {
            "status": "TRAINING_COMPLETE",
            "strategy": strat,
            "model_id": saved_manifest["model_id"],
            "artifact_sha256": saved_manifest["artifact_sha256"],
            "oos_metrics": oos_metrics,
            "temporal_splits": segments,
            "instruments_count": len(clean_insts),
            "manifest": saved_manifest
        }
