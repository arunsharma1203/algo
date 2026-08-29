import logging
import time
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd

from app.analytics.foundation_models.base import ForecastResult, FoundationModelFeatures
from app.analytics.foundation_models.timesfm_adapter import TimesFMAdapter
from app.analytics.foundation_models.chronos_adapter import ChronosAdapter

logger = logging.getLogger(__name__)

class FoundationModelManager:
    """
    Centralized Foundation Model Manager & Agreement Engine.
    Coordinates TimesFM 2.5 and Chronos-2 forecasting pipelines with caching,
    concurrency safeguards, and Layer-2 Meta-Learner feature extraction.
    """

    def __init__(self):
        self.timesfm = TimesFMAdapter()
        self.chronos = ChronosAdapter()
        self._forecast_cache: Dict[str, Tuple[float, Any]] = {}
        self._cache_ttl = 300  # 5 minutes cache
        self._initialized = False

    def initialize_models(self) -> Dict[str, bool]:
        """Loads available weights for TimesFM and Chronos."""
        tfm_ok = self.timesfm.load_model()
        chronos_ok = self.chronos.load_model()
        self._initialized = True
        return {
            "timesfm_loaded": tfm_ok,
            "chronos_loaded": chronos_ok
        }

    def generate_foundation_signals(
        self,
        symbol: str,
        historical_df: pd.DataFrame,
        timeframe: str = "1d",
        horizon_bars: Optional[int] = None,
        as_of_time: Optional[datetime] = None
    ) -> Tuple[ForecastResult, ForecastResult, FoundationModelFeatures]:
        """
        Executes point-in-time forecasts from both foundation models and computes
        agreement and confluence features for the Layer-2 Meta-Learner.
        """
        if as_of_time is None:
            as_of_time = datetime.now()

        if horizon_bars is None:
            horizon_bars = 1 if timeframe == "15m" else 5

        as_of_iso = as_of_time.isoformat()
        cache_key_tfm = f"timesfm_{symbol}_{timeframe}_{as_of_iso}_{horizon_bars}"
        cache_key_chr = f"chronos_{symbol}_{timeframe}_{as_of_iso}_{horizon_bars}"

        now = time.time()

        # 1. TimesFM Forecast (with cache lookup)
        if cache_key_tfm in self._forecast_cache and (now - self._forecast_cache[cache_key_tfm][0]) < self._cache_ttl:
            timesfm_res = self._forecast_cache[cache_key_tfm][1]
        else:
            try:
                timesfm_res = self.timesfm.forecast(
                    symbol=symbol,
                    historical_df=historical_df,
                    horizon_bars=horizon_bars,
                    timeframe=timeframe,
                    as_of_time=as_of_time
                )
                self._forecast_cache[cache_key_tfm] = (now, timesfm_res)
            except Exception as e:
                logger.warning(f"TimesFM execution error for {symbol}: {e}")
                timesfm_res = ForecastResult(
                    model_name="timesfm_2.5",
                    model_version=self.timesfm.model_version,
                    symbol=symbol,
                    timeframe=timeframe,
                    as_of_time=as_of_iso,
                    horizon_bars=horizon_bars,
                    expected_return_pct=0.0,
                    status="error",
                    error_message=str(e)
                )

        # 2. Chronos Forecast (with cache lookup)
        if cache_key_chr in self._forecast_cache and (now - self._forecast_cache[cache_key_chr][0]) < self._cache_ttl:
            chronos_res = self._forecast_cache[cache_key_chr][1]
        else:
            try:
                chronos_res = self.chronos.forecast(
                    symbol=symbol,
                    historical_df=historical_df,
                    horizon_bars=horizon_bars,
                    timeframe=timeframe,
                    as_of_time=as_of_time
                )
                self._forecast_cache[cache_key_chr] = (now, chronos_res)
            except Exception as e:
                logger.warning(f"Chronos execution error for {symbol}: {e}")
                chronos_res = ForecastResult(
                    model_name="chronos_2",
                    model_version=self.chronos.model_version,
                    symbol=symbol,
                    timeframe=timeframe,
                    as_of_time=as_of_iso,
                    horizon_bars=horizon_bars,
                    expected_return_pct=0.0,
                    status="error",
                    error_message=str(e)
                )

        # 3. Extract Structured Features & Agreement Metrics
        features = self._calculate_foundation_features(timesfm_res, chronos_res)

        return timesfm_res, chronos_res, features

    def _calculate_foundation_features(
        self,
        tfm: ForecastResult,
        chr_res: ForecastResult
    ) -> FoundationModelFeatures:
        """
        Computes mathematical confluence and disagreement metrics across TimesFM & Chronos.
        """
        tfm_active = (tfm.status == "success")
        chr_active = (chr_res.status == "success")
        available_count = (1 if tfm_active else 0) + (1 if chr_active else 0)

        # TimesFM features
        tfm_ret = tfm.expected_return_pct if tfm_active else 0.0
        tfm_unc = tfm.uncertainty_score if tfm_active else 0.0
        tfm_dir = 1.0 if tfm.direction == "BULLISH" else (-1.0 if tfm.direction == "BEARISH" else 0.0)

        # Chronos features
        chr_ret = chr_res.expected_return_pct if chr_active else 0.0
        chr_med = chr_res.median_return_pct if (chr_active and chr_res.median_return_pct is not None) else 0.0
        chr_down = chr_res.downside_risk_pct if (chr_active and chr_res.downside_risk_pct is not None) else 0.0
        chr_up = chr_res.upside_potential_pct if (chr_active and chr_res.upside_potential_pct is not None) else 0.0
        chr_unc = chr_res.uncertainty_score if chr_active else 0.0
        chr_dir = 1.0 if chr_res.direction == "BULLISH" else (-1.0 if chr_res.direction == "BEARISH" else 0.0)

        # Confluence / Agreement Metrics
        if tfm_active and chr_active:
            spread = abs(tfm_ret - chr_ret)
            # Direction agreement: 1.0 (both bullish or both bearish), -1.0 (opposing), 0.0 (neutral)
            if tfm_dir != 0.0 and chr_dir != 0.0:
                dir_agreement = 1.0 if (tfm_dir == chr_dir) else -1.0
            else:
                dir_agreement = 0.0
                
            unc_diff = chr_unc - tfm_unc
            consensus_score = (tfm_ret + chr_ret) / 2.0 if dir_agreement >= 0 else 0.0
        elif tfm_active:
            spread = 0.0
            dir_agreement = 0.0
            unc_diff = 0.0
            consensus_score = tfm_ret
        elif chr_active:
            spread = 0.0
            dir_agreement = 0.0
            unc_diff = 0.0
            consensus_score = chr_ret
        else:
            spread = 0.0
            dir_agreement = 0.0
            unc_diff = 0.0
            consensus_score = 0.0

        return FoundationModelFeatures(
            timesfm_expected_return=tfm_ret,
            timesfm_uncertainty=tfm_unc,
            timesfm_direction_code=tfm_dir,
            timesfm_status=tfm.status,
            chronos_expected_return=chr_ret,
            chronos_median_return=chr_med,
            chronos_downside_risk=chr_down,
            chronos_upside_potential=chr_up,
            chronos_uncertainty=chr_unc,
            chronos_direction_code=chr_dir,
            chronos_status=chr_res.status,
            foundation_expected_return_spread=spread,
            foundation_direction_agreement=dir_agreement,
            foundation_uncertainty_diff=unc_diff,
            foundation_consensus_score=consensus_score,
            models_available_count=available_count
        )

    def get_status(self) -> Dict[str, Any]:
        return {
            "timesfm": self.timesfm.get_model_info(),
            "chronos": self.chronos.get_model_info(),
            "cache_entries": len(self._forecast_cache),
            "manager_initialized": self._initialized
        }

foundation_model_manager = FoundationModelManager()

