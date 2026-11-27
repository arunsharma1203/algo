"""Upstox V3/V2 Broker Client and Deterministic Mock Client for V2 Live Execution.

Verified Upstox Official API Semantics (https://upstox.com/developer/api-documentation/api-overview/):
- Orders (HFT V3): POST https://api-hft.upstox.com/v3/order/place, PUT /v3/order/modify, DELETE /v3/order/cancel
- Market Protection: ONLY valid on `MARKET` / `SL-M` (`-1` auto or `1..25` percent). Ignored/omitted on `LIMIT` / `SL`.
- GTT (V3): POST/PUT/DELETE https://api.upstox.com/v3/order/gtt/{place,modify,cancel}, GET /v3/order/gtt.
  IMPORTANT: GET /v3/order/gtt excludes completed/triggered GTT rules, and DELETE /v3/order/gtt/cancel ack
  does not prove no child order already fired -> callers MUST reconcile order book + positions before placing
  another exit order.
- Order/Position Reconciliation (V2): GET /v2/order/retrieve-all, GET /v2/order/history,
  GET /v2/portfolio/short-term-positions, GET /v2/user/get-funds-and-margin, POST /v2/charges/margin.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Protocol, Set

try:
    import requests
except ImportError:  # Pure stdlib `/usr/bin/python3 -B` test isolation
    requests = None  # type: ignore

from app.analytics.common.options_accounting import round_to_nse_tick

logger = logging.getLogger(__name__)

UPSTOX_HFT_BASE_URL = "https://api-hft.upstox.com/v3"
UPSTOX_V3_BASE_URL = "https://api.upstox.com/v3"
UPSTOX_V2_BASE_URL = "https://api.upstox.com/v2"

UPSTOX_VERIFIED_API_FACTS: Dict[str, Any] = {
    "hft_orders": {
        "place": f"{UPSTOX_HFT_BASE_URL}/order/place",
        "modify": f"{UPSTOX_HFT_BASE_URL}/order/modify",
        "cancel": f"{UPSTOX_HFT_BASE_URL}/order/cancel",
        "tag_constraints": "Alphanumeric and hyphens only, max 20 characters (no underscores/spaces).",
        "market_protection_rule": "Applies ONLY to MARKET and SL-M orders (-1 auto or 1..25%). Stripped on LIMIT and SL.",
    },
    "v3_gtt": {
        "place": f"{UPSTOX_V3_BASE_URL}/order/gtt/place",
        "modify": f"{UPSTOX_V3_BASE_URL}/order/gtt/modify",
        "cancel": f"{UPSTOX_V3_BASE_URL}/order/gtt/cancel",
        "list": f"{UPSTOX_V3_BASE_URL}/order/gtt",
        "race_hazard": "GET /v3/order/gtt excludes triggered/completed rules and cancel ack does not prove no child order triggered; always verify get_orders() and get_positions() before manual exit.",
    },
    "v2_reconciliation": {
        "orders": f"{UPSTOX_V2_BASE_URL}/order/retrieve-all",
        "order_history": f"{UPSTOX_V2_BASE_URL}/order/history",
        "positions": f"{UPSTOX_V2_BASE_URL}/portfolio/short-term-positions",
        "funds": f"{UPSTOX_V2_BASE_URL}/user/get-funds-and-margin",
        "margin": f"{UPSTOX_V2_BASE_URL}/charges/margin",
    },
    "portfolio_stream_ws": "wss://api.upstox.com/v2/feed/portfolio-stream-feed?update_types=order,gtt_order,position",
}


class BrokerTimeoutAmbiguousError(RuntimeError):
    """Raised when an HTTP order call times out or disconnects — order outcome at broker is UNKNOWN."""


class BrokerOrderRejectedError(RuntimeError):
    """Raised when broker returns an explicit business rejection (order definitely NOT accepted)."""


def sanitize_upstox_tag(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9-]", "-", str(raw or "OSV2"))
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not cleaned:
        cleaned = "OSV2"
    return cleaned[:20]


def normalize_market_protection(order_type: str, market_protection: Optional[float]) -> Optional[float]:
    ot = str(order_type or "").upper()
    if ot not in ("MARKET", "SL-M"):
        return None
    if market_protection is None:
        return -1.0
    val = float(market_protection)
    if val == -1.0:
        return -1.0
    if 1.0 <= val <= 25.0:
        return round(val, 2)
    raise ValueError(f"Upstox market_protection must be -1 or 1..25% for {ot}, got {market_protection}")


class V2BrokerClientProtocol(Protocol):
    def get_funds_and_margin(self) -> Dict[str, Any]: ...
    def calculate_margin(self, instruments: List[Dict[str, Any]]) -> Dict[str, Any]: ...
    def get_positions(self) -> List[Dict[str, Any]]: ...
    def get_orders(self) -> List[Dict[str, Any]]: ...
    def get_gtt_orders(self) -> List[Dict[str, Any]]: ...
    def place_order(self, payload: Dict[str, Any], *, is_emergency: bool = False) -> Dict[str, Any]: ...
    def cancel_order(self, order_id: str, *, is_emergency: bool = True) -> Dict[str, Any]: ...
    def place_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...
    def modify_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...
    def cancel_gtt_order(self, gtt_id: str, *, is_emergency: bool = True) -> Dict[str, Any]: ...


def _resolve_optional_proxy() -> Optional[str]:
    env_proxy = (os.environ.get("UPSTOX_V2_PROXY_URL") or os.environ.get("UPSTOX_PROXY_URL") or "").strip()
    if env_proxy:
        return env_proxy
    try:
        from app.analytics.option_sniffer.option_live_adapter import DEFAULT_PROXY_URL

        return (DEFAULT_PROXY_URL or "").strip() or None
    except Exception as exc:
        logger.debug("Optional proxy lookup unavailable: %s", exc)
        return None


class UpstoxV2BrokerClient:
    """Production HTTP client for Upstox HFT V3 orders, V3 GTTs, and V2 reconciliation."""

    def __init__(self, *, timeout_sec: float = 6.0, proxy_url: Optional[str] = None):
        self.timeout_sec = float(timeout_sec)
        self.proxy_url = proxy_url if proxy_url is not None else _resolve_optional_proxy()

    def _headers(self) -> Dict[str, str]:
        from app.data.upstox_provider import get_upstox_algo_token

        token = get_upstox_algo_token()
        if not token:
            raise BrokerOrderRejectedError("Upstox algo token not configured.")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        is_emergency: bool = False,
        ambiguous_on_transport_error: bool = False,
    ) -> Dict[str, Any]:
        if requests is None:
            raise BrokerOrderRejectedError("The 'requests' package is required for live Upstox HTTP calls (run inside backend/venv).")
        from app.data.upstox_provider import get_upstox_limiter

        get_upstox_limiter().acquire(timeout=self.timeout_sec, is_emergency=is_emergency)
        proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=self._headers(),
                json=json_body,
                params=params,
                proxies=proxies,
                timeout=self.timeout_sec,
            )
        except (requests.Timeout, requests.ConnectionError, TimeoutError, ConnectionError) as exc:
            if ambiguous_on_transport_error:
                raise BrokerTimeoutAmbiguousError(
                    f"Transport error during {method} {url}; broker state is UNKNOWN: {exc}"
                ) from exc
            raise
        if resp.status_code >= 500 and ambiguous_on_transport_error:
            raise BrokerTimeoutAmbiguousError(
                f"HTTP {resp.status_code} during {method} {url}; broker state is UNKNOWN: {resp.text[:250]}"
            )
        try:
            payload = resp.json()
        except Exception:
            payload = {"status": "error", "raw_text": resp.text[:400]}
        if resp.status_code >= 400 or str(payload.get("status", "")).lower() == "error":
            raise BrokerOrderRejectedError(
                f"Upstox rejected {method} {url} ({resp.status_code}): {payload}"
            )
        return payload

    def get_funds_and_margin(self) -> Dict[str, Any]:
        return self._request("GET", f"{UPSTOX_V2_BASE_URL}/user/get-funds-and-margin")

    def calculate_margin(self, instruments: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"{UPSTOX_V2_BASE_URL}/charges/margin",
            json_body={"instruments": instruments},
        )

    def get_positions(self) -> List[Dict[str, Any]]:
        res = self._request("GET", f"{UPSTOX_V2_BASE_URL}/portfolio/short-term-positions")
        data = res.get("data")
        return list(data) if isinstance(data, list) else []

    def get_orders(self) -> List[Dict[str, Any]]:
        res = self._request("GET", f"{UPSTOX_V2_BASE_URL}/order/retrieve-all")
        data = res.get("data")
        return list(data) if isinstance(data, list) else []

    def get_gtt_orders(self) -> List[Dict[str, Any]]:
        res = self._request("GET", f"{UPSTOX_V3_BASE_URL}/order/gtt")
        data = res.get("data")
        return list(data) if isinstance(data, list) else []

    def place_order(self, payload: Dict[str, Any], *, is_emergency: bool = False) -> Dict[str, Any]:
        body = dict(payload)
        body["tag"] = sanitize_upstox_tag(body.get("tag", "OSV2"))
        mp = normalize_market_protection(body.get("order_type", "LIMIT"), body.get("market_protection"))
        if mp is None:
            body.pop("market_protection", None)
        else:
            body["market_protection"] = mp
        if body.get("price") is not None and float(body["price"]) > 0:
            body["price"] = round_to_nse_tick(float(body["price"]))
        if body.get("trigger_price") is not None and float(body["trigger_price"]) > 0:
            body["trigger_price"] = round_to_nse_tick(float(body["trigger_price"]))
        return self._request(
            "POST",
            f"{UPSTOX_HFT_BASE_URL}/order/place",
            json_body=body,
            is_emergency=is_emergency,
            ambiguous_on_transport_error=True,
        )

    def cancel_order(self, order_id: str, *, is_emergency: bool = True) -> Dict[str, Any]:
        return self._request(
            "DELETE",
            f"{UPSTOX_HFT_BASE_URL}/order/cancel",
            params={"order_id": order_id},
            is_emergency=is_emergency,
            ambiguous_on_transport_error=True,
        )

    def place_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"{UPSTOX_V3_BASE_URL}/order/gtt/place",
            json_body=payload,
            ambiguous_on_transport_error=True,
        )

    def modify_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request(
            "PUT",
            f"{UPSTOX_V3_BASE_URL}/order/gtt/modify",
            json_body=payload,
            ambiguous_on_transport_error=True,
        )

    def cancel_gtt_order(self, gtt_id: str, *, is_emergency: bool = True) -> Dict[str, Any]:
        return self._request(
            "DELETE",
            f"{UPSTOX_V3_BASE_URL}/order/gtt/cancel",
            json_body={"gtt_id": gtt_id},
            is_emergency=is_emergency,
            ambiguous_on_transport_error=True,
        )


class MockV2BrokerClient:
    """Deterministic in-memory broker simulator with fault-injection hooks for isolated testing."""

    def __init__(self, *, available_margin_inr: float = 200000.0):
        self._lock = threading.RLock()
        self.available_margin_inr = float(available_margin_inr)
        self._seq = 0
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._gtts: Dict[str, Dict[str, Any]] = {}
        self._positions: Dict[str, Dict[str, Any]] = {}
        self.call_log: List[Dict[str, Any]] = []

        # Fault-injection controls
        self.inject_timeout_order_applied: bool = False
        self.inject_timeout_order_not_received: bool = False
        self.inject_timeout_tags: Set[str] = set()
        self.inject_reject_instrument_keys: Set[str] = set()
        self.inject_partial_fill_ratio: Dict[str, float] = {}
        self.inject_unwind_fail: bool = False
        # Map of gtt_id (or "*") -> fill_price when triggered during cancel
        self.inject_gtt_triggers_during_cancel: Dict[str, float] = {}

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:06d}"

    def _record_fill_into_positions(
        self, instrument_key: str, trading_symbol: str, side: str, qty: int, price: float
    ) -> None:
        if qty <= 0:
            return
        cur = self._positions.get(instrument_key) or {
            "instrument_token": instrument_key,
            "trading_symbol": trading_symbol,
            "quantity": 0,
            "buy_quantity": 0,
            "sell_quantity": 0,
            "average_price": 0.0,
            "last_price": float(price),
        }
        cur_qty = int(cur["quantity"])
        if str(side).upper() == "BUY":
            cur["buy_quantity"] = int(cur["buy_quantity"]) + int(qty)
            new_qty = cur_qty + int(qty)
        else:
            cur["sell_quantity"] = int(cur["sell_quantity"]) + int(qty)
            new_qty = cur_qty - int(qty)
        cur["quantity"] = new_qty
        cur["last_price"] = float(price)
        if cur["average_price"] == 0.0:
            cur["average_price"] = float(price)
        self._positions[instrument_key] = cur

    def get_funds_and_margin(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "status": "success",
                "data": {
                    "equity": {
                        "available_margin": self.available_margin_inr,
                        "used_margin": 0.0,
                    }
                },
            }

    def calculate_margin(self, instruments: List[Dict[str, Any]]) -> Dict[str, Any]:
        with self._lock:
            total = 0.0
            for item in instruments:
                q = int(item.get("quantity", 0))
                p = float(item.get("price", 150.0) or 150.0)
                if str(item.get("transaction_type", "BUY")).upper() == "BUY":
                    total += q * p
                else:
                    total += q * 500.0
            return {"status": "success", "data": {"required_margin": round(total, 2)}}

    def get_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._positions.values()]

    def get_orders(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._orders.values()]

    def get_gtt_orders(self) -> List[Dict[str, Any]]:
        with self._lock:
            # Matches Upstox V3: completed/triggered GTTs are NOT returned
            return [
                dict(v)
                for v in self._gtts.values()
                if v.get("status") not in ("COMPLETED", "TRIGGERED", "CANCELLED")
            ]

    def place_order(self, payload: Dict[str, Any], *, is_emergency: bool = False) -> Dict[str, Any]:
        with self._lock:
            body = dict(payload)
            tag = sanitize_upstox_tag(body.get("tag", "OSV2"))
            ikey = str(body["instrument_token"])
            tsym = str(body.get("trading_symbol") or ikey)
            side = str(body["transaction_type"]).upper()
            qty = int(body["quantity"])
            order_type = str(body.get("order_type", "LIMIT")).upper()
            mp = normalize_market_protection(order_type, body.get("market_protection"))
            px = float(body.get("price") or 100.0)
            if px <= 0:
                px = 100.0
            self.call_log.append(
                {
                    "method": "place_order",
                    "instrument_token": ikey,
                    "side": side,
                    "qty": qty,
                    "tag": tag,
                    "market_protection": mp,
                    "is_emergency": is_emergency,
                }
            )

            # Unwind failure injection (triggered on emergency buy-wing unwind sell)
            if self.inject_unwind_fail and is_emergency and side == "SELL":
                raise BrokerOrderRejectedError("Injected broker rejection during buy-wing unwind.")

            if ikey in self.inject_reject_instrument_keys:
                raise BrokerOrderRejectedError(f"Injected broker rejection for {ikey}")

            tag_should_timeout = (not self.inject_timeout_tags) or (tag in self.inject_timeout_tags)
            if self.inject_timeout_order_not_received and tag_should_timeout:
                self.inject_timeout_order_not_received = False
                raise BrokerTimeoutAmbiguousError("Injected timeout: order NOT received by broker.")

            oid = self._next_id("ORD")
            ratio = float(self.inject_partial_fill_ratio.get(ikey, 1.0))
            filled_qty = int(qty * ratio)
            # Snap partial fills to NIFTY lot multiples when lot <= qty
            if 0 < filled_qty < qty and qty >= 65 and filled_qty >= 65:
                filled_qty = (filled_qty // 65) * 65
            status = "complete" if filled_qty == qty else ("open" if filled_qty > 0 else "rejected")

            order_rec = {
                "order_id": oid,
                "instrument_token": ikey,
                "trading_symbol": tsym,
                "transaction_type": side,
                "order_type": order_type,
                "quantity": qty,
                "filled_quantity": filled_qty,
                "average_price": round_to_nse_tick(px),
                "price": round_to_nse_tick(px),
                "status": status,
                "tag": tag,
                "market_protection": mp,
                "order_timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if filled_qty > 0:
                self._record_fill_into_positions(ikey, tsym, side, filled_qty, round_to_nse_tick(px))
            self._orders[oid] = order_rec

            if self.inject_timeout_order_applied and tag_should_timeout:
                self.inject_timeout_order_applied = False
                raise BrokerTimeoutAmbiguousError("Injected timeout AFTER broker accepted and filled order.")

            return {"status": "success", "data": {"order_ids": [oid], "order_id": oid, "order": order_rec}}

    def cancel_order(self, order_id: str, *, is_emergency: bool = True) -> Dict[str, Any]:
        with self._lock:
            self.call_log.append({"method": "cancel_order", "order_id": order_id})
            rec = self._orders.get(order_id)
            if rec and rec["status"] not in ("complete", "cancelled", "rejected"):
                rec["status"] = "cancelled"
            return {"status": "success", "data": {"order_id": order_id}}

    def place_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            gid = self._next_id("GTT")
            rec = dict(payload)
            rec["gtt_order_id"] = gid
            rec["status"] = "SCHEDULED"
            self._gtts[gid] = rec
            self.call_log.append({"method": "place_gtt_order", "gtt_order_id": gid})
            return {"status": "success", "data": {"gtt_order_ids": [gid], "gtt_order_id": gid}}

    def modify_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            gid = str(payload.get("gtt_id") or payload.get("gtt_order_id") or "")
            if gid in self._gtts:
                self._gtts[gid].update(payload)
            self.call_log.append({"method": "modify_gtt_order", "gtt_order_id": gid})
            return {"status": "success", "data": {"gtt_order_id": gid}}

    def cancel_gtt_order(self, gtt_id: str, *, is_emergency: bool = True) -> Dict[str, Any]:
        with self._lock:
            self.call_log.append({"method": "cancel_gtt_order", "gtt_order_id": gtt_id})
            rec = self._gtts.get(gtt_id)
            trig_px = self.inject_gtt_triggers_during_cancel.get(gtt_id) or self.inject_gtt_triggers_during_cancel.get("*")
            if rec and trig_px is not None:
                # Race: GTT triggered just before cancel executed, placing and filling a child SELL order
                ikey = str(rec["instrument_token"])
                qty = int(rec["quantity"])
                tsym = str(rec.get("trading_symbol") or ikey)
                fill_px = round_to_nse_tick(float(trig_px))
                oid = self._next_id("ORD_GTT")
                self._orders[oid] = {
                    "order_id": oid,
                    "instrument_token": ikey,
                    "trading_symbol": tsym,
                    "transaction_type": str(rec.get("transaction_type", "SELL")).upper(),
                    "order_type": "LIMIT",
                    "quantity": qty,
                    "filled_quantity": qty,
                    "average_price": fill_px,
                    "price": fill_px,
                    "status": "complete",
                    "tag": sanitize_upstox_tag(f"GTT-{gtt_id}"),
                    "parent_gtt_id": gtt_id,
                    "order_timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                self._record_fill_into_positions(
                    ikey, tsym, str(rec.get("transaction_type", "SELL")).upper(), qty, fill_px
                )
                rec["status"] = "TRIGGERED"
                return {"status": "success", "data": {"gtt_order_id": gtt_id, "message": "Cancel ack (race)"}}
            if rec:
                rec["status"] = "CANCELLED"
            return {"status": "success", "data": {"gtt_order_id": gtt_id}}
