"""Isolated live execution subsystem for Option Sniffer Advance V2.

Strictly separated from:
- V2 paper baseline (`app.analytics.option_sniffer_v2` & `option_sniffer_advance_v2.db`)
- V1 / Main / Consensus (`market_data.db` & model weights)

Always boots disarmed (`armed=False`) and requires explicit human PIN confirmation
to arm via interactive UI controls.
"""

from app.analytics.option_sniffer_v2_live.v2_live_ledger import (
    V2LiveDatabaseIsolationError,
    V2LiveLedgerStore,
    get_default_v2_live_db_path,
    set_v2_live_test_db_override,
)
from app.analytics.option_sniffer_v2_live.v2_upstox_broker_client import (
    UPSTOX_VERIFIED_API_FACTS,
    BrokerOrderRejectedError,
    BrokerTimeoutAmbiguousError,
    MockV2BrokerClient,
    UpstoxV2BrokerClient,
    compute_smart_limit_collar,
    prepare_upstox_v3_order_payload,
    sanitize_upstox_tag,
    validate_upstox_v3_gtt_payload,
)
from app.analytics.option_sniffer_v2_live.v2_live_execution_engine import (
    V2LiveExecutionEngine,
    get_option_sniffer_v2_live_engine,
    reset_option_sniffer_v2_live_engine,
)

__all__ = [
    "UPSTOX_VERIFIED_API_FACTS",
    "BrokerOrderRejectedError",
    "BrokerTimeoutAmbiguousError",
    "MockV2BrokerClient",
    "UpstoxV2BrokerClient",
    "V2LiveDatabaseIsolationError",
    "V2LiveExecutionEngine",
    "V2LiveLedgerStore",
    "get_default_v2_live_db_path",
    "get_option_sniffer_v2_live_engine",
    "reset_option_sniffer_v2_live_engine",
    "sanitize_upstox_tag",
    "set_v2_live_test_db_override",
]
