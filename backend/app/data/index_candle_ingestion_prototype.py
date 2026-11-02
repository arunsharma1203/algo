"""
Index Candle Ingestion Prototype: Isolated Real-Time Ingestion for NIFTY & BANKNIFTY.

Authoritative Phase 1 Implementation (Audited & Hardened):
- Upstox Market Data Feed V3 compliant: strictly rejects deprecated V2 endpoints.
- Enforces standard secure TLS configuration (CERT_REQUIRED, check_hostname=True).
- Evaluates Upstox V3 provider-supplied I1 candles alongside local tick aggregation with deterministic precedence.
- Decouples WebSocket transport health from individual index tick frequency (no false outages on quiescent markets).
- Native protobuf binary decoding via MarketDataFeedV3_pb2.
- Explicit distinction between provisional in-flight candles (is_complete=0) and finalized completed candles (is_complete=1).
- Wall-clock minute finalization operates independently of tick arrivals (handles silent market boundaries).
- Enforces point-in-time causal provenance: records received_at, available_at, reconciled_at, and versioning.
- Persists strictly into an isolated SQLite table (`continuous_index_ohlcv`); zero touches to existing trading tables.
- Enforces strict fail-closed warm-up: synthetic carry bars, data gaps, and provisional bars are strictly disallowed from indicator warm-up.
- Report-only: 100% decoupled from production strategy execution.
"""

import collections
import logging
import sqlite3
import ssl
import threading
import time
from datetime import datetime, timedelta, time as dtime
from typing import Dict, Any, List, Optional, Tuple, Set

logger = logging.getLogger(__name__)

# Authoritative Upstox V3 Endpoints
UPSTOX_V3_AUTH_ENDPOINT = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
UPSTOX_V3_WS_BASE_URL = "wss://api.upstox.com/v3/feed/market-data-feed"

# Authoritative Index Keys in Upstox V3
INDEX_KEYS = {
    "NSE_INDEX|Nifty 50": "NIFTY",
    "NSE_INDEX|Nifty Bank": "BANKNIFTY",
}

# Market Trading Hours IST
MARKET_OPEN_TIME = dtime(9, 15)
MARKET_CLOSE_TIME = dtime(15, 30)

MAX_CLOCK_SKEW_SEC = 5.0
FUTURE_TICK_TOLERANCE_SEC = 2.0
TRANSPORT_HEARTBEAT_TIMEOUT_SEC = 60.0  # Transport timeout (heartbeat / ping-pong)
DISCREPANCY_TOLERANCE_PTS = 0.05  # Point difference threshold for discrepancy flag


def get_secure_ssl_context() -> ssl.SSLContext:
    """Creates a secure TLS context strictly enforcing certificate and hostname verification."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class IndexCandleIngestionPrototype:
    """
    Isolated prototype managing continuous 1-minute OHLCV ingestion for cash indices
    using Upstox V3 Market Data Feed with dual-engine verification (Provider I1 + Local Aggregator).
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        ws_url: str = UPSTOX_V3_WS_BASE_URL,
        auth_url: str = UPSTOX_V3_AUTH_ENDPOINT,
        transport_heartbeat_timeout_sec: float = TRANSPORT_HEARTBEAT_TIMEOUT_SEC
    ):
        self._db_path = db_path or "market_data.db"
        self._ws_url = ws_url
        self._auth_url = auth_url
        self.transport_heartbeat_timeout_sec = transport_heartbeat_timeout_sec
        self._lock = threading.RLock()
        
        # Verify V3 endpoint configuration
        self._validate_endpoint_configuration()

        # State & Transport Health (Decoupled from index tick frequency)
        self._is_running = False
        self._status = "INITIALIZED"  # 'INITIALIZED' | 'CONNECTED' | 'DISCONNECTED' | 'AUTH_EXPIRED' | 'CONFIG_ERROR'
        self._is_transport_connected = False
        self._last_transport_heartbeat = 0.0  # Local epoch timestamp of last received transport frame
        
        # Per-Ticker State
        self._last_tick_time: Dict[str, float] = {}  # ticker -> local epoch sec of last price tick
        self._last_tick_timestamp_ms: Dict[str, int] = {}  # ticker -> exchange epoch ms
        self._last_finalized_minute: Dict[str, str] = {}  # ticker -> last finalized minute_str ('YYYY-MM-DD HH:MM:00')
        self._last_skew_log_time: Dict[str, float] = {}  # ticker -> local epoch sec of last skew warning log
        self._skew_warnings_count = 0
        self._out_of_order_count = 0
        self._total_ticks_processed = 0
        self._discrepancy_count = 0
        
        # In-Memory Active Minute Accumulators (Provisional / In-Flight):
        # ticker -> { "minute_str", "minute_dt", "open", "high", "low", "close", "tick_count", "provider_i1", "last_price", "received_at" }
        self._open_buckets: Dict[str, Dict[str, Any]] = {}
        
        # Completed 1-Minute Ring Buffer:
        # ticker -> collections.deque(maxlen=390) of completed candle dicts
        self._completed_bars: Dict[str, collections.deque] = {
            "NIFTY": collections.deque(maxlen=390),
            "BANKNIFTY": collections.deque(maxlen=390),
        }
        
        self.init_db()

    def _validate_endpoint_configuration(self) -> None:
        """Validates that endpoints strictly adhere to Upstox Market Data Feed V3 and rejects legacy V2."""
        if "/v2/" in self._ws_url or "/v2/" in self._auth_url:
            err = f"Legacy V2 endpoints rejected: ws_url='{self._ws_url}', auth_url='{self._auth_url}'. Upstox V3 required."
            logger.critical(f"[IndexIngestion] 🚨 CONFIGURATION ERROR: {err}")
            self._status = "CONFIG_ERROR"
            raise ValueError(err)

    def init_db(self) -> None:
        """Initializes isolated continuous_index_ohlcv table with full point-in-time and audit metadata."""
        with sqlite3.connect(self._db_path, timeout=10.0) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS continuous_index_ohlcv (
                    ticker TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER DEFAULT 0,
                    tick_count INTEGER DEFAULT 0,
                    source TEXT NOT NULL,
                    is_complete INTEGER DEFAULT 1,
                    is_synthetic INTEGER DEFAULT 0,
                    is_gap INTEGER DEFAULT 0,
                    provider_open REAL,
                    provider_high REAL,
                    provider_low REAL,
                    provider_close REAL,
                    local_open REAL,
                    local_high REAL,
                    local_low REAL,
                    local_close REAL,
                    discrepancy_flag INTEGER DEFAULT 0,
                    received_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    reconciled_at TEXT,
                    version INTEGER DEFAULT 1,
                    is_point_in_time INTEGER DEFAULT 1,
                    PRIMARY KEY (ticker, timestamp, version)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_index_ohlcv_time ON continuous_index_ohlcv(ticker, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_index_ohlcv_pit ON continuous_index_ohlcv(ticker, timestamp, available_at, is_point_in_time)")
            conn.commit()

    # ──────────────────────────────────────────────────────────────────────────
    # PROTOBUF V3 BINARY DECODING
    # ──────────────────────────────────────────────────────────────────────────

    def decode_protobuf_message(self, raw_bytes: bytes) -> Optional[Dict[str, Any]]:
        """
        Decodes binary WebSocket buffer using official Upstox V3 protobuf definition.
        Converts Message to standard Python dictionary with 64-bit string-int handling.
        """
        try:
            from upstox_client.feeder.proto import MarketDataFeedV3_pb2
            from google.protobuf.json_format import MessageToDict
            
            feed_response = MarketDataFeedV3_pb2.FeedResponse()
            feed_response.ParseFromString(raw_bytes)
            return MessageToDict(feed_response)
        except Exception as e:
            logger.error(f"[IndexIngestion] Protobuf V3 decode failed: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # MESSAGE DECODING & TICK PROCESSING
    # ──────────────────────────────────────────────────────────────────────────

    def process_feed_message(self, msg: Dict[str, Any], wall_clock_time: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """
        Processes a raw/decoded Upstox V3 Market Data Feed message for NIFTY and BANKNIFTY.
        Updates transport heartbeat independently of whether index prices changed.
        Updates provisional minute accumulators and triggers finalization of any elapsed minutes.
        """
        now = wall_clock_time or datetime.now()
        now_ts = now.timestamp()
        finalized_candles: List[Dict[str, Any]] = []

        with self._lock:
            # 1. Update Transport Heartbeat (any frame proves transport is alive)
            self._last_transport_heartbeat = now_ts
            self._is_transport_connected = True
            if self._status != "AUTH_EXPIRED" and self._status != "CONFIG_ERROR":
                self._status = "CONNECTED"

            if self._status in ("AUTH_EXPIRED", "CONFIG_ERROR"):
                logger.warning(f"[IndexIngestion] Ingestion halted fail-closed (status={self._status}).")
                return []

            # 2. Check and finalize any elapsed minutes ONLY for tickers not actively receiving ticks in this frame
            feeds = msg.get("feeds") if isinstance(msg.get("feeds"), dict) else {}
            last_finalized_map = getattr(self, "_last_finalized_minute", None)
            if last_finalized_map is None:
                self._last_finalized_minute = {}
                last_finalized_map = self._last_finalized_minute
            last_skew_log_map = getattr(self, "_last_skew_log_time", None)
            if last_skew_log_map is None:
                self._last_skew_log_time = {}
                last_skew_log_map = self._last_skew_log_time

            for open_ticker in list(self._open_buckets.keys()):
                bucket = self._open_buckets[open_ticker]
                b_dt = bucket["minute_dt"]
                last_t_sec = self._last_tick_time.get(open_ticker, 0.0)
                # Only wall-clock finalize inside process_feed_message when the minute has elapsed + 2s grace
                # AND no index ticks have arrived for this ticker in the last 2.0s of wall-clock time
                if now >= (b_dt + timedelta(minutes=1, seconds=2)) and (now_ts - last_t_sec) >= 2.0:
                    candle = self._finalize_bucket(open_ticker, bucket, completion_time=now)
                    if candle:
                        finalized_candles.append(candle)
                    del self._open_buckets[open_ticker]

            # Handle non-feed frames (e.g. market_info or heartbeats)
            msg_type = msg.get("type")
            if msg_type == "market_info":
                logger.debug(f"[IndexIngestion] Received market_info transport frame @ {now.isoformat()}")
                return finalized_candles

            if not feeds:
                return finalized_candles

            for ikey, feed_obj in feeds.items():
                if ikey not in INDEX_KEYS:
                    continue
                ticker = INDEX_KEYS[ikey]
                full_feed = feed_obj.get("fullFeed") or {}
                
                index_ff = full_feed.get("indexFF") or full_feed.get("marketFF") or {}
                ltpc = index_ff.get("ltpc") or {}
                raw_ltp = ltpc.get("ltp")
                if raw_ltp is None:
                    continue

                try:
                    ltp = float(raw_ltp)
                except (ValueError, TypeError):
                    continue

                if ltp <= 0:
                    continue

                # 3. Exchange Timestamp Validation & Skew Defense
                raw_ltt = ltpc.get("ltt")
                ltt_ms = int(raw_ltt) if raw_ltt is not None else int(now_ts * 1000)
                ltt_sec = ltt_ms / 1000.0
                
                # Check for future timestamp (exceeding tolerance)
                if (ltt_sec - now_ts) > FUTURE_TICK_TOLERANCE_SEC:
                    logger.warning(f"[IndexIngestion] Rejected future tick for {ticker}: exch_ts={ltt_sec}, local_ts={now_ts}")
                    continue

                # Check clock skew (rate-limit log spam to once per 30s per ticker)
                skew = abs(now_ts - ltt_sec)
                if skew > MAX_CLOCK_SKEW_SEC:
                    self._skew_warnings_count += 1
                    if (now_ts - last_skew_log_map.get(ticker, 0.0)) >= 30.0:
                        last_skew_log_map[ticker] = now_ts
                        logger.warning(f"[IndexIngestion] Clock skew warning for {ticker}: {skew:.2f}s divergence.")

                # Out-of-order check
                last_ltt = self._last_tick_timestamp_ms.get(ticker, 0)
                if ltt_ms < last_ltt:
                    self._out_of_order_count += 1
                    logger.debug(f"[IndexIngestion] Out-of-order tick discarded for {ticker}: {ltt_ms} < {last_ltt}")
                    continue

                self._last_tick_timestamp_ms[ticker] = ltt_ms
                self._last_tick_time[ticker] = now_ts
                self._total_ticks_processed += 1

                # 4. Check for Provider-Supplied I1 Candle
                provider_i1 = None
                market_ohlc = index_ff.get("marketOHLC") or {}
                ohlc_list = market_ohlc.get("ohlc") or []
                for c in ohlc_list:
                    if c.get("interval") == "I1":
                        try:
                            raw_c_ts = c.get("ts")
                            c_ts_ms = int(raw_c_ts) if raw_c_ts is not None else ltt_ms
                            c_dt = datetime.fromtimestamp(c_ts_ms / 1000.0)
                            provider_i1 = {
                                "open": float(c.get("open") or ltp),
                                "high": float(c.get("high") or ltp),
                                "low": float(c.get("low") or ltp),
                                "close": float(c.get("close") or ltp),
                                "volume": int(c.get("vol") or 0),
                                "ts_dt": c_dt,
                                "ts_str": c_dt.strftime("%Y-%m-%d %H:%M:00")
                            }
                        except Exception as e:
                            logger.debug(f"[IndexIngestion] Error parsing I1 candle: {e}")
                        break

                # 5. Minute Bucket Aggregator (Provisional In-Flight Bucket)
                tick_dt = datetime.fromtimestamp(ltt_sec)
                curr_min_str = tick_dt.strftime("%Y-%m-%d %H:%M:00")
                # Guard against late straggler ticks reopening an already-finalized minute
                last_fin_min = last_finalized_map.get(ticker)
                if last_fin_min and curr_min_str <= last_fin_min:
                    continue

                bucket = self._open_buckets.get(ticker)

                if bucket is None:
                    # Initialize provisional bucket
                    self._open_buckets[ticker] = {
                        "minute_str": curr_min_str,
                        "minute_dt": tick_dt.replace(second=0, microsecond=0),
                        "open": ltp,
                        "high": ltp,
                        "low": ltp,
                        "close": ltp,
                        "tick_count": 1,
                        "provider_i1": provider_i1,
                        "last_price": ltp,
                        "received_at": now.isoformat()
                    }
                elif bucket["minute_str"] == curr_min_str:
                    # Update active in-flight bucket
                    bucket["high"] = max(bucket["high"], ltp)
                    bucket["low"] = min(bucket["low"], ltp)
                    bucket["close"] = ltp
                    bucket["tick_count"] += 1
                    bucket["last_price"] = ltp
                    if provider_i1:
                        bucket["provider_i1"] = provider_i1
                elif curr_min_str > bucket["minute_str"]:
                    # A tick from a later minute arrived -> finalize the prior bucket
                    prior_candle = self._finalize_bucket(ticker, bucket, completion_time=now)
                    if prior_candle:
                        finalized_candles.append(prior_candle)

                    # Start new provisional bucket
                    self._open_buckets[ticker] = {
                        "minute_str": curr_min_str,
                        "minute_dt": tick_dt.replace(second=0, microsecond=0),
                        "open": ltp,
                        "high": ltp,
                        "low": ltp,
                        "close": ltp,
                        "tick_count": 1,
                        "provider_i1": provider_i1,
                        "last_price": ltp,
                        "received_at": now.isoformat()
                    }

        return finalized_candles

    # ──────────────────────────────────────────────────────────────────────────
    # SILENT WALL-CLOCK MINUTE FINALIZATION
    # ──────────────────────────────────────────────────────────────────────────

    def check_and_finalize_elapsed_minutes(self, wall_clock_time: datetime) -> List[Dict[str, Any]]:
        """
        Finalizes any open buckets whose minute has elapsed (wall_clock_time >= bucket_minute + 60s).
        Enables deterministic completion during periods with zero incoming ticks.
        """
        finalized: List[Dict[str, Any]] = []
        with self._lock:
            for ticker in list(self._open_buckets.keys()):
                bucket = self._open_buckets[ticker]
                b_dt = bucket["minute_dt"]
                
                # A 1-minute candle starting at b_dt completes at b_dt + 60s
                if wall_clock_time >= (b_dt + timedelta(minutes=1)):
                    candle = self._finalize_bucket(ticker, bucket, completion_time=wall_clock_time)
                    if candle:
                        finalized.append(candle)
                    del self._open_buckets[ticker]

        return finalized

    # ──────────────────────────────────────────────────────────────────────────
    # CONFLICT RESOLUTION & PRECEDENCE HIERARCHY
    # ──────────────────────────────────────────────────────────────────────────

    def _finalize_bucket(self, ticker: str, bucket: Dict[str, Any], completion_time: datetime) -> Optional[Dict[str, Any]]:
        """
        Finalizes an elapsed bucket into a completed 1-minute OHLCV bar with deterministic precedence.
        
        PRECEDENCE HIERARCHY:
          1. Provider I1: Authoritative if confirmed complete (minute ended).
          2. Local Aggregated: Used if Provider I1 omitted.
          
        Preserves received_at, available_at, provider and local values, tick counts, and discrepancy flags.
        """
        minute_str = bucket["minute_str"]
        p_i1 = bucket.get("provider_i1")
        
        local_o = bucket["open"]
        local_h = bucket["high"]
        local_l = bucket["low"]
        local_c = bucket["close"]
        t_count = bucket["tick_count"]
        candle_end_dt = bucket["minute_dt"] + timedelta(minutes=1)
        rec_at = bucket.get("received_at", candle_end_dt.isoformat())
        avail_at = candle_end_dt.isoformat()

        # Conflict resolution and discrepancy check
        discrepancy = 0
        if p_i1 and p_i1["ts_str"] == minute_str:
            p_o = p_i1["open"]
            p_h = p_i1["high"]
            p_l = p_i1["low"]
            p_c = p_i1["close"]
            p_vol = p_i1["volume"]

            max_diff = max(abs(p_o - local_o), abs(p_h - local_h), abs(p_l - local_l), abs(p_c - local_c))
            if max_diff > DISCREPANCY_TOLERANCE_PTS:
                discrepancy = 1
                self._discrepancy_count += 1
                logger.info(
                    f"[IndexIngestion] Data discrepancy noted for {ticker} @ {minute_str}: "
                    f"diff={max_diff:.2f}pts (Prov close={p_c}, Local close={local_c})"
                )

            candle = {
                "ticker": ticker,
                "timestamp": minute_str,
                "open": p_o,
                "high": p_h,
                "low": p_l,
                "close": p_c,
                "volume": p_vol,
                "tick_count": t_count,
                "source": "PROVIDER_I1",
                "is_complete": 1,
                "is_synthetic": 0,
                "is_gap": 0,
                "provider_open": p_o,
                "provider_high": p_h,
                "provider_low": p_l,
                "provider_close": p_c,
                "local_open": local_o,
                "local_high": local_h,
                "local_low": local_l,
                "local_close": local_c,
                "discrepancy_flag": discrepancy,
                "received_at": rec_at,
                "available_at": avail_at,
                "reconciled_at": None,
                "version": 1,
                "is_point_in_time": 1
            }
        else:
            candle = {
                "ticker": ticker,
                "timestamp": minute_str,
                "open": local_o,
                "high": local_h,
                "low": local_l,
                "close": local_c,
                "volume": 0,
                "tick_count": t_count,
                "source": "LOCAL_AGGREGATED",
                "is_complete": 1,
                "is_synthetic": 0,
                "is_gap": 0,
                "provider_open": None,
                "provider_high": None,
                "provider_low": None,
                "provider_close": None,
                "local_open": local_o,
                "local_high": local_h,
                "local_low": local_l,
                "local_close": local_c,
                "discrepancy_flag": 0,
                "received_at": rec_at,
                "available_at": avail_at,
                "reconciled_at": None,
                "version": 1,
                "is_point_in_time": 1
            }

        last_finalized_map = getattr(self, "_last_finalized_minute", None)
        if last_finalized_map is None:
            self._last_finalized_minute = {}
            last_finalized_map = self._last_finalized_minute
        if minute_str > last_finalized_map.get(ticker, ""):
            last_finalized_map[ticker] = minute_str

        self._persist_candle(candle)
        self._completed_bars[ticker].append(candle)
        return candle

    def handle_quiescent_market(
        self,
        ticker: str,
        current_minute: datetime,
        transport_override: Optional[bool] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Handles periods during trading hours where zero ticks arrived for an entire minute.
        DECOUPLED LOGIC:
          - If the WebSocket transport is CONNECTED and healthy:
            Zero ticks is an inactive/quiescent market, NOT a transport outage.
            Writes a FLAT_CARRY bar with is_synthetic = 1, is_gap = 0.
          - If the WebSocket transport is DISCONNECTED or timed out (> 30s):
            Writes a DATA_GAP bar with is_gap = 1, is_complete = 0, is_synthetic = 1.
        """
        with self._lock:
            t_time = current_minute.time()
            if t_time < MARKET_OPEN_TIME or t_time >= MARKET_CLOSE_TIME:
                return None

            now_ts = current_minute.timestamp()
            
            # Transport health determination
            if transport_override is not None:
                is_transport_healthy = transport_override
            else:
                is_transport_healthy = (
                    self._is_transport_connected and
                    self._status == "CONNECTED" and
                    (now_ts - self._last_transport_heartbeat) <= self.transport_heartbeat_timeout_sec
                )

            last_price = 0.0
            if self._completed_bars[ticker]:
                last_price = self._completed_bars[ticker][-1]["close"]
            elif ticker in self._open_buckets:
                last_price = self._open_buckets[ticker]["last_price"]

            if last_price <= 0:
                return None

            min_str = current_minute.strftime("%Y-%m-%d %H:%M:00")
            avail_at = current_minute.isoformat()

            if is_transport_healthy:
                # Healthy transport, quiescent market
                candle = {
                    "ticker": ticker,
                    "timestamp": min_str,
                    "open": last_price,
                    "high": last_price,
                    "low": last_price,
                    "close": last_price,
                    "volume": 0,
                    "tick_count": 0,
                    "source": "FLAT_CARRY",
                    "is_complete": 1,
                    "is_synthetic": 1,
                    "is_gap": 0,
                    "provider_open": None,
                    "provider_high": None,
                    "provider_low": None,
                    "provider_close": None,
                    "local_open": None,
                    "local_high": None,
                    "local_low": None,
                    "local_close": None,
                    "discrepancy_flag": 0,
                    "received_at": avail_at,
                    "available_at": avail_at,
                    "reconciled_at": None,
                    "version": 1,
                    "is_point_in_time": 1
                }
            else:
                # Transport outage / disconnected gap
                candle = {
                    "ticker": ticker,
                    "timestamp": min_str,
                    "open": last_price,
                    "high": last_price,
                    "low": last_price,
                    "close": last_price,
                    "volume": 0,
                    "tick_count": 0,
                    "source": "DATA_GAP",
                    "is_complete": 0,
                    "is_synthetic": 1,
                    "is_gap": 1,
                    "provider_open": None,
                    "provider_high": None,
                    "provider_low": None,
                    "provider_close": None,
                    "local_open": None,
                    "local_high": None,
                    "local_low": None,
                    "local_close": None,
                    "discrepancy_flag": 0,
                    "received_at": avail_at,
                    "available_at": avail_at,
                    "reconciled_at": None,
                    "version": 1,
                    "is_point_in_time": 1
                }

            self._persist_candle(candle)
            self._completed_bars[ticker].append(candle)
            return candle

    def reconcile_historical_candle(
        self,
        ticker: str,
        timestamp: str,
        corrected_ohlcv: Dict[str, float],
        reconciled_at: datetime
    ) -> Dict[str, Any]:
        """
        Reconciles a historical candle post-session with strict point-in-time provenance.
        Preserves the original version as is_point_in_time = 0, and inserts the reconciled
        candle with version = original_version + 1 and reconciled_at.
        """
        with sqlite3.connect(self._db_path, timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            orig = conn.execute("""
                SELECT * FROM continuous_index_ohlcv
                WHERE ticker = ? AND timestamp = ?
                ORDER BY version DESC LIMIT 1
            """, (ticker, timestamp)).fetchone()

            if not orig:
                raise ValueError(f"No existing candle found to reconcile for {ticker} @ {timestamp}")

            orig_dict = dict(orig)
            new_version = orig_dict["version"] + 1

            # Demote prior versions to is_point_in_time = 0
            conn.execute("""
                UPDATE continuous_index_ohlcv
                SET is_point_in_time = 0
                WHERE ticker = ? AND timestamp = ?
            """, (ticker, timestamp))

            # Insert reconciled version
            conn.execute("""
                INSERT INTO continuous_index_ohlcv
                (ticker, timestamp, open, high, low, close, volume, tick_count, source, is_complete, is_synthetic, is_gap,
                 provider_open, provider_high, provider_low, provider_close, local_open, local_high, local_low, local_close,
                 discrepancy_flag, received_at, available_at, reconciled_at, version, is_point_in_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ticker, timestamp, corrected_ohlcv["open"], corrected_ohlcv["high"],
                corrected_ohlcv["low"], corrected_ohlcv["close"],
                int(corrected_ohlcv.get("volume", orig_dict["volume"])),
                orig_dict["tick_count"], "RECONCILED", 1, 0, 0,
                corrected_ohlcv.get("open"), corrected_ohlcv.get("high"),
                corrected_ohlcv.get("low"), corrected_ohlcv.get("close"),
                orig_dict.get("local_open"), orig_dict.get("local_high"),
                orig_dict.get("local_low"), orig_dict.get("local_close"),
                orig_dict.get("discrepancy_flag", 0),
                orig_dict["received_at"], orig_dict["available_at"],
                reconciled_at.isoformat(), new_version, 1
            ))
            conn.commit()

            return {
                "ticker": ticker,
                "timestamp": timestamp,
                "version": new_version,
                "reconciled_at": reconciled_at.isoformat(),
                "available_at": orig_dict["available_at"]
            }

    def _persist_candle(self, c: Dict[str, Any]) -> None:
        """Atomically persists candle into continuous_index_ohlcv without degrading multi-tick completed bars."""
        try:
            default_avail = (datetime.fromisoformat(c["timestamp"]) + timedelta(minutes=1)).isoformat() if c.get("timestamp") else datetime.now().isoformat()
            rec_at = c.get("received_at") or default_avail
            avail_at = c.get("available_at") or default_avail
            with sqlite3.connect(self._db_path, timeout=15.0) as conn:
                conn.execute("PRAGMA busy_timeout = 15000")
                if c.get("is_synthetic", 0) == 0 and c.get("is_gap", 0) == 0:
                    conn.execute("""
                        INSERT INTO continuous_index_ohlcv
                        (ticker, timestamp, open, high, low, close, volume, tick_count, source, is_complete, is_synthetic, is_gap,
                         provider_open, provider_high, provider_low, provider_close, local_open, local_high, local_low, local_close,
                         discrepancy_flag, received_at, available_at, reconciled_at, version, is_point_in_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(ticker, timestamp, version) DO UPDATE SET
                            open = CASE WHEN excluded.tick_count >= continuous_index_ohlcv.tick_count THEN excluded.open ELSE continuous_index_ohlcv.open END,
                            high = MAX(continuous_index_ohlcv.high, excluded.high),
                            low = MIN(continuous_index_ohlcv.low, excluded.low),
                            close = CASE WHEN excluded.tick_count >= continuous_index_ohlcv.tick_count THEN excluded.close ELSE continuous_index_ohlcv.close END,
                            volume = MAX(continuous_index_ohlcv.volume, excluded.volume),
                            tick_count = MAX(continuous_index_ohlcv.tick_count, excluded.tick_count),
                            source = CASE WHEN excluded.tick_count >= continuous_index_ohlcv.tick_count THEN excluded.source ELSE continuous_index_ohlcv.source END,
                            is_complete = MAX(continuous_index_ohlcv.is_complete, excluded.is_complete),
                            is_synthetic = 0,
                            is_gap = 0,
                            provider_open = COALESCE(excluded.provider_open, continuous_index_ohlcv.provider_open),
                            provider_high = COALESCE(excluded.provider_high, continuous_index_ohlcv.provider_high),
                            provider_low = COALESCE(excluded.provider_low, continuous_index_ohlcv.provider_low),
                            provider_close = COALESCE(excluded.provider_close, continuous_index_ohlcv.provider_close),
                            local_open = COALESCE(excluded.local_open, continuous_index_ohlcv.local_open),
                            local_high = MAX(COALESCE(continuous_index_ohlcv.local_high, excluded.local_high), COALESCE(excluded.local_high, continuous_index_ohlcv.local_high)),
                            local_low = MIN(COALESCE(continuous_index_ohlcv.local_low, excluded.local_low), COALESCE(excluded.local_low, continuous_index_ohlcv.local_low)),
                            local_close = COALESCE(excluded.local_close, continuous_index_ohlcv.local_close),
                            discrepancy_flag = MAX(continuous_index_ohlcv.discrepancy_flag, excluded.discrepancy_flag),
                            available_at = MIN(continuous_index_ohlcv.available_at, excluded.available_at),
                            is_point_in_time = MAX(continuous_index_ohlcv.is_point_in_time, excluded.is_point_in_time)
                    """, (
                        c["ticker"], c["timestamp"], c["open"], c["high"], c["low"], c["close"],
                        c["volume"], c["tick_count"], c["source"], c["is_complete"], c["is_synthetic"], c["is_gap"],
                        c.get("provider_open"), c.get("provider_high"), c.get("provider_low"), c.get("provider_close"),
                        c.get("local_open"), c.get("local_high"), c.get("local_low"), c.get("local_close"),
                        c.get("discrepancy_flag", 0),
                        rec_at,
                        avail_at,
                        c.get("reconciled_at"),
                        c.get("version", 1),
                        c.get("is_point_in_time", 1)
                    ))
                else:
                    conn.execute("""
                        INSERT OR REPLACE INTO continuous_index_ohlcv
                        (ticker, timestamp, open, high, low, close, volume, tick_count, source, is_complete, is_synthetic, is_gap,
                         provider_open, provider_high, provider_low, provider_close, local_open, local_high, local_low, local_close,
                         discrepancy_flag, received_at, available_at, reconciled_at, version, is_point_in_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        c["ticker"], c["timestamp"], c["open"], c["high"], c["low"], c["close"],
                        c["volume"], c["tick_count"], c["source"], c["is_complete"], c["is_synthetic"], c["is_gap"],
                        c.get("provider_open"), c.get("provider_high"), c.get("provider_low"), c.get("provider_close"),
                        c.get("local_open"), c.get("local_high"), c.get("local_low"), c.get("local_close"),
                        c.get("discrepancy_flag", 0),
                        rec_at,
                        avail_at,
                        c.get("reconciled_at"),
                        c.get("version", 1),
                        c.get("is_point_in_time", 1)
                    ))
                conn.commit()
        except Exception as e:
            logger.error(f"[IndexIngestion] Failed to persist candle for {c.get('ticker')}: {e}")

    def backfill_intraday_session_gaps(self) -> int:
        """
        Repairs missing or 1-tick flat bars in continuous_index_ohlcv for the current session
        using Upstox authoritative 1-minute intraday candles (strictly point-in-time for completed minutes).
        """
        repaired = 0
        try:
            import urllib.parse
            from app.data.upstox_provider import get_upstox_token, upstox_request, UPSTOX_BASE_URL
            token = get_upstox_token()
            if not token:
                return 0
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            }
            now = datetime.now()
            current_min_dt = now.replace(second=0, microsecond=0)

            for inst_key, ticker in INDEX_KEYS.items():
                encoded_key = urllib.parse.quote(inst_key, safe="")
                url = f"{UPSTOX_BASE_URL}/historical-candle/intraday/{encoded_key}/1minute"
                res = upstox_request("GET", url, headers=headers, timeout=5)
                if res.status_code != 200:
                    continue
                candles = res.json().get("data", {}).get("candles", [])
                if not candles:
                    continue
                with sqlite3.connect(self._db_path, timeout=15.0) as conn:
                    conn.execute("PRAGMA busy_timeout = 15000")
                    for row in candles:
                        if len(row) < 5:
                            continue
                        raw_ts, o_val, h_val, l_val, c_val = row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4])
                        vol_val = int(row[5]) if len(row) > 5 and row[5] is not None else 0
                        bar_dt = datetime.fromisoformat(str(raw_ts)).replace(tzinfo=None, second=0, microsecond=0)
                        if bar_dt >= current_min_dt:
                            continue  # Never backfill still-forming current minute
                        bar_str = bar_dt.strftime("%Y-%m-%d %H:%M:00")
                        avail_str = (bar_dt + timedelta(minutes=1)).isoformat()
                        conn.execute("""
                            INSERT INTO continuous_index_ohlcv
                            (ticker, timestamp, open, high, low, close, volume, tick_count, source, is_complete, is_synthetic, is_gap,
                             provider_open, provider_high, provider_low, provider_close, local_open, local_high, local_low, local_close,
                             discrepancy_flag, received_at, available_at, reconciled_at, version, is_point_in_time)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 100, 'PROVIDER_I1', 1, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, 1, 1)
                            ON CONFLICT(ticker, timestamp, version) DO UPDATE SET
                                open = CASE WHEN continuous_index_ohlcv.tick_count <= 1 THEN excluded.open ELSE continuous_index_ohlcv.open END,
                                high = MAX(continuous_index_ohlcv.high, excluded.high),
                                low = MIN(continuous_index_ohlcv.low, excluded.low),
                                close = CASE WHEN continuous_index_ohlcv.tick_count <= 1 THEN excluded.close ELSE continuous_index_ohlcv.close END,
                                tick_count = MAX(continuous_index_ohlcv.tick_count, excluded.tick_count),
                                source = CASE WHEN continuous_index_ohlcv.tick_count <= 1 THEN 'PROVIDER_I1' ELSE continuous_index_ohlcv.source END,
                                is_complete = 1,
                                is_synthetic = 0,
                                is_gap = 0,
                                provider_open = COALESCE(continuous_index_ohlcv.provider_open, excluded.provider_open),
                                provider_high = COALESCE(continuous_index_ohlcv.provider_high, excluded.provider_high),
                                provider_low = COALESCE(continuous_index_ohlcv.provider_low, excluded.provider_low),
                                provider_close = COALESCE(continuous_index_ohlcv.provider_close, excluded.provider_close),
                                available_at = MIN(continuous_index_ohlcv.available_at, excluded.available_at),
                                is_point_in_time = 1
                        """, (
                            ticker, bar_str, o_val, h_val, l_val, c_val, vol_val,
                            o_val, h_val, l_val, c_val, o_val, h_val, l_val, c_val,
                            avail_str, avail_str
                        ))
                        repaired += 1
                    conn.commit()
            if repaired > 0:
                logger.info(f"[IndexIngestion] Verified/repaired {repaired} completed 1m index candles via Upstox intraday feed.")
        except Exception as e:
            logger.debug(f"[IndexIngestion] Intraday session gap backfill note: {e}")
        return repaired

    def backfill_historical_session_gaps(self, from_date: str, to_date: str) -> int:
        """
        Repairs missing 1-minute index bars across historical dates [from_date, to_date]
        using Upstox authoritative 1-minute historical candles with strict point-in-time
        available_at = bar_timestamp + 60s.
        """
        repaired = 0
        try:
            import urllib.parse
            from app.data.upstox_provider import get_upstox_token, upstox_request, UPSTOX_BASE_URL
            token = get_upstox_token()
            if not token:
                return 0
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            }
            now = datetime.now()
            current_min_dt = now.replace(second=0, microsecond=0)

            for inst_key, ticker in INDEX_KEYS.items():
                encoded_key = urllib.parse.quote(inst_key, safe="")
                url = f"{UPSTOX_BASE_URL}/historical-candle/{encoded_key}/1minute/{to_date}/{from_date}"
                res = upstox_request("GET", url, headers=headers, timeout=10)
                if res.status_code != 200:
                    continue
                candles = res.json().get("data", {}).get("candles", [])
                if not candles:
                    continue
                with sqlite3.connect(self._db_path, timeout=15.0) as conn:
                    conn.execute("PRAGMA busy_timeout = 15000")
                    for row in candles:
                        if len(row) < 5:
                            continue
                        raw_ts, o_val, h_val, l_val, c_val = row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4])
                        vol_val = int(row[5]) if len(row) > 5 and row[5] is not None else 0
                        bar_dt = datetime.fromisoformat(str(raw_ts)).replace(tzinfo=None, second=0, microsecond=0)
                        if bar_dt >= current_min_dt:
                            continue
                        bar_str = bar_dt.strftime("%Y-%m-%d %H:%M:00")
                        avail_str = (bar_dt + timedelta(minutes=1)).isoformat()
                        conn.execute("""
                            INSERT INTO continuous_index_ohlcv
                            (ticker, timestamp, open, high, low, close, volume, tick_count, source, is_complete, is_synthetic, is_gap,
                             provider_open, provider_high, provider_low, provider_close, local_open, local_high, local_low, local_close,
                             discrepancy_flag, received_at, available_at, reconciled_at, version, is_point_in_time)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 100, 'PROVIDER_I1', 1, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, 1, 1)
                            ON CONFLICT(ticker, timestamp, version) DO UPDATE SET
                                open = CASE WHEN continuous_index_ohlcv.tick_count <= 1 THEN excluded.open ELSE continuous_index_ohlcv.open END,
                                high = MAX(continuous_index_ohlcv.high, excluded.high),
                                low = MIN(continuous_index_ohlcv.low, excluded.low),
                                close = CASE WHEN continuous_index_ohlcv.tick_count <= 1 THEN excluded.close ELSE continuous_index_ohlcv.close END,
                                tick_count = MAX(continuous_index_ohlcv.tick_count, excluded.tick_count),
                                source = CASE WHEN continuous_index_ohlcv.tick_count <= 1 THEN 'PROVIDER_I1' ELSE continuous_index_ohlcv.source END,
                                is_complete = 1,
                                is_synthetic = 0,
                                is_gap = 0,
                                provider_open = COALESCE(continuous_index_ohlcv.provider_open, excluded.provider_open),
                                provider_high = COALESCE(continuous_index_ohlcv.provider_high, excluded.provider_high),
                                provider_low = COALESCE(continuous_index_ohlcv.provider_low, excluded.provider_low),
                                provider_close = COALESCE(continuous_index_ohlcv.provider_close, excluded.provider_close),
                                available_at = MIN(continuous_index_ohlcv.available_at, excluded.available_at),
                                is_point_in_time = 1
                        """, (
                            ticker, bar_str, o_val, h_val, l_val, c_val, vol_val,
                            o_val, h_val, l_val, c_val, o_val, h_val, l_val, c_val,
                            avail_str, avail_str
                        ))
                        repaired += 1
                    conn.commit()
        except Exception as e:
            logger.debug(f"[IndexIngestion] Historical session gap backfill note: {e}")
        return repaired

    def heal_intraday_gaps_if_needed(
        self,
        as_of_time: Optional[datetime] = None,
        min_interval_sec: float = 45.0
    ) -> int:
        """
        Rate-limit-governed self-healing for 1-minute index bars:
          1. Finalizes any elapsed in-memory 1-minute buckets (0 REST calls).
          2. Enforces a strict cooldown (default 45s -> max 2 REST calls per 45s across NIFTY & BANKNIFTY)
             so Upstox API quota (64 req/min) is never pressured.
          3. Calls backfill_intraday_session_gaps() to repair any missing/gap 1m bars.
        """
        wall_dt = as_of_time or (datetime.now() - timedelta(seconds=2))
        try:
            self.check_and_finalize_elapsed_minutes(wall_dt)
        except Exception:
            pass

        now_mono = time.monotonic()
        with self._lock:
            last_heal = getattr(self, "_last_gap_heal_mono", 0.0)
            if (now_mono - last_heal) < min_interval_sec:
                return 0
            self._last_gap_heal_mono = now_mono

        return self.backfill_intraday_session_gaps()

    # ──────────────────────────────────────────────────────────────────────────
    # AUTHENTICATION & RECONNECT HANDLING
    # ──────────────────────────────────────────────────────────────────────────

    def handle_connect(self) -> None:
        """Records successful WebSocket transport connection."""
        with self._lock:
            self._is_transport_connected = True
            if self._status not in ("AUTH_EXPIRED", "CONFIG_ERROR"):
                self._status = "CONNECTED"
            self._last_transport_heartbeat = time.time()
            logger.info("[IndexIngestion] Transport connected successfully.")

    def handle_disconnect(self, reason: str = "Connection closed") -> None:
        """Records WebSocket transport disconnection, transitioning to DISCONNECTED state."""
        with self._lock:
            self._is_transport_connected = False
            if self._status not in ("AUTH_EXPIRED", "CONFIG_ERROR"):
                self._status = "DISCONNECTED"
            logger.warning(f"[IndexIngestion] Transport disconnected: {reason}")

    def record_transport_heartbeat(self, wall_clock_time: Optional[datetime] = None) -> None:
        """Records reception of a transport-level frame or ping/pong to maintain heartbeat."""
        with self._lock:
            now_ts = (wall_clock_time or datetime.now()).timestamp()
            self._last_transport_heartbeat = now_ts
            self._is_transport_connected = True
            if self._status not in ("AUTH_EXPIRED", "CONFIG_ERROR"):
                self._status = "CONNECTED"

    def handle_auth_error(self, err_msg: str) -> None:
        """Latches state to AUTH_EXPIRED and silences outbound traffic to protect credentials."""
        with self._lock:
            self._status = "AUTH_EXPIRED"
            self._is_transport_connected = False
            logger.critical(f"[IndexIngestion] 🚨 AUTHENTICATION EXPIRED: {err_msg}. Ingestion halted fail-closed.")

    def handle_reconnect(self, disconnect_duration_sec: float) -> Dict[str, Any]:
        """
        Assesses disconnect duration on reconnect.
        If outage > 60s, flags that historical backfill is required.
        """
        with self._lock:
            needs_backfill = disconnect_duration_sec > 60.0
            self._status = "CONNECTED"
            self._is_transport_connected = True
            return {
                "status": "RECONNECTED",
                "outage_seconds": disconnect_duration_sec,
                "needs_backfill": needs_backfill
            }

    # ──────────────────────────────────────────────────────────────────────────
    # STRICT INDICATOR WARM-UP ENFORCEMENT (POINT-IN-TIME CAUSAL INTEGRITY)
    # ──────────────────────────────────────────────────────────────────────────

    def get_valid_warmup_bars(
        self,
        ticker: str,
        session_date: str,
        as_of_time: datetime,
        required_bars: int = 28
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Returns strictly completed, valid same-session 1-minute bars for indicator calculations.
        INVARIANTS ENFORCED:
          1. Strictly same session (timestamp >= session_date 09:15:00 and timestamp < as_of_time).
          2. Strictly point-in-time: available_at <= as_of_time (excludes any future-reconciled data).
          3. Must have at least required_bars (e.g. 28 for Wilder ADX-14).
          4. ZERO provisional/still-forming bars permitted (is_complete must equal 1).
          5. ZERO synthetic bars permitted (if any bar has is_synthetic = 1, returns None).
          6. ZERO gap bars permitted (if any bar has is_gap = 1, returns None).
          7. Strict temporal continuity (sequential bars exactly 60s apart).
        """
        session_start = f"{session_date} 09:15:00"
        cutoff = as_of_time.strftime("%Y-%m-%d %H:%M:00")
        as_of_iso = as_of_time.isoformat()

        with sqlite3.connect(self._db_path, timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            # Point-in-time filter: only bars made available at or before as_of_time
            rows = conn.execute("""
                SELECT ticker, timestamp, open, high, low, close, volume, is_complete, is_synthetic, is_gap, available_at
                FROM continuous_index_ohlcv
                WHERE ticker = ?
                  AND timestamp >= ?
                  AND timestamp < ?
                  AND available_at <= ?
                  AND is_point_in_time = 1
                ORDER BY timestamp ASC
            """, (ticker, session_start, cutoff, as_of_iso)).fetchall()

        if len(rows) < required_bars:
            return None

        bars = [dict(r) for r in rows]

        # Invariant checks across the warm-up window
        recent_bars = bars[-required_bars:]
        for i, b in enumerate(recent_bars):
            # Check 1: Incomplete or provisional bars
            if b["is_complete"] != 1:
                return None
            # Check 2: Synthetic bars strictly disallowed from indicator warm-up
            if b["is_synthetic"] != 0:
                return None
            # Check 3: Gap bars strictly disallowed
            if b["is_gap"] != 0:
                return None
            # Check 4: Contiguity
            if i > 0:
                prev_dt = datetime.fromisoformat(recent_bars[i - 1]["timestamp"])
                curr_dt = datetime.fromisoformat(b["timestamp"])
                if (curr_dt - prev_dt).total_seconds() != 60.0:
                    return None

        return recent_bars


_INDEX_INGESTION_LOCK = globals().get("_INDEX_INGESTION_LOCK") or threading.RLock()
_INDEX_INGESTION_INSTANCE: Optional["IndexCandleIngestionPrototype"] = globals().get("_INDEX_INGESTION_INSTANCE")
if _INDEX_INGESTION_INSTANCE is not None:
    _INDEX_INGESTION_INSTANCE.__class__ = IndexCandleIngestionPrototype
    threading.Thread(target=_INDEX_INGESTION_INSTANCE.backfill_intraday_session_gaps, name="IndexGapBackfill", daemon=True).start()


def get_index_candle_ingestion_service(db_path: Optional[str] = None) -> "IndexCandleIngestionPrototype":
    """Thread-safe singleton getter for IndexCandleIngestionPrototype."""
    global _INDEX_INGESTION_INSTANCE
    with _INDEX_INGESTION_LOCK:
        if _INDEX_INGESTION_INSTANCE is None:
            from app.data.historical_data_layer import get_db_path
            target_db = db_path or get_db_path()
            _INDEX_INGESTION_INSTANCE = IndexCandleIngestionPrototype(db_path=target_db)
        return _INDEX_INGESTION_INSTANCE
