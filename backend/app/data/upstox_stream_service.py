"""
Upstox Continuous Market Data Feed V3 WebSocket Service.

Provides continuous, rate-limit-free intraday streaming for the full ~507 equity universe:
  1. Subscribes all ~507 instruments to Upstox WebSocket V3 in Full mode.
  2. Ingests real-time ticks, LTP, ATP (VWAP), and 1-minute OHLCV candles (I1 feed).
  3. Locally constructs deterministic, completed 15-minute decision bars (bucketed 09:15-09:30, etc.).
  4. Flushes completed 15m bars to SQLite canonical storage without consuming REST API budget.
  5. Provides frozen snapshot synchronization (same decision_bar and snapshot_timestamp)
     for dual-engine Consensus evaluation (Legacy AI + Qlib).
  6. Enforces fail-closed live execution quote retrieval without candle-close execution fallback.
"""

import collections
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, time as dtime
from typing import Dict, Any, List, Optional, Tuple, Set

import numpy as np
import pandas as pd

from app.data.historical_data_layer import get_db_path
from app.data.upstox_provider import (
    get_upstox_token,
    get_instrument_key,
    fetch_upstox_bulk_quotes,
    get_upstox_limiter,
)
from app.analytics.universe_config import (
    resolve_all_database_stocks,
    normalize_ticker,
)

logger = logging.getLogger(__name__)

# Market Hours IST (09:15 to 15:30)
MARKET_OPEN_TIME = dtime(9, 15)
MARKET_CLOSE_TIME = dtime(15, 30)
MAX_QUOTE_AGE_MS = 15000  # 15 seconds freshness threshold for WebSocket LTP


class UpstoxStreamService:
    """
    Singleton service managing the continuous Upstox V3 Market Data Feed WebSocket,
    1-minute streaming ingestion, deterministic 15-minute bar construction, and
    frozen snapshot provenance for the full ~507-stock universe.
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
        self._is_running = False
        self._is_connected = False
        self._streamer = None
        self._worker_thread: Optional[threading.Thread] = None
        self._flusher_thread: Optional[threading.Thread] = None

        # Telemetry
        self._connected_at: Optional[str] = None
        self._last_msg_ts: float = 0.0
        self._total_messages: int = 0
        self._total_ticks: int = 0
        self._reconnect_count: int = 0
        self._last_error: Optional[str] = None

        # Mapping: instrument_key <-> canonical ticker (e.g. 'NSE_EQ|INE002A01018' <-> 'RELIANCE.NS')
        self._key_to_symbol: Dict[str, str] = {}
        self._symbol_to_key: Dict[str, str] = {}

        # Real-time state
        # symbol -> {ltp, vwap, open, high, low, close, volume, timestamp_ms, quote_freshness}
        self._live_quotes: Dict[str, Dict[str, Any]] = {}

        # 1-minute bars: symbol -> { "YYYY-MM-DD HH:MM:00": {open, high, low, close, volume, ts_ms} }
        self._minute_bars: Dict[str, Dict[str, Dict[str, Any]]] = collections.defaultdict(dict)

        # Completed 15m bars: symbol -> { "YYYY-MM-DD HH:MM:00": {open, high, low, close, volume, ts_ms} }
        self._completed_15m_bars: Dict[str, Dict[str, Dict[str, Any]]] = collections.defaultdict(dict)

        # Track which 15m slots have been flushed to SQLite
        self._flushed_slots: Set[Tuple[str, str]] = set()  # (symbol, slot_start_str)

        # Frozen snapshot storage for active consensus scans
        self._latest_frozen_snapshot: Optional[Dict[str, Any]] = None

        # External listeners and dynamic subscriptions (e.g. Option Sniffer, Index Candle Ingestion)
        self._feed_listeners: List[Any] = []
        self._additional_keys: Set[str] = {"NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"}

        # Wire continuous index candle ingestion service (for NIFTY and BANKNIFTY continuous_index_ohlcv)
        try:
            from app.data.index_candle_ingestion_prototype import get_index_candle_ingestion_service
            self.register_feed_listener(lambda feeds: get_index_candle_ingestion_service().process_feed_message({"feeds": feeds}))
            logger.info("[UpstoxStreamService] Wired IndexCandleIngestionService feed listener for continuous_index_ohlcv.")
        except Exception as e:
            logger.warning(f"[UpstoxStreamService] Could not wire index candle ingestion listener: {e}")

        self._initialized = True
        logger.info("[UpstoxStreamService] Initialized continuous streaming service singleton.")

    # ──────────────────────────────────────────────────────────────────────────
    # UNIVERSE & INSTRUMENT KEY RESOLUTION
    # ──────────────────────────────────────────────────────────────────────────

    def resolve_stream_universe(self) -> List[str]:
        """
        Discovers the complete ~507 stock universe and populates bidirectional key mappings.
        Returns list of canonical symbols ('RELIANCE.NS', etc.).
        """
        try:
            res = resolve_all_database_stocks(timeframe="1d", min_bars=60)
            symbols = res.get("scan_ready_symbols") or res.get("valid_symbols") or []
        except Exception as e:
            logger.warning(f"[UpstoxStreamService] Database universe discovery fallback: {e}")
            from app.analytics.universe_config import LIVE_UNIVERSE
            symbols = list(LIVE_UNIVERSE)

        with self._lock:
            self._key_to_symbol.clear()
            self._symbol_to_key.clear()

            valid_symbols = []
            for s in symbols:
                canonical = normalize_ticker(s)
                clean_sym = canonical.replace(".NS", "").replace(".BO", "").strip().upper()
                ikey = get_instrument_key(clean_sym)
                if ikey:
                    self._key_to_symbol[ikey] = canonical
                    self._symbol_to_key[canonical] = ikey
                    self._symbol_to_key[clean_sym] = ikey
                    valid_symbols.append(canonical)

        logger.info(
            f"[UpstoxStreamService] Resolved {len(valid_symbols)} equity instruments "
            f"with authoritative Upstox V3 keys ({len(self._key_to_symbol)} unique keys)."
        )
        return valid_symbols

    def register_feed_listener(self, listener: Any) -> None:
        """Registers a callback receiving raw feeds dict: listener(feeds)."""
        if callable(listener):
            with self._lock:
                if listener not in self._feed_listeners:
                    self._feed_listeners.append(listener)
            logger.info(f"[UpstoxStreamService] Registered feed listener. Total listeners: {len(self._feed_listeners)}")

    def subscribe_additional_keys(self, keys: List[str], mode: str = "full") -> None:
        """Dynamically subscribes non-equity keys (e.g. options, indices) to the running WebSocket."""
        if not keys:
            return
        to_add: List[str] = []
        with self._lock:
            for k in keys:
                if k and k not in self._additional_keys and k not in self._key_to_symbol:
                    self._additional_keys.add(k)
                    to_add.append(k)
            st = self._streamer
            is_conn = self._is_connected

        if to_add and st and is_conn:
            try:
                st.subscribe(to_add, mode=mode)
                logger.info(f"[UpstoxStreamService] Dynamically subscribed {len(to_add)} additional keys to WebSocket.")
            except Exception as sub_err:
                logger.warning(f"[UpstoxStreamService] Dynamic subscribe note: {sub_err}")

    # ──────────────────────────────────────────────────────────────────────────
    # LIFECYCLE MANAGEMENT
    # ──────────────────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """
        Starts the continuous WebSocket background listener and 15m bar aggregator.
        Safe to call multiple times (idempotent).
        """
        with self._lock:
            if self._is_running:
                logger.info("[UpstoxStreamService] Service is already running.")
                return True

            token = get_upstox_token()
            if not token or len(token) < 20:
                logger.warning("[UpstoxStreamService] Valid Upstox token not available. Streaming deferred.")
                return False

            self.resolve_stream_universe()
            if not self._key_to_symbol:
                logger.warning("[UpstoxStreamService] No instrument keys resolved. Cannot connect WebSocket.")
                return False

            self._is_running = True
            self._worker_thread = threading.Thread(target=self._run_streamer, name="UpstoxStreamerWorker", daemon=True)
            self._worker_thread.start()

            self._flusher_thread = threading.Thread(target=self._flusher_loop, name="UpstoxBarFlusher", daemon=True)
            self._flusher_thread.start()

            logger.info(
                f"[UpstoxStreamService] Started WebSocket streaming worker and bar flusher "
                f"for {len(self._key_to_symbol)} instruments."
            )
            return True

    def stop(self) -> None:
        """Gracefully disconnects WebSocket and terminates worker threads."""
        with self._lock:
            self._is_running = False
            self._is_connected = False
            if self._streamer:
                try:
                    self._streamer.disconnect()
                except Exception as e:
                    logger.debug(f"[UpstoxStreamService] Disconnect note: {e}")
                self._streamer = None
            logger.info("[UpstoxStreamService] Stopped streaming service.")

    def _run_streamer(self) -> None:
        """Worker thread establishing and maintaining the Upstox MarketDataStreamerV3 connection."""
        while self._is_running:
            try:
                token = get_upstox_token()
                if not token:
                    time.sleep(5.0)
                    continue

                import upstox_client
                cfg = upstox_client.Configuration()
                cfg.access_token = token
                client = upstox_client.ApiClient(cfg)

                with self._lock:
                    additional = list(self._additional_keys)
                    instrument_keys = list(dict.fromkeys(list(self._key_to_symbol.keys()) + additional))
                logger.info(
                    f"[UpstoxStreamService] Initializing MarketDataStreamerV3 with {len(instrument_keys)} keys "
                    f"({len(additional)} additional/options) (mode=full)..."
                )

                streamer = upstox_client.MarketDataStreamerV3(client, instrument_keys, mode="full")

                def _on_open(*args, **kwargs):
                    with self._lock:
                        self._is_connected = True
                        self._connected_at = datetime.now().isoformat()
                        self._last_error = None
                    logger.info(
                        f"[UpstoxStreamService] ✅ WebSocket V3 CONNECTED. Streaming live ticks for {len(instrument_keys)} instruments."
                    )

                def _on_message(msg, *args, **kwargs):
                    self._handle_feed_message(msg)

                def _on_error(err, *args, **kwargs):
                    with self._lock:
                        self._last_error = str(err)
                    logger.warning(f"[UpstoxStreamService] WebSocket error: {err}")

                def _on_close(*args, **kwargs):
                    with self._lock:
                        self._is_connected = False
                    logger.info(f"[UpstoxStreamService] WebSocket connection closed: {args}")

                streamer.on("open", _on_open)
                streamer.on("message", _on_message)
                streamer.on("error", _on_error)
                streamer.on("close", _on_close)

                self._streamer = streamer
                streamer.connect()

                # Wait up to 10s for initial connection handshake
                for _ in range(50):
                    if not self._is_running or self._is_connected:
                        break
                    time.sleep(0.2)

                # Keep thread alive while service is running
                while self._is_running:
                    time.sleep(2.0)
                    if not self._is_connected and (self._last_msg_ts == 0 or (time.time() - self._last_msg_ts > 30.0)):
                        logger.warning("[UpstoxStreamService] Connection failed or stale for > 30s. Triggering re-initialization...")
                        break

            except Exception as e:
                with self._lock:
                    self._is_connected = False
                    self._last_error = str(e)
                    self._reconnect_count += 1
                logger.error(f"[UpstoxStreamService] Streamer loop exception (reconnecting in 5s): {e}")
                time.sleep(5.0)

    # ──────────────────────────────────────────────────────────────────────────
    # MESSAGE DECODING & REAL-TIME INGESTION
    # ──────────────────────────────────────────────────────────────────────────

    def _handle_feed_message(self, msg: Any) -> None:
        """Processes binary/dict feed message from Upstox WebSocket."""
        if not isinstance(msg, dict):
            return

        now_ms = int(time.time() * 1000)
        self._last_msg_ts = time.time()
        self._total_messages += 1

        feeds = msg.get("feeds")
        if not feeds or not isinstance(feeds, dict):
            return

        # Native broadcast to registered external listeners (e.g. Option Sniffer)
        if self._feed_listeners:
            with self._lock:
                active_listeners = list(self._feed_listeners)
            for listener in active_listeners:
                try:
                    listener(feeds)
                except Exception as l_err:
                    logger.debug(f"[UpstoxStreamService] Listener error: {l_err}")

        now_dt = datetime.now()

        for ikey, feed_obj in feeds.items():
            canonical_sym = self._key_to_symbol.get(ikey)
            if not canonical_sym:
                continue

            full_feed = feed_obj.get("fullFeed") or {}
            market_ff = full_feed.get("marketFF") or {}
            ltpc = market_ff.get("ltpc") or {}

            raw_ltp = ltpc.get("ltp")
            if raw_ltp is None:
                continue

            try:
                ltp = float(raw_ltp)
            except (ValueError, TypeError):
                continue

            if ltp <= 0:
                continue

            ltt = int(ltpc.get("ltt") or now_ms)
            cp = float(ltpc.get("cp") or ltp)
            atp = float(market_ff.get("atp") or ltp)
            vtt = int(market_ff.get("vtt") or 0)

            # Update live quote cache
            with self._lock:
                self._total_ticks += 1
                self._live_quotes[canonical_sym] = {
                    "ticker": canonical_sym,
                    "ltp": ltp,
                    "vwap": atp,
                    "open": float(market_ff.get("open") or ltp),
                    "high": float(market_ff.get("high") or ltp),
                    "low": float(market_ff.get("low") or ltp),
                    "close": ltp,
                    "cp": cp,
                    "volume": vtt,
                    "timestamp_ms": ltt,
                    "quote_timestamp": datetime.fromtimestamp(ltt / 1000.0).isoformat(),
                    "quote_freshness": "REAL_TIME",
                }

            # Ingest 1-minute OHLCV bar
            market_ohlc = market_ff.get("marketOHLC") or {}
            ohlc_list = market_ohlc.get("ohlc") or []

            one_min_candle = None
            for candle in ohlc_list:
                if candle.get("interval") == "I1":
                    one_min_candle = candle
                    break

            if one_min_candle:
                try:
                    c_open = float(one_min_candle.get("open") or ltp)
                    c_high = float(one_min_candle.get("high") or ltp)
                    c_low = float(one_min_candle.get("low") or ltp)
                    c_close = float(one_min_candle.get("close") or ltp)
                    c_vol = int(one_min_candle.get("vol") or 0)
                    c_ts_ms = int(one_min_candle.get("ts") or now_ms)

                    # Upstox I1 ts is epoch ms in IST
                    c_dt = datetime.fromtimestamp(c_ts_ms / 1000.0)
                    minute_str = c_dt.strftime("%Y-%m-%d %H:%M:00")

                    with self._lock:
                        self._minute_bars[canonical_sym][minute_str] = {
                            "open": c_open,
                            "high": c_high,
                            "low": c_low,
                            "close": c_close,
                            "volume": c_vol,
                            "ts_ms": c_ts_ms,
                            "dt": c_dt,
                        }
                except Exception:
                    pass
            else:
                # Synthesize 1-minute candle from continuous LTP updates if I1 message omitted
                curr_min_str = now_dt.strftime("%Y-%m-%d %H:%M:00")
                with self._lock:
                    m_bar = self._minute_bars[canonical_sym].get(curr_min_str)
                    if not m_bar:
                        self._minute_bars[canonical_sym][curr_min_str] = {
                            "open": ltp,
                            "high": ltp,
                            "low": ltp,
                            "close": ltp,
                            "volume": 0,
                            "ts_ms": now_ms,
                            "dt": now_dt,
                        }
                    else:
                        m_bar["high"] = max(m_bar["high"], ltp)
                        m_bar["low"] = min(m_bar["low"], ltp)
                        m_bar["close"] = ltp
                        m_bar["ts_ms"] = now_ms

    # ──────────────────────────────────────────────────────────────────────────
    # DETERMINISTIC 15-MINUTE BAR AGGREGATION & FLUSHING
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def get_market_session_15m_slots(today_dt: datetime) -> List[Tuple[datetime, datetime]]:
        """
        Returns list of (slot_start, slot_end) tuples for the full NSE market session:
        09:15 to 15:30 IST.
        Slots: 09:15-09:30, 09:30-09:45, 09:45-10:00, ..., 15:15-15:30.
        """
        d = today_dt.date()
        start_session = datetime.combine(d, MARKET_OPEN_TIME)
        end_session = datetime.combine(d, MARKET_CLOSE_TIME)

        slots = []
        curr = start_session
        while curr < end_session:
            nxt = curr + timedelta(minutes=15)
            slots.append((curr, nxt))
            curr = nxt
        return slots

    def _flusher_loop(self) -> None:
        """
        Background flusher thread executing every 5 seconds:
          1. Detects any 15-minute slot that has just COMPLETED (now >= slot_end).
          2. Deterministically aggregates 1m bars for each symbol into completed 15m bars.
          3. Flushes completed 15m bars in bulk to SQLite ohlcv storage.
          4. Prunes stale in-memory 1m bars to keep memory bounded.
        """
        while self._is_running:
            try:
                self.aggregate_and_flush_completed_bars()
            except Exception as e:
                logger.error(f"[UpstoxStreamService] Bar flusher loop exception: {e}")
            try:
                from app.data.index_candle_ingestion_prototype import get_index_candle_ingestion_service
                get_index_candle_ingestion_service().check_and_finalize_elapsed_minutes(
                    datetime.now() - timedelta(seconds=2)
                )
            except Exception as idx_err:
                logger.debug(f"[UpstoxStreamService] Index minute finalizer note: {idx_err}")
            time.sleep(5.0)

    def aggregate_and_flush_completed_bars(self, force_as_of: Optional[datetime] = None) -> int:
        """
        Aggregates all 1m bars into completed 15m decision bars and persists to SQLite.
        A bar is ONLY completed when now >= slot_end. Incomplete bars are NEVER finalized.
        Returns count of new 15m bars persisted.
        """
        now_dt = force_as_of or datetime.now()
        session_slots = self.get_market_session_15m_slots(now_dt)

        completed_slots = [
            (s_start, s_end)
            for s_start, s_end in session_slots
            if now_dt >= s_end
        ]

        if not completed_slots:
            return 0

        rows_to_insert = []
        now_iso = datetime.now().isoformat()

        with self._lock:
            for sym, m_dict in self._minute_bars.items():
                if not m_dict:
                    continue

                for s_start, s_end in completed_slots:
                    slot_key = s_start.strftime("%Y-%m-%d %H:%M:%S")
                    flush_token = (sym, slot_key)
                    if flush_token in self._flushed_slots:
                        continue

                    # Collect 1m bars belonging to [s_start, s_end)
                    slot_bars = []
                    for m_key, b in m_dict.items():
                        b_dt = b.get("dt")
                        if not b_dt:
                            try:
                                b_dt = datetime.strptime(m_key, "%Y-%m-%d %H:%M:%S")
                            except Exception:
                                continue
                        if s_start <= b_dt < s_end:
                            slot_bars.append((b_dt, b))

                    if not slot_bars:
                        continue

                    # Deterministic OHLCV construction from completed 1m bars
                    slot_bars.sort(key=lambda x: x[0])
                    bar_open = float(slot_bars[0][1]["open"])
                    bar_close = float(slot_bars[-1][1]["close"])
                    bar_high = float(max(b["high"] for _, b in slot_bars))
                    bar_low = float(min(b["low"] for _, b in slot_bars))
                    bar_vol = int(sum(b["volume"] for _, b in slot_bars))

                    # Record completed 15m bar
                    self._completed_15m_bars[sym][slot_key] = {
                        "ticker": sym,
                        "date": slot_key,
                        "open": bar_open,
                        "high": bar_high,
                        "low": bar_low,
                        "close": bar_close,
                        "volume": bar_vol,
                        "slot_start": s_start.isoformat(),
                        "slot_end": s_end.isoformat(),
                    }

                    rows_to_insert.append((
                        sym,
                        slot_key,
                        bar_open,
                        bar_high,
                        bar_low,
                        bar_close,
                        bar_vol,
                        "15m",
                        "upstox_ws",
                        now_iso
                    ))
                    self._flushed_slots.add(flush_token)

        if not rows_to_insert:
            return 0

        # Persist to canonical SQLite in single high-speed transaction
        try:
            db_p = get_db_path()
            conn = sqlite3.connect(db_p, timeout=15.0)
            cur = conn.cursor()
            cur.executemany("""
                INSERT OR REPLACE INTO ohlcv (
                    ticker, date, open, high, low, close, volume, timeframe, source, hoard_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, rows_to_insert)
            conn.commit()
            conn.close()

            logger.info(
                f"[UpstoxStreamService] Persisted {len(rows_to_insert)} completed 15m bars "
                f"to SQLite across full universe (zero REST quota consumed)."
            )
            return len(rows_to_insert)
        except Exception as e:
            logger.error(f"[UpstoxStreamService] Failed persisting 15m bars to SQLite: {e}")
            return 0

    # ──────────────────────────────────────────────────────────────────────────
    # FROZEN DECISION SNAPSHOT API FOR CONSENSUS
    # ──────────────────────────────────────────────────────────────────────────

    def freeze_decision_snapshot(
        self,
        universe: Optional[List[str]] = None,
        as_of_dt: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Freezes a single, immutable decision snapshot for the current 15-minute decision cycle:
          1. Freezes one decision_bar (latest completed 15m interval).
          2. Freezes snapshot_timestamp and decision_timestamp.
          3. Evaluates which symbols have that exact completed bar in SQLite and memory.
          4. Symbols with missing bar -> flagged DATA_INCOMPLETE / NOT_ACTIONABLE.
          5. Pre-caches fresh executable LTPs from WebSocket stream.
        """
        now_dt = as_of_dt or datetime.now()
        session_slots = self.get_market_session_15m_slots(now_dt)

        completed_slots = [
            (s_start, s_end)
            for s_start, s_end in session_slots
            if now_dt >= s_end
        ]

        if not completed_slots:
            # Market just opened or pre-market: fallback to latest available bar
            latest_slot_start = session_slots[0][0]
            latest_slot_end = session_slots[0][1]
        else:
            latest_slot_start, latest_slot_end = completed_slots[-1]

        decision_bar_str = latest_slot_start.strftime("%Y-%m-%d %H:%M:%S")
        snapshot_ts_iso = now_dt.isoformat()
        decision_ts_iso = latest_slot_end.isoformat()

        target_universe = universe or list(self._symbol_to_key.keys())
        target_universe = list(dict.fromkeys(normalize_ticker(s) for s in target_universe if s))

        # Check which symbols possess this exact completed bar in SQLite
        db_p = get_db_path()
        conn = sqlite3.connect(db_p, timeout=10.0)
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT DISTINCT ticker FROM ohlcv WHERE timeframe = '15m' AND date = ?",
            (decision_bar_str,)
        ).fetchall()
        conn.close()

        sqlite_ready_set = {r[0] for r in rows}

        eligible_symbols = []
        data_incomplete_symbols = []
        live_quotes_snapshot = {}

        now_ms = int(time.time() * 1000)

        with self._lock:
            for sym in target_universe:
                has_sqlite = (sym in sqlite_ready_set) or (sym.replace(".NS", "") in sqlite_ready_set)
                q = self._live_quotes.get(sym) or self._live_quotes.get(sym.replace(".NS", ""))

                if has_sqlite:
                    eligible_symbols.append(sym)
                else:
                    data_incomplete_symbols.append(sym)

                if q:
                    age_ms = now_ms - q.get("timestamp_ms", 0)
                    freshness = "REAL_TIME" if age_ms <= MAX_QUOTE_AGE_MS else "DELAYED"
                    live_quotes_snapshot[sym] = {
                        "ticker": sym,
                        "ltp": q["ltp"],
                        "vwap": q.get("vwap", q["ltp"]),
                        "open": q.get("open", q["ltp"]),
                        "high": q.get("high", q["ltp"]),
                        "low": q.get("low", q["ltp"]),
                        "close": q["close"],
                        "volume": q.get("volume", 0),
                        "quote_timestamp": q.get("quote_timestamp", snapshot_ts_iso),
                        "freshness_ms": age_ms,
                        "quote_freshness": freshness,
                        "is_executable": (freshness == "REAL_TIME" and q["ltp"] > 0)
                    }

        snapshot = {
            "decision_bar": decision_bar_str,
            "decision_timestamp": decision_ts_iso,
            "snapshot_timestamp": snapshot_ts_iso,
            "eligible_symbols": eligible_symbols,
            "data_incomplete_symbols": data_incomplete_symbols,
            "total_universe_count": len(target_universe),
            "eligible_count": len(eligible_symbols),
            "incomplete_count": len(data_incomplete_symbols),
            "quotes": live_quotes_snapshot,
            "ws_connected": self._is_connected,
            "status": "FROZEN_READY"
        }

        with self._lock:
            self._latest_frozen_snapshot = snapshot

        logger.info(
            f"[UpstoxStreamService] Frozen Decision Snapshot [{decision_bar_str}]: "
            f"{len(eligible_symbols)}/{len(target_universe)} equities eligible "
            f"({len(data_incomplete_symbols)} incomplete)."
        )
        return snapshot

    # ──────────────────────────────────────────────────────────────────────────
    # FAIL-CLOSED LIVE EXECUTION QUOTE PROVIDER
    # ──────────────────────────────────────────────────────────────────────────

    def get_executable_quote(self, symbol: str) -> Dict[str, Any]:
        """
        Provides fresh executable quote for trade execution:
          1. Uses WebSocket LTP if fresh (age <= 15s).
          2. If stale or missing, requests rate-governed REST bulk quote (chunked in 500s).
          3. If unavailable, returns fail-closed UNAVAILABLE (NEVER falls back to candle close).
        """
        canonical = normalize_ticker(symbol)
        clean = canonical.replace(".NS", "").replace(".BO", "").strip().upper()
        now_ms = int(time.time() * 1000)

        with self._lock:
            q = self._live_quotes.get(canonical) or self._live_quotes.get(clean)
            if q:
                age_ms = now_ms - q.get("timestamp_ms", 0)
                if age_ms <= MAX_QUOTE_AGE_MS and float(q.get("ltp", 0)) > 0:
                    return {
                        "ticker": canonical,
                        "entry_price": float(q["ltp"]),
                        "ltp": float(q["ltp"]),
                        "vwap": float(q.get("vwap", q["ltp"])),
                        "open": float(q.get("open", q["ltp"])),
                        "quote_timestamp": q.get("quote_timestamp"),
                        "freshness_ms": age_ms,
                        "quote_freshness": "REAL_TIME",
                        "is_actionable": True,
                        "source": "UPSTOX_WS"
                    }

        # Stale or missing from WebSocket: fetch via REST bulk quotes (rate-governed)
        try:
            bulk_map = fetch_upstox_bulk_quotes([canonical])
            res_q = bulk_map.get(canonical) or bulk_map.get(clean)
            if res_q and float(res_q.get("ltp", 0)) > 0:
                return {
                    "ticker": canonical,
                    "entry_price": float(res_q["ltp"]),
                    "ltp": float(res_q["ltp"]),
                    "vwap": float(res_q.get("vwap", res_q["ltp"])),
                    "open": float(res_q.get("open", res_q["ltp"])),
                    "quote_timestamp": datetime.now().isoformat(),
                    "freshness_ms": 100,
                    "quote_freshness": "REAL_TIME",
                    "is_actionable": True,
                    "source": "UPSTOX_REST_BULK"
                }
        except Exception as e:
            logger.warning(f"[UpstoxStreamService] REST fallback quote error for {canonical}: {e}")

        # FAIL CLOSED: No executable entry from candle close permitted
        return {
            "ticker": canonical,
            "entry_price": None,
            "ltp": None,
            "quote_freshness": "UNAVAILABLE",
            "is_actionable": False,
            "source": "FAIL_CLOSED"
        }

    # ──────────────────────────────────────────────────────────────────────────
    # STATUS & TELEMETRY
    # ──────────────────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Returns comprehensive real-time telemetry on the streaming engine."""
        now = time.time()
        with self._lock:
            return {
                "is_running": self._is_running,
                "is_connected": self._is_connected,
                "connected_at": self._connected_at,
                "last_message_seconds_ago": round(now - self._last_msg_ts, 1) if self._last_msg_ts > 0 else None,
                "total_messages": self._total_messages,
                "total_ticks": self._total_ticks,
                "reconnect_count": self._reconnect_count,
                "last_error": self._last_error,
                "subscribed_instruments_count": len(self._key_to_symbol),
                "live_quotes_cached": len(self._live_quotes),
                "symbols_with_1m_data": len(self._minute_bars),
                "symbols_with_15m_completed": len(self._completed_15m_bars),
                "flushed_slots_count": len(self._flushed_slots),
                "latest_decision_bar": (
                    self._latest_frozen_snapshot.get("decision_bar")
                    if self._latest_frozen_snapshot else None
                ),
                "rate_governor": get_upstox_limiter().get_stats()
            }


_PREV_STREAM_INSTANCE = getattr(globals().get("UpstoxStreamService"), "_instance", None) or globals().get("_GLOBAL_STREAM_SERVICE")
if _PREV_STREAM_INSTANCE is not None:
    UpstoxStreamService._instance = _PREV_STREAM_INSTANCE
    _PREV_STREAM_INSTANCE.__class__ = UpstoxStreamService
    _GLOBAL_STREAM_SERVICE = _PREV_STREAM_INSTANCE
    try:
        import sys
        import importlib
        if "app.data.index_candle_ingestion_prototype" in sys.modules:
            importlib.reload(sys.modules["app.data.index_candle_ingestion_prototype"])
    except Exception as _reload_err:
        logger.debug(f"[UpstoxStreamService] IndexIngestion reload note: {_reload_err}")
else:
    _GLOBAL_STREAM_SERVICE = UpstoxStreamService()


def get_stream_service() -> UpstoxStreamService:
    """Returns the authoritative global UpstoxStreamService singleton."""
    return _GLOBAL_STREAM_SERVICE
