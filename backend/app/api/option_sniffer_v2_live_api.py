"""Isolated FastAPI Router for Option Sniffer Advance V2 Live Execution Control.

Prefix: `/api/option-sniffer-advance-v2-live`

Invariants:
- Zero overlap with V2 paper routes (`/api/option-sniffer-advance-v2/*`) or V1 live routes.
- Always boots disarmed; read-only GET endpoints never create a cold DB or mutate state.
- Live arm/disarm belongs exclusively to human operators supplying PIN + explicit confirmation.
"""

from __future__ import annotations

import os
import secrets
from typing import Any, Callable, Dict, List, Optional

try:
    from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

    _HAS_FASTAPI = True
except ImportError:  # Enables stdlib `/usr/bin/python3 -B` isolated tests without FastAPI installed
    _HAS_FASTAPI = False
    Request = Any  # type: ignore

    def Depends(dependency: Any = None, **_: Any) -> Any:  # type: ignore
        return dependency

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
    OPEN_LIKE_PACKAGE_STATES,
    get_option_sniffer_v2_live_engine,
)

router = APIRouter(
    prefix="/api/option-sniffer-advance-v2-live",
    tags=["Option Sniffer Advance V2 Live"],
)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_LOCAL_ORIGIN_PREFIXES = (
    "http://localhost:",
    "http://127.0.0.1:",
    "http://[::1]:",
    "https://localhost:",
    "https://127.0.0.1:",
    "http://localhost/",
    "http://127.0.0.1/",
)


def _redact_http_pin(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            k: _redact_http_pin(v)
            for k, v in payload.items()
            if k != "current_pin"
        }
    if isinstance(payload, list):
        return [_redact_http_pin(item) for item in payload]
    return payload


def verify_v2_live_operator(request: Request) -> Dict[str, Any]:
    """
    Server-side operator boundary for V2 live mutating routes & arm-challenge:
      1. Loopback-only client IP check (including X-Forwarded-For if set).
      2. Same-host Origin/Referer check to block cross-site browser CSRF.
      3. Custom `X-V2-Operator-Intent: confirmed` header (forces CORS preflight & intent).
      4. Constant-time `X-V2-Operator-Token` check whenever `V2_LIVE_OPERATOR_TOKEN` env is configured.
    """
    if request is None:
        raise HTTPException(status_code=403, detail="FORBIDDEN_MISSING_REQUEST_CONTEXT")
    client = getattr(request, "client", None)
    client_host = str(getattr(client, "host", "") or "").strip().lower()
    if not client_host or client_host not in _LOOPBACK_HOSTS:
        raise HTTPException(status_code=403, detail="FORBIDDEN_NON_LOOPBACK_CLIENT")
    headers = getattr(request, "headers", None)
    if headers is None:
        raise HTTPException(status_code=403, detail="FORBIDDEN_MISSING_HEADERS")
    xff = str(headers.get("x-forwarded-for") or "").strip().lower()
    if xff:
        first_hop = xff.split(",")[0].strip()
        if first_hop not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="FORBIDDEN_FORWARDED_NON_LOOPBACK_CLIENT")
    origin = str(headers.get("origin") or "").strip().lower()
    if origin and origin not in ("http://localhost", "http://127.0.0.1") and not origin.startswith(_LOCAL_ORIGIN_PREFIXES):
        raise HTTPException(status_code=403, detail="FORBIDDEN_CROSS_ORIGIN_LIVE_CONTROL")
    referer = str(headers.get("referer") or "").strip().lower()
    if referer and not referer.startswith(_LOCAL_ORIGIN_PREFIXES):
        raise HTTPException(status_code=403, detail="FORBIDDEN_CROSS_ORIGIN_LIVE_CONTROL")
    intent = str(headers.get("x-v2-operator-intent") or "").strip().lower()
    if intent != "confirmed":
        raise HTTPException(status_code=403, detail="MISSING_OPERATOR_INTENT_HEADER")
    expected_tok = str(os.environ.get("V2_LIVE_OPERATOR_TOKEN") or "").strip()
    if expected_tok:
        provided_tok = str(headers.get("x-v2-operator-token") or "").strip()
        if not provided_tok or not secrets.compare_digest(provided_tok, expected_tok):
            raise HTTPException(status_code=403, detail="INVALID_OPERATOR_TOKEN")
    return {
        "authorized": True,
        "client_host": client_host or "loopback",
        "token_enforced": bool(expected_tok),
    }


@router.get("/status")
def get_v2_live_status(include_paper_baseline: bool = Query(default=True)) -> Dict[str, Any]:
    return get_option_sniffer_v2_live_engine().get_status(
        include_paper_baseline=include_paper_baseline,
        include_pin=False,
    )


@router.get("/funds-preview")
def get_v2_live_funds_preview(force_refresh: bool = Query(default=False)) -> Dict[str, Any]:
    raw = getattr(force_refresh, "default", force_refresh)
    fr = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes")
    return get_option_sniffer_v2_live_engine().get_funds_preview(force_refresh=fr)


@router.get("/positions")
def get_v2_live_positions() -> Dict[str, Any]:
    return get_option_sniffer_v2_live_engine().get_positions()


@router.get("/audit-log")
def get_v2_live_audit_log(limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
    return get_option_sniffer_v2_live_engine().get_audit_log(limit=limit)


@router.post("/arm-challenge")
def post_v2_live_arm_challenge(
    _operator: Dict[str, Any] = Depends(verify_v2_live_operator),
) -> Dict[str, Any]:
    return get_option_sniffer_v2_live_engine().get_operator_arm_challenge()


@router.post("/arm")
def post_v2_live_arm(
    payload: Dict[str, Any] = Body(default={}),
    _operator: Dict[str, Any] = Depends(verify_v2_live_operator),
) -> Dict[str, Any]:
    engine = get_option_sniffer_v2_live_engine()
    try:
        res = engine.arm_live(
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
        return _redact_http_pin(res)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/disarm")
def post_v2_live_disarm(
    payload: Optional[Dict[str, Any]] = Body(default=None),
    _operator: Dict[str, Any] = Depends(verify_v2_live_operator),
) -> Dict[str, Any]:
    body = payload or {}
    res = get_option_sniffer_v2_live_engine().disarm_live(
        reason=str(body.get("reason") or "HUMAN_UI_DISARM"),
        actor="HUMAN_UI",
    )
    return _redact_http_pin(res)


@router.post("/sos-flatten")
def post_v2_live_sos_flatten(
    payload: Optional[Dict[str, Any]] = Body(default=None),
    _operator: Dict[str, Any] = Depends(verify_v2_live_operator),
) -> Dict[str, Any]:
    body = payload or {}
    res = get_option_sniffer_v2_live_engine().sos_flatten_all_and_disarm(
        reason=str(body.get("reason") or "OPERATOR_SOS_FLATTEN"),
        actor="HUMAN_UI_SOS",
    )
    return _redact_http_pin(res)


@router.post("/reconcile")
def post_v2_live_reconcile(
    _operator: Dict[str, Any] = Depends(verify_v2_live_operator),
) -> Dict[str, Any]:
    return _redact_http_pin(
        get_option_sniffer_v2_live_engine().reconcile_with_broker(actor="HUMAN_UI_RECONCILE")
    )


@router.post("/positions/{live_package_id}/exit")
def post_v2_live_exit_position(
    live_package_id: str,
    payload: Optional[Dict[str, Any]] = Body(default=None),
    _operator: Dict[str, Any] = Depends(verify_v2_live_operator),
) -> Dict[str, Any]:
    body = payload or {}
    try:
        res = get_option_sniffer_v2_live_engine().exit_live_package(
            live_package_id=live_package_id,
            reason=str(body.get("reason") or "HUMAN_UI_SQUARE_OFF"),
            exit_price=float(body["exit_price"]) if body.get("exit_price") is not None else None,
            short_exit_price=float(body["short_exit_price"]) if body.get("short_exit_price") is not None else None,
            actor="HUMAN_UI",
        )
        return _redact_http_pin(res)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


if _HAS_FASTAPI:

    @router.on_event("startup")
    def _on_v2_live_startup() -> None:
        try:
            from app.data.database import is_testing_environment

            if is_testing_environment():
                return
            eng = get_option_sniffer_v2_live_engine()
            if eng.store.db_exists() and eng.store.list_packages(states=OPEN_LIKE_PACKAGE_STATES):
                eng.reconcile_with_broker(actor="STARTUP_BOOT_RECONCILE")
        except Exception:
            pass
