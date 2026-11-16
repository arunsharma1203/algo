"""
Consensus V3 — Audited 6-Layer Equity Sniffer & Funded-Cash Arena Engine
========================================================================
Implements:
  1. Baseline  : Published 12-1 Sharpe-Momentum (20d scheduled rebalance) & Intraday ORB30 / PDH Sweep baselines.
  2. Qlib      : Vectorized (<0.5s) cross-sectional horizon forecaster (`20d` Book 1; `Entry->15:15 IST` Book 2).
  3. Legacy    : Strictly independent causal payoff/risk estimator (ACTIVE in Book 2; SHADOW_ONLY in Book 1).
  4. Consensus : Expected net payoff (bps), disagreement penalty, Macro Breadth/SMA50 shield, and abstention gate.
  5. Execution : Whole shares, funded cash (0x leverage), pending-order cash reservations, exact NSE CNC/MIS fees,
                 and human-exclusive PIN interlock for Upstox live routing (`simulation_mode = True` default).
  6. Exits     : Scheduled 20d rebalance / 15:15 IST square-off + explicit structural risk stops.
"""

import os

ENGINE_VERSION = "CONSENSUS_V3_6LAYER_1.0"
PROTOCOL_ID = "CONSENSUS_SHADOW_FORWARD_V3"


def resolve_v3_db_path() -> str:
    """Resolves `market_data.db` path without forcing top-level `pandas` import in pure-stdlib test/audit runners."""
    env_p = os.environ.get("MARKET_DB_PATH")
    if env_p:
        return env_p
    try:
        from app.data.historical_data_layer import get_db_path
        return get_db_path()
    except Exception:
        backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        return os.path.join(backend_dir, "market_data.db")
