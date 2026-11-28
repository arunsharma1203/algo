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
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple

try:
    import requests
except ImportError:  # Pure stdlib `/usr/bin/python3 -B` test isolation
    requests = None  # type: ignore

from app.analytics.common.options_accounting import round_to_nse_tick
from app.analytics.option_sniffer_v2_live.v2_live_ledger import now_ist_iso

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


def normalize_market_protection(order_type: str, market_protection: Optional[float]) -> Optional[int]:
    ot = str(order_type or "").upper()
    if ot not in ("MARKET", "SL-M"):
        return None
    if market_protection is None or isinstance(market_protection, bool):
        if isinstance(market_protection, bool):
            raise ValueError(f"Upstox market_protection must be int -1 or 1..25, got bool {market_protection}")
        return -1
    val = float(market_protection)
    if val == -1.0:
        return -1
    if 1.0 <= val <= 25.0:
        return int(round(val))
    raise ValueError(f"Upstox market_protection must be -1 or 1..25% for {ot}, got {market_protection}")


def compute_smart_limit_collar(ref_price: float, side: str) -> float:
    """Computes a tight 1.2% (min Rs 0.50) marketable LIMIT collar rounded to Rs 0.05 tick."""
    px = max(0.05, float(ref_price))
    buf = max(0.50, px * 0.012)
    if str(side or "").upper() == "BUY":
        return round_to_nse_tick(px + buf, mode="adverse_buy")
    return round_to_nse_tick(max(0.05, px - buf), mode="adverse_sell")


def prepare_upstox_v3_order_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Enforces exact Upstox HFT V3 /order/place wire semantics before HTTP or Mock submission."""
    body = dict(payload)
    ot = str(body.get("order_type", "LIMIT")).upper()
    body["order_type"] = ot
    body["tag"] = sanitize_upstox_tag(body.get("tag", "OSV2"))
    body["quantity"] = int(body["quantity"])
    mp = normalize_market_protection(ot, body.get("market_protection"))
    if mp is None:
        body.pop("market_protection", None)
    else:
        body["market_protection"] = int(mp)

    if ot == "MARKET":
        body["price"] = 0
        body["trigger_price"] = 0
    elif ot == "SL-M":
        body["price"] = 0
        if body.get("trigger_price") is not None and float(body["trigger_price"]) > 0:
            body["trigger_price"] = round_to_nse_tick(float(body["trigger_price"]))
    else:
        if body.get("price") is not None and float(body["price"]) > 0:
            body["price"] = round_to_nse_tick(float(body["price"]))
        else:
            body["price"] = 0.0
        if ot == "SL" and body.get("trigger_price") is not None and float(body["trigger_price"]) > 0:
            body["trigger_price"] = round_to_nse_tick(float(body["trigger_price"]))
        else:
            body["trigger_price"] = 0
    return body


def validate_upstox_v3_gtt_payload(payload: Dict[str, Any], *, mode: str = "place") -> Dict[str, Any]:
    """
    Validates & sanitizes official Upstox V3 GTT place/modify schema.
    Prevents UDAPI1141 (`SINGLE` GTT rejects `SL`/`TARGET` strategy & nested `limit_price`).
    """
    if not isinstance(payload, dict):
        raise BrokerOrderRejectedError("Upstox V3 GTT payload must be a dict.")
    body = dict(payload)
    gtt_type = str(body.get("type") or "").upper()
    if gtt_type not in ("SINGLE", "MULTIPLE"):
        raise BrokerOrderRejectedError(f"Upstox V3 GTT invalid type={gtt_type!r}; expected SINGLE or MULTIPLE.")
    qty = int(body.get("quantity") or 0)
    if qty <= 0:
        raise BrokerOrderRejectedError(f"Upstox V3 GTT quantity must be positive, got {qty}.")
    body["type"] = gtt_type
    body["quantity"] = qty

    if mode == "place":
        if not str(body.get("instrument_token") or "").strip():
            raise BrokerOrderRejectedError("Upstox V3 GTT place requires instrument_token.")
        prod = str(body.get("product") or "").upper()
        if prod not in ("D", "I"):
            raise BrokerOrderRejectedError(f"Upstox V3 GTT invalid product={prod!r}.")
        tx = str(body.get("transaction_type") or "").upper()
        if tx not in ("BUY", "SELL"):
            raise BrokerOrderRejectedError(f"Upstox V3 GTT invalid transaction_type={tx!r}.")
        body["product"] = prod
        body["transaction_type"] = tx
        # Upstox V3 GTT place schema does not take root `trading_symbol`
        body.pop("trading_symbol", None)
    elif mode == "modify":
        if not str(body.get("gtt_order_id") or "").strip():
            raise BrokerOrderRejectedError("Upstox V3 GTT modify requires gtt_order_id.")
        body["gtt_order_id"] = str(body["gtt_order_id"]).strip()

    rules = body.get("rules")
    if not isinstance(rules, list) or not rules:
        raise BrokerOrderRejectedError("Upstox V3 GTT requires non-empty rules list.")
    if gtt_type == "SINGLE" and len(rules) != 1:
        raise BrokerOrderRejectedError("Upstox V3 SINGLE GTT requires exactly 1 rule.")

    clean_rules: List[Dict[str, Any]] = []
    for raw_r in rules:
        if not isinstance(raw_r, dict):
            raise BrokerOrderRejectedError("Each Upstox V3 GTT rule must be a dict.")
        r = dict(raw_r)
        if "limit_price" in r:
            raise BrokerOrderRejectedError("Upstox V3 GTT rules do not support 'limit_price'.")
        strat = str(r.get("strategy") or "").upper()
        if gtt_type == "SINGLE" and strat != "ENTRY":
            raise BrokerOrderRejectedError(
                f"UDAPI1141: Upstox V3 SINGLE GTT strategy must be 'ENTRY', got {strat!r}."
            )
        if gtt_type == "MULTIPLE" and strat not in ("ENTRY", "TARGET", "STOPLOSS"):
            raise BrokerOrderRejectedError(f"Invalid MULTIPLE GTT strategy={strat!r}.")
        trig_t = str(r.get("trigger_type") or "").upper()
        if trig_t not in ("ABOVE", "BELOW", "IMMEDIATE"):
            raise BrokerOrderRejectedError(f"Invalid Upstox V3 GTT trigger_type={trig_t!r}.")
        trig_px = float(r.get("trigger_price") or 0.0)
        if trig_px <= 0.0:
            raise BrokerOrderRejectedError(f"Upstox V3 GTT trigger_price must be > 0, got {trig_px}.")
        clean_r: Dict[str, Any] = {
            "strategy": strat,
            "trigger_type": trig_t,
            "trigger_price": round_to_nse_tick(trig_px),
        }
        if r.get("market_protection") is not None:
            clean_r["market_protection"] = int(normalize_market_protection("MARKET", r["market_protection"]))
        if r.get("trailing_gap") is not None:
            clean_r["trailing_gap"] = float(r["trailing_gap"])
        clean_rules.append(clean_r)
    body["rules"] = clean_rules
    return body


class V2BrokerClientProtocol(Protocol):
    def get_funds_and_margin(self) -> Dict[str, Any]: ...
    def calculate_margin(self, instruments: List[Dict[str, Any]]) -> Dict[str, Any]: ...
    def get_positions(self) -> List[Dict[str, Any]]: ...
    def get_orders(self) -> List[Dict[str, Any]]: ...
    def get_order_history(self, order_id: str) -> List[Dict[str, Any]]: ...
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
        from app.data.upstox_provider import UpstoxAuthFailure, UpstoxRateLimitExceeded, get_upstox_limiter

        try:
            get_upstox_limiter().acquire(timeout=self.timeout_sec, is_emergency=is_emergency)
            hdrs = self._headers()
        except (UpstoxRateLimitExceeded, UpstoxAuthFailure) as exc:
            raise BrokerOrderRejectedError(
                f"Pre-send local guard blocked {method} {url} (0 packets sent): {exc}"
            ) from exc

        proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=hdrs,
                json=json_body,
                params=params,
                proxies=proxies,
                timeout=self.timeout_sec,
            )
        except (requests.RequestException, TimeoutError, ConnectionError, OSError) as exc:
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

    def get_order_history(self, order_id: str) -> List[Dict[str, Any]]:
        res = self._request(
            "GET",
            f"{UPSTOX_V2_BASE_URL}/order/history",
            params={"order_id": str(order_id)},
        )
        data = res.get("data")
        return list(data) if isinstance(data, list) else ([dict(data)] if isinstance(data, dict) else [])

    def get_gtt_orders(self) -> List[Dict[str, Any]]:
        res = self._request("GET", f"{UPSTOX_V3_BASE_URL}/order/gtt")
        data = res.get("data")
        return list(data) if isinstance(data, list) else []

    def place_order(self, payload: Dict[str, Any], *, is_emergency: bool = False) -> Dict[str, Any]:
        body = prepare_upstox_v3_order_payload(payload)
        # Upstox HFT V3 /order/place uses instrument_token only; strip non-wire trading_symbol
        body.pop("trading_symbol", None)
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
        body = validate_upstox_v3_gtt_payload(payload, mode="place")
        return self._request(
            "POST",
            f"{UPSTOX_V3_BASE_URL}/order/gtt/place",
            json_body=body,
            is_emergency=True,
            ambiguous_on_transport_error=True,
        )

    def modify_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = validate_upstox_v3_gtt_payload(payload, mode="modify")
        return self._request(
            "PUT",
            f"{UPSTOX_V3_BASE_URL}/order/gtt/modify",
            json_body=body,
            is_emergency=True,
            ambiguous_on_transport_error=True,
        )

    def cancel_gtt_order(self, gtt_id: str, *, is_emergency: bool = True) -> Dict[str, Any]:
        return self._request(
            "DELETE",
            f"{UPSTOX_V3_BASE_URL}/order/gtt/cancel",
            json_body={"gtt_order_id": str(gtt_id)},
            is_emergency=is_emergency,
            ambiguous_on_transport_error=True,
        )


class MockV2BrokerClient:
    """Deterministic in-memory broker simulator with strict Upstox V3 schema enforcement & fault hooks."""

    def __init__(self, *, available_margin_inr: float = 200000.0):
        self._lock = threading.RLock()
        self.available_margin_inr = float(available_margin_inr)
        self._seq = 0
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._order_history: Dict[str, List[Dict[str, Any]]] = {}
        self._gtts: Dict[str, Dict[str, Any]] = {}
        self._positions: Dict[str, Dict[str, Any]] = {}
        self.call_log: List[Dict[str, Any]] = []

        # Live LTP oracle for MARKET fills when payload.price == 0
        self.market_ltp_by_key: Dict[str, float] = {}
        # Upstox V3 /order/place returns ONLY {"order_ids": [oid]} without inline order object
        self.omit_inline_order_in_place_ack: bool = True

        # Fault-injection controls
        self.inject_timeout_order_applied: bool = False
        self.inject_timeout_order_not_received: bool = False
        self.inject_timeout_tags: Set[str] = set()
        self.inject_reject_instrument_keys: Set[str] = set()
        self.inject_partial_fill_ratio: Dict[str, float] = {}
        self.inject_unwind_fail: bool = False
        self.inject_gtt_place_fail: bool = False
        self.inject_gtt_timeout_applied: bool = False
        self.inject_order_delay_sec: float = 0.0
        self.inject_resting_polls_before_fill: Dict[str, int] = {}
        self.inject_unfilled_resting_keys: Set[str] = set()
        self._resting_poll_counters: Dict[str, Tuple[int, float]] = {}
        # Map of gtt_id (or "*") -> fill_price when triggered during cancel
        self.inject_gtt_triggers_during_cancel: Dict[str, float] = {}
        self.injected_margin_required_inr: Optional[float] = None

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

    def _advance_resting_order_if_ready(self, oid: str) -> None:
        rec = self._orders.get(oid)
        if not rec or rec["status"] not in ("open", "validation pending", "open pending"):
            return
        pending = self._resting_poll_counters.get(oid)
        if not pending:
            return
        rem_polls, fill_px = pending
        if rem_polls > 1:
            self._resting_poll_counters[oid] = (rem_polls - 1, fill_px)
            return
        self._resting_poll_counters.pop(oid, None)
        qty = int(rec["quantity"])
        rec["filled_quantity"] = qty
        rec["average_price"] = round_to_nse_tick(fill_px)
        rec["status"] = "complete"
        self._order_history.setdefault(oid, []).append(dict(rec))
        self._record_fill_into_positions(
            rec["instrument_token"], rec["trading_symbol"], rec["transaction_type"], qty, rec["average_price"]
        )

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
            if self.injected_margin_required_inr is not None:
                return {
                    "status": "success",
                    "data": {"required_margin": round(float(self.injected_margin_required_inr), 2)},
                }
            buys = [i for i in instruments if str(i.get("transaction_type", "BUY")).upper() == "BUY"]
            sells = [i for i in instruments if str(i.get("transaction_type", "")).upper() == "SELL"]
            if buys and sells:
                # Hedged vertical spread SPAN margin (~Rs 48,500 per 65-unit NIFTY lot)
                sell_qty = sum(int(i.get("quantity", 0)) for i in sells)
                lots = max(1.0, float(sell_qty) / 65.0)
                return {"status": "success", "data": {"required_margin": round(48500.0 * lots, 2)}}
            if sells:
                sell_qty = sum(int(i.get("quantity", 0)) for i in sells)
                lots = max(1.0, float(sell_qty) / 65.0)
                return {"status": "success", "data": {"required_margin": round(135000.0 * lots, 2)}}
            total = 0.0
            for item in buys:
                q = int(item.get("quantity", 0))
                p = float(item.get("price", 150.0) or 150.0)
                total += q * p
            return {"status": "success", "data": {"required_margin": round(total, 2)}}

    def get_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._positions.values()]

    def get_orders(self) -> List[Dict[str, Any]]:
        with self._lock:
            for oid in list(self._orders.keys()):
                self._advance_resting_order_if_ready(oid)
            return [dict(v) for v in self._orders.values()]

    def get_order_history(self, order_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            self._advance_resting_order_if_ready(order_id)
            return [dict(x) for x in self._order_history.get(order_id, [])]

    def get_gtt_orders(self) -> List[Dict[str, Any]]:
        with self._lock:
            # Matches Upstox V3: completed/triggered GTTs are NOT returned
            return [
                dict(v)
                for v in self._gtts.values()
                if v.get("status") not in ("COMPLETED", "TRIGGERED", "CANCELLED")
            ]

    def place_order(self, payload: Dict[str, Any], *, is_emergency: bool = False) -> Dict[str, Any]:
        if self.inject_order_delay_sec > 0:
            time.sleep(self.inject_order_delay_sec)
        with self._lock:
            body = dict(payload)
            tag = sanitize_upstox_tag(body.get("tag", "OSV2"))
            ikey = str(body["instrument_token"])
            tsym = str(body.get("trading_symbol") or ikey)
            side = str(body["transaction_type"]).upper()
            qty = int(body["quantity"])
            order_type = str(body.get("order_type", "LIMIT")).upper()

            # Strict Upstox V3 schema rejections in Mock too
            if order_type == "MARKET":
                if float(body.get("price") or 0.0) != 0.0:
                    raise BrokerOrderRejectedError(
                        f"Upstox V3 MARKET order requires price=0, got {body.get('price')!r}."
                    )
                if "market_protection" in body and body["market_protection"] is not None:
                    if isinstance(body["market_protection"], (float, bool)) or not isinstance(body["market_protection"], int):
                        raise BrokerOrderRejectedError(
                            f"Upstox V3 market_protection must be int32, got {type(body['market_protection']).__name__}."
                        )
            mp = normalize_market_protection(order_type, body.get("market_protection"))

            pos_px = float((self._positions.get(ikey) or {}).get("last_price") or 0.0)
            limit_px = float(body.get("price") or 0.0)
            if order_type == "MARKET":
                exec_px = float(self.market_ltp_by_key.get(ikey) or pos_px or 100.0)
            else:
                exec_px = float(limit_px if limit_px > 0 else (self.market_ltp_by_key.get(ikey) or pos_px or 100.0))

            self.call_log.append(
                {
                    "method": "place_order",
                    "instrument_token": ikey,
                    "side": side,
                    "order_type": order_type,
                    "price": float(body.get("price") or 0.0),
                    "qty": qty,
                    "tag": tag,
                    "market_protection": mp,
                    "is_emergency": is_emergency,
                }
            )

            if self.inject_unwind_fail and is_emergency and side == "SELL":
                raise BrokerOrderRejectedError("Injected broker rejection during buy-wing unwind.")

            if ikey in self.inject_reject_instrument_keys or f"{ikey}:{order_type}" in self.inject_reject_instrument_keys:
                raise BrokerOrderRejectedError(f"Injected broker rejection for {ikey}")

            tag_should_timeout = (not self.inject_timeout_tags) or (tag in self.inject_timeout_tags)
            if self.inject_timeout_order_not_received and tag_should_timeout:
                self.inject_timeout_order_not_received = False
                raise BrokerTimeoutAmbiguousError("Injected timeout: order NOT received by broker.")

            oid = self._next_id("ORD")
            resting_polls = int(self.inject_resting_polls_before_fill.get(ikey, 0))
            stay_unfilled = (
                ikey in self.inject_unfilled_resting_keys
                or f"{ikey}:{order_type}" in self.inject_unfilled_resting_keys
            )

            if stay_unfilled:
                filled_qty = 0
                status = "open"
                avg_recorded = 0.0
            elif resting_polls > 0:
                filled_qty = 0
                status = "open"
                avg_recorded = 0.0
                self._resting_poll_counters[oid] = (resting_polls, exec_px)
            else:
                ratio = float(self.inject_partial_fill_ratio.get(ikey, 1.0))
                filled_qty = int(qty * ratio)
                if 0 < filled_qty < qty and qty >= 65 and filled_qty >= 65:
                    filled_qty = (filled_qty // 65) * 65
                status = "complete" if filled_qty == qty else ("open" if filled_qty > 0 else "rejected")
                avg_recorded = round_to_nse_tick(exec_px) if filled_qty > 0 else 0.0

            order_rec = {
                "order_id": oid,
                "instrument_token": ikey,
                "trading_symbol": tsym,
                "transaction_type": side,
                "order_type": order_type,
                "quantity": qty,
                "filled_quantity": filled_qty,
                "average_price": avg_recorded,
                "price": round_to_nse_tick(limit_px) if limit_px > 0 else 0.0,
                "status": status,
                "tag": tag,
                "market_protection": mp,
                "order_timestamp": now_ist_iso()[:19].replace("T", " "),
            }
            if filled_qty > 0:
                self._record_fill_into_positions(ikey, tsym, side, filled_qty, avg_recorded)
            self._orders[oid] = order_rec
            self._order_history[oid] = [dict(order_rec)]

            if self.inject_timeout_order_applied and tag_should_timeout:
                self.inject_timeout_order_applied = False
                raise BrokerTimeoutAmbiguousError("Injected timeout AFTER broker accepted and filled order.")

            ack_data: Dict[str, Any] = {"order_ids": [oid]}
            if not self.omit_inline_order_in_place_ack:
                ack_data["order_id"] = oid
                ack_data["order"] = dict(order_rec)
            return {"status": "success", "data": ack_data}

    def cancel_order(self, order_id: str, *, is_emergency: bool = True) -> Dict[str, Any]:
        with self._lock:
            self.call_log.append({"method": "cancel_order", "order_id": order_id})
            self._resting_poll_counters.pop(order_id, None)
            rec = self._orders.get(order_id)
            if rec and rec["status"] not in ("complete", "cancelled", "rejected"):
                rec["status"] = "cancelled"
                self._order_history.setdefault(order_id, []).append(dict(rec))
            return {"status": "success", "data": {"order_id": order_id}}

    def place_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if self.inject_gtt_place_fail:
                self.call_log.append({"method": "place_gtt_order", "failed": True})
                raise BrokerOrderRejectedError("Injected GTT placement failure.")
            clean = validate_upstox_v3_gtt_payload(payload, mode="place")
            gid = self._next_id("GTT")
            rec = dict(clean)
            rec["gtt_order_id"] = gid
            rec["status"] = "SCHEDULED"
            self._gtts[gid] = rec
            self.call_log.append({"method": "place_gtt_order", "gtt_order_id": gid, "payload": clean})
            if self.inject_gtt_timeout_applied:
                self.inject_gtt_timeout_applied = False
                raise BrokerTimeoutAmbiguousError("Injected GTT timeout AFTER broker created GTT.")
            return {"status": "success", "data": {"gtt_order_ids": [gid]}}

    def modify_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            clean = validate_upstox_v3_gtt_payload(payload, mode="modify")
            gid = str(clean["gtt_order_id"])
            if gid in self._gtts:
                self._gtts[gid].update(clean)
            self.call_log.append({"method": "modify_gtt_order", "gtt_order_id": gid, "payload": clean})
            return {"status": "success", "data": {"gtt_order_ids": [gid]}}

    def cancel_gtt_order(self, gtt_id: str, *, is_emergency: bool = True) -> Dict[str, Any]:
        with self._lock:
            self.call_log.append({"method": "cancel_gtt_order", "gtt_order_id": gtt_id})
            rec = self._gtts.get(gtt_id)
            trig_px = self.inject_gtt_triggers_during_cancel.get(gtt_id) or self.inject_gtt_triggers_during_cancel.get("*")
            if rec and trig_px is not None:
                # Real Upstox V3 GTT child order: NO parent_gtt_id and NO custom "GTT-" tag!
                ikey = str(rec["instrument_token"])
                qty = int(rec["quantity"])
                tsym = str((self._positions.get(ikey) or {}).get("trading_symbol") or ikey)
                fill_px = round_to_nse_tick(float(trig_px))
                oid = self._next_id("ORD_GTT")
                ord_rec = {
                    "order_id": oid,
                    "instrument_token": ikey,
                    "trading_symbol": tsym,
                    "transaction_type": str(rec.get("transaction_type", "SELL")).upper(),
                    "order_type": "MARKET",
                    "quantity": qty,
                    "filled_quantity": qty,
                    "average_price": fill_px,
                    "price": 0.0,
                    "status": "complete",
                    "tag": "",
                    "order_timestamp": now_ist_iso()[:19].replace("T", " "),
                }
                self._orders[oid] = ord_rec
                self._order_history[oid] = [dict(ord_rec)]
                self._record_fill_into_positions(
                    ikey, tsym, str(rec.get("transaction_type", "SELL")).upper(), qty, fill_px
                )
                rec["status"] = "TRIGGERED"
                return {"status": "success", "data": {"gtt_order_ids": [gtt_id], "message": "Cancel ack (race)"}}
            if rec:
                rec["status"] = "CANCELLED"
            return {"status": "success", "data": {"gtt_order_ids": [gtt_id]}}
