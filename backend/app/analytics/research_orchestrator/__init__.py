"""
AUTONOMOUS RESEARCH ORCHESTRATOR PACKAGE
========================================
Modular quantitative research engine supporting both:
1. New Autonomous Research Lab V1 (continuous alpha discovery, memory, candidate vault, 500+ universe expansion).
2. Legacy Research Orchestrator Daemon & Job Queue (backward-compatible).
"""

# New Autonomous Research Lab V1 Components
from app.analytics.research_orchestrator.research_mission import ResearchMission
from app.analytics.research_orchestrator.research_budget import ResearchBudgetManager
from app.analytics.research_orchestrator.research_governance import ResearchGovernance
from app.analytics.research_orchestrator.research_metrics import ResearchMetricsEngine
from app.analytics.research_orchestrator.research_memory import ResearchMemory
from app.analytics.research_orchestrator.experiment_generator import ExperimentGenerator
from app.analytics.research_orchestrator.research_scheduler import ResearchScheduler
from app.analytics.research_orchestrator.candidate_vault import CandidateVault
from app.analytics.research_orchestrator.universe_transfer import UniverseTransferEngine
from app.analytics.research_orchestrator.research_orchestrator import ResearchOrchestrator
from app.analytics.research_orchestrator.autonomous_runner import AutonomousResearchRunner

# Backward-Compatible Legacy Orchestrator Components
from app.analytics.research_orchestrator.legacy_engine import (
    JobPriority,
    JobStatus,
    JobType,
    ErrorCategory,
    ResearchOrchestrator as LegacyResearchOrchestrator,
    research_orchestrator
)

__all__ = [
    # New V1 Engine
    "ResearchMission",
    "ResearchBudgetManager",
    "ResearchGovernance",
    "ResearchMetricsEngine",
    "ResearchMemory",
    "ExperimentGenerator",
    "ResearchScheduler",
    "CandidateVault",
    "UniverseTransferEngine",
    "ResearchOrchestrator",
    "AutonomousResearchRunner",

    # Legacy Compatibility
    "JobPriority",
    "JobStatus",
    "JobType",
    "ErrorCategory",
    "LegacyResearchOrchestrator",
    "research_orchestrator"
]

