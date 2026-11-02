"""
RESEARCH BUDGET & RESOURCE ENFORCEMENT
======================================
Monitors research resource limits (max experiments, max runtime, concurrency,
memory thresholds, model/feature caps) on local macOS hardware.
Prevents unbounded execution or system exhaustion.
"""

import time
import os
import resource
import logging
from typing import Dict, Any, Optional, Tuple
from app.analytics.research_orchestrator.research_mission import ResearchMission

logger = logging.getLogger(__name__)

class ResearchBudgetManager:
    """
    Authoritative budget controller for a research mission.
    """

    @staticmethod
    def get_current_rss_mb() -> float:
        """Returns peak Resident Set Size (RSS) memory usage in Megabytes."""
        try:
            # On macOS ru_maxrss is in bytes; on Linux in kilobytes
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if os.uname().sysname == "Darwin":
                return usage / (1024.0 * 1024.0)
            return usage / 1024.0
        except Exception:
            return 0.0

    @classmethod
    def check_budget_status(
        cls,
        mission: ResearchMission,
        completed_count: int,
        failed_count: int,
        start_time_epoch: float,
        feature_counts: Optional[Dict[str, int]] = None,
        model_counts: Optional[Dict[str, int]] = None
    ) -> Tuple[bool, str]:
        """
        Evaluates whether research budget or runtime limits have been exceeded.
        Returns: (is_exhausted: bool, reason: str)
        """
        total_runs = completed_count + failed_count
        budget = mission.budget

        max_exps = budget.get("max_experiments", 100)
        if total_runs >= max_exps:
            return True, f"Experiment budget reached ({total_runs}/{max_exps})"

        max_runtime = budget.get("max_runtime_seconds", 86400)
        elapsed = time.time() - start_time_epoch
        if elapsed >= max_runtime:
            return True, f"Runtime limit exceeded ({elapsed:.1f}s >= {max_runtime}s)"

        # Memory safeguard (safety cap at 6 GB RSS)
        rss_mb = cls.get_current_rss_mb()
        if rss_mb > 6144.0:
            return True, f"RSS memory limit exceeded ({rss_mb:.1f} MB > 6144 MB)"

        return False, "BUDGET_OK"

    @classmethod
    def can_explore_feature(cls, mission: ResearchMission, feature_family: str, count: int) -> bool:
        """Checks if a feature family has hit its mission exploration cap."""
        cap = mission.budget.get("max_per_feature", 50)
        return count < cap

    @classmethod
    def can_explore_model(cls, mission: ResearchMission, model_family: str, count: int) -> bool:
        """Checks if a model family has hit its mission exploration cap."""
        cap = mission.budget.get("max_per_model", 50)
        return count < cap

