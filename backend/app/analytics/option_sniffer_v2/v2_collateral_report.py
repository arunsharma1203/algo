"""
V2 Option Sniffer — Optional Collateral Yield Reporting Scenario.

Guarantees:
  - Strictly reporting-only (`ZERO` purchases, pledges, transfers, or broker API calls).
  - Separates trading P&L, hypothetical net collateral income (`ASSUMED_YIELD_NOT_TRADING_ALPHA`),
    haircuts, cash available for MTM, and the 6.5% risk-free benchmark without double-counting.
"""

from __future__ import annotations

from typing import Any, Dict


def compute_optional_collateral_report(
    starting_capital_inr: float,
    trading_net_pnl_inr: float,
    peak_reserved_margin_inr: float,
    calendar_days_observed: int,
    collateral_cfg: Dict[str, Any],
    enabled: bool = False,
) -> Dict[str, Any]:
    """
    Computes hypothetical pledged Liquid ETF / G-Sec reporting metrics while explicitly
    keeping trading alpha isolated from passive collateral carry.
    """
    cap = max(1.0, float(starting_capital_inr))
    days = max(1, int(calendar_days_observed))
    rf_pa = float(collateral_cfg.get("risk_free_benchmark_pa_pct", 6.5)) / 100.0
    rf_period_inr = round(cap * rf_pa * (days / 365.0), 2)

    pledge_frac = max(0.0, min(0.90, float(collateral_cfg.get("liquid_etf_pledge_fraction", 0.75))))
    haircut = max(0.0, min(0.50, float(collateral_cfg.get("broker_haircut_pct", 10.0)) / 100.0))
    gross_yield_pa = float(collateral_cfg.get("assumed_gross_yield_pa_pct", 6.0)) / 100.0
    drag_pa = float(collateral_cfg.get("etf_expense_and_tracking_drag_pa_pct", 0.20)) / 100.0
    net_yield_pa = max(0.0, gross_yield_pa - drag_pa)

    pledged_principal = round(cap * pledge_frac, 2)
    haircut_inr = round(pledged_principal * haircut, 2)
    usable_collateral_margin = round(pledged_principal - haircut_inr, 2)
    free_cash_for_mtm_and_50_50_rule = round(cap - pledged_principal, 2)

    # Check 50:50 SEBI/exchange cash-to-collateral rule feasibility under peak margin lock
    half_peak_margin = round(0.50 * float(peak_reserved_margin_inr), 2)
    cash_rule_buffer_inr = round(free_cash_for_mtm_and_50_50_rule - half_peak_margin, 2)

    # Never double-count yield on funds converted to realized trading losses; apply net yield strictly to pledged principal.
    hypothetical_collateral_income = round(pledged_principal * net_yield_pa * (days / 365.0), 2) if enabled else 0.0
    combined_account_total = round(float(trading_net_pnl_inr) + hypothetical_collateral_income, 2)
    excess_trading_only_vs_rf = round(float(trading_net_pnl_inr) - rf_period_inr, 2)
    excess_combined_vs_rf = round(combined_account_total - rf_period_inr, 2)

    return {
        "scenario_enabled": bool(enabled),
        "classification_label": "ASSUMED_YIELD_NOT_TRADING_ALPHA",
        "no_pledge_or_transfer_side_effect": True,
        "calendar_days_observed": days,
        "starting_capital_inr": cap,
        "trading_net_pnl_inr": round(float(trading_net_pnl_inr), 2),
        "pledged_collateral_principal_inr": pledged_principal if enabled else 0.0,
        "haircut_pct": round(haircut * 100.0, 2),
        "haircut_inr": haircut_inr if enabled else 0.0,
        "usable_collateral_margin_inr": usable_collateral_margin if enabled else 0.0,
        "cash_available_for_mtm_inr": free_cash_for_mtm_and_50_50_rule if enabled else cap,
        "peak_reserved_margin_inr": round(float(peak_reserved_margin_inr), 2),
        "cash_margin_50pct_requirement_inr": half_peak_margin,
        "cash_buffer_after_50pct_rule_inr": cash_rule_buffer_inr if enabled else round(cap - float(peak_reserved_margin_inr), 2),
        "assumed_net_yield_pa_pct": round(net_yield_pa * 100.0, 2),
        "hypothetical_collateral_income_inr": hypothetical_collateral_income,
        "combined_hypothetical_total_pnl_inr": combined_account_total,
        "rf_benchmark_6p5_period_inr": rf_period_inr,
        "excess_trading_alpha_vs_rf_inr": excess_trading_only_vs_rf,
        "excess_combined_vs_rf_inr": excess_combined_vs_rf,
    }
