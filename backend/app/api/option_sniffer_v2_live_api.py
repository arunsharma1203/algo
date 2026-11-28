"""Isolated FastAPI Router for Option Sniffer Advance V2 Live Execution Control.

Prefix: `/api/option-sniffer-advance-v2-live`

Invariants:
- Zero overlap with V2 paper routes (`/api/option-sniffer-advance-v2/*`) or V1 live routes.
- Always boots disarmed; read-only GET endpoints never create a cold DB or mutate state.
- Live arm/disarm belongs exclusively to human operators supplying PIN + explicit confirmation.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

try:
    from fastapi import APIRouter, Body, HTTPException, Query

    _HAS_FASTAPI = True
except ImportError:  # Enables stdlib `/usr/bin/python3 -B` isolated tests without FastAPI installed
    _HAS_FASTAPI = False

    def Body(default: Any = None, **_: Any) -> Any:  # type: ignore
        return default

    def Query(default: Any = None, **_: Any) -> Any:  # type: ignore
        return default

    class HTTPException(Exception):  # type: ignore
        def __init__(self, status_code: int, detail: Any = "") -> None:
            self.status_code = status_code
            self.detail = detail
            super().__init__(f"HTTP {status_code}: {detail}")

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


from app.analytics.option_sniffer_v2_live.v2_live_execution_engine import (
    get_option_sniffer_v2_live_engine,
)

router = APIRouter(
    prefix="/api/option-sniffer-advance-v2-live",
    tags=["Option Sniffer Advance V2 Live"],
)


@router.get("/status")
def get_v2_live_status(include_paper_baseline: bool = Query(default=True)) -> Dict[str, Any]:
    return get_option_sniffer_v2_live_engine().get_status(include_paper_baseline=include_paper_baseline)


@router.get("/positions")
def get_v2_live_positions() -> Dict[str, Any]:
    return get_option_sniffer_v2_live_engine().get_positions()


@router.get("/audit-log")
def get_v2_live_audit_log(limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
    return get_option_sniffer_v2_live_engine().get_audit_log(limit=limit)


@router.post("/arm")
def post_v2_live_arm(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    engine = get_option_sniffer_v2_live_engine()
    try:
        return engine.arm_live(
            pin=str(payload.get("pin") or ""),
            human_confirmed=bool(payload.get("human_confirmed", False)),
            selected_books=list(payload.get("selected_books") or []),
            live_book_scope=payload.get("live_book_scope"),
            protocol_profile_id=str(payload.get("protocol_profile_id") or "V2_4_PAPER_DUAL_BOOK"),
            max_lots_per_book=int(payload.get("max_lots_per_book") or 1),
            max_daily_loss_inr=float(payload.get("max_daily_loss_inr") or 2500.0),
            max_open_packages=int(payload.get("max_open_packages") or 1),
            actor="HUMAN_UI",
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/disarm")
def post_v2_live_disarm(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    body = payload or {}
    return get_option_sniffer_v2_live_engine().disarm_live(
        reason=str(body.get("reason") or "HUMAN_UI_DISARM"),
        actor="HUMAN_UI",
    )


@router.post("/sos-flatten")
def post_v2_live_sos_flatten(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    body = payload or {}
    return get_option_sniffer_v2_live_engine().sos_flatten_all_and_disarm(
        reason=str(body.get("reason") or "OPERATOR_SOS_FLATTEN"),
        actor="HUMAN_UI_SOS",
    )


@router.post("/reconcile")
def post_v2_live_reconcile() -> Dict[str, Any]:
    return get_option_sniffer_v2_live_engine().reconcile_with_broker(actor="HUMAN_UI_RECONCILE")


@router.post("/positions/{live_package_id}/exit")
def post_v2_live_exit_position(
    live_package_id: str,
    payload: Optional[Dict[str, Any]] = Body(default=None),
) -> Dict[str, Any]:
    body = payload or {}
    try:
        return get_option_sniffer_v2_live_engine().exit_live_package(
            live_package_id=live_package_id,
            reason=str(body.get("reason") or "HUMAN_UI_SQUARE_OFF"),
            exit_price=float(body["exit_price"]) if body.get("exit_price") is not None else None,
            short_exit_price=float(body["short_exit_price"]) if body.get("short_exit_price") is not None else None,
            actor="HUMAN_UI",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
