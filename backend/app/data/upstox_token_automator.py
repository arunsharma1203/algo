"""
Isolated Upstox Daily Token Automator (Zero Copy-Paste & Zero-Click Architecture).

Strictly decoupled from all core trading, alpha, scanner, and execution modules:
communicates exclusively through the canonical SQLite `app_settings` table
(`upstox_access_token`, `upstox_market_token`, `upstox_algo_token`, `upstox_auth_status`).

Capabilities:
  1. 100% Autonomous Headless Morning Login (Zero-Click):
     Uses local Headless Chrome (CDP) + saved Mobile Number, 6-Digit PIN, and
     RFC-6238 TOTP Secret to log in automatically every morning (or whenever
     the token expires) without any human intervention.
  2. 1-Click OAuth Popup Code Exchange with Dual-Slot Sync (Market + Algo Tokens)
     and automatic popup postMessage + self-close.
  3. Built-in RFC-6238 TOTP 6-Digit Authenticator Generator (pure Python stdlib).
  4. Paste-Anything Smart Extractor (accepts a raw redirect URL `?code=...`,
     an auth `code`, or a raw JWT and handles exchange + dual-slot activation).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

_DAEMON_LOCK = threading.Lock()
_DAEMON_STARTED = False
_AUTO_LOGIN_IN_PROGRESS = False
_LAST_AUTO_ATTEMPT_TS = 0.0


def _resolve_db_path() -> str:
    try:
        from app.data.historical_data_layer import get_db_path
        return get_db_path()
    except Exception:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "market_data.db"))


def _find_chrome_binary() -> Optional[str]:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class UpstoxTokenAutomator:
    """Standalone, zero-dependency Upstox daily token renewal and dual-slot synchronizer."""

    @staticmethod
    def now_ist() -> datetime:
        return datetime.now(IST)

    @classmethod
    def now_ist_str(cls) -> str:
        return cls.now_ist().strftime("%Y-%m-%d %H:%M:%S IST")

    @staticmethod
    def generate_totp(secret_b32: str, interval: int = 30) -> Dict[str, Any]:
        """
        Pure-Python RFC-6238 TOTP generator (HMAC-SHA1).
        Returns the current 6-digit TOTP code and remaining validity seconds in the 30s window.
        """
        cleaned = "".join(c for c in (secret_b32 or "").strip().upper() if c.isalnum())
        if not cleaned:
            return {"has_totp": False, "code": None, "remaining_sec": 0}
        if cleaned.isdigit() and len(cleaned) <= 8:
            return {
                "has_totp": False,
                "code": None,
                "remaining_sec": 0,
                "error": "A 6-digit PIN/OTP was entered instead of the 16-32 char Base32 TOTP Secret Key.",
            }
        try:
            pad_len = (8 - (len(cleaned) % 8)) % 8
            key = base64.b32decode(cleaned + ("=" * pad_len), casefold=True)
            now_int = int(time.time())
            counter = now_int // interval
            rem_sec = interval - (now_int % interval)

            msg = struct.pack(">Q", counter)
            digest = hmac.new(key, msg, hashlib.sha1).digest()
            offset = digest[-1] & 0x0F
            binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
            otp = f"{binary % 1000000:06d}"
            return {
                "has_totp": True,
                "code": otp,
                "remaining_sec": rem_sec,
            }
        except Exception:
            return {"has_totp": False, "code": None, "remaining_sec": 0, "error": "Invalid Base32 TOTP secret"}

    @staticmethod
    def decode_jwt_metadata(token: str) -> Dict[str, Any]:
        """Extracts non-sensitive JWT claims (sub, exp, issued_at) without external libraries."""
        try:
            parts = (token or "").strip().split(".")
            if len(parts) < 2:
                return {}
            payload_b64 = parts[1]
            pad = "=" * ((4 - len(payload_b64) % 4) % 4)
            raw = base64.urlsafe_b64decode(payload_b64 + pad)
            claims = json.loads(raw.decode("utf-8"))
            exp_ts = claims.get("exp")
            exp_ist = None
            is_expired = False
            if exp_ts:
                exp_f = float(exp_ts)
                exp_ist = datetime.fromtimestamp(exp_f, IST).strftime("%Y-%m-%d %H:%M:%S IST")
                is_expired = time.time() >= (exp_f - 60)
            return {
                "sub": claims.get("sub"),
                "exp_ts": exp_ts,
                "expires_at_ist": exp_ist,
                "is_expired": is_expired,
            }
        except Exception:
            return {}

    @classmethod
    def get_settings_map(cls) -> Dict[str, str]:
        conn = sqlite3.connect(_resolve_db_path(), timeout=5.0)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("SELECT key, value FROM app_settings WHERE key LIKE 'upstox_%'")
        rows = cur.fetchall()
        conn.close()
        return {str(k): (str(v) if v is not None else "") for k, v in rows}

    @classmethod
    def _set_setting(cls, key: str, value: str) -> None:
        conn = sqlite3.connect(_resolve_db_path(), timeout=5.0)
        with conn:
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
        conn.close()

    @classmethod
    def get_automator_status(cls) -> Dict[str, Any]:
        cls.ensure_background_daemon_started()
        smap = cls.get_settings_map()
        sync_both = smap.get("upstox_sync_both_slots", "true").lower() != "false"
        auto_login_enabled = smap.get("upstox_auto_login_enabled", "true").lower() != "false"
        totp_secret = smap.get("upstox_totp_secret", "").strip()
        mobile_number = smap.get("upstox_mobile_number", "").strip()
        pin = smap.get("upstox_pin", "").strip()

        totp_info = cls.generate_totp(totp_secret) if totp_secret else {"has_totp": False, "code": None, "remaining_sec": 0}
        access_tok = smap.get("upstox_access_token", "")
        algo_tok = smap.get("upstox_algo_token", "")
        jwt_meta = cls.decode_jwt_metadata(access_tok) if access_tok else {}

        masked_totp = f"{totp_secret[:3]}****{totp_secret[-3:]}" if len(totp_secret) > 6 else ("****" if totp_secret else "")
        masked_mobile = f"******{mobile_number[-4:]}" if len(mobile_number) >= 4 else ""
        has_pin = len(pin) >= 4

        has_full_totp_vault = bool(
            smap.get("upstox_api_key", "").strip()
            and smap.get("upstox_api_secret", "").strip()
            and len(mobile_number) == 10
            and has_pin
            and totp_info.get("has_totp")
        )
        persistent_profile_dir = os.path.expanduser("~/.gemini/antigravity/upstox_chrome_profile")
        has_saved_session = os.path.exists(os.path.join(persistent_profile_dir, "Default", "Cookies"))
        auto_login_ready = bool(
            smap.get("upstox_api_key", "").strip()
            and smap.get("upstox_api_secret", "").strip()
            and has_pin
        )

        return {
            "sync_both_slots": sync_both,
            "auto_login_enabled": auto_login_enabled,
            "auto_login_ready": auto_login_ready,
            "has_full_totp_vault": has_full_totp_vault,
            "has_saved_session": has_saved_session,
            "auto_login_in_progress": _AUTO_LOGIN_IN_PROGRESS,
            "has_mobile_number": len(mobile_number) == 10,
            "mobile_number_masked": masked_mobile,
            "has_pin": has_pin,
            "has_totp_secret": bool(totp_info.get("has_totp")),
            "totp_secret_masked": masked_totp if totp_info.get("has_totp") else "",
            "totp_error": totp_info.get("error"),
            "live_totp": totp_info.get("code"),
            "totp_remaining_sec": totp_info.get("remaining_sec", 0),
            "last_refresh_ist": smap.get("upstox_last_token_refresh_ist", ""),
            "last_refresh_method": smap.get("upstox_last_refresh_method", ""),
            "last_refresh_user": smap.get("upstox_last_refresh_user", ""),
            "last_auto_login_status": smap.get("upstox_last_auto_login_status", ""),
            "tokens_in_sync": bool(access_tok and algo_tok and access_tok == algo_tok),
            "jwt_expires_at_ist": jwt_meta.get("expires_at_ist"),
            "jwt_is_expired": bool(jwt_meta.get("is_expired", False)),
            "redirect_uri": smap.get("upstox_redirect_uri") or "http://localhost:8000/api/settings/upstox/callback",
            "chrome_available": bool(_find_chrome_binary()),
        }

    @classmethod
    def save_automator_config(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        conn = sqlite3.connect(_resolve_db_path(), timeout=5.0)
        with conn:
            if "sync_both_slots" in payload and payload["sync_both_slots"] is not None:
                val = "true" if bool(payload["sync_both_slots"]) else "false"
                conn.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_sync_both_slots', ?)",
                    (val,),
                )
            if "auto_login_enabled" in payload and payload["auto_login_enabled"] is not None:
                val = "true" if bool(payload["auto_login_enabled"]) else "false"
                conn.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_auto_login_enabled', ?)",
                    (val,),
                )
            if "mobile_number" in payload and payload["mobile_number"] is not None:
                raw_mob = "".join(c for c in str(payload["mobile_number"]).strip() if c.isdigit())
                if str(payload["mobile_number"]).strip() == "__CLEAR__":
                    conn.execute("DELETE FROM app_settings WHERE key = 'upstox_mobile_number'")
                elif len(raw_mob) == 10:
                    conn.execute(
                        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_mobile_number', ?)",
                        (raw_mob,),
                    )
            if "pin" in payload and payload["pin"] is not None:
                raw_pin = "".join(c for c in str(payload["pin"]).strip() if c.isdigit())
                if str(payload["pin"]).strip() == "__CLEAR__":
                    conn.execute("DELETE FROM app_settings WHERE key = 'upstox_pin'")
                elif len(raw_pin) >= 4:
                    conn.execute(
                        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_pin', ?)",
                        (raw_pin,),
                    )
            if "totp_secret" in payload and payload["totp_secret"] is not None:
                raw_secret = str(payload["totp_secret"]).strip().replace(" ", "")
                if raw_secret == "__CLEAR__":
                    conn.execute("DELETE FROM app_settings WHERE key = 'upstox_totp_secret'")
                elif raw_secret and "*" not in raw_secret:
                    conn.execute(
                        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_totp_secret', ?)",
                        (raw_secret,),
                    )
        conn.close()
        return cls.get_automator_status()

    @classmethod
    def activate_and_persist_token(
        cls,
        access_token: str,
        sync_both_slots: Optional[bool] = None,
        target_slot: str = "both",
        user_name: str = "Upstox Trader",
        method_label: str = "1-Click OAuth",
    ) -> Dict[str, Any]:
        """
        Atomically writes the fresh JWT access_token to SQLite `app_settings`,
        clears all auth failure / rate-limit lockouts, and hot-reloads the WebSocket stream.
        """
        tok = (access_token or "").strip()
        if len(tok) < 20:
            return {"status": "error", "message": "Invalid or empty access token received."}

        smap = cls.get_settings_map()
        if sync_both_slots is None:
            sync_both_slots = smap.get("upstox_sync_both_slots", "true").lower() != "false"

        now_str = cls.now_ist_str()
        conn = sqlite3.connect(_resolve_db_path(), timeout=5.0)
        with conn:
            if sync_both_slots or target_slot in ("both", "market"):
                conn.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_access_token', ?)",
                    (tok,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_market_token', ?)",
                    (tok,),
                )
            if sync_both_slots or target_slot in ("both", "algo"):
                conn.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_algo_token', ?)",
                    (tok,),
                )

            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_auth_status', 'ACTIVE')")
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('market_data_source', 'upstox')")
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_last_token_refresh_ist', ?)",
                (now_str,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_last_refresh_method', ?)",
                (method_label,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_last_auto_login_status', ?)",
                (f"SUCCESS at {now_str} via {method_label}",),
            )
            if user_name:
                conn.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_last_refresh_user', ?)",
                    (user_name,),
                )
            conn.execute(
                "DELETE FROM app_settings WHERE key IN ('upstox_auth_error', 'upstox_auth_error_time', 'upstox_circuit_cooldown_until')"
            )
        conn.close()

        # Reset rate limiter circuit breaker & hot-reload WebSocket V3 stream
        try:
            from app.data.upstox_provider import get_upstox_limiter
            get_upstox_limiter().reset_circuit_breaker()
        except Exception as e:
            logger.debug(f"[UpstoxTokenAutomator] Limiter reset note: {e}")

        stream_reconnected = False
        try:
            from app.data.upstox_stream_service import get_stream_service
            svc = get_stream_service()
            if not svc._is_running:
                svc.start()
                stream_reconnected = True
            elif svc._streamer:
                try:
                    svc._streamer.disconnect()
                    stream_reconnected = True
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[UpstoxTokenAutomator] Stream hot-reload note: {e}")

        jwt_meta = cls.decode_jwt_metadata(tok)
        slots_updated = (
            ["Market Data Token", "Algo Trading Token"]
            if (sync_both_slots or target_slot == "both")
            else ([target_slot.upper()])
        )
        logger.info(
            f"[UpstoxTokenAutomator] ✅ Activated fresh Upstox token via {method_label} "
            f"for {user_name} ({', '.join(slots_updated)})."
        )
        return {
            "status": "success",
            "user_name": user_name,
            "method": method_label,
            "slots_updated": slots_updated,
            "refreshed_at_ist": now_str,
            "expires_at_ist": jwt_meta.get("expires_at_ist"),
            "stream_reconnected": stream_reconnected,
            "message": f"Upstox token activated across {' & '.join(slots_updated)} ({now_str}).",
        }

    @classmethod
    def exchange_oauth_code(
        cls,
        code: str,
        sync_both_slots: Optional[bool] = None,
        method_label: str = "1-Click OAuth Popup",
    ) -> Dict[str, Any]:
        """Exchanges an Upstox OAuth2 authorization `code` for an `access_token` and activates it."""
        clean_code = (code or "").strip()
        if not clean_code:
            return {"status": "error", "message": "Missing authorization code from Upstox."}

        smap = cls.get_settings_map()
        api_key = smap.get("upstox_api_key", "").strip()
        api_secret = smap.get("upstox_api_secret", "").strip()
        redirect_uri = smap.get("upstox_redirect_uri", "").strip() or "http://localhost:8000/api/settings/upstox/callback"

        if not api_key or not api_secret:
            return {
                "status": "error",
                "message": "Upstox API Key or API Secret is missing in Settings. Save them once and retry.",
            }

        token_url = "https://api.upstox.com/v2/login/authorization/token"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "code": clean_code,
            "client_id": api_key,
            "client_secret": api_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }

        try:
            import requests
            resp = requests.post(token_url, headers=headers, data=data, timeout=12)
            if resp.status_code == 200:
                body = resp.json()
                access_token = body.get("access_token")
                user_name = body.get("user_name") or body.get("user_id") or "Upstox Trader"
                if access_token:
                    return cls.activate_and_persist_token(
                        access_token=access_token,
                        sync_both_slots=sync_both_slots,
                        user_name=str(user_name),
                        method_label=method_label,
                    )
            return {
                "status": "error",
                "http_status": resp.status_code,
                "message": f"Upstox token exchange failed (HTTP {resp.status_code}): {resp.text[:300]}",
            }
        except Exception as e:
            return {"status": "error", "message": f"Network error during Upstox code exchange: {e}"}

    @classmethod
    def smart_exchange_or_activate(cls, raw_input: str, sync_both_slots: Optional[bool] = None) -> Dict[str, Any]:
        """
        Accepts ANY of:
          1. A full callback URL (e.g. `http://localhost:8000/api/settings/upstox/callback?code=XYZ`)
          2. A short OAuth authorization `code` (`XYZ`)
          3. A full JWT `access_token` (`eyJ...`)
        Automatically detects which one was provided, exchanges if needed, and activates all slots!
        """
        text = (raw_input or "").strip()
        if not text:
            return {"status": "error", "message": "Empty input provided."}

        if "code=" in text:
            try:
                parsed = urlparse(text)
                qs = parse_qs(parsed.query)
                code_val = (qs.get("code") or [None])[0]
                if code_val:
                    return cls.exchange_oauth_code(
                        code_val, sync_both_slots=sync_both_slots, method_label="Redirect URL Code Exchange"
                    )
            except Exception:
                pass

        if text.startswith("eyJ") and text.count(".") == 2 and len(text) > 80:
            return cls.activate_and_persist_token(
                access_token=text,
                sync_both_slots=sync_both_slots,
                method_label="Direct JWT Sync (Market + Algo)",
            )

        return cls.exchange_oauth_code(
            text, sync_both_slots=sync_both_slots, method_label="Direct Auth Code Exchange"
        )

    @classmethod
    def run_headless_auto_login(
        cls,
        trigger_source: str = "Autonomous Headless Auto-Login",
        allow_interactive_fallback: bool = True,
    ) -> Dict[str, Any]:
        """
        Autonomous Upstox Login via Chrome DevTools Protocol (CDP) with Persistent Device Session:
          1. Uses a persistent isolated Chrome profile (`~/.gemini/antigravity/upstox_chrome_profile`)
             so Upstox device/session recognition persists across days.
          2. If Mobile + TOTP + PIN are all configured (or if the persistent session goes straight to
             the 6-Digit PIN screen), runs 100% headlessly in the background.
          3. If Upstox requests Mobile/OTP and TOTP Secret is not yet configured, launches a compact
             CDP-driven Chrome window, auto-captures the 10-digit Mobile Number for future runs,
             automatically fills the saved 6-digit PIN (`#pinCode`) the instant the PIN screen appears,
             exchanges the OAuth code, activates both Market & Algo tokens, and auto-closes the window.
        """
        global _AUTO_LOGIN_IN_PROGRESS, _LAST_AUTO_ATTEMPT_TS
        if _AUTO_LOGIN_IN_PROGRESS:
            return {"status": "busy", "message": "Autonomous login is already running in the background."}

        smap = cls.get_settings_map()
        api_key = smap.get("upstox_api_key", "").strip()
        api_secret = smap.get("upstox_api_secret", "").strip()
        redirect_uri = smap.get("upstox_redirect_uri", "").strip() or "http://localhost:8000/api/settings/upstox/callback"
        mobile = smap.get("upstox_mobile_number", "").strip()
        pin = smap.get("upstox_pin", "").strip()
        totp_secret = smap.get("upstox_totp_secret", "").strip()
        has_totp = bool(totp_secret and cls.generate_totp(totp_secret).get("has_totp"))

        if not api_key or not api_secret:
            msg = "Cannot run automatic login — Upstox API Key & Secret are missing."
            cls._set_setting("upstox_last_auto_login_status", f"SKIPPED: {msg}")
            return {"status": "error", "message": msg}
        if len(pin) < 4:
            msg = "Cannot run automatic login — 6-Digit Upstox PIN is missing in the Vault."
            cls._set_setting("upstox_last_auto_login_status", f"SKIPPED: {msg}")
            return {"status": "error", "message": msg}

        chrome_bin = _find_chrome_binary()
        if not chrome_bin:
            msg = "Google Chrome binary not found on this machine for automation."
            cls._set_setting("upstox_last_auto_login_status", f"ERROR: {msg}")
            return {"status": "error", "message": msg}

        _AUTO_LOGIN_IN_PROGRESS = True
        _LAST_AUTO_ATTEMPT_TS = time.time()
        pre_refresh_ts = smap.get("upstox_last_token_refresh_ist", "")
        profile_dir = os.path.expanduser("~/.gemini/antigravity/upstox_chrome_profile")
        os.makedirs(profile_dir, exist_ok=True)
        # Clean stale SingletonLock if any terminated Chrome left it behind
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                lock_p = os.path.join(profile_dir, lock_name)
                if os.path.lexists(lock_p):
                    os.unlink(lock_p)
            except Exception:
                pass

        auth_url = (
            f"https://api.upstox.com/v2/login/authorization/dialog"
            f"?response_type=code&client_id={api_key}&redirect_uri={redirect_uri}"
        )

        # Decide whether to start in headless mode or Assisted Auto-PIN App Window mode:
        session_verified_marker = os.path.join(profile_dir, ".upstox_session_verified")
        has_verified_cookies = os.path.exists(session_verified_marker)
        start_headless = (len(mobile) == 10 and has_totp) or has_verified_cookies or (not allow_interactive_fallback)

        def _drive_chrome_session(headless_mode: bool, max_wait_sec: float) -> Dict[str, Any]:
            nonlocal mobile
            port = _get_free_port()
            proc = None
            ws = None
            try:
                import requests
                import websocket

                cmd = [
                    chrome_bin,
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions",
                    "--disable-popup-blocking",
                    f"--remote-debugging-port={port}",
                    "--remote-allow-origins=*",
                    f"--user-data-dir={profile_dir}",
                ]
                if headless_mode:
                    cmd.extend(["--headless=new", "--window-size=1280,900", auth_url])
                else:
                    cmd.extend(["--window-size=520,700", f"--app={auth_url}"])

                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if not headless_mode:
                    try:
                        subprocess.Popen(
                            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    except Exception:
                        pass

                def _connect_ws(max_tries: int = 30) -> Any:
                    for _ in range(max_tries):
                        if proc.poll() is not None:
                            return None
                        time.sleep(0.3)
                        try:
                            r = requests.get(f"http://127.0.0.1:{port}/json", timeout=1.5)
                            if r.status_code == 200:
                                targets = r.json()
                                for t in targets:
                                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                                        return websocket.create_connection(t["webSocketDebuggerUrl"], timeout=8)
                        except Exception:
                            pass
                    return None

                ws = _connect_ws(max_tries=30)
                if not ws:
                    return {"status": "error", "message": "Chrome DevTools WebSocket did not respond in time."}

                msg_id = 0

                def cdp_call(method: str, params: Dict[str, Any]) -> Any:
                    nonlocal msg_id, ws
                    for _ in range(2):
                        if ws is None:
                            ws = _connect_ws(max_tries=12)
                            if ws is None:
                                return None
                        try:
                            msg_id += 1
                            cur_id = msg_id
                            payload = {"id": cur_id, "method": method, "params": params}
                            ws.send(json.dumps(payload))
                            deadline = time.time() + 5.0
                            while time.time() < deadline:
                                raw = ws.recv()
                                data = json.loads(raw)
                                if data.get("id") == cur_id:
                                    return data.get("result", {})
                            return None
                        except Exception:
                            try:
                                if ws is not None:
                                    ws.close()
                            except Exception:
                                pass
                            ws = None
                            time.sleep(0.25)
                    return None

                def cdp_eval(js_expr: str) -> Any:
                    res = cdp_call("Runtime.evaluate", {"expression": js_expr, "returnByValue": True})
                    if isinstance(res, dict):
                        return res.get("result", {}).get("value")
                    return None

                def cdp_type_and_submit(focus_js: str, text_val: str, submit_js: str) -> bool:
                    focused = cdp_eval(focus_js)
                    if not focused:
                        return False
                    time.sleep(0.15)
                    for ch in str(text_val):
                        cdp_call("Input.insertText", {"text": ch})
                        time.sleep(0.035)
                    time.sleep(0.25)
                    cdp_eval(submit_js)
                    return True

                step_mobile_done = False
                step_mobile_ts = 0.0
                step_totp_done = False
                step_pin_done = False
                step_pin_ts = 0.0
                last_error_text = ""
                last_seen_state = "INIT"

                start_loop = time.time()
                while time.time() - start_loop < max_wait_sec:
                    # 1. Check if localhost callback ALREADY received the code and updated SQLite!
                    cur_smap = cls.get_settings_map()
                    cur_refresh = cur_smap.get("upstox_last_token_refresh_ist", "")
                    if cur_refresh and cur_refresh != pre_refresh_ts and cur_smap.get("upstox_auth_status") == "ACTIVE":
                        try:
                            with open(session_verified_marker, "w") as mf:
                                mf.write(cur_refresh)
                        except Exception:
                            pass
                        cls._set_setting("upstox_last_refresh_method", trigger_source)
                        cls._set_setting(
                            "upstox_last_auto_login_status",
                            f"SUCCESS at {cur_refresh} ({trigger_source})",
                        )
                        return {
                            "status": "success",
                            "message": f"✅ Autonomous login succeeded! Both Market & Algo tokens refreshed ({cur_refresh}).",
                            "refreshed_at_ist": cur_refresh,
                            "user_name": cur_smap.get("upstox_last_refresh_user", "Upstox Trader"),
                        }

                    if proc.poll() is not None:
                        time.sleep(0.5)
                        break

                    totp_bundle = cls.generate_totp(totp_secret) if has_totp else {}
                    if totp_bundle.get("remaining_sec", 30) <= 2 and not step_totp_done and has_totp:
                        time.sleep(2.2)
                        totp_bundle = cls.generate_totp(totp_secret)
                    live_otp = totp_bundle.get("code") or ""

                    js_step = """
                    (function() {
                        var href = window.location.href || '';
                        if (href.indexOf('code=') !== -1) {
                            return { state: 'REDIRECTED', href: href };
                        }
                        var bodyTxt = (document.body && document.body.innerText) ? document.body.innerText : '';
                        var errEl = document.querySelector('.error-message, [class*="error"], [id*="error"]');
                        var errTxt = errEl && errEl.innerText ? errEl.innerText.trim() : '';

                        var pinInput = document.querySelector('#pinCode, input[name="pinCode"], input[type="password"], input[id*="pin" i]');
                        var otpInput = document.querySelector('#otpNum, input[name="otpNum"], input[id*="otp" i]');
                        var mobInput = document.querySelector('#mobileNum, input[name="mobileNum"], input[id*="mobile" i]');

                        var visibleInputs = Array.from(document.querySelectorAll('input')).filter(function(i) {
                            return i.offsetParent !== null && i.type !== 'hidden' && i.type !== 'checkbox';
                        });
                        if (!pinInput && visibleInputs.length > 0 && /6-digit PIN|enter.*pin|passcode/i.test(bodyTxt)) {
                            pinInput = visibleInputs[0];
                        }
                        if (!otpInput && visibleInputs.length > 0 && /enter.*otp|one time password|sent to/i.test(bodyTxt)) {
                            otpInput = visibleInputs[0];
                        }
                        if (!mobInput && visibleInputs.length > 0 && /mobile number|phone number|10-digit/i.test(bodyTxt)) {
                            mobInput = visibleInputs[0];
                        }

                        var typedMob = mobInput && mobInput.value ? String(mobInput.value).replace(/\\D/g, '') : '';

                        if (pinInput && pinInput.offsetParent !== null) {
                            return { state: 'PIN_SCREEN', err: errTxt, typedMob: typedMob };
                        }
                        if (otpInput && otpInput.offsetParent !== null) {
                            return { state: 'OTP_SCREEN', err: errTxt, typedMob: typedMob };
                        }
                        if (mobInput && mobInput.offsetParent !== null) {
                            return { state: 'MOBILE_SCREEN', err: errTxt, typedMob: typedMob };
                        }
                        return { state: 'LOADING', href: href, err: errTxt };
                    })();
                    """
                    state_obj = cdp_eval(js_step) or {}
                    st = state_obj.get("state", "LOADING")
                    if st != "LOADING":
                        last_seen_state = st
                    if state_obj.get("err"):
                        last_error_text = state_obj["err"]

                    # Auto-capture 10-digit mobile number if user typed it once so future runs have it!
                    typed_mob = str(state_obj.get("typedMob") or "")
                    if len(typed_mob) == 10 and len(mobile) != 10:
                        mobile = typed_mob
                        cls._set_setting("upstox_mobile_number", mobile)

                    if st == "REDIRECTED":
                        href = state_obj.get("href", "")
                        res = cls.smart_exchange_or_activate(href)
                        if res.get("status") == "success":
                            try:
                                with open(session_verified_marker, "w") as mf:
                                    mf.write(cls.now_ist_str())
                            except Exception:
                                pass
                            return res

                    elif st == "MOBILE_SCREEN":
                        if headless_mode and not has_totp:
                            return {"status": "needs_interactive", "reason": "MOBILE_SCREEN"}
                        if len(mobile) == 10 and (not step_mobile_done or (time.time() - step_mobile_ts > 6.0 and not typed_mob)):
                            focus_mob_js = """
                            (function() {
                                var el = document.querySelector('#mobileNum, input[name="mobileNum"], input[id*="mobile" i]')
                                      || Array.from(document.querySelectorAll('input')).find(function(i){ return i.offsetParent !== null && i.type !== 'hidden'; });
                                if (!el) return false;
                                el.focus();
                                el.value = '';
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                return true;
                            })();
                            """
                            submit_mob_js = """
                            (function() {
                                var btn = document.querySelector('#getOtp, button[type="submit"], button');
                                if (btn) { btn.click(); return true; }
                                return false;
                            })();
                            """
                            if cdp_type_and_submit(focus_mob_js, mobile, submit_mob_js):
                                step_mobile_done = True
                                step_mobile_ts = time.time()
                                time.sleep(1.2)
                                continue
                        elif headless_mode:
                            return {"status": "needs_interactive", "reason": "MOBILE_SCREEN"}

                    elif st == "OTP_SCREEN":
                        if live_otp and not step_totp_done:
                            focus_otp_js = """
                            (function() {
                                var el = document.querySelector('#otpNum, input[name="otpNum"], input[id*="otp" i]')
                                      || Array.from(document.querySelectorAll('input')).find(function(i){ return i.offsetParent !== null && i.type !== 'hidden'; });
                                if (!el) return false;
                                el.focus();
                                return true;
                            })();
                            """
                            submit_otp_js = """
                            (function() {
                                var btn = document.querySelector('#continueBtn, button[type="submit"], button');
                                if (btn) { btn.click(); return true; }
                                return false;
                            })();
                            """
                            if cdp_type_and_submit(focus_otp_js, live_otp, submit_otp_js):
                                step_totp_done = True
                                time.sleep(1.2)
                                continue
                        elif headless_mode and not live_otp:
                            return {"status": "needs_interactive", "reason": "OTP_SCREEN"}

                    elif st == "PIN_SCREEN" and (not step_pin_done or (time.time() - step_pin_ts > 5.0)):
                        focus_pin_js = """
                        (function() {
                            var el = document.querySelector('#pinCode, input[name="pinCode"], input[type="password"], input[id*="pin" i]')
                                  || Array.from(document.querySelectorAll('input')).find(function(i){ return i.offsetParent !== null && i.type !== 'hidden'; });
                            if (!el) return false;
                            el.focus();
                            return true;
                        })();
                        """
                        submit_pin_js = """
                        (function() {
                            var btn = document.querySelector('#pinContinueBtn, button[type="submit"], button');
                            if (btn) { btn.click(); return true; }
                            return false;
                        })();
                        """
                        if cdp_type_and_submit(focus_pin_js, pin, submit_pin_js):
                            step_pin_done = True
                            step_pin_ts = time.time()
                            time.sleep(1.5)
                            continue

                    time.sleep(0.45)

                final_smap = cls.get_settings_map()
                final_refresh = final_smap.get("upstox_last_token_refresh_ist", "")
                if final_refresh and final_refresh != pre_refresh_ts and final_smap.get("upstox_auth_status") == "ACTIVE":
                    try:
                        with open(session_verified_marker, "w") as mf:
                            mf.write(final_refresh)
                    except Exception:
                        pass
                    return {
                        "status": "success",
                        "message": f"✅ Autonomous login succeeded! Both Market & Algo tokens refreshed ({final_refresh}).",
                        "refreshed_at_ist": final_refresh,
                        "user_name": final_smap.get("upstox_last_refresh_user", "Upstox Trader"),
                    }

                err_detail = f" (Upstox page: {last_error_text})" if last_error_text else ""
                return {
                    "status": "error",
                    "message": (
                        f"Auto-login timed out at step [State={last_seen_state}, Mobile={step_mobile_done}, OTP={step_totp_done}, PIN={step_pin_done}]{err_detail}."
                    ),
                }
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                if proc is not None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

        try:
            cls._set_setting("upstox_last_auto_login_status", f"RUNNING ({cls.now_ist_str()})...")
            if start_headless:
                res = _drive_chrome_session(headless_mode=True, max_wait_sec=28.0)
                if res.get("status") == "success":
                    return res
                if res.get("status") == "needs_interactive" and allow_interactive_fallback:
                    logger.info(
                        f"[UpstoxTokenAutomator] Persistent profile needs one-time {res.get('reason')} verification; "
                        f"launching Auto-PIN Assisted window..."
                    )
                    res = _drive_chrome_session(headless_mode=False, max_wait_sec=180.0)
                    return res
                return res
            elif allow_interactive_fallback:
                res = _drive_chrome_session(headless_mode=False, max_wait_sec=180.0)
                return res
            else:
                return {
                    "status": "error",
                    "message": "Headless auto-login requires Mobile + TOTP Secret or an established persistent session.",
                }
        except Exception as e:
            err_msg = f"Autonomous login exception: {e}"
            logger.warning(f"[UpstoxTokenAutomator] {err_msg}")
            cls._set_setting("upstox_last_auto_login_status", f"ERROR: {err_msg}")
            return {"status": "error", "message": err_msg}
        finally:
            _AUTO_LOGIN_IN_PROGRESS = False

    @classmethod
    def _needs_daily_refresh(cls, smap: Dict[str, str]) -> bool:
        """
        Determines whether today's Upstox token is missing or expired.
        Upstox expires all tokens at 03:30 AM IST daily.
        """
        tok = smap.get("upstox_access_token", "").strip()
        if not tok:
            return True
        if smap.get("upstox_auth_status", "") == "TOKEN_EXPIRED":
            return True
        jwt_meta = cls.decode_jwt_metadata(tok)
        if jwt_meta.get("is_expired"):
            return True
        return False

    @classmethod
    def ensure_background_daemon_started(cls) -> None:
        """
        Starts an isolated background daemon thread inside `UpstoxTokenAutomator`
        that checks every 60 seconds if today's token is expired (e.g. every morning
        after 08:45 AM IST or on server startup) and runs `run_headless_auto_login()`
        automatically on its own when credentials are saved.
        """
        global _DAEMON_STARTED
        with _DAEMON_LOCK:
            if _DAEMON_STARTED:
                return
            _DAEMON_STARTED = True

        def _daemon_loop():
            logger.info("[UpstoxTokenAutomator] 🤖 Autonomous Daily Token Daemon started (isolated).")
            while True:
                try:
                    time.sleep(45)
                    smap = cls.get_settings_map()
                    if smap.get("upstox_auto_login_enabled", "true").lower() == "false":
                        continue
                    # Cooldown of 15 minutes between automatic attempts
                    if time.time() - _LAST_AUTO_ATTEMPT_TS < 900:
                        continue
                    now_ist = cls.now_ist()
                    # Run automatically if token is expired/missing and we are past 04:00 AM IST (after 03:30 AM cutoff)
                    if now_ist.hour >= 4 and cls._needs_daily_refresh(smap):
                        mobile = smap.get("upstox_mobile_number", "").strip()
                        pin = smap.get("upstox_pin", "").strip()
                        totp_secret = smap.get("upstox_totp_secret", "").strip()
                        persistent_dir = os.path.expanduser("~/.gemini/antigravity/upstox_chrome_profile")
                        has_session = os.path.exists(os.path.join(persistent_dir, "Default", "Cookies"))
                        if len(pin) >= 4 and (has_session or (len(mobile) == 10 and totp_secret)):
                            logger.info(
                                "[UpstoxTokenAutomator] 🤖 Detected expired/missing Upstox token — "
                                "triggering Autonomous Headless Morning Login..."
                            )
                            cls.run_headless_auto_login(
                                trigger_source="Autonomous Morning Daemon",
                                allow_interactive_fallback=False,
                            )
                except Exception as e:
                    logger.debug(f"[UpstoxTokenAutomator] Daemon tick note: {e}")

        t = threading.Thread(target=_daemon_loop, name="upstox-auto-login-daemon", daemon=True)
        t.start()
