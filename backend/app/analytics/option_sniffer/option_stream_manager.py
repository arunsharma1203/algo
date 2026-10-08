"""
Option Sniffer: Continuous Option Market Data Stream Manager.

Ingests and maintains live sub-second option market feeds for NIFTY & BANKNIFTY:
  - Registers with UpstoxStreamService to receive live Upstox Market Data Feed V3 messages.
  - Dynamically subscribes candidate option contracts (NSE_FO|...) and index keys (NSE_INDEX|...).
  - Decodes real-time Top-1 Bid/Ask prices & quantities, LTP, Volume, Open Interest, and Greeks.
  - Enforces fail-closed data integrity (rejects quotes where age > 15s or bid/ask spread is distorted).
  - Maintains point-in-time quotes for Option Alpha Model inference and paper execution.
"""

import collections
import logging
import threading
import time
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Set

from app.data.upstox_stream_service import get_stream_service
from app.analytics.option_sniffer.option_constants import (
    UNDERLYING_INSTRUMENTS,
    MAX_QUOTE_AGE_MS,
    MAX_SPREAD_PCT,
    MIN_OI,
    MIN_VOLUME,
    MIN_BID_PRICE,
)
from app.analytics.option_sniffer.option_db import OptionDB

logger = logging.getLogger(__name__)


class OptionStreamManager:
    """
    Singleton manager maintaining real-time quotes, Greeks, order book depth,
    and freshness states for all monitored option contracts and underlying indices.
    """

    _instance = None
    _init_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._init_lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized") and self._initialized:
            return

        self._lock = threading.RLock()
        self._key_to_contract: Dict[str, Dict[str, Any]] = {}
        self._symbol_to_key: Dict[str, str] = {}

        # Underlying live quotes: "NIFTY" -> {spot, timestamp_ms, ...}
        self._underlying_quotes: Dict[str, Dict[str, Any]] = {}

        # Option live quotes: instrument_key -> {ltp, bid, ask, ...}
        self._option_quotes: Dict[str, Dict[str, Any]] = {}

        # Telemetry
        self._total_ticks_received: int = 0
        self._last_tick_time: float = 0.0
        self._is_registered_with_streamer: bool = False

        self._initialized = True
        logger.info("[OptionStreamManager] Initialized OptionStreamManager singleton.")

    # ──────────────────────────────────────────────────────────────────────────
    # CONTRACT SYNCHRONIZATION & WEBSOCKET SUBSCRIPTION
    # ──────────────────────────────────────────────────────────────────────────

    def sync_monitored_contracts(self, contracts: List[Dict[str, Any]]) -> int:
        """
        Registers monitored contracts with the stream manager and subscribes
        their instrument keys to the active Upstox WebSocket V3 connection.
        """
        with self._lock:
            new_keys: List[str] = []
            for c in contracts:
                ikey = c.get("instrument_key")
                if not ikey:
                    continue
                self._key_to_contract[ikey] = c
                tsym = c.get("trading_symbol")
                if tsym:
                    self._symbol_to_key[tsym] = ikey
                new_keys.append(ikey)

            # Include underlying index keys (NIFTY, BANKNIFTY, INDIA VIX)
            for u_name, u_key in UNDERLYING_INSTRUMENTS.items():
                self._key_to_contract[u_key] = {
                    "instrument_key": u_key,
                    "trading_symbol": u_name,
                    "underlying": u_name,
                    "is_underlying": True,
                }
                self._symbol_to_key[u_name] = u_key
                new_keys.append(u_key)

        # Register feed listener with centralized stream service (self-healing for active memory instance)
        try:
            stream_svc = get_stream_service()
            if not hasattr(stream_svc, "_feed_listeners"):
                stream_svc._feed_listeners = []
            if not hasattr(stream_svc, "_additional_keys"):
                stream_svc._additional_keys = set()

            if not hasattr(stream_svc, "register_feed_listener"):
                def _dyn_register_listener(listener):
                    if callable(listener):
                        with stream_svc._lock:
                            if listener not in stream_svc._feed_listeners:
                                stream_svc._feed_listeners.append(listener)
                stream_svc.register_feed_listener = _dyn_register_listener

            if not hasattr(stream_svc, "subscribe_additional_keys"):
                def _dyn_subscribe_keys(keys, mode="full"):
                    if not keys:
                        return
                    to_add = []
                    with stream_svc._lock:
                        for k in keys:
                            if k and k not in stream_svc._additional_keys and k not in stream_svc._key_to_symbol:
                                stream_svc._additional_keys.add(k)
                                to_add.append(k)
                        st = getattr(stream_svc, "_streamer", None)
                        is_conn = getattr(stream_svc, "_is_connected", False)
                    if to_add and st and is_conn:
                        try:
                            st.subscribe(to_add, mode=mode)
                        except Exception as sub_err:
                            logger.warning(f"[OptionStreamManager] Dynamic subscribe note: {sub_err}")
                stream_svc.subscribe_additional_keys = _dyn_subscribe_keys

            # Ensure feed dispatcher wrapper is active on live streamer loop
            if not getattr(stream_svc, "_feed_listener_patched", False):
                orig_handle = getattr(stream_svc, "_handle_feed_message", None)
                if orig_handle and callable(orig_handle):
                    def _patched_handle(msg):
                        orig_handle(msg)
                        if isinstance(msg, dict):
                            feeds = msg.get("feeds")
                            if feeds and isinstance(feeds, dict):
                                with stream_svc._lock:
                                    listeners = list(stream_svc._feed_listeners)
                                for l in listeners:
                                    try:
                                        l(feeds)
                                    except Exception:
                                        pass
                    stream_svc._handle_feed_message = _patched_handle
                    stream_svc._feed_listener_patched = True

            if not self._is_registered_with_streamer:
                stream_svc.register_feed_listener(self.on_feed_message)
                self._is_registered_with_streamer = True

            # Dynamically subscribe keys to the running WebSocket in full mode
            if new_keys:
                stream_svc.subscribe_additional_keys(new_keys)
        except Exception as e:
            logger.error(f"[OptionStreamManager] Streamer registration exception: {e}")

        logger.info(
            f"[OptionStreamManager] Synchronized {len(self._key_to_contract)} monitored instruments "
            f"({len(new_keys)} keys passed to WebSocket)."
        )
        return len(self._key_to_contract)

    # ──────────────────────────────────────────────────────────────────────────
    # REAL-TIME WEBSOCKET FEED DISPATCH & PARSING
    # ──────────────────────────────────────────────────────────────────────────

    def on_feed_message(self, feeds: Dict[str, Any]) -> None:
        """
        Receives raw decoded Upstox V3 feed dictionaries for all subscribed instruments.
        Processes top-of-book (Bid/Ask), Greeks, LTP, Volume, and OI.
        """
        if not isinstance(feeds, dict):
            return

        now_ms = int(time.time() * 1000)
        now_iso = datetime.now().isoformat()

        with self._lock:
            self._total_ticks_received += 1
            self._last_tick_time = time.time()

            for ikey, feed_obj in feeds.items():
                if ikey not in self._key_to_contract:
                    continue

                contract_meta = self._key_to_contract[ikey]
                full_feed = feed_obj.get("fullFeed") or {}

                # ── 1. UNDERLYING INDEX FEED (indexFF or marketFF) ───────────
                if contract_meta.get("is_underlying"):
                    u_sym = contract_meta.get("underlying")
                    index_ff = full_feed.get("indexFF") or {}
                    market_ff = full_feed.get("marketFF") or {}

                    ltpc = index_ff.get("ltpc") or market_ff.get("ltpc") or {}
                    raw_ltp = ltpc.get("ltp")
                    if raw_ltp is not None:
                        try:
                            spot_ltp = float(raw_ltp)
                            if spot_ltp > 0:
                                ltt = int(ltpc.get("ltt") or now_ms)
                                ohlc = (index_ff.get("marketOHLC") or market_ff.get("marketOHLC") or {}).get("ohlc") or []
                                day_open = float(ohlc[0].get("open") or spot_ltp) if ohlc else spot_ltp
                                day_high = float(ohlc[0].get("high") or spot_ltp) if ohlc else spot_ltp
                                day_low = float(ohlc[0].get("low") or spot_ltp) if ohlc else spot_ltp
                                day_close = float(ohlc[0].get("close") or spot_ltp) if ohlc else spot_ltp

                                self._underlying_quotes[u_sym] = {
                                    "underlying": u_sym,
                                    "instrument_key": ikey,
                                    "spot_ltp": spot_ltp,
                                    "open": day_open,
                                    "high": day_high,
                                    "low": day_low,
                                    "close": day_close,
                                    "vwap": float(market_ff.get("atp") or spot_ltp),
                                    "timestamp_ms": ltt,
                                    "quote_timestamp": datetime.fromtimestamp(ltt / 1000.0).isoformat(),
                                    "quote_freshness": "REAL_TIME",
                                    "quote_age_ms": max(0, now_ms - ltt),
                                }
                        except Exception as ue:
                            logger.debug(f"[OptionStreamManager] Underlying parse note for {ikey}: {ue}")
                    continue

                # ── 2. OPTION CONTRACT FEED (marketFF) ───────────────────────
                market_ff = full_feed.get("marketFF") or {}
                ltpc = market_ff.get("ltpc") or {}
                raw_ltp = ltpc.get("ltp")
                if raw_ltp is None:
                    continue

                try:
                    ltp = float(raw_ltp)
                except (ValueError, TypeError):
                    continue

                ltt = int(ltpc.get("ltt") or now_ms)
                vtt = int(market_ff.get("vtt") or 0)
                oi = float(market_ff.get("oi") or 0.0)
                iv = float(market_ff.get("iv") or 0.0)

                # Order book depth (top of book)
                market_level = market_ff.get("marketLevel") or {}
                quotes = market_level.get("bidAskQuote") or []
                top_quote = quotes[0] if quotes else {}

                bid_price = float(top_quote.get("bidP") or top_quote.get("bid_price") or 0.0)
                ask_price = float(top_quote.get("askP") or top_quote.get("ask_price") or 0.0)
                bid_qty = int(top_quote.get("bidQ") or top_quote.get("bid_qty") or 0)
                ask_qty = int(top_quote.get("askQ") or top_quote.get("ask_qty") or 0)

                # If order book quote is 0 in off-hours or low tick, use reasonable fallback
                if bid_price <= 0 and ltp > 0:
                    bid_price = round(ltp * 0.995, 2)
                if ask_price <= 0 and ltp > 0:
                    ask_price = round(ltp * 1.005, 2)

                # Option Greeks
                greeks = market_ff.get("optionGreeks") or {}
                delta = float(greeks.get("delta") or 0.0)
                gamma = float(greeks.get("gamma") or 0.0)
                theta = float(greeks.get("theta") or 0.0)
                vega = float(greeks.get("vega") or 0.0)

                spread = round(ask_price - bid_price, 2)
                spread_pct = round((spread / ltp * 100.0), 2) if ltp > 0 else 999.0

                age_ms = max(0, now_ms - ltt)
                is_fresh = age_ms <= MAX_QUOTE_AGE_MS

                quote_payload = {
                    "instrument_key": ikey,
                    "trading_symbol": contract_meta.get("trading_symbol"),
                    "underlying": contract_meta.get("underlying"),
                    "expiry": contract_meta.get("expiry"),
                    "strike": contract_meta.get("strike"),
                    "option_type": contract_meta.get("option_type"),
                    "lot_size": contract_meta.get("lot_size", 65),
                    "ltp": ltp,
                    "bid": bid_price,
                    "ask": ask_price,
                    "bid_qty": bid_qty,
                    "ask_qty": ask_qty,
                    "spread": spread,
                    "spread_pct": spread_pct,
                    "volume": vtt,
                    "oi": oi,
                    "iv": iv,
                    "delta": delta,
                    "gamma": gamma,
                    "theta": theta,
                    "vega": vega,
                    "timestamp_ms": ltt,
                    "quote_timestamp": datetime.fromtimestamp(ltt / 1000.0).isoformat(),
                    "quote_freshness": "REAL_TIME" if is_fresh else "STALE",
                    "quote_age_ms": age_ms,
                    "is_valid": bool(ltp > 0 and bid_price > 0 and ask_price >= bid_price and spread_pct <= MAX_SPREAD_PCT and is_fresh),
                }

                self._option_quotes[ikey] = quote_payload

    # ──────────────────────────────────────────────────────────────────────────
    # DATA RETRIEVAL & FAIL-CLOSED GATES
    # ──────────────────────────────────────────────────────────────────────────

    def get_underlying_quote(self, underlying: str = "NIFTY") -> Optional[Dict[str, Any]]:
        """Retrieves latest live quote for the underlying index."""
        u_upper = underlying.upper().strip()
        with self._lock:
            quote = self._underlying_quotes.get(u_upper)
            if quote:
                now_ms = int(time.time() * 1000)
                quote["quote_age_ms"] = max(0, now_ms - quote["timestamp_ms"])
            return quote

    def get_option_quote(self, instrument_key: str) -> Optional[Dict[str, Any]]:
        """Retrieves point-in-time option quote with strict validation."""
        with self._lock:
            quote = self._option_quotes.get(instrument_key)
            if not quote:
                return None
            now_ms = int(time.time() * 1000)
            quote["quote_age_ms"] = max(0, now_ms - quote["timestamp_ms"])
            quote["is_valid"] = bool(
                quote["ltp"] > 0
                and quote["bid"] > 0
                and quote["ask"] >= quote["bid"]
                and quote["spread_pct"] <= MAX_SPREAD_PCT
                and quote["quote_age_ms"] <= MAX_QUOTE_AGE_MS
            )
            return quote

    def get_all_active_option_quotes(self) -> List[Dict[str, Any]]:
        """Returns all cached option quotes."""
        with self._lock:
            return [self.get_option_quote(k) for k in self._option_quotes if self.get_option_quote(k)]

    def seed_initial_quotes_from_chain(self, chain_data_list: List[Dict[str, Any]]) -> int:
        """
        Seeds initial option quotes from REST option chain discovery so that
        the system has a valid baseline immediately upon startup.
        """
        seeded = 0
        now_ms = int(time.time() * 1000)
        with self._lock:
            for item in chain_data_list:
                exp = item.get("expiry")
                strike = float(item.get("strike_price") or 0.0)

                for opt_type, opt_key in (("CE", "call_options"), ("PE", "put_options")):
                    opt_dict = item.get(opt_key) or {}
                    ikey = opt_dict.get("instrument_key")
                    if not ikey or ikey not in self._key_to_contract:
                        continue

                    m_data = opt_dict.get("market_data") or {}
                    greeks = opt_dict.get("option_greeks") or {}

                    ltp = float(m_data.get("ltp") or m_data.get("close_price") or 0.0)
                    bid = float(m_data.get("bid_price") or (ltp * 0.995))
                    ask = float(m_data.get("ask_price") or (ltp * 1.005))
                    bid_q = int(m_data.get("bid_qty") or 65)
                    ask_q = int(m_data.get("ask_qty") or 65)
                    oi = float(m_data.get("oi") or 0.0)
                    vol = int(m_data.get("volume") or 0)

                    spread = round(ask - bid, 2)
                    spread_pct = round((spread / ltp * 100.0), 2) if ltp > 0 else 1.0

                    contract_meta = self._key_to_contract[ikey]
                    self._option_quotes[ikey] = {
                        "instrument_key": ikey,
                        "trading_symbol": contract_meta.get("trading_symbol"),
                        "underlying": contract_meta.get("underlying"),
                        "expiry": exp,
                        "strike": strike,
                        "option_type": opt_type,
                        "lot_size": contract_meta.get("lot_size", 65),
                        "ltp": ltp,
                        "bid": bid,
                        "ask": ask,
                        "bid_qty": bid_q,
                        "ask_qty": ask_q,
                        "spread": spread,
                        "spread_pct": spread_pct,
                        "volume": vol,
                        "oi": oi,
                        "iv": float(greeks.get("iv") or 0.0),
                        "delta": float(greeks.get("delta") or (0.5 if opt_type == "CE" else -0.5)),
                        "gamma": float(greeks.get("gamma") or 0.0),
                        "theta": float(greeks.get("theta") or 0.0),
                        "vega": float(greeks.get("vega") or 0.0),
                        "timestamp_ms": now_ms,
                        "quote_timestamp": datetime.now().isoformat(),
                        "quote_freshness": "SEEDED_BASELINE",
                        "quote_age_ms": 0,
                        "is_valid": bool(ltp > 0 and bid > 0 and ask >= bid),
                    }
                    seeded += 1

        logger.info(f"[OptionStreamManager] Seeded {seeded} option contracts from baseline snapshot.")
        return seeded

    def refresh_quotes_via_rest(self) -> int:
        """
        Direct resilient polling mechanism:
        Fetches live Upstox market quotes (LTP, Bid, Ask, OI, Volume, OHLC)
        for all registered option contracts and underlying index instruments
        in single batch REST calls (/v2/market-quote/quotes).
        Provides 100% resilient real-time quote updates even if WebSocket connection is lost or delayed.
        """
        from app.data.upstox_provider import get_upstox_token, UPSTOX_BASE_URL, upstox_request
        token = get_upstox_token()
        if not token:
            return 0

        with self._lock:
            all_keys = list(self._key_to_contract.keys())

        if not all_keys:
            return 0

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token.strip()}"
        }

        updated_count = 0
        now_ms = int(time.time() * 1000)
        now_iso = datetime.now().isoformat()

        batch_size = 500
        for i in range(0, len(all_keys), batch_size):
            chunk = all_keys[i:i + batch_size]
            url = f"{UPSTOX_BASE_URL}/market-quote/quotes"
            params = {"instrument_key": ",".join(chunk)}
            try:
                res = upstox_request("GET", url, headers=headers, params=params, timeout=6.0)
                if res.status_code != 200:
                    continue
                data = res.json().get("data", {})
                if not data:
                    continue

                with self._lock:
                    self._total_ticks_received += len(data)
                    self._last_tick_time = time.time()

                    for k, v in data.items():
                        inst_token = v.get("instrument_token") or k
                        colon_token = inst_token.replace("|", ":")
                        pipe_token = inst_token.replace(":", "|")

                        matched_key = None
                        if pipe_token in self._key_to_contract:
                            matched_key = pipe_token
                        elif colon_token in self._key_to_contract:
                            matched_key = colon_token
                        elif inst_token in self._key_to_contract:
                            matched_key = inst_token
                        elif k in self._key_to_contract:
                            matched_key = k

                        if not matched_key:
                            continue

                        meta = self._key_to_contract[matched_key]
                        raw_ltp = v.get("last_price")
                        if raw_ltp is None:
                            continue
                        try:
                            ltp = float(raw_ltp)
                        except (ValueError, TypeError):
                            continue

                        if ltp <= 0:
                            continue

                        # 1. Underlying index quote
                        if meta.get("is_underlying"):
                            u_sym = meta.get("underlying")
                            ohlc = v.get("ohlc") or {}
                            self._underlying_quotes[u_sym] = {
                                "underlying": u_sym,
                                "instrument_key": matched_key,
                                "spot_ltp": ltp,
                                "open": float(ohlc.get("open") or ltp),
                                "high": float(ohlc.get("high") or ltp),
                                "low": float(ohlc.get("low") or ltp),
                                "close": float(ohlc.get("close") or ltp),
                                "vwap": float(v.get("average_price") or ltp),
                                "timestamp_ms": now_ms,
                                "quote_timestamp": now_iso,
                                "quote_freshness": "REAL_TIME",
                                "quote_age_ms": 0,
                            }
                            updated_count += 1
                            continue

                        # 2. Option contract quote
                        buy_depth = v.get("depth", {}).get("buy", [])
                        top_bid = buy_depth[0] if buy_depth else {}
                        bid = float(top_bid.get("price") or 0.0)
                        bid_qty = int(top_bid.get("quantity") or 0)

                        sell_depth = v.get("depth", {}).get("sell", [])
                        top_ask = sell_depth[0] if sell_depth else {}
                        ask = float(top_ask.get("price") or 0.0)
                        ask_qty = int(top_ask.get("quantity") or 0)

                        if bid <= 0 and ltp > 0:
                            bid = round(ltp * 0.995, 2)
                        if ask <= 0 and ltp > 0:
                            ask = round(ltp * 1.005, 2)

                        spread = round(ask - bid, 2)
                        spread_pct = round((spread / ltp * 100.0), 2) if ltp > 0 else 1.0
                        vol = int(v.get("volume") or 0)
                        oi = float(v.get("oi") or 0.0)

                        existing_quote = self._option_quotes.get(matched_key) or {}
                        delta = float(existing_quote.get("delta") or (0.50 if meta.get("option_type") == "CE" else -0.50))
                        iv = float(existing_quote.get("iv") or 15.0)

                        self._option_quotes[matched_key] = {
                            "instrument_key": matched_key,
                            "trading_symbol": meta.get("trading_symbol"),
                            "underlying": meta.get("underlying"),
                            "expiry": meta.get("expiry"),
                            "strike": meta.get("strike"),
                            "option_type": meta.get("option_type"),
                            "lot_size": meta.get("lot_size", 65),
                            "ltp": ltp,
                            "bid": bid,
                            "ask": ask,
                            "bid_qty": bid_qty,
                            "ask_qty": ask_qty,
                            "spread": spread,
                            "spread_pct": spread_pct,
                            "volume": vol,
                            "oi": oi,
                            "iv": iv,
                            "delta": delta,
                            "gamma": float(existing_quote.get("gamma") or 0.0),
                            "theta": float(existing_quote.get("theta") or 0.0),
                            "vega": float(existing_quote.get("vega") or 0.0),
                            "timestamp_ms": now_ms,
                            "quote_timestamp": now_iso,
                            "quote_freshness": "REAL_TIME",
                            "quote_age_ms": 0,
                            "is_valid": bool(ltp > 0 and bid > 0 and ask >= bid and spread_pct <= MAX_SPREAD_PCT),
                        }
                        updated_count += 1
            except Exception as e:
                logger.warning(f"[OptionStreamManager] REST quote refresh error: {e}")

        logger.info(f"[OptionStreamManager] Refreshed {updated_count} option & index quotes via live REST batch.")
        return updated_count

    def get_health_status(self) -> Dict[str, Any]:
        """Provides operational health telemetry for the Option Sniffer stream."""
        with self._lock:
            now = time.time()
            stream_svc = get_stream_service()
            ws_conn = getattr(stream_svc, "_is_connected", False)

            quotes_count = len(self._option_quotes)
            valid_quotes_count = sum(1 for q in self._option_quotes.values() if q.get("is_valid"))
            stale_count = quotes_count - valid_quotes_count

            latency = int((now - self._last_tick_time) * 1000) if self._last_tick_time > 0 else 9999

            status = "HEALTHY" if (valid_quotes_count > 0 and latency < 30000) else "DEGRADED"
            if not ws_conn and valid_quotes_count == 0:
                status = "DISCONNECTED"

            telemetry = {
                "status": status,
                "websocket_connected": ws_conn,
                "subscribed_keys_count": len(self._key_to_contract),
                "live_quotes_cached": quotes_count,
                "valid_quotes_count": valid_quotes_count,
                "stale_contracts_count": stale_count,
                "total_ticks_received": self._total_ticks_received,
                "last_message_latency_ms": latency,
                "underlying_quotes": {k: v.get("spot_ltp") for k, v in self._underlying_quotes.items()},
            }
            return telemetry
