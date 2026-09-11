"""
RESEARCH GOVERNANCE & QUALITY EVALUATION
========================================
Implements immutable governance criteria, anti-overfitting checks,
and research candidate quality classifications.

IMPORTANT GOVERNANCE RULE:
Research quality classifications (EXCELLENT, STRONG, PROMISING, WEAK, OVERFIT,
UNSTABLE, REJECTED, INSUFFICIENT EVIDENCE) are exploratory ratings only.
They NEVER replace, bypass, or weaken the immutable formal promotion gates.
"""

from typing import Dict, Any, List, Tuple, Optional
import logging

logger = logging.getLogger(__name__)

# Hard immutable platform gates
MIN_TRADES_REQUIRED = 30
MAX_DRAWDOWN_CEILING_PCT = 20.0
MIN_PROFIT_FACTOR = 1.0

class ResearchGovernance:
    """
    Evaluates experiments against formal quantitative promotion gates
    and classifies exploratory candidate quality.
    """

    @staticmethod
    def evaluate_formal_governance_gates(
        metrics: Dict[str, Any],
        is_champion_replacement: bool = False,
        champion_metrics: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Evaluates immutable formal promotion gates.
        Returns detailed checklist and binary pass/fail verdict.
        """
        trades = metrics.get("trade_count", 0)
        cagr_net = metrics.get("cagr_net", -999.0)
        expectancy = metrics.get("expectancy", -999.0)
        pf = metrics.get("profit_factor", 0.0)
        sharpe = metrics.get("sharpe", -999.0)
        max_dd = metrics.get("max_drawdown_pct", 100.0)
        survives_costs = metrics.get("survives_friction_30bps", False)
        wf_stability = metrics.get("walk_forward_stability_pct", 0.0)

        gates = {
            "min_trades_30": {
                "passed": trades >= MIN_TRADES_REQUIRED,
                "value": trades,
                "threshold": f">= {MIN_TRADES_REQUIRED}"
            },
            "positive_net_cagr": {
                "passed": cagr_net > 0.0,
                "value": round(cagr_net, 2),
                "threshold": "> 0.0%"
            },
            "positive_expectancy": {
                "passed": expectancy > 0.0,
                "value": round(expectancy, 4),
                "threshold": "> 0.0"
            },
            "profit_factor_above_one": {
                "passed": pf > MIN_PROFIT_FACTOR,
                "value": round(pf, 2),
                "threshold": f"> {MIN_PROFIT_FACTOR}"
            },
            "positive_sharpe": {
                "passed": sharpe > 0.0,
                "value": round(sharpe, 2),
                "threshold": "> 0.0"
            },
            "max_drawdown_ceiling": {
                "passed": max_dd <= MAX_DRAWDOWN_CEILING_PCT,
                "value": round(max_dd, 2),
                "threshold": f"<= {MAX_DRAWDOWN_CEILING_PCT}%"
            },
            "cost_survival_30bps": {
                "passed": bool(survives_costs),
                "value": survives_costs,
                "threshold": "Net positive at 30 bps drag"
            },
            "walk_forward_stability": {
                "passed": wf_stability >= 60.0,
                "value": round(wf_stability, 1),
                "threshold": ">= 60.0% positive windows"
            }
        }

        # Additional gates if candidate is aiming to challenge/replace production Champion
        if is_champion_replacement and champion_metrics:
            champ_sharpe = champion_metrics.get("sharpe", 0.0)
            champ_f1 = champion_metrics.get("f1_score", 0.0)
            cand_f1 = metrics.get("f1_score", 0.0)

            f1_gain = cand_f1 - champ_f1
            sharpe_gain = sharpe - champ_sharpe

            gates["challenger_f1_gain"] = {
                "passed": f1_gain >= 0.0100,
                "value": round(f1_gain, 4),
                "threshold": ">= +0.0100"
            }
            gates["challenger_sharpe_gain"] = {
                "passed": sharpe_gain >= 0.0,
                "value": round(sharpe_gain, 2),
                "threshold": ">= 0.0"
            }
            gates["human_confirmation_required"] = {
                "passed": False,  # Machine never self-approves production replacement
                "value": "PENDING_HUMAN_REVIEW",
                "threshold": "Explicit Human Confirmation"
            }

        all_passed = all(g["passed"] for k, g in gates.items() if k != "human_confirmation_required")
        
        return {
            "passed": all_passed,
            "failed_gates": [k for k, g in gates.items() if not g["passed"]],
            "checklist": gates,
            "verdict": "PASS" if all_passed else "FAIL"
        }

    @staticmethod
    def classify_research_quality(metrics: Dict[str, Any]) -> str:
        """
        Classifies exploratory research candidate quality.
        Does NOT alter or relax formal promotion gates.
        Classes:
        - EXCELLENT
        - STRONG
        - PROMISING
        - WEAK
        - OVERFIT
        - UNSTABLE
        - REJECTED
        - INSUFFICIENT EVIDENCE
        """
        trades = metrics.get("trade_count", 0)
        cagr = metrics.get("cagr_net", -999.0)
        sharpe = metrics.get("sharpe", -999.0)
        max_dd = metrics.get("max_drawdown_pct", 100.0)
        turnover = metrics.get("turnover_pct", 9999.0)
        wf_stability = metrics.get("walk_forward_stability_pct", 0.0)
        survives_30 = metrics.get("survives_friction_30bps", False)
        survives_20 = metrics.get("survives_friction_20bps", False)
        survives_15 = metrics.get("survives_friction_15bps", False)
        regime_negative_count = metrics.get("regime_negative_count", 0)

        # 1. Sample Size Check
        if trades < MIN_TRADES_REQUIRED:
            return "INSUFFICIENT EVIDENCE"

        # 2. Hard Failure Check
        if cagr <= 0.0 or sharpe <= 0.0 or max_dd > MAX_DRAWDOWN_CEILING_PCT:
            return "REJECTED"

        # 3. Overfit / Unstable Diagnoses
        if sharpe >= 1.8 and wf_stability < 50.0:
            return "OVERFIT"
        if regime_negative_count >= 2 or max_dd > 18.0:
            return "UNSTABLE"

        # 4. Hierarchical Quality Tiers
        if sharpe >= 1.5 and max_dd <= 15.0 and turnover <= 800.0 and survives_30 and wf_stability >= 80.0:
            return "EXCELLENT"
        elif sharpe >= 1.2 and max_dd <= 18.0 and turnover <= 1200.0 and survives_20 and wf_stability >= 70.0:
            return "STRONG"
        elif sharpe >= 1.0 and max_dd <= 20.0 and survives_15 and wf_stability >= 60.0:
            return "PROMISING"
        else:
            return "WEAK"
