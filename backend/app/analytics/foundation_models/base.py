from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np

@dataclass
class ForecastResult:
    """
    Standardized result structure for Time-Series Foundation Models.
    Never fabricates fake probabilities when continuous returns or quantiles are produced.
    """
    model_name: str
    model_version: str
    symbol: str
    timeframe: str  # '15m' or '1d'
    as_of_time: str
    horizon_bars: int
    expected_return_pct: float
    median_return_pct: Optional[float] = None
    lower_quantile_return_pct: Optional[float] = None  # 10th percentile / downside
    upper_quantile_return_pct: Optional[float] = None  # 90th percentile / upside
    downside_risk_pct: Optional[float] = None
    upside_potential_pct: Optional[float] = None
    uncertainty_score: float = 0.0
    direction: str = "NEUTRAL"  # 'BULLISH' | 'BEARISH' | 'NEUTRAL'
    forecast_path: List[float] = field(default_factory=list)
    status: str = "success"  # 'success' | 'unavailable' | 'insufficient_data' | 'error'
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "as_of_time": self.as_of_time,
            "horizon_bars": self.horizon_bars,
            "expected_return_pct": round(float(self.expected_return_pct), 3) if self.expected_return_pct is not None else 0.0,
            "median_return_pct": round(float(self.median_return_pct), 3) if self.median_return_pct is not None else None,
            "lower_quantile_return_pct": round(float(self.lower_quantile_return_pct), 3) if self.lower_quantile_return_pct is not None else None,
            "upper_quantile_return_pct": round(float(self.upper_quantile_return_pct), 3) if self.upper_quantile_return_pct is not None else None,
            "downside_risk_pct": round(float(self.downside_risk_pct), 3) if self.downside_risk_pct is not None else None,
            "upside_potential_pct": round(float(self.upside_potential_pct), 3) if self.upside_potential_pct is not None else None,
            "uncertainty_score": round(float(self.uncertainty_score), 4),
            "direction": self.direction,
            "forecast_path": [round(float(p), 2) for p in self.forecast_path],
            "status": self.status,
            "error_message": self.error_message
        }

@dataclass
class FoundationModelFeatures:
    """
    Combined feature vector extracted from Foundation Models for Layer-2 Meta-Learner.
    """
    timesfm_expected_return: float = 0.0
    timesfm_uncertainty: float = 0.0
    timesfm_direction_code: float = 0.0  # 1.0 = Bullish, -1.0 = Bearish, 0.0 = Neutral
    timesfm_status: str = "unavailable"
    
    chronos_expected_return: float = 0.0
    chronos_median_return: float = 0.0
    chronos_downside_risk: float = 0.0
    chronos_upside_potential: float = 0.0
    chronos_uncertainty: float = 0.0
    chronos_direction_code: float = 0.0
    chronos_status: str = "unavailable"
    
    foundation_expected_return_spread: float = 0.0
    foundation_direction_agreement: float = 0.0  # 1.0 = Agree, 0.0 = Neutral/Single, -1.0 = Conflicting
    foundation_uncertainty_diff: float = 0.0
    foundation_consensus_score: float = 0.0
    models_available_count: int = 0

    def to_vector(self) -> List[float]:
        return [
            self.timesfm_expected_return,
            self.timesfm_uncertainty,
            self.timesfm_direction_code,
            self.chronos_expected_return,
            self.chronos_median_return,
            self.chronos_downside_risk,
            self.chronos_upside_potential,
            self.chronos_uncertainty,
            self.chronos_direction_code,
            self.foundation_expected_return_spread,
            self.foundation_direction_agreement,
            self.foundation_consensus_score
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timesfm_expected_return": round(self.timesfm_expected_return, 3),
            "timesfm_uncertainty": round(self.timesfm_uncertainty, 4),
            "timesfm_direction_code": self.timesfm_direction_code,
            "timesfm_status": self.timesfm_status,
            "chronos_expected_return": round(self.chronos_expected_return, 3),
            "chronos_median_return": round(self.chronos_median_return, 3),
            "chronos_downside_risk": round(self.chronos_downside_risk, 3),
            "chronos_upside_potential": round(self.chronos_upside_potential, 3),
            "chronos_uncertainty": round(self.chronos_uncertainty, 4),
            "chronos_direction_code": self.chronos_direction_code,
            "chronos_status": self.chronos_status,
            "foundation_expected_return_spread": round(self.foundation_expected_return_spread, 3),
            "foundation_direction_agreement": self.foundation_direction_agreement,
            "foundation_uncertainty_diff": round(self.foundation_uncertainty_diff, 4),
            "foundation_consensus_score": round(self.foundation_consensus_score, 3),
            "models_available_count": self.models_available_count
        }

class TimeSeriesFoundationModel(ABC):
    """
    Abstract Base Class for Time-Series Foundation Model Adapters.
    Enforces strict point-in-time guarantees and non-blocking failure tolerance.
    """

    @abstractmethod
    def load_model(self) -> bool:
        """Initializes and loads model weights onto target compute device."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Returns True if model weights are loaded and ready for inference."""
        pass

    @abstractmethod
    def forecast(
        self,
        symbol: str,
        historical_df: pd.DataFrame,
        horizon_bars: int,
        timeframe: str,
        as_of_time: Optional[datetime] = None
    ) -> ForecastResult:
        """
        Executes point-in-time time-series forecast on historical data.
        
        CRITICAL GUARD:
        Data with timestamp > as_of_time MUST be rejected immediately.
        """
        pass

    @abstractmethod
    def get_model_info(self) -> Dict[str, Any]:
        """Returns metadata about model architecture, version, device, and capabilities."""
        pass

