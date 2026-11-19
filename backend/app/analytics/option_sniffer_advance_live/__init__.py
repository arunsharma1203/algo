"""
Isolated Live Broker Execution Bridge for Option Sniffer Advance.
Strictly decoupled from `backend/app/analytics/option_sniffer_advance/*` (0 core files touched).

Uses PEP 562 lazy module attribute resolution (`__getattr__`) so importing `ShadowMirrorExecutor`
in pure-stdlib environments never eagerly loads third-party HTTP/DataFrame modules.
"""

from typing import Any

__all__ = [
    "AdvanceLiveBridge",
    "ShadowMirrorExecutor",
    "MODE_SHADOW_DRY_RUN_AB",
    "MODE_SHADOW_LIVE_BOOK_A",
    "MODE_SHADOW_LIVE_BOOK_B",
    "MODE_SHADOW_LIVE_AB",
    "get_advance_live_bridge",
    "get_shadow_mirror_executor",
    "init_advance_live_tables",
    "init_shadow_mirror_tables",
]

_SHADOW_MIRROR_EXPORTS = {
    "ShadowMirrorExecutor",
    "MODE_SHADOW_DRY_RUN_AB",
    "MODE_SHADOW_LIVE_BOOK_A",
    "MODE_SHADOW_LIVE_BOOK_B",
    "MODE_SHADOW_LIVE_AB",
    "get_shadow_mirror_executor",
    "init_shadow_mirror_tables",
}

_BRIDGE_EXPORTS = {
    "AdvanceLiveBridge",
    "get_advance_live_bridge",
    "init_advance_live_tables",
}


def __getattr__(name: str) -> Any:
    if name in _SHADOW_MIRROR_EXPORTS:
        from app.analytics.option_sniffer_advance_live import shadow_mirror_executor as _sm

        return getattr(_sm, name)
    if name in _BRIDGE_EXPORTS:
        from app.analytics.option_sniffer_advance_live import advance_live_bridge as _alb

        return getattr(_alb, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
