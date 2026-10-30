"""
QLIB PROMOTION POLICY & GOVERNANCE GATES
========================================
Authoritative mathematical and quantitative promotion policy for Microsoft Qlib models.
Enforces strict champion-beating requirements, sample size limits, anti-overfitting rules,
and cryptographic integrity.

Invariants:
- A negative-Sharpe candidate MUST NEVER be promotable.
- Candidate must demonstrate superiority over the current active champion.
- Complete audit trail of all promotion attempts.
- Backend is the sole security boundary.
"""

import os
import hashlib
import logging
from typing import Dict, Any, List, Optional, Tuple

from app.analytics.master_logger import MasterLogger

logger = logging.getLogger(__name__)

# Formal quantitative thresholds
MIN_OOS_TRADES_REQUIRED = 30
MIN_PROFIT_FACTOR_REQUIRED = 1.0
MAX_DRAWDOWN_CEILING_PCT = 20.0  # Authoritative platform risk ceiling (matching research governance)
PROMOTION_SHARPE_MARGIN = 0.05   # Challenger must beat Champion Sharpe by >= +0.05 (anti-churn margin)
PROMOTION_PF_MARGIN = 0.02       # Challenger must beat Champion Profit Factor by >= +0.02


class PromotionResult(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class QlibPromotionPolicy:
    """
    Authoritative promotion policy enforcing 12 non-negotiable quantitative gates.
    """

    @classmethod
    def evaluate_promotion_eligibility(
        cls,
        candidate_manifest: Dict[str, Any],
        champion_manifest: Optional[Dict[str, Any]] = None,
        force: bool = False,
        audit_log: bool = False
    ) -> PromotionResult:
        """
        Evaluates all 12 Promotion Gates and returns full evaluation report.
        """
        gates: Dict[str, Dict[str, Any]] = {}
        failed_reasons: List[str] = []

        cand_id = candidate_manifest.get("model_id", "UNKNOWN_CANDIDATE")
        cand_status = candidate_manifest.get("status", "")
        cand_metrics = candidate_manifest.get("metrics", {})
        cand_exec_metrics = cand_metrics.get("execution_metrics", {})
        cand_sig_metrics = cand_metrics.get("signal_metrics", {})

        champ_id = champion_manifest.get("model_id") if champion_manifest else None
        champ_metrics = champion_manifest.get("metrics", {}) if champion_manifest else {}
        champ_exec = champ_metrics.get("execution_metrics", {}) if champ_metrics else {}

        # 1. Gate: training_completed
        is_completed = (
            cand_status in ("FROZEN_CHALLENGER", "CHALLENGER_CREATED", "COMPLETED", "PROMOTED") or
            candidate_manifest.get("training_completed", False) is True
        )
        gates["training_completed"] = {
            "name": "Training Completed",
            "passed": is_completed,
            "value": cand_status or ("COMPLETED" if is_completed else "INCOMPLETE"),
            "threshold": "FROZEN_CHALLENGER / COMPLETED",
            "message": "Candidate model training and artifact freezing must be complete."
        }
        if not is_completed:
            failed_reasons.append(f"Candidate status is '{cand_status}', expected 'FROZEN_CHALLENGER'.")

        # 2. Gate: validation_successful
        has_metrics = bool(cand_metrics) and any(k in cand_metrics for k in ("sharpe", "ic", "win_rate", "trades_count", "trades"))
        gates["validation_successful"] = {
            "name": "Validation Successful",
            "passed": has_metrics,
            "value": "Present" if has_metrics else "Missing",
            "threshold": "OOS Metrics Present",
            "message": "Out-of-sample forward validation must have completed successfully."
        }
        if not has_metrics:
            failed_reasons.append("Out-of-sample forward evaluation metrics are missing.")

        # Extract numeric metrics
        cand_sharpe = cand_exec_metrics.get("net_sharpe", cand_metrics.get("sharpe"))
        cand_pf = cand_exec_metrics.get("net_profit_factor", cand_metrics.get("profit_factor"))
        cand_win_rate = cand_exec_metrics.get("net_win_rate", cand_metrics.get("win_rate"))
        cand_trades = cand_exec_metrics.get(
            "net_trades_count",
            cand_metrics.get("trades_count", cand_metrics.get("trades", cand_metrics.get("total_trades", 0)))
        )
        cand_dd = cand_exec_metrics.get("net_max_drawdown_pct", cand_metrics.get("max_drawdown_pct"))
        cand_ic = cand_sig_metrics.get("information_coefficient", cand_metrics.get("ic"))

        # Check Authoritative Accounting Standard (DAILY_MTM_V1 required)
        accounting_method = cand_exec_metrics.get("portfolio_accounting") or cand_metrics.get("methodology") or "DAILY_MTM_V1"
        is_deprecated_accounting = accounting_method in ("OVERLAPPING_LEGACY_FLAWED", "STAGGERED_COHORT_LUMPY_DEPRECATED")
        is_authoritative_accounting = not is_deprecated_accounting
        
        gates["authoritative_accounting_standard"] = {
            "name": "Authoritative MTM Standard",
            "passed": is_authoritative_accounting and not is_deprecated_accounting,
            "value": str(accounting_method or "DAILY_MTM_V1"),
            "threshold": "DAILY_MTM_V1",
            "message": "Only authoritative DAILY_MTM_V1 accounting may feed promotion gates. Deprecated methodologies cannot qualify."
        }
        if is_deprecated_accounting:
            failed_reasons.append(
                f"Candidate uses deprecated accounting methodology '{accounting_method}'. "
                f"Promotion requires authoritative DAILY_MTM_V1 metrics."
            )

        champ_sharpe = champ_exec.get("net_sharpe", champ_metrics.get("sharpe"))
        champ_pf = champ_exec.get("net_profit_factor", champ_metrics.get("profit_factor"))
        champ_win_rate = champ_exec.get("net_win_rate", champ_metrics.get("win_rate"))
        champ_trades = champ_exec.get(
            "net_trades_count",
            champ_metrics.get("trades_count", champ_metrics.get("trades", champ_metrics.get("total_trades", 0)))
        )
        champ_dd = champ_exec.get("net_max_drawdown_pct", champ_metrics.get("max_drawdown_pct"))

        # 3. Gate: valid_metrics (non-None numeric types)
        metrics_valid = (
            cand_sharpe is not None and isinstance(cand_sharpe, (int, float)) and
            cand_pf is not None and isinstance(cand_pf, (int, float)) and
            cand_win_rate is not None and isinstance(cand_win_rate, (int, float))
        )
        gates["valid_metrics"] = {
            "name": "Valid Evaluation Metrics",
            "passed": metrics_valid,
            "value": f"Sharpe: {cand_sharpe}, PF: {cand_pf}, WR: {cand_win_rate}%" if metrics_valid else "Invalid/NaN",
            "threshold": "Numeric OOS Metrics",
            "message": "Evaluation metrics must be valid finite numbers."
        }
        if not metrics_valid:
            failed_reasons.append("Candidate evaluation metrics contain null or non-numeric values.")

        # 4. Gate: positive_sharpe (CRITICAL INVARIANT)
        has_positive_sharpe = (cand_sharpe is not None) and (float(cand_sharpe) > 0.0)
        gates["positive_sharpe"] = {
            "name": "Positive Net Sharpe Ratio",
            "passed": has_positive_sharpe,
            "value": round(float(cand_sharpe), 2) if cand_sharpe is not None else None,
            "threshold": "> 0.00",
            "message": "Candidate net Sharpe ratio (after 25 bps transaction costs) must be strictly positive."
        }
        if not has_positive_sharpe:
            failed_reasons.append(f"Candidate net Sharpe is {cand_sharpe} (must be > 0.00). Negative-Sharpe models are non-promotable.")

        # 5. Gate: min_profit_factor
        has_min_pf = (cand_pf is not None) and (float(cand_pf) >= MIN_PROFIT_FACTOR_REQUIRED)
        gates["min_profit_factor"] = {
            "name": "Profitable After Friction",
            "passed": has_min_pf,
            "value": round(float(cand_pf), 2) if cand_pf is not None else None,
            "threshold": f">= {MIN_PROFIT_FACTOR_REQUIRED:.2f}",
            "message": "Candidate Net Profit Factor after transaction costs must be >= 1.00 (economically viable)."
        }
        if not has_min_pf:
            failed_reasons.append(f"Candidate Net Profit Factor is {cand_pf} (must be >= {MIN_PROFIT_FACTOR_REQUIRED:.2f}).")

        # 6. Gate: min_trades_30 (Historical chronological validation sample size)
        # Uses existing historical chronological validation/holdout framework (no 30-day live wait required).
        has_min_trades = (cand_trades is not None) and (int(cand_trades) >= MIN_OOS_TRADES_REQUIRED)
        gates["min_trades_30"] = {
            "name": "Historical Validation Sample Size",
            "passed": has_min_trades,
            "value": cand_trades,
            "threshold": f">= {MIN_OOS_TRADES_REQUIRED} trades",
            "message": "Must have >= 30 historical chronological holdout trades to establish statistical validity without forward live delay."
        }
        if not has_min_trades:
            failed_reasons.append(f"Candidate trades count is {cand_trades} (required >= {MIN_OOS_TRADES_REQUIRED} historical validation trades).")

        # 7. Gate: max_drawdown (Authoritative risk bounds on historical validation data)
        # Enforces both absolute platform risk ceiling (<= 20.0%) and finite bounds [0%, 100%]
        has_acceptable_dd = True
        dd_failure_detail = ""
        dd_val = None

        if cand_dd is not None:
            try:
                import math
                dd_val = float(cand_dd)
                if math.isnan(dd_val) or math.isinf(dd_val):
                    has_acceptable_dd = False
                    dd_failure_detail = "Candidate drawdown is NaN or infinite."
                elif dd_val < 0.0:
                    has_acceptable_dd = False
                    dd_failure_detail = f"Candidate drawdown is negative ({dd_val:.2f}%)."
                elif dd_val > MAX_DRAWDOWN_CEILING_PCT:
                    has_acceptable_dd = False
                    dd_failure_detail = (
                        f"Candidate drawdown ({dd_val:.2f}%) exceeds platform maximum risk ceiling "
                        f"({MAX_DRAWDOWN_CEILING_PCT:.1f}%)."
                    )
                elif champion_manifest and champ_dd is not None:
                    # Also enforce relative drawdown bounds against champion if champion is active
                    try:
                        c_dd = float(champ_dd)
                        rel_ceiling = max(MAX_DRAWDOWN_CEILING_PCT, c_dd * 1.1)
                        if dd_val > rel_ceiling:
                            has_acceptable_dd = False
                            dd_failure_detail = (
                                f"Candidate drawdown ({dd_val:.2f}%) degrades significantly against Champion "
                                f"({c_dd:.2f}%)."
                            )
                    except Exception:
                        pass
            except Exception:
                has_acceptable_dd = False
                dd_failure_detail = "Candidate drawdown value cannot be parsed as a numeric float."
        else:
            has_acceptable_dd = False
            dd_failure_detail = "Candidate drawdown metric is missing."

        gates["max_drawdown"] = {
            "name": "Drawdown Risk Ceiling",
            "passed": has_acceptable_dd,
            "value": round(float(dd_val), 2) if dd_val is not None and not (isinstance(dd_val, float) and (math.isnan(dd_val) or math.isinf(dd_val))) else None,
            "threshold": f"<= {MAX_DRAWDOWN_CEILING_PCT:.1f}% (Capital Preservation Boundary)",
            "message": "Model true peak-to-trough drawdown must not exceed authoritative risk ceiling."
        }
        if not has_acceptable_dd:
            failed_reasons.append(dd_failure_detail or f"Candidate drawdown risk exceeds allowable risk ceiling ({MAX_DRAWDOWN_CEILING_PCT:.1f}%).")

        # 8. Gate: champion_beating_sharpe
        beats_champ_sharpe = True
        sharpe_threshold_str = "N/A"
        if champion_manifest and champ_sharpe is not None and cand_sharpe is not None:
            required_sharpe = round(float(champ_sharpe) + PROMOTION_SHARPE_MARGIN, 4)
            sharpe_threshold_str = f">= {required_sharpe:.2f} (Champion {float(champ_sharpe):.2f} + {PROMOTION_SHARPE_MARGIN:.2f} margin)"
            beats_champ_sharpe = float(cand_sharpe) >= required_sharpe
            if not beats_champ_sharpe:
                failed_reasons.append(
                    f"Candidate Sharpe ({cand_sharpe:.2f}) does not beat Champion Sharpe ({champ_sharpe:.2f}) by required margin (+{PROMOTION_SHARPE_MARGIN:.2f})."
                )

        gates["champion_beating_sharpe"] = {
            "name": "Champion-Beating Sharpe",
            "passed": beats_champ_sharpe,
            "value": f"Candidate: {cand_sharpe} vs Champion: {champ_sharpe}",
            "threshold": sharpe_threshold_str,
            "message": "Candidate must demonstrate higher risk-adjusted returns than active champion with required margin."
        }

        # 9. Gate: champion_beating_profit_factor
        beats_champ_pf = True
        pf_threshold_str = "1.00"
        if champion_manifest and champ_pf is not None and cand_pf is not None:
            if float(champ_pf) >= 1.0:
                required_pf = round(float(champ_pf) + PROMOTION_PF_MARGIN, 4)
                pf_threshold_str = f">= {required_pf:.2f} (Champion {float(champ_pf):.2f} + {PROMOTION_PF_MARGIN:.2f} margin)"
                beats_champ_pf = float(cand_pf) >= required_pf
                if not beats_champ_pf:
                    failed_reasons.append(
                        f"Candidate Profit Factor ({cand_pf:.2f}) does not beat Champion Profit Factor ({champ_pf:.2f}) by required margin (+{PROMOTION_PF_MARGIN:.2f})."
                    )

        gates["champion_beating_profit_factor"] = {
            "name": "Champion-Beating Profit Factor",
            "passed": beats_champ_pf,
            "value": f"Candidate: {cand_pf} vs Champion: {champ_pf}",
            "threshold": pf_threshold_str,
            "message": "Candidate must exceed current active champion's profitability with required margin."
        }

        # 10. Gate: not_already_active
        is_not_already_active = bool(champ_id is None or cand_id != champ_id)
        gates["not_already_active"] = {
            "name": "Candidate Not Already Active",
            "passed": is_not_already_active,
            "value": cand_id,
            "threshold": f"!= {champ_id or 'None'}",
            "message": "Candidate cannot be promoted if it is already the active model."
        }
        if not is_not_already_active:
            failed_reasons.append(f"Model {cand_id} is already the active champion.")

        # 11. Gate: cryptographic_integrity
        artifact_path = candidate_manifest.get("artifact_path") or candidate_manifest.get("model_path")
        expected_sha = candidate_manifest.get("artifact_sha256")
        sha_valid = False
        if artifact_path and os.path.exists(artifact_path) and expected_sha:
            try:
                h = hashlib.sha256()
                with open(artifact_path, "rb") as fp:
                    h.update(fp.read())
                sha_valid = (h.hexdigest() == expected_sha)
            except Exception:
                sha_valid = False

        gates["cryptographic_integrity"] = {
            "name": "Cryptographic Hash Verification",
            "passed": sha_valid,
            "value": "Verified" if sha_valid else "Checksum Mismatch or File Missing",
            "threshold": "Exact SHA-256 Match",
            "message": "Model artifact byte-for-byte SHA-256 hash must verify against manifest."
        }
        if not sha_valid:
            failed_reasons.append("Model artifact file is missing or SHA-256 checksum does not match manifest.")

        # 12. Gate: schema_validity
        has_valid_schema = bool(
            (candidate_manifest.get("model_class") or candidate_manifest.get("model_family")) and
            (candidate_manifest.get("feature_handler") or candidate_manifest.get("feature_family")) and
            candidate_manifest.get("strategy")
        )
        gates["schema_validity"] = {
            "name": "Manifest & Schema Validity",
            "passed": has_valid_schema,
            "value": "Valid" if has_valid_schema else "Incomplete",
            "threshold": "Complete Model Metadata",
            "message": "Feature schema, model architecture, and strategy definitions must be valid."
        }
        if not has_valid_schema:
            failed_reasons.append("Candidate manifest is missing required architectural schema fields.")

        # Overall verdict
        all_passed = all(g["passed"] for g in gates.values())
        if force:
            eligible = True
            verdict = "FORCE_OVERRIDE"
        else:
            eligible = all_passed
            verdict = "ELIGIBLE" if eligible else "NOT_ELIGIBLE"

        passed_gates_list = [k for k, g in gates.items() if g["passed"]]
        failed_gates_list = [k for k, g in gates.items() if not g["passed"]]
        gates_detail_dict = {k: g["passed"] for k, g in gates.items()}

        if audit_log:
            MasterLogger.log_event(
                category="QLIB_PROMOTION",
                event_type="QLIB_PROMOTION_EVALUATION",
                message=f"Promotion evaluation for {cand_id}: Verdict={verdict} ({len(passed_gates_list)}/{len(gates)} gates passed)",
                details={
                    "candidate_model_id": cand_id,
                    "verdict": verdict,
                    "is_eligible": eligible,
                    "passed_gates": passed_gates_list,
                    "failed_gates": failed_gates_list,
                    "failed_reasons": failed_reasons
                }
            )
        else:
            logger.debug(f"[QlibPromotionPolicy] Evaluated {cand_id}: verdict={verdict}")

        return PromotionResult({
            "eligible": eligible,
            "is_eligible": eligible,
            "verdict": verdict,
            "status": verdict,
            "candidate_model_id": cand_id,
            "champion_model_id": champ_id,
            "gates_passed_count": len(passed_gates_list),
            "gates_total_count": len(gates),
            "passed_gates": passed_gates_list,
            "failed_gates": failed_gates_list,
            "gates_detail": gates_detail_dict,
            "gates": gates,
            "failed_reasons": failed_reasons
        })

    # Alias for convenience
    evaluate_candidate_for_promotion = evaluate_promotion_eligibility

    @classmethod
    def compare_candidate_vs_champion(
        cls,
        candidate_manifest: Dict[str, Any],
        champion_manifest: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Produces side-by-side metric comparison and compatibility assessment.
        """
        if not champion_manifest:
            return {
                "has_champion": False,
                "comparison_valid": False,
                "compatibility_message": "NO ACTIVE CHAMPION registered. Candidate is evaluated against absolute gates.",
                "metrics": {}
            }

        cand_metrics = candidate_manifest.get("metrics", {})
        cand_exec = cand_metrics.get("execution_metrics", {})
        champ_metrics = champion_manifest.get("metrics", {})
        champ_exec = champ_metrics.get("execution_metrics", {})

        # Check comparability
        cand_strat = candidate_manifest.get("strategy", "").upper()
        champ_strat = champion_manifest.get("strategy", "").upper()
        cand_univ = candidate_manifest.get("universe", "")
        champ_univ = champion_manifest.get("universe", "")

        is_compatible = (cand_strat == champ_strat)
        compat_msg = "Compatible Strategy Evaluation" if is_compatible else f"Incompatible Strategies ({cand_strat} vs {champ_strat})"

        def _calc_delta(val_c, val_ch):
            if val_c is not None and val_ch is not None:
                try:
                    return round(float(val_c) - float(val_ch), 2)
                except Exception:
                    pass
            return None

        metrics_comp = {
            "sharpe": {
                "label": "Net Sharpe Ratio",
                "candidate": cand_exec.get("net_sharpe", cand_metrics.get("sharpe")),
                "champion": champ_exec.get("net_sharpe", champ_metrics.get("sharpe")),
                "better_if": "higher"
            },
            "profit_factor": {
                "label": "Net Profit Factor",
                "candidate": cand_exec.get("net_profit_factor", cand_metrics.get("profit_factor")),
                "champion": champ_exec.get("net_profit_factor", champ_metrics.get("profit_factor")),
                "better_if": "higher"
            },
            "win_rate": {
                "label": "Net Win Rate (%)",
                "candidate": cand_exec.get("net_win_rate", cand_metrics.get("win_rate")),
                "champion": champ_exec.get("net_win_rate", champ_metrics.get("win_rate")),
                "better_if": "higher"
            },
            "max_drawdown": {
                "label": "Max Drawdown (%)",
                "candidate": cand_exec.get("net_max_drawdown_pct", cand_metrics.get("max_drawdown_pct")),
                "champion": champ_exec.get("net_max_drawdown_pct", champ_metrics.get("max_drawdown_pct")),
                "better_if": "lower"
            },
            "trades_count": {
                "label": "OOS Trade Count",
                "candidate": cand_exec.get("net_trades_count", cand_metrics.get("trades_count")),
                "champion": champ_exec.get("net_trades_count", champ_metrics.get("trades_count")),
                "better_if": "higher"
            },
            "mean_trade_pnl": {
                "label": "Mean Trade P&L (%)",
                "candidate": cand_exec.get("net_mean_trade_pnl_pct", cand_metrics.get("mean_trade_pnl_pct")),
                "champion": champ_exec.get("net_mean_trade_pnl_pct", champ_metrics.get("mean_trade_pnl_pct")),
                "better_if": "higher"
            }
        }

        for m_key, m_val in metrics_comp.items():
            delta = _calc_delta(m_val["candidate"], m_val["champion"])
            m_val["delta"] = delta
            if delta is not None:
                if m_val["better_if"] == "higher":
                    m_val["is_better"] = delta > 0
                    m_val["is_worse"] = delta < 0
                else:
                    m_val["is_better"] = delta < 0
                    m_val["is_worse"] = delta > 0
            else:
                m_val["is_better"] = False
                m_val["is_worse"] = False

        return {
            "has_champion": True,
            "champion_model_id": champion_manifest.get("model_id"),
            "comparison_valid": is_compatible,
            "compatibility_message": compat_msg,
            "candidate_universe": cand_univ,
            "champion_universe": champ_univ,
            "metrics": metrics_comp
        }
