"""
V2 Option Sniffer (`option_sniffer_v2`) — Isolated Paper-Only Parallel Experiment.

Never imports live execution, order intent, arm/disarm, or mirror modules.
All options P&L, Upstox fees, tick rounding, and structural risk route through
`app.analytics.common.options_accounting`.
"""

from app.analytics.option_sniffer_v2.v2_config import (
    DEFAULT_V2_PREREG_PATH,
    EXISTING_V2_SYSTEMS_INVENTORY,
    V2PaperOnlyInvariantError,
    canonicalize_v2_config,
    compute_v2_config_hash,
    is_nse_trading_session,
    load_v2_preregistration,
    resolve_contract_lot_size,
    trading_sessions_between,
)
from app.analytics.option_sniffer_v2.v2_service import (
    OptionSnifferV2Service,
    get_option_sniffer_v2_service,
    reset_option_sniffer_v2_service,
)

__all__ = [
    "DEFAULT_V2_PREREG_PATH",
    "EXISTING_V2_SYSTEMS_INVENTORY",
    "OptionSnifferV2Service",
    "V2PaperOnlyInvariantError",
    "canonicalize_v2_config",
    "compute_v2_config_hash",
    "get_option_sniffer_v2_service",
    "is_nse_trading_session",
    "load_v2_preregistration",
    "reset_option_sniffer_v2_service",
    "resolve_contract_lot_size",
    "trading_sessions_between",
]
