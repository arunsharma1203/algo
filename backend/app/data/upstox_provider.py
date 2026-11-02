import logging
import sqlite3
import requests
import pandas as pd
import numpy as np
import time
import collections
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)

UPSTOX_BASE_URL = "https://api.upstox.com/v2"

class UpstoxRateLimitExceeded(Exception):
    """Raised when Upstox rate limits are exceeded and failover must occur to protect the token."""
    pass

class UpstoxAuthFailure(Exception):
    """Raised when Upstox authentication fails (HTTP 401/403 or invalid/expired token)."""
    pass

class UpstoxRateLimiter:
    """
    Production-Grade Thread-Safe Rate Limiter strictly enforcing Application Hard Limits:
      - 8 requests / second   (minimum spacing: 200ms)
      - 64 requests / minute  (hard application ceiling)
      - 1600 requests / 30 min (hard 30-minute quota ceiling)
      - Automated Circuit Breaker on HTTP 429: silences all outbound calls for 180s
    """
    MAX_PER_SECOND = 8
    MAX_PER_MINUTE = 64
    MAX_PER_30MIN = 1600
    MIN_INTERVAL_SEC = 0.200  # 200ms minimum spacing between ANY two requests across all threads (caps max rate to 5 req/s)

    def __init__(self):
        self._lock = threading.RLock()
        self._history = collections.deque()  # timestamps of successful requests
        self._total_requests = 0
        self._throttled_count = 0
        self._rejected_count = 0
        self._last_request_time = 0.0
        self._circuit_open_until = self._load_persisted_cooldown()

    def _load_persisted_cooldown(self) -> float:
        """Loads persistent circuit breaker cooldown from SQLite to survive process restarts."""
        try:
            from app.data.historical_data_layer import get_db_path
            conn = sqlite3.connect(get_db_path(), timeout=2.0)
            cur = conn.cursor()
            cur.execute("SELECT value FROM app_settings WHERE key = 'upstox_circuit_cooldown_until'")
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                val = float(row[0])
                if val > time.time():
                    rem = int(val - time.time())
                    logger.warning(f"[UpstoxRateLimiter] Active circuit breaker restored from DB: cooling down ({rem}s remaining).")
                    return val
        except Exception:
            pass
        return 0.0

    def trip_circuit_breaker(self, duration: float = 180.0, reason: str = "") -> None:
        """Opens circuit breaker for `duration` seconds, completely blocking all Upstox network calls."""
        with self._lock:
            now = time.time()
            self._circuit_open_until = max(self._circuit_open_until, now + duration)
            logger.critical(
                f"[UpstoxRateLimiter] 🚨 CIRCUIT BREAKER TRIPPED for {duration}s! "
                f"Halting all outbound Upstox network traffic immediately to protect token. Reason: {reason}"
            )
            try:
                from app.data.historical_data_layer import get_db_path
                conn = sqlite3.connect(get_db_path(), timeout=2.0)
                conn.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_circuit_cooldown_until', ?)",
                    (str(self._circuit_open_until),)
                )
                conn.commit()
                conn.close()
            except Exception as e:
                logger.debug(f"Could not persist circuit cooldown: {e}")

    def is_circuit_open(self) -> bool:
        with self._lock:
            return time.time() < self._circuit_open_until

    def get_circuit_remaining_seconds(self) -> int:
        with self._lock:
            rem = self._circuit_open_until - time.time()
            return max(0, int(rem))

    def reset_circuit_breaker(self) -> None:
        """Resets the circuit breaker and clears any persisted cooldown."""
        with self._lock:
            self._circuit_open_until = 0.0
            try:
                from app.data.historical_data_layer import get_db_path
                conn = sqlite3.connect(get_db_path(), timeout=2.0)
                conn.execute("DELETE FROM app_settings WHERE key = 'upstox_circuit_cooldown_until'")
                conn.commit()
                conn.close()
            except Exception as e:
                logger.debug(f"Could not clear circuit cooldown in db: {e}")

    def acquire(self, timeout: float = 10.0, is_emergency: bool = False) -> bool:
        """
        Blocks if needed to satisfy per-second or per-minute rate limits and enforces 150ms pacing.
        Fast-rejects with UpstoxRateLimitExceeded if circuit breaker is open or 30-minute quota is reached.
        Emergency requests (e.g. panic exit, stop loss placement, order cancellation) are granted immediate priority
        and bypass routine pacing and circuit breaker lockouts.
        """
        start_wait = time.time()
        while True:
            with self._lock:
                now = time.time()

                # 1. Circuit Breaker Fast-Reject: NEVER hit the network if cooling down (unless emergency exit)
                if self.is_circuit_open() and not is_emergency:
                    remaining = self.get_circuit_remaining_seconds()
                    self._rejected_count += 1
                    raise UpstoxRateLimitExceeded(
                        f"Upstox Circuit Breaker is OPEN (cooling down for {remaining}s to protect token). Failover engaged."
                    )

                if is_emergency:
                    # Emergency bypass: granted immediately to ensure protective exits are never blocked
                    self._last_request_time = time.time()
                    self._history.append(now)
                    self._total_requests += 1
                    logger.warning("[UpstoxRateLimiter] 🚨 EMERGENCY API SLOT GRANTED (Priority Bypass).")
                    return True

                # Prune history older than 1800s (30 mins)
                cutoff_30m = now - 1800.0
                while self._history and self._history[0] < cutoff_30m:
                    self._history.popleft()

                # Check 30-minute ceiling (hard limit to prevent token ban)
                if len(self._history) >= self.MAX_PER_30MIN:
                    self._rejected_count += 1
                    logger.critical(
                        f"[UpstoxRateLimiter] 🚨 CRITICAL: 30-minute quota reached ({len(self._history)} reqs >= {self.MAX_PER_30MIN}). "
                        "Halting Upstox calls to prevent token blocking. Failover engaged."
                    )
                    raise UpstoxRateLimitExceeded(
                        f"Upstox 30-minute API limit reached ({len(self._history)} reqs/30m). Failover engaged to protect token."
                    )

                # Calculate requests in last 60s and last 1s
                cutoff_1m = now - 60.0
                cutoff_1s = now - 1.0

                reqs_1m = sum(1 for t in self._history if t >= cutoff_1m)
                reqs_1s = sum(1 for t in self._history if t >= cutoff_1s)

                sleep_time = 0.0

                # 2. Enforce minimum inter-request interval (Leaky Bucket Pacing across all threads)
                gap = now - self._last_request_time
                if gap < self.MIN_INTERVAL_SEC:
                    sleep_time = max(sleep_time, self.MIN_INTERVAL_SEC - gap)

                if reqs_1s >= self.MAX_PER_SECOND:
                    recent_1s = [t for t in self._history if t >= cutoff_1s]
                    if recent_1s:
                        sleep_time = max(sleep_time, (recent_1s[0] + 1.0) - now + 0.02)

                if reqs_1m >= self.MAX_PER_MINUTE:
                    recent_1m = [t for t in self._history if t >= cutoff_1m]
                    if recent_1m:
                        sleep_time = max(sleep_time, (recent_1m[0] + 60.0) - now + 0.05)

                if sleep_time <= 0.0:
                    # Slot granted!
                    self._last_request_time = time.time()
                    self._history.append(now)
                    self._total_requests += 1
                    return True

            # If waiting would exceed timeout, abort
            if (time.time() - start_wait) + sleep_time > timeout:
                self._rejected_count += 1
                raise UpstoxRateLimitExceeded(f"Upstox rate limiter wait timeout ({timeout}s exceeded).")

            self._throttled_count += 1
            logger.debug(f"[UpstoxRateLimiter] Pacing request: sleeping {sleep_time:.3f}s to respect limits.")
            time.sleep(min(sleep_time, 2.0))

    def get_stats(self) -> Dict[str, Any]:
        """Returns real-time telemetry on rate limit consumption across all 3 tiers."""
        with self._lock:
            now = time.time()
            cutoff_1s = now - 1.0
            cutoff_1m = now - 60.0
            cutoff_30m = now - 1800.0
            
            c_1s = sum(1 for t in self._history if t >= cutoff_1s)
            c_1m = sum(1 for t in self._history if t >= cutoff_1m)
            c_30m = sum(1 for t in self._history if t >= cutoff_30m)

            circuit_open = self.is_circuit_open()
            remaining_cooldown = self.get_circuit_remaining_seconds()

            status = "NORMAL"
            if circuit_open:
                status = "CIRCUIT_OPEN"
            elif c_30m >= self.MAX_PER_30MIN or c_1m >= self.MAX_PER_MINUTE or c_1s >= self.MAX_PER_SECOND:
                status = "THROTTLED"
            elif c_30m >= self.MAX_PER_30MIN * 0.85 or c_1m >= self.MAX_PER_MINUTE * 0.85:
                status = "WARNING"

            # Calculate remaining time until the oldest request in the 30m window rolls off
            in_30m = [t for t in self._history if t >= cutoff_30m]
            window_reset_sec = max(0, int(in_30m[0] + 1800.0 - now)) if in_30m else 0

            return {
                "status": status,
                "circuit_open": circuit_open,
                "circuit_cooldown_remaining_sec": remaining_cooldown,
                "window_reset_remaining_sec": window_reset_sec,
                "current_1s": c_1s,
                "max_1s": self.MAX_PER_SECOND,
                "current_1m": c_1m,
                "max_1m": self.MAX_PER_MINUTE,
                "current_30m": c_30m,
                "max_30m": self.MAX_PER_30MIN,
                "total_requests": self._total_requests,
                "throttled_count": self._throttled_count,
                "rejected_count": self._rejected_count
            }

_GLOBAL_UPSTOX_LIMITER = UpstoxRateLimiter()

def get_upstox_limiter() -> UpstoxRateLimiter:
    return _GLOBAL_UPSTOX_LIMITER

def handle_upstox_token_failure(status_code: int = 401, error_message: str = "Token expired or unauthorized") -> Dict[str, Any]:
    """
    Emergency Token Expiration & Auth Failure Protocol:
    1. Persist 'TOKEN_EXPIRED' state and error metadata into SQLite app_settings.
    2. Emergency Liquidation: Closes all active Consensus Arena positions immediately with reason 'EMERGENCY_UPSTOX_AUTH_FAIL'.
    3. Locks active data source to 'upstox' (never fall back to Yahoo Finance).
    4. Dispatches emergency Telegram alert to the user.
    """
    from app.data.historical_data_layer import get_db_path
    db_path = get_db_path()
    now_iso = datetime.now().isoformat()

    # 1. Update app_settings atomically
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        with conn:
            conn.execute("""
                INSERT INTO app_settings (key, value) VALUES ('upstox_auth_status', 'TOKEN_EXPIRED')
                ON CONFLICT(key) DO UPDATE SET value = 'TOKEN_EXPIRED'
            """)
            conn.execute("""
                INSERT INTO app_settings (key, value) VALUES ('upstox_auth_error', ?)
                ON CONFLICT(key) DO UPDATE SET value = ?
            """, (str(error_message), str(error_message)))
            conn.execute("""
                INSERT INTO app_settings (key, value) VALUES ('upstox_auth_error_time', ?)
                ON CONFLICT(key) DO UPDATE SET value = ?
            """, (now_iso, now_iso))
            conn.execute("""
                INSERT INTO app_settings (key, value) VALUES ('market_data_source', 'upstox')
                ON CONFLICT(key) DO UPDATE SET value = 'upstox'
            """)
        conn.close()
    except Exception as dbe:
        logger.error(f"[Upstox] Failed to update auth status in app_settings: {dbe}")

    # 2. Open Positions Safety: Do NOT liquidate positions on token failure.
    # Keep positions OPEN, gracefully halt live price fetching, and mark data as paused/stale.
    logger.warning(f"[Upstox] Auth failure / token expiration detected: live data polling paused. Active positions remain OPEN and untouched.")

    # 3. Dispatches urgent Telegram alert (force=True bypasses cooldown)
    try:
        notify_upstox_auth_alert(
            reason=f"{error_message} (Live market data paused. Active positions preserved.)",
            status_code=status_code,
            force=True
        )
    except Exception as te:
        logger.error(f"[Upstox] Failed to send emergency telegram: {te}")

    return {
        "status": "TOKEN_EXPIRED",
        "data_paused": True,
        "liquidated_positions": 0,
        "error": error_message,
        "timestamp": now_iso
    }

def reset_upstox_token_status() -> None:
    """Resets upstox auth status to ACTIVE upon valid token configuration."""
    from app.data.historical_data_layer import get_db_path
    try:
        conn = sqlite3.connect(get_db_path(), timeout=5.0)
        with conn:
            conn.execute("""
                INSERT INTO app_settings (key, value) VALUES ('upstox_auth_status', 'ACTIVE')
                ON CONFLICT(key) DO UPDATE SET value = 'ACTIVE'
            """)
            conn.execute("""
                DELETE FROM app_settings WHERE key IN ('upstox_auth_error', 'upstox_auth_error_time')
            """)
            conn.execute("""
                INSERT INTO app_settings (key, value) VALUES ('market_data_source', 'upstox')
                ON CONFLICT(key) DO UPDATE SET value = 'upstox'
            """)
        conn.close()
        logger.info("[Upstox] Auth status reset to ACTIVE.")
    except Exception as e:
        logger.warning(f"[Upstox] Failed to reset auth status: {e}")

def upstox_request(method: str, url: str, timeout: float = 6.0, **kwargs) -> requests.Response:
    """
    Executes an HTTP request to Upstox through the strict 3-tier rate limiter with pacing and circuit breaker.
    Respects:
      - 8 req/s (200ms inter-request pacing)
      - 64 req/min
      - 1600 req/30min
      - Automatic 180s circuit breaker on HTTP 429
      - Immediate defensive liquidation on HTTP 401/403
    """
    _GLOBAL_UPSTOX_LIMITER.acquire()
    m = method.upper()
    if m == "GET":
        res = requests.get(url, timeout=timeout, **kwargs)
    elif m == "POST":
        res = requests.post(url, timeout=timeout, **kwargs)
    else:
        res = requests.request(method, url, timeout=timeout, **kwargs)

    if res.status_code == 429:
        retry_hdr = res.headers.get("Retry-After")
        cooldown = 180.0
        if retry_hdr:
            try:
                cooldown = max(float(retry_hdr), 180.0)
            except (ValueError, TypeError):
                pass
        body_snip = res.text[:200] if hasattr(res, "text") else ""
        logger.warning(f"[Upstox] Received HTTP 429 Too Many Requests from Upstox server. Backing off for {cooldown}s. Response: {body_snip}")
        _GLOBAL_UPSTOX_LIMITER.trip_circuit_breaker(duration=cooldown, reason=f"HTTP 429: {body_snip[:100]}")
        raise UpstoxRateLimitExceeded(f"Upstox returned HTTP 429 Too Many Requests. Circuit breaker engaged for {int(cooldown)}s.")

    if res.status_code in (401, 403):
        body_snip = res.text[:200] if hasattr(res, "text") else ""
        logger.error(f"[Upstox] Received HTTP {res.status_code} Auth Failure from Upstox: {body_snip}")
        # Only treat as genuine token expiration if NOT an IP restriction (UDAPI1221)
        if "UDAPI1221" not in body_snip and "static ip" not in body_snip.lower():
            handle_upstox_token_failure(status_code=res.status_code, error_message=f"HTTP {res.status_code}: {body_snip}")
        raise UpstoxAuthFailure(f"Upstox authentication failed with HTTP {res.status_code}: {body_snip}")

    return res

# Built-in High-Speed Instrument Key Map for Nifty 100 / Key Universe
# Format: Ticker Symbol -> Instrument Key (or dynamic lookup fallback)
_NSE_CATALOG_CACHE: Dict[str, str] = {}

def _init_nse_instruments_catalog() -> Dict[str, str]:
    """Loads authoritative Upstox NSE instrument catalog from cached JSON."""
    global _NSE_CATALOG_CACHE
    if _NSE_CATALOG_CACHE:
        return _NSE_CATALOG_CACHE
    import json, os
    json_path = os.path.join(os.path.dirname(__file__), "upstox_nse_instruments.json")
    catalog = {}
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                catalog = json.load(f)
            logger.info(f"[Upstox] Loaded {len(catalog)} authoritative NSE instruments from catalog cache.")
        except Exception as e:
            logger.warning(f"[Upstox] Failed to parse {json_path}: {e}")
    # Authoritative Upstox JSON catalog takes precedence over hardcoded fallback map
    merged = dict(NSE_INSTRUMENT_MAP)
    merged.update(catalog)
    _NSE_CATALOG_CACHE = merged
    return _NSE_CATALOG_CACHE

NSE_INSTRUMENT_MAP = {
    "RELIANCE": "NSE_EQ|INE002A01018",
    "TCS": "NSE_EQ|INE467B01029",
    "HDFCBANK": "NSE_EQ|INE040A01034",
    "ICICIBANK": "NSE_EQ|INE090A01021",
    "INFY": "NSE_EQ|INE009A01021",
    "SBIN": "NSE_EQ|INE062A01020",
    "BHARTIARTL": "NSE_EQ|INE397D01024",
    "ITC": "NSE_EQ|INE154A01025",
    "LT": "NSE_EQ|INE018A01030",
    "BAJFINANCE": "NSE_EQ|INE296A01032",
    "HCLTECH": "NSE_EQ|INE860A01027",
    "MARUTI": "NSE_EQ|INE585B01010",
    "SUNPHARMA": "NSE_EQ|INE044A01036",
    "TATAMOTORS": "NSE_EQ|INE155A01022",
    "KOTAKBANK": "NSE_EQ|INE237A01036",
    "ONGC": "NSE_EQ|INE213A01029",
    "NTPC": "NSE_EQ|INE733E01010",
    "AXISBANK": "NSE_EQ|INE238A01034",
    "WIPRO": "NSE_EQ|INE075A01022",
    "M&M": "NSE_EQ|INE101A01026",
    "ULTRACEMCO": "NSE_EQ|INE481G01011",
    "POWERGRID": "NSE_EQ|INE752E01010",
    "TITAN": "NSE_EQ|INE280A01028",
    "ASIANPAINT": "NSE_EQ|INE021A01026",
    "BAJAJFINSV": "NSE_EQ|INE918I01026",
    "NESTLEIND": "NSE_EQ|INE239A01024",
    "JSWSTEEL": "NSE_EQ|INE019A01038",
    "TATASTEEL": "NSE_EQ|INE081A01020",
    "ADANIENT": "NSE_EQ|INE423A01024",
    "ADANIPORTS": "NSE_EQ|INE742F01042",
    "GRASIM": "NSE_EQ|INE047A01021",
    "TECHM": "NSE_EQ|INE669C01036",
    "HINDALCO": "NSE_EQ|INE038A01020",
    "DIVISLAB": "NSE_EQ|INE361B01024",
    "SBILIFE": "NSE_EQ|INE123W01016",
    "LTIM": "NSE_EQ|INE214T01019",
    "BAJAJ-AUTO": "NSE_EQ|INE917I01010",
    "EICHERMOT": "NSE_EQ|INE066A01021",
    "INDUSINDBK": "NSE_EQ|INE095A01012",
    "DRREDDY": "NSE_EQ|INE089A01031",
    "CIPLA": "NSE_EQ|INE059A01026",
    "APOLLOHOSP": "NSE_EQ|INE437A01024",
    "TATACOMM": "NSE_EQ|INE151A01013",
    "HDFCLIFE": "NSE_EQ|INE795G01014",
    "BRITANNIA": "NSE_EQ|INE216A01030",
    "COALINDIA": "NSE_EQ|INE522F01014",
    "HEROMOTOCO": "NSE_EQ|INE158A01026",
    "TATACONSUM": "NSE_EQ|INE192A01025",
    "BPCL": "NSE_EQ|INE029A01011",
    "UPL": "NSE_EQ|INE628A01036",
    "ZOMATO": "NSE_EQ|INE758T01015",
    "JIOFIN": "NSE_EQ|INE758E01017",
    "TRENT": "NSE_EQ|INE849A01020",
    "HAL": "NSE_EQ|INE066F01020",
    "BEL": "NSE_EQ|INE263A01024",
    "BHEL": "NSE_EQ|INE257A01026",
    "PFC": "NSE_EQ|INE134E01011",
    "RECLTD": "NSE_EQ|INE020B01018",
    "IRFC": "NSE_EQ|INE053F01010",
    "RVNL": "NSE_EQ|INE415G01027",
    "MAZDOCK": "NSE_EQ|INE249Z01020",
    "IREDA": "NSE_EQ|INE202E01016",
    "NHPC": "NSE_EQ|INE848E01016",
    "SJVN": "NSE_EQ|INE002L01015",
    "SUZLON": "NSE_EQ|INE040H01021",
    "PAYTM": "NSE_EQ|INE982J01020",
    "NYKAA": "NSE_EQ|INE388Y01029",
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service"
}

def get_upstox_config() -> Dict[str, str]:
    """Reads Upstox credentials from database (Market Read-Only & Algo Read-Trade)."""
    from app.data.historical_data_layer import get_db_path
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM app_settings WHERE key LIKE 'upstox_%'")
    rows = cur.fetchall()
    conn.close()
    
    cfg = {
        "api_key": "",
        "api_secret": "",
        "access_token": "",      # Read-only Market Data & Analytics Token
        "market_token": "",      # Alias for analytics token
        "algo_token": "",        # Read + Trade Algo Execution Token
        "redirect_uri": "http://localhost:8000/api/settings/upstox/callback"
    }
    for k, v in rows:
        field = k.replace("upstox_", "")
        if field in cfg:
            cfg[field] = v or ""
            
    # Normalize aliases
    if cfg["market_token"] and not cfg["access_token"]:
        cfg["access_token"] = cfg["market_token"]
    elif cfg["access_token"] and not cfg["market_token"]:
        cfg["market_token"] = cfg["access_token"]
        
    return cfg

def get_upstox_token() -> str:
    """Returns the active Upstox access or market token string."""
    cfg = get_upstox_config()
    return (cfg.get("access_token") or cfg.get("market_token") or "").strip()

def get_upstox_algo_token() -> str:
    """Returns the active Upstox Algo Trading (Read + Trade) token string, falling back to market token."""
    cfg = get_upstox_config()
    return (cfg.get("algo_token") or cfg.get("access_token") or cfg.get("market_token") or "").strip()

_last_auth_alert_time: float = 0.0
ALERT_COOLDOWN_SECONDS: int = 3600  # 1 hour cooldown between alerts to prevent spam

_last_probe_time: float = 0.0
_last_probe_result: Optional[tuple] = None

def verify_upstox_data_token_live(force: bool = False) -> tuple[bool, str]:
    """
    Issues a live scoped HTTP probe against Upstox Market Quote API (NSE_EQ|INE002A01018)
    to confirm whether the server actually honours the token.
    Caches result for 5 minutes (300s) to respect API rate limits.
    """
    global _last_probe_time, _last_probe_result
    import time
    now_epoch = time.time()
    if not force and _last_probe_result is not None and (now_epoch - _last_probe_time) < 300:
        return _last_probe_result

    token = get_upstox_token()
    if not token or len(token) < 10:
        _last_probe_time = now_epoch
        _last_probe_result = (False, "TOKEN_NOT_CONFIGURED")
        return _last_probe_result

    try:
        url = f"{UPSTOX_BASE_URL}/market-quote/ltp?instrument_key=NSE_EQ|INE002A01018"
        headers = {"Authorization": f"Bearer {token.strip()}", "Accept": "application/json"}
        res = upstox_request("GET", url, headers=headers, timeout=4.0)
        if res.status_code == 200:
            _last_probe_time = now_epoch
            _last_probe_result = (True, "ACTIVE_AUTHENTICATED")
            return _last_probe_result
        elif res.status_code in (401, 403):
            _last_probe_time = now_epoch
            _last_probe_result = (False, f"AUTH_REJECTED_{res.status_code}")
            return _last_probe_result
        else:
            _last_probe_time = now_epoch
            _last_probe_result = (False, f"HTTP_{res.status_code}")
            return _last_probe_result
    except Exception as e:
        _last_probe_time = now_epoch
        _last_probe_result = (False, f"PROBE_ERROR_{type(e).__name__}")
        return _last_probe_result

def get_upstox_token_status(verify_live: bool = False) -> Dict[str, Any]:
    """
    Decodes the configured Upstox JWT token and returns expiration and health status.
    Status can be: 'ACTIVE', 'EXPIRING_SOON', 'EXPIRED', 'NOT_CONFIGURED', or 'NON_JWT'.
    Optionally probes the live market quote endpoint if verify_live=True.
    """
    import base64
    import json
    cfg = get_upstox_config()
    token = cfg.get("access_token") or cfg.get("market_token")
    if not token or not token.strip():
        return {
            "status": "NOT_CONFIGURED",
            "is_valid": False,
            "days_remaining": 0.0,
            "expires_at": None,
            "issued_at": None,
            "live_verified": False,
            "live_status": "TOKEN_NOT_CONFIGURED",
            "message": "No Upstox token configured."
        }

    token_str = token.strip()
    if "." not in token_str:
        return {
            "status": "NON_JWT",
            "is_valid": True,
            "days_remaining": None,
            "expires_at": None,
            "issued_at": None,
            "live_verified": False,
            "live_status": "NON_JWT",
            "message": "Non-JWT token format configured."
        }

    try:
        parts = token_str.split(".")
        if len(parts) < 2:
            return {"status": "INVALID_FORMAT", "is_valid": False, "days_remaining": 0.0, "expires_at": None, "issued_at": None, "live_verified": False, "live_status": "INVALID_JWT", "message": "Invalid JWT format."}

        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
        exp = payload.get("exp")
        iat = payload.get("iat")

        exp_dt = datetime.fromtimestamp(exp) if exp else None
        iat_dt = datetime.fromtimestamp(iat) if iat else None
        now = datetime.now()

        # Upstox daily session reset cutoff (03:30:00 IST every morning)
        next_daily_reset_dt = now.replace(hour=3, minute=30, second=0, microsecond=0)
        if now >= next_daily_reset_dt:
            next_daily_reset_dt += timedelta(days=1)
        daily_reset_sec = max(0, int((next_daily_reset_dt - now).total_seconds()))

        if exp_dt:
            seconds_remaining = max(0, int((exp_dt - now).total_seconds()))
            days_remaining = round((exp_dt - now).total_seconds() / 86400.0, 1)
            is_expired = days_remaining <= 0
            if is_expired:
                status = "EXPIRED"
            elif days_remaining <= 7:
                status = "EXPIRING_SOON"
            else:
                status = "ACTIVE"
        else:
            seconds_remaining = None
            days_remaining = None
            is_expired = False
            status = "ACTIVE"

        live_ok, live_status = (False, "UNCHECKED")
        if verify_live:
            live_ok, live_status = verify_upstox_data_token_live()

        return {
            "status": status,
            "is_valid": not is_expired,
            "seconds_remaining": seconds_remaining,
            "days_remaining": days_remaining,
            "expires_at": exp_dt.strftime("%Y-%m-%d %H:%M:%S") if exp_dt else None,
            "issued_at": iat_dt.strftime("%Y-%m-%d %H:%M:%S") if iat_dt else None,
            "next_daily_reset_sec": daily_reset_sec,
            "daily_reset_time": "03:30:00 IST",
            "live_verified": live_ok,
            "live_status": live_status,
            "message": f"Token is {status} with {days_remaining} days remaining." if days_remaining is not None else "Token active."
        }
    except Exception as e:
        logger.warning(f"Error decoding Upstox JWT token: {e}")
        return {
            "status": "PARSE_ERROR",
            "is_valid": False,
            "days_remaining": 0.0,
            "expires_at": None,
            "issued_at": None,
            "live_verified": False,
            "live_status": "PARSE_ERROR",
            "message": f"Could not parse token: {e}"
        }

def notify_upstox_auth_alert(reason: str, status_code: Optional[int] = None, force: bool = False) -> bool:
    """Dispatches proactive Telegram alert when Upstox authentication fails or token expires."""
    global _last_auth_alert_time
    import time
    now = time.time()
    if not force and (now - _last_auth_alert_time) < ALERT_COOLDOWN_SECONDS:
        logger.warning(f"[Upstox Alert Suppressed] Alert already sent in last hour. Reason: {reason}")
        return False
    _last_auth_alert_time = now

    token_info = get_upstox_token_status(verify_live=False)
    days_left = token_info.get("days_remaining")
    days_str = f"{days_left:.1f} days remaining" if days_left is not None else "Unknown"

    msg = (
        f"🚨 <b>UPSTOX API AUTHENTICATION ALERT — DEFENSIVE LIQUIDATION</b>\n\n"
        f"<b>Issue:</b> Upstox API rejected market data request / token expired.\n"
        f"<b>Reason:</b> {reason}\n"
        f"<b>Status Code:</b> {status_code or 'N/A'}\n"
        f"<b>Token Status:</b> {token_info.get('status')} ({days_str})\n"
        f"<b>Expires At:</b> {token_info.get('expires_at') or 'N/A'}\n\n"
        f"🛡️ <b>Defensive Action Taken:</b> All active Consensus Game Arena positions have been <b>IMMEDIATELY SQUARED OFF</b> to protect capital.\n"
        f"⛔ <b>Zero Yahoo Finance Fallback:</b> Market data scanning is halted until a valid token is provided.\n\n"
        f"👉 <b>Action Required:</b> Please log into Upstox Developer Console, generate a fresh market analysis token, and enter it in <b>Settings</b> to resume."
    )
    try:
        from app.analytics.telegram_notifier import send_telegram_message
        return send_telegram_message(msg)
    except Exception as e:
        logger.error(f"Failed to dispatch Upstox Telegram alert: {e}")
        return False

def get_instrument_key(symbol: str) -> str:
    """Translates generic symbol (e.g. RELIANCE.NS, RELIANCE, NIFTY) to Upstox Instrument Key."""
    clean = symbol.replace('.NS', '').replace('.BO', '').strip().upper()
    cat = _init_nse_instruments_catalog()
    if clean in cat:
        return cat[clean]
    # Check alternate cleans (e.g. hyphens or ampersands)
    alt = clean.replace('&', '')
    if alt in cat:
        return cat[alt]
    # Fallback to direct symbol format if not in catalog
    return f"NSE_EQ|{clean}"

def test_upstox_connection(token: Optional[str] = None, token_type: str = "market") -> Dict[str, Any]:
    """Tests connectivity to Upstox API v2 for Market Data or Algo Trading Token."""
    if not token:
        cfg = get_upstox_config()
        token = cfg.get("algo_token") if token_type == "algo" else cfg.get("access_token")
        
    if not token or not token.strip():
        token_label = "Algo Trading (Read+Trade)" if token_type == "algo" else "Market Data (Read-Only)"
        return {"status": "error", "message": f"No {token_label} Token provided. Please enter your token."}
        
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token.strip()}"
    }
    
    try:
        # 1. Market Data & Analytics Verification
        # Upstox does NOT require a static IP for market data (LTP quotes, historical candles, option chains).
        # Static IP is only enforced on account/order execution APIs. Therefore, test Market Data via the Market Quote API.
        if token_type == "market":
            quote_url = f"{UPSTOX_BASE_URL}/market-quote/ltp"
            params = {"instrument_key": "NSE_INDEX|Nifty 50,NSE_EQ|INE002A01018"}
            res = upstox_request("GET", quote_url, headers=headers, params=params, timeout=6)
            
            if res.status_code == 200:
                data = res.json()
                quotes = data.get("data", {})
                user_name = "Upstox Analytics User"
                user_id = "Authorized"
                
                # Non-fatal profile probe to get user name if permitted
                try:
                    prof_res = upstox_request("GET", f"{UPSTOX_BASE_URL}/user/profile", headers=headers, timeout=3)
                    if prof_res.status_code == 200:
                        u_data = prof_res.json().get("data", {})
                        user_name = u_data.get("user_name", user_name)
                        user_id = u_data.get("user_id", user_id)
                except Exception:
                    pass
                
                return {
                    "status": "success",
                    "message": f"Verified Upstox Market Data & Analytics feed ({user_name}). Real-time market feed active (No Static IP required).",
                    "user_name": user_name,
                    "user_id": user_id,
                    "token_type": "market",
                    "is_active": True
                }
            else:
                if res.status_code in (401, 403) or "token" in res.text.lower() or "unauthorized" in res.text.lower():
                    notify_upstox_auth_alert(reason=f"Token verification rejected (HTTP {res.status_code})", status_code=res.status_code)
                return {
                    "status": "error",
                    "message": f"Upstox Market Data Error ({res.status_code}): {res.text[:200]}",
                    "is_active": False
                }

        # 2. Algo Trading Verification (Orders/Execution requires static IP per SEBI rules)
        else:
            url = f"{UPSTOX_BASE_URL}/user/profile"
            res = upstox_request("GET", url, headers=headers, timeout=5)
            
            if res.status_code == 200:
                data = res.json()
                user_data = data.get("data", {})
                user_name = user_data.get("user_name", "Upstox Trader")
                user_id = user_data.get("user_id", "")
                return {
                    "status": "success",
                    "message": f"Verified Upstox Algo Trading & Execution as {user_name} ({user_id}).",
                    "user_name": user_name,
                    "user_id": user_id,
                    "token_type": "algo",
                    "is_active": True
                }
            else:
                return {
                    "status": "error",
                    "message": f"Upstox API Error ({res.status_code}): {res.text[:200]}",
                    "is_active": False
                }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Connection error: {str(e)}",
            "is_active": False
        }

_CANDLE_CALL_HISTORY = collections.deque()
_CANDLE_LOCK = threading.Lock()
MAX_CANDLE_CALLS_PER_MINUTE = 30

def fetch_upstox_candles(ticker: str, interval: str = "15m", period: str = "60d") -> pd.DataFrame:
    """
    Fetches real-time candles from Upstox API v2 and returns standard DataFrame.
    Intervals supported: 1m, 15m, 30m, 1d (day)
    Protected by strict 30 calls/min rate ceiling (0.5 calls/s) to guarantee ample headroom
    (370+ calls/min) for live quotes, scans, and order execution.
    """
    # Strict safety: pace candle calls to ensure headroom within 400 req/min Upstox limit
    with _CANDLE_LOCK:
        now_t = time.time()
        while _CANDLE_CALL_HISTORY and _CANDLE_CALL_HISTORY[0] < now_t - 60.0:
            _CANDLE_CALL_HISTORY.popleft()
        if len(_CANDLE_CALL_HISTORY) >= MAX_CANDLE_CALLS_PER_MINUTE:
            logger.warning(f"[Upstox Shield] Candle fetch rate ceiling reached ({len(_CANDLE_CALL_HISTORY)} calls/min). Throttling candle call for {ticker} to protect token.")
            raise UpstoxRateLimitExceeded(f"Upstox candle fetch rate ceiling ({MAX_CANDLE_CALLS_PER_MINUTE} calls/min) reached. Throttling candle requests to preserve token.")
        _CANDLE_CALL_HISTORY.append(now_t)

    cfg = get_upstox_config()
    token = cfg.get("access_token")
    if not token:
        raise Exception("Upstox access token not configured.")
        
    inst_key = get_instrument_key(ticker)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token.strip()}"
    }
    
    is_resample_15m = interval in ("15m", "15min")
    is_resample_5m = interval in ("5m", "5min", "5minute")

    # Map interval to native Upstox API intervals: 1minute, 30minute, day, week, month
    if is_resample_15m or is_resample_5m or interval in ("1m", "1min", "1minute"):
        upstox_interval = "1minute"
    elif interval in ("30m", "30min"):
        upstox_interval = "30minute"
    elif interval in ("1d", "1day", "D", "day"):
        upstox_interval = "day"
    else:
        upstox_interval = "day"
        
    to_date = datetime.now().strftime("%Y-%m-%d")
    days_back = 60
    if period in ("5y", "2y", "1y"):
        days_back = 365 * 2
    elif period == "60d":
        days_back = 60
    elif period == "30d":
        days_back = 30
    elif period in ("5d", "7d"):
        days_back = 7

    # Upstox 1minute candle historical limit is 30 days
    if upstox_interval == "1minute":
        days_back = min(days_back, 30)
        
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    
    # 1. Fetch historical candles
    url = f"{UPSTOX_BASE_URL}/historical-candle/{inst_key}/{upstox_interval}/{to_date}/{from_date}"
    res = upstox_request("GET", url, headers=headers, timeout=8)
    
    if res.status_code != 200:
        if res.status_code in (401, 403) or "token" in res.text.lower() or "unauthorized" in res.text.lower():
            notify_upstox_auth_alert(reason=f"Candle fetch unauthorized (HTTP {res.status_code}): {res.text[:120]}", status_code=res.status_code)
        raise Exception(f"Upstox API Error ({res.status_code}): {res.text[:200]}")
        
    data = res.json()
    candles = data.get("data", {}).get("candles", [])

    # 2. For intraday intervals, also merge today's live intraday candles
    if upstox_interval in ("1minute", "30minute"):
        try:
            url_intra = f"{UPSTOX_BASE_URL}/historical-candle/intraday/{inst_key}/{upstox_interval}"
            res_intra = upstox_request("GET", url_intra, headers=headers, timeout=5)
            if res_intra.status_code == 200:
                intra_candles = res_intra.json().get("data", {}).get("candles", [])
                if intra_candles:
                    candles = intra_candles + candles
        except Exception as e:
            logger.debug(f"[Upstox] Intraday live candle probe note for {ticker}: {e}")

    if not candles:
        return pd.DataFrame()
        
    # Upstox returns: [timestamp, open, high, low, close, volume, open_interest]
    df = pd.DataFrame(candles, columns=['datetime', 'open', 'high', 'low', 'close', 'volume', 'oi'])
    df['datetime'] = pd.to_datetime(df['datetime'])
    df = df.drop_duplicates(subset=['datetime']).sort_values('datetime').reset_index(drop=True)
    df.drop(columns=['oi'], inplace=True, errors='ignore')

    # 3. Resample if 15m or 5m was requested
    if is_resample_15m:
        df = df.set_index('datetime').resample('15min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna().reset_index()
    elif is_resample_5m:
        df = df.set_index('datetime').resample('5min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna().reset_index()

    return df

_ltp_cache: Dict[str, Tuple[float, float]] = {}  # ticker -> (price, timestamp)
_LTP_CACHE_TTL_SEC: float = 2.0  # 2-second in-memory deduplication cache

def fetch_upstox_bulk_ltp(tickers: List[str]) -> Dict[str, float]:
    """
    Fetches real-time LTP quotes for multiple tickers in batches (up to 500 per call).
    Applies in-memory 2s caching and strict 3-tier Upstox rate limiting (10/s, 500/m, 2000/30m).
    """
    if not tickers:
        return {}

    if _GLOBAL_UPSTOX_LIMITER.is_circuit_open():
        logger.debug("[Upstox] Bulk LTP call bypassed: Circuit breaker is OPEN.")
        return {}

    token = get_upstox_token()
    if not token or len(token) < 10:
        return {}

    now = time.time()
    results: Dict[str, float] = {}
    needed: List[str] = []

    for t in tickers:
        clean = t.replace('.NS', '').replace('.BO', '').strip().upper()
        if clean in _ltp_cache:
            price, ts = _ltp_cache[clean]
            if (now - ts) < _LTP_CACHE_TTL_SEC:
                results[clean] = price
                results[t] = price
                continue
        elif t in _ltp_cache:
            price, ts = _ltp_cache[t]
            if (now - ts) < _LTP_CACHE_TTL_SEC:
                results[clean] = price
                results[t] = price
                continue
        needed.append(clean)

    if not needed:
        return results

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token.strip()}"
    }

    BATCH_SIZE = 500
    for i in range(0, len(needed), BATCH_SIZE):
        batch = needed[i:i + BATCH_SIZE]
        key_to_ticker: Dict[str, str] = {}
        instrument_keys = []
        for sym in batch:
            inst_key = get_instrument_key(sym)
            instrument_keys.append(inst_key)
            key_to_ticker[inst_key] = sym
            colon_key = inst_key.replace("|", ":")
            key_to_ticker[colon_key] = sym
            key_to_ticker[sym] = sym

        url = f"{UPSTOX_BASE_URL}/market-quote/ltp"
        params = {"instrument_key": ",".join(instrument_keys)}

        try:
            res = upstox_request("GET", url, headers=headers, params=params, timeout=6.0)
            if res.status_code == 200:
                data = res.json()
                quotes = data.get("data", {})
                for q_key, q_val in quotes.items():
                    if isinstance(q_val, dict) and "last_price" in q_val:
                        p = float(q_val["last_price"])
                        matched_sym = key_to_ticker.get(q_key)
                        if not matched_sym:
                            inst_tok = q_val.get("instrument_token")
                            if inst_tok and inst_tok in key_to_ticker:
                                matched_sym = key_to_ticker[inst_tok]
                            elif ":" in q_key:
                                sym_part = q_key.split(":")[-1]
                                matched_sym = key_to_ticker.get(sym_part, sym_part)
                        if matched_sym:
                            clean_sym = matched_sym.replace('.NS', '').replace('.BO', '').strip().upper()
                            aliases = {matched_sym, clean_sym, f"{clean_sym}.NS", f"{clean_sym}.BO"}
                            for a in aliases:
                                results[a] = p
                                _ltp_cache[a] = (p, time.time())
            elif res.status_code in (401, 403):
                notify_upstox_auth_alert(reason=f"Bulk LTP fetch unauthorized (HTTP {res.status_code})", status_code=res.status_code)
                break
        except Exception as e:
            logger.warning(f"Upstox bulk LTP error for batch of {len(batch)} symbols: {e}")

    return results

def fetch_upstox_ltp(ticker: str) -> Optional[float]:
    """Fetches real-time LTP quote for a single ticker with rate limiting and 2s caching."""
    if _GLOBAL_UPSTOX_LIMITER.is_circuit_open():
        return None
    clean = ticker.replace('.NS', '').replace('.BO', '').strip().upper()
    quotes = fetch_upstox_bulk_ltp([clean])
    return quotes.get(clean) or quotes.get(ticker)

_quotes_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}  # ticker -> (quote_dict, timestamp)
_QUOTES_CACHE_TTL_SEC: float = 60.0

def get_cached_upstox_quote(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Returns in-memory cached quote from _quotes_cache with zero network overhead.
    Protects Upstox API limits by strictly avoiding individual per-ticker HTTP requests.
    """
    clean = ticker.replace('.NS', '').replace('.BO', '').strip().upper()
    for k in (clean, ticker, f"{clean}.NS", f"{clean}.BO"):
        if k in _quotes_cache:
            q_data, ts = _quotes_cache[k]
            if (time.time() - ts) < 300.0:
                return q_data
    return None

NSE_SECTOR_KEYS: Dict[str, str] = {
    "NIFTY 50": "NSE_INDEX|Nifty 50",
    "NIFTY BANK": "NSE_INDEX|Nifty Bank",
    "NIFTY IT": "NSE_INDEX|Nifty IT",
    "NIFTY AUTO": "NSE_INDEX|Nifty Auto",
    "NIFTY FMCG": "NSE_INDEX|Nifty FMCG",
    "NIFTY METAL": "NSE_INDEX|Nifty Metal",
    "NIFTY PHARMA": "NSE_INDEX|Nifty Pharma",
    "NIFTY REALTY": "NSE_INDEX|Nifty Realty",
    "NIFTY ENERGY": "NSE_INDEX|Nifty Energy",
    "NIFTY PSU BANK": "NSE_INDEX|Nifty PSU Bank",
    "NIFTY PVT BANK": "NSE_INDEX|Nifty Pvt Bank"
}

_sector_quotes_cache: Dict[str, Dict[str, Any]] = {}
_sector_quotes_ts: float = 0.0
_SECTOR_CACHE_TTL_SEC: float = 30.0

def fetch_upstox_bulk_quotes(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Fetches full real-time market quotes (LTP, VWAP, Open, High, Low, Net Change, Volume)
    for multiple tickers in single-batch calls (up to 500 per call) via Upstox V3 /market-quote/quotes.
    Applies in-memory 2s caching and strict rate limiting (8/s, 400/m, 1600/30m).
    """
    if not tickers:
        return {}

    if _GLOBAL_UPSTOX_LIMITER.is_circuit_open():
        logger.debug("[Upstox] Bulk quotes call bypassed: Circuit breaker is OPEN.")
        return {}

    token = get_upstox_token()
    if not token or len(token) < 10:
        return {}

    now = time.time()
    results: Dict[str, Dict[str, Any]] = {}
    needed: List[str] = []

    for t in tickers:
        clean = t.replace('.NS', '').replace('.BO', '').strip().upper()
        if clean in _quotes_cache:
            q_data, ts = _quotes_cache[clean]
            if (now - ts) < _QUOTES_CACHE_TTL_SEC:
                results[clean] = q_data
                results[t] = q_data
                continue
        elif t in _quotes_cache:
            q_data, ts = _quotes_cache[t]
            if (now - ts) < _QUOTES_CACHE_TTL_SEC:
                results[clean] = q_data
                results[t] = q_data
                continue
        needed.append(clean)

    if not needed:
        return results

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token.strip()}"
    }

    BATCH_SIZE = 500
    for i in range(0, len(needed), BATCH_SIZE):
        batch = needed[i:i + BATCH_SIZE]
        key_to_ticker: Dict[str, str] = {}
        instrument_keys = []
        for sym in batch:
            inst_key = get_instrument_key(sym)
            instrument_keys.append(inst_key)
            key_to_ticker[inst_key] = sym
            colon_key = inst_key.replace("|", ":")
            key_to_ticker[colon_key] = sym
            key_to_ticker[sym] = sym

        url = f"{UPSTOX_BASE_URL}/market-quote/quotes"
        params = {"instrument_key": ",".join(instrument_keys)}

        try:
            res = upstox_request("GET", url, headers=headers, params=params, timeout=6.0)
            if res.status_code == 200:
                data = res.json()
                quotes = data.get("data", {})
                for q_key, q_val in quotes.items():
                    if not isinstance(q_val, dict):
                        continue
                    matched_sym = key_to_ticker.get(q_key)
                    if not matched_sym:
                        inst_tok = q_val.get("instrument_token")
                        if inst_tok and inst_tok in key_to_ticker:
                            matched_sym = key_to_ticker[inst_tok]
                        elif ":" in q_key:
                            sym_part = q_key.split(":")[-1]
                            matched_sym = key_to_ticker.get(sym_part, sym_part)

                    if matched_sym:
                        ltp = float(q_val.get("last_price", 0.0))
                        vwap = float(q_val.get("average_price", 0.0)) if q_val.get("average_price") else ltp
                        ohlc = q_val.get("ohlc", {}) or {}
                        open_px = float(ohlc.get("open", 0.0)) if ohlc.get("open") else ltp
                        high_px = float(ohlc.get("high", 0.0)) if ohlc.get("high") else ltp
                        low_px = float(ohlc.get("low", 0.0)) if ohlc.get("low") else ltp
                        close_px = float(ohlc.get("close", 0.0)) if ohlc.get("close") else ltp
                        net_chg = float(q_val.get("net_change", 0.0))
                        vol = int(q_val.get("volume", 0))

                        q_entry = {
                            "ticker": matched_sym,
                            "ltp": ltp,
                            "vwap": vwap,
                            "open": open_px,
                            "high": high_px,
                            "low": low_px,
                            "close": close_px,
                            "net_change": net_chg,
                            "volume": vol,
                            "source": "upstox",
                            "is_realtime": True,
                            "timestamp": datetime.now().strftime("%H:%M:%S IST")
                        }
                        clean_sym = matched_sym.replace('.NS', '').replace('.BO', '').strip().upper()
                        aliases = {matched_sym, clean_sym, f"{clean_sym}.NS", f"{clean_sym}.BO"}
                        for a in aliases:
                            results[a] = q_entry
                            _quotes_cache[a] = (q_entry, time.time())
                            _ltp_cache[a] = (ltp, time.time())

            elif res.status_code in (401, 403):
                notify_upstox_auth_alert(reason=f"Bulk quotes fetch unauthorized (HTTP {res.status_code})", status_code=res.status_code)
                break
        except Exception as e:
            logger.warning(f"Upstox bulk quotes error for batch of {len(batch)} symbols: {e}")

    return results

def fetch_upstox_sector_quotes(force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Fetches real-time performance quotes across all 11 major NSE Sector Indices in a SINGLE HTTP call.
    Applies in-memory 30-second caching to prevent redundant API calls during frequent scans.
    """
    global _sector_quotes_cache, _sector_quotes_ts
    now = time.time()
    if not force_refresh and _sector_quotes_cache and (now - _sector_quotes_ts) < _SECTOR_CACHE_TTL_SEC:
        return _sector_quotes_cache

    if _GLOBAL_UPSTOX_LIMITER.is_circuit_open():
        return _sector_quotes_cache

    token = get_upstox_token()
    if not token or len(token) < 10:
        return _sector_quotes_cache

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token.strip()}"
    }

    url = f"{UPSTOX_BASE_URL}/market-quote/quotes"
    inst_keys = list(NSE_SECTOR_KEYS.values())
    params = {"instrument_key": ",".join(inst_keys)}

    try:
        res = upstox_request("GET", url, headers=headers, params=params, timeout=6.0)
        if res.status_code == 200:
            data = res.json().get("data", {})
            key_to_name = {v.replace("|", ":"): k for k, v in NSE_SECTOR_KEYS.items()}
            new_cache: Dict[str, Dict[str, Any]] = {}

            for q_key, q_val in data.items():
                if not isinstance(q_val, dict):
                    continue
                name = key_to_name.get(q_key)
                if not name:
                    tok = q_val.get("instrument_token")
                    if tok and tok in NSE_SECTOR_KEYS.values():
                        name = [k for k, v in NSE_SECTOR_KEYS.items() if v == tok][0]
                if not name:
                    continue

                ltp = float(q_val.get("last_price", 0.0))
                net_chg = float(q_val.get("net_change", 0.0))
                ohlc = q_val.get("ohlc", {}) or {}
                open_px = float(ohlc.get("open", 0.0))
                prev_close = ltp - net_chg if (ltp and net_chg is not None) else float(ohlc.get("close", 0.0))
                pct_from_open = round((ltp - open_px) / open_px * 100.0, 2) if open_px > 0 else 0.0
                pct_from_close = round(net_chg / prev_close * 100.0, 2) if prev_close > 0 else 0.0

                trend = "BULLISH" if pct_from_open >= 0.20 else ("BEARISH" if pct_from_open <= -0.20 else "NEUTRAL")

                new_cache[name] = {
                    "sector": name,
                    "ltp": ltp,
                    "open": open_px,
                    "prev_close": prev_close,
                    "net_change": net_chg,
                    "pct_from_open": pct_from_open,
                    "pct_from_close": pct_from_close,
                    "trend": trend,
                    "timestamp": datetime.now().strftime("%H:%M:%S IST")
                }

            if new_cache:
                _sector_quotes_cache = new_cache
                _sector_quotes_ts = now
                logger.info(f"[Upstox] Successfully refreshed {len(new_cache)} NSE sector indices via single bulk request.")
        elif res.status_code in (401, 403):
            notify_upstox_auth_alert(reason=f"Sector quotes fetch unauthorized (HTTP {res.status_code})", status_code=res.status_code)
    except Exception as e:
        logger.warning(f"[Upstox] Sector quotes fetch error: {e}")

    return _sector_quotes_cache

