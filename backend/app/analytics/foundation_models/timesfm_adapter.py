import logging
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

from app.analytics.foundation_models.base import TimeSeriesFoundationModel, ForecastResult
from app.data.validator import DataValidationError

logger = logging.getLogger(__name__)

class TimesFMAdapter(TimeSeriesFoundationModel):
    """
    Adapter for Google TimesFM 2.5 (Time Series Foundation Model).
    Supports zero-shot multivariate/univariate time-series forecasting.
    Enforces strict point-in-time historical data isolation.
    """

    def __init__(self, model_version: str = "google/timesfm-2.5-200m-pytorch"):
        self.model_version = model_version
        self.model = None
        self._is_loaded = False
        self._device = "cpu"
        self._init_error = None

    def load_model(self) -> bool:
        """Loads genuine TimesFM 2.5 from Google checkpoint."""
        if self._is_loaded and self.model is not None:
            return True

        try:
            import torch
            if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                self._device = "mps"
            elif torch.cuda.is_available():
                self._device = "cuda"
            else:
                self._device = "cpu"

            try:
                import timesfm
                logger.info(f"Loading genuine TimesFM 2.5 checkpoint ({self.model_version})...")
                self.model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(self.model_version)
                config = timesfm.ForecastConfig(
                    max_context=512,
                    max_horizon=32,
                    normalize_inputs=True
                )
                self.model.compile(config)
                self._is_loaded = True
                self._checkpoint_info = {
                    "checkpoint": self.model_version,
                    "architecture": "TimesFM_2p5_200M_torch",
                    "parameters": "200M",
                    "device": self._device
                }
                logger.info(f"Genuine TimesFM 2.5 loaded and compiled successfully on {self._device}.")
                return True
            except ImportError as ie:
                self._init_error = f"timesfm package not installed: {ie}"
                self._is_loaded = False
                logger.warning(self._init_error)
                return False

        except Exception as e:
            self._init_error = f"TimesFM 2.5 loading failed: {e}"
            self._is_loaded = False
            logger.error(self._init_error)
            return False

    def is_available(self) -> bool:
        return self._is_loaded and self.model is not None

    def forecast(
        self,
        symbol: str,
        historical_df: pd.DataFrame,
        horizon_bars: int = 5,
        timeframe: str = "1d",
        as_of_time: Optional[datetime] = None
    ) -> ForecastResult:
        """
        Executes point-in-time TimesFM forecast on historical OHLCV data.
        """
        now_str = datetime.now().isoformat()
        if as_of_time is None:
            as_of_time = datetime.now()

        # 1. Point-in-time timestamp guard
        if historical_df is None or historical_df.empty:
            return ForecastResult(
                model_name="timesfm_2.5",
                model_version=self.model_version,
                symbol=symbol,
                timeframe=timeframe,
                as_of_time=as_of_time.isoformat(),
                horizon_bars=horizon_bars,
                expected_return_pct=0.0,
                status="insufficient_data",
                error_message="Historical dataframe is empty."
            )

        df = historical_df.copy()
        
        # Check for future timestamps: enforce strict point-in-time invariant
        ts_series = None
        for candidate in ['datetime', 'date', 'timestamp']:
            if candidate in df.columns:
                ts_series = pd.to_datetime(df[candidate])
                break
        if ts_series is None and isinstance(df.index, pd.DatetimeIndex):
            ts_series = pd.Series(df.index, index=df.index)

        if ts_series is not None:
            # Normalize to tz-naive
            if hasattr(ts_series.dt, 'tz') and ts_series.dt.tz is not None:
                ts_series = ts_series.dt.tz_localize(None)
            as_of_naive = as_of_time.replace(tzinfo=None) if hasattr(as_of_time, 'tzinfo') and as_of_time.tzinfo else as_of_time
            future_mask = ts_series > as_of_naive
            if future_mask.any():
                future_count = int(future_mask.sum())
                raise DataValidationError(
                    f"Point-in-time violation: DataFrame contains {future_count} bars after as_of_time ({as_of_naive})."
                )
            df = df.loc[future_mask == False]

        # 2. Data Sufficiency Check (NO synthetic data allowed)
        if len(df) < 30:
            return ForecastResult(
                model_name="timesfm_2.5",
                model_version=self.model_version,
                symbol=symbol,
                timeframe=timeframe,
                as_of_time=as_of_time.isoformat(),
                horizon_bars=horizon_bars,
                expected_return_pct=0.0,
                status="insufficient_data",
                error_message=f"Insufficient history: {len(df)} rows (minimum required: 30)."
            )

        # Extract close prices
        close_col = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else None)
        if not close_col:
            return ForecastResult(
                model_name="timesfm_2.5",
                model_version=self.model_version,
                symbol=symbol,
                timeframe=timeframe,
                as_of_time=as_of_time.isoformat(),
                horizon_bars=horizon_bars,
                expected_return_pct=0.0,
                status="error",
                error_message="Missing 'close' column in input data."
            )

        prices = df[close_col].dropna().values.astype(float)
        current_price = float(prices[-1])

        # 3. Model Inference if available
        if self.is_available() and self.model is not None:
            try:
                context_prices = prices[-512:] if len(prices) > 512 else prices
                
                # Official TimesFM 2.5 forecast call
                point_forecast, _ = self.model.forecast(
                    horizon=horizon_bars,
                    inputs=[context_prices]
                )
                
                forecast_path = [float(p) for p in point_forecast[0][:horizon_bars]]
                final_price = forecast_path[-1]
                expected_ret = ((final_price - current_price) / current_price) * 100.0
                
                # Calculate dispersion/uncertainty across forecast trajectory
                price_changes = np.diff(np.concatenate([[current_price], forecast_path]))
                uncertainty = float(np.std(price_changes) / current_price * 100.0) if current_price > 0 else 0.0
                
                direction = "BULLISH" if expected_ret > 0.5 else ("BEARISH" if expected_ret < -0.5 else "NEUTRAL")

                return ForecastResult(
                    model_name="timesfm_2.5",
                    model_version=self.model_version,
                    symbol=symbol,
                    timeframe=timeframe,
                    as_of_time=as_of_time.isoformat(),
                    horizon_bars=horizon_bars,
                    expected_return_pct=round(expected_ret, 3),
                    uncertainty_score=round(uncertainty, 4),
                    direction=direction,
                    forecast_path=forecast_path,
                    status="success"
                )

            except Exception as e:
                logger.warning(f"TimesFM 2.5 inference runtime error for {symbol}: {e}")
                return ForecastResult(
                    model_name="timesfm_2.5",
                    model_version=self.model_version,
                    symbol=symbol,
                    timeframe=timeframe,
                    as_of_time=as_of_time.isoformat(),
                    horizon_bars=horizon_bars,
                    expected_return_pct=0.0,
                    status="error",
                    error_message=str(e)
                )

        # 4. Fail-closed / Offline State (Non-blocking)
        return ForecastResult(
            model_name="timesfm_2.5",
            model_version=self.model_version,
            symbol=symbol,
            timeframe=timeframe,
            as_of_time=as_of_time.isoformat(),
            horizon_bars=horizon_bars,
            expected_return_pct=0.0,
            status="unavailable",
            error_message="TimesFM 2.5 model engine is currently offline/unloaded."
        )

    def forecast_batch(
        self,
        batch_inputs: List[Tuple[str, np.ndarray]],
        horizon_bars: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Executes fast vectorized batch inference for multiple series in a single forward pass.
        batch_inputs: List of (identifier, price_array)
        """
        if not self.is_available():
            raise RuntimeError("TimesFM 2.5 model is not loaded.")

        inputs_list = []
        current_prices = []
        identifiers = []

        for ident, p_arr in batch_inputs:
            p_clean = np.asarray(p_arr, dtype=np.float32)
            context = p_clean[-512:] if len(p_clean) > 512 else p_clean
            inputs_list.append(context)
            current_prices.append(float(context[-1]))
            identifiers.append(ident)

        point_forecasts, _ = self.model.forecast(
            horizon=horizon_bars,
            inputs=inputs_list
        )

        results = []
        for i, ident in enumerate(identifiers):
            f_path = [float(p) for p in point_forecasts[i][:horizon_bars]]
            curr = current_prices[i]
            exp_ret = ((f_path[-1] - curr) / curr) * 100.0 if curr > 0 else 0.0
            changes = np.diff(np.concatenate([[curr], f_path]))
            uncertainty = float(np.std(changes) / curr * 100.0) if curr > 0 else 0.0
            results.append({
                "identifier": ident,
                "current_price": curr,
                "forecast_path": f_path,
                "expected_return_pct": round(exp_ret, 4),
                "uncertainty_score": round(uncertainty, 4),
                "model_version": self.model_version
            })

        return results

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "model_name": "TimesFM 2.5",
            "version": self.model_version,
            "provider": "Google Research",
            "device": self._device,
            "is_loaded": self._is_loaded,
            "supported_timeframes": ["15m", "1d"],
            "max_context_length": 512,
            "output_type": "continuous_trajectory",
            "checkpoint_info": getattr(self, "_checkpoint_info", {}),
            "init_error": self._init_error
        }

