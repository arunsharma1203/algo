"""Common pure-stdlib quantitative utilities with zero runtime side effects."""

from app.analytics.common.options_accounting import (
    NSE_OPTION_TICK,
    OptionLeg,
    apply_adverse_slippage_and_tick,
    compute_intrinsic_value,
    compute_leg_accounting,
    compute_package_accounting,
    compute_single_leg_roundtrip,
    compute_structural_package_risk,
    get_upstox_option_tariff,
    round_to_nse_tick,
)

__all__ = [
    "NSE_OPTION_TICK",
    "OptionLeg",
    "apply_adverse_slippage_and_tick",
    "compute_intrinsic_value",
    "compute_leg_accounting",
    "compute_package_accounting",
    "compute_single_leg_roundtrip",
    "compute_structural_package_risk",
    "get_upstox_option_tariff",
    "round_to_nse_tick",
]
