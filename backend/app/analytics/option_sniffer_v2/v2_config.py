"""
V2 Option Sniffer — Frozen Configuration, Hard Paper-Only Invariants & Holiday Calendar.

Isolation guarantees:
  - Strictly decoupled from V1 (`option_sniffer`, `option_sniffer_advance`, `option_sniffer_advance_s205`,
    `option_sniffer_advance_live`).
  - Zero broker order, modification, cancellation, mirroring, or live-arm imports or toggles.
  - Enforces `simulation_mode is True` and `execution_adapter == "LOCAL_PAPER_SIMULATOR_ONLY"` at load time.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from typing import Any, Dict, Iterable, Optional, Set, Tuple
try:
    from zoneinfo import ZoneInfo

    IST_TZ = ZoneInfo("Asia/Kolkata")
except Exception:
    IST_TZ = timezone(timedelta(hours=5, minutes=30))

_V2_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_V2_PREREG_PATH = os.path.join(_V2_DIR, "V2_PREREGISTRATION_V1.json")

# Inventory of pre-existing identifiers in the codebase containing "V2" to prove zero collision.
EXISTING_V2_SYSTEMS_INVENTORY: Tuple[Dict[str, str], ...] = (
    {
        "identifier": "consensus_meta_learner_v2 / consensus_meta_trainer_v2",
        "path": "backend/app/analytics/consensus_meta_learner_v2.py",
        "domain": "Equity Consensus Stacking Meta-Learner",
    },
    {
        "identifier": "discovery_v2_ledger / signal_discovery_v2_engine / QlibDiscoveryV2Lab",
        "path": "backend/app/analytics/qlib_discovery/discovery_v2_ledger.py",
        "domain": "Qlib Discovery V2 Research Lab",
    },
    {
        "identifier": "HISTORICAL_LEGACY_V2 / V2.0 docstrings",
        "path": "backend/app/analytics/option_sniffer_advance/",
        "domain": "Retired Advance Paper Cohort (Nov 2-13, 2026)",
    },
    {
        "identifier": "UPSTOX_BASE_URL (/v2) & UPSTOX_V2_CHARGES_MARGIN_VERIFIED",
        "path": "backend/app/data/upstox_provider.py",
        "domain": "Upstox REST API v2 Transport",
    },
)

# Observed NSE weekday holidays & Muhurat short sessions (2024-2026) so trading-session DTE
# adjusts accurately across exchange holidays (including 2026-10-20, 2026-11-10, 2026-11-24
# empirically verified absent from continuous_index_ohlcv).
NSE_WEEKDAY_HOLIDAYS: frozenset[str] = frozenset(
    {
        # 2024
        "2024-01-26", "2024-03-08", "2024-03-25", "2024-03-29", "2024-04-11",
        "2024-04-17", "2024-05-01", "2024-05-20", "2024-06-17", "2024-07-17",
        "2024-08-15", "2024-10-02", "2024-11-01", "2024-11-15", "2024-11-20", "2024-12-25",
        # 2025
        "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10", "2025-04-14",
        "2025-04-18", "2025-05-01", "2025-08-15", "2025-08-27", "2025-10-02",
        "2025-10-21", "2025-10-22", "2025-11-05", "2025-12-25",
        # 2026
        "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
        "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14",
        "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
    }
)

SHORT_MUHURAT_DATES: frozenset[str] = frozenset(
    {"2023-11-12", "2024-03-02", "2024-05-18", "2024-11-01", "2025-10-21", "2026-11-08"}
)

NIFTY_LOT_SIZE_BY_EXPIRY_CUTOFF = (
    # NSE NIFTY lot size history for historical fidelity (75 up to Dec 2025 monthly, 65 afterward)
    ("2025-12-30", 75),
    ("9999-12-31", 65),
)


class V2PaperOnlyInvariantError(RuntimeError):
    """Raised whenever any configuration or caller violates V2's strict paper-only boundary."""


def canonicalize_v2_config(cfg: Dict[str, Any]) -> str:
    """Returns canonical sorted-key compact JSON representation for deterministic hashing."""
    return json.dumps(cfg, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_v2_config_hash(cfg: Dict[str, Any]) -> str:
    """Returns SHA-256 hex digest of canonicalized configuration."""
    return hashlib.sha256(canonicalize_v2_config(cfg).encode("utf-8")).hexdigest()


def validate_v2_paper_invariants(cfg: Dict[str, Any]) -> None:
    """Fail-closed guard ensuring no configuration flag can ever make V2 live-capable."""
    if not isinstance(cfg, dict):
        raise V2PaperOnlyInvariantError("V2 config must be a dictionary.")
    if cfg.get("simulation_mode") is not True:
        raise V2PaperOnlyInvariantError("V2 invariant breach: simulation_mode must be strictly True.")
    if cfg.get("live_capable_by_config") is not False:
        raise V2PaperOnlyInvariantError("V2 invariant breach: live_capable_by_config must be strictly False.")
    if cfg.get("execution_adapter") != "LOCAL_PAPER_SIMULATOR_ONLY":
        raise V2PaperOnlyInvariantError(
            "V2 invariant breach: execution_adapter must be 'LOCAL_PAPER_SIMULATOR_ONLY'."
        )
    if cfg.get("naked_short_allowed") is not False:
        raise V2PaperOnlyInvariantError("V2 invariant breach: naked_short_allowed must be False.")
    if cfg.get("allow_live_arm") is not False or cfg.get("allow_broker_orders") is not False:
        raise V2PaperOnlyInvariantError("V2 invariant breach: live arming and broker orders are forbidden.")


def load_v2_preregistration(path: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
    """Loads, validates, and returns `(config_dict, canonical_sha256)`."""
    target = path or DEFAULT_V2_PREREG_PATH
    with open(target, "rb") as fh:
        raw = fh.read()
    cfg = json.loads(raw.decode("utf-8"))
    validate_v2_paper_invariants(cfg)
    return cfg, compute_v2_config_hash(cfg)


def is_nse_trading_session(
    date_s: str,
    observed_sessions: Optional[Set[str]] = None,
    extra_holidays: Optional[Iterable[str]] = None,
) -> bool:
    """
    Returns True if `date_s` (YYYY-MM-DD) is a regular NSE trading session.
    Excludes weekends, SHORT_MUHURAT_DATES, and configured exchange holidays.
    """
    d_norm = str(date_s)[:10]
    if observed_sessions and d_norm in observed_sessions:
        return d_norm not in SHORT_MUHURAT_DATES
    dt_obj = datetime.strptime(d_norm, "%Y-%m-%d").date()
    if dt_obj.weekday() >= 5:
        return False
    holidays = set(NSE_WEEKDAY_HOLIDAYS)
    if extra_holidays:
        holidays.update(str(x)[:10] for x in extra_holidays)
    if d_norm in holidays or d_norm in SHORT_MUHURAT_DATES:
        return False
    return True


def trading_sessions_between(
    date_s: str,
    expiry_s: str,
    observed_sessions: Optional[Set[str]] = None,
    extra_holidays: Optional[Iterable[str]] = None,
) -> int:
    """
    Counts remaining regular NSE trading sessions strictly after `date_s` up to `expiry_s`.
    Returns `0` on `expiry_s` (or past expiry), `1` on the actual exchange session
    immediately preceding the contract's expiry session (`T-1` holiday-adjusted).
    """
    d0_s = str(date_s)[:10]
    d1_s = str(expiry_s)[:10]
    if d1_s <= d0_s:
        return 0
    if observed_sessions and max(observed_sessions) >= d1_s:
        return sum(
            1
            for s in observed_sessions
            if d0_s < s <= d1_s and s not in SHORT_MUHURAT_DATES
        )
    d0 = datetime.strptime(d0_s, "%Y-%m-%d").date()
    d1 = datetime.strptime(d1_s, "%Y-%m-%d").date()
    holidays = set(NSE_WEEKDAY_HOLIDAYS)
    if extra_holidays:
        holidays.update(str(x)[:10] for x in extra_holidays)
    cur = d0 + timedelta(days=1)
    cnt = 0
    while cur <= d1:
        cs = cur.isoformat()
        if cur.weekday() < 5 and cs not in holidays and cs not in SHORT_MUHURAT_DATES:
            cnt += 1
        cur += timedelta(days=1)
    return cnt


def resolve_contract_lot_size(expiry_s: str, contract_meta_lot: Optional[int] = None) -> int:
    """Uses explicit contract metadata lot_size when valid (>0), else historical expiry schedule."""
    if contract_meta_lot is not None and not isinstance(contract_meta_lot, bool):
        try:
            il = int(contract_meta_lot)
            if il > 0:
                return il
        except (TypeError, ValueError):
            pass
    exp_d = str(expiry_s)[:10]
    for cutoff_d, lot_sz in NIFTY_LOT_SIZE_BY_EXPIRY_CUTOFF:
        if exp_d <= cutoff_d:
            return int(lot_sz)
    return 65


def to_ist_aware(dt_val: Optional[datetime] = None) -> datetime:
    """Returns timezone-aware Asia/Kolkata datetime."""
    if dt_val is None:
        return datetime.now(IST_TZ)
    if getattr(dt_val, "tzinfo", None) is None:
        return dt_val.replace(tzinfo=IST_TZ)
    return dt_val.astimezone(IST_TZ)


def to_ist_naive(dt_val: Optional[datetime] = None) -> datetime:
    return to_ist_aware(dt_val).replace(tzinfo=None)


def parse_ist_timestamp_ms(ts_str: str) -> int:
    """Parses ISO or 'YYYY-MM-DD HH:MM:SS(.ffffff)' as explicit Asia/Kolkata epoch-ms."""
    raw = str(ts_str or "").strip()
    if not raw:
        raise ValueError("Empty timestamp string")
    norm = raw.replace(" ", "T")
    dt = datetime.fromisoformat(norm)
    return int(to_ist_aware(dt).timestamp() * 1000)


def format_ist_iso(ts_ms: int) -> str:
    """Formats epoch-ms cleanly as ISO-8601 Asia/Kolkata string (+05:30)."""
    dt = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=IST_TZ)
    return dt.isoformat()


def ist_hhmm_from_ms(ts_ms: int) -> int:
    dt = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=IST_TZ)
    return dt.hour * 100 + dt.minute


def bar_open_label_from_ms(ts_ms: int) -> Tuple[str, str, int, int, int]:
    """
    Given event epoch-ms, returns the bar [bar_open_ms, bar_close_ms) in IST:
    `(session_date, bar_open_ts_str, bar_open_ms, bar_close_ms, bar_open_hhmm)`.
    """
    dt = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=IST_TZ)
    dt_floor = dt.replace(second=0, microsecond=0)
    bar_open_ms = int(dt_floor.timestamp() * 1000)
    bar_close_ms = bar_open_ms + 60_000
    return (
        dt_floor.strftime("%Y-%m-%d"),
        dt_floor.strftime("%Y-%m-%d %H:%M:00"),
        bar_open_ms,
        bar_close_ms,
        dt_floor.hour * 100 + dt_floor.minute,
    )
