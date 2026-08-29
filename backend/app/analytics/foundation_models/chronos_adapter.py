import logging
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, Any, Optional, List

from app.analytics.foundation_models.base import TimeSeriesFoundationModel, ForecastResult
from app.data.validator import DataValidationError

logger = logging.getLogger(__name__)

class ChronosAdapter(TimeSeriesFoundationModel):
    """
    Adapter for Amazon Chronos-2 / Chronos-Bolt probabilistic forecasting model.
    Produces empirical quantiles (10%, 50%, 90%), upside/downside distribution spreads.
    Enforces strict point-in-time historical data isolation.
    """

    def __init__(self, model_version: str = "amazon/chronos-2"):
        self.model_version = model_version
        self.pipeline = None
        self._is_loaded = False
        self._device = "cpu"
        self._init_error = None

    def load_model(self) -> bool:
        """Attempts to load Chronos-2 from official repository."""
        if self._is_loaded and self.pipeline is not None:
            return True

        try:
            import torch
            if torch.cuda.is_available():
                self._device = "cuda"
            elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                self._device = "mps"
            else:
                self._device = "cpu"

            try:
                # Try Chronos2Pipeline first (Chronos-2)
                from chronos import Chronos2Pipeline
                self.pipeline = Chronos2Pipeline.from_pretrained(
                    self.model_version,
                    device_map=self._device,
                    torch_dtype=torch.bfloat16 if self._device == "cuda" else torch.float32
                )
                self._is_loaded = True
                logger.info(f"Chronos-2 ({self.model_version}) loaded successfully on {self._device}.")
                return True
            except (ImportError, Exception):
                try:
                    # Fallback to standard ChronosPipeline / ChronosBolt
                    from chronos import ChronosPipeline
                    self.pipeline = ChronosPipeline.from_pretrained(
                        "amazon/chronos-bolt-small",
                        device_map=self._device,
                        torch_dtype=torch.float32
                    )
                    self._is_loaded = True
                    logger.info(f"Chronos-Bolt loaded successfully on {self._device}.")
                    return True
                except Exception as e2:
                    self._init_error = f"Chronos loading: {e2}"
                    self._is_loaded = False
                    return False

        except Exception as e:
            self._init_error = str(e)
            self._is_loaded = False
            logger.warning(f"Chronos loading warning: {e}")
            return False

    def is_available(self) -> bool:
        return self._is_loaded and self.pipeline is not None

    def forecast(
        self,
        symbol: str,
        historical_df: pd.DataFrame,
        horizon_bars: int = 5,
        timeframe: str = "1d",
        as_of_time: Optional[datetime] = None
    ) -> ForecastResult:
        """
        Executes point-in-time Chronos probabilistic quantile forecast on historical OHLCV data.
        """
        if as_of_time is None:
            as_of_time = datetime.now()

        # 1. Point-in-time timestamp guard
        if historical_df is None or historical_df.empty:
            return ForecastResult(
                model_name="chronos_2",
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
                model_name="chronos_2",
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
                model_name="chronos_2",
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
        if self.is_available() and self.pipeline is not None:
            try:
                import torch
                context_tensor = torch.tensor(prices[-512:], dtype=torch.float32).unsqueeze(0)
                
                # Predict quantiles: 10th (downside), 50th (median), 90th (upside)
                quantiles, mean_forecast = self.pipeline.predict_quantiles(
                    context=context_tensor,
                    prediction_length=horizon_bars,
                    quantile_levels=[0.1, 0.5, 0.9]
                )
                
                q10 = float(quantiles[0, :, 0][-1])
                q50 = float(quantiles[0, :, 1][-1])
                q90 = float(quantiles[0, :, 2][-1])
                expected_price = float(mean_forecast[0][-1]) if mean_forecast is not None else q50

                expected_ret = ((expected_price - current_price) / current_price) * 100.0
                median_ret = ((q50 - current_price) / current_price) * 100.0
                lower_q_ret = ((q10 - current_price) / current_price) * 100.0
                upper_q_ret = ((q90 - current_price) / current_price) * 100.0
                
                downside_risk = abs(min(0.0, lower_q_ret))
                upside_potential = max(0.0, upper_q_ret)
                uncertainty = ((q90 - q10) / current_price) * 100.0

                direction = "BULLISH" if median_ret > 0.5 else ("BEARISH" if median_ret < -0.5 else "NEUTRAL")
                forecast_path = [float(p) for p in quantiles[0, :, 1]]

                return ForecastResult(
                    model_name="chronos_2",
                    model_version=self.model_version,
                    symbol=symbol,
                    timeframe=timeframe,
                    as_of_time=as_of_time.isoformat(),
                    horizon_bars=horizon_bars,
                    expected_return_pct=round(expected_ret, 3),
                    median_return_pct=round(median_ret, 3),
                    lower_quantile_return_pct=round(lower_q_ret, 3),
                    upper_quantile_return_pct=round(upper_q_ret, 3),
                    downside_risk_pct=round(downside_risk, 3),
                    upside_potential_pct=round(upside_potential, 3),
                    uncertainty_score=round(uncertainty, 4),
                    direction=direction,
                    forecast_path=forecast_path,
                    status="success"
                )

            except Exception as e:
                logger.warning(f"Chronos inference runtime note for {symbol}: {e}")
                return ForecastResult(
                    model_name="chronos_2",
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
            model_name="chronos_2",
            model_version=self.model_version,
            symbol=symbol,
            timeframe=timeframe,
            as_of_time=as_of_time.isoformat(),
            horizon_bars=horizon_bars,
            expected_return_pct=0.0,
            status="unavailable",
            error_message="Chronos model engine is currently offline/unloaded."
        )

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "model_name": "Chronos-2",
            "version": self.model_version,
            "provider": "Amazon Science / AutoGluon",
            "device": self._device,
            "is_loaded": self._is_loaded,
            "supported_timeframes": ["15m", "1d"],
            "max_context_length": 512,
            "output_type": "probabilistic_quantiles",
            "init_error": self._init_error
        }

