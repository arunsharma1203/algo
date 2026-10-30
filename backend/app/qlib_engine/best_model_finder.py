"""
QLIB BEST MODEL FINDER & AUTOMATED RESEARCH ORCHESTRATOR
========================================================
Executes the one-click primary research workflow for the Qlib Control Room.

Strict Scientific Standards & User Corrections:
1. Tests a curated, high-value set of genuine configurations:
   - Formulation A: Cross-Sectional Ranking (CSZScoreNorm), Alpha158, 5D Horizon, LGBModel
   - Formulation B: Cross-Sectional Ranking (CSZScoreNorm), Alpha158, 10D Horizon, LGBModel
   - Formulation C: Directional Return (FORWARD_RETURN), Alpha158, 5D Horizon, LGBModel
2. Does NOT assume CSZScoreNorm will reduce Max DD below 20% or produce positive Sharpe.
   It measures and reports the real, unvarnished outcome honestly.
3. Explicitly records friction/slippage (friction_bps) as a configurable parameter.
4. "BEST MODEL" strictly means: Highest-quality candidate that passes ALL 12 promotion gates.
   If none pass all gates, honestly reports: "NO QUALIFIED CHAMPION FOUND".
5. NEVER automatically promotes the winner. Promotion requires explicit manual approval.
6. Job state and progress persist server-side and stream live via SSE.
"""

import os
import json
import time
import logging
import threading
from datetime import datetime
from typing import Dict, Any, List, Optional

from app.qlib_engine.trainer import QlibTrainer
from app.qlib_engine.promotion_policy import QlibPromotionPolicy
from app.qlib_engine.model_registry import QlibModelRegistry
from app.qlib_engine.training_telemetry import training_telemetry
from app.qlib_engine.training_job_tracker import TrainingJobTracker
from app.analytics.master_logger import MasterLogger

logger = logging.getLogger(__name__)

# Module-level tracking for active research mission
_ACTIVE_MISSION: Optional[Dict[str, Any]] = None
_MISSION_LOCK = threading.Lock()


class BestModelFinder:
    """
    Orchestrates the single-action [FIND BEST QLIB MODEL] workflow.
    """

    @classmethod
    def get_curated_configs(
        cls,
        universe: str = "NIFTY_500_ESTABLISHED",
        friction_bps: float = 25.0,
        strategy: str = "SWING"
    ) -> List[Dict[str, Any]]:
        """
        Defines the small, high-value set of genuine configurations to evaluate.
        """
        return [
            {
                "config_id": "CONFIG_A_CS_RANK_5D",
                "display_name": "Alpha158 · Cross-Sectional Rank · 5D · Baseline (Top 5, EW, 100% Exp)",
                "strategy": strategy,
                "universe": universe,
                "feature_family": "Alpha158",
                "model_family": "LGBModel",
                "horizon_days": 5,
                "target_definition": "CROSS_SECTIONAL_RANK",
                "top_k": 5,
                "sizing_method": "EQUAL_WEIGHT",
                "max_exposure_pct": 100.0,
                "friction_bps": float(friction_bps)
            },
            {
                "config_id": "CONFIG_A_TOP15_EXP80",
                "display_name": "Alpha158 · Cross-Sectional Rank · 5D · Risk-Managed (Top 15, EW, 80% Exp)",
                "strategy": strategy,
                "universe": universe,
                "feature_family": "Alpha158",
                "model_family": "LGBModel",
                "horizon_days": 5,
                "target_definition": "CROSS_SECTIONAL_RANK",
                "top_k": 15,
                "sizing_method": "EQUAL_WEIGHT",
                "max_exposure_pct": 80.0,
                "friction_bps": float(friction_bps)
            },
            {
                "config_id": "CONFIG_A_TOP10_INVOL_EXP80",
                "display_name": "Alpha158 · Cross-Sectional Rank · 5D · Volatility-Managed (Top 10, Inv-Vol, 80% Exp)",
                "strategy": strategy,
                "universe": universe,
                "feature_family": "Alpha158",
                "model_family": "LGBModel",
                "horizon_days": 5,
                "target_definition": "CROSS_SECTIONAL_RANK",
                "top_k": 10,
                "sizing_method": "INVERSE_VOLATILITY",
                "max_exposure_pct": 80.0,
                "friction_bps": float(friction_bps)
            },
            {
                "config_id": "CONFIG_B_TOP10_EXP80",
                "display_name": "Alpha158 · Cross-Sectional Rank · 10D · Risk-Managed (Top 10, EW, 80% Exp)",
                "strategy": strategy,
                "universe": universe,
                "feature_family": "Alpha158",
                "model_family": "LGBModel",
                "horizon_days": 10,
                "target_definition": "CROSS_SECTIONAL_RANK",
                "top_k": 10,
                "sizing_method": "EQUAL_WEIGHT",
                "max_exposure_pct": 80.0,
                "friction_bps": float(friction_bps)
            },
            {
                "config_id": "CONFIG_C_ALPHA360_5D",
                "display_name": "Alpha360 · Cross-Sectional Rank · 5D · Baseline (Top 5, EW, 100% Exp)",
                "strategy": strategy,
                "universe": universe,
                "feature_family": "Alpha360",
                "model_family": "LGBModel",
                "horizon_days": 5,
                "target_definition": "CROSS_SECTIONAL_RANK",
                "top_k": 5,
                "sizing_method": "EQUAL_WEIGHT",
                "max_exposure_pct": 100.0,
                "friction_bps": float(friction_bps)
            }
        ]

    @classmethod
    def run_research_mission(
        cls,
        mission_id: str,
        universe: str = "NIFTY_500_ESTABLISHED",
        friction_bps: float = 25.0,
        strategy: str = "SWING"
    ) -> Dict[str, Any]:
        """
        Executes the full research pipeline across the curated configurations.
        Evaluates each candidate through all 11 stages and audits against 12 promotion gates.
        """
        global _ACTIVE_MISSION
        strat = strategy.upper().strip()
        configs = cls.get_curated_configs(universe=universe, friction_bps=friction_bps, strategy=strat)
        champion_manifest = QlibModelRegistry.get_active_manifest(strat)

        candidates_evaluated: List[Dict[str, Any]] = []
        best_candidate: Optional[Dict[str, Any]] = None
        qualified_candidates: List[Dict[str, Any]] = []

        total_configs = len(configs)
        mission_start_time = time.time()

        logger.info(f"[BestModelFinder] Starting mission {mission_id} testing {total_configs} curated configurations...")

        MasterLogger.log_event(
            category="QLIB_RESEARCH",
            event_type="QLIB_RESEARCH_MISSION_STARTED",
            message=f"Starting Qlib research mission {mission_id} testing {total_configs} curated configurations",
            universe=universe,
            details={
                "mission_id": mission_id,
                "strategy": strat,
                "universe": universe,
                "configs_count": total_configs,
                "friction_bps": float(friction_bps)
            }
        )

        for idx, cfg in enumerate(configs):
            config_label = cfg["config_id"]
            logger.info(f"[BestModelFinder] [{idx + 1}/{total_configs}] Running {config_label}...")

            step_pct = round(5.0 + (idx / total_configs) * 90.0, 1)
            try:
                TrainingJobTracker.update_progress(
                    job_id=mission_id,
                    stage=f"CONFIG_{idx + 1}",
                    stage_description=f"Testing {cfg['display_name']} ({idx + 1}/{total_configs})",
                    progress_pct=step_pct
                )
            except Exception:
                pass

            sub_job_id = f"{mission_id}_step_{idx + 1}_{config_label.lower()}"

            try:
                # Execute genuine Qlib model training and locked OOS backtesting
                train_result = QlibTrainer.train_model(
                    strategy=cfg["strategy"],
                    universe=cfg["universe"],
                    feature_family=cfg["feature_family"],
                    model_family=cfg["model_family"],
                    horizon_days=cfg["horizon_days"],
                    target_definition=cfg["target_definition"],
                    friction_bps=cfg["friction_bps"],
                    top_k=cfg.get("top_k", 5),
                    sizing_method=cfg.get("sizing_method", "EQUAL_WEIGHT"),
                    max_exposure_pct=cfg.get("max_exposure_pct", 100.0),
                    job_id=sub_job_id
                )

                candidate_id = train_result.get("model_id") or train_result.get("candidate_model_id")
                cand_manifest = train_result.get("manifest") or train_result.get("artifact", {}).get("manifest", {})
                if not cand_manifest and candidate_id:
                    cand_manifest = QlibModelRegistry.get_candidate_manifest(candidate_id) or {}

                # Authoritative 12-Gate Safety Evaluation
                gate_eval = QlibPromotionPolicy.evaluate_promotion_eligibility(
                    candidate_manifest=cand_manifest,
                    champion_manifest=champion_manifest
                )

                is_eligible = gate_eval.get("is_eligible", False)
                metrics = cand_manifest.get("metrics", {})

                exec_m = metrics.get("execution_metrics", {})
                eq_curve = exec_m.get("equity_curve", [])
                final_eq = eq_curve[-1].get("equity") if eq_curve else None
                net_ret = round(((final_eq / 100000.0) - 1.0) * 100.0, 2) if final_eq is not None else None

                cand_summary = {
                    "config_id": cfg["config_id"],
                    "display_name": cfg["display_name"],
                    "candidate_model_id": candidate_id,
                    "strategy": cfg["strategy"],
                    "features": cfg["feature_family"],
                    "universe": cfg["universe"],
                    "target_definition": cfg["target_definition"],
                    "horizon_days": cfg["horizon_days"],
                    "friction_bps": cfg["friction_bps"],
                    "sharpe": metrics.get("sharpe"),
                    "volatility": exec_m.get("annualized_vol_pct"),
                    "profit_factor": metrics.get("profit_factor"),
                    "max_drawdown_pct": metrics.get("max_drawdown_pct"),
                    "win_rate": metrics.get("win_rate"),
                    "trades_count": metrics.get("trades_count"),
                    "final_equity": final_eq,
                    "net_return": net_ret,
                    "capital_utilization": exec_m.get("max_capital_utilization_pct", 100.0),
                    "ic": metrics.get("ic"),
                    "rank_ic": metrics.get("rank_ic"),
                    "is_eligible": is_eligible,
                    "gates_passed_count": sum(1 for g in gate_eval.get("gates", {}).values() if g.get("passed")),
                    "gates_total": len(gate_eval.get("gates", {})),
                    "failed_reasons": gate_eval.get("failed_reasons", []),
                    "gates": gate_eval.get("gates", {}),
                    "manifest": cand_manifest
                }

                candidates_evaluated.append(cand_summary)

                if is_eligible:
                    qualified_candidates.append(cand_summary)

            except Exception as e:
                logger.error(f"[BestModelFinder] Error running {config_label}: {e}", exc_info=True)
                candidates_evaluated.append({
                    "config_id": cfg["config_id"],
                    "display_name": cfg["display_name"],
                    "error": str(e),
                    "is_eligible": False
                })

        # "BEST MODEL" selection criteria:
        # Highest-quality candidate that passes ALL existing promotion gates!
        if qualified_candidates:
            # Sort eligible candidates following Phase 4 priority:
            # 1. Passes all mandatory gates (already filtered by is_eligible)
            # 2. Lowest Max Drawdown ascending
            # 3. Highest Net Sharpe descending
            # 4. Highest Net Profit Factor descending
            qualified_candidates.sort(
                key=lambda x: (
                    float(x.get("max_drawdown_pct") or 999.0),
                    -float(x.get("sharpe") or -999.0),
                    -float(x.get("profit_factor") or -999.0)
                )
            )
            best_candidate = qualified_candidates[0]
            verdict = "QUALIFIED CANDIDATE — PROMOTION AVAILABLE"
            has_qualified_winner = True
        else:
            best_candidate = None
            verdict = "NO QUALIFIED QLIB CHAMPION"
            has_qualified_winner = False

        mission_result = {
            "mission_id": mission_id,
            "status": "COMPLETED",
            "verdict": verdict,
            "has_qualified_winner": has_qualified_winner,
            "best_candidate": best_candidate,
            "strategy": strat,
            "universe": universe,
            "friction_bps": float(friction_bps),
            "candidates_evaluated_count": len(candidates_evaluated),
            "qualified_candidates_count": len(qualified_candidates),
            "candidates": candidates_evaluated,
            "completed_at": datetime.now().isoformat(),
            "notes": "Evaluation strictly enforced on locked chronological OOS data. No automatic promotion performed."
        }

        MasterLogger.log_event(
            category="QLIB_RESEARCH",
            event_type="QLIB_RESEARCH_MISSION_COMPLETED",
            message=f"Research mission {mission_id} finished: {verdict}",
            universe=universe,
            details={
                "mission_id": mission_id,
                "verdict": verdict,
                "candidates_evaluated": len(candidates_evaluated),
                "qualified_candidates": len(qualified_candidates),
                "best_candidate_id": best_candidate.get("candidate_model_id") if best_candidate else None
            }
        )

        with _MISSION_LOCK:
            _ACTIVE_MISSION = mission_result

        # Persist to disk for cross-process access
        try:
            cache_file = os.path.join(QlibModelRegistry.get_base_dir(), "latest_mission.json")
            with open(cache_file, "w") as fp:
                json.dump(mission_result, fp, indent=2)
        except Exception as e:
            logger.warning(f"[BestModelFinder] Could not persist mission result to disk: {e}")

        # Explicitly complete the parent research mission job in TrainingJobTracker!
        elapsed_mission = round(time.time() - mission_start_time, 1)
        try:
            TrainingJobTracker.complete_job(
                job_id=mission_id,
                candidate_model_id=best_candidate.get("candidate_model_id") if best_candidate else "none",
                metrics=mission_result,
                elapsed_seconds=elapsed_mission
            )
        except Exception as e:
            logger.warning(f"[BestModelFinder] Could not mark mission {mission_id} completed in tracker: {e}")

        # Update telemetry to completed
        training_telemetry.complete_job(
            result=mission_result,
            candidate_model_id=best_candidate.get("candidate_model_id") if best_candidate else None
        )

        return mission_result

    @classmethod
    def start_mission_async(
        cls,
        universe: str = "NIFTY_500_ESTABLISHED",
        friction_bps: float = 25.0,
        strategy: str = "SWING"
    ) -> Dict[str, Any]:
        """
        Launches the research mission in a background thread and registers the job.
        """
        mission_id = f"mission_{strategy.lower()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        # Initialize job in persistent tracker
        tracker = TrainingJobTracker()
        tracker.create_job(
            job_id=mission_id,
            strategy=strategy,
            universe=universe,
            feature_family="Multi-Config Research",
            model_family="LGBModel"
        )
        # Initialize telemetry state for SSE streaming from moment of trigger
        try:
            training_telemetry.start_job(
                job_id=mission_id,
                strategy=strategy,
                universe_name=universe,
                instruments_count=0,
                feature_family="Multi-Config Research",
                model_family="LGBModel"
            )
        except Exception as e:
            logger.warning(f"[BestModelFinder] Telemetry start warning: {e}")

        def _worker():
            try:
                cls.run_research_mission(
                    mission_id=mission_id,
                    universe=universe,
                    friction_bps=friction_bps,
                    strategy=strategy
                )
            except Exception as e:
                logger.error(f"[BestModelFinder] Background mission error: {e}", exc_info=True)
                training_telemetry.fail_job(f"Mission failed: {e}")
                try:
                    TrainingJobTracker.fail_job(job_id=mission_id, error=str(e))
                except Exception:
                    pass

        th = threading.Thread(target=_worker, name=f"QlibResearchMission-{mission_id}", daemon=True)
        th.start()

        return {
            "job_id": mission_id,
            "mission_id": mission_id,
            "status": "LAUNCHED",
            "message": f"Research mission {mission_id} started in background across curated configurations.",
            "universe": universe,
            "friction_bps": friction_bps,
            "strategy": strategy
        }

    @classmethod
    def get_latest_mission_result(cls) -> Optional[Dict[str, Any]]:
        """Returns the latest completed or running mission result."""
        with _MISSION_LOCK:
            if _ACTIVE_MISSION is not None:
                return _ACTIVE_MISSION
        try:
            cache_file = os.path.join(QlibModelRegistry.get_base_dir(), "latest_mission.json")
            if os.path.exists(cache_file):
                with open(cache_file, "r") as fp:
                    return json.load(fp)
        except Exception as e:
            logger.error(f"[BestModelFinder] get_latest_mission_result error: {e}")
        return None
