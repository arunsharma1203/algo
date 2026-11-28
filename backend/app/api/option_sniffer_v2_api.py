"""
V2 Option Sniffer (`option_sniffer_v2`) — Isolated FastAPI Router (`/api/option-sniffer-advance-v2/*`).

Safeguards:
  - Read-only `GET` endpoints strictly query state and NEVER start the collector, add
    subscriptions, run background writes, or mutate SQLite tables.
  - Paper-only lifecycle controls (`POST /control/*`) manage only V2's passive listener & replay;
    zero live order placement, broker modification/cancellation, mirroring, or arm/disarm endpoints exist.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

try:
    from fastapi import APIRouter, Body, HTTPException, Query

    _HAS_FASTAPI = True
except ImportError:  # Allows stdlib `/usr/bin/python3 -B` tests without FastAPI installed
    _HAS_FASTAPI = False

    def Query(default: Any = None, **_: Any) -> Any:  # type: ignore
        return default

    def Body(default: Any = None, **_: Any) -> Any:  # type: ignore
        return default

    class HTTPException(Exception):  # type: ignore
        def __init__(self, status_code: int, detail: str) -> None:
            self.status_code = status_code
            self.detail = detail
            super().__init__(detail)

    class APIRouter:  # type: ignore
        def __init__(self, prefix: str = "", tags: List[str] | None = None) -> None:
            self.prefix = prefix
            self.tags = tags or []
            self.routes: List[Dict[str, Any]] = []

        def _add_route(self, method: str, path: str, fn: Callable[..., Any]) -> Callable[..., Any]:
            self.routes.append({"method": method, "path": f"{self.prefix}{path}", "endpoint": fn})
            return fn

        def get(self, path: str, **_: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            return lambda fn: self._add_route("GET", path, fn)

        def post(self, path: str, **_: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            return lambda fn: self._add_route("POST", path, fn)


from app.analytics.option_sniffer_v2.v2_service import get_option_sniffer_v2_service

router = APIRouter(
    prefix="/api/option-sniffer-advance-v2",
    tags=["Option Sniffer Advance V2 (Paper Only)"],
)


def _q_bool(val: Any) -> bool:
    raw = getattr(val, "default", val)
    if isinstance(raw, bool):
        return raw
    return str(raw).lower() in ("1", "true", "yes")


def _q_float(val: Any, fallback: float) -> float:
    raw = getattr(val, "default", val)
    try:
        return float(raw)
    except Exception:
        return float(fallback)


@router.get("/overview")
def get_v2_overview(
    include_collateral: bool = Query(False),
    collateral_yield_pa: float = Query(6.0),
    haircut_pct: float = Query(10.0),
) -> Dict[str, Any]:
    """Read-only V2 paper overview, hygiene telemetry, Book A/B status, gates, and optional collateral carry."""
    svc = get_option_sniffer_v2_service()
    return svc.get_overview(
        include_collateral=_q_bool(include_collateral),
        collateral_yield_pa=_q_float(collateral_yield_pa, 6.0),
        haircut_pct=_q_float(haircut_pct, 10.0),
    )


@router.get("/positions")
def get_v2_positions() -> Dict[str, Any]:
    """Read-only active/pending/unresolved exposure and reconciled closed paper ledger."""
    return get_option_sniffer_v2_service().get_positions()


@router.get("/daily-comparison")
def get_v2_daily_comparison() -> Dict[str, Any]:
    """Read-only daily V1 vs V2 comparison keeping ARCHIVE_REPLAY and FORWARD_PAPER separated."""
    return get_option_sniffer_v2_service().get_daily_comparison()


@router.get("/replay-audit")
def get_v2_replay_audit() -> Dict[str, Any]:
    """Read-only Phase-4 O3 reproduction, 6-conflict table, Book B overshoot, and single-factor ablation."""
    return get_option_sniffer_v2_service().get_replay_audit()


@router.get("/counterfactuals")
def get_v2_counterfactuals() -> Dict[str, Any]:
    """Read-only isolated V1 counterfactuals (`included_in_v2_totals = 0`)."""
    return get_option_sniffer_v2_service().get_counterfactuals()


@router.get("/diagnostics")
def get_v2_diagnostics() -> Dict[str, Any]:
    """Read-only isolation boundary, Upstox field inventory, protected hashes, and feed diagnostics."""
    return get_option_sniffer_v2_service().get_diagnostics()


@router.get("/opportunities")
def get_v2_opportunities(
    session_date: Any = Query(None),
    book_id: Any = Query(None),
    limit: Any = Query(200),
) -> Dict[str, Any]:
    """Read-only accepted & rejected opportunity evaluations with gate inputs and quote provenance."""
    sd_raw = getattr(session_date, "default", session_date)
    bid_raw = getattr(book_id, "default", book_id)
    lim_raw = getattr(limit, "default", limit)
    try:
        lim_i = int(lim_raw)
    except Exception:
        lim_i = 200
    return get_option_sniffer_v2_service().get_opportunities(
        session_date=str(sd_raw) if sd_raw else None,
        book_id=str(bid_raw) if bid_raw else None,
        limit=max(1, min(1000, lim_i)),
    )


@router.post("/control/start-collector")
def post_v2_start_collector() -> Dict[str, Any]:
    """Explicit operator action to attach V2 passive listener to shared OptionStreamManager (paper only)."""
    return get_option_sniffer_v2_service().start_parallel_collector(run_archive_warmup=False)


@router.post("/control/run-archive-replay")
def post_v2_run_archive_replay() -> Dict[str, Any]:
    """Explicit operator action to populate deterministic V2 archive replay & V1 counterfactuals."""
    res = get_option_sniffer_v2_service().run_and_persist_archive_audit()
    return {
        "replay_status": res["replay_status"],
        "canonical_replay_sha256": res["replay_and_attribution"]["canonical_replay"]["ledger_sha256"],
        "replay_stats": res["replay_and_attribution"]["canonical_replay"]["stats"],
        "cf1_summary": res["counterfactuals"]["cf1_summary"],
        "cf2_summary": res["counterfactuals"]["cf2_summary"],
    }


@router.post("/control/shutdown-v2-only")
def post_v2_shutdown_only() -> Dict[str, Any]:
    """Explicit operator action to detach ONLY V2 passive listener and V2-owned pins without stopping V1."""
    return get_option_sniffer_v2_service().stop_v2_only()


@router.post("/control/reconcile-booking-fault")
def post_v2_reconcile_booking_fault() -> Dict[str, Any]:
    """Explicit operator action to reconcile paper ledger and clear latched booking fault (paper only)."""
    return get_option_sniffer_v2_service().reconcile_booking_fault()


@router.post("/control/paper-mode")
def post_v2_paper_mode(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """Explicit operator control to route V2 Paper evaluation (`AUTO`, `BOOK_A_ONLY`, `BOOK_B_ONLY`)."""
    raw = getattr(payload, "default", payload)
    mode = str((raw or {}).get("paper_mode") or "").upper().strip()
    try:
        return get_option_sniffer_v2_service().set_paper_execution_mode(mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
