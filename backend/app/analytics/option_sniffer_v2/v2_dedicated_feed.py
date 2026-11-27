"""
V2 Option Sniffer — Market Data Feed Feasibility & Architecture Trade-Off Module.

Evaluates Upstox API V3 Market Data Feed WebSocket connection and subscription limits
against the running platform (`UpstoxStreamService` + `OptionStreamManager`) without
opening any unrequested or V1-disruptive WebSocket connections.

Verified Official Upstox V3 Limits:
  - Normal user tier: up to 2 concurrent V3 Market Data WebSocket connections per user.
  - Upstox Plus tier: up to 5 concurrent V3 Market Data WebSocket connections per user.
  - Normal user per-category limits (individual / combined):
      * Full (L5 depth): 2,000 / 1,500 keys
      * Option Greeks: 3,000 / 2,000 keys
      * LTPC: 5,000 / 2,000 keys
  - Shared `UpstoxStreamService` uses 1 concurrent V3 WebSocket connection for ~507 equities
    plus OptionSniffer contracts; therefore opening a second V3 WebSocket leaves 0 spare
    connection slots on Normal accounts and risks connection-limit rejection if any external
    tool/tab opens another socket.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

UPSTOX_V3_MARKET_FEED_LIMITS: Dict[str, Any] = {
    "normal_max_concurrent_ws_connections": 2,
    "plus_max_concurrent_ws_connections": 5,
    "normal_subscription_limits": {
        "full": {"individual_keys": 2000, "combined_keys": 1500},
        "option_greeks": {"individual_keys": 3000, "combined_keys": 2000},
        "ltpc": {"individual_keys": 5000, "combined_keys": 2000},
    },
    "shared_platform_ws_connections_used": 1,
    "remaining_normal_ws_slots": 1,
    "dedicated_feed_default_enabled": False,
}


def evaluate_feed_architecture_options(
    health_metrics: Optional[Dict[str, Any]] = None,
    shared_subscribed_keys_count: Optional[int] = None,
    account_tier: str = "NORMAL",
) -> Dict[str, Any]:
    """
    Compares `SHARED_PASSIVE_PRIORITY` (default) vs `DEDICATED_V2_OPT_IN_WS` using verified
    Upstox limits and observed V2 4-stage latency/skew telemetry.
    """
    hm = health_metrics or {}
    tier_u = str(account_tier or "NORMAL").upper().strip()
    max_ws = (
        int(UPSTOX_V3_MARKET_FEED_LIMITS["plus_max_concurrent_ws_connections"])
        if tier_u == "PLUS"
        else int(UPSTOX_V3_MARKET_FEED_LIMITS["normal_max_concurrent_ws_connections"])
    )
    remaining_slots = max(0, max_ws - int(UPSTOX_V3_MARKET_FEED_LIMITS["shared_platform_ws_connections_used"]))
    shared_keys = int(shared_subscribed_keys_count or 0)

    return {
        "active_mode": "SHARED_PASSIVE_PRIORITY",
        "dedicated_ws_enabled": False,
        "account_tier_assumed": tier_u,
        "upstox_limits": UPSTOX_V3_MARKET_FEED_LIMITS,
        "remaining_ws_slots_if_shared_running": remaining_slots,
        "shared_subscribed_keys_observed": shared_keys,
        "observed_latency_and_skew": {
            "source_age_p50_ms": hm.get("source_age_p50_ms", 0.0),
            "source_age_p99_ms": hm.get("source_age_p99_ms", 0.0),
            "queue_lag_p50_ms": hm.get("queue_lag_p50_ms", 0.0),
            "queue_lag_p99_ms": hm.get("queue_lag_p99_ms", 0.0),
            "priority_queue_lag_p99_ms": hm.get("priority_queue_lag_p99_ms", 0.0),
            "processing_lag_p50_ms": hm.get("processing_lag_p50_ms", 0.0),
            "processing_lag_p99_ms": hm.get("processing_lag_p99_ms", 0.0),
            "spot_fut_skew_p50_ms": hm.get("spot_fut_skew_p50_ms", 0.0),
            "spot_fut_skew_p99_ms": hm.get("spot_fut_skew_p99_ms", 0.0),
            "cross_leg_skew_p50_ms": hm.get("cross_leg_skew_p50_ms", 0.0),
            "cross_leg_skew_p99_ms": hm.get("cross_leg_skew_p99_ms", 0.0),
            "reconnect_count": hm.get("reconnect_count", 0),
        },
        "options_compared": [
            {
                "mode": "SHARED_PASSIVE_PRIORITY",
                "selected_by_default": True,
                "ws_slots_consumed": 0,
                "risk_to_v1": "ZERO_EXTRA_WS_SLOT (bounded priority queue protects Spot/Fut/active legs and yields watchlist keys first)",
                "pros": [
                    "Consumes 0 extra Upstox WS slots (safe alongside V1 and mobile/web session)",
                    "Active position legs, NIFTY spot, and front FUT stay in dedicated priority queue",
                    "Never evicts V1/Consensus keys",
                ],
                "cons": [
                    "Still depends on upstream shared UpstoxStreamService event loop during severe ~507-equity open bursts",
                ],
            },
            {
                "mode": "DEDICATED_V2_OPT_IN_WS",
                "selected_by_default": False,
                "ws_slots_consumed": 1,
                "risk_to_v1": (
                    "Exhausts final WS slot on Normal accounts (2 max) if enabled; MUST NOT be auto-enabled "
                    "unless verified zero competing WS clients and explicit operator opt-in."
                ),
                "pros": [
                    "Isolates <=25 NIFTY/VIX/FUT/option keys completely from 507-equity tick callbacks",
                ],
                "cons": [
                    "Uses the 2nd of 2 allowed WS connections on a Normal Upstox account",
                    "Candidate only until multi-session source_age / skew telemetry confirms genuine improvement",
                ],
            },
        ],
    }


def compute_v2_priority_keys(open_packages: List[Dict[str, Any]]) -> List[str]:
    """Extracts active position option legs plus NIFTY spot/VIX keys for priority queuing."""
    keys = {"NSE_INDEX|Nifty 50", "NSE_INDEX|India VIX"}
    for pkg in open_packages or []:
        ls = pkg.get("legs_state") or {}
        for role in ("long_leg", "wing_leg", "short_leg"):
            leg = ls.get(role)
            if isinstance(leg, dict) and leg.get("instrument_key"):
                keys.add(str(leg["instrument_key"]).strip())
    return sorted(keys)
