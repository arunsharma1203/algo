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
    to_ist_naive,
)
from app.analytics.option_sniffer.option_db import OptionDB

logger = logging.getLogger(__name__)


_PREV_OSM_INSTANCE = getattr(globals().get("OptionStreamManager"), "_instance", None)


class OptionStreamManager:
    """
    Singleton manager maintaining real-time quotes, Greeks, order book depth,
    and freshness states for all monitored option contracts and underlying indices.
    """

    _instance = _PREV_OSM_INSTANCE
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

        # Front-Month Futures live quotes: "NIFTY" -> {fut_ltp, fut_atp_vwap, fut_oi, ...}
        self._futures_quotes: Dict[str, Dict[str, Any]] = {}

        # Event-driven listeners for Option Sniffer Advance S2_05 (1m bar aggregator & readiness)
        self._advance_feed_listeners: List[Any] = []

        # In-memory short-lived cache for underlying 1m OHLCV bars: "NIFTY" -> (fetch_time, df)
        self._history_cache: Dict[str, Tuple[float, Any]] = {}

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
        open_position_contracts: List[Dict[str, Any]] = []
        try:
            conn = OptionDB.get_connection()
            cur = conn.cursor()
            for tbl in ("option_sniffer_paper_trades", "option_sniffer_advance_trades"):
                try:
                    cur.execute(
                        f"SELECT instrument_key, trading_symbol, underlying, strike, option_type, expiry, lot_size "
                        f"FROM {tbl} WHERE status = 'OPEN'"
                    )
                    for row in cur.fetchall():
                        if row[0]:
                            open_position_contracts.append({
                                "instrument_key": row[0],
                                "trading_symbol": row[1],
                                "underlying": row[2] or "NIFTY",
                                "strike": float(row[3] or 0.0),
                                "option_type": row[4] or "CE",
                                "expiry": row[5] or "",
                                "lot_size": int(row[6] or 65),
                            })
                except Exception:
                    pass
            try:
                cur.execute(
                    "SELECT instrument_key, trading_symbol, underlying, action, lot_size "
                    "FROM option_live_order_intents "
                    "WHERE state IN ('SUBMITTED', 'PARTIALLY_FILLED', 'FILLED') AND exit_state != 'CONFIRMED_FLAT'"
                )
                for row in cur.fetchall():
                    if row[0]:
                        act_str = str(row[3] or "BUY_CE").upper()
                        open_position_contracts.append({
                            "instrument_key": row[0],
                            "trading_symbol": row[1],
                            "underlying": row[2] or "NIFTY",
                            "option_type": "PE" if "PE" in act_str else "CE",
                            "lot_size": int(row[4] or 65),
                        })
            except Exception:
                pass
            conn.close()
        except Exception:
            pass

        futures_contracts: List[Dict[str, Any]] = []
        try:
            from app.analytics.option_sniffer.option_contract_discovery import OptionContractDiscovery
            fut_map = OptionContractDiscovery.discover_front_futures(allow_network=False) or {}
            for _, slots in fut_map.items():
                front_f = (slots or {}).get("front")
                if isinstance(front_f, dict) and front_f.get("instrument_key"):
                    futures_contracts.append(dict(front_f))
        except Exception:
            pass

        with self._lock:
            if not hasattr(self, "_futures_quotes"):
                self._futures_quotes = {}
            if not hasattr(self, "_advance_feed_listeners"):
                self._advance_feed_listeners = []

            # Preserve pinned catastrophe wings or explicitly pinned futures across universe syncs
            preserved_pinned = [
                m for m in self._key_to_contract.values()
                if m.get("is_catastrophe_wing") or m.get("is_futures")
            ]

            new_keys: List[str] = []
            updated_key_to_contract: Dict[str, Dict[str, Any]] = {}
            for c in list(contracts) + open_position_contracts + preserved_pinned + futures_contracts:
                ikey = c.get("instrument_key")
                if not ikey:
                    continue
                updated_key_to_contract[ikey] = c
                tsym = c.get("trading_symbol")
                if tsym:
                    self._symbol_to_key[tsym] = ikey
                if ikey not in new_keys:
                    new_keys.append(ikey)

            # Include underlying index keys (NIFTY, BANKNIFTY, INDIA VIX)
            for u_name, u_key in UNDERLYING_INSTRUMENTS.items():
                updated_key_to_contract[u_key] = {
                    "instrument_key": u_key,
                    "trading_symbol": u_name,
                    "underlying": u_name,
                    "is_underlying": True,
                }
                self._symbol_to_key[u_name] = u_key
                new_keys.append(u_key)

            self._key_to_contract = updated_key_to_contract

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

            if not hasattr(stream_svc, "_subscribed_ws_keys"):
                stream_svc._subscribed_ws_keys = set()

            if not hasattr(stream_svc, "subscribe_additional_keys"):
                def _dyn_subscribe_keys(keys, mode="full"):
                    if not keys:
                        return
                    to_add = []
                    with stream_svc._lock:
                        for k in keys:
                            if k and k not in stream_svc._key_to_symbol:
                                stream_svc._additional_keys.add(k)
                                if k not in stream_svc._subscribed_ws_keys:
                                    to_add.append(k)
                        st = getattr(stream_svc, "_streamer", None)
                        is_conn = getattr(stream_svc, "_is_connected", False)
                    if to_add and st and is_conn:
                        try:
                            try:
                                st.subscribe(to_add, mode)
                            except TypeError:
                                st.subscribe(to_add, mode=mode)
                            with stream_svc._lock:
                                stream_svc._subscribed_ws_keys.update(to_add)
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
        if "feeds" in feeds and isinstance(feeds["feeds"], dict) and len(feeds) <= 4:
            feeds = feeds["feeds"]

        now_ms = int(time.time() * 1000)
        now_iso = datetime.now().isoformat()
        quotes_to_guard: List[Tuple[str, Dict[str, Any]]] = []
        adv_events: List[Tuple[str, str, Dict[str, Any]]] = []

        with self._lock:
            if not hasattr(self, "_futures_quotes"):
                self._futures_quotes = {}
            if not hasattr(self, "_advance_feed_listeners"):
                self._advance_feed_listeners = []

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

                                u_payload = {
                                    "underlying": u_sym,
                                    "instrument_key": ikey,
                                    "spot_ltp": spot_ltp,
                                    "open": day_open,
                                    "high": day_high,
                                    "low": day_low,
                                    "close": day_close,
                                    "vwap": float(market_ff.get("atp") or spot_ltp),
                                    "timestamp_ms": now_ms,
                                    "exchange_ltt_ms": ltt,
                                    "quote_timestamp": now_iso,
                                    "quote_freshness": "REAL_TIME",
                                    "quote_age_ms": 0,
                                }
                                self._underlying_quotes[u_sym] = u_payload
                                adv_events.append(("INDEX", str(u_sym), dict(u_payload)))
                        except Exception as ue:
                            logger.debug(f"[OptionStreamManager] Underlying parse note for {ikey}: {ue}")
                    continue

                # ── 1B. FRONT-MONTH FUTURES FEED (marketFF) ──────────────────
                if contract_meta.get("is_futures"):
                    u_sym = str(contract_meta.get("underlying") or "NIFTY").upper()
                    market_ff = full_feed.get("marketFF") or {}
                    ltpc = market_ff.get("ltpc") or {}
                    raw_ltp = ltpc.get("ltp")
                    if raw_ltp is not None:
                        try:
                            fut_ltp = float(raw_ltp)
                            if fut_ltp > 0:
                                ltt = int(ltpc.get("ltt") or now_ms)
                                vtt = int(market_ff.get("vtt") or 0)
                                oi = float(market_ff.get("oi") or 0.0)
                                atp = float(market_ff.get("atp") or fut_ltp)
                                tbq = float(market_ff.get("tbq") or 0.0)
                                tsq = float(market_ff.get("tsq") or 0.0)
                                quotes_f = (market_ff.get("marketLevel") or {}).get("bidAskQuote") or []
                                top_f = quotes_f[0] if quotes_f else {}
                                f_bid = float(top_f.get("bidP") or top_f.get("bid_price") or fut_ltp)
                                f_ask = float(top_f.get("askP") or top_f.get("ask_price") or fut_ltp)
                                l5_bid_q = sum(int(q.get("bidQ") or q.get("bid_qty") or 0) for q in quotes_f[:5])
                                l5_ask_q = sum(int(q.get("askQ") or q.get("ask_qty") or 0) for q in quotes_f[:5])
                                f_obi = round((l5_bid_q - l5_ask_q) / max(1, l5_bid_q + l5_ask_q), 4) if (l5_bid_q + l5_ask_q) > 0 else round((tbq - tsq) / max(1.0, tbq + tsq), 4)
                                prev_f = self._futures_quotes.get(u_sym) or {}
                                oi_open = float(prev_f.get("fut_oi_open") or oi or 0.0)
                                if oi_open <= 0 and oi > 0:
                                    oi_open = oi
                                oi_chg_pct = round(((oi - oi_open) / oi_open) * 100.0, 2) if oi_open > 0 else 0.0
                                fut_payload = {
                                    "underlying": u_sym,
                                    "instrument_key": ikey,
                                    "trading_symbol": contract_meta.get("trading_symbol"),
                                    "expiry": contract_meta.get("expiry"),
                                    "fut_ltp": fut_ltp,
                                    "fut_atp_vwap": atp if atp > 0 else fut_ltp,
                                    "fut_volume_vtt": vtt,
                                    "fut_oi": oi,
                                    "fut_oi_open": oi_open,
                                    "fut_oi_chg_pct": oi_chg_pct,
                                    "fut_tbq": tbq,
                                    "fut_tsq": tsq,
                                    "fut_obi": f_obi,
                                    "bid": f_bid,
                                    "ask": f_ask,
                                    "timestamp_ms": now_ms,
                                    "exchange_ltt_ms": ltt,
                                    "quote_timestamp": now_iso,
                                    "quote_freshness": "REAL_TIME",
                                    "quote_age_ms": 0,
                                }
                                self._futures_quotes[u_sym] = fut_payload
                                adv_events.append(("FUTURES", u_sym, dict(fut_payload)))
                        except Exception as fe:
                            logger.debug(f"[OptionStreamManager] Futures parse note for {ikey}: {fe}")
                    continue

                # ── 2. OPTION CONTRACT FEED (marketFF + Full L5 Book) ────────
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
                atp = float(market_ff.get("atp") or 0.0)
                tbq = float(market_ff.get("tbq") or 0.0)
                tsq = float(market_ff.get("tsq") or 0.0)
                iv = float(market_ff.get("iv") or 0.0)
                if 0.0 < iv < 1.5:
                    iv = round(iv * 100.0, 4)

                # Order book depth (top of book + full 5-level Upstox V3 book)
                market_level = market_ff.get("marketLevel") or {}
                quotes = market_level.get("bidAskQuote") or []
                top_quote = quotes[0] if quotes else {}

                bid_price = float(top_quote.get("bidP") or top_quote.get("bid_price") or 0.0)
                ask_price = float(top_quote.get("askP") or top_quote.get("ask_price") or 0.0)
                bid_qty = int(top_quote.get("bidQ") or top_quote.get("bid_qty") or 0)
                ask_qty = int(top_quote.get("askQ") or top_quote.get("ask_qty") or 0)

                depth_levels: List[Dict[str, Any]] = []
                l5_bid_qty = 0
                l5_ask_qty = 0
                for lvl in quotes[:5]:
                    bp = float(lvl.get("bidP") or lvl.get("bid_price") or 0.0)
                    ap = float(lvl.get("askP") or lvl.get("ask_price") or 0.0)
                    bq = int(lvl.get("bidQ") or lvl.get("bid_qty") or 0)
                    aq = int(lvl.get("askQ") or lvl.get("ask_qty") or 0)
                    l5_bid_qty += bq
                    l5_ask_qty += aq
                    depth_levels.append({"bid_p": bp, "bid_q": bq, "ask_p": ap, "ask_q": aq})

                # Preserve real order book depth or exact exchange LTP if partial tick omits top-of-book
                prev_q = self._option_quotes.get(ikey) or {}
                if bid_price <= 0 and ltp > 0:
                    prev_bid = float(prev_q.get("bid") or 0.0)
                    bid_price = prev_bid if (prev_bid > 0 and abs(prev_bid - ltp) / ltp <= 0.03) else ltp
                if ask_price <= 0 and ltp > 0:
                    prev_ask = float(prev_q.get("ask") or 0.0)
                    ask_price = prev_ask if (prev_ask >= bid_price and abs(prev_ask - ltp) / ltp <= 0.03) else max(ltp, bid_price)

                if not depth_levels and prev_q.get("depth_levels"):
                    depth_levels = list(prev_q["depth_levels"])
                    l5_bid_qty = int(prev_q.get("l5_bid_qty") or bid_qty)
                    l5_ask_qty = int(prev_q.get("l5_ask_qty") or ask_qty)

                book_obi_l5 = round((l5_bid_qty - l5_ask_qty) / max(1, l5_bid_qty + l5_ask_qty), 4) if (l5_bid_qty + l5_ask_qty) > 0 else 0.0
                book_obi_total = round((tbq - tsq) / max(1.0, tbq + tsq), 4) if (tbq + tsq) > 0 else book_obi_l5

                # Option Greeks
                greeks = market_ff.get("optionGreeks") or {}
                delta = float(greeks.get("delta") or prev_q.get("delta") or 0.0)
                gamma = float(greeks.get("gamma") or prev_q.get("gamma") or 0.0)
                theta = float(greeks.get("theta") or prev_q.get("theta") or 0.0)
                vega = float(greeks.get("vega") or prev_q.get("vega") or 0.0)

                spread = round(ask_price - bid_price, 2)
                spread_pct = round((spread / ltp * 100.0), 2) if ltp > 0 else 999.0

                age_ms = 0
                is_fresh = True

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
                    "option_vwap_atp": atp if atp > 0 else float(prev_q.get("option_vwap_atp") or 0.0),
                    "tbq": tbq if tbq > 0 else float(prev_q.get("tbq") or 0.0),
                    "tsq": tsq if tsq > 0 else float(prev_q.get("tsq") or 0.0),
                    "l5_bid_qty": l5_bid_qty,
                    "l5_ask_qty": l5_ask_qty,
                    "book_obi_l5": book_obi_l5,
                    "book_obi_total": book_obi_total,
                    "depth_levels": depth_levels,
                    "iv": iv if iv > 0 else float(prev_q.get("iv") or 0.0),
                    "delta": delta,
                    "gamma": gamma,
                    "theta": theta,
                    "vega": vega,
                    "timestamp_ms": now_ms,
                    "exchange_ltt_ms": ltt,
                    "quote_timestamp": now_iso,
                    "quote_freshness": "REAL_TIME",
                    "quote_age_ms": age_ms,
                    "is_valid": bool(ltp > 0 and bid_price > 0 and ask_price >= bid_price and spread_pct <= MAX_SPREAD_PCT and is_fresh),
                }

                self._option_quotes[ikey] = quote_payload
                adv_events.append(("OPTION", ikey, dict(quote_payload)))
                if quote_payload["is_valid"]:
                    quotes_to_guard.append((ikey, dict(quote_payload)))

            adv_listeners = list(self._advance_feed_listeners)

        # Evaluate tick protective guards outside self._lock to prevent AB-BA lock inversion with OptionDB._DB_LOCK
        for ikey, q_copy in quotes_to_guard:
            self._evaluate_tick_protective_guards(ikey, q_copy)

        if adv_listeners and adv_events:
            for cb in adv_listeners:
                try:
                    cb(adv_events)
                except Exception:
                    pass

    def _evaluate_tick_protective_guards(self, ikey: str, quote_payload: Dict[str, Any]) -> None:
        """
        Decoupled Sub-Second Tick Guard for Open Positions:
          1. High-Water Mark Peak Ratchet: Immediately captures sub-second bid spikes into peak_price
             so Chandelier floors ratchet upward without waiting for the 10-second sweep.
          2. Hard Stop-Loss Emergency Exit: Immediately executes STOP_LOSS_HIT if bid drops below
             -stop_hurdle (eliminating 10-second polling slippage on gamma drops).
          3. Sustained Profit Trail Protection: Never triggers TRAILING_STOP_HIT on a 1-second tick
             flicker; trailing stop evaluation remains on the 10-second surveillance sweep.
        """
        # 0A. Evaluate S2_05 sub-second Hard-SL & -₹3,500 MTM kill (preserving close-only ratchets!)
        try:
            from app.analytics.option_sniffer_advance_s205.s205_upstox_engine import get_s205_orchestrator_if_loaded
            s205_orch = get_s205_orchestrator_if_loaded()
            if s205_orch is not None:
                s205_orch.evaluate_subsecond_tick_guard(ikey, quote_payload)
        except Exception as s205_te:
            logger.debug(f"[OptionStreamManager] S2_05 tick protective guard note for {ikey}: {s205_te}")

        # 0B. Evaluate legacy V2.0 sub-second tick protective guards only if non-V6B open trades exist
        try:
            from app.analytics.option_sniffer_advance.advance_db import AdvanceDB as _AdvDBGuard
            legacy_open = [
                t for t in (_AdvDBGuard.get_open_trades() or [])
                if not str(t.get("strategy_mode") or "").startswith("V6B_")
            ]
            if legacy_open:
                from app.analytics.option_sniffer_advance.advance_paper_execution import AdvancePaperExecution
                AdvancePaperExecution.evaluate_subsecond_tick_guard(ikey, quote_payload)
        except Exception as ate:
            logger.debug(f"[OptionStreamManager] Advance tick protective guard note for {ikey}: {ate}")

        try:
            from app.analytics.option_sniffer.option_db import OptionDB
            from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
            from app.analytics.option_sniffer.option_constants import STOP_LOSS_PCT, to_ist_naive

            open_trades = OptionDB.get_open_paper_trades()
            if not open_trades:
                return

            current_bid = float(quote_payload.get("bid") or quote_payload.get("ltp") or 0.0)
            if current_bid <= 0:
                return

            now_dt = to_ist_naive()
            for trade in open_trades:
                if trade.get("instrument_key") != ikey:
                    continue

                entry_ask = float(trade.get("initial_entry_ask") or trade.get("entry_ask") or 0.0)
                if entry_ask <= 0:
                    continue

                prev_peak = float(trade.get("peak_price") or entry_ask)
                prev_trough = float(trade.get("trough_price") or entry_ask)
                current_peak = max(prev_peak, current_bid)
                current_trough = min(prev_trough, current_bid)
                mfe_pct = round(((current_peak - entry_ask) / entry_ask * 100.0), 2)
                mae_pct = round(((current_trough - entry_ask) / entry_ask * 100.0), 2)
                current_pnl_pct = ((current_bid - entry_ask) / entry_ask) * 100.0

                stop_hurdle = float(trade.get("dynamic_sl_pct") or STOP_LOSS_PCT)
                base_lot_qty = int(trade.get("lot_size") or trade.get("quantity") or 0)
                if base_lot_qty > 0:
                    stop_hurdle = OptionPaperExecution.calculate_rupee_capped_sl_pct(entry_ask, base_lot_qty, stop_hurdle)

                if current_pnl_pct <= -stop_hurdle:
                    try:
                        entry_dt = datetime.fromisoformat(trade["entry_time"])
                        duration_sec = max(0, int((now_dt - entry_dt).total_seconds()))
                    except Exception:
                        duration_sec = 0
                    OptionPaperExecution.close_trade(
                        trade=trade,
                        exit_bid=current_bid,
                        exit_ask=float(quote_payload.get("ask") or current_bid),
                        exit_reason="STOP_LOSS_HIT",
                        peak_price=current_peak,
                        trough_price=current_trough,
                        mfe_pct=mfe_pct,
                        mae_pct=mae_pct,
                        duration_sec=duration_sec,
                    )
                elif current_peak > prev_peak or current_trough < prev_trough:
                    OptionDB.update_trade_extrema(
                        trade_id=trade["trade_id"],
                        peak_price=current_peak,
                        trough_price=current_trough,
                        mfe_pct=mfe_pct,
                        mae_pct=mae_pct,
                    )
        except Exception as te:
            logger.debug(f"[OptionStreamManager] Tick protective guard note for {ikey}: {te}")

    # ──────────────────────────────────────────────────────────────────────────
    # DATA RETRIEVAL & FAIL-CLOSED GATES
    # ──────────────────────────────────────────────────────────────────────────

    def compute_session_index_benchmark(
        self,
        underlying: str = "NIFTY",
        as_of_time: Optional[datetime] = None
    ) -> Optional[float]:
        """
        Computes the Session Typical-Price Benchmark (TPB) for the underlying index:
            TPB = mean((High + Low + Close) / 3)
        across all verified completed 1-minute bars of the current trading session (09:15 to 15:30 IST).

        Fail-Safe Policies:
          - Rejects unverified, incomplete, pre-market (< 09:15), or post-market (> 15:30) bars.
          - Requires at least 3 completed bars in the current session. Returns None if < 3 bars.
          - Never forward-fills or substitutes spot LTP.
          - Cached for 2.0 seconds to eliminate SQLite locking overhead during rapid sweeps.
        """
        u_upper = underlying.upper().strip()
        now_dt = to_ist_naive(as_of_time) if as_of_time else datetime.now()
        date_str = now_dt.strftime("%Y-%m-%d")
        session_start = f"{date_str} 09:15:00"
        session_end = f"{date_str} 15:30:00"
        as_of_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        cutoff_str = min(session_end, as_of_str)

        now_mono = time.time()
        with self._lock:
            cache = getattr(self, "_benchmark_cache", {}).get(u_upper)
            if cache:
                cache_time, cache_val = cache
                if (now_mono - cache_time) < 2.0:
                    return cache_val

        try:
            from app.analytics.option_sniffer.option_db import OptionDB as _ActiveOptionDB
            conn = _ActiveOptionDB.get_connection()
            query = """
                SELECT open, high, low, close, timestamp
                FROM continuous_index_ohlcv
                WHERE ticker = ?
                  AND timestamp >= ?
                  AND timestamp <= ?
                  AND is_complete = 1
                  AND COALESCE(is_synthetic, 0) = 0
                  AND COALESCE(is_gap, 0) = 0
                ORDER BY timestamp ASC
            """
            cur = conn.cursor()
            cur.execute(query, (u_upper, session_start, cutoff_str))
            rows = cur.fetchall()
            conn.close()

            if not rows or len(rows) < 3:
                logger.debug(
                    f"[OptionStreamManager] Insufficient session bars for {u_upper} benchmark "
                    f"(count={len(rows) if rows else 0} < 3). Returning None."
                )
                with self._lock:
                    if not hasattr(self, "_benchmark_cache"):
                        self._benchmark_cache = {}
                    self._benchmark_cache[u_upper] = (now_mono, None)
                return None

            # Arithmetic mean of typical price: (H + L + C) / 3.0
            typical_sum = sum((float(r[1]) + float(r[2]) + float(r[3])) / 3.0 for r in rows)
            benchmark_val = round(typical_sum / len(rows), 2)
            s_open = float(rows[0][0])
            s_high = max(float(r[1]) for r in rows)
            s_low = min(float(r[2]) for r in rows)
            first_ts = str(rows[0][4] or "") if len(rows[0]) > 4 else ""
            has_full_open = ("09:15" <= first_ts[11:16] <= "09:20") or ("T09:15" <= first_ts[10:16] <= "T09:20")

            with self._lock:
                if not hasattr(self, "_benchmark_cache"):
                    self._benchmark_cache = {}
                if not hasattr(self, "_session_ohlc_cache"):
                    self._session_ohlc_cache = {}
                self._benchmark_cache[u_upper] = (now_mono, benchmark_val)
                self._session_ohlc_cache[u_upper] = (now_mono, (s_open, s_high, s_low, has_full_open))

            return benchmark_val
        except Exception as e:
            logger.warning(f"[OptionStreamManager] Benchmark calculation error for {u_upper}: {e}")
            return None

    def get_underlying_quote(self, underlying: str = "NIFTY") -> Optional[Dict[str, Any]]:
        """Retrieves latest live quote for the underlying index with session benchmark and sanitized >=09:15 OHLC."""
        u_upper = underlying.upper().strip()
        with self._lock:
            raw_quote = self._underlying_quotes.get(u_upper)
            if raw_quote:
                now_ms = int(time.time() * 1000)
                quote = dict(raw_quote)
                quote["quote_age_ms"] = max(0, now_ms - quote["timestamp_ms"])
            else:
                quote = None

        if quote:
            benchmark = self.compute_session_index_benchmark(u_upper)
            quote["session_benchmark"] = benchmark
            curr_vwap = float(quote.get("vwap") or 0.0)
            quote["raw_broker_vwap"] = curr_vwap
            spot_val = float(quote.get("spot_ltp") or 0.0)
            if benchmark and float(benchmark) > 0:
                if curr_vwap <= 0 or abs(curr_vwap - spot_val) < 1e-4:
                    quote["vwap"] = float(benchmark)

            # Sanitize open/high/low against >= 09:15 IST session bars when Upstox 1d OHLC is contaminated by 09:00 pre-open spikes
            with self._lock:
                s_ohlc_entry = getattr(self, "_session_ohlc_cache", {}).get(u_upper)
            if s_ohlc_entry:
                _, ohlc_tuple = s_ohlc_entry
                s_open, s_high, s_low = ohlc_tuple[0], ohlc_tuple[1], ohlc_tuple[2]
                has_full_open = bool(ohlc_tuple[3]) if len(ohlc_tuple) > 3 else False
                eff_high = max(s_high, spot_val) if spot_val > 0 else s_high
                eff_low = min(s_low, spot_val) if (s_low > 0 and spot_val > 0) else (s_low or spot_val)
                q_high = float(quote.get("high") or 0.0)
                q_low = float(quote.get("low") or 0.0)
                spike_tol = 0.0 if has_full_open else (120.0 if "BANK" in u_upper else 45.0)
                if eff_high > 0 and (q_high <= 0 or (q_high - eff_high) > spike_tol):
                    quote["high"] = eff_high
                    if s_open > 0:
                        quote["open"] = s_open
                if eff_low > 0 and (q_low <= 0 or (eff_low - q_low) > spike_tol):
                    quote["low"] = eff_low
                    if s_open > 0:
                        quote["open"] = s_open
        return quote

    def get_underlying_history_df(self, underlying: str = "NIFTY", limit: int = 375) -> Optional[Any]:
        """
        Retrieves recent continuous 1-minute OHLCV bars for the underlying index
        from continuous_index_ohlcv SQLite table (prioritizing current session >= 09:15 IST
        so Opening Range 09:15-09:30 bars remain available all day without T-1/pre-market contamination).
        Cached for 1.5 seconds to eliminate SQLite contention during rapid surveillance sweeps.
        """
        u_upper = underlying.upper().strip()
        now = time.time()

        with self._lock:
            cached = getattr(self, "_history_cache", {}).get(u_upper)
            if cached:
                cached_time, cached_df = cached
                if (now - cached_time) < 1.5:
                    return cached_df

        try:
            import pandas as pd
            from app.analytics.option_sniffer.option_db import OptionDB as _ActiveOptionDB
            conn = _ActiveOptionDB.get_connection()
            today_str = to_ist_naive().strftime("%Y-%m-%d")
            session_start = f"{today_str} 09:15:00"
            session_query = """
                SELECT timestamp, open, high, low, close, volume
                FROM continuous_index_ohlcv
                WHERE ticker = ?
                  AND substr(replace(timestamp, 'T', ' '), 1, 19) >= ?
                  AND COALESCE(is_complete, 1) = 1
                  AND COALESCE(is_synthetic, 0) = 0
                  AND COALESCE(is_gap, 0) = 0
                ORDER BY timestamp DESC
                LIMIT ?
            """
            df = pd.read_sql_query(session_query, conn, params=(u_upper, session_start, limit))
            if df is None or df.empty:
                fallback_query = """
                    SELECT timestamp, open, high, low, close, volume
                    FROM continuous_index_ohlcv
                    WHERE ticker = ?
                      AND COALESCE(is_complete, 1) = 1
                      AND COALESCE(is_synthetic, 0) = 0
                      AND COALESCE(is_gap, 0) = 0
                    ORDER BY timestamp DESC
                    LIMIT ?
                """
                df = pd.read_sql_query(fallback_query, conn, params=(u_upper, limit))
            conn.close()

            if df is not None and not df.empty:
                df = df.iloc[::-1].reset_index(drop=True)
                # Ensure float numeric casting for OHLCV
                for col in ["open", "high", "low", "close", "volume"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
            else:
                df = None

            with self._lock:
                if not hasattr(self, "_history_cache"):
                    self._history_cache = {}
                self._history_cache[u_upper] = (now, df)

            return df
        except Exception as e:
            logger.debug(f"[OptionStreamManager] get_underlying_history_df error for {u_upper}: {e}")
            return None

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
        """Returns all cached option quotes for currently monitored option contracts."""
        with self._lock:
            monitored_opt_keys = [
                k for k, m in self._key_to_contract.items()
                if not m.get("is_underlying")
                and not m.get("is_futures")
                and not m.get("is_catastrophe_wing")
            ]
            target_keys = monitored_opt_keys if monitored_opt_keys else [
                k for k in self._option_quotes.keys()
                if not (self._key_to_contract.get(k) or {}).get("is_catastrophe_wing")
            ]
            return [self.get_option_quote(k) for k in target_keys if self.get_option_quote(k)]

    def get_futures_quote(self, underlying: str = "NIFTY") -> Optional[Dict[str, Any]]:
        """Retrieves live Front-Month Futures quote (LTP, exchange ATP VWAP, OI, TBQ/TSQ, L5 OBI)."""
        u_upper = str(underlying or "NIFTY").upper().strip()
        with self._lock:
            fmap = getattr(self, "_futures_quotes", {})
            fq = fmap.get(u_upper)
            if not fq:
                return None
            out = dict(fq)
            now_ms = int(time.time() * 1000)
            out["quote_age_ms"] = max(0, now_ms - int(out.get("timestamp_ms") or now_ms))
            return out

    def register_advance_feed_listener(self, listener: Any) -> None:
        """Registers an event-driven tick/bar callback for Option Sniffer Advance S2_05."""
        if not callable(listener):
            return
        with self._lock:
            if not hasattr(self, "_advance_feed_listeners"):
                self._advance_feed_listeners = []
            if listener not in self._advance_feed_listeners:
                self._advance_feed_listeners.append(listener)

    def pin_extra_contracts(self, contracts: List[Dict[str, Any]]) -> List[str]:
        """Pins Futures or far-OTM Catastrophe Wings into OptionStreamManager & WebSocket V3."""
        pinned: List[str] = []
        if not contracts:
            return pinned
        with self._lock:
            for c in contracts:
                if not isinstance(c, dict):
                    continue
                ikey = str(c.get("instrument_key") or "").strip()
                if not ikey:
                    continue
                self._key_to_contract[ikey] = dict(c)
                tsym = c.get("trading_symbol")
                if tsym:
                    self._symbol_to_key[str(tsym)] = ikey
                pinned.append(ikey)
        if pinned:
            try:
                stream_svc = get_stream_service()
                if hasattr(stream_svc, "subscribe_additional_keys"):
                    stream_svc.subscribe_additional_keys(pinned)
            except Exception:
                pass
        return pinned

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
                        "iv": round(float(greeks.get("iv") or 0.0) * 100.0, 4) if 0.0 < float(greeks.get("iv") or 0.0) < 1.5 else float(greeks.get("iv") or 0.0),
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

    def refresh_quotes_via_rest(self, min_interval_sec: float = 3.5) -> int:
        """
        Direct resilient polling mechanism:
        Fetches live Upstox market quotes (LTP, Bid, Ask, OI, Volume, OHLC)
        for all registered option contracts and underlying index instruments
        in single batch REST calls (/v2/market-quote/quotes).
        Provides 100% resilient real-time quote updates even if WebSocket connection is lost or delayed,
        while strictly deduplicating concurrent calls to respect Upstox API rate limits.
        """
        effective_interval = max(2.5, float(min_interval_sec or 3.5))
        with self._lock:
            now_t = time.time()
            last_rest = getattr(self, "_last_rest_refresh_time", 0.0)
            if (now_t - last_rest) < effective_interval and len(self._option_quotes) > 0:
                return len(self._option_quotes)
            self._last_rest_refresh_time = now_t
            all_keys = list(self._key_to_contract.keys())

        from app.data.upstox_provider import get_upstox_token, UPSTOX_BASE_URL, upstox_request
        token = get_upstox_token()
        if not token:
            return 0

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

                        # 1B. Front-Month Futures quote
                        if meta.get("is_futures"):
                            if not hasattr(self, "_futures_quotes"):
                                self._futures_quotes = {}
                            u_sym = str(meta.get("underlying") or "NIFTY").upper()
                            buy_d = v.get("depth", {}).get("buy", [])
                            sell_d = v.get("depth", {}).get("sell", [])
                            f_bid = float((buy_d[0] if buy_d else {}).get("price") or ltp)
                            f_ask = float((sell_d[0] if sell_d else {}).get("price") or ltp)
                            l5_bq = sum(int(x.get("quantity") or 0) for x in buy_d[:5])
                            l5_aq = sum(int(x.get("quantity") or 0) for x in sell_d[:5])
                            tbq_f = float(v.get("total_buy_quantity") or l5_bq or 0.0)
                            tsq_f = float(v.get("total_sell_quantity") or l5_aq or 0.0)
                            f_obi = round((l5_bq - l5_aq) / max(1, l5_bq + l5_aq), 4) if (l5_bq + l5_aq) > 0 else round((tbq_f - tsq_f) / max(1.0, tbq_f + tsq_f), 4)
                            oi_f = float(v.get("oi") or 0.0)
                            prev_f = self._futures_quotes.get(u_sym) or {}
                            oi_open = float(prev_f.get("fut_oi_open") or oi_f or 0.0)
                            if oi_open <= 0 and oi_f > 0:
                                oi_open = oi_f
                            self._futures_quotes[u_sym] = {
                                "underlying": u_sym,
                                "instrument_key": matched_key,
                                "trading_symbol": meta.get("trading_symbol"),
                                "expiry": meta.get("expiry"),
                                "fut_ltp": ltp,
                                "fut_atp_vwap": float(v.get("average_price") or ltp),
                                "fut_volume_vtt": int(v.get("volume") or 0),
                                "fut_oi": oi_f,
                                "fut_oi_open": oi_open,
                                "fut_oi_chg_pct": round(((oi_f - oi_open) / oi_open) * 100.0, 2) if oi_open > 0 else 0.0,
                                "fut_tbq": tbq_f,
                                "fut_tsq": tsq_f,
                                "fut_obi": f_obi,
                                "bid": f_bid,
                                "ask": f_ask,
                                "timestamp_ms": now_ms,
                                "quote_timestamp": now_iso,
                                "quote_freshness": "REAL_TIME",
                                "quote_age_ms": 0,
                            }
                            updated_count += 1
                            continue

                        # 2. Option contract quote (with full 5-level Upstox depth + exchange ATP)
                        buy_depth = v.get("depth", {}).get("buy", [])
                        top_bid = buy_depth[0] if buy_depth else {}
                        bid = float(top_bid.get("price") or 0.0)
                        bid_qty = int(top_bid.get("quantity") or 0)

                        sell_depth = v.get("depth", {}).get("sell", [])
                        top_ask = sell_depth[0] if sell_depth else {}
                        ask = float(top_ask.get("price") or 0.0)
                        ask_qty = int(top_ask.get("quantity") or 0)

                        depth_levels: List[Dict[str, Any]] = []
                        l5_bid_qty = 0
                        l5_ask_qty = 0
                        for lvl_idx in range(min(5, max(len(buy_depth), len(sell_depth)))):
                            b_item = buy_depth[lvl_idx] if lvl_idx < len(buy_depth) else {}
                            s_item = sell_depth[lvl_idx] if lvl_idx < len(sell_depth) else {}
                            bp = float(b_item.get("price") or 0.0)
                            bq = int(b_item.get("quantity") or 0)
                            ap = float(s_item.get("price") or 0.0)
                            aq = int(s_item.get("quantity") or 0)
                            l5_bid_qty += bq
                            l5_ask_qty += aq
                            depth_levels.append({"bid_p": bp, "bid_q": bq, "ask_p": ap, "ask_q": aq})

                        existing_quote = self._option_quotes.get(matched_key) or {}
                        if bid <= 0 and ltp > 0:
                            prev_bid = float(existing_quote.get("bid") or 0.0)
                            bid = prev_bid if (prev_bid > 0 and abs(prev_bid - ltp) / ltp <= 0.03) else ltp
                        if ask <= 0 and ltp > 0:
                            prev_ask = float(existing_quote.get("ask") or 0.0)
                            ask = prev_ask if (prev_ask >= bid and abs(prev_ask - ltp) / ltp <= 0.03) else max(ltp, bid)

                        spread = round(ask - bid, 2)
                        spread_pct = round((spread / ltp * 100.0), 2) if ltp > 0 else 1.0
                        vol = int(v.get("volume") or 0)
                        oi = float(v.get("oi") or 0.0)
                        atp = float(v.get("average_price") or existing_quote.get("option_vwap_atp") or 0.0)
                        tbq = float(v.get("total_buy_quantity") or existing_quote.get("tbq") or 0.0)
                        tsq = float(v.get("total_sell_quantity") or existing_quote.get("tsq") or 0.0)
                        book_obi_l5 = round((l5_bid_qty - l5_ask_qty) / max(1, l5_bid_qty + l5_ask_qty), 4) if (l5_bid_qty + l5_ask_qty) > 0 else 0.0
                        book_obi_total = round((tbq - tsq) / max(1.0, tbq + tsq), 4) if (tbq + tsq) > 0 else book_obi_l5

                        opt_t = str(meta.get("option_type") or "CE").upper()
                        raw_existing_delta = existing_quote.get("delta")
                        iv = float(existing_quote.get("iv") or 15.0)
                        if 0.0 < iv < 1.5:
                            iv = round(iv * 100.0, 4)
                        delta = float(raw_existing_delta) if raw_existing_delta is not None else (0.50 if opt_t == "CE" else -0.50)

                        # Analytical Black-Scholes Delta fallback when REST quote lacks live WebSocket Greeks
                        # (prevents OTM strikes like +250pt CE from spoofing placeholder delta = 0.50)
                        try:
                            u_sym_m = str(meta.get("underlying") or "NIFTY").upper()
                            u_spot_m = float((self._underlying_quotes.get(u_sym_m) or {}).get("spot_ltp") or 0.0)
                            strike_m = float(meta.get("strike") or 0.0)
                            if u_spot_m > 0 and strike_m > 0 and (raw_existing_delta is None or (abs(abs(delta) - 0.50) < 1e-4 and abs(strike_m - u_spot_m) > 30.0)):
                                import math
                                dte_days = 4.0
                                exp_s = str(meta.get("expiry") or "")
                                if exp_s:
                                    try:
                                        exp_d = datetime.strptime(exp_s[:10], "%Y-%m-%d").date()
                                        dte_days = max(0.5, float((exp_d - to_ist_naive().date()).days))
                                    except Exception:
                                        dte_days = 4.0
                                t_yr = max(1.0 / 365.0, dte_days / 365.0)
                                sigma = max(0.08, iv / 100.0)
                                d1 = (math.log(u_spot_m / strike_m) + (0.065 + 0.5 * sigma * sigma) * t_yr) / (sigma * math.sqrt(t_yr))
                                call_delta = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
                                call_delta = max(0.02, min(0.98, call_delta))
                                delta = round(call_delta if opt_t == "CE" else (call_delta - 1.0), 4)
                        except Exception:
                            pass

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
                            "option_vwap_atp": atp,
                            "tbq": tbq,
                            "tsq": tsq,
                            "l5_bid_qty": l5_bid_qty,
                            "l5_ask_qty": l5_ask_qty,
                            "book_obi_l5": book_obi_l5,
                            "book_obi_total": book_obi_total,
                            "depth_levels": depth_levels,
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

        if updated_count > 0:
            self._last_rest_refresh_time = time.time()
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

            fut_map = getattr(self, "_futures_quotes", {})
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
                "futures_quotes": {k: v.get("fut_ltp") for k, v in fut_map.items()},
            }
            return telemetry


if _PREV_OSM_INSTANCE is not None:
    _PREV_OSM_INSTANCE.__class__ = OptionStreamManager


def get_stream_manager() -> OptionStreamManager:
    """Returns singleton instance of OptionStreamManager."""
    return OptionStreamManager()

