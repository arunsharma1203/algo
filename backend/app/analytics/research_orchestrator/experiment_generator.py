"""
RESEARCH EXPERIMENT GENERATOR (REPAIR V1)
=========================================
Generates structured, staged hypothesis variations across feature families,
target horizons, model families, and hysteresis portfolio constructions.
Uses immutable canonical configuration identity (SHA-256) and persistent
state memory to guarantee that no duplicate configuration is ever regenerated.
Correctly reports search-space exhaustion when the finite hypothesis space is covered.
"""

import hashlib
import json
import random
from typing import Dict, Any, List, Optional, Set, Tuple

from app.analytics.research_orchestrator.research_mission import ResearchMission
from app.analytics.research_orchestrator.research_memory import ResearchMemory

class ExperimentGenerator:
    """
    Generates controlled research experiments aligned with mission objectives.
    Guarantees deterministic, reproducible, collision-free experiment identities.
    """

    @classmethod
    def _normalize_item(cls, val: Any) -> Any:
        """Recursively normalizes any nested item for deterministic hashing."""
        if isinstance(val, dict):
            return {str(k): cls._normalize_item(v) for k, v in sorted(val.items())}
        elif isinstance(val, (list, tuple)):
            return [cls._normalize_item(x) for x in val]
        elif isinstance(val, float):
            return round(val, 6)
        elif isinstance(val, (int, bool)) or val is None:
            return val
        else:
            return str(val).strip()

    @classmethod
    def canonicalize_config(cls, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalizes and canonicalizes research configuration dictionary.
        Enforces stable key ordering, standard types, and normalized values.
        Excludes execution metadata (mission_id, experiment_id, status, timestamps).
        """
        raw_constraints = config.get("secondary_constraints") or config.get("risk_rules") or {}
        norm_constraints = cls._normalize_item(raw_constraints)

        raw_boundaries = config.get("data_boundaries") or {}
        norm_boundaries = cls._normalize_item(raw_boundaries)

        raw_cost_tiers = config.get("cost_tiers") or [0.001, 0.0015, 0.002, 0.003]
        norm_cost_tiers = sorted([round(float(x), 6) for x in raw_cost_tiers])

        horizon_val = config.get("horizon_days") if config.get("horizon_days") is not None else config.get("horizon", 10)
        try:
            horizon = int(horizon_val)
        except (ValueError, TypeError):
            horizon = 10

        raw_model = str(config.get("model_family", "LightGBM")).strip()
        raw_feature = str(config.get("feature_family", "Alpha158")).strip()
        raw_universe = str(config.get("universe", "LIVE_52")).strip().upper()
        raw_strategy = str(config.get("strategy_type", "SWING")).strip().upper()
        raw_timeframe = str(config.get("timeframe", "1d")).strip().lower()
        raw_portfolio = str(config.get("portfolio_family", "HYSTERESIS_TOP5_15")).strip()
        raw_primary = str(config.get("primary_objective_metric", "SHARPE")).strip().upper()

        hyperparams = config.get("hyperparameters") or config.get("model_params") or {}
        norm_hyperparams = cls._normalize_item(hyperparams)

        sizing = config.get("position_sizing") or config.get("sizing") or "EQUAL_WEIGHT"
        norm_sizing = cls._normalize_item(sizing)

        dataset_hash = str(config.get("dataset_hash", "")).strip()

        standard_keys = {
            "mission_id", "experiment_id", "parent_id", "status", "priority",
            "created_at", "updated_at", "hypothesis", "retry_count", "config_hash",
            "secondary_constraints", "risk_rules", "data_boundaries", "cost_tiers", "horizon_days",
            "horizon", "model_family", "feature_family", "feature_version", "universe",
            "strategy_type", "timeframe", "portfolio_family", "primary_objective_metric",
            "entry_top_k", "exit_top_k", "holding_period", "rebalance_frequency",
            "seed", "code_version", "slippage_bps", "hyperparameters", "model_params",
            "position_sizing", "sizing", "dataset_hash"
        }

        # Collect any additional experiment-specific parameters
        extra_params = {}
        for k, v in sorted(config.items()):
            if k not in standard_keys:
                extra_params[str(k)] = cls._normalize_item(v)

        canonical = {
            "strategy_type": raw_strategy,
            "universe": raw_universe,
            "timeframe": raw_timeframe,
            "model_family": raw_model,
            "feature_family": raw_feature,
            "feature_version": str(config.get("feature_version", "v1.0")).strip(),
            "horizon_days": horizon,
            "hyperparameters": norm_hyperparams,
            "dataset_hash": dataset_hash,
            "portfolio_family": raw_portfolio,
            "entry_top_k": int(config.get("entry_top_k", 5)),
            "exit_top_k": int(config.get("exit_top_k", 15)),
            "holding_period": int(config.get("holding_period", horizon)),
            "rebalance_frequency": int(config.get("rebalance_frequency", 1)),
            "position_sizing": norm_sizing,
            "primary_objective_metric": raw_primary,
            "seed": int(config.get("seed", 42)),
            "code_version": str(config.get("code_version", "v1.0-autopilot")).strip(),
            "cost_tiers": norm_cost_tiers,
            "slippage_bps": round(float(config.get("slippage_bps", 5.0)), 2),
            "secondary_constraints": norm_constraints,
            "data_boundaries": norm_boundaries
        }
        if extra_params:
            canonical["extra_params"] = extra_params

        return canonical

    @classmethod
    def compute_config_hash(cls, config: Dict[str, Any]) -> str:
        """
        Computes authoritative deterministic SHA-256 hash of canonical experiment configuration.
        Identical research parameters always yield identical hashes.
        Different research parameters always yield different hashes.
        """
        canonical = cls.canonicalize_config(config)
        raw = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def get_search_space_grid(cls, mission: ResearchMission) -> List[Tuple[str, str, int, str, int, int]]:
        """
        Builds the complete discrete parameter combinations for a mission.
        Returns: list of (model, feature, horizon, portfolio, entry_k, exit_k).
        """
        feature_pool = mission.feature_families or ["Alpha158"]
        model_pool = mission.model_families or ["LightGBM"]
        horizon_pool = mission.target_horizons or [10]
        portfolio_pool = mission.portfolio_families or ["HYSTERESIS_TOP5_15"]

        grid = []
        for feat in feature_pool:
            for model in model_pool:
                for horiz in horizon_pool:
                    for port in portfolio_pool:
                        if "HYSTERESIS_TOP5_15" in port:
                            e_k, x_k = 5, 15
                        elif "HYSTERESIS_TOP10_20" in port:
                            e_k, x_k = 10, 20
                        elif "HYSTERESIS_TOP10_30" in port:
                            e_k, x_k = 10, 30
                        elif "TOP5" in port:
                            e_k, x_k = 5, 5
                        elif "TOP10" in port:
                            e_k, x_k = 10, 10
                        elif "TOP20" in port:
                            e_k, x_k = 20, 20
                        else:
                            e_k, x_k = 10, 15
                        grid.append((model, feat, int(horiz), port, e_k, x_k))
        return grid

    @classmethod
    def get_total_search_space_size(cls, mission: ResearchMission) -> int:
        """Returns total distinct parameter combinations available for the mission."""
        return len(cls.get_search_space_grid(mission))

    @classmethod
    def generate_next_experiments(
        cls,
        mission: ResearchMission,
        batch_size: int = 5,
        parent_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Generates a batch of truly unique candidate experiments within the mission's parameter space.
        Enumerates the discrete search space, deterministically shuffles by research_seed,
        checks persistent research memory to skip already queued/completed configurations,
        and returns an empty list if the parameter search space is exhausted.
        """
        grid = cls.get_search_space_grid(mission)
        if not grid:
            return []

        # Deterministic shuffle seeded by research_seed for consistent exploration sequence
        shuffled_grid = list(grid)
        rng = random.Random(mission.research_seed)
        rng.shuffle(shuffled_grid)

        # Query authoritative persistent memory for all existing config hashes
        existing_hashes = ResearchMemory.get_all_config_hashes(mission.mission_id)

        experiments: List[Dict[str, Any]] = []

        for chosen_model, chosen_feature, chosen_horizon, chosen_portfolio, entry_k, exit_k in shuffled_grid:
            if len(experiments) >= batch_size:
                break

            config = {
                "mission_id": mission.mission_id,
                "strategy_type": mission.strategy_type,
                "universe": mission.universe,
                "feature_family": chosen_feature,
                "feature_version": "v1.0",
                "model_family": chosen_model,
                "horizon_days": chosen_horizon,
                "portfolio_family": chosen_portfolio,
                "entry_top_k": entry_k,
                "exit_top_k": exit_k,
                "holding_period": chosen_horizon,
                "rebalance_frequency": 1,
                "primary_objective_metric": mission.primary_objective_metric,
                "secondary_constraints": mission.secondary_constraints,
                "cost_tiers": mission.cost_tiers,
                "slippage_bps": 5.0,
                "timeframe": mission.timeframe,
                "data_boundaries": mission.data_boundaries,
                "seed": mission.research_seed,
                "code_version": "v1.0-autopilot"
            }

            cfg_hash = cls.compute_config_hash(config)
            if cfg_hash in existing_hashes:
                continue

            existing_hashes.add(cfg_hash)
            exp_id = f"exp_{mission.mission_id}_{cfg_hash[:8]}"

            hypothesis = (
                f"Evaluate {chosen_model} on {chosen_feature} with {chosen_horizon}D target "
                f"under {chosen_portfolio} portfolio construction to optimize {mission.primary_objective_metric} "
                f"while enforcing drawdown <= {mission.secondary_constraints.get('max_drawdown_pct', 20.0)}%."
            )

            experiments.append({
                "experiment_id": exp_id,
                "mission_id": mission.mission_id,
                "parent_id": parent_id,
                "priority": "HIGH" if "DoubleEnsemble" in chosen_model or "HYSTERESIS" in chosen_portfolio else "MEDIUM",
                "status": "QUEUED",
                "hypothesis": hypothesis,
                "config": config,
                "config_hash": cfg_hash,
                "changes": {
                    "model_family": chosen_model,
                    "feature_family": chosen_feature,
                    "horizon_days": chosen_horizon,
                    "portfolio_family": chosen_portfolio
                }
            })

        return experiments

