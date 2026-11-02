"""
AUTONOMOUS RESEARCH MISSION SPECIFICATION
=========================================
Defines the immutable objective, parameter bounds, budgets, primary metrics,
and secondary constraints for an autonomous quantitative research mission.
Once a mission enters SEARCHING status, its core configuration and governance
rules are frozen to prevent adaptive goalpost-shifting or data-snooping bias.
"""

import json
import hashlib
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Any, Optional

CANONICAL_DATA_CUTOFF = "2026-09-04"

DEFAULT_SECONDARY_CONSTRAINTS: Dict[str, Any] = {
    "max_drawdown_pct": 20.0,
    "max_turnover_pct": 1200.0,
    "min_trades": 30,
    "min_win_rate_pct": 50.0,
    "survives_friction_bps": 30,
    "min_profit_factor": 1.0,
    "min_expectancy": 0.0
}

DEFAULT_BUDGET: Dict[str, Any] = {
    "max_experiments": 100,
    "max_runtime_seconds": 86400,
    "max_concurrent": 4,
    "max_repeat_variants": 10,
    "max_per_feature": 50,
    "max_per_model": 50,
    "timeout_per_experiment_seconds": 120
}

DEFAULT_DATA_BOUNDARIES: Dict[str, str] = {
    "train_start": "2016-08-29",
    "train_end": "2021-09-04",
    "val_start": "2021-09-05",
    "val_end": "2023-09-04",
    "oos_start": "2023-09-05",
    "oos_end": CANONICAL_DATA_CUTOFF
}

@dataclass
class ResearchMission:
    """
    Authoritative container for a quantitative research mission.
    Enforces immutability after exploration starts.
    """
    mission_id: str
    objective: str
    primary_objective_metric: str = "SHARPE"  # "SHARPE", "CAGR", "TURNOVER_EFFICIENCY", "EXPECTANCY"
    secondary_constraints: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SECONDARY_CONSTRAINTS))
    strategy_type: str = "SWING"  # "SWING" | "INTRADAY"
    universe: str = "LIVE_52"  # Initial discovery universe
    timeframe: str = "1d"  # "1d" | "15m"
    data_boundaries: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DATA_BOUNDARIES))
    target_horizons: List[int] = field(default_factory=lambda: [5, 10, 15, 20])
    feature_families: List[str] = field(default_factory=lambda: [
        "Alpha158", "Alpha360", "Technical", "Volume", "Volatility", "RelativeStrength"
    ])
    model_families: List[str] = field(default_factory=lambda: [
        "Ridge", "LightGBM", "XGBoost", "CatBoost", "DoubleEnsemble"
    ])
    portfolio_families: List[str] = field(default_factory=lambda: [
        "TOP5", "TOP10", "TOP20", "HYSTERESIS_TOP5_15", "HYSTERESIS_TOP10_20", "HYSTERESIS_TOP10_30"
    ])
    cost_tiers: List[float] = field(default_factory=lambda: [0.0010, 0.0015, 0.0020, 0.0030])
    budget: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_BUDGET))
    benchmark: str = "BENCHMARK_5"
    research_seed: int = 42
    governance_version: str = "gov_v1_immutable"
    status: str = "INITIALIZED"  # "INITIALIZED", "SEARCHING", "PAUSED", "STOPPED", "COMPLETED"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    config_hash: str = ""

    def __post_init__(self):
        if not self.budget:
            self.budget = dict(DEFAULT_BUDGET)
        if not self.secondary_constraints:
            self.secondary_constraints = dict(DEFAULT_SECONDARY_CONSTRAINTS)
        if not self.data_boundaries:
            self.data_boundaries = dict(DEFAULT_DATA_BOUNDARIES)
        if not self.config_hash:
            self.config_hash = self.compute_hash()

    def compute_hash(self) -> str:
        """Computes deterministic SHA-256 fingerprint of mission configuration."""
        payload = {
            "objective": self.objective,
            "primary_objective_metric": self.primary_objective_metric,
            "secondary_constraints": self.secondary_constraints,
            "strategy_type": self.strategy_type,
            "universe": self.universe,
            "timeframe": self.timeframe,
            "data_boundaries": self.data_boundaries,
            "target_horizons": sorted(self.target_horizons),
            "feature_families": sorted(self.feature_families),
            "model_families": sorted(self.model_families),
            "portfolio_families": sorted(self.portfolio_families),
            "cost_tiers": sorted(self.cost_tiers),
            "budget": self.budget,
            "benchmark": self.benchmark,
            "research_seed": self.research_seed,
            "governance_version": self.governance_version
        }
        raw = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def validate_bounds(self) -> None:
        """Enforces sanity constraints on mission setup."""
        valid_metrics = {"SHARPE", "CAGR", "TURNOVER_EFFICIENCY", "EXPECTANCY", "SORTINO"}
        if self.primary_objective_metric not in valid_metrics:
            raise ValueError(f"Invalid primary_objective_metric: {self.primary_objective_metric}. Allowed: {valid_metrics}")
        valid_universes = {
            "LIVE_52", "BENCHMARK_5", "NIFTY_50", "RESEARCH_100", "NIFTY_500",
            "AUTOPILOT_PRIORITY_20", "ALL_COLLECTED", "ALL_117", "CUSTOM",
            "WATCHLIST", "USER_WATCHLIST", "BANK_NIFTY", "NIFTY_BANK", "NIFTY_IT", "IT"
        }
        if self.universe not in valid_universes:
            raise ValueError(f"Invalid universe: {self.universe}. Allowed: {sorted(list(valid_universes))}")
        if self.budget.get("max_concurrent", 1) > 4:
            raise ValueError("max_concurrent cannot exceed 4 to safeguard local system stability.")
        if self.budget.get("max_experiments", 0) <= 0:
            raise ValueError("max_experiments must be positive.")
        if self.data_boundaries.get("oos_end", "") > CANONICAL_DATA_CUTOFF:
            raise ValueError(f"OOS cannot extend beyond canonical database cutoff ({CANONICAL_DATA_CUTOFF}).")

    def to_dict(self) -> Dict[str, Any]:
        """Serializes mission to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResearchMission":
        """Reconstructs mission from dictionary."""
        clean = dict(data)
        # Ensure nested dict defaults if missing
        if "secondary_constraints" not in clean or not clean["secondary_constraints"]:
            clean["secondary_constraints"] = dict(DEFAULT_SECONDARY_CONSTRAINTS)
        if "budget" not in clean or not clean["budget"]:
            clean["budget"] = dict(DEFAULT_BUDGET)
        if "data_boundaries" not in clean or not clean["data_boundaries"]:
            clean["data_boundaries"] = dict(DEFAULT_DATA_BOUNDARIES)
        return cls(**clean)
