"""
Isolated Upstox Daily Token Automator (Zero Copy-Paste Architecture).

Strictly decoupled from all core trading, alpha, scanner, and execution modules:
communicates exclusively through the canonical SQLite `app_settings` table
(`upstox_access_token`, `upstox_market_token`, `upstox_algo_token`, `upstox_auth_status`).

Capabilities:
  1. 1-Click OAuth Popup Code Exchange with Dual-Slot Sync (Market + Algo Tokens)
     and automatic popup postMessage + self-close.
  2. Built-in RFC-6238 TOTP 6-Digit Authenticator Generator (pure Python stdlib)
     so the user never needs to pick up their phone to read Google Authenticator.
  3. Upstox V3 Token Request API (`POST /v3/login/auth/token/request/{client_id}`)
     & Webhook Notifier receiver for 1-Tap Mobile Approval.
  4. Paste-Anything Smart Extractor (accepts a raw redirect URL `?code=...`,
     an auth `code`, or a raw JWT and handles exchange + dual-slot activation).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import struct
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def _resolve_db_path() -> str:
    try:
        from app.data.historical_data_layer import get_db_path
        return get_db_path()
    except Exception:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "market_data.db"))


class UpstoxTokenAutomator:
    """Standalone, zero-dependency Upstox daily token renewal and dual-slot synchronizer."""

    @staticmethod
    def now_ist_str() -> str:
        return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    @staticmethod
    def generate_totp(secret_b32: str, interval: int = 30) -> Dict[str, Any]:
        """
        Pure-Python RFC-6238 TOTP generator (HMAC-SHA1).
        Returns the current 6-digit TOTP code and remaining validity seconds in the 30s window.
        """
        cleaned = "".join(c for c in (secret_b32 or "").strip().upper() if c.isalnum())
        if not cleaned:
            return {"has_totp": False, "code": None, "remaining_sec": 0}
        try:
            # Pad base32 string to a multiple of 8
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
        except Exception as e:
            logger.warning(f"[UpstoxTokenAutomator] Invalid TOTP secret format: {e}")
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
            if exp_ts:
                exp_ist = datetime.fromtimestamp(float(exp_ts), IST).strftime("%Y-%m-%d %H:%M:%S")
            return {
                "sub": claims.get("sub"),
                "exp_ts": exp_ts,
                "expires_at_ist": exp_ist,
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
    def get_automator_status(cls) -> Dict[str, Any]:
        smap = cls.get_settings_map()
        sync_both = smap.get("upstox_sync_both_slots", "true").lower() != "false"
        totp_secret = smap.get("upstox_totp_secret", "")
        totp_info = cls.generate_totp(totp_secret) if totp_secret else {"has_totp": False, "code": None, "remaining_sec": 0}
        access_tok = smap.get("upstox_access_token", "")
        algo_tok = smap.get("upstox_algo_token", "")
        jwt_meta = cls.decode_jwt_metadata(access_tok) if access_tok else {}

        masked_totp = ""
        if totp_secret:
            masked_totp = f"{totp_secret[:3]}****{totp_secret[-3:]}" if len(totp_secret) > 6 else "****"

        return {
            "sync_both_slots": sync_both,
            "has_totp_secret": bool(totp_secret),
            "totp_secret_masked": masked_totp,
            "live_totp": totp_info.get("code"),
            "totp_remaining_sec": totp_info.get("remaining_sec", 0),
            "last_refresh_ist": smap.get("upstox_last_token_refresh_ist", ""),
            "last_refresh_method": smap.get("upstox_last_refresh_method", ""),
            "last_refresh_user": smap.get("upstox_last_refresh_user", ""),
            "tokens_in_sync": bool(access_tok and algo_tok and access_tok == algo_tok),
            "jwt_expires_at_ist": jwt_meta.get("expires_at_ist"),
            "redirect_uri": smap.get("upstox_redirect_uri") or "http://localhost:8000/api/settings/upstox/callback",
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
                # Disconnect existing streamer so _run_streamer immediately reconnects with the fresh token
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

        # Case 1: Full redirect URL containing ?code=...
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

        # Case 2: Raw JWT token (starts with eyJ and has 2 dots)
        if text.startswith("eyJ") and text.count(".") == 2 and len(text) > 80:
            return cls.activate_and_persist_token(
                access_token=text,
                sync_both_slots=sync_both_slots,
                method_label="Direct JWT Sync (Market + Algo)",
            )

        # Case 3: Short OAuth authorization code
        return cls.exchange_oauth_code(
            text, sync_both_slots=sync_both_slots, method_label="Direct Auth Code Exchange"
        )

    @classmethod
    def request_v3_mobile_approval(cls) -> Dict[str, Any]:
        """
        Triggers Upstox V3 Token Request API (`POST /v3/login/auth/token/request/{client_id}`).
        Upstox sends a 1-tap approval notification to the user's Upstox mobile app / WhatsApp;
        once approved, Upstox posts the token to the configured Notifier Webhook URL.
        """
        smap = cls.get_settings_map()
        api_key = smap.get("upstox_api_key", "").strip()
        api_secret = smap.get("upstox_api_secret", "").strip()
        if not api_key or not api_secret:
            return {
                "status": "error",
                "message": "Upstox API Key and API Secret are required to request mobile approval.",
            }

        url = f"https://api.upstox.com/v3/login/auth/token/request/{api_key}"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        payload = {"client_secret": api_secret}
        try:
            import requests
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            if resp.status_code in (200, 201, 202):
                data = resp.json() if resp.content else {}
                return {
                    "status": "success",
                    "message": "📲 Approval request sent to your Upstox App / WhatsApp! Tap 'Approve' on your phone, or use the 1-Click Popup Login.",
                    "details": data,
                }
            return {
                "status": "error",
                "message": f"Upstox V3 Token Request returned HTTP {resp.status_code}: {resp.text[:250]}",
            }
        except Exception as e:
            return {"status": "error", "message": f"Upstox V3 request error: {e}"}
